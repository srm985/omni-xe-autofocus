"""Command-line interface: ``omni-autofocus <command>``."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import logging
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__, autofocus, config, flash, lens
from .controller import Controller, ControllerBusyError, ControllerError

log = logging.getLogger("omni_autofocus")

CONFLICTING_PROGRAMS = {"commarker_studio.exe": "ComMarker Studio"}
WARN_PROGRAMS = {"lightburn.exe": "LightBurn"}


def _running_programs() -> set[str]:
    if sys.platform != "win32":
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {row[0].lower() for row in csv.reader(io.StringIO(out)) if row}


def _check_other_software(force: bool) -> None:
    if _SIMULATE:
        return
    running = _running_programs()
    for exe, name in CONFLICTING_PROGRAMS.items():
        if exe in running and not force:
            raise SystemExit(
                f"{name} is running and talks to the laser constantly. Close it first (or use --force)."
            )
    for exe, name in WARN_PROGRAMS.items():
        if exe in running:
            print(f"Note: {name} is running. Make sure it is not connected to the laser while this runs.")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


_SIMULATE = False


def _open_controller():
    if _SIMULATE:
        from .simulator import FakeClock, SimulatedBoard, factory_flash_image

        board = SimulatedBoard(sensor_mm=205.0, flash_image=factory_flash_image())
        clock = FakeClock()
        return contextlib.nullcontext(board), Controller(board, sleep=clock.sleep, clock=clock)
    from .cyusb import CyUsbDevice  # imported lazily: Windows-only

    dev = CyUsbDevice.open_first()
    log.info("opened %s", dev.path)
    return dev, Controller(dev)


def _read_laser_calibration(ctl: Controller) -> dict:
    """The ComMarker parameter file the factory stored in the controller's flash."""
    return config.decode_commarker_cfg(flash.read_named(ctl, "lcsparam.cfg"))


def _factory_calibration() -> tuple[config.Settings, str]:
    """Calibration from the laser itself, else from an installed ComMarker Studio."""
    try:
        dev, ctl = _open_controller()
        with dev:
            return config.from_commarker(_read_laser_calibration(ctl)), "the laser"
    except (ControllerError, OSError, ValueError, KeyError, IndexError) as e:
        log.info("could not read the calibration from the laser: %s", e)
    try:
        return config.load_commarker(config.COMMARKER_DIR), "ComMarker Studio's settings"
    except (OSError, ValueError, KeyError, IndexError) as e:
        log.info("no ComMarker Studio settings: %s", e)
    raise ControllerError(
        "no focus calibration found: the laser's stored calibration could not be read and ComMarker "
        "Studio is not installed. Run 'omni-autofocus config init', then enter the focus heights from "
        "the card that came with the machine as focus.target_a_mm / focus.target_b_mm."
    )


def _settings(args, *, calibrate: bool = False) -> config.Settings:
    """Settings from the file. With ``calibrate``, a missing file is first created from the factory
    calibration (laser, then ComMarker Studio) so built-in example values are never used silently."""
    path = config.settings_path(Path(args.config) if args.config else None)
    if calibrate and not path.exists():
        s, source = _factory_calibration()
        config.save(s, path)
        print(f"First run: saved the factory calibration from {source} to {path}")
    s = config.load(path)
    if args.lens:
        s = replace(s, focus=replace(s.focus, lens=args.lens))
    return s


def _settings_with_lens(args) -> config.Settings:
    """Settings with the lens resolved (explicit, LightBurn, then ComMarker); prints the choice."""
    s = _settings(args, calibrate=True)
    try:
        s, why = lens.resolve(s)
    except lens.LensError:
        if not _SIMULATE:
            raise
        s, why = replace(s, focus=replace(s.focus, lens="b")), "lens B (simulation default)"
    print(f"Using {why}: focus at sensor reading {s.focus.target_mm:.1f} mm")
    return s


# --- commands ----------------------------------------------------------------------------------------


