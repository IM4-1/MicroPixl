#!/usr/bin/env python3
"""
MicroPixl - a lightweight, 100% local desktop utility (no AI models, no cloud).

  1. OCR text extraction   (Tesseract via pytesseract)
  2. Raster -> SVG         (vtracer)
  3. App / PWA icon resizer (Pillow)

GUI: built to the Bareeed Design System (light + dark tokens, flat surfaces,
1px structural borders, 0-4px radii, strict 8pt spacing, pentagon indicators,
angular single-stroke icons, no shadows, no animation).
Layout: 72px icon rail | 360px queue column | fluid tool pane (content <= 720px).

Backend guarantees (unchanged from v1)
--------------------------------------
* One background thread per job; the UI only polls a queue.
* One job at a time, one file at a time, images closed + gc.collect() per file.
* Large images are downscaled before OCR / vectorising / icon generation.
* The queue column is paginated (user-initiated "Show more") so a huge batch
  never creates thousands of widgets.

Requirements
------------
    pip install customtkinter pillow pytesseract vtracer
    + the Tesseract OCR engine (separate program)

Fonts (Offline Sovereignty): drop static IBM Plex Sans / IBM Plex Sans Arabic
.ttf files into a `fonts/` folder next to this file. They are loaded locally;
nothing is ever fetched from the network. Without them the OS default is used.
"""

from __future__ import annotations

import gc
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
import weakref
from functools import lru_cache
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
    from PIL import Image, ImageOps
except ImportError as exc:  # pragma: no cover
    print(f"Missing dependency: {exc}\nInstall with: pip install customtkinter pillow pytesseract vtracer")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Backend constants (unchanged)
# --------------------------------------------------------------------------- #
APP_NAME = "MicroPixl"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
FILETYPES = [("Images", " ".join(f"*{e}" for e in IMAGE_EXTS)), ("All files", "*.*")]

MAX_OCR_SIDE = 3500
MIN_OCR_SIDE = 1000
MAX_SVG_SIDE = 2000
MAX_ICON_SOURCE = 4096

PRESET_ICON_SIZES = [16, 32, 48, 64, 72, 96, 128, 144, 152, 167, 180, 192, 256, 300, 384, 512, 1024]
DEFAULT_CHECKED = {48, 72, 96, 128, 144, 152, 167, 180, 192, 256, 384, 512}

SVG_PRESETS = {
    "Color - detailed (photos / illustrations)": dict(
        colormode="color", hierarchical="stacked", mode="spline", filter_speckle=4,
        color_precision=6, layer_difference=16, corner_threshold=60, length_threshold=4.0,
        max_iterations=10, splice_threshold=45, path_precision=3),
    "Color - simple (logos / flat art)": dict(
        colormode="color", hierarchical="stacked", mode="spline", filter_speckle=8,
        color_precision=4, layer_difference=32, corner_threshold=60, length_threshold=4.0,
        max_iterations=10, splice_threshold=45, path_precision=2),
    "Black & white (line art / text / scans)": dict(
        colormode="binary", hierarchical="stacked", mode="spline", filter_speckle=4,
        color_precision=6, layer_difference=16, corner_threshold=60, length_threshold=4.0,
        max_iterations=10, splice_threshold=45, path_precision=3),
}

PAGE_SIZE = 20  # queue rows rendered per page (user-initiated "Show more")

# --------------------------------------------------------------------------- #
# Design tokens (Bareeed Design System) - every colour is a (light, dark) pair
# --------------------------------------------------------------------------- #
def _rgb(h: str) -> tuple[int, ...]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a: str, b: str, t: float) -> str:
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def _mixp(a: tuple[str, str], b: tuple[str, str], t: float) -> tuple[str, str]:
    return (_mix(a[0], b[0], t), _mix(a[1], b[1], t))


SURFACE = ("#F1EDE4", "#181B1F")   # color.surface.base
TEXT = ("#2A2D2D", "#F1F0E9")      # color.text.primary
TEXT2 = ("#4A4F4E", "#9D9D9B")     # color.text.secondary
MUTED = ("#D1B9A7", "#4F5450")     # color.text.muted (indicator outlines only - too faint for text)
BORDER = ("#3A3F3F", "#4F5450")    # color.border.default
SUBTLE = ("#4A4F4E", "#4F5450")    # color.border.subtle (dark mode defines none; reuses default)
ACCENT = ("#CC7245", "#C96939")    # color.accent.primary
STRONG = ("#C96939", "#9C5532")    # color.accent.strong
SOFT = ("#D38A63", "#643C29")      # color.accent.soft (light) / accent.muted (dark) - fills only
ERROR = (_mix("#C96939", "#000000", 0.10), _mix("#9C5532", "#000000", 0.10))  # status.error
ON_ACCENT = ("#2A2D2D", "#181B1F")  # dark text on accent fills (best available contrast)
HOVER = _mixp(SURFACE, TEXT, 0.04)    # row hover: 4% toward text.primary
SELECT = _mixp(SURFACE, ACCENT, 0.08)  # row selected: 8% toward accent.primary
HOVER_ACCENT = _mixp(ACCENT, TEXT, 0.08)
TRACK = _mixp(SURFACE, TEXT, 0.12)

# Spacing (strict 8pt) and type scale (major second)
S1, S2, S3, S4, S5, S6 = 4, 8, 16, 24, 32, 48
XS, SM, BASE, MD, LG, XL = 12, 14, 16, 18, 22, 28

RADIUS_CHIP = 2  # all other components use 0

STATE = {"mode": 0}  # 0 = light, 1 = dark
THEMED: "weakref.WeakSet" = weakref.WeakSet()  # canvases that must redraw on theme change


def cur(pair: tuple[str, str]) -> str:
    """Resolve a (light, dark) pair to the active mode's hex colour."""
    return pair[STATE["mode"]]


# --------------------------------------------------------------------------- #
# Fonts (local only; IBM Plex Sans Arabic -> IBM Plex Sans -> OS default)
# --------------------------------------------------------------------------- #
_FAMILY = "TkDefaultFont"
FONT_CANDIDATES = ("IBM Plex Sans Arabic", "IBM Plex Sans")


