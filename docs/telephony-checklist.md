# Local telephony implementation checklist

Status: built-in Asterisk echo passed by user confirmation; Python AudioSocket
echo is running and awaiting real-call verification. Spoken AI Q&A is pending.

## Integration plan (written before implementation)

Reuse `app/main.py:create_app` and the existing multipart `POST /v1/query` interface with
`channel=phone`. Keep the website, medicine database, guardrails, Gnani Prisma STT,
Timbre TTS and session management in the existing backend. `data1` remains the source
of medicine facts. The telephone service must not implement medicine search itself.

`app/adapters/gnani_stt.py` accepts complete utterances up to 30 seconds in WAV,
MP3, OGG, FLAC, AAC or M4A; automatic detection uses Prisma v3 with `indic-auto`.
`app/adapters/gnani_tts.py` returns signed 16-bit little-endian mono PCM and accepts
a requested sample rate. `app/pipeline.py` already selects 8 kHz for phone calls.
`app/api/sessions.py` owns conversation and pending medicine-confirmation context.
Each call UUID will map to its own backend session, rather than using a shared one.

First establish a Linphone call to Asterisk's built-in Echo application on extension
700. Then switch 700 to an asyncio AudioSocket echo service. AudioSocket uses a
one-byte type plus a two-byte big-endian length; the dialplan application carries
8 kHz signed 16-bit little-endian mono PCM. Read complete frames with `readexactly`,
not one TCP read per message. Only after both real call checks pass, add utterance
segmentation, resampling if needed, HTTP backend requests and paced response audio.

The confirmed target is this Omarchy/Arch laptop, with Docker-isolated Asterisk.
Provide Fedora native-package instructions as an alternative. Bind SIP and the
unauthenticated AudioSocket listener to loopback; no public trunk or firewall
opening is needed for a same-laptop test.

## Phase 1: inspection and plan

- [x] Confirm target OS and installation boundary with the user (Omarchy).
- [x] Locate backend entry point and existing medicine query interface.
- [x] Inspect Gnani STT/TTS formats, phone sample rate and language detection.
- [x] Inspect conversation/session and interruption interfaces.
- [x] Read the official AudioSocket protocol and dialplan application documentation.
- [x] Write the integration plan before changing implementation code.

## Phase 2: first SIP call

- [x] Provide isolated Asterisk installation and secure generated credentials.
- [x] Configure PJSIP account 1001, loopback transport and narrow RTP range.
- [x] Configure extension 700 as a built-in echo test first.
- [x] Start Asterisk and verify PJSIP, codec, AudioSocket and UUID modules.
- [x] Download official Linphone 6.2.4 AppImage and attempt GUI launch.
- [x] Confirm Linphone is visible (user screenshot).
- [x] Register the local third-party SIP account 1001 (confirmed in PBX logs).
- [x] **REAL CALL GATE 1:** dial 700 and hear your own speech echoed.
- [x] Record the actual call result: user confirmed "echo coming" after routing/authentication fixes.

## Phase 3: Python audio transport

- [x] Implement validated AudioSocket framing, UUID, PCM, DTMF, errors and hangup.
- [x] Test fragmented/coalesced messages, malformed payloads and disconnections.
- [x] Test independent simultaneous calls and duplicate UUID rejection.
- [x] Configure a unique UUID per call and switch 700 to the Python echo service.
- [ ] **REAL CALL GATE 2:** hear speech echoed through Python and verify cleanup.

## Phase 4: AI bridge (after both real call gates)

- [ ] Reuse existing backend over HTTP with independent per-call session IDs.
- [ ] Segment caller speech with bounded utterances and silence handling.
- [ ] Validate audio conversion and paced 8 kHz response streaming.
- [ ] Add greeting, follow-up, invalid-name handling and localized recovery prompts.
- [ ] Add bounded call/idle/provider timeouts and feasible caller interruption.
- [ ] Test Gnani request contracts and Hindi/Marathi/English context flow.
- [ ] **REAL CALL GATE 3:** complete a spoken question-and-answer exchange in Linphone.
- [ ] Explicitly report actual languages tested and any untested languages.

## Documentation and readiness

- [x] Omarchy launch, Linphone account setup and manual acceptance steps.
- [x] Fedora installation, module checks and configuration instructions.
- [x] Troubleshooting SIP registration, RTP, AudioSocket and provider failures.
- [x] Future Indian SIP trunk requirements: provider approval, NAT/public IP,
      firewall/SIP/RTP, security, billing and emergency-calling constraints.
