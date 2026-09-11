from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.files import DocumentFiles
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.mineru.models import MinerUModelCatalog
from easylearn.mineru.result import ParseSource
from easylearn.mineru.schema import MinerUOptions
from easylearn.parsing.backend import ParseBackendExecutor
from easylearn.parsing.normalizer import ParseResultNormalizer
from easylearn.parsing.preflight import (
    ParsePreflight,
    _normalise_print_range,
    _prepare_xlsx_selection,
    _sha256_file,
)
from easylearn.parsing.publisher import ParsePublisher
from easylearn.persistence.documents import DocumentStore
from easylearn.publication import ArtifactPublisher
from easylearn.storage import LocalStorage, StoredObject
from easylearn.tasks import TaskContext, TaskManager, TaskRecord

logger = logging.getLogger(__name__)


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            yield chunk


class OfficeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sheet: str | None = Field(default=None, min_length=1, max_length=255)
    print_range: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("print_range")
    @classmethod
    def validate_print_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_print_range(value)


def _parse_office_scope(value: object) -> OfficeSelection | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DomainError("OFFICE_SELECTION_INVALID", "Office selection is invalid")
    try:
        selection = OfficeSelection.model_validate(value)
    except ValueError as exc:
        raise DomainError("OFFICE_SELECTION_INVALID", "Office selection is invalid") from exc
    if selection.sheet is None and selection.print_range is None:
        return None
    return selection


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    options: dict[str, Any] | None = None
    office: OfficeSelection | None = None
    auto_translate: bool = False


