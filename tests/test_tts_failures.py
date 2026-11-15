import base64

import httpx
import pytest

from app.adapters.gnani_tts import TimbreTTS, TTSError


@pytest.mark.parametrize("mode", ["rest", "stream"])
async def test_busy_speech_key_retries_and_honors_retry_after(settings, monkeypatch, mode):
    calls, waits = [], []
    pcm = b"\x01\x02" * 100

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"detail": "one active request"})
        if mode == "rest":
            return httpx.Response(200, content=pcm)
        return httpx.Response(200, text='data: {"audio":"' + base64.b64encode(pcm).decode() + '","is_final":true}\n\n')

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("app.adapters.gnani_tts.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tts = TimbreTTS(settings, http)
        result = await tts.synthesize("test", "en") if mode == "rest" else b"".join([c async for c in tts.stream("test", "en")])
    assert result == pcm
    assert len(calls) == 2 and waits == [2]


@pytest.mark.parametrize("mode", ["rest", "stream"])
async def test_empty_provider_reply_is_a_bounded_error(settings, monkeypatch, mode):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"" if mode == "rest" else b'event: complete\ndata: {}\n\n')

    async def sleep(seconds):
        pass

    monkeypatch.setattr("app.adapters.gnani_tts.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tts = TimbreTTS(settings, http)
        with pytest.raises(TTSError, match="no audio"):
            if mode == "rest":
                await tts.synthesize("test", "en")
            else:
                _ = [c async for c in tts.stream("test", "en")]
    assert len(calls) == 3


def test_failed_reply_does_not_return_silence_as_success(make_client):
    class FailedTTS:
        live = True

        async def stream(self, *args):
            raise TTSError("HTTP_429", "provider busy", True)
            yield b""  # mark this as an async generator

    client = make_client(tts=FailedTTS())
    reply = client.post("/v1/query", data={"text": "Dolo 650 price", "lang": "en"}).json()
    audio = client.get(reply["audio_stream_url"])
    assert audio.status_code == 503
    assert audio.json()["error"]["code"] == "TTS_UNAVAILABLE"
    assert client.get(reply["audio_url"]).status_code == 503
    job = client.app.state.audio.get(reply["audio_id"])
    assert job.error and job.pcm() == b""
