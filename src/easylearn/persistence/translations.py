from __future__ import annotations

from collections.abc import Iterable
from sqlite3 import Row
from typing import Any
from uuid import UUID, uuid4

from easylearn.database import Database
from easylearn.errors import DomainError


class TranslationStore:
    """Encapsulates all persistence operations for translations and translation history."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_translations(self, parse_id: UUID) -> list[Row]:
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision, source_fingerprint "
                    "FROM translations WHERE parse_id = ?",
                    (str(parse_id),),
                )
            ).fetchall()
            return list(rows)

    async def get_translation_row(
        self, parse_id: UUID, block_id: str, unit_id: str
    ) -> Row | None:
        async with self.database.read() as connection:
            row = await (
                await connection.execute(
                    "SELECT auto_text, manual_text, use_manual, locked, revision, source_fingerprint "
                    "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(parse_id), block_id, unit_id),
                )
            ).fetchone()
            return row

    async def get_history(
        self,
        parse_id: UUID,
        unit_id: str,
        block_id: str | None = None,
        limit: int | None = None,
    ) -> list[Row]:
        query = (
            "SELECT id, parse_id, block_id, unit_id, text, origin, revision, created_at "
            "FROM translation_history "
            "WHERE parse_id = ? AND unit_id = ?"
        )
        params: list[Any] = [str(parse_id), unit_id]
        if block_id is not None:
            query += " AND block_id = ?"
            params.append(block_id)
        query += " ORDER BY created_at DESC, revision DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        async with self.database.read() as connection:
            rows = await (await connection.execute(query, tuple(params))).fetchall()
            return list(rows)

    async def trim_history(
        self, connection: Any, parse_id: UUID, block_id: str, unit_id: str, limit: int
    ) -> None:
        cursor = await connection.execute(
            "SELECT id FROM translation_history "
            "WHERE parse_id = ? AND block_id = ? AND unit_id = ? "
            "ORDER BY created_at DESC, revision DESC",
            (str(parse_id), block_id, unit_id),
        )
        rows = await cursor.fetchall()
        if len(rows) <= limit:
            return
        to_delete = [row[0] for row in rows[limit:]]
        placeholders = ",".join("?" for _ in to_delete)
        await connection.execute(
            f"DELETE FROM translation_history WHERE id IN ({placeholders})", tuple(to_delete)
        )

    async def save_batch(
        self,
        *,
        parse_id: UUID,
        batch: Iterable[dict[str, Any]],
        snapshots: dict[str, tuple[int, str | None, bool, bool]],
        timestamp: str,
        history_limit: int | None = None,
    ) -> int:
        conflict_count = 0
        async with self.database.transaction() as connection:
            for item in batch:
                unit_id = str(item["unit_id"])
                block_id = str(item["block_id"])
                value = str(item["value"])
                unit_fp = str(item.get("fingerprint", ""))
                row = await (
                    await connection.execute(
                        "SELECT auto_text, manual_text, use_manual, locked, revision "
                        "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                        (str(parse_id), block_id, unit_id),
                    )
                ).fetchone()
                if row is None:
                    await connection.execute(
                        "INSERT INTO translations "
                        "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, "
                        "locked, revision, source_fingerprint, updated_at) "
                        "VALUES (?, ?, ?, ?, NULL, 0, 0, 0, ?, ?)",
                        (str(parse_id), block_id, unit_id, value, unit_fp, timestamp),
                    )
                    revision = 0
                else:
                    revision = row[4]
                    if revision != snapshots.get(unit_id, (0, None, False, False))[0]:
                        conflict_count += 1
                    await connection.execute(
                        "UPDATE translations SET auto_text = ?, source_fingerprint = ?, updated_at = ? "
                        "WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                        (value, unit_fp, timestamp, str(parse_id), block_id, unit_id),
                    )
                await connection.execute(
                    "INSERT INTO translation_history "
                    "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'auto', ?, ?)",
                    (
                        str(uuid4()),
                        str(parse_id),
                        block_id,
                        unit_id,
                        value,
                        revision,
                        timestamp,
                    ),
                )
                if history_limit is not None:
                    await self.trim_history(
                        connection, parse_id, block_id, unit_id, history_limit
                    )
        return conflict_count

    async def edit(
        self,
        *,
        parse_id: UUID,
        block_id: str,
        unit_id: str,
        text: str | None,
        use_manual: bool | None,
        locked: bool | None,
        expected_revision: int,
        unit_fingerprint: str,
        default_source_text: str = "",
        timestamp: str,
        history_limit: int | None = None,
    ) -> tuple[int, str, str | None, str | None, bool, bool]:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT auto_text, manual_text, use_manual, locked, revision "
                    "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(parse_id), block_id, unit_id),
                )
            ).fetchone()
            current_revision = row[4] if row else 0
            if current_revision != expected_revision:
                raise DomainError(
                    "TRANSLATION_REVISION_CONFLICT",
                    "Translation changed; keep the submitted draft and reload the current value",
                    status=409,
                )
            if row and row[3] and text is not None and locked is not False:
                raise DomainError(
                    "TRANSLATION_LOCKED", "Unlock the translation before editing", status=409
                )
            auto_text = row[0] if row else None
            old_manual = row[1] if row else None
            old_use_manual = bool(row[2]) if row else False
            old_locked = bool(row[3]) if row else False
            manual_text = text if text is not None else old_manual
            final_use_manual = (
                use_manual
                if use_manual is not None
                else (text is not None or old_use_manual)
            )
            final_locked = (
                locked
                if locked is not None
                else (False if final_use_manual is False else old_locked)
            )
            revision = current_revision + 1
            effective_before = (
                old_manual
                if old_use_manual and old_manual is not None
                else auto_text or default_source_text
            )
            await connection.execute(
                "INSERT INTO translation_history "
                "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)",
                (
                    str(uuid4()),
                    str(parse_id),
                    block_id,
                    unit_id,
                    effective_before,
                    revision,
                    timestamp,
                ),
            )
            if row is None:
                await connection.execute(
                    "INSERT INTO translations "
                    "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision, source_fingerprint, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(parse_id),
                        block_id,
                        unit_id,
                        auto_text,
                        manual_text,
                        1 if final_use_manual else 0,
                        1 if final_locked else 0,
                        revision,
                        unit_fingerprint,
                        timestamp,
                    ),
                )
            else:
                await connection.execute(
                    "UPDATE translations "
                    "SET manual_text = ?, use_manual = ?, locked = ?, revision = ?, "
                    "source_fingerprint = ?, updated_at = ? "
                    "WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (
                        manual_text,
                        1 if final_use_manual else 0,
                        1 if final_locked else 0,
                        revision,
                        unit_fingerprint,
                        timestamp,
                        str(parse_id),
                        block_id,
                        unit_id,
                    ),
                )
            if history_limit is not None:
                await self.trim_history(
                    connection, parse_id, block_id, unit_id, history_limit
                )
            return revision, timestamp, auto_text, manual_text, final_use_manual, final_locked

    async def restore(
        self,
        *,
        parse_id: UUID,
        block_id: str,
        unit_id: str,
        history_id: UUID,
        expected_revision: int,
        unit_fingerprint: str,
        default_source_text: str,
        timestamp: str,
        history_limit: int | None = None,
    ) -> tuple[int, str, str | None, str, bool, bool]:
        async with self.database.transaction() as connection:
            history = await (
                await connection.execute(
                    "SELECT text, revision FROM translation_history "
                    "WHERE id = ? AND parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(history_id), str(parse_id), block_id, unit_id),
                )
            ).fetchone()
            if history is None:
                raise DomainError(
                    "TRANSLATION_HISTORY_NOT_FOUND", "Translation history not found", status=404
                )
            row = await (
                await connection.execute(
                    "SELECT auto_text, manual_text, use_manual, locked, revision "
                    "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(parse_id), block_id, unit_id),
                )
            ).fetchone()
            current_revision = row[4] if row else 0
            if current_revision != expected_revision:
                raise DomainError(
                    "TRANSLATION_REVISION_CONFLICT",
                    "Translation changed; keep the submitted draft and reload the current value",
                    status=409,
                )
            if row is not None and row[3]:
                raise DomainError(
                    "TRANSLATION_LOCKED",
                    "Unlock the translation before restoring a revision",
                    status=409,
                )
            auto_text = row[0] if row is not None else None
            previous = (
                row[1]
                if row is not None and row[2] and row[1] is not None
                else row[0]
                if row is not None and row[0] is not None
                else default_source_text
            )
            revision = current_revision + 1
            await connection.execute(
                "INSERT INTO translation_history "
                "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'restore', ?, ?)",
                (
                    str(uuid4()),
                    str(parse_id),
                    block_id,
                    unit_id,
                    previous,
                    revision,
                    timestamp,
                ),
            )
            if row is None:
                await connection.execute(
                    "INSERT INTO translations "
                    "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision, source_fingerprint, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, 1, 0, ?, ?, ?)",
                    (
                        str(parse_id),
                        block_id,
                        unit_id,
                        history[0],
                        revision,
                        unit_fingerprint,
                        timestamp,
                    ),
                )
            else:
                await connection.execute(
                    "UPDATE translations "
                    "SET manual_text = ?, use_manual = 1, locked = 0, revision = ?, "
                    "source_fingerprint = ?, updated_at = ? "
                    "WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (
                        history[0],
                        revision,
                        unit_fingerprint,
                        timestamp,
                        str(parse_id),
                        block_id,
                        unit_id,
                    ),
                )
            if history_limit is not None:
                await self.trim_history(
                    connection, parse_id, block_id, unit_id, history_limit
                )
            return revision, timestamp, auto_text, history[0], True, False
