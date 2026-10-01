"""Look and feel of the app window: a light/dark palette that follows Windows, an antialiased
rounded button and a segmented control. Plain tkinter, no extra packages."""

from __future__ import annotations

import base64
import ctypes
import math
import os
import struct
import sys
import tkinter as tk
import zlib
from dataclasses import dataclass

FONT = "Segoe UI"


@dataclass(frozen=True)
class Palette:
    bg: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_disabled: str
    on_accent: str
    ok: str
    warn: str
    err: str
    info: str


LIGHT = Palette(
    bg="#ffffff",
    border="#d9dce1",
    text="#1f2328",
    muted="#6b7280",
    accent="#1859a0",
    accent_hover="#1f6bc0",
    accent_pressed="#134a86",
    accent_disabled="#a9bcd6",
    on_accent="#ffffff",
    ok="#1a7f37",
    warn="#9a6700",
    err="#cf222e",
    info="#8b929c",
)

DARK = Palette(
    bg="#202124",
    border="#474a51",
    text="#e8e9eb",
    muted="#9aa0a8",
    accent="#2b6fd6",
    accent_hover="#3a7fe3",
    accent_pressed="#2560bd",
    accent_disabled="#34465e",
    on_accent="#ffffff",
    ok="#3fb950",
    warn="#d29922",
    err="#f85149",
    info="#80868f",
)


def windows_prefers_dark() -> bool:
    forced = os.environ.get("OMNI_AUTOFOCUS_THEME", "").lower()  # "light"/"dark", for screenshots
    if forced in ("light", "dark"):
        return forced == "dark"
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def dark_title_bar(root: tk.Tk, dark: bool) -> None:
    """Match the Windows title bar to the theme (Windows 10 20H1+ / 11; ignored elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def scale_of(root: tk.Misc) -> float:
    """Screen scaling relative to 96 dpi, for sizes given in 'design pixels'."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


# --- antialiased rounded rectangles (Tk's canvas cannot smooth curves) -------------------------------------


def focus_ring(widget: tk.Widget, colour: str, normal) -> None:
    """Show a keyboard-focus ring on a Label. Tk on Windows draws a label's highlight with
    highlightbackground even when focused, so recolour that on focus changes. ``normal`` gives the
    colour to restore (a callable, as it can depend on state)."""
    widget.bind("<FocusIn>", lambda e: widget.configure(highlightbackground=colour), add="+")
    widget.bind("<FocusOut>", lambda e: widget.configure(highlightbackground=normal()), add="+")


