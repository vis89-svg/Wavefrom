# Waveform

A free, Windows push-to-talk dictation app: hold a hotkey, speak, release — your words appear as clean, correctly punctuated text wherever your cursor is. Built on Groq's free-tier Whisper + LLM APIs instead of a paid subscription.

## Features

- **Real-time streaming transcription** — text appears as you speak, not after you stop.
- **Two modes**: hold-to-talk (`ctrl+win` by default) or tap-to-toggle live typing (`ctrl+alt+d`).
- **AI cleanup** — removes filler words, fixes grammar/punctuation, corrects obvious mis-transcriptions, while never inventing or dropping content you actually said.
- **On-demand Polish** — a stronger editorial pass (via the floating review panel) for a final grammar/flow polish, with Send-to-cursor and copy-to-clipboard.
- **Accuracy safeguards** — a second, independent decode cross-checks longer dictations and an LLM adjudicates any disagreement; a hallucination-phrase filter drops Whisper's known "stock phrase on silence" artifacts (e.g. "thanks for watching").
- **Glossary & correction map** — teach it your names, jargon, and recurring mis-hearings.
- **History, system tray, floating overlay, autostart** — the everyday-use conveniences.
- **Optional fully offline mode** — local `faster-whisper` inference, no API calls, no internet required.

## Install

Grab the installer from the [latest release](https://github.com/vis89-svg/Wavefrom/releases/latest) — `Waveform_Installer.exe`. It's a per-user install (no admin rights needed for the installer itself).

You'll need a free [Groq API key](https://console.groq.com/keys) — the app links you straight there from Settings the first time you need one.

## Usage

| Action | Hotkey |
|---|---|
| Hold-to-talk (types once, cleaned, on release) | `ctrl+win` |
| Tap-to-toggle live typing | `ctrl+alt+d` |

Both hotkeys are remappable in Settings. After a dictation finishes, a small review panel lets you Polish, Send, or copy the result.

## Building from source

```powershell
git clone https://github.com/vis89-svg/Wavefrom.git
cd Wavefrom
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m src.main dictate
```

Copy `.env.example` to `.env` and add your `GROQ_API_KEY` (or paste it into Settings once running — either goes to the OS credential store when available).

To build the standalone `dictation.exe` and installer yourself:

```powershell
.\build.ps1                              # produces dist\dictation.exe
iscc voiceflow_install.iss               # produces Output\Waveform_Installer.exe (needs Inno Setup)
```

Releases are also built automatically by [GitHub Actions](.github/workflows/release.yml) — push a `v*` tag, or run the workflow manually from the Actions tab.

## Optional: fully offline mode

```powershell
.venv\Scripts\pip install -r requirements-optional.txt
```

Then enable `local_engine` in Settings — dictation runs entirely on-device via `faster-whisper`, no network calls.

## Tests

```powershell
.venv\Scripts\pip install pytest
.venv\Scripts\pytest tests/
```

## How it works

Mic audio is captured in short overlapping slices and transcribed as you speak (Groq `whisper-large-v3-turbo`), stitched together with an overlap-diff merge for the live "text appears as you talk" feel. On release, a longer dictation gets a second full-audio pass — optionally cross-checked against a second model — before an LLM cleanup pass produces the final text.
