import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from app.telephony.echo import EchoServer
from app.telephony.protocol import Message, ProtocolError, read_message


async def test_fragmented_and_coalesced_tcp_data():
    reader = asyncio.StreamReader()
    expected = [Message(1, uuid4().bytes), Message(0x10, b'\x01\x00' * 160), Message(3, b'#'), Message(0)]
    wire = b''.join(m.encode() for m in expected)

    async def feed():
        nonlocal wire
        for size in [1, 1, 2, 5, 13, len(wire)]:
            reader.feed_data(wire[:size])
            wire = wire[size:]
            await asyncio.sleep(0)
        reader.feed_eof()

    producer = asyncio.create_task(feed())
    assert [await read_message(reader) for _ in expected] == expected
    assert await read_message(reader) is None
    await producer


@pytest.mark.parametrize('wire', [b'\x01', b'\x10\x00\x04\x00', b'\x01\x00\x01', b'\x00\x00\x01', b'\x02\x00\x00', b'\x10\x00\x03', b'\x03\x00\x01x'])
async def test_rejects_invalid_frames(wire):
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_message(reader)


async def test_length_is_network_order_and_limit_checked_before_payload_read():
    assert Message(0x10, bytes(320)).encode()[:3] == b'\x10\x01\x40'
    reader = asyncio.StreamReader()
    reader.feed_data(b'\x10\x10\x00')
    with pytest.raises(ProtocolError, match='configured limit'):
        await read_message(reader, max_payload=1600)


@asynccontextmanager
async def running(**kwargs):
    bridge = EchoServer(**kwargs)
    server = await asyncio.start_server(bridge.handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield bridge, port
    finally:
        server.close()
        await server.wait_closed()
        for _ in range(100):
            if not bridge.connections:
                break
            await asyncio.sleep(.01)
        assert bridge.connections == 0
        assert bridge.calls == {}


async def test_concurrent_calls_are_isolated_and_cleanup_on_eof():
    async with running() as (bridge, port):
        async def call(sample):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                pcm = sample * 160
                writer.write(Message(1, uuid4().bytes).encode() + Message(0x10, pcm).encode())
                await writer.drain()
                assert await asyncio.wait_for(read_message(reader), 1) == Message(0x10, pcm)
            finally:
                writer.close()
                await writer.wait_closed()
        await asyncio.gather(call(b'\x01\x00'), call(b'\x02\x00'), call(b'\x03\x00'))


async def test_duplicate_uuid_does_not_remove_original_session():
    async with running() as (bridge, port):
        ident = uuid4()
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        second_reader, second_writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(Message(1, ident.bytes).encode() + Message(0x10, bytes(320)).encode())
            await writer.drain()
            await asyncio.wait_for(read_message(reader), 1)
            second_writer.write(Message(1, ident.bytes).encode())
            await second_writer.drain()
            assert (await asyncio.wait_for(read_message(second_reader), 1)).kind == 0
            assert ident in bridge.calls
            writer.write(Message(3, b'0').encode())
            await writer.drain()
            assert (await asyncio.wait_for(read_message(reader), 1)).kind == 0
        finally:
            writer.close()
            second_writer.close()
            await writer.wait_closed()
            await second_writer.wait_closed()


@pytest.mark.parametrize('ending', [Message(0).encode(), Message(0xff, b'\x01').encode(), Message(0x11, bytes(320)).encode(), b'\x10\x00\x04\x00'])
async def test_hangup_error_wrong_rate_and_truncated_disconnect(ending):
    async with running() as (_, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(Message(1, uuid4().bytes).encode() + ending)
            await writer.drain()
            writer.write_eof()
            assert (await asyncio.wait_for(read_message(reader), 1)).kind == 0
        finally:
            writer.close()
            await writer.wait_closed()


async def test_idle_timeout_releases_session():
    async with running(idle_timeout=.03) as (_, port):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(Message(1, uuid4().bytes).encode())
            await writer.drain()
            assert (await asyncio.wait_for(read_message(reader), 1)).kind == 0
        finally:
            writer.close()
            await writer.wait_closed()
