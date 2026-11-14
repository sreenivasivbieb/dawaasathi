"""Salt names, dosage forms and release classes: parsing and canonicalisation.

Composition strings come in many shapes, e.g.
  "Amoxycillin (500mg) + Clavulanic Acid (125mg)"
  "Amoxicillin 200mg/5ml + Clavulanic acid 28.5mg/5ml"
  "Paracetamol 650 mg", "Cholecalciferol (60000 IU)", "Levothyroxine 50mcg"
"""

from __future__ import annotations

import re

from app.models import SaltDose

# Synonym -> canonical name. Extended at runtime from the `salts` table.
_SYNONYMS: dict[str, str] = {
    "amoxycillin": "amoxicillin",
    "amoxicilin": "amoxicillin",
    "potassium clavulanate": "clavulanic acid",
    "clavulanate potassium": "clavulanic acid",
    "clavulanate": "clavulanic acid",
    "acetaminophen": "paracetamol",
    "pcm": "paracetamol",
    "salbutamol": "salbutamol",
    "albuterol": "salbutamol",
    "vitamin d3": "cholecalciferol",
    "vit d3": "cholecalciferol",
    "levocetrizine": "levocetirizine",
    "cetrizine": "cetirizine",
    "ranitidine": "ranitidine",
    "glimiperide": "glimepiride",
    "rabeprazole sodium": "rabeprazole",
    "pantoprazole sodium": "pantoprazole",
    "thyroxine": "levothyroxine",
    "l-thyroxine": "levothyroxine",
    "chlorphenamine": "chlorpheniramine",
}

_SALT_SUFFIXES = (
    "hydrochloride", "dihydrochloride", "hydrobromide", "oxalate", "hcl", "trihydrate", "dihydrate", "monohydrate", "sesquihydrate",
    "maleate", "besylate", "besilate", "sulphate", "sulfate", "phosphate", "citrate",
    "succinate", "tartrate", "mesylate", "fumarate", "bromide", "acetate", "magnesium",
    "calcium", "sodium", "potassium", "dried", "diluted", "anhydrous", "micronised",
    "micronized", "ip", "bp", "usp", "eq", "equivalent", "to",
)
_SALT_PREFIX_NOISE = re.compile(r"^(each|contains|of|eq\.?|equivalent to|as)\s+")

_KNOWN: set[str] = set(_SYNONYMS.values())
for _canon in list(_KNOWN):
    _SYNONYMS.setdefault(_canon, _canon)
_EQ_TO = re.compile(r"\b(?:eq\.?|equiv\.?|equivalent)\s*(?:to)?\b", re.I)


def register_salts(rows: list[tuple[str, list[str]]]) -> None:
    """Load canonical names + synonyms from the database."""
    for name, synonyms in rows:
        canon = name.strip().lower()
        _KNOWN.add(canon)
        _SYNONYMS.setdefault(canon, canon)
        for syn in synonyms:
            syn = syn.strip().lower()
            if syn:
                _SYNONYMS[syn] = canon


def known_salts() -> set[str]:
    return set(_KNOWN)


def all_salt_surface_forms() -> dict[str, str]:
    return dict(_SYNONYMS)


def canonical_salt(name: str) -> str:
    s = name.lower().strip()
    pieces = _EQ_TO.split(s)
    if len(pieces) > 1 and pieces[-1].strip(" .:"):
        s = pieces[-1]
    s = re.sub(r"[()\[\],.:]", " ", s)
    s = re.sub(r"\s+-+\s*|\s*-+\s*$", " ", s)  # "metformin hydrochloride - " (keep "n-acetyl")
    s = _SALT_PREFIX_NOISE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in _SYNONYMS:
        return _SYNONYMS[s]
    words = s.split()
    # Strip pharmacopoeia / salt-form suffix words from the end (keep at least one word).
    while len(words) > 1 and words[-1] in _SALT_SUFFIXES:
        words.pop()
    s = " ".join(words)
    if s in _SYNONYMS:
        return _SYNONYMS[s]
    # "X eq to Y" style: prefer the trailing moiety if it is known.
    if " " in s:
        for i in range(1, len(words)):
            tail = " ".join(words[i:])
            if tail in _SYNONYMS:
                return _SYNONYMS[tail]
    return s


