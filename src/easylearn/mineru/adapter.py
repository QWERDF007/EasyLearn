from collections.abc import Iterator
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from easylearn.document_ir.coordinates import CoordinateMapper
from easylearn.document_ir.schema import (
    AssetDescriptor,
    BBox,
    Block,
    BlockRef,
    BlockRelation,
    BlockType,
    CodeNode,
    DocumentIR,
    ImageNode,
    InlineNode,
    MathNode,
    PageLocator,
    SourceCoordinates,
    SourceRegion,
    TextNode,
)
from easylearn.errors import DomainError
from easylearn.mineru.assets import ImageReferences
from easylearn.mineru.schema import MinerUOptions, MinerUTableLimits, MinerUVersion
from easylearn.mineru.tables import normalize_table
from easylearn.paths import PortablePath
from easylearn.previews.schema import PreflightReport

_BLOCK_TYPES: dict[str, BlockType] = {
    "title": "heading",
    "text": "paragraph",
    "interline_equation": "formula",
    "image": "image",
    "image_caption": "caption",
    "image_footnote": "footnote",
    "chart": "image",
    "chart_caption": "caption",
    "chart_footnote": "footnote",
    "table": "table",
    "table_caption": "caption",
    "table_footnote": "footnote",
    "code": "code",
    "code_caption": "caption",
    "code_footnote": "footnote",
    "list": "list",
    "index": "list",
    "ref_text": "reference",
    "header": "paragraph",
    "page_footnote": "footnote",
    "doc_title": "heading",
    "paragraph_title": "heading",
    "abstract": "paragraph",
    "vertical_text": "paragraph",
    "phonetic": "paragraph",
    "footer": "paragraph",
    "page_number": "paragraph",
    "aside_text": "paragraph",
    "discarded": "paragraph",
    "caption": "caption",
    "footnote": "footnote",
    "formula_number": "paragraph",
}


