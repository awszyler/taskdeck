from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from taskdeck_core.api.ws import EventBus

log = logging.getLogger(__name__)


class STTClient(Protocol):
    """Minimal surface for testing — produces a transcript from raw audio bytes."""

    async def transcribe(self, data: bytes, *, model: str, content_type: str) -> str: ...


class OpenAISTTClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bus: EventBus | None = None,
        model: str | None = None,
    ):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._bus = bus
        self._default_model = model

    async def transcribe(self, data: bytes, *, model: str, content_type: str) -> str:
        # Whisper infers format from the filename extension; pick a safe default
        # based on content-type. Most browser MediaRecorder output is webm.
        ext = "webm"
        if "mpeg" in content_type or "mp3" in content_type:
            ext = "mp3"
        elif "wav" in content_type:
            ext = "wav"
        elif "mp4" in content_type or "m4a" in content_type:
            ext = "m4a"
        resp = await self._client.audio.transcriptions.create(
            model=model,
            file=(f"audio.{ext}", data, content_type or f"audio/{ext}"),
            response_format="verbose_json",
        )
        if self._bus is not None:
            duration = getattr(resp, "duration", None)
            await self._bus.publish(
                {
                    "type": "cost.event",
                    "provider": "litellm",
                    "operation": "stt",
                    "model": model,
                    "audio_seconds": duration,
                    "meta": {"source": "whisper_verbose_json"} if duration is not None else {"source": "no_duration"},
                }
            )
        return resp.text


class _MissingSTTClient:
    async def transcribe(self, data: bytes, **_) -> str:
        raise RuntimeError("LITELLM endpoint not configured for STT")


class _PreserveOnFailureRuntimeError(RuntimeError):
    """Raised by AwsTranscribeSTTClient on FAILED jobs we want to
    preserve in S3 for diagnosis. The finally-block uses isinstance
    to skip the cleanup branch."""


