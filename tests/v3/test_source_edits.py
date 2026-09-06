import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, Settings
from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode
from easylearn.main import create_app


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
def pdf_bytes() -> bytes:
    return Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()


async def _create_parse(client: AsyncClient, pdf_bytes: bytes) -> tuple[str, str, Path]:
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document_id"]
    services = client._transport.app.state.services
    parse_id = uuid4()
    parse_directory = services.files.paths.parse(document_id, parse_id)
    parse_directory.mkdir(parents=True)
    original = await services.documents.file_path(document_id, "original")
    shutil.copyfile(original, parse_directory / "preview.pdf")
    ir = DocumentIR(
        document_id=document_id,
        parse_run_id=parse_id,
        preview_asset_id=uuid4(),
        preview_sha256="a" * 64,
        mineru_version="test",
        adapter_version="test",
        pages=(
            PageGeometry(
                page_index=0,
                media_box=(0, 0, 100, 100),
                crop_box=(0, 0, 100, 100),
            ),
        ),
        blocks=(
            Block(
                block_id="block-1",
                block_type="paragraph",
                order_index=0,
                source_nodes=(TextNode(node_id="node-1", text="Original text"),),
            ),
        ),
    )
    (parse_directory / "document.json").write_text(
        ir.model_dump_json(exclude_computed_fields=True), encoding="utf-8"
    )
    async with services.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO parse_results "
            "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 1, ?, '2026-01-01T00:00:00+00:00')",
            (
                str(parse_id),
                document_id,
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                json.dumps({}),
            ),
        )
        await connection.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(parse_id), document_id),
        )
    return document_id, str(parse_id), parse_directory / "document.json"


@pytest.mark.asyncio
async def test_source_edit_is_versioned_and_projected_without_mutating_ir(client, pdf_bytes):
    document_id, parse_id, ir_path = await _create_parse(client, pdf_bytes)

    listed = await client.get(
        f"/api/documents/{document_id}/source-edits?parse_id={parse_id}"
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    edited = await client.patch(
        f"/api/documents/{document_id}/source-edits",
        json={
            "parse_id": parse_id,
            "block_id": "block-1",
            "node_id": "node-1",
            "expected_revision": 0,
            "text": "Edited text",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 1
    assert edited.json()["effective_text"] == "Edited text"

    parse = await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
    assert parse.json()["blocks"][0]["source_nodes"][0]["text"] == "Edited text"
    assert (
        json.loads(ir_path.read_text(encoding="utf-8"))["blocks"][0]["source_nodes"][0]["text"]
        == "Original text"
    )

    conflict = await client.patch(
        f"/api/documents/{document_id}/source-edits",
        json={
            "parse_id": parse_id,
            "block_id": "block-1",
            "node_id": "node-1",
            "expected_revision": 0,
            "text": "Stale text",
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "SOURCE_EDIT_CONFLICT"
