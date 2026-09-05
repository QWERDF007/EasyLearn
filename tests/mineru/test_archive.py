import hashlib
import os
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from PIL import Image

from easylearn.errors import DomainError
from easylearn.mineru.archive import MinerUArchive
from easylearn.mineru.schema import MinerUOptions


@pytest.fixture
def result_zip(tmp_path):
    """Synthetic metadata in the fixed upstream layout; the PDF is a real read-only sample."""

    def build(*, root="input/vlm", changes=None):
        files = {
            f"{root}/input.md": b"# Synthetic archive fixture",
            f"{root}/input_middle.json": b'{"pdf_info": []}',
            f"{root}/input_model.json": b"[]",
            f"{root}/input_content_list.json": b"[]",
            f"{root}/input_content_list_v2.json": b"[]",
            f"{root}/input_origin.pdf": Path(
                "3rdparty/MinerU/tests/unittest/pdfs/test.pdf"
            ).read_bytes(),
        }
        for name, value in (changes or {}).items():
            if value is None:
                files.pop(name)
            else:
                files[name] = value
        destination = tmp_path / "result.zip"
        with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return destination

    return build


@pytest.mark.parametrize(
    "backend, method, root",
    [
        ("pipeline", "auto", "input/auto"),
        ("pipeline", "txt", "input/txt"),
        ("vlm-engine", "auto", "input/vlm"),
        ("vlm-http-client", "auto", "input/vlm"),
        ("hybrid-engine", "ocr", "input/hybrid_ocr"),
        ("hybrid-http-client", "auto", "input/hybrid_auto"),
    ],
)
def test_archive_inspection_preserves_raw_artifacts_and_checksums_without_extracting(
    result_zip, backend, method, root
):
    path = result_zip(root=root)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = MinerUArchive().inspect(
        path,
        options=MinerUOptions(
            page_count=1,
            backend=backend,
            parse_method=method,
            server_url="http://inference.test" if backend.endswith("-http-client") else None,
        ),
    )
    assert manifest.sha256 == before
    assert manifest.size == path.stat().st_size
    by_kind = {member.kind: member for member in manifest.members}
    assert set(by_kind) == {
        "markdown",
        "middle",
        "model",
        "content_list",
        "content_list_v2",
        "original",
    }
    assert by_kind["middle"].path == root + "/input_middle.json"
    assert by_kind["original"].size == 125121
    assert (
        by_kind["original"].sha256
        == "ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "name",
    [
        "../escaped.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "input/vlm/../escaped.txt",
        "input\\vlm\\images\\figure.png",
        "input/vlm/images/CON.png",
        "input/vlm/images/figure.png.",
        "input/vlm/images/figure.png:stream",
        "input/vlm/images/nested/figure.png",
        "input/vlm/images/../figure.png",
        "input/vlm/images/figure.exe",
        "input/vlm/images/",
        "input/vlm/unknown.json",
        "other/vlm/input.md",
        "input/auto/input.md",
    ],
)
def test_archive_rejects_nonportable_names_and_files_outside_the_fixed_result_layout(
    result_zip, name
):
    path = result_zip(changes={name: b"untrusted"})
    if "\\" in name:
        path.write_bytes(path.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    with ZipFile(path) as archive:
        assert name in {member.orig_filename for member in archive.infolist()}
    with pytest.raises(DomainError, match="MINERU_RESULT_INVALID"):
        MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "missing",
    [
        "input.md",
        "input_middle.json",
        "input_model.json",
        "input_content_list.json",
        "input_content_list_v2.json",
        "input_origin.pdf",
    ],
)
def test_archive_must_contain_every_requested_artifact(result_zip, missing):
    path = result_zip(changes={f"input/vlm/{missing}": None})
    with pytest.raises(DomainError, match="MINERU_RESULT_INVALID"):
        MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))


