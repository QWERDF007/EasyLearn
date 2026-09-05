from pydantic import BaseModel, Field, JsonValue


class ErrorView(BaseModel):
    code: str
    message: str
    retryable: bool = False
    request_id: str
    run_ref: dict[str, JsonValue] | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class DomainError(Exception):
    def __init__(
        self, code: str, message: str, *, status: int = 422, retryable: bool = False
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
