"""FastAPI app: one stateless-ish service (sessions live in memory for 15 minutes)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.adapters.gnani_stt import MockSTT, PrismaSTT
from app.adapters.gnani_tts import MockTTS, TimbreTTS
from app.adapters.llm import GroqLLM
from app.adapters.ocr import OCRReader
from app.api import routes, stream, whatsapp
from app.api.routes import APIError, error_response
from app.api.sessions import SessionStore
from app.config import ROOT, Settings, get_settings
from app.db import Store, connect
from app.match.index import MatchIndex
from app.adapters.tavily import Tavily
from app.engine.web_uses import WebUseLookup
from app.pipeline import Pipeline
from app.tts.cache import AudioService, SegmentCache

logging.basicConfig(level=logging.INFO, format="%(message)s")


def build_state(app: FastAPI, settings: Settings, stt=None, tts=None, ocr=None, llm=None) -> None:
    conn = connect(settings.db_path)
    store = Store.load(conn)
    conn.close()
    index = MatchIndex(store).build()
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    if stt is None:
        stt = PrismaSTT(settings, http) if settings.stt_live else MockSTT()
    if tts is None:
        tts = TimbreTTS(settings, http) if settings.tts_live else MockTTS(settings.tts_sample_rate)
    if ocr is None:
        ocr = OCRReader(settings, http)
    cache = SegmentCache(settings.cache_dir, settings.tts_voice, settings.tts_model)
    audio = AudioService(tts, cache, settings.tts_sample_rate, ttl_s=settings.session_ttl_s)
    sessions = SessionStore(settings.session_ttl_s)
    app.state.settings = settings
    app.state.store = store
    app.state.index = index
    app.state.http = http
    app.state.ocr = ocr
    app.state.audio = audio
    app.state.sessions = sessions
    if llm is None and (settings.llm_nlu or settings.llm_reply):
        llm = GroqLLM(settings, http)
    app.state.llm = llm
    web_uses = None
    if settings.web_lookup:
        web_llm = llm if llm is not None else GroqLLM(settings, http)
        web_uses = WebUseLookup(Tavily(settings.tavily_api_key, http), web_llm,
                                settings.cache_dir.parent / "web_uses.json")
    app.state.pipeline = Pipeline(settings, store, index, stt, ocr, audio, sessions, llm, web_uses)


def create_app(settings: Settings | None = None, **overrides) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not getattr(app.state, "pipeline", None):
            build_state(app, settings, **overrides)
        yield
        await app.state.http.aclose()

    app = FastAPI(title="Dawaa Saathi", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return error_response(422, "VALIDATION", str(exc.errors()[:3]))

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    app.include_router(routes.router)
    app.include_router(stream.router)
    app.include_router(whatsapp.router)

    web = ROOT / "web"
    if web.exists():
        @app.get("/sw.js", include_in_schema=False)
        async def _sw():
            return FileResponse(web / "sw.js", media_type="application/javascript",
                                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=Path(web), html=True), name="web")
    return app


app = create_app()
