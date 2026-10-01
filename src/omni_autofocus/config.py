"""Settings: defaults, a TOML settings file, and import from a ComMarker Studio install."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from .commands import AxisParams

COMMARKER_DIR = Path(r"C:\Program Files (x86)\ComMarker_Studio")
COMMARKER_XOR_KEY = b"this is elfive\x00\x00"
COMMARKER_HEADER_LEN = 8


def default_config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "omni-autofocus" / "config.toml"


@dataclass(frozen=True)
class FocusSettings:
    lens: str = "auto"  # "auto" (from LightBurn, then ComMarker), "a" (small field) or "b" (large field)
    target_a_mm: float = 181.0  # sensor reading at best focus, lens A (fBestFocalDistance)
    target_b_mm: float = 222.0  # sensor reading at best focus, lens B (fBestFocalDistance_B)
    field_a_mm: float = 70.0  # marking field of lens A (galvoParam.workSize), used by "auto"
    field_b_mm: float = 150.0  # marking field of lens B (galvo2Param.workSize), used by "auto"
    offset_mm: float = 0.0  # added to the target sensor distance (e.g. to defocus)
    deadband_mm: float = 0.1  # ComMarker skips moves smaller than this
    sensor_min_mm: float = 120.0
    sensor_max_mm: float = 280.0
    max_move_mm: float = 60.0  # safety limit for a single autofocus move
    samples: int = 3  # sensor readings per measurement (median); one reading wobbles about +-0.2 mm

    @property
    def target_mm(self) -> float:
        lens = self.lens.lower()
        if lens not in ("a", "b"):
            raise ValueError(f"lens {self.lens!r} has not been resolved to 'a' or 'b'")
        return (self.target_b_mm if lens == "b" else self.target_a_mm) + self.offset_mm


@dataclass(frozen=True)
class ZAxisSettings:
    axis_id: int = 1
    reverse: bool = True
    pitch_pulse: int = 3200
    screw_pitch: float = 4.0
    max_run_speed: float = 320.0
    start_speed: float = 0.0
    run_speed: float = 8.0
    acc_speed: float = 5.0
    gear_ratio: float = 1.0
    invert_direction: bool = False  # extra knob if positive moves turn out to go the wrong way

    def axis_params(self) -> AxisParams:
        return AxisParams(
            axis_id=self.axis_id,
            reverse=self.reverse ^ self.invert_direction,
            pitch_pulse=self.pitch_pulse,
            screw_pitch=self.screw_pitch,
            max_run_speed=self.max_run_speed,
            start_speed=self.start_speed,
            run_speed=self.run_speed,
            acc_speed=self.acc_speed,
            gear_ratio=self.gear_ratio,
        )


@dataclass(frozen=True)
class Settings:
    focus: FocusSettings = field(default_factory=FocusSettings)
    z_axis: ZAxisSettings = field(default_factory=ZAxisSettings)


def _apply(section_cls, data: dict, where: str):
    known = {f.name for f in fields(section_cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown setting(s) in [{where}]: {', '.join(sorted(unknown))}")
    return section_cls(**data)


def settings_path(path: Path | None = None) -> Path:
    """The settings file in use: ``path``, else $OMNI_AUTOFOCUS_CONFIG, else the default location."""
    if path is not None:
        return path
    env = os.environ.get("OMNI_AUTOFOCUS_CONFIG")
    return Path(env) if env else default_config_path()


def load(path: Path | None = None) -> Settings:
    """Load settings from ``path``, $OMNI_AUTOFOCUS_CONFIG or the default location; fall back to defaults."""
    path = settings_path(path)
    if not path.exists():
        return Settings()  # a not-yet-created settings file means defaults
    with open(path, "rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - {"focus", "z_axis"}
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")
    return Settings(
        focus=_apply(FocusSettings, data.get("focus", {}), "focus"),
        z_axis=_apply(ZAxisSettings, data.get("z_axis", {}), "z_axis"),
    )


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    return repr(v)


def dumps(settings: Settings) -> str:
    out = [
        "# omni-autofocus settings",
        "# Sensor distances are what the height sensor reads, not lens-to-surface distances.",
        "",
    ]
    for name, section in (("focus", settings.focus), ("z_axis", settings.z_axis)):
        out.append(f"[{name}]")
        out.extend(f"{k} = {_toml_value(v)}" for k, v in asdict(section).items())
        out.append("")
    return "\n".join(out)


def save(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(settings), encoding="utf-8")


# --- ComMarker Studio import -------------------------------------------------------------------------


def decode_commarker_cfg(raw: bytes) -> dict:
    """Decode one of ComMarker Studio's config/*.cfg files (8-byte header + repeating-key XOR JSON)."""
    body = raw[COMMARKER_HEADER_LEN:]
    k = COMMARKER_XOR_KEY
    plain = bytes(b ^ k[i % len(k)] for i, b in enumerate(body))
    return json.loads(plain.decode("utf-8"))


