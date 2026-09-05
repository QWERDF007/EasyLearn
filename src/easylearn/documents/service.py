from uuid import UUID, uuid4

from sqlalchemy import select

from easylearn.assets import Asset
from easylearn.database import Database
from easylearn.documents.models import Document, PreviewRun
from easylearn.documents.schema import (
    DocumentAccepted,
    DocumentRequest,
    DocumentView,
    PreviewView,
)
from easylearn.errors import DomainError
from easylearn.idempotency import IdempotencyRecord
from easylearn.jobs.models import JobRun
from easylearn.jobs.schema import JobKind, JobStatus, RunRef
from easylearn.jobs.service import JobService
from easylearn.parses.models import ParseArtifact, ParseRun
from easylearn.previews.schema import PreflightReport
from easylearn.uploads.models import Upload


class DocumentService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, body: DocumentRequest, key: str) -> DocumentAccepted:
        async with self.database.sessions.begin() as session:
            record = await IdempotencyRecord.acquire(
                session, "create-document", key, body.model_dump(mode="json")
            )
            if record.response is not None:
                return DocumentAccepted.model_validate(record.response)
            upload = await session.get(Upload, body.upload_id)
            if upload is None:
                raise DomainError("UPLOAD_NOT_FOUND", "Upload not found", status=404)
            if upload.status != "UPLOADED" or upload.asset_id is None:
                raise DomainError(
                    "UPLOAD_INCOMPLETE", "Complete upload before creating document", status=409
                )
            document = Document(
                original_asset_id=upload.asset_id,
                filename=upload.filename,
                client_id=body.client_id,
            )
            session.add(document)
            await session.flush()
            preview_id = uuid4()
            job = await JobService.enqueue(
                session, RunRef(document_id=document.id, run_id=preview_id, kind=JobKind.PREVIEW)
            )
            session.add(PreviewRun(id=preview_id, document_id=document.id, job_id=job.id))
            accepted = DocumentAccepted(
                document_id=document.id,
                preview_run_id=preview_id,
                job_id=job.id,
                status_url=f"/api/v1/jobs/{job.id}",
            )
            record.response = accepted.model_dump(mode="json")
            return accepted

    async def get(self, document_id: UUID) -> DocumentView:
        async with self.database.sessions() as session:
            document = await session.get(Document, document_id)
            if document is None:
                raise DomainError("DOCUMENT_NOT_FOUND", "Document not found", status=404)
            rows = await session.execute(
                select(PreviewRun, JobRun)
                .join(JobRun, PreviewRun.job_id == JobRun.id)
                .where(PreviewRun.document_id == document_id)
                .order_by(JobRun.created_at, JobRun.id)
            )
            previews = []
            for run, job in rows:
                report = PreflightReport.model_validate(run.report) if run.report else None
                if job.status == JobStatus.SUCCEEDED:
                    status = "READY"
                elif job.status == JobStatus.RUNNING:
                    status = "VALIDATING" if job.stage == "QUEUED" else job.stage
                else:
                    status = job.status
                previews.append(
                    PreviewView.model_validate(
                        {
                            "preview_run_id": run.id,
                            "job_id": job.id,
                            "status": status,
                            "preview_asset_id": run.preview_asset_id,
                            "preview_sha256": report.sha256 if report else None,
                            "pages": report.pages if report else (),
                        }
                    )
                )
            return DocumentView(
                document_id=document.id,
                original_asset_id=document.original_asset_id,
                filename=document.filename,
                client_id=document.client_id,
                active_parse_run_id=document.active_parse_run_id,
                created_at=document.created_at,
                preview_runs=previews,
            )

    async def asset(self, document_id: UUID, asset_id: UUID) -> Asset:
        async with self.database.sessions() as session:
            belongs = (
                select(PreviewRun.id)
                .where(
                    PreviewRun.document_id == document_id, PreviewRun.preview_asset_id == asset_id
                )
                .exists()
            )
            asset = await session.scalar(select(Asset).where(Asset.id == asset_id, belongs))
            if asset is None:
                asset = await session.scalar(
                    select(Asset)
                    .join(ParseArtifact, ParseArtifact.asset_id == Asset.id)
                    .join(ParseRun, ParseArtifact.parse_run_id == ParseRun.id)
                    .join(JobRun, ParseRun.job_id == JobRun.id)
                    .where(
                        ParseArtifact.id == asset_id,
                        ParseArtifact.kind == "image",
                        ParseRun.document_id == document_id,
                        JobRun.status == JobStatus.SUCCEEDED,
                    )
                )
            if asset is None:
                raise DomainError(
                    "ASSET_NOT_FOUND", "Published document asset not found", status=404
                )
            return asset
