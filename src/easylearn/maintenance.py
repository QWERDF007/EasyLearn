"""Bounded startup housekeeping for local files and successful parse history."""

from __future__ import annotations

import logging
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from easylearn.database import Database
from easylearn.execution import run_blocking
from easylearn.paths import DataPaths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupReport:
    removed_tmp: int = 0
    removed_exports: int = 0
    removed_parse_versions: int = 0
    removed_orphan_parses: int = 0


class MaintenanceService:
    """Clean only old, unreferenced data while no application task is running."""

    def __init__(
        self,
        database: Database | None,
        paths: DataPaths,
        *,
        tmp_retention_seconds: int,
        export_retention_seconds: int,
        parse_keep: int,
    ) -> None:
        if min(tmp_retention_seconds, export_retention_seconds, parse_keep) < 1:
            raise ValueError("Maintenance limits must be positive")
        self.database = database
        self.paths = paths
        self.tmp_retention_seconds = tmp_retention_seconds
        self.export_retention_seconds = export_retention_seconds
        self.parse_keep = parse_keep

    async def cleanup(self) -> CleanupReport:
        report = CleanupReport()
        remaining_parse_ids: set[tuple[str, str]] = set()
        if self.database is not None:
            removed, remaining_parse_ids = await self._prune_database_versions()
            report = CleanupReport(removed_parse_versions=removed)
        now = time.time()
        removed_tmp = await self._remove_old_directories(
            self.paths.temporary, now - self.tmp_retention_seconds
        )
        removed_exports = await self._remove_old_exports(now - self.export_retention_seconds)
        removed_orphans = await self._remove_orphan_parses(
            remaining_parse_ids, now - self.tmp_retention_seconds
        )
        report = CleanupReport(
            removed_tmp=removed_tmp,
            removed_exports=removed_exports,
            removed_parse_versions=report.removed_parse_versions,
            removed_orphan_parses=removed_orphans,
        )
        if any(report.__dict__.values()):
            logger.info("Startup cleanup removed %s", report)
        return report

    async def _prune_database_versions(self) -> tuple[int, set[tuple[str, str]]]:
        assert self.database is not None
        async with self.database.read() as connection:
            parse_rows = await (
                await connection.execute(
                    "SELECT parse_results.id, parse_results.document_id, "
                    "documents.active_parse_id, parse_results.created_at "
                    "FROM parse_results JOIN documents ON documents.id = parse_results.document_id "
                    "ORDER BY parse_results.document_id, parse_results.created_at DESC, "
                    "parse_results.id DESC"
                )
            ).fetchall()
            references = {
                (row[0], row[1])
                for row in await (
                    await connection.execute(
                        "SELECT document_id, parse_id FROM qa_records"
                    )
                ).fetchall()
            }
        grouped: defaultdict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
        for row in parse_rows:
            grouped[row[1]].append((row[0], row[3], row[2]))
        keep: set[tuple[str, str]] = set()
        for document_id, rows in grouped.items():
            keep.update((document_id, row[0]) for row in rows[: self.parse_keep])
            keep.update(
                (document_id, row[0])
                for row in rows
                if (document_id, row[0]) in references
            )
            keep.update(
                (document_id, row[0])
                for row in rows
                if row[2] is not None and row[0] == row[2]
            )
        all_ids = {(row[1], row[0]) for row in parse_rows}
        old = all_ids - keep
        if old:
            async with self.database.transaction() as connection:
                for _, parse_id in old:
                    await connection.execute("DELETE FROM parse_results WHERE id = ?", (parse_id,))
        return len(old), all_ids - old

    async def _remove_old_directories(self, root: Path, cutoff: float) -> int:
        removed = 0
        for directory in _directories(root):
            if _old_enough(directory, cutoff) and await self._remove(directory):
                removed += 1
        return removed

    async def _remove_old_exports(self, cutoff: float) -> int:
        removed = 0
        for document in _directories(self.paths.documents):
            for export in _directories(document / "exports"):
                if _old_enough(export, cutoff) and await self._remove(export):
                    removed += 1
        return removed

    async def _remove_orphan_parses(
        self, remaining: set[tuple[str, str]], cutoff: float
    ) -> int:
        removed = 0
        for document in _directories(self.paths.documents):
            try:
                document_id = str(UUID(document.name))
            except ValueError:
                continue
            for parse in _directories(document / "parses"):
                if (document_id, parse.name) in remaining:
                    continue
                if _old_enough(parse, cutoff) and await self._remove(parse):
                    removed += 1
        return removed

    async def _remove(self, directory: Path) -> bool:
        try:
            await run_blocking(shutil.rmtree, directory, True)
            return not directory.exists()
        except OSError:
            logger.warning("Could not remove stale directory %s", directory, exc_info=True)
            return False


def _directories(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return ()


def _old_enough(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False
