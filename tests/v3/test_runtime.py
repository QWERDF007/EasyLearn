from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from easylearn.cache import DocumentCache
from easylearn.config import ExtensionSettings, Settings
from easylearn.document_ir.schema import (
    AssetDescriptor,
    Block,
    BlockRef,
    DocumentIR,
    ImageNode,
    PageGeometry,
    PageLocator,
    TableCell,
    TableStructure,
    TextNode,
)
from easylearn.images import ImageLimits, inspect_image
from easylearn.logging_setup import configure_logging
from easylearn.maintenance import MaintenanceService
from easylearn.paths import DataPaths
from easylearn.qa import QARequest, QAService
from easylearn.rendering import render_markdown


def _ir(document_id, parse_id, text: str) -> DocumentIR:
    return DocumentIR(
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
                source_nodes=(TextNode(node_id="n1", text=text),),
            ),
        ),
    )


def test_document_cache_is_bounded_and_refreshes_lru_order():
    document_id = uuid4()
    first, second, third = uuid4(), uuid4(), uuid4()
    cache = DocumentCache(max_documents=2, max_bytes=100_000)
    first_ir = _ir(document_id, first, "first")
    cache.put(document_id, first, first_ir)
    cache.put(document_id, second, _ir(document_id, second, "second"))
    assert cache.get(document_id, first) is first_ir
    cache.put(document_id, third, _ir(document_id, third, "third"))
    assert cache.get(document_id, first) is first_ir
    assert cache.get(document_id, second) is None
    assert cache.get(document_id, third) is not None
    assert cache.stats.entries == 2


@pytest.mark.asyncio
async def test_maintenance_removes_stale_tmp_and_unreferenced_parse(tmp_path: Path):
    paths = DataPaths(tmp_path).ensure()
    stale_tmp = paths.task(uuid4())
    stale_tmp.mkdir()
    stale_parse = paths.parse(uuid4(), uuid4())
    stale_parse.mkdir(parents=True)
    old = time.time() - 48 * 60 * 60
    for path in (stale_tmp, stale_parse):
        os.utime(path, (old, old))

    report = await MaintenanceService(
        database=None,
        paths=paths,
        tmp_retention_seconds=60,
        export_retention_seconds=60,
        parse_keep=1,
    ).cleanup()
    assert not stale_tmp.exists()
    assert not stale_parse.exists()
    assert report.removed_tmp >= 1
    assert report.removed_orphan_parses >= 1


def test_logging_splits_by_local_date_without_size_rotation(tmp_path: Path):
    current_date = date(2026, 9, 6)
    controller = configure_logging(
        tmp_path / "logs", date_provider=lambda: current_date
    )
    logger = logging.getLogger("easylearn.runtime-test")
    try:
        logger.warning("first day")
        current_date = date(2026, 9, 7)
        logger.warning("second day")
        assert (tmp_path / "logs" / "2026-09-06.log").read_text(encoding="utf-8").count(
            "first day"
        ) == 1
        assert (tmp_path / "logs" / "2026-09-07.log").read_text(encoding="utf-8").count(
            "second day"
        ) == 1
        assert not (tmp_path / "logs" / "easylearn.log.1").exists()
    finally:
        controller.close()


def test_logging_removes_console_handlers(tmp_path: Path):
    root = logging.getLogger()
    test_stream_handler = logging.StreamHandler()
    root.addHandler(test_stream_handler)
    assert test_stream_handler in root.handlers

    controller = configure_logging(tmp_path / "logs")
    try:
        assert test_stream_handler not in root.handlers
        assert not any(
            isinstance(h, logging.StreamHandler)
            and not h.__class__.__module__.startswith(("_pytest", "pytest"))
            for h in root.handlers
        )
    finally:
        controller.close()


def test_lifecycle_logs_to_console_while_domain_logs_go_to_file(tmp_path: Path):
    controller = configure_logging(tmp_path / "logs")
    main_logger = logging.getLogger("easylearn.main")
    trans_logger = logging.getLogger("easylearn.translation")
    try:
        # easylearn.main has a console handler for startup/shutdown
        assert any(
            isinstance(h, logging.StreamHandler)
            and not h.__class__.__module__.startswith(("_pytest", "pytest"))
            for h in main_logger.handlers
        )
        # domain loggers (easylearn.translation, parser, etc.) do NOT have a console handler
        assert not any(
            isinstance(h, logging.StreamHandler)
            and not h.__class__.__module__.startswith(("_pytest", "pytest"))
            for h in trans_logger.handlers
        )
    finally:
        controller.close()


