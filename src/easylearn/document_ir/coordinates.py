from math import isfinite
from typing import Literal, Self

from pydantic import model_validator

from easylearn.document_ir.schema import (
    BBox,
    FrozenModel,
    PageGeometry,
    SourceRegion,
    Transform,
)
from easylearn.errors import DomainError


class SourceCoordinates(FrozenModel):
    space: Literal["page_units", "normalized_1000", "normalized_01", "image_pixels"]
    size: tuple[float, float]
    origin: Literal["top_left", "bottom_left"]
    rotation_applied: bool
    crop_applied: bool
    to_pdf: Transform

    @model_validator(mode="after")
    def validate_transform(self) -> Self:
        if min(self.size) <= 0:
            raise ValueError("Source dimensions must be positive")
        a, b, c, d, _, _ = self.to_pdf
        if abs(a * d - b * c) < 1e-12:
            raise ValueError("Coordinate transform must be invertible")
        return self


class CoordinateMapper:
    def __init__(self, page: PageGeometry, source: SourceCoordinates) -> None:
        self.page = page
        self.source = source

    def map_bbox(self, bbox: BBox) -> SourceRegion:
        source = self.source
        if not all(isfinite(value) for value in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise DomainError("ADAPTER_COORDINATE_INVALID", "Invalid source region")
        sx, sy = (1.0, 1.0)
        if source.space in ("normalized_1000", "normalized_01"):
            divisor = 1000 if source.space == "normalized_1000" else 1
            sx, sy = source.size[0] / divisor, source.size[1] / divisor
        a, b, c, d, e, f = source.to_pdf
        x0, y0, x1, y1 = bbox
        polygon = tuple(
            (a * x * sx + c * y * sy + e, b * x * sx + d * y * sy + f)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        )
        left = min(p[0] for p in polygon)
        right = max(p[0] for p in polygon)
        bottom = min(p[1] for p in polygon)
        top = max(p[1] for p in polygon)
        cx0, cy0, cx1, cy1 = self.page.crop_box
        width, height = cx1 - cx0, cy1 - cy0
        if left < cx0 or right > cx1 or bottom < cy0 or top > cy1:
            raise DomainError("ADAPTER_COORDINATE_INVALID", "Region outside preview CropBox")
        return SourceRegion(
            page_index=self.page.page_index,
            bbox_pdf=(left, bottom, right, top),
            bbox_norm=(
                (left - cx0) / width,
                (cy1 - top) / height,
                (right - cx0) / width,
                (cy1 - bottom) / height,
            ),
            polygon_pdf=(polygon[0], polygon[1], polygon[2], polygon[3]),
            source_bbox=bbox,
            source_to_pdf_transform=(a * sx, b * sx, c * sy, d * sy, e, f),
        )
