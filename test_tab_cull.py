# -*- coding: utf-8 -*-
"""Тесты вкладки «Отбор» (tab_cull.py).

Окна не показываются: корневое окно сразу прячется (withdraw), события
прокачиваются root.update().  Без Tk (нет tcl/tk, нет дисплея) тесты, которым
нужно окно, пропускаются.  Снимки - только синтетические, во временной папке;
поиск лиц выключен, чтобы результат не зависел от OpenCV на машине CI.
"""
from __future__ import annotations

import gc
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from datetime import datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError as exc:                       # pragma: no cover
    raise unittest.SkipTest("нужны numpy и Pillow: %s" % exc)

try:
    import tkinter as tk
    from tkinter import ttk
except Exception as exc:                         # pragma: no cover
    raise unittest.SkipTest("tkinter недоступен: %s" % exc)

import cull  # noqa: E402
import gui_common  # noqa: E402
import tab_cull  # noqa: E402

BASE = datetime(2011, 4, 23, 21, 0, 0)           # часы камеры врут - как в жизни


def scene(seed: int, blur: float = 0.0) -> Image.Image:
    """Кадр с резкими краями; один seed - одна сцена, blur - резкость."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, size=(8, 12, 3), dtype=np.uint8)
    a = np.asarray(Image.fromarray(blocks).resize((360, 240), Image.NEAREST)).copy()
    step = int(rng.integers(9, 15))
    a[::step] = 255 - a[::step]
    a[:, ::step] = 255 - a[:, ::step]
    im = Image.fromarray(a)
    return im.filter(ImageFilter.GaussianBlur(blur)) if blur else im


def save_frame(path: Path, im: Image.Image, t: datetime) -> Path:
    exif = Image.Exif()
    stamp = t.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x0132] = stamp
    sub = exif.get_ifd(0x8769)
    sub[36867] = stamp
    sub[37521] = "%02d" % (t.microsecond // 10000)
    im.save(path, quality=90, exif=exif)
    return path


def make_shoot(folder: Path) -> None:
    """Три фрагмента через 10 минут: серия из 4 + 2 одиночных; 3 одиночных; 2 одиночных."""
    n = 100
    for k, blur in enumerate((2.0, 0.0, 1.0, 3.0)):
        save_frame(folder / ("IMG_%04d.jpg" % n), scene(1, blur),
                   BASE + timedelta(milliseconds=300 * k))
        n += 1
    for k, seed in enumerate((2, 3)):
        save_frame(folder / ("IMG_%04d.jpg" % n), scene(seed),
                   BASE + timedelta(seconds=40 + 40 * k))
        n += 1
    for k, (seed, blur) in enumerate(((4, 0.0), (5, 9.0), (6, 0.0))):
        save_frame(folder / ("_MG_%04d.jpg" % n), scene(seed, blur),
                   BASE + timedelta(minutes=10, seconds=40 * k))
        n += 1
    for k, seed in enumerate((7, 8)):
        save_frame(folder / ("IMG_%04d.jpg" % n), scene(seed),
                   BASE + timedelta(minutes=20, seconds=40 * k))
        n += 1


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Имя -> (размер, mtime_ns): чтобы убедиться, что папку съёмки не трогали."""
    return {e.name: (e.stat().st_size, e.stat().st_mtime_ns) for e in os.scandir(folder)}


