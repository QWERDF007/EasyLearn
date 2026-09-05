import os
import tomllib
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from easylearn.previews.schema import PreviewLimits


class Settings(BaseSettings):
    """One schema for all application processes.

    Priority: constructor > environment > cwd .env > selected file > secrets > defaults.
    EASYLEARN_CONFIG selects a file; otherwise discover exactly one config.toml/yaml/yml
    in cwd. Relative storage paths supplied by a file are rooted at its directory.
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
            if isinstance(data.get("storage_root"), str):
                data["storage_root"] = (path.parent / data["storage_root"]).resolve()
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
