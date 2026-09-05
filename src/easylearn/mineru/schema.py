from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from easylearn.document_ir.schema import Sha256
from easylearn.paths import PortablePath

MinerUBackend = Literal[
    "pipeline", "vlm-engine", "hybrid-engine", "vlm-http-client", "hybrid-http-client"
]


@dataclass(frozen=True)
class CapturedResponse[T]:
    value: T
    raw_body: bytes


class MinerULimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    download_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    metadata_max_bytes: int = Field(default=1024 * 1024, gt=0)
    submission_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    download_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)


class MinerUOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    page_count: int = Field(gt=0, le=100000)
    backend: MinerUBackend = "vlm-engine"
    server_url: AnyHttpUrl | None = None
    language: Literal["ch", "en"] = "ch"
    parse_method: Literal["auto", "txt", "ocr"] = "auto"
    effort: Literal["medium", "high"] = "medium"
    formula_enable: bool = True
    table_enable: bool = True
    image_analysis: bool = False

    @model_validator(mode="after")
    def validate_inference_server(self) -> Self:
        if self.backend.endswith("-http-client") != (self.server_url is not None):
            raise ValueError("server_url is required only for HTTP-client backends")
        return self


class MinerUTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: UUID
    status: Literal["pending", "processing", "completed", "failed"]
    backend: MinerUBackend
    file_names: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    queued_ahead: int | None = Field(default=None, ge=0)


class MinerUCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    cancel: Literal[False] = False
    reconcile: Literal[False] = False
    idempotent_submission: Literal[False] = False


class MinerUHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["healthy"]
    version: Literal["3.4.5"]
    protocol_version: Literal[2]
    task_retention_seconds: int = Field(ge=0)


MinerUArtifactKind = Literal[
    "markdown", "middle", "model", "content_list", "content_list_v2", "original", "image"
]


class MinerUArchiveLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_archive_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    max_expanded_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_member_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    max_members: int = Field(default=10000, gt=0)


class MinerUArchiveMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: PortablePath
    kind: MinerUArtifactKind
    sha256: Sha256
    size: int = Field(ge=0)


class MinerUArchiveManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: Sha256
    size: int = Field(gt=0)
    members: tuple[MinerUArchiveMember, ...]
