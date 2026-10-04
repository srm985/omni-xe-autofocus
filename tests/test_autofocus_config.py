from dataclasses import replace

import pytest

from omni_autofocus import autofocus, config, lens


def lens_b() -> config.Settings:
    s = config.Settings()
    return replace(s, focus=replace(s.focus, lens="b"))


def test_defaults_match_commarker_install():
    s = lens_b()
    assert s.focus.target_mm == 222.0
    assert s.z_axis.axis_params().pulses_per_mm == 800.0


def test_plan_positive_and_negative_moves():
    s = lens_b()
    up = autofocus.plan(200.0, s)
    assert up.move_mm == pytest.approx(22.0) and up.pulses == 17600
    down = autofocus.plan(230.0, s)
    assert down.move_mm == pytest.approx(-8.0) and down.pulses == -6400


def test_plan_deadband():
    p = autofocus.plan(222.05, lens_b())
    assert not p.needed and p.pulses == 0


def test_plan_lens_a_and_offset():
    s = config.Settings()
    s = replace(s, focus=replace(s.focus, lens="a", offset_mm=1.5))
    assert autofocus.plan(180.0, s).move_mm == pytest.approx(2.5)


@pytest.mark.parametrize("height", [119.9, 280.1])
def test_plan_rejects_out_of_range(height):
    with pytest.raises(autofocus.FocusError):
        autofocus.plan(height, config.Settings())


def test_plan_respects_safety_limit():
    s = lens_b()
    s = replace(s, focus=replace(s.focus, max_move_mm=10))
    with pytest.raises(autofocus.FocusError):
        autofocus.plan(150.0, s)


def test_invert_direction_flips_reverse():
    s = config.Settings()
    assert s.z_axis.axis_params().reverse is True
    assert replace(s.z_axis, invert_direction=True).axis_params().reverse is False


COMMARKER_SAMPLE = {
    "lmcPars": {
        "params": [
            {
                "parName": "default",
                "IsGALVO_B": False,
                "fBestFocalDistance": 180.5,
                "fBestFocalDistance_B": 221.0,
                "fMinDistanceOfSensor": 110.0,
                "fMaxDistanceOfSensor": 290.0,
            }
        ]
    },
    "extMarkerPar": {
        "axisZParExt": {
            "setting": {
                "axisId": 1,
                "bRevRot": True,
                "pitchPulse": 3200,
                "screwPitch": 4.0,
                "maxRunSpeed": 320.0,
                "gearRatio": 1.0,
            },
            "runData": {"startSpeed": 0.0, "runSpeed": 8.0, "accSpeed": 5.0},
        }
    },
}


def test_commarker_cfg_roundtrip_and_import():
    raw = config.encode_commarker_cfg(COMMARKER_SAMPLE)
    assert raw[8:10] != b'{"'  # obfuscated
    decoded = config.decode_commarker_cfg(raw)
    assert decoded == COMMARKER_SAMPLE
    s = config.from_commarker(decoded)
    assert s.focus.lens == "auto" and config.commarker_lens(decoded) == "a"
    assert replace(s.focus, lens="a").target_mm == 180.5
    assert s.focus.sensor_min_mm == 110.0 and s.z_axis.screw_pitch == 4.0


def test_settings_file_roundtrip(tmp_path):
    s = config.Settings()
    s = replace(s, focus=replace(s.focus, lens="a", offset_mm=-0.25))
    path = tmp_path / "config.toml"
    config.save(s, path)
    assert config.load(path) == s


