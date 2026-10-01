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


def test_describe_error_is_actionable():
    assert "framing preview" in app.describe_error(ControllerBusyError("busy"))
    assert "Z buttons" in app.describe_error(SensorNoTargetError("x"))
    assert app.describe_error(session.ConflictError("ComMarker Studio is running")).startswith("ComMarker")
    assert app.describe_error(autofocus.FocusError("required move too big")) == "Required move too big."


def test_describe_result():
    p1, p2 = plan(17.0), plan(0.0)
    moved = autofocus.FocusResult(autofocus.Outcome.IN_FOCUS, 222.0, 222.0, (p1, p2), True)
    assert app.describe_result(moved) == (
        "In focus",
        "Moved +17.0 mm · sensor 222.0 mm, target 222.0 mm",
        app.OK,
    )
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
    path = tmp_path_factory.mktemp("app") / "app.toml"
    config.save(config.Settings(app=config.AppSettings(hotkey="", sounds=False)), path)
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
    assert "Lens B" in window.lens_info.cget("text")
    window.autofocus()
    settle(window)
    assert window.status.cget("text") == "In focus"
    assert window.detail.cget("text").startswith("Moved +17.0 mm")
    window.autofocus()  # the simulated laser keeps its Z position
    settle(window)
    assert window.status.cget("text") == "Already in focus"


def test_window_asks_before_a_large_downward_move(window, monkeypatch):
    board = window._session.open()[0].__enter__()
    board.sensor_mm = 240.0  # 18 mm too far: the head would move down
    asked = []
    monkeypatch.setattr(window, "_ask_yes_no", lambda q: asked.append(q) or False)
    window.autofocus()
    settle(window)
    assert asked and "DOWN 18.0 mm" in asked[0]
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


def test_window_cannot_close_while_busy(window, monkeypatch):
    window.busy = True
    try:
        window.close()
        assert window.root.winfo_exists() and "Wait until Z has stopped" in window.detail.cget("text")
    finally:
        window.busy = False


def test_bad_event_does_not_stop_the_window(window):
    window.events.put(("lens",))  # malformed: missing text
    window.events.put(("status", "still alive", app.INFO))
    settle(window)
    assert window.status.cget("text") == "still alive"


def test_first_run_shows_the_calibration_and_can_cancel(window, monkeypatch, tmp_path):
    fresh = tmp_path / "fresh.toml"
    monkeypatch.setattr(window._session, "path", fresh)
    asked = []
    monkeypatch.setattr(window, "_ask_yes_no", lambda q: asked.append(q) or False)
    window.autofocus()
    settle(window)
    assert "First run" in asked[0] and "lens B 222.0 mm" in asked[0]
    assert window.status.cget("text") == "Cancelled"
    board = window._session.open()[0].__enter__()
    assert board.sensor_mm == 205.0


def test_lens_summary():
    why = "lens B (150 mm field, from the only BSL profile in LightBurn, 'BSLFiber')"
    assert app.lens_summary(why, 222.0) == "Lens B · from LightBurn profile 'BSLFiber' · focus 222.0 mm"
    assert app.lens_summary("lens A (set explicitly)", None) == (
        "Lens A · chosen here · focus read from the laser on first use"
    )
    assert app.lens_summary("lens A (from ComMarker Studio's lens setting; x)", 181.0).startswith(
        "Lens A · from ComMarker Studio"
    )
