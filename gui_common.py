# -*- coding: utf-8 -*-
"""gui_common - общий инструментарий вкладок приложения.

Каждая вкладка (tab_enhance, tab_cull, tab_poster) получает от оболочки
cr2_gui.pyw объект AppContext и берёт отсюда всё, на чём уже однажды
спотыкались: фоновую работу, цвета, миниатюры, выбор файлов, чтение снимков.
Смысл модуля - чтобы ни одна вкладка не изобретала заново потоки и тему.

--------------------------------------------------------------------------
КОНТРАКТ ВКЛАДКИ
--------------------------------------------------------------------------
Модуль вкладки объявляет ровно два имени:

    TAB_TITLE: str
    def build_tab(parent: ttk.Notebook, ctx: AppContext) -> ttk.Frame

build_tab создаёт фрейм ПОТОМКОМ parent и возвращает его; добавляет его в
блокнот оболочка.  Исключение из импорта или из build_tab не роняет
программу: вместо вкладки появляется панель с понятной ошибкой.

--------------------------------------------------------------------------
КОНТРАКТ ПОТОКОВ (тот же, что в cr2_gui.pyw)
--------------------------------------------------------------------------
* Рабочий поток НИКОГДА не трогает виджет, tk-переменную, root.after и не
  создаёт ImageTk.PhotoImage.  Он производит ДАННЫЕ - числа, строки, пути,
  объекты PIL.Image - и отдаёт их через очередь.
* Очередь разбирает главный (Tk) поток в цикле root.after(); только он
  вызывает колбэки on_progress / on_done / on_error / on_cancelled и
  подписчиков шины событий.
* Отмена - threading.Event.  cancel() только ставит событие и сразу
  возвращает управление: интерфейс не замирает.

Модуль не импортирует cr2_gui (это .pyw, и зависимость была бы круговой):
всё, что живёт там (запись настроек, журнал ошибок, «показать в проводнике»),
передаётся в AppContext снаружи.  Pillow и cr2_core импортируются лениво:
модуль грузится и без них, а понятная ошибка появляется при первом
обращении к снимку.
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

__all__ = [
    "AppContext", "BackgroundJob", "Cancelled", "ImageLoadError", "Reporter",
    "IMAGE_EXTENSIONS", "IMAGE_FILETYPES", "RAW_EXTENSIONS", "PALETTE_ROLES",
    "POLL_MS", "TOPIC_LOG", "TOPIC_PROCESSED", "TOPIC_SELECTION",
    "drain_events", "is_dark_mode", "is_tk_thread", "list_images", "load_image_fast", "path_key",
    "palette", "photo_image", "pick_files", "pick_folder", "pick_save_file",
    "run_background", "run_standalone", "system_prefers_dark", "thumbnail",
]

POLL_MS = 60            # период опроса очередей, как в cr2_gui (50-100 мс)
MAX_DRAIN = 200         # не более стольких сообщений за один тик
MAX_LOG_LINES = 500     # сколько строк журнала помнит контекст

#: Тема шины событий: список отмеченных в «Отборе» путей (list[Path]).
TOPIC_SELECTION = "selection"
#: Тема шины событий: строка журнала, payload = (text, level).
TOPIC_LOG = "log"
#: Тема шины событий: готовые файлы «Обработки», payload = {исходный путь: результат}.
#: Контекст копит их (ctx.processed_for), чтобы «Афиши» брали обработанный кадр.
TOPIC_PROCESSED = "processed"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------------
# Какой поток - Tk
# --------------------------------------------------------------------------

#: Поток, в котором живёт Tk.  Это главный поток процесса: tkinter иначе и не
#: работает.  AppContext уточняет значение при создании - на случай, если
#: когда-нибудь окно поднимут не из главного потока.
_tk_thread: threading.Thread = threading.main_thread()


def is_tk_thread() -> bool:
    """True, если код выполняется в потоке Tk (там, где можно трогать виджеты)."""
    return threading.current_thread() is _tk_thread


def drain_events(widget: tk.Misc, limit: int = 5000) -> int:
    """Обработать накопившиеся события Tk, но не больше limit за один вызов.

    Замена root.update() там, где очередь нужно прокрутить вне mainloop
    (тесты).  На macOS root.update() у спрятанного окна может не вернуться
    вовсе: в CI тест со всеми вкладками простоял в одном update() двенадцать
    минут, хотя из Python за это время выполнялись только два штатных таймера
    по 60 мс - бесконечную работу делал сам Tk Aqua.  Здесь каждое событие
    обрабатывается через dooneevent(DONT_WAIT), и число их ограничено.
    Рабочий код программы update() не вызывает вовсе - только mainloop().
    Возвращает число обработанных событий.
    """
    import _tkinter
    app = widget.tk
    done = 0
    while done < limit:
        try:
            if not app.dooneevent(_tkinter.DONT_WAIT):
                break
        except tk.TclError:             # окно уже уничтожено
            break
        done += 1
    return done


def _require_tk_thread(what: str) -> None:
    """Громко упасть, если виджетный вызов сделан из рабочего потока.

    Проверка стоит всегда, а не только под assert: без неё такая ошибка
    проявляется не здесь, а позже и случайно - RuntimeError «main thread is not
    in main loop» ровно в момент закрытия окна.
    """
    if not is_tk_thread():
        raise RuntimeError(
            "%s можно вызывать только из потока Tk, а вызван из потока %r. "
            "Рабочий поток должен вернуть данные (например, PIL.Image), "
            "а обернуть их для Tk - колбэк on_done/on_progress."
            % (what, threading.current_thread().name))


# --------------------------------------------------------------------------
# Фоновая работа
# --------------------------------------------------------------------------


class Cancelled(Exception):
    """Бросается из report.check(), когда работу отменили.

    Рабочая функция может и не бросать его, а просто вернуться, проверив
    report.cancelled: результат отменённой работы всё равно дойдёт до
    on_cancelled.
    """


class Reporter:
    """Второй конец очереди, который получает рабочая функция.

    report(0.4, "Кадр 120 из 300")  - прогресс (доля 0..1 или None = «неизвестно»);
    report.cancelled                - True, если нажали «Отмена»;
    report.check()                  - бросить Cancelled, если нажали «Отмена»;
    report.cancel_event             - сам threading.Event (для cr2_core и пулов).

    Вызывать можно из любого потока: report только кладёт кортеж в очередь.
    """

    __slots__ = ("_q", "cancel_event")

    def __init__(self, q: "queue.Queue", cancel_event: threading.Event) -> None:
        self._q = q
        self.cancel_event = cancel_event

    def __call__(self, fraction: float | None = None, text: str = "") -> None:
        if fraction is not None:
            try:
                fraction = max(0.0, min(1.0, float(fraction)))
            except (TypeError, ValueError):
                fraction = None
        self._q.put(("progress", fraction, str(text or "")))

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()


class BackgroundJob:
    """Ручка фоновой работы, которую возвращает run_background().

    Все колбэки вызываются только в потоке Tk.  Итог ровно один:
      * fn вернула значение, отмены не было  -> on_done(result);
      * отменили (fn вернулась или бросила Cancelled) -> on_cancelled(result|None);
      * fn бросила исключение                -> on_error(exc).
    После cancel() прогресс больше не показывается.  После detach() (закрытие
    окна) не вызывается ничего: виджетов, которым адресованы колбэки, уже нет.
    """

    def __init__(self, fn: Callable[[Reporter], Any], *, widget: tk.Misc,
                 on_progress: Callable[[float | None, str], None] | None,
                 on_done: Callable[[Any], None] | None,
                 on_error: Callable[[BaseException], None] | None,
                 on_cancelled: Callable[[Any], None] | None,
                 cancel_event: threading.Event | None,
                 name: str, poll_ms: int) -> None:
        self._fn = fn
        self._widget = widget
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.name = name
        self._poll_ms = max(10, int(poll_ms))
        self._q: "queue.Queue" = queue.Queue()
        self._after_id: str | None = None
        self._finished = False       # итог доставлен (или доставлять некому)
        self._detached = False
        self.thread = threading.Thread(target=self._run, name="bg-%s" % name,
                                       daemon=True)

    # ---- рабочий поток: только очередь ----

    def _run(self) -> None:
        report = Reporter(self._q, self.cancel_event)
        try:
            result = self._fn(report)
        except Cancelled:
            self._q.put(("cancelled", None))
        except BaseException as exc:            # noqa: BLE001 - отдаём в Tk-поток
            if self.cancel_event.is_set():
                # Работу прервали, и она упала на полпути (закрытый файл,
                # остановленный пул).  Для пользователя это «отменено».
                self._q.put(("cancelled", None))
            else:
                self._q.put(("error", exc))
        else:
            if self.cancel_event.is_set():
                self._q.put(("cancelled", result))
            else:
                self._q.put(("done", result))

    # ---- поток Tk ----

    def _start(self) -> None:
        self.thread.start()
        self._schedule()

    def _schedule(self) -> None:
        if self._finished or self._detached:
            return
        try:
            self._after_id = self._widget.after(self._poll_ms, self._poll)
        except (tk.TclError, RuntimeError):
            # Виджет уничтожен: доставлять итог некому.
            self._after_id = None
            self._finished = True

    def _poll(self) -> None:
        self._after_id = None
        if self._detached:
            return
        progress: tuple | None = None
        final: tuple | None = None
        for _ in range(MAX_DRAIN):
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "progress":
                progress = item             # показываем только последний
            else:
                final = item
                break
        try:
            if (progress is not None and self._on_progress is not None
                    and not self.cancel_event.is_set()):
                self._call(self._on_progress, progress[1], progress[2])
            if final is not None:
                self._finished = True
                kind, value = final
                if kind == "done":
                    if self._on_done is not None:
                        self._call(self._on_done, value)
                elif kind == "cancelled":
                    if self._on_cancelled is not None:
                        self._call(self._on_cancelled, value)
                elif self._on_error is not None:
                    self._call(self._on_error, value)
                else:
                    # Никто не взялся обработать ошибку - отдаём её обработчику
                    # tk-колбэков (cr2_gui ставит туда запись в журнал и окно).
                    self._report_exception(value)
        finally:
            self._schedule()

    def _call(self, cb: Callable, *args: Any) -> None:
        """Колбэк вкладки не должен останавливать цикл опроса."""
        try:
            cb(*args)
        except Exception as exc:                # noqa: BLE001
            self._report_exception(exc)

    def _report_exception(self, exc: BaseException) -> None:
        try:
            root = self._widget._root()         # noqa: SLF001
            root.report_callback_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    # ---- публичное ----

    def cancel(self) -> None:
        """Попросить работу остановиться.  Возвращается сразу."""
        self.cancel_event.set()

    def detach(self) -> None:
        """Отменить и больше не вызывать ни одного колбэка (закрытие окна)."""
        self.cancel_event.set()
        self._detached = True
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def finished(self) -> bool:
        """Итог доставлен в поток Tk (или отменён без доставки)."""
        return self._finished or (self._detached and not self.thread.is_alive())

    @property
    def running(self) -> bool:
        """Рабочий поток ещё выполняется."""
        return self.thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        """Дождаться рабочего потока.  True, если он завершился.

        Из потока Tk звать только с маленьким timeout: иначе окно замрёт.
        """
        self.thread.join(timeout)
        return not self.thread.is_alive()


def run_background(fn: Callable[[Reporter], Any], *,
                   on_progress: Callable[[float | None, str], None] | None = None,
                   on_done: Callable[[Any], None] | None = None,
                   on_error: Callable[[BaseException], None] | None = None,
                   on_cancelled: Callable[[Any], None] | None = None,
                   cancel_event: threading.Event | None = None,
                   widget: tk.Misc | None = None,
                   name: str = "job",
                   poll_ms: int = POLL_MS) -> BackgroundJob:
    """Выполнить fn(report) в рабочем потоке, итог доставить в поток Tk.

    Вызывать из потока Tk.  widget - любой живой виджет окна (по умолчанию
    корневое окно tkinter).  Внутри вкладки удобнее ctx.run_background(): он
    сам подставит окно и отменит работу при закрытии программы.
    """
    _require_tk_thread("run_background")
    if widget is None:
        widget = getattr(tk, "_default_root", None)
        if widget is None:
            raise RuntimeError("run_background: нет окна Tk, передайте widget=")
    job = BackgroundJob(fn, widget=widget, on_progress=on_progress,
                        on_done=on_done, on_error=on_error,
                        on_cancelled=on_cancelled, cancel_event=cancel_event,
                        name=name, poll_ms=poll_ms)
    job._start()                                # noqa: SLF001
    return job


# --------------------------------------------------------------------------
# Цвета: светлое и тёмное оформление
# --------------------------------------------------------------------------
#
# Почему цвет выбирается по ФОНУ, который рисует Tk, а не по настройке системы.
# Тема vista (Windows) и clam (Linux) тёмного оформления не умеют вовсе: при
# тёмной Windows окно Tk остаётся светлым, и «тёмные» цвета текста легли бы на
# светлый фон.  Тема aqua (macOS) наоборот следует системе, а собранный .app
# разрешает Dark Mode.  Единственный вопрос, ответ на который верен везде: «какой
# фон у окна на самом деле».  Его и задаём - тем же способом, что cr2_gui.

#: Пары подобраны под фоны ~#f0f0f0 и ~#323232 и совпадают с цветами строк
#: таблицы конвертера (ok / warn / error).
_PALETTE_LIGHT: dict[str, str] = {
    "fg": "#1a1a1a",
    "muted": "#555555",
    "ok": "#14521f",
    "warn": "#8a4b00",
    "error": "#8b0000",
    "accent": "#1d5fb4",
    "bg": "#f0f0f0",
    "card_bg": "#ffffff",
    "card_border": "#c8c8c8",
    "selection": "#cce0f5",
    "selection_fg": "#0b2a4a",
}
_PALETTE_DARK: dict[str, str] = {
    "fg": "#e8e8e8",
    "muted": "#a0a0a0",
    "ok": "#8fdca4",
    "warn": "#ffc46b",
    "error": "#ff8a8a",
    "accent": "#7db4ff",
    "bg": "#1e1e1e",
    "card_bg": "#2b2b2b",
    "card_border": "#4a4a4a",
    "selection": "#264f78",
    "selection_fg": "#ffffff",
}
#: Все роли, которые понимает palette().
PALETTE_ROLES: tuple[str, ...] = tuple(_PALETTE_LIGHT)

_last_dark = [False]        # последний ответ Tk - для вызовов не из потока Tk


def _luma(widget: tk.Misc, color: str) -> float:
    """Яркость цвета 0..1.  При ошибке 1.0 (считать фон светлым безопаснее)."""
    try:
        r, g, b = widget.winfo_rgb(color)
    except Exception:
        return 1.0
    return (0.299 * (r >> 8) + 0.587 * (g >> 8) + 0.114 * (b >> 8)) / 255.0


def system_prefers_dark() -> bool:
    """Включено ли тёмное оформление В СИСТЕМЕ.  Никогда не бросает.

    Это настройка системы, а не цвет окна Tk - см. is_dark_mode().
    Windows: реестр AppsUseLightTheme.  macOS: AppleInterfaceStyle.
    Linux: GTK_THEME с суффиксом «:dark» (другого надёжного признака нет).
    """
    try:
        if IS_WINDOWS:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        if IS_MACOS:
            import subprocess
            done = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                  capture_output=True, text=True, timeout=3,
                                  encoding="utf-8", errors="replace", check=False)
            return "dark" in (done.stdout or "").lower()
        return (os.environ.get("GTK_THEME") or "").lower().endswith(":dark")
    except Exception:
        return False


def is_dark_mode(widget: tk.Misc | None = None) -> bool:
    """Тёмный ли фон у окон Tk ПРЯМО СЕЙЧАС.

    Спрашивает тему ttk о фоне (так же, как cr2_gui.ttk_style_is_dark), потому
    что заливку ttk рисует тема, и только она знает цвет.  Без окна Tk ответ
    берётся из системы на macOS (aqua следует системе) и False на Windows и
    Linux (vista и clam всегда светлые).  Вне потока Tk возвращается
    последний ответ, полученный в потоке Tk.
    """
    if widget is None:
        widget = getattr(tk, "_default_root", None)
    if widget is None:
        return system_prefers_dark() if IS_MACOS else False
    if not is_tk_thread():
        return _last_dark[0]
    try:
        style = ttk.Style(widget)
        color = style.lookup(".", "background") or style.lookup("TFrame", "background")
    except Exception:
        color = ""
    dark = bool(color) and _luma(widget, color) < 0.5
    _last_dark[0] = dark
    return dark


def palette(role: str, widget: tk.Misc | None = None, *,
            dark: bool | None = None) -> str:
    """Цвет «#rrggbb» для роли, верный и при светлом, и при тёмном оформлении.

    Роли: fg, muted, ok, warn, error, accent, card_bg, card_border, selection
    (плюс bg и selection_fg).  dark=None - определить по окну (is_dark_mode);
    True/False - принудительно.  Цвет спрашивайте при построении виджета, а не
    храните в модульной константе: на macOS оформление меняется на ходу.
    """
    if dark is None:
        dark = is_dark_mode(widget)
    table = _PALETTE_DARK if dark else _PALETTE_LIGHT
    try:
        return table[role]
    except KeyError:
        raise ValueError("palette: неизвестная роль %r, есть: %s"
                         % (role, ", ".join(PALETTE_ROLES))) from None


# --------------------------------------------------------------------------
# Снимки: чтение, миниатюры, PhotoImage
# --------------------------------------------------------------------------

#: Что вкладки считают снимком (сравнивать с Path.suffix.lower()).
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".cr2"})
RAW_EXTENSIONS: frozenset[str] = frozenset({".cr2"})


def path_key(path: str | Path) -> str:
    """Ключ пути для словарей: абсолютный, без учёта регистра там, где его нет (Windows)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _both_cases(*exts: str) -> tuple[str, ...]:
    out: list[str] = []
    for e in exts:
        out += ["*." + e.lower(), "*." + e.upper()]
    return tuple(out)


