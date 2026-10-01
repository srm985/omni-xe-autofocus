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
    # The real layout seen on the first user's PC: last-used profile is a JCZ one, one BSL profile.
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

    real_move = Controller.move_axis
    monkeypatch.setattr(
        Controller, "move_axis", lambda self, params, pulses, **kw: real_move(self, params, 1)
    )
    assert cli.main(["--simulate", "--lens", "b", "focus", "--yes"]) == 3
    assert "Warning: still" in capsys.readouterr().out


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
