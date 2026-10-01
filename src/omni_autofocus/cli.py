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

from . import __version__, autofocus, config
from .controller import Controller, ControllerError

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
        from .simulator import SimulatedBoard

        board = SimulatedBoard(sensor_mm=205.0)
        return contextlib.nullcontext(board), Controller(board, sleep=lambda s: None)
    from .cyusb import CyUsbDevice  # imported lazily: Windows-only

    dev = CyUsbDevice.open_first()
    log.info("opened %s", dev.path)
    return dev, Controller(dev)


def _settings(args) -> config.Settings:
    s = config.load(Path(args.config) if args.config else None)
    if args.lens:
        s = replace(s, focus=replace(s.focus, lens=args.lens))
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
    if args.raw:
        print(f"AA05 reply      : {st.raw.hex(' ')}")
        print(f"AA07 reply      : {ext.raw.hex(' ')}")
    return 0


def cmd_height(args) -> int:
    _check_other_software(args.force)
    s = _settings(args)
    dev, ctl = _open_controller()
    with dev:
        for i in range(args.count):
            h = ctl.read_height()
            print(f"{h:.3f} mm   (focus target {s.focus.target_mm:.3f} mm, lens {s.focus.lens.upper()})")
            if i + 1 < args.count:
                time.sleep(args.interval)
    return 0


def cmd_move_z(args) -> int:
    _check_other_software(args.force)
    s = _settings(args)
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
        ctl.move_axis(params, pulses)
        after = _try_height(ctl)
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
    s = _settings(args)
    dev, ctl = _open_controller()
    with dev:
        for iteration in range(1, args.passes + 1):
            height = ctl.read_height()
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
            if args.dry_run:
                print("Dry run: not moving.")
                return 0
            if iteration == 1 and not _confirm("Move the Z axis now?", args.yes):
                print("Cancelled.")
                return 1
            ctl.move_axis(s.z_axis.axis_params(), p.pulses)
        final = autofocus.plan(ctl.read_height(), s)
        print(f"final: sensor {final.height_mm:.3f} mm, error {final.target_mm - final.height_mm:+.3f} mm")
    return 0


def cmd_config(args) -> int:
    path = Path(args.config) if args.config else config.default_config_path()
    if args.action == "path":
        print(path)
    elif args.action == "show":
        print(f"# effective settings (file: {path}{'' if path.exists() else ', not present'})")
        print(config.dumps(_settings(args)))
    elif args.action == "init":
        if args.from_commarker:
            s = config.load_commarker(Path(args.commarker_dir))
            print(f"Imported values from {args.commarker_dir}")
        else:
            s = config.Settings()
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
    p.add_argument("--lens", choices=["a", "b"], help="override the configured lens")
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

    sp = sub.add_parser("config", help="manage the settings file")
    sp.add_argument("action", choices=["show", "init", "path"])
    sp.add_argument("--from-commarker", action="store_true", help="init: import values from ComMarker Studio")
    sp.add_argument("--commarker-dir", default=str(config.COMMARKER_DIR))
    sp.add_argument("--overwrite", action="store_true")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
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