_UNIT_RE = r"(mg|mcg|µg|ug|g|gm|iu|i\.u\.|%|ml)"
_DOSE_RE = re.compile(
    rf"(?P<amt>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_RE})"
    r"(?:\s*(?:/|per|in|each)?\s*(?P<per>\d+(?:\.\d+)?)?\s*ml\b)?",
    re.I,
)


def _unit(u: str) -> str:
    u = u.lower().replace(".", "")
    return {"gm": "g", "µg": "mcg", "ug": "mcg", "iu": "iu"}.get(u, u)


def parse_composition(text: str | None) -> list[SaltDose]:
    """Split a composition string into SaltDose entries."""
    if not text:
        return []
    parts = re.split(r"\s*\+\s*|\s*;\s*|\s+and\s+|\s*,\s*(?=[A-Za-z])", text)
    salts: list[SaltDose] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _DOSE_RE.search(part)
        if m:
            name = part[: m.start()]
            amount = float(m.group("amt"))
            unit = _unit(m.group("unit"))
            per = m.group("per")
            per_ml = float(per) if per else (1.0 if re.search(r"/\s*ml", m.group(0), re.I) else None)
            if unit == "ml":
                continue
        else:
            name, amount, unit, per_ml = part, None, None, None
        name = canonical_salt(name)
        if not name or re.fullmatch(r"[\d\s.]+", name):
            continue
        salts.append(SaltDose(name=name, amount=amount, unit=unit, per_ml=per_ml))
    return salts


def salt_key(salts: list[SaltDose]) -> str:
    return "+".join(sorted({s.name for s in salts}))


def strength_key(salts: list[SaltDose], form: str | None) -> str | None:
    """Strengths aligned with salt_key order; concentration for liquids. None if any is missing."""
    parts = []
    for s in sorted(salts, key=lambda s: s.name):
        if form in ("oral_liquid", "drops"):
            c = s.concentration()
        else:
            c = s.comparable()
        if c is None:
            return None
        parts.append(f"{c[0]:g}{c[1]}")
    return "+".join(parts) if parts else None


# ------------------------------------------------------------------ dosage forms

_FORM_WORDS = [
    (r"eye\s*drop|ophthalmic", "eye_drops"),
    (r"ear\s*drop|otic", "ear_drops"),
    (r"nasal\s*spray|nasal\s*drop", "nasal"),
    (r"oral\s*drop|paediatric\s*drop|pediatric\s*drop|\bdrops?\b", "drops"),
    (r"dry\s*syrup|suspension|syrup|\bsyp\b|\bsyr\b|oral\s*solution|elixir|liquid|linctus", "oral_liquid"),
    (r"injection|vial|ampoule|infusion|\binj\b", "injection"),
    (r"inhaler|rotacap|respule|nebul", "inhaler"),
    (r"capsule|\bcap\b|\bcaps\b", "capsule"),
    (r"tablet|\btab\b|\btabs\b|caplet", "tablet"),
    (r"cream", "cream"),
    (r"ointment", "ointment"),
    (r"\bgel\b", "gel"),
    (r"sachet|powder|granules", "powder"),
    (r"lotion", "lotion"),
    (r"soap|shampoo", "topical_wash"),
]
_FORM_COMPILED = [(re.compile(p, re.I), f) for p, f in _FORM_WORDS]

