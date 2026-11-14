"""Strip / prescription reading.

Workflow for one photo:
1. Detect (optional, app/adapters/detect.py): zero-shot YOLO-World finds the pack (strip / bottle /
   box); when it covers a small part of the photo, the padded crop is read instead of the photo.
2. Read, in parallel:
   a. a vision-language model reads brand, each salt with its strength, form, release, MRP, batch
      into a fixed JSON schema (Groq qwen/qwen3.8-27b ~0.7 s, or Claude);
   b. RapidOCR (PP-OCR text detection + recognition, ONNX, offline) reads every printed line.
   The VLM answer is primary; the OCR lines cross-check it and give the brand matcher more text.
   When the VLM is unavailable / rate-limited, the OCR lines alone are used; Tesseract is last.
3. Identification against the brand DB happens later in the pipeline (closed-set matching), so
   the readers only have to read, not know drugs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
from collections import OrderedDict
import json
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.adapters.detect import DetectResult, PackDetector
from app.config import Settings


class OCRSalt(BaseModel):
    name: str
    amount: float | None = None
    unit: str | None = None
    per_ml: float | None = None


class OCRItem(BaseModel):
    brand: str | None = None
    salts: list[OCRSalt] = Field(default_factory=list)
    composition_text: str | None = None
    form: str | None = None
    release: str | None = None
    mrp: float | None = None
    pack_qty: float | None = None
    pack_unit: str | None = None
    batch_no: str | None = None
    expiry: str | None = None
    manufacturer: str | None = None
    directions: str | None = None
    category: str | None = None  # "medicine" | "supplement" (label says dietary / food supplement)
    raw_text: str | None = None  # OCR text in reading order, used for closed-set matching
    big_text: list[str] = Field(default_factory=list)  # OCR lines printed largest (brand candidates)


class OCRPayload(BaseModel):
    kind: Literal["strip", "rx", "unknown"] = "unknown"
    readable: bool = True
    items: list[OCRItem] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):  # models say "bottle", "box", "carton", "label" for a pack photo
        if v in ("strip", "rx", "unknown"):
            return v
        if isinstance(v, str) and re.search(r"pres|rx", v, re.I):
            return "rx"
        return "strip" if v else "unknown"


@dataclass
class OCRResult:
    payload: OCRPayload
    provider: str
    latency_ms: float
    error: str | None = None
    detect: dict | None = None  # pack detector output (label, conf, area, cropped)
    local_ms: float | None = None  # RapidOCR time when it ran beside the VLM


STRIP_PROMPT = """You are reading a photo of an Indian medicine pack: usually the BACK of a blister
strip (shiny, curved foil with repeated printing), or a bottle label / carton. Read ONLY what is
printed. Do not use outside knowledge to fill gaps; use null when unreadable.

Return JSON with this shape:
{"kind":"strip","readable":true,"items":[{"brand":str|null,"salts":[{"name":str,"amount":number|null,
"unit":"mg"|"mcg"|"g"|"iu"|null,"per_ml":number|null}],"composition_text":str|null,"form":str|null,
"release":"IR"|"SR"|"ER"|"DR"|null,"mrp":number|null,"pack_qty":number|null,"pack_unit":str|null,
"batch_no":str|null,"expiry":str|null,"manufacturer":str|null,"directions":null,
"category":"medicine"|"supplement"}],"notes":str|null}

Rules:
- One item for the pack. brand = the trade name as printed incl. its number/suffix (e.g. "Augmentin 625 Duo").
- salts: every active ingredient separately, with the strength printed for THAT ingredient
  ("Amoxycillin 500 mg" and "Clavulanic Acid 125 mg"). Never use a brand number such as 625 as a
  salt strength. For "X eq. to Y 500 mg" use Y. For liquids give per_ml (e.g. 5 for "per 5 ml").
