from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class JobKind(StrEnum):
    PREVIEW = "PREVIEW"
    PARSE = "PARSE"

    @property
    def queue_name(self) -> str:
        return self.value.lower()


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class RunRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    run_id: UUID
    kind: JobKind


class JobFailure(BaseModel):
    code: str
    message: str
    retryable: bool


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    job_id: UUID = Field(validation_alias="id")
    run_ref: RunRef
    status: JobStatus
    generation: int
    stage: str
    checkpoint: dict[str, JsonValue]
    created_at: datetime
    failure: JobFailure | None
