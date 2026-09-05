import asyncio
import gzip
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from easylearn.errors import DomainError
from easylearn.mineru.client import MinerUClient
from easylearn.mineru.schema import MinerULimits, MinerUOptions

TASK_ID = UUID("896c8864-4f1c-45d2-9d48-f766c5041c33")


@pytest.mark.parametrize(
    "options",
    [
        {"backend": "vlm-http-client"},
        {"backend": "hybrid-http-client"},
        {"backend": "pipeline", "server_url": "http://inference.test:30000"},
        {"backend": "vlm-http-client", "server_url": "file:///models"},
    ],
)
def test_remote_inference_address_must_match_the_selected_backend(options):
    with pytest.raises(ValidationError):
        MinerUOptions(page_count=1, **options)


def task_payload(status="pending"):
    return {
        "task_id": str(TASK_ID),
        "status": status,
        "backend": "vlm-engine",
        "file_names": ["input"],
        "created_at": "2026-09-05T00:00:00Z",
        "started_at": None,
        "completed_at": None,
        "error": None,
        "status_url": "https://untrusted.test/ignore",
        "result_url": "https://untrusted.test/ignore",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["vlm-http-client", "hybrid-http-client"])
async def test_remote_backend_submits_its_explicit_inference_endpoint(backend):
    def upstream(request):
        assert b'name="server_url"\r\n\r\nhttp://inference.test:30000/v1\r\n' in request.read()
        return httpx.Response(202, json={**task_payload(), "backend": backend})

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        receipt = await MinerUClient(http).submit(
            BytesIO(b"pdf"),
            request_id=TASK_ID,
            options=MinerUOptions(
                page_count=1, backend=backend, server_url="http://inference.test:30000/v1"
            ),
        )
        assert receipt.value.backend == backend


@pytest.mark.asyncio
async def test_submit_uploads_one_snapshot_and_query_uses_only_configured_service():
    request_id = UUID("4e8f99d6-3e87-46cc-bdfd-9bcb44261de1")

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "mineru.test"
        if request.method == "POST":
            assert request.url.path == "/tasks"
            assert request.headers["X-Request-ID"] == str(request_id)
            assert "Idempotency-Key" not in request.headers
            body = request.read()
            assert b'filename="input.pdf"' in body
            assert b"%PDF-canonical-snapshot" in body
            assert b'name="end_page_id"\r\n\r\n26\r\n' in body
            for option in ("return_middle_json", "return_images", "response_format_zip"):
                assert f'name="{option}"\r\n\r\ntrue\r\n'.encode() in body
            return httpx.Response(202, json=task_payload())
        assert request.method == "GET"
        assert request.url.path == f"/tasks/{TASK_ID}"
        return httpx.Response(200, json=task_payload("completed"))

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        client = MinerUClient(http)
        receipt = await client.submit(
            BytesIO(b"%PDF-canonical-snapshot"),
            request_id=request_id,
            options=MinerUOptions(page_count=27),
        )
        assert receipt.value.task_id == TASK_ID
        assert receipt.value.status == "pending"
        assert b'"task_id"' in receipt.raw_body
        completed = await client.query(TASK_ID)
        assert completed.value.status == "completed"


