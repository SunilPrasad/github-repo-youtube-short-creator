"""Pillow post-processing for GitHub screenshots.

Composites each screenshot onto a dark 1080×1920 canvas with
rounded corners, drop shadow, and center vertical placement.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


# ── Constants ─────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1080, 1920
BG_TOP = (15, 15, 35)
BG_BOTTOM = (26, 26, 62)
PADDING = 60
CORNER_RADIUS = 20
SHADOW_OFFSET = 15
SHADOW_BLUR = 15
SHADOW_COLOR = (0, 0, 0, 180)


# ── Gradient Background ───────────────────────────────────────────────────────

def _make_gradient_bg(w: int = CANVAS_W, h: int = CANVAS_H) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img


# ── Rounded Corners ───────────────────────────────────────────────────────────

def _add_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
    """Return image with an alpha mask giving rounded corners."""
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
    img.putalpha(mask)
    return img


# ── Drop Shadow ───────────────────────────────────────────────────────────────

def _make_shadow(w: int, h: int, offset: int, blur: int) -> Image.Image:
    shadow_size = (w + offset * 2 + blur * 2, h + offset * 2 + blur * 2)
    shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x0, y0 = blur + offset, blur + offset
    draw.rectangle([x0, y0, x0 + w, y0 + h], fill=SHADOW_COLOR)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    return shadow


# ── Main Post-Processor ───────────────────────────────────────────────────────

def process_screenshot(
    screenshot_path: str,
    output_path: str,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    padding: int = PADDING,
    corner_radius: int = CORNER_RADIUS,
) -> None:
    """
    Composite a GitHub screenshot onto a dark 1080×1920 canvas.

    Steps:
    1. Create gradient background canvas
    2. Resize screenshot to fit with padding
    3. Add rounded corners
    4. Add drop shadow
    5. Center vertically on canvas
    """
    canvas = _make_gradient_bg(canvas_w, canvas_h)

    # Load and resize screenshot
    shot = Image.open(screenshot_path).convert("RGBA")
    max_w = canvas_w - padding * 2
    max_h = canvas_h - padding * 2
    shot.thumbnail((max_w, max_h), Image.LANCZOS)

    # Add rounded corners
    shot = _add_rounded_corners(shot, corner_radius)

    # Drop shadow
    shadow = _make_shadow(shot.width, shot.height, SHADOW_OFFSET, SHADOW_BLUR)
    shadow_canvas = canvas.convert("RGBA")
    sx = (canvas_w - shadow.width) // 2
    sy = (canvas_h - shadow.height) // 2
    shadow_canvas.paste(shadow, (sx, sy), shadow)
    canvas = shadow_canvas.convert("RGB")

    # Paste screenshot centered
    canvas_rgba = canvas.convert("RGBA")
    x = (canvas_w - shot.width) // 2
    y = (canvas_h - shot.height) // 2
    canvas_rgba.paste(shot, (x, y), shot)

    canvas_rgba.convert("RGB").save(output_path, "PNG")


def ensure_vertical(image_path: str, output_path: str | None = None) -> str:
    """
    Ensure image is 1080×1920 (vertical/portrait).
    If not, wrap it in a canvas. Returns the output path.
    """
    out = output_path or image_path
    img = Image.open(image_path)
    if img.size == (CANVAS_W, CANVAS_H):
        if out != image_path:
            img.save(out, "PNG")
        return out
    process_screenshot(image_path, out)
    return out
