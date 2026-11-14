"""Labelled cases for the deterministic rule engine (rules.compare / rules.lookup) and the banned
FDC check (safety.check_banned). Plain data; tests/test_rules.py turns the specs into DrugItems.

Drug spec: item(composition, form, release, brand, brand_id). The composition string is parsed with
app.engine.salts.parse_composition, so "paracetamol" (no amount) means "strength unknown" and ""
means "salts unknown". form/release None mean "not known".

compare cases: rx, strip, safety status, expected verdict code, optional expected secondary flags.
lookup cases:  item, safety status, has_cheaper_alternative, expected code, optional flags.
banned cases:  item, store variant, expected SafetyResult.status.
"""

from __future__ import annotations


def item(comp: str, form: str | None = "tablet", release: str | None = "IR", brand: str | None = None,
         brand_id: int | None = None, uncertain: bool = False) -> dict:
    return {"comp": comp, "form": form, "release": release, "brand": brand, "brand_id": brand_id,
            "uncertain": uncertain}


AUG = "amoxycillin 500mg + clavulanic acid 125mg"
FDC = "aceclofenac 100mg + paracetamol 325mg + rabeprazole 20mg"

# id, rx, strip, safety, expected, flags (secondary differences kept on the verdict)
COMPARE_CASES: list[tuple] = [
    # ---- equal -> MATCH / GENERIC_EQUIVALENT
    ("same-sku-ids", item(AUG, brand="Augmentin 625 Duo", brand_id=1), item(AUG, brand="Augmentin 625 Duo Tablet", brand_id=1),
     "not_listed", "MATCH", []),
    ("same-brand-no-ids", item("paracetamol 650mg", brand="Dolo 650"), item("paracetamol 650mg", brand="DOLO 650 Tablet"),
     "not_listed", "MATCH", []),
    ("brand-differs-only", item(AUG, brand="Augmentin 625 Duo", brand_id=1), item(AUG, brand="Moxikind-CV 625", brand_id=4),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("rx-generic-name", item("paracetamol 650mg"), item("paracetamol 650mg", brand="Calpol 650"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("same-brand-rx-without-id", item("metformin 500mg", brand="Glycomet 500"),
     item("metformin 500mg", brand="Glycomet 500 Tablet", brand_id=17), "not_listed", "MATCH", []),
    ("salt-order-free", item("clavulanic acid 125mg + amoxicillin 500mg", brand="Augmentin"), item(AUG, brand="Clavam 625"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("salt-synonyms", item("amoxicillin 500mg + potassium clavulanate 125mg", brand="Augmentin"), item(AUG, brand="Clavam"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("units-g-vs-mg", item("paracetamol 0.5g", brand="Crocin"), item("paracetamol 500mg", brand="Calpol 500"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("units-1g-vs-1000mg", item("paracetamol 1g", brand="Pacimol"), item("paracetamol 1000mg", brand="Dolo 1000"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("units-mcg-vs-mg", item("levothyroxine 50mcg", brand="Thyronorm 50"), item("levothyroxine 0.05mg", brand="Eltroxin"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("units-iu-equal", item("cholecalciferol 60000IU", brand="Uprise D3"), item("cholecalciferol 60000 iu", brand="Calcirol"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("release-er-er", item("metformin 500mg", release="ER", brand="Glycomet XR"), item("metformin 500mg", release="ER", brand="Glyciphage XR"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("release-dr-dr", item("pantoprazole 40mg", release="DR", brand="Pan 40"), item("pantoprazole 40mg", release="DR", brand="Pantocid 40"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("capsule-capsule", item("amoxicillin 500mg", form="capsule", brand="Mox 500"), item("amoxicillin 500mg", form="capsule", brand="Novamox 500"),
     "not_listed", "GENERIC_EQUIVALENT", []),
    ("liquid-250-5-eq-50-1", item("paracetamol 250mg/5ml", form="oral_liquid", brand="Calpol 250"),
     item("paracetamol 50mg/ml", form="oral_liquid", brand="P-250"), "not_listed", "GENERIC_EQUIVALENT", []),
    ("liquid-125-5-eq-25-1", item("paracetamol 125mg/5ml", form="oral_liquid", brand="Calpol 125"),
     item("paracetamol 25mg/ml", form="oral_liquid", brand="Paracip"), "not_listed", "GENERIC_EQUIVALENT", []),
    ("drops-same-brand", item("paracetamol 100mg/ml", form="drops", brand="Crocin Drops", brand_id=14),
     item("paracetamol 100mg/ml", form="drops", brand="Crocin Drops", brand_id=14), "not_listed", "MATCH", []),
    ("liquid-combo-equal", item("amoxicillin 400mg/5ml + clavulanic acid 57mg/5ml", form="oral_liquid", brand="Augmentin Duo"),
     item("amoxicillin 80mg/ml + clavulanic acid 11.4mg/ml", form="oral_liquid", brand="Moxclav DS"),
     "not_listed", "GENERIC_EQUIVALENT", []),

    # ---- salt set differs
    ("salt-single-vs-combo", item("paracetamol 325mg", brand="Pacimol"), item("ibuprofen 400mg + paracetamol 325mg", brand="Combiflam"),
     "not_listed", "SALT_MISMATCH", []),
    ("salt-combo-vs-single", item(AUG, brand="Augmentin 625"), item("amoxicillin 500mg", brand="Mox 500"),
     "not_listed", "SALT_MISMATCH", []),
    ("salt-lasa-pair", item("linagliptin 5mg", brand="Trajenta 5"), item("linezolid 600mg", brand="Linospan 600"),
     "not_listed", "SALT_MISMATCH", []),
    ("salt-different-with-form-missing", item("cetirizine 10mg", form=None), item("levocetirizine 5mg"),
     "not_listed", "SALT_MISMATCH", []),
    ("salt-mismatch-banned-unverified", item("paracetamol 650mg"), item("aceclofenac 100mg + paracetamol 325mg"),
     "unverified", "SALT_MISMATCH", []),

    # ---- strength per salt
    ("strength-combo-amox-875", item(AUG, brand="Augmentin 625 Duo"), item("amoxicillin 875mg + clavulanic acid 125mg", brand="Augmentin 1000 Duo"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-combo-amox-250", item(AUG, brand="Augmentin 625 Duo"), item("amoxicillin 250mg + clavulanic acid 125mg", brand="Augmentin 375"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-same-total-625", item(AUG, brand="Augmentin 625"), item("amoxicillin 525mg + clavulanic acid 100mg", brand="Odd 625"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-single", item("paracetamol 500mg"), item("paracetamol 650mg", brand="Dolo 650"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-mcg", item("levothyroxine 50mcg", brand="Thyronorm 50"), item("levothyroxine 100mcg", brand="Thyronorm 100"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-mcg-vs-mg", item("levothyroxine 25mcg"), item("levothyroxine 0.05mg"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-g-vs-mg", item("paracetamol 1g"), item("paracetamol 650mg"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-beats-missing-release", item("metformin 500mg", release=None), item("metformin 1000mg", release="IR"),
     "not_listed", "STRENGTH_MISMATCH", []),
    ("strength-beats-banned-unverified", item("paracetamol 500mg"), item("paracetamol 650mg"),
     "unverified", "STRENGTH_MISMATCH", []),
    ("strength-then-release", item("metformin 500mg", release="IR"), item("metformin 1000mg", release="SR"),
     "not_listed", "STRENGTH_MISMATCH", ["RELEASE_MISMATCH"]),

    # ---- release class
    ("release-ir-vs-sr", item("metformin 500mg", release="IR", brand="Glycomet 500"), item("metformin 500mg", release="SR", brand="Glycomet SR 500"),
     "not_listed", "RELEASE_MISMATCH", []),
    ("release-sr-vs-ir", item("metformin 500mg", release="SR", brand="Glycomet SR 500"), item("metformin 500mg", release="IR", brand="Glycomet 500"),
     "not_listed", "RELEASE_MISMATCH", []),
    ("release-sr-vs-er", item("metformin 500mg", release="SR"), item("metformin 500mg", release="ER"),
     "not_listed", "RELEASE_MISMATCH", []),
    ("release-dr-vs-ir", item("pantoprazole 40mg", release="DR", brand="Pan 40"), item("pantoprazole 40mg", release="IR"),
     "not_listed", "RELEASE_MISMATCH", []),
    ("release-beats-missing-form", item("metformin 500mg", form=None, release="IR"), item("metformin 500mg", release="SR"),
     "not_listed", "RELEASE_MISMATCH", []),
    ("release-then-form", item("metformin 500mg", release="IR"), item("metformin 500mg", form="capsule", release="ER"),
     "not_listed", "RELEASE_MISMATCH", ["FORM_MISMATCH"]),

    # ---- form, then concentration for liquids
    ("form-tablet-vs-capsule", item("amoxicillin 500mg", form="tablet"), item("amoxicillin 500mg", form="capsule", brand="Mox 500"),
     "not_listed", "FORM_MISMATCH", []),
    ("form-syrup-vs-drops", item("paracetamol 250mg/5ml", form="oral_liquid", brand="Calpol 250"),
     item("paracetamol 100mg/ml", form="drops", brand="Crocin Drops"), "not_listed", "FORM_MISMATCH", []),
    ("form-tablet-vs-syrup", item("paracetamol 500mg", form="tablet"), item("paracetamol 250mg/5ml", form="oral_liquid"),
     "not_listed", "FORM_MISMATCH", []),
    ("conc-120-vs-250", item("paracetamol 120mg/5ml", form="oral_liquid"), item("paracetamol 250mg/5ml", form="oral_liquid", brand="Calpol 250"),
     "not_listed", "CONCENTRATION_MISMATCH", []),
    ("conc-drops-diff", item("paracetamol 100mg/ml", form="drops"), item("paracetamol 150mg/ml", form="drops"),
     "not_listed", "CONCENTRATION_MISMATCH", []),
    ("conc-combo-diff", item("amoxicillin 400mg/5ml + clavulanic acid 57mg/5ml", form="oral_liquid"),
     item("amoxicillin 200mg/5ml + clavulanic acid 28.5mg/5ml", form="oral_liquid"), "not_listed", "CONCENTRATION_MISMATCH", []),

    # ---- missing fields -> UNVERIFIED (never MATCH / GENERIC_EQUIVALENT)
    ("missing-salts-strip", item(AUG, brand="Augmentin"), item("", brand="Unknown strip"), "not_listed", "UNVERIFIED", []),
    ("missing-salts-rx", item("", brand="Augmentin 625"), item(AUG, brand="Clavam 625"), "not_listed", "UNVERIFIED", []),
    ("missing-strength", item("paracetamol"), item("paracetamol 650mg", brand="Dolo 650"), "not_listed", "UNVERIFIED", []),
    ("missing-strength-one-salt", item("amoxicillin 500mg + clavulanic acid"), item(AUG), "not_listed", "UNVERIFIED", []),
    ("missing-release", item("metformin 500mg", release=None), item("metformin 500mg", brand="Glycomet 500"),
     "not_listed", "UNVERIFIED", []),
    ("missing-form", item("metformin 500mg", form=None), item("metformin 500mg"), "not_listed", "UNVERIFIED", []),
    ("missing-concentration", item("paracetamol 250mg", form="oral_liquid"), item("paracetamol 250mg/5ml", form="oral_liquid"),
     "not_listed", "UNVERIFIED", []),
    ("banned-check-unverified-same-sku", item(AUG, brand="Augmentin 625 Duo", brand_id=1),
     item(AUG, brand="Augmentin 625 Duo", brand_id=1), "unverified", "UNVERIFIED", []),
    ("banned-check-possible-same-sku", item(FDC, form=None, brand="Saathi-Demo FDC", brand_id=39),
     item(FDC, form=None, brand="Saathi-Demo FDC", brand_id=39), "possible", "UNVERIFIED", []),
    ("banned-check-unverified-generic", item("paracetamol 650mg", brand="Dolo"), item("paracetamol 650mg", brand="Calpol"),
     "unverified", "UNVERIFIED", []),

    # ---- banned first; other differences ride along as flags
    ("banned-same", item(FDC, brand="Saathi-Demo FDC"), item(FDC, brand="Saathi-Demo FDC"), "banned", "BANNED_COMBINATION", []),
    ("banned-vs-salt-diff", item("paracetamol 650mg", brand="Dolo 650"), item(FDC, brand="Saathi-Demo FDC"),
     "banned", "BANNED_COMBINATION", ["SALT_MISMATCH"]),
    ("banned-vs-strength-diff", item("aceclofenac 100mg + paracetamol 500mg + rabeprazole 20mg"), item(FDC, brand="Saathi-Demo FDC"),
     "banned", "BANNED_COMBINATION", ["STRENGTH_MISMATCH"]),
    ("banned-vs-release-form-diff", item(FDC, form="capsule", release="SR"), item(FDC, brand="Saathi-Demo FDC"),
     "banned", "BANNED_COMBINATION", ["RELEASE_MISMATCH", "FORM_MISMATCH"]),
    ("banned-with-missing-field", item("", brand="Some Rx"), item(FDC, brand="Saathi-Demo FDC"), "banned", "BANNED_COMBINATION", []),
]

# id, item, safety, has_cheaper_alternative, expected, flags
LOOKUP_CASES: list[tuple] = [
    ("lookup-banned", item(FDC, brand="Saathi-Demo FDC"), "banned", False, "BANNED_COMBINATION", []),
    ("lookup-banned-even-with-alt", item(FDC, brand="Saathi-Demo FDC"), "banned", True, "BANNED_COMBINATION", []),
    ("lookup-banned-missing-form", item(FDC, form=None), "banned", False, "BANNED_COMBINATION", []),
    ("lookup-cheaper-alt", item(AUG, brand="Augmentin 625 Duo"), "not_listed", True, "GENERIC_EQUIVALENT", []),
    ("lookup-no-alt", item("linagliptin 5mg", brand="Trajenta 5"), "not_listed", False, "MATCH", ["NO_COMPARISON"]),
    ("lookup-no-alt-release-unknown", item("cefixime 200mg", release=None, brand="Zifi 200"), "not_listed", False, "MATCH",
     ["NO_COMPARISON"]),
    ("lookup-unverified-with-alt", item("paracetamol 650mg", brand="Dolo 650"), "unverified", True, "UNVERIFIED", []),
    ("lookup-possible", item(FDC, form=None), "possible", False, "UNVERIFIED", []),
    ("lookup-missing-salts", item("", brand="Mystery 10"), "not_listed", False, "UNVERIFIED", []),
    ("lookup-missing-strength", item("paracetamol", brand="Crocin"), "not_listed", True, "UNVERIFIED", []),
    ("lookup-missing-form", item("paracetamol 650mg", form=None), "not_listed", True, "UNVERIFIED", []),
]

# id, item, store variant, expected status. Store variants are built in tests/test_rules.py:
#   fixture   - the dev fixture DB (form-specific FDC entry, list dated 2026-11-12)
#   empty     - banned list not loaded
#   stale     - list as-of 2020-01-01
#   strengths - the FDC entry restricted to 100 + 325 + 20 mg
#   formless  - the FDC entry without a dosage form
BANNED_CASES: list[tuple] = [
    ("fdc-listed", item(FDC), "fixture", "banned"),
    ("fdc-order-free", item("rabeprazole 20mg + aceclofenac 100mg + paracetamol 325mg"), "fixture", "banned"),
    ("fdc-synonyms-and-salt-forms", item("Aceclofenac 100mg + Acetaminophen 325mg + Rabeprazole Sodium 20mg"), "fixture", "banned"),
    ("fdc-no-strengths", item("aceclofenac + paracetamol + rabeprazole"), "fixture", "banned"),
    ("single-salt", item("paracetamol 650mg"), "fixture", "not_listed"),
    ("two-of-three", item("aceclofenac 100mg + paracetamol 325mg"), "fixture", "not_listed"),
    ("superset", item(FDC + " + caffeine 30mg"), "fixture", "not_listed"),
    ("form-unknown", item(FDC, form=None), "fixture", "possible"),
    ("form-differs", item(FDC, form="capsule"), "fixture", "not_listed"),
    ("no-salts", item(""), "fixture", "unverified"),
    ("composition-uncertain", item(FDC, uncertain=True), "fixture", "unverified"),  # low-confidence OCR read
    ("empty-list", item(FDC), "empty", "unverified"),
    ("empty-list-single-salt", item("paracetamol 650mg"), "empty", "unverified"),
    ("stale-list", item(FDC), "stale", "unverified"),
    ("stale-list-not-listed-combo", item(AUG), "stale", "unverified"),
    ("strengths-match", item(FDC), "strengths", "banned"),
    ("strengths-differ", item("aceclofenac 100mg + paracetamol 500mg + rabeprazole 20mg"), "strengths", "not_listed"),
    ("strengths-unknown", item("aceclofenac 100mg + paracetamol + rabeprazole 20mg"), "strengths", "possible"),
    ("strengths-units", item("aceclofenac 0.1g + paracetamol 325mg + rabeprazole 20000mcg"), "strengths", "banned"),
    ("formless-entry-any-form", item(FDC, form="capsule"), "formless", "banned"),
    ("formless-entry-form-unknown", item(FDC, form=None), "formless", "banned"),
]
