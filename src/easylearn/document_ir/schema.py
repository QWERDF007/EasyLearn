import hashlib
import json
from bisect import bisect_left
from graphlib import CycleError, TopologicalSorter
from math import isclose
from pathlib import PurePosixPath
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

from easylearn.paths import PortablePath

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


class SourceCoordinates(FrozenModel):
    space: Literal["page_units", "normalized_1000", "normalized_01", "image_pixels"]
    size: tuple[float, float]
    origin: Literal["top_left", "bottom_left"]
    rotation_applied: bool
    crop_applied: bool
    to_pdf: Transform

    @model_validator(mode="after")
    def validate_transform(self) -> Self:
        if min(self.size) <= 0:
            raise ValueError("Source dimensions must be positive")
        a, b, c, d, _, _ = self.to_pdf
        if abs(a * d - b * c) < 1e-12:
            raise ValueError("Coordinate transform must be invertible")
        return self

    @property
    def effective_transform(self) -> Transform:
        sx = sy = 1.0
        if self.space in ("normalized_1000", "normalized_01"):
            divisor = 1000 if self.space == "normalized_1000" else 1
            sx, sy = self.size[0] / divisor, self.size[1] / divisor
        a, b, c, d, e, f = self.to_pdf
        return a * sx, b * sx, c * sy, d * sy, e, f


class SourceRegion(FrozenModel):
    page_index: int = Field(ge=0)
    bbox_pdf: BBox
    bbox_norm: BBox
    polygon_pdf: tuple[Point, Point, Point, Point]
    source_bbox: BBox
    source_to_pdf_transform: Transform
    source_coordinates: SourceCoordinates

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        for bbox in (self.bbox_pdf, self.source_bbox):
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                raise ValueError("Region bounds must have positive area")
        if not (
            0 <= self.bbox_norm[0] < self.bbox_norm[2] <= 1
            and 0 <= self.bbox_norm[1] < self.bbox_norm[3] <= 1
        ):
            raise ValueError("Normalized region must have positive area inside [0, 1]")
        polygon = self.polygon_pdf
        area = sum(
            polygon[i][0] * polygon[(i + 1) % 4][1] - polygon[(i + 1) % 4][0] * polygon[i][1]
            for i in range(4)
        )
        if isclose(area, 0, abs_tol=1e-12):
            raise ValueError("Region polygon must have nonzero area")
        envelope = (
            min(p[0] for p in polygon),
            min(p[1] for p in polygon),
            max(p[0] for p in polygon),
            max(p[1] for p in polygon),
        )
        if not all(
            isclose(a, b, abs_tol=1e-6) for a, b in zip(envelope, self.bbox_pdf, strict=True)
        ):
            raise ValueError("Region bounds must enclose exactly the transformed polygon")
        a, b, c, d, e, f = self.source_to_pdf_transform
        if not all(
            isclose(actual, expected, abs_tol=1e-12)
            for actual, expected in zip(
                self.source_to_pdf_transform,
                self.source_coordinates.effective_transform,
                strict=True,
            )
        ):
            raise ValueError("Region transform disagrees with the source coordinate declaration")
        x0, y0, x1, y1 = self.source_bbox
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        if abs(a * d - b * c) < 1e-12 or not all(
            isclose(px, a * x + c * y + e, abs_tol=1e-6)
            and isclose(py, b * x + d * y + f, abs_tol=1e-6)
            for (x, y), (px, py) in zip(corners, polygon, strict=True)
        ):
            raise ValueError("Region transform does not reproduce the polygon")
        return self


class TextNode(FrozenModel):
    node_id: Identifier
    type: Literal["text"] = "text"
    text: str


class MathNode(FrozenModel):
    node_id: Identifier
    type: Literal["math"] = "math"
    latex: str
    screenshot_asset_id: UUID | None = None


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


class ReferenceNode(FrozenModel):
    node_id: Identifier
    type: Literal["reference"] = "reference"
    label: str
    target: BlockRef


class ImageNode(FrozenModel):
    node_id: Identifier
    type: Literal["image"] = "image"
    asset_id: UUID
    alt: str = ""


InlineNode = Annotated[
    TextNode | MathNode | CodeNode | LinkNode | ReferenceNode | ImageNode,
    Field(discriminator="type"),
]


class AssetDescriptor(FrozenModel):
    asset_id: UUID
    sha256: Sha256
    mime: str
    export_path: PortablePath


