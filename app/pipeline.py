"""Request orchestration: one pipeline for REST, WebSocket and phone mode.

 1. ASR (Prisma) and OCR (strip / rx) run in parallel
 2. entities + intent from the transcript (Hindi numbers parsed)
 3. matcher resolves the brand; with an image, candidates are first limited to OCR'd drugs
 4. low confidence / close second / look-alike name -> spoken confirmation question
 5. banned check, rule engine, price gap, cheaper alternatives, symptom / pouch / batch checks
 6. template script -> guard -> speakable text -> TTS job (cached segments + Timbre stream)
 7. the turn (question + reply) is saved to the chat thread in the local DB

A spoken / typed name the drug DB cannot match well is looked up on the web (app/engine/web_brands.py);
a verified composition is saved to the local DB and the name is matched again.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.adapters.gnani_stt import STTError
from app.adapters.llm import GroqLLM, LLMError, nlu_to_text
from app.adapters.ocr import OCRItem, OCRReader
from app.config import Settings
from app.db import Store
from app.dialogue import guard
from app.dialogue.templates import ANSWER_FLAG_PREFIXES, RenderContext, Segment, brand_display, render
from app.dialogue.templates import load as load_templates
from app.dialogue.verbalize import speakable
from app.engine import alternatives as altmod
from app.engine import checks, price as pricemod, rules, safety as safetymod
from app.engine.strip_text import SaltVocab, extract_salts
from app.guardrails import check_query, clean_input, safe_label, unsafe_reply_text
from app.engine.web_brands import WebBrandLookup, query_name
from app.engine.web_uses import WebUseLookup
from app.db import LEARNED_ID_BASE
from app.localdb import LocalDB
from app.engine.salts import (canonical_form, canonical_release, canonical_salt, parse_composition,
                              salt_key, strength_key)
from app.match.index import MatchIndex, MatchResult, Scored
from app.metrics import METRICS, log_event
from app.models import (Candidate, Card, DrugItem, Flag, QueryResponse, SafetyResult, SaltDose, Script,
                        Verdict)
from app.nlu.entities import Entities, extract, tokenize
from app.nlu.numbers import parse_numbers
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
    channel: str = "web"  # web | phone | stream
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
    llm_meta: dict[str, Any] = field(default_factory=dict)


# If the template names one of these, the reworded reply must keep it (a dropped "at Jan Aushadhi"
# turns the generic's price into the user's own medicine's price; the number guard cannot see that).
ANCHORS = (("जन औषधि", "jan aushadhi", "जन औषधी"), ("सरकारी", "nppa", "government"),
           ("प्रतिबंधित", "banned", "प्रतिबंधित यादी"), ("पक्का नहीं", "not sure", "खात्री", "पक्की जाँच"),
           ("बचत", "save", "saving"), ("गुणवत्ता", "quality"))

# Phrases a reworded reply may never contain (unless the template itself said it).
FORBIDDEN = ("बंद कर", "बंद करें", "मत लें", "मत लीजिए", "मत खाइए", "धोखा", "नकली", "फ्रॉड", "थांबव", "घेऊ नका",
             "stop taking", "do not take", "don't take", "fraud", "fake", "cheat", "scam")


def _ents_summary(e: Entities | None) -> dict[str, Any]:
    if e is None:
        return {"intent": "CLARIFY"}
    return {"intent": e.intent, "strength": e.strength, "strengths": e.strengths, "unit": e.unit,
            "form": e.form, "release": e.release, "symptom": e.symptom}


class Pipeline:
    def __init__(self, settings: Settings, store: Store, index: MatchIndex, stt, ocr: OCRReader,
                 audio: AudioService, sessions: SessionStore, llm: GroqLLM | None = None,
                 web_uses: WebUseLookup | None = None, web_brands: WebBrandLookup | None = None,
                 history: LocalDB | None = None):
        self.s = settings
        self.web_brands = web_brands
        self.history = history
        self.llm = llm
        self.web_uses = web_uses
        self.store = store
        self.index = index
        self.stt = stt
        self.ocr = ocr
        self.audio = audio
        self.sessions = sessions
        self.keyterms = index.keyterms(settings.stt_keyterms_n)
        self._vocab: SaltVocab | None = None

    @property
    def vocab(self) -> SaltVocab:
        """Every salt named by a product in the brand DB (closed set for OCR'd ingredients)."""
        if self._vocab is None:
            from app.engine.salts import all_salt_surface_forms

            names = {s.name for b in self.store.brands.values() for s in b.salts}
            self._vocab = SaltVocab(names, all_salt_surface_forms())
        return self._vocab

    # ================================================================== entry points
    async def query(self, inp: QueryInput) -> QueryResponse:
        resp = await self._query(inp)
        attachments = [k for k, v in (("audio", inp.audio), ("strip", inp.strip), ("rx", inp.rx)) if v]
        self._record(resp, inp.lang, inp.text or resp.transcript, attachments)
        return resp

    async def confirm(self, session_id: str, candidate_id: str | None, none: bool, lang: str | None = None
                      ) -> QueryResponse:
        resp = await self._confirm(session_id, candidate_id, none, lang)
        label = None
        if none:
            label = "—"
        elif candidate_id:
            label = next((c.label for c in resp.candidates if c.candidate_id == candidate_id), None) or \
                ((resp.match or {}).get("brand"))
        self._record(resp, resp.lang, label, [])
        return resp

    def _record(self, resp: QueryResponse, lang: str, user_text: str | None, attachments: list[str]) -> None:
        """Save the turn to the chat thread (never the audio or photos themselves)."""
        if self.history is None:
            return
        from app.guardrails import public_response

        try:
            reply = public_response(resp.model_dump(mode="json"), self.s, False)
            if resp.status == "blocked":  # guardrail hits: keep only that it happened, not the text
                user_text = None
            self.history.add_turn(resp.session_id, lang, user_text, attachments, reply)
        except Exception as exc:  # history must never break a reply
            log_event("history_error", session=resp.session_id, error=type(exc).__name__)

    async def _query(self, inp: QueryInput) -> QueryResponse:
        t_start = time.perf_counter()
        session = self.sessions.get_or_create(inp.session_id, inp.lang)
        session.lang = inp.lang
        timings: dict[str, float | None] = {"asr": None, "ocr": None, "match": None, "engine": None,
                                            "script": None, "tts_first_chunk": None}
        inp.text = clean_input(inp.text)
        if inp.text and (g := check_query(inp.text)).blocked:  # typed: stop before OCR / LLM run
            return self._finish_guard(session, inp, g.kind, timings, t_start, inp.text)

        ocr_meta: dict[str, dict] = {}
        asr_meta: dict[str, Any] = {}

        async def run_asr() -> str | None:
            if inp.text:
                return inp.text.strip()
            if not inp.audio:
                return None
            t0 = time.perf_counter()
            if not self.s.stt_dual:
                res = await self.stt.transcribe(inp.audio, inp.audio_name, inp.lang, self.keyterms,
                                                session.last_transcript)
                asr_meta["model"] = res.model or self.s.stt_model
            else:
                res, dual_meta = await self._dual_asr(inp, session.last_transcript)
                asr_meta.update(dual_meta)
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
            if res.detect is not None:
                ocr_meta[kind]["detect"] = res.detect
            if res.local_ms is not None:
                ocr_meta[kind]["local_ocr_ms"] = res.local_ms
            if res.payload.items:
                first = res.payload.items[0]
                ocr_meta[kind]["read"] = {"brand": first.brand, "salts": [s.name for s in first.salts],
                                          "category": first.category, "big_text": first.big_text[:6]}
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
                code = getattr(asr_r, "code", "STT_ERROR")
                # A busy / unreachable STT service is not the caller's unclear voice: say "try again later".
                service = getattr(asr_r, "retryable", False) or code in ("RATE_LIMITED", "NETWORK")
                return self._finish_simple(session, inp, "error", timings, t_start,
                                           error_kind="service" if service else "stt", error_code=code)
        else:
            transcript = clean_input(asr_r)
            if transcript and (g := check_query(transcript)).blocked:  # spoken: before the LLM sees it
                return self._finish_guard(session, inp, g.kind, timings, t_start, transcript)
        for r in (strip_r, rx_r):
            if isinstance(r, BaseException):
                METRICS.count("ocr_error")
        strip_res = None if isinstance(strip_r, BaseException) else strip_r
        rx_res = None if isinstance(rx_r, BaseException) else rx_r

        ents = extract(transcript) if transcript else None
        llm_meta: dict[str, Any] = {}
        if asr_meta:
            llm_meta["asr"] = asr_meta
        if transcript and not session.pending and self.llm is not None and self.s.llm_nlu:
            ents = await self._llm_understand(transcript, ents, inp.lang, timings, llm_meta)
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

        # ---- follow-up about the last medicine ("बुखार के लिए", "what is it used for?")
        reference_intents = {"USE_QUERY", "PRICE_CHECK", "FIND_GENERIC", "DOSAGE_QUERY", "VERIFY_MATCH"}
        if (ents and (ents.symptom or ents.intent in reference_intents)
                and not ents.name_tokens and not ents.strengths and not ents.form and not ents.release
                and not inp.strip and not inp.rx
                and session.last_item is not None and session.last_context):
            ctx = dict(session.last_context)
            follow_ents = {**ctx["ents"], "symptom": ents.symptom}
            if ents.intent in reference_intents:
                follow_ents["intent"] = ents.intent
            turn = Turn(lang=inp.lang, transcript=transcript, ents=follow_ents,
                        strip_item=ctx.get("strip_item"), rx_items=ctx.get("rx_items", []),
                        voice_item=ctx.get("voice_item"), chosen_rx=ctx.get("chosen_rx"),
                        pouch=inp.pouch, timings=timings, channel=inp.channel,
                        tts_sample_rate=inp.tts_sample_rate, followup="symptom" if ents.symptom else "reference")
            return await self._answer(session, turn, t_start)

        # ---- OCR -> items (closed-set against the brand DB)
        t_match = time.perf_counter()
        strip_item = None
        if strip_res and strip_res.payload.items:
            strip_item = self.resolve_ocr(strip_res.payload.items[0], "strip", strip_res.provider,
                                          ocr_meta.setdefault("strip", {}))
        rx_items = [self.resolve_ocr(i, "rx", rx_res.provider) for i in (rx_res.payload.items if rx_res else [])]

        turn = Turn(lang=inp.lang, transcript=transcript, ents=_ents_summary(ents), strip_item=strip_item,
                    rx_items=rx_items, voice_item=None, chosen_rx=None, pouch=inp.pouch, timings=timings,
                    channel=inp.channel, tts_sample_rate=inp.tts_sample_rate, ocr_meta=ocr_meta,
                    llm_meta=llm_meta)

        # ---- voice -> brand (the web lookup first, if the drug DB has no good match for the name)
        if ents and ents.name_tokens and not inp.strip and not inp.rx:
            await self._web_brand_fallback(ents, turn)
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

    async def _confirm(self, session_id: str, candidate_id: str | None, none: bool, lang: str | None = None
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

    async def _dual_asr(self, inp: QueryInput, previous: str | None):
        """Two Prisma models in parallel; keep the transcript whose drug reading the DB matches best
        (ties go to the primary model). Same latency, twice the STT credits."""
        models = [self.s.stt_model, self.s.stt_model_2]
        results = await asyncio.gather(*[
            self.stt.transcribe(inp.audio, inp.audio_name, inp.lang, self.keyterms, previous, model=m)
            for m in models], return_exceptions=True)
        ok = [(m, r) for m, r in zip(models, results) if not isinstance(r, BaseException)]
        if not ok:
            raise results[0]
        scored = [(self._top_score(extract(r.text)) if r.text else 0.0, -i, m, r) for i, (m, r) in enumerate(ok)]
        best = max(scored, key=lambda x: (x[0], x[1]))
        inp_meta = {"model": best[2], "dual": [{"model": m, "text": r.text, "score": sc, "ms": round(r.latency_ms)}
                                               for sc, _, m, r in scored]}
        METRICS.count(f"asr_dual_pick_{best[2]}")
        return best[3], inp_meta

    # ================================================================== LLM
    async def _llm_understand(self, transcript: str, ents: Entities, lang: str, timings: dict,
                              meta: dict) -> Entities:
        """Ask the LLM what was said; keep whichever reading (rules or LLM) the DB matches better."""
        try:
            res = await self.llm.understand(transcript, lang)
        except (LLMError, KeyError, ValueError) as exc:
            meta["nlu"] = {"used": False, "error": str(exc)[:200]}
            METRICS.count("llm_nlu_error")
            return ents
        timings["llm_nlu"] = res.latency_ms
        METRICS.observe("llm_nlu", res.latency_ms)
        nlu = res.data
        meta["nlu"] = {"used": False, "model": res.model, "ms": res.latency_ms, "output": nlu}
        chosen = ents
        text = nlu_to_text(nlu)
        if text:
            alt = extract(text)
            rule_top = self._top_score(ents)
            llm_top = self._top_score(alt)
            meta["nlu"].update(rule_score=rule_top, llm_score=llm_top, llm_text=text)
            # Equal name scores (salt names score the same with or without a number): prefer the
            # reading that also carries the strength, e.g. ASR "Tellmisartan Salis" -> LLM "Telmisartan 40".
            gains_strength = bool(alt.strengths) and not ents.strengths and llm_top >= rule_top - 0.02
            # ASR split one name into pieces ("टेल मी साट इन 40"): the rule reading then fuzzily hits a
            # short brand ("Tel 40mg") but cannot decide; the LLM's joined name resolves on its own.
            rejoined = (llm_top >= self.s.match_t1 and self._confident(alt) and not self._confident(ents))
            if llm_top > rule_top + 1e-9 or gains_strength or rejoined:
                alt.text, alt.tokens = ents.text, ents.tokens
                alt.ordinal, alt.yes, alt.no = ents.ordinal, ents.yes, ents.no
                chosen = alt
                meta["nlu"]["used"] = True
        if nlu.get("intent") and nlu["intent"] != "CLARIFY":
            chosen.intent = nlu["intent"]
        # The LLM sometimes names the drug's usual condition ("Glycomet" -> diabetes) although the
        # speaker never mentioned one; only a "is it right for ..." question carries a symptom.
        llm_symptom = nlu.get("symptom") if nlu.get("intent") == "VERIFY_MATCH" else None
        chosen.symptom = ents.symptom or llm_symptom
        return chosen

    def _search_ents(self, e: Entities):
        return self.index.search(e.name_tokens, strength=e.strength, strengths=e.strengths, form=e.form,
                                 release=e.release, t1=self.s.match_t1, t2=self.s.match_t2)

    def _top_score(self, e: Entities | None) -> float:
        if e is None or not e.name_tokens:
            return 0.0
        res = self._search_ents(e)
        return round(res.top.score, 4) if res.top else 0.0

    def _confident(self, e: Entities | None) -> bool:
        return bool(e is not None and e.name_tokens and self._search_ents(e).confident)

    async def _llm_rewrite(self, ctx: RenderContext, turn: Turn, facts: dict) -> str | None:
        rendered = render(ctx)
        doctor = load_templates(ctx.lang)["lines"]["doctor"]
        try:
            res = await self.llm.phrase(rendered.text, facts, ctx.lang, doctor)
        except (LLMError, KeyError, ValueError) as exc:
            turn.llm_meta["reply"] = {"used": False, "error": str(exc)[:200]}
            METRICS.count("llm_reply_error")
            return None
        turn.timings["llm_reply"] = res.latency_ms
        METRICS.observe("llm_reply", res.latency_ms)
        turn.llm_meta["reply"] = {"model": res.model, "ms": res.latency_ms, "candidate": res.data["text"]}
        return res.data["text"] or None

    def _rewrite_problems(self, text: str, template_text: str, doctor: str, sources: list) -> list[str]:
        problems = []
        g = guard.validate(text, *sources)
        if not g.ok:
            problems.append(f"guard: numbers={g.bad_numbers} salts={g.bad_salts}")
        allowed, _ = guard.allowed_facts(*sources)
        word_nums = [sp.value for sp in parse_numbers(tokenize(re.sub(r"\d+(?:\.\d+)?", " ", text)))]
        bad_words = [v for v in word_nums if round(float(v), 4) not in allowed and v not in (1, 2)]
        if bad_words:
            problems.append(f"number words not in facts: {bad_words}")
        low, tlow = text.lower(), template_text.lower()
        hits = [w for w in FORBIDDEN if w in low and w not in tlow]
        if hits:
            problems.append(f"forbidden: {hits}")
        for group in ANCHORS:
            if any(a in tlow for a in group) and not any(a in low for a in group):
                problems.append(f"dropped: {group[0]}")
        unsafe = [h for h in unsafe_reply_text(text) if h not in tlow]
        if unsafe:
            problems.append(f"unsafe: {unsafe}")
        if not text.strip().endswith(doctor.strip()):
            problems.append("missing doctor line")
        if len(text.split()) > 60:
            problems.append("too long")
        return problems

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
        uses_question = (ents.intent == "VERIFY_MATCH" and ents.symptom) or ents.intent == "USE_QUERY"
        if (needs and lasa is None and uses_question and top.score >= self.s.match_t1
                and self._same_salts(res.candidates[:2])):
            # "शुगर के लिए Glycomet?": Glycomet 500 vs 250 have one usual use; asking which is noise.
            needs = False
        if needs and res.reason == "close_second" and lasa is None and self._spoken_disambiguates(res, ents):
            needs = False
        if not needs:
            turn.voice_item = self._scored_to_item(top, ents)
            turn.match_info = self._match_info(top, "voice")
            return None
        cands = list(res.candidates)
        if lasa is not None and all(c.key != lasa.key for c in cands):
            cands = [cands[0], lasa] + cands[1:]
        return {"kind": "voice", "candidates": cands[:3], "reason": res.reason if not lasa else "look_alike"}

    async def _web_brand_fallback(self, ents: Entities, turn: Turn) -> None:
        """Name not (well) matched by the drug DB -> find the brand's composition on the web and add it."""
        # A bare word with no question and no strength ("hello", "acha") is chatter, not a drug name.
        if self.web_brands is None or (ents.intent == "CLARIFY" and not ents.strengths):
            return
        res = self._search_ents(ents)
        if res.top is not None and res.top.score >= self.s.web_brand_min_score:
            return
        nlu = turn.llm_meta.get("nlu", {})
        name = nlu.get("llm_text") if nlu.get("used") else None  # the LLM's Latin-script reading
        search = query_name(extract(name).name_tokens) if name else query_name(ents.name_tokens)
        if not search:
            return
        t0 = time.perf_counter()
        found = await self.web_brands.lookup(search, ents.strength)
        turn.timings["web_brand"] = round((time.perf_counter() - t0) * 1000, 1)
        turn.llm_meta["web_brand"] = ({"found": False, "query": search} if found is None else
                                      {"found": True, "query": search, "brand": found.name,
                                       "composition": found.composition, "source": found.source,
                                       "url": found.url})
        METRICS.count("web_brand_found" if found else "web_brand_miss")
        if found is not None:
            self._vocab = None  # new salts may have arrived with the brand

    async def _use_flags(self, target: DrugItem, symptom: str | None, intent: str, turn: Turn) -> list[Flag]:
        """Symptom check / "what is it for?" from salts.usual_use, else from a trusted web source."""
        if not target.salts or not (symptom or intent == "USE_QUERY"):
            return []
        uses = checks.usual_uses(self.store, target)
        web = None
        if uses is None and self.web_uses is not None and self.s.web_lookup:
            found = await self.web_uses.lookup(target)
            turn.llm_meta["web_uses"] = ({"found": False} if found is None else
                                         {"found": True, "uses": found.uses, "source": found.source,
                                          "url": found.url, "evidence": found.evidence, "cached": found.cached,
                                          "fetched_at": found.fetched_at, "ms": found.ms})
            if found is not None:
                if found.ms is not None:
                    turn.timings["web_uses"] = found.ms
                uses = found.uses
                web = {"source": found.source, "url": found.url}
        if symptom:
            if uses is None:
                return [Flag(code="SYMPTOM_UNKNOWN", detail="usual use of this salt is not in the data",
                             facts={"symptom": symptom})]
            return [checks.symptom_flag(symptom, uses, web)]
        return [checks.use_flag(uses, web)]

    def _same_salts(self, cands: list[Scored]) -> bool:
        sets = []
        for c in cands:
            if c.brand_id is not None:
                sets.append(frozenset(s.name for s in self.store.brands[c.brand_id].salts))
            elif c.salt:
                sets.append(frozenset([c.salt]))
        return len(sets) == len(cands) and len(set(sets)) == 1 and bool(sets[0])

    def _spoken_disambiguates(self, res: MatchResult, ents: Entities) -> bool:
        """'Glycomet SR 500': top and #2 are sibling SKUs and only the top one has the spoken
        release / form / strength, so asking "SR or plain?" would repeat the user's own words."""
        if len(res.candidates) < 2 or res.top.brand_id is None or res.candidates[1].brand_id is None:
            return False
        a, b = self.store.brands[res.top.brand_id], self.store.brands[res.candidates[1].brand_id]
        if a.family != b.family:
            return False
        said = []
        if ents.release:
            said.append(((a.release or "IR") == ents.release, (b.release or "IR") == ents.release))
        if ents.form:
            said.append((a.form == ents.form, b.form == ents.form))
        if ents.strengths:
            said.append((bool(res.top.strength_ok), bool(res.candidates[1].strength_ok)))
        return bool(said) and all(x for x, _ in said) and any(not y for _, y in said)

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
        if not forms:  # no brand of this strength in the DB: use the government price lists
            for ref in self.store.price_refs:
                if ref.salt_key == sk and ref.form and strength_key(item.salts, ref.form) == ref.strength_key:
                    forms[ref.form] = forms.get(ref.form, 0) + (2 if ref.form in ("tablet", "capsule") else 1)
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
            # Keep "Drops" / "Suspension" in the label: "Crocin" vs "Crocin 1000mg" is no choice at all.
            label = brand_display(b.name, keep_form=b.form not in (None, "tablet", "capsule"))
            return Candidate(candidate_id=f"c{idx}_b{b.id}", brand_id=b.id, brand=b.name, label=label,
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
        if canon in self.vocab:
            return self.vocab.forms[canon]
        hits = {c for _, _, c in self.vocab.find(name)}  # "Metformin Hydrochloride I.P." -> metformin
        if len(hits) == 1:
            return hits.pop()
        return self.vocab.canon(canon) or canon

    def resolve_ocr(self, o: OCRItem, source: str, provider: str = "vlm", meta: dict | None = None) -> DrugItem:
        meta = meta if meta is not None else {}
        local = provider.startswith(("rapidocr", "tesseract"))
        # Text read off a photo is untrusted input: anything that may be spoken must look like a
        # brand / salt / dose line (no links, phone numbers or instructions printed on the pack).
        o = o.model_copy()
        printed_brand = o.brand
        o.brand = safe_label(o.brand)
        if printed_brand and o.brand is None:
            meta["brand_dropped"] = True
        o.directions = safe_label(o.directions, max_len=120, max_words=20)
        ocr_lines = (o.raw_text or "").splitlines()
        # Offline OCR has no structure: salts come only from the closed-set extractor, never from
        # whatever words happen to sit next to a "mg".
        ocr_salts = extract_salts(ocr_lines, self.vocab) if ocr_lines else []
        salts = [] if local else [SaltDose(name=self._fix_salt_name(s.name), amount=s.amount,
                                           unit=(s.unit or "").lower() or None, per_ml=s.per_ml)
                                  for s in o.salts if s.name]
        if local:
            salts = ocr_salts
        else:  # a VLM salt we do not know is spoken as read: it must at least look like a salt name
            salts = [s for s in salts if s.name in self.vocab or safe_label(s.name, max_len=40, max_words=4)]
        if not local and salts and ocr_salts:
            # VLM vs printed text: if the OCR lines name salts and none of them is what the VLM said,
            # the VLM may have read (or invented) the wrong thing; do not trust its list as complete.
            vlm_names, seen = {s.name for s in salts}, {s.name for s in ocr_salts}
            meta["crosscheck"] = {"vlm_salts": sorted(vlm_names), "ocr_salts": sorted(seen),
                                  "agree": bool(vlm_names & seen)}
        if not salts and o.composition_text and not local:
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
                        directions=o.directions, raw_text=o.composition_text,
                        category="supplement" if (o.category or "").lower() == "supplement" else None)
        if not local and meta.get("crosscheck", {}).get("agree") is False:
            item.composition_uncertain = True
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
        if source == "strip":  # OCR text: the largest printed lines, then the first rows
            extra = list(o.big_text) or [ln for ln in ocr_lines if re.search(r"[A-Za-z]{3,}", ln)][:8]
            for ln in extra:
                ln = re.sub(r"[^\w\s.+\-]", " ", ln)
                # "Paracetamol Tablets IP" is the composition, not a brand: matching it would pin a
                # generic product (and its strength) that the pack never printed.
                if ln != o.brand and not self.vocab.find(ln):
                    brand_lines.append(ln)
        found: list[tuple[str, MatchResult]] = []
        for line in brand_lines:
            # OCR often joins the trade name and strength (AUGMENTIN625,
            # GLYCOMET-SR500). Keep the printed line but tokenize those boundaries.
            spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", line)
            spaced = re.sub(r"-(?=(?:SR|ER|XR|CR|DR)\b)", " ", spaced, flags=re.I)
            tokens = re.findall(r"[^\s]+", spaced)
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", line)]
            res = self.index.search(tokens, strengths=nums or None, form=form, release=release,
                                    t1=self.s.match_t1, t2=self.s.match_t2)
            if res.top and res.top.brand_id is not None:
                # A fuzzy hit on OCR text must be fully confident; the printed brand gets a little slack.
                if (res.confident or (line is o.brand and res.top.score >= self.s.match_t1 - 0.05)
                        or self._release_settles(res, release)):
                    found.append((line, res))
            if line is o.brand and res.confident:
                break
        if item.category == "supplement":
            found = []  # the brand DB is medicines only: a supplement must not become a look-alike drug
        found.sort(key=lambda lr: -lr[1].top.score)
        if found:
            meta["brand_match"] = []
        best = None
        for line, res in found:
            b = self.store.brands[res.top.brand_id]
            agree = self._same_product(item.salts, b.salts)
            meta["brand_match"].append({"line": line, "brand": b.name, "score": round(res.top.score, 3),
                                        "salts_agree": agree})
            if agree:
                best = (line, res)
                break
        if best is not None:
            top = best[1].top
            b = self.store.brands[top.brand_id]
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
            if local:  # the product is known: its salt list is the DB's, not a partial OCR read
                item.salts = [s.model_copy() for s in b.salts]
        if local and item.brand_id is None:
            # Offline OCR that did not resolve to a known product: never trust the salt list as
            # complete (a missed ingredient could hide a banned combination); a guessed line is no brand.
            item.composition_uncertain = True
            item.brand = None
        if item.release is None:
            item.release = self.infer_release(item)
        return item

    def _release_settles(self, res: MatchResult, release: str | None) -> bool:
        """'Diabetrol SR' 0.94 vs 'Diabetrol' 0.91 is a close call by name, but the pack printed
        'Sustained Release': if only the top candidate in the close group has that release, take it."""
        from app.engine.salts import release_class

        top = res.top
        if release is None or top is None or top.brand_id is None or top.score < self.s.match_t1:
            return False
        close = [c for c in res.candidates if c.brand_id is not None and c.score >= top.score - self.s.match_t2]
        def fits(c: Scored) -> bool:
            b = self.store.brands[c.brand_id]
            return release_class(b.release or "IR", b.salt_key) == release_class(release, b.salt_key)

        same = [c for c in close if fits(c)]
        return len(same) == 1 and same[0] is top

    @staticmethod
    def _same_product(read: list[SaltDose], db: list[SaltDose]) -> bool:
        """The read salts fit the DB product: same names, and no printed strength that differs.
        ("Albomol" read as paracetamol 500 mg must not become "Albimol 650 mg".)"""
        if not read:
            return True
        if {s.name for s in read} != {s.name for s in db}:
            return False
        by_name = {s.name: s for s in db}
        for s in read:
            a, b = s.comparable(), by_name[s.name].comparable()
            if a is not None and b is not None and a != b:
                return False
        return True

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
            # "सर्दी के लिए पैरासिटामोल सही है?" names a symptom: answer it from the salt's usual use.
            # Without one, "is it right / the same?" means strip vs prescription, which needs the photos.
            if intent == "VERIFY_MATCH" and not turn.ents.get("symptom"):
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
        strip = turn.strip_item
        if strip is not None and target is strip:
            if strip.category == "supplement":
                flags.append(Flag(code="SUPPLEMENT", detail="pack says dietary / food supplement"))
            elif strip.brand and strip.brand_id is None and strip.salts:
                flags.append(Flag(code="BRAND_NOT_IN_DATA", detail="printed brand not in the brand DB",
                                  facts={"printed": strip.brand}))
        if target.brand_id is not None and target.brand_id >= LEARNED_ID_BASE:
            b = self.store.brands[target.brand_id]
            flags.append(Flag(code="WEB_SOURCED", detail="brand composition from a web source, not the checked DB",
                              facts={"source": (b.source or "web").removeprefix("web: ").split(" (")[0],
                                     "updated_at": b.updated_at}))
        for f in (
            checks.dup_salt(target, turn.pouch),
            *(await self._use_flags(target, turn.ents.get("symptom"), intent, turn)),
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
            # A "is it right for <symptom>?" question gets the symptom answer, not the price detail.
            directions=directions, skip_detail=turn.followup == "symptom" or intent == "USE_QUERY" or (
                intent == "VERIFY_MATCH" and bool(turn.ents.get("symptom"))),
        )
        rewrite = None
        # "Is it right for <symptom>?" answers are medical claims: the vetted template speaks them as is.
        # (A rewrite turned "cold is not its usual use" into "not taken in a cold", which the guard can't see.)
        symptom_answer = any(f.code.startswith(ANSWER_FLAG_PREFIXES) for f in flags)
        from app.config import REWRITE_LANGS

        if self.llm is not None and self.s.llm_reply and not symptom_answer and turn.lang in REWRITE_LANGS:
            facts = {"verdict": verdict.code, "medicine": target.model_dump(include={"brand", "salts", "form",
                                                                                        "release", "mrp", "pack_qty"}),
                     "price": price.model_dump(exclude_none=True),
                     "alternatives": [a.model_dump(include={"name", "price", "pack_qty", "saves_pct"})
                                      for a in spoken_alts[:1]],
                     "flags": [f.code for f in flags]}
            rewrite = await self._llm_rewrite(ctx, turn, facts)
        return self._respond(session, turn, ctx, status=status, t_start=t_start, verdict=verdict,
                             safety=safety, price=price, alternatives=alts, flags=flags, focus=focus,
                             prescribed=prescribed, dispensed=dispensed, target=target, rewrite=rewrite)

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

    def _finish_guard(self, session: Session, inp: QueryInput, kind: str, timings: dict, t_start: float,
                      transcript: str | None) -> QueryResponse:
        """Guardrail reply. Only the kind is logged, never the text."""
        METRICS.count(f"guard_{kind}")
        log_event("guard", session=session.id, kind=kind)
        session.pending = None
        turn = Turn(lang=inp.lang, transcript=transcript, ents={"intent": "CLARIFY"}, strip_item=None,
                    rx_items=[], voice_item=None, chosen_rx=None, pouch=inp.pouch, timings=timings,
                    channel=inp.channel, tts_sample_rate=inp.tts_sample_rate)
        ctx = RenderContext(lang=inp.lang, status="blocked", guard_kind=kind)
        resp = self._respond(session, turn, ctx, status="blocked", t_start=t_start,
                             guard_sources=[{"helplines": HELPLINES}])
        resp.card = Card(color="red" if kind in ("self_harm", "emergency") else "amber", icon="question",
                         title=f"GUARD_{kind.upper()}")
        resp.data_sources["guard"] = {"kind": kind}
        return resp

    def _respond(self, session: Session, turn: Turn, ctx: RenderContext, status: str, t_start: float,
                 verdict: Verdict | None = None, safety: SafetyResult | None = None, price=None,
                 alternatives=None, flags=None, candidates=None, focus=None, prescribed=None, dispensed=None,
                 target=None, guard_sources=None, rewrite: str | None = None) -> QueryResponse:
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
        if rewrite is not None:
            doctor = load_templates(ctx.lang)["lines"]["doctor"]
            problems = self._rewrite_problems(rewrite, rendered.text, doctor, sources)
            turn.llm_meta.setdefault("reply", {})
            turn.llm_meta["reply"].update(used=not problems, rejected=problems or None)
            if problems:
                METRICS.count("llm_reply_rejected")
                log_event("llm_reply_rejected", session=session.id, problems=problems)
            else:
                METRICS.count("llm_reply_used")
                body = rewrite.strip()
                while body.endswith(doctor.strip()):  # the LLM sometimes repeats the closing line
                    body = body[: -len(doctor.strip())].strip()
                segments = [Segment(body, "dynamic"), Segment(doctor, "fixed")]
                text = f"{body} {doctor}"
        chemist = rendered.chemist_text
        if chemist and not guard.validate(chemist, *sources).ok:
            chemist = ""
        if status == "blocked":  # helpline numbers are spoken digit by digit: "एक चार चार एक छह"
            speech_segments = [speakable(_digits_apart(s.text), ctx.lang) for s in segments]
        else:
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
        if turn.llm_meta:
            meta = dict(turn.llm_meta)
            if "asr" in meta:
                resp.data_sources["asr"] = meta.pop("asr")
            if meta:
                resp.data_sources["llm"] = meta
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


HELPLINES = [14416, 112, 108]  # Tele-MANAS, national emergency, ambulance (spoken in guard replies)


def _digits_apart(text: str) -> str:
    return re.sub(r"\d{3,}", lambda m: " ".join(m.group(0)), text)


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
