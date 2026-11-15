from dataclasses import replace
from pathlib import Path
import csv
import json
import sqlite3

import pytest

from app.config import get_settings
from scripts import import_data1


def test_data1_policy_disables_web_even_with_credentials(settings):
    strict = replace(settings, data1_only=True, groq_api_keys=("test-only",),
                     tavily_api_key="test-only", web_lookup_on=True, web_brands_on=True)
    assert not strict.web_lookup
    assert not strict.web_brands
    assert strict.llm_nlu  # language understanding is still available


def test_data1_mode_refuses_legacy_fixture_db(settings):
    from app.main import create_app
    from fastapi.testclient import TestClient

    with pytest.raises(RuntimeError, match="requires the data1 database"):
        with TestClient(create_app(replace(settings, data1_only=True))):
            pass


def test_pdf_legal_footnotes_and_strength_restrictions_are_not_flattened():
    entries = [
        {"row": 120, "page": 9, "description": "Nimesulide + Diclofenac", "notification": "S.O.4380(E) Dated 07.09.2018", "footnotes": ""},
        {"row": 114, "page": 9, "description": "Aceclofenac + Paracetamol + Rabeprazole", "notification": "S.O.4379(E) Dated 07.09.2018", "footnotes": "5#"},
        {"row": 217, "page": 15, "description": "Pioglitazone 15mg + Metformin 850mg", "notification": "S.O.4481(E) Dated 07.09.2018", "footnotes": ""},
    ]
    known = {"nimesulide", "diclofenac", "aceclofenac", "paracetamol", "rabeprazole", "pioglitazone", "metformin"}
    rows, audit = import_data1.banned_rows(entries, known)
    assert len(rows) == 1
    assert rows[0]["salts"] == "nimesulide+diclofenac"
    assert [r["imported"] for r in audit] == [True, False, False]
    assert "legal" in audit[1]["reason"]
    assert "strength" in audit[2]["reason"]


def test_missing_source_does_not_touch_existing_database(tmp_path):
    target = tmp_path / "existing.db"
    target.write_bytes(b"preserve this database")
    with pytest.raises(FileNotFoundError):
        import_data1.build_database(tmp_path / "data1", target)
    assert target.read_bytes() == b"preserve this database"


def test_complete_data1_import_has_no_fixture_or_web_rows(tmp_path, monkeypatch):
    from app.db import Store, connect
    from app.engine.safety import check_banned
    from app.models import DrugItem, SaltDose
    from app.main import create_app
    from fastapi.testclient import TestClient
    from app.adapters.gnani_tts import MockTTS
    from helpers import FakeOCR

    src = tmp_path / "data1"
    (src / "archive").mkdir(parents=True)
    with (src / import_data1.AZ).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "Is_discontinued", "short_composition1", "short_composition2", "pack_size_label", "price(₹)"])
        w.writeheader()
        w.writerows([
            {"name": "Dolo 650 Tablet", "Is_discontinued": "FALSE", "short_composition1": "Paracetamol (650mg)", "pack_size_label": "strip of 15 tablets", "price(₹)": "33.76"},
            {"name": "Retired tablet", "Is_discontinued": "TRUE", "short_composition1": "Paracetamol (650mg)"},
        ])
    for name in (import_data1.PMBJP, import_data1.DPCO, import_data1.BANNED):
        (src / name).write_text("source test only")
    monkeypatch.setattr(import_data1, "pdf_entries", lambda p: [{"row": 120, "page": 9,
        "description": "Paracetamol + Nimesulide", "notification": "S.O.4380(E) Dated 07.09.2018", "footnotes": ""}])
    monkeypatch.setattr(import_data1, "price_rows", lambda p: ([{"composition": "Paracetamol 650mg", "form": "tablet", "release": "IR",
        "pmbj_name": "Jan Aushadhi Paracetamol", "pmbj_price": "10", "pmbj_pack_qty": "10", "pmbj_as_of": "2026-11-13"}], {}))
    # The other ingredient must itself appear in the supplied brand list for the conservative PDF importer.
    with (src / import_data1.AZ).open("a", newline="") as f:
        csv.writer(f).writerow(["Nimesulide tablet", "FALSE", "Nimesulide (100mg)", "", "strip of 10 tablets", "20"])
    target = tmp_path / "active.db"
    report = import_data1.build_database(src, target)
    assert report["brands"] == 2
    con = connect(target)
    store = Store.load(con)
    con.close()
    assert len(store.brands) == 2 and not store.nsq and not store.lasa
    assert store.dataset("brands")["as_of"] is None
    assert store.dataset("banned_fdc")["as_of"] == "2021-11-22"
    assert all(not tags for tags in store.salt_use.values())
    result = check_banned(store, DrugItem(salts=[SaltDose(name="paracetamol"), SaltDose(name="nimesulide")]), 400)
    assert result.status == "unverified" and result.reason == "banned_list_stale"
    manifest = json.loads(target.with_suffix(".import.json").read_text())
    assert set(manifest["sources"]) == set(import_data1.FILES)
    assert all(len(s["sha256"]) == 64 for s in manifest["sources"].values())
    # A successful rebuild also preserves the previous medicine DB.
    second = import_data1.build_database(src, target)
    assert Path(second["backup"]).exists()
    with sqlite3.connect(second["backup"]) as backup:
        assert backup.execute("SELECT count(*) FROM brands").fetchone()[0] == 2

    settings = replace(get_settings(), db_path=target, gnani_api_key=None, groq_api_keys=(),
                       tavily_api_key=None, cache_dir=tmp_path / "audio", local_db_path=tmp_path / "history.db",
                       data1_only=True)
    from app.engine.web_brands import WebBrandLookup
    from unittest.mock import Mock
    learned = Mock(side_effect=AssertionError("must not load previously learned external brands"))
    monkeypatch.setattr(WebBrandLookup, "load_learned", learned)
    with TestClient(create_app(settings, ocr=FakeOCR(), tts=MockTTS())) as client:
        assert client.get("/healthz").json()["brands"] == 2
    learned.assert_not_called()
