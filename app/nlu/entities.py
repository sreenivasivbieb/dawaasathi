"""Rule-based entity and intent extraction from code-mixed transcripts.

No LLM: everything here is deterministic and unit-tested.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.match.translit import normalize
from app.nlu.numbers import is_number_token, parse_numbers

_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _nk(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = s.replace("़", "").replace("ँ", "ं")
    return unicodedata.normalize("NFC", s)


def _set(words: str) -> set[str]:
    return {_nk(w) for w in words.split()}


FILLERS = _set(
    "भैया भइया भाई भाईसाहब दीदी बहन जी सर मैडम अंकल आंटी मुझे मुझको मेरे मेरी मेरा हमें हमारे हमारी "
    "बताओ बताइए बताइये बताना बता बतायें बताएं बोलो दो दीजिए दीजिये देना दे चाहिए चाहिये का की के को "
    "है हैं था थी थे हो होगा होगी यह ये वह वो इस उस इसका इसकी इसके क्या कौन कौनसा कौनसी कोई सा सी से "
    "में पर ही भी और या तो कि जो ना न नहीं हां हाँ जरा ज़रा थोड़ा एक बार आप तुम मैं हम अपना अपनी लिए "
    "वाला वाली वाले लेना लेनी लेने ले लो खाना खाने खानी कर करो करें करना करने रहा रही रहे गया गई "
    "दवा दवाई दवाइयां दवाइयाँ दवाएं मेडिसिन मेडिसिन्स गोली गोलियां टैबलेट टेबलेट पत्ता पत्ती स्ट्रिप "
    "डॉक्टर डाक्टर साहब ने लिखा लिखी लिखे पर्चा पर्चे पर्ची प्रिस्क्रिप्शन केमिस्ट दुकान वाले "
    "bhaiya bhai didi ji sir madam mujhe mere meri mera batao bataiye bata bolo do dijiye chahiye "
    "ka ki ke ko hai hain tha yeh ye woh vo kya kaun koi sa si se me mein par hi bhi aur ya to ki "
    "please plz pls tell me my the a an of for is it this that what which give want need about "
    "medicine medicines tablet tablets tab strip dawa dawai doctor prescription chemist shop "
    "wala wali wale lena lo khana kar karo"
)

INTENT_WORDS = {
    "FIND_GENERIC": _set(
        "सस्ता सस्ती सस्ते सस्त जेनेरिक जनरिक जेनरिक जेनेरीक विकल्प ऑप्शन ओप्शन दूसरा दूसरी बदले "
        "जनऔषधि औषधि जन स्वस्त स्वस्तात पर्याय generic generics cheap cheaper cheapest sasta sasti "
        "alternative alternatives substitute option aushadhi janaushadhi"
    ),
    "PRICE_CHECK": _set(
        "कीमत क़ीमत दाम रेट भाव कितने कितना एमआरपी महंगा महंगी महँगा किंमत किमत "
        "price cost mrp rate expensive kitne kitna daam keemat"
    ),
    "DOSAGE_QUERY": _set(
        "खुराक डोज़ डोज कितनी बार कब खानी खाएं लेनी सुबह शाम रात दिन खाने पहले बाद "
        "dose dosage times when how many morning night daily before after food khurak"
    ),
    "VERIFY_MATCH": _set(
        "सही वही वहीं असली मिलान मैच मेल मिलती मिलता बराबर same correct right match verify check "
        "chek sahi wahi asli चेक जांच जाँच तपासा बरोबर"
    ),
}
_DOSAGE_PHRASES = [r"कितनी\s+बार", r"कब\s+(?:लेनी|खानी|लें|खाएं)", r"how\s+many\s+times", r"when\s+to\s+take",
                   r"दिन\s+में", r"खाने\s+(?:से\s+)?(?:पहले|बाद)"]

FORM_WORDS = {
    "tablet": _set("गोली गोलियां टैबलेट टेबलेट टेबलेट्स tablet tablets tab tabs goli"),
    "capsule": _set("कैप्सूल कैपसूल capsule capsules cap caps"),
    "oral_liquid": _set("सिरप शरबत सस्पेंशन syrup suspension sirap"),
    "drops": _set("ड्रॉप ड्रॉप्स ड्राप drops drop"),
    "injection": _set("इंजेक्शन सुई injection inj"),
    "cream": _set("क्रीम cream"),
    "ointment": _set("मरहम ointment"),
    "gel": _set("जेल gel"),
}
RELEASE_WORDS = {
    "SR": _set("sr एसआर एस.आर"),
    "ER": _set("er xr xl cr od ईआर एक्सआर एक्सएल सीआर ओडी"),
    "DR": _set("dr डीआर"),
}
UNIT_WORDS = {
    "mg": _set("mg एमजी मिलीग्राम मिग्रा milligram milligrams"),
    "mcg": _set("mcg माइक्रोग्राम microgram"),
    "g": _set("g gm gram ग्राम"),
    "ml": _set("ml एमएल मिलीलीटर मिली milliliter"),
    "iu": _set("iu आईयू"),
}
MODIFIERS = _set("duo ड्यूओ ड्युओ forte फोर्ट plus प्लस ds डीएस kid किड dt advance एडवांस")
# Kept as name tokens too: brand base names include them ("Crocin Advance"; see db.split_brand_name).
_NAME_MODIFIERS = _set("plus प्लस advance एडवांस")

YES = _set("हाँ हां हा जी हम्म ठीक सही बिलकुल बिल्कुल yes yeah yep haan han ha correct right ok okay होय हो")
NO = _set("नहीं नही ना नो मत गलत no nope nahi nahin galat नाही")
ORDINALS = {
    1: _set("पहला पहली पहले एक first one 1 पहिला"),
    2: _set("दूसरा दूसरी दूसरे दो second two 2 दुसरा"),
    3: _set("तीसरा तीसरी तीसरे तीन third three 3 तिसरा"),
}

# Symptom lexicon -> usual-use tags (matched against salts.usual_use).
SYMPTOMS: dict[str, set[str]] = {
    "fever": _set("बुखार बुख़ार ताप ज्वर ताप fever bukhar taap ताप"),
    "pain": _set("दर्द पीड़ा सरदर्द सिरदर्द बदनदर्द दुखणे वेदना pain ache headache dard"),
    "infection": _set("इन्फेक्शन संक्रमण पस infection infections"),
    "cold": _set("सर्दी जुकाम ज़ुकाम नज़ला सर्दी-जुकाम cold sardi zukam सर्दी"),
    "cough": _set("खांसी खाँसी खोकला cough khansi"),
    "allergy": _set("एलर्जी खुजली allergy itching kharish"),
    "diabetes": _set("शुगर डायबिटीज मधुमेह sugar diabetes"),
    "bp": _set("बीपी ब्लडप्रेशर रक्तचाप bp pressure hypertension"),
    "acidity": _set("एसिडिटी गैस जलन अपच acidity gas heartburn"),
    "thyroid": _set("थायराइड थाइराइड thyroid"),
    "cholesterol": _set("कोलेस्ट्रॉल कोलेस्ट्रोल cholesterol"),
    "asthma": _set("दमा अस्थमा सांस asthma breathing"),
    "diarrhoea": _set("दस्त लूज़ loose motion diarrhoea diarrhea"),
    "vomiting": _set("उल्टी उलटी vomiting vomit"),
    "vitamin": _set("कमजोरी कमज़ोरी विटामिन weakness vitamin"),
}


_AMBIGUOUS_NUM = _set("दो एक सो ek")


@dataclass
class Entities:
    text: str
    tokens: list[str]
    name_tokens: list[str]
    strength: float | None = None
    strengths: list[float] = field(default_factory=list)
    unit: str | None = None
    form: str | None = None
    release: str | None = None
    modifiers: list[str] = field(default_factory=list)
    intent: str = "CLARIFY"
    symptom: str | None = None
    yes: bool = False
    no: bool = False
    ordinal: int | None = None


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).translate(_DEV_DIGITS)
    text = re.sub(r"(\d)([^\d\s.,])", r"\1 \2", text)
    text = re.sub(r"([^\d\s.,])(\d)", r"\1 \2", text)
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = "".join(
        ch if unicodedata.category(ch)[0] in "LMN" or ch in " .-/" else " " for ch in text
    )
    text = text.replace("/", " / ")
    toks = [t.strip(".-") for t in text.split()]
    return [t for t in toks if t]


def _lookup(tok: str, table: dict) -> str | None:
    n = _nk(tok)
    for key, words in table.items():
        if n in words:
            return key
    return None


def extract(text: str) -> Entities:
    tokens = tokenize(text)
    ents = Entities(text=text, tokens=tokens, name_tokens=[])
    spans = parse_numbers(tokens)
    number_idx = {i for s in spans for i in range(s.start, s.end)}
    values = [s.value for s in spans]

    for i, tok in enumerate(tokens):
        n = _nk(tok)
        unit = _lookup(tok, UNIT_WORDS)
        if unit and ents.unit is None and (i - 1 in number_idx or i == 0):
            ents.unit = unit
        form = _lookup(tok, FORM_WORDS)
        if form and ents.form is None:
            ents.form = form
        rel = _lookup(tok, RELEASE_WORDS)
        if rel and ents.release is None:
            ents.release = rel
        if n in MODIFIERS:
            ents.modifiers.append(normalize(tok))
        if n in YES:
            ents.yes = True
        if n in NO:
            ents.no = True
        for sym, words in SYMPTOMS.items():
            if n in words and ents.symptom is None:
                ents.symptom = sym

    # Strengths: plausible numbers (not counts like "2 गोली" or "10 दिन").
    unit_words = set().union(*UNIT_WORDS.values())
    for s in spans:
        nxt = _nk(tokens[s.end]) if s.end < len(tokens) else ""
        if nxt in _set("दिन बार दिनों days day times बार गोली goli"):
            continue
        if s.end - s.start == 1 and _nk(tokens[s.start]) in _AMBIGUOUS_NUM and nxt not in unit_words:
            continue  # "बता दो" is "tell me", not 2 mg
        if s.value >= 2 or (s.value > 0 and not float(s.value).is_integer()):
            ents.strengths.append(s.value)
    if ents.strengths:
        ents.strength = ents.strengths[0]

    # Ordinal answers ("दूसरा") for confirmation turns: only when the utterance is short.
    # An ordinal word ("second") beats a bare numeral ("one") in "second one".
    if len(tokens) <= 3:
        hits = [(is_number_token(t), k) for t in tokens for k, words in ORDINALS.items() if _nk(t) in words]
        if hits:
            ents.ordinal = min(hits, key=lambda h: h[0])[1]

    stop = set(FILLERS)
    for words in INTENT_WORDS.values():
        stop |= words
    for table in (FORM_WORDS, RELEASE_WORDS, UNIT_WORDS):
        for words in table.values():
            stop |= words
    for words in SYMPTOMS.values():
        stop |= words
    stop |= YES | NO | (MODIFIERS - _NAME_MODIFIERS)
    for words in ORDINALS.values():  # "पहला" / "first" answer a confirmation; never a drug name
        stop |= words
    for i, tok in enumerate(tokens):
        if i in number_idx or is_number_token(tok):
            continue
        if _nk(tok) in stop:
            continue
        if len(normalize(tok).replace(" ", "")) < 2:
            continue
        ents.name_tokens.append(tok)

    ents.intent = classify_intent(text, tokens, ents)
    return ents


def classify_intent(text: str, tokens: list[str], ents: Entities | None = None) -> str:
    norm = [_nk(t) for t in tokens]
    lowered = _nk(text)
    if any(re.search(p, lowered) for p in _DOSAGE_PHRASES):
        return "DOSAGE_QUERY"
    scores = {intent: sum(1 for t in norm if t in words) for intent, words in INTENT_WORDS.items()}
    # Dosage words like "दिन"/"बाद" are weak on their own; require two hits.
    if scores["DOSAGE_QUERY"] < 2:
        scores["DOSAGE_QUERY"] = 0
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] > 0:
        # Prefer explicit generic/price asks over verify when tied.
        order = ["FIND_GENERIC", "PRICE_CHECK", "VERIFY_MATCH", "DOSAGE_QUERY"]
        top = max(scores.values())
        for intent in order:
            if scores[intent] == top:
                return intent
    if ents is not None and ents.name_tokens:
        return "FIND_GENERIC"
    return "CLARIFY"
