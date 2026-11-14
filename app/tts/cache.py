"""Clause-level TTS segment cache and reply-audio assembly.

Key = sha1(lang | voice | text | model | sample_rate). Fixed lines (doctor line, warnings,
fillers) hit the cache; slot-filled clauses are streamed from Timbre while being written to
the cache. A reply is an AudioJob: segments are produced in order into a growing PCM buffer
that any number of HTTP / WebSocket readers can follow while it is still being made.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import time
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.metrics import METRICS

GAP_MS = 90
FADE_MS = 8


class SegmentCache:
    def __init__(self, directory: Path, voice: str, model: str):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.voice = voice
        self.model = model

    def key(self, lang: str, text: str, sample_rate: int) -> str:
        raw = f"{lang}|{self.voice}|{text}|{self.model}|{sample_rate}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.pcm"

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        if p.exists():
            return p.read_bytes()
        return None

    def put(self, key: str, pcm: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent replies may synthesize the same clause at once.
        with tempfile.NamedTemporaryFile(dir=p.parent, suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            f.write(pcm)
        try:
            tmp.replace(p)
        finally:
            tmp.unlink(missing_ok=True)

    def has(self, key: str) -> bool:
        return self._path(key).exists()


def _fade(pcm: bytes, sample_rate: int, fade_in: bool = True, fade_out: bool = True) -> bytes:
    if len(pcm) < 4:
        return pcm
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    n = min(len(a) // 2, int(sample_rate * FADE_MS / 1000))
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        if fade_in:
            a[:n] *= ramp
        if fade_out:
            a[-n:] *= ramp[::-1]
    return a.astype(np.int16).tobytes()


@dataclass
class AudioJob:
    id: str
    lang: str
    segments: list[str]
    sample_rate: int
    live: bool
    chunks: list[bytes] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    first_chunk_ms: float | None = None
    created: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def _push(self, chunk: bytes, t0: float) -> None:
        if not chunk:
            return
        async with self._cond:
            if self.first_chunk_ms is None:
                self.first_chunk_ms = (time.perf_counter() - t0) * 1000
                METRICS.observe("tts_first_chunk", self.first_chunk_ms)
            self.chunks.append(chunk)
            self._cond.notify_all()

    async def _finish(self, error: str | None = None) -> None:
        async with self._cond:
            self.done = True
            self.error = error
            self._cond.notify_all()

    async def run(self, tts, cache: SegmentCache) -> None:
        t0 = time.perf_counter()
        gap = bytes(int(self.sample_rate * GAP_MS / 1000) * 2)
        failed = None
        try:
            for i, text in enumerate(self.segments):
                if i:
                    await self._push(gap, t0)
                key = cache.key(self.lang, text, self.sample_rate)
                cached = cache.get(key) if self.live else None
                if cached is not None:
                    self.cache_hits += 1
                    METRICS.count("tts_cache_hit")
                    await self._push(_fade(cached, self.sample_rate), t0)
                    continue
                self.cache_misses += 1
                METRICS.count("tts_cache_miss")
                buf = bytearray()
                try:
                    first = True
                    async for chunk in tts.stream(text, self.lang, self.sample_rate):
                        if first:
                            chunk = _fade(chunk, self.sample_rate, fade_out=False)
                            first = False
                        buf.extend(chunk)
                        await self._push(chunk, t0)
                except Exception as exc:  # fall back to whatever is cached; text card still shows
                    failed = f"{type(exc).__name__}: {exc}"[:200]
                    METRICS.count("tts_error")
                    continue
                if self.live and buf:
                    cache.put(key, bytes(buf))
        except asyncio.CancelledError:
            await self._finish("cancelled")
            raise
        await self._finish(failed)

    async def iter_pcm(self):
        sent = 0
        while True:
            async with self._cond:
                while sent >= len(self.chunks) and not self.done:
                    await self._cond.wait()
                new = self.chunks[sent:]
                finished = self.done
            for c in new:
                yield c
            sent += len(new)
            if finished and sent >= len(self.chunks):
                return

    def pcm(self) -> bytes:
        return b"".join(self.chunks)


class AudioService:
    def __init__(self, tts, cache: SegmentCache, sample_rate: int, ttl_s: int = 900):
        self.tts = tts
        self.cache = cache
        self.sample_rate = sample_rate
        self.ttl_s = ttl_s
        self.jobs: dict[str, AudioJob] = {}

    @property
    def live(self) -> bool:
        return bool(getattr(self.tts, "live", False))

    def create(self, lang: str, segments: list[str], sample_rate: int | None = None) -> AudioJob:
        self._gc()
        job = AudioJob(id=uuid.uuid4().hex[:16], lang=lang, segments=[s for s in segments if s.strip()],
                       sample_rate=sample_rate or self.sample_rate, live=self.live)
        job.task = asyncio.create_task(job.run(self.tts, self.cache))
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> AudioJob | None:
        self._gc()
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.task and not job.task.done():
            job.task.cancel()

    def all_cached(self, lang: str, segments: list[str], sample_rate: int | None = None) -> bool:
        sr = sample_rate or self.sample_rate
        return self.live and all(self.cache.has(self.cache.key(lang, s, sr)) for s in segments)

    async def prewarm(self, items: list[tuple[str, str]], sample_rate: int | None = None) -> dict:
        sr = sample_rate or self.sample_rate
        made = skipped = failed = 0
        for lang, text in items:
            key = self.cache.key(lang, text, sr)
            if self.cache.has(key):
                skipped += 1
                continue
            if not self.live:
                failed += 1
                continue
            try:
                pcm = b"".join([c async for c in self.tts.stream(text, lang, sr)])
                self.cache.put(key, pcm)
                made += 1
            except Exception:
                failed += 1
        return {"synthesized": made, "already_cached": skipped, "failed": failed}

    def _gc(self) -> None:
        now = time.time()
        for jid in [j for j, job in self.jobs.items() if now - job.created > self.ttl_s]:
            self.cancel(jid)
            self.jobs.pop(jid, None)


def encode(pcm: bytes, sample_rate: int, fmt: str) -> tuple[bytes, str]:
    """PCM s16le mono -> wav | ogg (Opus) via ffmpeg."""
    if fmt == "wav" or not shutil.which("ffmpeg"):
        import io
        import wave

        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        return out.getvalue(), "audio/wav"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sample_rate),
            "-ac", "1", "-i", "pipe:0"]
    if fmt == "ogg":
        args += ["-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-f", "ogg", "pipe:1"]
        mime = "audio/ogg"
    else:
        args += ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"]
        mime = "audio/mpeg"
    proc = subprocess.run(args, input=pcm, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[:300])
    return proc.stdout, mime
