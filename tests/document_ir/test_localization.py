import pytest
from pydantic import ValidationError

from easylearn.document_ir.coordinates import CoordinateMapper
from easylearn.document_ir.schema import DocumentIR, PageGeometry, SourceCoordinates


@pytest.mark.parametrize(
    "locator, expected",
    [
        ({"kind": "pdf_page", "page_indices": [0]}, "page"),
        ({"kind": "office_element", "format": "docx", "element_id": "paragraph-12"}, "element"),
        (None, "none"),
    ],
)
def test_localization_reports_only_the_evidence_available(document_payload, locator, expected):
    document_payload["blocks"] = [
        {
            "block_id": "p1",
            "block_type": "paragraph",
            "order_index": 0,
            "source_locator": locator,
        }
    ]
    block = DocumentIR.model_validate(document_payload).blocks[0]
    assert block.localization_level == expected
    assert block.source_regions == ()


@pytest.mark.parametrize("pages", [[1], [0, 0]])
def test_page_only_locator_must_resolve_unambiguously(document_payload, pages):
    document_payload["blocks"] = [
        {
            "block_id": "p1",
            "block_type": "paragraph",
            "order_index": 0,
            "source_locator": {"kind": "pdf_page", "page_indices": pages},
        }
    ]
    with pytest.raises(ValidationError, match="Locator pages"):
        DocumentIR.model_validate(document_payload)


def test_region_localization_preserves_coordinate_provenance_without_native_fallback(
    document_payload,
):
    page = PageGeometry.model_validate(document_payload["pages"][0])
    source = SourceCoordinates(
        space="normalized_1000",
        size=(600, 800),
        origin="top_left",
        rotation_applied=False,
        crop_applied=True,
        to_pdf=(1, 0, 0, -1, 0, 800),
    )
    region = CoordinateMapper(page, source).map_bbox((0, 0, 100, 100))
    document_payload["blocks"] = [
        {
            "block_id": "p1",
            "block_type": "paragraph",
            "order_index": 0,
            "source_regions": [region.model_dump(exclude_computed_fields=True)],
        }
    ]
    block = DocumentIR.model_validate(document_payload).blocks[0]
    assert block.localization_level == "region"
    assert block.source_regions[0].source_coordinates == source
    assert block.source_regions[0].source_to_pdf_transform == (0.6, 0, 0, -0.8, 0, 800)
    document_payload["blocks"][0]["source_locator"] = {
        "kind": "office_element",
        "format": "docx",
        "element_id": "paragraph-12",
    }
    with pytest.raises(ValidationError, match="Fallback locator"):
        DocumentIR.model_validate(document_payload)
