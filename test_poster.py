# -*- coding: utf-8 -*-
"""Тесты фирменного слоя: brand.py (растр, тон, цвет) и poster.py (шаблоны).

Только синтетические изображения - ни одной настоящей фотографии.  Шрифты -
из папки fonts/ репозитория (SIL OFL); Morfin Sans в репозитории нет, и
тесты от него не зависят.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import brand  # noqa: E402
import poster  # noqa: E402


def synthetic_photo(w: int = 1600, h: int = 1067) -> tuple[Image.Image, tuple]:
    """Кадр «человек у стены»: тёмный фон, светлое «лицо» с тёмными деталями.

    -> (изображение, face_box в пикселях).
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 40 + 60 * (xx / w) + 30 * np.sin(yy / 37.0)
    arr = np.repeat(base[..., None], 3, axis=2)
    arr[..., 0] += 25                                        # тёплый оттенок
    fl, ft, fr, fb = int(w * 0.55), int(h * 0.22), int(w * 0.70), int(h * 0.50)
    arr[ft:fb, fl:fr] = (205, 170, 150)
    ew = (fr - fl) // 5
    arr[ft + (fb - ft) // 3: ft + (fb - ft) // 3 + ew // 2, fl + ew: fl + 2 * ew] = 30
    arr[ft + (fb - ft) // 3: ft + (fb - ft) // 3 + ew // 2, fr - 2 * ew: fr - ew] = 30
    arr[fb - (fb - ft) // 4: fb - (fb - ft) // 4 + ew // 3, fl + 2 * ew: fr - 2 * ew] = 90
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    return img, (fl, ft, fr, fb)


_RENDERS: dict[str, Image.Image] = {}


def rendered(template_id: str) -> Image.Image:
    """Отрисовка шаблона в полном размере с примером полей (кэш на модуль)."""
    if template_id not in _RENDERS:
        photo, face = synthetic_photo()
        _RENDERS[template_id] = poster.render(
            template_id, poster.example_fields(template_id), photo, face_box=face)
    return _RENDERS[template_id]


# --------------------------------------------------------------------------- #
#  brand.py                                                                    #
# --------------------------------------------------------------------------- #

class TestHalftone(unittest.TestCase):

    def test_numeric_self_check_passes(self):
        self.assertEqual(brand.self_check(verbose=False), 0)

    def test_dot_area_tracks_requested_coverage(self):
        acc = brand.screen_accuracy(pitches=(4.0, 8.0, 12.0, 20.0))
        worst = max(abs(got - c) for (_, c), got in acc.items())
        self.assertLess(worst, 0.015, acc)

    def test_mid_grey_gets_dense_ink_through_lstar(self):
        """Дефект прототипа: gamma давала средне-серому 0.16 краски."""
        self.assertAlmostEqual(brand.coverage_for_srgb(128), 0.84, delta=0.02)
        grey = np.full((32, 32, 3), 128, np.uint8)
        cov = brand.tone_to_coverage(grey, levels="none", max_ink=1.0)
        self.assertAlmostEqual(float(cov.mean()), 0.84, delta=0.02)

    def test_printed_field_matches_source_lightness(self):
        c = brand.coverage_for_srgb(128)
        alpha = float(brand.screen_alpha(np.full((400, 400), c, np.float32), 9.0).mean())
        y = ((1 - alpha) * brand.relative_luminance(brand.CREAM)
             + alpha * brand.relative_luminance(brand.CRIMSON))
        target = float(brand.lstar_from_y(brand.srgb_to_linear(128 / 255)))
        self.assertLess(abs(float(brand.lstar_from_y(y)) - target), 1.0)

    def test_coverage_is_monotone_and_clipped(self):
        ramp = np.repeat(np.arange(256, dtype=np.uint8)[None, :, None], 3, axis=2)
        cov = brand.tone_to_coverage(ramp, levels="none", max_ink=0.9)[0]
        self.assertTrue(np.all(np.diff(cov) <= 1e-6))
        self.assertLessEqual(float(cov.max()), 0.9 + 1e-6)
        self.assertEqual(float(cov[-1]), 0.0)

    def test_face_levels_put_a_dark_face_into_the_midtones(self):
        img, face = synthetic_photo(600, 400)
        arr = (np.asarray(img).astype(np.float32) * 0.35).astype(np.uint8)  # недодержка
        l, t, r, b = face
        plain = brand.tone_to_coverage(arr, levels="none")[t:b, l:r].mean()
        by_face = brand.tone_to_coverage(arr, face_box=face)[t:b, l:r].mean()
        self.assertGreater(plain, 0.85)                  # без уровней - плашка
        self.assertTrue(0.3 < by_face < 0.8, by_face)    # с уровнями - полутон

    def test_pitch_follows_display_size(self):
        self.assertAlmostEqual(brand.pitch_for_display(1080, 360), 12.0)
        self.assertAlmostEqual(brand.pitch_for_display(3508, 1754, dot_display_px=5), 10.0)
        self.assertGreaterEqual(brand.pitch_for_display(300, 3000), 2.5)
        with self.assertRaises(ValueError):
            brand.pitch_for_display(1080, 0)

    def test_halftone_does_not_modify_input_and_stays_on_ramp(self):
        img, face = synthetic_photo(300, 200)
        src = np.asarray(img).copy()
        out = brand.halftone(img, pitch=6.0, face_box=face)
        self.assertTrue(np.array_equal(np.asarray(img), src))
        self.assertEqual(out.size, img.size)
        lut = brand._duotone_lut(brand.CRIMSON, brand.CREAM)
        uniq = np.unique(np.asarray(out).reshape(-1, 3), axis=0)
        d = np.abs(uniq[:, None, :].astype(int) - lut[None, :, :].astype(int)).sum(2).min(1)
        self.assertEqual(int(d.max()), 0)

    def test_contrast_ratios(self):
        self.assertAlmostEqual(brand.contrast_ratio(brand.CREAM, brand.CRIMSON), 8.08, places=1)
        self.assertLess(brand.contrast_ratio(brand.SAND, brand.CREAM), 1.3)
        self.assertLess(brand.contrast_ratio(brand.GRAPHITE, brand.CRIMSON), 1.4)


# --------------------------------------------------------------------------- #
#  poster.py: шаблоны                                                          #
# --------------------------------------------------------------------------- #

class TestTemplates(unittest.TestCase):

    REQUIRED_IDS = {"afisha_a3", "post", "stories", "album_cover"}

    def test_required_templates_exist_with_brand_sizes(self):
        ids = {tid for tid, _ in poster.list_templates()}
        self.assertTrue(self.REQUIRED_IDS <= ids)
        self.assertTrue({"summary", "quote"} & ids)
        sizes = {tid: poster.TEMPLATES[tid]["size"] for tid in ids}
        self.assertEqual(sizes["afisha_a3"], (3508, 4961))
        self.assertEqual(poster.TEMPLATES["afisha_a3"]["dpi"], 300)
        self.assertEqual(sizes["post"], (1080, 1350))
        self.assertEqual(sizes["stories"], (1080, 1920))
        self.assertEqual(sizes["album_cover"], (1080, 1080))

    def test_every_template_declares_fields_and_a_photo(self):
        for tid, title in poster.list_templates():
            with self.subTest(tid=tid):
                keys = {f.key for f in poster.template_fields(tid)}
                self.assertIn("photo", keys)
                self.assertTrue(keys & {"headline", "quote"})
                self.assertTrue(re.search("[А-Яа-я]", title))
                for f in poster.template_fields(tid):
                    self.assertTrue(re.search("[А-Яа-я]", f.label), f)
                block_fields = {b.get("field") for b in poster.TEMPLATES[tid]["blocks"]}
                self.assertTrue(block_fields - {None} <= keys)

    def test_every_template_renders_at_its_size(self):
        for tid, _ in poster.list_templates():
            with self.subTest(tid=tid):
                im = rendered(tid)
                self.assertEqual(im.size, tuple(poster.TEMPLATES[tid]["size"]))
                self.assertEqual(im.mode, "RGB")
                rep = poster.render_report(im)
                self.assertIsNotNone(rep)
                self.assertEqual(len(rep.photo_boxes), 1)
                self.assertGreater(rep.pitch_px, 2.0)
                bad = [w for w in rep.warnings
                       if "не помещается" in w or "фон под текстом" in w]
                self.assertEqual(bad, [])

    def test_missing_required_field_is_reported_in_russian(self):
        photo, _ = synthetic_photo(200, 150)
        fields = poster.example_fields("post")
        fields.pop("headline")
        with self.assertRaisesRegex(ValueError, "Заголовок"):
            poster.render("post", fields, photo)
        with self.assertRaisesRegex(ValueError, "шаблона"):
            poster.render("nope", fields, photo)

    def test_optional_fields_may_be_empty(self):
        photo, _ = synthetic_photo(400, 300)
        im = poster.render("post", {"headline": "Лекция", "date": "01.09"}, photo,
                           size=(540, 675))
        self.assertEqual(im.size, (540, 675))


class TestRectanglesOnly(unittest.TestCase):
    """Вне объявленных прямоугольников - только фон; внутри - только своё."""

    def _owner_map(self, rep: poster.RenderReport) -> np.ndarray:
        W, H = rep.size
        owner = np.full((H, W), -1, np.int32)
        for i, b in enumerate(rep.blocks):
            x0, y0, x1, y1 = b.box
            owner[y0:y1, x0:x1] = i
        return owner

    def test_no_brand_pixels_outside_declared_blocks(self):
        for tid, _ in poster.list_templates():
            with self.subTest(tid=tid):
                im = rendered(tid)
                rep = poster.render_report(im)
                arr = np.asarray(im)
                owner = self._owner_map(rep)
                bg = np.array(brand.hex_to_rgb(poster.color(poster.TEMPLATES[tid]["bg"])))
                outside = arr[owner < 0]
                self.assertTrue(np.all(outside == bg),
                                f"{int((outside != bg).any(1).sum())} чужих пикселей")

    def test_each_block_contains_only_its_own_colours(self):
        lut = brand._duotone_lut(brand.CRIMSON, brand.CREAM).astype(np.int32)
        for tid, _ in poster.list_templates():
            im = rendered(tid)
            rep = poster.render_report(im)
            arr = np.asarray(im).astype(np.int32)
            owner = self._owner_map(rep)
            texts = {p.field: p for p in rep.text_pairs}
            for i, b in enumerate(rep.blocks):
                px = arr[owner == i]
                if px.size == 0:
                    continue
                with self.subTest(tid=tid, block=i, kind=b.kind):
                    if b.kind == "rect":
                        fill = np.array(brand.hex_to_rgb(b.fill))
                        self.assertTrue(np.all(px == fill), "плашка не сплошная")
                    elif b.kind == "text":
                        # сглаженный текст лежит на отрезке «цвет текста - фон»
                        fg = np.array(brand.hex_to_rgb(texts[b.field].fg))
                        bgc = np.array(brand.hex_to_rgb(texts[b.field].bg))
                        u = np.unique(px, axis=0)
                        seg = bgc - fg
                        t = np.clip(((u - fg) @ seg) / float(seg @ seg), 0, 1)
                        dist = np.abs(u - (fg + t[:, None] * seg)).max(1)
                        self.assertLessEqual(int(dist.max()), 3)
                    elif b.kind == "photo":
                        u = np.unique(px, axis=0)
                        d = np.abs(u[:, None, :] - lut[None]).sum(2).min(1)
                        self.assertEqual(int(d.max()), 0, "в фото не только растр бренда")

    def test_template_schema_has_no_other_shapes(self):
        allowed = {"rect", "swatches", "bands", "image", "text"}
        for tid, tpl in poster.TEMPLATES.items():
            for b in tpl["blocks"]:
                self.assertIn(b["type"], allowed)
                self.assertEqual(len(b["rect"]), 4)
                x, y, w, h = b["rect"]
                self.assertTrue(0 <= x and 0 <= y and x + w <= 1.0001 and y + h <= 1.0001,
                                (tid, b))


class TestBrandBalance(unittest.TestCase):

    def test_default_templates_pass_60_30_10(self):
        for tid, _ in poster.list_templates():
            with self.subTest(tid=tid):
                use = poster.usage_report(rendered(tid))
                total = sum(v for k, v in use.items() if k != "photo_ink")
                self.assertAlmostEqual(total, 1.0, places=6)
                self.assertEqual(poster.check_60_30_10(use), [], use)
                layout = 1.0 - use["photo"]
                self.assertGreater(use["graphite"] / layout, 0.20)   # было 0.6 %

    def test_off_balance_layout_is_flagged_in_russian(self):
        cream = Image.new("RGB", (200, 200), brand.hex_to_rgb(brand.CREAM))
        warnings = poster.check_60_30_10(poster.usage_report(cream, photo_boxes=[]))
        self.assertTrue(any("графит" in w for w in warnings), warnings)
        self.assertTrue(any("кремовый" in w for w in warnings), warnings)

    def test_contrast_pairs_pass_wcag(self):
        for tid, _ in poster.list_templates():
            pairs = poster.contrast_pairs(tid)
            self.assertTrue(pairs)
            for p in pairs:
                with self.subTest(tid=tid, field=p.field):
                    self.assertTrue(p.ok, p)
                    if p.body:
                        self.assertGreaterEqual(p.ratio, brand.WCAG_BODY)
                    self.assertNotIn((p.fg, p.bg), brand.FORBIDDEN_PAIRS)

    def test_used_pairs_match_the_real_background(self):
        for tid, _ in poster.list_templates():
            rep = poster.render_report(rendered(tid))
            used = {(p.field, p.fg, p.bg) for p in rep.text_pairs}
            declared = {(p.field, p.fg, p.bg) for p in poster.contrast_pairs(tid)}
            self.assertTrue(used <= declared)

    def test_failing_pair_is_refused(self):
        bad = copy.deepcopy(poster.TEMPLATES["post"])
        for b in bad["blocks"]:
            if b.get("field") == "subline":
                b["color"] = "sand"            # песок на креме - 1.25:1
        with mock.patch.dict(poster.TEMPLATES, {"_bad": bad}):
            photo, _ = synthetic_photo(300, 200)
            with self.assertRaisesRegex(ValueError, "контраст"):
                poster.render("_bad", poster.example_fields("post"), photo,
                              size=(540, 675))


# --------------------------------------------------------------------------- #
#  Фото, шрифты, экспорт                                                       #
# --------------------------------------------------------------------------- #

class TestPhotoPlacement(unittest.TestCase):

    def test_cover_crop_keeps_face_and_aspect(self):
        face = (4000, 300, 4400, 800)
        box = poster.cover_crop_box(5184, 3456, 918, 547, face_box=face,
                                    face_fill=0.28, max_zoom=2.5)
        l, t, r, b = box
        self.assertAlmostEqual((r - l) / (b - t), 918 / 547, delta=0.01)
        self.assertTrue(l <= face[0] and r >= face[2] and t <= face[1] and b >= face[3])
        self.assertTrue(0 <= l and r <= 5184 and 0 <= t and b <= 3456)

    def test_face_at_the_edge_is_not_cut(self):
        for face in ((0, 0, 300, 300), (4884, 3156, 5184, 3456)):
            l, t, r, b = poster.cover_crop_box(5184, 3456, 1080, 1920, face_box=face)
            self.assertTrue(l <= face[0] and r >= face[2] and t <= face[1] and b >= face[3])

    def test_photo_file_is_opened_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "frame.jpg"
            synthetic_photo(900, 600)[0].save(path, quality=90)
            before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            poster.render("album_cover", poster.example_fields("album_cover"), path,
                          size=(540, 540))
            after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            self.assertEqual(before, after)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["frame.jpg"])

    def test_missing_photo_uses_placeholder_and_says_so(self):
        im = poster.render("post", poster.example_fields("post"), None, size=(540, 675))
        self.assertTrue(any("Фотография" in w for w in poster.render_report(im).warnings))


class TestFonts(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="poster_fonts_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.bundled = HERE / "fonts"
        self.empty = self.tmp / "empty"
        self.empty.mkdir()

    def test_bundled_fonts_are_licence_clean(self):
        names = {p.name for p in self.bundled.iterdir()}
        fonts = {n for n in names if n.lower().endswith((".ttf", ".otf"))}
        self.assertTrue(fonts)
        for n in fonts:
            self.assertNotIn("morfin", n.lower())
            family = n.split("-")[0]
            self.assertIn(f"{family}-OFL.txt", names, n)
        for n in names:
            if n.endswith("-OFL.txt"):
                text = (self.bundled / n).read_text(encoding="utf-8")
                self.assertIn("SIL OPEN FONT LICENSE Version 1.1", text)

    def test_missing_user_file_falls_back_to_bundled_substitute(self):
        ch = poster.resolve_font("display", self.tmp / "нет-такого.ttf",
                                 user_dir=self.empty, bundled_dir=self.bundled,
                                 system_dirs=[])
        self.assertEqual(ch.source, "bundled")
        self.assertTrue(ch.is_substitute)
        self.assertIn("Oswald", ch.family)
        self.assertIn("вместо Morfin Sans", ch.label)
        text = poster.resolve_font("text", None, user_dir=self.empty,
                                   bundled_dir=self.bundled, system_dirs=[])
        self.assertIn("Fira Sans Extra Condensed", text.family)
        self.assertTrue(text.is_substitute)

    def test_user_font_dir_wins_over_bundled(self):
        shutil.copy(self.bundled / "Oswald-Bold.ttf", self.empty / "Morfin Sans Regular.TTF")
        ch = poster.resolve_font("display", None, user_dir=self.empty,
                                 bundled_dir=self.bundled, system_dirs=[])
        self.assertEqual(ch.source, "user_dir")
        self.assertFalse(ch.is_substitute)

    def test_user_chosen_file_wins(self):
        path = self.bundled / "FiraSansExtraCondensed-Regular.ttf"
        ch = poster.resolve_font("display", path, user_dir=self.empty,
                                 bundled_dir=self.bundled, system_dirs=[])
        self.assertEqual((ch.source, ch.path), ("user", str(path)))
        self.assertTrue(ch.is_substitute)

    def test_system_dir_used_when_bundled_is_missing(self):
        sysdir = self.tmp / "sys" / "nested"
        sysdir.mkdir(parents=True)
        shutil.copy(self.bundled / "Oswald-Bold.ttf", sysdir / "oswald_bold.ttf")
        ch = poster.resolve_font("display", None, user_dir=self.empty,
                                 bundled_dir=self.empty, system_dirs=[self.tmp / "sys"])
        self.assertEqual(ch.source, "system")

    def test_nothing_found_raises_russian_error(self):
        with self.assertRaisesRegex(poster.FontNotFoundError, "Не найден шрифт"):
            poster.resolve_font("display", None, user_dir=self.empty,
                                bundled_dir=self.empty, system_dirs=[])

    def test_broken_font_file_is_skipped(self):
        broken = self.empty / "MorfinSans-Regular.ttf"
        broken.write_bytes(b"not a font")
        ch = poster.resolve_font("display", broken, user_dir=self.empty,
                                 bundled_dir=self.bundled, system_dirs=[])
        self.assertEqual(ch.source, "bundled")

    def test_pyinstaller_bundle_dir(self):
        frozen = self.tmp / "_internal"
        (frozen / "fonts").mkdir(parents=True)
        with mock.patch.object(sys, "_MEIPASS", str(frozen), create=True):
            self.assertEqual(poster.bundled_fonts_dir(), frozen / "fonts")
        with mock.patch.object(sys, "_MEIPASS", str(self.empty), create=True):
            self.assertEqual(poster.bundled_fonts_dir(), HERE / "fonts")

    def test_render_reports_faces_actually_used(self):
        photo, _ = synthetic_photo(300, 200)
        im = poster.render("post", poster.example_fields("post"), photo,
                           display_font=self.tmp / "missing.ttf", size=(540, 675))
        rep = poster.render_report(im)
        self.assertEqual(set(rep.fonts), {"display", "text"})
        self.assertNotEqual(rep.fonts["display"].source, "user")
        self.assertTrue(any("missing.ttf" in w for w in rep.warnings))
        if rep.fonts["display"].is_substitute:
            self.assertTrue(any("Заголовок набран заменой" in w for w in rep.warnings))
        self.assertIn("text", rep.substituted)


class TestExport(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="poster_export_")
        self.addCleanup(self._td.cleanup)
        self.out = Path(self._td.name)
        photo, face = synthetic_photo(600, 400)
        self.im = poster.render("post", poster.example_fields("post"), photo, face_box=face)

    def test_png(self):
        p = poster.export(self.im, self.out / "пост.png")
        with Image.open(p) as got:
            self.assertEqual(got.format, "PNG")
            self.assertTrue(np.array_equal(np.asarray(got.convert("RGB")), np.asarray(self.im)))

    def test_jpeg_high_quality(self):
        p = poster.export(self.im, self.out / "post.jpg")
        with Image.open(p) as got:
            self.assertEqual(got.format, "JPEG")
            self.assertEqual(got.size, self.im.size)
            # стандартная таблица IJG масштабируется качеством: максимум 121
            # даёт 19 при q=92 и 22 при q=91 - значит q >= 92
            self.assertLessEqual(max(got.quantization[0]), 19)
            diff = np.abs(np.asarray(got, np.int16) - np.asarray(self.im, np.int16)).mean()
            self.assertLess(diff, 3.0)

    def test_pdf_is_a3(self):
        p = poster.export(rendered("afisha_a3"), self.out / "afisha", "pdf")
        data = p.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", data)
        self.assertIsNotNone(m)
        w_mm, h_mm = (float(v) / 72.0 * 25.4 for v in m.groups())
        self.assertAlmostEqual(w_mm, 297.0, delta=1.0)
        self.assertAlmostEqual(h_mm, 420.0, delta=1.0)

    def test_pdf_of_a_post_is_still_an_a3_page(self):
        p = poster.export(self.im, self.out / "post.pdf")
        m = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", p.read_bytes())
        w_mm, h_mm = (float(v) / 72.0 * 25.4 for v in m.groups())
        self.assertAlmostEqual(h_mm / w_mm, 420 / 297, delta=0.01)

    def test_unknown_format_leaves_no_file(self):
        with self.assertRaisesRegex(ValueError, "не поддерживается"):
            poster.export(self.im, self.out / "post.tiff")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_no_temporary_file_is_left_behind(self):
        for name in ("a.png", "b.jpg", "c.pdf"):
            poster.export(self.im, self.out / name)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["a.png", "b.jpg", "c.pdf"])

    @unittest.skipIf(sys.platform == "win32", "права доступа POSIX")
    def test_exported_file_follows_the_umask_not_0600(self):
        old = os.umask(0o022)
        try:
            p = poster.export(self.im, self.out / "shared.png")
        finally:
            os.umask(old)
        self.assertEqual(p.stat().st_mode & 0o777, 0o644)


class TestTextFitting(unittest.TestCase):
    """Длинный текст из жизни: переносы и предупреждение «слишком мелко»."""

    def warnings(self, template_id: str, **fields: str) -> list[str]:
        values = {**poster.example_fields(template_id), **fields}
        im = poster.render(template_id, values, None)
        return [w for w in poster.render_report(im).warnings if "заменой" not in w
                and "Фотография" not in w]

    def test_examples_are_readable(self):
        for tid, _title in poster.list_templates():
            with self.subTest(template=tid):
                self.assertEqual(self.warnings(tid), [])

    def test_a_full_address_is_reported_as_unreadable(self):
        got = self.warnings("post", venue="Главный корпус, ул. Миклухо-Маклая, 6, аудитория 374, "
                                          "вход со стороны парка")
        self.assertTrue(any("«venue»" in w and "мелко" in w for w in got), got)

    def test_hyphenated_word_breaks_after_the_hyphen_before_shrinking(self):
        font = poster.resolve_font("display").path
        word = "МЕЖДУНАРОДНО-ПРАВОВОЙ"
        size = 100.0
        f = poster._font(font, size)
        full = f.getlength(word)
        width = full * 0.7                      # the whole word does not fit
        lines = poster._wrap(word + " КОНГРЕСС", f, 0.0, width)
        self.assertIsNotNone(lines)
        self.assertEqual(lines[0], "МЕЖДУНАРОДНО-")
        self.assertTrue(lines[1].startswith("ПРАВОВОЙ"))
        self.assertIsNone(poster._wrap("ОЧЕНЬДЛИННОЕСЛОВОБЕЗДЕФИСА", f, 0.0, width / 3))
        # And fit_text keeps the big size instead of shrinking the whole line.
        lay = poster.fit_text(word, font, width, size * 3, max_size=size, min_size=10,
                              max_lines=2)
        self.assertEqual(lay.lines, ["МЕЖДУНАРОДНО-", "ПРАВОВОЙ"])
        self.assertGreater(lay.size, size * 0.9)

    def test_long_venue_uses_two_lines(self):
        venue = "Главный корпус, аудитория 374"
        im = poster.render("post", {**poster.example_fields("post"), "venue": venue}, None)
        self.assertEqual(self.warnings("post", venue=venue), [])
        self.assertIsNotNone(im)


if __name__ == "__main__":
    unittest.main()
