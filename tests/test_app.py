import gc
import json
import os
import time

import pytest

from omni_autofocus import app, autofocus, config, session
from omni_autofocus.controller import ControllerBusyError, SensorNoTargetError


def plan(move_mm: float) -> autofocus.FocusPlan:
    return autofocus.FocusPlan(222.0 - move_mm, 222.0, move_mm, int(move_mm * 800))


def test_only_large_downward_moves_need_confirmation():
    a = config.AppSettings(confirm_down_above_mm=10.0)
    assert not app.needs_confirmation(plan(+40.0), a)  # up: away from the work
    assert not app.needs_confirmation(plan(-9.9), a)
    assert app.needs_confirmation(plan(-10.1), a)
    assert app.needs_confirmation(plan(+0.5), a, motion_fault=True)  # after a fault: every move


def test_describe_error_is_actionable():
    assert "framing preview" in app.describe_error(ControllerBusyError("busy"))
    assert "Z buttons" in app.describe_error(SensorNoTargetError("x"))
    assert app.describe_error(session.ConflictError("ComMarker Studio is running")).startswith("ComMarker")
    assert app.describe_error(autofocus.FocusError("required move too big")) == "Required move too big."


def test_describe_result():
    p1, p2 = plan(17.0), plan(0.0)
    moved = autofocus.FocusResult(autofocus.Outcome.IN_FOCUS, 222.0, 222.0, (p1, p2), True)
    assert app.describe_result(moved) == ("In focus", "Moved up 17.0 mm · height 222.0 mm", app.OK)
    still = autofocus.FocusResult(autofocus.Outcome.IN_FOCUS, 222.0, 222.0, (p2,), False)
    assert app.describe_result(still)[0] == "Already in focus"
    off = autofocus.FocusResult(autofocus.Outcome.NOT_CONVERGED, 221.0, 222.0, (p1,), True)
    assert app.describe_result(off)[2] == app.ERR
    no = autofocus.FocusResult(autofocus.Outcome.CANCELLED, 240.0, 222.0, (plan(-18.0),), False)
    assert app.describe_result(no)[:2] == ("Cancelled", "Z did not move.")


def test_run_command(tmp_path):
    exe = tmp_path / "OmniAutofocus.exe"
    assert app.run_command(True, str(exe)) == f'"{exe}"'
    assert app.run_command(False, str(tmp_path / "python.exe")).endswith("-m omni_autofocus.app")


def test_offset_labels():
    assert [app.fmt_offset(o) for o in (-4.0, 0.0, 2.0)] == ["−4", "0", "+2"]
    assert app.parse_offset_label("−3") == -3.0 and app.parse_offset_label("+1") == 1.0


def test_hotkey_parsing():
    assert config.parse_hotkey("ctrl+alt+f") == (0x2 | 0x1, ord("F"))
    assert config.parse_hotkey("Win+Shift+F9") == (0x8 | 0x4, 0x78)
    for bad in ("f", "ctrl+", "hyper+f", "ctrl+alt+pause"):
        with pytest.raises(ValueError):
            config.parse_hotkey(bad)
    with pytest.raises(ValueError):
        config.set_value(config.Settings(), "app.hotkey", "ctrl+")
    assert config.set_value(config.Settings(), "app.hotkey", "").app.hotkey == ""


def test_app_section_round_trips(tmp_path):
    path = tmp_path / "s.toml"
    s = config.Settings(app=config.AppSettings(hotkey="ctrl+shift+a", sounds=False))
    config.save(s, path)
    assert config.load(path).app == s.app


# --- the real window against the simulator ---------------------------------------------------------


@pytest.fixture(scope="module")
def shared_window(tmp_path_factory):
    # One window for the module: Tk cannot always create a second interpreter in one process.
    tk = pytest.importorskip("tkinter")
    home = tmp_path_factory.mktemp("app")
    path = home / "app.toml"
    config.save(config.Settings(app=config.AppSettings(hotkey="", sounds=False)), path)
    with pytest.MonkeyPatch.context() as mp:  # module scope: the per-test isolation is not active yet
        mp.setenv("LOCALAPPDATA", str(home))
        mp.setenv("APPDATA", str(home))
        # A test must never block on a real dialog: unanswered questions count as "no".
        mp.setattr(app.App, "_ask_yes_no", lambda self, q, **kw: False)
        try:
            w = app.App(config_path=path, simulate=True)
        except tk.TclError as e:  # no display
            pytest.skip(f"no Tk display: {e}")
        yield w
        w.root.destroy()


@pytest.fixture
def window(shared_window):
    settle(shared_window)
    board = shared_window._session.open()[0].__enter__()
    board.sensor_mm = 205.0
    return shared_window


