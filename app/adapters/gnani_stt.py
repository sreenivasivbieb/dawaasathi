"""Gnani Prisma STT: REST client (audio up to 30 s) with one retry, plus a mock for tests.

Docs: POST {base}/stt/v3, multipart `audio_file`, header X-API-Key-ID.
Realtime streaming is proxied in app/api/stream.py (browsers cannot set the auth header).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from app.config import LANGS, Settings

PRISMA_FORMATS = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac",
                  ".aac": "audio/aac", ".m4a": "audio/mp4"}

TOPIC = ("A patient in India asking a chemist about a medicine: brand names, salt names, "
         "strengths in mg or ml, tablet, capsule, syrup, generic, Jan Aushadhi, price.")


class STTError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class STTResult:
    text: str
    detected_lang: str | None
    latency_ms: float
    request_id: str | None = None
    model: str | None = None


def language_code(settings: Settings, lang: str) -> str:
    if lang == "auto":
        return "indic-auto" if settings.stt_model.startswith("prisma-v3") else "hi-IN"
    code = LANGS.get(lang, LANGS["hi"])[0]
    if settings.stt_model.startswith("prisma-v3") and settings.stt_codemix_with_english and code != "en-IN":
        return f"{code},en-IN"
    return code


class PrismaSTT:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        lang: str,
        keyterms: list[str] | None = None,
        previous_text: str | None = None,
    ) -> STTResult:
        v3 = self.s.stt_model.startswith("prisma-v3")
        data: dict[str, str] = {"language_code": language_code(self.s, lang)}
        if v3:
            data["model"] = self.s.stt_model
            data["format"] = self.s.stt_format
            data["topic"] = TOPIC
            if keyterms:
                data["keyterms"] = json.dumps(keyterms[:200], ensure_ascii=False)
            if previous_text:
                data["previous_text"] = previous_text[-600:]
        else:
            data["format"] = "transcribe"
            if keyterms:
                words = [k for k in keyterms if k.isalpha()][:100]
                data["bias_list"] = ",".join(words)
                data["bias_score"] = "1.0"
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".wav"
        mime = PRISMA_FORMATS.get(ext, "audio/wav")
        last_exc: STTError | None = None
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                resp = await self.client.post(
                    f"{self.s.gnani_base_url}/stt/v3",
                    headers={"X-API-Key-ID": self.s.gnani_api_key or ""},
                    data=data,
                    files={"audio_file": (filename, audio, mime)},
                    timeout=httpx.Timeout(15.0, connect=5.0),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = STTError("NETWORK", str(exc), retryable=True)
            else:
                latency = (time.perf_counter() - t0) * 1000
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("success") is False:
                        err = body.get("error") or {}
                        last_exc = STTError(err.get("type", "STT_ERROR"), err.get("message", ""), False)
                    else:
                        return STTResult(
                            text=(body.get("transcript") or "").strip(),
                            detected_lang=body.get("detected_language_code"),
                            latency_ms=latency,
                            request_id=body.get("request_id"),
                            model=body.get("model"),
                        )
                else:
                    last_exc = _error_from(resp)
            if not last_exc.retryable or attempt == 1:
                break
            await asyncio.sleep(0.4)
        assert last_exc is not None
        raise last_exc


def _error_from(resp: httpx.Response) -> STTError:
    retryable = resp.status_code in (429, 500, 502, 503, 504)
    try:
        body = resp.json()
    except ValueError:
        return STTError(f"HTTP_{resp.status_code}", resp.text[:200], retryable)
    if isinstance(body.get("error"), dict):
        return STTError(body["error"].get("type", f"HTTP_{resp.status_code}"),
                        body["error"].get("message", ""), retryable)
    detail = body.get("detail")
    if isinstance(detail, dict):
        return STTError(detail.get("error_code", f"HTTP_{resp.status_code}"), detail.get("message", ""), retryable)
    return STTError(body.get("code", f"HTTP_{resp.status_code}"), str(body)[:200], retryable)


class MockSTT:
    """Used when no GNANI_API_KEY is set and in tests. Returns a fixed transcript."""

    def __init__(self, transcript: str = ""):
        self.transcript = transcript

    async def transcribe(self, audio: bytes, filename: str, lang: str, keyterms=None, previous_text=None) -> STTResult:
        if not self.transcript:
            raise STTError("STT_NOT_CONFIGURED", "Set GNANI_API_KEY to transcribe audio, or send `text`.")
        return STTResult(text=self.transcript, detected_lang=None, latency_ms=0.0, model="mock")
