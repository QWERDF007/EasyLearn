from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import urllib.request
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.document_ir.schema import (
    DocumentIR,
    ImageNode,
    LinkNode,
    ReferenceNode,
    TextNode,
)
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.tasks import TaskContext, TaskManager, TaskRecord

logger = logging.getLogger(__name__)


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    block_ids: tuple[str, ...] | None = None
    force: bool = False


class EditTranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    block_id: str
    expected_revision: int = Field(ge=0)
    text: str | None = None
    use_manual: bool | None = None
    locked: bool | None = None


class RestoreTranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    block_id: str
    expected_revision: int = Field(ge=0)


class TranslationUnitView(BaseModel):
    model_config = ConfigDict(frozen=True)

    parse_id: UUID
    block_id: str
    unit_id: str
    source_text: str
    auto_text: str | None = None
    manual_text: str | None = None
    effective_text: str
    use_manual: bool
    locked: bool
    revision: int


class TranslationHistoryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    parse_id: UUID
    block_id: str
    unit_id: str
    text: str
    origin: str
    revision: int
    created_at: datetime


@dataclass(frozen=True)
class TranslationUnit:
    block_id: str
    unit_id: str
    source_text: str
    kind: str


def translation_units(
    ir: DocumentIR, block_ids: set[str] | None = None
) -> tuple[TranslationUnit, ...]:
    result: list[TranslationUnit] = []
    for block in ir.blocks:
        if block_ids is not None and block.block_id not in block_ids:
            continue
        for node in block.source_nodes:
            text = (
                node.text
                if isinstance(node, TextNode)
                else node.label
                if isinstance(node, (LinkNode, ReferenceNode))
                else node.alt
                if isinstance(node, ImageNode)
                else ""
            )
            if text.strip():
                result.append(
                    TranslationUnit(
                        block_id=block.block_id,
                        unit_id=f"{block.block_id}:{node.node_id}",
                        source_text=text,
                        kind=node.type,
                    )
                )
    return tuple(result)


