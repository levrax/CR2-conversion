# -*- coding: utf-8 -*-
"""Тесты оболочки с вкладками (cr2_gui.Shell) и общего модуля gui_common.

Окна не показываются: корневое окно сразу прячется (withdraw), события
прокачиваются root.update().  Если Tk недоступен (нет tcl/tk, нет дисплея),
тесты, которым нужно окно, пропускаются, а не падают.

Модули вкладок подставляются через sys.modules: настоящие tab_enhance /
tab_cull / tab_poster могут лежать рядом, но тесты оболочки от них не
зависят - каждый тест сам решает, какие вкладки «существуют».
"""
from __future__ import annotations

import gc
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TAB_NAMES = ("tab_cull", "tab_enhance", "tab_poster")
REQUIRED_ROLES = ("fg", "muted", "ok", "warn", "error", "accent", "card_bg",
                  "card_border", "selection")


def load_gui():
    """cr2_gui.pyw под своим именем, с явным SourceFileLoader.

    importlib признаёт .pyw исходником только на Windows; без явного loader
    на macOS и Linux spec_from_file_location вернул бы None.
    """
    for name in ("tkinter", "cr2_core"):
        try:
            importlib.import_module(name)
        except BaseException as exc:
            raise unittest.SkipTest("%s недоступен: %s" % (name, exc))
    name = "cr2_gui_shell_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = str(HERE / "cr2_gui.pyw")
    spec = importlib.util.spec_from_file_location(
        name, path, loader=SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # до exec_module: нужно @dataclass
    spec.loader.exec_module(mod)
    return mod


def make_root():
    """Спрятанное корневое окно или SkipTest."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        raise unittest.SkipTest("Tk недоступен: %s" % exc)
    root.withdraw()
    return root


def pump(root, until=lambda: False, timeout: float = 5.0) -> bool:
    """Крутить события Tk, пока until() не станет истинным.  False - таймаут."""
    import tkinter as tk
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:              # окно уничтожено
            return bool(until())
        if until():
            return True
        time.sleep(0.01)
    return bool(until())


class TabModules:
    """Подменить модули вкладок в sys.modules на время блока.

    None == «модуля нет».  Восстанавливаются ровно эти имена: patch.dict на
    всём sys.modules выкинул бы модули, импортированные во время теста
    (Pillow), и следующий импорт выполнил бы их второй раз.
    """

    def __init__(self, **modules) -> None:
        self.modules = {name: modules.get(name) for name in TAB_NAMES}
        self.modules.update(modules)
        self._saved: dict = {}

    def __enter__(self):
        for name, mod in self.modules.items():
            self._saved[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = mod
        return self

    def __exit__(self, *exc) -> None:
        for name, old in self._saved.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


_MISSING = object()


def fake_tab(name: str, title: str, build=None) -> types.ModuleType:
    """Модуль вкладки по контракту: TAB_TITLE + build_tab(parent, ctx)."""
    from tkinter import ttk
    mod = types.ModuleType(name)
    mod.TAB_TITLE = title
    mod.built_with = []

    def default_build(parent, ctx):
        mod.built_with.append(ctx)
        frame = ttk.Frame(parent)
        ttk.Label(frame, text=title).pack()
        return frame

    mod.build_tab = build or default_build
    return mod


class GuiTestCase(unittest.TestCase):
    """Общая подготовка: модуль GUI, временные настройки и журнал, окно."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gui = load_gui()
        import gui_common
        cls.gc = gui_common

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cr2_shell_")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        # Настройки и журнал - только во временной папке: тесты не должны
        # оставлять файлов рядом с программой.
        old_settings, old_log = self.gui.SETTINGS_PATH, self.gui.ERROR_LOG_PATH
        self.gui.SETTINGS_PATH = self.tmp / "settings.json"
        self.gui.ERROR_LOG_PATH = self.tmp / "error.log"

        def restore() -> None:
            self.gui.SETTINGS_PATH = old_settings
            self.gui.ERROR_LOG_PATH = old_log
        self.addCleanup(restore)
        self.root = None

    def open_root(self):
        self.root = make_root()
        self.root.report_callback_exception = self._collect_tk_exception
        self.tk_errors: list[BaseException] = []
        self.addCleanup(self._destroy_root)
        return self.root

    def make_shell(self, root, **kwargs):
        """Shell, который после теста гасит свои таймеры, как при закрытии окна."""
        shell = self.gui.Shell(root, **kwargs)

        def stop() -> None:
            shell.ctx.shutdown()
            if shell.app is not None:
                shell.app._destroy()           # снимает after-цикл конвертера
        self.addCleanup(stop)
        return shell

    def _collect_tk_exception(self, exc_type, exc, tb) -> None:
        self.tk_errors.append(exc)

    def _destroy_root(self) -> None:
        try:
            if self.root is not None:
                self.root.destroy()
        except Exception:
            pass
        # Мусор этого теста (tk-переменные в циклах) разбираем здесь, в потоке
        # Tk, а не в рабочем потоке следующего теста: там Variable.__del__
        # сыплет «main thread is not in main loop».
        gc.collect()


# --------------------------------------------------------------------------
# Оболочка
# --------------------------------------------------------------------------


class TestShell(GuiTestCase):

    def test_builds_with_zero_tab_modules(self):
        root = self.open_root()
        with TabModules():
            shell = self.make_shell(root)
        pump(root, timeout=0.2)
        self.assertEqual(len(shell.notebook.tabs()), 1)
        self.assertEqual(shell.notebook.tab(0, "text"), "Конвертация")
        self.assertIsInstance(shell.app, self.gui.App)
        self.assertEqual([t.key for t in shell.tabs], ["convert"])

    def test_fake_tab_module_is_plugged_in(self):
        root = self.open_root()
        enhance = fake_tab("tab_enhance", "Улучшение")
        with TabModules(tab_enhance=enhance):
            shell = self.make_shell(root)
        pump(root, timeout=0.2)
        self.assertEqual(len(shell.notebook.tabs()), 2)
        self.assertEqual(shell.notebook.tab(1, "text"), "Улучшение")
        self.assertEqual(shell.tabs[1].key, "enhance")
        self.assertEqual(shell.tabs[1].error, "")
        self.assertEqual(len(enhance.built_with), 1)
        self.assertIs(enhance.built_with[0], shell.ctx)
        self.assertIsInstance(shell.ctx, self.gc.AppContext)

    def test_tabs_keep_the_declared_order(self):
        root = self.open_root()
        with TabModules(tab_enhance=fake_tab("tab_enhance", "Обработка"),
                        tab_cull=fake_tab("tab_cull", "Отбор"),
                        tab_poster=fake_tab("tab_poster", "Афиши")):
            shell = self.make_shell(root)
        # Порядок работы: отобрать -> обработать отобранное -> сделать афишу.
        self.assertEqual([shell.notebook.tab(i, "text") for i in range(4)],
                         ["Конвертация", "Отбор", "Обработка", "Афиши"])
        self.assertEqual(self.gui.TAB_MODULES, ("tab_cull", "tab_enhance", "tab_poster"))

    def test_tab_that_raises_in_build_gets_an_error_panel(self):
        root = self.open_root()
        from tkinter import ttk

        def broken_build(parent, ctx):
            ttk.Frame(parent)                 # полупостроенный мусор
            raise RuntimeError("вкладка сломалась")

        with TabModules(tab_enhance=fake_tab("tab_enhance", "Улучшение"),
                        tab_cull=fake_tab("tab_cull", "Отбор", broken_build)):
            shell = self.make_shell(root)
        pump(root, timeout=0.2)
        self.assertEqual(len(shell.notebook.tabs()), 3)
        cull = shell.tabs[1]                  # «Отбор» идёт первым после конвертера
        self.assertEqual(cull.title, "Отбор")
        self.assertIn("вкладка сломалась", cull.error)
        # Мусор, созданный до исключения, убран: в блокноте только страницы.
        self.assertEqual(len(shell.notebook.winfo_children()),
                         len(shell.notebook.tabs()))
        # Ошибка записана в журнал.
        log = self.gui.ERROR_LOG_PATH.read_text(encoding="utf-8")
        self.assertIn("вкладка сломалась", log)
        self.assertIn("Отбор", log)
        # Строка состояния сообщает о проблеме по-русски.
        self.assertIn("не загрузилась", shell.status_var.get())
        # Программа жива: остальные вкладки переключаются.
        self.assertTrue(shell.select_tab("enhance"))
        self.assertTrue(shell.select_tab("cull"))
        pump(root, timeout=0.2)
        self.assertEqual(shell.current_key(), "cull")
        self.assertEqual(shell.tabs[2].error, "")

    def test_tab_that_raises_on_import_gets_an_error_panel(self):
        root = self.open_root()
        mod_dir = self.tmp / "mods"
        mod_dir.mkdir()
        (mod_dir / "tab_shelltest_broken.py").write_text(textwrap.dedent('''\
            # -*- coding: utf-8 -*-
            import cv2_shelltest_not_installed_zz
            TAB_TITLE = "Не дойдёт"
            '''), encoding="utf-8")
        sys.path.insert(0, str(mod_dir))
        self.addCleanup(lambda: sys.path.remove(str(mod_dir)))
        self.addCleanup(lambda: sys.modules.pop("tab_shelltest_broken", None))
        shell = self.make_shell(root, tab_modules=("tab_shelltest_missing",
                                                  "tab_shelltest_broken"))
        pump(root, timeout=0.2)
        # Отсутствующий модуль пропущен молча, сломанный - панель ошибки.
        self.assertEqual([t.key for t in shell.tabs],
                         ["convert", "shelltest_broken"])
        self.assertIn("ModuleNotFoundError", shell.tabs[1].error)
        texts = _all_label_texts(shell.tabs[1].frame)
        self.assertTrue(any("python -m pip install" in t for t in texts), texts)
        self.assertTrue(any("не загрузилась" in t for t in texts), texts)

    def test_contract_violation_is_reported_not_crashed(self):
        root = self.open_root()
        no_build = types.ModuleType("tab_poster")
        no_build.TAB_TITLE = "Постеры"
        not_a_frame = fake_tab("tab_cull", "Отбор", lambda parent, ctx: 42)
        with TabModules(tab_cull=not_a_frame, tab_poster=no_build):
            shell = self.make_shell(root)
        self.assertEqual(len(shell.tabs), 3)
        self.assertIn("TabContractError", shell.tabs[1].error)
        self.assertIn("build_tab", shell.tabs[2].error)

    def test_last_tab_is_remembered(self):
        root = self.open_root()
        with TabModules(tab_enhance=fake_tab("tab_enhance", "Улучшение"),
                        tab_cull=fake_tab("tab_cull", "Отбор")):
            shell = self.make_shell(root)
            shell.select_tab("cull")
            pump(root, timeout=0.2)
            self.assertEqual(shell.settings["last_tab"], "cull")
            shell.on_close()
            pump(root, lambda: not _alive(root), timeout=10)
            self.assertFalse(_alive(root))
            saved = self.gui.load_settings()
            self.assertEqual(saved["last_tab"], "cull")

            self.root = root2 = make_root()
            shell2 = self.make_shell(root2)
            pump(root2, timeout=0.2)
            self.assertEqual(shell2.current_key(), "cull")

    def test_converter_save_keeps_tab_settings(self):
        """Регрессия: App._save_settings писал только свои ключи и стирал вкладки."""
        root = self.open_root()
        with TabModules():
            shell = self.make_shell(root)
        shell.ctx.tab_settings("enhance")["strength"] = 0.7
        shell.app._save_settings()
        loaded = self.gui.load_settings()
        self.assertEqual(loaded["tabs"]["enhance"]["strength"], 0.7)
        self.assertEqual(loaded["quality"], shell.app._quality())

    def test_close_cancels_jobs_and_runs_shutdown_callbacks(self):
        root = self.open_root()
        seen = {"shutdown": 0, "done": 0, "cancelled": 0, "job": None}

        def build(parent, ctx):
            from tkinter import ttk

            def work(report):
                while not report.cancelled:
                    time.sleep(0.01)
                return "partial"

            seen["job"] = ctx.run_background(
                work, name="test",
                on_done=lambda r: seen.__setitem__("done", seen["done"] + 1),
                on_cancelled=lambda r: seen.__setitem__("cancelled", 1))
            ctx.register_shutdown(
                lambda: seen.__setitem__("shutdown", seen["shutdown"] + 1))
            return ttk.Frame(parent)

        with TabModules(tab_enhance=fake_tab("tab_enhance", "Улучшение", build)):
            shell = self.make_shell(root)
        pump(root, timeout=0.2)
        self.assertTrue(seen["job"].running)
        shell.on_close()
        shell.on_close()                      # повторный щелчок безопасен
        self.assertTrue(pump(root, lambda: not _alive(root), timeout=10),
                        "окно не закрылось")
        self.assertEqual(seen["shutdown"], 1)
        self.assertTrue(seen["job"].cancelled)
        self.assertTrue(seen["job"].join(2.0))
        self.assertEqual(seen["done"], 0)
        self.assertTrue(self.gui.SETTINGS_PATH.exists())

    def test_window_title_names_the_product_and_not_dpp(self):
        src = (HERE / "cr2_gui.pyw").read_text(encoding="utf-8")
        titles = re.findall(r'root\.title\(\s*"(.+?)"', src)
        self.assertEqual(titles[0], "Медиа-инструменты ЮИ РУДН")
        for title in titles:
            self.assertNotIn("DPP", title)

    def test_tab_imports_are_visible_to_pyinstaller(self):
        """Каждое имя из TAB_MODULES импортируется явным оператором import."""
        src = (HERE / "cr2_gui.pyw").read_text(encoding="utf-8")
        for name in self.gui.TAB_MODULES:
            self.assertRegex(src, r"\n\s+import %s as module" % name)


def _alive(root) -> bool:
    try:
        return bool(root.winfo_exists())
    except Exception:
        return False


def _all_label_texts(widget) -> list[str]:
    out = []
    for child in widget.winfo_children():
        try:
            out.append(str(child.cget("text")))
        except Exception:
            pass
        out.extend(_all_label_texts(child))
    return out


# --------------------------------------------------------------------------
# run_background и шина событий
# --------------------------------------------------------------------------


class TestBackground(GuiTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.open_root()
        self.ctx = self.gc.AppContext(self.root, poll_ms=10)
        self.main = threading.main_thread()

    def test_progress_and_done_arrive_on_the_tk_thread(self):
        threads, progress, done = [], [], []

        def work(report):
            self.assertIsNot(threading.current_thread(), self.main)
            report(0.5, "половина")
            time.sleep(0.05)
            report(1.0, "всё")
            return 42

        job = self.gc.run_background(
            work, widget=self.root, poll_ms=10,
            on_progress=lambda f, t: (threads.append(threading.current_thread()),
                                      progress.append((f, t))),
            on_done=lambda r: (threads.append(threading.current_thread()),
                               done.append(r)))
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertEqual(done, [42])
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (1.0, "всё"))
        self.assertTrue(all(t is self.main for t in threads), threads)
        self.assertFalse(self.tk_errors)

    def test_cancel_stops_progress_and_skips_on_done(self):
        started = threading.Event()
        calls = {"done": 0, "cancelled": [], "late_progress": 0}
        cancelled_at = [False]

        def work(report):
            started.set()
            while not report.cancelled:
                report(None, "работаю")
                time.sleep(0.005)
            return "частично"

        def on_progress(_f, _t):
            if cancelled_at[0]:
                calls["late_progress"] += 1

        job = self.ctx.run_background(
            work, on_progress=on_progress,
            on_done=lambda r: calls.__setitem__("done", calls["done"] + 1),
            on_cancelled=lambda r: calls["cancelled"].append(r))
        self.assertTrue(started.wait(5))
        pump(self.root, timeout=0.1)
        job.cancel()
        cancelled_at[0] = True
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertEqual(calls["done"], 0)
        self.assertEqual(calls["cancelled"], ["частично"])
        self.assertEqual(calls["late_progress"], 0)
        self.assertFalse(job.running)

    def test_report_check_raises_cancelled(self):
        ev = threading.Event()
        got = []

        def work(report):
            while True:
                report.check()
                time.sleep(0.005)

        job = self.ctx.run_background(work, cancel_event=ev,
                                      on_cancelled=got.append,
                                      on_error=lambda e: got.append(("error", e)))
        pump(self.root, timeout=0.05)
        ev.set()                               # внешний Event тоже отменяет
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertEqual(got, [None])

    def test_error_goes_to_on_error_on_the_tk_thread(self):
        got = []

        def work(report):
            raise ValueError("плохой кадр")

        job = self.ctx.run_background(
            work, on_error=lambda e: got.append((threading.current_thread(), e)))
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertEqual(len(got), 1)
        self.assertIs(got[0][0], self.main)
        self.assertIsInstance(got[0][1], ValueError)

    def test_unhandled_error_and_broken_callback_reach_tk_hook(self):
        job = self.ctx.run_background(lambda report: 1 / 0)
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertTrue(any(isinstance(e, ZeroDivisionError) for e in self.tk_errors))

        done = []

        def bad_progress(_f, _t):
            raise RuntimeError("колбэк сломан")

        def work(report):
            report(0.1, "")
            time.sleep(0.05)
            return "ok"

        job = self.ctx.run_background(work, on_progress=bad_progress,
                                      on_done=done.append)
        self.assertTrue(pump(self.root, lambda: job.finished))
        self.assertEqual(done, ["ok"], "сломанный колбэк остановил опрос")

    def test_shutdown_detaches_jobs(self):
        calls = []
        job = self.ctx.run_background(
            lambda report: [time.sleep(0.01) for _ in iter(lambda: report.cancelled, True)],
            on_done=calls.append, on_cancelled=calls.append)
        hook = []
        self.ctx.register_shutdown(lambda: hook.append(1))
        self.ctx.shutdown()
        self.ctx.shutdown()
        self.assertEqual(hook, [1])
        self.assertTrue(job.cancelled)
        self.assertTrue(job.join(2))
        pump(self.root, timeout=0.2)
        self.assertEqual(calls, [])
        self.assertTrue(job.finished)
        with self.assertRaises(RuntimeError):
            self.ctx.run_background(lambda report: None)

    def test_publish_from_worker_is_delivered_on_tk_thread(self):
        got = []
        unsubscribe = self.ctx.subscribe("selection",
                                         lambda p: got.append((threading.current_thread(), p)))
        t = threading.Thread(target=lambda: self.ctx.set_selection(["a.JPG", "b.CR2"]))
        t.start()
        t.join()
        self.assertTrue(pump(self.root, lambda: got))
        self.assertIs(got[0][0], self.main)
        self.assertEqual(got[0][1], [Path("a.JPG"), Path("b.CR2")])
        self.assertEqual(self.ctx.selection, [Path("a.JPG"), Path("b.CR2")])
        # Копия: изменение снаружи не портит состояние контекста.
        self.ctx.selection.append(Path("x"))
        self.assertEqual(len(self.ctx.selection), 2)
        unsubscribe()
        self.ctx.publish("selection", [])
        self.assertEqual(len(got), 1)
        self.assertEqual(self.ctx.selection, [])

    def test_broken_subscriber_does_not_block_others(self):
        errors = []
        ctx = self.gc.AppContext(self.root, record_error=lambda w, t: errors.append(w))
        got = []
        ctx.subscribe("t", lambda p: 1 / 0)
        ctx.subscribe("t", got.append)
        ctx.publish("t", 5)
        self.assertEqual(got, [5])
        self.assertEqual(len(errors), 1)

    def test_log_reaches_subscribers_from_any_thread(self):
        got = []
        self.ctx.subscribe(self.gc.TOPIC_LOG, got.append)
        threading.Thread(target=lambda: self.ctx.log("из потока", "warn")).start()
        self.assertTrue(pump(self.root, lambda: got))
        self.assertEqual(got[0], ("из потока", "warn"))
        self.assertEqual(self.ctx.log_lines[-1], ("из потока", "warn"))

    def test_record_error_formats_the_traceback(self):
        written = []
        ctx = self.gc.AppContext(self.root,
                                 record_error=lambda w, t: written.append((w, t)) or
                                 self.tmp / "e.log")
        try:
            raise KeyError("ключ")
        except KeyError as exc:
            path = ctx.record_error("проверка", exc)
        self.assertEqual(path, self.tmp / "e.log")
        self.assertEqual(written[0][0], "проверка")
        self.assertIn("Traceback", written[0][1])
        self.assertIn("KeyError", written[0][1])

    def test_photo_image_refuses_worker_threads(self):
        try:
            from PIL import Image
        except Exception as exc:
            self.skipTest("Pillow недоступен: %s" % exc)
        img = Image.new("RGB", (8, 8), "red")
        photo = self.gc.photo_image(img, master=self.root)
        self.assertEqual(photo.width(), 8)
        errors = []

        def worker():
            try:
                self.gc.photo_image(img)
            except RuntimeError as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("потока Tk", str(errors[0]))


# --------------------------------------------------------------------------
# Цвета
# --------------------------------------------------------------------------


class TestPalette(GuiTestCase):

    @staticmethod
    def _luma(color: str) -> float:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

    def test_every_role_has_a_colour_in_both_modes(self):
        for dark in (False, True):
            for role in REQUIRED_ROLES + self.gc.PALETTE_ROLES:
                with self.subTest(dark=dark, role=role):
                    color = self.gc.palette(role, dark=dark)
                    self.assertRegex(color, r"^#[0-9a-f]{6}$")

    def test_text_contrasts_with_its_background(self):
        """Цвета текста читаются на фоне карточки своего же режима."""
        for dark in (False, True):
            bg = self._luma(self.gc.palette("card_bg", dark=dark))
            for role in ("fg", "muted", "ok", "warn", "error", "accent"):
                with self.subTest(dark=dark, role=role):
                    fg = self._luma(self.gc.palette(role, dark=dark))
                    self.assertGreater(abs(fg - bg), 0.35)
        self.assertNotEqual(self.gc.palette("fg", dark=False),
                            self.gc.palette("fg", dark=True))

    def test_unknown_role_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            self.gc.palette("purple", dark=False)

    def test_detects_mode_from_the_real_window(self):
        root = self.open_root()
        dark = self.gc.is_dark_mode(root)
        self.assertIsInstance(dark, bool)
        self.assertEqual(self.gc.palette("fg", root),
                         self.gc.palette("fg", dark=dark))
        for role in REQUIRED_ROLES:
            root.winfo_rgb(self.gc.palette(role, root))     # Tk понимает цвет
        self.assertIsInstance(self.gc.system_prefers_dark(), bool)


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


class TestSettingsNamespaces(GuiTestCase):

    def test_tab_settings_round_trip(self):
        root = self.open_root()
        settings = self.gui.load_settings()
        ctx = self.gc.AppContext(root, settings=settings,
                                 save_settings=self.gui.save_settings)
        ctx.tab_settings("enhance")["strength"] = 0.7
        ctx.tab_settings("enhance")["folder"] = self.tmp / "Съёмка"   # Path
        ctx.tab_settings("cull")["keep"] = ["IMG_0001.CR2"]
        self.assertIs(ctx.tab_settings("enhance"), ctx.tab_settings("enhance"))
        ctx.save_settings()

        loaded = self.gui.load_settings()
        self.assertEqual(loaded["tabs"]["enhance"]["strength"], 0.7)
        self.assertEqual(loaded["tabs"]["enhance"]["folder"], str(self.tmp / "Съёмка"))
        self.assertEqual(loaded["tabs"]["cull"]["keep"], ["IMG_0001.CR2"])
        self.assertEqual(loaded["quality"], 95)          # прежние ключи на месте
        with self.assertRaises(ValueError):
            ctx.tab_settings("")

    def test_defaults_are_not_shared_between_loads(self):
        a = self.gui.load_settings()
        a["tabs"]["enhance"] = {"x": 1}
        b = self.gui.load_settings()
        self.assertEqual(b["tabs"], {})
        self.assertEqual(self.gui.DEFAULT_SETTINGS["tabs"], {})

    def test_malformed_tabs_do_not_break_loading(self):
        self.gui.SETTINGS_PATH.write_text(json.dumps({
            "src": "D:/keep", "tabs": {"enhance": {"a": 1}, "cull": 5, "poster": "x"},
            "last_tab": "enhance"}), encoding="utf-8")
        loaded = self.gui.load_settings()
        self.assertEqual(loaded["tabs"], {"enhance": {"a": 1}})
        self.assertEqual(loaded["last_tab"], "enhance")
        self.assertEqual(loaded["src"], "D:/keep")
        self.gui.SETTINGS_PATH.write_text('{"tabs": "мусор", "last_tab": null}',
                                          encoding="utf-8")
        loaded = self.gui.load_settings()
        self.assertEqual(loaded["tabs"], {})
        self.assertEqual(loaded["last_tab"], "")

    def test_old_settings_file_without_tabs_still_loads(self):
        old = {k: v for k, v in self.gui.DEFAULT_SETTINGS.items()
               if k not in ("tabs", "last_tab")}
        old["quality"] = 80
        self.gui.SETTINGS_PATH.write_text(json.dumps(old), encoding="utf-8")
        loaded = self.gui.load_settings()
        self.assertEqual(loaded["quality"], 80)
        self.assertEqual(loaded["tabs"], {})

    def test_standalone_context_uses_memory_settings(self):
        root = self.open_root()
        ctx = self.gc.AppContext(root)
        ctx.tab_settings("poster")["size"] = "1080x1350"
        ctx.save_settings()                              # без записи - не падает
        self.assertEqual(ctx.settings["tabs"]["poster"]["size"], "1080x1350")
        self.assertFalse(self.gui.SETTINGS_PATH.exists())

    def test_folder_memory_is_per_tab(self):
        root = self.open_root()
        ctx = self.gc.AppContext(root)
        self.gc._remember_dir(ctx, "enhance", self.tmp)
        self.assertEqual(self.gc._initial_dir(ctx, "enhance"), os.path.normpath(str(self.tmp)))
        self.assertNotEqual(ctx.tab_settings("cull").get(self.gc.LAST_DIR_KEY), str(self.tmp))


# --------------------------------------------------------------------------
# Снимки
# --------------------------------------------------------------------------


class TestImages(GuiTestCase):

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        try:
            from PIL import Image  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest("Pillow недоступен: %s" % exc)

    def test_filetypes_include_uppercase_variants(self):
        patterns = self.gc.IMAGE_FILETYPES[0][1]
        for ext in ("jpg", "jpeg", "png", "tif", "tiff", "cr2"):
            self.assertIn("*." + ext, patterns)
            self.assertIn("*." + ext.upper(), patterns)

    def test_jpeg_is_rotated_by_exif_and_downscaled(self):
        from PIL import Image
        path = self.tmp / "IMG_0001.JPG"
        exif = Image.Exif()
        exif[0x0112] = 6                                  # повернуть на 90
        Image.new("RGB", (800, 400), "white").save(path, "JPEG", exif=exif)
        before = path.stat().st_mtime_ns
        im = self.gc.load_image_fast(path, 200)
        self.assertEqual(im.size, (100, 200))
        self.assertEqual(im.mode, "RGB")
        full = self.gc.load_image_fast(path)
        self.assertEqual(full.size, (400, 800))
        self.assertEqual(path.stat().st_mtime_ns, before)  # только чтение

    def test_cr2_uses_embedded_jpeg_with_orientation(self):
        import make_test_cr2
        path = make_test_cr2.make_cr2(self.tmp / "IMG_0002.CR2", orientation=8,
                                      preview_size=(640, 424), raw_size=(640, 424))
        im = self.gc.load_image_fast(path, 320)
        self.assertEqual(max(im.size), 320)
        self.assertGreater(im.size[1], im.size[0])        # портрет после поворота

    def test_png_modes_are_normalised(self):
        from PIL import Image
        path = self.tmp / "deep.PNG"
        Image.new("I;16", (40, 20), 65535).save(path)
        im = self.gc.load_image_fast(path, 0)
        self.assertIn(im.mode, ("L", "RGB", "RGBA"))
        self.assertEqual(im.getpixel((0, 0)), 255)

    def test_bad_file_raises_readable_error(self):
        bad = self.tmp / "broken.jpg"
        bad.write_bytes(b"not a jpeg")
        with self.assertRaises(self.gc.ImageLoadError) as cm:
            self.gc.load_image_fast(bad, 100)
        self.assertIn("broken.jpg", str(cm.exception))
        with self.assertRaises(self.gc.ImageLoadError):
            self.gc.load_image_fast(self.tmp / "нет.jpg", 100)
        not_cr2 = self.tmp / "fake.CR2"
        not_cr2.write_bytes(b"\0" * 64)
        with self.assertRaises(self.gc.ImageLoadError):
            self.gc.load_image_fast(not_cr2, 100)

    def test_thumbnail_leaves_the_original_alone(self):
        from PIL import Image
        img = Image.new("RGB", (1000, 500))
        small = self.gc.thumbnail(img, 100)
        self.assertEqual(small.size, (100, 50))
        self.assertEqual(img.size, (1000, 500))
        self.assertEqual(self.gc.thumbnail(img, 5000).size, (1000, 500))

    def test_list_images_is_case_insensitive_and_skips_junk(self):
        for name in ("b.JPG", "a.cr2", "._b.JPG", "notes.txt", "c.Tiff"):
            (self.tmp / name).write_bytes(b"x")
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "d.png").write_bytes(b"x")
        names = [p.name for p in self.gc.list_images(self.tmp)]
        self.assertEqual(names, ["a.cr2", "b.JPG", "c.Tiff"])
        deep = [p.name for p in self.gc.list_images(self.tmp, recursive=True)]
        self.assertIn("d.png", deep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
