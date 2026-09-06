"""Configuration for the single-process v3 application."""

import os
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from easylearn.images import ImageLimits
from easylearn.mineru.schema import MinerUArchiveLimits, MinerUParseOptions, MinerUTableLimits
from easylearn.previews.schema import PreviewLimits


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class AppSettings(_Config):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    data_dir: Path = Path("./data")
    open_browser: bool = False


class TaskSettings(_Config):
    queue_limit: int = Field(default=8, ge=1, le=1000)
    parse_concurrency: int = Field(default=1, ge=1, le=16)
    translation_requests: int = Field(default=1, ge=1, le=16)
    qa_requests: int = Field(default=1, ge=1, le=16)
    finished_task_retention_minutes: int = Field(default=30, ge=1, le=1440)


class CacheSettings(_Config):
    max_mb: int = Field(default=128, ge=1, le=4096)
    max_documents: int = Field(default=3, ge=1, le=100)


class MinerUSettings(_Config):
    mode: Literal["cli", "api"] = "cli"
    command: tuple[str, ...] = ("mineru",)
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=900, gt=0)
    poll_interval_seconds: float = Field(default=1, gt=0)
    parse: MinerUParseOptions = Field(default_factory=MinerUParseOptions)
    archive_limits: MinerUArchiveLimits = Field(default_factory=MinerUArchiveLimits)
    table_limits: MinerUTableLimits = Field(default_factory=MinerUTableLimits)
    image_limits: ImageLimits = Field(default_factory=ImageLimits)
    preview_limits: PreviewLimits = Field(default_factory=PreviewLimits)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("MinerU command must contain at least one non-empty argument")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode == "api" and not self.base_url:
            raise ValueError("MinerU API mode requires base_url")
        return self


class LLMSettings(_Config):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = ""
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=600, gt=0)
    local_only: bool = True
    json_mode: bool = False


class FileSettings(_Config):
    max_upload_mb: int = Field(default=100, gt=0, le=4096)
    keep_parse_versions: int = Field(default=2, ge=1, le=20)
    revision_history_limit: int = Field(default=10, ge=1, le=100)
    tmp_retention_hours: int = Field(default=24, ge=1, le=168)
    export_retention_days: int = Field(default=30, ge=1, le=3650)
    log_max_mb: int = Field(default=10, ge=1, le=1024)
    log_backup_count: int = Field(default=3, ge=0, le=20)


class ExtensionSettings(_Config):
    office_enabled: bool = False
    qa_enabled: bool = False
    semantic_search_enabled: bool = False
    office_command: tuple[str, ...] = ("soffice",)
    qa_context_chars: int = Field(default=12000, gt=0, le=1_000_000)
    qa_max_blocks: int = Field(default=64, gt=0, le=1000)

    @field_validator("office_command")
    @classmethod
    def validate_office_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("Office command must contain at least one non-empty argument")
        return value


class Settings(BaseModel):
    """One TOML-backed schema shared by the HTTP process and task runners."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    app: AppSettings = Field(default_factory=AppSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    mineru: MinerUSettings = Field(default_factory=MinerUSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    files: FileSettings = Field(default_factory=FileSettings)
    extensions: ExtensionSettings = Field(default_factory=ExtensionSettings)

    @classmethod
    def load(cls, config_path: Path | str | None = None) -> "Settings":
        explicit = Path(config_path) if config_path is not None else None
        configured = explicit or (
            Path(os.environ["EASYLEARN_CONFIG"])
            if os.environ.get("EASYLEARN_CONFIG")
            else Path("config.toml")
        )
        if not configured.is_file():
            if explicit is not None or os.environ.get("EASYLEARN_CONFIG"):
                raise FileNotFoundError(f"Configuration file does not exist: {configured}")
            return cls()
        if configured.suffix.lower() != ".toml":
            raise ValueError("Only TOML configuration files are supported")
        try:
            data = tomllib.loads(configured.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML configuration: {configured}") from exc
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a TOML table")
        result = cls.model_validate(data)
        data_dir = result.app.data_dir
        if not data_dir.is_absolute():
            result = result.model_copy(
                update={
                    "app": result.app.model_copy(update={"data_dir": configured.parent / data_dir})
                }
            )
        return result

    @property
    def data_dir(self) -> Path:
        return self.app.data_dir.resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.files.max_upload_mb * 1024 * 1024

    @property
    def mineru_api_key(self) -> str | None:
        return os.environ.get(self.mineru.api_key_env) if self.mineru.api_key_env else None

    @property
    def llm_api_key(self) -> str | None:
        return os.environ.get(self.llm.api_key_env) if self.llm.api_key_env else None