# Обе записи регистра - из-за Linux: диалог Tk отбирает файлы командой glob без
# -nocase, и «*.cr2» там не показывает ни одного IMG_0001.CR2 (см. CR2_TYPES в
# cr2_gui).  «*», а не «*.*»: под glob «*.*» означает «в имени есть точка».
IMAGE_FILETYPES: list[tuple[str, Any]] = [
    ("Снимки", _both_cases("jpg", "jpeg", "png", "tif", "tiff", "cr2")),
    ("JPEG", _both_cases("jpg", "jpeg")),
    ("Canon RAW (CR2)", _both_cases("cr2")),
    ("PNG и TIFF", _both_cases("png", "tif", "tiff")),
    ("Все файлы", "*"),
]


class ImageLoadError(Exception):
    """Снимок не удалось прочитать.  str(exc) - готовый текст по-русски."""


def _pil():
    """Импорт Pillow с понятной ошибкой вместо голого ModuleNotFoundError."""
    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        raise ImageLoadError(
            "Для работы со снимками нужна библиотека Pillow. Установите её "
            "командой:  python -m pip install Pillow  (%s)" % exc) from exc
    return Image, ImageOps


def _is_junk_name(name: str) -> bool:
    # «._IMG_0001.JPG» - двойники AppleDouble, которые macOS оставляет на
    # флешках и картах exFAT/FAT32.  Расширение у них настоящее, а снимка нет.
    return name.startswith(".")


