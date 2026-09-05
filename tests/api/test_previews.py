import asyncio
import hashlib
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from pypdf import PdfWriter

from easylearn.config import Settings
from easylearn.previews.schema import PreviewLimits
from easylearn.previews.service import PreviewService
from easylearn.storage import LocalStorage


@pytest.fixture
def pdf_bytes(request):
    original = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
    kind = getattr(request, "param", "original")
    if kind == "broken":
        return b"%PDF-1.7\nBroken document\n%%EOF"
    if kind == "original":
        return original
    writer = PdfWriter(BytesIO(original))
    if kind == "encrypted":
        writer.encrypt("test-password")
    elif kind == "too_many_pages":
        writer.add_page(writer.pages[0])
    elif kind == "oversized":
        writer.pages[0].mediabox.upper_right = (20000, 20000)
    else:
        raise ValueError(kind)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pdf_bytes", "code"),
    [
        ("broken", "PREVIEW_INVALID"),
        ("encrypted", "PREVIEW_PASSWORD_REQUIRED"),
        ("too_many_pages", "PREVIEW_PAGE_LIMIT"),
        ("oversized", "PREVIEW_PAGE_SIZE_LIMIT"),
    ],
    indirect=["pdf_bytes"],
)
async def test_invalid_pdf_reports_failure_without_publishing_assets(
    client, database, database_url, tmp_path, accepted_document, code
):
    settings = Settings(database_url=database_url, preview_limits=PreviewLimits(max_pages=1))
    previews = PreviewService(database, LocalStorage(tmp_path), settings)
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    status = (await client.get(accepted_document["status_url"])).json()
    assert status["status"] == "FAILED"
    assert status["failure"]["code"] == code
    assert status["failure"]["retryable"] is False
    document = (await client.get(f"/api/v1/documents/{accepted_document['document_id']}")).json()
    assert document["preview_runs"][0]["preview_asset_id"] is None
    assert document["preview_runs"][0]["pages"] == []
    retry = await client.post(
        accepted_document["status_url"] + "/retry", headers={"Idempotency-Key": "retry-invalid"}
    )
    assert retry.status_code == 409


@pytest.mark.asyncio
async def test_pdf_deadline_is_retryable_and_does_not_publish_a_partial_preview(
    client, database, database_url, tmp_path, accepted_document
):
    settings = Settings(database_url=database_url, preview_timeout_seconds=0.001)
    previews = PreviewService(database, LocalStorage(tmp_path), settings)
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    status = (await client.get(accepted_document["status_url"])).json()
    assert status["status"] == "FAILED"
    assert status["failure"]["code"] == "PREVIEW_TIMEOUT"
    assert status["failure"]["retryable"] is True


@pytest.mark.asyncio
async def test_only_published_assets_belonging_to_the_document_can_be_downloaded(
    client, database, database_url, tmp_path, accepted_document, uploaded_pdf
):
    path = f"/api/v1/documents/{accepted_document['document_id']}"
    asset_path = f"/assets/{uploaded_pdf['asset_id']}"
    assert (await client.get(path + asset_path)).status_code == 404
    previews = PreviewService(database, LocalStorage(tmp_path), Settings(database_url=database_url))
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    another = await client.post(
        "/api/v1/documents",
        json={"upload_id": uploaded_pdf["upload_id"]},
        headers={"Idempotency-Key": "another-document"},
    )
    assert another.status_code == 202
    assert (
        await client.get(f"/api/v1/documents/{another.json()['document_id']}" + asset_path)
    ).status_code == 404
    download = await client.get(path + asset_path)
    assert download.status_code == 200
    head = await client.head(path + asset_path)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == download.headers["content-length"]
    assert head.headers["etag"] == download.headers["etag"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("range_header", "status"), [("bytes=999999999-", 416), ("not-a-range", 400)]
)
async def test_invalid_asset_ranges_use_the_same_error_contract(
    client, database, database_url, tmp_path, accepted_document, range_header, status
):
    previews = PreviewService(database, LocalStorage(tmp_path), Settings(database_url=database_url))
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    path = f"/api/v1/documents/{accepted_document['document_id']}"
    asset_id = (await client.get(path)).json()["preview_runs"][0]["preview_asset_id"]
    response = await client.get(path + f"/assets/{asset_id}", headers={"Range": range_header})
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.json()["code"] == f"HTTP_{status}"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    if status == 416:
        assert response.headers["content-range"] == "bytes */125121"


@pytest.mark.asyncio
async def test_cancellation_stops_pdf_child_before_acknowledging_and_allows_retry(
    client, database, database_url, tmp_path, accepted_document, monkeypatch
):
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def slow_child(*args, **kwargs):
        child = await spawn(
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(30)",
            **kwargs,
        )
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_child)
    previews = PreviewService(database, LocalStorage(tmp_path), Settings(database_url=database_url))
    execution = asyncio.create_task(
        previews.execute(UUID(accepted_document["job_id"]), generation=1)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        cancelled = await client.post(accepted_document["status_url"] + "/cancel")
        assert cancelled.json()["status"] == "CANCEL_REQUESTED"
        await asyncio.wait_for(asyncio.shield(execution), timeout=5)
        assert children[0].returncode is not None
        status = (await client.get(accepted_document["status_url"])).json()
        assert status["status"] == "CANCELLED"
        document = (
            await client.get(f"/api/v1/documents/{accepted_document['document_id']}")
        ).json()
        assert document["preview_runs"][0]["preview_asset_id"] is None
        retry = await client.post(
            accepted_document["status_url"] + "/retry", headers={"Idempotency-Key": "retry"}
        )
        assert retry.json()["generation"] == 2
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_pdf_preview_is_validated_in_child_process_and_reuses_exact_original(
    client,
    database,
    database_url,
    tmp_path,
    accepted_document,
    uploaded_pdf,
):
    previews = PreviewService(database, LocalStorage(tmp_path), Settings(database_url=database_url))
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    path = f"/api/v1/documents/{accepted_document['document_id']}"
    document = (await client.get(path)).json()
    run = document["preview_runs"][0]
    assert run["status"] == "READY", (await client.get(accepted_document["status_url"])).json()
    assert run["preview_asset_id"] == uploaded_pdf["asset_id"]
    assert run["preview_sha256"] == uploaded_pdf["sha256"]
    assert run["pages"] == [
        {
            "page_index": 0,
            "media_box": [0.0, 0.0, 612.0, 792.0],
            "crop_box": [0.0, 0.0, 612.0, 792.0],
            "intrinsic_rotation": 0,
            "user_unit": 1.0,
            "page_label": "1",
        }
    ]
    download = await client.get(path + f"/assets/{run['preview_asset_id']}")
    assert download.status_code == 200
    assert hashlib.sha256(download.content).hexdigest() == uploaded_pdf["sha256"]
    partial = await client.get(
        path + f"/assets/{run['preview_asset_id']}", headers={"Range": "bytes=0-7"}
    )
    assert partial.status_code == 206
    assert partial.content == b"%PDF-1.5"
    assert partial.headers["ETag"] == download.headers["ETag"]
    await previews.execute(UUID(accepted_document["job_id"]), generation=1)
    assert (await client.get(path)).json() == document
    assert Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").stat().st_size == 125121
