from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from easylearn.config import AppSettings, ExtensionSettings, FileSettings, Settings
from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode
from easylearn.main import create_app


async def wait_for_task(client: AsyncClient, task_id: str) -> dict:
    for _ in range(50):
        response = await client.get(f"/api/tasks/{task_id}")
        payload = response.json()
        if payload["status"] in ("succeeded", "failed", "cancelled"):
            return payload
        await asyncio.sleep(0.05)
    raise TimeoutError("Task did not complete")


@pytest_asyncio.fixture
async def qa_env(tmp_path: Path):
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
                    source_nodes=(TextNode(node_id="n1", text="First evidence block."),),
                ),
                Block(
                    block_id="b2",
                    block_type="paragraph",
                    order_index=1,
                    source_nodes=(TextNode(node_id="n2", text="Second evidence block."),),
                ),
            ),
        )
        (parse_directory / "document.json").write_bytes(
            ir.model_dump_json(exclude_computed_fields=True).encode("utf-8")
        )
        async with app.state.services.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO parse_results "
                "(id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at) "
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
        yield client, document_id, parse_id, app


@pytest.mark.asyncio
async def test_follow_up_sends_full_document_context_when_session_unknown(qa_env):
    client, document_id, parse_id, app = qa_env

    received_prompts: list[list[dict[str, str]]] = []

    class UnknownSessionLLM:
        async def stream(self, messages, session_id=None):
            received_prompts.append(messages)
            yield "Turn answer [^1]"

    app.state.services.qa.llm = UnknownSessionLLM()

    # Turn 1
    res1 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 1", "block_ids": ["b1"]},
    )
    assert res1.status_code == 202
    await wait_for_task(client, res1.json()["task_id"])
    assert len(received_prompts) == 1
    assert "以下是正在阅读的完整文档内容（Markdown）：" in received_prompts[0][1]["content"]

    # Turn 2: Follow-up. Unknown session capability -> must send full doc context
    res2 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 2", "block_ids": ["b1"]},
    )
    assert res2.status_code == 202
    await wait_for_task(client, res2.json()["task_id"])
    assert len(received_prompts) == 2
    assert "以下是正在阅读的完整文档内容（Markdown）：" in received_prompts[1][1]["content"]


@pytest.mark.asyncio
async def test_follow_up_sends_full_document_context_when_session_inactive(qa_env):
    client, document_id, parse_id, app = qa_env

    received_prompts: list[list[dict[str, str]]] = []

    class InactiveSessionLLM:
        async def has_active_session(self, session_id: str) -> bool:
            return False

        async def stream(self, messages, session_id=None):
            received_prompts.append(messages)
            yield "Turn answer [^1]"

    app.state.services.qa.llm = InactiveSessionLLM()

    # Turn 1
    res1 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 1", "block_ids": ["b1"]},
    )
    await wait_for_task(client, res1.json()["task_id"])

    # Turn 2: Follow-up with inactive session
    res2 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 2", "block_ids": ["b1"]},
    )
    await wait_for_task(client, res2.json()["task_id"])
    assert len(received_prompts) == 2
    assert "以下是正在阅读的完整文档内容（Markdown）：" in received_prompts[1][1]["content"]


@pytest.mark.asyncio
async def test_follow_up_sends_short_turn_when_session_is_active(qa_env):
    client, document_id, parse_id, app = qa_env

    received_prompts: list[list[dict[str, str]]] = []

    class ActiveSessionLLM:
        async def has_active_session(self, session_id: str) -> bool:
            return True

        async def stream(self, messages, session_id=None):
            received_prompts.append(messages)
            yield "Turn answer [^1]"

    app.state.services.qa.llm = ActiveSessionLLM()

    # Turn 1
    res1 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 1", "block_ids": ["b1"]},
    )
    await wait_for_task(client, res1.json()["task_id"])
    assert "以下是正在阅读的完整文档内容（Markdown）：" in received_prompts[0][1]["content"]

    # Turn 2: Follow-up with active session -> short turn, no full document repetition
    res2 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Question 2", "block_ids": ["b1"]},
    )
    await wait_for_task(client, res2.json()["task_id"])
    assert len(received_prompts) == 2
    assert "以下是正在阅读的完整文档内容（Markdown）：" not in received_prompts[1][1]["content"]
    assert "【用户问题】\nQuestion 2" in received_prompts[1][1]["content"]


