from uuid import UUID, uuid4

from sqlalchemy import select

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
from easylearn.jobs.schema import JobKind, RunRef
from easylearn.jobs.service import JobService
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
            return DocumentView(
                document_id=document.id,
                original_asset_id=document.original_asset_id,
                filename=document.filename,
                client_id=document.client_id,
                created_at=document.created_at,
                preview_runs=[
                    PreviewView(preview_run_id=run.id, job_id=job.id, status=job.status)
                    for run, job in rows
                ],
            )
