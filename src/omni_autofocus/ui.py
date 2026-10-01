"""Look and feel of the app window: a light/dark palette that follows Windows, an antialiased
rounded button and a segmented control. Plain tkinter, no extra packages."""

from __future__ import annotations

import base64
import ctypes
import functools
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


def _box_sdf(px: float, py: float, half_w: float, half_h: float, r: float) -> float:
    """Signed distance from a pixel centre to a rounded rectangle centred on (half_w, half_h); < 0 inside."""
    qx = abs(px - half_w) - (half_w - r)
    qy = abs(py - half_h) - (half_h - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _cover(d: float) -> float:
    """Pixel coverage for a signed distance (one-pixel antialiasing ramp)."""
    return min(max(0.5 - d, 0.0), 1.0)


def _png(w: int, h: int, pixel, *, mirror: bool = False) -> bytes:
    """PNG bytes for a w×h RGB image; ``pixel(x, y)`` gives each pixel's (r, g, b). ``mirror``: the
    image is symmetric top to bottom, so only the top half is computed."""
    rows: list[bytes] = []
    for y in range((h + 1) // 2 if mirror else h):
        row = bytearray([0])  # PNG filter: none
        for x in range(w):
            row += bytes(pixel(x, y))
        rows.append(bytes(row))
    if mirror:
        rows += rows[: h // 2][::-1]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    body = zlib.compress(b"".join(rows), 6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b"")


@functools.lru_cache(maxsize=64)  # buttons of one size share their images
def rounded_rect_png(
    w: int, h: int, r: float, fill: str, bg: str, ring: str | None = None, ring_inset: float = 3.0
) -> bytes:
    """A filled rounded rectangle on ``bg`` as PNG bytes, edges antialiased via a signed distance
    field. ``ring`` adds a thin inner outline (keyboard focus)."""
    f, b = _rgb(fill), _rgb(bg)
    ring_rgb = _rgb(ring) if ring else None
    solid = -(ring_inset + 2.0) if ring_rgb else -1.0  # further inside than this: plain fill

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        d = _box_sdf(x + 0.5, y + 0.5, w / 2, h / 2, r)
        if d <= solid:
            return f
        colour = f
        if ring_rgb is not None:
            colour = _mix(f, ring_rgb, _cover(abs(d + ring_inset) - 0.75) * 0.85)
        return _mix(b, colour, _cover(d))

    return _png(w, h, pixel, mirror=True)


def _segment_distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = min(max(((px - a[0]) * dx + (py - a[1]) * dy) / (dx * dx + dy * dy), 0.0), 1.0)
    return math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)


@functools.lru_cache(maxsize=16)
def checkbox_png(size: int, checked: bool, palette: Palette, *, border: float = 1.5) -> bytes:
    """A check box drawn at the screen's scale: an outlined rounded square, or an accent-filled one with
    a tick. Tk's own check box stays 13 px however large the text is."""
    p = palette
    bg, edge, inner = _rgb(p.bg), _rgb(p.muted), _rgb(p.accent if checked else p.bg)
    mark = _rgb(p.on_accent)
    half, r = size / 2, size * 0.2
    tick = ((0.26 * size, 0.52 * size), (0.43 * size, 0.69 * size), (0.75 * size, 0.33 * size))
    stroke = max(1.5, size * 0.12)

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        px, py = x + 0.5, y + 0.5
        d = _box_sdf(px, py, half, half, r)
        if checked:
            dt = min(_segment_distance(px, py, tick[0], tick[1]), _segment_distance(px, py, tick[1], tick[2]))
            colour = _mix(inner, mark, _cover(dt - stroke / 2))
        else:
            colour = _mix(edge, inner, _cover(d + border))
        return _mix(bg, colour, _cover(d))

    return _png(size, size, pixel)


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
    """A row of mutually exclusive options, like a toggle group. Click, or Tab to it and press Space.
    ``stretch`` shares the width it is given out equally (pack it with fill="x")."""

    def __init__(
        self,
        parent,
        *,
        options: list[str],
        value: str,
        command,
        palette: Palette,
        scale: float = 1.0,
        pad: int = 14,
        labels: dict[str, str] | None = None,
        stretch: bool = False,
    ):
        super().__init__(parent, bg=palette.border, padx=1, pady=1)
        self.p, self.command = palette, command
        self.value = value
        self.enabled = True
        self._labels: dict[str, tk.Label] = {}
        for i, option in enumerate(options):
            lbl = tk.Label(
                self,
                text=(labels or {}).get(option, option),
                padx=int(pad * scale),
                pady=int(3 * scale),
                cursor="hand2",
                takefocus=1,
                highlightthickness=2,
                highlightcolor=palette.text,  # contrasts with both the accent and the background
            )
            lbl.grid(row=0, column=i, padx=(0 if i == 0 else 1, 0), sticky="ew")
            if stretch:
                self.columnconfigure(i, weight=1, uniform="option")
            lbl.bind("<Button-1>", lambda e, o=option: self.select(o, notify=True))
            lbl.bind("<space>", lambda e, o=option: self.select(o, notify=True))
            focus_ring(lbl, palette.text, lambda o=option: self._bg(o))
            self._labels[option] = lbl
        self._paint()

    def relabel(self, labels: dict[str, str]) -> None:
        """Change what the options show; their values stay the same."""
        for option, text in labels.items():
            if option in self._labels:
                self._labels[option].configure(text=text)

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


class Check(tk.Frame):
    """A check box with its label, drawn at the screen's scale and in the theme's colours. Click it
    (box or text), or Tab to it and press Space. Bound to a ``tk.BooleanVar``."""

    def __init__(self, parent, *, text: str, variable: tk.BooleanVar, palette: Palette, scale: float, font):
        # The 1 px padding keeps the focus ring off the box and puts the box where a Label's text starts.
        super().__init__(parent, bg=palette.bg, takefocus=1, cursor="hand2", highlightthickness=2, bd=0)
        self.configure(highlightbackground=palette.bg, highlightcolor=palette.text, padx=1, pady=1)
        self.p, self.variable = palette, variable
        size = round(14 * scale)
        self._images = {
            on: tk.PhotoImage(master=self, data=base64.b64encode(checkbox_png(size, on, palette)))
            for on in (False, True)
        }
        self.box = tk.Label(self, bg=palette.bg, bd=0, padx=0, pady=0, cursor="hand2")
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, bg=palette.bg, fg=palette.text, font=font, cursor="hand2")
        self.label.configure(padx=0, pady=0)
        gap = round(7 * scale)
        self.label.pack(side="left", padx=(gap, round(2 * scale)))
        # Where the text starts, from the widget's left edge (for notes lined up beneath it).
        self.text_x = (
            2 + 1 + size + gap + self.label.winfo_pixels(self.label.cget("bd"))
        )  # Tcl_Obj on older Pythons
        self._pressed = False
        for w in (self, self.box, self.label):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<ButtonRelease-1>", self._click)
        # Space acts on release, and only for a press made here (holding it down must not flicker it).
        self._key_down = False
        self.bind("<KeyPress-space>", self._key_press)
        self.bind("<KeyRelease-space>", self._key_release)
        focus_ring(self, palette.text, lambda: palette.bg)
        self.bind("<FocusOut>", lambda e: setattr(self, "_key_down", False), add="+")
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", lambda e: self._untrace() if e.widget is self else None)
        self._draw()

    def _press(self, event) -> None:
        self._pressed = True

    def _click(self, event) -> None:
        """Toggle on release over the check box after a press on it (like a native one: dragging off
        cancels). A click does not take the keyboard focus, like the app's other click targets."""
        pressed, self._pressed = self._pressed, False
        if pressed and self.winfo_containing(event.x_root, event.y_root) in (self, self.box, self.label):
            self._toggle()

    def _key_press(self, event) -> str:
        self._key_down = True
        return "break"

    def _key_release(self, event) -> str:
        if self._key_down:
            self._key_down = False
            self._toggle()
        return "break"

    def _toggle(self, event=None) -> str:
        self.variable.set(not self.variable.get())
        return "break"

    def _draw(self) -> None:
        self.box.configure(image=self._images[bool(self.variable.get())])

    def _untrace(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace)
        except tk.TclError:
            pass
