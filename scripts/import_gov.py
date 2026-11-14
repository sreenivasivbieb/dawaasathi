"""Convert the official price lists into price_ref.csv (format: data/README.md).

  uv run python -m scripts.import_gov \
      --pmbjp "data/raw/pmbjp-product-list-2026-11-13.csv" --pmbjp-as-of 2026-11-13 \
      --dpco data/raw/dpco-2026-ceiling-prices.csv --out data/gov/price_ref.csv

PMBJP (Jan Aushadhi) product list: "Generic Name" holds composition + form in free text, e.g.
  "Amoxycillin 500mg and Potassium Clavulanate 125mg Tablets IP", "Paracetamol Tablets IP 650 mg",
  "Cetirizine Syrup IP 5 mg per 5 ml"; "Unit Size" is "10's" / "60 ml"; MRP 0 = not on sale (skipped).
DPCO ceiling list: "Amoxicillin (A) + Clavulanic acid (B)" / "Tablet 500 mg (A) + 125 mg (B)" /
  "1 Tablet" / ceiling per unit excluding GST, with the gazette order per row.

Rows are keyed on salt set + per-salt strength + form + release (the same key the engine uses);
for each key the cheapest Jan Aushadhi product per unit is kept. Prints coverage counts.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.salts import (canonical_form, canonical_release, parse_composition, release_class,  # noqa: E402
                              salt_key, strength_key)
from app.models import SaltDose  # noqa: E402

SKIP_GROUPS = {"surgical & medical consumables", "nutraceuticals", "ayurvedic", "mosquito repellent",
               "smoking cessation", "footcare cream", "throat spray for freshness"}
# Forms where a per-unit price comparison makes sense for a spoken query.
KEEP_FORMS = {"tablet", "capsule", "oral_liquid", "drops", "injection", "powder", "inhaler", "eye_drops",
              "ear_drops", "nasal", "cream", "ointment", "gel", "lotion"}

_NOISE = re.compile(
    r"\b(tablets?|capsules?|caplets?|soft\s*gel(?:atin)?|softgel|hard\s*gelatin|film[- ]coated|chewable|"
    r"dispersible|orally\s*disintegrating|mouth\s*dissolving|effervescent|oral|paediatric|pediatric|"
    r"suspension|syrup|dry\s*syrup|solution|drops?|injection|infusion|intravenous|for|i\.?v\.?|"
    r"sustained|prolonged|extended|modified|controlled|delayed|immediate|release|gastro[- ]?resist\w*|"
    r"enteric[- ]?coated|enteric|coated|ip|bp|usp|nfi|sr|er|xr|cr|pr|mr|dr|ec|with|in|a|flavoured|base|"
    r"powder|granules|sachet|inhalation|rotacaps?|respules?|cream|ointment|gel|lotion|eye|ear|nasal|spray)\b",
    re.I)


def _float(x: str | None) -> float | None:
    try:
        return float(str(x).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def _pack(unit_size: str, form: str | None) -> tuple[float | None, str | None]:
    s = (unit_size or "").strip().lower()
    m = re.match(r"(\d+(?:\.\d+)?)\s*'?s\b", s)  # "10's", "100's in bottle"
    if m:
        return float(m.group(1)), "capsules" if form == "capsule" else "tablets"
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", s)
    if m:
        return float(m.group(1)), "ml"
    if "vial" in s or "ampoule" in s or s in ("1's", "kit"):
        return 1.0, "unit"
    return None, None


def parse_pmbjp_name(name: str) -> tuple[list[SaltDose], str | None, str | None]:
    """'Metformin Hydrochloride 500mg (Sustained Release) and Voglibose 0.2mg Tablets' -> salts, form, release."""
    form = canonical_form(name)
    release = canonical_release(name)
    text = re.sub(r"\([^)]*\)", " ", name)  # "(Sustained Release)", "(Enteric Coated)"
    # Liquids: "... per 5 ml" / "per 5ml" / "per ml" applies to every salt.
    per_ml = None
    m = re.search(r"\bper\s*(\d+(?:\.\d+)?)?\s*ml\b", text, re.I)
    if m:
        per_ml = float(m.group(1)) if m.group(1) else 1.0
        text = text[: m.start()] + text[m.end():]
    text = re.sub(r"(\d)\s*(mg|mcg|g|gm|iu)\b", r"\1\2", text, flags=re.I)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    # Single salt with the strength at the end: "Paracetamol 650mg" already; "Paracetamol  650mg" fine.
    salts = parse_composition(text)
    if not salts or any(s.amount is None for s in salts):
        return [], form, release
    if any(len(s.name.split()) > 4 or not re.search(r"[a-z]{3}", s.name) for s in salts):
        return [], form, release
    if per_ml and form in ("oral_liquid", "drops", "injection"):
        for s in salts:
            if s.per_ml is None:
                s.per_ml = per_ml
    return salts, form, release


def load_pmbjp(path: Path, as_of: str) -> tuple[dict, dict]:
    best: dict[tuple, dict] = {}
    stats = {"rows": 0, "skipped_group": 0, "skipped_no_price": 0, "unparsed": 0, "kept": 0}
    unparsed: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            stats["rows"] += 1
            if (r.get("Group Name") or "").strip().lower() in SKIP_GROUPS:
                stats["skipped_group"] += 1
                continue
            mrp = _float(r.get("MRP"))
            if not mrp or mrp <= 0:
                stats["skipped_no_price"] += 1
                continue
            name = (r.get("Generic Name") or "").strip()
            salts, form, release = parse_pmbjp_name(name)
            qty, unit = _pack(r.get("Unit Size", ""), form)
            if not salts or form not in KEEP_FORMS or not qty:
                stats["unparsed"] += 1
                unparsed.append(name)
                continue
            sk, stk = salt_key(salts), strength_key(salts, form)
            if stk is None:
                stats["unparsed"] += 1
                unparsed.append(name)
                continue
            key = (sk, stk, form, release_class(release))
            row = {"salts": salts, "form": form, "release": release or "IR",
                   "pmbj_name": f"Jan Aushadhi {name}", "pmbj_price": mrp, "pmbj_pack_qty": qty,
                   "pmbj_code": r.get("Drug Code"), "unit": unit}
            cur = best.get(key)
            if cur is None or mrp / qty < cur["pmbj_price"] / cur["pmbj_pack_qty"]:
                best[key] = row
            stats["kept"] += 1
    stats["unparsed_examples"] = unparsed[:15]
    stats["keys"] = len(best)
    return best, stats


_UNIT_OK = re.compile(r"^1\s*(tablet|capsule|ml)$", re.I)


def load_dpco(path: Path) -> tuple[dict, dict]:
    out: dict[tuple, dict] = {}
    stats = {"rows": 0, "kept": 0, "skipped_unit": 0, "unparsed": 0}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            stats["rows"] += 1
            unit = (r.get("Unit") or "").strip()
            if not _UNIT_OK.match(unit):
                stats["skipped_unit"] += 1
                continue
            drug = r.get("Drug") or ""
            inner = re.search(r"\[(.*)\]", drug)
            drug = inner.group(1) if inner else drug
            names = [re.sub(r"\([A-Z]\)", "", n).strip() for n in drug.split("+")]
            names = [re.sub(r"\(.*?\)", "", n).strip() for n in names if n.strip()]
            dosage = r.get("Dosage form and strength") or ""
            if "/" in dosage.split()[0] if dosage.split() else False:
                stats["unparsed"] += 1  # "Conventional/Effervescent/..." -> release ambiguous
                continue
            form = canonical_form(dosage)
            release = canonical_release(re.sub(r"\(p\)", "", dosage)) or "IR"
            doses = parse_composition(" + ".join(f"x {d}" for d in re.findall(
                r"\d+(?:\.\d+)?\s*(?:mg|mcg|g|iu)(?:\s*/\s*\d*\s*ml)?", dosage, re.I)))
            if form not in ("tablet", "capsule", "oral_liquid", "drops") or len(doses) != len(names) or not names:
                stats["unparsed"] += 1
                continue
            salts = [SaltDose(name=parse_composition(n)[0].name if parse_composition(n) else n.lower(),
                              amount=d.amount, unit=d.unit, per_ml=d.per_ml) for n, d in zip(names, doses)]
            stk = strength_key(salts, form)
            if stk is None:
                stats["unparsed"] += 1
                continue
            key = (salt_key(salts), stk, form, release_class(release))
            out[key] = {"salts": salts, "form": form, "release": release,
                        "nppa_ceiling_unit": _float(r.get("Ceiling price 2026 (Rs ex-GST)")),
                        "nppa_order": (r.get("Gazette order") or "").strip(),
                        "nppa_effective": (r.get("Effective from") or "").strip()}
            stats["kept"] += 1
    stats["keys"] = len(out)
    return out, stats


def _iso(d: str) -> str:
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", d or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else d


def _composition(salts: list[SaltDose]) -> str:
    parts = []
    for s in salts:
        amt = f"{s.amount:g}{s.unit}"
        if s.per_ml:
            amt += f"/{s.per_ml:g}ml"
        parts.append(f"{s.name.title()} {amt}")
    return " + ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pmbjp", type=Path, required=True)
    ap.add_argument("--pmbjp-as-of", required=True, help="YYYY-MM-DD the product list was downloaded")
    ap.add_argument("--dpco", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "gov" / "price_ref.csv")
    args = ap.parse_args()

    pm, pm_stats = load_pmbjp(args.pmbjp, args.pmbjp_as_of)
    dp, dp_stats = load_dpco(args.dpco)
    keys = sorted(set(pm) | set(dp))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["composition", "form", "release", "pmbj_name", "pmbj_price", "pmbj_pack_qty", "pmbj_as_of",
              "pmbj_source", "nppa_ceiling_unit", "nppa_as_of", "nppa_source"]
    both = 0
    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for key in keys:
            p, d = pm.get(key), dp.get(key)
            both += bool(p and d)
            base = p or d
            w.writerow({
                "composition": _composition(base["salts"]), "form": base["form"], "release": base["release"],
                "pmbj_name": p["pmbj_name"] if p else "", "pmbj_price": p["pmbj_price"] if p else "",
                "pmbj_pack_qty": p["pmbj_pack_qty"] if p else "",
                "pmbj_as_of": args.pmbjp_as_of if p else "",
                "pmbj_source": f"PMBJP product list (drug code {p['pmbj_code']})" if p else "",
                "nppa_ceiling_unit": d["nppa_ceiling_unit"] if d else "",
                "nppa_as_of": _iso(d["nppa_effective"]) if d else "",
                "nppa_source": f"NPPA DPCO ceiling, {d['nppa_order']}" if d else "",
            })
    meta = {"as_of": args.pmbjp_as_of,
            "source": f"PMBJP product list ({args.pmbjp_as_of}) + NPPA DPCO 2026 ceiling prices (S.O. 1575(E) etc.)"}
    (args.out.parent / "price_ref.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "rows": len(keys), "pmbjp_and_nppa": both,
                      "pmbjp": pm_stats, "dpco": dp_stats}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
