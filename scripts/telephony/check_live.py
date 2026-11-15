"""Opt-in real-provider probe with synthetic speech, NOT a real SIP call.

Uses Gnani credits. Run only after both real echo gates pass and callers are idle:
  .venv/bin/python -m scripts.telephony.check_live --live
"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

from app.adapters.gnani_tts import TimbreTTS
from app.audio_io import pcm_to_wav
from app.config import get_settings
from app.telephony.audio import resample

QUESTIONS = {
    'en': ('What is the price of Dolo six hundred fifty?', 'Is there a cheaper option?'),
    'hi': ('डोलो छह सौ पचास की कीमत क्या है?', 'इसका सस्ता विकल्प है?'),
}


async def main(args):
    settings = get_settings()
    if not settings.gnani_api_key:
        raise SystemExit('Gnani key is not configured')
    evidence = {'kind':'synthetic speech through live providers; not SIP/human validation', 'checks':[]}
    async with httpx.AsyncClient(timeout=45, trust_env=False) as provider, httpx.AsyncClient(
        base_url=args.backend, timeout=60, trust_env=False,
    ) as backend:
        tts = TimbreTTS(settings, provider)
        for lang in args.lang:
            text, followup = QUESTIONS[lang]
            pcm = await tts.synthesize(text, lang, 8000)
            wav = pcm_to_wav(await asyncio.to_thread(resample, pcm, 8000, 16000), 16000)
            r = await backend.post('/v1/query',data={'channel':'phone','lang':lang,'detect_language':'true'},
                                   files={'audio':('probe.wav',wav,'audio/wav')})
            r.raise_for_status()
            answer = r.json()
            audio = await backend.get(answer['audio_stream_url'])
            audio.raise_for_status()
            check = {'requested_lang':lang,'reply_lang':answer['lang'],'transcript':answer['transcript'],
                     'status':answer['status'],'brand':(answer.get('match') or {}).get('brand'),
                     'sample_rate':audio.headers.get('X-Sample-Rate'),'tts':audio.headers.get('X-TTS'),
                     'audio_bytes':len(audio.content)}
            r = await backend.post('/v1/query',data={'channel':'phone','lang':answer['lang'],
                'text':followup,'session_id':answer['session_id']})
            r.raise_for_status()
            follow = r.json()
            check['followup_same_session'] = follow['session_id'] == answer['session_id']
            check['followup_lang'] = follow['lang']
            check['passed'] = (check['reply_lang'] == lang and check['sample_rate'] == '8000'
                and check['tts'] == 'timbre' and check['audio_bytes'] > 320
                and check['followup_same_session'] and check['followup_lang'] == lang
                and bool(check['brand']) and 'dolo' in check['brand'].lower())
            # Drain follow-up TTS before another live request on the shared key.
            audio = await backend.get(follow['audio_stream_url'])
            audio.raise_for_status()
            evidence['checks'].append(check)
            print(json.dumps(check,ensure_ascii=False),flush=True)
    Path(args.output).write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
    if not all(x['passed'] for x in evidence['checks']):
        raise SystemExit('One or more live checks failed; inspect evidence')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Authorize live speech-provider requests using credits')
    parser.add_argument('--lang',choices=list(QUESTIONS),nargs='+',default=list(QUESTIONS))
    parser.add_argument('--backend',default='http://127.0.0.1:8004')
    parser.add_argument('--output',default='var/telephony/live-provider-check.json')
    args=parser.parse_args()
    if not args.live:
        parser.error('Pass --live to explicitly enable provider requests')
    asyncio.run(main(args))
