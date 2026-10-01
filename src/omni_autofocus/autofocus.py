"""The autofocus calculation (same rules as ComMarker Studio's ``UiGWK_CalcMovePluses``)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


class FocusError(RuntimeError):
    pass


@dataclass(frozen=True)
class FocusPlan:
    height_mm: float  # current sensor reading
    target_mm: float  # sensor reading at best focus
    move_mm: float  # signed Z move (positive = increase the sensor distance)
    pulses: int

    @property
    def needed(self) -> bool:
        return self.pulses != 0


def plan(height_mm: float, settings: Settings) -> FocusPlan:
    f = settings.focus
    if not f.sensor_min_mm <= height_mm <= f.sensor_max_mm:
        raise FocusError(
            f"sensor reads {height_mm:.3f} mm, outside its valid range "
            f"{f.sensor_min_mm:g}-{f.sensor_max_mm:g} mm (is something under the sensor?)"
        )
    target = f.target_mm
    move = target - height_mm
    if abs(move) < f.deadband_mm:
        return FocusPlan(height_mm, target, 0.0, 0)
    if abs(move) > f.max_move_mm:
        raise FocusError(
            f"required move {move:+.2f} mm exceeds the safety limit of {f.max_move_mm:g} mm "
            "(raise focus.max_move_mm if this is expected)"
        )
    pulses = settings.z_axis.axis_params().mm_to_pulses(move)
    return FocusPlan(height_mm, target, move, pulses)
