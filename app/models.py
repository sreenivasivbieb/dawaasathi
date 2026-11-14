"""Shared pydantic models: drug items, verdicts and the /v1/query response."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Intent = Literal["FIND_GENERIC", "PRICE_CHECK", "DOSAGE_QUERY", "VERIFY_MATCH", "USE_QUERY", "CLARIFY"]
Status = Literal["ok", "needs_confirmation", "needs_image", "unverified", "error"]
VerdictCode = Literal[
    "BANNED_COMBINATION",
    "MATCH",
    "GENERIC_EQUIVALENT",
    "SALT_MISMATCH",
    "STRENGTH_MISMATCH",
    "RELEASE_MISMATCH",
    "FORM_MISMATCH",
    "CONCENTRATION_MISMATCH",
    "UNVERIFIED",
]

LIQUID_FORMS = {"oral_liquid", "drops"}


class SaltDose(BaseModel):
    """One active ingredient. For liquids, amount is per `per_ml` millilitres."""

    name: str
    amount: float | None = None
    unit: str | None = None  # mg | mcg | g | iu | %
    per_ml: float | None = None

    def amount_mg(self) -> float | None:
        if self.amount is None or self.unit is None:
            return None
        unit = self.unit.lower()
        if unit == "mg":
            return self.amount
        if unit == "g":
            return self.amount * 1000
        if unit in ("mcg", "µg", "ug"):
            return self.amount / 1000
        return None  # IU, % etc. are compared as-is

    def comparable(self) -> tuple[float, str] | None:
        """Strength in a canonical unit for comparison (mg, or the original unit)."""
        if self.amount is None or self.unit is None:
            return None
        mg = self.amount_mg()
        if mg is not None:
            return (round(mg, 4), "mg")
        return (round(self.amount, 4), self.unit.lower())

    def concentration(self) -> tuple[float, str] | None:
        """Strength per millilitre, for liquids."""
        base = self.comparable()
        if base is None or not self.per_ml:
            return None
        return (round(base[0] / self.per_ml, 4), f"{base[1]}/ml")

    def label(self) -> str:
        if self.amount is None:
            return self.name.title()
        amt = _fmt_num(self.amount)
        unit = self.unit or ""
        if self.per_ml:
            return f"{self.name.title()} {amt} {unit}/{_fmt_num(self.per_ml)} ml".strip()
        return f"{self.name.title()} {amt} {unit}".strip()


def _fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


class DrugItem(BaseModel):
    source: Literal["voice", "rx", "strip", "db"] = "db"
    brand_id: int | None = None
    brand: str | None = None
    salts: list[SaltDose] = Field(default_factory=list)
    form: str | None = None
    release: str | None = None
    mrp: float | None = None
    pack_qty: float | None = None
    pack_unit: str | None = None
    scheduled: bool | None = None
    manufacturer: str | None = None
    batch_no: str | None = None
    directions: str | None = None
    raw_text: str | None = None
    confidence: float | None = None
    composition_uncertain: bool = False  # low-confidence OCR: salt list may be incomplete

    def salt_names(self) -> list[str]:
        return sorted({s.name for s in self.salts})

    def salt_labels(self) -> list[str]:
        return [s.label() for s in sorted(self.salts, key=lambda s: s.name)]

    def unit_price(self) -> float | None:
        if self.mrp is None or not self.pack_qty:
            return None
        return self.mrp / self.pack_qty


class Flag(BaseModel):
    code: str
    detail: str = ""
    facts: dict[str, Any] = Field(default_factory=dict)


class SafetyResult(BaseModel):
    banned: bool | None  # None = could not verify
    status: Literal["banned", "not_listed", "unverified", "possible"]
    list_as_of: str | None = None
    source: str | None = None
    notification_ref: str | None = None
    notification_date: str | None = None
    matched_salts: list[str] = Field(default_factory=list)
    reason: str | None = None


class Verdict(BaseModel):
    code: VerdictCode
    mode: Literal["lookup", "compare"]
    facts: dict[str, Any] = Field(default_factory=dict)
    flags: list[Flag] = Field(default_factory=list)


class PriceInfo(BaseModel):
    mrp: float | None = None
    pack_qty: float | None = None
    reference: float | None = None
    reference_pack_qty: float | None = None
    reference_name: str | None = None
    source: str | None = None
    ratio: float | None = None
    as_of: str | None = None
    nppa_ceiling_unit: float | None = None
    nppa_as_of: str | None = None
    above_ceiling: bool | None = None
    stale: bool = False


class Alternative(BaseModel):
    name: str
    source: str
    price: float
    pack_qty: float | None = None
    unit_price: float | None = None
    saves_pct: int | None = None
    brand_id: int | None = None
    as_of: str | None = None
    is_jan_aushadhi: bool = False


class Candidate(BaseModel):
    candidate_id: str
    brand_id: int | None = None
    brand: str
    label: str
    confidence: float
    source: str = "voice"


class Script(BaseModel):
    lang: str
    text: str
    chemist_text: str
    guard_ok: bool = True
    speech_text: str = ""


class Card(BaseModel):
    color: Literal["green", "amber", "red"]
    icon: Literal["check", "cross", "question"]
    title: str = ""


class QueryResponse(BaseModel):
    session_id: str
    status: Status
    transcript: str | None = None
    lang: str
    intent: Intent | None = None
    mode: Literal["lookup", "compare"] | None = None
    match: dict[str, Any] | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    safety: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    price: dict[str, Any] | None = None
    alternatives: list[Alternative] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    items: dict[str, Any] = Field(default_factory=dict)
    script: Script | None = None
    audio_url: str | None = None
    audio_stream_url: str | None = None
    audio_id: str | None = None
    tts: Literal["timbre", "mock", "none"] = "none"
    card: Card | None = None
    timings_ms: dict[str, float | None] = Field(default_factory=dict)
    data_sources: dict[str, Any] = Field(default_factory=dict)