def encode_commarker_cfg(obj: dict, header: bytes = b"\x01" + bytes(7)) -> bytes:
    """Inverse of :func:`decode_commarker_cfg` (used by tests)."""
    plain = json.dumps(obj).encode("utf-8")
    k = COMMARKER_XOR_KEY
    return header + bytes(b ^ k[i % len(k)] for i, b in enumerate(plain))


def from_commarker(param_cfg: dict, base: Settings | None = None) -> Settings:
    """Build settings from a decoded ComMarker ``lcsparam.cfg``."""
    base = base or Settings()
    params = param_cfg["lmcPars"]["params"]
    lmc = next((p for p in params if p.get("parName") == "default"), params[0])

    def field(key: str, default: float) -> float:
        try:
            return float(lmc[key]["workSize"]["x"])
        except (KeyError, TypeError, ValueError):
            return default

    focus = replace(
        base.focus,
        field_a_mm=field("galvoParam", base.focus.field_a_mm),
        field_b_mm=field("galvo2Param", base.focus.field_b_mm),
        target_a_mm=float(lmc.get("fBestFocalDistance", base.focus.target_a_mm)),
        target_b_mm=float(lmc.get("fBestFocalDistance_B", base.focus.target_b_mm)),
        sensor_min_mm=float(lmc.get("fMinDistanceOfSensor", base.focus.sensor_min_mm)),
        sensor_max_mm=float(lmc.get("fMaxDistanceOfSensor", base.focus.sensor_max_mm)),
    )
    z = base.z_axis
    zpar = param_cfg.get("extMarkerPar", {}).get("axisZParExt")
    if zpar:
        s, r = zpar["setting"], zpar["runData"]
        z = replace(
            z,
            axis_id=int(s["axisId"]),
            reverse=bool(s["bRevRot"]),
            pitch_pulse=int(s["pitchPulse"]),
            screw_pitch=float(s["screwPitch"]),
            max_run_speed=float(s["maxRunSpeed"]),
            gear_ratio=float(s["gearRatio"]),
            start_speed=float(r["startSpeed"]),
            run_speed=float(r["runSpeed"]),
            acc_speed=float(r["accSpeed"]),
        )
    return Settings(focus=focus, z_axis=z)


def read_commarker_cfg(install_dir: Path = COMMARKER_DIR) -> dict:
    return decode_commarker_cfg((install_dir / "config" / "lcsparam.cfg").read_bytes())


def load_commarker(install_dir: Path = COMMARKER_DIR) -> Settings:
    return from_commarker(read_commarker_cfg(install_dir))


def commarker_lens(param_cfg: dict) -> str | None:
    """The lens selected in ComMarker Studio ("Galvo B" checkbox), if recorded."""
    params = param_cfg["lmcPars"]["params"]
    lmc = next((p for p in params if p.get("parName") == "default"), params[0])
    if "IsGALVO_B" not in lmc:
        return None
    return "b" if lmc["IsGALVO_B"] else "a"
