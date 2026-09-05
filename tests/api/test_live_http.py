import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from easylearn.config import Settings
from easylearn.previews.service import PreviewService
from easylearn.storage import LocalStorage


@pytest.fixture(params=["toml", "yaml"])
def live_client(database_url, tmp_path, request, uvicorn_server):
    env = {key: value for key, value in os.environ.items() if not key.startswith("EASYLEARN_")}
    configuration = tmp_path / f"native.{request.param}"
    config_data = {"database_url": database_url, "storage_root": str(tmp_path)}
    configuration.write_text(
        "\n".join(f"{key} = {json.dumps(value)}" for key, value in config_data.items())
        if request.param == "toml"
        else yaml.safe_dump(config_data),
        encoding="utf-8",
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
            "-ConfigPath",
            str(configuration),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert migrated.returncode == 0, migrated.stderr
    with uvicorn_server(
        "easylearn.main:create_app",
        health_path="/health/ready",
        env=dict(env, EASYLEARN_CONFIG=str(configuration)),
        factory=True,
    ) as client:
        yield client


def test_real_uvicorn_process_accepts_pdf_upload(live_client, pdf_bytes):
    client = live_client
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
    document = client.post(
        "/api/v1/documents",
        json={"upload_id": response.json()["upload_id"]},
        headers={"Idempotency-Key": "native-document"},
    )
    assert document.status_code == 202
    status_url = document.headers["Location"]
    assert client.get(status_url).json()["status"] == "QUEUED"
    assert client.post(status_url + "/cancel").json()["status"] == "CANCELLED"
    retried = client.post(status_url + "/retry", headers={"Idempotency-Key": "native-retry"})
    assert retried.status_code == 202
    assert retried.json()["generation"] == 2


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"), reason="Real sample not configured"
)
async def test_user_paper_is_validated_and_served_unchanged_over_real_http(
    live_client, database, database_url, tmp_path
):
    source = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    assert digest == "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
    client = live_client
    upload = client.post(
        "/api/v1/uploads", json={"filename": source.name, "size": len(content), "sha256": digest}
    )
    assert upload.status_code == 201
    upload_id = upload.json()["upload_id"]
    upload_path = f"/api/v1/uploads/{upload_id}"
    assert (
        client.patch(
            upload_path + "/content", content=content, headers={"Upload-Offset": "0"}
        ).status_code
        == 204
    )
    assert client.post(upload_path + "/complete").status_code == 200
    accepted = client.post(
        "/api/v1/documents",
        json={"upload_id": upload_id},
        headers={"Idempotency-Key": "real-paper-preview"},
    )
    assert accepted.status_code == 202
    started = time.perf_counter()
    previews = PreviewService(database, LocalStorage(tmp_path), Settings(database_url=database_url))
    await previews.execute(UUID(accepted.json()["job_id"]), generation=1)
    document_path = f"/api/v1/documents/{accepted.json()['document_id']}"
    document = client.get(document_path).json()
    preview = document["preview_runs"][0]
    assert preview["status"] == "READY", client.get(accepted.headers["Location"]).json()
    assert len(preview["pages"]) == 27
    assert preview["preview_sha256"] == digest
    download_path = document_path + f"/assets/{preview['preview_asset_id']}"
    assert hashlib.sha256(client.get(download_path).content).hexdigest() == digest
    partial = client.get(download_path, headers={"Range": "bytes=0-7"})
    assert partial.status_code == 206
    assert partial.content == b"%PDF-1.5"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    print(f"27-page PDF preview and HTTP download: {time.perf_counter() - started:.2f}s")