def init_fonts(root: tk.Misc) -> None:
    """Load bundled fonts from ./fonts (no network) and pick the best family."""
    global _FAMILY
    folder = Path(__file__).resolve().parent / "fonts"
    loader = getattr(ctk, "FontManager", None)
    if folder.is_dir() and loader is not None:
        for f in sorted(list(folder.glob("*.ttf")) + list(folder.glob("*.otf"))):
            try:
                loader.load_font(str(f))
            except Exception:
                pass
    families = set(tkfont.families(root))
    for name in FONT_CANDIDATES:
        if name in families:
            _FAMILY = name
            return
    _FAMILY = tkfont.nametofont("TkDefaultFont").actual("family")


@lru_cache(maxsize=None)
def F(size: int, bold: bool = False) -> "ctk.CTkFont":
    """Cached font. Tk only has normal/bold, so 500/600 weights map to bold."""
    return ctk.CTkFont(family=_FAMILY, size=size, weight="bold" if bold else "normal")


def ui_scale(widget: tk.Misc) -> float:
    """Scale factor CustomTkinter applies to its own widgets (for raw canvases)."""
    try:
        from customtkinter.windows.widgets.scaling import ScalingTracker
        return float(ScalingTracker.get_widget_scaling(widget))
    except Exception:
        return 1.0


# --------------------------------------------------------------------------- #
# Small helpers (backend + formatting)
# --------------------------------------------------------------------------- #
def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def flatten_to_gray(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, "white")
        bg.alpha_composite(rgba)
        rgba.close()
        im = bg
    return im.convert("L")


def parse_sizes(text: str) -> list[int]:
    sizes: set[int] = set()
    for token in text.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        token = token.split("x")[0]
        if not token.isdigit():
            raise ValueError(f"Invalid size: '{token}'")
        value = int(token)
        if not 1 <= value <= 4096:
            raise ValueError(f"Size out of range (1-4096): {value}")
        sizes.add(value)
    return sorted(sizes)


def short(text: str, limit: int = 30) -> str:
    return text if len(text) <= limit else text[: limit - 9] + "…" + text[-8:]


def fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------- #
# Background job plumbing (thread -> UI through a queue) - UNCHANGED logic,
# plus file_status() so the queue column can show per-file state.
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, q: queue.Queue):
        self._q = q
        self.cancel = threading.Event()

    def log(self, message: str) -> None:
        self._q.put(("log", message))

    def progress(self, fraction: float) -> None:
        self._q.put(("progress", max(0.0, min(1.0, fraction))))

    def result_text(self, text: str) -> None:
        self._q.put(("text", text))

    def file_status(self, index: int, state: str) -> None:
        self._q.put(("file", (index, state)))  # state: working | done | failed


# --------------------------------------------------------------------------- #
# Themed primitives: canvases (icons, pentagon), flat buttons, chips, fields
# --------------------------------------------------------------------------- #
class ThemedCanvas(tk.Canvas):
    """Raw canvas that knows its (light, dark) background and redraws on theme change."""

    def __init__(self, master, w: int, h: int, bg_pair=SURFACE, **kw):
        self.sc = ui_scale(master)
        super().__init__(master, width=int(w * self.sc), height=int(h * self.sc),
                         highlightthickness=0, bd=0, bg=cur(bg_pair), **kw)
        self.bg_pair = bg_pair
        THEMED.add(self)

    def retheme(self) -> None:
        self.configure(bg=cur(self.bg_pair))
        self.redraw()

    def redraw(self) -> None: ...


class PentagonDot(ThemedCanvas):
    """Pentagonal indicator (Rule 8): filled or outline, never circular."""

    def __init__(self, master, size: int, bg_pair=SURFACE):
        super().__init__(master, size, size, bg_pair)
        self.size = size
        self.color, self.filled = ACCENT, True
        self.redraw()

    def set(self, color, filled: bool, bg_pair=None) -> None:
        self.color, self.filled = color, filled
        if bg_pair is not None:
            self.bg_pair = bg_pair
            self.configure(bg=cur(bg_pair))
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        cx = cy = self.size * self.sc / 2
        r = cx - 1.5 * self.sc
        pts: list[float] = []
        for k in range(5):
            a = math.radians(-90 + 72 * k)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a) + r * 0.095]
        col = cur(self.color)
        self.create_polygon(*pts, fill=col if self.filled else "", outline=col,
                            width=max(1.0, 1.5 * self.sc), joinstyle="miter")


# Angular, single-weight stroke icons on a 24px grid (45 / 90 degree joins only).
ICONS = {
    "ocr": [("line", [(5, 3), (15, 3), (19, 7), (19, 21), (5, 21), (5, 3)]),
            ("line", [(15, 3), (15, 7), (19, 7)]),
            ("line", [(8, 11), (16, 11)]), ("line", [(8, 14), (16, 14)]), ("line", [(8, 17), (13, 17)])],
    "svg": [("line", [(4, 19), (4, 11), (10, 5), (20, 5)]),
            ("line", [(2, 17), (6, 17), (6, 21), (2, 21), (2, 17)]),
            ("line", [(18, 3), (22, 3), (22, 7), (18, 7), (18, 3)])],
    "icons": [("line", [(4, 4), (20, 4), (20, 20), (4, 20), (4, 4)]),
              ("line", [(4, 12), (12, 12), (12, 20)]),
              ("line", [(12, 12), (20, 4)])],
    "theme": [("line", [(6, 6), (18, 6), (18, 18), (6, 18), (6, 6)]),
              ("fill", [(6, 6), (18, 18), (6, 18)])],
}


def draw_icon(cv: tk.Canvas, name: str, ox: float, oy: float, s: float, color: str) -> None:
    for kind, pts in ICONS[name]:
        flat = [v for x, y in pts for v in (ox + x * s, oy + y * s)]
        if kind == "line":
            cv.create_line(*flat, fill=color, width=1.5 * s, capstyle="butt", joinstyle="miter")
        else:
            cv.create_polygon(*flat, fill=color, outline=color)