@pytest.mark.parametrize("case", ["duplicate", "case_collision", "symlink", "device"])
def test_archive_rejects_ambiguous_names_and_non_regular_files(result_zip, case):
    path = result_zip()
    with ZipFile(path, "a") as archive:
        if case == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("input/vlm/input.md", b"duplicate")
        elif case == "case_collision":
            archive.writestr("input/vlm/images/figure.png", b"first")
            archive.writestr("input/vlm/images/FIGURE.PNG", b"second")
        else:
            info = ZipInfo("input/vlm/images/figure.png")
            info.create_system = 3
            info.external_attr = (0o120777 if case == "symlink" else 0o020600) << 16
            archive.writestr(info, b"../unrelated")
    with pytest.raises(DomainError, match="MINERU_RESULT_INVALID"):
        MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))


@pytest.mark.parametrize("case", ["not_zip", "truncated", "crc", "encrypted", "compression"])
def test_archive_rejects_corrupt_or_unsupported_zip_data_as_a_domain_error(result_zip, case):
    path = result_zip()
    if case == "not_zip":
        path.write_bytes(b"not a zip")
    elif case == "truncated":
        path.write_bytes(path.read_bytes()[:-30])
    else:
        content = bytearray(path.read_bytes())
        with ZipFile(path) as archive:
            central = content.index(b"PK\x01\x02")
            local = archive.infolist()[0].header_offset
        if case == "crc":
            content[central + 16] ^= 0xFF
            content[local + 14] ^= 0xFF
        elif case == "encrypted":
            content[central + 8] |= 1
            content[local + 6] |= 1
        else:
            content[central + 10 : central + 12] = (99).to_bytes(2, "little")
            content[local + 8 : local + 10] = (99).to_bytes(2, "little")
        path.write_bytes(content)
    with pytest.raises(DomainError, match="MINERU_RESULT_INVALID"):
        MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))


@pytest.mark.parametrize("limit", ["compressed", "expanded", "member", "count"])
def test_archive_inspection_enforces_configured_resource_limits(result_zip, limit):
    from easylearn.mineru.schema import MinerUArchiveLimits

    path = result_zip()
    limits = {
        "compressed": {"max_archive_bytes": 100},
        "expanded": {"max_expanded_bytes": 125121},
        "member": {"max_member_bytes": 100},
        "count": {"max_members": 5},
    }
    with pytest.raises(DomainError, match="MINERU_RESULT_TOO_LARGE"):
        MinerUArchive(limits=MinerUArchiveLimits(**limits[limit])).inspect(
            path, options=MinerUOptions(page_count=1)
        )


@pytest.mark.skipif(
    not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"), reason="Real user PDF not configured"
)
def test_real_paper_and_image_are_checksummed_in_a_synthetic_result_package(result_zip):
    source = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
    original = source.read_bytes()
    expected = "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
    assert hashlib.sha256(original).hexdigest() == expected
    image = BytesIO()
    with Image.new("RGB", (32, 32), color="white") as fixture:
        fixture.save(image, format="PNG")
    path = result_zip(
        changes={
            "input/vlm/input_origin.pdf": original,
            "input/vlm/images/figure.png": image.getvalue(),
        }
    )
    manifest = MinerUArchive().inspect(path, options=MinerUOptions(page_count=27))
    by_kind = {member.kind: member for member in manifest.members}
    assert by_kind["original"].sha256 == expected
    assert by_kind["original"].size == 2660025
    assert by_kind["image"].path == "input/vlm/images/figure.png"
    assert by_kind["image"].size == len(image.getvalue())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    assert list(path.parent.iterdir()) == [path]


def test_archive_image_metadata_comes_from_actual_decoding(result_zip):
    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as picture:
        picture.save(buffer, format="PNG")
        png = buffer.getvalue()
    path = result_zip(changes={"input/vlm/images/figure.png": png})
    manifest = MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))
    member = next(member for member in manifest.members if member.kind == "image")
    assert member.image.mime == "image/png"
    assert (member.image.width, member.image.height, member.image.frames) == (40, 20, 1)
    assert member.sha256 == hashlib.sha256(png).hexdigest()
    assert next(member for member in manifest.members if member.kind == "original").image is None


