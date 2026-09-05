from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EASYLEARN_", env_file=".env", extra="ignore")

    database_url: SecretStr
    storage_root: Path = Path(".runtime/artifacts")
    upload_max_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    upload_chunk_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    upload_session_ttl: int = Field(default=86400, gt=0)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if make_url(value.get_secret_value()).drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value
