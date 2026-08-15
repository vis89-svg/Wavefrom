"""Cleanup prompt/mode selection tests (no network)."""
from src.cleanup import (CONSERVATIVE_PROMPT, CORRECTING_PROMPT, SYSTEM_PROMPT,
                         CleanupClient)


def test_default_mode_is_correcting():
    client = CleanupClient(api_key="test-key")
    assert client.mode == "correcting"


def test_invalid_mode_falls_back_to_correcting():
    client = CleanupClient(api_key="test-key", mode="banana")
    assert client.mode == "correcting"


def test_conservative_mode_picks_conservative_prompt():
    client = CleanupClient(api_key="test-key", mode="conservative")
    assert client.system_prompt == CONSERVATIVE_PROMPT
    assert "Preserve every word as spoken" in client.system_prompt


def test_correcting_prompt_has_guardrails():
    client = CleanupClient(api_key="test-key", mode="correcting")
    assert client.system_prompt == CORRECTING_PROMPT
    assert "Do NOT invent content" in client.system_prompt
    assert "obvious from context" in client.system_prompt


def test_system_prompt_alias_is_correcting():
    assert SYSTEM_PROMPT == CORRECTING_PROMPT


def test_prompts_guard_against_omission():
    assert "Never omit" in CONSERVATIVE_PROMPT
    assert "Never omit" in CORRECTING_PROMPT
