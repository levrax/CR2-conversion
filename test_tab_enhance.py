# -*- coding: utf-8 -*-
"""Тесты вкладки «Обработка» (tab_enhance.py).

Окно не показывается: корневое окно сразу прячется (withdraw), события
прокачиваются root.update().  Без Tk (нет tcl/tk, нет дисплея) тесты, которым
нужно окно, пропускаются, а не падают.  Снимки - только синтетические,
записанные во временную папку; результат пакета - тоже только туда.

Там, где важен порядок событий в потоках (устаревший предпросмотр, отмена
пакета), функция движка подменяется обёрткой с «воротами» threading.Event:
тест сам решает, когда рабочий поток идёт дальше, и не зависит от скорости
машины CI.
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import threading
import time
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


def synthetic_photo(w: int = 900, h: int = 600, seed: int = 0) -> Image.Image:
    """Тёмный «зал»: градиент, полосы и светлое пятно-лицо."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 25 + 60 * (xx / w) + 15 * np.sin((yy + seed * 7) / 23.0)
    arr = np.repeat(base[..., None], 3, axis=2)
    arr[..., 2] += 12                          # лёгкий синий оттенок
    arr[int(h * 0.25):int(h * 0.5), int(w * 0.4):int(w * 0.55)] = (150, 115, 95)
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


