"""Exercise a running server as a client, using actual sample image bytes and no adapter mocks.

Run against an isolated local instance, e.g.:
  LOCAL_DB_PATH=/tmp/dawa-check.db .venv/bin/python -m uvicorn app.main:app --port 8001
  .venv/bin/python scripts/check_user_flows.py --base-url http://127.0.0.1:8001

Creates synthetic conversations and removes only the threads it creates. The supplied database
must contain the project's demo brands. No credential or private conversation is printed.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
import wave
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks = []
    created = set()
    with httpx.Client(base_url=args.base_url, timeout=90) as client:
        def check(name, condition, detail=None):
            checks.append({"name": name, "passed": bool(condition), **({"detail": detail} if detail else {})})
            print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)

        def query(text=None, photos=None, **fields):
            files = {kind: (name, (ROOT / "samples" / name).read_bytes(), "image/png")
                     for kind, name in (photos or {}).items()}
            start = time.monotonic()
            response = client.post("/v1/query", data={"lang": "en", **({"text": text} if text else {}), **fields}, files=files or None)
            response.raise_for_status()
            data = response.json()
            created.add(data["session_id"])
            return data, round(time.monotonic() - start, 2)

        try:
            check("health", client.get("/healthz").json().get("ok"))
            cfg = client.get("/v1/config").json()
            check("config matches supported upload limits and languages", cfg.get("max_image_bytes", 0) > 0 and len(cfg["languages"]) == 11)
            check("search endpoint finds Dolo", bool(client.get("/v1/drugs/search", params={"q": "Dolo 650"}).json()["results"]))
            data, elapsed = query("Dolo 650 price and generic")
            check("typed lookup", data["status"] == "ok" and "Dolo" in data["match"]["brand"], {"seconds": elapsed})
            sid = data["session_id"]
            follow, _ = query("What is it used for?", session_id=sid)
            check("follow-up retains session and medicine", follow["session_id"] == sid and "Paracetamol" in follow["script"]["text"])
            pouch, _ = query("Dolo 650", pouch='["paracetamol"]')
            check("duplicate ingredient check", any(f["code"] == "DUPLICATE_SALT" for f in pouch["flags"]))
            ambiguous, _ = query("Glycomet")
            check("ambiguous name requests confirmation", ambiguous["status"] == "needs_confirmation" and len(ambiguous["candidates"]) > 1)
            confirmed = client.post("/v1/confirm", json={"session_id": ambiguous["session_id"], "candidate_id": ambiguous["candidates"][0]["candidate_id"], "lang": "en"}).json()
            check("candidate selection resolves medicine", confirmed["status"] == "ok" and bool(confirmed.get("items")))
            none, _ = query("Glycomet")
            rejected = client.post("/v1/confirm", json={"session_id": none["session_id"], "none": True}).json()
            check("none-of-these requests new evidence", rejected["status"] == "needs_image")
            unknown, _ = query("zzzyyyqqq 777")
            check("unknown medicine stays unverified", unknown["status"] == "needs_image" and not unknown.get("alternatives"))
            for name, photos, expected in [
                ("real strip OCR", {"strip": "strip_augmentin_625.png"}, "GENERIC_EQUIVALENT"),
                ("real matching prescription and strip", {"rx": "rx_augmentin_625.png", "strip": "strip_augmentin_625.png"}, "MATCH"),
                ("real IR versus SR comparison", {"rx": "rx_glycomet_500.png", "strip": "strip_glycomet_sr_500.png"}, "RELEASE_MISMATCH"),
                ("real liquid concentration comparison", {"rx": "rx_paracetamol_syrup_120.png", "strip": "bottle_calpol_250.png"}, "CONCENTRATION_MISMATCH"),
                ("synthetic banned-combination example", {"strip": "strip_demo_fdc.png"}, "BANNED_COMBINATION"),
            ]:
                result, elapsed = query(photos=photos)
                check(name, (result.get("verdict") or {}).get("code") == expected, {"seconds": elapsed, "status": result["status"], "verdict": (result.get("verdict") or {}).get("code")})
            nsq, _ = query(photos={"strip": "strip_dolo_650_nsq.png"})
            check("synthetic batch alert", any(f["code"] == "NSQ_BATCH" for f in nsq["flags"]))
            messages = client.get(f"/v1/threads/{sid}").json()["messages"]
            check("history contains actual follow-up", len(messages) >= 4 and messages[-1]["role"] == "assistant")
            check("history summaries", len(client.get("/v1/threads", params={"ids": sid}).json()["threads"]) == 1)
            resumed, _ = query("Dolo 650 price", session_id=sid)
            check("resume same conversation", resumed["session_id"] == sid)
            spoken = client.post("/v1/speak", json={"session_id": sid, "which": "chemist"})
            check("chemist script", spoken.status_code == 200 and bool(spoken.json().get("text")))
            audio = client.get(spoken.json()["audio_url"].replace(".ogg", ".wav"))
            check("audio file endpoint", audio.status_code == 200 and audio.content.startswith(b"RIFF"))
            check("audio interrupt", client.post("/v1/interrupt", json={"session_id": sid}).json() == {"ok": True})
            draft = client.post("/v1/grievance/draft", json={"session_id": sid}).json()
            check("draft is based on actual result and never submitted", draft.get("submitted") is False and "Dolo" in draft["draft"]["subject"])
            for name, response, expected in [
                ("empty query", client.post("/v1/query", data={"lang": "en"}), 400),
                ("wrong photo type", client.post("/v1/query", files={"strip": ("text.txt", b"hello", "text/plain")}), 415),
                ("oversized photo", client.post("/v1/query", files={"strip": ("big.png", b"0" * (cfg["max_image_bytes"] + 1), "image/png")}), 413),
                ("missing confirmation choice", client.post("/v1/confirm", json={"session_id": sid}), 400),
                ("expired audio", client.get("/v1/audio/missing.wav"), 404),
                ("unknown history", client.get("/v1/threads/s_000000000000"), 404),
            ]:
                check(name, response.status_code == expected and bool(response.json().get("error")))
            corrupt = client.post("/v1/query", data={"lang": "en"}, files={"strip": ("corrupt.png", b"not an image", "image/png")}).json()
            if corrupt.get("session_id"):
                created.add(corrupt["session_id"])
            check("unreadable photo asks for another photo", corrupt.get("status") == "needs_image")
            wav = io.BytesIO()
            with wave.open(wav, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(16000))
            voice = client.post("/v1/query", data={"lang": "en"}, files={"audio": ("silence.wav", wav.getvalue(), "audio/wav")}).json()
            if voice.get("session_id"):
                created.add(voice["session_id"])
            check("silent/unconfigured voice does not invent a medicine", voice.get("status") in ("error", "needs_image") and not voice.get("match"))
            for lang in [v["code"] for v in cfg["languages"]]:
                localized, _ = query("Dolo 650", lang=lang)
                check(f"reply language {lang}", localized["lang"] == lang and bool(localized.get("script", {}).get("text")))
            asyncio.run(check_socket(args.base_url, check, created))
            check("delete owned test conversation", client.delete(f"/v1/threads/{sid}").status_code == 200 and client.get(f"/v1/threads/{sid}").status_code == 404)
        finally:
            for thread in created:
                client.delete(f"/v1/threads/{thread}")
    report = {"base_url": args.base_url, "adapter_overrides": False, "providers": {k: cfg.get(k) for k in ("stt", "tts", "ocr")}, "note": "Real HTTP/WebSocket requests and actual sample images. Provider configuration is reported by /v1/config; no claim of live STT/TTS or real-world medicine validation.", "checks": checks, "passed": sum(c["passed"] for c in checks), "total": len(checks)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['passed']}/{report['total']} user-flow checks passed")
    return 0 if all(c["passed"] for c in checks) else 1


async def check_socket(base, check, created):
    url = base.replace("http://", "ws://").replace("https://", "wss://") + "/v1/stream"
    for message, expected in [([], "EXPECTED_START"), ({"type": "start", "sample_rate": "bad"}, "BAD_SAMPLE_RATE")]:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(message))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            check(f"WebSocket invalid start: {expected}", reply.get("code") == expected)
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "start", "lang": "en", "session_id": 42}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10)); created.add(ready["session_id"])
        await ws.send("[]")
        bad = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("WebSocket malformed message returns error", bad.get("code") == "BAD_MESSAGE")
        await ws.send(json.dumps({"type": "text", "text": "Dolo 650 price"}))
        while True:
            message = await asyncio.wait_for(ws.recv(), 30)
            if isinstance(message, str):
                result = json.loads(message)
                if result.get("type") == "result":
                    created.add(result["session_id"])
                    check("WebSocket recovers and answers next question", result.get("status") == "ok")
                    break
        await ws.send(json.dumps({"type": "stop"}))


if __name__ == "__main__":
    raise SystemExit(main())
