"""Deterministic verdict engine: prescribed (rx or voice) vs dispensed (strip), or a lookup.

Order (first failure wins), from the design doc:
  1. banned combination  -> BANNED_COMBINATION (other differences attached as flags)
  2. salt set differs    -> SALT_MISMATCH
  3. strength per salt   -> STRENGTH_MISMATCH        (solid forms)
  4. release class       -> RELEASE_MISMATCH
  5. dosage form         -> FORM_MISMATCH; liquids: mg/ml differs -> CONCENTRATION_MISMATCH
  6. brand differs only  -> GENERIC_EQUIVALENT
  7. all equal           -> MATCH
  8. a needed field missing on either side, or banned status unknown -> UNVERIFIED
A definite mismatch on known fields wins over a missing field elsewhere; a missing field
always beats MATCH / GENERIC_EQUIVALENT ("never safe when unknown").
"""

from __future__ import annotations

from app.models import LIQUID_FORMS, DrugItem, Flag, SafetyResult, Verdict


def _salt_facts(item: DrugItem) -> list[str]:
    return item.salt_labels()


def _base_facts(item: DrugItem) -> dict:
    return {
        "brand": item.brand,
        "salts": _salt_facts(item),
        "salt_names": item.salt_names(),
        "form": item.form,
        "release": item.release,
        "mrp": item.mrp,
        "pack_qty": item.pack_qty,
        "source": item.source,
    }


def _release(item: DrugItem) -> str | None:
    return item.release


def compare(prescribed: DrugItem, dispensed: DrugItem, safety: SafetyResult) -> Verdict:
    facts = {"prescribed": _base_facts(prescribed), "dispensed": _base_facts(dispensed)}
    facts.update(salts=_salt_facts(dispensed), form=dispensed.form, release=dispensed.release,
                 brand=dispensed.brand)
    differences: list[Flag] = []
    missing: list[str] = []

    # 2. salt set
    if not prescribed.salts or not dispensed.salts:
        missing.append("salts")
    else:
        a, b = set(prescribed.salt_names()), set(dispensed.salt_names())
        if a != b:
            differences.append(Flag(code="SALT_MISMATCH", facts={
                "prescribed": sorted(a), "dispensed": sorted(b),
                "missing": sorted(a - b), "extra": sorted(b - a)}))

    salts_equal = bool(prescribed.salts and dispensed.salts and
                       set(prescribed.salt_names()) == set(dispensed.salt_names()))
    p_form, d_form = prescribed.form, dispensed.form
    liquid = (p_form in LIQUID_FORMS) or (d_form in LIQUID_FORMS)

    # 3. strength per salt (solid forms)
    if salts_equal and not liquid:
        pm = {s.name: s for s in prescribed.salts}
        dm = {s.name: s for s in dispensed.salts}
        diffs = []
        for name in sorted(pm):
            pc, dc = pm[name].comparable(), dm[name].comparable()
            if pc is None or dc is None:
                missing.append(f"strength:{name}")
            elif pc != dc:
                diffs.append({"salt": name, "prescribed": pm[name].label(), "dispensed": dm[name].label()})
        if diffs:
            differences.append(Flag(code="STRENGTH_MISMATCH", facts={"salts": diffs}))

    # 4. release class
    pr, dr = _release(prescribed), _release(dispensed)
    if pr is None or dr is None:
        missing.append("release")
    elif pr != dr:
        differences.append(Flag(code="RELEASE_MISMATCH", facts={"prescribed": pr, "dispensed": dr}))

    # 5. form, then concentration for liquids
    if p_form is None or d_form is None:
        missing.append("form")
    elif p_form != d_form:
        differences.append(Flag(code="FORM_MISMATCH", facts={"prescribed": p_form, "dispensed": d_form}))
    elif salts_equal and p_form in LIQUID_FORMS:
        pm = {s.name: s for s in prescribed.salts}
        dm = {s.name: s for s in dispensed.salts}
        diffs = []
        for name in sorted(pm):
            pc, dc = pm[name].concentration(), dm[name].concentration()
            if pc is None or dc is None:
                missing.append(f"concentration:{name}")
            elif pc != dc:
                diffs.append({"salt": name, "prescribed": pm[name].label(), "dispensed": dm[name].label()})
        if diffs:
            differences.append(Flag(code="CONCENTRATION_MISMATCH", facts={"salts": diffs}))

    flags = list(differences)
    if missing:
        facts["missing"] = sorted(set(missing))

    if safety.status == "banned":
        return Verdict(code="BANNED_COMBINATION", mode="compare", facts=facts, flags=flags)
    if differences:
        facts["primary"] = differences[0].facts
        return Verdict(code=differences[0].code, mode="compare", facts=facts, flags=flags[1:])
    if missing or safety.status in ("unverified", "possible"):
        if safety.status in ("unverified", "possible"):
            facts["banned_check"] = safety.status
        return Verdict(code="UNVERIFIED", mode="compare", facts=facts, flags=flags)
    same_brand = _same_brand(prescribed, dispensed)
    code = "MATCH" if same_brand else "GENERIC_EQUIVALENT"
    return Verdict(code=code, mode="compare", facts=facts, flags=flags)


def _same_brand(a: DrugItem, b: DrugItem) -> bool:
    if a.brand_id is not None and b.brand_id is not None:
        return a.brand_id == b.brand_id
    if not a.brand or not b.brand:
        return False
    return a.brand.lower().split()[0] == b.brand.lower().split()[0]


def lookup(item: DrugItem, safety: SafetyResult, has_cheaper_alternative: bool) -> Verdict:
    """Voice-only (or single image) query: nothing to compare against.

    BANNED_COMBINATION if listed; UNVERIFIED if composition or the banned check is unknown;
    GENERIC_EQUIVALENT when a same salt/strength/form/release option exists; else MATCH with
    a no_comparison flag.
    """
    facts = _base_facts(item)
    if safety.status == "banned":
        return Verdict(code="BANNED_COMBINATION", mode="lookup", facts=facts)
    missing = []
    if not item.salts:
        missing.append("salts")
    elif any(s.amount is None for s in item.salts):
        missing.append("strength")
    if item.form is None:
        missing.append("form")
    if missing or safety.status in ("unverified", "possible"):
        if missing:
            facts["missing"] = missing
        if safety.status in ("unverified", "possible"):
            facts["banned_check"] = safety.status
        return Verdict(code="UNVERIFIED", mode="lookup", facts=facts)
    if has_cheaper_alternative:
        return Verdict(code="GENERIC_EQUIVALENT", mode="lookup", facts=facts)
    return Verdict(code="MATCH", mode="lookup", facts=facts,
                   flags=[Flag(code="NO_COMPARISON", detail="identified only; nothing to compare")])
