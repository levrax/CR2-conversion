# -*- coding: utf-8 -*-
"""cr2_gui - графическая оболочка (tkinter, только стандартная библиотека)
для конвертера cr2_core: Canon CR2 -> JPEG.

Запускается двойным щелчком (.pyw => pythonw.exe, без окна консоли).

--------------------------------------------------------------------------
КОНТРАКТ ПОТОКОВ (прочитайте это перед любой правкой)
--------------------------------------------------------------------------
* Вся конвертация выполняется ВНЕ главного потока.  cr2_core.convert_many()
  вызывается РОВНО ОДИН РАЗ в ОДНОМ рабочем потоке (threading.Thread,
  daemon=True).  Внутри он сам распараллеливается через ThreadPoolExecutor и
  закрывает пул контекстным менеджером, поэтому процесс всегда завершается.
* Колбэки on_result / on_progress вызываются из этого же рабочего потока и
  делают ровно одно: кладут неизменяемый объект-сообщение в queue.Queue.
* Главный (Tk) поток разбирает очередь в самоперезапускающемся цикле
  root.after(POLL_MS, self._poll) и только он трогает виджеты.
* НИ ОДИН рабочий поток НИКОГДА не обращается к виджету, к tk-переменной,
  к root.after / after_idle / update.  Причина: _tkinter принимает
  межпоточный вызов только пока главный поток находится внутри mainloop и
  раздаёт события; тот же самый вызов до старта mainloop и после его выхода
  падает с RuntimeError('main thread is not in main loop').  То есть
  after_idle из рабочего потока - это гонка, которая проявится ровно в тот
  момент, когда пользователь закроет окно во время конвертации.
* Отмена - это threading.Event, который cr2_core проверяет между файлами.
  Кнопка «Отмена» только ставит событие и сразу возвращает управление, поэтому
  интерфейс остаётся живым.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Пути рядом со скриптом
# --------------------------------------------------------------------------

try:
    APP_DIR = Path(__file__).resolve().parent
except Exception:                                   # pragma: no cover
    APP_DIR = Path(os.getcwd())

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


# --------------------------------------------------------------------------
# Платформа: ЕДИНСТВЕННОЕ место в программе, которое знает про разницу систем
# --------------------------------------------------------------------------
#
# Правило раздела: каждая функция обязана отработать на любой из трёх систем и
# НИКОГДА не бросать исключение наружу.  Где возможности нет - функция просто
# ничего не делает и сообщает об этом возвращаемым значением.  Поэтому ниже по
# файлу нет ни одного обращения к sys.platform, ctypes.windll, os.startfile и
# ни одного зашитого имени шрифта: весь разбор системы собран здесь.
#
# Поведение на Windows при этом не меняется ни в одной точке: ветка IS_WINDOWS
# в каждой функции повторяет прежний код дословно.
# --------------------------------------------------------------------------

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
# Всё остальное - X11/Wayland: Linux, *BSD.  Ведут себя одинаково.
IS_LINUX = not IS_WINDOWS and not IS_MACOS

APP_ID = "CR2Converter"               # имя папки настроек вне папки программы
SETTINGS_NAME = "cr2_gui_settings.json"
ERROR_LOG_NAME = "cr2_gui_error.log"

# ERROR_LOG_PATH определяется ниже, рядом с SETTINGS_PATH: ему нужны те же
# проверки (.app, доступность на запись), а они опираются на функции, которых
# в этой точке файла ещё нет.


# ---------------- системное окно с ошибкой (без tkinter) ----------------


def _applescript_string(text: str) -> str:
    """Строковый литерал AppleScript.  Без этого кавычка в пути ломает скрипт."""
    body = (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))
    return '"%s"' % body


def native_error_dialog(title: str, text: str) -> bool:
    """Показать системное окно с ошибкой БЕЗ участия tkinter.

    Нужно ровно в одном случае: tkinter не загрузился, значит messagebox
    недоступен, а окна ещё нет.  Возвращает True, если окно удалось показать.
    Никогда не бросает исключение: это последний рубеж перед os._exit().

    Windows: user32.MessageBoxW (как было).
    macOS:   osascript display dialog - есть в любой системе, ставить нечего.
    Linux:   zenity, затем kdialog, затем xmessage; если нет ничего - False,
             и вызывающий код остаётся с записью в журнале, что уже не молчание.
    """
    # На сборочной машине окно показывать НЕКОМУ, а ждать оно будет вечно:
    # MessageBoxW таймаута не имеет вовсе, osascript - 600 с.  Один такой вызов
    # вешает шаг CI на десять минут и валит его без единой строки объяснения.
    if os.environ.get("CI"):
        return False

    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return True
        except Exception:
            return False

    try:
        import subprocess
    except Exception:
        return False

    if IS_MACOS:
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
            # Нет такой программы (FileNotFoundError), таймаут - молча к
            # следующей.
            continue
        # Ненулевой код возврата - окно не показано (чаще всего нет дисплея),
        # поэтому пробуем следующую программу, а не рапортуем об успехе.
        if done.returncode == 0:
            return True
    return False


# ---------------- DPI / масштаб ----------------


def enable_dpi_awareness() -> str:
    """Лучший доступный режим DPI.  ОБЯЗАТЕЛЬНО вызвать ДО tk.Tk().

    Windows: без этого окно рисует сама система, растягивая картинку 96 DPI, -
        текст получается мыльным.  Три способа по убыванию качества, потому что
        первые два появились только в Windows 10 1703 и 8.1 соответственно.
    macOS:   Retina обслуживает система, окно всегда живёт в логических точках;
        делать нечего, и попытка что-то настроить была бы вредна.
    Linux:   масштабом управляет сессия (GDK_SCALE, Xft.dpi, tk scaling);
        навязывать своё - значит ломать настройку пользователя.
    """
    if not IS_WINDOWS:
        return "n/a"
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "permonitor_v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return "system"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "legacy"
    except Exception:
        return "none"


# ---------------- где лежит файл настроек ----------------


def _dir_is_writable(path: Path) -> bool:
    """Проверка БЕЗ побочных эффектов: ничего не создаём и не открываем.

    Пробная запись файла здесь недопустима - ровно из-за такой «проверки»
    раньше при каждом запуске появлялся пустой журнал ошибок.
    """
    try:
        return path.is_dir() and os.access(str(path), os.W_OK)
    except Exception:
        return False


def _inside_macos_app_bundle(path: Path) -> bool:
    """Программа запущена изнутри .app?  Тогда писать внутрь нельзя.

    Содержимое бандла подписано; запись в него ломает подпись, а в /Applications
    её вдобавок обычно и не разрешат.
    """
    if not IS_MACOS:
        return False
    try:
        return any(part.endswith(".app") for part in path.parts)
    except Exception:
        return False


def user_config_dir() -> Path:
    """Стандартная папка настроек текущей системы.  Каталог НЕ создаётся."""
    try:
        home = Path.home()
    except Exception:                                   # pragma: no cover
        home = Path(os.path.expanduser("~"))
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or ""
        return (Path(base) if base else home / "AppData" / "Roaming") / APP_ID
    if IS_MACOS:
        return home / "Library" / "Application Support" / APP_ID
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    return (Path(base) if base else home / ".config") / APP_ID.lower()


def _prefer_app_dir() -> bool:
    """Можно ли класть свои файлы рядом с программой.

    Windows: всегда - ровно как было.  Программа переносимая: папку можно
        скопировать на флешку вместе с настройками и журналом.
    macOS/Linux: тоже рядом с программой, пока туда можно писать -
        переносимость важнее единообразия.  А вот если программа лежит внутри
        .app или в системном каталоге (/Applications, /usr/local/bin), запись
        туда либо запрещена, либо ломает подпись бандла.
    """
    if IS_WINDOWS:
        return True
    return not _inside_macos_app_bundle(APP_DIR) and _dir_is_writable(APP_DIR)


def settings_path() -> Path:
    """Куда класть cr2_gui_settings.json."""
    return (APP_DIR if _prefer_app_dir() else user_config_dir()) / SETTINGS_NAME


def error_log_path() -> Path:
    """Куда класть cr2_gui_error.log.

    Те же проверки, что и у settings_path().  Раньше журнал безусловно
    привязывался к папке программы, и на macOS/Linux из каталога только для
    чтения выходило так: окно писало в строке состояния «журнал будет здесь»,
    record_error по факту неудачи сваливался во временную папку, а пользователь
    искал файл там, где ему сказали, и не находил.
    """
    return (APP_DIR if _prefer_app_dir() else user_config_dir()) / ERROR_LOG_NAME


def legacy_text_encodings() -> tuple:
    """Кодировки-кандидаты для файла настроек, который оказался не UTF-8.

    Первой всегда идёт кодировка системы, затем историческая однобайтовая
    кириллица.  На Windows это прежняя пара (кодировка системы, cp1251) -
    поведение не изменилось.  На macOS добавлена mac_cyrillic (кириллица
    классического Mac OS), на Linux список тот же cp1251: не-UTF-8 файл
    настроек там может появиться единственным способом - его принесли с Windows.
    """
    found = []
    try:
        import locale
        pref = locale.getpreferredencoding(False)
        if pref:
            found.append(pref)
    except Exception:
        pass
    if IS_MACOS:
        found.append("mac_cyrillic")
    found.append("cp1251")
    seen = set()
    result = []
    for enc in found:
        key = enc.lower().replace("-", "_")
        if key not in seen:
            seen.add(key)
            result.append(enc)
    return tuple(result)


# ---------------- «показать папку в проводнике» ----------------


def reveal_in_file_manager(target) -> None:
    """Открыть папку в файловом менеджере системы.

    Бросает OSError, если открыть не удалось - вызывающий код показывает это
    пользователю.  Другие исключения наружу не выходят.
    """
    path = os.path.normpath(str(target))
    if IS_WINDOWS:
        starter = getattr(os, "startfile", None)
        if starter is None:                             # pragma: no cover
            raise OSError("os.startfile недоступен")
        starter(path)          # корректно работает с кириллицей
        return

    try:
        import subprocess
    except Exception as exc:                            # pragma: no cover
        raise OSError("subprocess недоступен: %s" % exc)

    if IS_MACOS:
        commands = [["open", path]]
    else:
        # xdg-open - стандарт freedesktop; gio есть везде, где есть GLib;
        # дальше конкретные менеджеры на случай голого окружения.
        commands = [["xdg-open", path], ["gio", "open", path],
                    ["nautilus", path], ["dolphin", path],
                    ["thunar", path], ["pcmanfm", path], ["nemo", path]]

    last = None
    for cmd in commands:
        try:
            # Popen, а не run: файловый менеджер живёт своей жизнью, ждать его
            # нельзя - иначе интерфейс замрёт до закрытия окна проводника.
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            return
        except OSError as exc:      # нет такой программы - пробуем следующую
            last = exc
        except Exception as exc:                        # pragma: no cover
            last = exc
    raise OSError("не нашлось программы для показа папки (%s)" % (last,))


# ---------------- шрифты ----------------


def _available_font_families(widget=None) -> frozenset:
    """Список установленных семейств шрифтов.  Пустой, если Tk ещё не готов."""
    try:
        from tkinter import font as tkfont
        names = tkfont.families(widget) if widget is not None else tkfont.families()
        return frozenset(names)
    except Exception:
        return frozenset()


def _first_installed(widget, candidates) -> str:
    families = _available_font_families(widget)
    if not families:
        return ""
    lowered = {name.lower(): name for name in families}
    for want in candidates:
        got = lowered.get(want.lower())
        if got:
            return got
    return ""


def monospace_font(widget=None):
    """Моноширинный шрифт для журнала.

    Consolas есть только на Windows; Menlo - только на macOS; DejaVu Sans Mono -
    типовой в дистрибутивах Linux.  Если ни одного из списка нет, возвращаем
    именованный шрифт Tk «TkFixedFont»: он существует всегда и по определению
    моноширинный.
    """
    if IS_WINDOWS:
        return ("Consolas", 9)
    if IS_MACOS:
        candidates = ("SF Mono", "Menlo", "Monaco", "Courier New")
        size = 11
    else:
        candidates = ("DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
                      "Ubuntu Mono", "FreeMono", "Courier New")
        size = 9
    family = _first_installed(widget, candidates)
    return (family, size) if family else "TkFixedFont"


def apply_default_ui_font(root=None) -> str:
    """Привести шрифт интерфейса в порядок там, где система этого не делает.

    Windows и macOS: НИЧЕГО не трогаем.  Tk и так берёт Segoe UI и системный
    шрифт macOS соответственно, и вмешательство только испортило бы вид.
    X11: у Tk остаётся запасной вариант вроде растрового «Helvetica», если в
    системе нет ни одного из привычных семейств - в этом (и только в этом)
    случае подставляем нормальный масштабируемый шрифт.

    Возвращает итоговое семейство или "" - для журнала, не для логики.
    """
    if IS_WINDOWS or IS_MACOS:
        return ""
    try:
        from tkinter import font as tkfont
        base = tkfont.nametofont("TkDefaultFont", root)
        current = str(base.actual("family") or "")
        if current.lower() not in ("helvetica", "fixed", "clean", "courier", ""):
            return current
        family = _first_installed(root, ("DejaVu Sans", "Liberation Sans",
                                         "Noto Sans", "Cantarell", "Arial"))
        if not family:
            return current
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont"):
            try:
                tkfont.nametofont(name, root).configure(family=family)
            except Exception:
                continue
        return family
    except Exception:
        return ""


# ---------------- тема ttk ----------------


def apply_ui_theme(root=None) -> str:
    """Выбрать тему ttk, родную для системы.  Возвращает имя выбранной темы.

    Windows: vista - как было.
    macOS:   aqua.  Раньше сюда доходила ветка «нет vista -> clam», то есть на
             Mac программа сама отключала родную тему и выглядела чужой.
    Linux:   clam: единственная из встроенных, у которой прилично выглядят
             Treeview и Combobox; default/alt - запасные.
    """
    try:
        style = ttk.Style(root)
        names = style.theme_names()
    except Exception:
        return ""
    if IS_WINDOWS:
        preferred = ("vista", "clam")
    elif IS_MACOS:
        preferred = ("aqua", "clam")
    else:
        preferred = ("clam", "alt", "default")
    for name in preferred:
        if name in names:
            try:
                style.theme_use(name)
                break
            except Exception:
                continue
    try:
        return str(style.theme_use())
    except Exception:
        return ""


def theme_honours_widget_colors(widget=None) -> bool:
    """Слушается ли тема параметров -foreground/-background у ttk-виджетов.

    На macOS тема aqua рисует виджеты силами системы и цвета, заданные
    программой, игнорирует.  Это НЕ ошибка и не повод что-то чинить: подписи
    в этой программе раскрашены лишь для подсказки, а сам смысл всегда написан
    словами.  Функция нужна, чтобы не передавать в такой теме заведомо
    бесполезные параметры.

    Отдельно и специально: РАСКРАСКА СТРОК ТАБЛИЦЫ через tag_configure к этому
    отношения не имеет - теги Treeview работают и на aqua, поэтому цвета
    ok/warn/error задаются именно тегами и снимать их нельзя нигде.
    """
    try:
        return str(ttk.Style(widget).theme_use()) != "aqua"
    except Exception:
        return True


SETTINGS_PATH = settings_path()
# Оба имени app.py подменяет по имени в _retarget_app_paths() для собранного
# приложения, поэтому они обязаны оставаться модульными переменными.
ERROR_LOG_PATH = error_log_path()

POLL_MS = 60           # период опроса очереди (50-100 мс)
MAX_DRAIN = 200        # не более стольких сообщений за один тик
MAX_LOG_LINES = 3000
CLOSE_TIMEOUT = 6.0    # сколько ждать рабочий поток при закрытии окна


# --------------------------------------------------------------------------
# Аварийный журнал: без консоли трассировка иначе не видна вообще
# --------------------------------------------------------------------------


def _error_log_path() -> Path:
    """Предпочтительный путь журнала ошибок — рядом со скриптом.

    ВАЖНО: путь только вычисляется, файл НЕ создаётся.  Раньше здесь стояла
    проверка доступности через open(..., "a"), и пустой cr2_gui_error.log
    появлялся при КАЖДОМ запуске — пользователь видел в папке «журнал ошибок»
    и думал, что программа упала, хотя ничего не случилось.  Запасной путь во
    временную папку выбирается теперь в record_error(), по факту неудачи.
    """
    return ERROR_LOG_PATH


def _fallback_log_path() -> Path:
    """Куда писать, если рядом со скриптом нельзя (только чтение, флешка)."""
    import tempfile
    return Path(tempfile.gettempdir()) / ERROR_LOG_NAME


def record_error(header: str, text: str) -> Path:
    """Дописать блок в cr2_gui_error.log. Никогда не бросает исключение.

    Пробует сначала папку скрипта, затем временную.  Возвращает путь, куда
    реально записалось (или запасной, если не удалось никуда).
    """
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = "\n=== %s | %s ===\n%s\n" % (stamp, header, text)
    for path in (_error_log_path(), _fallback_log_path()):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(block)
            return path
        except Exception:
            continue
    return _fallback_log_path()


# Под pythonw.exe sys.stdout/stderr равны None ещё до первой строки кода:
# любой print() во время загрузки модуля убил бы процесс молча.  Ставим заглушку
# ЗДЕСЬ, а не в install_crash_hooks(), которая вызывается только из main().
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
except Exception:
    pass


def _fatal_bootstrap(header: str, exc: BaseException) -> None:
    """Сообщить об ошибке ЗАГРУЗКИ модуля и завершить процесс.

    До этой точки нет ни sys.excepthook, ни окна, ни даже tkinter — поэтому
    messagebox использовать нельзя, только системное окно (native_error_dialog).
    Раньше такая ошибка (нет tcl/tk, скопирован один файл из двух) убивала
    pythonw.exe вообще без следов: ни окна, ни строки в журнале, а .bat
    рапортовал успех.
    """
    text = "%s: %s: %s" % (header, type(exc).__name__, exc)
    path = record_error("bootstrap", text + "\n" + traceback.format_exc())
    if __name__ != "__main__":
        # Модуль ИМПОРТИРОВАН, а не запущен: прогон тестов, проба при упаковке,
        # app.py в собранном приложении.  os._exit(1) убил бы чужой процесс
        # мимо unittest, atexit и всех except — весь набор тестов обрывался на
        # полуслове без трассировки, без строки «FAILED» и без единого skip, а
        # на Windows перед этим ещё и вешал сборку на модальном окне без
        # таймаута.  Отдаём ошибку вызывающему коду: у app.py есть свой
        # _fatal(), а тест превращает её в skip.
        raise exc
    native_error_dialog(
        "Конвертер CR2",
        "%s\n\nПодробности записаны в файл:\n%s\n\n"
        "Проверьте, что рядом лежат cr2_gui.pyw и cr2_core.py, "
        "и что Python установлен вместе с компонентом tcl/tk." % (text, path))
    os._exit(1)


try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except BaseException as _exc:          # нет tcl/tk в установке Python
    _fatal_bootstrap("не удалось загрузить tkinter", _exc)


_error_box_shown = [0]


def _show_error_box(summary: str, path: Path) -> None:
    """Показать messagebox, но не более трёх раз за сеанс (защита от лавины)."""
    if _error_box_shown[0] >= 3:
        return
    _error_box_shown[0] += 1
    try:
        parent = tk._default_root                    # noqa: SLF001
        created = None
        if parent is None:
            created = tk.Tk()
            created.withdraw()
            parent = created
        messagebox.showerror(
            "Непредвиденная ошибка",
            "%s\n\nПодробности записаны в файл:\n%s" % (summary, path),
            parent=parent,
        )
        if created is not None:
            created.destroy()
    except Exception:
        pass


def install_crash_hooks(root: tk.Misc | None = None) -> None:
    """Три независимых перехватчика: обычный, tk-колбэки, рабочие потоки."""

    def sys_hook(exc_type, exc, tb):
        p = record_error("sys.excepthook",
                         "".join(traceback.format_exception(exc_type, exc, tb)))
        _show_error_box("%s: %s" % (exc_type.__name__, exc), p)

    sys.excepthook = sys_hook

    def thread_hook(args):
        record_error(
            "поток %s" % getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)),
        )

    threading.excepthook = thread_hook

    if root is not None:
        def tk_hook(exc_type, exc, tb):
            # Исключения из tk-колбэков НЕ доходят до sys.excepthook.
            p = record_error("tk callback",
                             "".join(traceback.format_exception(exc_type, exc, tb)))
            _show_error_box("%s: %s" % (exc_type.__name__, exc), p)

        root.report_callback_exception = tk_hook      # type: ignore[assignment]


# --------------------------------------------------------------------------
# DPI: enable_dpi_awareness() живёт в разделе «Платформа» выше и вызывается
# из main() ДО создания Tk(), иначе окно будет растянутым и мыльным.
# --------------------------------------------------------------------------


def ui_scale(widget: tk.Misc) -> float:
    """1.0 при 96 DPI, 2.0 при 192 DPI. Только для ПИКСЕЛЬНЫХ величин.

    macOS: Tk/aqua ВСЕГДА сообщает 72 dpi — окно живёт в логических точках, а
    retina обслуживает система, и множитель заднего буфера от Tk скрыт (ровно
    поэтому enable_dpi_awareness() возвращает там "n/a").  Делить 72 на 96
    нельзя: получалось вечное 0.75, то есть все пиксельные размеры ужимались на
    четверть — и одновременно системный шрифт там 13 pt против 9 pt Segoe UI,
    под которые константы подбирались.  Строки таблицы (rowheight 22 -> 16)
    обрезали текст, окно открывалось 765x570 вместо 1020x760, а minsize
    позволял сжать его до 570x420, где кнопки и колонки уже не помещаются.
    """
    if IS_MACOS:
        return 1.0
    try:
        return float(widget.winfo_fpixels("1i")) / 96.0
    except Exception:
        return 1.0


# --------------------------------------------------------------------------
# Настройки (маленький JSON рядом со скриптом; порча файла не фатальна)
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "src": "",
    "recursive": True,
    "out_mode": "beside",          # 'beside' | 'folder'
    "out_dir": "",
    "lossless": True,
    "quality": 95,
    "max_side": 0,
    "bake_rotation": False,
    "overwrite": False,
    "suffix": "",
    "strip_gps": False,
    "prefer_dpp_preview": False,
    "show_log": False,
    "last_file_dir": "",
}


def _read_settings_text() -> str:
    """Сначала СТРОГИЙ UTF-8, и только потом кодировка системы.

    errors="replace" здесь недопустим: он никогда не бросает исключение, то есть
    запасная ветка стала бы мёртвым кодом, а путь с кириллицей превратился бы в
    строку из U+FFFD, которую программа при выходе записала бы поверх настроек.
    """
    blob = SETTINGS_PATH.read_bytes()
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        for enc in legacy_text_encodings():
            try:
                return blob.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
        raise


def load_settings() -> dict:
    """Прочитать настройки. Битый файл не теряем, а отводим в сторону."""
    data = dict(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return data
    try:
        raw = json.loads(_read_settings_text())
    except Exception:
        try:
            bad = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".bad")
            os.replace(SETTINGS_PATH, bad)
            record_error("load_settings",
                         "настройки не прочитаны, файл сохранён как %s\n%s"
                         % (bad, traceback.format_exc()))
        except Exception:
            pass
        return data
    if not isinstance(raw, dict):
        return data
    for key, default in DEFAULT_SETTINGS.items():
        val = raw.get(key, default)
        if val is None:
            continue                      # явный null == «взять умолчание»
        if isinstance(default, bool):
            data[key] = bool(val)
        elif isinstance(default, int):
            try:
                # OverflowError тоже: json умеет 1e999 -> inf, и без этого
                # цикл обрывался, молча сбрасывая ВСЕ последующие ключи.
                val = int(val)
            except (TypeError, ValueError, OverflowError):
                continue
            if key == "quality":
                val = max(60, min(100, val))     # тот же диапазон, что в _build_vars
            elif key == "max_side":
                val = max(0, min(30000, val))
            data[key] = val
        elif isinstance(val, (str, int, float, bool)):
            data[key] = str(val)          # dict/list в строку не превращаем
    return data


def save_settings(data: dict) -> None:
    """Запись через временный файл: обрыв не оставляет обрезанный JSON."""
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".%d.tmp" % os.getpid())
    try:
        # Папка программы уже существует (мы из неё запустились), а вот
        # ~/Library/Application Support/CR2Converter или ~/.config/cr2converter
        # может ещё не существовать - создаём, но только при первой ЗАПИСИ,
        # чтобы простой запуск программы не оставлял следов в системе.
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Сообщения рабочий поток -> интерфейс (простые неизменяемые данные)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MLog:
    text: str
    tag: str = ""


@dataclass(frozen=True)
class MTotal:
    total: int


@dataclass(frozen=True)
class MProgress:
    done: int
    total: int


@dataclass(frozen=True)
class MRow:
    name: str
    preview: str
    recipe: str
    share: str          # доля площади кадра, "?" если неизвестна
    status: str
    tag: str            # 'ok' | 'warn' | 'error' | 'skipped'
    detail: str
    # Числовые ключи для сортировки таблицы.  Строки «2592 x 1728» и «55%»
    # сортируются как текст неправильно (100% встанет перед 55%), а разбирать
    # их обратно из ячеек - значит держать разбор в двух местах и рассинхронить
    # его при первой же смене формата.  Поэтому числа едут вместе со строкой.
    preview_px: int = 0
    share_val: float = -1.0     # -1.0 == долю определить не удалось
    has_recipe: bool = False


@dataclass(frozen=True)
class MDone:
    ok: int
    failed: int
    skipped: int
    cancelled: bool
    out_dir: str
    probe_only: bool
    dpp: int = 0        # файлов с рецептом DPP (применить его мы не можем)
    small: int = 0      # файлов с заведомо неполноразмерным превью
    full: int = 0       # файлов, у которых превью равно полному кадру
    seen: int = 0       # файлов реально просмотрено (ok + failed + skipped)
    crashed: str = ""   # непустая строка == задание НЕ доведено до конца
    # Куда record_error записал трассировку НА САМОМ ДЕЛЕ.  Предпочтительный
    # путь может оказаться недоступен (только чтение, флешка), и тогда запись
    # уходит во временную папку; показывать пользователю надо файл, который
    # существует, а не тот, который мы хотели.
    log_path: str = ""


@dataclass(frozen=True)
class MCaps:
    pillow: bool
    rawpy: bool


# --------------------------------------------------------------------------
# Рабочая логика. НИ ОДНОГО обращения к tkinter в этом разделе.
# --------------------------------------------------------------------------

try:
    import cr2_core  # noqa: E402  (после sys.path.insert - модуль лежит рядом)
except BaseException as _exc:          # скопировали не всю папку
    _fatal_bootstrap("не удалось загрузить cr2_core", _exc)

PIP_PILLOW = "python -m pip install Pillow"
PIP_RAWPY = "python -m pip install rawpy Pillow"

# Единственная честная формулировка про рецепт DPP.  Рецепт — это список правок
# в проприетарном формате Canon; отрисовать его умеет только сам движок Canon,
# поэтому никакая программа на чистом Python (включая эту) применить рецепт не
# может.  Мы лишь сообщаем, что он в файле есть.
DPP_RECIPE_TEXT = (
    "В файле найден рецепт Canon DPP. Эта программа применить его НЕ может: "
    "она извлекает снимок таким, каким его отрисовала камера. Чтобы получить "
    "изображение с правками DPP, откройте файл в Canon Digital Photo "
    "Professional и выполните «Конвертировать и сохранить» "
    "(или «Пакетная обработка»)."
)


def _fmt_size(info) -> str:
    if info is None:
        return "—"
    best = info.best
    if best is None:
        return "нет превью"
    return "%d x %d" % (best.width, best.height)


def _share_of_frame(info) -> float:
    """Доля площади кадра, занятая лучшим превью (0.0, если неизвестна).

    Ядро считает то же самое, но ТОЛЬКО на пути конвертации (min_preview_ratio),
    поэтому в режиме «Проверить» половинные превью выглядели как обычные
    зелёные строки. Считаем здесь, чтобы обе таблицы предупреждали одинаково.
    """
    if info is None or getattr(info, "raw_subsampled", False):
        return 0.0
    best = info.best
    raw_px = (info.raw_width or 0) * (info.raw_height or 0)
    if best is None or not raw_px or not best.pixels:
        return 0.0
    return best.pixels / raw_px


def _fmt_share(info) -> str:
    share = _share_of_frame(info)
    return "%d%%" % round(share * 100) if share else "?"


def _preview_px(info, res=None) -> int:
    """Площадь лучшего превью в пикселях (0, если её вообще нет)."""
    best = info.best if info is not None else None
    if best is not None:
        return best.pixels
    if res is not None and res.width and res.height:
        return res.width * res.height
    return 0


# Превью считается полноразмерным, если его площадь практически равна площади
# кадра.  Округление размеров у разных камер даёт доли процента расхождения,
# поэтому сравниваем не с 1.0, а с 0.995.
FULL_FRAME_RATIO = 0.995


def _is_full_frame(info) -> bool:
    return _share_of_frame(info) >= FULL_FRAME_RATIO


def _fmt_recipe(info) -> str:
    """Столбец «Рецепт DPP».

    Рецепт может быть только НАЙДЕН или НЕ НАЙДЕН в файле.  Применить его эта
    программа не умеет ни в каком случае, поэтому «есть» пишется вместе с
    оговоркой, а отсутствие рецепта не выдаётся за «правок нет».
    """
    if info is None:
        return "—"
    return "есть, не применим" if info.has_dpp_recipe else "не найден"


def _row_from_result(res, min_ratio: float = 0.4) -> MRow:
    """Построить строку таблицы из cr2_core.Result (чистая функция)."""
    info = res.info
    if res.skipped:
        tag = "skipped"
        status = res.message or "Пропущен"
    elif not res.ok:
        tag = "error"
        status = res.message or "Ошибка"
    else:
        has_recipe = bool(info is not None and info.has_dpp_recipe)
        share = _share_of_frame(info)
        small = bool(share and share < min_ratio)
        # Раньше строка была зелёной, а всё, что ядро написало в res.message
        # (половинное превью, выброшенные теги, подобранное имя), пряталось в
        # окне «Подробности» по двойному щелчку — то есть не показывалось.
        tag = "warn" if (has_recipe or small or res.mode == "raw") else "ok"
        mode_ru = {"lossless": "копия встроенного JPEG, без перекодирования",
                   "reencode": "перекодирование",
                   "raw": "декодирование RAW"}.get(res.mode, res.mode or "готово")
        status = "Готово (%s), %d x %d, %d КБ" % (
            mode_ru, res.width, res.height, max(1, res.bytes_out // 1024))
        if small:
            status += " — превью меньше кадра (%d%%)" % round(share * 100)
        elif has_recipe:
            status += " — найден рецепт DPP, он НЕ применён"
        elif tag == "warn":
            status += " — см. подробности"
    size = _fmt_size(info)
    if info is None and res.width:
        size = "%d x %d" % (res.width, res.height)
    detail_parts = [str(res.src)]
    if res.dst is not None:
        detail_parts.append("-> %s" % res.dst)
    if res.message:
        detail_parts.append(res.message)
    if info is not None and info.has_dpp_recipe:
        detail_parts.append(DPP_RECIPE_TEXT)
    if info is not None and info.recipe_hint:
        detail_parts.append(info.recipe_hint)
    return MRow(name=res.src.name, preview=size, recipe=_fmt_recipe(info),
                share=_fmt_share(info), status=status, tag=tag,
                detail="\n".join(detail_parts),
                preview_px=_preview_px(info, res),
                share_val=_share_of_frame(info) or -1.0,
                has_recipe=bool(info is not None and info.has_dpp_recipe))


def _row_from_info(info, min_ratio: float = 0.4) -> MRow:
    """Строка таблицы для режима «Проверить» (ничего не записывается)."""
    if info.error:
        return MRow(info.path.name, _fmt_size(info), _fmt_recipe(info), "?",
                    "Ошибка: %s" % info.error, "error",
                    "%s\n%s" % (info.path, info.error),
                    preview_px=_preview_px(info),
                    has_recipe=bool(info.has_dpp_recipe))
    best = info.best
    bits = []
    if info.camera:
        bits.append(info.camera)
    if best is not None:
        bits.append("источник превью: %s" % best.source)
    if info.raw_width and info.raw_height:
        bits.append("кадр %d x %d" % (info.raw_width, info.raw_height))
    if info.orientation != 1:
        bits.append("EXIF Orientation = %d" % info.orientation)
    share = _share_of_frame(info)
    small = bool(share and share < min_ratio)
    if _is_full_frame(info):
        bits.append("превью полного размера")
    elif small:
        bits.append("превью — лишь %d%% площади кадра" % round(share * 100))
    elif share:
        bits.append("превью — %d%% площади кадра" % round(share * 100))
    if info.has_dpp_recipe:
        bits.append("есть рецепт DPP, применить его нельзя")
    status = "Готов к извлечению" + (" — " + ", ".join(bits) if bits else "")
    tag = "warn" if (info.has_dpp_recipe or small) else "ok"
    detail = [str(info.path), status]
    if info.shot_at:
        detail.append("Снято: %s" % info.shot_at)
    if info.has_dpp_recipe:
        detail.append(DPP_RECIPE_TEXT)
    if info.recipe_hint:
        detail.append(info.recipe_hint)
    return MRow(info.path.name, _fmt_size(info), _fmt_recipe(info),
                _fmt_share(info), status, tag, "\n".join(detail),
                preview_px=_preview_px(info), share_val=share or -1.0,
                has_recipe=bool(info.has_dpp_recipe))


def collect_files(spec: dict,
                  cancel: "threading.Event | None" = None,
                  problems: "list[str] | None" = None) -> list[Path]:
    """Развернуть выбор пользователя в список путей CR2 (может быть долго).

    Обход каталогов — единственная неотменяемая фаза задания: пока она шла,
    кнопка «Отмена» ничего не делала. Событие уходит в find_cr2 в ОБЕИХ ветках,
    иначе выбор «Файлы…», в котором оказалась большая папка, так и остаётся
    неубиваемым. Недоступные каталоги больше не исчезают молча: о каждом
    сообщается через `problems`.
    """
    files: list[Path] = []

    def note(path: Path, exc: OSError) -> None:
        if problems is not None:
            problems.append("каталог недоступен: %s (%s)" % (path, exc))

    recursive = spec.get("recursive", True)
    explicit = spec.get("files") or []
    if explicit:
        for item in explicit:
            if cancel is not None and cancel.is_set():
                break
            p = Path(item)
            if p.is_dir():
                files.extend(cr2_core.find_cr2(p, recursive,
                                               cancel=cancel, on_problem=note))
            elif p.suffix.lower() in cr2_core.CR2_EXTS and p.is_file():
                files.append(p)
        return files
    root = spec.get("root") or ""
    if not root:
        return files
    p = Path(root)
    if p.is_file():
        if p.suffix.lower() in cr2_core.CR2_EXTS:
            files.append(p)
        return files
    return cr2_core.find_cr2(p, recursive, cancel=cancel, on_problem=note)


def caps_worker(q: "queue.Queue") -> None:
    """Проверка необязательных зависимостей в фоне. Ничего не навязывает."""
    try:
        q.put(MCaps(pillow=cr2_core.has_pillow(), rawpy=cr2_core.has_rawpy()))
    except Exception:
        q.put(MCaps(pillow=False, rawpy=False))


def job_worker(spec: dict, opts, probe_only: bool,
               cancel: threading.Event, q: "queue.Queue") -> None:
    """Единственный рабочий поток задания. Общается только через очередь."""
    counters = {"ok": 0, "failed": 0, "skipped": 0,
                "dpp": 0, "small": 0, "full": 0}
    out_dir = ""
    # Непустая строка означает «задание НЕ доведено до конца». Сбрасывается на
    # каждом нормальном выходе, поэтому и KeyboardInterrupt/SystemExit, которые
    # не ловятся `except Exception`, тоже не выдадут себя за успешный прогон.
    crash = ""
    crash_log = ""          # реальный путь журнала, см. except ниже
    min_ratio = getattr(opts, "min_preview_ratio", 0.4)
    try:
        q.put(MLog("Поиск файлов CR2…"))
        scan_problems: list[str] = []
        try:
            files = collect_files(spec, cancel, scan_problems)
        except Exception as exc:
            record_error("collect_files", traceback.format_exc())
            q.put(MLog("Не удалось получить список файлов: %s: %s"
                       % (type(exc).__name__, exc), "err"))
            files = []
        # Отмена проверяется ДО MTotal: иначе прерванный обход рапортует
        # «Файлы CR2 не найдены» и выдаёт итог по неполному списку.
        if cancel.is_set():
            q.put(MLog("Поиск отменён пользователем.", "warn"))
            crash = ""
            return
        for text in scan_problems:
            # Именно строка таблицы, а не только запись в журнале: журнал по
            # умолчанию свёрнут, и «ошибок 0» выглядело бы правдой.
            q.put(MRow(Path(text.split(": ", 1)[-1]).name or "—", "—", "—", "?",
                       text, "error", text))
            counters["failed"] += 1
            q.put(MLog(text, "err"))
        total = len(files)
        q.put(MTotal(total))
        if total == 0:
            q.put(MLog("Файлы CR2 не найдены.", "warn"))
            crash = ""
            return
        q.put(MLog("Найдено файлов: %d" % total))
        if files:
            out_dir = str(opts.out_dir) if opts.out_dir else str(files[0].parent)

        if probe_only:
            q.put(MLog("Проверка без записи файлов."))
            for i, path in enumerate(files, 1):
                if cancel.is_set():
                    q.put(MLog("Проверка отменена пользователем.", "warn"))
                    break
                try:
                    info = cr2_core.probe(path)
                except Exception as exc:       # probe() не должен бросать, но всё же
                    record_error("probe %s" % path, traceback.format_exc())
                    # MRow - это семь полей (name, preview, recipe, share,
                    # status, tag, detail).  Здесь их было шесть: сообщение об
                    # ошибке уезжало в столбец «Доля кадра», а конструктор
                    # падал с TypeError прямо в рабочем потоке, то есть один
                    # нечитаемый файл обрывал ВСЮ проверку без MDone.
                    q.put(MRow(path.name, "—", "—", "?",
                               "Ошибка разбора: %s: %s" % (type(exc).__name__, exc),
                               "error", "%s\n%s: %s"
                               % (path, type(exc).__name__, exc)))
                    counters["failed"] += 1
                    q.put(MProgress(i, total))
                    continue
                row = _row_from_info(info, min_ratio)
                if row.tag == "error":
                    counters["failed"] += 1
                    q.put(MLog("%s: %s" % (path.name, info.error), "err"))
                else:
                    counters["ok"] += 1
                    if info.has_dpp_recipe:
                        counters["dpp"] += 1
                        q.put(MLog("%s: %s" % (path.name, DPP_RECIPE_TEXT), "warn"))
                    share = _share_of_frame(info)
                    if _is_full_frame(info):
                        counters["full"] += 1
                    elif share and share < min_ratio:
                        counters["small"] += 1
                q.put(row)
                q.put(MProgress(i, total))
            crash = ""
            return

        def on_result(res) -> None:
            # Выполняется в рабочем потоке: только put() в очередь.
            row = _row_from_result(res, min_ratio)
            if res.skipped:
                counters["skipped"] += 1
            elif res.ok:
                counters["ok"] += 1
            else:
                counters["failed"] += 1
            if res.info is not None and res.info.has_dpp_recipe:
                counters["dpp"] += 1
            share = _share_of_frame(res.info)
            if res.ok and _is_full_frame(res.info):
                counters["full"] += 1
            elif res.ok and share and share < min_ratio:
                counters["small"] += 1
            q.put(row)
            if row.tag == "error":
                q.put(MLog("ОШИБКА %s: %s" % (res.src.name, res.message), "err"))
            elif row.tag == "warn":
                q.put(MLog("%s: %s" % (res.src.name, res.message), "warn"))
            else:
                q.put(MLog("%s: %s" % (res.src.name, res.message)))

        def on_progress(done: int, total_: int) -> None:
            q.put(MProgress(done, total_))

        crash = "прервано"
        cr2_core.convert_many(files, opts, on_result=on_result,
                              on_progress=on_progress, cancel=cancel)
        crash = ""
    except Exception as exc:
        # Возвращённый путь НЕ выбрасываем: только он говорит, куда запись
        # действительно легла.
        crash_log = str(record_error("job_worker", traceback.format_exc()))
        crash = "%s: %s" % (type(exc).__name__, exc)
        q.put(MLog("Сбой обработки: %s" % crash, "err"))
    finally:
        # MDone приходит ВСЕГДА - иначе интерфейс останется заблокированным.
        q.put(MDone(ok=counters["ok"], failed=counters["failed"],
                    skipped=counters["skipped"], cancelled=cancel.is_set(),
                    out_dir=out_dir, probe_only=probe_only,
                    dpp=counters["dpp"], small=counters["small"],
                    full=counters["full"],
                    seen=counters["ok"] + counters["failed"] + counters["skipped"],
                    crashed=crash, log_path=crash_log))


# --------------------------------------------------------------------------
# Интерфейс
# --------------------------------------------------------------------------

SIZE_CHOICES = ("не уменьшать", "1024", "1600", "2048", "2560", "3200", "4096", "6000")
NO_RESIZE = SIZE_CHOICES[0]

CR2_TYPES = [("Файлы Canon RAW", "*.cr2"), ("Все файлы", "*.*")]


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=(10, 8))
        self.master_root = master
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        self.scale = ui_scale(master)
        # Слушается ли тема цветов у ttk-виджетов (на macOS/aqua - нет).
        # Считаем один раз: тему выбирает main() до сборки окна.
        self._colors_ok = theme_honours_widget_colors(master)
        self.q: "queue.Queue" = queue.Queue()
        self.cancel_evt = threading.Event()
        self.thread: threading.Thread | None = None
        self.running = False
        self.probe_only = False

        self.settings = load_settings()
        self.caps_pillow: bool | None = None
        self.caps_rawpy: bool | None = None
        self._pillow_warned = False
        self._syncing = False
        self._log_buf: list[tuple[str, str]] = []
        self._details: dict[str, str] = {}
        self._row_seq = 0
        self._tree_tail: str | None = None
        # Состояние таблицы. _all_rows - порядок вставки, _order - то, что
        # реально видно (после фильтра и сортировки), _row_keys - числовые
        # ключи строк.  Всё это ведётся инкрементно: новая строка вставляется
        # на своё место за O(log N), а не пересортировкой всей таблицы.
        self._all_rows: list[str] = []
        self._order: list[str] = []
        self._row_keys: dict[str, tuple] = {}
        self._row_problem: set[str] = set()
        self._hidden_count = 0
        self._sort_col: str | None = None
        self._sort_desc = False
        self._sort_idx = 0
        self.total = 0
        self.done = 0
        self.errors = 0
        self.last_out_dir: str = self.settings.get("out_dir") or ""
        self._close_deadline = 0.0
        self._closing = False
        self._poll_id: str | None = None
        self._destroyed = False

        self._build_vars()
        self._build_ui()
        self._sync_option_states()

        try:
            threading.Thread(target=caps_worker, args=(self.q,),
                             name="caps", daemon=True).start()
        except Exception:
            # Иначе __init__ прервётся ДО self.after(...) ниже и окно вообще
            # не оживёт: цикл опроса очереди не будет запланирован.
            record_error("caps", traceback.format_exc())
            self.caps_pillow = self.caps_rawpy = False
        self._poll_id = self.after(POLL_MS, self._poll)

    # ---------------- переменные ----------------

    def _build_vars(self) -> None:
        s = self.settings
        self.src_var = tk.StringVar(value=s.get("src", ""))
        self.recursive_var = tk.BooleanVar(value=bool(s.get("recursive", True)))
        self.out_mode_var = tk.StringVar(value=s.get("out_mode", "beside"))
        self.out_dir_var = tk.StringVar(value=s.get("out_dir", ""))
        self.lossless_var = tk.BooleanVar(value=bool(s.get("lossless", True)))
        self.quality_var = tk.IntVar(value=max(60, min(100, int(s.get("quality", 95)))))
        self.quality_label_var = tk.StringVar(value=str(self.quality_var.get()))
        max_side = int(s.get("max_side", 0) or 0)
        self.size_var = tk.StringVar(value=(NO_RESIZE if max_side <= 0 else str(max_side)))
        self.rotate_var = tk.BooleanVar(value=bool(s.get("bake_rotation", False)))
        self.overwrite_var = tk.BooleanVar(value=bool(s.get("overwrite", False)))
        self.suffix_var = tk.StringVar(value=s.get("suffix", ""))
        self.strip_gps_var = tk.BooleanVar(value=bool(s.get("strip_gps", False)))
        self.prefer_dpp_var = tk.BooleanVar(value=bool(s.get("prefer_dpp_preview", False)))
        self.status_var = tk.StringVar(value="Готово 0 из 0, ошибок 0")
        self.hint_var = tk.StringVar(value="")
        self.summary_var = tk.StringVar(value="")
        self.filter_var = tk.BooleanVar(value=False)
        self.filter_info_var = tk.StringVar(value="")
        self.log_visible = bool(s.get("show_log", False))
        self.log_btn_var = tk.StringVar(value="Журнал ▼")
        self._explicit_files: list[str] = []

        self.src_var.trace_add("write", lambda *_a: self._on_src_typed())
        self.lossless_var.trace_add("write", lambda *_a: self._on_lossless_toggle())
        self.size_var.trace_add("write", lambda *_a: self._on_size_change())
        self.rotate_var.trace_add("write", lambda *_a: self._on_rotate_toggle())

    # ---------------- построение виджетов ----------------

    def _px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _fg(self, color: str) -> dict:
        """Цвет текста для ttk-подписи: {} там, где тема его игнорирует.

        Цвет здесь - только подсказка: смысл каждой такой подписи написан
        словами, поэтому на macOS (тема aqua рисует виджеты сама и цвета не
        принимает) ничего не теряется, а лишний игнорируемый параметр не
        передаётся.  К раскраске строк таблицы это не относится: там цвета
        живут в тегах Treeview и работают на всех системах.
        """
        return {"foreground": color} if self._colors_ok else {}

    def _build_ui(self) -> None:
        s = self.scale
        self.columnconfigure(0, weight=1)
        row = 0

        # --- 1. Источник ---------------------------------------------------
        src = ttk.LabelFrame(self, text="Источник", padding=(8, 6))
        src.grid(row=row, column=0, sticky="ew")
        src.columnconfigure(0, weight=1)
        self.src_entry = ttk.Entry(src, textvariable=self.src_var)
        self.src_entry.grid(row=0, column=0, sticky="ew", padx=(0, self._px(6)))
        self.btn_browse_src = ttk.Button(src, text="Обзор…", width=10,
                                         command=self.choose_dir)
        self.btn_browse_src.grid(row=0, column=1, padx=(0, self._px(4)))
        self.btn_files = ttk.Button(src, text="Файлы…", width=10,
                                    command=self.choose_files)
        self.btn_files.grid(row=0, column=2)
        self.chk_recursive = ttk.Checkbutton(src, text="Вложенные папки",
                                             variable=self.recursive_var)
        self.chk_recursive.grid(row=1, column=0, columnspan=3, sticky="w",
                                pady=(self._px(4), 0))
        row += 1

        # --- 2. Куда сохранять ---------------------------------------------
        out = ttk.LabelFrame(self, text="Куда сохранять", padding=(8, 6))
        out.grid(row=row, column=0, sticky="ew", pady=(self._px(6), 0))
        out.columnconfigure(1, weight=1)
        self.rb_beside = ttk.Radiobutton(out, text="Рядом с исходником",
                                         value="beside", variable=self.out_mode_var,
                                         command=self._sync_option_states)
        self.rb_beside.grid(row=0, column=0, columnspan=3, sticky="w")
        self.rb_folder = ttk.Radiobutton(out, text="В папку", value="folder",
                                         variable=self.out_mode_var,
                                         command=self._sync_option_states)
        self.rb_folder.grid(row=1, column=0, sticky="w", padx=(0, self._px(6)))
        self.out_entry = ttk.Entry(out, textvariable=self.out_dir_var)
        self.out_entry.grid(row=1, column=1, sticky="ew", padx=(0, self._px(6)))
        self.btn_browse_out = ttk.Button(out, text="Обзор…", width=10,
                                         command=self.choose_out_dir)
        self.btn_browse_out.grid(row=1, column=2)
        row += 1

        # --- 3. Параметры ---------------------------------------------------
        opt = ttk.LabelFrame(self, text="Параметры", padding=(8, 6))
        opt.grid(row=row, column=0, sticky="ew", pady=(self._px(6), 0))
        opt.columnconfigure(1, weight=1)

        # Источник не зашит: info.best может оказаться ifd1/ifd2/makernote, а с
        # галочкой «Брать превью DPP» — и vrd_ihl.  Поэтому «встроенного», а не
        # «камеры» и тем более не «DPP».
        self.chk_lossless = ttk.Checkbutton(
            opt, text="Извлечь встроенный JPEG как есть, без перекодирования",
            variable=self.lossless_var)
        self.chk_lossless.grid(row=0, column=0, columnspan=3, sticky="w")

        hint = ttk.Label(
            opt,
            text=("В каждом CR2 уже лежит готовый JPEG, отрисованный камерой "
                  "(стиль изображения, баланс белого,\nконтраст — как на экране "
                  "фотоаппарата). Его сжатые данные копируются байт в байт: "
                  "потерь нет,\nповторного сжатия не происходит. У большинства "
                  "камер этот JPEG равен полному кадру — точный\nразмер по "
                  "каждому файлу покажет кнопка «Проверить». Поворот требует "
                  "перекодирования; при\nуменьшении размера пересжимаются только "
                  "те файлы, которые больше заданного предела."),
            justify="left", **self._fg("#555555"))
        hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, self._px(4)))

        ttk.Label(opt, text="Качество JPEG:").grid(row=2, column=0, sticky="w")
        qframe = ttk.Frame(opt)
        qframe.grid(row=2, column=1, columnspan=2, sticky="ew")
        qframe.columnconfigure(0, weight=1)
        self.quality_scale = ttk.Scale(
            qframe, from_=60, to=100, orient="horizontal",
            variable=self.quality_var, command=self._on_quality_move)
        self.quality_scale.grid(row=0, column=0, sticky="ew", padx=(0, self._px(8)))
        self.quality_value = ttk.Label(qframe, textvariable=self.quality_label_var,
                                       width=4, anchor="e")
        self.quality_value.grid(row=0, column=1, sticky="e")

        ttk.Label(opt, text="Уменьшить до, px по длинной стороне:").grid(
            row=3, column=0, sticky="w", pady=(self._px(4), 0))
        self.size_spin = ttk.Spinbox(opt, values=SIZE_CHOICES, width=14,
                                     textvariable=self.size_var)
        self.size_spin.grid(row=3, column=1, sticky="w", pady=(self._px(4), 0))

        self.chk_rotate = ttk.Checkbutton(
            opt, text="Повернуть по EXIF (запечь поворот в пиксели)",
            variable=self.rotate_var)
        self.chk_rotate.grid(row=4, column=0, columnspan=3, sticky="w",
                             pady=(self._px(4), 0))
        self.chk_overwrite = ttk.Checkbutton(
            opt, text="Перезаписывать существующие", variable=self.overwrite_var)
        self.chk_overwrite.grid(row=5, column=0, columnspan=3, sticky="w")

        ttk.Label(opt, text="Суффикс к имени:").grid(row=6, column=0, sticky="w",
                                                     pady=(self._px(4), 0))
        self.suffix_entry = ttk.Entry(opt, textvariable=self.suffix_var, width=20)
        self.suffix_entry.grid(row=6, column=1, sticky="w", pady=(self._px(4), 0))

        self.chk_gps = ttk.Checkbutton(opt, text="Удалять GPS",
                                       variable=self.strip_gps_var)
        self.chk_gps.grid(row=7, column=0, columnspan=3, sticky="w")

        self.chk_dpp = ttk.Checkbutton(
            opt, text=("Если DPP когда-то записала в файл своё готовое превью "
                       "(блок IHLData) — брать его"),
            variable=self.prefer_dpp_var)
        self.chk_dpp.grid(row=8, column=0, columnspan=3, sticky="w")

        # Постоянная подпись, а НЕ предупреждение постфактум: оранжевая строка
        # таблицы появляется только у файлов с найденным трейлером и только
        # после того, как файлы уже записаны.  Цвет тот же, что у warn-строк.
        self.dpp_note = ttk.Label(
            opt,
            text=("Эта программа не применяет правки Canon DPP и не умеет этого "
                  "в принципе: рецепт DPP отрисовывает\nтолько движок самой Canon. "
                  "Извлекается снимок таким, каким его отрисовала камера. Если "
                  "нужны правки\nDPP — откройте файл в Canon Digital Photo "
                  "Professional и выполните «Конвертировать и сохранить»\n"
                  "или «Пакетная обработка»."),
            justify="left", wraplength=self._px(720), **self._fg("#8a4b00"))
        self.dpp_note.grid(row=9, column=0, columnspan=3, sticky="w",
                           pady=(self._px(6), 0))

        self.hint_label = ttk.Label(opt, textvariable=self.hint_var,
                                    justify="left",
                                    wraplength=self._px(720),
                                    **self._fg("#a05000"))
        self.hint_label.grid(row=10, column=0, columnspan=3, sticky="w",
                             pady=(self._px(4), 0))
        row += 1

        # --- 4. Кнопки -------------------------------------------------------
        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, sticky="ew", pady=(self._px(8), 0))
        self.btn_probe = ttk.Button(btns, text="Проверить", width=14,
                                    command=self.start_probe)
        self.btn_probe.grid(row=0, column=0, padx=(0, self._px(6)))
        self.btn_convert = ttk.Button(btns, text="Конвертировать", width=18,
                                      command=self.start_convert)
        self.btn_convert.grid(row=0, column=1, padx=(0, self._px(6)))
        self.btn_cancel = ttk.Button(btns, text="Отмена", width=12,
                                     command=self.cancel)
        self.btn_cancel.grid(row=0, column=2, padx=(0, self._px(6)))
        self.btn_cancel.state(["disabled"])
        self.btn_open = ttk.Button(btns, text="Открыть папку результата", width=26,
                                   command=self.open_out_dir)
        self.btn_open.grid(row=0, column=3)
        row += 1

        # --- 4a. Итог проверки ------------------------------------------------
        # Кнопка «Проверить» раньше оставляла пользователя наедине с таблицей на
        # полторы сотни строк: чтобы понять, все ли превью полноразмерные и нет
        # ли где рецепта DPP, надо было листать её глазами.  Здесь тот же итог
        # одной строкой, по которой можно принять решение.
        self.summary_label = ttk.Label(self, textvariable=self.summary_var,
                                       justify="left",
                                       wraplength=self._px(960))
        self.summary_label.grid(row=row, column=0, sticky="ew",
                                pady=(self._px(6), 0))
        self.summary_label.grid_remove()          # появляется после первого прогона
        self.summary_row = row
        row += 1

        # --- 4b. Фильтр таблицы ----------------------------------------------
        # Сводка выше отвечает «сколько», эта строка - «какие именно».  На пачке
        # в 500 снимков проблемные строки иначе приходится искать глазами по
        # цвету, прокручивая сотни зелёных.
        filt = ttk.Frame(self)
        filt.grid(row=row, column=0, sticky="ew", pady=(self._px(8), 0))
        self.chk_filter = ttk.Checkbutton(
            filt, text="Только проблемные строки (ошибки, рецепт DPP, превью меньше кадра)",
            variable=self.filter_var, command=self._on_filter_toggle)
        self.chk_filter.grid(row=0, column=0, sticky="w")
        ttk.Label(filt, textvariable=self.filter_info_var,
                  **self._fg("#8a4b00")).grid(row=0, column=1,
                                              padx=(self._px(10), 0), sticky="w")
        ttk.Label(filt, text="Щелчок по заголовку столбца сортирует таблицу.",
                  **self._fg("#555555")).grid(row=0, column=2,
                                              padx=(self._px(10), 0), sticky="w")
        row += 1

        # --- 5. Таблица ------------------------------------------------------
        table = ttk.Frame(self)
        table.grid(row=row, column=0, sticky="nsew", pady=(self._px(8), 0))
        self.rowconfigure(row, weight=1)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        style = ttk.Style()
        try:
            # Высота строки обязана следовать за ШРИФТОМ, а не только за DPI:
            # системный шрифт на разных платформах разной кегли, и 22
            # логических пикселя, подобранные под Segoe UI 9 pt, обрезают
            # текст под более крупным системным шрифтом.  На Windows
            # linespace + 4 меньше масштабированных 22, так что вид не меняется.
            from tkinter import font as _tkfont
            _line = _tkfont.nametofont("TkDefaultFont", self).metrics("linespace")
            style.configure("Treeview",
                            rowheight=max(self._px(22), int(_line) + self._px(4)))
        except Exception:
            try:
                style.configure("Treeview", rowheight=self._px(22))
            except Exception:
                pass

        cols = ("file", "preview", "share", "recipe", "status")
        self.tree = ttk.Treeview(table, columns=cols, show="headings",
                                 height=10, selectmode="extended")
        titles = (("file", "Файл", 200, "w", False),
                  ("preview", "Встроенный JPEG", 130, "center", False),
                  ("share", "Доля кадра", 90, "center", False),
                  # Столбец отвечает ровно на один вопрос: лежит ли рецепт в
                  # файле.  Применить его программа не может ни при каком
                  # значении, поэтому оговорка стоит прямо в заголовке.
                  ("recipe", "Рецепт DPP (не применяется)", 190, "center", False),
                  ("status", "Статус", 420, "w", True))
        self._col_titles = {cid: title for cid, title, _w, _a, _s in titles}
        for cid, title, width, anchor, stretch in titles:
            self.tree.heading(cid, text=title,
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=self._px(width), anchor=anchor,
                             stretch=stretch, minwidth=self._px(60))
        # Цвета строк задаются ТЕГАМИ, а не стилем, и это принципиально:
        # tag_configure у Treeview работает на всех трёх системах, включая
        # macOS/aqua, тогда как style.configure("Treeview", background=...) там
        # игнорируется.  Каждый тег ставится отдельно: если какая-то сборка Tk
        # откажется от одного цвета, остальные должны уцелеть, а окно -
        # построиться.
        for _tag, _bg, _fg in (("ok", "#e8f6e8", "#14521f"),
                               ("warn", "#fff2de", "#8a4b00"),
                               ("error", "#fde8e8", "#8b0000"),
                               ("skipped", "#f2f2f2", "#6b6b6b")):
            try:
                self.tree.tag_configure(_tag, background=_bg, foreground=_fg)
            except tk.TclError:                      # pragma: no cover
                pass
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._on_row_activate)
        self.tree.bind("<Return>", self._on_row_activate)

        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        row += 1

        # --- 6. Прогресс и статус -------------------------------------------
        prog = ttk.Frame(self)
        prog.grid(row=row, column=0, sticky="ew", pady=(self._px(6), 0))
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate",
                                        maximum=100, value=0)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.status_var).grid(
            row=1, column=0, sticky="w", pady=(self._px(3), 0))
        row += 1

        # --- 7. Сворачиваемый журнал ----------------------------------------
        logbar = ttk.Frame(self)
        logbar.grid(row=row, column=0, sticky="ew", pady=(self._px(6), 0))
        self.btn_log = ttk.Button(logbar, textvariable=self.log_btn_var, width=14,
                                  command=self.toggle_log)
        self.btn_log.grid(row=0, column=0, sticky="w")
        ttk.Button(logbar, text="Очистить журнал", width=18,
                   command=self.clear_log).grid(row=0, column=1, padx=(self._px(6), 0))
        row += 1

        self.log_frame = ttk.Frame(self)
        self.log_frame.grid(row=row, column=0, sticky="nsew", pady=(self._px(4), 0))
        self.log_frame.rowconfigure(0, weight=1)
        self.log_frame.columnconfigure(0, weight=1)
        # Шрифт подбирается по системе: Consolas есть только на Windows, и
        # зашитое имя на Mac/Linux дало бы молчаливую подмену на пропорциональный
        # шрифт - колонки журнала перестали бы совпадать.
        self.log_text = tk.Text(self.log_frame, height=8, wrap="none",
                                state="disabled", font=monospace_font(self))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        # Тег задаёт ТОЛЬКО цвет текста, а фон у tk.Text системный и
        # динамический (на macOS это systemTextBackgroundColor).  Бандл сам
        # разрешает тёмную тему (NSRequiresAquaSystemAppearance=False в
        # cr2app.spec), и тёмно-красный ложился на почти чёрное: ровные строки
        # белые и читаемые, а строки ОШИБОК — те, ради которых журнал и
        # открывают, — почти не видны.  Поэтому сначала выясняем фактический
        # фон, потом берём пару под него.
        try:
            _rgb = self.log_text.winfo_rgb(self.log_text.cget("background"))
            _luma = (0.299 * (_rgb[0] >> 8) + 0.587 * (_rgb[1] >> 8)
                     + 0.114 * (_rgb[2] >> 8)) / 255.0
        except Exception:                                   # pragma: no cover
            _luma = 1.0                                     # считаем фон светлым
        _err, _warn = (("#ff6b6b", "#ffb454") if _luma < 0.5
                       else ("#b00000", "#a06000"))
        for _tag, _color in (("err", _err), ("warn", _warn)):
            try:
                self.log_text.tag_configure(_tag, foreground=_color)
            except Exception:                               # pragma: no cover
                pass
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical",
                            command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        lhsb = ttk.Scrollbar(self.log_frame, orient="horizontal",
                             command=self.log_text.xview)
        lhsb.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=lsb.set, xscrollcommand=lhsb.set)
        self.log_row = row
        if not self.log_visible:
            self.log_frame.grid_remove()
        else:
            self.rowconfigure(self.log_row, weight=1)
        self.log_btn_var.set("Журнал ▲" if self.log_visible else "Журнал ▼")

        self.log("Готов к работе. Журнал ошибок (если возникнут): %s"
                 % _error_log_path())

    # ---------------- журнал ----------------

    def log(self, text: str, tag: str = "") -> None:
        """Буферизованная запись: реальная вставка происходит раз в тик."""
        self._log_buf.append((text, tag))

    def _flush_log(self) -> None:
        if not self._log_buf:
            return
        buf, self._log_buf = self._log_buf, []
        try:
            at_end = self.log_text.yview()[1] > 0.999
            self.log_text.configure(state="normal")
            run_tag = buf[0][1]
            run: list[str] = []
            for text, tag in buf:
                if tag != run_tag:
                    self.log_text.insert("end", "".join(run), run_tag or ())
                    run_tag, run = tag, []
                run.append(text + "\n")
            self.log_text.insert("end", "".join(run), run_tag or ())
            n = int(self.log_text.index("end-1c").split(".")[0])
            if n > MAX_LOG_LINES:
                self.log_text.delete("1.0", "%d.0" % (n - MAX_LOG_LINES))
            self.log_text.configure(state="disabled")
            if at_end:
                self.log_text.see("end")           # ровно один see() за тик
        except tk.TclError:
            pass

    def clear_log(self) -> None:
        self._log_buf = []
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.grid()
            self.rowconfigure(self.log_row, weight=1)
            self.log_btn_var.set("Журнал ▲")
        else:
            self.log_frame.grid_remove()
            self.rowconfigure(self.log_row, weight=0)
            self.log_btn_var.set("Журнал ▼")

    # ---------------- выбор путей ----------------

    def _initial_dir(self) -> str:
        for cand in (self.src_var.get(), self.settings.get("last_file_dir", ""),
                     str(Path.home() / "Pictures"), str(Path.home())):
            if cand and Path(cand).is_dir():
                return cand
        return ""

    def choose_dir(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Выберите папку с файлами CR2",
                                    initialdir=self._initial_dir(), mustexist=True)
        if not d:
            return
        self._explicit_files = []
        self._syncing = True
        try:
            self.src_var.set(os.path.normpath(d))
        finally:
            self._syncing = False
        self.settings["last_file_dir"] = os.path.normpath(d)
        self.log("Выбрана папка: %s" % os.path.normpath(d))

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(parent=self, title="Выберите файлы CR2",
                                            initialdir=self._initial_dir(),
                                            filetypes=CR2_TYPES)
        if not paths:
            return
        files = [os.path.normpath(p) for p in paths
                 if Path(p).suffix.lower() in cr2_core.CR2_EXTS]
        if not files:
            messagebox.showinfo("Файлы CR2 не выбраны",
                                "Ни один из выбранных файлов не имеет расширения .CR2.",
                                parent=self)
            return
        self._explicit_files = files
        self._syncing = True
        try:
            if len(files) == 1:
                self.src_var.set(files[0])
            else:
                self.src_var.set("Выбрано файлов: %d (папка: %s)"
                                 % (len(files), Path(files[0]).parent))
        finally:
            self._syncing = False
        self.settings["last_file_dir"] = str(Path(files[0]).parent)
        self.log("Выбрано файлов: %d" % len(files))

    def choose_out_dir(self) -> None:
        init = self.out_dir_var.get() or self._initial_dir()
        d = filedialog.askdirectory(parent=self, title="Папка для результатов",
                                    initialdir=init if Path(init).is_dir() else "",
                                    mustexist=False)
        if not d:
            return
        self.out_dir_var.set(os.path.normpath(d))
        self.out_mode_var.set("folder")
        self._sync_option_states()

    def _on_src_typed(self) -> None:
        # Ручная правка строки источника отменяет ранее выбранный список файлов.
        if self._syncing:
            return
        self._explicit_files = []

    # ---------------- взаимные блокировки параметров ----------------

    def _max_side(self) -> int:
        raw = (self.size_var.get() or "").strip()
        if not raw or raw == NO_RESIZE:
            return 0
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return 0
        try:
            value = int(digits)
        except ValueError:
            return 0
        return max(0, min(30000, value))

    def _quality(self) -> int:
        try:
            value = int(round(float(self.quality_scale.get())))
        except Exception:
            value = 95
        return max(60, min(100, value))

    def _on_quality_move(self, _value: str = "") -> None:
        self.quality_label_var.set(str(self._quality()))

    def _explain(self, text: str) -> None:
        self.hint_var.set(text)
        self.log(text, "warn")

    def _on_lossless_toggle(self) -> None:
        if self._syncing:
            return
        if self.lossless_var.get():
            reverted = []
            self._syncing = True
            try:
                # Уменьшение размера СОВМЕСТИМО с «без перекодирования»: оно
                # действует лишь на файлы больше предела.  Поворот — нет: он
                # применяется ко всем файлам без исключения.
                if self.rotate_var.get():
                    self.rotate_var.set(False)
                    reverted.append("поворот по EXIF")
            finally:
                self._syncing = False
            if reverted:
                self._explain(
                    "Режим «без перекодирования» отключает %s: копируются исходные "
                    "сжатые данные JPEG, а изменить размер или повернуть пиксели "
                    "без повторного сжатия невозможно." % " и ".join(reverted))
            else:
                self.hint_var.set("")
        self._sync_option_states()

    def _on_size_change(self) -> None:
        if self._syncing:
            return
        # Ядро решает это ПОФАЙЛОВО (need_resize сравнивает превью с пределом),
        # поэтому глобально снимать «без перекодирования» нельзя: иначе файлы,
        # которые и так меньше предела, пересжимались без всякой пользы.
        if self._max_side() > 0:
            if self.lossless_var.get():
                self._explain(
                    "Пересжаты будут только файлы, превью которых больше "
                    "выбранного предела; те, что уже меньше, скопируются "
                    "байт в байт без потерь.")
            self._warn_if_no_pillow()
        self._sync_option_states()

    def _on_rotate_toggle(self) -> None:
        if self._syncing:
            return
        if self.rotate_var.get():
            if self.lossless_var.get():
                self._syncing = True
                try:
                    self.lossless_var.set(False)
                finally:
                    self._syncing = False
                self._explain(
                    "Поворот по EXIF запекается в пиксели, а это перекодирование, "
                    "поэтому режим «без перекодирования» выключен.")
            self._warn_if_no_pillow()
        self._sync_option_states()

    def _has_pillow(self) -> bool:
        if self.caps_pillow is None:
            try:
                self.caps_pillow = cr2_core.has_pillow()
            except Exception:
                self.caps_pillow = False
        return bool(self.caps_pillow)

    def _has_rawpy(self) -> bool:
        if self.caps_rawpy is None:
            try:
                self.caps_rawpy = cr2_core.has_rawpy()
            except Exception:
                self.caps_rawpy = False
        return bool(self.caps_rawpy)

    def _warn_if_no_pillow(self) -> None:
        """Сообщаем о нехватке Pillow ТОЛЬКО когда включена опция, которой она нужна."""
        if self._has_pillow() or self._pillow_warned:
            return
        self._pillow_warned = True
        text = ("Для уменьшения размера и поворота нужна библиотека Pillow, "
                "она не установлена.\n\nУстановите её командой:\n\n    %s\n\n"
                "Без Pillow доступен только режим «без перекодирования»." % PIP_PILLOW)
        self.log(text.replace("\n\n", " "), "warn")
        messagebox.showinfo("Нужна библиотека Pillow", text, parent=self)

    def _sync_option_states(self) -> None:
        """Согласованное включение/выключение виджетов."""
        run = self.running
        lossless = bool(self.lossless_var.get())

        def setstate(widget, enabled: bool) -> None:
            try:
                widget.state(["!disabled"] if enabled else ["disabled"])
            except Exception:
                try:
                    widget.configure(state="normal" if enabled else "disabled")
                except Exception:
                    pass

        for w in (self.src_entry, self.btn_browse_src, self.btn_files,
                  self.chk_recursive, self.rb_beside, self.rb_folder,
                  self.chk_lossless, self.size_spin, self.chk_rotate,
                  self.chk_overwrite, self.suffix_entry, self.chk_gps,
                  self.btn_probe, self.btn_convert):
            setstate(w, not run)
        setstate(self.out_entry, not run and self.out_mode_var.get() == "folder")
        setstate(self.btn_browse_out, not run)
        # Качество нужно и в режиме «без перекодирования», если часть файлов
        # всё-таки будет пересжата (предел размера или запекание поворота).
        needs_quality = (not lossless) or self._max_side() > 0 or bool(self.rotate_var.get())
        setstate(self.quality_scale, not run and needs_quality)
        setstate(self.chk_dpp, not run)
        setstate(self.btn_cancel, run)
        setstate(self.btn_open, not run)

    # ---------------- запуск ----------------

    def _build_spec(self) -> dict | None:
        if self._explicit_files:
            return {"files": list(self._explicit_files),
                    "recursive": bool(self.recursive_var.get())}
        raw = (self.src_var.get() or "").strip().strip('"')
        if not raw:
            messagebox.showwarning("Не выбран источник",
                                   "Укажите папку или выберите файлы CR2.",
                                   parent=self)
            return None
        p = Path(raw)
        if not p.exists():
            messagebox.showwarning("Путь не найден",
                                   "Путь не существует:\n%s" % raw, parent=self)
            return None
        return {"root": str(p), "recursive": bool(self.recursive_var.get())}

    def _build_options(self):
        out_dir = None
        if self.out_mode_var.get() == "folder":
            raw = (self.out_dir_var.get() or "").strip().strip('"')
            if not raw:
                messagebox.showwarning("Не указана папка",
                                       "Выберите папку для результатов "
                                       "или выберите «Рядом с исходником».",
                                       parent=self)
                return None
            out_dir = Path(raw)
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("Папка недоступна",
                                     "Не удалось создать папку:\n%s\n\n%s" % (raw, exc),
                                     parent=self)
                return None
        max_side = self._max_side()
        lossless = bool(self.lossless_var.get())
        rotate = bool(self.rotate_var.get())
        if (max_side > 0 or rotate) and not self._has_pillow():
            self._pillow_warned = False
            self._warn_if_no_pillow()
        suffix = (self.suffix_var.get() or "").strip()
        problem = cr2_core.validate_suffix(suffix)
        if problem:
            messagebox.showwarning("Некорректный суффикс", problem.capitalize(),
                                   parent=self)
            return None
        return cr2_core.ConvertOptions(
            out_dir=out_dir,
            quality=self._quality(),
            max_side=max_side,
            lossless=lossless,
            bake_rotation=rotate,
            copy_exif=True,
            keep_makernote=False,
            strip_gps=bool(self.strip_gps_var.get()),
            overwrite=bool(self.overwrite_var.get()),
            suffix=suffix,
            prefer_dpp_preview=bool(self.prefer_dpp_var.get()),
        )

    def start_probe(self) -> None:
        self._start(probe_only=True)

    def start_convert(self) -> None:
        self._start(probe_only=False)

    def _start(self, probe_only: bool) -> None:
        if self.running or (self.thread is not None and self.thread.is_alive()):
            return                                   # второй запуск невозможен
        spec = self._build_spec()
        if spec is None:
            return
        opts = self._build_options()
        if opts is None:
            return

        self.tree.delete(*self.tree.get_children())
        self._details.clear()
        self._set_summary("")
        self._row_seq = 0
        self._tree_tail = None
        # Выбранные пользователем сортировка и фильтр переживают запуск: это его
        # решение, а не состояние прогона.  Сбрасывается только учёт строк.
        self._all_rows.clear()
        self._order.clear()
        self._row_keys.clear()
        self._row_problem.clear()
        self._hidden_count = 0
        self.filter_info_var.set("")      # иначе висит счётчик прошлого прогона
        self.total = 0
        self.done = 0
        self.errors = 0
        self.probe_only = probe_only
        self.cancel_evt = threading.Event()          # новое событие на каждый запуск
        self.running = True
        self._sync_option_states()
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        try:
            self.progress.start(15)
        except tk.TclError:
            pass
        self.status_var.set("Поиск файлов…")
        self.log("---- %s ----" % ("Проверка" if probe_only else "Конвертация"))
        if not probe_only and opts.out_dir is None:
            self.log("Результаты сохраняются рядом с исходными файлами.")

        try:
            self.thread = threading.Thread(
                target=job_worker, args=(spec, opts, probe_only, self.cancel_evt, self.q),
                name="cr2job", daemon=True)
            self.thread.start()
        except Exception:
            # RuntimeError("can't start new thread"), MemoryError, отказ ОС.
            # running остаётся True -> все кнопки, кроме «Отмена», навсегда
            # заблокированы, а «Отмена» ставит событие, которое некому читать.
            # Возвращаем ровно то состояние, которое успели поменять выше.
            self.thread = None
            self.running = False
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            # Именно mode, а не только stop(): иначе на экране остаётся
            # ползающий блок индикатора.
            self.progress.configure(mode="determinate", maximum=1, value=0)
            self._sync_option_states()
            p = record_error("_start", traceback.format_exc())
            self.status_var.set("Не удалось запустить рабочий поток")
            self.log("Не удалось запустить рабочий поток; подробности: %s" % p, "err")

    def cancel(self) -> None:
        if not self.running:
            return
        self.cancel_evt.set()                        # cr2_core проверяет его между файлами
        try:
            self.btn_cancel.state(["disabled"])
        except Exception:
            pass
        self.status_var.set("Отмена…")
        self.log("Запрошена отмена: уже начатые файлы будут дописаны.", "warn")

    # ---------------- цикл опроса очереди (только главный поток) ----------------

    def _poll(self) -> None:
        self._poll_id = None
        if self._destroyed or not self.winfo_exists():
            return
        try:
            n = 0
            while n < MAX_DRAIN:
                msg = self.q.get_nowait()
                n += 1
                self._handle(msg)
        except queue.Empty:
            pass
        except Exception:
            record_error("_poll", traceback.format_exc())
        finally:
            self._flush_log()
            if self._tree_tail is not None:
                try:
                    self.tree.see(self._tree_tail)     # ровно один see() за тик
                except tk.TclError:
                    pass
                self._tree_tail = None
            if not self._destroyed:
                # цикл перезапускает сам себя и живёт всё время работы окна
                self._poll_id = self.after(POLL_MS, self._poll)

    def _handle(self, msg) -> None:
        if isinstance(msg, MProgress):
            self.done, self.total = msg.done, msg.total
            if str(self.progress["mode"]) != "determinate":
                try:
                    self.progress.stop()
                except tk.TclError:
                    pass
                self.progress.configure(mode="determinate")
            self.progress.configure(maximum=max(1, msg.total), value=msg.done)
            self._update_status()
        elif isinstance(msg, MTotal):
            self.total = msg.total
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            self.progress.configure(mode="determinate",
                                    maximum=max(1, msg.total), value=0)
            self._update_status()
        elif isinstance(msg, MRow):
            self._insert_row(msg)
        elif isinstance(msg, MLog):
            self.log(msg.text, msg.tag)
        elif isinstance(msg, MCaps):
            self.caps_pillow, self.caps_rawpy = msg.pillow, msg.rawpy
            self.log("Дополнительно: Pillow — %s, rawpy — %s."
                     % ("есть" if msg.pillow else "нет",
                        "есть" if msg.rawpy else "нет"))
        elif isinstance(msg, MDone):
            self._finish_run(msg)

    # ---------------- сортировка и фильтр таблицы ----------------

    # Порядок полей в ключе строки совпадает с порядком столбцов таблицы.
    _SORT_FIELDS = ("file", "preview", "share", "recipe", "status")
    _TAG_RANK = {"error": 0, "warn": 1, "skipped": 2, "ok": 3}

    @classmethod
    def _row_key(cls, row: MRow, seq: int) -> tuple:
        """Ключ сортировки: по одному элементу на столбец плюс номер вставки.

        Номер вставки замыкает кортеж, поэтому сортировка устойчива: строки с
        одинаковым значением столбца сохраняют исходный порядок и не «прыгают»
        при каждой новой вставке.
        """
        return (row.name.lower(),
                row.preview_px,
                row.share_val,
                (1 if row.has_recipe else 0, row.name.lower()),
                (cls._TAG_RANK.get(row.tag, 9), row.status.lower()),
                seq)

    def _sort_value(self, iid: str) -> tuple:
        key = self._row_keys[iid]
        return (key[self._sort_idx], key[-1])          # _sort_idx задан вместе с _sort_col

    def _insert_pos(self, value: tuple) -> int:
        """Место новой строки в уже отсортированном self._order (бинарный поиск).

        Полная пересортировка на каждой вставке была бы O(N log N) внутри
        after()-колбэка, до 200 раз за тик; здесь - O(log N) сравнений и ровно
        один move().
        """
        lo, hi = 0, len(self._order)
        while lo < hi:
            mid = (lo + hi) // 2
            other = self._sort_value(self._order[mid])
            # «Новая строка стоит РАНЬШЕ той, что в середине» - с учётом того,
            # что при убывании порядок сравнения зеркальный.
            before = (value > other) if self._sort_desc else (value < other)
            if before:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _update_headings(self) -> None:
        for cid, title in self._col_titles.items():
            mark = ""
            if cid == self._sort_col:
                mark = " ▼" if self._sort_desc else " ▲"
            try:
                self.tree.heading(cid, text=title + mark)
            except tk.TclError:
                pass

    def _sort_by(self, col: str) -> None:
        """Щелчок по заголовку: по возрастанию -> по убыванию -> порядок ввода."""
        if self._sort_col == col:
            if not self._sort_desc:
                self._sort_desc = True
            else:
                self._sort_col, self._sort_desc = None, False
        else:
            self._sort_col, self._sort_desc = col, False
        self._sort_idx = (self._SORT_FIELDS.index(self._sort_col)
                          if self._sort_col is not None else 0)
        self._update_headings()
        self._apply_view()

    def _on_filter_toggle(self) -> None:
        self._apply_view()

    def _apply_view(self) -> None:
        """Полная пересборка порядка и видимости строк.

        Вызывается только по действию пользователя (щелчок по заголовку или
        переключение фильтра), а не на каждой вставке.
        """
        only_bad = bool(self.filter_var.get())
        hidden = [i for i in self._all_rows
                  if only_bad and i not in self._row_problem]
        drop = set(hidden)                 # считается ОДИН раз, а не на строку
        order = [i for i in self._all_rows if i not in drop]
        if self._sort_col is not None:
            order.sort(key=self._sort_value, reverse=self._sort_desc)
        try:
            if hidden:
                # Именно detach, а не delete: _details и _on_row_activate
                # держатся за iid, и удалённую строку уже не вернуть.
                self.tree.detach(*hidden)
            for pos, iid in enumerate(order):
                self.tree.move(iid, "", pos)          # move возвращает и отцепленные
        except tk.TclError:
            pass
        self._order = order
        self._hidden_count = len(hidden)
        self._update_status()

    def _insert_row(self, row: MRow) -> None:
        if row.tag == "error":
            self.errors += 1
        self._row_seq += 1
        iid = "row%d" % self._row_seq
        # yview() снимается ДО вставки: новая строка сама сдвигает вид.
        at_end = True
        try:
            at_end = self.tree.yview()[1] > 0.999
        except tk.TclError:
            pass
        self.tree.insert("", "end", iid=iid,
                         values=(row.name, row.preview, row.share, row.recipe,
                                 row.status),
                         tags=(row.tag,))
        self._details[iid] = row.detail
        self._all_rows.append(iid)
        self._row_keys[iid] = self._row_key(row, self._row_seq)
        problem = row.tag in ("warn", "error")
        if problem:
            self._row_problem.add(iid)
        # Строка, пришедшая уже ПОСЛЕ включения фильтра, обязана подчиниться
        # ему сама, иначе отфильтрованная таблица понемногу зарастает обратно.
        if self.filter_var.get() and not problem:
            try:
                self.tree.detach(iid)
            except tk.TclError:
                pass
            self._hidden_count += 1
        elif self._sort_col is not None:
            pos = self._insert_pos(self._sort_value(iid))
            try:
                self.tree.move(iid, "", pos)
            except tk.TclError:
                pass
            self._order.insert(pos, iid)
        else:
            self._order.append(iid)
        # Раньше здесь был get_children(), который на каждой строке строил
        # кортеж ВСЕХ идентификаторов дерева: O(N^2) внутри after()-колбэка, до
        # 200 раз за тик.  Прокрутка откладывается до конца тика — так же, как
        # это давно сделано для журнала в _flush_log.  При активной сортировке
        # прокрутки нет вовсе: строка встаёт в середину списка, и прыжок туда
        # выдернул бы пользователя с того места, которое он читает.
        if at_end and self._sort_col is None:
            self._tree_tail = iid
        self._update_status()

    def _update_status(self) -> None:
        text = "Готово %d из %d, ошибок %d" % (self.done, self.total, self.errors)
        self.status_var.set(text)
        self.filter_info_var.set(
            "скрыто строк: %d" % self._hidden_count if self._hidden_count else "")

    def _set_summary(self, text: str, warn: bool = False) -> None:
        """Показать (или убрать) сводную строку над таблицей."""
        self.summary_var.set(text)
        try:
            self.summary_label.configure(
                **self._fg("#8a4b00" if warn else "#14521f"))
            if text:
                self.summary_label.grid()
            else:
                self.summary_label.grid_remove()
        except tk.TclError:
            pass

    def _build_summary(self, msg: MDone) -> tuple[str, bool]:
        """Сводка по пачке: сколько просмотрено, какие превью, где рецепты.

        Считаем только по тому, что реально прочитано (msg.seen), а не по
        self.total: при отмене или сбое обхода эти числа расходятся, и итог по
        полному списку был бы неправдой.
        """
        seen = msg.seen or (msg.ok + msg.failed + msg.skipped)
        good = msg.ok
        reduced = max(0, good - msg.full)
        head = "Проверено файлов: %d." if msg.probe_only else "Обработано файлов: %d."
        parts = [head % seen]
        if good:
            parts.append("Встроенный JPEG полного размера: %d." % msg.full)
            if reduced:
                parts.append(
                    "Меньше полного кадра либо размер определить не удалось: "
                    "%d — у этих файлов извлечённый JPEG будет мельче снимка "
                    "(из них заметно меньше: %d)." % (reduced, msg.small))
            else:
                parts.append("Уменьшенных нет.")
        if msg.failed:
            parts.append("Не удалось прочитать: %d." % msg.failed)
        if msg.dpp:
            parts.append(
                "С рецептом Canon DPP: %d. Применить рецепт эта программа не "
                "может — для этого нужна сама Canon DPP «Конвертировать и "
                "сохранить» или «Пакетная обработка»." % msg.dpp)
        else:
            # НЕ «правок нет»: отсутствие рецепта в файле не доказывает,
            # что снимок нигде не редактировали.
            parts.append("Рецепт DPP не найден ни в одном файле.")
        warn = bool(msg.dpp or reduced or msg.failed)
        return " ".join(parts), warn

    def _finish_run(self, msg: MDone) -> None:
        self.running = False
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.progress.configure(mode="determinate",
                                maximum=max(1, self.total or 1),
                                value=self.total if not msg.cancelled else self.done)
        self.errors = msg.failed
        self._sync_option_states()
        if msg.out_dir:
            self.last_out_dir = msg.out_dir
        done_total = msg.ok + msg.failed + msg.skipped
        if msg.crashed:
            # Раньше здесь печаталось «Готово 0 из N, ошибок 0» с полной
            # полосой прогресса: единственным следом аварии была строка в
            # журнале, который по умолчанию свёрнут.
            self.progress.configure(value=self.done)
            tail = "Сбой обработки, файлы не сконвертированы (%s)" % msg.crashed
            self.status_var.set(tail)
            self.log(tail, "err")
            # Именно msg.log_path: это файл, в который трассировка реально
            # записалась, а не тот, который мы предпочли бы.
            _show_error_box("Сбой обработки: %s" % msg.crashed,
                            Path(msg.log_path) if msg.log_path
                            else _error_log_path())
            self._save_settings()
            return
        summary_text, summary_warn = self._build_summary(msg)
        self._set_summary(summary_text, summary_warn)
        if not msg.probe_only and not msg.cancelled and done_total != self.total:
            tail = ("Обработка завершена не полностью: %d из %d"
                    % (done_total, self.total))
            self.progress.configure(value=self.done)
            self.status_var.set(tail)
            self.log(tail, "err")
            self._save_settings()
            return
        if msg.cancelled:
            # msg.skipped включает файлы, которые отмена сняла с очереди,
            # поэтому «готово» здесь - это реально обработанные файлы.
            tail = "Отменено. Готово %d из %d, ошибок %d, не обработано %d" % (
                msg.ok, self.total, msg.failed, msg.skipped)
        elif msg.probe_only:
            tail = "Проверка завершена: пригодных %d, с ошибками %d (из %d)" % (
                msg.ok, msg.failed, self.total)
        else:
            tail = "Готово %d из %d, ошибок %d, пропущено %d" % (
                msg.ok, self.total, msg.failed, msg.skipped)
        # Две цифры, которые решают судьбу всей пачки, раньше нигде не
        # суммировались: пользователь искал оранжевые строки глазами.
        extra = []
        if msg.dpp:
            extra.append("с рецептом DPP %d (не применён)" % msg.dpp)
        if msg.small:
            extra.append("с неполноразмерным превью %d" % msg.small)
        if extra:
            tail += " (" + ", ".join(extra) + ")"
        self.status_var.set(tail)
        self.log(tail, "err" if msg.failed else ("warn" if extra else ""))
        self.log(summary_text, "warn" if summary_warn else "")
        if msg.dpp:
            self.log("У %d файл(ов) найден рецепт Canon DPP. %s"
                     % (msg.dpp, DPP_RECIPE_TEXT), "warn")
        self._save_settings()

    def _on_row_activate(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        detail = self._details.get(sel[0], "")
        if not detail:
            return
        messagebox.showinfo("Подробности", detail, parent=self)

    # ---------------- прочее ----------------

    def open_out_dir(self) -> None:
        target = ""
        if self.out_mode_var.get() == "folder" and (self.out_dir_var.get() or "").strip():
            target = self.out_dir_var.get().strip().strip('"')
        elif self.last_out_dir:
            target = self.last_out_dir
        elif self._explicit_files:
            target = str(Path(self._explicit_files[0]).parent)
        else:
            raw = (self.src_var.get() or "").strip().strip('"')
            if raw:
                p = Path(raw)
                target = str(p if p.is_dir() else p.parent)
        if not target or not Path(target).is_dir():
            messagebox.showinfo("Папка не найдена",
                                "Папка результата ещё не определена.\n"
                                "Запустите конвертацию или выберите папку вывода.",
                                parent=self)
            return
        try:
            reveal_in_file_manager(target)
        except AttributeError:                       # pragma: no cover
            messagebox.showinfo("Папка результата", target, parent=self)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку",
                                 "%s\n\n%s" % (target, exc), parent=self)

    def _save_settings(self) -> None:
        data = {
            "src": "" if self._explicit_files else (self.src_var.get() or ""),
            "recursive": bool(self.recursive_var.get()),
            "out_mode": self.out_mode_var.get(),
            "out_dir": self.out_dir_var.get() or "",
            "lossless": bool(self.lossless_var.get()),
            "quality": self._quality(),
            "max_side": self._max_side(),
            "bake_rotation": bool(self.rotate_var.get()),
            "overwrite": bool(self.overwrite_var.get()),
            "suffix": self.suffix_var.get() or "",
            "strip_gps": bool(self.strip_gps_var.get()),
            "prefer_dpp_preview": bool(self.prefer_dpp_var.get()),
            "show_log": bool(self.log_visible),
            "last_file_dir": self.settings.get("last_file_dir", ""),
        }
        self.settings.update(data)
        save_settings(data)

    # ---------------- закрытие окна ----------------

    def on_close(self) -> None:
        """Отмена -> ожидание с таймаутом -> destroy. Процесс обязан завершиться."""
        if self._closing:
            # Повторные щелчки по X раньше заново взводили таймаут и плодили
            # параллельные цепочки _wait_close: окно можно было держать
            # открытым бесконечно, щёлкая по крестику.
            try:
                self.bell()
                self.status_var.set("Завершение… ожидание рабочего потока (до %.0f с)"
                                    % CLOSE_TIMEOUT)
            except Exception:
                pass
            return
        self._closing = True
        self._save_settings()
        if self.thread is not None and self.thread.is_alive():
            self.cancel_evt.set()
            self.status_var.set("Завершение…")
            self._close_deadline = time.monotonic() + CLOSE_TIMEOUT
            try:
                self.btn_cancel.state(["disabled"])
            except Exception:
                pass
            self.after(100, self._wait_close)
            return
        self._destroy()

    def _wait_close(self) -> None:
        # Ждём короткими шагами, не блокируя главный поток внутри mainloop.
        if (self.thread is not None and self.thread.is_alive()
                and time.monotonic() < self._close_deadline):
            self.after(100, self._wait_close)
            return
        if self.thread is not None:
            self.thread.join(timeout=0.3)            # короткий join, без зависания
            if self.thread.is_alive():
                record_error("close", "рабочий поток не завершился за %.0f с; "
                                      "окно закрывается принудительно"
                                      % CLOSE_TIMEOUT)
                self._destroy()
                # Обычного завершения тут недостаточно.  Потоки пула
                # ThreadPoolExecutor с 3.9 НЕ демоны, и concurrent.futures
                # регистрирует _python_exit через threading._register_atexit,
                # поэтому интерпретатор дожидается каждого из них: окно уже
                # исчезло, а процесс ещё десятки секунд грузит CPU и дописывает
                # .jpg в папку назначения.  os._exit обходит и _python_exit, и
                # threading._shutdown.  Настройки сохранены в начале on_close.
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except Exception:
                    pass
                os._exit(0)
        self._destroy()

    def _destroy(self) -> None:
        # Снимаем всё, что Tk мог бы вызвать уже после разрушения окна:
        # незавершённый after-цикл и внутренний таймер индикатора прогресса.
        if self._destroyed:
            return
        self._destroyed = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        try:
            self.progress.stop()
        except Exception:
            pass
        try:
            self.master_root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------


def main() -> int:
    enable_dpi_awareness()                 # ОБЯЗАТЕЛЬНО до tk.Tk()
    install_crash_hooks(None)
    root = tk.Tk()
    install_crash_hooks(root)              # повторно: теперь и для tk-колбэков
    # Заголовок обещает ровно то, что программа делает: достаёт из CR2 готовый
    # JPEG камеры без перекодирования.  Прежний вариант упоминал DPP и наводил
    # на мысль, что правки DPP как-то учитываются.
    root.title("CR2 в JPEG (без потерь, как снято)")
    apply_ui_theme(root)                   # vista / aqua / clam - по системе
    apply_default_ui_font(root)            # пустая операция на Windows и macOS
    s = ui_scale(root)
    root.geometry("%dx%d" % (int(1020 * s), int(760 * s)))
    root.minsize(int(760 * s), int(560 * s))
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        path = record_error("main", traceback.format_exc())
        _show_error_box("Приложение не смогло запуститься.", path)
        sys.exit(1)
