"""Deterministic verdict engine + banned FDC / NSQ checks + soft checks, table-driven."""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path

import pytest
from helpers import SUMMARY

from app.db import Store
from app.engine import checks, rules, safety as safetymod
from app.engine.salts import parse_composition
from app.models import DrugItem, SafetyResult, SaltDose

_spec = importlib.util.spec_from_file_location("rule_cases", Path(__file__).parent / "data" / "rule_cases.py")
rule_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rule_cases)

STALE_DAYS = 400


def build(spec: dict, source: str) -> DrugItem:
    salts = parse_composition(spec["comp"]) if spec["comp"] else []
    return DrugItem(source=source, brand=spec["brand"], brand_id=spec["brand_id"], salts=salts,
                    form=spec["form"], release=spec["release"], composition_uncertain=spec.get("uncertain", False))


def safety_of(status: str) -> SafetyResult:
    banned = {"banned": True, "not_listed": False}.get(status)
    return SafetyResult(banned=banned, status=status, list_as_of="2026-11-12", source="test")


def store_variant(store: Store, name: str) -> Store:
    if name == "fixture":
        return store
    if name == "empty":
        return dataclasses.replace(store, banned=[])
    if name == "stale":
        ds = dict(store.datasets)
        ds["banned_fdc"] = {**ds["banned_fdc"], "as_of": "2020-01-01"}
        return dataclasses.replace(store, datasets=ds)
    if name == "strengths":
        rec = dataclasses.replace(store.banned[0], strengths={"aceclofenac": 100.0, "paracetamol": 325.0,
                                                               "rabeprazole": 20.0})
        return dataclasses.replace(store, banned=[rec])
    if name == "formless":
        return dataclasses.replace(store, banned=[dataclasses.replace(store.banned[0], form=None)])
    raise KeyError(name)


# ---------------------------------------------------------------- evaluation
def run_compare(case) -> tuple[str, list[str]]:
    _id, rx, strip, status, _exp, _flags = case
    v = rules.compare(build(rx, "rx"), build(strip, "strip"), safety_of(status))
    assert v.mode == "compare"
    return v.code, [f.code for f in v.flags]


def run_lookup(case) -> tuple[str, list[str]]:
    _id, spec, status, has_alt, _exp, _flags = case
    v = rules.lookup(build(spec, "strip"), safety_of(status), has_alt)
    assert v.mode == "lookup"
    return v.code, [f.code for f in v.flags]


def run_banned(store: Store, case) -> str:
    _id, spec, variant, _exp = case
    return safetymod.check_banned(store_variant(store, variant), build(spec, "strip"), STALE_DAYS).status


def _ids(cases):
    return [c[0] for c in cases]


@pytest.mark.parametrize("case", rule_cases.COMPARE_CASES, ids=_ids(rule_cases.COMPARE_CASES))
def test_compare_case(store, case):
    code, flags = run_compare(case)
    assert code == case[4]
    assert flags == case[5]


@pytest.mark.parametrize("case", rule_cases.LOOKUP_CASES, ids=_ids(rule_cases.LOOKUP_CASES))
def test_lookup_case(store, case):
    code, flags = run_lookup(case)
    assert code == case[4]
    assert flags == case[5]


@pytest.mark.parametrize("case", rule_cases.BANNED_CASES, ids=_ids(rule_cases.BANNED_CASES))
def test_banned_case(store, case):
    assert run_banned(store, case) == case[3]


def test_rule_case_summary(store):
    total = correct = 0
    wrong = []
    labelled = [(run_compare, c) for c in rule_cases.COMPARE_CASES] + [(run_lookup, c) for c in rule_cases.LOOKUP_CASES]
    for run, case in labelled:
        got = run(case)
        ok = got == (case[4], case[5])
        total += 1
        correct += ok
        if not ok:
            wrong.append((case[0], case[4], got))
    for case in rule_cases.BANNED_CASES:
        got = run_banned(store, case)
        total += 1
        correct += got == case[3]
        if got != case[3]:
            wrong.append((case[0], case[3], got))
    line = (f"rule cases: {correct}/{total} correct ({len(rule_cases.COMPARE_CASES)} compare, "
            f"{len(rule_cases.LOOKUP_CASES)} lookup, {len(rule_cases.BANNED_CASES)} banned-list)")
    print(line)
    SUMMARY.append(line)
    assert total >= 50
    assert not wrong, wrong


