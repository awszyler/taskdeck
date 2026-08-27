"""download_attachments fail-loud tests (P-H Phase 6)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from taskdeck_proto.crp import TaskAttachment
from taskdeck_runner.attachments import (
    AttachmentError,
    download_attachments,
)


def _att(filename: str = "f.txt") -> TaskAttachment:
    return TaskAttachment(
        id="a1",
        filename=filename,
        content_type="text/plain",
        size_bytes=10,
    )


@pytest.mark.asyncio
async def test_no_attachments_returns_empty(tmp_path: Path):
    out = await download_attachments(
        cwd=tmp_path, attachments=[],
        core_http_url="http://core", bearer_token="t",
    )
    assert out == []
    # No inputs dir created either.
    assert not (tmp_path / ".taskdeck").exists()


@pytest.mark.asyncio
async def test_http_error_raises_attachment_error(tmp_path: Path):
    """Non-2xx response → fail-loud, raise AttachmentError listing
    the file + status code."""
    # Mock httpx.AsyncClient.stream to return a 404 response.
    class FakeResp:
        is_success = False
        status_code = 404

        async def aread(self) -> bytes:
            return b"not found"

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, **kw):
            return FakeStreamCtx()

    with patch("taskdeck_runner.attachments.httpx.AsyncClient", FakeClient):
        with pytest.raises(AttachmentError) as ei:
            await download_attachments(
                cwd=tmp_path,
                attachments=[_att("doc.pdf")],
                core_http_url="http://core",
                bearer_token="t",
            )
    err = ei.value
    assert len(err.failures) == 1
    name, reason = err.failures[0]
    assert name == "doc.pdf"
    assert "404" in reason


@pytest.mark.asyncio
async def test_network_error_raises_attachment_error(tmp_path: Path):
    """httpx.HTTPError mid-stream → AttachmentError."""
    class BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, **kw):
            raise httpx.ConnectError("connection refused")

    with patch("taskdeck_runner.attachments.httpx.AsyncClient", BoomClient):
        with pytest.raises(AttachmentError) as ei:
            await download_attachments(
                cwd=tmp_path,
                attachments=[_att("a.txt")],
                core_http_url="http://core",
                bearer_token="t",
            )
    name, reason = ei.value.failures[0]
    assert name == "a.txt"
    assert "network error" in reason


@pytest.mark.asyncio
async def test_partial_success_drops_partial_files(tmp_path: Path):
    """If file 1 succeeds but file 2 fails, the partial write of
    file 1 should be cleaned up (we abort the task anyway)."""
    chunks_by_url: dict[str, list[bytes]] = {
        "http://core/api/v1/attachments/ok/file": [b"hello"],
    }

    class FakeResp:
        def __init__(self, ok: bool, body: bytes = b""):
            self.is_success = ok
            self.status_code = 200 if ok else 500
            self._body = body

        async def aread(self) -> bytes:
            return self._body

        async def aiter_bytes(self, n: int):
            yield self._body

    class FakeStreamCtx:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, **kw):
            if "/ok/" in url:
                return FakeStreamCtx(FakeResp(True, b"hello"))
            return FakeStreamCtx(FakeResp(False, b"sad"))

    att_ok = TaskAttachment(id="ok", filename="ok.txt", content_type="text/plain", size_bytes=5)
    att_bad = TaskAttachment(id="bad", filename="bad.txt", content_type="text/plain", size_bytes=5)
    with patch("taskdeck_runner.attachments.httpx.AsyncClient", FakeClient):
        with pytest.raises(AttachmentError):
            await download_attachments(
                cwd=tmp_path,
                attachments=[att_ok, att_bad],
                core_http_url="http://core",
                bearer_token="t",
            )
    # Partial success file should not survive.
    assert not (tmp_path / ".taskdeck" / "inputs" / "ok.txt").exists()
