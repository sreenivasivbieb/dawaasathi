"""Diagnostic AudioSocket echo only. No STT, medicine lookup or TTS is invoked.

Run: .venv/bin/python -m app.telephony.echo
The dialplan AudioSocket() application uses 8 kHz s16le mono, type 0x10.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from uuid import UUID as CallUUID

from .protocol import DTMF, ERROR, HANGUP, UUID, Message, ProtocolError, read_message

LOG = logging.getLogger(__name__)


@dataclass
class Call:
    id: CallUUID
    received: int = 0
    sent: int = 0


class EchoServer:
    def __init__(self, max_calls: int = 4, idle_timeout: float = 10, call_timeout: float = 180):
        self.max_calls = max_calls
        self.idle_timeout = idle_timeout
        self.call_timeout = call_timeout
        self.calls: dict[CallUUID, Call] = {}
        self.connections = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        call = None
        sender = None
        admitted = False
        try:
            if self.connections >= self.max_calls:
                raise ProtocolError("call capacity reached")
            self.connections += 1
            admitted = True
            async with asyncio.timeout(self.call_timeout):
                first = await asyncio.wait_for(read_message(reader, 1600), 5)
                if first is None or first.kind != UUID:
                    raise ProtocolError("UUID must be first message")
                ident = CallUUID(bytes=first.payload)
                if ident.int == 0 or ident in self.calls:
                    raise ProtocolError("empty or duplicate call UUID")
                call = Call(ident)
                self.calls[ident] = call
                LOG.info("call_started uuid=%s mode=echo", ident)
                await self.exchange(call, reader, writer)
        except (ProtocolError, TimeoutError, ConnectionError, OSError) as exc:
            LOG.warning("call_closed reason=%s", type(exc).__name__)
        finally:
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, ConnectionError, OSError, TimeoutError):
                    await sender
            if call:
                self.calls.pop(call.id, None)
                LOG.info("call_finished uuid=%s received_bytes=%s sent_bytes=%s", call.id, call.received, call.sent)
            if admitted:
                self.connections -= 1
            with suppress(ConnectionError, OSError, TimeoutError):
                writer.write(Message(HANGUP).encode())
                await asyncio.wait_for(writer.drain(), 1)
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)


    async def exchange(self, call: Call, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        ident = call.id
        sender = None
        try:
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

            async def send_audio():
                clock = asyncio.get_running_loop()
                deadline = clock.time()
                while True:
                    pcm = await queue.get()
                    for offset in range(0, len(pcm), 320):
                        chunk = pcm[offset:offset + 320]
                        await asyncio.sleep(max(0, deadline - clock.time()))
                        writer.write(Message(0x10, chunk).encode())
                        await asyncio.wait_for(writer.drain(), 2)
                        call.sent += len(chunk)
                        deadline = max(deadline, clock.time()) + len(chunk) / 16000

            sender = asyncio.create_task(send_audio())
            while True:
                # Surface a failed output task instead of buffering indefinitely.
                if sender.done():
                    await sender
                message = await asyncio.wait_for(read_message(reader, 1600), self.idle_timeout)
                if message is None or message.kind == HANGUP:
                    break
                if message.kind == ERROR:
                    LOG.warning("peer_error uuid=%s", ident)
                    break
                if message.kind == DTMF:
                    # Echo diagnostic: DTMF 0 ends the test. Do not log digits.
                    if message.payload == b"0":
                        break
                elif message.kind == 0x10:
                    call.received += len(message.payload)
                    try:
                        queue.put_nowait(message.payload)
                    except asyncio.QueueFull:
                        raise ProtocolError("audio queue overrun") from None
                else:
                    raise ProtocolError("echo requires 8 kHz PCM after UUID")
        finally:
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, ConnectionError, OSError, TimeoutError):
                    await sender




async def serve(port: int):
    bridge = EchoServer()
    server = await asyncio.start_server(bridge.handle, "127.0.0.1", port, limit=65536)
    LOG.info("AudioSocket echo listening 127.0.0.1:%s; AI is not connected", port)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9092)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
