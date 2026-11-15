from __future__ import annotations

import threading
import time

from backend.runtime_control import (
    automatic_shutdown_enabled,
    clear_shutdown_configuration,
    configure_shutdown,
    register_browser_session,
    request_shutdown,
    runtime_status,
    unregister_browser_session,
)


def test_automatic_shutdown_waits_for_last_browser_and_grace_period():
    stopped = threading.Event()
    configure_shutdown(stopped.set, "private-token", automatic=True, grace_seconds=0.12)
    try:
        first = register_browser_session()
        second = register_browser_session()
        assert first and second
        assert runtime_status()["active_browser_sessions"] == 2

        unregister_browser_session(first)
        assert not stopped.wait(0.2)
        assert runtime_status()["active_browser_sessions"] == 1

        unregister_browser_session(second)
        assert stopped.wait(0.5)
        assert not automatic_shutdown_enabled()
    finally:
        clear_shutdown_configuration()


def test_reconnection_cancels_pending_automatic_shutdown():
    stopped = threading.Event()
    configure_shutdown(stopped.set, "private-token", automatic=True, grace_seconds=0.15)
    try:
        first = register_browser_session()
        assert first
        unregister_browser_session(first)
        time.sleep(0.04)

        replacement = register_browser_session()
        assert replacement
        assert not stopped.wait(0.25)

        unregister_browser_session(replacement)
        assert stopped.wait(0.5)
    finally:
        clear_shutdown_configuration()


def test_manual_shutdown_remains_available_when_automatic_mode_is_disabled():
    stopped = threading.Event()
    configure_shutdown(stopped.set, "private-token", automatic=False)
    try:
        assert register_browser_session() is None
        assert request_shutdown("incorrect-token") is False
        assert not stopped.is_set()
        assert request_shutdown("private-token") is True
        assert stopped.wait(0.2)
    finally:
        clear_shutdown_configuration()
