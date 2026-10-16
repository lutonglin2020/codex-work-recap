"""Single-canvas GDI+ painter for a non-activating Windows layered island.

rect is screen (x, y, width, height); line coordinates are logical pixels
relative to the black pill. scale is DPI only, never the animation ratio.
Import creates no window, service, network connection, or model run.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import math
import os

SHADOW = 12
BODY_FONT = 14
META_FONT = 11
P = c.c_void_p
PP = c.POINTER(P)
F = c.c_float
I = c.c_int
U = c.c_uint32


class BitmapHeader(c.Structure):
    _fields_ = [("size", U), ("width", w.LONG), ("height", w.LONG),
                ("planes", w.WORD), ("bits", w.WORD), ("compression", U),
                ("image_size", U), ("xppm", w.LONG), ("yppm", w.LONG),
                ("used", U), ("important", U)]


class BitmapInfo(c.Structure):
    _fields_ = [("header", BitmapHeader), ("colors", U * 3)]


class RectF(c.Structure):
    _fields_ = [("x", F), ("y", F), ("width", F), ("height", F)]


class Blend(c.Structure):
    _fields_ = [("op", w.BYTE), ("flags", w.BYTE), ("alpha", w.BYTE), ("format", w.BYTE)]


class Startup(c.Structure):
    _fields_ = [("version", U), ("callback", P), ("no_thread", w.BOOL), ("no_codecs", w.BOOL)]


def _bind(dll, name, args, result=I):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


def _ok(status):
    if status:
        raise OSError(status, "GDI+ drawing failed")


def _argb(rgb, alpha=1.0):
    return (round(255 * min(1.0, max(0.0, alpha))) << 24) | rgb


class IslandPainter:
    """Owns one premultiplied BGRA DIB; render/close stay on the UI thread."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("IslandPainter requires Windows")
        self.gdi = c.WinDLL("gdi32.dll", use_last_error=True, winmode=0x800)
        self.user = c.WinDLL("user32.dll", use_last_error=True, winmode=0x800)
        self.plus = c.WinDLL("gdiplus.dll", winmode=0x800)
        self.token = c.c_size_t()
        self.image, self.graphics, self.family, self.format = (P() for _ in range(4))
        self.fonts = {}
        self.dc = self.bitmap = self.old_bitmap = None
        self.pixels, self.size = P(), None
        _bind(self.gdi, "CreateCompatibleDC", [P], P)
        _bind(self.gdi, "CreateDIBSection", [P, c.POINTER(BitmapInfo), U, PP, P, U], P)
        _bind(self.gdi, "SelectObject", [P, P], P)
        _bind(self.gdi, "DeleteObject", [P], w.BOOL)
        _bind(self.gdi, "DeleteDC", [P], w.BOOL)
        _bind(self.user, "UpdateLayeredWindow", [P, P, c.POINTER(w.POINT), c.POINTER(w.SIZE),
                                                P, c.POINTER(w.POINT), U, c.POINTER(Blend), U], w.BOOL)
        definitions = {
            "GdiplusStartup": ([c.POINTER(c.c_size_t), c.POINTER(Startup), P], I),
            "GdiplusShutdown": ([c.c_size_t], None),
            "GdipCreateBitmapFromScan0": ([I, I, I, I, P, PP], I),
            "GdipGetImageGraphicsContext": ([P, PP], I),
            "GdipDeleteGraphics": ([P], I), "GdipDisposeImage": ([P], I),
            "GdipSetSmoothingMode": ([P, I], I), "GdipSetPixelOffsetMode": ([P, I], I),
            "GdipSetTextRenderingHint": ([P, I], I), "GdipGraphicsClear": ([P, U], I),
            "GdipResetClip": ([P], I), "GdipSetClipRect": ([P, F, F, F, F, I], I),
            "GdipCreatePath": ([I, PP], I), "GdipDeletePath": ([P], I),
            "GdipAddPathArc": ([P, F, F, F, F, F, F], I),
            "GdipClosePathFigure": ([P], I), "GdipFillPath": ([P, P, P], I),
            "GdipCreateSolidFill": ([U, PP], I), "GdipDeleteBrush": ([P], I),
            "GdipFillEllipse": ([P, P, F, F, F, F], I),
            "GdipCreateFontFamilyFromName": ([w.LPCWSTR, P, PP], I),
            "GdipDeleteFontFamily": ([P], I), "GdipCreateFont": ([P, F, I, I, PP], I),
            "GdipDeleteFont": ([P], I), "GdipStringFormatGetGenericTypographic": ([PP], I),
            "GdipSetStringFormatFlags": ([P, I], I), "GdipDeleteStringFormat": ([P], I),
            "GdipDrawString": ([P, w.LPCWSTR, I, P, c.POINTER(RectF), P, P], I),
            "GdipCreatePen1": ([U, F, I, PP], I), "GdipDeletePen": ([P], I),
            "GdipSetPenStartCap": ([P, I], I), "GdipSetPenEndCap": ([P, I], I),
            "GdipDrawLine": ([P, P, F, F, F, F], I),
        }
        for name, (args, result) in definitions.items():
            _bind(self.plus, name, args, result)
        try:
            _ok(self.plus.GdiplusStartup(c.byref(self.token), c.byref(Startup(1, None, False, False)), None))
            _ok(self.plus.GdipCreateFontFamilyFromName("Microsoft YaHei UI", None, c.byref(self.family)))
            _ok(self.plus.GdipStringFormatGetGenericTypographic(c.byref(self.format)))
            _ok(self.plus.GdipSetStringFormatFlags(self.format, 0x5000))  # NoWrap | NoClip.
        except Exception:
            self.close()
            raise

    def _drop_canvas(self):
        for name, field in (("GdipDeleteGraphics", "graphics"), ("GdipDisposeImage", "image")):
            value = getattr(self, field)
            if value:
                getattr(self.plus, name)(value)
                setattr(self, field, P())
        if self.dc and self.old_bitmap:
            self.gdi.SelectObject(self.dc, self.old_bitmap)
        if self.bitmap:
            self.gdi.DeleteObject(self.bitmap)
        if self.dc:
            self.gdi.DeleteDC(self.dc)
        self.dc = self.bitmap = self.old_bitmap = None
        self.pixels, self.size = P(), None

    def reserve(self, width, height):
        """Preallocate physical-pixel capacity; never shows or activates a window."""
        if not self.token.value:
            raise RuntimeError("IslandPainter is closed")
        self._canvas(round(width), round(height))

    def _canvas(self, width, height):
        if min(width, height) <= 0:
            raise ValueError("Invalid canvas dimensions")
        if self.size:
            if self.size[0] >= width and self.size[1] >= height:
                return
            width, height = max(width, self.size[0]), max(height, self.size[1])
        if width * height > 8_000_000:
            raise ValueError("Island canvas capacity is too large")
        self._drop_canvas()
        try:
            self.dc = self.gdi.CreateCompatibleDC(None)
            if not self.dc:
                raise c.WinError(c.get_last_error())
            info = BitmapInfo()
            info.header = BitmapHeader(c.sizeof(BitmapHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            self.bitmap = self.gdi.CreateDIBSection(self.dc, c.byref(info), 0, c.byref(self.pixels), None, 0)
            if not self.bitmap:
                raise c.WinError(c.get_last_error())
            self.old_bitmap = self.gdi.SelectObject(self.dc, self.bitmap)
            if self.old_bitmap in (None, c.c_void_p(-1).value):
                self.old_bitmap = None
                raise c.WinError(c.get_last_error())
            _ok(self.plus.GdipCreateBitmapFromScan0(width, height, width * 4, 0xE200B,
                                                   self.pixels, c.byref(self.image)))
            _ok(self.plus.GdipGetImageGraphicsContext(self.image, c.byref(self.graphics)))
            _ok(self.plus.GdipSetSmoothingMode(self.graphics, 4))
            _ok(self.plus.GdipSetPixelOffsetMode(self.graphics, 4))
            _ok(self.plus.GdipSetTextRenderingHint(self.graphics, 3))
            self.size = width, height
        except Exception:
            self._drop_canvas()
            raise

    def _rounded(self, x, y, width, height, radius, color):
        path, brush = P(), P()
        diameter = max(0.1, min(radius * 2, width, height))
        try:
            _ok(self.plus.GdipCreatePath(0, c.byref(path)))
            for px, py, angle in ((x, y, 180), (x + width - diameter, y, 270),
                                  (x + width - diameter, y + height - diameter, 0),
                                  (x, y + height - diameter, 90)):
                _ok(self.plus.GdipAddPathArc(path, px, py, diameter, diameter, angle, 90))
            _ok(self.plus.GdipClosePathFigure(path))
            _ok(self.plus.GdipCreateSolidFill(color, c.byref(brush)))
            _ok(self.plus.GdipFillPath(self.graphics, brush, path))
        finally:
            if brush: self.plus.GdipDeleteBrush(brush)
            if path: self.plus.GdipDeletePath(path)

    def _text(self, lines, scale, x, y, width, alpha, *, size=BODY_FONT, color=0xEEF0F3):
        # Cache the two text sizes so animation does not recreate font handles.
        key = (scale, size)
        if key not in self.fonts:
            font = P()
            _ok(self.plus.GdipCreateFont(self.family, round(size * scale), 0, 2, c.byref(font)))
            self.fonts[key] = font
        brush = P()
        try:
            # Shapes use subpixel coverage; glyph baselines use the physical
            # pixel grid so 175% DPI does not alternate half-pixel text rows.
            _ok(self.plus.GdipSetPixelOffsetMode(self.graphics, 3))
            _ok(self.plus.GdipCreateSolidFill(_argb(color, alpha), c.byref(brush)))
            for text, lx, ly in lines:
                box = RectF(round(x + lx * scale), round(y + ly * scale),
                            max(1, round(width - (lx + 24) * scale)), round(24 * scale))
                _ok(self.plus.GdipDrawString(self.graphics, str(text), -1, self.fonts[key],
                                             c.byref(box), self.format, brush))
        finally:
            self.plus.GdipSetPixelOffsetMode(self.graphics, 4)
            if brush: self.plus.GdipDeleteBrush(brush)

    def draw(self, width, height, scale, lines, text_alpha=1.0, pill_alpha=1.0,
             expanded=True, hovering=False, pending=False, caption=""):
        """Draw into our own DIB without creating, showing or updating a window."""
        if not self.token.value:
            raise RuntimeError("IslandPainter is closed")
        width, height = round(width), round(height)
        scale = float(scale)
        if not math.isfinite(scale) or scale <= 0 or not 0 < width * height <= 8_000_000 or min(width, height) <= 0:
            raise ValueError("Invalid island dimensions or DPI")
        self._canvas(width, height)
        _ok(self.plus.GdipResetClip(self.graphics))
        _ok(self.plus.GdipGraphicsClear(self.graphics, 0))
        margin = SHADOW * scale
        pw, ph = width - 2 * margin, height - 2 * margin
        pa, ta = min(1.0, max(0.0, pill_alpha)), min(1.0, max(0.0, text_alpha))
        if pw > 0 and ph > 0 and pa:
            radius = min(ph / 2, 24 * scale)
            for layer in range(8, 0, -1):
                spread = 9 * scale * layer / 8
                self._rounded(margin - spread, margin - spread + 1.5 * scale,
                              pw + 2 * spread, ph + 2 * spread, radius + spread,
                              _argb(0, (1 + (1 - layer / 8) * 2) / 255 * pa))
            self._rounded(margin, margin, pw, ph, radius, _argb(0x3B3E44, pa))
            border = max(1, .75 * scale)
            self._rounded(margin + border, margin + border, pw - 2 * border,
                          ph - 2 * border, max(0, radius - border), _argb(0x1B1D21, pa))
            _ok(self.plus.GdipSetClipRect(self.graphics, margin, margin, pw, ph, 1))
            if expanded and ta:
                if caption:
                    self._text([(caption, 28, 16)], scale, margin, margin, pw, ta * pa,
                               size=META_FONT, color=0x9CA4B1)
                self._text(lines, scale, margin, margin, pw, ta * pa)
            if not expanded:
                self._rounded(margin + pw / 2 - 16 * scale, margin + ph / 2 - 1.5 * scale,
                              32 * scale, 3 * scale, 1.5 * scale, _argb(0xB6B8BC, 0.65 * pa))
                if pending:
                    brush = P()
                    try:
                        _ok(self.plus.GdipCreateSolidFill(_argb(0xD8B57A, .9 * pa), c.byref(brush)))
                        _ok(self.plus.GdipFillEllipse(self.graphics, brush, margin + pw / 2 + 24 * scale,
                                                      margin + ph / 2 - 2 * scale, 4 * scale, 4 * scale))
                    finally:
                        if brush: self.plus.GdipDeleteBrush(brush)
            if expanded and hovering and ta > 0.8:
                pen = P()
                try:
                    _ok(self.plus.GdipCreatePen1(_argb(0x8F9298, ta * pa * .85), 1.25 * scale, 2, c.byref(pen)))
                    self.plus.GdipSetPenStartCap(pen, 2)
                    self.plus.GdipSetPenEndCap(pen, 2)
                    cx, cy, d = margin + pw - 20 * scale, margin + 20 * scale, 3.5 * scale
                    _ok(self.plus.GdipDrawLine(self.graphics, pen, cx - d, cy - d, cx + d, cy + d))
                    _ok(self.plus.GdipDrawLine(self.graphics, pen, cx - d, cy + d, cx + d, cy - d))
                finally:
                    if pen: self.plus.GdipDeletePen(pen)

    def render(self, hwnd, rect, scale, lines, text_alpha=1.0, pill_alpha=1.0,
               expanded=True, hovering=False, pending=False, caption=""):
        x, y, width, height = (round(value) for value in rect)
        self.draw(width, height, scale, lines, text_alpha, pill_alpha,
                  expanded, hovering, pending, caption)
        position, size, origin, blend = w.POINT(x, y), w.SIZE(width, height), w.POINT(0, 0), Blend(0, 0, 255, 1)
        if not self.user.UpdateLayeredWindow(hwnd, None, c.byref(position), c.byref(size), self.dc,
                                             c.byref(origin), 0, c.byref(blend), 2):
            raise c.WinError(c.get_last_error())

    def close(self):
        self._drop_canvas()
        for font in self.fonts.values():
            self.plus.GdipDeleteFont(font)
        self.fonts.clear()
        for name, field in (("GdipDeleteStringFormat", "format"),
                            ("GdipDeleteFontFamily", "family")):
            value = getattr(self, field)
            if value:
                getattr(self.plus, name)(value)
                setattr(self, field, P())
        if self.token.value:
            self.plus.GdiplusShutdown(self.token)
            self.token.value = 0
