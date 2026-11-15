# Local telephone integration

Follow the live status in [the checklist](telephony-checklist.md). No real SIP
question-and-answer exchange has been verified. The first milestone is deliberately
a PBX echo call, before adding Python or calling any AI provider.

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
| Domain / SIP server / proxy | `sip:127.0.0.1:5070;transport=udp` |
| Password | Read `var/telephony/sip-password` locally |
| Transport | UDP |
| Audio codecs | PCMU / G.711 μ-law, or PCMA / G.711 A-law |
| STUN / ICE | Disabled for this same-laptop test |
| Media encryption | None for this loopback-only test |

The PBX uses port 5070 to avoid Linphone's own default client socket on 5060.
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

After the real built-in echo call succeeds, implement and start the Python echo
listener on `127.0.0.1:9092`. Then render the next dialplan and reload:

```bash
.venv/bin/python scripts/telephony/local_pbx.py prepare --stage audiosocket
.venv/bin/python scripts/telephony/local_pbx.py cli --command 'dialplan reload'
```

This changes 700 to `Set(CALL_UUID=${UUID()})`, followed by
`AudioSocket(${CALL_UUID},127.0.0.1:9092)`. UUIDs are generated per call, never
hard-coded or derived from a caller-supplied value. Do not switch before the
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
AI integration is pending the two real echo-call checkpoints.

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