def test_settings_file_rejects_typos(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[focus]\ntarget_mmm = 3\n")
    with pytest.raises(ValueError, match="target_mmm"):
        config.load(path)


def test_cli_simulated_focus(capsys):
    from omni_autofocus.cli import main

    assert main(["--simulate", "--lens", "b", "focus", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "move +17.000 mm" in out and "In focus (error +0.000 mm)" in out


def test_cli_dry_run_does_not_move(capsys):
    from omni_autofocus.cli import main

    assert main(["--simulate", "focus", "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_no_arguments_runs_focus_and_pauses(monkeypatch):
    from omni_autofocus import cli

    calls = []
    monkeypatch.setattr(cli.sys, "argv", ["omni-autofocus.exe"])
    monkeypatch.setattr(cli, "_run", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli, "_owns_console", lambda: True)
    monkeypatch.setattr(cli, "_pause", lambda code: calls.append(("paused", code)))
    assert cli.main() == 0
    assert calls == [["focus"], ("paused", 0)]


def test_shortcut_with_arguments_pauses_too_even_on_usage_errors(monkeypatch):
    from omni_autofocus import cli

    paused = []
    monkeypatch.setattr(cli.sys, "argv", ["omni-autofocus.exe", "focus", "--bogus"])
    monkeypatch.setattr(cli, "_owns_console", lambda: True)
    monkeypatch.setattr(cli, "_pause", paused.append)
    assert cli.main() == 2
    assert paused == [2]


def test_no_pause_when_run_from_a_terminal(monkeypatch):
    from omni_autofocus import cli

    monkeypatch.setattr(cli.sys, "argv", ["omni-autofocus", "--simulate", "focus", "--dry-run"])
    monkeypatch.setattr(cli, "_owns_console", lambda: False)
    monkeypatch.setattr(cli, "_pause", lambda code: pytest.fail("paused"))
    assert cli.main() == 0


# --- automatic lens selection -------------------------------------------------------------------------


def write_prefs(tmp_path, devices, default=0):
    import json

    prefs = {"DefaultDevice": default, "DeviceList": devices}
    path = tmp_path / "LightBurn" / "prefs.ini"
    path.parent.mkdir()
    path.write_text(json.dumps(prefs), encoding="utf-8")
    return path


def dev(name, profile, size):
    return {"Name": name, "ProfilePath": profile, "Width": size, "Height": size}


def test_lens_explicit_wins(tmp_path):
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="a"))
    resolved, why = lens.resolve(s, lightburn_prefs=tmp_path / "missing")
    assert resolved.focus.lens == "a" and "explicitly" in why


def test_lens_from_users_lightburn_layout(tmp_path):
    # The real layout seen on the first user's PC: the default device is a JCZ one, one BSL profile.
    prefs = write_prefs(
        tmp_path,
        [dev("JCZFiber", "JCZFiber", 150), dev("JCZFiber", "JCZFiber", 70), dev("BSLFiber", "BSLFiber", 150)],
    )
    resolved, why = lens.resolve(config.Settings(), lightburn_prefs=prefs)
    assert resolved.focus.lens == "b" and "BSLFiber" in why and "only BSL" in why


def test_lens_from_last_used_bsl_profile(tmp_path):
    prefs = write_prefs(tmp_path, [dev("Omni B", "BSLFiber", 150), dev("Omni A", "BSLFiber", 70)], default=1)
    resolved, why = lens.resolve(config.Settings(), lightburn_prefs=prefs)
    assert resolved.focus.lens == "a" and "Omni A" in why


def test_lens_unmatched_field_falls_back_to_commarker(tmp_path):
    prefs = write_prefs(tmp_path, [dev("BSL odd", "BSLFiber", 110)])
    cm = tmp_path / "cm" / "config"
    cm.mkdir(parents=True)
    (cm / "lcsparam.cfg").write_bytes(config.encode_commarker_cfg(COMMARKER_SAMPLE))
    resolved, why = lens.resolve(config.Settings(), lightburn_prefs=prefs, commarker_dir=tmp_path / "cm")
    assert resolved.focus.lens == "a" and "ComMarker" in why and "110 mm" in why


def test_lens_unknown_is_an_error(tmp_path):
    with pytest.raises(lens.LensError, match="--lens"):
        lens.resolve(config.Settings(), lightburn_prefs=tmp_path / "missing")


def test_lens_field_tolerance():
    f = config.FocusSettings()
    assert lens.lens_for_field(150, f) == "b" and lens.lens_for_field(72, f) == "a"
    assert lens.lens_for_field(110, f) is None


def test_commarker_import_reads_field_sizes():
    sample = json_copy(COMMARKER_SAMPLE)
    params = sample["lmcPars"]["params"][0]
    params["galvoParam"] = {"workSize": {"x": 60.0, "y": 60.0}}
    params["galvo2Param"] = {"workSize": {"x": 110.0, "y": 110.0}}
    s = config.from_commarker(sample)
    assert (s.focus.field_a_mm, s.focus.field_b_mm) == (60.0, 110.0)


def json_copy(obj):
    import json

    return json.loads(json.dumps(obj))


def scripted(answers: dict):
    """A _prompt replacement answering by keyword; burn prompts get Enter."""
    seen = []

    def prompt(text):
        seen.append(text)
        for key, value in answers.items():
            if key in text:
                return value
        return ""

    return prompt, seen


def test_cli_focus_ladder_default_range_ascending_and_returns(monkeypatch, capsys):
    from omni_autofocus import cli

    prompt, seen = scripted({"Lowest": ""})
    monkeypatch.setattr(cli, "_prompt", prompt)
    assert cli.main(["--simulate", "--lens", "b", "focus-ladder", "--yes"]) == 0
    out = capsys.readouterr().out
    labels = [p.split("labelled '")[1].split("'")[0] for p in seen if "labelled" in p]
    assert labels == ["-4", "-3", "-2", "-1", "+0", "+1", "+2", "+3", "+4"]
    assert "Returning Z to the autofocus height (-4 mm)" in out
    assert "No result saved" in out


def test_cli_focus_ladder_dials_in_and_saves(monkeypatch, capsys, tmp_path):
    from omni_autofocus import cli

    cfg = tmp_path / "dial.toml"
    prompt, _ = scripted({"Lowest": "-2", "Highest": "+3", "Save": "y"})
    monkeypatch.setattr(cli, "_prompt", prompt)
    assert cli.main(["--simulate", "--config", str(cfg), "--lens", "b", "focus-ladder", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "centre +0.5 mm" in out and "Autofocus now targets 222.5 mm for lens B" in out
    saved = config.load(cfg)
    assert saved.focus.target_b_mm == 222.5
    assert saved.focus.target_a_mm == 181.0
    assert saved.focus.lens == "auto"  # the --lens override is not persisted


def test_cli_focus_ladder_keeps_offset_separate(monkeypatch, capsys, tmp_path):
    from omni_autofocus import cli

    cfg = tmp_path / "offset.toml"
    config.save(config.Settings(focus=config.FocusSettings(offset_mm=0.5)), cfg)
    prompt, _ = scripted({"Lowest": "-2", "Highest": "+3", "Save": "y"})
    monkeypatch.setattr(cli, "_prompt", prompt)
    assert cli.main(["--simulate", "--config", str(cfg), "--lens", "b", "focus-ladder", "--yes"]) == 0
    # Autofocus lands on 222 + 0.5; best focus is 0.5 mm above that. The offset stays on top.
    saved = config.load(cfg)
    assert saved.focus.target_b_mm == 223.0 and saved.focus.offset_mm == 0.5


def test_cli_focus_warns_when_not_converged(monkeypatch, capsys):
    from omni_autofocus import cli
    from omni_autofocus.controller import Controller

    real_move = Controller.move_axis  # Z only travels 80 % of each move: plausible, but never converges
    monkeypatch.setattr(
        Controller, "move_axis", lambda self, params, pulses, **kw: real_move(self, params, int(pulses * 0.8))
    )
    assert cli.main(["--simulate", "--lens", "b", "focus", "--yes"]) == 3
    assert "Warning: still" in capsys.readouterr().out


def test_cli_focus_stops_when_z_does_not_follow(monkeypatch, capsys):
    from omni_autofocus import cli
    from omni_autofocus.controller import Controller

    real_move = Controller.move_axis  # stalled axis: the counter moves, the head does not
    monkeypatch.setattr(
        Controller, "move_axis", lambda self, params, pulses, **kw: real_move(self, params, 1)
    )
    assert cli.main(["--simulate", "--lens", "b", "focus", "--yes"]) == 2
    assert "Z did not move as expected" in capsys.readouterr().out


def test_reversed_z_direction_stops_after_the_first_move(tmp_path):
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=205.0, z_reverse=False)  # machine wired the other way round
    clock = FakeClock()
    ctl = Controller(board, sleep=clock.sleep, clock=clock)
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))
    with pytest.raises(autofocus.FocusError, match="invert_direction"):
        autofocus.run(ctl, s, confirm=lambda p: True)
    assert board.sensor_mm == pytest.approx(202.0)  # only the 3 mm probe went the wrong way


def test_check_motion_ignores_sensor_noise():
    from omni_autofocus import autofocus

    autofocus.check_motion(0.3, -0.2)  # tiny move inside the noise
    autofocus.check_motion(17.0, 16.6)
    for move, change in ((17.0, -17.0), (17.0, 3.0), (2.0, 5.0)):
        with pytest.raises(autofocus.FocusError):
            autofocus.check_motion(move, change)


def test_cli_focus_ladder_retries_when_controller_busy(monkeypatch, capsys):
    from omni_autofocus import cli
    from omni_autofocus.controller import Controller, ControllerBusyError

    real_move = Controller.move_axis
    state = {"busy_once": True}

    def flaky_move(self, params, pulses, **kw):
        if state["busy_once"] and pulses < 0:
            state["busy_once"] = False
            raise ControllerBusyError("busy")
        return real_move(self, params, pulses, **kw)

    monkeypatch.setattr(Controller, "move_axis", flaky_move)
    prompt, seen = scripted({"Lowest": ""})
    monkeypatch.setattr(cli, "_prompt", prompt)
    assert cli.main(["--simulate", "--lens", "b", "focus-ladder", "--yes", "--offsets", "0,2"]) == 0
    assert any("controller is busy" in p for p in seen)
    assert "Z left at" not in capsys.readouterr().out


def test_cli_focus_ladder_stop_early_still_returns(monkeypatch, capsys):
    from omni_autofocus import cli

    answers = iter(["", "q"])
    monkeypatch.setattr(cli, "_prompt", lambda text: next(answers))
    assert cli.main(["--simulate", "--lens", "b", "focus-ladder", "--yes", "--offsets", "0,-1,-2"]) == 0
    assert "Returning Z to the autofocus height (+1 mm)" in capsys.readouterr().out


# --- manual calibration: set-focus and config set ----------------------------------------------------


def run(*argv):
    from omni_autofocus.cli import main

    return main(["--simulate", *argv])


def test_set_focus_typed_value(tmp_path, capsys):
    cfg = tmp_path / "s.toml"
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "223.4", "-y") == 0
    saved = config.load(cfg)
    assert saved.focus.target_b_mm == 223.4 and saved.focus.target_a_mm == 181.0
    assert saved.focus.lens == "auto"


def test_set_focus_here_measures_current_height(tmp_path, capsys):
    cfg = tmp_path / "s.toml"
    assert run("--config", str(cfg), "--lens", "a", "set-focus", "--here", "-y") == 0
    assert "Measured sensor reading at the current Z: 205.000 mm" in capsys.readouterr().out
    assert config.load(cfg).focus.target_a_mm == 205.0


def test_set_focus_factory_restores_laser_value(tmp_path):
    cfg = tmp_path / "s.toml"
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "230", "-y") == 0
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "--factory", "-y") == 0
    assert config.load(cfg).focus.target_b_mm == 222.0


