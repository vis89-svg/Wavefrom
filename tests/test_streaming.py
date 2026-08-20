"""Tests for the streaming engine with mocked transcriber + injector."""
import io
import math
import wave

import numpy as np

import src.streaming as streaming
from src.config import Config
from src.streaming import (DictationEngine, _is_silent, _to_wav, pcm_from_wav,
                           slice_audio)


class FakeTranscriber:
    def __init__(self, map_by_slice):
        self._map = map_by_slice
        self.calls = []
        self.kwargs = []

    def transcribe_bytes(self, audio_bytes, **kwargs):
        self.calls.append(len(audio_bytes))
        self.kwargs.append(kwargs)
        idx = len(self.calls) - 1
        return self._map[min(idx, len(self._map) - 1)]


class FakeInjector:
    def __init__(self):
        self.parts = []
        self.deleted = 0

    def inject_text(self, text):
        self.parts.append(text)

    def delete_chars(self, n):
        self.deleted += n

    @property
    def typed(self):
        out = ""
        for p in self.parts:
            if out and not out.endswith(" ") and not p.startswith(" "):
                out += " "
            out += p
        return out


def make_config():
    return Config(
        groq_api_key="test-key",
        hotkey="ctrl+space",
        sample_rate=16000,
        local_engine=True,
        toasts=False,
        tray=False,
    )


def make_engine(transcriber, injector=None):
    config = make_config()
    engine = DictationEngine(config, transcriber, cleaner=None, injector=injector,
                             notify=None, tray=None, local_engine=None)
    return engine


def test_silence_vs_speech():
    rate = 16000
    assert _is_silent(np.zeros(rate, dtype=np.int16), rate)
    tone = (np.sin(np.arange(rate) / 8) * 8000).astype(np.int16)
    assert not _is_silent(tone, rate)


def test_to_wav_payload():
    rate = 16000
    tone = (np.sin(np.arange(int(rate * 0.1)) / 8) * 5000).astype(np.int16)
    payload = _to_wav(tone, rate)
    assert payload[:4] == b"RIFF"
    import wave
    import io

    with wave.open(io.BytesIO(payload), "rb") as w:
        assert w.getframerate() == rate
        assert w.getnframes() == len(tone)


def test_worker_slices_merge_and_type():
    transcriber = FakeTranscriber([
        "hello world",
        "world this is streaming",
        "this is streaming dictation",
    ])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine.set_hold_active(False)  # hotkey released
    engine._slice_q.put(None)  # finalize
    engine._worker.join(timeout=5)

    assert engine._committed == ["hello", "world", "this", "is", "streaming", "dictation"]
    typed = injector.typed
    assert "hello world this is streaming dictation" in typed
    assert typed.endswith(".")


def test_finalize_full_audio_merges_missed_content():
    # The slice pass never hears "this is streaming"; only the full-audio pass
    # does. The union final text must keep it (no content replacement).
    transcriber = FakeTranscriber([
        "hello world goodbye",
        "hello world this is streaming goodbye",
    ])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine.set_hold_active(False)  # hotkey released
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert "this is streaming" in engine.status.committed_text
    assert "hello world this is streaming goodbye." == engine.status.committed_text
    assert injector.deleted > 0


