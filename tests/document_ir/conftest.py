from uuid import UUID

import pytest


@pytest.fixture
def document_payload():
    return {
        "document_id": str(UUID(int=1)),
        "parse_run_id": str(UUID(int=2)),
        "preview_asset_id": str(UUID(int=3)),
        "preview_sha256": "0" * 64,
        "mineru_version": "3.4.5",
        "adapter_version": "2.0.0",
        "pages": [{"page_index": 0, "media_box": [0, 0, 600, 800], "crop_box": [0, 0, 600, 800]}],
        "blocks": [],
    }
