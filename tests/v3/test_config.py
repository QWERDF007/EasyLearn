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


def test_config_resolves_llm_settings_and_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CUSTOM_LLM_KEY", "env-secret-123")
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm]\n"
        'base_url = "https://api.pinaic.com/v1"\n'
        'model = "gpt-5.5"\n'
        'reasoning_effort = "low"\n'
        'api_key_env = "CUSTOM_LLM_KEY"\n'
        "local_only = false\n",
        encoding="utf-8",
    )

    settings = Settings.load(config)
    assert settings.llm.base_url == "https://api.pinaic.com/v1"
    assert settings.llm.model == "gpt-5.5"
    assert settings.llm.reasoning_effort == "low"
    assert settings.llm.local_only is False
    assert settings.llm_api_key == "env-secret-123"

    # Explicit api_key overrides env
    override = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"api_key": "explicit-key-456"})}
    )
    assert override.llm_api_key == "explicit-key-456"


def test_config_loads_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DOTENV_TEST_KEY", raising=False)
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text('DOTENV_TEST_KEY="from-dotenv-789"\n', encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm]\n"
        'base_url = "https://api.pinaic.com/v1"\n'
        'model = "gpt-5.5"\n'
        'api_key_env = "DOTENV_TEST_KEY"\n'
        "local_only = false\n",
        encoding="utf-8",
    )

    settings = Settings.load(config)
    assert settings.llm_api_key == "from-dotenv-789"


def test_config_resolves_translation_concurrency(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[tasks]\n"
        "translation_concurrency = 6\n",
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.tasks.translation_concurrency == 6


