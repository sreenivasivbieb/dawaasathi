"""Price gap vs Jan Aushadhi / NPPA ceiling, and cheaper same-composition alternatives."""

from __future__ import annotations

import pytest

from app.engine import alternatives as altmod
from app.engine import price as pricemod
from app.engine.salts import salt_key, strength_key
from app.models import DrugItem

GST = 12.0
FRESH = 400


@pytest.fixture
def item(store, brand_ids):
    def _item(name: str, **changes) -> DrugItem:
        it = store.brand_item(brand_ids[name], "strip")
        return it.model_copy(update=changes)

    return _item


# ---------------------------------------------------------------- alternatives
def test_augmentin_alternatives(store, item):
    aug = item("Augmentin 625 Duo Tablet")
    alts = altmod.cheaper(store, aug)
    assert alts, "expected at least the Jan Aushadhi generic"
    ja = alts[0]
    assert ja.is_jan_aushadhi
    assert ja.name == "Jan Aushadhi Amoxycillin + Clavulanic Acid 625"
    assert ja.unit_price == pytest.approx(6.2)
    assert ja.saves_pct == 72  # 1 - 6.20 / 22.34 per tablet
    # Moxikind-CV (Rs 168 for 6) is cheaper per pack but dearer per tablet than Augmentin: not offered.
    names = [a.name for a in alts]
    assert "Moxikind-CV 625 Tablet" not in names
    assert "Clavam 625 Tablet" not in names


def test_only_cheaper_options_and_sorted(store, item):
    for name in ("Dolo 650 Tablet", "Azithral 500 Tablet", "Mox 500 Capsule", "Pan 40 Tablet", "Augmentin 625 Duo Tablet"):
        it = item(name)
        base = it.unit_price()
        alts = altmod.cheaper(store, it, k=10)
        assert alts, name
        assert all(a.unit_price < base for a in alts), name
        assert all(a.brand_id != it.brand_id for a in alts), name
        rest = [a.unit_price for a in alts if not a.is_jan_aushadhi]
        assert rest == sorted(rest), name
        assert [a.is_jan_aushadhi for a in alts] == sorted((a.is_jan_aushadhi for a in alts), reverse=True), name


def test_dolo_alternatives_include_cheaper_brand(store, item):
    alts = altmod.cheaper(store, item("Dolo 650 Tablet"))
    assert [a.name for a in alts] == ["Jan Aushadhi Paracetamol 650", "Calpol 650 Tablet"]
    assert [a.saves_pct for a in alts] == [46, 9]
    assert alts[1].brand_id is not None and not alts[1].is_jan_aushadhi


def test_savings_are_per_unit_when_pack_sizes_differ(store, item):
    gly = item("Glycomet 500 Tablet")  # Rs 25 for 20; Jan Aushadhi Rs 6 for 10
    alts = altmod.cheaper(store, gly)
    assert alts[0].is_jan_aushadhi
    assert alts[0].saves_pct == 52  # per tablet: 1 - 0.60 / 1.25 (pack-to-pack would wrongly say 76)
    azi = item("Azithral 500 Tablet")  # Rs 119.50 for 5; Jan Aushadhi Rs 27 for 3
    assert altmod.cheaper(store, azi)[0].saves_pct == 62


def test_same_salt_strength_form_release_only(store, item):
    for name in ("Glycomet 500 Tablet", "Glycomet SR 500 Tablet", "Dolo 650 Tablet", "Mox 500 Capsule",
                 "Calpol 250 Suspension", "Augmentin 625 Duo Tablet", "Pan 40 Tablet"):
        it = item(name)
        sk, stk, rel = salt_key(it.salts), strength_key(it.salts, it.form), it.release or "IR"
        for a in altmod.cheaper(store, it, k=10):
            if a.brand_id is not None:
                b = store.brands[a.brand_id]
                assert (b.salt_key, b.strength_key, b.form, b.release or "IR") == (sk, stk, it.form, rel), (name, a)


def test_glycomet_sr_and_ir_never_cross(store, item):
    ir_alts = [a.name for a in altmod.cheaper(store, item("Glycomet 500 Tablet", mrp=400.0), k=10)]
    sr_alts = [a.name for a in altmod.cheaper(store, item("Glycomet SR 500 Tablet", mrp=400.0), k=10)]
    # Even with an inflated MRP (so every option is "cheaper"), the release class never crosses.
    assert "Glycomet SR 500 Tablet" not in ir_alts and "Jan Aushadhi Metformin SR 500" not in ir_alts
    assert "Glycomet 500 Tablet" not in sr_alts and "Jan Aushadhi Metformin 500" not in sr_alts
    assert "Jan Aushadhi Metformin 500" in ir_alts
    assert "Jan Aushadhi Metformin SR 500" in sr_alts
    assert all("GP" not in n for n in ir_alts + sr_alts)  # glimepiride combo is a different salt set


def test_different_strength_or_form_never_offered(store, item):
    names = [a.name for a in altmod.cheaper(store, item("Dolo 650 Tablet", mrp=999.0), k=10)]
    assert "Calpol 500 Tablet" not in names and "Crocin Advance Tablet" not in names
    assert "Calpol 250 Suspension" not in names and "Crocin Drops" not in names
    caps = [a.name for a in altmod.cheaper(store, item("Mox 500 Capsule"), k=10)]
    assert caps == ["Jan Aushadhi Amoxycillin 500", "Novamox 500 Capsule"]