def cmd_devices(args) -> int:
    from .cyusb import list_devices

    devices = list_devices()
    if not devices:
        print("No devices bound to the CYUSB driver were found.")
        return 1
    for d in devices:
        print(("[supported] " if d.supported else "[other]     ") + d.path)
    return 0


def cmd_status(args) -> int:
    _check_other_software(args.force)
    dev, ctl = _open_controller()
    with dev:
        st = ctl.state()
        ext = ctl.ext_state()
    print(f"board state     : {st.board_state} ({'idle' if st.idle else 'busy'})")
    print(f"free cache      : {st.free_cache_kb} KiB")
    print(f"axis status     : {' '.join(f'{i}:{v:#x}' for i, v in enumerate(ext.axis_status))}")
    print(f"axis positions  : {' '.join(f'{i}:{ext.axis_position(i):+d}' for i in range(2))} pulses")
    if args.raw:
        print(f"AA05 reply      : {st.raw.hex(' ')}")
        print(f"AA07 reply      : {ext.raw.hex(' ')}")
    return 0


def cmd_height(args) -> int:
    _check_other_software(args.force)
    s = _settings_with_lens(args)
    dev, ctl = _open_controller()
    with dev:
        for i in range(args.count):
            h = ctl.read_height()
            print(f"{h:.3f} mm   (focus target {s.focus.target_mm:.3f} mm)")
            if i + 1 < args.count:
                time.sleep(args.interval)
    return 0


def cmd_move_z(args) -> int:
    _check_other_software(args.force)
    s = _settings(args, calibrate=True)
    params = s.z_axis.axis_params()
    pulses = params.mm_to_pulses(args.mm)
    if abs(args.mm) > s.focus.max_move_mm:
        print(f"Refusing to move {args.mm:+g} mm (limit {s.focus.max_move_mm:g} mm, see focus.max_move_mm).")
        return 2
    print(f"Z move: {args.mm:+.3f} mm = {pulses:+d} pulses on axis {params.axis_id}")
    if not _confirm("Move the Z axis now?", args.yes):
        print("Cancelled.")
        return 1
    dev, ctl = _open_controller()
    with dev:
        before = _try_height(ctl)
        moved = ctl.move_axis(params, pulses)
        after = _try_height(ctl)
    if moved is not None:
        print(f"axis counter: {moved:+d} pulses ({moved / params.pulses_per_mm:+.3f} mm at configured pitch)")
    if before is not None and after is not None:
        print(f"sensor: {before:.3f} mm -> {after:.3f} mm (change {after - before:+.3f} mm)")
    return 0


def _try_height(ctl: Controller) -> float | None:
    try:
        return ctl.read_height()
    except ControllerError as e:
        log.warning("%s", e)
        return None


def cmd_focus(args) -> int:
    _check_other_software(args.force)
    s = _settings_with_lens(args)
    dev, ctl = _open_controller()
    with dev:
        return _autofocus(ctl, s, passes=args.passes, dry_run=args.dry_run, yes=args.yes)


def _autofocus(ctl: Controller, s: config.Settings, *, passes: int, dry_run: bool, yes: bool) -> int:
    for iteration in range(1, passes + 1):
        height = ctl.read_height_median(s.focus.samples)
        try:
            p = autofocus.plan(height, s)
        except autofocus.FocusError as e:
            print(f"Error: {e}")
            return 2
        print(
            f"pass {iteration}: sensor {p.height_mm:.3f} mm, target {p.target_mm:.3f} mm, "
            f"move {p.move_mm:+.3f} mm ({p.pulses:+d} pulses)"
        )
        if not p.needed:
            print(f"In focus (error {p.target_mm - p.height_mm:+.3f} mm).")
            return 0
        if dry_run:
            print("Dry run: not moving.")
            return 0
        if iteration == 1 and not _confirm("Move the Z axis now?", yes):
            print("Cancelled.")
            return 1
        ctl.move_axis(s.z_axis.axis_params(), p.pulses)
    final = autofocus.plan(ctl.read_height_median(s.focus.samples), s)
    print(f"final: sensor {final.height_mm:.3f} mm, error {final.target_mm - final.height_mm:+.3f} mm")
    return 0


