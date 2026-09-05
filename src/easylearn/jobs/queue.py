import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol
from uuid import UUID

from dramatiq import Actor, Broker, Message, Worker
from dramatiq.asyncio import get_event_loop_thread
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO, Middleware, MiddlewareError, Retries
from redis import Redis
from sqlalchemy import text

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.jobs.delivery import Outbox
from easylearn.jobs.schema import JobKind
from easylearn.jobs.service import JobService
from easylearn.parses.worker import ParseWorker
from easylearn.previews.service import PreviewService
from easylearn.storage import LocalStorage


class _Executor(Protocol):
    async def execute(self, job_id: UUID, *, generation: int) -> None: ...


@dataclass
class _Runtime:
    executors: dict[JobKind, _Executor]
    active: set[asyncio.Task[None]] = field(default_factory=set)
    stopping: bool = False

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: Settings, kinds: set[JobKind]) -> AsyncIterator["_Runtime"]:
        async with AsyncExitStack() as stack:
            database = Database(settings.database_url.get_secret_value())
            stack.push_async_callback(database.close)
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            storage = LocalStorage(settings.storage_root)
            jobs = JobService(database)
            executors: dict[JobKind, _Executor] = {}
            if JobKind.PREVIEW in kinds:
                executors[JobKind.PREVIEW] = PreviewService(database, storage, settings)
            if JobKind.PARSE in kinds:
                executors[JobKind.PARSE] = await stack.enter_async_context(
                    ParseWorker.open(jobs, storage, settings)
                )
            runtime = cls(executors)
            try:
                yield runtime
            finally:
                await runtime.cancel_active()

    async def execute(self, kind: JobKind, job_id: str, generation: int) -> None:
        if self.stopping:
            raise RuntimeError("Worker is stopping; unclaimed work remains recoverable in Outbox")
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            await self.executors[kind].execute(UUID(job_id), generation=generation)
        finally:
            self.active.discard(task)

    async def cancel_active(self) -> None:
        self.stopping = True
        active = tuple(self.active)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


class _Lifecycle(Middleware):
    def __init__(self, settings: Settings, kinds: set[JobKind]) -> None:
        self.settings = settings
        self.kinds = kinds
        self.context: AbstractAsyncContextManager[_Runtime] | None = None
        self.runtime: _Runtime | None = None

    def before_worker_boot(self, broker: Broker, worker: Worker) -> None:
        thread = get_event_loop_thread()
        assert thread is not None
        self.context = _Runtime.open(self.settings, self.kinds)
        try:
            self.runtime = thread.run_coroutine(self.context.__aenter__())
        except Exception as exc:
            raise MiddlewareError("Worker resources could not be initialized") from exc

    def before_worker_shutdown(self, broker: Broker, worker: Worker) -> None:
        thread = get_event_loop_thread()
        if self.runtime is not None and thread is not None:
            thread.run_coroutine(self.runtime.cancel_active())

    def after_worker_shutdown(self, broker: Broker, worker: Worker) -> None:
        thread = get_event_loop_thread()
        if self.context is not None and thread is not None:
            thread.run_coroutine(self.context.__aexit__(None, None, None))
        self.runtime = None

    async def execute(self, kind: JobKind, job_id: str, generation: int) -> None:
        if self.runtime is None:
            raise RuntimeError("Worker runtime is not initialized")
        await self.runtime.execute(kind, job_id, generation)


class JobQueue:
    """One broker protocol for producers and one owned event loop per consumer process."""

    def __init__(self, settings: Settings, *, broker: Broker | None = None) -> None:
        if settings.queue is None:
            raise DomainError("QUEUE_NOT_CONFIGURED", "Configure a Redis broker", status=503)
        self.settings = settings
        self.options = settings.queue
        self.broker = broker or RedisBroker(
            client=Redis.from_url(
                self.options.redis_url.get_secret_value(),
                socket_timeout=self.options.socket_timeout_seconds,
                socket_connect_timeout=self.options.socket_timeout_seconds,
            ),
            namespace=self.options.namespace,
            middleware=[],
        )
        for kind in JobKind:
            self.broker.declare_queue(kind.queue_name)

    async def dispatch_once(self, database: Database) -> int:
        outbox = Outbox(database)
        deliveries = await outbox.claim(self.options.outbox_batch_size)
        for delivery in deliveries:
            message: Message[None] = Message(
                queue_name=delivery.kind.queue_name,
                actor_name=delivery.kind.queue_name,
                args=(str(delivery.job_id), delivery.generation),
                kwargs={},
                options={},
            )
            await run_blocking(self.broker.enqueue, message)
            await outbox.acknowledge(delivery)
        return len(deliveries)

    @contextmanager
    def worker(self, kinds: set[JobKind]) -> Iterator[None]:
        if not kinds:
            raise ValueError("Select at least one worker queue")
        lifecycle = _Lifecycle(self.settings, kinds)
        self.broker.add_middleware(AsyncIO())
        self.broker.add_middleware(lifecycle)
        self.broker.add_middleware(Retries(max_retries=0))
        for kind in kinds:
            Actor(
                partial(lifecycle.execute, kind),
                broker=self.broker,
                actor_name=kind.queue_name,
                queue_name=kind.queue_name,
                priority=0,
                options={},
            )
        worker = Worker(
            self.broker,
            queues={kind.queue_name for kind in kinds},
            worker_threads=self.options.worker_threads,
        )
        started = False
        try:
            worker.start()
            started = True
            yield
        finally:
            if started:
                worker.stop(timeout=int(self.options.shutdown_timeout_seconds * 1000))
            else:
                self.broker.emit_after("worker_shutdown", worker)

    def close(self) -> None:
        self.broker.close()
        if isinstance(self.broker, RedisBroker):
            self.broker.client.close()
