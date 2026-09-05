from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from easylearn.document_ir.schema import Identifier, Sha256
from easylearn.inference.config import LocalModel
from easylearn.mineru.schema import MinerUOptions, MinerUParseOptions, MinerUTask
from easylearn.storage import StoredObject
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


class SubmissionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    request_id: UUID
    generation: int = Field(gt=0)
    source_sha256: Sha256
    configuration_sha256: Sha256
    state: Literal["SUBMITTING", "SUBMIT_UNKNOWN", "REJECTED", "ACCEPTED"] = "SUBMITTING"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    response: StoredObject | None = None
    task: MinerUTask | None = None


class ParseCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    submissions: list[SubmissionAttempt] = Field(default_factory=list)
    task: MinerUTask | None = None
    task_response: StoredObject | None = None
    archive: StoredObject | None = None

    @property
    def upstream_may_be_running(self) -> bool:
        if not self.submissions:
            return False
        last = self.submissions[-1]
        if last.state in ("SUBMITTING", "SUBMIT_UNKNOWN"):
            return True
        task = self.task or last.task
        return task is not None and task.status in ("pending", "processing")


class ParseView(BaseModel):
    parse_run_id: UUID
    document_id: UUID
    preview_run_id: UUID
    preview_asset_id: UUID
    preview_sha256: Sha256
    job_id: UUID
    upstream_may_be_running: bool
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
