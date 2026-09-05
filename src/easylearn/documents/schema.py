from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from easylearn.document_ir.schema import PageGeometry, Sha256
from easylearn.previews.schema import PreviewStatus


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    client_id: UUID | None = None


class DocumentAccepted(BaseModel):
    document_id: UUID
    preview_run_id: UUID
    job_id: UUID
    status_url: str


class PreviewView(BaseModel):
    preview_run_id: UUID
    job_id: UUID
    status: PreviewStatus
    preview_asset_id: UUID | None = None
    preview_sha256: Sha256 | None = None
    pages: tuple[PageGeometry, ...] = ()


class DocumentView(BaseModel):
    document_id: UUID
    original_asset_id: UUID
    filename: str
    client_id: UUID | None
    created_at: datetime
    preview_runs: list[PreviewView]
