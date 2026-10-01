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
class AppSettings:
    """The Omni Autofocus app (the button bar). The command line ignores these."""

    hotkey: str = "ctrl+alt+f"  # global shortcut for Autofocus; "" turns it off
    sounds: bool = True  # a short system sound when autofocus finishes (handy with the hotkey)
    # One-click autofocus moves without asking, except a downward move (towards the work) larger
    # than this. Upward moves only take the head away from the work.
    confirm_down_above_mm: float = 10.0


@dataclass(frozen=True)
class Settings:
    focus: FocusSettings = field(default_factory=FocusSettings)
    z_axis: ZAxisSettings = field(default_factory=ZAxisSettings)
    app: AppSettings = field(default_factory=AppSettings)


SECTIONS = ("focus", "z_axis", "app")


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
    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")
    return Settings(
        focus=_apply(FocusSettings, data.get("focus", {}), "focus"),
        z_axis=_apply(ZAxisSettings, data.get("z_axis", {}), "z_axis"),
        app=_apply(AppSettings, data.get("app", {}), "app"),
    )


def set_value(settings: Settings, dotted_key: str, text: str) -> Settings:
    """Return ``settings`` with ``section.key`` set from ``text``, converted to the field's type."""
    section_name, _, key = dotted_key.partition(".")
    sections = {name: getattr(settings, name) for name in SECTIONS}
    if section_name not in sections or not key:
        raise ValueError(f"unknown setting {dotted_key!r}: use focus.<key>, z_axis.<key> or app.<key>")
    section = sections[section_name]
    if key not in {f.name for f in fields(section)}:
        known = ", ".join(f.name for f in fields(section))
        raise ValueError(f"unknown setting {dotted_key!r}; {section_name} has: {known}")
    current = getattr(section, key)
    if isinstance(current, bool):
        if text.lower() not in ("true", "false", "yes", "no", "1", "0"):
            raise ValueError(f"{dotted_key} needs true or false")
        value: object = text.lower() in ("true", "yes", "1")
    elif isinstance(current, int):
        value = int(text)
    elif isinstance(current, float):
        value = float(text)
    else:
        value = text
    if dotted_key == "focus.lens" and str(value).lower() not in ("auto", "a", "b"):
        raise ValueError("focus.lens must be auto, a or b")
    if dotted_key == "app.hotkey" and value:
        parse_hotkey(str(value))  # raises ValueError if malformed
    return replace(settings, **{section_name: replace(section, **{key: value})})


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
    for name in SECTIONS:
        section = getattr(settings, name)
        out.append(f"[{name}]")
        out.extend(f"{k} = {_toml_value(v)}" for k, v in asdict(section).items())
        out.append("")
    return "\n".join(out)


def save(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(settings), encoding="utf-8")


_MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
_NAMED_KEYS = {f"f{i}": 0x6F + i for i in range(1, 13)} | {"space": 0x20, "home": 0x24, "end": 0x23}


def parse_hotkey(text: str) -> tuple[int, int]:
    """``"ctrl+alt+f"`` -> (Windows MOD_* flags, virtual-key code). Needs at least one modifier."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"hotkey {text!r} needs a modifier and a key, like ctrl+alt+f")
    mods = 0
    for p in parts[:-1]:
        if p not in _MODIFIERS:
            raise ValueError(f"unknown modifier {p!r} in hotkey {text!r} (use ctrl, alt, shift, win)")
        mods |= _MODIFIERS[p]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"unknown key {key!r} in hotkey {text!r} (use a letter, digit or F1-F12)")
    return mods, vk


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
    return Settings(focus=focus, z_axis=z, app=base.app)


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
