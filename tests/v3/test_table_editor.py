import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, Settings
from easylearn.main import create_app


@pytest_asyncio.fixture
async def client(tmp_path):
    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_table_rendering_removes_cell_level_editor_and_buttons(client):
    """appendTable must not contain addEditor or createSourceEditButton on each cell."""
    response = await client.get("/static/app.js")
    assert response.status_code == 200
    js = response.text

    assert "function appendTable(" in js
    idx_start = js.index("function appendTable(")
    idx_end = js.index("function safeHref(")
    table_func = js[idx_start:idx_end]

    # Must NOT call addEditor or createSourceEditButton per cell inside appendTable
    assert "addEditor(" not in table_func
    assert "createSourceEditButton(" not in table_func
    assert "addSourceEditor(" not in table_func


@pytest.mark.asyncio
async def test_table_block_edit_button_and_style_consistency(client):
    """Table block must be editable and correction button must not wrap vertically."""
    js_res = await client.get("/static/app.js")
    assert js_res.status_code == 200
    js = js_res.text

    # isBlockEditable or createSourceEditButton must support table blocks
    assert "renderTableEditCard" in js
    assert "tableToMarkdown" in js

    css_res = await client.get("/static/app.css")
    assert css_res.status_code == 200
    css = css_res.text

    # .result-block-action-btn and .block-edit-button in actions must have white-space: nowrap and auto width
    assert "white-space: nowrap" in css
    assert ".table-edit-card" in css
    assert ".table-edit-latex" in css or ".table-edit-latex-section" in css


@pytest.mark.asyncio
async def test_table_edit_card_features(client):
    """Table edit card must provide rich toolbar, contenteditable grid, and LaTeX preview."""
    response = await client.get("/static/app.js")
    assert response.status_code == 200
    js = response.text

    assert "function renderTableEditCard(" in js
    assert "tableToLatex(" in js or "generateTableLatex(" in js
    assert "contenteditable" in js


@pytest.mark.asyncio
async def test_table_whole_block_selection_only(client):
    """Table cells must not be individually selectable or bind block click events."""
    js_res = await client.get("/static/app.js")
    assert js_res.status_code == 200
    js = js_res.text

    idx_start = js.index("function appendTable(")
    idx_end = js.index("function safeHref(")
    table_func = js[idx_start:idx_end]

    # appendTable should not bindResultBlock on cell elements
    assert "bindResultBlock(element" not in table_func
    assert "element.dataset.blockId" not in table_func

    # CSS should not style individual cells as selected
    css_res = await client.get("/static/app.css")
    assert css_res.status_code == 200
    css = css_res.text
    assert ".result-table td.is-selected" not in css


@pytest.mark.asyncio
async def test_page_divider_and_caption_label_formatting(client):
    """Page dividers and caption/footnote/page-number labels and styles must be properly configured."""
    pdf_res = await client.get("/static/pdf-viewer.js")
    assert pdf_res.status_code == 200
    pdf_js = pdf_res.text
    assert 'caption: "图片标题"' in pdf_js or '"图片标题"' in pdf_js
    assert '"图片描述"' in pdf_js
    assert '"页码"' in pdf_js

    app_res = await client.get("/static/app.js")
    assert app_res.status_code == 200
    app_js = app_res.text
    assert "result-page-divider" in app_js
    assert "page-number-block" in app_js

    css_res = await client.get("/static/app.css")
    assert css_res.status_code == 200
    css = css_res.text
    assert ".result-page-divider" in css
    assert 'data-block-type="caption"' in css
    assert 'data-block-type="image"' in css
    assert "margin: 8px auto" in css or "margin: 8px auto;" in css
    assert "max-width: 100%" in css
    assert "520px" not in css


@pytest.mark.asyncio
async def test_page_footer_divider_and_suppress_standalone_number(client):
    """Page divider must appear at the footer of each page, and standalone page number blocks must be suppressed."""
    app_res = await client.get("/static/app.js")
    assert app_res.status_code == 200
    app_js = app_res.text

    # app.js must suppress standalone page numbers from main card flow
    assert "isPageNumber" in app_js
    assert "appendPageDivider" in app_js

    # app.css must hide page-number-block completely
    css_res = await client.get("/static/app.css")
    assert css_res.status_code == 200
    css = css_res.text
    assert ".result-block.page-number-block" in css
    assert "display: none !important" in css


@pytest.mark.asyncio
async def test_page_divider_is_static_without_navigation(client):
    """Page divider must be a static visual indicator without click navigation or pointer cursor."""
    app_res = await client.get("/static/app.js")
    assert app_res.status_code == 200
    app_js = app_res.text

    idx_start = app_js.index("const appendPageDivider =")
    idx_end = app_js.index("let currentPageIndex =")
    divider_code = app_js[idx_start:idx_end]

    # Must NOT have click event listener on divider
    assert "divider.addEventListener" not in divider_code
    assert "focusPageNumber" not in divider_code

    # CSS must use cursor: default rather than pointer, and not have hover effects
    css_res = await client.get("/static/app.css")
    assert css_res.status_code == 200
    css = css_res.text
    assert ".result-page-divider" in css
    assert "cursor: default" in css
    assert ".result-page-divider:hover" not in css


