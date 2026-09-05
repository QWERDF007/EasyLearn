from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from easylearn.document_ir.schema import Identifier, Sha256
from easylearn.inference.config import LocalModel
from easylearn.mineru.schema import MinerUOptions, MinerUParseOptions
from easylearn.urls import ServiceUrl


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_run_id: UUID
    options: MinerUParseOptions | None = Field(
        default=None,
        description="Omit to use the configured profile; otherwise replace all parse options.",
    )


class ParseConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service_url: ServiceUrl
    profile_revision: Identifier
    local_model: LocalModel | None = None
    options: MinerUOptions


class ParseAccepted(BaseModel):
    document_id: UUID
    parse_run_id: UUID
    job_id: UUID
    status_url: str


class ParseView(BaseModel):
    parse_run_id: UUID
    document_id: UUID
    preview_run_id: UUID
    preview_asset_id: UUID
    preview_sha256: Sha256
    job_id: UUID
    status: Literal[
        "QUEUED",
        "SUBMITTING",
        "SUBMIT_UNKNOWN",
        "RUNNING",
        "DOWNLOADING",
        "NORMALIZING",
        "READY",
        "FAILED",
        "CANCEL_REQUESTED",
        "CANCELLED",
    ]
    configuration: ParseConfiguration
    created_at: datetime