def _rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def rounded_rect_png(
    w: int, h: int, r: float, fill: str, bg: str, ring: str | None = None, ring_inset: float = 3.0
) -> bytes:
    """A filled rounded rectangle on ``bg`` as PNG bytes, edges antialiased via a signed distance
    field. ``ring`` adds a thin inner outline (keyboard focus)."""
    f, b = _rgb(fill), _rgb(bg)
    ring_rgb = _rgb(ring) if ring else None
    half_w, half_h = w / 2, h / 2
    top: list[bytes] = []
    for y in range((h + 1) // 2):
        qy = abs(y + 0.5 - half_h) - (half_h - r)
        row = bytearray([0])  # PNG filter: none
        for x in range(w):
            qx = abs(x + 0.5 - half_w) - (half_w - r)
            d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r  # < 0 inside
            colour = f
            if ring_rgb is not None:
                cover = min(max(0.5 - (abs(d + ring_inset) - 0.75), 0.0), 1.0)
                colour = _mix(f, ring_rgb, cover * 0.85)
            row += bytes(_mix(b, colour, min(max(0.5 - d, 0.0), 1.0)))
        top.append(bytes(row))
    rows = top + top[: h // 2][::-1]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    body = zlib.compress(b"".join(rows), 6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b"")


class RoundButton(tk.Canvas):
    """A filled, rounded button: hover, pressed, disabled and keyboard-focus states. Space or Enter
    activate it when it has the focus; ``guard`` can veto an activation (e.g. right after a dialog)."""

    def __init__(
        self, parent, *, text, command, palette: Palette, scale: float, width: int, height: int, guard=None
    ):
        self.p, self.command, self.guard = palette, command, guard
        self.w, self.h, self.r = int(width * scale), int(height * scale), 10 * scale
        super().__init__(parent, width=self.w, height=self.h, bg=palette.bg, highlightthickness=0, bd=0)
        self.configure(takefocus=1, cursor="hand2")
        self.enabled = True
        self._hover = self._pressed = self._focused = False
        self._images: dict[tuple[str, bool], tk.PhotoImage] = {}
        self._bg = self.create_image(0, 0, anchor="nw", image=self._image(palette.accent, False))
        self._label = self.create_text(
            self.w / 2, self.h / 2, text=text, fill=palette.on_accent, font=(FONT, 13, "bold")
        )
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focused=True))
        self.bind("<FocusOut>", lambda e: self._set(focused=False))
        # Act on key release, and only for a press that started here: a key held down or pressed
        # while a dialog was open must not start (or confirm) anything.
        self._key_down = False
        for key in ("space", "Return"):
            self.bind(f"<KeyPress-{key}>", self._key_press)
            self.bind(f"<KeyRelease-{key}>", self._key_release)

    def _image(self, fill: str, focused: bool) -> tk.PhotoImage:
        key = (fill, focused)
        if key not in self._images:
            png = rounded_rect_png(
                self.w, self.h, self.r, fill, self.p.bg, ring=self.p.on_accent if focused else None
            )
            self._images[key] = tk.PhotoImage(master=self, data=base64.b64encode(png))
        return self._images[key]

    def _set(self, *, hover=None, pressed=None, focused=None) -> None:
        if hover is not None:
            self._hover = hover
        if pressed is not None:
            self._pressed = pressed
        if focused is not None:
            self._focused = focused
        p = self.p
        if not self.enabled:
            fill = p.accent_disabled
        elif self._pressed:
            fill = p.accent_pressed
        elif self._hover:
            fill = p.accent_hover
        else:
            fill = p.accent
        self.itemconfigure(self._bg, image=self._image(fill, self._focused and self.enabled))

    def _release(self, event) -> None:
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._set(pressed=False)
        if inside:
            self._invoke()

    def _key_press(self, event) -> None:
        self._key_down = self._key_down or self.enabled

    def _key_release(self, event) -> None:
        if self._key_down:
            self._key_down = False
            self._invoke()

    def _invoke(self) -> None:
        if self.enabled and (self.guard is None or self.guard()):
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._set()

    def set_text(self, text: str) -> None:
        self.itemconfigure(self._label, text=text)

    @property
    def text(self) -> str:
        return self.itemcget(self._label, "text")


class Segmented(tk.Frame):
    """A row of mutually exclusive options, like a toggle group. Click, or Tab to it and press Space."""

    def __init__(
        self, parent, *, options: list[str], value: str, command, palette: Palette, scale: float = 1.0
    ):
        super().__init__(parent, bg=palette.border, padx=1, pady=1)
        self.p, self.command = palette, command
        self.value = value
        self.enabled = True
        self._labels: dict[str, tk.Label] = {}
        for i, option in enumerate(options):
            lbl = tk.Label(
                self,
                text=option,
                padx=int(14 * scale),
                pady=int(3 * scale),
                cursor="hand2",
                takefocus=1,
                highlightthickness=2,
                highlightcolor=palette.text,  # contrasts with both the accent and the background
            )
            lbl.grid(row=0, column=i, padx=(0 if i == 0 else 1, 0))
            lbl.bind("<Button-1>", lambda e, o=option: self.select(o, notify=True))
            lbl.bind("<space>", lambda e, o=option: self.select(o, notify=True))
            focus_ring(lbl, palette.text, lambda o=option: self._bg(o))
            self._labels[option] = lbl
        self._paint()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        for lbl in self._labels.values():
            lbl.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def select(self, option: str, *, notify: bool = False) -> None:
        if not self.enabled:
            return
        changed = option != self.value
        self.value = option
        self._paint()
        if notify and changed:
            self.command(option)

    def _bg(self, option: str) -> str:
        if option != self.value:
            return self.p.bg
        return self.p.accent if self.enabled else self.p.accent_disabled

    def _paint(self) -> None:
        focused = self.focus_get()
        for option, lbl in self._labels.items():
            on = option == self.value
            bg = self._bg(option)
            lbl.configure(
                bg=bg,
                highlightbackground=self.p.text if lbl is focused else bg,
                fg=self.p.on_accent if on else (self.p.text if self.enabled else self.p.muted),
                font=(FONT, 9, "bold" if on else "normal"),
            )
