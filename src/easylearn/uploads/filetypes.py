from pathlib import Path
from zipfile import BadZipFile, ZipFile

from easylearn.errors import DomainError

INPUT_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def validate_input(path: Path, suffix: str) -> str:
    with path.open("rb") as source:
        header = source.read(16)
    signatures = {
        ".pdf": b"%PDF-",
        ".png": b"\x89PNG\r\n\x1a\n",
        ".jpg": b"\xff\xd8\xff",
        ".jpeg": b"\xff\xd8\xff",
    }
    if suffix in signatures:
        valid = header.startswith(signatures[suffix])
    else:
        part = {
            ".docx": "word/document.xml",
            ".pptx": "ppt/presentation.xml",
            ".xlsx": "xl/workbook.xml",
        }.get(suffix)
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                valid = part in names and "[Content_Types].xml" in names
        except BadZipFile:
            valid = False
    if not valid:
        raise DomainError("UPLOAD_FORMAT_MISMATCH", "File format does not match its extension")
    return INPUT_MIME[suffix]
