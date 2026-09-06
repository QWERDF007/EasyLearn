from __future__ import annotations

import json
import re
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


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    block_ids: tuple[str, ...] | None = None


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
        if configuration.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with self._client() as http:
                response = await http.post(
                    f"{configuration.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=configuration.timeout_seconds,
                )
        except httpx.TimeoutException:
            raise DomainError("LLM_TIMEOUT", "LLM request timed out", retryable=True) from None
        except httpx.TransportError:
            raise DomainError(
                "LLM_UNAVAILABLE", "LLM could not be reached", retryable=True
            ) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise DomainError("LLM_UNAVAILABLE", "LLM is temporarily unavailable", retryable=True)
        if response.status_code >= 400:
            raise DomainError("LLM_REQUEST_REJECTED", "LLM rejected the request")
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
        payload = {
            "model": configuration.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
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
                    raise DomainError(
                        "LLM_UNAVAILABLE", "LLM is temporarily unavailable", retryable=True
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
                        delta = payload_value["choices"][0]["delta"].get("content", "")
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise DomainError(
                            "LLM_PROTOCOL_INVALID", "LLM returned an invalid stream event"
                        ) from None
                    if isinstance(delta, str) and delta:
                        yield delta
        except DomainError:
            raise
        except httpx.TimeoutException:
            raise DomainError("LLM_TIMEOUT", "LLM request timed out", retryable=True) from None
        except httpx.TransportError:
            raise DomainError(
                "LLM_UNAVAILABLE", "LLM could not be reached", retryable=True
            ) from None

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.http is not None:
            yield self.http
        else:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
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

        return await self.manager.submit(
            document_id,
            JobKind.TRANSLATE,
            {
                "parse_id": str(request.parse_id),
                "block_ids": list(request.block_ids) if request.block_ids is not None else None,
            },
            admission=admit,
        )

    async def execute(self, record: TaskRecord, context: TaskContext) -> dict[str, JsonValue]:
        parse_id = UUID(str(record.scope["parse_id"]))
        raw_ids = record.scope.get("block_ids")
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
                    "SELECT block_id, unit_id, revision, manual_text, use_manual, locked "
                    "FROM translations WHERE parse_id = ?",
                    (str(parse_id),),
                )
            ).fetchall()
        snapshots = {row[1]: (row[2], row[3], bool(row[4]), bool(row[5])) for row in rows}
        result: dict[str, str] = {}
        conflict_count = 0
        batches = _batches(units)
        for index, batch in enumerate(batches):
            await context.check()
            await context.progress(
                index / len(batches), f"Translating {index}/{len(batches)} batches"
            )
            result.update(await self._translate_batch(batch))
        await context.check()
        timestamp = _now()
        result_ref: dict[str, JsonValue] = {
            "parse_id": str(parse_id),
            "translated_units": len(units),
            "conflicted_units": 0,
        }

        async def publish() -> None:
            nonlocal conflict_count
            async with self.database.transaction() as connection:
                for unit in units:
                    value = result[unit.unit_id]
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
                            "locked, "
                            "revision, updated_at) "
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
            result_ref["conflicted_units"] = conflict_count

        await context.publish(result_ref, publish)
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
        return next(item for item in values if item.unit_id == unit_id)

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
        return next(item for item in values if item.unit_id == unit_id)

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
        payload = {unit.unit_id: unit.source_text for unit in units}
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
            value = json.loads(content, object_pairs_hook=_unique_json_object)
        except (ValueError, TypeError):
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID", "LLM did not return a JSON object"
            ) from None
        if not isinstance(value, dict) or set(value) != set(payload):
            raise DomainError(
                "TRANSLATION_PROTOCOL_INVALID",
                "LLM returned missing or extra translation IDs",
            )
        result: dict[str, str] = {}
        for unit in units:
            text = value[unit.unit_id]
            if not isinstance(text, str) or not text.strip():
                raise DomainError(
                    "TRANSLATION_PROTOCOL_INVALID", "LLM returned empty translation text"
                )
            if _protected_tokens(unit.source_text) != _protected_tokens(text):
                raise DomainError(
                    "TRANSLATION_STRUCTURE_INVALID",
                    f"Protected tokens changed for {unit.unit_id}",
                )
            result[unit.unit_id] = text
        return result


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


def _protected_tokens(text: str) -> Counter[str]:
    return Counter(re.findall(r"\{\{[^{}]+\}\}|https?://\S+|(?<![A-Za-z])\d+(?:\.\d+)?%?", text))


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
