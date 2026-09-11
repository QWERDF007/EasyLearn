from __future__ import annotations

from uuid import uuid4

import pytest

from easylearn.database import Database
from easylearn.files import DocumentFiles
from easylearn.paths import DataPaths
from easylearn.publication import ArtifactPublisher


@pytest.mark.asyncio
async def test_publication_rolls_back_files_when_recording_fails(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    database = Database(paths.database)
    await database.open()
    try:
        document_id = uuid4()
        artifact_id = uuid4()
        document = paths.document(document_id)
        (document / "parses").mkdir(parents=True)
        staging = paths.task(uuid4()) / "published"
        staging.mkdir(parents=True)
        (staging / "document.json").write_text("{}", encoding="utf-8")
        destination = paths.parse(document_id, artifact_id)
        publisher = ArtifactPublisher(database, DocumentFiles(paths))

        async def fail_recording(_connection):
            raise RuntimeError("recording failed")

        with pytest.raises(RuntimeError, match="recording failed"):
            await publisher.publish(
                document_id=document_id,
                artifact_id=artifact_id,
                kind="parse",
                staging=staging,
                destination=destination,
                payload={"parse_id": str(artifact_id)},
                record=fail_recording,
            )

        assert not destination.exists()
        assert staging.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publication_reconciliation_records_a_published_parse(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    database = Database(paths.database)
    await database.open()
    try:
        document_id = uuid4()
        parse_id = uuid4()
        document = paths.document(document_id)
        (document / "original").mkdir(parents=True)
        (document / "parses").mkdir()
        (document / "exports").mkdir()
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, created_at) VALUES (?, ?, ?, ?)",
                (str(document_id), "paper.pdf", "original/source.pdf", "2026-01-01T00:00:00+00:00"),
            )
        destination = paths.parse(document_id, parse_id)
        destination.mkdir(parents=True)
        (destination / "preview.pdf").write_bytes(b"pdf")
        publisher = ArtifactPublisher(database, DocumentFiles(paths))
        staging = paths.task(uuid4()) / "published"
        payload = {
            "parse_id": str(parse_id),
            "preview_path": f"parses/{parse_id}/preview.pdf",
            "ir_path": f"parses/{parse_id}/document.json",
            "raw_path": None,
            "pages": 1,
            "metadata": {},
        }
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO publications "
                "(id, document_id, artifact_id, kind, staging_path, destination_path, "
                "payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(document_id),
                    str(parse_id),
                    "parse",
                    str(staging),
                    str(destination),
                    __import__("json").dumps(payload),
                    "published",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        report = await publisher.reconcile()

        assert report.recorded == 1
        async with database.read() as connection:
            row = await (
                await connection.execute(
                    "SELECT id FROM parse_results WHERE id = ?", (str(parse_id),)
                )
            ).fetchone()
            publication = await (
                await connection.execute(
                    "SELECT status FROM publications WHERE artifact_id = ?", (str(parse_id),)
                )
            ).fetchone()
        assert row is not None
        assert publication[0] == "recorded"

        # Second reconciliation must be idempotent (0 recorded, 0 recovered)
        second_report = await publisher.reconcile()
        assert second_report.recorded == 0
        assert second_report.recovered == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_export_publication_and_reconciliation(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    database = Database(paths.database)
    await database.open()
    try:
        document_id = uuid4()
        export_id = uuid4()
        document = paths.document(document_id)
        (document / "exports").mkdir(parents=True)
        async with database.transaction() as connection:
            await connection.execute(
                "INSERT INTO documents(id, name, original_path, created_at) VALUES (?, ?, ?, ?)",
                (str(document_id), "paper.pdf", "original/source.pdf", "2026-01-01T00:00:00+00:00"),
            )
        staging = paths.task(uuid4()) / "export"
        staging.mkdir(parents=True)
        (staging / "export.zip").write_bytes(b"zipcontent")
        destination = paths.export(document_id, export_id)
        publisher = ArtifactPublisher(database, DocumentFiles(paths))

        async def record_export(conn):
            pass

        pub_id = await publisher.publish(
            document_id=document_id,
            artifact_id=export_id,
            kind="export",
            staging=staging,
            destination=destination,
            payload={"export_id": str(export_id)},
            record=record_export,
        )

        assert destination.exists()
        assert (destination / "export.zip").exists()
        async with database.read() as connection:
            row = await (
                await connection.execute(
                    "SELECT status FROM publications WHERE id = ?", (str(pub_id),)
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "recorded"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_document_deletion_converges_on_reconciliation(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    database = Database(paths.database)
    await database.open()
    try:
        document_id = uuid4()
        document = paths.document(document_id)
        (document / "original").mkdir(parents=True)
        (document / "parses").mkdir()
        (document / "exports").mkdir()
        # Simulate that document was deleted from DB (so no row in documents),
        # but directory still exists on disk
        publisher = ArtifactPublisher(database, DocumentFiles(paths))
        assert document.exists()

        await publisher.reconcile()

        # Reconcile must have cleaned up the orphan document directory
        assert not document.exists()
    finally:
        await database.close()

