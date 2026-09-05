import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import Settings
from easylearn.inference.config import MinerUSettings
from easylearn.jobs.delivery import Outbox
from easylearn.main import create_app
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
