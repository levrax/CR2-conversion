# -*- coding: utf-8 -*-
"""tab_enhance - вкладка «Обработка»: автоулучшение и фильтры.

Что делает вкладка
------------------
* Источник: папка, отдельные файлы (JPEG / PNG / TIFF / CR2) или кадры,
  отмеченные во вкладке «Отбор» (ctx.selection).  Пара RAW+JPEG одного кадра
  (IMG_0001.CR2 и IMG_0001.JPG в одной папке) обрабатывается один раз - по
  JPEG, как и в «Отборе»; CR2 берётся, только если JPEG рядом нет.
* Список снимков.  Щелчок по снимку показывает живой предпросмотр «до / после»:
  шторкой (перетаскивается мышью) или двумя кадрами рядом.  Предпросмотр
  считается в рабочем потоке в размере холста, с задержкой DEBOUNCE_MS после
  последнего движения ползунка и со счётчиком поколений: устаревший результат
  выбрасывается, одновременно считается не больше одного предпросмотра, и
  интерфейс не замирает, пока ползунок тянут.
* Настройки: сила автоулучшения, баланс белого, сочность цвета, фильтр из
  реестра enhance.FILTERS и его сила.  Пресеты - именованные наборы настроек.
* «Обработать всё»: enhance.process_many в рабочем потоке через
  ctx.run_background, с прогрессом, отменой, списком итогов по файлам и
  кнопкой «Открыть папку результата».  Папка результата по умолчанию - рядом
  с папкой съёмки («<папка> — обработка»), а не внутри неё: папку съёмки
  программа только читает.  Запись в саму папку снимков или внутрь неё - только
  после явного подтверждения; исходные файлы не перезаписываются никогда (это
  гарантирует и сам enhance).  Суффикс имени входит в план имён, так что файл
  сразу пишется под окончательным именем.  Готовые файлы уходят в
  ctx.publish_processed - «Афиши» берут обработанный кадр вместо оригинала.

Потоки - по контракту gui_common: рабочий поток получает снимок параметров
(пути, числа, enhance.Params) и возвращает PIL.Image и FileResult; PhotoImage
создаёт только поток Tk и держит на него ссылку в полях вкладки.

Настройки (ctx.tab_settings("enhance")):
    enhance_strength  0..100        filter_id        id из enhance.FILTERS
    filter_strength   0..100        white_balance    bool
    vibrance          bool          view             "split" | "side"
    out_dir           папка результата, выбранная пользователем ("" - рядом
                      с папкой съёмки, см. default_out_dir)
    quality           качество JPEG  suffix          суффикс имени файла
    keep_exif         bool          presets          {имя: {поле Params: значение}}
    last_dir          последняя папка снимков (pick_folder / pick_files)
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cr2_core
import enhance
import gui_common as gc

__all__ = ["TAB_TITLE", "build_tab", "EnhanceTab", "clean_suffix",
           "default_out_dir", "params_from_settings", "params_to_settings",
           "prefer_jpeg", "same_folder", "inside_folder"]

TAB_TITLE = "Обработка"
TAB_KEY = "enhance"

DEBOUNCE_MS = 150               # пауза после движения ползунка до пересчёта
PREVIEW_LOAD_SIDE = 1400        # исходник для предпросмотра, px по длинной стороне
PREVIEW_MIN_BOX = (640, 420)    # если холст ещё не показан, логические px
PREVIEW_GAP = 8                 # промежуток между «до» и «после» в режиме «рядом»
SOURCE_CACHE = 6                # столько исходников предпросмотра держим в памяти
DEFAULT_QUALITY = 92
QUALITY_MIN, QUALITY_MAX = 60, 100
SUFFIX_MAX = 40
#: Папка результата по умолчанию: «<папка съёмки> — обработка» рядом с ней.
DEFAULT_OUT_SUFFIX = " — обработка"
_JPEG_EXTS = frozenset({".jpg", ".jpeg"})

VIEW_SPLIT = "split"
VIEW_SIDE = "side"
_VIEW_TITLES = {VIEW_SPLIT: "Шторка", VIEW_SIDE: "Рядом"}

_DEFAULTS = enhance.Params()
# Запрещённые в именах файлов символы (Windows строже всех - берём её набор).
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TREE_STYLE = "EnhanceTab.Treeview"
_PREVIEW_ERRORS = (gc.ImageLoadError, OSError, ValueError, cr2_core.Cr2Error)


# --------------------------------------------------------------------------
# Без Tk: имена, настройки, перемещение файла
# --------------------------------------------------------------------------


def clean_suffix(text: str) -> str:
    """Суффикс имени файла без запрещённых символов, пробелов по краям и точек.

    «_обр» -> «_обр»; « a/b: » -> «ab».  Длина не больше SUFFIX_MAX.
    """
    s = _BAD_NAME_CHARS.sub("", str(text or "")).strip()
    # Windows не любит точки и пробелы в конце имени; точка в начале суффикса
    # превратила бы его в «второе расширение».
    s = s.strip(". ")
    return s[:SUFFIX_MAX]


def default_out_dir(sources: Iterable[str | Path]) -> Path | None:
    """Папка результата по умолчанию: «<папка первого снимка> — обработка» РЯДОМ с ней.

    Не внутри: папку съёмки программа только читает (так же решает «Отбор»),
    а подпапка к тому же уезжала бы в облако вместе с архивом съёмки.  Снимки в
    корне диска - папки по умолчанию нет (None), пользователь выберет сам.
    """
    for p in sources:
        folder = Path(os.path.abspath(str(Path(p).parent)))
        if not folder.name or folder.parent == folder:
            return None
        return folder.parent / (folder.name + DEFAULT_OUT_SUFFIX)
    return None


def prefer_jpeg(paths: Iterable[str | Path]) -> tuple[list[Path], int]:
    """Пары RAW+JPEG - по JPEG: (список без CR2, у которых рядом есть JPEG, сколько убрано).

    Камера снимает RAW+JPEG, и без этого папка давала бы каждый кадр дважды:
    вдвое дольше пакет и пары почти одинаковых файлов в результате.  Пара -
    та же папка и то же имя без расширения (без учёта регистра).  Порядок
    остальных снимков не меняется.
    """
    items = [Path(p) for p in paths]
    jpeg_keys = {(gc.path_key(p.parent), p.stem.lower()) for p in items
                 if p.suffix.lower() in _JPEG_EXTS}
    kept = [p for p in items if not (p.suffix.lower() in gc.RAW_EXTENSIONS
                                     and (gc.path_key(p.parent), p.stem.lower()) in jpeg_keys)]
    return kept, len(items) - len(kept)


def same_folder(a: str | Path, b: str | Path) -> bool:
    """Одна ли это папка (с учётом регистра Windows и ссылок, если папки есть)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return (os.path.normcase(os.path.abspath(str(a)))
                == os.path.normcase(os.path.abspath(str(b))))


def inside_folder(child: str | Path, folder: str | Path) -> bool:
    """child - это folder или папка внутри неё (пути могут ещё не существовать)."""
    c = os.path.normcase(os.path.realpath(os.path.abspath(str(child))))
    f = os.path.normcase(os.path.realpath(os.path.abspath(str(folder))))
    try:
        return os.path.commonpath([c, f]) == f
    except ValueError:                          # разные диски Windows
        return False


