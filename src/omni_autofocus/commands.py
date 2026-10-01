"""Controller command encoding (``LcsCmd`` in executor.dll, board protocol 1 / ``Executor7``).

An ``LcsCmd`` is::

    0  code (BE u16)
    2  total length (low byte, header included)
    3  flag
    4  data...

Status queries use a fixed 12-byte form: the code followed by zeros (length byte left at 0).
Multi-byte numeric fields inside command data are big-endian.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from . import modbus

# Command codes
CMD_PROBE_PROTOCOL = 0x0123  # sent unframed by the vendor software to detect the board protocol
CMD_DEV_STATE = 0xAA05
CMD_DEV_EXT_STATE = 0xAA07
CMD_DATA_TRANSMIT = 0xAAC1  # RS-485 pass-through ("setDataTransmit2")
CMD_AXIS_MOVE_01 = 0x03A0  # list command: move axes 0/1
CMD_AXIS_MOVE_23 = 0x03A1  # list command: move axes 2/3
CMD_MOVE_TO_REL = 0x0242  # list command: relative galvo move (used as a list terminator after axis moves)
CMD_SET_RUN_STATE = 0xAA10  # immediate: param 3 = run (execute lists), param 1 = reset/stop
CMD_DELAY = 0x0A00  # list command: delay

AXIS_COUNTER_ORIGIN = 0x40000000

RUN_STATE_RUN = 3
RUN_STATE_RESET = 1

MAX_TRANSMIT_LEN = 0xFB


def lcs_cmd(code: int, data: bytes = b"", flag: int = 0) -> bytes:
    total = len(data) + 4
    return struct.pack(">HBB", code, total & 0xFF, flag & 0xFF) + data


def status_cmd(code: int) -> bytes:
    """The fixed 12-byte form used by state queries."""
    return struct.pack(">H", code) + bytes(10)


def run_state_cmd(state: int) -> bytes:
    """``Executor7::setRun`` (state 3) / ``setReset`` (state 1): 12-byte form with a BE u16 at offset 2."""
    return struct.pack(">HH", CMD_SET_RUN_STATE, state) + bytes(8)


def delay_cmd(time_units: int = 1) -> bytes:
    """``sendDelayTime(n)`` list command; the vendor encodes ``2 * n``."""
    return lcs_cmd(CMD_DELAY, struct.pack(">I", 2 * time_units))


def data_transmit(payload: bytes, flag: int = 0) -> bytes:
    if len(payload) > MAX_TRANSMIT_LEN:
        raise ValueError(f"RS-485 payload too long ({len(payload)} > {MAX_TRANSMIT_LEN})")
    return lcs_cmd(CMD_DATA_TRANSMIT, payload, flag)


def parse_data_transmit_reply(reply: bytes) -> bytes:
    """Bytes received on the RS-485 side, from an ``0xAAC1`` reply payload."""
    if len(reply) < 4:
        raise ValueError(f"short AAC1 reply: {reply.hex(' ')}")
    count = reply[2]
    if count > len(reply) - 4:
        raise ValueError(f"AAC1 reply claims {count} bytes but only {len(reply) - 4} present")
    return bytes(reply[4 : 4 + count])


# --- Height sensor ---------------------------------------------------------------------------------

SENSOR_SLAVE = 1
HEIGHT_REQUEST = modbus.read_input_registers(SENSOR_SLAVE, 0, 2)  # 01 04 00 00 00 02 71 CB


SENSOR_NO_TARGET = 0x7FFFFFFF  # observed on hardware when nothing is within the measuring range


class SensorNoTarget(Exception):
    """The sensor answered correctly but reported that it has no valid measurement."""


def parse_height_reply(frame: bytes) -> float:
    """Distance in mm from the sensor's Modbus reply (two registers, big-endian micrometres)."""
    regs = modbus.parse_read_registers(frame, slave=SENSOR_SLAVE)
    if len(regs) != 4:
        raise modbus.ModbusError(f"expected 4 register bytes, got {len(regs)}")
    (micrometres,) = struct.unpack(">I", regs)
    if micrometres == SENSOR_NO_TARGET:
        raise SensorNoTarget(f"sensor reports no target ({regs.hex(' ')})")
    return micrometres / 1000.0


