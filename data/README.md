# Data

## Active runtime: data1 only

Run `uv run python -m scripts.load_data --src data1` from the repository root. This is also the
default source and Docker build source. `DATA1_ONLY=1` prevents startup against a legacy fixture
database and disables external medicine facts, including previously learned web brands.

The import uses only the supplied A-Z CSV (all active parseable rows), PMBJP product-list CSV,
DPCO ceiling CSV and banned-drug PDF. It builds atomically, saves a backup of the previous DB,
and writes `<db-name>.import.json` with SHA-256 hashes, row counts and all PDF exclusions.
Poppler's `pdftotext` is required. The raw inputs are unchanged.

The PDF states status **as of 2021-11-22**. Legal footnotes include stayed/quashed/revoked entries;
these and unrepresentable restrictions are excluded from exact combination matching and retained
in the import report. The actual old date is preserved, so the app reports the list as stale.
A-Z has no supplied snapshot date; it is left unknown. PMBJP's filename supplies 2026-11-13 and
DPCO effective dates are retained per row. No indications, batch alerts, LASA pairs, aliases,
synthetic prices or synthetic bans are added. Test fixtures remain available only for tests.

## Legacy normalized CSV importer (DATA1_ONLY=0)

Everything the verdict engine knows comes from CSV files in one directory, loaded into a single
SQLite file by `scripts/load_data.py`:

```bash
uv run python -m scripts.load_data --src data/seed_dev            # dev fixture (default)
uv run python -m scripts.load_data --src data/real                # your verified data
uv run python -m scripts.load_data --src data/real \
    --az data/raw/A_Z_medicine_dataset_of_India.csv --az-as-of 2026-11-01 --az-limit 20000
```

The DB goes to `DAWA_DB_PATH` (default `var/dawasaathi.db`; `--db PATH` overrides). It is rebuilt
from scratch on every run unless you pass `--keep` (append). The script prints row counts per table.
Restart the server after reloading: the DB is read into memory at startup.

> [!WARNING]
> **`data/seed_dev` is a DEV FIXTURE, not real data.**
>
> - MRPs and pack sizes are approximate and unverified.
> - The PMBJP (Jan Aushadhi) and NPPA ceiling values are placeholders.
> - The single banned-combination row (`Aceclofenac + Paracetamol + Rabeprazole`, tablet) is
>   **synthetic**. Its reference reads `DEV FIXTURE - not a real notification`. The
>   `Saathi-Demo FDC Tablet` brand exists only to trigger it.
> - The NSQ batch `DEMO123` is synthetic.
>
> Before recording the demo or giving judges the public URL, replace it with verified data, each
> file with its source and as-of date:
> 1. **Banned FDC list**: the official Government of India gazette notifications banning fixed-dose
>    combinations. One row per combination, with the notification reference and date, checked against
>    the source.
> 2. **PMBJP product list**: Jan Aushadhi product names, MRPs and pack sizes.
> 3. **NPPA ceiling price list**: per-unit ceiling prices for scheduled formulations.
> 4. **Brand DB**: your licensed brand list or the Kaggle import below, with its licence and date.
>
> Put the files in `data/real/` (same names as below) and build with `--src data/real`. The
> Docker image builds the DB with `--build-arg DATA_DIR=data/real`.

## How the data is used

| Data | Used for | When missing or stale |
|---|---|---|
| `brands` | Name matching, salts / strength / form / release for the verdict, MRP, cheaper brands | The drug can't be identified, so the reply asks for a photo or a repeat |
| `salts` | Synonyms (`amoxycillin` → `amoxicillin`), usual use for the symptom check | Synonyms aren't merged; no symptom flag |
| `aliases` | Extra spoken or Devanagari spellings for the matcher | Matching relies on transliteration only |
| `price_ref` | Price gap vs Jan Aushadhi, NPPA ceiling check, the Jan Aushadhi alternative | "No price data". No Jan Aushadhi alternative |
| `banned_fdc` | `BANNED_COMBINATION` check on the order-free salt set + form | Every multi-salt drug is reported `UNVERIFIED` (never "safe") |
| `nsq_alerts` | Batch-number alert from strip OCR (stretch feature) | No batch flag |
| `lasa_pairs` | Look-alike / sound-alike names, which always trigger a confirmation question | No LASA flag |
| `datasets.json` | Source + as-of shown in every response (`data_sources`) and the staleness check | as-of unknown, so the data is treated as stale |

Staleness: data whose as-of date is older than `DATA_STALE_DAYS` (default 400) is treated as
unknown. A stale banned list makes the check `UNVERIFIED`. A stale PMBJP / NPPA row is not used for a
price claim.

## File formats

All files are UTF-8 CSV with a header row; column order does not matter, extra columns are
ignored, empty cells mean "unknown". Prices are in rupees (a `₹` sign or `,` separators are
stripped). Dates are ISO `YYYY-MM-DD`.

