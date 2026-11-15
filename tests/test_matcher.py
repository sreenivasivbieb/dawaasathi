"""Phonetic brand matcher over the dev fixture DB: spoken variants -> SKUs / salts."""

from __future__ import annotations

import dataclasses

import pytest
from helpers import SUMMARY

from app.db import Store
from app.match.index import MatchIndex
from app.match.translit import normalize, script_of, to_roman
from app.nlu.entities import extract


def search(index: MatchIndex, text: str, **kw):
    e = extract(text)
    return e, index.search(e.name_tokens, strength=e.strength, strengths=e.strengths, form=e.form,
                           release=e.release, **kw)


def name_of(store: Store, cand) -> str:
    return store.brands[cand.brand_id].name if cand.brand_id is not None else f"salt:{cand.salt}"


# ---------------------------------------------------------------- transliteration
def test_transliteration_basics():
    assert to_roman("ऑगमेंटिन") == "ogmentin"
    assert to_roman("डोलो") == "dolo"
    assert script_of("ஆக்மென்டின்") == "tamil"
    assert script_of("ఆగ్మెంటిన్") == "telugu"
    assert script_of("Dolo") == "latin"
    # Spelling / script variants collapse to (nearly) the same key.
    from rapidfuzz import fuzz

    assert normalize("Augmentin") == normalize("augmentin")
    assert normalize("Zifi") == normalize("ziphi")
    for a, b in [("Augmentin", "augmenten"), ("Augmentin", "ऑगमेंटिन"), ("Glycomet", "ग्लाइकोमेट"),
                 ("Dolo", "डोलो")]:
        assert fuzz.ratio(normalize(a), normalize(b)) >= 75, (a, b, normalize(a), normalize(b))


# ---------------------------------------------------------------- required top-1 cases
@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("ऑगमेंटिन 625", "Augmentin 625 Duo Tablet"),
        ("augmenten 625", "Augmentin 625 Duo Tablet"),
        ("डोलो 650", "Dolo 650 Tablet"),
        ("dolo six fifty", "Dolo 650 Tablet"),
        ("पैन 40", "Pan 40 Tablet"),
        ("ग्लाइकोमेट 500", "Glycomet 500 Tablet"),
        ("glycomet sr 500", "Glycomet SR 500 Tablet"),
        ("मॉन्टेयर एलसी", "Montair LC Tablet"),
        ("taxim o 200", "Taxim-O 200 Tablet"),
        ("zifi 200", "Zifi 200 Tablet"),
        ("पैरासिटामोल 650", "salt:paracetamol"),
    ],
)
def test_spoken_variant_top1(index, store, spoken, expected):
    _, res = search(index, spoken)
    assert res.candidates, spoken
    assert name_of(store, res.top) == expected, [name_of(store, c) for c in res.candidates]


def test_dolo_six_fifty_carries_strength(index, store):
    ents, res = search(index, "dolo six fifty")
    assert ents.strength == 650
    assert res.top.strength_ok is True
    assert res.confident


def test_glycomet_sr_resolves_to_sr_sku(index, store):
    _, res = search(index, "glycomet sr 500")
    b = store.brands[res.top.brand_id]
    assert b.release == "SR"
    assert b.name == "Glycomet SR 500 Tablet"
    # The plain IR SKU and the glimepiride combo rank below it.
    assert all(store.brands[c.brand_id].release == "SR" or c.score < res.top.score for c in res.candidates[1:])


def test_glycomet_without_release_prefers_ir_but_is_not_confident(index, store):
    _, res = search(index, "glycomet 500")
    assert name_of(store, res.top) == "Glycomet 500 Tablet"
    assert not res.confident  # IR vs SR is ambiguous when "SR" is not said -> ask


def test_unspoken_sr_does_not_win_due_to_printed_strength(store, brand_ids):
    # Match the supplied A-Z representation: IR strength exists in the composition but its
    # product name is simply "Glycomet Tablet"; SR has 500 printed in the product name.
    ir_id = brand_ids["Glycomet 500 Tablet"]
    sr_id = brand_ids["Glycomet SR 500 Tablet"]
    ir = dataclasses.replace(store.brands[ir_id], name="Glycomet Tablet", label_tokens=["tablet"])
    sr = dataclasses.replace(store.brands[sr_id], name="Glycomet 500 SR Tablet", label_tokens=["500", "sr", "tablet"])
    sample = Store(brands={ir_id: ir, sr_id: sr})
    result = MatchIndex(sample).build().search(["Glycomet"], strengths=[500], form="tablet")
    assert result.top.brand_id == ir_id
    assert not result.confident