class TestHelpers(unittest.TestCase):
    """Функции без Tk: настройки, суффикс, переименование."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter  # noqa: F401
            import tab_enhance
        except Exception as exc:
            raise unittest.SkipTest("tab_enhance не импортируется: %s" % exc)
        import enhance
        cls.te, cls.enhance = tab_enhance, enhance

    def test_params_round_trip(self):
        p = self.enhance.Params(enhance_strength=0.35, filter_id="warm",
                                filter_strength=0.7, white_balance=False, vibrance=True)
        d = self.te.params_to_settings(p)
        self.assertEqual(d["enhance_strength"], 35)
        self.assertEqual(self.te.params_from_settings(d), p)

    def test_params_from_garbage_falls_back_to_engine_defaults(self):
        p = self.te.params_from_settings({"enhance_strength": "abc", "filter_id": "нет-такого",
                                          "filter_strength": 900, "white_balance": "да",
                                          "extra": 1})
        d = self.enhance.Params()
        self.assertEqual(p.enhance_strength, d.enhance_strength)
        self.assertEqual(p.filter_id, "none")
        self.assertEqual(p.filter_strength, 1.0)
        self.assertEqual(p.white_balance, d.white_balance)
        self.assertEqual(self.te.params_from_settings(None), d.validated())

    def test_clean_suffix(self):
        self.assertEqual(self.te.clean_suffix("_обр"), "_обр")
        self.assertEqual(self.te.clean_suffix(' a/b:c*? '), "abc")
        self.assertEqual(self.te.clean_suffix(".jpg."), "jpg")
        self.assertEqual(len(self.te.clean_suffix("x" * 200)), self.te.SUFFIX_MAX)

    def test_default_out_dir_is_next_to_the_shoot_not_inside(self):
        src = Path("съёмки") / "Лекция" / "IMG_0001.JPG"
        want = Path(os.path.abspath("съёмки")) / ("Лекция" + self.te.DEFAULT_OUT_SUFFIX)
        self.assertEqual(self.te.default_out_dir([src]), want)
        self.assertFalse(self.te.inside_folder(want, src.parent))
        self.assertIsNone(self.te.default_out_dir([]))
        root_photo = Path(os.path.abspath(os.sep)) / "IMG_0001.JPG"
        self.assertIsNone(self.te.default_out_dir([root_photo]))

    def test_inside_folder(self):
        base = Path(os.path.abspath("photos"))
        self.assertTrue(self.te.inside_folder(base, base))
        self.assertTrue(self.te.inside_folder(base / "Обработка", base))
        self.assertFalse(self.te.inside_folder(base.parent / "photos — обработка", base))
        self.assertFalse(self.te.inside_folder(base.parent, base))

    def test_prefer_jpeg_drops_only_paired_raws(self):
        a = Path("shoot")
        paths = [a / "IMG_0001.CR2", a / "IMG_0001.JPG", a / "IMG_0002.CR2",
                 a / "img_0003.cr2", a / "IMG_0003.jpeg", Path("other") / "IMG_0001.CR2",
                 a / "IMG_0004.png", a / "IMG_0004.CR2"]
        kept, dropped = self.te.prefer_jpeg(paths)
        self.assertEqual(dropped, 2)
        self.assertEqual(kept, [a / "IMG_0001.JPG", a / "IMG_0002.CR2", a / "IMG_0003.jpeg",
                                Path("other") / "IMG_0001.CR2", a / "IMG_0004.png",
                                a / "IMG_0004.CR2"])


class TabEnhanceTestCase(unittest.TestCase):
    """Общая подготовка: модуль, временная папка, окно, вкладка."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import tkinter  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest("tkinter недоступен: %s" % exc)
        import enhance
        import gui_common
        import tab_enhance
        cls.gc, cls.enhance, cls.te = gui_common, enhance, tab_enhance

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cr2_tab_enhance_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = make_root()
        self.tk_errors: list[BaseException] = []
        self.root.report_callback_exception = \
            lambda t, e, tb: self.tk_errors.append(e)
        self.addCleanup(self._destroy_root)
        self.settings: dict = {}
        self.saved = 0

        def save(_data: dict) -> None:
            self.saved += 1

        self.ctx = self.gc.AppContext(self.root, settings=self.settings, scale=1.0,
                                      save_settings=save,
                                      record_error=lambda where, text: None,
                                      reveal=lambda path: None)
        from tkinter import ttk
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self.root.geometry("1200x820")

    def tearDown(self) -> None:
        self.assertEqual(self.tk_errors, [], "исключения в колбэках Tk")

    def _destroy_root(self) -> None:
        try:
            self.ctx.shutdown()
            for job in list(self.ctx._jobs):          # noqa: SLF001
                job.join(10)
            self.root.destroy()
        except Exception:
            pass
        gc.collect()        # мусор Tk - в потоке Tk, см. test_shell._destroy_root

    def build(self):
        frame = self.te.build_tab(self.nb, self.ctx)
        self.nb.add(frame, text=self.te.TAB_TITLE)
        self.root.update()
        return frame, frame.enhance_tab

    def photos(self, n: int = 3, folder: str = "src") -> list[Path]:
        d = self.tmp / folder
        d.mkdir(exist_ok=True)
        out = []
        for i in range(n):
            p = d / ("IMG_%04d.JPG" % (i + 1))
            synthetic_photo(seed=i).save(p, "JPEG", quality=90)
            out.append(p)
        return out

    def wait_preview(self, tab, timeout: float = 30.0) -> None:
        ok = pump(self.root, lambda: tab._debounce_id is None
                  and tab._preview_job is None
                  and tab.shown_generation == tab.generation, timeout)
        self.assertTrue(ok, "предпросмотр не дорисовался")

    def wait_batch(self, tab, timeout: float = 120.0) -> None:
        ok = pump(self.root, lambda: tab._batch_job is None, timeout)
        self.assertTrue(ok, "пакет не закончился")


