"""Bounded utterances and mono signed little-endian PCM conversion."""
from collections import deque
import subprocess

import numpy as np


def resample(pcm: bytes, source: int, target: int) -> bytes:
    if len(pcm) % 2 or source not in (8000, 12000, 16000, 24000, 32000, 44100, 48000, 96000, 192000) or target not in (8000, 16000):
        raise ValueError("unsupported PCM format")
    if len(pcm) > source * 2 * 120:
        raise ValueError("audio exceeds conversion limit")
    if source == target:
        return pcm
    try:
        process = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(source),
             "-ac", "1", "-i", "pipe:0", "-ar", str(target), "-ac", "1", "-f", "s16le", "pipe:1"],
            input=pcm, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("PCM converter unavailable or timed out") from exc
    if process.returncode or not process.stdout:
        raise ValueError("PCM conversion failed")
    return process.stdout


class Segmenter:
    """Energy VAD over 20 ms frames, with preroll and bounded speech turns.

    Tune threshold for the microphone/noise floor. Headphones help prevent TTS
    leakage from being mistaken for a caller interruption.
    """
    def __init__(self, threshold: float = 350, max_seconds: float = 20):
        self.threshold = threshold
        self.max_frames = int(max_seconds * 50)
        self.pending = bytearray()
        self.pre = deque(maxlen=10)
        self.frames = []
        self.voiced = 0
        self.quiet = 0
        self.start_count = 0
        self.active = False

    def feed(self, data: bytes) -> tuple[bool, list[bytes]]:
        self.pending.extend(data)
        started = False
        utterances = []
        while len(self.pending) >= 320:
            frame = bytes(self.pending[:320])
            del self.pending[:320]
            values = np.frombuffer(frame, dtype='<i2').astype(np.float32)
            speech = float(np.sqrt(np.mean(values * values))) >= self.threshold
            if not self.active:
                self.pre.append(frame)
                self.start_count = self.start_count + 1 if speech else 0
                if self.start_count < 6:  # 120 ms before treating noise as speech.
                    continue
                self.active = True
                started = True
                self.frames = list(self.pre)
                self.pre.clear()
                self.voiced = 6
                self.quiet = 0
                continue
            self.frames.append(frame)
            self.voiced += int(speech)
            self.quiet = 0 if speech else self.quiet + 1
            if self.quiet >= 35 or len(self.frames) >= self.max_frames:
                if self.voiced >= 12:
                    utterances.append(b''.join(self.frames))
                self.frames = []
                self.active = False
                self.start_count = 0
                self.quiet = 0
        return started, utterances
