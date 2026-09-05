from pydantic import BaseModel, ConfigDict, Field, RedisDsn, SecretStr, field_validator

from easylearn.document_ir.schema import Identifier


class QueueSettings(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )

    redis_url: SecretStr
    namespace: Identifier = "easylearn"
    worker_threads: int = Field(default=4, ge=1, le=64)
    socket_timeout_seconds: float = Field(default=5, gt=0)
    shutdown_timeout_seconds: float = Field(default=30, gt=0)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        RedisDsn(value.get_secret_value())
        return value
