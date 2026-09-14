from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import threading
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.requests import Request

from easylearn.config import (
    AppSettings,
    ExtensionSettings,
    FileSettings,
    LLMProviderSettings,
    LLMSettings,
    Settings,
)
from easylearn.document_ir.schema import (
    AssetDescriptor,
    Block,
    DocumentIR,
    ImageNode,
    PageGeometry,
    TextNode,
)
from easylearn.errors import DomainError
from easylearn.exports import ExportRequest
from easylearn.jobs.schema import JobKind
from easylearn.main import create_app
from easylearn.qa import QARequest
from easylearn.translation import LLMClient, TranslateRequest


def _parse_test_input(content: str) -> dict[str, str]:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except Exception:
            pass
    anchors = re.findall(r"\[§(\d+)\]\s*([\s\S]*?)(?=(?:\[§\d+\]|\Z))", content)
    if anchors:
        return {f"[§{idx}]": text.strip() for idx, text in anchors}
    return {"[§1]": stripped}


class FakeLLM:
    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        content = messages[-1]["content"]
        if content.strip().startswith("{") and content.strip().endswith("}"):
            payload = json.loads(content)
            return json.dumps(
                {unit_id: f"译：{text}" for unit_id, text in payload.items()}, ensure_ascii=False
            )
        payload = _parse_test_input(content)
        return "\n\n".join(f"{key} 译：{text}" for key, text in payload.items())

    async def stream(self, messages: list[dict[str, str]]):
        del messages
        for chunk in ("该值是 42%。", " [^1]"):
            await asyncio.sleep(0)
            yield chunk


class InvalidCitationLLM(FakeLLM):
    async def stream(self, messages: list[dict[str, str]]):
        del messages
        yield "依据不足 [^99]"


class BlockingTranslationLLM(FakeLLM):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        self.started.set()
        await self.release.wait()
        return await super().complete_json(messages)


class DuplicateKeyTranslationLLM(FakeLLM):
    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        payload = _parse_test_input(messages[-1]["content"])
        unit_id = next(iter(payload))
        return json.dumps({unit_id: "first 42%"})[:-1] + f',"{unit_id}":"second 42%"}}'


class DropKeyOnceTranslationLLM(FakeLLM):
    def __init__(self):
        self.call_count = 0
        self.requested_keys = []

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        self.call_count += 1
        payload = _parse_test_input(messages[-1]["content"])
        self.requested_keys.append(list(payload.keys()))
        items = list(payload.items())
        if self.call_count == 1 and len(items) > 1:
            items = items[:-1]
        return "\n\n".join(f"{k} 译：{text}" for k, text in items)


class WhitespaceKeyTranslationLLM(FakeLLM):
    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        payload = _parse_test_input(messages[-1]["content"])
        return "\n\n".join(
            f"  {unit_id}  \n 译：{text}" for unit_id, text in payload.items()
        )


class FailSecondBatchThenResumeLLM(FakeLLM):
    def __init__(self):
        self.call_history: list[list[str]] = []
        self.should_fail_b2 = True

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        payload = _parse_test_input(messages[-1]["content"])
        self.call_history.append(list(payload.values()))
        if any("explanation" in text for text in payload.values()) and self.should_fail_b2:
            await asyncio.sleep(0.05)
            raise DomainError("LLM_UNAVAILABLE", "Transient network drop", retryable=True)
        return await super().complete_json(messages)


class MissingPlaceholderTranslationLLM(FakeLLM):
    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        payload = _parse_test_input(messages[-1]["content"])
        return "\n\n".join(f"{unit_id} 缺少占位符的译文" for unit_id in payload)


class ConcurrentBatchTrackingLLM(FakeLLM):
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def complete_json(self, messages: list[dict[str, str]]) -> str:
        async with self.lock:
            self.active += 1
            if self.active > self.max_active:
                self.max_active = self.active
        try:
            await asyncio.sleep(0.05)
            return await super().complete_json(messages)
        finally:
            async with self.lock:
                self.active -= 1


class CapturingQALLM(FakeLLM):
    def __init__(self):
        self.messages = []

    async def stream(self, messages: list[dict[str, str]]):
        self.messages.append(messages)
        yield "依据 [1]"


class BlockingQALLM(FakeLLM):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def stream(self, messages: list[dict[str, str]]):
        del messages
        self.calls += 1
        self.started.set()
        await self.release.wait()
        yield "依据 [1]"


class PartialBlockingQALLM(FakeLLM):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def stream(self, messages: list[dict[str, str]]):
        del messages
        self.calls += 1
        self.started.set()
        yield "部分答案 [1]"
        await self.release.wait()