class BlockRelation(FrozenModel):
    source: BlockRef
    target: BlockRef
    kind: Literal["caption_of", "footnote_of", "continues", "merged_from", "related"]


class TableCell(FrozenModel):
    block_ref: BlockRef
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    role: Literal["header", "data"] = "data"


class TableStructure(FrozenModel):
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)
    cells: tuple[TableCell, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def complete(self) -> bool:
        return (
            sum(cell.row_span * cell.column_span for cell in self.cells) == self.rows * self.columns
        )

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        if len({cell.block_ref for cell in self.cells}) != len(self.cells):
            raise ValueError("Duplicate table cell identity")
        events: list[tuple[int, int, tuple[int, int, int]]] = []
        for index, cell in enumerate(self.cells):
            end_row = cell.row + cell.row_span
            end_column = cell.column + cell.column_span
            if end_row > self.rows or end_column > self.columns:
                raise ValueError("Cell outside table dimensions")
            interval = (cell.column, end_column, index)
            events.extend(((cell.row, 1, interval), (end_row, 0, interval)))
        active: list[tuple[int, int, int]] = []
        for _, starting, interval in sorted(events):
            position = bisect_left(active, interval)
            if not starting:
                active.pop(position)
            else:
                if (position > 0 and active[position - 1][1] > interval[0]) or (
                    position < len(active) and active[position][0] < interval[1]
                ):
                    raise ValueError("Overlapping table cells")
                active.insert(position, interval)
        return self


class PageLocator(FrozenModel):
    kind: Literal["pdf_page"] = "pdf_page"
    page_indices: tuple[Annotated[int, Field(ge=0)], ...] = Field(min_length=1)


class OfficeElementLocator(FrozenModel):
    kind: Literal["office_element"] = "office_element"
    format: Literal["docx", "pptx", "xlsx"]
    element_id: Identifier


SourceLocator = Annotated[PageLocator | OfficeElementLocator, Field(discriminator="kind")]


BlockType = Literal[
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


class Block(FrozenModel):
    block_id: Identifier
    block_type: BlockType
    order_index: int = Field(ge=0)
    parent_block_id: Identifier | None = None
    section_path: tuple[str, ...] = ()
    source_nodes: tuple[InlineNode, ...] = ()
    source_regions: tuple[SourceRegion, ...] = ()
    parse_warnings: tuple[str, ...] = ()
    table: TableStructure | None = None
    source_locator: SourceLocator | None = Field(
        default=None, description="Fallback locator when no validated PDF regions are available"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def localization_level(self) -> Literal["region", "page", "element", "none"]:
        if self.source_regions:
            return "region"
        if isinstance(self.source_locator, PageLocator):
            return "page"
        if isinstance(self.source_locator, OfficeElementLocator):
            return "element"
        return "none"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_content_hash(self) -> Sha256:
        structure = self.model_dump(
            mode="json",
            include={"block_type", "source_nodes", "table"},
            exclude_computed_fields=True,
        )
        canonical = json.dumps(
            structure, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
            if isinstance(node, (LinkNode, ReferenceNode))
            else node.alt
            for node in self.source_nodes
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def translatable(self) -> bool:
        return any(
            (isinstance(node, TextNode) and bool(node.text.strip()))
            or (isinstance(node, (LinkNode, ReferenceNode)) and bool(node.label.strip()))
            or (isinstance(node, ImageNode) and bool(node.alt.strip()))
            for node in self.source_nodes
        )

    @model_validator(mode="after")
    def validate_nodes(self) -> Self:
        if self.source_regions and self.source_locator is not None:
            raise ValueError("Fallback locator cannot accompany validated PDF regions")
        if self.table is not None and self.block_type != "table":
            raise ValueError("Table structure is only valid on a table block")
        if len({node.node_id for node in self.source_nodes}) != len(self.source_nodes):
            raise ValueError("Duplicate node in block")
        return self


class DocumentIR(FrozenModel):
    schema_version: Literal["3.0"] = "3.0"
    document_id: UUID
    parse_run_id: UUID
    preview_asset_id: UUID
    preview_sha256: Sha256
    mineru_version: str
    adapter_version: str
    pages: tuple[PageGeometry, ...] = Field(min_length=1)
    blocks: tuple[Block, ...]
    assets: tuple[AssetDescriptor, ...] = ()
    relations: tuple[BlockRelation, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_count(self) -> int:
        return len(self.pages)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if tuple(page.page_index for page in self.pages) != tuple(range(len(self.pages))):
            raise ValueError("Page indices must be complete, ordered and start at zero")
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Duplicate block in parse snapshot")
        orders = [block.order_index for block in self.blocks]
        if orders != sorted(set(orders)):
            raise ValueError("Reading order must be unique and increasing")
        blocks_by_id = {block.block_id: block for block in self.blocks}
        block_refs = {
            BlockRef(
                document_id=self.document_id, parse_run_id=self.parse_run_id, block_id=block_id
            )
            for block_id in blocks_by_id
        }
        assets_by_id = {asset.asset_id: asset for asset in self.assets}
        export_paths = {asset.export_path.casefold() for asset in self.assets}
        if len(assets_by_id) != len(self.assets) or len(export_paths) != len(self.assets):
            raise ValueError("Duplicate asset identity or export path")
        if any(
            str(parent) in export_paths
            for path in export_paths
            for parent in PurePosixPath(path).parents
        ):
            raise ValueError("Duplicate asset path conflicts with an export directory")
        if len(set(self.relations)) != len(self.relations):
            raise ValueError("Relation identities must be unique")
        for relation in self.relations:
            if relation.source not in block_refs or relation.target not in block_refs:
                raise ValueError("Reference outside snapshot")
            if relation.source == relation.target:
                raise ValueError("Relation cannot point to itself")
            source_type = blocks_by_id[relation.source.block_id].block_type
            if (relation.kind == "caption_of" and source_type != "caption") or (
                relation.kind == "footnote_of" and source_type != "footnote"
            ):
                raise ValueError("Relation kind must match its source block type")
        graph: dict[str, tuple[str, ...]] = {}
        listed_cells: set[str] = set()
        for block in self.blocks:
            if isinstance(block.source_locator, PageLocator):
                indices = block.source_locator.page_indices
                if len(set(indices)) != len(indices) or any(
                    index >= len(self.pages) for index in indices
                ):
                    raise ValueError("Locator pages must uniquely identify pages in the snapshot")
            for node in block.source_nodes:
                asset_id = (
                    node.asset_id
                    if isinstance(node, ImageNode)
                    else (node.screenshot_asset_id if isinstance(node, MathNode) else None)
                )
                if asset_id is not None:
                    if asset_id not in assets_by_id:
                        raise ValueError("Unknown asset in image node")
                    if not assets_by_id[asset_id].mime.startswith("image/"):
                        raise ValueError("Image asset must use an image MIME type")
                if isinstance(node, ReferenceNode) and node.target not in block_refs:
                    raise ValueError("Reference outside snapshot")
            if block.parent_block_id is not None and block.parent_block_id not in blocks_by_id:
                raise ValueError("Unknown parent block")
            graph[block.block_id] = (block.parent_block_id,) if block.parent_block_id else ()
            if block.table is not None:
                for cell in block.table.cells:
                    ref = cell.block_ref
                    if (ref.document_id, ref.parse_run_id) != (self.document_id, self.parse_run_id):
                        raise ValueError("Table cell version does not match snapshot")
                    if ref.block_id not in blocks_by_id:
                        raise ValueError("Unknown table cell")
                    child = blocks_by_id[ref.block_id]
                    if child.block_type != "table_cell" or child.parent_block_id != block.block_id:
                        raise ValueError("Table cell must belong to the containing table block")
                    listed_cells.add(ref.block_id)
            for region in block.source_regions:
                if region.page_index >= len(self.pages):
                    raise ValueError("Unknown region page")
                cx0, cy0, cx1, cy1 = self.pages[region.page_index].crop_box
                left, bottom, right, top = region.bbox_pdf
                if left < cx0 or bottom < cy0 or right > cx1 or top > cy1:
                    raise ValueError("Region bounds outside page CropBox")
                normalized = (
                    (left - cx0) / (cx1 - cx0),
                    (cy1 - top) / (cy1 - cy0),
                    (right - cx0) / (cx1 - cx0),
                    (cy1 - bottom) / (cy1 - cy0),
                )
                if not all(
                    isclose(a, b, abs_tol=1e-9)
                    for a, b in zip(normalized, region.bbox_norm, strict=True)
                ):
                    raise ValueError("Normalized region does not match the page CropBox")
        if any(
            block.block_type == "table_cell" and block.block_id not in listed_cells
            for block in self.blocks
        ):
            raise ValueError("Table structure must list every child table cell")
        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            raise ValueError("Parent cycle in parse snapshot") from exc
        return self