def test_set_focus_rejects_out_of_range_and_ambiguous(tmp_path, capsys):
    cfg = tmp_path / "s.toml"
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "30", "-y") == 2
    assert "not a lens-to-work distance" in capsys.readouterr().out
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "222", "--here") == 2


def test_set_focus_asks_before_saving(tmp_path, monkeypatch):
    from omni_autofocus import cli

    cfg = tmp_path / "s.toml"
    monkeypatch.setattr(cli, "_prompt", lambda text: "n")
    assert run("--config", str(cfg), "--lens", "b", "set-focus", "224") == 0
    assert config.load(cfg).focus.target_b_mm == 222.0  # first-run calibration only


def test_config_set_values_and_validation(tmp_path):
    cfg = tmp_path / "c.toml"
    assert run("--config", str(cfg), "config", "set", "focus.offset_mm", "0.3") == 1  # no file yet
    assert not cfg.exists()
    assert run("--config", str(cfg), "config", "init", "--from-laser") == 0
    assert run("--config", str(cfg), "config", "set", "focus.offset_mm", "0.3") == 0
    assert run("--config", str(cfg), "config", "set", "z_axis.invert_direction", "true") == 0
    assert run("--config", str(cfg), "config", "set", "focus.lens", "a") == 0
    s = config.load(cfg)
    assert s.focus.offset_mm == 0.3 and s.z_axis.invert_direction is True and s.focus.lens == "a"
    assert run("--config", str(cfg), "config", "set", "focus.offset", "1") == 1  # typo -> error
    assert run("--config", str(cfg), "config", "set", "focus.lens", "c") == 1


