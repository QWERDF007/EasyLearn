import asyncio
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import PurePath
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.errors import DomainError
from easylearn.storage import LocalStorage
from easylearn.uploads.filetypes import validate_input
from easylearn.uploads.models import Asset, Upload, UploadChunk
from easylearn.uploads.schema import UploadRequest, UploadView


class UploadService:
    def __init__(self, database: Database, storage: LocalStorage, settings: Settings) -> None:
        self.database = database
        self.storage = storage
        self.settings = settings

    async def create(self, request: UploadRequest) -> UploadView:
        if request.size > self.settings.upload_max_bytes:
            raise DomainError("UPLOAD_TOO_LARGE", "Document exceeds upload limit", status=413)
        upload = Upload(
            **request.model_dump(),
            expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.upload_session_ttl),
        )
        async with self.database.sessions.begin() as session:
            session.add(upload)
            await session.flush()
        return UploadView.model_validate(upload)

    async def get(self, upload_id: UUID) -> UploadView:
        async with self.database.sessions.begin() as session:
            upload = await session.scalar(
                select(Upload).where(Upload.id == upload_id).with_for_update()
            )
            if upload is None:
                raise DomainError("UPLOAD_NOT_FOUND", "Upload not found", status=404)
            if upload.status in ("CREATED", "UPLOADING") and upload.expires_at <= datetime.now(UTC):
                upload.status = "EXPIRED"
            return UploadView.model_validate(upload)

    async def append(self, upload_id: UUID, offset: int, content: bytes) -> int:
        if not content or len(content) > self.settings.upload_chunk_bytes:
            raise DomainError("UPLOAD_CHUNK_INVALID", "Chunk exceeds limit or is empty", status=413)
        snapshot = await self.get(upload_id)
        if snapshot.status == "EXPIRED":
            raise DomainError("UPLOAD_EXPIRED", "Upload session expired", status=410)
        if snapshot.status not in ("CREATED", "UPLOADING"):
            raise DomainError("UPLOAD_NOT_WRITABLE", "Upload cannot accept bytes", status=409)
        stored = await asyncio.to_thread(self.storage.write, (content,))
        async with self.database.sessions.begin() as session:
            upload = await session.scalar(
                select(Upload).where(Upload.id == upload_id).with_for_update()
            )
            if upload is None:
                raise DomainError("UPLOAD_NOT_FOUND", "Upload not found", status=404)
            if upload.status not in ("CREATED", "UPLOADING"):
                raise DomainError("UPLOAD_NOT_WRITABLE", "Upload cannot accept bytes", status=409)
            if upload.expires_at <= datetime.now(UTC):
                raise DomainError("UPLOAD_EXPIRED", "Upload session expired", status=410)
            if offset != upload.offset:
                previous = await session.get(UploadChunk, (upload_id, offset))
                if previous and previous.sha256 == stored.sha256 and previous.size == stored.size:
                    return upload.offset
                raise DomainError("UPLOAD_OFFSET_MISMATCH", "Incorrect upload offset", status=409)
            if upload.offset + stored.size > upload.size:
                raise DomainError("UPLOAD_TOO_LARGE", "Bytes exceed declared length", status=413)
            session.add(
                UploadChunk(
                    upload_id=upload_id,
                    offset=offset,
                    size=stored.size,
                    sha256=stored.sha256,
                    storage_key=stored.key,
                )
            )
            upload.offset += stored.size
            upload.status = "UPLOADING"
            return upload.offset

    async def complete(self, upload_id: UUID) -> UploadView:
        snapshot = await self.get(upload_id)
        if snapshot.status == "EXPIRED":
            raise DomainError("UPLOAD_EXPIRED", "Upload session expired", status=410)
        async with self.database.sessions() as session:
            upload = await session.get(Upload, upload_id)
            if upload is None:
                raise DomainError("UPLOAD_NOT_FOUND", "Upload not found", status=404)
            if upload.status == "UPLOADED":
                return UploadView.model_validate(upload)
            if upload.status not in ("CREATED", "UPLOADING"):
                raise DomainError("UPLOAD_NOT_WRITABLE", "Upload cannot be completed", status=409)
            if upload.offset != upload.size:
                raise DomainError("UPLOAD_INCOMPLETE", "Upload has missing bytes", status=409)
            keys = (
                await session.scalars(
                    select(UploadChunk.storage_key)
                    .where(UploadChunk.upload_id == upload_id)
                    .order_by(UploadChunk.offset)
                )
            ).all()
        stored = await asyncio.to_thread(
            self.storage.write, chain.from_iterable(self.storage.read(key) for key in keys)
        )
        failure: DomainError | None = None
        mime = ""
        if stored.sha256 != upload.sha256 or stored.size != upload.size:
            failure = DomainError("UPLOAD_HASH_MISMATCH", "File checksum does not match")
        else:
            try:
                mime = await asyncio.to_thread(
                    validate_input,
                    self.storage.path(stored.key),
                    PurePath(upload.filename).suffix.lower(),
                )
            except DomainError as exc:
                failure = exc
        async with self.database.sessions.begin() as session:
            upload = await session.scalar(
                select(Upload).where(Upload.id == upload_id).with_for_update()
            )
            assert upload is not None
            if upload.status == "UPLOADED":
                return UploadView.model_validate(upload)
            if upload.status not in ("CREATED", "UPLOADING"):
                raise DomainError("UPLOAD_NOT_WRITABLE", "Upload cannot be completed", status=409)
            expired = upload.expires_at <= datetime.now(UTC)
            if expired:
                upload.status = "EXPIRED"
            elif failure is None:
                await session.execute(
                    insert(Asset)
                    .values(
                        id=uuid4(),
                        sha256=stored.sha256,
                        size=stored.size,
                        storage_key=stored.key,
                        mime=mime,
                    )
                    .on_conflict_do_nothing(index_elements=[Asset.sha256])
                )
                upload.asset_id = await session.scalar(
                    select(Asset.id).where(Asset.sha256 == stored.sha256)
                )
                upload.status = "UPLOADED"
            else:
                upload.status = "INVALID"
            view = UploadView.model_validate(upload)
        if expired:
            raise DomainError("UPLOAD_EXPIRED", "Upload session expired", status=410)
        if failure is not None:
            raise failure
        return view
