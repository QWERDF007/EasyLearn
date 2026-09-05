import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from easylearn.errors import DomainError
from easylearn.jobs.delivery import Outbox
from easylearn.jobs.schema import JobFailure
from easylearn.jobs.service import JobService


@pytest.mark.asyncio
async def test_only_current_lease_can_checkpoint_and_recovery_fences_old_worker(
    database,
    accepted_document,
):
    jobs = JobService(database, lease_duration=timedelta(seconds=1))
    job_id = UUID(accepted_document["job_id"])
    leases = await asyncio.gather(
        *(jobs.acquire(job_id, generation=1, owner=uuid4()) for _ in range(2))
    )
    owned = [lease for lease in leases if lease is not None]
    assert len(owned) == 1
    original = owned[0]
    await jobs.save_checkpoint(original, stage="VALIDATING", data={"validated_pages": 2})
    await jobs.heartbeat(original)
    assert (await jobs.get(job_id)).checkpoint == {"validated_pages": 2}
    await asyncio.sleep(1.1)
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        await jobs.heartbeat(original)
    assert await jobs.reconcile() == 1
    snapshot = await jobs.get(job_id)
    assert snapshot.status == "QUEUED"
    assert snapshot.generation == 2
    assert snapshot.checkpoint == {"validated_pages": 2}
    assert await jobs.acquire(job_id, generation=1, owner=uuid4()) is None
    current = await jobs.acquire(job_id, generation=2, owner=uuid4())
    assert current is not None
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        await jobs.save_checkpoint(original, stage="VALIDATING", data={"validated_pages": 99})
    await jobs.save_checkpoint(current, stage="VALIDATING", data={"validated_pages": 3})
    assert (await jobs.get(job_id)).checkpoint == {"validated_pages": 3}
    events = await Outbox(database).claim()
    assert any(event.job_id == job_id and event.generation == 2 for event in events)


@pytest.mark.asyncio
async def test_failure_records_retryability_and_rejects_late_completion(
    database, accepted_document
):
    jobs = JobService(database)
    job_id = UUID(accepted_document["job_id"])
    lease = await jobs.acquire(job_id, generation=1, owner=uuid4())
    await jobs.fail(
        lease, JobFailure(code="PREVIEW_INVALID", message="Invalid PDF", retryable=False)
    )
    failed = await jobs.get(job_id)
    assert failed.status == "FAILED"
    assert failed.failure.code == "PREVIEW_INVALID"
    with pytest.raises(DomainError, match="JOB_NOT_RETRYABLE"):
        await jobs.retry(job_id, "retry-invalid")
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        async with jobs.publication(lease):
            pytest.fail("A failed task must not enter publication")


@pytest.mark.asyncio
async def test_cancel_fences_writes_and_requires_worker_exit_before_retry(
    client,
    database,
    accepted_document,
):
    jobs = JobService(database)
    job_id = UUID(accepted_document["job_id"])
    lease = await jobs.acquire(job_id, generation=1, owner=uuid4())
    await jobs.save_checkpoint(lease, stage="VALIDATING", data={"validated_pages": 2})
    cancelled = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCEL_REQUESTED"
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        await jobs.save_checkpoint(lease, stage="VALIDATING", data={"validated_pages": 3})
    headers = {"Idempotency-Key": "retry-preview"}
    assert (await client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers)).status_code == 409
    await jobs.acknowledge_cancel(lease)
    assert (await jobs.get(job_id)).status == "CANCELLED"
    retried = await client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers)
    assert retried.status_code == 202
    assert retried.json()["generation"] == 2
    assert (
        await client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers)
    ).json() == retried.json()
    assert (await jobs.get(job_id)).checkpoint == {"validated_pages": 2}
    assert await jobs.acquire(job_id, generation=1, owner=uuid4()) is None


@pytest.mark.asyncio
async def test_publication_is_atomic_and_terminal_under_duplicate_delivery(
    database, accepted_document
):
    jobs = JobService(database)
    job_id = UUID(accepted_document["job_id"])
    lease = await jobs.acquire(job_id, generation=1, owner=uuid4())
    with pytest.raises(ValueError, match="publication aborted"):
        async with jobs.publication(lease):
            raise ValueError("publication aborted")
    assert (await jobs.get(job_id)).status == "RUNNING"
    async with jobs.publication(lease):
        pass
    assert (await jobs.get(job_id)).status == "SUCCEEDED"
    assert await jobs.acquire(job_id, generation=1, owner=uuid4()) is None
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        async with jobs.publication(lease):
            pytest.fail("Terminal task must not publish twice")


@pytest.mark.asyncio
async def test_cancelled_worker_that_dies_is_reconciled_without_requeue(
    database, accepted_document
):
    jobs = JobService(database, lease_duration=timedelta(seconds=1))
    job_id = UUID(accepted_document["job_id"])
    lease = await jobs.acquire(job_id, generation=1, owner=uuid4())
    await jobs.cancel(job_id)
    await asyncio.sleep(1.1)
    assert await jobs.reconcile() == 1
    assert (await jobs.get(job_id)).status == "CANCELLED"
    with pytest.raises(DomainError, match="JOB_LEASE_LOST"):
        async with jobs.publication(lease):
            pytest.fail("Cancelled task must not publish")
    assert await jobs.acquire(job_id, generation=1, owner=uuid4()) is None
