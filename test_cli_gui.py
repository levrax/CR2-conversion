# -*- coding: utf-8 -*-
"""Дымовые тесты для слоёв поверх ядра: cr2_convert.py и cr2_gui.pyw.

До этого файла оба модуля не имели ни одного теста — даже проверки импорта.
Именно там живут дефекты, которые ядро поймать не может: разбор аргументов,
экранирование масок, вывод в консоль с чужой кодировкой, создание файлов при
старте GUI.

Тесты не требуют Pillow/rawpy и не открывают ни одного настоящего окна:
GUI собирается под Tk() только если дисплей доступен, иначе тест пропускается.
"""
from __future__ import annotations

import importlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cr2_core  # noqa: E402
import make_test_cr2  # noqa: E402


def load_gui():
    """Импортировать cr2_gui.pyw под своим именем.

    Регистрация в sys.modules обязательна: без неё @dataclass внутри модуля
    падает с AttributeError, потому что dataclasses ищет sys.modules[__module__].

    Проверка tkinter и cr2_core идёт ДО exec_module и превращает их отсутствие
    в skip: cr2_gui на уровне модуля импортирует оба и на неудаче зовёт
    _fatal_bootstrap.  Тот больше не делает os._exit(1) из импортированного
    модуля, но пробрасывать сюда голое исключение всё равно незачем - на
    машине без tcl/tk эти тесты просто нечего запускать.
    """
    for name in ("tkinter", "cr2_core"):
        try:
            importlib.import_module(name)
        except BaseException as exc:
            raise unittest.SkipTest("%s недоступен: %s" % (name, exc))
    # loader задаётся явно: importlib признаёт .pyw исходником только на
    # Windows, а на macOS и Linux spec_from_file_location вернул бы None.
    path = str(HERE / "cr2_gui.pyw")
    spec = importlib.util.spec_from_file_location(
        "cr2_gui", path, loader=SourceFileLoader("cr2_gui", path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cr2_gui"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCliSmoke(unittest.TestCase):
    """cr2_convert.py: запуск, разбор путей, кодировка вывода."""

    def _run(self, args, **kw):
        env = dict(os.environ, PYTHONIOENCODING=kw.pop("enc", "utf-8"))
        return subprocess.run([sys.executable, "cr2_convert.py", *args],
                              cwd=str(HERE), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=env, timeout=180, **kw)

    def test_help_works(self):
        p = self._run(["--help"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), "--help ничего не напечатал")

    def test_help_does_not_promise_dpp_edits(self):
        """Текст помощи не должен утверждать, что правки DPP применяются."""
        p = self._run(["--help"])
        low = p.stdout.lower()
        self.assertNotIn("применяет рецепт", low)
        self.assertNotIn("с правками dpp", low)

    def test_console_encoding_does_not_crash(self):
        """Однобайтовая кодовая страница не должна ронять вывод."""
        for enc in ("cp866", "cp1251"):
            with self.subTest(encoding=enc):
                p = self._run(["--help"], enc=enc)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn("UnicodeEncodeError", p.stderr)

    def test_bracketed_folder_is_not_treated_as_glob_class(self):
        """Папка «Съёмка [2024]» — скобки литеральные, а не класс символов."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "Съёмка [2024]"
            src.mkdir()
            make_test_cr2.make_cr2(str(src / "IMG_0001.CR2"))
            out = Path(td) / "out"
            out.mkdir()
            p = self._run([str(src), "-o", str(out)])
            made = sorted(f for f in os.listdir(out) if f.lower().endswith(".jpg"))
            self.assertEqual(len(made), 1, f"rc={p.returncode} out={made}\n{p.stdout}\n{p.stderr}")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            make_test_cr2.make_cr2(str(Path(td) / "a.CR2"))
            before = sorted(os.listdir(td))
            p = self._run(["--dry-run", td])
            self.assertEqual(sorted(os.listdir(td)), before,
                             "--dry-run изменил содержимое папки")
            self.assertEqual(p.returncode, 0, p.stderr)


class TestGuiSmoke(unittest.TestCase):
    """cr2_gui.pyw: импорт, аварийный журнал, сборка окна."""

    LOG = HERE / "cr2_gui_error.log"

    def setUp(self):
        self._had_log = self.LOG.exists()
        self._saved = self.LOG.read_bytes() if self._had_log else None
        if self._had_log:
            self.LOG.unlink()

    def tearDown(self):
        if self.LOG.exists():
            self.LOG.unlink()
        if self._had_log and self._saved is not None:
            self.LOG.write_bytes(self._saved)

    def test_import_creates_no_error_log(self):
        """Регрессия: пустой cr2_gui_error.log появлялся при каждом запуске.

        _error_log_path() проверял доступность через open(..., 'a') и тем самым
        создавал файл. Пользователь видел «журнал ошибок» и решал, что упало.
        """
        load_gui()
        self.assertFalse(self.LOG.exists(),
                         "импорт GUI создал журнал ошибок, хотя ошибок не было")

    def test_record_error_actually_writes(self):
        gui = load_gui()
        path = gui.record_error("проверка", "трассировка")
        self.assertTrue(Path(path).exists())
        self.assertIn("проверка", Path(path).read_text(encoding="utf-8"))

    def test_title_does_not_claim_dpp_edits(self):
        src = (HERE / "cr2_gui.pyw").read_text(encoding="utf-8")
        import re
        m = re.search(r'root\.title\(\s*["\'](.+?)["\']', src)
        self.assertIsNotNone(m, "не найден вызов root.title()")
        title = m.group(1).lower()
        self.assertNotIn("с правками dpp", title)
        self.assertNotIn("правки dpp", title)

    def test_window_builds_and_closes(self):
        # Проба дисплея идёт ДО load_gui(): сборка окна без него бессмысленна.
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:                      # нет дисплея / нет tcl-tk
            self.skipTest(f"Tk недоступен: {exc}")
        gui = load_gui()
        try:
            root.withdraw()
            app = gui.App(root) if hasattr(gui, "App") else None
            if app is None:
                self.skipTest("в модуле нет класса App — нечего собирать")
            for _ in range(5):
                root.update()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
