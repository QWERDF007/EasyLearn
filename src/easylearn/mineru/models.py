from __future__ import annotations

import json
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
    """Expose only model directories explicitly owned by the MinerU model root."""

    def __init__(self, settings: MinerUSettings) -> None:
        self.settings = settings

    @property
    def root(self) -> Path:
        return (self.settings.model_root or self.settings.model_path.parent).resolve()

    @property
    def default_model_id(self) -> str:
        return self.settings.model_path.name

    def list(self) -> tuple[MinerUModelView, ...]:
        candidates: dict[str, Path] = {}
        if self.root.is_dir():
            candidates.update(
                (path.name, path)
                for path in self.root.iterdir()
                if self._is_usable_model(path)
            )
        selected = self.settings.model_path.resolve()
        if self._is_usable_model(selected) and selected.parent == self.root:
            candidates.setdefault(selected.name, selected)
        return tuple(
            MinerUModelView(
                model_id=name,
                name=name,
                selected=path.resolve() == selected,
            )
            for name, path in sorted(candidates.items(), key=lambda item: item[0].casefold())
        )

    @staticmethod
    def _is_usable_model(path: Path) -> bool:
        if not path.is_dir() or path.name.startswith("."):
            return False
        try:
            config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(config, dict):
            return False
        architectures = config.get("architectures")
        architecture_names = architectures if isinstance(architectures, list) else []
        is_vlm = config.get("model_type") in {"qwen2_vl", "qwen2_5_vl"} or any(
            isinstance(name, str) and "VLForConditionalGeneration" in name
            for name in architecture_names
        )
        if not is_vlm or not (path / "preprocessor_config.json").is_file():
            return False
        return any(path.glob("*.safetensors")) or any(path.glob("pytorch_model*.bin"))

    def resolve(self, model_id: str | None) -> Path:
        if model_id is None or not model_id.strip():
            target = self.settings.model_path.resolve()
        else:
            candidate = PurePath(model_id)
            if candidate.name != model_id or model_id in {".", ".."}:
                raise DomainError("MINERU_MODEL_INVALID", "MinerU model selection is invalid")
            target = (self.root / model_id).resolve()
        if not target.is_dir() or target.parent != self.root:
            raise DomainError("MINERU_MODEL_NOT_FOUND", "Selected MinerU model is unavailable")
        return target

    def model_id_for(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved.parent != self.root:
            raise DomainError("MINERU_MODEL_INVALID", "MinerU model is outside the model root")
        return resolved.name
