from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base
from easylearn.mineru.schema import MinerUArtifactKind


class ParseRun(Base):
    __tablename__ = "parse_runs"
    __table_args__ = (
        UniqueConstraint("document_id", "id", name="uq_parse_runs_document_identity"),
        ForeignKeyConstraint(
            ["preview_run_id", "document_id", "preview_asset_id"],
            ["preview_runs.id", "preview_runs.document_id", "preview_runs.preview_asset_id"],
            name="fk_parse_runs_preview_source",
        ),
        ForeignKeyConstraint(
            ["job_id", "document_id", "id"],
            ["job_runs.id", "job_runs.document_id", "job_runs.run_id"],
            name="fk_parse_runs_job_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    preview_run_id: Mapped[UUID]
    preview_asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"))
    job_id: Mapped[UUID] = mapped_column(unique=True)
    configuration: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    document_ir_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"))
    archive_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"))
    evidence: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))


class ParseArtifact(Base):
    __tablename__ = "parse_artifacts"
    __table_args__ = (UniqueConstraint("parse_run_id", "path", name="uq_parse_artifacts_path"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    parse_run_id: Mapped[UUID] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[MinerUArtifactKind] = mapped_column(String(30))
