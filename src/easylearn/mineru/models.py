from __future__ import annotations

from pathlib import Path, PurePath

from pydantic import BaseModel, ConfigDict

from easylearn.config import MinerUSettings
from easylearn.errors import DomainError


class MinerUModelView(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    name: str
    selected: bool = False


class MinerUModelCatalog:
    """Resolve only models explicitly registered in application configuration."""

    def __init__(self, settings: MinerUSettings) -> None:
        self.settings = settings
        self._candidates = {candidate.model_id: candidate for candidate in settings.models}

    @property
    def default_model_id(self) -> str:
        return self.settings.default_model_id or self.settings.models[0].model_id

    @property
    def default_model_path(self) -> Path:
        return self._candidates[self.default_model_id].path

    def list(self) -> tuple[MinerUModelView, ...]:
        return tuple(
            MinerUModelView(
                model_id=candidate.model_id,
                name=candidate.name or candidate.model_id,
                selected=candidate.model_id == self.default_model_id,
            )
            for candidate in self.settings.models
        )

    def resolve(self, model_id: str | None) -> Path:
        selected_id = (
            self.default_model_id
            if model_id is None or not model_id.strip()
            else model_id
        )
        if PurePath(selected_id).name != selected_id or selected_id in {".", ".."}:
            raise DomainError("MINERU_MODEL_INVALID", "MinerU model selection is invalid")
        candidate = self._candidates.get(selected_id)
        if candidate is None:
            raise DomainError("MINERU_MODEL_NOT_FOUND", "Selected MinerU model is unavailable")
        target = candidate.path.resolve()
        if not target.is_dir():
            raise DomainError("MINERU_MODEL_NOT_FOUND", "Selected MinerU model is unavailable")
        return target

    def model_id_for(self, path: Path) -> str:
        resolved = path.resolve()
        for candidate in self.settings.models:
            if candidate.path.resolve() == resolved:
                return candidate.model_id
        raise DomainError("MINERU_MODEL_INVALID", "MinerU model is not configured")
