"""Read ComMarker's settings files from the controller's flash (factory calibration).

The controller keeps a small file store in its flash user area. ComMarker Studio's
``LoadParamFromFlash`` downloads ``lcsparam.cfg`` etc. from it the first time a machine is connected;
that is where the per-machine focus heights come from. Everything here only reads.

Commands (``Executor7``): ``AAE0`` flash user-area range, ``AAE1`` flash state, ``AAE4`` load a flash
range into the controller's buffer, ``AAE5`` fetch that buffer.

Store layout (little-endian): a 4 KiB index at offset 0 (backup copy at 0x1000) with a CRC16 at
offset 0, header ``u16 0x1000 @2, u32 1 @4, u32 sector_size @8``, then 50 entries of 0x50 bytes at
0x60: ``u16 crc16 @0 (of bytes 2..0x4F), u16 sector @2, u32 size @4, u64 crc64 @0x10,
u8 flags @0x18 (bit0 = LZMA), name @0x1C``. File data starts at ``sector * sector_size``.
"""

from __future__ import annotations

import lzma
import struct
import zlib
from dataclasses import dataclass

from . import commands, modbus
from .controller import Controller, ControllerError

CMD_FLASH_INFO = 0xAAE0
CMD_FLASH_STATE = 0xAAE1
CMD_FLASH_LOAD = 0xAAE4
CMD_FLASH_FETCH = 0xAAE5

CHUNK = 0x1E0
INDEX_SIZE = 0x1000
ENTRY_OFFSET = 0x60
ENTRY_SIZE = 0x50
MAX_ENTRIES = 50
FLAG_LZMA = 0x01


class FlashError(ControllerError):
    """Reading the flash failed (communication, controller busy)."""


class StoreError(FlashError):
    """The flash was read, but holds no usable file (missing, corrupt, undecodable)."""


def crc16(data: bytes) -> int:
    """The store's CRC16 as the vendor computes it (Modbus CRC16 with its bytes swapped)."""
    m = modbus.crc16(data)
    return ((m & 0xFF) << 8) | (m >> 8)


_CRC64_POLY = 0x42F0E1EBA9EA3693  # CRC-64/ECMA-182 (utils::algorithm::crc, CRC64Algorithms 0)
_CRC64_TABLE: list[int] = []


def crc64(data: bytes) -> int:
    """CRC-64/ECMA-182: MSB-first, init 0, no final XOR."""
    if not _CRC64_TABLE:
        for i in range(256):
            c = i << 56
            for _ in range(8):
                c = ((c << 1) ^ _CRC64_POLY) if c & (1 << 63) else (c << 1)
            _CRC64_TABLE.append(c & 0xFFFFFFFFFFFFFFFF)
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFFFFFFFFFF) ^ _CRC64_TABLE[((crc >> 56) ^ b) & 0xFF]
    return crc


@dataclass(frozen=True)
class FlashInfo:
    start: int
    end: int


@dataclass(frozen=True)
class Entry:
    index: int
    name: str
    sector: int
    size: int
    crc64: int
    flags: int

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FLAG_LZMA)


def _check(reply: bytes, what: str) -> bytes:
    if len(reply) < 4 or reply[2] != 0:
        raise FlashError(f"{what} failed: {reply[:8].hex(' ')}")
    return reply


def flash_info(ctl: Controller) -> FlashInfo:
    r = _check(ctl.command(commands.status_cmd(CMD_FLASH_INFO)), "flash info")
    start, end = struct.unpack_from(">II", r, 4)
    return FlashInfo(start, end)


def flash_state(ctl: Controller) -> tuple[bool, bool, bool]:
    """(busy, read-buffer ready, second busy flag)."""
    r = _check(ctl.command(commands.status_cmd(CMD_FLASH_STATE)), "flash state")
    bits = r[4]
    return bool(bits & 1), bool(bits & 2), bool(bits & 4)


