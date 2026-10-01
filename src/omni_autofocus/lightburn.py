"""Read the active galvo field size from LightBurn's settings (used to pick the lens automatically).

LightBurn keeps device profiles in ``%LOCALAPPDATA%\\LightBurn\\prefs.ini`` (JSON). For galvo lasers
the recommended setup is one device profile per lens, so the profile's field size identifies the lens.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def default_prefs_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "LightBurn" / "prefs.ini"


@dataclass(frozen=True)
class LightBurnDevice:
    index: int
    name: str
    profile: str
    width_mm: float
    height_mm: float

    @property
    def is_bsl(self) -> bool:
        return self.profile.lower().startswith("bsl") or self.name.lower().startswith("bsl")


def devices(prefs: dict) -> list[LightBurnDevice]:
    out = []
    for i, d in enumerate(prefs.get("DeviceList", [])):
        try:
            out.append(
                LightBurnDevice(
                    index=i,
                    name=str(d.get("Name", "")),
                    profile=str(d.get("ProfilePath", "")),
                    width_mm=float(d["Width"]),
                    height_mm=float(d["Height"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def omni_device(prefs: dict) -> tuple[LightBurnDevice | None, str]:
    """The LightBurn profile used for the BSL laser, plus a short explanation of the choice."""
    devs = devices(prefs)
    bsl = [d for d in devs if d.is_bsl]
    current = prefs.get("DefaultDevice")
    active = next((d for d in devs if d.index == current), None)
    if active is not None and active.is_bsl:
        return active, f"last-used LightBurn profile '{active.name}'"
    if len(bsl) == 1:
        return bsl[0], f"the only BSL profile in LightBurn, '{bsl[0].name}'"
    if not bsl:
        return None, "no BSL device profile in LightBurn"
    return None, "several BSL profiles in LightBurn and the last-used one is not BSL"


def load_prefs(path: Path | None = None) -> dict | None:
    path = path or default_prefs_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
