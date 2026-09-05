from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from easylearn.document_ir.schema import Identifier
from easylearn.mineru.schema import MinerUArchiveLimits, MinerULimits, MinerUParseOptions
from easylearn.urls import ServiceUrl


class _Config(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class LocalModel(_Config):
    path: Path
    revision: Identifier


class MinerUSettings(_Config):
    base_url: ServiceUrl
    profile_revision: Identifier
    local_model: Identifier | None = None
    api_key: SecretStr | None = None
    parse: MinerUParseOptions = Field(default_factory=MinerUParseOptions)
    limits: MinerULimits = Field(default_factory=MinerULimits)
    archive_limits: MinerUArchiveLimits = Field(default_factory=MinerUArchiveLimits)


class ProviderCapabilities(_Config):
    chat: bool = False
    stream: bool = False
    json_schema: bool = False
    vision: bool = False
    embedding: bool = False
    token_count: bool = False
    cancel: bool = False

    @model_validator(mode="after")
    def validate_generation_capabilities(self) -> Self:
        if (self.stream or self.json_schema or self.vision) and not self.chat:
            raise ValueError("Stream, JSON schema and vision capabilities require chat")
        return self


class ProviderTimeouts(_Config):
    connect_seconds: float = Field(default=5, gt=0)
    read_idle_seconds: float = Field(default=60, gt=0)
    total_seconds: float = Field(default=180, gt=0)


class ProviderScheduling(_Config):
    max_inflight: int = Field(default=4, gt=0)
    reserved_qa_slots: int = Field(default=1, ge=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_reserved_capacity(self) -> Self:
        if self.reserved_qa_slots > self.max_inflight:
            raise ValueError("Reserved QA slots must not exceed max_inflight")
        return self


class GenerationParameters(_Config):
    max_output_tokens: int = Field(default=2048, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    reasoning_budget: int | None = Field(default=None, ge=0)
    safety_margin: int = Field(default=512, ge=0)

    @model_validator(mode="after")
    def validate_reasoning_budget(self) -> Self:
        if self.reasoning_budget is not None and self.reasoning_budget > self.max_output_tokens:
            raise ValueError("Reasoning budget must not exceed max_output_tokens")
        return self


class EmbeddingParameters(_Config):
    dimensions: int = Field(gt=0)
    normalize: bool = True
    max_batch_size: int = Field(default=16, gt=0)


class ProviderProfile(_Config):
    protocol: Literal["openai_compatible"] = "openai_compatible"
    base_url: ServiceUrl
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    revision: Identifier
    api_key: SecretStr | None = None
    scope: Literal["local", "external"] = "external"
    local_model: Identifier | None = None
    tokenizer_model: Identifier | None = None
    model_revision: str | None = None
    chat_template_revision: str | None = None
    context_limit: int = Field(gt=0)
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    generation: GenerationParameters = Field(default_factory=GenerationParameters)
    embedding: EmbeddingParameters | None = None
    timeouts: ProviderTimeouts = Field(default_factory=ProviderTimeouts)
    scheduling: ProviderScheduling = Field(default_factory=ProviderScheduling)

    @model_validator(mode="after")
    def validate_model_parameters(self) -> Self:
        if self.capabilities.chat and (
            self.generation.max_output_tokens + self.generation.safety_margin >= self.context_limit
        ):
            raise ValueError("Output budget and safety margin must leave room for input tokens")
        if self.capabilities.embedding != (self.embedding is not None):
            raise ValueError("Embedding capability and parameters must be configured together")
        return self


class ModelRoutes(_Config):
    translation: Identifier | None = None
    qa: Identifier | None = None
    embedding: Identifier | None = None
    vision: Identifier | None = None
