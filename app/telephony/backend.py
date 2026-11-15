"""HTTP adapter to the existing assistant; no medicine or provider logic here."""
import asyncio
from urllib.parse import urlsplit

import httpx

from app.audio_io import pcm_to_wav
from .audio import resample


class Backend:
    def __init__(self, client: httpx.AsyncClient, lang: str = "hi"):
        self.client = client
        self.lang = lang
        self.session_id = None
        self.audio_id = None
        self.auto_language = True

    async def query(self, pcm: bytes | None = None, text: str | None = None):
        data = {"lang": self.lang, "channel": "phone", "detect_language": str(self.auto_language).lower()}
        if self.session_id:
            data["session_id"] = self.session_id
        files = None
        if pcm is not None:
            converted = await asyncio.to_thread(resample, pcm, 8000, 16000)
            files = {"audio": ("call.wav", pcm_to_wav(converted, 16000), "audio/wav")}
        else:
            data["text"] = text or "hello"
        response = await self.client.post("/v1/query", data=data, files=files)
        response.raise_for_status()
        result = response.json()
        self.session_id = result["session_id"]
        path = self.remember(result)
        if result.get("lang", self.lang) not in ("hi", "en"):
            await self.interrupt()
            raise UnsupportedLanguage("Telephone calls support Hindi and English")
        self.lang = result.get("lang", self.lang)
        return path

    def remember(self, result):
        if result.get("tts") != "timbre":
            raise ValueError("live TTS is required for calling")
        self.audio_id = result["audio_id"]
        path = result["audio_stream_url"]
        if urlsplit(path).scheme or urlsplit(path).netloc or not path.startswith("/v1/audio/"):
            raise ValueError("invalid backend audio path")
        return path

    async def prompt(self, purpose: str):
        response = await self.client.post("/v1/phone/prompt", json={
            "lang": self.lang if self.lang in ("hi", "en") else "en", "purpose": purpose,
        })
        response.raise_for_status()
        return self.remember(response.json())

    async def interrupt(self):
        if self.session_id and self.audio_id:
            response = await self.client.post("/v1/interrupt", json={
                "session_id": self.session_id, "audio_id": self.audio_id,
            }, timeout=2)
            response.raise_for_status()

    async def pcm(self, path):
        async with self.client.stream("GET", path) as response:
            response.raise_for_status()
            if response.headers.get("X-TTS") != "timbre":
                raise ValueError("mock audio is not valid telephone output")
            rate = int(response.headers.get("X-Sample-Rate", "0"))
            if rate not in (8000, 12000, 16000, 24000, 32000, 44100, 48000, 96000, 192000):
                raise ValueError("unsupported backend sample rate")
            count = 0
            converted = bytearray()
            async for chunk in response.aiter_bytes():
                count += len(chunk)
                if count > rate * 2 * 120:
                    raise ValueError("backend audio exceeds limit")
                if rate == 8000:
                    yield chunk
                else:
                    converted.extend(chunk)
            if rate != 8000:
                yield await asyncio.to_thread(resample, bytes(converted), rate, 8000)


class UnsupportedLanguage(ValueError):
    pass
