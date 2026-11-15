"""Per-language templates, the zero-hallucination guard and the speakable-text verbaliser."""

from __future__ import annotations

import re

import pytest
from helpers import doctor_line

from app.dialogue import guard
from app.dialogue.templates import MAX_WORDS, MISMATCH_CODES, RenderContext, load, render
from app.dialogue.verbalize import speakable
from app.engine import alternatives as altmod, checks, price as pricemod, rules, safety as safetymod
from app.models import Candidate, DrugItem, Flag, SafetyResult, SaltDose

from app.config import TEMPLATE_LANGS as LANGS  # every language with reply templates
VERDICT_CODES = ("BANNED_COMBINATION", "MATCH", "GENERIC_EQUIVALENT", "SALT_MISMATCH", "STRENGTH_MISMATCH",
                 "RELEASE_MISMATCH", "FORM_MISMATCH", "CONCENTRATION_MISMATCH", "UNVERIFIED")
FORBIDDEN = ("stop taking", "बंद कर", "fraud", "धोखा", "नकली", "fake", "बंद करा", "थांबवा")
GST, STALE = 12.0, 400


# ---------------------------------------------------------------- building realistic contexts
def _engine(store, target: DrugItem, prescribed: DrugItem | None, extra_flags=()):
    safety = safetymod.check_banned(store, target, STALE)
    alts = [] if safety.status == "banned" else altmod.cheaper(store, target, 3, STALE)
    price = pricemod.price_gap(store, target, GST, STALE)
    verdict = rules.compare(prescribed, target, safety) if prescribed else rules.lookup(target, safety, bool(alts))
    flags = list(extra_flags)
    if safety.status == "possible":
        flags.append(Flag(code="POSSIBLE_BANNED", facts={"notification_ref": safety.notification_ref}))
    return safety, alts, price, verdict, flags


def build_ctx(store, brand_ids, code: str, lang: str, mode: str, intent: str = "FIND_GENERIC",
              warnings: bool = True) -> tuple:
    def b(name, source="strip"):
        return store.brand_item(brand_ids[name], source)

    prescribed = None
    extra: list[Flag] = []
    if code == "BANNED_COMBINATION":
        target = b("Saathi-Demo FDC Tablet")
        if mode == "compare":
            prescribed = b("Zerodol-P Tablet", "rx")
    elif code == "MATCH":
        if mode == "compare":
            target, prescribed = b("Augmentin 625 Duo Tablet"), b("Augmentin 625 Duo Tablet", "rx")
        else:
            target = b("Trajenta 5 Tablet", "voice")
    elif code == "GENERIC_EQUIVALENT":
        target = b("Augmentin 625 Duo Tablet")
        if mode == "compare":
            target, prescribed = b("Moxikind-CV 625 Tablet"), b("Augmentin 625 Duo Tablet", "rx")
        extra = [Flag(code="DUPLICATE_SALT", facts={"salts": ["amoxicillin"]})]
    elif code == "SALT_MISMATCH":
        target, prescribed = b("Linospan 600 Tablet"), b("Trajenta 5 Tablet", "rx")
        extra = [Flag(code="LOOK_ALIKE", facts={"brand": "Trajenta 5 Tablet", "similar_to": ["Linezolid", "Linospan"]})]
    elif code == "STRENGTH_MISMATCH":
        target, prescribed = b("Augmentin 375 Tablet"), b("Augmentin 625 Duo Tablet", "rx")
    elif code == "RELEASE_MISMATCH":
        target, prescribed = b("Glycomet SR 500 Tablet"), b("Glycomet 500 Tablet", "rx")
    elif code == "FORM_MISMATCH":
        target, prescribed = b("Crocin Drops"), b("Calpol 250 Suspension", "rx")
    elif code == "CONCENTRATION_MISMATCH":
        target, prescribed = b("Calpol 250 Suspension"), b("Paracetamol Paediatric Suspension 120", "rx")
    elif code == "UNVERIFIED":
        if mode == "compare":
            target = b("Dolo 650 Tablet")
            prescribed = DrugItem(source="rx", brand="Dolo", salts=[SaltDose(name="paracetamol")], form="tablet")
        else:
            target = b("Saathi-Demo FDC Tablet").model_copy(update={"form": None})  # banned check -> possible
    else:  # pragma: no cover
        raise KeyError(code)

    safety, alts, price, verdict, flags = _engine(store, target, prescribed, extra if warnings else [])
    assert verdict.code == code, (code, mode, verdict.code)
    ctx = RenderContext(
        lang=lang, status="unverified" if code == "UNVERIFIED" else "ok", intent=intent, verdict=verdict,
        item=target, prescribed=prescribed, dispensed=target if prescribed else None, safety=safety, price=price,
        alternatives=alts, flags=[f for f in flags if f.code not in {x.code for x in verdict.flags}] + verdict.flags,
        directions="1-0-1 after food" if intent == "DOSAGE_QUERY" else None,
    )
    sources = guard_sources(ctx, flags + verdict.flags)
    return ctx, sources


