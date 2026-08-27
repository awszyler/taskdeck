from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class BodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, max_bytes: int, exempt_prefixes: tuple[str, ...] = ()):
        super().__init__(app)
        self._max = max_bytes
        # Path prefixes that bypass the global body cap. Used for
        # multipart upload endpoints which enforce their own per-file
        # ceiling and stream the body without buffering.
        self._exempt = tuple(exempt_prefixes)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self._exempt):
            return await call_next(request)
        cl = request.headers.get("content-length")
        if cl:
            try:
                size = int(cl)
            except ValueError:
                return JSONResponse({"detail": "invalid content-length"}, status_code=400)
            if size > self._max:
                return JSONResponse(
                    {"detail": f"body too large (>{self._max} bytes)"},
                    status_code=413,
                )
        return await call_next(request)
