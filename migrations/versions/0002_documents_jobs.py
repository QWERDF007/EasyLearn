"""Documents, preview runs and transactional task delivery."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("original_asset_id", sa.Uuid(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("client_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_documents_client_id", "documents", ["client_id"])
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("checkpoint", JSONB(), nullable=False),
        sa.Column("failure", JSONB()),
        sa.Column("lease_owner", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("generation > 0", name="job_generation"),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCEL_REQUESTED','CANCELLED')",
            name="job_status",
        ),
    )
    op.create_index("ix_job_runs_document_id", "job_runs", ["document_id"])
    op.create_index("ix_job_runs_lease_expires_at", "job_runs", ["lease_expires_at"])
    op.create_table(
        "preview_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job_runs.id"), nullable=False, unique=True),
    )
    op.create_index("ix_preview_runs_document_id", "preview_runs", ["document_id"])
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job_runs.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_outbox_events_job_id", "outbox_events", ["job_id"])
    op.create_index(
        "ix_outbox_pending",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(200), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response", JSONB()),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("outbox_events")
    op.drop_table("preview_runs")
    op.drop_table("job_runs")
    op.drop_table("documents")
