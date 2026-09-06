"""Single rendering rule shared by the reader and export jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

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

RenderLanguage = Literal["source", "chinese"]


def render_markdown(
    ir: DocumentIR,
    translations: Mapping[str, str] | None = None,
    *,
    bilingual: bool = False,
    language: RenderLanguage | None = None,
    asset_links: Mapping[str, str] | None = None,
) -> str:
    """Render a parse snapshot without inferring structure from Markdown."""

    translated = translations or {}
    links = asset_links or {}
    selected_language: RenderLanguage = (
        language if language is not None else "chinese" if translations is not None else "source"
    )
    chunks: list[str] = []
    for block in ir.blocks:
        if block.block_type == "table_cell":
            continue
        if block.block_type == "table" and block.table is not None:
            chunks.append(
                _render_table(ir, block, translated, bilingual, selected_language, links)
            )
            continue
        source = _render_block(block, {}, "source", links)
        if not source.strip():
            continue
        if bilingual:
            translated_text = _render_block(block, translated, "chinese", links)
            content = source
            if translated_text.strip() and translated_text != source:
                content += "\n\n> " + translated_text.replace("\n", "\n> ")
        else:
            content = _render_block(block, translated, selected_language, links)
        if block.block_type == "heading":
            content = f"## {content}"
        elif block.block_type == "list_item":
            content = f"- {content}"
        elif block.block_type == "code":
            content = f"```\n{content}\n```"
        chunks.append(content)
    return "\n\n".join(chunks).rstrip() + "\n"


def block_text(block: Block) -> str:
    return _render_block(block, {}, "source", {})


def _render_block(
    block: Block,
    translations: Mapping[str, str],
    language: RenderLanguage,
    asset_links: Mapping[str, str],
) -> str:
    parts: list[str] = []
    for node in block.source_nodes:
        unit_id = f"{block.block_id}:{node.node_id}"
        replacement = translations.get(unit_id) if language == "chinese" else None
        if isinstance(node, TextNode):
            parts.append(replacement if replacement is not None else node.text)
        elif isinstance(node, MathNode):
            parts.append(f"$${node.latex}$$")
        elif isinstance(node, CodeNode):
            parts.append(f"`{node.code}`")
        elif isinstance(node, LinkNode):
            label = replacement if replacement is not None else node.label
            parts.append(f"[{label}]({node.target})")
        elif isinstance(node, ReferenceNode):
            label = replacement if replacement is not None else node.label
            parts.append(f"[{label}](#{node.target.block_id})")
        elif isinstance(node, ImageNode):
            alt = replacement if replacement is not None else node.alt
            target = asset_links.get(str(node.asset_id), f"asset:{node.asset_id}")
            parts.append(f"![{alt}]({target})")
    return "".join(parts)


def _render_table(
    ir: DocumentIR,
    block: Block,
    translations: Mapping[str, str],
    bilingual: bool,
    language: RenderLanguage,
    asset_links: Mapping[str, str],
) -> str:
    assert block.table is not None
    children = {
        child.block_id: child for child in ir.blocks if child.parent_block_id == block.block_id
    }
    grid = [["" for _ in range(block.table.columns)] for _ in range(block.table.rows)]
    for cell in block.table.cells:
        child = children.get(cell.block_ref.block_id)
        if child is None:
            continue
        source = _render_block(child, {}, "source", asset_links)
        translated = _render_block(child, translations, "chinese", asset_links)
        if bilingual and translated != source and translated.strip():
            text = f"{source}<br>{translated}"
        else:
            text = _render_block(child, translations, language, asset_links)
        for row in range(cell.row, min(block.table.rows, cell.row + cell.row_span)):
            for column in range(
                cell.column, min(block.table.columns, cell.column + cell.column_span)
            ):
                grid[row][column] = _escape_table_cell(text)
    if not grid:
        return ""
    lines = [
        "| " + " | ".join(grid[0]) + " |",
        "| " + " | ".join("---" for _ in grid[0]) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n".join(lines)


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")
