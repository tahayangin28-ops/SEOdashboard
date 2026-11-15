from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


def get(url: str, timeout: float = 2) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status} for {url}")
        return response.read()


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def wait_until_ready(process: subprocess.Popen, data_dir: Path) -> str:
    runtime_file = data_dir / "runtime.json"
    for _ in range(180):
        if runtime_file.exists():
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            base = f"http://127.0.0.1:{runtime['port']}"
            try:
                health = json.loads(get(f"{base}/api/health"))
                index = get(base).decode("utf-8")
                if health.get("status") == "ok" and '<div id="root"></div>' in index:
                    return base
            except OSError:
                pass
        if process.poll() is not None:
            raise RuntimeError(f"Packaged application exited with {process.returncode}")
        time.sleep(0.25)
    raise RuntimeError("Packaged application did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="seo-dashboard-smoke-") as temporary:
        data_dir = Path(temporary)
        environment = {**os.environ, "SEO_DATA_DIR": str(data_dir)}
        process = subprocess.Popen([str(args.executable), "--no-browser"], env=environment)
        try:
            wait_until_ready(process, data_dir)
            stopped = subprocess.run([str(args.executable), "--stop"], env=environment, timeout=15)
            if stopped.returncode != 0:
                raise RuntimeError("Packaged stop command failed")
            process.wait(timeout=15)

            automatic_environment = {**environment, "SEO_AUTO_SHUTDOWN": "1", "SEO_AUTO_SHUTDOWN_GRACE": "0.5", "SEO_RUNTIME_PORT": str(available_port())}
            process = subprocess.Popen([str(args.executable), "--no-browser"], env=automatic_environment)
            base = wait_until_ready(process, data_dir)
            socket = connect(base.replace("http://", "ws://") + "/api/runtime/session", origin=base, legacy=True)
            socket.send("ping")
            if socket.recv(timeout=3) != "pong":
                raise RuntimeError("Packaged browser session heartbeat failed")
            socket.close()
            process.wait(timeout=15)
            if process.returncode != 0:
                raise RuntimeError(f"Packaged automatic shutdown exited with {process.returncode}")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
