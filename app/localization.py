"""Render stored result facts in another language, without another model or OCR request."""
from app.dialogue import guard
from app.dialogue.templates import RenderContext, render
from app.dialogue.verbalize import speakable
from app.models import DrugItem, PriceInfo, QueryResponse, SafetyResult, Script, Verdict


def localize_response(data: dict, lang: str) -> dict:
    response = QueryResponse.model_validate(data)
    def item(key):
        value = response.items.get(key)
        return DrugItem.model_validate(value) if value else None
    prescribed, dispensed, focus = item("prescribed"), item("dispensed"), item("focus")
    target = dispensed or focus or prescribed
    kind = (response.card.title or "").removeprefix("GUARD_").lower() if response.card else ""
    kind = kind if kind in {"injection", "self_harm", "emergency", "misuse"} else "injection"
    rx_candidates = any(c.source == "rx" for c in response.candidates)
    code = (response.data_sources.get("error") or {}).get("code", "")
    context = RenderContext(
        lang=lang, status="greeting" if response.card and response.card.title == "GREETING" else response.status, intent=response.intent,
        guard_kind=kind if response.status == "blocked" else None,
        verdict=Verdict.model_validate(response.verdict) if response.verdict else None,
        item=target, prescribed=prescribed, dispensed=dispensed,
        safety=SafetyResult.model_validate(response.safety) if response.safety else None,
        price=PriceInfo.model_validate(response.price) if response.price else None,
        alternatives=response.alternatives, flags=response.flags,
        candidates=[] if rx_candidates else response.candidates,
        rx_count=len(response.candidates) if rx_candidates else 0,
        directions=(prescribed or focus).directions if prescribed or focus else None,
        error_kind="stt" if code.startswith("STT") else None,
        skip_detail=response.intent == "USE_QUERY" or any(f.code.startswith("SYMPTOM_") for f in response.flags),
    )
    rendered = render(context)
    sources = [data, {"directions": context.directions, "helplines": [14416, 112, 108]}]
    text = rendered.text if guard.validate(rendered.text, *sources).ok else " ".join(s.text for s in rendered.plain_segments)
    chemist = rendered.chemist_text if guard.validate(rendered.chemist_text, *sources).ok else ""
    response.lang = lang
    response.script = Script(lang=lang, text=text, chemist_text=chemist, speech_text=speakable(text, lang))
    # Stored audio belongs to the original language and may have expired.
    response.tts = "none"
    response.audio_id = response.audio_url = response.audio_stream_url = None
    return response.model_dump(mode="json")
