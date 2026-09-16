# -*- coding: utf-8 -*-
"""Сквозные тесты: настоящие вкладки в настоящей оболочке, самопроверка
сборки и то, что обязано держаться при выпуске в публичный репозиторий.

Тесты отдельных вкладок и движков живут в test_tab_*.py и test_enhance /
test_cull / test_poster; здесь - только стыки между ними.
"""
from __future__ import annotations

import gc
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TAB_NAMES = ("tab_cull", "tab_enhance", "tab_poster")          # порядок работы
#: Папки, которых нет в репозитории (сборка, кэши, окружения).
NOT_SHIPPED = {".git", "build", "dist", "__pycache__", ".venv", "venv"}


def repo_files() -> list[Path]:
    """Файлы папки программы без сборки, кэшей и .git (os.walk с отсечением)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(HERE):
        dirnames[:] = [d for d in dirnames if d not in NOT_SHIPPED]
        out += [Path(dirpath) / f for f in filenames]
    return sorted(out)


def load_gui():
    """cr2_gui.pyw под отдельным именем, с явным SourceFileLoader (см. test_shell)."""
    name = "cr2_gui_integration_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = str(HERE / "cr2_gui.pyw")
    spec = importlib.util.spec_from_file_location(
        name, path, loader=SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # до exec_module: нужно @dataclass
    spec.loader.exec_module(mod)
    return mod


def require(*modules: str) -> None:
    """SkipTest, если библиотеки нет (из исходников вкладки необязательны)."""
    for name in modules:
        if importlib.util.find_spec(name) is None:
            raise unittest.SkipTest("нет библиотеки %s" % name)


def pump(root, until=lambda: False, timeout: float = 10.0) -> bool:
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


# --------------------------------------------------------------------------
# Контракт настоящих модулей вкладок
# --------------------------------------------------------------------------


class TestRealTabModules(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        require("tkinter", "PIL", "numpy")
        cls.gui = load_gui()

    def test_shell_lists_exactly_the_shipped_tabs_in_workflow_order(self):
        self.assertEqual(tuple(self.gui.TAB_MODULES), TAB_NAMES)

    def test_every_tab_module_meets_the_contract(self):
        for name in TAB_NAMES:
            with self.subTest(tab=name):
                module = importlib.import_module(name)
                self.assertIsInstance(module.TAB_TITLE, str)
                self.assertTrue(module.TAB_TITLE.strip())
                self.assertTrue(callable(module.build_tab))

    def test_error_panel_titles_match_the_real_tab_titles(self):
        """Панель «вкладка не загрузилась» называет вкладку так же, как вкладка себя."""
        for name in TAB_NAMES:
            with self.subTest(tab=name):
                module = importlib.import_module(name)
                self.assertEqual(self.gui._TAB_FALLBACK_TITLES[name], module.TAB_TITLE)

    def test_self_test_covers_every_tab_module(self):
        import app
        for name in TAB_NAMES + ("gui_common", "enhance", "cull", "poster", "brand", "cv2"):
            self.assertIn(name, app.SELF_TEST_MODULES)


class TestWindowFitsTheWorkArea(unittest.TestCase):
    """Окно не должно уходить низом под панель задач (замечено на 2880x1800, 200 %)."""

    @classmethod
    def setUpClass(cls) -> None:
        require("tkinter")
        cls.gui = load_gui()

    def test_window_leaves_room_for_title_bar_and_taskbar(self):
        class Screen:
            def __init__(self, w: int, h: int) -> None:
                self.w, self.h = w, h

            def winfo_screenwidth(self) -> int:
                return self.w

            def winfo_screenheight(self) -> int:
                return self.h

        for (sw, sh), scale in (((1366, 768), 1.25), ((2880, 1800), 2.0),
                                ((1440, 900), 1.0), ((3840, 2160), 1.5)):
            with self.subTest(screen=(sw, sh)):
                geo = self.gui._fit_geometry(Screen(sw, sh), int(1180 * scale),
                                             int(820 * scale))
                w, h, x, y = map(int, re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", geo).groups())
                self.assertLessEqual(x + w, sw)
                # Заголовок окна ~32 px и панель задач ~48 px при 96 DPI.
                self.assertLessEqual(y + h + int(80 * scale), sh)
                # Урезанное по высоте окно открывается развёрнутым.
                self.assertEqual(self.gui._fit_clips_height(Screen(sw, sh), int(820 * scale)),
                                 h < int(820 * scale))
        self.assertTrue(self.gui._fit_clips_height(Screen(2880, 1800), 1640))
        self.assertFalse(self.gui._fit_clips_height(Screen(3840, 2160), 1230))


class TestShellWithRealTabs(unittest.TestCase):
    """Окно целиком: конвертер и три настоящие вкладки, без единой панели ошибки."""

    @classmethod
    def setUpClass(cls) -> None:
        require("tkinter", "PIL", "numpy", "cr2_core")
        cls.gui = load_gui()

    def setUp(self) -> None:
        import tkinter as tk
        self._td = tempfile.TemporaryDirectory(prefix="cr2_integration_")
        self.addCleanup(self._td.cleanup)
        tmp = Path(self._td.name)
        # Настройки и журнал - только во временной папке.  Файл настроек рядом
        # с программой уже однажды перезаписали ручной проверкой.
        saved = (self.gui.SETTINGS_PATH, self.gui.ERROR_LOG_PATH)
        self.gui.SETTINGS_PATH = tmp / "settings.json"
        self.gui.ERROR_LOG_PATH = tmp / "error.log"
        self.addCleanup(self._restore_paths, saved)
        try:
            self.root = tk.Tk()
        except Exception as exc:
            raise unittest.SkipTest("Tk недоступен: %s" % exc)
        self.root.withdraw()
        self.tk_errors: list[BaseException] = []
        self.root.report_callback_exception = (
            lambda _t, exc, _tb: self.tk_errors.append(exc))
        self.settings = self.gui.load_settings()
        self.shell = self.gui.Shell(self.root, settings=self.settings)
        self.addCleanup(self._close)

    def _restore_paths(self, saved) -> None:
        self.gui.SETTINGS_PATH, self.gui.ERROR_LOG_PATH = saved

    def _close(self) -> None:
        self.shell.ctx.shutdown()
        for job in list(self.shell.ctx._jobs):          # noqa: SLF001
            job.join(10)
        if self.shell.app is not None:
            self.shell.app._destroy()                    # noqa: SLF001
        try:
            self.root.destroy()
        except Exception:
            pass
        gc.collect()        # мусор Tk - в потоке Tk, см. test_shell

    def test_all_four_tabs_load_without_error_panels(self):
        pump(self.root, timeout=0.3)
        self.assertEqual([t.key for t in self.shell.tabs],
                         ["convert", "cull", "enhance", "poster"])
        for rec in self.shell.tabs:
            with self.subTest(tab=rec.key):
                self.assertEqual(rec.error, "")
        self.assertFalse(self.gui.ERROR_LOG_PATH.exists(),
                         "при построении вкладок записаны ошибки")

    def test_each_tab_can_be_opened_and_is_remembered(self):
        for key in ("enhance", "cull", "poster", "convert"):
            with self.subTest(tab=key):
                self.assertTrue(self.shell.select_tab(key))
                pump(self.root, timeout=0.3)
                self.assertEqual(self.shell.current_key(), key)
                self.assertEqual(self.settings["last_tab"], key)
        self.assertEqual(self.tk_errors, [])
        # Смена вкладки пишет настройки - во временную папку, а не рядом с программой.
        self.assertTrue(self.gui.SETTINGS_PATH.is_file())

    def _button_texts(self, key: str) -> list[str]:
        frame = next(t.frame for t in self.shell.tabs if t.key == key)
        texts, stack = [], [frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            try:
                texts.append(str(widget.cget("text")))
                var = str(widget.cget("textvariable"))
                if var:
                    texts.append(str(widget.getvar(var)))
            except Exception:
                pass
        return texts

    def test_selection_from_cull_reaches_the_other_tabs(self):
        """Шина событий общая: отмеченное в «Отборе» видно в «Обработке» и «Афишах»."""
        picked = [Path(self._td.name) / "IMG_0001.JPG", Path(self._td.name) / "IMG_0002.JPG"]
        seen: list = []
        self.shell.ctx.subscribe("selection", seen.append)
        self.shell.ctx.set_selection(picked)
        pump(self.root, timeout=0.3)
        self.assertEqual(seen, [picked])
        self.assertEqual(self.shell.ctx.selection, picked)
        self.assertIn("Из отмеченных в «Отборе» (2)", self._button_texts("poster"))
        self.assertIn("Взять отмеченные в «Отборе» (2)", self._button_texts("enhance"))
        self.assertEqual(self.tk_errors, [])


# --------------------------------------------------------------------------
# Интерпретатор Tcl не должен умирать в рабочем потоке
# --------------------------------------------------------------------------


class TestTclInterpreterIsFreedOnlyInTkThread(unittest.TestCase):
    """Регрессия: полный прогон тестов падал с «Tcl_AsyncDelete: async handler
    deleted by the wrong thread» - abort() всего процесса без трассировки.

    Сценарий: окно, отданное AppContext, уничтожено, но осталось в цикле
    ссылок; сборщик мусора разбирает цикл в рабочем потоке.  Проверяется в
    отдельном процессе, потому что без исправления процесс просто умирает.
    """

    SCRIPT = textwrap.dedent("""
        import gc, sys, threading, tkinter as tk
        sys.path.insert(0, sys.argv[1])
        import gui_common
        gc.disable()
        try:
            root = tk.Tk()
        except Exception:
            print("NO-TK")
            sys.exit(0)
        root.withdraw()
        ctx = gui_common.AppContext(root, settings={})
        ctx.shutdown()

        class Holder:
            pass

        holder = Holder()
        holder.root, holder.me = root, holder     # цикл: освободит только gc
        root.destroy()
        del ctx, root, holder
        worker = threading.Thread(target=gc.collect)
        worker.start()
        worker.join()
        print("SURVIVED")
    """)

    def test_root_garbage_collected_in_a_worker_thread_does_not_abort(self):
        require("tkinter")
        done = subprocess.run([sys.executable, "-c", self.SCRIPT, str(HERE)],
                              capture_output=True, timeout=60)
        out = done.stdout.decode("utf-8", "replace")
        if "NO-TK" in out:
            self.skipTest("Tk недоступен")
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertIn("SURVIVED", out)


class TestConverterTimerDiesWithItsWindow(unittest.TestCase):
    """Регрессия: полный прогон печатал «invalid command name ..._poll».

    test_cli_gui закрывал окно конвертера через root.destroy(), минуя
    App._destroy; таймер _poll оставался в очереди Tcl потока и срабатывал на
    update() следующего окна - уже без своей команды.  Tcl печатает это прямо
    в stderr, поэтому проверка - в отдельном процессе.
    """

    SCRIPT = textwrap.dedent("""
        import sys, tkinter as tk
        from importlib.machinery import SourceFileLoader
        import importlib.util
        sys.path.insert(0, sys.argv[1])
        try:
            root = tk.Tk()
        except Exception:
            print("NO-TK")
            sys.exit(0)
        path = sys.argv[1] + "/cr2_gui.pyw"
        spec = importlib.util.spec_from_file_location(
            "gui_timer_probe", path, loader=SourceFileLoader("gui_timer_probe", path))
        gui = importlib.util.module_from_spec(spec)
        sys.modules["gui_timer_probe"] = gui
        spec.loader.exec_module(gui)
        root.withdraw()
        gui.App(root)
        for _ in range(3):
            root.update()
        root.destroy()
        other = tk.Tk()
        other.withdraw()
        other.after(4 * gui.POLL_MS, other.quit)
        other.mainloop()
        other.destroy()
        print("DONE")
    """)

    def test_destroying_the_root_directly_cancels_the_poll_timer(self):
        require("tkinter")
        with tempfile.TemporaryDirectory(prefix="cr2_timer_") as tmp:
            done = subprocess.run([sys.executable, "-c", self.SCRIPT, str(HERE)],
                                  capture_output=True, timeout=120, cwd=tmp)
        out = done.stdout.decode("utf-8", "replace")
        err = done.stderr.decode("utf-8", "replace")
        if "NO-TK" in out:
            self.skipTest("Tk недоступен")
        self.assertEqual(done.returncode, 0, err)
        self.assertIn("DONE", out)
        self.assertNotIn("invalid command name", err)


# --------------------------------------------------------------------------
# Самопроверка, которой CI проверяет собранный бандл
# --------------------------------------------------------------------------


class TestSelfTest(unittest.TestCase):

    def test_self_test_passes_from_source(self):
        require("tkinter", "PIL", "numpy", "rawpy", "cv2")
        with tempfile.TemporaryDirectory(prefix="cr2_selftest_") as tmp:
            report = Path(tmp) / "отчёт.txt"
            done = subprocess.run([sys.executable, str(HERE / "app.py"),
                                   "--self-test", str(report)],
                                  capture_output=True, timeout=180)
            text = report.read_text(encoding="utf-8") if report.exists() else ""
        self.assertEqual(done.returncode, 0, text or done.stderr.decode("utf-8", "replace"))
        self.assertNotIn("FAIL", text)
        for line in ("ok    import tab_cull", "ok    поиск лиц (OpenCV)",
                     "ok    шрифты fonts/", "ok    движок афиш"):
            self.assertIn(line, text)

    def test_self_test_reports_a_missing_module_and_exits_1(self):
        require("tkinter")
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, sys.argv[1])
            import app
            app.SELF_TEST_MODULES = app.SELF_TEST_MODULES + ("tab_that_does_not_exist",)
            sys.exit(app.self_test(sys.argv[2]))
        """)
        with tempfile.TemporaryDirectory(prefix="cr2_selftest_") as tmp:
            report = Path(tmp) / "report.txt"
            done = subprocess.run([sys.executable, "-c", script, str(HERE), str(report)],
                                  capture_output=True, timeout=180)
            text = report.read_text(encoding="utf-8")
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAIL  import tab_that_does_not_exist", text)


