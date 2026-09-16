# -*- coding: utf-8 -*-
"""Тесты вкладки «Афиши» (tab_poster.py).

Окно не показывается: корневое окно сразу прячется (withdraw), события
прокачиваются root.update().  Без Tk (нет tcl/tk, нет дисплея) тесты
пропускаются, а не падают.  Фотографии - только синтетические, записанные во
временную папку; экспорт - тоже только туда.

Поиск шрифтов подменяется: папка шрифтов пользователя - пустая временная, а
системных папок нет.  Так тест не зависит от того, установлен ли Morfin Sans на
машине, и плашка «заголовок набран заменой» обязана появиться.
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import numpy as np
    from PIL import Image
except Exception as exc:                        # pragma: no cover
    raise unittest.SkipTest("нет numpy/Pillow: %s" % exc)


def synthetic_photo(w: int = 1200, h: int = 800) -> Image.Image:
    """Кадр «человек у стены»: градиентный фон и светлый прямоугольник-лицо."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 40 + 80 * (xx / w) + 30 * np.sin(yy / 29.0)
    arr = np.repeat(base[..., None], 3, axis=2)
    arr[..., 0] += 25
    arr[int(h * 0.2):int(h * 0.5), int(w * 0.55):int(w * 0.7)] = (205, 170, 150)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def make_root():
    """Спрятанное корневое окно или SkipTest."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        raise unittest.SkipTest("Tk недоступен: %s" % exc)
    root.withdraw()
    return root


def pump(root, until=lambda: False, timeout: float = 30.0) -> bool:
    """Крутить события Tk, пока until() не станет истинным.  False - таймаут."""
    import tkinter as tk
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return bool(until())
        if until():
            return True
        time.sleep(0.01)
    return bool(until())


class TabPosterTestCase(unittest.TestCase):
    """Общая подготовка: модуль, временная папка, окно, вкладка."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest("tkinter недоступен: %s" % exc)
        import gui_common
        import poster
        import tab_poster
        cls.gc, cls.poster, cls.tp = gui_common, poster, tab_poster

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cr2_tab_poster_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fonts_dir = self.tmp / "user_fonts"
        fonts_dir.mkdir()
        for target, value in (("user_fonts_dir", fonts_dir), ("system_font_dirs", [])):
            p = mock.patch.object(self.poster, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.root = make_root()
        self.tk_errors: list[BaseException] = []
        self.root.report_callback_exception = \
            lambda t, e, tb: self.tk_errors.append(e)
        self.addCleanup(self._destroy_root)
        self.settings: dict = {}
        self.ctx = self.gc.AppContext(self.root, settings=self.settings, scale=1.0,
                                      record_error=lambda where, text: None)
        from tkinter import ttk
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self.root.geometry("1100x800")

    def _destroy_root(self) -> None:
        try:
            self.ctx.shutdown()
            self.root.destroy()
        except Exception:
            pass
        gc.collect()        # мусор Tk - в потоке Tk, см. test_shell._destroy_root

    def build(self):
        frame = self.tp.build_tab(self.nb, self.ctx)
        self.nb.add(frame, text=self.tp.TAB_TITLE)
        self.root.update()
        return frame, frame.poster_tab

    def photo_file(self, name: str = "frame.jpg") -> Path:
        path = self.tmp / "photos" / name
        path.parent.mkdir(exist_ok=True)
        synthetic_photo().save(path, "JPEG", quality=90)
        return path

    def wait_preview(self, tab) -> None:
        ok = pump(self.root, lambda: tab._debounce_id is None
                  and tab._preview_job is None
                  and tab.shown_generation == tab.generation)
        self.assertTrue(ok, "предпросмотр не дорисовался")

    def wait_export(self, tab) -> None:
        ok = pump(self.root, lambda: tab._export_job is not None
                  and tab._export_job.finished, timeout=120.0)
        self.assertTrue(ok, "экспорт не закончился")


class TestBuild(TabPosterTestCase):

    def test_builds_as_child_of_notebook(self):
        frame, tab = self.build()
        self.assertEqual(self.tp.TAB_TITLE, "Афиши")
        self.assertIs(frame.master, self.nb)
        titles = list(tab.template_combo.cget("values"))
        self.assertEqual(titles, [t for _, t in self.poster.list_templates()])
        self.wait_preview(tab)
        self.assertEqual(self.tk_errors, [])

    def test_switching_templates_rebuilds_form_and_remembers_fields(self):
        _frame, tab = self.build()
        self.assertEqual(tab.template_id, "afisha_a3")
        tab.select_template("post")
        tab.set_field("headline", "Моя лекция")
        tab.select_template("quote")
        self.assertEqual(tab.form_keys(),
                         [s.key for s in self.poster.template_fields("quote")
                          if s.kind == "text"])
        self.assertIn("quote", tab._texts)             # многострочное поле
        self.assertNotIn("headline", tab.form_keys())
        tab.select_template("post")
        self.assertEqual(tab.field_values()["headline"], "Моя лекция")
        st = self.ctx.tab_settings("poster")
        self.assertEqual(st["template"], "post")
        self.assertEqual(st["fields"]["post"]["headline"], "Моя лекция")

    def test_fields_restored_from_settings_in_new_tab(self):
        st = self.ctx.tab_settings("poster")
        st["template"] = "post"
        st["fields"] = {"post": {"headline": "Из прошлого запуска", "subline": ""}}
        _frame, tab = self.build()
        self.assertEqual(tab.template_id, "post")
        values = tab.field_values()
        self.assertEqual(values["headline"], "Из прошлого запуска")
        self.assertEqual(values["subline"], "")          # пустое осталось пустым
        example = self.poster.example_fields("post")
        for key in set(tab.form_keys()) - {"headline", "subline"}:
            self.assertEqual(values[key], example.get(key, ""))

    def test_new_template_carries_shared_fields(self):
        _frame, tab = self.build()
        tab.set_field("headline", "Неделя юриста")
        tab.select_template("stories")
        self.assertEqual(tab.field_values()["headline"], "Неделя юриста")

    def test_pdf_only_for_a3(self):
        _frame, tab = self.build()
        tab.format_var.set("pdf")
        self.assertNotIn("disabled", tab.format_buttons["pdf"].state())
        tab.select_template("post")
        self.assertIn("disabled", tab.format_buttons["pdf"].state())
        self.assertEqual(tab.format_var.get(), "png")


class TestPreview(TabPosterTestCase):

    def test_preview_renders_from_synthetic_photo(self):
        _frame, tab = self.build()
        tab.select_template("post")
        tab.set_photo(self.photo_file())
        tab.set_focus((0.62, 0.35))
        self.wait_preview(tab)
        self.assertEqual(tab.preview_error, "")
        im = tab.preview_image
        self.assertIsNotNone(im)
        W, H = self.poster.TEMPLATES["post"]["size"]
        self.assertLess(max(im.size), max(W, H))            # уменьшенный масштаб
        self.assertAlmostEqual(im.width / im.height, W / H, delta=0.02)
        self.assertEqual(tab.last_report.template_id, "post")
        self.assertTrue(tab.last_report.photo_boxes)
        self.assertFalse(any("заглушка" in w for w in tab.last_warnings))
        for key in ("cream", "graphite", "crimson", "sand", "photo"):
            self.assertIn(key, tab.last_usage)
        self.assertTrue(tab.usage_canvas.find_all())         # полоски нарисованы
        self.assertTrue(tab.preview_canvas.find_all())
        pump(self.root, lambda: tab._focus_thumb is not None)
        self.assertIsNotNone(tab._focus_thumb)
        self.assertEqual(self.tk_errors, [])

    def test_fast_typing_shows_only_latest_generation(self):
        _frame, tab = self.build()
        self.wait_preview(tab)
        for i in range(8):
            tab.set_field("headline", "Заголовок %d" % i)
            self.root.update()
        self.wait_preview(tab)
        self.assertEqual(tab.shown_generation, tab.generation)
        self.assertEqual(tab.field_values()["headline"], "Заголовок 7")

    def test_missing_required_field_is_reported_not_crashed(self):
        _frame, tab = self.build()
        tab.set_field("headline", "")
        self.wait_preview(tab)
        self.assertIn("Заголовок", tab.preview_error)
        self.assertEqual(self.tk_errors, [])

    def test_substitute_font_banner_appears_without_morfin(self):
        _frame, tab = self.build()
        self.wait_preview(tab)
        self.assertTrue(tab.banner_visible())
        text = tab.banner_label.cget("text")
        self.assertIn("Oswald", text)
        self.assertIn("Morfin Sans", text)
        self.assertIn("Morfin Sans", tab.banner_btn.cget("text"))
        # Файл, который не Morfin Sans, запоминается, но плашка не пропадает.
        oswald = self.poster.bundled_fonts_dir() / "Oswald-Bold.ttf"
        tab.set_display_font(oswald)
        self.wait_preview(tab)
        self.assertEqual(self.ctx.tab_settings("poster")["display_font"], str(oswald))
        self.assertEqual(tab.last_report.fonts["display"].source, "user")
        self.assertTrue(tab.banner_visible())
        tab.set_display_font(None)
        self.assertNotIn("display_font", self.ctx.tab_settings("poster"))

    def test_selection_strip_picks_photo(self):
        _frame, tab = self.build()
        paths = [self.photo_file("a.jpg"), self.photo_file("b.jpg")]
        self.ctx.set_selection(paths)
        self.assertIn("(2)", tab.from_sel_btn.cget("text"))
        tab.show_selection_strip()
        self.assertTrue(pump(self.root, lambda: len(tab._strip_items) == 2))
        tab.set_photo(tab._strip_items[1][0])
        self.assertEqual(tab.photo_path, paths[1])
        self.wait_preview(tab)
        self.assertEqual(self.tk_errors, [])


class TestFacesProcessedAndTheme(TabPosterTestCase):

    FACE = (0.55, 0.2, 0.15, 0.3)          # «лицо» synthetic_photo, в долях кадра

    def test_face_from_cull_sets_focus_and_zoom(self):
        path = self.photo_file("face.jpg")
        self.ctx.set_photo_hints({path: {"face_box": self.FACE}})
        _frame, tab = self.build()
        tab.select_template("post")
        tab.set_photo(path)
        self.assertTrue(pump(self.root, lambda: tab._focus_thumb is not None and tab.face))
        self.assertEqual(tab.face, self.FACE)
        self.assertAlmostEqual(tab.focus[0], 0.625, places=3)
        self.assertAlmostEqual(tab.focus[1], 0.35, places=3)
        thumb = tab._focus_thumb
        want = self.tp.auto_zoom("post", thumb.width, thumb.height, self.FACE)
        self.assertAlmostEqual(tab.zoom, want, places=3)
        self.assertGreater(tab.zoom, 1.0)
        self.wait_preview(tab)
        self.assertEqual(self.tk_errors, [])

    def test_click_on_a_found_face_snaps_to_it(self):
        _frame, tab = self.build()
        tab.select_template("post")
        tab.set_photo(self.photo_file())
        self.assertTrue(pump(self.root, lambda: tab._focus_thumb is not None))
        tab.faces = [self.FACE]
        x0, y0, w, h = tab._focus_geom
        inside = types.SimpleNamespace(x=x0 + int(w * 0.62), y=y0 + int(h * 0.3))
        tab._on_focus_click(inside)
        self.assertEqual(tab.face, self.FACE)
        self.assertGreater(tab.zoom, 1.0)
        outside = types.SimpleNamespace(x=x0 + int(w * 0.1), y=y0 + int(h * 0.9))
        tab._on_focus_click(outside)
        self.assertIsNone(tab.face)
        self.assertAlmostEqual(tab.focus[0], 0.1, delta=0.02)
        self.wait_preview(tab)

    def test_strip_prefers_the_processed_file_and_says_so(self):
        _frame, tab = self.build()
        originals = [self.photo_file("a.jpg"), self.photo_file("b.jpg")]
        done = self.tmp / "обработка" / "a.jpg"
        done.parent.mkdir()
        synthetic_photo().save(done, "JPEG", quality=90)
        self.ctx.set_selection(originals)
        self.ctx.publish_processed({originals[0]: done})
        tab.show_selection_strip()
        self.assertTrue(pump(self.root, lambda: len(tab._strip_items) == 2))
        self.assertEqual([item[0] for item in tab._strip_items], [done, originals[1]])
        self.assertIn("Обработанных: 1", tab.strip_status.cget("text"))
        tab._on_strip_click(types.SimpleNamespace(x=self.ctx.px(4) + 2, y=5))
        self.assertEqual(tab.photo_path, done)
        self.assertEqual(tab.photo_origin, originals[0])
        self.assertIn("обработанный", tab.photo_label.cget("text"))
        self.wait_preview(tab)

    def test_theme_change_recolours_the_tab(self):
        _frame, tab = self.build()
        tab.select_template("quote")
        self.wait_preview(tab)
        with mock.patch.object(self.gc, "is_dark_mode", return_value=True):
            tab.frame.event_generate("<<ThemeChanged>>")
            self.root.update()
            dark = lambda role: self.gc.palette(role, dark=True)     # noqa: E731
            self.assertEqual(str(tab.fonts_label.cget("foreground")), dark("muted"))
            self.assertEqual(str(tab.banner.cget("background")), dark("card_bg"))
            self.assertEqual(str(tab.strip_canvas.cget("background")), dark("card_bg"))
            quote = tab._texts["quote"]
            self.assertEqual(str(quote.cget("background")), dark("card_bg"))
            self.assertEqual(str(quote.cget("foreground")), dark("fg"))
        self.assertEqual(self.tk_errors, [])

    def test_quote_field_uses_the_entry_font_not_courier(self):
        _frame, tab = self.build()
        tab.select_template("quote")
        font = str(tab._texts["quote"].cget("font"))
        self.assertEqual(font, tab._entry_font())
        self.assertNotIn("courier", font.lower())

    def test_photo_panel_comes_before_the_text_form(self):
        _frame, tab = self.build()
        photo_row = int(tab.photo_label.master.grid_info()["row"])
        form_row = int(tab.form_box.grid_info()["row"])
        self.assertLess(photo_row, form_row)


class TestExport(TabPosterTestCase):

    def test_export_png_writes_full_size_file(self):
        _frame, tab = self.build()
        tab.select_template("post")
        tab.set_photo(self.photo_file())
        out = self.tmp / "out" / "post"
        job = tab.export(out, "png")
        self.assertIsNotNone(job)
        self.wait_export(tab)
        self.assertEqual(len(tab.last_export_paths), 1)
        path = tab.last_export_paths[0]
        self.assertEqual(path.suffix, ".png")
        with Image.open(path) as im:
            self.assertEqual(im.size, self.poster.TEMPLATES["post"]["size"])
        self.assertEqual(self.tk_errors, [])

    def test_export_a3_pdf(self):
        _frame, tab = self.build()
        self.assertEqual(tab.template_id, "afisha_a3")
        tab.export(self.tmp / "out" / "afisha.pdf", "pdf")
        self.wait_export(tab)
        path = tab.last_export_paths[0]
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))

    def test_export_all_formats(self):
        _frame, tab = self.build()
        tab.select_template("post")
        tab.set_field("headline", "Итоги недели")
        tab.set_photo(self.photo_file())
        folder = self.tmp / "social"
        folder.mkdir()
        self.assertIsNotNone(tab.export_all(folder, "jpeg"))
        self.wait_export(tab)
        sizes = {}
        for p in tab.last_export_paths:
            self.assertEqual(p.parent, folder)
            with Image.open(p) as im:
                self.assertEqual(im.format, "JPEG")
                sizes[im.size] = p.name
        self.assertEqual(set(sizes), {self.poster.TEMPLATES[t]["size"]
                                      for t in self.tp.SOCIAL_TEMPLATES})
        self.assertTrue(all(n.startswith("Итоги_недели_") for n in sizes.values()))

    def test_export_refuses_the_photo_folder_and_foreign_images(self):
        _frame, tab = self.build()
        src = self.photo_file("src.jpg")
        neighbour = self.photo_file("neighbour.jpg")
        before = neighbour.read_bytes()
        tab.select_template("post")
        tab.set_photo(src)
        self.assertIsNone(tab.export(src.parent / "афиша.png", "png"))
        self.assertIn("папку со снимками", tab.export_status.cget("text"))
        self.assertIsNone(tab.export(src.parent / "подпапка" / "афиша.png", "png"))
        # Чужой снимок в другой папке тоже не затирается.
        other = self.tmp / "other"
        other.mkdir()
        foreign = other / "IMG_0101.jpg"
        foreign.write_bytes(before)
        self.assertIsNone(tab.export(foreign, "jpeg"))
        self.assertIn("уже есть", tab.export_status.cget("text"))
        self.assertEqual(foreign.read_bytes(), before)
        # А свою прошлую афишу заменить можно.
        mine = other / "post.png"
        tab.export(mine, "png")
        self.wait_export(tab)
        self.assertTrue(mine.exists())
        self.assertIsNotNone(tab.export(mine, "png"))
        self.wait_export(tab)
        self.assertEqual(neighbour.read_bytes(), before)

    def test_export_refuses_to_overwrite_source_photo(self):
        _frame, tab = self.build()
        src = self.photo_file("src.jpg")
        tab.select_template("post")
        tab.set_photo(src)
        before = src.read_bytes()
        self.assertIsNone(tab.export(src, "jpeg"))
        self.assertIn("исходного снимка", tab.export_status.cget("text"))
        self.assertEqual(src.read_bytes(), before)


