"""Guardrails on what comes in (the user's question) and what goes out (the response).

Input (`check_query`), run on the transcript / typed text BEFORE any LLM sees it:
  * injection   - "ignore previous instructions", "system prompt batao", API keys / passwords,
                  other users' data or history, database / logs. Answered with a fixed refusal.
  * self_harm   - wanting to die / how many pills kill. Answered with the Tele-MANAS helpline.
  * emergency   - someone already took too many pills / poison. Answered with 108 / hospital.
  * misuse      - getting high, fake prescriptions, dosing someone without their knowledge.
A matched query never reaches the LLM, the web lookup or the matcher; only the kind is logged.

Output:
  * `public_response` drops internal debug data (raw LLM output, provider errors, OCR internals)
    unless EXPOSE_DEBUG=1, and redacts anything shaped like a secret everywhere in the response.
  * `safe_label` cleans text read off a photo before it is spoken (a pack printed with
    "call 98xxxxxx" or "ignore your rules" must not be read out as a brand).
  * `unsafe_reply_text` is an extra check on LLM-rewritten replies: no links, emails, phone numbers
    or talk about prompts / keys.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- input normalisation
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_input(text: str | None, max_len: int = 500) -> str | None:
    """Text passed on to the pipeline: NFC, no zero-width / bidi / control characters."""
    if text is None:
        return None
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub(" ", _INVISIBLE.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()[:max_len]
    return text or None


def _match_form(text: str) -> tuple[str, str]:
    """(spaced, squashed) lowercase copies used only for matching: compatibility forms folded
    ("ｉｇｎｏｒｅ"), nukta dropped (ज़ -> ज), punctuation spaced; squashed removes all separators
    so "s y s t e m  p r o m p t" or "a.p.i k.e.y" still match."""
    t = unicodedata.normalize("NFKC", _INVISIBLE.sub("", text)).lower()
    t = unicodedata.normalize("NFD", t).replace("़", "")
    t = unicodedata.normalize("NFC", t)
    spaced = re.sub(r"[^\wऀ-ॿ]+", " ", t).strip()
    squashed = re.sub(r"[^a-z0-9ऀ-ॿ]+", "", t)
    return spaced, squashed


# ---------------------------------------------------------------- input patterns
def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts))


_INJECTION = _rx(
    # override / role play
    # ("if I forget the previous dose" is a real question: only instructions / rules / prompt count)
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?)\b",
    r"\b(ignore|disregard)\s+(all|everything)\b",
    r"\b(you are now|act as|pretend (to be|you are)|role ?play|jail ?break|dan mode|developer mode|god mode)\b",
    r"\b(pichl[ae]|upar wal[ae]|sar[ae]) .{0,20}(instruction|nirdesh|rules?).{0,20}(bhool|bhul|ignore|chhod)",
    r"(इग्नोर|भूल जाओ|भूल जाइए|अनदेखा).{0,25}(निर्देश|इंस्ट्रक्शन|नियम|रूल)",
    r"(निर्देश|इंस्ट्रक्शन|नियम|रूल).{0,25}(इग्नोर|भूल जाओ|भूल जाइए|अनदेखा)",
    # internals / secrets
    r"\b(system|developer|hidden|initial|original)\s+(prompt|instructions?|message)\b",
    r"\b(your|apna|apne|tumhar[ae]|aapk[ae])\s+(prompt|instructions?|rules|config|configuration|source code)\b",
    r"\b(api|secret|access|auth)\s*(key|token)s?\b|\bpasswords?\b|\bcredentials?\b|\.env\b|\benv(ironment)? var",
    r"(सिस्टम|डेवलपर)\s*(प्रॉम्प्ट|प्रोम्प्ट|प्राम्प्ट)|(प्रॉम्प्ट|प्रोम्प्ट)\s*(बताओ|दिखाओ|बताइए)",
    r"(एपीआई|एपिआई)\s*(की|के|key|कुंजी)|पासवर्ड|टोकन|सीक्रेट",
    # other people's data / the backend
    r"\b(other|another|previous|all|dusr[aeo]|baaki|sab(hi)?|pichl[ae])\s+(users?|people|patients?|customers?|"
    r"log(on)?|logo|mariz|marij)\b.{0,30}\b(data|history|queries|questions|sawal|details|info|jaankari|"
    r"jankari|number|phone|records?|chats?|messages?)\b",
    r"\b(data ?base|sql|server logs?|logs (dikhao|batao|show)|session (id|data)|dump)\b",
    r"\bkisne\b.{0,20}\b(poocha|pucha|search kiya)\b",
    r"(दूसरे|बाकी|सभी|सारे|पिछले)\s*(यूज़र|यूजर|लोगों|लोग|मरीज़|मरीजों|मरीज|ग्राहक).{0,30}"
    r"(डेटा|डाटा|जानकारी|सवाल|हिस्ट्री|नंबर|रिकॉर्ड)",
    r"डेटाबेस|डाटाबेस|किसने .{0,15}पूछा",
)
_INJECTION_SQUASHED = ("systemprompt", "ignorepreviousinstructions", "ignoreallinstructions", "apikey",
                       "jailbreak", "developermode", "promptinjection", "सिस्टमप्रॉम्प्ट")

_SELF_HARM = _rx(
    r"\b(suicide|suicidal|kill (my ?self|me)|end (my|this) life|want to die|wanna die|don'?t want to live|"
    r"self ?harm|hurt myself)\b",
    r"\b(khud ?kushi|khudkhushi|aatma? ?hatya|atma? ?hatya)\b",
    r"\b(marna|mar jana|mar jaun|mar jaunga|mar jaungi)\s+(hai|chahta|chahti|chahiye)\b",
    r"\b(jeena|jina) (nahi|nahin) (hai|chahta|chahti)\b|\bjaan de (dunga|dungi|du|doon)\b|zindagi khatam",
    r"\b(how many|kitni|kitne)\b.{0,30}\b(die|death|maut|mar jaye|mar jaate|mar jaunga|marne|kill)\b",
    r"\b(lethal|fatal|deadly) (dose|amount)\b",
    r"आत्महत्या|खुदकुशी|ख़ुदकुशी|मरना (है|चाहता|चाहती)|मर जाना चाहता|मर जाना चाहती|जीना नहीं (है|चाहता|चाहती)"
    r"|जान दे (दूंगा|दूंगी|दूँ|दूं)|ज़िंदगी खत्म|जिंदगी खत्म|जानलेवा (खुराक|डोज)",
    r"(कितनी|कितने).{0,25}(मौत|मर जा|मरने|मर जाऊं|मर जाऊँ)",
    r"मरायचे आहे|जीव द्यायचा|आत्महत्या करायची",
)

_EMERGENCY = _rx(
    r"\b(took|taken|swallowed|ate|had)\b.{0,25}\b(too many|lots of|whole (strip|bottle)|overdose|poison)\b",
    r"\boverdose\b.{0,20}\b(ho gaya|ho gayi|kar liya|kar li|le liya|le li|happened|taken)\b",
    r"\b(zyada|jyada|bahut saari|bahut sari|poora patta|puri strip)\b.{0,25}\b(goli|golia|goliyan|tablet|dawa|"
    r"dawai)\b.{0,15}\b(kha li|kha liya|kha gaya|kha gayi|le li|le liya|nigal)",
    r"\b(galti se|bachche ne|bacche ne)\b.{0,30}\b(kha li|kha liya|kha gaya|kha gayi|pee li|pi li|nigal)",
    r"\b(zeher|zehar|jahar|poison)\b.{0,15}\b(kha|pee|pi|piya|liya|li)\b",
    r"(ज़्यादा|ज्यादा|बहुत सारी|पूरा पत्ता|पूरी स्ट्रिप).{0,25}(गोली|गोलियां|गोलियाँ|दवा|टैबलेट).{0,15}"
    r"(खा ली|खा लीं|खा लिया|खा गया|खा गई|निगल)",
    r"(गलती से|बच्चे ने).{0,30}(खा ली|खा लिया|खा गया|खा गई|पी ली|पी लिया|निगल)",
    r"(ज़हर|जहर).{0,15}(खा|पी)|ओवरडोज़? (हो गया|हो गई|कर लिया|ले लिया)",
)

_MISUSE = _rx(
    r"\b(get(ting)? high|recreational|for (a )?kick|trip(ping)? on|to get stoned)\b",
    # "is dawa se nasha hota hai?" (does it make me drowsy) is a side-effect question: only intent counts
    r"\b(nash[ae]|nashey) (ke liye|karne|karna|chahiye|wali|wala|wale)\b|\b(nasheeli|nashili) (goli|dawa|dawai)\b",
    r"\b(fake|nakli|naqli|farzi|forged?)\s+(prescription|parchi|parchee|parcha|rx)\b",
    r"\b(prescription|parchi|parcha)\b.{0,15}\b(kaise banaye|kaise banau|forge|bana do|bana ke do)\b",
    # Giving a medicine covertly / to knock someone out. (Mixing a child's medicine in food, openly,
    # is a normal question and is not matched.)
    r"\b(without (him|her|them|their|his) knowing|bina bataye|bina batae|chupke se)\b.{0,40}"
    r"\b(dawa|dawai|goli|tablet|medicine|khila|pila|de do|dena|mila)\b",
    r"\b(behosh|unconscious)\s+(karne|karna|kar\s*(de|do|du|doon))\b",
    r"नशे (के लिए|वाली|वाला)|नशा (करने|करना|चाहिए)|नशीली (गोली|दवा)|नकली (पर्ची|प्रिस्क्रिप्शन)"
    r"|फर्जी (पर्ची|प्रिस्क्रिप्शन)",
    r"(बिना बताए|चुपके से).{0,40}(दवा|गोली|टैबलेट).{0,15}(खिला|पिला|दे|मिला)|बेहोश (करने|करना|कर दे)",
)


@dataclass
class QueryGuard:
    kind: str  # "ok" | "injection" | "self_harm" | "emergency" | "misuse"

    @property
    def blocked(self) -> bool:
        return self.kind != "ok"


def check_query(text: str | None) -> QueryGuard:
    """Classify one user utterance. Order matters: someone in danger gets help first."""
    if not text:
        return QueryGuard("ok")
    spaced, squashed = _match_form(text)
    if _EMERGENCY.search(spaced):
        return QueryGuard("emergency")
    if _SELF_HARM.search(spaced):
        return QueryGuard("self_harm")
    if _MISUSE.search(spaced):
        return QueryGuard("misuse")
    if _INJECTION.search(spaced) or any(k in squashed for k in _INJECTION_SQUASHED):
        return QueryGuard("injection")
    return QueryGuard("ok")


# ---------------------------------------------------------------- output: text read off a photo
_URLISH = re.compile(r"https?://|www\.|\.(com|in|org|net|io)\b|@", re.I)
_PHONEISH = re.compile(r"(?:\+?\d[\s\-]?){8,}")  # 8+ digits: a phone number, not "1-0-1"
_LABEL_OK = re.compile(r"^[A-Za-z0-9ऀ-ॿ][A-Za-z0-9ऀ-ॿ .,:;+\-/&()%']*$")


def safe_label(text: str | None, max_len: int = 40, max_words: int = 5) -> str | None:
    """A brand / salt name read off a pack, or None when it does not look like one."""
    if not text:
        return None
    t = re.sub(r"[™®©\"“”]", " ", clean_input(text, 300) or "")
    t = re.sub(r"\s+", " ", t).strip()
    if (len(t) > max_len or len(t.split()) > max_words or not _LABEL_OK.match(t) or _URLISH.search(t)
            or _PHONEISH.search(t) or check_query(t).blocked):
        return None
    return t


# ---------------------------------------------------------------- output: LLM-rewritten replies
_REPLY_BAD = re.compile(
    r"https?://|www\.|\S+@\S+\.\w+|\b(prompt|instructions?|api|token|password|system message|as an ai|"
    r"language model|openai|groq|llama|gpt)\b|प्रॉम्प्ट|निर्देशों के अनुसार", re.I)


def unsafe_reply_text(text: str) -> list[str]:
    hits = sorted({m.group(0).lower() for m in _REPLY_BAD.finditer(text)})
    if _PHONEISH.search(text):
        hits.append("phone-like number")
    return hits


# ---------------------------------------------------------------- output: response JSON
_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),  # Groq
    re.compile(r"tvly-[A-Za-z0-9\-_]{10,}"),  # Tavily
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),  # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.=]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|key|token|secret|password|authorization)\s*[=:]\s*\S+"),
]
_SECRET_ENV = ("GNANI_API_KEY", "GROQ_API_KEY", "GROQ_API_KEYS", "TAVILY_API_KEY", "ANTHROPIC_API_KEY",
               "ANTHROPIC_AUTH_TOKEN", "WHATSAPP_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")


def known_secrets(settings: Any = None) -> list[str]:
    vals: list[str] = []
    for name in _SECRET_ENV:
        for v in (os.environ.get(name) or "").split(","):
            if len(v.strip()) >= 8:
                vals.append(v.strip())
    if settings is not None:
        for attr in ("gnani_api_key", "tavily_api_key"):
            v = getattr(settings, attr, None)
            if v and len(v) >= 8:
                vals.append(v)
        vals += [k for k in getattr(settings, "groq_api_keys", ()) or () if k and len(k) >= 8]
    return sorted(set(vals), key=len, reverse=True)


def redact(obj: Any, secrets: list[str]) -> Any:
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, "[REDACTED]")
        for pat in _SECRET_PATTERNS:
            obj = pat.sub("[REDACTED]", obj)
        return obj
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    return obj


# Keys under data_sources that are user-facing provenance (kept); everything else is debug.
_PUBLIC_SOURCES = ("brands", "banned_fdc", "price_ref", "nsq_alerts")


def public_response(data: dict, settings: Any = None, debug: bool = False) -> dict:
    """The response as sent to a client: debug internals removed unless `debug`, secrets redacted."""
    data = dict(data)
    if not debug:
        ds = data.get("data_sources") or {}
        public = {k: ds[k] for k in _PUBLIC_SOURCES if k in ds}
        web = (ds.get("llm") or {}).get("web_uses")
        if web and web.get("found"):  # the reply names this source, so the client may show it
            public["web_uses"] = {"source": web.get("source"), "url": web.get("url"),
                                  "fetched_at": web.get("fetched_at")}
        if "error" in ds:
            public["error"] = {"code": (ds["error"] or {}).get("code")}
        data["data_sources"] = public
        items = data.get("items")
        if isinstance(items, dict):
            data["items"] = {k: ({**v, "raw_text": None} if isinstance(v, dict) else v) for k, v in items.items()}
    return redact(data, known_secrets(settings))