@pytest.mark.asyncio
async def test_stream_retry_before_first_delta_succeeds_without_duplicate_records(qa_env):
    client, document_id, parse_id, app = qa_env

    call_count = 0

    class FailFirstStreamLLM:
        async def stream(self, messages, session_id=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Network reset before stream header")
            yield "Successful answer after retry [^1]"

    app.state.services.qa.llm = FailFirstStreamLLM()

    res = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Retry question", "block_ids": ["b1"]},
    )
    assert res.status_code == 202
    task = await wait_for_task(client, res.json()["task_id"])
    assert task["status"] == "succeeded", task
    assert task["answer"] == "Successful answer after retry [^1]"
    assert call_count == 2

    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert len(records) == 1
    assert records[0]["answer"] == "Successful answer after retry [^1]"


@pytest.mark.asyncio
async def test_stream_failure_after_partial_delta_fails_without_repeating_prefix(qa_env):
    client, document_id, parse_id, app = qa_env

    class FailAfterPartialDeltaLLM:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, session_id=None):
            self.calls += 1
            yield "Partial initial delta"
            await asyncio.sleep(0.01)
            raise RuntimeError("Stream interrupted mid-generation")

    llm = FailAfterPartialDeltaLLM()
    app.state.services.qa.llm = llm

    res = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Failing question", "block_ids": ["b1"]},
    )
    assert res.status_code == 202
    task = await wait_for_task(client, res.json()["task_id"])
    assert task["status"] == "failed", task
    assert llm.calls == 1
    assert task["answer"] == "Partial initial delta"

    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert len(records) == 0


@pytest.mark.asyncio
async def test_valid_reserved_citations_are_structured_and_point_to_evidence(qa_env):
    client, document_id, parse_id, app = qa_env

    class ValidCitationLLM:
        async def stream(self, messages, session_id=None):
            yield "Answer with footnote style [^1] and cite tag style [cite:2]."

    app.state.services.qa.llm = ValidCitationLLM()

    res = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Citation question", "block_ids": ["b1", "b2"]},
    )
    assert res.status_code == 202
    task = await wait_for_task(client, res.json()["task_id"])
    assert task["status"] == "succeeded", task

    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert len(records) == 1
    citations = records[0]["citations"]
    assert len(citations) == 2
    assert citations[0]["citation"] == 1
    assert citations[0]["block_id"] == "b1"
    assert citations[0]["text"] == "First evidence block."
    assert citations[1]["citation"] == 2
    assert citations[1]["block_id"] == "b2"
    assert citations[1]["text"] == "Second evidence block."


@pytest.mark.asyncio
async def test_out_of_range_reserved_citation_fails_with_protocol_error(qa_env):
    client, document_id, parse_id, app = qa_env

    class OutOfRangeCaretLLM:
        async def stream(self, messages, session_id=None):
            yield "Citing non-existent block [^99]"

    app.state.services.qa.llm = OutOfRangeCaretLLM()

    res = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Out of range question", "block_ids": ["b1"]},
    )
    task = await wait_for_task(client, res.json()["task_id"])
    assert task["status"] == "failed", task
    assert task["failure"]["code"] == "QA_CITATION_INVALID"

    class ZeroIndexCiteLLM:
        async def stream(self, messages, session_id=None):
            yield "Citing zero index [cite:0]"

    app.state.services.qa.llm = ZeroIndexCiteLLM()

    res2 = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Zero question", "block_ids": ["b1"]},
    )
    task2 = await wait_for_task(client, res2.json()["task_id"])
    assert task2["status"] == "failed", task2
    assert task2["failure"]["code"] == "QA_CITATION_INVALID"


@pytest.mark.asyncio
async def test_ordinary_bracketed_numbers_not_mistaken_for_citations(qa_env):
    client, document_id, parse_id, app = qa_env

    class OrdinaryNumbersInProseLLM:
        async def stream(self, messages, session_id=None):
            yield (
                "In [1998], the experiment was initialized at index [0] with score [42]. "
                "Evidence from [^1]."
            )

    app.state.services.qa.llm = OrdinaryNumbersInProseLLM()

    res = await client.post(
        f"/api/documents/{document_id}/qa",
        json={"parse_id": str(parse_id), "question": "Prose question", "block_ids": ["b1"]},
    )
    assert res.status_code == 202
    task = await wait_for_task(client, res.json()["task_id"])
    assert task["status"] == "succeeded", task

    records = (await client.get(f"/api/documents/{document_id}/qa")).json()
    assert len(records) == 1
    assert len(records[0]["citations"]) == 1
    assert records[0]["citations"][0]["citation"] == 1
    assert records[0]["citations"][0]["block_id"] == "b1"
    assert "In [1998]" in records[0]["answer"]
    assert "index [0]" in records[0]["answer"]
    assert "score [42]" in records[0]["answer"]
