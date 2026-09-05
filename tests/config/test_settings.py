import os
from pathlib import Path

import pytest
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
