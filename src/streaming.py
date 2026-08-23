"""Streaming dictation engine: continuous capture, slice transcription, live merge.

The engine captures mic audio while the hotkey is held, cuts it into fixed
slices (with overlap so Whisper sees stable context), transcribes each slice
on a worker thread, merges it into the committed text via overlap-diff, and
types only the new tail — giving the "text appears as you speak" feel.
"""
from __future__ import annotations

import concurrent.futures
import difflib
import io
import logging
import queue
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from src.merge import (PUNCT, MIN_REPEAT_WORDS, _norm, apply_disputes,
                       collapse_adjacent_repeats, diff_plan, ensure_period,
                       find_disputed_blocks, merge_segments,
                       strip_trailing_repeat, tokenize, union_text)

log = logging.getLogger(__name__)

SLICE_SECS = 3.0
OVERLAP_SECS = 0.8
SILENCE_PERIOD_SECS = 1.5
MAX_SILENCE_SECS = 3.0
MAX_RECORD_SECS = 120.0
MAX_PROMPT_CHARS = 400
FINAL_CHUNK_SECS = 20.0
FINAL_CHUNK_OVERLAP_SECS = 2.0
_LOW_CONF_LOGPROB = -0.5  # avg_logprob below this marks a decode as uncertain
_ECHO_TAIL_WORDS = 15  # how many committed words a prompt echo may repeat
_FULL_PROMPT_ECHO_RATIO = 0.90  # slice ~identical to the whole prompt = echo
_LEVEL_REPORT_EVERY = 0.1  # seconds between overlay level updates


def _low_conf_words(result) -> set[str]:
    """Normalized words from segments with an uncertain decode (low logprob).

    These are the regions most likely to contain substitutions or
    hallucinations; the reconciliation prompt uses the set to tell the LLM
    which candidate the audio itself thought was shaky.
    """
    low: set[str] = set()
    for seg in getattr(result, "segments", []):
        if seg.avg_logprob is not None and seg.avg_logprob < _LOW_CONF_LOGPROB:
            low.update(_norm(w) for w in seg.text.split())
    return low


@dataclass
class EngineStatus:
    state: str = "idle"  # idle | recording | transcribing | cleaning | error
    committed_text: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "text": self.committed_text}


