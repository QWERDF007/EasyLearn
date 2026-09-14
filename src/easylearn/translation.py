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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from easylearn.config import LLMSettings, Settings
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
from easylearn.source_edits import compute_unit_fingerprint
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
    source_fingerprint: str | None = None
    stale: bool = False


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

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        provider: str | None = None,
        for_qa: bool = False,
    ) -> None:
        self.settings = settings
        self.http = http
        self.provider = provider
        self.for_qa = for_qa

    @property
    def configuration(self) -> LLMSettings:
        if self.for_qa:
            return self.settings.qa_llm
        if self.provider:
            return self.settings.llm.for_provider(self.provider)
        return self.settings.llm

    @property
    def api_key(self) -> str | None:
        cfg = self.configuration
        if cfg.api_key:
            return cfg.api_key
        return os.environ.get(cfg.api_key_env) if cfg.api_key_env else None

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
        stateless: bool = False,
    ) -> str:
        configuration = self.configuration
        if not configuration.model or not configuration.base_url:
            raise DomainError(
                "LLM_NOT_CONFIGURED", "Configure an OpenAI-compatible LLM", status=503
            )
        _ensure_local_policy(configuration.base_url, configuration.local_only)
        api_key = self.api_key
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key
            else {}
        )
        if session_id:
            headers["x-agent-session"] = session_id
        if stateless:
            headers["x-stateless"] = "true"
        if self.for_qa:
            headers["x-thinking-enabled"] = "true"
        payload: dict[str, Any] = {
            "model": configuration.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if session_id:
            payload["user"] = session_id
        if self.for_qa and (
            configuration.local_only
            or "deepseek" in configuration.base_url.lower()
            or "deepseek" in configuration.model.lower()
        ):
            payload["thinking_enabled"] = True
        if configuration.reasoning_effort:
            payload["reasoning_effort"] = configuration.reasoning_effort
        if configuration.json_mode:
            payload["response_format"] = {"type": "json_object"}
        max_retries = getattr(configuration, "max_retries", 5)
        min_delay = getattr(configuration, "retry_min_delay", 2.0)
        max_delay = getattr(configuration, "retry_max_delay", 30.0)
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

    async def stream(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
        stateless: bool = False,
    ) -> AsyncIterator[str]:
        """Yield content deltas from an OpenAI-compatible streaming response."""

        configuration = self.configuration
        if not configuration.model or not configuration.base_url:
            raise DomainError(
                "LLM_NOT_CONFIGURED", "Configure an OpenAI-compatible LLM", status=503
            )
        _ensure_local_policy(configuration.base_url, configuration.local_only)
        api_key = self.api_key
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key
            else {}
        )
        if session_id:
            headers["x-agent-session"] = session_id
        if stateless:
            headers["x-stateless"] = "true"
        if self.for_qa:
            headers["x-thinking-enabled"] = "true"
        payload: dict[str, Any] = {
            "model": configuration.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
        if session_id:
            payload["user"] = session_id
        if self.for_qa and (
            configuration.local_only
            or "deepseek" in configuration.base_url.lower()
            or "deepseek" in configuration.model.lower()
        ):
            payload["thinking_enabled"] = True
        if configuration.reasoning_effort:
            payload["reasoning_effort"] = configuration.reasoning_effort
        max_retries = getattr(configuration, "max_retries", 5)
        min_delay = getattr(configuration, "retry_min_delay", 2.0)
        max_delay = getattr(configuration, "retry_max_delay", 30.0)
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

    async def delete_session(self, session_id: str) -> bool:
        """Instruct the LLM proxy to delete the temporary session locally and upstream."""
        configuration = self.configuration
        if not configuration.base_url:
            return False
        base = configuration.base_url.rstrip("/")
        candidates: list[tuple[str, str]] = [
            ("DELETE", f"{base}/sessions/{session_id}"),
        ]
        if base.endswith("/v1"):
            origin = base[:-3].rstrip("/")
            candidates.append(("DELETE", f"{origin}/v1/sessions/{session_id}"))
            candidates.append(("POST", f"{origin}/reset-session?agent={session_id}&delete_remote=true"))
        else:
            candidates.append(("DELETE", f"{base}/v1/sessions/{session_id}"))
            candidates.append(("POST", f"{base}/reset-session?agent={session_id}&delete_remote=true"))

        api_key = self.api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        try:
            async with self._client() as http:
                for method, url in candidates:
                    try:
                        resp = await http.request(method, url, headers=headers, timeout=5.0)
                        if resp.status_code in (200, 204):
                            logger.info("Deleted LLM session %s via %s %s", session_id, method, url)
                            return True
                    except Exception as exc:
                        logger.debug("Failed deleting session %s via %s %s: %s", session_id, method, url, exc)
        except Exception as exc:
            logger.debug("Error attempting session deletion for %s: %s", session_id, exc)
        return False

    async def has_active_session(self, session_id: str) -> bool | None:
        """Check if the given session is currently active in the LLM proxy.

        Returns:
            True if session exists and is active.
            False if proxy responded and session is not found/not active.
            None if the provider does not support session probing (e.g. standard OpenAI).
        """
        configuration = self.configuration
        if not configuration.base_url:
            return None
        base = configuration.base_url.rstrip("/")
        if base.endswith("/v1"):
            origin = base[:-3].rstrip("/")
            single_candidates = [
                f"{origin}/v1/sessions/{session_id}",
                f"{origin}/sessions/{session_id}",
            ]
            list_candidates = [
                f"{origin}/v1/sessions",
                f"{origin}/sessions",
            ]
        else:
            single_candidates = [
                f"{base}/v1/sessions/{session_id}",
                f"{base}/sessions/{session_id}",
            ]
            list_candidates = [
                f"{base}/v1/sessions",
                f"{base}/sessions",
            ]

        api_key = self.api_key
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        try:
            async with self._client() as http:
                for url in single_candidates:
                    try:
                        resp = await http.get(url, headers=headers, timeout=3.0)
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, dict) and "active" in data:
                                return bool(data.get("active"))
                    except Exception:
                        continue

                for url in list_candidates:
                    try:
                        resp = await http.get(url, headers=headers, timeout=3.0)
                        if resp.status_code == 200:
                            data = resp.json()
                            agents = data.get("agents")
                            if isinstance(agents, list):
                                return any(
                                    a.get("agent") == session_id and bool(a.get("session_id"))
                                    for a in agents
                                    if isinstance(a, dict)
                                )
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _resolve_proxy(self) -> str | None:
        if hasattr(self.configuration, "resolved_proxy"):
            return self.configuration.resolved_proxy
        explicit = getattr(self.configuration, "proxy", None)
        if explicit is not None:
            explicit = explicit.strip()
            if not explicit or explicit.lower() in ("none", "false", "off", "direct"):
                return None
            return explicit
        if not self.configuration.local_only:
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
            proxy = self._resolve_proxy()
            trust_env = not self.configuration.local_only and not proxy
            async with httpx.AsyncClient(
                trust_env=trust_env, proxy=proxy, follow_redirects=False
            ) as client:
                yield client


