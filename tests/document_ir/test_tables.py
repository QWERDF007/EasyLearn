from uuid import UUID

import pytest
from pydantic import ValidationError

from easylearn.document_ir.schema import DocumentIR


@pytest.fixture
def table_payload(document_payload):
    def ref(block_id):
        return {
            "document_id": str(UUID(int=1)),
            "parse_run_id": str(UUID(int=2)),
            "block_id": block_id,
        }

    document_payload["blocks"] = [
        {
            "block_id": "table1",
            "block_type": "table",
            "order_index": 0,
            "table": {
                "rows": 2,
                "columns": 2,
                "cells": [
                    {
                        "block_ref": ref("header"),
                        "row": 0,
                        "column": 0,
                        "column_span": 2,
                        "role": "header",
                    },
                    {"block_ref": ref("left"), "row": 1, "column": 0},
                    {"block_ref": ref("right"), "row": 1, "column": 1},
                ],
            },
        },
        *(
            {
                "block_id": block_id,
                "block_type": "table_cell",
                "order_index": order,
                "parent_block_id": "table1",
                "source_nodes": [{"node_id": "text", "type": "text", "text": text}],
            }
            for order, (block_id, text) in enumerate(
                [("header", "Measurements"), ("left", "Height"), ("right", "12 mm")], start=1
            )
        ),
    ]
    return document_payload


def test_table_keeps_merged_header_and_single_source_of_cell_text(table_payload):
    document = DocumentIR.model_validate(table_payload)
    restored = DocumentIR.model_validate_json(
        document.model_dump_json(exclude_computed_fields=True)
    )
    table = restored.blocks[0].table
    assert table.rows == 2 and table.columns == 2
    assert table.cells[0].column_span == 2
    assert table.cells[0].role == "header"
    assert table.cells[0].block_ref.block_id == "header"
    assert restored.blocks[1].source_text == "Measurements"
    assert table.complete is True
    assert restored == document


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"row": 0}, "Overlapping table cells"),
        ({"row_span": 2}, "Cell outside table dimensions"),
        (
            {
                "block_ref": {
                    "document_id": str(UUID(int=1)),
                    "parse_run_id": str(UUID(int=99)),
                    "block_id": "right",
                }
            },
            "Table cell version",
        ),
        (
            {
                "block_ref": {
                    "document_id": str(UUID(int=1)),
                    "parse_run_id": str(UUID(int=2)),
                    "block_id": "missing",
                }
            },
            "Unknown table cell",
        ),
    ],
)
def test_table_rejects_overlapping_out_of_bounds_and_foreign_cells(table_payload, change, reason):
    table_payload["blocks"][0]["table"]["cells"][2].update(change)
    with pytest.raises(ValidationError, match=reason):
        DocumentIR.model_validate(table_payload)


def test_table_cells_cannot_disagree_with_parent_membership(table_payload):
    table_payload["blocks"][3]["parent_block_id"] = None
    with pytest.raises(ValidationError, match="Table cell must belong"):
        DocumentIR.model_validate(table_payload)


def test_incomplete_table_preserves_known_cells_without_inventing_missing_cells(table_payload):
    table_payload["blocks"].pop()
    table_payload["blocks"][0]["table"]["cells"].pop()
    document = DocumentIR.model_validate(table_payload)
    assert document.blocks[0].table.complete is False
    assert len(document.blocks[0].table.cells) == 2


@pytest.mark.parametrize("case", ["unlisted_cell", "non_table_container"])
def test_table_membership_has_one_authoritative_structure(table_payload, case):
    if case == "unlisted_cell":
        table_payload["blocks"][0]["table"]["cells"].pop()
    else:
        table_payload["blocks"][0]["block_type"] = "paragraph"
    with pytest.raises(ValidationError, match="Table structure"):
        DocumentIR.model_validate(table_payload)