class DictationEngine:
    def __init__(self, config, transcriber, cleaner=None, injector=None,
                 notify=None, tray=None, local_engine=None, overlay=None):
        self._config = config
        self._transcriber = transcriber
        self._cleaner = cleaner
        self._injector = injector
        self._notify = notify
        self._tray = tray
        self._local = local_engine
        self._overlay = overlay

        self._slice_q: queue.Queue[bytes | None] = queue.Queue()
        self._committed: list[str] = []
        self._typed_text = ""
        self._typed_chars = 0
        self._full_audio: bytes | None = None
        self._last_disputes: list = []
        self._active = threading.Event()
        self._stop_capture = threading.Event()
        self._busy = threading.Event()
        self._worker: threading.Thread | None = None
        self._status = EngineStatus()
        self._lock = threading.Lock()
        self._pending_period = False
        self._last_level_report = 0.0
        self._hold_active = False
        self._mode = getattr(config, "mode", "hold")

    # ------------------------------------------------------------- lifecycle
    def start(self, mode: str | None = None) -> bool:
        """Begin a dictation. `mode` ("hold" or "tap") governs this session's
        behavior — whether live slice typing is deferred until release
        (hold) or typed as you speak with silence-based auto-stop (tap).
        Two independent hotkeys can each call this with their own fixed
        mode; when omitted (offline callers like dictate_bytes()/tests),
        falls back to the config's mode default.
        """
        if self._busy.is_set():
            log.warning("Engine busy; ignoring start")
            return False
        self._mode = mode or getattr(self._config, "mode", "hold")
        self._busy.set()
        self._stop_capture.clear()
        self._active.set()
        self._pending_period = False
        # Fresh per-dictation state: without this, words from a previous
        # session stay in _committed and leak into the next prompt/merge.
        self._committed = []
        self._typed_text = ""
        self._typed_chars = 0
        self._full_audio = None
        self._last_disputes = []
        # Belt-and-suspenders: in hold mode the engine defers live slice
        # typing for the whole recording even if the controller's
        # set_hold_active(True) lands late. Typing while Ctrl+Win is held
        # would reach the app as shortcuts (no visible text) yet inflate
        # _typed_text — which previously let the final diff backspace far
        # beyond this dictation's own text.
        self._hold_active = self._mode == "hold"
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        return True

    def stop(self) -> None:
        self._stop_capture.set()
        # Wait only for the capture loop to exit (recording stopped), not for
        # the worker's finalize/typing pass — that finishes in the background so
        # the hotkey hook thread is never blocked for the whole cleanup.
        if self._active.is_set():
            self._active.wait(timeout=10)

    @property
    def status(self) -> EngineStatus:
        return self._status

    def set_hold_active(self, active: bool) -> None:
        """Record whether the hotkey combo is held (hold mode).

        Event-driven (set by the hotkey controller from key events), so it
        clears the instant the key-up is seen — unlike GetAsyncKeyState, which
        can keep reporting a suppressed Win key as down for a long time. The
        engine defers live slice typing and gate final typing on this flag
        instead of polling the physical modifier state.
        """
        self._hold_active = active

    # ---------------------------------------------------------- capture loop
    def capture(self) -> None:
        """Read mic in a loop, emitting slices of SLICE_SECS + overlap.

        Also retains every frame so the full utterance can be re-transcribed
        on release (chunked, overlap-merged) for an accurate final text.
        In hold mode recording ends only on hotkey release (or MAX_RECORD_SECS);
        in tap mode a long silence auto-stops the capture.
        """
        rate = self._config.sample_rate
        hold_mode = self._mode == "hold"
        chunk = int(rate * 0.03)
        buffer: list[np.ndarray] = []
        all_frames: list[np.ndarray] = []
        n_buffer = 0
        slice_secs = float(getattr(self._config, "slice_secs", SLICE_SECS) or SLICE_SECS)
        slice_len = int(rate * slice_secs)
        hold_len = int(rate * (slice_secs + OVERLAP_SECS))
        silence_run = 0
        total = 0
        had_speech = False
        max_len = int(rate * MAX_RECORD_SECS)

        with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                            blocksize=chunk) as stream:
            self._overlay_state("recording")
            while not self._stop_capture.is_set() and self._active.is_set():
                data, _ = stream.read(chunk)
                buffer.append(data.copy())
                all_frames.append(data.copy())
                n_buffer += len(data)
                total += len(data)

                silent = _is_silent(data, rate)
                if silent:
                    silence_run += len(data)
                else:
                    silence_run = 0
                    had_speech = True
                self._report_level(data)

                if had_speech and silence_run > int(rate * SILENCE_PERIOD_SECS) \
                        and not self._pending_period:
                    self._pending_period = True

                if not hold_mode and had_speech \
                        and silence_run > int(rate * MAX_SILENCE_SECS):
                    self._stop_capture.set()

                if n_buffer >= slice_len:
                    window = np.concatenate(buffer)
                    if len(window) > hold_len:
                        window = window[-hold_len:]
                    self._slice_q.put(_to_wav(window, rate))
                    buffer = []
                    n_buffer = 0

                if total > max_len:
                    self._stop_capture.set()

            if buffer and had_speech:
                self._slice_q.put(_to_wav(np.concatenate(buffer), rate))

        if had_speech and all_frames:
            self._full_audio = _to_wav(np.concatenate(all_frames), rate)
        else:
            self._full_audio = None
        self._slice_q.put(None)
        self._active.clear()
        self._overlay_state("idle")

    # ------------------------------------------------------------ worker loop
    def _worker_loop(self) -> None:
        try:
            while True:
                try:
                    item = self._slice_q.get(timeout=1.0)
                except queue.Empty:
                    if not self._active.is_set():
                        break
                    continue
                if item is None:
                    break
                self._process_slice(item)
            self._finalize(self._full_audio)
        finally:
            self._busy.clear()

    def _process_slice(self, wav_bytes: bytes) -> None:
        self._status.state = "transcribing"
        self._notify_tray()
        prompt = self._context_prompt()
        text = None
        for attempt in range(2):
            try:
                text = self._transcriber.transcribe_bytes(
                    wav_bytes, language=self._config.language, prompt=prompt)
                break
            except Exception as e:
                if attempt == 0 and _is_rate_limit(e):
                    delay = _retry_delay(self._transcriber)
                    log.warning("Rate limited; retrying slice in %.1fs", delay)
                    time.sleep(delay)
                    continue
                log.warning("Slice transcription failed: %s", e)
                self._status.state = "error"
                self.notify("Dictation",
                            "Transcription failed. Check rate limits / network.")
                self._notify_tray()
                return
        if text is None:
            return

        if self._local:
            # optional watermark is handled at display layer in M2+; skip here
            pass

        # Whisper loops a phrase on silence/echo; collapse adjacent repeats
        # before any merge so the loop never becomes committed text.
        text = collapse_adjacent_repeats(text)

        appended = ""
        with self._lock:
            # A transcript that is only words we already sent as the Whisper
            # prompt is the model echoing the prompt back, not new speech.
            # Real speech that merely matches glossary/hint words is NOT an
            # echo — the user legitimately says those words.
            if text.strip() and self._is_prompt_echo(text, prompt):
                log.info("Slice is a prompt echo; ignoring: %r", text)
            else:
                old_committed = self._committed
                # 0.4s of audio overlap is only a few words; cap the overlap
                # search so a coincidental longer match can never swallow real
                # content.
                committed, appended = merge_segments(self._committed, text,
                                                     max_overlap=8)
                # The tail to be typed may itself be only prompt words echoed
                # back after the overlap match ("real words" + prompt echo).
                # Anything past the overlap was already covered by committed,
                # so dropping an all-echo tail loses no new speech.
                if appended.strip() and self._is_prompt_echo(appended, prompt):
                    log.info("Slice tail is a prompt echo; dropping: %r",
                             appended)
                    committed = old_committed
                    appended = ""
                self._committed = committed
                if self._pending_period:
                    self._committed = ensure_period(self._committed)
                    self._pending_period = False
                    appended = (appended + " " if appended else "") + "."
            committed_text = " ".join(self._committed)
        if self._injector and appended:
            if self._hold_active or self._mode == "hold":
                # Hold mode never types slices live: everything is typed at
                # finalize by the early-type, which runs right after the user
                # releases. Typing a slice here — even one transcribed after
                # the release — used to set _typed_text = committed_text,
                # claiming the WHOLE committed text was on screen when only
                # this tail was typed. That made the early-type skip (it saw
                # raw.startswith(typed)) and the final diff skip (to_delete
                # exceeded the session ledger), so most of the dictation never
                # appeared and the screen did not match the corrected text.
                # Deferring keeps _typed_text == "" so the early-type types
                # the full raw text at release, bounded by the ledger.
                log.info("Deferring slice text (%d chars) while hotkey held",
                         len(appended))
            else:
                sep = "" if (not self._typed_text or self._typed_text.endswith(" ")
                             or appended.startswith(" ")) else " "
                self._injector.inject_text(sep + appended)
                self._typed_chars += len(sep + appended)
                # Track the literal screen text, never the whole committed
                # text: committed may contain deferred content that is not on
                # screen, and the final diff must only ever delete what was
                # actually typed by this session.
                self._typed_text = self._typed_text + sep + appended
                log.info("Typed slice text (%d chars); session ledger=%d",
                         len(sep + appended), self._typed_chars)
        elif not self._injector:
            # No injection target (offline/eval): keep the bookkeeping text
            # aligned with committed so status/finalize stay consistent.
            self._typed_text = committed_text
        self._status.committed_text = committed_text
        self._status.state = "recording" if self._active.is_set() else "idle"
        self._overlay_state(self._status.state, committed_text)
        self._notify_tray()

    def _is_prompt_echo(self, text: str, prompt: str | None) -> bool:
        """True when `text` is Whisper echoing prompt content back, not speech.

        Two signals:
          1. The whole text appears contiguously in the last committed words —
             a re-decode returned nothing new (the overlap the merge would
             otherwise deduplicate). Scattered word re-use is real speech and
             is kept, because dropping it loses the new words mixed in.
          2. The text is (almost) identical to the whole prompt (domain hint +
             glossary + committed tail) — silence-induced regurgitation. The
             prompt must be reasonably long and the text long enough that a
             high ratio is a genuine echo, never a short glossary phrase.
        Glossary/hint words by themselves are never an echo: the user speaks
        them for real, so matching them must not drop dictation.
        """
        tokens = tokenize(text)
        if not tokens:
            return True
        tail = tokenize(" ".join(self._committed[-_ECHO_TAIL_WORDS:]))
        if _is_contiguous_run(tokens, tail):
            return True
        if prompt:
            p = tokenize(prompt)
            if (len(tokens) >= 5 and len(p) >= 6 and
                    difflib.SequenceMatcher(None, tokens, p).ratio()
                    >= _FULL_PROMPT_ECHO_RATIO):
                return True
        return False


    # -------------------------------------------------------------- overlay
    def set_overlay(self, overlay) -> None:
        self._overlay = overlay

    def _overlay_state(self, state: str, text: str = "") -> None:
        if not self._overlay:
            return
        try:
            self._overlay.set_state(state, text)
        except Exception as e:
            log.debug("Overlay update failed: %s", e)

    def _report_level(self, data: np.ndarray) -> None:
        """Throttled mic-level feed for the overlay waveform."""
        if not self._overlay:
            return
        now = time.monotonic()
        if now - self._last_level_report < _LEVEL_REPORT_EVERY:
            return
        self._last_level_report = now
        rms = np.sqrt(np.mean(np.square(data.astype(np.float32) / 32768.0)))
        db = 20.0 * np.log10(max(rms, 1e-8))
        try:
            self._overlay.set_level(float(max(db, -70.0)))
        except Exception as e:
            log.debug("Overlay level update failed: %s", e)

    def _app_hint(self) -> str | None:
        """Foreground window title for the cleanup tone hint ("" = no hint)."""
        if not getattr(self._config, "app_tone", True):
            return ""
        try:
            from src.inject import foreground_window_title
            title = foreground_window_title().strip()
        except Exception as e:
            log.debug("Could not read foreground window title: %s", e)
            return ""
        if not title or len(title) > 60:
            return ""
        lowered = title.lower()
        if any(tok in lowered for tok in ("dictat", "settings", "voiceflow")):
            return ""
        return title

    def _context_matches(self, baseline: dict, now: dict) -> tuple[bool, bool]:
        """Compare two capture_typing_context() snapshots.

        Returns (window_ok, caret_ok). window_ok is False when focus moved to
        a different window; caret_ok is False when the caret moved more than
        a couple pixels within the same window. Shared by every point in this
        engine that is about to type or backspace based on a snapshot taken
        before a slow call (LLM cleanup/polish) — never act on a stale one.
        """
        window_ok = now["hwnd"] == baseline["hwnd"]
        caret_ok = True
        if window_ok and baseline["caret"] is not None and now["caret"] is not None:
            bx, by = baseline["caret"]
            nx, ny = now["caret"]
            if nx < bx - 2 or nx > bx + 2 or ny < by - 2 or ny > by + 2:
                caret_ok = False
        return window_ok, caret_ok

    def _finalize(self, wav_bytes: bytes | None) -> None:
        with self._lock:
            raw = " ".join(self._committed)
            typed = self._typed_text
        # Collapse loops/echoes in the streaming text before it is typed, so
        # whatever gets typed first is already as clean as possible.
        raw = strip_trailing_repeat(collapse_adjacent_repeats(raw))

        baseline = None
        pre_cleaned = False
        quick = None
        if (self._injector and wav_bytes and raw and not self._hold_active
                and not typed and self._cleaner):
            # Nothing is on screen yet (hold mode never types live) and a
            # cleaner is configured: run cleanup BEFORE typing anything, so
            # the very first text the user sees is already correct instead of
            # typing raw text and then visibly swapping it out afterward.
            self._wait_hold_release()
            quick_base = raw
            if quick_base[-1] not in PUNCT:
                quick_base += "."
            correction_map = getattr(self._config, "correction_map", None) or {}
            for wrong, right in correction_map.items():
                quick_base = quick_base.replace(wrong, right)
            from src.merge import fuzzy_glossary_replace
            glossary = getattr(self._config, "glossary", None) or []
            quick_base = fuzzy_glossary_replace(quick_base, glossary)

            from src.inject import capture_typing_context
            baseline = capture_typing_context()
            self._status.state = "cleaning"
            self._overlay_state("cleaning", self._status.committed_text)
            self._notify_tray()
            try:
                quick = self._cleaner.clean(quick_base, app_hint=self._app_hint())
            except Exception as e:
                log.warning("Pre-type cleanup failed, using raw: %s", e)
                quick = quick_base
            if not quick or not quick.strip():
                quick = quick_base
            elif quick[-1] not in PUNCT:
                quick += "."

            now = capture_typing_context()
            window_ok, caret_ok = self._context_matches(baseline, now)
            if window_ok and caret_ok:
                self._injector.inject_text(quick)
                typed = quick
                self._typed_chars = len(quick)
                log.info("Pre-typed cleaned text (%d chars)", len(quick))
            else:
                # Focus/caret moved during the cleanup call: don't type stale
                # text into a window the user may have left. The cleaned text
                # still lands in the review panel (Polish/Send/Clipboard) via
                # committed_text below, so nothing is lost.
                log.warning("Pre-type skipped (window=%s caret=%s); text kept "
                            "in review panel only", window_ok, caret_ok)
                typed = ""
                self._typed_chars = 0
            pre_cleaned = True

        elif self._injector and wav_bytes and raw and not self._hold_active:
            from src.inject import capture_typing_context
            # Snapshot where the caret/focus is now; the final correction may
            # only touch this window/position seconds later (after the full
            # re-transcription + cleanup).
            baseline = capture_typing_context()
            # Type the streaming transcript now so text appears immediately
            # instead of after the expensive full re-transcription + cleanup.
            # Verbatim (no period) so the final diff_plan baseline matches the
            # proven tap-mode path; the passes below refine it via diff.
            if raw.startswith(typed):
                tail = raw[len(typed):]
            elif not typed:
                tail = raw
            else:
                tail = ""  # can't cleanly append; the final diff reconciles
            if tail:
                try:
                    self._injector.inject_text(tail)
                except Exception as e:
                    log.warning("Early finalize typing failed: %s", e)
                else:
                    self._typed_chars += len(tail)
                    typed = raw
            log.info("Early-type: typed=%d raw=%d tail=%d ledger=%d",
                     len(typed), len(raw), len(tail), self._typed_chars)

        final = raw
        heavy_pass_changed = False
        if wav_bytes:
            # Skip expensive full-audio re-transcription for short dictations
            # (< 12s) — streaming slices already covered it well with overlap.
            pcm, rate = pcm_from_wav(wav_bytes)
            audio_secs = len(pcm) / rate if rate else 0
            if audio_secs >= 12.0:
                self._status.state = "transcribing"
                self._overlay_state("transcribing", self._status.committed_text)
                self._notify_tray()
                try:
                    if self._verify_enabled():
                        # Primary and verify are independent decodes of the
                        # SAME audio — running them one after another (as
                        # before) doubles the wait for no reason. Running
                        # them concurrently cuts that part of the lag
                        # roughly in half without skipping either decode.
                        with concurrent.futures.ThreadPoolExecutor(
                                max_workers=2) as pool:
                            primary_future = pool.submit(
                                self._transcribe_full_audio, wav_bytes)
                            verify_future = pool.submit(
                                self._transcribe_full_audio, wav_bytes,
                                model=self._verify_model(), temperature=0.4)
                            # A primary failure is fatal to this whole pass
                            # (same as before: propagates to the except
                            # below, falling back to the streaming
                            # transcript). A verify failure alone must not
                            # discard a good primary result.
                            full_text, primary_low = primary_future.result()
                            try:
                                verify_text, verify_low = verify_future.result()
                            except Exception as e:
                                log.warning("Verify pass failed, using primary: %s", e)
                                verify_text, verify_low = "", set()
                        if full_text and full_text.strip():
                            full_text = self._reconcile(
                                full_text, primary_low, verify_text, verify_low)
                    else:
                        full_text, primary_low = self._transcribe_full_audio(wav_bytes)
                    if full_text and full_text.strip():
                        unioned = union_text(full_text.strip(), raw)
                        if unioned != raw:
                            final = unioned
                            heavy_pass_changed = True
                except Exception as e:
                    log.warning("Full-audio transcription failed, using merged slices: %s", e)
            else:
                log.info("Short audio (%.1fs), using streaming transcript", audio_secs)

        # Final safety net: collapse adjacent loops and drop trailing echoes
        # anywhere in the assembled text before cleanup/typing.
        final = strip_trailing_repeat(collapse_adjacent_repeats(final))

        if not final.strip():
            self._status.state = "idle"
            self._notify_tray()
            return
        if final[-1] not in PUNCT:
            final += "."

        if pre_cleaned and not heavy_pass_changed:
            # `quick` is already the correctly-cleaned version of this exact
            # text (the heavy pass either didn't run or found nothing new) —
            # reuse it instead of paying for (and risking phrasing drift from)
            # a second LLM call on unchanged input.
            final = quick
        else:
            correction_map = getattr(self._config, "correction_map", None) or {}
            if correction_map:
                for wrong, right in correction_map.items():
                    final = final.replace(wrong, right)

            from src.merge import fuzzy_glossary_replace
            glossary = getattr(self._config, "glossary", None) or []
            final = fuzzy_glossary_replace(final, glossary)

            if self._cleaner:
                self._status.state = "cleaning"
                self._overlay_state("cleaning", self._status.committed_text)
                self._notify_tray()
                try:
                    final = self._cleaner.clean(final, app_hint=self._app_hint())
                except Exception as e:
                    log.warning("Cleanup failed, using raw transcript: %s", e)

        if self._injector and wav_bytes:
            # Hold off the final typing while the hotkey is held (e.g. the user
            # already re-pressed for the next dictation while this finalize
            # finishes — typing into a held Ctrl+Win would become shortcuts).
            # This is event-driven, so it clears the instant the key-up is seen
            # instead of waiting out a stuck physical Win key.
            self._wait_hold_release()
            to_delete, to_type = diff_plan(typed, final)
            if to_delete or to_type:
                window_ok = caret_ok = ledger_ok = True
                if baseline is not None:
                    from src.inject import capture_typing_context
                    now = capture_typing_context()
                    window_ok, caret_ok = self._context_matches(baseline, now)
                if to_delete > self._typed_chars:
                    ledger_ok = False
                log.info("Final correction: raw=%d typed=%d final=%d prefix=%d "
                         "to_delete=%d to_type=%d ledger=%d window=%s caret=%s",
                         len(raw), len(typed), len(final),
                         len(typed) - to_delete, to_delete, len(to_type),
                         self._typed_chars, window_ok, caret_ok)
                if window_ok and caret_ok and ledger_ok:
                    if to_delete:
                        self._injector.delete_chars(to_delete)
                        self._typed_chars = max(0, self._typed_chars - to_delete)
                    if to_type:
                        # Paste instead of char-by-char: this is a correction
                        # replacing text already on screen, so a multi-second
                        # visible retype is exactly the "removes the whole
                        # typed one" flicker — a paste makes the swap instant.
                        self._injector.paste_text(to_type)
                        self._typed_chars += len(to_type)
                    typed = final
                else:
                    # The screen no longer matches the engine's bookkeeping (focus
                    # moved, caret moved, or the bookkeeping claims more text than
                    # this dictation actually typed). Backspacing now could destroy
                    # pre-existing text (e.g. a previous dictation), so leave the
                    # screen untouched.
                    log.warning("Correction skipped (window=%s caret=%s ledger=%s); "
                                "on-screen text left as-is", window_ok, caret_ok,
                                ledger_ok)
            # `typed` now accurately reflects the actual on-screen text —
            # whether the correction applied, was a no-op, or was skipped.
            # Later on-demand actions (Polish/Send) must diff against this,
            # not committed_text: committed_text may already hold the cleaned
            # text even when the on-screen correction above was skipped, and
            # diffing against the wrong "before" state corrupts the screen
            # (deletes too few/wrong characters, then appends the new tail
            # onto what's left — the polished-and-unpolished-text-mashed-
            # together bug).
            self._typed_text = typed

        self._status.state = "idle"
        self._status.committed_text = final
        self._overlay_state("done", final)
        self._notify_tray()
        log.info("Dictation finalized: %r", final)

    def polish(self) -> str | None:
        """On-demand polish of the finalized text (overlay "Polish" button).

        Runs the LLM polish pass over the current final text and replaces the
        on-screen text with the polished version using the same guarded diff as
        _finalize: the foreground window and caret must be unchanged, and the
        deletion is bounded by what this session actually typed. Returns the
        polished text, or None when polish is unavailable, refused, or failed.
        """
        if not self._cleaner:
            log.warning("Polish unavailable: cleanup is disabled")
            return None
        if self._active.is_set() or self._busy.is_set():
            log.info("Polish refused: a dictation is in progress")
            return None
        final = self._status.committed_text
        if not final or not final.strip():
            return None
        on_screen = self._typed_text  # what's ACTUALLY on screen right now —
                                      # committed_text may already be cleaned
                                      # even if a prior correction never made
                                      # it to the screen (guard skip).
        from src.inject import capture_typing_context
        baseline = capture_typing_context()
        self._status.state = "cleaning"
        self._notify_tray()
        try:
            polished = self._cleaner.polish(
                final, app_hint=self._app_hint(),
                model=getattr(self._config, "polish_model", None) or None)
        except Exception as e:
            log.warning("Polish pass failed, keeping cleaned text: %s", e)
            self._status.state = "idle"
            self._notify_tray()
            return None
        if not polished or not polished.strip():
            self._status.state = "idle"
            self._notify_tray()
            return final
        if polished.strip() != final.strip() and polished[-1] not in PUNCT:
            polished += "."
        if self._injector:
            to_delete, to_type = diff_plan(on_screen, polished)
            now = capture_typing_context()
            window_ok, caret_ok = self._context_matches(baseline, now)
            ledger_ok = True
            if to_delete > self._typed_chars:
                ledger_ok = False
            log.info("Polish correction: on_screen=%d polished=%d prefix=%d "
                     "to_delete=%d to_type=%d ledger=%d window=%s caret=%s",
                     len(on_screen), len(polished), len(on_screen) - to_delete,
                     to_delete, len(to_type), self._typed_chars,
                     window_ok, caret_ok)
            if window_ok and caret_ok and ledger_ok:
                if to_delete:
                    self._injector.delete_chars(to_delete)
                    self._typed_chars = max(0, self._typed_chars - to_delete)
                if to_type:
                    self._injector.paste_text(to_type)
                    self._typed_chars += len(to_type)
                self._typed_text = polished
            else:
                log.warning("Polish skipped (window=%s caret=%s ledger=%s); "
                            "on-screen text left as-is", window_ok, caret_ok,
                            ledger_ok)
        self._status.committed_text = polished
        self._status.state = "idle"
        self._notify_tray()
        log.info("Polished final text: %r", polished)
        return polished

    def send(self) -> str | None:
        """Type the last polished text at the current caret position.

        Unlike Polish, this bypasses the window/caret/ledger guards and simply
        injects the polished text. Returns the text typed, or None when no
        polished text is available.
        """
        if not self._cleaner:
            log.warning("Send unavailable: cleanup is disabled")
            return None
        polished = self._status.committed_text
        if not polished or not polished.strip():
            log.info("Send: no polished text yet; run Polish first")
            return None
        if self._injector:
            self._injector.paste_text(polished)
        log.info("Send: typed polished text (%d chars)", len(polished))
        self._status.state = "idle"
        self._notify_tray()
        return polished

    def copy_to_clipboard(self) -> str | None:
        """Copy the last polished text to the system clipboard.

        Returns the text copied, or None when no polished text is available.
        """
        if not self._cleaner:
            log.warning("Clipboard unavailable: cleanup is disabled")
            return None
        polished = self._status.committed_text
        if not polished or not polished.strip():
            log.info("Clipboard: no polished text yet")
            return None
        import pyperclip
        pyperclip.copy(polished)
        log.info("Clipboard: copied polished text (%d chars)", len(polished))
        return polished

    def _wait_hold_release(self, timeout: float = 2.0) -> None:
        """Block until the hotkey is released (or a short timeout elapses).

        Event-driven counterpart of the old physical wait_for_modifiers_up:
        typing while the combo is held would reach the app as shortcuts, but
        the physical check could be fooled by a stuck Win key for the whole
        timeout. This polls the engine's hold flag, which the hotkey controller
        clears on the key-up event — normally zero delay.
        """
        deadline = time.monotonic() + timeout
        while self._hold_active and time.monotonic() < deadline:
            time.sleep(0.02)

    # ------------------------------------------------- full-audio transcription
    def _context_prompt(self, tail: int = 15) -> str | None:
        """Whisper context prompt = domain hint + glossary + last committed words.

        Whisper uses this to bias recognition toward the right words (e.g.
        "login system" instead of "logging system", or a custom name like
        "Razorpay" instead of a mis-hearing). Kept short — a long prompt can be
        echoed back verbatim, so the user-specific bias (hint + glossary) wins
        over the committed-words tail when the prompt gets too long.
        """
        parts: list[str] = []
        hint = getattr(self._config, "domain_hint", "") or ""
        if hint.strip():
            parts.append(hint.strip())
        seen: set[str] = set()
        terms: list[str] = []
        for t in (getattr(self._config, "glossary", None) or []):
            term = str(t).strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
        if terms:
            parts.append(" ".join(terms))
        if self._committed:
            parts.append(" ".join(self._committed[-tail:]))
        prompt = " ".join(parts)
        if len(prompt) > MAX_PROMPT_CHARS:
            # keep the hint + glossary bias, drop the committed-words tail;
            # if the glossary alone is still too long, trim whole terms.
            head = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
            if len(head) > MAX_PROMPT_CHARS:
                cut = head[:MAX_PROMPT_CHARS]
                idx = cut.rfind(",")
                if idx > 0:
                    cut = cut[:idx]
                prompt = cut.strip()
            else:
                prompt = head
        return prompt or None

    def _transcribe_full_audio(self, wav_bytes: bytes, model: str | None = None,
                               temperature: float | None = None
                               ) -> tuple[str, set[str]]:
        """Chunked transcription of the whole recording.

        The full utterance is split into overlapping FINAL_CHUNK_SECS windows
        and each is transcribed separately; the overlap-diff merge then stitches
        them into one transcript. Long single-pass Whisper calls are the classic
        cause of dropped sentences, so we never rely on one call over long audio.

        Returns (text, low_conf_words). `model`/`temperature` override the
        decode (used by the verify pass). `low_conf_words` holds normalized
        words from segments whose average log probability suggests an uncertain
        decode — useful for adjudicating disagreements.
        """
        pcm, rate = pcm_from_wav(wav_bytes)
        if len(pcm) == 0:
            return "", set()
        chunk_len = int(rate * FINAL_CHUNK_SECS)
        overlap = int(rate * FINAL_CHUNK_OVERLAP_SECS)
        min_len = int(rate * 1.0)
        committed: list[str] = []
        low_conf: set[str] = set()
        i = 0
        while i < len(pcm):
            window = pcm[i:i + chunk_len]
            if len(window) >= min_len:
                kwargs: dict = {
                    "language": self._config.language,
                    "prompt": self._context_prompt(tail=24),
                }
                if not self._local:
                    kwargs["verbose"] = True
                    if model is not None:
                        kwargs["model"] = model
                    if temperature is not None:
                        kwargs["temperature"] = temperature
                result = self._transcriber.transcribe_bytes(_to_wav(window, rate),
                                                             **kwargs)
                if isinstance(result, str):
                    text = result
                else:  # TranscriptResult with confidence metadata
                    text = result.text
                    low_conf.update(_low_conf_words(result))
                # A chunk may itself loop or echo back prompt words; strip both
                # before stitching, so they can't leak into mid-text when the
                # chunk is merged with the next one.
                text = strip_trailing_repeat(collapse_adjacent_repeats(text))
                committed, _ = merge_segments(committed, text, max_overlap=24)
            i += chunk_len - overlap
        return " ".join(committed), low_conf

    # -------------------------------------------------- verify / reconciliation
    def _verify_enabled(self) -> bool:
        """Verify pass needs a second decode + an LLM to adjudicate disputes."""
        return (bool(getattr(self._config, "verify", True))
                and not self._local
                and self._cleaner is not None)

    def _verify_model(self) -> str:
        verify_model = getattr(self._config, "verify_model", None)
        if verify_model:
            return verify_model
        primary = getattr(self._config, "whisper_model", "") or ""
        if primary == "whisper-large-v3-turbo":
            return "whisper-large-v3"
        return "whisper-large-v3-turbo"

    def _reconcile(self, full_text: str, primary_low: set[str],
                   verify_text: str, verify_low: set[str]) -> str:
        """Cross-check the primary full pass against a second model decode.

        Where the two transcripts substitute different wording for the same
        audio, the cleanup LLM picks the wording that fits the context; the
        chosen candidate is spliced into the primary text. Any failure keeps
        the primary text (never a fabricated third option).

        Takes both decodes already computed (the caller runs the primary and
        verify transcriptions concurrently, since they're independent decodes
        of the same audio — this method only does the comparison/adjudication
        step, which genuinely depends on both being done).
        """
        if not verify_text or not verify_text.strip():
            self._last_disputes = []
            return full_text

        disputes = find_disputed_blocks(full_text, verify_text,
                                        primary_low=primary_low,
                                        verify_low=verify_low)
        self._last_disputes = disputes
        if not disputes:
            return full_text
        log.info("Verify pass found %d disputed block(s)", len(disputes))
        try:
            choices = self._cleaner.reconcile(disputes)
        except Exception as e:
            log.warning("Reconcile failed, keeping primary: %s", e)
            choices = {}
        reconciled = apply_disputes(full_text, disputes, choices)
        if reconciled != full_text:
            log.info("Reconciled %d dispute(s) into final transcript",
                     sum(1 for _ in disputes))
        return reconciled

    def dictate_bytes(self, audio_bytes: bytes) -> str:
        """Offline one-shot dictation of a WAV payload (used by eval/tests).

        Feeds the real capture-slicing + worker + chunked-finalize pipeline and
        returns the final committed text.
        """
        pcm, rate = pcm_from_wav(audio_bytes)
        if len(pcm) == 0:
            return ""
        if not self.start():
            return ""
        for wav in slice_audio(pcm, rate):
            self._slice_q.put(wav)
        self._full_audio = _to_wav(pcm, rate)
        self._slice_q.put(None)
        self._worker.join(timeout=120)
        return self._status.committed_text

    # ------------------------------------------------------------------ misc
    def _notify_tray(self) -> None:
        if self._tray:
            try:
                self._tray.set_state(self._status.state)
            except Exception:
                pass

    def notify(self, title: str, msg: str) -> None:
        if self._notify:
            try:
                self._notify(title, msg)
            except Exception as e:
                log.warning("Toast failed: %s", e)

    def set_tray(self, tray) -> None:
        self._tray = tray

    def set_cleaner_mode(self, enabled: bool) -> None:
        """Enable or disable the LLM cleanup pass at runtime."""
        if enabled and self._transcriber:
            from src.cleanup import CleanupClient
            from src.config import get_api_key, load_settings
            settings = load_settings()
            api_key = get_api_key()
            if api_key and settings.cleanup_model:
                self._cleaner = CleanupClient(
                    api_key, model=settings.cleanup_model,
                    mode=settings.cleanup_mode,
                    glossary=settings.glossary,
                    correction_map=settings.correction_map)
        else:
            self._cleaner = None


