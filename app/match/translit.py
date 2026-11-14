"""Indic script -> Roman transliteration tuned for spoken drug names, plus phonetic keys.

Devanagari is converted with Hindi schwa deletion ("ऑगमेंटिन" -> "ogmentin"). Other Indic
scripts are first mapped to Devanagari with indic_transliteration, then handled the same way.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import jellyfish

try:
    from metaphone import doublemetaphone
except ImportError:  # pragma: no cover
    doublemetaphone = None

try:
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate as _sanscript
except ImportError:  # pragma: no cover
    sanscript = None
    _sanscript = None

_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e",
    "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e", "ऎ": "e", "ऒ": "o", "ॠ": "ri",
}
_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai",
    "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e", "ॆ": "e", "ॊ": "o", "ॄ": "ri",
}
_CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j",
    "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t",
    "थ": "th", "द": "d", "ध": "dh", "न": "n", "ऩ": "n", "प": "p", "फ": "ph", "ब": "b",
    "भ": "bh", "म": "m", "य": "y", "र": "r", "ऱ": "r", "ल": "l", "ळ": "l", "ऴ": "l",
    "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}
_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_PRECOMPOSED_NUKTA = {
    "क़": "क", "ख़": "ख", "ग़": "ग", "ज़": "ज", "ड़": "ड", "ढ़": "ढ", "फ़": "फ", "य़": "य",
}
_HALANT = "्"
_NUKTA_SIGN = "़"
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_SCRIPT_RANGES = [
    ((0x0900, 0x097F), "devanagari"),
    ((0x0980, 0x09FF), "bengali"),
    ((0x0A00, 0x0A7F), "gurmukhi"),
    ((0x0A80, 0x0AFF), "gujarati"),
    ((0x0B00, 0x0B7F), "oriya"),
    ((0x0B80, 0x0BFF), "tamil"),
    ((0x0C00, 0x0C7F), "telugu"),
    ((0x0C80, 0x0CFF), "kannada"),
    ((0x0D00, 0x0D7F), "malayalam"),
]


def script_of(text: str) -> str:
    for ch in text:
        cp = ord(ch)
        for (lo, hi), name in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                return name
    return "latin"


def _to_devanagari(text: str, script: str) -> str:
    if script == "devanagari" or _sanscript is None:
        return text
    scheme = getattr(sanscript, script.upper(), None)
    if scheme is None:
        return text
    try:
        return _sanscript(text, scheme, sanscript.DEVANAGARI)
    except Exception:  # pragma: no cover - library edge cases
        return text


def _dev_word_to_roman(word: str) -> str:
    for pre, base in _PRECOMPOSED_NUKTA.items():
        word = word.replace(pre, base + _NUKTA_SIGN)
    units: list[list[str]] = []  # [kind, text]; kind: C consonant, V vowel, A inherent schwa, N nasal, X other
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        if ch in _CONS:
            roman = _CONS[ch]
            if i + 1 < n and word[i + 1] == _NUKTA_SIGN:
                roman = _NUKTA.get(ch, roman)
                i += 1
            units.append(["C", roman])
            nxt = word[i + 1] if i + 1 < n else ""
            if nxt in _MATRAS:
                units.append(["V", _MATRAS[nxt]])
                i += 2
                continue
            if nxt == _HALANT:
                i += 2
                continue
            units.append(["A", "a"])
            i += 1
            continue
        if ch in _VOWELS:
            units.append(["V", _VOWELS[ch]])
        elif ch in ("ं", "ँ"):
            units.append(["N", "n"])
        elif ch == "ः":
            units.append(["C", "h"])
        elif ch == _NUKTA_SIGN:
            pass
        else:
            units.append(["X", ch.translate(_DEV_DIGITS)])
        i += 1

    vowel_count = sum(1 for k, _ in units if k in ("V", "A"))

    def is_vowel(idx: int) -> bool:
        return 0 <= idx < len(units) and units[idx][0] in ("V", "A")

    # Word-final schwa deletion (keep it for one-syllable words).
    for j in range(len(units) - 1, -1, -1):
        if units[j][0] == "A" and j == len(units) - 1 and vowel_count > 1:
            units[j][0] = "D"
        break
    # Medial schwa deletion: V C [a] C V -> drop the a.
    j = 0
    while j < len(units):
        if units[j][0] == "A" and j >= 2 and j + 2 < len(units):
            if units[j - 1][0] == "C" and is_vowel(j - 2) and units[j + 1][0] == "C" and (
                is_vowel(j + 2) or units[j + 2][0] == "N"
            ):
                units[j][0] = "D"
                j += 2
                continue
        j += 1
    return "".join(text for kind, text in units if kind != "D")


@lru_cache(maxsize=20000)
def to_roman(text: str) -> str:
    """Transliterate any Indic text to lowercase Roman; Latin text passes through."""
    text = unicodedata.normalize("NFC", text)
    out_words = []
    for word in text.split():
        script = script_of(word)
        if script == "latin":
            out_words.append(word.lower())
            continue
        dev = _to_devanagari(word, script)
        out_words.append(_dev_word_to_roman(dev).lower())
    return " ".join(out_words)


_NORM_RULES = [
    (r"[^a-z0-9 ]+", " "),
    (r"ph", "f"),
    (r"ck", "k"),
    (r"q", "k"),
    (r"x", "ks"),
    (r"z", "j"),
    (r"w", "v"),
    (r"chh", "ch"),
    (r"c(?=[eiy])", "s"),
    (r"c(?!h)", "k"),
    (r"kh", "k"),
    (r"gh", "g"),
    (r"jh", "j"),
    (r"th", "t"),
    (r"dh", "d"),
    (r"bh", "b"),
    (r"sh", "s"),
    (r"aa+", "a"),
    (r"ee+", "i"),
    (r"oo+", "u"),
    (r"ai", "e"),
    (r"au", "o"),
    (r"ou", "o"),
    (r"ei", "i"),
    (r"ie", "i"),
    (r"(?<=[^aeiou ])y(?=[^aeiou ]|$)", "i"),
    (r"y\b", "i"),
]
_NORM_COMPILED = [(re.compile(p), r) for p, r in _NORM_RULES]
_DOUBLE = re.compile(r"([a-z])\1+")


@lru_cache(maxsize=50000)
def normalize(text: str) -> str:
    """Collapse spelling variants so 'Augmentin', 'ogmentin' and 'augmenten' land close."""
    s = to_roman(text).lower()
    for pattern, repl in _NORM_COMPILED:
        s = pattern.sub(repl, s)
    s = _DOUBLE.sub(r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def skeleton(norm: str) -> str:
    s = re.sub(r"[aeiouhy ]", "", norm)
    return _DOUBLE.sub(r"\1", s)


def dm_keys(norm: str) -> tuple[str, ...]:
    word = norm.replace(" ", "")
    if not word:
        return ()
    if doublemetaphone is not None:
        keys = tuple(k for k in doublemetaphone(word) if k)
        if keys:
            return keys
    return (jellyfish.metaphone(word),)


def soundex(norm: str) -> str:
    word = norm.replace(" ", "")
    return jellyfish.soundex(word) if word else ""


def trigrams(norm: str) -> set[str]:
    s = f"  {norm.replace(' ', '_')} "
    return {s[i : i + 3] for i in range(len(s) - 2)}
