# Local telephone integration

Follow the live status in [the checklist](telephony-checklist.md). Both real echo
checkpoints passed. Live synthetic-provider probes passed; a human spoken SIP
question-and-answer exchange still needs verification. Telephone languages are
**Hindi and English only**, per the latest user instruction.

## Video demonstration

The demonstration covers **“What is the alternate of Dolo 650?”**. The call flow
is greeting, beep, caller question, then an answer naming Jan Aushadhi Paracetamol
Tablets IP 650 mg and asking the caller to
confirm ingredient, strength and formulation with a doctor/pharmacist. This
alternative was checked through the existing backend against data1 drug code 511.

Prepare Hindi/English audio once (uses existing Gnani credentials/credits):

```bash
cd "/home/vishal/D DRIVE/dawasaathi"
.venv/bin/python -m app.telephony.demo --prepare
```

Start the demo server in its own terminal:

```bash
.venv/bin/python -m app.telephony.demo --lang en
```

It binds `127.0.0.1:9094`. Keep Asterisk running and select the demo call route:

```bash
docker start dawasaathi-pbx
.venv/bin/python scripts/telephony/local_pbx.py prepare --stage demo
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'dialplan reload'
```

Call `sip:700@127.0.0.1:5070`, wait for the greeting/beep, ask the demo question,
then pause roughly one second. Speech is ignored during playback to avoid echo
interruptions. The answer plays once, thanks the caller, and the call ends.
DTMF **1** continues to the answer; **7/8** select Hindi/English, and **#** ends
the call. Runtime files are under ignored `var/telephony/demo/`.

To restore general live AI calling, start `app.telephony.server` and select
`--stage assistant`, then reload the dialplan. Real human spoken general-AI
acceptance is tracked separately from this demonstration.

## Start the AI call stack (after the passed echo checkpoints)

Use the existing `.venv`, `.env` and data1 database. No new medicine database,
provider credentials or website build is needed. Run these in separate terminals
from `/home/vishal/D DRIVE/dawasaathi`. Do not start duplicate listeners.

Terminal 1 — existing backend **and frontend**:

```bash
cd "/home/vishal/D DRIVE/dawasaathi"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8004
```

Website: http://127.0.0.1:8004. Wait for backend startup before terminal 2.

Terminal 2 — Python AI AudioSocket server (requires ffmpeg):

```bash
cd "/home/vishal/D DRIVE/dawasaathi"
.venv/bin/python -m app.telephony.server --lang hi
```

It binds `127.0.0.1:9093` and refuses a backend configured with mock speech.
Use `--lang en` for an English greeting. Automatic language detection initially
chooses Hindi/English. Other detected languages receive a selection prompt.

Terminal 3 — PBX and the AI dialplan:

```bash
cd "/home/vishal/D DRIVE/dawasaathi"
docker start dawasaathi-pbx
.venv/bin/python scripts/telephony/local_pbx.py prepare --stage assistant
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'dialplan reload'
.venv/bin/python scripts/telephony/local_pbx.py status
```

Terminal 4 — Linphone with this project's saved account and audio devices:

```bash
cd "/home/vishal/D DRIVE/dawasaathi"
XDG_CONFIG_HOME="$PWD/var/telephony/linphone/config" \
XDG_CACHE_HOME="$PWD/var/telephony/linphone/cache" \
XDG_DATA_HOME="$PWD/var/telephony/linphone/data" \
./var/telephony/Linphone.AppImage --appimage-extract-and-run
```

Dial **`sip:700@127.0.0.1:5070`**. The AI stage greets you instead of echoing.
Speak a medicine question, then pause about one second. Recognition operates on
complete utterances, bounded to 20 seconds. A short periodic tone means a request
is still processing. Fixed localized prompts handle silence and errors; if speech
providers are unavailable, a tone is the last-resort cue.

| Key | Action |
| --- | --- |
| 7 | Lock recognition/replies to Hindi and repeat greeting |
| 8 | Lock recognition/replies to English and repeat greeting |
| 1 / 2 / 3 | Select a pending medicine candidate |
| 9 | None of the pending candidates |
| 0 | Interrupt current playback |
| # | End call |

