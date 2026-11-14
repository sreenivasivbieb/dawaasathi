"""Cheaper options with the same salt set, strength, dosage form and release class.

Jan Aushadhi first (when cheaper), then lowest-priced brands; never a different salt or
release class. Savings are per unit, against the strip's MRP.
"""

from __future__ import annotations

from app.db import Store
from app.engine.price import find_ref
from app.engine.salts import release_class, salt_key, strength_key
from app.models import Alternative, DrugItem


def cheaper(store: Store, item: DrugItem, k: int = 3, stale_days: int = 400) -> list[Alternative]:
    if not item.salts or item.form is None:
        return []
    sk = salt_key(item.salts)
    stk = strength_key(item.salts, item.form)
    if stk is None:
        return []
    rc = release_class(item.release, sk)
    base_unit = item.unit_price()

    options: list[Alternative] = []
    ref = find_ref(store, item)
    if ref and ref.pmbj_price and ref.pmbj_pack_qty and not store.is_stale(ref.pmbj_as_of, stale_days):
        unit = ref.pmbj_price / ref.pmbj_pack_qty
        options.append(Alternative(
            name=ref.pmbj_name or "Jan Aushadhi generic", source=ref.pmbj_source or "PMBJP",
            price=ref.pmbj_price, pack_qty=ref.pmbj_pack_qty, unit_price=round(unit, 4),
            as_of=ref.pmbj_as_of, is_jan_aushadhi=True,
        ))

    seen_names = set()
    for bid in store.by_salt.get(sk, []):
        b = store.brands[bid]
        if bid == item.brand_id or b.strength_key != stk or b.form != item.form:
            continue
        if release_class(b.release, sk) != rc or b.mrp is None or not b.pack_qty:
            continue
        key = b.name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        options.append(Alternative(
            name=b.name, source=b.source or "brand DB", price=b.mrp, pack_qty=b.pack_qty,
            unit_price=round(b.mrp / b.pack_qty, 4), brand_id=b.id, as_of=b.updated_at,
        ))

    if base_unit is not None:
        options = [o for o in options if o.unit_price is not None and o.unit_price < base_unit - 1e-9]
        for o in options:
            o.saves_pct = int(round((1 - o.unit_price / base_unit) * 100))
    options.sort(key=lambda o: (not o.is_jan_aushadhi, o.unit_price or 1e9, o.name))
    # Jan Aushadhi first only when it is in the list (i.e. cheaper); the rest by unit price.
    ja = [o for o in options if o.is_jan_aushadhi]
    rest = sorted([o for o in options if not o.is_jan_aushadhi], key=lambda o: (o.unit_price or 1e9, o.name))
    return (ja + rest)[:k]
