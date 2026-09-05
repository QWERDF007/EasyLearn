from typing import IO

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from easylearn.errors import DomainError


class ImageLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pixels: int = Field(default=40_000_000, gt=0)
    max_frames: int = Field(default=100, gt=0)
    max_total_pixels: int = Field(default=80_000_000, gt=0)


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
