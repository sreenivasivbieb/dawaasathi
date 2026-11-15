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


@pytest.mark.parametrize("question", ["Dolo 650 price", "Dolo 650 used for", "zzzyyyqqq 777 price", "Glycomet"])
def test_voice_requests_use_language_helpers_but_respect_data1_only(make_client, monkeypatch, question):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.engine import checks

    client = make_client(stt=DetectedSTT("en-IN", question))
    pipeline = client.app.state.pipeline
    pipeline.s = replace(pipeline.s, groq_api_keys=("test-only-not-a-real-key",),
                         llm_mode="both", tavily_api_key="test-only", web_lookup_on=True, web_brands_on=True,
                         data1_only=True)
    pipeline.llm = object()
    understand = AsyncMock(side_effect=lambda text, ents, *args: ents)
    rewrite = AsyncMock(return_value=None)
    brand_lookup = AsyncMock(return_value=None)
    uses_lookup = AsyncMock(return_value=None)
    pipeline._llm_understand = understand
    pipeline._llm_rewrite = rewrite
    pipeline.web_brands = SimpleNamespace(lookup=brand_lookup)
    pipeline.web_uses = SimpleNamespace(lookup=uses_lookup)
    monkeypatch.setattr(checks, "usual_uses", lambda *args: None)
    result = client.post("/v1/query", data={"lang": "en"},
                         files={"audio": ("voice.wav", tiny_wav(), "audio/wav")}).json()
    assert result["status"] in {"ok", "unverified", "needs_image", "needs_confirmation"}
    if result["candidates"]:
        confirmed = client.post("/v1/confirm", json={"session_id": result["session_id"],
                                "candidate_id": result["candidates"][0]["candidate_id"], "lang": "en"})
        assert confirmed.status_code == 200
    understand.assert_awaited_once()
    if question == "Dolo 650 price":
        rewrite.assert_awaited_once()
    for helper in (brand_lookup, uses_lookup):
        helper.assert_not_awaited()


@pytest.mark.parametrize("channel", ["web", "stream", "phone"])
def test_spoken_misheard_strength_recovers_through_nlu(make_client, channel):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.adapters.llm import LLMResult

    client = make_client(stt=DetectedSTT("en-IN", "Tellmisartan Salis price"))
    pipeline = client.app.state.pipeline
    pipeline.s = replace(pipeline.s, groq_api_keys=("test-only",), llm_mode="nlu")
    understand = AsyncMock(return_value=LLMResult({"intent": "PRICE_CHECK", "drugs": [
        {"name": "Telmisartan", "strength": 40, "unit": "mg", "form": "tablet", "release": None}],
        "symptom": None}, 1.0, "test-only"))
    pipeline.llm = SimpleNamespace(understand=understand)
    data = {"lang": "en"}
    if channel == "web":
        result = client.post("/v1/query", data=data,
                             files={"audio": ("voice.wav", tiny_wav(), "audio/wav")}).json()
    else:
        # Streaming transcripts have already gone through STT; keep the voice pipeline enabled.
        import asyncio
        from app.pipeline import QueryInput
        result = asyncio.run(pipeline.query(QueryInput(text="Tellmisartan Salis price", lang="en",
                                                       channel=channel))).model_dump(mode="json")
    understand.assert_awaited_once()
    assert result["intent"] == "PRICE_CHECK"
    assert result["data_sources"]["llm"]["nlu"]["used"] is True
    item = result["items"]["focus"]
    assert item["salts"][0]["name"] == "telmisartan"
    assert item["salts"][0]["amount"] == 40


def test_typed_questions_keep_optional_text_helpers(make_client):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    client = make_client()
    pipeline = client.app.state.pipeline
    pipeline.s = replace(pipeline.s, groq_api_keys=("test-only-not-a-real-key",), llm_mode="both")
    pipeline.llm = object()
    understand = AsyncMock(side_effect=lambda text, ents, *args: ents)
    rewrite = AsyncMock(return_value=None)
    pipeline._llm_understand = understand
    pipeline._llm_rewrite = rewrite
    result = client.post('/v1/query', data={'text':'Dolo 650 price','lang':'en'}).json()
    assert result['status'] == 'ok'
    understand.assert_awaited_once()
    rewrite.assert_awaited_once()


def test_stale_interrupt_does_not_cancel_new_reply(client, monkeypatch):
    first = client.post('/v1/query', data={'text':'Dolo 650 price'}).json()
    second = client.post('/v1/query', data={'text':'Dolo 650 generic','session_id':first['session_id']}).json()
    cancelled=[]
    monkeypatch.setattr(client.app.state.audio,'cancel',cancelled.append)
    assert client.post('/v1/interrupt',json={'session_id':first['session_id'],'audio_id':first['audio_id']}).status_code == 200
    assert cancelled == []
    client.post('/v1/interrupt',json={'session_id':first['session_id'],'audio_id':second['audio_id']})
    assert cancelled == [second['audio_id']]