def test_max_k(store, item):
    dolo = item("Dolo 650 Tablet")
    assert len(altmod.cheaper(store, dolo, k=1)) == 1
    assert altmod.cheaper(store, dolo, k=1)[0].is_jan_aushadhi
    assert len(altmod.cheaper(store, item("Augmentin 625 Duo Tablet", mrp=10000.0), k=2)) == 2
    assert len(altmod.cheaper(store, item("Augmentin 625 Duo Tablet", mrp=10000.0), k=10)) == 3


def test_jan_aushadhi_only_when_cheaper(store, item):
    cheap_dolo = item("Dolo 650 Tablet", mrp=9.0)  # Rs 0.60 a tablet, below Jan Aushadhi's Rs 1.20
    assert altmod.cheaper(store, cheap_dolo) == []


def test_unknown_mrp_lists_options_without_savings(store, item):
    alts = altmod.cheaper(store, item("Dolo 650 Tablet", mrp=None))
    assert alts and alts[0].is_jan_aushadhi
    assert all(a.saves_pct is None for a in alts)


def test_stale_reference_is_not_offered(store, item):
    alts = altmod.cheaper(store, item("Dolo 650 Tablet"), stale_days=10)  # Jan Aushadhi list is 2026-10-01
    assert all(not a.is_jan_aushadhi for a in alts)


def test_missing_composition_gives_nothing(store, item):
    assert altmod.cheaper(store, DrugItem(source="strip", brand="x")) == []
    assert altmod.cheaper(store, item("Dolo 650 Tablet", form=None)) == []


# ---------------------------------------------------------------- price gap
def test_augmentin_price_gap_and_ceiling(store, item):
    info = pricemod.price_gap(store, item("Augmentin 625 Duo Tablet"), GST, FRESH)
    assert info.reference == 62.0 and info.reference_pack_qty == 10
    assert info.ratio == 3.6  # 22.34 / 6.20 per tablet
    assert info.nppa_ceiling_unit == 15.97
    assert 22.34 > 15.97 * 1.12
    assert info.above_ceiling is True
    assert info.stale is False


def test_ceiling_only_for_scheduled(store, item):
    info = pricemod.price_gap(store, item("Augmentin 625 Duo Tablet", scheduled=False), GST, FRESH)
    assert info.nppa_ceiling_unit == 15.97
    assert info.above_ceiling is None
    info = pricemod.price_gap(store, item("Augmentin 625 Duo Tablet", scheduled=None), GST, FRESH)
    assert info.above_ceiling is None


def test_ceiling_includes_gst(store, item):
    # Dolo 650: Rs 2.24 a tablet; ceiling 2.04 + 12% GST = 2.28 -> not above.
    info = pricemod.price_gap(store, item("Dolo 650 Tablet"), GST, FRESH)
    assert info.nppa_ceiling_unit == 2.04
    assert info.above_ceiling is False
    info = pricemod.price_gap(store, item("Dolo 650 Tablet"), 0.0, FRESH)
    assert info.above_ceiling is True


def test_no_ceiling_listed(store, item):
    info = pricemod.price_gap(store, item("Glycomet 500 Tablet"), GST, FRESH)
    assert info.ratio == 2.1
    assert info.nppa_ceiling_unit is None and info.above_ceiling is None


def test_stale_price_data_gives_no_ratio(store, item):
    info = pricemod.price_gap(store, item("Augmentin 625 Duo Tablet"), GST, 10)
    assert info.stale is True
    assert info.ratio is None
    assert info.reference == 62.0  # still reported, flagged stale
    assert info.nppa_ceiling_unit is None and info.above_ceiling is None


def test_liquid_price_gap_per_ml(store, item):
    info = pricemod.price_gap(store, item("Calpol 250 Suspension"), GST, FRESH)
    assert info.ratio == 3.2  # 52/60 vs 16/60


def test_release_class_picks_the_right_reference(store, item):
    assert pricemod.find_ref(store, item("Glycomet SR 500 Tablet")).pmbj_name == "Jan Aushadhi Metformin SR 500"
    assert pricemod.find_ref(store, item("Glycomet 500 Tablet")).pmbj_name == "Jan Aushadhi Metformin 500"


def test_no_reference(store, item):
    info = pricemod.price_gap(store, item("Trajenta 5 Tablet"), GST, FRESH)
    assert info.mrp == 950.0 and info.reference is None and info.ratio is None


def test_missing_pack_qty_gives_no_ratio(store, item):
    info = pricemod.price_gap(store, item("Dolo 650 Tablet", pack_qty=None), GST, FRESH)
    assert info.reference == 12.0 and info.ratio is None and info.above_ceiling is None


# ---------------------------------------------------------------- API
def test_api_banned_item_has_no_alternatives(client):
    r = client.get("/v1/alternatives", params={"salt": "aceclofenac,paracetamol,rabeprazole",
                                               "strength": "100mg,325mg,20mg", "form": "tablet"})
    assert r.status_code == 200
    body = r.json()
    assert body["banned"]["status"] == "banned"
    assert body["alternatives"] == []
    r = client.post("/v1/query", data={"text": "saathi demo fdc का सस्ता जेनेरिक", "lang": "hi"})
    body = r.json()
    assert body["verdict"]["code"] == "BANNED_COMBINATION"
    assert body["alternatives"] == []
