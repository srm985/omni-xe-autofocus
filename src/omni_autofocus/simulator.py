"""A simulated controller speaking the same framed protocol, for tests and hardware-free development.

It models only what this tool uses: state queries, the RS-485 height sensor, the run/reset state and
auxiliary axis moves. As observed on hardware, list commands sent while the board is not in the run
state are held (status byte 0x0A bit 0 drops to 0) instead of executed; the simulator assumes a reset
discards them. The simulated sensor reading changes by +1 mm for every +1 mm of commanded Z move.
"""

from __future__ import annotations

import struct
from collections import deque

from . import commands, framing, modbus
from .controller import EP_CMD_IN, EP_CMD_OUT, EP_DATA_IN, EP_DATA_OUT


class SimulatedBoard:
    def __init__(
        self,
        *,
        sensor_mm: float = 200.0,
        z_axis_id: int = 1,
        z_pulses_per_mm: float = 800.0,
        z_reverse: bool = True,
        moving_polls: int = 3,
    ):
        self.sensor_mm = sensor_mm
        self.z_axis_id = z_axis_id
        self.z_pulses_per_mm = z_pulses_per_mm
        self.z_reverse = z_reverse
        self.moving_polls = moving_polls
        self._moving = 0
        self.counters = [0x40000000, 0x40000000]
        self.running = False
        self._pending: list[bytes] = []
        self._rx = b""
        self._queues: dict[int, deque[bytes]] = {EP_CMD_IN: deque(), EP_DATA_IN: deque()}
        self.sent_frames: list[tuple[int, bytes]] = []
        self.lists: list[bytes] = []

    # Transport interface ----------------------------------------------------------------------------

    def write(self, endpoint: int, data: bytes, timeout_ms: int) -> int:
        self.sent_frames.append((endpoint, bytes(data)))
        frame = framing.parse_frame(data)
        if endpoint == EP_CMD_OUT:
            reply = self._command(frame.payload)
            self._queues[EP_CMD_IN].append(framing.build_frame(reply, seq=frame.sequence))
        elif endpoint == EP_DATA_OUT:
            self._list(frame.payload)
            ack = framing.DATA_ACK_RESENT if frame.retry else framing.DATA_ACK_FIRST
            self._queues[EP_DATA_IN].append(framing.build_frame(ack, seq=frame.seq & 0x7F))
        else:
            raise OSError(f"unexpected OUT endpoint 0x{endpoint:02x}")
        return len(data)

    def read(self, endpoint: int, size: int, timeout_ms: int) -> bytes:
        q = self._queues.get(endpoint)
        if not q:
            raise TimeoutError(f"simulated timeout on 0x{endpoint:02x}")
        return q.popleft()[:size]

    # Command handling -------------------------------------------------------------------------------

    def _command(self, payload: bytes) -> bytes:
        code = struct.unpack(">H", payload[:2])[0]
        if code in (commands.CMD_DEV_STATE, commands.CMD_DEV_EXT_STATE):
            moving = 1 if self._moving else 0
            if self._moving:
                self._moving -= 1  # each status poll advances the simulated motion
        if code == commands.CMD_DEV_STATE:
            reply = bytearray(0x40)
            reply[0:2] = payload[:2]
            reply[4] = 3 if self.running else 0  # board state (3 = run state, as on hardware)
            reply[12] = 0 if (moving or self._pending) else 1  # "finished" flag
            reply[13:16] = (1024).to_bytes(3, "big")  # free cache, KiB
            return bytes(reply)
        if code == commands.CMD_DEV_EXT_STATE:
            reply = bytearray(0x40)
            reply[0:2] = payload[:2]
            reply[12:20] = struct.pack(">II", *self.counters)
            nibble_shift = 4 if self.z_axis_id % 2 else 0
            reply[0x28 + self.z_axis_id // 2] = moving << nibble_shift
            return bytes(reply)
        if code == commands.CMD_SET_RUN_STATE:
            state = struct.unpack(">H", payload[2:4])[0]
            if state == commands.RUN_STATE_RUN:
                self.running = True
                pending, self._pending = self._pending, []
                for data in pending:
                    self._execute(data)
            elif state == commands.RUN_STATE_RESET:
                self.running = False
                self._pending.clear()
            return payload[:2] + bytes(10)
        if code == commands.CMD_DATA_TRANSMIT:
            length = payload[2] - 4
            out = payload[4 : 4 + length]
            if out:
                self._rx = self._sensor_reply(out)
                rx = b""
            else:
                rx, self._rx = self._rx, b""
            return payload[:2] + bytes([len(rx), 0]) + rx
        return payload[:2] + bytes(10)

    def _sensor_reply(self, request: bytes) -> bytes:
        if request != commands.HEIGHT_REQUEST:
            return b""
        micrometres = round(self.sensor_mm * 1000)
        return modbus.with_crc(bytes([1, 4, 4]) + struct.pack(">I", micrometres))

    def _list(self, data: bytes) -> None:
        self.lists.append(bytes(data))
        if self.running:
            self._execute(data)
        else:
            self._pending.append(bytes(data))

    def _execute(self, data: bytes) -> None:
        i = 0
        while i + 4 <= len(data):
            code = struct.unpack(">H", data[i : i + 2])[0]
            length = data[i + 2] or 4
            if code in (commands.CMD_AXIS_MOVE_01, commands.CMD_AXIS_MOVE_23):
                self._axis_move(code, data[i + 4 : i + length])
            i += length

    def _axis_move(self, code: int, body: bytes) -> None:
        flags = body[0]
        counts = struct.unpack(">II", body[2:10])
        base = 0 if code == commands.CMD_AXIS_MOVE_01 else 2
        for slot in range(2):
            if base + slot != self.z_axis_id or counts[slot] == 0:
                continue
            hw_dir = bool(flags & (1 << slot))
            positive = hw_dir == self.z_reverse  # dir bit = (pulses < 0) XOR reverse
            mm = counts[slot] / self.z_pulses_per_mm
            self.sensor_mm += mm if positive else -mm
            if self.z_axis_id < 2:
                self.counters[self.z_axis_id] += counts[slot] if positive else -counts[slot]
            self._moving = self.moving_polls


class FakeClock:
    """Deterministic clock/sleep pair so simulated runs and tests never really wait."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    def __call__(self) -> float:
        return self.now