def settle(w, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        w.root.update()
        if not w.busy and w.events.empty():
            w.root.update()
            return
        time.sleep(0.01)
    raise AssertionError("the app did not finish")


def test_window_autofocus_in_simulation(window):
    assert window.lens_info.cget("text").startswith("Auto → 150 mm (B)")
    window.autofocus()
    settle(window)
    assert window.status.cget("text") == "In focus"
    assert window.detail.cget("text").startswith("Moved up 17.0 mm")
    window.autofocus()  # the simulated laser keeps its Z position
    settle(window)
    assert window.status.cget("text") == "Already in focus"


def test_window_asks_before_a_large_downward_move(window, monkeypatch):
    board = window._session.open()[0].__enter__()
    board.sensor_mm = 240.0  # 18 mm too far: the head would move down
    asked = []
    monkeypatch.setattr(window, "_ask_yes_no", lambda q, **kw: asked.append(q) or False)
    window.autofocus()
    settle(window)
    assert asked and "down 18.0 mm, towards the work" in asked[0]
    assert window.status.cget("text") == "Cancelled"


def test_window_reports_errors(window, monkeypatch):
    def busy(self, *a, **k):
        raise ControllerBusyError("busy")

    monkeypatch.setattr("omni_autofocus.controller.Controller.move_axis", busy)
    window.autofocus()
    settle(window)
    assert window.status.cget("text") == "Stopped"
    assert "framing preview" in window.detail.cget("text")
    assert window.button.enabled  # usable again


def test_settings_types_are_checked(tmp_path):
    path = tmp_path / "bad.toml"
    heights = "[focus]\ntarget_a_mm = 181.0\ntarget_b_mm = 222.0\n"
    path.write_text(heights + "[app]\nhotkey = true\n")
    with pytest.raises(ValueError, match="app.hotkey must be str"):
        config.load(path)
    path.write_text(heights + "offset_mm = 1\nsamples = 5\n")  # ints are fine for float settings
    s = config.load(path)
    assert s.focus.offset_mm == 1.0 and isinstance(s.focus.offset_mm, float) and s.focus.samples == 5
    path.write_text(heights + "samples = 2.5\n")
    with pytest.raises(ValueError, match="focus.samples must be int"):
        config.load(path)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows named mutex")
def test_laser_lock_is_exclusive():
    name = r"Local\OmniAutofocusLaserTest"
    with session.LaserLock(name):
        with pytest.raises(session.ConflictError, match="another Omni Autofocus"):
            with session.LaserLock(name):
                pass
    with session.LaserLock(name):  # released again
        pass


def test_simulation_is_marked_and_never_registers_the_hotkey(window):
    assert window.title == "Omni Autofocus (simulation)"
    assert window.root.title() == window.title
    assert window.hotkey is None


def test_window_cannot_close_while_z_may_move(window):
    window.moving = True
    try:
        window.close()
        assert window.root.winfo_exists() and "Wait until Z has stopped" in window.detail.cget("text")
    finally:
        window.moving = False


def test_bad_event_does_not_stop_the_window(window):
    window.events.put(("lens",))  # malformed: missing text
    window.events.put(("status", "still alive", app.INFO))
    settle(window)
    assert window.status.cget("text") == "still alive"


def test_first_run_shows_the_calibration_and_can_cancel(window, monkeypatch, tmp_path):
    fresh = tmp_path / "fresh.toml"
    monkeypatch.setattr(window._session, "path", fresh)
    asked = []
    monkeypatch.setattr(window, "_ask_yes_no", lambda q, **kw: asked.append(q) or False)
    window.autofocus()
    settle(window)
    assert "focus heights stored in your laser" in asked[0] and "150 mm (B) lens: 222.0 mm" in asked[0]
    assert window.status.cget("text") == "Not saved"
    assert not fresh.exists()  # declined: nothing saved
    board = window._session.open()[0].__enter__()
    assert board.sensor_mm == 205.0


def test_lens_summary():
    why = "lens B (150 mm field, from the only BSL profile in LightBurn, 'BSLFiber')"
    assert app.lens_summary("Auto", why, 222.0) == "Auto → 150 mm (B) · LightBurn 'BSLFiber' · 222.0 mm"
    assert app.lens_summary("A", "lens A (set explicitly)", None) == "70 mm (A) lens · focus not read"
    assert app.lens_summary("Auto", "lens A (from ComMarker Studio's lens setting; x)", 181.0) == (
        "Auto → 70 mm (A) · ComMarker Studio · 181.0 mm"
    )


def test_updown():
    assert app.updown(17.0) == "up 17.0 mm" and app.updown(-3.04) == "down 3.0 mm"


def test_rounded_button_image_is_a_valid_png():
    from omni_autofocus import ui

    png = ui.rounded_rect_png(40, 20, 6, "#1859a0", "#ffffff", ring="#ffffff")
    assert png.startswith(b"\x89PNG") and b"IEND" in png


def _png_rows(png: bytes) -> tuple[int, int, list[bytes]]:
    import struct
    import zlib

    w, h = struct.unpack(">II", png[16:24])
    start = png.index(b"IDAT") + 4
    length = struct.unpack(">I", png[start - 8 : start - 4])[0]
    raw = zlib.decompress(png[start : start + length])
    stride = 1 + 3 * w
    assert len(raw) == h * stride  # exactly h rows, no more
    return w, h, [raw[i * stride : (i + 1) * stride] for i in range(h)]


def test_button_and_check_box_images():
    from omni_autofocus import ui

    w, h, rows = _png_rows(ui.rounded_rect_png(40, 21, 6, "#1859a0", "#ffffff", ring="#ffffff"))
    assert (w, h, len(rows)) == (40, 21, 21) and rows == rows[::-1]  # odd height, mirrored halves
    for on in (False, True):
        w, h, rows = _png_rows(ui.checkbox_png(25, on, ui.DARK))
        assert (w, h) == (25, 25) and all(len(r) == 1 + 3 * 25 for r in rows)
        inside = rows[12][1 + 3 * 3 : 1 + 3 * 4]  # inside the box, left of the tick
        assert inside == bytes(ui._rgb(ui.DARK.accent if on else ui.DARK.bg))


def test_check_box_widget_toggles_and_lets_go_of_its_variable(window):
    import tkinter as tk

    from omni_autofocus import ui

    top = tk.Toplevel(window.root)
    var = tk.BooleanVar(master=top, value=False)
    box = ui.Check(top, text="Sound", variable=var, palette=ui.LIGHT, scale=1.75, font=("Segoe UI", 9))
    box.pack()
    top.update()
    assert box.text_x == box.label.winfo_x() + box.label.winfo_pixels(box.label.cget("bd"))
    assert box._toggle() == "break" and var.get() is True
    assert str(box.box.cget("image")) == str(box._images[True])
    var.set(False)  # the variable drives the picture too
    assert str(box.box.cget("image")) == str(box._images[False])

    class Mouse:  # a press or release at some screen point
        x_root = y_root = 0

    for over, expect in ((box.label, True), (None, True), (box.box, False)):  # None: released elsewhere
        box.winfo_containing = lambda x, y, over=over: over
        box._press(Mouse())
        box._click(Mouse())
        assert var.get() is expect
    box._click(Mouse())  # a release without a press here (the press was elsewhere) does nothing
    assert var.get() is False
    box._key_release(None)  # a release without a press here (e.g. Space held in a dialog) does nothing
    assert var.get() is False
    box._key_press(None), box._key_press(None), box._key_release(None)  # held down: one toggle
    assert var.get() is True
    box.destroy()
    assert not var.trace_info()
    top.destroy()
    del var, box, top
    gc.collect()  # free the Tk objects here, on the Tk thread, not later from a worker thread


def test_window_keeps_its_width_in_every_view_and_with_long_text(window):
    w = window
    w.root.update_idletasks()
    width = w.root.winfo_reqwidth()

    def same_width(what: str) -> None:
        w.root.update_idletasks()
        assert w.root.winfo_reqwidth() == width, what

    hint, lens_line = w.hint.cget("text"), w.lens_info.cget("text")
    w.hint.configure(text="Ctrl + Shift + Alt + Win + F12 is taken · ⋯ → Settings")
    w._set_status("Installing the driver…", app.INFO)
    w._set_detail(r"C:\Users\someone\AppData\Roaming\omni-autofocus\config.toml " * 3)
    long_name = "lens B (LightBurn 'A very long device name here')"
    w.lens_info.configure(text=app.lens_summary("Auto", long_name, 222.0))
    same_width("long texts")
    w._set_detail(" ")
    w.hint.configure(text=hint)
    w.lens_info.configure(text=lens_line)
    w.open_settings_view()
    assert w.settings_open
    same_width("settings")
    w._close_settings()
    w.open_fine_tune()
    assert w.tune is not None
    same_width("fine-tune")
    for stage in ("burn", "result"):
        w.tune["stage"] = stage
        w._tune_render()
        same_width(stage)
    w._close_tune()
    w._set_status("Ready", app.INFO)


WIDTHS_AT_SCALE = """
import json, sys, tkinter as tk
scale = float(sys.argv[1])
class Scaled(tk.Tk):
    def __init__(self, *a, **k):
        try:
            super().__init__(*a, **k)
        except tk.TclError:
            sys.exit(77)  # no display: the only reason to skip
        self.tk.call("tk", "scaling", scale * 96 / 72)
tk.Tk = Scaled
from pathlib import Path
from omni_autofocus import app, config
config.COMMARKER_DIR = Path(sys.argv[2]).parent / "no-commarker"  # as conftest.py does in-process
import time
w = app.App(config_path=Path(sys.argv[2]), simulate=True)
deadline = time.monotonic() + 20
while (w.busy or not w.events.empty()) and time.monotonic() < deadline:  # the startup reading
    w.root.update()
    time.sleep(0.01)
assert not w.busy, "the app did not finish starting"
widths = []
def width():
    w.root.update_idletasks()
    widths.append(w.root.winfo_reqwidth())
w.hint.configure(text="Ctrl + Shift + Alt + Win + F12 is taken · ⋯ → Settings")
w._set_detail("Measured 222.0 mm. Use it only with the 150 mm (B) lens fitted and Z at its best focus. " * 2)
width()
w.open_settings_view(); assert w.settings_open; width(); w._close_settings()
w.open_fine_tune(); assert w.tune is not None; width()
w.tune["stage"] = "result"; w._tune_render(); width()
w._close_tune()
from omni_autofocus import ui
print(json.dumps([widths, w.button.winfo_reqwidth(), int(20 * ui.scale_of(w.root))]))
w.root.destroy()
"""


@pytest.mark.parametrize("scale", [1.25, 1.75, 2.0])
def test_window_width_is_the_same_in_every_view_on_scaled_displays(tmp_path, scale):
    # A process of its own: the scaling is fixed when the window is made, and Tk allows one window here.
    import subprocess
    import sys

    pytest.importorskip("tkinter")
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path), "APPDATA": str(tmp_path), "PYTHONUTF8": "1"}
    out = subprocess.run(
        [sys.executable, "-c", WIDTHS_AT_SCALE, str(scale), str(tmp_path / "settings.toml")],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )  # fmt: skip
    if out.returncode == 77:
        pytest.skip("no Tk display")
    assert out.returncode == 0, out.stderr
    widths, button, margin = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(set(widths)) == 1, widths
    assert widths[0] == button + 2 * margin  # nothing is wider than the Autofocus button


