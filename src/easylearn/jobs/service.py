import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.idempotency import IdempotencyRecord
from easylearn.jobs.models import JobRun, OutboxEvent
from easylearn.jobs.schema import JobFailure, JobStatus, JobView, RunRef


@dataclass(frozen=True)
class JobLease:
    job_id: UUID
    generation: int
    owner: UUID

    def guard(self) -> ColumnElement[bool]:
        return (
            (JobRun.id == self.job_id)
            & (JobRun.generation == self.generation)
            & (JobRun.lease_owner == self.owner)
            & (JobRun.status == JobStatus.RUNNING)
            & (JobRun.lease_expires_at > func.clock_timestamp())
        )


class JobService:
    def __init__(
        self, database: Database, *, lease_duration: timedelta = timedelta(seconds=60)
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Job lease must be positive")
        self.database = database
        self.lease_duration = lease_duration

    @staticmethod
    async def enqueue(session: AsyncSession, run_ref: RunRef) -> JobRun:
        job = JobRun(id=uuid4(), **run_ref.model_dump(), generation=1)
        session.add(job)
        await session.flush()
        session.add(OutboxEvent(job_id=job.id, generation=job.generation))
        return job

    async def get(self, job_id: UUID) -> JobView:
        async with self.database.sessions() as session:
            job = await session.get(JobRun, job_id)
            if job is None:
                raise DomainError("JOB_NOT_FOUND", "Job not found", status=404)
            return JobView.model_validate(job)

    async def acquire(self, job_id: UUID, *, generation: int, owner: UUID) -> JobLease | None:
        async with self.database.sessions.begin() as session:
            acquired = await session.scalar(
                update(JobRun)
                .where(
                    JobRun.id == job_id,
                    JobRun.status == JobStatus.QUEUED,
                    JobRun.generation == generation,
                )
                .values(
                    status=JobStatus.RUNNING,
                    lease_owner=owner,
                    lease_expires_at=func.clock_timestamp() + self.lease_duration,
                    heartbeat_at=func.clock_timestamp(),
                )
                .returning(JobRun.id)
            )
            return JobLease(job_id, generation, owner) if acquired is not None else None

    async def heartbeat(self, lease: JobLease) -> None:
        async with self.database.sessions.begin() as session:
            renewed = await session.scalar(
                update(JobRun)
                .where(lease.guard())
                .values(
                    lease_expires_at=func.clock_timestamp() + self.lease_duration,
                    heartbeat_at=func.clock_timestamp(),
                )
                .returning(JobRun.id)
            )
            if renewed is None:
                raise DomainError("JOB_LEASE_LOST", "Job lease is no longer owned", status=409)

    async def supervise[T](self, lease: JobLease, operation: Awaitable[T]) -> T:
        """Renew ownership while awaiting work; join its cleanup before returning."""
        task = asyncio.ensure_future(operation)
        interval = min(1.0, self.lease_duration.total_seconds() / 3)
        try:
            while True:
                done, _ = await asyncio.wait((task,), timeout=interval)
                if done:
                    return task.result()
                await self.heartbeat(lease)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def save_checkpoint(
        self, lease: JobLease, *, stage: str, data: dict[str, JsonValue]
    ) -> None:
        if not 1 <= len(stage) <= 40:
            raise ValueError("Stage must contain 1 to 40 characters")
        async with self.database.sessions.begin() as session:
            saved = await session.scalar(
                update(JobRun)
                .where(lease.guard())
                .values(stage=stage, checkpoint=data)
                .returning(JobRun.id)
            )
            if saved is None:
                raise DomainError("JOB_LEASE_LOST", "Job lease is no longer owned", status=409)

    async def reconcile(
        self, limit: int = 100, *, dispatch_grace: timedelta = timedelta(seconds=60)
    ) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("Reconciliation limit must be in [1, 1000]")
        if dispatch_grace < timedelta(0):
            raise ValueError("Dispatch grace cannot be negative")
        async with self.database.sessions.begin() as session:
            pending = (
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.job_id == JobRun.id,
                    OutboxEvent.generation == JobRun.generation,
                    OutboxEvent.delivered_at.is_(None),
                )
                .exists()
            )
            overdue_delivery = (
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.job_id == JobRun.id,
                    OutboxEvent.generation == JobRun.generation,
                    OutboxEvent.delivered_at <= func.clock_timestamp() - dispatch_grace,
                )
                .exists()
            )
            jobs = (
                await session.scalars(
                    select(JobRun)
                    .where(
                        or_(
                            and_(
                                JobRun.status.in_([JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED]),
                                JobRun.lease_expires_at <= func.clock_timestamp(),
                            ),
                            and_(JobRun.status == JobStatus.QUEUED, ~pending, overdue_delivery),
                        )
                    )
                    .order_by(JobRun.created_at, JobRun.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job in jobs:
                if job.status == JobStatus.CANCEL_REQUESTED:
                    job.status = JobStatus.CANCELLED
                else:
                    job.generation += 1
                    job.status = JobStatus.QUEUED
                    session.add(OutboxEvent(job_id=job.id, generation=job.generation))
                job.lease_owner = None
                job.lease_expires_at = None
            return len(jobs)

    async def cancel(self, job_id: UUID) -> JobView:
        async with self.database.sessions.begin() as session:
            job = await session.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())
            if job is None:
                raise DomainError("JOB_NOT_FOUND", "Job not found", status=404)
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
            elif job.status == JobStatus.RUNNING:
                job.status = JobStatus.CANCEL_REQUESTED
            return JobView.model_validate(job)

    async def acknowledge_cancel(self, lease: JobLease) -> None:
        async with self.database.sessions.begin() as session:
            stopped = await session.scalar(
                update(JobRun)
                .where(
                    JobRun.id == lease.job_id,
                    JobRun.generation == lease.generation,
                    JobRun.lease_owner == lease.owner,
                    JobRun.status == JobStatus.CANCEL_REQUESTED,
                    JobRun.lease_expires_at > func.clock_timestamp(),
                )
                .values(status=JobStatus.CANCELLED, lease_owner=None, lease_expires_at=None)
                .returning(JobRun.id)
            )
            if stopped is None:
                raise DomainError("JOB_LEASE_LOST", "Job lease is no longer owned", status=409)

    async def retry(self, job_id: UUID, key: str) -> JobView:
        async with self.database.sessions.begin() as session:
            record = await IdempotencyRecord.acquire(session, f"retry-job:{job_id}", key, {})
            if record.response is not None:
                return JobView.model_validate(record.response)
            job = await session.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())
            if job is None:
                raise DomainError("JOB_NOT_FOUND", "Job not found", status=404)
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED) or (
                job.status == JobStatus.FAILED
                and (job.failure is None or not JobFailure.model_validate(job.failure).retryable)
            ):
                raise DomainError("JOB_NOT_RETRYABLE", "Job has not stopped or failed", status=409)
            job.generation += 1
            job.status = JobStatus.QUEUED
            job.lease_owner = None
            job.lease_expires_at = None
            job.failure = None
            session.add(OutboxEvent(job_id=job.id, generation=job.generation))
            view = JobView.model_validate(job)
            record.response = view.model_dump(mode="json")
            return view

    async def fail(self, lease: JobLease, failure: JobFailure) -> None:
        async with self.database.sessions.begin() as session:
            failed = await session.scalar(
                update(JobRun)
                .where(lease.guard())
                .values(
                    status=JobStatus.FAILED,
                    failure=failure.model_dump(mode="json"),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(JobRun.id)
            )
            if failed is None:
                raise DomainError("JOB_LEASE_LOST", "Job lease is no longer owned", status=409)

    @asynccontextmanager
    async def publication(self, lease: JobLease) -> AsyncIterator[AsyncSession]:
        """Commit result references and the terminal state in one fenced transaction.

        Prepare assets outside this scope; only database writes belong inside it.
        """
        async with self.database.sessions.begin() as session:
            owned = await session.scalar(select(JobRun.id).where(lease.guard()).with_for_update())
            if owned is None:
                raise DomainError("JOB_LEASE_LOST", "Job lease is no longer owned", status=409)
            yield session
            await session.flush()
            completed = await session.scalar(
                update(JobRun)
                .where(lease.guard())
                .values(
                    status=JobStatus.SUCCEEDED,
                    stage="READY",
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(JobRun.id)
            )
            if completed is None:
                raise DomainError(
                    "JOB_LEASE_LOST", "Job lease expired during publication", status=409
                )
