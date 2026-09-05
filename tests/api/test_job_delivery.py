import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from easylearn.errors import DomainError
from easylearn.jobs.delivery import Outbox
from easylearn.jobs.service import JobService


@pytest.mark.asyncio
async def test_outbox_survives_lost_ack_and_rejects_stale_dispatcher(database, accepted_document):
    first = Outbox(database, lease_duration=timedelta(seconds=1))
    competing = Outbox(database, lease_duration=timedelta(seconds=1))
    batches = await asyncio.gather(first.claim(), competing.claim())
    deliveries = [item for batch in batches for item in batch]
    assert len(deliveries) == 1
    original = deliveries[0]
    assert str(original.job_id) == accepted_document["job_id"]
    assert original.generation == 1
    assert await first.claim() == []
    # Broker accepted the event, but the dispatcher died before acknowledging it.
    await asyncio.sleep(1.1)
    (reclaimed,) = await competing.claim()
    assert reclaimed.event_id == original.event_id
    assert reclaimed.token != original.token
    with pytest.raises(DomainError, match="OUTBOX_LEASE_LOST"):
        await first.acknowledge(original)
    await competing.acknowledge(reclaimed)
    assert await first.claim() == []


@pytest.mark.asyncio
async def test_reconciler_recovers_acknowledged_but_lost_broker_message(
    database,
    accepted_document,
):
    outbox = Outbox(database)
    jobs = JobService(database)
    (original,) = await outbox.claim()
    await outbox.acknowledge(original)
    assert await outbox.claim() == []
    recovered = await asyncio.gather(
        *(jobs.reconcile(dispatch_grace=timedelta(0)) for _ in range(2))
    )
    assert sum(recovered) == 1
    assert await jobs.reconcile(dispatch_grace=timedelta(0)) == 0
    (replacement,) = await outbox.claim()
    assert replacement.job_id == UUID(accepted_document["job_id"])
    assert replacement.generation == 2
    assert await jobs.acquire(original.job_id, generation=1, owner=uuid4()) is None
    assert await jobs.acquire(replacement.job_id, generation=2, owner=uuid4()) is not None