def test_motion_fault_latches_until_z_follows_again(window, monkeypatch):
    board = window._session.open()[0].__enter__()
    board.z_reverse = False  # Z wired the other way round
    window._verified_axis = None  # as on a fresh start: direction not yet seen
    asked = []
    monkeypatch.setattr(window, "_ask_yes_no", lambda q, **kw: asked.append(q) or True)
    try:
        window.autofocus()
        settle(window)
        assert window.status.cget("text") == "Stopped" and "invert_direction" in window.detail.cget("text")
        assert board.sensor_mm == 202.0 and window.motion_fault  # only the 3 mm probe went wrong
        assert asked == []
        board.z_reverse = True  # fixed
        window.autofocus()
        settle(window)
        assert asked and "did not move as expected" in asked[0]  # asked because of the earlier fault
        assert window.status.cget("text") == "In focus" and not window.motion_fault
    finally:
        board.z_reverse = True
        window.motion_fault = False
        window._verified_axis = None


def test_button_acts_on_key_release_of_its_own_press(window):
    calls = []
    window.button.command = lambda: calls.append(1)
    try:
        window.button._key_release(None)  # release without a press here (e.g. Enter from a dialog)
        assert calls == []
        window.button._key_press(None)
        window.button._key_release(None)
        assert calls == [1]
    finally:
        window.button.command = window.autofocus