- composition_text: the composition line(s) verbatim.
- form: as printed (tablet, film-coated tablet, capsule, suspension, syrup, drops, injection...).
- release: SR/ER/XR/CR/DR/gastro-resistant if printed, else null.
- mrp: rupees for the whole strip/bottle as printed; pack_qty/pack_unit e.g. 10 "tablets", 60 "ml".
- category: "supplement" when the label says dietary / food / health supplement or nutraceutical,
  else "medicine".
- kind is always "strip" for a pack photo (strip, bottle, box or carton).
- If the image is not a medicine pack or is unreadable, return readable=false and items=[].
Return only the JSON object."""

RX_PROMPT = """You are reading a photo of an Indian doctor's prescription (handwritten or printed).
List each medicine line exactly as written. Do not guess; use null when unreadable.

Return JSON:
{"kind":"rx","readable":true,"items":[{"brand":str|null,"salts":[{"name":str,"amount":number|null,
"unit":str|null,"per_ml":number|null}],"composition_text":str|null,"form":str|null,
"release":"IR"|"SR"|"ER"|"DR"|null,"mrp":null,"pack_qty":null,"pack_unit":null,"batch_no":null,
"expiry":null,"manufacturer":null,"directions":str|null}],"notes":str|null}

Rules:
- brand: the medicine name as written, with its strength number if written next to it ("Augmentin 625").
- salts: only if the prescription writes the generic/salt name; otherwise [].
- form: Tab/Cap/Syp/Inj -> tablet/capsule/syrup/injection when written.
- directions: dose/frequency/duration text verbatim (e.g. "1-0-1 x 5 days after food").
- Ignore patient name, phone numbers, addresses and doctor registration numbers; never copy them.
Return only the JSON object."""


def prepare_image(data: bytes, max_side: int = 1280) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), "image/jpeg"


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


class RateLimited(Exception):
    pass


class OCRReader:
    """Provider chain (OCR_PROVIDERS, default groq,anthropic,tesseract). Groq keys from
    GROQ_API_KEYS rotate on 429. Results are cached in memory by image hash (the image itself
    is never stored)."""

    CACHE_SIZE = 256

    def __init__(self, settings: Settings, client: httpx.AsyncClient, detector: PackDetector | None = None,
                 local: "LocalOCR | None" = None):
        self.s = settings
        self.client = client
        self._anthropic = None
        self._key_idx = 0
        self._cache: OrderedDict[str, OCRResult] = OrderedDict()
        self.detector = detector
        self.local = local if local is not None else LocalOCR()

    def available(self) -> list[str]:
        out = []
        for p in self.s.ocr_providers:
            if p == "groq" and self.s.groq_api_keys:
                out.append(p)
            elif p == "anthropic" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                out.append(p)
            elif p == "rapidocr" and self.local.available:
                out.append(p)
            elif p == "tesseract" and shutil.which("tesseract"):
                out.append(p)
        return out

    @property
    def provider(self) -> str:
        avail = self.available()
        return avail[0] if avail else "none"

    async def read_strip(self, image: bytes) -> OCRResult:
        return await self._read(image, STRIP_PROMPT, "strip")

    async def read_rx(self, image: bytes) -> OCRResult:
        return await self._read(image, RX_PROMPT, "rx")

    async def _read(self, image: bytes, prompt: str, kind: str) -> OCRResult:
        t0 = time.perf_counter()
        key = hashlib.sha1(kind.encode() + image).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            hit = self._cache[key]
            return OCRResult(hit.payload.model_copy(deep=True), hit.provider + "+cache", 0.0, hit.error)
        detect_meta = None
        if kind == "strip" and self.detector is not None and self.detector.available:
            try:
                det: DetectResult = await asyncio.to_thread(self.detector.detect, image)
            except Exception as exc:  # a broken model file must not block reading the photo
                detect_meta = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            else:
                detect_meta = {"ms": det.ms, "cropped": det.crop is not None,
                               "pack": None if det.used is None else {
                                   "label": det.used.label, "conf": det.used.conf, "area": det.used.area,
                                   "box": list(det.used.box)},
                               "detections": [{"label": d.label, "conf": d.conf, "area": d.area}
                                              for d in det.detections[:3]]}
                if det.crop is not None:
                    image = det.crop
        jpeg, mime = await asyncio.to_thread(prepare_image, image)
        providers = self.available()
        vlm = [p for p in providers if p in ("groq", "anthropic")]
        # Offline OCR runs beside the VLM (cross-check + brand lines) and is the fallback reader.
        local_task = (asyncio.create_task(self._local(jpeg, kind))
                      if kind == "strip" and "rapidocr" in providers else None)
        errors: list[str] = []
        for provider in vlm:
            try:
                payload = await (self._groq(jpeg, mime, prompt) if provider == "groq"
                                 else self._claude(jpeg, mime, prompt))
            except Exception as exc:  # rate limit, network, schema -> next provider
                errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            payload.kind = kind if payload.kind == "unknown" else payload.kind
            if not payload.items and payload.readable:
                errors.append(f"{provider}: empty")
                continue
            local, local_ms = await self._await_local(local_task)
            if local is not None and local.items and payload.items:
                lo = local.items[0]
                for item in payload.items[:1]:
                    item.raw_text, item.big_text = lo.raw_text, lo.big_text
                    if item.category is None and lo.category:
                        item.category = lo.category
            res = OCRResult(payload, provider, (time.perf_counter() - t0) * 1000, "; ".join(errors) or None,
                            detect=detect_meta, local_ms=local_ms)
            self._cache[key] = res
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
            return res
        if local_task is not None:
            local, local_ms = await self._await_local(local_task)
            if local is not None and local.readable and local.items:
                return OCRResult(local, "rapidocr", (time.perf_counter() - t0) * 1000, "; ".join(errors) or None,
                                 detect=detect_meta, local_ms=local_ms)
            errors.append("rapidocr: nothing readable")
        if "tesseract" in providers:
            try:
                payload = await asyncio.to_thread(_tesseract, jpeg, kind)
            except Exception as exc:
                errors.append(f"tesseract: {type(exc).__name__}: {str(exc)[:160]}")
            else:
                payload.kind = kind if payload.kind == "unknown" else payload.kind
                return OCRResult(payload, "tesseract", (time.perf_counter() - t0) * 1000,
                                 "; ".join(errors) or None, detect=detect_meta)
        return OCRResult(OCRPayload(kind=kind, readable=False), "none", (time.perf_counter() - t0) * 1000,
                         "; ".join(errors) or "no OCR provider available", detect=detect_meta)

    async def _local(self, jpeg: bytes, kind: str) -> tuple[OCRPayload | None, float]:
        t = time.perf_counter()
        payload = await asyncio.to_thread(self.local.read, jpeg, kind)
        return payload, round((time.perf_counter() - t) * 1000, 1)

    @staticmethod
    async def _await_local(task) -> tuple[OCRPayload | None, float | None]:
        if task is None:
            return None, None
        try:
            return await task
        except Exception:
            return None, None

    async def _groq(self, jpeg: bytes, mime: str, prompt: str) -> OCRPayload:
        b64 = base64.b64encode(jpeg).decode()
        body = {
            "model": self.s.groq_vision_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
        }
        keys = self.s.groq_api_keys
        for attempt in range(len(keys)):
            api_key = keys[(self._key_idx + attempt) % len(keys)]
            resp = await self.client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body, timeout=httpx.Timeout(20.0, connect=5.0),
            )
            if resp.status_code == 429:
                continue
            resp.raise_for_status()
            self._key_idx = (self._key_idx + attempt) % len(keys)
            content = resp.json()["choices"][0]["message"]["content"]
            try:
                return OCRPayload.model_validate(_parse_json(content))
            except ValidationError as exc:
                raise ValueError(f"schema: {exc}") from exc
        self._key_idx = (self._key_idx + 1) % len(keys)
        raise RateLimited("all Groq keys rate-limited")

    async def _claude(self, jpeg: bytes, mime: str, prompt: str) -> OCRPayload:
        import anthropic

        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic()
        b64 = base64.standard_b64encode(jpeg).decode("utf-8")
        response = await self._anthropic.messages.parse(
            model=self.s.anthropic_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
            output_format=OCRPayload,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise ValueError(f"no parsed output (stop_reason={response.stop_reason})")
        return response.parsed_output


# ------------------------------------------------------------------ Tesseract fallback

_MRP_RE = re.compile(r"M\.?\s*R\.?\s*P\.?[^0-9]{0,25}(\d+(?:[.,]\d{1,2})?)", re.I)
_BATCH_RE = re.compile(r"\b(?:B\.?\s*No|Batch\s*No|Lot)\.?\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/]{2,14})", re.I)
_EXP_RE = re.compile(r"\bExp(?:iry)?\.?\s*[:.]?\s*(\d{1,2}\s*[/\-.]\s*\d{2,4})", re.I)
_PACK_RE = re.compile(r"(\d+)\s*(tablets?|tabs?|capsules?|caps?|ml)\b", re.I)
_MRP_PAISE_RE = re.compile(r"M\.?\s*R\.?\s*P\.?[^0-9]{0,25}\d+[.,]\d{2}\b", re.I)
_DOSE_LINE = re.compile(r"\d+(?:\.\d+)?\s*(mg|mcg|g|iu|ml)\b", re.I)


def _tesseract(jpeg: bytes, kind: str) -> OCRPayload:
    """Two passes (as-is, and grayscale x1.6) merged; foil glare defeats any single setting."""
    import subprocess

    base = Image.open(io.BytesIO(jpeg))
    gray = base.convert("L")
    gray = gray.resize((int(gray.width * 1.6), int(gray.height * 1.6)), Image.LANCZOS)
    texts = []
    for img in (base, gray):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(buf.getvalue())
            f.flush()
            try:
                out = subprocess.run(["tesseract", f.name, "stdout", "--psm", "6", "-l", "eng"],
                                     capture_output=True, text=True, timeout=20)
                texts.append(out.stdout)
            except (subprocess.TimeoutExpired, OSError):
                continue
    if not texts:
        return OCRPayload(kind=kind, readable=False, notes="tesseract failed")
    # Prefer the pass that found more dose lines; keep the other's lines for brand matching.
    texts.sort(key=lambda t: len(_DOSE_LINE.findall(t)), reverse=True)
    payload = parse_ocr_text(texts[0], kind)
    raw = "\n".join(texts)
    for item in payload.items:
        item.raw_text = raw[:4000]
    return payload


def parse_ocr_text(text: str, kind: str = "strip") -> OCRPayload:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return OCRPayload(kind=kind, readable=False)
    if kind == "rx":
        items: list[OCRItem] = []
        for ln in lines:
            m = re.match(r"^(?:\d+[.)]\s*)?(Tab|Tah|Cap|Syp|Syr|Inj)\.?\s+(.+)$", ln, re.I)
            if m:
                form = {"tab": "tablet", "tah": "tablet", "cap": "capsule", "syp": "syrup", "syr": "syrup",
                        "inj": "injection"}[m.group(1).lower()]
                items.append(OCRItem(brand=m.group(2).split("  ")[0][:60], form=form))
            elif items and items[-1].directions is None and re.search(r"\d\s*-\s*\d|days?|food|sos", ln, re.I):
                items[-1].directions = ln[:120]
        return OCRPayload(kind="rx", readable=bool(items), items=items, notes="tesseract")
    item = OCRItem()
    comp = [ln for ln in lines if _DOSE_LINE.search(ln) and not _MRP_RE.search(ln)]
    item.composition_text = " + ".join(comp) or None
    for ln in lines:
        if re.search(r"[A-Za-z]{4,}", ln) and not _DOSE_LINE.search(ln):
            item.brand = re.sub(r"[^A-Za-z0-9 \-]", "", ln).strip()[:60] or None
            break
    if m := _MRP_RE.search(text):
        item.mrp = float(m.group(1).replace(",", "."))
    if m := _BATCH_RE.search(text):
        item.batch_no = m.group(1)
    if m := _EXP_RE.search(text):
        item.expiry = m.group(1)
    if m := _PACK_RE.search(text):
        item.pack_qty = float(m.group(1))
        unit = m.group(2).lower()
        item.pack_unit = "tablets" if unit.startswith("tab") else "capsules" if unit.startswith("cap") else unit
    low = text.lower()
    for word in ("suspension", "syrup", "capsule", "tablet", "drops", "injection"):
        if word in low:
            item.form = word
            break
    if re.search(r"gastro[- ]?resistant|delayed", low):
        item.release = "DR"
    elif re.search(r"sustained|\bsr\b", low):
        item.release = "SR"
    elif re.search(r"extended|prolonged|\b(er|xr|cr)\b", low):
        item.release = "ER"
    return OCRPayload(kind="strip", readable=True, items=[item], notes="tesseract")


# ------------------------------------------------------------------ RapidOCR (offline)


class LocalOCR:
    """RapidOCR: PP-OCR text detection + recognition as ONNX models on CPU (~1-3 s per photo).

    Boxes are grouped into printed rows (reading order) so "Glibenclamide I.P." and its "5 mg"
    printed beside it end up on one line; the tallest text lines are kept as brand candidates.
    The models are fetched by the rapidocr package on first use, then reused from disk."""

    MIN_SCORE = 0.6

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        try:
            import rapidocr  # noqa: F401
            self.available = True
        except ImportError:  # pragma: no cover
            self.available = False

    def warm(self) -> None:
        if self.available:
            self._get()

    def _get(self):
        with self._lock:
            if self._engine is None:
                from rapidocr import RapidOCR

                self._engine = RapidOCR(params={"Global.log_level": "critical"})
            return self._engine

    def read(self, jpeg: bytes, kind: str = "strip") -> OCRPayload:
        engine = self._get()
        img = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))[:, :, ::-1]  # BGR like OpenCV
        with self._lock:
            res = engine(img)
        boxes = [] if res.boxes is None else list(res.boxes)
        cells = []
        for box, txt, score in zip(boxes, res.txts or (), res.scores or ()):
            if score < self.MIN_SCORE or not txt.strip():
                continue
            ys, xs = [p[1] for p in box], [p[0] for p in box]
            cells.append({"t": txt.strip(), "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys),
                          "x": min(xs), "s": float(score)})
        if not cells:
            return OCRPayload(kind=kind, readable=False, notes="rapidocr: no text")
        rows = group_rows(cells)
        text = "\n".join(rows)
        payload = parse_ocr_text(text, kind)
        tall = sorted((c for c in cells if c["s"] >= 0.8 and re.search(r"[A-Za-z]{3,}", c["t"])),
                      key=lambda c: -c["h"])
        seen: list[str] = []
        for c in tall:
            if c["t"].lower() not in (x.lower() for x in seen):
                seen.append(c["t"])
            if len(seen) >= 8:
                break
        from app.engine.strip_text import is_supplement

        for item in payload.items:
            item.raw_text = text[:4000]
            item.big_text = seen
            item.brand = None  # a guessed line is not a brand; the matcher picks from big_text
            item.composition_text = None  # salts come from the closed-set extractor in the pipeline
            if item.mrp is not None and not _MRP_PAISE_RE.search(text):
                item.mrp = None  # "M.R.P.13.75" misread as "MRP75": a wrong price is worse than none
            if is_supplement(text):
                item.category = "supplement"
        payload.notes = "rapidocr"
        return payload


def group_rows(cells: list[dict]) -> list[str]:
    """OCR boxes -> printed rows, top to bottom, left to right."""
    rows: list[list[dict]] = []
    for c in sorted(cells, key=lambda c: c["y"]):
        row = rows[-1] if rows else None
        if row is not None:
            ry = sum(x["y"] for x in row) / len(row)
            rh = min(min(x["h"] for x in row), c["h"])
            if abs(c["y"] - ry) <= 0.5 * rh:
                row.append(c)
                continue
        rows.append([c])
    return [" ".join(x["t"] for x in sorted(r, key=lambda x: x["x"])) for r in rows]