@pytest_asyncio.fixture
async def feature_context(tmp_path: Path):
    settings = Settings(
        app=AppSettings(data_dir=tmp_path),
        files=FileSettings(revision_history_limit=2),
        extensions=ExtensionSettings(qa_enabled=True),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.services.translation.llm = FakeLLM()
        app.state.services.qa.llm = FakeLLM()
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()
        created = await client.post(
            "/api/documents", files={"file": ("paper.pdf", source, "application/pdf")}
        )
        document_id = UUID(created.json()["document_id"])
        parse_id = uuid4()
        parse_directory = app.state.services.files.paths.parse(document_id, parse_id)
        parse_directory.mkdir(parents=True)
        original = await app.state.services.documents.file_path(document_id, "original")
        shutil.copyfile(original, parse_directory / "preview.pdf")
        ir = DocumentIR(
            document_id=document_id,
            parse_run_id=parse_id,
            preview_asset_id=uuid4(),
            preview_sha256="0" * 64,
            mineru_version="3.4.5",
            adapter_version="3.0.0",
            pages=(
                PageGeometry(page_index=0, media_box=(0, 0, 600, 800), crop_box=(0, 0, 600, 800)),
            ),
            blocks=(
                Block(
                    block_id="b1",
                    block_type="paragraph",
                    order_index=0,
                    source_nodes=(TextNode(node_id="n1", text="The value is 42%."),),
                ),
                Block(
                    block_id="b2",
                    block_type="paragraph",
                    order_index=1,
                    source_nodes=(TextNode(node_id="n1", text="A nearby explanation."),),
                ),
            ),
        )
        (parse_directory / "document.json").write_bytes(
            ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
        )
        async with app.state.services.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO parse_results "
                "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, "
                "created_at) "
                "VALUES (?, ?, ?, ?, NULL, 1, '{}', '2026-01-01T00:00:00+00:00')",
                (
                    str(parse_id),
                    str(document_id),
                    f"parses/{parse_id}/preview.pdf",
                    f"parses/{parse_id}/document.json",
                ),
            )
            await connection.execute(
                "UPDATE documents SET active_parse_id = ? WHERE id = ?",
                (str(parse_id), str(document_id)),
            )
        yield client, document_id, parse_id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("parse", "export", "translate", "qa"))
async def test_task_submission_revalidates_its_source_under_task_lock(
    feature_context, operation, monkeypatch
):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    services = application.state.services
    service = {
        "parse": services.parser,
        "export": services.exporter,
        "translate": services.translation,
        "qa": services.qa,
    }[operation]
    method_name = "get" if operation == "parse" else "load_ir"
    kind = {
        "parse": JobKind.PARSE,
        "export": JobKind.EXPORT,
        "translate": JobKind.TRANSLATE,
        "qa": JobKind.QA,
    }[operation]
    original = getattr(service.documents, method_name)
    entered = asyncio.Event()
    release = asyncio.Event()
    executor_release = asyncio.Event()
    calls = 0

    async def guarded(*args):
        nonlocal calls
        result = await original(*args)
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(service.documents, method_name, guarded)
    async def hold_executor(record, context):
        del record, context
        await executor_release.wait()

    services.tasks._executors[kind] = hold_executor
    request = {
        "parse": None,
        "export": ExportRequest(parse_id=parse_id),
        "translate": TranslateRequest(parse_id=parse_id, block_ids=("b1",)),
        "qa": QARequest(parse_id=parse_id, question="What is the value?", block_ids=("b1",)),
    }[operation]
    submission = asyncio.create_task(
        service.submit(document_id)
        if operation == "parse"
        else service.submit(document_id, request)
    )
    accepted = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        exclusive = asyncio.create_task(services.tasks.run_exclusive(lambda: asyncio.sleep(0)))
        await asyncio.sleep(0)
        assert not exclusive.done()
        release.set()
        accepted = await submission
        await exclusive
        assert calls == 2
    finally:
        release.set()
        if not submission.done():
            await submission
        if accepted is not None:
            await services.tasks.cancel(accepted.task_id)
        executor_release.set()