def pump(root: tk.Misc, until=lambda: False, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if until():
            return True
        time.sleep(0.005)
    root.update()
    return bool(until())


class _ShootMixin:
    """Синтетическая съёмка во временной папке, одна на класс."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(prefix="tab_cull_test_")
        cls.tmp = Path(cls._td.name)
        cls.photos = cls.tmp / "photos"
        cls.photos.mkdir()
        make_shoot(cls.photos)
        cls.result = cull.scan(cls.photos, use_faces=False, workers=2)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()


class _Base(_ShootMixin, unittest.TestCase):
    """Спрятанное окно и контекст приложения поверх синтетической съёмки."""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except Exception as exc:
            raise unittest.SkipTest("Tk недоступен: %s" % exc)
        self.root.withdraw()
        self.settings: dict = {}
        self.saves = 0

        def save(_data: dict) -> None:
            self.saves += 1

        self.ctx = gui_common.AppContext(self.root, settings=self.settings,
                                         save_settings=save, scale=1.0, poll_ms=15)
        self.published: list[list[Path]] = []
        self.ctx.subscribe(gui_common.TOPIC_SELECTION, self.published.append)
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self.tab = tab_cull.build_tab(self.nb, self.ctx)
        self.nb.add(self.tab, text=tab_cull.TAB_TITLE)

    def tearDown(self) -> None:
        try:
            self.ctx.shutdown()
            for job in list(self.ctx._jobs):          # noqa: SLF001
                job.join(10)
            self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()        # мусор Tk - в потоке Tk, см. test_shell._destroy_root

    def show(self, result: cull.CullResult | None = None, folder: Path | None = None) -> None:
        self.tab.set_result(result or self.result, folder=folder or self.photos)
        self.assertTrue(pump(self.root, lambda: not self.tab.is_building))

    def click(self, card: "tab_cull._Card", part: str, double: bool = False) -> None:
        """Щелчок по центру элемента карточки (лист не прокручен: холст = окно)."""
        if part == "thumb":                       # картинки может ещё не быть
            x = card.x + self.tab._card_width() // 2                            # noqa: SLF001
            y = card.y + self.tab.ctx.px(tab_cull.CARD_PAD) + self.tab.box_h // 2
        else:
            x1, y1, x2, y2 = self.tab.canvas.bbox(card.items[part])
            x, y = (x1 + x2) // 2, (y1 + y2) // 2
        event = types.SimpleNamespace(x=x, y=y)
        (self.tab._on_sheet_double if double else self.tab._on_sheet_click)(event)  # noqa: SLF001

    def multi_burst(self) -> cull.Burst:
        return next(b for b in self.result.bursts if b.size > 1)


class TestHelpers(unittest.TestCase):
    def test_plural_and_counter(self) -> None:
        forms = ("серия", "серии", "серий")
        self.assertEqual([tab_cull.plural_ru(n, forms) for n in (1, 2, 5, 11, 12, 21, 22, 111)],
                         ["серия", "серии", "серий", "серий", "серий", "серия", "серии", "серий"])
        self.assertEqual(tab_cull.counter_text(27, 153), "отмечено 27 из 153 серий")
        self.assertEqual(tab_cull.counter_text(1, 21), "отмечено 1 из 21 серии")

    def test_burst_text_and_clock(self) -> None:
        self.assertEqual(tab_cull.burst_text(5), "серия из 5 — раскрыть")
        self.assertEqual(tab_cull.burst_text(1), "одиночный кадр")
        self.assertEqual(tab_cull.fmt_clock(425), "7:05")
        self.assertEqual(tab_cull.fmt_clock(3750), "1:02:30")
        self.assertEqual(tab_cull.fmt_clock(None), "")

    def test_segment_title(self) -> None:
        seg = cull.Segment(id=2, first=0, last=20, t_start=750.0, t_end=1025.0,
                           burst_ids=list(range(12)))
        self.assertEqual(tab_cull.segment_title(seg), "фрагмент 3 · 12 серий · 12:30–17:05")
        self.assertTrue(tab_cull.segment_title(seg, 2).endswith("· отмечено 2"))

    def test_export_problem_refuses_photo_folder_and_subfolders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            photos = Path(td) / "photos"
            photos.mkdir()
            src = [photos / "IMG_0001.jpg"]
            self.assertTrue(tab_cull.export_problem(photos, src))
            self.assertTrue(tab_cull.export_problem(photos / "отбор", src))
            self.assertTrue(tab_cull.export_problem(Path(td) / "x", [], shoot_folder=Path(td)))
            self.assertEqual(tab_cull.export_problem(Path(td) / "отбор", src, photos), "")
            self.assertTrue(tab_cull.export_problem("", src))

    def test_fit_thumbnail_never_upscales(self) -> None:
        big = Image.new("RGB", (400, 600))
        small = Image.new("RGB", (50, 40))
        fitted = tab_cull.fit_thumbnail(big, 176, 132)
        self.assertLessEqual(fitted.width, 176)
        self.assertLessEqual(fitted.height, 132)
        self.assertEqual(tab_cull.fit_thumbnail(small, 176, 132).size, (50, 40))


class TestSharpnessPlace(_ShootMixin, unittest.TestCase):
    """«Резкость» на карточке - место во фрагменте, а не процент."""

    def test_places_cover_every_readable_frame_once_per_segment(self) -> None:
        places = tab_cull.sharpness_places(self.result)
        self.assertEqual(set(places), {r.capture_order for r in self.result.images if not r.error})
        for seg in self.result.segments:
            members = [r for r in self.result.images[seg.first:seg.last + 1] if not r.error]
            got = sorted(places[r.capture_order][0] for r in members)
            self.assertEqual(got, list(range(1, len(members) + 1)))
            sharpest = max(members, key=lambda r: r.sharpness)
            self.assertEqual(places[sharpest.capture_order], (1, len(members)))

    def test_text(self) -> None:
        self.assertEqual(tab_cull.sharpness_text((3, 17)), "по резкости 3-й из 17 во фрагменте")
        self.assertEqual(tab_cull.sharpness_text((1, 1)), "единственный кадр фрагмента")
        self.assertEqual(tab_cull.sharpness_text(None), "")
        self.assertNotIn("%", tab_cull.sharpness_text((1, 5)))


class TestLayoutItems(_ShootMixin, unittest.TestCase):
    """layout_items() без окна."""

    def test_event_order_groups_by_segment(self) -> None:
        items = tab_cull.layout_items(self.result)
        heads = [i for k, i in items if k == "segment"]
        self.assertEqual(heads, [s.id for s in self.result.segments])
        self.assertEqual([i for k, i in items if k == "burst"],
                         [b for s in self.result.segments for b in s.burst_ids])

    def test_score_order_and_only_ticked(self) -> None:
        items = tab_cull.layout_items(self.result, sort=tab_cull.SORT_SCORE)
        self.assertEqual([i for _k, i in items],
                         [r.burst_id for r in cull.suggest(self.result, None)])
        self.assertFalse(any(k == "segment" for k, _i in items))
        only = tab_cull.layout_items(self.result, ticked={0}, only_ticked=True)
        self.assertEqual(only, [("segment", self.result.bursts[0].segment_id), ("burst", 0)])


class TestTab(_Base):
    def test_contract(self) -> None:
        self.assertEqual(tab_cull.TAB_TITLE, "Отбор")
        self.assertIsInstance(self.tab, ttk.Frame)
        self.assertEqual(str(self.tab.master), str(self.nb))
        self.assertEqual(self.tab.counter_var.get(), "отмечено 0 из 0 серий")

    def test_result_renders_cards_headers_and_thumbnails(self) -> None:
        self.assertGreater(len(self.result.bursts), 3)
        self.assertLess(len(self.result.bursts), len(self.result.images))
        self.show()
        self.assertEqual(set(self.tab.cards), {b.id for b in self.result.bursts})
        self.assertEqual(set(self.tab.headers), {s.id for s in self.result.segments})
        self.assertEqual(self.tab.visible_burst_ids(),
                         [b for s in self.result.segments for b in s.burst_ids])
        burst = self.multi_burst()
        self.assertEqual(self.tab.card_text(burst.id, "link"),
                         "серия из %d — раскрыть" % burst.size)
        self.assertIn("фрагмент 1 ·", self.tab.header_text(0))
        self.assertIn("Режим: без лиц", self.tab.mode_var.get())
        self.assertEqual(self.tab.counter_var.get(),
                         tab_cull.counter_text(0, len(self.result.bursts)))
        # Миниатюры читаются в фоне и оборачиваются в PhotoImage в потоке Tk.
        self.assertTrue(pump(self.root, lambda: not self.tab.thumbs_pending))
        for card in self.tab.cards.values():
            self.assertTrue(self.tab.canvas.itemcget(card.items["thumb"], "image"))
            self.assertEqual(self.tab.card_text(card.burst_id, "ph"), "")
        self.assertEqual(len(self.tab._photos), len(self.result.bursts))        # noqa: SLF001

    def test_flag_chips_use_palette(self) -> None:
        self.show()
        self.assertTrue(any(r.flags for r in self.result.images),
                        "в синтетике есть нерезкий кадр - должна быть метка")
        flagged = [c for c in self.tab.cards.values() if c.chips]
        self.assertTrue(flagged)
        rect, label, role = flagged[0].chips[0]
        canvas = self.tab.canvas
        self.assertEqual(canvas.itemcget(label, "fill"), gui_common.palette(role, self.tab))
        self.assertEqual(canvas.itemcget(rect, "outline"), gui_common.palette(role, self.tab))
        self.assertIn(canvas.itemcget(label, "text"),
                      {t for t, _r in tab_cull.FLAG_CHIPS.values()})

    def test_card_shows_place_not_percent(self) -> None:
        self.show()
        for bid in self.tab.cards:
            meta = self.tab.card_text(bid, "meta")
            self.assertNotIn("%", meta)
            self.assertRegex(meta, r"по резкости \d+-й из \d+ во фрагменте|единственный кадр")

    def test_face_boxes_of_ticked_frames_reach_the_context(self) -> None:
        rec = self.result.images[self.result.bursts[1].representative]
        with mock.patch.object(rec, "face_box", (0.25, 0.2, 0.1, 0.15)):
            self.show()
            self.tab.set_ticked(1, True)
        self.assertEqual(self.ctx.photo_hint(rec.path).get("face_box"), (0.25, 0.2, 0.1, 0.15))

    def test_ticking_publishes_selection(self) -> None:
        self.show()
        self.published.clear()
        burst = self.result.bursts[1]
        card = self.tab.cards[burst.id]
        self.click(card, "box")                   # щелчок по нарисованному флажку
        expected = [self.result.images[burst.representative].path]
        self.assertEqual(self.ctx.selection, expected)
        self.assertEqual(self.published[-1], expected)
        self.assertEqual(self.tab.counter_var.get(),
                         tab_cull.counter_text(1, len(self.result.bursts)))
        self.tab.set_ticked(0, True)
        self.assertEqual(len(self.ctx.selection), 2)
        # Порядок выбора - порядок съёмки.
        orders = [next(r.capture_order for r in self.result.images if r.path == p)
                  for p in self.ctx.selection]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(self.tab.canvas.itemcget(card.items["mark"], "state"), "normal")
        self.click(card, "name")                  # щелчок по имени файла - тоже флажок
        self.assertFalse(self.tab.is_ticked(burst.id))
        self.assertEqual(self.tab.canvas.itemcget(card.items["mark"], "state"), "hidden")
        self.tab.set_ticked(burst.id, True)
        self.tab.clear_ticks()
        self.assertEqual(self.ctx.selection, [])
        self.assertFalse(self.tab.is_ticked(burst.id))

    def test_suggest_ticks_expected_count(self) -> None:
        self.show()
        job = self.tab.suggest_best(3)
        self.assertIsNotNone(job)
        self.assertTrue(pump(self.root, lambda: self.tab._suggest_job is None))  # noqa: SLF001
        expected = {r.burst_id for r in cull.suggest(self.result, 3)}
        self.assertEqual(len(expected), 3)
        self.assertEqual(self.tab.ticked, expected)
        self.assertEqual(len(self.ctx.selection), 3)
        self.assertEqual(self.tab.counter_var.get(),
                         tab_cull.counter_text(3, len(self.result.bursts)))
        # N больше числа серий - отмечается всё, что есть.
        self.tab.suggest_best(999)
        self.assertTrue(pump(self.root, lambda: self.tab._suggest_job is None))  # noqa: SLF001
        self.assertEqual(len(self.tab.ticked), len(self.result.bursts))

    def test_expand_and_swap_representative(self) -> None:
        self.show()
        burst = self.multi_burst()
        self.tab.set_ticked(burst.id, True)
        self.tab.expand_burst(burst.id)
        self.root.update()
        self.assertEqual(sorted(self.tab.panel_orders()), sorted(burst.members))
        self.assertEqual(self.tab.card_text(burst.id, "link"),
                         "серия из %d — свернуть" % burst.size)
        other = next(o for o in burst.members if o != burst.representative)
        self.tab.set_representative(burst.id, other)
        self.assertEqual(self.ctx.selection, [self.result.images[other].path])
        self.assertEqual(self.tab.card_text(burst.id, "name"),
                         self.result.images[other].path.name)
        self.assertIn("выбран вручную", self.tab.card_text(burst.id, "meta"))
        stranger = next(r.capture_order for r in self.result.images
                        if r.capture_order not in burst.members)
        with self.assertRaises(ValueError):
            self.tab.set_representative(burst.id, stranger)
        self.tab.collapse_burst()
        self.assertEqual(self.tab.panel_orders(), [])

    def test_cards_do_not_overlap_and_follow_width(self) -> None:
        self.show()
        width = self.tab._card_width()                            # noqa: SLF001
        cards = list(self.tab.cards.values())
        for i, a in enumerate(cards):
            for b in cards[i + 1:]:
                apart = (a.x + width <= b.x or b.x + width <= a.x
                         or a.y + a.height <= b.y or b.y + b.height <= a.y)
                self.assertTrue(apart, "карточки %d и %d наложились" % (a.burst_id, b.burst_id))
        # Узкое окно - одна колонка, широкое - несколько.
        self.tab._cols = 1                                        # noqa: SLF001
        self.tab._layout_now()                                    # noqa: SLF001
        self.assertEqual({c.x for c in self.tab.cards.values()},
                         {self.tab.ctx.px(tab_cull.SHEET_MARGIN)})
        self.tab._cols = 4                                        # noqa: SLF001
        self.tab._layout_now()                                    # noqa: SLF001
        widest = max(len(s.burst_ids) for s in self.result.segments)
        self.assertGreater(widest, 1)
        self.assertEqual(len({c.x for c in self.tab.cards.values()}), min(4, widest))

    def test_link_click_expands_and_double_click_opens_original(self) -> None:
        self.show()
        burst = self.multi_burst()
        card = self.tab.cards[burst.id]
        self.click(card, "link")
        self.assertEqual(self.tab.expanded, burst.id)
        self.click(card, "link")
        self.assertIsNone(self.tab.expanded)
        with mock.patch.object(tab_cull, "open_in_viewer") as opener:
            self.click(card, "thumb", double=True)
        opener.assert_called_once_with(self.result.images[card.order].path)
        self.assertFalse(self.tab.is_ticked(burst.id))

    def test_sort_and_only_ticked(self) -> None:
        self.show()
        self.tab.set_sort(tab_cull.SORT_SCORE)
        self.assertEqual(self.tab.visible_burst_ids(),
                         [r.burst_id for r in cull.suggest(self.result, None)])
        self.assertEqual(self.settings["tabs"]["cull"]["sort"], "score")
        self.tab.set_sort(tab_cull.SORT_EVENT)
        self.tab.set_ticked(2, True)
        self.tab.set_only_ticked(True)
        self.root.update()
        self.assertEqual(self.tab.visible_burst_ids(), [2])
        shown = [b for b, c in self.tab.cards.items()
                 if c.shown and self.tab.canvas.itemcget(c.items["bg"], "state") != "hidden"]
        self.assertEqual(shown, [2])
        # Спрятанные карточки не ловят щелчков.
        hidden = self.tab.cards[0]
        self.assertFalse(hidden.shown)
        self.assertIsNot(self.tab.card_at(hidden.x + 5, hidden.y + 5), hidden)
        self.tab.set_only_ticked(False)
        self.assertEqual(len(self.tab.visible_burst_ids()), len(self.result.bursts))

    def test_selection_restored_for_same_folder(self) -> None:
        before = snapshot(self.photos)
        self.show()
        burst = self.multi_burst()
        other = next(o for o in burst.members if o != burst.representative)
        self.tab.set_ticked(burst.id, True)
        self.tab.set_representative(burst.id, other)
        self.tab.set_ticked(0 if burst.id != 0 else 1, True)
        saved = self.settings["tabs"]["cull"]["selections"][tab_cull.folder_key(self.photos)]
        self.assertIn(self.result.images[other].path.name, saved["ticked"])
        chosen = self.ctx.selection

        # Новая вкладка с теми же настройками - как повторное открытие папки.
        tab2 = tab_cull.build_tab(self.nb, self.ctx)
        tab2.set_result(self.result, folder=self.photos)
        self.assertEqual(tab2.selected_paths(), chosen)
        self.assertEqual(tab2.rep[burst.id], other)
        # Другая папка - своих отметок нет.
        tab2.set_result(self.result, folder=self.tmp / "другая")
        self.assertEqual(tab2.selected_paths(), [])
        # Отметки хранятся в настройках, а в папку со снимками ничего не пишется.
        self.assertEqual(snapshot(self.photos), before)
        self.assertTrue(pump(self.root, lambda: self.saves > 0, timeout=5))

    def test_export_copies_into_temp_dir(self) -> None:
        before = snapshot(self.photos)
        self.show()
        self.tab.set_ticked(0, True)
        self.tab.set_ticked(len(self.result.bursts) - 1, True)
        out = self.tmp / "копии"
        job = self.tab.export_to(out, interactive=False)
        self.assertIsNotNone(job)
        self.assertTrue(pump(self.root, lambda: self.tab._job is None))  # noqa: SLF001
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(names, sorted(p.name for p in self.ctx.selection))
        for p in self.ctx.selection:
            self.assertEqual((out / p.name).read_bytes(), p.read_bytes())
        self.assertIn("Скопировано 2 файла", self.tab.export_var.get())
        # Второй раз - не перезаписывает, а переименовывает.
        self.tab.export_to(out, interactive=False)
        self.assertTrue(pump(self.root, lambda: self.tab._job is None))  # noqa: SLF001
        self.assertEqual(len(list(out.iterdir())), 4)
        # В папку съёмки и внутрь неё копировать нельзя.
        self.assertIsNone(self.tab.export_to(self.photos, interactive=False))
        self.assertIsNone(self.tab.export_to(self.photos / "отбор", interactive=False))
        self.assertEqual(snapshot(self.photos), before)
        self.shutil_cleanup(out)

    @staticmethod
    def shutil_cleanup(folder: Path) -> None:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    def test_analyse_runs_scan_in_background(self) -> None:
        before = snapshot(self.photos)
        job = self.tab.analyse(self.photos, use_faces=False)
        self.assertIsNotNone(job)
        self.assertIsNone(self.tab.analyse(self.photos, use_faces=False))  # уже идёт
        self.assertTrue(pump(self.root, lambda: self.tab.result is not None
                             and not self.tab.is_building))
        self.assertEqual(len(self.tab.cards), len(self.result.bursts))
        self.assertIn("Режим: без лиц", self.tab.mode_var.get())
        # Миниатюры пришли из самого разбора - отдельного чтения файлов не нужно:
        # фоновое чтение не ставилось в очередь вовсе.  Это детерминировано.
        self.assertFalse(self.tab._thumb_queue)  # noqa: SLF001
        self.assertIsNone(self.tab._thumb_job)  # noqa: SLF001
        # А обёртка в PhotoImage идёт пачками через after() и на медленной машине
        # (раннер CI) к этому моменту может ещё не закончиться.  Раньше здесь была
        # мгновенная проверка thumbs_pending - на ноутбуке она проходила, а на
        # windows-latest падала.  Завершение дожидаемся, а не проверяем сразу.
        self.assertTrue(pump(self.root, lambda: not self.tab.thumbs_pending))
        self.assertEqual(self.settings["tabs"]["cull"]["folder"], str(self.photos))
        self.assertEqual(snapshot(self.photos), before)

    def test_analyse_cancel(self) -> None:
        job = self.tab.analyse(self.photos, use_faces=False)
        self.assertIsNotNone(job)
        self.tab.cancel()
        self.assertTrue(pump(self.root, lambda: self.tab._job is None))  # noqa: SLF001
        self.assertIsNone(self.tab.result)
        self.assertIn("отмен", self.tab.progress_var.get())

    def test_empty_folder(self) -> None:
        empty = self.tmp / "пусто"
        empty.mkdir(exist_ok=True)
        self.tab.analyse(empty, use_faces=False)
        self.assertTrue(pump(self.root, lambda: self.tab.result is not None))
        self.assertEqual(self.tab.cards, {})
        self.assertIn("нет снимков", self.tab.empty_text())


class TestInShell(_Base):
    """Оболочка cr2_gui.Shell подхватывает вкладку без панели ошибки."""

    def test_shell_builds_cull_tab(self) -> None:
        name = "cr2_gui_tab_cull_under_test"
        path = str(HERE / "cr2_gui.pyw")
        spec = importlib.util.spec_from_file_location(name, path,
                                                      loader=SourceFileLoader(name, path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            del sys.modules[name]
            raise unittest.SkipTest("cr2_gui не загрузился: %s" % exc)
        if not hasattr(mod, "Shell"):
            raise unittest.SkipTest("в cr2_gui нет Shell")
        top = tk.Toplevel(self.root)
        top.withdraw()
        shell = mod.Shell(top, settings={}, tab_modules=("tab_cull",))
        try:
            rec = next(t for t in shell.tabs if t.key == "cull")
            self.assertEqual(rec.error, "")
            self.assertEqual(rec.title, "Отбор")
        finally:
            # Без shell.on_close(): он пишет настройки в настоящий файл пользователя.
            # Таймер опроса конвертера снимаем сами, иначе он сработает на
            # уничтоженном окне («invalid command name ..._poll»).
            app = getattr(shell, "app", None)
            poll_id = getattr(app, "_poll_id", None)
            if poll_id:
                try:
                    app.after_cancel(poll_id)
                except tk.TclError:
                    pass
            shell.ctx.shutdown()
            top.destroy()


if __name__ == "__main__":
    unittest.main()
