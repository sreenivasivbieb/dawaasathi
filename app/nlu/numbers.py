"""Spoken numbers -> digits for Hindi, Marathi, English and romanised Hindi.

parse_numbers(tokens) finds number spans in a token list:
  "छह सौ पच्चीस" -> 625, "ढाई सौ" -> 250, "सहाशे पंचवीस" -> 625, "six twenty five" -> 625,
  "पांच सौ एक सौ पच्चीस" -> 500, 125.
Also number_to_words() for the TTS side (Timbre wants numbers spoken as words).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_HI_1_99 = (
    "शून्य एक दो तीन चार पांच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
    "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस "
    "पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस "
    "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी "
    "अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()

_MR_1_99 = (
    "शून्य एक दोन तीन चार पाच सहा सात आठ नऊ दहा अकरा बारा तेरा चौदा पंधरा सोळा सतरा अठरा एकोणीस "
    "वीस एकवीस बावीस तेवीस चोवीस पंचवीस सव्वीस सत्तावीस अठ्ठावीस एकोणतीस तीस एकतीस बत्तीस तेहतीस "
    "चौतीस पस्तीस छत्तीस सदतीस अडतीस एकोणचाळीस चाळीस एक्केचाळीस बेचाळीस त्रेचाळीस चव्वेचाळीस "
    "पंचेचाळीस सेहेचाळीस सत्तेचाळीस अठ्ठेचाळीस एकोणपन्नास पन्नास एक्कावन्न बावन्न त्रेपन्न चोपन्न "
    "पंचावन्न छप्पन्न सत्तावन्न अठ्ठावन्न एकोणसाठ साठ एकसष्ट बासष्ट त्रेसष्ट चौसष्ट पासष्ट सहासष्ट "
    "सदुसष्ट अडुसष्ट एकोणसत्तर सत्तर एक्काहत्तर बाहत्तर त्र्याहत्तर चौऱ्याहत्तर पंच्याहत्तर शहात्तर "
    "सत्याहत्तर अठ्ठ्याहत्तर एकोणऐंशी ऐंशी एक्क्याऐंशी ब्याऐंशी त्र्याऐंशी चौऱ्याऐंशी पंच्याऐंशी "
    "शहाऐंशी सत्त्याऐंशी अठ्ठ्याऐंशी एकोणनव्वद नव्वद एक्क्याण्णव ब्याण्णव त्र्याण्णव चौऱ्याण्णव "
    "पंच्याण्णव शहाण्णव सत्त्याण्णव अठ्ठ्याण्णव नव्व्याण्णव"
).split()

_EN_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90}

_HI_EXTRA = {
    "पाँच": 5, "छः": 6, "छ": 6, "छे": 6, "छै": 6, "जीरो": 0, "ज़ीरो": 0, "पन्द्रह": 15,
    "चवालीस": 44, "सैतालीस": 47, "सैंतीस": 37, "इकत्तीस": 31, "उन्चास": 49, "तिरेपन": 53,
    "चौव्वन": 54, "सत्तावन्": 57, "छियासठ": 66, "इकहत्तर": 71, "बहत्तर": 72, "उन्यासी": 79,
    "इक्यानबे": 91, "बानबे": 92, "तिरानबे": 93, "चौरानबे": 94, "पंचानबे": 95, "पचानबे": 95,
    "छियानबे": 96, "सत्तानबे": 97, "अट्ठानबे": 98, "निन्यानबे": 99, "नब्बे": 90,
    "अट्ठारह": 18, "उन्नीस": 19, "सत्तरह": 17, "छत्तीस": 36,
}
_ROMAN_HI = {
    "ek": 1, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "chah": 6, "chhah": 6, "saat": 7, "aath": 8, "nau": 9,
    "das": 10, "gyarah": 11, "barah": 12, "pandrah": 15, "bees": 20, "bis": 20,
    "pachchis": 25, "pachees": 25, "pachis": 25, "tees": 30, "chalis": 40, "chalees": 40,
    "pachas": 50, "pachaas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
}

_MULT = {
    "सौ": 100, "सो": 100, "शंभर": 100, "हजार": 1000, "हज़ार": 1000, "लाख": 100000,
    "hundred": 100, "thousand": 1000, "lakh": 100000, "sau": 100,
    "hazar": 1000, "hazaar": 1000, "hajar": 1000,
}
_FRACTION = {  # value, prefix-kind
    "साढ़े": 0.5, "साढे": 0.5, "sadhe": 0.5, "saade": 0.5, "साडे": 0.5,
    "सवा": 0.25, "sawa": 0.25, "पौने": -0.25, "paune": -0.25,
}
_STANDALONE = {"डेढ़": 1.5, "डेढ": 1.5, "dedh": 1.5, "ढाई": 2.5, "dhai": 2.5, "अडीच": 2.5, "दीड": 1.5}
_POINT = {"point", "दशमलव", "पॉइंट", "प्वाइंट", "पॉईंट", "dashamlav"}


def _nk(s: str) -> str:
    """Normalise a Devanagari key: drop nukta, chandrabindu -> anusvara, visarga dropped."""
    s = unicodedata.normalize("NFD", s)
    s = s.replace("़", "").replace("ँ", "ं").replace("ः", "")
    return unicodedata.normalize("NFC", s).lower()


_WORDS: dict[str, int] = {}
for _i, _w in enumerate(_HI_1_99):
    _WORDS[_nk(_w)] = _i
for _i, _w in enumerate(_MR_1_99):
    _WORDS.setdefault(_nk(_w), _i)
for _w, _v in _HI_EXTRA.items():
    _WORDS.setdefault(_nk(_w), _v)
for _i, _w in enumerate(_EN_UNITS):
    _WORDS[_w] = _i
_WORDS.update(_EN_TENS)
for _w, _v in _ROMAN_HI.items():
    _WORDS.setdefault(_w, _v)
_MULT_N = {_nk(k): v for k, v in _MULT.items()}
_FRACTION_N = {_nk(k): v for k, v in _FRACTION.items()}
_STANDALONE_N = {_nk(k): v for k, v in _STANDALONE.items()}
_POINT_N = {_nk(k) for k in _POINT}
_MR_SHE = _nk("शे")

_DIGIT_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


@dataclass
class NumberSpan:
    start: int
    end: int  # exclusive
    value: float


def _word_value(tok: str) -> tuple[str, float] | None:
    t = _nk(tok.translate(_DEV_DIGITS))
    if _DIGIT_RE.match(t):
        return ("dig", float(t))
    if t in _WORDS:
        return ("num", float(_WORDS[t]))
    if t in _MULT_N:
        return ("mult", float(_MULT_N[t]))
    if t in _FRACTION_N:
        return ("frac", _FRACTION_N[t])
    if t in _STANDALONE_N:
        return ("num", _STANDALONE_N[t])
    if t in _POINT_N:
        return ("point", 0.0)
    if t.endswith(_MR_SHE) and t[: -len(_MR_SHE)] in _WORDS:  # Marathi "सहाशे" = 600
        return ("hund", float(_WORDS[t[: -len(_MR_SHE)]] * 100))
    if t.endswith("sau") and t[:-3] in _WORDS:
        return ("hund", float(_WORDS[t[:-3]] * 100))
    if "-" in t:  # English compounds as ASR / number_to_words write them: "twenty-five"
        tens, _, unit = t.partition("-")
        if tens in _EN_TENS and unit in _EN_UNITS[1:10]:
            return ("num", float(_EN_TENS[tens] + _EN_UNITS.index(unit)))
    return None


def is_number_token(tok: str) -> bool:
    return _word_value(tok) is not None


def parse_number(tokens: list[str]) -> float | None:
    spans = parse_numbers(tokens)
    return spans[0].value if spans else None


def parse_numbers(tokens: list[str]) -> list[NumberSpan]:
    spans: list[NumberSpan] = []
    i, n = 0, len(tokens)
    while i < n:
        if _word_value(tokens[i]) is None:
            i += 1
            continue
        j = i
        run: list[tuple[str, float]] = []
        while j < n:
            wv = _word_value(tokens[j])
            if wv is None:
                break
            run.append(wv)
            j += 1
        for start_off, end_off, value in _evaluate(run):
            spans.append(NumberSpan(i + start_off, i + end_off, value))
        i = j
    return spans


def _evaluate(run: list[tuple[str, float]]) -> list[tuple[int, int, float]]:
    """Evaluate a run of number-ish tokens, splitting it into separate numbers where needed."""
    out: list[tuple[int, int, float]] = []
    st = {"total": 0.0, "small": 0.0, "frac": 0.0, "has": False, "dec": None, "start": 0}
    last = None  # kind of the previous token: num | combo | digit | mult | frac | point | dec

    def emit(end: int, value: float | None = None) -> None:
        if st["has"]:
            v = st["total"] + st["small"] if value is None else value
            if st["dec"]:
                v += float("0." + "".join(str(int(d)) for d in st["dec"]))
            out.append((st["start"], end, v))
        st.update(total=0.0, small=0.0, frac=0.0, has=False, dec=None)

    for k, (kind, val) in enumerate(run):
        if st["dec"] is not None:
            if kind in ("num", "dig") and val < 10 and float(val).is_integer():
                st["dec"].append(val)
                last = "dec"
                continue
            emit(k)
            st["start"] = k
        if kind == "point":
            if st["has"]:
                st["dec"] = []
            last = "point"
            continue
        if kind == "frac":
            if st["has"] and last in ("num", "combo", "digit"):
                emit(k)
                st["start"] = k
            st["frac"] = val
            last = "frac"
            continue
        if kind in ("num", "dig"):
            small = st["small"]
            if st["has"] and last in ("num", "combo", "digit"):
                is_tens = small >= 20 and small % 10 == 0 and 20 <= small % 100 <= 90
                if kind == "num" and last != "digit" and is_tens and 1 <= val <= 9:
                    st["small"] = small + val  # "twenty five", "six twenty five"
                    last = "combo"
                    continue
                if (kind == "num" and last == "num" and st["total"] == 0 and 1 <= small <= 9
                        and float(small).is_integer() and 10 <= val <= 99):
                    st["small"] = small * 100 + val  # colloquial "छह पच्चीस" -> 625
                    last = "combo"
                    continue
                emit(k)
                st["start"] = k
            elif last == "mult" and st["has"] and val >= 100:
                emit(k)
                st["start"] = k
            if st["frac"]:
                st["small"] = val + st["frac"]
                st["frac"] = 0.0
            else:
                st["small"] += val
            st["has"] = True
            last = "digit" if kind == "dig" else "num"
            continue
        if kind == "hund":
            if st["has"] and (last in ("num", "combo", "digit") or st["total"] % 1000 >= 100):
                emit(k)
                st["start"] = k
            st["total"] += val
            st["small"] = 0.0
            st["has"] = True
            last = "mult"
            continue
        if kind == "mult":
            small = st["small"]
            base = small if small else (1.0 + st["frac"] if st["frac"] else 1.0)
            st["frac"] = 0.0
            if val == 100 and st["total"] % 1000 >= 100:
                # "पांच सौ | एक सौ पच्चीस": a second hundreds group starts a new number.
                split_at = k - 1 if small else k
                emit(split_at, st["total"])
                st["start"] = split_at
            if val == 100:
                st["total"] += base * 100
            elif st["total"] < val:
                st["total"] = (st["total"] + base) * val
            else:
                st["total"] += base * val
            st["small"] = 0.0
            st["has"] = True
            last = "mult"
            continue
    emit(len(run))
    return out


# ---------------------------------------------------------------- number -> words


def number_to_words(value: float, lang: str) -> str:
    if lang == "en":
        return _en_words(value)
    if lang not in ("hi", "mr"):
        # Other Indian languages: Timbre reads digits in the voice's language; keep them as digits.
        return str(int(value)) if float(value).is_integer() else f"{value:g}"
    table = _MR_1_99 if lang == "mr" else _HI_1_99
    hundred = "शे" if lang == "mr" else " सौ"
    thousand = "हजार" if lang == "mr" else "हज़ार"
    point = "पॉइंट" if lang == "mr" else "दशमलव"
    if not float(value).is_integer():
        whole, _, dec = f"{value:.2f}".rstrip("0").partition(".")
        head = _indic_int(int(whole), table, hundred, thousand)
        return f"{head} {point} " + " ".join(table[int(d)] for d in dec)
    return _indic_int(int(value), table, hundred, thousand)


def _indic_int(n: int, table: list[str], hundred: str, thousand: str) -> str:
    if n < 100:
        return table[n]
    parts = []
    if n >= 100000:
        parts.append(f"{_indic_int(n // 100000, table, hundred, thousand)} लाख")
        n %= 100000
    if n >= 1000:
        parts.append(f"{_indic_int(n // 1000, table, hundred, thousand)} {thousand}")
        n %= 1000
    if n >= 100:
        parts.append(f"{table[n // 100]}{hundred}")
        n %= 100
    if n:
        parts.append(table[n])
    return " ".join(parts)


_EN_TENS_LIST = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _en_words(value: float) -> str:
    if not float(value).is_integer():
        whole, _, dec = f"{value:.2f}".rstrip("0").partition(".")
        return f"{_en_int(int(whole))} point " + " ".join(_EN_UNITS[int(d)] for d in dec)
    return _en_int(int(value))


def _en_int(n: int) -> str:
    if n < 20:
        return _EN_UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _EN_TENS_LIST[tens] + (f"-{_EN_UNITS[unit]}" if unit else "")
    if n < 1000:
        h, rest = divmod(n, 100)
        return f"{_EN_UNITS[h]} hundred" + (f" {_en_int(rest)}" if rest else "")
    if n < 100000:
        t, rest = divmod(n, 1000)
        return f"{_en_int(t)} thousand" + (f" {_en_int(rest)}" if rest else "")
    lk, rest = divmod(n, 100000)
    return f"{_en_int(lk)} lakh" + (f" {_en_int(rest)}" if rest else "")
