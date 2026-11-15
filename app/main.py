"""FastAPI app. Short-term session context lives in memory (15 minutes); chat history and
web-learned brands live in the local SQLite file (app/localdb.py)."""

from __future__ import annotations

import logging
import threading
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
from app.adapters.detect import PackDetector
from app.adapters.ocr import OCRReader
from app.api import routes, stream
from app.api.routes import APIError, error_response
from app.api.sessions import SessionStore
from app.config import ROOT, Settings, get_settings
from app.db import Store, connect
from app.match.index import MatchIndex
from app.adapters.tavily import Tavily
from app.engine.web_brands import WebBrandLookup
from app.engine.web_uses import WebUseLookup
from app.localdb import LocalDB
from app.pipeline import Pipeline
from app.tts.cache import AudioService, SegmentCache

logging.basicConfig(level=logging.INFO, format="%(message)s")


def build_state(app: FastAPI, settings: Settings, stt=None, tts=None, ocr=None, llm=None) -> None:
    conn = connect(settings.db_path)
    store = Store.load(conn)
    conn.close()
    if settings.data1_only and not store.dataset("data1"):
        raise RuntimeError("DATA1_ONLY requires the data1 database. Run: python -m scripts.load_data --src data1")
    index = MatchIndex(store).build()
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    if stt is None:
        stt = PrismaSTT(settings, http) if settings.stt_live else MockSTT()
    if tts is None:
        tts = TimbreTTS(settings, http) if settings.tts_live else MockTTS(settings.tts_sample_rate)
    if ocr is None:
        detector = PackDetector(settings.detector_model)
        ocr = OCRReader(settings, http, detector=detector if detector.available else None)
    cache = SegmentCache(settings.cache_dir, settings.tts_voice, settings.tts_model)
    audio = AudioService(tts, cache, settings.tts_sample_rate, ttl_s=settings.session_ttl_s)
    local = LocalDB(settings.local_db_path)
    sessions = SessionStore(settings.session_ttl_s, history=local)
    app.state.settings = settings
    app.state.store = store
    app.state.index = index
    app.state.http = http
    app.state.ocr = ocr
    app.state.audio = audio
    app.state.sessions = sessions
    app.state.local = local
    if llm is None and (settings.llm_nlu or settings.llm_reply):
        llm = GroqLLM(settings, http)
    app.state.llm = llm
    web_uses = None
    if settings.web_lookup:
        web_llm = llm if llm is not None else GroqLLM(settings, http)
        web_uses = WebUseLookup(Tavily(settings.tavily_api_key, http), web_llm,
                                settings.cache_dir.parent / "web_uses.json")
    tavily = Tavily(settings.tavily_api_key, http)
    web_brands = WebBrandLookup(tavily, llm if llm is not None else GroqLLM(settings, http), local, store, index)
    # Legacy mode can reuse learned web brands; data1-only mode must never load them.
    learned = 0 if settings.data1_only else web_brands.load_learned()
    if learned:
        logging.getLogger("dawasaathi").info("loaded %d web-learned brands from %s", learned, settings.local_db_path)
    app.state.pipeline = pipeline = Pipeline(settings, store, index, stt, ocr, audio, sessions, llm, web_uses,
                                             web_brands=web_brands if settings.web_brands else None,
                                             history=local)
    if isinstance(ocr, OCRReader) and "rapidocr" in ocr.available():
        # Load the OCR models and the salt vocabulary off the request path (~1-2 s).
        threading.Thread(target=lambda: (ocr.local.warm(), pipeline.vocab), daemon=True).start()


def create_app(settings: Settings | None = None, **overrides) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not getattr(app.state, "pipeline", None):
            build_state(app, settings, **overrides)
        yield
        await app.state.http.aclose()
        app.state.local.close()

    app = FastAPI(title="Dawaa Saathi", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logging.getLogger("dawasaathi").exception("unhandled error: %s", type(exc).__name__)
        return error_response(500, "INTERNAL", "Something went wrong. Please try again.")

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

    web = ROOT / "web"
    if web.exists():
        @app.get("/sw.js", include_in_schema=False)
        async def _sw():
            return FileResponse(web / "sw.js", media_type="application/javascript",
                                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=Path(web), html=True), name="web")
    return app


app = create_app()
