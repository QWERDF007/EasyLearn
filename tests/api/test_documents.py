import asyncio
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_document_creation_is_durable_and_idempotent(client, uploaded_pdf):
    body = {"upload_id": uploaded_pdf["upload_id"], "client_id": str(uuid4())}
    headers = {"Idempotency-Key": "create-paper"}
    responses = await asyncio.gather(
        *(client.post("/api/v1/documents", json=body, headers=headers) for _ in range(2))
    )
    assert [response.status_code for response in responses] == [202, 202]
    accepted = responses[0].json()
    assert responses[1].json() == accepted
    assert responses[0].headers["Location"] == accepted["status_url"]
    job = (await client.get(accepted["status_url"])).json()
    assert job["status"] == "QUEUED"
    assert job["generation"] == 1
    assert job["run_ref"] == {
        "document_id": accepted["document_id"],
        "run_id": accepted["preview_run_id"],
        "kind": "PREVIEW",
    }
    document = (await client.get(f"/api/v1/documents/{accepted['document_id']}")).json()
    assert document["original_asset_id"] == uploaded_pdf["asset_id"]
    assert document["filename"] == "paper.pdf"
    assert document["preview_runs"][0]["preview_run_id"] == accepted["preview_run_id"]
    assert document["preview_runs"][0]["status"] == "QUEUED"
    conflict = await client.post(
        "/api/v1/documents", json={**body, "client_id": str(uuid4())}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_rejected_document_leaves_no_delivery_or_poisoned_idempotency_key(
    client,
    database,
    uploaded_pdf,
):
    from easylearn.jobs.delivery import Outbox

    missing = await client.post(
        "/api/v1/documents",
        json={"upload_id": str(uuid4())},
        headers={"Idempotency-Key": "after-invalid-upload"},
    )
    assert missing.status_code == 404
    assert await Outbox(database).claim() == []
    accepted = await client.post(
        "/api/v1/documents",
        json={"upload_id": uploaded_pdf["upload_id"]},
        headers={"Idempotency-Key": "after-invalid-upload"},
    )
    assert accepted.status_code == 202
    events = await Outbox(database).claim()
    assert len(events) == 1
    assert str(events[0].job_id) == accepted.json()["job_id"]
