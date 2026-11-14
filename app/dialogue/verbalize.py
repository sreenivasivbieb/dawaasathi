"""Display script -> speakable text (Timbre wants numbers, units and money as words)."""

from __future__ import annotations

import re

from app.dialogue.templates import load, template_lang
from app.nlu.numbers import number_to_words

_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|ml|g|iu)\b(?:\s*/\s*(\d+(?:\.\d+)?)?\s*ml\b)?", re.I)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_CODE_DIGITS_RE = re.compile(r"(?<=[A-Za-z])\d+")


def speakable(text: str, lang: str) -> str:
    lang = template_lang(lang)
    w = load(lang)["words"]
    rupee_words = [w["rupees"], "रुपयांत", "रुपयांचे", "रुपये"]

    def money(m: re.Match) -> str:
        value = float(m.group(1))
        rupees = int(value)
        paise = int(round((value - rupees) * 100))
        out = f"{number_to_words(rupees, lang)} {m.group(2)}"
        if paise:
            out += f" {number_to_words(paise, lang)} {w['paise']}"
        return out

    for word in dict.fromkeys(rupee_words):
        text = re.sub(rf"(\d+(?:\.\d+)?)\s*({re.escape(word)})", money, text)

    def unit(m: re.Match) -> str:
        amount = number_to_words(float(m.group(1)), lang)
        u = w.get(m.group(2).lower(), m.group(2))
        out = f"{amount} {u}"
        if m.group(0).count("/"):
            per = m.group(3)
            per_words = number_to_words(float(per), lang) + " " if per else ""
            out += f" {w['per']} {per_words}{w['ml']}"
        return out

    text = _UNIT_RE.sub(unit, text)
    # Codes such as batch "DEMO123": digits glued to Latin letters are split off and, from three
    # digits on, read one by one ("D3" -> "D three", "DEMO123" -> "DEMO one two three").
    def code(m: re.Match) -> str:
        d = m.group(0)
        spoken = [number_to_words(int(c), lang) for c in d] if len(d) >= 3 else [number_to_words(int(d), lang)]
        return " " + " ".join(spoken) + " "

    text = _CODE_DIGITS_RE.sub(code, text)
    text = text.replace(" + ", f" {w['and']} ")
    text = text.replace("%", f" {w['percent']}")
    text = _NUM_RE.sub(lambda m: number_to_words(float(m.group(0)), lang), text)
    return re.sub(r"\s+", " ", text).strip()
