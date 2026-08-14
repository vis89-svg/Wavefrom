# Flow-Like Dictation App — Implementation Plan

**Goal:** A free, local-first voice dictation tool on Windows with Wispr-Flow-like
behavior (push-to-talk, "text appears as you speak" feel, AI cleanup) — built on the
Groq free tier instead of subscription cloud products.

**Budget:** $0. All heavy compute runs on Groq's free API; everything else runs locally.

---

## 1. Architecture (Hybrid)

```
┌───────────────────────── LOCAL (nimble, instant) ─────────────────────────┐
│  Global hotkey listener   →   Mic capture (WASAPI)   →   VAD/segmenting    │
│  Status UI (tray/overlay) ←   Result queue           ←   Text injection   │
│  (optional: local tiny/whisper.cpp watermark for instant partials)        │
└────────────────────────────────────────────────────────────────────────────┘
                          │  audio chunks (WAV, base64)
                          ▼
┌───────────────────────── CLOUD — Groq Free Tier ──────────────────────────┐
│  1. whisper-large-v3-turbo   → raw transcript        (20 RPM / 2,000 RPD) │
│  2. llama-3.3-70b-versatile  → cleanup/formatting    (30 RPM / 1,000 RPD) │
└────────────────────────────────────────────────────────────────────────────┘
```

Why not Render/Railway free tier: 512MB RAM free tier cannot run a meaningful
Whisper model; CPU inference adds seconds of latency plus cold starts. Groq's
free API gives large-v3-turbo with ~228x real-time speed — no hosting needed.

---

## 2. Tech Stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fast to build, rich audio/UI libs |
| Mic capture | `sounddevice` (PortAudio) | Low-latency WASAPI on Windows |
| VAD / segmentation | `webrtcvad` + silence detection | Cut speech into dictation chunks |
| Groq API | `groq` Python SDK (OpenAI-compatible) | Official, handles retries |
| Global hotkey | `keyboard` (or `pynput`) | Toggle record/paste anywhere |
| Text injection | Win32 `SendInput` via `ctypes` | Types into any focused app |
| Optional partials | `faster-whisper` (tiny.en int8, local) | Instant watermark text (~300ms) |
| Config/keys | `.env` file (GROQ_API_KEY) | No hardcoded secrets |
| Packaging | `pyinstaller` (later phase) | Single .exe for the 12GB/CPU laptop |

---

## 3. Milestones

### M0 — Foundation (day 1)
- Scaffold repo: `src/`, `tests/`, `.env.example`, `requirements.txt`
- CLI app skeleton with config loading (Groq key, hotkey, model names)
- Verify Groq key works: transcribe a bundled test WAV via
  `whisper-large-v3-turbo`, print transcript. **Acceptance:** clean transcript.

### M1 — MVP: Push-to-Talk Dictation (days 2-4)
- Global hotkey (e.g. `Ctrl+Space`) toggles recording
- Microphone capture → 16kHz mono WAV chunk → upload to Groq
- Raw transcript → cleanup pass with `llama-3.3-70b-versatile`:
  - remove filler words ("um", "uh"), fix grammar/punctuation
  - optional "code mode" (instruct model to format as code comments)
- Text injected into focused window via SendInput
- Rate-limit handling: read `x-ratelimit-remaining` / `retry-after` headers,
  queue on 429, local fallback message on exhaustion
- Tray icon with status (recording / transcribing / idle)
- **Acceptance:** 3N dictation session works with ~1-3s end-to-end latency,
  no 429 crashes, pastes correctly into notepad/VS Code/browser.

### M2 — "As you speak" feel (days 5-7)
- Streaming UX: during dictation, send fixed 2-3s slices; merge results with
  overlap-diff algorithm (delete-then-retype final text)
- Optional local `tiny` watermark: transcribe first 400ms locally for
  instant placeholder text while Groq's accurate pass replaces it
- Auto-punctuation: add period after 1.5s silence; newline on long pause
- **Acceptance:** holding the hotkey and speaking shows text appearing
  progressively with ~1-2s lag, final text is accurate.

### M3 — Polish & packaging (days 8-10)
- Voice activity detection on push-to-talk + "tap-to-start" mode
- Language support (config: auto-detect or fixed, e.g. `en`, `hi`)
- `.exe` build via PyInstaller; runs on the 12GB/no-GPU laptop
- Error surfaces: toast on rate limit, log file, hotkey remap UI
- **Acceptance:** fresh Windows install → 2-min setup → working dictation.

### M4 — Optional fallbacks (if rate limits bite)
- Rotate to Deepgram ($200 credit) / AssemblyAI free tier as secondary provider
- Full-local mode: `faster-whisper` `small` int8 + `llama.cpp` 3B int4
  (draft-level latency, unlimited quota)

---

## 4. Rate Limit Budget (Groq free, per org)

| Model | RPM | RPD | Audio limits |
|---|---|---|---|
| whisper-large-v3-turbo | 20 | 2,000 | 7,200s/hour, 28,800s/day |
| llama-3.3-70b-versatile | 30 | 1,000 | 12K tokens/min |

- Typical 200-sentence dictation day → ~10% of daily Whisper quota.
- Headers returned: `x-ratelimit-*`, `retry-after` → implement graceful queueing.
- Limits apply per organization; one user = ample headroom, ~20 concurrent
  users max. Not a multi-tenant product — fine for personal use.

---

## 5. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| 429 rate limiting mid-session | Queue + retry-after backoff, tray warning, warm cache prompt |
| Free tier throttling change | Provider abstraction (M4: Deepgram/AssemblyAI/local fallback) |
| Cloud latency spikes | Streaming UX makes lag invisible; local tiny watermark fills gaps |
| Whisper drafts (no formatting) | LLM cleanup pass is mandatory in pipeline |
| Mic/audio quality on laptop | 16kHz capture, optional noise gate via VAD levels |
| Privacy (audio to cloud) | Mode toggle: local-only transcription option |

---

## 6. Repo Layout (target)

```
src/
  main.py            # entrypoint, hotkey loop, tray
  audio_capture.py   # sounddevice capture, VAD segmentation
  transcribe.py      # Groq whisper client + retry/429 handling
  cleanup.py         # Groq LLM cleanup prompt (incl. code mode)
  inject.py          # SendInput text injection
  config.py          # .env + settings
  ui/                # tray icon, status toasts
tests/
  test_transcribe.py # fixture WAVs, mock 429
  test_cleanup.py    # prompt output checks
.env.example
requirements.txt
PLAN.md
```

---

## 7. Acceptance Summary

- Free ($0) end-to-end on Groq free tier
- Dictation feel: ~1-3s E2E, "as-you-speak" partials in M2
- Accuracy: large-v3 (better than local `small`) + 70B cleanup formatting
- Runs entirely locally for UI/hotkey/capture; no Render/Railway dependency