def list_images(folder: str | Path, recursive: bool = False, *,
                extensions: Iterable[str] = IMAGE_EXTENSIONS) -> list[Path]:
    """Снимки в папке, отсортированные по имени.  Папку только читает.

    Расширение сравнивается без учёта регистра (камера пишет IMG_0001.JPG),
    скрытые файлы и двойники AppleDouble «._*» пропускаются.  Недоступные
    подпапки пропускаются молча - для списка это не ошибка.
    """
    exts = frozenset(e.lower() for e in extensions)
    root = Path(folder)
    found: list[Path] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            if _is_junk_name(entry.name):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        stack.append(Path(entry.path))
                    continue
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in exts:
                    found.append(Path(entry.path))
            except OSError:
                continue
    found.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return found


def _normalise_mode(im):
    """RGB / RGBA / L - то, что понимают и ImageTk, и JPEG-кодер."""
    if im.mode in ("RGB", "RGBA", "L"):
        return im
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        # convert("L") из 16 бит обрезает всё выше 255 в белое.
        return im.point(lambda v: v / 257.0).convert("L")
    if im.mode in ("LA", "PA", "P") and ("transparency" in im.info or im.mode != "P"):
        return im.convert("RGBA")
    return im.convert("RGB")


_TRANSPOSE_BY_ORIENTATION = {
    2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE", 6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90",
}


