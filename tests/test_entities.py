"""Rule-based entities + intent from code-mixed transcripts."""

from __future__ import annotations

import pytest

from app.nlu.entities import extract, tokenize


# ---------------------------------------------------------------- tokenizer
def test_tokenize_splits_glued_units_and_devanagari_digits():
    assert tokenize("Dolo650mg") == ["Dolo", "650", "mg"]
    assert tokenize("डोलो ६५०") == ["डोलो", "650"]
    assert tokenize("250mg/5ml") == ["250", "mg", "/", "5", "ml"]
    assert tokenize("1,000 mg") == ["1000", "mg"]
    assert tokenize("क्या यह सही है?") == ["क्या", "यह", "सही", "है"]


# ---------------------------------------------------------------- name tokens
@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("भैया मुझे ऑगमेंटिन छह सौ पच्चीस का सस्ता जेनेरिक बताओ", ["ऑगमेंटिन"]),
        ("डोलो 650 की कीमत कितनी है", ["डोलो"]),
        ("glycomet sr 500", ["glycomet"]),
        ("मॉन्टेयर एलसी", ["मॉन्टेयर", "एलसी"]),
        ("Augmentin 625 Duo tablet", ["Augmentin"]),
        ("please tell me the price of calpol syrup", ["calpol"]),
        ("मुझे दवा चाहिए", []),
        ("बुखार के लिए", []),
    ],
)
def test_name_tokens(text, names):
    assert extract(text).name_tokens == names


def test_ordinal_words_are_not_drug_names():
    # A confirmation answer must not look like a new drug name.
    for text in ("पहला", "पहला वाला", "पहली वाली", "दूसरा", "दुसरा", "तीसरा", "first", "the second", "third", "पहिला"):
        assert extract(text).name_tokens == [], text


# ---------------------------------------------------------------- strength / unit
@pytest.mark.parametrize(
    ("text", "strength", "unit"),
    [
        ("ऑगमेंटिन छह सौ पच्चीस", 625, None),
        ("dolo six fifty", 650, None),
        ("डोलो 650", 650, None),
        ("thyronorm 50 mcg", 50, "mcg"),
        ("glimepiride दो mg", 2, "mg"),
        ("linagliptin 5 mg", 5, "mg"),
        ("paracetamol zero point five g", 0.5, "g"),
        ("पैरासिटामोल पांच सौ मिलीग्राम", 500, "mg"),
        ("calpol 250 mg/5ml", 250, "mg"),
        ("crocin drops", None, None),
    ],
)
def test_strength_and_unit(text, strength, unit):
    e = extract(text)
    assert e.strength == strength
    assert e.unit == unit


def test_bata_do_is_not_strength_two():
    e = extract("बता दो")
    assert e.strength is None
    assert e.strengths == []
    e = extract("ऑगमेंटिन का जेनेरिक बता दो")
    assert e.strength is None
    assert e.name_tokens == ["ऑगमेंटिन"]


def test_counts_and_durations_are_not_strengths():
    assert extract("2 गोली dolo 650").strengths == [650]
    assert extract("10 दिन dolo 650").strengths == [650]
    assert extract("dolo 650 for 3 days").strengths == [650]


def test_two_numbers_both_kept():
    e = extract("amoxicillin 500 clavulanic 125")
    assert e.strengths == [500, 125]
    assert e.strength == 500


# ---------------------------------------------------------------- form / release / modifiers
@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("dolo 650 tablet", "tablet"),
        ("डोलो की गोली", "tablet"),
        ("mox 500 capsule", "capsule"),
        ("calpol सिरप", "oral_liquid"),
        ("crocin drops", "drops"),
        ("crocin ड्रॉप्स", "drops"),
        ("dolo 650", None),
    ],
)
def test_form(text, form):
    assert extract(text).form == form


@pytest.mark.parametrize(
    ("text", "release"),
    [
        ("glycomet sr 500", "SR"),
        ("ग्लाइकोमेट एसआर 500", "SR"),
        ("telma 40 xl", "ER"),
        ("pan 40 dr", "DR"),
        ("glycomet 500", None),
    ],
)
def test_release(text, release):
    assert extract(text).release == release


def test_modifiers():
    assert extract("augmentin 625 duo").modifiers == ["duo"]


# ---------------------------------------------------------------- intent
@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("भैया मुझे ऑगमेंटिन छह सौ पच्चीस का सस्ता जेनेरिक बताओ", "FIND_GENERIC"),
        ("dolo 650 ka generic", "FIND_GENERIC"),
        ("cheaper option for pan 40", "FIND_GENERIC"),
        ("ऑगमेंटिन 625", "FIND_GENERIC"),  # a bare name defaults to the generic lookup
        ("डोलो 650 की कीमत कितनी है", "PRICE_CHECK"),
        ("dolo 650 price", "PRICE_CHECK"),
        ("pan 40 का mrp", "PRICE_CHECK"),
        ("पैन 40 दिन में कितनी बार लेनी है", "DOSAGE_QUERY"),
        ("augmentin 625 how many times a day", "DOSAGE_QUERY"),
        ("dolo खाने के बाद लेनी है", "DOSAGE_QUERY"),
        ("पैन 40 सही है क्या", "VERIFY_MATCH"),
        ("is this the same as augmentin", "VERIFY_MATCH"),
        ("augmentin 625 असली है", "VERIFY_MATCH"),
        ("मुझे दवा चाहिए", "CLARIFY"),
        ("हाँ", "CLARIFY"),
    ],
)
def test_intent(text, intent):
    assert extract(text).intent == intent


def test_generic_beats_verify_on_tie():
    assert extract("सही सस्ता").intent == "FIND_GENERIC"


# ---------------------------------------------------------------- symptom
@pytest.mark.parametrize(
    ("text", "symptom"),
    [
        ("बुखार के लिए", "fever"),
        ("सिरदर्द के लिए dolo", "pain"),
        ("for fever", "fever"),
        ("खांसी की दवा", "cough"),
        ("शुगर की दवा glycomet", "diabetes"),
        ("dolo 650", None),
    ],
)
def test_symptom(text, symptom):
    e = extract(text)
    assert e.symptom == symptom
    if symptom:
        assert all(t not in ("बुखार", "fever", "सिरदर्द", "खांसी", "शुगर") for t in e.name_tokens)


# ---------------------------------------------------------------- yes / no / ordinal
@pytest.mark.parametrize("text", ["हाँ", "हां जी", "yes", "haan", "ठीक है", "होय"])
def test_yes(text):
    e = extract(text)
    assert e.yes and not e.no


@pytest.mark.parametrize("text", ["नहीं", "no", "nahi", "गलत", "नाही"])
def test_no(text):
    e = extract(text)
    assert e.no and not e.yes


@pytest.mark.parametrize(
    ("text", "ordinal"),
    [
        ("पहला", 1),
        ("पहला वाला", 1),
        ("first", 1),
        ("दूसरा", 2),
        ("दूसरी वाली", 2),
        ("second one", 2),  # the ordinal word wins over the numeral "one"
        ("तीसरा", 3),
        ("third", 3),
        ("पहिला", 1),  # Marathi
        ("दुसरा", 2),  # Marathi
    ],
)
def test_ordinal(text, ordinal):
    assert extract(text).ordinal == ordinal


def test_no_ordinal_in_long_utterances():
    assert extract("मुझे दूसरा वाला सस्ता जेनेरिक बताइए भैया").ordinal is None