def slice_audio(pcm: np.ndarray, rate: int) -> list[bytes]:
    """Cut a full PCM buffer into the same overlapping slice windows the live
    capture loop emits: a window every SLICE_SECS of new audio, each window
    holding the last SLICE_SECS + OVERLAP_SECS of audio.
    """
    slice_len = int(rate * SLICE_SECS)
    hold_len = int(rate * (SLICE_SECS + OVERLAP_SECS))
    if len(pcm) == 0:
        return []
    out: list[bytes] = []
    i = 0
    while i < len(pcm):
        end = min(i + slice_len, len(pcm))
        window = pcm[max(0, end - hold_len):end]
        out.append(_to_wav(window, rate))
        if end == len(pcm):
            break
        i = end
    return out


def pcm_from_wav(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV payload to (mono int16 PCM, sample_rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if nch > 1:
            pcm = pcm[::nch]
    return pcm, rate


def _is_rate_limit(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "ratelimit" in name or "rate limit" in str(exc).lower()


def _retry_delay(transcriber) -> float:
    rl = getattr(transcriber, "last_rate_limit", None)
    if rl and getattr(rl, "retry_after", None):
        return max(float(rl.retry_after), 1.0)
    return 2.0


def _is_silent(data: np.ndarray, rate: int) -> bool:
    if len(data) == 0:
        return True
    rms = np.sqrt(np.mean(np.square(data.astype(np.float32) / 32768.0)))
    db = 20.0 * np.log10(max(rms, 1e-8))
    return db < -40.0


def _to_wav(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


def _is_contiguous_run(tokens: list[str], tail: list[str]) -> bool:
    """True when the whole `tokens` list appears verbatim inside `tail`.

    The old subsequence test matched scattered words (greedy iterator), so
    real speech that merely reused recently-committed words in a new order
    was dropped as an "echo". Whisper regurgitation is contiguous — only a
    run that covers the entire text and repeats nothing new is an echo.
    """
    if len(tokens) < MIN_REPEAT_WORDS or len(tokens) > len(tail):
        return False
    nt = [_norm(w) for w in tokens]
    ntail = [_norm(w) for w in tail]
    for i in range(len(ntail) - len(nt) + 1):
        if ntail[i:i + len(nt)] == nt:
            return True
    return False