# --------------------------------------------------------------------------
# Публичный репозиторий и выпуск
# --------------------------------------------------------------------------


class TestReleaseHygiene(unittest.TestCase):

    def test_opencv_is_the_headless_build_with_haar_cascades(self):
        lines = [ln.split("#", 1)[0].strip()
                 for ln in (HERE / "requirements.txt").read_text(encoding="utf-8").splitlines()]
        reqs = [ln for ln in lines if ln]
        opencv = [ln for ln in reqs if ln.lower().startswith("opencv")]
        self.assertTrue(opencv, "в requirements.txt нет OpenCV")
        for ln in opencv:
            self.assertTrue(ln.lower().startswith("opencv-python-headless"), ln)
            # 5.0 выпускает колесо без cv2/data/haarcascade_*.xml.
            self.assertRegex(ln, r"<\s*5\b")

    def test_every_bundled_font_carries_its_ofl_licence(self):
        fonts = HERE / "fonts"
        found = [p for p in fonts.iterdir() if p.suffix.lower() in (".ttf", ".otf")]
        self.assertTrue(found)
        for font in found:
            with self.subTest(font=font.name):
                licence = fonts / ("%s-OFL.txt" % font.stem.split("-")[0])
                self.assertTrue(licence.is_file(), "нет %s" % licence.name)
                self.assertIn("SIL Open Font License",
                              licence.read_text(encoding="utf-8"))

    def test_gitignore_keeps_unlicensed_fonts_out_of_commits(self):
        """Morfin Sans в fonts/ для пробы не должен попасть в `git add -A`."""
        text = (HERE / ".gitignore").read_text(encoding="utf-8")
        patterns = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertTrue(any("morfin" in ln.lower() for ln in patterns), patterns)
        self.assertIn("fonts/*.otf", patterns)
        git = shutil.which("git")
        if git is None or not (HERE / ".git").exists():
            self.skipTest("нет git или это не рабочая копия")
        for name in ("fonts/MorfinSans-Regular.otf", "fonts/Morfin Sans.ttf",
                     "fonts/morfinsans-bold.TTF"):
            with self.subTest(name=name):
                done = subprocess.run([git, "check-ignore", "-q", "--no-index", name],
                                      cwd=str(HERE), capture_output=True)
                self.assertEqual(done.returncode, 0, "git не игнорирует %s" % name)

    def test_no_font_without_a_redistribution_licence_is_in_the_repository(self):
        """Лицензия Morfin Sans не даёт права распространять файл шрифта."""
        for path in repo_files():
            self.assertNotIn("morfin", path.name.lower(), str(path))

    def test_no_personal_paths_or_private_links_in_shipped_text(self):
        # Шаблоны собраны из частей, чтобы этот файл не находил сам себя.
        needles = [re.compile(p, re.IGNORECASE) for p in (
            r"[\\/]Users[\\/]" + "Len" + "ovo",
            r"disk\." + "yandex",
            r"yadi\." + "sk",
            # Номера кадров настоящей съёмки пользователя (счётчик камеры шёл
            # 52xx-58xx) и дата события: только синтетика вроде IMG_0001.
            r"(?:IMG|_MG)_5[2-8]" + r"\d\d",
            r"12\.09\.20" + "26",
            r"2026:09:" + "12",
        )]
        suffixes = {".py", ".pyw", ".md", ".txt", ".spec", ".yml", ".yaml",
                    ".bat", ".js", ".json", ".cfg", ".toml"}
        for path in repo_files():
            rel = path.relative_to(HERE)
            if path.suffix.lower() not in suffixes:
                continue
            if path.name in ("cr2_gui_settings.json",):    # в .gitignore
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                with self.subTest(file=str(rel), pattern=needle.pattern):
                    self.assertIsNone(needle.search(text))


if __name__ == "__main__":
    unittest.main()
