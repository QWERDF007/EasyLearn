import pytest
from pydantic import ValidationError

from easylearn.document_ir.schema import DocumentIR


@pytest.mark.parametrize("indices", [[0, 0], [1], [0, 2], [1, 0]])
def test_snapshot_requires_complete_ordered_page_identity(document_payload, indices):
    document_payload["pages"] = [
        {**document_payload["pages"][0], "page_index": index} for index in indices
    ]
    with pytest.raises(ValidationError, match="Page indices"):
        DocumentIR.model_validate(document_payload)


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"page_index": 1}, "Unknown region page"),
        ({"bbox_norm": [0, 0, 1, 1]}, "Normalized region"),
        ({"bbox_pdf": [-1, 700, 100, 800]}, "Region bounds"),
        ({"polygon_pdf": [[0, 800], [0, 800], [0, 800], [0, 800]]}, "Region polygon"),
        ({"source_to_pdf_transform": [1, 0, 0, -1, 20, 800]}, "Region transform"),
    ],
)
def test_snapshot_rejects_geometry_that_cannot_be_reproduced(document_payload, changes, reason):
    region = {
        "page_index": 0,
        "bbox_pdf": [0, 700, 100, 800],
        "bbox_norm": [0, 0, 1 / 6, 0.125],
        "polygon_pdf": [[0, 800], [100, 800], [100, 700], [0, 700]],
        "source_bbox": [0, 0, 100, 100],
        "source_to_pdf_transform": [1, 0, 0, -1, 0, 800],
        "source_coordinates": {
            "space": "page_units",
            "size": [600, 800],
            "origin": "top_left",
            "rotation_applied": False,
            "crop_applied": True,
            "to_pdf": [1, 0, 0, -1, 0, 800],
        },
        **changes,
    }
    document_payload["blocks"] = [
        {
            "block_id": "p1",
            "block_type": "paragraph",
            "order_index": 0,
            "source_regions": [region],
        }
    ]
    with pytest.raises(ValidationError, match=reason):
        DocumentIR.model_validate(document_payload)