Sustained caller speech can interrupt playback. Use headphones to avoid speaker
audio retriggering energy VAD. `--vad-threshold 350` is the default; increase it
for noise, reduce it for a quiet microphone. VAD is energy-based, so noisy rooms
and acoustic echo need real testing. It is not a clinical or production call VAD.
Calls are capped at 180 seconds and four concurrent connections. Three idle
intervals end a silent call after prompts. Backend/provider requests have bounded
timeouts. Gnani TTS still serializes requests per API key; backend rate limits
apply across local calls. Revisit capacity before using a public trunk.

Hangup cancels call tasks, audio playback and known backend audio generation.
Backend conversation history and its ordinary session TTL follow existing app
policy; this bridge does not delete saved conversations or record raw caller audio.

## Acceptance and automated checks

The two echo tests have passed. For the remaining real-call gate:

1. Call 700 and hear the initial greeting.
2. Say “Dolo 650 price”, pause, and hear a relevant spoken answer.
3. Ask “Is there a cheaper option?” and verify context carries forward.
4. Repeat in Hindi; press 7/8 to check explicit language selection.
5. Test interruption, unclear names/candidate selection and hangup.
6. Confirm cleanup in the Python log and `core show channels`.

Do not mark this gate passed from byte counts or synthetic speech alone. Existing
data1 banned-list limitations remain in answers; a phone call does not change the
data's safety/provenance status.

```bash
.venv/bin/python -m pytest tests/test_audiosocket.py tests/test_telephone_ai.py tests/test_telephony_setup.py -q
```

Tests use fixtures and HTTP mocks. The opt-in live probe consumes provider credits,
uses generated speech, and must run with other callers idle:

```bash
.venv/bin/python -m scripts.telephony.check_live --live
```

It checks Hindi/English STT, medicine query, TTS sample rate and follow-up session
identity. Its JSON evidence under `var/telephony/` is not a human SIP test.

If started by the assistant, current diagnostic logs are
`var/telephony/backend.log`, `assistant.log` and `audiosocket-echo.log`.
Foreground commands above print their logs to their respective terminals.

## Omarchy laptop: isolated Asterisk

Docker is already available on this machine. This setup builds Asterisk 22.12.0
from the upstream source tarball with its upstream SHA-256 checksum. It does not
install an AUR package or alter the desktop. The container shares the Linux host
network, runs as your user with all capabilities dropped, and listens for SIP
only on `127.0.0.1:5070/UDP`. RTP uses ports 10000–10019 and is bound to loopback.
The generated password and configuration are in ignored `var/telephony/`.

From the repository root:

```bash
.venv/bin/python scripts/telephony/local_pbx.py prepare --stage echo
.venv/bin/python scripts/telephony/local_pbx.py build
.venv/bin/python scripts/telephony/local_pbx.py start
.venv/bin/python scripts/telephony/local_pbx.py status
```

Build needs internet access and can take several minutes. Re-running prepare
retains your password but replaces generated configuration. It does not change a
running PBX until reload/restart. Do not start a second PBX on the same SIP port.

Linphone's Linux AppImage can be downloaded from the [official download page](https://www.linphone.org/en/download/).
If `var/telephony/Linphone.AppImage` has already been downloaded:

```bash
./var/telephony/Linphone.AppImage --appimage-extract-and-run
```

Use an existing/custom SIP account (no Linphone public account required):

