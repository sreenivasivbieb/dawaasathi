"""Per-language slot templates -> reply script, Speak-to-Chemist script and TTS segments.

Every fact in a script comes from the verdict / price / alternatives objects; the guard
(dialogue/guard.py) re-checks that before anything is spoken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import TEMPLATE_LANGS
from app.engine.salts import FORM_LABELS, RELEASE_LABELS
from app.models import Alternative, Candidate, DrugItem, Flag, PriceInfo, SafetyResult, Verdict

_DIR = Path(__file__).parent / "templates"
MAX_WORDS = 45
WARNING_FLAG_CODES = {"NSQ_BATCH", "POSSIBLE_BANNED", "DUPLICATE_SALT", "SYMPTOM_MISMATCH", "LOOK_ALIKE"}
MISMATCH_CODES = {"SALT_MISMATCH", "STRENGTH_MISMATCH", "RELEASE_MISMATCH", "FORM_MISMATCH",
                  "CONCENTRATION_MISMATCH"}


@lru_cache(maxsize=8)
def load(lang: str) -> dict:
    lang = lang if lang in TEMPLATE_LANGS else "en"
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def template_lang(lang: str) -> str:
    return lang if lang in TEMPLATE_LANGS else "en"


@dataclass
class Segment:
    text: str
    kind: str  # fixed | dynamic


@dataclass
class RenderContext:
    lang: str
    status: str
    intent: str | None = None
    verdict: Verdict | None = None
    item: DrugItem | None = None
    prescribed: DrugItem | None = None
    dispensed: DrugItem | None = None
    safety: SafetyResult | None = None
    price: PriceInfo | None = None
    alternatives: list[Alternative] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    rx_count: int = 0
    directions: str | None = None
    error_kind: str | None = None
    needs_image_kind: str | None = None
    skip_detail: bool = False


@dataclass
class Rendered:
    segments: list[Segment]
    text: str
    chemist_text: str
    plain_segments: list[Segment]


def fmt_num(x: float | None) -> str:
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def fmt_money(x: float | None) -> str:
    if x is None:
        return ""
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.2f}"


def fmt_date(value: str | None, lang: str) -> str:
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return value
    months = load(lang)["words"]["months"]
    return f"{d.day} {months[d.month - 1]} {d.year}"


_FORM_SUFFIX = re.compile(r"\s+(tablets?|capsules?|oral suspension|suspension|syrup|drops|injection|cream|gel|"
                          r"ointment)$", re.I)


_ALL_CAPS = re.compile(r"\b[A-Z]{3,}\b")
_KEEP_CAPS = {"SR", "ER", "XR", "CR", "MR", "DR", "OD", "DS", "DSR", "LS", "MD", "CV", "XL", "IV", "HCL", "TR"}


def brand_display(name: str | None, keep_form: bool = False) -> str:
    """'PAN 40 Tablet' -> 'Pan 40': all-caps brand words in the A-Z list would be spelled out by TTS.
    keep_form keeps a non-tablet form word ('Crocin Drops'), for choices between SKUs."""
    if not name:
        return ""
    name = _ALL_CAPS.sub(lambda m: m.group(0) if m.group(0) in _KEEP_CAPS else m.group(0).title(), name.strip())
    if keep_form:
        return re.sub(r"\s+(tablets?|capsules?)$", "", name, flags=re.I)
    return _FORM_SUFFIX.sub("", name)


def salts_display(item: DrugItem | None) -> str:
    if item is None or not item.salts:
        return ""
    return " + ".join(item.salt_labels())


def salt_names_display(item: DrugItem | None) -> str:
    if item is None or not item.salts:
        return ""
    return " + ".join(n.title() for n in item.salt_names())


def form_label(form: str | None, lang: str) -> str:
    if not form:
        return ""
    return FORM_LABELS.get(form, {}).get(template_lang(lang), form)


def release_label(rel: str | None, lang: str) -> str:
    if not rel:
        return ""
    return RELEASE_LABELS.get(rel, {}).get(template_lang(lang), rel)


def _unit_word(form: str | None, lang: str, one: bool = False) -> str:
    w = load(lang)["words"]
    if one:
        return w.get("one_" + {"tablet": "tablet", "capsule": "capsule"}.get(form or "", "ml"
                      if form in ("oral_liquid", "drops") else "other"), "")
    if form == "tablet":
        return w["unit_tablet"]
    if form == "capsule":
        return w["unit_capsule"]
    if form in ("oral_liquid", "drops"):
        return w["unit_ml"]
    return w["unit_other"]


def _fill(template: str, slots: dict[str, Any]) -> str:
    class _Safe(dict):
        def __missing__(self, key):  # unknown slot -> empty string
            return ""

    return template.format_map(_Safe({k: ("" if v is None else v) for k, v in slots.items()}))


def render(ctx: RenderContext) -> Rendered:
    lang = template_lang(ctx.lang)
    t = load(lang)
    lines, verdicts, plain, clauses, chemist = (
        t["lines"], t["verdict"], t["verdict_plain"], t["clauses"], t["chemist"])
    doctor = Segment(lines["doctor"], "fixed")
    segs: list[Segment] = []
    plain_segs: list[Segment] = []
    chemist_text = ""

    item = ctx.item
    slots: dict[str, Any] = {
        "brand": (brand_display(item.brand) if item and item.brand else salts_display(item)),
        "salts": salts_display(item),
        "salt_names": salt_names_display(item),
        "form": form_label(item.form if item else None, lang),
    }

    if ctx.status == "error":
        key = "error_stt" if ctx.error_kind == "stt" else "error_generic"
        segs = [Segment(lines[key], "fixed")]
        return Rendered(segs + [doctor], _join(segs + [doctor]), "", segs + [doctor])

    if ctx.status == "needs_image":
        key = "needs_image_verify" if ctx.needs_image_kind == "verify" else "needs_image"
        segs = [Segment(lines[key], "fixed")]
        return Rendered(segs + [doctor], _join(segs + [doctor]), "", segs + [doctor])

    if ctx.status == "needs_confirmation":
        if ctx.needs_image_kind == "none_of_these":
            segs = [Segment(lines["confirm_none"], "fixed")]
        elif ctx.rx_count > 1 and not ctx.candidates[:1]:
            segs = [Segment(_fill(lines["confirm_rx"], {"n": ctx.rx_count}), "dynamic")]
        elif len(ctx.candidates) >= 2:
            segs = [Segment(_fill(lines["confirm_two"], {"cand1": ctx.candidates[0].label,
                                                         "cand2": ctx.candidates[1].label}), "dynamic")]
        elif ctx.candidates:
            segs = [Segment(_fill(lines["confirm_one"], {"cand1": ctx.candidates[0].label}), "dynamic")]
        else:
            segs = [Segment(lines["needs_image"], "fixed")]
        out = segs + [doctor]
        return Rendered(out, _join(out), "", [Segment(lines["needs_image"], "fixed"), doctor])

    v = ctx.verdict
    assert v is not None
    code = v.code
    safety = ctx.safety

    # ---- verdict sentence
    if code == "BANNED_COMBINATION":
        slots["list_date"] = fmt_date((safety.notification_date or safety.list_as_of) if safety else None, lang)
        slots["list_ref"] = safety.notification_ref if safety else ""
        segs.append(Segment(_fill(verdicts["BANNED_COMBINATION"], slots), "dynamic"))
    elif v.mode == "compare":
        p, d = ctx.prescribed, ctx.dispensed
        slots.update(
            rx_brand=(brand_display(p.brand) if p and p.brand else salts_display(p)),
            strip_brand=(brand_display(d.brand) if d and d.brand else salts_display(d)),
            rx_salts=salt_names_display(p), strip_salts=salt_names_display(d),
            rx_release=release_label(p.release if p else None, lang),
            strip_release=release_label(d.release if d else None, lang),
            rx_form=form_label(p.form if p else None, lang),
            strip_form=form_label(d.form if d else None, lang),
        )
        primary = v.facts.get("primary") or {}
        if code in ("STRENGTH_MISMATCH", "CONCENTRATION_MISMATCH"):
            diffs = primary.get("salts", [])
            slots["rx_strength"] = " + ".join(x["prescribed"] for x in diffs)
            slots["strip_strength"] = " + ".join(x["dispensed"] for x in diffs)
        if code == "UNVERIFIED":
            segs.append(Segment(verdicts["UNVERIFIED"], "fixed"))
            if v.facts.get("banned_check"):
                segs.append(Segment(verdicts["UNVERIFIED_BANNED_CHECK"], "fixed"))
        else:
            segs.append(Segment(_fill(verdicts[code], slots), "dynamic"))
    else:  # lookup
        # A symptom question about a salt named without strength ("सर्दी के लिए पैरासिटामोल?") needs
        # only the salt; "full details missing" would bury the answer. Banned-check doubts still speak.
        symptom_only = (code == "UNVERIFIED" and ctx.skip_detail and item is not None and bool(item.salts)
                        and not v.facts.get("banned_check") and "salts" not in v.facts.get("missing", []))
        if symptom_only:
            segs.append(Segment(_fill(lines["lookup_intro" if item.brand else "lookup_intro_generic"], slots),
                                "dynamic"))
        elif code == "UNVERIFIED":
            if item and (item.brand or item.salts):
                segs.append(Segment(_fill(verdicts["UNVERIFIED_LOOKUP"], slots), "dynamic"))
            else:
                segs.append(Segment(verdicts["UNVERIFIED"], "fixed"))
            if v.facts.get("banned_check"):
                segs.append(Segment(verdicts["UNVERIFIED_BANNED_CHECK"], "fixed"))
        elif item and item.brand:
            segs.append(Segment(_fill(lines["lookup_intro"], slots), "dynamic"))
        else:
            segs.append(Segment(_fill(lines["lookup_intro_generic"], slots), "dynamic"))
    plain_segs.append(Segment(plain.get(code, plain["UNVERIFIED"]), "fixed"))

    # ---- intent detail
    detail: list[Segment] = []
    price = ctx.price
    alts = ctx.alternatives
    show_alts = code not in MISMATCH_CODES and code != "BANNED_COMBINATION"
    if ctx.intent == "DOSAGE_QUERY":
        if ctx.directions:
            detail.append(Segment(_fill(clauses["dosage_rx"], {"directions": ctx.directions}), "dynamic"))
        else:
            detail.append(Segment(clauses["dosage_none"], "fixed"))
    elif ctx.intent == "PRICE_CHECK" and code != "BANNED_COMBINATION":
        unit = _unit_word(item.form if item else None, lang)
        pslots = {"mrp": fmt_money(price.mrp) if price else "", "qty": fmt_num(price.pack_qty) if price else "",
                  "unit": unit}
        has_qty = bool(price and price.pack_qty)
        if price and price.mrp is not None and price.reference is not None and not price.stale:
            pslots.update(ref_price=fmt_money(price.reference), ref_qty=fmt_num(price.reference_pack_qty))
            key = "price_gap" if has_qty and price.reference_pack_qty else "price_gap_noqty"
            detail.append(Segment(_fill(clauses[key], pslots), "dynamic"))
            if price.ratio and price.ratio >= 2:
                detail.append(Segment(_fill(clauses["price_ratio"], {"ratio": fmt_num(price.ratio)}), "dynamic"))
        elif price and price.mrp is not None:
            key = "price_mrp_only" if has_qty else "price_mrp_only_noqty"
            detail.append(Segment(_fill(clauses[key], pslots), "dynamic"))
        elif price and price.reference is not None and not price.stale:
            detail.append(Segment(_fill(clauses["price_ref_only"], {
                "ref_price": fmt_money(price.reference), "ref_qty": fmt_num(price.reference_pack_qty),
                "unit": unit}), "dynamic"))
        elif not (price and price.nppa_ceiling_unit is not None):
            detail.append(Segment(clauses["price_unknown"], "fixed"))
        if price and price.above_ceiling:
            detail.append(Segment(clauses["above_ceiling"], "fixed"))
        elif price and price.above_ceiling is False:
            detail.append(Segment(clauses["within_ceiling"], "fixed"))
        elif price and price.nppa_ceiling_unit is not None:
            detail.append(Segment(_fill(clauses["ceiling_info"], {
                "ceiling": fmt_money(price.nppa_ceiling_unit),
                "unit_one": _unit_word(item.form if item else None, lang, one=True)}), "dynamic"))
        if price and price.stale:
            detail.append(Segment(clauses["data_stale"], "fixed"))
    if show_alts and ctx.intent in ("FIND_GENERIC", "PRICE_CHECK", "VERIFY_MATCH", "CLARIFY", None):
        if alts:
            a = alts[0]
            alt_slots = {
                "alt_name": brand_display(a.name), "alt_qty": fmt_num(a.pack_qty),
                "alt_unit": _unit_word(item.form if item else None, lang),
                "alt_price": fmt_money(a.price), "saves_pct": a.saves_pct,
            }
            key = "alt_cheapest" if a.saves_pct is not None else "alt_cheapest_noprice"
            detail.append(Segment(_fill(clauses[key], alt_slots), "dynamic"))
        elif ctx.intent == "FIND_GENERIC" and code in ("MATCH", "GENERIC_EQUIVALENT"):
            detail.append(Segment(clauses["no_alt"], "fixed"))

    if ctx.skip_detail:
        detail = []

    # ---- flags: safety warnings outrank price detail; info flags come last
    warn_segs: list[Segment] = []
    flag_segs: list[Segment] = []
    answer_segs: list[Segment] = []  # the reply to "is it right for X?" / "what is it for?": never cut
    uses = t["uses"]
    for f in ctx.flags:
        key = f"flag_{f.code}"
        if key not in clauses:
            continue
        fs: dict[str, Any] = {}
        if f.code == "DUPLICATE_SALT":
            fs["dup_salts"] = " + ".join(s.title() for s in f.facts.get("salts", []))
        elif f.code.startswith(("SYMPTOM_", "USE_")):
            names = [uses.get(u, u) for u in f.facts.get("usual_use", [])[:3]]
            sep = f" {load(lang)['words']['or']} "
            fs["usual_use"] = sep.join(names) if len(names) < 3 else ", ".join(names[:-1]) + sep + names[-1]
            fs["symptom"] = uses.get(f.facts.get("symptom"), f.facts.get("symptom"))
            fs["source"] = f.facts.get("source", "")
        elif f.code == "LOOK_ALIKE":
            fs["similar_to"] = ", ".join(f.facts.get("similar_to", [])[:2])
        elif f.code == "NSQ_BATCH":
            fs["batch"] = f.facts.get("batch_no", "")
        kind = "fixed" if "{" not in clauses[key] else "dynamic"
        target = warn_segs if f.code in WARNING_FLAG_CODES else flag_segs
        if f.code.startswith(("SYMPTOM_", "USE_")):
            target = answer_segs
        target.append(Segment(_fill(clauses[key], fs), kind))

    # ---- length budget: verdict + warnings + detail + info flags + doctor <= ~MAX_WORDS
    body = list(segs) + answer_segs
    for group in (warn_segs, detail, flag_segs):
        for s in group:
            if _words(body + [s, doctor]) <= MAX_WORDS or not body:
                body.append(s)
    out = body + [doctor]

    # ---- Speak-to-Chemist
    cslots = dict(slots)
    if code in chemist:
        chemist_text = _fill(chemist[code], cslots)
    elif code == "UNVERIFIED":
        chemist_text = _fill(chemist["UNVERIFIED"], cslots)
    else:
        chemist_text = _fill(chemist["GENERIC"], cslots)

    return Rendered(out, _join(out), chemist_text, plain_segs + [doctor])


def _words(segs: list[Segment]) -> int:
    return sum(len(s.text.split()) for s in segs)


def _join(segs: list[Segment]) -> str:
    return " ".join(s.text.strip() for s in segs if s.text.strip())
