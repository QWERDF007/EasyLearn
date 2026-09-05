"""Real ZIP/storage/decoder/subprocess; payloads are synthetic, not MinerU captures."""

import asyncio
import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from easylearn.errors import DomainError
from easylearn.images import ImageLimits
from easylearn.mineru.schema import MinerUArchiveLimits, MinerUOptions
from easylearn.previews.schema import PreviewLimits
from easylearn.storage import LocalStorage


@pytest.fixture
def result_input(tmp_path):
    storage = LocalStorage(tmp_path / "storage")
    pdf = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
    with BytesIO() as image, Image.new("RGB", (40, 20), "white") as picture:
        picture.save(image, format="PNG")
        png = image.getvalue()
    files = {
        "input/vlm/input.md": "# 合成示例".encode(),
        "input/vlm/input_middle.json": json.dumps(
            {
                "_version_name": "3.4.5",
                "_backend": "vlm",
                "pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": []}],
            }
        ).encode(),
        "input/vlm/input_model.json": b"[]",
        "input/vlm/input_content_list.json": b"[]",
        "input/vlm/input_content_list_v2.json": b"[]",
        "input/vlm/input_origin.pdf": pdf,
        "input/vlm/images/figure.png": png,
    }

    def build(changes=None, *, root="input/vlm"):
        contents = {
            name.replace("input/vlm", root, 1): data
            for name, data in (files | (changes or {})).items()
        }
        with BytesIO() as buffer:
            with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            stored = storage.write([buffer.getvalue()])
        return storage, stored, contents

    return build


@pytest.mark.asyncio
async def test_result_validation_returns_readable_verified_objects_and_pdf_evidence(result_input):
    from easylearn.mineru.result import MinerUResultValidator

    storage, archive, contents = result_input()
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    result = await validator.inspect(archive, options=MinerUOptions(page_count=1))
    assert result.manifest.sha256 == archive.sha256
    assert (
        result.origin.sha256 == "ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b"
    )
    assert len(result.origin.pages) == 1
    assert result.origin.pages[0].crop_box == (0, 0, 612, 792)
    assert set(result.objects) == set(contents)
    for member in result.manifest.members:
        stored = result.objects[member.path]
        assert (stored.sha256, stored.size) == (member.sha256, member.size)
        assert b"".join(storage.read(stored.key)) == contents[member.path]
    image = next(member for member in result.manifest.members if member.kind == "image")
    assert (image.image.width, image.image.height, image.image.decoded_pixels) == (40, 20, 800)
    assert hashlib.sha256(b"".join(storage.read(archive.key))).hexdigest() == archive.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["middle", "model", "content_list", "content_list_v2"])
@pytest.mark.parametrize("payload", [b"{", b"[NaN]", b'{"x":1,"x":2}', b'"\\ud800"'])
async def test_result_rejects_invalid_or_ambiguous_json_artifacts(result_input, name, payload):
    from easylearn.mineru.result import MinerUResultValidator

    storage, archive, _ = result_input({f"input/vlm/input_{name}.json": payload})
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    with pytest.raises(DomainError, match="MINERU_RESULT_JSON_INVALID"):
        await validator.inspect(archive, options=MinerUOptions(page_count=1))


@pytest.mark.asyncio
async def test_result_rejects_origin_page_count_different_from_submitted_input(result_input):
    from easylearn.mineru.result import MinerUResultValidator

    storage, archive, _ = result_input()
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    with pytest.raises(DomainError, match="MINERU_PROTOCOL_MISMATCH"):
        await validator.inspect(archive, options=MinerUOptions(page_count=2))


@pytest.mark.asyncio
async def test_result_enforces_json_byte_limit_before_materializing_artifacts(result_input):
    from easylearn.mineru.result import MinerUResultValidator

    storage, archive, _ = result_input()
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(max_json_bytes=2),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    with pytest.raises(DomainError, match="MINERU_RESULT_TOO_LARGE"):
        await validator.inspect(archive, options=MinerUOptions(page_count=1))
    assert list((storage.root / "objects").iterdir()) == [storage.path(archive.key)]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["starting", "working", "flooding"])