### Compositions

Wherever a column holds a composition, it is parsed by `app.engine.salts.parse_composition`:

- salts are separated by `+`, `;`, ` and ` or `,`;
- each salt is `name amount unit`, unit one of `mg mcg g iu %` (also `µg`, `gm`, `I.U.`);
- liquids give the strength per volume: `Paracetamol 250mg/5ml` or `Amoxycillin 400mg/5ml`;
- salt names are canonicalised: case-folded, synonyms from `salts.csv` applied, suffixes such as
  `IP`, `hydrochloride`, `trihydrate`, `sodium` and `potassium` dropped, and `X eq. to Y 500 mg`
  read as `Y 500 mg`.

Brand totals such as "625" are never a salt strength: write `Amoxycillin 500mg + Clavulanic Acid 125mg`.

### Forms and release classes

`form` free text is mapped to: `tablet`, `capsule`, `oral_liquid` (syrup, suspension, dry syrup,
oral solution), `drops`, `injection`, `inhaler`, `cream`, `ointment`, `gel`, `powder`, `lotion`,
`eye_drops`, `ear_drops`, `nasal`, `topical_wash`. If `form` is empty it is inferred from the brand
name.

`release` is one of `IR` (immediate), `SR` (sustained), `ER` (extended: also XR/XL/CR/PR/OD) or
`DR` (delayed / enteric-coated / gastro-resistant). If empty it is inferred from the brand name
(`Glycomet SR 500` → `SR`), else `IR`.

### `brands.csv` (required)

| column | required | meaning |
|---|---|---|
| `name` | yes | Brand name as printed, including the strength number and suffix: `Augmentin 625 Duo Tablet` |
| `composition` | yes | Per-unit composition (see above). Rows that don't parse are skipped |
| `form` | no | Dosage form (see above) |
| `release` | no | `IR` / `SR` / `ER` / `DR` |
| `pack_qty` | no | Units per pack: 10 (tablets), 60 (ml) |
| `pack_unit` | no | `tablets`, `capsules`, `ml`, … |
| `mrp` | no | MRP of the whole pack in ₹. Used per unit (`mrp / pack_qty`) |
| `manufacturer` | no | Shown in the response |
| `scheduled` | no | `1` if the formulation is under NPPA price control (enables the "above ceiling" check) |
| `source` | no | Per-row source; defaults to `datasets.json` → `brands.source` |
| `updated_at` | no | Per-row as-of; defaults to `brands.as_of` |

The matcher's brand "family" and "base" come from the name: the words before the first number,
form word or release suffix (`Glycomet SR 500 Tablet` → base `glycomet`; `Moxikind-CV 625` →
`moxikind-cv`). Keep names the way people say them.

```csv
name,composition,form,release,pack_qty,pack_unit,mrp,manufacturer,scheduled
Augmentin 625 Duo Tablet,Amoxycillin 500mg + Clavulanic Acid 125mg,tablet,IR,10,tablets,223.40,GlaxoSmithKline,1
Calpol 250 Suspension,Paracetamol 250mg/5ml,suspension,IR,60,ml,52.00,GlaxoSmithKline,1
Glycomet SR 500 Tablet,Metformin 500mg,tablet,SR,20,tablets,40.00,USV,1
```

### `salts.csv`

| column | meaning |
|---|---|
| `name` | Canonical salt name (lowercase is used) |
| `synonyms` | `|`-separated alternative names: `amoxycillin|amoxicilin|amoxycillin trihydrate` |
| `usual_use` | `|`-separated tags for the soft symptom check. Tags with spoken symptom words: `fever pain infection cold cough allergy diabetes bp acidity thyroid cholesterol asthma diarrhoea vomiting vitamin` |

```csv
name,synonyms,usual_use
paracetamol,acetaminophen|pcm,fever|pain
clavulanic acid,potassium clavulanate|clavulanate|clavulanate potassium,infection
```

### `aliases.csv`

| column | meaning |
|---|---|
| `brand` | A full product name (exact, case-insensitive) attaches the alias to **that SKU only**. A family name (`Glycomet`) attaches it to **every SKU of the family**; prefer this |
| `alias` | Another spelling people (or Prisma) use: Devanagari `ऑगमेंटिन`, a misspelling `ogmentin` |

```csv
brand,alias
Augmentin,ऑगमेंटिन
Glycomet,ग्लाइकोमेट
```

### `price_ref.csv`

One row per salt + strength + form + release with its Jan Aushadhi price and/or NPPA ceiling.