@pytest.mark.asyncio
async def test_pruning_before_admission_rejects_an_export_of_the_deleted_parse(
    feature_context, monkeypatch
):
    client, document_id, current_parse_id = feature_context
    application = client._transport.app
    services = application.state.services
    source_directory = services.files.paths.parse(document_id, current_parse_id)
    old_parse_id, middle_parse_id = uuid4(), uuid4()
    for parse_id in (old_parse_id, middle_parse_id):
        destination = services.files.paths.parse(document_id, parse_id)
        shutil.copytree(source_directory, destination)
        copied_ir = DocumentIR.model_validate_json((destination / "document.json").read_bytes())
        (destination / "document.json").write_bytes(
            copied_ir.model_copy(update={"parse_run_id": parse_id})
            .model_dump_json(exclude_computed_fields=True)
            .encode("utf-8")
        )
    async with services.database.transaction() as connection:
        await connection.execute(
            "UPDATE parse_results SET created_at = ? WHERE id = ?",
            ("2026-01-03T00:00:00+00:00", str(current_parse_id)),
        )
        for parse_id, created_at in (
            (middle_parse_id, "2026-01-02T00:00:00+00:00"),
            (old_parse_id, "2026-01-01T00:00:00+00:00"),
        ):
            await connection.execute(
                "INSERT INTO parse_results "
                "(id, document_id, preview_path, ir_path, raw_path, pages, "
                "metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, NULL, 1, '{}', ?)",
                (
                    str(parse_id),
                    str(document_id),
                    f"parses/{parse_id}/preview.pdf",
                    f"parses/{parse_id}/document.json",
                    created_at,
                ),
            )

    service = services.exporter
    original = service.documents.load_ir
    entered = asyncio.Event()
    release = asyncio.Event()

    async def guarded(document_id, parse_id):
        result = await original(document_id, parse_id)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(service.documents, "load_ir", guarded)
    submission = asyncio.create_task(
        service.submit(document_id, ExportRequest(parse_id=old_parse_id))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await services.parser._prune(document_id)
        assert not services.files.paths.parse(document_id, old_parse_id).exists()
        release.set()
        with pytest.raises(DomainError, match="Parse result not found"):
            await submission
    finally:
        release.set()
        if not submission.done():
            await submission


async def wait_for_task(client: AsyncClient, task_id: str) -> dict:
    async with asyncio.timeout(5):
        while True:
            task = (await client.get(f"/api/tasks/{task_id}")).json()
            if task["status"] in {"succeeded", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_translation_edit_history_and_markdown(feature_context):
    client, document_id, parse_id = feature_context
    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task

    units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    assert units[0]["effective_text"].startswith("译：")
    unit_id = units[0]["unit_id"]

    edited = await client.patch(
        f"/api/documents/{document_id}/translations/{unit_id}",
        json={
            "parse_id": str(parse_id),
            "block_id": "b1",
            "expected_revision": units[0]["revision"],
            "text": "人工译文 42%。",
            "locked": True,
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["effective_text"] == "人工译文 42%。"

    stale = await client.patch(
        f"/api/documents/{document_id}/translations/{unit_id}",
        json={
            "parse_id": str(parse_id),
            "block_id": "b1",
            "expected_revision": 0,
            "text": "过期",
        },
    )
    assert stale.status_code == 409
    history = await client.get(
        f"/api/documents/{document_id}/translations/{unit_id}/history?parse_id={parse_id}"
    )
    assert history.status_code == 200
    assert len(history.json()) <= 2
    locked_restore = await client.post(
        f"/api/documents/{document_id}/translations/{unit_id}/history/{history.json()[0]['id']}/restore",
        json={
            "parse_id": str(parse_id),
            "block_id": "b1",
            "expected_revision": edited.json()["revision"],
        },
    )
    assert locked_restore.status_code == 409
    assert locked_restore.json()["code"] == "TRANSLATION_LOCKED"
    markdown = await client.get(
        f"/api/documents/{document_id}/parses/{parse_id}/markdown?language=zh"
    )
    assert markdown.status_code == 200
    assert "人工译文" in markdown.text


@pytest.mark.asyncio
async def test_first_manual_translation_edit_records_the_previous_source_text(feature_context):
    client, document_id, parse_id = feature_context
    units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    unit = units[0]
    assert unit["revision"] == 0
    assert unit["auto_text"] is None

    edited = await client.patch(
        f"/api/documents/{document_id}/translations/{unit['unit_id']}",
        json={
            "parse_id": str(parse_id),
            "block_id": unit["block_id"],
            "expected_revision": 0,
            "text": "首次人工译文",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 1

    history = await client.get(
        f"/api/documents/{document_id}/translations/{unit['unit_id']}/history?parse_id={parse_id}"
    )
    assert history.status_code == 200
    assert history.json()[0]["text"] == "The value is 42%."
    assert history.json()[0]["origin"] == "manual"


@pytest.mark.asyncio
async def test_translation_rejects_duplicate_json_ids(feature_context):
    client, document_id, parse_id = feature_context
    client._transport.app.state.services.translation.llm = DuplicateKeyTranslationLLM()
    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "failed", task
    assert task["failure"]["code"] == "TRANSLATION_PROTOCOL_INVALID"


def test_protected_tokens_normalizes_urls_and_preserves_placeholders():
    from easylearn.translation import _protected_tokens

    source = (
        "Available at https://github.com/org/repo. Appendix A.1 with {{IMG_0}} "
        "and (https://site.com/doc,). "
        "Ross Wightman. Pytorch image models. "
        "https://github.com/rwightman/pytorch-image-models, 2019."
    )
    translated = (
        "可在 https://github.com/org/repo 获取。附录 A.1 包含 {{IMG_0}} "
        "以及 (https://site.com/doc，)。"
        "Ross Wightman。Pytorch 图像模型。https://github.com/rwightman/pytorch-image-models，2019年。"
    )
    assert _protected_tokens(source) == _protected_tokens(translated)


@pytest.mark.asyncio
async def test_translation_rejects_mismatched_placeholders(feature_context):
    client, document_id, parse_id = feature_context
    # Replace block b1 source with one having a placeholder
    services = client._transport.app.state.services
    ir = await services.documents.load_ir(document_id, parse_id)
    new_blocks = list(ir.blocks)
    new_blocks[0] = Block(
        block_id="b1",
        block_type="paragraph",
        order_index=0,
        source_nodes=(TextNode(node_id="n1", text="See figure {{IMG_0}}."),),
    )
    ir = ir.model_copy(update={"blocks": tuple(new_blocks)})
    if services.documents.cache is not None:
        services.documents.cache.put(document_id, parse_id, ir)
    parse_directory = services.files.paths.parse(document_id, parse_id)
    (parse_directory / "document.json").write_bytes(
        ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
    )

    services.translation.llm = MissingPlaceholderTranslationLLM()
    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "failed", task
    assert task["failure"]["code"] == "TRANSLATION_STRUCTURE_INVALID"


@pytest.mark.asyncio
async def test_translation_repairs_missing_keys_incrementally(feature_context):
    client, document_id, parse_id = feature_context
    llm = DropKeyOnceTranslationLLM()
    client._transport.app.state.services.translation.llm = llm
    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    assert llm.call_count == 2
    # Verify that the second call requested only the missing key
    assert len(llm.requested_keys[0]) == 2
    assert len(llm.requested_keys[1]) == 1
    units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    assert len(units) == 2
    assert all(unit["auto_text"].startswith("译：") for unit in units)


@pytest.mark.asyncio
async def test_translation_normalizes_whitespace_in_keys(feature_context):
    client, document_id, parse_id = feature_context
    client._transport.app.state.services.translation.llm = WhitespaceKeyTranslationLLM()
    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    assert len(units) == 2
    assert all(unit["auto_text"].startswith("译：") for unit in units)


@pytest.mark.asyncio
async def test_translation_keeps_a_concurrent_locked_edit_and_publishes_new_auto_text(
    feature_context,
):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = BlockingTranslationLLM()
    application.state.services.translation.llm = llm

    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    await asyncio.wait_for(llm.started.wait(), timeout=2)

    units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    edited = await client.patch(
        f"/api/documents/{document_id}/translations/{units[0]['unit_id']}",
        json={
            "parse_id": str(parse_id),
            "block_id": "b1",
            "expected_revision": units[0]["revision"],
            "text": "人工锁定译文",
            "locked": True,
        },
    )
    assert edited.status_code == 200, edited.text
    llm.release.set()

    finished = await wait_for_task(client, accepted.json()["task_id"])
    assert finished["status"] == "succeeded", finished
    assert finished["result_ref"]["conflicted_units"] == 1
    latest = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()[0]
    assert latest["auto_text"] == "译：The value is 42%."
    assert latest["effective_text"] == "人工锁定译文"
    assert latest["locked"] is True


@pytest.mark.asyncio
async def test_translation_executes_batches_concurrently(feature_context, monkeypatch):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = ConcurrentBatchTrackingLLM()
    application.state.services.translation.llm = llm

    monkeypatch.setattr("easylearn.translation._batches", lambda units: [units[:1], units[1:]])

    accepted = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    assert llm.max_active == 2


@pytest.mark.asyncio
async def test_export_contains_a_consistent_snapshot_and_downloadable_zip(feature_context):
    client, document_id, parse_id = feature_context
    accepted = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": str(parse_id), "format": "zip"},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    result = task["result_ref"]
    downloaded = await client.get(f"/api/documents/{document_id}/files/{result['file_id']}")
    assert downloaded.status_code == 200, downloaded.text
    import io

    with ZipFile(io.BytesIO(downloaded.content)) as archive:
        names = set(archive.namelist())
        assert {"source.md", "中文.md", "document.json", "manifest.json"} <= names
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["document_id"] == str(document_id)
    assert manifest["parse_id"] == str(parse_id)


@pytest.mark.asyncio
async def test_export_zip_contains_image_assets_and_usable_markdown_links(feature_context):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    asset_id = uuid4()
    image_bytes = BytesIO()
    with Image.new("RGB", (2, 2), "red") as image:
        image.save(image_bytes, format="PNG")
    raw_image = image_bytes.getvalue()
    asset = AssetDescriptor(
        asset_id=asset_id,
        sha256=hashlib.sha256(raw_image).hexdigest(),
        mime="image/png",
        export_path="images/figure.png",
    )
    current = await application.state.services.documents.load_ir(document_id, parse_id)
    image_block = Block(
        block_id="figure",
        block_type="image",
        order_index=len(current.blocks),
        source_nodes=(ImageNode(node_id="image", asset_id=asset_id, alt="figure"),),
    )
    updated = current.model_copy(
        update={"blocks": (*current.blocks, image_block), "assets": (asset,)}
    )
    parse_directory = application.state.services.files.paths.parse(document_id, parse_id)
    (parse_directory / asset.export_path).parent.mkdir(parents=True, exist_ok=True)
    (parse_directory / asset.export_path).write_bytes(raw_image)
    (parse_directory / "document.json").write_bytes(
        updated.model_dump_json(exclude_computed_fields=True).encode("utf-8")
    )
    application.state.services.documents.invalidate_ir(document_id, parse_id)

    accepted = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": str(parse_id), "format": "zip"},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    downloaded = await client.get(
        f"/api/documents/{document_id}/files/{task['result_ref']['file_id']}"
    )
    with ZipFile(BytesIO(downloaded.content)) as archive:
        names = set(archive.namelist())
        assert "images/figure.png" in names
        assert "images/figure.png" in archive.read("source.md").decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))
    assert any(item["path"] == "images/figure.png" for item in manifest["files"])
    asset_response = await client.get(
        f"/api/documents/{document_id}/files/asset:{parse_id}:{asset_id}"
    )
    assert asset_response.status_code == 200
    assert asset_response.content == image_bytes.getvalue()


@pytest.mark.asyncio
async def test_export_uses_the_translation_snapshot_taken_before_later_edits(
    feature_context, monkeypatch
):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    translated = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert translated.status_code == 202, translated.text
    assert (await wait_for_task(client, translated.json()["task_id"]))["status"] == "succeeded"
    unit = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()[0]

    started = threading.Event()
    release = threading.Event()
    original_write = application.state.services.exporter._write_export

    def delayed_write(*args, **kwargs):
        started.set()
        while not release.wait(0.01):
            pass
        return original_write(*args, **kwargs)

    monkeypatch.setattr(application.state.services.exporter, "_write_export", delayed_write)
    accepted = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": str(parse_id), "format": "zip"},
    )
    assert accepted.status_code == 202, accepted.text
    await asyncio.to_thread(started.wait, 2)
    edited = await client.patch(
        f"/api/documents/{document_id}/translations/{unit['unit_id']}",
        json={
            "parse_id": str(parse_id),
            "block_id": "b1",
            "expected_revision": unit["revision"],
            "text": "导出开始后修改",
        },
    )
    assert edited.status_code == 200, edited.text
    release.set()
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    downloaded = await client.get(
        f"/api/documents/{document_id}/files/{task['result_ref']['file_id']}"
    )
    with ZipFile(BytesIO(downloaded.content)) as archive:
        chinese = archive.read("中文.md").decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))
    assert "译：The value is 42%." in chinese
    assert "导出开始后修改" not in chinese
    assert manifest["translation_revisions"][unit["unit_id"]] == unit["revision"]


