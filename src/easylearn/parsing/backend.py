from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

from easylearn.config import Settings
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.mineru.archive import result_root
from easylearn.mineru.embedded import EmbeddedMinerU
from easylearn.mineru.models import MinerUModelCatalog
from easylearn.mineru.schema import MinerUOptions
from easylearn.storage import LocalStorage, StoredObject

logger = logging.getLogger(__name__)


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            yield chunk


@dataclass(frozen=True)
class BackendExecutionResult:
    archive_object: StoredObject
    preview_object: StoredObject


class ParseBackendExecutor:
    """Executes backend OCR / VLM inference and bundles outputs into content-addressable storage."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_catalog = MinerUModelCatalog(settings.mineru)
        self.mineru = EmbeddedMinerU(
            self.model_catalog.default_model_path,
            timeout=settings.mineru.timeout_seconds,
        )

    async def close(self) -> None:
        await self.mineru.close()

    def package_output(
        self, output: Path, archive_path: Path, options: MinerUOptions, input_pdf: Path
    ) -> None:
        middle = next(output.rglob("input_middle.json"), None)
        if middle is None:
            raise DomainError("MINERU_RESULT_INVALID", "MinerU output is missing input_middle.json")
        root = middle.parent
        archive_root = result_root(options)
        with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
            names: set[str] = set()
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                name = str(archive_root / PurePosixPath(path.relative_to(root).as_posix()))
                names.add(name.casefold())
                archive.write(path, name)
            original_name = str(archive_root / "input_origin.pdf")
            if original_name.casefold() not in names:
                archive.write(input_pdf, original_name)

    async def execute(
        self,
        *,
        input_pdf: Path,
        task_directory: Path,
        cas: LocalStorage,
        options: MinerUOptions,
        context: Any,
        model_id: str | None = None,
    ) -> BackendExecutionResult:
        preview_object = await run_blocking(cas.write, _file_chunks(input_pdf))
        output = task_directory / "mineru-output"
        output.mkdir(parents=True, exist_ok=True)
        model_path = self.model_catalog.resolve(model_id if isinstance(model_id, str) else None)
        await context.progress(0.15, "MinerU 正在推理")
        await self.mineru.parse(
            input_pdf,
            output,
            options,
            check=context.check,
            model_path=model_path,
        )
        archive_path = task_directory / "mineru.zip"
        self.package_output(output, archive_path, options, input_pdf)
        archive_object = await run_blocking(cas.write, _file_chunks(archive_path))
        return BackendExecutionResult(
            archive_object=archive_object,
            preview_object=preview_object,
        )
