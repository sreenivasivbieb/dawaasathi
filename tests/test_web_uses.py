"""Web usual-use lookup: only answers grounded in a trusted source survive."""

from __future__ import annotations

import asyncio

from app.adapters.llm import LLMResult
from app.adapters.tavily import SearchHit
from app.engine import checks
from app.engine.salts import parse_composition
from app.engine.web_uses import WebUseLookup
from app.models import DrugItem

PAGE = ("Ascoril LS Syrup is a combination of three medicines: ambroxol, levosalbutamol, and guaifenesin. "
        "It is used in the treatment of cough with mucus. It thins and loosens mucus in the airways.")


class FakeTavily:
    available = True

    def __init__(self, hits):
        self.hits = hits
        self.calls = 0

    async def search(self, query, domains, max_results=5, depth="advanced"):
        self.calls += 1
        return self.hits, 100.0


class FakeLLM:
    available = True

    def __init__(self, data):
        self.data = data

    async def chat_json(self, system, user, max_tokens=400):
        return LLMResult(dict(self.data), 50.0, "fake")


def _item():
    return DrugItem(source="voice", salts=parse_composition("ambroxol 30mg + levosalbutamol 1mg"), form="syrup")


def _run(lookup):
    return asyncio.run(lookup.lookup(_item()))


def test_grounded_answer_is_kept_and_cached(tmp_path):
    tav = FakeTavily([SearchHit("https://www.1mg.com/drugs/ascoril-ls-syrup-63412", "Ascoril LS", PAGE, 0.8)])
    llm = FakeLLM({"uses": ["cough", "fever"], "source": 1,
                   "evidence": "It is used in the treatment of cough with mucus."})
    lookup = WebUseLookup(tav, llm, tmp_path / "web.json")
    res = _run(lookup)
    assert res is not None and res.uses == ["cough"]  # "fever" is not in the evidence sentence
    assert res.source == "1mg" and res.url.startswith("https://www.1mg.com/")
    again = WebUseLookup(tav, llm, tmp_path / "web.json")  # reloads the cache file
    assert _run(again).cached and tav.calls == 1


def test_evidence_not_in_source_is_rejected(tmp_path):
    tav = FakeTavily([SearchHit("https://www.1mg.com/x", "Ascoril LS", PAGE, 0.8)])
    llm = FakeLLM({"uses": ["cough"], "source": 1, "evidence": "Ambroxol is used for cough in adults."})
    assert _run(WebUseLookup(tav, llm, tmp_path / "w.json")) is None


def test_untrusted_or_unrelated_source_is_ignored(tmp_path):
    hits = [SearchHit("https://random-blog.example/ascoril", "Ascoril", PAGE, 0.9),
            SearchHit("https://www.drugs.com/paracetamol.html", "Paracetamol",
                      "Paracetamol is used to treat pain and fever.", 0.5)]
    llm = FakeLLM({"uses": ["fever"], "source": 1, "evidence": "Paracetamol is used to treat pain and fever."})
    tav = FakeTavily(hits)
    assert _run(WebUseLookup(tav, llm, tmp_path / "w.json")) is None


def test_web_flags_name_the_source():
    f = checks.symptom_flag("fever", ["cough"], {"source": "1mg", "url": "https://www.1mg.com/x"})
    assert f.code == "SYMPTOM_MISMATCH_WEB" and f.facts["source"] == "1mg"
    assert checks.symptom_flag("cough", ["cough"], None).code == "SYMPTOM_OK"
    assert checks.use_flag(None).code == "USE_UNKNOWN"
    assert checks.use_flag(["cough"], {"source": "1mg"}).code == "USE_INFO_WEB"
