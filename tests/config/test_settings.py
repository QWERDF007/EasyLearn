import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from pydantic_settings import SettingsError

from easylearn.config import Settings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    for name in os.environ:
        if name.upper().startswith("EASYLEARN_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_toml_file_configures_typed_settings_and_relative_storage(tmp_path):
    (tmp_path / "config.toml").write_text(
        'database_url = "postgresql+asyncpg://user@localhost/easylearn"\n'
        'storage_root = "论文资产"\n'
        "[preview_limits]\nmax_pages = 75\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert (
        settings.database_url.get_secret_value() == "postgresql+asyncpg://user@localhost/easylearn"
    )
    assert settings.storage_root == tmp_path / "论文资产"
    assert settings.preview_limits.max_pages == 75


def test_explicit_configuration_path_selects_one_file_and_roots_its_paths(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("upload_max_bytes = 10\n", encoding="utf-8")
    chosen = tmp_path / "deploy" / "selected.yaml"
    chosen.parent.mkdir()
    chosen.write_text(
        "database_url: postgresql+asyncpg://user@localhost/selected\nstorage_root: artifacts\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EASYLEARN_CONFIG", str(chosen))
    settings = Settings()
    assert (
        settings.database_url.get_secret_value() == "postgresql+asyncpg://user@localhost/selected"
    )
    assert settings.storage_root == chosen.parent / "artifacts"
    assert settings.upload_max_bytes > 10


@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_yaml_file_uses_the_same_typed_settings(tmp_path, suffix):
    (tmp_path / f"config.{suffix}").write_text(
        "database_url: postgresql+asyncpg://user@localhost/easylearn\n"
        "storage_root: 论文资产\n"
        "preview_limits:\n  max_pages: 75\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert (
        settings.database_url.get_secret_value() == "postgresql+asyncpg://user@localhost/easylearn"
    )
    assert settings.storage_root == tmp_path / "论文资产"
    assert settings.preview_limits.max_pages == 75


@pytest.mark.parametrize("choice", ["ambiguous", "missing", "unsupported"])
def test_invalid_file_selection_is_not_silently_ignored(tmp_path, monkeypatch, choice):
    if choice == "ambiguous":
        (tmp_path / "config.toml").touch()
        (tmp_path / "config.yaml").touch()
        error = "Multiple configuration files"
    elif choice == "missing":
        monkeypatch.setenv("EASYLEARN_CONFIG", str(tmp_path / "missing.yaml"))
        error = "Configuration file does not exist"
    else:
        file = tmp_path / "config.json"
        file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("EASYLEARN_CONFIG", str(file))
        error = "Unsupported configuration format"
    with pytest.raises(SettingsError, match=error):
        Settings(database_url="postgresql+asyncpg://user@localhost/test")


def test_constructor_environment_dotenv_file_defaults_precedence_is_shared_for_nested_values(
    tmp_path, monkeypatch
):
    (tmp_path / "config.toml").write_text(
        'database_url = "postgresql+asyncpg://file@localhost/test"\n'
        "upload_max_bytes = 1000\nupload_chunk_bytes = 100\n"
        "[preview_limits]\nmax_pages = 75\nrender_edge_pixels = 512\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "EASYLEARN_UPLOAD_MAX_BYTES=2000\nEASYLEARN_UPLOAD_CHUNK_BYTES=200\n", encoding="utf-8"
    )
    monkeypatch.setenv("EASYLEARN_UPLOAD_MAX_BYTES", "3000")
    monkeypatch.setenv("EASYLEARN_PREVIEW_LIMITS__MAX_PAGES", "50")
    monkeypatch.setenv("EASYLEARN_STORAGE_ROOT", "environment-assets")
    settings = Settings(upload_max_bytes=4000)
    assert settings.upload_max_bytes == 4000
    assert settings.upload_chunk_bytes == 200
    assert Settings().upload_max_bytes == 3000
    assert settings.preview_limits.max_pages == 50
    assert settings.preview_limits.render_edge_pixels == 512
    assert settings.preview_limits.max_page_points == 14400
    assert settings.storage_root == Path("environment-assets")


@pytest.mark.parametrize(
    "contents, issue",
    [
        ("upload_max_byte = 100", "extra_forbidden"),
        ("upload_max_bytes = 0", "greater_than"),
        ("[preview_limits]\nmax_pages = 0", "greater_than"),
    ],
)
def test_configuration_mistakes_fail_validation_without_exposing_secrets(tmp_path, contents, issue):
    (tmp_path / "config.toml").write_text(
        'database_url = "postgresql+asyncpg://user:private-password@localhost/test"\n' + contents,
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as error:
        Settings()
    assert error.value.errors(include_input=False)[0]["type"] == issue
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize(
    "suffix, contents",
    [
        ("yaml", "database_url: [private-password"),
        ("yaml", "- private-password"),
        ("yaml", "!!python/object/apply:os.system ['private-password']"),
        ("toml", 'database_url = "private-password'),
    ],
)
def test_malformed_or_non_mapping_files_fail_without_dumping_file_contents(
    tmp_path, suffix, contents
):
    (tmp_path / f"config.{suffix}").write_text(contents, encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        Settings()
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "not-a-database-url",
        "postgresql+asyncpg://user@localhost:private-password/test",
        "sqlite:///local.db",
    ],
)
def test_database_configuration_errors_are_typed_and_redacted(value):
    with pytest.raises(ValidationError) as error:
        Settings(database_url=value)
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize("suffix", ["toml", "yaml"])
def test_model_paths_mineru_connection_and_shared_llm_routes_come_from_the_selected_file(
    tmp_path, suffix
):
    data = {
        "database_url": "postgresql+asyncpg://user@localhost/test",
        "models_root": "models",
        "local_models": {"mineru_vlm": {"path": "mineru-weights", "revision": "weights-v1"}},
        "mineru": {
            "base_url": "http://127.0.0.1:8001",
            "profile_revision": "mineru-v1",
            "local_model": "mineru_vlm",
            "parse": {"backend": "vlm-engine", "language": "en"},
        },
        "providers": {
            "main": {
                "base_url": "http://127.0.0.1:8002/v1",
                "model": "chat-model",
                "revision": "chat-v1",
                "api_key": "private-model-key",
                "context_limit": 32768,
                "capabilities": {"chat": True, "stream": True},
            }
        },
        "model_routes": {"translation": "main", "qa": "main"},
    }
    toml = """
database_url = "postgresql+asyncpg://user@localhost/test"
models_root = "models"
[local_models.mineru_vlm]
path = "mineru-weights"
revision = "weights-v1"
[mineru]
base_url = "http://127.0.0.1:8001"
profile_revision = "mineru-v1"
local_model = "mineru_vlm"
[mineru.parse]
backend = "vlm-engine"
language = "en"
[providers.main]
base_url = "http://127.0.0.1:8002/v1"
model = "chat-model"
revision = "chat-v1"
api_key = "private-model-key"
context_limit = 32768
[providers.main.capabilities]
chat = true
stream = true
[model_routes]
translation = "main"
qa = "main"
"""
    configuration = tmp_path / f"config.{suffix}"
    configuration.write_text(toml if suffix == "toml" else yaml.safe_dump(data), encoding="utf-8")
    settings = Settings()
    assert settings.models_root == tmp_path / "models"
    assert settings.local_models["mineru_vlm"].path == tmp_path / "models" / "mineru-weights"
    assert settings.mineru.parse.backend == "vlm-engine"
    assert settings.mineru.parse.language == "en"
    assert settings.mineru.local_model == "mineru_vlm"
    assert str(settings.mineru.base_url) == "http://127.0.0.1:8001/"
    assert settings.providers["main"].model == "chat-model"
    assert settings.providers["main"].api_key.get_secret_value() == "private-model-key"
    assert settings.model_routes.translation == settings.model_routes.qa == "main"
    assert "private-model-key" not in repr(settings)


@pytest.mark.parametrize("case", ["missing_profile", "qa", "embedding", "vision", "local_model"])
def test_model_routes_require_existing_profiles_with_the_requested_capability(case):
    data = {
        "database_url": "postgresql+asyncpg://user@localhost/test",
        "providers": {
            "main": {
                "base_url": "http://127.0.0.1:8002/v1",
                "model": "configured-model",
                "revision": "v1",
                "context_limit": 8192,
                "capabilities": {"chat": case != "qa"},
            }
        },
    }
    if case == "missing_profile":
        data["model_routes"] = {"qa": "missing"}
    elif case == "local_model":
        data["mineru"] = {
            "base_url": "http://127.0.0.1:8001",
            "profile_revision": "v1",
            "local_model": "missing",
        }
    else:
        data["model_routes"] = {case: "main"}
    with pytest.raises(ValidationError):
        Settings(**data)


def test_generation_and_embedding_profiles_have_separate_models_budgets_and_transport_limits(
    tmp_path,
):
    settings = Settings(
        database_url="postgresql+asyncpg://user@localhost/test",
        models_root=tmp_path,
        local_models={"chat_weights": {"path": "chat", "revision": "weights-v1"}},
        providers={
            "chat": {
                "base_url": "http://127.0.0.1:8002/v1",
                "model": "chat-model",
                "revision": "v2",
                "scope": "local",
                "local_model": "chat_weights",
                "tokenizer_model": "chat_weights",
                "model_revision": "served-v2",
                "chat_template_revision": "template-v1",
                "context_limit": 32768,
                "capabilities": {"chat": True, "stream": True},
                "generation": {
                    "max_output_tokens": 4096,
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "reasoning_budget": 1024,
                    "safety_margin": 512,
                },
                "timeouts": {"connect_seconds": 5, "read_idle_seconds": 30, "total_seconds": 120},
                "scheduling": {
                    "max_inflight": 4,
                    "reserved_qa_slots": 1,
                    "tokens_per_minute": 60000,
                },
            },
            "embed": {
                "base_url": "http://127.0.0.1:8003/v1",
                "model": "embedding-model",
                "revision": "v1",
                "context_limit": 8192,
                "capabilities": {"embedding": True},
                "embedding": {"dimensions": 1024, "normalize": True, "max_batch_size": 16},
            },
        },
        model_routes={"translation": "chat", "qa": "chat", "embedding": "embed"},
    )
    assert settings.providers["chat"].generation.max_output_tokens == 4096
    assert settings.providers["chat"].timeouts.total_seconds == 120
    assert settings.providers["chat"].scheduling.reserved_qa_slots == 1
    assert settings.providers["chat"].tokenizer_model == "chat_weights"
    assert settings.providers["embed"].embedding.dimensions == 1024
    assert settings.providers["embed"].capabilities.chat is False
    assert settings.local_models["chat_weights"].path == tmp_path / "chat"


@pytest.mark.parametrize(
    "changed",
    [
        {"model": "   "},
        {"base_url": "http://user:private-password@127.0.0.1/v1"},
        {"base_url": "http://127.0.0.1/v1?api_key=private-password"},
        {"base_url": "http://127.0.0.1/v1#fragment"},
        {"local_model": "missing"},
        {"tokenizer_model": "missing"},
        {"generation": {"max_output_tokens": 8192}},
        {"generation": {"max_output_tokens": 100, "reasoning_budget": 101}},
        {"scheduling": {"max_inflight": 1, "reserved_qa_slots": 2}},
        {"timeouts": {"total_seconds": float("inf")}},
        {"capabilities": {"embedding": True}},
        {"capabilities": {"vision": True}},
        {"capabilities": {"stream": True}},
        {"capabilities": {"json_schema": True}},
        {"embedding": {"dimensions": 1024}},
    ],
)
def test_invalid_model_connection_budgets_capabilities_and_references_are_rejected_without_secrets(
    changed,
):
    provider = {
        "base_url": "http://127.0.0.1/v1",
        "model": "chat",
        "revision": "v1",
        "context_limit": 8192,
        "capabilities": {"chat": True},
        "api_key": "private-password",
        **changed,
    }
    with pytest.raises(ValidationError) as error:
        Settings(
            database_url="postgresql+asyncpg://user@localhost/test", providers={"main": provider}
        )
    assert "private-password" not in str(error.value)


def test_distributed_toml_and_yaml_examples_describe_the_same_model_deployment(monkeypatch):
    project = Path(__file__).resolve().parents[2]
    configurations = []
    for suffix in ("toml", "yaml"):
        monkeypatch.setenv("EASYLEARN_CONFIG", str(project / f"config.example.{suffix}"))
        configurations.append(Settings())
    toml, yaml_settings = configurations
    assert toml.model_dump() == yaml_settings.model_dump()
    assert toml.models_root == Path("D:/Models")
    assert toml.local_models["mineru_vlm"].path == Path("D:/Models/MinerU2.5-Pro-2605-1.2B")
    assert toml.mineru.local_model == "mineru_vlm"
    assert toml.mineru.parse.backend == "vlm-engine"
    assert (
        toml.model_routes.translation == toml.model_routes.qa == toml.model_routes.vision == "chat"
    )
    assert toml.model_routes.embedding == "embed"
    assert toml.providers["chat"].tokenizer_model == "chat_weights"
    assert toml.local_models["chat_weights"].path == Path("D:/Models/replace-with-chat-model")
    assert toml.providers["embed"].embedding.dimensions == 1024


@pytest.mark.parametrize("suffix", ["toml", "yaml"])
def test_model_environment_overrides_merge_with_file_and_resolve_only_relative_paths(
    monkeypatch, tmp_path, suffix
):
    project = Path(__file__).resolve().parents[2]
    root = tmp_path / "environment-models"
    absolute_chat = tmp_path / "separate-chat-model"
    monkeypatch.setenv("EASYLEARN_CONFIG", str(project / f"config.example.{suffix}"))
    monkeypatch.setenv("EASYLEARN_MODELS_ROOT", str(root))
    monkeypatch.setenv("EASYLEARN_LOCAL_MODELS__CHAT_WEIGHTS__PATH", str(absolute_chat))
    monkeypatch.setenv("EASYLEARN_PROVIDERS__CHAT__MODEL", "env-chat-model")
    monkeypatch.setenv("EASYLEARN_PROVIDERS__CHAT__API_KEY", "private-env-key")
    monkeypatch.setenv("EASYLEARN_PROVIDERS__CHAT__GENERATION__TEMPERATURE", "0.5")
    monkeypatch.setenv("EASYLEARN_MINERU__LIMITS__REQUEST_TIMEOUT_SECONDS", "45")
    settings = Settings(providers={"chat": {"model": "constructor-chat-model"}})
    assert settings.providers["chat"].model == "constructor-chat-model"
    assert Settings().providers["chat"].model == "env-chat-model"
    assert settings.providers["chat"].generation.temperature == 0.5
    assert settings.providers["chat"].generation.max_output_tokens == 4096
    assert settings.providers["chat"].api_key.get_secret_value() == "private-env-key"
    assert "private-env-key" not in repr(settings)
    assert settings.mineru.limits.request_timeout_seconds == 45
    assert settings.models_root == root
    assert settings.local_models["mineru_vlm"].path == root / "MinerU2.5-Pro-2605-1.2B"
    assert settings.local_models["chat_weights"].path == absolute_chat
    assert not root.exists()
    assert not absolute_chat.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://user:private-password@127.0.0.1/service",
        "http://127.0.0.1/service?api_key=private-password",
        "http://127.0.0.1/service#private-password",
    ],
)
@pytest.mark.parametrize("endpoint", ["mineru", "mineru_backend"])
def test_mineru_connections_share_service_url_validation_without_exposing_input(url, endpoint):
    mineru = {"base_url": "http://127.0.0.1/mineru", "profile_revision": "v1"}
    if endpoint == "mineru":
        mineru["base_url"] = url
    else:
        mineru["parse"] = {"backend": "vlm-http-client", "server_url": url}
    with pytest.raises(ValidationError) as error:
        Settings(database_url="postgresql+asyncpg://user@localhost/test", mineru=mineru)
    assert "private-password" not in str(error.value)