def _read_cr2_preview(path: Path) -> tuple[bytes, int]:
    """Встроенный JPEG камеры и EXIF-ориентация.  RAW не декодируется."""
    try:
        import cr2_core
    except Exception as exc:
        raise ImageLoadError("Не загружен модуль cr2_core: %s" % exc) from exc
    info = cr2_core.probe(path)
    if info.error:
        raise ImageLoadError("%s: %s" % (path.name, info.error))
    preview = info.best
    if preview is None:
        raise ImageLoadError("%s: в файле CR2 нет встроенного JPEG" % path.name)
    try:
        with open(path, "rb") as f:
            f.seek(preview.offset)
            blob = f.read(preview.length)
    except OSError as exc:
        raise ImageLoadError("%s: не удалось прочитать файл: %s" % (path.name, exc)) from exc
    orientation = info.orientation
    reconcile = getattr(cr2_core, "_reconcile_orientation", None)
    if reconcile is not None:
        try:
            # Тот же разбор, что при конвертации: если превью уже повёрнуто,
            # второй раз не крутим.
            orientation, _ = reconcile(info.orientation, preview.width, preview.height,
                                       info.raw_width, info.raw_height, preview.source)
        except Exception:
            orientation = info.orientation
    return blob, int(orientation or 1)


def load_image_fast(path: str | Path, max_side: int | None = None):
    """Быстро открыть снимок для просмотра и анализа.  Файл только читается.

    * JPEG: Image.draft() - декодер сразу уменьшает в 2/4/8 раз, для кадра
      5184x3456 до 1600 px это в разы быстрее полного декодирования;
    * CR2: берётся встроенный JPEG камеры через cr2_core (RAW не декодируется),
      поворот - по EXIF самого CR2;
    * поворот по EXIF применяется к пикселям (ImageOps.exif_transpose);
    * max_side > 0 - результат вписан в квадрат max_side (без увеличения).

    Возвращает PIL.Image в режиме RGB, RGBA или L, не связанный с файлом.
    Бросает ImageLoadError с текстом по-русски.  Безопасно в рабочем потоке.
    """
    Image, ImageOps = _pil()
    p = Path(path)
    limit = int(max_side or 0)
    try:
        if p.suffix.lower() in RAW_EXTENSIONS:
            blob, orientation = _read_cr2_preview(p)
            with Image.open(io.BytesIO(blob)) as src:
                if limit > 0:
                    try:
                        src.draft("RGB", (limit, limit))
                    except Exception:
                        pass
                src.load()
                im = src.copy()
            op = _TRANSPOSE_BY_ORIENTATION.get(orientation)
            if op is not None:
                im = im.transpose(getattr(Image.Transpose, op))
        else:
            with Image.open(p) as src:
                if limit > 0 and src.format == "JPEG":
                    try:
                        src.draft(src.mode if src.mode in ("RGB", "L") else "RGB",
                                  (limit, limit))
                    except Exception:
                        pass
                src.load()
                # exif_transpose возвращает НОВОЕ изображение: после выхода
                # из with файл закрыт, а результат от него не зависит.
                im = ImageOps.exif_transpose(src)
                if im is src:                                # pragma: no cover
                    im = src.copy()
    except ImageLoadError:
        raise
    except FileNotFoundError as exc:
        raise ImageLoadError("Файл не найден: %s" % p) from exc
    except Exception as exc:                     # noqa: BLE001
        raise ImageLoadError("Не удалось открыть %s: %s" % (p.name, exc)) from exc
    im = _normalise_mode(im)
    if limit > 0 and max(im.size) > limit:
        im.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    return im