# --- Axis moves ------------------------------------------------------------------------------------


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


@dataclass(frozen=True)
class AxisParams:
    """Subset of the vendor ``ExtAxisPar`` needed for a plain relative move.

    Defaults are the vendor constructor defaults, used for the unused slot of a two-axis command.
    """

    axis_id: int = 0
    reverse: bool = False  # bRevRot
    pitch_pulse: int = 10000  # pulses per revolution
    screw_pitch: float = 10.0  # mm per revolution
    max_run_speed: float = 320.0
    start_speed: float = 2.0  # mm/s
    run_speed: float = 10.0  # mm/s
    acc_speed: float = 10.0  # mm/s^2
    gear_ratio: float = 1.0
    signal_valid_type: int = 0

    @property
    def pulses_per_mm(self) -> float:
        return _f32(_f32(float(self.pitch_pulse)) / _f32(self.screw_pitch))

    def mm_to_pulses(self, mm: float) -> int:
        return int(_f32(self.pitch_pulse * mm / self.screw_pitch))


def _round_half_away(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


@dataclass(frozen=True)
class _Slot:
    params: AxisParams
    pulses: float
    direction: bool


def _encode_slot(slot: _Slot) -> dict:
    p = slot.params
    ppm = p.pulses_per_mm
    start = _f32(p.start_speed)
    run = _f32(p.run_speed)
    if start >= run:
        run = start
    max_run = _f32(p.max_run_speed)
    if run > max_run:
        run = max_run
    t = (run - start) / _f32(p.acc_speed)
    acc_byte = 0xFF if t > 0.255 else int(t * 1000.0) & 0xFF
    acc_dist = _f32(_f32(p.start_speed) + _f32(p.run_speed)) * t * 0.5 * ppm
    if acc_dist > 65535.0:
        acc_dist = 65535.0
    return {
        "dir": bool(slot.direction) ^ p.reverse,
        "signal": p.signal_valid_type == 1,
        "count": _round_half_away(slot.pulses) & 0xFFFFFFFF,
        "start_freq": int(min(65535.0, start * ppm)),
        "run_freq": int(min(65535.0, run * ppm)),
        "acc_byte": acc_byte,
        "acc_dist": int(acc_dist),
    }


def axis_move_cmd(params: AxisParams, pulses: int) -> bytes:
    """Encode a relative move of ``pulses`` (signed) on one axis as an ``0x03A0``/``0x03A1`` list command."""
    if not 0 <= params.axis_id < 4:
        raise ValueError("axis_id must be 0..3")
    count = abs(pulses) * params.gear_ratio if params.gear_ratio > 0 else abs(pulses)
    count = max(1.0, count)
    slots = [_Slot(AxisParams(), 0.0, False), _Slot(AxisParams(), 0.0, False)]
    slots[params.axis_id % 2] = _Slot(params, count, pulses < 0)
    a, b = (_encode_slot(s) for s in slots)
    flags = (1 if a["dir"] else 0) | (2 if b["dir"] else 0)
    signal = (1 if a["signal"] else 0) | (2 if b["signal"] else 0)
    data = struct.pack(
        ">BBIIHHBHHBHH",
        flags,
        signal,
        a["count"],
        b["count"],
        a["start_freq"],
        a["run_freq"],
        a["acc_byte"],
        b["start_freq"],
        b["run_freq"],
        b["acc_byte"],
        a["acc_dist"],
        b["acc_dist"],
    )
    code = CMD_AXIS_MOVE_01 if params.axis_id < 2 else CMD_AXIS_MOVE_23
    return lcs_cmd(code, data)


def estimated_move_seconds(params: AxisParams, pulses: int) -> float:
    """Rough duration of a trapezoidal move, used only as a lower bound when waiting."""
    ppm = params.pulses_per_mm
    run = min(max(params.run_speed, params.start_speed), params.max_run_speed)
    if run <= 0 or ppm <= 0:
        return 0.0
    distance_mm = abs(pulses) / ppm
    accel_time = (run - params.start_speed) / params.acc_speed if params.acc_speed > 0 else 0.0
    accel_dist = (params.start_speed + run) * accel_time / 2
    if 2 * accel_dist >= distance_mm:  # triangular profile
        peak = math.sqrt(params.start_speed**2 + params.acc_speed * distance_mm)
        return 2 * (peak - params.start_speed) / params.acc_speed
    return 2 * accel_time + (distance_mm - 2 * accel_dist) / run


def move_to_rel_cmd(x: int = 0, y: int = 0, a: int = 0, b: int = 1) -> bytes:
    """``sendMoveToRel``; the vendor appends ``move_to_rel_cmd()`` after every axis move."""
    if a == 0 and (x or y):
        a = 1
    data = struct.pack(">HH", a, b) + (x & 0xFFFFFF).to_bytes(3, "big") + (y & 0xFFFFFF).to_bytes(3, "big")
    return lcs_cmd(CMD_MOVE_TO_REL, data + bytes(4))


def command_list(*cmds: bytes) -> bytes:
    """Concatenate list commands, padding short lists with a zero filler command (uncompressed form)."""
    buf = b"".join(cmds)
    pad = 0
    while len(buf) + pad < 12:
        pad += 4
    if pad:
        buf += bytes([0, 0, pad, 0]) + bytes(pad - 4)
    return buf


# --- State replies ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DevState:
    board_state: int
    free_cache_kb: int
    finished_flag: bool  # status byte 0x0A bit 0; the vendor's waitForFinish treats it as "done"
    raw: bytes

    @property
    def idle(self) -> bool:
        return self.board_state <= 1

    @property
    def finished(self) -> bool:
        """The vendor's ``Executor::waitForFinish`` exit condition."""
        return self.finished_flag or self.idle


def parse_dev_state(reply: bytes) -> DevState:
    if len(reply) < 0x20 or reply[2] != 0:
        raise ValueError(f"bad AA05 reply: {reply[:16].hex(' ')}")
    g = lambda i: reply[i + 2]  # noqa: E731 - mirrors LcsStateEx indexing
    return DevState(
        board_state=g(2),
        free_cache_kb=(g(0xB) << 16) | (g(0xC) << 8) | g(0xD),
        finished_flag=bool(g(0xA) & 1),
        raw=bytes(reply),
    )


@dataclass(frozen=True)
class DevExtState:
    axis_status: tuple[int, int, int]  # low bit set while the axis is moving
    axis_counters: tuple[int, int]  # raw position counters of axes 0 and 1 (reply bytes 12-15, 16-19)
    raw: bytes

    def axis_moving(self, axis_id: int) -> bool:
        return bool(self.axis_status[axis_id] & 1)

    def axis_position(self, axis_id: int) -> int:
        """Position in pulses relative to the counter's 0x40000000 origin.

        Hardware-confirmed for axis 1 (Z): a +800 pulse move changed the counter by exactly +800.
        """
        return self.axis_counters[axis_id] - AXIS_COUNTER_ORIGIN


def parse_dev_ext_state(reply: bytes) -> DevExtState:
    if len(reply) < 0x2C or reply[2] != 0:
        raise ValueError(f"bad AA07 reply: {reply[:16].hex(' ')}")
    b26, b27 = reply[0x26 + 2], reply[0x27 + 2]
    counters = struct.unpack_from(">II", reply, 12)
    return DevExtState(axis_status=(b26 & 0xF, b26 >> 4, b27 & 0xF), axis_counters=counters, raw=bytes(reply))