def params_to_settings(p: enhance.Params) -> dict[str, Any]:
    """Params -> словарь, переживающий JSON (силы в процентах 0..100)."""
    return {
        "enhance_strength": int(round(p.enhance_strength * 100)),
        "filter_id": p.filter_id,
        "filter_strength": int(round(p.filter_strength * 100)),
        "white_balance": bool(p.white_balance),
        "vibrance": bool(p.vibrance),
    }


def params_from_settings(data: Mapping[str, Any] | None) -> enhance.Params:
    """Словарь из настроек -> Params.  Мусор и неизвестный фильтр не роняют вкладку.

    Настройки пишет и старая, и будущая версия программы: всё, что не
    распознано, заменяется значением по умолчанию движка.
    """
    d = data if isinstance(data, Mapping) else {}

    def pct(key: str, default: float) -> float:
        try:
            v = float(d.get(key, default * 100))
        except (TypeError, ValueError):
            return default
        if v != v:                                  # NaN
            return default
        return max(0.0, min(100.0, v)) / 100.0

    fid = d.get("filter_id", _DEFAULTS.filter_id)
    if not isinstance(fid, str) or fid not in enhance.FILTERS:
        fid = _DEFAULTS.filter_id
    wb = d.get("white_balance", _DEFAULTS.white_balance)
    vib = d.get("vibrance", _DEFAULTS.vibrance)
    return enhance.Params(
        enhance_strength=pct("enhance_strength", _DEFAULTS.enhance_strength),
        filter_id=fid,
        filter_strength=pct("filter_strength", _DEFAULTS.filter_strength),
        white_balance=wb if isinstance(wb, bool) else _DEFAULTS.white_balance,
        vibrance=vib if isinstance(vib, bool) else _DEFAULTS.vibrance,
    ).validated()


def _describe_result(r: enhance.FileResult) -> tuple[str, str]:
    """(короткий итог, роль цвета) для строки списка результатов."""
    if r.ok:
        return "готово", "ok"
    if r.skipped and r.message == "Отменено":
        return "отменено", "muted"
    if r.skipped:
        return "пропущен", "warn"
    return "ошибка", "error"


# --------------------------------------------------------------------------
# Вкладка
# --------------------------------------------------------------------------


