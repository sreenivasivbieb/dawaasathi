"""Usual use of a salt combination from the web, for salts our data does not cover.

Only used to answer "is X right for <symptom>?" / "what is X for?" when salts.usual_use has no
entry. Every step is checked so the answer stays grounded in a named source:

1. Tavily search restricted to TRUSTED medical / pharmacy domains.
2. The LLM picks use tags from the closed SYMPTOMS list and copies ONE sentence from a source.
3. Code verifies that sentence is really in that source, that the source names one of the salts,
   and keeps only tags whose keywords appear in that sentence. Otherwise: no answer.
4. Results (and misses) are cached in var/web_uses.json with URL and date; the reply names the source.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.adapters.llm import SYMPTOMS, GroqLLM, LLMError
from app.adapters.tavily import Tavily, TavilyError
from app.engine.salts import all_salt_surface_forms
from app.models import DrugItem

# Domain -> name spoken in the reply.
TRUSTED: dict[str, str] = {
    "1mg.com": "1mg", "medlineplus.gov": "MedlinePlus", "drugs.com": "Drugs.com", "nhs.uk": "NHS",
    "medicines.org.uk": "EMC", "apollopharmacy.in": "Apollo Pharmacy", "pharmeasy.in": "PharmEasy",
    "netmeds.com": "Netmeds", "mayoclinic.org": "Mayo Clinic", "webmd.com": "WebMD",
}

# A tag is kept only when the copied source sentence contains one of its keywords.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "fever": ("fever", "pyrexia", "temperature"),
    "pain": ("pain", "ache", "analgesic", "migraine"),
    "infection": ("infection", "bacterial", "antibiotic", "fungal", "viral", "antiviral"),
    "cold": ("common cold", "runny nose", "sneez", "nasal congestion", "blocked nose", "rhinitis", "cold"),
    "cough": ("cough", "mucus", "phlegm", "sputum", "expectorant", "mucolytic"),
    "allergy": ("allerg", "itch", "hives", "urticaria", "hay fever"),
    "diabetes": ("diabet", "blood sugar", "blood glucose", "glycemic"),
    "bp": ("blood pressure", "hypertension"),
    "acidity": ("acidity", "heartburn", "reflux", "gerd", "gord", "ulcer", "indigestion", "stomach acid",
                "acid in the stomach"),
    "thyroid": ("thyroid",),
    "cholesterol": ("cholesterol", "lipid", "triglyceride"),
    "asthma": ("asthma", "bronch", "wheez", "copd", "breathless", "airway"),
    "diarrhoea": ("diarrh", "loose motion"),
    "vomiting": ("vomit", "nausea"),
    "vitamin": ("vitamin", "deficien", "supplement", "mineral"),
    "heart": ("heart", "angina", "cardiac", "cardiovascular"),
    "skin": ("skin", "dermat", "acne", "eczema", "psoriasis"),
    "eye": ("eye", "ocular", "conjunctiv", "glaucoma"),
    "inflammation": ("inflammat", "swelling", "arthritis"),
}

PROMPT = """You extract what a medicine is commonly used for, ONLY from the numbered SOURCES.
Allowed tags (closed list): {tags}.
Tag meanings (map the conditions the source names to the closest tags):
fever = fever; pain = any pain or ache, headache, migraine, cramps; infection = bacterial, fungal or
viral infections; cold = common cold, runny or blocked nose, sneezing, allergic rhinitis; cough = cough,
mucus, phlegm; allergy = allergies, itching, hives; diabetes = diabetes, high blood sugar; bp = high
blood pressure / hypertension; acidity = excess stomach acid, heartburn, acid reflux / GERD / GORD,
stomach or duodenal ulcers, indigestion; thyroid = thyroid disorders; cholesterol = high cholesterol or
lipids; asthma = asthma, COPD, bronchospasm, wheezing; diarrhoea = diarrhoea; vomiting = nausea or
vomiting; vitamin = vitamin or mineral deficiency, supplement; heart = angina, heart failure, heart
attack or stroke prevention; skin = skin conditions; eye = eye conditions; inflammation = swelling,
inflammation, arthritis.
Rules:
- Use only a source about this medicine (the same salt or a combination containing these salts).
- Pick tags only for uses the source states. Never use your own knowledge. Prefer the source whose
  sentence lists the main uses; at most 3 tags.
