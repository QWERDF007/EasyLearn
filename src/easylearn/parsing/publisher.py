from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from easylearn.config import Settings
from easylearn.execution import run_blocking
from easylearn.files import DocumentFiles
from easylearn.jobs.schema import JsonValue
from easylearn.persistence.documents import DocumentStore
from easylearn.previews.schema import PreflightReport
from easylearn.publication import ArtifactPublisher, PublicationKind

logger = logging.getLogger(__name__)


class ParsePublisher:
    """Publishes validated parse artifacts to durable storage and handles parse retention."""

    def __init__(
        self,
        *,
        document_store: DocumentStore,
        files: DocumentFiles,
        publisher: ArtifactPublisher,
        settings: Settings,
        manager: Any,
    ) -> None:
        self.document_store = document_store
        self.files = files
        self.publisher = publisher
        self.settings = settings
        self.manager = manager

    async def publish(
        self,
        *,
        document_id: UUID,
        parse_id: UUID,
        task_id: UUID,
        report: PreflightReport,
        evidence: Any,
        staging_directory: Any,
        total_units: int,
        office: Any,
        context: Any,
    ) -> dict[str, JsonValue]:
        destination = self.files.paths.parse(document_id, parse_id)
        relative_root = destination.relative_to(self.files.paths.document(document_id))
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
        timestamp = datetime.now(UTC).isoformat()

        async def record_parse(connection: Any) -> None:
            await self.document_store.record_parse(
                document_id=document_id,
                parse_id=parse_id,
                preview_path=str(relative_root / "preview.pdf"),
                ir_path=str(relative_root / "document.json"),
                raw_path=str(relative_root / "mineru.zip"),
                pages=len(report.pages),
                metadata=metadata,
                timestamp=timestamp,
                set_active=True,
                connection=connection,
            )

        async def publish_operation() -> object:
            return await self.publisher.publish(
                document_id=document_id,
                artifact_id=parse_id,
                kind=PublicationKind.PARSE,
                staging=staging_directory,
                destination=destination,
                payload={
                    "parse_id": str(parse_id),
                    "preview_path": str(relative_root / "preview.pdf"),
                    "ir_path": str(relative_root / "document.json"),
                    "raw_path": str(relative_root / "mineru.zip"),
                    "pages": len(report.pages),
                    "metadata": metadata,
                    "created_at": timestamp,
                    "set_active": True,
                },
                record=record_parse,
            )

        await context.publish(result_ref, publish_operation)
        logger.info(
            "Parse published: document_id=%s, parse_id=%s, pages=%d",
            document_id,
            parse_id,
            len(report.pages),
        )
        return result_ref

    async def prune(self, document_id: UUID) -> None:
        await self.manager.run_exclusive(lambda: self._prune_locked(document_id))

    async def _prune_locked(self, document_id: UUID) -> None:
        keep = self.settings.files.keep_parse_versions
        protected = self.manager.active_parse_ids(document_id)
        pruned_ids = await self.document_store.prune_unreferenced_parses(
            document_id=document_id,
            keep_count=keep,
            protected_ids=protected,
        )
        for pid in pruned_ids:
            await run_blocking(
                shutil.rmtree, self.files.paths.parse(document_id, pid), True
            )