class EnhanceTab:
    """Состояние и виджеты вкладки «Обработка».  Живёт в потоке Tk.

    Для тестов и соседних вкладок открыты: set_sources, load_folder,
    take_selection, show, params, set_params, request_preview, set_view,
    set_split, save_preset, apply_preset, delete_preset, preset_names,
    start_batch, cancel_batch, open_result_folder и поля sources, current,
    generation, shown_generation, dropped_previews, preview_before,
    preview_after, preview_error, batch_results, batch_workers.
    """

    def __init__(self, parent: tk.Misc, ctx: gc.AppContext) -> None:
        self.ctx = ctx
        self.st = ctx.tab_settings(TAB_KEY)
        if not isinstance(self.st.get("presets"), dict):
            self.st["presets"] = {}
        self.frame = ttk.Frame(parent)
        self.frame.enhance_tab = self            # type: ignore[attr-defined]

        # источник
        self.sources: list[Path] = []
        self.current: Path | None = None
        self._out_auto = not bool(str(self.st.get("out_dir") or "").strip())

        # предпросмотр
        self.generation = 0
        self.shown_generation = -1
        self.dropped_previews = 0
        self.preview_before = None               # PIL «до» последнего предпросмотра
        self.preview_after = None                # PIL «после»
        self.preview_error = ""
        self._before_tk = None                   # PhotoImage: держим ссылки,
        self._after_tk = None                    # иначе сборщик мусора сотрёт
        self._split_tk = None                    # картинку с холста
        self._split_geom: tuple[int, int, int, int] | None = None
        self._preview_job: gc.BackgroundJob | None = None
        self._debounce_id: str | None = None
        self._resize_id: str | None = None
        self._rendered_box = (0, 0)
        self._cache: "OrderedDict[tuple, Any]" = OrderedDict()
        self._cache_lock = threading.Lock()

        # пакетная обработка
        self._batch_job: gc.BackgroundJob | None = None
        self._batch_q: "queue.Queue[enhance.FileResult]" = queue.Queue()
        self._batch_out: Path | None = None
        self._batch_total = 0
        self._batch_t0 = 0.0
        self.batch_results: list[enhance.FileResult] = []
        self.last_out_dir: Path | None = None
        #: Потоков у process_many (None - по умолчанию движка).
        self.batch_workers: int | None = None

        self._suppress = False
        self._suppress_out = False
        #: Роль цвета у строк состояния - чтобы перекрасить их при смене темы.
        self._note_roles: dict[str, str] = {}
        self._titles = dict(enhance.filter_choices())          # id -> title
        self._ids_by_title = {t: i for i, t in self._titles.items()}

        self._build_vars()
        self._build()

        self._unsubs: list[Callable[[], None]] = [
            ctx.subscribe(gc.TOPIC_SELECTION, self._on_selection_changed),
            ctx.register_shutdown(self._shutdown),
        ]
        self.frame.bind("<Destroy>", self._on_destroy, add="+")
        self.frame.bind("<<ThemeChanged>>", self._on_theme_changed, add="+")
        self._on_selection_changed(ctx.selection)
        self._refresh_presets()
        self._sync_param_widgets()
        self._update_buttons()
        self._draw_preview()

    # ------------------------------------------------------------------
    # Переменные и построение интерфейса
    # ------------------------------------------------------------------

    def _color(self, role: str) -> str:
        return gc.palette(role, self.frame)

    def _theme_bg(self) -> str:
        try:
            return ttk.Style(self.frame).lookup(".", "background") or self._color("bg")
        except tk.TclError:
            return self._color("bg")

    def _build_vars(self) -> None:
        st = self.st
        p = params_from_settings(st)
        self.enhance_var = tk.DoubleVar(value=round(p.enhance_strength * 100))
        self.filter_strength_var = tk.DoubleVar(value=round(p.filter_strength * 100))
        self.filter_var = tk.StringVar(value=self._titles[p.filter_id])
        self.wb_var = tk.BooleanVar(value=p.white_balance)
        self.vib_var = tk.BooleanVar(value=p.vibrance)
        self.enhance_text = tk.StringVar()
        self.filter_strength_text = tk.StringVar()
        self.filter_desc_var = tk.StringVar()

        view = st.get("view")
        self.view_var = tk.StringVar(value=view if view in _VIEW_TITLES else VIEW_SPLIT)
        self.split_var = tk.DoubleVar(value=0.5)

        self.out_var = tk.StringVar(value=str(st.get("out_dir") or ""))
        q = st.get("quality", DEFAULT_QUALITY)
        q = q if isinstance(q, int) and QUALITY_MIN <= q <= QUALITY_MAX else DEFAULT_QUALITY
        self.quality_var = tk.StringVar(value=str(q))
        self.suffix_var = tk.StringVar(value=clean_suffix(st.get("suffix") or ""))
        ke = st.get("keep_exif", True)
        self.exif_var = tk.BooleanVar(value=ke if isinstance(ke, bool) else True)
        self.preset_var = tk.StringVar()
        self.source_text = tk.StringVar(value="Снимки не выбраны")
        self.take_text = tk.StringVar(value="Взять отмеченные в «Отборе»")

        for var in (self.enhance_var, self.filter_strength_var, self.wb_var, self.vib_var):
            var.trace_add("write", lambda *_a: self._on_param_change())
        self.split_var.trace_add("write", lambda *_a: self._update_split())
        self.out_var.trace_add("write", lambda *_a: self._on_out_typed())
        for var in (self.quality_var, self.suffix_var, self.exif_var):
            var.trace_add("write", lambda *_a: self._store_output_settings())

    def _build(self) -> None:
        px = self.ctx.px
        f = self.frame
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)

        # ---- строка источника ----
        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", padx=px(8), pady=(px(8), px(4)))
        ttk.Button(bar, text="Папка…", command=self.choose_folder).pack(side="left")
        ttk.Button(bar, text="Файлы…", command=self.choose_files).pack(side="left", padx=(px(4), 0))
        self.take_btn = ttk.Button(bar, textvariable=self.take_text, command=self.take_selection)
        self.take_btn.pack(side="left", padx=(px(4), 0))
        self.source_label = ttk.Label(bar, textvariable=self.source_text,
                                      foreground=self._color("muted"))
        self.source_label.pack(side="left", padx=(px(10), 0), fill="x", expand=True)

        # ---- левая колонка: список снимков ----
        left = ttk.Frame(f)
        left.grid(row=1, column=0, rowspan=2, sticky="nsw", padx=(px(8), px(4)), pady=(0, px(8)))
        left.rowconfigure(0, weight=1)
        self._style_tables()
        self.tree = ttk.Treeview(left, columns=("name",), show="headings",
                                 selectmode="browse", style=_TREE_STYLE)
        self.tree.heading("name", text="Снимки")
        self.tree.column("name", width=px(180), stretch=True)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="ns")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ---- центр: предпросмотр ----
        center = ttk.Frame(f)
        center.grid(row=1, column=1, sticky="nsew", padx=px(4))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)
        top = ttk.Frame(center)
        top.grid(row=0, column=0, sticky="ew", pady=(0, px(4)))
        ttk.Label(top, text="Сравнение:").pack(side="left")
        for key in (VIEW_SPLIT, VIEW_SIDE):
            ttk.Radiobutton(top, text=_VIEW_TITLES[key], value=key, variable=self.view_var,
                            command=self._on_view_change).pack(side="left", padx=(px(6), 0))
        self.preview_status = ttk.Label(top, text="", foreground=self._color("muted"))
        self.preview_status.pack(side="right")
        self.canvas = tk.Canvas(center, highlightthickness=0, borderwidth=0,
                                background=self._theme_bg(),
                                width=px(PREVIEW_MIN_BOX[0]), height=px(PREVIEW_MIN_BOX[1]))
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_canvas_drag)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.split_scale = ttk.Scale(center, from_=0.0, to=1.0, orient="horizontal",
                                     variable=self.split_var)
        self.split_scale.grid(row=2, column=0, sticky="ew", pady=(px(4), 0))
        self.caption = ttk.Label(center, text="", foreground=self._color("muted"))
        self.caption.grid(row=3, column=0, sticky="w", pady=(px(2), 0))

        # ---- центр внизу: пакетная обработка ----
        batch = ttk.LabelFrame(f, text="Пакетная обработка", padding=px(6))
        batch.grid(row=2, column=1, sticky="nsew", padx=px(4), pady=(px(6), px(8)))
        batch.columnconfigure(3, weight=1)
        self.run_btn = ttk.Button(batch, text="Обработать всё", command=self.start_batch)
        self.run_btn.grid(row=0, column=0, sticky="w")
        self.cancel_btn = ttk.Button(batch, text="Отмена", command=self.cancel_batch)
        self.cancel_btn.grid(row=0, column=1, sticky="w", padx=(px(4), 0))
        self.open_btn = ttk.Button(batch, text="Открыть папку результата",
                                   command=self.open_result_folder)
        self.open_btn.grid(row=0, column=2, sticky="w", padx=(px(4), 0))
        self.progress = ttk.Progressbar(batch, orient="horizontal", mode="determinate",
                                        maximum=100.0)
        self.progress.grid(row=0, column=3, sticky="ew", padx=(px(8), 0))
        self.batch_status = ttk.Label(batch, text="", foreground=self._color("muted"))
        self.batch_status.grid(row=1, column=0, columnspan=4, sticky="w", pady=(px(4), px(2)))
        res = ttk.Frame(batch)
        res.grid(row=2, column=0, columnspan=4, sticky="nsew")
        res.columnconfigure(0, weight=1)
        self.results = ttk.Treeview(res, columns=("file", "result", "message"),
                                    show="headings", height=5, selectmode="browse",
                                    style=_TREE_STYLE)
        for col, title, width, stretch in (("file", "Файл", 150, False),
                                           ("result", "Итог", 70, False),
                                           ("message", "Подробности", 280, True)):
            self.results.heading(col, text=title)
            self.results.column(col, width=px(width), stretch=stretch)
        rsb = ttk.Scrollbar(res, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=rsb.set)
        self.results.grid(row=0, column=0, sticky="nsew")
        rsb.grid(row=0, column=1, sticky="ns")
        self.results.bind("<Double-1>", self._on_result_activate)
        self._color_result_tags()

        # ---- правая колонка: настройки ----
        right = ttk.Frame(f)
        right.grid(row=1, column=2, rowspan=2, sticky="nse", padx=(px(4), px(8)), pady=(0, px(8)))
        right.columnconfigure(0, weight=1)
        wrap = px(240)

        box = ttk.LabelFrame(right, text="Автоулучшение", padding=px(6))
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self.enhance_scale = ttk.Scale(box, from_=0, to=100, orient="horizontal",
                                       variable=self.enhance_var)
        self.enhance_scale.grid(row=0, column=0, sticky="ew")
        ttk.Label(box, textvariable=self.enhance_text, width=6, anchor="e").grid(
            row=0, column=1, sticky="e", padx=(px(4), 0))
        ttk.Checkbutton(box, text="Баланс белого", variable=self.wb_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        ttk.Checkbutton(box, text="Сочность цвета (кожа защищена)", variable=self.vib_var).grid(
            row=2, column=0, columnspan=2, sticky="w")

        box = ttk.LabelFrame(right, text="Фильтр", padding=px(6))
        box.grid(row=1, column=0, sticky="ew", pady=(px(6), 0))
        box.columnconfigure(0, weight=1)
        self.filter_combo = ttk.Combobox(box, textvariable=self.filter_var, state="readonly",
                                         values=[t for _i, t in enhance.filter_choices()])
        self.filter_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_param_change())
        self.filter_desc = ttk.Label(box, textvariable=self.filter_desc_var,
                                     foreground=self._color("muted"), wraplength=wrap,
                                     justify="left")
        self.filter_desc.grid(row=1, column=0, columnspan=2, sticky="w", pady=(px(2), px(4)))
        ttk.Label(box, text="Сила фильтра").grid(row=2, column=0, columnspan=2, sticky="w")
        self.filter_scale = ttk.Scale(box, from_=0, to=100, orient="horizontal",
                                      variable=self.filter_strength_var)
        self.filter_scale.grid(row=3, column=0, sticky="ew")
        ttk.Label(box, textvariable=self.filter_strength_text, width=6, anchor="e").grid(
            row=3, column=1, sticky="e", padx=(px(4), 0))
        ttk.Button(box, text="Сбросить настройки", command=self.reset_params).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(px(6), 0))

        box = ttk.LabelFrame(right, text="Пресеты", padding=px(6))
        box.grid(row=2, column=0, sticky="ew", pady=(px(6), 0))
        box.columnconfigure(0, weight=1)
        self.preset_combo = ttk.Combobox(box, textvariable=self.preset_var)
        self.preset_combo.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>",
                               lambda _e: self.apply_preset(self.preset_var.get()))
        btns = ttk.Frame(box)
        btns.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        ttk.Button(btns, text="Применить",
                   command=lambda: self.apply_preset(self.preset_var.get())).pack(side="left")
        ttk.Button(btns, text="Сохранить",
                   command=lambda: self.save_preset(self.preset_var.get())).pack(
            side="left", padx=(px(4), 0))
        ttk.Button(btns, text="Удалить",
                   command=lambda: self.delete_preset(self.preset_var.get())).pack(
            side="left", padx=(px(4), 0))
        self.preset_status = ttk.Label(box, text="Впишите имя и нажмите «Сохранить»",
                                       foreground=self._color("muted"), wraplength=wrap,
                                       justify="left")
        self.preset_status.grid(row=2, column=0, sticky="w", pady=(px(4), 0))

        box = ttk.LabelFrame(right, text="Сохранение", padding=px(6))
        box.grid(row=3, column=0, sticky="ew", pady=(px(6), 0))
        box.columnconfigure(0, weight=1)
        ttk.Label(box, text="Папка результата").grid(row=0, column=0, columnspan=2, sticky="w")
        self.out_entry = ttk.Entry(box, textvariable=self.out_var)
        self.out_entry.grid(row=1, column=0, sticky="ew")
        ttk.Button(box, text="…", width=3, command=self.choose_out_dir).grid(
            row=1, column=1, sticky="e", padx=(px(4), 0))
        self.out_hint = ttk.Label(box, text="", foreground=self._color("muted"),
                                  wraplength=wrap, justify="left")
        self.out_hint.grid(row=2, column=0, columnspan=2, sticky="w")
        row = ttk.Frame(box)
        row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(px(4), 0))
        ttk.Label(row, text="Качество JPEG").pack(side="left")
        self.quality_spin = ttk.Spinbox(row, from_=QUALITY_MIN, to=QUALITY_MAX, increment=1,
                                        width=5, textvariable=self.quality_var)
        self.quality_spin.pack(side="left", padx=(px(6), 0))
        row = ttk.Frame(box)
        row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(px(4), 0))
        ttk.Label(row, text="Суффикс имени").pack(side="left")
        ttk.Entry(row, textvariable=self.suffix_var, width=12).pack(side="left", padx=(px(6), 0))
        ttk.Checkbutton(box, text="Сохранять EXIF", variable=self.exif_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
        self._update_out_hint()

    def _style_tables(self) -> None:
        """Высота строк таблиц - по шрифту, как в конвертере (cr2_gui).

        Собственный стиль, а не общий «Treeview»: вкладка не меняет вид чужих
        таблиц, но и не обрезает текст, если оболочка высоту не настроила
        (запуск вкладки отдельно, крупный системный шрифт, 200 % DPI).
        """
        px = self.ctx.px
        style = ttk.Style(self.frame)
        try:
            from tkinter import font as tkfont
            line = tkfont.nametofont("TkDefaultFont", self.frame).metrics("linespace")
            style.configure(_TREE_STYLE, rowheight=max(px(22), int(line) + px(4)))
        except Exception:
            try:
                style.configure(_TREE_STYLE, rowheight=px(22))
            except tk.TclError:
                pass

    def _color_result_tags(self) -> None:
        for role in ("ok", "warn", "error", "muted"):
            self.results.tag_configure(role, foreground=self._color(role))

    def _on_theme_changed(self, _event: Any = None) -> None:
        try:
            self.canvas.configure(background=self._theme_bg())
            self._color_result_tags()
            for w, role in ((self.source_label, "muted"), (self.filter_desc, "muted"),
                            (self.caption, "muted"), (self.out_hint, "muted"),
                            (self.preview_status, "muted")):
                w.configure(foreground=self._color(role))
            for name, role in self._note_roles.items():
                getattr(self, name).configure(foreground=self._color(role))
        except tk.TclError:
            return
        self._draw_preview()

    # ------------------------------------------------------------------
    # Источник
    # ------------------------------------------------------------------

    def choose_folder(self) -> None:
        folder = gc.pick_folder(self.ctx, TAB_KEY, parent=self.frame)
        if folder is not None:
            self.load_folder(folder)

    def choose_files(self) -> None:
        paths = gc.pick_files(self.ctx, TAB_KEY, parent=self.frame)
        if paths:
            self.set_sources(paths, "Выбрано файлов: %d" % len(paths))

    def load_folder(self, folder: str | Path) -> int:
        """Взять все снимки папки (без подпапок).  Возвращает их число."""
        folder = Path(folder)
        paths = gc.list_images(folder)
        if not paths:
            self.source_text.set("В папке «%s» нет снимков JPEG, PNG, TIFF или CR2"
                                 % folder.name)
            self.source_label.configure(foreground=self._color("warn"))
            return 0
        kept, paired = prefer_jpeg(paths)
        label = "%s — снимков: %d" % (folder, len(kept))
        if paired:
            label += " (RAW+JPEG: взяты JPEG, CR2 пропущено: %d)" % paired
        return self.set_sources(kept, label)

    def take_selection(self) -> int:
        """Взять снимки, отмеченные во вкладке «Отбор»."""
        paths = self.ctx.selection
        if not paths:
            self.source_text.set("Во вкладке «Отбор» ничего не отмечено")
            self.source_label.configure(foreground=self._color("warn"))
            return 0
        return self.set_sources(paths, "Отмеченные в «Отборе»: %d" % len(paths))

    def set_sources(self, paths: Iterable[str | Path], label: str = "") -> int:
        """Заменить список снимков.  Не снимки, повторы и CR2 из пар RAW+JPEG
        отбрасываются (см. prefer_jpeg).

        Возвращает число снимков в списке; первый сразу показывается.
        """
        seen: set[str] = set()
        clean: list[Path] = []
        for raw in paths:
            p = Path(raw)
            if p.suffix.lower() not in gc.IMAGE_EXTENSIONS:
                continue
            key = gc.path_key(p)
            if key in seen:
                continue
            seen.add(key)
            clean.append(p)
        clean, paired = prefer_jpeg(clean)
        if paired and label:
            label += " (RAW+JPEG: взяты JPEG, CR2 пропущено: %d)" % paired
        self.sources = clean
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(clean):
            self.tree.insert("", "end", iid=str(i), values=(p.name,))
        self.tree.heading("name", text="Снимки (%d)" % len(clean))
        self.source_text.set(label or ("Снимков: %d" % len(clean) if clean
                                       else "Снимки не выбраны"))
        self.source_label.configure(foreground=self._color("muted"))
        if self._out_auto:
            auto = default_out_dir(clean)
            self._set_out(str(auto) if auto is not None else "", auto=True)
        self._update_out_hint()
        if clean:
            self.tree.selection_set("0")
            self.tree.see("0")
            self.show(clean[0])
        else:
            self.show(None)
        self._update_buttons()
        return len(clean)

    def _on_tree_select(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            path = self.sources[int(sel[0])]
        except (ValueError, IndexError):
            return
        if path != self.current:
            self.show(path)

    def show(self, path: str | Path | None) -> None:
        """Показать предпросмотр этого снимка (None - очистить)."""
        self.current = Path(path) if path is not None else None
        if self.current is None:
            self.preview_before = self.preview_after = None
            self.preview_error = ""
            self.generation += 1
            self.shown_generation = self.generation
            self._draw_preview()
            self.caption.configure(text="")
            return
        self.caption.configure(text=str(self.current))
        self.request_preview(immediate=True)

    def _on_selection_changed(self, paths: Any) -> None:
        n = len(paths or ())
        self.take_text.set("Взять отмеченные в «Отборе» (%d)" % n if n
                           else "Взять отмеченные в «Отборе»")
        try:
            self.take_btn.state(["!disabled"] if n else ["disabled"])
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Настройки обработки и пресеты
    # ------------------------------------------------------------------

    @staticmethod
    def _pct(var: tk.Variable, default: float) -> int:
        try:
            v = float(var.get())
        except (tk.TclError, ValueError, TypeError):
            return int(round(default * 100))
        return int(round(max(0.0, min(100.0, v))))

    @property
    def filter_id(self) -> str:
        return self._ids_by_title.get(self.filter_var.get(), _DEFAULTS.filter_id)

    def params(self) -> enhance.Params:
        """Текущие настройки обработки (проверенные)."""
        return enhance.Params(
            enhance_strength=self._pct(self.enhance_var, _DEFAULTS.enhance_strength) / 100.0,
            filter_id=self.filter_id,
            filter_strength=self._pct(self.filter_strength_var, _DEFAULTS.filter_strength) / 100.0,
            white_balance=bool(self.wb_var.get()),
            vibrance=bool(self.vib_var.get()),
        ).validated()

    def set_params(self, params: enhance.Params | Mapping[str, Any]) -> None:
        """Выставить настройки в интерфейсе (и перерисовать предпросмотр)."""
        p = enhance.Params.of(params)
        self._suppress = True
        try:
            self.enhance_var.set(round(p.enhance_strength * 100))
            self.filter_var.set(self._titles[p.filter_id])
            self.filter_strength_var.set(round(p.filter_strength * 100))
            self.wb_var.set(p.white_balance)
            self.vib_var.set(p.vibrance)
        finally:
            self._suppress = False
        self._on_param_change()

    def reset_params(self) -> None:
        self.set_params(enhance.Params())

    def _sync_param_widgets(self) -> None:
        p = self.params()
        self.enhance_text.set("%d %%" % round(p.enhance_strength * 100))
        self.filter_strength_text.set("%d %%" % round(p.filter_strength * 100))
        self.filter_desc_var.set(enhance.FILTERS[p.filter_id].description)
        try:
            self.filter_scale.state(["disabled"] if p.filter_id == "none" else ["!disabled"])
        except tk.TclError:
            pass

    def _on_param_change(self) -> None:
        if self._suppress:
            return
        self._sync_param_widgets()
        self.st.update(params_to_settings(self.params()))
        self.request_preview()

    def preset_names(self) -> list[str]:
        return sorted(self.st["presets"], key=lambda s: s.casefold())

    def _refresh_presets(self) -> None:
        self.preset_combo.configure(values=self.preset_names())

    def _preset_note(self, text: str, role: str = "muted") -> None:
        self._note_roles["preset_status"] = role
        self.preset_status.configure(text=text, foreground=self._color(role))

    def save_preset(self, name: str, *, overwrite: bool | None = None) -> bool:
        """Сохранить текущие настройки под именем.  True, если сохранено.

        overwrite=None - спросить, если имя уже занято.
        """
        name = str(name or "").strip()
        if not name:
            self._preset_note("Впишите имя пресета в поле выше", "warn")
            return False
        presets = self.st["presets"]
        if name in presets:
            if overwrite is None:
                overwrite = self._ask("Пресет уже есть",
                                      "Пресет «%s» уже есть. Заменить его текущими "
                                      "настройками?" % name)
            if not overwrite:
                return False
        p = self.params()
        presets[name] = params_to_settings(p)
        self.ctx.save_settings()
        self._refresh_presets()
        self.preset_var.set(name)
        self._preset_note("Сохранён «%s»: %s" % (name, p.describe()), "ok")
        return True

    def apply_preset(self, name: str) -> bool:
        """Применить пресет.  False, если такого нет."""
        name = str(name or "").strip()
        data = self.st["presets"].get(name)
        if not isinstance(data, dict):
            self._preset_note("Пресета «%s» нет" % name if name else "Выберите пресет", "warn")
            return False
        p = params_from_settings(data)
        self.set_params(p)
        self.preset_var.set(name)
        self._preset_note("Применён «%s»: %s" % (name, p.describe()), "ok")
        return True

    def delete_preset(self, name: str, *, confirm: bool | None = None) -> bool:
        """Удалить пресет.  confirm=None - спросить пользователя."""
        name = str(name or "").strip()
        presets = self.st["presets"]
        if name not in presets:
            self._preset_note("Пресета «%s» нет" % name if name else "Выберите пресет", "warn")
            return False
        if confirm is None:
            confirm = self._ask("Удалить пресет", "Удалить пресет «%s»?" % name)
        if not confirm:
            return False
        del presets[name]
        self.ctx.save_settings()
        self._refresh_presets()
        self.preset_var.set("")
        self._preset_note("Пресет «%s» удалён" % name)
        return True

    def _ask(self, title: str, text: str) -> bool:
        """Вопрос «да / нет».  Тесты подменяют этот метод."""
        return bool(messagebox.askyesno(title, text, parent=self.frame))

    # ------------------------------------------------------------------
    # Папка результата и параметры файла
    # ------------------------------------------------------------------

    def _set_out(self, value: str, auto: bool) -> None:
        self._suppress_out = True
        try:
            self.out_var.set(value)
        finally:
            self._suppress_out = False
        self._out_auto = auto
        self.st["out_dir"] = "" if auto else value
        self._update_out_hint()

    def _on_out_typed(self) -> None:
        if getattr(self, "_suppress_out", False):
            return
        value = self.out_var.get().strip()
        self._out_auto = not value
        self.st["out_dir"] = value
        self._update_out_hint()

    def _update_out_hint(self) -> None:
        if not hasattr(self, "out_hint"):
            return
        if self._out_auto and self.out_var.get().strip():
            text = "Папка рядом с папкой съёмки — снимки съёмки не трогаются"
        elif not self.out_var.get().strip():
            text = "Выберите папку, куда сохранить результат"
        else:
            text = ""
        self.out_hint.configure(text=text)

    def choose_out_dir(self) -> None:
        init = self.out_var.get().strip()
        if not init or not Path(init).is_dir():
            init = str(self.sources[0].parent) if self.sources else ""
        d = filedialog.askdirectory(parent=self.frame, title="Папка для обработанных снимков",
                                    initialdir=init or None, mustexist=False)
        if d:
            self._set_out(os.path.normpath(d), auto=False)

    def quality(self) -> int:
        """Качество JPEG из поля (QUALITY_MIN..QUALITY_MAX; мусор - DEFAULT_QUALITY)."""
        try:
            q = int(float(self.quality_var.get()))
        except (ValueError, tk.TclError):
            return DEFAULT_QUALITY
        return max(QUALITY_MIN, min(QUALITY_MAX, q))

    def _store_output_settings(self) -> None:
        self.st["quality"] = self.quality()
        self.st["suffix"] = clean_suffix(self.suffix_var.get())
        try:
            self.st["keep_exif"] = bool(self.exif_var.get())
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------

    def request_preview(self, immediate: bool = False) -> None:
        """Пересчитать предпросмотр (через DEBOUNCE_MS после последнего вызова)."""
        self.generation += 1
        if self._debounce_id is not None:
            try:
                self.frame.after_cancel(self._debounce_id)
            except tk.TclError:
                pass
            self._debounce_id = None
        if self.ctx.closing or self.current is None:
            return
        try:
            self._debounce_id = self.frame.after(1 if immediate else DEBOUNCE_MS,
                                                 self._on_debounce)
        except tk.TclError:
            self._debounce_id = None

    def _on_debounce(self) -> None:
        self._debounce_id = None
        self._kick_preview()

    def _canvas_box(self) -> tuple[int, int]:
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            w, h = self.ctx.px(PREVIEW_MIN_BOX[0]), self.ctx.px(PREVIEW_MIN_BOX[1])
        return w, h

    def _image_box(self, view: str) -> tuple[int, int]:
        w, h = self._canvas_box()
        if view == VIEW_SIDE:
            w = max(32, (w - self.ctx.px(PREVIEW_GAP)) // 2)
        return w, h

    def _source_image(self, path: Path):
        """Исходник предпросмотра (RGB, до PREVIEW_LOAD_SIDE).  Рабочий поток."""
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = 0
        key = (os.path.normcase(str(path)), stamp)
        with self._cache_lock:
            im = self._cache.get(key)
            if im is not None:
                self._cache.move_to_end(key)
                return im
        im = enhance.load_image(path, max_side=PREVIEW_LOAD_SIDE)
        if im.mode != "RGB":
            im = enhance.to_image(enhance.to_float(im))
        with self._cache_lock:
            self._cache[key] = im
            while len(self._cache) > SOURCE_CACHE:
                self._cache.popitem(last=False)
        return im

    def _kick_preview(self) -> None:
        if self._preview_job is not None and not self._preview_job.finished:
            return                              # допишет - finish() запустит снова
        if self.current is None:
            return
        gen = self.generation
        path = self.current
        params = self.params()
        view = self.view_var.get()
        box = self._image_box(view)
        self._rendered_box = self._canvas_box()
        self.preview_status.configure(text="Обновляется…", foreground=self._color("muted"))

        def work(report: gc.Reporter) -> dict[str, Any]:
            base = self._source_image(path)
            report.check()
            w, h = base.size
            k = min(box[0] / max(w, 1), box[1] / max(h, 1), 1.0)
            side = max(32, int(round(max(w, h) * k)))
            before = gc.thumbnail(base, side)
            report.check()
            after = enhance.preview(before, params, max_side=side)
            return {"gen": gen, "path": path, "before": before, "after": after,
                    "params": params}

        def finish() -> None:
            self._preview_job = None
            if (self.generation != gen and self._debounce_id is None
                    and not self.ctx.closing):
                self._kick_preview()

        def done(res: dict[str, Any]) -> None:
            if res["gen"] != self.generation:
                self.dropped_previews += 1       # настройки успели измениться
            else:
                self._show_preview(res)
            finish()

        def error(exc: BaseException) -> None:
            if gen != self.generation:
                self.dropped_previews += 1
            else:
                if isinstance(exc, _PREVIEW_ERRORS):
                    msg = "Не удалось открыть %s: %s" % (path.name, exc)
                else:
                    self.ctx.record_error("предпросмотр обработки", exc)
                    msg = "Не удалось построить предпросмотр: %s" % exc
                self.preview_before = self.preview_after = None
                self.preview_error = msg
                self.shown_generation = gen
                self.preview_status.configure(text="")
                self._draw_preview()
            finish()

        def cancelled(_res: Any) -> None:
            finish()

        self._preview_job = self._run(work, on_done=done, on_error=error,
                                      on_cancelled=cancelled, name="enhance-preview")
        if self._preview_job is None:
            self.preview_status.configure(text="")

    def _show_preview(self, res: dict[str, Any]) -> None:
        self.preview_before = res["before"]
        self.preview_after = res["after"]
        self.preview_error = ""
        self.shown_generation = res["gen"]
        self.preview_status.configure(text=res["params"].describe(),
                                      foreground=self._color("muted"))
        self._draw_preview()

    @staticmethod
    def _fit(im: Any, box: tuple[int, int]) -> Any:
        k = min(box[0] / im.width, box[1] / im.height)
        if k >= 1.0:
            return im
        from PIL import Image
        return im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))),
                         Image.Resampling.BILINEAR)

    def _label(self, x: int, y: int, text: str, anchor: str) -> None:
        c = self.canvas
        pad = self.ctx.px(4)
        tid = c.create_text(x, y, text=text, anchor=anchor, fill=self._color("fg"))
        bbox = c.bbox(tid)
        if bbox:
            rid = c.create_rectangle(bbox[0] - pad, bbox[1] - pad // 2, bbox[2] + pad,
                                     bbox[3] + pad // 2, fill=self._color("card_bg"),
                                     outline=self._color("card_border"))
            c.tag_lower(rid, tid)

    def _draw_preview(self) -> None:
        """Перерисовать холст из готовых PIL-картинок.  Только поток Tk."""
        c = self.canvas
        c.delete("all")
        self._split_geom = None
        cw, ch = self._canvas_box()
        split_mode = self.view_var.get() != VIEW_SIDE
        try:
            self.split_scale.grid() if split_mode else self.split_scale.grid_remove()
        except tk.TclError:
            pass
        if self.preview_error:
            c.create_text(cw // 2, ch // 2, text=self.preview_error, width=max(100, cw - 40),
                          fill=self._color("error"), justify="center")
            self._before_tk = self._after_tk = self._split_tk = None
            return
        if self.preview_after is None or self.preview_before is None:
            text = ("Выберите снимок в списке слева" if self.sources
                    else "Выберите папку, файлы или отмеченные в «Отборе» снимки")
            c.create_text(cw // 2, ch // 2, text=text, width=max(100, cw - 40),
                          fill=self._color("muted"), justify="center")
            self._before_tk = self._after_tk = self._split_tk = None
            return
        pad = self.ctx.px(6)
        if not split_mode:
            half = (max(32, (cw - self.ctx.px(PREVIEW_GAP)) // 2), ch)
            b = self._fit(self.preview_before, half)
            a = self._fit(self.preview_after, half)
            self._before_tk = gc.photo_image(b, master=c)
            self._after_tk = gc.photo_image(a, master=c)
            self._split_tk = None
            cx1 = half[0] // 2
            cx2 = cw - half[0] // 2
            c.create_image(cx1, ch // 2, image=self._before_tk, anchor="center")
            c.create_image(cx2, ch // 2, image=self._after_tk, anchor="center")
            top_b = (ch - b.height) // 2 + pad
            top_a = (ch - a.height) // 2 + pad
            self._label(cx1 - b.width // 2 + pad * 2, top_b, "До", "nw")
            self._label(cx2 - a.width // 2 + pad * 2, top_a, "После", "nw")
            return
        a = self._fit(self.preview_after, (cw, ch))
        b = self.preview_before
        if b.size != a.size:
            from PIL import Image
            b = b.resize(a.size, Image.Resampling.BILINEAR)
        self._after_tk = gc.photo_image(a, master=c)
        self._before_tk = gc.photo_image(b, master=c)
        w, h = a.size
        x0, y0 = (cw - w) // 2, (ch - h) // 2
        self._split_geom = (x0, y0, w, h)
        c.create_image(x0, y0, image=self._after_tk, anchor="nw", tags=("after",))
        self._split_tk = tk.PhotoImage(master=c, width=1, height=h)
        c.create_image(x0, y0, image=self._split_tk, anchor="nw", tags=("before",))
        c.create_line(x0, y0, x0, y0 + h, fill=self._color("card_bg"),
                      width=max(1, self.ctx.px(2)), tags=("divider",))
        self._label(x0 + pad * 2, y0 + pad, "До", "nw")
        self._label(x0 + w - pad * 2, y0 + pad, "После", "ne")
        self._update_split()

    def _update_split(self) -> None:
        """Сдвинуть шторку: скопировать левую часть «до» поверх «после»."""
        geom = self._split_geom
        if geom is None or self._split_tk is None or self._before_tk is None:
            return
        x0, y0, w, h = geom
        try:
            pos = max(0.0, min(1.0, float(self.split_var.get())))
        except (tk.TclError, ValueError):
            pos = 0.5
        sx = int(round(w * pos))
        c = self.canvas
        try:
            img = self._split_tk
            img.blank()
            if sx <= 0:
                c.itemconfigure("before", state="hidden")
            else:
                img.configure(width=sx, height=h)
                img.tk.call(str(img), "copy", str(self._before_tk),
                            "-from", 0, 0, sx, h, "-to", 0, 0)
                c.itemconfigure("before", state="normal")
            c.coords("divider", x0 + sx, y0, x0 + sx, y0 + h)
        except tk.TclError:
            pass

    def set_split(self, fraction: float) -> None:
        """Положение шторки 0..1 (0 - всё «после», 1 - всё «до»)."""
        self.split_var.set(max(0.0, min(1.0, float(fraction))))

    def set_view(self, view: str) -> None:
        """Режим сравнения: VIEW_SPLIT или VIEW_SIDE."""
        if view not in _VIEW_TITLES:
            raise ValueError("неизвестный режим сравнения: %r" % (view,))
        self.view_var.set(view)
        self._on_view_change()

    def _on_view_change(self) -> None:
        self.st["view"] = self.view_var.get()
        self._draw_preview()
        # Размер картинки у режимов разный: «рядом» - вдвое уже.
        self.request_preview()

    def _on_canvas_drag(self, event: Any) -> None:
        geom = self._split_geom
        if geom is None:
            return
        x0, _y0, w, _h = geom
        self.set_split((event.x - x0) / max(1, w))

    def _on_canvas_resize(self, _event: Any) -> None:
        if self._resize_id is not None:
            try:
                self.frame.after_cancel(self._resize_id)
            except tk.TclError:
                pass
        try:
            self._resize_id = self.frame.after(DEBOUNCE_MS, self._after_resize)
        except tk.TclError:
            self._resize_id = None

    def _after_resize(self) -> None:
        self._resize_id = None
        self._draw_preview()
        w, h = self._canvas_box()
        rw, rh = self._rendered_box
        if self.current is not None and (rw <= 0 or w > rw * 1.12 or h > rh * 1.12
                                         or w < rw * 0.7 or h < rh * 0.7):
            self.request_preview()

    # ------------------------------------------------------------------
    # Пакетная обработка
    # ------------------------------------------------------------------

    def _run(self, fn: Callable[[gc.Reporter], Any], **kwargs: Any) -> gc.BackgroundJob | None:
        try:
            return self.ctx.run_background(fn, **kwargs)
        except RuntimeError:                    # окно закрывается
            return None

    def _batch_note(self, text: str, role: str = "muted") -> None:
        self._note_roles["batch_status"] = role
        self.batch_status.configure(text=text, foreground=self._color(role))

    @property
    def batch_running(self) -> bool:
        return self._batch_job is not None and not self._batch_job.finished

    def _update_buttons(self) -> None:
        running = self.batch_running
        try:
            self.run_btn.state(["disabled"] if running or not self.sources else ["!disabled"])
            self.cancel_btn.state(["!disabled"] if running and not self._batch_job.cancelled
                                  else ["disabled"])
            has_out = self.last_out_dir is not None or bool(self.out_var.get().strip())
            self.open_btn.state(["!disabled"] if has_out else ["disabled"])
        except tk.TclError:
            pass

    def output_dir(self) -> Path | None:
        """Папка результата из поля ввода (None, если поле пустое)."""
        raw = self.out_var.get().strip().strip('"')
        if not raw:
            return None
        return Path(os.path.normpath(os.path.expanduser(raw)))

    def start_batch(self) -> gc.BackgroundJob | None:
        """«Обработать всё».  Возвращает работу или None, если не начали."""
        if self.batch_running:
            return None
        if not self.sources:
            self._batch_note("Сначала выберите снимки", "warn")
            return None
        out = self.output_dir()
        if out is None:
            self._batch_note("Укажите папку для результата", "warn")
            return None
        if not out.is_absolute():
            self._batch_note("Укажите полный путь к папке результата", "warn")
            return None
        if out.exists() and not out.is_dir():
            self._batch_note("«%s» — это файл, а не папка" % out, "error")
            return None
        paths = list(self.sources)
        allow_source_dir = False
        clash = sorted({str(p.parent) for p in paths
                        if same_folder(p.parent, out) or inside_folder(out, p.parent)})
        if clash:
            ok = self._ask(
                "Папка результата — в папке снимков",
                "Результат будет записан в папку с оригиналами или внутрь неё:\n%s\n\n"
                "Оригиналы не перезаписываются — новые файлы получат другие имена, "
                "но окажутся среди снимков съёмки. Лучше выбрать отдельную папку.\n\n"
                "Всё равно сохранить туда?" % clash[0])
            if not ok:
                self._batch_note("Обработка не начата: выберите другую папку результата", "warn")
                return None
            allow_source_dir = True

        params = self.params()
        quality = self.quality()
        raw_suffix = self.suffix_var.get()
        suffix = clean_suffix(raw_suffix)
        if suffix != raw_suffix:
            self.suffix_var.set(suffix)
        keep_exif = bool(self.exif_var.get())
        workers = self.batch_workers
        self.st.update(params_to_settings(params))
        self._store_output_settings()
        self.ctx.save_settings()

        self.results.delete(*self.results.get_children())
        self.batch_results = []
        q: "queue.Queue[enhance.FileResult]" = queue.Queue()
        self._batch_q = q
        self._batch_out = out
        self._batch_total = len(paths)
        self._batch_t0 = time.monotonic()
        self.progress.configure(value=0.0)

        def work(report: gc.Reporter) -> list[enhance.FileResult]:
            def on_file(done: int, total: int, r: enhance.FileResult) -> None:
                q.put(r)
                report(done / max(total, 1), "Обработано %d из %d — %s"
                       % (done, total, r.src.name))

            return enhance.process_many(paths, out, params, workers=workers, report=on_file,
                                        cancel_event=report.cancel_event, quality=quality,
                                        keep_exif=keep_exif,
                                        allow_source_dir=allow_source_dir, suffix=suffix)

        job = self._run(work, on_progress=self._on_batch_progress,
                        on_done=lambda res: self._batch_finished(res, cancelled=False),
                        on_cancelled=lambda res: self._batch_finished(res, cancelled=True),
                        on_error=self._on_batch_error, name="enhance-batch")
        if job is None:
            return None
        self._batch_job = job
        self._batch_note("Обработка %d снимков: %s" % (len(paths), params.describe()))
        self.ctx.log("Обработка: %d снимков → %s" % (len(paths), out))
        self._update_buttons()
        return job

    def _drain_rows(self) -> None:
        while True:
            try:
                r = self._batch_q.get_nowait()
            except queue.Empty:
                break
            self.batch_results.append(r)
            self._insert_result(r)

    def _insert_result(self, r: enhance.FileResult) -> None:
        short, role = _describe_result(r)
        name = r.dst.name if (r.ok and r.dst is not None) else r.src.name
        iid = self.results.insert("", "end", values=(name, short, r.message), tags=(role,))
        self.results.see(iid)

    def _on_batch_progress(self, fraction: float | None, text: str) -> None:
        self._drain_rows()
        if fraction is not None:
            self.progress.configure(value=fraction * 100.0)
        if text:
            self._batch_note(text)

    def _batch_finished(self, results: Any, cancelled: bool) -> None:
        self._drain_rows()
        if isinstance(results, list):
            # Итог process_many - в порядке списка снимков; строки во время работы
            # шли в порядке готовности.  Показываем окончательный порядок.
            self.batch_results = list(results)
            self.results.delete(*self.results.get_children())
            for r in self.batch_results:
                self._insert_result(r)
        self._batch_job = None
        self.last_out_dir = self._batch_out
        res = self.batch_results
        done_files = {r.src: r.dst for r in res if r.ok and r.dst is not None}
        if done_files:
            self.ctx.publish_processed(done_files)
        ok = sum(1 for r in res if r.ok)
        cancelled_n = sum(1 for r in res if r.skipped and r.message == "Отменено")
        skipped = sum(1 for r in res if r.skipped) - cancelled_n
        failed = sum(1 for r in res if not r.ok and not r.skipped)
        seconds = time.monotonic() - self._batch_t0
        total = self._batch_total
        parts = ["готово %d из %d" % (ok, total)]
        if skipped:
            parts.append("пропущено %d" % skipped)
        if failed:
            parts.append("ошибок %d" % failed)
        if cancelled:
            not_done = cancelled_n + max(0, total - len(res))
            text = "Отменено: %s, не начато %d" % (", ".join(parts), not_done)
            role, level = "warn", "warn"
        else:
            text = "Обработка завершена: %s за %.0f с" % (", ".join(parts), seconds)
            role, level = ("error", "error") if failed else (
                ("warn", "warn") if skipped else ("ok", "ok"))
        if not cancelled:
            self.progress.configure(value=100.0)
        self._batch_note(text, role)
        self.ctx.log(text, level)
        self._update_buttons()

    def _on_batch_error(self, exc: BaseException) -> None:
        self._drain_rows()
        self._batch_job = None
        self._update_buttons()
        text = "Обработка остановлена: %s" % exc
        self._batch_note(text, "error")
        self._report_error("Обработка снимков", exc, text)

    def _report_error(self, where: str, exc: BaseException, summary: str) -> None:
        """Окно с ошибкой.  Тесты подменяют этот метод."""
        self.ctx.show_error(where, exc, summary)

    def cancel_batch(self) -> None:
        """Остановить пакет: файлы в работе допишутся, остальные не начнутся."""
        job = self._batch_job
        if job is None or job.finished:
            return
        job.cancel()
        self._batch_note("Отмена… файлы, которые уже в работе, будут дописаны", "warn")
        self._update_buttons()

    def open_result_folder(self) -> bool:
        target = self.output_dir()
        if target is None or not target.is_dir():
            target = self.last_out_dir
        if target is None or not target.is_dir():
            self._batch_note("Папки результата ещё нет — сначала обработайте снимки", "warn")
            return False
        return self.ctx.reveal(target)

    def _on_result_activate(self, _event: Any = None) -> None:
        sel = self.results.selection()
        if not sel:
            return
        idx = self.results.index(sel[0])
        if 0 <= idx < len(self.batch_results):
            r = self.batch_results[idx]
            if r.ok and r.dst is not None and r.dst.exists():
                self.ctx.reveal(r.dst)

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------

    def _cancel_timers(self) -> None:
        for attr in ("_debounce_id", "_resize_id"):
            after_id = getattr(self, attr)
            if after_id is not None:
                try:
                    self.frame.after_cancel(after_id)
                except (tk.TclError, RuntimeError):
                    pass
                setattr(self, attr, None)

    def _shutdown(self) -> None:
        self._cancel_timers()
        for job in (self._preview_job, self._batch_job):
            if job is not None:
                job.cancel()

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.frame:
            return
        self._shutdown()
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs = []


def build_tab(parent: ttk.Notebook, ctx: gc.AppContext) -> ttk.Frame:
    """Контракт вкладки: фрейм - потомок parent; в блокнот его добавит оболочка."""
    return EnhanceTab(parent, ctx).frame


if __name__ == "__main__":          # pragma: no cover - разработка вкладки
    sys.exit(gc.run_standalone(sys.modules[__name__]))