# ---------------------------------------------------------------- verdict facts
def test_printed_total_is_never_a_salt_strength(store, brand_ids):
    aug = store.brand_item(brand_ids["Augmentin 625 Duo Tablet"], "rx")
    assert sorted((s.name, s.amount) for s in aug.salts) == [("amoxicillin", 500), ("clavulanic acid", 125)]
    moxi = store.brand_item(brand_ids["Moxikind-CV 625 Tablet"], "strip")
    v = rules.compare(aug, moxi, safety_of("not_listed"))
    assert v.code == "GENERIC_EQUIVALENT"
    labels = v.facts["salts"]
    assert labels == ["Amoxicillin 500 mg", "Clavulanic Acid 125 mg"]
    assert not any("625" in lab for lab in labels)
    # 625 as a lone "amoxicillin" strength would be a different salt set, not a strength match.
    wrong = DrugItem(source="rx", brand="Augmentin 625", salts=[SaltDose(name="amoxicillin", amount=625, unit="mg")],
                     form="tablet", release="IR")
    assert rules.compare(wrong, moxi, safety_of("not_listed")).code == "SALT_MISMATCH"


def test_strength_mismatch_facts_name_only_the_differing_salt():
    rx = DrugItem(source="rx", salts=parse_composition("amoxicillin 500mg + clavulanic acid 125mg"),
                  form="tablet", release="IR")
    strip = DrugItem(source="strip", salts=parse_composition("amoxicillin 875mg + clavulanic acid 125mg"),
                     form="tablet", release="IR")
    v = rules.compare(rx, strip, safety_of("not_listed"))
    assert v.code == "STRENGTH_MISMATCH"
    assert v.facts["primary"]["salts"] == [
        {"salt": "amoxicillin", "prescribed": "Amoxicillin 500 mg", "dispensed": "Amoxicillin 875 mg"}]


def test_salt_mismatch_facts():
    rx = DrugItem(source="rx", salts=parse_composition("paracetamol 325mg"), form="tablet", release="IR")
    strip = DrugItem(source="strip", salts=parse_composition("ibuprofen 400mg + paracetamol 325mg"),
                     form="tablet", release="IR")
    v = rules.compare(rx, strip, safety_of("not_listed"))
    assert v.facts["primary"] == {"prescribed": ["paracetamol"], "dispensed": ["ibuprofen", "paracetamol"],
                                  "missing": [], "extra": ["ibuprofen"]}


def test_unverified_facts_list_missing_fields_and_banned_check():
    rx = DrugItem(source="rx", salts=parse_composition("metformin 500mg"), form=None, release=None)
    strip = DrugItem(source="strip", salts=parse_composition("metformin 500mg"), form="tablet", release="IR")
    v = rules.compare(rx, strip, safety_of("unverified"))
    assert v.code == "UNVERIFIED"
    assert v.facts["missing"] == ["form", "release"]
    assert v.facts["banned_check"] == "unverified"


def test_banned_compare_keeps_both_sides_in_facts():
    rx = DrugItem(source="rx", brand="Dolo 650", salts=parse_composition("paracetamol 650mg"), form="tablet",
                  release="IR")
    strip = DrugItem(source="strip", brand="Saathi-Demo FDC",
                     salts=parse_composition(rule_cases.FDC), form="tablet", release="IR")
    v = rules.compare(rx, strip, safety_of("banned"))
    assert v.code == "BANNED_COMBINATION"
    assert v.facts["prescribed"]["brand"] == "Dolo 650"
    assert v.facts["dispensed"]["salt_names"] == ["aceclofenac", "paracetamol", "rabeprazole"]


def test_lookup_no_comparison_flag_detail():
    item = DrugItem(source="voice", brand="Trajenta 5", salts=parse_composition("linagliptin 5mg"), form="tablet")
    v = rules.lookup(item, safety_of("not_listed"), False)
    assert v.code == "MATCH" and v.flags[0].code == "NO_COMPARISON"


# ---------------------------------------------------------------- units
@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        (SaltDose(name="x", amount=0.5, unit="g"), SaltDose(name="x", amount=500, unit="mg"), True),
        (SaltDose(name="x", amount=50, unit="mcg"), SaltDose(name="x", amount=0.05, unit="mg"), True),
        (SaltDose(name="x", amount=50, unit="µg"), SaltDose(name="x", amount=50, unit="mcg"), True),
        (SaltDose(name="x", amount=60000, unit="IU"), SaltDose(name="x", amount=60000, unit="iu"), True),
        (SaltDose(name="x", amount=500, unit="mg"), SaltDose(name="x", amount=650, unit="mg"), False),
    ],
)
def test_comparable_units(a, b, equal):
    assert (a.comparable() == b.comparable()) is equal


def test_concentration():
    assert SaltDose(name="p", amount=250, unit="mg", per_ml=5).concentration() == (50.0, "mg/ml")
    assert SaltDose(name="p", amount=50, unit="mg", per_ml=1).concentration() == (50.0, "mg/ml")
    assert SaltDose(name="p", amount=250, unit="mg").concentration() is None
    assert SaltDose(name="p").comparable() is None


