"""Durable upload sessions and content-addressed assets."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(180), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
    )
    op.create_table(
        "uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.id")),
        sa.CheckConstraint(
            "size > 0 AND byte_offset >= 0 AND byte_offset <= size", name="upload_bounds"
        ),
        sa.CheckConstraint(
            "status IN ('CREATED','UPLOADING','UPLOADED','INVALID','EXPIRED')", name="upload_status"
        ),
    )
    op.create_table(
        "upload_chunks",
        sa.Column("upload_id", sa.Uuid(), sa.ForeignKey("uploads.id"), primary_key=True),
        sa.Column("byte_offset", sa.BigInteger(), primary_key=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(180), nullable=False),
        sa.CheckConstraint("byte_offset >= 0 AND size > 0", name="chunk_bounds"),
    )


def downgrade() -> None:
    op.drop_table("upload_chunks")
    op.drop_table("uploads")
    op.drop_table("assets")
