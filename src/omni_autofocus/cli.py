"""Command-line interface: ``omni-autofocus <command>``."""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, autofocus, config, ladder, session
from .controller import Controller, ControllerBusyError, ControllerError

log = logging.getLogger("omni_autofocus")

_SESSION = session.Session()  # replaced in _run() from the command-line options


def _check_other_software(force: bool = False) -> None:
    try:
        note = _SESSION.check_other_software()
    except session.ConflictError as e:
        raise ControllerError(f"{e} (Or use --force.)") from None
    if note:
        print(f"Note: {note}")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _open_controller():
    return _SESSION.open()


def _read_laser_calibration(ctl: Controller) -> dict:
    return session.read_laser_calibration(ctl)


def _accept_calibration(args):
    """First use: show the focus heights found and ask before saving them (simulation: just show)."""

    def accept(found: config.Settings, source: str) -> bool:
        f = found.focus
        heights = f"lens A {f.target_a_mm:.1f} mm, lens B {f.target_b_mm:.1f} mm"
        print(f"First use: focus heights from {source}: {heights}")
        if _SESSION.simulate:
            return True
        return _confirm(f"Use them and save them to {_SESSION.path}?", getattr(args, "yes", False))

    return accept


def _settings(args, *, calibrate: bool = False) -> config.Settings:
    """Settings from the file; with ``calibrate`` a missing file is created from the factory calibration."""
    s, note = _SESSION.settings(calibrate=calibrate, accept=_accept_calibration(args))
    if note:
        print(note)
    return s


def _settings_with_lens(args) -> config.Settings:
    """Settings with the lens resolved (explicit, LightBurn, then ComMarker); prints the choice."""
    s, why, note = _SESSION.settings_with_lens(accept=_accept_calibration(args))
    if note:
        print(note)
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


def cmd_app(args) -> int:
    """Open the Omni Autofocus window (Autofocus button and hotkey)."""
    from .app import main as app_main

    argv = ["--config", args.config] if args.config else []
    return app_main(argv + (["--simulate"] if args.simulate else []))


def cmd_driver(args) -> int:
    """Check the laser's USB driver and help install it."""
    from . import driver

    diag = driver.diagnose(driver.usb_devices())
    print(diag.message)
    if diag.state is driver.State.OK:
        return 0
    if diag.state is driver.State.NO_LASER:
        return 1
    installers = driver.find_installers()
    installer = installers[0] if installers else None
    print()
    for line in driver.guidance(diag, staged=driver.driver_staged(), installer=installer):
        print(line)
    if installer and _confirm(
        f"\nRun {installer.name} now? Windows will ask for administrator permission.", args.yes
    ):
        code = driver.run_installer(installer)
        print(f"The installer finished (exit code {code}). Unplug and replug the laser's USB cable.")
        if _prompt("Press Enter when it is plugged back in (q + Enter to skip the check): ").strip() != "q":
            after = driver.diagnose(driver.usb_devices())
            print(after.message)
            return 0 if after.state is driver.State.OK else 1
    return 1


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
    if args.passes < 1:
        print("--passes must be at least 1.")
        return 2
    _check_other_software(args.force)
    s = _settings_with_lens(args)
    dev, ctl = _open_controller()
    with dev:
        return _autofocus(ctl, s, passes=args.passes, dry_run=args.dry_run, yes=args.yes)


def _autofocus(ctl: Controller, s: config.Settings, *, passes: int, dry_run: bool, yes: bool) -> int:
    def show(iteration: int, p: autofocus.FocusPlan) -> None:
        print(
            f"pass {iteration}: sensor {p.height_mm:.3f} mm, target {p.target_mm:.3f} mm, "
            f"move {p.move_mm:+.3f} mm ({p.pulses:+d} pulses)"
        )

    asked: list[bool] = []

    def confirm(p: autofocus.FocusPlan) -> bool:
        # Ask before the first move, and before any later large move towards the work.
        if asked and not autofocus.is_large_downward(p, s.app.confirm_down_above_mm):
            return True
        asked.append(True)
        return _confirm(f"Move the Z axis {p.move_mm:+.1f} mm now?", yes)

    try:
        result = autofocus.run(ctl, s, passes=passes, dry_run=dry_run, confirm=confirm, on_plan=show)
    except autofocus.FocusError as e:
        print(f"Error: {e}")
        return 2
    if result.outcome is autofocus.Outcome.DRY_RUN:
        print("Dry run: not moving.")
        return 0
    if result.outcome is autofocus.Outcome.CANCELLED:
        print("Cancelled.")
        return 1
    if len(result.plans) and not result.plans[-1].needed:
        print(f"In focus (error {result.error_mm:+.3f} mm).")
        return 0
    print(f"final: sensor {result.height_mm:.3f} mm, error {result.error_mm:+.3f} mm")
    if result.outcome is autofocus.Outcome.NOT_CONVERGED:
        print(f"Warning: still {result.error_mm:+.2f} mm from focus. Check the Z axis and run focus again.")
        return 3
    return 0


