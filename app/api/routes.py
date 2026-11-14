"""REST routes (/v1/*), health and metrics. Errors use {"error": {"code", "message"}}."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.guardrails import clean_input, public_response
from app.audio_io import prepare_for_prisma
from app.config import LANGS, TEMPLATE_LANGS
from app.dialogue.templates import load as load_templates
from app.dialogue.verbalize import speakable
from app.engine import alternatives as altmod
from app.engine import safety as safetymod
from app.engine.salts import canonical_form, canonical_release, canonical_salt, parse_composition
from app.metrics import METRICS
from app.models import DrugItem, SaltDose
from app.pipeline import QueryInput
from app.tts.cache import encode

router = APIRouter()

AUDIO_TYPES = ("audio/", "video/webm", "application/octet-stream")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# ---------------------------------------------------------------- rate limiting
_HITS: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    limit = request.app.state.settings.rate_limit_per_min
    key = request.client.host if request.client else "anon"
    now = time.time()
    q = _HITS[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise APIError(429, "RATE_LIMITED", "Too many requests; please wait a minute.")
    q.append(now)


async def _read_upload(f: UploadFile | None, max_bytes: int, kinds: tuple[str, ...], label: str) -> bytes | None:
    if f is None or not f.filename and not f.size:
        return None
    ctype = (f.content_type or "").lower()
    if not any(ctype.startswith(k) for k in kinds):
        raise APIError(415, "UNSUPPORTED_MEDIA", f"{label}: unsupported type {ctype or 'unknown'}")
    data = await f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise APIError(413, "TOO_LARGE", f"{label} is larger than {max_bytes // (1024 * 1024)} MB")
    return data or None


def _parse_pouch(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v)[:80] for v in val][:50]
    except ValueError:
        pass
    return [p.strip()[:80] for p in raw.split(",") if p.strip()][:50]


def _lang(lang: str | None) -> str:
    return lang if lang in LANGS else "hi"


# ---------------------------------------------------------------- main call
@router.post("/v1/query")
async def query(
    request: Request,
    audio: UploadFile | None = File(None),
    strip: UploadFile | None = File(None),
    rx: UploadFile | None = File(None),
    lang: str = Form("hi"),
    session_id: str | None = Form(None),
    pouch: str | None = Form(None),
    text: str | None = Form(None),
    channel: str = Form("web"),
):
    rate_limit(request)
    s = request.app.state.settings
    audio_b = await _read_upload(audio, s.max_audio_bytes, AUDIO_TYPES, "audio")
    strip_b = await _read_upload(strip, s.max_image_bytes, IMAGE_TYPES, "strip")
    rx_b = await _read_upload(rx, s.max_image_bytes, IMAGE_TYPES, "rx")
    if text is not None:
        text = clean_input(text, 500)
    if not (audio_b or strip_b or rx_b or text):
        raise APIError(400, "EMPTY_REQUEST", "Send audio, text, a strip photo or a prescription photo.")
    audio_name = "audio.wav"
    if audio_b and not text:
        try:
            audio_b, audio_name = await asyncio.to_thread(prepare_for_prisma, audio_b, audio.filename)
        except (ValueError, RuntimeError) as exc:
            raise APIError(400, "AUDIO_DECODE", str(exc)) from exc
    inp = QueryInput(session_id=session_id, lang=_lang(lang), audio=audio_b, audio_name=audio_name, text=text,
                     strip=strip_b, rx=rx_b, pouch=_parse_pouch(pouch),
                     channel=channel if channel in ("web", "phone") else "web")
    resp = await request.app.state.pipeline.query(inp)
    return JSONResponse(public_response(resp.model_dump(mode="json"), s, s.expose_debug))


class ConfirmBody(BaseModel):
    session_id: str
    candidate_id: str | None = None
    none: bool = False
    lang: str | None = None


@router.post("/v1/confirm")
async def confirm(request: Request, body: ConfirmBody):
    rate_limit(request)
    if not body.none and not body.candidate_id:
        raise APIError(400, "BAD_CONFIRM", "Send candidate_id or none: true.")
    resp = await request.app.state.pipeline.confirm(body.session_id, body.candidate_id, body.none,
                                                    _lang(body.lang) if body.lang else None)
    s = request.app.state.settings
    return JSONResponse(public_response(resp.model_dump(mode="json"), s, s.expose_debug))


class SpeakBody(BaseModel):
    session_id: str
    which: str = "chemist"
    lang: str | None = None


@router.post("/v1/speak")
async def speak(request: Request, body: SpeakBody):
    """On-demand audio for the Speak-to-Chemist script of the session's last reply."""
    rate_limit(request)
    session = request.app.state.sessions.get(body.session_id)
    if session is None or body.which != "chemist" or not session.last_chemist_text:
        raise APIError(404, "NOTHING_TO_SPEAK", "No chemist script for this session.")
    lang = _lang(body.lang) if body.lang else session.lang
    job = request.app.state.audio.create(lang, [speakable(session.last_chemist_text, lang)])
    return {"audio_id": job.id, "audio_stream_url": f"/v1/audio/{job.id}.pcm",
            "audio_url": f"/v1/audio/{job.id}.ogg", "tts": "timbre" if job.live else "mock",
            "text": session.last_chemist_text}


