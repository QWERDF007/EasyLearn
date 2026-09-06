"""Document-scoped question answering with frozen evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.document_ir.schema import Block, BlockRef, DocumentIR
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.tasks import TaskContext, TaskManager, TaskRecord
from easylearn.translation import LLMClient


class QARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    block_ids: tuple[str, ...] = ()
    related_block_ids: tuple[str, ...] = ()
    exclude_block_ids: tuple[str, ...] = ()
    auto_related: bool = True

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question cannot be blank")
        return normalized


class QACitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation: int = Field(ge=1)
    block_id: str
    block_ref: BlockRef
    text: str
    page_indices: tuple[int, ...] = ()
    localization_level: str


class QARecordView(BaseModel):
    model_config = ConfigDict(frozen=True)

    qa_id: UUID
    document_id: UUID
    parse_id: UUID
    question: str
    answer: str
    context: tuple[dict[str, JsonValue], ...]
    citations: tuple[QACitation, ...]
    created_at: datetime


class QAService:
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

    async def submit(self, document_id: UUID, request: QARequest) -> TaskView:
        if not self.settings.extensions.qa_enabled:
            raise DomainError("QA_DISABLED", "AI question answering is disabled", status=503)
        ir = await self.documents.load_ir(document_id, request.parse_id)
        context = self.build_context(document_id, ir, request)
        qa_id = uuid4()

        async def admit() -> None:
            await self.documents.load_ir(document_id, request.parse_id)

        return await self.manager.submit(
            document_id,
            JobKind.QA,
            {
                "qa_id": str(qa_id),
                "parse_id": str(request.parse_id),
                "question": request.question.strip(),
                "block_ids": list(request.block_ids),
                "related_block_ids": list(request.related_block_ids),
                "exclude_block_ids": list(request.exclude_block_ids),
                "auto_related": request.auto_related,
                "context": cast(JsonValue, context),
            },
            admission=admit,
        )

    def build_context(
        self, document_id: UUID, ir: DocumentIR, request: QARequest
    ) -> list[dict[str, JsonValue]]:
        blocks = {block.block_id: block for block in ir.blocks}
        order = {block.block_id: index for index, block in enumerate(ir.blocks)}
        selected = _unique(request.block_ids)
        manual = _unique(request.related_block_ids)
        excluded = set(request.exclude_block_ids)
        missing = [
            block_id for block_id in (*selected, *manual, *excluded) if block_id not in blocks
        ]
        if missing:
            raise DomainError(
                "BLOCK_NOT_FOUND", "A question context block does not exist", status=404
            )
        required_ids = list(dict.fromkeys((*selected, *manual)))
        if excluded.intersection(required_ids):
            raise DomainError(
                "QA_REQUIRED_BLOCK_EXCLUDED",
                "A selected or manually included block cannot be excluded",
                status=422,
            )
        candidates: list[str] = list(required_ids)
        if request.auto_related:
            selected_set = set(required_ids)
            for relation in ir.relations:
                if relation.source.block_id in selected_set:
                    candidates.append(relation.target.block_id)
                if relation.target.block_id in selected_set:
                    candidates.append(relation.source.block_id)
            for block in ir.blocks:
                if block.block_id in selected_set or block.block_id in excluded:
                    continue
                if any(block.section_path == blocks[item].section_path for item in selected_set):
                    candidates.append(block.block_id)
            keywords = _keywords(request.question)
            scored = sorted(
                (
                    (
                        sum(_keyword_hits(keyword, block.source_text) for keyword in keywords),
                        order[block_id],
                    ),
                    block_id,
                )
                for block_id, block in blocks.items()
                if block_id not in excluded
            )
            candidates.extend(
                block_id for (_, block_id) in reversed(scored) if _score(blocks[block_id], keywords)
            )
        if not candidates:
            candidates = [block.block_id for block in ir.blocks]
        candidates = list(
            dict.fromkeys(block_id for block_id in candidates if block_id not in excluded)
        )
        required_set = set(required_ids)
        required_order = [block_id for block_id in required_ids if block_id not in excluded]
        optional_order = [block_id for block_id in candidates if block_id not in required_set]
        candidates = required_order + optional_order

        budget = self.settings.extensions.qa_context_chars
        max_blocks = self.settings.extensions.qa_max_blocks
        selected_context: list[tuple[str, Block, bool]] = []
        used = 0
        for block_id in candidates:
            block = blocks[block_id]
            text = block.source_text.strip()
            if not text:
                continue
            required = block_id in required_set
            if required and used + len(text) > budget:
                raise DomainError(
                    "QA_CONTEXT_TOO_LARGE",
                    "Required question context exceeds the configured context budget",
                    status=422,
                )
            if len(selected_context) >= max_blocks or used + len(text) > budget:
                if required:
                    raise DomainError(
                        "QA_CONTEXT_TOO_LARGE",
                        "Required question context exceeds the configured block budget",
                        status=422,
                    )
                continue
            selected_context.append((block_id, block, required))
            used += len(text)
        if not selected_context:
            raise DomainError(
                "QA_CONTEXT_EMPTY", "No readable blocks are available for this question"
            )
        if any(block_id not in {item[0] for item in selected_context} for block_id in required_ids):
            raise DomainError(
                "QA_CONTEXT_EMPTY", "A required question context block contains no readable text"
            )
        result: list[dict[str, JsonValue]] = []
        for citation, (block_id, block, required) in enumerate(selected_context, start=1):
            pages = tuple(
                sorted({region.page_index for region in block.source_regions})
                or (
                    list(block.source_locator.page_indices)
                    if block.source_locator is not None
                    and hasattr(block.source_locator, "page_indices")
                    else []
                )
            )
            result.append(
                {
                    "citation": citation,
                    "block_id": block_id,
                    "block_ref": {
                        "document_id": str(document_id),
                        "parse_run_id": str(ir.parse_run_id),
                        "block_id": block_id,
                    },
                    "text": block.source_text,
                    "page_indices": list(pages),
                    "localization_level": block.localization_level,
                    "required": required,
                }
            )
        return result

    async def execute(self, record: TaskRecord, context: TaskContext) -> dict[str, JsonValue]:
        if not self.settings.extensions.qa_enabled:
            raise DomainError("QA_DISABLED", "AI question answering is disabled", status=503)
        evidence_value = record.scope.get("context")
        question = record.scope.get("question")
        if not isinstance(evidence_value, list) or not all(
            isinstance(item, dict) and all(isinstance(key, str) for key in item)
            for item in evidence_value
        ) or not isinstance(question, str):
            raise DomainError("QA_SCOPE_INVALID", "Question scope is invalid")
        evidence = cast(list[dict[str, JsonValue]], evidence_value)
        prompt = _prompt(evidence)
        await context.progress(0.1, "Generating answer")
        async for chunk in self.llm.stream(
            [
                {
                    "role": "system",
                    "content": (
                        "Answer only from the supplied evidence. Cite supporting evidence with the "
                        "exact bracket number such as [1]. If evidence is insufficient, "
                        "say so plainly; "
                        "never invent facts or citation numbers."
                    ),
                },
                {"role": "user", "content": f"Question: {question}\n\n{prompt}"},
            ]
        ):
            await context.append_answer(chunk)
        await context.check()
        answer = record.answer or ""
        if not answer.strip():
            raise DomainError("QA_EMPTY_ANSWER", "LLM returned an empty answer")
        citations = _extract_citations(answer, len(evidence))
        if citations is None:
            raise DomainError(
                "QA_CITATION_INVALID",
                "The answer contains a citation outside the frozen evidence range",
            )
        qa_id = UUID(str(record.scope["qa_id"]))
        parse_id = UUID(str(record.scope["parse_id"]))
        timestamp = _now()
        citation_views = [evidence[index - 1] for index in citations]
        result_ref: dict[str, JsonValue] = {
            "qa_id": str(qa_id),
            "citations": cast(JsonValue, citations),
        }

        async def publish() -> None:
            async with self.database.transaction() as connection:
                await connection.execute(
                    "INSERT INTO qa_records "
                    "(id, document_id, parse_id, question, answer, context_json, citations_json, "
                    "created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(qa_id),
                        str(record.document_id),
                        str(parse_id),
                        question,
                        answer,
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(citation_views, ensure_ascii=False, separators=(",", ":")),
                        timestamp,
                    ),
                )

        await context.publish(result_ref, publish)
        return result_ref

    async def list(self, document_id: UUID) -> tuple[QARecordView, ...]:
        await self.documents.get(document_id)
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT id, document_id, parse_id, question, answer, context_json, "
                    "citations_json, created_at FROM qa_records WHERE document_id = ? "
                    "ORDER BY created_at DESC, id DESC",
                    (str(document_id),),
                )
            ).fetchall()
        return tuple(
            QARecordView(
                qa_id=UUID(row[0]),
                document_id=UUID(row[1]),
                parse_id=UUID(row[2]),
                question=row[3],
                answer=row[4],
                context=tuple(json.loads(row[5])),
                citations=tuple(QACitation.model_validate(item) for item in json.loads(row[6])),
                created_at=datetime.fromisoformat(row[7]),
            )
            for row in rows
        )

    async def delete(self, document_id: UUID, qa_id: UUID) -> None:
        await self.documents.get(document_id)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM qa_records WHERE id = ? AND document_id = ?",
                (str(qa_id), str(document_id)),
            )
            if cursor.rowcount != 1:
                raise DomainError("QA_NOT_FOUND", "Question record not found", status=404)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _keywords(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff]{2,}", question.lower())))


def _keyword_hits(keyword: str, text: str) -> int:
    return text.lower().count(keyword)


def _score(block: Block, keywords: tuple[str, ...]) -> int:
    return sum(_keyword_hits(keyword, block.source_text) for keyword in keywords)


def _prompt(evidence: list[dict[str, JsonValue]]) -> str:
    return "\n\n".join(
        f"[{item['citation']}] block {item['block_id']}: {item['text']}" for item in evidence
    )


def _extract_citations(answer: str, maximum: int) -> list[int] | None:
    values = [int(value) for value in re.findall(r"\[(\d+)\]", answer)]
    if any(value < 1 or value > maximum for value in values):
        return None
    return list(dict.fromkeys(values))


def _now() -> str:
    return datetime.now(UTC).isoformat()
