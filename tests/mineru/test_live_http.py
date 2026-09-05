import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest

from easylearn.mineru.client import MinerUClient
from easylearn.mineru.schema import MinerUOptions


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", ["repository", "user_paper"])
async def test_pdf_roundtrip_through_a_real_http_protocol_peer(uvicorn_server, tmp_path, sample):
    if sample == "user_paper":
        if not os.environ.get("EASYLEARN_ACCEPTANCE_PDF"):
            pytest.skip("Real user PDF not configured")
        source = Path(os.environ["EASYLEARN_ACCEPTANCE_PDF"])
        digest = "9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8"
        pages = 27
    else:
        source = Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf")
        digest = "ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b"
        pages = 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    request_id = uuid4()
    with uvicorn_server(
        "protocol_server:app", health_path="/health", app_dir=Path(__file__).parent
    ) as peer:
        async with httpx.AsyncClient(base_url=peer.base_url, trust_env=False) as http:
            client = MinerUClient(http)
            assert (await client.health()).protocol_version == 2
            started = time.perf_counter()
            with source.open("rb") as input_file:
                receipt = await client.submit(
                    input_file, request_id=request_id, options=MinerUOptions(page_count=pages)
                )
            task = await client.query(receipt.value.task_id)
            assert task.value.status == "completed"
            assert json.loads(receipt.raw_body)["task_id"] == str(task.value.task_id)
            destination = tmp_path / "download.zip"
            async with client.download(task.value.task_id) as chunks:
                with destination.open("wb") as output:
                    async for chunk in chunks:
                        output.write(chunk)
            with ZipFile(destination) as archive:
                assert (
                    hashlib.sha256(archive.read("transport-test/input.pdf")).hexdigest() == digest
                )
                evidence = json.loads(archive.read("transport-test/received.json"))
                assert evidence["sha256"] == digest
                assert evidence["filename"] == "input.pdf"
                assert evidence["content_type"] == "application/pdf"
                assert evidence["request_id"] == str(request_id)
                assert evidence["options"]["end_page_id"] == str(pages - 1)
                assert evidence["options"]["return_original_file"] == "true"
                assert evidence["options"]["response_format_zip"] == "true"
            assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
            print(
                f"{pages}-page PDF protocol transport roundtrip: "
                f"{time.perf_counter() - started:.2f}s"
            )
