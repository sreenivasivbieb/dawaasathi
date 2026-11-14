# Backend alignment and user-flow verification

The website now represents the medicine assistant implemented by the backend. It offers typed
questions, photo uploads (strip, prescription, or both), candidate confirmation,
conversation history, answer playback, and chemist scripts. The current UI uses a mint gradient
scene, translucent panels and a prominent voice orb inspired by the supplied reference. The
medicine checker stays focused; it does not include appointment or clinic-record workflows.
The home-medicine manager, complaint drafts, example questions, numbered steps, and technical
provider badges remain removed. The voice panel stays visible with a setup explanation when
Gnani is not configured. Supporting composition, price and dataset details are collapsed.
See [voice setup and language verification](voice-setup.md) for configuration and limitations.

Removed the receptionist/call simulation, appointment dates, clinic-record approval/sync button,
seeded conversations, fabricated fallback prices, inferred patient diagnoses, and unsupported
"verified safe" claims. Results are rendered from the server's QueryResponse. Demo data is clearly
labelled; alternatives retain the backend's order and source labels.

## Confirmed problems and repairs

| Problem observed | Cause | Change |
| --- | --- | --- |
| Actual Augmentin sample photo was unverified | OCR joined brand and strength into AUGMENTIN625 | Separate letter/number boundaries before matching |
| Prescription comparison could not resolve the prescribed medicine | Local OCR discarded the parsed prescription brand; prescription text could mix medicine lines | Keep each prescription medicine's name and raw text separate; use RapidOCR for prescriptions as well as strips |
| IR/SR pack name was missed | OCR used GLYCOMET-SR500 | Normalize release suffix separators before matching |
| "What is it used for?" lost the preceding medicine | Only symptom follow-ups reused context | Reuse the current medicine for unnamed use, price, alternative and related follow-ups; a new named medicine still takes precedence |
| Candidate/history controls did nothing | Missing click handlers | Wire confirmations, none-of-these, history open/delete and result actions to the corresponding endpoints |
| Failed requests lost input or showed placeholder content | Client cleared data early and supplied invented fallbacks | Keep failed query input/photos, show request errors, and show drafts/scripts only from an actual result |
| Voice input appeared available without STT | Client ignored provider configuration | Disable microphone when STT is not configured; report its availability |
| Malformed WebSocket requests disconnected abruptly | Unchecked JSON types and sample-rate conversions | Validate messages and return protocol errors; allow the next valid message to succeed |
| Concurrent stream turns could race or outlive the connection | Untracked background tasks | Bound and serialize turns, apply rate limiting, and cancel work on disconnect |
| Concurrent TTS cache writes could collide | Shared temporary filename for identical clauses | Use a unique temporary file for each writer |
| Expired audio remained accessible until another job was created | Expiry was checked only on creation | Collect/cancel expired jobs on lookup as well |
| Stale offline pages/assets could remain misleading | Old shell identity and HTML fallback for missing JS assets | Update cache version, scope cache cleanup and limit HTML fallback to navigations |

## What was actually exercised

- **47/47 live client checks:** real HTTP and WebSocket calls to a running local Uvicorn server;
  actual PNG sample uploads through the OCR adapter; typed lookup, follow-up, duplicate ingredient,
  confirmation/rejection, unknown medicine, matching photos, release and concentration mismatches,
  synthetic banned/batch examples, history/resume/delete, chemist/audio/draft endpoints, all 11 reply
  languages, and malformed/missing/oversized inputs.
- **17/17 DOM interaction checks:** the actual website module and markup with requests sent to
  that server; confirmations, history, language selection, chemist expansion, actual photo uploads and
  comparisons, collapsed supporting details, removal of secondary tools, and recovery from an
  injected network failure.
- **687 Python tests passed**, including new regressions for the failures repaired here.
- JavaScript syntax and Git whitespace checks passed.

Machine-readable results: [live user flows](../artifacts/user-flow-checks.json) and
[DOM actions](../artifacts/web-action-checks.json).

These are separate from visual or device validation. The original DOM checks did not verify rendering. Subsequently, user-authorized local Chromium
captured and verified representative desktop/mobile screens and the real sample-image comparison;
see [screenshot provenance](screenshots/README.md). Physical microphone/camera capture and audible
playback remain unverified. DOM checks do not establish those device results.

The running configuration reported **STT mock, TTS mock, OCR rapidocr**. No adapter was replaced
with a test fake for the live checks, but live speech providers were not configured or verified.
The WAV endpoint check verifies file output, not intelligible speech from the mock synthesizer.
The loaded medicine/pricing/banned/batch datasets are **DEV FIXTURE** material: their synthetic
prices and alerts are not validated medical or regulatory information. See [data guidance](../data/README.md).
Persistent history is separate from the session's short-term medicine/confirmation context, which
expires after 15 minutes by default. Reopening old history displays a saved result; it is labelled
as such and a fresh named question can be submitted.

## Run the updated application

From the repository root:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open <http://127.0.0.1:8001>. Restart any already-running backend process to load the Python changes.
Existing runtime databases and saved conversations were not rebuilt or removed.

## Repeat the live checks

Use an isolated history database and audio cache. Only synthetic threads created by these checks
are deleted. The script expects the demo brands in the medicine database; do not run its demo
expectations against unrelated production datasets.

```bash
LOCAL_DB_PATH=/tmp/dawa-check.db DAWA_CACHE_DIR=/tmp/dawa-check-audio RATE_LIMIT_PER_MIN=1000 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

In another terminal:

```bash
.venv/bin/python scripts/check_user_flows.py --base-url http://127.0.0.1:8001 \
  --output artifacts/user-flow-checks.json
npm install --prefix /tmp/dawa-ui-check jsdom
JSDOM_MODULE=/tmp/dawa-ui-check/node_modules/jsdom/lib/api.js \
  DAWA_WEB_REPORT=artifacts/web-action-checks.json node scripts/check_web_actions.mjs
.venv/bin/python -m pytest -q
```
