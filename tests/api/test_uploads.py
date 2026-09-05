import asyncio
import hashlib

import pytest


@pytest.mark.asyncio
async def test_user_can_resume_upload_and_complete_it_without_losing_bytes(client, pdf_bytes):
    content = pdf_bytes
    created = await client.post(
        "/api/v1/uploads",
        json={
            "filename": "paper.pdf",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert created.status_code == 201
    upload_id = created.json()["upload_id"]
    first = await client.patch(
        f"/api/v1/uploads/{upload_id}/content", headers={"Upload-Offset": "0"}, content=content[:10]
    )
    assert first.status_code == 204
    snapshot = await client.get(f"/api/v1/uploads/{upload_id}")
    assert snapshot.json()["offset"] == 10
    second = await client.patch(
        f"/api/v1/uploads/{upload_id}/content",
        headers={"Upload-Offset": "10"},
        content=content[10:],
    )
    assert second.status_code == 204
    completed = await client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert completed.status_code == 200
    assert completed.json()["status"] == "UPLOADED"
    assert (
        completed.json()["sha256"]
        == "ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b"
    )


@pytest.mark.asyncio
async def test_retried_chunk_is_idempotent_but_different_bytes_at_same_offset_conflict(client):
    created = await client.post(
        "/api/v1/uploads",
        json={
            "filename": "paper.pdf",
            "size": 6,
            "sha256": hashlib.sha256(b"abcdef").hexdigest(),
        },
    )
    path = f"/api/v1/uploads/{created.json()['upload_id']}"
    results = await asyncio.gather(
        *(
            client.patch(path + "/content", headers={"Upload-Offset": "0"}, content=b"abc")
            for _ in range(2)
        )
    )
    assert [response.status_code for response in results] == [204, 204]
    conflict = await client.patch(path + "/content", headers={"Upload-Offset": "0"}, content=b"xyz")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "UPLOAD_OFFSET_MISMATCH"
    assert (await client.get(path)).json()["offset"] == 3


@pytest.mark.asyncio
async def test_checksum_failure_is_persisted_and_cannot_publish_an_asset(client):
    created = await client.post(
        "/api/v1/uploads",
        json={
            "filename": "paper.pdf",
            "size": 3,
            "sha256": "0" * 64,
        },
    )
    path = f"/api/v1/uploads/{created.json()['upload_id']}"
    await client.patch(path + "/content", headers={"Upload-Offset": "0"}, content=b"bad")
    failed = await client.post(path + "/complete")
    assert failed.json()["code"] == "UPLOAD_HASH_MISMATCH"
    snapshot = (await client.get(path)).json()
    assert snapshot["status"] == "INVALID"
    assert snapshot["asset_id"] is None
    assert (await client.post(path + "/complete")).status_code == 409


@pytest.mark.asyncio
async def test_filename_cannot_disguise_unrelated_bytes_as_a_supported_document(client):
    content = b"this is not a PDF"
    created = await client.post(
        "/api/v1/uploads",
        json={
            "filename": "paper.pdf",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    path = f"/api/v1/uploads/{created.json()['upload_id']}"
    await client.patch(path + "/content", headers={"Upload-Offset": "0"}, content=content)
    result = await client.post(path + "/complete")
    assert result.status_code == 422
    assert result.json()["code"] == "UPLOAD_FORMAT_MISMATCH"
    assert (await client.get(path)).json()["status"] == "INVALID"
