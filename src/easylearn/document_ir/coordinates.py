from math import isfinite

from easylearn.document_ir.schema import (
    BBox,
    PageGeometry,
    SourceCoordinates,
    SourceRegion,
)
from easylearn.errors import DomainError


class CoordinateMapper:
    def __init__(self, page: PageGeometry, source: SourceCoordinates) -> None:
        self.page = page
        self.source = source

    def map_bbox(self, bbox: BBox) -> SourceRegion:
        source = self.source
        if not all(isfinite(value) for value in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise DomainError("ADAPTER_COORDINATE_INVALID", "Invalid source region")
        a, b, c, d, e, f = source.effective_transform
        x0, y0, x1, y1 = bbox
        polygon = tuple(
            (a * x + c * y + e, b * x + d * y + f)
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
            source_to_pdf_transform=source.effective_transform,
            source_coordinates=source,
        )
