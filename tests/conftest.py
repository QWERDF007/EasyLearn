import os
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
import pytest


@pytest.fixture
def uvicorn_server(unused_tcp_port_factory):
    """A real loopback process with bounded startup and guaranteed process cleanup."""

    @contextmanager
    def start(app, *, health_path, env=None, factory=False, app_dir=None):
        port = unused_tcp_port_factory()
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            app,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
        if factory:
            command.append("--factory")
        if app_dir is not None:
            command.extend(["--app-dir", str(app_dir)])
        process = subprocess.Popen(
            command,
            env=os.environ.copy() if env is None else env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as http:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        output, _ = process.communicate(timeout=1)
                        raise AssertionError(
                            "HTTP process exited before readiness: "
                            + output.decode(errors="replace")
                        )
                    try:
                        if http.get(health_path).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("HTTP process did not become ready")
                yield http
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    return start
