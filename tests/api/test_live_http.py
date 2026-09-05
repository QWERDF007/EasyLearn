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
from easylearn.document_ir.schema import DocumentIR
from easylearn.jobs.service import JobService
from easylearn.parses.worker import ParseWorker
from easylearn.previews.service import PreviewService
from easylearn.storage import LocalStorage


@pytest.fixture(params=["toml", "yaml"])
def live_client(database_url, tmp_path, request, uvicorn_server, monkeypatch, native_mineru):
    env = {key: value for key, value in os.environ.items() if not key.startswith("EASYLEARN_")}
    configuration = tmp_path / f"native.{request.param}"
    template = Path(f"config.example.{request.param}").read_text(encoding="utf-8")
    if request.param == "toml":
        configuration_text = (
            f"database_url = {json.dumps(database_url)}\n"
            f"storage_root = {json.dumps(str(tmp_path))}\n"
            + "\n".join(
                line for line in template.splitlines() if not line.startswith("database_url =")
            )
        )
        configuration_text = configuration_text.replace(
            'base_url = "http://127.0.0.1:8001"',
            f'base_url = "{native_mineru.base_url}"\napi_key = "native-test-key"',
        )
    else:
        configuration_data = dict(
            yaml.safe_load(template), database_url=database_url, storage_root=str(tmp_path)
        )
        configuration_data["mineru"].update(
            base_url=str(native_mineru.base_url), api_key="native-test-key"
        )
        configuration_text = yaml.safe_dump(configuration_data)
    configuration.write_text(
        configuration_text,
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
    with monkeypatch.context() as context:
        for name in os.environ:
            if name.startswith("EASYLEARN_"):
                context.delenv(name)
        context.setenv("EASYLEARN_CONFIG", str(configuration))
        settings = Settings()
    with uvicorn_server(
        "easylearn.main:create_app",
        health_path="/health/ready",
        env=dict(env, EASYLEARN_CONFIG=str(configuration)),
        factory=True,
    ) as client:
        yield client, settings


@pytest.fixture
def native_mineru(uvicorn_server):
    with uvicorn_server(
        "protocol_server:app",
        health_path="/health",
        app_dir=Path("tests/mineru"),
        env=dict(
            os.environ, EASYLEARN_TEST_PARSE_RESULT="1", EASYLEARN_TEST_MINERU_KEY="native-test-key"
        ),
    ) as client:
        yield client


def test_real_uvicorn_process_accepts_pdf_upload(live_client, pdf_bytes):
    client, _ = live_client
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
    client, settings = live_client
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
    parse_path = document_path + "/parse-runs"
    parsed = client.post(
        parse_path,
        json={"preview_run_id": accepted.json()["preview_run_id"]},
        headers={"Idempotency-Key": "real-paper-parse"},
    )
    assert parsed.status_code == 202
    (run,) = client.get(parse_path).json()
    assert run["configuration"]["options"]["page_count"] == 27
    assert run["configuration"]["options"]["language"] == "en"
    assert run["configuration"]["profile_revision"] == "mineru-v1"
    assert Path(run["configuration"]["local_model"]["path"]) == Path(
        "D:/Models/MinerU2.5-Pro-2605-1.2B"
    )
    assert run["status"] == "QUEUED"
    fixed_preview = client.get(parse_path + f"/{parsed.json()['parse_run_id']}/preview")
    assert fixed_preview.status_code == 200
    assert hashlib.sha256(fixed_preview.content).hexdigest() == digest
    async with ParseWorker.open(JobService(database), LocalStorage(tmp_path), settings) as worker:
        await worker.execute(UUID(parsed.json()["job_id"]), generation=1)
    status = client.get(parsed.json()["status_url"]).json()
    assert status["status"] == "SUCCEEDED", status
    snapshot = client.get(parse_path + f"/{parsed.json()['parse_run_id']}/document-ir")
    assert snapshot.status_code == 200
    ir = DocumentIR.model_validate_json(snapshot.content)
    assert len(ir.pages) == 27
    assert len(ir.blocks) == 27
    assert ir.preview_sha256 == digest
    assert ir.blocks[0].source_regions[0].bbox_pdf == (10, 752, 50, 772)
    assert "Synthetic page 27" in snapshot.text
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    print(
        f"27-page PDF + synthetic HTTP parse + published IR: {time.perf_counter() - started:.2f}s"
    )
