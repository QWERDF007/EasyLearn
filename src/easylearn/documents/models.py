from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    original_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    filename: Mapped[str] = mapped_column(String(255))
    client_id: Mapped[UUID | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PreviewRun(Base):
    __tablename__ = "preview_runs"
    __table_args__ = (
        UniqueConstraint("id", "document_id", "preview_asset_id", name="uq_preview_runs_source"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_runs.id"), unique=True)
    preview_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"))
    report: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
