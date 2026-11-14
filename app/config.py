"""Runtime settings, read once from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    return int(_get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_get(name, str(default)))


# UI language -> (Prisma language_code, Timbre language)
LANGS: dict[str, tuple[str, str]] = {
    "hi": ("hi-IN", "hi-IN"),
    "en": ("en-IN", "en-IN"),
    "mr": ("mr-IN", "mr-IN"),
    "ta": ("ta-IN", "ta-IN"),
    "te": ("te-IN", "te-IN"),
}
TEMPLATE_LANGS = ("hi", "en", "mr")


@dataclass(frozen=True)
class Settings:
    gnani_api_key: str | None
    gnani_base_url: str
    gnani_ws_url: str
    stt_model: str
    stt_model_2: str  # second Prisma model for PRISMA_DUAL
    stt_dual: bool
    stt_format: str
    stt_codemix_with_english: bool
    stt_keyterms_n: int
    tts_model: str
    tts_voice: str
    tts_sample_rate: int
    tts_speed: float
    ocr_providers: tuple[str, ...]
    detector_model: Path
    groq_api_keys: tuple[str, ...]
    groq_vision_model: str
    groq_llm_model: str
    llm_mode: str  # off | nlu | reply | both  (active only when a Groq key is set)
    llm_timeout_s: float
    tavily_api_key: str | None
    web_lookup_on: bool  # WEB_LOOKUP=1: usual use from trusted sites when our data has none
    anthropic_model: str
    db_path: Path
    cache_dir: Path
    session_ttl_s: int
    match_t1: float
    match_t2: float
    stale_days: int
    nppa_gst_pct: float
    max_audio_bytes: int
    max_image_bytes: int
    rate_limit_per_min: int
    admin_token: str | None
    whatsapp_token: str | None
    whatsapp_phone_id: str | None
    whatsapp_verify_token: str | None
    public_base_url: str | None

    @property
    def stt_live(self) -> bool:
        return bool(self.gnani_api_key)

    @property
    def llm_nlu(self) -> bool:
        return bool(self.groq_api_keys) and self.llm_mode in ("nlu", "both")

    @property
    def llm_reply(self) -> bool:
        return bool(self.groq_api_keys) and self.llm_mode in ("reply", "both")

    @property
    def web_lookup(self) -> bool:
        return self.web_lookup_on and bool(self.tavily_api_key) and bool(self.groq_api_keys)

    @property
    def tts_live(self) -> bool:
        return bool(self.gnani_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    return Settings(
        gnani_api_key=_get("GNANI_API_KEY"),
        gnani_base_url=_get("GNANI_BASE_URL", "https://api.vachana.ai"),
        gnani_ws_url=_get("GNANI_WS_URL", "wss://api.vachana.ai"),
        stt_model=_get("PRISMA_MODEL", "prisma-v2.5"),
        stt_model_2=_get("PRISMA_MODEL_2", "prisma-v3.0"),
        stt_dual=_get("PRISMA_DUAL", "0") == "1",
        stt_format=_get("PRISMA_FORMAT", "codemix"),
        stt_codemix_with_english=_get("PRISMA_ADD_ENGLISH", "1") == "1",
        stt_keyterms_n=_int("PRISMA_KEYTERMS", 150),
        tts_model=_get("TIMBRE_MODEL", "timbre-v3.0"),
        tts_voice=_get("TIMBRE_VOICE", "Shivani"),
        tts_sample_rate=_int("TIMBRE_SAMPLE_RATE", 24000),
        tts_speed=_float("TIMBRE_SPEED", 1.0),
        ocr_providers=tuple(p.strip() for p in _get("OCR_PROVIDERS", "groq,anthropic,rapidocr,tesseract").split(",")
                            if p.strip()),
        groq_api_keys=tuple(dict.fromkeys(
            k.strip() for k in ((_get("GROQ_API_KEYS") or "") + "," + (_get("GROQ_API_KEY") or "")).split(",")
            if k.strip())),
        groq_vision_model=_get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
        groq_llm_model=_get("GROQ_LLM_MODEL", "openai/gpt-oss-120b"),
        llm_mode=(_get("LLM_MODE", "both") or "both").lower(),
        llm_timeout_s=_float("LLM_TIMEOUT_S", 6.0),
        tavily_api_key=_get("TAVILY_API_KEY"),
        web_lookup_on=(_get("WEB_LOOKUP", "1") or "1") == "1",
        anthropic_model=_get("ANTHROPIC_OCR_MODEL", "claude-opus-5-5"),
        db_path=Path(_get("DAWA_DB_PATH", str(ROOT / "var" / "dawasaathi.db"))),
        detector_model=Path(_get("DETECTOR_MODEL", str(ROOT / "var" / "models" / "pack_detector.onnx"))),
        cache_dir=Path(_get("DAWA_CACHE_DIR", str(ROOT / "var" / "tts_cache"))),
        session_ttl_s=_int("SESSION_TTL_S", 900),
        match_t1=_float("MATCH_T1", 0.80),
        match_t2=_float("MATCH_T2", 0.06),
        stale_days=_int("DATA_STALE_DAYS", 400),
        nppa_gst_pct=_float("NPPA_GST_PCT", 12.0),
        max_audio_bytes=_int("MAX_AUDIO_BYTES", 6 * 1024 * 1024),
        max_image_bytes=_int("MAX_IMAGE_BYTES", 10 * 1024 * 1024),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 40),
        admin_token=_get("ADMIN_TOKEN"),
        whatsapp_token=_get("WHATSAPP_TOKEN"),
        whatsapp_phone_id=_get("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_verify_token=_get("WHATSAPP_VERIFY_TOKEN"),
        public_base_url=_get("PUBLIC_BASE_URL"),
    )
