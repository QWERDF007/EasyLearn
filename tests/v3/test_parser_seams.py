import asyncio
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.files import DocumentFiles
from easylearn.parsing.preflight import ParsePreflight
from easylearn.persistence import DocumentStore


class DummyContext:
    async def check(self) -> None:
        pass

    async def progress(self, value: float | None, message: str) -> None:
        pass


@pytest.mark.asyncio
async def test_parse_preflight_converts_image_to_pdf(tmp_path):
    preflight = ParsePreflight(Settings())
    source_img = tmp_path / "test.png"
    with Image.new("RGB", (100, 100), "blue") as img:
        img.save(source_img, format="PNG")

    dest_pdf = tmp_path / "preview.pdf"
    res = await preflight.prepare_input(source_img, dest_pdf, DummyContext())

    assert dest_pdf.is_file()
    assert dest_pdf.read_bytes().startswith(b"%PDF-")
    assert res.pages == 1
    assert len(res.preview_sha256) == 64


@pytest.mark.asyncio
async def test_document_store_prune_unreferenced_parses(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.open()
    try:
        store = DocumentStore(db)
        doc_id = uuid4()
        now = datetime.now(UTC).isoformat()
        await store.create(doc_id, "doc.pdf", "doc.pdf", now)

        parse_ids = [uuid4() for _ in range(7)]
        for i, pid in enumerate(parse_ids):
            # Create timestamp so order is deterministic
            ts = f"2026-01-0{i+1}T00:00:00Z"
            await store.record_parse(
                document_id=doc_id,
                parse_id=pid,
                preview_path="p",
                ir_path="i",
                raw_path=None,
                pages=1,
                metadata={},
                timestamp=ts,
            )

        # Keep latest 3, protect parse_ids[1] (which is older)
        # Parse ids in DESC order: parse_ids[6], [5], [4], [3], [2], [1], [0]
        # Keep 3 means [6], [5], [4] are kept.
        # [3], [2], [1], [0] are candidates.
        # If [1] is protected, then [3], [2], [0] should be pruned.
        pruned = await store.prune_unreferenced_parses(
            document_id=doc_id,
            keep_count=3,
            protected_ids={parse_ids[1]},
        )
        assert set(pruned) == {parse_ids[3], parse_ids[2], parse_ids[0]}
        assert await store.get_parse(doc_id, parse_ids[1]) is not None
        assert await store.get_parse(doc_id, parse_ids[4]) is not None
    finally:
        await db.close()