class RailButton(ThemedCanvas):
    """72x64 icon-only rail item: hover tint, selected = tint + 2px accent bar, 2px focus ring."""
    W, H = 72, 64

    def __init__(self, master, icon: str, command, selectable: bool = True):
        super().__init__(master, self.W, self.H, SURFACE, takefocus=1)
        self.icon, self.command, self.selectable = icon, command, selectable
        self.selected = self.hover = self.focused = False
        self.bind("<Button-1>", lambda e: (self.focus_set(), self.command()))
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<FocusIn>", lambda e: self._set(focused=True))
        self.bind("<FocusOut>", lambda e: self._set(focused=False))
        self.redraw()

    def _set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.redraw()

    def set_selected(self, value: bool) -> None:
        self._set(selected=value)

    def redraw(self) -> None:
        self.delete("all")
        s = self.sc
        bg = SELECT if self.selected else (HOVER if self.hover else SURFACE)
        self.configure(bg=cur(bg))
        w, h = self.W * s, self.H * s
        if self.selected:
            self.create_rectangle(0, 0, 2 * s, h, fill=cur(ACCENT), outline="")
        draw_icon(self, self.icon, (w - 24 * s) / 2, (h - 24 * s) / 2, s,
                  cur(TEXT if self.selected else TEXT2))
        if self.focused:
            self.create_rectangle(S1 * s, S1 * s, w - S1 * s, h - S1 * s,
                                  outline=cur(ACCENT), width=2 * s)


class FlatButton(ctk.CTkButton):
    """Square, flat, >=40px-high button. kinds: primary | secondary | bar | chip."""

    def __init__(self, master, text: str, command=None, kind: str = "secondary", width: int = 120):
        radius = RADIUS_CHIP if kind == "chip" else 0
        if kind == "primary":
            fg, hov, txt, bc, bw = ACCENT, HOVER_ACCENT, ON_ACCENT, ACCENT, 1
        elif kind == "bar":
            fg, hov, txt, bc, bw = SURFACE, HOVER, TEXT, SURFACE, 0
        else:
            fg, hov, txt, bc, bw = SURFACE, HOVER, TEXT, BORDER, 1
        super().__init__(master, text=text, command=command, width=width, height=40,
                         corner_radius=radius, border_width=bw, fg_color=fg, hover_color=hov,
                         text_color=txt, text_color_disabled=TEXT2, border_color=bc,
                         font=F(SM, kind == "primary"))
        self._kind, self._rest = kind, (bw, bc)
        try:  # keyboard focus + 2px square focus ring (no glow)
            cv = self._canvas
            cv.configure(takefocus=1)
            cv.bind("<FocusIn>", self._focus_in, add="+")
            cv.bind("<FocusOut>", self._focus_out, add="+")
            cv.bind("<Button-1>", lambda e: cv.focus_set(), add="+")
            cv.bind("<Return>", lambda e: self._press(), add="+")
            cv.bind("<space>", lambda e: self._press(), add="+")
        except AttributeError:
            pass

    def _press(self) -> None:
        if self.cget("state") != "disabled":
            self.invoke()

    def _focus_in(self, _e) -> None:
        if self.cget("state") != "disabled":
            self.configure(border_width=2, border_color=TEXT if self._kind == "primary" else ACCENT)

    def _focus_out(self, _e) -> None:
        self.configure(border_width=self._rest[0], border_color=self._rest[1])


class Chip(FlatButton):
    """Toggle chip: rectangular, 1px border; active = filled accent.soft with text.primary."""

    def __init__(self, master, text: str, active: bool = False, width: int = 64):
        super().__init__(master, text, kind="chip", width=width)
        self.active = active
        self.configure(command=self.toggle)
        self._apply()

    def toggle(self) -> None:
        self.active = not self.active
        self._apply()

    def _apply(self) -> None:
        self.configure(fg_color=SOFT if self.active else SURFACE,
                       hover_color=SOFT if self.active else HOVER, text_color=TEXT)


