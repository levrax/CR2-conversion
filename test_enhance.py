# -*- coding: utf-8 -*-
"""Тесты enhance.py: автоулучшение, фильтры, запись файлов, пакетная обработка.

Только синтетические изображения: ни одной фотографии пользователя, никакой
сети.  Все файлы создаются во временных папках и удаляются после теста.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import numpy as np
    from PIL import Image, ImageOps
except ImportError as exc:  # pragma: no cover - requirements.txt lists both
    raise unittest.SkipTest("нужны numpy и Pillow: %s" % exc)

import cr2_core  # noqa: E402
import enhance  # noqa: E402
import make_test_cr2  # noqa: E402


# --------------------------------------------------------------------------
# Synthetic material
# --------------------------------------------------------------------------


def textured(h: int = 240, w: int = 360, seed: int = 1) -> np.ndarray:
    """A smooth colourful scene with texture, values spread over 0..1."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 0.5 + 0.45 * np.sin(xx / 37.0) * np.cos(yy / 23.0)
    g = 0.5 + 0.45 * np.sin((xx + yy) / 41.0)
    b = 0.5 + 0.45 * np.cos(xx / 29.0 - yy / 53.0)
    img = np.stack([r, g, b], axis=2) + rng.normal(0, 0.02, (h, w, 3))
    return np.clip(img, 0, 1).astype(np.float32)


def dark_scene(h: int = 300, w: int = 400) -> np.ndarray:
    """An under-exposed frame: like the team's dark raw render (mean ~ 60/255)."""
    return (textured(h, w, seed=2) ** 1.6 * 0.42).astype(np.float32)


def luma(a: np.ndarray) -> np.ndarray:
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def clipped(a: np.ndarray) -> float:
    return float(np.mean(luma(a) >= 250 / 255))


def tiff_exif(orientation: int, px: int, py: int, *, thumb: bytes = b"THUMB" * 20,
              makernote: bytes = b"MN\x00\x01" + bytes(range(60))) -> bytes:
    """b'Exif\\0\\0' + a little-endian TIFF: IFD0 -> ExifIFD, and IFD1 with a thumbnail."""
    make = b"Canon\x00"
    dto = b"2020:01:01 12:00:00\x00"
    # Layout: header(8) | IFD0 (3 entries) | IFD1 (2 entries) | ExifIFD (4 entries) | data
    ifd0_off = 8
    ifd0_len = 2 + 3 * 12 + 4
    ifd1_off = ifd0_off + ifd0_len
    ifd1_len = 2 + 2 * 12 + 4
    exif_off = ifd1_off + ifd1_len
    exif_len = 2 + 4 * 12 + 4
    data_off = exif_off + exif_len
    make_off = data_off
    dto_off = make_off + len(make)
    mn_off = dto_off + len(dto)
    thumb_off = mn_off + len(makernote)

    def ent(tag, typ, count, value: bytes) -> bytes:
        return struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\x00")

    out = bytearray(b"II*\x00" + struct.pack("<I", ifd0_off))
    out += struct.pack("<H", 3)
    out += ent(0x010F, 2, len(make), struct.pack("<I", make_off))
    out += ent(0x0112, 3, 1, struct.pack("<H", orientation))
    out += ent(0x8769, 4, 1, struct.pack("<I", exif_off))
    out += struct.pack("<I", ifd1_off)
    out += struct.pack("<H", 2)
    out += ent(0x0201, 4, 1, struct.pack("<I", thumb_off))
    out += ent(0x0202, 4, 1, struct.pack("<I", len(thumb)))
    out += struct.pack("<I", 0)
    out += struct.pack("<H", 4)
    out += ent(0x9003, 2, len(dto), struct.pack("<I", dto_off))
    out += ent(0x927C, 7, len(makernote), struct.pack("<I", mn_off))
    out += ent(0xA002, 3, 1, struct.pack("<H", px))
    out += ent(0xA003, 3, 1, struct.pack("<H", py))
    out += struct.pack("<I", 0)
    assert len(out) == data_off
    out += make + dto + makernote + thumb
    return b"Exif\x00\x00" + bytes(out)


