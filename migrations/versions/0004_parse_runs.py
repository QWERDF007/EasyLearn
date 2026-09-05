"""Fixed preview and engine configuration for each parse run."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_preview_runs_source", "preview_runs", ["id", "document_id", "preview_asset_id"]
    )
    op.create_unique_constraint("uq_job_runs_identity", "job_runs", ["id", "document_id", "run_id"])
    op.create_table(
        "parse_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("preview_run_id", sa.Uuid(), nullable=False),
        sa.Column("preview_asset_id", sa.Uuid(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("configuration", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["preview_run_id", "document_id", "preview_asset_id"],
            ["preview_runs.id", "preview_runs.document_id", "preview_runs.preview_asset_id"],
            name="fk_parse_runs_preview_source",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "document_id", "id"],
            ["job_runs.id", "job_runs.document_id", "job_runs.run_id"],
            name="fk_parse_runs_job_identity",
        ),
    )
    op.create_index("ix_parse_runs_document_id", "parse_runs", ["document_id"])


def downgrade() -> None:
    op.drop_table("parse_runs")
    op.drop_constraint("uq_job_runs_identity", "job_runs", type_="unique")
    op.drop_constraint("uq_preview_runs_source", "preview_runs", type_="unique")
