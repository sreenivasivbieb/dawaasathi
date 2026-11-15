# Gnani speech, automatic language and chat history

## Website follow-up repair (2026-11-15)

The current site uses the browser's native `MediaRecorder` for the uploaded recording, with
AudioWorklet/ScriptProcessor retained for input levels and silence detection. Microphone access
does not wait for `AudioContext.resume()`. If Web Audio is unavailable, recording still works;
tap the microphone again to send the question. The native recording is decoded to mono 16 kHz
WAV on the backend. A microphone selector and input meter help identify a wrong or silent device.

Reply playback uses a browser-native WAV player with visible controls, rather than assuming that
received PCM means audible playback. Autoplay rejection is displayed. Replay of saved or expired
audio uses `POST /v1/speak` with `which=reply` to regenerate server speech, so it does not depend
on locally installed browser voices. `which=chemist` regenerates the saved chemist script.

Enter sends a typed question; Shift+Enter inserts a newline. A bare greeting such as `hello`
returns a greeting, without trying to match Halo/Hallo medicines.

Verified: 717 Python tests, four JavaScript tests, and three real Chromium browser flows with
Web Audio deliberately disabled: Enter-to-greeting with native playback advancing; saved-history
Replay through server speech; and synthetic native microphone capture through live Hindi STT,
the data1-backed answer and native TTS playback. No room audio was recorded. Evidence:
[native browser report](../artifacts/native-browser-voice.json),
[screenshot](../artifacts/native-browser-voice.png). Physical-device confirmation still needs the user.

Hard refresh an existing tab with Ctrl+Shift+R at http://127.0.0.1:8004 to load this version.

## Previous implementation and verification

The UI language controls labels and typed questions. Spoken questions use automatic language
detection: the browser uploads its native recording (or WAV fallback) with `detect_language=true`;
the backend normalizes it to mono 16 kHz WAV and requests
`prisma-v3.0` with `language_code=indic-auto` even if the configured legacy model is v2.5.
The detected language controls reply templates, the session, persisted reply, and Timbre audio.
Voice confirmations keep the detected language until the user explicitly changes the UI language.

Only these ten Gnani-supported languages are selectable: Hindi, English, Marathi, Bengali,
Gujarati, Punjabi, Tamil, Telugu, Kannada and Malayalam. Odia was removed from the active language
lists. See [Gnani's official SDK documentation](https://github.com/Gnani-AI-Mintlify/Gnani-Vachana#prisma-v30-multilingual-model)
and its TTS language list. Historical locale assets do not add selectable languages.

Spoken replies are on by default for both typed and voice questions. The sound toggle disables
automatic playback; replay and stop remain available. The transcript is visible in the result.
A desktop sidebar keeps conversations visible, with open, delete and new-chat actions; on narrow
screens the History button opens the same panel. Quiet manually submitted recordings are sent
for recognition instead of discarded by local VAD. Empty recognition produces a recoverable
speech error. Empty audio playback triggers the browser-speech fallback instead of failing silently.

## Run

The configured key stays in server-side `.env` as `GNANI_API_KEY`; it is never sent to the browser.
Restart after editing environment configuration:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Open http://127.0.0.1:8001 and refresh the page. Allow microphone access when prompted.
Microphone capture requires localhost or HTTPS. The voice indicator reflects configuration;
errors such as an invalid key are reported when a request is made.

## Evidence

- `artifacts/live-spoken-language-checks.json`: **live** Gnani STT and TTS using synthesized Hindi
  and English input. Hindi input with an English UI returned Hindi text/audio; English input with
  a Hindi UI returned English text/audio. Generated reply WAVs were nonempty. These are synthetic
  recordings, not physical microphone recordings, and their data/price claims use demo datasets.
- `artifacts/voice-language-checks.json`: all ten frontend language routes, WAV upload, playback
  language, quiet manual submission, mute control, cancellation and sidebar presence. These use
  simulated capture and a controlled transcript; they are separate from live STT validation.
- `artifacts/ui-language-checks.json`: all ten interface languages and changing language during
  confirmation. Python regressions cover detected-language routing, storage, and audio jobs.

Physical microphone capture and audible playback remain unverified. Representative UI rendering
was later verified using authorized local Chromium capture; see [screenshots](screenshots/README.md). Live-provider checks establish the
recorded-file API path for Hindi and English, not recognition accuracy for every accent/language.

Voice and typed requests both use configured Groq language understanding, including recovery of
misheard medicine names and strengths. Replies can use guarded phrasing in the supported languages
before Timbre synthesis. `DATA1_ONLY=1` disables web fact lookups and loading old web-learned brands;
all medicine facts come from the imported `data1` database. Explicit photo reading remains configurable.

## Chromium capture regression (2026-11-15)

A real browser capture test reproduced a worklet bug: after transferring the first Float32Array,
its detached length became zero and the next recording buffer was allocated with zero length.
The recorder now allocates the replacement before transfer. Playback also no longer sends an
interrupt for a freshly generated answer; Stop targets the audio ID actually being played.

`artifacts/browser-voice-check.json` records the repaired path through Chromium getUserMedia and
the real AudioWorklet, live Prisma transcription, a Hindi reply, nonempty Timbre PCM, running
WebAudio playback, and a correctly scoped Stop request. Capture used an injected synthetic WAV,
so physical microphone hardware and audible speaker output remain separate checks.

The transfer-detachment regression can be run without a browser:

```bash
node --test tests/web/recorder-worklet.test.mjs
```

For a configured isolated server and a synthetic question WAV, run the full browser check:

```bash
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
DAWA_CHECK_URL=http://127.0.0.1:8004 \
DAWA_MIC_SAMPLE=/absolute/path/to/synthetic-question.wav \
node scripts/check_browser_voice.mjs
```

## Microphone startup diagnostics

The recorder now prefers AudioWorklet and falls back to the browser's legacy ScriptProcessor
when the worklet API is missing or its module cannot load. Microphone startup errors are shown
in a persistent localized panel with the native error code and a retry button. Permission denial,
missing input, device-start errors, insecure contexts and capture-engine failures have distinct
messages. No system/browser microphone settings are changed by this fallback.

`artifacts/microphone-compatibility-checks.json` records actual Chromium callback tests for both
fallback cases, plus simulated permission/device failures and cleanup. These tests use synthetic
devices; they do not record room audio. A separate local hardware check opened and immediately
stopped the input stream successfully, without constructing a recorder or sending audio.

```bash
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
DAWA_CHECK_URL=http://127.0.0.1:8004 \
node scripts/check_microphone_compatibility.mjs
```

If the UI still cannot start capture, share the code displayed under the voice orb. In Chromium,
check microphone permission through the site controls to the left of the address, and verify
the selected input device in the browser/system sound settings.
