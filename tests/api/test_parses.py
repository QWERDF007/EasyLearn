import asyncio
import hashlib
import json
import os
import threading
from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, MockTransport, ReadError, Response
from PIL import Image

from easylearn.config import Settings
from easylearn.inference.config import MinerUSettings
from easylearn.jobs.delivery import Outbox
from easylearn.jobs.service import JobService
from easylearn.main import create_app
from easylearn.parses.worker import ParseWorker
from easylearn.previews.service import PreviewService
from easylearn.storage import LocalStorage


@pytest.fixture
def settings(database_url, tmp_path):
    return Settings(
        database_url=database_url,
        storage_root=tmp_path,
        mineru=MinerUSettings(
            base_url="http://mineru.test/", profile_revision="mineru-test-v1", api_key="private"
        ),
    )


@pytest_asyncio.fixture
async def client(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest_asyncio.fixture
async def ready_document(database, settings, accepted_document):
    previews = PreviewService(database, LocalStorage(settings.storage_root), settings)
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    return accepted_document


@pytest.mark.asyncio
async def test_parse_creation_freezes_ready_preview_and_enqueues_one_idempotent_job(
    client, ready_document, uploaded_pdf
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    body = {"preview_run_id": ready_document["preview_run_id"]}
    response = await client.post(path, json=body, headers={"Idempotency-Key": "parse"})
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert response.headers["location"] == accepted["status_url"]
    again = await client.post(path, json=body, headers={"Idempotency-Key": "parse"})
    assert again.json() == accepted
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "QUEUED"
    assert job["run_ref"] == {
        "kind": "PARSE",
        "document_id": ready_document["document_id"],
        "run_id": accepted["parse_run_id"],
    }
    runs = await client.get(path)
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    run = runs.json()[0]
    assert run["parse_run_id"] == accepted["parse_run_id"]
    assert run["preview_run_id"] == ready_document["preview_run_id"]
    assert run["preview_asset_id"] == uploaded_pdf["asset_id"]
    assert run["preview_sha256"] == uploaded_pdf["sha256"]
    assert run["configuration"]["profile_revision"] == "mineru-test-v1"
    assert run["configuration"]["options"]["page_count"] == 1
    assert run["configuration"]["options"]["backend"] == "vlm-engine"
    assert "private" not in runs.text
    assert "api_key" not in runs.text
    assert run["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_parse_has_a_fixed_preview_with_range_and_etag_before_inference(
    client, ready_document, uploaded_pdf, pdf_bytes
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    response = await client.post(
        path,
        json={"preview_run_id": ready_document["preview_run_id"]},
        headers={"Idempotency-Key": "fixed-preview"},
    )
    preview = path + f"/{response.json()['parse_run_id']}/preview"
    download = await client.get(preview)
    assert download.status_code == 200, download.text
    assert download.content == pdf_bytes
    assert download.headers["etag"] == f'"{uploaded_pdf["sha256"]}"'
    partial = await client.get(preview, headers={"Range": "bytes=0-7"})
    assert partial.status_code == 206
    assert partial.content == b"%PDF-1.5"
    head = await client.head(preview)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(pdf_bytes))
    other = f"/api/v1/documents/{uuid4()}/parse-runs/{response.json()['parse_run_id']}/preview"
    assert (await client.get(other)).status_code == 404
    assert (await client.get(path + f"/{uuid4()}/preview")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pending", "missing", "other_document", "no_service"])
async def test_rejected_parse_does_not_leave_a_run_or_outbox_event(
    client, database, accepted_document, settings, uploaded_pdf, case
):
    outbox = Outbox(database)
    for event in await outbox.claim():
        await outbox.acknowledge(event)
    document_id = accepted_document["document_id"]
    preview_id = accepted_document["preview_run_id"]
    expected = (409, "PREVIEW_NOT_READY")
    if case == "missing":
        preview_id = str(uuid4())
        expected = (404, "PREVIEW_NOT_FOUND")
    elif case == "other_document":
        another = await client.post(
            "/api/v1/documents",
            json={"upload_id": uploaded_pdf["upload_id"]},
            headers={"Idempotency-Key": "another"},
        )
        document_id = another.json()["document_id"]
        for event in await outbox.claim():
            await outbox.acknowledge(event)
        expected = (404, "PREVIEW_NOT_FOUND")
    elif case == "no_service":
        previews = PreviewService(database, LocalStorage(settings.storage_root), settings)
        await previews.execute(UUID(accepted_document["job_id"]), generation=1)
        settings.mineru = None
        expected = (503, "MINERU_NOT_CONFIGURED")
    path = f"/api/v1/documents/{document_id}/parse-runs"
    response = await client.post(
        path, json={"preview_run_id": preview_id}, headers={"Idempotency-Key": "rejected"}
    )
    assert (response.status_code, response.json()["code"]) == expected
    assert (await client.get(path)).json() == []
    assert await outbox.claim() == []


@pytest.mark.asyncio
async def test_concurrent_parse_replay_enqueues_once_and_conflicting_options_are_rejected(
    client, database, ready_document
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    body = {"preview_run_id": ready_document["preview_run_id"]}
    outbox = Outbox(database)
    for event in await outbox.claim():
        await outbox.acknowledge(event)
    responses = await asyncio.gather(
        *(client.post(path, json=body, headers={"Idempotency-Key": "concurrent"}) for _ in range(4))
    )
    assert all(response.status_code == 202 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    (event,) = await outbox.claim()
    assert str(event.job_id) == responses[0].json()["job_id"]
    assert event.generation == 1
    conflict = await client.post(
        path,
        json=dict(body, options={"language": "en"}),
        headers={"Idempotency-Key": "concurrent"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert len((await client.get(path)).json()) == 1


@pytest.mark.asyncio
async def test_file_configuration_changes_do_not_rewrite_an_accepted_parse(
    client, ready_document, settings
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    body = {"preview_run_id": ready_document["preview_run_id"]}
    first = await client.post(path, json=body, headers={"Idempotency-Key": "frozen"})
    before = (await client.get(path)).json()[0]
    settings.mineru = MinerUSettings(
        base_url="http://different.test/",
        profile_revision="mineru-test-v2",
        parse={"language": "en"},
    )
    replay = await client.post(path, json=body, headers={"Idempotency-Key": "frozen"})
    assert replay.json() == first.json()
    assert (await client.get(path)).json() == [before]
    second = await client.post(path, json=body, headers={"Idempotency-Key": "new-config"})
    assert second.status_code == 202
    runs = (await client.get(path)).json()
    assert len(runs) == 2
    assert runs[0] == before
    assert runs[1]["configuration"]["profile_revision"] == "mineru-test-v2"
    assert runs[1]["configuration"]["options"]["language"] == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"page_count": 2}, {"backend": "vlm-http-client"}])
async def test_invalid_options_cannot_override_verified_page_count(client, ready_document, options):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    response = await client.post(
        path,
        json={"preview_run_id": ready_document["preview_run_id"], "options": options},
        headers={"Idempotency-Key": "invalid-options"},
    )
    assert response.status_code == 422
    assert (await client.get(path)).json() == []


@pytest.mark.asyncio
async def test_runs_sharing_pdf_content_keep_independent_jobs_and_frozen_retry_configuration(
    client, ready_document, database, settings, uploaded_pdf
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    first = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "first"},
        )
    ).json()
    frozen = (await client.get(path)).json()[0]["configuration"]
    another = (
        await client.post(
            "/api/v1/documents",
            json={"upload_id": uploaded_pdf["upload_id"]},
            headers={"Idempotency-Key": "another"},
        )
    ).json()
    previews = PreviewService(database, LocalStorage(settings.storage_root), settings)
    await previews.execute(UUID(another["job_id"]), generation=1)
    other_path = f"/api/v1/documents/{another['document_id']}/parse-runs"
    second = (
        await client.post(
            other_path,
            json={"preview_run_id": another["preview_run_id"]},
            headers={"Idempotency-Key": "first"},
        )
    ).json()
    assert second["parse_run_id"] != first["parse_run_id"]
    assert second["job_id"] != first["job_id"]
    assert (await client.post(first["status_url"] + "/cancel")).json()["status"] == "CANCELLED"
    assert (await client.get(path)).json()[0]["status"] == "CANCELLED"
    assert (await client.get(other_path)).json()[0]["status"] == "QUEUED"
    assert (await client.get(other_path)).json()[0]["preview_asset_id"] == uploaded_pdf["asset_id"]
    retried = await client.post(
        first["status_url"] + "/retry", headers={"Idempotency-Key": "retry"}
    )
    assert retried.status_code == 202
    assert retried.json()["generation"] == 2
    assert (await client.get(path)).json()[0]["configuration"] == frozen
    assert (await client.get(other_path)).json()[0]["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_uncertain_submission_was_durable_before_http_and_cannot_be_blindly_retried(
    client, ready_document, database, settings, pdf_bytes
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "unknown"},
        )
    ).json()
    submissions = []

    async def upstream(request):
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        assert request.url.path == "/tasks"
        assert request.method == "POST"
        assert pdf_bytes in await request.aread()
        durable = (await client.get(accepted["status_url"])).json()
        assert durable["stage"] == "SUBMITTING"
        attempt = durable["checkpoint"]["submissions"][-1]
        assert attempt["state"] == "SUBMITTING"
        assert attempt["request_id"] == request.headers["X-Request-ID"]
        assert attempt["source_sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
        assert len(attempt["configuration_sha256"]) == 64
        submissions.append(attempt)
        return Response(504, content=b"gateway timeout after upstream accepted")

    storage = LocalStorage(settings.storage_root)
    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(JobService(database), storage, settings, http)
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        await worker.execute(UUID(accepted["job_id"]), generation=1)
    assert len(submissions) == 1
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "FAILED"
    assert job["stage"] == "SUBMIT_UNKNOWN"
    assert job["failure"]["code"] == "MINERU_SUBMIT_UNKNOWN"
    assert job["failure"]["retryable"] is False
    attempt = job["checkpoint"]["submissions"][-1]
    assert attempt["request_id"] == submissions[0]["request_id"]
    assert attempt["state"] == "SUBMIT_UNKNOWN"
    assert b"".join(storage.read(attempt["response"]["key"])) == (
        b"gateway timeout after upstream accepted"
    )
    assert (await client.get(path)).json()[0]["status"] == "SUBMIT_UNKNOWN"
    retry = await client.post(
        accepted["status_url"] + "/retry", headers={"Idempotency-Key": "retry"}
    )
    assert retry.status_code == 409


@pytest.fixture
def parse_archive(pdf_bytes, request):
    middle = {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [10, 20, 50, 40],
                        "lines": [
                            {
                                "bbox": [10, 20, 50, 40],
                                "spans": [{"type": "text", "content": "A synthetic paragraph."}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    images = {}
    if getattr(request, "param", "text") == "images":
        with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as picture:
            picture.save(buffer, format="PNG")
            png = buffer.getvalue()
        for name in ("figure1.png", "figure2.png"):
            images[f"input/vlm/images/{name}"] = png
            middle["pdf_info"][0]["para_blocks"].append(
                {
                    "type": "interline_equation",
                    "bbox": [10, 50, 50, 70],
                    "lines": [
                        {
                            "bbox": [10, 50, 50, 70],
                            "spans": [
                                {
                                    "type": "interline_equation",
                                    "content": "x^2",
                                    "image_path": name,
                                }
                            ],
                        }
                    ],
                }
            )
    files = {
        "input/vlm/input.md": b"A synthetic paragraph.",
        "input/vlm/input_middle.json": json.dumps(middle).encode(),
        "input/vlm/input_model.json": b"[]",
        "input/vlm/input_content_list.json": b"[]",
        "input/vlm/input_content_list_v2.json": b"[]",
        "input/vlm/input_origin.pdf": pdf_bytes,
        **images,
    }
    with BytesIO() as buffer:
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("parse_archive", ["text", "images"], indirect=True)
async def test_parse_worker_publishes_validated_ir_and_fixed_version_once(
    client, ready_document, database, settings, parse_archive, uploaded_pdf
):
    from easylearn.document_ir.schema import DocumentIR

    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "complete"},
        )
    ).json()
    ir_path = path + f"/{accepted['parse_run_id']}/document-ir"
    assert (await client.get(ir_path)).status_code == 409
    task_id = uuid4()
    posts = []

    def upstream(request):
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        if request.method == "POST":
            posts.append(request.headers["X-Request-ID"])
        elif request.url.path.endswith("/result"):
            return Response(200, content=parse_archive, headers={"content-type": "application/zip"})
        else:
            assert request.url.path == f"/tasks/{task_id}"
        return Response(
            202 if request.method == "POST" else 200,
            json={
                "task_id": str(task_id),
                "status": "pending" if request.method == "POST" else "completed",
                "backend": "vlm-engine",
                "file_names": ["input"],
                "created_at": "2026-09-05T00:00:00Z",
                "started_at": None,
                "completed_at": None,
                "error": None,
            },
        )

    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(
            JobService(database), LocalStorage(settings.storage_root), settings, http
        )
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        job = (await client.get(accepted["status_url"])).json()
        assert job["status"] == "SUCCEEDED", job
        document_ir = await client.get(ir_path)
        assert document_ir.status_code == 200
        ir = DocumentIR.model_validate_json(document_ir.content)
        assert str(ir.parse_run_id) == accepted["parse_run_id"]
        assert str(ir.preview_asset_id) == uploaded_pdf["asset_id"]
        assert ir.preview_sha256 == uploaded_pdf["sha256"]
        assert ir.blocks[0].source_regions[0].bbox_pdf == (10, 752, 50, 772)
        assert "A synthetic paragraph." in document_ir.text
        assert (await client.get(path)).json()[0]["status"] == "READY"
        document_path = f"/api/v1/documents/{ready_document['document_id']}"
        assert (await client.get(document_path)).json()["active_parse_run_id"] == accepted[
            "parse_run_id"
        ]
        if ir.assets:
            assert len(ir.assets) == 2
            assert ir.assets[0].asset_id != ir.assets[1].asset_id
            assert ir.assets[0].sha256 == ir.assets[1].sha256
        for image in ir.assets:
            response = await client.get(document_path + f"/assets/{image.asset_id}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert hashlib.sha256(response.content).hexdigest() == image.sha256
            other = await client.get(f"/api/v1/documents/{uuid4()}/assets/{image.asset_id}")
            assert other.status_code == 404
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        assert (await client.get(ir_path)).content == document_ir.content
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_interrupted_submit_is_recovered_as_unknown_without_reposting(
    client, ready_document, database, settings
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "interrupted"},
        )
    ).json()
    posts = []

    def upstream(request):
        if request.method == "POST":
            posts.append(request.headers["X-Request-ID"])
            raise asyncio.CancelledError
        return Response(
            200,
            json={
                "status": "healthy",
                "version": "3.4.5",
                "protocol_version": 2,
                "task_retention_seconds": 86400,
            },
        )

    jobs = JobService(database, lease_duration=timedelta(seconds=0.4))
    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(jobs, LocalStorage(settings.storage_root), settings, http)
        with pytest.raises(asyncio.CancelledError):
            await worker.execute(UUID(accepted["job_id"]), generation=1)
        job = (await client.get(accepted["status_url"])).json()
        assert job["stage"] == "SUBMITTING"
        await asyncio.sleep(0.45)
        assert await jobs.reconcile() == 1
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        await worker.execute(UUID(accepted["job_id"]), generation=2)
    assert len(posts) == 1
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "FAILED"
    assert job["failure"]["code"] == "MINERU_SUBMIT_UNKNOWN"
    assert job["checkpoint"]["submissions"][0]["request_id"] == posts[0]


@pytest.mark.asyncio
async def test_cancelling_an_external_submission_records_possible_upstream_work(
    client, ready_document, database, settings
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "cancel-upstream"},
        )
    ).json()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def upstream(request):
        if request.method == "POST":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return Response(
            200,
            json={
                "status": "healthy",
                "version": "3.4.5",
                "protocol_version": 2,
                "task_retention_seconds": 86400,
            },
        )

    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(
            JobService(database), LocalStorage(settings.storage_root), settings, http
        )
        execution = asyncio.create_task(worker.execute(UUID(accepted["job_id"]), generation=1))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert (await client.post(accepted["status_url"] + "/cancel")).json()[
                "status"
            ] == "CANCEL_REQUESTED"
            await asyncio.wait_for(asyncio.shield(execution), timeout=5)
            assert stopped.is_set()
            job = (await client.get(accepted["status_url"])).json()
            assert job["status"] == "CANCELLED"
            run = (await client.get(path)).json()[0]
            assert run["upstream_may_be_running"] is True
            assert (
                await client.get(path + f"/{accepted['parse_run_id']}/document-ir")
            ).status_code == 409
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["query", "download", "normalizing", "deadline"])
async def test_retry_resumes_existing_external_task_and_reuses_completed_download(
    client, ready_document, database, settings, parse_archive, interruption
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "resume"},
        )
    ).json()
    task_id = uuid4()
    first_attempt = True
    posts = []
    downloads = []

    def upstream(request):
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        if request.method == "POST":
            posts.append(request.headers["X-Request-ID"])
        elif request.url.path.endswith("/result"):
            downloads.append(request.url.path)
            if first_attempt and interruption == "download":
                raise ReadError("Download connection interrupted")
            return Response(200, content=parse_archive, headers={"content-type": "application/zip"})
        elif first_attempt and interruption == "query":
            raise ReadError("Polling connection interrupted")
        status = "pending" if request.method == "POST" else "completed"
        if first_attempt and interruption == "deadline":
            status = "processing"
        return Response(
            202 if request.method == "POST" else 200,
            json={
                "task_id": str(task_id),
                "status": status,
                "backend": "vlm-engine",
                "file_names": ["input"],
                "created_at": "2026-09-05T00:00:00Z",
                "started_at": None,
                "completed_at": None,
                "error": None,
            },
        )

    if interruption == "normalizing":
        settings.mineru = settings.mineru.model_copy(update={"validation_timeout_seconds": 0.001})
    elif interruption == "deadline":
        settings.mineru = settings.mineru.model_copy(
            update={"task_timeout_seconds": 0.1, "poll_interval_seconds": 0.01}
        )
    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(
            JobService(database), LocalStorage(settings.storage_root), settings, http
        )
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        failed = (await client.get(accepted["status_url"])).json()
        assert failed["status"] == "FAILED", failed
        assert failed["failure"]["retryable"] is True
        assert (
            failed["failure"]["code"]
            == {
                "query": "MINERU_UNAVAILABLE",
                "download": "MINERU_UNAVAILABLE",
                "normalizing": "MINERU_RESULT_TIMEOUT",
                "deadline": "MINERU_TASK_TIMEOUT",
            }[interruption]
        )
        assert failed["checkpoint"]["submissions"][0]["task"]["task_id"] == str(task_id)
        assert (
            await client.get(path + f"/{accepted['parse_run_id']}/document-ir")
        ).status_code == 409
        first_attempt = False
        settings.mineru = settings.mineru.model_copy(
            update={"validation_timeout_seconds": 300, "task_timeout_seconds": 3600}
        )
        retry = await client.post(
            accepted["status_url"] + "/retry", headers={"Idempotency-Key": "resume"}
        )
        assert retry.status_code == 202
        await worker.execute(UUID(accepted["job_id"]), generation=2)
    assert len(posts) == 1
    assert len(downloads) == (2 if interruption == "download" else 1)
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "SUCCEEDED", job
    assert (await client.get(path + f"/{accepted['parse_run_id']}/document-ir")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_archive", "late_cancel"])
@pytest.mark.parametrize("parse_archive", ["images"], indirect=True)
async def test_late_old_runs_and_failed_new_runs_do_not_replace_the_latest_ready_version(
    client, ready_document, database, settings, parse_archive, failure
):
    document_path = f"/api/v1/documents/{ready_document['document_id']}"
    path = document_path + "/parse-runs"
    runs = []
    for key in ("old", "new", "failed"):
        runs.append(
            (
                await client.post(
                    path,
                    json={"preview_run_id": ready_document["preview_run_id"]},
                    headers={"Idempotency-Key": key},
                )
            ).json()
        )
    requests = 0

    async def upstream(request):
        nonlocal requests
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        if request.method == "POST":
            requests += 1
            return Response(
                202,
                json={
                    "task_id": str(uuid4()),
                    "status": "completed",
                    "backend": "vlm-engine",
                    "file_names": ["input"],
                    "created_at": "2026-09-05T00:00:00Z",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
            )
        if requests == 3:
            if failure == "late_cancel":
                cancelled = await client.post(runs[2]["status_url"] + "/cancel")
                assert cancelled.json()["status"] == "CANCEL_REQUESTED"
            else:
                return Response(
                    200, content=b"not a ZIP", headers={"content-type": "application/zip"}
                )
        return Response(200, content=parse_archive, headers={"content-type": "application/zip"})

    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(
            JobService(database), LocalStorage(settings.storage_root), settings, http
        )
        for run in (runs[1], runs[0], runs[2]):
            await worker.execute(UUID(run["job_id"]), generation=1)
            assert (await client.get(document_path)).json()["active_parse_run_id"] == runs[1][
                "parse_run_id"
            ]
    snapshots = []
    for run in runs[:2]:
        response = await client.get(path + f"/{run['parse_run_id']}/document-ir")
        assert response.status_code == 200
        snapshots.append(response.json())
    first_image = snapshots[0]["assets"][0]
    second_image = snapshots[1]["assets"][0]
    assert first_image["asset_id"] != second_image["asset_id"]
    assert first_image["sha256"] == second_image["sha256"]
    assert (
        await client.get(document_path + f"/assets/{first_image['asset_id']}")
    ).status_code == 200
    assert (
        await client.get(document_path + f"/assets/{second_image['asset_id']}")
    ).status_code == 200
    failed = (await client.get(runs[2]["status_url"])).json()
    assert failed["status"] == ("FAILED" if failure == "invalid_archive" else "CANCELLED")
    if failure == "invalid_archive":
        assert failed["failure"]["code"] == "MINERU_RESULT_INVALID"
    assert (await client.get(path + f"/{runs[2]['parse_run_id']}/document-ir")).status_code == 409


@pytest.mark.asyncio
async def test_definite_rejection_can_retry_with_a_new_request_and_keeps_the_original_response(
    client, ready_document, database, settings, parse_archive
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "rejected"},
        )
    ).json()
    posts = []

    def upstream(request):
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        if request.method == "POST":
            posts.append(request.headers["X-Request-ID"])
            if len(posts) == 1:
                return Response(429, content=b"capacity busy")
            return Response(
                202,
                json={
                    "task_id": str(uuid4()),
                    "status": "completed",
                    "backend": "vlm-engine",
                    "file_names": ["input"],
                    "created_at": "2026-09-05T00:00:00Z",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
            )
        return Response(200, content=parse_archive, headers={"content-type": "application/zip"})

    storage = LocalStorage(settings.storage_root)
    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(JobService(database), storage, settings, http)
        await worker.execute(UUID(accepted["job_id"]), generation=1)
        job = (await client.get(accepted["status_url"])).json()
        assert job["status"] == "FAILED"
        assert job["failure"]["code"] == "MINERU_SUBMIT_REJECTED"
        assert job["failure"]["retryable"] is True
        attempt = job["checkpoint"]["submissions"][0]
        assert attempt["response"] is not None
        assert b"".join(storage.read(attempt["response"]["key"])) == b"capacity busy"
        retry = await client.post(
            accepted["status_url"] + "/retry", headers={"Idempotency-Key": "retry"}
        )
        assert retry.status_code == 202
        await worker.execute(UUID(accepted["job_id"]), generation=2)
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "SUCCEEDED"
    assert len(posts) == 2 and posts[0] != posts[1]
    assert job["checkpoint"]["submissions"][0] == attempt


@pytest.mark.asyncio
async def test_cancel_waits_for_inflight_file_io_before_acknowledging(
    client, ready_document, database, settings, monkeypatch
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "cancel-io"},
        )
    ).json()
    fsync = os.fsync
    started = asyncio.Event()
    released = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_fsync(descriptor):
        fsync(descriptor)
        loop.call_soon_threadsafe(started.set)
        assert released.wait(timeout=10)
        finished.set()

    def upstream(request):
        if request.url.path == "/health":
            return Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.4.5",
                    "protocol_version": 2,
                    "task_retention_seconds": 86400,
                },
            )
        assert request.method == "POST"
        return Response(
            202,
            json={
                "task_id": str(uuid4()),
                "status": "completed",
                "backend": "vlm-engine",
                "file_names": ["input"],
                "created_at": "2026-09-05T00:00:00Z",
                "started_at": None,
                "completed_at": None,
                "error": None,
            },
        )

    storage = LocalStorage(settings.storage_root)
    monkeypatch.setattr(os, "fsync", slow_fsync)
    async with AsyncClient(
        base_url="http://mineru.test/", transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(JobService(database), storage, settings, http)
        execution = asyncio.create_task(worker.execute(UUID(accepted["job_id"]), generation=1))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await client.post(accepted["status_url"] + "/cancel")
            await asyncio.sleep(1.1)
            assert not execution.done()
            assert (await client.get(accepted["status_url"])).json()["status"] == "CANCEL_REQUESTED"
            released.set()
            await asyncio.wait_for(asyncio.shield(execution), timeout=5)
            assert finished.is_set()
            assert (await client.get(accepted["status_url"])).json()["status"] == "CANCELLED"
            assert list((storage.root / "staging").iterdir()) == []
        finally:
            released.set()
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["service", "revision"])
async def test_changed_profile_cannot_redirect_a_frozen_run_to_another_deployment(
    client, ready_document, database, settings, change
):
    path = f"/api/v1/documents/{ready_document['document_id']}/parse-runs"
    accepted = (
        await client.post(
            path,
            json={"preview_run_id": ready_document["preview_run_id"]},
            headers={"Idempotency-Key": "fixed-profile"},
        )
    ).json()
    original = (await client.get(path)).json()[0]["configuration"]
    settings.mineru = MinerUSettings(
        base_url="http://different.test/" if change == "service" else "http://mineru.test/",
        profile_revision="other-revision" if change == "revision" else "mineru-test-v1",
    )

    def upstream(request):
        pytest.fail("A changed deployment must not receive any request")

    async with AsyncClient(
        base_url=str(settings.mineru.base_url), transport=MockTransport(upstream)
    ) as http:
        worker = ParseWorker(
            JobService(database), LocalStorage(settings.storage_root), settings, http
        )
        await worker.execute(UUID(accepted["job_id"]), generation=1)
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "FAILED"
    assert job["failure"]["code"] == "MINERU_PROFILE_CHANGED"
    assert job["checkpoint"] == {}
    assert (await client.get(path)).json()[0]["configuration"] == original
