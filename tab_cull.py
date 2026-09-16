# -*- coding: utf-8 -*-
"""tab_cull - вкладка «Отбор»: контактный лист съёмки, один кадр на серию.

Что делает редактор на этой вкладке:

  1. выбирает папку съёмки (JPEG или CR2) и нажимает «Анализировать» -
     cull.scan() разбирает кадры в фоне, с прогрессом и отменой;
  2. видит КОНТАКТНЫЙ ЛИСТ: по карточке на серию (главный кадр серии), в порядке
     события и под заголовками фрагментов («фрагмент 3 · 12 серий»); на карточке
     миниатюра, флажок, размер серии («серия из 5 — раскрыть») и замечания
     («тёмное лицо», «нерезко» ...);
  3. «Предложить 30 лучших» отмечает cull.suggest(result, 30), «раскрыть»
     показывает всю серию, и главный кадр можно заменить; двойной щелчок
     открывает оригинал в просмотрщике системы;
  4. отмеченное сразу уходит в ctx.selection (тема "selection") - его берут
     другие вкладки; «Скопировать отмеченные в папку…» КОПИРУЕТ файлы
     (cull.export_selection никогда не перемещает и не перезаписывает).

ПАПКА СЪЁМКИ - ТОЛЬКО ДЛЯ ЧТЕНИЯ.  Отметки запоминаются в настройках программы
(ctx.tab_settings("cull")["selections"], ключ - папка), а не рядом с фото.
Копировать в саму папку съёмки или внутрь неё вкладка отказывается.

ПОТОКИ.  Разбор, подбор предложения, чтение миниатюр и копирование идут через
ctx.run_background.  Рабочий поток отдаёт ДАННЫЕ (CullResult, PIL.Image);
ImageTk.PhotoImage создаётся только в потоке Tk, пачками через after() и
начиная с карточек, которые видны на экране.

ЛИСТ.  Карточки - элементы одного tk.Canvas (рамка, картинка, нарисованный
флажок, тексты, метки), а не виджеты: 300 карточек из виджетов - это ~2000
окон, и их раскладка на Windows замораживала программу на секунды.  Элементы
холста строятся пачками и перекладываются (сортировка, «только отмеченные»,
ширина окна) простым сдвигом.

ЦВЕТА.  Ни одного жёстко заданного цвета текста: фон и рамки карточек, флажки и
метки замечаний берут цвет из gui_common.palette() и перекрашиваются при
смене темы (на macOS тёмное оформление включается на ходу).
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import cull
import gui_common

__all__ = [
    "TAB_TITLE", "TAB_KEY", "build_tab", "CullTab",
    "SORT_EVENT", "SORT_SCORE", "FLAG_CHIPS",
    "plural_ru", "counter_text", "segment_title", "burst_text", "fmt_clock",
    "folder_key", "common_folder", "layout_items", "score_order_for", "sharpness_places",
    "sharpness_text",
    "export_problem", "fit_thumbnail", "open_in_viewer",
]

TAB_TITLE = "Отбор"
#: Ключ вкладки в ctx.tab_settings() и у оболочки (имя модуля без «tab_»).
TAB_KEY = "cull"
#: Отдельный ключ для диалога «куда копировать», чтобы он не сбивал папку съёмки.
EXPORT_DIALOG_KEY = "cull_export"

SORT_EVENT = "event"
SORT_SCORE = "score"
SORT_LABELS: dict[str, str] = {SORT_EVENT: "по ходу события", SORT_SCORE: "по оценке"}

DEFAULT_N = 30
MAX_N = 999
THUMB_W = 176            # логические пиксели (96 DPI); ctx.px() переводит в реальные
THUMB_H = 132
BUILD_BATCH = 24         # карточек за один тик after(): ~30 мс на Windows
STEP_MS = 12             # пауза между пачками: окно успевает перерисоваться
THUMB_BATCH = 16         # миниатюр за одну фоновую работу
PHOTO_BATCH = 16         # PhotoImage за один тик after() в потоке Tk
CARD_PAD = 6             # поля внутри карточки
CARD_GAP = 8             # зазор между карточками
SHEET_MARGIN = 6         # поле листа
THUMB_WORKERS = max(1, min(8, os.cpu_count() or 2))
SAVE_DELAY_MS = 1000     # отметки пишутся на диск не чаще раза в секунду
LAYOUT_DELAY_MS = 80
MAX_SAVED_FOLDERS = 40   # столько папок съёмки помнят свои отметки

#: Флаг cull -> (короткая надпись на карточке, роль цвета в palette()).
FLAG_CHIPS: dict[str, tuple[str, str]] = {
    "unreadable": ("не читается", "error"),
    "soft": ("нерезко", "error"),
    "blown_face": ("пересвет", "warn"),
    "dark_face": ("тёмное лицо", "warn"),
    "no_face": ("лица не найдено", "muted"),
}

_SCROLL_TAG = "CullSheetScroll"      # колесо мыши над листом
_STRIP_TAG = "CullStripScroll"       # колесо мыши над лентой серии

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------------
# Чистые помощники (без Tk) - их проверяют тесты
# --------------------------------------------------------------------------


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Форма слова для числа: forms = (1 серия, 2 серии, 5 серий)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def counter_text(ticked: int, total: int) -> str:
    """«отмечено 27 из 153 серий» (после «из» - родительный падеж)."""
    return "отмечено %d из %d %s" % (ticked, total,
                                     plural_ru(total, ("серии", "серий", "серий")))


def fmt_clock(seconds: float | None) -> str:
    """Время от начала съёмки: «7:05» или «1:02:30».  Пусто, если часов нет."""
    if seconds is None:
        return ""
    s = max(0, int(round(float(seconds))))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def segment_title(seg: cull.Segment, ticked: int = 0) -> str:
    """«фрагмент 3 · 12 серий · 12:30–17:05 · отмечено 2»."""
    n = len(seg.burst_ids)
    parts = ["фрагмент %d" % (seg.id + 1),
             "%d %s" % (n, plural_ru(n, ("серия", "серии", "серий")))]
    start, end = fmt_clock(seg.t_start), fmt_clock(seg.t_end)
    if start and end:
        parts.append(start if start == end else "%s–%s" % (start, end))
    if ticked:
        parts.append("отмечено %d" % ticked)
    return " · ".join(parts)


def burst_text(size: int, expanded: bool = False) -> str:
    """Подпись размера серии на карточке."""
    if size <= 1:
        return "одиночный кадр"
    return "серия из %d — %s" % (size, "свернуть" if expanded else "раскрыть")


def sharpness_places(result: cull.CullResult) -> dict[int, tuple[int, int]]:
    """capture_order -> (место по резкости во фрагменте, сколько там читаемых кадров).

    Место, а не процент: процент - это ранг внутри фрагмента, и «резкость 8 %»
    у совершенно резкого кадра читается как «мыло».
    """
    places: dict[int, tuple[int, int]] = {}
    for seg in result.segments:
        ok = [r for r in result.images[seg.first:seg.last + 1] if not r.error]
        ok.sort(key=lambda r: (-r.sharpness, r.capture_order))
        for place, rec in enumerate(ok, 1):
            places[rec.capture_order] = (place, len(ok))
    return places


def sharpness_text(place: tuple[int, int] | None) -> str:
    """«по резкости 3-й из 17 во фрагменте» (пусто, если места нет)."""
    if place is None:
        return ""
    k, n = place
    if n <= 1:
        return "единственный кадр фрагмента"
    return "по резкости %d-й из %d во фрагменте" % (k, n)


def folder_key(folder: str | os.PathLike[str]) -> str:
    """Ключ папки в настройках: абсолютный путь без учёта регистра там, где его нет."""
    return os.path.normcase(os.path.abspath(str(folder)))


def common_folder(result: cull.CullResult) -> Path | None:
    """Папка, где лежат все кадры результата, или None (кадры из разных папок)."""
    parents = {folder_key(r.path.parent) for r in result.images}
    if len(parents) != 1 or not result.images:
        return None
    return result.images[0].path.parent


def score_order_for(result: cull.CullResult) -> list[int]:
    """Номера серий в порядке «по оценке»: как cull.suggest(result, None).

    Дорогая функция (сравнение похожести кадров) - в программе считается в
    рабочем потоке вместе с разбором.
    """
    order = [r.burst_id for r in cull.suggest(result, None)]
    seen = set(order)
    order += [b.id for b in result.bursts if b.id not in seen]
    return order


def layout_items(result: cull.CullResult | None, *, sort: str = SORT_EVENT,
                 ticked: Iterable[int] = (), only_ticked: bool = False,
                 score_order: list[int] | None = None) -> list[tuple[str, int]]:
    """Что показывает лист, по порядку: ("segment", id) и ("burst", id).

    По ходу события серии идут под заголовками фрагментов; по оценке - одним
    списком без заголовков.  only_ticked оставляет только отмеченные серии
    (и заголовки фрагментов, где они есть).
    """
    if result is None:
        return []
    marked = set(ticked)

    def keep(bid: int) -> bool:
        return not only_ticked or bid in marked

    items: list[tuple[str, int]] = []
    if sort == SORT_SCORE:
        order = score_order if score_order is not None else score_order_for(result)
        return [("burst", b) for b in order if keep(b)]
    for seg in result.segments:
        bids = [b for b in seg.burst_ids if keep(b)]
        if bids:
            items.append(("segment", seg.id))
            items.extend(("burst", b) for b in bids)
    return items


def _norm_dir(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _is_inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:               # разные диски Windows
        return False


def export_problem(out_dir: str | os.PathLike[str] | None,
                   sources: Iterable[str | os.PathLike[str]],
                   shoot_folder: str | os.PathLike[str] | None = None) -> str:
    """Почему копировать в out_dir нельзя (текст по-русски) или "" - можно.

    Нельзя в папку, где лежит любой из исходных файлов, в папку съёмки и в
    любую их подпапку: папки со снимками программа только читает.
    """
    if out_dir is None or not str(out_dir).strip():
        return "Не выбрана папка для копий."
    folders = {_norm_dir(Path(s).parent) for s in sources}
    if shoot_folder:
        folders.add(_norm_dir(shoot_folder))
    out = _norm_dir(out_dir)
    for f in sorted(folders):
        if _is_inside(out, f):
            return ("Копии нельзя класть в папку со снимками или внутрь неё:\n%s\n\n"
                    "Выберите другую папку." % f)
    return ""


def fit_thumbnail(im: Any, box_w: int, box_h: int) -> Any:
    """Копия PIL.Image, вписанная в box_w x box_h, без увеличения.  Для рабочего потока."""
    from PIL import Image
    out = im.copy()
    if out.width > box_w or out.height > box_h:
        out.thumbnail((max(1, box_w), max(1, box_h)), Image.Resampling.LANCZOS)
    return out


def open_in_viewer(path: str | os.PathLike[str]) -> None:
    """Открыть файл программой системы по умолчанию.  Файл не меняется.

    Бросает OSError, если открыть не удалось.
    """
    p = os.path.normpath(os.path.abspath(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError("Файл не найден: %s" % p)
    if IS_WINDOWS:
        starter = getattr(os, "startfile", None)
        if starter is None:                               # pragma: no cover
            raise OSError("os.startfile недоступен")
        starter(p)
        return
    import subprocess
    # abspath гарантирует ведущую «/»: путь не примут за ключ командной строки.
    cmd = ["open", "--", p] if IS_MACOS else ["xdg-open", p]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def _elide(text: str, font: tkfont.Font, width: int) -> str:
    """Обрезать текст с «…», чтобы он влез в width пикселей."""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _load_thumbs(items: list[tuple[int, Path]], box: tuple[int, int],
                 report: gui_common.Reporter) -> list[tuple[int, Any, str]]:
    """Рабочий поток: [(capture_order, PIL.Image | None, ошибка)].  Файлы только читаются."""
    def one(item: tuple[int, Path]) -> tuple[int, Any, str]:
        order, path = item
        if report.cancelled:
            return order, None, ""
        try:
            im = gui_common.load_image_fast(path, max_side=max(box))
            return order, fit_thumbnail(im, box[0], box[1]), ""
        except Exception as exc:                         # noqa: BLE001
            return order, None, str(exc) or type(exc).__name__

    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(THUMB_WORKERS, len(items)),
                            thread_name_prefix="cull-thumb") as pool:
        return list(pool.map(one, items))


@dataclass
class _Prepared:
    """Итог фонового разбора: всё, что нужно листу, посчитанное вне потока Tk."""
    result: cull.CullResult
    folder: Path
    score_order: list[int]


@dataclass
class _Card:
    """Карточка листа - группа элементов холста с тегом tag («b12»).

    Карточки рисуются элементами одного tk.Canvas, а не отдельными виджетами:
    300 карточек из виджетов - это ~2000 окон, и одна их раскладка на Windows
    замораживала программу на 4 секунды.  Элементы холста двигаются за
    миллисекунды.
    """
    burst_id: int
    tag: str
    order: int = -1                                   # capture_order показанного кадра
    x: int = 0                                        # левый верхний угол на холсте
    y: int = 0
    height: int = 0
    shown: bool = True
    items: dict[str, int] = field(default_factory=dict)              # роль -> элемент холста
    chips: list[tuple[int, int, str]] = field(default_factory=list)   # (рамка, текст, роль)

    def contains(self, x: float, y: float, width: int) -> bool:
        return self.x <= x <= self.x + width and self.y <= y <= self.y + self.height


# --------------------------------------------------------------------------
# Вкладка
# --------------------------------------------------------------------------


class CullTab(ttk.Frame):
    """Вкладка «Отбор».  Создаётся и живёт в потоке Tk.

    Для тестов и других вкладок открыты: set_result(), analyse(), set_ticked(),
    toggle(), clear_ticks(), suggest_best(), apply_suggestion(),
    set_representative(), expand_burst(), collapse_burst(), set_sort(),
    set_only_ticked(), selected_paths(), export_to(), а для проверки листа -
    cards, card_text(), header_text(), empty_text(), card_at(), hit_test().
    """

    def __init__(self, parent: tk.Misc, ctx: gui_common.AppContext) -> None:
        super().__init__(parent, padding=ctx.px(8))
        self.ctx = ctx
        self.settings: dict = ctx.tab_settings(TAB_KEY)

        self.result: cull.CullResult | None = None
        self.folder: Path | None = None
        self.score_order: list[int] | None = None
        self._places: dict[int, tuple[int, int]] = {}
        self.rep: dict[int, int] = {}                 # burst_id -> capture_order главного кадра
        self.ticked: set[int] = set()
        self.expanded: int | None = None
        self.sort = self.settings.get("sort") if self.settings.get("sort") in SORT_LABELS \
            else SORT_EVENT

        self.cards: dict[int, _Card] = {}
        self.headers: dict[int, int] = {}             # segment_id -> элемент холста
        self._pending_build: list[int] = []
        self._build_after: str | None = None
        self._layout_after: str | None = None
        self._save_after: str | None = None
        self._photo_after: str | None = None
        self._cols = 1
        self._colors: dict[str, str] = {}

        self._gen = 0                                 # номер результата: старые ответы - мимо
        self._photos: dict[int, Any] = {}             # capture_order -> PhotoImage (держим ссылки)
        self._pil: dict[int, Any] = {}                # готовые PIL-миниатюры, ещё не обёрнутые
        self._thumb_failed: dict[int, str] = {}
        self._thumb_queue: list[int] = []
        self._thumb_inflight: set[int] = set()
        self._thumb_job: gui_common.BackgroundJob | None = None
        self._job: gui_common.BackgroundJob | None = None       # разбор или копирование
        self._job_kind = ""
        self._suggest_job: gui_common.BackgroundJob | None = None
        self._last_export_dir: Path | None = None
        self._panel_members: dict[int, tk.Label] = {}
        self._muted_labels: list[ttk.Label] = []

        self.box_w = ctx.px(THUMB_W)
        self.box_h = ctx.px(THUMB_H)
        self._blank = tk.PhotoImage(master=self, width=self.box_w, height=self.box_h)
        self._make_fonts()
        self._refresh_colors()
        self._build_ui()
        self._install_scroll_bindings()

        self._unregister_shutdown = ctx.register_shutdown(self._on_shutdown)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<<ThemeChanged>>", lambda _e: self.after_idle(self._recolor), add="+")
        self._update_counter()
        self._update_controls()
        self._layout_now()

    # ---------------- построение интерфейса ----------------

    def _make_fonts(self) -> None:
        try:
            base = tkfont.nametofont("TkDefaultFont", root=self)
            actual = base.actual()
        except Exception:
            actual = {"family": "TkDefaultFont", "size": 9}
        size = int(actual.get("size") or 9)
        small = max(7, size - 1) if size > 0 else min(-9, size + 1)
        self.base_font = tkfont.Font(self, family=actual.get("family"), size=size)
        self.small_font = tkfont.Font(self, family=actual.get("family"), size=small)
        self.bold_font = tkfont.Font(self, family=actual.get("family"), size=size,
                                     weight="bold")

    def _refresh_colors(self) -> None:
        """Запомнить цвета palette() для текущей темы (светлой или тёмной)."""
        dark = gui_common.is_dark_mode(self)
        self._colors = {role: gui_common.palette(role, dark=dark)
                        for role in gui_common.PALETTE_ROLES}

    def color(self, role: str) -> str:
        """Цвет роли palette() для текущей темы."""
        try:
            return self._colors[role]
        except KeyError:
            return gui_common.palette(role, self)

    def _muted(self, label: ttk.Label) -> ttk.Label:
        label.configure(foreground=gui_common.palette("muted", self))
        self._muted_labels.append(label)
        return label

    def _build_ui(self) -> None:
        px = self.ctx.px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # -- строка 0: папка, анализ, прогресс --
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Папка съёмки:").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=str(self.settings.get("folder") or ""))
        self.folder_entry = ttk.Entry(bar, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=(px(6), px(6)))
        self.folder_entry.bind("<Return>", lambda _e: self.analyse())
        self.pick_btn = ttk.Button(bar, text="Выбрать…", command=self.choose_folder)
        self.pick_btn.grid(row=0, column=2)
        self.analyse_btn = ttk.Button(bar, text="Анализировать", command=self.analyse)
        self.analyse_btn.grid(row=0, column=3, padx=(px(6), 0))
        self.cancel_btn = ttk.Button(bar, text="Отмена", command=self.cancel)
        self.cancel_btn.grid(row=0, column=4, padx=(px(6), 0))
        self.progress = ttk.Progressbar(bar, length=px(150), maximum=100)
        self.progress.grid(row=0, column=5, padx=(px(10), 0))
        self.progress_var = tk.StringVar(value="")
        self._muted(ttk.Label(bar, textvariable=self.progress_var, width=24)).grid(
            row=0, column=6, sticky="w", padx=(px(6), 0))

        # -- строка 1: режим и итоги разбора --
        self.mode_var = tk.StringVar(
            value="Выберите папку с JPEG или CR2 и нажмите «Анализировать». "
                  "Файлы в папке только читаются.")
        self.mode_label = self._muted(ttk.Label(self, textvariable=self.mode_var,
                                                justify="left"))
        self.mode_label.grid(row=1, column=0, sticky="ew", pady=(px(4), px(4)))
        self.bind("<Configure>", self._on_resize, add="+")

        # -- строка 2: отметки, порядок, счётчик --
        tools = ttk.Frame(self)
        tools.grid(row=2, column=0, sticky="ew", pady=(0, px(6)))
        self.n_var = tk.StringVar(value=str(self._saved_n()))
        self.suggest_btn = ttk.Button(tools, command=self._on_suggest_click)
        self.suggest_btn.pack(side="left")
        self.n_spin = ttk.Spinbox(tools, from_=1, to=MAX_N, increment=1, width=5,
                                  textvariable=self.n_var)
        self.n_spin.pack(side="left", padx=(px(4), px(10)))
        self.n_var.trace_add("write", lambda *_a: self._on_n_changed())
        self._on_n_changed()
        self.clear_btn = ttk.Button(tools, text="Снять всё", command=self._on_clear_click)
        self.clear_btn.pack(side="left")
        ttk.Label(tools, text="Порядок:").pack(side="left", padx=(px(14), px(4)))
        self.sort_box = ttk.Combobox(tools, state="readonly", width=16,
                                     values=[SORT_LABELS[SORT_EVENT], SORT_LABELS[SORT_SCORE]])
        self.sort_box.set(SORT_LABELS[self.sort])
        self.sort_box.bind("<<ComboboxSelected>>", self._on_sort_selected)
        self.sort_box.pack(side="left")
        self.only_var = tk.BooleanVar(value=bool(self.settings.get("only_ticked", False)))
        ttk.Checkbutton(tools, text="только отмеченные", variable=self.only_var,
                        command=lambda: self.set_only_ticked(self.only_var.get())
                        ).pack(side="left", padx=(px(14), 0))
        self.counter_var = tk.StringVar(value="")
        ttk.Label(tools, textvariable=self.counter_var, font=self.bold_font
                  ).pack(side="right")

        # -- строка 3: контактный лист --
        sheet = ttk.Frame(self)
        sheet.grid(row=3, column=0, sticky="nsew")
        sheet.columnconfigure(0, weight=1)
        sheet.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(sheet, highlightthickness=0, bd=0, takefocus=1,
                                yscrollincrement=px(40), bg=self._theme_bg())
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar = ttk.Scrollbar(sheet, orient="vertical", command=self.canvas.yview)
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self._on_sheet_scrolled)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_sheet_click)
        self.canvas.bind("<Double-Button-1>", self._on_sheet_double)
        self.canvas.bind("<Motion>", self._on_sheet_motion)
        self.canvas.bind("<Leave>", lambda _e: self.canvas.configure(cursor=""))
        self.empty_item = self.canvas.create_text(
            px(SHEET_MARGIN), px(SHEET_MARGIN) + px(4), anchor="nw", text="",
            fill=self.color("muted"), tags=("empty",))

        # -- строка 4: раскрытая серия (скрыта, пока не нужна) --
        self.panel = ttk.Frame(self, padding=(0, px(8), 0, 0))
        self.panel.columnconfigure(0, weight=1)
        top = ttk.Frame(self.panel)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.panel_title = ttk.Label(top, text="", font=self.bold_font)
        self.panel_title.pack(side="left")
        self._muted(ttk.Label(top, text="  щелчок - сделать кадр главным, "
                                        "двойной щелчок - открыть оригинал")).pack(side="left")
        ttk.Button(top, text="Свернуть", command=self.collapse_burst).pack(side="right")
        self.panel_canvas = tk.Canvas(self.panel, highlightthickness=0, bd=0,
                                      height=self.box_h + px(90), bg=self._theme_bg(),
                                      xscrollincrement=px(40))
        self.panel_canvas.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        hbar = ttk.Scrollbar(self.panel, orient="horizontal",
                             command=self.panel_canvas.xview)
        hbar.grid(row=2, column=0, sticky="ew")
        self.panel_canvas.configure(xscrollcommand=hbar.set)
        self.panel_inner = ttk.Frame(self.panel_canvas)
        self.panel_canvas.create_window((0, 0), window=self.panel_inner, anchor="nw")
        self.panel_inner.bind("<Configure>", self._on_panel_inner_configure)

        # -- строка 5: копирование --
        out = ttk.Frame(self)
        out.grid(row=5, column=0, sticky="ew", pady=(px(8), 0))
        self.export_btn = ttk.Button(out, text="Скопировать отмеченные в папку…",
                                     command=self.choose_export_folder)
        self.export_btn.pack(side="left")
        self.with_raw_var = tk.BooleanVar(value=bool(self.settings.get("with_raw", False)))
        ttk.Checkbutton(out, text="вместе с парными CR2", variable=self.with_raw_var,
                        command=self._on_with_raw).pack(side="left", padx=(px(10), 0))
        self.open_export_btn = ttk.Button(out, text="Открыть папку",
                                          command=self._open_export_dir)
        self.open_export_btn.pack(side="right")
        self.export_var = tk.StringVar(value="Файлы копируются, оригиналы остаются на месте.")
        self._muted(ttk.Label(out, textvariable=self.export_var)).pack(
            side="left", padx=(px(10), 0), fill="x", expand=True)

        self._add_tag(self.canvas, _SCROLL_TAG)
        for w in (self.panel_canvas, self.panel_inner):
            self._add_tag(w, _STRIP_TAG)

    def _theme_bg(self) -> str:
        """Фон окна по теме ttk (на aqua это системный цвет, он меняется сам)."""
        try:
            color = ttk.Style(self).lookup("TFrame", "background")
            if color:
                self.winfo_rgb(color)
                return color
        except Exception:
            pass
        return gui_common.palette("bg", self)

    # ---------------- прокрутка колесом ----------------

    @staticmethod
    def _add_tag(widget: tk.Misc, tag: str) -> None:
        tags = widget.bindtags()
        if tag not in tags:
            widget.bindtags((tag,) + tuple(tags))

    def _install_scroll_bindings(self) -> None:
        for tag in (_SCROLL_TAG, _STRIP_TAG):
            self.bind_class(tag, "<MouseWheel>", _on_wheel)
            self.bind_class(tag, "<Shift-MouseWheel>", _on_wheel)
            self.bind_class(tag, "<Button-4>", _on_wheel)
            self.bind_class(tag, "<Button-5>", _on_wheel)

    # ---------------- настройки ----------------

    def _saved_n(self) -> int:
        try:
            return max(1, min(MAX_N, int(self.settings.get("n", DEFAULT_N))))
        except (TypeError, ValueError):
            return DEFAULT_N

    def n_value(self) -> int:
        """N из поля «сколько лучших» (1..999; мусор -> 30)."""
        try:
            return max(1, min(MAX_N, int(str(self.n_var.get()).strip())))
        except (TypeError, ValueError):
            return DEFAULT_N

    def _on_n_changed(self) -> None:
        n = self.n_value()
        self.suggest_btn.configure(text="Предложить %d %s" % (
            n, plural_ru(n, ("лучший", "лучших", "лучших"))))
        if "n" not in self.settings:
            self.settings["n"] = n                # первое построение: писать на диск нечего
        elif self.settings.get("n") != n:
            self.settings["n"] = n
            self._schedule_disk_save()

    def _on_with_raw(self) -> None:
        self.settings["with_raw"] = bool(self.with_raw_var.get())
        self._schedule_disk_save()

    def _schedule_disk_save(self) -> None:
        if self._save_after is not None:
            return
        try:
            self._save_after = self.after(SAVE_DELAY_MS, self._flush_disk_save)
        except (tk.TclError, RuntimeError):
            self._save_after = None

    def _flush_disk_save(self) -> None:
        self._save_after = None
        self.ctx.save_settings()

    def _save_state(self) -> None:
        """Отметки текущей папки -> settings["selections"][папка] (и на диск позже)."""
        if self.result is None or self.folder is None:
            return
        selections = self.settings.get("selections")
        if not isinstance(selections, dict):
            selections = self.settings["selections"] = {}
        key = folder_key(self.folder)
        images = self.result.images
        ticked = [images[self.rep[b]].path.name
                  for b in sorted(self.ticked, key=lambda b: self.rep[b])]
        reps = [images[o].path.name for b, o in sorted(self.rep.items())
                if b not in self.ticked and o != self.result.bursts[b].representative]
        if not ticked and not reps:
            selections.pop(key, None)
        else:
            selections[key] = {"folder": str(self.folder), "ticked": ticked,
                               "reps": reps, "saved_at": int(time.time())}
            if len(selections) > MAX_SAVED_FOLDERS:
                def age(k: str) -> float:
                    v = selections.get(k)
                    return float(v.get("saved_at", 0)) if isinstance(v, dict) else 0.0
                for old in sorted(selections, key=age)[:len(selections) - MAX_SAVED_FOLDERS]:
                    if old != key:
                        selections.pop(old, None)
        self._schedule_disk_save()

    def _restore_state(self) -> int:
        """Вернуть отметки и выбранные главные кадры для папки.  Сколько серий отмечено."""
        if self.result is None or self.folder is None:
            return 0
        selections = self.settings.get("selections")
        entry = selections.get(folder_key(self.folder)) if isinstance(selections, dict) else None
        if not isinstance(entry, dict):
            return 0
        by_name = {r.path.name.lower(): r for r in self.result.images}

        def records(key: str) -> list[cull.ImageRecord]:
            names = entry.get(key)
            if not isinstance(names, list):
                return []
            return [by_name[n.lower()] for n in names
                    if isinstance(n, str) and n.lower() in by_name]

        for rec in records("reps"):
            self.rep[rec.burst_id] = rec.capture_order
        for rec in records("ticked"):
            self.rep[rec.burst_id] = rec.capture_order
            self.ticked.add(rec.burst_id)
        return len(self.ticked)

    # ---------------- папка и разбор ----------------

    def choose_folder(self) -> None:
        folder = gui_common.pick_folder(self.ctx, TAB_KEY, parent=self)
        if folder is not None:
            self.folder_var.set(str(folder))
            self.analyse(folder)

    def analyse(self, folder: str | os.PathLike[str] | None = None, *,
                use_faces: bool | None = None) -> gui_common.BackgroundJob | None:
        """Запустить cull.scan() по папке в фоне.  None - не запущено (и сказано почему)."""
        if self._job is not None:
            return None
        text = str(folder) if folder is not None else self.folder_var.get().strip()
        if not text:
            self._warn("Отбор", "Сначала выберите папку со снимками.")
            return None
        target = Path(os.path.normpath(os.path.abspath(text)))
        if not target.is_dir():
            self._warn("Отбор", "Папка не найдена:\n%s" % target)
            return None
        self.folder_var.set(str(target))
        box = (self.box_w, self.box_h)

        def work(report: gui_common.Reporter) -> _Prepared | None:
            report(0.0, "Ищу снимки…")

            def on_file(done: int, total: int) -> None:
                report(done / total if total else None, "Кадр %d из %d" % (done, total))

            res = cull.scan(target, report=on_file, cancel_event=report.cancel_event,
                            use_faces=use_faces, thumb_px=max(box))
            if report.cancelled:
                return None
            report(1.0, "Подбираю порядок…")
            for rec in res.images:
                if rec.thumbnail is not None:
                    rec.thumbnail = fit_thumbnail(rec.thumbnail, box[0], box[1])
            order = score_order_for(res)
            return _Prepared(res, target, order)

        try:
            job = self.ctx.run_background(
                work, on_progress=self._on_progress, on_done=self._on_scan_done,
                on_error=self._on_scan_error, on_cancelled=self._on_scan_cancelled,
                name="cull-scan")
        except RuntimeError:
            return None
        self._job, self._job_kind = job, "scan"
        self.progress["value"] = 0
        self.progress_var.set("Разбор…")
        self.ctx.log("Отбор: разбираю папку %s" % target)
        self._update_controls()
        return job

    def cancel(self) -> None:
        """Остановить разбор или копирование."""
        if self._job is not None:
            self._job.cancel()
            self.progress_var.set("Останавливаю…")

    def _on_progress(self, fraction: float | None, text: str) -> None:
        if fraction is not None:
            self.progress["value"] = round(fraction * 100)
        if text:
            self.progress_var.set(text)

    def _job_finished(self) -> None:
        self._job, self._job_kind = None, ""
        self._update_controls()

    def _on_scan_done(self, prep: _Prepared | None) -> None:
        self._job_finished()
        if prep is None:
            self._on_scan_cancelled(None)
            return
        self.progress["value"] = 100
        self.progress_var.set("Готово за %s с" % ("%.1f" % prep.result.elapsed_s).replace(".", ","))
        self.settings["folder"] = str(prep.folder)
        self.set_result(prep.result, folder=prep.folder, score_order=prep.score_order)
        res = prep.result
        if res.images:
            self.ctx.log("Отбор: %d кадров, %d %s, режим «%s»" % (
                len(res.images), len(res.bursts),
                plural_ru(len(res.bursts), ("серия", "серии", "серий")),
                self.mode_name(res)), "ok")
        else:
            self.ctx.log("Отбор: в папке нет JPEG или CR2", "warn")

    def _on_scan_error(self, exc: BaseException) -> None:
        self._job_finished()
        self.progress["value"] = 0
        self.progress_var.set("Ошибка разбора")
        self.ctx.show_error("Отбор: разбор папки", exc,
                            "Не удалось разобрать папку: %s" % exc)

    def _on_scan_cancelled(self, _value: Any) -> None:
        self._job_finished()
        self.progress["value"] = 0
        self.progress_var.set("Разбор отменён")
        self.ctx.log("Отбор: разбор отменён", "warn")

    @staticmethod
    def mode_name(result: cull.CullResult) -> str:
        """«с лицами» / «без лиц»."""
        return "с лицами" if result.mode == cull.MODE_FACES else "без лиц"

    def _mode_text(self) -> str:
        res = self.result
        if res is None:
            return ""
        nb, ns = len(res.bursts), len(res.segments)
        parts = ["Режим: %s." % self.mode_name(res),
                 "%d %s · %d %s · %d %s · %s с." % (
                     len(res.images), plural_ru(len(res.images), ("кадр", "кадра", "кадров")),
                     nb, plural_ru(nb, ("серия", "серии", "серий")),
                     ns, plural_ru(ns, ("фрагмент", "фрагмента", "фрагментов")),
                     ("%.1f" % res.elapsed_s).replace(".", ","))]
        if res.time_basis:
            parts.append("Порядок: %s." % res.time_basis)
        parts.extend(n for n in res.notes if n)
        return " ".join(parts)

    # ---------------- результат и карточки ----------------

    def set_result(self, result: cull.CullResult, folder: str | os.PathLike[str] | None = None,
                   *, score_order: list[int] | None = None) -> None:
        """Показать результат разбора: карточки, восстановленные отметки, выбор."""
        self._gen += 1
        for attr in ("_build_after", "_layout_after", "_photo_after"):
            self._cancel_after(attr)
        if self._thumb_job is not None:
            self._thumb_job.cancel()
        self._thumb_queue.clear()
        self._thumb_inflight.clear()
        self.collapse_burst()
        self.canvas.delete("card", "hdr")
        self.cards.clear()
        self.headers.clear()
        self._photos.clear()
        self._pil.clear()
        self._thumb_failed.clear()
        self._refresh_colors()

        self.result = result
        self.folder = Path(folder) if folder is not None else common_folder(result)
        self.score_order = score_order
        self._places = sharpness_places(result)
        self.rep = {b.id: b.representative for b in result.bursts}
        self.ticked = set()
        for rec in result.images:
            if rec.error:
                self._thumb_failed[rec.capture_order] = rec.error
            elif (rec.thumbnail is not None and rec.thumbnail.width <= self.box_w
                  and rec.thumbnail.height <= self.box_h):
                self._pil[rec.capture_order] = rec.thumbnail
        restored = self._restore_state()

        visible = [i for k, i in self._items() if k == "burst"]
        seen = set(visible)
        self._pending_build = visible + [b.id for b in result.bursts if b.id not in seen]
        self.mode_var.set(self._mode_text())
        self.canvas.yview_moveto(0)
        self._layout_now()
        self._build_step()
        self._publish_selection()
        self._update_counter()
        self._update_controls()
        if restored:
            self.ctx.log("Отбор: восстановлены отметки этой папки - %d %s" % (
                restored, plural_ru(restored, ("серия", "серии", "серий"))), "ok")

    @property
    def is_building(self) -> bool:
        """Ещё не все карточки построены."""
        return bool(self._pending_build)

    @property
    def thumbs_pending(self) -> bool:
        """Миниатюры ещё читаются или ещё не обёрнуты в PhotoImage."""
        return (bool(self._thumb_queue) or self._thumb_job is not None
                or self._photo_after is not None)

    def _build_step(self) -> None:
        self._build_after = None
        if self.result is None:
            return
        made = 0
        while self._pending_build and made < BUILD_BATCH:
            bid = self._pending_build.pop(0)
            if bid not in self.cards:
                self._make_card(bid)
                made += 1
        self._layout_now()
        if self._pending_build:
            try:
                self._build_after = self.after(STEP_MS, self._build_step)
            except (tk.TclError, RuntimeError):
                self._build_after = None

    def _card_width(self) -> int:
        return self.box_w + 2 * self.ctx.px(CARD_PAD)

    def _make_card(self, bid: int) -> _Card:
        card = _Card(bid, "b%d" % bid)
        self.cards[bid] = card
        self._draw_card(card)
        return card

    def _bottom(self, item: int, default: float) -> float:
        box = self.canvas.bbox(item)
        return float(box[3]) if box else default

    def _draw_card(self, card: _Card) -> None:
        """(Пере)рисовать карточку в её текущем углу (card.x, card.y)."""
        assert self.result is not None
        c, px, res = self.canvas, self.ctx.px, self.result
        c.delete(card.tag)
        order = self.rep[card.burst_id]
        rec = res.images[order]
        burst = res.bursts[card.burst_id]
        card.order = order
        card.items, card.chips = {}, []

        def tags(*extra: str) -> tuple[str, ...]:
            return ("card", card.tag) + extra

        x0, y0 = card.x, card.y
        pad, width = px(CARD_PAD), self._card_width()
        items = card.items
        items["bg"] = c.create_rectangle(x0, y0, x0 + width, y0 + 1, tags=tags("bg"))
        cx, cy = x0 + width // 2, y0 + pad + self.box_h // 2
        items["thumb"] = c.create_image(cx, cy, anchor="center", tags=tags("thumb"))
        items["ph"] = c.create_text(cx, cy, anchor="center", text="", font=self.small_font,
                                    tags=tags("thumb", "ph"))

        # флажок: квадрат и галочка рисуются, поэтому растут с масштабом экрана
        y = y0 + pad + self.box_h + px(6)
        cb = px(14)
        bx = x0 + pad
        items["box"] = c.create_rectangle(bx, y, bx + cb, y + cb, width=max(1, px(1)),
                                          tags=tags("chk", "box"))
        items["mark"] = c.create_line(bx + cb * 0.22, y + cb * 0.52, bx + cb * 0.43, y + cb * 0.74,
                                      bx + cb * 0.80, y + cb * 0.28, width=max(2, px(2)),
                                      capstyle="round", joinstyle="round",
                                      tags=tags("chk", "mark"))
        name_w = self.box_w - cb - px(6)
        items["name"] = c.create_text(bx + cb + px(6), y + cb / 2, anchor="w",
                                      text=_elide(rec.path.name, self.base_font, name_w),
                                      font=self.base_font, tags=tags("chk", "name"))
        y = max(y + cb, self._bottom(items["name"], y + cb)) + px(3)

        meta = ["фр. %d" % (rec.segment_id + 1)]
        if not rec.error:
            meta.append(sharpness_text(self._places.get(order)))
        if order != burst.representative:
            meta.append("выбран вручную")
        items["meta"] = c.create_text(bx, y, anchor="nw", width=self.box_w, text=" · ".join(meta),
                                      font=self.small_font, tags=tags("meta"))
        y = self._bottom(items["meta"], y) + px(1)
        items["link"] = c.create_text(
            bx, y, anchor="nw", font=self.small_font,
            text=burst_text(burst.size, self.expanded == card.burst_id),
            tags=tags("link") if burst.size > 1 else tags("single"))
        y = self._bottom(items["link"], y)

        if rec.flags:
            y += px(4)
            chip_h = self.small_font.metrics("linespace") + px(2)
            used = 0
            for flag in rec.flags:
                text, role = FLAG_CHIPS.get(flag, (cull.FLAG_LABELS.get(flag, flag), "warn"))
                w = self.small_font.measure(text) + 2 * px(5)
                if used and used + w > self.box_w:
                    used, y = 0, y + chip_h + px(3)
                rx = bx + used
                rect = c.create_rectangle(rx, y, rx + w, y + chip_h, width=max(1, px(1)),
                                          tags=tags("chip"))
                label = c.create_text(rx + w / 2, y + chip_h / 2, anchor="center", text=text,
                                      font=self.small_font, tags=tags("chip"))
                card.chips.append((rect, label, role))
                used += w + px(4)
            y += chip_h
        card.height = int(round(y - y0)) + pad
        c.coords(items["bg"], x0, y0, x0 + width, y0 + card.height)
        self._set_card_thumb(card, load=False)
        self._style_card(card)
        if not card.shown:
            c.itemconfigure(card.tag, state="hidden")

    def _set_card_thumb(self, card: _Card, load: bool = True) -> None:
        """Картинка карточки.  load=False - только уже готовый PhotoImage."""
        order = card.order
        photo = self._photo_for(order) if load else self._photos.get(order)
        c = self.canvas
        c.itemconfigure(card.items["thumb"], image=photo if photo is not None else "")
        if photo is not None:
            text = ""
        else:
            text = "не читается" if order in self._thumb_failed else "загрузка…"
        c.itemconfigure(card.items["ph"], text=text)

    def _set_label_thumb(self, label: tk.Label, order: int, priority: bool = False) -> None:
        photo = self._photo_for(order, priority=priority)
        if photo is not None:
            label.configure(image=photo, text="")
        else:
            label.configure(image=self._blank,
                            text="не читается" if order in self._thumb_failed else "загрузка…")

    def card_text(self, bid: int, part: str) -> str:
        """Текст элемента карточки: "name", "meta", "link" или "ph"."""
        return str(self.canvas.itemcget(self.cards[bid].items[part], "text"))

    def _style_card(self, card: _Card) -> None:
        """Цвета карточки из palette(): отмеченная - с заливкой и толстой рамкой."""
        c, col, px = self.canvas, self.color, self.ctx.px
        ticked = card.burst_id in self.ticked
        items = card.items
        c.itemconfigure(items["bg"], fill=col("selection") if ticked else col("card_bg"),
                        outline=col("accent") if ticked else col("card_border"),
                        width=px(3) if ticked else max(1, px(1)))
        c.itemconfigure(items["box"], fill=col("accent") if ticked else col("card_bg"),
                        outline=col("accent") if ticked else col("muted"))
        c.itemconfigure(items["mark"], fill=col("card_bg"),
                        state="normal" if (ticked and card.shown) else "hidden")
        c.itemconfigure(items["name"], fill=col("fg"))
        c.itemconfigure(items["meta"], fill=col("muted"))
        c.itemconfigure(items["ph"], fill=col("muted"))
        multi = self.result is not None and self.result.bursts[card.burst_id].size > 1
        c.itemconfigure(items["link"], fill=col("accent") if multi else col("muted"))
        for rect, label, role in card.chips:
            color = col(role)
            c.itemconfigure(rect, outline=color, fill="")
            c.itemconfigure(label, fill=color)

    def _update_link(self, bid: int | None) -> None:
        if bid is None or self.result is None or bid not in self.cards:
            return
        self.canvas.itemconfigure(
            self.cards[bid].items["link"],
            text=burst_text(self.result.bursts[bid].size, self.expanded == bid))

    def _recolor(self) -> None:
        """Перекрасить всё под текущую тему (светлая / тёмная)."""
        self._refresh_colors()
        bg = self._theme_bg()
        for canvas in (self.canvas, self.panel_canvas):
            try:
                canvas.configure(bg=bg)
            except tk.TclError:
                pass
        muted = self.color("muted")
        for lbl in self._muted_labels:
            try:
                lbl.configure(foreground=muted)
            except tk.TclError:
                pass
        self.canvas.itemconfigure("hdr", fill=self.color("fg"))
        self.canvas.itemconfigure(self.empty_item, fill=muted)
        for card in self.cards.values():
            self._style_card(card)
        if self.expanded is not None:
            self._build_panel()

    # ---------------- щелчки по листу ----------------

    def card_at(self, x: float, y: float) -> _Card | None:
        """Карточка под точкой холста (координаты холста, а не окна)."""
        width = self._card_width()
        for card in self.cards.values():
            if card.shown and card.contains(x, y, width):
                return card
        return None

    def hit_test(self, x: float, y: float) -> tuple[_Card | None, str]:
        """Что под точкой холста: (карточка, "thumb" | "chk" | "link" | "card")."""
        card = self.card_at(x, y)
        if card is None:
            return None, ""
        c = self.canvas
        for part in ("chk", "link"):
            for item in c.find_overlapping(x - 1, y - 1, x + 1, y + 1):
                tags = c.gettags(item)
                if card.tag in tags and part in tags:
                    return card, part
        if y - card.y <= self.ctx.px(CARD_PAD) + self.box_h:
            return card, "thumb"
        return card, "card"

    def _event_point(self, event: Any) -> tuple[float, float]:
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _on_sheet_click(self, event: Any) -> None:
        card, part = self.hit_test(*self._event_point(event))
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass
        if card is None:
            return
        if part == "chk":
            self.toggle(card.burst_id)
        elif part == "link":
            self._on_link(card.burst_id)

    def _on_sheet_double(self, event: Any) -> None:
        card, part = self.hit_test(*self._event_point(event))
        if card is None:
            return
        if part == "chk":
            self.toggle(card.burst_id)          # второй щелчок двойного - тоже щелчок
        elif part in ("thumb", "card"):
            self.open_original(card.order)

    def _on_sheet_motion(self, event: Any) -> None:
        _card, part = self.hit_test(*self._event_point(event))
        cursor = "hand2" if part in ("chk", "link") else ""
        if str(self.canvas.cget("cursor")) != cursor:
            self.canvas.configure(cursor=cursor)

    def _on_sheet_scrolled(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        self._schedule_photos()

    # ---------------- раскладка ----------------

    def _items(self) -> list[tuple[str, int]]:
        if self.result is not None and self.sort == SORT_SCORE and self.score_order is None:
            self.score_order = score_order_for(self.result)
        return layout_items(self.result, sort=self.sort, ticked=self.ticked,
                            only_ticked=bool(self.only_var.get()),
                            score_order=self.score_order)

    def _columns_for(self, width: int) -> int:
        px = self.ctx.px
        return max(1, (width - 2 * px(SHEET_MARGIN) + px(CARD_GAP))
                   // (self._card_width() + px(CARD_GAP)))

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfigure(self.empty_item,
                                  width=max(100, event.width - 2 * self.ctx.px(SHEET_MARGIN)))
        cols = self._columns_for(event.width)
        if cols != self._cols:
            self._cols = cols
            self._schedule_layout()
        else:
            self._schedule_photos()             # окно стало выше - видно больше карточек

    def _on_resize(self, event: Any) -> None:
        if event.widget is self:
            self.mode_label.configure(wraplength=max(200, event.width - self.ctx.px(20)))

    def _schedule_layout(self) -> None:
        if self._layout_after is not None:
            return
        try:
            self._layout_after = self.after(LAYOUT_DELAY_MS, self._layout_now)
        except (tk.TclError, RuntimeError):
            self._layout_after = None

    def _place_card(self, card: _Card, x: int, y: int) -> None:
        if not card.shown:
            card.shown = True
            self.canvas.itemconfigure(card.tag, state="normal")
            self._style_card(card)              # галочка неотмеченной снова прячется
        if (x, y) != (card.x, card.y):
            self.canvas.move(card.tag, x - card.x, y - card.y)
            card.x, card.y = x, y

    def _hide_card(self, card: _Card) -> None:
        if card.shown:
            card.shown = False
            self.canvas.itemconfigure(card.tag, state="hidden")

    def _layout_now(self) -> None:
        """Разложить заголовки и карточки по сетке.  Только движение элементов холста."""
        self._cancel_after("_layout_after")
        c, px = self.canvas, self.ctx.px
        items = self._items()
        cols, margin, gap = self._cols, px(SHEET_MARGIN), px(CARD_GAP)
        step = self._card_width() + gap
        header_h = self.bold_font.metrics("linespace") + px(6)
        y, col, row_h = margin, 0, 0
        shown_cards: set[int] = set()
        shown_headers: set[int] = set()
        for kind, ident in items:
            if kind == "segment":
                if col:
                    y, col, row_h = y + row_h + gap, 0, 0
                if shown_cards or shown_headers:
                    y += px(8)
                hdr = self._header(ident)
                c.coords(hdr, margin, y)
                c.itemconfigure(hdr, state="normal")
                shown_headers.add(ident)
                y += header_h
                continue
            card = self.cards.get(ident)
            if card is None:
                continue                       # ещё не построена
            self._place_card(card, margin + col * step, y)
            shown_cards.add(ident)
            row_h = max(row_h, card.height)
            col += 1
            if col >= cols:
                y, col, row_h = y + row_h + gap, 0, 0
        if col:
            y += row_h + gap
        for bid, card in self.cards.items():
            if bid not in shown_cards:
                self._hide_card(card)
        for sid, hdr in self.headers.items():
            if sid not in shown_headers:
                c.itemconfigure(hdr, state="hidden")
        if items:
            c.itemconfigure(self.empty_item, state="hidden", text="")
        else:
            if self.result is None:
                text = "Здесь появится контактный лист: по карточке на каждую серию кадров."
            elif not self.result.images:
                text = "В папке нет снимков JPEG или CR2."
            else:
                text = ("Ничего не отмечено. Снимите флажок «только отмеченные», "
                        "чтобы увидеть все серии.")
            c.itemconfigure(self.empty_item, state="normal", text=text)
            y = margin + 3 * self.base_font.metrics("linespace")
        width = max(c.winfo_width(), cols * step + 2 * margin)
        c.configure(scrollregion=(0, 0, width, max(y + margin, c.winfo_height(), 1)))
        self._schedule_photos()

    def _header(self, sid: int) -> int:
        hdr = self.headers.get(sid)
        if hdr is None:
            hdr = self.canvas.create_text(0, 0, anchor="nw", font=self.bold_font,
                                          fill=self.color("fg"), tags=("hdr", "s%d" % sid))
            self.headers[sid] = hdr
            self._update_header(sid)
        return hdr

    def _update_header(self, sid: int) -> None:
        hdr = self.headers.get(sid)
        if hdr is None or self.result is None:
            return
        seg = self.result.segments[sid]
        n = sum(1 for b in seg.burst_ids if b in self.ticked)
        self.canvas.itemconfigure(hdr, text=segment_title(seg, n))

    def header_text(self, sid: int) -> str:
        """Текст заголовка фрагмента на листе."""
        return str(self.canvas.itemcget(self.headers[sid], "text"))

    def empty_text(self) -> str:
        """Подсказка пустого листа ("" - лист не пуст)."""
        return str(self.canvas.itemcget(self.empty_item, "text"))

    def visible_burst_ids(self) -> list[int]:
        """Серии, которые сейчас разложены на листе, по порядку."""
        return [i for k, i in self._items() if k == "burst" and i in self.cards]

    def set_sort(self, sort: str) -> None:
        """SORT_EVENT - по ходу события, SORT_SCORE - по оценке."""
        if sort not in SORT_LABELS:
            raise ValueError("неизвестный порядок: %r" % (sort,))
        self.sort = sort
        self.settings["sort"] = sort
        self.sort_box.set(SORT_LABELS[sort])
        self._schedule_disk_save()
        self.canvas.yview_moveto(0)
        self._layout_now()

    def _on_sort_selected(self, _event: Any = None) -> None:
        label = self.sort_box.get()
        for key, text in SORT_LABELS.items():
            if text == label:
                self.set_sort(key)
                return

    def set_only_ticked(self, flag: bool) -> None:
        """Показывать только отмеченные серии."""
        self.only_var.set(bool(flag))
        self.settings["only_ticked"] = bool(flag)
        self._schedule_disk_save()
        self.canvas.yview_moveto(0)
        self._layout_now()

    # ---------------- отметки ----------------

    def set_ticked(self, bid: int, on: bool) -> None:
        """Отметить серию (её текущий главный кадр) или снять отметку."""
        if self.result is None or not 0 <= bid < len(self.result.bursts):
            return
        was = bid in self.ticked
        if on:
            self.ticked.add(bid)
        else:
            self.ticked.discard(bid)
        card = self.cards.get(bid)
        if card is not None:
            self._style_card(card)
        if was != bool(on):
            self._selection_changed(relayout=True)

    def is_ticked(self, bid: int) -> bool:
        return bid in self.ticked

    def toggle(self, bid: int) -> None:
        self.set_ticked(bid, bid not in self.ticked)

    def clear_ticks(self) -> None:
        """Снять все отметки."""
        if not self.ticked:
            return
        self._replace_ticks(set())

    def _replace_ticks(self, bids: set[int]) -> None:
        old, self.ticked = self.ticked, set(bids)
        for bid in old ^ self.ticked:
            card = self.cards.get(bid)
            if card is not None:
                self._style_card(card)
        self._selection_changed(relayout=True)

    def _selection_changed(self, relayout: bool = False) -> None:
        self._update_counter()
        if self.result is not None:
            for sid in self.headers:
                self._update_header(sid)
        self._publish_selection()
        self._save_state()
        self._update_controls()
        if relayout and self.only_var.get():
            self._schedule_layout()

    def selected_records(self) -> list[cull.ImageRecord]:
        """Отмеченные кадры (главные кадры отмеченных серий) в порядке съёмки."""
        if self.result is None:
            return []
        return [self.result.images[o] for o in sorted(self.rep[b] for b in self.ticked)]

    def selected_paths(self) -> list[Path]:
        return [r.path for r in self.selected_records()]

    def _publish_selection(self) -> None:
        records = self.selected_records()
        # Лицо, найденное при разборе, пригодится «Афишам»: кадр сразу
        # кадрируется по лицу, и голову не режет.
        self.ctx.set_photo_hints({r.path: {"face_box": r.face_box} for r in records
                                  if r.face_box is not None})
        self.ctx.set_selection([r.path for r in records])

    def _update_counter(self) -> None:
        total = len(self.result.bursts) if self.result is not None else 0
        self.counter_var.set(counter_text(len(self.ticked), total))

    def _on_clear_click(self) -> None:
        n = len(self.ticked)
        if n and n > 3 and not messagebox.askyesno(
                "Снять всё", "Снять отметки со всех серий (%d)?" % n, parent=self):
            return
        self.clear_ticks()

    def _on_suggest_click(self) -> None:
        n = self.n_value()
        if self.ticked and not messagebox.askyesno(
                "Предложить лучшие",
                "Сейчас отмечено серий: %d.\nЗаменить отметки предложением из %d %s?"
                % (len(self.ticked), n, plural_ru(n, ("кадра", "кадров", "кадров"))),
                parent=self):
            return
        self.suggest_best(n)

    def suggest_best(self, n: int | None = None) -> gui_common.BackgroundJob | None:
        """Подобрать cull.suggest(result, n) в фоне и отметить эти серии."""
        if self.result is None or self._suggest_job is not None:
            return None
        count = self.n_value() if n is None else max(0, int(n))
        res, gen = self.result, self._gen

        def done(records: list[cull.ImageRecord]) -> None:
            self._suggest_job = None
            self._update_controls()
            if gen == self._gen:
                self.apply_suggestion(records, requested=count)

        def failed(exc: BaseException) -> None:
            self._suggest_job = None
            self._update_controls()
            self.ctx.show_error("Отбор: предложение", exc,
                                "Не удалось подобрать лучшие кадры: %s" % exc)

        def cancelled(_value: Any) -> None:
            self._suggest_job = None
            self._update_controls()

        try:
            self._suggest_job = self.ctx.run_background(
                lambda _report: cull.suggest(res, count), on_done=done,
                on_error=failed, on_cancelled=cancelled, name="cull-suggest")
        except RuntimeError:
            return None
        self._update_controls()
        return self._suggest_job

    def apply_suggestion(self, records: Iterable[cull.ImageRecord],
                         requested: int | None = None) -> None:
        """Отметить серии предложенных кадров (прежние отметки заменяются)."""
        if self.result is None:
            return
        bids = {r.burst_id for r in records if 0 <= r.burst_id < len(self.result.bursts)}
        self._replace_ticks(bids)
        text = "Отбор: отмечено %d %s" % (len(bids), plural_ru(len(bids), ("серия", "серии", "серий")))
        if requested is not None and requested > len(bids):
            text += " (всего серий в съёмке: %d)" % len(self.result.bursts)
        self.ctx.log(text, "ok")

    # ---------------- серия: раскрыть и сменить главный кадр ----------------

    def _on_link(self, bid: int) -> None:
        if self.result is None or self.result.bursts[bid].size <= 1:
            return
        if self.expanded == bid:
            self.collapse_burst()
        else:
            self.expand_burst(bid)

    def expand_burst(self, bid: int) -> None:
        """Показать все кадры серии под листом."""
        if self.result is None or not 0 <= bid < len(self.result.bursts):
            return
        previous = self.expanded
        self.expanded = bid
        for b in (previous, bid):
            self._update_link(b)
        self._build_panel()
        self.panel.grid(row=4, column=0, sticky="ew")
        self.panel_canvas.xview_moveto(0)

    def collapse_burst(self) -> None:
        """Спрятать ленту серии."""
        previous, self.expanded = self.expanded, None
        for w in self.panel_inner.winfo_children():
            w.destroy()
        self._panel_members.clear()
        self.panel.grid_remove()
        self._update_link(previous)

    def panel_orders(self) -> list[int]:
        """capture_order кадров в раскрытой серии (пусто, если ничего не раскрыто)."""
        return list(self._panel_members)

    def _build_panel(self) -> None:
        for w in self.panel_inner.winfo_children():
            w.destroy()
        self._panel_members.clear()
        res, bid = self.result, self.expanded
        if res is None or bid is None:
            return
        px = self.ctx.px
        dark = gui_common.is_dark_mode(self)

        def pal(role: str) -> str:
            return gui_common.palette(role, dark=dark)

        members = res.burst_members(bid)
        seg = res.segments[res.bursts[bid].segment_id]
        self.panel_title.configure(text="Серия из %d %s · фрагмент %d" % (
            len(members), plural_ru(len(members), ("кадра", "кадров", "кадров")), seg.id + 1))
        bg = pal("card_bg")
        for col, rec in enumerate(members):
            current = rec.capture_order == self.rep[bid]
            border = pal("accent") if current else pal("card_border")
            frame = tk.Frame(self.panel_inner, bg=bg, bd=0, highlightthickness=px(2),
                             highlightbackground=border, highlightcolor=border,
                             padx=px(6), pady=px(6))
            frame.grid(row=0, column=col, padx=px(4), pady=px(2), sticky="n")
            thumb = tk.Label(frame, image=self._blank, width=self.box_w, height=self.box_h,
                             compound="center", bd=0, bg=bg, fg=pal("muted"),
                             font=self.small_font, cursor="hand2")
            thumb.pack()
            self._set_label_thumb(thumb, rec.capture_order, priority=True)
            thumb.bind("<Button-1>", lambda _e, o=rec.capture_order: self.set_representative(bid, o))
            thumb.bind("<Double-Button-1>", lambda _e, o=rec.capture_order: self.open_original(o))
            name = tk.Label(frame, text=rec.path.name, anchor="w", bg=bg, fg=pal("fg"), bd=0)
            name.pack(fill="x", pady=(px(4), 0))
            info = "лучший в серии" if rec.burst_rank == 1 else "%d-й по оценке" % rec.burst_rank
            if not rec.error:
                info += " · " + sharpness_text(self._places.get(rec.capture_order))
            if rec.flags:
                info += " · " + ", ".join(FLAG_CHIPS.get(f, (cull.FLAG_LABELS.get(f, f), ""))[0]
                                          for f in rec.flags)
            meta = tk.Label(frame, text=info, anchor="w", justify="left", bg=bg,
                            fg=pal("warn") if rec.flags else pal("muted"),
                            font=self.small_font, wraplength=self.box_w, bd=0)
            meta.pack(fill="x")
            widgets: list[tk.Misc] = [frame, thumb, name, meta]
            if current:
                mark = tk.Label(frame, text="главный кадр серии", anchor="w", bg=bg,
                                fg=pal("ok"), font=self.small_font, bd=0)
                mark.pack(fill="x", pady=(px(2), 0))
                widgets.append(mark)
            else:
                btn = ttk.Button(frame, text="Сделать главным",
                                 command=lambda o=rec.capture_order: self.set_representative(bid, o))
                btn.pack(anchor="w", pady=(px(2), 0))
                widgets.append(btn)
            for w in widgets:
                self._add_tag(w, _STRIP_TAG)
            self._panel_members[rec.capture_order] = thumb

    def _on_panel_inner_configure(self, event: Any) -> None:
        self.panel_canvas.configure(scrollregion=self.panel_canvas.bbox("all"),
                                    height=max(event.height, 1))

    def set_representative(self, bid: int, order: int) -> None:
        """Сделать кадр order главным в серии bid (он и попадёт в выбор)."""
        if self.result is None or not 0 <= bid < len(self.result.bursts):
            return
        if order not in self.result.bursts[bid].members:
            raise ValueError("кадр %d не входит в серию %d" % (order, bid))
        if self.rep.get(bid) == order:
            return
        self.rep[bid] = order
        card = self.cards.get(bid)
        if card is not None:
            height = card.height
            self._draw_card(card)
            if card.height != height:
                self._layout_now()
        if self.expanded == bid:
            self._build_panel()
        if bid in self.ticked:
            self._publish_selection()
        self._save_state()

    # ---------------- миниатюры ----------------

    def _photo_for(self, order: int, priority: bool = False) -> Any:
        """PhotoImage кадра или None (тогда чтение поставлено в очередь)."""
        photo = self._photos.get(order)
        if photo is not None:
            return photo
        pil = self._pil.pop(order, None)
        if pil is not None:
            try:
                photo = gui_common.photo_image(pil, master=self)
            except Exception as exc:                     # noqa: BLE001
                self._thumb_failed[order] = str(exc)
                return None
            self._photos[order] = photo
            return photo
        if order in self._thumb_failed or order in self._thumb_inflight or self.result is None:
            return None
        if order in self._thumb_queue:
            if priority:
                self._thumb_queue.remove(order)
                self._thumb_queue.insert(0, order)
        elif priority:
            self._thumb_queue.insert(0, order)
        else:
            self._thumb_queue.append(order)
        self._pump_thumbs()
        return None

    def _pump_thumbs(self) -> None:
        if self._thumb_job is not None or not self._thumb_queue or self.result is None:
            return
        if self.ctx.closing:
            return
        batch = self._thumb_queue[:THUMB_BATCH]
        del self._thumb_queue[:THUMB_BATCH]
        self._thumb_inflight.update(batch)
        items = [(o, self.result.images[o].path) for o in batch]
        box, gen = (self.box_w, self.box_h), self._gen

        def done(out: list[tuple[int, Any, str]]) -> None:
            self._thumb_job = None
            self._thumb_inflight.difference_update(batch)
            if gen == self._gen:
                for order, pil, err in out:
                    if pil is not None:
                        self._pil[order] = pil
                    elif err:
                        self._thumb_failed[order] = err
                    else:
                        continue
                    self._thumb_arrived(order)
            self._pump_thumbs()

        def failed(exc: BaseException) -> None:
            self._thumb_job = None
            self._thumb_inflight.difference_update(batch)
            self.ctx.record_error("Отбор: миниатюры", exc)
            if gen == self._gen:
                for order in batch:
                    self._thumb_failed[order] = str(exc)
                    self._thumb_arrived(order)
            self._pump_thumbs()

        def cancelled(_value: Any) -> None:
            self._thumb_job = None
            self._thumb_inflight.difference_update(batch)
            if gen == self._gen:
                self._thumb_queue[:0] = [o for o in batch if o not in self._thumb_queue]
            self._pump_thumbs()

        try:
            self._thumb_job = self.ctx.run_background(
                lambda report: _load_thumbs(items, box, report), on_done=done,
                on_error=failed, on_cancelled=cancelled, name="cull-thumbs")
        except RuntimeError:
            self._thumb_job = None

    def _schedule_photos(self) -> None:
        if self._photo_after is not None or self.result is None or self.ctx.closing:
            return
        try:
            self._photo_after = self.after(STEP_MS, self._photo_step)
        except (tk.TclError, RuntimeError):
            self._photo_after = None

    def _photo_step(self) -> None:
        """Обернуть очередную пачку миниатюр в PhotoImage: сначала видимые карточки.

        PIL-миниатюры готовит рабочий поток (или сам разбор), а PhotoImage
        создаётся здесь, в потоке Tk, пачками по PHOTO_BATCH за тик.
        """
        self._photo_after = None
        if self.result is None:
            return
        c = self.canvas
        top = c.canvasy(0)
        view = max(c.winfo_height(), self.box_h)
        near_top, near_bottom = top - view, top + 2 * view
        queued = set(self._thumb_queue) | self._thumb_inflight
        todo: list[tuple[int, int, int, _Card]] = []
        for card in self.cards.values():
            o = card.order
            if (not card.shown or o in self._photos or o in self._thumb_failed
                    or o in queued):
                continue
            near = card.y + card.height >= near_top and card.y <= near_bottom
            todo.append((0 if near else 1, card.y, card.x, card))
        todo.sort(key=lambda t: t[:3])
        for _near, _y, _x, card in todo[:PHOTO_BATCH]:
            self._set_card_thumb(card)
        if len(todo) > PHOTO_BATCH:
            self._schedule_photos()

    def _thumb_arrived(self, order: int) -> None:
        for card in self.cards.values():
            if card.order == order:
                self._set_card_thumb(card)
        label = self._panel_members.get(order)
        if label is not None:
            self._set_label_thumb(label, order)

    # ---------------- открыть оригинал ----------------

    def open_original(self, order: int) -> None:
        """Открыть кадр в просмотрщике системы; не вышло - показать его папку."""
        if self.result is None or not 0 <= order < len(self.result.images):
            return
        path = self.result.images[order].path
        try:
            open_in_viewer(path)
        except Exception as exc:                         # noqa: BLE001
            self.ctx.log("Не удалось открыть %s (%s), открываю папку" % (path.name, exc), "warn")
            self.ctx.reveal(path)

    # ---------------- копирование ----------------

    def export_sources(self, with_raw: bool | None = None) -> list[Path]:
        """Файлы для копирования: отмеченные кадры (и их CR2, если with_raw)."""
        if with_raw is None:
            with_raw = bool(self.with_raw_var.get())
        out: list[Path] = []
        for rec in self.selected_records():
            out.append(rec.path)
            if with_raw and rec.pair_path is not None:
                out.append(rec.pair_path)
        return out

    def choose_export_folder(self) -> None:
        if not self.ticked:
            self._warn("Копирование", "Сначала отметьте кадры.", info=True)
            return
        out = gui_common.pick_folder(self.ctx, EXPORT_DIALOG_KEY, parent=self,
                                     title="Куда скопировать отмеченные кадры")
        if out is not None:
            self.export_to(out)

    def export_to(self, out_dir: str | os.PathLike[str], *, with_raw: bool | None = None,
                  interactive: bool = True) -> gui_common.BackgroundJob | None:
        """Скопировать отмеченные кадры в out_dir в фоне.  None - не запущено."""
        if self._job is not None:
            return None
        sources = self.export_sources(with_raw)
        if not sources:
            self.export_var.set("Нечего копировать: ничего не отмечено.")
            return None
        problem = export_problem(out_dir, sources, self.folder)
        if problem:
            self.export_var.set(problem.split("\n", 1)[0])
            if interactive:
                self._warn("Копирование", problem)
            return None
        target = Path(out_dir)

        def work(report: gui_common.Reporter) -> list[cull.ExportItem]:
            def on_file(done: int, total: int) -> None:
                report(done / total if total else None, "Копирую %d из %d" % (done, total))
            return cull.export_selection(sources, target, "copy",
                                         cancel_event=report.cancel_event, report=on_file)

        def done(items: list[cull.ExportItem]) -> None:
            self._job_finished()
            self._on_export_done(target, items, cancelled=False)

        def failed(exc: BaseException) -> None:
            self._job_finished()
            self.progress["value"] = 0
            self.progress_var.set("Ошибка копирования")
            self.export_var.set("Копирование не удалось: %s" % exc)
            self.ctx.show_error("Отбор: копирование", exc, "Не удалось скопировать: %s" % exc)

        def cancelled(items: Any) -> None:
            self._job_finished()
            self._on_export_done(target, list(items or []), cancelled=True)

        try:
            job = self.ctx.run_background(work, on_progress=self._on_progress, on_done=done,
                                          on_error=failed, on_cancelled=cancelled,
                                          name="cull-export")
        except RuntimeError:
            return None
        self._job, self._job_kind = job, "export"
        self.progress["value"] = 0
        self.progress_var.set("Копирование…")
        self.export_var.set("Копирую в %s…" % target)
        self._update_controls()
        return job

    def _on_export_done(self, target: Path, items: list[cull.ExportItem],
                        cancelled: bool) -> None:
        copied = [i for i in items if i.dst is not None]
        renamed = [i for i in copied if i.renamed]
        errors = [i for i in items if i.error]
        n = len(copied)
        text = "%s %d %s в %s" % ("Остановлено: скопировано" if cancelled else "Скопировано",
                                   n, plural_ru(n, ("файл", "файла", "файлов")), target)
        if renamed:
            text += "; переименовано (имя было занято): %d" % len(renamed)
        if errors:
            text += "; ошибок: %d" % len(errors)
            self.ctx.record_error("Отбор: копирование",
                                  "\n".join("%s: %s" % (i.src, i.error) for i in errors))
        level = "warn" if (errors or cancelled) else "ok"
        self.progress["value"] = 100 if not cancelled else 0
        self.progress_var.set("Копирование остановлено" if cancelled else "Скопировано")
        self.export_var.set(text)
        self._last_export_dir = target
        self.settings["export_dir"] = str(target)
        self._schedule_disk_save()
        self._update_controls()
        self.ctx.log("Отбор: " + text[0].lower() + text[1:], level)

    def _open_export_dir(self) -> None:
        if self._last_export_dir is not None:
            self.ctx.reveal(self._last_export_dir)

    # ---------------- состояние кнопок, окна, закрытие ----------------

    @staticmethod
    def _enable(widget: ttk.Widget, on: bool) -> None:
        widget.state(["!disabled"] if on else ["disabled"])

    def _update_controls(self) -> None:
        busy = self._job is not None
        has_result = self.result is not None and bool(self.result.bursts)
        self._enable(self.analyse_btn, not busy)
        self._enable(self.pick_btn, not busy)
        self._enable(self.cancel_btn, busy)
        self._enable(self.suggest_btn, has_result and self._suggest_job is None
                     and self._job_kind != "scan")
        self._enable(self.clear_btn, bool(self.ticked))
        self._enable(self.export_btn, bool(self.ticked) and not busy)
        self._enable(self.open_export_btn, self._last_export_dir is not None)

    def _warn(self, title: str, text: str, info: bool = False) -> None:
        try:
            (messagebox.showinfo if info else messagebox.showwarning)(title, text, parent=self)
        except tk.TclError:
            pass

    def _cancel_after(self, attr: str) -> None:
        after_id = getattr(self, attr)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            setattr(self, attr, None)

    def _on_shutdown(self) -> None:
        for attr in ("_build_after", "_layout_after", "_save_after", "_photo_after"):
            self._cancel_after(attr)
        self._pending_build.clear()
        self._thumb_queue.clear()

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self:
            return
        self._on_shutdown()
        for job in (self._job, self._thumb_job, self._suggest_job):
            if job is not None:
                job.cancel()
        try:
            self._unregister_shutdown()
        except Exception:
            pass


def _on_wheel(event: Any) -> str | None:
    """Колесо мыши над листом или лентой серии (общий обработчик тегов)."""
    widget = event.widget
    tab = widget
    while tab is not None and not isinstance(tab, CullTab):
        tab = getattr(tab, "master", None)
    if tab is None:
        return None
    strip = _STRIP_TAG in widget.bindtags()
    canvas = tab.panel_canvas if strip else tab.canvas
    num = getattr(event, "num", None)
    if num == 4:
        steps = -1
    elif num == 5:
        steps = 1
    else:
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return None
        steps = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
    try:
        if strip:
            if canvas.xview() != (0.0, 1.0):
                canvas.xview_scroll(steps * 2, "units")
        elif canvas.yview() != (0.0, 1.0):
            canvas.yview_scroll(steps * 2, "units")
    except tk.TclError:
        return None
    return "break"


def build_tab(parent: ttk.Notebook, ctx: gui_common.AppContext) -> ttk.Frame:
    """Контракт вкладки: фрейм - потомок parent; в блокнот его добавит оболочка."""
    return CullTab(parent, ctx)


if __name__ == "__main__":
    sys.exit(gui_common.run_standalone(sys.modules[__name__]))