class TestBuild(TabEnhanceTestCase):

    def test_builds_as_child_of_notebook(self):
        frame, tab = self.build()
        self.assertEqual(self.te.TAB_TITLE, "Обработка")
        self.assertIs(frame.master, self.nb)
        self.assertEqual(self.nb.tab(frame, "text"), "Обработка")
        self.assertEqual(tuple(tab.filter_combo.cget("values")),
                         tuple(t for _i, t in self.enhance.filter_choices()))
        self.assertEqual(tab.params(), self.enhance.Params().validated())
        self.assertTrue(tab.run_btn.instate(["disabled"]), "без снимков кнопка не активна")
        self.assertTrue(tab.cancel_btn.instate(["disabled"]))
        self.assertIsInstance(self.settings["tabs"]["enhance"]["presets"], dict)

    def test_restores_saved_settings(self):
        self.settings["tabs"] = {"enhance": {
            "enhance_strength": 25, "filter_id": "bw", "filter_strength": 40,
            "white_balance": False, "vibrance": False, "quality": 80,
            "suffix": "_ч/б", "keep_exif": False, "view": "side"}}
        _frame, tab = self.build()
        p = tab.params()
        self.assertEqual((p.enhance_strength, p.filter_id, p.filter_strength,
                          p.white_balance, p.vibrance), (0.25, "bw", 0.4, False, False))
        self.assertEqual(tab.quality(), 80)
        tab.quality_var.set("5")
        self.assertEqual(tab.quality(), self.te.QUALITY_MIN)
        tab.quality_var.set("abc")
        self.assertEqual(tab.quality(), self.te.DEFAULT_QUALITY)
        self.assertEqual(tab.suffix_var.get(), "_чб")
        self.assertFalse(tab.exif_var.get())
        self.assertEqual(tab.view_var.get(), self.te.VIEW_SIDE)

    def test_selection_button_follows_ctx_selection(self):
        _frame, tab = self.build()
        self.assertTrue(tab.take_btn.instate(["disabled"]))
        paths = self.photos(2)
        self.ctx.set_selection(paths)
        self.assertTrue(tab.take_btn.instate(["!disabled"]))
        self.assertIn("(2)", tab.take_text.get())
        self.assertEqual(tab.take_selection(), 2)
        self.assertEqual(tab.sources, paths)
        self.assertEqual(Path(tab.out_var.get()), self.te.default_out_dir(paths))
        self.assertEqual(Path(tab.out_var.get()).parent, paths[0].parent.parent)
        self.wait_preview(tab)

    def test_raw_jpeg_pair_is_one_photo(self):
        paths = self.photos(2)
        import make_test_cr2
        make_test_cr2.make_cr2(paths[0].with_suffix(".CR2"), preview_size=(480, 320),
                               raw_size=(480, 320))
        lonely = make_test_cr2.make_cr2(paths[0].parent / "IMG_0009.CR2",
                                        preview_size=(480, 320), raw_size=(480, 320))
        _frame, tab = self.build()
        self.assertEqual(tab.load_folder(paths[0].parent), 3)
        self.assertEqual(sorted(p.name for p in tab.sources),
                         ["IMG_0001.JPG", "IMG_0002.JPG", lonely.name])
        self.assertIn("CR2 пропущено: 1", tab.source_text.get())
        self.wait_preview(tab)

    def test_load_folder_filters_non_images(self):
        paths = self.photos(2)
        (paths[0].parent / "notes.txt").write_text("x", encoding="utf-8")
        _frame, tab = self.build()
        self.assertEqual(tab.load_folder(paths[0].parent), 2)
        self.assertEqual(len(tab.tree.get_children()), 2)
        self.wait_preview(tab)