def thumbnail(pil_image, max_side: int):
    """Уменьшенная КОПИЯ, вписанная в квадрат max_side.  Оригинал не меняется.

    Меньше max_side изображение не увеличивается.  Безопасно в рабочем потоке.
    """
    Image, _ImageOps = _pil()
    im = pil_image.copy()
    side = int(max_side or 0)
    if side > 0 and max(im.size) > side:
        im.thumbnail((side, side), Image.Resampling.LANCZOS, reducing_gap=3.0)
    return im


def photo_image(pil_image, master: tk.Misc | None = None):
    """PIL.Image -> ImageTk.PhotoImage.  ТОЛЬКО в потоке Tk.

    Ссылку на результат обязательно держите (например, label.image = photo):
    иначе сборщик мусора удалит картинку, и виджет покажет пустое место.
    """
    _require_tk_thread("photo_image")
    _pil()
    try:
        from PIL import ImageTk
    except Exception as exc:
        raise ImageLoadError("В Pillow нет модуля ImageTk: %s" % exc) from exc
    return ImageTk.PhotoImage(_normalise_mode(pil_image), master=master)


# --------------------------------------------------------------------------
# Выбор папок и файлов (последняя папка запоминается для каждой вкладки)
# --------------------------------------------------------------------------

#: Ключ в ctx.tab_settings(tab), где лежит последняя папка диалога.
LAST_DIR_KEY = "last_dir"


def _initial_dir(ctx: "AppContext", tab: str) -> str:
    st = ctx.tab_settings(tab)
    for cand in (st.get(LAST_DIR_KEY, ""), str(Path.home() / "Pictures"),
                 str(Path.home())):
        try:
            if cand and Path(cand).is_dir():
                return str(cand)
        except OSError:
            continue
    return ""


def _remember_dir(ctx: "AppContext", tab: str, folder: str | Path) -> None:
    ctx.tab_settings(tab)[LAST_DIR_KEY] = os.path.normpath(str(folder))


def pick_folder(ctx: "AppContext", tab: str, *,
                title: str = "Выберите папку со снимками",
                parent: tk.Misc | None = None) -> Path | None:
    """Диалог выбора папки.  None, если пользователь передумал."""
    _require_tk_thread("pick_folder")
    d = filedialog.askdirectory(parent=parent or ctx.root, title=title,
                                initialdir=_initial_dir(ctx, tab), mustexist=True)
    if not d:
        return None
    _remember_dir(ctx, tab, d)
    return Path(os.path.normpath(d))


