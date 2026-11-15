"""Isolated local PBX setup. Secrets and rendered configuration stay under var/."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "telephony" / "asterisk"
RUNTIME = ROOT / "var" / "telephony"
IMAGE = "dawasaathi-asterisk:22.12.0"
NAME = "dawasaathi-pbx"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def prepare(stage: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUNTIME, 0o700)
    password_path = RUNTIME / "sip-password"
    if not password_path.exists():
        fd = os.open(password_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    password = password_path.read_text().strip()
    config = RUNTIME / "asterisk"
    config.mkdir(exist_ok=True, mode=0o700)
    for file in SOURCE.glob("*.conf"):
        shutil.copyfile(file, config / file.name)
    (config / "pjsip.conf").write_text(
        (SOURCE / "pjsip.conf.in").read_text().replace("__SIP_PASSWORD__", password)
    )
    body = " same => n,Playtones(440/150)\n same => n,Wait(0.2)\n same => n,StopPlayTones()\n same => n,Echo()"
    if stage == "audiosocket":
        body = " same => n,Set(CALL_UUID=${UUID()})\n same => n,AudioSocket(${CALL_UUID},127.0.0.1:9092)"
    (config / "extensions.conf").write_text(
        (SOURCE / "extensions.conf.in").read_text()
        .replace("__STAGE__", stage).replace("__CALL_BODY__", body)
    )
    for file in config.iterdir():
        os.chmod(file, 0o600)
    (RUNTIME / "stage").write_text(stage + "\n")
    print(f"Prepared {stage} configuration. SIP password is in {password_path}; it is not logged.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "start", "stop", "status", "cli"])
    parser.add_argument("--stage", choices=["echo", "audiosocket"], default="echo")
    parser.add_argument("--command", default="core show version")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.stage)
    elif args.action == "build":
        run("docker", "build", "-t", IMAGE, str(SOURCE))
    elif args.action == "start":
        if not (RUNTIME / "asterisk" / "pjsip.conf").exists():
            prepare(args.stage)
        run("docker", "run", "-d", "--name", NAME, "--network", "host",
            "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--pids-limit", "128", "--memory", "512m", "--cpus", "2",
            "--mount", f"type=bind,source={RUNTIME / 'asterisk'},target=/etc/asterisk,readonly",
            IMAGE)
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", NAME, "asterisk", "-rx", "core waitfullybooted"],
                capture_output=True, timeout=3,
            )
            if ready.returncode == 0:
                print("Asterisk is ready. Run status, then register Linphone and dial 700.")
                break
            alive = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", NAME],
                capture_output=True, text=True, check=True,
            )
            if alive.stdout.strip() != "true":
                raise SystemExit(f"Asterisk exited; inspect: docker logs {NAME}")
            time.sleep(0.25)
        else:
            raise SystemExit(f"Asterisk did not become ready; inspect: docker logs {NAME}")
    elif args.action == "stop":
        run("docker", "rm", "-f", NAME)
    elif args.action == "status":
        run("docker", "exec", NAME, "asterisk", "-rx", "core show version")
        run("docker", "exec", NAME, "asterisk", "-rx", "pjsip show endpoints")
        run("docker", "exec", NAME, "asterisk", "-rx", "pjsip show contacts")
        run("docker", "exec", NAME, "asterisk", "-rx", "dialplan show 700@dawa-local")
    else:
        run("docker", "exec", NAME, "asterisk", "-rx", args.command)


if __name__ == "__main__":
    main()