def test_lens_picker_is_locked_while_z_may_move(window, monkeypatch):
    seen = []
    real = window.lens_picker.set_enabled
    monkeypatch.setattr(window.lens_picker, "set_enabled", lambda e: seen.append(e) or real(e))
    window.autofocus()
    settle(window)
    assert seen[:1] == [False] and seen[-1] is True and window.lens_picker.enabled


def test_focus_ring_shows_on_the_lens_picker(window):
    from omni_autofocus import ui

    label = window.lens_picker._labels["A"]
    label.event_generate("<FocusIn>")
    assert label.cget("highlightbackground") == window.palette.text
    label.event_generate("<FocusOut>")
    assert label.cget("highlightbackground") == window.palette.bg
    assert callable(ui.focus_ring)


# --- fine-tuning in the window ------------------------------------------------------------------------


def board_of(window):
    return window._session.open()[0].__enter__()


def start_tune(window):
    window.open_fine_tune()
    assert window.tune["stage"] == "intro" and window.status.cget("text") == "Fine-tune focus"
    window._tune_primary()  # Start: autofocus, then go to the first mark
    settle(window)
    assert window.tune["stage"] == "burn" and window.tune["index"] == 0


def test_fine_tune_full_run_saves_the_best_focus(window):
    board = board_of(window)
    start_tune(window)
    reference = window.tune["run"].reference
    assert board.sensor_mm == pytest.approx(reference - 4.0, abs=0.01)
    assert window.t_big.cget("text") == "−4 mm" and window.status.cget("text") == "Burn mark 1 of 9"
    window.autofocus()  # the Autofocus view is paused while fine-tuning
    assert "Finish or stop fine-tuning" in window.detail.cget("text")
    for _ in range(9):
        window._tune_primary()
        settle(window)
    assert window.tune["stage"] == "result" and window.tune["dev"] is None  # laser released
    assert board.sensor_mm == pytest.approx(reference, abs=0.01)  # back at focus
    window.t_low.select("−2", notify=True)
    window.t_high.select("+3", notify=True)
    assert "150 mm (B) lens focus height 222.0 → 222.5 mm" in window.t_outcome.cget("text")
    window._tune_save()
    settle(window)
    assert window.tune is None and window.status.cget("text") == "Focus saved"
    assert config.load(window._session.path).focus.target_b_mm == 222.5
    config.save(config.Settings(app=config.AppSettings(hotkey="", sounds=False)), window._session.path)