@pytest.mark.asyncio
async def test_deleting_a_document_waits_for_export_file_io_to_finish(feature_context, monkeypatch):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    started = threading.Event()
    release = threading.Event()

    def delayed_write(output, ir, translations, revisions, *, include_bilingual):
        del ir, translations, revisions, include_bilingual
        started.set()
        while not release.wait(0.01):
            pass
        (output / "source.md").write_text("source", encoding="utf-8")

    monkeypatch.setattr(application.state.services.exporter, "_write_export", delayed_write)
    accepted = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": str(parse_id), "format": "zip"},
    )
    assert accepted.status_code == 202, accepted.text
    await asyncio.to_thread(started.wait, 2)
    deleting = asyncio.create_task(client.delete(f"/api/documents/{document_id}"))
    try:
        await asyncio.sleep(0.05)
        assert not deleting.done()
    finally:
        release.set()
    response = await deleting
    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_image_export_without_images_fails_with_a_clear_error(feature_context):
    client, document_id, parse_id = feature_context
    accepted = await client.post(
        f"/api/documents/{document_id}/exports",
        json={"parse_id": str(parse_id), "format": "images"},
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "failed", task
    assert task["failure"]["code"] == "EXPORT_NO_IMAGES"


@pytest.mark.asyncio
async def test_qa_freezes_evidence_validates_citations_and_streams_answer(feature_context):
    client, document_id, parse_id = feature_context
    accepted = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What is the value?", "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["task_id"]
    task = await wait_for_task(client, task_id)
    assert task["status"] == "succeeded", task
    assert task["answer"].endswith("[^1]")
    stream = await client.get(f"/api/tasks/{task_id}/answer-stream")
    assert stream.status_code == 200
    assert "该值是 42%" in stream.text
    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert records[0]["parse_id"] == str(parse_id)
    assert records[0]["citations"][0]["citation"] == 1


@pytest.mark.asyncio
async def test_qa_rejects_a_blank_question(feature_context):
    client, document_id, parse_id = feature_context
    response = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": " \t\n", "block_ids": ["b1"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_INVALID"


@pytest.mark.asyncio
async def test_qa_rejects_a_citation_outside_the_frozen_evidence(feature_context):
    client, document_id, parse_id = feature_context
    # The fixture's default fake remains useful for the other feature cases.
    # Replace only the QA client for this protocol-failure case.
    application = client._transport.app
    application.state.services.qa.llm = InvalidCitationLLM()
    accepted = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What is the value?", "block_ids": ["b1"]},
    )
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "failed", task
    assert task["failure"]["code"] == "QA_CITATION_INVALID"
    assert (await client.get(f"/api/documents/{document_id}/qa")).json() == []


