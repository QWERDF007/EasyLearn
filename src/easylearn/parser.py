from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.document_ir.schema import DocumentIR
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.files import DocumentFiles
from easylearn.images import inspect_image
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.mineru.archive import result_root
from easylearn.mineru.embedded import EmbeddedMinerU
from easylearn.mineru.models import MinerUModelCatalog
from easylearn.mineru.result import MinerUResultValidator, NormalizedEvidence, ParseSource
from easylearn.mineru.schema import MinerUOptions
from easylearn.previews.pdf import PdfPreflight
from easylearn.storage import LocalStorage, StoredObject
from easylearn.tasks import TaskContext, TaskManager, TaskRecord
from easylearn.translation import translation_units

logger = logging.getLogger(__name__)


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


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    options: dict[str, Any] | None = None
    office: OfficeSelection | None = None
    auto_translate: bool = False


class ParseService:
    def __init__(
        self,
        database: Database,
        files: DocumentFiles,
        documents: DocumentService,
        manager: TaskManager,
        settings: Settings,
    ) -> None:
        self.database = database
        self.files = files
        self.documents = documents
        self.manager = manager
        self.settings = settings
        self.model_catalog = MinerUModelCatalog(settings.mineru)
        self.mineru = EmbeddedMinerU(
            self.model_catalog.default_model_path,
            timeout=settings.mineru.timeout_seconds,
        )

    async def close(self) -> None:
        await self.mineru.close()

    async def submit(self, document_id: UUID, request: ParseRequest | None = None) -> TaskView:
        await self.documents.get(document_id)
        options = self.settings.mineru.parse
        if request is not None and request.options is not None:
            try:
                options = options.__class__.model_validate(request.options)
            except ValueError as exc:
                raise DomainError("PARSE_OPTIONS_INVALID", "Invalid MinerU parse options") from exc
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
            original = await self.documents.file_path(record.document_id, "original")
            input_pdf = task_directory / "input.pdf"
            await context.progress(None, "Preparing PDF preview")
            await self._prepare_pdf(original, input_pdf, context, office)
            preview_sha256 = await run_blocking(_sha256_file, input_pdf)
            report = await PdfPreflight(
                self.settings.mineru.preview_limits, timeout=self.settings.mineru.timeout_seconds
            ).inspect(input_pdf, preview_sha256)
            logger.info(
                "PDF preflight completed: document_id=%s, pages=%d",
                record.document_id,
                len(report.pages),
            )
            await context.progress(0.15, "Preview is ready")
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
            await context.progress(0.55, "Validating MinerU result")
            evidence: NormalizedEvidence = await MinerUResultValidator(
                cas,
                archive_limits=self.settings.mineru.archive_limits,
                image_limits=self.settings.mineru.image_limits,
                preview_limits=self.settings.mineru.preview_limits,
                table_limits=self.settings.mineru.table_limits,
                timeout=self.settings.mineru.timeout_seconds,
            ).normalize(
                archive,
                options=miner_options,
                source=ParseSource(
                    document_id=record.document_id,
                    parse_run_id=parse_id,
                    preview_asset_id=uuid5(NAMESPACE_URL, f"{record.document_id}:preview"),
                    preview=preview_object,
                ),
            )
            logger.info(
                "MinerU parsing completed: document_id=%s, objects=%d",
                record.document_id,
                len(evidence.objects),
            )
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
            destination = self.files.paths.parse(record.document_id, parse_id)
            relative_root = destination.relative_to(self.files.paths.document(record.document_id))
            metadata = {
                "preview": report.model_dump(mode="json"),
                "mineru": evidence.manifest.model_dump(mode="json"),
                "total_units": total_units,
            }
            if office is not None:
                metadata["office"] = office.model_dump(mode="json")
            result_ref: dict[str, JsonValue] = {
                "parse_id": str(parse_id),
                "file_id": f"ir:{parse_id}",
            }

            async def publish() -> None:
                self.files.publish_directory(final_directory, destination)
                async with self.database.transaction() as connection:
                    exists = await (
                        await connection.execute(
                            "SELECT 1 FROM documents WHERE id = ?", (str(record.document_id),)
                        )
                    ).fetchone()
                    if exists is None:
                        raise DomainError(
                            "DOCUMENT_NOT_FOUND",
                            "Document was deleted during parsing",
                            status=404,
                        )
                    timestamp = _now()
                    await connection.execute(
                        "INSERT INTO parse_results "
                        "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, "
                        "created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(parse_id),
                            str(record.document_id),
                            str(relative_root / "preview.pdf"),
                            str(relative_root / "document.json"),
                            str(relative_root / "mineru.zip"),
                            len(report.pages),
                            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                            timestamp,
                        ),
                    )
                    await connection.execute(
                        "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                        (str(parse_id), str(record.document_id)),
                    )

            await context.publish(result_ref, publish)
            logger.info(
                "Parse published: document_id=%s, parse_id=%s, pages=%d",
                record.document_id,
                parse_id,
                len(report.pages),
            )
            if record.scope.get("auto_translate") is True:
                await context.enqueue_after_success(
                    JobKind.TRANSLATE,
                    {"parse_id": str(parse_id), "block_ids": None},
                )
            try:
                await self._prune(record.document_id)
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

    async def _run_mineru(
        self,
        input_pdf: Path,
        task_directory: Path,
        cas: LocalStorage,
        options: MinerUOptions,
        context: TaskContext,
    ) -> StoredObject:
        output = task_directory / "mineru-output"
        output.mkdir()
        model_id = context.record.scope.get("model_id")
        model_path = self.model_catalog.resolve(model_id if isinstance(model_id, str) else None)
        await context.progress(0.15, "MinerU 正在推理")
        await self.mineru.parse(
            input_pdf,
            output,
            options,
            check=context.check,
            model_path=model_path,
        )
        archive_path = task_directory / "mineru.zip"
        self._package_output(output, archive_path, options, input_pdf)
        return await run_blocking(cas.write, _file_chunks(archive_path))

    async def _prepare_pdf(
        self,
        original: Path,
        destination: Path,
        context: TaskContext,
        office: OfficeSelection | None = None,
    ) -> None:
        await context.check()
        suffix = original.suffix.lower()
        if suffix == ".pdf":
            if office is not None:
                raise DomainError(
                    "OFFICE_SELECTION_INVALID", "Office selection requires an XLSX input"
                )
            await run_blocking(shutil.copyfile, original, destination)
            return
        if suffix in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".bmp",
            ".gif",
            ".jp2",
            ".tif",
            ".tiff",
        }:
            if office is not None:
                raise DomainError(
                    "OFFICE_SELECTION_INVALID", "Office selection requires an XLSX input"
                )
            from PIL import Image, ImageOps

            with original.open("rb") as source:
                await run_blocking(
                    inspect_image,
                    source,
                    expected_suffix=suffix,
                    limits=self.settings.mineru.image_limits,
                )

            def convert() -> None:
                with Image.open(original) as image:
                    normalized = ImageOps.exif_transpose(image)
                    try:
                        normalized.convert("RGB").save(destination, "PDF", resolution=150.0)
                    finally:
                        if normalized is not image:
                            normalized.close()

            await run_blocking(convert)
            return
        if self.settings.extensions.office_enabled:
            await self._convert_office(original, destination, context, office)
            return
        raise DomainError("OFFICE_CONVERTER_REQUIRED", "Office conversion is disabled", status=422)

    async def _convert_office(
        self,
        original: Path,
        destination: Path,
        context: TaskContext,
        selection: OfficeSelection | None = None,
    ) -> None:
        output = destination.parent / "office-output"
        output.mkdir()
        conversion_input = original
        if selection is not None:
            if original.suffix.lower() != ".xlsx":
                raise DomainError(
                    "OFFICE_SELECTION_INVALID", "Office selection requires an XLSX input"
                )
            conversion_input = output / original.name
            await run_blocking(_prepare_xlsx_selection, original, conversion_input, selection)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.settings.extensions.office_command,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output),
                str(conversion_input),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            raise DomainError(
                "OFFICE_CONVERTER_UNAVAILABLE",
                "LibreOffice is not available",
                retryable=True,
            ) from None
        try:
            async with asyncio.timeout(self.settings.mineru.timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            await _terminate_process_tree(process)
            raise DomainError(
                "OFFICE_CONVERSION_TIMEOUT",
                "Office conversion exceeded the task time limit",
                retryable=True,
            ) from None
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            raise
        await context.check()
        converted = output / f"{conversion_input.stem}.pdf"
        if process.returncode != 0 or not converted.is_file():
            detail = (stderr or stdout).decode(errors="replace")[-1000:]
            raise DomainError("OFFICE_CONVERSION_FAILED", detail or "Office conversion failed")
        await run_blocking(shutil.copyfile, converted, destination)

    def _package_output(
        self, output: Path, archive_path: Path, options: MinerUOptions, input_pdf: Path
    ) -> None:
        middle = next(output.rglob("input_middle.json"), None)
        if middle is None:
            raise DomainError("MINERU_RESULT_INVALID", "MinerU output is missing input_middle.json")
        root = middle.parent
        archive_root = result_root(options)
        with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
            names: set[str] = set()
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                name = str(archive_root / PurePosixPath(path.relative_to(root).as_posix()))
                names.add(name.casefold())
                archive.write(path, name)
            original_name = str(archive_root / "input_origin.pdf")
            if original_name.casefold() not in names:
                archive.write(input_pdf, original_name)

    def _build_published_directory(
        self,
        directory: Path,
        input_pdf: Path,
        cas: LocalStorage,
        archive: StoredObject,
        evidence: NormalizedEvidence,
    ) -> int:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "images").mkdir()
        shutil.copyfile(input_pdf, directory / "preview.pdf")
        shutil.copyfile(cas.path(archive.key), directory / "mineru.zip")
        shutil.copyfile(cas.path(evidence.document_ir.key), directory / "document.json")
        ir = DocumentIR.model_validate_json(cas.path(evidence.document_ir.key).read_bytes())
        total_units = len(translation_units(ir))
        for asset in ir.assets:
            member = next(
                member
                for member in evidence.manifest.members
                if member.asset_id(ir.parse_run_id) == asset.asset_id
            )
            source = evidence.objects[member.path]
            destination = directory / asset.export_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cas.path(source.key), destination)
        return total_units

    async def _prune(self, document_id: UUID) -> None:
        await self.manager.run_exclusive(lambda: self._prune_locked(document_id))

    async def _prune_locked(self, document_id: UUID) -> None:
        keep = self.settings.files.keep_parse_versions
        protected = self.manager.active_parse_ids(document_id)
        async with self.database.read() as connection:
            rows = tuple(await (
                await connection.execute(
                    "SELECT id FROM parse_results WHERE document_id = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (str(document_id),),
                )
            ).fetchall())
            referenced = {
                row[0]
                for row in await (
                    await connection.execute(
                        "SELECT DISTINCT parse_id FROM qa_records WHERE document_id = ?",
                        (str(document_id),),
                    )
                ).fetchall()
            }
        old = [
            row[0]
            for row in rows[keep:]
            if row[0] not in referenced and UUID(row[0]) not in protected
        ]
        if not old:
            return
        async with self.database.transaction() as connection:
            for parse_id in old:
                await connection.execute("DELETE FROM parse_results WHERE id = ?", (parse_id,))
        for parse_id in old:
            await run_blocking(
                shutil.rmtree, self.files.paths.parse(document_id, UUID(parse_id)), True
            )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
                yield chunk