def test_mineru_table_limits_use_the_shared_file_configuration(tmp_path):
    (tmp_path / "config.toml").write_text(
        'database_url = "postgresql+asyncpg://user@localhost/test"\n'
        '[mineru]\nbase_url = "http://127.0.0.1:8001"\nprofile_revision = "v1"\n'
        "[mineru.table_limits]\nmax_cells = 2500\nmax_columns = 100\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.mineru.table_limits.max_cells == 2500
    assert settings.mineru.table_limits.max_columns == 100


@pytest.mark.parametrize("suffix", ["toml", "yaml"])
def test_image_limits_load_from_file_and_keep_nested_overrides(tmp_path, monkeypatch, suffix):
    config = {
        "toml": 'database_url = "postgresql+asyncpg://user@localhost/test"\n'
        "[image_limits]\nmax_pixels = 900\nmax_frames = 3\nmax_total_pixels = 2400\n",
        "yaml": "database_url: postgresql+asyncpg://user@localhost/test\n"
        "image_limits:\n  max_pixels: 900\n  max_frames: 3\n  max_total_pixels: 2400\n",
    }
    (tmp_path / f"config.{suffix}").write_text(config[suffix], encoding="utf-8")
    monkeypatch.setenv("EASYLEARN_IMAGE_LIMITS__MAX_FRAMES", "2")
    settings = Settings()
    assert settings.image_limits.max_pixels == 900
    assert settings.image_limits.max_frames == 2
    assert settings.image_limits.max_total_pixels == 2400
    assert Settings(image_limits={"max_frames": 1}).image_limits.max_frames == 1
    with pytest.raises(ValidationError):
        Settings(image_limits={"max_total_pixels": 0})