class ParseService:
    """Coordinates parsing across input preflight, backend execution, normalization, and publication."""

    def __init__(
        self,
        database: Database,
        files: DocumentFiles,
        documents: DocumentService,
        manager: TaskManager,
        settings: Settings,
        publisher: ArtifactPublisher | None = None,
        *,
        preflight: ParsePreflight | None = None,
        backend: ParseBackendExecutor | None = None,
        normalizer: ParseResultNormalizer | None = None,
        parse_publisher: ParsePublisher | None = None,
        document_store: DocumentStore | None = None,
    ) -> None:
        self.database = database
        self.files = files
        self.documents = documents
        self.manager = manager
        self.settings = settings
        self.document_store = document_store or (DocumentStore(database) if database is not None else None)
        self.model_catalog = MinerUModelCatalog(settings.mineru)
        self.preflight = preflight or ParsePreflight(settings)
        self.backend = backend or ParseBackendExecutor(settings)
        self.mineru = self.backend.mineru
        self.normalizer = normalizer or ParseResultNormalizer(settings)
        art_pub = publisher or (ArtifactPublisher(database, files) if database is not None and files is not None else None)
        self.parse_publisher = parse_publisher or (
            ParsePublisher(
                document_store=self.document_store,
                files=files,
                publisher=art_pub,
                settings=settings,
                manager=manager,
            )
            if self.document_store is not None and files is not None and art_pub is not None
            else None
        )

    async def close(self) -> None:
        await self.backend.close()

    async def submit(self, document_id: UUID, request: ParseRequest | None = None) -> TaskView:
        await self.documents.get(document_id)
        options = self.settings.mineru.parse
        if request is not None and request.options is not None:
            try:
                options = options.__class__.model_validate(request.options)
            except ValueError as exc:
                raise DomainError("PARSE_OPTIONS_INVALID", "Invalid MinerU parse options") from exc
        self.mineru.validate_options(MinerUOptions(**options.model_dump(), page_count=1))
        office = request.office if request is not None else None
        requested_model_id = request.model_id if request is not None else None
        if requested_model_id is not None:
            self.model_catalog.resolve(requested_model_id)
        model_id = (
            self.model_catalog.default_model_id
            if requested_model_id is None
            else requested_model_id
        )

        async def admit() -> None:
            await self.documents.get(document_id)

        task_view = await self.manager.submit(
            document_id,
            JobKind.PARSE,
            {
                "parse_id": str(uuid4()),
                "model_id": model_id,
                "options": options.model_dump(mode="json"),
                "office": office.model_dump(mode="json") if office is not None else None,
                "auto_translate": request.auto_translate if request is not None else False,
            },
            admission=admit,
        )
        logger.info(
            "Parse task submitted: document_id=%s, task_id=%s, model_id=%s",
            document_id,
            task_view.task_id,
            model_id,
        )
        return task_view

    async def execute(self, record: TaskRecord, context: TaskContext) -> dict[str, JsonValue]:
        parse_id = UUID(str(record.scope["parse_id"]))
        if self.document_store is not None:
            already_recorded = await self.document_store.get_parse(record.document_id, parse_id)
            if already_recorded is not None:
                logger.info(
                    "Parse %s for document %s is already recorded; skipping duplicate execution",
                    parse_id,
                    record.document_id,
                )
                return {
                    "parse_id": str(parse_id),
                    "file_id": f"ir:{parse_id}",
                }

        options = self.settings.mineru.parse.__class__.model_validate(record.scope["options"])
        office = _parse_office_scope(record.scope.get("office"))
        logger.info(
            "Parse started: document_id=%s, parse_id=%s, model=%s",
            record.document_id,
            parse_id,
            record.scope.get("model_id"),
        )
        task_directory = self.files.paths.task(record.task_id)
        task_directory.mkdir(parents=True, exist_ok=False)
        try:
            # 1. Preflight
            original = await self.documents.file_path(record.document_id, "original")
            input_pdf = task_directory / "input.pdf"
            await context.progress(None, "Preparing PDF preview")
            await self._prepare_pdf(original, input_pdf, context, office)
            preview_sha256, report = await self.preflight.inspect_preview(input_pdf)
            logger.info(
                "PDF preflight completed: document_id=%s, pages=%d",
                record.document_id,
                len(report.pages),
            )
            await context.progress(0.15, "Preview is ready")

            # 2. Backend Execution
            miner_options = MinerUOptions(
                **options.model_dump(),
                page_count=len(report.pages),
            )
            cas = LocalStorage(task_directory / "cas")
            preview_object = await run_blocking(cas.write, _file_chunks(input_pdf))
            archive = await self._run_mineru(
                input_pdf,
                task_directory,
                cas,
                miner_options,
                context,
            )

            # 3. Normalization
            await context.progress(0.55, "Validating MinerU result")
            source = ParseSource(
                document_id=record.document_id,
                parse_run_id=parse_id,
                preview_asset_id=uuid5(NAMESPACE_URL, f"{record.document_id}:preview"),
                preview=preview_object,
            )
            from easylearn.mineru.result import MinerUResultValidator

            evidence = await MinerUResultValidator(
                cas,
                archive_limits=self.settings.mineru.archive_limits,
                image_limits=self.settings.mineru.image_limits,
                preview_limits=self.settings.mineru.preview_limits,
                table_limits=self.settings.mineru.table_limits,
                timeout=self.settings.mineru.timeout_seconds,
            ).normalize(
                archive,
                options=miner_options,
                source=source,
            )
            logger.info(
                "MinerU parsing completed: document_id=%s, objects=%d",
                record.document_id,
                len(evidence.objects),
            )

            # 4. Publication
            await context.progress(0.8, "Publishing parsed document")
            await context.check()
            final_directory = task_directory / "published"
            total_units = await run_blocking(
                self._build_published_directory,
                final_directory,
                input_pdf,
                cas,
                archive,
                evidence,
            )
            assert self.parse_publisher is not None
            result_ref = await self.parse_publisher.publish(
                document_id=record.document_id,
                parse_id=parse_id,
                task_id=record.task_id,
                report=report,
                evidence=evidence,
                staging_directory=final_directory,
                total_units=total_units,
                office=office,
                context=context,
            )

            if record.scope.get("auto_translate") is True:
                await context.enqueue_after_success(
                    JobKind.TRANSLATE,
                    {"parse_id": str(parse_id), "block_ids": None},
                )
            try:
                await self.parse_publisher.prune(record.document_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Cleanup cannot invalidate an already published parse.
                pass
            return result_ref
        except BaseException as exc:
            logger.error(
                "Parse failed: document_id=%s, parse_id=%s, error=%s",
                record.document_id,
                parse_id,
                exc,
            )
            raise
        finally:
            await run_blocking(self.files.remove_task_directory, record.task_id)

    # Legacy helper wrappers for existing test suites
    async def _prepare_pdf(
        self,
        original: Path,
        destination: Path,
        context: TaskContext,
        office: OfficeSelection | None = None,
    ) -> None:
        await self.preflight.prepare_input(original, destination, context, office)

    async def _run_mineru(
        self,
        input_pdf: Path,
        task_directory: Path,
        cas: LocalStorage,
        options: MinerUOptions,
        context: TaskContext,
    ) -> StoredObject:
        res = await self.backend.execute(
            input_pdf=input_pdf,
            task_directory=task_directory,
            cas=cas,
            options=options,
            context=context,
            model_id=context.record.scope.get("model_id"),
        )
        return res.archive_object

    def _package_output(
        self, output: Path, archive_path: Path, options: MinerUOptions, input_pdf: Path
    ) -> None:
        self.backend.package_output(output, archive_path, options, input_pdf)

    def _build_published_directory(
        self,
        directory: Path,
        input_pdf: Path,
        cas: LocalStorage,
        archive: StoredObject,
        evidence: Any,
    ) -> int:
        return self.normalizer.build_published_directory(
            directory, input_pdf, cas, archive, evidence
        )

    async def _prune(self, document_id: UUID) -> None:
        if self.parse_publisher is not None:
            await self.parse_publisher.prune(document_id)
