import asyncio
import io
import json
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from easylearn.config import AppSettings, FileSettings, MinerUSettings, Settings
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobKind
from easylearn.main import create_app
from easylearn.mineru.schema import MinerUOptions
from easylearn.parser import OfficeSelection, ParseService, _prepare_xlsx_selection
from easylearn.tasks import TaskContext, TaskManager


async def wait_for_task(client, task_id):
    async with asyncio.timeout(10):
        while True:
            task = (await client.get(f"/api/tasks/{task_id}")).json()
            if task["status"] in {"succeeded", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, image_format", [(".gif", "GIF"), (".jp2", "JPEG2000")])
async def test_mineru_supported_image_inputs_are_converted_to_pdf(tmp_path, suffix, image_format):
    source = tmp_path / f"source{suffix}"
    with Image.new("RGB", (40, 20), "white") as image:
        image.save(source, format=image_format)
    destination = tmp_path / "preview.pdf"

    class Context:
        async def check(self):
            return None

    parser = ParseService(None, None, None, None, Settings())
    await parser._prepare_pdf(source, destination, Context())
    assert destination.read_bytes().startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_parse_publishes_an_immutable_result_directory(tmp_path, monkeypatch):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", source, "application/pdf")}
        )
        document_id = created.json()["document_id"]

        async def fake_mineru(input_pdf, task_directory, cas, options: MinerUOptions, context):
            contents = {
                "input/vlm/input.md": b"# Synthetic",
                "input/vlm/input_middle.json": json.dumps(
                    {
                        "_version_name": "3.4.5",
                        "_backend": "vlm",
                        "pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": []}],
                    }
                ).encode(),
                "input/vlm/input_model.json": b"[]",
                "input/vlm/input_content_list.json": b"[]",
                "input/vlm/input_content_list_v2.json": b"[]",
                "input/vlm/input_origin.pdf": input_pdf.read_bytes(),
            }
            buffer = io.BytesIO()
            with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            return await asyncio.to_thread(cas.write, [buffer.getvalue()])

        monkeypatch.setattr(app.state.services.parser, "_run_mineru", fake_mineru)
        accepted = await client.post(f"/api/documents/{document_id}/parse", json={})
        assert accepted.status_code == 202, accepted.text
        task = await wait_for_task(client, accepted.json()["task_id"])
        assert task["status"] == "succeeded", task
        parse_id = task["result_ref"]["parse_id"]
        document = (await client.get(f"/api/documents/{document_id}")).json()
        assert document["active_parse_id"] == parse_id
        assert document["parse_results"][0]["pages"] == 1
        ir = await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
        assert ir.status_code == 200
        assert ir.json()["parse_run_id"] == parse_id
        assert (
            await client.get(f"/api/documents/{document_id}/files/preview:{parse_id}")
        ).content == source
        raw_markdown = await client.get(
            f"/api/documents/{document_id}/parses/{parse_id}/markdown?language=raw"
        )
        assert raw_markdown.status_code == 200, raw_markdown.text
        assert raw_markdown.text == "# Synthetic"


