"""Bulk USB access to the controller through Cypress's CYUSB3 driver (Windows only).

This mirrors what Cypress's CyAPI (statically linked into the vendor's transfer.dll) does:
devices are found by the CYUSB driver interface GUID and bulk transfers are issued with
``DeviceIoControl(IOCTL_ADAPT_SEND_NON_EP0_DIRECT)`` using a 38-byte ``SINGLE_TRANSFER`` header.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from ctypes import wintypes as wt
from dataclasses import dataclass

log = logging.getLogger(__name__)

CYUSB_GUID = uuid.UUID("ae18aa60-7f6a-11d4-97dd-00010229b959")
SUPPORTED_IDS = (("04b4", "1004"), ("7580", "0101"))

IOCTL_ADAPT_SEND_NON_EP0_DIRECT = 0x0022004B
IOCTL_ADAPT_ABORT_PIPE = 0x00220044
IOCTL_ADAPT_RESET_PIPE = 0x0022002C
SINGLE_TRANSFER_SIZE = 38
SINGLE_TRANSFER_EP_OFFSET = 0x0D

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class UsbError(OSError):
    pass


class UsbTimeout(UsbError, TimeoutError):
    pass


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_uuid(cls, u: uuid.UUID) -> _GUID:
        g = cls()
        ctypes.memmove(ctypes.byref(g), u.bytes_le, 16)
        return g


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wt.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wt.DWORD),
        ("OffsetHigh", wt.DWORD),
        ("hEvent", wt.HANDLE),
    ]


def _win():
    if sys.platform != "win32":
        raise UsbError("the CYUSB transport only works on Windows")
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        wt.DWORD,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]

    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        wt.HANDLE,
    ]
    kernel32.CreateEventW.restype = wt.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    kernel32.DeviceIoControl.restype = wt.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(_OVERLAPPED),
    ]
    kernel32.GetOverlappedResult.restype = wt.BOOL
    kernel32.GetOverlappedResult.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(_OVERLAPPED),
        ctypes.POINTER(wt.DWORD),
        wt.BOOL,
    ]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.CancelIoEx.restype = wt.BOOL
    kernel32.CancelIoEx.argtypes = [wt.HANDLE, ctypes.POINTER(_OVERLAPPED)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    return setupapi, kernel32


@dataclass(frozen=True)
class DeviceInfo:
    path: str

    @property
    def supported(self) -> bool:
        p = self.path.lower()
        return any(f"vid_{v}&pid_{d}" in p for v, d in SUPPORTED_IDS)


def list_devices() -> list[DeviceInfo]:
    """All present devices bound to the CYUSB driver."""
    setupapi, _ = _win()
    guid = _GUID.from_uuid(CYUSB_GUID)
    hdi = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdi in (None, INVALID_HANDLE_VALUE):
        raise UsbError(ctypes.get_last_error(), "SetupDiGetClassDevs failed")
    devices = []
    try:
        index = 0
        while True:
            ifd = _SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                hdi, None, ctypes.byref(guid), index, ctypes.byref(ifd)
            ):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                raise UsbError(ctypes.get_last_error(), "SetupDiEnumDeviceInterfaces failed")
            required = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdi, ctypes.byref(ifd), None, 0, ctypes.byref(required), None
            )
            buf = ctypes.create_string_buffer(required.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is 8 on 64-bit, 6 on 32-bit.
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdi, ctypes.byref(ifd), buf, required, None, None
            ):
                raise UsbError(ctypes.get_last_error(), "SetupDiGetDeviceInterfaceDetail failed")
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            devices.append(DeviceInfo(path))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdi)
    return devices


class CyUsbDevice:
    """An open handle to the controller with bulk read/write by endpoint address."""

    def __init__(self, path: str):
        _, self._k32 = _win()
        self.path = path
        h = self._k32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None,
        )
        if h in (None, INVALID_HANDLE_VALUE):
            raise UsbError(ctypes.get_last_error(), f"cannot open {path}")
        self._h = h

    @classmethod
    def open_first(cls) -> CyUsbDevice:
        devices = [d for d in list_devices() if d.supported]
        if not devices:
            raise UsbError(
                "no ComMarker/BSL controller found (is the laser on, connected, and using the CYUSB3 driver?)"
            )
        if len(devices) > 1:
            log.warning("several controllers found, using %s", devices[0].path)
        return cls(devices[0].path)

    def close(self) -> None:
        if self._h:
            self._k32.CloseHandle(self._h)
            self._h = None

    def __enter__(self) -> CyUsbDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transfers --------------------------------------------------------------------------------

    def write(self, endpoint: int, data: bytes, timeout_ms: int) -> int:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return self._xfer(endpoint, buf, len(data), timeout_ms)

    def read(self, endpoint: int, size: int, timeout_ms: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = self._xfer(endpoint, buf, size, timeout_ms)
        return buf.raw[:n]

    def _xfer(self, endpoint: int, buf, length: int, timeout_ms: int) -> int:
        k = self._k32
        header = (ctypes.c_ubyte * SINGLE_TRANSFER_SIZE)()
        header[SINGLE_TRANSFER_EP_OFFSET] = endpoint
        ov = _OVERLAPPED()
        ov.hEvent = k.CreateEventW(None, True, False, None)
        transferred = wt.DWORD(0)
        try:
            ok = k.DeviceIoControl(
                self._h,
                IOCTL_ADAPT_SEND_NON_EP0_DIRECT,
                header,
                SINGLE_TRANSFER_SIZE,
                buf,
                length,
                ctypes.byref(transferred),
                ctypes.byref(ov),
            )
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    raise UsbError(err, f"transfer on endpoint 0x{endpoint:02x} failed")
                if k.WaitForSingleObject(ov.hEvent, timeout_ms) != WAIT_OBJECT_0:
                    self._cancel(endpoint, ov)
                    raise UsbTimeout(f"endpoint 0x{endpoint:02x} timed out after {timeout_ms} ms")
            if not k.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(transferred), False):
                raise UsbError(ctypes.get_last_error(), f"transfer on endpoint 0x{endpoint:02x} failed")
            return transferred.value
        finally:
            k.CloseHandle(ov.hEvent)

    def _cancel(self, endpoint: int, ov: _OVERLAPPED) -> None:
        """Abort the pipe (as CyAPI does) and make sure the pending request has finished with our buffers."""
        k = self._k32
        self._simple_ioctl(IOCTL_ADAPT_ABORT_PIPE, endpoint)
        if k.WaitForSingleObject(ov.hEvent, 500) != WAIT_OBJECT_0:
            k.CancelIoEx(self._h, ctypes.byref(ov))
        dummy = wt.DWORD(0)
        k.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(dummy), True)

    def reset_pipe(self, endpoint: int) -> None:
        self._simple_ioctl(IOCTL_ADAPT_RESET_PIPE, endpoint)

    def _simple_ioctl(self, code: int, endpoint: int) -> None:
        k = self._k32
        arg = ctypes.c_ubyte(endpoint)
        ov = _OVERLAPPED()
        ov.hEvent = k.CreateEventW(None, True, False, None)
        dummy = wt.DWORD(0)
        try:
            ok = k.DeviceIoControl(
                self._h, code, ctypes.byref(arg), 1, None, 0, ctypes.byref(dummy), ctypes.byref(ov)
            )
            if not ok and ctypes.get_last_error() == ERROR_IO_PENDING:
                if k.WaitForSingleObject(ov.hEvent, 1000) != WAIT_OBJECT_0:
                    k.CancelIoEx(self._h, ctypes.byref(ov))
                # Never return while the driver may still reference `arg`/`ov`.
                k.GetOverlappedResult(self._h, ctypes.byref(ov), ctypes.byref(dummy), True)
        finally:
            k.CloseHandle(ov.hEvent)