def _wait(ctl: Controller, done, what: str, timeout_s: float = 5.0) -> None:
    # Like the vendor, sleep before every state query: checking immediately after a command can
    # see the previous transfer's "buffer ready" bit and fetch an empty buffer.
    def check() -> bool:
        ctl._sleep(0.001)
        return done(flash_state(ctl))

    ctl._poll(check, what, timeout_s, 0.0)


def read(ctl: Controller, info: FlashInfo, offset: int, length: int) -> bytes:
    """``Executor7::readFlashData``: read ``length`` bytes at ``offset`` within the user area."""
    addr = info.start + offset
    if addr < info.start or addr + length > info.end:
        raise FlashError(f"read of {length} bytes at +0x{offset:x} is outside the flash user area")
    _wait(ctl, lambda s: not s[0] and not s[2], "flash to become idle")
    out = bytearray()
    while len(out) < length:
        n = CHUNK
        if len(out) + n > length:
            n = length - len(out)
            n += n & 1
        load = struct.pack(">HBB", CMD_FLASH_LOAD, 1, 0) + struct.pack(">II", n, addr + len(out))
        for _attempt in range(3):
            _check(ctl.command(load), "flash load")
            _wait(ctl, lambda s: not s[0] and s[1], "flash read buffer")
            r = _check(ctl.command(commands.status_cmd(CMD_FLASH_FETCH)), "flash fetch")
            (got,) = struct.unpack_from(">H", r, 4)
            if got == n and len(r) >= 6 + got:
                break
        else:
            raise FlashError(f"flash fetch returned {got} bytes, expected {n}")
        out += r[6 : 6 + min(got, length - len(out))]
    return bytes(out)


def parse_index(block: bytes) -> list[Entry]:
    if len(block) < INDEX_SIZE:
        raise StoreError("index block too short")
    (stored,) = struct.unpack_from("<H", block, 0)
    if crc16(block[2:INDEX_SIZE]) != stored:
        raise StoreError("index CRC mismatch")
    marker, version, sector = struct.unpack_from("<HII", block, 2)
    if (marker, version, sector) != (0x1000, 1, 0x1000):
        raise StoreError(f"unexpected index header {marker:#x}/{version}/{sector:#x}")
    entries = []
    for i in range(MAX_ENTRIES):
        raw = block[ENTRY_OFFSET + i * ENTRY_SIZE : ENTRY_OFFSET + (i + 1) * ENTRY_SIZE]
        (crc,) = struct.unpack_from("<H", raw, 0)
        if (crc == 0 and struct.unpack_from("<I", raw, 0xC)[0] == 0) or raw[0x1C] == 0:
            continue
        if crc16(raw[2:ENTRY_SIZE]) != crc:
            continue
        sector_no, size = struct.unpack_from("<HI", raw, 2)
        (crc64,) = struct.unpack_from("<Q", raw, 0x10)
        name = raw[0x1C:].split(b"\0")[0].decode("utf-8", "replace")
        entries.append(Entry(i, name, sector_no, size, crc64, raw[0x18]))
    return entries


def read_index(ctl: Controller, info: FlashInfo) -> list[Entry]:
    last: Exception | None = None
    for copy in (0, 1):  # primary, then backup
        try:
            return parse_index(read(ctl, info, copy * INDEX_SIZE, INDEX_SIZE))
        except FlashError as e:
            last = e
    if isinstance(last, StoreError):  # both copies read fine but are unusable
        raise StoreError(f"no valid flash index ({last})")
    raise last


def decompress(data: bytes) -> bytes:
    """Undo the vendor's compression (``utils::algorithm::lzma``): 5-byte LZMA properties, 8-byte
    little-endian uncompressed size, then the raw LZMA stream, i.e. the classic ``.lzma`` format."""
    if len(data) < 13:
        raise StoreError("compressed file too short")
    (size,) = struct.unpack_from("<Q", data, 5)
    props, dict_size = data[0], struct.unpack_from("<I", data, 1)[0]
    lc, rem = props % 9, props // 9
    lp, pb = rem % 5, rem // 5
    filt = {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}
    try:
        out = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filt]).decompress(data[13:])
    except lzma.LZMAError as e:
        raise StoreError(f"could not decompress flash file: {e}") from e
    return out[:size] if size != 0xFFFFFFFFFFFFFFFF else out


