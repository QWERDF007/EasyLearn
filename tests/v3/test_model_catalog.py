import json
from pathlib import Path

import pytest

from easylearn.config import MinerUSettings
from easylearn.errors import DomainError
from easylearn.mineru.models import MinerUModelCatalog


def test_model_catalog_lists_only_directories_and_resolves_a_model(tmp_path: Path):
    first = tmp_path / "MinerU-first"
    second = tmp_path / "MinerU-second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "config.json").write_text(
            json.dumps({
                "model_type": "qwen2_vl",
                "architectures": ["Qwen2VLForConditionalGeneration"],
            }),
            encoding="utf-8",
        )
        (directory / "preprocessor_config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(b"test")
    (tmp_path / "not-a-model.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "not-a-vlm").mkdir()
    settings = MinerUSettings(model_path=first, model_root=tmp_path)

    catalog = MinerUModelCatalog(settings)

    assert [item.model_id for item in catalog.list()] == ["MinerU-first", "MinerU-second"]
    assert catalog.resolve("MinerU-second") == second.resolve()
    assert catalog.list()[0].selected is True


def test_model_catalog_ignores_unrelated_model_directories(tmp_path: Path):
    unrelated = tmp_path / "dinov2"
    unrelated.mkdir()
    (unrelated / "weights.pth").write_bytes(b"test")
    selected = tmp_path / "MinerU"
    selected.mkdir()
    (selected / "config.json").write_text(
        json.dumps({"model_type": "qwen2_vl"}), encoding="utf-8"
    )
    (selected / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (selected / "model.safetensors").write_bytes(b"test")

    catalog = MinerUModelCatalog(MinerUSettings(model_path=selected, model_root=tmp_path))

    assert [item.model_id for item in catalog.list()] == ["MinerU"]


def test_model_catalog_rejects_paths_outside_model_root(tmp_path: Path):
    selected = tmp_path / "selected"
    selected.mkdir()
    catalog = MinerUModelCatalog(MinerUSettings(model_path=selected, model_root=tmp_path))

    with pytest.raises(DomainError, match="MinerU model"):
        catalog.resolve("..")


def test_parse_request_keeps_a_model_id_for_task_scope():
    from easylearn.parser import ParseRequest

    request = ParseRequest(model_id="MinerU-second")

    assert request.model_id == "MinerU-second"
