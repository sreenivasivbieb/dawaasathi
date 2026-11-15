"""Build a database exclusively from the four supplied data1 files, with an audit manifest.

No seed rows, web lookups, invented snapshot dates or inferred indications are added.
The PDF is a historical list with legal footnotes; only unambiguous, unrestricted
ingredient combinations are machine imported. Every other row is retained in the report.
Its actual 2021 status date is retained, so the engine reports the list as stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from app.db import connect
from app.engine.salts import canonical_salt
from scripts.import_gov import _composition, _iso, load_dpco, load_pmbjp
from scripts.load_data import load_az, load_banned, load_price_ref

AZ = "archive/A_Z_medicines_dataset_of_India.csv"
PMBJP = "Product List_13_11_2026 @ 16_32_21.csv"
DPCO = "dpco-2026-ceiling-prices.csv"
BANNED = "Updated Banned Drugs List.pdf"
FILES = (AZ, PMBJP, DPCO, BANNED)
NS = {"x": "http://www.w3.org/1999/xhtml"}


def _text(words) -> str:
    words = sorted(words, key=lambda w: (round(float(w.get("yMin")) / 3), float(w.get("xMin"))))
    return " ".join(w.text or "" for w in words).strip()


def pdf_entries(path: Path) -> list[dict]:
    """Read the PDF's three table columns without merging notification text into drug names."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("Install Poppler (pdftotext) to import the supplied banned-drug PDF.")
    result = subprocess.run(["pdftotext", "-bbox-layout", str(path), "-"],
                            capture_output=True, check=True)
    document = ET.fromstring(result.stdout)
    entries = []
    for page_number, page in enumerate(document.findall(".//x:page", NS), 1):
        words = page.findall(".//x:word", NS)
        serials = sorted((w for w in words if float(w.get("xMin")) < 90
                          and re.fullmatch(r"\d+\.", w.text or "")),
                         key=lambda w: float(w.get("yMin")))
        if not serials:
            continue
        # Pages after row 518 contain the legal footnotes, not further drug records.
        if entries and int(serials[0].text[:-1]) <= entries[-1]["row"]:
            break
        divider = 468 if page_number == 1 else 491
        first_y = float(serials[0].get("yMin")) - 2
        if entries:
            continuation = [w for w in words if float(w.get("yMin")) < first_y]
            entries[-1]["description"] += " " + _text([w for w in continuation if 110 <= float(w.get("xMin")) < divider])
            entries[-1]["notification"] += " " + _text([w for w in continuation if float(w.get("xMin")) >= divider])
        for i, serial in enumerate(serials):
            low = float(serial.get("yMin")) - 2
            high = float(serials[i + 1].get("yMin")) - 2 if i + 1 < len(serials) else float(page.get("height"))
            band = [w for w in words if low <= float(w.get("yMin")) < high]
            entries.append({
                "row": int(serial.text[:-1]), "page": page_number,
                "description": _text([w for w in band if 110 <= float(w.get("xMin")) < divider]),
                "notification": _text([w for w in band if float(w.get("xMin")) >= divider]),
                "footnotes": _text([w for w in band if re.fullmatch(r"[1-6]#", w.text or "")]),
            })
    if [e["row"] for e in entries] != list(range(1, 519)):
        raise ValueError("PDF extraction did not preserve all 518 numbered entries; import stopped")
    return entries


def banned_rows(entries: list[dict], known_salts: set[str]) -> tuple[list[dict], list[dict]]:
    rows, audit = [], []
    for entry in entries:
        text = entry["description"].strip()
        reason = None
        # The early entries include whole drug classes, single ingredients, exemptions,
        # paediatric/route restrictions and revoked suspensions. Do not flatten their scope.
        if entry["row"] < 95:
            reason = "class/single-ingredient/conditional restriction requires manual scope review"
        elif entry["footnotes"] or re.search(r"revok|quash|stay|suspend|sub.judice", entry["notification"], re.I):
            reason = "legal footnote or changed status; not an unconditional ban"
        elif re.search(r"\d|\b(?:except|with|for|kit|combikit|dispersible|coated|release|topical|external)\b", text, re.I):
            reason = "strength, route, release or other scope cannot be represented safely"
        clean = re.sub(r"^Fixed\s+Dose\s+Combinations?\s+(?:of\s+)?", "", text, flags=re.I)
        clean = re.sub(r"\b(?:tablets?|capsules?|syrup)\b", "", clean, flags=re.I).strip(" .")
        parts = [canonical_salt(p.strip(" .")) for p in clean.split("+")]
        if not reason and (len(parts) < 2 or any(p not in known_salts for p in parts) or len(set(parts)) != len(parts)):
            reason = "not an exact combination of ingredient names present in data1"
        notification = re.sub(r"\s+", "", entry["notification"])
        ref = re.search(r"(?:S\.?O\.?|G\.?S\.?R\.?)(?:No\.?)?(\d+)\(?E\)?", notification, re.I)
        date = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", notification)
        if not reason and (ref is None or date is None):
            reason = "notification reference/date not reliably extracted"
        if reason:
            audit.append({**entry, "imported": False, "reason": reason})
            continue
        form = "tablet" if re.search(r"\btablets?\b", text, re.I) else "capsule" if re.search(r"\bcapsules?\b", text, re.I) else "oral_liquid" if re.search(r"\bsyrup\b", text, re.I) else ""
        rows.append({"salts": "+".join(parts), "form": form,
                     "notification_ref": f"S.O. {ref.group(1)}(E); supplied PDF row {entry['row']}",
                     "notification_date": f"{date.group(3)}-{date.group(2)}-{date.group(1)}",
                     "source_url": f"data1/{BANNED}#page={entry['page']}"})
        audit.append({**entry, "imported": True, "salts": parts})
    return rows, audit


