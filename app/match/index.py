"""Phonetic + trigram index over brand names, brand aliases and salt names.

search() takes the content tokens of an utterance (or an OCR'd brand line), scores every
1-3 token span against the index and resolves the best names to concrete SKUs using the
spoken strength / form / release.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.db import Store
from app.match.translit import dm_keys, normalize, skeleton, soundex, trigrams


# Score cut for a SKU whose name has a word the speaker did not say (see _unspoken_words). Larger
# than T2 so "Telma 40" -> "Telma 40 Tablet" is accepted over "Telma AZ 40mg/8mg Tablet".
EXTRA_WORD_PENALTY = 0.07


@dataclass(slots=True)
class Entry:
    norm: str
    kind: str  # family | base | alias | salt
    brand_ids: list[int]
    salt: str | None
    dm: tuple[str, ...]
    skel: str
    sdx: str


@dataclass
class Scored:
    key: str  # "b:<id>" or "s:<salt>|<strength>|<form>"
    brand_id: int | None
    salt: str | None
    name_score: float
    score: float
    span: str
    entry_norm: str
    strength_ok: bool | None = None
    tiebreak: float = 0.0  # query vs full SKU name; only orders exact score ties


@dataclass
class MatchResult:
    candidates: list[Scored] = field(default_factory=list)
    confident: bool = False
    reason: str = ""

    @property
    def top(self) -> Scored | None:
        return self.candidates[0] if self.candidates else None


class MatchIndex:
    def __init__(self, store: Store):
        self.store = store
        self.entries: list[Entry] = []
        self.tri: dict[str, list[int]] = defaultdict(list)
        self.by_dm: dict[str, list[int]] = defaultdict(list)
        self.by_skel: dict[str, list[int]] = defaultdict(list)
        self.weak_salts: set[str] = set()
        self._by_name: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- build
    def add_brand(self, brand_id: int) -> None:
        """Index one brand added to the store after build() (a web-learned brand)."""
        b = self.store.brands[brand_id]
        keys = [(normalize(b.base), "base")]
        if b.family != b.base:
            keys.append((normalize(b.family), "family"))
        for norm, kind in keys:
            if not norm:
                continue
            idx = self._by_name.get((norm, kind))
            if idx is not None:
                if brand_id not in self.entries[idx].brand_ids:
                    self.entries[idx].brand_ids.append(brand_id)
            else:
                self._add(Entry(norm, kind, [brand_id], None, dm_keys(norm), skeleton(norm), soundex(norm)))
        for s in b.salts:
            n = normalize(s.name)
            if (n, "salt") not in self._by_name:
                self._add(Entry(n, "salt", [], s.name, dm_keys(n), skeleton(n), soundex(n)))

    def build(self) -> "MatchIndex":
        groups: dict[tuple[str, str], set[int]] = defaultdict(set)
        for b in self.store.brands.values():
            groups[(normalize(b.base), "base")].add(b.id)
            if b.family != b.base:
                groups[(normalize(b.family), "family")].add(b.id)
        for brand_id, alias in self.store.aliases:
            groups[(normalize(alias), "alias")].add(brand_id)
        for key, (norm_kind) in list(groups.items()):
            norm, kind = key
            if not norm:
                continue
            self._add(Entry(norm, kind, sorted(groups[key]), None, dm_keys(norm), skeleton(norm),
                            soundex(norm)))
        core = set(self.store.salt_use) | {s.name for b in self.store.brands.values() for s in b.salts}
        price_only = {s.name for r in self.store.price_refs for s in r.salts} - core
        # Salts known only from the price lists are matchable, but need a close name match:
        # a garbled transcript ("Salis") must not turn into a confirmation about "Cilastatin".
        self.weak_salts = price_only
        salts = core | price_only
        from app.engine.salts import all_salt_surface_forms

        surface = all_salt_surface_forms()
        for form, canon in surface.items():
            if canon in salts:
                n = normalize(form)
                self._add(Entry(n, "salt", [], canon, dm_keys(n), skeleton(n), soundex(n)))
        for s in salts:
            n = normalize(s)
            self._add(Entry(n, "salt", [], s, dm_keys(n), skeleton(n), soundex(n)))
        return self

    def _add(self, e: Entry) -> None:
        idx = len(self.entries)
        self.entries.append(e)
        self._by_name.setdefault((e.norm, e.kind), idx)
        for t in trigrams(e.norm):
            self.tri[t].append(idx)
        for k in e.dm:
            self.by_dm[k].append(idx)
        if e.skel:
            self.by_skel[e.skel].append(idx)

    def keyterms(self, n: int) -> list[str]:
        """Most useful brand words to bias Prisma towards (Latin script)."""
        seen: dict[str, int] = defaultdict(int)
        for b in self.store.brands.values():
            seen[b.family.title()] += 1
        ranked = [name for name, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
        # Then single-ingredient salts that have a Jan Aushadhi / NPPA price (spoken as generics).
        salt_count: dict[str, int] = defaultdict(int)
        for r in self.store.price_refs:
            if len(r.salts) == 1:
                salt_count[r.salts[0].name.title()] += 1
        ranked += [name for name, _ in sorted(salt_count.items(), key=lambda kv: (-kv[1], kv[0]))
                   if name not in ranked and len(name) <= 24]
        return ranked[:n]

    # ---------------------------------------------------------------- scoring
    def _candidates(self, norm: str, allowed: set[int] | None) -> set[int]:
        counts: dict[int, int] = defaultdict(int)
        for t in trigrams(norm):
            for idx in self.tri.get(t, ()):
                counts[idx] += 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:60]
        cand = {idx for idx, _ in top}
        for k in dm_keys(norm):
            cand.update(self.by_dm.get(k, ()))
        sk = skeleton(norm)
        if sk:
            cand.update(self.by_skel.get(sk, ()))
        if allowed is not None:
            cand &= allowed
        return cand

    def name_score(self, span_norm: str, e: Entry) -> float:
        ratio = fuzz.ratio(span_norm, e.norm) / 100
        wratio = fuzz.WRatio(span_norm, e.norm) / 100
        # WRatio over-rewards tiny spans matching inside long names.
        if len(span_norm) < 0.6 * len(e.norm):
            wratio *= 0.85
        sim = 0.5 * ratio + 0.5 * wratio
        q_dm = set(dm_keys(span_norm))
        key = 0.0
        if q_dm & set(e.dm):
            key += 1.0
        q_sk = skeleton(span_norm)
        if q_sk and q_sk == e.skel:
            key += 1.0
        if soundex(span_norm) == e.sdx:
            key += 0.5
        skel_sim = fuzz.ratio(q_sk, e.skel) / 100 if q_sk and e.skel else 0.0
        return 0.6 * sim + 0.25 * (key / 2.5) + 0.15 * skel_sim

    # ---------------------------------------------------------------- search
    def search(
        self,
        tokens: list[str],
        strength: float | None = None,
        strengths: list[float] | None = None,
        form: str | None = None,
        release: str | None = None,
        restrict_ids: set[int] | None = None,
        restrict_salts: set[str] | None = None,
        t1: float = 0.80,
        t2: float = 0.06,
        k: int = 3,
    ) -> MatchResult:
        if not tokens:
            return MatchResult(reason="no_tokens")
        allowed: set[int] | None = None
        if restrict_ids is not None or restrict_salts is not None:
            rid = restrict_ids or set()
            rs = restrict_salts or set()
            allowed = {
                i for i, e in enumerate(self.entries)
                if (e.brand_ids and rid.intersection(e.brand_ids)) or (e.salt and e.salt in rs)
            }

        spans: list[str] = []
        for i in range(len(tokens)):
            for size in (1, 2, 3):
                if i + size <= len(tokens):
                    span = normalize(" ".join(tokens[i : i + size]))
                    if len(span.replace(" ", "")) >= 3:
                        spans.append(span)

        best_entry: dict[int, tuple[float, str]] = {}
        for span in dict.fromkeys(spans):
            for idx in self._candidates(span, allowed):
                s = self.name_score(span, self.entries[idx])
                if idx not in best_entry or s > best_entry[idx][0]:
                    best_entry[idx] = (s, span)

        wanted = list(strengths or ([] if strength is None else [strength]))
        query_norm = normalize(" ".join(tokens))
        q_toks = set(query_norm.split())
        q_compact = query_norm.replace(" ", "")
        scored: dict[str, Scored] = {}
        for idx, (ns, span) in sorted(best_entry.items(), key=lambda kv: -kv[1][0])[:25]:
            if ns < 0.55:
                continue
            e = self.entries[idx]
            if e.salt and e.salt in self.weak_salts and ns < 0.8:
                continue
            if e.salt:
                key = f"s:{e.salt}"
                sc = Scored(key, None, e.salt, ns, 0.80 * ns + 0.10 + 0.05 + 0.03 * 0.5, span, e.norm)
                if key not in scored or sc.score > scored[key].score:
                    scored[key] = sc
                continue
            ids = e.brand_ids
            if restrict_ids is not None:
                ids = [i for i in ids if i in restrict_ids] or ids
            for bid in ids:
                b = self.store.brands[bid]
                nums = b.numbers()
                if wanted:
                    hit = any(any(abs(w - n) < 1e-6 for n in nums) for w in wanted)
                    # A number printed in the SKU name ("Glycomet SR 500") outranks one that only
                    # equals an ingredient amount (the metformin 500 inside "Glycomet GP 1").
                    label = {float(t) for t in b.label_tokens if t.replace(".", "", 1).isdigit()}
                    on_label = any(any(abs(w - n) < 1e-6 for n in label) for w in wanted)
                    st_score = 1.0 if on_label else (0.75 if hit else 0.0)
                else:
                    hit = None
                    st_score = 0.5
                fm = 0.5 if form is None else (1.0 if b.form == form else 0.0)
                rl = 0.5 if release is None else (1.0 if (b.release or "IR") == release else 0.0)
                total = 0.80 * ns + 0.12 * st_score + 0.05 * fm + 0.03 * rl
                if self._unspoken_words(b.base, q_toks, q_compact):
                    total -= EXTRA_WORD_PENALTY
                elif self._spoken_suffix(b.base, q_toks):
                    # "Zerodol SP" said: "Zerodol-SP" outranks plain "Zerodol" by the same margin.
                    total += EXTRA_WORD_PENALTY
                key = f"b:{bid}"
                sc = Scored(key, bid, None, ns, total, span, e.norm, hit,
                            fuzz.ratio(query_norm, normalize(b.name)) / 100)
                if key not in scored or sc.score > scored[key].score:
                    scored[key] = sc

        # Equal scores (sibling SKUs sharing one base name, e.g. "Glycomet 500" vs "Glycomet SR 500")
        # are ordered by how closely the whole query reads as the SKU name, never by row id.
        ranked = sorted(scored.values(), key=lambda s: (-round(s.score, 9), -s.tiebreak, s.key))
        ranked = self._dedupe_equivalent(ranked)
        result = MatchResult(candidates=ranked[: max(k, 5)])
        if not ranked:
            result.reason = "no_candidates"
            return result
        top = ranked[0]
        second = ranked[1].score if len(ranked) > 1 else 0.0
        if top.score >= t1 and top.score - second >= t2:
            result.confident = True
            result.reason = "accepted"
        elif top.score < t1:
            result.reason = "low_score"
        else:
            result.reason = "close_second"
        result.candidates = ranked[:k]
        return result

    @staticmethod
    def _unspoken_words(base: str, q_toks: set[str], q_compact: str) -> bool:
        """'Telma AZ' / 'Pan Up' / 'Glycomet GP' when the speaker said only 'Telma 40': the extra
        name word marks a different product (often another salt), so the plain SKU should win."""
        for t in base.split()[1:]:
            t = normalize(t.replace("-", ""))  # same spelling rules as the query ("lc" -> "lk")
            if not t or t.isdigit():
                continue
            if len(t) <= 2:
                if t not in q_toks:
                    return True
            elif t not in q_compact and fuzz.partial_ratio(t, q_compact) < 85:
                return True
        return False

    @staticmethod
    def _spoken_suffix(base: str, q_toks: set[str]) -> bool:
        """The brand name has a second word ('zerodol-sp', 'montair lc') that the speaker said."""
        words = [normalize(w) for w in re.split(r"[\s\-]+", base) if w]
        return len(words) > 1 and any(w in q_toks and not w.isdigit() for w in words[1:])

    def _dedupe_equivalent(self, ranked: list[Scored]) -> list[Scored]:
        """Collapse SKUs that are the same product listed twice (same base + composition)."""
        out: list[Scored] = []
        seen: set[tuple] = set()
        for s in ranked:
            if s.brand_id is not None:
                b = self.store.brands[s.brand_id]
                sig = (re.sub(r"[\s\-]+", " ", b.base).strip(), b.salt_key, b.strength_key, b.form, b.release)
            else:
                sig = ("salt", s.salt)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(s)
        return out
