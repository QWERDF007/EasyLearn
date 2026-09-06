import re

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

from easylearn.document_ir.schema import (
    Block,
    BlockRef,
    CodeNode,
    ImageNode,
    InlineNode,
    LinkNode,
    MathNode,
    PageLocator,
    TableCell,
    TableStructure,
    TextNode,
)
from easylearn.errors import DomainError
from easylearn.mineru.assets import ImageReferences
from easylearn.mineru.schema import MinerUTableLimits


def cell_nodes(cell: Tag, images: ImageReferences) -> tuple[InlineNode, ...]:
    nodes: list[InlineNode] = []

    def visit(element: Tag | NavigableString) -> None:
        if isinstance(element, Comment):
            return
        if (
            isinstance(element, Tag)
            and element.name in ("a", "code")
            and any(
                child.name not in ("b", "strong", "i", "em", "u", "s", "sub", "sup", "span")
                for child in element.find_all()
            )
        ):
            raise DomainError(
                "MINERU_TABLE_INVALID", "Unsupported nesting inside atomic inline content"
            )
        if isinstance(element, NavigableString):
            text = str(element)
            offset = 0
            for match in re.finditer(r"\\\((.*?)\\\)|\\\[(.*?)\\\]", text, re.DOTALL):
                if match.start() > offset:
                    nodes.append(
                        TextNode(node_id=f"n{len(nodes)}", text=text[offset : match.start()])
                    )
                nodes.append(
                    MathNode(
                        node_id=f"n{len(nodes)}",
                        latex=next(group for group in match.groups() if group is not None),
                    )
                )
                offset = match.end()
            if offset < len(text):
                nodes.append(TextNode(node_id=f"n{len(nodes)}", text=text[offset:]))
        elif element.name == "br":
            nodes.append(TextNode(node_id=f"n{len(nodes)}", text="\n"))
        elif element.name == "code":
            nodes.append(CodeNode(node_id=f"n{len(nodes)}", code=element.get_text()))
        elif element.name == "eq":
            if element.find(True) is not None:
                raise DomainError(
                    "MINERU_TABLE_INVALID", "Unsupported nesting inside table formula content"
                )
            nodes.append(MathNode(node_id=f"n{len(nodes)}", latex=element.get_text()))
        elif element.name == "a":
            target = element.get("href")
            if not isinstance(target, str):
                raise DomainError("MINERU_TABLE_INVALID", "Table link target is missing")
            nodes.append(
                LinkNode(node_id=f"n{len(nodes)}", target=target, label=element.get_text())
            )
        elif element.name == "img":
            path = element.get("src")
            if not isinstance(path, str):
                raise DomainError("MINERU_ASSET_INVALID", "Table image source is missing")
            asset = images.resolve(path)
            nodes.append(
                ImageNode(
                    node_id=f"n{len(nodes)}",
                    asset_id=asset.asset_id,
                    alt=str(element.get("alt", "")),
                )
            )
        elif element.name in (
            "td",
            "th",
            "b",
            "strong",
            "i",
            "em",
            "u",
            "s",
            "sub",
            "sup",
            "span",
            "p",
            "div",
        ):
            paragraph = element.name in ("p", "div")
            if (
                paragraph
                and nodes
                and not (isinstance(nodes[-1], TextNode) and nodes[-1].text.endswith("\n"))
            ):
                nodes.append(TextNode(node_id=f"n{len(nodes)}", text="\n"))
            for child in element.children:
                if isinstance(child, (Tag, NavigableString)):
                    visit(child)
            if (
                paragraph
                and element.next_sibling is not None
                and nodes
                and not (isinstance(nodes[-1], TextNode) and nodes[-1].text.endswith("\n"))
            ):
                nodes.append(TextNode(node_id=f"n{len(nodes)}", text="\n"))
        else:
            raise DomainError("MINERU_TABLE_INVALID", "Unsupported table cell markup")

    visit(cell)
    return tuple(nodes)


