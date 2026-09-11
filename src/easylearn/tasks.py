"""In-process bounded task execution for the v3 application."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobFailure, JobKind, JobStatus, TaskView
from easylearn.persistence.tasks import TaskStore

logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    """Raised inside an executor when the user has requested cancellation."""


@dataclass
class TaskRecord:
    task_id: UUID
    server_boot_id: UUID
    document_id: UUID
    kind: JobKind
    scope: dict[str, JsonValue]
    created_at: datetime
    status: JobStatus = JobStatus.QUEUED
    progress: float | None = None
    message: str = "Queued"
    cancel_requested: bool = False
    finished_at: datetime | None = None
    result_ref: dict[str, JsonValue] | None = None
    failure: JobFailure | None = None
    answer: str | None = None
    publication_committed: bool = field(default=False, repr=False)
    execution: asyncio.Task[None] | None = field(default=None, repr=False)

    def view(self) -> TaskView:
        return TaskView(
            task_id=self.task_id,
            server_boot_id=self.server_boot_id,
            document_id=self.document_id,
            kind=self.kind,
            scope=dict(self.scope),
            status=self.status,
            progress=self.progress,
            message=self.message,
            cancel_requested=self.cancel_requested,
            created_at=self.created_at,
            finished_at=self.finished_at,
            result_ref=self.result_ref,
            failure=self.failure,
            answer=self.answer,
        )


type TaskResult = dict[str, JsonValue] | None
type TaskExecutor = Callable[[TaskRecord, "TaskContext"], Awaitable[TaskResult]]
type TaskAdmission = Callable[[], Awaitable[None]]


class TaskContext:
    def __init__(self, manager: TaskManager, record: TaskRecord) -> None:
        self.manager = manager
        self.record = record
        self._follow_ups: list[tuple[JobKind, dict[str, JsonValue]]] = []

    async def check(self) -> None:
        if self.record.cancel_requested or self.manager.stopping:
            raise TaskCancelled

    async def progress(self, value: float | None, message: str) -> None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("Task progress must be between 0 and 1")
        self.record.progress = value
        self.record.message = message
        await self.check()

    async def append_answer(self, text: str) -> None:
        self.record.answer = (self.record.answer or "") + text
        await self.check()

    async def publish(
        self, result: TaskResult, operation: Callable[[], Awaitable[object]]
    ) -> None:
        """Complete an irreversible publication before honoring cancellation."""

        await self.check()
        publication = asyncio.ensure_future(operation())
        while True:
            try:
                await asyncio.shield(publication)
                break
            except asyncio.CancelledError:
                if publication.cancelled():
                    raise
        self.record.result_ref = dict(result) if result is not None else None
        self.record.publication_committed = True

    async def enqueue_after_success(
        self, kind: JobKind, scope: dict[str, JsonValue]
    ) -> None:
        """Queue a dependent operation only after this task has been published."""
        self._follow_ups.append((kind, dict(scope)))


class TaskManager:
    """Owns in-process task execution with durable lifecycle records and restart recovery."""

    _CONFLICTING = frozenset({JobKind.PARSE, JobKind.TRANSLATE})

    def __init__(
        self,
        *,
        server_boot_id: UUID | None = None,
        queue_limit: int = 8,
        concurrency: dict[JobKind, int] | None = None,
        retention_seconds: int = 1800,
        database: Database | None = None,
    ) -> None:
        if queue_limit < 1 or retention_seconds < 1:
            raise ValueError("Task limits must be positive")
        defaults = {
            JobKind.PARSE: 1,
            JobKind.TRANSLATE: 1,
            JobKind.EXPORT: 1,
            JobKind.QA: 1,
        }
        self.server_boot_id = server_boot_id or uuid4()
        self.queue: asyncio.Queue[TaskRecord] = asyncio.Queue(maxsize=queue_limit)
        self.concurrency = {**defaults, **(concurrency or {})}
        self.retention_seconds = retention_seconds
        self.database = database
        self.store = TaskStore(database) if database is not None else None
        self._records: dict[UUID, TaskRecord] = {}
        self._executors: dict[JobKind, TaskExecutor] = {}
        self._semaphores = {
            kind: asyncio.Semaphore(limit) for kind, limit in self.concurrency.items()
        }
        self._dispatcher: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()
        self._state_lock = asyncio.Lock()
        self._deleting_documents: set[UUID] = set()
        self.stopping = False

    def register(self, kind: JobKind, executor: TaskExecutor) -> None:
        if self._dispatcher is not None:
            raise RuntimeError("Task executors must be registered before start")
        self._executors[kind] = executor

    async def start(self) -> None:
        if self._dispatcher is not None:
            return
        missing = set(JobKind) - set(self._executors)
        if missing:
            raise RuntimeError(f"No executor registered for: {sorted(missing)}")
        if self.store is not None:
            await self._reconcile_restart()
        self.stopping = False
        self._dispatcher = asyncio.create_task(self._dispatch(), name="easylearn-task-dispatcher")

    async def _reconcile_restart(self) -> None:
        if self.store is None:
            return
        now_iso = datetime.now(UTC).isoformat()
        interrupted_failure = json.dumps(
            {"code": "TASK_INTERRUPTED", "message": "Task interrupted by server restart", "retryable": True},
            ensure_ascii=False,
        )
        await self.store.reconcile_orphans(interrupted_failure, now_iso)

    async def _persist_record(self, record: TaskRecord) -> None:
        if self.store is None:
            return
        await self.store.upsert(record)

    async def _load_from_db(self, task_id: UUID) -> TaskRecord | None:
        if self.store is None:
            return None
        return await self.store.load(task_id)

    async def close(self) -> None:
        self.stopping = True
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        async with self._state_lock:
            for record in self._records.values():
                if record.status == JobStatus.QUEUED:
                    record.cancel_requested = True
                    record.status = JobStatus.CANCELLED
                    record.message = "Cancelled during shutdown"
                    record.finished_at = datetime.now(UTC)
                elif record.status == JobStatus.RUNNING:
                    record.cancel_requested = True
                    record.message = "Cancellation requested during shutdown"
            active = tuple(self._active)
            to_persist = list(self._records.values())
        for record in to_persist:
            await self._persist_record(record)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.difference_update(active)
        if self._dispatcher is dispatcher:
            self._dispatcher = None

    async def submit(
        self,
        document_id: UUID,
        kind: JobKind,
        scope: dict[str, JsonValue],
        *,
        admission: TaskAdmission | None = None,
    ) -> TaskView:
        async with self._state_lock:
            if admission is not None:
                await admission()
            view = self._submit_locked(document_id, kind, scope)
            record = self._records[view.task_id]
        await self._persist_record(record)
        return view

    async def retry(self, task_id: UUID) -> TaskView:
        view = await self.get(task_id)
        if not view.status.terminal:
            raise DomainError("TASK_BUSY", "Cannot retry an active task", status=409)
        if view.failure is None or not view.failure.retryable:
            raise DomainError("TASK_NOT_RETRYABLE", "Task is not retryable", status=409)
        return await self.submit(
            document_id=view.document_id,
            kind=view.kind,
            scope=dict(view.scope),
        )

    async def run_exclusive[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run a short maintenance operation beside task admission atomically."""

        async with self._state_lock:
            return await operation()

    def _submit_locked(
        self,
        document_id: UUID,
        kind: JobKind,
        scope: dict[str, JsonValue],
        *,
        ignore_task_id: UUID | None = None,
    ) -> TaskView:
        self._purge_locked()
        if self.stopping or self._dispatcher is None:
            raise DomainError("TASKS_UNAVAILABLE", "Task manager is stopping", status=503)
        if document_id in self._deleting_documents:
            raise DomainError("DOCUMENT_BUSY", "Document deletion is in progress", status=409)
        if kind in self._CONFLICTING and any(
            record.task_id != ignore_task_id
            and record.document_id == document_id
            and record.kind in self._CONFLICTING
            and record.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            for record in self._records.values()
        ):
            raise DomainError(
                "DOCUMENT_BUSY",
                "Another parse or translation is running for this document",
                status=409,
            )
        active_count = sum(
            record.task_id != ignore_task_id
            and record.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            for record in self._records.values()
        )
        if active_count >= self.queue.maxsize:
            raise DomainError("TASK_QUEUE_FULL", "Task queue is full", status=429)
        record = TaskRecord(
            task_id=uuid4(),
            server_boot_id=self.server_boot_id,
            document_id=document_id,
            kind=kind,
            scope=dict(scope),
            created_at=datetime.now(UTC),
        )
        self._records[record.task_id] = record
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            del self._records[record.task_id]
            raise DomainError("TASK_QUEUE_FULL", "Task queue is full", status=429) from None
        return record.view()

    async def get(self, task_id: UUID) -> TaskView:
        async with self._state_lock:
            self._purge_locked()
            record = self._records.get(task_id)
            if record is not None:
                return record.view()
        if self.store is not None:
            record_from_db = await self._load_from_db(task_id)
            if record_from_db is not None:
                async with self._state_lock:
                    self._records[task_id] = record_from_db
                    self._purge_locked()
                    if task_id in self._records:
                        return record_from_db.view()
        raise DomainError("TASK_EXPIRED", "Task is no longer available", status=410)

    async def answer_stream(self, task_id: UUID) -> AsyncIterator[tuple[str, TaskView]]:
        """Poll the in-memory answer without persisting token events."""

        offset = 0
        while True:
            view = await self.get(task_id)
            if view.kind != JobKind.QA:
                raise DomainError(
                    "TASK_KIND_INVALID", "Answer streaming is only available for QA tasks"
                )
            answer = view.answer or ""
            if len(answer) > offset:
                chunk = answer[offset:]
                offset = len(answer)
                yield chunk, view
            if view.status.terminal:
                if len(answer) == offset:
                    yield "", view
                return
            await asyncio.sleep(0.05)

    async def cancel(self, task_id: UUID) -> TaskView:
        async with self._state_lock:
            record = self._records.get(task_id)
            if record is None:
                raise DomainError("TASK_EXPIRED", "Task is no longer available", status=410)
            if record.status == JobStatus.QUEUED:
                record.cancel_requested = True
                record.status = JobStatus.CANCELLED
                record.message = "Cancelled before execution"
                record.finished_at = datetime.now(UTC)
                if record.execution is not None:
                    record.execution.cancel()
            elif record.status == JobStatus.RUNNING:
                record.cancel_requested = True
                record.message = "Cancellation requested"
                if record.execution is not None:
                    record.execution.cancel()
            view = record.view()
        await self._persist_record(record)
        return view

    async def cancel_document(self, document_id: UUID) -> None:
        async with self._state_lock:
            self._request_document_cancel_locked(document_id)
            affected = [r for r in self._records.values() if r.document_id == document_id]
        for record in affected:
            await self._persist_record(record)
        await self._wait_for_document_idle(document_id)

    async def begin_document_deletion(self, document_id: UUID) -> None:
        """Reserve a document against new work while its data is being removed."""

        async with self._state_lock:
            if document_id in self._deleting_documents:
                raise DomainError(
                    "DOCUMENT_BUSY", "Document deletion is already in progress", status=409
                )
            self._deleting_documents.add(document_id)
            self._request_document_cancel_locked(document_id)
        await self._wait_for_document_idle(document_id)

    async def end_document_deletion(self, document_id: UUID) -> None:
        async with self._state_lock:
            self._deleting_documents.discard(document_id)

    async def active_for(self, document_id: UUID) -> tuple[TaskView, ...]:
        async with self._state_lock:
            return tuple(
                record.view()
                for record in self._records.values()
                if record.document_id == document_id
                and record.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            )

    def active_parse_ids(self, document_id: UUID) -> frozenset[UUID]:
        """Return parse snapshots pinned by active version-scoped operations."""
        result: set[UUID] = set()
        for record in self._records.values():
            if record.document_id != document_id or record.status not in (
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            ):
                continue
            value = record.scope.get("parse_id")
            if not isinstance(value, str):
                continue
            try:
                result.add(UUID(value))
            except ValueError:
                continue
        return frozenset(result)

    async def _dispatch(self) -> None:
        while True:
            record = await self.queue.get()
            try:
                if record.status == JobStatus.CANCELLED or record.cancel_requested:
                    continue
                task = asyncio.create_task(
                    self._execute(record), name=f"easylearn-task-{record.task_id}"
                )
                record.execution = task
                self._active.add(task)
            finally:
                self.queue.task_done()

    def _request_document_cancel_locked(self, document_id: UUID) -> None:
        for record in self._records.values():
            if record.document_id != document_id:
                continue
            if record.status == JobStatus.QUEUED:
                record.cancel_requested = True
                record.status = JobStatus.CANCELLED
                record.message = "Cancelled before execution"
                record.finished_at = datetime.now(UTC)
                if record.execution is not None:
                    record.execution.cancel()
            elif record.status == JobStatus.RUNNING:
                record.cancel_requested = True
                if record.execution is not None:
                    record.execution.cancel()

    async def _wait_for_document_idle(self, document_id: UUID) -> None:
        while True:
            async with self._state_lock:
                running = any(
                    record.document_id == document_id and record.status == JobStatus.RUNNING
                    for record in self._records.values()
                )
            if not running:
                return
            await asyncio.sleep(0.02)

    async def _execute(self, record: TaskRecord) -> None:
        current = asyncio.current_task()
        assert current is not None
        semaphore = self._semaphores[record.kind]
        acquired = False
        context: TaskContext | None = None
        try:
            context = TaskContext(self, record)
            await context.check()
            await semaphore.acquire()
            acquired = True
            await context.check()
            record.status = JobStatus.RUNNING
            record.message = "Running"
            await self._persist_record(record)
            result = await self._executors[record.kind](record, context)
            await self._finish_success(record, result, context._follow_ups)
        except TaskCancelled:
            if record.publication_committed and context is not None:
                await self._finish_success(record, record.result_ref, context._follow_ups)
            else:
                record.status = JobStatus.CANCELLED
                record.message = "Cancelled"
        except asyncio.CancelledError:
            if record.publication_committed and context is not None:
                await self._finish_success(record, record.result_ref, context._follow_ups)
            else:
                record.status = JobStatus.CANCELLED
                record.message = "Cancelled"
        except DomainError as exc:
            if record.publication_committed and context is not None:
                logger.warning("Task %s cleanup failed after publication: %s", record.task_id, exc)
                await self._finish_success(record, record.result_ref, context._follow_ups)
            else:
                record.status = JobStatus.FAILED
                record.failure = JobFailure(
                    code=exc.code, message=exc.message, retryable=exc.retryable
                )
                record.message = exc.message
                logger.warning("Task %s failed: %s", record.task_id, exc)
        except Exception:
            if record.publication_committed and context is not None:
                logger.exception("Task %s cleanup failed after publication", record.task_id)
                await self._finish_success(record, record.result_ref, context._follow_ups)
            else:
                record.status = JobStatus.FAILED
                record.failure = JobFailure(code="TASK_FAILED", message="Task failed unexpectedly")
                record.message = "Task failed unexpectedly"
                logger.exception("Task %s failed unexpectedly", record.task_id)
        finally:
            record.finished_at = datetime.now(UTC) if record.status.terminal else None
            await self._persist_record(record)
            if acquired:
                semaphore.release()
            self._active.discard(current)

    async def _finish_success(
        self,
        record: TaskRecord,
        result: TaskResult,
        follow_ups: list[tuple[JobKind, dict[str, JsonValue]]],
    ) -> None:
        """Publish a result and its dependent tasks as one visible state change."""

        async with self._state_lock:
            if not record.publication_committed and (record.cancel_requested or self.stopping):
                record.status = JobStatus.CANCELLED
                record.message = "Cancelled"
                return

            if not record.publication_committed:
                record.result_ref = dict(result) if result is not None else None
            record.progress = 1
            follow_up_ids: list[JsonValue] = []
            for kind, scope in follow_ups:
                try:
                    follow_up = self._submit_locked(
                        record.document_id,
                        kind,
                        scope,
                        ignore_task_id=record.task_id,
                    )
                except DomainError as exc:
                    record.message = f"Completed; follow-up was not queued: {exc.message}"
                    logger.warning(
                        "Follow-up for task %s was not queued: %s", record.task_id, exc
                    )
                else:
                    follow_up_ids.append(str(follow_up.task_id))

            if follow_up_ids:
                if record.result_ref is None:
                    record.result_ref = {}
                record.result_ref["follow_up_task_id"] = follow_up_ids[0]
                if len(follow_up_ids) > 1:
                    record.result_ref["follow_up_task_ids"] = follow_up_ids
            record.status = JobStatus.SUCCEEDED
            if not record.message.startswith("Completed; follow-up was not queued:"):
                record.message = "Completed"
            new_follow_ups = [
                self._records[UUID(fid)] for fid in follow_up_ids if UUID(fid) in self._records
            ]
        for follow_up_record in new_follow_ups:
            await self._persist_record(follow_up_record)
        await self._persist_record(record)

    def _purge_locked(self) -> None:
        now = datetime.now(UTC)
        for task_id, record in tuple(self._records.items()):
            if (
                record.finished_at is not None
                and (now - record.finished_at).total_seconds() >= self.retention_seconds
            ):
                del self._records[task_id]
