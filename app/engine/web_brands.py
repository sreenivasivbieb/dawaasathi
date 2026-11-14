"""Web fallback for brands the drug DB does not know: find the composition, verify it, learn it.

The drug DB is checked first (fast, offline, reviewed data). Only when nobody in it matches the
spoken / typed name well enough does this run:

1. Tavily search for "<name> composition" restricted to Indian pharmacy / drug-info sites.
2. The LLM picks the product page that is about THIS name and copies its name, composition, form,
   pack and MRP from the page. It may not use its own knowledge.
3. Code verifies, before anything is used:
   - the product name is in the source text and is close to the name the user said;
   - every salt name and every strength number of the composition is in the source text;
   - the composition parses into salts + strengths (app.engine.salts.parse_composition);
   - an MRP is kept only if that number is in the source text.
4. A verified product is stored in var/local.db (learned_brands, with URL + date) and added to the
   in-memory store and matcher, so the next question about it is answered from the DB.

What it does NOT change: the banned-combination check, Jan Aushadhi / NPPA prices and NSQ alerts
still come only from the official lists. A web-learned brand only tells us which salts the brand
contains; the reply says the details came from the web and names the site.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from rapidfuzz import fuzz

from app.adapters.llm import GroqLLM, LLMError
from app.adapters.tavily import Tavily, TavilyError
from app.db import Store
from app.engine.salts import canonical_form, parse_composition
from app.localdb import LocalDB
from app.match.index import MatchIndex
from app.match.translit import normalize, to_roman

# Domain -> name spoken / shown in the reply. Product pages on these sites print the composition.
SOURCES: dict[str, str] = {
    "1mg.com": "1mg", "pharmeasy.in": "PharmEasy", "netmeds.com": "Netmeds",
    "apollopharmacy.in": "Apollo Pharmacy", "truemeds.in": "Truemeds", "medplusmart.com": "MedPlus",
    "practo.com": "Practo", "medindia.net": "Medindia",
}

MISS_TTL_S = 7 * 24 * 3600
MIN_NAME_SIMILARITY = 80  # rapidfuzz ratio between the spoken name and the product's brand name

PROMPT = """You read web search results about a medicine sold in India and copy its details.
The user asked about: "{name}".
Rules:
- Use ONLY the numbered SOURCES. Never use your own knowledge. Copy text exactly as written.
- Pick the source whose product is the medicine the user named (same brand name; the strength
  may be given in the name, e.g. "Dolo 650 Tablet"). If none is clearly that product, return
  {{"found": false}}.
- "composition": the salts with strengths exactly as the source writes them, e.g.
  "Amoxycillin (500mg) + Clavulanic Acid (125mg)". Salt names in English.
- "product_name": the product name exactly as written in the source.
- "form": tablet | capsule | syrup | suspension | drops | injection | cream | gel | ointment |
  inhaler | powder | lotion | null.
