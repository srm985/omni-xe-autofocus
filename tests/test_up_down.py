"""Up / down 1 mm: a one-off, sensor-checked 1 mm Z move (autofocus.up_down), on the simulator."""

import struct
from dataclasses import replace

import pytest

from omni_autofocus import autofocus, commands, config, framing
from omni_autofocus.controller import EP_CMD_OUT, EP_DATA_OUT, Controller, ControllerBusyError
from omni_autofocus.simulator import FakeClock, SimulatedBoard

DOWN, UP = -autofocus.UP_DOWN_STEP_MM, autofocus.UP_DOWN_STEP_MM


def lens_b(**focus) -> config.Settings:
    s = config.Settings()
    return replace(s, focus=replace(s.focus, lens="b", **focus))


def sim(sensor_mm=222.0, **kw):
    board = SimulatedBoard(sensor_mm=sensor_mm, sensor_window=(120.0, 280.0), **kw)
    clock = FakeClock()
    return board, Controller(board, sleep=clock.sleep, clock=clock), clock


def press(ctl, clock, mm, s=None, **kw):
    return autofocus.up_down(ctl, s or lens_b(), mm, sleep=clock.sleep, clock=clock, **kw)


def codes(board, start=0):
    """(endpoint, command code) of every frame sent since ``start``."""
    out = []
    for ep, frame in board.sent_frames[start:]:
        payload = framing.parse_frame(frame).payload if ep == EP_CMD_OUT else b""
        out.append((ep, struct.unpack(">H", payload[:2])[0] if payload else None))
    return out


def test_down_and_up_move_the_head_1_mm():
    board, ctl, clock = sim(222.0)
    r = press(ctl, clock, DOWN)
    assert (r.kind, r.step_mm, r.target_mm) == ("ok", -1.0, 222.0)
    assert r.start_mm == pytest.approx(222.0) and r.end_mm == pytest.approx(221.0)
    assert board.sensor_mm == pytest.approx(221.0)
    r = press(ctl, clock, UP)
    assert r.kind == "ok" and r.change_mm == pytest.approx(1.0) and board.sensor_mm == pytest.approx(222.0)


def test_readings_use_at_least_five_samples(monkeypatch):
    board, ctl, clock = sim()
    seen = []
    real = Controller.read_height_median
    monkeypatch.setattr(Controller, "read_height_median", lambda self, n=3: seen.append(n) or real(self, n))
    press(ctl, clock, DOWN)
    assert seen and min(seen) >= 5
    seen.clear()
    press(ctl, clock, DOWN, lens_b(samples=9))
    assert set(seen) == {9}


def test_works_with_lightburn_connected_and_restores_its_run_state():
    board, ctl, clock = sim()
    board.running = True  # LightBurn leaves the board in the run state (board state 3)
    assert not ctl.state().idle and ctl.state().finished_flag
    assert press(ctl, clock, DOWN).kind == "ok"
    assert board.running  # left as LightBurn had it


def test_refuses_while_a_job_runs_or_is_paused_and_sends_no_move():
    board, ctl, clock = sim()
    board.job_running = True
    with pytest.raises(autofocus.UpDownRefused) as e:
        press(ctl, clock, DOWN)
    assert e.value.title == "Laser busy" and "between jobs" in str(e.value)
    assert board.lists == [] and board.sensor_mm == 222.0


def test_a_job_starting_between_the_two_idle_samples_is_refused():
    board, ctl, clock = sim()

    def job_starts(seconds):
        clock.sleep(seconds)
        board.job_running = True

    with pytest.raises(autofocus.UpDownRefused, match="between jobs"):
        autofocus.up_down(ctl, lens_b(), DOWN, sleep=job_starts, clock=clock)
    assert board.lists == []


def test_two_state_samples_come_after_the_question_and_before_the_move():
    board, ctl, clock = sim()
    mark = []
    press(ctl, clock, DOWN, confirm=lambda p: mark.append(len(board.sent_frames)) or True)
    after = codes(board, mark[0])
    first_list = next(i for i, (ep, _) in enumerate(after) if ep == EP_DATA_OUT)
    states = [c for ep, c in after[:first_list] if c == commands.CMD_DEV_STATE]
    assert len(states) >= 3  # up_down's two, then move_axis's own


def test_refuses_when_the_sensor_cannot_see_the_work():
    board, ctl, clock = sim(300.0)  # beyond the sensor's window: "no target"
    with pytest.raises(autofocus.UpDownRefused) as e:
        press(ctl, clock, DOWN)
    assert e.value.title == "Can't see the work" and isinstance(e.value, autofocus.FocusError)
    assert board.lists == []


