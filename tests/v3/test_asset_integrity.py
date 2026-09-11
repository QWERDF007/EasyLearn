import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, Settings
from easylearn.database import Database
from easylearn.document_ir.schema import AssetDescriptor, Block, DocumentIR, ImageNode, PageGeometry, TextNode
from easylearn.documents.schema import ParseResultView
from easylearn.errors import DomainError
from easylearn.files import DocumentFiles
from easylearn.main import create_app
from easylearn.paths import DataPaths
from easylearn.publication import ArtifactPublisher, PublicationKind, PublicationStatus


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _make_dummy_ir(doc_id: UUID, parse_id: UUID, asset_id: UUID, asset_sha: str) -> DocumentIR:
    return DocumentIR(
        document_id=doc_id,
        parse_run_id=parse_id,
        preview_sha256=_sha256(b"preview"),
        preview_asset_id=uuid4(),
        mineru_version="1.0.0",
        adapter_version="1.0.0",
        pages=(
            PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
        ),
        blocks=(
            Block(
                block_id="b1",
                block_type="paragraph",
                order_index=0,
                source_nodes=(ImageNode(node_id="n1", asset_id=asset_id),),
            ),
        ),
        assets=(
            AssetDescriptor(
                asset_id=asset_id,
                sha256=asset_sha,
                mime="image/png",
                export_path="images/test.png",
            ),
        ),
    )


@pytest_asyncio.fixture
async def app_setup(tmp_path):
    settings = Settings(app=AppSettings(data_dir=tmp_path))
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        services = app.state.services
        yield services, client, tmp_path


