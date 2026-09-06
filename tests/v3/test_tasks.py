import asyncio
import threading
from uuid import UUID, uuid4

import pytest

from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.jobs.schema import JobKind, JobStatus
from easylearn.tasks import TaskContext, TaskManager


async def wait_for_status(manager, task_id, status):
    async with asyncio.timeout(2):
        while True:
            view = await manager.get(task_id)
            if view.status == status:
                return view
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_task_manager_runs_bounded_work_and_reports_progress():
    manager = TaskManager(queue_limit=2)

    async def execute(record, context: TaskContext):
        await context.progress(0.5, "half way")
        return {"value": "done"}

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        accepted = await manager.submit(uuid4(), JobKind.PARSE, {"scope": "all"})
        finished = await wait_for_status(manager, accepted.task_id, JobStatus.SUCCEEDED)
        assert finished.progress == 1
        assert finished.result_ref == {"value": "done"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_parse_and_translation_for_one_document_are_mutually_exclusive():
    manager = TaskManager(queue_limit=4)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        started.set()
        await release.wait()
        await context.check()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    document_id = uuid4()
    try:
        parse = await manager.submit(document_id, JobKind.PARSE, {})
        await started.wait()
        with pytest.raises(Exception, match="Another parse"):
            await manager.submit(document_id, JobKind.TRANSLATE, {})
        release.set()
        await wait_for_status(manager, parse.task_id, JobStatus.SUCCEEDED)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancelled_queued_task_does_not_run():
    manager = TaskManager(queue_limit=2)
    calls = 0
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        nonlocal calls
        calls += 1
        await release.wait()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        first = await manager.submit(uuid4(), JobKind.EXPORT, {})
        second = await manager.submit(uuid4(), JobKind.EXPORT, {})
        await manager.cancel(second.task_id)
        release.set()
        await wait_for_status(manager, first.task_id, JobStatus.SUCCEEDED)
        cancelled = await wait_for_status(manager, second.task_id, JobStatus.CANCELLED)
        assert cancelled.cancel_requested
        assert calls == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_full_queue_is_rejected_without_waiting():
    manager = TaskManager(queue_limit=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        del record
        started.set()
        await release.wait()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        first = await manager.submit(uuid4(), JobKind.EXPORT, {})
        await started.wait()
        with pytest.raises(DomainError, match="queue is full"):
            await manager.submit(uuid4(), JobKind.EXPORT, {})
        release.set()
        await wait_for_status(manager, first.task_id, JobStatus.SUCCEEDED)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancelled_running_task_cannot_publish_a_late_result():
    manager = TaskManager(queue_limit=2)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        del record
        started.set()
        await release.wait()
        return {"late": "value"}

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        accepted = await manager.submit(uuid4(), JobKind.EXPORT, {})
        await started.wait()
        await manager.cancel(accepted.task_id)
        release.set()
        cancelled = await wait_for_status(manager, accepted.task_id, JobStatus.CANCELLED)
        assert cancelled.result_ref is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancellation_during_final_publication_finishes_successfully():
    manager = TaskManager(queue_limit=2)
    publication_started = asyncio.Event()
    release_publication = asyncio.Event()

    async def execute(record, context: TaskContext):
        del record

        async def publish() -> None:
            publication_started.set()
            await release_publication.wait()

        await context.publish({"published": True}, publish)
        return {"published": True}

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        accepted = await manager.submit(uuid4(), JobKind.EXPORT, {})
        await asyncio.wait_for(publication_started.wait(), timeout=1)
        cancelled = await manager.cancel(accepted.task_id)
        assert cancelled.cancel_requested
        release_publication.set()
        finished = await wait_for_status(manager, accepted.task_id, JobStatus.SUCCEEDED)
        assert finished.result_ref == {"published": True}
        assert finished.message == "Completed"
    finally:
        release_publication.set()
        await manager.close()


@pytest.mark.asyncio
async def test_close_marks_a_task_waiting_for_concurrency_as_cancelled():
    manager = TaskManager(queue_limit=4, concurrency={JobKind.EXPORT: 1})
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        del record
        started.set()
        await release.wait()
        await context.check()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    first = await manager.submit(uuid4(), JobKind.EXPORT, {})
    second = await manager.submit(uuid4(), JobKind.EXPORT, {})
    try:
        await started.wait()
        await manager.close()
        closed = await manager.get(second.task_id)
        assert closed.status == JobStatus.CANCELLED
        assert closed.finished_at is not None
    finally:
        release.set()
        await manager.close()
        del first


@pytest.mark.asyncio
async def test_close_marks_tasks_not_yet_dispatched_as_cancelled():
    manager = TaskManager(queue_limit=2)

    async def execute(record, context: TaskContext):
        del record, context
        raise AssertionError("A task queued at close must not execute")

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    accepted = await manager.submit(uuid4(), JobKind.EXPORT, {})
    await manager.close()
    closed = await manager.get(accepted.task_id)
    assert closed.status == JobStatus.CANCELLED
    assert closed.finished_at is not None


@pytest.mark.asyncio
async def test_close_waits_for_a_cancellation_safe_blocking_executor():
    manager = TaskManager(queue_limit=1)
    entered = threading.Event()
    release = threading.Event()

    def blocking_work() -> None:
        entered.set()
        release.wait(2)

    async def execute(record, context: TaskContext):
        del record
        await run_blocking(blocking_work)
        await context.check()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    accepted = await manager.submit(uuid4(), JobKind.EXPORT, {})
    try:
        await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.05)
        assert not closing.done()
        release.set()
        await closing
        closed = await manager.get(accepted.task_id)
        assert closed.status == JobStatus.CANCELLED
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_successful_task_can_enqueue_a_follow_up_after_parent_finishes():
    manager = TaskManager(queue_limit=4)
    calls = []

    async def execute(record, context: TaskContext):
        calls.append(record.kind)
        if record.kind == JobKind.PARSE:
            await context.enqueue_after_success(JobKind.TRANSLATE, {"parse_id": "parse-1"})
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        parent = await manager.submit(uuid4(), JobKind.PARSE, {})
        finished = await wait_for_status(manager, parent.task_id, JobStatus.SUCCEEDED)
        follow_up_id = finished.result_ref["follow_up_task_id"]
        follow_up = await wait_for_status(manager, UUID(follow_up_id), JobStatus.SUCCEEDED)
        assert follow_up.kind == JobKind.TRANSLATE
        assert calls == [JobKind.PARSE, JobKind.TRANSLATE]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_parent_task_is_not_visible_as_succeeded_before_follow_up_is_published():
    class DelayedFollowUpManager(TaskManager):
        def __init__(self):
            super().__init__(queue_limit=4)
            self.follow_up_started = asyncio.Event()
            self.release_follow_up = asyncio.Event()

        async def _finish_success(self, record, result, follow_ups):
            if follow_ups:
                self.follow_up_started.set()
                await self.release_follow_up.wait()
            return await super()._finish_success(record, result, follow_ups)

    manager = DelayedFollowUpManager()

    async def execute(record, context: TaskContext):
        if record.kind == JobKind.PARSE:
            await context.enqueue_after_success(JobKind.TRANSLATE, {"parse_id": "parse-1"})
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        parent = await manager.submit(uuid4(), JobKind.PARSE, {})
        await manager.follow_up_started.wait()
        observed = await manager.get(parent.task_id)
        assert observed.status != JobStatus.SUCCEEDED
        manager.release_follow_up.set()
        finished = await wait_for_status(manager, parent.task_id, JobStatus.SUCCEEDED)
        assert finished.result_ref is not None
        assert "follow_up_task_id" in finished.result_ref
    finally:
        manager.release_follow_up.set()
        await manager.close()


@pytest.mark.asyncio
async def test_active_parse_ids_pin_versions_used_by_queued_tasks():
    manager = TaskManager(queue_limit=2)
    document_id = uuid4()
    parse_id = uuid4()
    release = asyncio.Event()

    async def execute(record, context: TaskContext):
        del record
        await release.wait()
        await context.check()
        return None

    for kind in JobKind:
        manager.register(kind, execute)
    await manager.start()
    try:
        task = await manager.submit(
            document_id, JobKind.EXPORT, {"parse_id": str(parse_id)}
        )
        assert manager.active_parse_ids(document_id) == frozenset({parse_id})
        release.set()
        await wait_for_status(manager, task.task_id, JobStatus.SUCCEEDED)
        assert manager.active_parse_ids(document_id) == frozenset()
    finally:
        await manager.close()
