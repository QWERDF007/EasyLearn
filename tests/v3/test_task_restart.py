import asyncio
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, Settings
from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobFailure, JobKind, JobStatus
from easylearn.main import create_app
from easylearn.publication import ArtifactPublisher, PublicationKind
from easylearn.tasks import TaskContext, TaskManager


@pytest_asyncio.fixture
async def task_db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.open()
    # Insert a dummy document so foreign keys pass
    doc_id = str(uuid4())
    async with database.transaction() as conn:
        await conn.execute(
            "INSERT INTO documents (id, name, original_path, active_parse_id, favorite, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, "test.pdf", "/tmp/test.pdf", None, 0, datetime.now(UTC).isoformat()),
        )
    yield database, UUID(doc_id)
    await database.close()


@pytest.mark.asyncio
async def test_interrupted_tasks_marked_failed_on_restart(task_db):
    database, doc_id = task_db

    # Simulate tasks from a previous server boot left in queued and running state
    old_boot_id = uuid4()
    queued_task_id = uuid4()
    running_task_id = uuid4()
    succeeded_task_id = uuid4()

    async with database.transaction() as conn:
        await conn.execute(
            "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
            "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(queued_task_id),
                str(old_boot_id),
                str(doc_id),
                JobKind.PARSE.value,
                json.dumps({"parse_id": str(uuid4())}),
                JobStatus.QUEUED.value,
                None,
                "Queued",
                0,
                datetime.now(UTC).isoformat(),
                None,
                None,
                None,
                None,
            ),
        )
        await conn.execute(
            "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
            "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(running_task_id),
                str(old_boot_id),
                str(doc_id),
                JobKind.TRANSLATE.value,
                json.dumps({"parse_id": str(uuid4())}),
                JobStatus.RUNNING.value,
                0.5,
                "Running",
                0,
                datetime.now(UTC).isoformat(),
                None,
                None,
                None,
                None,
            ),
        )
        await conn.execute(
            "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
            "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(succeeded_task_id),
                str(old_boot_id),
                str(doc_id),
                JobKind.EXPORT.value,
                json.dumps({"export_id": str(uuid4())}),
                JobStatus.SUCCEEDED.value,
                1.0,
                "Completed",
                0,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                json.dumps({"file_id": "test"}),
                None,
                None,
            ),
        )

    # Now start a new TaskManager (server restart)
    new_manager = TaskManager(database=database)

    async def noop_executor(record, context):
        return None

    for kind in JobKind:
        new_manager.register(kind, noop_executor)

    await new_manager.start()
    try:
        # Document should have NO active tasks running
        active = await new_manager.active_for(doc_id)
        assert len(active) == 0

        # Queued task from before restart should be marked FAILED with TASK_INTERRUPTED
        queued_view = await new_manager.get(queued_task_id)
        assert queued_view.status == JobStatus.FAILED
        assert queued_view.failure is not None
        assert queued_view.failure.code == "TASK_INTERRUPTED"
        assert queued_view.failure.retryable is True
        assert queued_view.finished_at is not None

        # Running task from before restart should also be marked FAILED with TASK_INTERRUPTED
        running_view = await new_manager.get(running_task_id)
        assert running_view.status == JobStatus.FAILED
        assert running_view.failure is not None
        assert running_view.failure.code == "TASK_INTERRUPTED"
        assert running_view.failure.retryable is True
        assert running_view.finished_at is not None

        # Succeeded task should remain SUCCEEDED
        succeeded_view = await new_manager.get(succeeded_task_id)
        assert succeeded_view.status == JobStatus.SUCCEEDED
        assert succeeded_view.failure is None
    finally:
        await new_manager.close()


