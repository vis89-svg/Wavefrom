"""Tests for HotkeyController (hold-mode activation, suppression, self-heal).

The controller decides whether the hotkey combo is "held" from BOTH the event
stream (down/up events the hook actually sees) AND the physical key state
(GetAsyncKeyState): press/suppression need both, while a release fires when
either clears. A stuck physical Win key (its key-up is suppressed from the
async state table) can therefore never keep a dictation running or swallow the
next Ctrl+C / Ctrl+A / Ctrl+X.
"""
import types

import src.main as main


def _evt(name, event_type):
    return types.SimpleNamespace(name=name, event_type=event_type)


class Phys:
    """Fake physical key state: a set of currently-held VK codes."""

    def __init__(self):
        self.down = set()

    def __call__(self, vks):
        return any(vk in self.down for vk in vks)

    def press(self, *vks):
        self.down.update(vks)

    def release(self, *vks):
        self.down.difference_update(vks)


def make_controller(monkeypatch, hotkey="ctrl+win", debounce=0.0):
    phys = Phys()
    monkeypatch.setattr(main, "_physically_down", phys)
    monkeypatch.setattr(main.keyboard, "hook", lambda cb, suppress=True: cb)
    monkeypatch.setattr(main.keyboard, "unhook", lambda _cb: None)
    events = []
    hc = main.HotkeyController(
        "hold", hotkey,
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"))
    hc.RELEASE_DEBOUNCE_SECS = debounce
    return hc, phys, events


def test_hold_combo_triggers_exactly_once_then_releases(monkeypatch):
    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11)                      # ctrl alone: not the combo yet
    hc._callback(_evt("ctrl", "down"))
    assert events == []

    phys.press(0x5B)                      # win joins: combo held
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    hc._callback(_evt("a", "down"))       # extra keys while held: no re-trigger
    hc._callback(_evt("a", "up"))
    assert events == ["press"]

    phys.release(0x5B)                    # release win: dictation ends
    hc._callback(_evt("windows", "up"))
    assert events == ["press", "release"]
    assert hc._pressed == set()
    assert hc._blocked == set()

    phys.release(0x11)
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]


def test_ctrl_alone_after_dictation_never_triggers(monkeypatch):
    # THE regression: after a dictation, pressing Ctrl (for Ctrl+C / Ctrl+A /
    # Ctrl+X) must NOT start a new capture and must pass through to the app.
    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]

    phys.press(0x11)                      # the Ctrl of a Ctrl+C
    hc._callback(_evt("ctrl", "down"))
    assert events == ["press", "release"]
    assert hc._callback(_evt("c", "down")) is True   # pass-through: copy works
    hc._callback(_evt("c", "up"))
    hc._callback(_evt("ctrl", "up"))
    phys.release(0x11)
    assert events == ["press", "release"]


def test_stale_event_set_cannot_fake_active(monkeypatch):
    # Even if the event set leaked a "win" (its key-up was lost while the hook
    # thread was busy finalizing), the physical state decides: win is up, so
    # Ctrl alone stays inactive and passes through.
    hc, phys, events = make_controller(monkeypatch)
    hc._pressed = {"ctrl", "win"}          # leaked/stale entries

    phys.press(0x11)
    assert hc._callback(_evt("ctrl", "down")) is True
    assert events == []
    hc._callback(_evt("ctrl", "up"))
    phys.release(0x11)


def test_keys_are_suppressed_only_while_combo_held(monkeypatch):
    hc, phys, events = make_controller(monkeypatch)

    assert hc._callback(_evt("ctrl", "down")) is True   # idle: pass-through
    assert hc._callback(_evt("x", "down")) is True

    phys.press(0x11, 0x5B)
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]
    assert hc._callback(_evt("x", "down")) is False     # held: suppressed

    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    assert events == ["press", "release"]
    assert hc._callback(_evt("x", "down")) is True      # released: passes again
    hc._callback(_evt("x", "up"))


def test_release_debounce_ignores_instant_retrigger(monkeypatch):
    # A press landing right after a release (queued-event burst) is ignored
    # and its matching release is not emitted either.
    hc, phys, events = make_controller(monkeypatch, debounce=0.25)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]

    # Instant re-hold (within the debounce window) must not re-capture...
    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press", "release"]
    # ...and its release must not emit a spurious release either.
    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]
    assert hc._pressed == set()
    assert hc._blocked == set()