def compress(data: bytes) -> bytes:
    """Inverse of :func:`decompress` (used to build simulated flash stores; never written to a device)."""
    filt = {"id": lzma.FILTER_LZMA1, "preset": 6}
    stream = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[filt])
    props = lzma._encode_filter_properties(filt)  # 5 bytes: lc/lp/pb byte + dict size
    return props + struct.pack("<Q", len(data)) + stream


def read_file(ctl: Controller, info: FlashInfo, entry: Entry, sector_size: int = INDEX_SIZE) -> bytes:
    data = read(ctl, info, entry.sector * sector_size, entry.size)
    if crc64(data) != entry.crc64:
        raise StoreError(f"{entry.name}: CRC64 mismatch")
    return decompress(data) if entry.compressed else data


def build_store(files: dict[str, bytes], *, compressed: bool = True) -> bytes:
    """Build a flash user-area image in the vendor's layout (for the simulator and tests only)."""
    index = bytearray(INDEX_SIZE)
    struct.pack_into("<HII", index, 2, 0x1000, 1, INDEX_SIZE)
    body = bytearray()
    sector = 2  # sectors 0 and 1 hold the index and its backup
    for i, (name, content) in enumerate(files.items()):
        stored = compress(content) if compressed else content
        entry = bytearray(ENTRY_SIZE)
        struct.pack_into("<HI", entry, 2, sector, len(stored))
        struct.pack_into("<Q", entry, 0x10, crc64(stored))
        entry[0x18] = FLAG_LZMA if compressed else 0
        encoded = name.encode()[: ENTRY_SIZE - 0x1C - 1]
        entry[0x1C : 0x1C + len(encoded)] = encoded
        struct.pack_into("<H", entry, 0, crc16(bytes(entry[2:])))
        index[ENTRY_OFFSET + i * ENTRY_SIZE : ENTRY_OFFSET + (i + 1) * ENTRY_SIZE] = entry
        start = (sector - 2) * INDEX_SIZE
        body.extend(bytes(start - len(body)))
        body += stored
        sector += (len(stored) + INDEX_SIZE - 1) // INDEX_SIZE
    struct.pack_into("<H", index, 0, crc16(bytes(index[2:])))
    return bytes(index) + bytes(index) + bytes(body)


def read_named(ctl: Controller, name: str) -> bytes:
    """A stored file's bytes (after the store's own optional LZMA layer)."""
    info = flash_info(ctl)
    for entry in read_index(ctl, info):
        if entry.name == name or entry.name.endswith("/" + name.lstrip("./")):
            return read_file(ctl, info, entry)
    raise StoreError(f"{name} is not stored on the controller")


def qt_uncompress(data: bytes) -> bytes:
    """Qt ``qUncompress``: 4-byte big-endian length, then a zlib stream."""
    if len(data) < 6:
        raise StoreError("compressed file too short")
    (size,) = struct.unpack_from(">I", data, 0)
    try:
        out = zlib.decompress(data[4:])
    except zlib.error as e:
        raise StoreError(f"could not uncompress file: {e}") from e
    if len(out) != size:
        raise StoreError(f"uncompressed size {len(out)} differs from the declared {size}")
    return out


def qt_compress(data: bytes) -> bytes:
    """Qt ``qCompress`` (for simulated stores)."""
    return struct.pack(">I", len(data)) + zlib.compress(data)


def read_commarker_file(ctl: Controller, name: str) -> bytes:
    """A ComMarker parameter file as ComMarker's ``LoadParamFromFlash`` sees it. The application
    stores files ``qCompress``-ed (hardware: Omni Xe 6W, 2026-10-01); plain files are passed through."""
    data = read_named(ctl, name)
    if len(data) > 5 and data[4] == 0x78:  # zlib header after the 4-byte length
        return qt_uncompress(data)
    return data
