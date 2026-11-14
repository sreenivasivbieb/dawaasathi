"""Synthetic code-mixed eval set for drug-entity accuracy (no real recordings, per challenge rules).

  uv run python -m scripts.gen_eval_set                       # eval/utterances.jsonl (free, no network)
  uv run python -m scripts.gen_eval_set --synthesize          # prints the Timbre call estimate, spends nothing
  uv run python -m scripts.gen_eval_set --synthesize --yes    # Timbre audio + noise + 8 kHz variants (CREDITS)

Utterances are built from the brands in the SQLite DB (DAWA_DB_PATH, built by scripts.load_data):
Hindi / Hinglish / English carrier phrases, brand names in Devanagari or Latin, strengths as
Hindi number words, colloquial Hindi ("ढाई सौ"), digits or English words.

--synthesize renders each utterance once with Timbre REST (raw PCM s16le, 16 kHz, cached under
eval/audio/_clean/ so reruns cost nothing), then writes WAV variants with numpy:
  wideband 16 kHz at SNR clean / 20 / 10 / 5 / 0 dB, noise = white | pink | babble (3-4 OTHER
  synthesized utterances summed at random offsets), and the same through a telephone channel
  (300-3400 Hz band-limit, 8 kHz, G.711 mu-law round trip). Manifest: eval/manifest.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Store, connect  # noqa: E402
from app.engine.salts import known_salts  # noqa: E402
from app.nlu.numbers import number_to_words  # noqa: E402

EVAL = ROOT / "eval"
AUDIO = EVAL / "audio"
CLEAN_CACHE = AUDIO / "_clean"
SR = 16000
TEL_SR = 8000

# Devanagari spellings for the dev-fixture brand words (how Prisma's codemix output tends to
# write them). Words not listed stay in Latin script, which is also realistic ASR output.
DEV_MAP: dict[str, str] = {
    "augmentin": "ऑगमेंटिन", "duo": "ड्यूओ", "moxikind": "मॉक्सीकाइंड", "cv": "सीवी", "clavam": "क्लैवम",
    "mox": "मॉक्स", "novamox": "नोवामॉक्स", "dolo": "डोलो", "calpol": "कालपोल", "crocin": "क्रोसिन",
    "advance": "एडवांस", "pan": "पैन", "pantocid": "पैंटोसिड", "glycomet": "ग्लाइकोमेट", "sr": "एसआर",
    "gp": "जीपी", "amlong": "अमलोंग", "amlokind": "अमलोकाइंड", "telma": "टेल्मा", "atorva": "एटोर्वा",
    "cetzine": "सेटज़ीन", "okacet": "ओकासेट", "allegra": "एलेग्रा", "azithral": "अज़ीथ्रल", "azee": "एज़ी",
    "taxim": "टैक्सिम", "o": "ओ", "zifi": "ज़ीफ़ी", "thyronorm": "थायरोनॉर्म", "montair": "मोंटेयर",
    "lc": "एलसी", "combiflam": "कॉम्बिफ्लेम", "zerodol": "ज़ेरोडोल", "p": "पी", "trajenta": "ट्राजेंटा",
    "linospan": "लिनोस्पैन", "ecosprin": "इकोस्प्रिन", "shelcal": "शेलकैल", "saathi": "साथी", "demo": "डेमो",
    "fdc": "एफडीसी",
}

# Words in a brand's display name that are not spoken as part of the name.
_DROP = {"tablet", "tablets", "tab", "capsule", "capsules", "cap", "oral", "suspension", "syrup", "drops",
         "drop", "injection", "mg", "mcg", "ml", "gm", "iu", "paediatric", "pediatric"}

FORM_SPOKEN = {  # form -> (Devanagari, Latin); liquids always say it, solids sometimes
    "oral_liquid": ("सिरप", "syrup"),
    "drops": ("ड्रॉप्स", "drops"),
    "capsule": ("कैप्सूल", "capsule"),
    "tablet": ("गोली", "tablet"),
}

# (template id, carrier script, TTS language, intent the phrase expresses, text with {drug})
TEMPLATES: list[tuple[str, str, str, str, str]] = [
    ("hi_generic_bhaiya", "dev", "hi", "FIND_GENERIC", "भैया मुझे {drug} का सस्ता जेनेरिक बताओ"),
    ("hi_price_kitne", "dev", "hi", "PRICE_CHECK", "{drug} कितने का है"),
    ("hi_verify_doctor", "dev", "hi", "VERIFY_MATCH", "डॉक्टर ने {drug} लिखा है ये सही है क्या"),
    ("hi_generic_jagah", "dev", "hi", "FIND_GENERIC", "{drug} की जगह कोई सस्ती दवा है क्या"),
    ("hi_janaushadhi", "dev", "hi", "FIND_GENERIC", "मेरे पास {drug} की पत्ती है इसका जन औषधि वाला मिलेगा"),
    ("hi_price_didi", "dev", "hi", "PRICE_CHECK", "दीदी {drug} का दाम क्या है"),
    ("hi_dosage", "dev", "hi", "DOSAGE_QUERY", "{drug} दिन में कितनी बार लेनी है"),
    ("hinglish_generic", "roman", "hi", "FIND_GENERIC", "bhaiya {drug} ka sasta generic batao"),
    ("hinglish_price", "roman", "hi", "PRICE_CHECK", "{drug} kitne ka hai"),
    ("en_generic", "en", "en", "FIND_GENERIC", "is there a cheaper generic for {drug}"),
    ("en_price", "en", "en", "PRICE_CHECK", "what is the price of {drug}"),
    ("en_verify", "en", "en", "VERIFY_MATCH", "doctor prescribed {drug}, is this the right medicine"),
]

# number rendering styles per carrier, with weights
NUMBER_STYLES = {
    "dev": [("hi_words", 0.40), ("hi_colloquial", 0.10), ("digits", 0.35), ("en_words", 0.15)],
    "roman": [("digits", 0.60), ("en_colloquial", 0.20), ("en_words", 0.20)],
    "en": [("digits", 0.50), ("en_words", 0.25), ("en_colloquial", 0.25)],
}


# ---------------------------------------------------------------------------- text set
def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def render_number(n: float, style: str) -> tuple[str, str]:
    """-> (text, style actually used)."""
    if style == "hi_colloquial":
        if float(n).is_integer() and n >= 150 and int(n) % 100 == 50:
            h = int(n) // 100
            if h == 1:
                return "डेढ़ सौ", style
            if h == 2:
                return "ढाई सौ", style
            return f"साढ़े {number_to_words(h, 'hi')} सौ", style
        style = "hi_words"
    if style == "en_colloquial":
        if float(n).is_integer() and 100 < n < 1000 and int(n) % 100:
            h, rest = divmod(int(n), 100)
            return f"{number_to_words(h, 'en')} {number_to_words(rest, 'en')}".replace("-", " "), style
        style = "en_words"
    if style == "hi_words":
        return number_to_words(n, "hi"), style
    if style == "en_words":
        return number_to_words(n, "en").replace("-", " "), style
    return _fmt(n), "digits"


def spoken_parts(name: str) -> tuple[list[str], float | None, list[str]]:
    """'Augmentin 625 Duo Tablet' -> (['augmentin'], 625.0, ['duo'])."""
    words = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", name)
    pre: list[str] = []
    post: list[str] = []
    number: float | None = None
    for w in words:
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            if number is None:
                number = float(w)
            continue
        if w.lower() in _DROP:
            continue
        (pre if number is None else post).append(w.lower())
    return pre, number, post


def eligible_brands(store: Store) -> list:
    salts = known_salts() | set(store.salt_use)
    out = []
    for b in sorted(store.brands.values(), key=lambda b: b.id):
        if not b.salts or b.family.lower() in salts or b.base.split()[0] in salts:
            continue  # "Paracetamol Paediatric Suspension" is a salt query, not a brand query
        pre, _, post = spoken_parts(b.name)
        if pre or post:
            out.append(b)
    return out


_ACRONYMS = {"sr", "gp", "lc", "cv", "o", "p", "fdc", "dr", "er", "xr", "xl", "cr", "od", "ds", "dt", "mr"}


def _pick(rng: random.Random, weighted: list[tuple[str, float]]) -> str:
    r = rng.random() * sum(w for _, w in weighted)
    for name, w in weighted:
        r -= w
        if r <= 0:
            return name
    return weighted[-1][0]


def _word(w: str, script: str, rng: random.Random) -> str:
    if script == "dev" and w in DEV_MAP:
        return DEV_MAP[w]
    if w in _ACRONYMS:
        return w.upper()
    return w.capitalize() if rng.random() < 0.6 else w


def tts_text(text: str, lang: str) -> str:
    """Timbre reads number words more reliably than digits."""
    return re.sub(r"\d+(?:\.\d+)?", lambda m: number_to_words(float(m.group(0)), "en" if lang == "en" else "hi"), text)


def build_utterances(store: Store, n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    brands = eligible_brands(store)
    if not brands:
        raise SystemExit("No eligible brands in the DB. Run: uv run python -m scripts.load_data --src data/seed_dev")
    if n <= len(brands):
        chosen = rng.sample(brands, n)
    else:
        chosen = [b for b in brands for _ in range(n // len(brands))]
        chosen += rng.sample(brands, n - len(chosen))
    rng.shuffle(chosen)
    templates = []
    while len(templates) < len(chosen):
        block = list(TEMPLATES)
        rng.shuffle(block)
        templates.extend(block)

    out = []
    for i, (b, tpl) in enumerate(zip(chosen, templates)):
        tid, carrier, lang, intent, pattern = tpl
        pre, number, post = spoken_parts(b.name)
        brand_script = "dev" if carrier == "dev" and rng.random() < 0.6 else "latin"
        pre_s = [_word(w, brand_script, rng) for w in pre]
        post_s = [_word(w, brand_script, rng) for w in post]
        strength_text, style = "", "none"
        if number is not None:
            style = _pick(rng, NUMBER_STYLES[carrier])
            strength_text, style = render_number(number, style)
        form_text = ""
        if b.form in FORM_SPOKEN and (b.form in ("oral_liquid", "drops") or rng.random() < 0.25):
            dev, lat = FORM_SPOKEN[b.form]
            form_text = dev if carrier == "dev" else lat
        brand_text = " ".join(pre_s)
        drug = " ".join(x for x in [brand_text, strength_text, " ".join(post_s), form_text] if x)
        text = pattern.format(drug=drug)
        out.append({
            "id": f"u{i + 1:03d}",
            "text": text,
            "truth_brand_id": b.id,
            "truth_brand": b.name,
            "truth_family": b.family,
            "truth_strength": number,
            "truth_strength_key": b.strength_key,
            "truth_form": b.form,
            "truth_release": b.release,
            "template": tid,
            "intent": intent,
            "carrier": carrier,
            "lang": lang,
            "brand_script": brand_script,
            "number_style": style,
            "brand_text": brand_text,
            "brand_latin": " ".join(pre),
            "strength_text": strength_text,
            "tts_text": tts_text(text, lang),
        })
    return out


# ---------------------------------------------------------------------------- audio
def _cache_path(u: dict, voice: str, model: str) -> Path:
    key = hashlib.sha1(f"{u['lang']}|{voice}|{model}|{SR}|{u['tts_text']}".encode()).hexdigest()
    return CLEAN_CACHE / f"{key}.pcm"


async def synthesize(utts: list[dict], yes: bool, limit: int | None, pause_s: float) -> None:
    import httpx

    from app.adapters.gnani_tts import TimbreTTS, TTSError

    s = get_settings()
    missing = [u for u in utts if not _cache_path(u, s.tts_voice, s.tts_model).exists()]
    cached = len(utts) - len(missing)
    todo = missing[:limit] if limit is not None else missing
    chars = sum(len(u["tts_text"]) for u in todo)
    print(f"Timbre: {len(todo)} REST calls needed ({chars} characters), {cached} utterances already cached "
          f"in {CLEAN_CACHE.relative_to(ROOT)}. Noise / babble / 8 kHz variants cost no extra calls.")
    if not todo:
        return
    if not s.gnani_api_key:
        raise SystemExit("GNANI_API_KEY is not set; cannot synthesize.")
    if not yes:
        raise SystemExit("These calls spend programme credits. Re-run with --yes to proceed "
                         "(try --limit 3 first for a pilot).")
    CLEAN_CACHE.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient() as client:
        tts = TimbreTTS(s, client)
        for k, u in enumerate(todo, 1):
            delay = 1.0
            for attempt in range(6):
                try:
                    pcm = await tts.synthesize(u["tts_text"], u["lang"], sample_rate=SR)
                    if len(pcm) < SR // 10:
                        raise TTSError("EMPTY_AUDIO", f"{len(pcm)} bytes", True)
                    break
                except TTSError as exc:
                    if not exc.retryable or attempt == 5:
                        raise SystemExit(f"Timbre failed on {u['id']}: {exc.code} {exc}")
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt == 5:
                        raise SystemExit(f"Timbre network error on {u['id']}: {exc}")
                print(f"  retry {u['id']} in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            _cache_path(u, s.tts_voice, s.tts_model).write_bytes(pcm)
            print(f"  [{k}/{len(todo)}] {u['id']} {len(pcm) / 2 / SR:.1f}s")
            await asyncio.sleep(pause_s)


def _pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def write_wav(path: Path, x: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def speech_power(x: np.ndarray) -> float:
    """Mean power over the non-silent part (frames within 40 dB of the loudest frame)."""
    frame = SR // 50
    n = len(x) // frame
    if n == 0:
        return float(np.mean(x ** 2) + 1e-12)
    e = np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1)
    active = e[e > e.max() * 1e-4]
    return float(active.mean() if active.size else e.mean()) + 1e-12


def white_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(n).astype(np.float32)


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(spec.size, dtype=np.float64)
    f[0] = 1.0
    x = np.fft.irfft(spec / np.sqrt(f), n)
    return (x / (np.std(x) + 1e-12)).astype(np.float32)


def babble_noise(n: int, others: list[np.ndarray], rng: np.random.Generator) -> np.ndarray:
    out = np.zeros(n, dtype=np.float32)
    for o in others:
        o = o / (np.sqrt(speech_power(o)))
        reps = int(np.ceil((n + len(o)) / len(o)))
        tiled = np.tile(o, reps)
        start = int(rng.integers(0, len(o)))
        out += tiled[start : start + n]
    return out


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    ps = speech_power(speech)
    pn = float(np.mean(noise ** 2)) + 1e-12
    scaled = noise * np.sqrt(ps / (pn * 10 ** (snr_db / 10)))
    y = speech + scaled
    peak = float(np.max(np.abs(y))) or 1.0
    return y * (0.99 / peak) if peak > 0.99 else y


def telephone(x16: np.ndarray) -> np.ndarray:
    """300-3400 Hz band-limit, 16 -> 8 kHz, G.711 mu-law round trip."""
    spec = np.fft.rfft(x16)
    freqs = np.fft.rfftfreq(len(x16), 1 / SR)
    spec[(freqs < 300) | (freqs > 3400)] = 0
    x8 = np.fft.irfft(spec, len(x16))[::2]
    mu = 255.0
    y = np.sign(x8) * np.log1p(mu * np.minimum(np.abs(x8), 1.0)) / np.log1p(mu)
    y = np.round(y * 127) / 127
    return (np.sign(y) * ((1 + mu) ** np.abs(y) - 1) / mu).astype(np.float32)


def _parse_snr(spec: str) -> list[float | None]:
    out: list[float | None] = []
    for tok in spec.split(","):
        tok = tok.strip().lower()
        if not tok or tok == "none":
            continue
        out.append(None if tok == "clean" else float(tok))
    return out


def build_variants(utts: list[dict], snrs: list[float | None], tel_snrs: list[float | None],
                   noise_types: list[str], seed: int) -> int:
    s = get_settings()
    clean: dict[str, np.ndarray] = {}
    for u in utts:
        p = _cache_path(u, s.tts_voice, s.tts_model)
        if p.exists():
            clean[u["id"]] = _pcm_to_float(p.read_bytes())
    if not clean:
        print("No synthesized audio yet; manifest not written.")
        return 0
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)
    ids = sorted(clean)
    lines = []
    for u in utts:
        if u["id"] not in clean:
            continue
        x = clean[u["id"]]
        truth = {k: v for k, v in u.items() if k.startswith("truth_")}
        base = {"id": u["id"], "text": u["text"], "lang": u["lang"], "template": u["template"], **truth}
        conds: dict[tuple[float | None, str], np.ndarray] = {}
        for snr in sorted(set(snrs) | set(tel_snrs), key=lambda v: 1e9 if v is None else -v):
            if snr is None:
                conds[(None, "none")] = x
                continue
            kind = pyrng.choice(noise_types)
            others = [o for o in ids if o != u["id"]]
            if kind == "babble" and len(others) < 3:
                kind = "pink"
            if kind == "white":
                noise = white_noise(len(x), rng)
            elif kind == "pink":
                noise = pink_noise(len(x), rng)
            else:
                picks = pyrng.sample(others, pyrng.choice((3, 4)) if len(others) >= 4 else 3)
                noise = babble_noise(len(x), [clean[o] for o in picks], rng)
            conds[(snr, kind)] = mix_at_snr(x, noise, snr)
        for (snr, kind), y in conds.items():
            tag = "clean" if snr is None else f"snr{int(snr)}_{kind}"
            if snr in snrs:
                f = AUDIO / "wb16k" / f"{u['id']}_{tag}_16k.wav"
                write_wav(f, y, SR)
                lines.append({**base, "file": str(f.relative_to(ROOT)), "snr_db": snr, "sample_rate": SR,
                              "noise": kind, "channel": "wideband"})
            if snr in tel_snrs:
                f = AUDIO / "tel8k" / f"{u['id']}_{tag}_8k.wav"
                write_wav(f, telephone(y), TEL_SR)
                lines.append({**base, "file": str(f.relative_to(ROOT)), "snr_db": snr, "sample_rate": TEL_SR,
                              "noise": kind, "channel": "telephone"})
    with (EVAL / "manifest.jsonl").open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    print(f"Wrote {len(lines)} WAVs for {len(clean)} utterances -> eval/manifest.jsonl")
    return len(lines)


# ---------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100, help="number of utterances (default 100)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", type=Path, default=None, help="SQLite DB (default: DAWA_DB_PATH / var/dawasaathi.db)")
    ap.add_argument("--out", type=Path, default=EVAL / "utterances.jsonl")
    ap.add_argument("--synthesize", action="store_true", help="render audio with Timbre (spends credits)")
    ap.add_argument("--yes", action="store_true", help="confirm spending Timbre credits")
    ap.add_argument("--limit", type=int, default=None, help="synthesize at most N new utterances (pilot)")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds between Timbre calls")
    ap.add_argument("--snr", default="clean,20,10,5,0", help="wideband 16 kHz conditions")
    ap.add_argument("--tel-snr", default="clean,20,10,5,0", help="telephone 8 kHz conditions ('none' to skip)")
    ap.add_argument("--noise-types", default="white,pink,babble")
    ap.add_argument("--variants-only", action="store_true", help="rebuild WAVs/manifest from cached audio, no calls")
    args = ap.parse_args()

    db = args.db or get_settings().db_path
    if not db.exists():
        raise SystemExit(f"{db} not found. Run: uv run python -m scripts.load_data --src data/seed_dev")
    conn = connect(db)
    store = Store.load(conn)
    conn.close()

    if args.out.exists() and (args.synthesize or args.variants_only):
        utts = [json.loads(ln) for ln in args.out.read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(f"Using existing {args.out.relative_to(ROOT)} ({len(utts)} utterances)")
    else:
        utts = build_utterances(store, args.n, args.seed)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            for u in utts:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        fams = len({u["truth_family"] for u in utts})
        print(f"Wrote {len(utts)} utterances ({fams} brand families, {len({u['truth_brand_id'] for u in utts})} SKUs) "
              f"-> {args.out.relative_to(ROOT)}")
        for u in utts[:5]:
            print("  ", u["id"], u["text"])

    if args.synthesize and not args.variants_only:
        asyncio.run(synthesize(utts, args.yes, args.limit, args.pause))
    if args.synthesize or args.variants_only:
        noise_types = [t.strip() for t in args.noise_types.split(",") if t.strip() in ("white", "pink", "babble")]
        build_variants(utts, _parse_snr(args.snr), _parse_snr(args.tel_snr), noise_types or ["white"], args.seed)


if __name__ == "__main__":
    main()
