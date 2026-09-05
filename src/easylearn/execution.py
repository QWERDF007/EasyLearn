import asyncio
import math
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress

from pydantic import BaseModel, TypeAdapter, ValidationError

from easylearn.errors import DomainError
from easylearn.jobs.schema import JobFailure


async def run_blocking[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Join blocking I/O before propagating cancellation or releasing its file handles."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_validation[T: BaseModel](
    module: str,
    request: BaseModel,
    response: type[T],
    *,
    timeout: float,
    error_prefix: str,
) -> T:
    """Run a trusted validation entry point; reap its child before returning on cancellation."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Validation timeout must be finite and positive")
    payload = request.model_dump_json().encode()
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    )
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.shield(spawning)
            stdout, _ = await process.communicate(payload)
    except TimeoutError:
        raise DomainError(
            f"{error_prefix}_TIMEOUT", "Validation exceeded time limit", retryable=True
        ) from None
    except OSError:
        raise DomainError(
            f"{error_prefix}_PROCESS_FAILED", "Validation process could not start", retryable=True
        ) from None
    finally:
        cleanup = asyncio.create_task(_reap_child(spawning))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
    if process.returncode != 0:
        raise DomainError(
            f"{error_prefix}_PROCESS_FAILED", "Validation process failed", retryable=True
        )
    try:
        adapter: TypeAdapter[T | JobFailure] = TypeAdapter(response | JobFailure)
        result = adapter.validate_json(stdout)
    except ValidationError:
        raise DomainError(
            f"{error_prefix}_PROTOCOL_INVALID", "Invalid validation response"
        ) from None
    if isinstance(result, JobFailure):
        raise DomainError(result.code, result.message, retryable=result.retryable)
    return result


async def _reap_child(spawning: asyncio.Task[asyncio.subprocess.Process]) -> None:
    """One shieldable lifecycle operation, including the in-flight OS spawn handshake."""
    try:
        process = await spawning
    except OSError:
        return
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.communicate()
