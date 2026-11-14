"""Build the SQLite database from CSV files (see data/README.md for the formats).

  uv run python -m scripts.load_data --src data/seed_dev
  uv run python -m scripts.load_data --src data/real --az data/raw/A_Z_medicine_dataset_of_India.csv \
      --az-as-of 2026-11-01 --az-limit 20000

Every dataset is stored with its source and as-of date (datasets table); the API shows them.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import connect, split_brand_name  # noqa: E402
from app.engine import salts as saltmod  # noqa: E402
from app.engine.salts import (canonical_form, canonical_release, canonical_salt, parse_composition,  # noqa: E402
                              release_from_name, salt_key, strength_key)
from app.match.translit import dm_keys, normalize, skeleton, soundex  # noqa: E402


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def _f(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _pack(label: str) -> tuple[float | None, str | None]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tablets?|capsules?|ml|g|gm|sachets?|drops?|units?)", label or "", re.I)
    if not m:
        return None, None
    unit = m.group(2).lower().rstrip("s")
    return float(m.group(1)), {"tablet": "tablets", "capsule": "capsules"}.get(unit, unit)


def load_salts(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for r in rows:
        name = r["name"].lower()
        syn = [s.strip().lower() for s in r.get("synonyms", "").split("|") if s.strip()]
        use = [s.strip().lower() for s in r.get("usual_use", "").split("|") if s.strip()]
        conn.execute("INSERT OR REPLACE INTO salts(name, synonyms, usual_use) VALUES (?,?,?)",
                     (name, json.dumps(syn), json.dumps(use)))
    saltmod.register_salts([(r["name"], r.get("synonyms", "").split("|")) for r in rows])


def insert_brand(conn: sqlite3.Connection, name: str, composition: str, form: str | None, release: str | None,
                 pack_qty, pack_unit, mrp, manufacturer, scheduled, source, updated_at, pack_label=None) -> int | None:
    salts = parse_composition(composition)
    if not salts:
        return None
    form_c = canonical_form(form) or canonical_form(name) or canonical_form(pack_label)
    rel = canonical_release(release) or release_from_name(name)
    base, family, _ = split_brand_name(name)
    cur = conn.execute(
        """INSERT INTO brands(name, name_norm, base_name, family, salts, salt_key, strength_key, form, release,
           pack, pack_qty, pack_unit, mrp, manufacturer, scheduled, source, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, normalize(name), base, family, json.dumps([s.model_dump() for s in salts]), salt_key(salts),
         strength_key(salts, form_c), form_c, rel, pack_label, pack_qty, pack_unit, mrp, manufacturer,
         1 if str(scheduled).lower() in ("1", "true", "yes", "y") else 0, source, updated_at),
    )
    return cur.lastrowid


def load_brands(conn: sqlite3.Connection, rows: list[dict], meta: dict) -> int:
    n = 0
    for r in rows:
        bid = insert_brand(conn, r["name"], r["composition"], r.get("form"), r.get("release"),
                           _f(r.get("pack_qty")), r.get("pack_unit") or None, _f(r.get("mrp")),
                           r.get("manufacturer") or None, r.get("scheduled", "0"),
                           r.get("source") or meta.get("source"), r.get("updated_at") or meta.get("as_of"))
        n += 1 if bid else 0
    return n


def load_az(conn: sqlite3.Connection, path: Path, as_of: str, limit: int | None) -> int:
    """Kaggle 'A-Z Medicine Dataset of India' (id,name,price(₹),Is_discontinued,manufacturer_name,type,
    pack_size_label,short_composition1,short_composition2)."""
    n = 0
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("Is_discontinued", "")).strip().upper() == "TRUE":
                continue
            comp = " + ".join(x.strip() for x in (r.get("short_composition1"), r.get("short_composition2")) if x and x.strip())
            price_key = next((k for k in r if k and k.lower().startswith("price")), None)
            qty, unit = _pack(r.get("pack_size_label", ""))
            bid = insert_brand(conn, r["name"].strip(), comp, None, None, qty, unit, _f(r.get(price_key)) if price_key else None,
                               (r.get("manufacturer_name") or "").strip() or None, "0",
                               "A-Z Medicine Dataset of India (Kaggle)", as_of, r.get("pack_size_label"))
            if bid:
                n += 1
            if limit and n >= limit:
                break
    return n