class TestPreview(TabEnhanceTestCase):

    def test_preview_completes_and_updates_canvas(self):
        paths = self.photos(1)
        _frame, tab = self.build()
        tab.set_sources(paths)
        self.wait_preview(tab)
        self.assertEqual(tab.preview_error, "")
        self.assertIsNotNone(tab.preview_after)
        self.assertEqual(tab.preview_before.size, tab.preview_after.size)
        before = np.asarray(tab.preview_before, dtype=np.float32)
        after = np.asarray(tab.preview_after, dtype=np.float32)
        self.assertGreater(after.mean(), before.mean(), "тёмный кадр должен посветлеть")
        # Картинки на холсте есть, и ссылки на PhotoImage держит вкладка.
        images = tab.canvas.find_withtag("after") + tab.canvas.find_withtag("before")
        self.assertEqual(len(images), 2)
        self.assertIsNotNone(tab._after_tk)
        self.assertIsNotNone(tab._split_tk)
        self.assertEqual(tab.canvas.itemcget("after", "image"), str(tab._after_tk))

        tab.set_split(0.3)
        self.root.update()
        _x0, _y0, w, _h = tab._split_geom
        self.assertEqual(int(tab._split_tk.cget("width")), round(w * 0.3))

        tab.set_view(self.te.VIEW_SIDE)
        self.wait_preview(tab)
        kinds = [tab.canvas.type(i) for i in tab.canvas.find_all()]
        self.assertEqual(kinds.count("image"), 2)
        self.assertEqual(self.settings["tabs"]["enhance"]["view"], "side")

    def test_bad_file_shows_error_instead_of_crashing(self):
        bad = self.tmp / "broken.jpg"
        bad.write_bytes(b"not a jpeg at all")
        _frame, tab = self.build()
        tab.set_sources([bad])
        self.wait_preview(tab)
        self.assertIn("broken.jpg", tab.preview_error)
        self.assertIsNone(tab.preview_after)

    def test_stale_preview_is_dropped(self):
        paths = self.photos(1)
        _frame, tab = self.build()
        entered = threading.Event()
        gate = threading.Event()
        seen: list[float] = []
        real = self.enhance.preview

        def gated(img, params=None, max_side=1400):
            seen.append(params.enhance_strength)
            entered.set()
            gate.wait(20)
            return real(img, params, max_side)

        with mock.patch.object(self.te.enhance, "preview", side_effect=gated):
            tab.set_sources(paths)
            self.assertTrue(pump(self.root, entered.is_set), "предпросмотр не начался")
            tab.set_params({"enhance_strength": 0.2})
            # Задержка отработала, но второй расчёт не стартует, пока идёт первый.
            pump(self.root, lambda: tab._debounce_id is None)
            self.assertEqual(len(seen), 1)
            gate.set()
            self.wait_preview(tab)
        self.assertGreaterEqual(tab.dropped_previews, 1)
        # Первый расчёт - со старыми настройками, все последующие - только с новыми
        # (лишний пересчёт после изменения размера холста на медленной машине
        # допустим, устаревший - нет).
        self.assertEqual(seen[0], 0.6)
        self.assertEqual(set(seen[1:]), {0.2})
        self.assertIn("20 %", tab.preview_status.cget("text"))

    def test_slider_drag_is_debounced(self):
        paths = self.photos(1)
        _frame, tab = self.build()
        tab.set_sources(paths)
        self.wait_preview(tab)
        calls: list[float] = []
        real = self.enhance.preview

        def counted(img, params=None, max_side=1400):
            calls.append(params.enhance_strength)
            return real(img, params, max_side)

        # Задержка побольше: на медленной машине CI десять движений не должны
        # разъехаться дальше паузы - проверяется склейка, а не скорость раннера.
        with mock.patch.object(self.te.enhance, "preview", side_effect=counted),                 mock.patch.object(self.te, "DEBOUNCE_MS", 600):
            for v in range(10, 60, 5):          # «тянем» ползунок: 10 движений
                tab.enhance_var.set(v)
                self.root.update()
            self.wait_preview(tab)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(calls[-1], 0.55)


