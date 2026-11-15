"""Explicit single-question demo: greeting -> caller speech -> cached Dolo reply.

No speech recognition or live medicine query occurs during a demo call. Any
completed spoken utterance triggers the same prepared response. Use only for the
specified demonstration, not as a general medicine assistant.
"""
import argparse
import asyncio
from contextlib import suppress
import json
import logging
from pathlib import Path
import struct
import math

import httpx

from app.adapters.gnani_tts import TimbreTTS
from app.config import get_settings
from .audio import Segmenter
from .echo import EchoServer
from .protocol import Message, DTMF, HANGUP, ERROR, ProtocolError, read_message

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / 'var/telephony/demo'
TEXT = {
    'en': {
        'greeting': "Welcome to the Dawaa Saathi medicine information demo. Please ask your question after the beep.",
        'answer': "An alternative listed for Dolo six hundred fifty is Jan Aushadhi Paracetamol Tablets, six hundred fifty milligrams. Please ask your doctor or pharmacist to confirm the ingredient, strength and tablet formulation before switching. Thank you for calling Dawaa Saathi.",
    },
    'hi': {
        'greeting': "दवा साथी के इस डेमो में आपका स्वागत है। बीप के बाद अपना सवाल पूछें।",
        'answer': "डोलो छह सौ पचास के लिए सूची में जन औषधि पैरासिटामोल छह सौ पचास मिलीग्राम की टैबलेट एक विकल्प है। दवा बदलने से पहले डॉक्टर या फार्मासिस्ट से दवा के घटक, मात्रा और टैबलेट के प्रकार की पुष्टि करें। दवा साथी को कॉल करने के लिए धन्यवाद।",
    },
}


def load_audio(directory):
    result = {}
    for lang in TEXT:
        result[lang] = {}
        for kind in TEXT[lang]:
            data = (directory / f'{lang}-{kind}.pcm').read_bytes()
            if not data or len(data) % 2 or len(data) > 8000 * 2 * 60:
                raise ValueError('Invalid demo audio; run --prepare again')
            result[lang][kind] = data
    return result


async def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = get_settings()
    if not settings.gnani_api_key:
        raise RuntimeError('Gnani is required once to prepare real speech')
    async with httpx.AsyncClient(trust_env=False) as client:
        tts = TimbreTTS(settings, client)
        for lang, items in TEXT.items():
            for kind, text in items.items():
                pcm = await tts.synthesize(text, lang, 8000)
                if not pcm or len(pcm) % 2 or not any(pcm):
                    raise ValueError('Speech provider returned invalid/silent demo audio')
                temporary = directory / f'{lang}-{kind}.tmp'
                temporary.write_bytes(pcm)
                temporary.chmod(0o600)
                temporary.replace(directory / f'{lang}-{kind}.pcm')
                print(f'Prepared {lang} {kind}: {len(pcm)} PCM bytes', flush=True)
    load_audio(directory)
    (directory / 'manifest.json').write_text(json.dumps({
        'mode':'single-question-demo', 'question':'What is the alternate of Dolo 650?',
        'sample_rate':8000,'format':'s16le mono','texts':TEXT,
        'source':'data1 Jan Aushadhi product list, drug code 511; confirmed through existing backend',
        'recognition':'none; a spoken utterance or DTMF 1 triggers the fixed response',
    }, ensure_ascii=False, indent=2)+'\n')


class DemoServer(EchoServer):
    mode = 'single-question-demo'
    def __init__(self, audio, lang='en', threshold=350, **kwargs):
        super().__init__(**kwargs)
        self.audio, self.lang, self.threshold = audio, lang, threshold

    async def exchange(self, call, reader, writer):
        events = asyncio.Queue(maxsize=1)
        listening = False
        ready_after = 0
        vad = Segmenter(self.threshold, max_seconds=8)
        clock = asyncio.get_running_loop()

        async def send(pcm):
            deadline = clock.time()
            for offset in range(0,len(pcm),320):
                frame = pcm[offset:offset+320]
                await asyncio.sleep(max(0,deadline-clock.time()))
                writer.write(Message(0x10,frame).encode())
                await asyncio.wait_for(writer.drain(),2)
                call.sent += len(frame)
                deadline = max(deadline,clock.time()) + len(frame)/16000
            await asyncio.sleep(max(0,deadline-clock.time()))

        async def receive():
            while True:
                # A quiet caller must not truncate greeting/answer playback.
                # The conversation has a 30s question timeout, and the enclosing
                # transport enforces the absolute call timeout.
                message = await read_message(reader,1600)
                if message is None or message.kind in (HANGUP, ERROR):
                    return
                if message.kind == DTMF:
                    if message.payload == b'#': return
                    event = {b'1':'answer',b'7':'hi',b'8':'en'}.get(message.payload)
                    if event and events.empty(): events.put_nowait(event)
                elif message.kind == 0x10:
                    call.received += len(message.payload)
                    if listening and clock.time() >= ready_after:
                        _, turns = vad.feed(message.payload)
                        if turns and events.empty(): events.put_nowait('answer')
                else:
                    raise ProtocolError('Demo requires 8 kHz PCM')

        async def conversation():
            nonlocal listening, ready_after, vad
            lang = self.lang
            while True:
                listening = False
                await send(self.audio[lang]['greeting'])
                beep = b''.join(struct.pack('<h',int(1800*math.sin(2*math.pi*660*n/8000))) for n in range(1200))
                await send(beep)
                vad = Segmenter(self.threshold,max_seconds=8)
                ready_after = clock.time()+.2
                listening = True
                try:
                    event = await asyncio.wait_for(events.get(),30)
                except TimeoutError:
                    LOG.info('demo_idle uuid=%s',call.id)
                    return
                if event in ('hi','en'):
                    lang = event
                    continue
                listening = False
                await asyncio.sleep(.3)
                LOG.info('demo_fixed_answer uuid=%s lang=%s',call.id,lang)
                await send(self.audio[lang]['answer'])
                await asyncio.sleep(1)
                return

        tasks=[asyncio.create_task(receive()),asyncio.create_task(conversation())]
        try:
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: await task
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


async def main(args):
    if args.prepare:
        await prepare(args.cache)
        return
    bridge=DemoServer(load_audio(args.cache),args.lang,args.vad_threshold)
    server=await asyncio.start_server(bridge.handle,'127.0.0.1',args.port)
    LOG.info('Single-question DEMO listening 127.0.0.1:%s; no live STT/AI',args.port)
    async with server: await server.serve_forever()


def cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true',help='Generate cached speech using Gnani credits once')
    parser.add_argument('--cache',type=Path,default=CACHE)
    parser.add_argument('--port',type=int,default=9094)
    parser.add_argument('--lang',choices=['en','hi'],default='en')
    parser.add_argument('--vad-threshold',type=float,default=350)
    args=parser.parse_args()
    if args.vad_threshold<=0:parser.error('VAD threshold must be positive')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    with suppress(KeyboardInterrupt):asyncio.run(main(args))


if __name__=='__main__': cli()