class TestPureHelpers(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tab_poster
        except Exception as exc:
            raise unittest.SkipTest("tab_poster не загружается: %s" % exc)
        import poster
        cls.tp, cls.poster = tab_poster, poster

    def test_focus_box_without_zoom_keeps_full_crop(self):
        w, h = 2000, 800            # шире блока фото: есть куда сдвигать кроп
        box = self.tp.focus_face_box("post", w, h, (0.9, 0.3), 1.0)
        self.assertTrue(0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h)
        block = self.tp._image_block("post")
        W, H = self.poster.TEMPLATES["post"]["size"]
        bw, bh = int(block["rect"][2] * W), int(block["rect"][3] * H)
        plain = self.poster.cover_crop_box(w, h, bw, bh)
        focused = self.poster.cover_crop_box(w, h, bw, bh, face_box=box,
                                             face_fill=block["face_fill"],
                                             max_zoom=block["max_zoom"])
        self.assertAlmostEqual(focused[3] - focused[1], plain[3] - plain[1], delta=2)
        self.assertGreater(focused[0], plain[0])            # сдвинут к фокусу
        self.assertIsNone(self.tp.focus_face_box("post", w, h, None))

    def test_focus_zoom_narrows_crop(self):
        w, h = 1200, 800
        block = self.tp._image_block("post")
        W, H = self.poster.TEMPLATES["post"]["size"]
        bw, bh = int(block["rect"][2] * W), int(block["rect"][3] * H)
        plain = self.poster.cover_crop_box(w, h, bw, bh)
        for zoom in (1.0, 2.0):
            box = self.tp.focus_face_box("post", w, h, (0.5, 0.5), zoom)
            crop = self.poster.cover_crop_box(w, h, bw, bh, face_box=box,
                                              face_fill=block["face_fill"],
                                              max_zoom=block["max_zoom"])
            self.assertAlmostEqual((plain[2] - plain[0]) / (crop[2] - crop[0]),
                                   zoom, delta=0.02)

    def test_face_zoom_never_cuts_the_head(self):
        w, h = 1200, 800
        block = self.tp._image_block("post")
        W, H = self.poster.TEMPLATES["post"]["size"]
        bw, bh = int(block["rect"][2] * W), int(block["rect"][3] * H)
        for face in ((0.55, 0.2, 0.08, 0.12), (0.4, 0.1, 0.2, 0.35), (0.05, 0.02, 0.1, 0.15)):
            for zoom in (1.0, 1.8, 2.5, None):
                with self.subTest(face=face, zoom=zoom):
                    z = self.tp.auto_zoom("post", w, h, face) if zoom is None else zoom
                    box = self.tp.focus_face_box("post", w, h, None, z, face)
                    crop = self.poster.cover_crop_box(w, h, bw, bh, face_box=box,
                                                      face_fill=block["face_fill"],
                                                      max_zoom=block["max_zoom"])
                    hl, ht, hr, hb = self.tp._head_px(face, w, h)
                    self.assertLessEqual(crop[0], hl + 1)
                    self.assertGreaterEqual(crop[2], hr - 1)
                    self.assertLessEqual(crop[1], ht + 1)
                    self.assertGreaterEqual(crop[3], hb - 1)

    def test_auto_zoom_makes_a_small_face_fill_its_share(self):
        w, h = 1200, 800
        face = (0.45, 0.3, 0.06, 0.09)
        block = self.tp._image_block("post")
        W, H = self.poster.TEMPLATES["post"]["size"]
        bw, bh = int(block["rect"][2] * W), int(block["rect"][3] * H)
        z = self.tp.auto_zoom("post", w, h, face)
        self.assertGreater(z, 1.5)
        box = self.tp.focus_face_box("post", w, h, None, z, face)
        crop = self.poster.cover_crop_box(w, h, bw, bh, face_box=box,
                                          face_fill=block["face_fill"],
                                          max_zoom=block["max_zoom"])
        share = face[2] * w / (crop[2] - crop[0])
        # 72 px face in a 1200 px frame: max_zoom 2.5 is the limit, 0.06 -> 0.15.
        self.assertAlmostEqual(z, block["max_zoom"], places=3)
        self.assertAlmostEqual(share, face[2] * block["max_zoom"], delta=0.005)
        self.assertLessEqual(share, block["face_fill"] + 0.02)

    def test_export_problem(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shoot = root / "Съёмка"
            shoot.mkdir()
            photo = shoot / "IMG_0001.JPG"
            photo.write_bytes(b"x")
            other = root / "Афиши"
            other.mkdir()
            ok = other / "post.png"
            self.assertEqual(self.tp.export_problem(ok, [photo], [shoot]), "")
            self.assertIn("исходного снимка",
                          self.tp.export_problem(shoot / "img_0001.jpg", [photo], [shoot]))
            self.assertIn("папку со снимками",
                          self.tp.export_problem(shoot / "x" / "post.png", [photo], [shoot]))
            (other / "IMG_0101.jpg").write_bytes(b"camera")
            self.assertIn("уже есть",
                          self.tp.export_problem(other / "IMG_0101.jpg", [photo], [shoot]))
            ok.write_bytes(b"old poster")
            key = self.tp.cr2_core._dst_key(ok)
            self.assertEqual(self.tp.export_problem(ok, [photo], [shoot], {key}), "")
            self.assertIn("уже есть", self.tp.export_problem(ok, [photo], [shoot]))
            (other / "notes.pdf").write_bytes(b"%PDF")
            self.assertEqual(self.tp.export_problem(other / "notes.pdf", [photo], [shoot]), "")
            self.assertNotEqual(os.sep, "")

    def test_file_names(self):
        name = self.tp.export_file_name("post", {"headline": 'Лекция: "Право" / 2026?'},
                                        "jpeg")
        self.assertEqual(name, "Лекция_Право_2026_post.jpg")
        self.assertEqual(self.tp.export_file_name("quote", {}, "png"), "poster_quote.png")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.png"
            self.assertEqual(self.tp.unique_path(p), p)
            p.write_bytes(b"x")
            self.assertEqual(self.tp.unique_path(p).name, "a (2).png")


if __name__ == "__main__":
    unittest.main()
