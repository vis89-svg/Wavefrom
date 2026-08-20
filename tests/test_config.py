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


def test_domain_hint_defaults_empty():
    cfg = Config()
    assert cfg.domain_hint == ""


def test_glossary_defaults_empty():
    cfg = Config()
    assert cfg.glossary == []


def test_language_defaults_english():
    cfg = Config()
    assert cfg.language == "en"


def test_load_config_reads_language_env():
    import os
    os.environ["LANGUAGE"] = "hi"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("LANGUAGE", None)
    assert cfg.language == "hi"


def test_load_config_language_none_overrides():
    import os
    os.environ["LANGUAGE"] = "none"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("LANGUAGE", None)
    assert cfg.language is None


def test_load_config_reads_glossary_env():
    import os
    os.environ["GLOSSARY"] = "Razorpay, Lorem Ipsum,  stripe"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("GLOSSARY", None)
    assert cfg.glossary == ["Razorpay", "Lorem Ipsum", "stripe"]


def test_verify_defaults_on():
    cfg = Config()
    assert cfg.verify is True
    assert cfg.verify_model is None


def test_load_config_reads_verify_env():
    import os
    os.environ["VERIFY"] = "0"
    os.environ["VERIFY_MODEL"] = "whisper-large-v3"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("VERIFY", None)
        os.environ.pop("VERIFY_MODEL", None)
    assert cfg.verify is False
    assert cfg.verify_model == "whisper-large-v3"


def test_load_config_verify_model_none():
    import os
    os.environ["VERIFY_MODEL"] = "none"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("VERIFY_MODEL", None)
    assert cfg.verify is True
    assert cfg.verify_model is None


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


def test_load_config_reads_domain_hint():
    import os
    os.environ["DOMAIN_HINT"] = "software development"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("DOMAIN_HINT", None)
    assert cfg.domain_hint == "software development"


def test_polish_cleanup_mode_accepted():
    import os
    os.environ["CLEANUP_MODE"] = "polish"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("CLEANUP_MODE", None)
    assert cfg.cleanup_mode == "polish"


def test_invalid_cleanup_mode_falls_back_to_correcting():
    import os
    os.environ["CLEANUP_MODE"] = "banana"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("CLEANUP_MODE", None)
    assert cfg.cleanup_mode == "correcting"


def test_get_api_key_handles_bom_env(monkeypatch, tmp_path):
    # .env written by some editors starts with a UTF-8 BOM, which python-dotenv
    # can't parse on the first line; get_api_key must still find the key.
    from src.config import get_api_key

    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfGROQ_API_KEY=gsk-bom-key\n")
    monkeypatch.setattr("src.config.ENV_PATH", env)
    monkeypatch.setattr("src.config._HAS_KEYRING", False)
    monkeypatch.setattr("src.config.keyring", None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert get_api_key() == "gsk-bom-key"