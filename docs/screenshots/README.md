# Project screenshots

These PNGs were captured from the actual local FastAPI-served website in headless Chromium.
The UI and API were not replaced with a visual mockup, and no generated illustration stands in
for a project screenshot.

| File | Screen |
| --- | --- |
| `desktop.png` | Desktop home screen with voice panel, upload form and history sidebar. |
| `medicine-result.png` | Actual typed Dolo lookup against the demo database. |
| `prescription-comparison.png` | Result card after uploading `rx_glycomet_500.png` and `strip_glycomet_sr_500.png`; the backend reports a release mismatch. |
| `hindi.png` | Interface switched to Hindi, preserving original user-entered history titles. |
| `mobile.png` | Full page at 390 px width, with History available as a drawer. |

Desktop viewport: **1440 × 1100**. Mobile viewport: **390 × 844**. Reduced motion was enabled.
The isolated screenshot server used demo medicine data, local OCR, no speech/search credentials,
and no optional text-model calls. The voice setup state is intentional; these images do not claim
a live microphone session. Spoken replies were switched off during the result captures.

`capture.json` records the capture time, configuration and browser checks. This run reported
zero uncaught page errors and no mobile horizontal overflow. All five images were visually
inspected. These checks do not establish physical microphone capture or audible playback.

## Reproduce

Run from the repository root. Start an isolated screenshot instance, keeping existing runtime
history and caches separate:

```bash
GNANI_API_KEY= TAVILY_API_KEY= LLM_MODE=off WEB_LOOKUP=0 WEB_BRANDS=0 \
OCR_PROVIDERS=rapidocr,tesseract \
LOCAL_DB_PATH=/tmp/dawa-readme-history.db DAWA_CACHE_DIR=/tmp/dawa-readme-audio \
RATE_LIMIT_PER_MIN=1000 \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8004
```

The disabled helper flags and empty speech/search variables prevent an existing `.env` from enabling external calls
for this documentation capture. No private history is imported into the fresh browser context.

In another terminal, install the screenshot tool separately from the app if needed:

```bash
npm install --prefix /tmp/dawa-docs playwright
PLAYWRIGHT_MODULE=/tmp/dawa-docs/node_modules/playwright/index.mjs \
CHROMIUM_PATH=/usr/bin/chromium \
DAWA_DOCS_URL=http://127.0.0.1:8004 \
node scripts/capture_readme.mjs
```

Set `CHROMIUM_PATH` to an installed Chromium executable on your system. The script overwrites
these documentation PNGs and removes only the synthetic conversation IDs it creates. This is
an optional documentation tool; Node.js and Playwright are not application runtime requirements.
