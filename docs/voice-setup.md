# Gnani speech, automatic language and chat history

The UI language controls labels and typed questions. Spoken questions use automatic language
detection: the browser posts mono 16 kHz WAV with `detect_language=true`; the backend requests
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

Physical microphone capture, audible playback, and browser visual rendering have not been
verified: browser control is unavailable in this environment. Live-provider checks establish the
recorded-file API path for Hindi and English, not recognition accuracy for every accent/language.