def test_fine_tune_stop_returns_z_to_focus(window):
    board = board_of(window)
    start_tune(window)
    reference = window.tune["run"].reference
    window.events.put(("hotkey",))  # the hotkey means Next while fine-tuning
    settle(window)
    assert window.tune["index"] == 1
    window.close()  # refused mid-run
    assert window.root.winfo_exists() and "Stop fine-tuning first" in window.detail.cget("text")
    window._tune_secondary()  # Stop and return Z to focus
    settle(window)
    assert window.tune is None and board.sensor_mm == pytest.approx(reference, abs=0.01)
    assert "back at the focus height" in window.detail.cget("text")


def test_fine_tune_waits_while_lightburn_is_busy(window, monkeypatch):
    from omni_autofocus.controller import Controller, ControllerBusyError

    start_tune(window)
    real_move = Controller.move_axis
    state = {"busy": True}

    def busy_once(self, params, pulses, **kw):
        if state["busy"]:
            state["busy"] = False
            raise ControllerBusyError("busy")
        return real_move(self, params, pulses, **kw)

    monkeypatch.setattr(Controller, "move_axis", busy_once)
    window._tune_primary()
    settle(window)
    assert window.tune["index"] == 0 and window.status.cget("text") == "Laser busy"
    window._tune_primary()
    settle(window)
    assert window.tune["index"] == 1
    window._tune_secondary()
    settle(window)
    assert window.tune is None


def test_fine_tune_fault_reports_where_z_is_and_moves_nothing_more(window, monkeypatch):
    from omni_autofocus.controller import Controller, SensorNoTargetError

    board = board_of(window)
    start_tune(window)
    real_read = Controller.read_height_median

    def no_target(self, samples=3):
        raise SensorNoTargetError("no surface")

    monkeypatch.setattr(Controller, "read_height_median", no_target)
    lists_before = len(board.lists)
    window._tune_primary()  # Next: moves to -3, then the reading fails
    settle(window)
    monkeypatch.setattr(Controller, "read_height_median", real_read)
    assert window.tune is None and window.status.cget("text") == "Stopped"
    assert "Where Z is now is unknown" in window.detail.cget("text")  # the sensor failed: no guessing
    assert len(board.lists) == lists_before + 1  # the one step, no automatic return
    window.motion_fault = False
    window._verified_axis = None
    board.sensor_mm = 205.0


def test_fine_tune_busy_during_the_approach_keeps_the_run(window, monkeypatch):
    from omni_autofocus import ladder
    from omni_autofocus.controller import ControllerBusyError

    board = board_of(window)
    real_move = ladder.Ladder._move
    armed = {"up": True}

    def busy_on_the_way_up(self, mm):  # the approach is -5 mm, then +1 mm: fail the second half once
        if armed["up"] and mm > 0:
            armed["up"] = False
            raise ControllerBusyError("busy")
        return real_move(self, mm)

    monkeypatch.setattr(ladder.Ladder, "_move", busy_on_the_way_up)
    window.open_fine_tune()
    window.events.put(("hotkey",))  # the hotkey does not start a run from the intro
    settle(window)
    assert window.tune["stage"] == "intro" and "Press Start" in window.detail.cget("text")
    window._tune_primary()
    settle(window)
    reference = window.tune["run"].reference
    assert window.tune.get("pending") and window.status.cget("text") == "Laser busy"
    assert "do not burn" in window.t_sub.cget("text") and window.t_hint.cget("text").strip() == ""
    assert window.t_button.text == "Continue"
    assert board.sensor_mm == pytest.approx(reference - 5.0, abs=0.01)  # below the first mark, run kept
    window._tune_primary()  # Next retries the approach
    settle(window)
    assert not window.tune.get("pending") and window.tune["index"] == 0
    assert board.sensor_mm == pytest.approx(reference - 4.0, abs=0.01)
    window._tune_secondary()
    settle(window)
    assert window.tune is None and board.sensor_mm == pytest.approx(reference, abs=0.01)


