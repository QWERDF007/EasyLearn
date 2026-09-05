"""Immutable preview result references and geometry metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("preview_runs", sa.Column("preview_asset_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_preview_runs_preview_asset_id_assets",
        "preview_runs",
        "assets",
        ["preview_asset_id"],
        ["id"],
    )
    op.add_column("preview_runs", sa.Column("report", JSONB()))


def downgrade() -> None:
    op.drop_column("preview_runs", "report")
    op.drop_constraint(
        "fk_preview_runs_preview_asset_id_assets", "preview_runs", type_="foreignkey"
    )
    op.drop_column("preview_runs", "preview_asset_id")
