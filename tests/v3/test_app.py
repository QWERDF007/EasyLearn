import asyncio
import json
import shutil
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image

from easylearn.config import AppSettings, Settings
from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode
from easylearn.main import create_app


@pytest_asyncio.fixture
async def client(tmp_path):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
def pdf_bytes():
    return Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()


@pytest.mark.asyncio
async def test_document_upload_is_stored_in_a_generated_directory(client, pdf_bytes, tmp_path):
    response = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 201, response.text
    document = response.json()
    document_id = document["document_id"]
    assert document["name"] == "paper.pdf"
    assert (tmp_path / "documents" / document_id / "original" / "source.pdf").is_file()
    assert (await client.get(f"/api/documents/{document_id}/files/original")).content == pdf_bytes

    invalid_file = await client.get(f"/api/documents/{document_id}/files/preview:not-a-uuid")
    assert invalid_file.status_code == 404
    assert invalid_file.json()["code"] == "FILE_NOT_FOUND"

    assert (await client.get("/api/documents")).json()[0]["document_id"] == document_id

    marked = await client.patch(f"/api/documents/{document_id}/favorite", json={"favorite": True})
    assert marked.json()["favorite"] is True
    assert (await client.get("/api/documents?favorite=true")).json()[0]["favorite"] is True

    assert (await client.delete(f"/api/documents/{document_id}")).status_code == 204
    assert not (tmp_path / "documents" / document_id).exists()
    assert (await client.get(f"/api/documents/{document_id}")).status_code == 404


