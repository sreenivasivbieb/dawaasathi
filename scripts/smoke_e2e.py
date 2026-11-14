"""End-to-end smoke test with the real services (spends a few credits per query):

  Timbre TTS (synthetic question audio) -> POST /v1/query [Prisma STT -> Groq LLM understand ->
  matcher + engine (alternatives, price) -> template -> Groq LLM reword -> guard] -> Timbre reply audio

  uv run python -m scripts.smoke_e2e                          # default Hindi questions
  uv run python -m scripts.smoke_e2e --q "डोलो 650 का सस्ता बताओ" --q "pan 40 ka daam"
  uv run python -m scripts.smoke_e2e --text                   # skip TTS+STT, send the text directly
  uv run python -m scripts.smoke_e2e --sr 8000                # question audio at 8 kHz (phone)

Question and reply audio are written to var/smoke/. Synthetic voices only, no real recordings.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.gnani_tts import TimbreTTS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

DEFAULT_QS = [
    "भैया डोलो छह सौ पचास का सस्ता जेनेरिक बताओ",
    "ऑगमेंटिन छह सौ पच्चीस का जन औषधि वाला विकल्प क्या है",
    "पैन चालीस गोली कितने की आती है",
    "टेल्मिसार्टन चालीस का सस्ता विकल्प बताइए",
]

OK, FAIL, SKIP = "PASS", "FAIL", "skip"


def wav_bytes(pcm: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


async def synth(text: str, sr: int) -> tuple[bytes, float]:
    s = get_settings()
    async with httpx.AsyncClient() as client:
        t0 = time.perf_counter()
        pcm = await TimbreTTS(s, client).synthesize(text, "hi", sr)
        return pcm, (time.perf_counter() - t0) * 1000


def line(stage: str, status: str, detail: str = "") -> None:
    print(f"  [{status:4}] {stage:<24} {detail}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", action="append", help="question (repeatable)")
    ap.add_argument("--text", action="store_true", help="send text instead of synthesized audio")
    ap.add_argument("--sr", type=int, default=16000, help="question audio sample rate (16000 or 8000)")
    ap.add_argument("--lang", default="hi")
    args = ap.parse_args()

    s = get_settings()
    print("config:", json.dumps({
        "gnani_key": bool(s.gnani_api_key), "prisma": s.stt_model, "timbre": f"{s.tts_model}/{s.tts_voice}",
        "groq_keys": len(s.groq_api_keys), "llm_model": s.groq_llm_model, "llm_mode": s.llm_mode,
        "llm_nlu": s.llm_nlu, "llm_reply": s.llm_reply}, ensure_ascii=False))
    out = ROOT / "var" / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    questions = args.q or DEFAULT_QS
    summary = []

    with TestClient(create_app()) as c:
        health = c.get("/healthz").json()
        print("healthz:", health)
        for n, q in enumerate(questions, 1):
            print(f"\n#{n} {q}")
            row = {"q": q}
            files, data = {}, {"lang": args.lang}
            if args.text or not s.gnani_api_key:
                line("question audio (Timbre)", SKIP, "sending text")
                data["text"] = q
            else:
                try:
                    pcm, ms = asyncio.run(synth(q, args.sr))
                    wav = wav_bytes(pcm, args.sr)
                    (out / f"q{n}.wav").write_bytes(wav)
                    line("question audio (Timbre)", OK if pcm else FAIL, f"{ms:.0f} ms, {len(pcm) / 2 / args.sr:.1f} s")
                    files["audio"] = (f"q{n}.wav", wav, "audio/wav")
                except Exception as exc:  # noqa: BLE001
                    line("question audio (Timbre)", FAIL, f"{type(exc).__name__}: {exc}"[:200])
                    data["text"] = q
            t0 = time.perf_counter()
            r = c.post("/v1/query", data=data, files=files or None)
            wall = (time.perf_counter() - t0) * 1000
            j = r.json()
            if r.status_code != 200 or "error" in j:
                line("/v1/query", FAIL, f"HTTP {r.status_code} {j}"[:300])
                summary.append({**row, "ok": False})
                continue
            tm = j.get("timings_ms", {})
            err = (j.get("data_sources") or {}).get("error")
            if files:
                asr = (j.get("data_sources") or {}).get("asr", {})
                line("Prisma STT", FAIL if err else OK,
                     f"{asr.get('model')} {tm.get('asr')} ms -> {j.get('transcript')!r}" if not err else str(err))
                for d in asr.get("dual", []):
                    line("  dual", "info", f"{d['model']} {d['ms']} ms score={d['score']} {d['text']!r}")
            llm = (j.get("data_sources") or {}).get("llm", {})
            nlu = llm.get("nlu")
            if nlu is None:
                line("LLM understand", SKIP, "not run (no key / LLM_MODE / pending question)")
            elif "error" in nlu:
                line("LLM understand", FAIL, nlu["error"])
            else:
                line("LLM understand", OK, f"{nlu.get('ms')} ms used={nlu.get('used')} "
                     f"{json.dumps(nlu.get('output'), ensure_ascii=False)}")
            match = j.get("match") or {}
            verdict = (j.get("verdict") or {}).get("code")
            line("match + engine", OK if j["status"] in ("ok", "unverified", "needs_confirmation") else FAIL,
                 f"status={j['status']} verdict={verdict} brand={match.get('brand')} "
                 f"conf={match.get('confidence')}")
            if j.get("candidates"):
                line("  candidates", "info", ", ".join(x["label"] for x in j["candidates"]))
            for a in j.get("alternatives", [])[:3]:
                line("  alternative", "info", f"{a['name'][:60]} | Rs {a['price']} / {a['pack_qty']} "
                     f"| saves {a['saves_pct']}%")
            price = j.get("price") or {}
            if price.get("nppa_ceiling_unit") is not None or price.get("mrp") is not None:
                line("  price", "info", f"mrp={price.get('mrp')} ja={price.get('reference')} "
                     f"ceiling/unit={price.get('nppa_ceiling_unit')} above={price.get('above_ceiling')}")
            rep = llm.get("reply")
            if rep is None:
                line("LLM reply", SKIP, "template only")
            elif rep.get("error"):
                line("LLM reply", FAIL, rep["error"])
            else:
                line("LLM reply", OK if rep.get("used") else "rej",
                     f"{rep.get('ms')} ms" + (f" rejected: {rep.get('rejected')}" if rep.get("rejected") else ""))
                if rep.get("rejected"):
                    line("  LLM said", "info", rep.get("candidate", "")[:300])
            line("spoken script", "info", j["script"]["text"])
            t1 = time.perf_counter()
            first_ms = None
            with c.stream("GET", f"/v1/audio/{j['audio_id']}.pcm") as st:
                for chunk in st.iter_bytes():
                    if chunk:
                        first_ms = (time.perf_counter() - t1) * 1000
                        break
            line("reply first audio", OK if first_ms is not None else FAIL,
                 f"{first_ms:.0f} ms after the JSON reply (streamed PCM)" if first_ms is not None else "no audio")
            t1 = time.perf_counter()
            a = c.get(f"/v1/audio/{j['audio_id']}.wav")
            tts_ms = (time.perf_counter() - t1) * 1000
            ok_audio = a.status_code == 200 and len(a.content) > 1000 and j.get("tts") == "timbre"
            (out / f"reply{n}.wav").write_bytes(a.content)
            line("reply audio (Timbre)", OK if ok_audio else FAIL,
                 f"tts={j.get('tts')} {len(a.content) // 1024} KB, {tts_ms:.0f} ms -> var/smoke/reply{n}.wav")
            line("timings", "info", json.dumps({k: v for k, v in tm.items() if v is not None}))
            summary.append({**row, "ok": ok_audio and j["status"] != "error", "status": j["status"],
                            "pipeline_ms": tm.get("total"), "wall_ms": round(wall)})
    print("\nsummary:")
    for x in summary:
        print(" ", "PASS" if x.get("ok") else "FAIL", x)


if __name__ == "__main__":
    main()
