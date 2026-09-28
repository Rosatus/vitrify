"""glass — bake a Nebula-style wallpaper into a single image file.

Produces one PNG/JPEG that already contains fit/alignment, opacity blending,
the frosted-glass recipe and the chrome scrim — so any terminal that can set
a background image reproduces the look with zero per-app tuning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import UnidentifiedImageError

from .pipeline import (
    NOISE_ALPHA,
    TINT_ALPHA_DARK,
    Alignment,
    Bounds,
    Fit,
    Params,
    parse_hex_rgb,
    process,
)

# preset name → fields that differ from the hard defaults in Params.
# Faithful to the source: card = wallpaper over theme bg at 0.38; cover adds
# the shell scrim (min(window_opacity, 0.78)); glass applies the retired
# in-app overlay recipe (blur + white tint + deterministic noise).
PRESETS: dict[str, dict] = {
    "nebula-card": {},
    "nebula-cover": {"mode": "cover"},
    "nebula-glass": {
        "blur_sigma": 30.0,
        "tint_alpha": TINT_ALPHA_DARK,
        "noise_alpha": NOISE_ALPHA,
    },
}


def _size(text: str) -> tuple[int, int]:
    for sep in ("x", ","):
        if sep in text:
            w, h = text.lower().split(sep, 1)
            w, h = int(w), int(h)
            if w <= 0 or h <= 0:
                break
            return w, h
    raise argparse.ArgumentTypeError(f"expected WxH, got {text!r}")


def _aspect(text: str) -> tuple[int, int]:
    for sep in (":", "x", "/"):
        if sep in text:
            a, b = text.lower().split(sep, 1)
            a, b = int(a), int(b)
            if a <= 0 or b <= 0:
                break
            return a, b
    raise argparse.ArgumentTypeError(f"expected W:H, got {text!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="glass",
        description="Bake a Nebula-style wallpaper into one image for any terminal.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", required=True, type=Path, help="source image path")
    p.add_argument("-o", "--output", required=True, type=Path, help="output image path")
    p.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="nebula-card",
        help="baseline parameter set; explicit flags override it",
    )
    p.add_argument(
        "--size",
        type=_size,
        default=None,
        help="exact canvas WxH; default = source layout resolution (no resampling)",
    )
    p.add_argument(
        "--aspect",
        type=_aspect,
        default=None,
        help="crop to a W:H ratio at layout resolution (e.g. 16:9); ignored if --size set",
    )
    p.add_argument(
        "--fit",
        default=None,
        help="fill|stretch | uniform|contain | uniform_to_fill|cover | none|native",
    )
    p.add_argument(
        "--align",
        default=None,
        help="top-left|top|top-right|left|center|right|bottom-left|bottom|bottom-right",
    )
    p.add_argument(
        "--image-opacity",
        type=float,
        default=None,
        help="wallpaper opacity blended toward --bg (Nebula default: 0.38)",
    )
    p.add_argument(
        "--bg",
        default=None,
        help="theme background the image fades toward, #rrggbb (default: #2e3440, Nebula Nord)",
    )
    p.add_argument("--mode", choices=["card", "cover"], default=None)
    p.add_argument(
        "--window-opacity",
        type=float,
        default=None,
        help="drives the cover-mode scrim: min(window_opacity, 0.78)",
    )
    p.add_argument("--scrim", default=None, help="chrome veil color, #rrggbb (default: --bg)")
    p.add_argument(
        "--scrim-alpha",
        type=float,
        default=None,
        help="explicit veil alpha; default in cover mode = min(window_opacity, 0.78)",
    )
    p.add_argument("--blur-sigma", type=float, default=None, help="frost blur applied to the image")
    p.add_argument("--tint-alpha", type=float, default=None, help="white veil over the image, 0..1")
    p.add_argument("--noise-alpha", type=float, default=None, help="grain over the image, 0..1")
    p.add_argument(
        "--preserve-alpha",
        action="store_true",
        default=None,
        help="keep src alpha × image_opacity; the terminal's own bg fills the fade",
    )
    p.add_argument("--format", choices=["png", "jpeg"], default=None)
    p.add_argument("--jpeg-quality", type=int, default=92)
    # Loader budgets (Bounds).
    p.add_argument("--no-bound", action="store_true", help="disable admission/size budgets")
    p.add_argument("--max-file-mb", type=float, default=None)
    p.add_argument("--max-decode-mb", type=float, default=None)
    p.add_argument("--tex-edge", type=int, default=None)
    p.add_argument("--tex-mb", type=float, default=None)
    p.add_argument("--layout-edge", type=int, default=None)
    p.add_argument(
        "--print-params", action="store_true", help="dump the resolved parameter set and exit"
    )
    return p


def resolve_params(args: argparse.Namespace) -> Params:
    preset = PRESETS[args.preset]

    def pick(name: str, default):
        cli_value = getattr(args, name)
        return cli_value if cli_value is not None else preset.get(name, default)

    bounds = Bounds(bounded=not args.no_bound)
    if args.max_file_mb is not None:
        bounds.max_file_bytes = int(args.max_file_mb * 1024 * 1024)
    if args.max_decode_mb is not None:
        bounds.max_decode_bytes = int(args.max_decode_mb * 1024 * 1024)
    if args.tex_edge is not None:
        bounds.max_image_edge = args.tex_edge
    if args.tex_mb is not None:
        bounds.max_image_bytes = int(args.tex_mb * 1024 * 1024)
    if args.layout_edge is not None:
        bounds.layout_edge = args.layout_edge

    params = Params(
        size=pick("size", None),
        aspect=pick("aspect", None),
        fit=Fit.parse(pick("fit", "uniform_to_fill")),
        alignment=Alignment.parse(pick("align", "center")),
        image_opacity=pick("image_opacity", 0.38),
        bg=parse_hex_rgb(pick("bg", "#2e3440")),
        mode=pick("mode", "card"),
        window_opacity=pick("window_opacity", 1.0),
        scrim=parse_hex_rgb(args.scrim) if args.scrim else None,
        scrim_alpha=args.scrim_alpha,
        blur_sigma=pick("blur_sigma", 0.0),
        tint_alpha=pick("tint_alpha", 0.0),
        noise_alpha=pick("noise_alpha", 0.0),
        preserve_alpha=bool(args.preserve_alpha),
        bounds=bounds,
    )
    return params


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        params = resolve_params(args)
    except ValueError as exc:
        print(f"glass: {exc}", file=sys.stderr)
        return 2

    if args.print_params:
        print(params)
        return 0

    fmt = args.format
    if fmt is None:
        fmt = "jpeg" if args.output.suffix.lower() in (".jpg", ".jpeg") else "png"

    try:
        report = process(args.input, args.output, params, fmt, args.jpeg_quality)
    except (ValueError, OSError, UnidentifiedImageError) as exc:
        print(f"glass: {exc}", file=sys.stderr)
        return 1

    print(
        f"glass: {report['texture'][0]}x{report['texture'][1]} texture "
        f"(layout {report['layout'][0]}x{report['layout'][1]}) "
        f"→ {report['output'][0]}x{report['output'][1]} {report['path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