def test_salt_candidate_for_generic_name(index):
    _, res = search(index, "पैरासिटामोल 650")
    assert res.top.brand_id is None
    assert res.top.salt == "paracetamol"
    assert res.confident


@pytest.mark.parametrize("spoken", ["ஆக்மென்டின்", "ఆగ్మెంటిన్"])
def test_south_indian_scripts_reach_augmentin_family(index, store, spoken):
    _, res = search(index, spoken)
    top3 = [store.brands[c.brand_id].family for c in res.candidates[:3] if c.brand_id is not None]
    assert "augmentin" in top3


def test_brand_without_strength_is_not_sku_confident(index, store):
    ents, res = search(index, "augmentin")
    assert ents.strengths == []
    skus = [c for c in res.candidates if c.brand_id is not None and store.brands[c.brand_id].family == "augmentin"]
    assert len(skus) >= 2  # several SKUs ranked ...
    assert not res.confident  # ... and none accepted on its own
    assert all(c.strength_ok is None for c in skus)


def test_strength_disambiguates_siblings(index, store):
    _, res = search(index, "augmentin 375")
    assert name_of(store, res.top) == "Augmentin 375 Tablet"
    _, res = search(index, "calpol 500")
    assert name_of(store, res.top) == "Calpol 500 Tablet"


def test_form_disambiguates_siblings(index, store):
    _, res = search(index, "calpol syrup 250")
    assert name_of(store, res.top) == "Calpol 250 Suspension"


# ---------------------------------------------------------------- restriction
def test_restrict_ids_limits_results(index, store, brand_ids):
    only = {brand_ids["Augmentin 375 Tablet"]}
    res = index.search(["augmentin"], restrict_ids=only)
    assert res.candidates
    assert {c.brand_id for c in res.candidates} <= only


def test_restrict_ids_excludes_other_brands(index, brand_ids):
    res = index.search(["augmentin"], strengths=[625.0], restrict_ids={brand_ids["Dolo 650 Tablet"]})
    assert all(c.brand_id == brand_ids["Dolo 650 Tablet"] for c in res.candidates)
    assert not res.confident


def test_restrict_salts_allows_salt_entries(index):
    res = index.search(["paracetamol"], restrict_ids=set(), restrict_salts={"paracetamol"})
    assert res.candidates
    assert all(c.salt == "paracetamol" for c in res.candidates)


def test_empty_tokens():
    from app.match.index import MatchResult

    res = MatchIndex(Store()).build().search([])
    assert isinstance(res, MatchResult)
    assert res.reason == "no_tokens" and not res.candidates


def test_sibling_ranking_does_not_depend_on_row_ids(store):
    """Re-number the brands in reverse: printed names must still pick the same SKUs."""
    n = max(store.brands) + 1000
    remap = {bid: n - bid for bid in store.brands}
    perm = Store(
        brands={remap[b]: dataclasses.replace(rec, id=remap[b]) for b, rec in store.brands.items()},
        by_salt={k: [remap[i] for i in v] for k, v in store.by_salt.items()},
        aliases=[(remap[b], a) for b, a in store.aliases],
        salt_use=store.salt_use, price_refs=store.price_refs, banned=store.banned, nsq=store.nsq,
        lasa=store.lasa, datasets=store.datasets,
    )
    for idx_store in (store, perm):
        idx = MatchIndex(idx_store).build()
        top = idx.search(["Glycomet", "500"], strengths=[500.0]).top
        assert idx_store.brands[top.brand_id].name == "Glycomet 500 Tablet"
        top = idx.search(["Glycomet", "SR", "500"], strengths=[500.0], release="SR").top
        assert idx_store.brands[top.brand_id].name == "Glycomet SR 500 Tablet"
        top = idx.search(["Calpol", "650"]).top
        assert idx_store.brands[top.brand_id].name == "Calpol 650 Tablet"


