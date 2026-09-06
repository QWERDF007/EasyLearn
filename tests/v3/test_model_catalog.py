from pathlib import Path

import pytest
from pydantic import ValidationError

from easylearn.config import MinerUModelCandidate, MinerUSettings
from easylearn.errors import DomainError
from easylearn.mineru.models import MinerUModelCatalog


def test_model_catalog_uses_only_explicit_candidates(tmp_path: Path):
    first = tmp_path / "MinerU-first"
    second = tmp_path / "MinerU-second"
    unconfigured = tmp_path / "unconfigured"
    for directory in (first, second, unconfigured):
        directory.mkdir()
    settings = MinerUSettings(
        models=(
            MinerUModelCandidate(model_id="first", name="MinerU first", path=first),
            MinerUModelCandidate(model_id="second", path=second),
        ),
        default_model_id="second",
    )

    catalog = MinerUModelCatalog(settings)

    assert [item.model_id for item in catalog.list()] == ["first", "second"]
    assert catalog.list()[0].name == "MinerU first"
    assert catalog.list()[1].selected is True
    assert catalog.default_model_id == "second"
    assert catalog.resolve(None) == second.resolve()
    assert catalog.resolve("first") == first.resolve()
    assert catalog.model_id_for(second) == "second"


def test_model_catalog_does_not_discover_unconfigured_directories(tmp_path: Path):
    selected = tmp_path / "MinerU"
    unrelated = tmp_path / "dinov2"
    selected.mkdir()
    unrelated.mkdir()
    settings = MinerUSettings(
        models=(MinerUModelCandidate(model_id="MinerU", path=selected),),
    )

    catalog = MinerUModelCatalog(settings)

    assert [item.model_id for item in catalog.list()] == ["MinerU"]
    assert catalog.resolve("MinerU") == selected.resolve()


def test_model_catalog_rejects_unknown_model_id(tmp_path: Path):
    selected = tmp_path / "selected"
    selected.mkdir()
    catalog = MinerUModelCatalog(
        MinerUSettings(models=(MinerUModelCandidate(model_id="selected", path=selected),))
    )

    with pytest.raises(DomainError, match="MinerU model"):
        catalog.resolve("..")


def test_model_settings_reject_duplicate_ids_and_unknown_default(tmp_path: Path):
    with pytest.raises(ValidationError):
        MinerUSettings(
            models=(
                MinerUModelCandidate(model_id="same", path=tmp_path / "one"),
                MinerUModelCandidate(model_id="same", path=tmp_path / "two"),
            )
        )
    with pytest.raises(ValidationError):
        MinerUSettings(
            models=(MinerUModelCandidate(model_id="one", path=tmp_path / "one"),),
            default_model_id="missing",
        )


def test_parse_request_keeps_a_model_id_for_task_scope():
    from easylearn.parser import ParseRequest

    request = ParseRequest(model_id="MinerU-second")

    assert request.model_id == "MinerU-second"
