from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base
from easylearn.errors import DomainError
from easylearn.storage import StoredObject


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(180))
    mime: Mapped[str] = mapped_column(String(100))

    @classmethod
    async def register(cls, session: AsyncSession, stored: StoredObject, mime: str) -> "Asset":
        return (await cls.register_many(session, [(stored, mime)]))[stored.sha256]

    @classmethod
    async def register_many(
        cls, session: AsyncSession, objects: Sequence[tuple[StoredObject, str]]
    ) -> dict[str, "Asset"]:
        """Register in digest order with bounded batches, sharing locks across publishers."""
        unique: dict[str, tuple[StoredObject, str]] = {}
        for stored, mime in objects:
            if stored.sha256 in unique and unique[stored.sha256][0] != stored:
                raise DomainError("STORAGE_CORRUPT", "Conflicting object metadata")
            unique[stored.sha256] = stored, mime
        hashes = sorted(unique)
        registered = {}
        for start in range(0, len(hashes), 1000):
            batch = hashes[start : start + 1000]
            await session.execute(
                insert(cls)
                .values(
                    [
                        dict(
                            id=uuid4(),
                            sha256=sha,
                            size=unique[sha][0].size,
                            storage_key=unique[sha][0].key,
                            mime=unique[sha][1],
                        )
                        for sha in batch
                    ]
                )
                .on_conflict_do_nothing(index_elements=[cls.sha256])
            )
            for asset in await session.scalars(select(cls).where(cls.sha256.in_(batch))):
                stored = unique[asset.sha256][0]
                if (asset.size, asset.storage_key) != (stored.size, stored.key):
                    raise DomainError(
                        "STORAGE_CORRUPT", "Stored object registration disagrees with content"
                    )
                registered[asset.sha256] = asset
        return registered
