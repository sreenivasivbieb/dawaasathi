"""Spoken numbers (Hindi / Marathi / English / romanised Hindi) -> values, and back to words."""

from __future__ import annotations

import pytest

from app.nlu.entities import tokenize
from app.nlu.numbers import is_number_token, number_to_words, parse_number, parse_numbers


def values(text: str) -> list[float]:
    return [s.value for s in parse_numbers(tokenize(text))]


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("छह सौ पच्चीस", [625]),
        ("पांच सौ एक सौ पच्चीस", [500, 125]),
        ("ढाई सौ", [250]),
        ("डेढ़ हज़ार", [1500]),
        ("साढ़े तीन सौ", [350]),
        ("सहाशे पंचवीस", [625]),  # Marathi
        ("पाचशे", [500]),  # Marathi
        ("six hundred twenty five", [625]),
        ("six twenty five", [625]),
        ("छह पच्चीस", [625]),  # colloquial "six twenty-five"
        ("two fifty", [250]),
        ("500 125", [500, 125]),
        ("शून्य दशमलव पांच", [0.5]),
        ("zero point five", [0.5]),
        ("५००", [500]),  # Devanagari digits
        ("सवा सौ", [125]),
        ("पौने तीन सौ", [275]),
        # extras
        ("छः सौ पच्चीस", [625]),  # visarga spelling
        ("पाँच सौ", [500]),  # chandrabindu spelling
        ("एक हज़ार", [1000]),
        ("six twenty-five", [625]),  # hyphenated English compound as ASR writes it
        ("sixty-five", [65]),
        ("chhe sau pachchis", [625]),  # romanised Hindi
    ],
)
def test_parse_numbers(spoken, expected):
    assert values(spoken) == expected


def test_spans_point_at_tokens():
    toks = tokenize("ऑगमेंटिन छह सौ पच्चीस का जेनेरिक")
    spans = parse_numbers(toks)
    assert len(spans) == 1
    s = spans[0]
    assert (s.start, s.end, s.value) == (1, 4, 625)
    assert toks[s.start:s.end] == ["छह", "सौ", "पच्चीस"]


def test_numbers_inside_sentence_are_found():
    assert values("भैया मुझे डोलो 650 का सस्ता जेनेरिक बताओ") == [650]
    assert values("dolo six fifty please") == [650]
    assert values("no numbers here") == []
    assert parse_number(tokenize("पैन चालीस")) == 40
    assert parse_number(tokenize("कुछ नहीं")) is None


def test_is_number_token():
    for tok in ("पच्चीस", "सौ", "ढाई", "पाचशे", "twenty", "५००", "625", "दशमलव", "sixty-five"):
        assert is_number_token(tok), tok
    for tok in ("डोलो", "augmentin", "mg", "twenty-dolo"):
        assert not is_number_token(tok), tok


@pytest.mark.parametrize("lang", ["hi", "mr", "en"])
def test_number_to_words_round_trip(lang):
    failures = []
    for n in range(0, 2001, 7):
        words = number_to_words(n, lang)
        got = [s.value for s in parse_numbers(words.split())]
        if got != [n]:
            failures.append((n, words, got))
    assert not failures, failures[:10]


@pytest.mark.parametrize("lang", ["hi", "mr", "en"])
@pytest.mark.parametrize("n", [10000, 12345, 99999, 100000, 150000])
def test_number_to_words_round_trip_large(lang, n):
    words = number_to_words(n, lang)
    assert [s.value for s in parse_numbers(words.split())] == [n], words


@pytest.mark.parametrize("lang", ["hi", "mr", "en"])
@pytest.mark.parametrize("value", [0.5, 2.5, 62.5, 1.25, 0.05, 12.75])
def test_number_to_words_decimals_round_trip(lang, value):
    words = number_to_words(value, lang)
    assert [s.value for s in parse_numbers(words.split())] == [value], words


def test_number_to_words_samples():
    assert number_to_words(625, "hi") == "छह सौ पच्चीस"
    assert number_to_words(625, "mr") == "सहाशे पंचवीस"
    assert number_to_words(625, "en") == "six hundred twenty-five"
    assert number_to_words(0.5, "hi") == "शून्य दशमलव पांच"
    assert number_to_words(1500, "hi") == "एक हज़ार पांच सौ"
    for lang in ("hi", "mr", "en"):
        assert not any(ch.isdigit() for ch in number_to_words(1234.5, lang))
