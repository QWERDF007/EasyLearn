"""Input format recognition kept at the document boundary."""

from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from easylearn.errors import DomainError

INPUT_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jp2": "image/jp2",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_SIGNATURES = {
    ".pdf": b"%PDF-",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
}


def validate_input(path: Path, suffix: str) -> str:
    """Validate the cheap container signature before registering an upload."""

    with path.open("rb") as source:
        header = source.read(16)
    signature = _SIGNATURES.get(suffix)
    if signature is not None:
        valid = header.startswith(signature)
    elif suffix in {".webp", ".bmp", ".gif", ".jp2", ".tif", ".tiff"}:
        # Pillow performs the complete image check during preview preparation.
        valid = True
    else:
        required_member = {
            ".docx": "word/document.xml",
            ".pptx": "ppt/presentation.xml",
            ".xlsx": "xl/workbook.xml",
        }.get(suffix)
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                valid = required_member in names and "[Content_Types].xml" in names
        except BadZipFile:
            valid = False
    if not valid:
        raise DomainError("UPLOAD_FORMAT_MISMATCH", "File format does not match its extension")
    return INPUT_MIME[suffix]