@pytest.mark.asyncio
async def test_qa_excludes_a_block_from_the_model_context(feature_context):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = CapturingQALLM()
    application.state.services.qa.llm = llm
    accepted = await client.post(
        f"/api/documents/{document_id}/qa",
        json={
            "parse_id": str(parse_id),
            "question": "What is the value?",
            "block_ids": ["b1"],
            "exclude_block_ids": ["b2"],
            "auto_related": True,
        },
    )
    assert accepted.status_code == 202, accepted.text
    task = await wait_for_task(client, accepted.json()["task_id"])
    assert task["status"] == "succeeded", task
    prompt = llm.messages[0][-1]["content"]
    assert "The value is 42%." in prompt
    assert "A nearby explanation." not in prompt


@pytest.mark.asyncio
async def test_cancelled_qa_does_not_persist_or_start_a_second_model_request(feature_context):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = BlockingQALLM()
    application.state.services.qa.llm = llm
    accepted = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What is the value?", "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["task_id"]
    await asyncio.wait_for(llm.started.wait(), timeout=2)
    cancelled = await client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    finished = await wait_for_task(client, task_id)
    assert finished["status"] == "cancelled", finished
    assert llm.calls == 1
    assert (await client.get(f"/api/documents/{document_id}/qa")).json() == []


