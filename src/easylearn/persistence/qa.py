from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from easylearn.database import Database
from easylearn.errors import DomainError

if TYPE_CHECKING:
    from easylearn.qa import QACitation, QARecordView


class QAStore:
    """Encapsulates persistence operations for QA interaction history."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def count(self, document_id: UUID) -> int:
        async with self.database.read() as connection:
            row = await (
                await connection.execute(
                    "SELECT COUNT(*) FROM qa_records WHERE document_id = ?",
                    (str(document_id),),
                )
            ).fetchone()
            return int(row[0]) if row else 0

    async def save(self, record: QARecordView) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO qa_records "
                "(id, document_id, parse_id, question, answer, context_json, citations_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.qa_id),
                    str(record.document_id),
                    str(record.parse_id),
                    record.question,
                    record.answer,
                    json.dumps(record.context, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        [
                            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                            for item in record.citations
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    record.created_at.isoformat() if isinstance(record.created_at, datetime) else str(record.created_at),
                ),
            )

    async def list(self, document_id: UUID) -> list[QARecordView]:
        from easylearn.qa import QACitation, QARecordView

        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT id, document_id, parse_id, question, answer, context_json, "
                    "citations_json, created_at FROM qa_records WHERE document_id = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (str(document_id),),
                )
            ).fetchall()
        return [
            QARecordView(
                qa_id=UUID(row[0]),
                document_id=UUID(row[1]),
                parse_id=UUID(row[2]),
                question=row[3],
                answer=row[4],
                context=tuple(json.loads(row[5])),
                citations=tuple(QACitation.model_validate(item) for item in json.loads(row[6])),
                created_at=datetime.fromisoformat(row[7]),
            )
            for row in rows
        ]

    async def delete(self, document_id: UUID, qa_id: UUID) -> None:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM qa_records WHERE id = ? AND document_id = ?",
                (str(qa_id), str(document_id)),
            )
            if cursor.rowcount != 1:
                raise DomainError("QA_NOT_FOUND", "Question record not found", status=404)
