import httpx
import pytest
from app.adapters.gnani_stt import STTResult, PrismaSTT, detected_language
from app.config import LANGS
from helpers import tiny_wav


class DetectedSTT:
    def __init__(self, language, text):
        self.language, self.text, self.request_lang = language, text, None

    async def transcribe(self, audio, filename, lang, *args, **kwargs):
        self.request_lang = lang
        return STTResult(self.text, self.language, 1.0, model="prisma-v3.0")


@pytest.mark.parametrize("ui,detected,expected,text", [
    ("en", "hi-IN", "hi", "डोलो 650 का दाम बताइए"),
    ("hi", "en-IN", "en", "Dolo 650 price"),
    ("en", "ta-IN", "ta", "Dolo 650 price"),
])
def test_audio_reply_uses_detected_language(make_client, ui, detected, expected, text):
    stt = DetectedSTT(detected, text)
    client = make_client(stt=stt)
    result = client.post("/v1/query", data={"lang": ui}, files={"audio": ("voice.wav", tiny_wav(), "audio/wav")}).json()
    assert stt.request_lang == "auto"
    assert result["detected_language"] == expected
    assert result["lang"] == result["script"]["lang"] == expected
    session = client.app.state.sessions.get(result["session_id"])
    assert session.lang == expected
    assert client.app.state.audio.get(result["audio_id"]).lang == expected
    history = client.get('/v1/threads/' + result["session_id"]).json()
    assert history["messages"][-1]["lang"] == expected


def test_language_detection_opt_out(make_client):
    stt = DetectedSTT("hi-IN", "Dolo 650 price")
    client = make_client(stt=stt)
    result = client.post("/v1/query", data={"lang": "en", "detect_language": "false"}, files={"audio": ("voice.wav", tiny_wav(), "audio/wav")}).json()
    assert stt.request_lang == "en"
    assert result["lang"] == "en"


async def test_auto_mode_overrides_legacy_configured_model(settings):
    calls = []
    async def handler(request):
        body = (await request.aread()).decode('latin1')
        calls.append(body)
        return httpx.Response(200, json={"transcript": "Dolo 650", "detected_language_code": "hi-IN"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await PrismaSTT(settings, http).transcribe(tiny_wav(), "voice.wav", "auto")
    assert 'prisma-v3.0' in calls[0] and 'indic-auto' in calls[0]
    assert result.detected_lang == "hi-IN"


def test_only_gnani_supported_languages_are_exposed():
    assert set(LANGS) == {'hi','en','mr','bn','gu','pa','ta','te','kn','ml'}
    assert detected_language('or-IN') is None
    assert detected_language('unrecognized') is None


def test_empty_transcript_has_a_recoverable_speech_error(make_client):
    client = make_client(stt=DetectedSTT("en-IN", ""))
    result = client.post("/v1/query", files={"audio": ("quiet.wav", tiny_wav(), "audio/wav")}).json()
    assert result["status"] == "error"
    assert result["data_sources"]["error"]["code"] == "NO_SPEECH"
    assert result["match"] is None
