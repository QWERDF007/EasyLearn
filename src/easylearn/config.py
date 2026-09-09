"""Configuration for the single-process v3 application."""

import os
import tomllib
from pathlib import Path

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
    translation_concurrency: int = Field(default=4, ge=1, le=16)
    qa_requests: int = Field(default=1, ge=1, le=16)
    finished_task_retention_minutes: int = Field(default=30, ge=1, le=1440)


class CacheSettings(_Config):
    max_mb: int = Field(default=128, ge=1, le=4096)
    max_documents: int = Field(default=3, ge=1, le=100)


class MinerUModelCandidate(_Config):
    model_id: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    path: Path


class MinerUSettings(_Config):
    models: tuple[MinerUModelCandidate, ...] = Field(
        default_factory=lambda: (
            MinerUModelCandidate(
                model_id="MinerU2.5-Pro-2605-1.2B",
                name="MinerU2.5-Pro-2605-1.2B",
                path=Path("F:/models/MinerU2.5-Pro-2605-1.2B"),
            ),
        )
    )
    default_model_id: str | None = Field(default=None, min_length=1, max_length=255)
    timeout_seconds: float = Field(default=900, gt=0)
    parse: MinerUParseOptions = Field(default_factory=MinerUParseOptions)
    archive_limits: MinerUArchiveLimits = Field(default_factory=MinerUArchiveLimits)
    table_limits: MinerUTableLimits = Field(default_factory=MinerUTableLimits)
    image_limits: ImageLimits = Field(default_factory=ImageLimits)
    preview_limits: PreviewLimits = Field(default_factory=PreviewLimits)

    @model_validator(mode="after")
    def validate_model_candidates(self) -> "MinerUSettings":
        model_ids = [candidate.model_id for candidate in self.models]
        if not model_ids or len(model_ids) != len(set(model_ids)):
            raise ValueError("MinerU model IDs must be unique and non-empty")
        if self.default_model_id is not None and self.default_model_id not in model_ids:
            raise ValueError("MinerU default_model_id must reference a configured model")
        return self

class LLMProviderSettings(_Config):
    base_url: str = Field(default="http://127.0.0.1:8000/v1", min_length=1)
    model: str = Field(default="", max_length=255)
    api_key: str | None = None
    api_key_env: str | None = None
    local_only: bool = False
    json_mode: bool = False
    reasoning_effort: str | None = None


class LLMSettings(_Config):
    active_provider: str | None = None
    providers: dict[str, LLMProviderSettings] = Field(default_factory=dict)
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=600, gt=0)
    local_only: bool = True
    json_mode: bool = False
    reasoning_effort: str | None = None
    proxy: str | None = None
    max_retries: int = Field(default=5, ge=0, le=10)
    retry_min_delay: float = Field(default=2.0, ge=0.0, le=60.0)
    retry_max_delay: float = Field(default=30.0, ge=0.0, le=300.0)

    @model_validator(mode="after")
    def resolve_provider(self) -> "LLMSettings":
        if not self.providers:
            return self
        if not self.active_provider or self.active_provider not in self.providers:
            configured = list(self.providers.keys())
            raise ValueError(
                f"LLM active_provider '{self.active_provider}' not found in configured providers: "
                f"{configured}"
            )
        active = self.providers[self.active_provider]
        return self.model_copy(
            update={
                "base_url": active.base_url,
                "model": active.model,
                "api_key": active.api_key,
                "api_key_env": active.api_key_env,
                "local_only": active.local_only,
                "reasoning_effort": active.reasoning_effort,
                "json_mode": active.json_mode,
            }
        )

    @property
    def resolved_proxy(self) -> str | None:
        from urllib.parse import urlparse

        hostname = (urlparse(self.base_url).hostname or "").lower()
        if self.local_only or hostname in {"127.0.0.1", "localhost", "::1"}:
            return None
        if self.proxy is not None:
            explicit = self.proxy.strip()
            if not explicit or explicit.lower() in ("none", "false", "off", "direct"):
                return None
            return explicit
        env_proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        if env_proxy:
            return env_proxy
        try:
            import urllib.request

            system_proxies = urllib.request.getproxies()
            return system_proxies.get("https") or system_proxies.get("http")
        except Exception:
            return None


class FileSettings(_Config):
    max_upload_mb: int = Field(default=100, gt=0, le=4096)
    keep_parse_versions: int = Field(default=2, ge=1, le=20)
    revision_history_limit: int = Field(default=10, ge=1, le=100)
    tmp_dir: Path | None = None
    tmp_retention_hours: int = Field(default=24, ge=1, le=168)
    export_retention_days: int = Field(default=30, ge=1, le=3650)
    log_dir: Path = Path("./logs")


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
        dotenv_path = (configured.parent / ".env") if configured.is_file() else Path(".env")
        if dotenv_path.is_file():
            _load_dotenv(dotenv_path)
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
        updates: dict[str, object] = {}
        data_dir = result.app.data_dir
        if not data_dir.is_absolute():
            updates["app"] = result.app.model_copy(
                update={"data_dir": configured.parent / data_dir}
            )
        file_updates: dict[str, object] = {}
        log_dir = result.files.log_dir
        if not log_dir.is_absolute():
            file_updates["log_dir"] = configured.parent / log_dir
        tmp_dir = result.files.tmp_dir
        if tmp_dir is not None and not tmp_dir.is_absolute():
            file_updates["tmp_dir"] = configured.parent / tmp_dir
        if file_updates:
            updates["files"] = result.files.model_copy(update=file_updates)
        mineru_updates: dict[str, object] = {}
        if any(not candidate.path.is_absolute() for candidate in result.mineru.models):
            resolved_models = tuple(
                candidate.model_copy(
                    update={
                        "path": candidate.path
                        if candidate.path.is_absolute()
                        else configured.parent / candidate.path
                    }
                )
                for candidate in result.mineru.models
            )
            mineru_updates["models"] = resolved_models
        if mineru_updates:
            updates["mineru"] = result.mineru.model_copy(update=mineru_updates)
        if updates:
            result = result.model_copy(update=updates)
        return result

    @property
    def data_dir(self) -> Path:
        return self.app.data_dir.resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def log_dir(self) -> Path:
        return self.files.log_dir.resolve()

    @property
    def tmp_dir(self) -> Path:
        return (self.files.tmp_dir or (self.data_dir / "tmp")).resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.files.max_upload_mb * 1024 * 1024

    @property
    def llm_api_key(self) -> str | None:
        if self.llm.api_key:
            return self.llm.api_key
        return os.environ.get(self.llm.api_key_env) if self.llm.api_key_env else None


def _load_dotenv(path: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=True)
    except ImportError:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            if key:
                os.environ[key] = val
