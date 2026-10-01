import pytest

from omni_autofocus import autofocus, commands, config, framing
from omni_autofocus.controller import EP_CMD_OUT, EP_DATA_OUT, Controller, ControllerError
from omni_autofocus.simulator import FakeClock, SimulatedBoard


def make(sensor_mm=200.0, board_cls=SimulatedBoard, **kw):
    board, clock = board_cls(sensor_mm=sensor_mm, **kw), FakeClock()
    return board, Controller(board, sleep=clock.sleep, clock=clock)


def cmd_payloads(board):
    return [framing.parse_frame(f).payload for ep, f in board.sent_frames if ep == EP_CMD_OUT]


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
    assert payload[:2] == b"\x03\xa0"
    assert payload[28:36] == bytes.fromhex("0a 00 08 00 00 00 00 02")  # sendDelayTime(1)


def test_move_axis_follows_vendor_run_state_sequence():
    board, ctl = make()
    params = config.Settings().z_axis.axis_params()
    ctl.move_axis(params, 800)
    codes = [p[:4].hex() for p in cmd_payloads(board)]
    assert codes[0] == "aa100001" and codes[1] == "aa100003"  # reset, then run
    assert codes[-1] == "aa100001"  # reset at the end
    first_data = next(i for i, (ep, _) in enumerate(board.sent_frames) if ep == EP_DATA_OUT)
    run_index = next(i for i, (ep, f) in enumerate(board.sent_frames) if f[6:10].hex() == "aa100003")
    assert run_index < first_data  # run state set before the list goes out
    assert not board.running


def test_list_without_run_state_is_not_executed():
    """Regression: what happened on hardware on 2026-10-01 (list acked, Z did not move)."""
    board, ctl = make(sensor_mm=200.0)
    params = config.Settings().z_axis.axis_params()
    ctl.send_list(commands.command_list(commands.axis_move_cmd(params, 800), commands.delay_cmd(1)))
    assert board.sensor_mm == 200.0
    assert not ctl.state().finished_flag


def test_move_waits_at_least_the_travel_time():
    board, ctl = make()
    clock = ctl._clock
    params = config.Settings().z_axis.axis_params()
    t0 = clock()
    ctl.move_axis(params, params.mm_to_pulses(10.0))
    assert clock() - t0 >= commands.estimated_move_seconds(params, 8000)


def test_reset_is_sent_even_if_waiting_fails():
    class StuckBoard(SimulatedBoard):
        def _command(self, payload):
            reply = super()._command(payload)
            if payload[:2] == b"\xaa\x05":
                reply = bytearray(reply)
                reply[4], reply[12] = 2, 0  # always busy
                reply = bytes(reply)
            return reply

    board, ctl = make(board_cls=StuckBoard)
    with pytest.raises(ControllerError, match="move to finish"):
        ctl.move_axis(config.Settings().z_axis.axis_params(), 800, timeout_s=1.0)
    assert cmd_payloads(board)[-1][:4].hex() == "aa100001"


def test_data_sequence_wraps_1_to_15():
    board, ctl = make()
    for _ in range(16):
        ctl.send_list(commands.command_list(commands.delay_cmd(1)))
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
    board, ctl = make(board_cls=SilentBoard)
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
    board, ctl = make(board_cls=GarbageFirstBoard)
    assert ctl.state().idle


def test_no_target_is_reported_without_retrying():
    from omni_autofocus.controller import SensorNoTargetError

    board, ctl = make(sensor_mm=0x7FFFFFFF / 1000)
    with pytest.raises(SensorNoTargetError, match="no surface"):
        ctl.read_height()
    assert len(board.sent_frames) == 2  # one request + one collect, no retries


def test_height_read_retries_until_valid():
    class FlakySensor(SimulatedBoard):
        fails = 3

        def _sensor_reply(self, request):
            if self.fails:
                self.fails -= 1
                return b"\x01\x04"  # truncated
            return super()._sensor_reply(request)

    board, ctl = make(sensor_mm=150.0, board_cls=FlakySensor)
    assert ctl.read_height() == pytest.approx(150.0)
