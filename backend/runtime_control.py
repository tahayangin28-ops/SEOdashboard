from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable


_shutdown_callback: Callable[[], None] | None = None
_shutdown_token: str | None = None
_automatic_shutdown = False
_shutdown_grace_seconds = 15.0
_browser_sessions: set[str] = set()
_has_seen_browser_session = False
_shutdown_timer: threading.Timer | None = None
_shutdown_deadline: float | None = None
_shutdown_started = False
_lock = threading.RLock()


def _cancel_shutdown_timer() -> None:
    global _shutdown_timer, _shutdown_deadline
    if _shutdown_timer:
        _shutdown_timer.cancel()
        _shutdown_timer = None
    _shutdown_deadline = None


def configure_shutdown(callback: Callable[[], None], token: str, *, automatic: bool = False, grace_seconds: float = 15.0) -> None:
    global _shutdown_callback, _shutdown_token, _automatic_shutdown, _shutdown_grace_seconds
    global _has_seen_browser_session, _shutdown_started
    with _lock:
        _cancel_shutdown_timer()
        _shutdown_callback = callback
        _shutdown_token = token
        _automatic_shutdown = automatic
        _shutdown_grace_seconds = max(0.1, float(grace_seconds))
        _browser_sessions.clear()
        _has_seen_browser_session = False
        _shutdown_started = False


def clear_shutdown_configuration() -> None:
    global _shutdown_callback, _shutdown_token, _automatic_shutdown
    global _has_seen_browser_session, _shutdown_started
    with _lock:
        _cancel_shutdown_timer()
        _browser_sessions.clear()
        _shutdown_callback = None
        _shutdown_token = None
        _automatic_shutdown = False
        _has_seen_browser_session = False
        _shutdown_started = False


def runtime_enabled() -> bool:
    with _lock:
        return _shutdown_callback is not None and _shutdown_token is not None


def automatic_shutdown_enabled() -> bool:
    with _lock:
        return runtime_enabled() and _automatic_shutdown and not _shutdown_started


def runtime_status() -> dict:
    with _lock:
        return {
            "local_runtime": _shutdown_callback is not None and _shutdown_token is not None,
            "automatic_shutdown": _automatic_shutdown and not _shutdown_started,
            "active_browser_sessions": len(_browser_sessions),
            "shutdown_grace_seconds": _shutdown_grace_seconds,
        }


def register_browser_session() -> str | None:
    global _has_seen_browser_session
    with _lock:
        if not automatic_shutdown_enabled():
            return None
        _cancel_shutdown_timer()
        session_id = secrets.token_urlsafe(18)
        _browser_sessions.add(session_id)
        _has_seen_browser_session = True
        return session_id


def browser_session_active(session_id: str) -> bool:
    with _lock:
        return session_id in _browser_sessions and not _shutdown_started


def _shutdown_if_browser_absent() -> None:
    global _shutdown_timer, _shutdown_deadline, _shutdown_started
    callback = None
    with _lock:
        _shutdown_timer = None
        if not _automatic_shutdown or not _has_seen_browser_session or _browser_sessions or _shutdown_started:
            _shutdown_deadline = None
            return
        # A timer that fires very late usually means the computer slept. Give
        # the browser one fresh grace window to reconnect after wake-up.
        if _shutdown_deadline is not None and time.monotonic() - _shutdown_deadline > max(5.0, _shutdown_grace_seconds):
            _shutdown_deadline = time.monotonic() + _shutdown_grace_seconds
            _shutdown_timer = threading.Timer(_shutdown_grace_seconds, _shutdown_if_browser_absent)
            _shutdown_timer.daemon = True
            _shutdown_timer.start()
            return
        _shutdown_deadline = None
        _shutdown_started = True
        callback = _shutdown_callback
    if callback:
        callback()


def unregister_browser_session(session_id: str) -> None:
    global _shutdown_timer, _shutdown_deadline
    with _lock:
        _browser_sessions.discard(session_id)
        if not _automatic_shutdown or not _has_seen_browser_session or _browser_sessions or _shutdown_started:
            return
        _cancel_shutdown_timer()
        _shutdown_deadline = time.monotonic() + _shutdown_grace_seconds
        _shutdown_timer = threading.Timer(_shutdown_grace_seconds, _shutdown_if_browser_absent)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def request_shutdown(token: str) -> bool:
    global _automatic_shutdown, _shutdown_started
    callback = None
    with _lock:
        if not runtime_enabled() or not hmac.compare_digest(token, _shutdown_token or ""):
            return False
        _automatic_shutdown = False
        _shutdown_started = True
        _cancel_shutdown_timer()
        callback = _shutdown_callback
    assert callback is not None
    callback()
    return True
