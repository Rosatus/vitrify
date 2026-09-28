# vitrify

Bake a Nebula-terminal-style wallpaper into a single image that reproduces the
same look — dimmed fit, frosted grain, chrome scrim — in **any** terminal that
supports a custom background image (kitty, WezTerm, Windows Terminal,
Alacritty, …), with zero per-terminal tuning.

The pipeline is extracted line-by-line from the Nebula terminal's wallpaper
renderer (bounded decode → fit/alignment layout → opacity fade → scrim →
optional static frost). The full derivation spec lives in
[docs/pipeline.md](docs/pipeline.md) (中文).

## Install

```bash
uv sync                       # deps: Pillow only
uv run vitrify --help         # or: uv run python -m vitrify
pipx install .                # or `pip install .` for a `vitrify` command
uv run python -m unittest discover -s tests   # run the test suite
```

## Quick start

```bash
# Adaptive (recommended): keep alpha — the image fades toward the terminal's
# OWN background color, so one PNG works across themes and terminals
uv run vitrify -i wall.jpg -o bg.png --preserve-alpha

# Opaque bake: fade toward a fixed theme color (default #2e3440 = Nebula's
# stock Nord terminal background)
uv run vitrify -i wall.jpg -o bg.png --bg "#2e3440" --image-opacity 0.38

# Crop a 4:3 source to 16:9 at full resolution
uv run vitrify -i wall.jpg -o bg.png --aspect 16:9 --preserve-alpha

# Exact canvas when you want pixel-perfect control (fit/alignment apply here)
uv run vitrify -i wall.jpg -o bg.png --size 1920x1080 --fit cover

# Nebula cover-chrome look: wallpaper under a translucent shell scrim
uv run vitrify -i wall.jpg -o bg.png --preset nebula-cover --window-opacity 0.8

# Frosted texture on the image itself (blur + white tint + LCG grain)
uv run vitrify -i wall.jpg -o bg.png --preset nebula-glass --image-opacity 1.0
```

On the terminal side, set the image layout to *fill/stretch/cover* and leave
opacity/tint options at their defaults — everything is already baked in.

## Options

| Option | Default | Meaning |
|---|---|---|
| `-i` / `-o` | required | input image / output path (`.png`/`.jpg` decides format) |
| `--size WxH` | source size | exact canvas; unset = follow source resolution (≤2560px cap) |
| `--aspect W:H` | source ratio | crop to a ratio at layout resolution, e.g. `16:9` |
| `--fit` | `cover` | `fill`/`uniform`(contain)/`cover`/`none`(native) |
| `--align` | `center` | 3×3 anchors `top-left` … `bottom-right` |
| `--image-opacity` | `0.38` | fade strength (Nebula's persisted default) |
| `--bg` | `#2e3440` | plate color for the opaque bake; also JPEG flatten base |
| `--preserve-alpha` | off | keep `src_alpha × image_opacity`; terminal's own bg does the fade |
| `--mode` | `card` | `card` or `cover` (cover adds the shell scrim) |
| `--window-opacity` | `1.0` | drives cover scrim `min(op, 0.78)` |
| `--scrim` / `--scrim-alpha` | bg / auto | veil color (default = `--bg`) and strength |
| `--blur-sigma` | `0` | Gaussian blur applied to the image itself |
| `--tint-alpha` | `0` | white veil over the image (dark theme: 0.08, light: 0.05) |
| `--noise-alpha` | `0` | deterministic LCG grain (Nebula's recipe: 14/255 ≈ 0.055) |
| `--format` | by extension | `png`/`jpeg` (JPEG always flattens onto `--bg`) |
| `--no-bound`, `--max-*` | Nebula values | disable/tune decode budgets (64MiB/128MiB/2048px/8MiB/2560px) |
| `--preset` | `nebula-card` | `nebula-card` / `nebula-cover` / `nebula-glass` baselines |
| `--print-params` | — | print the resolved parameter set without writing |

Explicit flags always override the preset; presets only provide baselines.

## How it looks (composition model)

Bottom → top, mirroring Nebula's render order:

```
L0  OS window material (live blur)   — not bakeable, deliberately out of scope
L1  plate    theme background color  (transparent under --preserve-alpha)
L2  image    fitted wallpaper × image_opacity
L3  scrim    cover mode only: shell color × min(window_opacity, 0.78)
L4  content  your terminal's text stays opaque on top
```

`--preserve-alpha` is the more faithful port: the baked pixel alpha is
`src_alpha × image_opacity`, so the terminal composites the image over **its
own** background — Nebula's exact formula `img·o + bg·(1−o)` with `bg` supplied
by the terminal theme instead of a guessed hex. Requires a terminal that
composites PNG alpha correctly (kitty, WezTerm, Windows Terminal all do; if
not, the image shows at full RGB strength — fall back to the opaque bake).

## Notes

- Real-time blur of content *behind* the window (Mica/Acrylic/CGS/KWin) is an
  OS compositor feature and cannot be baked into a PNG — `nebula-glass`
  simulates a *static* frosted texture on the image itself instead.
- Decode budgets mirror Nebula's loader: file ≤64MiB, decode ≤128MiB,
  texture ≤2048px/8MiB, layout ≤2560px; `--no-bound` disables them.
- Grain uses the same seeded LCG (`0x9E3779B9`) as the source — identical
  input produces byte-identical speckle.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

---

## 中文速览

把 Nebula 终端的壁纸视觉管线离线烘焙成一张普通图片，丢进任何支持自定义
背景的终端即复刻同款观感，无需逐软件调参。

```bash
uv run vitrify -i 原图 -o out.png --preserve-alpha   # 推荐：alpha 自适应终端底色
uv run vitrify -i 原图 -o out.png --bg "#2e3440"      # 保守：不透明烘焙(Nord 底色)
uv run vitrify -i 原图 -o out.png --aspect 16:9       # 按目标比例裁剪
uv run vitrify -i 原图 -o out.png --preset nebula-glass  # 磨砂质感
```

终端侧把背景图设为 fill/stretch，透明度与 tint 参数留默认即可。完整提炼
规范与像素公式见 [docs/pipeline.md](docs/pipeline.md)。
