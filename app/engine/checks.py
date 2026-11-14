"""Soft checks: symptom vs usual use, duplicate salt vs the home pouch, look-alike names."""

from __future__ import annotations

from app.db import Store
from app.engine.salts import canonical_salt
from app.models import DrugItem, Flag


def usual_uses(store: Store, item: DrugItem) -> list[str] | None:
    """Usual-use tags of the item's salts from our data; None unless EVERY salt has an entry.
    A combination is used for what the combination treats: Sinarest's paracetamol alone says
    "fever / pain", which would wrongly answer "not for a cold"."""
    uses: list[str] = []
    for s in item.salts:
        tags = store.salt_use.get(s.name)
        if not tags:
            return None
        uses += [t.lower() for t in tags if t.lower() not in uses]
    return uses or None


def symptom_check(store: Store, item: DrugItem, symptom: str | None) -> Flag | None:
    if not symptom or not item.salts:
        return None
    uses = usual_uses(store, item)
    if uses is None:
        # Asked "is it right for X?" but the salt's usual use is not in our data: say so, never guess.
        return Flag(code="SYMPTOM_UNKNOWN", detail="usual use of this salt is not in the data",
                    facts={"symptom": symptom})
    return symptom_flag(symptom, uses)


def symptom_flag(symptom: str, uses: list[str], web: dict | None = None) -> Flag:
    """SYMPTOM_OK / SYMPTOM_MISMATCH; with web provenance (source, url) the *_WEB variants, whose
    reply names the source."""
    sfx = "_WEB" if web else ""
    extra = dict(web or {})
    if symptom in uses:
        return Flag(code="SYMPTOM_OK" + sfx, detail="stated symptom matches usual use",
                    facts={"symptom": symptom, "usual_use": [symptom], **extra})
    return Flag(code="SYMPTOM_MISMATCH" + sfx, detail="usual use differs from stated symptom",
                facts={"symptom": symptom, "usual_use": sorted(uses) if not web else uses, **extra})


def use_flag(uses: list[str] | None, web: dict | None = None) -> Flag:
    """Answer to "what is this medicine for?"."""
    if not uses:
        return Flag(code="USE_UNKNOWN", detail="usual use not found")
    return Flag(code="USE_INFO_WEB" if web else "USE_INFO", detail="usual use",
                facts={"usual_use": uses, **dict(web or {})})


def dup_salt(item: DrugItem, pouch: list[str]) -> Flag | None:
    if not pouch or not item.salts:
        return None
    have = {canonical_salt(p) for p in pouch if p and p.strip()}
    dupes = sorted({s.name for s in item.salts} & have)
    if not dupes:
        return None
    return Flag(code="DUPLICATE_SALT", detail="same salt already in the home pouch",
                facts={"salts": dupes})


def look_alike(store: Store, item: DrugItem) -> Flag | None:
    keys = [s.name for s in item.salts]
    if item.brand:
        keys = [item.brand.lower().split()[0], item.brand.lower()] + keys
    others: set[str] = set()
    for k in keys:
        others |= store.lasa.get(k, set())
    if not others:
        return None
    return Flag(code="LOOK_ALIKE", detail="name looks/sounds like another drug",
                facts={"brand": item.brand, "similar_to": sorted(o.title() for o in others)})
