"""Soft checks: symptom vs usual use, duplicate salt vs the home pouch, look-alike names."""

from __future__ import annotations

from app.db import Store
from app.engine.salts import canonical_salt
from app.models import DrugItem, Flag


def symptom_check(store: Store, item: DrugItem, symptom: str | None) -> Flag | None:
    if not symptom or not item.salts:
        return None
    uses: set[str] = set()
    known = False
    for s in item.salts:
        tags = store.salt_use.get(s.name)
        if tags:
            known = True
            uses.update(t.lower() for t in tags)
    if not known:
        return None
    if symptom in uses:
        return Flag(code="SYMPTOM_OK", detail="stated symptom matches usual use",
                    facts={"symptom": symptom, "usual_use": [symptom]})
    return Flag(code="SYMPTOM_MISMATCH", detail="usual use differs from stated symptom",
                facts={"symptom": symptom, "usual_use": sorted(uses)})


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
