"""Tavily web search (https://api.tavily.com/search), used only to look up a salt's usual use
when our own data has none (see app/engine/web_uses.py). Key: TAVILY_API_KEY."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

URL = "https://api.tavily.com/search"


class TavilyError(Exception):
    pass


@dataclass
class SearchHit:
    url: str
    title: str
    content: str
    score: float


class Tavily:
    def __init__(self, api_key: str | None, client: httpx.AsyncClient, timeout_s: float = 8.0):
        self.api_key = api_key
        self.client = client
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, domains: list[str], max_results: int = 5,
                     depth: str = "advanced") -> tuple[list[SearchHit], float]:
        if not self.api_key:
            raise TavilyError("TAVILY_API_KEY not set")
        body = {"query": query, "search_depth": depth, "max_results": max_results,
                "include_domains": domains, "include_answer": False}
        t0 = time.perf_counter()
        try:
            resp = await self.client.post(URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"},
                                          timeout=httpx.Timeout(self.timeout_s, connect=4.0))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TavilyError(f"network: {type(exc).__name__}") from exc
        ms = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            raise TavilyError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        hits = [SearchHit(r.get("url", ""), r.get("title", ""), r.get("content", "") or "", float(r.get("score") or 0))
                for r in resp.json().get("results", [])]
        return hits, round(ms, 1)
