from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class JobKind(StrEnum):
    PARSE = "parse"
    TRANSLATE = "translate"
    EXPORT = "export"
    QA = "qa"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (self.SUCCEEDED, self.FAILED, self.CANCELLED)


class JobFailure(BaseModel):
    code: str
    message: str
    retryable: bool = False


class TaskView(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: UUID
    server_boot_id: UUID
    document_id: UUID
    kind: JobKind
    scope: dict[str, JsonValue]
    status: JobStatus
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str = ""
    cancel_requested: bool = False
    created_at: datetime
    finished_at: datetime | None = None
    result_ref: dict[str, JsonValue] | None = None
    failure: JobFailure | None = None
    answer: str | None = None
