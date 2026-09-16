# -*- coding: utf-8 -*-
"""app.py — точка входа для PyInstaller.

Запускает ровно то же окно, что и двойной щелчок по cr2_gui.pyw.  Вся логика
живёт в cr2_gui.pyw, здесь её НЕТ и быть не должно: этот файл только
1) находит и загружает модуль cr2_gui,
2) ставит перехватчики аварий раньше, чем что-либо успеет упасть,
3) в замороженной сборке уводит настройки и журнал ошибок туда, куда
   пользователю действительно можно писать,
4) вызывает cr2_gui.main(),
5) с ключом --self-test вместо окна проверяет, что в сборку попало всё
   для всех вкладок (см. self_test; им пользуется дымовой тест CI).

ЗАЧЕМ ДВА ПУТИ ЗАГРУЗКИ.  Расширение .pyw импортируется по-разному на разных
системах: importlib добавляет '.pyw' в SOURCE_SUFFIXES ТОЛЬКО на Windows
(os.name == 'nt'), поэтому `import cr2_gui` здесь находит cr2_gui.pyw сам, а на
macOS и Linux — нет.  Полагаться на это нельзя, приложение собирается и под
macOS.  Отсюда:

* `import cr2_gui` — собранное приложение (cr2app.spec кладёт в сборку копию
  под именем cr2_gui.py, см. комментарий в спеке), запуск из исходников на
  Windows, а также случай, если файл когда-нибудь переименуют в cr2_gui.py;
* загрузка cr2_gui.pyw через importlib по явному пути — macOS и Linux.

Во втором случае модуль ОБЯЗАТЕЛЬНО регистрируется в sys.modules ДО
exec_module(): @dataclass ищет модуль по __module__ в sys.modules, чтобы
разобрать аннотации, и без регистрации падает на классе MRow/MCaps.

Запуск из исходников:   pythonw app.py     (или python app.py)
Сборка:                 pyinstaller --noconfirm cr2app.spec
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

APP_TITLE = "Конвертер CR2"
APP_DIRNAME = "CR2 Converter"          # имя папки в профиле пользователя
GUI_MODULE = "cr2_gui"
GUI_SOURCE = "cr2_gui.pyw"

IS_FROZEN = bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------
# Под pythonw.exe и в оконной сборке sys.stdout/sys.stderr равны None.
# Любой print() из любой библиотеки убил бы процесс молча, поэтому заглушка
# ставится ПЕРВОЙ строкой работы, до загрузки чего бы то ни было.
# --------------------------------------------------------------------------

try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
except Exception:                                   # pragma: no cover
    pass


# --------------------------------------------------------------------------
# Куда писать настройки и журнал
# --------------------------------------------------------------------------


def _user_data_dir() -> Path:
    """Папка для данных приложения в профиле пользователя."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_DIRNAME


def _is_writable(path: Path) -> bool:
    """Проверка записью, а не os.access: под Windows права на папку врут."""
    probe = path / (".write_probe.%d" % os.getpid())
    try:
        path.mkdir(parents=True, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
    except Exception:
        return False
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass
    return True


def _frozen_data_dir() -> Path:
    """Папка для cr2_gui_settings.json и cr2_gui_error.log в сборке.

    В исходниках cr2_gui кладёт их рядом с собой — это правильно для папки на
    рабочем столе.  В собранном виде «рядом с модулем» — это _internal внутри
    дистрибутива: в Program Files туда писать нельзя, а внутри .app запись
    ломает подпись Apple.  Поэтому: рядом с .exe, если туда пускают (обычный
    случай «распаковал zip в свою папку»), иначе профиль пользователя.
    """
    exe_dir = Path(sys.executable).resolve().parent
    inside_bundle = ".app/Contents/MacOS" in exe_dir.as_posix()
    if not inside_bundle and _is_writable(exe_dir):
        return exe_dir
    return _user_data_dir()


def _retarget_app_paths(gui) -> None:
    """Переставить пути модуля cr2_gui на записываемую папку (только в сборке).

    Все три имени читаются модулем в момент вызова (_error_log_path() возвращает
    ERROR_LOG_PATH, load_settings/save_settings читают SETTINGS_PATH), поэтому
    подмена здесь — до main() — действует на всё приложение.
    """
    if not IS_FROZEN:
        return
    try:
        data_dir = _frozen_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        gui.APP_DIR = data_dir
        gui.SETTINGS_PATH = data_dir / "cr2_gui_settings.json"
        gui.ERROR_LOG_PATH = data_dir / "cr2_gui_error.log"
    except Exception:
        # Не смертельно: record_error сам свалится во временную папку.
        pass


# --------------------------------------------------------------------------
# Аварийное сообщение ДО того, как загружен cr2_gui
# --------------------------------------------------------------------------


def _applescript_string(text: str) -> str:
    """Строковый литерал AppleScript. Без него кавычка в пути ломает скрипт."""
    body = (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))
    return '"%s"' % body


