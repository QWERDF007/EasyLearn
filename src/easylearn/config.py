import os
import tomllib
from pathlib import Path
from typing import Self

import yaml  # type: ignore[import-untyped]
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from easylearn.document_ir.schema import Identifier
from easylearn.inference.config import LocalModel, MinerUSettings, ModelRoutes, ProviderProfile
from easylearn.previews.schema import PreviewLimits


class Settings(BaseSettings):
    """One schema for all application processes.

    Priority: constructor > environment > cwd .env > selected file > secrets > defaults.
    EASYLEARN_CONFIG selects a file; otherwise discover exactly one config.toml/yaml/yml
    in cwd. File-supplied storage_root and models_root are relative to that file;
    local model paths are relative to the final models_root after all overrides.
    Loading settings does not access models or start/connect to inference services.
    """

    model_config = SettingsConfigDict(
        env_prefix="EASYLEARN_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    storage_root: Path = Path(".runtime/artifacts")
    upload_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    upload_chunk_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    upload_session_ttl: int = Field(default=86400, gt=0)
    preview_limits: PreviewLimits = Field(default_factory=PreviewLimits)
    preview_timeout_seconds: float = Field(default=120, gt=0)
    models_root: Path = Path("D:/Models")
    local_models: dict[Identifier, LocalModel] = Field(default_factory=dict)
    mineru: MinerUSettings | None = None
    providers: dict[Identifier, ProviderProfile] = Field(default_factory=dict)
    model_routes: ModelRoutes = Field(default_factory=ModelRoutes)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: tuple[PydanticBaseSettingsSource, ...] = (
            init_settings,
            env_settings,
            dotenv_settings,
        )
        explicit = os.environ.get("EASYLEARN_CONFIG")
        candidates = (
            [Path(explicit).resolve()]
            if explicit
            else [
                Path(name).resolve()
                for name in ("config.toml", "config.yaml", "config.yml")
                if Path(name).is_file()
            ]
        )
        if len(candidates) > 1:
            raise SettingsError("Multiple configuration files found; set EASYLEARN_CONFIG")
        if candidates:
            path = candidates[0]
            if not path.is_file():
                raise SettingsError(f"Configuration file does not exist: {path}")
            if path.suffix.lower() not in (".toml", ".yaml", ".yml"):
                raise SettingsError(f"Unsupported configuration format: {path.suffix}")
            try:
                text = path.read_text(encoding="utf-8-sig")
                data = (
                    tomllib.loads(text) if path.suffix.lower() == ".toml" else yaml.safe_load(text)
                )
            except (OSError, UnicodeError):
                raise SettingsError(f"Configuration file could not be read: {path}") from None
            except (tomllib.TOMLDecodeError, yaml.YAMLError):
                raise SettingsError(f"Invalid configuration syntax: {path}") from None
            if data is None:
                data = {}
            if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
                raise SettingsError(f"Configuration must be a mapping with string keys: {path}")
            for name in ("storage_root", "models_root"):
                if isinstance(data.get(name), str):
                    data[name] = (path.parent / data[name]).resolve()
            sources += (InitSettingsSource(settings_cls, init_kwargs=data),)
        return sources + (file_secret_settings,)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy connection URL") from None
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

    @model_validator(mode="after")
    def validate_model_configuration(self) -> Self:
        if (
            self.mineru is not None
            and self.mineru.local_model is not None
            and self.mineru.local_model not in self.local_models
        ):
            raise ValueError("MinerU local_model must reference a registered local model")
        for profile in self.providers.values():
            for reference in (profile.local_model, profile.tokenizer_model):
                if reference is not None and reference not in self.local_models:
                    raise ValueError(
                        "Provider model references must identify registered local models"
                    )
        for purpose, profile_id in self.model_routes.model_dump().items():
            if profile_id is None:
                continue
            if profile_id not in self.providers:
                raise ValueError("Model routes must reference a registered provider profile")
            capability = "chat" if purpose in ("translation", "qa") else purpose
            if not getattr(self.providers[profile_id].capabilities, capability):
                raise ValueError(f"Selected provider does not support {purpose}")
        self.local_models = {
            name: model.model_copy(update={"path": (self.models_root / model.path).resolve()})
            for name, model in self.local_models.items()
        }
        return self
