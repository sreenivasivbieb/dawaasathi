import pytest


@pytest.mark.parametrize("text,lang", [("hello", "en"), ("Hi!", "en"), ("नमस्ते", "hi"), ("வணக்கம்", "ta")])
def test_greetings_never_become_drug_candidates(client, text, lang, monkeypatch):
    def no_match(*args, **kwargs):
        raise AssertionError("a greeting must not search for a medicine")
    monkeypatch.setattr(client.app.state.index, "search", no_match)
    response = client.post("/v1/query", data={"text": text, "lang": lang}).json()
    assert response["status"] == "ok"
    assert response["card"]["title"] == "GREETING"
    assert not response["candidates"] and response["match"] is None
    assert response["script"]["text"]
    localized = client.get(f"/v1/threads/{response['session_id']}/localized?lang=hi")
    assert localized.status_code == 200
    assert localized.json()["messages"][-1]["response"]["card"]["title"] == "GREETING"


def test_greeting_word_in_a_medicine_question_is_not_discarded(client):
    response = client.post("/v1/query", data={"text": "Hello, Dolo 650 price", "lang": "en"}).json()
    assert response["match"]["brand"] == "Dolo 650 Tablet"
    assert response["intent"] == "PRICE_CHECK"


def test_saved_reply_replays_without_short_term_session_or_browser_voices(client):
    first = client.post("/v1/query", data={"text": "hello", "lang": "en"}).json()
    client.app.state.sessions.forget(first["session_id"])
    result = client.post("/v1/speak", json={"session_id": first["session_id"], "which": "reply", "lang": "hi"})
    assert result.status_code == 200
    result = result.json()
    assert "नमस्ते" in result["text"]
    assert result["audio_url"].endswith(".wav")
    audio = client.get(result["audio_url"])
    assert audio.status_code == 200 and audio.content.startswith(b"RIFF")
    assert client.post("/v1/speak", json={"session_id": "s_000000000000", "which": "reply"}).status_code == 404
