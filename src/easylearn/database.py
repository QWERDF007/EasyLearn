"""The small SQLite access layer used by the application.

There is deliberately one connection per application process. The lock is
part of the interface: a repository operation owns it for its complete short
transaction, so no caller can observe a half-written document or revision.
Long-running parsing and model requests never run while this lock is held.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 5

_DOCUMENTS_COLUMNS = """
(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    original_path TEXT NOT NULL,
    active_parse_id TEXT,
    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    created_at TEXT NOT NULL
)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents (
    {_DOCUMENTS_COLUMNS.strip()[1:-1]}
);

CREATE TABLE IF NOT EXISTS parse_results (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    preview_path TEXT NOT NULL,
    ir_path TEXT NOT NULL,
    raw_path TEXT,
    pages INTEGER NOT NULL CHECK (pages > 0),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, id)
);

CREATE INDEX IF NOT EXISTS ix_parse_results_document_created
    ON parse_results(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS translations (
    parse_id TEXT NOT NULL REFERENCES parse_results(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    auto_text TEXT,
    manual_text TEXT,
    use_manual INTEGER NOT NULL DEFAULT 0 CHECK (use_manual IN (0, 1)),
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    source_fingerprint TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (parse_id, block_id, unit_id)
);

CREATE TABLE IF NOT EXISTS translation_history (
    id TEXT PRIMARY KEY,
    parse_id TEXT NOT NULL REFERENCES parse_results(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    text TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('manual', 'auto', 'restore')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_translation_history_unit
    ON translation_history(parse_id, block_id, unit_id, created_at DESC);

CREATE TABLE IF NOT EXISTS source_edits (
    parse_id TEXT NOT NULL REFERENCES parse_results(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (parse_id, block_id, node_id)
);

CREATE TABLE IF NOT EXISTS qa_records (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parse_id TEXT NOT NULL REFERENCES parse_results(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    context_json TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_qa_records_document_created
    ON qa_records(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('parse', 'export')),
    staging_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('staged', 'validated', 'published', 'recorded', 'abandoned', 'reconciled')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, artifact_id)
);

CREATE INDEX IF NOT EXISTS ix_publications_status
    ON publications(status, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    server_boot_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL,
    message TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    result_ref_json TEXT,
    failure_json TEXT,
    answer TEXT
);

CREATE INDEX IF NOT EXISTS ix_tasks_document_created
    ON tasks(document_id, created_at DESC);
"""


class Database:
    """One process-local SQLite connection with explicit transaction seams."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._lock:
            if self.connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self.path)
            try:
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.execute("PRAGMA busy_timeout = 5000")
                await connection.execute("PRAGMA journal_mode = WAL")
                cursor = await connection.execute("PRAGMA user_version")
                row = await cursor.fetchone()
                version = int(row[0]) if row else 0
                await cursor.close()
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database version {version} is newer than supported version {SCHEMA_VERSION}"
                    )
                if version == 0:
                    await connection.executescript(_SCHEMA)
                    await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version == 1:
                    await _migrate_v1_to_v2(connection)
                    await _migrate_v2_to_v3(connection)
                    await _migrate_v3_to_v4(connection)
                    await _migrate_v4_to_v5(connection)
                elif version == 2:
                    await _migrate_v2_to_v3(connection)
                    await _migrate_v3_to_v4(connection)
                    await _migrate_v4_to_v5(connection)
                elif version == 3:
                    await _migrate_v3_to_v4(connection)
                    await _migrate_v4_to_v5(connection)
                elif version == 4:
                    await _migrate_v4_to_v5(connection)
            except BaseException:
                try:
                    await connection.close()
                except BaseException:
                    pass
                raise
            self.connection = connection

    async def close(self) -> None:
        async with self._lock:
            if self.connection is not None:
                await self.connection.close()
                self.connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not open")
        return self.connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run one short write transaction while holding the process lock."""

        connection = self._require_connection()
        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize a read with writes on the single connection."""

        connection = self._require_connection()
        async with self._lock:
            yield connection


async def _migrate_v1_to_v2(connection: aiosqlite.Connection) -> None:
    """Remove the legacy global path uniqueness without losing document rows."""

    await connection.execute("PRAGMA foreign_keys = OFF")
    try:
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(f"CREATE TABLE documents_v2 {_DOCUMENTS_COLUMNS}")
        await connection.execute(
            "INSERT INTO documents_v2 "
            "(id, name, original_path, active_parse_id, favorite, created_at) "
            "SELECT id, name, original_path, active_parse_id, favorite, created_at "
            "FROM documents"
        )
        await connection.execute("DROP TABLE documents")
        await connection.execute("ALTER TABLE documents_v2 RENAME TO documents")
        await connection.execute("PRAGMA user_version = 2")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise
    finally:
        await connection.execute("PRAGMA foreign_keys = ON")


async def _migrate_v2_to_v3(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS source_edits ("
        "parse_id TEXT NOT NULL REFERENCES parse_results(id) ON DELETE CASCADE,"
        "block_id TEXT NOT NULL, node_id TEXT NOT NULL, text TEXT NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),"
        "updated_at TEXT NOT NULL, PRIMARY KEY (parse_id, block_id, node_id))"
    )
    await connection.execute("PRAGMA user_version = 3")


async def _migrate_v3_to_v4(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS publications ("
        "id TEXT PRIMARY KEY, document_id TEXT NOT NULL, artifact_id TEXT NOT NULL,"
        "kind TEXT NOT NULL CHECK (kind IN ('parse', 'export')),"
        "staging_path TEXT NOT NULL, destination_path TEXT NOT NULL, payload_json TEXT NOT NULL,"
        "status TEXT NOT NULL CHECK (status IN ('staged', 'validated', 'published',"
        "'recorded', 'abandoned', 'reconciled')), created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL, UNIQUE (kind, artifact_id))"
    )
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_publications_status "
        "ON publications(status, created_at)"
    )
    table_info = await (await connection.execute("PRAGMA table_info(translations)")).fetchall()
    columns = {row[1] for row in table_info}
    if columns and "source_fingerprint" not in columns:
        await connection.execute("ALTER TABLE translations ADD COLUMN source_fingerprint TEXT")
    await connection.execute("PRAGMA user_version = 4")


async def _migrate_v4_to_v5(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "task_id TEXT PRIMARY KEY, "
        "server_boot_id TEXT NOT NULL, "
        "document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
        "kind TEXT NOT NULL, "
        "scope_json TEXT NOT NULL, "
        "status TEXT NOT NULL, "
        "progress REAL, "
        "message TEXT NOT NULL, "
        "cancel_requested INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL, "
        "finished_at TEXT, "
        "result_ref_json TEXT, "
        "failure_json TEXT, "
        "answer TEXT)"
    )
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_document_created "
        "ON tasks(document_id, created_at DESC)"
    )
    await connection.execute("PRAGMA user_version = 5")

