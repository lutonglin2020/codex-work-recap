"""Synthetic native-UI preview: no real cache, window, service or model access.

Run from the repository: python -X utf8 tests/render_native_preview.py
The PNGs use the production GDI+ painter and synthetic fixtures below.
They demonstrate drawing only, not verified desktop interaction.
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recap.island_paint import IslandPainter, SHADOW
from recap.island_widget import IslandWidget, COLLAPSED
from recap.reveal_policy import RevealPolicy
from recap.widget import WinApi


SYNTHETIC_SNAPSHOT = {
    "state": "ready",
    "summary": {
        "id": "synthetic-preview-only",
        "overview": "完成了导出功能，重复下载可以复用已有文件。\n定位了登录异常，修复后仍需在真实环境验证。",
        "generated_at": "2026-10-16T10:24:00+08:00",
        "period_label": "合成演示",
        "retained": False,
    },
}


def render_snapshot(snapshot, *, scale=2, expanded=True, hovering=False, presented=False):
    """Return RGBA pixels, exact measured body lines, and metadata from a DIB."""
    widget = IslandWidget.__new__(IslandWidget)
    widget.api, widget.painter = WinApi(), IslandPainter()
    widget.hwnd, widget.path = None, Path("synthetic-preview-unused")
    widget.policy, widget.font, widget.font_scale = RevealPolicy(), None, None
    widget.scale, widget.work = scale, (0, 0, round(1920 * scale), round(1040 * scale))
    widget.read_json = lambda path: snapshot
    try:
        widget.read_display()
        if presented:
            widget.policy.last_presented_id = widget.summary_id
        widget.measure()
        width, height = widget.expanded_size if expanded else tuple(
            round((value + 2 * SHADOW) * scale) for value in COLLAPSED)
        widget.painter.draw(width, height, scale, widget.lines, expanded=expanded,
                            hovering=hovering, pending=widget.has_new_summary(), caption=widget.caption)
        stride = widget.painter.size[0] * 4
        bgra = ctypes.string_at(widget.painter.pixels, stride * height)
        rgba = bytearray()
        for y in range(height):
            for x in range(width):
                b, g, r, a = bgra[y * stride + x * 4:y * stride + x * 4 + 4]
                # The layered-window DIB stores premultiplied channels; PNG
                # requires straight alpha to avoid dark fringes when composed.
                rgba.extend((min(255, round(r * 255 / a)), min(255, round(g * 255 / a)),
                             min(255, round(b * 255 / a)), a) if a else (0, 0, 0, 0))
        return {"width": width, "height": height, "rgba": bytes(rgba),
                "lines": widget.lines, "caption": widget.caption,
                "pending": widget.has_new_summary()}
    finally:
        if widget.font:
            widget.api.gdi.DeleteObject(widget.font)
        widget.painter.close()


def write_png(path, rendered):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    width, height, rgba = rendered["width"], rendered["height"], rendered["rgba"]
    scanlines = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evidence/native-ui"))
    parser.add_argument("--scale", type=float, default=2)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    retained = {**SYNTHETIC_SNAPSHOT, "summary": {**SYNTHETIC_SNAPSHOT["summary"], "retained": True}}
    cases = (
        ("collapsed", SYNTHETIC_SNAPSHOT, {"expanded": False, "presented": True}),
        ("collapsed-new", SYNTHETIC_SNAPSHOT, {"expanded": False}),
        ("expanded-new", SYNTHETIC_SNAPSHOT, {"hovering": True}),
        ("expanded-retained", retained, {"hovering": True}),
        ("empty", {"state": "idle", "summary": None}, {}),
        ("paused", {"state": "paused", "paused": True, "summary": None}, {}),
    )
    manifest = {"source": "Synthetic test data only; production GDI+ painter; no live window or cache", "scale": args.scale, "images": []}
    for name, snapshot, options in cases:
        rendered = render_snapshot(snapshot, scale=args.scale, **options)
        path = args.output / f"{name}.png"
        write_png(path, rendered)
        manifest["images"].append({"file": path.name, "width": rendered["width"], "height": rendered["height"],
                                   "caption": rendered["caption"]})
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
