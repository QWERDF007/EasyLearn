from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from easylearn.document_ir.schema import PageGeometry, Sha256


class PreviewLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pages: int = Field(default=2000, gt=0, le=100000)
    max_page_points: float = Field(default=14400, gt=0, allow_inf_nan=False)
    render_edge_pixels: int = Field(default=1024, ge=64, le=4096)


class PreflightRequest(BaseModel):
    path: Path
    expected_sha256: Sha256
    limits: PreviewLimits


class PreflightReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: Sha256
    pages: tuple[PageGeometry, ...] = Field(min_length=1)
    renderer: str
    metadata_reader: str


PreviewStatus = Literal[
    "QUEUED", "CONVERTING", "VALIDATING", "READY", "FAILED", "CANCEL_REQUESTED", "CANCELLED"
]