def _native_dialog(title: str, text: str) -> bool:
    """Системное окно с ошибкой без tkinter. Никогда не бросает исключение.

    Повторяет cr2_gui.native_error_dialog, и повторяет намеренно: _fatal()
    существует ровно для случая «cr2_gui не загрузился», то есть позвать
    оригинал неоткуда.  subprocess импортируется ВНУТРИ функции - app.py
    сознательно почти ничего не тянет на уровне модуля, чтобы _fatal работал
    в полуживом интерпретаторе.
    """
    try:
        import subprocess
    except Exception:
        return False
    if sys.platform == "darwin":
        script = ("display dialog %s with title %s buttons {\"OK\"} "
                  "default button 1 with icon stop"
                  % (_applescript_string(text), _applescript_string(title)))
        cmds = [["osascript", "-e", script]]
    else:
        cmds = [
            ["zenity", "--error", "--no-wrap", "--title", title, "--text", text],
            ["kdialog", "--title", title, "--error", text],
            ["xmessage", "-center", "%s\n\n%s" % (title, text)],
        ]
    for cmd in cmds:
        try:
            done = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=600,
                                  check=False)
        except Exception:
            continue
        # Ненулевой код - окно не показано (обычно нет дисплея): пробуем
        # следующую программу, а не рапортуем об успехе.
        if done.returncode == 0:
            return True
    return False


def _fatal(header: str, detail: str) -> None:
    """Показать окно и выйти. Работает без tkinter и без cr2_gui.

    Та же логика, что в cr2_gui._fatal_bootstrap: запись в журнал, СИСТЕМНОЕ
    окно (на всех трёх платформах), выход.  Раньше окно было только на
    Windows, а всё остальное уходило в sys.stderr - который в приложении,
    запущенном из Finder, выше по этому же файлу подменён на os.devnull.
    То есть на macOS отказ загрузки выглядел так: значок в Dock подпрыгнул
    один раз и исчез, ни окна, ни намёка на то, что где-то есть журнал.
    """
    text = "%s\n\n%s" % (header, detail)
    where = ""
    try:
        log = _user_data_dir() / "cr2_gui_error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8", errors="replace") as f:
            f.write("\n=== app.py bootstrap ===\n%s\n" % text)
        where = "\n\nПодробности записаны в файл:\n%s" % log
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text + where, APP_TITLE, 0x10)
        except Exception:
            pass
    else:
        try:
            _native_dialog(APP_TITLE, text + where)
        except Exception:
            pass
        # stderr пишем ВСЕГДА, а не только при неудаче окна: когда приложение
        # запущено из терминала, именно этот текст и нужен.
        try:
            sys.stderr.write(text + where + "\n")
            sys.stderr.flush()
        except Exception:
            pass
    os._exit(1)


# --------------------------------------------------------------------------
# Загрузка cr2_gui
# --------------------------------------------------------------------------


def _source_dir() -> Path:
    """Папка, в которой лежит cr2_gui.pyw рядом с этим файлом."""
    if IS_FROZEN:
        # .pyw мог быть положен в сборку как данные (запасной путь).
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    try:
        return Path(__file__).resolve().parent
    except NameError:                               # pragma: no cover
        return Path(os.getcwd())


