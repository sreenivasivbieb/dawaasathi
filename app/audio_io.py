"""Uploaded audio -> something Prisma accepts (WAV/MP3/OGG/FLAC/AAC/M4A, <= 30 s)."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

from app.adapters.gnani_stt import PRISMA_FORMATS

MAX_SECONDS = 30.0


def wav_duration(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return None


def to_wav(data: bytes, sample_rate: int = 16000, max_seconds: float = MAX_SECONDS) -> bytes:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to convert this audio format")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-t", str(max_seconds),
         "-ac", "1", "-ar", str(sample_rate), "-f", "wav", "pipe:1"],
        input=data, capture_output=True, timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError("could not decode audio: " + proc.stderr.decode()[:200])
    return proc.stdout


def prepare_for_prisma(data: bytes, filename: str | None) -> tuple[bytes, str]:
    name = (filename or "audio.wav").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext == ".wav":
        dur = wav_duration(data)
        if dur is not None and dur <= MAX_SECONDS:
            return data, "audio.wav"
        return to_wav(data), "audio.wav"
    if ext in PRISMA_FORMATS and ext != ".wav":
        # Can't cheaply check duration without decoding; let ffmpeg trim to be safe.
        return to_wav(data), "audio.wav"
    return to_wav(data), "audio.wav"


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return out.getvalue()
