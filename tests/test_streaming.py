"""Tests for the streaming engine with mocked transcriber + injector."""
import io
import wave

import numpy as np

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
    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
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
        def clean(self, raw: str) -> str:
            return "hello world, everyone here."

    engine._cleaner = FakeCleaner()
    engine.start()
    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "hello world this is a test goodbye world."


def test_full_audio_chunked_into_overlapping_windows():
    rate = 16000
    transcriber = FakeTranscriber([
        "hello world",                  # chunk 1 [0s,30s)
        "world this is a test",         # chunk 2 [28s,58s) overlap head "world"
        "test goodbye",                 # chunk 3 [56s,62s)
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._full_audio = _to_wav(np.zeros(int(rate * 62), dtype=np.int16), rate)
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # 62s -> 3 chunks, overlap-diff stitched with no duplication/loss
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
    assert len(slices) == 3
    sizes = []
    for s in slices:
        with wave.open(io.BytesIO(s), "rb") as w:
            sizes.append(w.getnframes())
    # first window is SLICE_SECS (no backlog yet); later windows are capped at
    # SLICE_SECS + OVERLAP_SECS of retained audio.
    assert sizes[0] == int(rate * 2.5)
    assert sizes[1] == int(rate * 2.9)
    assert sizes[2] == int(rate * 2.9)


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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
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
    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
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

    def clean(self, raw):
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # 3 transcribe calls: slice + primary chunk + verify chunk
    assert len(transcriber.calls) == 3
    # verify chunk used the alternate model at higher temperature
    assert transcriber.kwargs[2]["model"] == "whisper-large-v3"
    assert transcriber.kwargs[2]["temperature"] == 0.2
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
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

    engine._process_slice(_to_wav(np.zeros(1600, dtype=np.int16), 16000))

    assert " ".join(engine._committed) == "we are done"
    assert injector.typed.strip() == "we are done"


def test_slice_short_stutter_is_preserved():
    # Real 1-2 word stutters are speech, never collapsed or dropped.
    injector = FakeInjector()
    engine = make_engine(FakeTranscriber(["no no no and then we left"]), injector)

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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
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
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "the end is near."