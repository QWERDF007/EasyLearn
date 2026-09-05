import json
import math
import sys
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from pypdf.errors import PyPdfError

from easylearn.document_ir.schema import AssetDescriptor
from easylearn.errors import DomainError
from easylearn.execution import run_validation
from easylearn.images import ImageLimits
from easylearn.jobs.schema import JobFailure
from easylearn.mineru.adapter import MinerUAdapter, NormalizationContext
from easylearn.mineru.archive import JSON_ARTIFACT_KINDS, MinerUArchive
from easylearn.mineru.registration import PdfRegistration, register_pdf
from easylearn.mineru.schema import (
    MINERU_VALIDATION_TIMEOUT_SECONDS,
    MinerUArchiveLimits,
    MinerUArchiveManifest,
    MinerUOptions,
    MinerUTableLimits,
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


class ParseSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: UUID
    parse_run_id: UUID
    preview_asset_id: UUID
    preview: StoredObject


class NormalizedEvidence(ResultEvidence):
    registration: PdfRegistration
    document_ir: StoredObject


class ResultRequest(BaseModel):
    storage_root: Path
    archive: StoredObject
    options: MinerUOptions
    archive_limits: MinerUArchiveLimits
    image_limits: ImageLimits
    preview_limits: PreviewLimits
    source: ParseSource | None = None
    table_limits: MinerUTableLimits = Field(default_factory=MinerUTableLimits)


class MinerUResultValidator:
    def __init__(
        self,
        storage: LocalStorage,
        *,
        archive_limits: MinerUArchiveLimits,
        image_limits: ImageLimits,
        preview_limits: PreviewLimits,
        table_limits: MinerUTableLimits | None = None,
        timeout: float = MINERU_VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Result validation timeout must be finite and positive")
        self.storage = storage
        self.archive_limits = archive_limits
        self.image_limits = image_limits
        self.preview_limits = preview_limits
        self.table_limits = table_limits or MinerUTableLimits()
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

    async def normalize(
        self, archive: StoredObject, *, options: MinerUOptions, source: ParseSource
    ) -> NormalizedEvidence:
        return await run_validation(
            "easylearn.mineru.result",
            ResultRequest(
                storage_root=self.storage.root,
                archive=archive,
                options=options,
                archive_limits=self.archive_limits,
                image_limits=self.image_limits,
                preview_limits=self.preview_limits,
                table_limits=self.table_limits,
                source=source,
            ),
            NormalizedEvidence,
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


def normalize_result(
    request: ResultRequest, evidence: ResultEvidence, source: ParseSource
) -> NormalizedEvidence:
    storage = LocalStorage(request.storage_root)
    by_kind = {member.kind: member for member in evidence.manifest.members}
    preview_path = storage.path(source.preview.key)
    if preview_path.stat().st_size != source.preview.size:
        raise DomainError("STORAGE_CORRUPT", "Preview size does not match the input snapshot")
    registration = register_pdf(
        PreflightRequest(
            path=preview_path,
            expected_sha256=source.preview.sha256,
            limits=request.preview_limits,
        ),
        origin_path=storage.path(evidence.objects[by_kind["original"].path].key),
        origin=evidence.origin,
        backend=request.options.backend,
    )
    assets = {}
    for member in evidence.manifest.members:
        if member.image is not None:
            name = Path(member.path).name
            assets[name] = AssetDescriptor(
                asset_id=member.asset_id(source.parse_run_id),
                sha256=member.sha256,
                mime=member.image.mime,
                export_path=f"images/{name}",
            )
    raw = b"".join(storage.read(evidence.objects[by_kind["middle"].path].key))
    ir = MinerUAdapter().normalize(
        raw,
        context=NormalizationContext(
            document_id=source.document_id,
            parse_run_id=source.parse_run_id,
            preview_asset_id=source.preview_asset_id,
            preview=registration.preview,
            options=request.options,
            coordinates=registration.coordinates,
            assets=assets,
            table_limits=request.table_limits,
        ),
    )
    stored_ir = storage.write([ir.model_dump_json(exclude_computed_fields=True).encode()])
    return NormalizedEvidence(
        manifest=evidence.manifest,
        objects=evidence.objects,
        origin=evidence.origin,
        registration=registration,
        document_ir=stored_ir,
    )


def main() -> None:
    request = ResultRequest.model_validate_json(sys.stdin.buffer.read())
    result: ResultEvidence | JobFailure
    try:
        result = validate_result(request)
        if request.source is not None:
            result = normalize_result(request, result, request.source)
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
