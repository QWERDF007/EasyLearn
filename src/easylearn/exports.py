"""Snapshot-based document exports."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import BaseModel, ConfigDict, JsonValue

from easylearn.database import Database
from easylearn.document_ir.schema import DocumentIR
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.persistence.translations import TranslationStore
from easylearn.publication import ArtifactPublisher, PublicationKind
from easylearn.rendering import render_markdown
from easylearn.source_edits import compute_unit_fingerprint
from easylearn.tasks import TaskContext, TaskManager, TaskRecord
from easylearn.translation import translation_units

logger = logging.getLogger(__name__)


class ExportFormat(StrEnum):
    ZIP = "zip"
    SOURCE_MARKDOWN = "source_markdown"
    CHINESE_MARKDOWN = "chinese_markdown"
    BILINGUAL_MARKDOWN = "bilingual_markdown"
    JSON = "json"
    IMAGES = "images"


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    format: ExportFormat = ExportFormat.ZIP
    include_bilingual: bool = True


class ExportService:
    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        manager: TaskManager,
        publisher: ArtifactPublisher | None = None,
        translation_store: TranslationStore | None = None,
    ) -> None:
        self.database = database
        self.documents = documents
        self.manager = manager
        self.publisher = publisher or ArtifactPublisher(database, documents.files)
        self.translation_store = translation_store or TranslationStore(database)

    async def submit(self, document_id: UUID, request: ExportRequest) -> TaskView:
        await self.documents.effective_snapshot(document_id, request.parse_id)
        export_id = uuid4()

        async def admit() -> None:
            await self.documents.effective_snapshot(document_id, request.parse_id)

        task_view = await self.manager.submit(
            document_id,
            JobKind.EXPORT,
            {
                "parse_id": str(request.parse_id),
                "export_id": str(export_id),
                "format": request.format.value,
                "include_bilingual": request.include_bilingual,
            },
            admission=admit,
        )
        logger.info(
            "Export task submitted: document_id=%s, task_id=%s, format=%s",
            document_id,
            task_view.task_id,
            request.format.value,
        )
        return task_view

    async def execute(self, record: TaskRecord, context: TaskContext) -> dict[str, JsonValue]:
        parse_id = UUID(str(record.scope["parse_id"]))
        export_id = UUID(str(record.scope["export_id"]))
        selected = ExportFormat(str(record.scope["format"]))

        file_name = {
            ExportFormat.ZIP: "export.zip",
            ExportFormat.SOURCE_MARKDOWN: "source.md",
            ExportFormat.CHINESE_MARKDOWN: "中文.md",
            ExportFormat.BILINGUAL_MARKDOWN: "中英对照.md",
            ExportFormat.JSON: "document.json",
            ExportFormat.IMAGES: "images.zip",
        }[selected]
        result_ref: dict[str, JsonValue] = {
            "export_id": str(export_id),
            "file_id": f"export:{export_id}:{file_name}",
            "manifest_file_id": f"export:{export_id}:manifest.json",
        }

        if await self.publisher.is_recorded("export", export_id):
            logger.info("Export %s is already recorded; skipping duplicate execution", export_id)
            return result_ref

        logger.info(
            "Export started: document_id=%s, parse_id=%s, format=%s",
            record.document_id,
            parse_id,
            selected.value,
        )
        output = self.documents.files.paths.task(record.task_id) / "export"
        output.mkdir(parents=True, exist_ok=False)
        try:
            await context.progress(0.05, "Freezing export snapshot")
            ir, translations, revisions = await self._snapshot(record.document_id, parse_id)
            include_bilingual = bool(record.scope.get("include_bilingual", True))
            if selected == ExportFormat.BILINGUAL_MARKDOWN and not include_bilingual:
                raise DomainError(
                    "EXPORT_FORMAT_UNAVAILABLE",
                    "Bilingual Markdown was disabled for this export",
                    status=422,
                )
            if selected == ExportFormat.IMAGES and not ir.assets:
                raise DomainError(
                    "EXPORT_NO_IMAGES",
                    "This document has no images to export",
                    status=422,
                )
            await context.check()
            await run_blocking(
                self._write_export,
                output,
                ir,
                translations,
                revisions,
                include_bilingual=include_bilingual,
            )
            await context.progress(0.65, "Copying document assets")
            await self._copy_assets(record.document_id, parse_id, ir, output)
            await context.check()
            await run_blocking(
                self._finish_export,
                output,
                ir,
                revisions,
                include_bilingual=include_bilingual,
            )
            destination = self.documents.files.paths.export(record.document_id, export_id)
            file_name = {
                ExportFormat.ZIP: "export.zip",
                ExportFormat.SOURCE_MARKDOWN: "source.md",
                ExportFormat.CHINESE_MARKDOWN: "中文.md",
                ExportFormat.BILINGUAL_MARKDOWN: "中英对照.md",
                ExportFormat.JSON: "document.json",
                ExportFormat.IMAGES: "images.zip",
            }[selected]
            result_ref: dict[str, JsonValue] = {
                "export_id": str(export_id),
                "file_id": f"export:{export_id}:{file_name}",
                "manifest_file_id": f"export:{export_id}:manifest.json",
            }

            async def record_export(_connection: object) -> None:
                pass

            async def publish() -> object:
                return await self.publisher.publish(
                    document_id=record.document_id,
                    artifact_id=export_id,
                    kind=PublicationKind.EXPORT,
                    staging=output,
                    destination=destination,
                    payload={
                        "export_id": str(export_id),
                        "format": selected.value,
                        "file_name": file_name,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    record=record_export,
                )

            await context.publish(result_ref, publish)
            logger.info(
                "Export published: document_id=%s, export_id=%s, file=%s",
                record.document_id,
                export_id,
                file_name,
            )
            return result_ref
        except BaseException as exc:
            logger.error(
                "Export failed: document_id=%s, export_id=%s, error=%s",
                record.document_id,
                export_id,
                exc,
            )
            raise
        finally:
            await run_blocking(self.documents.files.remove_task_directory, record.task_id)

    async def _snapshot(
        self, document_id: UUID, parse_id: UUID
    ) -> tuple[DocumentIR, dict[str, str], dict[str, int]]:
        snapshot = await self.documents.effective_snapshot(document_id, parse_id)
        ir = snapshot.ir
        rows = await self.translation_store.get_translations(parse_id)
        values = {
            row["unit_id"]: (
                row["manual_text"] if row["use_manual"] and row["manual_text"] is not None else row["auto_text"],
                int(row["revision"]),
                row["source_fingerprint"],
            )
            for row in rows
        }
        effective: dict[str, str] = {}
        revisions: dict[str, int] = {}
        for unit in translation_units(ir):
            if unit.unit_id in values:
                val, rev, fp = values[unit.unit_id]
                expected_fp = compute_unit_fingerprint(unit.unit_id, unit.source_text)
                is_stale = fp is not None and fp != expected_fp
                if not is_stale and val is not None:
                    effective[unit.unit_id] = val
                revisions[unit.unit_id] = rev
        return ir, effective, revisions

    async def _copy_assets(
        self, document_id: UUID, parse_id: UUID, ir: DocumentIR, output: Path
    ) -> None:
        for asset in ir.assets:
            source = await self.documents.file_path(
                document_id, f"asset:{parse_id}:{asset.asset_id}"
            )
            destination = output / asset.export_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            await run_blocking(shutil.copyfile, source, destination)

    def _write_export(
        self,
        output: Path,
        ir: DocumentIR,
        translations: Mapping[str, str],
        revisions: Mapping[str, int],
        *,
        include_bilingual: bool,
    ) -> None:
        asset_links = {str(asset.asset_id): asset.export_path for asset in ir.assets}
        (output / "source.md").write_text(
            render_markdown(ir, language="source", asset_links=asset_links), encoding="utf-8"
        )
        (output / "中文.md").write_text(
            render_markdown(
                ir, translations, language="chinese", asset_links=asset_links
            ),
            encoding="utf-8",
        )
        if include_bilingual:
            (output / "中英对照.md").write_text(
                render_markdown(
                    ir, translations, bilingual=True, asset_links=asset_links
                ),
                encoding="utf-8",
            )
        (output / "document.json").write_text(
            ir.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
        )
        (output / "_snapshot.json").write_text(
            json.dumps({"revisions": revisions}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _finish_export(
        self,
        output: Path,
        ir: DocumentIR,
        revisions: Mapping[str, int],
        *,
        include_bilingual: bool,
    ) -> None:
        (output / "_snapshot.json").unlink(missing_ok=True)
        manifest_files: list[JsonValue] = []
        manifest: dict[str, JsonValue] = {
            "document_id": str(ir.document_id),
            "parse_id": str(ir.parse_run_id),
            "preview_sha256": ir.preview_sha256,
            "generated_at": datetime.now(UTC).isoformat(),
            "translation_revisions": dict(revisions),
            "included": {
                "bilingual_markdown": include_bilingual,
                "images": bool(ir.assets),
            },
            "files": manifest_files,
        }
        for path in sorted(output.rglob("*")):
            if not path.is_file():
                continue
            manifest_files.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        files = [
            path
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name not in {"export.zip", "images.zip"}
        ]
        with ZipFile(output / "export.zip", "w", ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(output).as_posix())
        image_files = [path for path in files if path.is_relative_to(output / "images")]
        if image_files:
            with ZipFile(output / "images.zip", "w", ZIP_DEFLATED) as archive:
                for path in image_files:
                    archive.write(path, path.relative_to(output / "images").as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