_A1_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _normalise_print_range(value: str) -> str:
    cells = value.replace(" ", "").split(":")
    if len(cells) not in (1, 2):
        raise ValueError("print_range must contain one or two A1 cells")
    normalised: list[str] = []
    for cell in cells:
        match = _A1_CELL.fullmatch(cell)
        if match is None:
            raise ValueError("print_range must use A1 notation")
        normalised.append(f"${match.group(1).upper()}${match.group(2)}")
    return ":".join(normalised)


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


def _prepare_xlsx_selection(
    source: Path, destination: Path, selection: OfficeSelection
) -> None:
    """Copy an XLSX while making the requested sheet and print area explicit."""

    try:
        with ZipFile(source) as archive:
            workbook_data = archive.read("xl/workbook.xml")
            relationships_data = archive.read("xl/_rels/workbook.xml.rels")
            infos = tuple(archive.infolist())
            workbook = ElementTree.fromstring(workbook_data)
            relationships = ElementTree.fromstring(relationships_data)
            namespace = {"main": _XLSX_MAIN_NS, "rel": _XLSX_REL_NS, "package": _PACKAGE_REL_NS}
            sheets = workbook.find("main:sheets", namespace)
            if sheets is None:
                raise DomainError("OFFICE_SHEET_NOT_FOUND", "XLSX has no worksheets")
            sheet_nodes = list(sheets)
            if not sheet_nodes:
                raise DomainError("OFFICE_SHEET_NOT_FOUND", "XLSX has no worksheets")
            active_index = _active_sheet_index(workbook, len(sheet_nodes), namespace)
            requested_name = selection.sheet
            if requested_name is None:
                index = active_index
            else:
                found_index = next(
                    (position for position, item in enumerate(sheet_nodes)
                     if item.attrib.get("name") == requested_name),
                    None,
                )
                if found_index is None:
                    raise DomainError(
                        "OFFICE_SHEET_NOT_FOUND", f"XLSX worksheet not found: {requested_name}"
                    )
                index = found_index
            selected_sheet = sheet_nodes[index]
            relation_id = selected_sheet.attrib.get(f"{{{_XLSX_REL_NS}}}id")
            if not relation_id:
                raise DomainError("OFFICE_SHEET_INVALID", "XLSX worksheet relationship is missing")
            relation = next(
                (item for item in relationships if item.attrib.get("Id") == relation_id), None
            )
            if relation is None:
                raise DomainError("OFFICE_SHEET_INVALID", "XLSX worksheet relationship is invalid")
            worksheet_path = posixpath.normpath(
                posixpath.join("xl", relation.attrib.get("Target", ""))
            )
            if not worksheet_path.startswith("xl/") or worksheet_path not in archive.namelist():
                raise DomainError("OFFICE_SHEET_INVALID", "XLSX worksheet target is invalid")
            views = workbook.find("main:bookViews", namespace)
            if views is None:
                views = ElementTree.Element(f"{{{_XLSX_MAIN_NS}}}bookViews")
                workbook.insert(0, views)
            view = views.find("main:workbookView", namespace)
            if view is None:
                view = ElementTree.SubElement(views, f"{{{_XLSX_MAIN_NS}}}workbookView")
            view.set("activeTab", str(index))
            if selection.print_range is not None:
                defined_names = workbook.find("main:definedNames", namespace)
                if defined_names is None:
                    defined_names = ElementTree.Element(f"{{{_XLSX_MAIN_NS}}}definedNames")
                    insert_at = next(
                        (position for position, item in enumerate(workbook)
                         if _local_name(item.tag) == "calcPr"),
                        len(workbook),
                    )
                    workbook.insert(insert_at, defined_names)
                print_area = next(
                    (
                        item
                        for item in defined_names
                        if item.attrib.get("name", "").casefold()
                        in {"print_area", "_xlnm.print_area"}
                        and item.attrib.get("localSheetId") == str(index)
                    ),
                    None,
                )
                if print_area is None:
                    print_area = ElementTree.SubElement(
                        defined_names,
                        f"{{{_XLSX_MAIN_NS}}}definedName",
                        {"name": "_xlnm.Print_Area", "localSheetId": str(index)},
                    )
                sheet_name = selected_sheet.attrib.get("name", "").replace("'", "''")
                print_area.text = f"'{sheet_name}'!{selection.print_range}"
            ElementTree.register_namespace("", _XLSX_MAIN_NS)
            ElementTree.register_namespace("r", _XLSX_REL_NS)
            rewritten = ElementTree.tostring(workbook, encoding="utf-8", xml_declaration=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with ZipFile(destination, "w", ZIP_DEFLATED) as result:
                for info in infos:
                    data = rewritten if info.filename == "xl/workbook.xml" else archive.read(info)
                    result.writestr(info, data)
    except (KeyError, ElementTree.ParseError, UnicodeError, BadZipFile) as exc:
        raise DomainError("OFFICE_SELECTION_INVALID", "XLSX cannot be edited safely") from exc


def _active_sheet_index(
    workbook: ElementTree.Element, count: int, namespace: dict[str, str]
) -> int:
    views = workbook.find("main:bookViews", namespace)
    view = views.find("main:workbookView", namespace) if views is not None else None
    try:
        index = int(view.attrib.get("activeTab", "0")) if view is not None else 0
    except ValueError:
        index = 0
    return min(max(index, 0), count - 1)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        if os.name == "nt" and process.pid is not None:
            with suppress(OSError):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.communicate()
        else:
            with suppress(ProcessLookupError):
                process.kill()
    with suppress(Exception):
        await process.communicate()