# ---------------------------------------------------------------- accuracy over spoken variants
# (spoken, truth) -- truth is an exact SKU name, "salt:<name>" or "family:<family>".
SPOKEN_VARIANTS = [
    ("ऑगमेंटिन 625", "Augmentin 625 Duo Tablet"),
    ("augmenten 625", "Augmentin 625 Duo Tablet"),
    ("ओगमेंटिन छह सौ पच्चीस", "Augmentin 625 Duo Tablet"),
    ("डोलो 650", "Dolo 650 Tablet"),
    ("dolo six fifty", "Dolo 650 Tablet"),
    ("डोलो छह सौ पचास", "Dolo 650 Tablet"),
    ("डोलो साढ़े छह सौ", "Dolo 650 Tablet"),
    ("पैन 40", "Pan 40 Tablet"),
    ("pan chalis", "Pan 40 Tablet"),
    ("ग्लाइकोमेट 500", "Glycomet 500 Tablet"),
    ("glycomet sr 500", "Glycomet SR 500 Tablet"),
    ("मॉन्टेयर एलसी", "Montair LC Tablet"),
    ("taxim o 200", "Taxim-O 200 Tablet"),
    ("zifi 200", "Zifi 200 Tablet"),
    ("ज़िफ़ी दो सौ", "Zifi 200 Tablet"),
    ("पैरासिटामोल 650", "salt:paracetamol"),
    ("ஆக்மென்டின்", "family:augmentin"),
    ("ఆగ్మెంటిన్", "family:augmentin"),
    ("calpol 650", "Calpol 650 Tablet"),
    ("calpol syrup 250", "Calpol 250 Suspension"),
    ("टेल्मा 40", "Telma 40 Tablet"),
    ("telma chalis", "Telma 40 Tablet"),
    ("अमलोंग 5", "Amlong 5 Tablet"),
    ("एज़िथ्रल 500", "Azithral 500 Tablet"),
    ("azee 500", "Azee 500 Tablet"),
    ("थायरोनॉर्म 50", "Thyronorm 50 mcg Tablet"),
    ("सेटज़ीन", "Cetzine 10 Tablet"),
    ("ओकासेट 10", "Okacet 10 Tablet"),
    ("allegra 120", "Allegra 120 Tablet"),
    ("कॉम्बिफ्लेम", "Combiflam Tablet"),
    ("ज़ीरोडोल पी", "Zerodol-P Tablet"),
    ("इकोस्प्रिन 75", "Ecosprin 75 Tablet"),
    ("ecosprin pachattar", "Ecosprin 75 Tablet"),
    ("शेलकैल 500", "Shelcal 500 Tablet"),
    ("क्लैवम 625", "Clavam 625 Tablet"),
    ("नोवामॉक्स 500", "Novamox 500 Capsule"),
    ("mox 500 capsule", "Mox 500 Capsule"),
    ("मेटफॉर्मिन 500", "salt:metformin"),
    ("azithromycin 500", "salt:azithromycin"),
    ("लिनोस्पैन 600", "Linospan 600 Tablet"),
    ("ट्रेजेंटा 5", "Trajenta 5 Tablet"),
    ("पैंटोसिड 40", "Pantocid 40 Tablet"),
    ("moxikind cv 625", "Moxikind-CV 625 Tablet"),
    ("एटोर्वा दस", "Atorva 10 Tablet"),
    ("crocin advance", "Crocin Advance Tablet"),
]


def _hit(store: Store, cand, truth: str) -> bool:
    if truth.startswith("salt:"):
        return cand.salt == truth[5:]
    if truth.startswith("family:"):
        return cand.brand_id is not None and store.brands[cand.brand_id].family == truth[7:]
    return cand.brand_id is not None and store.brands[cand.brand_id].name == truth


def test_spoken_variant_accuracy(index, store):
    top1 = top3 = 0
    misses = []
    for spoken, truth in SPOKEN_VARIANTS:
        _, res = search(index, spoken)
        c = res.candidates
        h1 = bool(c) and _hit(store, c[0], truth)
        h3 = any(_hit(store, x, truth) for x in c[:3])
        top1 += h1
        top3 += h3
        if not h1:
            misses.append((spoken, truth, [name_of(store, x) for x in c[:3]]))
    n = len(SPOKEN_VARIANTS)
    line = (f"matcher: {n} spoken variants, top-1 {top1}/{n} ({100 * top1 / n:.1f}%), "
            f"top-3 {top3}/{n} ({100 * top3 / n:.1f}%)")
    print(line)
    SUMMARY.append(line)
    for m in misses:
        SUMMARY.append(f"  top-1 miss: {m}")
    assert n >= 30
    assert top3 / n >= 0.90, misses
    assert top1 / n >= 0.85, misses