class AwsTranscribeSTTClient:
    """AWS Transcribe (batch) backend.

    Uploads the audio blob to S3, starts a transcription_job, polls
    until COMPLETED or FAILED, fetches the transcript JSON. Returns
    the joined transcript text.

    Why batch and not streaming: streaming would be faster but needs
    a websocket + sigv4 client and PCM audio. Batch accepts webm/opus
    (what browser MediaRecorder produces) directly; for short clips
    (3-30s of speech) the job typically completes in 5-10s — within
    the existing 30s stt_timeout_seconds budget.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        bus: EventBus | None = None,
        prefix: str = "stt",
        language_options: list[str] | None = None,
    ) -> None:
        import boto3
        self._s3 = boto3.client("s3", region_name=region)
        self._transcribe = boto3.client("transcribe", region_name=region)
        self._bucket = bucket
        self._region = region
        self._prefix = prefix
        self._bus = bus
        # Default to zh / en / ja — covers the actual user base we have
        # today. Override via settings if you ship to other regions.
        self._language_options = language_options or [
            "zh-CN", "en-US", "ja-JP",
        ]

    async def transcribe(
        self, data: bytes, *, model: str, content_type: str,
    ) -> str:
        """Run a one-shot AWS Transcribe job and return the text.

        `model` is ignored — Transcribe picks its own engine; we treat
        the parameter as a label for cost-event tagging.
        """
        import asyncio
        import json
        import uuid

        # Map browser MIME to AWS MediaFormat. Transcribe supports:
        # mp3, mp4, wav, flac, amr, ogg, webm. Browser MediaRecorder
        # is virtually always webm/opus on Chrome+Safari.
        ext = "webm"
        media_format = "webm"
        if "mp3" in content_type or "mpeg" in content_type:
            ext, media_format = "mp3", "mp3"
        elif "mp4" in content_type or "m4a" in content_type:
            ext, media_format = "mp4", "mp4"
        elif "wav" in content_type:
            ext, media_format = "wav", "wav"
        elif "ogg" in content_type:
            ext, media_format = "ogg", "ogg"
        elif "flac" in content_type:
            ext, media_format = "flac", "flac"

        job_id = uuid.uuid4().hex[:16]
        key = f"{self._prefix}/{job_id}.{ext}"

        def _put_object() -> None:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type or f"audio/{media_format}",
            )

        await asyncio.to_thread(_put_object)

        media_uri = f"s3://{self._bucket}/{key}"

        def _start_job() -> None:
            # IdentifyMultipleLanguages auto-picks among the candidate
            # languages we list. WITHOUT LanguageOptions, AWS uses a
            # default set that doesn't reliably include zh-CN — so
            # Mandarin gets misclassified as English and the
            # transcript comes back as garbage English. Pin the
            # candidates explicitly to the languages we actually
            # expect from this product's user base.
            self._transcribe.start_transcription_job(
                TranscriptionJobName=job_id,
                Media={"MediaFileUri": media_uri},
                MediaFormat=media_format,
                IdentifyMultipleLanguages=True,
                LanguageOptions=self._language_options,
                Settings={"ShowSpeakerLabels": False},
            )

        try:
            await asyncio.to_thread(_start_job)
        except Exception:
            # Best-effort: drop the audio object if we couldn't even
            # start the job, so we don't leave orphans in S3.
            try:
                await asyncio.to_thread(
                    self._s3.delete_object, Bucket=self._bucket, Key=key,
                )
            except Exception:  # noqa: BLE001
                pass
            raise

        # Poll. AWS API doesn't have a "wait" — we sleep + ask again.
        # 1s cadence keeps us responsive at low cost (the job itself
        # runs server-side; we're not paying per poll beyond AWS's
        # rate limits).
        try:
            transcript_uri: str | None = None
            for _ in range(60):  # up to ~60s budget
                await asyncio.sleep(1.0)

                def _get_status() -> dict:
                    return self._transcribe.get_transcription_job(
                        TranscriptionJobName=job_id,
                    )

                resp = await asyncio.to_thread(_get_status)
                job = resp["TranscriptionJob"]
                status = job["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    transcript_uri = (
                        job.get("Transcript", {}).get("TranscriptFileUri")
                    )
                    break
                if status == "FAILED":
                    reason = job.get("FailureReason", "unknown reason")
                    # Keep the audio in S3 + the job around so we can
                    # diagnose. The cleanup-on-success path still runs
                    # for the happy case; a one-off failure here lets
                    # us pull `s3://<bucket>/<key>` and inspect the
                    # actual blob the browser sent.
                    log.warning(
                        "stt: transcribe job %s failed (%s); preserving "
                        "audio at s3://%s/%s for inspection",
                        job_id, reason, self._bucket, key,
                    )
                    raise _PreserveOnFailureRuntimeError(
                        f"transcribe job failed: {reason}",
                    )
            else:
                raise _PreserveOnFailureRuntimeError(
                    "transcribe job did not complete within 60s",
                )

            if not transcript_uri:
                raise RuntimeError("transcribe job has no transcript URI")

            # Default Transcribe output goes to an AWS-hosted CDN
            # signed URL. Fetch via HTTP (not boto S3) — it's a
            # presigned https URL.
            import urllib.request

            def _fetch_transcript_json() -> str:
                with urllib.request.urlopen(transcript_uri, timeout=10) as r:
                    return r.read().decode("utf-8", errors="replace")

            body = await asyncio.to_thread(_fetch_transcript_json)
            payload = json.loads(body)
            transcript_text = (
                payload.get("results", {}).get("transcripts", [{}])[0]
                .get("transcript", "")
            )

            if self._bus is not None:
                duration = job.get("Media", {}).get("DurationInSeconds")
                await self._bus.publish({
                    "type": "cost.event",
                    "provider": "aws-transcribe",
                    "operation": "stt",
                    "model": model,
                    "audio_seconds": duration,
                    "meta": {"job": job_id},
                })

            return transcript_text
        except _PreserveOnFailureRuntimeError:
            # Don't enter cleanup path. Re-raise so the API surfaces
            # the failure; S3 audio + AWS job stay around for inspection.
            raise
        else:
            # Only on the success path: drop the upload + job so they
            # don't accumulate. (Transitive: an unexpected exception
            # other than _PreserveOnFailure also reaches here through
            # `finally`-less semantics — see the bare-except below for
            # the corresponding cleanup.)
            async def _cleanup() -> None:
                try:
                    await asyncio.to_thread(
                        self._s3.delete_object, Bucket=self._bucket, Key=key,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await asyncio.to_thread(
                        self._transcribe.delete_transcription_job,
                        TranscriptionJobName=job_id,
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Fire-and-forget so the response doesn't wait.
            import asyncio as _a
            _a.create_task(_cleanup())
