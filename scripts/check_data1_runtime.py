"""Live HTTP/OCR/speech checks against an already-running data1 server.

Uses supplied dataset facts and synthetic pictures/speech. Does not use a physical microphone.
Run: python -m scripts.check_data1_runtime --base-url http://127.0.0.1:8004
"""
import argparse
import asyncio
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import httpx

from app.adapters.gnani_tts import TimbreTTS
from app.config import ROOT, get_settings
from app.tts.cache import encode
from scripts.import_data1 import FILES, AZ


async def check(base: str, output: Path):
    checks, sessions = [], set()
    report = {"checked_at": datetime.now().isoformat(), "base_url": base,
              "live_providers": True, "physical_microphone_verified": False,
              "source_hashes": {f: hashlib.sha256((ROOT / "data1" / f).read_bytes()).hexdigest() for f in FILES},
              "checks": checks}
    with (ROOT / "data1" / AZ).open(encoding="utf-8-sig", newline="") as f:
        wanted = {"Dolo 650 Tablet", "Augmentin 625 Duo Tablet", "Azithral 500 Tablet"}
        products = {r["name"]: r for r in csv.DictReader(f) if r["name"] in wanted}

    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        async def query(label, data, files=None):
            t0 = time.perf_counter()
            r = await client.post("/v1/query", data=data, files=files)
            assert r.status_code == 200, (label, r.status_code, r.text[:300])
            d = r.json()
            sessions.add(d["session_id"])
            assert d["status"] not in {"error", "blocked"}, (label, d)
            checks.append({"name": label, "status": d["status"], "seconds": round(time.perf_counter() - t0, 2),
                           "transcript": d["transcript"], "language": d["lang"], "match": d["match"],
                           "script": d["script"]["text"], "tts": d["tts"], "passed": True})
            return d

        async def stop(d):
            await client.post("/v1/interrupt", json={"session_id": d["session_id"], "audio_id": d["audio_id"]})

        try:
            health = (await client.get("/healthz")).json()
            report["health"] = health
            assert health["brands"] > 240000 and health["price_refs"] > 1500
            assert health["stt"] == "prisma" and health["tts"] == "timbre"
            # Generate the synthetic inputs before starting server-side speech jobs: Gnani
            # permits only one active TTS request per key, including across these processes.
            samples = []
            async with httpx.AsyncClient() as provider:
                tts = TimbreTTS(get_settings(), provider)
                for lang, utterance in [("en", "What is Augmentin six twenty five used for?"),
                                        ("hi", "डोलो छह सौ पचास का दाम बताइए।")]:
                    pcm = await tts.synthesize(utterance, lang)
                    assert len(pcm) > 1000
                    samples.append((lang, encode(pcm, tts.sample_rate, "wav")[0]))
            config = (await client.get("/v1/config")).json()
            report["data_sources"] = config["data_sources"]
            assert config["data_sources"]["banned_fdc"]["as_of"] == "2021-11-22"
            assert config["data_sources"]["nsq_alerts"]["rows"] == 0
            assert "DEV FIXTURE" not in json.dumps(config)

            d = await query("typed price from supplied A-Z and PMBJP", {"text": "Dolo 650 price", "lang": "en"})
            assert d["match"]["brand"] == "Dolo 650 Tablet"
            assert d["price"]["mrp"] == float(products["Dolo 650 Tablet"]["price(₹)"])
            assert d["price"]["reference"] == 15.47
            assert d["safety"]["reason"] == "banned_list_stale"
            assert all(x in d["script"]["text"] for x in ("650", "34.27", "15.47"))
            assert "paracetamol" in d["script"]["text"].lower()
            await stop(d)
            follow = await query("follow-up retains medicine, unavailable use remains unknown",
                                 {"text": "What is it used for?", "lang": "en", "session_id": d["session_id"]})
            assert follow["match"]["brand"] == "Dolo 650 Tablet"
            assert any(f["code"] == "USE_UNKNOWN" for f in follow["flags"])
            await stop(follow)

            d = await query("misheard name and number recovered by language understanding",
                            {"text": "Tellmisartan Salis price", "lang": "en"})
            item = d["items"]["focus"]
            assert item["salts"][0]["name"] == "telmisartan" and item["salts"][0]["amount"] == 40
            await stop(d)

            d = await query("larger dataset ambiguity is confirmed explicitly", {"text": "Azithral 500 price", "lang": "en"})
            assert d["status"] == "needs_confirmation"
            await stop(d)
            choice = next(c for c in d["candidates"] if c["brand"] == "Azithral 500 Tablet")
            confirmed = await client.post("/v1/confirm", json={"session_id": d["session_id"], "candidate_id": choice["candidate_id"], "lang": "en"})
            confirmed.raise_for_status()
            confirmed = confirmed.json()
            assert confirmed["match"]["brand"] == "Azithral 500 Tablet"
            assert confirmed["price"]["mrp"] == float(products["Azithral 500 Tablet"]["price(₹)"])
            await stop(confirmed)
            checks.append({"name": "candidate confirmation resolves the supplied product", "passed": True})

            d = await query("actual sample prescription and strip through configured OCR",
                            {"lang": "en"}, files={
                                "strip": ("strip.png", (ROOT / "web/samples/strip_glycomet_sr_500.png").read_bytes(), "image/png"),
                                "rx": ("rx.png", (ROOT / "web/samples/rx_glycomet_500.png").read_bytes(), "image/png")})
            assert d["verdict"]["code"] == "RELEASE_MISMATCH", d
            await stop(d)

            for lang, wav in samples:
                d = await query(f"live {lang} STT to answer to TTS", {"lang": "hi" if lang == "en" else "en"},
                                files={"audio": ("speech.wav", wav, "audio/wav")})
                assert d["transcript"] and d["detected_language"] == d["lang"] == lang
                assert d["tts"] == "timbre"
                audio = await client.get(d["audio_stream_url"])
                assert audio.status_code == 200 and sum(b != 0 for b in audio.content) > 1000
                checks[-1]["speech_pcm_bytes"] = len(audio.content)
                checks[-1]["nonzero_pcm_bytes"] = sum(b != 0 for b in audio.content)
                assert d["match"]["brand"] == ("Augmentin 625 Duo Tablet" if lang == "en" else "Dolo 650 Tablet")
                if lang == "hi":
                    assert "34.27" in d["script"]["text"] and "15.47" in d["script"]["text"]
                history = (await client.get("/v1/threads/" + d["session_id"])).json()
                assert history["messages"][-1]["lang"] == lang
            report["passed"] = True
        except Exception as exc:
            report["passed"] = False
            report["error"] = type(exc).__name__
            if checks:
                checks[-1]["passed"] = False
            raise
        finally:
            for session in sessions:
                await client.delete("/v1/threads/" + session)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report.get("passed", False), "checks": len(checks), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/data1-live-checks.json")
    args = parser.parse_args()
    asyncio.run(check(args.base_url, args.output))
