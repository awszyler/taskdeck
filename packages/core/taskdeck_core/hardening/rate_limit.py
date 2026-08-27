from __future__ import annotations

from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

# Module-level limiter instance shared by main.py and auth.py.
# Per-route limits are set via @limiter.limit() decorators.
# The limit string "30/minute" matches the default TD_RATE_LIMIT_AUTH_PER_MINUTE.
# Note: slowapi decorators take a static string, so the env-var setting tunes the
# SlowAPIMiddleware default_limits but not the per-route decorator limit here.
limiter = Limiter(key_func=get_remote_address, default_limits=[])


async def _rate_limit_handler(request, exc: Exception) -> JSONResponse:
    # Signature must accept Exception (FastAPI's add_exception_handler is generic
    # over base Exception, not the narrower RateLimitExceeded class).
    del request, exc
    return JSONResponse({"detail": "rate limited"}, status_code=429)


__all__ = ["limiter", "RateLimitExceeded", "SlowAPIMiddleware", "_rate_limit_handler"]