class TestPresets(TabEnhanceTestCase):

    def test_save_apply_delete(self):
        _frame, tab = self.build()
        tab.set_params({"enhance_strength": 0.3, "filter_id": "bw", "filter_strength": 0.5})
        self.assertFalse(tab.save_preset("   "))
        self.assertTrue(tab.save_preset("Зал вечером"))
        stored = self.settings["tabs"]["enhance"]["presets"]["Зал вечером"]
        self.assertEqual(stored["filter_id"], "bw")
        self.assertEqual(stored["enhance_strength"], 30)
        self.assertGreater(self.saved, 0)

        tab.reset_params()
        self.assertEqual(tab.params().filter_id, "none")
        self.assertTrue(tab.apply_preset("Зал вечером"))
        self.assertEqual(tab.params().filter_id, "bw")
        self.assertEqual(tab.filter_var.get(), self.enhance.FILTERS["bw"].title)

        tab.reset_params()
        tab._ask = lambda title, text: False
        self.assertFalse(tab.save_preset("Зал вечером"))            # не заменили
        self.assertEqual(self.settings["tabs"]["enhance"]["presets"]["Зал вечером"]["filter_id"],
                         "bw")
        self.assertFalse(tab.delete_preset("Зал вечером"))          # не подтвердили
        self.assertTrue(tab.delete_preset("Зал вечером", confirm=True))
        self.assertEqual(tab.preset_names(), [])
        self.assertFalse(tab.apply_preset("Зал вечером"))

    def test_preset_with_a_removed_filter_falls_back_to_none(self):
        # Пресет из прошлой версии с фильтром «Плёнка + зерно», которого больше нет.
        self.settings["tabs"] = {"enhance": {"filter_id": "film", "presets": {
            "Старый": {"enhance_strength": 40, "filter_id": "film", "filter_strength": 70}}}}
        _frame, tab = self.build()
        self.assertEqual(tab.params().filter_id, "none")
        self.assertTrue(tab.apply_preset("Старый"))
        self.assertEqual(tab.params().filter_id, "none")
        self.assertEqual(tab.params().enhance_strength, 0.4)