def test_fine_tune_result_ignores_sensor_noise(window):
    start_tune(window)
    window.tune["run"].reference = 221.9  # the reading after autofocus wobbled by 0.1 mm
    window.tune["stage"] = "result"
    window._tune_render()
    window.t_low.select("0", notify=True)
    window.t_high.select("0", notify=True)
    assert "matches the saved height (222.0 mm)" in window.t_outcome.cget("text")
    assert not window.t_save.enabled and window.t_save.text == "Nothing to save"
    window.tune["stage"] = "burn"
    window._tune_secondary()  # Stop: back to focus
    settle(window)
    assert window.tune is None


def test_lens_summary_for_the_commarker_fallback_does_not_claim_lightburn():
    why = (
        "lens B (from ComMarker Studio's lens setting; several BSL profiles in LightBurn, which does not "
        "record the one in use)"
    )
    assert app.lens_summary("Auto", why, 222.0) == "Auto → 150 mm (B) · ComMarker Studio · 222.0 mm"


def test_picker_shows_lens_sizes_with_the_letters(window):
    texts = [lbl.cget("text") for lbl in window.lens_picker._labels.values()]
    assert texts == ["Auto", "70 mm (A)", "150 mm (B)"]
    focus = config.FocusSettings(field_a_mm=110.0, field_b_mm=200.0)
    assert app.lens_labels(focus) == {"Auto": "Auto", "A": "110 mm (A)", "B": "200 mm (B)"}


# --- settings in the window ----------------------------------------------------------------------------


@pytest.fixture
def settings_window(window):
    path = window._session.path
    saved = path.read_text(encoding="utf-8") if path.exists() else None
    yield window
    window._close_settings()
    if saved is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(saved, encoding="utf-8")


def test_settings_view_edits_and_saves(settings_window):
    w = settings_window
    w.open_settings_view()
    assert w.settings_open and w.s_heights["B"].get() == "222.0" and w.s_offset.get() == "0"
    w.autofocus()  # paused while Settings is open
    assert "Save or cancel Settings first" in w.detail.cget("text")
    w._set_entry(w.s_offset, "0.3")
    w._set_entry(w.s_confirm, "15")
    w.s_invert.set(True)
    w._settings_save()
    settle(w)
    saved = config.load(w._session.path)
    assert saved.focus.offset_mm == 0.3 and saved.app.confirm_down_above_mm == 15.0
    assert saved.z_axis.invert_direction and not w.settings_open
    assert w.status.cget("text") == "Settings saved"


def test_settings_view_rejects_bad_values_and_keeps_the_file(settings_window):
    w = settings_window
    before = w._session.path.read_text(encoding="utf-8")
    for entry, text, expect in (
        (w.s_offset, "abc", "'abc' is not a number"),
        (w.s_heights["B"], "35", "between 120 and 280 mm"),
        (w.s_hotkey, "ctrl+", "Hotkey"),
        (w.s_confirm, "500", "out of range"),
    ):
        w.open_settings_view()
        w._set_entry(entry, text)
        w._settings_save()
        assert w.settings_open and w.status.cget("text") == "Not saved", text
        assert expect in w.detail.cget("text"), (text, w.detail.cget("text"))
        w._close_settings()
    assert w._session.path.read_text(encoding="utf-8") == before


def test_settings_factory_and_current_height_fill_the_fields(settings_window):
    w = settings_window
    board = board_of(w)
    w.open_settings_view()
    w._set_entry(w.s_heights["A"], "")
    w._settings_factory("A")
    settle(w)
    assert w.s_heights["A"].get() == "181.0" and "Factory value" in w.detail.cget("text")
    board.sensor_mm = 223.4
    w._settings_here("B")
    settle(w)
    assert w.s_heights["B"].get() == "223.4"
    board.sensor_mm = 205.0


def test_settings_without_a_file_need_both_heights(settings_window):
    w = settings_window
    w._session.path.unlink()
    w.open_settings_view()
    assert w.s_heights["A"].get() == "" and "No focus heights saved yet" in w.detail.cget("text")
    w._settings_save()
    assert "enter both focus heights" in w.detail.cget("text").lower()
    w._settings_factory("A")
    settle(w)
    w._settings_factory("B")
    settle(w)
    w._settings_save()
    settle(w)
    assert config.load(w._session.path).focus.target_b_mm == 222.0


