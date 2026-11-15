"""Regressions for failures found while exercising the real server and sample uploads."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.adapters.ocr import parse_ocr_text
from app.tts.cache import SegmentCache


def test_prescription_raw_text_stays_with_its_own_medicine():
    payload = parse_ocr_text(
        "Demo Patient\nTab. Glycomet 500\n1-0-1 after food\nTab. Dolo 650\n1-0-0 x 3 days", "rx")
    assert [item.brand for item in payload.items] == ["Glycomet 500", "Dolo 650"]
    assert [item.raw_text for item in payload.items] == ["Glycomet 500", "Dolo 650"]
    assert payload.items[0].directions == "1-0-1 after food"


@pytest.mark.parametrize("followup,intent", [
    ("What is it used for?", "USE_QUERY"),
    ("price?", "PRICE_CHECK"),
    ("cheaper alternatives?", "FIND_GENERIC"),
    ("इसके उपयोग क्या हैं?", "USE_QUERY"),
])
def test_reference_followup_keeps_last_medicine(client, followup, intent):
    first = client.post("/v1/query", data={"text": "Dolo 650", "lang": "en"}).json()
    result = client.post("/v1/query", data={"text": followup, "lang": "en", "session_id": first["session_id"]}).json()
    assert result["session_id"] == first["session_id"]
    assert result["status"] == "ok"
    assert result["intent"] == intent
    assert result["items"]["focus"]["brand"] == "Dolo 650 Tablet"


def test_new_named_question_does_not_reuse_last_medicine(client):
    first = client.post("/v1/query", data={"text": "Dolo 650"}).json()
    result = client.post("/v1/query", data={"text": "Glycomet 500 price", "session_id": first["session_id"]}).json()
    # IR/SR variants may require a choice; either way the previous Dolo must not leak in.
    names = [result["match"]["brand"]] if result.get("match") else [c["brand"] for c in result["candidates"]]
    assert names and all("Glycomet" in name for name in names)


def test_concurrent_cache_writes_have_separate_temporary_files(tmp_path):
    cache = SegmentCache(tmp_path, "voice", "model")
    key = cache.key("en", "same clause", 24000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: cache.put(key, b"1234"), range(40)))
    assert cache.get(key) == b"1234"
    assert not list(tmp_path.rglob("*.tmp"))


def test_expired_audio_is_no_longer_served(client):
    result = client.post("/v1/query", data={"text": "Dolo 650"}).json()
    job = client.app.state.audio.get(result["audio_id"])
    job.created -= client.app.state.settings.session_ttl_s + 1
    assert client.get(result["audio_url"]).status_code == 404


@pytest.mark.parametrize("start,code", [([], "EXPECTED_START"), ({"type": "start", "sample_rate": "invalid"}, "BAD_SAMPLE_RATE")])
def test_invalid_websocket_start_is_a_protocol_error(client, start, code):
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json(start)
        assert ws.receive_json()["code"] == code


def test_websocket_recovers_after_invalid_message(client):
    with client.websocket_connect("/v1/stream") as ws:
        ws.send_json({"type": "start", "lang": "xx", "session_id": 42})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json([])
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
        ws.send_json({"type": "text", "text": 42})
        assert ws.receive_json()["code"] == "BAD_MESSAGE"
        ws.send_json({"type": "text", "text": "Dolo 650 price"})
        result = ws.receive_json()
        assert result["type"] == "result"
        assert result["status"] == "ok"
        assert result["lang"] == "hi"
        ws.send_json({"type": "stop"})
