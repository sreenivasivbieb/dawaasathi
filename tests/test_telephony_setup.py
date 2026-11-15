"""Safety and persistence checks for generated local PBX configuration."""
import stat

from scripts.telephony import local_pbx


def test_private_credentials_survive_stage_switch(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "telephony"
    monkeypatch.setattr(local_pbx, "RUNTIME", runtime)
    local_pbx.prepare("echo")
    password = (runtime / "sip-password").read_text().strip()
    assert len(password) >= 40
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime / "sip-password").stat().st_mode) == 0o600
    assert password not in capsys.readouterr().out
    echo = (runtime / "asterisk" / "extensions.conf").read_text()
    assert "Echo()" in echo and "AudioSocket(" not in echo
    local_pbx.prepare("audiosocket")
    assert (runtime / "sip-password").read_text().strip() == password
    assert password not in capsys.readouterr().out
    dialplan = (runtime / "asterisk" / "extensions.conf").read_text()
    assert "${UUID()}" in dialplan
    assert "AudioSocket(${CALL_UUID},127.0.0.1:9092)" in dialplan
    assert "Echo()" not in dialplan
    assert "Set(TIMEOUT(absolute)=180)" in dialplan
    for path in (runtime / "asterisk").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sip_is_loopback_authenticated_and_has_no_pstn_route(tmp_path, monkeypatch):
    monkeypatch.setattr(local_pbx, "RUNTIME", tmp_path)
    local_pbx.prepare("echo")
    sip = (tmp_path / "asterisk" / "pjsip.conf").read_text()
    assert "__SIP_PASSWORD__" not in sip
    assert "bind=127.0.0.1:5070" in sip
    assert "auth=1001-auth" in sip
    assert "permit=127.0.0.1/32" in sip
    assert "media_address=127.0.0.1" in sip
    assert "bind_rtp_to_media_address=yes" in sip
    assert "allow=ulaw,alaw" in sip
    assert "direct_media=no" in sip
    dialplan = (tmp_path / "asterisk" / "extensions.conf").read_text()
    assert "_X" not in dialplan
    assert "Dial(PJSIP/1001" not in dialplan
    assert "1001,1,Goto(dawa-local,700,1)" in dialplan
