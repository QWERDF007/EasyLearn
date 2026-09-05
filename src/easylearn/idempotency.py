import hashlib
import json

from pydantic import JsonValue
from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from easylearn.database import Base
from easylearn.errors import DomainError


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    scope: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)

    @classmethod
    async def acquire(
        cls, session: AsyncSession, scope: str, key: str, payload: dict[str, JsonValue]
    ) -> "IdempotencyRecord":
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await session.execute(
            insert(cls)
            .values(scope=scope, key=key, request_sha256=digest)
            .on_conflict_do_nothing(index_elements=[cls.scope, cls.key])
        )
        record = await session.scalar(
            select(cls).where(cls.scope == scope, cls.key == key).with_for_update()
        )
        assert record is not None
        if record.request_sha256 != digest:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT", "Key was used with different parameters", status=409
            )
        return record