class NormalizationContext(BaseModel):
    """Frozen parse input and coordinate registration supplied by the parse execution module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: UUID
    parse_run_id: UUID
    preview_asset_id: UUID
    preview: PreflightReport
    options: MinerUOptions
    coordinates: tuple[SourceCoordinates | None, ...]
    assets: dict[PortablePath, AssetDescriptor] = Field(default_factory=dict)
    table_limits: MinerUTableLimits = Field(default_factory=MinerUTableLimits)


class _Upstream(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False, strict=True)


class _Span(_Upstream):
    cross_page: bool = False
    image_path: PortablePath | Literal[""] = ""


class _TextSpan(_Span):
    type: Literal["text", "inline_equation", "interline_equation"]
    content: str


class _ImageSpan(_Span):
    type: Literal["image", "chart"]
    content: str = ""


class _TableSpan(_Span):
    type: Literal["table"]
    html: str | None = None


class _Line(_Upstream):
    spans: tuple[Annotated[_TextSpan | _ImageSpan | _TableSpan, Field(discriminator="type")], ...]
    bbox: BBox | None = None
    is_list_start_line: bool = False

    @property
    def source_key(self) -> str:
        return self.model_dump_json(
            exclude={
                "spans": {"__all__": {"cross_page"}},
                "is_list_start_line": True,
            }
        )


class _Block(_Upstream):
    type: Literal[
        "title",
        "text",
        "interline_equation",
        "image",
        "image_body",
        "image_caption",
        "image_footnote",
        "chart",
        "chart_body",
        "chart_caption",
        "chart_footnote",
        "table",
        "table_body",
        "table_caption",
        "table_footnote",
        "code",
        "code_body",
        "code_caption",
        "code_footnote",
        "list",
        "index",
        "ref_text",
        "header",
        "page_footnote",
        "doc_title",
        "paragraph_title",
        "abstract",
        "vertical_text",
        "phonetic",
        "footer",
        "page_number",
        "aside_text",
        "discarded",
        "caption",
        "footnote",
        "formula_number",
    ]
    bbox: BBox | None = None
    lines: tuple[_Line, ...] = ()
    blocks: tuple["_Block", ...] = ()
    level: int = Field(default=1, ge=0)
    lines_deleted: bool = False
    cross_page: bool = False
    guess_lang: str | None = None

    @model_validator(mode="after")
    def validate_content_shape(self) -> Self:
        if self.type not in ("image", "chart", "table", "code", "list", "index") and (
            "lines" not in self.model_fields_set or self.blocks
        ):
            raise ValueError("Leaf block must contain lines and cannot contain nested blocks")
        return self

    def flatten(
        self, block_id: str, parent_id: str | None = None
    ) -> Iterator[tuple[str, str | None, "_Block"]]:
        if self.type in ("list", "index"):
            if (
                (self.lines and self.blocks)
                or not {"blocks", "lines"}.intersection(self.model_fields_set)
                or any(child.type not in ("text", "ref_text") for child in self.blocks)
            ):
                raise DomainError("MINERU_PROTOCOL_MISMATCH", "Invalid list structure")
            children = self.blocks
            if self.lines:
                items: list[list[_Line]] = []
                for line in self.lines:
                    if not items or line.is_list_start_line:
                        items.append([])
                    items[-1].append(line)
                children = tuple(_Block(type="text", lines=tuple(lines)) for lines in items)
            yield (
                block_id,
                parent_id,
                self.model_copy(
                    update={
                        "lines": tuple(line for child in children for line in child.lines),
                        "blocks": (),
                    }
                ),
            )
            for index, child in enumerate(children):
                yield from child.flatten(f"{block_id}.c{index}", block_id)
        elif self.type in ("image", "chart", "table", "code"):
            body_type = f"{self.type}_body"
            bodies = [child for child in self.blocks if child.type == body_type]
            if (
                self.lines
                or len(bodies) != 1
                or any(
                    child.type not in (body_type, f"{self.type}_caption", f"{self.type}_footnote")
                    for child in self.blocks
                )
            ):
                raise DomainError("MINERU_PROTOCOL_MISMATCH", "Invalid visual group structure")
            body = bodies[0]
            for index, child in enumerate(self.blocks):
                if child.type == body_type:
                    yield (
                        block_id,
                        parent_id,
                        self.model_copy(
                            update={
                                "lines": body.lines,
                                "bbox": body.bbox,
                                "blocks": (),
                                "lines_deleted": body.lines_deleted,
                                "cross_page": self.cross_page or body.cross_page,
                            }
                        ),
                    )
                else:
                    yield from child.flatten(f"{block_id}.c{index}", block_id)
        else:
            if self.blocks or self.type in ("image_body", "chart_body", "table_body", "code_body"):
                raise DomainError("MINERU_PROTOCOL_MISMATCH", "Unexpected nested body")
            yield block_id, parent_id, self


class _Page(_Upstream):
    page_idx: int = Field(ge=0)
    page_size: tuple[float, float]
    para_blocks: tuple[_Block, ...]
    preproc_blocks: tuple[_Block, ...] = ()
    discarded_blocks: tuple[_Block, ...] = ()


class _Middle(_Upstream):
    version: MinerUVersion = Field(alias="_version_name")
    backend: Literal["pipeline", "vlm", "hybrid"] = Field(alias="_backend")
    pdf_info: tuple[_Page, ...]


class MinerUAdapter:
    def normalize(self, raw: bytes, *, context: NormalizationContext) -> DocumentIR:
        try:
            middle = _Middle.model_validate_json(raw)
        except ValidationError:
            raise DomainError(
                "MINERU_PROTOCOL_MISMATCH", "Invalid MinerU middle protocol"
            ) from None
        page_count = len(context.preview.pages)
        if (
            middle.backend != context.options.backend.split("-", 1)[0]
            or context.options.page_count != page_count
            or len(middle.pdf_info) != page_count
            or tuple(page.page_idx for page in middle.pdf_info) != tuple(range(page_count))
        ):
            raise DomainError(
                "MINERU_PROTOCOL_MISMATCH", "MinerU result does not match parse input"
            )
        if len(context.coordinates) != page_count:
            raise DomainError(
                "ADAPTER_COORDINATE_INVALID", "Coordinate registrations do not match pages"
            )
        line_sources: dict[str, set[int]] = {}
        for page in middle.pdf_info:
            for index, original in enumerate((*page.preproc_blocks, *page.discarded_blocks)):
                for _, _, content in original.flatten(f"p{page.page_idx}.b{index}"):
                    for line in content.lines:
                        if (
                            line.bbox is not None
                            and not content.cross_page
                            and not any(span.cross_page for span in line.spans)
                        ):
                            line_sources.setdefault(line.source_key, set()).add(page.page_idx)
        blocks: list[Block] = []
        images = ImageReferences(context.assets)
        relations: list[BlockRelation] = []
        headings: list[tuple[int, str]] = []
        for page in middle.pdf_info:
            source = context.coordinates[page.page_idx]
            if min(page.page_size) <= 0 or (
                source is not None
                and (
                    source.space != "page_units"
                    or source.origin != "top_left"
                    or source.size != page.page_size
                )
            ):
                raise DomainError(
                    "ADAPTER_COORDINATE_INVALID", "Invalid middle coordinate registration"
                )
            content_blocks = (
                (content, namespace == "d")
                for namespace, group in (("b", page.para_blocks), ("d", page.discarded_blocks))
                for index, upstream in enumerate(group)
                for content in upstream.flatten(f"p{page.page_idx}.{namespace}{index}")
            )
            for (block_id, parent_id, upstream), discarded in content_blocks:
                if upstream.lines_deleted:
                    if upstream.lines:
                        raise DomainError(
                            "MINERU_PROTOCOL_MISMATCH", "Deleted block still contains lines"
                        )
                    continue
                nodes: list[InlineNode] = []
                table_html: str | None = None
                table_span_seen = False
                for line_index, line in enumerate(
                    () if upstream.type in ("list", "index") else upstream.lines
                ):
                    if line_index:
                        nodes.append(TextNode(node_id=f"l{line_index}.break", text="\n"))
                    for span_index, span in enumerate(line.spans):
                        node_id = f"l{line_index}.s{span_index}"
                        asset = None
                        if span.image_path:
                            asset = images.resolve(span.image_path)
                        if isinstance(span, _TableSpan):
                            if upstream.type != "table" or table_span_seen:
                                raise DomainError(
                                    "MINERU_TABLE_INVALID", "Expected one table span per table"
                                )
                            table_html = span.html
                            table_span_seen = True
                            if asset is not None:
                                nodes.append(ImageNode(node_id=node_id, asset_id=asset.asset_id))
                            continue
                        if isinstance(span, _ImageSpan) and asset is None:
                            raise DomainError("MINERU_ASSET_INVALID", "Image asset is missing")
                        nodes.append(
                            ImageNode(node_id=node_id, asset_id=asset.asset_id, alt=span.content)
                            if isinstance(span, _ImageSpan) and asset is not None
                            else CodeNode(
                                node_id=node_id, code=span.content, language=upstream.guess_lang
                            )
                            if upstream.type == "code" and span.type == "text"
                            else TextNode(node_id=node_id, text=span.content)
                            if span.type == "text"
                            else MathNode(
                                node_id=node_id,
                                latex=span.content,
                                screenshot_asset_id=asset.asset_id if asset is not None else None,
                            )
                        )
                block_type = _BLOCK_TYPES[upstream.type]
                if parent_id is not None and block_type == "paragraph":
                    block_type = "list_item"
                if block_type == "heading":
                    while headings and headings[-1][0] >= upstream.level:
                        headings.pop()
                    headings.append(
                        (
                            upstream.level,
                            "".join(
                                node.text if isinstance(node, TextNode) else node.latex
                                for node in nodes
                                if isinstance(node, (TextNode, MathNode))
                            ),
                        )
                    )
                locations: list[tuple[int, BBox | None]] = []
                source_warnings: list[str] = []
                if block_type == "table" and any(
                    page.page_idx not in line_sources.get(line.source_key, set())
                    for line in upstream.lines
                ):
                    source_warnings.append("TABLE_SOURCE_UNRESOLVED")
                for line in upstream.lines:
                    source_page = page.page_idx
                    if upstream.cross_page or any(span.cross_page for span in line.spans):
                        candidates = line_sources.get(line.source_key, set()) - {page.page_idx}
                        if len(candidates) != 1:
                            source_warnings.append("CROSS_PAGE_SOURCE_UNRESOLVED")
                            continue
                        source_page = next(iter(candidates))
                    locations.append((source_page, line.bbox))
                unresolved = bool(source_warnings)
                if not any(bbox is not None for _, bbox in locations) and not unresolved:
                    locations = [(page.page_idx, upstream.bbox)]
                regions: list[SourceRegion] = []
                if not unresolved:
                    for source_page, bbox in locations:
                        registration = context.coordinates[source_page]
                        if bbox is None or registration is None:
                            regions.clear()
                            break
                        regions.append(
                            CoordinateMapper(
                                context.preview.pages[source_page], registration
                            ).map_bbox(bbox)
                        )
                blocks.append(
                    Block(
                        block_id=block_id,
                        block_type=block_type,
                        order_index=len(blocks),
                        parent_block_id=parent_id,
                        section_path=tuple(text for _, text in headings),
                        source_nodes=tuple(nodes),
                        source_regions=tuple(regions),
                        source_locator=(
                            PageLocator(
                                page_indices=tuple(sorted({index for index, _ in locations}))
                            )
                            if not regions and not unresolved
                            else None
                        ),
                        parse_warnings=(
                            (f"UPSTREAM_DISCARDED:{upstream.type}",) if discarded else ()
                        )
                        + (
                            tuple(dict.fromkeys(source_warnings))
                            if unresolved
                            else ("COORDINATE_EVIDENCE_MISSING",)
                            if not regions
                            else ()
                        ),
                    )
                )
                if block_type == "table":
                    parent = blocks.pop()
                    if not table_html:
                        if not table_span_seen or not any(
                            isinstance(node, ImageNode) for node in nodes
                        ):
                            raise DomainError(
                                "MINERU_TABLE_INVALID", "Table markup and screenshot are missing"
                            )
                        blocks.append(
                            parent.model_copy(
                                update={
                                    "parse_warnings": (
                                        *parent.parse_warnings,
                                        "TABLE_STRUCTURE_UNAVAILABLE",
                                    ),
                                }
                            )
                        )
                    else:
                        blocks.extend(
                            normalize_table(
                                table_html,
                                parent=parent,
                                identity=BlockRef(
                                    document_id=context.document_id,
                                    parse_run_id=context.parse_run_id,
                                    block_id=block_id,
                                ),
                                images=images,
                                limits=context.table_limits,
                            )
                        )
                if parent_id is not None and block_type in ("caption", "footnote"):
                    relations.append(
                        BlockRelation(
                            source=BlockRef(
                                document_id=context.document_id,
                                parse_run_id=context.parse_run_id,
                                block_id=block_id,
                            ),
                            target=BlockRef(
                                document_id=context.document_id,
                                parse_run_id=context.parse_run_id,
                                block_id=parent_id,
                            ),
                            kind="caption_of" if block_type == "caption" else "footnote_of",
                        )
                    )
        return DocumentIR(
            document_id=context.document_id,
            parse_run_id=context.parse_run_id,
            preview_asset_id=context.preview_asset_id,
            preview_sha256=context.preview.sha256,
            mineru_version=middle.version,
            adapter_version="3.0.0",
            pages=context.preview.pages,
            blocks=tuple(blocks),
            assets=images.used,
            relations=tuple(relations),
        )