def test_finalize_cleanup_replaces_text():
    transcriber = FakeTranscriber(["um hello world"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def clean(self, raw: str, app_hint: str | None = None) -> str:
            return "hello world, everyone here."

    engine._cleaner = FakeCleaner()
    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine.set_hold_active(False)  # hotkey released
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "hello world, everyone here."
    assert injector.deleted > 0
    assert any(", everyone here." in p for p in injector.parts)


def test_slice_failure_records_error_and_notifies():
    class BrokenTranscriber:
        def transcribe_bytes(self, audio_bytes, **kwargs):
            raise RuntimeError("429 rate limited")

    toasts = []

    engine = make_engine(BrokenTranscriber(), None)
    engine._notify = lambda title, msg: toasts.append((title, msg))
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert any("Transcription failed" in msg for _t, msg in toasts)
    # with nothing committed, finalize returns to idle without crashing
    assert engine.status.state == "idle"


def test_slice_retries_once_on_rate_limit(monkeypatch):
    class FlakyTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe_bytes(self, audio_bytes, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 rate limited")
            return "hello world"

    t = FlakyTranscriber()
    engine = make_engine(t, None)
    monkeypatch.setattr("src.streaming.time.sleep", lambda s: None)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert t.calls == 2
    assert engine._committed == ["hello", "world"]
    assert engine.status.state == "idle"


def test_chunked_final_union_recovers_dropped_sentence():
    # Slices omit "this is a test"; the full-audio chunk has it. The union of
    # both passes must keep it — a dropped sentence is never lost.
    transcriber = FakeTranscriber([
        "hello world",                              # slice 1
        "world goodbye world",                      # slice 2
        "hello world this is a test goodbye world", # full-audio chunk
    ])
    engine = make_engine(transcriber, None)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "hello world this is a test goodbye world."


def test_full_audio_chunked_into_overlapping_windows():
    rate = 16000
    transcriber = FakeTranscriber([
        "hello world",                  # chunk 1 [0s,20s)
        "world this is a test",         # chunk 2 [18s,38s)
        "test goodbye",                 # chunk 3 [36s,42s)
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._full_audio = _to_wav(np.zeros(int(rate * 42), dtype=np.int16), rate)
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # 42s -> 3 chunks (FINAL_CHUNK_SECS=20, overlap 2s), overlap-diff stitched
    # with no duplication/loss
    assert len(transcriber.calls) == 3
    assert engine.status.committed_text == "hello world this is a test goodbye."


class _FakeStream:
    def __init__(self, chunk, speak_chunks, silent_chunks, resume_speak=0):
        self._chunk = chunk
        self._speak = speak_chunks
        self._silent = silent_chunks
        self._resume = resume_speak

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if self._speak > 0:
            self._speak -= 1
            return (np.sin(np.arange(n) / 8) * 8000).astype(np.int16), None
        if self._silent > 0:
            self._silent -= 1
            return np.zeros(n, dtype=np.int16), None
        if self._resume > 0:
            self._resume -= 1
            return (np.sin(np.arange(n) / 8) * 8000).astype(np.int16), None
        raise StopIteration


def test_hold_mode_ignores_silence_autostop(monkeypatch):
    rate = 16000
    chunk = int(rate * 0.03)
    engine = make_engine(FakeTranscriber(["hello world"]))
    monkeypatch.setattr("src.streaming.sd.InputStream",
                        lambda **kw: _FakeStream(chunk, speak_chunks=10,
                                                 silent_chunks=120))
    engine.start()
    try:
        engine.capture()
    except StopIteration:
        pass
    # 120 silent chunks (~3.6s) exceeds MAX_SILENCE_SECS, but hold mode must
    # keep recording (only hotkey release / MAX_RECORD_SECS ends capture).
    assert not engine._stop_capture.is_set()


def test_hold_mode_capture_survives_pause_then_resumes(monkeypatch):
    rate = 16000
    chunk = int(rate * 0.03)
    engine = make_engine(FakeTranscriber(["hello world"]))
    # Speak, then ~4s of silence (far beyond SILENCE_PERIOD_SECS), then speak
    # again. Hold mode must still be recording after the resumed speech.
    monkeypatch.setattr("src.streaming.sd.InputStream",
                        lambda **kw: _FakeStream(chunk, speak_chunks=10,
                                                 silent_chunks=134,
                                                 resume_speak=10))
    engine.start()
    try:
        engine.capture()
    except StopIteration:
        pass
    assert not engine._stop_capture.is_set()


def test_pause_period_does_not_drop_post_pause_content():
    # A >1s pause sets _pending_period (a "." is typed at the pause). The
    # resumed speech slice must still be merged and kept — content spoken after
    # the pause is never lost.
    transcriber = FakeTranscriber([
        "",                     # silence during the pause
        "and then more",        # speech resumed after the pause
    ])
    engine = make_engine(transcriber, None)
    engine._committed = ["hello", "world"]
    engine._pending_period = True

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))
    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert " ".join(engine._committed) == "hello world . and then more"


def test_tap_mode_stops_after_silence(monkeypatch):
    rate = 16000
    chunk = int(rate * 0.03)
    engine = make_engine(FakeTranscriber(["hello world"]))
    engine._config.mode = "tap"
    monkeypatch.setattr("src.streaming.sd.InputStream",
                        lambda **kw: _FakeStream(chunk, speak_chunks=10,
                                                 silent_chunks=120))
    engine.start()
    engine.capture()
    assert engine._stop_capture.is_set()


def test_slice_audio_matches_capture_windows():
    rate = 16000
    pcm = np.zeros(int(rate * 7.0), dtype=np.int16)
    slices = slice_audio(pcm, rate)
    n_expected = math.ceil(7.0 / streaming.SLICE_SECS)
    assert len(slices) == n_expected
    sizes = []
    for s in slices:
        with wave.open(io.BytesIO(s), "rb") as w:
            sizes.append(w.getnframes())
    # first window is SLICE_SECS (no backlog yet); later windows are capped at
    # SLICE_SECS + OVERLAP_SECS of retained audio.
    assert sizes[0] == int(rate * streaming.SLICE_SECS)
    assert sizes[1] == int(rate * (streaming.SLICE_SECS + streaming.OVERLAP_SECS))


def test_pcm_from_wav_roundtrip():
    rate = 16000
    tone = (np.sin(np.arange(1000) / 8) * 5000).astype(np.int16)
    pcm, r = pcm_from_wav(_to_wav(tone, rate))
    assert r == rate
    assert np.array_equal(pcm, tone)


def test_domain_hint_biases_slice_and_full_audio_prompts():
    # The optional domain hint is prepended to the Whisper context prompt for
    # both the per-slice calls and the final full-audio chunk calls.
    transcriber = FakeTranscriber([
        "hello world",                       # slice
        "hello world this is streaming",     # full-audio chunk
    ])
    engine = make_engine(transcriber, None)
    engine._config.domain_hint = "software development"

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # slice prompt: hint only (nothing committed yet)
    assert transcriber.kwargs[0]["prompt"] == "software development"
    # full-audio chunk prompt: hint + last committed words
    chunk_prompt = transcriber.kwargs[1]["prompt"]
    assert chunk_prompt.startswith("software development")
    assert "hello world" in chunk_prompt
    assert engine.status.state == "idle"


def test_no_hint_and_empty_committed_omits_prompt():
    transcriber = FakeTranscriber(["hello world"])
    engine = make_engine(transcriber, None)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert transcriber.kwargs[0]["prompt"] is None


def test_glossary_biases_slice_and_full_audio_prompts():
    transcriber = FakeTranscriber([
        "hello world",                       # slice
        "hello world this is streaming",     # full-audio chunk
    ])
    engine = make_engine(transcriber, None)
    engine._config.domain_hint = "software development"
    engine._config.glossary = ["Razorpay", "Lorem Ipsum"]

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    for prompt in transcriber.kwargs[0]["prompt"], transcriber.kwargs[1]["prompt"]:
        assert "Razorpay" in prompt
        assert "Lorem Ipsum" in prompt
    # hint comes first, glossary after, then committed words
    assert transcriber.kwargs[0]["prompt"].split(" Razorpay ")[0] == "software development"


def test_prompt_capped_when_glossary_long(monkeypatch):
    from src.streaming import MAX_PROMPT_CHARS

    transcriber = FakeTranscriber(["hello world"])
    engine = make_engine(transcriber, None)
    engine._config.glossary = [f"term{i}" for i in range(100)]
    engine._committed = ["word"] * 50

    prompt = engine._context_prompt()
    assert prompt is not None
    assert len(prompt) <= MAX_PROMPT_CHARS
    # the committed-words tail is dropped before the glossary bias is
    assert "word" not in prompt


class FakeCleaner:
    def __init__(self, choices=None):
        self.choices = choices or {}
        self.reconciled = None

    def clean(self, raw, app_hint: str | None = None):
        return raw

    def reconcile(self, disputes):
        self.reconciled = disputes
        return self.choices


def test_verify_pass_reconciles_substitution():
    # slice: "hello world"; primary full pass mis-hears "four web apps are";
    # verify pass hears "whole app is" and reconciliation picks B.
    transcriber = FakeTranscriber([
        "hello world",                          # slice
        "hello the four web apps are gone",     # primary full chunk
        "hello the whole app is gone",          # verify chunk (alt model)
    ])
    engine = make_engine(transcriber, None)
    engine._cleaner = FakeCleaner(choices={0: "B"})

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # 3 transcribe calls: slice + primary chunk + verify chunk
    assert len(transcriber.calls) == 3
    # verify chunk used the alternate model at higher temperature
    assert transcriber.kwargs[2]["model"] == "whisper-large-v3"
    assert transcriber.kwargs[2]["temperature"] == 0.4
    assert len(engine._last_disputes) == 1
    # reconciled: verify wording chosen, hallucinated "four web apps" gone
    assert "whole app is gone" in engine.status.committed_text
    assert "four web apps" not in engine.status.committed_text


def test_verify_skipped_when_disabled():
    transcriber = FakeTranscriber([
        "hello world",
        "hello the four web apps are gone",
    ])
    engine = make_engine(transcriber, None)
    engine._config.verify = False
    engine._cleaner = FakeCleaner()

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert len(transcriber.calls) == 2  # no verify chunk
    assert engine._last_disputes == []
    assert "four web apps" in engine.status.committed_text


def test_verify_reconcile_failure_keeps_primary():
    # If reconciliation throws, the primary wording must survive untouched.
    transcriber = FakeTranscriber([
        "hello world",
        "hello the four web apps are gone",
        "hello the whole app is gone",
    ])
    engine = make_engine(transcriber, None)

    class BoomCleaner:
        def clean(self, raw):
            return raw

        def reconcile(self, disputes):
            raise RuntimeError("boom")

    engine._cleaner = BoomCleaner()
    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert len(engine._last_disputes) == 1
    assert "four web apps" in engine.status.committed_text


# ------------------------------------------------------- echo / past-content leak


def test_start_resets_state_between_dictations():
    # start() must clear all per-dictation state so nothing from a previous
    # session leaks into the next prompt/merge.
    engine = make_engine(FakeTranscriber(["x"]), None)
    engine._committed = ["stale", "words"]
    engine._typed_text = "stale words"
    engine._full_audio = b"old"
    engine._last_disputes = ["old"]
    engine.start()
    assert engine._committed == []
    assert engine._typed_text == ""
    assert engine._full_audio is None
    assert engine._last_disputes == []
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)


def test_two_dictations_do_not_contaminate_each_other():
    # Dictation 2 must never contain words from dictation 1.
    engine = make_engine(FakeTranscriber(["first session words here"]), None)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)
    assert " ".join(engine._committed) == "first session words here"

    engine._transcriber = FakeTranscriber(["second session"])
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)
    assert " ".join(engine._committed) == "second session"
    assert engine.status.committed_text == "second session."
    assert "first" not in engine.status.committed_text
    assert "session" not in engine.status.committed_text.replace("second session", "")