def normalize_table(
    markup: str,
    *,
    parent: Block,
    identity: BlockRef,
    images: ImageReferences,
    limits: MinerUTableLimits,
) -> tuple[Block, ...]:
    """Decode table structure into versioned cells; table geometry is not cell evidence."""
    if len(markup) > limits.max_markup_chars:
        raise DomainError("MINERU_TABLE_LIMIT", "Table markup exceeds the configured limit")
    soup = BeautifulSoup(markup, "html.parser")
    tables = soup.find_all("table")
    if len(tables) != 1:
        raise DomainError("MINERU_TABLE_INVALID", "Expected exactly one non-nested HTML table")
    children_by_tag = {
        "table": ("thead", "tbody", "tfoot", "tr"),
        "thead": ("tr",),
        "tbody": ("tr",),
        "tfoot": ("tr",),
        "tr": ("td", "th"),
    }
    for container in [tables[0], *tables[0].find_all(["thead", "tbody", "tfoot", "tr"])]:
        for child in container.children:
            if isinstance(child, Comment):
                continue
            if (isinstance(child, Tag) and child.name not in children_by_tag[container.name]) or (
                isinstance(child, NavigableString) and str(child).strip()
            ):
                raise DomainError("MINERU_TABLE_INVALID", "Invalid table structural nesting")
    rows = tables[0].find_all("tr")
    if len(rows) > limits.max_rows:
        raise DomainError("MINERU_TABLE_LIMIT", "Table row count exceeds the configured limit")
    cells: list[TableCell] = []
    blocks: list[Block] = []
    active_spans: list[tuple[int, int, int]] = []
    try:
        for row_index, row in enumerate(rows):
            column = 0
            active_spans = [entry for entry in active_spans if entry[0] > row_index]
            occupied = sorted((left, right) for _, left, right in active_spans)
            for tag in row.find_all(["th", "td"], recursive=False):
                if not isinstance(tag, Tag):
                    continue
                for left, right in occupied:
                    if left <= column < right:
                        column = right
                    elif column < left:
                        break
                cell_id = f"{parent.block_id}.r{row_index}.c{column}"
                cell = TableCell(
                    block_ref=BlockRef(
                        document_id=identity.document_id,
                        parse_run_id=identity.parse_run_id,
                        block_id=cell_id,
                    ),
                    row=row_index,
                    column=column,
                    row_span=int(str(tag.get("rowspan", "1"))),
                    column_span=int(str(tag.get("colspan", "1"))),
                    role="header" if tag.name == "th" else "data",
                )
                if (
                    cell.column + cell.column_span > limits.max_columns
                    or len(cells) >= limits.max_cells
                ):
                    raise DomainError(
                        "MINERU_TABLE_LIMIT", "Table dimensions exceed the configured limits"
                    )
                cells.append(cell)
                if cell.row_span > 1:
                    active_spans.append(
                        (cell.row + cell.row_span, column, column + cell.column_span)
                    )
                column += cell.column_span
                page_indices = tuple(
                    sorted({region.page_index for region in parent.source_regions})
                )
                blocks.append(
                    Block(
                        block_id=cell_id,
                        block_type="table_cell",
                        parent_block_id=parent.block_id,
                        order_index=parent.order_index + 1 + len(blocks),
                        section_path=parent.section_path,
                        source_nodes=cell_nodes(tag, images),
                        source_locator=(
                            PageLocator(page_indices=page_indices)
                            if page_indices
                            else parent.source_locator
                        ),
                        parse_warnings=(*parent.parse_warnings, "TABLE_CELL_REGION_UNAVAILABLE"),
                    )
                )
        structure = TableStructure(
            rows=len(rows),
            columns=max((cell.column + cell.column_span for cell in cells), default=0),
            cells=tuple(cells),
        )
    except ValueError:
        raise DomainError("MINERU_TABLE_INVALID", "Invalid table grid or cell metadata") from None
    warnings = parent.parse_warnings + (() if structure.complete else ("TABLE_INCOMPLETE",))
    return (parent.model_copy(update={"table": structure, "parse_warnings": warnings}), *blocks)