@pytest.mark.asyncio
async def test_parse_can_queue_translation_only_after_result_publication(tmp_path, monkeypatch):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", source, "application/pdf")}
        )
        document_id = created.json()["document_id"]

        class AutoTranslateLLM:
            async def complete_json(self, messages: list[dict[str, str]]) -> str:
                payload = json.loads(messages[-1]["content"])
                return json.dumps({key: f"译：{value}" for key, value in payload.items()})

        app.state.services.translation.llm = AutoTranslateLLM()

        async def fake_mineru(input_pdf, task_directory, cas, options: MinerUOptions, context):
            del task_directory, options, context
            middle = {
                "_version_name": "3.4.5",
                "_backend": "vlm",
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [612, 792],
                        "para_blocks": [
                            {
                                "type": "text",
                                "bbox": [10, 20, 100, 40],
                                "lines": [
                                    {
                                        "bbox": [10, 20, 100, 40],
                                        "spans": [{"type": "text", "content": "Hello"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            contents = {
                "input/vlm/input.md": b"Hello",
                "input/vlm/input_middle.json": json.dumps(middle).encode(),
                "input/vlm/input_model.json": b"[]",
                "input/vlm/input_content_list.json": b"[]",
                "input/vlm/input_content_list_v2.json": b"[]",
                "input/vlm/input_origin.pdf": input_pdf.read_bytes(),
            }
            buffer = io.BytesIO()
            with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            return await asyncio.to_thread(cas.write, [buffer.getvalue()])

        monkeypatch.setattr(app.state.services.parser, "_run_mineru", fake_mineru)
        accepted = await client.post(
            f"/api/documents/{document_id}/parse", json={"auto_translate": True}
        )
        assert accepted.status_code == 202, accepted.text
        parse_task = await wait_for_task(client, accepted.json()["task_id"])
        assert parse_task["status"] == "succeeded", parse_task
        follow_up_id = parse_task["result_ref"]["follow_up_task_id"]
        translate_task = await wait_for_task(client, follow_up_id)
        assert translate_task["status"] == "succeeded", translate_task
        translations = await client.get(
            f"/api/documents/{document_id}/translations?parse_id={parse_task['result_ref']['parse_id']}"
        )
        assert translations.status_code == 200, translations.text
        assert translations.json()[0]["effective_text"] == "译：Hello"


@pytest.mark.asyncio
async def test_invalid_image_parse_reports_a_specific_error(tmp_path):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/api/documents", files={"file": ("broken.webp", b"not an image", "image/webp")}
        )
        assert created.status_code == 201, created.text
        document_id = created.json()["document_id"]
        accepted = await client.post(f"/api/documents/{document_id}/parse", json={})
        task = await wait_for_task(client, accepted.json()["task_id"])
        assert task["status"] == "failed", task
        assert task["failure"]["code"] == "IMAGE_INVALID"


@pytest.mark.asyncio
async def test_parse_without_a_mineru_command_fails_with_a_clear_error(tmp_path):
    app = create_app(
        Settings(
            app=AppSettings(data_dir=tmp_path),
            mineru=MinerUSettings(command=("__missing_easylearn_mineru__",)),
        )
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mineru"]["configured"] is False
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", source, "application/pdf")}
        )
        document_id = created.json()["document_id"]
        accepted = await client.post(f"/api/documents/{document_id}/parse", json={})
        assert accepted.status_code == 202, accepted.text
        task = await wait_for_task(client, accepted.json()["task_id"])
        assert task["status"] == "failed", task
        assert task["failure"]["code"] == "MINERU_UNAVAILABLE"
        assert (await client.get(f"/api/documents/{document_id}")).json()["parse_results"] == []


@pytest.mark.asyncio
async def test_parse_version_cleanup_keeps_versions_used_by_active_tasks(tmp_path):
    settings = Settings(
        app=AppSettings(data_dir=tmp_path),
        files=FileSettings(keep_parse_versions=1),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", source, "application/pdf")}
        )
        document_id = UUID(created.json()["document_id"])
        old_parse, current_parse = uuid4(), uuid4()
        for parse_id in (old_parse, current_parse):
            directory = app.state.services.files.paths.parse(document_id, parse_id)
            directory.mkdir(parents=True)
            (directory / "document.json").write_text("{}", encoding="utf-8")
        async with app.state.services.database.transaction() as connection:
            for parse_id, created_at in (
                (old_parse, "2026-01-01T00:00:00+00:00"),
                (current_parse, "2026-01-02T00:00:00+00:00"),
            ):
                await connection.execute(
                    "INSERT INTO parse_results "
                    "(id, document_id, preview_path, ir_path, raw_path, pages, "
                    "metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, NULL, 1, '{}', ?)",
                    (
                        str(parse_id),
                        str(document_id),
                        f"parses/{parse_id}/preview.pdf",
                        f"parses/{parse_id}/document.json",
                        created_at,
                    ),
                )
            await connection.execute(
                "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                (str(current_parse), str(document_id)),
            )

        manager = TaskManager(queue_limit=2)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(record, context: TaskContext):
            del record
            started.set()
            await release.wait()
            await context.check()
            return None

        for kind in JobKind:
            manager.register(kind, hold)
        await manager.start()
        try:
            active = await manager.submit(document_id, JobKind.EXPORT, {"parse_id": str(old_parse)})
            await started.wait()
            parser = ParseService(
                app.state.services.database,
                app.state.services.files,
                app.state.services.documents,
                manager,
                settings,
            )
            await parser._prune(document_id)
            assert (
                await app.state.services.documents.parse_row(document_id, old_parse)
            )["id"] == str(old_parse)
            assert app.state.services.files.paths.parse(document_id, old_parse).is_dir()
            release.set()
            async with asyncio.timeout(2):
                while True:
                    finished = await manager.get(active.task_id)
                    if finished.status.terminal:
                        break
                    await asyncio.sleep(0.01)
            await parser._prune(document_id)
            with pytest.raises(DomainError, match="Parse result not found"):
                await app.state.services.documents.parse_row(document_id, old_parse)
            assert not app.state.services.files.paths.parse(document_id, old_parse).exists()
        finally:
            release.set()
            await manager.close()


@pytest.mark.asyncio
async def test_office_parse_when_disabled_reports_a_specific_error(tmp_path):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")

    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/api/documents",
            files={
                "file": (
                    "document.docx",
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert created.status_code == 201, created.text
        document_id = created.json()["document_id"]
        accepted = await client.post(f"/api/documents/{document_id}/parse", json={})
        task = await wait_for_task(client, accepted.json()["task_id"])
        assert task["status"] == "failed", task
        assert task["failure"]["code"] == "OFFICE_CONVERTER_REQUIRED"


def test_xlsx_selection_sets_the_active_sheet_and_print_area(tmp_path):
    source = tmp_path / "book.xlsx"
    destination = tmp_path / "selected.xlsx"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<bookViews><workbookView activeTab="0"/></bookViews>'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/>'
            '<sheet name="Other" sheetId="2" r:id="rId2"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        archive.writestr("xl/worksheets/sheet2.xml", "<worksheet/>")

    _prepare_xlsx_selection(
        source, destination, OfficeSelection(sheet="Other", print_range="A1:D20")
    )
    with ZipFile(destination) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
    assert "activeTab='1'" in workbook or 'activeTab="1"' in workbook
    assert "'Other'!$A$1:$D$20" in workbook
