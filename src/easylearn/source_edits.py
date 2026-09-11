import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from easylearn.database import Database
from easylearn.document_ir.schema import (
    Block,
    CodeNode,
    DocumentIR,
    ImageNode,
    LinkNode,
    MathNode,
    ReferenceNode,
    TextNode,
)
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError

logger = logging.getLogger(__name__)


class SourceEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_id: UUID
    block_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    text: str = Field(max_length=1_000_000)


class SourceEditView(BaseModel):
    model_config = ConfigDict(frozen=True)

    parse_id: UUID
    block_id: str
    node_id: str
    original_text: str
    effective_text: str
    revision: int
    updated_at: datetime


@dataclass(frozen=True)
class EffectiveDocumentSnapshot:
    document_id: UUID
    parse_id: UUID
    ir: DocumentIR
    source_fingerprint: str
    revision: int
    edits: tuple[SourceEditView, ...]


def compute_source_fingerprint(parse_id: UUID, edits: tuple[SourceEditView, ...]) -> str:
    hasher = hashlib.sha256(f"parse:{parse_id}".encode("utf-8"))
    for edit in sorted(edits, key=lambda e: (e.block_id, e.node_id)):
        hasher.update(
            f":{edit.block_id}:{edit.node_id}:{edit.revision}:{edit.effective_text}".encode("utf-8")
        )
    return hasher.hexdigest()


def compute_unit_fingerprint(unit_id: str, source_text: str) -> str:
    return hashlib.sha256(f"{unit_id}:{source_text.strip()}".encode("utf-8")).hexdigest()[:16]


EditableNode = TextNode | MathNode | CodeNode | LinkNode | ReferenceNode | ImageNode


from easylearn.persistence.source_edits import SourceEditStore


class SourceEditService:
    """Persist editable text overlays while keeping the parsed IR immutable."""

    def __init__(
        self,
        database: Database,
        documents: DocumentService,
        store: SourceEditStore | None = None,
    ) -> None:
        self.database = database
        self.store = store or SourceEditStore(database)
        self.documents = documents

    async def list(self, document_id: UUID, parse_id: UUID) -> tuple[SourceEditView, ...]:
        ir = await self.documents.load_ir(document_id, parse_id)
        blocks = _editable_nodes(ir)
        rows = await self.store.list_edits(parse_id)
        result: list[SourceEditView] = []
        for row in rows:
            node = blocks.get((row[0], row[1]))
            if node is None:
                continue
            result.append(
                _view(
                    parse_id,
                    row[0],
                    row[1],
                    node,
                    row[2],
                    row[3],
                    row[4],
                )
            )
        return tuple(result)

    async def effective_snapshot(
        self, document_id: UUID, parse_id: UUID
    ) -> EffectiveDocumentSnapshot:
        ir = await self.documents.load_ir(document_id, parse_id)
        edits = await self.list(document_id, parse_id)
        effective_ir = apply_source_edits(ir, edits)
        fingerprint = compute_source_fingerprint(parse_id, edits)
        revision = sum(edit.revision for edit in edits)
        return EffectiveDocumentSnapshot(
            document_id=document_id,
            parse_id=parse_id,
            ir=effective_ir,
            source_fingerprint=fingerprint,
            revision=revision,
            edits=edits,
        )

    async def effective_ir(self, document_id: UUID, parse_id: UUID) -> DocumentIR:
        snapshot = await self.effective_snapshot(document_id, parse_id)
        return snapshot.ir

    async def edit(self, document_id: UUID, request: SourceEditRequest) -> SourceEditView:
        ir = await self.documents.load_ir(document_id, request.parse_id)
        node = _editable_nodes(ir).get((request.block_id, request.node_id))
        if node is None:
            raise DomainError("SOURCE_NODE_NOT_FOUND", "Editable source node not found", status=404)
        if not request.text.strip():
            raise DomainError("SOURCE_TEXT_EMPTY", "Edited source text cannot be empty")
        now = datetime.now(UTC).isoformat()
        revision, _ = await self.store.save_edit(
            parse_id=request.parse_id,
            block_id=request.block_id,
            node_id=request.node_id,
            text=request.text,
            expected_revision=request.expected_revision,
            updated_at=now,
        )
        logger.info(
            "Source edit saved: document_id=%s, parse_id=%s, block_id=%s, node_id=%s, revision=%d",
            document_id,
            request.parse_id,
            request.block_id,
            request.node_id,
            revision,
        )
        return _view(
            request.parse_id,
            request.block_id,
            request.node_id,
            node,
            request.text,
            revision,
            now,
        )


def apply_source_edits(ir: DocumentIR, edits: tuple[SourceEditView, ...]) -> DocumentIR:
    values = {(edit.block_id, edit.node_id): edit.effective_text for edit in edits}
    if not values:
        return ir
    blocks: list[Block] = []
    for block in ir.blocks:
        nodes = tuple(
            _replace_node(node, values[(block.block_id, node.node_id)])
            if (block.block_id, node.node_id) in values
            else node
            for node in block.source_nodes
        )
        blocks.append(
            block.model_copy(update={"source_nodes": nodes})
            if nodes != block.source_nodes
            else block
        )
    return ir.model_copy(update={"blocks": tuple(blocks)})


def _editable_nodes(ir: DocumentIR) -> dict[tuple[str, str], EditableNode]:
    return {
        (block.block_id, node.node_id): node
        for block in ir.blocks
        for node in block.source_nodes
        if isinstance(node, (TextNode, MathNode, CodeNode, LinkNode, ReferenceNode, ImageNode))
    }


def _node_text(node: EditableNode) -> str:
    if isinstance(node, TextNode):
        return node.text
    if isinstance(node, MathNode):
        return node.latex
    if isinstance(node, CodeNode):
        return node.code
    if isinstance(node, (LinkNode, ReferenceNode)):
        return node.label
    return node.alt


def _replace_node(node: EditableNode, text: str) -> EditableNode:
    if isinstance(node, TextNode):
        return node.model_copy(update={"text": text})
    if isinstance(node, MathNode):
        return node.model_copy(update={"latex": text})
    if isinstance(node, CodeNode):
        return node.model_copy(update={"code": text})
    if isinstance(node, (LinkNode, ReferenceNode)):
        return node.model_copy(update={"label": text})
    return node.model_copy(update={"alt": text})


def _view(
    parse_id: UUID,
    block_id: str,
    node_id: str,
    node: EditableNode,
    text: str,
    revision: int,
    updated_at: str,
) -> SourceEditView:
    return SourceEditView(
        parse_id=parse_id,
        block_id=block_id,
        node_id=node_id,
        original_text=_node_text(node),
        effective_text=text,
        revision=revision,
        updated_at=datetime.fromisoformat(updated_at),
    )