def load_aliases(conn: sqlite3.Connection, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        ids = [row[0] for row in conn.execute("SELECT id FROM brands WHERE lower(name) = lower(?)", (r["brand"],))]
        if not ids:
            ids = [row[0] for row in conn.execute("SELECT id FROM brands WHERE family = ?", (r["brand"].lower(),))]
        alias = r["alias"]
        roman = normalize(alias)
        dev = alias if re.search(r"[ऀ-ॿ]", alias) else None
        for bid in ids:
            conn.execute("INSERT INTO variants(brand_id, roman, devanagari, dm_key, soundex, skeleton) VALUES (?,?,?,?,?,?)",
                         (bid, roman, dev, ",".join(dm_keys(roman)), soundex(roman), skeleton(roman)))
            n += 1
    return n


def load_price_ref(conn: sqlite3.Connection, rows: list[dict]) -> int:
    for r in rows:
        salts = parse_composition(r["composition"])
        form = canonical_form(r.get("form"))
        conn.execute(
            """INSERT INTO price_ref(salt_key, strength_key, form, release, salts, pmbj_name, pmbj_price, pmbj_pack_qty,
               pmbj_as_of, pmbj_source, nppa_ceiling, nppa_as_of, nppa_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (salt_key(salts), strength_key(salts, form), form, canonical_release(r.get("release")) or "IR",
             json.dumps([s.model_dump() for s in salts]), r.get("pmbj_name") or None, _f(r.get("pmbj_price")),
             _f(r.get("pmbj_pack_qty")), r.get("pmbj_as_of") or None, r.get("pmbj_source") or None,
             _f(r.get("nppa_ceiling_unit")), r.get("nppa_as_of") or None, r.get("nppa_source") or None),
        )
    return len(rows)


def load_banned(conn: sqlite3.Connection, rows: list[dict]) -> int:
    for r in rows:
        names = sorted({canonical_salt(s) for s in re.split(r"\s*\+\s*", r["salts"]) if s.strip()})
        strengths = None
        if r.get("strengths"):
            parsed = parse_composition(r["strengths"])
            strengths = {s.name: s.comparable()[0] for s in parsed if s.comparable()}
        conn.execute(
            "INSERT INTO banned_fdc(salts, salt_key, form, strengths, notification_ref, notification_date, source_url) "
            "VALUES (?,?,?,?,?,?,?)",
            (json.dumps(names), "+".join(names), canonical_form(r.get("form")), json.dumps(strengths) if strengths else None,
             r["notification_ref"], r.get("notification_date") or None, r.get("source_url") or None),
        )
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "seed_dev")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--az", type=Path, default=None, help="A-Z Medicine Dataset of India CSV")
    ap.add_argument("--az-as-of", default=None)
    ap.add_argument("--az-limit", type=int, default=None)
    ap.add_argument("--keep", action="store_true", help="append instead of rebuilding")
    args = ap.parse_args()

    db = args.db or get_settings().db_path
    if db.exists() and not args.keep:
        db.unlink()
    conn = connect(db)
    meta_path = args.src / "datasets.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    counts = {}
    load_salts(conn, _rows(args.src / "salts.csv"))
    counts["brands"] = load_brands(conn, _rows(args.src / "brands.csv"), meta.get("brands", {}))
    if args.az:
        as_of = args.az_as_of or datetime.now().date().isoformat()
        counts["brands"] += load_az(conn, args.az, as_of, args.az_limit)
        meta.setdefault("brands", {})
        meta["brands"]["source"] = (meta["brands"].get("source", "") + " + A-Z Medicine Dataset of India").strip(" +")
        meta["brands"]["as_of"] = meta["brands"].get("as_of") or as_of
    counts["variants"] = load_aliases(conn, _rows(args.src / "aliases.csv"))
    counts["price_ref"] = load_price_ref(conn, _rows(args.src / "price_ref.csv"))
    counts["banned_fdc"] = load_banned(conn, _rows(args.src / "banned_fdc.csv"))
    nsq = _rows(args.src / "nsq_alerts.csv")
    for r in nsq:
        conn.execute("INSERT INTO nsq_alerts VALUES (?,?,?,?,?)",
                     (r.get("drug"), r.get("batch_no"), r.get("manufacturer"), r.get("alert_month"), r.get("source_url")))
    counts["nsq_alerts"] = len(nsq)
    lasa = _rows(args.src / "lasa_pairs.csv")
    for r in lasa:
        conn.execute("INSERT INTO lasa_pairs VALUES (?,?,?)", (r["brand_a"], r["brand_b"], _f(r.get("similarity"))))
    counts["lasa_pairs"] = len(lasa)

    for name in ("brands", "price_ref", "banned_fdc", "nsq_alerts"):
        m = meta.get(name, {})
        conn.execute("INSERT OR REPLACE INTO datasets(name, as_of, source, loaded_at, rows) VALUES (?,?,?,?,?)",
                     (name, m.get("as_of"), m.get("source"), _now(), counts.get(name, 0)))
    conn.commit()
    conn.close()
    print(json.dumps({"db": str(db), **counts}, indent=2))


if __name__ == "__main__":
    main()
