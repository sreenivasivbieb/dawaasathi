"""Request orchestration: one pipeline for REST, WebSocket, phone mode and WhatsApp.

 1. ASR (Prisma) and OCR (strip / rx) run in parallel
 2. entities + intent from the transcript (Hindi numbers parsed)
 3. matcher resolves the brand; with an image, candidates are first limited to OCR'd drugs
 4. low confidence / close second / look-alike name -> spoken confirmation question
 5. banned check, rule engine, price gap, cheaper alternatives, symptom / pouch / batch checks
 6. template script -> guard -> speakable text -> TTS job (cached segments + Timbre stream)
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.adapters.gnani_stt import STTError
from app.adapters.ocr import OCRItem, OCRReader
from app.config import Settings
from app.db import Store
from app.dialogue import guard
from app.dialogue.templates import RenderContext, brand_display, render
from app.dialogue.verbalize import speakable
from app.engine import alternatives as altmod
from app.engine import checks, price as pricemod, rules, safety as safetymod
from app.engine.salts import (canonical_form, canonical_release, canonical_salt, parse_composition,
                              salt_key, strength_key)
from app.match.index import MatchIndex, MatchResult, Scored
from app.metrics import METRICS, log_event
from app.models import (Candidate, Card, DrugItem, Flag, QueryResponse, SafetyResult, SaltDose, Script,
                        Verdict)
from app.nlu.entities import Entities, extract
from app.api.sessions import Session, SessionStore
from app.tts.cache import AudioService

WARNING_FLAGS = {"DUPLICATE_SALT", "SYMPTOM_MISMATCH", "LOOK_ALIKE", "NSQ_BATCH", "POSSIBLE_BANNED"}
RED_CODES = {"BANNED_COMBINATION", "SALT_MISMATCH", "STRENGTH_MISMATCH", "RELEASE_MISMATCH",
             "FORM_MISMATCH", "CONCENTRATION_MISMATCH"}


@dataclass
class QueryInput:
    session_id: str | None = None
    lang: str = "hi"
    audio: bytes | None = None
    audio_name: str = "audio.wav"
    text: str | None = None
    strip: bytes | None = None
    rx: bytes | None = None
    pouch: list[str] = field(default_factory=list)
    channel: str = "web"  # web | phone | whatsapp | stream
    tts_sample_rate: int | None = None


@dataclass
class Turn:
    lang: str
    transcript: str | None
    ents: dict[str, Any]
    strip_item: DrugItem | None
    rx_items: list[DrugItem]
    voice_item: DrugItem | None
    chosen_rx: int | None
    pouch: list[str]
    timings: dict[str, float | None]
    channel: str
    tts_sample_rate: int | None
    match_info: dict[str, Any] | None = None
    followup: str | None = None
    ocr_meta: dict[str, Any] = field(default_factory=dict)


def _ents_summary(e: Entities | None) -> dict[str, Any]:
    if e is None:
        return {"intent": "CLARIFY"}
    return {"intent": e.intent, "strength": e.strength, "strengths": e.strengths, "unit": e.unit,
            "form": e.form, "release": e.release, "symptom": e.symptom}


class Pipeline:
    def __init__(self, settings: Settings, store: Store, index: MatchIndex, stt, ocr: OCRReader,
                 audio: AudioService, sessions: SessionStore):
        self.s = settings
        self.store = store
        self.index = index
        self.stt = stt
        self.ocr = ocr
        self.audio = audio
        self.sessions = sessions
        self.keyterms = index.keyterms(settings.stt_keyterms_n)

    # ================================================================== entry points
    async def query(self, inp: QueryInput) -> QueryResponse:
        t_start = time.perf_counter()
        session = self.sessions.get_or_create(inp.session_id, inp.lang)
        session.lang = inp.lang
        timings: dict[str, float | None] = {"asr": None, "ocr": None, "match": None, "engine": None,
                                            "script": None, "tts_first_chunk": None}

        ocr_meta: dict[str, dict] = {}

        async def run_asr() -> str | None:
            if inp.text:
                return inp.text.strip()
            if not inp.audio:
                return None
            t0 = time.perf_counter()
            res = await self.stt.transcribe(inp.audio, inp.audio_name, inp.lang, self.keyterms,
                                            session.last_transcript)
            timings["asr"] = round((time.perf_counter() - t0) * 1000, 1)
            METRICS.observe("asr", timings["asr"])
            return res.text

        async def run_ocr(data: bytes | None, kind: str):
            if not data:
                return None
            t0 = time.perf_counter()
            res = await (self.ocr.read_strip(data) if kind == "strip" else self.ocr.read_rx(data))
            ms = round((time.perf_counter() - t0) * 1000, 1)
            timings["ocr"] = max(timings["ocr"] or 0, ms)
            METRICS.observe(f"ocr_{kind}", ms)
            METRICS.count(f"ocr_provider_{res.provider}")
            ocr_meta[kind] = {"provider": res.provider, "readable": res.payload.readable,
                              "items": len(res.payload.items), "fallback_reason": res.error}
            log_event("ocr", session=session.id, kind=kind, provider=res.provider, ms=ms,
                      items=len(res.payload.items), error=res.error)
            return res

        asr_r, strip_r, rx_r = await asyncio.gather(
            run_asr(), run_ocr(inp.strip, "strip"), run_ocr(inp.rx, "rx"), return_exceptions=True)

        transcript: str | None = None
        if isinstance(asr_r, BaseException):
            METRICS.count("asr_error")
            log_event("asr_error", session=session.id, error=getattr(asr_r, "code", type(asr_r).__name__))
            if not inp.strip and not inp.rx:
                return self._finish_simple(session, inp, "error", timings, t_start, error_kind="stt",
                                           error_code=getattr(asr_r, "code", "STT_ERROR"))
        else:
            transcript = asr_r
        for r in (strip_r, rx_r):
            if isinstance(r, BaseException):
                METRICS.count("ocr_error")
        strip_res = None if isinstance(strip_r, BaseException) else strip_r
        rx_res = None if isinstance(rx_r, BaseException) else rx_r

        ents = extract(transcript) if transcript else None
        if transcript:
            session.last_transcript = transcript
        session.turns += 1

        # ---- answer to a pending confirmation question?
        if session.pending and (inp.strip or inp.rx):
            session.pending = None
        if session.pending and ents and not inp.strip and not inp.rx:
            pick = self._confirmation_pick(ents, session.pending)
            if pick == "no":
                session.pending = None
                return self._finish_simple(session, inp, "needs_image", timings, t_start,
                                           transcript=transcript)
            if isinstance(pick, int):
                return await self._resume(session, pick, timings, t_start, transcript)
            session.pending = None  # a fresh question; fall through

        # ---- symptom-only follow-up about the last medicine ("बुखार के लिए")
        if (ents and ents.symptom and not ents.name_tokens and not inp.strip and not inp.rx
                and session.last_item is not None and session.last_context):
            ctx = dict(session.last_context)
            turn = Turn(lang=inp.lang, transcript=transcript, ents={**ctx["ents"], "symptom": ents.symptom},
                        strip_item=ctx.get("strip_item"), rx_items=ctx.get("rx_items", []),
                        voice_item=ctx.get("voice_item"), chosen_rx=ctx.get("chosen_rx"),
                        pouch=inp.pouch, timings=timings, channel=inp.channel,
                        tts_sample_rate=inp.tts_sample_rate, followup="symptom")
            return await self._answer(session, turn, t_start)

        # ---- OCR -> items (closed-set against the brand DB)
        t_match = time.perf_counter()
        strip_item = None
        if strip_res and strip_res.payload.items:
            strip_item = self.resolve_ocr(strip_res.payload.items[0], "strip", strip_res.provider)
        rx_items = [self.resolve_ocr(i, "rx", rx_res.provider) for i in (rx_res.payload.items if rx_res else [])]

        turn = Turn(lang=inp.lang, transcript=transcript, ents=_ents_summary(ents), strip_item=strip_item,
                    rx_items=rx_items, voice_item=None, chosen_rx=None, pouch=inp.pouch, timings=timings,
                    channel=inp.channel, tts_sample_rate=inp.tts_sample_rate, ocr_meta=ocr_meta)

        # ---- voice -> brand
        if ents and ents.name_tokens:
            outcome = self._match_voice(ents, turn, session)
            timings["match"] = round((time.perf_counter() - t_match) * 1000, 1)
            METRICS.observe("match", timings["match"])
            if outcome is not None:  # a confirmation question
                return self._ask_confirmation(session, turn, outcome, t_start)
        else:
            timings["match"] = round((time.perf_counter() - t_match) * 1000, 1)

        if (inp.strip or inp.rx) and not strip_item and not rx_items and turn.voice_item is None:
            # Images were sent but nothing readable came back.
            return self._finish_simple(session, inp, "needs_image", timings, t_start, transcript=transcript)
        return await self._answer(session, turn, t_start)

    async def confirm(self, session_id: str, candidate_id: str | None, none: bool, lang: str | None = None
                      ) -> QueryResponse:
        t_start = time.perf_counter()
        session = self.sessions.get(session_id)
        timings: dict[str, float | None] = {"asr": None, "ocr": None, "match": None, "engine": None,
                                            "script": None, "tts_first_chunk": None}
        if session is None or not session.pending:
            session = session or self.sessions.get_or_create(session_id, lang or "hi")
            return self._finish_simple(session, QueryInput(session_id=session.id, lang=lang or session.lang),
                                       "needs_image", timings, t_start)
        if lang:
            session.lang = lang
        if none:
            session.pending = None
            return self._finish_simple(session, QueryInput(session_id=session.id, lang=session.lang),
                                       "needs_image", timings, t_start)
        ids = [c["candidate_id"] for c in session.pending["candidates"]]
        if candidate_id not in ids:
            return self._finish_simple(session, QueryInput(session_id=session.id, lang=session.lang),
                                       "error", timings, t_start, error_code="UNKNOWN_CANDIDATE")
        return await self._resume(session, ids.index(candidate_id), timings, t_start, None)

    # ================================================================== matching
    def _match_voice(self, ents: Entities, turn: Turn, session: Session) -> dict | None:
        image_items = [i for i in [turn.strip_item, *turn.rx_items] if i is not None]
        kwargs = dict(strength=ents.strength, strengths=ents.strengths, form=ents.form, release=ents.release,
                      t1=self.s.match_t1, t2=self.s.match_t2)
        if image_items:
            ids = {i.brand_id for i in image_items if i.brand_id is not None}
            salts = {s.name for i in image_items for s in i.salts}
            res = self.index.search(ents.name_tokens, restrict_ids=ids, restrict_salts=salts, **kwargs)
            if res.top and res.top.score >= self.s.match_t1 - 0.08:
                # The user named one of the pictured drugs.
                turn.match_info = self._match_info(res.top, "voice+image")
                if turn.rx_items:
                    turn.chosen_rx = self._rx_index_for(res.top, turn.rx_items)
                return None
            unres = self.index.search(ents.name_tokens, **kwargs)
            if unres.confident and turn.strip_item is not None:
                # Spoken drug is different from the strip: spoken = what the doctor prescribed.
                turn.voice_item = self._scored_to_item(unres.top, ents)
                turn.match_info = self._match_info(unres.top, "voice")
            return None

        res = self.index.search(ents.name_tokens, **kwargs)
        if res.top:
            METRICS.confidence(res.top.score)
        if not res.candidates:
            return None
        top = res.top
        lasa = self._lasa_partner(top)
        needs = (not res.confident) or (lasa is not None and top.brand_id not in session.confirmed_brand_ids)
        if top.brand_id is not None and top.strength_ok is None and not ents.strengths:
            # Brand heard but no strength and several SKUs: ask which one.
            siblings = [c for c in res.candidates if c.brand_id is not None and
                        self.store.brands[c.brand_id].family == self.store.brands[top.brand_id].family]
            if len(siblings) > 1:
                needs = True
        if not needs:
            turn.voice_item = self._scored_to_item(top, ents)
            turn.match_info = self._match_info(top, "voice")
            return None
        cands = list(res.candidates)
        if lasa is not None and all(c.key != lasa.key for c in cands):
            cands = [cands[0], lasa] + cands[1:]
        return {"kind": "voice", "candidates": cands[:3], "reason": res.reason if not lasa else "look_alike"}

    def _lasa_partner(self, top: Scored) -> Scored | None:
        keys: list[str] = []
        if top.brand_id is not None:
            b = self.store.brands[top.brand_id]
            keys = [b.family] + [s.name for s in b.salts]
        elif top.salt:
            keys = [top.salt]
        for key in keys:
            for name in sorted(self.store.lasa.get(key, set())):
                res = self.index.search(name.split(), t1=0.0, t2=0.0, k=1)
                if res.top and res.top.key != top.key:
                    return res.top
        return None

    def _scored_to_item(self, sc: Scored, ents: Entities | dict) -> DrugItem:
        e = ents if isinstance(ents, dict) else _ents_summary(ents)
        if sc.brand_id is not None:
            return self.store.brand_item(sc.brand_id, "voice")
        amount = e.get("strength")
        unit = e.get("unit") or ("mg" if amount is not None else None)
        form = e.get("form")
        salts = [SaltDose(name=sc.salt, amount=amount, unit=unit)]
        item = DrugItem(source="voice", brand=None, salts=salts, form=form, release=e.get("release"))
        if item.form is None:
            item.form = self._common_form(item)
        if item.release is None:
            item.release = self.infer_release(item)
        return item

    def _common_form(self, item: DrugItem) -> str | None:
        sk = salt_key(item.salts)
        forms: dict[str, int] = {}
        for bid in self.store.by_salt.get(sk, []):
            b = self.store.brands[bid]
            if strength_key(item.salts, b.form) == b.strength_key and b.form:
                forms[b.form] = forms.get(b.form, 0) + 1
        return max(forms, key=forms.get) if forms else None

    def _match_info(self, sc: Scored, source: str) -> dict:
        return {"brand_id": sc.brand_id, "brand": self.store.brands[sc.brand_id].name if sc.brand_id else None,
                "salt": sc.salt, "confidence": round(sc.score, 3), "heard": sc.span, "source": source}

    def _rx_index_for(self, sc: Scored, rx_items: list[DrugItem]) -> int | None:
        for i, it in enumerate(rx_items):
            if sc.brand_id is not None and it.brand_id == sc.brand_id:
                return i
            if sc.salt and any(s.name == sc.salt for s in it.salts):
                return i
        return None

    def _candidate(self, sc: Scored, idx: int) -> Candidate:
        if sc.brand_id is not None:
            b = self.store.brands[sc.brand_id]
            return Candidate(candidate_id=f"c{idx}_b{b.id}", brand_id=b.id, brand=b.name, label=brand_display(b.name),
                             confidence=round(sc.score, 3))
        return Candidate(candidate_id=f"c{idx}_s", brand=sc.salt.title(), label=sc.salt.title(),
                         confidence=round(sc.score, 3), source="salt")

    def _confirmation_pick(self, ents: Entities, pending: dict) -> int | str | None:
        n = len(pending["candidates"])
        if ents.ordinal and 1 <= ents.ordinal <= n and not ents.name_tokens:
            return ents.ordinal - 1
        if ents.no and not ents.yes and not ents.name_tokens:
            return "no"
        if ents.yes and not ents.name_tokens:
            return 0
        if ents.name_tokens:  # the user repeated a name: pick the candidate it matches best
            labels = [c["label"] for c in pending["candidates"]]
            from rapidfuzz import fuzz
            from app.match.translit import normalize

            spoken = normalize(" ".join(ents.name_tokens))
            scores = [fuzz.WRatio(spoken, normalize(lab)) for lab in labels]
            best = max(range(n), key=lambda i: scores[i])
            if scores[best] >= 85:
                return best
        return None

    # ================================================================== confirmation
    def _ask_confirmation(self, session: Session, turn: Turn, outcome: dict, t_start: float) -> QueryResponse:
        cands = [self._candidate(sc, i) for i, sc in enumerate(outcome["candidates"])]
        session.pending = {
            "kind": outcome["kind"], "candidates": [c.model_dump() for c in cands],
            "scored": outcome["candidates"], "turn": turn,
        }
        METRICS.count("needs_confirmation")
        ctx = RenderContext(lang=turn.lang, status="needs_confirmation", candidates=cands)
        return self._respond(session, turn, ctx, status="needs_confirmation", t_start=t_start,
                             candidates=cands, guard_sources=[c.model_dump() for c in cands])

    async def _resume(self, session: Session, pick: int, timings: dict, t_start: float,
                      transcript: str | None) -> QueryResponse:
        pending = session.pending
        session.pending = None
        turn: Turn = pending["turn"]
        turn.timings = timings
        if transcript:
            turn.transcript = transcript
        if pending["kind"] == "rx":
            turn.chosen_rx = pick
        else:
            sc: Scored = pending["scored"][pick]
            turn.voice_item = self._scored_to_item(sc, turn.ents)
            turn.match_info = self._match_info(sc, "voice_confirmed")
            if sc.brand_id is not None:
                session.confirmed_brand_ids.add(sc.brand_id)
        return await self._answer(session, turn, t_start)

    # ================================================================== OCR resolution
    def _fix_salt_name(self, name: str) -> str:
        """OCR noise ('ceclolenac 1p') -> closest known salt when it is clearly the same word."""
        from rapidfuzz import fuzz, process

        from app.engine.salts import all_salt_surface_forms

        name = re.sub(r"\s+[0-9]?[il1|]p\b.*$", "", name.strip(), flags=re.I)
        canon = canonical_salt(name)
        forms = all_salt_surface_forms()
        if canon in forms.values() or not canon:
            return canon
        hit = process.extractOne(canon, list(forms.keys()), scorer=fuzz.ratio)
        if hit and hit[1] >= 85:
            return forms[hit[0]]
        return canon

    def resolve_ocr(self, o: OCRItem, source: str, provider: str = "vlm") -> DrugItem:
        salts = [SaltDose(name=self._fix_salt_name(s.name), amount=s.amount,
                          unit=(s.unit or "").lower() or None, per_ml=s.per_ml)
                 for s in o.salts if s.name]
        if not salts and o.composition_text:
            salts = parse_composition(o.composition_text)
            for s in salts:
                s.name = self._fix_salt_name(s.name)
        if not salts and o.brand and re.search(r"\d\s*(mg|mcg|g|iu)\b", o.brand, re.I):
            # Prescriptions often write the generic: "Syp. Paracetamol 120 mg / 5 ml"
            from app.engine.salts import known_salts

            parsed = parse_composition(re.sub(r"^(tab|cap|syp|syr|inj)\.?\s+", "", o.brand, flags=re.I))
            if parsed and all(p.name in known_salts() for p in parsed):
                salts = parsed
        form = canonical_form(o.form) or canonical_form(o.brand)
        release = canonical_release(o.release) or (canonical_release(o.brand) if o.brand else None)
        item = DrugItem(source=source, brand=o.brand, salts=salts, form=form, release=release, mrp=o.mrp,
                        pack_qty=o.pack_qty, pack_unit=o.pack_unit, batch_no=o.batch_no,
                        directions=o.directions, raw_text=o.composition_text)
        if form is None and any(s.per_ml for s in item.salts):
            item.form = form = "oral_liquid"
        if form in ("oral_liquid", "drops") and any(s.per_ml is None for s in item.salts):
            # VLMs often drop "/5 ml"; recover it from the printed text if present.
            texts = [t for t in (o.composition_text, o.brand) if t]
            for text in texts:
                parsed = {p.name: p for p in parse_composition(text)}
                for s in item.salts:
                    p = parsed.get(s.name)
                    if s.per_ml is None and p is not None and p.per_ml and (
                            s.amount is None or abs((p.amount or 0) - s.amount) < 1e-6):
                        s.amount, s.unit, s.per_ml = p.amount, p.unit, p.per_ml
        brand_lines = [o.brand] if o.brand else []
        if o.raw_text and source == "strip":  # fallback OCR: any of the first lines may hold the brand
            brand_lines += [ln for ln in o.raw_text.splitlines() if re.search(r"[A-Za-z]{3,}", ln)][:8]
        best = None
        for line in brand_lines:
            tokens = re.findall(r"[^\s]+", line)
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", line)]
            res = self.index.search(tokens, strengths=nums or None, form=form, release=release,
                                    t1=self.s.match_t1, t2=self.s.match_t2)
            if res.top and res.top.brand_id is not None and (best is None or res.top.score > best[1].top.score):
                best = (line, res)
            if line is o.brand and res.confident:
                break
        if best is not None:
            res = best[1]
            top = res.top
            from_printed_brand = best[0] is o.brand
            # A fuzzy hit on raw fallback-OCR text must be fully confident before we trust it.
            ok = res.confident or (from_printed_brand and top.score >= self.s.match_t1 - 0.05)
            if top and top.brand_id is not None and ok:
                b = self.store.brands[top.brand_id]
                ocr_names = {s.name for s in item.salts}
                consistent = not ocr_names or ocr_names == {s.name for s in b.salts}
                if consistent:
                    item.brand_id = b.id
                    item.brand = b.name
                    item.scheduled = b.scheduled
                    item.manufacturer = item.manufacturer or b.manufacturer
                    if not item.salts:
                        item.salts = [s.model_copy() for s in b.salts]
                    else:
                        db = {s.name: s for s in b.salts}
                        for s in item.salts:
                            if s.amount is None and s.name in db:
                                s.amount, s.unit, s.per_ml = db[s.name].amount, db[s.name].unit, db[s.name].per_ml
                    item.form = item.form or b.form
                    item.release = item.release or b.release
                    if item.pack_qty is None:
                        item.pack_qty, item.pack_unit = b.pack_qty, b.pack_unit
                    if source == "rx":
                        item.mrp = None
                    item.confidence = round(top.score, 3)
        if provider.startswith("tesseract") and item.brand_id is None:
            # Fallback OCR that did not resolve to a known product: never trust the salt list as
            # complete (a missed ingredient could hide a banned combination), drop garbled names.
            item.composition_uncertain = True
            item.brand = None
        if item.release is None:
            item.release = self.infer_release(item)
        return item

    def infer_release(self, item: DrugItem) -> str | None:
        """No SR/ER/DR marker: use the DB consensus for this salt/strength/form, else IR."""
        if not item.salts or item.form is None:
            return None
        sk = salt_key(item.salts)
        stk = strength_key(item.salts, item.form)
        releases = {self.store.brands[b].release or "IR" for b in self.store.by_salt.get(sk, [])
                    if self.store.brands[b].form == item.form and
                    (stk is None or self.store.brands[b].strength_key == stk)}
        if len(releases) == 1:
            return releases.pop()
        return "IR"

    # ================================================================== decision + engine
    def _pick_rx_for_strip(self, strip: DrugItem, rx_items: list[DrugItem]) -> int:
        from rapidfuzz import fuzz

        def score(it: DrugItem) -> float:
            a, b = set(it.salt_names()), set(strip.salt_names())
            jac = len(a & b) / len(a | b) if a and b else 0.0
            same = 1.0 if it.brand_id is not None and it.brand_id == strip.brand_id else 0.0
            name = fuzz.WRatio((it.brand or "").lower(), (strip.brand or "").lower()) / 100
            return 2 * jac + same + 0.5 * name

        return max(range(len(rx_items)), key=lambda i: score(rx_items[i]))

    async def _answer(self, session: Session, turn: Turn, t_start: float) -> QueryResponse:
        t0 = time.perf_counter()
        intent = turn.ents.get("intent") or "CLARIFY"
        prescribed = dispensed = focus = None
        if turn.strip_item and turn.rx_items:
            idx = turn.chosen_rx if turn.chosen_rx is not None else self._pick_rx_for_strip(
                turn.strip_item, turn.rx_items)
            prescribed, dispensed = turn.rx_items[idx], turn.strip_item
        elif turn.strip_item and turn.voice_item:
            prescribed, dispensed = turn.voice_item, turn.strip_item
        elif turn.strip_item:
            focus = turn.strip_item
        elif turn.rx_items:
            if turn.chosen_rx is not None:
                focus = turn.rx_items[turn.chosen_rx]
            elif len(turn.rx_items) == 1:
                focus = turn.rx_items[0]
            else:
                cands = [Candidate(candidate_id=f"rx{i}", brand_id=it.brand_id, brand=it.brand or "?",
                                   label=it.brand or " + ".join(it.salt_labels()), confidence=1.0, source="rx")
                         for i, it in enumerate(turn.rx_items)]
                session.pending = {"kind": "rx", "candidates": [c.model_dump() for c in cands], "turn": turn}
                ctx = RenderContext(lang=turn.lang, status="needs_confirmation", candidates=[],
                                    rx_count=len(cands))
                return self._respond(session, turn, ctx, status="needs_confirmation", t_start=t_start,
                                     candidates=cands,
                                     guard_sources=[c.model_dump() for c in cands] + [{"rx_count": len(cands)}])
        elif turn.voice_item:
            if intent == "VERIFY_MATCH":
                session.last_item = turn.voice_item
                session.last_context = self._context_dict(turn)
                ctx = RenderContext(lang=turn.lang, status="needs_image", needs_image_kind="verify")
                return self._respond(session, turn, ctx, status="needs_image", t_start=t_start,
                                     focus=turn.voice_item)
            focus = turn.voice_item
        else:
            ctx = RenderContext(lang=turn.lang, status="needs_image")
            return self._respond(session, turn, ctx, status="needs_image", t_start=t_start)

        target = dispensed or focus
        assert target is not None
        safety = safetymod.check_banned(self.store, target, self.s.stale_days)
        alts = [] if safety.status == "banned" else altmod.cheaper(self.store, target, 3, self.s.stale_days)
        price = pricemod.price_gap(self.store, target, self.s.nppa_gst_pct, self.s.stale_days)
        if prescribed is not None:
            verdict = rules.compare(prescribed, dispensed, safety)
        else:
            verdict = rules.lookup(target, safety, bool(alts))

        flags: list[Flag] = list(verdict.flags)
        if safety.status == "possible":
            flags.append(Flag(code="POSSIBLE_BANNED", facts={"notification_ref": safety.notification_ref}))
        for f in (
            checks.dup_salt(target, turn.pouch),
            checks.symptom_check(self.store, target, turn.ents.get("symptom")),
            checks.look_alike(self.store, target) if target.source == "voice" else None,
        ):
            if f is not None:
                flags.append(f)
        nsq = safetymod.check_batch(self.store, turn.strip_item) if turn.strip_item else None
        if nsq:
            flags.append(Flag(code="NSQ_BATCH", facts={"batch_no": nsq.get("batch_no"),
                                                       "alert_month": nsq.get("alert_month"),
                                                       "source_url": nsq.get("source_url")}))
        verdict.flags = [f for f in verdict.flags]  # secondary differences stay on the verdict
        directions = None
        rx_src = prescribed if prescribed is not None and prescribed.source == "rx" else (
            focus if focus is not None and focus.source == "rx" else None)
        if rx_src is not None:
            directions = rx_src.directions
        turn.timings["engine"] = round((time.perf_counter() - t0) * 1000, 1)
        METRICS.observe("engine", turn.timings["engine"])

        session.last_item = target
        session.last_context = self._context_dict(turn)

        status = "unverified" if verdict.code == "UNVERIFIED" else "ok"
        spoken_alts = [] if (verdict.mode == "compare" and verdict.code == "UNVERIFIED") else alts
        ctx = RenderContext(
            lang=turn.lang, status=status, intent=intent, verdict=verdict, item=target, prescribed=prescribed,
            dispensed=dispensed, safety=safety, price=price, alternatives=spoken_alts,
            flags=[f for f in flags if f.code not in {fl.code for fl in verdict.flags}] + verdict.flags,
            directions=directions, skip_detail=turn.followup == "symptom",
        )
        return self._respond(session, turn, ctx, status=status, t_start=t_start, verdict=verdict,
                             safety=safety, price=price, alternatives=alts, flags=flags, focus=focus,
                             prescribed=prescribed, dispensed=dispensed, target=target)

    def _context_dict(self, turn: Turn) -> dict:
        return {"ents": dict(turn.ents), "strip_item": turn.strip_item, "rx_items": turn.rx_items,
                "voice_item": turn.voice_item, "chosen_rx": turn.chosen_rx}

    # ================================================================== response
    def _finish_simple(self, session: Session, inp: QueryInput, status: str, timings: dict, t_start: float,
                       transcript: str | None = None, error_kind: str | None = None,
                       error_code: str | None = None) -> QueryResponse:
        turn = Turn(lang=inp.lang, transcript=transcript, ents={"intent": "CLARIFY"}, strip_item=None,
                    rx_items=[], voice_item=None, chosen_rx=None, pouch=inp.pouch, timings=timings,
                    channel=inp.channel, tts_sample_rate=inp.tts_sample_rate)
        ctx = RenderContext(lang=inp.lang, status=status, error_kind=error_kind)
        resp = self._respond(session, turn, ctx, status=status, t_start=t_start)
        if error_code:
            resp.data_sources["error"] = {"code": error_code}
        return resp

    def _respond(self, session: Session, turn: Turn, ctx: RenderContext, status: str, t_start: float,
                 verdict: Verdict | None = None, safety: SafetyResult | None = None, price=None,
                 alternatives=None, flags=None, candidates=None, focus=None, prescribed=None, dispensed=None,
                 target=None, guard_sources=None) -> QueryResponse:
        t0 = time.perf_counter()
        rendered = render(ctx)
        sources = [verdict.model_dump() if verdict else None, safety.model_dump() if safety else None,
                   price.model_dump() if price else None, [a.model_dump() for a in alternatives or []],
                   [f.model_dump() for f in flags or []], guard_sources or [],
                   {"directions": ctx.directions, "list_date": _date_parts(safety)},
                   target.model_dump() if target else None, prescribed.model_dump() if prescribed else None]
        g = guard.validate(rendered.text, *sources)
        segments = rendered.segments
        text = rendered.text
        if not g.ok:
            METRICS.count("guard_fallback")
            log_event("guard_fallback", session=session.id, bad_numbers=g.bad_numbers, bad_salts=g.bad_salts)
            segments = rendered.plain_segments
            text = " ".join(s.text for s in segments)
        chemist = rendered.chemist_text
        if chemist and not guard.validate(chemist, *sources).ok:
            chemist = ""
        speech_segments = [speakable(s.text, ctx.lang) for s in segments]
        session.last_chemist_text = chemist or None
        session.lang = ctx.lang
        turn.timings["script"] = round((time.perf_counter() - t0) * 1000, 1)

        sr = turn.tts_sample_rate or (8000 if turn.channel == "phone" else None)
        job = self.audio.create(ctx.lang, speech_segments, sr)
        session.current_audio_id = job.id

        code = verdict.code if verdict else None
        card = _card(status, code, flags or [])
        match = None
        if target is not None or focus is not None:
            it = target or focus
            match = {"brand_id": it.brand_id, "brand": it.brand,
                     "confidence": (turn.match_info or {}).get("confidence", it.confidence),
                     "heard": (turn.match_info or {}).get("heard"), "source": it.source}
        resp = QueryResponse(
            session_id=session.id, status=status, transcript=turn.transcript, lang=ctx.lang,
            intent=turn.ents.get("intent"), mode=verdict.mode if verdict else None, match=match,
            candidates=candidates or [],
            safety=safety.model_dump() if safety else None,
            verdict={"code": verdict.code, "mode": verdict.mode, "facts": verdict.facts} if verdict else None,
            price=price.model_dump() if price else None, alternatives=alternatives or [], flags=flags or [],
            items={k: v.model_dump() for k, v in (("prescribed", prescribed), ("dispensed", dispensed),
                                                   ("focus", focus)) if v is not None},
            script=Script(lang=ctx.lang, text=text, chemist_text=chemist, guard_ok=g.ok,
                          speech_text=" ".join(speech_segments)),
            audio_id=job.id, audio_url=f"/v1/audio/{job.id}.ogg", audio_stream_url=f"/v1/audio/{job.id}.pcm",
            tts="timbre" if self.audio.live else "mock", card=card,
            timings_ms=dict(turn.timings), data_sources=self._data_sources(),
        )
        if turn.ocr_meta:
            resp.data_sources["ocr"] = turn.ocr_meta
        total = round((time.perf_counter() - t_start) * 1000, 1)
        resp.timings_ms["total"] = total
        METRICS.observe("total", total)
        METRICS.count(f"status_{status}")
        if code:
            METRICS.count(f"verdict_{code}")
        log_event("query", session=session.id, status=status, verdict=code, intent=resp.intent,
                  brand_id=match.get("brand_id") if match else None,
                  confidence=match.get("confidence") if match else None, timings=resp.timings_ms,
                  channel=turn.channel)
        return resp

    def _data_sources(self) -> dict:
        out = {}
        for name in ("brands", "banned_fdc", "price_ref", "nsq_alerts"):
            meta = self.store.dataset(name)
            if meta:
                out[name] = {"as_of": meta.get("as_of"), "source": meta.get("source"), "rows": meta.get("rows")}
        return out


def _date_parts(safety: SafetyResult | None) -> str | None:
    if safety is None:
        return None
    return safety.notification_date or safety.list_as_of


def _card(status: str, code: str | None, flags: list[Flag]) -> Card:
    if status in ("needs_confirmation", "needs_image", "error"):
        return Card(color="amber", icon="question", title=status)
    if code in RED_CODES:
        return Card(color="red", icon="cross", title=code)
    if code == "UNVERIFIED":
        return Card(color="amber", icon="question", title=code)
    if any(f.code in WARNING_FLAGS for f in flags):
        return Card(color="amber", icon="question", title=code or "")
    return Card(color="green", icon="check", title=code or "")