@pytest.mark.asyncio
async def test_health_checks_fixed_protocol_and_declares_only_supported_capabilities():
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "status": "healthy",
                "version": "3.4.5",
                "protocol_version": 2,
                "task_retention_seconds": 86400,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        client = MinerUClient(http)
        health = await client.health()
        assert health.version == "3.4.5"
        assert health.task_retention_seconds == 86400
        assert client.capabilities.cancel is False
        assert client.capabilities.reconcile is False
        assert client.capabilities.idempotent_submission is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["read_timeout", "write_error", "server_error", "invalid_receipt"]
)
async def test_uncertain_submission_never_retries_and_retains_request_identity(failure):
    attempts = []
    request_id = UUID("4e8f99d6-3e87-46cc-bdfd-9bcb44261de1")

    def upstream(request):
        attempts.append(request)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("upstream may have accepted", request=request)
        if failure == "write_error":
            raise httpx.WriteError("upstream may have accepted", request=request)
        return httpx.Response(500 if failure == "server_error" else 202, content=b"invalid receipt")

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        with pytest.raises(DomainError) as error:
            await MinerUClient(http).submit(
                BytesIO(b"%PDF-snapshot"),
                request_id=request_id,
                options=MinerUOptions(page_count=1),
            )
        assert error.value.code == "MINERU_SUBMIT_UNKNOWN"
        assert error.value.retryable is False
        assert error.value.submission_request_id == request_id
        assert len(attempts) == 1
        if failure in ("server_error", "invalid_receipt"):
            assert error.value.raw_body == b"invalid receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, code, retryable",
    [
        ("connect", "MINERU_UNAVAILABLE", True),
        ("pool", "MINERU_UNAVAILABLE", True),
        (400, "MINERU_SUBMIT_REJECTED", False),
        (429, "MINERU_SUBMIT_REJECTED", True),
    ],
)
async def test_definite_submission_rejection_is_not_classified_as_accepted(
    failure, code, retryable
):
    def upstream(request):
        if failure == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "pool":
            raise httpx.PoolTimeout("pool exhausted", request=request)
        return httpx.Response(failure, content=b"private upstream message")

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        with pytest.raises(DomainError) as error:
            await MinerUClient(http).submit(
                BytesIO(b"%PDF-snapshot"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
            )
        assert error.value.code == code
        assert error.value.retryable == retryable
        assert "private upstream" not in str(error.value)


@pytest.mark.asyncio
async def test_submission_never_follows_redirects_even_when_http_client_enables_them():
    destinations = []

    def upstream(request):
        destinations.append(request.url.host)
        if request.url.host == "mineru.test":
            return httpx.Response(307, headers={"Location": "https://other.test/tasks"})
        return httpx.Response(202, json=task_payload())

    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        follow_redirects=True,
        transport=httpx.MockTransport(upstream),
    ) as http:
        with pytest.raises(DomainError, match="MINERU_SUBMIT_UNKNOWN"):
            await MinerUClient(http).submit(
                BytesIO(b"%PDF-snapshot"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
            )
        assert destinations == ["mineru.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {**task_payload(), "task_id": "888c8864-4f1c-45d2-9d48-f766c5041c33"},
        {**task_payload(), "status": "unknown-new-state"},
    ],
)
async def test_query_rejects_mismatched_identity_and_unknown_states(payload):
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as http:
        with pytest.raises(DomainError, match="MINERU_PROTOCOL_INVALID"):
            await MinerUClient(http).query(TASK_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, code, retryable",
    [
        (404, "MINERU_TASK_NOT_FOUND", False),
        (503, "MINERU_UNAVAILABLE", True),
        (429, "MINERU_UNAVAILABLE", True),
    ],
)
async def test_query_exposes_operational_errors_without_proxy_contents(status, code, retryable):
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=b"proxy secret")
        ),
    ) as http:
        with pytest.raises(DomainError) as error:
            await MinerUClient(http).query(TASK_ID)
        assert error.value.code == code
        assert error.value.retryable == retryable
        assert "proxy secret" not in str(error.value)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "reconcile"])
async def test_unsupported_control_operations_never_make_up_an_upstream_route(operation):
    def upstream(request):
        pytest.fail("Unsupported capabilities must not send HTTP requests")

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        with pytest.raises(DomainError, match="MINERU_CAPABILITY_UNAVAILABLE"):
            await getattr(MinerUClient(http), operation)(TASK_ID)


@pytest.mark.asyncio
async def test_download_streams_archive_from_task_endpoint_and_closes_connection():
    stream = ChunkStream([b"PK\x03\x04", b"raw-upstream-archive"])

    def upstream(request):
        assert request.url.path == f"/tasks/{TASK_ID}/result"
        return httpx.Response(200, headers={"Content-Type": "application/zip"}, stream=stream)

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        async with MinerUClient(http).download(TASK_ID) as content:
            downloaded = b"".join([part async for part in content])
        assert downloaded == b"PK\x03\x04raw-upstream-archive"
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.ReadTimeout, TimeoutError, OSError])
async def test_download_does_not_reclassify_errors_raised_by_its_consumer(failure):
    stream = ChunkStream([b"PK"])
    consumer_error = failure("consumer failed independently of MinerU")
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/zip"}, stream=stream
            )
        ),
    ) as http:
        with pytest.raises(failure) as error:
            async with MinerUClient(http).download(TASK_ID) as content:
                assert await anext(content) == b"PK"
                raise consumer_error
        assert error.value is consumer_error
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, code",
    [
        ("query", "MINERU_PROTOCOL_INVALID"),
        ("submit", "MINERU_SUBMIT_UNKNOWN"),
        ("download", "MINERU_PROTOCOL_INVALID"),
    ],
)
async def test_responses_require_identity_encoding_before_any_body_decoding(operation, code):
    stream = ChunkStream([gzip.compress(b"A" * 1024)])

    def upstream(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            202 if operation == "submit" else 200,
            headers={"Content-Type": "application/zip", "Content-Encoding": "gzip"},
            stream=stream,
        )

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        client = MinerUClient(
            http, limits=MinerULimits(metadata_max_bytes=32, download_max_bytes=32)
        )
        with pytest.raises(DomainError, match=code):
            if operation == "query":
                await client.query(TASK_ID)
            elif operation == "submit":
                await client.submit(
                    BytesIO(b"pdf"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
                )
            else:
                async with client.download(TASK_ID) as content:
                    _ = [part async for part in content]
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["early_exit", "cancel"])
async def test_download_releases_the_stream_when_consumer_stops_early(exit_mode):
    stream = ChunkStream([b"PK", b"unfinished"])
    started = asyncio.Event()

    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/zip"}, stream=stream
            )
        ),
    ) as http:

        async def consume():
            async with MinerUClient(http).download(TASK_ID) as content:
                assert await anext(content) == b"PK"
                started.set()
                if exit_mode == "cancel":
                    await asyncio.Event().wait()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        if exit_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("version, protocol", [("3.4.4", 2), ("3.4.5", 3)])