def pick_files(ctx: "AppContext", tab: str, *,
               title: str = "Выберите снимки",
               parent: tk.Misc | None = None,
               filetypes: list[tuple[str, Any]] | None = None,
               multiple: bool = True) -> list[Path]:
    """Диалог выбора файлов.  Пустой список, если пользователь передумал."""
    _require_tk_thread("pick_files")
    master = parent or ctx.root
    kw = dict(parent=master, title=title, initialdir=_initial_dir(ctx, tab),
              filetypes=filetypes or IMAGE_FILETYPES)
    if multiple:
        raw = filedialog.askopenfilenames(**kw)
        if isinstance(raw, str):
            # Отдельные сборки Tk отдают список Tcl одной строкой; splitlist
            # учитывает пробелы и фигурные скобки в именах.
            raw = master.tk.splitlist(raw)
        paths = [Path(os.path.normpath(p)) for p in (raw or ()) if p]
    else:
        one = filedialog.askopenfilename(**kw)
        paths = [Path(os.path.normpath(one))] if one else []
    if paths:
        _remember_dir(ctx, tab, paths[0].parent)
    return paths


def pick_save_file(ctx: "AppContext", tab: str, *,
                   title: str = "Сохранить как",
                   initialfile: str = "",
                   defaultextension: str = ".jpg",
                   filetypes: list[tuple[str, Any]] | None = None,
                   parent: tk.Misc | None = None) -> Path | None:
    """Диалог «Сохранить как».  None, если пользователь передумал.

    Вызывающий код обязан сам следить, чтобы результат не лёг поверх
    исходных снимков (папки съёмки - только для чтения).
    """
    _require_tk_thread("pick_save_file")
    types = filetypes or [("JPEG", _both_cases("jpg", "jpeg")),
                          ("PNG", _both_cases("png")), ("Все файлы", "*")]
    f = filedialog.asksaveasfilename(parent=parent or ctx.root, title=title,
                                     initialdir=_initial_dir(ctx, tab),
                                     initialfile=initialfile,
                                     defaultextension=defaultextension,
                                     filetypes=types)
    if not f:
        return None
    p = Path(os.path.normpath(f))
    _remember_dir(ctx, tab, p.parent)
    return p


# --------------------------------------------------------------------------
# Контекст приложения
# --------------------------------------------------------------------------


def _default_record_error(header: str, text: str) -> Path | None:
    try:
        sys.stderr.write("=== %s ===\n%s\n" % (header, text))
    except Exception:
        pass
    return None


def _default_reveal(target: str | Path) -> None:
    path = os.path.normpath(os.path.abspath(str(target)))
    if IS_WINDOWS:
        os.startfile(path)                          # type: ignore[attr-defined]
        return
    import subprocess
    cmd = ["open", "--", path] if IS_MACOS else ["xdg-open", path]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def _default_scale(widget: tk.Misc) -> float:
    if IS_MACOS:            # aqua живёт в логических точках, см. cr2_gui.ui_scale
        return 1.0
    try:
        return float(widget.winfo_fpixels("1i")) / 96.0
    except Exception:
        return 1.0


#: Корневые окна, которые обслуживал AppContext.  См. _pin_tk_interpreter().
_PINNED_ROOTS: list[tk.Misc] = []


def _pin_tk_interpreter(root: tk.Misc) -> None:
    """Не дать интерпретатору Tcl освободиться в рабочем потоке.

    Интерпретатор Tcl обязан умирать в том потоке, где родился, иначе Tcl
    вызывает abort() с «Tcl_AsyncDelete: async handler deleted by the wrong
    thread» - процесс падает целиком, без трассировки.  А последняя ссылка на
    окно легко оказывается в рабочем потоке: колбэки фоновых работ держат
    вкладку, вкладка - окно, и сборщик мусора разбирает этот цикл в том потоке,
    который случайно выделил память.  Так падал полный прогон тестов, где окна
    создаются и уничтожаются десятками; в программе то же грозило при закрытии,
    пока дорабатывает фоновая работа.  Поэтому окно, отданное контексту, живёт до
    конца процесса (освобождается при завершении интерпретатора, в главном
    потоке).
    """
    try:
        tk_root = root._root()                  # noqa: SLF001 - сам tk.Tk
    except Exception:
        tk_root = root
    if not any(r is tk_root for r in _PINNED_ROOTS):
        _PINNED_ROOTS.append(tk_root)