class TestBatch(TabEnhanceTestCase):

    def test_batch_processes_three_files(self):
        paths = self.photos(3)
        originals = {p: p.read_bytes() for p in paths}
        out = self.tmp / "out"
        _frame, tab = self.build()
        tab.set_sources(paths)
        tab._set_out(str(out), auto=False)
        tab.set_params({"enhance_strength": 0.5, "filter_id": "warm"})
        tab.suffix_var.set("_обр")
        job = tab.start_batch()
        self.assertIsNotNone(job)
        self.assertTrue(tab.run_btn.instate(["disabled"]))
        self.assertTrue(tab.cancel_btn.instate(["!disabled"]))
        self.wait_batch(tab)
        self.assertEqual(len(tab.batch_results), 3)
        self.assertTrue(all(r.ok for r in tab.batch_results),
                        [r.message for r in tab.batch_results])
        self.assertEqual([r.src for r in tab.batch_results], paths)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(names, ["IMG_0001_обр.jpg", "IMG_0002_обр.jpg", "IMG_0003_обр.jpg"])
        self.assertEqual(len(tab.results.get_children()), 3)
        self.assertEqual(float(tab.progress.cget("value")), 100.0)
        self.assertIn("готово 3 из 3", tab.batch_status.cget("text"))
        self.assertEqual(tab.last_out_dir, out)
        self.assertTrue(tab.open_btn.instate(["!disabled"]))
        self.assertTrue(tab.open_result_folder())
        for p, data in originals.items():
            self.assertEqual(p.read_bytes(), data, "исходник изменился")
        with Image.open(out / "IMG_0001_обр.jpg") as im:
            self.assertEqual(im.size, (900, 600))

        # Второй прогон в ту же папку ничего не затирает.
        tab.start_batch()
        self.wait_batch(tab)
        self.assertEqual(len(list(out.iterdir())), 6)

    def test_cancel_stops_files_not_yet_started(self):
        paths = self.photos(3)
        out = self.tmp / "out"
        _frame, tab = self.build()
        tab.set_sources(paths)
        tab._set_out(str(out), auto=False)
        tab.batch_workers = 1
        entered = threading.Event()
        gate = threading.Event()
        real = self.enhance.process_file

        def gated(*args, **kwargs):
            entered.set()
            gate.wait(20)
            return real(*args, **kwargs)

        with mock.patch.object(self.enhance, "process_file", side_effect=gated):
            job = tab.start_batch()
            self.assertIsNotNone(job)
            self.assertTrue(pump(self.root, entered.is_set), "пакет не начался")
            tab.cancel_batch()
            self.assertTrue(tab.cancel_btn.instate(["disabled"]))
            gate.set()
            self.wait_batch(tab)
        self.assertTrue(job.cancelled)
        res = tab.batch_results
        self.assertEqual(len(res), 3)
        self.assertTrue(res[0].ok, res[0].message)
        self.assertEqual([r.message for r in res[1:]], ["Отменено", "Отменено"])
        self.assertEqual([p.name for p in out.iterdir()], ["IMG_0001.jpg"])
        self.assertTrue(tab.batch_status.cget("text").startswith("Отменено"))
        self.assertTrue(tab.run_btn.instate(["!disabled"]))

    def test_source_folder_needs_confirmation(self):
        paths = self.photos(2)
        src_dir = paths[0].parent
        originals = {p: p.read_bytes() for p in paths}
        before = sorted(p.name for p in src_dir.iterdir())
        _frame, tab = self.build()
        tab.set_sources(paths)
        tab._set_out(str(src_dir), auto=False)

        asked: list[str] = []
        tab._ask = lambda title, text: asked.append(title) or False
        self.assertIsNone(tab.start_batch())
        self.assertEqual(len(asked), 1)
        self.assertEqual(sorted(p.name for p in src_dir.iterdir()), before)
        self.assertIn("не начата", tab.batch_status.cget("text"))

        tab._ask = lambda title, text: True
        self.assertIsNotNone(tab.start_batch())
        self.wait_batch(tab)
        self.assertTrue(all(r.ok for r in tab.batch_results))
        for p, data in originals.items():
            self.assertEqual(p.read_bytes(), data, "оригинал перезаписан")
        made = sorted(p.name for p in src_dir.iterdir() if p not in originals)
        # На томе без учёта регистра (Windows, macOS) имя IMG_0001.jpg занято
        # оригиналом IMG_0001.JPG, и результат получает _2; на Linux - нет.
        self.assertEqual(len(made), 2, made)
        self.assertTrue(all(n.endswith(".jpg") for n in made), made)

    def test_subfolder_of_the_shoot_needs_confirmation_too(self):
        paths = self.photos(1)
        inside = paths[0].parent / "Обработка"
        _frame, tab = self.build()
        tab.set_sources(paths)
        tab._set_out(str(inside), auto=False)
        asked: list[str] = []
        tab._ask = lambda title, text: asked.append(title) or False
        self.assertIsNone(tab.start_batch())
        self.assertEqual(len(asked), 1)
        self.assertFalse(inside.exists())
        self.wait_preview(tab)

    def test_finished_batch_is_offered_to_other_tabs(self):
        paths = self.photos(2)
        out = self.tmp / "out"
        got: list[dict] = []
        self.ctx.subscribe(self.gc.TOPIC_PROCESSED, got.append)
        _frame, tab = self.build()
        tab.set_sources(paths)
        tab._set_out(str(out), auto=False)
        tab.start_batch()
        self.wait_batch(tab)
        self.assertEqual(len(got), 1)
        for src in paths:
            self.assertEqual(self.ctx.processed_for(src), out / src.with_suffix(".jpg").name)
        self.assertIsNone(self.ctx.processed_for(self.tmp / "чужой.jpg"))

    def test_nothing_to_do_is_reported(self):
        _frame, tab = self.build()
        self.assertIsNone(tab.start_batch())
        self.assertIn("выберите снимки", tab.batch_status.cget("text").lower())
        tab.set_sources(self.photos(1))
        tab._set_out("", auto=False)
        self.assertIsNone(tab.start_batch())
        self.assertIn("папку", tab.batch_status.cget("text"))
        self.wait_preview(tab)
        # Смена темы перекрашивает строку состояния в цвет той же роли.
        tab.batch_status.configure(foreground="#123456")
        tab.frame.event_generate("<<ThemeChanged>>")
        self.root.update()
        self.assertEqual(str(tab.batch_status.cget("foreground")),
                         self.gc.palette("warn", tab.frame))


if __name__ == "__main__":
    unittest.main()