@pytest.mark.asyncio
async def test_document_state_sync_on_refresh_and_parse_loading(client):
    """Document state must be synchronized in refreshDocuments, openParse, and syncToolbar."""
    app_res = await client.get("/static/app.js")
    assert app_res.status_code == 200
    js = app_res.text

    # 1. refreshDocuments must sync state.document if currently open
    idx_start = js.index("async function refreshDocuments(")
    idx_end = js.index("async function setDocumentFavorite(")
    refresh_func = js[idx_start:idx_end]
    assert "state.document" in refresh_func
    assert "find(" in refresh_func or "document_id === state.document.document_id" in refresh_func

    # 2. openParse must reload wire.getDocument to keep state.document in sync
    parse_start = js.index("async function openParse(")
    parse_end = js.index("function renderVersionSelect(")
    open_parse_func = js[parse_start:parse_end]
    assert "wire.getDocument(documentId)" in open_parse_func
    assert "state.document = " in open_parse_func

    # 3. syncToolbar must align translation button tooltip with active parse run id
    tb_start = js.index("function syncToolbar(")
    tb_end = js.index("function syncWorkspaceLayout(")
    tb_func = js[tb_start:tb_end]
    assert "state.parse?.parse_run_id" in tb_func


@pytest.mark.asyncio
async def test_block_markdown_unified_editor_with_preview_and_code(client):
    """Block editing must provide unified single-markdown editor with preview and code mode toggling."""
    app_res = await client.get("/static/app.js")
    assert app_res.status_code == 200
    js = app_res.text

    card_start = js.index("function renderBlockEditCard(")
    card_end = js.index("async function saveBlockEdit(")
    card_func = js[card_start:card_end]

    # Must NOT have block-edit-nodes anymore (no multi-fragment chopping)
    assert "block-edit-nodes" not in card_func

    # Must provide preview and code mode toggle
    assert "block-edit-mode-btn" in card_func
    assert "预览" in card_func
    assert "code" in card_func
    assert "block-edit-preview-pane" in card_func
    assert "block-edit-code-pane" in card_func

    # Button text must be "修改" instead of "纠正"
    btn_start = js.index("function createSourceEditButton(")
    btn_end = js.index("function renderBlockEditCard(")
    btn_func = js[btn_start:btn_end]
    assert 'button.textContent = "修改"' in btn_func

    # Must provide dropdown for heading/body switching
    assert "block-format-dropdown" in card_func
    assert "block-format-dropdown-menu" in card_func
    assert "标题 1" in card_func
    assert "标题 2" in card_func
    assert "标题 3" in card_func

    # Must provide undo and redo buttons
    assert "undo" in card_func.lower()
    assert "redo" in card_func.lower()


@pytest.mark.asyncio
async def test_block_level_translation_and_source_edit_persistence(tmp_path):
    """Test block-level translation and source-edits persistence and markdown rendering directly."""
    from uuid import uuid4
    from easylearn.config import AppSettings, Settings
    from easylearn.main import create_app
    from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode, MathNode

    app = create_app(Settings(app=AppSettings(data_dir=tmp_path)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        doc_res = await c.post("/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")})
        assert doc_res.status_code == 201
        doc_id = doc_res.json()["document_id"]

        parse_id = uuid4()
        ir = DocumentIR(
            document_id=doc_id,
            parse_run_id=parse_id,
            preview_asset_id=uuid4(),
            preview_sha256="a" * 64,
            mineru_version="test",
            adapter_version="test",
            pages=(PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),),
            blocks=(
                Block(
                    block_id="b1",
                    block_type="paragraph",
                    order_index=0,
                    source_nodes=(
                        TextNode(node_id="n0", text="Before formula "),
                        MathNode(node_id="n1", latex=r"9\times"),
                        TextNode(node_id="n2", text=" after formula."),
                    ),
                ),
            ),
        )

        parse_dir = tmp_path / "documents" / str(doc_id) / "parses" / str(parse_id)
        parse_dir.mkdir(parents=True, exist_ok=True)
        (parse_dir / "document.json").write_text(ir.model_dump_json(exclude_computed_fields=True), encoding="utf-8")
        (parse_dir / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

        state = app.state.services
        async with state.database.transaction() as conn:
            await conn.execute(
                "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, 1, '{}', '2026-01-01T00:00:00+00:00')",
                (str(parse_id), str(doc_id), f"parses/{parse_id}/preview.pdf", f"parses/{parse_id}/document.json"),
            )
            await conn.execute(
                "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                (str(parse_id), str(doc_id)),
            )

        # 1. PATCH block-level translation
        edit_zh_res = await c.patch(
            f"/api/documents/{doc_id}/translations/b1",
            json={
                "parse_id": str(parse_id),
                "block_id": "b1",
                "expected_revision": 0,
                "text": "这是整段纠正后的译文，包含 $9\\times$ 公式！",
            },
        )
        assert edit_zh_res.status_code == 200, edit_zh_res.text
        assert edit_zh_res.json()["unit_id"] == "b1"
        assert edit_zh_res.json()["effective_text"] == "这是整段纠正后的译文，包含 $9\\times$ 公式！"

        # 2. Verify markdown export reflects block-level translation
        md_zh_res = await c.get(f"/api/documents/{doc_id}/parses/{parse_id}/markdown?language=zh")
        assert md_zh_res.status_code == 200
        assert "这是整段纠正后的译文，包含 $9\\times$ 公式！" in md_zh_res.text

        # 3. PATCH block-level source-edit with node_id='all'
        edit_src_res = await c.patch(
            f"/api/documents/{doc_id}/source-edits",
            json={
                "parse_id": str(parse_id),
                "block_id": "b1",
                "node_id": "all",
                "expected_revision": 0,
                "text": "This is whole corrected source markdown with $9\\times$!",
            },
        )
        assert edit_src_res.status_code == 200, edit_src_res.text
        assert edit_src_res.json()["effective_text"] == "This is whole corrected source markdown with $9\\times$!"

        # 4. Verify source markdown export reflects block-level edit
        md_src_res = await c.get(f"/api/documents/{doc_id}/parses/{parse_id}/markdown?language=source")
        assert md_src_res.status_code == 200
        assert "This is whole corrected source markdown with $9\\times$!" in md_src_res.text








