import pytest
from app.config import LANGS


@pytest.mark.parametrize("lang", LANGS)
def test_localized_result_preserves_facts_and_history(client, lang):
    original = client.post("/v1/query", data={"text": "Dolo 650 price and generic", "lang": "en"}).json()
    tid = original["session_id"]
    localized = client.get(f"/v1/threads/{tid}/localized", params={"lang": lang}).json()
    response = localized["messages"][-1]["response"]
    assert response["lang"] == lang
    assert response["script"]["lang"] == lang
    if lang != "en":
        assert response["script"]["text"] != original["script"]["text"]
    for key in ("items", "price", "safety", "verdict", "alternatives", "flags"):
        assert response[key] == original[key]
    assert response["audio_stream_url"] is None
    # Reading another language must not rewrite the original history or create another query.
    stored = client.get(f"/v1/threads/{tid}").json()["messages"]
    assert len(stored) == 2
    assert stored[-1]["response"]["script"]["text"] == original["script"]["text"]
    assert localized["messages"][0]["text"] == "Dolo 650 price and generic"


def test_localization_preserves_pending_confirmation(client):
    original = client.post("/v1/query", data={"text": "Glycomet", "lang": "en"}).json()
    tid = original["session_id"]
    localized = client.get(f"/v1/threads/{tid}/localized?lang=ta").json()["messages"][-1]["response"]
    assert localized["status"] == "needs_confirmation"
    assert localized["candidates"] == original["candidates"]
    choice = client.post("/v1/confirm", json={"session_id": tid, "candidate_id": localized["candidates"][0]["candidate_id"], "lang": "ta"}).json()
    assert choice["status"] == "ok"
    assert choice["lang"] == "ta"


def test_localized_history_requires_known_thread(client):
    assert client.get("/v1/threads/s_000000000000/localized?lang=hi").status_code == 404
