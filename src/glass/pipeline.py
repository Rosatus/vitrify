"""Core image pipeline — a faithful, offline port of Nebula's wallpaper path.

Source of truth (pebrel repo):
  - layout math:   nebula_app/src/renderer/image_layout.rs  (wallpaper_rect)
  - bounds:        nebula_app/src/gpui_shell/wallpaper/image_loader.rs
  - compositing:   nebula_app/src/gpui_shell/wallpaper.rs   (layer/paint)
  - scrim recipe:  wallpaper.rs::chrome_surface_opacity     (min(op, 0.78))
  - glass overlay: wallpaper.rs::paint_glass_overlay        (retired recipe)

Everything is plain Pillow + stdlib; no runtime window/compositor involved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image, ImageFilter

# ---------------------------------------------------------------------------
# Bounds (image_loader.rs): owned-resource limits of the *loader*, not process
# guarantees. Defaults mirror the source constants.
# ---------------------------------------------------------------------------
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DECODE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_EDGE = 32768
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_EDGE = 2048
LAYOUT_EDGE = 2560

NOISE_TILE_PX = 512
NOISE_ALPHA = 14 / 255.0  # source: NOISE_ALPHA: u8 = 14
TINT_ALPHA_DARK = 0.08
TINT_ALPHA_LIGHT = 0.05

SCRIM_CAP = 0.78  # chrome_surface_opacity: min(user_opacity, 0.78)


class Fit(Enum):
    """BackgroundImageFit — names accept the same aliases as Fit::parse."""

    FILL = "fill"            # distort to canvas (CSS `fill`)
    UNIFORM = "uniform"      # contain — whole image visible, letterboxed
    UNIFORM_TO_FILL = "uniform_to_fill"  # cover — crop overflow (default)
    NONE = "none"            # native layout size, no scaling

    @classmethod
    def parse(cls, value: str) -> "Fit":
        match value.strip().lower():
            case "fill" | "stretch":
                return cls.FILL
            case "uniform" | "contain":
                return cls.UNIFORM
            case "uniform_to_fill" | "uniformtofill" | "cover":
                return cls.UNIFORM_TO_FILL
            case "none" | "native":
                return cls.NONE
        raise ValueError(f"unknown fit: {value!r}")


class Alignment(Enum):
    TOP_LEFT = "top_left"
    TOP = "top"
    TOP_RIGHT = "top_right"
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM = "bottom"
    BOTTOM_RIGHT = "bottom_right"

    @classmethod
    def parse(cls, value: str) -> "Alignment":
        key = value.strip().lower().replace("_", "-")
        table = {
            "top-left": cls.TOP_LEFT,
            "top": cls.TOP,
            "top-right": cls.TOP_RIGHT,
            "left": cls.LEFT,
            "center": cls.CENTER,
            "centre": cls.CENTER,
            "right": cls.RIGHT,
            "bottom-left": cls.BOTTOM_LEFT,
            "bottom": cls.BOTTOM,
            "bottom-right": cls.BOTTOM_RIGHT,
        }
        if key in table:
            return table[key]
        raise ValueError(f"unknown alignment: {value!r}")

    @property
    def factors(self) -> tuple[float, float]:
        return {
            Alignment.TOP_LEFT: (0.0, 0.0),
            Alignment.TOP: (0.5, 0.0),
            Alignment.TOP_RIGHT: (1.0, 0.0),
            Alignment.LEFT: (0.0, 0.5),
            Alignment.CENTER: (0.5, 0.5),
            Alignment.RIGHT: (1.0, 0.5),
            Alignment.BOTTOM_LEFT: (0.0, 1.0),
            Alignment.BOTTOM: (0.5, 1.0),
            Alignment.BOTTOM_RIGHT: (1.0, 1.0),
        }[self]


def wallpaper_rect(
    win_w: float,
    win_h: float,
    img_w: float,
    img_h: float,
    fit: Fit,
    alignment: Alignment,
) -> tuple[float, float, float, float]:
    """Port of image_layout.rs::wallpaper_rect. Returns (x, y, w, h); x/y may
    be negative (cover crops by overflowing past the canvas)."""
    win_w = max(win_w, 1.0)
    win_h = max(win_h, 1.0)
    img_w = max(img_w, 1.0)
    img_h = max(img_h, 1.0)

    match fit:
        case Fit.FILL:
            draw_w, draw_h = win_w, win_h
        case Fit.UNIFORM:
            scale = min(win_w / img_w, win_h / img_h)
            draw_w, draw_h = img_w * scale, img_h * scale
        case Fit.UNIFORM_TO_FILL:
            scale = max(win_w / img_w, win_h / img_h)
            draw_w, draw_h = img_w * scale, img_h * scale
        case Fit.NONE:
            draw_w, draw_h = img_w, img_h

    fx, fy = alignment.factors
    return (win_w - draw_w) * fx, (win_h - draw_h) * fy, draw_w, draw_h


# ---------------------------------------------------------------------------
# Parameter model
# ---------------------------------------------------------------------------


def parse_hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        raise ValueError(f"expected #rrggbb, got {value!r}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


@dataclass
class Bounds:
    """Loader budgets; set bounded=False to disable admission checks."""

    bounded: bool = True
    max_file_bytes: int = MAX_FILE_BYTES
    max_decode_bytes: int = MAX_DECODE_BYTES
    max_source_edge: int = MAX_SOURCE_EDGE
    max_image_bytes: int = MAX_IMAGE_BYTES
    max_image_edge: int = MAX_IMAGE_EDGE
    layout_edge: int = LAYOUT_EDGE


@dataclass
class Params:
    # Output canvas. None = follow the source's layout resolution (recommended:
    # no resampling, the terminal scales it); `aspect` crops to a target ratio
    # at layout resolution; `size` renders an exact WxH canvas.
    size: tuple[int, int] | None = None
    aspect: tuple[int, int] | None = None
    fit: Fit = Fit.UNIFORM_TO_FILL
    alignment: Alignment = Alignment.CENTER
    # Wallpaper's own opacity; 0.38 is Nebula's persisted default.
    image_opacity: float = 0.38
    # Theme background the image fades toward (card mode) / sits on (cover).
    # Default = Nebula's stock Nord palette: TermTheme.background /
    # ReviewedPalette.shell = 0x2e3440 (nord0).
    bg: tuple[int, int, int] = (0x2E, 0x34, 0x40)
    mode: str = "card"  # "card" | "cover"
    # Window opacity only drives the cover-mode scrim: min(op, 0.78).
    window_opacity: float = 1.0
    scrim: tuple[int, int, int] | None = None  # default = bg (Nord shell = #2e3440)
    scrim_alpha: float | None = None  # default: cover → min(win_op, 0.78)
    # Frosted-glass simulation applied to the image itself (static bake):
    blur_sigma: float = 0.0
    tint_alpha: float = 0.0
    noise_alpha: float = 0.0
    # Keep per-pixel alpha (src_a × image_opacity; transparent letterbox) so the
    # terminal blends the image over *its own* background — Nebula's fade math
    # without guessing --bg. Requires a terminal that honors PNG alpha.
    preserve_alpha: bool = False
    bounds: Bounds = field(default_factory=Bounds)


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def load_bounded(path: Path, bounds: Bounds) -> tuple[Image.Image, int, int]:
    """Decode → admission checks → bounded thumbnail → RGBA.

    Returns (texture_rgba, layout_w, layout_h). layout_* preserves the
    source's native-fit geometry independently of texture resolution.
    """
    file_bytes = path.stat().st_size
    if bounds.bounded and file_bytes > bounds.max_file_bytes:
        raise ValueError(
            f"file too large: {file_bytes} bytes > {bounds.max_file_bytes} (MAX_FILE_BYTES)"
        )

    raw = Image.open(path)
    src_w, src_h = raw.size
    if src_w == 0 or src_h == 0:
        raise ValueError("image has zero-sized dimension")
    if bounds.bounded:
        if src_w > bounds.max_source_edge or src_h > bounds.max_source_edge:
            raise ValueError(f"source edge {src_w}x{src_h} exceeds {bounds.max_source_edge}")
        if src_w * src_h * 4 > bounds.max_decode_bytes:
            raise ValueError(
                f"decoded RGBA would exceed {bounds.max_decode_bytes} bytes (MAX_DECODE_BYTES)"
            )

    # Layout geometry uses the *original* dimensions, capped at layout_edge —
    # identical to layout_scale in image_loader.rs.
    layout_scale = min(bounds.layout_edge / max(src_w, src_h), 1.0)
    layout_w = max(1, round(src_w * layout_scale))
    layout_h = max(1, round(src_h * layout_scale))

    # bounded_dimensions: satisfy BOTH the edge cap and the byte cap.
    pixels = float(src_w) * float(src_h)
    scale = min(
        bounds.max_image_edge / max(src_w, src_h),
        math.sqrt(bounds.max_image_bytes / (pixels * 4.0)),
        1.0,
    ) if bounds.bounded else 1.0
    tex_w = max(1, math.floor(src_w * scale))
    tex_h = max(1, math.floor(src_h * scale))

    try:
        if (tex_w, tex_h) != (src_w, src_h):
            # Shrink before RGBA conversion (source: thumbnail_exact).
            img = raw.convert("RGB").resize((tex_w, tex_h), Image.Resampling.LANCZOS)
        else:
            img = raw.convert("RGBA")
    finally:
        raw.close()
    return img.convert("RGBA"), layout_w, layout_h


def _lcg_noise_tile(size: int = NOISE_TILE_PX) -> Image.Image:
    """Deterministic LCG grayscale tile (Numerical Recipes constants), same as
    wallpaper.rs::noise_tile. 512² tiles keep the pattern identical per run."""
    buf = bytearray(size * size * 4)
    state = 0x9E3779B9
    i = 0
    for _ in range(size * size):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        luma = state >> 24
        buf[i] = luma
        buf[i + 1] = luma
        buf[i + 2] = luma
        buf[i + 3] = 255
        i += 4
    return Image.frombytes("RGBA", (size, size), bytes(buf))


def _tiled(tile: Image.Image, size: tuple[int, int]) -> Image.Image:
    out = Image.new("RGBA", size)
    tw, th = tile.size
    for y in range(0, size[1], th):
        for x in range(0, size[0], tw):
            out.paste(tile, (x, y))
    return out


def _veil_over_patch(patch: Image.Image, rgb: tuple[int, int, int], alpha: float) -> Image.Image:
    """Uniform color veil over the patch's occupied pixels only."""
    if alpha <= 0.0:
        return patch
    veil = Image.new("RGBA", patch.size, (*rgb, 0))
    mask = patch.getchannel("A").point(lambda v: round(v * alpha))
    veil.putalpha(mask)
    return Image.alpha_composite(patch, veil)


