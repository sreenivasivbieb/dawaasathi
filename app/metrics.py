"""In-process metrics: per-stage timings (p50/p95), counters, match confidence. No audio, no PII."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque

log = logging.getLogger("dawasaathi")


class Metrics:
    def __init__(self, window: int = 2000):
        self.timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self.counters: dict[str, int] = defaultdict(int)
        self.confidences: deque[float] = deque(maxlen=window)
        self.started = time.time()

    def observe(self, stage: str, ms: float) -> None:
        self.timings[stage].append(ms)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def confidence(self, value: float) -> None:
        self.confidences.append(value)

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
        return round(s[idx], 1)

    def snapshot(self) -> dict:
        hits = self.counters.get("tts_cache_hit", 0)
        misses = self.counters.get("tts_cache_miss", 0)
        conf = list(self.confidences)
        return {
            "uptime_s": int(time.time() - self.started),
            "stages": {
                k: {"n": len(v), "p50_ms": self._pct(list(v), 50), "p95_ms": self._pct(list(v), 95)}
                for k, v in sorted(self.timings.items())
            },
            "counters": dict(sorted(self.counters.items())),
            "tts_cache_hit_rate": round(hits / (hits + misses), 3) if hits + misses else None,
            "match_confidence": {
                "n": len(conf), "p50": self._pct(conf, 50), "p10": self._pct(conf, 10),
            },
        }


METRICS = Metrics()


def log_event(event: str, **fields) -> None:
    """Structured log line: timings and ids only (never audio, images or transcripts)."""
    log.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))
