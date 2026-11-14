"""Gnani Timbre TTS: SSE streaming (raw 16-bit PCM chunks) and REST, plus a mock.

The SSE endpoint allows only one active request per key, so every call goes through a
process-wide lock. Audio is requested as raw linear PCM so cached segments can be joined.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections.abc import AsyncIterator

import httpx

from app.config import LANGS, Settings

_LOCK = asyncio.Lock()


class TTSError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def tts_language(lang: str) -> str:
    return LANGS.get(lang, LANGS["hi"])[1]


def strip_wav_header(chunk: bytes) -> bytes:
    if chunk[:4] != b"RIFF" or chunk[8:12] != b"WAVE":
        return chunk
    pos = 12
    while pos + 8 <= len(chunk):
        cid = chunk[pos : pos + 4]
        size = struct.unpack("<I", chunk[pos + 4 : pos + 8])[0]
        if cid == b"data":
            return chunk[pos + 8 :]
        pos += 8 + size
    return b""


class TimbreTTS:
    live = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client
        self.sample_rate = settings.tts_sample_rate

    def _body(self, text: str, lang: str, sample_rate: int | None = None) -> dict:
        return {
            "text": text,
            "model": self.s.tts_model,
            "voice": self.s.tts_voice,
            "language": tts_language(lang),
            "speed": self.s.tts_speed,
            "audio_config": {
                "sample_rate": sample_rate or self.sample_rate,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": "raw",
            },
        }

    async def stream(self, text: str, lang: str, sample_rate: int | None = None) -> AsyncIterator[bytes]:
        """Yield raw PCM s16le mono chunks. Retries once if nothing was produced yet."""
        for attempt in range(2):
            produced = False
            try:
                async with _LOCK:
                    async for chunk in self._sse(text, lang, sample_rate):
                        produced = True
                        yield chunk
                return
            except TTSError as exc:
                if produced or not exc.retryable or attempt == 1:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if produced or attempt == 1:
                    raise TTSError("NETWORK", str(exc), True) from exc
            await asyncio.sleep(0.3)

    async def _sse(self, text: str, lang: str, sample_rate: int | None) -> AsyncIterator[bytes]:
        headers = {"X-API-Key-ID": self.s.gnani_api_key or "", "Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        async with self.client.stream(
            "POST", f"{self.s.gnani_base_url}/api/v1/tts/sse", headers=headers,
            json=self._body(text, lang, sample_rate), timeout=httpx.Timeout(20.0, connect=5.0),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise TTSError(f"HTTP_{resp.status_code}", body, resp.status_code in (429, 500, 502, 503, 504))
            event = None
            first = True
            async for line in resp.aiter_lines():
                if not line:
                    event = None
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                kind = event or data.get("event") or data.get("type")
                if kind == "error":
                    raise TTSError(data.get("type", "TTS_ERROR"), data.get("message", ""), False)
                audio_b64 = data.get("audio")
                if audio_b64:
                    pcm = base64.b64decode(audio_b64)
                    if first:
                        pcm = strip_wav_header(pcm)
                        first = False
                    if pcm:
                        yield pcm
                if data.get("is_final") or kind == "complete":
                    return

    async def synthesize(self, text: str, lang: str, sample_rate: int | None = None) -> bytes:
        for attempt in range(2):  # retry once on a connect / network error
            try:
                async with _LOCK:
                    resp = await self.client.post(
                        f"{self.s.gnani_base_url}/api/v1/tts/inference",
                        headers={"X-API-Key-ID": self.s.gnani_api_key or "", "Content-Type": "application/json"},
                        json=self._body(text, lang, sample_rate), timeout=httpx.Timeout(30.0, connect=8.0),
                    )
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 1:
                    raise TTSError("NETWORK", str(exc) or type(exc).__name__, True) from exc
                await asyncio.sleep(0.5)
        if resp.status_code != 200:
            raise TTSError(f"HTTP_{resp.status_code}", resp.text[:300], resp.status_code >= 500)
        return strip_wav_header(resp.content)


class MockTTS:
    """No key configured: produce silence of a plausible length; the PWA falls back to the
    browser's speechSynthesis when the response says tts == "mock"."""

    live = False

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    async def stream(self, text: str, lang: str, sample_rate: int | None = None) -> AsyncIterator[bytes]:
        sr = sample_rate or self.sample_rate
        seconds = min(8.0, 0.12 * max(1, len(text.split())))
        total = int(sr * seconds) * 2
        step = sr // 5 * 2
        for start in range(0, total, step):
            await asyncio.sleep(0)
            yield bytes(min(step, total - start))

    async def synthesize(self, text: str, lang: str, sample_rate: int | None = None) -> bytes:
        return b"".join([c async for c in self.stream(text, lang, sample_rate)])