| column | meaning |
|---|---|
| `composition` | Composition (see above); matched on salt set + per-salt strength |
| `form`, `release` | As above; `release` defaults to `IR` |
| `pmbj_name` | Jan Aushadhi product name (spoken as the cheapest alternative) |
| `pmbj_price` | Jan Aushadhi MRP for the pack (₹) |
| `pmbj_pack_qty` | Units in that pack (prices are compared per unit) |
| `pmbj_as_of`, `pmbj_source` | Date and source of the PMBJP product list |
| `nppa_ceiling_unit` | NPPA ceiling price **per unit, excluding GST** (₹). Compared as ceiling × (1 + `NPPA_GST_PCT`/100), only for `scheduled=1` brands |
| `nppa_as_of`, `nppa_source` | Date and source of the NPPA notification / list |

```csv
composition,form,release,pmbj_name,pmbj_price,pmbj_pack_qty,pmbj_as_of,pmbj_source,nppa_ceiling_unit,nppa_as_of,nppa_source
Paracetamol 650mg,tablet,IR,Jan Aushadhi Paracetamol 650,12.00,10,2026-10-01,PMBJP product list,2.04,2026-04-01,NPPA ceiling list
```

### `banned_fdc.csv`

One row per banned fixed-dose combination.

| column | required | meaning |
|---|---|---|
| `salts` | yes | Salts separated by `+`; order doesn't matter (stored sorted, canonicalised) |
| `form` | no | Restrict to one dosage form; empty = any form |
| `strengths` | no | Composition with strengths, if the notification bans only those strengths: `Aceclofenac 100mg + Paracetamol 325mg` |
| `notification_ref` | yes | The gazette notification reference, quoted in the reply and the Speak-to-Chemist script |
| `notification_date` | yes* | Date of the notification (spoken as "list date") |
| `source_url` | no | Link to the notification |

Matching is on the exact salt set, never on the brand name. If the form or strength of the drug is
unknown but the salts match a row, the result is `possible`, which is reported as `UNVERIFIED`
with a warning flag.

```csv
salts,form,strengths,notification_ref,notification_date,source_url
Salt A + Salt B,tablet,,<gazette notification no.>,<YYYY-MM-DD>,<url>
```

### `nsq_alerts.csv` (stretch)

| column | meaning |
|---|---|
| `drug` | Drug description as in the alert (`Paracetamol Tablets IP 650 mg`); a salt or brand word must appear in it |
| `batch_no` | Batch number; compared to the OCR'd batch ignoring case and punctuation |
| `manufacturer`, `alert_month` (`YYYY-MM`), `source_url` | Shown with the flag |

### `lasa_pairs.csv`

| column | meaning |
|---|---|
| `brand_a`, `brand_b` | Lowercase brand family or salt names that look or sound alike (`linagliptin,linezolid`) |
| `similarity` | Informational (0–1) |

A match on either side always triggers a confirmation question, until the user confirms that brand
in the session.

### `datasets.json`

Source and as-of date per dataset. They are shown in API responses and drive the staleness check.

```json
{
  "brands":     {"as_of": "2026-11-01", "source": "A-Z Medicine Dataset of India (Kaggle), licence ..."},
  "price_ref":  {"as_of": "2026-10-01", "source": "PMBJP product list + NPPA ceiling list"},
  "banned_fdc": {"as_of": "2026-11-12", "source": "MoHFW gazette notifications, checked against source on ..."},
  "nsq_alerts": {"as_of": "2026-11-12", "source": "CDSCO monthly NSQ alerts"}
}
```

For `banned_fdc`, if `as_of` is missing, the latest `notification_date` is used.

## Kaggle "A-Z Medicine Dataset of India"

`--az PATH` appends brands from that CSV (columns `id, name, price(₹), Is_discontinued,
manufacturer_name, type, pack_size_label, short_composition1, short_composition2`):

- discontinued rows (`Is_discontinued = TRUE`) are skipped;
- composition = `short_composition1 + short_composition2`; rows that don't parse are skipped;
- form and release are inferred from the name / pack label; `pack_size_label` ("strip of 10
  tablets") gives `pack_qty` / `pack_unit`; `price(₹)` becomes the MRP;
- `scheduled` is set to 0 (no NPPA ceiling flag for these rows);
- `--az-as-of YYYY-MM-DD` records the snapshot date (default: today); `--az-limit N` stops after N
  loaded rows (≈10k–20k keeps startup fast and the matcher snappy);
- the brands source in `datasets.json` gets " + A-Z Medicine Dataset of India" appended.

Keep the CSV in `data/raw/` (git-ignored). Check the dataset's licence before redistributing it.
The prices are a web snapshot, not verified MRPs.

## Synthetic data only

Per the challenge rules, the repo and the demo use only synthetic material: drawn strips and
prescriptions (`scripts/make_samples.py`), Timbre-generated eval audio (`scripts/gen_eval_set.py`),
and no real phone numbers, recordings, Aadhaar or PAN.
