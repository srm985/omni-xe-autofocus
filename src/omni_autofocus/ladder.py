"""Fine-tuning focus with test burns (the "focus ladder"), shared by the command line and the app.

After an autofocus, Z steps through offsets around the autofocus height (default -4..+4 mm); the user
burns the same small design at each one, then names the lowest and highest marks that still look
good. The middle of that range becomes the lens's new focus height.

Every mark is approached moving up, so lead-screw backlash does not skew the comparison, and every
step is checked with the sensor like an autofocus move.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import autofocus, config
from .controller import ControllerBusyError, ControllerError

BACKLASH_PRETRAVEL_MM = 1.0


def default_offsets(range_mm: float = 4.0, step_mm: float = 1.0) -> list[float]:
    if step_mm <= 0 or range_mm <= 0:
        raise ValueError("--range and --step must be positive")
    n = int(round(range_mm / step_mm))
    return [round(i * step_mm, 6) for i in range(-n, n + 1)]


def parse_offsets(text: str) -> list[float]:
    try:
        offsets = [float(x) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"bad --offsets {text!r}: use comma-separated mm values like 0,-1,1") from e
    if not offsets:
        raise ValueError("--offsets is empty")
    return offsets


def reach_mm(offsets: list[float]) -> float:
    """The furthest Z gets from the autofocus height, backlash pre-travel included."""
    return max(abs(o) for o in offsets) + BACKLASH_PRETRAVEL_MM


def best_focus(reference_mm: float, lowest: float, highest: float) -> tuple[float, float]:
    """(centre of the good range, new focus height as a sensor reading). ``focus.offset_mm`` stays a
    separate nudge on top of the saved height."""
    lo, hi = min(lowest, highest), max(lowest, highest)
    centre = (lo + hi) / 2
    return centre, round(reference_mm + centre, 1)


def save_focus_target(path: Path, lens_key: str, value: float) -> config.Settings:
    """Store ``value`` as lens ``lens_key``'s focus height in the settings file and return it.

    Starts from the file itself, so one-off overrides (``--lens``, the app's lens picker) are not
    persisted.
    """
    stored = config.load(path) if path.exists() else config.Settings()
    stored = config.validate(replace(stored, focus=replace(stored.focus, **{f"target_{lens_key}_mm": value})))
    config.save(stored, path)
    return stored


class Ladder:
    """Z positions relative to the autofocus height for one fine-tuning run.

    ``ctl`` must stay open (and the laser lock held) for the whole run. Moves raise
    :class:`ControllerBusyError` before anything moves when a job or the framing preview is running,
    so the same step can simply be retried.
    """

    def __init__(self, ctl, settings: config.Settings, offsets: list[float]):
        if reach_mm(offsets) > settings.focus.max_move_mm:
            raise ValueError(f"the offsets exceed the safety limit of {settings.focus.max_move_mm:g} mm")
        self.ctl, self.settings, self.offsets = ctl, settings, list(offsets)
        self.params = settings.z_axis.axis_params()
        self.current = 0.0  # mm from the autofocus height
        self.reference: float | None = None  # sensor reading at the autofocus height
        self.height: float | None = None  # last sensor reading

    def measure_reference(self) -> float:
        self.reference = self.height = self.ctl.read_height_median(max(self.settings.focus.samples, 3))
        return self.reference

    def go_to(self, offset: float) -> None:
        """Move to ``offset`` mm from the autofocus height, arriving from below."""
        if offset < self.current:
            self._move(offset - BACKLASH_PRETRAVEL_MM - self.current)
        if offset != self.current:
            self._move(offset - self.current)

    def return_to_focus(self) -> None:
        if self.current:
            self._move(-self.current)

    def _move(self, mm: float) -> None:
        pulses = self.params.mm_to_pulses(mm)
        if not pulses:
            return
        start = (
            self.height
            if self.height is not None
            else self.ctl.read_height_median(self.settings.focus.samples)
        )
        try:
            self.ctl.move_axis(self.params, pulses)
        except ControllerBusyError:
            raise  # nothing moved
        except (ControllerError, OSError) as e:
            raise autofocus.MotionError(f"Z did not complete a {mm:+.1f} mm move ({e}). Stopped") from e
        self.current = round(self.current + mm, 6)
        try:
            self.height = self.ctl.read_height_median(self.settings.focus.samples)
        except ControllerError as e:
            raise autofocus.MotionError(
                f"after moving Z {mm:+.1f} mm the sensor could not measure ({e})"
            ) from e
        autofocus.check_motion(mm, self.height - start, inverted=self.settings.z_axis.invert_direction)