def price_rows(src: Path) -> tuple[list[dict], dict]:
    pm, pm_stats = load_pmbjp(src / PMBJP, "2026-11-13")
    dp, dp_stats = load_dpco(src / DPCO)
    rows = []
    for key in sorted(set(pm) | set(dp)):
        p, d = pm.get(key), dp.get(key)
        base = p or d
        rows.append({
            "composition": _composition(base["salts"]), "form": base["form"], "release": base["release"],
            "pmbj_name": p["pmbj_name"] if p else "", "pmbj_price": str(p["pmbj_price"]) if p else "",
            "pmbj_pack_qty": str(p["pmbj_pack_qty"]) if p else "", "pmbj_as_of": "2026-11-13" if p else "",
            "pmbj_source": f"data1/{PMBJP}; drug code {p['pmbj_code']}" if p else "",
            "nppa_ceiling_unit": str(d["nppa_ceiling_unit"]) if d else "",
            "nppa_as_of": _iso(d["nppa_effective"]) if d else "",
            "nppa_source": f"data1/{DPCO}; {d['nppa_order']}" if d else "",
        })
    return rows, {"pmbjp": pm_stats, "dpco": dp_stats}


def build_database(src: Path, target: Path) -> dict:
    """Validate/build before atomically replacing the target; keep the previous DB as a backup."""
    src, target = src.resolve(), target.resolve()
    for name in FILES:
        if not (src / name).is_file():
            raise FileNotFoundError(f"Required data1 source is missing: {name}")
    manifest = {name: {"sha256": hashlib.sha256((src / name).read_bytes()).hexdigest(),
                       "bytes": (src / name).stat().st_size} for name in FILES}
    entries = pdf_entries(src / BANNED)
    prices, price_stats = price_rows(src)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix="data1-build-") as work:
        built = Path(work) / "data1.db"
        conn = connect(built)
        try:
            # No dataset date was supplied for A-Z. Do not turn today's import date into its as-of.
            brands = load_az(conn, src / AZ, None, None)
            conn.execute("UPDATE brands SET source=?", (f"data1/{AZ}; snapshot date not supplied",))
            salts = set()
            for row in conn.execute("SELECT DISTINCT salts FROM brands"):
                salts.update(s["name"] for s in json.loads(row[0]))
            conn.executemany("INSERT INTO salts(name,synonyms,usual_use) VALUES (?, '[]', '[]')",
                             [(s,) for s in sorted(salts)])
            price_count = load_price_ref(conn, prices)
            banned, audit = banned_rows(entries, salts)
            banned_count = load_banned(conn, banned)
            counts = {"brands": brands, "salts": len(salts), "price_ref": price_count,
                      "banned_fdc": banned_count, "nsq_alerts": 0, "lasa_pairs": 0}
            metadata = {
                "brands": (None, f"data1/{AZ}; snapshot date not supplied"),
                "price_ref": ("2026-11-13", f"data1/{PMBJP} + data1/{DPCO}; dates retained per row"),
                "banned_fdc": ("2021-11-22", f"data1/{BANNED}; historical partial exact-combination import; legal footnotes excluded"),
                "nsq_alerts": (None, "Not supplied in data1"),
                "data1": (None, json.dumps(manifest, sort_keys=True)),
            }
            now = datetime.now().isoformat(timespec="seconds")
            for name, (as_of, source) in metadata.items():
                conn.execute("INSERT INTO datasets VALUES (?,?,?,?,?)",
                             (name, as_of, source, now, counts.get(name, 0)))
            if not brands or not price_count or not banned_count:
                raise ValueError("data1 import produced an empty required dataset")
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("data1 database integrity check failed")
        finally:
            conn.close()
        backup = None
        if target.exists():
            backup = target.with_name(f"{target.stem}.before-data1-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
            # SQLite backup handles a source DB that another process still has open.
            with sqlite3.connect(target) as old, sqlite3.connect(backup) as saved:
                old.backup(saved)
        report = {"db": str(target), "backup": str(backup) if backup else None,
                  "loaded_at": now, "sources": manifest, "counts": counts, **price_stats,
                  "banned_pdf": {"as_of": "2021-11-22", "total": len(audit), "imported": banned_count,
                                  "excluded": len(audit) - banned_count, "entries": audit},
                  "limitations": ["A-Z snapshot date is unknown; prices are supplied dataset values.",
                                  "Banned PDF is historical and partial; stale checks remain enabled.",
                                  "No indication, NSQ-batch or LASA dataset was supplied; none is invented."]}
        report_path = target.with_suffix(".import.json")
        report_file = Path(work) / "import.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        built.replace(target)
        report_file.replace(report_path)
    return {"db": str(target), "backup": str(backup) if backup else None,
            "report": str(report_path), **counts}
