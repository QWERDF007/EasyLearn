from uuid import UUID

import pytest
from pydantic import ValidationError

from easylearn.document_ir.schema import DocumentIR


@pytest.fixture
def illustrated_payload(document_payload):
    def ref(block_id):
        return {
            "document_id": document_payload["document_id"],
            "parse_run_id": document_payload["parse_run_id"],
            "block_id": block_id,
        }

    document_payload["assets"] = [
        {
            "asset_id": str(UUID(int=4)),
            "sha256": "a" * 64,
            "mime": "image/png",
            "export_path": "images/figure.png",
        }
    ]
    document_payload["blocks"] = [
        {
            "block_id": "figure",
            "block_type": "image",
            "order_index": 0,
            "source_nodes": [
                {"type": "image", "node_id": "img", "asset_id": str(UUID(int=4)), "alt": "Diagram"}
            ],
        },
        {
            "block_id": "caption",
            "block_type": "caption",
            "order_index": 1,
            "source_nodes": [{"type": "text", "node_id": "text", "text": "System overview"}],
        },
        {
            "block_id": "paragraph",
            "block_type": "paragraph",
            "order_index": 2,
            "source_nodes": [
                {
                    "type": "reference",
                    "node_id": "ref",
                    "label": "Figure 1",
                    "target": ref("figure"),
                }
            ],
        },
    ]
    document_payload["relations"] = [
        {"source": ref("caption"), "target": ref("figure"), "kind": "caption_of"},
    ]
    return document_payload


def test_image_reference_and_caption_keep_versioned_identity(illustrated_payload):
    document = DocumentIR.model_validate(illustrated_payload)
    restored = DocumentIR.model_validate_json(
        document.model_dump_json(exclude_computed_fields=True)
    )
    assert restored == document
    assert document.assets[0].export_path == "images/figure.png"
    assert document.blocks[0].source_text == "Diagram"
    assert document.blocks[0].translatable is True
    assert document.blocks[2].source_text == "Figure 1"
    assert document.blocks[2].source_nodes[0].target.parse_run_id == UUID(int=2)
    assert document.relations[0].source.block_id == "caption"


@pytest.mark.parametrize(
    "case", ["unknown_asset", "foreign_reference", "foreign_relation", "unknown_relation"]
)
def test_references_must_resolve_inside_the_exact_snapshot(illustrated_payload, case):
    if case == "unknown_asset":
        illustrated_payload["assets"] = []
    elif case == "foreign_reference":
        illustrated_payload["blocks"][2]["source_nodes"][0]["target"]["parse_run_id"] = str(
            UUID(int=99)
        )
    elif case == "foreign_relation":
        illustrated_payload["relations"][0]["target"]["document_id"] = str(UUID(int=99))
    else:
        illustrated_payload["relations"][0]["target"]["block_id"] = "missing"
    with pytest.raises(ValidationError, match="(Unknown asset|Reference outside snapshot)"):
        DocumentIR.model_validate(illustrated_payload)


@pytest.mark.parametrize(
    "path",
    [".", "../image.png", "/image.png", "C:/image.png", "images\\image.png", "images/./image.png"],
)
def test_asset_export_names_are_canonical_relative_paths(illustrated_payload, path):
    illustrated_payload["assets"][0]["export_path"] = path
    with pytest.raises(ValidationError, match="Export path"):
        DocumentIR.model_validate(illustrated_payload)


@pytest.mark.parametrize("duplicate", ["id", "path", "directory"])
def test_asset_registry_cannot_have_ambiguous_export_identity(illustrated_payload, duplicate):
    original = illustrated_payload["assets"][0]
    second = {**original, "asset_id": str(UUID(int=5)), "export_path": "images/second.png"}
    if duplicate == "id":
        second["asset_id"] = original["asset_id"]
    elif duplicate == "directory":
        second["export_path"] = "images"
    else:
        second["export_path"] = "IMAGES/FIGURE.PNG"
    illustrated_payload["assets"].append(second)
    with pytest.raises(ValidationError, match="Duplicate asset"):
        DocumentIR.model_validate(illustrated_payload)


def test_formula_keeps_original_latex_and_registered_fallback_image(illustrated_payload):
    illustrated_payload["blocks"][0]["block_type"] = "formula"
    illustrated_payload["blocks"][0]["source_nodes"] = [
        {
            "node_id": "equation",
            "type": "math",
            "latex": r"\unsupported{x}",
            "screenshot_asset_id": str(UUID(int=4)),
        }
    ]
    document = DocumentIR.model_validate(illustrated_payload)
    assert document.blocks[0].source_nodes[0].latex == r"\unsupported{x}"
    assert document.blocks[0].source_nodes[0].screenshot_asset_id == UUID(int=4)
    assert document.blocks[0].translatable is False
    illustrated_payload["assets"] = []
    with pytest.raises(ValidationError, match="Unknown asset"):
        DocumentIR.model_validate(illustrated_payload)


@pytest.mark.parametrize("case", ["duplicate", "self", "caption_source", "image_mime"])
def test_snapshot_rejects_ambiguous_or_mistyped_relationships(illustrated_payload, case):
    if case == "duplicate":
        illustrated_payload["relations"].append(illustrated_payload["relations"][0])
    elif case == "self":
        illustrated_payload["relations"][0]["target"] = illustrated_payload["relations"][0][
            "source"
        ]
    elif case == "caption_source":
        illustrated_payload["relations"][0]["source"]["block_id"] = "paragraph"
    else:
        illustrated_payload["assets"][0]["mime"] = "application/pdf"
    with pytest.raises(ValidationError, match="(Relation|Image asset)"):
        DocumentIR.model_validate(illustrated_payload)