def test_prompt_echo_slice_is_not_retyped():
    # A transcript that is only the words we sent as the Whisper prompt is the
    # model echoing the prompt back — nothing new, nothing typed.
    words = ("the quick brown fox jumps over the lazy dog and runs away "
             "into the forest").split()
    engine = make_engine(FakeTranscriber([" ".join(words)]), None)
    engine._committed = list(words)
    engine._typed_text = " ".join(words)

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert engine._committed == list(words)
    assert engine._typed_text == " ".join(words)


def test_slice_tail_that_is_prompt_echo_is_not_typed():
    # "brown fox jumps over" re-covers already-typed words (overlap); the tail
    # "the quick brown fox" is a prompt echo and must not be typed/committed.
    engine = make_engine(FakeTranscriber(
        ["brown fox jumps over the quick brown fox"]), None)
    engine._committed = ["the", "quick", "brown", "fox", "jumps", "over"]
    engine._typed_text = "the quick brown fox jumps over"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert engine._committed == ["the", "quick", "brown", "fox", "jumps", "over"]
    assert engine._typed_text == "the quick brown fox jumps over"


def test_slice_with_adjacent_loop_is_collapsed():
    # Whisper looping "we are done" back to back must type only one copy.
    injector = FakeInjector()
    engine = make_engine(FakeTranscriber(["we are done we are done"]), injector)
    engine._config.mode = "tap"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert " ".join(engine._committed) == "we are done"
    assert injector.typed.strip() == "we are done"


