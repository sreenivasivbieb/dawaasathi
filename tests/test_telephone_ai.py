import asyncio
import io
import wave
from uuid import uuid4

import httpx
import numpy as np
import pytest

from app.telephony.audio import Segmenter, resample
from app.telephony.backend import Backend
from app.telephony.protocol import Message, read_message
from app.telephony.server import AssistantServer


def tone(rate=8000, seconds=.4):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(t * 2 * np.pi * 440) * 4000).astype('<i2').tobytes()


def test_resampling_duration_frequency_and_malformed_samples():
    original = tone()
    up = resample(original, 8000, 16000)
    down = resample(up, 16000, 8000)
    assert abs(len(up) - len(original) * 2) < 64
    assert abs(len(down) - len(original)) < 64
    values = np.frombuffer(down, dtype='<i2')
    peak = np.argmax(abs(np.fft.rfft(values))) * 8000 / len(values)
    assert abs(peak - 440) < 5
    with pytest.raises(ValueError):
        resample(b'x', 8000, 16000)


def test_vad_silence_speech_pause_and_max_length():
    vad = Segmenter()
    assert vad.feed(bytes(16000)) == (False, [])
    audio = tone(seconds=.5) + bytes(12000)
    results = []
    started = False
    # Deliberately split samples/frames across arbitrary TCP-style chunks.
    for offset in range(0, len(audio), 73):
        event, turns = vad.feed(audio[offset:offset+73])
        started |= event
        results.extend(turns)
    assert started and len(results) == 1
    assert len(results[0]) < 16000 * 2
    bounded = Segmenter(max_seconds=1)
    _, results = bounded.feed(tone(seconds=3))
    assert len(results) >= 2
    assert all(len(x) <= 16000 for x in results)


@pytest.mark.parametrize('lang', ['en', 'hi'])
async def test_backend_contract_preserves_language_session_and_wav(lang):
    seen = []

    async def respond(request):
        seen.append(request)
        if request.url.path == '/v1/query':
            body = await request.aread()
            assert b'phone' in body and b'true' in body
            if b'filename="call.wav"' in body:
                start = body.index(b'RIFF')
                end = body.index(b'\r\n--', start)
                with wave.open(io.BytesIO(body[start:end])) as w:
                    assert w.getframerate() == 16000 and w.getnchannels() == 1
            return httpx.Response(200, json={'session_id':'s_test', 'lang':lang, 'tts':'timbre',
                'audio_id':'a', 'audio_stream_url':'/v1/audio/a.pcm'})
        return httpx.Response(200, content=bytes(320), headers={'X-TTS':'timbre', 'X-Sample-Rate':'8000'})

    async with httpx.AsyncClient(base_url='http://local', transport=httpx.MockTransport(respond)) as client:
        backend = Backend(client, lang)
        await backend.query(text='hello')
        path = await backend.query(pcm=tone())
        assert b's_test' in await seen[1].aread()
        assert backend.lang == lang
        assert b''.join([x async for x in backend.pcm(path)]) == bytes(320)


@pytest.mark.parametrize('lang', ['en','hi'])
def test_localized_prompts_use_shared_tts_at_phone_rate(client, lang):
    response = client.post('/v1/phone/prompt', json={'lang':lang, 'purpose':'retry'})
    assert response.status_code == 200
    job = client.app.state.audio.get(response.json()['audio_id'])
    assert job.sample_rate == 8000 and job.lang == lang
    assert job.segments
    assert client.post('/v1/phone/prompt',json={'lang':lang,'purpose':'arbitrary text'}).status_code == 422