FORM_LABELS = {
    "tablet": {"hi": "गोली", "en": "tablet", "mr": "गोळी"},
    "capsule": {"hi": "कैप्सूल", "en": "capsule", "mr": "कॅप्सूल"},
    "oral_liquid": {"hi": "सिरप", "en": "syrup", "mr": "सिरप"},
    "drops": {"hi": "ड्रॉप्स", "en": "drops", "mr": "ड्रॉप्स"},
    "injection": {"hi": "इंजेक्शन", "en": "injection", "mr": "इंजेक्शन"},
    "inhaler": {"hi": "इनहेलर", "en": "inhaler", "mr": "इनहेलर"},
    "cream": {"hi": "क्रीम", "en": "cream", "mr": "क्रीम"},
    "ointment": {"hi": "मरहम", "en": "ointment", "mr": "मलम"},
    "gel": {"hi": "जेल", "en": "gel", "mr": "जेल"},
    "powder": {"hi": "पाउडर", "en": "powder", "mr": "पावडर"},
    "eye_drops": {"hi": "आँख की ड्रॉप्स", "en": "eye drops", "mr": "डोळ्याचे ड्रॉप्स"},
    "ear_drops": {"hi": "कान की ड्रॉप्स", "en": "ear drops", "mr": "कानाचे ड्रॉप्स"},
    "nasal": {"hi": "नाक का स्प्रे", "en": "nasal spray", "mr": "नाकाचा स्प्रे"},
    "lotion": {"hi": "लोशन", "en": "lotion", "mr": "लोशन"},
    "topical_wash": {"hi": "साबुन/शैम्पू", "en": "wash", "mr": "साबण/शॅम्पू"},
}


def canonical_form(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    if t in FORM_LABELS:
        return t
    for pattern, form in _FORM_COMPILED:
        if pattern.search(t):
            return form
    return None


# ---------------------------------------------------------------- release classes

_RELEASE_PATTERNS = [
    (re.compile(r"\b(sr|sustained)\b", re.I), "SR"),
    (re.compile(r"\b(er|xr|xl|cr|pr|od|extended|controlled|prolonged|modified)\b", re.I), "ER"),
    (re.compile(r"\b(dr|ec|delayed|gastro[- ]?resistant|enteric)\b", re.I), "DR"),
    (re.compile(r"\b(ir|immediate)\b", re.I), "IR"),
]

RELEASE_LABELS = {
    "IR": {"hi": "सामान्य", "en": "regular (IR)", "mr": "साधी"},
    "SR": {"hi": "धीरे असर वाली (SR)", "en": "sustained-release (SR)", "mr": "हळू परिणाम (SR)"},
    "ER": {"hi": "लंबे असर वाली (ER)", "en": "extended-release (ER)", "mr": "दीर्घ परिणाम (ER)"},
    "DR": {"hi": "देर से घुलने वाली (DR)", "en": "delayed-release (DR)", "mr": "उशिरा विरघळणारी (DR)"},
}


def canonical_release(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if t.upper() in RELEASE_LABELS:
        return t.upper()
    for pattern, rel in _RELEASE_PATTERNS:
        if pattern.search(t):
            return rel
    return None


# Single-salt PPIs exist only as gastro-resistant tablets / DR capsules, but brand lists
# usually omit the label ("Pan 40 Tablet") while PMBJP writes "Gastro-resistant": same product.
GASTRO_RESISTANT_ONLY = {"pantoprazole", "rabeprazole", "esomeprazole", "omeprazole", "lansoprazole",
                         "dexlansoprazole", "ilaprazole"}


def release_class(release: str | None, salt_key: str | None = None) -> str:
    """Equivalence class for price lookup and cheaper alternatives. Indian labels use SR / ER /
    PR / XR / "modified release" interchangeably for the same once-a-day products, so SR and ER
    are one class here (the verdict engine still reports SR vs ER as RELEASE_MISMATCH).
    With salt_key, a single PPI's IR / DR labels are one class (see GASTRO_RESISTANT_ONLY)."""
    r = (release or "IR").upper()
    if salt_key in GASTRO_RESISTANT_ONLY and r in ("IR", "DR"):
        return "DR"
    return "MR" if r in ("SR", "ER") else r


def release_from_name(name: str) -> str:
    """Brand-name suffixes like 'Glycomet SR 500' or 'Telma 40 XL'. Default IR."""
    return canonical_release(name) or "IR"
