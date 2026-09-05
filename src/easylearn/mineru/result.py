import json
import math
import sys
from pathlib import Path
from zipfile import ZipFile

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from pypdf.errors import PyPdfError

from easylearn.errors import DomainError
from easylearn.execution import run_validation
from easylearn.images import ImageLimits
from easylearn.jobs.schema import JobFailure
from easylearn.mineru.archive import JSON_ARTIFACT_KINDS, MinerUArchive
from easylearn.mineru.schema import (
    MINERU_VALIDATION_TIMEOUT_SECONDS,
    MinerUArchiveLimits,
    MinerUArchiveManifest,
    MinerUOptions,
)
from easylearn.paths import PortablePath
from easylearn.previews.pdf import inspect_pdf
from easylearn.previews.schema import PreflightReport, PreflightRequest, PreviewLimits
from easylearn.storage import LocalStorage, StoredObject


class ResultEvidence(BaseModel):
    """Verified raw artifacts, not a normalized or published parse run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: MinerUArchiveManifest
    objects: dict[PortablePath, StoredObject]
    origin: PreflightReport


class ResultRequest(BaseModel):
    storage_root: Path
    archive: StoredObject
    options: MinerUOptions
    archive_limits: MinerUArchiveLimits
    image_limits: ImageLimits
    preview_limits: PreviewLimits


class MinerUResultValidator:
    def __init__(
        self,
        storage: LocalStorage,
        *,
        archive_limits: MinerUArchiveLimits,
        image_limits: ImageLimits,
        preview_limits: PreviewLimits,
        timeout: float = MINERU_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Result validation timeout must be finite and positive")
        self.storage = storage
        self.archive_limits = archive_limits
        self.image_limits = image_limits
        self.preview_limits = preview_limits
        self.timeout = timeout

    async def inspect(self, archive: StoredObject, *, options: MinerUOptions) -> ResultEvidence:
        return await run_validation(
            "easylearn.mineru.result",
            ResultRequest(
                storage_root=self.storage.root,
                archive=archive,
                options=options,
                archive_limits=self.archive_limits,
                image_limits=self.image_limits,
                preview_limits=self.preview_limits,
            ),
            ResultEvidence,
            timeout=self.timeout,
            error_prefix="MINERU_RESULT",
        )


def validate_result(request: ResultRequest) -> ResultEvidence:
    """Child-only decoding. CAS writes remain unreferenced until fenced DB publication."""
    storage = LocalStorage(request.storage_root)
    path = storage.path(request.archive.key)
    manifest = MinerUArchive(
        limits=request.archive_limits, image_limits=request.image_limits
    ).inspect(path, options=request.options)
    if (manifest.sha256, manifest.size) != (request.archive.sha256, request.archive.size):
        raise DomainError("STORAGE_CORRUPT", "Result archive does not match the stored object")
    objects = {}
    with ZipFile(path) as archive:
        for member in manifest.members:
            with archive.open(member.path) as source:
                stored = storage.write(iter(lambda: source.read(1024 * 1024), b""))
            if (stored.sha256, stored.size) != (member.sha256, member.size):
                raise DomainError("STORAGE_CORRUPT", "Result member changed during validation")
            objects[member.path] = stored
            if member.kind in JSON_ARTIFACT_KINDS:
                try:
                    with storage.path(stored.key).open("r", encoding="utf-8") as text:
                        value = json.load(text, object_pairs_hook=unique_json_object)
                    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, UnicodeError, RecursionError):
                    raise DomainError(
                        "MINERU_RESULT_JSON_INVALID", "Invalid or ambiguous result JSON"
                    ) from None
    original = next(member for member in manifest.members if member.kind == "original")
    origin = inspect_pdf(
        PreflightRequest(
            path=storage.path(objects[original.path].key),
            expected_sha256=original.sha256,
            limits=request.preview_limits,
        )
    )
    if len(origin.pages) != request.options.page_count:
        raise DomainError("MINERU_PROTOCOL_MISMATCH", "Origin PDF page count differs from input")
    return ResultEvidence(manifest=manifest, objects=objects, origin=origin)


def unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """json object_pairs_hook rejects duplicate keys before dictionary construction."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return result


def main() -> None:
    request = ResultRequest.model_validate_json(sys.stdin.buffer.read())
    result: ResultEvidence | JobFailure
    try:
        result = validate_result(request)
    except DomainError as exc:
        result = JobFailure(code=exc.code, message=exc.message, retryable=exc.retryable)
    except (PyPdfError, pdfium.PdfiumError, ValidationError, ValueError, OverflowError):
        result = JobFailure(
            code="MINERU_RESULT_INVALID",
            message="Result artifacts cannot be validated",
            retryable=False,
        )
    except OSError:
        result = JobFailure(
            code="MINERU_RESULT_IO_FAILED",
            message="Result assets could not be accessed",
            retryable=True,
        )
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