async def test_two_ai_calls_use_independent_sessions_and_cleanup():
    sessions = {}
    async def respond(request):
        if request.url.path == '/v1/query':
            body = (await request.aread()).decode(errors='replace')
            sid = next((x for x in sessions if x in body), None) or 's_' + uuid4().hex
            sessions[sid] = sessions.get(sid, 0) + 1
            return httpx.Response(200, json={'session_id':sid,'lang':'hi','tts':'timbre',
                'audio_id':sid,'audio_stream_url':'/v1/audio/'+sid+'.pcm'})
        if request.url.path.startswith('/v1/audio/'):
            return httpx.Response(200,content=bytes(320),headers={'X-TTS':'timbre','X-Sample-Rate':'8000'})
        return httpx.Response(200,json={'ok':True})

    async with httpx.AsyncClient(base_url='http://local',transport=httpx.MockTransport(respond)) as client:
        bridge = AssistantServer(client)
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async def call():
            reader,writer = await asyncio.open_connection('127.0.0.1',port)
            try:
                writer.write(Message(1,uuid4().bytes).encode()); await writer.drain()
                assert (await asyncio.wait_for(read_message(reader),2)).kind == 0x10
                for frame in [tone(seconds=.02)] * 20 + [bytes(320)] * 36:
                    writer.write(Message(0x10,frame).encode())
                await writer.drain()
                assert (await asyncio.wait_for(read_message(reader),3)).kind == 0x10
                writer.write(Message(3,b'#').encode());await writer.drain()
                assert (await asyncio.wait_for(read_message(reader),2)).kind == 0
            finally:
                writer.close();await writer.wait_closed()
        try:
            await asyncio.gather(call(),call())
            for _ in range(100):
                if not bridge.calls: break
                await asyncio.sleep(.01)
            assert bridge.calls == {} and bridge.connections == 0
            assert len(sessions) == 2 and set(sessions.values()) == {2}
        finally:
            server.close();await server.wait_closed()


async def test_backend_rejects_mock_audio_and_external_urls():
    async with httpx.AsyncClient(base_url='http://local') as client:
        backend=Backend(client)
        with pytest.raises(ValueError):
            backend.remember({'tts':'mock'})
        with pytest.raises(ValueError):
            backend.remember({'tts':'timbre','audio_id':'a','audio_stream_url':'https://other/audio'})


async def test_call_rejects_other_languages_and_fixed_language_disables_detection():
    from app.telephony.backend import UnsupportedLanguage
    seen=[]
    async def respond(request):
        seen.append(request)
        return httpx.Response(200,json={'session_id':'s_lang','lang':'mr','tts':'timbre',
            'audio_id':'other','audio_stream_url':'/v1/audio/other.pcm'})
    async with httpx.AsyncClient(base_url='http://local',transport=httpx.MockTransport(respond)) as client:
        backend=Backend(client,'en')
        backend.auto_language=False
        with pytest.raises(UnsupportedLanguage):
            await backend.query(text='hello')
        assert backend.lang=='en'
        assert b'detect_language=false' in await seen[0].aread()
        assert seen[1].url.path=='/v1/interrupt'


def test_call_prompts_allow_only_hindi_and_english(client):
    assert client.post('/v1/phone/prompt',json={'lang':'mr','purpose':'retry'}).status_code==422


async def test_barge_in_cancels_playback_and_recovers_from_provider_failure():
    count = 0
    interruptions = []
    async def respond(request):
        nonlocal count
        if request.url.path == '/v1/query':
            count += 1
            if count == 2:
                return httpx.Response(503)
            return httpx.Response(200,json={'session_id':'s_only','lang':'hi','tts':'timbre',
                'audio_id':'greeting','audio_stream_url':'/v1/audio/greeting.pcm'})
        if request.url.path == '/v1/interrupt':
            interruptions.append(await request.aread())
            return httpx.Response(200,json={'ok':True})
        if request.url.path == '/v1/phone/prompt':
            return httpx.Response(200,json={'audio_id':'retry','tts':'timbre','audio_stream_url':'/v1/audio/retry.pcm'})
        value = b'\x01\x00' if 'greeting' in request.url.path else b'\x02\x00'
        return httpx.Response(200,content=value * (16000 if 'greeting' in request.url.path else 160),
                              headers={'X-TTS':'timbre','X-Sample-Rate':'8000'})
    async with httpx.AsyncClient(base_url='http://local',transport=httpx.MockTransport(respond)) as client:
        bridge=AssistantServer(client)
        server=await asyncio.start_server(bridge.handle,'127.0.0.1',0)
        reader,writer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
        try:
            writer.write(Message(1,uuid4().bytes).encode());await writer.drain()
            assert (await asyncio.wait_for(read_message(reader),2)).payload == b'\x01\x00'*160
            for frame in [tone(seconds=.02)]*20 + [bytes(320)]*36:
                writer.write(Message(0x10,frame).encode())
            await writer.drain()
            async with asyncio.timeout(2):
                while True:
                    message=await read_message(reader)
                    if message.payload==b'\x02\x00'*160: break
            assert interruptions and count==2
        finally:
            writer.close();await writer.wait_closed()
            server.close();await server.wait_closed()
            for _ in range(100):
                if not bridge.calls:break
                await asyncio.sleep(.01)
            assert bridge.calls=={}