- [ ] Final checks and honest validation status; no paid trunk implemented.

## Evidence log

- Real call gate 1 passed: user reports hearing echo in Linphone. Started Python
  diagnostic listener on `127.0.0.1:9092`, passed a synthetic UUID/PCM echo probe,
  then reloaded 700 to `UUID()` + `AudioSocket()`. The next call now tests Python
  transport. Gate 2 remains open until audible echo and cleanup are verified.
  Diagnostic log: ignored `var/telephony/audiosocket-echo.log`.

- Corrected authentication domain after changing the SIP identity: the stored
  credentials still used `127.0.0.1:5070`, causing `fill_auth_info(): no auth info
  found` and registration failure. Matched credentials to identity host
  `127.0.0.1`, retained realm `asterisk` and existing secret, and reopened Linphone.
  Verified a fresh `LinphoneRegistrationProgress` -> `LinphoneRegistrationOk`
  transition at 2026-11-15 16:57:28 local time. This verifies registration only;
  the next real call must still confirm audio.

- Further incoming-call investigation: Linphone logged `INVITE sip:700@127.0.0.1`
  sent to UDP **5060**, then received its own INVITE. That bypassed Asterisk on
  **5070** entirely. Closed Linphone, backed up its configuration, corrected the
  malformed bracketed IPv4 SIP identity, and set an explicit outbound `reg_route`
  to `sip:127.0.0.1:5070;transport=udp;lr`. Reopened Linphone. A new real call is
  required to verify that INVITE routing and audible echo now work.

- Latest disconnect investigation: Asterisk logged a call to extension **1001**,
  which used `Dial(PJSIP/1001)` and rang the same client back. Replaced that route
  with `Goto(dawa-local,700,1)` and reloaded the dialplan, preventing this self-call
  path. The explicit test destination remains `sip:700@127.0.0.1:5070`.
  Audible echo is still unverified.

- Latest call: endpoint 1001 registered, 700 answered and reached `Echo()`;
  Asterisk learned incoming RTP from loopback. User reports hearing nothing, so
  real-call gate 1 remains open. Linphone was routed to HDMI/DisplayPort 3 instead
  of the laptop speaker and to the stereo rather than default digital microphone.
  Moved only Linphone's streams to system-default output/input, then closed the
  verified Linphone process, backed up its private configuration and saved Speaker
  for playback/media/ringer and Digital Microphone for capture. Reopened Linphone.
  A new audible echo test is still required; corrected routing is not proof of sound.
- Python AudioSocket diagnostic implementation is ready but is not connected to
  the dialplan. `tests/test_audiosocket.py` plus setup tests: **18 passed**.
  Includes partial/coalesced reads, payload validation, actual concurrent TCP
  connections, UUID isolation, DTMF hangup, peer errors, EOF and timeout cleanup.
  This is synthetic transport evidence, not a real SIP/Python audio test.

- Initial inspection: this host is Omarchy; Asterisk and Linphone are absent.
  Docker daemon is available. Existing application edits are preserved.
- 2026-11-15: Built upstream Asterisk 22.12.0 after verifying tarball SHA-256
  `f8b2ba98aac6c4ff3376bc0a2868bea6ed577ca684efb7ec739ae6e8ce6e35e2`.
  Container `dawasaathi-pbx` is running, SIP transport is `127.0.0.1:5070/UDP`,
  endpoint 1001 exists but has no registered contact yet. An unauthenticated SIP
  OPTIONS probe returned `401 Unauthorized`, confirming the listener/auth challenge.
  Port 5070 avoids a conflict with Linphone's own 5060 listener.
- Verified `chan_pjsip`, `app_audiosocket`, `res_audiosocket`, `UUID()`, the 700
  echo dialplan, and G.711-to-signed-linear codec translation in the running PBX.
- `tests/test_telephony_setup.py`: 2 passed (private credentials, persistence,
  stage rendering, local-only SIP/media and restricted dialplan). These tests
  do not cover AudioSocket, Gnani, or a real SIP speech exchange yet.
- Linphone official AppImage downloaded and launch attempted with private XDG
  paths. The sandbox reports GPU device warnings; visible GUI and microphone
  access require confirmation on the desktop. No SIP call has been placed.
- Protocol reference: https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/
- Dialplan reference: https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/AudioSocket/
