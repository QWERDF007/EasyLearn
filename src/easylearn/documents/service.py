from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path, PurePath
from sqlite3 import Row
from typing import Protocol
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

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.database = database
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
            async with self.database.transaction() as connection:
                await connection.execute(
                    "INSERT INTO documents(id, name, original_path, favorite, created_at) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (str(document_id), filename, str(destination.relative_to(directory)), now),
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
        query = "SELECT * FROM documents"
        parameters: tuple[object, ...] = ()
        if favorite is not None:
            query += " WHERE favorite = ?"
            parameters = (int(favorite),)
        query += " ORDER BY created_at DESC, id DESC"
        async with self.database.read() as connection:
            cursor = await connection.execute(query, parameters)
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple([await self._view(row) for row in rows])

    async def get(self, document_id: UUID) -> DocumentView:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT * FROM documents WHERE id = ?", (str(document_id),)
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
        return await self._view(row)

    async def set_favorite(self, document_id: UUID, favorite: bool) -> DocumentView:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE documents SET favorite = ? WHERE id = ?",
                (int(favorite), str(document_id)),
            )
            if cursor.rowcount != 1:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
        logger.info("Document favorite changed: id=%s, favorite=%s", document_id, favorite)
        return await self.get(document_id)

    async def delete(self, document_id: UUID) -> None:
        await self.get(document_id)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM documents WHERE id = ?", (str(document_id),)
            )
            if cursor.rowcount != 1:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
        if self.cache is not None:
            self.cache.invalidate(document_id)
        await self.files.remove_document(document_id)
        logger.info("Document deleted: id=%s", document_id)

    async def parse_row(self, document_id: UUID, parse_id: UUID) -> Row:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT p.* FROM parse_results p WHERE p.document_id = ? AND p.id = ?",
                (str(document_id), str(parse_id)),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise DomainError("PARSE_NOT_FOUND", "Parse result not found", status=404)
        return row

    async def file_path(self, document_id: UUID, file_id: str) -> Path:
        await self.get(document_id)
        if file_id == "original":
            async with self.database.read() as connection:
                cursor = await connection.execute(
                    "SELECT original_path FROM documents WHERE id = ?", (str(document_id),)
                )
                row = await cursor.fetchone()
                await cursor.close()
            assert row is not None
            return self.files.read_relative(document_id, row[0])
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
            return self.files.read_relative(
                document_id, f"parses/{parse_id}/{descriptor.export_path}"
            )
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
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT p.*, "
                "EXISTS("
                "  SELECT 1 FROM translations t "
                "  WHERE t.parse_id = p.id "
                "  AND ("
                "    (t.auto_text IS NOT NULL AND t.auto_text != '') "
                "    OR (t.manual_text IS NOT NULL AND t.manual_text != '')"
                "  )"
                ") AS has_translation "
                "FROM parse_results p WHERE p.document_id = ? "
                "ORDER BY p.created_at DESC, p.id DESC",
                (row["id"],),
            )
            parse_rows = await cursor.fetchall()
            await cursor.close()
        results = tuple(
            ParseResultView(
                parse_id=UUID(parse_row["id"]),
                created_at=datetime.fromisoformat(parse_row["created_at"]),
                pages=parse_row["pages"],
                preview_file_id=f"preview:{parse_row['id']}",
                ir_file_id=f"ir:{parse_row['id']}",
                raw_file_id=(f"raw:{parse_row['id']}" if parse_row["raw_path"] else None),
                metadata=json.loads(parse_row["metadata_json"]),
                has_translation=bool(parse_row["has_translation"]),
            )
            for parse_row in parse_rows
        )
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

        return DocumentView(
            document_id=UUID(row["id"]),
            name=row["name"],
            favorite=bool(row["favorite"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            active_parse_id=active_parse_id,
            size_bytes=original_size,
            parse_results=results,
            has_translation=doc_has_translation,
        )


def _file_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404) from None
