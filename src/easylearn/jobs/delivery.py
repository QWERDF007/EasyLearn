from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update

from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.jobs.models import OutboxEvent


@dataclass(frozen=True)
class Delivery:
    event_id: UUID
    job_id: UUID
    generation: int
    token: UUID


class Outbox:
    def __init__(self, database: Database, *, lease_duration: timedelta = timedelta(seconds=30)):
        if lease_duration <= timedelta(0):
            raise ValueError("Outbox lease must be positive")
        self.database = database
        self.lease_duration = lease_duration

    async def claim(self, limit: int = 100) -> list[Delivery]:
        if not 1 <= limit <= 1000:
            raise ValueError("Outbox batch limit must be in [1, 1000]")
        async with self.database.sessions.begin() as session:
            events = await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.delivered_at.is_(None),
                    or_(
                        OutboxEvent.lease_expires_at.is_(None),
                        OutboxEvent.lease_expires_at <= func.clock_timestamp(),
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            deliveries = []
            for event in events:
                event.claim_token = uuid4()
                event.lease_expires_at = now + self.lease_duration
                deliveries.append(
                    Delivery(event.id, event.job_id, event.generation, event.claim_token)
                )
            return deliveries

    async def acknowledge(self, delivery: Delivery) -> None:
        async with self.database.sessions.begin() as session:
            acknowledged = await session.scalar(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == delivery.event_id,
                    OutboxEvent.claim_token == delivery.token,
                    OutboxEvent.lease_expires_at > func.clock_timestamp(),
                    OutboxEvent.delivered_at.is_(None),
                )
                .values(
                    delivered_at=func.clock_timestamp(), claim_token=None, lease_expires_at=None
                )
                .returning(OutboxEvent.id)
            )
            if acknowledged is None:
                raise DomainError(
                    "OUTBOX_LEASE_LOST", "Delivery lease is no longer owned", status=409
                )
