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


def test_run_and_cli_commands(tmp_path):
    exe = tmp_path / "OmniAutofocus.exe"
    assert app.run_command(True, str(exe)) == f'"{exe}"'
    assert app.cli_command(True, str(exe)) is None
    (tmp_path / "omni-autofocus.exe").write_bytes(b"")
    assert app.cli_command(True, str(exe)) == [str(tmp_path / "omni-autofocus.exe")]
    assert app.run_command(False, str(tmp_path / "python.exe")).endswith("-m omni_autofocus.app")


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
    assert window.lens_info.cget("text").startswith("Auto → B")
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
    path.write_text("[app]\nhotkey = true\n")
    with pytest.raises(ValueError, match="app.hotkey must be str"):
        config.load(path)
    path.write_text("[focus]\noffset_mm = 1\nsamples = 5\n")  # ints are fine for float settings
    s = config.load(path)
    assert s.focus.offset_mm == 1.0 and isinstance(s.focus.offset_mm, float) and s.focus.samples == 5
    path.write_text("[focus]\nsamples = 2.5\n")
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
    assert "focus heights stored in your laser" in asked[0] and "Lens B (150 mm field): 222.0 mm" in asked[0]
    assert window.status.cget("text") == "Not saved"
    assert not fresh.exists()  # declined: nothing saved
    board = window._session.open()[0].__enter__()
    assert board.sensor_mm == 205.0


def test_lens_summary():
    why = "lens B (150 mm field, from the only BSL profile in LightBurn, 'BSLFiber')"
    assert app.lens_summary("Auto", why, 222.0) == "Auto → B · LightBurn 'BSLFiber' · 222.0 mm"
    assert app.lens_summary("A", "lens A (set explicitly)", None) == "Lens A · focus read on first use"
    assert app.lens_summary("Auto", "lens A (from ComMarker Studio's lens setting; x)", 181.0) == (
        "Auto → A · ComMarker Studio · 181.0 mm"
    )


def test_updown():
    assert app.updown(17.0) == "up 17.0 mm" and app.updown(-3.04) == "down 3.0 mm"


def test_rounded_button_image_is_a_valid_png():
    from omni_autofocus import ui

    png = ui.rounded_rect_png(40, 20, 6, "#1859a0", "#ffffff", ring="#ffffff")
    assert png.startswith(b"\x89PNG") and b"IEND" in png


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


def test_autofocus_is_paused_while_fine_tuning_runs(window):
    class Running:
        def poll(self):
            return None

    window._ladder = Running()
    try:
        window.autofocus()
        assert not window.busy and "Fine-tuning is using the laser" in window.detail.cget("text")
    finally:
        window._ladder = None


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