def test_svg_is_decoded_as_a_safe_image():
    from io import BytesIO

    source = BytesIO(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
        b'<rect width="20" height="10" fill="red"/></svg>'
    )
    result = inspect_image(
        source,
        expected_suffix=".svg",
        limits=ImageLimits(max_pixels=1000, max_total_pixels=1000),
    )
    assert result.mime == "image/svg+xml"
    assert (result.width, result.height, result.frames) == (20, 10, 1)


def test_svg_external_content_is_rejected():
    from io import BytesIO

    source = BytesIO(
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    )
    with pytest.raises(Exception, match="SVG"):
        inspect_image(source, expected_suffix=".svg", limits=ImageLimits())


def test_markdown_uses_exportable_asset_links():
    document_id, parse_id, asset_id = uuid4(), uuid4(), uuid4()
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
                block_type="image",
                order_index=0,
                source_nodes=(ImageNode(node_id="image", asset_id=asset_id, alt="figure"),),
            ),
        ),
        assets=(
            AssetDescriptor(
                asset_id=asset_id,
                sha256="1" * 64,
                mime="image/png",
                export_path="images/figure.png",
            ),
        ),
    )
    markdown = render_markdown(ir, asset_links={str(asset_id): "images/figure.png"})
    assert "![figure](images/figure.png)" in markdown


def test_markdown_does_not_render_nested_table_cells_as_top_level_blocks():
    document_id, parse_id = uuid4(), uuid4()
    cell_id = "table.r0.c0"
    table = Block(
        block_id="table",
        block_type="table",
        order_index=0,
        table=TableStructure(
            rows=1,
            columns=1,
            cells=(
                TableCell(
                    block_ref=BlockRef(
                        document_id=document_id, parse_run_id=parse_id, block_id=cell_id
                    ),
                    row=0,
                    column=0,
                ),
            ),
        ),
    )
    cell = Block(
        block_id=cell_id,
        block_type="table_cell",
        order_index=1,
        parent_block_id="table",
        source_locator=PageLocator(page_indices=(0,)),
        source_nodes=(TextNode(node_id="text", text="once"),),
    )
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
        blocks=(table, cell),
    )
    assert render_markdown(ir).count("once") == 1


def test_markdown_renders_non_table_children_in_reading_order():
    document_id, parse_id = uuid4(), uuid4()
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
                block_id="list",
                block_type="list",
                order_index=0,
            ),
            Block(
                block_id="item",
                block_type="list_item",
                order_index=1,
                parent_block_id="list",
                source_nodes=(TextNode(node_id="text", text="kept item"),),
            ),
        ),
    )

    assert render_markdown(ir) == "- kept item\n"


def test_qa_budget_keeps_required_blocks_before_optional_candidates():
    document_id, parse_id = uuid4(), uuid4()
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
                block_id="optional",
                block_type="paragraph",
                order_index=0,
                source_locator=PageLocator(page_indices=(0,)),
                source_nodes=(TextNode(node_id="text", text="keyword"),),
            ),
            Block(
                block_id="required",
                block_type="paragraph",
                order_index=1,
                source_locator=PageLocator(page_indices=(0,)),
                source_nodes=(TextNode(node_id="text", text="needed"),),
            ),
        ),
    )
    service = QAService(
        None,
        None,
        None,
        Settings(extensions=ExtensionSettings(qa_context_chars=10)),
    )
    context = service.build_context(
        document_id,
        ir,
        QARequest(parse_id=parse_id, question="keyword", block_ids=("required",)),
    )
    assert [item["block_id"] for item in context] == ["required"]


def test_qa_keyword_candidates_prioritize_relevance_before_document_order():
    document_id, parse_id = uuid4(), uuid4()
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
                block_id="low-score",
                block_type="paragraph",
                order_index=0,
                source_locator=PageLocator(page_indices=(0,)),
                source_nodes=(TextNode(node_id="text", text="target"),),
            ),
            Block(
                block_id="high-score",
                block_type="paragraph",
                order_index=1,
                source_locator=PageLocator(page_indices=(0,)),
                source_nodes=(TextNode(node_id="text", text="target target"),),
            ),
        ),
    )
    service = QAService(
        None,
        None,
        None,
        Settings(extensions=ExtensionSettings(qa_context_chars=100, qa_max_blocks=1)),
    )
    context = service.build_context(
        document_id,
        ir,
        QARequest(parse_id=parse_id, question="target"),
    )
    assert [item["block_id"] for item in context] == ["high-score"]
