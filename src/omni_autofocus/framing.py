"""USB transfer framing used by the BSL controller (``TransferPackage`` in transfer.dll).

Frame layout (``N`` = total frame length)::

    0   FE FF          magic (0xFFFE stored little-endian)
    2   len (BE u16)   total frame length N
    4   seq byte       bit7 = command port, bits4-6 = retry count, bits0-3 = sequence
    5   status         device -> host: bits1-3 are a network/transport error code
    6   payload...     padded to an even length of at least 12 bytes
    N-2 00
    N-1 checksum       8-bit sum of bytes 6, 7, N-4 and N-3

Board protocol 1 (the Omni's ``Executor7`` board) always uses this framing with padding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"\xfe\xff"
HEADER_LEN = 6
TRAILER_LEN = 2
OVERHEAD = HEADER_LEN + TRAILER_LEN
MIN_PAYLOAD = 12

# Data-port acknowledgement markers (first two payload bytes of a data-port reply).
DATA_ACK_FIRST = b"\xfa\x01"
DATA_ACK_RESENT = b"\xfa\x02"


class FrameError(ValueError):
    """A received frame failed validation."""


def padded_length(n: int) -> int:
    """Payload length after the controller's padding rule (even, minimum 12)."""
    if n % 2:
        n += 1
    return max(n, MIN_PAYLOAD)


def seq_byte(*, command_port: bool, seq: int, retry: int = 0) -> int:
    return (0x80 if command_port else 0) | ((retry & 0x7) << 4) | (seq & 0xF)


def checksum(frame: bytes | bytearray) -> int:
    n = len(frame)
    return (frame[6] + frame[7] + frame[n - 4] + frame[n - 3]) & 0xFF


def build_frame(payload: bytes, *, seq: int) -> bytes:
    """Wrap ``payload`` in a transfer frame. ``seq`` is the full seq byte (see :func:`seq_byte`)."""
    body_len = padded_length(len(payload))
    total = body_len + OVERHEAD
    if total > 0xFFFF:
        raise ValueError(f"payload too large ({len(payload)} bytes)")
    frame = bytearray(total)
    frame[0:2] = MAGIC
    struct.pack_into(">H", frame, 2, total)
    frame[4] = seq & 0xFF
    frame[HEADER_LEN : HEADER_LEN + len(payload)] = payload
    frame[total - 1] = checksum(frame)
    return bytes(frame)


@dataclass(frozen=True)
class Frame:
    seq: int  # raw seq byte
    status: int  # raw status byte (offset 5)
    payload: bytes  # padded payload (frame length - 8 bytes)

    @property
    def sequence(self) -> int:
        return self.seq & 0xF

    @property
    def retry(self) -> int:
        return (self.seq >> 4) & 0x7

    @property
    def net_error(self) -> int:
        return (self.status >> 1) & 0x7


def parse_frame(data: bytes | bytearray) -> Frame:
    """Validate and decode a frame received from the controller."""
    if len(data) < OVERHEAD + 1:
        raise FrameError(f"frame too short ({len(data)} bytes)")
    if data[0:2] != MAGIC:
        raise FrameError(f"bad magic {bytes(data[0:2]).hex()}")
    (total,) = struct.unpack_from(">H", data, 2)
    if total <= OVERHEAD or total > len(data):
        raise FrameError(f"bad length field {total} (received {len(data)} bytes)")
    frame = bytes(data[:total])
    if checksum(frame) != frame[total - 1]:
        raise FrameError("checksum mismatch")
    return Frame(seq=frame[4], status=frame[5], payload=frame[HEADER_LEN : total - TRAILER_LEN])
