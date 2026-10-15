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


def test_verify_defaults_off():
    cfg = Config()
    assert cfg.verify is False
    assert cfg.verify_model is None


def test_v3_settings_migrate_to_accuracy_defaults(tmp_path, monkeypatch):
    import json
    import src.config as config
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 3, "verify": True,
                                "cleanup_model": "openai/gpt-oss-20b",
                                "hotkey": "ctrl+space"}))
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    s = config.load_settings()
    assert s.cleanup_model == config.DEFAULT_CLEANUP_MODEL
    assert s.verify is False
    assert s.final_model == "whisper-large-v3"
    assert s.hotkey == "ctrl+space"  # user choices kept
    assert json.loads(path.read_text())["version"] == config.SETTINGS_VERSION


def test_v3_migration_keeps_user_picked_cleanup_model(tmp_path, monkeypatch):
    import json
    import src.config as config
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 3,
                                "cleanup_model": "openai/gpt-oss-120b"}))
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    assert config.load_settings().cleanup_model == "openai/gpt-oss-120b"


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
    assert cfg.verify is False
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


def test_polish_model_defaults_to_120b():
    cfg = Config()
    assert cfg.polish_model == "openai/gpt-oss-120b"


def test_load_config_reads_polish_model_env():
    import os
    os.environ["POLISH_MODEL"] = "openai/gpt-oss-20b"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("POLISH_MODEL", None)
    assert cfg.polish_model == "openai/gpt-oss-20b"


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