def test_simulate_never_writes_the_real_settings_file(tmp_path, monkeypatch):
    from omni_autofocus import cli

    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    assert cli.main(["--simulate", "--lens", "b", "focus", "--yes"]) == 0
    assert not config.default_config_path().exists()
    assert (tmp_path / "tmp" / "omni-autofocus-simulate.toml").exists()


def test_unexpected_error_keeps_the_console_open(monkeypatch, capsys):
    from omni_autofocus import cli

    def boom(argv):
        raise KeyError("surprise")

    paused = []
    monkeypatch.setattr(cli.sys, "argv", ["omni-autofocus.exe"])
    monkeypatch.setattr(cli, "_run", boom)
    monkeypatch.setattr(cli, "_owns_console", lambda: True)
    monkeypatch.setattr(cli, "_pause", paused.append)
    assert cli.main() == 1
    assert paused == [1] and "surprise" in capsys.readouterr().err


def test_probe_is_skipped_once_z_is_known_to_follow():
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=205.0)
    clock = FakeClock()
    ctl = Controller(board, sleep=clock.sleep, clock=clock)
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))
    first = autofocus.run(ctl, s)
    assert first.outcome is autofocus.Outcome.IN_FOCUS and first.verified
    probed = len(board.lists)
    assert probed == 2  # 3 mm probe, then the remaining 14 mm
    board.sensor_mm = 205.0
    autofocus.run(ctl, s, probe=False)
    assert len(board.lists) == probed + 1  # one 17 mm move


