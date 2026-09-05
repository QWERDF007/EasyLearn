from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Point = tuple[float, float]
BBox = tuple[float, float, float, float]
Transform = tuple[float, float, float, float, float, float]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class BlockRef(FrozenModel):
    document_id: UUID
    parse_run_id: UUID
    block_id: Identifier


class PageGeometry(FrozenModel):
    page_index: int = Field(ge=0)
    media_box: BBox
    crop_box: BBox
    intrinsic_rotation: Literal[0, 90, 180, 270] = 0
    user_unit: float = Field(default=1, gt=0)
    page_label: str | None = None

    @model_validator(mode="after")
    def validate_boxes(self) -> Self:
        for box in (self.media_box, self.crop_box):
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("Page boxes must have positive area")
        if not (
            self.media_box[0] <= self.crop_box[0] < self.crop_box[2] <= self.media_box[2]
            and self.media_box[1] <= self.crop_box[1] < self.crop_box[3] <= self.media_box[3]
        ):
            raise ValueError("CropBox must be contained in MediaBox")
        return self


class SourceRegion(FrozenModel):
    page_index: int = Field(ge=0)
    bbox_pdf: BBox
    bbox_norm: BBox
    polygon_pdf: tuple[Point, Point, Point, Point]
    source_bbox: BBox
    source_to_pdf_transform: Transform


class TextNode(FrozenModel):
    node_id: Identifier
    type: Literal["text"] = "text"
    text: str


class MathNode(FrozenModel):
    node_id: Identifier
    type: Literal["math"] = "math"
    latex: str


class CodeNode(FrozenModel):
    node_id: Identifier
    type: Literal["code"] = "code"
    code: str
    language: str | None = None


class LinkNode(FrozenModel):
    node_id: Identifier
    type: Literal["link"] = "link"
    label: str
    target: str


InlineNode = Annotated[TextNode | MathNode | CodeNode | LinkNode, Field(discriminator="type")]


class Block(FrozenModel):
    block_id: Identifier
    block_type: Literal[
        "heading",
        "paragraph",
        "list",
        "list_item",
        "table",
        "table_cell",
        "formula",
        "code",
        "image",
        "caption",
        "reference",
        "footnote",
    ]
    order_index: int = Field(ge=0)
    parent_block_id: Identifier | None = None
    section_path: tuple[str, ...] = ()
    source_nodes: tuple[InlineNode, ...] = ()
    source_regions: tuple[SourceRegion, ...] = ()
    parse_warnings: tuple[str, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_text(self) -> str:
        return "".join(
            node.text
            if isinstance(node, TextNode)
            else node.latex
            if isinstance(node, MathNode)
            else node.code
            if isinstance(node, CodeNode)
            else node.label
            for node in self.source_nodes
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def translatable(self) -> bool:
        return any(
            (isinstance(node, TextNode) and bool(node.text.strip()))
            or (isinstance(node, LinkNode) and bool(node.label.strip()))
            for node in self.source_nodes
        )

    @model_validator(mode="after")
    def validate_nodes(self) -> Self:
        if len({node.node_id for node in self.source_nodes}) != len(self.source_nodes):
            raise ValueError("Duplicate node in block")
        return self


class DocumentIR(FrozenModel):
    schema_version: Literal["2.0"] = "2.0"
    document_id: UUID
    parse_run_id: UUID
    preview_asset_id: UUID
    preview_sha256: Sha256
    mineru_version: str
    adapter_version: str
    pages: tuple[PageGeometry, ...] = Field(min_length=1)
    blocks: tuple[Block, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Duplicate block in parse snapshot")
        ids = {block.block_id for block in self.blocks}
        graph: dict[str, tuple[str, ...]] = {}
        for block in self.blocks:
            if block.parent_block_id is not None and block.parent_block_id not in ids:
                raise ValueError("Unknown parent block")
            graph[block.block_id] = (block.parent_block_id,) if block.parent_block_id else ()
        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            raise ValueError("Parent cycle in parse snapshot") from exc
        return self
