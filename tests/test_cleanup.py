"""Cleanup prompt/mode selection tests (no network)."""
from src.cleanup import (CONSERVATIVE_PROMPT, CORRECTING_PROMPT, RECONCILE_PROMPT,
                         SYSTEM_PROMPT, CleanupClient)
from src.merge import Dispute


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
    assert "NEVER change proper nouns" in client.system_prompt
    assert "When in doubt" in client.system_prompt


def test_correcting_prompt_fixes_common_confusions():
    assert "NEVER change proper nouns" in CORRECTING_PROMPT
    assert "NEVER hallucinate or invent words" in CORRECTING_PROMPT
    assert "When in doubt" in CORRECTING_PROMPT


def test_glossary_added_to_correcting_prompt():
    client = CleanupClient(api_key="test-key", glossary=["Razorpay", "Lorem Ipsum"])
    prompt = client.system_prompt
    assert prompt.startswith(CORRECTING_PROMPT)
    assert "MUST appear in your output" in prompt
    assert "Razorpay, Lorem Ipsum" in prompt


def test_glossary_added_to_conservative_prompt():
    client = CleanupClient(api_key="test-key", mode="conservative",
                           glossary=["Razorpay"])
    prompt = client.system_prompt
    assert prompt.startswith(CONSERVATIVE_PROMPT)
    assert "MUST appear in your output" in prompt
    assert "Razorpay" in prompt


def test_glossary_strips_and_deduplicates():
    client = CleanupClient(api_key="test-key", glossary=[" Razorpay ", "", "Razorpay"])
    assert client.glossary == ["Razorpay"]
    assert client.system_prompt.count("Razorpay") == 1


def test_no_glossary_leaves_prompt_unchanged():
    client = CleanupClient(api_key="test-key", glossary=[])
    assert client.system_prompt == CORRECTING_PROMPT


def test_system_prompt_alias_is_correcting():
    assert SYSTEM_PROMPT == CORRECTING_PROMPT


def test_prompts_guard_against_omission():
    assert "Never omit" in CONSERVATIVE_PROMPT
    assert "Never omit" in CORRECTING_PROMPT


def test_reconcile_prompt_is_strict():
    assert '": A"' in RECONCILE_PROMPT or '"<number>: A"' in RECONCILE_PROMPT
    assert "never introduce new words" in RECONCILE_PROMPT
    assert "one line per dispute" in RECONCILE_PROMPT


def _dispute(index, primary, verify, plow=False, vlow=False):
    return Dispute(index=index, prefix="ctx before ", primary_text=primary,
                   verify_text=verify, suffix=" ctx after",
                   primary_low_conf=plow, verify_low_conf=vlow)


def test_reconcile_parses_choices(monkeypatch):
    client = CleanupClient(api_key="test-key")

    class FakeResponse:
        choices = [type("C", (), {"message": type("M", (), {
            "content": "0: B\n1: A\n"})})()]

    monkeypatch.setattr(
        client._client.chat.completions, "create",
        lambda **kw: FakeResponse())
    choices = client.reconcile([
        _dispute(0, "four web applications", "whole application"),
        _dispute(1, "business strategy", "business logic"),
    ])
    assert choices == {0: "B", 1: "A"}


def test_reconcile_ignores_garbage_lines(monkeypatch):
    client = CleanupClient(api_key="test-key")

    class FakeResponse:
        choices = [type("C", (), {"message": type("M", (), {
            "content": "0: B\nnoise line\n7: Z\nx: A\n"})})()]

    monkeypatch.setattr(
        client._client.chat.completions, "create",
        lambda **kw: FakeResponse())
    choices = client.reconcile([_dispute(0, "a", "b")])
    assert choices == {0: "B"}


def test_reconcile_returns_empty_on_error(monkeypatch):
    client = CleanupClient(api_key="test-key")

    def boom(**kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(client._client.chat.completions, "create", boom)
    assert client.reconcile([_dispute(0, "a", "b")]) == {}


def test_reconcile_empty_disputes():
    client = CleanupClient(api_key="test-key")
    assert client.reconcile([]) == {}


def test_reconcile_prompt_includes_confidence_note(monkeypatch):
    client = CleanupClient(api_key="test-key")
    sent = {}

    class FakeResponse:
        choices = [type("C", (), {"message": type("M", (), {"content": ""})})()]

    def capture(**kw):
        sent["user"] = kw["messages"][1]["content"]
        return FakeResponse()

    monkeypatch.setattr(client._client.chat.completions, "create", capture)
    client.reconcile([_dispute(0, "x", "y", plow=True, vlow=True)])
    assert "low audio confidence" in sent["user"]
    assert "0: A: 'x' | B: 'y'" in sent["user"]


def test_reconcile_prompt_includes_glossary(monkeypatch):
    client = CleanupClient(api_key="test-key", glossary=["Razorpay"])
    sent = {}

    class FakeResponse:
        choices = [type("C", (), {"message": type("M", (), {"content": ""})})()]

    def capture(**kw):
        sent["system"] = kw["messages"][0]["content"]
        return FakeResponse()

    monkeypatch.setattr(client._client.chat.completions, "create", capture)
    client.reconcile([_dispute(0, "resopay", "razorpay")])
    assert "MUST appear in your output" in sent["system"]
    assert "Razorpay" in sent["system"]
