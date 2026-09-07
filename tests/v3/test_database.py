import sqlite3

import pytest

from easylearn.database import Database


@pytest.mark.asyncio
async def test_database_initializes_the_v3_schema_without_external_services(tmp_path):
    database = Database(tmp_path / "app.db")
    await database.open()
    try:
        async with database.read() as connection:
            version = await (await connection.execute("PRAGMA user_version")).fetchone()
            tables = await (
                await connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ).fetchall()
        assert version[0] == 3
        assert {row[0] for row in tables} == {
            "documents",
            "parse_results",
            "translations",
            "translation_history",
            "source_edits",
            "qa_records",
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_serializes_a_short_transaction(tmp_path):
    database = Database(tmp_path / "app.db")
    await database.open()
    try:
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, created_at) "
                "VALUES ('doc', 'paper.pdf', 'original/source.pdf', '2026-01-01T00:00:00+00:00')"
            )
        async with database.read() as connection:
            row = await (
                await connection.execute("SELECT name FROM documents WHERE id = 'doc'")
            ).fetchone()
        assert row[0] == "paper.pdf"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_migrates_legacy_document_paths_to_allow_multiple_documents(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                original_path TEXT NOT NULL UNIQUE,
                active_parse_id TEXT,
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )

    database = Database(path)
    await database.open()
    try:
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, created_at) "
                "VALUES ('first', 'first.pdf', 'original/source.pdf', '2026-01-01T00:00:00+00:00')"
            )
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, created_at) "
                "VALUES ('second', 'second.pdf', 'original/source.pdf', "
                "'2026-01-01T00:00:01+00:00')"
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_migration_preserves_children_and_foreign_key_cascade(tmp_path):
    path = tmp_path / "legacy-with-children.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                original_path TEXT NOT NULL UNIQUE,
                active_parse_id TEXT,
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE parse_results (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                preview_path TEXT NOT NULL,
                ir_path TEXT NOT NULL,
                pages INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO documents(id, name, original_path, created_at)
            VALUES ('doc', 'paper.pdf', 'original/source.pdf', '2026-01-01T00:00:00+00:00');
            INSERT INTO parse_results(
                id, document_id, preview_path, ir_path, pages, metadata_json, created_at
            ) VALUES (
                'parse', 'doc', 'preview.pdf', 'document.json', 1, '{}',
                '2026-01-01T00:00:01+00:00'
            );
            PRAGMA user_version = 1;
            """
        )

    database = Database(path)
    await database.open()
    try:
        async with database.read() as connection:
            child = await (
                await connection.execute(
                    "SELECT document_id FROM parse_results WHERE id = 'parse'"
                )
            ).fetchone()
            foreign_key_errors = await (
                await connection.execute("PRAGMA foreign_key_check")
            ).fetchall()
        assert child[0] == "doc"
        assert foreign_key_errors == []

        async with database.transaction() as connection:
            await connection.execute("DELETE FROM documents WHERE id = 'doc'")

        async with database.read() as connection:
            remaining_child = await (
                await connection.execute(
                    "SELECT id FROM parse_results WHERE id = 'parse'"
                )
            ).fetchone()
        assert remaining_child is None
    finally:
        await database.close()
