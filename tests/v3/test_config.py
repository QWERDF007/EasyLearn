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


def test_config_llm_provider_switching_deepseek_and_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TEST_PINAI_KEY", "secret-pinai-key")
    config_content = (
        "[llm]\n"
        'active_provider = "deepseek"\n'
        'proxy = "http://127.0.0.1:7890"\n'
        "timeout_seconds = 300.0\n"
        "\n"
        "[llm.providers.deepseek]\n"
        'base_url = "http://127.0.0.1:9655/v1"\n'
        'model = "deepseek-chat"\n'
        'qa_model = "deepseek-reasoner"\n'
        'api_key = "sk-freedeepseek"\n'
        "local_only = true\n"
        "translation_concurrency = 1\n"
        "\n"
        "[llm.providers.openai]\n"
        'base_url = "https://api.pinaic.com/v1"\n'
        'model = "gpt-5.6-luna"\n'
        'api_key_env = "TEST_PINAI_KEY"\n'
        "local_only = false\n"
        'reasoning_effort = "low"\n'
        "translation_concurrency = 4\n"
    )
    config = tmp_path / "config.toml"
    config.write_text(config_content, encoding="utf-8")

    # 1. When active_provider is deepseek
    settings = Settings.load(config)
    assert settings.llm.active_provider == "deepseek"
    assert settings.llm.base_url == "http://127.0.0.1:9655/v1"
    assert settings.llm.model == "deepseek-chat"
    assert settings.llm.local_only is True
    assert settings.llm_api_key == "sk-freedeepseek"
    assert settings.translation_concurrency == 1
    assert settings.qa_llm.model == "deepseek-reasoner"
    # Proxy is kept in settings.llm.proxy, but resolved_proxy automatically bypasses it
    assert settings.llm.proxy == "http://127.0.0.1:7890"
    assert settings.llm.resolved_proxy is None

    # 2. When active_provider is switched to openai
    openai_toml = config_content.replace(
        'active_provider = "deepseek"', 'active_provider = "openai"'
    )
    config.write_text(openai_toml, encoding="utf-8")
    settings_openai = Settings.load(config)
    assert settings_openai.llm.active_provider == "openai"
    assert settings_openai.llm.base_url == "https://api.pinaic.com/v1"
    assert settings_openai.llm.model == "gpt-5.6-luna"
    assert settings_openai.llm.local_only is False
    assert settings_openai.llm.reasoning_effort == "low"
    assert settings_openai.llm_api_key == "secret-pinai-key"
    assert settings_openai.translation_concurrency == 4
    assert settings_openai.qa_llm.model == "gpt-5.6-luna"
    assert settings_openai.llm.proxy == "http://127.0.0.1:7890"
    assert settings_openai.llm.resolved_proxy == "http://127.0.0.1:7890"


def test_config_qa_provider_override(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm]\n"
        'active_provider = "deepseek"\n'
        'qa_provider = "openai"\n'
        "[llm.providers.deepseek]\n"
        'base_url = "http://127.0.0.1:9655/v1"\n'
        'model = "deepseek-chat"\n'
        "[llm.providers.openai]\n"
        'base_url = "https://api.pinaic.com/v1"\n'
        'model = "gpt-5.6-luna"\n',
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.llm.active_provider == "deepseek"
    assert settings.llm.model == "deepseek-chat"
    assert settings.llm.qa_provider == "openai"
    assert settings.qa_llm.active_provider == "openai"
    assert settings.qa_llm.model == "gpt-5.6-luna"


def test_config_llm_invalid_provider_raises(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm]\n"
        'active_provider = "unknown_provider"\n'
        "[llm.providers.deepseek]\n"
        'base_url = "http://127.0.0.1:9655/v1"\n'
        'model = "deepseek-chat"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="active_provider 'unknown_provider' not found"):
        Settings.load(config)


def test_config_qa_model_defaults_to_deepseek_reasoner(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[llm]\n"
        'active_provider = "deepseek"\n'
        "[llm.providers.deepseek]\n"
        'base_url = "http://127.0.0.1:9655/v1"\n'
        'model = "deepseek-chat"\n',
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.llm.model == "deepseek-chat"
    assert settings.qa_llm.model == "deepseek-reasoner"





