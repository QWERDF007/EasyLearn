from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from easylearn.config import Settings
from easylearn.document_ir.schema import DocumentIR
from easylearn.execution import run_blocking
from easylearn.mineru.result import MinerUResultValidator, NormalizedEvidence, ParseSource
from easylearn.mineru.schema import MinerUOptions
from easylearn.storage import LocalStorage, StoredObject
from easylearn.translation import translation_units


@dataclass(frozen=True)
class NormalizedParseResult:
    evidence: NormalizedEvidence
    total_units: int
    staging_directory: Path


class ParseResultNormalizer:
    """Normalizes raw backend evidence into verified DocumentIR and builds staged artifacts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def normalize(
        self,
        *,
        archive: StoredObject,
        preview_object: StoredObject,
        options: MinerUOptions,
        source: ParseSource,
        task_directory: Path,
        cas: LocalStorage,
        input_pdf: Path,
    ) -> NormalizedParseResult:
        evidence: NormalizedEvidence = await MinerUResultValidator(
            cas,
            archive_limits=self.settings.mineru.archive_limits,
            image_limits=self.settings.mineru.image_limits,
            preview_limits=self.settings.mineru.preview_limits,
            table_limits=self.settings.mineru.table_limits,
            timeout=self.settings.mineru.timeout_seconds,
        ).normalize(
            archive,
            options=options,
            source=source,
        )

        staging_directory = task_directory / "published"
        total_units = await run_blocking(
            self.build_published_directory,
            staging_directory,
            input_pdf,
            cas,
            archive,
            evidence,
        )

        return NormalizedParseResult(
            evidence=evidence,
            total_units=total_units,
            staging_directory=staging_directory,
        )

    def build_published_directory(
        self,
        directory: Path,
        input_pdf: Path,
        cas: LocalStorage,
        archive: StoredObject,
        evidence: NormalizedEvidence,
    ) -> int:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "images").mkdir()
        shutil.copyfile(input_pdf, directory / "preview.pdf")
        shutil.copyfile(cas.path(archive.key), directory / "mineru.zip")
        shutil.copyfile(cas.path(evidence.document_ir.key), directory / "document.json")
        ir = DocumentIR.model_validate_json(cas.path(evidence.document_ir.key).read_bytes())
        total_units = len(translation_units(ir))
        for asset in ir.assets:
            member = next(
                member
                for member in evidence.manifest.members
                if member.asset_id(ir.parse_run_id) == asset.asset_id
            )
            source = evidence.objects[member.path]
            destination = directory / asset.export_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cas.path(source.key), destination)
        return total_units
