from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import AfterValidator


def validate_portable_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.name
        or path.is_absolute()
        or path.as_posix() != value
        or any(char in value for char in '\\:*?"<>|')
        or any(ord(char) < 32 for char in value)
        or any(
            part in (".", "..") or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
    ):
        raise ValueError("Export path must be canonical, relative and portable")
    return value


PortablePath = Annotated[str, AfterValidator(validate_portable_path)]
