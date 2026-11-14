"""SQLite schema and an in-memory read model loaded at startup.

Every stage reads from one SQLite file. Loading is done by scripts/load_data.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.engine import salts as saltmod
from app.models import SaltDose

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  base_name TEXT NOT NULL,
  family TEXT NOT NULL,
  salts TEXT NOT NULL,            -- JSON list of {name, amount, unit, per_ml}
  salt_key TEXT NOT NULL,
  strength_key TEXT,
  form TEXT,
  release TEXT,
  pack TEXT,
  pack_qty REAL,
  pack_unit TEXT,
  mrp REAL,
  manufacturer TEXT,
  scheduled INTEGER DEFAULT 0,
  source TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS brands_salt ON brands(salt_key, strength_key, form, release);
CREATE TABLE IF NOT EXISTS variants (
  brand_id INTEGER, roman TEXT, devanagari TEXT, dm_key TEXT, soundex TEXT, skeleton TEXT
);
CREATE TABLE IF NOT EXISTS salts (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, synonyms TEXT, usual_use TEXT
);
CREATE TABLE IF NOT EXISTS price_ref (
  salt_key TEXT, strength_key TEXT, form TEXT, release TEXT,
  salts TEXT,
  pmbj_name TEXT, pmbj_price REAL, pmbj_pack_qty REAL, pmbj_as_of TEXT, pmbj_source TEXT,
  nppa_ceiling REAL, nppa_as_of TEXT, nppa_source TEXT
);
CREATE INDEX IF NOT EXISTS price_ref_key ON price_ref(salt_key, strength_key, form, release);
CREATE TABLE IF NOT EXISTS banned_fdc (
  id INTEGER PRIMARY KEY, salts TEXT, salt_key TEXT, form TEXT, strengths TEXT,
  notification_ref TEXT, notification_date TEXT, source_url TEXT
);
CREATE TABLE IF NOT EXISTS nsq_alerts (
  drug TEXT, batch_no TEXT, manufacturer TEXT, alert_month TEXT, source_url TEXT
);
CREATE TABLE IF NOT EXISTS lasa_pairs (brand_a TEXT, brand_b TEXT, similarity REAL);
CREATE TABLE IF NOT EXISTS datasets (
  name TEXT PRIMARY KEY, as_of TEXT, source TEXT, loaded_at TEXT, rows INTEGER
);
CREATE TABLE IF NOT EXISTS eval_cases (
  id TEXT PRIMARY KEY, audio_ref TEXT, transcript TEXT, truth_brand TEXT, truth_strength TEXT,
  snr_db REAL, sample_rate INTEGER
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


_NAME_STOP = re.compile(
    r"^(\d|mg$|mcg$|ml$|tablets?$|tab$|capsules?$|cap$|syrup$|suspension$|drops?$|injection$|"
    r"sr$|er$|xr$|xl$|cr$|od$|dr$|ds$|duo$|forte$|kid$|dt$|ped$|oral$|dry$|cream$|gel$|ointment$)",
    re.I,
)


def split_brand_name(name: str) -> tuple[str, str, list[str]]:
    """'Augmentin 625 Duo Tablet' -> (base 'augmentin', family 'augmentin', label tokens)."""
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]*|\d+(?:\.\d+)?", name)
    base_tokens = []
    for tok in tokens:
        if _NAME_STOP.match(tok):
            break
        base_tokens.append(tok.lower())
    if not base_tokens and tokens:
        base_tokens = [tokens[0].lower()]
    base = " ".join(base_tokens)
    family = base_tokens[0] if base_tokens else base
    rest = [t.lower() for t in tokens[len(base_tokens):]]
    return base, family, rest


LEARNED_ID_BASE = 10_000_000  # brand ids >= this came from the web lookup (var/local.db)


@dataclass(slots=True)
class BrandRec:
    id: int
    name: str
    base: str
    family: str
    label_tokens: list[str]
    salts: list[SaltDose]
    salt_key: str
    strength_key: str | None
    form: str | None
    release: str | None
    pack_qty: float | None
    pack_unit: str | None
    mrp: float | None
    manufacturer: str | None
    scheduled: bool
    source: str | None
    updated_at: str | None

    def numbers(self) -> set[float]:
        nums = {float(t) for t in self.label_tokens if re.fullmatch(r"\d+(?:\.\d+)?", t)}
        amounts = [s.amount for s in self.salts if s.amount is not None]
        nums.update(a for a in amounts)
        if len(amounts) > 1:
            nums.add(sum(amounts))
        return nums

    def sku_label(self) -> str:
        return self.name


@dataclass(slots=True)
class PriceRef:
    salt_key: str
    strength_key: str | None
    form: str | None
    release: str | None
    salts: list[SaltDose]
    pmbj_name: str | None
    pmbj_price: float | None
    pmbj_pack_qty: float | None
    pmbj_as_of: str | None
    pmbj_source: str | None
    nppa_ceiling: float | None
    nppa_as_of: str | None
    nppa_source: str | None


@dataclass(slots=True)
class BannedRec:
    id: int
    salts: list[str]
    salt_key: str
    form: str | None
    strengths: dict[str, float] | None
    notification_ref: str
    notification_date: str | None
    source_url: str | None


@dataclass
class Store:
    brands: dict[int, BrandRec] = field(default_factory=dict)
    by_salt: dict[str, list[int]] = field(default_factory=dict)
    aliases: list[tuple[int, str]] = field(default_factory=list)
    salt_use: dict[str, list[str]] = field(default_factory=dict)
    price_refs: list[PriceRef] = field(default_factory=list)
    banned: list[BannedRec] = field(default_factory=list)
    nsq: list[dict] = field(default_factory=list)
    lasa: dict[str, set[str]] = field(default_factory=dict)
    datasets: dict[str, dict] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Store":
        st = cls()
        salt_rows = conn.execute("SELECT name, synonyms, usual_use FROM salts").fetchall()
        saltmod.register_salts([(r["name"], json.loads(r["synonyms"] or "[]")) for r in salt_rows])
        for r in salt_rows:
            st.salt_use[r["name"].lower()] = json.loads(r["usual_use"] or "[]")
        for r in conn.execute("SELECT * FROM brands"):
            salts = [SaltDose(**s) for s in json.loads(r["salts"])]
            base, family, rest = split_brand_name(r["name"])
            rec = BrandRec(
                id=r["id"], name=r["name"], base=r["base_name"] or base, family=r["family"] or family,
                label_tokens=rest, salts=salts, salt_key=r["salt_key"], strength_key=r["strength_key"],
                form=r["form"], release=r["release"], pack_qty=r["pack_qty"], pack_unit=r["pack_unit"],
                mrp=r["mrp"], manufacturer=r["manufacturer"], scheduled=bool(r["scheduled"]),
                source=r["source"], updated_at=r["updated_at"],
            )
            st.brands[rec.id] = rec
            st.by_salt.setdefault(rec.salt_key, []).append(rec.id)
        for r in conn.execute("SELECT brand_id, roman FROM variants WHERE roman IS NOT NULL"):
            st.aliases.append((r["brand_id"], r["roman"]))
        for r in conn.execute("SELECT * FROM price_ref"):
            st.price_refs.append(PriceRef(
                salt_key=r["salt_key"], strength_key=r["strength_key"], form=r["form"],
                release=r["release"], salts=[SaltDose(**s) for s in json.loads(r["salts"] or "[]")],
                pmbj_name=r["pmbj_name"], pmbj_price=r["pmbj_price"], pmbj_pack_qty=r["pmbj_pack_qty"],
                pmbj_as_of=r["pmbj_as_of"], pmbj_source=r["pmbj_source"], nppa_ceiling=r["nppa_ceiling"],
                nppa_as_of=r["nppa_as_of"], nppa_source=r["nppa_source"],
            ))
        for r in conn.execute("SELECT * FROM banned_fdc"):
            st.banned.append(BannedRec(
                id=r["id"], salts=json.loads(r["salts"]), salt_key=r["salt_key"], form=r["form"],
                strengths=json.loads(r["strengths"]) if r["strengths"] else None,
                notification_ref=r["notification_ref"], notification_date=r["notification_date"],
                source_url=r["source_url"],
            ))
        st.nsq = [dict(r) for r in conn.execute("SELECT * FROM nsq_alerts")]
        for r in conn.execute("SELECT brand_a, brand_b FROM lasa_pairs"):
            a, b = r["brand_a"].lower(), r["brand_b"].lower()
            st.lasa.setdefault(a, set()).add(b)
            st.lasa.setdefault(b, set()).add(a)
        st.datasets = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM datasets")}
        return st

    # ------------------------------------------------------------------ learned (web) brands
    def add_learned(self, row: dict) -> BrandRec | None:
        """Add a brand found by the web lookup (a learned_brands row, app/localdb.py) to the
        in-memory store. Ids are offset so they never collide with the drug DB's own ids."""
        salts = saltmod.parse_composition(row["composition"])
        if not salts:
            return None
        bid = LEARNED_ID_BASE + int(row["id"])
        if bid in self.brands:
            return self.brands[bid]
        name = row["name"]
        form = saltmod.canonical_form(row.get("form")) or saltmod.canonical_form(name)
        release = saltmod.canonical_release(row.get("release")) or saltmod.canonical_release(name) or "IR"
        base, family, rest = split_brand_name(name)
        rec = BrandRec(
            id=bid, name=name, base=base, family=family, label_tokens=rest, salts=salts,
            salt_key=saltmod.salt_key(salts), strength_key=saltmod.strength_key(salts, form), form=form,
            release=release, pack_qty=row.get("pack_qty"), pack_unit=row.get("pack_unit"), mrp=row.get("mrp"),
            manufacturer=row.get("manufacturer"), scheduled=False,
            source=f"web: {row.get('source')} ({row.get('source_url')})", updated_at=(row.get("fetched_at") or "")[:10],
        )
        self.brands[bid] = rec
        self.by_salt.setdefault(rec.salt_key, []).append(bid)
        return rec

    # ------------------------------------------------------------------ helpers
    def dataset(self, name: str) -> dict | None:
        return self.datasets.get(name)

    def is_stale(self, as_of: str | None, max_days: int, today: date | None = None) -> bool:
        if not as_of:
            return True
        try:
            d = datetime.fromisoformat(as_of[:10]).date()
        except ValueError:
            return True
        return ((today or date.today()) - d).days > max_days

    def brand_item(self, brand_id: int, source: str = "db"):
        from app.models import DrugItem

        b = self.brands[brand_id]
        return DrugItem(
            source=source, brand_id=b.id, brand=b.name, salts=[s.model_copy() for s in b.salts],
            form=b.form, release=b.release, mrp=b.mrp, pack_qty=b.pack_qty, pack_unit=b.pack_unit,
            scheduled=b.scheduled, manufacturer=b.manufacturer,
        )
