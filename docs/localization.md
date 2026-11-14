# Interface language and logo

The language picker updates the interface in all ten supported languages: headings, form
labels, photo instructions, voice controls/setup, notices, errors/toasts, history controls,
metadata, accessibility labels and document title. New interface copy lives in
`web/i18n/interface.js`; existing medicine terminology remains in the existing language tables.
English duplicate labels have been removed from non-English result cards.

The read-only `/v1/threads/{thread_id}/localized?lang=...` endpoint re-renders saved assistant
responses using the backend templates and stored facts. It does not repeat OCR, call a language
model, rewrite the original history, or change medical verdicts. Audio URLs from the original
language are cleared. Medicine names, user messages, filenames, identifiers, and factual source
names remain as supplied. The selected language also applies when confirming a pending match.

`web/icons/icon.svg` now contains a mint capsule-and-spark logo used in the header and favicon.
The logo was rendered locally and inspected; the full website could not be visually reviewed
because browser control is unavailable.

Verification: `artifacts/ui-language-checks.json` covers all ten languages, static/dynamic
labels, accessibility text, saved answers, and a language change during confirmation. The updated
Python suite passes 681 tests. Live Gnani/device verification remains separate; see
[voice setup](voice-setup.md).