@pytest.mark.asyncio
async def test_closing_an_incomplete_answer_stream_cancels_the_qa_task(feature_context):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = PartialBlockingQALLM()
    application.state.services.qa.llm = llm
    accepted = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What is the value?", "block_ids": ["b1"]},
    )
    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["task_id"]
    await asyncio.wait_for(llm.started.wait(), timeout=2)

    route = next(
        route
        for route in application.routes
        if getattr(route, "path", None) == "/api/tasks/{task_id}/answer-stream"
    )
    response = await route.endpoint(
        task_id=UUID(task_id),
        request=Request(
            {
                "type": "http",
                "app": application,
                "method": "GET",
                "path": f"/api/tasks/{task_id}/answer-stream",
                "headers": [],
                "query_string": b"",
            }
        ),
    )
    first_event = await anext(response.body_iterator)
    assert "部分答案" in first_event
    await response.body_iterator.aclose()

    finished = await wait_for_task(client, task_id)
    assert finished["status"] == "cancelled", finished
    assert llm.calls == 1
    assert (await client.get(f"/api/documents/{document_id}/qa")).json() == []
    llm.release.set()


@pytest.mark.asyncio
async def test_page_and_static_assets_are_served(feature_context):
    client, _, _ = feature_context
    page = await client.get("/")
    assert page.status_code == 200
    assert "EasyLearn" in page.text
    script = await client.get("/static/app.js")
    assert script.status_code == 200


@pytest.mark.asyncio
async def test_local_only_blocks_an_external_llm_before_network_access():
    settings = Settings(
        llm=LLMSettings(base_url="https://llm.example.test/v1", model="model", local_only=True)
    )
    client = LLMClient(settings)
    with pytest.raises(DomainError, match="local_only blocks"):
        await client.complete_json([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_translation_saves_batches_incrementally_and_resumes_on_retry(
    feature_context, monkeypatch
):
    client, document_id, parse_id = feature_context
    application = client._transport.app
    llm = FailSecondBatchThenResumeLLM()
    application.state.services.translation.llm = llm

    # Split the 2 units (b1 and b2) into 2 batches
    monkeypatch.setattr(
        "easylearn.translation._batches",
        lambda units: [b for b in [units[:1], units[1:]] if b],
    )

    # Run 1: first batch (b1) succeeds, second batch (b2) fails
    accepted1 = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted1.status_code == 202
    task1 = await wait_for_task(client, accepted1.json()["task_id"])
    assert task1["status"] == "failed"

    # Verify incremental persistence: b1 was saved, b2 was not
    units_after_fail = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    b1_unit = next(u for u in units_after_fail if u["block_id"] == "b1")
    b2_unit = next(u for u in units_after_fail if u["block_id"] == "b2")
    assert b1_unit["auto_text"] is not None
    assert b2_unit["auto_text"] is None

    # Run 2: allow b2 to succeed, retry translation
    llm.should_fail_b2 = False
    llm.call_history.clear()
    accepted2 = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted2.status_code == 202
    task2 = await wait_for_task(client, accepted2.json()["task_id"])
    assert task2["status"] == "succeeded"

    # Verify that only b2 was sent to LLM on the retry (b1 was skipped!)
    assert len(llm.call_history) == 1
    assert not any("42%" in text for text in llm.call_history[0])
    assert any("explanation" in text for text in llm.call_history[0])

    # Verify both are now completed
    final_units = (
        await client.get(f"/api/documents/{document_id}/translations?parse_id={parse_id}")
    ).json()
    assert all(u["auto_text"] is not None for u in final_units)


@pytest.mark.asyncio
async def test_translation_force_retranslates_all_units(feature_context):
    client, document_id, parse_id = feature_context
    accepted1 = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted1.status_code == 202
    task1 = await wait_for_task(client, accepted1.json()["task_id"])
    assert task1["status"] == "succeeded"

    # Default translate without force: skips all, completes immediately without LLM call
    tracking_llm = FailSecondBatchThenResumeLLM()
    tracking_llm.should_fail_b2 = False
    client._transport.app.state.services.translation.llm = tracking_llm

    accepted2 = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id)},
    )
    assert accepted2.status_code == 202
    task2 = await wait_for_task(client, accepted2.json()["task_id"])
    assert task2["status"] == "succeeded"
    assert len(tracking_llm.call_history) == 0

    # Force translate: calls LLM for all units
    accepted3 = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "force": True},
    )
    assert accepted3.status_code == 202
    task3 = await wait_for_task(client, accepted3.json()["task_id"])
    assert task3["status"] == "succeeded"
    assert len(tracking_llm.call_history) > 0


@pytest.mark.asyncio
async def test_llm_client_retries_transient_503_and_recovers():
    from easylearn.translation import compute_retry_delay

    # Test compute_retry_delay properties
    d0 = compute_retry_delay(0, min_delay=2.0, max_delay=30.0, jitter=False)
    assert d0 == 2.0
    d1 = compute_retry_delay(1, min_delay=2.0, max_delay=30.0, jitter=False)
    assert d1 == 4.0
    d4 = compute_retry_delay(4, min_delay=2.0, max_delay=30.0, jitter=False)
    assert d4 == 30.0  # capped at max_delay

    # Retry-After header override
    d_after = compute_retry_delay(0, min_delay=2.0, max_delay=30.0, retry_after=10.0, jitter=False)
    assert d_after == 10.0

    # Test LLMClient with mock transport
    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="Service Unavailable")
        if calls == 2:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"test": "ok"})}}]},
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                base_url="https://llm.example.test/v1",
                model="test-model",
                local_only=False,
                max_retries=3,
                retry_min_delay=0.0,
                retry_max_delay=0.0,
            )
        )
        llm = LLMClient(settings, http=http_client)
        result = await llm.complete_json([{"role": "user", "content": "hello"}])
        assert json.loads(result) == {"test": "ok"}
        assert calls == 3