def test_driver_guidance_in_the_app_points_to_the_menu():
    from omni_autofocus import driver
    from omni_autofocus.driver import UsbDevice

    diag = driver.diagnose([UsbDevice((r"USB\VID_04B4&PID_1004",), "x", "", 28)])
    lines = driver.guidance(diag, staged=True, installer=None, check_again=driver.CHECK_AGAIN_APP)
    assert lines[-1].startswith("Then use ⋯ → Check USB driver") and "omni-autofocus" not in " ".join(lines)


def test_settings_blocks_fine_tuning(settings_window):
    w = settings_window
    w.open_settings_view()
    w.open_fine_tune()
    assert w.tune is None and w.settings_open


def test_menu_greys_out_what_cannot_start_in_the_current_view(settings_window):
    w = settings_window

    def check(free: bool) -> None:
        w._update_menu()
        state = {label: str(w.menu.entrycget(label, "state")) for label in w.TASK_ENTRIES}
        assert set(state.values()) == {"normal" if free else "disabled"}, state
        assert str(w.menu.entrycget("Always on top", "state")) == "normal"
        assert str(w.menu.entrycget("About", "state")) == "normal"

    check(free=True)
    w.open_settings_view()
    check(free=False)
    w._close_settings()
    check(free=True)
    w.open_fine_tune()
    check(free=False)
    w._close_tune()
    w.busy = True  # a task running in the main view
    check(free=False)
    w.busy = False
    check(free=True)


def test_settings_check_height_plus_nudge(settings_window):
    w = settings_window
    w.open_settings_view()
    w._set_entry(w.s_heights["B"], "279")
    w._set_entry(w.s_offset, "20")
    w._settings_save()
    assert w.status.cget("text") == "Not saved" and "with the +20 mm nudge" in w.detail.cget("text")


def test_settings_keep_untouched_values_exactly_and_allow_a_placeholder_lens(settings_window):
    w = settings_window
    path = w._session.path
    s = config.load(path)
    config.save(
        config.Settings(focus=config.FocusSettings(target_a_mm=0.0, target_b_mm=222.25), app=s.app), path
    )
    w.open_settings_view()
    assert w.s_heights["B"].get() == "222.25"
    w._set_entry(w.s_hotkey, "ctrl+shift+k")  # change something else only
    w._settings_save()
    settle(w)
    saved = config.load(path)
    assert saved.focus.target_a_mm == 0.0 and saved.focus.target_b_mm == 222.25
    assert saved.app.hotkey == "ctrl+shift+k"


def test_settings_reject_one_modifier_hotkeys(settings_window):
    w = settings_window
    for bad in ("shift+a", "ctrl+c", "ctrl+ctrl+f"):
        w.open_settings_view()
        w._set_entry(w.s_hotkey, bad)
        w._settings_save()
        assert w.status.cget("text") == "Not saved" and "two" in w.detail.cget("text"), bad
        w._close_settings()


def test_first_settings_file_builds_on_the_lasers_calibration(settings_window, monkeypatch):
    from omni_autofocus import session as session_mod

    w = settings_window
    cfg = {
        "lmcPars": {
            "params": [{"parName": "default", "fBestFocalDistance": 180.0, "fBestFocalDistance_B": 221.0}]
        },
        "extMarkerPar": {
            "axisZParExt": {
                "setting": {"axisId": 1, "bRevRot": True, "pitchPulse": 6400, "screwPitch": 4.0,
                            "maxRunSpeed": 320.0, "gearRatio": 1.0},
                "runData": {"startSpeed": 0.0, "runSpeed": 8.0, "accSpeed": 5.0},
            }
        },
    }  # fmt: skip
    monkeypatch.setattr(session_mod, "read_laser_calibration", lambda ctl: cfg)
    w._session.path.unlink()
    w.open_settings_view()
    w._settings_factory("A")
    settle(w)
    w._settings_factory("B")
    settle(w)
    w._settings_save()
    settle(w)
    saved = config.load(w._session.path)
    assert saved.z_axis.pitch_pulse == 6400 and saved.focus.target_b_mm == 221.0


def test_settings_reading_after_cancel_is_ignored(settings_window):
    w = settings_window
    w.open_settings_view()
    w._settings_factory("A")
    w._close_settings()  # Cancel while the laser is being read
    settle(w)
    assert "Press Save" not in w.detail.cget("text") and not w.settings_open


def test_broken_settings_file_offers_to_open_it(settings_window, monkeypatch):
    w = settings_window
    w._session.path.write_text("[focus]\n", encoding="utf-8")  # no focus heights
    asked = []
    monkeypatch.setattr(w, "_ask_yes_no", lambda q, **kw: asked.append(q) or False)
    w.open_settings_view()
    assert not w.settings_open and asked and "has a problem" in asked[0]
    assert w.status.cget("text") == "Settings file problem"


def test_settings_file_writes_are_atomic_and_empty_files_are_refused(tmp_path):
    path = tmp_path / "s.toml"
    config.save(config.Settings(), path)
    assert path.exists() and not (tmp_path / "s.toml.tmp").exists()
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no focus heights"):
        config.load(path)


