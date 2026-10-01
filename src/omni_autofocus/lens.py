"""Decide which lens is installed: explicit setting, else LightBurn's field size, else ComMarker."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import config, lightburn

FIELD_TOLERANCE = 0.10  # a profile's field may differ from the nominal lens field by up to 10 %


class LensError(ValueError):
    pass


def lens_for_field(width_mm: float, focus: config.FocusSettings) -> str | None:
    candidates = {"a": focus.field_a_mm, "b": focus.field_b_mm}
    lens, field = min(candidates.items(), key=lambda kv: abs(kv[1] - width_mm))
    return lens if abs(field - width_mm) <= FIELD_TOLERANCE * field else None


def resolve(
    settings: config.Settings,
    *,
    lightburn_prefs: Path | None = None,
    commarker_dir: Path | None = None,
) -> tuple[config.Settings, str]:
    """Return settings with ``focus.lens`` set to "a" or "b", and a sentence explaining the choice."""
    commarker_dir = commarker_dir or config.COMMARKER_DIR
    f = settings.focus
    chosen = f.lens.lower()
    if chosen in ("a", "b"):
        return settings, f"lens {chosen.upper()} (set explicitly)"
    if chosen != "auto":
        raise LensError(f"focus.lens must be 'auto', 'a' or 'b', not {f.lens!r}")

    reasons = []
    prefs = lightburn.load_prefs(lightburn_prefs)
    if prefs is None:
        reasons.append("LightBurn settings not found")
    else:
        dev, why = lightburn.omni_device(prefs)
        if dev is None:
            reasons.append(why)
        else:
            lens = lens_for_field(dev.width_mm, f)
            if lens:
                return (
                    replace(settings, focus=replace(f, lens=lens)),
                    f"lens {lens.upper()} ({dev.width_mm:g} mm field, from {why})",
                )
            reasons.append(f"{why} has a {dev.width_mm:g} mm field that matches neither lens")

    try:
        lens = config.commarker_lens(config.read_commarker_cfg(commarker_dir))
    except (OSError, ValueError, KeyError, IndexError):
        lens = None
        reasons.append("ComMarker Studio settings not found")
    if lens:
        return (
            replace(settings, focus=replace(f, lens=lens)),
            f"lens {lens.upper()} (from ComMarker Studio's lens setting; {'; '.join(reasons)})",
        )
    raise LensError(
        "cannot tell which lens is installed (" + "; ".join(reasons) + "). Use --lens a or --lens b."
    )
