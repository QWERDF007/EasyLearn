from collections.abc import Mapping
from uuid import UUID

from easylearn.document_ir.schema import AssetDescriptor
from easylearn.errors import DomainError
from easylearn.paths import validate_portable_path


class ImageReferences:
    """Resolve upstream names only against the parse execution's registered assets."""

    def __init__(self, registered: Mapping[str, AssetDescriptor]) -> None:
        self._registered = dict(registered)
        self._used: dict[UUID, AssetDescriptor] = {}

    def resolve(self, path: str) -> AssetDescriptor:
        try:
            validate_portable_path(path)
        except ValueError:
            raise DomainError("MINERU_ASSET_INVALID", "Invalid image asset reference") from None
        asset = self._registered.get(path)
        if asset is None or not asset.mime.startswith("image/"):
            raise DomainError(
                "MINERU_ASSET_INVALID", "Image must reference a registered image asset"
            )
        previous = self._used.get(asset.asset_id)
        if previous is not None and previous != asset:
            raise DomainError("MINERU_ASSET_INVALID", "Conflicting image asset identity")
        self._used[asset.asset_id] = asset
        return asset

    @property
    def used(self) -> tuple[AssetDescriptor, ...]:
        return tuple(self._used.values())
