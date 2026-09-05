import asyncio
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from easylearn.config import Settings
from easylearn.main import create_app


@pytest.mark.asyncio
async def test_another_application_instance_can_resume_and_complete_upload(
    database_url, tmp_path, pdf_bytes
):
    settings = Settings(database_url=database_url, storage_root=tmp_path)
    first_app = create_app(settings)
    content = pdf_bytes
    async with (
        first_app.router.lifespan_context(first_app),
        AsyncClient(transport=ASGITransport(app=first_app), base_url="http://first") as first,
    ):
        created = await first.post(
            "/api/v1/uploads",
            json={
                "filename": "restart.pdf",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        path = f"/api/v1/uploads/{created.json()['upload_id']}"
        await first.patch(path + "/content", headers={"Upload-Offset": "0"}, content=content[:9])

    second_app = create_app(settings)
    async with (
        second_app.router.lifespan_context(second_app),
        AsyncClient(transport=ASGITransport(app=second_app), base_url="http://second") as second,
    ):
        assert (await second.get(path)).json()["offset"] == 9
        await second.patch(path + "/content", headers={"Upload-Offset": "9"}, content=content[9:])
        completed = await second.post(path + "/complete")
        assert completed.status_code == 200
        assert completed.json()["status"] == "UPLOADED"
        repeated = await second.post(path + "/complete")
        assert repeated.json()["asset_id"] == completed.json()["asset_id"]


@pytest.mark.asyncio
async def test_expired_session_cannot_publish_even_when_all_bytes_arrived(database_url, tmp_path):
    app = create_app(
        Settings(database_url=database_url, storage_root=tmp_path, upload_session_ttl=1)
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://expiry") as client,
    ):
        created = await client.post(
            "/api/v1/uploads",
            json={
                "filename": "expired.pdf",
                "size": 3,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            },
        )
        path = f"/api/v1/uploads/{created.json()['upload_id']}"
        await client.patch(path + "/content", headers={"Upload-Offset": "0"}, content=b"abc")
        await asyncio.sleep(1.05)
        completed = await client.post(path + "/complete")
        assert completed.status_code == 410
        assert (await client.get(path)).json()["status"] == "EXPIRED"
        assert (await client.get(path)).json()["asset_id"] is None