@pytest.mark.parametrize(
    ("start", "mm", "moves"),
    [
        (122.5, DOWN, False),  # would end 0.5 mm inside the 2 mm margin
        (122.5, UP, True),
        (121.0, DOWN, False),  # already inside the margin: only back out
        (121.0, UP, True),  # ends still in the margin, but closer to the window: allowed
        (277.5, UP, False),
        (277.5, DOWN, True),
        (123.0, DOWN, True),  # ends exactly on the margin
    ],
)
def test_stays_inside_the_sensors_window_but_can_always_step_back(start, mm, moves):
    board, ctl, clock = sim(start)
    if moves:
        assert press(ctl, clock, mm).kind == "ok"
        assert board.sensor_mm == pytest.approx(start + mm)
    else:
        with pytest.raises(autofocus.UpDownRefused) as e:
            press(ctl, clock, mm)
        assert e.value.title == "At the sensor's limit" and "120–280 mm" in str(e.value)
        assert board.sensor_mm == start and board.lists == []


def test_no_limit_relative_to_the_focus_height():
    board, ctl, clock = sim(260.0)  # 38 mm above lens B's focus, e.g. after a lens change
    r = press(ctl, clock, DOWN)
    assert r.kind == "ok" and board.sensor_mm == pytest.approx(259.0)
    r = press(ctl, clock, UP, lens_b(max_move_mm=0.5))  # the autofocus move limit does not apply
    assert r.kind == "ok"


def test_the_question_can_say_no_and_nothing_moves():
    board, ctl, clock = sim()
    asked = []
    r = press(ctl, clock, DOWN, confirm=lambda p: asked.append(p) or False)
    assert r.kind == "cancelled" and r.end_mm == r.start_mm and board.lists == []
    p = asked[0]
    assert (p.height_mm, p.target_mm, p.move_mm, p.pulses) == (222.0, 222.0, -1.0, -800)


def test_a_slow_answer_measures_again_before_moving():
    board, ctl, clock = sim()

    def slow(moved_by):
        def answer(p):
            clock.sleep(5.0)
            board.sensor_mm += moved_by  # the head was moved by hand meanwhile
            return True

        return answer

    with pytest.raises(autofocus.UpDownRefused) as e:
        press(ctl, clock, DOWN, confirm=slow(2.0))
    assert e.value.title == "Height changed" and board.lists == []
    board.sensor_mm = 222.0
    r = press(ctl, clock, DOWN, confirm=slow(0.3))  # within the noise: the new reading is the start
    assert r.start_mm == pytest.approx(222.3) and r.end_mm == pytest.approx(221.3)


def test_a_slow_answer_within_the_noise_still_checks_the_window():
    board, ctl, clock = sim(123.2)

    def answer(p):
        clock.sleep(5.0)
        board.sensor_mm = 122.8  # 0.4 mm: within the re-check noise, but now Down would leave
        return True

    with pytest.raises(autofocus.UpDownRefused) as e:
        press(ctl, clock, DOWN, confirm=answer)
    assert e.value.title == "At the sensor's limit" and board.lists == []


def test_a_stall_is_a_motion_fault_without_invert_advice():
    board, ctl, clock = sim(z_pulses_per_mm=8000)  # Z really moves 0.1 mm
    with pytest.raises(autofocus.MotionError) as e:
        press(ctl, clock, DOWN)
    assert "did not move as asked" in str(e.value) and "invert" not in str(e.value)
    assert not isinstance(e.value, autofocus.UpDownRefused)


def test_a_short_move_says_less_than_asked_without_a_fault():
    board, ctl, clock = sim(z_pulses_per_mm=2667)  # 0.3 mm: e.g. slack on a reversal
    r = press(ctl, clock, DOWN)
    assert r.kind == "less" and r.change_mm == pytest.approx(-0.3, abs=0.01)


@pytest.mark.parametrize(("pulses_per_mm", "words"), [(444, "moved too far"), (320, "pitch settings")])
def test_too_far_is_a_motion_fault(pulses_per_mm, words):
    board, ctl, clock = sim(z_pulses_per_mm=pulses_per_mm)  # 1.8 mm and 2.5 mm per commanded mm
    with pytest.raises(autofocus.MotionError, match=words):
        press(ctl, clock, UP)


def test_wrong_way_is_a_motion_fault_with_invert_advice():
    board, ctl, clock = sim(z_reverse=False)
    with pytest.raises(autofocus.MotionError, match="invert_direction"):
        press(ctl, clock, DOWN)


def test_the_moves_own_busy_refusal_passes_through(monkeypatch):
    board, ctl, clock = sim()

    def busy(self, params, pulses, **kw):
        raise ControllerBusyError("busy")

    monkeypatch.setattr(Controller, "move_axis", busy)
    with pytest.raises(ControllerBusyError):
        press(ctl, clock, DOWN)


def test_checked_move_keeps_autofocus_behaviour():
    board, ctl, clock = sim(205.0)
    s = lens_b()
    params = s.z_axis.axis_params()
    h = autofocus.checked_move(ctl, params, 3, False, 17.0, params.mm_to_pulses(17.0), 205.0)
    assert h == pytest.approx(222.0)
    with pytest.raises(autofocus.MotionError, match="pitch"):
        autofocus.checked_move(ctl, params, 3, False, 17.0, params.mm_to_pulses(3.0), 222.0)
