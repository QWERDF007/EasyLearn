from datetime import datetime
from pathlib import PurePath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from easylearn.document_ir.schema import Sha256
from easylearn.uploads.filetypes import INPUT_MIME


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: Sha256

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if (
            any(char in value for char in "/\\\x00\r\n")
            or PurePath(value).suffix.lower() not in INPUT_MIME
        ):
            raise ValueError("Unsupported document filename")
        return value


class UploadView(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    upload_id: UUID = Field(validation_alias="id")
    filename: str
    size: int
    sha256: Sha256
    offset: int
    status: Literal["CREATED", "UPLOADING", "UPLOADED", "INVALID", "EXPIRED"]
    expires_at: datetime
    asset_id: UUID | None


class UploadLimits(BaseModel):
    max_file_bytes: int
    max_chunk_bytes: int


class UploadCreatedView(UploadView):
    limits: UploadLimits
