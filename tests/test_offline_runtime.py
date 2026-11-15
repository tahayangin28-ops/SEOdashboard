from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from websockets.sync.client import connect


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("This sandbox does not permit local listening sockets")
        return candidate.getsockname()[1]


def request(url: str, *, payload: dict | None = None) -> tuple[int, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=3) as response:
        return response.status, json.loads(response.read())


def wait_for_runtime(process: subprocess.Popen, data_dir: Path) -> str:
    runtime_file = data_dir / "runtime.json"
    for _ in range(120):
        if runtime_file.exists():
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            base = f"http://127.0.0.1:{runtime['port']}"
            try:
                if request(f"{base}/api/health")[0] == 200:
                    return base
            except (OSError, urllib.error.URLError):
                pass
        if process.poll() is not None:
            raise AssertionError(f"Local runtime exited early with {process.returncode}")
        time.sleep(0.1)
    raise AssertionError("Local runtime did not become ready")


def start_runtime(data_dir: Path, port: int, *, automatic_shutdown: bool = False, grace_seconds: float = 15) -> tuple[subprocess.Popen, str, dict[str, str]]:
    environment = {**os.environ, "SEO_DATA_DIR": str(data_dir), "SEO_RUNTIME_PORT": str(port), "PYTHONPATH": str(Path.cwd())}
    if automatic_shutdown:
        environment["SEO_AUTO_SHUTDOWN"] = "1"
        environment["SEO_AUTO_SHUTDOWN_GRACE"] = str(grace_seconds)
    process = subprocess.Popen([sys.executable, "-m", "backend.launcher", "--no-browser"], env=environment)
    return process, wait_for_runtime(process, data_dir), environment


def stop_runtime(process: subprocess.Popen, environment: dict[str, str]) -> None:
    stopped = subprocess.run([sys.executable, "-m", "backend.launcher", "--stop"], env=environment, timeout=10)
    assert stopped.returncode == 0
    process.wait(timeout=10)


def test_local_runtime_persists_data_outside_application(tmp_path: Path):
    data_dir = tmp_path / "user-data"
    first, base, environment = start_runtime(data_dir, available_port())
    try:
        status, created = request(f"{base}/api/projects", payload={"name": "Offline project", "site_url": "https://example.com", "locale": "en-GB"})
        assert status == 201
    finally:
        stop_runtime(first, environment)

    assert (data_dir / "seo_workspace.db").is_file()
    second, base, environment = start_runtime(data_dir, available_port())
    try:
        status, projects = request(f"{base}/api/projects?include_archived=true")
        assert status == 200
        assert [project["id"] for project in projects] == [created["id"]]
    finally:
        stop_runtime(second, environment)


def test_local_runtime_stops_after_last_browser_session_disconnects(tmp_path: Path):
    process, base, environment = start_runtime(tmp_path / "automatic-user-data", available_port(), automatic_shutdown=True, grace_seconds=0.25)
    first = second = replacement = None
    try:
        status, runtime = request(f"{base}/api/runtime/status")
        assert status == 200
        assert runtime["automatic_shutdown"] is True
        websocket_url = base.replace("http://", "ws://") + "/api/runtime/session"
        first = connect(websocket_url, origin=base, legacy=True)
        second = connect(websocket_url, origin=base, legacy=True)
        first.send("ping")
        assert first.recv(timeout=2) == "pong"

        first.close()
        first = None
        time.sleep(0.45)
        assert process.poll() is None

        second.close()
        second = None
        time.sleep(0.08)
        replacement = connect(websocket_url, origin=base, legacy=True)
        time.sleep(0.35)
        assert process.poll() is None

        replacement.close()
        replacement = None
        process.wait(timeout=8)
        assert process.returncode == 0
        assert not (tmp_path / "automatic-user-data" / "runtime.json").exists()
    finally:
        if first:
            first.close()
        if second:
            second.close()
        if replacement:
            replacement.close()
        if process.poll() is None:
            stop_runtime(process, environment)
