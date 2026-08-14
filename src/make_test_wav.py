"""Generate a test WAV with synthetic speech using Windows SAPI (no TTS download needed)."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
SILENCE_SECS = 1.0


def _sapi_speech(text: str, out_path: Path) -> None:
    escaped = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$t = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$t.SetOutputToWaveFile('{out_path}'); "
        f"$t.Speak('{escaped}'); $t.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello world. This is a Groq whisper transcription test."
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent.parent / "tests" / "fixtures" / "sample.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = out.with_suffix(".tmp.wav")
    _sapi_speech(text, tmp)

    with wave.open(str(tmp), "rb") as src:
        nch = src.getnchannels()
        pcm = np.frombuffer(src.readframes(src.getnframes()), dtype=np.int16)
        if nch > 1:
            pcm = pcm[::nch]

    silence = np.zeros(int(SILENCE_SECS * SAMPLE_RATE), dtype=np.int16)
    pcm = np.concatenate([silence, pcm, silence])

    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(SAMPLE_RATE)
        dst.writeframes(pcm.tobytes())
    tmp.unlink(missing_ok=True)
    print(f"Wrote {out} ({len(pcm) / SAMPLE_RATE:.1f}s)")


if __name__ == "__main__":
    main()