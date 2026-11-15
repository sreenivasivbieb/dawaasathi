# Backend, voice and data1 audit — 2026-11-15

The subsequent website capture/playback repair, greeting fix and Enter-to-send behavior are in
[voice setup](voice-setup.md#website-follow-up-repair-2026-11-15). Its browser checks exercise native
recording/playback without Web Audio; the original checks below exercised the earlier PCM player.

## Layout and confirmed-selection follow-up

The header logo and history text now share a 24 px left alignment. A shared sidebar/header grid,
sticky header and bounded history panel keep these aligned while scrolling. Voice and composer
cards share top/bottom edges, padding and a common result width. The composer Send button sits
at the bottom, with a smaller hero and microphone treatment on shorter desktop screens.
Tablet/mobile layouts use the History drawer and stack the panels at narrow widths.

The additional screenshot was an existing saved result after the user explicitly selected Dioben
from a confirmation list. It was not an automatic acceptance of the low-confidence transcript.
The result now shows **Medicine selected / You selected**, with the earlier transcript underneath
as supporting detail. A confirmation marker is included in new results; older saved turns recover
it only when the recorded selection exactly matches a candidate and the resulting medicine ID.
Stored transcripts, medicine facts and histories are not rewritten.

Validation: **719 Python tests**, **5 JavaScript tests**, and Chromium layout checks at 320, 390,
768, 1024, 1280, 1502 and 1878 px. No horizontal overflow or page errors; desktop card edges and
logo/sidebar alignment checked. Mobile History open/close, explicit candidate selection, saved
history reopening and sticky header/sidebar were exercised. A fresh synthetic test conversation
was used and removed. Evidence: [layout checks](../artifacts/ui-alignment-check.json),
[compact desktop](../artifacts/ui-alignment-desktop-compact.png),
[mobile](../artifacts/ui-alignment-mobile.png),
[selected result](../artifacts/ui-alignment-selected-result.png).

The last commit authored by sreenivas is `29f76e6` (guardrails). The supplied final GitHub
repository's `upstream/main` was fetched at `3277d7e`; its tree matches local `31ee1c2` before
these repairs. This work is local and uncommitted. No remote branch, pull request or deployment
was changed.

## Findings and repairs

| Finding | Evidence | Repair |
| --- | --- | --- |
| Speech lost language-understanding and guarded phrasing after the working backend revision | Commit `0934ddb` added `not voice_request` conditions around NLU, phrasing and optional web helpers. Tests explicitly enforced that bypass. The earlier `29f76e6` backend used NLU for speech too. | Restored configured NLU and guarded phrasing for audio, stream and phone turns. Added regressions for recovering `Tellmisartan Salis` as Telmisartan 40. Medicine facts remain local. |
| New data had never been activated | The running `/healthz` reported 39 brands, 15 price references and one synthetic banned entry; SQLite metadata named DEV FIXTURE sources. | Rebuilt the default DB from the four data1 inputs, with source hashes and a previous-DB backup. Defaults and Docker now import data1. |
| Turning off web requests did not exclude old web-learned brands | `build_state` unconditionally called `load_learned`, even with web flags disabled. | Default `DATA1_ONLY=1` blocks web fact lookups and old learned-brand loading; refuses a legacy database; uses separate `var/data1-local.db` history. |
| Stale safety data erased useful known medicine information in the reply | The real Dolo response said full details were missing despite identifying its composition and prices. The word budget could drop prices while keeping only “2.2 times.” | Keep known composition, primary prices and the explicit unverified banned check. Do not emit a dependent detail after dropping its preceding price clause. The UI identifies a known medicine while the safety section stays unverified. |
| Full-dataset naming caused a wrong SR match | A-Z stores IR `Glycomet Tablet` with 500 mg in composition, alongside `Glycomet 500 SR Tablet`. Printed-number scoring incorrectly preferred SR for the prescription `Glycomet 500`. Actual photo comparison consequently missed the release difference. | Penalize unspoken modified-release markers while keeping ambiguous candidates close. The same real OCR upload now returns `RELEASE_MISMATCH`. |
| Busy or failed TTS could become apparent successful playback | Real overlapping checks reproduced Gnani HTTP 429: only one active request per key. REST TTS did not retry 429. Failed clauses could leave only silent gaps; audio routes still returned 200. | Bounded 429/5xx/network retries, Retry-After support, empty-audio rejection, no silence gaps after failure, HTTP 503 for failed audio and a WebSocket error. The browser's existing speech fallback can now detect failures. |

Both the old running process and direct provider probes had valid speech credentials. Real
English/Hindi STT and TTS succeeded before the repair: this was not simply a missing-key problem.
The earlier Git revision references `/app.js` but does not contain that file in its tracked tree;
the backend comparison does not establish that a clean checkout of that revision had a complete UI.

## Active data

Only these supplied raw files are used, without modification:

- `data1/archive/A_Z_medicines_dataset_of_India.csv`
- `data1/Product List_13_11_2026 @ 16_32_21.csv`
- `data1/dpco-2026-ceiling-prices.csv`
- `data1/Updated Banned Drugs List.pdf`

The database contains **246,003 active parseable medicine rows**, **1,774 ingredient names**,
**1,709 merged price references**, and **243 historical exact-combination banned-list rows**.
The A-Z input has 253,973 rows: 7,905 are discontinued and another 65 lack a parseable composition.
There is no 20,000-row cap and no seed prices, seed aliases, synthetic bans or synthetic batch alerts.

PMBJP: 2,453 input rows; 1,474 supported rows collapse to 1,448 formulation keys. Unsupported
groups, absent/zero prices and unparsed formulations are counted separately in the import report.
DPCO: 935 input rows; 486 supported rows collapse to 485 keys. Unsupported price units and
ambiguous formulations are excluded rather than guessed.

The PDF's heading explicitly states status **as of 22 November 2021**. It contains legal footnotes
for stayed, revoked, quashed and disputed notifications. All 518 numbered entries were extracted;
243 unambiguous ingredient combinations are imported, and all 275 excluded rows and reasons are
preserved in `var/dawasaathi.import.json`. The previously synthetic Aceclofenac + Paracetamol +
Rabeprazole example is not retained as an unconditional ban: the PDF marks it with legal footnote 5.
The real 2021 date is preserved, so the current banned check is **stale/unverified**.

A-Z has no supplied snapshot date; its date is left unknown. PMBJP uses the date in the supplied
filename, and DPCO effective dates are retained per formulation. Detailed indications, NSQ batches
and LASA pairs were not supplied. The app reports unknown uses instead of inventing clinical facts
or fetching other data. Reported A-Z prices are dataset values, not verified current retail prices.

The importer builds and validates a temporary SQLite DB before replacing the destination. The
original DB is backed up at
`var/dawasaathi.before-data1-20261115-123633-525316.db`. Existing old history is preserved in its
original file. The source hashes and complete import audit are in `var/dawasaathi.import.json`.

## Verification

- **711 Python tests passed**, including spoken-number recovery across audio/stream/phone,
  data isolation, import atomicity, historical PDF treatment, full-data naming, response wording,
  speech rate limits, empty replies and failed-audio HTTP status.
- **2 JavaScript tests passed**, covering the microphone worklet and the known-medicine title
  with unverified historical safety data. Compilation and Git whitespace checks passed.
- **8 live runtime checks passed** against the default data1 database with configured real providers:
  typed dataset prices; medicine follow-ups; misheard-name/number interpretation; ambiguity;
  confirmation; actual synthetic PNG uploads through configured OCR; and English/Hindi STT-to-answer-to-TTS.
  Returned audio was nonempty and contained nonzero PCM. Test-created threads were removed.
  Evidence: [live checks](../artifacts/data1-live-checks.json).
- The original WebSocket endpoint was exercised with synthetic Hindi PCM against live Prisma,
  through the restored interpretation pipeline to a Dolo result and 241,920 bytes of streamed
  reply audio. Evidence: [stream check](../artifacts/data1-stream-check.json).
- A real Chromium capture used a synthetic Hindi microphone recording, AudioWorklet and live
  Prisma/Timbre. It verified capture, detected-language routing, a running audio context, nonempty
  streamed audio, playback start, Stop targeting the correct audio ID, and zero page errors.
  Evidence: [browser voice report](../artifacts/data1-browser-voice-check.json) and
  [screenshot](../artifacts/data1-browser-voice-check.png).

These checks do not verify a physical microphone, audible speaker output, every language/accent,
clinical correctness, Docker execution, public deployment or the remote GitHub version.

## Run and repeat

The corrected local app runs at **http://127.0.0.1:8004**. The pre-existing process at port 8001
still holds its earlier in-memory fixture data; do not use it to evaluate these repairs.

```bash
# Build exclusively from data1 (backs up any previous target database).
.venv/bin/python -m scripts.load_data --src data1

# Start after stopping any instance already using this port.
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8004

# Run live checks serially: Gnani permits one active speech request per key.
.venv/bin/python -m scripts.check_data1_runtime --base-url http://127.0.0.1:8004

.venv/bin/python -m pytest -q
node --test tests/web/*.test.mjs
```

The live check generates synthetic speech using the configured provider, uses the synthetic
sample images, and deletes only the threads it creates. It does not record the physical microphone.
