import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator


def validate_portable_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.name
        or path.is_absolute()
        or path.as_posix() != value
        or any(char in value for char in '\\:*?"<>|')
        or any(ord(char) < 32 for char in value)
        or any(
            part in (".", "..") or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
    ):
        raise ValueError("Export path must be canonical, relative and portable")
    return value


PortablePath = Annotated[str, AfterValidator(validate_portable_path)]


@dataclass(frozen=True)
class DataPaths:
    """Generated application paths; user filenames never become path segments."""

    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def documents(self) -> Path:
        return self.data_dir / "documents"

    @property
    def temporary(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock(self) -> Path:
        return self.data_dir / "instance.lock"

    def ensure(self) -> "DataPaths":
        for path in (self.data_dir, self.documents, self.temporary, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def document(self, document_id: UUID) -> Path:
        return self.documents / str(document_id)

    def original(self, document_id: UUID) -> Path:
        return self.document(document_id) / "original"

    def parse(self, document_id: UUID, parse_id: UUID) -> Path:
        return self.document(document_id) / "parses" / str(parse_id)

    def export(self, document_id: UUID, export_id: UUID) -> Path:
        return self.document(document_id) / "exports" / str(export_id)

    def task(self, task_id: UUID) -> Path:
        return self.temporary / str(task_id)

    def remove_document(self, document_id: UUID) -> None:
        shutil.rmtree(self.document(document_id), ignore_errors=False)
