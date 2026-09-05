from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base
from easylearn.jobs.schema import JobKind, JobStatus, RunRef


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("id", "document_id", "run_id", name="uq_job_runs_identity"),
        CheckConstraint("generation > 0", name="job_generation"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCEL_REQUESTED','CANCELLED')",
            name="job_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(unique=True)
    kind: Mapped[JobKind] = mapped_column(String(30))
    status: Mapped[JobStatus] = mapped_column(String(30), default=JobStatus.QUEUED)
    generation: Mapped[int] = mapped_column(default=1)
    stage: Mapped[str] = mapped_column(String(40), default="QUEUED")
    checkpoint: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    failure: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def run_ref(self) -> RunRef:
        return RunRef(document_id=self.document_id, run_id=self.run_id, kind=self.kind)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_pending", "created_at", postgresql_where="delivered_at IS NULL"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("job_runs.id"), index=True)
    generation: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