from easylearn.persistence.translations import TranslationStore


class TranslationService:
    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        manager: TaskManager,
        settings: Settings,
        llm: LLMClient | None = None,
        store: TranslationStore | None = None,
    ) -> None:
        self.database = database
        self.store = store or TranslationStore(database)
        self.documents = documents
        self.manager = manager
        self.settings = settings
        self.llm = llm or LLMClient(settings)

    async def submit(self, document_id: UUID, request: TranslateRequest) -> TaskView:
        snapshot = await self.documents.effective_snapshot(document_id, request.parse_id)
        ir = snapshot.ir
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
            await self.documents.effective_snapshot(document_id, request.parse_id)

        task_view = await self.manager.submit(
            document_id,
            JobKind.TRANSLATE,
            {
                "parse_id": str(request.parse_id),
                "source_fingerprint": snapshot.source_fingerprint,
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
        snapshot = await self.documents.effective_snapshot(record.document_id, parse_id)
        ir = snapshot.ir
        units = translation_units(ir, selected)
        if not units:
            raise DomainError(
                "TRANSLATION_SCOPE_EMPTY", "The selected blocks contain no translatable text"
            )
        from easylearn.source_edits import compute_unit_fingerprint
        rows = await self.store.get_translations(parse_id)
        snapshots = {
            row["unit_id"]: (
                row["revision"],
                row["manual_text"],
                bool(row["use_manual"]),
                bool(row["locked"]),
            )
            for row in rows
        }
        existing_auto = {row["unit_id"]: row["auto_text"] for row in rows if row["auto_text"] is not None}
        fps = {row["unit_id"]: row["source_fingerprint"] for row in rows}

        def _is_stale(u: TranslationUnit) -> bool:
            if u.unit_id not in existing_auto:
                return True
            stored_fp = fps.get(u.unit_id)
            curr_fp = compute_unit_fingerprint(u.unit_id, u.source_text)
            if stored_fp is not None:
                return stored_fp != curr_fp
            return any(
                e.block_id == u.block_id and f"{e.block_id}:{e.node_id}" == u.unit_id
                for e in snapshot.edits
            )

        if force:
            units_to_translate = units
        else:
            units_to_translate = tuple(u for u in units if _is_stale(u))

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
        concurrency = max(1, self.settings.translation_concurrency)
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
        worker_queue: asyncio.Queue[int] = asyncio.Queue()
        for wid in range(concurrency):
            worker_queue.put_nowait(wid)
        worker_turn_counts = [0] * concurrency
        active_sessions: set[str] = set()
        batches_per_session = 5

        async def publish_batch(
            batch: tuple[TranslationUnit, ...], batch_result: dict[str, str]
        ) -> None:
            nonlocal conflict_count
            timestamp = _now()
            batch_items = [
                {
                    "block_id": unit.block_id,
                    "unit_id": unit.unit_id,
                    "value": batch_result[unit.unit_id],
                    "fingerprint": compute_unit_fingerprint(unit.unit_id, unit.source_text),
                }
                for unit in batch
            ]
            conflicts = await self.store.save_batch(
                parse_id=parse_id,
                batch=batch_items,
                snapshots=snapshots,
                timestamp=timestamp,
                history_limit=self.settings.files.revision_history_limit,
            )
            conflict_count += conflicts

        async def translate_worker(
            index: int, batch: tuple[TranslationUnit, ...]
        ) -> dict[str, str]:
            nonlocal completed_batches, completed_units
            async with semaphore:
                worker_id = await worker_queue.get()
                try:
                    await context.check()
                    turn = worker_turn_counts[worker_id]
                    worker_turn_counts[worker_id] += 1
                    segment = turn // batches_per_session
                    session_id = f"easylearn-translate-{record.document_id}-w{worker_id}-s{segment}"
                    active_sessions.add(session_id)

                    if turn > 0 and turn % batches_per_session == 0:
                        old_session_id = f"easylearn-translate-{record.document_id}-w{worker_id}-s{segment - 1}"
                        if hasattr(self.llm, "delete_session"):
                            asyncio.create_task(self.llm.delete_session(old_session_id))

                    t0 = time.monotonic()
                    batch_result = await self._translate_batch(
                        batch, session_id=session_id, stateless=True
                    )
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
                    active_prov = (getattr(self.settings.llm, "active_provider", None) or "").lower()
                    active_url = (getattr(self.settings.llm, "base_url", None) or "").lower()
                    if (
                        "deepseek" in active_prov or "deepseek" in active_url
                    ) and index + 1 < len(batches):
                        await asyncio.sleep(0.5)
                    return batch_result
                finally:
                    worker_queue.put_nowait(worker_id)

        tasks = [
            asyncio.create_task(translate_worker(index, batch))
            for index, batch in enumerate(batches)
        ]
        try:
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
        finally:
            if hasattr(self.llm, "delete_session"):
                for sid in list(active_sessions):
                    try:
                        await self.llm.delete_session(sid)
                    except Exception as exc:
                        logger.debug(
                            "Error cleaning up translation session %s: %s", sid, exc
                        )

    async def list(self, document_id: UUID, parse_id: UUID) -> tuple[TranslationUnitView, ...]:
        snapshot = await self.documents.effective_snapshot(document_id, parse_id)
        ir = snapshot.ir
        units = translation_units(ir)
        from easylearn.source_edits import compute_unit_fingerprint
        rows = await self.store.get_translations(parse_id)
        values = {row[1]: row for row in rows}
        result: list[TranslationUnitView] = []
        for unit in units:
            row = values.get(unit.unit_id)
            current_fp = compute_unit_fingerprint(unit.unit_id, unit.source_text)
            stale = False
            if row is not None:
                row_fp = row[7] if len(row) > 7 else None
                if row_fp is not None:
                    stale = (row_fp != current_fp)
                else:
                    stale = any(
                        e.block_id == unit.block_id and f"{e.block_id}:{e.node_id}" == unit.unit_id
                        for e in snapshot.edits
                    )
            effective_text = unit.source_text if stale else _effective(unit, row)
            result.append(
                TranslationUnitView(
                    parse_id=parse_id,
                    block_id=unit.block_id,
                    unit_id=unit.unit_id,
                    source_text=unit.source_text,
                    auto_text=row[2] if row else None,
                    manual_text=row[3] if row else None,
                    effective_text=effective_text,
                    use_manual=bool(row[4]) if row else False,
                    locked=bool(row[5]) if row else False,
                    revision=row[6] if row else 0,
                    source_fingerprint=row[7] if row and len(row) > 7 else None,
                    stale=stale,
                )
            )
        return tuple(result)

    async def effective_map_for_ir(
        self, ir: DocumentIR, edits: tuple[object, ...] | None = None
    ) -> dict[str, str]:
        units = translation_units(ir)
        from easylearn.source_edits import compute_unit_fingerprint
        rows = await self.store.get_translations(ir.parse_run_id)
        values = {row[1]: row for row in rows}
        result: dict[str, str] = {}
        for unit in units:
            if unit.unit_id in values:
                row = values[unit.unit_id]
                if row[2] is not None or row[3] is not None:
                    current_fp = compute_unit_fingerprint(unit.unit_id, unit.source_text)
                    row_fp = row[7] if len(row) > 7 else None
                    if row_fp is not None:
                        stale = (row_fp != current_fp)
                    elif edits is not None:
                        stale = any(
                            getattr(e, "block_id", None) == unit.block_id
                            and f"{getattr(e, 'block_id', '')}:{getattr(e, 'node_id', '')}" == unit.unit_id
                            for e in edits
                        )
                    else:
                        stale = False
                    if not stale:
                        result[unit.unit_id] = _effective(unit, row)
        return result

    async def effective_map(self, document_id: UUID, parse_id: UUID) -> dict[str, str]:
        snapshot = await self.documents.effective_snapshot(document_id, parse_id)
        return await self.effective_map_for_ir(snapshot.ir, snapshot.edits)

    async def edit(
        self, document_id: UUID, unit_id: str, request: EditTranslationRequest
    ) -> TranslationUnitView:
        snapshot = await self.documents.effective_snapshot(document_id, request.parse_id)
        unit = next((item for item in translation_units(snapshot.ir) if item.unit_id == unit_id), None)
        if unit is None or unit.block_id != request.block_id:
            raise DomainError(
                "TRANSLATION_UNIT_NOT_FOUND", "Translation unit not found", status=404
            )
        if request.text is not None and not request.text.strip():
            raise DomainError("TRANSLATION_TEXT_EMPTY", "Manual translation cannot be empty")
        unit_fp = compute_unit_fingerprint(unit.unit_id, unit.source_text)
        new_revision, timestamp, auto_text, manual_text, use_manual, locked = await self.store.edit(
            parse_id=request.parse_id,
            block_id=request.block_id,
            unit_id=unit_id,
            text=request.text,
            use_manual=request.use_manual,
            locked=request.locked,
            expected_revision=request.expected_revision,
            unit_fingerprint=unit_fp,
            default_source_text=unit.source_text,
            timestamp=_now(),
            history_limit=self.settings.files.revision_history_limit,
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

        snapshot = await self.documents.effective_snapshot(document_id, request.parse_id)
        unit = next((item for item in translation_units(snapshot.ir) if item.unit_id == unit_id), None)
        if unit is None or unit.block_id != request.block_id:
            raise DomainError(
                "TRANSLATION_UNIT_NOT_FOUND", "Translation unit not found", status=404
            )
        unit_fp = compute_unit_fingerprint(unit.unit_id, unit.source_text)
        revision, timestamp, auto_text, man_text, use_manual, locked = await self.store.restore(
            parse_id=request.parse_id,
            block_id=request.block_id,
            unit_id=unit_id,
            history_id=history_id,
            expected_revision=request.expected_revision,
            unit_fingerprint=unit_fp,
            default_source_text=unit.source_text,
            timestamp=_now(),
            history_limit=self.settings.files.revision_history_limit,
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
        rows = await self.store.get_history(
            parse_id=parse_id,
            unit_id=unit_id,
            limit=self.settings.files.revision_history_limit,
        )
        return tuple(
            TranslationHistoryView(
                id=UUID(str(row["id"])),
                parse_id=parse_id,
                block_id=row["block_id"],
                unit_id=row["unit_id"],
                text=row["text"],
                origin=row["origin"],
                revision=row["revision"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    async def _translate_batch(
        self,
        units: tuple[TranslationUnit, ...],
        session_id: str | None = None,
        stateless: bool = True,
    ) -> dict[str, str]:
        completed_results: dict[str, str] = {}
        pending_units: list[TranslationUnit] = list(units)
        max_attempts = 1 + max(0, getattr(self.settings.llm, "max_retries", 2))
        last_structure_error: str | None = None
        last_protocol_error: bool = False
        last_empty_error: bool = False

        for attempt in range(max_attempts):
            prompt_messages = _build_translation_prompt(pending_units)
            try:
                try:
                    content = await self.llm.complete_json(
                        prompt_messages, session_id=session_id, stateless=stateless
                    )
                except TypeError:
                    try:
                        content = await self.llm.complete_json(
                            prompt_messages, session_id=session_id
                        )
                    except TypeError:
                        content = await self.llm.complete_json(prompt_messages)
            except DomainError:
                raise
            except Exception as exc:
                logger.warning(
                    "LLM request exception on attempt %d/%d for %d units: %s",
                    attempt + 1,
                    max_attempts,
                    len(pending_units),
                    exc,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue

            try:
                parsed_values = _parse_translation_response(content, pending_units)
            except DomainError:
                raise
            except Exception as exc:
                last_protocol_error = True
                logger.warning(
                    "Error parsing translation response on attempt %d/%d: %s",
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue

            if not parsed_values:
                last_protocol_error = True
                logger.warning(
                    "LLM returned empty or invalid translation structure on attempt %d/%d: %s",
                    attempt + 1,
                    max_attempts,
                    content[:200],
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))
                continue

            for unit in list(pending_units):
                if unit.unit_id not in parsed_values:
                    continue
                text = parsed_values[unit.unit_id]
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
            logger.warning(
                "LLM batch translation incomplete (attempt %d/%d): %d/%d resolved, "
                "missing=%s",
                attempt + 1,
                max_attempts,
                len(completed_results),
                len(units),
                missing_ids,
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.1 * (attempt + 1))

        if last_structure_error is not None:
            raise DomainError("TRANSLATION_STRUCTURE_INVALID", last_structure_error)
        if last_empty_error and not completed_results:
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "LLM returned empty translation text"
            )
        if last_protocol_error and not completed_results:
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "LLM did not follow translation protocol"
            )
        missing_ids = [u.unit_id for u in pending_units]
        raise DomainError(
            "TRANSLATION_PROTOCOL_INVALID",
            f"LLM returned missing translation IDs: {missing_ids}",
        )


_ANCHOR_PATTERN = re.compile(r"\[§(\d+)\][:：]?\s*([\s\S]*?)(?=(?:\[§\d+\]|\Z))")
_ANCHOR_KEY_PATTERN = re.compile(r"^\[?§?(\d+)\]?$")

_TRANSLATION_SYSTEM_PROMPT = (
    "你是一位专业的高质量学术与技术文档翻译专家。请将输入的文档英文文本翻译为规范、地道、学术风格的简体中文。\n"
    "输入文本由段落编号锚点（如 [§1]、[§2] 等）分隔。\n"
    "翻译要求：\n"
    "1. 保持段落锚点标记（如 [§1]、[§2]）严格不变，且置于对应译文段落的最前面；\n"
    "2. 保持段落顺序与数量严格一致，严禁合并、删除或遗漏任何段落锚点；\n"
    "3. 严格保留文本中的专有名词、公式、代码、数字、URL、文件路径以及形如 {{PLACEHOLDER}} 的占位符格式；\n"
    "4. 仅输出翻译结果与段落锚点，不要输出多余的解释、前后缀或元说明。"
)


def _build_translation_prompt(units: Sequence[TranslationUnit]) -> list[dict[str, str]]:
    user_content = "\n\n".join(f"[§{i + 1}] {unit.source_text}" for i, unit in enumerate(units))
    return [
        {"role": "system", "content": _TRANSLATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_translation_response(
    content: str, units: Sequence[TranslationUnit]
) -> dict[str, str]:
    content = _strip_code_fence(content).strip()
    if not content:
        return {}

    # Level 2 (Dual parsing): JSON fallback if response is wrapped as a JSON object
    if content.startswith("{") and content.endswith("}"):
        try:
            raw_json = json.loads(content, object_pairs_hook=_unique_json_object)
            if isinstance(raw_json, dict):
                result: dict[str, str] = {}
                unit_by_id = {u.unit_id: u for u in units}
                for k, v in raw_json.items():
                    if not isinstance(k, str):
                        continue
                    k_clean = k.strip()
                    if k_clean in unit_by_id:
                        result[k_clean] = str(v).strip()
                    else:
                        m = _ANCHOR_KEY_PATTERN.match(k_clean)
                        if m:
                            idx = int(m.group(1))
                            if 1 <= idx <= len(units):
                                result[units[idx - 1].unit_id] = str(v).strip()
                return result
        except ValueError:
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "Duplicate JSON key in translation response"
            )
        except Exception:
            pass

    # Level 1 (Primary): Anchor stream regex extraction
    matches = _ANCHOR_PATTERN.findall(content)
    if matches:
        seen_indices: set[int] = set()
        result = {}
        for num_str, text in matches:
            idx = int(num_str)
            if idx in seen_indices:
                raise DomainError(
                    "TRANSLATION_PROTOCOL_INVALID", f"Duplicate translation anchor [§{idx}]"
                )
            seen_indices.add(idx)
            if 1 <= idx <= len(units):
                cleaned = text.strip()
                if cleaned:
                    result[units[idx - 1].unit_id] = cleaned
        return result

    # Level 3: Single unit fallback without anchor
    if len(units) == 1 and content:
        return {units[0].unit_id: content}

    return {}


def _batches(units: tuple[TranslationUnit, ...]) -> list[tuple[TranslationUnit, ...]]:
    batches: list[tuple[TranslationUnit, ...]] = []
    current: list[TranslationUnit] = []
    size = 0
    for unit in units:
        if current and (len(current) >= 40 or size + len(unit.source_text) > 8000):
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
