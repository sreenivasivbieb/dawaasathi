"""Price gap vs Jan Aushadhi (PMBJP) reference and NPPA ceiling.

ratio = strip MRP per unit / PMBJP price per unit, same salt, strength, form and release.
"Above NPPA ceiling" only for scheduled formulations with a listed ceiling (ceiling excludes
GST, so the comparison uses ceiling * (1 + GST)); otherwise it is only a "price gap".
"""

from __future__ import annotations

from app.db import PriceRef, Store
from app.engine.salts import salt_key, strength_key
from app.models import DrugItem, PriceInfo


def find_ref(store: Store, item: DrugItem) -> PriceRef | None:
    if not item.salts:
        return None
    sk = salt_key(item.salts)
    stk = strength_key(item.salts, item.form)
    if stk is None:
        return None
    release = item.release or "IR"
    for ref in store.price_refs:
        if ref.salt_key == sk and ref.strength_key == stk and ref.form == item.form and (
            (ref.release or "IR") == release
        ):
            return ref
    return None


def price_gap(store: Store, item: DrugItem, gst_pct: float, stale_days: int) -> PriceInfo:
    info = PriceInfo(mrp=item.mrp, pack_qty=item.pack_qty)
    ref = find_ref(store, item)
    if ref is None:
        return info
    if ref.pmbj_price is not None:
        info.reference = ref.pmbj_price
        info.reference_pack_qty = ref.pmbj_pack_qty
        info.reference_name = ref.pmbj_name
        info.source = ref.pmbj_source or "PMBJP"
        info.as_of = ref.pmbj_as_of
        info.stale = store.is_stale(ref.pmbj_as_of, stale_days)
        unit_mrp = item.unit_price()
        if unit_mrp is not None and ref.pmbj_pack_qty and not info.stale:
            unit_ref = ref.pmbj_price / ref.pmbj_pack_qty
            if unit_ref > 0:
                info.ratio = round(unit_mrp / unit_ref, 1)
    if ref.nppa_ceiling is not None and not store.is_stale(ref.nppa_as_of, stale_days):
        info.nppa_ceiling_unit = ref.nppa_ceiling
        info.nppa_as_of = ref.nppa_as_of
        unit_mrp = item.unit_price()
        if item.scheduled and unit_mrp is not None:
            info.above_ceiling = unit_mrp > ref.nppa_ceiling * (1 + gst_pct / 100) + 1e-9
    return info
