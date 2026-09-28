"""Port-parity tests — expected values mirror renderer/image.rs unit tests."""

import unittest

from PIL import Image

from glass.pipeline import (
    NOISE_ALPHA,
    Alignment,
    Bounds,
    Fit,
    Params,
    load_bounded,
    parse_hex_rgb,
    render,
    wallpaper_rect,
)


class WallpaperRectTests(unittest.TestCase):
    def test_uniform_to_fill_crops_overflow(self):
        # mirror: uniform_to_fill_crops_and_honors_alignment (image.rs tests)
        self.assertEqual(
            wallpaper_rect(1000, 500, 400, 400, Fit.UNIFORM_TO_FILL, Alignment.CENTER),
            (0.0, -250.0, 1000.0, 1000.0),
        )
        self.assertEqual(
            wallpaper_rect(1000, 500, 400, 400, Fit.UNIFORM_TO_FILL, Alignment.RIGHT),
            (0.0, -250.0, 1000.0, 1000.0),
        )

    def test_uniform_contain_and_bottom_right_anchor(self):
        self.assertEqual(
            wallpaper_rect(1000, 500, 400, 400, Fit.UNIFORM, Alignment.BOTTOM_RIGHT),
            (500.0, 0.0, 500.0, 500.0),
        )

    def test_native_size_uses_nine_anchors(self):
        self.assertEqual(
            wallpaper_rect(1000, 500, 200, 100, Fit.NONE, Alignment.TOP_LEFT),
            (0.0, 0.0, 200.0, 100.0),
        )
        self.assertEqual(
            wallpaper_rect(1000, 500, 200, 100, Fit.NONE, Alignment.CENTER),
            (400.0, 200.0, 200.0, 100.0),
        )
        self.assertEqual(
            wallpaper_rect(1000, 500, 200, 100, Fit.NONE, Alignment.BOTTOM_RIGHT),
            (800.0, 400.0, 200.0, 100.0),
        )

    def test_persisted_aliases_parse(self):
        self.assertEqual(Fit.parse("UniformToFill"), Fit.UNIFORM_TO_FILL)
        self.assertEqual(Fit.parse("contain"), Fit.UNIFORM)
        self.assertEqual(Alignment.parse("BOTTOM_RIGHT"), Alignment.BOTTOM_RIGHT)