def marker_image(w: int = 96, h: int = 64) -> Image.Image:
    """Grey frame with a red block in the TOP-LEFT corner, to track rotations."""
    a = np.full((h, w, 3), 128, dtype=np.uint8)
    a[: h // 3, : w // 3] = (220, 30, 30)
    return Image.fromarray(a, "RGB")


def opened(path: Path) -> Image.Image:
    """Image.open, fully loaded and with the file handle closed again."""
    with Image.open(path) as im:
        im.load()
    return im


def red_corner(im: Image.Image) -> tuple[bool, bool]:
    """(is the red block at the top?, is it at the left?)"""
    a = np.asarray(im.convert("RGB"), dtype=np.float32)
    h, w = a.shape[:2]
    red = (a[..., 0] - a[..., 1]) > 80
    ys, xs = np.nonzero(red)
    return bool(ys.mean() < h / 2), bool(xs.mean() < w / 2)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="enhance_test_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.src_dir = self.root / "исходники"
        self.out_dir = self.root / "результат"
        self.src_dir.mkdir()

    def write_jpeg(self, name: str, im: Image.Image | None = None, *,
                   exif: bytes | None = None, folder: Path | None = None) -> Path:
        path = (folder or self.src_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        im = im if im is not None else Image.fromarray(
            (textured(64, 96) * 255).astype(np.uint8), "RGB")
        kw = {"quality": 95}
        if exif is not None:
            kw["exif"] = exif
        im.save(path, "JPEG", **kw)
        return path


# --------------------------------------------------------------------------
# auto_enhance
# --------------------------------------------------------------------------


def curtain_with_face(h: int = 300, w: int = 400) -> tuple[np.ndarray, tuple[slice, slice]]:
    """A lit face (~2 % of the frame) against a dark red curtain."""
    rng = np.random.default_rng(7)
    _yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    fold = 0.5 + 0.5 * np.sin(xx / 9.0)
    img = np.stack([0.16 + 0.10 * fold, 0.05 + 0.03 * fold, 0.05 + 0.03 * fold], axis=2)
    face = (slice(110, 170), slice(180, 225))
    shade = np.linspace(1.0, 0.75, 45, dtype=np.float32)[None, :]
    img[face] = np.stack([0.80 * shade, 0.56 * shade, 0.45 * shade], axis=2) \
        + rng.normal(0, 0.01, (60, 45, 3))
    img = img + rng.normal(0, 0.004, img.shape)
    return np.clip(img, 0, 1).astype(np.float32), face


class TestAutoEnhance(unittest.TestCase):

    def test_a_lit_face_on_a_dark_background_is_not_blown(self):
        img, face = curtain_with_face()

        def blown(a: np.ndarray) -> float:
            return float((a[face].max(axis=2) >= 250 / 255).mean())

        out = enhance.auto_enhance(img, 0.6)
        self.assertLessEqual(blown(out) - blown(img), 0.02)
        self.assertGreater(luma(out).mean(), luma(img).mean() * 1.5, "кадр всё равно светлеет")
        # The skin cap is what does it: without it the face core blows out.
        with mock.patch.object(enhance, "_SKIN_CLIP_ADD_MAX", 1.0):
            self.assertGreater(blown(enhance.auto_enhance(img, 0.6)), 0.2)

    def test_strength_zero_is_exact_identity(self):
        for img in (textured(), dark_scene()):
            for s in (0.0, -0.5):
                out = enhance.auto_enhance(img, s)
                self.assertEqual(out.dtype, np.float32)
                self.assertTrue(np.array_equal(out, img))
                self.assertIsNot(out, img)

    def test_output_in_range_dtype_and_input_untouched(self):
        rng = np.random.default_rng(3)
        cases = [textured(), dark_scene(), np.zeros((50, 70, 3), np.float32),
                 np.ones((50, 70, 3), np.float32),
                 rng.random((123, 77, 3), dtype=np.float32)]
        sat = np.zeros((40, 40, 3), np.float32)
        sat[..., 0] = 1.0
        sat[20:, :, 2] = 0.9
        cases.append(sat)
        for img in cases:
            before = img.copy()
            for s in (0.3, 0.6, 1.0):
                out = enhance.auto_enhance(img, s)
                self.assertEqual(out.dtype, np.float32)
                self.assertEqual(out.shape, img.shape)
                self.assertTrue(np.isfinite(out).all())
                self.assertGreaterEqual(float(out.min()), 0.0)
                self.assertLessEqual(float(out.max()), 1.0)
            self.assertTrue(np.array_equal(img, before))

    def test_float64_input_is_accepted_and_bad_shape_rejected(self):
        out = enhance.auto_enhance(textured().astype(np.float64), 0.6)
        self.assertEqual(out.dtype, np.float32)
        with self.assertRaises(ValueError):
            enhance.auto_enhance(np.zeros((10, 10), np.float32))

    def test_dark_scene_gets_brighter_without_blowing_highlights(self):
        img = dark_scene()
        img[10:40, 10:60] = 0.93            # a lit face / shirt: already bright
        out = enhance.auto_enhance(img, 0.6)
        self.assertGreater(luma(out).mean(), luma(img).mean() * 1.3)
        self.assertLessEqual(clipped(out) - clipped(img), enhance._CLIP_ADD_MAX + 0.001)
        # The bright patch stays below white: highlights are protected.
        self.assertLess(float(luma(out[10:40, 10:60]).mean()), 250 / 255)

    def test_clip_cap_holds_on_a_frame_full_of_near_whites(self):
        # Walls at 0.88 / 0.92 are exactly what an uncapped stretch + lift pushes
        # to >= 250: without the back-off 25 % (or 2 %) of the frame is blown.
        for level, share in ((0.88, 0.25), (0.92, 0.02), (0.95, 0.02)):
            img = dark_scene()
            img[: int(img.shape[0] * share)] = level
            before = clipped(img)
            for s in (0.6, 1.0):
                with self.subTest(level=level, share=share, strength=s):
                    out = enhance.auto_enhance(img, s)
                    self.assertLessEqual(clipped(out) - before, enhance._CLIP_ADD_MAX + 0.001)
                    self.assertGreater(luma(out).mean(), luma(img).mean())

    def test_strength_scales_smoothly(self):
        img = dark_scene()
        means = [float(luma(enhance.auto_enhance(img, s)).mean())
                 for s in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
        for a, b in zip(means, means[1:]):
            self.assertGreaterEqual(b, a - 1e-4)
        tiny = enhance.auto_enhance(img, 0.01)
        self.assertLess(float(np.abs(tiny - img).max()), 0.03)

    def test_low_key_scene_is_not_stretched_to_flat_grey(self):
        img = np.full((300, 400, 3), 0.02, np.float32)
        img += np.random.default_rng(4).normal(0, 0.005, img.shape).astype(np.float32)
        img[120:180, 170:230] = (0.85, 0.7, 0.6)   # a lit speaker on a dark stage
        img = np.clip(img, 0, 1)
        out = enhance.auto_enhance(img, 1.0)
        bg = luma(out)[:100]
        self.assertLess(float(bg.mean()), 0.08)     # the stage stays dark
        self.assertGreater(float(np.std(luma(out))), float(np.std(luma(img))) * 0.9)

    def test_warm_scene_stays_warm(self):
        img = textured() * 0.5 + 0.2
        img[..., 0] *= 1.25
        img[..., 2] *= 0.65
        img = np.clip(img, 0, 1).astype(np.float32)
        out = enhance.auto_enhance(img, 1.0, vibrance=False)
        rb_in = img[..., 0].mean() / img[..., 2].mean()
        rb_out = out[..., 0].mean() / out[..., 2].mean()
        self.assertGreater(rb_out, 1.4)
        self.assertGreater(rb_out, rb_in * 0.7)       # nudged, not neutralised

    def test_white_balance_gains_are_clamped(self):
        img = np.full((100, 100, 3), 0.5, np.float32)
        img[..., 2] = 0.1                              # extreme yellow cast
        gains = enhance._wb_gains(img, 1.0)
        self.assertLessEqual(float(gains.max()), enhance._WB_MAX_GAIN * 1.08)
        self.assertGreaterEqual(float(gains.min()), 1 / enhance._WB_MAX_GAIN / 1.08)

    def test_neutral_scene_keeps_neutral(self):
        g = textured()[..., 1:2]
        img = np.repeat(g * 0.6 + 0.2, 3, axis=2).astype(np.float32)
        out = enhance.auto_enhance(img, 1.0)
        chroma = np.abs(out[..., 0] - out[..., 2]).mean()
        self.assertLess(float(chroma), 0.01)

    def test_vibrance_boosts_muted_colours_more_than_skin(self):
        img = np.zeros((40, 80, 3), np.float32)
        img[:, :40] = (0.45, 0.50, 0.58)        # muted blue-grey
        img[:, 40:] = (0.80, 0.62, 0.52)        # skin tone
        pl = [np.ascontiguousarray(img[..., c]) for c in range(3)]
        y = enhance._luma3(*pl)
        enhance._vibrance_planes(pl, y, 0.3)
        out = np.stack(pl, axis=2)

        def sat(a):
            return float((a.max(axis=2) - a.min(axis=2)).mean())
        gain_muted = sat(out[:, :40]) / sat(img[:, :40])
        gain_skin = sat(out[:, 40:]) / sat(img[:, 40:])
        self.assertGreater(gain_muted, 1.1)
        self.assertLess(gain_skin, 1.06)
        self.assertLess(gain_skin, gain_muted)

    def test_strips_do_not_change_the_result(self):
        img = dark_scene(h=700, w=300)
        a = enhance.auto_enhance(img, 0.8)
        with mock.patch.object(enhance, "_STRIP", 10_000):
            b = enhance.auto_enhance(img, 0.8)
        self.assertTrue(np.allclose(a, b, atol=1e-6))

    def test_preview_size_gets_the_same_decisions(self):
        img = dark_scene(h=900, w=1200)
        small = np.asarray(Image.fromarray((img * 255 + 0.5).astype(np.uint8)).resize(
            (400, 300), Image.Resampling.BILINEAR), dtype=np.float32) / 255
        full = enhance.auto_enhance(img, 0.6)
        prev = enhance.auto_enhance(small, 0.6)
        self.assertLess(abs(float(luma(full).mean()) - float(luma(prev).mean())), 3 / 255)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


class TestFilters(unittest.TestCase):

    def test_registry_shape(self):
        ids = list(enhance.FILTERS)
        self.assertEqual(ids[0], "none")
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), len(ids))
        titles = [t for _i, t in enhance.filter_choices()]
        self.assertIn("Без фильтра", titles)
        self.assertIn("Чёрно-белый", titles)
        for f in enhance.FILTERS.values():
            self.assertTrue(f.title and f.description)
            self.assertTrue(any("а" <= ch.lower() <= "я" for ch in f.title), f.title)

    def test_every_filter_at_strength_zero_is_identity(self):
        img = textured()
        for fid in enhance.FILTERS:
            with self.subTest(filter=fid):
                out = enhance.apply_filter(img, fid, 0.0)
                self.assertTrue(np.array_equal(out, img))

    def test_every_filter_at_strength_one_changes_the_image(self):
        img = textured()
        for fid in enhance.FILTERS:
            with self.subTest(filter=fid):
                out = enhance.apply_filter(img, fid, 1.0)
                self.assertEqual(out.dtype, np.float32)
                self.assertEqual(out.shape, img.shape)
                self.assertGreaterEqual(float(out.min()), 0.0)
                self.assertLessEqual(float(out.max()), 1.0)
                diff = float(np.abs(out - img).mean())
                if fid == "none":
                    self.assertEqual(diff, 0.0)
                else:
                    self.assertGreater(diff, 0.004)

    def test_strength_blends_between_input_and_full_filter(self):
        img = textured()
        for fid in enhance.FILTERS:
            if fid == "none":
                continue
            with self.subTest(filter=fid):
                full = enhance.apply_filter(img, fid, 1.0)
                half = enhance.apply_filter(img, fid, 0.5)
                self.assertTrue(np.allclose(half, (img + full) / 2, atol=2e-3))

    def test_bw_is_grey(self):
        out = enhance.apply_filter(textured(), "bw", 1.0)
        self.assertTrue(np.array_equal(out[..., 0], out[..., 1]))
        self.assertTrue(np.array_equal(out[..., 1], out[..., 2]))

    def test_warm_moves_toward_red(self):
        img = textured()
        warm = enhance.apply_filter(img, "warm", 1.0)
        self.assertGreater(warm[..., 0].mean() - warm[..., 2].mean(),
                           img[..., 0].mean() - img[..., 2].mean())

    def test_filters_that_spoil_skin_are_not_offered(self):
        # «Плёнка + зерно», «Холодный» и «Матовый» портили кожу на снимках
        # мероприятий; сохранённый пресет с ними откатывается на «Без фильтра».
        self.assertEqual(list(enhance.FILTERS),
                         ["none", "shadows", "contrast", "bw", "warm", "vignette"])
        for gone in ("film", "cold", "matte"):
            with self.assertRaises(ValueError):
                enhance.Params(filter_id=gone).validated()

    def test_unknown_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            enhance.apply_filter(textured(), "sepia", 1.0)
        with self.assertRaises(ValueError):
            enhance.Params(filter_id="sepia").validated()

    def test_position_dependent_filters_ignore_strip_boundaries(self):
        img = textured(h=900, w=200)
        for fid in ("vignette",):
            with self.subTest(filter=fid):
                a = enhance.apply_filter(img, fid, 1.0)
                with mock.patch.object(enhance, "_STRIP", 10_000):
                    b = enhance.apply_filter(img, fid, 1.0)
                self.assertTrue(np.allclose(a, b, atol=1e-6))

    def test_vignette_darkens_corners_not_centre(self):
        img = np.full((200, 300, 3), 0.6, np.float32)
        out = enhance.apply_filter(img, "vignette", 1.0)
        self.assertAlmostEqual(float(out[100, 150, 0]), 0.6, places=4)
        self.assertLess(float(out[0, 0, 0]), 0.45)


# --------------------------------------------------------------------------
# process / preview
# --------------------------------------------------------------------------


class TestProcess(unittest.TestCase):

    def test_order_is_enhance_then_filter(self):
        img = dark_scene()
        got = enhance.process(img, enhance_strength=0.7, filter_id="contrast", filter_strength=0.8)
        want = enhance.apply_filter(enhance.auto_enhance(img, 0.7), "contrast", 0.8)
        self.assertTrue(np.allclose(got, want, atol=1e-6))
        other = enhance.auto_enhance(enhance.apply_filter(img, "contrast", 0.8), 0.7)
        self.assertFalse(np.allclose(got, other, atol=1e-3))

    def test_all_off_is_identity(self):
        img = textured()
        out = enhance.process(img, enhance_strength=0, filter_id="bw", filter_strength=0)
        self.assertTrue(np.array_equal(out, img))

    def test_params_from_mapping(self):
        p = enhance.Params.of({"enhance_strength": 3, "filter_id": "warm"})
        self.assertEqual(p.enhance_strength, 1.0)
        self.assertEqual(p.filter_id, "warm")
        self.assertEqual(enhance.Params.of(None), enhance.Params())
        with self.assertRaises(ValueError):
            enhance.Params.of({"strength": 1})
        self.assertIn("Тёплый", p.describe())

    def test_preview_returns_small_rgb_image(self):
        im = Image.fromarray((dark_scene(900, 1600) * 255).astype(np.uint8), "RGB")
        out = enhance.preview(im, {"filter_id": "vignette"}, max_side=400)
        self.assertIsInstance(out, Image.Image)
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(max(out.size), 400)
        self.assertEqual(im.size, (1600, 900))          # the input is not shrunk in place
        arr_out = enhance.preview(dark_scene(90, 160), None, max_side=400)
        self.assertEqual(arr_out.size, (160, 90))

    def test_to_float_and_back_round_trips_8_bit(self):
        a = (np.arange(256, dtype=np.uint8)[None, :, None] * np.ones((4, 1, 3), np.uint8))
        im = Image.fromarray(a, "RGB")
        back = np.asarray(enhance.to_image(enhance.to_float(im)))
        self.assertTrue(np.array_equal(back, a))


# --------------------------------------------------------------------------
# process_file
# --------------------------------------------------------------------------


class TestProcessFile(TempDirCase):

    def test_keeps_exif_and_writes_orientation_1(self):
        im = marker_image(96, 64)
        mn = b"MN\x00\x01" + bytes(range(60))
        src = self.write_jpeg("IMG_0001.JPG", im, exif=tiff_exif(6, 96, 64, makernote=mn))
        dst = self.out_dir / "IMG_0001.jpg"
        res = enhance.process_file(src, dst)
        self.assertTrue(res.ok, res.message)
        self.assertIn("Готово", res.message)
        out = opened(dst)
        exif = out.getexif()
        self.assertEqual(exif.get(0x0112), 1)
        self.assertEqual(exif.get(0x010F), "Canon")
        sub = exif.get_ifd(0x8769)
        self.assertEqual(sub.get(0x9003), "2020:01:01 12:00:00")
        self.assertEqual((sub.get(0xA002), sub.get(0xA003)), (64, 96))
        # MakerNote bytes survive untouched (no re-layout), the stale thumbnail link is gone.
        self.assertIn(mn, out.info["exif"])
        from PIL import ExifTags
        self.assertEqual(len(exif.get_ifd(ExifTags.IFD.IFD1)), 0)
        # APP1 Exif must be the first segment after SOI.
        raw = dst.read_bytes()
        self.assertEqual(raw[:4], b"\xff\xd8\xff\xe1")
        self.assertEqual(raw[6:12], b"Exif\x00\x00")

    def test_orientation_is_applied_once_for_every_value(self):
        base = marker_image(96, 64)
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                src = self.write_jpeg("o%d.jpg" % orientation, base,
                                      exif=tiff_exif(orientation, 96, 64))
                want = ImageOps.exif_transpose(opened(src))
                dst = self.out_dir / ("o%d.jpg" % orientation)
                res = enhance.process_file(src, dst, enhance_strength=0)
                self.assertTrue(res.ok, res.message)
                got = opened(dst)
                self.assertEqual(got.size, want.size)
                self.assertEqual(red_corner(got), red_corner(want))
                self.assertEqual(got.getexif().get(0x0112), 1)
                # Processing the result again must not rotate it a second time.
                dst2 = self.root / "второй проход" / ("o%d.jpg" % orientation)
                res2 = enhance.process_file(dst, dst2, enhance_strength=0)
                self.assertTrue(res2.ok, res2.message)
                again = opened(dst2)
                self.assertEqual(again.size, want.size)
                self.assertEqual(red_corner(again), red_corner(want))

    def test_already_rotated_pixels_are_not_rotated_again(self):
        # An editor rotated the pixels to portrait but left Orientation=6 and
        # the camera's landscape PixelX/YDimension behind.
        rotated = marker_image(96, 64).transpose(Image.Transpose.ROTATE_270)
        src = self.write_jpeg("baked.jpg", rotated, exif=tiff_exif(6, 96, 64))
        dst = self.out_dir / "baked.jpg"
        res = enhance.process_file(src, dst, enhance_strength=0)
        self.assertTrue(res.ok, res.message)
        got = opened(dst)
        self.assertEqual(got.size, rotated.size)
        self.assertEqual(red_corner(got), red_corner(rotated))
        self.assertEqual(got.getexif().get(0x0112), 1)
        self.assertIn("повторно не поворачивается", res.message)

    def test_draft_preview_decides_rotation_like_the_full_frame(self):
        rotated = marker_image(960, 640).transpose(Image.Transpose.ROTATE_270)
        src = self.write_jpeg("baked_big.jpg", rotated, exif=tiff_exif(6, 960, 640))
        small = enhance.load_image(src, max_side=200)
        self.assertGreater(small.size[1], small.size[0])
        self.assertEqual(red_corner(small), red_corner(rotated))

    def test_cr2_uses_embedded_jpeg_exif_and_orientation(self):
        src = make_test_cr2.make_cr2(self.src_dir / "IMG_0002.CR2", orientation=8,
                                     preview_size=(1936, 1288), raw_size=(1936, 1288),
                                     model="Canon EOS 550D")
        dst = self.out_dir / "IMG_0002.jpg"
        res = enhance.process_file(src, dst, filter_id="warm")
        self.assertTrue(res.ok, res.message)
        out = opened(dst)
        self.assertEqual(out.size, (1288, 1936))
        exif = out.getexif()
        self.assertEqual(exif.get(0x0112), 1)
        self.assertEqual(exif.get(0x0110), "Canon EOS 550D")
        self.assertEqual(exif.get_ifd(0x8769).get(0xA002), 1288)

    def test_cr2_with_baked_rotation_is_not_rotated_twice(self):
        src = make_test_cr2.make_cr2(self.src_dir / "IMG_0003.CR2", orientation=6,
                                     preview_size=(1288, 1936), raw_size=(1936, 1288))
        dst = self.out_dir / "IMG_0003.jpg"
        res = enhance.process_file(src, dst)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(opened(dst).size, (1288, 1936))

    def test_png_and_tiff_inputs(self):
        im = Image.fromarray((dark_scene(60, 90) * 255).astype(np.uint8), "RGB")
        for ext in (".png", ".tif"):
            with self.subTest(ext=ext):
                src = self.src_dir / ("frame" + ext)
                im.save(src)
                dst = self.out_dir / ("frame_%s.jpg" % ext[1:])
                res = enhance.process_file(src, dst)
                self.assertTrue(res.ok, res.message)
                self.assertEqual(opened(dst).size, (90, 60))

    def test_never_overwrites_the_source(self):
        src = self.write_jpeg("IMG_0004.jpg")
        before = src.read_bytes()
        for dst in (src, Path(str(src)), src.parent / "." / src.name):
            res = enhance.process_file(src, dst, allow_source_dir=True, overwrite=True)
            self.assertFalse(res.ok)
            self.assertIn("оригинал не перезаписывается", res.message)
        if os.name == "nt":
            res = enhance.process_file(src, Path(str(src).upper()),
                                       allow_source_dir=True, overwrite=True)
            self.assertFalse(res.ok)
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.src_dir.iterdir()), ["IMG_0004.jpg"])

    def test_source_folder_needs_explicit_permission(self):
        src = self.write_jpeg("IMG_0005.jpg")
        dst = self.src_dir / "IMG_0005_улучшено.jpg"
        res = enhance.process_file(src, dst)
        self.assertFalse(res.ok)
        self.assertIn("папку с оригиналами", res.message)
        self.assertFalse(dst.exists())
        res = enhance.process_file(src, dst, allow_source_dir=True)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(dst.exists())

    def test_existing_destination_is_kept_unless_overwrite(self):
        src = self.write_jpeg("IMG_0006.jpg")
        dst = self.out_dir / "IMG_0006.jpg"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"do not touch")
        res = enhance.process_file(src, dst)
        self.assertFalse(res.ok)
        self.assertTrue(res.skipped)
        self.assertEqual(dst.read_bytes(), b"do not touch")
        res = enhance.process_file(src, dst, overwrite=True)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(dst.read_bytes()[:2], b"\xff\xd8")

    def test_writes_through_a_temp_file_then_replaces(self):
        src = self.write_jpeg("IMG_0007.jpg")
        dst = self.out_dir / "IMG_0007.jpg"
        seen: list[tuple[Path, bool, bool]] = []
        real = cr2_core._replace_with_retry

        def spy(tmp: Path, target: Path) -> None:
            seen.append((Path(tmp), Path(tmp).exists(), Path(target).exists()))
            real(tmp, target)

        with mock.patch.object(cr2_core, "_replace_with_retry", spy):
            res = enhance.process_file(src, dst)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(len(seen), 1)
        tmp, tmp_existed, dst_existed = seen[0]
        self.assertEqual(tmp.parent, dst.parent)
        self.assertTrue(tmp.name.endswith(".tmp"))
        self.assertTrue(tmp_existed)
        self.assertFalse(dst_existed)
        self.assertFalse(tmp.exists())
        self.assertEqual([p.name for p in self.out_dir.iterdir()], ["IMG_0007.jpg"])

    def test_failed_replace_leaves_old_file_and_no_temp(self):
        src = self.write_jpeg("IMG_0008.jpg")
        dst = self.out_dir / "IMG_0008.jpg"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"previous result")

        def boom(tmp: Path, target: Path) -> None:
            raise OSError("диск отключён")

        with mock.patch.object(cr2_core, "_replace_with_retry", boom):
            res = enhance.process_file(src, dst, overwrite=True)
        self.assertFalse(res.ok)
        self.assertIn("не удалось записать", res.message)
        self.assertEqual(dst.read_bytes(), b"previous result")
        self.assertEqual([p.name for p in self.out_dir.iterdir()], ["IMG_0008.jpg"])

    def test_bad_inputs_are_reported_not_raised(self):
        bad = self.src_dir / "broken.jpg"
        bad.write_bytes(b"\xff\xd8 this is not a jpeg")
        res = enhance.process_file(bad, self.out_dir / "broken.jpg")
        self.assertFalse(res.ok)
        self.assertIn("Ошибка", res.message)
        self.assertFalse((self.out_dir / "broken.jpg").exists())
        res = enhance.process_file(self.src_dir / "missing.jpg", self.out_dir / "m.jpg")
        self.assertFalse(res.ok)
        self.assertIn("не найден", res.message)
        src = self.write_jpeg("ok.jpg")
        res = enhance.process_file(src, self.out_dir / "ok.png")
        self.assertFalse(res.ok)
        self.assertIn("JPEG", res.message)
        with self.assertRaises(ValueError):
            enhance.process_file(src, self.out_dir / "ok.jpg", filter_id="sepia")

    def test_keep_exif_false_writes_no_exif(self):
        src = self.write_jpeg("IMG_0009.jpg", marker_image(), exif=tiff_exif(3, 96, 64))
        dst = self.out_dir / "IMG_0009.jpg"
        res = enhance.process_file(src, dst, keep_exif=False)
        self.assertTrue(res.ok, res.message)
        out = opened(dst)
        self.assertNotIn("exif", out.info)
        self.assertEqual(red_corner(out), (False, False))     # 180 degrees still applied

    def test_16_bit_grey_opens_for_preview_at_any_size(self):
        ramp = (np.linspace(0, 65535, 600, dtype=np.float64)[None, :]
                * np.ones((400, 1))).astype(np.uint16)
        src = self.src_dir / "scan16.tif"
        Image.fromarray(ramp).save(src)             # mode I;16
        with Image.open(src) as check:
            self.assertTrue(check.mode.startswith("I"), check.mode)
        for side in (1400, 100):
            with self.subTest(max_side=side):
                im = enhance.load_image(src, max_side=side)
                self.assertEqual(im.mode, "RGB")
                self.assertLessEqual(max(im.size), max(side, 1))
                a = np.asarray(im)
                self.assertLess(int(a[..., 0].min()), 10)       # scaled, not clipped white
                self.assertGreater(int(a[..., 0].max()), 245)

    def test_only_an_rgb_icc_profile_is_carried_over(self):
        def profile(space: bytes) -> bytes:
            head = bytearray(128)
            head[12:16] = b"mntr"
            head[16:20] = space
            head[36:40] = b"acsp"
            return bytes(head)

        rgb = Image.fromarray((textured(40, 60) * 255).astype(np.uint8), "RGB")
        cases = [("rgb.jpg", rgb, profile(b"RGB "), True),
                 ("grey.jpg", rgb.convert("L"), profile(b"GRAY"), False),
                 ("cmyk.jpg", rgb.convert("CMYK"), profile(b"CMYK"), False)]
        for name, im, icc, kept in cases:
            with self.subTest(source=name):
                src = self.src_dir / name
                im.save(src, "JPEG", quality=95, icc_profile=icc)
                dst = self.out_dir / name
                res = enhance.process_file(src, dst)
                self.assertTrue(res.ok, res.message)
                out = opened(dst)
                self.assertEqual(out.mode, "RGB")
                got = out.info.get("icc_profile")
                if kept:
                    self.assertEqual(got, icc)
                else:
                    self.assertIsNone(got)

    def test_load_image_orients_and_shrinks(self):
        src = self.write_jpeg("big.jpg", marker_image(960, 640), exif=tiff_exif(6, 960, 640))
        im = enhance.load_image(src, max_side=300)
        self.assertEqual(im.mode, "RGB")
        self.assertLessEqual(max(im.size), 300)
        self.assertGreater(im.size[1], im.size[0])            # portrait after rotation


