"""Zero-hallucination guard for spoken scripts.

Extracts every number and every known salt name from the final script; all of them must be
present in the facts (verdict JSON, price, alternatives, safety, directions). Otherwise the
caller falls back to the plain, slot-free template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.engine.salts import all_salt_surface_forms

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class GuardResult:
    ok: bool
    bad_numbers: list[str] = field(default_factory=list)
    bad_salts: list[str] = field(default_factory=list)


def _walk(value: Any, nums: set[float], strings: list[str]) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        nums.add(round(float(value), 4))
        return
    if isinstance(value, str):
        strings.append(value)
        for m in _NUM_RE.findall(value):
            nums.add(round(float(m), 4))
        return
    if isinstance(value, dict):
        for v in value.values():
            _walk(v, nums, strings)
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            _walk(v, nums, strings)
        return
    if hasattr(value, "model_dump"):
        _walk(value.model_dump(), nums, strings)


def allowed_facts(*sources: Any) -> tuple[set[float], set[str]]:
    nums: set[float] = set()
    strings: list[str] = []
    for src in sources:
        _walk(src, nums, strings)
    blob = " ".join(strings).lower()
    salts = {canon for form, canon in all_salt_surface_forms().items() if _contains(blob, form)}
    return nums, salts


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) is not None


def validate(script: str, *sources: Any) -> GuardResult:
    nums, salts = allowed_facts(*sources)
    bad_nums = []
    for m in _NUM_RE.findall(script):
        if round(float(m), 4) not in nums:
            bad_nums.append(m)
    low = script.lower()
    bad_salts = sorted({canon for form, canon in all_salt_surface_forms().items()
                        if _contains(low, form) and canon not in salts})
    return GuardResult(ok=not bad_nums and not bad_salts, bad_numbers=bad_nums, bad_salts=bad_salts)
