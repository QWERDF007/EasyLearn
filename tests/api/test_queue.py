import asyncio
from uuid import uuid4

import pytest
from dramatiq.brokers.stub import StubBroker

from easylearn.config import Settings
from easylearn.jobs.delivery import Outbox
from easylearn.jobs.queue import JobQueue
from easylearn.jobs.schema import JobKind


@pytest.mark.asyncio
async def test_outbox_delivers_preview_to_async_actor_with_its_own_connection_lifecycle(
    client, database, database_url, tmp_path, accepted_document, pdf_bytes
):
    settings = Settings(
        database_url=database_url,
        storage_root=tmp_path,
        queue={"redis_url": "redis://127.0.0.1:6379/0", "namespace": "test_" + uuid4().hex},
    )
    broker = StubBroker(middleware=[])
    queue = JobQueue(settings, broker=broker)
    with queue.worker({JobKind.PREVIEW}):
        assert await queue.dispatch_once(database) == 1
        async with asyncio.timeout(10):
            while True:
                job = (await client.get(accepted_document["status_url"])).json()
                if job["status"] in ("SUCCEEDED", "FAILED"):
                    break
                await asyncio.sleep(0.02)
        assert job["status"] == "SUCCEEDED", job
        assert await queue.dispatch_once(database) == 0
        path = f"/api/v1/documents/{accepted_document['document_id']}"
        run = (await client.get(path)).json()["preview_runs"][0]
        assert run["status"] == "READY"
        assert (await client.get(path + f"/assets/{run['preview_asset_id']}")).content == pdf_bytes
    assert broker.dead_letters == []
    assert await Outbox(database).claim() == []
