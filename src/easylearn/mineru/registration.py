import hashlib
from contextlib import closing
from math import ceil, isclose
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

from easylearn.document_ir.schema import Sha256, SourceCoordinates
from easylearn.errors import DomainError
from easylearn.mineru.schema import MinerUBackend
from easylearn.previews.pdf import inspect_pdf
from easylearn.previews.schema import PreflightReport, PreflightRequest


class PdfRegistration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    preview: PreflightReport
    origin_sha256: Sha256
    method: Literal["identical_bytes", "rendered_pages"]
    render_edge_pixels: int
    page_render_sha256: tuple[Sha256, ...]
    coordinates: tuple[SourceCoordinates, ...]


def register_pdf(
    preview: PreflightRequest, *, origin_path: Path, origin: PreflightReport, backend: MinerUBackend
) -> PdfRegistration:
    """Child-only registration from verified input bytes and PDFium coordinate conversion."""
    report = inspect_pdf(preview)
    if len(report.pages) != len(origin.pages) or any(
        target.model_dump(exclude={"page_label"}) != source.model_dump(exclude={"page_label"})
        for target, source in zip(report.pages, origin.pages, strict=True)
    ):
        raise DomainError(
            "MINERU_PREVIEW_MISMATCH", "Origin page geometry differs from fixed preview"
        )
    coordinates = []
    hashes = []
    identical = report.sha256 == origin.sha256
    with pdfium.PdfDocument(preview.path) as document, pdfium.PdfDocument(origin_path) as upstream:
        document.init_forms()
        upstream.init_forms()
        for index in range(len(document)):
            with closing(document[index]) as page, closing(upstream[index]) as origin_page:
                if not identical:
                    scale = preview.limits.render_edge_pixels / max(page.get_size())
                    with (
                        closing(page.render(scale=scale)) as target_bitmap,
                        closing(origin_page.render(scale=scale)) as source_bitmap,
                        target_bitmap.to_pil() as target_image,
                        source_bitmap.to_pil() as source_image,
                    ):
                        target_hash = hashlib.sha256(target_image.tobytes()).hexdigest()
                        source_hash = hashlib.sha256(source_image.tobytes()).hexdigest()
                        if (
                            target_image.size != source_image.size
                            or target_image.mode != source_image.mode
                            or target_hash != source_hash
                        ):
                            raise DomainError(
                                "MINERU_PREVIEW_MISMATCH",
                                "Origin page rendering differs from preview",
                            )
                        hashes.append(target_hash)
                width, height = map(int, page.get_size())
                if min(width, height) <= 0:
                    raise DomainError(
                        "ADAPTER_COORDINATE_INVALID", "Invalid source page dimensions"
                    )
                device_width, device_height = width, height
                frame_width, frame_height = float(width), float(height)
                if backend == "pipeline":
                    # Fixed MinerU 3.4.5 pdf_reader.page_to_image / pipeline MagicModel contract.
                    scale = min(200 / 72, 3500 / max(page.get_size()))
                    device_width, device_height = (ceil(value * scale) for value in page.get_size())
                    frame_width, frame_height = device_width / scale, device_height / scale
                converter = pdfium.PdfPosConv(page, (0, 0, device_width, device_height, 0))
                left, low, right_edge, high = report.pages[index].crop_box
                corners = ((left, low), (left, high), (right_edge, low), (right_edge, high))
                # A float32 matrix's inverse error scales with the frame extent, even at zero.
                corner_tolerance = max(right_edge - left, high - low) * 2**-20
                points = []
                for device_point in ((0, 0), (device_width, 0), (0, device_height)):
                    converted = converter.to_page(*device_point)
                    matches = [
                        corner
                        for corner in corners
                        if all(
                            isclose(a, b, rel_tol=0, abs_tol=corner_tolerance)
                            for a, b in zip(corner, converted, strict=True)
                        )
                    ]
                    if len(matches) != 1:
                        raise DomainError(
                            "ADAPTER_COORDINATE_INVALID", "PDFium frame does not match CropBox"
                        )
                    # Stabilize PDFium's float matrix at exact, independently verified box corners.
                    points.append(matches[0])
                origin_point, right, bottom = points
                coordinates.append(
                    SourceCoordinates(
                        space="page_units",
                        size=(width, height),
                        origin="top_left",
                        rotation_applied=True,
                        crop_applied=True,
                        to_pdf=(
                            (right[0] - origin_point[0]) / frame_width,
                            (right[1] - origin_point[1]) / frame_width,
                            (bottom[0] - origin_point[0]) / frame_height,
                            (bottom[1] - origin_point[1]) / frame_height,
                            origin_point[0],
                            origin_point[1],
                        ),
                    )
                )
    return PdfRegistration(
        preview=report,
        origin_sha256=origin.sha256,
        method="identical_bytes" if identical else "rendered_pages",
        render_edge_pixels=preview.limits.render_edge_pixels,
        page_render_sha256=tuple(hashes),
        coordinates=tuple(coordinates),
    )
