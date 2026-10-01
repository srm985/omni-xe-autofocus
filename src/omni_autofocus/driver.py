"""Check that the laser has the USB driver it needs (Cypress CyUSB3) and help the user install it.

The tool, LightBurn and ComMarker Studio all reach the controller through Cypress's CyUSB3 driver.
Its licence only lets hardware makers redistribute it, so this tool does not ship it. ComMarker
ships it with ComMarker Studio (``CypressDriverInstaller.exe``), so if that installer is on this PC
(or on the USB stick that came with the laser) the user can run it from here.
"""

from __future__ import annotations

import ctypes
import os
import string
import sys
from ctypes import wintypes as wt
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import config
from .cyusb import _GUID, INVALID_HANDLE_VALUE, SUPPORTED_IDS, UsbError

DRIVER_SERVICE = "cyusb3"
INSTALLER_NAME = "CypressDriverInstaller.exe"
DOWNLOAD_URL = "https://commarker.com/download-center"
PROBLEM_NOT_INSTALLED = 28  # CM_PROB_FAILED_INSTALL: "the drivers for this device are not installed"

DIGCF_PRESENT = 0x02
DIGCF_ALLCLASSES = 0x04
SPDRP_DEVICEDESC = 0x00
SPDRP_HARDWAREID = 0x01
SPDRP_SERVICE = 0x04
SPDRP_FRIENDLYNAME = 0x0C
ERROR_NO_MORE_ITEMS = 259
DN_HAS_PROBLEM = 0x400
DRIVE_REMOVABLE = 2


@dataclass(frozen=True)
class UsbDevice:
    hardware_ids: tuple[str, ...]
    name: str
    service: str  # bound driver service, "" when no driver is installed
    problem: int  # Device Manager problem code, 0 when the device works

    @property
    def is_laser(self) -> bool:
        ids = " ".join(self.hardware_ids).lower()
        return any(f"vid_{v}&pid_{p}" in ids for v, p in SUPPORTED_IDS)


class State(Enum):
    OK = "ok"
    NO_LASER = "no laser"
    NO_DRIVER = "no driver"
    WRONG_DRIVER = "wrong driver"
    PROBLEM = "driver problem"


@dataclass(frozen=True)
class Diagnosis:
    state: State
    device: UsbDevice | None
    message: str


def diagnose(devices: list[UsbDevice]) -> Diagnosis:
    lasers = [d for d in devices if d.is_laser]
    if not lasers:
        return Diagnosis(
            State.NO_LASER,
            None,
            "No ComMarker/BSL laser is connected over USB. Check that the laser is switched on and the "
            "USB cable is plugged in (try another cable or port).",
        )
    working = next((d for d in lasers if d.service.lower() == DRIVER_SERVICE and not d.problem), None)
    if working:
        return Diagnosis(State.OK, working, f"Laser found ('{working.name}'), using the CyUSB3 driver.")
    dev = lasers[0]
    if not dev.service or dev.problem == PROBLEM_NOT_INSTALLED:
        return Diagnosis(
            State.NO_DRIVER, dev, f"The laser is connected ('{dev.name}') but has no driver installed."
        )
    if dev.service.lower() != DRIVER_SERVICE:
        return Diagnosis(
            State.WRONG_DRIVER,
            dev,
            f"The laser is connected but uses the '{dev.service}' driver. This tool, LightBurn and "
            "ComMarker Studio need Cypress's CyUSB3 driver.",
        )
    return Diagnosis(
        State.PROBLEM,
        dev,
        f"The laser has the CyUSB3 driver, but Windows reports a problem with it (code {dev.problem}).",
    )


def guidance(diag: Diagnosis, *, staged: bool, installer: Path | None) -> list[str]:
    """What the user should do next, as lines of text."""
    if diag.state in (State.OK, State.NO_LASER):
        return []
    if diag.state is State.WRONG_DRIVER:
        steps = [
            "In Device Manager, right-click the laser, choose Update driver > Browse my computer > "
            "Let me pick, and select 'Cypress FX2LP Sample Device'.",
        ]
        if not staged:
            steps.insert(0, "First install the driver (see below), then switch the laser to it:")
    elif diag.state is State.PROBLEM:
        steps = ["Unplug the laser's USB cable, wait a few seconds and plug it back in."]
    elif staged:
        steps = [
            "Windows already has the driver. Unplug the laser's USB cable and plug it back in.",
            "If that does not help: Device Manager > right-click the laser > Update driver > "
            "Search automatically for drivers.",
        ]
    else:
        steps = []
    if installer:
        steps.append(f"ComMarker's driver installer was found at {installer}; it can be run from here.")
    elif not staged:
        steps += [
            f"Download ComMarker Studio for Windows from {DOWNLOAD_URL} and install it: its installer",
            "includes the driver. (The USB stick that came with the laser may have it too.)",
            "Then unplug and replug the laser's USB cable. ComMarker Studio can stay installed;",
            "just close it while using this tool.",
        ]
    steps.append("Run 'omni-autofocus driver' again to check.")
    return steps