def test_settings_ranges_are_enforced(tmp_path):
    path = tmp_path / "s.toml"
    heights = "[focus]\ntarget_a_mm = 181.0\ntarget_b_mm = 222.0\n"
    for bad in (
        heights + "max_move_mm = nan\n",
        heights + "max_move_mm = 0.0\n",
        heights + "max_move_mm = 200.0\n",
        heights + "samples = 0\n",
        "[focus]\ntarget_a_mm = 181.0\ntarget_b_mm = -1.0\n",
        heights + "[z_axis]\npitch_pulse = -3200\n",
    ):
        path.write_text(bad)
        with pytest.raises(config.SettingsFileError, match="out of range"):
            config.load(path)
    with pytest.raises(ValueError, match="out of range"):
        config.set_value(config.Settings(), "focus.max_move_mm", "nan")


def test_retired_settings_are_accepted_and_dropped_on_save(tmp_path):
    path = tmp_path / "s.toml"
    heights = "[focus]\ntarget_a_mm = 181.0\ntarget_b_mm = 222.0\n"
    path.write_text(heights + "[app]\nconfirm_down_above_mm = 10.0\n")
    s = config.load(path)  # a file from 1.0.0 still loads
    config.save(s, path)
    assert "confirm_down_above_mm" not in path.read_text()
    path.write_text("[focus]\ntarget_a_mm = 181.0\ntarget_b_mm = 222.0\n[app]\nconfirm_down = 1.0\n")
    with pytest.raises(config.SettingsFileError, match="unknown setting"):
        config.load(path)  # anything else unknown is still a mistake


def test_cli_asks_once_per_autofocus(monkeypatch):
    from omni_autofocus import cli
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=182.0)  # 40 mm below focus
    clock = FakeClock()
    ctl = Controller(board, sleep=clock.sleep, clock=clock)
    real_move = Controller.move_axis

    def overshoot(self, params, pulses, **kw):  # the long move (not the 3 mm probe) travels 45 % too far
        return real_move(self, params, int(pulses * 1.45) if abs(pulses) > 2400 else pulses)

    monkeypatch.setattr(Controller, "move_axis", overshoot)
    asked = []
    monkeypatch.setattr(cli, "_confirm", lambda prompt, yes: asked.append(prompt) or True)
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))
    cli._autofocus(ctl, s, passes=2, dry_run=False, yes=False)
    # first move up (+40 mm), then the overshoot needs a large move down: that is part of the same run
    assert len(asked) == 1 and "+40.0 mm" in asked[0]


