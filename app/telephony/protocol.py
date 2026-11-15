"""Asterisk AudioSocket framing, independent of TCP read boundaries.

https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import struct

HANGUP, UUID, DTMF, ERROR = 0x00, 0x01, 0x03, 0xFF
AUDIO_RATES = dict(zip(range(0x10, 0x19), (8000, 12000, 16000, 24000, 32000, 44100, 48000, 96000, 192000)))
HEADER = struct.Struct("!BH")


class ProtocolError(ValueError):
    pass


def validate(kind: int, size: int) -> None:
    if not 0 <= size <= 65535:
        raise ProtocolError("invalid payload length")
    if kind == HANGUP:
        valid = size == 0
    elif kind == UUID:
        valid = size == 16
    elif kind == DTMF:
        valid = size == 1
    elif kind in AUDIO_RATES:
        valid = size > 0 and size % 2 == 0
    elif kind == ERROR:
        valid = True  # Application-specific optional payload.
    else:
        raise ProtocolError("unsupported message type")
    if not valid:
        raise ProtocolError("invalid payload size for message type")


@dataclass(frozen=True)
class Message:
    kind: int
    payload: bytes = b""

    def encode(self) -> bytes:
        validate(self.kind, len(self.payload))
        if self.kind == DTMF and self.payload not in [bytes([c]) for c in b"0123456789ABCDabcd*#"]:
            raise ProtocolError("invalid DTMF digit")
        return HEADER.pack(self.kind, len(self.payload)) + self.payload


async def read_message(reader: asyncio.StreamReader, max_payload: int = 65535) -> Message | None:
    try:
        header = await reader.readexactly(3)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None  # Clean EOF between messages.
        raise ProtocolError("truncated header") from exc
    kind, size = HEADER.unpack(header)
    validate(kind, size)
    if size > max_payload:
        raise ProtocolError("payload exceeds configured limit")
    try:
        payload = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("truncated payload") from exc
    message = Message(kind, payload)
    if kind == DTMF:
        message.encode()  # Validate ASCII digit as well as length.
    return message
