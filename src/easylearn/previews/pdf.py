import asyncio
import hashlib
import math
import subprocess
import sys
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from easylearn.document_ir.schema import PageGeometry
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobFailure
from easylearn.previews.schema import PreflightReport, PreflightRequest, PreviewLimits

PreflightResponse: TypeAdapter[PreflightReport | JobFailure] = TypeAdapter(
    PreflightReport | JobFailure
)


class PdfPreflight:
    def __init__(self, limits: PreviewLimits, *, timeout: float = 120) -> None:
        if timeout <= 0:
            raise ValueError("Preflight timeout must be positive")
        self.limits = limits
        self.timeout = timeout

    async def inspect(self, path: Path, expected_sha256: str) -> PreflightReport:
        request = PreflightRequest(
            path=path.resolve(), expected_sha256=expected_sha256, limits=self.limits
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "easylearn.previews.pdf",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            async with asyncio.timeout(self.timeout):
                stdout, _ = await process.communicate(request.model_dump_json().encode())
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DomainError(
                "PREVIEW_TIMEOUT", "PDF validation exceeded time limit", retryable=True
            ) from None
        if process.returncode != 0:
            raise DomainError(
                "PREVIEW_PROCESS_FAILED", "PDF validation process failed", retryable=True
            )
        try:
            result = PreflightResponse.validate_json(stdout)
        except ValidationError:
            raise DomainError("PREVIEW_PROTOCOL_INVALID", "Invalid preflight response") from None
        if isinstance(result, JobFailure):
            raise DomainError(result.code, result.message, retryable=result.retryable)
        if result.sha256 != expected_sha256:
            raise DomainError("STORAGE_CORRUPT", "PDF checksum does not match the asset")
        return result


def inspect_pdf(request: PreflightRequest) -> PreflightReport:
    """Executed exclusively by the dedicated PDF child process."""
    from pypdf import PdfReader

    with request.path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != request.expected_sha256:
            raise DomainError("STORAGE_CORRUPT", "PDF checksum does not match the asset")
        source.seek(0)
        reader = PdfReader(source, strict=True)
        if reader.is_encrypted:
            raise DomainError("PREVIEW_PASSWORD_REQUIRED", "Encrypted PDF requires a password")
        count = len(reader.pages)
        if not 1 <= count <= request.limits.max_pages:
            raise DomainError("PREVIEW_PAGE_LIMIT", "PDF page count is outside allowed limits")
        geometries = []
        labels = reader.page_labels
        with pdfium.PdfDocument(request.path) as document:
            document.init_forms()
            if len(document) != count:
                raise DomainError("PREVIEW_GEOMETRY_MISMATCH", "PDF readers disagree on page count")
            for index, metadata in enumerate(reader.pages):
                geometry = PageGeometry.model_validate(
                    {
                        "page_index": index,
                        "media_box": tuple(float(v) for v in metadata.mediabox),
                        "crop_box": tuple(float(v) for v in metadata.cropbox),
                        "intrinsic_rotation": metadata.rotation % 360,
                        "user_unit": metadata.user_unit,
                        "page_label": labels[index],
                    }
                )
                dimensions = (
                    (geometry.media_box[2] - geometry.media_box[0]) * geometry.user_unit,
                    (geometry.media_box[3] - geometry.media_box[1]) * geometry.user_unit,
                )
                if max(dimensions) > request.limits.max_page_points:
                    raise DomainError("PREVIEW_PAGE_SIZE_LIMIT", "PDF page exceeds size limit")
                with closing(document[index]) as page:
                    if page.get_rotation() != geometry.intrinsic_rotation or not all(
                        math.isclose(actual, declared, abs_tol=0.01)
                        for actual, declared in zip(page.get_bbox(), geometry.crop_box, strict=True)
                    ):
                        raise DomainError(
                            "PREVIEW_GEOMETRY_MISMATCH", "PDF readers disagree on page geometry"
                        )
                    edge = max(page.get_size())
                    if not math.isfinite(edge) or edge <= 0:
                        raise DomainError("PREVIEW_INVALID", "PDF page is not renderable")
                    with closing(
                        page.render(scale=request.limits.render_edge_pixels / edge)
                    ) as bitmap:
                        if bitmap.width <= 0 or bitmap.height <= 0:
                            raise DomainError("PREVIEW_INVALID", "PDF page rendered empty")
                geometries.append(geometry)
        source.seek(0)
        if hashlib.file_digest(source, "sha256").hexdigest() != digest:
            raise DomainError("STORAGE_CORRUPT", "PDF changed during validation")
        return PreflightReport(
            sha256=digest,
            pages=tuple(geometries),
            renderer=f"pypdfium2/{version('pypdfium2')};pdfium/{pdfium.PDFIUM_INFO}",
            metadata_reader=f"pypdf/{version('pypdf')}",
        )


def main() -> None:
    from pypdf.errors import PyPdfError

    request = PreflightRequest.model_validate_json(sys.stdin.buffer.read())
    result: PreflightReport | JobFailure
    try:
        result = inspect_pdf(request)
    except DomainError as exc:
        result = JobFailure(code=exc.code, message=exc.message, retryable=exc.retryable)
    except (PyPdfError, pdfium.PdfiumError, ValidationError, ValueError, OverflowError):
        result = JobFailure(
            code="PREVIEW_INVALID", message="PDF cannot be validated or rendered", retryable=False
        )
    except OSError:
        result = JobFailure(
            code="PREVIEW_IO_FAILED", message="PDF asset could not be read", retryable=True
        )
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
