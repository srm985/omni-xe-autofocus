"""Minimal Modbus RTU helpers for the Omni's RS-485 distance sensor."""

from __future__ import annotations

import struct


class ModbusError(ValueError):
    pass


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(pdu: bytes) -> bytes:
    return pdu + struct.pack("<H", crc16(pdu))


def read_input_registers(slave: int, address: int, count: int) -> bytes:
    return with_crc(struct.pack(">BBHH", slave, 0x04, address, count))


def check_crc(frame: bytes) -> None:
    if len(frame) < 6:
        raise ModbusError(f"response too short ({len(frame)} bytes): {frame.hex(' ')}")
    expected = crc16(frame[:-2])
    (actual,) = struct.unpack_from("<H", frame, len(frame) - 2)
    if expected != actual:
        raise ModbusError(f"CRC mismatch in response {frame.hex(' ')}")


def parse_read_registers(frame: bytes, *, slave: int, function: int = 0x04) -> bytes:
    """Return the register bytes of a read-registers response."""
    check_crc(frame)
    if frame[0] != slave:
        raise ModbusError(f"response from slave {frame[0]}, expected {slave}")
    if frame[1] & 0x80:
        raise ModbusError(f"exception response, code {frame[2]}")
    if frame[1] != function:
        raise ModbusError(f"unexpected function 0x{frame[1]:02x}")
    count = frame[2]
    if len(frame) != count + 5:
        raise ModbusError(f"byte count {count} does not match frame length {len(frame)}")
    return frame[3 : 3 + count]
