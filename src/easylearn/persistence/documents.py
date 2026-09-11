from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlite3 import Row

from easylearn.database import Database
from easylearn.errors import DomainError


class DocumentStore:
    """Encapsulates all persistence operations for documents and parse results."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self, document_id: UUID, name: str, original_path: str, created_at: str
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, favorite, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (str(document_id), name, original_path, created_at),
            )

    async def get(self, document_id: UUID) -> Row | None:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT * FROM documents WHERE id = ?", (str(document_id),)
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def list(self, favorite: bool | None = None) -> list[Row]:
        query = "SELECT * FROM documents"
        parameters: tuple[object, ...] = ()
        if favorite is not None:
            query += " WHERE favorite = ?"
            parameters = (int(favorite),)
        query += " ORDER BY created_at DESC, id DESC"
        async with self.database.read() as connection:
            cursor = await connection.execute(query, parameters)
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    async def set_favorite(self, document_id: UUID, favorite: bool) -> None:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE documents SET favorite = ? WHERE id = ?",
                (int(favorite), str(document_id)),
            )
            if cursor.rowcount != 1:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)

    async def delete(self, document_id: UUID) -> None:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM documents WHERE id = ?", (str(document_id),)
            )
            if cursor.rowcount != 1:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)

    async def get_parse(self, document_id: UUID, parse_id: UUID) -> Row | None:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT p.* FROM parse_results p WHERE p.document_id = ? AND p.id = ?",
                (str(document_id), str(parse_id)),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def list_parses_with_translation_counts(self, document_id: UUID) -> list[Row]:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT p.*, "
                "("
                "  SELECT count(*) FROM translations t "
                "  WHERE t.parse_id = p.id "
                "  AND ("
                "    (t.auto_text IS NOT NULL AND t.auto_text != '') "
                "    OR (t.manual_text IS NOT NULL AND t.manual_text != '')"
                "  )"
                ") AS translated_units "
                "FROM parse_results p WHERE p.document_id = ? "
                "ORDER BY p.created_at DESC, p.id DESC",
                (str(document_id),),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    async def record_parse(
        self,
        *,
        document_id: UUID,
        parse_id: UUID,
        preview_path: str,
        ir_path: str,
        raw_path: str | None,
        pages: int,
        metadata: dict[str, Any],
        timestamp: str,
        set_active: bool = True,
        connection: Any = None,
    ) -> None:
        """Atomically record a parse result and optionally make it active."""

        async def _execute_record(conn: Any) -> None:
            exists = await (
                await conn.execute("SELECT 1 FROM documents WHERE id = ?", (str(document_id),))
            ).fetchone()
            if exists is None:
                raise DomainError(
                    "DOCUMENT_NOT_FOUND",
                    "Document was deleted during parsing",
                    status=404,
                )
            await conn.execute(
                "INSERT INTO parse_results "
                "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(parse_id),
                    str(document_id),
                    preview_path,
                    ir_path,
                    raw_path,
                    pages,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                ),
            )
            if set_active:
                await conn.execute(
                    "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                    (str(parse_id), str(document_id)),
                )

        if connection is not None:
            await _execute_record(connection)
        else:
            async with self.database.transaction() as conn:
                await _execute_record(conn)

    async def update_parse_metadata(self, parse_id: UUID, metadata: dict[str, Any]) -> None:
        async with self.database.transaction() as conn:
            await conn.execute(
                "UPDATE parse_results SET metadata_json = ? WHERE id = ?",
                (
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    str(parse_id),
                ),
            )

    async def prune_older_parses(self, document_id: UUID, keep_limit: int = 5) -> list[UUID]:
        return await self.prune_unreferenced_parses(document_id, keep_count=keep_limit, protected_ids=set())

    async def prune_unreferenced_parses(
        self,
        document_id: UUID,
        keep_count: int,
        protected_ids: set[UUID] | frozenset[UUID],
    ) -> list[UUID]:
        async with self.database.read() as connection:
            rows = tuple(
                await (
                    await connection.execute(
                        "SELECT id FROM parse_results WHERE document_id = ? "
                        "ORDER BY created_at DESC, id DESC",
                        (str(document_id),),
                    )
                ).fetchall()
            )
            referenced = {
                UUID(row[0])
                for row in await (
                    await connection.execute(
                        "SELECT DISTINCT parse_id FROM qa_records WHERE document_id = ?",
                        (str(document_id),),
                    )
                ).fetchall()
            }
        candidates = [
            UUID(row[0])
            for row in rows[keep_count:]
            if UUID(row[0]) not in referenced and UUID(row[0]) not in protected_ids
        ]
        if not candidates:
            return []
        async with self.database.transaction() as connection:
            placeholders = ",".join("?" for _ in candidates)
            await connection.execute(
                f"DELETE FROM parse_results WHERE id IN ({placeholders})",
                tuple(str(cid) for cid in candidates),
            )
        return candidates