def cmd_focus_ladder(args) -> int:
    """Autofocus, step Z through offsets for test burns, then optionally save the dialled-in focus
    (see :mod:`omni_autofocus.ladder`)."""
    _check_other_software(args.force)
    s = _settings_with_lens(args)
    offsets = (
        ladder.parse_offsets(args.offsets) if args.offsets else ladder.default_offsets(args.range, args.step)
    )
    if ladder.reach_mm(offsets) > s.focus.max_move_mm:
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
        code = _autofocus(ctl, s, passes=2, dry_run=False, yes=args.yes)
        if code:
            return code
        run = ladder.Ladder(ctl, s, offsets)
        reference = run.measure_reference()
        print(f"Reference (offset 0): sensor reading {reference:.3f} mm")
        burned: list[float] = []
        faulted = False
        try:
            for offset in offsets:
                try:
                    _when_free(lambda o=offset: run.go_to(o))
                except _LeftInPlace:
                    break
                answer = _prompt(
                    f"\nZ is at {offset:+g} mm. Burn the mark labelled '{offset:+g}', "
                    "then press Enter (q + Enter to stop): "
                )
                if answer.strip().lower() == "q":
                    break
                burned.append(offset)
        except autofocus.MotionError:
            faulted = True  # do not move Z again after a fault
            print(f"{run.where()}; not moving it again.")
            raise
        finally:
            if run.current and not faulted:
                print(f"Returning Z to the autofocus height ({-run.current:+g} mm).")
                try:
                    _when_free(run.return_to_focus)
                except _LeftInPlace:
                    print(f"Z left at {run.current:+g} mm from focus; run 'omni-autofocus focus' to return.")
                except autofocus.MotionError as e:
                    faulted = True
                    print(f"Could not return Z to focus ({e}). {run.where()}.")
    if faulted:
        return 2
    if len(burned) < 2:
        return 0
    return _dial_in(args, s, burned, reference)


class _LeftInPlace(Exception):
    """The user chose not to retry a move the controller refused because it was busy."""


def _when_free(move) -> None:
    """Run a Z move; if the controller is busy (job or framing preview running), ask and retry."""
    while True:
        try:
            move()
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
    centre, new_target = ladder.best_focus(s.focus.target_mm, lo, hi)
    lens_key = s.focus.lens.lower()
    old_target = s.focus.target_b_mm if lens_key == "b" else s.focus.target_a_mm
    print(f"Good range {lo:+g} .. {hi:+g} mm -> centre {centre:+g} mm from the autofocus height.")
    if round(new_target - old_target, 1) == 0:
        print(f"Lens {lens_key.upper()} focus height {old_target:.1f} mm is already centred.")
        return 0
    _save_focus_target(args, lens_key, old_target, new_target, yes=False)
    return 0


def _save_focus_target(args, lens_key: str, old: float, new: float, *, yes: bool) -> bool:
    """Ask, then store ``new`` as the focus height (sensor reading) of lens ``lens_key``."""
    path = config.settings_path(Path(args.config) if args.config else None)
    question = f"Save lens {lens_key.upper()} focus height {old:.1f} -> {new:.1f} mm to {path}? [y/N] "
    if not yes and _prompt(question).strip().lower() not in ("y", "yes"):
        print("Not saved.")
        return False
    stored = ladder.save_focus_target(path, lens_key, new)
    print(f"Saved. Autofocus now targets {new:.1f} mm for lens {lens_key.upper()}.")
    if stored.focus.offset_mm:
        print(f"(focus.offset_mm = {stored.focus.offset_mm:+g} mm is still added on top.)")
    return True


