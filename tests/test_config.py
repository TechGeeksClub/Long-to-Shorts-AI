import os
from pathlib import Path

from app.config import HUGGINGFACE_CACHE_ENV_VARS, Settings


def clear_huggingface_cache_env(monkeypatch) -> None:
    for name in (*HUGGINGFACE_CACHE_ENV_VARS, "LTS_HF_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_huggingface_cache_defaults_to_data_dir(tmp_path: Path, monkeypatch) -> None:
    clear_huggingface_cache_env(monkeypatch)
    settings = Settings(data_dir=tmp_path / "data")

    settings.ensure_directories()

    assert os.environ["HF_HOME"] == str(settings.huggingface_cache_dir)
    assert os.environ["HF_HUB_CACHE"] == str(settings.huggingface_cache_dir / "hub")
    assert (settings.huggingface_cache_dir / "hub").is_dir()


def test_invalid_huggingface_cache_env_falls_back_to_data_dir(
    tmp_path: Path, monkeypatch
) -> None:
    clear_huggingface_cache_env(monkeypatch)
    invalid_cache = tmp_path / "cache-file"
    invalid_cache.write_text("", encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setenv("HF_HOME", str(invalid_cache))

    settings.ensure_directories()

    assert os.environ["HF_HOME"] == str(settings.huggingface_cache_dir)
    assert os.environ["HF_HUB_CACHE"] == str(settings.huggingface_cache_dir / "hub")
    assert (settings.huggingface_cache_dir / "hub").is_dir()


def test_valid_huggingface_cache_env_is_preserved(tmp_path: Path, monkeypatch) -> None:
    clear_huggingface_cache_env(monkeypatch)
    custom_cache = tmp_path / "custom-cache"
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setenv("HF_HOME", str(custom_cache))

    settings.ensure_directories()

    assert os.environ["HF_HOME"] == str(custom_cache)
    assert custom_cache.is_dir()
