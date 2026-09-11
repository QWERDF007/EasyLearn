from __future__ import annotations

from uuid import UUID
from sqlite3 import Row

from easylearn.database import Database
from easylearn.errors import DomainError


class SourceEditStore:
    """Encapsulates persistence operations for source edits with optimistic revision protection."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_edits(self, parse_id: UUID) -> list[Row]:
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT block_id, node_id, text, revision, updated_at "
                    "FROM source_edits WHERE parse_id = ? ORDER BY block_id, node_id",
                    (str(parse_id),),
                )
            ).fetchall()
            return list(rows)

    async def get_edit(self, parse_id: UUID, block_id: str, node_id: str) -> Row | None:
        async with self.database.read() as connection:
            row = await (
                await connection.execute(
                    "SELECT text, revision, updated_at FROM source_edits "
                    "WHERE parse_id = ? AND block_id = ? AND node_id = ?",
                    (str(parse_id), block_id, node_id),
                )
            ).fetchone()
            return row

    async def save_edit(
        self,
        *,
        parse_id: UUID,
        block_id: str,
        node_id: str,
        text: str,
        expected_revision: int,
        updated_at: str,
    ) -> tuple[int, str]:
        """Save a source edit under a short transaction with optimistic locking."""
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT text, revision, updated_at FROM source_edits "
                    "WHERE parse_id = ? AND block_id = ? AND node_id = ?",
                    (str(parse_id), block_id, node_id),
                )
            ).fetchone()
            current_revision = int(row[1]) if row is not None else 0
            if current_revision != expected_revision:
                raise DomainError(
                    "SOURCE_EDIT_CONFLICT",
                    "Source text changed; reload the block before saving",
                    status=409,
                )
            revision = current_revision + 1
            await connection.execute(
                "INSERT INTO source_edits "
                "(parse_id, block_id, node_id, text, revision, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(parse_id, block_id, node_id) DO UPDATE SET "
                "text = excluded.text, revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (
                    str(parse_id),
                    block_id,
                    node_id,
                    text,
                    revision,
                    updated_at,
                ),
            )
            return revision, updated_at
