import pytest

from easylearn.document_ir.coordinates import CoordinateMapper, SourceCoordinates
from easylearn.document_ir.schema import PageGeometry
from easylearn.errors import DomainError


@pytest.mark.parametrize(
    ("space", "bbox"),
    [
        ("normalized_1000", (100, 200, 300, 400)),
        ("normalized_01", (0.1, 0.2, 0.3, 0.4)),
        ("page_units", (60, 160, 180, 320)),
        ("image_pixels", (60, 160, 180, 320)),
    ],
)
def test_declared_coordinate_spaces_project_to_same_nonzero_cropbox(space, bbox):
    page = PageGeometry(page_index=0, media_box=(0, 0, 620, 840), crop_box=(10, 20, 610, 820))
    source = SourceCoordinates(
        space=space,
        size=(600, 800),
        origin="top_left",
        rotation_applied=False,
        crop_applied=True,
        to_pdf=(1, 0, 0, -1, 10, 820),
    )

    region = CoordinateMapper(page, source).map_bbox(bbox)

    assert region.bbox_pdf == pytest.approx((70, 500, 190, 660))
    assert region.bbox_norm == pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert region.page_index == 0


@pytest.mark.parametrize(
    "bbox",
    [
        (0, 0, 0, 10),
        (10, 0, 0, 10),
        (0, 0, float("nan"), 10),
        (0, 0, float("inf"), 10),
        (-10, 0, 100, 100),
        (0, 0, 101, 100),
    ],
)
def test_invalid_regions_are_rejected_instead_of_published(bbox):
    page = PageGeometry(page_index=0, media_box=(0, 0, 100, 100), crop_box=(0, 0, 100, 100))
    source = SourceCoordinates(
        space="page_units",
        size=(100, 100),
        origin="top_left",
        rotation_applied=False,
        crop_applied=True,
        to_pdf=(1, 0, 0, -1, 0, 100),
    )

    with pytest.raises(DomainError, match="ADAPTER_COORDINATE_INVALID"):
        CoordinateMapper(page, source).map_bbox(bbox)


def test_rotation_restoration_transforms_all_four_corners():
    page = PageGeometry(page_index=1, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800))
    source = SourceCoordinates(
        space="page_units",
        size=(800, 600),
        origin="top_left",
        rotation_applied=True,
        crop_applied=True,
        to_pdf=(0, 1, 1, 0, 0, 0),
    )

    region = CoordinateMapper(page, source).map_bbox((100, 200, 300, 400))

    assert region.polygon_pdf == ((200, 100), (200, 300), (400, 300), (400, 100))
    assert region.bbox_pdf == (200, 100, 400, 300)


@pytest.mark.parametrize("matrix", [(1, 0, 0, 0, 0, 0), (1, 0, 0, float("nan"), 0, 0)])
def test_coordinate_declarations_cannot_contain_singular_or_nonfinite_transforms(matrix):
    with pytest.raises(ValueError):
        SourceCoordinates(
            space="page_units",
            size=(100, 100),
            origin="bottom_left",
            rotation_applied=False,
            crop_applied=False,
            to_pdf=matrix,
        )