def compute_retry_delay(
    attempt: int,
    min_delay: float = 2.0,
    max_delay: float = 30.0,
    *,
    retry_after: float | None = None,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff delay with full jitter and Retry-After support."""
    if min_delay <= 0 and max_delay <= 0:
        return 0.0
    ceiling = min(max_delay, max(min_delay, min_delay * (2**attempt)))
    if jitter and min_delay > 0:
        lower = min_delay * 0.5
        calculated = random.uniform(lower, ceiling)
    else:
        calculated = ceiling
    if retry_after is not None and retry_after > 0:
        return min(max_delay, max(calculated, retry_after))
    return calculated


def _parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        val = float(header.strip())
        return val if val >= 0 else None
    except ValueError:
        return None


class LLMClient:
    """Small OpenAI-compatible HTTP adapter; prompts and structure stay in this module."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        configuration = self.settings.llm
        if not configuration.model or not configuration.base_url:
            raise DomainError(
                "LLM_NOT_CONFIGURED", "Configure an OpenAI-compatible LLM", status=503
            )
        _ensure_local_policy(configuration.base_url, configuration.local_only)
        headers = (
            {"Authorization": f"Bearer {self.settings.llm_api_key}"}
            if self.settings.llm_api_key
            else {}
        )
        payload: dict[str, Any] = {
            "model": configuration.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if configuration.reasoning_effort:
            payload["reasoning_effort"] = configuration.reasoning_effort
        if configuration.json_mode:
            payload["response_format"] = {"type": "json_object"}
        max_retries = getattr(self.settings.llm, "max_retries", 5)
        min_delay = getattr(self.settings.llm, "retry_min_delay", 2.0)
        max_delay = getattr(self.settings.llm, "retry_max_delay", 30.0)
        response: httpx.Response | None = None
        for attempt in range(max_retries + 1):
            try:
                async with self._client() as http:
                    response = await http.post(
                        f"{configuration.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=configuration.timeout_seconds,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                exc_name = type(exc).__name__
                if attempt < max_retries:
                    delay = compute_retry_delay(attempt, min_delay, max_delay)
                    logger.warning(
                        "LLM request error (%s: %s) on attempt %d/%d; retrying in %.1fs...",
                        exc_name,
                        exc,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    logger.error(
                        "LLM request timed out after %d attempts: %s", max_retries + 1, exc
                    )
                    raise DomainError(
                        "LLM_TIMEOUT", "LLM request timed out", retryable=True
                    ) from exc
                logger.error(
                    "LLM transport error after %d attempts: %s (%s)",
                    max_retries + 1,
                    exc,
                    exc_name,
                )
                detail = (
                    f"LLM connection error ({exc_name}): {exc}"
                    if str(exc)
                    else f"LLM connection error: {exc_name}"
                )
                raise DomainError("LLM_UNAVAILABLE", detail, retryable=True) from exc

            if response.status_code >= 500 or response.status_code == 429:
                if attempt < max_retries:
                    retry_after = _parse_retry_after(response)
                    delay = compute_retry_delay(
                        attempt, min_delay, max_delay, retry_after=retry_after
                    )
                    logger.warning(
                        "LLM returned HTTP %d on attempt %d/%d; retrying in %.1fs...",
                        response.status_code,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DomainError(
                    "LLM_UNAVAILABLE",
                    f"LLM is temporarily unavailable (HTTP {response.status_code})",
                    retryable=True,
                )
            if response.status_code >= 400:
                body_snippet = response.text[:200]
                logger.error(
                    "LLM rejected request: HTTP %d, body=%s", response.status_code, body_snippet
                )
                raise DomainError(
                    "LLM_REQUEST_REJECTED",
                    f"LLM rejected the request (HTTP {response.status_code}): {body_snippet}",
                )
            break
        try:
            value = response.json()
            content = value["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise DomainError("LLM_PROTOCOL_INVALID", "LLM returned an invalid response") from None
        if not isinstance(content, str) or not content.strip():
            raise DomainError("LLM_PROTOCOL_INVALID", "LLM returned an empty response")
        return content

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        """Yield content deltas from an OpenAI-compatible streaming response."""

        configuration = self.settings.llm
        if not configuration.model or not configuration.base_url:
            raise DomainError(
                "LLM_NOT_CONFIGURED", "Configure an OpenAI-compatible LLM", status=503
            )
        _ensure_local_policy(configuration.base_url, configuration.local_only)
        headers = (
            {"Authorization": f"Bearer {self.settings.llm_api_key}"}
            if self.settings.llm_api_key
            else {}
        )
        payload: dict[str, Any] = {
            "model": configuration.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
        if configuration.reasoning_effort:
            payload["reasoning_effort"] = configuration.reasoning_effort
        max_retries = getattr(self.settings.llm, "max_retries", 5)
        min_delay = getattr(self.settings.llm, "retry_min_delay", 2.0)
        max_delay = getattr(self.settings.llm, "retry_max_delay", 30.0)
        for attempt in range(max_retries + 1):
            try:
                async with (
                    self._client() as http,
                    http.stream(
                        "POST",
                        f"{configuration.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=configuration.timeout_seconds,
                    ) as response,
                ):
                    if response.status_code >= 500 or response.status_code == 429:
                        if attempt < max_retries:
                            retry_after = _parse_retry_after(response)
                            delay = compute_retry_delay(
                                attempt, min_delay, max_delay, retry_after=retry_after
                            )
                            logger.warning(
                                "LLM stream returned HTTP %d on attempt %d/%d; "
                                "retrying in %.1fs...",
                                response.status_code,
                                attempt + 1,
                                max_retries + 1,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise DomainError(
                            "LLM_UNAVAILABLE",
                            f"LLM is temporarily unavailable (HTTP {response.status_code})",
                            retryable=True,
                        )
                    if response.status_code >= 400:
                        raise DomainError("LLM_REQUEST_REJECTED", "LLM rejected the request")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            payload_value = json.loads(data)
                            choices = payload_value.get("choices") or []
                            delta = choices[0]["delta"].get("content", "") if choices else ""
                        except (ValueError, KeyError, IndexError, TypeError):
                            raise DomainError(
                                "LLM_PROTOCOL_INVALID", "LLM returned an invalid stream event"
                            ) from None
                        if isinstance(delta, str) and delta:
                            yield delta
                    return
            except DomainError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                exc_name = type(exc).__name__
                if attempt < max_retries:
                    delay = compute_retry_delay(attempt, min_delay, max_delay)
                    logger.warning(
                        "LLM stream error (%s: %s) on attempt %d/%d; retrying in %.1fs...",
                        exc_name,
                        exc,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    logger.error(
                        "LLM stream timed out after %d attempts: %s", max_retries + 1, exc
                    )
                    raise DomainError(
                        "LLM_TIMEOUT", "LLM request timed out", retryable=True
                    ) from exc
                logger.error(
                    "LLM stream transport error after %d attempts: %s (%s)",
                    max_retries + 1,
                    exc,
                    exc_name,
                )
                detail = (
                    f"LLM connection error ({exc_name}): {exc}"
                    if str(exc)
                    else f"LLM connection error: {exc_name}"
                )
                raise DomainError("LLM_UNAVAILABLE", detail, retryable=True) from exc

    def _resolve_proxy(self) -> str | None:
        explicit = getattr(self.settings.llm, "proxy", None)
        if explicit is not None:
            explicit = explicit.strip()
            if not explicit or explicit.lower() in ("none", "false", "off", "direct"):
                return None
            return explicit
        if not self.settings.llm.local_only:
            env_proxy = (
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("ALL_PROXY")
            )
            if env_proxy:
                return env_proxy
            try:
                system_proxies = urllib.request.getproxies()
                return system_proxies.get("https") or system_proxies.get("http")
            except Exception:
                return None
        return None

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.http is not None:
            yield self.http
        else:
            trust_env = not self.settings.llm.local_only
            proxy = self._resolve_proxy()
            async with httpx.AsyncClient(
                trust_env=trust_env, proxy=proxy, follow_redirects=False
            ) as client:
                yield client


class TranslationService:
    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        manager: TaskManager,
        settings: Settings,
        llm: LLMClient | None = None,
    ) -> None:
        self.database = database
        self.documents = documents
        self.manager = manager
        self.settings = settings
        self.llm = llm or LLMClient(settings)

    async def submit(self, document_id: UUID, request: TranslateRequest) -> TaskView:
        ir = await self.documents.load_ir(document_id, request.parse_id)
        selected = set(request.block_ids) if request.block_ids is not None else None
        if selected is not None and any(
            block_id not in {block.block_id for block in ir.blocks} for block_id in selected
        ):
            raise DomainError("BLOCK_NOT_FOUND", "A selected block does not exist", status=404)
        units = translation_units(ir, selected)
        if not units:
            raise DomainError(
                "TRANSLATION_SCOPE_EMPTY", "The selected blocks contain no translatable text"
            )

        async def admit() -> None:
            await self.documents.load_ir(document_id, request.parse_id)

        task_view = await self.manager.submit(
            document_id,
            JobKind.TRANSLATE,
            {
                "parse_id": str(request.parse_id),
                "block_ids": list(request.block_ids) if request.block_ids is not None else None,
                "force": request.force,
            },
            admission=admit,
        )
        logger.info(
            "Translation task submitted: document_id=%s, task_id=%s, parse_id=%s",
            document_id,
            task_view.task_id,
            request.parse_id,
        )
        return task_view

    async def execute(self, record: TaskRecord, context: TaskContext) -> dict[str, JsonValue]:
        parse_id = UUID(str(record.scope["parse_id"]))
        raw_ids = record.scope.get("block_ids")
        force = bool(record.scope.get("force", False))
        if raw_ids is None:
            selected = None
        elif isinstance(raw_ids, list):
            string_ids = [item for item in raw_ids if isinstance(item, str)]
            if len(string_ids) != len(raw_ids):
                raise DomainError("TRANSLATION_SCOPE_INVALID", "Translation scope is invalid")
            selected = set(string_ids)
        else:
            raise DomainError("TRANSLATION_SCOPE_INVALID", "Translation scope is invalid")
        ir = await self.documents.load_ir(record.document_id, parse_id)
        units = translation_units(ir, selected)
        if not units:
            raise DomainError(
                "TRANSLATION_SCOPE_EMPTY", "The selected blocks contain no translatable text"
            )
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT block_id, unit_id, revision, manual_text, use_manual, "
                    "locked, auto_text FROM translations WHERE parse_id = ?",
                    (str(parse_id),),
                )
            ).fetchall()
        snapshots = {row[1]: (row[2], row[3], bool(row[4]), bool(row[5])) for row in rows}
        existing_auto = {row[1]: row[6] for row in rows if row[6] is not None}

        if force:
            units_to_translate = units
        else:
            units_to_translate = tuple(u for u in units if u.unit_id not in existing_auto)

        conflict_count = 0
        total_units = len(units)
        already_completed = total_units - len(units_to_translate)

        if not units_to_translate:
            logger.info(
                "Translation already up-to-date: document_id=%s, parse_id=%s, units=%d",
                record.document_id,
                parse_id,
                total_units,
            )
            await context.progress(1.0, f"已翻译 {total_units}/{total_units} 单元 (100%)")
            result_ref: dict[str, JsonValue] = {
                "parse_id": str(parse_id),
                "translated_units": total_units,
                "conflicted_units": 0,
            }

            async def noop_publish() -> None:
                pass

            await context.publish(result_ref, noop_publish)
            return result_ref

        batches = [b for b in _batches(units_to_translate) if b]
        if not batches:
            logger.info(
                "Translation already up-to-date: document_id=%s, parse_id=%s, units=%d",
                record.document_id,
                parse_id,
                total_units,
            )
            await context.progress(1.0, f"已翻译 {total_units}/{total_units} 单元 (100%)")
            result_ref: dict[str, JsonValue] = {
                "parse_id": str(parse_id),
                "translated_units": total_units,
                "conflicted_units": 0,
            }

            async def noop_publish() -> None:
                pass

            await context.publish(result_ref, noop_publish)
            return result_ref
        concurrency = max(1, self.settings.tasks.translation_concurrency)
        logger.info(
            "Translation started: document_id=%s, parse_id=%s, total_units=%d, "
            "to_translate=%d, batches=%d, concurrency=%d",
            record.document_id,
            parse_id,
            total_units,
            len(units_to_translate),
            len(batches),
            concurrency,
        )
        semaphore = asyncio.Semaphore(concurrency)
        completed_batches = 0
        completed_units = already_completed
        progress_lock = asyncio.Lock()
        start_time = time.monotonic()

        async def publish_batch(
            batch: tuple[TranslationUnit, ...], batch_result: dict[str, str]
        ) -> None:
            nonlocal conflict_count
            timestamp = _now()
            async with self.database.transaction() as connection:
                for unit in batch:
                    value = batch_result[unit.unit_id]
                    row = await (
                        await connection.execute(
                            "SELECT auto_text, manual_text, use_manual, locked, revision "
                            "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                            (str(parse_id), unit.block_id, unit.unit_id),
                        )
                    ).fetchone()
                    if row is None:
                        await connection.execute(
                            "INSERT INTO translations "
                            "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, "
                            "locked, revision, updated_at) "
                            "VALUES (?, ?, ?, ?, NULL, 0, 0, 0, ?)",
                            (str(parse_id), unit.block_id, unit.unit_id, value, timestamp),
                        )
                        revision = 0
                    else:
                        revision = row[4]
                        if revision != snapshots.get(unit.unit_id, (0, None, False, False))[0]:
                            conflict_count += 1
                        await connection.execute(
                            "UPDATE translations SET auto_text = ?, updated_at = ? "
                            "WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                            (value, timestamp, str(parse_id), unit.block_id, unit.unit_id),
                        )
                    await connection.execute(
                        "INSERT INTO translation_history "
                        "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'auto', ?, ?)",
                        (
                            str(uuid4()),
                            str(parse_id),
                            unit.block_id,
                            unit.unit_id,
                            value,
                            revision,
                            timestamp,
                        ),
                    )
                    await _trim_history(
                        connection,
                        parse_id,
                        unit.block_id,
                        unit.unit_id,
                        self.settings.files.revision_history_limit,
                    )

        async def translate_worker(
            index: int, batch: tuple[TranslationUnit, ...]
        ) -> dict[str, str]:
            nonlocal completed_batches, completed_units
            async with semaphore:
                await context.check()
                t0 = time.monotonic()
                batch_result = await self._translate_batch(batch)
                elapsed = time.monotonic() - t0
                await publish_batch(batch, batch_result)
                async with progress_lock:
                    completed_batches += 1
                    completed_units += len(batch)
                    curr_completed_units = completed_units
                logger.info(
                    "Translation batch %d/%d completed (%d units, %.2fs, progress %d/%d units)",
                    index + 1,
                    len(batches),
                    len(batch),
                    elapsed,
                    curr_completed_units,
                    total_units,
                )
                fraction = curr_completed_units / total_units
                percent = int(fraction * 100)
                await context.progress(
                    fraction,
                    f"已翻译 {curr_completed_units}/{total_units} 单元 ({percent}%)",
                )
                return batch_result

        tasks = [
            asyncio.create_task(translate_worker(index, batch))
            for index, batch in enumerate(batches)
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException as exc:
            for t in tasks:
                if not t.done():
                    t.cancel()
            logger.error(
                "Translation failed: document_id=%s, parse_id=%s, error=%s",
                record.document_id,
                parse_id,
                exc,
            )
            raise

        total_elapsed = time.monotonic() - start_time
        logger.info(
            "Translation completed: document_id=%s, parse_id=%s, units=%d, elapsed=%.2fs",
            record.document_id,
            parse_id,
            total_units,
            total_elapsed,
        )
        await context.check()
        result_ref = {
            "parse_id": str(parse_id),
            "translated_units": total_units,
            "conflicted_units": conflict_count,
        }

        async def finalize_publish() -> None:
            pass

        await context.publish(result_ref, finalize_publish)
        return result_ref

    async def list(self, document_id: UUID, parse_id: UUID) -> tuple[TranslationUnitView, ...]:
        ir = await self.documents.load_ir(document_id, parse_id)
        units = translation_units(ir)
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision "
                    "FROM translations WHERE parse_id = ?",
                    (str(parse_id),),
                )
            ).fetchall()
        values = {row[1]: row for row in rows}
        return tuple(
            TranslationUnitView(
                parse_id=parse_id,
                block_id=unit.block_id,
                unit_id=unit.unit_id,
                source_text=unit.source_text,
                auto_text=values[unit.unit_id][2] if unit.unit_id in values else None,
                manual_text=values[unit.unit_id][3] if unit.unit_id in values else None,
                effective_text=_effective(unit, values.get(unit.unit_id)),
                use_manual=bool(values[unit.unit_id][4]) if unit.unit_id in values else False,
                locked=bool(values[unit.unit_id][5]) if unit.unit_id in values else False,
                revision=values[unit.unit_id][6] if unit.unit_id in values else 0,
            )
            for unit in units
        )

    async def effective_map(self, document_id: UUID, parse_id: UUID) -> dict[str, str]:
        return {
            item.unit_id: item.effective_text
            for item in await self.list(document_id, parse_id)
            if item.auto_text is not None or item.manual_text is not None
        }

    async def edit(
        self, document_id: UUID, unit_id: str, request: EditTranslationRequest
    ) -> TranslationUnitView:
        ir = await self.documents.load_ir(document_id, request.parse_id)
        unit = next((item for item in translation_units(ir) if item.unit_id == unit_id), None)
        if unit is None or unit.block_id != request.block_id:
            raise DomainError(
                "TRANSLATION_UNIT_NOT_FOUND", "Translation unit not found", status=404
            )
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT auto_text, manual_text, use_manual, locked, revision "
                    "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(request.parse_id), request.block_id, unit_id),
                )
            ).fetchone()
            current_revision = row[4] if row else 0
            if current_revision != request.expected_revision:
                raise DomainError(
                    "TRANSLATION_REVISION_CONFLICT",
                    "Translation changed; keep the submitted draft and reload the current value",
                    status=409,
                )
            if row and row[3] and request.text is not None and request.locked is not False:
                raise DomainError(
                    "TRANSLATION_LOCKED", "Unlock the translation before editing", status=409
                )
            auto_text = row[0] if row else None
            old_manual = row[1] if row else None
            old_use_manual = bool(row[2]) if row else False
            old_locked = bool(row[3]) if row else False
            manual_text = request.text if request.text is not None else old_manual
            use_manual = (
                request.use_manual
                if request.use_manual is not None
                else request.text is not None or old_use_manual
            )
            locked = (
                request.locked
                if request.locked is not None
                else (False if use_manual is False else old_locked)
            )
            if request.text is not None and not request.text.strip():
                raise DomainError("TRANSLATION_TEXT_EMPTY", "Manual translation cannot be empty")
            new_revision = current_revision + 1
            effective_before = (
                old_manual
                if old_use_manual and old_manual is not None
                else auto_text or unit.source_text
            )
            await connection.execute(
                "INSERT INTO translation_history "
                "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)",
                (
                    str(uuid4()),
                    str(request.parse_id),
                    request.block_id,
                    unit_id,
                    effective_before,
                    new_revision,
                    _now(),
                ),
            )
            if row is not None:
                await connection.execute(
                    "UPDATE translations SET manual_text = ?, use_manual = ?, locked = ?, "
                    "revision = ?, updated_at = ? WHERE parse_id = ? AND block_id = ? "
                    "AND unit_id = ?",
                    (
                        manual_text,
                        int(use_manual),
                        int(locked),
                        new_revision,
                        _now(),
                        str(request.parse_id),
                        request.block_id,
                        unit_id,
                    ),
                )
            else:
                await connection.execute(
                    "INSERT INTO translations "
                    "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        str(request.parse_id),
                        request.block_id,
                        unit_id,
                        manual_text,
                        int(use_manual),
                        int(locked),
                        new_revision,
                        _now(),
                    ),
                )
            await _trim_history(
                connection,
                request.parse_id,
                request.block_id,
                unit_id,
                self.settings.files.revision_history_limit,
            )
        values = await self.list(document_id, request.parse_id)
        result = next(item for item in values if item.unit_id == unit_id)
        logger.info(
            "Translation edited: document_id=%s, parse_id=%s, unit_id=%s, revision=%d",
            document_id,
            request.parse_id,
            unit_id,
            new_revision,
        )
        return result

    async def restore(
        self,
        document_id: UUID,
        unit_id: str,
        history_id: UUID,
        request: RestoreTranslationRequest,
    ) -> TranslationUnitView:
        """Restore a history value as a new manual revision."""

        ir = await self.documents.load_ir(document_id, request.parse_id)
        unit = next((item for item in translation_units(ir) if item.unit_id == unit_id), None)
        if unit is None or unit.block_id != request.block_id:
            raise DomainError(
                "TRANSLATION_UNIT_NOT_FOUND", "Translation unit not found", status=404
            )
        async with self.database.transaction() as connection:
            history = await (
                await connection.execute(
                    "SELECT text FROM translation_history WHERE id = ? AND parse_id = ? "
                    "AND block_id = ? AND unit_id = ?",
                    (str(history_id), str(request.parse_id), request.block_id, unit_id),
                )
            ).fetchone()
            if history is None:
                raise DomainError(
                    "TRANSLATION_HISTORY_NOT_FOUND", "Translation history not found", status=404
                )
            row = await (
                await connection.execute(
                    "SELECT auto_text, manual_text, use_manual, locked, revision "
                    "FROM translations WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (str(request.parse_id), request.block_id, unit_id),
                )
            ).fetchone()
            current_revision = row[4] if row else 0
            if current_revision != request.expected_revision:
                raise DomainError(
                    "TRANSLATION_REVISION_CONFLICT",
                    "Translation changed; keep the submitted draft and reload the current value",
                    status=409,
                )
            if row is not None and row[3]:
                raise DomainError(
                    "TRANSLATION_LOCKED",
                    "Unlock the translation before restoring a revision",
                    status=409,
                )
            previous = (
                row[1]
                if row is not None and row[2] and row[1] is not None
                else row[0]
                if row is not None and row[0] is not None
                else unit.source_text
            )
            revision = current_revision + 1
            timestamp = _now()
            await connection.execute(
                "INSERT INTO translation_history "
                "(id, parse_id, block_id, unit_id, text, origin, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'restore', ?, ?)",
                (
                    str(uuid4()),
                    str(request.parse_id),
                    request.block_id,
                    unit_id,
                    previous,
                    revision,
                    timestamp,
                ),
            )
            if row is None:
                await connection.execute(
                    "INSERT INTO translations "
                    "(parse_id, block_id, unit_id, auto_text, manual_text, use_manual, locked, "
                    "revision, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, 1, 0, ?, ?)",
                    (
                        str(request.parse_id),
                        request.block_id,
                        unit_id,
                        history[0],
                        revision,
                        timestamp,
                    ),
                )
            else:
                await connection.execute(
                    "UPDATE translations SET manual_text = ?, use_manual = 1, revision = ?, "
                    "updated_at = ? "
                    "WHERE parse_id = ? AND block_id = ? AND unit_id = ?",
                    (
                        history[0],
                        revision,
                        timestamp,
                        str(request.parse_id),
                        request.block_id,
                        unit_id,
                    ),
                )
            await _trim_history(
                connection,
                request.parse_id,
                request.block_id,
                unit_id,
                self.settings.files.revision_history_limit,
            )
        values = await self.list(document_id, request.parse_id)
        result = next(item for item in values if item.unit_id == unit_id)
        logger.info(
            "Translation restored: document_id=%s, parse_id=%s, unit_id=%s, revision=%d",
            document_id,
            request.parse_id,
            unit_id,
            revision,
        )
        return result

    async def history(
        self, document_id: UUID, parse_id: UUID, unit_id: str
    ) -> tuple[TranslationHistoryView, ...]:
        await self.documents.load_ir(document_id, parse_id)
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT id, block_id, unit_id, text, origin, revision, created_at "
                    "FROM translation_history WHERE parse_id = ? AND unit_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(parse_id), unit_id, self.settings.files.revision_history_limit),
                )
            ).fetchall()
        return tuple(
            TranslationHistoryView(
                id=UUID(row[0]),
                parse_id=parse_id,
                block_id=row[1],
                unit_id=row[2],
                text=row[3],
                origin=row[4],
                revision=row[5],
                created_at=datetime.fromisoformat(row[6]),
            )
            for row in rows
        )

    async def _translate_batch(self, units: tuple[TranslationUnit, ...]) -> dict[str, str]:
        completed_results: dict[str, str] = {}
        pending_units: list[TranslationUnit] = list(units)
        max_attempts = 1 + max(0, getattr(self.settings.llm, "max_retries", 2))
        last_structure_error: str | None = None
        last_json_error: bool = False
        last_empty_error: bool = False

        for attempt in range(max_attempts):
            payload = {unit.unit_id: unit.source_text for unit in pending_units}
            content = await self.llm.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Translate English document text into Simplified Chinese. "
                            "Return only a JSON object mapping every supplied unit_id to one "
                            "non-empty string. Do not add, remove, or rename IDs. Preserve "
                            "numbers, URLs, paths, and {{PLACEHOLDER}} tokens."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            content = _strip_code_fence(content)
            try:
                raw_value = json.loads(content, object_pairs_hook=_unique_json_object)
            except (ValueError, TypeError):
                last_json_error = True
                logger.warning(
                    "LLM returned invalid JSON on attempt %d/%d for %d units: %s",
                    attempt + 1,
                    max_attempts,
                    len(pending_units),
                    content[:200],
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue

            if not isinstance(raw_value, dict):
                last_json_error = True
                logger.warning(
                    "LLM did not return a JSON object on attempt %d/%d: %s",
                    attempt + 1,
                    max_attempts,
                    content[:200],
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue

            cleaned_value: dict[str, Any] = {
                k.strip(): v for k, v in raw_value.items() if isinstance(k, str)
            }

            for unit in list(pending_units):
                if unit.unit_id not in cleaned_value:
                    continue
                text = cleaned_value[unit.unit_id]
                if not isinstance(text, str) or not text.strip():
                    last_empty_error = True
                    logger.warning(
                        "LLM returned empty translation for %s on attempt %d/%d",
                        unit.unit_id,
                        attempt + 1,
                        max_attempts,
                    )
                    continue

                expected_tokens = _protected_tokens(unit.source_text)
                actual_tokens = _protected_tokens(text)
                if expected_tokens != actual_tokens:
                    logger.warning(
                        "Translation structure invalid for %s on attempt %d/%d: "
                        "expected=%s, actual=%s",
                        unit.unit_id,
                        attempt + 1,
                        max_attempts,
                        expected_tokens,
                        actual_tokens,
                    )
                    last_structure_error = f"Protected tokens changed for {unit.unit_id}"
                    continue

                completed_results[unit.unit_id] = text

            pending_units = [u for u in units if u.unit_id not in completed_results]
            if not pending_units:
                return completed_results

            missing_ids = [u.unit_id for u in pending_units]
            extra_ids = [k for k in cleaned_value if k not in {u.unit_id for u in units}]
            logger.warning(
                "LLM batch translation incomplete (attempt %d/%d): %d/%d resolved, "
                "missing=%s, extra=%s",
                attempt + 1,
                max_attempts,
                len(completed_results),
                len(units),
                missing_ids,
                extra_ids,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.1 * (attempt + 1))

        if last_structure_error is not None:
            raise DomainError("TRANSLATION_STRUCTURE_INVALID", last_structure_error)
        if last_json_error and not completed_results:
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "LLM did not return a JSON object"
            )
        if last_empty_error and not completed_results:
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "LLM returned empty translation text"
            )
        missing_ids = [u.unit_id for u in pending_units]
        raise DomainError(
            "TRANSLATION_PROTOCOL_INVALID",
            f"LLM returned missing translation IDs: {missing_ids}",
        )


