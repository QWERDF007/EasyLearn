import hashlib
import os
import re
from collections.abc import AsyncIterable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from uuid import uuid4

from easylearn.errors import DomainError
from easylearn.execution import run_blocking


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size: int
    key: str


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "staging").mkdir(exist_ok=True)

    def write(self, chunks: Iterable[bytes]) -> StoredObject:
        temporary = self.root / "staging" / uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                for chunk in chunks:
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            key = "objects/" + digest.hexdigest()
            target = self.path(key)
            try:
                os.link(temporary, target)
            except FileExistsError:
                with target.open("rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != digest.hexdigest():
                        raise DomainError(
                            "STORAGE_CORRUPT", "Stored object checksum mismatch", status=500
                        ) from None
            return StoredObject(sha256=digest.hexdigest(), size=size, key=key)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, key: str) -> Iterator[bytes]:
        digest = hashlib.sha256()
        with self.path(key).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                yield chunk
        if digest.hexdigest() != key.removeprefix("objects/"):
            raise DomainError("STORAGE_CORRUPT", "Stored object checksum mismatch", status=500)

    async def write_stream(self, chunks: AsyncIterable[bytes]) -> StoredObject:
        """Bound memory while keeping the synchronous CAS publication path authoritative."""
        with TemporaryFile(dir=self.root / "staging") as spool:
            async for chunk in chunks:
                await run_blocking(spool.write, chunk)
            spool.seek(0)
            return await run_blocking(self.write, iter(lambda: spool.read(1024 * 1024), b""))

    def path(self, key: str) -> Path:
        if re.fullmatch(r"objects/[a-f0-9]{64}", key) is None:
            raise DomainError("ASSET_KEY_INVALID", "Invalid object key")
        return self.root / key

    def check(self) -> None:
        with TemporaryFile(dir=self.root / "staging") as probe:
            probe.write(b"easylearn-storage-probe")
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            if probe.read() != b"easylearn-storage-probe":
                raise OSError("Storage readback failed")