def test_slice_short_stutter_is_preserved():
    # Real 1-2 word stutters are speech, never collapsed or dropped.
    injector = FakeInjector()
    engine = make_engine(FakeTranscriber(["no no no and then we left"]), injector)
    engine._config.mode = "tap"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert " ".join(engine._committed) == "no no no and then we left"
    assert injector.typed.strip() == "no no no and then we left"


def test_full_audio_chunk_trailing_echo_is_stripped():
    # A full-audio chunk that ends by echoing its own head must be deduped
    # before it becomes mid-text when chunk windows are stitched.
    transcriber = FakeTranscriber([
        "we go to the store every day we go to the store",
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "we go to the store every day."
    assert "we go to the store every day we go to the store" not in (
        engine.status.committed_text)


def test_final_strips_trailing_echo_from_slice_text():
    # A slice-level transcript with a trailing echo survives the slice pass
    # (it is not a subsequence of the empty prompt); the final assembly must
    # strip the echo before typing.
    transcriber = FakeTranscriber([
        "we go to the store every day we go to the store",
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "we go to the store every day."


def test_final_collapses_adjacent_loop():
    transcriber = FakeTranscriber([
        "the end is near the end is near",
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000 * 15, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "the end is near."


def test_modifiers_held_defer_partials_then_final_diff_types_missing_tail():
    # Bug 1 regression: in hold mode the user holds Ctrl+Win for the WHOLE
    # dictation, so every partial is deferred. _typed_text must reflect only
    # what was actually typed; the final diff must type everything missing.
    # (Old code updated _typed_text even when typing was skipped, so the final
    # diff saw no difference and the dictation was lost entirely.)
    transcriber = FakeTranscriber(["hello world", "this is dictation"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine.set_hold_active(True)
    engine.start()
    # Short audio (<12s) keeps the finalize on the streaming transcript.
    engine._full_audio = _to_wav(np.zeros(16000 * 5, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))

    assert injector.parts == []            # nothing typed while modifiers held
    assert engine._typed_text == ""        # bookkeeping matches reality

    # Modifiers released at finalize: the diff must type the whole text.
    engine.set_hold_active(False)
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert "hello world this is dictation" in injector.typed
    assert injector.typed.endswith(".")


def test_glossary_words_in_speech_are_not_dropped_as_echo():
    # Bug 2 regression: real speech made of glossary terms must NOT be flagged
    # as a "prompt echo". The old guard matched against the WHOLE prompt (hint
    # + glossary + committed tail), silently dropping genuine dictation like
    # "superintendent intimated institution" or "regular feed".
    config = make_config()
    config.glossary = ["superintendent", "intimated", "institution"]
    config.domain_hint = "superintendent intimated institution"
    injector = FakeInjector()
    engine = make_engine(
        FakeTranscriber(["superintendent intimated institution"]), injector)
    engine._config = config
    engine._config.mode = "tap"
    engine._committed = ["the", "meeting", "is", "at", "ten"]
    engine._typed_text = "the meeting is at ten"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert engine._committed == ["the", "meeting", "is", "at", "ten",
                                 "superintendent", "intimated", "institution"]
    assert "superintendent intimated institution" in injector.typed


def test_stop_never_started_returns_immediately():
    # Regression: stop() racing ahead of start() used to wait 10s on an event
    # that would never be set, stalling the hotkey hook thread.
    transcriber = FakeTranscriber(["x"])
    engine = make_engine(transcriber, None)
    engine.stop()
    assert engine._stop_capture.is_set()
    assert not engine._active.is_set()


def test_start_refused_while_busy():
    # Regression: a new dictation must never reset _committed/_typed_text while
    # the previous worker is still finalizing.
    transcriber = FakeTranscriber(["x"])
    engine = make_engine(transcriber, None)
    assert engine.start() is True
    assert engine.start() is False
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)
    assert engine.start() is True
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)


def test_finalize_early_types_raw_then_period_diff():
    # Hold-mode deferral leaves _typed_text empty; finalize must type the raw
    # text immediately (verbatim, no period) and let the final diff add the
    # period instead of waiting for the expensive pass.
    transcriber = FakeTranscriber(["irrelevant"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)
    engine._committed = ["hello", "world"]
    engine._typed_text = ""

    wav = _to_wav(np.zeros(16000, dtype=np.int16), 16000)  # 1s → short path
    engine._finalize(wav)

    assert injector.parts == ["hello world", "."]
    assert engine.status.committed_text == "hello world."


def test_finalize_early_type_then_cleanup_refines():
    # The early raw type must be replaced by the cleaned final via diff_plan
    # (delete the stale tail, type the cleaned text).
    transcriber = FakeTranscriber(["irrelevant"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def clean(self, raw: str, app_hint: str | None = None) -> str:
            return "hello world, everyone here."

    engine._cleaner = FakeCleaner()
    engine._committed = ["um", "hello", "world"]
    engine._typed_text = ""

    wav = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._finalize(wav)

    assert injector.parts[0] == "um hello world"
    assert injector.deleted == len("um hello world")
    assert injector.parts[-1] == "hello world, everyone here."


# ------------------------------------------------------- hold-mode deferral belt


def test_start_sets_hold_active_in_hold_mode():
    # Belt: in hold mode the engine defers live slice typing for the whole
    # recording even if the controller's set_hold_active(True) lands late.
    engine = make_engine(FakeTranscriber(["x"]))
    engine._config.mode = "hold"
    engine.start()
    assert engine._hold_active is True
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)


def test_start_clears_hold_active_in_tap_mode():
    # Tap mode records into live typing, so the belt must NOT defer.
    engine = make_engine(FakeTranscriber(["x"]))
    engine._config.mode = "tap"
    engine.start()
    assert engine._hold_active is False
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)


def test_hold_mode_post_release_slice_is_deferred_not_live_typed():
    # Bug regression: the tail slice transcribed right AFTER the release used
    # to be typed live, and its _typed_text = committed_text update claimed the
    # ENTIRE committed text was on screen when only the tail was typed. The
    # early-type then typed nothing (raw.startswith(typed)) and the final diff
    # skipped (to_delete > session ledger), so most of the dictation never got
    # typed and the screen did not match the corrected text. Hold mode must
    # defer every slice — even post-release — and let the finalize early-type
    # type the full text, bounded by the ledger.
    transcriber = FakeTranscriber(["hello world this is", "this is a test"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)
    engine._config.mode = "hold"

    engine.start()                    # belt sets _hold_active True
    engine._full_audio = _to_wav(np.zeros(16000 * 5, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))

    engine.set_hold_active(False)     # release; the next slice arrives post-release
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))

    assert injector.parts == []       # nothing typed live, even after release
    assert engine._typed_text == ""   # bookkeeping never claims committed text

    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert injector.parts == ["hello world this is a test", "."]


def test_tap_mode_typed_text_tracks_actual_screen_text():
    # Bookkeeping regression: _typed_text must equal the literal text injected
    # (each appended slice), never the whole committed text. Deferred content
    # may be in committed but absent from the screen, and the final diff only
    # ever deletes what this session actually typed.
    transcriber = FakeTranscriber(["hello world", "world this is streaming"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)
    engine._config.mode = "tap"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))
    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert injector.parts == ["hello world", " this is streaming"]
    assert engine._typed_text == "hello world this is streaming"
    assert engine._typed_chars == len("hello world this is streaming")


# ------------------------------------------------------------- echo tightening


def test_scattered_word_reuse_is_not_dropped_as_echo():
    # Bug regression: the old subsequence check matched scattered words, so
    # real speech that merely reused committed words in a new order was
    # dropped. Only a CONTIGUOUS repeat of the tail is an echo.
    engine = make_engine(FakeTranscriber(["dog the fox jumps"]), None)
    engine._committed = ["the", "quick", "brown", "fox", "jumps",
                         "over", "the", "lazy", "dog"]
    engine._typed_text = "the quick brown fox jumps over the lazy dog"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    # "dog the fox jumps" re-uses committed words but is not contiguous in the
    # tail — it is real speech and must be kept, not dropped as an echo.
    assert engine._committed == ["the", "quick", "brown", "fox", "jumps",
                                 "over", "the", "lazy", "dog",
                                 "the", "fox", "jumps"]


def test_real_speech_matching_short_prompt_is_not_dropped_as_echo():
    # Bug regression: the full-prompt ratio check flagged a slice that closely
    # matched a SHORT prompt (few hint/glossary/committed words). A long-enough
    # prompt requirement keeps genuine dictation of glossary topics.
    engine = make_engine(FakeTranscriber(["testing the report is due again"]),
                         None)
    engine._committed = ["testing", "the", "report", "is", "due"]
    engine._typed_text = "testing the report is due"

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert " ".join(engine._committed) == "testing the report is due again"


# ------------------------------------------------------- safe final correction


def test_finalize_never_deletes_more_than_session_typed():
    # Bug regression: bookkeeping claimed text "on screen" that this dictation
    # never actually typed (live slice text went out as Ctrl+Win shortcuts).
    # The old final diff backspaced it all, eating the PREVIOUS dictation's
    # text. The ledger bounds the correction to what this session injected.
    transcriber = FakeTranscriber(["irrelevant"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def clean(self, raw: str, app_hint: str | None = None) -> str:
            return "the real cleaned final text."

    engine._cleaner = FakeCleaner()
    engine._committed = ["hello", "world"]
    engine._typed_text = "hello world"     # engine *thinks* it's on screen
    engine._typed_chars = 0                # but nothing was actually typed

    wav = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._finalize(wav)

    assert injector.deleted == 0           # the previous text is untouched
    assert injector.parts == []            # no correction typed anywhere


def _scenario_broken_ledger():
    transcriber = FakeTranscriber(["irrelevant"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def clean(self, raw: str, app_hint: str | None = None) -> str:
            return "cleaned final text."

    engine._cleaner = FakeCleaner()
    engine._committed = ["um", "hello"]
    engine._typed_text = ""
    wav = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    return engine, injector, wav


def test_correction_skipped_when_foreground_window_changes(monkeypatch):
    from src import inject as inject_mod
    snapshots = iter([
        {"hwnd": 100, "caret": (500, 100)},
        {"hwnd": 200, "caret": (500, 100)},   # focus moved to another window
    ])
    monkeypatch.setattr(inject_mod, "capture_typing_context",
                        lambda: next(snapshots))

    engine, injector, wav = _scenario_broken_ledger()
    engine._finalize(wav)

    assert injector.parts == ["um hello"]   # early-type still typed the raw
    assert injector.deleted == 0            # but the correction was skipped


# ------------------------------------------------------- on-demand polish pass


def _engine_with_polish_cleaner(injector):
    transcriber = FakeTranscriber(["irrelevant"])
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def polish(self, raw, app_hint=None):
            return "hello world, everyone here."

    engine._cleaner = FakeCleaner()
    return engine


def test_engine_polish_replaces_text_via_guarded_diff(monkeypatch):
    from src import inject as inject_mod
    snapshots = iter([
        {"hwnd": 100, "caret": (500, 100)},
        {"hwnd": 100, "caret": (500, 100)},
    ])
    monkeypatch.setattr(inject_mod, "capture_typing_context",
                        lambda: next(snapshots))

    injector = FakeInjector()
    engine = _engine_with_polish_cleaner(injector)
    engine._committed = ["hello", "world"]
    engine._typed_text = "hello world"
    engine._typed_chars = len("hello world")
    engine._status.committed_text = "hello world"

    result = engine.polish()

    assert result == "hello world, everyone here."
    assert engine.status.committed_text == result
    assert injector.deleted == 0                      # shared prefix kept
    assert injector.parts == [", everyone here."]


def test_engine_polish_returns_none_without_cleaner():
    injector = FakeInjector()
    engine = make_engine(FakeTranscriber(["x"]), injector)
    engine._status.committed_text = "hello"
    assert engine.polish() is None
    assert injector.parts == []


def test_engine_polish_skipped_when_window_changes(monkeypatch):
    from src import inject as inject_mod
    snapshots = iter([
        {"hwnd": 100, "caret": (500, 100)},
        {"hwnd": 200, "caret": (500, 100)},   # focus moved during the LLM call
    ])
    monkeypatch.setattr(inject_mod, "capture_typing_context",
                        lambda: next(snapshots))

    injector = FakeInjector()
    engine = _engine_with_polish_cleaner(injector)
    engine._committed = ["hello", "world"]
    engine._typed_text = "hello world"
    engine._typed_chars = len("hello world")
    engine._status.committed_text = "hello world"

    result = engine.polish()

    assert result == "hello world, everyone here."   # text still produced
    assert injector.parts == []                       # but nothing typed
    assert injector.deleted == 0                      # screen left untouched


def test_correction_skipped_when_caret_moves(monkeypatch):
    from src import inject as inject_mod
    snapshots = iter([
        {"hwnd": 100, "caret": (500, 100)},
        {"hwnd": 100, "caret": (30, 100)},    # user clicked elsewhere / Home
    ])
    monkeypatch.setattr(inject_mod, "capture_typing_context",
                        lambda: next(snapshots))

    engine, injector, wav = _scenario_broken_ledger()
    engine._finalize(wav)

    assert injector.parts == ["um hello"]   # early-type still typed the raw
    assert injector.deleted == 0            # but the correction was skipped