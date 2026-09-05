import pytest


@pytest.mark.asyncio
async def test_native_web_process_reports_database_and_storage_readiness(client):
    assert (await client.get("/health/live")).json() == {"status": "alive"}
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": True, "storage": True}