- "pack": pack size text as written (e.g. "strip of 15 tablets") or null.
- "mrp": the pack MRP in rupees as a number, only if the source states it; else null.
- "manufacturer": as written, or null.
Return JSON: {{"found": true, "source": <source number>, "product_name": "...", "composition": "...",
 "form": "...", "pack": "...", "mrp": <number|null>, "manufacturer": "..."}}"""


@dataclass
class LearnedBrand:
    brand_id: int
    name: str
    composition: str
    source: str
    url: str
    cached: bool = False
    ms: float | None = None


def _domain_name(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for dom, name in SOURCES.items():
        if host == dom or host.endswith("." + dom):
            return name
    return None


def _flat(text: str) -> str:
    """Lowercase, markdown and punctuation flattened: for 'is this text in the source' checks."""
    text = re.sub(r"[#*_`>|\[\]()]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _nums(text: str) -> set[float]:
    return {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


_PACK = re.compile(r"(\d+(?:\.\d+)?)\s*(tablets?|capsules?|ml|g|gm)\b", re.I)


def _pack(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    m = _PACK.search(text)
    if not m:
        return None, None
    unit = m.group(2).lower()
    unit = {"tablet": "tablets", "capsule": "capsules", "gm": "g"}.get(unit, unit)
    return float(m.group(1)), unit


def query_name(name_tokens: list[str]) -> str:
    """Spoken tokens (any script) -> a Latin-script name to search for."""
    return re.sub(r"\s+", " ", to_roman(" ".join(name_tokens))).strip()


class WebBrandLookup:
    def __init__(self, tavily: Tavily, llm: GroqLLM | None, local: LocalDB, store: Store, index: MatchIndex):
        self.tavily = tavily
        self.llm = llm
        self.local = local
        self.store = store
        self.index = index
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self.tavily.available and self.llm is not None and self.llm.available

    def load_learned(self) -> int:
        """Merge every stored learned brand into the store + index (startup)."""
        n = 0
        for row in self.local.learned_brands():
            rec = self.store.add_learned(row)
            if rec is not None:
                self.index.add_brand(rec.id)
                n += 1
        return n

    async def lookup(self, name: str, strength: float | None = None) -> LearnedBrand | None:
        """Find `name` on the web, verify it and add it to the store. None if not found / unverified."""
        name = name.strip()
        key = normalize(name)
        if len(key) < 3 or not self.available:
            return None
        miss_key = f"{key}|{strength or ''}"
        lock = self._locks.setdefault(miss_key, asyncio.Lock())
        async with lock:
            if self.local.recent_miss(miss_key, MISS_TTL_S):
                return None
            t0 = asyncio.get_running_loop().time()
            found, final = await self._fetch(name, strength)
            if found is None:
                if final:  # a network / LLM error is retried next time, not remembered as a miss
                    self.local.add_miss(miss_key)
                return None
            found.ms = round((asyncio.get_running_loop().time() - t0) * 1000, 1)
            return found

    async def _fetch(self, name: str, strength: float | None) -> tuple[LearnedBrand | None, bool]:
        q = f"{name} {strength:g}" if strength else name
        try:
            hits, _ = await self.tavily.search(f"{q} tablet composition price", list(SOURCES),
                                               max_results=6, depth="basic")
        except TavilyError:
            return None, False
        hits = [h for h in hits if _domain_name(h.url)]
        if not hits:
            return None, True
        sources = "\n\n".join(f"[{i + 1}] {h.url}\n{h.title}\n{h.content[:1800]}" for i, h in enumerate(hits))
        try:
            res = await self.llm.chat_json(PROMPT.format(name=q), f"SOURCES:\n{sources}", max_tokens=600)
        except (LLMError, KeyError, ValueError):
            return None, False
        row = self._verify(res.data, hits, name, q)
        if row is None:
            return None, True
        row_id = self.local.add_learned_brand(row)
        if row_id is None:  # same product already learned (e.g. by a parallel request)
            existing = next((r for r in self.local.learned_brands()
                             if r["name_key"] == row["name_key"] and r["composition"] == row["composition"]), None)
            if existing is None:
                return None, True
            row = existing
        else:
            row["id"] = row_id
        rec = self.store.add_learned(row)
        if rec is None:
            return None, True
        self.index.add_brand(rec.id)
        return LearnedBrand(brand_id=rec.id, name=rec.name, composition=row["composition"],
                            source=row["source"], url=row["source_url"]), True

    def _verify(self, data: dict, hits: list, name: str, query: str) -> dict | None:
        if not data.get("found"):
            return None
        try:
            idx = int(data.get("source")) - 1
        except (TypeError, ValueError):
            return None
        if not 0 <= idx < len(hits):
            return None
        hit = hits[idx]
        text = _flat(hit.title + " " + hit.content)
        product = re.sub(r"\s+", " ", str(data.get("product_name") or "")).strip()
        composition = re.sub(r"\s+", " ", str(data.get("composition") or "")).strip()
        if not product or not composition or len(product) > 120 or len(composition) > 300:
            return None
        if _flat(product) not in text:
            return None  # the model wrote a name the page does not have
        # The product must be the one the user named, not a different brand on the same page.
        from app.db import split_brand_name

        base, _, _ = split_brand_name(product)
        if fuzz.ratio(normalize(base), normalize(name)) < MIN_NAME_SIMILARITY and \
                fuzz.partial_ratio(normalize(name), normalize(product)) < 90:
            return None
        clean = re.sub(r"\s+", " ", re.sub(r"[()]", " ", composition)).strip()
        salts = parse_composition(clean)
        if not salts or any(s.amount is None for s in salts):
            return None
        page_nums = _nums(text)
        # Check the composition as the page wrote it (parse_composition canonicalises the names:
        # "Amoxycillin" -> "amoxicillin", which the page may not spell that way).
        parts = [p for p in re.split(r"\+|;|,|\band\b", clean.lower()) if p.strip()]
        if len(parts) != len(salts):
            return None
        for part in parts:
            words = [w for w in re.findall(r"[a-z]{3,}", part) if w not in ("mcg", "per")]
            if not words or not all(w in text for w in words[:2]):
                return None  # salt name not on the page
            if not _nums(part) or not _nums(part) <= page_nums:
                return None  # strength not on the page
        mrp = data.get("mrp")
        try:
            mrp = float(mrp) if mrp is not None else None
        except (TypeError, ValueError):
            mrp = None
        if mrp is not None and (mrp <= 0 or mrp not in page_nums):
            mrp = None
        pack_qty, pack_unit = _pack(data.get("pack") if isinstance(data.get("pack"), str) else None)
        if pack_qty is not None and pack_qty not in page_nums:
            pack_qty = pack_unit = None
        manufacturer = str(data.get("manufacturer") or "").strip() or None
        if manufacturer and _flat(manufacturer) not in text:
            manufacturer = None
        form = canonical_form(str(data.get("form") or "")) or canonical_form(product)
        return {
            "name": product, "name_key": normalize(product), "composition": clean.strip(),
            "form": form, "release": None, "pack_qty": pack_qty, "pack_unit": pack_unit,
            "mrp": mrp if pack_qty else None,  # an MRP without its pack size cannot be compared per unit
            "manufacturer": manufacturer, "source": _domain_name(hit.url) or "web", "source_url": hit.url,
            "evidence": composition, "query": query,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