class AppContext:
    """Всё, что оболочка даёт вкладке.  Создаётся и используется в потоке Tk.

    Из любого потока можно звать только log() и publish(): они сами доставят
    сообщение в поток Tk.  Остальное - только из потока Tk.
    """

    def __init__(self, root: tk.Misc, *,
                 settings: dict | None = None,
                 save_settings: Callable[[dict], None] | None = None,
                 record_error: Callable[[str, str], Any] | None = None,
                 reveal: Callable[[str | Path], None] | None = None,
                 scale: float | None = None,
                 poll_ms: int = POLL_MS) -> None:
        global _tk_thread
        _tk_thread = threading.current_thread()
        self.root = root
        _pin_tk_interpreter(root)
        #: Общий словарь настроек программы.  Вкладкам - только через tab_settings().
        self.settings: dict = settings if settings is not None else {}
        self._save_fn = save_settings
        self._record_fn = record_error or _default_record_error
        self._reveal_fn = reveal or _default_reveal
        #: Множитель для пиксельных размеров: 1.0 при 96 DPI.  См. px().
        self.scale: float = float(scale) if scale else _default_scale(root)
        self._poll_ms = max(10, int(poll_ms))
        self._subs: dict[str, list[Callable[[Any], None]]] = {}
        self._inbox: "queue.Queue" = queue.Queue()
        self._jobs: list[BackgroundJob] = []
        self._shutdown_cbs: list[Callable[[], None]] = []
        self._selection: list[Path] = []
        self._processed: dict[str, Path] = {}
        self._hints: dict[str, dict[str, Any]] = {}
        self._closing = False
        self._pump_id: str | None = None
        #: Последние строки журнала: (text, level).
        self.log_lines: deque[tuple[str, str]] = deque(maxlen=MAX_LOG_LINES)
        try:
            # Окно уничтожили мимо shutdown() (тесты, аварийный выход): таймеры
            # after иначе переживают окно и сыплют «invalid command name».
            root.bind("<Destroy>", self._on_root_destroy, add="+")
        except Exception:
            pass
        self._schedule_pump()

    def _on_root_destroy(self, event: Any) -> None:
        if getattr(event, "widget", None) is not self.root:
            return                  # привязка корня срабатывает и для потомков
        self._closing = True
        for job in list(self._jobs):
            job.detach()
        if self._pump_id is not None:
            try:
                self.root.after_cancel(self._pump_id)
            except Exception:
                pass
            self._pump_id = None

    # ---------------- настройки ----------------

    def tab_settings(self, tab: str) -> dict:
        """Словарь настроек вкладки: settings["tabs"][tab].

        Изменения в нём сохраняются вместе с настройками программы (при
        закрытии окна, при смене вкладки или по ctx.save_settings()).  Значения -
        только то, что переживёт JSON: str, int, float, bool, None, list, dict.
        Ключ «last_dir» занят pick_folder/pick_files.
        """
        if not isinstance(tab, str) or not tab:
            raise ValueError("tab_settings: имя вкладки должно быть непустой строкой")
        tabs = self.settings.get("tabs")
        if not isinstance(tabs, dict):
            tabs = self.settings["tabs"] = {}
        own = tabs.get(tab)
        if not isinstance(own, dict):
            own = tabs[tab] = {}
        return own

    def save_settings(self) -> None:
        """Записать настройки на диск сейчас.  Ошибка записи не бросается."""
        if self._save_fn is None:
            return
        try:
            self._save_fn(self.settings)
        except Exception as exc:                # noqa: BLE001
            self.record_error("save_settings", exc)

    # ---------------- журнал и ошибки ----------------

    def log(self, msg: str, level: str = "") -> None:
        """Строка в строку состояния окна.  level: "", "ok", "warn", "error".

        Можно звать из любого потока.
        """
        self.publish(TOPIC_LOG, (str(msg), str(level or "")))

    def record_error(self, where: str, exc: BaseException | str) -> Path | None:
        """Записать трассировку в журнал ошибок.  Никогда не бросает.

        Возвращает путь файла журнала (None, если записать было некуда).
        """
        if isinstance(exc, BaseException):
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            text = str(exc)
        try:
            result = self._record_fn(str(where), text)
        except Exception:
            return None
        return Path(result) if result else None

    def show_error(self, where: str, exc: BaseException | str, summary: str = "") -> None:
        """Записать ошибку в журнал и показать окно с понятным текстом."""
        path = self.record_error(where, exc)
        text = summary or (str(exc) if not isinstance(exc, BaseException)
                           else "%s: %s" % (type(exc).__name__, exc))
        if path is not None:
            text += "\n\nПодробности записаны в файл:\n%s" % path
        self.log(summary or text.split("\n", 1)[0], "error")
        try:
            messagebox.showerror("Ошибка", text, parent=self.root)
        except Exception:
            pass

    def reveal(self, path: str | Path) -> bool:
        """Показать папку (или папку, где лежит файл) в файловом менеджере.

        При неудаче показывает окно с ошибкой и возвращает False.
        """
        target = Path(path)
        try:
            if target.is_file():
                target = target.parent
        except OSError:
            pass
        try:
            self._reveal_fn(target)
            return True
        except Exception as exc:                # noqa: BLE001
            try:
                messagebox.showerror("Не удалось открыть папку",
                                     "%s\n\n%s" % (target, exc), parent=self.root)
            except Exception:
                pass
            return False

    # ---------------- шина событий ----------------

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Подписаться на тему.  Колбэк всегда вызывается в потоке Tk.

        Возвращает функцию отписки.  Исключение в колбэке записывается в
        журнал и не мешает остальным подписчикам.
        """
        lst = self._subs.setdefault(str(topic), [])
        lst.append(callback)

        def unsubscribe() -> None:
            try:
                lst.remove(callback)
            except ValueError:
                pass
        return unsubscribe

    def publish(self, topic: str, payload: Any = None) -> None:
        """Разослать событие.  Можно звать из любого потока.

        Из потока Tk подписчики вызываются сразу, до возврата из publish;
        из рабочего потока - на ближайшем тике опроса в потоке Tk.
        """
        if is_tk_thread():
            self._deliver(str(topic), payload)
        else:
            self._inbox.put((str(topic), payload))

    def _deliver(self, topic: str, payload: Any) -> None:
        if topic == TOPIC_SELECTION:
            self._selection = [Path(p) for p in (payload or ())]
            payload = list(self._selection)
        elif topic == TOPIC_PROCESSED:
            fresh = {Path(src): Path(dst) for src, dst in dict(payload or {}).items()}
            for src, dst in fresh.items():
                self._processed[path_key(src)] = dst
            payload = fresh
        elif topic == TOPIC_LOG:
            text, level = payload if isinstance(payload, tuple) else (str(payload), "")
            payload = (text, level)
            self.log_lines.append(payload)
        for cb in list(self._subs.get(topic, ())):
            try:
                cb(payload)
            except Exception as exc:            # noqa: BLE001
                self.record_error("подписчик темы %r" % topic, exc)

    def _schedule_pump(self) -> None:
        if self._closing:
            return
        try:
            self._pump_id = self.root.after(self._poll_ms, self._pump)
        except (tk.TclError, RuntimeError):
            self._pump_id = None

    def _pump(self) -> None:
        self._pump_id = None
        try:
            for _ in range(MAX_DRAIN):
                try:
                    topic, payload = self._inbox.get_nowait()
                except queue.Empty:
                    break
                self._deliver(topic, payload)
        finally:
            self._schedule_pump()

    # ---------------- отмеченные снимки ----------------

    @property
    def selection(self) -> list[Path]:
        """Снимки, отмеченные во вкладке «Отбор» (копия списка)."""
        return list(self._selection)

    def set_selection(self, paths: Iterable[str | Path]) -> None:
        """Заменить отмеченные снимки и разослать тему "selection"."""
        self.publish(TOPIC_SELECTION, [Path(p) for p in paths])

    # ---------------- обработанные кадры и подсказки о кадрах ----------------

    def publish_processed(self, mapping: dict[str | Path, str | Path]) -> None:
        """Сообщить о готовых файлах «Обработки»: {исходный снимок: результат}.

        Можно звать из любого потока.  Более поздний результат того же снимка
        заменяет прежний.
        """
        self.publish(TOPIC_PROCESSED, {Path(k): Path(v) for k, v in mapping.items()})

    def processed_for(self, path: str | Path) -> Path | None:
        """Обработанная версия снимка, если она есть на диске (иначе None)."""
        out = self._processed.get(path_key(path))
        try:
            return out if out is not None and out.is_file() else None
        except OSError:
            return None

    def set_photo_hints(self, hints: dict[str | Path, dict[str, Any]]) -> None:
        """Что вкладка знает о кадрах, например {"face_box": (x, y, w, h) в долях}.

        Подсказки дополняют прежние (ключ - путь снимка).  Только поток Tk.
        """
        for path, info in hints.items():
            if isinstance(info, dict):
                self._hints[path_key(path)] = dict(info)

    def photo_hint(self, path: str | Path) -> dict[str, Any]:
        """Подсказки о кадре (копия словаря; пустой, если ничего не известно)."""
        return dict(self._hints.get(path_key(path), {}))

    # ---------------- фоновая работа и закрытие ----------------

    def run_background(self, fn: Callable[[Reporter], Any], **kwargs: Any) -> BackgroundJob:
        """run_background() с окном программы; работа отменяется при закрытии.

        Принимает те же именованные аргументы: on_progress, on_done, on_error,
        on_cancelled, cancel_event, name.
        """
        if self._closing:
            raise RuntimeError("программа закрывается, новая работа не запускается")
        kwargs.setdefault("widget", self.root)
        kwargs.setdefault("poll_ms", self._poll_ms)
        job = run_background(fn, **kwargs)
        self._jobs = [j for j in self._jobs if not j.finished]
        self._jobs.append(job)
        return job

    def active_jobs(self) -> list[BackgroundJob]:
        """Работы, рабочий поток которых ещё выполняется."""
        return [j for j in self._jobs if j.running]

    def register_shutdown(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Колбэк при закрытии окна (поток Tk).  Возвращает функцию отмены.

        Здесь вкладка останавливает то, чего ctx не видит сам: свои пулы
        процессов, таймеры after, открытые файлы.  Работы, запущенные через
        ctx.run_background, отменяются автоматически.  Колбэк должен
        вернуться быстро: ждать потоки будет оболочка.
        """
        self._shutdown_cbs.append(callback)

        def unregister() -> None:
            try:
                self._shutdown_cbs.remove(callback)
            except ValueError:
                pass
        return unregister

    @property
    def closing(self) -> bool:
        """Окно закрывается: новую работу не начинать."""
        return self._closing

    def shutdown(self) -> None:
        """Отменить все работы и вызвать колбэки закрытия.  Повторно - ничего."""
        if self._closing:
            return
        self._closing = True
        for job in list(self._jobs):
            job.detach()
        for cb in reversed(list(self._shutdown_cbs)):
            try:
                cb()
            except Exception as exc:            # noqa: BLE001
                self.record_error("колбэк закрытия", exc)
        if self._pump_id is not None:
            try:
                self.root.after_cancel(self._pump_id)
            except Exception:
                pass
            self._pump_id = None

    # ---------------- мелочи ----------------

    def px(self, value: float) -> int:
        """Логические пиксели (при 96 DPI) -> реальные для этого экрана."""
        return int(round(value * self.scale))


# --------------------------------------------------------------------------
# Запуск одной вкладки отдельно (для разработки вкладок)
# --------------------------------------------------------------------------


def run_standalone(module: Any, *, settings: dict | None = None) -> int:
    """Открыть окно с одной вкладкой - без конвертера и без файла настроек.

    Пример в конце tab_enhance.py:
        if __name__ == "__main__":
            import gui_common, sys
            sys.exit(gui_common.run_standalone(sys.modules[__name__]))
    """
    root = tk.Tk()
    title = str(getattr(module, "TAB_TITLE", "Вкладка"))
    root.title(title)
    ctx = AppContext(root, settings=settings if settings is not None else {})
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    frame = module.build_tab(nb, ctx)
    nb.add(frame, text=title)
    root.geometry("%dx%d" % (ctx.px(1100), ctx.px(760)))

    def close() -> None:
        ctx.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0
