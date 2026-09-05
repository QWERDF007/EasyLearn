from uuid import UUID

import pytest
from pydantic import ValidationError

from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode


def test_document_rejects_duplicate_blocks_in_a_parse_snapshot():
    block = Block(
        block_id="b1",
        block_type="paragraph",
        order_index=0,
        source_nodes=(TextNode(node_id="n1", text="Evidence."),),
    )

    with pytest.raises(ValidationError, match="Duplicate block"):
        DocumentIR(
            document_id=UUID(int=1),
            parse_run_id=UUID(int=2),
            preview_asset_id=UUID(int=3),
            preview_sha256="0" * 64,
            mineru_version="3.4.5",
            adapter_version="2.0.0",
            pages=(
                PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
            ),
            blocks=(block, block),
        )


def test_protected_nodes_survive_snapshot_roundtrip_and_text_is_derived():
    payload = {
        "document_id": str(UUID(int=1)),
        "parse_run_id": str(UUID(int=2)),
        "preview_asset_id": str(UUID(int=3)),
        "preview_sha256": "0" * 64,
        "mineru_version": "3.4.5",
        "adapter_version": "2.0.0",
        "pages": [{"page_index": 0, "media_box": [0, 0, 600, 800], "crop_box": [0, 0, 600, 800]}],
        "blocks": [
            {
                "block_id": "b1",
                "block_type": "paragraph",
                "order_index": 0,
                "source_nodes": [
                    {"node_id": "n1", "type": "text", "text": "Keep "},
                    {"node_id": "n2", "type": "math", "latex": r"x^2 + \alpha"},
                    {"node_id": "n3", "type": "code", "code": "x < 3", "language": "python"},
                    {
                        "node_id": "n4",
                        "type": "link",
                        "label": "source",
                        "target": "https://example.org",
                    },
                ],
            }
        ],
    }
    document = DocumentIR.model_validate(payload)
    restored = DocumentIR.model_validate_json(
        document.model_dump_json(exclude_computed_fields=True)
    )

    assert restored == document
    assert restored.blocks[0].source_text == r"Keep x^2 + \alphax < 3source"
    assert restored.blocks[0].translatable is True
    with pytest.raises(ValidationError):
        restored.blocks[0].order_index = 1


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"parent_block_id": "missing"}, "Unknown parent"),
        ({"parent_block_id": "b1"}, "Parent cycle"),
        (
            {"source_nodes": (TextNode(node_id="n1", text="A"), TextNode(node_id="n1", text="B"))},
            "Duplicate node",
        ),
    ],
)
def test_snapshot_rejects_unresolvable_structure(changes, error):
    with pytest.raises(ValidationError, match=error):
        DocumentIR(
            document_id=UUID(int=1),
            parse_run_id=UUID(int=2),
            preview_asset_id=UUID(int=3),
            preview_sha256="0" * 64,
            mineru_version="3.4.5",
            adapter_version="2.0.0",
            pages=(
                PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
            ),
            blocks=(Block(block_id="b1", block_type="paragraph", order_index=0, **changes),),
        )
