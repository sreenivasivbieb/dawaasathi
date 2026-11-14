"""Banned fixed-dose-combination check and (stretch) not-of-standard-quality batch alerts.

Matching is on the order-free canonical salt set + dosage form, never on brand name.
Unknown salts, an empty or stale list, or an ambiguous form match -> UNVERIFIED, never "safe".
"""

from __future__ import annotations

import re

from app.db import Store
from app.models import DrugItem, SafetyResult


def list_meta(store: Store) -> tuple[str | None, str | None]:
    meta = store.dataset("banned_fdc") or {}
    as_of = meta.get("as_of")
    if not as_of and store.banned:
        as_of = max((b.notification_date or "") for b in store.banned) or None
    return as_of, meta.get("source")


def check_banned(store: Store, item: DrugItem, stale_days: int) -> SafetyResult:
    as_of, source = list_meta(store)
    if not store.banned:
        return SafetyResult(banned=None, status="unverified", list_as_of=as_of, source=source,
                            reason="banned_list_not_loaded")
    if not item.salts:
        return SafetyResult(banned=None, status="unverified", list_as_of=as_of, source=source,
                            reason="salts_unknown")
    if item.composition_uncertain:
        return SafetyResult(banned=None, status="unverified", list_as_of=as_of, source=source,
                            reason="composition_uncertain")
    if store.is_stale(as_of, stale_days):
        return SafetyResult(banned=None, status="unverified", list_as_of=as_of, source=source,
                            reason="banned_list_stale")
    names = {s.name for s in item.salts}
    if len(names) < 2:
        return SafetyResult(banned=False, status="not_listed", list_as_of=as_of, source=source,
                            reason="single_ingredient")
    possible = None
    for rec in store.banned:
        if set(rec.salts) != names:
            continue
        if rec.form and item.form is None:
            possible = rec
            continue
        if rec.form and item.form and rec.form != item.form:
            continue
        if rec.strengths:
            if not _strengths_match(rec.strengths, item):
                if any(s.amount is None for s in item.salts):
                    possible = rec
                continue
        return SafetyResult(
            banned=True, status="banned", list_as_of=as_of, source=source,
            notification_ref=rec.notification_ref, notification_date=rec.notification_date,
            matched_salts=sorted(rec.salts),
        )
    if possible is not None:
        return SafetyResult(
            banned=None, status="possible", list_as_of=as_of, source=source,
            notification_ref=possible.notification_ref, notification_date=possible.notification_date,
            matched_salts=sorted(possible.salts), reason="form_or_strength_unknown",
        )
    return SafetyResult(banned=False, status="not_listed", list_as_of=as_of, source=source)


def _strengths_match(expected: dict[str, float], item: DrugItem) -> bool:
    have = {s.name: s.comparable() for s in item.salts}
    for name, mg in expected.items():
        c = have.get(name)
        if c is None or abs(c[0] - float(mg)) > 1e-6:
            return False
    return True


def check_batch(store: Store, item: DrugItem) -> dict | None:
    """Stretch: OCR'd batch number against not-of-standard-quality alerts."""
    if not item.batch_no or not store.nsq:
        return None
    batch = re.sub(r"[^A-Z0-9]", "", item.batch_no.upper())
    for alert in store.nsq:
        if re.sub(r"[^A-Z0-9]", "", (alert.get("batch_no") or "").upper()) != batch:
            continue
        drug = (alert.get("drug") or "").lower()
        if item.salts and not any(s.name in drug for s in item.salts) and (
            not item.brand or item.brand.lower().split()[0] not in drug
        ):
            continue
        return alert
    return None