If you are on the **Register** page with email/phone fields and `sip.linphone.org`,
go back to Linphone's home screen and choose **Third-Party SIP Account**. Accept
the informational screen, then enter the local account below. Do not create a
public account. See [Linphone's official login steps](https://www.linphone.org/en/docs/login-sip-account/).

| Setting | Value |
| --- | --- |
| Identity / SIP address | `sip:1001@127.0.0.1` |
| Authentication username | `1001` |
| Domain | `127.0.0.1` (host only) |
| Registrar / SIP server | `sip:127.0.0.1:5070;transport=udp` |
| Outbound SIP proxy | `sip:127.0.0.1:5070;transport=udp;lr` |
| Password | Read `var/telephony/sip-password` locally |
| Transport | UDP |
| Audio codecs | PCMU / G.711 μ-law, or PCMA / G.711 A-law |
| STUN / ICE | Disabled for this same-laptop test |
| Media encryption | None for this loopback-only test |

The PBX uses port 5070 to avoid Linphone's own default client socket on 5060.
Set the outbound proxy explicitly: otherwise `sip:700@127.0.0.1` without a port
can go to the client's own 5060 listener and appear as an incoming self-call.
Select the correct microphone and speaker in Linphone's audio settings. Prefer
headphones to reduce feedback.

Dial `sip:700@127.0.0.1:5070`. Expect a short tone, followed by your own speech echoed.
Hang up after the test. Confirm that both directions of speech work, then report
the result before advancing the checklist. Registration and a connected call timer
are insufficient evidence of working audio.

Inspect or stop just this PBX:

```bash
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'pjsip show contacts'
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'core show channels'
docker logs --tail 80 dawasaathi-pbx
.venv/bin/python scripts/telephony/local_pbx.py stop
```

## Next checkpoint: Python AudioSocket

This diagnostic passed a real call. To repeat it, start the echo listener in
another terminal (unless it is already running):

```bash
.venv/bin/python -m app.telephony.echo
```

It accepts at most four connections, requires a unique UUID first, validates
frame lengths, accepts 8 kHz PCM, paces output and bounds queues/call duration.
DTMF 0 ends the diagnostic. It logs byte counts, not audio or transcripts.
Then render the next dialplan and reload:

```bash
.venv/bin/python scripts/telephony/local_pbx.py prepare --stage audiosocket
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'dialplan reload'
```

This changes 700 to `Set(CALL_UUID=${UUID()})`, followed by
`AudioSocket(${CALL_UUID},127.0.0.1:9092)`. UUIDs are generated per call, never
reused between calls or derived from a caller-supplied value. Do not switch before the
listener is running: the call will end if TCP cannot connect.

The [AudioSocket dialplan application](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/AudioSocket/)
answers only when preceded by `Answer()`, and uses mono 8 kHz 16-bit PCM. SIP
G.711 is decoded by Asterisk; sending G.711 bytes as PCM would produce noise.
The [TCP protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
uses three-byte headers, not TCP packet boundaries. This bridge will use audio
type `0x10`; the broader protocol also defines higher-rate PCM types.

The existing backend stays on `127.0.0.1:8004`. Later, the Python bridge will
submit WAV utterances via `POST /v1/query`, using `channel=phone` and one backend
session per call. Responses already have 8 kHz PCM audio URLs and existing
language detection, confirmation, follow-up, medicine guardrails and data1 facts.
The AI bridge uses the same transport on port 9093; select `--stage assistant`
after starting it. Use `--stage audiosocket` to return to Python echo, or
`--stage echo` to return to built-in PBX echo. Reload the dialplan after switching.

## Fedora native alternative

The confirmed target is Omarchy. These commands are instructions for a separate
Fedora host, not commands to run on this Arch laptop.

Fedora ships Asterisk, but versions differ by Fedora release. Check packages and
module availability rather than assuming the package has every application:

```bash
sudo dnf install asterisk
dnf search asterisk
dnf provides '*/app_audiosocket.so'
dnf provides '*/res_audiosocket.so'
dnf provides '*/chan_pjsip.so'
dnf provides '*/func_uuid.so'
```

Install any module subpackages identified by `dnf provides`. See the
[Fedora package listing](https://packages.fedoraproject.org/pkgs/asterisk/asterisk/).
Older releases may not forward AudioSocket DTMF in the same way as Asterisk 22;
use the pinned container setup if the native build lacks required behavior.
Obtain Linphone from its official Linux download. Do not run both a native PBX
and the Docker PBX on port 5070.

Render the configuration with `local_pbx.py prepare --stage echo`. Back up any
existing `/etc/asterisk` configuration first. Install the generated `pjsip.conf`,
`extensions.conf`, `rtp.conf` and `modules.conf` into `/etc/asterisk`, owned by
root with group `asterisk` and permissions 0640. **Retain the Fedora package's
`asterisk.conf` and logging paths**; the repository's version uses container-only
temporary paths and `/usr/lib` rather than Fedora's possible `/usr/lib64`.

```bash
sudo install -o root -g asterisk -m 0640 var/telephony/asterisk/pjsip.conf /etc/asterisk/pjsip.conf
sudo install -o root -g asterisk -m 0640 var/telephony/asterisk/extensions.conf /etc/asterisk/extensions.conf
sudo install -o root -g asterisk -m 0640 var/telephony/asterisk/rtp.conf /etc/asterisk/rtp.conf
sudo install -o root -g asterisk -m 0640 var/telephony/asterisk/modules.conf /etc/asterisk/modules.conf
sudo restorecon -Rv /etc/asterisk
sudo systemctl start asterisk
sudo asterisk -rx 'pjsip show endpoints'
sudo asterisk -rx 'module show like audiosocket'
sudo asterisk -rx 'core show function UUID'
sudo asterisk -rx 'dialplan show 700@dawa-local'
```

Keep SELinux enabled. If Python TCP connections are denied, inspect
`sudo ausearch -m AVC -ts recent` and resolve the specific service/port policy;
do not blindly disable SELinux or install an unrestricted generated policy.
For a same-host loopback call, do not open public firewalld SIP/RTP ports.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Image build fails | Internet/DNS, upstream checksum and complete build log. Never bypass checksum validation to make a build pass. |
| Container exits | `docker logs dawasaathi-pbx`; check port conflicts and configuration/module errors. |
| Linphone fails registration | Account username/password/domain/UDP, 5070 listener, `pjsip show endpoints` and `pjsip show contacts`. |
| Registration succeeds, call gets 404 | Dial 700 at 127.0.0.1; inspect `dialplan show 700@dawa-local`. |
| No audio / one-way audio | PCMU/PCMA negotiation, correct mic/speaker, loopback SDP/RTP, no STUN/ICE, endpoint `direct_media=no`. |
| AppImage fails with FUSE error | Use its `--appimage-extract` mode in a private runtime directory; run the extracted `AppRun`. Do not disable the application's sandbox to work around an unrelated error. |
| Call drops at AudioSocket step | Verify TCP listener 127.0.0.1:9092 and that Python is running; return to the built-in echo stage to isolate SIP. |
| AI speech is slow (later phase) | Existing Gnani TTS serializes requests per API key; concurrent calls can queue. Bounded prompts/timeouts are required. |
| Backend 429 (later phase) | Existing `/v1/query` rate limit is shared by the local bridge IP; size call capacity explicitly rather than disabling limits. |

For a short SIP diagnostic, enable `pjsip set logger on`, reproduce, then disable
with `pjsip set logger off`. SIP traces and caller audio can contain private
information; do not commit traces, recordings or credentials. This initial PBX
does not record calls or enable AMI, ARI, HTTP management, public registration or
paid outbound dial routes.

## Eventual Indian telephone number (not implemented)

A local SIP extension is not a public phone number. A future provider must
explicitly support an Indian inbound number delivered over a SIP trunk to this
PBX, with its onboarding/KYC, permitted use, concurrent call capacity, rates and
number routing confirmed directly with the provider. No provider account,
purchase, public port forwarding or outbound/PSTN route is configured here.

Before that stage, plan:

- A stable reachable public IP or provider-approved VPN/SBC. Consumer CGNAT
  usually prevents unsolicited inbound SIP/RTP; DDNS alone cannot remove CGNAT.
- Correct PJSIP `local_net`, `external_signaling_address` and
  `external_media_address`, router forwarding and SIP ALG behavior. See the
  [official NAT guide](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/Configuring-res_pjsip-to-work-through-NAT/).
- Provider-specific SIP transport/authentication and RTP ranges. Restrict
  firewall rules to provider signaling/media IPs; use TLS/SRTP when supported.
  Keep AudioSocket and the FastAPI backend private, even when SIP is public.
- A separate trunk context permitting only the AI extension, no arbitrary
  dialplan destinations, no anonymous calling, and bounded concurrency/duration.
  Protect management interfaces, rotate credentials and patch the PBX.
- Monitoring, spending/call caps, abuse prevention, secure logging/retention,
  caller notice and any consent/recording obligations checked with the provider
  and appropriate local guidance. Do not present this assistant as emergency
  calling or medical emergency care.
- An actual provider-to-PBX spoken acceptance test, including DTMF, hangup,
  language changes, interruptions, simultaneous calls and network failure.

None of these future requirements proves current public-number readiness.