def _noise_over_patch(patch: Image.Image, noise_alpha: float) -> Image.Image:
    if noise_alpha <= 0.0:
        return patch
    noise = _tiled(_lcg_noise_tile(), patch.size)
    mask = patch.getchannel("A").point(lambda v: round(v * noise_alpha))
    noise.putalpha(mask)
    return Image.alpha_composite(patch, noise)


def canvas_size(params: Params, layout_w: int, layout_h: int) -> tuple[int, int]:
    if params.size:
        return params.size
    if params.aspect:
        aw, ah = params.aspect
        # Largest aw:ah box inside the layout frame (cover-crop to the ratio).
        scale = min(layout_w / aw, layout_h / ah)
        return max(1, round(aw * scale)), max(1, round(ah * scale))
    return layout_w, layout_h


def render(params: Params, src: Image.Image, layout_w: int, layout_h: int) -> Image.Image:
    """Composite one baked frame.

    Layer order (bottom → top), mirroring the GPUI shell:
      bg plate → fitted wallpaper (+tint/noise) → cover-mode scrim

    Default output is opaque RGB. With preserve_alpha the plate is transparent
    and the image keeps src_a × image_opacity alpha, so the terminal's own
    background fills the fade.
    """
    win_w, win_h = canvas_size(params, layout_w, layout_h)
    plate_alpha = 0 if params.preserve_alpha else 255
    canvas = Image.new("RGBA", (win_w, win_h), (*params.bg, plate_alpha))

    if params.image_opacity > 0.0:
        tex = src
        if params.blur_sigma > 0.0:
            tex = tex.filter(ImageFilter.GaussianBlur(params.blur_sigma))

        x0, y0, draw_w, draw_h = wallpaper_rect(
            win_w, win_h, layout_w, layout_h, params.fit, params.alignment
        )
        patch = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
        fitted = tex.resize(
            (max(1, round(draw_w)), max(1, round(draw_h))), Image.Resampling.LANCZOS
        )
        # Paste with clipping: negative offsets crop the overflow (cover mode).
        ix, iy = round(x0), round(y0)
        left, top = max(0, -ix), max(0, -iy)
        right = min(fitted.width, win_w - ix)
        bottom = min(fitted.height, win_h - iy)
        if right > left and bottom > top:
            patch.paste(fitted.crop((left, top, right, bottom)), (ix + left, iy + top))

        # Glass recipe order is fixed: tint under noise (source comment).
        patch = _veil_over_patch(patch, (255, 255, 255), params.tint_alpha)
        patch = _noise_over_patch(patch, params.noise_alpha)

        if params.image_opacity < 1.0:
            patch.putalpha(patch.getchannel("A").point(lambda v: round(v * params.image_opacity)))
        canvas = Image.alpha_composite(canvas, patch)

    if params.mode == "cover":
        scrim_alpha = params.scrim_alpha
        if scrim_alpha is None:
            scrim_alpha = min(params.window_opacity, SCRIM_CAP)
        if scrim_alpha > 0.0:
            scrim_rgb = params.scrim or params.bg
            veil = Image.new("RGBA", (win_w, win_h), (*scrim_rgb, round(scrim_alpha * 255)))
            canvas = Image.alpha_composite(canvas, veil)

    return canvas if params.preserve_alpha else canvas.convert("RGB")


def save(img: Image.Image, path: Path, fmt: str, bg: tuple[int, int, int], jpeg_quality: int = 92) -> None:
    if fmt == "jpeg":
        # JPEG has no alpha: flatten onto the plate color.
        plate = Image.new("RGBA", img.size, (*bg, 255))
        img = Image.alpha_composite(plate, img.convert("RGBA")).convert("RGB")
        img.save(path, "JPEG", quality=jpeg_quality)
    else:
        img.save(path, "PNG")


def process(
    input_path: Path,
    output_path: Path,
    params: Params,
    fmt: str = "png",
    jpeg_quality: int = 92,
) -> dict:
    """Full pipeline; returns a small report dict for the CLI to print."""
    src, layout_w, layout_h = load_bounded(input_path, params.bounds)
    out = render(params, src, layout_w, layout_h)
    save(out, output_path, fmt, params.bg, jpeg_quality)
    return {
        "texture": src.size,
        "layout": (layout_w, layout_h),
        "output": out.size,
        "path": str(output_path),
    }
