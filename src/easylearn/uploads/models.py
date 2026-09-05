from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (
        CheckConstraint(
            "size > 0 AND byte_offset >= 0 AND byte_offset <= size", name="upload_bounds"
        ),
        CheckConstraint(
            "status IN ('CREATED','UPLOADING','UPLOADED','INVALID','EXPIRED')", name="upload_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    offset: Mapped[int] = mapped_column("byte_offset", BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (CheckConstraint("byte_offset >= 0 AND size > 0", name="chunk_bounds"),)

    upload_id: Mapped[UUID] = mapped_column(ForeignKey("uploads.id"), primary_key=True)
    offset: Mapped[int] = mapped_column("byte_offset", BigInteger, primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(180))
