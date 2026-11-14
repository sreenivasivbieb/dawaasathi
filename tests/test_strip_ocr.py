"""Pack photo -> medicine: detection + OCR reading + closed-set identification."""

from __future__ import annotations

import numpy as np

from app.adapters.detect import _decode
from app.adapters.ocr import OCRItem, OCRPayload, OCRResult, OCRSalt, group_rows
from app.engine.strip_text import SaltVocab, extract_salts, is_supplement
from app.models import SaltDose
from app.pipeline import Pipeline

VOCAB = SaltVocab(["paracetamol", "glibenclamide", "metformin", "diclofenac", "c", "hydrochloride",
                   "calcium carbonate"])


# ---------------------------------------------------------------- reading helpers
def test_model_kind_bottle_is_a_pack():
    assert OCRPayload.model_validate({"kind": "bottle", "items": []}).kind == "strip"
    assert OCRPayload.model_validate({"kind": "prescription", "items": []}).kind == "rx"


def test_rows_join_boxes_printed_side_by_side():
    cells = [{"t": "500 mg", "y": 52, "h": 10, "x": 200}, {"t": "Metformin I.P.", "y": 50, "h": 12, "x": 10},
             {"t": "Composition:", "y": 20, "h": 10, "x": 10}]
    assert group_rows(cells) == ["Composition:", "Metformin I.P. 500 mg"]


def test_salts_and_strengths_from_ocr_lines():
    lines = ["Glibenclamide & Metformin Hydrochloride", "Sustained Release Tablets", "5mg+500mg"]
    got = extract_salts(lines, VOCAB)
    assert [(s.name, s.amount) for s in got] == [("glibenclamide", 5.0), ("metformin", 500.0)]
    same_row = extract_salts(["GlibenclamideI.P. 5 mg Accent Pharma", "Metformin Hydrochloride 1.P. 500mg"], VOCAB)
    assert [(s.name, s.amount) for s in same_row] == [("glibenclamide", 5.0), ("metformin", 500.0)]


def test_ocr_noise_never_becomes_a_salt():
    assert extract_salts(["Epip Karthofl 500 mg", "Block-D, EPIP, Kartholl", "C 16 g"], VOCAB) == []
    assert [s.name for s in extract_salts(["Hydrochloride 500 mg"], VOCAB)] == []


def test_supplement_label():
    assert is_supplement("New High Power Formula\nDietary Supplemen")
    assert not is_supplement("Paracetamol Tablets IP")


def test_detector_decode_maps_back_to_photo_pixels():
    out = np.zeros((10, 8400), dtype=np.float32)
    out[:4, 0] = [320, 320, 200, 100]  # centre box in the 640 letterbox
    out[4 + 2, 0] = 0.7  # "bottle"
    dets = _decode(out, scale=0.5, px=0, py=0, w=1280, h=1280)
    assert dets[0].label == "bottle" and dets[0].box == (440, 540, 840, 740)


# ---------------------------------------------------------------- identification
def test_strength_must_agree_with_the_db_product():
    db = [SaltDose(name="paracetamol", amount=650, unit="mg")]
    assert not Pipeline._same_product([SaltDose(name="paracetamol", amount=500, unit="mg")], db)
    assert Pipeline._same_product([SaltDose(name="paracetamol")], db)


class _OCR:
    def __init__(self, payload: OCRPayload, provider: str):
        self.payload, self.provider = payload, provider

    async def read_strip(self, image):
        return OCRResult(self.payload.model_copy(deep=True), self.provider, 1.0)

    async def read_rx(self, image):
        return OCRResult(OCRPayload(kind="rx", readable=False), self.provider, 1.0)


def _ask(make_client, payload, provider="groq"):
    client = make_client(ocr=_OCR(payload, provider))
    r = client.post("/v1/query", data={"text": "यह कौन सी दवा है", "lang": "hi"},
                    files={"strip": ("s.jpg", b"\xff\xd8photo", "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_brand_answers_from_the_printed_salt_not_a_look_alike(make_client):
    # "Calpol 650" exists in the fixture DB as paracetamol 650 mg; the pack printed 500 mg.
    j = _ask(make_client, OCRPayload(kind="strip", items=[OCRItem(
        brand="Calpol 650", salts=[OCRSalt(name="Paracetamol", amount=500, unit="mg")], form="tablet")]))
    assert j["match"]["brand_id"] is None
    assert [s["amount"] for s in j["items"]["focus"]["salts"]] == [500.0]
    assert "BRAND_NOT_IN_DATA" in [f["code"] for f in j["flags"]]


def test_supplement_is_never_matched_to_a_medicine(make_client):
    j = _ask(make_client, OCRPayload(kind="strip", items=[OCRItem(brand="Calpol 650", category="supplement")]))
    assert j["match"]["brand_id"] is None
    codes = [f["code"] for f in j["flags"]]
    assert "SUPPLEMENT" in codes
    assert "सप्लीमेंट" in j["script"]["text"] and "Paracetamol" not in j["script"]["text"]


def test_offline_ocr_garbage_is_not_spoken(make_client):
    j = _ask(make_client, OCRPayload(kind="strip", items=[OCRItem(
        raw_text="12 Finsustainedrelease 0mg\nEpip Karthofl 500 mg\nBlock-D, EPIP", big_text=["Karthofl"])]),
        provider="rapidocr")
    assert j["items"]["focus"]["salts"] == []
    assert "Epip" not in j["script"]["text"] and "Karthofl" not in j["script"]["text"]


def test_offline_ocr_finds_brand_from_big_text(make_client):
    j = _ask(make_client, OCRPayload(kind="strip", items=[OCRItem(
        raw_text="Calpol 500\nEach tablet contains Paracetamol IP 500 mg", form="tablet",
        big_text=["Calpol 500", "Paracetamol Tablets IP"])]), provider="rapidocr")
    assert j["match"]["brand"] == "Calpol 500 Tablet"
    assert j["items"]["focus"]["composition_uncertain"] is False


def test_vlm_salt_names_with_pharmacopoeia_marks(make_client):
    j = _ask(make_client, OCRPayload(kind="strip", items=[OCRItem(
        brand="Calpol 500", salts=[OCRSalt(name="Paracetamol I.P.", amount=500, unit="mg")], form="tablet")]))
    assert j["match"]["brand"] == "Calpol 500 Tablet"
    assert [s["name"] for s in j["items"]["focus"]["salts"]] == ["paracetamol"]