def test_parse_composition_per_salt_strengths():
    salts = parse_composition("Amoxycillin (500mg) + Clavulanic Acid (125mg)")
    assert [(s.name, s.amount, s.unit) for s in salts] == [("amoxicillin", 500, "mg"), ("clavulanic acid", 125, "mg")]
    salts = parse_composition("Amoxicillin 200mg/5ml + Clavulanic acid 28.5mg/5ml")
    assert [(s.name, s.amount, s.per_ml) for s in salts] == [("amoxicillin", 200, 5), ("clavulanic acid", 28.5, 5)]
    assert [(s.name, s.amount, s.unit) for s in parse_composition("Levothyroxine 50mcg")] == [
        ("levothyroxine", 50, "mcg")]


# ---------------------------------------------------------------- banned list details
def test_check_banned_reports_notification(store):
    item = build(rule_cases.item(rule_cases.FDC), "strip")
    res = safetymod.check_banned(store, item, STALE_DAYS)
    assert res.banned is True
    assert res.notification_ref == "DEV FIXTURE - not a real notification"
    assert res.notification_date == "2026-01-01"
    assert res.list_as_of == "2026-11-12"
    assert res.matched_salts == ["aceclofenac", "paracetamol", "rabeprazole"]


def test_check_banned_never_safe_when_unknown(store):
    item = build(rule_cases.item(rule_cases.FDC), "strip")
    for variant, reason in (("empty", "banned_list_not_loaded"), ("stale", "banned_list_stale")):
        res = safetymod.check_banned(store_variant(store, variant), item, STALE_DAYS)
        assert res.banned is None and res.status == "unverified" and res.reason == reason
    res = safetymod.check_banned(store, DrugItem(source="strip", brand="x"), STALE_DAYS)
    assert res.banned is None and res.reason == "salts_unknown"
    res = safetymod.check_banned(store, build(rule_cases.item(rule_cases.FDC, form=None), "strip"), STALE_DAYS)
    assert res.banned is None and res.status == "possible"


# ---------------------------------------------------------------- NSQ batch
@pytest.mark.parametrize(
    ("batch", "comp", "brand", "hit"),
    [
        ("DEMO123", "paracetamol 650mg", "Dolo 650", True),
        ("demo-123", "paracetamol 650mg", "Dolo 650", True),
        ("DEMO 123", "paracetamol 650mg", None, True),
        ("DEMO124", "paracetamol 650mg", "Dolo 650", False),
        ("DEMO123", "metformin 500mg", "Glycomet 500", False),  # same batch code, different drug
        (None, "paracetamol 650mg", "Dolo 650", False),
    ],
)
def test_check_batch(store, batch, comp, brand, hit):
    item = DrugItem(source="strip", brand=brand, salts=parse_composition(comp), form="tablet", batch_no=batch)
    alert = safetymod.check_batch(store, item)
    assert (alert is not None) is hit
    if hit:
        assert alert["batch_no"] == "DEMO123"


# ---------------------------------------------------------------- soft checks
def test_symptom_check(store):
    dolo = DrugItem(source="voice", salts=parse_composition("paracetamol 650mg"), form="tablet")
    assert checks.symptom_check(store, dolo, "fever").code == "SYMPTOM_OK"
    f = checks.symptom_check(store, dolo, "diabetes")
    assert f.code == "SYMPTOM_MISMATCH" and f.facts["usual_use"] == ["fever", "pain"]
    aug = DrugItem(source="voice", salts=parse_composition("amoxicillin 500mg + clavulanic acid 125mg"))
    assert checks.symptom_check(store, aug, "fever").code == "SYMPTOM_MISMATCH"
    assert checks.symptom_check(store, aug, None) is None
    unknown = DrugItem(source="voice", salts=[SaltDose(name="unobtainium")])
    assert checks.symptom_check(store, unknown, "fever") is None


def test_dup_salt():
    dolo = DrugItem(source="voice", salts=parse_composition("paracetamol 650mg"))
    f = checks.dup_salt(dolo, ["Paracetamol", "cetirizine"])
    assert f.code == "DUPLICATE_SALT" and f.facts["salts"] == ["paracetamol"]
    assert checks.dup_salt(dolo, ["acetaminophen"]).facts["salts"] == ["paracetamol"]  # synonym
    assert checks.dup_salt(dolo, ["metformin"]) is None
    assert checks.dup_salt(dolo, []) is None


def test_look_alike(store):
    lina = DrugItem(source="voice", salts=parse_composition("linagliptin 5mg"))
    f = checks.look_alike(store, lina)
    assert f.code == "LOOK_ALIKE" and f.facts["similar_to"] == ["Linezolid"]
    traj = DrugItem(source="voice", brand="Trajenta 5 Tablet", salts=parse_composition("linagliptin 5mg"))
    assert set(checks.look_alike(store, traj).facts["similar_to"]) == {"Linezolid", "Linospan"}
    assert checks.look_alike(store, DrugItem(source="voice", salts=parse_composition("metformin 500mg"))) is None
