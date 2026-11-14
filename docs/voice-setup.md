# Gnani voice

The voice orb uses the selected UI language. The backend routes speech recognition to Gnani
Prisma and answer audio to Gnani Timbre. Eleven language codes are wired throughout the flow:
`hi`, `en`, `mr`, `bn`, `gu`, `pa`, `or`, `ta`, `te`, `kn`, `ml`.

A recorded question sends a mono 16 kHz WAV and the selected `lang` to `/v1/query`. The returned
answer plays automatically for voice questions, including a confirmation prompt. You can choose
a candidate by voice or button, replay the answer, stop playback, or tap the orb to interrupt and
record another question. Typed queries do not auto-play.

## Connect the provider

No Gnani key is currently configured in this checkout. If you do not already have a `.env`, copy
`.env.example` to `.env`. Edit the server-side `.env` and set:

```dotenv
GNANI_API_KEY=your_actual_gnani_key
```

Keep the key on the server. Do not put it into JavaScript, the browser console, or a URL. `.env`
is already ignored by Git. Restart the server after setting or changing the key:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001, select a language and allow microphone access when you tap the orb.
Microphone capture requires localhost or HTTPS. When configured, the orb becomes available.
The connection indicator reflects server configuration, not a completed provider health check.
An invalid key or provider error is shown when you submit speech.

## Verification limits

`artifacts/voice-language-checks.json` records 11 language flows through the actual client and local
backend, using simulated microphone samples and a controlled text transcript. It checks localized
controls, the WAV/lang request, reply language, automatic playback language and cancellation.
It does **not** verify live Gnani recognition, audible speech quality, accent handling, browser
microphone permissions, or physical capture. These need a configured key and device testing.
Browser control was unavailable during this change, so visual rendering is also unverified.

Repeat control checks with an isolated local server and the check-only `jsdom` dependency:

```bash
JSDOM_MODULE=/tmp/dawa-ui-check/node_modules/jsdom/lib/api.js \
DAWA_CHECK_URL=http://127.0.0.1:8002 \
DAWA_VOICE_REPORT=artifacts/voice-language-checks.json \
node scripts/check_voice_controls.mjs
```
