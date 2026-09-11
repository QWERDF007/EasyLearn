import asyncio
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, ExtensionSettings, Settings
from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode
from easylearn.main import create_app


class FakeLLM:
    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {unit_id: f"译：{text}" for unit_id, text in payload.items()}, ensure_ascii=False
        )

    async def stream(self, messages: list[dict[str, str]]):
        del messages
        for chunk in ("回答内容", " [^1]"):
            await asyncio.sleep(0)
            yield chunk


async def wait_for_task(client: AsyncClient, task_id: str) -> dict:
    for _ in range(50):
        res = await client.get(f"/api/tasks/{task_id}")
        if res.json()["status"] in ("succeeded", "failed", "cancelled"):
            return res.json()
        await asyncio.sleep(0.05)
    raise TimeoutError("Task did not complete")


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    app = create_app(
        Settings(
            app=AppSettings(data_dir=tmp_path),
            extensions=ExtensionSettings(qa_enabled=True),
        )
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.services.translation.llm = FakeLLM()
        app.state.services.qa.llm = FakeLLM()
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


@pytest.mark.asyncio
async def test_source_edit_propagates_to_reader_translation_qa_and_export(client, pdf_bytes):
    document_id, parse_id, ir_path = await _create_parse(client, pdf_bytes)

    # 1. Initially create a manual translation for block-1:node-1
    edit_trans = await client.patch(
        f"/api/documents/{document_id}/translations/block-1:node-1",
        json={
            "parse_id": parse_id,
            "block_id": "block-1",
            "expected_revision": 0,
            "text": "初始翻译",
        },
    )
    assert edit_trans.status_code == 200, edit_trans.text
    assert edit_trans.json()["effective_text"] == "初始翻译"
    assert edit_trans.json()["stale"] is False

    # 2. Verify reader displays the original text initially
    reader = await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
    assert reader.status_code == 200
    assert reader.json()["blocks"][0]["source_nodes"][0]["text"] == "Original text"

    # 3. Edit source text via source-edits API
    edit_src = await client.patch(
        f"/api/documents/{document_id}/source-edits",
        json={
            "parse_id": parse_id,
            "block_id": "block-1",
            "node_id": "node-1",
            "expected_revision": 0,
            "text": "Edited source text",
        },
    )
    assert edit_src.status_code == 200, edit_src.text
    assert edit_src.json()["effective_text"] == "Edited source text"

    # 4. Verify reader displays the edited text, while base IR file on disk remains immutable
    reader2 = await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
    assert reader2.json()["blocks"][0]["source_nodes"][0]["text"] == "Edited source text"
    assert (
        json.loads(ir_path.read_text(encoding="utf-8"))["blocks"][0]["source_nodes"][0]["text"]
        == "Original text"
    )

    # 5. Verify translation API marks the translation as stale and excludes it from effective text
    trans_list = await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    assert trans_list.status_code == 200
    unit = next(u for u in trans_list.json() if u["unit_id"] == "block-1:node-1")
    assert unit["stale"] is True
    # Audit trail preserved:
    assert unit["manual_text"] == "初始翻译"
    # But effective text is no longer the stale translation; falls back to current source text:
    assert unit["effective_text"] == "Edited source text"

    # 6. Verify QA uses the edited text in its evidence context
    qa_resp = await client.post(
        f"/api/documents/{document_id}/qa",
        json={
            "parse_id": parse_id,
            "question": "What is the content?",
            "block_ids": ["block-1"],
            "language": "chinese",
        },
    )
    assert qa_resp.status_code == 202, qa_resp.text
    qa_task = await wait_for_task(client, qa_resp.json()["task_id"])
    assert qa_task["status"] == "succeeded", qa_task
    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert len(records) >= 1
    # Evidence citation text reflects the edited source text, NOT the stale translation or original base IR text:
    assert records[0]["citations"][0]["text"] == "Edited source text"

    # 7. Verify Export uses the effective snapshot with edited source text
    export_resp = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": parse_id, "format": "source_markdown"},
    )
    assert export_resp.status_code == 202, export_resp.text
    export_task = await wait_for_task(client, export_resp.json()["task_id"])
    assert export_task["status"] == "succeeded", export_task
    file_id = export_task["result_ref"]["file_id"]
    download = await client.get(f"/api/documents/{document_id}/files/{file_id}")
    assert download.status_code == 200
    assert "Edited source text" in download.text
    assert "Original text" not in download.text

    # 8. Re-translating updates the translation fingerprint and clears the stale state
    edit_trans2 = await client.patch(
        f"/api/documents/{document_id}/translations/block-1:node-1",
        json={
            "parse_id": parse_id,
            "block_id": "block-1",
            "expected_revision": unit["revision"],
            "text": "修改后的翻译",
        },
    )
    assert edit_trans2.status_code == 200, edit_trans2.text
    assert edit_trans2.json()["stale"] is False
    assert edit_trans2.json()["effective_text"] == "修改后的翻译"
