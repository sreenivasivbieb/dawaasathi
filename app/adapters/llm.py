"""Groq LLM (OpenAI-compatible chat API) for two jobs, never for medical facts:

1. understand(): transcript -> JSON {intent, drugs[{name, strength, unit, form, release}], symptom}.
   The matcher still resolves names against the brand DB (closed set), so a wrong guess can only
   lead to a confirmation question, not to an invented drug.
2. phrase(): rewords the template reply from the verdict facts. The pipeline runs the zero-
   hallucination guard on the result and falls back to the template if anything new appears.

Keys: GROQ_API_KEYS (comma list, rotated on 429) / GROQ_API_KEY. Model: GROQ_LLM_MODEL.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from app.config import Settings

URL = "https://api.groq.com/openai/v1/chat/completions"

INTENTS = ("FIND_GENERIC", "PRICE_CHECK", "DOSAGE_QUERY", "VERIFY_MATCH", "USE_QUERY", "CLARIFY")
SYMPTOMS = ("fever", "pain", "infection", "cold", "cough", "allergy", "diabetes", "bp", "acidity", "thyroid",
            "cholesterol", "asthma", "diarrhoea", "vomiting", "vitamin", "heart", "skin", "eye", "inflammation")

NLU_PROMPT = f"""You read one transcribed utterance from a patient in India talking to a medicine helper.
Speech is often Hindi/Marathi/English code-mixed and the transcript may contain ASR errors.
Extract ONLY what the speaker said. Never add a drug, strength or fact they did not say.

Return JSON: {{"intent": one of {list(INTENTS)},
 "drugs": [{{"name": "drug/brand name as heard, written in Latin script (transliterate Devanagari, e.g. "
 "ऑगमेंटिन -> Augmentin)", "strength": number|null, "unit": "mg"|"mcg"|"g"|"ml"|"iu"|null,
 "form": "tablet"|"capsule"|"syrup"|"drops"|"injection"|null, "release": "SR"|"ER"|"DR"|null}}],
 "symptom": one of {list(SYMPTOMS)} or null  (ONLY if the speaker names their own health problem,
   e.g. "बुखार के लिए"; NEVER infer it from the drug: "Glycomet 500 ka sasta" -> null),
 "yes_no": "yes"|"no"|null}}

Intents: FIND_GENERIC = wants a cheaper / generic / Jan Aushadhi option; PRICE_CHECK = asks the price or
whether it is overpriced; DOSAGE_QUERY = how/when/how much to take; VERIFY_MATCH = asks if the medicine
is right / same as prescribed / correct for a problem; USE_QUERY = asks what the medicine is used for
("ये गोली किस लिए है", "kis kaam aati hai", "what is it for"); CLARIFY = anything else.
Number words are numbers: "छह सौ पच्चीस" = 625, "six fifty" = 650. The ASR sometimes writes Hindi
number words or drug names in garbled Latin script: "Salis"/"chalis" = 40 (चालीस), "pachas" = 50,
"Saturn Salis" may be "Telmisartan 40". Give your best reading of the drug name and number; if you
cannot tell, still return the name as heard (the app double-checks against its drug database). Brand numbers such as "Dolo 650"
or "Augmentin 625" go in strength. Fillers like भैया / दीदी / बताओ are not drug names."""

PHRASE_PROMPT = """You turn a fixed medicine-helper reply into natural, warm, simple spoken {lang_name} for a
low-literacy listener. Rules, all mandatory:
- Use ONLY the facts in REPLY and FACTS. Do not add any number, price, percentage, drug, salt, dose,
  brand or medical claim that is not already there. Keep every number exactly as written.
