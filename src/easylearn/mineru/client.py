import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager
from typing import BinaryIO
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from easylearn.errors import DomainError
from easylearn.mineru.schema import (
    CapturedResponse,
    MinerUCapabilities,
    MinerUHealth,
    MinerULimits,
    MinerUOptions,
    MinerUTask,
)


class SubmissionUnknown(DomainError):
    def __init__(self, request_id: UUID, raw_body: bytes = b"") -> None:
        super().__init__(
            "MINERU_SUBMIT_UNKNOWN",
            "MinerU may have accepted this submission; reconcile before retry",
            status=409,
        )
        self.submission_request_id = request_id
        self.raw_body = raw_body


@contextmanager
def _download_errors() -> Iterator[None]:
    try:
        yield
    except httpx.TimeoutException:
        raise DomainError(
            "MINERU_DOWNLOAD_TIMEOUT", "MinerU download timed out", retryable=True
        ) from None
    except httpx.TransportError:
        raise DomainError(
            "MINERU_UNAVAILABLE", "MinerU download was interrupted", status=503, retryable=True
        ) from None


class MinerUClient:
    """Fixed upstream contract; HTTP connection lifecycle belongs to the calling process."""

    capabilities = MinerUCapabilities()

    def __init__(self, http: httpx.AsyncClient, *, limits: MinerULimits | None = None) -> None:
        self.http = http
        self.limits = limits or MinerULimits()

    async def health(self) -> MinerUHealth:
        response = await self._get("/health")
        if response.status_code != 200:
            raise DomainError("MINERU_PROTOCOL_INVALID", "MinerU health endpoint is not available")
        return self._capture(response, MinerUHealth).value

    async def submit(
        self, source: BinaryIO, *, request_id: UUID, options: MinerUOptions
    ) -> CapturedResponse[MinerUTask]:
        try:
            request = self.http.build_request(
                "POST",
                "/tasks",
                timeout=self.limits.submission_timeout_seconds,
                headers={"X-Request-ID": str(request_id), "Accept-Encoding": "identity"},
                files={"files": ("input.pdf", source, "application/pdf")},
                data={
                    "backend": options.backend,
                    **({"server_url": str(options.server_url)} if options.server_url else {}),
                    "lang_list": options.language,
                    "effort": options.effort,
                    "parse_method": options.parse_method,
                    "formula_enable": str(options.formula_enable).lower(),
                    "table_enable": str(options.table_enable).lower(),
                    "image_analysis": str(options.image_analysis).lower(),
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_model_output": "true",
                    "return_content_list": "true",
                    "return_images": "true",
                    "return_original_file": "true",
                    "response_format_zip": "true",
                    "start_page_id": "0",
                    "end_page_id": str(options.page_count - 1),
                },
            )
            response = await self._metadata(request, self.limits.submission_timeout_seconds)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise DomainError(
                "MINERU_UNAVAILABLE", "MinerU could not be reached", status=503, retryable=True
            ) from None
        except httpx.TransportError:
            raise SubmissionUnknown(request_id) from None
        except DomainError:
            raise SubmissionUnknown(request_id) from None
        if 400 <= response.status_code < 500 and response.status_code != 408:
            raise DomainError(
                "MINERU_SUBMIT_REJECTED",
                "MinerU rejected the submission",
                status=502,
                retryable=response.status_code == 429,
            )
        if response.status_code != 202:
            raise SubmissionUnknown(request_id, response.content)
        try:
            result = self._capture(response, MinerUTask)
        except DomainError:
            raise SubmissionUnknown(request_id, response.content) from None
        if result.value.backend != options.backend or result.value.file_names != ("input",):
            raise SubmissionUnknown(request_id, response.content)
        return result

    async def query(self, task_id: UUID) -> CapturedResponse[MinerUTask]:
        response = await self._get(f"/tasks/{task_id}")
        if response.status_code == 404:
            raise DomainError(
                "MINERU_TASK_NOT_FOUND", "MinerU task is missing or expired", status=404
            )
        if response.status_code != 200:
            raise DomainError("MINERU_PROTOCOL_INVALID", "Unexpected MinerU task response")
        result = self._capture(response, MinerUTask)
        if result.value.task_id != task_id:
            raise DomainError("MINERU_PROTOCOL_INVALID", "MinerU task identity does not match")
        return result

    async def cancel(self, task_id: UUID) -> None:
        raise DomainError(
            "MINERU_CAPABILITY_UNAVAILABLE", "This MinerU contract has no cancellation endpoint"
        )

    async def reconcile(self, submission_request_id: UUID) -> CapturedResponse[MinerUTask] | None:
        raise DomainError(
            "MINERU_CAPABILITY_UNAVAILABLE",
            "This MinerU contract cannot query by submission identity",
        )

    @asynccontextmanager
    async def download(self, task_id: UUID) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream to staging; the caller must validate the complete archive before publication."""
        deadline = asyncio.timeout(self.limits.download_timeout_seconds)
        try:
            async with deadline:
                with _download_errors():
                    response = await self.http.send(
                        self.http.build_request(
                            "GET",
                            f"/tasks/{task_id}/result",
                            headers={"Accept-Encoding": "identity"},
                            timeout=self.limits.download_timeout_seconds,
                        ),
                        stream=True,
                        follow_redirects=False,
                    )
                try:
                    if response.status_code == 202:
                        raise DomainError(
                            "MINERU_RESULT_PENDING", "MinerU result is not ready", retryable=True
                        )
                    if response.status_code == 404:
                        raise DomainError(
                            "MINERU_TASK_NOT_FOUND", "MinerU task is missing or expired", status=404
                        )
                    if response.status_code == 409:
                        raise DomainError("MINERU_TASK_FAILED", "MinerU task failed")
                    if response.status_code >= 500 or response.status_code == 429:
                        raise DomainError(
                            "MINERU_UNAVAILABLE", "MinerU is not ready", status=503, retryable=True
                        )
                    if (
                        response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0]
                        != "application/zip"
                    ):
                        raise DomainError(
                            "MINERU_PROTOCOL_INVALID", "MinerU did not return a ZIP archive"
                        )
                    async with aclosing(self._download_body(response)) as content:
                        yield content
                finally:
                    await response.aclose()
        except TimeoutError:
            if not deadline.expired():
                raise
            raise DomainError(
                "MINERU_DOWNLOAD_TIMEOUT", "MinerU download exceeded its deadline", retryable=True
            ) from None

    async def _download_body(self, response: httpx.Response) -> AsyncGenerator[bytes]:
        with _download_errors():
            async for chunk in self._body(response, self.limits.download_max_bytes):
                yield chunk

    @staticmethod
    async def _body(response: httpx.Response, limit: int) -> AsyncIterator[bytes]:
        if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
            raise DomainError("MINERU_PROTOCOL_INVALID", "MinerU must use identity encoding")
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise DomainError("MINERU_RESPONSE_TOO_LARGE", "MinerU response exceeds size limit")
            yield chunk

    async def _get(self, path: str) -> httpx.Response:
        try:
            response = await self._metadata(
                self.http.build_request(
                    "GET",
                    path,
                    headers={"Accept-Encoding": "identity"},
                    timeout=self.limits.request_timeout_seconds,
                ),
                self.limits.request_timeout_seconds,
            )
        except httpx.TransportError:
            raise DomainError(
                "MINERU_UNAVAILABLE", "MinerU could not be reached", status=503, retryable=True
            ) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise DomainError(
                "MINERU_UNAVAILABLE", "MinerU is not ready", status=503, retryable=True
            )
        return response

    async def _metadata(self, request: httpx.Request, timeout: float) -> httpx.Response:
        try:
            async with asyncio.timeout(timeout):
                response = await self.http.send(request, stream=True, follow_redirects=False)
                try:
                    content = b"".join(
                        [
                            part
                            async for part in self._body(response, self.limits.metadata_max_bytes)
                        ]
                    )
                    return httpx.Response(response.status_code, content=content, request=request)
                finally:
                    await response.aclose()
        except TimeoutError:
            raise httpx.ReadTimeout(
                "MinerU response exceeded its deadline", request=request
            ) from None

    @staticmethod
    def _capture[T: BaseModel](response: httpx.Response, schema: type[T]) -> CapturedResponse[T]:
        try:
            return CapturedResponse(schema.model_validate_json(response.content), response.content)
        except ValidationError:
            raise DomainError(
                "MINERU_PROTOCOL_INVALID", "Invalid MinerU response protocol"
            ) from None
