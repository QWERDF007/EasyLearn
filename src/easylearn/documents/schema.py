from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from easylearn.jobs.schema import TaskView

TranslationStatus = Literal["none", "partial", "completed"]


class ParseResultView(BaseModel):
    parse_id: UUID
    created_at: datetime
    pages: int = Field(gt=0)
    preview_file_id: str
    ir_file_id: str
    raw_file_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    has_translation: bool = False
    total_units: int = 0
    translated_units: int = 0
    translation_status: TranslationStatus = "none"
    integrity_status: Literal["valid", "corrupt"] = "valid"
    corrupt_reasons: tuple[str, ...] = ()


class DocumentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    name: str
    favorite: bool
    created_at: datetime
    active_parse_id: UUID | None = None
    original_file_id: str = "original"
    size_bytes: int | None = None
    parse_results: tuple[ParseResultView, ...] = ()
    tasks: tuple[TaskView, ...] = ()
    has_translation: bool = False
    total_units: int = 0
    translated_units: int = 0
    translation_status: TranslationStatus = "none"


class DocumentListView(BaseModel):
    documents: tuple[DocumentView, ...]


class FavoriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    favorite: bool