def load_gui():
    """Вернуть модуль cr2_gui, как бы он ни назывался на диске."""
    base = _source_dir()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    # 1. Обычный импорт: собранное приложение и вариант с cr2_gui.py.
    try:
        import cr2_gui                               # noqa: F401
        return sys.modules[GUI_MODULE]
    except ImportError:
        pass
    except BaseException as exc:                     # модуль есть, но упал
        # cr2_gui сам зовёт _fatal_bootstrap и os._exit(1) на своих ошибках
        # загрузки, так что сюда попадает только что-то неожиданное.
        _fatal("Не удалось загрузить модуль %s." % GUI_MODULE,
               "%s: %s\n\n%s" % (type(exc).__name__, exc, traceback.format_exc()))

    # 2. Запуск из папки с исходниками: cr2_gui.pyw нельзя импортировать по имени.
    import importlib.util
    from importlib.machinery import SourceFileLoader

    path = base / GUI_SOURCE
    if not path.is_file():
        _fatal(
            "Не найден файл %s." % GUI_SOURCE,
            "Ожидался здесь:\n%s\n\nПоложите app.py рядом с %s и cr2_core.py."
            % (path, GUI_SOURCE),
        )

    loader = SourceFileLoader(GUI_MODULE, str(path))
    spec = importlib.util.spec_from_file_location(GUI_MODULE, str(path), loader=loader)
    if spec is None or spec.loader is None:          # pragma: no cover
        _fatal("Не удалось подготовить загрузку %s." % GUI_SOURCE, str(path))
    module = importlib.util.module_from_spec(spec)
    # Регистрация ДО exec_module обязательна: @dataclass ищет модуль в
    # sys.modules по __module__, иначе TypeError на первом же датаклассе.
    sys.modules[GUI_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        sys.modules.pop(GUI_MODULE, None)
        _fatal("Не удалось выполнить %s." % GUI_SOURCE,
               "%s: %s\n\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
    return module


# --------------------------------------------------------------------------
# Самопроверка собранного приложения (без окна)
# --------------------------------------------------------------------------
#
# Оконный exe не печатает ничего, а консольный собран из другого графа
# импортов (cr2_convert.py) и о вкладках не знает вовсе.  Поэтому дымовой тест
# CI запускает САМО окно с ключом --self-test: оно импортирует всё, что должно
# было попасть в сборку, прогоняет по крошечному примеру через каждый движок и
# выходит с кодом 0 или 1.  Отчёт пишется в файл (у оконного exe на Windows
# stdout нет) и, если есть куда, в stdout.  Окно Tk при этом не создаётся.

SELF_TEST_FLAG = "--self-test"

#: Модули, без которых сборка неполна.  Порядок - порядок отчёта.
SELF_TEST_MODULES: tuple[str, ...] = (
    "cr2_core", "cr2_gui", "gui_common",
    "enhance", "cull", "brand", "poster",
    "tab_enhance", "tab_cull", "tab_poster",
    "PIL", "numpy", "rawpy", "cv2",
)


def _import_for_self_test(name: str):
    """Импорт без _fatal: самопроверка сообщает об ошибке, а не показывает окно."""
    import importlib
    if name != GUI_MODULE:
        return importlib.import_module(name)
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
    # Исходники на macOS и Linux: .pyw по имени не импортируется.
    import importlib.util
    from importlib.machinery import SourceFileLoader
    path = _source_dir() / GUI_SOURCE
    spec = importlib.util.spec_from_file_location(
        name, str(path), loader=SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def self_test(report_path: str | None = None) -> int:
    """Проверить, что в приложении есть всё для всех вкладок.  0 - да, 1 - нет.

    Проверяется то, что ломается именно при сборке: модуль не попал в граф,
    нет файла каскада лиц OpenCV, нет папки fonts/, у нативной библиотеки не
    загрузилась зависимость.  Каждая проверка независима: одна упавшая не
    скрывает остальные.
    """
    base = _source_dir()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    lines: list[str] = ["%s %s, frozen=%s, %s" % (APP_TITLE, SELF_TEST_FLAG,
                                                  IS_FROZEN, sys.platform)]
    failed = 0

    def check(name: str, fn) -> None:
        nonlocal failed
        try:
            detail = fn()
        except BaseException as exc:                # noqa: BLE001 - это отчёт
            failed += 1
            lines.append("FAIL  %s: %s: %s" % (name, type(exc).__name__, exc))
        else:
            lines.append("ok    %s%s" % (name, ": %s" % detail if detail else ""))

    for mod_name in SELF_TEST_MODULES:
        check("import " + mod_name,
              lambda m=mod_name: str(getattr(_import_for_self_test(m),
                                             "__version__", "") or ""))

    def faces() -> str:
        import cull
        mode, note = cull.face_backend()
        if mode != cull.MODE_FACES:
            raise RuntimeError("режим «%s»: %s" % (mode, note))
        return note

    def fonts() -> str:
        import poster
        folder = poster.bundled_fonts_dir()
        missing = [f for f in poster._BUNDLED_FILES.values()     # noqa: SLF001
                   if not (folder / f).is_file()]
        if missing:
            raise FileNotFoundError("нет в %s: %s" % (folder, ", ".join(missing)))
        if IS_FROZEN and Path(getattr(sys, "_MEIPASS", "")) not in folder.parents:
            raise RuntimeError("папка шрифтов не внутри сборки: %s" % folder)
        return str(folder)

    def enhance_engine() -> str:
        import enhance
        from PIL import Image
        src = Image.new("RGB", (64, 48), (40, 30, 25))
        out = enhance.preview(src, None)
        if out.size != src.size or out.getpixel((10, 10)) == src.getpixel((10, 10)):
            raise RuntimeError("предпросмотр не изменил тёмный кадр")
        return "%dx%d" % out.size

    def poster_engine() -> str:
        import poster
        tid = poster.list_templates()[0][0]
        img = poster.render(tid, poster.example_fields(tid), None, size=(216, 270))
        report = poster.render_report(img)
        used = ", ".join(f.label for f in (report.fonts.values() if report else ()))
        return "%s %dx%d, %s" % (tid, img.size[0], img.size[1], used)

    def tk_runtime() -> str:
        import tkinter
        return "Tcl/Tk %s" % tkinter.TkVersion

    check("поиск лиц (OpenCV)", faces)
    check("шрифты fonts/", fonts)
    check("движок обработки", enhance_engine)
    check("движок афиш", poster_engine)
    check("tkinter", tk_runtime)
    lines.append("итог: %s" % ("всё на месте" if not failed
                               else "провалено проверок: %d" % failed))

    text = "\n".join(lines) + "\n"
    if report_path:
        try:
            Path(report_path).write_text(text, encoding="utf-8")
        except OSError as exc:
            failed += 1
            text += "не удалось записать отчёт %s: %s\n" % (report_path, exc)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------


def main() -> int:
    gui = load_gui()
    _retarget_app_paths(gui)

    # Перехватчики — ДО main(): дальше они ставятся ещё раз, уже вместе с
    # обработчиком tk-колбэков, но окно между этими точками успевает создаться.
    try:
        gui.install_crash_hooks(None)
    except Exception:
        pass

    try:
        return int(gui.main() or 0)
    except SystemExit:
        raise
    except BaseException:
        # Тот же хвост, что в блоке __main__ самого cr2_gui.pyw.
        tb = traceback.format_exc()
        shown = False
        try:
            path = gui.record_error("app.py main", tb)
            # bool(): старая версия _show_error_box возвращала None, и тогда
            # мы обязаны эскалировать, а не счесть окно показанным.
            shown = bool(gui._show_error_box(
                "Приложение не смогло запуститься.", path))
        except Exception:
            shown = False
        if not shown:
            # Сюда попадаем, когда непригоден САМ tkinter (нет tk.tcl после
            # карантина антивируса, нет дисплея).  _fatal не возвращается:
            # пишет журнал, показывает окно через ctypes/osascript, os._exit.
            _fatal("Приложение не смогло запуститься.", tb)
        return 1


if __name__ == "__main__":
    # freeze_support() - ПЕРВЫМ делом, до load_gui() и до любого тяжёлого
    # импорта (tkinter, Pillow, numpy, cv2 во вкладках).  Причина: вкладки
    # вправе раздавать работу ProcessPoolExecutor/multiprocessing, а в
    # собранном приложении дочерний процесс - это тот же самый .exe, запущенный
    # с особыми аргументами.  Только freeze_support() узнаёт такой запуск и
    # превращает процесс в рабочий; без неё дочерний процесс проходит мимо и
    # открывает ЕЩЁ ОДНО окно программы, а оно - своих детей: на Windows .exe
    # перезапускает себя бесконечно.  PyInstaller подменяет эту функцию так,
    # что она работает и в .app на macOS (там дети тоже запускаются через
    # spawn).  В обычном запуске из исходников вызов ничего не делает.
    import multiprocessing
    multiprocessing.freeze_support()
    if len(sys.argv) >= 2 and sys.argv[1] == SELF_TEST_FLAG:
        sys.exit(self_test(sys.argv[2] if len(sys.argv) >= 3 else None))
    sys.exit(main())
