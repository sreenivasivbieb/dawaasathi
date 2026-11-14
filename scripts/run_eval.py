"""Drug-entity accuracy, match confidence calibration and latency on the synthetic eval set.

  uv run python -m scripts.run_eval --text                              # transcript = utterance text (free)
  uv run python -m scripts.run_eval --text --asr-noise 0,0.1,0.2,0.3    # + simulated ASR errors (seeded)
  uv run python -m scripts.run_eval --text --pipeline                   # + full pipeline status (mock TTS)
  uv run python -m scripts.run_eval --audio                             # prints the Prisma call estimate
  uv run python -m scripts.run_eval --audio --yes [--snr clean,10] [--sr 8000] [--per-condition 30]

Text mode feeds each utterance through app.nlu.entities.extract + MatchIndex.search (the same
calls the pipeline makes). Audio mode first transcribes eval/manifest.jsonl with Prisma REST
(keyterms = MatchIndex.keyterms, concurrency <= 2, retries, transcripts cached in
eval/audio/stt_cache.jsonl so reruns are free) and then does the same.

Correct = the candidate's brand family AND strength (strength_key) equal the truth SKU's
("entity"); "brand" = family only; "sku" = the exact product (or an identical duplicate listing).
Writes eval/report.json and eval/report.md (tables for the metrics slide).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import Store, connect  # noqa: E402
from app.match.index import MatchIndex  # noqa: E402
from app.match.translit import script_of  # noqa: E402
from app.nlu.entities import extract  # noqa: E402
from app.nlu.numbers import number_to_words, parse_numbers  # noqa: E402

EVAL = ROOT / "eval"
STT_CACHE = EVAL / "audio" / "stt_cache.jsonl"
T1_GRID = [round(0.70 + 0.02 * i, 2) for i in range(11)]  # 0.70 .. 0.90
T2_GRID = [round(0.02 + 0.02 * i, 2) for i in range(6)]  # 0.02 .. 0.12


# ============================================================================ ASR-noise simulation
_LAT_VOWEL_SWAP = {"a": "e", "e": "i", "i": "e", "o": "u", "u": "o"}
_DEV_MATRA_SWAP = {"ि": "ी", "ी": "ि", "ु": "ू", "ू": "ु", "े": "ै", "ै": "े", "ो": "ौ", "ौ": "ो", "ॉ": "ो", "ा": ""}
_LAT_CONS_SWAP = {"t": "d", "d": "t", "k": "g", "g": "k", "p": "b", "b": "p", "s": "z", "z": "s", "v": "w"}
_DEV_CONS_SWAP = {"ट": "ड", "ड": "ट", "क": "ग", "ग": "क", "प": "ब", "ब": "प", "स": "ज़", "त": "द", "द": "त"}


def _perturb_word(w: str, rng: random.Random) -> str:
    dev = script_of(w) == "devanagari"
    idx = list(range(1, len(w)))  # keep the first letter: ASR rarely loses the onset
    if not idx:
        return w
    op = rng.choice(("vowel_drop", "vowel_swap", "consonant_swap"))
    if dev:
        vowels = [i for i in idx if w[i] in _DEV_MATRA_SWAP]
        cons = [i for i in idx if w[i] in _DEV_CONS_SWAP]
        if op == "vowel_drop" and vowels:
            i = rng.choice(vowels)
            return w[:i] + w[i + 1:]
        if op in ("vowel_drop", "vowel_swap") and vowels:
            i = rng.choice(vowels)
            return w[:i] + _DEV_MATRA_SWAP[w[i]] + w[i + 1:]
        if cons:
            i = rng.choice(cons)
            return w[:i] + _DEV_CONS_SWAP[w[i]] + w[i + 1:]
        return w
    low = w.lower()
    vowels = [i for i in idx if low[i] in _LAT_VOWEL_SWAP]
    cons = [i for i in idx if low[i] in _LAT_CONS_SWAP]
    if op == "vowel_drop" and vowels and len(w) > 3:
        i = rng.choice(vowels)
        return w[:i] + w[i + 1:]
    if op in ("vowel_drop", "vowel_swap") and vowels:
        i = rng.choice(vowels)
        return w[:i] + _LAT_VOWEL_SWAP[low[i]] + w[i + 1:]
    if cons:
        i = rng.choice(cons)
        return w[:i] + _LAT_CONS_SWAP[low[i]] + w[i + 1:]
    return w


def _swap_number(text: str, u: dict, rng: random.Random) -> str:
    """digits <-> number words, or a partly digitised mix ("छह सौ पच्चीस" -> "6 सौ 25")."""
    n = u.get("truth_strength")
    if n is None:
        return text
    if re.fullmatch(r"\d+(?:\.\d+)?", text.strip()):
        return number_to_words(n, "hi" if u.get("carrier") == "dev" else "en").replace("-", " ")
    if rng.random() < 0.5:
        return str(int(n)) if float(n).is_integer() else str(n)
    out = []
    for t in text.split():  # digitise unit words only; "सौ" / "hundred" stay words
        spans = parse_numbers([t])
        v = spans[0].value if spans else None
        out.append(f"{v:g}" if v is not None and 1 <= v < 100 and rng.random() < 0.7 else t)
    return " ".join(out)


def simulate_asr(u: dict, p: float, seed: int) -> tuple[str, list[str]]:
    """Seeded ASR-style corruption of the brand span and the strength span of an utterance."""
    text = u["text"]
    if p <= 0:
        return text, []
    rng = random.Random(f"{seed}|{u['id']}|{p}")
    ops: list[str] = []
    brand = u.get("brand_text") or ""
    if brand and brand in text:
        new = brand
        if rng.random() < p:  # script swap (Prisma may write a brand in either script)
            from scripts.gen_eval_set import DEV_MAP

            if script_of(new) == "devanagari":
                new = u.get("brand_latin") or new
            else:
                words = new.lower().split()
                if all(w in DEV_MAP for w in words):
                    new = " ".join(DEV_MAP[w] for w in words)
            if new != brand:
                ops.append("script_swap")
        words = new.split()
        for i, w in enumerate(words):
            if rng.random() < p:
                nw = _perturb_word(w, rng)
                if nw != w:
                    words[i] = nw
                    ops.append("brand_typo")
        text = text.replace(brand, " ".join(words), 1)
    st = u.get("strength_text") or ""
    if st and st in text and rng.random() < p:
        new = _swap_number(st, u, rng)
        if new != st:
            text = text.replace(st, new, 1)
            ops.append("number_swap")
    return text, ops


# ============================================================================ scoring
class Scorer:
    def __init__(self, store: Store, index: MatchIndex, t1: float, t2: float):
        self.store = store
        self.index = index
        self.t1 = t1
        self.t2 = t2
        self.by_name = {b.name: b for b in store.brands.values()}

    def truth(self, u: dict):
        b = self.by_name.get(u["truth_brand"]) or self.store.brands.get(u.get("truth_brand_id"))
        return b

    def _sig(self, b) -> tuple:
        return (b.base, b.salt_key, b.strength_key, b.form, b.release)

    def judge(self, cand_brand_id: int | None, truth) -> dict:
        if cand_brand_id is None:
            return {"brand": False, "entity": False, "sku": False}
        c = self.store.brands[cand_brand_id]
        brand = c.family == truth.family
        return {"brand": brand, "entity": brand and c.strength_key == truth.strength_key,
                "sku": c.id == truth.id or self._sig(c) == self._sig(truth)}

    def score(self, text: str, truth) -> dict:
        t0 = time.perf_counter()
        ents = extract(text)
        t1 = time.perf_counter()
        res = self.index.search(ents.name_tokens, strength=ents.strength, strengths=ents.strengths,
                                form=ents.form, release=ents.release, t1=self.t1, t2=self.t2, k=3)
        t2 = time.perf_counter()
        cands = res.candidates[:3]
        judged = [self.judge(c.brand_id, truth) for c in cands]
        top = cands[0].score if cands else 0.0
        second = cands[1].score if len(cands) > 1 else 0.0
        return {
            "transcript": text,
            "name_tokens": ents.name_tokens,
            "strengths": ents.strengths,
            "intent": ents.intent,
            "candidates": [
                {"brand_id": c.brand_id, "name": self.store.brands[c.brand_id].name if c.brand_id else None,
                 "salt": c.salt, "score": round(c.score, 4)} for c in cands],
            "top1": judged[0] if judged else {"brand": False, "entity": False, "sku": False},
            "top3": {k: any(j[k] for j in judged) for k in ("brand", "entity", "sku")},
            "score": round(top, 4),
            "margin": round(top - second, 4),
            "accepted": bool(res.confident),
            "reason": res.reason,
            "nlu_ms": (t1 - t0) * 1000,
            "match_ms": (t2 - t1) * 1000,
        }


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))], 2)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def summarize(items: list[dict]) -> dict:
    n = len(items)
    acc = [r for r in items if r["accepted"]]
    out = {"n": n}
    for k in ("entity", "brand", "sku"):
        out[f"top1_{k}"] = _rate(sum(r["top1"][k] for r in items), n)
        out[f"top3_{k}"] = _rate(sum(r["top3"][k] for r in items), n)
    out["no_candidate"] = _rate(sum(1 for r in items if not r["candidates"]), n)
    out["auto_accept_rate"] = _rate(len(acc), n)
    out["accepted_precision"] = _rate(sum(r["top1"]["entity"] for r in acc), len(acc))
    out["match_p50_ms"] = pct([r["match_ms"] + r["nlu_ms"] for r in items], 50)
    out["match_p95_ms"] = pct([r["match_ms"] + r["nlu_ms"] for r in items], 95)
    stt = [r["stt_ms"] for r in items if r.get("stt_ms") is not None]
    if stt:
        out["stt_p50_ms"] = pct(stt, 50)
        out["stt_p95_ms"] = pct(stt, 95)
    return out


def sweep(items: list[dict]) -> list[dict]:
    """Matcher acceptance rule (score >= T1 and margin >= T2) over the grid."""
    rows = []
    n = len(items)
    for t1 in T1_GRID:
        for t2 in T2_GRID:
            acc = [r for r in items if r["candidates"] and r["score"] >= t1 and r["margin"] >= t2]
            conf = [r for r in items if r["candidates"] and not (r["score"] >= t1 and r["margin"] >= t2)]
            rows.append({
                "t1": t1, "t2": t2,
                "auto_accept_rate": _rate(len(acc), n),
                "accepted_precision": _rate(sum(r["top1"]["entity"] for r in acc), len(acc)),
                "wrong_auto_accepts": sum(1 for r in acc if not r["top1"]["entity"]),
                "confirmation_rate": _rate(len(conf), n),
                "truth_offered_in_confirmation": _rate(sum(r["top3"]["entity"] for r in conf), len(conf)),
            })
    return rows


def recommend(rows: list[dict]) -> list[dict]:
    """Highest auto-accept rate at a given minimum accepted precision (ties -> stricter thresholds)."""
    out = []
    for floor in (1.0, 0.99, 0.98, 0.95):
        ok = [r for r in rows if r["accepted_precision"] is not None and r["accepted_precision"] >= floor]
        if ok:
            best = max(ok, key=lambda r: (r["auto_accept_rate"], r["t1"], r["t2"]))
            out.append({"min_precision": floor, **best})
    return out


def calibration(items: list[dict]) -> list[dict]:
    bins = [(0.0, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.01)]
    out = []
    for lo, hi in bins:
        sel = [r for r in items if r["candidates"] and lo <= r["score"] < hi]
        out.append({"score_bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(sel),
                    "top1_entity": _rate(sum(r["top1"]["entity"] for r in sel), len(sel))})
    return out


def group(items: list[dict], key) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in items:
        buckets[key(r)].append(r)
    return {k: summarize(v) for k, v in buckets.items()}


# ============================================================================ audio mode
def _load_stt_cache() -> dict[str, dict]:
    if not STT_CACHE.exists():
        return {}
    out = {}
    for ln in STT_CACHE.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            rec = json.loads(ln)
            out[rec["key"]] = rec
    return out


async def transcribe_all(rows: list[dict], settings, keyterms: list[str], yes: bool, concurrency: int) -> None:
    import httpx

    from app.adapters.gnani_stt import PrismaSTT, STTError

    kt_hash = hashlib.sha1("|".join(keyterms).encode()).hexdigest()[:10]
    cache = _load_stt_cache()
    todo = []
    for r in rows:
        data = (ROOT / r["file"]).read_bytes()
        r["_audio"] = data
        r["_key"] = hashlib.sha1(data + f"|{r['file']}|{settings.stt_model}|{settings.stt_format}|{r['lang']}|"
                                        f"{kt_hash}".encode()).hexdigest()
        if r["_key"] in cache:
            r["transcript_stt"] = cache[r["_key"]]["text"]
            r["stt_ms"] = cache[r["_key"]]["ms"]
            r["stt_cached"] = True
        else:
            todo.append(r)
    secs = sum(max(0, len(r["_audio"]) - 44) / 2 / r["sample_rate"] for r in todo)
    print(f"Prisma: {len(todo)} REST calls needed ({secs / 60:.1f} min of audio), {len(rows) - len(todo)} cached "
          f"(model {settings.stt_model}, format {settings.stt_format}, {len(keyterms)} keyterms).")
    if not todo:
        return
    if not settings.gnani_api_key:
        raise SystemExit("GNANI_API_KEY is not set; cannot run --audio.")
    if not yes:
        raise SystemExit("These calls spend programme credits. Re-run with --yes (narrow with --snr / --sr / "
                         "--per-condition / --limit first).")
    STT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, min(2, concurrency)))
    fatal: list[str] = []
    done = 0
    async with httpx.AsyncClient() as client:
        stt = PrismaSTT(settings, client)

        async def one(r: dict) -> None:
            nonlocal done
            async with sem:
                if fatal:
                    return
                delay = 2.0
                for attempt in range(4):
                    try:
                        res = await stt.transcribe(r["_audio"], Path(r["file"]).name, r["lang"], keyterms)
                        break
                    except STTError as exc:
                        if not exc.retryable or attempt == 3:
                            if exc.code in ("HTTP_401", "HTTP_403") or "auth" in exc.code.lower():
                                fatal.append(f"{exc.code}: {exc}")
                            r["stt_error"] = f"{exc.code}: {str(exc)[:120]}"
                            return
                    await asyncio.sleep(delay)
                    delay *= 2
                r["transcript_stt"] = res.text
                r["stt_ms"] = round(res.latency_ms, 1)
                with STT_CACHE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": r["_key"], "file": r["file"], "text": res.text, "ms": r["stt_ms"],
                                        "model": res.model, "at": datetime.now().isoformat(timespec="seconds")},
                                       ensure_ascii=False) + "\n")
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(todo)}")

        await asyncio.gather(*(one(r) for r in todo))
    if fatal:
        raise SystemExit(f"Prisma rejected the key: {fatal[0]}")


def select_manifest(args) -> list[dict]:
    path = EVAL / "manifest.jsonl"
    if not path.exists():
        raise SystemExit("eval/manifest.jsonl not found. Run: uv run python -m scripts.gen_eval_set --synthesize --yes")
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.snr:
        want = {None if t.strip() == "clean" else float(t) for t in args.snr.split(",") if t.strip()}
        rows = [r for r in rows if r["snr_db"] in want]
    if args.sr:
        srs = {int(x) for x in args.sr.split(",") if x.strip()}
        rows = [r for r in rows if r["sample_rate"] in srs]
    if args.per_condition:
        rng = random.Random(args.seed)
        by: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            by[(r["snr_db"], r["sample_rate"])].append(r)
        ids = sorted({r["id"] for r in rows})
        keep = set(rng.sample(ids, min(args.per_condition, len(ids))))  # same utterances in every condition
        rows = [r for r in rows if r["id"] in keep]
    if args.limit:
        rows = rows[: args.limit]
    return rows


# ============================================================================ pipeline mode
async def pipeline_outcomes(utts: list[dict], settings, store: Store, index: MatchIndex, scorer: Scorer) -> dict:
    """End-to-end decision per utterance through Pipeline.query (typed text, mock STT/TTS)."""
    import tempfile

    import httpx

    from app.adapters.gnani_stt import MockSTT
    from app.adapters.gnani_tts import MockTTS
    from app.adapters.ocr import OCRReader
    from app.api.sessions import SessionStore
    from app.pipeline import Pipeline, QueryInput
    from app.tts.cache import AudioService, SegmentCache

    outcomes: Counter = Counter()
    statuses: Counter = Counter()
    verdicts: Counter = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        async with httpx.AsyncClient() as http:
            audio = AudioService(MockTTS(settings.tts_sample_rate), SegmentCache(Path(tmp), settings.tts_voice,
                                 settings.tts_model), settings.tts_sample_rate)
            pipe = Pipeline(settings, store, index, MockSTT(), OCRReader(settings, http), audio,
                            SessionStore(settings.session_ttl_s))
            for u in utts:
                truth = scorer.truth(u)
                resp = await pipe.query(QueryInput(lang=u["lang"] if u["lang"] in ("hi", "en") else "hi",
                                                   text=u["text"]))
                statuses[resp.status] += 1
                if resp.verdict:
                    verdicts[resp.verdict["code"]] += 1
                bid = (resp.match or {}).get("brand_id")
                if resp.status == "needs_confirmation":
                    offered = any(scorer.judge(c.brand_id, truth)["entity"] for c in resp.candidates)
                    outcomes["asked_to_confirm_truth_offered" if offered else "asked_to_confirm_truth_missing"] += 1
                elif bid is not None:
                    ok = scorer.judge(bid, truth)["entity"]
                    tag = "answered" if resp.status in ("ok", "unverified") else f"{resp.status}"
                    outcomes[f"{tag}_{'correct' if ok else 'wrong'}"] += 1
                else:
                    outcomes[f"{resp.status}_no_match"] += 1
            for job in list(audio.jobs.values()):
                if job.task and not job.task.done():
                    job.task.cancel()
    n = len(utts)
    return {"n": n, "status": dict(statuses.most_common()), "verdict": dict(verdicts.most_common()),
            "outcome": dict(outcomes.most_common()),
            "outcome_rate": {k: round(v / n, 4) for k, v in outcomes.most_common()}}


# ============================================================================ report
def _fmt(v, pct_: bool = True) -> str:
    if v is None:
        return "–"
    return f"{v * 100:.1f}%" if pct_ else f"{v}"


def write_report(report: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    m = report["meta"]
    L = [f"# Dawaa Saathi – drug-entity eval ({m['mode']} mode)", ""]
    L.append(f"Generated {m['generated_at']} · {m['n_items']} items from {m['n_utterances']} synthetic utterances "
             f"· DB: {m['db_brands']} brands ({m['db_source']}) · thresholds T1={m['t1']} T2={m['t2']}")
    if m.get("note"):
        L.append("")
        L.append(f"> {m['note']}")
    L += ["", "**Entity** = brand family and strength both right · **Brand** = family only · "
          "**SKU** = exact product · **Auto-accept** = matcher confident at T1/T2 (no confirmation question) · "
          "**Precision** = entity-correct among auto-accepted.", ""]

    def table(title: str, rows: dict[str, dict], first: str) -> None:
        L.append(f"## {title}")
        L.append("")
        has_stt = any("stt_p50_ms" in r for r in rows.values())
        head = f"| {first} | n | Top-1 entity | Top-3 entity | Top-1 brand | Top-3 brand | SKU | Auto-accept | Precision |"
        sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
        if has_stt:
            head += " STT p50 / p95 ms |"
            sep += "---:|"
        L.extend([head, sep])
        for k, r in rows.items():
            line = (f"| {k} | {r['n']} | {_fmt(r['top1_entity'])} | {_fmt(r['top3_entity'])} | {_fmt(r['top1_brand'])} "
                    f"| {_fmt(r['top3_brand'])} | {_fmt(r['top1_sku'])} | {_fmt(r['auto_accept_rate'])} "
                    f"| {_fmt(r['accepted_precision'])} |")
            if has_stt:
                line += f" {r.get('stt_p50_ms', '–')} / {r.get('stt_p95_ms', '–')} |"
            L.append(line)
        L.append("")

    table("Overall", {"all": report["overall"]}, "set")
    for title, key in report["group_titles"]:
        table(title, report["groups"][key], key)

    L += ["## Confidence vs correctness", "", "| top score | n | top-1 entity correct |", "|---|---:|---:|"]
    for b in report["calibration"]:
        L.append(f"| {b['score_bin']} | {b['n']} | {_fmt(b['top1_entity'])} |")
    L.append("")

    L += ["## Threshold sweep (T1 = min top score, T2 = min margin over #2)", "",
          "Recommended operating points (highest auto-accept rate at each precision floor):", "",
          "| min precision | T1 | T2 | auto-accept | precision | wrong auto-accepts | confirmation rate | truth offered in confirmation |",
          "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in report["recommended"]:
        L.append(f"| {r['min_precision'] * 100:.0f}% | {r['t1']} | {r['t2']} | {_fmt(r['auto_accept_rate'])} "
                 f"| {_fmt(r['accepted_precision'])} | {r['wrong_auto_accepts']} | {_fmt(r['confirmation_rate'])} "
                 f"| {_fmt(r['truth_offered_in_confirmation'])} |")
    cur = next((r for r in report["sweep"] if r["t1"] == m["t1"] and r["t2"] == m["t2"]), None)
    if cur:
        L += ["", f"Current setting T1={m['t1']}, T2={m['t2']}: auto-accept {_fmt(cur['auto_accept_rate'])}, "
              f"precision {_fmt(cur['accepted_precision'])}, {cur['wrong_auto_accepts']} wrong auto-accepts, "
              f"confirmation {_fmt(cur['confirmation_rate'])}."]
    L += ["", "Accepted precision / auto-accept rate by (T1 row, T2 column):", "",
          "| T1 \\ T2 | " + " | ".join(f"{t2:.2f}" for t2 in T2_GRID) + " |",
          "|---|" + "---:|" * len(T2_GRID)]
    idx = {(r["t1"], r["t2"]): r for r in report["sweep"]}
    for t1 in T1_GRID:
        cells = []
        for t2 in T2_GRID:
            r = idx[(t1, t2)]
            cells.append(f"{_fmt(r['accepted_precision'])} / {_fmt(r['auto_accept_rate'])}")
        L.append(f"| {t1:.2f} | " + " | ".join(cells) + " |")
    L += ["", "The pipeline asks for confirmation in more cases than this matcher rule alone: a look-alike "
          "(LASA) partner, or a brand heard without a strength when the family has several SKUs.", ""]

    if report.get("pipeline"):
        p = report["pipeline"]
        L += ["## End-to-end decision (Pipeline.query, typed text, mock TTS)", "",
              "| outcome | share |", "|---|---:|"]
        for k, v in p["outcome_rate"].items():
            L.append(f"| {k} | {_fmt(v)} |")
        L += ["", "Statuses: " + ", ".join(f"{k} {v}" for k, v in p["status"].items()),
              "", "Verdicts: " + ", ".join(f"{k} {v}" for k, v in p["verdict"].items()), ""]

    lat = report["latency_ms"]
    L += ["## Latency", "", "| stage | p50 ms | p95 ms |", "|---|---:|---:|",
          f"| entity extraction + match (in-process) | {lat['match_p50']} | {lat['match_p95']} |"]
    if lat.get("stt_p50") is not None:
        L.append(f"| Prisma STT REST (round trip) | {lat['stt_p50']} | {lat['stt_p95']} |")
    L.append("")

    if report["failures"]:
        L += ["## Top-1 entity misses (first 20)", "", "| id | condition | transcript | truth | top-1 (score) |",
              "|---|---|---|---|---|"]
        for f in report["failures"][:20]:
            got = f"{f['top1']} ({f['score']})" if f["top1"] else "no candidate"
            L.append(f"| {f['id']} | {f['condition']} | {f['transcript']} | {f['truth']} | {got} |")
        L.append("")
    out.with_suffix(".md").write_text("\n".join(L), encoding="utf-8")


# ============================================================================ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--text", action="store_true", help="no STT: utterance text -> extract -> match")
    mode.add_argument("--audio", action="store_true", help="Prisma STT on eval/manifest.jsonl (spends credits)")
    ap.add_argument("--utterances", type=Path, default=EVAL / "utterances.jsonl")
    ap.add_argument("--asr-noise", default="0", help="text mode: comma list of corruption probabilities, e.g. 0,0.1,0.2")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--pipeline", action="store_true", help="text mode: also run Pipeline.query (mock STT/TTS)")
    ap.add_argument("--snr", default=None, help="audio mode: e.g. clean,10,0")
    ap.add_argument("--sr", default=None, help="audio mode: e.g. 8000 or 16000")
    ap.add_argument("--per-condition", type=int, default=None, help="audio mode: N utterances per condition")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=2, help="audio mode: parallel Prisma calls (max 2)")
    ap.add_argument("--yes", action="store_true", help="audio mode: confirm spending Prisma credits")
    ap.add_argument("--out", type=Path, default=EVAL / "report", help="output prefix (.json and .md)")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.db_path.exists():
        raise SystemExit(f"{settings.db_path} not found. Run: uv run python -m scripts.load_data --src data/seed_dev")
    conn = connect(settings.db_path)
    store = Store.load(conn)
    conn.close()
    index = MatchIndex(store).build()
    scorer = Scorer(store, index, settings.match_t1, settings.match_t2)
    if not args.utterances.exists():
        raise SystemExit("eval/utterances.jsonl not found. Run: uv run python -m scripts.gen_eval_set")
    utts = [json.loads(ln) for ln in args.utterances.read_text(encoding="utf-8").splitlines() if ln.strip()]
    utts = [u for u in utts if scorer.truth(u) is not None] if utts else utts
    if args.limit and args.text:
        utts = utts[: args.limit]

    items: list[dict] = []
    group_titles: list[tuple[str, str]] = []
    note = None
    if args.text:
        levels = [float(x) for x in args.asr_noise.split(",") if x.strip()]
        for p in levels:
            for u in utts:
                text, ops = simulate_asr(u, p, args.seed)
                r = scorer.score(text, scorer.truth(u))
                r.update(id=u["id"], truth=u["truth_brand"], asr_noise=p, ops=ops, template=u["template"],
                         carrier=u["carrier"], brand_script=u["brand_script"], number_style=u["number_style"],
                         condition=f"asr_noise={p:g}")
                items.append(r)
        group_titles.append(("By simulated ASR error rate (per-token corruption probability)", "asr_noise"))
        group_titles.append(("By number style (clean text)", "number_style"))
        group_titles.append(("By brand script (clean text)", "brand_script"))
        group_titles.append(("By carrier phrase language (clean text)", "carrier"))
        group_titles.append(("By template (clean text)", "template"))
        note = ("Text mode: transcripts are the generated utterance text (optionally corrupted), not Prisma output. "
                "Use --audio for real STT accuracy by SNR and sample rate.")
        base = [r for r in items if r["asr_noise"] == levels[0]]
        groups = {
            "asr_noise": group(items, lambda r: f"{r['asr_noise']:g}"),
            "number_style": group(base, lambda r: r["number_style"]),
            "brand_script": group(base, lambda r: r["brand_script"]),
            "carrier": group(base, lambda r: r["carrier"]),
            "template": group(base, lambda r: r["template"]),
        }
    else:
        rows = select_manifest(args)
        keyterms = index.keyterms(settings.stt_keyterms_n)
        asyncio.run(transcribe_all(rows, settings, keyterms, args.yes, args.concurrency))
        for row in rows:
            if row.get("transcript_stt") is None:
                continue
            truth = scorer.truth(row)
            if truth is None:
                continue
            r = scorer.score(row["transcript_stt"], truth)
            snr = "clean" if row["snr_db"] is None else f"{row['snr_db']:g} dB"
            r.update(id=row["id"], truth=row["truth_brand"], snr_db=row["snr_db"], sample_rate=row["sample_rate"],
                     noise=row["noise"], channel=row["channel"], stt_ms=row.get("stt_ms"),
                     stt_cached=row.get("stt_cached", False), reference=row["text"],
                     condition=f"{snr} @ {row['sample_rate'] // 1000} kHz")
            items.append(r)
        errors = sum(1 for row in rows if row.get("stt_error"))
        if errors:
            note = f"{errors} files failed STT and are excluded."
        group_titles += [("By SNR and sample rate", "condition"), ("By SNR", "snr"), ("By sample rate", "sample_rate"),
                         ("By noise type", "noise")]

        def _snr_key(r):
            return "clean" if r["snr_db"] is None else f"{r['snr_db']:g} dB"

        def _snr_order(label: str) -> float:
            return -1e9 if label.startswith("clean") else -float(label.split()[0])

        def _ordered(d: dict, key) -> dict:
            return dict(sorted(d.items(), key=lambda kv: key(kv[0])))

        groups = {
            "condition": _ordered(group(items, lambda r: r["condition"]),
                                  lambda k: (_snr_order(k), -int(k.split(" @ ")[1].split()[0]))),
            "snr": _ordered(group(items, _snr_key), _snr_order),
            "sample_rate": _ordered(group(items, lambda r: f"{r['sample_rate']} Hz"), lambda k: -int(k.split()[0])),
            "noise": group(items, lambda r: r["noise"]),
        }
    if not items:
        raise SystemExit("Nothing to evaluate.")

    sw = sweep(items)
    failures = [{"id": r["id"], "condition": r["condition"], "transcript": r["transcript"], "truth": r["truth"],
                 "top1": r["candidates"][0]["name"] or r["candidates"][0]["salt"] if r["candidates"] else None,
                 "score": r["score"]} for r in items if not r["top1"]["entity"]]
    report = {
        "meta": {
            "mode": "text" if args.text else "audio",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "n_utterances": len({r["id"] for r in items}), "n_items": len(items), "t1": settings.match_t1, "t2": settings.match_t2,
            "db_brands": len(store.brands),
            "db_source": (store.dataset("brands") or {}).get("source") or "unknown",
            "asr_noise": args.asr_noise if args.text else None, "seed": args.seed,
            "stt_model": None if args.text else settings.stt_model, "note": note,
        },
        "overall": summarize(items),
        "groups": groups,
        "group_titles": group_titles,
        "calibration": calibration(items),
        "sweep": sw,
        "recommended": recommend(sw),
        "latency_ms": {
            "match_p50": pct([r["nlu_ms"] + r["match_ms"] for r in items], 50),
            "match_p95": pct([r["nlu_ms"] + r["match_ms"] for r in items], 95),
            "stt_p50": pct([r["stt_ms"] for r in items if r.get("stt_ms") is not None], 50),
            "stt_p95": pct([r["stt_ms"] for r in items if r.get("stt_ms") is not None], 95),
        },
        "failures": failures,
        "items": [{k: v for k, v in r.items() if not k.startswith("_")} for r in items],
    }
    if args.text and args.pipeline:
        try:
            report["pipeline"] = asyncio.run(pipeline_outcomes(utts, settings, store, index, scorer))
        except Exception as exc:  # the pipeline is still moving; keep the matcher report
            report["pipeline"] = None
            print(f"pipeline mode failed: {type(exc).__name__}: {exc}")
    write_report(report, args.out)

    o = report["overall"]
    print(f"{report['meta']['mode']} mode, {o['n']} items: top-1 entity {_fmt(o['top1_entity'])}, top-3 entity "
          f"{_fmt(o['top3_entity'])}, top-1 brand {_fmt(o['top1_brand'])}, auto-accept {_fmt(o['auto_accept_rate'])} "
          f"at precision {_fmt(o['accepted_precision'])}; match p50/p95 {o['match_p50_ms']}/{o['match_p95_ms']} ms")
    for k, r in groups[group_titles[0][1]].items():
        print(f"  {k:>14}: top-1 entity {_fmt(r['top1_entity'])}  top-3 {_fmt(r['top3_entity'])}  "
              f"auto-accept {_fmt(r['auto_accept_rate'])}  precision {_fmt(r['accepted_precision'])}")
    print(f"-> {args.out.with_suffix('.md').relative_to(ROOT)}, {args.out.with_suffix('.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