def _laser_cfg(a: float, b: float, pitch: int = 6400) -> dict:
    return {
        "lmcPars": {"params": [{"parName": "default", "fBestFocalDistance": a, "fBestFocalDistance_B": b}]},
        "extMarkerPar": {
            "axisZParExt": {
                "setting": {"axisId": 1, "bRevRot": True, "pitchPulse": pitch, "screwPitch": 4.0,
                            "maxRunSpeed": 320.0, "gearRatio": 1.0},
                "runData": {"startSpeed": 0.0, "runSpeed": 8.0, "accSpeed": 5.0},
            }
        },
    }  # fmt: skip


def test_first_save_accepts_a_placeholder_lens_from_the_laser(settings_window, monkeypatch):
    from omni_autofocus import session as session_mod

    w = settings_window
    monkeypatch.setattr(session_mod, "read_laser_calibration", lambda ctl: _laser_cfg(0.0, 221.0))
    w._session.path.unlink()
    w.open_settings_view()
    for letter in "AB":
        w._settings_factory(letter)
        settle(w)
    w._settings_save()
    settle(w)
    saved = config.load(w._session.path)
    assert saved.focus.target_a_mm == 0.0 and saved.focus.target_b_mm == 221.0


def test_first_save_with_typed_heights_still_uses_the_lasers_z_axis(settings_window, monkeypatch):
    from omni_autofocus import session as session_mod

    w = settings_window
    monkeypatch.setattr(session_mod, "read_laser_calibration", lambda ctl: _laser_cfg(180.0, 221.0))
    w._session.path.unlink()
    w.open_settings_view()
    w._set_entry(w.s_heights["A"], "180.5")
    w._set_entry(w.s_heights["B"], "222.5")
    w._settings_save()  # no Factory pressed: the save reads the laser itself
    settle(w)
    saved = config.load(w._session.path)
    assert saved.z_axis.pitch_pulse == 6400
    assert (saved.focus.target_a_mm, saved.focus.target_b_mm) == (180.5, 222.5)


def test_old_one_modifier_hotkey_is_reported_as_invalid(settings_window):
    w = settings_window
    s = config.load(w._session.path)
    from dataclasses import replace as dc_replace

    config.save(dc_replace(s, app=dc_replace(s.app, hotkey="ctrl+f")), w._session.path)
    w.open_settings_view()
    w._set_entry(w.s_offset, "   ")  # spaces only: means 0
    w._set_entry(w.s_confirm, "12")
    w.simulate = False  # the invalid key is rejected before anything is registered
    try:
        w._settings_save()
    finally:
        w.simulate = True
    settle(w)
    assert w.status.cget("text") == "Hotkey not valid" and "two" in w.detail.cget("text")
    assert config.load(w._session.path).app.confirm_down_above_mm == 12.0


def test_status_after_cancelled_reading_stays_on_the_main_view(settings_window):
    w = settings_window
    w.open_settings_view()
    w._settings_here("B")
    w._close_settings()
    settle(w)
    assert w.status.cget("text") == "Ready"  # neither "Measuring…" nor "Settings" leaks through


def test_first_save_through_the_laser_finishes_cleanly_and_compares_numbers(settings_window, monkeypatch):
    from omni_autofocus import session as session_mod

    w = settings_window
    monkeypatch.setattr(session_mod, "read_laser_calibration", lambda ctl: _laser_cfg(0.0, 221.0))
    w._session.path.unlink()
    w.open_settings_view()
    w._set_entry(w.s_heights["A"], "0")  # typed, equal to the laser's placeholder 0.0
    w._set_entry(w.s_heights["B"], "221")  # equal to the laser's 221.0
    w._settings_save()
    settle(w)
    assert w.status.cget("text") == "Settings saved" and "Still working" not in w.detail.cget("text")
    saved = config.load(w._session.path)
    assert saved.focus.target_a_mm == 0.0 and saved.focus.target_b_mm == 221.0


def test_settings_reading_errors_show_in_settings_only(settings_window, monkeypatch):
    from omni_autofocus import session as session_mod
    from omni_autofocus.controller import ControllerError

    def broken(ctl):
        raise ControllerError("no valid reply to command 0xAAE0")

    monkeypatch.setattr(session_mod, "read_laser_calibration", broken)
    w = settings_window
    w.open_settings_view()
    w._settings_factory("A")
    settle(w)
    assert w.settings_open and w.status.cget("text") == "Stopped" and "0xAAE0" in w.detail.cget("text")
    w._settings_factory("A")
    w._close_settings()  # Cancel while it runs: the error must not reach the main view
    settle(w)
    assert w.status.cget("text") == "Ready"
