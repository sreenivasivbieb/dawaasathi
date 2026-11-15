import asyncio
from uuid import uuid4

import numpy as np
import pytest

from app.telephony.demo import DemoServer, load_audio
from app.telephony.protocol import Message, read_message


def test_demo_rejects_missing_or_invalid_audio(tmp_path):
    with pytest.raises(FileNotFoundError): load_audio(tmp_path)
    (tmp_path/'en-greeting.pcm').write_bytes(b'x')
    with pytest.raises(ValueError): load_audio(tmp_path)


@pytest.mark.parametrize('trigger',['speech','keypad'])
async def test_cached_greeting_then_fixed_answer_without_backend(trigger):
    audio={lang:{'greeting':b'\x01\x00'*160,'answer':b'\x02\x00'*160} for lang in ('en','hi')}
    bridge=DemoServer(audio,idle_timeout=.03)
    server=await asyncio.start_server(bridge.handle,'127.0.0.1',0)
    reader,writer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
    try:
        writer.write(Message(1,uuid4().bytes).encode());await writer.drain()
        assert (await asyncio.wait_for(read_message(reader),1)).payload==audio['en']['greeting']
        if trigger=='speech':
            # Wait for greeting/beep/echo guard, then inject a complete utterance.
            await asyncio.sleep(.5)
            wave=(np.sin(np.arange(160)*2*np.pi*440/8000)*4000).astype('<i2').tobytes()
            for frame in [wave]*20+[bytes(320)]*36:
                writer.write(Message(0x10,frame).encode())
        else:
            writer.write(Message(3,b'1').encode())
        await writer.drain()
        answered=False
        async with asyncio.timeout(3):
            while True:
                message=await read_message(reader)
                if message.kind==0:break
                if message.payload==audio['en']['answer']:answered=True
        assert answered
    finally:
        writer.close();await writer.wait_closed()
        server.close();await server.wait_closed()
        for _ in range(100):
            if not bridge.calls:break
            await asyncio.sleep(.01)
        assert bridge.calls=={} and bridge.connections==0


def test_demo_stage_routes_to_cached_audio_listener(tmp_path,monkeypatch):
    from scripts.telephony import local_pbx
    monkeypatch.setattr(local_pbx,'RUNTIME',tmp_path)
    local_pbx.prepare('demo')
    config=(tmp_path/'asterisk/extensions.conf').read_text()
    assert '127.0.0.1:9094' in config
    assert '${UUID()}' in config