@pytest.mark.asyncio
async def test_asset_download_checksum_verification_and_missing(app_setup):
    services, client, tmp_path = app_setup
    doc_resp = await client.post(
        "/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    )
    assert doc_resp.status_code == 201
    doc_id = UUID(doc_resp.json()["document_id"])
    parse_id = uuid4()
    asset_id = uuid4()
    asset_bytes = b"\x89PNG\r\n\x1a\nfakeimagecontent"
    asset_sha = _sha256(asset_bytes)

    # Setup parse directory on disk
    parse_dir = services.files.paths.document(doc_id) / "parses" / str(parse_id)
    (parse_dir / "images").mkdir(parents=True, exist_ok=True)
    asset_path = parse_dir / "images" / "test.png"
    asset_path.write_bytes(asset_bytes)

    ir = _make_dummy_ir(doc_id, parse_id, asset_id, asset_sha)
    (parse_dir / "document.json").write_text(
        ir.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
    )
    (parse_dir / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

    # Record parse in database
    async with services.database.transaction() as conn:
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(parse_id),
                str(doc_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(parse_id), str(doc_id)),
        )

    # 1. Download asset normally -> 200
    resp = await client.get(f"/api/documents/{doc_id}/files/asset:{parse_id}:{asset_id}")
    assert resp.status_code == 200, resp.text
    assert resp.content == asset_bytes

    # 2. Corrupt asset -> raises ASSET_CORRUPT (500)
    asset_path.write_bytes(b"corrupted bytes")
    corrupt_resp = await client.get(f"/api/documents/{doc_id}/files/asset:{parse_id}:{asset_id}")
    assert corrupt_resp.status_code == 500
    assert corrupt_resp.json()["code"] == "ASSET_CORRUPT"

    # 3. Missing asset -> raises ASSET_CORRUPT (500)
    asset_path.unlink()
    missing_resp = await client.get(f"/api/documents/{doc_id}/files/asset:{parse_id}:{asset_id}")
    assert missing_resp.status_code == 500
    assert missing_resp.json()["code"] == "ASSET_CORRUPT"


@pytest.mark.asyncio
async def test_export_download_manifest_checksum_verification(app_setup):
    services, client, tmp_path = app_setup
    doc_resp = await client.post(
        "/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    )
    doc_id = UUID(doc_resp.json()["document_id"])
    export_id = uuid4()

    # Setup export directory
    export_dir = services.files.paths.export(doc_id, export_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    source_content = b"# Document Content\n"
    source_sha = _sha256(source_content)
    (export_dir / "source.md").write_bytes(source_content)

    manifest = {
        "document_id": str(doc_id),
        "export_id": str(export_id),
        "files": [
            {"path": "source.md", "size": len(source_content), "sha256": source_sha}
        ],
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Record publication in database
    async with services.database.transaction() as conn:
        await conn.execute(
            "INSERT INTO publications (id, document_id, artifact_id, kind, staging_path, destination_path, payload_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(doc_id),
                str(export_id),
                PublicationKind.EXPORT.value,
                str(export_dir),
                str(export_dir),
                json.dumps({"export_id": str(export_id)}),
                PublicationStatus.RECORDED.value,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

    # 1. Download export file -> 200
    resp = await client.get(f"/api/documents/{doc_id}/files/export:{export_id}:source.md")
    assert resp.status_code == 200
    assert resp.content == source_content

    # 2. Corrupt source.md -> 500 EXPORT_CORRUPT
    (export_dir / "source.md").write_bytes(b"# Corrupted Content\n")
    bad_resp = await client.get(f"/api/documents/{doc_id}/files/export:{export_id}:source.md")
    assert bad_resp.status_code == 500
    assert bad_resp.json()["code"] == "EXPORT_CORRUPT"


@pytest.mark.asyncio
async def test_reconciliation_cleans_orphan_assets_without_touching_referenced_ones(app_setup):
    services, client, tmp_path = app_setup
    doc_resp = await client.post(
        "/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    )
    doc_id = UUID(doc_resp.json()["document_id"])
    parse_id = uuid4()
    asset_id = uuid4()
    asset_bytes = b"valid_asset_data"
    asset_sha = _sha256(asset_bytes)

    parse_dir = services.files.paths.document(doc_id) / "parses" / str(parse_id)
    (parse_dir / "images").mkdir(parents=True, exist_ok=True)
    valid_asset = parse_dir / "images" / "test.png"
    valid_asset.write_bytes(asset_bytes)

    # Create an orphan file in images
    orphan_file = parse_dir / "images" / "orphan.png"
    orphan_file.write_bytes(b"orphan_unreferenced_data")

    ir = _make_dummy_ir(doc_id, parse_id, asset_id, asset_sha)
    (parse_dir / "document.json").write_text(
        ir.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
    )
    (parse_dir / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

    async with services.database.transaction() as conn:
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(parse_id),
                str(doc_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )

    # Run publisher reconciliation
    publisher = ArtifactPublisher(services.database, services.files)
    report = await publisher.reconcile()

    # Orphan file should be removed
    assert not orphan_file.exists()
    assert report.removed_orphan_assets >= 1

    # Referenced valid asset must still exist
    assert valid_asset.is_file()
    assert valid_asset.read_bytes() == asset_bytes

    # Running reconciliation again should be idempotent
    report2 = await publisher.reconcile()
    assert report2.removed_orphan_assets == 0
    assert valid_asset.is_file()


@pytest.mark.asyncio
async def test_parse_result_view_reports_corrupt_when_asset_missing(app_setup):
    services, client, tmp_path = app_setup
    doc_resp = await client.post(
        "/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    )
    doc_id = UUID(doc_resp.json()["document_id"])
    parse_id = uuid4()
    asset_id = uuid4()
    asset_bytes = b"valid_asset_data"
    asset_sha = _sha256(asset_bytes)

    parse_dir = services.files.paths.document(doc_id) / "parses" / str(parse_id)
    (parse_dir / "images").mkdir(parents=True, exist_ok=True)
    valid_asset = parse_dir / "images" / "test.png"
    valid_asset.write_bytes(asset_bytes)

    ir = _make_dummy_ir(doc_id, parse_id, asset_id, asset_sha)
    (parse_dir / "document.json").write_text(
        ir.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
    )
    (parse_dir / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

    async with services.database.transaction() as conn:
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(parse_id),
                str(doc_id),
                f"parses/{parse_id}/preview.pdf",
                f"parses/{parse_id}/document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(parse_id), str(doc_id)),
        )

    # 1. When intact, integrity_status is valid
    doc_view = await services.documents.get(doc_id)
    assert doc_view.parse_results[0].integrity_status == "valid", doc_view.parse_results[0].corrupt_reasons

    # 2. Corrupt or delete the asset
    valid_asset.unlink()
    services.documents.invalidate_ir(doc_id, parse_id)
    doc_view_corrupt = await services.documents.get(doc_id)
    assert doc_view_corrupt.parse_results[0].integrity_status == "corrupt"
    assert len(doc_view_corrupt.parse_results[0].corrupt_reasons) > 0


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent_and_preserves_old_version_assets(app_setup):
    services, client, tmp_path = app_setup
    doc_resp = await client.post(
        "/api/documents", files={"file": ("test.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    )
    doc_id = UUID(doc_resp.json()["document_id"])

    # Create two parse versions: v1 (old) and v2 (active)
    parse_v1 = uuid4()
    asset_v1 = uuid4()
    bytes_v1 = b"image_v1_data"
    sha_v1 = _sha256(bytes_v1)

    dir_v1 = services.files.paths.document(doc_id) / "parses" / str(parse_v1)
    (dir_v1 / "images").mkdir(parents=True, exist_ok=True)
    file_v1 = dir_v1 / "images" / "test.png"
    file_v1.write_bytes(bytes_v1)
    ir_v1 = _make_dummy_ir(doc_id, parse_v1, asset_v1, sha_v1)
    (dir_v1 / "document.json").write_text(
        ir_v1.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
    )
    (dir_v1 / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

    # Add an orphan file in v1
    orphan_in_v1 = dir_v1 / "images" / "orphan_v1.png"
    orphan_in_v1.write_bytes(b"orphan_data")

    parse_v2 = uuid4()
    asset_v2 = uuid4()
    bytes_v2 = b"image_v2_data"
    sha_v2 = _sha256(bytes_v2)

    dir_v2 = services.files.paths.document(doc_id) / "parses" / str(parse_v2)
    (dir_v2 / "images").mkdir(parents=True, exist_ok=True)
    file_v2 = dir_v2 / "images" / "test.png"
    file_v2.write_bytes(bytes_v2)
    ir_v2 = _make_dummy_ir(doc_id, parse_v2, asset_v2, sha_v2)
    (dir_v2 / "document.json").write_text(
        ir_v2.model_dump_json(exclude_computed_fields=True, indent=2), encoding="utf-8"
    )
    (dir_v2 / "preview.pdf").write_bytes(b"%PDF-1.4 preview")

    # Both parses recorded in parse_results, v2 is active
    async with services.database.transaction() as conn:
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(parse_v1),
                str(doc_id),
                f"parses/{parse_v1}/preview.pdf",
                f"parses/{parse_v1}/document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, pages, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(parse_v2),
                str(doc_id),
                f"parses/{parse_v2}/preview.pdf",
                f"parses/{parse_v2}/document.json",
                1,
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.execute(
            "UPDATE documents SET active_parse_id = ? WHERE id = ?",
            (str(parse_v2), str(doc_id)),
        )

    publisher = ArtifactPublisher(services.database, services.files)

    # First reconciliation
    report_1 = await publisher.reconcile()
    assert report_1.removed_orphan_assets >= 1
    assert not orphan_in_v1.exists()
    assert file_v1.is_file()
    assert file_v2.is_file()

    # Second reconciliation (idempotent)
    report_2 = await publisher.reconcile()
    assert report_2.removed_orphan_assets == 0
    assert file_v1.is_file()
    assert file_v2.is_file()

