import pytest

from omni_autofocus import autofocus, config, framing
from omni_autofocus.controller import EP_CMD_OUT, EP_DATA_OUT, Controller, ControllerError
from omni_autofocus.simulator import SimulatedBoard


def make(sensor_mm=200.0, **kw):
    board = SimulatedBoard(sensor_mm=sensor_mm, **kw)
    return board, Controller(board, sleep=lambda s: None)


def test_read_height_uses_two_transmits():
    board, ctl = make(sensor_mm=213.456)
    assert ctl.read_height() == pytest.approx(213.456)
    sent = [framing.parse_frame(f).payload for ep, f in board.sent_frames if ep == EP_CMD_OUT]
    assert sent[0][:12] == bytes.fromhex("aa c1 0c 00 01 04 00 00 00 02 71 cb")
    assert sent[1][:4] == bytes.fromhex("aa c1 04 00")


def test_command_frames_use_command_port_seq_byte():
    board, ctl = make()
    ctl.state()
    ep, raw = board.sent_frames[0]
    assert ep == EP_CMD_OUT and raw[4] == 0x80


def test_move_axis_sends_list_and_waits():
    board, ctl = make(sensor_mm=200.0)
    params = config.Settings().z_axis.axis_params()
    ctl.move_axis(params, params.mm_to_pulses(10.0))
    assert board.sensor_mm == pytest.approx(210.0)
    data_frames = [f for ep, f in board.sent_frames if ep == EP_DATA_OUT]
    assert len(data_frames) == 1
    assert data_frames[0][4] == 0x01  # data port, first sequence number
    payload = framing.parse_frame(data_frames[0]).payload
    assert payload[:2] == b"\x03\xa0" and payload[28:30] == b"\x02\x42"


def test_data_sequence_wraps_1_to_15():
    board, ctl = make()
    params = config.Settings().z_axis.axis_params()
    for _ in range(16):
        ctl.move_axis(params, 8, wait=False)
    seqs = [f[4] & 0x0F for ep, f in board.sent_frames if ep == EP_DATA_OUT]
    assert seqs == list(range(1, 16)) + [1]


def test_end_to_end_focus_plan_and_move():
    board, ctl = make(sensor_mm=205.0)
    s = config.Settings()  # lens B, target 222 mm
    p = autofocus.plan(ctl.read_height(), s)
    assert p.move_mm == pytest.approx(17.0)
    ctl.move_axis(s.z_axis.axis_params(), p.pulses)
    assert ctl.read_height() == pytest.approx(222.0, abs=0.01)


class SilentBoard(SimulatedBoard):
    def read(self, endpoint, size, timeout_ms):
        raise TimeoutError


def test_no_reply_raises_after_retries():
    board = SilentBoard()
    ctl = Controller(board, sleep=lambda s: None)
    with pytest.raises(ControllerError):
        ctl.state()
    assert len(board.sent_frames) == 2  # vendor behaviour: one resend
    assert board.sent_frames[1][1][4] == 0x90  # retry count in bits 4-6


class GarbageFirstBoard(SimulatedBoard):
    """Returns a corrupt frame before each real reply."""

    def write(self, endpoint, data, timeout_ms):
        n = super().write(endpoint, data, timeout_ms)
        for q in self._queues.values():
            if q:
                q.appendleft(b"\xfe\xff\x00\x10" + bytes(12))
        return n


def test_corrupt_frame_is_skipped():
    board = GarbageFirstBoard()
    ctl = Controller(board, sleep=lambda s: None)
    assert ctl.state().idle


def test_height_read_retries_until_valid():
    class FlakySensor(SimulatedBoard):
        fails = 3

        def _sensor_reply(self, request):
            if self.fails:
                self.fails -= 1
                return b"\x01\x04"  # truncated
            return super()._sensor_reply(request)

    board = FlakySensor(sensor_mm=150.0)
    ctl = Controller(board, sleep=lambda s: None)
    assert ctl.read_height() == pytest.approx(150.0)