def no_device_message() -> str:
    """Explain why no controller could be opened (used when the CyUSB3 driver lists no laser)."""
    try:
        diag = diagnose(usb_devices())
    except (OSError, UsbError):
        return "No ComMarker/BSL laser found. Run 'omni-autofocus driver' to check the USB driver."
    if diag.state in (State.OK, State.NO_LASER):
        return diag.message
    return diag.message + " Run 'omni-autofocus driver' for help installing it."


# --- where the driver can come from ---------------------------------------------------------------


def driver_staged(driver_store: Path | None = None) -> bool:
    """True if Windows' driver store already holds a Cypress driver package that matches the laser."""
    store = (
        driver_store
        or Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/DriverStore/FileRepository"
    )
    ids = [f"vid_{v}&pid_{p}" for v, p in SUPPORTED_IDS]
    try:
        infs = [inf for d in store.glob("cyusb*.inf_*") for inf in d.glob("*.inf")]
    except OSError:
        return False
    for inf in infs:
        try:
            raw = inf.read_bytes()
        except OSError:
            continue
        text = (
            raw.decode("utf-16", "ignore") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("latin-1")
        )
        if any(i in text.lower() for i in ids):
            return True
    return False


def find_installers(extra_dirs: list[Path] | None = None) -> list[Path]:
    """ComMarker's driver installer in a ComMarker Studio install or on a removable drive."""
    dirs = [config.COMMARKER_DIR, Path(r"C:\Program Files\ComMarker_Studio")]
    dirs += extra_dirs if extra_dirs is not None else _removable_drives()
    found = []
    for base in dirs:
        for path in _search(base, depth=2):
            if path not in found:
                found.append(path)
    return found


def _search(base: Path, depth: int) -> list[Path]:
    hits = []
    try:
        for entry in os.scandir(base):
            if entry.is_file() and entry.name.lower() == INSTALLER_NAME.lower():
                hits.append(Path(entry.path))
            elif depth > 0 and entry.is_dir(follow_symlinks=False):
                hits += _search(Path(entry.path), depth - 1)
    except OSError:
        pass
    return hits


def _removable_drives() -> list[Path]:
    if sys.platform != "win32":
        return []
    k32 = ctypes.WinDLL("kernel32")
    mask = k32.GetLogicalDrives()
    roots = [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask >> i & 1]
    return [Path(r) for r in roots if k32.GetDriveTypeW(r) == DRIVE_REMOVABLE]


def run_installer(path: Path) -> int:
    """Run ComMarker's driver installer (Windows shows its administrator prompt) and wait for it."""

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wt.HWND),
            ("lpVerb", wt.LPCWSTR),
            ("lpFile", wt.LPCWSTR),
            ("lpParameters", wt.LPCWSTR),
            ("lpDirectory", wt.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wt.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wt.LPCWSTR),
            ("hkeyClass", wt.HKEY),
            ("dwHotKey", wt.DWORD),
            ("hIconOrMonitor", wt.HANDLE),
            ("hProcess", wt.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(path)
    info.lpDirectory = str(path.parent)
    info.nShow = 1  # SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise UsbError(ctypes.get_last_error(), f"could not start {path}")
    if not info.hProcess:
        return 0
    try:
        k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wt.DWORD(0)
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(info.hProcess)


# --- Windows device enumeration ---------------------------------------------------------------------


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wt.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


def usb_devices() -> list[UsbDevice]:
    """All present USB devices with their bound driver and problem code (with or without a driver)."""
    if sys.platform != "win32":
        raise UsbError("USB driver checks only work on Windows")
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    cfgmgr = ctypes.WinDLL("cfgmgr32")
    setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
    cfgmgr.CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(wt.ULONG),
        ctypes.POINTER(wt.ULONG),
        wt.DWORD,
        wt.ULONG,
    ]

    hdi = setupapi.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if hdi in (None, INVALID_HANDLE_VALUE):
        raise UsbError(ctypes.get_last_error(), "SetupDiGetClassDevs failed")

    def prop(info, key) -> list[str]:
        buf = ctypes.create_unicode_buffer(1024)
        if not setupapi.SetupDiGetDeviceRegistryPropertyW(
            hdi, ctypes.byref(info), key, None, buf, ctypes.sizeof(buf), None
        ):
            return []
        raw = ctypes.wstring_at(ctypes.addressof(buf), len(buf))
        return [s for s in raw.split("\0") if s]  # REG_MULTI_SZ or REG_SZ

    out = []
    try:
        index = 0
        while True:
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setupapi.SetupDiEnumDeviceInfo(hdi, index, ctypes.byref(info)):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                raise UsbError(ctypes.get_last_error(), "SetupDiEnumDeviceInfo failed")
            index += 1
            status, problem = wt.ULONG(0), wt.ULONG(0)
            if cfgmgr.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0):
                problem.value = 0
            name = (prop(info, SPDRP_FRIENDLYNAME) or prop(info, SPDRP_DEVICEDESC) or ["?"])[0]
            out.append(
                UsbDevice(
                    hardware_ids=tuple(prop(info, SPDRP_HARDWAREID)),
                    name=name,
                    service=(prop(info, SPDRP_SERVICE) or [""])[0],
                    problem=problem.value if status.value & DN_HAS_PROBLEM else 0,
                )
            )
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdi)
    return out