def test_focus_height_outside_the_sensor_range_only_blocks_that_lens(tmp_path):
    from omni_autofocus import autofocus

    s = config.Settings(focus=config.FocusSettings(target_a_mm=0.0))  # placeholder for an unfitted lens
    config.validate(s)
    b = replace(s, focus=replace(s.focus, lens="b"))
    assert autofocus.plan(205.0, b).move_mm == pytest.approx(17.0)
    a = replace(s, focus=replace(s.focus, lens="a"))
    with pytest.raises(autofocus.FocusError, match="outside the"):
        autofocus.plan(205.0, a)
    off = replace(s, focus=replace(s.focus, lens="b", offset_mm=-20.0, target_b_mm=121.0))
    with pytest.raises(autofocus.FocusError, match="offset included"):
        autofocus.plan(205.0, off)


def test_probe_with_wrong_pitch_stops_before_the_long_move():
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=182.0, z_pulses_per_mm=800 / 1.45)  # settings say 800, really 552
    clock = FakeClock()
    ctl = Controller(board, sleep=clock.sleep, clock=clock)
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))
    with pytest.raises(autofocus.MotionError, match="pitch"):
        autofocus.run(ctl, s)
    assert board.sensor_mm == pytest.approx(182.0 + 3 * 1.45, abs=0.01)  # only the probe ran


def test_reading_failure_after_a_move_is_a_motion_fault(monkeypatch):
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller, SensorNoTargetError
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=205.0)
    clock = FakeClock()
    ctl = Controller(board, sleep=clock.sleep, clock=clock)
    real_read = Controller.read_height_median
    calls = {"n": 0}

    def read(self, samples=3):
        calls["n"] += 1
        if calls["n"] > 1:
            raise SensorNoTargetError("no surface")
        return real_read(self, samples)

    monkeypatch.setattr(Controller, "read_height_median", read)
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))
    with pytest.raises(autofocus.MotionError, match="could not measure"):
        autofocus.run(ctl, s)


def test_cli_survives_unencodable_output(monkeypatch, capsys):
    import io
    import sys

    from omni_autofocus import cli

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert cli.main(["--simulate", "--lens", "b", "focus", "--dry-run"]) == 0
    sys.stdout.write("⋯")  # would raise UnicodeEncodeError without errors="replace"
    sys.stdout.flush()


def test_focus_rejects_zero_passes(capsys):
    from omni_autofocus import cli

    assert cli.main(["--simulate", "--lens", "b", "focus", "--passes", "0"]) == 2


def _sim(sensor_mm=205.0, **kw):
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=sensor_mm, **kw)
    clock = FakeClock()
    return board, Controller(board, sleep=clock.sleep, clock=clock)


def _lens_b():
    return replace(config.Settings(), focus=replace(config.Settings().focus, lens="b"))


def test_stalled_move_is_a_motion_fault_but_busy_is_not(monkeypatch):
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller, ControllerBusyError, ControllerError

    board, ctl = _sim()

    def stall(self, params, pulses, **kw):
        raise ControllerError("axis 1 counter moved +100 pulses, expected ±2400")

    monkeypatch.setattr(Controller, "move_axis", stall)
    with pytest.raises(autofocus.MotionError, match="did not complete"):
        autofocus.run(ctl, _lens_b())

    def busy(self, params, pulses, **kw):
        raise ControllerBusyError("busy")

    monkeypatch.setattr(Controller, "move_axis", busy)
    with pytest.raises(ControllerBusyError):
        autofocus.run(ctl, _lens_b())


def test_rest_after_the_probe_never_exceeds_the_approved_move(monkeypatch):
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller

    board, ctl = _sim(sensor_mm=232.0, z_pulses_per_mm=800 / 0.88)  # Z travels 12 % short
    commanded = []
    real_move = Controller.move_axis

    def record(self, params, pulses, **kw):
        commanded.append(pulses)
        return real_move(self, params, pulses, **kw)

    monkeypatch.setattr(Controller, "move_axis", record)
    approved = []
    autofocus.run(ctl, _lens_b(), passes=1, confirm=lambda p: approved.append(p.pulses) or True)
    assert approved[0] == -8000  # down 10 mm
    assert sum(commanded) >= approved[0]  # never further down than approved (a later pass corrects)


