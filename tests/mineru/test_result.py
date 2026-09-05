"""Real ZIP/storage/decoder/subprocess; payloads are synthetic, not MinerU captures."""

import asyncio
import hashlib
import json
import os
import sys
from io import BytesIO
from pathlib import Path
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

    def build(changes=None):
        contents = files | (changes or {})
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