def guard_sources(ctx: RenderContext, flags) -> list:
    """The same fact sources Pipeline._respond hands to the guard."""
    s = ctx.safety
    return [ctx.verdict.model_dump() if ctx.verdict else None, s.model_dump() if s else None,
            ctx.price.model_dump() if ctx.price else None, [a.model_dump() for a in ctx.alternatives],
            [f.model_dump() for f in flags], [c.model_dump() for c in ctx.candidates],
            {"directions": ctx.directions, "list_date": (s.notification_date or s.list_as_of) if s else None},
            ctx.item.model_dump() if ctx.item else None,
            ctx.prescribed.model_dump() if ctx.prescribed else None]


def _modes(code: str) -> list[str]:
    if code in MISMATCH_CODES:
        return ["compare"]
    return ["lookup", "compare"]


CASES = [(code, mode, lang) for code in VERDICT_CODES for mode in _modes(code) for lang in LANGS]


def _words(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------- every verdict x language
@pytest.mark.parametrize(("code", "mode", "lang"), CASES, ids=[f"{c}-{m}-{lng}" for c, m, lng in CASES])
def test_verdict_script(store, brand_ids, code, mode, lang):
    ctx, sources = build_ctx(store, brand_ids, code, lang, mode)
    r = render(ctx)
    text = r.text
    assert text.endswith(doctor_line(lang)), text
    assert _words(text) <= MAX_WORDS, (_words(text), text)
    low = (text + " " + r.chemist_text).lower()
    for bad in FORBIDDEN:
        assert bad not in low, (bad, text, r.chemist_text)
    g = guard.validate(text, *sources)
    assert g.ok, (g, text)
    assert r.chemist_text.strip(), "Speak-to-Chemist text must exist"
    assert guard.validate(r.chemist_text, *sources).ok, r.chemist_text
    # The slot-free fallback is also complete and ends with the doctor line.
    plain = " ".join(s.text for s in r.plain_segments)
    assert plain.endswith(doctor_line(lang))
    assert guard.validate(plain, *sources).ok
    # Speech text: no digits at all in Hindi / Marathi.
    speech = " ".join(speakable(s.text, lang) for s in r.segments)
    if lang in ("hi", "mr"):
        assert not re.search(r"[0-9०-९]", speech), speech
    assert speech.endswith(speakable(doctor_line(lang), lang))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("code", sorted(MISMATCH_CODES | {"BANNED_COMBINATION"}))
def test_chemist_text_for_mismatch_and_banned(store, brand_ids, code, lang):
    ctx, sources = build_ctx(store, brand_ids, code, lang, "compare")
    r = render(ctx)
    chem = load(lang)["chemist"][code]
    # The code-specific chemist template was used (its fixed opening text is present).
    opening = chem.split("{")[0].strip()
    assert r.chemist_text.startswith(opening)
    assert "{" not in r.chemist_text and "}" not in r.chemist_text


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("intent", ["FIND_GENERIC", "PRICE_CHECK", "DOSAGE_QUERY", "VERIFY_MATCH", "CLARIFY"])
def test_every_intent_keeps_budget_and_guard(store, brand_ids, lang, intent):
    for code, mode in [("GENERIC_EQUIVALENT", "lookup"), ("BANNED_COMBINATION", "lookup"), ("MATCH", "lookup"),
                       ("RELEASE_MISMATCH", "compare")]:
        ctx, sources = build_ctx(store, brand_ids, code, lang, mode, intent=intent)
        r = render(ctx)
        assert r.text.endswith(doctor_line(lang))
        assert _words(r.text) <= MAX_WORDS, r.text
        assert guard.validate(r.text, *sources).ok, r.text


def test_banned_script_names_salts_and_list_date(store, brand_ids):
    ctx, _ = build_ctx(store, brand_ids, "BANNED_COMBINATION", "en", "lookup")
    text = render(ctx).text
    assert "Aceclofenac + Paracetamol + Rabeprazole" in text
    assert "1 January 2026" in text
    assert "cheapest" not in text.lower()  # no alternatives for a banned combination


def test_generic_equivalent_lookup_mentions_jan_aushadhi_and_savings(store, brand_ids):
    ctx, _ = build_ctx(store, brand_ids, "GENERIC_EQUIVALENT", "en", "lookup", warnings=False)
    text = render(ctx).text
    assert "Jan Aushadhi Amoxycillin + Clavulanic Acid 625" in text
    assert "72 percent" in text


def test_warnings_outrank_price_detail_within_budget(store, brand_ids):
    ctx, sources = build_ctx(store, brand_ids, "GENERIC_EQUIVALENT", "en", "lookup")  # + DUPLICATE_SALT
    text = render(ctx).text
    assert "already in your home medicines" in text
    assert _words(text) <= MAX_WORDS and guard.validate(text, *sources).ok


def test_price_check_lines(store, brand_ids):
    ctx, sources = build_ctx(store, brand_ids, "GENERIC_EQUIVALENT", "en", "lookup", intent="PRICE_CHECK",
                             warnings=False)
    text = render(ctx).text
    assert "This costs 223.40 rupees for 10 tablets; at Jan Aushadhi, 10 tablets cost 62 rupees." in text
    assert "That is about 3.6 times." in text
    # Augmentin is above the (GST-inclusive) NPPA ceiling in the fixture; that clause is only left out
    # when it does not fit the word budget (detail clauses are added in order: gap, ratio, ceiling).
    nppa = load("en")["clauses"]["above_ceiling"]
    if nppa not in text:
        assert _words(text) + _words(nppa) > MAX_WORDS
    assert ctx.price.above_ceiling is True
    assert guard.validate(text, *sources).ok


@pytest.mark.parametrize("lang", LANGS)
def test_stale_banned_list_keeps_known_composition_and_primary_prices(store, brand_ids, lang):
    from app.models import PriceInfo, Verdict

    item = store.brand_item(brand_ids["Dolo 650 Tablet"])
    safety = SafetyResult(status="unverified", banned=None, list_as_of="2021-11-22", reason="banned_list_stale")
    ctx = RenderContext(lang=lang, status="unverified", intent="PRICE_CHECK", item=item, safety=safety,
                        verdict=Verdict(code="UNVERIFIED", mode="lookup", facts={"banned_check": "unverified"}),
                        price=PriceInfo(mrp=34.27, pack_qty=15, reference=15.47, reference_pack_qty=15, ratio=2.2))
    result = render(ctx)
    assert "650" in result.text and "Paracetamol" in result.text
    assert "34.27" in result.text and "15.47" in result.text
    assert load(lang)["verdict"]["UNVERIFIED_BANNED_CHECK"] in result.text
    assert load(lang)["verdict"]["UNVERIFIED_LOOKUP"].format(brand="Dolo 650") not in result.text
    assert "Paracetamol" in result.chemist_text
    assert guard.validate(result.text, *guard_sources(ctx, [])).ok


def test_release_mismatch_names_both_release_types(store, brand_ids):
    ctx, _ = build_ctx(store, brand_ids, "RELEASE_MISMATCH", "en", "compare")
    text = render(ctx).text
    assert "regular (IR)" in text and "sustained-release (SR)" in text


def test_strength_mismatch_uses_per_salt_strengths(store, brand_ids):
    ctx, _ = build_ctx(store, brand_ids, "STRENGTH_MISMATCH", "en", "compare")
    text = render(ctx).text
    assert "Amoxicillin 500 mg" in text and "Amoxicillin 250 mg" in text
    assert "625" not in text and "375" not in text


def test_symptom_and_dosage_details(store, brand_ids):
    item = store.brand_item(brand_ids["Augmentin 625 Duo Tablet"], "voice")
    safety, alts, price, verdict, flags = _engine(store, item, None, [checks.symptom_check(store, item, "fever")])
    ctx = RenderContext(lang="hi", status="ok", intent="FIND_GENERIC", verdict=verdict, item=item, safety=safety,
                        price=price, alternatives=alts, flags=flags, skip_detail=True)
    text = render(ctx).text
    assert "इन्फेक्शन" in text and "बुखार" in text
    assert "सस्ता विकल्प" not in text  # skip_detail: the symptom follow-up only answers the symptom
    ctx = RenderContext(lang="en", status="ok", intent="DOSAGE_QUERY", verdict=verdict, item=item, safety=safety,
                        price=price, alternatives=alts, directions="1-0-1 x 5 days")
    text = render(ctx).text
    assert "The prescription says: 1-0-1 x 5 days." in text


# ---------------------------------------------------------------- non-verdict statuses
@pytest.mark.parametrize("lang", LANGS)
def test_status_scripts_end_with_doctor_line(lang):
    cands = [Candidate(candidate_id="c0_b1", brand_id=1, brand="Augmentin 625 Duo Tablet", label="Augmentin 625 Duo",
                       confidence=0.9),
             Candidate(candidate_id="c1_b2", brand_id=2, brand="Augmentin 375 Tablet", label="Augmentin 375",
                       confidence=0.9)]
    contexts = [
        RenderContext(lang=lang, status="error", error_kind="stt"),
        RenderContext(lang=lang, status="error"),
        RenderContext(lang=lang, status="needs_image"),
        RenderContext(lang=lang, status="needs_image", needs_image_kind="verify"),
        RenderContext(lang=lang, status="needs_confirmation", candidates=cands),
        RenderContext(lang=lang, status="needs_confirmation", candidates=cands[:1]),
        RenderContext(lang=lang, status="needs_confirmation", candidates=[], rx_count=2),
        RenderContext(lang=lang, status="needs_confirmation", needs_image_kind="none_of_these"),
    ]
    for ctx in contexts:
        r = render(ctx)
        assert r.text.endswith(doctor_line(lang)), r.text
        assert _words(r.text) <= MAX_WORDS
        assert "{" not in r.text
        # The rx-count question speaks the number of prescription lines, so that count is a fact.
        assert guard.validate(r.text, [c.model_dump() for c in ctx.candidates], {"rx_count": ctx.rx_count}).ok, r.text
    two = render(contexts[4]).text
    assert "Augmentin 625 Duo" in two and "Augmentin 375" in two


def test_unknown_language_falls_back_to_english(store, brand_ids):
    ctx, _ = build_ctx(store, brand_ids, "MATCH", "en", "lookup")
    ctx.lang = "xx"  # a code with no templates
    assert render(ctx).text.endswith(doctor_line("en"))


def test_templates_have_same_keys_and_no_unsafe_words():
    base = load("en")
    for lang in LANGS:
        t = load(lang)
        for section in ("lines", "verdict", "verdict_plain", "clauses", "chemist", "words"):
            assert set(t[section]) == set(base[section]), (lang, section, set(base[section]) ^ set(t[section]))
        blob = str(t).lower()
        for bad in FORBIDDEN:
            assert bad not in blob, (lang, bad)


# ---------------------------------------------------------------- guard
def test_guard_rejects_unknown_number():
    facts = {"salts": ["Paracetamol 650 mg"], "mrp": 33.6, "pack_qty": 15}
    assert guard.validate("Paracetamol 650 mg, 15 tablets for 33.60 rupees.", facts).ok
    res = guard.validate("This medicine is Paracetamol 999 mg.", facts)
    assert not res.ok and res.bad_numbers == ["999"]


def test_guard_rejects_unknown_salt():
    facts = {"salts": ["Paracetamol 650 mg"]}
    res = guard.validate("This contains Ibuprofen.", facts)
    assert not res.ok and res.bad_salts == ["ibuprofen"]
    assert guard.validate("This contains paracetamol.", facts).ok
    assert guard.validate("Same as acetaminophen.", facts).ok  # synonym of an allowed salt


def test_guard_accepts_numbers_from_nested_models():
    s = SafetyResult(banned=True, status="banned", notification_date="2026-01-01")
    assert guard.validate("List dated 1 January 2026.", s).ok
    assert not guard.validate("List dated 2 January 2026.", s).ok


def test_guard_ignores_words_inside_other_words():
    # "pan" inside "pantoprazole" is not a separate salt mention; numbers inside codes are checked.
    facts = {"salts": ["Pantoprazole 40 mg"]}
    assert guard.validate("Pantoprazole 40 mg", facts).ok


# ---------------------------------------------------------------- verbalize
def test_speakable_money_hi():
    assert speakable("62.50 रुपये", "hi") == "बासठ रुपये पचास पैसे"
    assert speakable("223.40 रुपये", "hi") == "दो सौ तेईस रुपये चालीस पैसे"
    assert speakable("12 रुपये", "hi") == "बारह रुपये"


def test_speakable_money_en_mr():
    assert speakable("62.50 rupees", "en") == "sixty-two rupees fifty paise"
    assert "पैसे" in speakable("62.50 रुपये", "mr")


def test_speakable_units():
    assert speakable("250 mg/5 ml", "hi") == "दो सौ पचास मिलीग्राम प्रति पांच एमएल"
    assert speakable("250 mg/5 ml", "en") == "two hundred fifty milligrams per five ml"
    assert speakable("100 mg/ml", "en") == "one hundred milligrams per ml"
    assert speakable("50 mcg", "hi") == "पचास माइक्रोग्राम"
    assert speakable("Amoxicillin 500 mg + Clavulanic Acid 125 mg", "en") == (
        "Amoxicillin five hundred milligrams and Clavulanic Acid one hundred twenty-five milligrams")
    assert speakable("72%", "en") == "seventy-two percent"


def test_speakable_codes_are_read_digit_by_digit():
    assert speakable("Batch DEMO123 has an alert.", "en") == "Batch DEMO one two three has an alert."
    assert speakable("इस बैच DEMO123 पर", "hi") == "इस बैच DEMO एक दो तीन पर"


@pytest.mark.parametrize("lang", ["hi", "mr"])
def test_speakable_removes_all_digits(lang):
    text = ("इसका दाम 15 गोलियाँ के 33.60 रुपये है, और जन औषधि में 10 गोलियाँ के 12 रुपये। Paracetamol 250 mg/5 ml, "
            "Levothyroxine 50 mcg, 0.5 g, 72%, सूची की तारीख 1 जनवरी 2026, बैच DEMO123, 1-0-1")
    out = speakable(text, lang)
    assert not re.search(r"[0-9०-९]", out), out