def test_usb_failure_mid_move_is_a_motion_fault(monkeypatch):
    from omni_autofocus import autofocus
    from omni_autofocus.controller import Controller
    from omni_autofocus.cyusb import UsbTimeout

    board, ctl = _sim()

    def lost(self, params, pulses, **kw):
        raise UsbTimeout("endpoint 0x02 timed out")

    monkeypatch.setattr(Controller, "move_axis", lost)
    with pytest.raises(autofocus.MotionError, match="did not complete"):
        autofocus.run(ctl, _lens_b())


def test_cli_ladder_does_not_move_again_after_a_motion_fault(monkeypatch, capsys):
    from omni_autofocus import cli, ladder
    from omni_autofocus.autofocus import MotionError

    real_go_to = ladder.Ladder.go_to
    moves = {"returns": 0}

    def go_to(self, offset):
        if offset == 1.0:
            self.current = 1.0
            raise MotionError("Z did not complete a +1.0 mm move")
        return real_go_to(self, offset)

    def count_return(self):
        moves["returns"] += 1

    monkeypatch.setattr(ladder.Ladder, "go_to", go_to)
    monkeypatch.setattr(ladder.Ladder, "return_to_focus", count_return)
    monkeypatch.setattr(cli, "_prompt", lambda text: "")
    assert cli.main(["--simulate", "--lens", "b", "focus-ladder", "--yes", "--offsets", "0,1,2"]) == 2
    out = capsys.readouterr()
    assert moves["returns"] == 0 and "not moving it again" in out.out


def test_cli_ladder_exits_2_when_the_return_to_focus_fails(monkeypatch, capsys):
    from omni_autofocus import cli, ladder
    from omni_autofocus.autofocus import MotionError

    def fail(self):
        raise MotionError("Z did not complete a -1.0 mm move")

    monkeypatch.setattr(ladder.Ladder, "return_to_focus", fail)
    monkeypatch.setattr(
        cli,
        "_prompt",
        lambda text: (
            "y" if "Save" in text else ("+1" if "Highest" in text else ("0" if "Lowest" in text else ""))
        ),
    )
    assert cli.main(["--simulate", "--lens", "b", "focus-ladder", "--yes", "--offsets", "0,1"]) == 2
    out = capsys.readouterr().out
    assert "Could not return Z to focus" in out and "Saved" not in out


def test_settings_save_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    import os

    path = tmp_path / "s.toml"

    def fail(src, dst):
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        config.save(config.Settings(), path)
    assert list(tmp_path.iterdir()) == []


def _sim_with_clock(sensor_mm):
    from omni_autofocus.controller import Controller
    from omni_autofocus.simulator import FakeClock, SimulatedBoard

    board = SimulatedBoard(sensor_mm=sensor_mm, sensor_window=(120.0, 280.0))
    clock = FakeClock()
    return board, Controller(board, sleep=clock.sleep, clock=clock), clock


def _wait(ctl, clock, s=None, **kw):
    return autofocus.wait_for_surface(ctl, s or _lens_b(), sleep=clock.sleep, clock=clock, **kw)


def test_wait_for_surface_returns_at_once_when_the_work_is_in_view():
    board, ctl, clock = _sim_with_clock(205.0)
    seen = []
    assert _wait(ctl, clock, cancelled=lambda: False, on_watch=seen.append) == pytest.approx(205.0)
    assert seen == []


def test_wait_for_surface_follows_the_z_buttons_until_the_head_holds_still():
    board, ctl, clock = _sim_with_clock(330.0)  # head far too high: the sensor sees nothing
    seen = []

    def user(w):  # holds Z down, 3 mm per poll, and lets go at 250 mm
        seen.append(w)
        if board.sensor_mm > 250.0:
            board.jog(-3.0)

    h = _wait(ctl, clock, cancelled=lambda: False, on_watch=user)
    assert h == pytest.approx(249.0)
    out_of_view = [w for w in seen if w.height_mm is None]
    assert not out_of_view[0].moving and out_of_view[1].moving  # the counter shows the jog
    assert all(w.direction == 0 for w in out_of_view)  # out of view the direction is unknown
    in_view = [w for w in seen if w.height_mm is not None]
    assert in_view[1].direction == -1  # in view, the sensor shows which way
    assert in_view[0].steady == 0.0 and in_view[-1].steady >= 0.6  # the bar fills once it holds still
    assert not board.lists  # waiting never moves Z


