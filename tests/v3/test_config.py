from pathlib import Path

import pytest

from easylearn.config import AppSettings, FileSettings, Settings
from easylearn.main import create_app


def test_config_resolves_explicit_model_candidates_and_task_tmp_dir(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[mineru]\n"
        'default_model_id = "vision"\n'
        "[[mineru.models]]\n"
        'model_id = "vision"\n'
        'name = "Vision parser"\n'
        'path = "models/vision"\n'
        "[files]\n"
        'tmp_dir = "tmp"\n',
        encoding="utf-8",
    )

    settings = Settings.load(config)

    assert settings.mineru.default_model_id == "vision"
    assert settings.mineru.models[0].path == (tmp_path / "models/vision").resolve()
    assert settings.tmp_dir == (tmp_path / "tmp").resolve()


@pytest.mark.asyncio
async def test_application_uses_configured_task_tmp_dir(tmp_path: Path):
    task_tmp = tmp_path / "external-tmp"
    app = create_app(
        Settings(
            app=AppSettings(data_dir=tmp_path / "data"),
            files=FileSettings(tmp_dir=task_tmp),
        )
    )

    async with app.router.lifespan_context(app):
        assert app.state.services.files.paths.temporary == task_tmp
        assert task_tmp.is_dir()
