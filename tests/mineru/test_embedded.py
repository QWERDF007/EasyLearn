import json
from pathlib import Path

import pytest

from easylearn.errors import DomainError
from easylearn.mineru.embedded import EmbeddedMinerU, resolve_vlm_backend
from easylearn.mineru.schema import MinerUOptions


@pytest.mark.asyncio
async def test_embedded_mineru_writes_the_canonical_result_tree(tmp_path: Path):
    model_path = tmp_path / "model"
    model_path.mkdir()
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-synthetic")
    calls: dict[str, object] = {}

    async def fake_analyzer(pdf_bytes, image_writer, **kwargs):
        calls.update(kwargs)
        assert pdf_bytes == input_pdf.read_bytes()
        middle = {
            "_version_name": "3.4.5",
            "_backend": "vlm",
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [600, 800],
                    "para_blocks": [],
                }
            ],
        }
        return middle, [{"page": 0}]

    runtime = EmbeddedMinerU(model_path, analyzer=fake_analyzer)
    output = tmp_path / "output"
    await runtime.parse(input_pdf, output, MinerUOptions(page_count=1))

    result = output / "input" / "vlm"
    assert calls == {
        "backend": resolve_vlm_backend("auto"),
        "model_path": str(model_path),
        "image_analysis": False,
    }
    assert json.loads((result / "input_middle.json").read_text(encoding="utf-8"))[
        "_backend"
    ] == "vlm"
    assert json.loads((result / "input_model.json").read_text(encoding="utf-8")) == [
        {"page": 0}
    ]
    assert (result / "input.md").read_text(encoding="utf-8") == ""
    assert (result / "input_origin.pdf").read_bytes() == input_pdf.read_bytes()


@pytest.mark.asyncio
async def test_embedded_mineru_rejects_a_missing_local_model(tmp_path: Path):
    runtime = EmbeddedMinerU(tmp_path / "missing")
    with pytest.raises(DomainError, match="MINERU_MODEL_UNAVAILABLE"):
        await runtime.parse(
            tmp_path / "input.pdf", tmp_path / "output", MinerUOptions(page_count=1)
        )


@pytest.mark.asyncio
async def test_embedded_mineru_honors_configured_vlm_backend_and_batch_size(tmp_path: Path):
    model_path = tmp_path / "model"
    model_path.mkdir()
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF-synthetic")
    calls: dict[str, object] = {}

    async def fake_analyzer(pdf_bytes, image_writer, **kwargs):
        calls.update(kwargs)
        middle = {
            "_version_name": "3.4.5",
            "_backend": "vlm",
            "pdf_info": [{"page_idx": 0, "page_size": [600, 800], "para_blocks": []}],
        }
        return middle, [{"page": 0}]

    runtime = EmbeddedMinerU(model_path, analyzer=fake_analyzer)
    output = tmp_path / "output"
    options = MinerUOptions(
        page_count=1,
        vlm_backend="lmdeploy-engine",
        batch_size=4,
        lmdeploy_backend="turbomind",
    )
    await runtime.parse(input_pdf, output, options)

    assert calls["backend"] == "lmdeploy-engine"
    assert calls["batch_size"] == 4
    assert calls["lmdeploy_backend"] == "turbomind"


def test_mineru_parse_options_configurable_from_toml():
    import tomllib
    from easylearn.config import Settings

    raw_toml = """
    [mineru]
    default_model_id = "test-model"
    [[mineru.models]]
    model_id = "test-model"
    path = "F:/models/test"

    [mineru.parse]
    vlm_backend = "lmdeploy-engine"
    batch_size = 8
    lmdeploy_backend = "turbomind"
    """
    data = tomllib.loads(raw_toml)
    settings = Settings.model_validate(data)
    assert settings.mineru.parse.vlm_backend == "lmdeploy-engine"
    assert settings.mineru.parse.batch_size == 8
    assert settings.mineru.parse.lmdeploy_backend == "turbomind"

