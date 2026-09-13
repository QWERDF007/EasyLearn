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