def cmd_set_focus(args) -> int:
    """Set the current lens's focus height: typed in, measured at the current Z, or the factory value."""
    sources = [args.value is not None, args.here, args.factory]
    if sum(sources) != 1:
        print("Give exactly one of: a value in mm, --here, or --factory.")
        return 2
    s = _settings_with_lens(args)
    lens_key = s.focus.lens.lower()
    old = s.focus.target_b_mm if lens_key == "b" else s.focus.target_a_mm
    if args.value is not None:
        new = args.value
    else:
        _check_other_software(args.force)
        dev, ctl = _open_controller()
        with dev:
            if args.here:
                new = ctl.read_height_median(max(s.focus.samples, 5))
                print(f"Measured sensor reading at the current Z: {new:.3f} mm")
            else:
                factory = config.from_commarker(_read_laser_calibration(ctl)).focus
                new = factory.target_b_mm if lens_key == "b" else factory.target_a_mm
                print(f"Factory value stored in the laser for lens {lens_key.upper()}: {new:.1f} mm")
    new = round(new, 1)
    if not s.focus.sensor_min_mm <= new <= s.focus.sensor_max_mm:
        print(
            f"{new:.1f} mm is outside the sensor's range "
            f"{s.focus.sensor_min_mm:g}-{s.focus.sensor_max_mm:g} mm; remember the value is the "
            "sensor reading at focus, not a lens-to-work distance."
        )
        return 2
    if abs(new - old) < 0.05:
        print(f"Lens {lens_key.upper()} focus height is already {old:.1f} mm.")
        return 0
    _save_focus_target(args, lens_key, old, new, yes=args.yes)
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
    path = config.settings_path(Path(args.config) if args.config else None)
    if args.action == "set":
        if len(args.values) != 2:
            print("Usage: omni-autofocus config set SECTION.KEY VALUE (e.g. focus.offset_mm 0.3)")
            return 2
        if not path.exists():  # never start a file from the example focus heights by accident
            print(
                f"There is no settings file yet ({path}). Create it from the laser first: "
                "'omni-autofocus config init --from-laser' (or press Autofocus in the app)."
            )
            return 1
        stored = config.load(path)
        updated = config.set_value(stored, args.values[0], args.values[1])
        config.save(updated, path)
        print(f"Set {args.values[0]} = {args.values[1]} in {path}")
        return 0
    if args.values:
        print(f"'config {args.action}' takes no extra arguments.")
        return 2
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

    sub.add_parser("app", help="open the Autofocus window (button + hotkey)").set_defaults(func=cmd_app)

    sp = sub.add_parser("driver", help="check the laser's USB driver and help install it")
    sp.add_argument("-y", "--yes", action="store_true", help="run a found driver installer without asking")
    sp.set_defaults(func=cmd_driver)

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

    sp = sub.add_parser(
        "set-focus", help="set the focus height of the current lens (value, --here, or --factory)"
    )
    sp.add_argument("value", nargs="?", type=float, help="sensor reading at best focus, in mm")
    sp.add_argument("--here", action="store_true", help="use the sensor reading at the current Z height")
    sp.add_argument("--factory", action="store_true", help="restore the value stored in the laser")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask before saving")
    sp.set_defaults(func=cmd_set_focus)

    sp = sub.add_parser("config", help="manage the settings file")
    sp.add_argument("action", choices=["show", "init", "path", "set"])
    sp.add_argument("values", nargs="*", help="set: SECTION.KEY VALUE")
    sp.add_argument(
        "--from-laser", action="store_true", help="init: read the factory calibration from the laser"
    )
    sp.add_argument("--from-commarker", action="store_true", help="init: import values from ComMarker Studio")
    sp.add_argument("--commarker-dir", default=str(config.COMMARKER_DIR))
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. With no arguments (e.g. the .exe double-clicked) it runs ``focus``. When the tool
    has its own console window (double-click or a shortcut), the window stays open long enough to read
    the result: a few seconds after success, until Enter after a problem."""
    if argv is not None:
        return _run(argv)
    try:
        code = _run(sys.argv[1:] or ["focus"])
    except SystemExit as e:  # argparse errors and --help/--version
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception:  # noqa: BLE001 - show it in the window instead of closing on a traceback
        import traceback

        traceback.print_exc()
        code = 1
    if _owns_console():
        _pause(code)
    return code


AUTO_CLOSE_SECONDS = 5


def _owns_console() -> bool:
    """True when no shell shares our console, i.e. Windows opened the window just for us.

    A PyInstaller one-file .exe runs as two processes (bootloader and Python), so two is still ours.
    """
    if sys.platform != "win32":
        return False
    try:
        ids = (ctypes.c_uint32 * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(ids, 4)
    except (AttributeError, OSError):
        return False
    return 0 < count <= (2 if getattr(sys, "frozen", False) else 1)


def _pause(code: int) -> None:
    try:
        if code == 0:
            import msvcrt

            print(f"\nDone. This window closes in {AUTO_CLOSE_SECONDS} s (press any key to close now).")
            deadline = time.monotonic() + AUTO_CLOSE_SECONDS
            while time.monotonic() < deadline and not msvcrt.kbhit():
                time.sleep(0.05)
        else:
            input("\nPress Enter to close...")
    except (EOFError, ImportError, OSError):
        pass


def _run(argv: list[str]) -> int:
    global _SESSION
    for stream in (sys.stdout, sys.stderr):  # never fail on a character the console cannot show
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.simulate and not args.config:
        # Never let the simulator's calibration or test runs touch the real settings file.
        args.config = str(Path(tempfile.gettempdir()) / "omni-autofocus-simulate.toml")
    _SESSION = session.Session(
        config_path=Path(args.config) if args.config else None,
        lens_override=args.lens,
        simulate=args.simulate,
        force=args.force,
    )
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except session.NotAccepted:
        print("Not saved; nothing moved.", file=sys.stderr)
        return 1
    except autofocus.FocusError as e:  # refused, or Z did not follow a move (README: exit code 2)
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except (ControllerError, OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