class InterruptBody(BaseModel):
    session_id: str


@router.post("/v1/interrupt")
async def interrupt(request: Request, body: InterruptBody):
    """REST barge-in: stop generating the current reply audio for this session."""
    session = request.app.state.sessions.get(body.session_id)
    if session and session.current_audio_id:
        request.app.state.audio.cancel(session.current_audio_id)
        METRICS.count("barge_in")
    return {"ok": True}


# ---------------------------------------------------------------- lookups
def _item_from_params(salt: str, strength: str | None, form: str | None, release: str | None) -> DrugItem:
    if strength:
        names = [canonical_salt(x) for x in salt.split(",")]
        doses = parse_composition(" + ".join(f"x {s}" for s in strength.split(",")))
        if len(doses) != len(names):
            raise APIError(400, "BAD_STRENGTH", "strength must list one value per salt, e.g. 500mg,125mg")
        salts = [SaltDose(name=n, amount=d.amount, unit=d.unit, per_ml=d.per_ml) for n, d in zip(names, doses)]
    else:
        salts = parse_composition(salt) or [SaltDose(name=canonical_salt(x)) for x in salt.split(",")]
    return DrugItem(source="db", salts=salts, form=canonical_form(form), release=canonical_release(release) or "IR")


@router.get("/v1/alternatives")
async def alternatives(request: Request, salt: str, strength: str | None = None, form: str = "tablet",
                       release: str | None = None, limit: int = Query(3, ge=1, le=10)):
    item = _item_from_params(salt, strength, form, release)
    store = request.app.state.store
    s = request.app.state.settings
    banned = safetymod.check_banned(store, item, s.stale_days)
    alts = [] if banned.status == "banned" else altmod.cheaper(store, item, limit, s.stale_days)
    return {"query": item.model_dump(), "banned": banned.model_dump(),
            "alternatives": [a.model_dump() for a in alts]}


@router.get("/v1/safety/banned")
async def banned(request: Request, salts: str, form: str | None = None, strengths: str | None = None):
    item = _item_from_params(salts, strengths, form, None)
    res = safetymod.check_banned(request.app.state.store, item, request.app.state.settings.stale_days)
    return {"query": {"salts": item.salt_names(), "form": item.form}, **res.model_dump()}


@router.get("/v1/drugs/search")
async def drugs_search(request: Request, q: str = Query(..., min_length=2, max_length=80),
                       limit: int = Query(8, ge=1, le=25)):
    from app.nlu.entities import extract

    ents = extract(q)
    res = request.app.state.index.search(ents.name_tokens or q.split(), strength=ents.strength,
                                         strengths=ents.strengths, form=ents.form, release=ents.release,
                                         k=limit)
    store = request.app.state.store
    out = []
    for c in res.candidates[:limit]:
        if c.brand_id is not None:
            b = store.brands[c.brand_id]
            out.append({"brand_id": b.id, "name": b.name, "salts": [x.label() for x in b.salts], "form": b.form,
                        "release": b.release, "mrp": b.mrp, "score": round(c.score, 3)})
        else:
            out.append({"salt": c.salt, "score": round(c.score, 3)})
    return {"q": q, "confident": res.confident, "results": out}


# ---------------------------------------------------------------- audio
@router.get("/v1/audio/filler")
async def filler(request: Request, lang: str = "hi", sr: int | None = None):
    """Short 'one moment' line, cached, played while the pipeline runs."""
    lang = lang if lang in TEMPLATE_LANGS else "en"
    text = speakable(load_templates(lang)["lines"]["filler"], lang)
    job = request.app.state.audio.create(lang, [text], sr)
    await job.task
    return Response(job.pcm(), media_type="application/octet-stream",
                    headers={"X-Sample-Rate": str(job.sample_rate), "Cache-Control": "no-store",
                             "X-TTS": "timbre" if job.live else "mock"})


@router.get("/v1/audio/{audio_file}")
async def audio_file(request: Request, audio_file: str):
    job_id, _, ext = audio_file.partition(".")
    job = request.app.state.audio.get(job_id)
    if job is None:
        raise APIError(404, "AUDIO_NOT_FOUND", "Audio expired or unknown id.")
    if ext == "pcm":
        return StreamingResponse(job.iter_pcm(), media_type="application/octet-stream",
                                 headers={"X-Sample-Rate": str(job.sample_rate), "Cache-Control": "no-store",
                                          "X-TTS": "timbre" if job.live else "mock"})
    if ext not in ("ogg", "wav", "mp3"):
        raise APIError(400, "BAD_FORMAT", "Use .pcm, .wav, .ogg or .mp3")
    if job.task and not job.task.done():
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    data, mime = await asyncio.to_thread(encode, job.pcm(), job.sample_rate, ext)
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


