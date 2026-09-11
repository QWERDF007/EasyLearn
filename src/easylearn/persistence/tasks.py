from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from easylearn.database import Database
from easylearn.jobs.schema import JobFailure, JobKind, JobStatus

if TYPE_CHECKING:
    from easylearn.tasks import TaskRecord

logger = logging.getLogger(__name__)


class TaskStore:
    """Encapsulates task persistence, state lease updates, and startup orphan reconciliation."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def reconcile_orphans(self, interrupted_failure_json: str, timestamp: str) -> None:
        async with self.database.transaction() as conn:
            await conn.execute(
                "UPDATE tasks SET status = ?, message = ?, failure_json = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (
                    JobStatus.FAILED.value,
                    "Interrupted by server restart",
                    interrupted_failure_json,
                    timestamp,
                ),
            )

    async def upsert(self, record: TaskRecord) -> None:
        try:
            async with self.database.transaction() as conn:
                await conn.execute(
                    "INSERT INTO tasks ("
                    "task_id, server_boot_id, document_id, kind, scope_json, status, "
                    "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET "
                    "status = excluded.status, "
                    "progress = excluded.progress, "
                    "message = excluded.message, "
                    "cancel_requested = excluded.cancel_requested, "
                    "finished_at = excluded.finished_at, "
                    "result_ref_json = excluded.result_ref_json, "
                    "failure_json = excluded.failure_json, "
                    "answer = excluded.answer",
                    (
                        str(record.task_id),
                        str(record.server_boot_id),
                        str(record.document_id),
                        record.kind.value,
                        json.dumps(record.scope, ensure_ascii=False),
                        record.status.value,
                        record.progress,
                        record.message,
                        1 if record.cancel_requested else 0,
                        record.created_at.isoformat(),
                        record.finished_at.isoformat() if record.finished_at is not None else None,
                        json.dumps(record.result_ref, ensure_ascii=False) if record.result_ref is not None else None,
                        json.dumps(record.failure.model_dump(mode="json"), ensure_ascii=False) if record.failure is not None else None,
                        record.answer,
                    ),
                )
        except Exception:
            logger.warning("Failed to persist task record %s", record.task_id, exc_info=True)

    async def load(self, task_id: UUID) -> TaskRecord | None:
        from easylearn.tasks import TaskRecord

        async with self.database.read() as conn:
            cursor = await conn.execute(
                "SELECT task_id, server_boot_id, document_id, kind, scope_json, status, "
                "progress, message, cancel_requested, created_at, finished_at, result_ref_json, failure_json, answer "
                "FROM tasks WHERE task_id = ?",
                (str(task_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            scope = json.loads(row[4]) if row[4] else {}
            status = JobStatus(row[5])
            created_at = datetime.fromisoformat(row[9])
            finished_at = datetime.fromisoformat(row[10]) if row[10] else None
            result_ref = json.loads(row[11]) if row[11] else None
            failure = JobFailure.model_validate_json(row[12]) if row[12] else None
            return TaskRecord(
                task_id=UUID(row[0]),
                server_boot_id=UUID(row[1]),
                document_id=UUID(row[2]),
                kind=JobKind(row[3]),
                scope=scope,
                created_at=created_at,
                status=status,
                progress=float(row[6]) if row[6] is not None else None,
                message=str(row[7]),
                cancel_requested=bool(row[8]),
                finished_at=finished_at,
                result_ref=result_ref,
                failure=failure,
                answer=row[13],
            )
        except Exception:
            logger.warning("Failed to parse task record from db for %s", task_id, exc_info=True)
            return None