def test_wait_for_surface_shows_an_upward_jog_as_up_although_the_counter_counts_down():
    board, ctl, clock = _sim_with_clock(200.0)
    seen, polls = [], []

    def holding_z_up(seconds):  # Autofocus pressed while still holding Z up: 2 mm per poll, 4 polls
        polls.append(seconds)
        if len(polls) <= 4:
            board.jog(+2.0)
        clock.sleep(seconds)

    h = autofocus.wait_for_surface(
        ctl, _lens_b(), cancelled=lambda: False, on_watch=seen.append, sleep=holding_z_up, clock=clock
    )
    assert h == pytest.approx(208.0)
    assert [w.direction for w in seen[:3]] == [1, 1, 1]  # not -1, though the counter fell
    assert not board.lists


def test_wait_for_surface_does_not_go_while_jog_counts_still_arrive():
    board, ctl, clock = _sim_with_clock(240.0)  # in view, the head creeping too little for the sensor
    seen, polls = [], []

    def creeping(seconds):
        polls.append(seconds)
        if len(polls) <= 4:
            board.counters[1] -= 40  # 0.05 mm: under the sensor's noise, yet the counter moves
        clock.sleep(seconds)

    autofocus.wait_for_surface(
        ctl, _lens_b(), cancelled=lambda: False, on_watch=seen.append, sleep=creeping, clock=clock
    )
    assert seen and all(w.moving for w in seen[:3])  # waited for the counter to settle
    assert seen[-1].steady >= 0.6 and not seen[-1].moving


def test_wait_for_surface_ignores_a_single_blip():
    board, ctl, clock = _sim_with_clock(330.0)
    n = []

    def user(w):
        n.append(w)
        if len(n) == 1:
            board.jog(-80.0)  # into view at 250
        elif len(n) == 3:
            board.sensor_window = (120.0, 200.0)  # one "no target" while the head holds still
        elif len(n) == 4:
            board.sensor_window = (120.0, 280.0)

    assert _wait(ctl, clock, cancelled=lambda: False, on_watch=user) == pytest.approx(250.0)
    assert all(w.height_mm is not None for w in n[1:])  # the blip never showed as "not in view"


def test_wait_for_surface_uses_the_configured_window_and_can_be_cancelled_or_time_out():
    board, ctl, clock = _sim_with_clock(275.0)  # in the sensor's view, outside a narrower setting
    s = _lens_b()
    s = replace(s, focus=replace(s.focus, sensor_max_mm=270.0))
    calls = []
    assert _wait(ctl, clock, s, cancelled=lambda: len(calls) >= 3, on_watch=calls.append) is None
    assert [w.height_mm for w in calls] == [None, None, None]
    with pytest.raises(autofocus.FocusError, match="nothing happened"):
        _wait(ctl, clock, s, cancelled=lambda: False, timeout_s=5.0)


def test_wait_for_surface_timeout_restarts_while_the_head_moves():
    board, ctl, clock = _sim_with_clock(400.0)
    polls = []

    def user(w):  # jogs down (still out of view) for longer than the 5 s timeout, then lets go
        polls.append(clock())
        if len(polls) <= 30:
            board.jog(-1.0)

    with pytest.raises(autofocus.FocusError, match="nothing happened"):
        _wait(ctl, clock, cancelled=lambda: False, on_watch=user, timeout_s=5.0)
    assert polls[30] - polls[0] > 5.0  # moving kept it waiting past the timeout
    assert 5.0 <= polls[-1] - polls[30] <= 6.0  # then it timed out 5 s after the last movement


def test_wait_for_surface_waits_until_focus_is_within_one_move():
    board, ctl, clock = _sim_with_clock(275.0)  # lens A focus at 181: 94 mm away, the limit is 60
    s = replace(config.Settings(), focus=replace(config.Settings().focus, lens="a"))
    seen = []

    def user(w):
        seen.append(w)
        if len(seen) == 3:
            board.jog(-40.0)  # keeps lowering: 235, within 60 mm of 181

    h = _wait(ctl, clock, s, cancelled=lambda: False, on_watch=user)
    assert h == pytest.approx(235.0)
    assert not seen[0].in_reach and seen[0].steady == 0.0 and seen[1].steady == 0.0  # no countdown yet
    assert seen[-1].in_reach
