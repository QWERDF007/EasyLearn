import os
import subprocess
import sys
import time

import httpx


def test_real_uvicorn_process_accepts_pdf_upload(
    database_url, tmp_path, unused_tcp_port, pdf_bytes
):
    env = dict(
        os.environ, EASYLEARN_DATABASE_URL=database_url, EASYLEARN_STORAGE_ROOT=str(tmp_path)
    )
    migrated = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            "deploy/run.ps1",
            "-Action",
            "migrate",
            "-Python",
            sys.executable,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert migrated.returncode == 0, migrated.stderr
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "easylearn.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(unused_tcp_port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{unused_tcp_port}", timeout=5) as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, "Web process exited before readiness"
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Web process did not become ready")
            response = client.post(
                "/api/v1/uploads",
                json={
                    "filename": "test.pdf",
                    "size": len(pdf_bytes),
                    "sha256": "ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b",
                },
            )
            assert response.status_code == 201
            path = f"/api/v1/uploads/{response.json()['upload_id']}"
            assert (
                client.patch(
                    path + "/content", headers={"Upload-Offset": "0"}, content=pdf_bytes
                ).status_code
                == 204
            )
            assert client.post(path + "/complete").json()["status"] == "UPLOADED"
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
