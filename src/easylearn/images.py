from math import ceil, isfinite
from re import fullmatch
from typing import IO
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from easylearn.errors import DomainError


class ImageLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pixels: int = Field(default=40_000_000, gt=0)
    max_frames: int = Field(default=100, gt=0)
    max_total_pixels: int = Field(default=80_000_000, gt=0)
    max_svg_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_svg_elements: int = Field(default=10_000, gt=0)


class ImageMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mime: str
    format: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frames: int = Field(gt=0)
    decoded_pixels: int = Field(gt=0)


def inspect_image(source: IO[bytes], *, expected_suffix: str, limits: ImageLimits) -> ImageMetadata:
    """Decode every frame. Run inside the caller's bounded validation execution unit."""
    if expected_suffix.lower() == ".svg":
        return _inspect_svg(source, limits)
    try:
        with Image.open(source) as probe:
            image_format = probe.format
            if Image.registered_extensions().get(expected_suffix.lower()) != image_format:
                raise DomainError(
                    "IMAGE_FORMAT_MISMATCH", "Image format does not match its extension"
                )
            if probe.width * probe.height > min(limits.max_pixels, limits.max_total_pixels):
                raise DomainError("IMAGE_LIMIT", "Image exceeds decoded pixel limit")
            probe.verify()
        source.seek(0)
        with Image.open(source) as picture:
            image_format = picture.format
            if image_format is None or image_format not in Image.MIME:
                raise DomainError(
                    "IMAGE_FORMAT_UNSUPPORTED", "Image decoder has no MIME declaration"
                )
            width, height = picture.size
            frames = 0
            decoded_pixels = 0
            while True:
                try:
                    picture.seek(frames)
                except EOFError:
                    break
                pixels = picture.width * picture.height
                if (
                    frames >= limits.max_frames
                    or pixels > limits.max_pixels
                    or decoded_pixels + pixels > limits.max_total_pixels
                ):
                    raise DomainError("IMAGE_LIMIT", "Image exceeds decoded resource limits")
                picture.load()
                decoded_pixels += pixels
                frames += 1
            return ImageMetadata(
                mime=Image.MIME[image_format],
                format=image_format,
                width=width,
                height=height,
                frames=frames,
                decoded_pixels=decoded_pixels,
            )
    except Image.DecompressionBombError:
        raise DomainError("IMAGE_LIMIT", "Image exceeds decoder pixel limit") from None
    except (UnidentifiedImageError, OSError, ValueError, EOFError, SyntaxError):
        raise DomainError("IMAGE_INVALID", "Image cannot be decoded completely") from None


_SVG_TAGS = {
    "svg",
    "g",
    "defs",
    "clipPath",
    "mask",
    "linearGradient",
    "radialGradient",
    "stop",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "text",
    "tspan",
    "title",
    "desc",
    "use",
}
_SVG_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _inspect_svg(source: IO[bytes], limits: ImageLimits) -> ImageMetadata:
    try:
        source.seek(0)
        data = source.read(limits.max_svg_bytes + 1)
    except (OSError, ValueError):
        raise DomainError("IMAGE_SVG_INVALID", "SVG cannot be read") from None
    if len(data) > limits.max_svg_bytes:
        raise DomainError("IMAGE_LIMIT", "SVG exceeds the configured byte limit")
    lowered = data.lower()
    if any(token in lowered for token in (b"<!doctype", b"<!entity", b"<script", b"foreignobject")):
        raise DomainError("IMAGE_SVG_INVALID", "SVG contains forbidden active content")
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, UnicodeDecodeError):
        raise DomainError("IMAGE_SVG_INVALID", "SVG XML is invalid") from None
    if _svg_name(root.tag) != "svg":
        raise DomainError("IMAGE_SVG_INVALID", "SVG root element is missing")
    for elements, element in enumerate(root.iter(), start=1):
        if elements > limits.max_svg_elements:
            raise DomainError("IMAGE_LIMIT", "SVG contains too many elements")
        if _svg_name(element.tag) not in _SVG_TAGS:
            raise DomainError("IMAGE_SVG_INVALID", "SVG contains a forbidden element")
        for attribute, value in element.attrib.items():
            name = _svg_name(attribute).lower()
            text = value.strip().lower()
            if (name.startswith("on") or name in {"src", "href"}) and not (
                name == "href" and text.startswith("#")
            ):
                raise DomainError("IMAGE_SVG_INVALID", "SVG contains an external reference")
            if name == "style" and (
                "javascript:" in text
                or "expression(" in text
                or ("url(" in text and "url(#" not in text)
            ):
                raise DomainError("IMAGE_SVG_INVALID", "SVG contains unsafe style content")
            if "javascript:" in text:
                raise DomainError("IMAGE_SVG_INVALID", "SVG contains executable content")
    width, height = _svg_dimensions(root)
    pixels = width * height
    if pixels > min(limits.max_pixels, limits.max_total_pixels):
        raise DomainError("IMAGE_LIMIT", "SVG exceeds decoded pixel limits")
    return ImageMetadata(
        mime="image/svg+xml",
        format="SVG",
        width=width,
        height=height,
        frames=1,
        decoded_pixels=pixels,
    )


def _svg_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _svg_dimensions(root: ElementTree.Element) -> tuple[int, int]:
    width = _svg_length(root.attrib.get("width"))
    height = _svg_length(root.attrib.get("height"))
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view_box:
        try:
            values = [float(value) for value in view_box.replace(",", " ").split()]
        except ValueError:
            raise DomainError("IMAGE_SVG_INVALID", "SVG viewBox is invalid") from None
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            raise DomainError("IMAGE_SVG_INVALID", "SVG viewBox is invalid")
        if not all(isfinite(value) for value in values):
            raise DomainError("IMAGE_SVG_INVALID", "SVG viewBox is invalid")
        width = width or values[2]
        height = height or values[3]
    width = width or 300
    height = height or 150
    return max(1, ceil(width)), max(1, ceil(height))


def _svg_length(value: str | None) -> float | None:
    if value is None:
        return None
    match = fullmatch(rf"({_SVG_NUMBER})(?:px|pt|pc|mm|cm|in)?", value.strip())
    if match is None:
        return None
    result = float(match.group(1))
    if not isfinite(result) or result <= 0:
        raise DomainError("IMAGE_SVG_INVALID", "SVG dimensions must be positive")
    return result
