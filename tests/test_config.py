import os
from datetime import date
from pathlib import Path

import pytest

from flashcards.config import Settings, load_settings
from flashcards.errors import ConfigError


def test_defaults():
    settings = load_settings()
    assert settings.model == "claude-haiku-5-5"
    assert settings.db_path == Path("flashcards.db")
    assert settings.api_key is None
    assert settings.today is None


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("FLASHCARDS_MODEL", "some-model")
    monkeypatch.setenv("FLASHCARDS_DB", "other.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-env")
    settings = load_settings()
    assert settings == Settings(
        model="some-model", db_path=Path("other.db"), api_key="key-from-env"
    )


def test_dotenv_in_working_directory_is_read(tmp_path):
    (tmp_path / ".env").write_text(
        "FLASHCARDS_MODEL=model-from-file\nFLASHCARDS_DB=file.db\nANTHROPIC_API_KEY=file-key\n"
    )
    settings = load_settings()
    assert settings.model == "model-from-file"
    assert settings.db_path == Path("file.db")
    assert settings.api_key == "file-key"


def test_real_env_var_beats_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FLASHCARDS_MODEL=model-from-file\nFLASHCARDS_DB=file.db\n")
    monkeypatch.setenv("FLASHCARDS_MODEL", "model-from-env")
    settings = load_settings()
    assert settings.model == "model-from-env"
    assert settings.db_path == Path("file.db")  # still taken from the file


def test_empty_values_count_as_not_set(tmp_path):
    (tmp_path / ".env").write_text("FLASHCARDS_MODEL=\nANTHROPIC_API_KEY=\n")
    settings = load_settings()
    assert settings.model == "claude-haiku-5-5"
    assert settings.api_key is None


def test_explicit_environ_mapping_is_used(tmp_path):
    (tmp_path / ".env").write_text("FLASHCARDS_MODEL=model-from-file\n")
    settings = load_settings({"FLASHCARDS_MODEL": "given"})
    assert settings.model == "given"


def test_repr_does_not_contain_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-key")
    settings = load_settings()
    assert settings.api_key == "super-secret-key"
    assert "super-secret-key" not in repr(settings)


def test_os_environ_is_not_modified(tmp_path):
    (tmp_path / ".env").write_text("FLASHCARDS_MODEL=model-from-file\nANTHROPIC_API_KEY=k\n")
    before = dict(os.environ)
    load_settings()
    assert dict(os.environ) == before


def test_empty_real_env_var_does_not_hide_a_dotenv_value(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FLASHCARDS_MODEL=model-from-file\n")
    monkeypatch.setenv("FLASHCARDS_MODEL", "")
    assert load_settings().model == "model-from-file"


def test_today_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FLASHCARDS_TODAY", "2026-11-02")
    assert load_settings().today == date(2026, 11, 2)


@pytest.mark.parametrize("value", ["tomorrow", "2026-13-01", "02/11/2026"])
def test_invalid_today_is_a_config_error(monkeypatch, value):
    monkeypatch.setenv("FLASHCARDS_TODAY", value)
    with pytest.raises(ConfigError, match="FLASHCARDS_TODAY"):
        load_settings()