@pytest.mark.parametrize(
    "case, code",
    [
        ("wrong_suffix", "IMAGE_FORMAT_MISMATCH"),
        ("missing_end", "IMAGE_INVALID"),
    ],
)
def test_archive_cannot_publish_disguised_or_incomplete_images(result_zip, case, code):
    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as picture:
        picture.save(buffer, format="JPEG" if case == "wrong_suffix" else "PNG")
        content = buffer.getvalue()
    if case == "missing_end":
        content = content[:-12]
    path = result_zip(changes={"input/vlm/images/figure.png": content})
    with pytest.raises(DomainError, match=code):
        MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))


@pytest.mark.parametrize("case", ["pixels", "frames", "archive_total"])
def test_archive_bounds_decoded_image_resources_not_just_compressed_bytes(result_zip, case):
    from easylearn.images import ImageLimits

    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as first:
        if case == "frames":
            with Image.new("RGB", (40, 20), "black") as second:
                first.save(buffer, format="GIF", save_all=True, append_images=[second])
        else:
            first.save(buffer, format="PNG")
        content = buffer.getvalue()
    changes = {
        "input/vlm/images/figure.gif"
        if case == "frames"
        else "input/vlm/images/figure.png": content
    }
    if case == "archive_total":
        changes["input/vlm/images/second.png"] = content
    policy = {
        "pixels": {"max_pixels": 799},
        "frames": {"max_frames": 1},
        "archive_total": {"max_total_pixels": 1200},
    }
    path = result_zip(changes=changes)
    with pytest.raises(DomainError, match="IMAGE_LIMIT"):
        MinerUArchive(image_limits=ImageLimits(**policy[case])).inspect(
            path, options=MinerUOptions(page_count=1)
        )


@pytest.mark.parametrize("budget, accepted", [(2400, True), (2399, False)])
def test_archive_counts_each_frame_at_its_actual_dimensions(result_zip, budget, accepted):
    from easylearn.images import ImageLimits

    with (
        BytesIO() as buffer,
        Image.new("RGB", (40, 20), "white") as first,
        Image.new("RGB", (40, 40), "black") as second,
    ):
        first.save(buffer, format="TIFF", save_all=True, append_images=[second])
        content = buffer.getvalue()
    path = result_zip(changes={"input/vlm/images/figure.tiff": content})
    checker = MinerUArchive(image_limits=ImageLimits(max_frames=2, max_total_pixels=budget))
    if not accepted:
        with pytest.raises(DomainError, match="IMAGE_LIMIT"):
            checker.inspect(path, options=MinerUOptions(page_count=1))
        return
    manifest = checker.inspect(path, options=MinerUOptions(page_count=1))
    metadata = next(member.image for member in manifest.members if member.kind == "image")
    assert (metadata.width, metadata.height, metadata.frames) == (40, 20, 2)
    assert metadata.decoded_pixels == 2400


@pytest.mark.parametrize(
    "format, suffix, mime",
    [
        ("PNG", ".png", "image/png"),
        ("JPEG", ".jpeg", "image/jpeg"),
        ("JPEG", ".JPG", "image/jpeg"),
        ("JPEG2000", ".jp2", "image/jp2"),
        ("WEBP", ".webp", "image/webp"),
        ("GIF", ".gif", "image/gif"),
        ("BMP", ".bmp", "image/bmp"),
        ("TIFF", ".tiff", "image/tiff"),
    ],
)
def test_archive_decodes_supported_raster_formats(result_zip, format, suffix, mime):
    with BytesIO() as buffer, Image.new("RGB", (40, 20), "white") as picture:
        picture.save(buffer, format=format)
        content = buffer.getvalue()
    path = result_zip(changes={f"input/vlm/images/figure{suffix}": content})
    manifest = MinerUArchive().inspect(path, options=MinerUOptions(page_count=1))
    metadata = next(member.image for member in manifest.members if member.kind == "image")
    assert metadata.mime == mime
    assert metadata.format == format
    assert (metadata.width, metadata.height, metadata.frames) == (40, 20, 1)
    assert metadata.decoded_pixels == 800