# --------------------------------------------------------------------------
# process_many
# --------------------------------------------------------------------------


class TestProcessMany(TempDirCase):

    def test_suffix_is_planned_not_renamed(self):
        a = self.write_jpeg("IMG_0001.jpg")
        b = self.write_jpeg("IMG_0001.jpg", folder=self.src_dir / "другая")
        self.out_dir.mkdir()
        (self.out_dir / "IMG_0001_обр.jpg").write_bytes(b"old")
        plan = enhance.plan_outputs([a, b], self.out_dir, "_обр")
        self.assertEqual([dst.name for _src, dst, _note in plan],
                         ["IMG_0001_обр_2.jpg", "IMG_0001_обр_3.jpg"])
        written: list[str] = []
        real = enhance.process_file

        def spy(src, dst, **kw):
            written.append(Path(dst).name)
            return real(src, dst, **kw)

        with mock.patch.object(enhance, "process_file", side_effect=spy):
            results = enhance.process_many([a, b], self.out_dir, None, suffix="_обр")
        self.assertTrue(all(r.ok for r in results), [r.message for r in results])
        self.assertEqual(written, ["IMG_0001_обр_2.jpg", "IMG_0001_обр_3.jpg"])
        self.assertEqual([r.dst.name for r in results], written)
        self.assertEqual((self.out_dir / "IMG_0001_обр.jpg").read_bytes(), b"old")

    def test_name_collisions_are_resolved(self):
        a = self.write_jpeg("IMG_0001.jpg", folder=self.src_dir / "день1")
        b = self.write_jpeg("IMG_0001.jpg", folder=self.src_dir / "день2")
        c = self.src_dir / "день1" / "IMG_0001.png"
        Image.fromarray((textured(40, 60) * 255).astype(np.uint8)).save(c)
        self.out_dir.mkdir()
        (self.out_dir / "IMG_0001.jpg").write_bytes(b"old export")
        results = enhance.process_many([a, b, c, a], self.out_dir, {"filter_id": "warm"},
                                       workers=3)
        self.assertEqual([r.src for r in results], [a, b, c, a])
        self.assertTrue(all(r.ok for r in results[:3]), [r.message for r in results])
        names = [r.dst.name for r in results[:3]]
        self.assertEqual(names, ["IMG_0001_2.jpg", "IMG_0001_3.jpg", "IMG_0001_4.jpg"])
        self.assertTrue(results[3].skipped)
        self.assertIsNone(results[3].dst)
        self.assertEqual((self.out_dir / "IMG_0001.jpg").read_bytes(), b"old export")
        self.assertIn("сохранено как", results[0].message)

    def test_cancel_between_files(self):
        paths = [self.write_jpeg("IMG_%04d.jpg" % i) for i in range(6)]
        cancel = threading.Event()
        calls: list[tuple[int, int, str]] = []
        caller = threading.get_ident()
        threads: set[int] = set()

        def report(done: int, total: int, res: enhance.FileResult) -> None:
            calls.append((done, total, res.message))
            threads.add(threading.get_ident())
            cancel.set()

        results = enhance.process_many(paths, self.out_dir, None, workers=1,
                                       report=report, cancel_event=cancel)
        self.assertEqual(len(results), 6)
        self.assertEqual([r.src for r in results], paths)
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertTrue(results[0].ok)
        for r in results[1:]:
            self.assertTrue(r.skipped)
            self.assertEqual(r.message, "Отменено")
        self.assertEqual([c[0] for c in calls], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(c[1] == 6 for c in calls))
        self.assertEqual(threads, {caller})
        self.assertEqual([p.name for p in self.out_dir.iterdir()], ["IMG_0000.jpg"])

    def test_cancel_before_start_writes_nothing(self):
        paths = [self.write_jpeg("IMG_%04d.jpg" % i) for i in range(3)]
        cancel = threading.Event()
        cancel.set()
        results = enhance.process_many(paths, self.out_dir, None, workers=4, cancel_event=cancel)
        self.assertTrue(all(r.skipped and r.message == "Отменено" for r in results))
        self.assertFalse(self.out_dir.exists() and any(self.out_dir.iterdir()))

    def test_parallel_batch_processes_everything(self):
        paths = [self.write_jpeg("IMG_%04d.jpg" % i) for i in range(8)]
        results = enhance.process_many(paths, self.out_dir, enhance.Params(filter_id="contrast"),
                                       workers=4)
        self.assertTrue(all(r.ok for r in results), [r.message for r in results])
        self.assertEqual(len(list(self.out_dir.iterdir())), 8)

    def test_batch_refuses_the_source_folder(self):
        src = self.write_jpeg("IMG_0001.jpg")
        results = enhance.process_many([src], self.src_dir, None)
        self.assertFalse(results[0].ok)
        self.assertIn("папку с оригиналами", results[0].message)
        self.assertEqual([p.name for p in self.src_dir.iterdir()], ["IMG_0001.jpg"])

    def test_invalid_params_raise_before_any_work(self):
        src = self.write_jpeg("IMG_0001.jpg")
        with self.assertRaises(ValueError):
            enhance.process_many([src], self.out_dir, {"filter_id": "nope"})
        self.assertFalse(self.out_dir.exists())


if __name__ == "__main__":
    unittest.main()
