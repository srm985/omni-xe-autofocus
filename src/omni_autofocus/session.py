"""What the command line and the app share: opening the laser, settings with first-run calibration,
the lens choice, and checks for other software that talks to the laser."""

from __future__ import annotations

import contextlib
import csv
import io
import logging
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from . import config, flash, lens
from .controller import Controller, ControllerError

log = logging.getLogger(__name__)

CONFLICTING_PROGRAMS = {"commarker_studio.exe": "ComMarker Studio"}
WARN_PROGRAMS = {"lightburn.exe": "LightBurn"}


class ConflictError(ControllerError):
    """Another program that must not run at the same time (ComMarker Studio) is running."""


LASER_LOCK_NAME = "Local\\OmniAutofocusLaser"


class LaserLock:
    """Held while this program talks to the laser, so two Omni Autofocus processes (say the app's
    hotkey during a fine-tuning run) never move Z at the same time. Windows only; a no-op elsewhere."""

    def __init__(self, name: str = LASER_LOCK_NAME):
        self.name = name
        self._handle = None

    def __enter__(self) -> LaserLock:
        if sys.platform != "win32":
            return self
        import ctypes
        from ctypes import wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wt.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        handle = k32.CreateMutexW(None, False, self.name)
        if not handle:
            return self  # cannot lock: carry on rather than block the user
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: someone else holds it
            k32.CloseHandle(handle)
            raise ConflictError(
                "another Omni Autofocus window is using the laser (fine-tuning?). Finish there first."
            )
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._handle:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


def running_programs() -> set[str]:
    if sys.platform != "win32":
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {row[0].lower() for row in csv.reader(io.StringIO(out)) if row}


def read_laser_calibration(ctl: Controller) -> dict:
    """The ComMarker parameter file the factory stored in the controller's flash."""
    return config.decode_commarker_cfg(flash.read_commarker_file(ctl, "lcsparam.cfg"))


class Session:
    """One user's view of the laser: where the settings live and how to reach the controller.

    With ``simulate`` a built-in simulated laser is used instead of USB, and it keeps its Z
    position between operations of the same session.
    """

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        lens_override: str | None = None,
        simulate: bool = False,
        force: bool = False,
    ):
        self.path = config.settings_path(config_path)
        self.lens_override = lens_override
        self.simulate = simulate
        self.force = force
        self._board = None

    # -- the laser ----------------------------------------------------------------------------------

    def open(self):
        """``(context manager, Controller)``; use as ``dev, ctl = s.open(); with dev: ...``."""
        if self.simulate:
            from .simulator import FakeClock, SimulatedBoard, factory_flash_image

            if self._board is None:
                self._board = SimulatedBoard(sensor_mm=205.0, flash_image=factory_flash_image())
            clock = FakeClock()
            return contextlib.nullcontext(self._board), Controller(
                self._board, sleep=clock.sleep, clock=clock
            )
        from .cyusb import CyUsbDevice  # imported lazily: Windows-only

        stack = contextlib.ExitStack()
        stack.enter_context(LaserLock())
        try:
            dev = stack.enter_context(CyUsbDevice.open_first())
        except BaseException:
            stack.close()
            raise
        log.info("opened %s", dev.path)
        return stack, Controller(dev)

    def check_other_software(self) -> str | None:
        """Raise if ComMarker Studio is running (unless forced); return a note if LightBurn is."""
        if self.simulate:
            return None
        running = running_programs()
        for exe, name in CONFLICTING_PROGRAMS.items():
            if exe in running and not self.force:
                raise ConflictError(f"{name} is running and talks to the laser constantly. Close it first.")
        for exe, name in WARN_PROGRAMS.items():
            if exe in running:
                return f"{name} is running. That is fine, but do not start a job until this finishes."
        return None

    # -- settings -----------------------------------------------------------------------------------

    def factory_calibration(self) -> tuple[config.Settings, str]:
        """Calibration from the laser itself, else (laser reachable, but its store unreadable) from an
        installed ComMarker Studio. Without a laser this raises: nothing can be focused then anyway, and
        the laser's own values should win once it is connected."""
        dev, ctl = self.open()
        try:
            with dev:
                return config.from_commarker(read_laser_calibration(ctl)), "the laser"
        except (flash.FlashError, ValueError, KeyError, IndexError) as e:
            # The laser answered but has no usable stored calibration. Busy or communication errors
            # propagate instead: retrying later gets the laser's own values.
            log.info("the laser has no usable stored calibration: %s", e)
        try:
            return config.load_commarker(config.COMMARKER_DIR), "ComMarker Studio's settings"
        except (OSError, ValueError, KeyError, IndexError) as e:
            log.info("no ComMarker Studio settings: %s", e)
        raise ControllerError(
            "no focus calibration found: the laser's stored calibration could not be read and "
            "ComMarker Studio is not installed. Is the laser on and connected? Otherwise run "
            "'omni-autofocus config init' and enter the focus heights from the card that came with the "
            "machine as focus.target_a_mm / focus.target_b_mm."
        )

    def settings(self, *, calibrate: bool = False) -> tuple[config.Settings, str | None]:
        """Settings from the file, plus a note when something noteworthy happened.

        With ``calibrate``, a missing file is first created from the factory calibration (laser, then
        ComMarker Studio) so built-in example values are never used silently.
        """
        note = None
        if calibrate and not self.path.exists():
            s, source = self.factory_calibration()
            config.save(s, self.path)
            note = f"First run: saved the factory calibration from {source} to {self.path}"
        s = config.load(self.path)
        if self.lens_override:
            s = replace(s, focus=replace(s.focus, lens=self.lens_override))
        return s, note

    def settings_with_lens(self, *, calibrate: bool = True) -> tuple[config.Settings, str, str | None]:
        """Settings with the lens resolved (explicit, LightBurn, then ComMarker), the reason for the
        choice, and the first-run note if any. Without ``calibrate`` and without a settings file, the
        focus heights are only placeholders: use it to show the lens, not to focus."""
        s, note = self.settings(calibrate=calibrate)
        try:
            s, why = lens.resolve(s)
        except lens.LensError:
            if not self.simulate:
                raise
            s, why = replace(s, focus=replace(s.focus, lens="b")), "lens B (simulation default)"
        return s, why, note