def _parse_offsets(text: str) -> list[float]:
    try:
        offsets = [float(x) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"bad --offsets {text!r}: use comma-separated mm values like 0,-1,1") from e
    if not offsets:
        raise ValueError("--offsets is empty")
    return offsets


BACKLASH_PRETRAVEL_MM = 1.0


def _ladder_offsets(args) -> list[float]:
    if args.offsets:
        return _parse_offsets(args.offsets)
    if args.step <= 0 or args.range <= 0:
        raise ValueError("--range and --step must be positive")
    n = int(round(args.range / args.step))
    return [round(i * args.step, 6) for i in range(-n, n + 1)]


def cmd_focus_ladder(args) -> int:
    """Autofocus, step Z through offsets for test burns, then optionally save the dialled-in focus.

    Each mark is approached from below (one-directional travel) so lead-screw backlash does not skew
    the comparison. The user reports the lowest and highest marks that still look good; the midpoint
    of that range becomes the new focus height for the current lens.
    """
    _check_other_software(args.force)
    s = _settings_with_lens(args)
    offsets = _ladder_offsets(args)
    params = s.z_axis.axis_params()
    ascending = offsets == sorted(offsets)
    reach = max(abs(o) for o in offsets) + (BACKLASH_PRETRAVEL_MM if ascending else 0)
    if reach > s.focus.max_move_mm:
        print(f"Offsets exceed the safety limit of {s.focus.max_move_mm:g} mm.")
        return 2
    print("Focus ladder: autofocus, then Z offsets " + ", ".join(f"{o:+g}" for o in offsets) + " mm.")
    print("At each step burn the same small test design at a new spot in LightBurn, then come back here.")
    print("Tip: use the lowest power that still marks; out-of-focus marks then fade, which shows the edges.")
    if not _confirm("Start (this moves the Z axis)?", args.yes):
        print("Cancelled.")
        return 1
    dev, ctl = _open_controller()
    with dev:
        code = _autofocus(ctl, s, passes=2, dry_run=False, yes=True)
        if code:
            return code
        reference = ctl.read_height_median(s.focus.samples)
        print(f"Reference (offset 0): sensor reading {reference:.3f} mm")
        current = 0.0
        burned: list[float] = []
        try:
            if ascending and offsets[0] < 0:  # take up backlash: arrive at every mark moving upwards
                _move_when_free(ctl, params, offsets[0] - BACKLASH_PRETRAVEL_MM)
                current = offsets[0] - BACKLASH_PRETRAVEL_MM
            for offset in offsets:
                step = offset - current
                if step:
                    _move_when_free(ctl, params, step)
                    current = offset
                answer = _prompt(
                    f"\nZ is at {offset:+g} mm. Burn the mark labelled '{offset:+g}', "
                    "then press Enter (q + Enter to stop): "
                )
                if answer.strip().lower() == "q":
                    break
                burned.append(offset)
        finally:
            if current:
                print(f"Returning Z to the autofocus height ({-current:+g} mm).")
                try:
                    _move_when_free(ctl, params, -current)
                except _LeftInPlace:
                    print(f"Z left at {current:+g} mm from focus; run 'omni-autofocus focus' to return.")
    if len(burned) < 2:
        return 0
    return _dial_in(args, s, burned, reference)


class _LeftInPlace(Exception):
    """The user chose not to retry a move the controller refused because it was busy."""


def _move_when_free(ctl: Controller, params, mm: float) -> None:
    """Move Z; if the controller is busy (job or framing preview running), ask the user and retry."""
    while True:
        try:
            ctl.move_axis(params, params.mm_to_pulses(mm))
            return
        except ControllerBusyError:
            answer = _prompt(
                "\nThe controller is busy: stop any LightBurn job and close the framing/red-light "
                "preview, then press Enter to retry (q + Enter to skip this move): "
            )
            if answer.strip().lower() == "q":
                raise _LeftInPlace from None