def lbl(parent, text: str, size: int = SM, color=TEXT, bold: bool = False, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(parent, text=text, font=F(size, bold), text_color=color,
                        fg_color="transparent", anchor="w", justify="left", **kw)


def rule(parent, color=BORDER) -> ctk.CTkFrame:
    """1px structural border (horizontal or vertical: stretch it with sticky / fill)."""
    return ctk.CTkFrame(parent, width=1, height=1, corner_radius=0, fg_color=color)


def make_entry(parent, width: int = 200, placeholder: str = "") -> ctk.CTkEntry:
    return ctk.CTkEntry(parent, width=width, height=40, corner_radius=0, border_width=1,
                        border_color=BORDER, fg_color=SURFACE, text_color=TEXT,
                        placeholder_text=placeholder, placeholder_text_color=TEXT2, font=F(SM))


def make_menu(parent, values: list[str], width: int = 360):
    """Option menu wrapped in a 1px border box. Returns (box_to_grid, menu)."""
    box = ctk.CTkFrame(parent, corner_radius=0, border_width=1, border_color=BORDER, fg_color=SURFACE)
    menu = ctk.CTkOptionMenu(box, values=values, width=width - 2, height=38, corner_radius=0,
                             fg_color=SURFACE, button_color=SURFACE, button_hover_color=HOVER,
                             text_color=TEXT, dropdown_fg_color=SURFACE, dropdown_hover_color=HOVER,
                             dropdown_text_color=TEXT, font=F(SM), dropdown_font=F(SM), anchor="w")
    menu.pack(padx=1, pady=1)
    return box, menu


def action_bar(parent, specs: list[tuple[str, object]], expand: bool = False):
    """Unified 1px-bordered bar of >=40px buttons separated by 1px rules (Rule 13)."""
    bar = ctk.CTkFrame(parent, corner_radius=0, border_width=1, border_color=BORDER, fg_color=SURFACE)
    buttons = []
    for i, (text, cmd) in enumerate(specs):
        if i:
            rule(bar).pack(side="left", fill="y", pady=1)
        b = FlatButton(bar, text, cmd, kind="bar", width=96)
        b.pack(side="left", padx=(1 if i == 0 else 0, 1 if i == len(specs) - 1 else 0), pady=1,
               fill="x" if expand else None, expand=expand)
        buttons.append(b)
    return bar, buttons


def make_textbox(parent, height: int) -> ctk.CTkTextbox:
    box = ctk.CTkTextbox(parent, height=height, corner_radius=0, border_width=1, border_color=BORDER,
                         fg_color=SURFACE, text_color=TEXT, font=F(SM), wrap="word", state="disabled",
                         scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT2)
    try:
        box._y_scrollbar.configure(corner_radius=0)
    except Exception:
        pass
    return box


def set_text(box: ctk.CTkTextbox, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


def append_text(box: ctk.CTkTextbox, text: str) -> None:
    box.configure(state="normal")
    box.insert("end", text)
    box.see("end")
    box.configure(state="disabled")


# --------------------------------------------------------------------------- #
# Queue row = "AI triage inbox item" (64px, 16px padding, 1px bottom border)
# --------------------------------------------------------------------------- #
STATE_LABEL = {"queued": "Queued", "working": "Working", "done": "Done", "failed": "Failed"}


class FileRow(ctk.CTkFrame):
    """queued = unread (filled pentagon) | working = prioritized (2px strong bar) |
    done = read (outline) | failed = error outline. Hover/selected per spec 4.1."""

    def __init__(self, master, page: "ToolPage", index: int):
        super().__init__(master, height=65, corner_radius=0, fg_color=SURFACE)
        self.grid_propagate(False)
        self.page, self.index, self.hover = page, index, False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.body = ctk.CTkFrame(self, height=64, corner_radius=0, fg_color=SURFACE)
        self.body.grid(row=0, column=0, sticky="nsew")
        self.sep = rule(self, SUBTLE)
        self.sep.grid(row=1, column=0, sticky="ew")

        b = self.body
        b.grid_columnconfigure(2, weight=1)
        b.grid_rowconfigure(0, weight=1)
        self.bar = ctk.CTkFrame(b, width=2, corner_radius=0, fg_color=SURFACE)
        self.bar.grid(row=0, column=0, sticky="ns")
        self.dot = PentagonDot(b, 16)
        self.dot.grid(row=0, column=1, padx=(S3, S2))
        self.txt = ctk.CTkFrame(b, corner_radius=0, fg_color=SURFACE)
        self.txt.grid(row=0, column=2, sticky="w")
        path = page.files[index]
        self.name = lbl(self.txt, short(path.name), SM)
        self.name.pack(anchor="w")
        self.meta = lbl(self.txt, page.meta_for(index), XS, TEXT2)
        self.meta.pack(anchor="w")
        self.chip = ctk.CTkFrame(b, corner_radius=RADIUS_CHIP, border_width=1, border_color=SUBTLE, fg_color=SURFACE)
        self.chip.grid(row=0, column=3, padx=(S2, S3))
        self.chip_lbl = lbl(self.chip, "", XS, TEXT)
        self.chip_lbl.pack(padx=S2, pady=S1)

        for w in (self, self.body, self.bar, self.dot, self.txt, self.name, self.meta, self.chip, self.chip_lbl):
            w.bind("<Button-1>", lambda e: self.page.select(self.index), add="+")
            w.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")
        self.body.bind("<Leave>", self._leave, add="+")
        self.paint()

    def _enter(self, _e) -> None:
        if not self.hover:
            self.hover = True
            self.paint()

    def _leave(self, e) -> None:
        under = self.winfo_containing(e.x_root, e.y_root)
        while under is not None:  # ignore moves between our own children
            if under is self:
                return
            under = getattr(under, "master", None)
        self.hover = False
        self.paint()

    def paint(self) -> None:
        p = self.page
        st = p.states[self.index]
        sel = p.selected == self.index
        bg = SELECT if sel else (HOVER if self.hover else SURFACE)
        for w in (self.body, self.txt, self.chip):
            w.configure(fg_color=bg)
        for w in (self.name, self.meta, self.chip_lbl):
            w.configure(fg_color=bg)
        self.bar.configure(fg_color=ACCENT if sel else (STRONG if st == "working" else bg))
        self.sep.configure(fg_color=BORDER if self.hover else SUBTLE)
        if st == "queued":
            self.dot.set(ACCENT, True, bg)
        elif st == "working":
            self.dot.set(STRONG, True, bg)
        elif st == "done":
            self.dot.set(MUTED, False, bg)
        else:
            self.dot.set(ERROR, False, bg)
        self.name.configure(font=F(SM, st in ("queued", "working")))
        self.chip_lbl.configure(text=STATE_LABEL[st])
        self.chip.configure(border_color=ERROR if st == "failed" else SUBTLE)


# --------------------------------------------------------------------------- #
# Tool page: [360px queue column] | [fluid tool pane, content <= 720px]
# --------------------------------------------------------------------------- #
class ToolPage(ctk.CTkFrame):
    TITLE = ""
    SUBTITLE = ""
    needs_output_dir = False
    OUTPUT_HEIGHT = 240
    LIST_W = 360
    CONTENT_MAX = 720

    def __init__(self, master, app: "App"):
        super().__init__(master, corner_radius=0, fg_color=SURFACE)
        self.app = app
        self.files: list[Path] = []
        self.states: list[str] = []
        self.selected: int | None = None
        self.visible = PAGE_SIZE
        self.rows: list[FileRow] = []
        self.out_dir: Path | None = None
        self._meta_cache: dict[Path, str] = {}
        self._last_extra = -1

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(2, weight=1, minsize=480)
        self._build_list_column()
        rule(self).grid(row=0, column=1, sticky="ns")
        self.grid_columnconfigure(1, minsize=1)
        self._build_reader()
        self._refresh()

    # ---- queue column -----------------------------------------------------
    def _build_list_column(self) -> None:
        col = ctk.CTkFrame(self, width=self.LIST_W, corner_radius=0, fg_color=SURFACE)
        col.grid(row=0, column=0, sticky="ns")
        col.grid_propagate(False)
        col.grid_columnconfigure(0, weight=1)
        col.grid_rowconfigure(4, weight=1)

        head = ctk.CTkFrame(col, height=48, corner_radius=0, fg_color=SURFACE)  # sovereign header
        head.grid(row=0, column=0, sticky="ew")
        head.grid_propagate(False)
        PentagonDot(head, 8).place(x=S3, rely=0.5, anchor="w")
        lbl(head, APP_NAME, MD, bold=True).place(x=S3 + 8 + S2, rely=0.5, anchor="w")
        rule(col).grid(row=1, column=0, sticky="ew")

        holder = ctk.CTkFrame(col, corner_radius=0, fg_color=SURFACE)
        holder.grid(row=2, column=0, sticky="ew", padx=S3, pady=S2)
        bar, (self.add_btn, self.remove_btn, self.clear_btn) = action_bar(
            holder, [("Add images", self.add_files), ("Remove", self.remove_selected),
                     ("Clear", self.clear_files)], expand=True)
        bar.pack(fill="x")
        rule(col).grid(row=3, column=0, sticky="ew")

        self.list_area = ctk.CTkScrollableFrame(
            col, corner_radius=0, fg_color=SURFACE, scrollbar_fg_color=SURFACE,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT2)
        try:
            self.list_area._scrollbar.configure(corner_radius=0)
        except Exception:
            pass
        self.list_area.grid(row=4, column=0, sticky="nsew")

        self.empty = ctk.CTkFrame(col, corner_radius=0, fg_color=SURFACE)  # typographic zero-state
        inner = ctk.CTkFrame(self.empty, corner_radius=0, fg_color=SURFACE)
        inner.place(relx=0.5, rely=0.4, anchor="center")
        lbl(inner, "No images in the queue.", LG).pack(pady=(0, S1))
        lbl(inner, "Add images to begin.", SM, TEXT2).pack(pady=(0, S3))
        self.empty_btn = FlatButton(inner, "Add images", self.add_files, width=160)
        self.empty_btn.pack()
        self.empty.grid(row=4, column=0, sticky="nsew")

        rule(col).grid(row=5, column=0, sticky="ew")
        foot = ctk.CTkFrame(col, height=56, corner_radius=0, fg_color=SURFACE)  # fixed height: no layout shift
        foot.grid(row=6, column=0, sticky="ew")
        foot.grid_propagate(False)
        foot.grid_columnconfigure(0, weight=1)
        self.count_label = lbl(foot, "", XS, TEXT2)
        self.count_label.grid(row=0, column=0, sticky="w", padx=S3, pady=S2)
        self.more_btn = FlatButton(foot, f"Show {PAGE_SIZE} more", self.show_more, width=140)
        self.more_btn.grid(row=0, column=1, padx=S3, pady=S2)

    def meta_for(self, i: int) -> str:
        p = self.files[i]
        if p not in self._meta_cache:
            try:
                size = p.stat().st_size
                with Image.open(p) as im:  # reads the header only
                    w, h = im.size
                self._meta_cache[p] = f"{w}×{h} · {p.suffix.lstrip('.').upper()} · {fmt_size(size)}"
            except Exception:
                self._meta_cache[p] = "Unreadable image"
        return self._meta_cache[p]

    def _render_rows(self) -> None:
        for r in self.rows:
            r.destroy()
        self.rows = []
        if not self.files:
            self.empty.tkraise()
            self.list_area.grid_remove()
            self.empty.grid()
        else:
            self.empty.grid_remove()
            self.list_area.grid()
            for i in range(min(self.visible, len(self.files))):
                row = FileRow(self.list_area, self, i)
                row.pack(fill="x")
                self.rows.append(row)

    def _refresh(self) -> None:
        self._render_rows()
        n, shown = len(self.files), min(self.visible, len(self.files))
        self.count_label.configure(text=f"Showing {shown} of {n}" if n else "0 images")
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self.app.busy
        idle = "disabled" if busy else "normal"
        self.add_btn.configure(state=idle)
        self.empty_btn.configure(state=idle)
        self.clear_btn.configure(state="normal" if (self.files and not busy) else "disabled")
        self.remove_btn.configure(state="normal" if (self.selected is not None and not busy) else "disabled")
        self.more_btn.configure(state="normal" if len(self.files) > self.visible else "disabled")
        self.run_btn.configure(state="normal" if (self.files and not busy) else "disabled")
        self.cancel_btn.configure(state="normal" if (busy and self.app.active_tab is self) else "disabled")

    # ---- queue actions ----------------------------------------------------
    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Select images", filetypes=FILETYPES)
        known = set(self.files)
        for p in paths:
            path = Path(p)
            if path not in known:
                self.files.append(path)
                self.states.append("queued")
                known.add(path)
        self._refresh()

    def remove_selected(self) -> None:
        if self.selected is None or self.app.busy:
            return
        del self.files[self.selected]
        del self.states[self.selected]
        self.selected = None
        self._refresh()

    def clear_files(self) -> None:
        self.files.clear()
        self.states.clear()
        self.selected = None
        self.visible = PAGE_SIZE
        self._meta_cache.clear()
        self._refresh()

    def show_more(self) -> None:
        self.visible += PAGE_SIZE
        self._refresh()

    def select(self, index: int) -> None:
        old = self.selected
        self.selected = None if old == index else index
        for i in (old, self.selected):
            if i is not None and i < len(self.rows):
                self.rows[i].paint()
        self._update_controls()

    # ---- tool pane --------------------------------------------------------
    def _build_reader(self) -> None:
        col = ctk.CTkFrame(self, corner_radius=0, fg_color=SURFACE)
        col.grid(row=0, column=2, sticky="nsew")
        col.grid_columnconfigure(0, weight=1)
        col.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(col, height=48, corner_radius=0, fg_color=SURFACE)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_propagate(False)
        lbl(head, self.TITLE, LG, bold=True).place(x=S4, rely=0.5, anchor="w")
        rule(col).grid(row=1, column=0, sticky="ew")

        scroll = ctk.CTkScrollableFrame(
            col, corner_radius=0, fg_color=SURFACE, scrollbar_fg_color=SURFACE,
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=TEXT2)
        try:
            scroll._scrollbar.configure(corner_radius=0)
        except Exception:
            pass
        scroll.grid(row=2, column=0, sticky="nsew")
        self.content = ctk.CTkFrame(scroll, corner_radius=0, fg_color=SURFACE)
        self.content.pack(fill="x", anchor="nw", padx=S4, pady=S4)
        c = self.content
        c.grid_columnconfigure(0, weight=1)

        lbl(c, self.SUBTITLE, SM, TEXT2, wraplength=432).grid(row=0, column=0, sticky="w", pady=(0, S3))

        self.options = ctk.CTkFrame(c, corner_radius=0, fg_color=SURFACE)
        self.options.grid(row=1, column=0, sticky="w", pady=(0, S3))
        self.build_options(self.options)

        r = 2
        if self.needs_output_dir:
            row = ctk.CTkFrame(c, corner_radius=0, fg_color=SURFACE)
            row.grid(row=r, column=0, sticky="w", pady=(0, S3))
            FlatButton(row, "Output folder…", self.pick_out_dir, width=144).pack(side="left")
            self.out_label = lbl(row, "Default: “micropixl_output” next to the first image", XS, TEXT2,
                                 wraplength=360)
            self.out_label.pack(side="left", padx=S3)
            r += 1

        act = ctk.CTkFrame(c, corner_radius=0, fg_color=SURFACE)
        act.grid(row=r, column=0, sticky="w", pady=(0, S3))
        self.run_btn = FlatButton(act, "Start", self.on_start, kind="primary", width=128)  # the one primary action
        self.run_btn.pack(side="left")
        self.cancel_btn = FlatButton(act, "Cancel", self.app.cancel_job, width=96)
        self.cancel_btn.pack(side="left", padx=S2)
        r += 1

        self.progress = ctk.CTkProgressBar(c, height=8, corner_radius=0, border_width=0,
                                           fg_color=TRACK, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.grid(row=r, column=0, sticky="ew")
        r += 1
        self.status = lbl(c, "Ready.", XS, TEXT2, height=24)
        self.status.grid(row=r, column=0, sticky="w", pady=(S1, S2))
        r += 1

        self.extra_actions(c, r)
        r += 1
        self.output = make_textbox(c, self.OUTPUT_HEIGHT)
        self.output.grid(row=r, column=0, sticky="ew")

        col.bind("<Configure>", self._on_resize)  # cap content at 720px

    def _on_resize(self, e) -> None:
        extra = max(0, e.width - (self.CONTENT_MAX + 2 * S4 + 16))
        if extra != self._last_extra:
            self._last_extra = extra
            self.content.pack_configure(padx=(S4, S4 + extra))

    # ---- hooks for subclasses ---------------------------------------------
    def build_options(self, parent) -> None: ...
    def extra_actions(self, parent, row: int) -> None: ...
    def prepare(self):  # -> (worker_fn, params)
        raise NotImplementedError

    def pick_out_dir(self) -> None:
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.out_dir = Path(d)
            self.out_label.configure(text=str(self.out_dir))

    def resolve_out_dir(self) -> Path:
        base = self.out_dir or (self.files[0].parent / "micropixl_output")
        base.mkdir(parents=True, exist_ok=True)
        return base

    # ---- run control ------------------------------------------------------
    def on_start(self) -> None:
        if not self.files:
            return
        try:
            prepared = self.prepare()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        if prepared is None:
            return
        fn, params = prepared
        self.states = ["queued"] * len(self.files)
        for r in self.rows:
            r.paint()
        self.progress.set(0)
        self.on_begin()
        self.app.start_job(self, fn, params)

    def on_begin(self) -> None:
        set_text(self.output, "")

    # ---- message handlers (UI thread) --------------------------------------
    def on_log(self, message: str) -> None:
        append_text(self.output, message + "\n")
        self.status.configure(text=message[:120])

    def on_text(self, text: str) -> None:
        set_text(self.output, text)

    def on_progress(self, fraction: float) -> None:
        self.progress.set(fraction)

    def on_file(self, index: int, state: str) -> None:
        if 0 <= index < len(self.states):
            self.states[index] = state
            if index < len(self.rows):
                self.rows[index].paint()

    def on_done(self) -> None:
        self.status.configure(text="Finished.")
        self._update_controls()


# --------------------------------------------------------------------------- #
# Tool 1: OCR
# --------------------------------------------------------------------------- #
class OCRPage(ToolPage):
    TITLE = "Extract text"
    SUBTITLE = "Read text from photos, scans and screenshots with Tesseract. Everything stays on this computer."
    OUTPUT_HEIGHT = 320
    PSM_MODES = {
        "Auto layout (default)": 3,
        "Single uniform block of text": 6,
        "Single line": 7,
        "Sparse text (find as much as possible)": 11,
    }

    def build_options(self, p) -> None:
        lbl(p, "Language(s)", SM, TEXT2).grid(row=0, column=0, sticky="w", padx=(0, S3), pady=S1)
        self.lang_entry = make_entry(p, 160)
        self.lang_entry.insert(0, "eng")
        self.lang_entry.grid(row=0, column=1, sticky="w", pady=S1)
        lbl(p, "Example: eng+ara+fra. Language data must be installed.", XS, TEXT2).grid(
            row=1, column=1, sticky="w")
        lbl(p, "Layout mode", SM, TEXT2).grid(row=2, column=0, sticky="w", padx=(0, S3), pady=(S3, S1))
        box, self.psm_menu = make_menu(p, list(self.PSM_MODES), 360)
        box.grid(row=2, column=1, sticky="w", pady=(S3, S1))

    def extra_actions(self, parent, row: int) -> None:
        bar, _ = action_bar(parent, [("Copy text", self.copy_text), ("Save .txt…", self.save_text)])
        bar.grid(row=row, column=0, sticky="w", pady=(0, S2))

    def copy_text(self) -> None:
        text = self.output.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status.configure(text="Text copied to clipboard.")

    def save_text(self) -> None:
        text = self.output.get("1.0", "end").strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.status.configure(text=f"Saved: {path}")

    def on_log(self, message: str) -> None:
        self.status.configure(text=message[:120])  # output box is reserved for the OCR result

    def prepare(self):
        tess = find_tesseract()
        if not tess:
            raise RuntimeError(
                "Tesseract OCR engine was not found.\n\nInstall it (see the header of main.py) "
                "and make sure it is on your PATH, then restart the app.")
        lang = self.lang_entry.get().strip() or "eng"
        psm = self.PSM_MODES[self.psm_menu.get()]
        return self.worker, dict(files=list(self.files), lang=lang, psm=psm, tess=tess)

    @staticmethod
    def worker(job: Job, p: dict) -> None:
        try:
            import pytesseract
        except ImportError:
            job.log("pytesseract is not installed (pip install pytesseract).")
            return
        pytesseract.pytesseract.tesseract_cmd = p["tess"]

        files: list[Path] = p["files"]
        chunks: list[str] = []
        for i, path in enumerate(files):
            if job.cancel.is_set():
                job.log("Cancelled.")
                break
            job.log(f"OCR {i + 1}/{len(files)}: {path.name}")
            job.file_status(i, "working")
            ok = False
            try:
                with Image.open(path) as src:
                    img = ImageOps.exif_transpose(src)
                    gray = flatten_to_gray(img)
                    if img is not src:
                        img.close()
                longest = max(gray.size)
                if longest > MAX_OCR_SIDE:
                    gray.thumbnail((MAX_OCR_SIDE, MAX_OCR_SIDE), Image.LANCZOS)
                elif longest < MIN_OCR_SIDE:
                    gray = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
                gray = ImageOps.autocontrast(gray)
                text = pytesseract.image_to_string(
                    gray, lang=p["lang"], config=f"--psm {p['psm']}", timeout=180).strip()
                gray.close()
                header = f"===== {path.name} =====\n" if len(files) > 1 else ""
                chunks.append(f"{header}{text if text else '(no text detected)'}")
                ok = True
            except pytesseract.TesseractNotFoundError:
                job.file_status(i, "failed")
                job.log("Tesseract executable could not be started.")
                return
            except RuntimeError as exc:
                chunks.append(f"===== {path.name} =====\n[OCR timed out: {exc}]")
            except Exception as exc:
                chunks.append(f"===== {path.name} =====\n[Error: {exc}]")
            finally:
                gc.collect()
            job.file_status(i, "done" if ok else "failed")
            job.progress((i + 1) / len(files))
            job.result_text("\n\n".join(chunks))
        else:
            job.log("OCR complete.")


# --------------------------------------------------------------------------- #
# Tool 2: Raster -> SVG
# --------------------------------------------------------------------------- #
class SVGPage(ToolPage):
    TITLE = "Raster to SVG"
    SUBTITLE = "Trace PNG and JPG images into clean vector files with vtracer."
    needs_output_dir = True

    def build_options(self, p) -> None:
        lbl(p, "Preset", SM, TEXT2).grid(row=0, column=0, sticky="w", padx=(0, S3), pady=S1)
        box, self.preset_menu = make_menu(p, list(SVG_PRESETS), 360)
        box.grid(row=0, column=1, sticky="w", pady=S1)
        lbl(p, f"Images larger than {MAX_SVG_SIDE}px are downscaled first to save memory.", XS, TEXT2).grid(
            row=1, column=1, sticky="w")

    def prepare(self):
        try:
            import vtracer  # noqa: F401
        except ImportError:
            raise RuntimeError("The 'vtracer' package is not installed.\n\nRun: pip install vtracer")
        return self.worker, dict(files=list(self.files), out=self.resolve_out_dir(),
                                 opts=SVG_PRESETS[self.preset_menu.get()])

    @staticmethod
    def worker(job: Job, p: dict) -> None:
        import vtracer

        files: list[Path] = p["files"]
        out_dir: Path = p["out"]
        opts: dict = p["opts"]
        done = 0
        with tempfile.TemporaryDirectory(prefix="micropixl_") as tmp:
            for i, path in enumerate(files):
                if job.cancel.is_set():
                    job.log("Cancelled.")
                    break
                job.log(f"Vectorising {i + 1}/{len(files)}: {path.name}")
                job.file_status(i, "working")
                ok = False
                try:
                    src = path
                    with Image.open(path) as im:
                        needs_prep = max(im.size) > MAX_SVG_SIDE or path.suffix.lower() not in (".png", ".jpg", ".jpeg")
                        if needs_prep:
                            work = ImageOps.exif_transpose(im)
                            work = work.convert("RGBA" if "A" in work.getbands() else "RGB")
                            work.thumbnail((MAX_SVG_SIDE, MAX_SVG_SIDE), Image.LANCZOS)
                            src = Path(tmp) / "prepared.png"
                            work.save(src)
                            work.close()
                    target = unique_path(out_dir / f"{path.stem}.svg")
                    try:
                        vtracer.convert_image_to_svg_py(str(src), str(target), **opts)
                    except TypeError:
                        core = {k: v for k, v in opts.items() if k != "path_precision"}
                        vtracer.convert_image_to_svg_py(str(src), str(target), **core)
                    job.log(f"  saved -> {target.name}")
                    done += 1
                    ok = True
                except Exception as exc:
                    job.log(f"  FAILED ({path.name}): {exc}")
                finally:
                    gc.collect()
                job.file_status(i, "done" if ok else "failed")
                job.progress((i + 1) / len(files))
        job.log(f"Done: {done}/{len(files)} converted. Output: {out_dir}")


# --------------------------------------------------------------------------- #
# Tool 3: App / PWA icons
# --------------------------------------------------------------------------- #
class IconPage(ToolPage):
    TITLE = "App icons"
    SUBTITLE = "Resize one image into the standard sizes used by PWAs, iOS, Android and desktop apps."
    needs_output_dir = True
    OUTPUT_HEIGHT = 160

    def build_options(self, p) -> None:
        lbl(p, "Sizes (px)", SM, TEXT2).grid(row=0, column=0, sticky="w", pady=(0, S2))
        grid = ctk.CTkFrame(p, corner_radius=0, fg_color=SURFACE)
        grid.grid(row=1, column=0, sticky="w")
        self.size_chips: dict[int, Chip] = {}
        for idx, size in enumerate(PRESET_ICON_SIZES):
            chip = Chip(grid, str(size), active=size in DEFAULT_CHECKED, width=64)
            chip.grid(row=idx // 6, column=idx % 6, padx=(0, S2), pady=(0, S2))
            self.size_chips[size] = chip

        lbl(p, "Custom sizes", SM, TEXT2).grid(row=2, column=0, sticky="w", pady=(S1, S2))
        self.custom_entry = make_entry(p, 280, "e.g. 300, 310x310, 620")
        self.custom_entry.grid(row=3, column=0, sticky="w")

        lbl(p, "Square mode", SM, TEXT2).grid(row=4, column=0, sticky="w", pady=(S3, S2))
        box, self.mode_menu = make_menu(p, ["Fit - keep whole image (transparent padding)",
                                            "Fill - crop to square"], 360)
        box.grid(row=5, column=0, sticky="w")

        self.ico_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(p, text="Also create favicon.ico (16 / 32 / 48)", variable=self.ico_var, height=40,
                        corner_radius=0, checkbox_width=24, checkbox_height=24, border_width=1,
                        fg_color=ACCENT, hover_color=HOVER_ACCENT, border_color=BORDER,
                        checkmark_color=ON_ACCENT, text_color=TEXT, font=F(SM)).grid(
            row=6, column=0, sticky="w", pady=(S2, 0))

    def prepare(self):
        sizes = {s for s, chip in self.size_chips.items() if chip.active}
        sizes.update(parse_sizes(self.custom_entry.get()))
        if not sizes:
            raise ValueError("Select at least one icon size.")
        return self.worker, dict(files=list(self.files), out=self.resolve_out_dir(), sizes=sorted(sizes),
                                 fill=self.mode_menu.get().startswith("Fill"), ico=self.ico_var.get())

    @staticmethod
    def worker(job: Job, p: dict) -> None:
        files: list[Path] = p["files"]
        sizes: list[int] = p["sizes"]
        total_steps = len(files) * len(sizes)
        step = 0
        for i, path in enumerate(files):
            if job.cancel.is_set():
                job.log("Cancelled.")
                break
            job.log(f"Processing {path.name}")
            job.file_status(i, "working")
            ok = False
            try:
                dest = p["out"] / f"{path.stem}_icons"
                dest.mkdir(parents=True, exist_ok=True)
                with Image.open(path) as raw:
                    src = ImageOps.exif_transpose(raw).convert("RGBA")
                if max(src.size) > MAX_ICON_SOURCE:
                    src.thumbnail((MAX_ICON_SOURCE, MAX_ICON_SOURCE), Image.LANCZOS)

                for size in sizes:
                    if job.cancel.is_set():
                        break
                    if p["fill"]:
                        icon = ImageOps.fit(src, (size, size), Image.LANCZOS)
                    else:
                        scaled = ImageOps.contain(src, (size, size), Image.LANCZOS)
                        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                        icon.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
                        scaled.close()
                    icon.save(unique_path(dest / f"{path.stem}_{size}x{size}.png"), optimize=True)
                    icon.close()
                    step += 1
                    job.progress(step / total_steps)

                if p["ico"] and not job.cancel.is_set():
                    base = ImageOps.contain(src, (256, 256), Image.LANCZOS)
                    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
                    canvas.paste(base, ((256 - base.width) // 2, (256 - base.height) // 2))
                    canvas.save(unique_path(dest / "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)])
                    base.close()
                    canvas.close()
                src.close()
                job.log(f"  saved {len(sizes)} icon(s) -> {dest}")
                ok = True
            except Exception as exc:
                job.log(f"  FAILED ({path.name}): {exc}")
                step += len(sizes)
                job.progress(min(1.0, step / total_steps))
            finally:
                gc.collect()
            job.file_status(i, "done" if ok else "failed")
        job.log("Done.")


# --------------------------------------------------------------------------- #
# Main window: 72px rail + pages
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    POLL_MS = 100
    RAIL_W = 72

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("system")
        STATE["mode"] = 1 if ctk.get_appearance_mode() == "Dark" else 0
        ctk.set_appearance_mode("dark" if STATE["mode"] else "light")  # lock to the resolved mode
        init_fonts(self)
        self.title(APP_NAME)
        self.geometry("1200x760")
        self.minsize(960, 640)
        self.configure(fg_color=SURFACE)

        self.queue: queue.Queue = queue.Queue()
        self.job: Job | None = None
        self.thread: threading.Thread | None = None
        self.active_tab: ToolPage | None = None
        self.busy = False

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(2, weight=1)
        self._build_rail()
        rule(self).grid(row=0, column=1, sticky="ns")

        self.stage = ctk.CTkFrame(self, corner_radius=0, fg_color=SURFACE)
        self.stage.grid(row=0, column=2, sticky="nsew")
        self.stage.grid_rowconfigure(0, weight=1)
        self.stage.grid_columnconfigure(0, weight=1)

        self.pages: dict[str, ToolPage] = {
            "ocr": OCRPage(self.stage, self),
            "svg": SVGPage(self.stage, self),
            "icons": IconPage(self.stage, self),
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
        self.show("ocr")
        self.after(self.POLL_MS, self._poll)

    # ---- rail ------------------------------------------------------------
    def _build_rail(self) -> None:
        rail = ctk.CTkFrame(self, width=self.RAIL_W, corner_radius=0, fg_color=SURFACE)
        rail.grid(row=0, column=0, sticky="ns")
        rail.grid_propagate(False)
        rail.pack_propagate(False)

        logo = ctk.CTkFrame(rail, width=self.RAIL_W, height=48, corner_radius=0, fg_color=SURFACE)
        logo.pack(fill="x")
        logo.pack_propagate(False)
        PentagonDot(logo, 24).place(relx=0.5, rely=0.5, anchor="center")
        rule(rail).pack(fill="x")

        self.rail_buttons: dict[str, RailButton] = {}
        for key, icon in (("ocr", "ocr"), ("svg", "svg"), ("icons", "icons")):
            btn = RailButton(rail, icon, lambda k=key: self.show(k))
            btn.pack(pady=(S2, 0))
            self.rail_buttons[key] = btn
        RailButton(rail, "theme", self.toggle_theme).pack(side="bottom", pady=(0, S2))

    def show(self, key: str) -> None:
        for k, page in self.pages.items():
            if k == key:
                page.grid()
            else:
                page.grid_remove()
        for k, btn in self.rail_buttons.items():
            btn.set_selected(k == key)

    def toggle_theme(self) -> None:
        STATE["mode"] ^= 1
        ctk.set_appearance_mode("dark" if STATE["mode"] else "light")
        for w in list(THEMED):
            try:
                w.retheme()
            except tk.TclError:
                pass

    # ---- job control (unchanged behaviour) -----------------------------------
    def start_job(self, tab: ToolPage, fn, params: dict) -> None:
        if self.busy or (self.thread and self.thread.is_alive()):
            messagebox.showinfo(APP_NAME, "A job is already running. Wait or cancel it first.")
            return
        self.job = Job(self.queue)
        self.active_tab = tab
        self.busy = True
        for page in self.pages.values():
            page._update_controls()
        job = self.job

        def runner() -> None:
            try:
                fn(job, params)
            except Exception as exc:
                job.log(f"Unexpected error: {exc}")
            finally:
                self.queue.put(("done", None))
                gc.collect()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def cancel_job(self) -> None:
        if self.job:
            self.job.cancel.set()

    def _poll(self) -> None:
        try:
            for _ in range(50):
                kind, payload = self.queue.get_nowait()
                tab = self.active_tab
                if tab is None:
                    continue
                if kind == "log":
                    tab.on_log(payload)
                elif kind == "progress":
                    tab.on_progress(payload)
                elif kind == "text":
                    tab.on_text(payload)
                elif kind == "file":
                    tab.on_file(*payload)
                elif kind == "done":
                    self.busy = False
                    tab.on_done()
                    for page in self.pages.values():
                        page._update_controls()
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._poll)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
