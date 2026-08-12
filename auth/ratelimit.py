"""Tiny in-process rate limiter + lockout for the public auth surfaces.

The vault runs single-process (one uvicorn worker — required by the in-memory MCP session
manager), so a process-local limiter is sufficient and needs no external store. Protects
/admin/login, /authorize, and /register from online password-guessing and client spam.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def check(key: str, *, limit: int, window: float, lockout: float) -> tuple[bool, int]:
    """Record an attempt for `key`. Returns (allowed, retry_after_seconds).

    More than `limit` attempts inside `window` seconds trips a `lockout`-second block.
    """
    now = time.monotonic()
    with _lock:
        until = _locked_until.get(key, 0.0)
        if until > now:
            return False, int(until - now) + 1
        hits = [t for t in _hits.get(key, ()) if now - t < window]
        hits.append(now)
        _hits[key] = hits
        if len(hits) > limit:
            _locked_until[key] = now + lockout
            _hits[key] = []
            return False, int(lockout) + 1
        return True, 0


def clear(key: str) -> None:
    """Wipe a key's history after a successful auth, so a good login resets the counter."""
    with _lock:
        _hits.pop(key, None)
        _locked_until.pop(key, None)


def client_ip(request) -> str:
    """Best-effort caller IP behind Render's proxy (x-forwarded-for is the client chain)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
