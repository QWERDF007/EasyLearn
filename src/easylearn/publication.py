"""Recoverable filesystem-to-database publication for durable artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from easylearn.database import Database
from easylearn.files import DocumentFiles
from easylearn.execution import run_blocking

logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class PublicationKind(StrEnum):
    PARSE = "parse"
    EXPORT = "export"


class PublicationStatus(StrEnum):
    STAGED = "staged"
    VALIDATED = "validated"
    PUBLISHED = "published"
    RECORDED = "recorded"
    ABANDONED = "abandoned"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class ReconciliationReport:
    recovered: int = 0
    recorded: int = 0
    abandoned: int = 0
    removed_orphan_assets: int = 0
    corrupt_assets: int = 0


RecordPublication = Callable[[aiosqlite.Connection], Awaitable[None]]


class ArtifactPublisher:
    """Own the staged/published/recorded state machine for durable artifacts."""

    def __init__(self, database: Database, files: DocumentFiles) -> None:
        self.database = database
        self.files = files

    async def is_recorded(self, kind: PublicationKind | str, artifact_id: UUID) -> bool:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM publications WHERE kind = ? AND artifact_id = ? AND status = ?",
                (PublicationKind(kind).value, str(artifact_id), PublicationStatus.RECORDED.value),
            )
            return await cursor.fetchone() is not None

    async def publish(
        self,
        *,
        document_id: UUID,
        artifact_id: UUID,
        kind: PublicationKind | str,
        staging: Path,
        destination: Path,
        payload: dict[str, Any],
        record: RecordPublication,
    ) -> UUID:
        async with self.database.read() as connection:
            cursor = await connection.execute(
                "SELECT id, status FROM publications WHERE kind = ? AND artifact_id = ?",
                (PublicationKind(kind).value, str(artifact_id)),
            )
            existing = await cursor.fetchone()
        if existing is not None and existing[1] == PublicationStatus.RECORDED.value:
            logger.info("Artifact %s for %s is already recorded; skipping publication", artifact_id, kind)
            return UUID(existing[0])

        publication_id = uuid4()
        timestamp = _now()
        await self._insert(
            publication_id,
            document_id,
            artifact_id,
            PublicationKind(kind),
            staging,
            destination,
            payload,
            PublicationStatus.STAGED,
            timestamp,
        )
        await self._set_status(publication_id, PublicationStatus.VALIDATED)
        try:
            await run_blocking(self.files.publish_directory, staging, destination)
        except BaseException:
            await self._mark_abandoned(publication_id)
            raise

        await self._set_status(publication_id, PublicationStatus.PUBLISHED)
        try:
            async with self.database.transaction() as connection:
                await record(connection)
                await connection.execute(
                    "UPDATE publications SET status = ?, updated_at = ? WHERE id = ?",
                    (PublicationStatus.RECORDED.value, _now(), str(publication_id)),
                )
        except BaseException:
            await self._rollback_published_path(staging, destination)
            await self._mark_abandoned(publication_id)
            raise
        return publication_id

    async def reconcile(self) -> ReconciliationReport:
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT id, document_id, artifact_id, kind, staging_path, destination_path, "
                    "payload_json, status FROM publications "
                    "WHERE status IN ('staged', 'validated', 'published') "
                    "ORDER BY created_at, id"
                )
            ).fetchall()

        recovered = recorded = abandoned = 0
        for row in rows:
            publication_id = UUID(row[0])
            staging = Path(row[4])
            destination = Path(row[5])
            status = PublicationStatus(row[7])
            if destination.is_dir():
                if status != PublicationStatus.PUBLISHED:
                    await self._set_status(publication_id, PublicationStatus.PUBLISHED)
                    recovered += 1
                if await self._record_recovered(row):
                    recorded += 1
                continue
            if status == PublicationStatus.VALIDATED and staging.is_dir():
                try:
                    await run_blocking(self.files.publish_directory, staging, destination)
                except (OSError, RuntimeError):
                    logger.warning(
                        "Could not recover staged publication %s", publication_id, exc_info=True
                    )
                    continue
                await self._set_status(publication_id, PublicationStatus.PUBLISHED)
                recovered += 1
                if await self._record_recovered(row):
                    recorded += 1
                continue
            if status == PublicationStatus.STAGED and staging.is_dir():
                await self._mark_abandoned(publication_id)
                abandoned += 1
                continue
            await self._mark_abandoned(publication_id)
            abandoned += 1

        removed_orphan_assets = 0
        corrupt_assets = 0

        if self.files.paths.documents.is_dir():
            for doc_dir in self.files.paths.documents.iterdir():
                if not doc_dir.is_dir():
                    continue
                try:
                    doc_id = str(UUID(doc_dir.name))
                except ValueError:
                    continue
                if not await self._document_exists(doc_id):
                    await self._remove_destination(doc_dir)
                    continue

                parses_dir = doc_dir / "parses"
                if parses_dir.is_dir():
                    async with self.database.read() as connection:
                        cursor = await connection.execute(
                            "SELECT id, ir_path FROM parse_results WHERE document_id = ?",
                            (doc_id,),
                        )
                        recorded_parses = {row[0]: row[1] for row in await cursor.fetchall()}

                    for parse_folder in parses_dir.iterdir():
                        if not parse_folder.is_dir():
                            continue
                        parse_id_str = parse_folder.name
                        if parse_id_str not in recorded_parses:
                            continue
                        ir_file = parse_folder / "document.json"
                        if not ir_file.is_file():
                            continue
                        try:
                            ir_data = json.loads(ir_file.read_text(encoding="utf-8"))
                            referenced_paths = set()
                            standard_names = {"document.json", "preview.pdf", "mineru.zip"}
                            for asset in ir_data.get("assets", []):
                                exp_path = asset.get("export_path")
                                if exp_path:
                                    referenced_paths.add(Path(exp_path).as_posix())
                                    asset_file = parse_folder / exp_path
                                    if not asset_file.is_file():
                                        corrupt_assets += 1
                                    else:
                                        actual_sha = _sha256_file(asset_file)
                                        if actual_sha != asset.get("sha256"):
                                            corrupt_assets += 1
                            for file_path in sorted(parse_folder.rglob("*")):
                                if not file_path.is_file():
                                    continue
                                rel_posix = file_path.relative_to(parse_folder).as_posix()
                                if rel_posix in standard_names or rel_posix in referenced_paths:
                                    continue
                                try:
                                    file_path.unlink()
                                    removed_orphan_assets += 1
                                except OSError as exc:
                                    logger.warning(
                                        "Could not remove orphan asset %s: %s", file_path, exc
                                    )
                        except Exception as exc:
                            logger.warning("Failed to reconcile parse %s: %s", parse_folder, exc)

        return ReconciliationReport(
            recovered=recovered,
            recorded=recorded,
            abandoned=abandoned,
            removed_orphan_assets=removed_orphan_assets,
            corrupt_assets=corrupt_assets,
        )

    async def _insert(
        self,
        publication_id: UUID,
        document_id: UUID,
        artifact_id: UUID,
        kind: PublicationKind,
        staging: Path,
        destination: Path,
        payload: dict[str, Any],
        status: PublicationStatus,
        timestamp: str,
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO publications "
                "(id, document_id, artifact_id, kind, staging_path, destination_path, "
                "payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(publication_id),
                    str(document_id),
                    str(artifact_id),
                    kind.value,
                    str(staging),
                    str(destination),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    status.value,
                    timestamp,
                    timestamp,
                ),
            )

    async def _set_status(self, publication_id: UUID, status: PublicationStatus) -> None:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE publications SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, _now(), str(publication_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Publication {publication_id} no longer exists")

    async def _mark_abandoned(self, publication_id: UUID) -> None:
        try:
            await self._set_status(publication_id, PublicationStatus.ABANDONED)
        except Exception:
            logger.warning("Could not mark publication %s abandoned", publication_id, exc_info=True)

    async def _rollback_published_path(self, staging: Path, destination: Path) -> None:
        if not destination.is_dir() or staging.exists():
            return
        try:
            await run_blocking(staging.parent.mkdir, parents=True, exist_ok=True)
            await run_blocking(os.replace, destination, staging)
        except OSError:
            logger.error(
                "Could not roll back published artifact from %s to %s",
                destination,
                staging,
                exc_info=True,
            )

    async def _record_recovered(self, row: aiosqlite.Row) -> bool:
        publication_id = UUID(row[0])
        kind = PublicationKind(row[3])
        try:
            payload = json.loads(row[6])
        except (TypeError, ValueError):
            await self._mark_abandoned(publication_id)
            return False
        if not isinstance(payload, dict):
            await self._mark_abandoned(publication_id)
            return False
        if kind is PublicationKind.PARSE:
            if not await self._document_exists(row[1]):
                await self._remove_destination(Path(row[5]))
                await self._mark_abandoned(publication_id)
                return False
            async with self.database.transaction() as connection:
                await connection.execute(
                    "INSERT OR IGNORE INTO parse_results "
                    "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(payload["parse_id"]),
                        row[1],
                        str(payload["preview_path"]),
                        str(payload["ir_path"]),
                        payload.get("raw_path"),
                        int(payload["pages"]),
                        json.dumps(
                            payload.get("metadata", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        str(payload.get("created_at") or _now()),
                    ),
                )
                if payload.get("set_active", True):
                    await connection.execute(
                        "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                        (str(payload["parse_id"]), row[1]),
                    )
                await connection.execute(
                    "UPDATE publications SET status = ?, updated_at = ? WHERE id = ?",
                    (PublicationStatus.RECORDED.value, _now(), str(publication_id)),
                )
            return True
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE publications SET status = ?, updated_at = ? WHERE id = ?",
                (PublicationStatus.RECORDED.value, _now(), str(publication_id)),
            )
        return True

    async def _document_exists(self, document_id: str) -> bool:
        async with self.database.read() as connection:
            row = await (
                await connection.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,))
            ).fetchone()
        return row is not None

    async def _remove_destination(self, destination: Path) -> None:
        try:
            await run_blocking(shutil.rmtree, destination, True)
        except OSError:
            logger.warning("Could not remove abandoned publication %s", destination, exc_info=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()
