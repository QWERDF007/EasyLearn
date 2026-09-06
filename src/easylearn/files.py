"""Atomic, document-scoped filesystem operations."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from uuid import UUID

from filelock import FileLock, Timeout

from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.paths import DataPaths


class InstanceLock:
    """Keep a second application process from using the same data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = FileLock(str(path))
        self._held = False

    def acquire(self) -> None:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError(
                "Another EasyLearn instance already uses this data directory"
            ) from None
        self._held = True

    def release(self) -> None:
        if self._held:
            self._lock.release()
            self._held = False


class DocumentFiles:
    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def create_document(self, document_id: UUID) -> Path:
        directory = self.paths.document(document_id)
        (directory / "original").mkdir(parents=True, exist_ok=False)
        (directory / "parses").mkdir()
        (directory / "exports").mkdir()
        return directory

    def original_path(self, document_id: UUID, suffix: str) -> Path:
        return self.paths.original(document_id) / f"source{suffix}"

    def parse_path(self, document_id: UUID, parse_id: UUID, name: str) -> Path:
        return self.paths.parse(document_id, parse_id) / name

    def export_path(self, document_id: UUID, export_id: UUID, name: str) -> Path:
        return self.paths.export(document_id, export_id) / name

    async def save_upload(self, upload: object, destination: Path, limit: int) -> tuple[int, str]:
        """Stream an UploadFile-like object to an fsynced temporary file."""

        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / f".{destination.name}.{os.getpid()}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as target:
                while True:
                    chunk = await upload.read(1024 * 1024)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise DomainError(
                            "FILE_TOO_LARGE",
                            "Uploaded file exceeds the configured limit",
                            status=413,
                        )
                    target.write(chunk)
                    digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return size, digest.hexdigest()

    def publish_directory(self, source: Path, destination: Path) -> None:
        """Move a fully validated temporary result into its generated directory."""

        if destination.exists():
            raise DomainError("RESULT_CONFLICT", "Result directory already exists", status=409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)

    async def remove_document(self, document_id: UUID) -> None:
        directory = self.paths.document(document_id)
        if directory.exists():
            await run_blocking(shutil.rmtree, directory)

    def remove_task_directory(self, task_id: UUID) -> None:
        directory = self.paths.task(task_id)
        if directory.exists():
            shutil.rmtree(directory)

    def read_relative(self, document_id: UUID, relative: str) -> Path:
        root = self.paths.document(document_id).resolve()
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise DomainError("FILE_NOT_FOUND", "File is outside the document", status=404)
        if not path.is_file():
            raise DomainError("FILE_NOT_FOUND", "Document file not found", status=404)
        return path
