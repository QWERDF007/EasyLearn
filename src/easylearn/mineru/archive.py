import hashlib
import stat
import zlib
from pathlib import Path, PurePosixPath
from typing import Final
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from easylearn.errors import DomainError
from easylearn.images import ImageLimits, inspect_image
from easylearn.mineru.schema import (
    MinerUArchiveLimits,
    MinerUArchiveManifest,
    MinerUArchiveMember,
    MinerUArtifactKind,
    MinerUOptions,
)
from easylearn.paths import validate_portable_path

RESULT_FILES: Final[dict[str, MinerUArtifactKind]] = {
    "input.md": "markdown",
    "input_middle.json": "middle",
    "input_model.json": "model",
    "input_content_list.json": "content_list",
    "input_content_list_v2.json": "content_list_v2",
    "input_origin.pdf": "original",
}
IMAGE_SUFFIXES: Final = {".png", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".jpg", ".tiff", ".svg"}
JSON_ARTIFACT_KINDS: Final = {"middle", "model", "content_list", "content_list_v2"}


def result_root(options: MinerUOptions) -> PurePosixPath:
    directory = (
        options.parse_method
        if options.backend == "pipeline"
        else f"hybrid_{options.parse_method}"
        if options.backend.startswith("hybrid-")
        else "vlm"
    )
    return PurePosixPath("input") / directory


class MinerUArchive:
    """Inspect fixed-profile artifacts without extracting or publishing any files."""

    def __init__(
        self, *, limits: MinerUArchiveLimits | None = None, image_limits: ImageLimits | None = None
    ) -> None:
        self.limits = limits or MinerUArchiveLimits()
        self.image_limits = image_limits or ImageLimits()

    def inspect(self, path: Path, *, options: MinerUOptions) -> MinerUArchiveManifest:
        root = f"{result_root(options)}/"
        members = []
        with path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            if size > self.limits.max_archive_bytes:
                raise DomainError(
                    "MINERU_RESULT_TOO_LARGE", "Archive exceeds compressed size limit"
                )
            source.seek(0)
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            try:
                with ZipFile(source) as archive:
                    entries = archive.infolist()
                    if (
                        len(entries) > self.limits.max_members
                        or sum(info.file_size for info in entries) > self.limits.max_expanded_bytes
                    ):
                        raise DomainError(
                            "MINERU_RESULT_TOO_LARGE", "Archive exceeds expansion limits"
                        )
                    if not {root + name for name in RESULT_FILES}.issubset(archive.namelist()):
                        raise DomainError(
                            "MINERU_RESULT_INVALID", "Archive is missing requested artifacts"
                        )
                    names = {info.filename.casefold() for info in archive.infolist()}
                    if len(names) != len(archive.infolist()):
                        raise DomainError(
                            "MINERU_RESULT_INVALID", "Archive member names must be unique"
                        )
                    expanded = 0
                    remaining_pixels = self.image_limits.max_total_pixels
                    for info in entries:
                        if info.file_size > self.limits.max_member_bytes:
                            raise DomainError(
                                "MINERU_RESULT_TOO_LARGE", "Archive member exceeds size limit"
                            )
                        if info.flag_bits & 1 or info.compress_type not in (
                            ZIP_STORED,
                            ZIP_DEFLATED,
                        ):
                            raise DomainError(
                                "MINERU_RESULT_INVALID",
                                "Unsupported archive compression or encryption",
                            )
                        if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
                            raise DomainError(
                                "MINERU_RESULT_INVALID", "Archive members must be regular files"
                            )
                        try:
                            validate_portable_path(info.orig_filename)
                        except ValueError:
                            raise DomainError(
                                "MINERU_RESULT_INVALID", "Invalid archive member path"
                            ) from None
                        relative = PurePosixPath(info.filename.removeprefix(root))
                        kind = RESULT_FILES.get(str(relative))
                        if kind is None and (
                            relative.parent == PurePosixPath("images")
                            and relative.suffix.lower() in IMAGE_SUFFIXES
                        ):
                            kind = "image"
                        if not info.filename.startswith(root) or kind is None:
                            raise DomainError("MINERU_RESULT_INVALID", "Unexpected archive member")
                        if (
                            kind in JSON_ARTIFACT_KINDS
                            and info.file_size > self.limits.max_json_bytes
                        ):
                            raise DomainError(
                                "MINERU_RESULT_TOO_LARGE", "JSON artifact exceeds size limit"
                            )
                        with archive.open(info) as member:
                            checksum = hashlib.sha256()
                            observed = 0
                            while chunk := member.read(1024 * 1024):
                                observed += len(chunk)
                                expanded += len(chunk)
                                if (
                                    observed > self.limits.max_member_bytes
                                    or expanded > self.limits.max_expanded_bytes
                                ):
                                    raise DomainError(
                                        "MINERU_RESULT_TOO_LARGE",
                                        "Archive exceeds expansion limits",
                                    )
                                checksum.update(chunk)
                            if observed != info.file_size:
                                raise DomainError(
                                    "MINERU_RESULT_INVALID", "Archive member size does not match"
                                )
                        image = None
                        if kind == "image":
                            if remaining_pixels == 0:
                                raise DomainError(
                                    "IMAGE_LIMIT", "Archive image pixel budget exhausted"
                                )
                            with archive.open(info) as image_source:
                                image = inspect_image(
                                    image_source,
                                    expected_suffix=relative.suffix,
                                    limits=self.image_limits.model_copy(
                                        update={"max_total_pixels": remaining_pixels}
                                    ),
                                )
                            remaining_pixels -= image.decoded_pixels
                        members.append(
                            MinerUArchiveMember(
                                path=info.filename,
                                kind=kind,
                                sha256=checksum.hexdigest(),
                                size=observed,
                                image=image,
                            )
                        )
            except (BadZipFile, EOFError, zlib.error, UnicodeError):
                raise DomainError("MINERU_RESULT_INVALID", "Archive data is corrupt") from None
        return MinerUArchiveManifest(sha256=digest, size=size, members=tuple(members))
