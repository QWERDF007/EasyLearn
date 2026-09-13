import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePath
from sqlite3 import Row
from typing import Literal, Protocol
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError

from easylearn.cache import DocumentCache
from easylearn.database import Database
from easylearn.document_ir.schema import DocumentIR
from easylearn.documents.schema import DocumentView, ParseResultView
from easylearn.errors import DomainError
from easylearn.files import DocumentFiles
from easylearn.filetypes import INPUT_MIME, validate_input
from easylearn.paths import validate_portable_path
from easylearn.persistence.documents import DocumentStore
from easylearn.persistence.source_edits import SourceEditStore

logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class UploadSource(Protocol):
    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


class DocumentService:
    def __init__(
        self,
        database: Database,
        files: DocumentFiles,
        *,
        max_upload_bytes: int,
        cache: DocumentCache | None = None,
        store: DocumentStore | None = None,
    ) -> None:
        self.database = database
        self.store = store or DocumentStore(database)
        self.files = files
        self.max_upload_bytes = max_upload_bytes
        self.cache = cache

    async def create(self, upload: UploadSource) -> DocumentView:
        filename = upload.filename or "document"
        suffix = PurePath(filename).suffix.lower()
        if (
            not filename
            or len(filename) > 255
            or any(char in filename for char in "/\\\x00\r\n")
            or suffix not in INPUT_MIME
        ):
            raise DomainError("FILE_TYPE_UNSUPPORTED", "Unsupported document filename", status=422)
        document_id = uuid4()
        directory = self.files.create_document(document_id)
        destination = self.files.original_path(document_id, suffix)
        try:
            size, _ = await self.files.save_upload(upload, destination, self.max_upload_bytes)
            validate_input(destination, suffix)
            if size == 0:
                raise DomainError("FILE_EMPTY", "Uploaded document is empty", status=422)
            now = datetime.now(UTC).isoformat()
            await self.store.create(
                document_id, filename, str(destination.relative_to(directory)), now
            )
            logger.info(
                "Document uploaded: filename=%s, id=%s, size=%d bytes",
                filename,
                document_id,
                size,
            )
            return await self.get(document_id)
        except BaseException:
            if self.cache is not None:
                self.cache.invalidate(document_id)
            await self.files.remove_document(document_id)
            raise

    async def list(self, *, favorite: bool | None = None) -> tuple[DocumentView, ...]:
        rows = await self.store.list(favorite=favorite)
        return tuple([await self._view(row) for row in rows])

    async def get(self, document_id: UUID) -> DocumentView:
        row = await self.store.get(document_id)
        if row is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
        return await self._view(row)

    async def set_favorite(self, document_id: UUID, favorite: bool) -> DocumentView:
        await self.store.set_favorite(document_id, favorite)
        logger.info("Document favorite changed: id=%s, favorite=%s", document_id, favorite)
        return await self.get(document_id)

    async def delete(self, document_id: UUID) -> None:
        await self.get(document_id)
        await self.store.delete(document_id)
        if self.cache is not None:
            self.cache.invalidate(document_id)
        await self.files.remove_document(document_id)
        logger.info("Document deleted: id=%s", document_id)

    async def parse_row(self, document_id: UUID, parse_id: UUID) -> Row:
        row = await self.store.get_parse(document_id, parse_id)
        if row is None:
            raise DomainError("PARSE_NOT_FOUND", "Parse result not found", status=404)
        return row

    async def file_path(self, document_id: UUID, file_id: str) -> Path:
        await self.get(document_id)
        if file_id == "original":
            row = await self.store.get(document_id)
            assert row is not None
            return self.files.read_relative(document_id, row["original_path"])
        parts = file_id.split(":", 2)
        if len(parts) not in (2, 3):
            raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404)
        if parts[0] in {"preview", "ir", "raw"} and len(parts) == 2:
            parse_id = _file_uuid(parts[1])
            row = await self.parse_row(document_id, parse_id)
            column = {"preview": "preview_path", "ir": "ir_path", "raw": "raw_path"}[parts[0]]
            relative = row[column]
            if relative is None:
                raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404)
            if parts[0] == "ir":
                await self.load_ir(document_id, parse_id)
            return self.files.read_relative(document_id, relative)
        if parts[0] == "asset" and len(parts) == 3:
            parse_id = _file_uuid(parts[1])
            asset_id = _file_uuid(parts[2])
            await self.parse_row(document_id, parse_id)
            ir = await self.load_ir(document_id, parse_id)
            descriptor = next((asset for asset in ir.assets if asset.asset_id == asset_id), None)
            if descriptor is None:
                raise DomainError("FILE_NOT_FOUND", "Document asset not found", status=404)
            root = self.files.paths.document(document_id).resolve()
            path = (root / f"parses/{parse_id}/{descriptor.export_path}").resolve()
            if path != root and root not in path.parents:
                raise DomainError("FILE_NOT_FOUND", "File is outside the document", status=404)
            if not path.is_file():
                raise DomainError(
                    "ASSET_CORRUPT",
                    f"Asset {asset_id} file is missing at {descriptor.export_path}",
                    status=500,
                )
            actual_sha = _sha256_file(path)
            if actual_sha != descriptor.sha256:
                raise DomainError(
                    "ASSET_CORRUPT",
                    f"Asset {asset_id} checksum mismatch: expected {descriptor.sha256}, got {actual_sha}",
                    status=500,
                )
            return path
        if parts[0] == "export" and len(parts) == 3:
            try:
                export_id = UUID(parts[1])
            except ValueError:
                raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404) from None
            relative = parts[2]
            try:
                validate_portable_path(relative)
            except ValueError:
                raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404) from None
            export_root = self.files.paths.export(document_id, export_id)
            path = (export_root / relative).resolve()
            if export_root.resolve() not in path.parents or not path.is_file():
                raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404)
            manifest_path = export_root / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    files_list = manifest.get("files", [])
                    entry = next((f for f in files_list if f.get("path") == relative), None)
                    if entry is not None and "sha256" in entry:
                        actual_sha = _sha256_file(path)
                        if actual_sha != entry["sha256"]:
                            raise DomainError(
                                "EXPORT_CORRUPT",
                                f"Export file {relative} checksum mismatch: expected {entry['sha256']}, got {actual_sha}",
                                status=500,
                            )
                except DomainError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to verify export manifest for %s: %s", export_id, exc)
            return path
        raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404)

    async def load_ir(self, document_id: UUID, parse_id: UUID) -> DocumentIR:
        row = await self.parse_row(document_id, parse_id)
        if self.cache is not None:
            cached = self.cache.get(document_id, parse_id)
            if cached is not None:
                return cached
        path = self.files.read_relative(document_id, row["ir_path"])
        try:
            result = DocumentIR.model_validate_json(path.read_bytes())
        except ValidationError:
            raise DomainError(
                "PARSE_RESULT_INVALID",
                "Parse result is not a valid document snapshot",
                status=500,
            ) from None
        if result.document_id != document_id or result.parse_run_id != parse_id:
            raise DomainError(
                "PARSE_RESULT_INVALID",
                "Parse result identity does not match its document and version",
                status=500,
            )
        if self.cache is not None:
            self.cache.put(document_id, parse_id, result)
        return result

    async def effective_snapshot(
        self, document_id: UUID, parse_id: UUID
    ) -> object:
        ir = await self.load_ir(document_id, parse_id)
        from easylearn.source_edits import (
            EffectiveDocumentSnapshot,
            SourceEditView,
            apply_source_edits,
            compute_source_fingerprint,
            _editable_nodes,
            _view,
        )
        nodes = _editable_nodes(ir)
        rows = await SourceEditStore(self.database).list_edits(parse_id)
        edits: list[SourceEditView] = []
        for row in rows:
            node = nodes.get((row[0], row[1]))
            if node is not None:
                edits.append(_view(parse_id, row[0], row[1], node, row[2], row[3], row[4]))
        edits_tuple = tuple(edits)
        effective_ir = apply_source_edits(ir, edits_tuple)
        fingerprint = compute_source_fingerprint(parse_id, edits_tuple)
        revision = sum(edit.revision for edit in edits_tuple)
        return EffectiveDocumentSnapshot(
            document_id=document_id,
            parse_id=parse_id,
            ir=effective_ir,
            source_fingerprint=fingerprint,
            revision=revision,
            edits=edits_tuple,
        )


    async def load_raw_markdown(self, document_id: UUID, parse_id: UUID) -> str:
        row = await self.parse_row(document_id, parse_id)
        relative = row["raw_path"]
        if relative is None:
            raise DomainError("FILE_NOT_FOUND", "Raw Markdown is not available", status=404)
        path = self.files.read_relative(document_id, relative)
        try:
            with ZipFile(path) as archive:
                candidates = tuple(
                    name
                    for name in archive.namelist()
                    if name.startswith("input/")
                    and name.count("/") == 2
                    and name.endswith("/input.md")
                )
                if len(candidates) != 1:
                    raise DomainError(
                        "PARSE_RESULT_INVALID",
                        "Raw Markdown is missing from the parse result",
                        status=500,
                    )
                return archive.read(candidates[0]).decode("utf-8")
        except DomainError:
            raise
        except (BadZipFile, KeyError, OSError, UnicodeDecodeError):
            raise DomainError(
                "PARSE_RESULT_INVALID",
                "Raw Markdown cannot be read from the parse result",
                status=500,
            ) from None

    def invalidate_ir(self, document_id: UUID, parse_id: UUID | None = None) -> None:
        if self.cache is not None:
            self.cache.invalidate(document_id, parse_id)

    async def _view(self, row: Row) -> DocumentView:
        document_id = UUID(row["id"])
        parse_rows = await self.store.list_parses_with_translation_counts(document_id)

        results_list: list[ParseResultView] = []
        for parse_row in parse_rows:
            metadata = json.loads(parse_row["metadata_json"])
            total_units = metadata.get("total_units")
            parse_id = UUID(parse_row["id"])
            corrupt_reasons: list[str] = []
            ir = None
            try:
                ir = await self.load_ir(document_id, parse_id)
            except Exception as exc:
                corrupt_reasons.append(f"Failed to load IR: {exc}")

            if ir is not None:
                from easylearn.translation import translation_units

                actual_units = len(translation_units(ir))
                if total_units != actual_units:
                    total_units = actual_units
                    metadata["total_units"] = total_units
                    try:
                        await self.store.update_parse_metadata(parse_id, metadata)
                    except Exception as exc:
                        logger.warning("Failed to update parse metadata for %s: %s", parse_id, exc)
            elif total_units is None:
                total_units = 0

            translated_units = int(parse_row["translated_units"])
            if total_units > 0 and translated_units >= total_units:
                translation_status = "completed"
            elif translated_units > 0:
                translation_status = "partial"
            else:
                translation_status = "none"

            if ir is not None:
                parse_root = self.files.paths.document(document_id) / "parses" / str(parse_id)
                for asset in ir.assets:
                    asset_file = parse_root / asset.export_path
                    if not asset_file.is_file():
                        corrupt_reasons.append(f"Missing asset file: {asset.export_path}")
                    else:
                        actual_sha = _sha256_file(asset_file)
                        if actual_sha != asset.sha256:
                            corrupt_reasons.append(
                                f"Asset checksum mismatch: {asset.export_path}"
                            )

            integrity_status: Literal["valid", "corrupt"] = (
                "corrupt" if corrupt_reasons else "valid"
            )

            results_list.append(
                ParseResultView(
                    parse_id=parse_id,
                    created_at=datetime.fromisoformat(parse_row["created_at"]),
                    pages=parse_row["pages"],
                    preview_file_id=f"preview:{parse_row['id']}",
                    ir_file_id=f"ir:{parse_row['id']}",
                    raw_file_id=(f"raw:{parse_row['id']}" if parse_row["raw_path"] else None),
                    metadata=metadata,
                    has_translation=(translated_units > 0),
                    total_units=total_units,
                    translated_units=translated_units,
                    translation_status=translation_status,
                    integrity_status=integrity_status,
                    corrupt_reasons=tuple(corrupt_reasons),
                )
            )

        results = tuple(results_list)
        original_size: int | None = None
        try:
            original_file = self.files.original_path(
                UUID(row["id"]), PurePath(row["original_path"]).suffix
            )
            if original_file.is_file():
                original_size = original_file.stat().st_size
        except Exception:
            original_size = None

        active_parse_id = UUID(row["active_parse_id"]) if row["active_parse_id"] else None
        active_parse = (
            next((r for r in results if r.parse_id == active_parse_id), None)
            if active_parse_id
            else (results[0] if results else None)
        )
        doc_has_translation = bool(active_parse.has_translation) if active_parse else False
        doc_total_units = active_parse.total_units if active_parse else 0
        doc_translated_units = active_parse.translated_units if active_parse else 0
        doc_translation_status = active_parse.translation_status if active_parse else "none"

        return DocumentView(
            document_id=document_id,
            name=row["name"],
            favorite=bool(row["favorite"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            active_parse_id=active_parse_id,
            size_bytes=original_size,
            parse_results=results,
            has_translation=doc_has_translation,
            total_units=doc_total_units,
            translated_units=doc_translated_units,
            translation_status=doc_translation_status,
        )


def _file_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404) from None
