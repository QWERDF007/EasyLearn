import hashlib
import json
import os
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from easylearn.document_ir.schema import AssetDescriptor, PageGeometry, SourceCoordinates
from easylearn.errors import DomainError
from easylearn.mineru.adapter import MinerUAdapter, NormalizationContext
from easylearn.mineru.archive import MinerUArchive
from easylearn.mineru.schema import MinerUOptions
from easylearn.previews.pdf import PdfPreflight
from easylearn.previews.schema import PreflightReport, PreviewLimits


@pytest.fixture
def context():
    return NormalizationContext(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=PreflightReport(
            sha256="a" * 64,
            renderer="synthetic-geometry",
            metadata_reader="synthetic-geometry",
            pages=(
                PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
            ),
        ),
        options=MinerUOptions(page_count=1),
        coordinates=(
            SourceCoordinates(
                space="page_units",
                size=(600, 800),
                origin="top_left",
                rotation_applied=True,
                crop_applied=True,
                to_pdf=(1, 0, 0, -1, 0, 800),
            ),
        ),
    )


@pytest.fixture
def middle():
    """A source-derived synthetic middle.json, not an actual MinerU inference capture."""
    return {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [600, 800],
                "discarded_blocks": [],
                "para_blocks": [
                    {
                        "type": "title",
                        "index": 0,
                        "level": 1,
                        "bbox": [20, 20, 580, 40],
                        "lines": [
                            {
                                "bbox": [20, 20, 580, 40],
                                "spans": [
                                    {"type": "text", "content": "Introduction"},
                                ],
                            }
                        ],
                    },
                    {
                        "type": "text",
                        "index": 1,
                        "bbox": [20, 40, 580, 100],
                        "lines": [
                            {
                                "bbox": [20, 40, 580, 100],
                                "spans": [
                                    {"type": "text", "content": "Energy "},
                                    {"type": "inline_equation", "content": "E=mc^2"},
                                    {"type": "text", "content": " is preserved."},
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }


def test_middle_adapter_keeps_typed_inline_content_identity_order_and_coordinate_evidence(
    context, middle
):
    raw = json.dumps(middle).encode()
    document = MinerUAdapter().normalize(raw, context=context)
    assert document.document_id == UUID(int=1)
    assert document.parse_run_id == UUID(int=2)
    assert document.preview_asset_id == UUID(int=3)
    assert document.preview_sha256 == "a" * 64
    assert document.mineru_version == "3.4.5"
    assert [block.block_type for block in document.blocks] == ["heading", "paragraph"]
    assert [block.order_index for block in document.blocks] == [0, 1]
    paragraph = document.blocks[1]
    assert paragraph.section_path == ("Introduction",)
    assert [node.type for node in paragraph.source_nodes] == ["text", "math", "text"]
    assert paragraph.source_nodes[1].latex == "E=mc^2"
    assert paragraph.source_text == "Energy E=mc^2 is preserved."
    assert paragraph.source_regions[0].bbox_pdf == (20, 700, 580, 760)
    assert paragraph.source_regions[0].source_bbox == (20, 40, 580, 100)
    assert paragraph.localization_level == "region"
    assert MinerUAdapter().normalize(raw, context=context) == document


@pytest.mark.parametrize(
    "case", ["version", "backend", "page_count", "page_order", "unknown_block"]
)
def test_adapter_rejects_protocol_and_snapshot_mismatches_before_returning_any_ir(
    context, middle, case
):
    if case == "version":
        middle["_version_name"] = "3.4.4"
    elif case == "backend":
        middle["_backend"] = "pipeline"
    elif case == "page_count":
        middle["pdf_info"] = []
    elif case == "page_order":
        middle["pdf_info"][0]["page_idx"] = 1
    else:
        middle["pdf_info"][0]["para_blocks"][0]["type"] = "unregistered_kind"
    with pytest.raises(DomainError, match="MINERU_PROTOCOL_MISMATCH"):
        MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)


@pytest.mark.parametrize("case", ["registration_count", "page_size", "space", "origin"])
def test_middle_coordinates_must_match_explicit_page_unit_registration(context, middle, case):
    if case == "registration_count":
        context = context.model_copy(update={"coordinates": ()})
    elif case == "page_size":
        middle["pdf_info"][0]["page_size"] = [6000, 8000]
    else:
        registration = context.coordinates[0].model_copy(
            update={case: "normalized_1000" if case == "space" else "bottom_left"}
        )
        context = context.model_copy(update={"coordinates": (registration,)})
    with pytest.raises(DomainError, match="ADAPTER_COORDINATE_INVALID"):
        MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)


@pytest.mark.parametrize("case", ["missing_registration", "missing_bbox"])
def test_missing_coordinate_evidence_keeps_content_with_an_explicit_page_fallback(
    context, middle, case
):
    if case == "missing_registration":
        context = context.model_copy(update={"coordinates": (None,)})
    else:
        middle["pdf_info"][0]["para_blocks"][1].pop("bbox")
        for line in middle["pdf_info"][0]["para_blocks"][1]["lines"]:
            line.pop("bbox")
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    paragraph = document.blocks[1]
    assert paragraph.source_text == "Energy E=mc^2 is preserved."
    assert paragraph.localization_level == "page"
    assert paragraph.source_locator.page_indices == (0,)
    assert paragraph.source_regions == ()
    assert "COORDINATE_EVIDENCE_MISSING" in paragraph.parse_warnings


def test_merged_paragraph_keeps_each_disjoint_line_region_instead_of_its_stale_root_box(
    context, middle
):
    paragraph = middle["pdf_info"][0]["para_blocks"][1]
    paragraph["lines"].append(
        {
            "bbox": [310, 120, 580, 160],
            "spans": [{"type": "text", "content": "Next column."}],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    block = document.blocks[1]
    assert block.source_text == "Energy E=mc^2 is preserved.\nNext column."
    assert [region.bbox_pdf for region in block.source_regions] == [
        (20, 700, 580, 760),
        (310, 640, 580, 680),
    ]


def test_cross_page_content_without_source_identity_is_not_projected_onto_the_parent_page(
    context, middle
):
    paragraph = middle["pdf_info"][0]["para_blocks"][1]
    paragraph["lines"].append(
        {
            "bbox": [20, 50, 580, 80],
            "spans": [{"type": "text", "content": "From another page.", "cross_page": True}],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    block = document.blocks[1]
    assert block.source_text.endswith("\nFrom another page.")
    assert block.localization_level == "none"
    assert block.source_regions == ()
    assert block.source_locator is None
    assert "CROSS_PAGE_SOURCE_UNRESOLVED" in block.parse_warnings


def test_cross_page_paragraph_uses_unique_preproc_line_evidence_and_skips_deleted_placeholders(
    context, middle
):
    second = {
        "type": "text",
        "bbox": [20, 50, 580, 80],
        "lines": [
            {
                "bbox": [20, 50, 580, 80],
                "spans": [{"type": "text", "content": "Next page."}],
            }
        ],
    }
    first_page = middle["pdf_info"][0]
    first_page["preproc_blocks"] = deepcopy(first_page["para_blocks"])
    appended_line = deepcopy(second["lines"][0])
    appended_line["spans"][0]["cross_page"] = True
    first_page["para_blocks"][1]["lines"].append(appended_line)
    middle["pdf_info"].append(
        {
            "page_idx": 1,
            "page_size": [600, 800],
            "preproc_blocks": [second],
            "para_blocks": [{**second, "lines": [], "lines_deleted": True}],
        }
    )
    preview = context.preview.model_copy(
        update={
            "pages": (
                context.preview.pages[0],
                context.preview.pages[0].model_copy(update={"page_index": 1}),
            )
        }
    )
    context = context.model_copy(
        update={
            "preview": preview,
            "options": MinerUOptions(page_count=2),
            "coordinates": context.coordinates * 2,
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    assert len(document.blocks) == 2
    paragraph = document.blocks[1]
    assert paragraph.source_text.endswith("\nNext page.")
    assert [region.page_index for region in paragraph.source_regions] == [0, 1]
    assert paragraph.source_regions[1].bbox_pdf == (20, 720, 580, 750)
    assert "CROSS_PAGE_SOURCE_UNRESOLVED" not in paragraph.parse_warnings


def test_formula_screenshot_uses_registered_internal_asset_and_keeps_original_latex(
    context, middle
):
    screenshot = AssetDescriptor(
        asset_id=UUID(int=10), sha256="b" * 64, mime="image/jpeg", export_path="images/formula.jpg"
    )
    context = NormalizationContext.model_validate(
        {**context.model_dump(exclude_computed_fields=True), "assets": {"formula.jpg": screenshot}}
    )
    equation = middle["pdf_info"][0]["para_blocks"][1]
    equation["type"] = "interline_equation"
    equation["lines"][0]["spans"] = [
        {"type": "interline_equation", "content": r"\frac{a}{b}", "image_path": "formula.jpg"}
    ]
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    formula = document.blocks[1]
    assert formula.block_type == "formula"
    assert formula.source_nodes[0].latex == r"\frac{a}{b}"
    assert formula.source_nodes[0].screenshot_asset_id == UUID(int=10)
    assert document.assets == (screenshot,)


@pytest.mark.parametrize("visual", ["image", "chart"])
def test_image_group_preserves_body_asset_caption_and_footnote_relations(context, middle, visual):
    asset = AssetDescriptor(
        asset_id=UUID(int=10), sha256="b" * 64, mime="image/jpeg", export_path="images/figure.jpg"
    )
    context = context.model_copy(update={"assets": {"figure.jpg": asset}})
    middle["pdf_info"][0]["para_blocks"].append(
        {
            "type": visual,
            "bbox": [20, 120, 580, 400],
            "blocks": [
                {
                    "type": f"{visual}_body",
                    "bbox": [20, 120, 580, 400],
                    "lines": [
                        {
                            "bbox": [20, 120, 580, 400],
                            "spans": [
                                {
                                    "type": visual,
                                    "image_path": "figure.jpg",
                                    "content": "A diagram",
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": f"{visual}_caption",
                    "bbox": [20, 410, 580, 430],
                    "lines": [
                        {
                            "bbox": [20, 410, 580, 430],
                            "spans": [{"type": "text", "content": "Figure 1. Architecture."}],
                        }
                    ],
                },
                {
                    "type": f"{visual}_footnote",
                    "bbox": [20, 440, 580, 460],
                    "lines": [
                        {
                            "bbox": [20, 440, 580, 460],
                            "spans": [{"type": "text", "content": "Not to scale."}],
                        }
                    ],
                },
            ],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    figure, caption, footnote = document.blocks[2:]
    assert figure.block_type == "image"
    assert figure.source_nodes[0].asset_id == UUID(int=10)
    assert figure.source_nodes[0].alt == "A diagram"
    assert figure.source_regions[0].bbox_pdf == (20, 400, 580, 680)
    assert (caption.block_type, caption.source_text) == ("caption", "Figure 1. Architecture.")
    assert (footnote.block_type, footnote.source_text) == ("footnote", "Not to scale.")
    assert caption.parent_block_id == footnote.parent_block_id == figure.block_id
    assert [
        (edge.source.block_id, edge.target.block_id, edge.kind) for edge in document.relations
    ] == [
        (caption.block_id, figure.block_id, "caption_of"),
        (footnote.block_id, figure.block_id, "footnote_of"),
    ]
    assert all(edge.target.document_id == context.document_id for edge in document.relations)
    assert all(edge.target.parse_run_id == context.parse_run_id for edge in document.relations)
    assert document.assets == (asset,)


@pytest.mark.parametrize(
    "markup",
    [
        '<table><tr><td rowspan="-1">bad</td></tr></table>',
        '<table><tr><td rowspan="2">outside</td></tr></table>',
        '<table><tr><td colspan="0">bad</td></tr></table>',
        '<table><tr><td colspan="wrong">bad</td></tr></table>',
        '<table><tr><td>A</td><td rowspan="2">B</td></tr>'
        '<tr><td colspan="2">overlap</td></tr></table>',
        "<table><tr><td><table><tr><td>nested</td></tr></table></td></tr></table>",
        "<table><tr><td><script>run()</script></td></tr></table>",
        "<table><tr><td>one</td></tr></table><table><tr><td>two</td></tr></table>",
        "<table><tr><div><td>misplaced</td></div><td>valid</td></tr></table>",
        "<table><td>outside-row</td><tr><td>valid</td></tr></table>",
    ],
)
def test_invalid_table_layout_cannot_silently_lose_cells_or_publish_a_false_grid(
    context, table_middle, markup
):
    table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]["html"] = (
        markup
    )
    with pytest.raises(DomainError, match="MINERU_TABLE_INVALID"):
        MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)


def test_missing_leaf_lines_is_a_protocol_error_not_an_empty_success(context, middle):
    middle["pdf_info"][0]["para_blocks"][1].pop("lines")
    with pytest.raises(DomainError, match="MINERU_PROTOCOL_MISMATCH"):
        MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)


@pytest.mark.parametrize(
    "reference", ["../figure.jpg", "https://example.test/figure.jpg", "missing.jpg"]
)
def test_image_reference_cannot_escape_or_bypass_the_registered_assets(context, middle, reference):
    middle["pdf_info"][0]["para_blocks"][1]["lines"][0]["spans"] = [
        {"type": "interline_equation", "content": "x=1", "image_path": reference}
    ]
    with pytest.raises(DomainError, match="MINERU_(PROTOCOL_MISMATCH|ASSET_INVALID)"):
        MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)


@pytest.fixture
def table_middle(middle):
    middle["pdf_info"][0]["para_blocks"].append(
        {
            "type": "table",
            "bbox": [20, 120, 580, 400],
            "blocks": [
                {
                    "type": "table_body",
                    "bbox": [20, 120, 580, 400],
                    "lines": [
                        {
                            "bbox": [20, 120, 580, 400],
                            "spans": [
                                {
                                    "type": "table",
                                    "html": (
                                        '<table><tr><th colspan="3">Measurements</th></tr>'
                                        '<tr><th rowspan="2">Sample</th>'
                                        "<td>Mass</td><td>2 kg</td></tr>"
                                        "<tr><td>Speed</td><td>3 m/s</td></tr></table>"
                                    ),
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    middle["pdf_info"][0]["preproc_blocks"] = deepcopy(middle["pdf_info"][0]["para_blocks"])
    return middle


def test_table_preserves_merged_headers_and_cell_identity_without_inventing_cell_regions(
    context, table_middle
):
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    parent, *cells = document.blocks[2:]
    assert parent.block_type == "table"
    assert parent.source_text == ""
    assert parent.table.rows == parent.table.columns == 3
    assert parent.table.complete is True
    assert [
        (cell.row, cell.column, cell.row_span, cell.column_span) for cell in parent.table.cells
    ] == [
        (0, 0, 1, 3),
        (1, 0, 2, 1),
        (1, 1, 1, 1),
        (1, 2, 1, 1),
        (2, 1, 1, 1),
        (2, 2, 1, 1),
    ]
    assert [cell.role for cell in parent.table.cells] == [
        "header",
        "header",
        "data",
        "data",
        "data",
        "data",
    ]
    assert [cell.source_text for cell in cells] == [
        "Measurements",
        "Sample",
        "Mass",
        "2 kg",
        "Speed",
        "3 m/s",
    ]
    assert [cell.block_id for cell in cells] == [
        cell.block_ref.block_id for cell in parent.table.cells
    ]
    assert all(cell.block_ref.parse_run_id == context.parse_run_id for cell in parent.table.cells)
    assert all(
        cell.parent_block_id == parent.block_id and cell.block_type == "table_cell"
        for cell in cells
    )
    assert parent.source_regions[0].bbox_pdf == (20, 400, 580, 680)
    assert all(cell.source_regions == () and cell.localization_level == "page" for cell in cells)
    assert all("TABLE_CELL_REGION_UNAVAILABLE" in cell.parse_warnings for cell in cells)
    assert [block.order_index for block in document.blocks] == list(range(9))


@pytest.mark.parametrize("evidence", ["missing_preproc", "merged_html"])
def test_table_without_matching_original_evidence_does_not_claim_the_first_page_region(
    context, table_middle, evidence
):
    page = table_middle["pdf_info"][0]
    if evidence == "missing_preproc":
        page.pop("preproc_blocks")
    else:
        span = page["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
        span["html"] = span["html"].replace("3 m/s", "Merged from another page")
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    parent, *cells = document.blocks[2:]
    assert parent.table.complete is True
    assert len(cells) == 6
    assert all(block.localization_level == "none" for block in (parent, *cells))
    assert all("TABLE_SOURCE_UNRESOLVED" in block.parse_warnings for block in (parent, *cells))


def test_table_footnote_marked_cross_page_at_block_level_requires_source_evidence(
    context, table_middle
):
    table_middle["pdf_info"][0]["para_blocks"][2]["blocks"].append(
        {
            "type": "table_footnote",
            "cross_page": True,
            "bbox": [20, 440, 580, 460],
            "lines": [
                {
                    "bbox": [20, 440, 580, 460],
                    "spans": [{"type": "text", "content": "From another page"}],
                }
            ],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    footnote = document.blocks[-1]
    assert footnote.block_type == "footnote"
    assert footnote.source_text == "From another page"
    assert footnote.localization_level == "none"
    assert "CROSS_PAGE_SOURCE_UNRESOLVED" in footnote.parse_warnings


def test_table_cells_keep_inline_math_code_links_images_and_line_breaks(context, table_middle):
    asset = AssetDescriptor(
        asset_id=UUID(int=11), sha256="c" * 64, mime="image/png", export_path="images/cell.png"
    )
    context = context.model_copy(update={"assets": {"cell.png": asset}})
    span = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
    span["html"] = (
        "<table><tr><td>A &amp; B<br/>Use <code>foo()</code> "
        '<a href="https://example.org/paper">ref</a> '
        r'\(E=mc^2\)<img src="cell.png" alt="plot"/></td></tr></table>'
    )
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    cell = document.blocks[3]
    assert cell.source_text == "A & B\nUse foo() ref E=mc^2plot"
    nodes = cell.source_nodes
    assert [node.type for node in nodes] == [
        "text",
        "text",
        "text",
        "code",
        "text",
        "link",
        "text",
        "math",
        "image",
    ]
    assert nodes[3].code == "foo()"
    assert nodes[5].target == "https://example.org/paper"
    assert nodes[7].latex == "E=mc^2"
    assert nodes[8].asset_id == UUID(int=11)
    assert document.assets == (asset,)


@pytest.mark.parametrize(
    "limit",
    [
        {"max_rows": 2},
        {"max_columns": 2},
        {"max_cells": 5},
        {"max_markup_chars": 50},
    ],
)
def test_table_resource_limits_are_enforced_before_publication(context, table_middle, limit):
    context = NormalizationContext.model_validate(
        {**context.model_dump(exclude_computed_fields=True), "table_limits": limit}
    )
    with pytest.raises(DomainError, match="MINERU_TABLE_LIMIT"):
        MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)


def test_conflicting_registered_image_identities_are_not_silently_overwritten(context, middle):
    first = AssetDescriptor(
        asset_id=UUID(int=10), sha256="b" * 64, mime="image/png", export_path="images/a.png"
    )
    second = first.model_copy(update={"sha256": "c" * 64, "export_path": "images/b.png"})
    context = context.model_copy(update={"assets": {"a.png": first, "b.png": second}})
    middle["pdf_info"][0]["para_blocks"][1]["lines"][0]["spans"] = [
        {"type": "inline_equation", "content": "x", "image_path": "a.png"},
        {"type": "inline_equation", "content": "y", "image_path": "b.png"},
    ]
    with pytest.raises(DomainError, match="MINERU_ASSET_INVALID"):
        MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)


def test_code_group_keeps_verbatim_code_language_and_separate_caption(context, middle):
    middle["pdf_info"][0]["para_blocks"].append(
        {
            "type": "code",
            "sub_type": "code",
            "guess_lang": "python",
            "bbox": [20, 120, 580, 400],
            "blocks": [
                {
                    "type": "code_body",
                    "lines": [
                        {
                            "bbox": [20, 120, 580, 140],
                            "spans": [{"type": "text", "content": "def f():"}],
                        },
                        {
                            "bbox": [20, 140, 580, 160],
                            "spans": [{"type": "text", "content": "    return 1"}],
                        },
                    ],
                },
                {
                    "type": "code_caption",
                    "lines": [
                        {
                            "bbox": [20, 410, 580, 430],
                            "spans": [{"type": "text", "content": "Listing 1"}],
                        }
                    ],
                },
            ],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    code, caption = document.blocks[2:]
    assert code.block_type == "code"
    assert code.source_text == "def f():\n    return 1"
    assert code.translatable is False
    assert code.source_nodes[0].type == code.source_nodes[2].type == "code"
    assert code.source_nodes[0].language == code.source_nodes[2].language == "python"
    assert caption.block_type == "caption" and caption.translatable is True
    assert document.relations[0].kind == "caption_of"


def test_nested_list_preserves_item_hierarchy_and_does_not_duplicate_container_text(
    context, middle
):
    middle["pdf_info"][0]["para_blocks"].append(
        {
            "type": "list",
            "bbox": [20, 120, 580, 300],
            "blocks": [
                {
                    "type": "text",
                    "lines": [
                        {
                            "bbox": [20, 120, 580, 140],
                            "spans": [{"type": "text", "content": "1. First"}],
                        }
                    ],
                },
                {
                    "type": "ref_text",
                    "lines": [
                        {
                            "bbox": [20, 150, 580, 170],
                            "spans": [{"type": "text", "content": "[2] Reference"}],
                        }
                    ],
                },
            ],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    container, first, reference = document.blocks[2:]
    assert container.block_type == "list" and container.source_text == ""
    assert first.block_type == "list_item" and first.source_text == "1. First"
    assert reference.block_type == "reference" and reference.source_text == "[2] Reference"
    assert first.parent_block_id == reference.parent_block_id == container.block_id
    assert container.source_regions == (first.source_regions[0], reference.source_regions[0])


def test_discarded_footnotes_and_headers_are_retained_with_explicit_source_warnings(
    context, middle
):
    middle["pdf_info"][0]["discarded_blocks"] = [
        {
            "type": "header",
            "lines": [
                {"bbox": [20, 5, 580, 15], "spans": [{"type": "text", "content": "Proceedings"}]}
            ],
        },
        {
            "type": "page_footnote",
            "lines": [
                {
                    "bbox": [20, 750, 580, 780],
                    "spans": [{"type": "text", "content": "1. Additional evidence"}],
                }
            ],
        },
    ]
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    header, footnote = document.blocks[2:]
    assert (header.block_type, header.source_text) == ("paragraph", "Proceedings")
    assert (footnote.block_type, footnote.source_text) == ("footnote", "1. Additional evidence")
    assert "UPSTREAM_DISCARDED:header" in header.parse_warnings
    assert "UPSTREAM_DISCARDED:page_footnote" in footnote.parse_warnings
    assert header.block_id == "p0.d0" and footnote.block_id == "p0.d1"
    assert header.source_regions[0].bbox_pdf == (20, 785, 580, 795)
    assert footnote.source_regions[0].bbox_pdf == (20, 20, 580, 50)


@pytest.mark.parametrize("kind", ["list", "index"])
def test_pipeline_list_uses_explicit_start_lines_without_splitting_continuations(
    context, middle, kind
):
    middle["_backend"] = "pipeline"
    context = context.model_copy(
        update={"options": MinerUOptions(page_count=1, backend="pipeline")}
    )
    middle["pdf_info"][0]["para_blocks"].append(
        {
            "type": kind,
            "bbox": [20, 120, 580, 300],
            "lines": [
                {
                    "bbox": [20, 120, 580, 140],
                    "is_list_start_line": True,
                    "spans": [{"type": "text", "content": "1. First"}],
                },
                {"bbox": [40, 140, 580, 160], "spans": [{"type": "text", "content": "continued"}]},
                {
                    "bbox": [20, 180, 580, 200],
                    "is_list_start_line": True,
                    "spans": [{"type": "text", "content": "2. Second"}],
                },
            ],
        }
    )
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    container, first, second = document.blocks[2:]
    assert container.block_type == "list" and container.source_text == ""
    assert first.source_text == "1. First\ncontinued"
    assert second.source_text == "2. Second"
    assert first.block_type == second.block_type == "list_item"
    assert len(first.source_regions) == 2


@pytest.mark.parametrize(
    "upstream_kind, ir_kind",
    [
        ("doc_title", "heading"),
        ("paragraph_title", "heading"),
        ("abstract", "paragraph"),
        ("vertical_text", "paragraph"),
        ("phonetic", "paragraph"),
        ("footer", "paragraph"),
        ("page_number", "paragraph"),
        ("aside_text", "paragraph"),
        ("discarded", "paragraph"),
        ("caption", "caption"),
        ("footnote", "footnote"),
        ("formula_number", "paragraph"),
    ],
)
def test_registered_upstream_text_roles_keep_content_and_geometry(
    context, middle, upstream_kind, ir_kind
):
    middle["pdf_info"][0]["para_blocks"][1]["type"] = upstream_kind
    document = MinerUAdapter().normalize(json.dumps(middle).encode(), context=context)
    block = document.blocks[1]
    assert block.block_type == ir_kind
    assert block.source_text == "Energy E=mc^2 is preserved."
    assert block.source_regions[0].bbox_pdf == (20, 700, 580, 760)


def test_table_without_html_keeps_registered_screenshot_as_an_explicit_structure_fallback(
    context, table_middle
):
    asset = AssetDescriptor(
        asset_id=UUID(int=15), sha256="d" * 64, mime="image/jpeg", export_path="images/table.jpg"
    )
    context = context.model_copy(update={"assets": {"table.jpg": asset}})
    span = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
    span["html"] = None
    span["image_path"] = "table.jpg"
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    assert len(document.blocks) == 3
    table = document.blocks[2]
    assert table.block_type == "table" and table.table is None
    assert table.source_nodes[0].type == "image"
    assert table.source_nodes[0].asset_id == UUID(int=15)
    assert "TABLE_STRUCTURE_UNAVAILABLE" in table.parse_warnings


def test_leaf_body_cannot_hide_unprocessed_nested_content(context, table_middle):
    body = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]
    body["blocks"] = [
        {"type": "text", "lines": [{"spans": [{"type": "text", "content": "Must not disappear"}]}]}
    ]
    with pytest.raises(DomainError, match="MINERU_PROTOCOL_MISMATCH"):
        MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"), reason="Real user PDF not configured"
)
async def test_synthetic_middle_with_real_pdf_and_image_resources_has_consistent_archive_and_ir(
    context, table_middle, tmp_path
):
    """Real resources and PDF subprocess; structural content is not a MinerU inference capture."""
    paper = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
    digest = "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
    preview = await PdfPreflight(PreviewLimits()).inspect(paper, digest)
    assert len(preview.pages) == 27
    first = preview.pages[0]
    assert first.intrinsic_rotation == 0
    x0, y0, x1, y1 = first.crop_box
    registration = SourceCoordinates(
        space="page_units",
        size=(600, 800),
        origin="top_left",
        rotation_applied=True,
        crop_applied=True,
        to_pdf=((x1 - x0) / 600, 0, 0, -(y1 - y0) / 800, x0, y1),
    )
    span = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
    span["image_path"] = "table.png"
    table_middle["pdf_info"][0]["preproc_blocks"] = deepcopy(
        table_middle["pdf_info"][0]["para_blocks"]
    )
    table_middle["pdf_info"].extend(
        {"page_idx": index, "page_size": [600, 800], "para_blocks": []} for index in range(1, 27)
    )
    options = MinerUOptions(page_count=27)
    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as picture:
        picture.save(buffer, format="PNG")
        png = buffer.getvalue()
    destination = tmp_path / "result.zip"
    artifacts = {
        "input.md": b"Synthetic structure",
        "input_middle.json": json.dumps(table_middle).encode(),
        "input_model.json": b"[]",
        "input_content_list.json": b"[]",
        "input_content_list_v2.json": b"[]",
        "input_origin.pdf": paper.read_bytes(),
        "images/table.png": png,
    }
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path, content in artifacts.items():
            archive.writestr(f"input/vlm/{path}", content)
    manifest = MinerUArchive().inspect(destination, options=options)
    by_kind = {member.kind: member for member in manifest.members}
    image_member = by_kind["image"]
    asset = AssetDescriptor(
        asset_id=UUID(int=15),
        sha256=image_member.sha256,
        mime="image/png",
        export_path="images/table.png",
    )
    context = context.model_copy(
        update={
            "preview": preview,
            "options": options,
            "coordinates": (registration, *(None for _ in range(26))),
            "assets": {"table.png": asset},
        }
    )
    with ZipFile(destination) as archive:
        raw = archive.read(by_kind["middle"].path)
        with Image.open(BytesIO(archive.read(image_member.path))) as restored:
            restored.load()
            assert restored.format == "PNG" and restored.size == (40, 20)
        document = MinerUAdapter().normalize(raw, context=context)
    assert by_kind["original"].sha256 == document.preview_sha256 == digest
    assert document.page_count == 27 and document.pages == preview.pages
    assert document.assets == (asset,)
    assert asset.sha256 == hashlib.sha256(png).hexdigest()
    assert document.blocks[2].source_regions[0].bbox_norm == pytest.approx(
        (0.0333333333333, 0.15, 0.966666666667, 0.5)
    )
    assert document.blocks[2].table.complete is True
    assert hashlib.sha256(paper.read_bytes()).hexdigest() == digest


def test_table_cell_paragraph_boundaries_do_not_concatenate_words(context, table_middle):
    span = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
    span["html"] = (
        "<table><tr><td><p>First</p><p>Second</p><div>Third<br/>last</div></td></tr></table>"
    )
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    assert document.blocks[3].source_text == "First\nSecond\nThird\nlast"


def test_caption_before_visual_body_retains_upstream_reading_order(context, table_middle):
    table_middle["pdf_info"][0]["para_blocks"][2]["blocks"].insert(
        0,
        {
            "type": "table_caption",
            "lines": [
                {
                    "bbox": [20, 100, 580, 115],
                    "spans": [{"type": "text", "content": "Table 1. Measurements"}],
                }
            ],
        },
    )
    document = MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
    caption, table = document.blocks[2:4]
    assert caption.block_type == "caption" and caption.source_text == "Table 1. Measurements"
    assert table.block_type == "table"
    assert caption.parent_block_id == table.block_id
    assert document.blocks[4].block_type == "table_cell"
    assert document.relations[0].target.block_id == table.block_id


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="https://example.org"><img src="cell.png"/></a>',
        "<code>first<br/>second</code>",
    ],
)
def test_atomic_inline_nodes_reject_nested_content_they_cannot_represent(
    context, table_middle, markup
):
    span = table_middle["pdf_info"][0]["para_blocks"][2]["blocks"][0]["lines"][0]["spans"][0]
    span["html"] = f"<table><tr><td>{markup}</td></tr></table>"
    with pytest.raises(DomainError, match="MINERU_TABLE_INVALID"):
        MinerUAdapter().normalize(json.dumps(table_middle).encode(), context=context)