def test_non_modifier_final_key_requires_event_tap(monkeypatch):
    # ctrl+alt+k: modifiers must be physically held AND 'k' must be tapped.
    hc, phys, events = make_controller(monkeypatch, hotkey="ctrl+alt+k")

    phys.press(0x11, 0x12)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("alt", "down"))
    assert events == []                    # k not pressed yet

    hc._callback(_evt("k", "down"))
    assert events == ["press"]
    hc._callback(_evt("k", "up"))
    assert events == ["press", "release"]  # momentary final key ends the hold

    phys.release(0x12)
    hc._callback(_evt("alt", "up"))
    phys.release(0x11)
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]


def test_watchdog_fires_release_when_events_miss_it(monkeypatch):
    # THE release-detection regression: the user lets go of the keys but the
    # key-up events never reach the hook (wedged thread / Start menu / callback
    # error). The watchdog polls the physical state and must stop the capture.
    import time

    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]
    assert hc._watch_thread is not None and hc._watch_thread.is_alive()

    # Physical release with NO key events delivered.
    phys.release(0x5B, 0x11)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and events != ["press", "release"]:
        time.sleep(0.02)
    assert events == ["press", "release"]
    assert hc._emitted is False
    assert hc._was_active is False
    assert hc._pressed == set()
    assert hc._blocked == set()


def test_watchdog_does_not_double_fire_release(monkeypatch):
    import time

    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    # The event path handles the release; the watchdog must stay quiet.
    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    assert events == ["press", "release"]
    time.sleep(0.3)
    assert events == ["press", "release"]


def test_watchdog_not_armed_for_debounce_rejected_press(monkeypatch):
    import time

    hc, phys, events = make_controller(monkeypatch, debounce=0.25)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]

    # Instant re-hold is rejected (no on_press) and must not arm a watchdog
    # that later emits a spurious release.
    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press", "release"]
    time.sleep(0.3)
    assert events == ["press", "release"]

    phys.release(0x5B, 0x11)
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))


def test_callback_handles_unnamed_event_without_crash(monkeypatch):
    # A key the library couldn't name arrives with name=None; the callback
    # must pass it through instead of raising (an exception there would be
    # silently swallowed by the hook and could lose the release event).
    hc, phys, events = make_controller(monkeypatch)
    assert hc._callback(_evt(None, "up")) is True
    assert hc._callback(_evt(None, "down")) is True
    assert events == []


def test_stuck_physical_win_release_fires_from_event_up(monkeypatch):
    # THE bug: after a dictation the hook suppresses the Win key-up, so
    # GetAsyncKeyState keeps reporting Win as down for a long time. The
    # key-up event alone must end the recording even though the physical
    # state never clears.
    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    # User lets go: key-up events arrive, but the physical state is stuck
    # (Win still reported down by GetAsyncKeyState).
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]
    assert hc._was_active is False
    assert hc._pressed == set()
    assert hc._blocked == set()

    # The stuck physical Win alone must not re-trigger a capture either.
    hc._callback(_evt("ctrl", "down"))
    assert hc._callback(_evt("c", "down")) is True
    assert events == ["press", "release"]
    hc._callback(_evt("c", "up"))
    hc._callback(_evt("ctrl", "up"))


def test_press_requires_event_and_physical_signals(monkeypatch):
    hc, phys, events = make_controller(monkeypatch)

    # Physical says held but the event stream doesn't (stuck/stale physical
    # Win): no press, everything passes through.
    phys.press(0x11, 0x5B)
    assert hc._callback(_evt("c", "down")) is True
    assert events == []
    hc._callback(_evt("c", "up"))

    # Events say held but physical doesn't (leaked event set): no press and no
    # suppression either.
    phys.release(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == []
    assert hc._callback(_evt("x", "down")) is True
    hc._callback(_evt("x", "up"))
    hc._callback(_evt("ctrl", "up"))
    hc._callback(_evt("windows", "up"))


def test_stuck_physical_win_never_suppresses_plain_ctrl_combo(monkeypatch):
    # With Win stuck "down" physically after a dictation, a plain Ctrl+C must
    # pass through to the app (copy works) instead of being swallowed.
    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x5B)                       # win stuck down physically
    hc._callback(_evt("ctrl", "down"))
    assert hc._callback(_evt("c", "down")) is True
    hc._callback(_evt("c", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == []
    phys.release(0x5B)


def test_watchdog_quiet_when_event_release_while_physical_stuck(monkeypatch):
    import time

    hc, phys, events = make_controller(monkeypatch)

    phys.press(0x11, 0x5B)
    hc._callback(_evt("ctrl", "down"))
    hc._callback(_evt("windows", "down"))
    assert events == ["press"]

    # Key-up events arrive but the physical Win stays stuck down: the event
    # path must fire the release and the watchdog must not double-fire.
    hc._callback(_evt("windows", "up"))
    hc._callback(_evt("ctrl", "up"))
    assert events == ["press", "release"]
    time.sleep(0.3)
    assert events == ["press", "release"]