async def test_health_rejects_an_unverified_upstream_contract(version, protocol):
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "version": version,
                    "protocol_version": protocol,
                    "task_retention_seconds": 86400,
                },
            )
        ),
    ) as http:
        with pytest.raises(DomainError, match="MINERU_PROTOCOL_INVALID"):
            await MinerUClient(http).health()


@pytest.mark.asyncio
async def test_query_deadline_is_retryable_and_closes_the_stalled_body():
    class StalledStream(ChunkStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

    stream = StalledStream([])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ) as http:
        client = MinerUClient(http, limits=MinerULimits(request_timeout_seconds=0.02))
        with pytest.raises(DomainError) as error:
            await asyncio.wait_for(client.query(TASK_ID), timeout=1)
        assert error.value.code == "MINERU_UNAVAILABLE"
        assert error.value.retryable is True
        assert stream.closed is True


@pytest.mark.asyncio
async def test_submission_has_a_total_deadline_even_if_upstream_keeps_connection_open():
    async def upstream(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        base_url="http://mineru.test", transport=httpx.MockTransport(upstream)
    ) as http:
        client = MinerUClient(http, limits=MinerULimits(submission_timeout_seconds=0.02))
        with pytest.raises(DomainError, match="MINERU_SUBMIT_UNKNOWN"):
            await asyncio.wait_for(
                client.submit(
                    BytesIO(b"pdf"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
                ),
                timeout=1,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, code", [("query", "MINERU_RESPONSE_TOO_LARGE"), ("submit", "MINERU_SUBMIT_UNKNOWN")]
)
async def test_metadata_size_limit_also_preserves_submission_uncertainty(operation, code):
    stream = ChunkStream([b"123456789"])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(202 if operation == "submit" else 200, stream=stream)
        ),
    ) as http:
        client = MinerUClient(http, limits=MinerULimits(metadata_max_bytes=8))
        with pytest.raises(DomainError, match=code):
            if operation == "query":
                await client.query(TASK_ID)
            else:
                await client.submit(
                    BytesIO(b"pdf"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
                )
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, media_type, code",
    [
        (202, "application/json", "MINERU_RESULT_PENDING"),
        (404, "application/json", "MINERU_TASK_NOT_FOUND"),
        (409, "application/json", "MINERU_TASK_FAILED"),
        (503, "text/html", "MINERU_UNAVAILABLE"),
        (200, "text/html", "MINERU_PROTOCOL_INVALID"),
    ],
)
async def test_download_rejects_non_archive_results(status, media_type, code):
    stream = ChunkStream([b"not an archive"])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"Content-Type": media_type}, stream=stream
            )
        ),
    ) as http:
        with pytest.raises(DomainError) as error:
            async with MinerUClient(http).download(TASK_ID) as content:
                _ = [part async for part in content]
        assert error.value.code == code
        assert stream.closed is True


@pytest.mark.asyncio
async def test_download_size_limit_is_enforced_when_server_omits_content_length():
    stream = ChunkStream([b"1234", b"56789"])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/zip"}, stream=stream
            )
        ),
    ) as http:
        client = MinerUClient(http, limits=MinerULimits(download_max_bytes=8))
        with pytest.raises(DomainError, match="MINERU_RESPONSE_TOO_LARGE"):
            async with client.download(TASK_ID) as content:
                _ = [part async for part in content]
        assert stream.closed is True


@pytest.mark.asyncio
async def test_download_deadline_closes_a_stalled_response():
    class StalledStream(ChunkStream):
        async def __aiter__(self):
            yield b"PK"
            await asyncio.Event().wait()

    stream = StalledStream([])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/zip"}, stream=stream
            )
        ),
    ) as http:
        client = MinerUClient(http, limits=MinerULimits(download_timeout_seconds=0.03))
        with pytest.raises(DomainError, match="MINERU_DOWNLOAD_TIMEOUT"):
            async with client.download(TASK_ID) as content:
                _ = [part async for part in content]
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, code",
    [
        (httpx.ReadError, "MINERU_UNAVAILABLE"),
        (httpx.ReadTimeout, "MINERU_DOWNLOAD_TIMEOUT"),
    ],
)
async def test_broken_download_is_retryable_and_closes_the_partial_stream(failure, code):
    class BrokenStream(ChunkStream):
        async def __aiter__(self):
            yield b"PK"
            raise failure("upstream internal failure")

    stream = BrokenStream([])
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "application/zip"}, stream=stream
            )
        ),
    ) as http:
        with pytest.raises(DomainError) as error:
            async with MinerUClient(http).download(TASK_ID) as content:
                _ = [part async for part in content]
        assert error.value.code == code
        assert error.value.retryable is True
        assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"backend": "pipeline"}, {"file_names": ["other"]}])
async def test_submission_receipt_must_match_its_frozen_input_and_backend(changed):
    async with httpx.AsyncClient(
        base_url="http://mineru.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(202, json={**task_payload(), **changed})
        ),
    ) as http:
        with pytest.raises(DomainError, match="MINERU_SUBMIT_UNKNOWN"):
            await MinerUClient(http).submit(
                BytesIO(b"pdf"), request_id=TASK_ID, options=MinerUOptions(page_count=1)
            )
