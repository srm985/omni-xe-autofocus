"""The autofocus calculation (same rules as ComMarker Studio's ``UiGWK_CalcMovePluses``)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .config import Settings
from .controller import ControllerBusyError, ControllerError, SensorNoTargetError


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
    if not f.sensor_min_mm <= target <= f.sensor_max_mm:
        raise FocusError(
            f"the focus height for lens {f.lens.upper()} ({target:.1f} mm, offset included) is outside the "
            f"sensor range {f.sensor_min_mm:g}-{f.sensor_max_mm:g} mm; set it in ⋯ → Settings or with "
            "fine-tuning (command line: set-focus), or check the focus nudge"
        )
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
    verified: bool = False  # a move of 1 mm or more was seen to go the commanded way

    @property
    def error_mm(self) -> float:
        return self.target_mm - self.height_mm


# A question that took longer than this was answered by a person: the head may have been moved by
# hand meanwhile, so measure again before moving.
HUMAN_PAUSE_S = 1.0
RECHECK_MM = 0.5
# Until Z has been seen to follow a move, a longer move starts with this much, checked first. A
# machine whose Z runs backwards then goes at most this far the wrong way.
PROBE_MM = 3.0


class MotionError(FocusError):
    """Z did not follow a move as commanded (reversed direction, stall, wrong pitch)."""


def check_motion(move_mm: float, change_mm: float, *, inverted: bool = False, strict: bool = False) -> None:
    """Raise :class:`MotionError` unless the sensor reading changed roughly as much as Z was told to
    move, the same way. Sensor noise is about +-0.2 mm, so small moves can never trip it. ``strict``
    (for the probe step) allows 15 % scale error plus 0.3 mm of noise (about 25 % on the 3 mm probe)
    instead of 50 % plus 0.5 mm, so a badly wrong pitch is caught before the long part of a move."""
    slack = 0.15 if strict else 0.5
    noise = 0.3 if strict else 0.5
    wrong_way = move_mm * change_mm < 0 and abs(change_mm) > 0.5
    too_little = abs(change_mm) < (1 - slack) * abs(move_mm) - noise
    too_much = abs(change_mm) > (1 + slack) * abs(move_mm) + noise
    if not (wrong_way or too_little or too_much):
        return
    msg = (
        f"Z did not move as expected: asked for {move_mm:+.1f} mm, but the sensor reading changed "
        f"{change_mm:+.1f} mm. Stopped"
    )
    if wrong_way:
        raise MotionError(
            msg + '. If Z moved the opposite way, switch "Z moves the wrong way" '
            f"{'off' if inverted else 'on'} in the app (⋯ → Settings), or run 'omni-autofocus config set "
            f"z_axis.invert_direction {str(not inverted).lower()}'"
        )
    raise MotionError(msg + " (stall, end of travel, or wrong z_axis pitch settings)")


# Waiting for the work to come into view while a person moves Z with the machine's buttons. The
# sensor gives the same "no target" answer when the head is too high and when it is too close, so
# the direction is theirs to choose: this only watches.
SURFACE_POLL_S = 0.3
SURFACE_STEADY_S = 1.0  # the head must hold still this long, so Z is not still moving under the buttons
SURFACE_STEADY_MM = 0.5
SURFACE_DIRECTION_MM = 0.2  # a reading change per poll beyond the sensor's noise: the head is moving
SURFACE_TIMEOUT_S = 180.0  # give up after this long with nothing happening


@dataclass(frozen=True)
class SurfaceWatch:
    """What the person moving the head should see, once per poll."""

    height_mm: float | None  # sensor reading, or None while the work is out of view
    moving: bool  # the Z counter or the reading changed since the last poll
    direction: int  # +1 up, -1 down (from the sensor, so only while in view), 0 unknown or still
    steady: float  # 0..1: how far through holding still before autofocus continues
    in_reach: bool = True  # in view, but further from focus than one move may go (focus.max_move_mm)


def wait_for_surface(
    ctl,
    settings: Settings,
    *,
    cancelled: Callable[[], bool],
    on_watch: Callable[[SurfaceWatch], None] | None = None,
    timeout_s: float = SURFACE_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> float | None:
    """Wait until the sensor sees a surface within its range and the head holds still.

    Returns the reading straight away if the surface is in view and the Z axis counter does not
    change over one poll. Otherwise reports every poll to ``on_watch`` and returns once the reading
    has held within :data:`SURFACE_STEADY_MM`, with the counter unchanged, for
    :data:`SURFACE_STEADY_S`. The machine's Z buttons move the axis counter too, but on the Omni it
    counts down whichever way the head goes, so the counter only says *whether* the head moves; the
    direction comes from the sensor while the work is in view. Moving Z while jog counts are still
    arriving would also spoil the move's own counter check, hence the exact "unchanged". A single
    "no target" between good readings is ignored. A reading further from the focus height than
    ``focus.max_move_mm`` waits too, so the person keeps moving the head. Returns ``None`` when
    ``cancelled()`` becomes true; raises :class:`FocusError` after ``timeout_s`` without any
    movement. Never moves Z.
    """
    f = settings.focus
    axis_id = settings.z_axis.axis_id

    def read() -> float | None:
        try:
            h = ctl.read_height()
        except SensorNoTargetError:
            return None
        return h if f.sensor_min_mm <= h <= f.sensor_max_mm else None

    def counter() -> int | None:
        return ctl.ext_state().axis_position(axis_id) if axis_id <= 1 else None

    def reachable(h: float) -> bool:
        try:
            return abs(f.target_mm - h) <= f.max_move_mm
        except ValueError:  # lens not resolved: autofocus.plan reports that
            return True

    pos, h = counter(), read()
    direction = 0
    if h is not None:  # in view: go at once, unless the head is still moving under the Z buttons
        sleep(SURFACE_POLL_S)
        again, reading = counter(), read()
        steady_now = again == pos and reading is not None and abs(reading - h) <= SURFACE_STEADY_MM
        if steady_now and reachable(reading):
            return reading
        if reading is not None and abs(reading - h) >= SURFACE_DIRECTION_MM:
            direction = 1 if reading > h else -1
        pos, h = again, reading
    deadline = clock() + timeout_s
    anchor, since, misses, moving = None, 0.0, 0, h is not None
    while True:
        steady = min(1.0, (clock() - since) / SURFACE_STEADY_S) if anchor is not None else 0.0
        if on_watch:
            on_watch(SurfaceWatch(h, moving, direction, steady, h is None or reachable(h)))
        if cancelled():
            return None
        if anchor is not None and steady >= 1.0:
            return h
        if clock() > deadline:
            raise FocusError(
                f"nothing happened for {timeout_s / 60:g} minutes, so autofocus stopped waiting; move the "
                "head nearer working height, then press Autofocus"
            )
        sleep(SURFACE_POLL_S)
        new_pos, reading = counter(), read()
        moving = new_pos != pos
        pos = new_pos
        if reading is None:
            misses += 1
            if misses >= 2 or h is None:  # a single blip between good readings is ignored
                h, anchor, direction = None, None, 0
            elif moving:
                since = clock()
        else:
            change = 0.0 if h is None else reading - h
            direction = 0 if abs(change) < SURFACE_DIRECTION_MM else (1 if change > 0 else -1)
            moving = moving or direction != 0
            if not reachable(reading):
                anchor = None  # keep going: no countdown yet
            elif moving or anchor is None or abs(reading - anchor) > SURFACE_STEADY_MM:
                anchor, since = reading, clock()
            if h is None:
                moving = True  # just came into view
            misses, h = 0, reading
        if moving:
            deadline = clock() + timeout_s  # someone is working the Z buttons


def run(
    ctl,
    settings: Settings,
    *,
    passes: int = 2,
    dry_run: bool = False,
    confirm: Callable[[FocusPlan], bool] | None = None,
    on_plan: Callable[[int, FocusPlan], None] | None = None,
    probe: bool = True,
) -> FocusResult:
    """Measure, move, re-measure: up to ``passes`` moves, then a final check.

    ``confirm`` is asked before every move and decides itself whether to ask a person; ``on_plan``
    sees every measurement. With ``probe`` (until Z is known to follow), a move longer than
    :data:`PROBE_MM` starts with a checked :data:`PROBE_MM` step. After every move the sensor must
    have changed as commanded (:func:`check_motion`). Raises :class:`FocusError` when a reading is
    out of range, a move exceeds the safety limit, Z did not follow (:class:`MotionError`), or the
    height changed while a person was asked.
    """
    samples = settings.focus.samples
    params = settings.z_axis.axis_params()
    inverted = settings.z_axis.invert_direction
    plans: list[FocusPlan] = []
    verified = False

    def move(mm: float, pulses: int, start_height: float, *, strict: bool = False) -> float:
        nonlocal verified
        try:
            ctl.move_axis(params, pulses)
        except ControllerBusyError:
            raise  # refused before moving: a job or the framing preview is running
        except (ControllerError, OSError) as e:  # stall, end of travel, counter mismatch, USB lost mid-move
            raise MotionError(
                f"Z did not complete a {mm:+.1f} mm move ({e}). Stopped; check the Z axis"
            ) from e
        try:
            new_height = ctl.read_height_median(samples)
        except ControllerError as e:  # e.g. the head left the sensor's range: Z went somewhere unexpected
            raise MotionError(
                f"after moving Z {mm:+.1f} mm the sensor could not measure ({e}). Stopped; check where the "
                "head is and whether z_axis.invert_direction is right"
            ) from e
        check_motion(mm, new_height - start_height, inverted=inverted, strict=strict)
        verified = verified or abs(mm) >= 1.0
        return new_height

    height = ctl.read_height_median(samples)
    for iteration in range(1, passes + 1):
        p = plan(height, settings)
        plans.append(p)
        if on_plan:
            on_plan(iteration, p)
        if not p.needed:
            return FocusResult(
                Outcome.IN_FOCUS, p.height_mm, p.target_mm, tuple(plans), iteration > 1, verified
            )
        if dry_run:
            return FocusResult(Outcome.DRY_RUN, p.height_mm, p.target_mm, tuple(plans), False, verified)
        asked_at = time.monotonic()
        if confirm and not confirm(p):
            return FocusResult(
                Outcome.CANCELLED, p.height_mm, p.target_mm, tuple(plans), iteration > 1, verified
            )
        if time.monotonic() - asked_at > HUMAN_PAUSE_S:
            again = ctl.read_height_median(samples)
            if abs(again - height) > RECHECK_MM:
                raise FocusError(
                    f"the height changed while waiting for an answer ({height:.1f} -> {again:.1f} mm); "
                    "nothing moved, start autofocus again"
                )
        if probe and not verified and abs(p.move_mm) > PROBE_MM + 1.0:
            step = PROBE_MM if p.move_mm > 0 else -PROBE_MM
            height = move(step, params.mm_to_pulses(step), height, strict=True)
            rest = plan(height, settings)  # re-plan from the measured height, not the commanded one
            allowed = p.move_mm - step  # never command more, in total, than the move that was approved
            if rest.move_mm * allowed > 0 and abs(rest.move_mm) > abs(allowed):
                rest = FocusPlan(
                    rest.height_mm, rest.target_mm, allowed, p.pulses - params.mm_to_pulses(step)
                )
            if rest.needed:
                height = move(rest.move_mm, rest.pulses, height)
        else:
            height = move(p.move_mm, p.pulses, height)
    target = settings.focus.target_mm
    outcome = Outcome.IN_FOCUS if abs(target - height) <= FOCUS_TOLERANCE_MM else Outcome.NOT_CONVERGED
    return FocusResult(outcome, height, target, tuple(plans), bool(plans), verified)