def _ask_label(question: str, burned: list[float]) -> float | None:
    while True:
        answer = _prompt(question).strip()
        if not answer or answer.lower() == "q":
            return None
        try:
            value = float(answer)
        except ValueError:
            value = None
        if value is not None and any(abs(value - b) < 1e-6 for b in burned):
            return value
        print("Please type one of the labels: " + ", ".join(f"{b:+g}" for b in burned))


def _dial_in(args, s: config.Settings, burned: list[float], reference: float) -> int:
    print("\nCompare the marks with a loupe or a zoomed phone photo.")
    print("Find the range of marks that look equally good (thin lines, even fill, full contrast).")
    lo = _ask_label("Lowest label that still looks good (Enter to skip): ", burned)
    hi = (
        _ask_label("Highest label that still looks good (Enter to skip): ", burned)
        if lo is not None
        else None
    )
    if lo is None or hi is None:
        print("No result saved.")
        return 0
    lo, hi = min(lo, hi), max(lo, hi)
    if lo == burned[0] or hi == burned[-1]:
        print(
            "Note: the good range reaches the end of the ladder; consider a wider --range to find its edge."
        )
    centre = (lo + hi) / 2
    lens_key = s.focus.lens.lower()
    new_target = round(reference + centre - s.focus.offset_mm, 1)
    old_target = s.focus.target_b_mm if lens_key == "b" else s.focus.target_a_mm
    print(f"Good range {lo:+g} .. {hi:+g} mm -> centre {centre:+g} mm from the autofocus height.")
    if abs(new_target - old_target) < 0.1:
        print(f"Lens {lens_key.upper()} focus height {old_target:.1f} mm is already centred.")
        return 0
    path = Path(args.config) if args.config else config.default_config_path()
    answer = _prompt(
        f"Save lens {lens_key.upper()} focus height {old_target:.1f} -> {new_target:.1f} mm to {path}? [y/N] "
    )
    if answer.strip().lower() not in ("y", "yes"):
        print("Not saved.")
        return 0
    # Start from the file itself so one-off overrides (e.g. --lens) are not persisted.
    stored = config.load(path) if path.exists() else config.Settings()
    stored = replace(stored, focus=replace(stored.focus, **{f"target_{lens_key}_mm": new_target}))
    config.save(stored, path)
    print(f"Saved. Autofocus now targets {new_target:.1f} mm for lens {lens_key.upper()}.")
    return 0


def _prompt(text: str) -> str:
    try:
        return input(text)
    except EOFError:
        return "q"


def cmd_calibration(args) -> int:
    """Show the factory calibration stored in the laser and compare it with the settings file."""
    _check_other_software(args.force)
    dev, ctl = _open_controller()
    with dev:
        raw = _read_laser_calibration(ctl)
    laser = config.from_commarker(raw)
    path = config.settings_path(Path(args.config) if args.config else None)
    current = config.load(path) if path.exists() else None
    rows = [
        ("lens A focus (sensor mm)", "target_a_mm", laser.focus.target_a_mm),
        ("lens B focus (sensor mm)", "target_b_mm", laser.focus.target_b_mm),
        ("lens A field (mm)", "field_a_mm", laser.focus.field_a_mm),
        ("lens B field (mm)", "field_b_mm", laser.focus.field_b_mm),
        ("sensor range min (mm)", "sensor_min_mm", laser.focus.sensor_min_mm),
        ("sensor range max (mm)", "sensor_max_mm", laser.focus.sensor_max_mm),
    ]
    print(f"{'stored in the laser':28s}{'laser':>10s}{'settings':>12s}")
    for label, key, value in rows:
        mine = getattr(current.focus, key) if current else None
        flag = "" if mine is None or abs(mine - value) < 1e-6 else "   <- differs"
        print(f"{label:28s}{value:10.1f}{'' if mine is None else f'{mine:12.1f}'}{flag}")
    z = laser.z_axis
    print(f"Z axis: axis {z.axis_id}, {z.axis_params().pulses_per_mm:g} pulses/mm, reversed={z.reverse}")
    if args.save:
        config.save(config.from_commarker(raw, base=current), path)
        print(f"Saved to {path} (lens choice, offset and safety settings kept).")
    elif current is None:
        print(f"No settings file yet; '--save' writes these values to {path}.")
    return 0