class PrewarmBody(BaseModel):
    langs: list[str] = ["hi", "en"]
    top_n_brands: int = 0


@router.post("/v1/tts/prewarm")
async def prewarm(request: Request, body: PrewarmBody, x_admin_token: str | None = Header(None)):
    s = request.app.state.settings
    if not s.admin_token or x_admin_token != s.admin_token:
        raise APIError(401, "UNAUTHORIZED", "Set ADMIN_TOKEN and send it as X-Admin-Token.")
    items: list[tuple[str, str]] = []
    for lang in body.langs:
        if lang not in TEMPLATE_LANGS:
            continue
        t = load_templates(lang)
        fixed = list(t["lines"].values()) + list(t["verdict_plain"].values())
        fixed += [v for k, v in t["clauses"].items() if "{" not in v]
        fixed += [v for k, v in t["verdict"].items() if "{" not in v]
        for line in fixed:
            if "{" not in line:
                items.append((lang, speakable(line, lang)))
    pipeline = request.app.state.pipeline
    if body.top_n_brands > 0:
        from app.dialogue.templates import RenderContext, render
        from app.engine import price as pricemod, rules

        store = request.app.state.store
        fam_seen: set[str] = set()
        for b in sorted(store.brands.values(), key=lambda b: b.name):
            if b.family in fam_seen:
                continue
            fam_seen.add(b.family)
            if len(fam_seen) > body.top_n_brands:
                break
            item = store.brand_item(b.id, "voice")
            safety = safetymod.check_banned(store, item, s.stale_days)
            alts = [] if safety.status == "banned" else altmod.cheaper(store, item, 3, s.stale_days)
            price = pricemod.price_gap(store, item, s.nppa_gst_pct, s.stale_days)
            verdict = rules.lookup(item, safety, bool(alts))
            for lang in body.langs:
                if lang not in TEMPLATE_LANGS:
                    continue
                r = render(RenderContext(lang=lang, status="ok", intent="FIND_GENERIC", verdict=verdict,
                                         item=item, safety=safety, price=price, alternatives=alts))
                items.extend((lang, speakable(seg.text, lang)) for seg in r.segments)
    result = await pipeline.audio.prewarm(list(dict.fromkeys(items)))
    return {"segments": len(set(items)), **result}


# ---------------------------------------------------------------- stretch: grievance draft
class GrievanceBody(BaseModel):
    session_id: str
    note: str | None = None


@router.post("/v1/grievance/draft")
async def grievance_draft(request: Request, body: GrievanceBody):
    """Builds a complaint draft from the last verdict. Never submitted anywhere."""
    session = request.app.state.sessions.get(body.session_id)
    if session is None or session.last_item is None:
        raise APIError(404, "NO_VERDICT", "No recent verdict in this session.")
    item: DrugItem = session.last_item
    return {
        "submitted": False,
        "channel_hint": "NPPA Pharma Jan Samadhan / State Drugs Controller (user files it themselves)",
        "draft": {
            "subject": f"Query about {item.brand or ' + '.join(item.salt_names())}",
            "medicine": item.model_dump(include={"brand", "salts", "form", "release", "mrp", "batch_no"}),
            "user_note": (body.note or "")[:500],
            "statement": "I request verification of the above medicine's price / composition.",
        },
        "warning": "Review before filing. Do not include Aadhaar, PAN or phone numbers here.",
    }


# ---------------------------------------------------------------- ops
@router.get("/healthz")
async def healthz(request: Request):
    st = request.app.state
    return {"ok": True, "brands": len(st.store.brands), "banned_entries": len(st.store.banned),
            "price_refs": len(st.store.price_refs), "stt": "prisma" if st.settings.stt_live else "mock",
            "tts": "timbre" if st.audio.live else "mock", "ocr": st.ocr.provider, "sessions": len(st.sessions)}


@router.get("/metrics")
async def metrics():
    return METRICS.snapshot()


@router.get("/v1/config")
async def config(request: Request):
    st = request.app.state
    langs = []
    for code in LANGS:
        langs.append({"code": code, "templates": code in TEMPLATE_LANGS,
                      "reviewed": load_templates(code).get("reviewed_by") is not None if code in TEMPLATE_LANGS
                      else False})
    return {"languages": langs, "stt": "prisma" if st.settings.stt_live else "mock",
            "tts": "timbre" if st.audio.live else "mock", "ocr": st.ocr.provider,
            "sample_rate": st.settings.tts_sample_rate,
            "data_sources": st.pipeline._data_sources()}