@pytest.mark.asyncio
async def test_multiple_documents_with_the_same_extension_are_listed(client, pdf_bytes):
    first = await client.post(
        "/api/documents", files={"file": ("first.pdf", pdf_bytes, "application/pdf")}
    )
    second = await client.post(
        "/api/documents", files={"file": ("second.pdf", pdf_bytes, "application/pdf")}
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert {item["name"] for item in (await client.get("/api/documents")).json()} == {
        "first.pdf",
        "second.pdf",
    }


@pytest.mark.asyncio
async def test_document_deletion_blocks_new_task_admission(client, pdf_bytes, monkeypatch):
    application = client._transport.app
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    document_id = created.json()["document_id"]
    started = asyncio.Event()
    release = asyncio.Event()
    original_delete = application.state.services.documents.delete

    async def delayed_delete(document):
        started.set()
        await release.wait()
        await original_delete(document)

    monkeypatch.setattr(application.state.services.documents, "delete", delayed_delete)
    deleting = asyncio.create_task(client.delete(f"/api/documents/{document_id}"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        rejected = await client.post(f"/api/documents/{document_id}/parse", json={})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "DOCUMENT_BUSY"
    finally:
        release.set()
        response = await deleting
        assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_upload_rejects_content_that_does_not_match_the_extension(client):
    response = await client.post(
        "/api/documents", files={"file": ("paper.pdf", b"not a pdf", "application/pdf")}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "UPLOAD_FORMAT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename, image_format, media_type", [
    ("image.gif", "GIF", "image/gif"),
    ("image.jp2", "JPEG2000", "image/jp2"),
])
async def test_mineru_supported_image_inputs_can_be_uploaded(
    client, filename, image_format, media_type
):
    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as image:
        image.save(buffer, format=image_format)
        payload = buffer.getvalue()
    response = await client.post(
        "/api/documents", files={"file": (filename, payload, media_type)}
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_health_exposes_a_new_boot_id(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["server_boot_id"]


@pytest.mark.asyncio
async def test_parse_result_identity_must_match_its_database_reference(client, pdf_bytes, tmp_path):
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert created.status_code == 201, created.text
    document_id = UUID(created.json()["document_id"])
    parse_id = uuid4()
    foreign_parse_id = uuid4()
    services = client._transport.app.state.services
    parse_directory = services.files.paths.parse(document_id, parse_id)
    parse_directory.mkdir(parents=True)
    shutil.copyfile(
        await services.documents.file_path(document_id, "original"),
        parse_directory / "preview.pdf",
    )
    ir = DocumentIR(
        document_id=document_id,
        parse_run_id=foreign_parse_id,
        preview_asset_id=uuid4(),
        preview_sha256="0" * 64,
        mineru_version="3.4.5",
        adapter_version="3.0.0",
        pages=(
            PageGeometry(
                page_index=0,
                media_box=(0, 0, 600, 800),
                crop_box=(0, 0, 600, 800),
            ),
        ),
        blocks=(),
    )
    (parse_directory / "document.json").write_bytes(
        ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
    )
    async with services.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO parse_results "
            "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 1, '{}', ?)",
            (
                str(parse_id),
                str(document_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    response = await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "PARSE_RESULT_INVALID"

    response = await client.get(f"/api/documents/{document_id}/files/ir:{parse_id}")
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "PARSE_RESULT_INVALID"


@pytest.mark.asyncio
async def test_corrupt_parse_ir_uses_the_parse_result_error_contract(client, pdf_bytes):
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert created.status_code == 201, created.text
    document_id = UUID(created.json()["document_id"])
    parse_id = uuid4()
    services = client._transport.app.state.services
    parse_directory = services.files.paths.parse(document_id, parse_id)
    parse_directory.mkdir(parents=True)
    (parse_directory / "document.json").write_bytes(b"{not valid json")
    async with services.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO parse_results "
            "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 1, '{}', ?)",
            (
                str(parse_id),
                str(document_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    for path in (
        f"/api/documents/{document_id}/parses/{parse_id}",
        f"/api/documents/{document_id}/files/ir:{parse_id}",
    ):
        response = await client.get(path)
        assert response.status_code == 500, response.text
        assert response.json()["code"] == "PARSE_RESULT_INVALID"


@pytest.mark.asyncio
async def test_export_file_requires_a_canonical_relative_path(client, pdf_bytes):
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert created.status_code == 201, created.text
    document_id = UUID(created.json()["document_id"])
    export_id = uuid4()
    services = client._transport.app.state.services
    export_directory = services.files.paths.export(document_id, export_id)
    export_directory.mkdir(parents=True)
    (export_directory / "manifest.json").write_text("{}", encoding="utf-8")

    valid = await client.get(
        f"/api/documents/{document_id}/files/export:{export_id}:manifest.json"
    )
    assert valid.status_code == 200
    for relative in ("./manifest.json", "nested/../manifest.json"):
        response = await client.get(
            f"/api/documents/{document_id}/files/export:{export_id}:{relative}"
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "FILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_page_exposes_full_result_and_question_history_controls(client):
    response = await client.get("/")
    assert response.status_code == 200
    for element_id in (
        "result-search-input",
        "qa-history-select",
        "empty-upload-state",
        "empty-upload-dropzone",
        "settings-button",
        "settings-panel",
        "model-select",
        "reparse-button",
        "download-button",
        "parse-progress",
        "parse-progress-cancel",
        "pdf-toolbar",
        "prev-page",
        "next-page",
        "zoom-out",
        "zoom-value",
        "zoom-in",
        "reset-zoom",
    ):
        assert f'id="{element_id}"' in response.text
    assert "点击上传或者拖入文件开始解析" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["app.js", "pdfjs/pdf.min.mjs"])
async def test_browser_module_is_served_with_a_javascript_mime_type(client, asset):
    response = await client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] in {
        "text/javascript",
        "application/javascript",
    }


@pytest.mark.asyncio
async def test_second_instance_with_the_same_data_directory_is_rejected(tmp_path):
    settings = Settings(app=AppSettings(data_dir=tmp_path))
    first = create_app(settings)
    second = create_app(settings)
    async with first.router.lifespan_context(first):
        with pytest.raises(RuntimeError, match="already uses this data directory"):
            async with second.router.lifespan_context(second):
                pass


@pytest.mark.asyncio
async def test_documents_survive_a_process_restart(tmp_path, pdf_bytes):
    settings = Settings(app=AppSettings(data_dir=tmp_path))
    first = create_app(settings)
    async with (
        first.router.lifespan_context(first),
        AsyncClient(transport=ASGITransport(app=first), base_url="http://test") as client,
    ):
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
        )
        assert created.status_code == 201, created.text
        document_id = created.json()["document_id"]
        marked = await client.patch(
            f"/api/documents/{document_id}/favorite", json={"favorite": True}
        )
        assert marked.status_code == 200

    second = create_app(settings)
    async with (
        second.router.lifespan_context(second),
        AsyncClient(transport=ASGITransport(app=second), base_url="http://test") as client,
    ):
        listed = await client.get("/api/documents")
        assert listed.status_code == 200
        assert listed.json()[0]["document_id"] == document_id
        assert listed.json()[0]["favorite"] is True
        original = await client.get(f"/api/documents/{document_id}/files/original")
        assert original.content == pdf_bytes


@pytest.mark.asyncio
async def test_completed_parse_translation_and_qa_survive_a_process_restart(tmp_path, pdf_bytes):
    settings = Settings(app=AppSettings(data_dir=tmp_path))
    first = create_app(settings)
    parse_id = uuid4()
    qa_id = uuid4()
    async with (
        first.router.lifespan_context(first),
        AsyncClient(transport=ASGITransport(app=first), base_url="http://test") as client,
    ):
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
        )
        assert created.status_code == 201, created.text
        document_id = UUID(created.json()["document_id"])
        parse_directory = first.state.services.files.paths.parse(document_id, parse_id)
        parse_directory.mkdir(parents=True)
        original = await first.state.services.documents.file_path(document_id, "original")
        shutil.copyfile(original, parse_directory / "preview.pdf")
        ir = DocumentIR(
            document_id=document_id,
            parse_run_id=parse_id,
            preview_asset_id=uuid4(),
            preview_sha256="0" * 64,
            mineru_version="3.4.5",
            adapter_version="3.0.0",
            pages=(
                PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
            ),
            blocks=(
                Block(
                    block_id="b1",
                    block_type="paragraph",
                    order_index=0,
                    source_nodes=(TextNode(node_id="n1", text="Persistent source."),),
                ),
            ),
        )
        (parse_directory / "document.json").write_bytes(
            ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
        )
        citation = {
            "citation": 1,
            "block_id": "b1",
            "block_ref": {
                "document_id": str(document_id),
                "parse_run_id": str(parse_id),
                "block_id": "b1",
            },
            "text": "Persistent source.",
            "page_indices": [0],
            "localization_level": "block",
        }
        async with first.state.services.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO parse_results "
                "(id, document_id, preview_path, ir_path, raw_path, pages, "
                "metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, NULL, 1, '{}', '2026-01-01T00:00:00+00:00')",
                (
                    str(parse_id),
                    str(document_id),
                    f"parses/{parse_id}/preview.pdf",
                    f"parses/{parse_id}/document.json",
                ),
            )
            await connection.execute(
                "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                (str(parse_id), str(document_id)),
            )
            await connection.execute(
                "INSERT INTO translations "
                "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                "revision, updated_at) "
                "VALUES (?, 'b1', 'b1:n1', 'Automatic', '人工结果', 1, 1, 2, ?)",
                (str(parse_id), "2026-01-01T00:00:00+00:00"),
            )
            await connection.execute(
                "INSERT INTO qa_records "
                "(id, document_id, parse_id, question, answer, context_json, citations_json, "
                "created_at) "
                "VALUES (?, ?, ?, 'What persists?', 'It persists [1].', ?, ?, ?)",
                (
                    str(qa_id),
                    str(document_id),
                    str(parse_id),
                    json.dumps(
                        [
                            {
                                **citation,
                                "required": True,
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps([citation], ensure_ascii=False),
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    second = create_app(settings)
    async with (
        second.router.lifespan_context(second),
        AsyncClient(transport=ASGITransport(app=second), base_url="http://test") as client,
    ):
        document = (await client.get(f"/api/documents/{document_id}")).json()
        assert document["active_parse_id"] == str(parse_id)
        assert document["parse_results"][0]["parse_id"] == str(parse_id)
        assert (
            await client.get(f"/api/documents/{document_id}/parses/{parse_id}")
        ).status_code == 200
        translations = (
            await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
        ).json()
        assert translations[0]["effective_text"] == "人工结果"
        assert translations[0]["locked"] is True
        markdown = await client.get(
            f"/api/documents/{document_id}/parses/{parse_id}/markdown?language=zh"
        )
        assert "人工结果" in markdown.text
        records = (await client.get(f"/api/documents/{document_id}/qa")).json()
        assert records[0]["qa_id"] == str(qa_id)
        assert records[0]["citations"][0]["block_ref"]["parse_run_id"] == str(parse_id)


@pytest.mark.asyncio
async def test_katex_assets_and_layout_served(client):
    index_res = await client.get("/")
    assert index_res.status_code == 200
    assert "/static/katex/katex.min.css" in index_res.text

    css_res = await client.get("/static/katex/katex.min.css")
    assert css_res.status_code == 200

    mjs_res = await client.get("/static/katex/katex.mjs")
    assert mjs_res.status_code == 200

    app_css = await client.get("/static/app.css")
    assert app_css.status_code == 200
    assert "result-formula-container" in app_css.text
    assert 'data-block-type="formula"' in app_css.text


@pytest.mark.asyncio
async def test_document_and_parse_result_has_translation_status(client, pdf_bytes):
    created = await client.post(
        "/api/documents", files={"file": ("paper.pdf", pdf_bytes, "application/pdf")}
    )
    assert created.status_code == 201
    document_id = UUID(created.json()["document_id"])
    parse_id = uuid4()
    services = client._transport.app.state.services
    parse_directory = services.files.paths.parse(document_id, parse_id)
    parse_directory.mkdir(parents=True)
    shutil.copyfile(
        await services.documents.file_path(document_id, "original"),
        parse_directory / "preview.pdf",
    )
    ir = DocumentIR(
        document_id=document_id,
        parse_run_id=parse_id,
        preview_asset_id=uuid4(),
        preview_sha256="0" * 64,
        mineru_version="3.4.5",
        adapter_version="3.0.0",
        pages=(
            PageGeometry(
                page_index=0,
                media_box=(0, 0, 600, 800),
                crop_box=(0, 0, 600, 800),
            ),
        ),
        blocks=(),
    )
    (parse_directory / "document.json").write_bytes(
        ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
    )
    async with services.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO parse_results "
            "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 1, '{}', ?)",
            (
                str(parse_id),
                str(document_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await connection.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(parse_id), str(document_id)),
        )

    # Initially has_translation is False
    doc_res = await client.get(f"/api/documents/{document_id}")
    assert doc_res.status_code == 200
    doc_data = doc_res.json()
    assert doc_data["has_translation"] is False
    assert doc_data["parse_results"][0]["has_translation"] is False

    # Insert a translation row
    async with services.database.transaction() as connection:
        await connection.execute(
            "INSERT INTO translations (parse_id, block_id, unit_id, auto_text, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(parse_id), "b1", "u1", "译文", "2026-01-01T00:00:00+00:00"),
        )

    # Now has_translation is True
    doc_res = await client.get(f"/api/documents/{document_id}")
    assert doc_res.status_code == 200
    doc_data = doc_res.json()
    assert doc_data["has_translation"] is True
    assert doc_data["parse_results"][0]["has_translation"] is True

    # Also check list documents
    list_res = await client.get("/api/documents")
    assert list_res.status_code == 200
    list_data = list_res.json()
    doc_item = next(d for d in list_data if d["document_id"] == str(document_id))
    assert doc_item["has_translation"] is True
    assert doc_item["parse_results"][0]["has_translation"] is True

