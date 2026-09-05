from uuid import UUID, uuid4

from sqlalchemy import select

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.documents.models import Document, PreviewRun
from easylearn.errors import DomainError
from easylearn.idempotency import IdempotencyRecord
from easylearn.jobs.models import JobRun
from easylearn.jobs.schema import JobKind, JobStatus, RunRef
from easylearn.jobs.service import JobService
from easylearn.mineru.schema import MinerUOptions
from easylearn.parses.models import ParseRun
from easylearn.parses.schema import ParseAccepted, ParseConfiguration, ParseRequest, ParseView
from easylearn.previews.schema import PreflightReport
from easylearn.uploads.models import Asset


class ParseService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def create(self, document_id: UUID, body: ParseRequest, key: str) -> ParseAccepted:
        async with self.database.sessions.begin() as session:
            record = await IdempotencyRecord.acquire(
                session, f"create-parse:{document_id}", key, body.model_dump(mode="json")
            )
            if record.response is not None:
                return ParseAccepted.model_validate(record.response)
            if await session.get(Document, document_id) is None:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
            row = (
                await session.execute(
                    select(PreviewRun, JobRun)
                    .join(JobRun, PreviewRun.job_id == JobRun.id)
                    .where(
                        PreviewRun.id == body.preview_run_id, PreviewRun.document_id == document_id
                    )
                )
            ).one_or_none()
            if row is None:
                raise DomainError("PREVIEW_NOT_FOUND", "Document preview not found", status=404)
            preview, job = row
            if (
                job.status != JobStatus.SUCCEEDED
                or preview.preview_asset_id is None
                or preview.report is None
            ):
                raise DomainError("PREVIEW_NOT_READY", "A ready preview is required", status=409)
            profile = self.settings.mineru
            if profile is None:
                raise DomainError("MINERU_NOT_CONFIGURED", "Configure a MinerU service", status=503)
            report = PreflightReport.model_validate(preview.report)
            configuration = ParseConfiguration(
                service_url=profile.base_url,
                profile_revision=profile.profile_revision,
                local_model=(
                    self.settings.local_models[profile.local_model] if profile.local_model else None
                ),
                options=MinerUOptions(
                    **(body.options or profile.parse).model_dump(), page_count=len(report.pages)
                ),
            )
            run_id = uuid4()
            job = await JobService.enqueue(
                session, RunRef(document_id=document_id, run_id=run_id, kind=JobKind.PARSE)
            )
            session.add(
                ParseRun(
                    id=run_id,
                    document_id=document_id,
                    preview_run_id=preview.id,
                    preview_asset_id=preview.preview_asset_id,
                    job_id=job.id,
                    configuration=configuration.model_dump(mode="json"),
                )
            )
            accepted = ParseAccepted(
                document_id=document_id,
                parse_run_id=run_id,
                job_id=job.id,
                status_url=f"/api/v1/jobs/{job.id}",
            )
            record.response = accepted.model_dump(mode="json")
            return accepted

    async def list(self, document_id: UUID) -> list[ParseView]:
        async with self.database.sessions() as session:
            if await session.get(Document, document_id) is None:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
            rows = await session.execute(
                select(ParseRun, JobRun, Asset)
                .join(JobRun, ParseRun.job_id == JobRun.id)
                .join(Asset, ParseRun.preview_asset_id == Asset.id)
                .where(ParseRun.document_id == document_id)
                .order_by(JobRun.created_at, JobRun.id)
            )
            return [
                ParseView.model_validate(
                    {
                        "parse_run_id": run.id,
                        "document_id": run.document_id,
                        "preview_run_id": run.preview_run_id,
                        "preview_asset_id": asset.id,
                        "preview_sha256": asset.sha256,
                        "job_id": job.id,
                        "status": (
                            "READY"
                            if job.status == JobStatus.SUCCEEDED
                            else job.stage
                            if job.status == JobStatus.RUNNING
                            else job.status
                        ),
                        "configuration": run.configuration,
                        "created_at": job.created_at,
                    }
                )
                for run, job, asset in rows
            ]

    async def preview(self, document_id: UUID, parse_run_id: UUID) -> Asset:
        async with self.database.sessions() as session:
            asset = await session.scalar(
                select(Asset)
                .join(ParseRun, ParseRun.preview_asset_id == Asset.id)
                .where(ParseRun.document_id == document_id, ParseRun.id == parse_run_id)
            )
            if asset is None:
                raise DomainError("PARSE_NOT_FOUND", "Document parse run not found", status=404)
            return asset
