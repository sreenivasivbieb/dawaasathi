"""Salts and strengths from raw OCR text of a pack (offline OCR returns lines, not structure).

Closed set: only names in the brand DB's salt vocabulary (~1,800 salts) count as ingredients, so
OCR noise such as "Epip" or "Karthofl" can never become a salt that is spoken back. A strength is
attached only when the label pairs it unambiguously with its salt:
  * same printed row:             "Glibenclamide I.P. 5 mg"
  * one strength list per salt:   "Glibenclamide & Metformin ..." + "5mg+500mg"
otherwise the salt is kept without a strength (the engine then says the strength is unknown).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from rapidfuzz import fuzz, process

from app.engine.salts import _DOSE_RE, _SALT_SUFFIXES, _unit, canonical_salt
from app.models import SaltDose

SUPPLEMENT_RE = re.compile(
    r"dietary\s*supp|food\s*supp|nutraceutical|health\s*supp|nutritional\s*supp|herbal\s*supp|\bfssai\b", re.I)

# Words printed on every pack that must never fuzzy-match a salt.
_STOP = {
    "tablet", "tablets", "capsule", "capsules", "composition", "contains", "excipients", "sustained",
    "release", "coated", "uncoated", "colour", "color", "dosage", "physician", "prescribed", "children",
    "temperature", "exceeding", "manufactured", "marketed", "license", "licence", "schedule", "warning",
    "formula", "supplement", "dietary", "store", "protect", "moisture", "light", "medicine", "contains",
    "bilayered", "hydrochloride", "pharma", "limited", "healthcare", "private", "india",
}
_JUNK = {w for w in _SALT_SUFFIXES if w not in ("calcium", "magnesium", "sodium", "potassium")}
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]*")
_DOSE_LIST = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|iu)\s*(?:\+|/|&|,|and)\s*(\d+(?:\.\d+)?)\s*(mg|mcg|g|iu)"
    r"(?:\s*(?:\+|/|&|,|and)\s*(\d+(?:\.\d+)?)\s*(mg|mcg|g|iu))?", re.I)


def _strip_suffixes(phrase: str) -> str:
    """Drop trailing salt-form words only ("metformin hydrochloride ip" -> "metformin"); unlike
    canonical_salt it never jumps to a later word, so two salts in one phrase stay two."""
    words = phrase.split()
    while len(words) > 1 and words[-1] in _SALT_SUFFIXES:
        words.pop()
    return " ".join(words)


class SaltVocab:
    def __init__(self, names: Iterable[str], synonyms: dict[str, str] | None = None):
        # Bad source rows give "salts" such as "c" or "hydrochloride": never ingredients on their own.
        self.forms: dict[str, str] = {n: n for n in names if n and len(n) >= 3 and n not in _JUNK}
        for surface, canon in (synonyms or {}).items():
            self.forms.setdefault(surface, canon)
        self._fuzzy = [f for f in self.forms if len(f) >= 6]
        self.max_words = max((f.count(" ") + 1 for f in self.forms), default=1)

    def __contains__(self, name: str) -> bool:
        return name in self.forms

    def canon(self, name: str) -> str | None:
        """Known salt for an OCR'd name ('Gibenclamide I.P.' -> glibenclamide), else None."""
        n = canonical_salt(name)
        if n in self.forms:
            return self.forms[n]
        return self._fuzzy_one(n)

    def _fuzzy_one(self, phrase: str) -> str | None:
        if len(phrase) < 6 or phrase in _STOP:
            return None
        hit = process.extractOne(phrase, self._fuzzy, scorer=fuzz.ratio, score_cutoff=88)
        return self.forms[hit[0]] if hit else None

    def find(self, line: str) -> list[tuple[int, int, str]]:
        """Salt mentions in one line: (start, end, canonical), left to right."""
        words = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(line)]
        out, i = [], 0
        while i < len(words):
            hit = None
            for n in range(min(self.max_words, 4, len(words) - i), 0, -1):
                phrase = " ".join(w for _, _, w in words[i:i + n])
                if phrase in _STOP:
                    continue
                # "Metformin Hydrochloride I.P." -> metformin (one salt, suffix words consumed)
                canon = self.forms.get(phrase) or (self.forms.get(_strip_suffixes(phrase)) if n > 1 else None)
                if canon is None and n <= 2:
                    canon = self._fuzzy_one(phrase)
                if canon is not None:
                    hit = (words[i][0], words[i + n - 1][1], canon, n)
                    break
            if hit:
                out.append(hit[:3])
                i += hit[3]
            else:
                i += 1
        return out


def extract_salts(lines: list[str], vocab: SaltVocab) -> list[SaltDose]:
    """Salts (with strengths where unambiguous) from OCR lines in reading order."""
    order: list[str] = []
    strengths: dict[str, Counter] = {}
    for line in lines:
        mentions = vocab.find(line)
        for k, (start, end, canon) in enumerate(mentions):
            if canon not in order:
                order.append(canon)
            # Dose on the same row, before the next salt mention.
            stop = mentions[k + 1][0] if k + 1 < len(mentions) else len(line)
            m = _DOSE_RE.search(line, end, stop)
            if m and m.group("unit").lower() not in ("%", "ml"):
                strengths.setdefault(canon, Counter())[(float(m.group("amt")), _unit(m.group("unit")))] += 1
    if not order:
        return []
    missing = [s for s in order if s not in strengths]
    if missing and len(order) >= 2:
        # "Glibenclamide & Metformin Hydrochloride" ... "5mg+500mg": one strength per salt, in order.
        for line in lines:
            m = _DOSE_LIST.search(line)
            if not m:
                continue
            doses = [(float(m.group(i)), _unit(m.group(i + 1))) for i in (1, 3, 5) if m.group(i)]
            if len(doses) == len(order):
                for canon, dose in zip(order, doses):
                    strengths.setdefault(canon, Counter())[dose] += 1
                break
    out = []
    for canon in order:
        c = strengths.get(canon)
        if c:
            (amt, unit), _ = c.most_common(1)[0]
            out.append(SaltDose(name=canon, amount=amt, unit=unit))
        else:
            out.append(SaltDose(name=canon))
    return out


def is_supplement(text: str | None) -> bool:
    return bool(text and SUPPLEMENT_RE.search(text))