- Never advise to stop, start, change or skip a medicine; never say fraud, fake or cheating.
- Keep drug and brand names in Latin script exactly as given.
- At most {max_words} words. End with exactly this sentence: "{doctor_line}"
Return ONLY the spoken reply text, nothing else (no quotes, no labels, no JSON)."""

LANG_NAMES = {"hi": "Hindi", "en": "Indian English", "mr": "Marathi", "ta": "Tamil", "te": "Telugu"}


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    data: dict
    latency_ms: float
    model: str


class GroqLLM:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client
        self._key_idx = 0

    @property
    def available(self) -> bool:
        return bool(self.s.groq_api_keys and self.s.groq_llm_model)

    async def chat_json(self, system: str, user: str, max_tokens: int = 400) -> LLMResult:
        res = await self._chat(system, user, max_tokens, json_mode=True)
        res.data = _parse_json(res.data["text"])
        return res

    async def _chat(self, system: str, user: str, max_tokens: int, json_mode: bool) -> LLMResult:
        if not self.available:
            raise LLMError("GROQ_API_KEY / GROQ_LLM_MODEL not set")
        body = {"model": self.s.groq_llm_model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if "gpt-oss" in self.s.groq_llm_model:  # reasoning models: keep latency low, hide the trace
            body["reasoning_effort"] = "low"
            body["include_reasoning"] = False
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        keys = self.s.groq_api_keys
        last = "no attempt"
        for attempt in range(len(keys) + 1):
            key = keys[(self._key_idx + attempt) % len(keys)]
            t0 = time.perf_counter()
            try:
                resp = await self.client.post(URL, headers={"Authorization": f"Bearer {key}"}, json=body,
                                              timeout=httpx.Timeout(self.s.llm_timeout_s, connect=4.0))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"network: {type(exc).__name__}"
                continue
            ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 429:
                last = "rate limited"
                continue
            if resp.status_code != 200:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            self._key_idx = (self._key_idx + attempt) % len(keys)
            content = resp.json()["choices"][0]["message"]["content"] or ""
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            return LLMResult({"text": content}, round(ms, 1), body["model"])
        raise LLMError(last)

    async def understand(self, transcript: str, lang: str) -> LLMResult:
        res = await self.chat_json(NLU_PROMPT, f"Language hint: {lang}\nUtterance: {transcript}", 700)
        res.data = clean_nlu(res.data)
        return res

    async def phrase(self, template_text: str, facts: dict, lang: str, doctor_line: str,
                     max_words: int = 45) -> LLMResult:
        system = PHRASE_PROMPT.format(lang_name=LANG_NAMES.get(lang, "Hindi"), max_words=max_words,
                                      doctor_line=doctor_line)
        user = "REPLY:\n" + template_text + "\n\nFACTS:\n" + json.dumps(facts, ensure_ascii=False, default=str)
        # Plain text: JSON mode makes gpt-oss fail on long Devanagari output (json_validate_failed).
        # Reasoning tokens count against max_tokens on gpt-oss; 400 truncated replies mid-sentence.
        res = await self._chat(system, user, 900, json_mode=False)
        res.data = {"text": res.data["text"].strip().strip('"').strip()}
        return res


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise LLMError("LLM did not return JSON")


def clean_nlu(d: dict) -> dict:
    intent = d.get("intent") if d.get("intent") in INTENTS else "CLARIFY"
    drugs = []
    for x in d.get("drugs") or []:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()[:60]
        if not name or not re.search(r"[A-Za-z]{2}", name):
            continue
        try:
            strength = float(x["strength"]) if x.get("strength") not in (None, "") else None
        except (TypeError, ValueError):
            strength = None
        drugs.append({"name": name, "strength": strength,
                      "unit": x.get("unit") if x.get("unit") in ("mg", "mcg", "g", "ml", "iu") else None,
                      "form": x.get("form") if x.get("form") in ("tablet", "capsule", "syrup", "drops",
                                                                  "injection") else None,
                      "release": x.get("release") if x.get("release") in ("SR", "ER", "DR") else None})
    symptom = d.get("symptom") if d.get("symptom") in SYMPTOMS else None
    yn = d.get("yes_no") if d.get("yes_no") in ("yes", "no") else None
    return {"intent": intent, "drugs": drugs[:3], "symptom": symptom, "yes_no": yn}


def nlu_to_text(nlu: dict) -> str | None:
    """Latin-script phrase the rule-based extractor + matcher understand: 'Augmentin 625 tablet SR'."""
    if not nlu.get("drugs"):
        return None
    d = nlu["drugs"][0]
    parts = [d["name"]]
    if d.get("strength") is not None:
        parts.append(f"{d['strength']:g}{d.get('unit') or ''}".strip())
    for k in ("release", "form"):
        if d.get(k):
            parts.append(d[k])
    return " ".join(parts)
