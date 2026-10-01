"""High-level controller operations: state queries, RS-485 pass-through, height reading, axis moves.

Retry/validation behaviour mirrors the vendor's ``Transfer::writeAndReadCmd`` /
``writeAndReadData`` and ``Executor`` wait loops.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Protocol

from . import commands, framing, modbus
from .commands import AxisParams, DevExtState, DevState

log = logging.getLogger(__name__)

EP_CMD_OUT = 0x06
EP_CMD_IN = 0x88
EP_DATA_OUT = 0x02
EP_DATA_IN = 0x84

WRITE_TIMEOUT_MS = 100
READ_TIMEOUT_MS = 100
CMD_REPLY_CAPACITY = 0x200
TRANSMIT_REPLY_CAPACITY = 0x5B8


class Transport(Protocol):
    def write(self, endpoint: int, data: bytes, timeout_ms: int) -> int: ...
    def read(self, endpoint: int, size: int, timeout_ms: int) -> bytes: ...


class ControllerError(RuntimeError):
    pass


class SensorNoTargetError(ControllerError):
    """The sensor replied with its "no measurement" value."""


class Controller:
    def __init__(self, transport: Transport, *, sleep=time.sleep, clock=time.monotonic):
        self.t = transport
        self._seq_cmd = 0  # the vendor never advances the command-port sequence over USB
        self._seq_data = 0
        self._sleep = sleep
        self._clock = clock

    # -- framed request/response ------------------------------------------------------------------

    def _read_frame(self, endpoint: int, capacity: int) -> framing.Frame | None:
        try:
            raw = self.t.read(endpoint, capacity + framing.OVERHEAD, READ_TIMEOUT_MS)
        except TimeoutError:
            return None
        log.debug("<- ep%02x %s", endpoint, raw.hex(" "))
        try:
            return framing.parse_frame(raw)
        except framing.FrameError as e:
            log.debug("discarding bad frame: %s", e)
            return None

    def command(
        self, payload: bytes, *, capacity: int = CMD_REPLY_CAPACITY, check_echo: bool = True
    ) -> bytes:
        """Send a command on the command port and return the reply payload."""
        code = struct.unpack(">H", payload[:2])[0]
        for attempt in range(2):
            frame = framing.build_frame(
                payload, seq=framing.seq_byte(command_port=True, seq=self._seq_cmd, retry=attempt)
            )
            log.debug("-> ep%02x %s", EP_CMD_OUT, frame.hex(" "))
            self.t.write(EP_CMD_OUT, frame, WRITE_TIMEOUT_MS)
            if code == 0x0141:
                self._sleep(0.03)
            for _ in range(3):
                reply = self._read_frame(EP_CMD_IN, capacity)
                if reply is None:
                    break  # timeout -> resend
                if reply.net_error:
                    log.debug("reply reports transport error %d", reply.net_error)
                    continue
                body = reply.payload
                if check_echo and len(body) >= 2 and body[:2] != payload[:2]:
                    log.debug("reply echo %s != %s", body[:2].hex(), payload[:2].hex())
                    continue
                if reply.sequence and reply.sequence != self._seq_cmd:
                    continue
                return body
        raise ControllerError(f"no valid reply to command 0x{code:04X}")

    def send_list(self, data: bytes) -> None:
        """Send list (motion) commands on the data port."""
        for attempt in range(6):
            if attempt == 0:
                self._seq_data = self._seq_data + 1 if self._seq_data + 1 < 0x10 else 1
            frame = framing.build_frame(
                data, seq=framing.seq_byte(command_port=False, seq=self._seq_data, retry=attempt)
            )
            log.debug("-> ep%02x %s", EP_DATA_OUT, frame.hex(" "))
            self.t.write(EP_DATA_OUT, frame, WRITE_TIMEOUT_MS)
            for _ in range(3):
                reply = self._read_frame(EP_DATA_IN, CMD_REPLY_CAPACITY)
                if reply is None:
                    break
                if reply.net_error:
                    continue
                expected = framing.DATA_ACK_RESENT if reply.retry else framing.DATA_ACK_FIRST
                if reply.payload[:2] != expected:
                    log.debug("unexpected data ack %s", reply.payload[:2].hex())
                    continue
                if reply.sequence and reply.sequence != self._seq_data:
                    continue
                return
        raise ControllerError("controller did not acknowledge list data")

    # -- state ------------------------------------------------------------------------------------

    def state(self) -> DevState:
        return commands.parse_dev_state(self.command(commands.status_cmd(commands.CMD_DEV_STATE)))

    def ext_state(self) -> DevExtState:
        return commands.parse_dev_ext_state(self.command(commands.status_cmd(commands.CMD_DEV_EXT_STATE)))

    def _poll(self, done, what: str, timeout_s: float, interval_s: float) -> None:
        deadline = self._clock() + timeout_s
        while True:
            if done():
                return
            if self._clock() > deadline:
                raise ControllerError(f"timed out waiting for {what}")
            self._sleep(interval_s)

    def wait_idle(self, timeout_s: float = 60.0) -> None:
        self._poll(lambda: self.state().idle, "the controller to become idle", timeout_s, 0.005)

    def wait_axis_stopped(self, axis_id: int, timeout_s: float = 120.0) -> None:
        self._poll(
            lambda: not self.ext_state().axis_moving(axis_id), f"axis {axis_id} to stop", timeout_s, 0.02
        )

    def wait_cache(self, needed_bytes: int, timeout_s: float = 10.0) -> None:
        self._poll(
            lambda: (self.state().free_cache_kb << 10) >= needed_bytes + 10000,
            "free command cache",
            timeout_s,
            0.01,
        )

    # -- RS-485 / height sensor ---------------------------------------------------------------------

    def transmit(self, data: bytes) -> bytes:
        """Send bytes out of the controller's RS-485 port and return whatever it has received."""
        reply = self.command(commands.data_transmit(data), capacity=TRANSMIT_REPLY_CAPACITY)
        return commands.parse_data_transmit_reply(reply)

    def read_height_once(self, settle_s: float = 0.2) -> float:
        self.transmit(commands.HEIGHT_REQUEST)
        self._sleep(settle_s)
        return commands.parse_height_reply(self.transmit(b""))

    def read_height(self, attempts: int = 10) -> float:
        """Read the sensor, retrying communication failures (not a "no target" answer)."""
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.read_height_once()
            except commands.SensorNoTarget as e:
                raise SensorNoTargetError(
                    "the height sensor answered but sees no surface within its measuring range "
                    "(the work surface is probably too far from or too close to the head)"
                ) from e
            except (modbus.ModbusError, ValueError, ControllerError) as e:
                log.debug("height read failed: %s", e)
                last = e
        raise ControllerError(f"could not read the height sensor ({last})")

    # -- motion -----------------------------------------------------------------------------------

    def set_run_state(self, state: int) -> None:
        reply = self.command(commands.run_state_cmd(state))
        if len(reply) >= 3 and reply[2] != 0:
            raise ControllerError(f"controller rejected run state {state} (status {reply[2]})")

    def move_axis(self, params: AxisParams, pulses: int, *, timeout_s: float = 120.0) -> None:
        """Relative move of one auxiliary axis, mirroring ``MarkControl::doMoveAxisPulse``.

        Vendor sequence: reset, 10 ms, run, list [axis move, delay], 5 ms, wait for finish, reset.
        The controller only executes list commands while in the run state.
        """
        if pulses == 0:
            return
        data = commands.command_list(commands.axis_move_cmd(params, pulses), commands.delay_cmd(1))
        min_duration = commands.estimated_move_seconds(params, pulses)
        self.set_run_state(commands.RUN_STATE_RESET)
        self._sleep(0.01)
        self.set_run_state(commands.RUN_STATE_RUN)
        try:
            self.wait_cache(len(data) * 2)
            started = self._clock()
            self.send_list(data)
            self._sleep(0.005)
            self._poll(lambda: self.state().finished, "the move to finish", timeout_s, 0.005)
            # The vendor relies on waitForFinish alone; also wait out the expected travel time so the
            # final reset can never cut a move short, then check the axis' moving flag.
            remaining = started + min_duration + 0.1 - self._clock()
            if remaining > 0:
                self._sleep(remaining)
            self.wait_axis_stopped(params.axis_id, timeout_s)
        finally:
            self.set_run_state(commands.RUN_STATE_RESET)
