"""Config validation tests."""
import pytest

from src.config import Config, load_config, validate_config


def test_validate_missing_key():
    cfg = Config(groq_api_key="")
    problems = validate_config(cfg)
    assert problems and "GROQ_API_KEY" in problems[0]


def test_validate_ok():
    cfg = Config(groq_api_key="sk-test")
    assert validate_config(cfg) == []


def test_vad_filter_defaults_off():
    cfg = Config()
    assert cfg.vad_filter is False


def test_load_config_uses_env():
    import os
    os.environ["GROQ_API_KEY"] = "sk-env"
    cfg = load_config()
    assert cfg.groq_api_key == "sk-env"
    assert cfg.mode in ("hold", "tap")

    os.environ["MODE"] = "tap"
    os.environ["LOCAL_ENGINE"] = "true"
    os.environ["CLEANUP_MODEL"] = "none"
    cfg = load_config()
    assert cfg.mode == "tap"
    assert cfg.local_engine is True
    assert cfg.cleanup_model is None