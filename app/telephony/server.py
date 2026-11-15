"""Local AudioSocket -> existing FastAPI/Gnani assistant. Bind only to loopback."""
import argparse
import asyncio
from contextlib import aclosing, suppress
import logging
import math
import struct

import httpx

from .audio import Segmenter
from .backend import Backend, UnsupportedLanguage
from .echo import EchoServer
from .protocol import DTMF, ERROR, HANGUP, Message, ProtocolError, read_message

LOG = logging.getLogger(__name__)


class AssistantServer(EchoServer):
    mode = "assistant"
    def __init__(self, client, lang="hi", threshold=350, **kwargs):
        super().__init__(**kwargs)
        self.client, self.lang, self.threshold = client, lang, threshold

    async def exchange(self, call, reader, writer):
        backend = Backend(self.client, self.lang)
        vad = Segmenter(self.threshold)
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(("greeting", None))
        output_lock = asyncio.Lock()
        playback = None

        async def send_pcm(source):
            async with output_lock:
                pending = bytearray()
                clock = asyncio.get_running_loop()
                deadline = clock.time()

                async def frame(data):
                    nonlocal deadline
                    await asyncio.sleep(max(0, deadline - clock.time()))
                    writer.write(Message(0x10, data).encode())
                    await asyncio.wait_for(writer.drain(), 2)
                    call.sent += len(data)
                    deadline = max(deadline, clock.time()) + len(data) / 16000

                async for chunk in source:
                    pending.extend(chunk)
                    while len(pending) >= 320:
                        await frame(bytes(pending[:320]))
                        del pending[:320]
                if len(pending) % 2:
                    raise ValueError("truncated PCM sample")
                if pending:
                    await frame(bytes(pending))

        async def tone():
            async def source():
                yield b''.join(struct.pack('<h', int(1800 * math.sin(2 * math.pi * 440 * n / 8000))) for n in range(640))
            await send_pcm(source())

        async def play(path):
            nonlocal playback
            async def speech():
                async with aclosing(backend.pcm(path)) as source:
                    first = asyncio.create_task(anext(source))
                    try:
                        while not first.done():
                            done, _ = await asyncio.wait({first}, timeout=3)
                            if not done:
                                await tone()
                        try:
                            chunk = await first
                        except StopAsyncIteration:
                            raise ValueError("empty backend audio") from None
                        async def ready():
                            yield chunk
                            async for following in source:
                                yield following
                        await send_pcm(ready())
                    finally:
                        first.cancel()
                        await asyncio.gather(first, return_exceptions=True)
            playback = asyncio.create_task(speech())
            try:
                await playback
            except asyncio.CancelledError:
                # Barge-in cancels just playback; cancellation of the entire
                # processor must still propagate during call teardown.
                if asyncio.current_task().cancelling():
                    raise
            finally:
                playback = None

        async def interrupt():
            if playback and not playback.done():
                playback.cancel()
                with suppress(httpx.HTTPError, TimeoutError):
                    await backend.interrupt()

        async def receive():
            while True:
                message = await asyncio.wait_for(read_message(reader, 1600), self.idle_timeout)
                if message is None or message.kind in (HANGUP, ERROR):
                    return
                if message.kind == DTMF:
                    if message.payload == b'#':
                        return
                    await interrupt()
                    if message.payload in (b'7', b'8'):
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(("language", "hi" if message.payload == b'7' else "en"))
                        continue
                    text = {b'1': 'first', b'2': 'second', b'3': 'third', b'9': 'none of these'}.get(message.payload)
                    if text and queue.empty():
                        queue.put_nowait(("text", text))
                elif message.kind == 0x10:
                    call.received += len(message.payload)
                    started, utterances = vad.feed(message.payload)
                    if started:
                        await interrupt()
                    for utterance in utterances:
                        # Only one pending question, bounded to <=20 seconds.
                        if queue.full():
                            queue.get_nowait()
                        queue.put_nowait(("audio", utterance))
                else:
                    raise ProtocolError("assistant requires 8 kHz PCM after UUID")

        async def process():
            idle = 0
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), 15)
                except TimeoutError:
                    if vad.active:
                        continue
                    idle += 1
                    try:
                        async with asyncio.timeout(12):
                            await play(await backend.prompt("goodbye" if idle >= 3 else "idle"))
                    except (httpx.HTTPError, ValueError, TimeoutError):
                        await tone()
                    if idle >= 3:
                        return
                    continue
                idle = 0
                if kind == "language":
                    backend.lang = value
                    backend.auto_language = False
                    kind, value = "text", "hello"
                task = asyncio.create_task(backend.query(
                    pcm=value if kind == "audio" else None,
                    text=value if kind == "text" else "hello",
                ))
                try:
                    async with asyncio.timeout(45):
                        while not task.done():
                            done, _ = await asyncio.wait({task}, timeout=4)
                            if not done:
                                await tone()  # Progress cue, independent of provider availability.
                        path = await task
                    LOG.info("turn_ready uuid=%s lang=%s", call.id, backend.lang)
                    async with asyncio.timeout(120):
                        await play(path)
                except (httpx.HTTPError, ValueError, KeyError, TimeoutError, OSError) as exc:
                    LOG.warning("turn_failed uuid=%s", call.id)
                    try:
                        async with asyncio.timeout(12):
                            await play(await backend.prompt("language" if isinstance(exc, UnsupportedLanguage) else "retry"))
                    except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                        await tone()
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError, httpx.HTTPError, ValueError, KeyError, OSError):
                        await task

        tasks = [asyncio.create_task(receive()), asyncio.create_task(process())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            if playback:
                playback.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(httpx.HTTPError, TimeoutError):
                await backend.interrupt()


async def serve(args):
    async with httpx.AsyncClient(base_url=args.backend, timeout=httpx.Timeout(35, connect=3),
                                 limits=httpx.Limits(max_connections=16), trust_env=False) as client:
        health = await client.get('/healthz')
        health.raise_for_status()
        if health.json().get('stt') != 'prisma' or health.json().get('tts') != 'timbre':
            raise RuntimeError('Backend must have live Prisma STT and Timbre TTS configured')
        bridge = AssistantServer(client, args.lang, args.vad_threshold)
        server = await asyncio.start_server(bridge.handle, '127.0.0.1', args.port, limit=65536)
        LOG.info('Assistant listening 127.0.0.1:%s backend=%s', args.port, args.backend)
        async with server:
            await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9093)
    parser.add_argument('--backend', default='http://127.0.0.1:8004')
    parser.add_argument('--lang', choices=['hi', 'en'], default='hi')
    parser.add_argument('--vad-threshold', type=float, default=350)
    args = parser.parse_args()
    if args.vad_threshold <= 0:
        parser.error('VAD threshold must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
