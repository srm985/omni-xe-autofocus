from dataclasses import replace

import pytest

from omni_autofocus import autofocus, config


def test_defaults_match_commarker_install():
    s = config.Settings()
    assert s.focus.target_mm == 222.0
    assert s.z_axis.axis_params().pulses_per_mm == 800.0


def test_plan_positive_and_negative_moves():
    s = config.Settings()
    up = autofocus.plan(200.0, s)
    assert up.move_mm == pytest.approx(22.0) and up.pulses == 17600
    down = autofocus.plan(230.0, s)
    assert down.move_mm == pytest.approx(-8.0) and down.pulses == -6400


def test_plan_deadband():
    p = autofocus.plan(222.05, config.Settings())
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
    s = config.Settings()
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
    assert s.focus.lens == "a" and s.focus.target_mm == 180.5
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

    assert main(["--simulate", "focus", "--yes"]) == 0
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
    monkeypatch.setattr("builtins.input", lambda prompt="": calls.append("paused"))
    assert cli.main() == 0
    assert calls == [["focus"], "paused"]
