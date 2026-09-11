from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import re
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from easylearn.config import Settings
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.images import inspect_image
from easylearn.previews.pdf import PdfPreflight
from easylearn.previews.schema import PreflightReport

logger = logging.getLogger(__name__)

_A1_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


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


def _prepare_xlsx_selection(
    source: Path, destination: Path, selection: object
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
            requested_name = getattr(selection, "sheet", None)
            if requested_name is None:
                index = active_index
            else:
                found_index = next(
                    (
                        position
                        for position, item in enumerate(sheet_nodes)
                        if item.attrib.get("name") == requested_name
                    ),
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
            print_range = getattr(selection, "print_range", None)
            if print_range is not None:
                defined_names = workbook.find("main:definedNames", namespace)
                if defined_names is None:
                    defined_names = ElementTree.Element(f"{{{_XLSX_MAIN_NS}}}definedNames")
                    insert_at = next(
                        (
                            position
                            for position, item in enumerate(workbook)
                            if _local_name(item.tag) == "calcPr"
                        ),
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
                print_area.text = f"'{sheet_name}'!{print_range}"
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


@dataclass(frozen=True)
class PreflightResult:
    pdf_path: Path
    pages: int
    preview_sha256: str
    report: PreflightReport


class ParsePreflight:
    """Preflight validation and format normalization from source inputs to a validated PDF."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def prepare_pdf(
        self,
        original: Path,
        destination: Path,
        context: Any,
        office: Any = None,
    ) -> None:
        await context.check()
        suffix = original.suffix.lower()
        if suffix == ".pdf":
            if office is not None:
                raise DomainError(
                    "OFFICE_SELECTION_INVALID", "Office selection requires an XLSX input"
                )
            await run_blocking(shutil.copyfile, original, destination)
        elif suffix in {
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
        elif self.settings.extensions.office_enabled:
            await self._convert_office(original, destination, context, office)
        else:
            raise DomainError(
                "OFFICE_CONVERTER_REQUIRED", "Office conversion is disabled", status=422
            )

    async def inspect_preview(self, destination: Path) -> tuple[str, PreflightReport]:
        preview_sha256 = await run_blocking(_sha256_file, destination)
        report = await PdfPreflight(
            self.settings.mineru.preview_limits, timeout=self.settings.mineru.timeout_seconds
        ).inspect(destination, preview_sha256)
        return preview_sha256, report

    async def prepare_input(
        self,
        original: Path,
        destination: Path,
        context: Any,
        office: Any = None,
    ) -> PreflightResult:
        await self.prepare_pdf(original, destination, context, office)
        preview_sha256, report = await self.inspect_preview(destination)
        return PreflightResult(
            pdf_path=destination,
            pages=len(report.pages),
            preview_sha256=preview_sha256,
            report=report,
        )

    async def _convert_office(
        self,
        original: Path,
        destination: Path,
        context: Any,
        selection: Any = None,
    ) -> None:
        output = destination.parent / "office-output"
        output.mkdir(parents=True, exist_ok=True)
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
