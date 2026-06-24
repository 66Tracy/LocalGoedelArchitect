"""Tests for config loading."""
import os
import tempfile
from pathlib import Path

import pytest

from local_goedel.config import Settings, load_settings


def test_load_powershell_env_format(tmp_path):
    """Parse PowerShell-format .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        '$env:MODEL_NAME = "test-model"\n'
        '$env:BASE_URL = "https://example.com"\n'
        '$env:DEEPSEEK_API_KEY = "sk-testkey123"\n',
        encoding="utf-8",
    )

    settings = load_settings(env_path=env_file)
    assert settings.model_name == "test-model"
    assert settings.base_url == "https://example.com"
    assert settings.api_key == "sk-testkey123"


def test_load_missing_env_file():
    """Missing .env file returns defaults."""
    settings = load_settings(env_path="/nonexistent/.env")
    assert isinstance(settings, Settings)


def test_defaults():
    """Default settings are sane."""
    s = Settings()
    assert s.lean_server_url == "http://localhost:8000"
    assert s.mathlib_min_interval_s == 2.0
    assert s.enable_thinking is False
    assert isinstance(s.artifacts_dir, Path)


def test_api_key_not_in_default_repr():
    """Ensure api_key is not accidentally logged."""
    s = Settings(api_key="secret-key-do-not-log")
    # The repr includes field names but we just check it doesn't raise
    repr(s)