@pytest.mark.asyncio
async def test_llm_client_exhausts_retries_and_raises_domain_error():
    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                base_url="https://llm.example.test/v1",
                model="test-model",
                local_only=False,
                max_retries=2,
                retry_min_delay=0.0,
                retry_max_delay=0.0,
            )
        )
        llm = LLMClient(settings, http=http_client)
        with pytest.raises(DomainError) as exc_info:
            await llm.complete_json([{"role": "user", "content": "hello"}])
        assert exc_info.value.code == "LLM_UNAVAILABLE"
        assert exc_info.value.retryable is True
        assert calls == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_llm_client_stream_retries_transient_503_and_recovers():
    calls = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="Service Unavailable")
        sse_data = 'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=sse_data)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                base_url="https://llm.example.test/v1",
                model="test-model",
                local_only=False,
                max_retries=2,
                retry_min_delay=0.0,
                retry_max_delay=0.0,
            )
        )
        llm = LLMClient(settings, http=http_client)
        chunks = [chunk async for chunk in llm.stream([{"role": "user", "content": "hi"}])]
        assert "".join(chunks) == "Hello"
        assert calls == 2


@pytest.mark.asyncio
async def test_llm_client_passes_session_id_in_headers_and_payload():
    recorded_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream"):
            sse_data = 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, text=sse_data)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"u1": "译文"}'}}]})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                base_url="https://llm.example.test/v1",
                model="test-model",
                local_only=False,
            )
        )
        llm = LLMClient(settings, http=http_client)

        res = await llm.complete_json(
            [{"role": "user", "content": "hi"}], session_id="session-doc-1"
        )
        assert res == '{"u1": "译文"}'
        req0 = recorded_requests[0]
        assert req0.headers.get("x-agent-session") == "session-doc-1"
        body0 = json.loads(req0.content.decode("utf-8"))
        assert body0.get("user") == "session-doc-1"

        chunks = [
            c
            async for c in llm.stream(
                [{"role": "user", "content": "hi"}], session_id="session-doc-1"
            )
        ]
        assert "".join(chunks) == "ok"
        req1 = recorded_requests[1]
        assert req1.headers.get("x-agent-session") == "session-doc-1"
        body1 = json.loads(req1.content.decode("utf-8"))
        assert body1.get("user") == "session-doc-1"


@pytest.mark.asyncio
async def test_llm_client_qa_enables_deep_thinking():
    recorded_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        sse_data = (
            'data: {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "Answer"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=sse_data)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                active_provider="deepseek",
                providers={
                    "deepseek": LLMProviderSettings(
                        base_url="http://127.0.0.1:9655/v1",
                        model="deepseek-chat",
                        qa_model="deepseek-reasoner",
                        local_only=True,
                    )
                },
            )
        )
        qa_llm = LLMClient(settings, http=http_client, for_qa=True)
        assert qa_llm.configuration.model == "deepseek-reasoner"

        chunks = [
            c
            async for c in qa_llm.stream(
                [{"role": "user", "content": "hi"}], session_id="session-doc-1"
            )
        ]
        assert "".join(chunks) == "Answer"

        req = recorded_requests[0]
        assert req.headers.get("x-thinking-enabled") == "true"
        assert req.headers.get("x-agent-session") == "session-doc-1"
        body = json.loads(req.content.decode("utf-8"))
        assert body.get("model") == "deepseek-reasoner"
        assert body.get("thinking_enabled") is True
        assert body.get("user") == "session-doc-1"


