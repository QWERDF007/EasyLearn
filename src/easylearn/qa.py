"""Document-scoped question answering with frozen evidence."""

from __future__ import annotations

import json
import logging
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
from easylearn.persistence.qa import QAStore
from easylearn.rendering import block_text, render_markdown
from easylearn.tasks import TaskContext, TaskManager, TaskRecord
from easylearn.translation import LLMClient, TranslationService

logger = logging.getLogger(__name__)


class QARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    block_ids: tuple[str, ...] = ()
    related_block_ids: tuple[str, ...] = ()
    exclude_block_ids: tuple[str, ...] = ()
    auto_related: bool = True
    language: str = "auto"

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
        translation: TranslationService | None = None,
    ) -> None:
        self.database = database
        self.documents = documents
        self.manager = manager
        self.settings = settings
        self.llm = llm or LLMClient(settings)
        self.translation = translation
        self.store = QAStore(database)

    async def submit(self, document_id: UUID, request: QARequest) -> TaskView:
        if not self.settings.extensions.qa_enabled:
            raise DomainError("QA_DISABLED", "AI question answering is disabled", status=503)
        snapshot = await self.documents.effective_snapshot(document_id, request.parse_id)
        ir = snapshot.ir
        translations = (
            await self.translation.effective_map_for_ir(ir, snapshot.edits)
            if self.translation is not None
            else {}
        )
        resolved_language = request.language
        if resolved_language == "auto":
            resolved_language = "zh" if translations else "source"
        context = self.build_context(
            document_id,
            ir,
            request,
            translations=translations,
            language=resolved_language,
        )
        qa_id = uuid4()

        async def admit() -> None:
            await self.documents.effective_snapshot(document_id, request.parse_id)

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
                "language": resolved_language,
                "context": cast(JsonValue, context),
            },
            admission=admit,
        )

    def build_context(
        self,
        document_id: UUID,
        ir: DocumentIR,
        request: QARequest,
        translations: Mapping[str, str] | None = None,
        language: str = "source",
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
        if request.auto_related and not selected:
            # Whole-document mode: auto-expand using relation/section/keyword matches
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
        elif request.auto_related and selected:
            # When specific blocks are selected, only consider direct relations, never entire section/doc keyword search
            selected_set = set(required_ids)
            for relation in ir.relations:
                if relation.source.block_id in selected_set:
                    candidates.append(relation.target.block_id)
                if relation.target.block_id in selected_set:
                    candidates.append(relation.source.block_id)
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
        selected_context: list[tuple[str, Block, bool, str]] = []
        used = 0
        render_lang: RenderLanguage = "chinese" if language in ("zh", "chinese") else "source"
        for block_id in candidates:
            block = blocks[block_id]
            text = (
                block_text(block, translations, language=render_lang).strip()
                if translations
                else block.source_text.strip()
            )
            if not text:
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
            selected_context.append((block_id, block, required, text))
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
        for citation, (block_id, block, required, text) in enumerate(selected_context, start=1):
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
                    "text": text,
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
        parse_id = UUID(str(record.scope["parse_id"]))
        logger.info(
            "QA started: document_id=%s, parse_id=%s, question=%.50r, context_blocks=%d",
            record.document_id,
            parse_id,
            question,
            len(evidence),
        )
        doc_session_id = f"easylearn-qa-{record.document_id}"
        prior_qa_count = await self.store.count(record.document_id)

        is_session_active = None
        if hasattr(self.llm, "has_active_session"):
            try:
                is_session_active = await self.llm.has_active_session(doc_session_id)
            except Exception as exc:
                logger.debug("Error checking active session for %s: %s", doc_session_id, exc)

        # Context continuity is guaranteed ONLY when there are prior QA turns AND the session is known to be active
        is_continuous = (prior_qa_count > 0) and (is_session_active is True)
        is_first_turn = not is_continuous
        if is_first_turn:
            snapshot = await self.documents.effective_snapshot(record.document_id, parse_id)
            ir = snapshot.ir
            excluded_set = set(record.scope.get("exclude_block_ids") or ())
            if excluded_set:
                ir_for_md = ir.model_copy(
                    update={"blocks": tuple(b for b in ir.blocks if b.block_id not in excluded_set)}
                )
            else:
                ir_for_md = ir
            translations = (
                await self.translation.effective_map_for_ir(ir_for_md, snapshot.edits)
                if self.translation is not None
                else {}
            )
            lang = str(record.scope.get("language") or "auto")
            use_zh = lang in ("zh", "chinese") or (lang == "auto" and bool(translations))
            doc_markdown = render_markdown(
                ir_for_md,
                translations=translations if use_zh else None,
                language="chinese" if use_zh else "source",
            )
            if len(doc_markdown) > 60000:
                doc_markdown = (
                    doc_markdown[:30000]
                    + "\n\n...[中间内容过长已折叠]...\n\n"
                    + doc_markdown[-30000:]
                )
            parts = [
                "以下是正在阅读的完整文档内容（Markdown）：\n\n"
                f"```markdown\n{doc_markdown.strip()}\n```\n\n"
                "请结合整篇文档的全局脉络以及下方提供的重点参考段落回答问题。\n\n"
                f"【用户问题】\n{question}"
            ]
            if prompt.strip():
                parts.append(f"【重点参考段落】\n{prompt}")
            user_content = "\n\n".join(parts)
        else:
            parts = [f"【用户问题】\n{question}"]
            if prompt.strip():
                parts.append(f"【重点参考段落】\n{prompt}")
            user_content = "\n\n".join(parts)

        try:
            await context.progress(0.1, "Generating answer")
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一位严谨专业的学术与技术文档阅读解读助手。\n"
                        "请结合整篇文档的全局脉络以及下方提供的重点参考段落，深入、客观地回答用户问题：\n"
                        "1. 若回答内容直接引用或依据了选定的参考段落，请使用类似 [^1] 或 [cite:1] 的保留引用标记标明引用出处；\n"
                        "2. 若参考段落未详尽涵盖问题的全部细节，应充分结合本文档已投喂的全文 Markdown 上下文进行补充说明与全局综合解答；\n"
                        "3. 保持回答客观严谨，严禁捏造文档中不存在的事实。"
                    ),
                },
                {"role": "user", "content": user_content},
            ]
            emitted_any_delta = False
            max_stream_retries = 1
            for attempt in range(max_stream_retries + 1):
                try:
                    try:
                        stream_iter = self.llm.stream(messages, session_id=doc_session_id)
                    except TypeError:
                        stream_iter = self.llm.stream(messages)
                    async for chunk in stream_iter:
                        emitted_any_delta = True
                        await context.append_answer(chunk)
                    break
                except Exception as exc:
                    if emitted_any_delta or attempt >= max_stream_retries:
                        logger.warning(
                            "QA stream failed (emitted_delta=%s, attempt=%d): %s",
                            emitted_any_delta,
                            attempt,
                            exc,
                        )
                        raise
                    logger.info(
                        "QA stream failed before first delta on attempt %d, retrying...",
                        attempt + 1,
                    )
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
            timestamp = _now()
            citation_views = [evidence[index - 1] for index in citations]
            result_ref: dict[str, JsonValue] = {
                "qa_id": str(qa_id),
                "citations": cast(JsonValue, citations),
            }

            qa_record = QARecordView(
                qa_id=qa_id,
                document_id=record.document_id,
                parse_id=parse_id,
                question=question,
                answer=answer,
                context=tuple(evidence),
                citations=tuple(
                    item if isinstance(item, QACitation) else QACitation.model_validate(item)
                    for item in citation_views
                ),
                created_at=datetime.fromisoformat(timestamp),
            )

            async def publish() -> None:
                await self.store.save(qa_record)

            await context.publish(result_ref, publish)
            logger.info(
                "QA completed: document_id=%s, qa_id=%s, answer_length=%d, citations=%d",
                record.document_id,
                qa_id,
                len(answer),
                len(citations),
            )
            return result_ref
        except BaseException as exc:
            logger.error(
                "QA failed: document_id=%s, error=%s",
                record.document_id,
                exc,
            )
            raise

    async def list(self, document_id: UUID) -> tuple[QARecordView, ...]:
        await self.documents.get(document_id)
        records = await self.store.list(document_id)
        return tuple(records)

    async def delete(self, document_id: UUID, qa_id: UUID) -> None:
        await self.documents.get(document_id)
        await self.store.delete(document_id, qa_id)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _keywords(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff]{2,}", question.lower())))


def _keyword_hits(keyword: str, text: str) -> int:
    return text.lower().count(keyword)


def _score(block: Block, keywords: tuple[str, ...]) -> int:
    return sum(_keyword_hits(keyword, block.source_text) for keyword in keywords)


RESERVED_CITATION_PATTERN = re.compile(r"\[(?:\^|cite:\s*)(\d+)\]")


def _prompt(evidence: list[dict[str, JsonValue]]) -> str:
    return "\n\n".join(
        f"[^{item['citation']}] 段落 {item['block_id']}: {item['text']}" for item in evidence
    )


def _extract_citations(answer: str, maximum: int) -> list[int] | None:
    values = [int(val) for val in RESERVED_CITATION_PATTERN.findall(answer)]
    if any(value < 1 or value > maximum for value in values):
        return None
    return list(dict.fromkeys(values))


def _now() -> str:
    return datetime.now(UTC).isoformat()
