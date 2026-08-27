import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type Phase = "idle" | "recording" | "transcribing" | "error";

/**
 * Voice input hook. Records audio via MediaRecorder, sends the blob
 * to /api/v1/stt (AWS Transcribe backend) on stop, surfaces the
 * resulting transcript.
 *
 * Why no Web Speech preview path: prior versions ran SpeechRecognition
 * in parallel with `lang="en-US"`. That hard-coded English caused
 * 中文 voice input to come back as nonsense English ("yo I must agent
 * Paul official Yahoo" for "用 hermes agent 帮我分析雅虎股票"). AWS
 * Transcribe with IdentifyMultipleLanguages handles zh/en/ja
 * automatically; we accept the 5-15s post-stop wait in exchange for
 * a single, accurate transcription source.
 */
export function useVoiceInput() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [transcript, setTranscript] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  // Wall-clock timestamp of recording start. Used to enforce a 1s
  // minimum on the client — AWS Transcribe rejects audio <0.5s with
  // an opaque error; we'd rather refuse client-side with a clear
  // "Hold longer" hint.
  const startedAtRef = useRef<number>(0);

  const start = useCallback(async () => {
    setError(null);
    setTranscript("");

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Pick a mimeType the browser claims to support, ordered by
      // what AWS Transcribe accepts most reliably:
      //   - mp4 (Safari): MediaFormat=mp4, AAC, always valid
      //   - webm/opus (Chrome): MediaFormat=webm, valid when we
      //     start(timeslice) so each chunk carries metadata
      //   - ogg/opus: fallback
      const candidates = [
        "audio/mp4",
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
      ];
      let chosen: string | undefined;
      for (const t of candidates) {
        if (
          typeof MediaRecorder !== "undefined" &&
          MediaRecorder.isTypeSupported &&
          MediaRecorder.isTypeSupported(t)
        ) {
          chosen = t;
          break;
        }
      }
      const mr = chosen
        ? new MediaRecorder(stream, { mimeType: chosen })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      // No timeslice. With start(timeslice), iOS Safari mp4 may emit
      // raw moof+mdat fragments without the leading ftyp+moov init
      // segment, producing a blob AWS Transcribe rejects as "input
      // media isn't valid". Calling start() without args fires one
      // ondataavailable at stop() with a complete container — works
      // on Safari mp4, Chrome webm, Firefox ogg.
      mr.start();
      mediaRecorderRef.current = mr;
      startedAtRef.current = Date.now();

      setPhase("recording");
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const stop = useCallback(async () => {
    if (phase !== "recording") return;

    const mr = mediaRecorderRef.current;
    if (!mr) {
      setPhase("idle");
      return;
    }
    const stopped = new Promise<Blob>((resolve) => {
      mr.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        mr.stream.getTracks().forEach((t) => t.stop());
        resolve(blob);
      };
    });
    mr.stop();
    const audio = await stopped;

    // AWS Transcribe rejects audio < 0.5s; 1s floor gives headroom.
    const recordedMs = Date.now() - (startedAtRef.current || Date.now());
    if (recordedMs < 1000) {
      setPhase("error");
      setError("Hold the mic for at least 1 second.");
      return;
    }

    setPhase("transcribing");
    try {
      const { transcript: t } = await api.transcribeAudio(audio);
      setTranscript(t);
      setPhase("idle");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [phase]);

  const reset = useCallback(() => {
    setTranscript("");
    setError(null);
    setPhase("idle");
  }, []);

  // Safety: 60s hard cap.
  useEffect(() => {
    if (phase !== "recording") return;
    const timer = window.setTimeout(() => { stop(); }, 60_000);
    return () => window.clearTimeout(timer);
  }, [phase, stop]);

  // hasWebSpeech kept in the return shape for API compatibility, but
  // always false now — we no longer use the SpeechRecognition path.
  return { phase, transcript, setTranscript, error, start, stop, reset, hasWebSpeech: false };
}