@pytest.mark.asyncio
async def test_retry_creates_new_task_identity_and_preserves_history(task_db):
    database, doc_id = task_db

    manager = TaskManager(database=database)
    executed_records = []

    async def execute(record, context: TaskContext):
        executed_records.append(record.task_id)
        return {"retried": True}

    for kind in JobKind:
        manager.register(kind, execute)

    await manager.start()
    try:
        # Submit a task that fails retryably
        original_task_id = uuid4()
        async with database.transaction() as conn:
            await conn.execute(
                "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
                "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(original_task_id),
                    str(manager.server_boot_id),
                    str(doc_id),
                    JobKind.PARSE.value,
                    json.dumps({"parse_id": "test-p1"}),
                    JobStatus.FAILED.value,
                    0.2,
                    "Interrupted",
                    0,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    None,
                    json.dumps({"code": "TASK_INTERRUPTED", "message": "interrupted", "retryable": True}),
                    None,
                ),
            )

        # Retry the task
        new_view = await manager.retry(original_task_id)
        assert new_view.task_id != original_task_id
        assert new_view.document_id == doc_id
        assert new_view.kind == JobKind.PARSE
        assert new_view.scope == {"parse_id": "test-p1"}

        # Wait for the retried task to complete
        await asyncio.sleep(0.1)
        retried_view = await manager.get(new_view.task_id)
        assert retried_view.status == JobStatus.SUCCEEDED
        assert retried_view.result_ref == {"retried": True}

        # Verify the original task history is preserved and untouched
        orig_view = await manager.get(original_task_id)
        assert orig_view.task_id == original_task_id
        assert orig_view.status == JobStatus.FAILED
        assert orig_view.failure.code == "TASK_INTERRUPTED"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_retry_non_retryable_or_active_task_raises_domain_error(task_db):
    database, doc_id = task_db

    manager = TaskManager(database=database)

    async def noop_executor(record, context):
        return None

    for kind in JobKind:
        manager.register(kind, noop_executor)

    await manager.start()
    try:
        # Non-retryable failed task
        task_id_1 = uuid4()
        async with database.transaction() as conn:
            await conn.execute(
                "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
                "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task_id_1),
                    str(manager.server_boot_id),
                    str(doc_id),
                    JobKind.PARSE.value,
                    json.dumps({}),
                    JobStatus.FAILED.value,
                    None,
                    "Failed permanent",
                    0,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    None,
                    json.dumps({"code": "PERMANENT_ERROR", "message": "no retry", "retryable": False}),
                    None,
                ),
            )

        with pytest.raises(DomainError) as exc_info:
            await manager.retry(task_id_1)
        assert exc_info.value.code == "TASK_NOT_RETRYABLE"
        assert exc_info.value.status == 409
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_task_retention_policy_on_database_loaded_tasks(task_db):
    database, doc_id = task_db

    # Finished long ago (older than retention_seconds)
    old_task_id = uuid4()
    expired_time = (datetime.now(UTC) - timedelta(seconds=2000)).isoformat()

    async with database.transaction() as conn:
        await conn.execute(
            "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
            "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(old_task_id),
                str(uuid4()),
                str(doc_id),
                JobKind.PARSE.value,
                json.dumps({}),
                JobStatus.SUCCEEDED.value,
                1.0,
                "Completed",
                0,
                expired_time,
                expired_time,
                json.dumps({}),
                None,
                None,
            ),
        )

    manager = TaskManager(database=database, retention_seconds=1800)

    async def noop_executor(record, context):
        return None

    for kind in JobKind:
        manager.register(kind, noop_executor)

    await manager.start()
    try:
        with pytest.raises(DomainError) as exc_info:
            await manager.get(old_task_id)
        assert exc_info.value.code == "TASK_EXPIRED"
        assert exc_info.value.status == 410
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_retry_already_published_result_does_not_republish_or_alter_active_pointer(task_db):
    database, doc_id = task_db

    old_parse_id = uuid4()
    new_parse_id = uuid4()

    # In database: old_parse exists, but new_parse is the active one
    async with database.transaction() as conn:
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(old_parse_id),
                str(doc_id),
                "preview.pdf",
                "document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(new_parse_id),
                str(doc_id),
                "preview.pdf",
                "document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(new_parse_id), str(doc_id)),
        )

        # Record publication for old_parse as recorded
        await conn.execute(
            "INSERT INTO publications (id, document_id, artifact_id, kind, staging_path, destination_path, payload_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(doc_id),
                str(old_parse_id),
                PublicationKind.PARSE.value,
                "/staging",
                "/dest",
                json.dumps({"parse_id": str(old_parse_id)}),
                "recorded",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

        # An interrupted task for old_parse
        interrupted_task_id = uuid4()
        await conn.execute(
            "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
            "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(interrupted_task_id),
                str(uuid4()),
                str(doc_id),
                JobKind.PARSE.value,
                json.dumps({"parse_id": str(old_parse_id)}),
                JobStatus.FAILED.value,
                0.9,
                "Interrupted",
                0,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                None,
                json.dumps({"code": "TASK_INTERRUPTED", "message": "interrupted", "retryable": True}),
                None,
            ),
        )

    manager = TaskManager(database=database)

    # Parser executor simulating parser.execute check
    async def parse_executor(record, context):
        parse_id = UUID(str(record.scope["parse_id"]))
        async with database.read() as connection:
            cursor = await connection.execute(
                "SELECT id FROM parse_results WHERE id = ? AND document_id = ?",
                (str(parse_id), str(record.document_id)),
            )
            already_recorded = await cursor.fetchone()
        if already_recorded is not None:
            return {"parse_id": str(parse_id), "file_id": f"ir:{parse_id}"}
        raise AssertionError("Should have detected already recorded parse")

    for kind in JobKind:
        manager.register(kind, parse_executor)

    await manager.start()
    try:
        new_task = await manager.retry(interrupted_task_id)
        assert new_task.task_id != interrupted_task_id

        # Wait for retry to complete
        await asyncio.sleep(0.1)
        done_view = await manager.get(new_task.task_id)
        assert done_view.status == JobStatus.SUCCEEDED
        assert done_view.result_ref == {
            "parse_id": str(old_parse_id),
            "file_id": f"ir:{old_parse_id}",
        }

        # Verify active_parse_id was NOT changed to old_parse_id
        async with database.read() as conn:
            cursor = await conn.execute("SELECT active_parse_id FROM documents WHERE id = ?", (str(doc_id),))
            row = await cursor.fetchone()
            assert row[0] == str(new_parse_id)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_task_retry_api_endpoint(tmp_path):
    settings = Settings(app=AppSettings(data_dir=tmp_path))
    app = create_app(settings)
    pdf_bytes = b"%PDF-1.4 test file content"

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # Create document
        doc_resp = await client.post(
            "/api/documents", files={"file": ("test.pdf", pdf_bytes, "application/pdf")}
        )
        assert doc_resp.status_code == 201
        doc_id = doc_resp.json()["document_id"]

        # Insert a retryable interrupted task
        task_id = uuid4()
        database = app.state.services.database
        async with database.transaction() as conn:
            await conn.execute(
                "INSERT INTO tasks (task_id, server_boot_id, document_id, kind, scope_json, status, "
                "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task_id),
                    str(uuid4()),
                    str(doc_id),
                    JobKind.PARSE.value,
                    json.dumps({"parse_id": str(uuid4())}),
                    JobStatus.FAILED.value,
                    0.1,
                    "Interrupted by server restart",
                    0,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                    None,
                    json.dumps({"code": "TASK_INTERRUPTED", "message": "restart", "retryable": True}),
                    None,
                ),
            )

        # Call POST /api/tasks/{task_id}/retry
        retry_resp = await client.post(f"/api/tasks/{task_id}/retry")
        assert retry_resp.status_code == 202
        new_task = retry_resp.json()
        assert new_task["task_id"] != str(task_id)
        assert new_task["document_id"] == str(doc_id)
        assert new_task["status"] in ("queued", "running", "succeeded")

        # Original task is still FAILED
        orig_resp = await client.get(f"/api/tasks/{task_id}")
        assert orig_resp.status_code == 200
        assert orig_resp.json()["status"] == "failed"


