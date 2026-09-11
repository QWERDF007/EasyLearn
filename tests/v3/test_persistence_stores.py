import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobKind, JobStatus
from easylearn.persistence import (
    DocumentStore,
    QAStore,
    SourceEditStore,
    TaskStore,
    TranslationStore,
)
from easylearn.qa import QACitation, QARecordView
from easylearn.tasks import TaskRecord


@pytest.mark.asyncio
async def test_document_store_crud_and_parse_recording(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        store = DocumentStore(db)
        doc_id = uuid4()
        now = datetime.now(UTC).isoformat()
        await store.create(doc_id, "test.pdf", "original/test.pdf", now)

        doc_row = await store.get(doc_id)
        assert doc_row is not None
        assert doc_row["name"] == "test.pdf"
        assert doc_row["favorite"] == 0

        await store.set_favorite(doc_id, True)
        doc_row = await store.get(doc_id)
        assert doc_row["favorite"] == 1

        parse_id = uuid4()
        await store.record_parse(
            document_id=doc_id,
            parse_id=parse_id,
            preview_path="parses/p/preview.pdf",
            ir_path="parses/p/document.json",
            raw_path="parses/p/mineru.zip",
            pages=3,
            metadata={"total_units": 10},
            timestamp=now,
            set_active=True,
        )

        parse_row = await store.get_parse(doc_id, parse_id)
        assert parse_row is not None
        assert parse_row["pages"] == 3
        doc_row = await store.get(doc_id)
        assert doc_row["active_parse_id"] == str(parse_id)

        # Deleting non-existent document raises 404
        with pytest.raises(DomainError) as exc:
            await store.delete(uuid4())
        assert exc.value.status == 404

        # Recording for non-existent document raises 404
        with pytest.raises(DomainError) as exc:
            await store.record_parse(
                document_id=uuid4(),
                parse_id=uuid4(),
                preview_path="p",
                ir_path="i",
                raw_path=None,
                pages=1,
                metadata={},
                timestamp=now,
            )
        assert exc.value.status == 404

        await store.delete(doc_id)
        assert await store.get(doc_id) is None
        assert await store.get_parse(doc_id, parse_id) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_source_edit_store_optimistic_concurrency(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        doc_store = DocumentStore(db)
        edit_store = SourceEditStore(db)
        doc_id = uuid4()
        parse_id = uuid4()
        now = datetime.now(UTC).isoformat()
        await doc_store.create(doc_id, "test.pdf", "test.pdf", now)
        await doc_store.record_parse(
            document_id=doc_id,
            parse_id=parse_id,
            preview_path="p",
            ir_path="i",
            raw_path=None,
            pages=1,
            metadata={},
            timestamp=now,
        )

        # Save initial edit at expected_revision = 0
        rev1, ts1 = await edit_store.save_edit(
            parse_id=parse_id,
            block_id="b1",
            node_id="n1",
            text="First edit",
            expected_revision=0,
            updated_at=now,
        )
        assert rev1 == 1

        # Trying to save with stale revision raises 409
        with pytest.raises(DomainError) as exc:
            await edit_store.save_edit(
                parse_id=parse_id,
                block_id="b1",
                node_id="n1",
                text="Stale edit",
                expected_revision=0,
                updated_at=now,
            )
        assert exc.value.status == 409

        # Saving with expected_revision = 1 succeeds
        rev2, ts2 = await edit_store.save_edit(
            parse_id=parse_id,
            block_id="b1",
            node_id="n1",
            text="Second edit",
            expected_revision=1,
            updated_at=now,
        )
        assert rev2 == 2

        edits = await edit_store.list_edits(parse_id)
        assert len(edits) == 1
        assert edits[0]["text"] == "Second edit"
        assert edits[0]["revision"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_translation_store_locking_and_history_audit(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        doc_store = DocumentStore(db)
        trans_store = TranslationStore(db)
        doc_id = uuid4()
        parse_id = uuid4()
        now = datetime.now(UTC).isoformat()
        await doc_store.create(doc_id, "test.pdf", "test.pdf", now)
        await doc_store.record_parse(
            document_id=doc_id,
            parse_id=parse_id,
            preview_path="p",
            ir_path="i",
            raw_path=None,
            pages=1,
            metadata={},
            timestamp=now,
        )

        # Batch save auto translations
        conflicts = await trans_store.save_batch(
            parse_id=parse_id,
            batch=[
                {"block_id": "b1", "unit_id": "u1", "value": "自动翻译1", "fingerprint": "fp1"},
                {"block_id": "b1", "unit_id": "u2", "value": "自动翻译2", "fingerprint": "fp2"},
            ],
            snapshots={},
            timestamp=now,
        )
        assert conflicts == 0

        rows = await trans_store.get_translations(parse_id)
        assert len(rows) == 2

        # Manual edit with expected_revision = 0
        rev, ts, auto_txt, man_txt, use_man, lck = await trans_store.edit(
            parse_id=parse_id,
            block_id="b1",
            unit_id="u1",
            text="手动翻译1",
            use_manual=True,
            locked=True,
            expected_revision=0,
            unit_fingerprint="fp1",
            timestamp=now,
        )
        assert rev == 1
        assert man_txt == "手动翻译1"
        assert use_man is True
        assert lck is True

        # Locked edit without unlocking raises 409
        with pytest.raises(DomainError) as exc:
            await trans_store.edit(
                parse_id=parse_id,
                block_id="b1",
                unit_id="u1",
                text="试图修改加锁翻译",
                use_manual=True,
                locked=True,
                expected_revision=1,
                unit_fingerprint="fp1",
                timestamp=now,
            )
        assert exc.value.status == 409
        assert exc.value.code == "TRANSLATION_LOCKED"

        # Check history has records
        history = await trans_store.get_history(parse_id, "u1", "b1")
        assert len(history) >= 2  # initial auto + manual edit
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_task_store_reconciles_orphans_and_persists(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        doc_store = DocumentStore(db)
        task_store = TaskStore(db)
        doc_id = uuid4()
        now = datetime.now(UTC)
        await doc_store.create(doc_id, "test.pdf", "test.pdf", now.isoformat())

        task_id = uuid4()
        boot_id = uuid4()
        record = TaskRecord(
            task_id=task_id,
            server_boot_id=boot_id,
            document_id=doc_id,
            kind=JobKind.PARSE,
            scope={"test": True},
            created_at=now,
            status=JobStatus.RUNNING,
            progress=0.5,
            message="In progress",
        )
        await task_store.upsert(record)

        loaded = await task_store.load(task_id)
        assert loaded is not None
        assert loaded.task_id == task_id
        assert loaded.status == JobStatus.RUNNING
        assert loaded.progress == 0.5

        # Reconcile orphans simulates restart recovery
        await task_store.reconcile_orphans(
            interrupted_failure_json=json.dumps(
                {"code": "TASK_INTERRUPTED", "message": "Interrupted by restart", "retryable": True}
            ),
            timestamp=datetime.now(UTC).isoformat(),
        )

        recovered = await task_store.load(task_id)
        assert recovered.status == JobStatus.FAILED
        assert recovered.failure is not None
        assert recovered.failure.code == "TASK_INTERRUPTED"
        assert recovered.failure.retryable is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_qa_store_crud(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        doc_store = DocumentStore(db)
        qa_store = QAStore(db)
        doc_id = uuid4()
        parse_id = uuid4()
        now = datetime.now(UTC)
        await doc_store.create(doc_id, "test.pdf", "test.pdf", now.isoformat())
        await doc_store.record_parse(
            document_id=doc_id,
            parse_id=parse_id,
            preview_path="p",
            ir_path="i",
            raw_path=None,
            pages=1,
            metadata={},
            timestamp=now.isoformat(),
        )

        from easylearn.document_ir.schema import BlockRef

        record_id = uuid4()
        view = QARecordView(
            qa_id=record_id,
            document_id=doc_id,
            parse_id=parse_id,
            question="What is this?",
            answer="A test document",
            context=({"citation": 1, "block_id": "b1", "text": "snippet"},),
            citations=(
                QACitation(
                    citation=1,
                    block_id="b1",
                    block_ref=BlockRef(document_id=doc_id, parse_run_id=parse_id, block_id="b1"),
                    text="snippet",
                    localization_level="block",
                ),
            ),
            created_at=now,
        )
        await qa_store.save(view)

        records = await qa_store.list(doc_id)
        assert len(records) == 1
        assert records[0].qa_id == record_id
        assert records[0].question == "What is this?"
        assert len(records[0].citations) == 1

        # Delete existing record
        await qa_store.delete(doc_id, record_id)
        assert len(await qa_store.list(doc_id)) == 0

        # Deleting non-existent record raises 404
        with pytest.raises(DomainError) as exc:
            await qa_store.delete(doc_id, record_id)
        assert exc.value.status == 404
    finally:
        await db.close()