def cmd_config(args) -> int:
    path = Path(args.config) if args.config else config.default_config_path()
    if args.action == "path":
        print(path)
    elif args.action == "show":
        print(f"# effective settings (file: {path}{'' if path.exists() else ', not present'})")
        print(config.dumps(_settings(args)))
    elif args.action == "init":
        if args.from_laser:
            _check_other_software(args.force)
            dev, ctl = _open_controller()
            with dev:
                s = config.from_commarker(_read_laser_calibration(ctl))
            print("Read the factory calibration from the laser")
        elif args.from_commarker:
            s = config.load_commarker(Path(args.commarker_dir))
            print(f"Imported values from {args.commarker_dir}")
        else:
            s = config.Settings()
            print("Wrote example values: set focus.target_a_mm / target_b_mm from your machine's card")
        if path.exists() and not args.overwrite:
            print(f"{path} already exists (use --overwrite).")
            return 1
        config.save(s, path)
        print(f"Wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omni-autofocus", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v: info, -vv: USB frame dump")
    p.add_argument("--config", help="settings file (default: %%APPDATA%%\\omni-autofocus\\config.toml)")
    p.add_argument(
        "--lens", choices=["a", "b", "auto"], help="lens A (small field) or B (large field); default: auto"
    )
    p.add_argument("--force", action="store_true", help="run even if ComMarker Studio is open")
    p.add_argument("--simulate", action="store_true", help="talk to a simulated controller instead of USB")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list controllers on the CYUSB driver").set_defaults(func=cmd_devices)

    sp = sub.add_parser("status", help="read controller state (read-only)")
    sp.add_argument("--raw", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("height", help="read the height sensor (read-only)")
    sp.add_argument("-n", "--count", type=int, default=1)
    sp.add_argument("--interval", type=float, default=0.5)
    sp.set_defaults(func=cmd_height)

    sp = sub.add_parser("move-z", help="relative Z move in mm (positive = larger sensor distance)")
    sp.add_argument("mm", type=float)
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sp.set_defaults(func=cmd_move_z)

    sp = sub.add_parser("focus", help="measure and move Z to the focus height")
    sp.add_argument("--dry-run", action="store_true", help="only print the planned move")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--passes", type=int, default=2, help="measure/move iterations (default 2)")
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser(
        "focus-ladder", help="dial in focus: test burns from -4 to +4 mm, then save the best height"
    )
    sp.add_argument("--range", type=float, default=4.0, help="test from -RANGE to +RANGE mm (default 4)")
    sp.add_argument("--step", type=float, default=1.0, help="step between marks in mm (default 1)")
    sp.add_argument("--offsets", help="explicit comma-separated offsets instead of --range/--step")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask before starting")
    sp.set_defaults(func=cmd_focus_ladder)

    sp = sub.add_parser("calibration", help="show the factory calibration stored in the laser (read-only)")
    sp.add_argument("--save", action="store_true", help="write it to the settings file")
    sp.set_defaults(func=cmd_calibration)

    sp = sub.add_parser("config", help="manage the settings file")
    sp.add_argument("action", choices=["show", "init", "path"])
    sp.add_argument(
        "--from-laser", action="store_true", help="init: read the factory calibration from the laser"
    )
    sp.add_argument("--from-commarker", action="store_true", help="init: import values from ComMarker Studio")
    sp.add_argument("--commarker-dir", default=str(config.COMMARKER_DIR))
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. With no arguments at all (e.g. the .exe double-clicked) it runs ``focus`` and keeps
    the console window open until Enter is pressed."""
    if argv is None and len(sys.argv) == 1:
        code = _run(["focus"])
        _pause()
        return code
    return _run(argv)


def _pause() -> None:
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass


def _run(argv: list[str] | None) -> int:
    global _SIMULATE
    args = build_parser().parse_args(argv)
    _SIMULATE = args.simulate
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (ControllerError, OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