def _batches(units: tuple[TranslationUnit, ...]) -> list[tuple[TranslationUnit, ...]]:
    batches: list[tuple[TranslationUnit, ...]] = []
    current: list[TranslationUnit] = []
    size = 0
    for unit in units:
        if current and (len(current) >= 20 or size + len(unit.source_text) > 8000):
            batches.append(tuple(current))
            current = []
            size = 0
        current.append(unit)
        size += len(unit.source_text)
    if current:
        batches.append(tuple(current))
    return batches


_TRAILING_URL_PUNCT = (
    r"[\.,;:!\?\)>\"'\]\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u300b\u201d\u2019]+"
)


def _protected_tokens(text: str) -> Counter[str]:
    placeholders = re.findall(r"\{\{[^{}]+\}\}", text)
    raw_urls = re.findall(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+", text)
    urls = [re.sub(f"{_TRAILING_URL_PUNCT}$", "", u) for u in raw_urls]
    return Counter(placeholders + urls)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _effective(unit: TranslationUnit, row: Row | None) -> str:
    if row is None:
        return unit.source_text
    return row[3] if row[4] and row[3] is not None else row[2] or unit.source_text


async def _trim_history(
    connection: Any, parse_id: UUID, block_id: str, unit_id: str, limit: int
) -> None:
    await connection.execute(
        "DELETE FROM translation_history WHERE parse_id = ? AND block_id = ? AND unit_id = ? "
        "AND id NOT IN (SELECT id FROM translation_history "
        "WHERE parse_id = ? AND block_id = ? AND unit_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?)",
        (str(parse_id), block_id, unit_id, str(parse_id), block_id, unit_id, limit),
    )


def _ensure_local_policy(base_url: str, local_only: bool) -> None:
    if not local_only:
        return
    from urllib.parse import urlparse

    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise DomainError(
            "EXTERNAL_SERVICE_BLOCKED", "local_only blocks this LLM address", status=422
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()