async def test_result_cancellation_reaps_child_even_during_startup(
    result_input, monkeypatch, phase
):
    from easylearn.mineru.result import MinerUResultValidator

    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release_spawn = asyncio.Event()
    children = []

    async def slow_child(*args, **kwargs):
        command = "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"
        if phase == "flooding":
            command = (
                "import sys,time; sys.stdin.buffer.read(); "
                "sys.stdout.write('x'*1048576); sys.stdout.flush(); time.sleep(30)"
            )
        child = await spawn(
            sys.executable,
            "-c",
            command,
            **kwargs,
        )
        children.append(child)
        started.set()
        if phase == "starting":
            await release_spawn.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_child)
    storage, archive, _ = result_input()
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    task = asyncio.create_task(validator.inspect(archive, options=MinerUOptions(page_count=1)))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if phase == "flooding":
            await asyncio.sleep(0.2)
        task.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        assert children[0].returncode is not None
    finally:
        release_spawn.set()
        task.cancel()
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, code",
    [
        ("timeout", "MINERU_RESULT_TIMEOUT"),
        ("crash", "MINERU_RESULT_PROCESS_FAILED"),
        ("protocol", "MINERU_RESULT_PROTOCOL_INVALID"),
    ],
)
async def test_result_child_failures_do_not_return_artifact_evidence(
    result_input, monkeypatch, mode, code
):
    from easylearn.mineru.result import MinerUResultValidator

    spawn = asyncio.create_subprocess_exec
    children = []
    commands = {
        "timeout": "import sys,time; sys.stdin.buffer.read(); time.sleep(30)",
        "crash": "import sys; sys.stdin.buffer.read(); sys.exit(2)",
        "protocol": "import sys; sys.stdin.buffer.read(); print('not json')",
    }

    async def failed_child(*args, **kwargs):
        child = await spawn(sys.executable, "-c", commands[mode], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_child)
    storage, archive, _ = result_input()
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
        timeout=0.5 if mode == "timeout" else 5,
    )
    with pytest.raises(DomainError, match=code) as error:
        await validator.inspect(archive, options=MinerUOptions(page_count=1))
    assert error.value.retryable is (mode != "protocol")
    assert children[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, code",
    [
        ("pdf", "MINERU_RESULT_INVALID"),
        ("image", "IMAGE_INVALID"),
        ("checksum", "STORAGE_CORRUPT"),
        ("size", "STORAGE_CORRUPT"),
        ("image_budget", "IMAGE_LIMIT"),
        ("page_budget", "PREVIEW_PAGE_SIZE_LIMIT"),
    ],
)
async def test_result_checks_actual_artifacts_and_injected_limits(result_input, kind, code):
    from dataclasses import replace

    from easylearn.mineru.result import MinerUResultValidator

    changes = {}
    if kind == "pdf":
        changes["input/vlm/input_origin.pdf"] = b"not PDF"
    elif kind == "image":
        changes["input/vlm/images/figure.png"] = b"not image"
    storage, archive, _ = result_input(changes)
    if kind == "checksum":
        archive = replace(archive, sha256="0" * 64)
    elif kind == "size":
        archive = replace(archive, size=archive.size + 1)
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(max_pixels=799) if kind == "image_budget" else ImageLimits(),
        preview_limits=PreviewLimits(max_page_points=100)
        if kind == "page_budget"
        else PreviewLimits(),
    )
    with pytest.raises(DomainError, match=code) as error:
        await validator.inspect(archive, options=MinerUOptions(page_count=1))
    assert error.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"), reason="Real user PDF not configured"
)
async def test_real_paper_result_evidence_is_replayable_without_mutating_original(result_input):
    from easylearn.mineru.result import MinerUResultValidator

    path = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
    pdf = path.read_bytes()
    expected = "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
    assert hashlib.sha256(pdf).hexdigest() == expected
    middle = {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {"page_idx": index, "page_size": [600, 800], "para_blocks": []} for index in range(27)
        ],
    }
    storage, archive, _ = result_input(
        {
            "input/vlm/input_origin.pdf": pdf,
            "input/vlm/input_middle.json": json.dumps(middle).encode(),
        }
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    first = await validator.inspect(archive, options=MinerUOptions(page_count=27))
    second = await validator.inspect(archive, options=MinerUOptions(page_count=27))
    assert first == second
    assert len(first.origin.pages) == 27
    assert first.origin.sha256 == expected
    original = first.objects["input/vlm/input_origin.pdf"]
    assert b"".join(storage.read(original.key)) == pdf
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case, code",
    [
        ("valid", None),
        ("version", "MINERU_PROTOCOL_MISMATCH"),
        ("page_size", "ADAPTER_COORDINATE_INVALID"),
        ("missing_asset", "MINERU_ASSET_INVALID"),
        ("table_limit", "MINERU_TABLE_LIMIT"),
    ],
)
async def test_normalized_result_binds_actual_preview_geometry_and_decoded_assets(
    result_input, case, code
):
    from easylearn.document_ir.schema import DocumentIR
    from easylearn.mineru.result import MinerUResultValidator, ParseSource
    from easylearn.mineru.schema import MinerUTableLimits

    middle = {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612, 792],
                "para_blocks": [
                    {
                        "type": "interline_equation",
                        "bbox": [10, 20, 50, 40],
                        "lines": [
                            {
                                "bbox": [10, 20, 50, 40],
                                "spans": [
                                    {
                                        "type": "interline_equation",
                                        "content": "x^2",
                                        "image_path": "figure.png",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    if case == "version":
        middle["_version_name"] = "0.0.0"
    elif case == "page_size":
        middle["pdf_info"][0]["page_size"] = [600, 800]
    elif case == "missing_asset":
        middle["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["image_path"] = (
            "missing.png"
        )
    elif case == "table_limit":
        middle["pdf_info"][0]["para_blocks"] = [
            {
                "type": "table",
                "blocks": [
                    {
                        "type": "table_body",
                        "lines": [
                            {
                                "spans": [
                                    {
                                        "type": "table",
                                        "html": "<table><tr><td>A</td><td>B</td></tr></table>",
                                    }
                                ]
                            }
                        ],
                    }
                ],
            }
        ]
    storage, archive, contents = result_input(
        {
            "input/vlm/input_middle.json": json.dumps(middle).encode(),
        }
    )
    preview = storage.write([contents["input/vlm/input_origin.pdf"]])
    source = ParseSource(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=preview,
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
        table_limits=MinerUTableLimits(max_cells=1),
    )
    if code is not None:
        with pytest.raises(DomainError, match=code):
            await validator.normalize(archive, options=MinerUOptions(page_count=1), source=source)
        return
    result = await validator.normalize(archive, options=MinerUOptions(page_count=1), source=source)
    ir = DocumentIR.model_validate_json(b"".join(storage.read(result.document_ir.key)))
    assert ir.document_id == source.document_id
    assert ir.parse_run_id == source.parse_run_id
    assert ir.preview_asset_id == source.preview_asset_id
    assert ir.preview_sha256 == preview.sha256
    assert ir.blocks[0].source_regions[0].bbox_pdf == (10, 752, 50, 772)
    assert ir.assets[0].mime == "image/png"
    assert ir.assets[0].sha256 == result.objects["input/vlm/images/figure.png"].sha256
    assert ir.assets[0].export_path == "images/figure.png"
    repeated = await validator.normalize(
        archive, options=MinerUOptions(page_count=1), source=source
    )
    assert repeated == result


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["metadata", "page_content"])
async def test_registration_checks_page_content_when_origin_pdf_bytes_are_rewritten(
    result_input, change
):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from easylearn.mineru.result import MinerUResultValidator, ParseSource

    pdf = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
    writer = PdfWriter(BytesIO(pdf))
    writer.add_metadata({"/Subject": "Synthetic upstream rewrite"})
    if change == "page_content":
        writer.pages[0][NameObject("/Contents")] = DecodedStreamObject()
    with BytesIO() as buffer:
        writer.write(buffer)
        rewritten = buffer.getvalue()
    assert hashlib.sha256(rewritten).digest() != hashlib.sha256(pdf).digest()
    storage, archive, _ = result_input({"input/vlm/input_origin.pdf": rewritten})
    source = ParseSource(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=storage.write([pdf]),
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    if change == "page_content":
        with pytest.raises(DomainError, match="MINERU_PREVIEW_MISMATCH"):
            await validator.normalize(archive, options=MinerUOptions(page_count=1), source=source)
    else:
        result = await validator.normalize(
            archive, options=MinerUOptions(page_count=1), source=source
        )
        assert result.registration.method == "rendered_pages"
        assert len(result.registration.page_render_sha256) == 1
        assert result.registration.preview.sha256 == source.preview.sha256
        assert result.registration.origin_sha256 != source.preview.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rotation, size, expected",
    [
        (0, [600, 760], (20, 740, 60, 760)),
        (90, [760, 600], (30, 30, 50, 70)),
        (180, [600, 760], (560, 40, 600, 60)),
        (270, [760, 600], (570, 730, 590, 770)),
    ],
)
async def test_normalization_uses_real_pdf_rotation_and_nonzero_cropbox(
    result_input, rotation, size, expected
):
    from pypdf import PdfWriter

    from easylearn.document_ir.schema import DocumentIR
    from easylearn.mineru.result import MinerUResultValidator, ParseSource

    writer = PdfWriter("3rdparty/MinerU/tests/unittest/pdfs/test.pdf")
    writer.pages[0].cropbox.lower_left = (10, 20)
    writer.pages[0].cropbox.upper_right = (610, 780)
    writer.pages[0].rotate(rotation)
    with BytesIO() as buffer:
        writer.write(buffer)
        pdf = buffer.getvalue()
    middle = {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": size,
                "para_blocks": [
                    {
                        "type": "text",
                        "lines": [
                            {
                                "bbox": [10, 20, 50, 40],
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": "Synthetic rotated region",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    storage, archive, _ = result_input(
        {
            "input/vlm/input_origin.pdf": pdf,
            "input/vlm/input_middle.json": json.dumps(middle).encode(),
        }
    )
    source = ParseSource(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=storage.write([pdf]),
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    result = await validator.normalize(archive, options=MinerUOptions(page_count=1), source=source)
    ir = DocumentIR.model_validate_json(b"".join(storage.read(result.document_ir.key)))
    assert ir.blocks[0].source_regions[0].bbox_pdf == pytest.approx(expected)
    assert ir.pages[0].crop_box == (10, 20, 610, 780)
    assert ir.pages[0].intrinsic_rotation == rotation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend, root, expected",
    [
        ("vlm-engine", "input/vlm", (9.007352941, 756.238636364, 45.036764706, 774.244318182)),
        (
            "hybrid-engine",
            "input/hybrid_auto",
            (9.007352941, 756.238636364, 45.036764706, 774.244318182),
        ),
        ("pipeline", "input/auto", (8.996768508, 756.254997728, 44.983842538, 774.252498864)),
    ],
)
@pytest.mark.parametrize("large", [False, True])
async def test_pages_follow_each_backend_coordinate_contract(
    result_input, backend, root, expected, large
):
    from pypdf import PdfWriter

    from easylearn.document_ir.schema import DocumentIR
    from easylearn.mineru.result import MinerUResultValidator, ParseSource

    writer = PdfWriter("3rdparty/MinerU/tests/unittest/pdfs/test.pdf")
    dimensions = (2000, 3000) if large else (612.5, 792.25)
    writer.pages[0].mediabox.upper_right = dimensions
    writer.pages[0].cropbox.upper_right = dimensions
    if large:
        # Capped pipeline raster: 2334x3500 at scale 7/6.
        expected = (
            (8.997429306, 2964, 44.987146530, 2982)
            if backend == "pipeline"
            else (9, 2964, 45, 2982)
        )
    with BytesIO() as buffer:
        writer.write(buffer)
        pdf = buffer.getvalue()
    # VLM/hybrid use the truncated 612x792 frame; pipeline uses 200dpi 1702x2201 pixels.
    middle = {
        "_version_name": "3.4.5",
        "_backend": backend.split("-")[0],
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [2000, 3000] if large else [612, 792],
                "para_blocks": [
                    {
                        "type": "text",
                        "lines": [
                            {
                                "bbox": [9, 18, 45, 36],
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": "Fractional coordinate example",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    storage, archive, _ = result_input(
        {
            "input/vlm/input_origin.pdf": pdf,
            "input/vlm/input_middle.json": json.dumps(middle).encode(),
        },
        root=root,
    )
    source = ParseSource(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=storage.write([pdf]),
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    result = await validator.normalize(
        archive, options=MinerUOptions(page_count=1, backend=backend), source=source
    )
    ir = DocumentIR.model_validate_json(b"".join(storage.read(result.document_ir.key)))
    assert ir.blocks[0].source_regions[0].bbox_pdf == pytest.approx(expected, abs=1e-6)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"), reason="Real user PDF not configured"
)
async def test_real_paper_can_be_normalized_against_its_fixed_preview(result_input):
    import pypdfium2 as pdfium

    from easylearn.document_ir.schema import DocumentIR
    from easylearn.mineru.result import MinerUResultValidator, ParseSource

    path = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
    pdf = path.read_bytes()
    expected = "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
    assert hashlib.sha256(pdf).hexdigest() == expected
    with pdfium.PdfDocument(path) as document:
        sizes = [list(map(int, document.get_page_size(index))) for index in range(len(document))]
    middle = {
        "_version_name": "3.4.5",
        "_backend": "vlm",
        "pdf_info": [
            {
                "page_idx": index,
                "page_size": size,
                "para_blocks": [
                    {
                        "type": "text",
                        "lines": [
                            {
                                "bbox": [10, 20, 50, 40],
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": f"Synthetic page {index}",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            for index, size in enumerate(sizes)
        ],
    }
    storage, archive, _ = result_input(
        {
            "input/vlm/input_origin.pdf": pdf,
            "input/vlm/input_middle.json": json.dumps(middle).encode(),
        }
    )
    source = ParseSource(
        document_id=UUID(int=1),
        parse_run_id=UUID(int=2),
        preview_asset_id=UUID(int=3),
        preview=storage.write([pdf]),
    )
    validator = MinerUResultValidator(
        storage,
        archive_limits=MinerUArchiveLimits(),
        image_limits=ImageLimits(),
        preview_limits=PreviewLimits(),
    )
    result = await validator.normalize(archive, options=MinerUOptions(page_count=27), source=source)
    ir = DocumentIR.model_validate_json(b"".join(storage.read(result.document_ir.key)))
    assert (ir.page_count, len(ir.blocks)) == (27, 27)
    assert ir.preview_sha256 == expected
    assert ir.blocks[0].source_regions[0].bbox_pdf == (10, 752, 50, 772)
    assert [block.source_regions[0].page_index for block in ir.blocks] == list(range(27))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
