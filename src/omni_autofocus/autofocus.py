"""The autofocus calculation (same rules as ComMarker Studio's ``UiGWK_CalcMovePluses``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

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


# Sensor readings wobble about +-0.2 mm, so a final error up to this counts as focused.
FOCUS_TOLERANCE_MM = 0.5


class Outcome(Enum):
    IN_FOCUS = "in focus"
    NOT_CONVERGED = "not converged"  # moved, but still more than FOCUS_TOLERANCE_MM away
    DRY_RUN = "dry run"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FocusResult:
    outcome: Outcome
    height_mm: float  # last sensor reading
    target_mm: float
    plans: tuple[FocusPlan, ...]
    moved: bool  # Z moved at least once

    @property
    def error_mm(self) -> float:
        return self.target_mm - self.height_mm


def run(
    ctl,
    settings: Settings,
    *,
    passes: int = 2,
    dry_run: bool = False,
    confirm: Callable[[FocusPlan], bool] | None = None,
    on_plan: Callable[[int, FocusPlan], None] | None = None,
) -> FocusResult:
    """Measure, move, re-measure: up to ``passes`` moves, then a final check.

    ``confirm`` is asked before the first move only; ``on_plan`` sees every measurement. Raises
    :class:`FocusError` when a reading is out of range or a move exceeds the safety limit.
    """
    samples = settings.focus.samples
    params = settings.z_axis.axis_params()
    plans: list[FocusPlan] = []
    for iteration in range(1, passes + 1):
        p = plan(ctl.read_height_median(samples), settings)
        plans.append(p)
        if on_plan:
            on_plan(iteration, p)
        if not p.needed:
            return FocusResult(Outcome.IN_FOCUS, p.height_mm, p.target_mm, tuple(plans), iteration > 1)
        if dry_run:
            return FocusResult(Outcome.DRY_RUN, p.height_mm, p.target_mm, tuple(plans), False)
        if iteration == 1 and confirm and not confirm(p):
            return FocusResult(Outcome.CANCELLED, p.height_mm, p.target_mm, tuple(plans), False)
        ctl.move_axis(params, p.pulses)
    height = ctl.read_height_median(samples)
    target = settings.focus.target_mm
    outcome = Outcome.IN_FOCUS if abs(target - height) <= FOCUS_TOLERANCE_MM else Outcome.NOT_CONVERGED
    return FocusResult(outcome, height, target, tuple(plans), bool(plans))