@pytest.mark.asyncio
async def test_llm_client_delete_session_and_has_active_session():
    recorded_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if request.method == "DELETE" and request.url.path == "/v1/sessions/test-agent":
            return httpx.Response(
                200, json={"status": "session_deleted", "agent": "test-agent", "remote_deleted": True}
            )
        if request.method == "GET" and request.url.path == "/v1/sessions":
            return httpx.Response(
                200, json={"agents": [{"agent": "active-agent", "session_id": "sess-123"}]}
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        settings = Settings(
            llm=LLMSettings(
                base_url="http://127.0.0.1:9655/v1",
                model="deepseek-chat",
                local_only=True,
            )
        )
        llm = LLMClient(settings, http=http_client)
        assert await llm.delete_session("test-agent") is True
        assert await llm.has_active_session("active-agent") is True
        assert await llm.has_active_session("missing-agent") is False


@pytest.mark.asyncio
async def test_translation_isolated_and_qa_document_scoped_session_id(feature_context):
    client, document_id, parse_id = feature_context
    application = client._transport.app

    class SessionTrackingLLM(FakeLLM):
        def __init__(self):
            self.translation_sessions: list[str | None] = []
            self.translation_messages: list[list[dict[str, str]]] = []
            self.qa_sessions: list[str | None] = []
            self.deleted_sessions: list[str] = []
            self.qa_messages: list[list[dict[str, str]]] = []
            self.active_probe_result: bool | None = None

        async def complete_json(
            self, messages: list[dict[str, str]], session_id: str | None = None
        ) -> str:
            self.translation_sessions.append(session_id)
            self.translation_messages.append(messages)
            return await super().complete_json(messages)

        async def stream(
            self, messages: list[dict[str, str]], session_id: str | None = None
        ):
            self.qa_sessions.append(session_id)
            self.qa_messages.append(messages)
            async for chunk in super().stream(messages):
                yield chunk

        async def delete_session(self, session_id: str) -> bool:
            self.deleted_sessions.append(session_id)
            return True

        async def has_active_session(self, session_id: str) -> bool | None:
            if self.active_probe_result is not None:
                return self.active_probe_result
            return session_id in self.qa_sessions

    tracker = SessionTrackingLLM()
    application.state.services.translation.llm = tracker
    application.state.services.qa.llm = tracker

    # 1. Trigger translation
    t_resp = await client.post(
        f"/api/documents/{document_id}/translate",
        json={"parse_id": str(parse_id), "block_ids": ["b1"]},
    )
    assert t_resp.status_code == 202, t_resp.text
    t_task = await wait_for_task(client, t_resp.json()["task_id"])
    assert t_task["status"] == "succeeded", t_task

    # Verify translation prompt is Chinese
    assert len(tracker.translation_messages) > 0
    assert "你是一位专业的高质量学术与技术文档翻译专家" in tracker.translation_messages[0][0]["content"]

    # 2. Trigger Turn 1 QA
    q_resp = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What is the value?", "block_ids": ["b1"]},
    )
    assert q_resp.status_code == 202, q_resp.text
    q_task = await wait_for_task(client, q_resp.json()["task_id"])
    assert q_task["status"] == "succeeded", q_task

    expected_trans_session = f"easylearn-translate-{document_id}"
    expected_qa_session = f"easylearn-qa-{document_id}"

    # Translation uses easylearn-translate-{doc_id} and deletes session afterwards
    assert len(tracker.translation_sessions) > 0
    assert all(s and s.startswith(expected_trans_session) for s in tracker.translation_sessions)
    assert any(s and s.startswith(expected_trans_session) for s in tracker.deleted_sessions)

    # QA uses easylearn-qa-{doc_id} and does NOT delete session (persistent per document)
    assert len(tracker.qa_sessions) == 1
    assert tracker.qa_sessions[0] == expected_qa_session
    assert expected_qa_session not in tracker.deleted_sessions

    # QA system prompt is Chinese and instructs combined markdown context
    qa_sys_prompt = tracker.qa_messages[0][0]["content"]
    assert "你是一位严谨专业的学术与技术文档阅读解读助手" in qa_sys_prompt
    assert "结合本文档已投喂的全文 Markdown 上下文" in qa_sys_prompt
    assert "Answer only from the supplied evidence" not in qa_sys_prompt

    # Turn 1 QA prompt includes full document markdown in Chinese
    turn1_prompt = tracker.qa_messages[0][-1]["content"]
    assert "以下是正在阅读的完整文档内容（Markdown）：" in turn1_prompt
    assert "```markdown" in turn1_prompt
    assert "【用户问题】\nWhat is the value?" in turn1_prompt
    assert "【重点参考段落】" in turn1_prompt
    assert "[^1] 段落 b1: 译：The value is 42%." in turn1_prompt

    # 3. Trigger Turn 2 QA for the same document
    q2_resp = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "What else?", "block_ids": ["b1"], "auto_related": False},
    )
    assert q2_resp.status_code == 202, q2_resp.text
    q2_task = await wait_for_task(client, q2_resp.json()["task_id"])
    assert q2_task["status"] == "succeeded", q2_task

    # Turn 2 continues in the same QA session
    assert len(tracker.qa_sessions) == 2
    assert tracker.qa_sessions[1] == expected_qa_session

    # Turn 2 QA prompt only sends the question and reference blocks, NOT the entire markdown
    turn2_prompt = tracker.qa_messages[1][-1]["content"]
    assert "以下是正在阅读的完整文档内容（Markdown）：" not in turn2_prompt
    assert "```markdown" not in turn2_prompt
    assert "【用户问题】\nWhat else?" in turn2_prompt
    assert "[^1] 段落 b1: 译：The value is 42%." in turn2_prompt
    assert "[^2] 段落" not in turn2_prompt

    # 4. Remote session is deleted on DeepSeek Web (has_active_session returns False)
    tracker.active_probe_result = False
    q3_resp = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Explain again?", "block_ids": ["b1"]},
    )
    assert q3_resp.status_code == 202, q3_resp.text
    q3_task = await wait_for_task(client, q3_resp.json()["task_id"])
    assert q3_task["status"] == "succeeded", q3_task

    assert len(tracker.qa_sessions) == 3
    # Turn 3 resends the full markdown because remote session was deleted
    turn3_prompt = tracker.qa_messages[2][-1]["content"]
    assert "以下是正在阅读的完整文档内容（Markdown）：" in turn3_prompt
    assert "```markdown" in turn3_prompt
    assert "【用户问题】\nExplain again?" in turn3_prompt




