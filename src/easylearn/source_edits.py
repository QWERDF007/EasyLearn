from __future__ import annotations

import logging
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


EditableNode = TextNode | MathNode | CodeNode | LinkNode | ReferenceNode | ImageNode


class SourceEditService:
    """Persist editable text overlays while keeping the parsed IR immutable."""

    def __init__(self, database: Database, documents: DocumentService) -> None:
        self.database = database
        self.documents = documents

    async def list(self, document_id: UUID, parse_id: UUID) -> tuple[SourceEditView, ...]:
        ir = await self.documents.load_ir(document_id, parse_id)
        blocks = _editable_nodes(ir)
        async with self.database.read() as connection:
            rows = await (
                await connection.execute(
                    "SELECT block_id, node_id, text, revision, updated_at "
                    "FROM source_edits WHERE parse_id = ? ORDER BY block_id, node_id",
                    (str(parse_id),),
                )
            ).fetchall()
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

    async def effective_ir(self, document_id: UUID, parse_id: UUID) -> DocumentIR:
        ir = await self.documents.load_ir(document_id, parse_id)
        edits = await self.list(document_id, parse_id)
        return apply_source_edits(ir, edits)

    async def edit(self, document_id: UUID, request: SourceEditRequest) -> SourceEditView:
        ir = await self.documents.load_ir(document_id, request.parse_id)
        node = _editable_nodes(ir).get((request.block_id, request.node_id))
        if node is None:
            raise DomainError("SOURCE_NODE_NOT_FOUND", "Editable source node not found", status=404)
        if not request.text.strip():
            raise DomainError("SOURCE_TEXT_EMPTY", "Edited source text cannot be empty")
        now = datetime.now(UTC).isoformat()
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT text, revision, updated_at FROM source_edits "
                    "WHERE parse_id = ? AND block_id = ? AND node_id = ?",
                    (str(request.parse_id), request.block_id, request.node_id),
                )
            ).fetchone()
            current_revision = int(row[1]) if row is not None else 0
            if current_revision != request.expected_revision:
                raise DomainError(
                    "SOURCE_EDIT_CONFLICT",
                    "Source text changed; reload the block before saving",
                    status=409,
                )
            revision = current_revision + 1
            await connection.execute(
                "INSERT INTO source_edits "
                "(parse_id, block_id, node_id, text, revision, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(parse_id, block_id, node_id) DO UPDATE SET "
                "text = excluded.text, revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (
                    str(request.parse_id),
                    request.block_id,
                    request.node_id,
                    request.text,
                    revision,
                    now,
                ),
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
