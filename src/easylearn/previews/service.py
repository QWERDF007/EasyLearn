from uuid import UUID, uuid4

from sqlalchemy import select

from easylearn.assets import Asset
from easylearn.config import Settings
from easylearn.database import Database
from easylearn.documents.models import Document, PreviewRun
from easylearn.errors import DomainError
from easylearn.jobs.service import JobService
from easylearn.previews.pdf import PdfPreflight
from easylearn.storage import LocalStorage


class PreviewService:
    def __init__(self, database: Database, storage: LocalStorage, settings: Settings) -> None:
        self.database = database
        self.storage = storage
        self.jobs = JobService(database)
        self.preflight = PdfPreflight(
            settings.preview_limits, timeout=settings.preview_timeout_seconds
        )

    async def execute(self, job_id: UUID, *, generation: int) -> None:
        lease = await self.jobs.acquire(job_id, generation=generation, owner=uuid4())
        if lease is None:
            return
        try:
            async with self.database.sessions() as session:
                row = (
                    await session.execute(
                        select(PreviewRun, Asset)
                        .join(Document, PreviewRun.document_id == Document.id)
                        .join(Asset, Document.original_asset_id == Asset.id)
                        .where(PreviewRun.job_id == job_id)
                    )
                ).one()
                preview, asset = row
            if asset.mime != "application/pdf":
                raise DomainError(
                    "PREVIEW_CONVERTER_UNAVAILABLE",
                    "This input requires a configured document converter",
                    retryable=True,
                )
            await self.jobs.save_checkpoint(lease, stage="VALIDATING", data={})
            report = await self.jobs.supervise(
                lease,
                self.preflight.inspect(self.storage.path(asset.storage_key), asset.sha256),
            )
            async with self.jobs.publication(lease) as session:
                run = await session.get(PreviewRun, preview.id)
                assert run is not None
                run.preview_asset_id = asset.id
                run.report = report.model_dump(mode="json")
        except DomainError as exc:
            await self.jobs.handle_error(lease, exc)
