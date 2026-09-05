"""Published parse objects, run-scoped artifact identities and current version."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("document_ir_asset_id", "archive_asset_id"):
        op.add_column("parse_runs", sa.Column(name, sa.Uuid(), sa.ForeignKey("assets.id")))
    op.add_column("parse_runs", sa.Column("evidence", JSONB()))
    op.create_table(
        "parse_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("parse_run_id", sa.Uuid(), sa.ForeignKey("parse_runs.id"), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.UniqueConstraint("parse_run_id", "path", name="uq_parse_artifacts_path"),
    )
    op.create_index("ix_parse_artifacts_parse_run_id", "parse_artifacts", ["parse_run_id"])
    op.create_index("ix_parse_artifacts_asset_id", "parse_artifacts", ["asset_id"])
    op.create_unique_constraint(
        "uq_parse_runs_document_identity", "parse_runs", ["document_id", "id"]
    )
    op.add_column("documents", sa.Column("active_parse_run_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_documents_active_parse",
        "documents",
        "parse_runs",
        ["id", "active_parse_run_id"],
        ["document_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_active_parse", "documents", type_="foreignkey")
    op.drop_column("documents", "active_parse_run_id")
    op.drop_constraint("uq_parse_runs_document_identity", "parse_runs", type_="unique")
    op.drop_table("parse_artifacts")
    op.drop_column("parse_runs", "evidence")
    for name in ("document_ir_asset_id", "archive_asset_id"):
        op.drop_constraint(f"fk_parse_runs_{name}_assets", "parse_runs", type_="foreignkey")
        op.drop_column("parse_runs", name)