class CompositeTests(unittest.TestCase):
    def solid(self, rgb, size=(8, 8)):
        return Image.new("RGBA", size, (*rgb, 255))

    def test_card_mode_blends_toward_bg(self):
        # image 100,100,100 at opacity 0.5 over bg 0,0,0 → ~50,50,50 opaque.
        params = Params(size=(8, 8), fit=Fit.FILL, image_opacity=0.5, bg=(0, 0, 0))
        out = render(params, self.solid((100, 100, 100)), 8, 8)
        self.assertEqual(out.getpixel((4, 4)), (50, 50, 50))
        self.assertEqual(out.mode, "RGB")

    def test_cover_mode_adds_scrim(self):
        # img 255 at o_i=1 → 255; scrim 0 at s=0.78 → 255*(1-0.78)=56.
        params = Params(
            size=(8, 8),
            fit=Fit.FILL,
            image_opacity=1.0,
            bg=(0, 0, 0),
            mode="cover",
            window_opacity=1.0,
            scrim=(0, 0, 0),
        )
        out = render(params, self.solid((255, 255, 255)), 8, 8)
        self.assertEqual(out.getpixel((4, 4)), (56, 56, 56))

    def test_contain_letterbox_shows_bg(self):
        # 8x8 red image, contain into 8x4 canvas → side pillars show bg.
        params = Params(
            size=(8, 4), fit=Fit.UNIFORM, image_opacity=1.0, bg=(0, 0, 255)
        )
        out = render(params, self.solid((255, 0, 0)), 8, 8)
        self.assertEqual(out.getpixel((0, 2)), (0, 0, 255))
        self.assertEqual(out.getpixel((4, 2)), (255, 0, 0))

    def test_opacity_zero_paints_nothing(self):
        params = Params(size=(4, 4), image_opacity=0.0, bg=(1, 2, 3))
        out = render(params, self.solid((255, 255, 255)), 4, 4)
        self.assertEqual(out.getpixel((0, 0)), (1, 2, 3))

    def test_transparent_source_pixels_blend_over_bg(self):
        # PNG alpha participates: a 50%-alpha white pixel at o_i=1 over black
        # lands mid-gray, and the output still flattens to opaque RGB.
        src = Image.new("RGBA", (4, 4), (255, 255, 255, 128))
        params = Params(size=(4, 4), fit=Fit.FILL, image_opacity=1.0, bg=(0, 0, 0))
        out = render(params, src, 4, 4)
        r, g, b = out.getpixel((0, 0))
        self.assertTrue(125 <= r <= 130)
        self.assertEqual(out.mode, "RGB")

    def test_preserve_alpha_keeps_scaled_alpha_and_clear_letterbox(self):
        # a = src_a × o_i (200×0.5 ≈ 100); off-image area stays transparent so
        # the terminal's own bg fills the fade — the adaptive equivalent of
        # blending toward --bg.
        params = Params(
            size=(4, 4), fit=Fit.FILL, image_opacity=0.5, preserve_alpha=True
        )
        src = Image.new("RGBA", (4, 4), (255, 255, 255, 200))
        out = render(params, src, 4, 4)
        self.assertEqual(out.mode, "RGBA")
        a = out.getpixel((0, 0))[3]
        self.assertTrue(98 <= a <= 102)
        # contain into a wider canvas → side pillar alpha 0.
        wide = Params(
            size=(8, 4), fit=Fit.UNIFORM, image_opacity=1.0, preserve_alpha=True
        )
        out = render(wide, Image.new("RGBA", (8, 8), (9, 9, 9, 255)), 8, 8)
        self.assertEqual(out.getpixel((0, 2))[3], 0)

    def test_default_canvas_follows_layout_and_aspect_crops(self):
        from glass.pipeline import canvas_size

        params = Params()
        self.assertEqual(canvas_size(params, 1600, 900), (1600, 900))
        params = Params(aspect=(16, 9))
        self.assertEqual(canvas_size(params, 1600, 1200), (1600, 900))
        params = Params(aspect=(1, 1))
        self.assertEqual(canvas_size(params, 1600, 900), (900, 900))
        params = Params(size=(640, 360), aspect=(1, 1))
        self.assertEqual(canvas_size(params, 1600, 900), (640, 360))

    def test_parse_hex(self):
        self.assertEqual(parse_hex_rgb("#1a2b3c"), (26, 43, 60))
        self.assertEqual(parse_hex_rgb("fff"), (255, 255, 255))


class BoundsTests(unittest.TestCase):
    def make_png(self, path, size, rgb=(10, 20, 30)):
        Image.new("RGBA", size, (*rgb, 255)).save(path)

    def test_bounded_shrink_and_layout_edge(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.png"
            # 3000x1000 → texture long edge 2048, layout long edge 2560.
            self.make_png(p, (3000, 1000))
            bounds = Bounds()
            img, lw, lh = load_bounded(p, bounds)
            self.assertEqual(img.size, (2048, 682))
            self.assertEqual((lw, lh), (2560, 853))

    def test_too_many_pixels_rejected(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "huge.png"
            self.make_png(p, (6000, 6000))  # 144MB RGBA > 128MB decode budget
            with self.assertRaises(ValueError):
                load_bounded(p, Bounds())

    def test_unbounded_allows(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.png"
            self.make_png(p, (100, 50))
            img, lw, lh = load_bounded(p, Bounds(bounded=False))
            self.assertEqual(img.size, (100, 50))
            self.assertEqual((lw, lh), (100, 50))


if __name__ == "__main__":
    unittest.main()