- "evidence" must be ONE sentence copied character-for-character from that source that states the uses.
- If no source states a use, return {{"uses": []}}.
Return JSON: {{"uses": [tags], "source": <source number>, "evidence": "<copied sentence>"}}"""


@dataclass
class WebUses:
    uses: list[str]
    evidence: str
    url: str
    source: str
    fetched_at: str
    query: str
    cached: bool = False
    ms: float | None = None


def _norm(text: str) -> str:
    text = re.sub(r"[#*_`>|\[\]]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _domain_name(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for dom, name in TRUSTED.items():
        if host == dom or host.endswith("." + dom):
            return name
    return None


class WebUseLookup:
    def __init__(self, tavily: Tavily, llm: GroqLLM | None, cache_path: Path, miss_ttl_days: int = 7):
        self.tavily = tavily
        self.llm = llm
        self.cache_path = cache_path
        self.miss_ttl_days = miss_ttl_days
        self._cache: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except ValueError:
                self._cache = {}
        forms = all_salt_surface_forms()
        self._surface: dict[str, set[str]] = {}
        for form, canon in forms.items():
            self._surface.setdefault(canon, set()).add(form.lower())

    @property
    def available(self) -> bool:
        return self.tavily.available and self.llm is not None and self.llm.available

    @staticmethod
    def key(item: DrugItem) -> str:
        return "+".join(sorted({s.name for s in item.salts}))

    async def lookup(self, item: DrugItem) -> WebUses | None:
        if not item.salts or not self.available:
            return None
        key = self.key(item)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # two callers asking about the same salt share one search
            hit = self._cached(key)
            if hit is not None:
                return hit or None
            result, final = await self._fetch(item)
            if final:  # a search / LLM error is retried next time, not remembered as "no answer"
                self._store(key, result)
            return result

    def _cached(self, key: str) -> WebUses | bool | None:
        row = self._cache.get(key)
        if row is None:
            return None
        if row.get("miss"):
            try:
                age = (date.today() - date.fromisoformat(row["fetched_at"][:10])).days
            except (KeyError, ValueError):
                return None
            return False if age <= self.miss_ttl_days else None
        return WebUses(**{**row, "cached": True, "ms": None})

    def _store(self, key: str, result: WebUses | None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        if result is None:
            self._cache[key] = {"miss": True, "fetched_at": now}
        else:
            row = asdict(result)
            row.pop("cached", None)
            row.pop("ms", None)
            self._cache[key] = row
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass

    async def _fetch(self, item: DrugItem) -> tuple[WebUses | None, bool]:
        names = [s.name for s in item.salts]
        query = " + ".join(n.title() for n in names) + " uses"
        try:
            hits, search_ms = await self.tavily.search(query, list(TRUSTED), max_results=5, depth="basic")
        except TavilyError:
            return None, False
        hits = [h for h in hits if _domain_name(h.url) and self._mentions_salt(h.title + " " + h.content, names)]
        if not hits:
            return None, True
        # Pages naming every salt of the combination first (not a 3-salt cousin of a 2-salt drug).
        hits.sort(key=lambda h: -sum(self._mentions_salt(h.title + " " + h.content, [n]) for n in names))
        sources = "\n\n".join(f"[{i + 1}] {h.url}\n{h.title}\n{h.content[:1500]}" for i, h in enumerate(hits))
        system = PROMPT.format(tags=", ".join(SYMPTOMS))
        user = f"MEDICINE: {' + '.join(names)}\n\nSOURCES:\n{sources}"
        try:
            res = await self.llm.chat_json(system, user, max_tokens=700)
        except (LLMError, KeyError, ValueError):
            return None, False
        return self._verify(res.data, hits, query, search_ms + res.latency_ms), True

    def _mentions_salt(self, text: str, names: list[str]) -> bool:
        low = text.lower()
        for n in names:
            for form in self._surface.get(n, set()) | {n}:
                if re.search(rf"(?<![a-z]){re.escape(form)}(?![a-z])", low):
                    return True
        return False

    def _verify(self, data: dict, hits: list, query: str, ms: float) -> WebUses | None:
        try:
            idx = int(data.get("source")) - 1
        except (TypeError, ValueError):
            return None
        evidence = str(data.get("evidence") or "").strip()
        if not (0 <= idx < len(hits)) or len(evidence) < 20:
            return None
        hit = hits[idx]
        if _norm(evidence) not in _norm(hit.title + " " + hit.content):
            return None  # not copied from the source: the model wrote it
        ev = evidence.lower()
        uses = []
        for tag in data.get("uses") or []:
            if tag in KEYWORDS and tag not in uses and any(k in ev for k in KEYWORDS[tag]):
                uses.append(tag)
        if not uses:
            return None
        return WebUses(uses=uses[:3], evidence=evidence, url=hit.url, source=_domain_name(hit.url) or "web",
                       fetched_at=datetime.now().isoformat(timespec="seconds"), query=query, ms=round(ms, 1))
