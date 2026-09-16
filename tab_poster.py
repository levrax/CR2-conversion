# -*- coding: utf-8 -*-
"""tab_poster - вкладка «Афиши»: фирменные афиши и посты из готового кадра.

Что делает вкладка
------------------
* Шаблон (афиша A3, пост, сторис, обложка альбома, итоги, цитата) и форма,
  которая строится из полей, объявленных шаблоном в poster.py.
* Фотография: файл с диска или кадр из отмеченных во вкладке «Отбор» (лента
  миниатюр).  Если кадр уже прошёл «Обработку», лента берёт обработанный файл
  (ctx.processed_for) и подписывает, какой взят.  Лицо кадра известно из
  «Отбора» (ctx.photo_hint) или находится тут же (cull.find_faces): кроп сразу
  приближается так, чтобы лицо заняло нужную долю блока, но голова целиком
  осталась в кадре.  Щелчок по лицу на миниатюре делает то же; щелчок в другое
  место ставит просто точку фокуса.  Фото и фокус стоят в панели над текстом.
* Живой предпросмотр: рисуется в рабочем потоке в уменьшенном масштабе,
  с задержкой после ввода и счётчиком поколений - набор текста не тормозит.
* Под предпросмотром: полоски 60/30/10, предупреждения по-русски (баланс,
  контраст, не влезший текст) и плашка, если заголовок набран заменой
  Morfin Sans, с кнопкой выбора файла шрифта.
* Экспорт в PNG / JPEG / PDF (PDF - только афиша A3) в полном размере в
  рабочем потоке с прогрессом, и «во все форматы»: пост, сторис и обложка
  альбома из одних и тех же полей.  Сохранять в папку со снимками (или внутрь
  неё) и поверх чужого снимка вкладка отказывается (export_problem).

Потоки - по контракту gui_common: рабочий поток получает снимок параметров
(строки, числа, пути), возвращает PIL.Image и числа; PhotoImage создаёт только
поток Tk.

Настройки (ctx.tab_settings("poster")):
    template      последний шаблон
    fields        {template_id: {key: value}} - последние поля каждого шаблона
    display_font  путь к файлу Morfin Sans, выбранному пользователем
    format        "png" | "jpeg" | "pdf"
    export_dir    последняя папка экспорта (отдельно от папки снимков)
    exported      ключи файлов, которые вкладка сама сохранила (их можно заменить)
    last_dir      последняя папка снимков (pick_files)

Morfin Sans с программой не поставляется: лицензия автора не даёт права
распространять файл, поэтому пользователь указывает его сам.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import ttk

import brand
import cr2_core
import gui_common as gc
import poster

__all__ = ["TAB_TITLE", "build_tab", "PosterTab", "focus_face_box", "auto_zoom",
           "effective_zoom", "export_problem", "export_file_name", "unique_path",
           "SOCIAL_TEMPLATES", "FORMATS"]

TAB_TITLE = "Афиши"
TAB_KEY = "poster"

#: Шаблоны, которые делает кнопка «Экспортировать во все форматы».
SOCIAL_TEMPLATES: tuple[str, ...] = ("post", "stories", "album_cover")
#: Формат -> (расширение, подпись).
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "PNG"),
    "jpeg": (".jpg", "JPEG"),
    "pdf": (".pdf", "PDF (A3)"),
}
PDF_TEMPLATES: frozenset[str] = frozenset({"afisha_a3"})

DEBOUNCE_MS = 300               # пауза после ввода до перерисовки
PREVIEW_PHOTO_SIDE = 1600       # фото для предпросмотра, px по длинной стороне
PREVIEW_MAX_SIDE = 1000         # предпросмотр не крупнее, px
PREVIEW_MIN_BOX = (240, 300)    # если холст ещё не показан
FOCUS_THUMB = (240, 170)        # миниатюра для точки фокуса, логические px
STRIP_THUMB = 84                # лента отмеченных кадров, логические px
STRIP_LIMIT = 300               # столько кадров из «Отбора» показываем
HEAD_ABOVE = 0.45               # голова над рамкой лица Хаара: волосы, доли высоты лица
HEAD_BELOW = 0.35               # и под ней: подбородок и шея
FACE_SNAP = 0.25                # щелчок в рамке лица, расширенной на столько, - «по лицу»
EXPORTED_MEMORY = 200           # столько своих файлов экспорта помним

# poster.render и шрифты FreeType не рассчитаны на одновременную работу из
# двух потоков (предпросмотр и экспорт): отрисовки идут по очереди.
_RENDER_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Чистые функции (без Tk)
# --------------------------------------------------------------------------

def _image_block(template_id: str) -> dict | None:
    """Блок фотографии шаблона (или None)."""
    for b in poster.TEMPLATES[template_id]["blocks"]:
        if b.get("type") == "image":
            return b
    return None


def _base_crop(template_id: str, img_w: int, img_h: int
               ) -> tuple[float, float, dict] | None:
    """(ширина, высота кропа без приближения, блок фото) - или None."""
    block = _image_block(template_id)
    if block is None or img_w <= 0 or img_h <= 0:
        return None
    W, H = poster.TEMPLATES[template_id]["size"]
    _x, _y, rw, rh = (float(v) for v in block["rect"])
    dst_ar = max(rw * W, 1.0) / max(rh * H, 1.0)
    if img_w / img_h > dst_ar:
        return img_h * dst_ar, float(img_h), block
    return float(img_w), img_w / dst_ar, block


def _face_px(face: tuple[float, float, float, float], img_w: int, img_h: int
             ) -> tuple[float, float, float, float]:
    """(x, y, w, h) в долях -> (left, top, right, bottom) в пикселях кадра."""
    x, y, w, h = (float(v) for v in face)
    return x * img_w, y * img_h, (x + w) * img_w, (y + h) * img_h


def _head_px(face: tuple[float, float, float, float], img_w: int, img_h: int
             ) -> tuple[float, float, float, float]:
    """Голова целиком: рамка лица с волосами сверху и подбородком снизу."""
    l, t, r, b = _face_px(face, img_w, img_h)
    fh = b - t
    return l, max(0.0, t - HEAD_ABOVE * fh), r, min(float(img_h), b + HEAD_BELOW * fh)


def effective_zoom(template_id: str, img_w: int, img_h: int, zoom: float,
                   face: tuple[float, float, float, float] | None = None) -> float:
    """Приближение, которое реально получится: не больше max_zoom шаблона и -
    если лицо известно - не больше того, при котором голова ещё влезает по высоте."""
    base = _base_crop(template_id, img_w, img_h)
    if base is None:
        return 1.0
    _cw, ch, block = base
    z = min(max(float(zoom), 1.0), max(1.0, float(block.get("max_zoom", 1.0))))
    if face is not None:
        _l, t, _r, b = _head_px(face, img_w, img_h)
        if b - t > 1.0:
            z = min(z, ch / (b - t))
    return max(1.0, z)


def auto_zoom(template_id: str, img_w: int, img_h: int,
              face: tuple[float, float, float, float]) -> float:
    """Приближение, при котором лицо занимает face_fill ширины блока фото.

    Не больше max_zoom шаблона и не больше, чем позволяет голова по высоте.
    """
    base = _base_crop(template_id, img_w, img_h)
    if base is None:
        return 1.0
    cw, _ch, block = base
    l, _t, r, _b = _face_px(face, img_w, img_h)
    fill = float(block.get("face_fill", 0.0))
    want = fill * cw / max(r - l, 1.0) if fill > 0 else 1.0
    return effective_zoom(template_id, img_w, img_h, want, face)


def focus_face_box(template_id: str, img_w: int, img_h: int,
                   focus: tuple[float, float] | None,
                   zoom: float = 1.0,
                   face: tuple[float, float, float, float] | None = None
                   ) -> tuple[float, float, float, float] | None:
    """Точка фокуса (доли кадра) -> face_box для poster.render в пикселях кадра.

    poster кадрирует «по лицу»: прямоугольник остаётся в кадре, его центр
    встаёт на 40 % высоты кропа, а кроп приближается так, чтобы прямоугольник
    занял face_fill ширины блока.  Размер прямоугольника подобран так, чтобы
    приближение было ровно `zoom` (1.0 - кадр не приближается).

    face - настоящее лицо (x, y, w, h в долях), если оно известно.  Тогда к
    прямоугольнику добавляется голова целиком (_head_px), а приближение
    ограничено effective_zoom: кроп не режет ни лоб, ни подбородок.  Без фокуса
    и без лица - None; фокус без лица по умолчанию - центр лица.
    """
    if face is not None and focus is None:
        focus = (float(face[0]) + float(face[2]) / 2.0, float(face[1]) + float(face[3]) / 2.0)
    if focus is None:
        return None
    base = _base_crop(template_id, img_w, img_h)
    if base is None:
        return None
    cw, _ch, block = base
    face_fill = float(block.get("face_fill", 0.0))
    z = effective_zoom(template_id, img_w, img_h, zoom, face)
    side = face_fill * cw / z if face_fill > 0 else 0.2 * cw
    side = max(4.0, min(side, float(img_w), float(img_h)))
    fx = min(max(float(focus[0]), 0.0), 1.0)
    fy = min(max(float(focus[1]), 0.0), 1.0)
    # Сдвигаем, а не обрезаем: обрезанный прямоугольник меньше и дал бы
    # лишнее приближение.
    left = min(max(fx * img_w - side / 2.0, 0.0), img_w - side)
    top = min(max(fy * img_h - side / 2.0, 0.0), img_h - side)
    box = (left, top, left + side, top + side)
    if face is None:
        return box
    hl, ht, hr, hb = _head_px(face, img_w, img_h)
    return (min(box[0], hl), min(box[1], ht), max(box[2], hr), max(box[3], hb))


def export_problem(target: str | Path, source_files: list[Path], shoot_folders: list[Path],
                   exported_keys: set[str] | frozenset[str] = frozenset()) -> str:
    """Почему сохранять афишу в target нельзя (текст по-русски) или "" - можно.

    * поверх исходного снимка;
    * в папку со снимками съёмки или внутрь неё - их программа только читает;
    * поверх существующего снимка (JPEG, PNG, TIFF, CR2), который не был
      сохранён этой вкладкой: диалог «Заменить?» легко подтвердить не глядя.
    Пути сравниваются так же, как в конвертере (cr2_core._dst_key): без учёта
    регистра и там, где его не учитывает сам том (macOS).
    """
    target = Path(target)
    tkey = cr2_core._dst_key(target)                      # noqa: SLF001
    for src in source_files:
        if tkey == cr2_core._dst_key(src):                 # noqa: SLF001
            return "Нельзя сохранить поверх исходного снимка: %s" % target.name
    folder_key = cr2_core._dst_key(target.parent)          # noqa: SLF001
    for folder in shoot_folders:
        fkey = cr2_core._dst_key(folder)                   # noqa: SLF001
        if folder_key == fkey or folder_key.startswith(fkey.rstrip("\\/") + os.sep):
            return ("Нельзя сохранять афишу в папку со снимками или внутрь неё: %s. "
                    "Выберите другую папку." % folder)
    try:
        exists = target.exists()
    except OSError:
        exists = False
    if exists and target.suffix.lower() in gc.IMAGE_EXTENSIONS and tkey not in exported_keys:
        return ("Файл «%s» уже есть, и это не афиша, сохранённая программой. "
                "Выберите другое имя." % target.name)
    return ""


def _crop_on_image(template_id: str, img_w: int, img_h: int,
                   focus: tuple[float, float] | None, zoom: float,
                   face: tuple[float, float, float, float] | None = None
                   ) -> tuple[int, int, int, int] | None:
    """Какую часть кадра займёт фото в шаблоне (для рамки на миниатюре)."""
    block = _image_block(template_id)
    if block is None or img_w <= 0 or img_h <= 0:
        return None
    W, H = poster.TEMPLATES[template_id]["size"]
    _x, _y, rw, rh = (float(v) for v in block["rect"])
    return poster.cover_crop_box(
        img_w, img_h, max(1, int(round(rw * W))), max(1, int(round(rh * H))),
        face_box=focus_face_box(template_id, img_w, img_h, focus, zoom, face),
        face_fill=float(block.get("face_fill", 0.0)),
        max_zoom=float(block.get("max_zoom", 1.0)))


def _slug(text: str, limit: int = 40) -> str:
    """Безопасная часть имени файла: буквы (и кириллица), цифры, «_»."""
    out: list[str] = []
    for ch in str(text or "").strip():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")[:limit].strip("_")


def export_file_name(template_id: str, fields: dict[str, str], fmt: str) -> str:
    """«Искусство_судебной_речи_post.png» - имя файла по умолчанию."""
    base = _slug(fields.get("headline") or fields.get("author") or "") or "poster"
    ext = FORMATS.get(fmt, FORMATS["png"])[0]
    return "%s_%s%s" % (base, template_id, ext)


def unique_path(path: Path) -> Path:
    """Путь, которого ещё нет: «имя.png», «имя (2).png», ...  Ничего не пишет."""
    if not path.exists():
        return path
    for i in range(2, 10000):
        cand = path.with_name("%s (%d)%s" % (path.stem, i, path.suffix))
        if not cand.exists():
            return cand
    raise OSError("Не удалось подобрать свободное имя для %s" % path.name)


def _short_family(family: str) -> str:
    """'Oswald Bold' -> 'Oswald' для плашки."""
    styles = {"bold", "regular", "medium", "semibold", "light", "black", "italic"}
    words = str(family or "").split()
    while len(words) > 1 and words[-1].lower() in styles:
        words.pop()
    return " ".join(words) or str(family)


def _load_photo(path: Path, max_side: int | None):
    """Снимок только на чтение, RGB, без EXIF (поворот уже применён).

    EXIF убирается, иначе poster.render повернул бы кадр второй раз (у CR2
    ориентация берётся из самого CR2, а во встроенном JPEG может быть своя).
    """
    im = gc.load_image_fast(path, max_side)
    im = im.convert("RGB")
    im.info.pop("exif", None)
    return im


def _pretty_warning(text: str, labels: dict[str, str]) -> str:
    """«Поле «headline»» -> «Поле «Заголовок»»."""
    for key, label in labels.items():
        text = text.replace("«%s»" % key, "«%s»" % label)
    return text


def _is_substitute_note(text: str) -> bool:
    return text.startswith("Заголовок набран заменой") or \
        text.startswith("Основной текст набран заменой")


# --------------------------------------------------------------------------
# Вкладка
# --------------------------------------------------------------------------

class PosterTab:
    """Состояние и виджеты вкладки «Афиши».  Живёт в потоке Tk.

    Для тестов и соседних вкладок открыты: select_template, field_values,
    set_field, set_photo, set_focus, set_zoom, set_display_font,
    request_preview, export, export_all и поля preview_image, last_report,
    last_usage, last_warnings, preview_error, generation, shown_generation.
    """

    def __init__(self, parent: tk.Misc, ctx: gc.AppContext) -> None:
        self.ctx = ctx
        self.st = ctx.tab_settings(TAB_KEY)
        if not isinstance(self.st.get("fields"), dict):
            self.st["fields"] = {}
        self.frame = ttk.Frame(parent)
        self.frame.poster_tab = self            # type: ignore[attr-defined]

        ids = [tid for tid, _ in poster.list_templates()]
        tid = self.st.get("template")
        self.template_id: str = tid if tid in ids else ids[0]

        # фото и фокус
        self.photo_path: Path | None = None
        #: Снимок съёмки, из которого получен photo_path (сам он или его
        #: обработанная версия), и взята ли обработанная версия.
        self.photo_origin: Path | None = None
        self.photo_processed = False
        self.focus: tuple[float, float] | None = None
        self.zoom: float = 1.0
        #: Лицо, по которому кадрируем (x, y, w, h в долях), и все найденные лица.
        self.face: tuple[float, float, float, float] | None = None
        self.faces: list[tuple[float, float, float, float]] = []
        self._user_focus = False
        self._zoom_manual = False
        self._photo_cache: dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._focus_thumb = None                # PIL
        self._focus_tk = None
        self._focus_key: tuple = ()
        self._focus_geom = (0, 0, 1, 1)         # x, y, w, h миниатюры на холсте
        self._thumb_job: gc.BackgroundJob | None = None
        self._strip_job: gc.BackgroundJob | None = None
        #: (файл для афиши, миниатюра, снимок съёмки, обработанный ли).
        self._strip_items: list[tuple[Path, Any, Path, bool]] = []
        self._strip_tk: list[Any] = []
        self._strip_gen = 0
        self._strip_shown = False

        # предпросмотр
        self.generation = 0
        self.shown_generation = -1
        self.preview_image = None               # PIL последнего предпросмотра
        self.last_report: poster.RenderReport | None = None
        self.last_usage: dict[str, float] | None = None
        self.last_warnings: list[str] = []
        self.preview_error: str = ""
        self._preview_tk = None
        self._preview_job: gc.BackgroundJob | None = None
        self._debounce_id: str | None = None
        self._resize_id: str | None = None
        self._rendered_box = (0, 0)

        # экспорт
        self._export_job: gc.BackgroundJob | None = None
        self.last_export_paths: list[Path] = []

        # форма
        self._vars: dict[str, tk.StringVar] = {}
        self._texts: dict[str, tk.Text] = {}
        self._building = False
        #: Виджет -> {параметр: роль palette()}: перекрашивается при смене темы.
        self._themed: dict[str, tuple[tk.Misc, dict[str, str]]] = {}

        self._build()
        self._build_form(self._initial_values(self.template_id))
        self._update_format_state()

        self._unsubs: list[Callable[[], None]] = [
            ctx.subscribe(gc.TOPIC_SELECTION, self._on_selection_changed),
            ctx.subscribe(gc.TOPIC_PROCESSED, self._on_processed),
            ctx.register_shutdown(self._shutdown),
        ]
        self.frame.bind("<Destroy>", self._on_destroy, add="+")
        self.frame.bind("<<ThemeChanged>>", self._on_theme_changed, add="+")
        self._on_selection_changed(ctx.selection)
        self.request_preview(immediate=True)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _color(self, role: str) -> str:
        return gc.palette(role, self.frame)

    def _theme_bg(self) -> str:
        try:
            return ttk.Style(self.frame).lookup(".", "background") or self._color("bg")
        except tk.TclError:
            return self._color("bg")

    def _paint(self, widget: tk.Misc, **roles: str) -> tk.Misc:
        """Задать цвета ролями palette() и запомнить их для смены темы.

        Роль "theme_bg" - фон окна, который рисует тема ttk.
        """
        entry = self._themed.setdefault(str(widget), (widget, {}))
        entry[1].update(roles)
        try:
            widget.configure(**{opt: self._resolve(role) for opt, role in roles.items()})
        except tk.TclError:
            pass
        return widget

    def _resolve(self, role: str) -> str:
        return self._theme_bg() if role == "theme_bg" else self._color(role)

    def _on_theme_changed(self, _event: Any = None) -> None:
        """Тема сменилась (macOS меняет оформление на ходу): перекрасить всё."""
        for key, (widget, roles) in list(self._themed.items()):
            try:
                widget.configure(**{opt: self._resolve(role) for opt, role in roles.items()})
            except tk.TclError:
                self._themed.pop(key, None)             # виджета уже нет
        try:
            self._draw_focus()
            self._draw_strip()
            self._draw_preview()
            self._draw_usage()
        except tk.TclError:
            pass

    def _build(self) -> None:
        px = self.ctx.px
        f = self.frame
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        # ---- левая колонка: прокручиваемая панель ----
        left_outer = ttk.Frame(f)
        left_outer.grid(row=0, column=0, sticky="nsw", padx=(px(8), px(4)), pady=px(8))
        left_outer.rowconfigure(0, weight=1)
        self._left_outer = left_outer
        self._left_canvas = tk.Canvas(left_outer, width=px(350), highlightthickness=0,
                                      borderwidth=0)
        self._paint(self._left_canvas, background="theme_bg")
        vsb = ttk.Scrollbar(left_outer, orient="vertical",
                            command=self._left_canvas.yview)
        self._left_canvas.configure(yscrollcommand=vsb.set)
        self._left_canvas.grid(row=0, column=0, sticky="ns")
        vsb.grid(row=0, column=1, sticky="ns")
        left = ttk.Frame(self._left_canvas, padding=(0, 0, px(6), 0))
        win = self._left_canvas.create_window(0, 0, window=left, anchor="nw")
        left.bind("<Configure>", lambda e: self._left_canvas.configure(
            scrollregion=self._left_canvas.bbox("all")))
        self._left_canvas.bind("<Configure>", lambda e: self._left_canvas.itemconfigure(
            win, width=e.width))
        for w in (self._left_canvas, left):
            w.bind("<Enter>", self._wheel_on, add="+")
            w.bind("<Leave>", self._on_wheel_leave, add="+")
        left.columnconfigure(0, weight=1)

        # шаблон
        box = ttk.LabelFrame(left, text="Шаблон", padding=px(6))
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        self._titles = dict(poster.list_templates())
        self.template_var = tk.StringVar(value=self._titles[self.template_id])
        self.template_combo = ttk.Combobox(box, state="readonly",
                                           textvariable=self.template_var,
                                           values=list(self._titles.values()))
        self.template_combo.grid(row=0, column=0, sticky="ew")
        self.template_combo.bind("<<ComboboxSelected>>", self._on_template_combo)

        # поля (под фотографией: фото и фокус видны без прокрутки)
        self.form_box = ttk.LabelFrame(left, text="Текст", padding=px(6))
        self.form_box.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        self.form_box.columnconfigure(0, weight=1)
        self.form = ttk.Frame(self.form_box)
        self.form.grid(row=0, column=0, sticky="ew")
        self.form.columnconfigure(0, weight=1)
        row = ttk.Frame(self.form_box)
        row.grid(row=1, column=0, sticky="ew", pady=(px(6), 0))
        ttk.Button(row, text="Подставить пример в пустые поля",
                   command=self.fill_examples).pack(side="left")
        self._paint(ttk.Label(self.form_box, text="* обязательное поле"),
                    foreground="muted").grid(row=2, column=0, sticky="w")

        # фотография
        pb = ttk.LabelFrame(left, text="Фотография *", padding=px(6))
        pb.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        pb.columnconfigure(0, weight=1)
        btns = ttk.Frame(pb)
        btns.grid(row=0, column=0, sticky="ew")
        ttk.Button(btns, text="Выбрать файл…", command=self._pick_photo).pack(side="left")
        ttk.Button(btns, text="Без фото",
                   command=lambda: self.set_photo(None)).pack(side="left", padx=(px(6), 0))
        self.from_sel_btn = ttk.Button(pb, text="Из отмеченных в «Отборе»",
                                       command=self.show_selection_strip)
        self.from_sel_btn.grid(row=1, column=0, sticky="w", pady=(px(6), 0))
        self.photo_label = ttk.Label(pb, text="Фото не выбрано - будет заглушка",
                                     wraplength=px(320))
        self._paint(self.photo_label, foreground="muted")
        self.photo_label.grid(row=2, column=0, sticky="w", pady=(px(4), 0))

        self.strip_frame = ttk.Frame(pb)
        self.strip_frame.grid(row=3, column=0, sticky="ew", pady=(px(6), 0))
        self.strip_frame.columnconfigure(0, weight=1)
        self.strip_canvas = tk.Canvas(self.strip_frame, height=px(STRIP_THUMB + 8),
                                      highlightthickness=1, borderwidth=0)
        self._paint(self.strip_canvas, background="card_bg", highlightbackground="card_border")
        hsb = ttk.Scrollbar(self.strip_frame, orient="horizontal",
                            command=self.strip_canvas.xview)
        self.strip_canvas.configure(xscrollcommand=hsb.set)
        self.strip_canvas.grid(row=0, column=0, sticky="ew")
        hsb.grid(row=1, column=0, sticky="ew")
        self.strip_canvas.bind("<Button-1>", self._on_strip_click)
        self.strip_status = ttk.Label(self.strip_frame, text="", wraplength=px(320))
        self._paint(self.strip_status, foreground="muted")
        self.strip_status.grid(row=2, column=0, sticky="w")
        self.strip_frame.grid_remove()

        self._paint(ttk.Label(pb, text="Щёлкните по лицу на снимке: кадр приблизится к "
                                       "нему, не обрезая голову, и тон растра подстроится "
                                       "под лицо.  Щелчок в другое место - просто точка "
                                       "фокуса.", wraplength=px(320), justify="left"),
                    foreground="muted").grid(row=4, column=0, sticky="w", pady=(px(6), 0))
        fw, fh = FOCUS_THUMB
        self.focus_canvas = tk.Canvas(pb, width=px(fw), height=px(fh),
                                      highlightthickness=1, borderwidth=0,
                                      cursor="crosshair")
        self._paint(self.focus_canvas, background="card_bg", highlightbackground="card_border")
        self.focus_canvas.grid(row=5, column=0, sticky="w", pady=(px(4), 0))
        self.focus_canvas.bind("<Button-1>", self._on_focus_click)
        zr = ttk.Frame(pb)
        zr.grid(row=6, column=0, sticky="ew", pady=(px(4), 0))
        zr.columnconfigure(1, weight=1)
        self.zoom_label = ttk.Label(zr, text="Приближение: 1.0×")
        self.zoom_label.grid(row=0, column=0, sticky="w")
        self.zoom_var = tk.DoubleVar(value=1.0)
        self.zoom_scale = ttk.Scale(zr, from_=1.0, to=2.5, variable=self.zoom_var,
                                    command=self._on_zoom_scale)
        self.zoom_scale.grid(row=0, column=1, sticky="ew", padx=(px(6), 0))
        self.reset_focus_btn = ttk.Button(pb, text="Сбросить фокус и приближение",
                                          command=lambda: self.set_focus(None))
        self.reset_focus_btn.grid(row=7, column=0, sticky="w", pady=(px(4), 0))
        self._update_focus_controls()
        self._draw_focus()

        # ---- правая колонка ----
        right = ttk.Frame(f)
        right.grid(row=0, column=1, sticky="nsew", padx=(px(4), px(8)), pady=px(8))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1, minsize=px(260))
        right.bind("<Configure>", self._on_right_resize, add="+")

        head = ttk.Frame(right)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Предпросмотр").pack(side="left")
        self.preview_status = ttk.Label(head, text="", wraplength=px(480), justify="left")
        self._paint(self.preview_status, foreground="muted")
        self.preview_status.pack(side="left", padx=(px(10), 0))

        self.preview_canvas = tk.Canvas(right, highlightthickness=0, borderwidth=0,
                                        width=px(420), height=px(420))
        self._paint(self.preview_canvas, background="theme_bg")
        self.preview_canvas.grid(row=1, column=0, sticky="nsew", pady=(px(4), px(6)))
        self.preview_canvas.bind("<Configure>", self._on_preview_resize)

        # плашка замены шрифта
        self.banner = tk.Frame(right, highlightthickness=2)
        self._paint(self.banner, background="card_bg", highlightbackground="warn",
                    highlightcolor="warn")
        self.banner.grid(row=2, column=0, sticky="ew")
        self.banner.columnconfigure(0, weight=1)
        self.banner_label = tk.Label(self.banner, text="", anchor="w", justify="left")
        self._paint(self.banner_label, background="card_bg", foreground="warn")
        self.banner_label.grid(row=0, column=0, sticky="ew", padx=px(8), pady=(px(6), 0))
        self.banner_btn = ttk.Button(self.banner, text="Указать файл Morfin Sans…",
                                     command=self._pick_display_font)
        self.banner_btn.grid(row=0, column=1, padx=px(8), pady=(px(6), 0))
        self.banner_hint = tk.Label(
            self.banner, anchor="w", justify="left",
            text="Файла шрифта нет в программе: лицензия автора не даёт явного "
                 "права его распространять.")
        self._paint(self.banner_hint, background="card_bg", foreground="muted")
        self.banner_hint.grid(row=1, column=0, columnspan=2, sticky="ew",
                              padx=px(8), pady=(px(2), px(6)))
        self.banner.grid_remove()

        fonts_row = ttk.Frame(right)
        fonts_row.grid(row=3, column=0, sticky="ew", pady=(px(4), 0))
        fonts_row.columnconfigure(0, weight=1)
        self.fonts_label = ttk.Label(fonts_row, text="", wraplength=px(560), justify="left")
        self._paint(self.fonts_label, foreground="muted")
        self.fonts_label.grid(row=0, column=0, sticky="w")
        self.reset_font_btn = ttk.Button(fonts_row, text="Забыть файл шрифта",
                                         command=lambda: self.set_display_font(None))
        self.reset_font_btn.grid(row=0, column=1, sticky="e")
        if not self.st.get("display_font"):
            self.reset_font_btn.grid_remove()

        # баланс 60/30/10
        bal = ttk.LabelFrame(right, text="Баланс цветов 60/30/10 (без фото)", padding=px(6))
        bal.grid(row=4, column=0, sticky="ew", pady=(px(6), 0))
        bal.columnconfigure(0, weight=1)
        self.usage_canvas = tk.Canvas(bal, height=px(3 * 20 + 4), highlightthickness=0,
                                      borderwidth=0)
        self._paint(self.usage_canvas, background="theme_bg")
        self.usage_canvas.grid(row=0, column=0, sticky="ew")
        self.usage_canvas.bind("<Configure>", lambda e: self._draw_usage())
        self.warn_label = ttk.Label(bal, text="", wraplength=px(560), justify="left")
        self.warn_label.grid(row=1, column=0, sticky="w", pady=(px(4), 0))

        # экспорт
        ex = ttk.LabelFrame(right, text="Экспорт", padding=px(6))
        ex.grid(row=5, column=0, sticky="ew", pady=(px(6), 0))
        ex.columnconfigure(3, weight=1)
        fmt = self.st.get("format")
        self.format_var = tk.StringVar(value=fmt if fmt in FORMATS else "png")
        self.format_buttons: dict[str, ttk.Radiobutton] = {}
        for i, (key, (_ext, label)) in enumerate(FORMATS.items()):
            rb = ttk.Radiobutton(ex, text=label, value=key, variable=self.format_var,
                                 command=self._on_format)
            rb.grid(row=0, column=i, sticky="w", padx=(0, px(10)))
            self.format_buttons[key] = rb
        row = ttk.Frame(ex)
        row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(px(6), 0))
        self.export_btn = ttk.Button(row, text="Экспорт…", command=self._on_export)
        self.export_btn.pack(side="left")
        self.export_all_btn = ttk.Button(
            row, text="Экспортировать во все форматы…", command=self._on_export_all)
        self.export_all_btn.pack(side="left", padx=(px(6), 0))
        self.cancel_btn = ttk.Button(row, text="Отмена", command=self._cancel_export,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=(px(6), 0))
        self.reveal_btn = ttk.Button(row, text="Открыть папку", state="disabled",
                                     command=self._reveal_export)
        self.reveal_btn.pack(side="left", padx=(px(6), 0))
        self.progress = ttk.Progressbar(ex, maximum=1.0, value=0.0)
        self.progress.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(px(6), 0))
        self.export_status = ttk.Label(ex, text="Во все форматы: пост, сторис и обложка "
                                                "альбома из тех же полей.",
                                       wraplength=px(560), justify="left")
        self._paint(self.export_status, foreground="muted")
        self.export_status.grid(row=3, column=0, columnspan=4, sticky="w",
                                pady=(px(4), 0))

    def _on_right_resize(self, event: Any) -> None:
        """Переносы строк - по ширине правой колонки, а не по константе."""
        width = max(int(event.width), self.ctx.px(200))
        button = self.banner_btn.winfo_reqwidth() + self.ctx.px(40)
        for label, w in ((self.banner_label, width - button),
                         (self.banner_hint, width - self.ctx.px(24)),
                         (self.fonts_label, width - self.ctx.px(20)
                          - (self.reset_font_btn.winfo_reqwidth()
                             if self.reset_font_btn.winfo_manager() else 0)),
                         (self.warn_label, width - self.ctx.px(24)),
                         (self.export_status, width - self.ctx.px(24)),
                         (self.preview_status, width - self.ctx.px(140))):
            label.configure(wraplength=max(self.ctx.px(120), w))

    # ---- колесо мыши над левой панелью ----

    def _wheel_on(self, _event: Any = None) -> None:
        c = self._left_canvas
        c.bind_all("<MouseWheel>", self._on_wheel)
        c.bind_all("<Button-4>", self._on_wheel)
        c.bind_all("<Button-5>", self._on_wheel)

    def _on_wheel_leave(self, _event: Any = None) -> None:
        # Переход указателя на поле формы - тоже <Leave> для панели: снимаем
        # привязку, только если указатель действительно ушёл с панели.
        try:
            x, y = self.frame.winfo_pointerxy()
            under = self.frame.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            under = None
        if under is not None and str(under).startswith(str(self._left_outer)):
            return
        self._wheel_off()

    def _wheel_off(self, _event: Any = None) -> None:
        c = self._left_canvas
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                c.unbind_all(seq)
            except tk.TclError:
                pass

    def _on_wheel(self, event: Any) -> None:
        if isinstance(event.widget, tk.Text):
            return
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            delta = int(getattr(event, "delta", 0) or 0)
            if sys.platform == "darwin":
                step = -delta
            else:
                step = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        if step:
            self._left_canvas.yview_scroll(step, "units")

    # ------------------------------------------------------------------
    # Форма
    # ------------------------------------------------------------------

    def _text_specs(self, template_id: str | None = None) -> list[poster.FieldSpec]:
        return [s for s in poster.template_fields(template_id or self.template_id)
                if s.kind == "text"]

    def _initial_values(self, template_id: str,
                        carry: dict[str, str] | None = None,
                        carry_from: str | None = None) -> dict[str, str]:
        """Последние поля шаблона; впервые - общие поля текущего и примеры."""
        saved = self.st["fields"].get(template_id)
        values = poster.example_fields(template_id)
        if isinstance(saved, dict):
            # Сохранённое - как есть (и пустые поля тоже); пример - только для
            # полей, которых в прошлый раз у шаблона не было.
            values.update({k: "" if v is None else str(v) for k, v in saved.items()})
            return values
        if carry and carry_from:
            labels = {s.key: s.label for s in self._text_specs(carry_from)}
            for spec in self._text_specs(template_id):
                v = carry.get(spec.key, "")
                if v.strip() and labels.get(spec.key) == spec.label:
                    values[spec.key] = v
        return values

    def _build_form(self, values: dict[str, str]) -> None:
        px = self.ctx.px
        self._building = True
        try:
            for child in self.form.winfo_children():
                child.destroy()
            self._vars.clear()
            self._texts.clear()
            r = 0
            for spec in self._text_specs():
                label = spec.label + (" *" if spec.required else "")
                ttk.Label(self.form, text=label).grid(row=r, column=0, sticky="w",
                                                      pady=(px(4) if r else 0, 0))
                r += 1
                value = values.get(spec.key, "")
                if spec.multiline:
                    # Шрифт - как у полей ввода: без него tk.Text на Windows
                    # набирает цитату моноширинным Courier.
                    txt = tk.Text(self.form, height=4, width=34, wrap="word", undo=True,
                                  font=self._entry_font(), highlightthickness=1,
                                  borderwidth=0)
                    self._paint(txt, background="card_bg", foreground="fg",
                                insertbackground="fg", highlightbackground="card_border")
                    txt.insert("1.0", value)
                    txt.edit_modified(False)
                    txt.bind("<<Modified>>", lambda e, t=txt: self._on_text_modified(t))
                    txt.grid(row=r, column=0, sticky="ew")
                    self._texts[spec.key] = txt
                else:
                    var = tk.StringVar(value=value)
                    var.trace_add("write", lambda *_a: self._on_field_change())
                    ttk.Entry(self.form, textvariable=var).grid(row=r, column=0, sticky="ew")
                    self._vars[spec.key] = var
                r += 1
        finally:
            self._building = False
        self._store_values()

    def _entry_font(self) -> str:
        """Шрифт полей ввода ttk (на Windows тема отдаёт пустую строку - берём шрифт окна)."""
        try:
            font = ttk.Style(self.frame).lookup("TEntry", "font")
        except tk.TclError:
            font = ""
        return str(font or "TkDefaultFont")

    def field_values(self) -> dict[str, str]:
        """Текущие значения полей формы."""
        out = {k: v.get() for k, v in self._vars.items()}
        for k, t in self._texts.items():
            out[k] = t.get("1.0", "end-1c")
        return out

    def form_keys(self) -> list[str]:
        """Ключи полей, которые сейчас в форме (в порядке формы)."""
        return [s.key for s in self._text_specs()
                if s.key in self._vars or s.key in self._texts]

    def set_field(self, key: str, value: str) -> None:
        """Записать значение в поле формы (как будто его набрали)."""
        if key in self._vars:
            self._vars[key].set(value)
        elif key in self._texts:
            t = self._texts[key]
            t.delete("1.0", "end")
            t.insert("1.0", value)
            self._on_field_change()
        else:
            raise KeyError("В шаблоне %s нет поля %r" % (self.template_id, key))

    def fill_examples(self) -> None:
        """Пустые поля - примером из шаблона."""
        values = self.field_values()
        for key, example in poster.example_fields(self.template_id).items():
            if not values.get(key, "").strip():
                self.set_field(key, example)

    def _store_values(self) -> None:
        self.st["fields"][self.template_id] = self.field_values()

    def _on_text_modified(self, txt: tk.Text) -> None:
        if txt.edit_modified():
            txt.edit_modified(False)
            self._on_field_change()

    def _on_field_change(self) -> None:
        if self._building:
            return
        self._store_values()
        self.request_preview()

    # ------------------------------------------------------------------
    # Шаблон и формат
    # ------------------------------------------------------------------

    def _on_template_combo(self, _event: Any = None) -> None:
        title = self.template_var.get()
        for tid, t in self._titles.items():
            if t == title:
                self.select_template(tid)
                return

    def select_template(self, template_id: str) -> None:
        """Сменить шаблон: поля текущего запоминаются, форма строится заново."""
        if template_id not in self._titles:
            raise ValueError("Нет шаблона %r" % template_id)
        if template_id == self.template_id:
            return
        current = self.field_values()
        old = self.template_id
        self._store_values()
        self.template_id = template_id
        self.st["template"] = template_id
        self.template_var.set(self._titles[template_id])
        self._build_form(self._initial_values(template_id, current, old))
        self._update_format_state()
        if self.face is not None and not self._zoom_manual:
            self._apply_face(self.face)         # приближение зависит от блока фото шаблона
        self._update_focus_controls()
        self._draw_focus()
        self.ctx.save_settings()
        self.request_preview(immediate=True)

    def _update_format_state(self) -> None:
        pdf_ok = self.template_id in PDF_TEMPLATES
        self.format_buttons["pdf"].configure(state="normal" if pdf_ok else "disabled")
        if not pdf_ok and self.format_var.get() == "pdf":
            self.format_var.set("png")
        self.st["format"] = self.format_var.get()

    def _on_format(self) -> None:
        self.st["format"] = self.format_var.get()

    # ------------------------------------------------------------------
    # Фото, фокус, лента «Отбора»
    # ------------------------------------------------------------------

    def _get_photo(self, path: Path):
        """Фото для предпросмотра из кэша или с диска.  Любой поток."""
        key = str(path)
        with self._cache_lock:
            im = self._photo_cache.get(key)
        if im is None:
            im = _load_photo(path, PREVIEW_PHOTO_SIDE)
            with self._cache_lock:
                if len(self._photo_cache) >= 4:
                    self._photo_cache.clear()
                self._photo_cache[key] = im
        return im

    def _pick_photo(self) -> None:
        paths = gc.pick_files(self.ctx, TAB_KEY, title="Выберите снимок для афиши",
                              parent=self.frame, multiple=False)
        if paths:
            self.set_photo(paths[0])

    def set_photo(self, path: str | Path | None, *, origin: str | Path | None = None) -> None:
        """Выбрать фото (None - без фото, будет заглушка).  Фокус сбрасывается.

        origin - снимок съёмки, если path - его обработанная версия.  Лицо берётся
        из подсказки «Отбора» (ctx.photo_hint), а если её нет - ищется в рабочем
        потоке; найденное лицо сразу задаёт фокус и приближение.
        """
        self.photo_path = Path(path) if path else None
        self.photo_origin = Path(origin) if (origin and path) else self.photo_path
        self.photo_processed = (self.photo_origin is not None and self.photo_path is not None
                                and gc.path_key(self.photo_origin) != gc.path_key(self.photo_path))
        self.focus = None
        self.zoom = 1.0
        self.zoom_var.set(1.0)
        self.face = None
        self.faces = []
        self._user_focus = False
        self._zoom_manual = False
        self._focus_thumb = None
        self._focus_tk = None
        if self._thumb_job is not None:
            self._thumb_job.cancel()
            self._thumb_job = None
        if self.photo_path is None:
            self._paint(self.photo_label, foreground="muted")
            self.photo_label.configure(text="Фото не выбрано - будет заглушка")
        else:
            text = self.photo_path.name
            if self.photo_processed:
                text += " — обработанный (из «Обработки»)"
            elif self.photo_origin is not None and self.ctx.processed_for(self.photo_origin):
                text += " — оригинал (есть обработанный: выберите его в ленте)"
            self._paint(self.photo_label, foreground="fg")
            self.photo_label.configure(text=text)
            hint = None
            for key in (self.photo_origin, self.photo_path):
                box = self.ctx.photo_hint(key).get("face_box") if key is not None else None
                if box is not None and len(box) == 4:
                    hint = tuple(float(v) for v in box)
                    break
            path_now = self.photo_path
            side = max(self.ctx.px(FOCUS_THUMB[0]), self.ctx.px(FOCUS_THUMB[1]))

            def work(report: gc.Reporter):
                photo = self._get_photo(path_now)
                thumb = gc.thumbnail(photo, side)
                report.check()
                faces: list = []
                if hint is None:
                    try:
                        import cull
                        faces = cull.find_faces(photo)
                    except Exception:                   # noqa: BLE001 - лица необязательны
                        faces = []
                return thumb, faces

            def done(result: Any) -> None:
                if self.photo_path != path_now:
                    return
                img, found = result
                self._focus_thumb = img
                self.faces = [hint] if hint is not None else list(found)
                if self.faces and not self._user_focus:
                    self._apply_face(self.faces[0])
                else:
                    self._draw_focus()

            def failed(exc: BaseException) -> None:
                if self.photo_path == path_now:
                    self._paint(self.photo_label, foreground="error")
                    self.photo_label.configure(text=str(exc))

            self._thumb_job = self._run(work, on_done=done, on_error=failed,
                                        name="poster-thumb")
        self._highlight_strip()
        self._update_focus_controls()
        self._draw_focus()
        self.request_preview(immediate=True)

    def _apply_face(self, face: tuple[float, float, float, float]) -> None:
        """Кадрировать по лицу: фокус в его центре, приближение - auto_zoom."""
        self.face = tuple(float(v) for v in face)            # type: ignore[assignment]
        self._zoom_manual = False
        self.focus = (self.face[0] + self.face[2] / 2.0, self.face[1] + self.face[3] / 2.0)
        w, h = self._photo_size()
        self.zoom = auto_zoom(self.template_id, w, h, self.face) if w and h else 1.0
        self.zoom_var.set(self.zoom)
        self._update_focus_controls()
        self._draw_focus()
        self.request_preview(immediate=True)

    def _photo_size(self) -> tuple[int, int]:
        """Размер миниатюры (пропорции кадра) или (0, 0), пока её нет."""
        im = self._focus_thumb
        return (im.width, im.height) if im is not None else (0, 0)

    def set_face(self, face: tuple[float, float, float, float] | None) -> None:
        """Кадрировать по этому лицу (x, y, w, h в долях кадра); None - как set_focus(None)."""
        self._user_focus = True
        if face is None:
            self.set_focus(None)
            return
        self._apply_face(face)

    def set_focus(self, focus: tuple[float, float] | None) -> None:
        """Точка фокуса в долях кадра (None - кадрировать по умолчанию).

        Точка - не лицо: кадрирование по лицу (set_face) при этом снимается.
        """
        self._user_focus = True
        if focus is not None:
            focus = (min(max(float(focus[0]), 0.0), 1.0),
                     min(max(float(focus[1]), 0.0), 1.0))
        else:
            self.zoom = 1.0
            self.zoom_var.set(1.0)
        self.focus = focus
        self.face = None
        self._update_focus_controls()
        self._draw_focus()
        self.request_preview(immediate=True)

    def set_zoom(self, zoom: float) -> None:
        """Приближение к точке фокуса (1.0 - без приближения)."""
        self._user_focus = True
        self._zoom_manual = True
        self.zoom = max(1.0, float(zoom))
        if abs(self.zoom_var.get() - self.zoom) > 1e-6:
            self.zoom_var.set(self.zoom)
        self._update_focus_controls()
        self._draw_focus()
        if self.focus is not None:
            self.request_preview()

    def _on_zoom_scale(self, _value: str) -> None:
        z = round(float(self.zoom_var.get()), 1)
        if abs(z - self.zoom) >= 0.05:
            self.set_zoom(z)

    def _update_focus_controls(self) -> None:
        block = _image_block(self.template_id) or {}
        top = max(1.0, float(block.get("max_zoom", 1.0)))
        self.zoom_scale.configure(to=top)
        if self.zoom > top:
            self.zoom = top
            self.zoom_var.set(top)
        text = "Приближение: %.1f×" % self.zoom
        w, h = self._photo_size()
        if self.face is not None and w and h:
            real = effective_zoom(self.template_id, w, h, self.zoom, self.face)
            if real < self.zoom - 0.05:
                text = "Приближение: %.1f× (больше — обрезало бы голову)" % real
        self.zoom_label.configure(text=text)
        has_focus = self.focus is not None
        self.zoom_scale.state(["!disabled"] if has_focus and top > 1.0 else ["disabled"])
        self.reset_focus_btn.state(["!disabled"] if has_focus else ["disabled"])

    def _draw_focus(self) -> None:
        c = self.focus_canvas
        c.delete("all")
        cw, ch = self.ctx.px(FOCUS_THUMB[0]), self.ctx.px(FOCUS_THUMB[1])
        if self._focus_thumb is None:
            text = ("Загрузка снимка…" if self.photo_path is not None
                    else "Выберите фотографию")
            c.create_text(cw // 2, ch // 2, text=text, fill=self._color("muted"))
            return
        im = self._focus_thumb
        key = (id(self._focus_thumb), cw, ch)
        k = min(cw / im.width, ch / im.height, 1.0)
        if k < 1.0:
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
        if self._focus_tk is None or self._focus_key != key:
            self._focus_tk = gc.photo_image(im, master=c)
            self._focus_key = key
        x0, y0 = (cw - im.width) // 2, (ch - im.height) // 2
        self._focus_geom = (x0, y0, im.width, im.height)
        c.create_image(x0, y0, image=self._focus_tk, anchor="nw")
        accent = self._color("accent")
        crop = _crop_on_image(self.template_id, im.width, im.height, self.focus, self.zoom,
                              self.face)
        if crop is not None:
            l, t, r, b = crop
            c.create_rectangle(x0 + l, y0 + t, x0 + r - 1, y0 + b - 1, outline=accent,
                               width=2, dash=(4, 3))
        for face in self.faces:
            fl, ft, fr, fb = _face_px(face, im.width, im.height)
            c.create_rectangle(x0 + fl, y0 + ft, x0 + fr, y0 + fb, width=1,
                               outline=accent if face == self.face else self._color("muted"))
        if self.focus is not None:
            fx = x0 + self.focus[0] * im.width
            fy = y0 + self.focus[1] * im.height
            s = self.ctx.px(7)
            c.create_line(fx - s, fy, fx + s, fy, fill=accent, width=2)
            c.create_line(fx, fy - s, fx, fy + s, fill=accent, width=2)

    def _on_focus_click(self, event: Any) -> None:
        if self._focus_thumb is None:
            return
        x0, y0, w, h = self._focus_geom
        if not (x0 <= event.x < x0 + w and y0 <= event.y < y0 + h):
            return
        fx, fy = (event.x - x0) / max(w, 1), (event.y - y0) / max(h, 1)
        for face in self.faces:
            x, y, fw, fh = face
            if (x - FACE_SNAP * fw <= fx <= x + fw * (1 + FACE_SNAP)
                    and y - FACE_SNAP * fh <= fy <= y + fh * (1 + FACE_SNAP)):
                self.set_face(face)
                return
        self.set_focus((fx, fy))

    def _on_processed(self, _mapping: Any) -> None:
        """«Обработка» закончила пакет: лента берёт обработанные файлы."""
        if self._strip_shown:
            self.show_selection_strip()

    def _on_selection_changed(self, paths: Any) -> None:
        n = len(paths or ())
        self.from_sel_btn.configure(
            text="Из отмеченных в «Отборе» (%d)" % n if n else "Из отмеченных в «Отборе»")
        if self._strip_shown:
            self.show_selection_strip()

    def show_selection_strip(self) -> None:
        """Показать ленту миниатюр отмеченных в «Отборе» кадров."""
        self.strip_frame.grid()
        self._strip_shown = True
        origins = [p for p in self.ctx.selection
                   if p.suffix.lower() in gc.IMAGE_EXTENSIONS][:STRIP_LIMIT]
        # (файл для афиши, снимок съёмки): обработанный - если «Обработка» его сделала.
        paths = [(self.ctx.processed_for(o) or o, o) for o in origins]
        self._strip_gen += 1
        gen = self._strip_gen
        if self._strip_job is not None:
            self._strip_job.cancel()
            self._strip_job = None
        self._strip_items = []
        self._strip_tk = []
        self.strip_canvas.delete("all")
        self._paint(self.strip_status, foreground="muted")
        if not paths:
            self.strip_status.configure(
                text="В «Отборе» пока ничего не отмечено: отметьте лучшие кадры "
                     "там или выберите файл.")
            return
        self.strip_status.configure(text="Загрузка миниатюр: %d…" % len(paths))
        side = self.ctx.px(STRIP_THUMB)

        def work(report: gc.Reporter):
            out: list[tuple[Path, Any, Path, bool]] = []
            failed = 0
            for i, (use, origin) in enumerate(paths):
                report.check()
                try:
                    out.append((use, gc.thumbnail(_load_photo(use, side * 2), side), origin,
                                use != origin))
                except gc.ImageLoadError:
                    failed += 1
                report((i + 1) / len(paths), "")
            return out, failed

        def done(result: Any) -> None:
            if gen != self._strip_gen:
                return
            items, failed = result
            self._strip_items = items
            self._draw_strip()
            text = "Кадров: %d." % len(items)
            processed = sum(1 for item in items if item[3])
            if processed == len(items) and items:
                text += " Все - обработанные в «Обработке»."
            elif processed:
                text += " Обработанных: %d, остальные - оригиналы." % processed
            else:
                text += " Это оригиналы: «Обработку» они ещё не проходили."
            if failed:
                text += " Не открылись: %d." % failed
            self.strip_status.configure(text=text + " Щёлкните по кадру, чтобы выбрать.")

        def error(exc: BaseException) -> None:
            if gen == self._strip_gen:
                self._paint(self.strip_status, foreground="error")
                self.strip_status.configure(text=str(exc))

        self._strip_job = self._run(work, on_done=done, on_error=error, name="poster-strip")

    def _draw_strip(self) -> None:
        c = self.strip_canvas
        c.delete("all")
        self._strip_tk = []
        pad = self.ctx.px(4)
        side = self.ctx.px(STRIP_THUMB)
        x = pad
        for i, (_path, im, _origin, _processed) in enumerate(self._strip_items):
            ph = gc.photo_image(im, master=c)
            self._strip_tk.append(ph)
            c.create_image(x + (side - im.width) // 2, pad + (side - im.height) // 2,
                           image=ph, anchor="nw", tags=("thumb", "i%d" % i))
            c.create_rectangle(x - 2, pad - 2, x + side + 1, pad + side + 1, width=2,
                               outline="", tags=("frame", "f%d" % i))
            x += side + pad
        c.configure(scrollregion=(0, 0, x, side + 2 * pad))
        self._highlight_strip()

    def _highlight_strip(self) -> None:
        c = self.strip_canvas
        for i, (path, _im, _origin, _processed) in enumerate(self._strip_items):
            on = self.photo_path is not None and path == self.photo_path
            c.itemconfigure("f%d" % i, outline=self._color("accent") if on else "")

    def _on_strip_click(self, event: Any) -> None:
        x = self.strip_canvas.canvasx(event.x)
        pad = self.ctx.px(4)
        step = self.ctx.px(STRIP_THUMB) + pad
        i = int((x - pad) // step) if x >= pad else -1
        if 0 <= i < len(self._strip_items):
            use, _im, origin, _processed = self._strip_items[i]
            self.set_photo(use, origin=origin)

    # ------------------------------------------------------------------
    # Шрифт заголовков
    # ------------------------------------------------------------------

    def _pick_display_font(self) -> None:
        types = [("Шрифты", ("*.ttf", "*.TTF", "*.otf", "*.OTF")), ("Все файлы", "*")]
        paths = self._with_dir("font_dir", lambda: gc.pick_files(
            self.ctx, TAB_KEY, title="Файл шрифта Morfin Sans", parent=self.frame,
            filetypes=types, multiple=False))
        if paths:
            self.set_display_font(paths[0])

    def set_display_font(self, path: str | Path | None) -> None:
        """Файл шрифта заголовков (None - искать самим).  Запоминается."""
        if path:
            self.st["display_font"] = os.path.normpath(str(path))
            self.reset_font_btn.grid()
        else:
            self.st.pop("display_font", None)
            self.reset_font_btn.grid_remove()
        self.ctx.save_settings()
        self.request_preview(immediate=True)

    def banner_visible(self) -> bool:
        """Показана ли плашка «заголовок набран заменой»."""
        return bool(self.banner.winfo_manager())

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        """Всё, что нужно рабочему потоку: только строки, числа и пути."""
        return {
            "template": self.template_id,
            "fields": self.field_values(),
            "photo": self.photo_path,
            "focus": self.focus,
            "zoom": self.zoom,
            "face": self.face,
            "display_font": self.st.get("display_font") or None,
        }

    def request_preview(self, immediate: bool = False) -> None:
        """Перерисовать предпросмотр (с задержкой, чтобы не мешать набору)."""
        self.generation += 1
        if self._debounce_id is not None:
            try:
                self.frame.after_cancel(self._debounce_id)
            except tk.TclError:
                pass
            self._debounce_id = None
        if self.ctx.closing:
            return
        try:
            self._debounce_id = self.frame.after(10 if immediate else DEBOUNCE_MS,
                                                 self._on_debounce)
        except tk.TclError:
            self._debounce_id = None

    def _on_debounce(self) -> None:
        self._debounce_id = None
        self._kick_preview()

    def _preview_box(self) -> tuple[int, int]:
        c = self.preview_canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            w, h = self.ctx.px(PREVIEW_MIN_BOX[0]), self.ctx.px(PREVIEW_MIN_BOX[1])
        return w, h

    def _kick_preview(self) -> None:
        if self._preview_job is not None and not self._preview_job.finished:
            return                              # допишет - запустим снова
        gen = self.generation
        snap = self._snapshot()
        box = self._preview_box()
        self._rendered_box = box
        self._paint(self.preview_status, foreground="muted")
        self.preview_status.configure(text="Обновляется…")

        def work(report: gc.Reporter) -> dict[str, Any]:
            tid = snap["template"]
            W, H = poster.TEMPLATES[tid]["size"]
            k = min(box[0] / W, box[1] / H, PREVIEW_MAX_SIDE / max(W, H))
            size = (max(64, int(round(W * k))), max(64, int(round(H * k))))
            photo = None
            face = None
            if snap["photo"] is not None:
                photo = self._get_photo(snap["photo"])
                face = focus_face_box(tid, photo.width, photo.height,
                                      snap["focus"], snap["zoom"], snap["face"])
            report.check()
            with _RENDER_LOCK:
                report.check()
                img = poster.render(tid, snap["fields"], photo, face_box=face,
                                    display_font=snap["display_font"], size=size)
            rep = poster.render_report(img)
            usage = poster.usage_report(img)
            return {"gen": gen, "image": img, "report": rep, "usage": usage,
                    "balance": poster.check_60_30_10(usage)}

        def finish() -> None:
            self._preview_job = None
            if self.generation != gen and self._debounce_id is None:
                self._kick_preview()

        def done(res: dict[str, Any]) -> None:
            self._show_preview(res)
            finish()

        def error(exc: BaseException) -> None:
            if isinstance(exc, (ValueError, gc.ImageLoadError, poster.FontNotFoundError)):
                msg = str(exc)
            else:
                self.ctx.record_error("предпросмотр афиши", exc)
                msg = "Не удалось нарисовать предпросмотр: %s" % exc
            self.preview_error = msg
            self.shown_generation = gen
            self._paint(self.preview_status, foreground="error")
            self.preview_status.configure(text=msg)
            finish()

        def cancelled(_res: Any) -> None:
            finish()

        self._preview_job = self._run(work, on_done=done, on_error=error,
                                      on_cancelled=cancelled, name="poster-preview")
        if self._preview_job is None:
            self.preview_status.configure(text="")

    def _show_preview(self, res: dict[str, Any]) -> None:
        self.preview_image = res["image"]
        self.last_report = res["report"]
        self.last_usage = res["usage"]
        self.preview_error = ""
        self.shown_generation = res["gen"]
        self.preview_status.configure(text="")
        self._draw_preview()
        self._draw_usage()
        self._show_analysis(res["report"], res["balance"])

    def _draw_preview(self) -> None:
        c = self.preview_canvas
        c.delete("all")
        if self.preview_image is None:
            return
        cw, ch = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        im = self.preview_image
        k = min(cw / im.width, ch / im.height)
        if k < 1.0:
            from PIL import Image
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))),
                           Image.Resampling.LANCZOS)
        self._preview_tk = gc.photo_image(im, master=c)
        c.create_image(max(cw, im.width) // 2, max(ch, im.height) // 2,
                       image=self._preview_tk, anchor="center")
        c.create_rectangle((cw - im.width) // 2 - 1, (ch - im.height) // 2 - 1,
                           (cw + im.width) // 2, (ch + im.height) // 2,
                           outline=self._color("card_border"))

    def _on_preview_resize(self, event: Any) -> None:
        if self._resize_id is not None:
            try:
                self.frame.after_cancel(self._resize_id)
            except tk.TclError:
                pass
        self._resize_id = self.frame.after(150, self._after_resize)

    def _after_resize(self) -> None:
        self._resize_id = None
        self._draw_preview()
        w, h = self._preview_box()
        rw, rh = self._rendered_box
        if rw <= 0 or abs(w - rw) > 0.12 * rw or abs(h - rh) > 0.12 * rh:
            self.request_preview()

    def _draw_usage(self) -> None:
        c = self.usage_canvas
        c.delete("all")
        width = max(c.winfo_width(), self.ctx.px(300))
        fg, muted = self._color("fg"), self._color("muted")
        border = self._color("card_border")
        u = self.last_usage
        layout = max(1e-6, 1.0 - float(u.get("photo", 0.0))) if u else 1.0
        rows = [("cream", "Кремовый", brand.CREAM), ("graphite", "Графит", brand.GRAPHITE),
                ("accent", "Акценты", brand.CRIMSON)]
        px = self.ctx.px
        label_w, value_w, row_h = px(92), px(120), px(20)
        bar_x0, bar_x1 = label_w, max(label_w + px(40), width - value_w)
        for i, (key, name, hexcolor) in enumerate(rows):
            y = i * row_h + px(2)
            yc = y + row_h // 2
            c.create_text(0, yc, text=name, anchor="w", fill=fg)
            c.create_rectangle(bar_x0, y + px(4), bar_x1, y + row_h - px(4),
                               outline=border, fill=self._color("card_bg"))
            lo, hi = poster.BALANCE_LIMITS[key]
            target = brand.USAGE_RULE[key]
            span = bar_x1 - bar_x0
            if u is not None:
                if key == "accent":
                    crim = float(u.get("crimson", 0.0)) / layout
                    sand = float(u.get("sand", 0.0)) / layout
                    share = crim + sand
                    xa = bar_x0 + span * min(crim, 1.0)
                    xb = bar_x0 + span * min(share, 1.0)
                    if xa > bar_x0:
                        c.create_rectangle(bar_x0, y + px(4), xa, y + row_h - px(4),
                                           outline="", fill=brand.CRIMSON)
                    if xb > xa:
                        c.create_rectangle(xa, y + px(4), xb, y + row_h - px(4),
                                           outline="", fill=brand.SAND)
                else:
                    share = float(u.get(key, 0.0)) / layout
                    xb = bar_x0 + span * min(share, 1.0)
                    if xb > bar_x0:
                        c.create_rectangle(bar_x0, y + px(4), xb, y + row_h - px(4),
                                           outline=border, fill=hexcolor)
                ok = lo <= share <= hi
                c.create_text(width - 2, yc, anchor="e", fill=self._color("ok" if ok else "warn"),
                              text="%d %% (норма %d %%)" % (round(share * 100), round(target * 100)))
            for v, dash in ((lo, (2, 2)), (hi, (2, 2)), (target, ())):
                x = bar_x0 + span * v
                c.create_line(x, y + px(1), x, y + row_h - px(1),
                              fill=fg if not dash else muted, dash=dash)

    def _show_analysis(self, rep: poster.RenderReport | None, balance: list[str]) -> None:
        labels = {s.key: s.label for s in poster.template_fields(self.template_id)}
        warnings: list[str] = []
        if rep is not None:
            for w in rep.warnings:
                if not _is_substitute_note(w):
                    warnings.append(_pretty_warning(w, labels))
            for pair in rep.text_pairs:
                if not pair.ok:
                    warnings.append("Поле «%s»: контраст %.2f:1 ниже нормы %.1f:1." % (
                        labels.get(pair.field, pair.field), pair.ratio, pair.required))
        warnings.extend(balance)
        if self.last_usage and self.last_usage.get("photo"):
            photo_note = "Фото занимает %d %% листа." % round(self.last_usage["photo"] * 100)
        else:
            photo_note = ""
        self.last_warnings = warnings
        if warnings:
            self._paint(self.warn_label, foreground="warn")
            self.warn_label.configure(text="\n".join("• " + w for w in warnings))
        else:
            self._paint(self.warn_label, foreground="ok")
            self.warn_label.configure(
                text="Баланс 60/30/10, контраст и размер текста в норме. " + photo_note)

        if rep is None:
            return
        disp, text = rep.fonts.get("display"), rep.fonts.get("text")
        if disp is not None and disp.is_substitute:
            self.banner_label.configure(
                text="Заголовок набран %s — укажите файл %s" % (
                    _short_family(disp.family), disp.brand_family))
            self.banner.grid()
        else:
            self.banner.grid_remove()
        parts = []
        if disp is not None:
            parts.append("заголовки - " + disp.label)
        if text is not None:
            parts.append("текст - " + text.label)
        self.fonts_label.configure(text="Шрифты: " + "; ".join(parts) + ".")

    # ------------------------------------------------------------------
    # Экспорт
    # ------------------------------------------------------------------

    def _with_dir(self, key: str, fn: Callable[[], Any]) -> Any:
        """Диалог с собственной «последней папкой» (экспорт, шрифты).

        Иначе диалог сохранения открывался бы в папке снимков - а туда
        класть результат не надо.
        """
        saved = self.st.get(gc.LAST_DIR_KEY)
        own = self.st.get(key)
        if own:
            self.st[gc.LAST_DIR_KEY] = own
        else:
            self.st.pop(gc.LAST_DIR_KEY, None)
        try:
            result = fn()
        finally:
            new_dir = self.st.get(gc.LAST_DIR_KEY)
            if new_dir and new_dir != own:
                self.st[key] = new_dir
            if saved:
                self.st[gc.LAST_DIR_KEY] = saved
            else:
                self.st.pop(gc.LAST_DIR_KEY, None)
        return result

    def _missing_required(self, template_id: str, fields: dict[str, str]) -> list[str]:
        return [s.label for s in self._text_specs(template_id)
                if s.required and not fields.get(s.key, "").strip()]

    def _set_export_status(self, text: str, role: str = "muted") -> None:
        self._paint(self.export_status, foreground=role)
        self.export_status.configure(text=text)

    def _export_problem(self, target: Path) -> str:
        """export_problem для текущего фото, отмеченных кадров и своих прошлых файлов."""
        files = [p for p in (self.photo_path, self.photo_origin) if p is not None]
        files += list(self.ctx.selection)
        folders = {gc.path_key(p.parent): p.parent
                   for p in ([self.photo_origin] if self.photo_origin else []) + self.ctx.selection}
        exported = self.st.get("exported")
        keys = frozenset(k for k in exported if isinstance(k, str)) \
            if isinstance(exported, list) else frozenset()
        return export_problem(target, files, list(folders.values()), keys)

    def _remember_exported(self, paths: list[Path]) -> None:
        """Запомнить свои файлы: их можно заменить следующим экспортом."""
        old = self.st.get("exported")
        keys = [k for k in old if isinstance(k, str)] if isinstance(old, list) else []
        for p in paths:
            key = cr2_core._dst_key(p)                      # noqa: SLF001
            if key in keys:
                keys.remove(key)
            keys.append(key)
        self.st["exported"] = keys[-EXPORTED_MEMORY:]
        self.ctx.save_settings()

    def _on_export(self) -> None:
        fmt = self.format_var.get()
        fields = self.field_values()
        missing = self._missing_required(self.template_id, fields)
        if missing:
            self._set_export_status("Заполните: " + ", ".join(missing) + ".", "error")
            return
        ext, label = FORMATS[fmt]
        types = [(label, ("*" + ext, "*" + ext.upper())), ("Все файлы", "*")]
        target = self._with_dir("export_dir", lambda: gc.pick_save_file(
            self.ctx, TAB_KEY, title="Сохранить афишу",
            initialfile=export_file_name(self.template_id, fields, fmt),
            defaultextension=ext, filetypes=types, parent=self.frame))
        if target is None:
            return
        self.export(target, fmt)

    def _on_export_all(self) -> None:
        folder = self._with_dir("export_dir", lambda: gc.pick_folder(
            self.ctx, TAB_KEY, title="Папка для поста, сторис и обложки",
            parent=self.frame))
        if folder is not None:
            self.export_all(folder)

    def export(self, path: str | Path, fmt: str | None = None) -> gc.BackgroundJob | None:
        """Экспорт текущего шаблона в полном размере (в рабочем потоке)."""
        fmt = fmt or self.format_var.get()
        if fmt not in FORMATS:
            raise ValueError("Формат %r не поддерживается" % fmt)
        if fmt == "pdf" and self.template_id not in PDF_TEMPLATES:
            self._set_export_status("PDF - только для афиши A3.", "error")
            return None
        target = Path(path)
        ext = FORMATS[fmt][0]
        valid = (".jpg", ".jpeg") if fmt == "jpeg" else (ext,)
        if target.suffix.lower() not in valid:
            target = target.with_name(target.name + ext)
        fields = self.field_values()
        missing = self._missing_required(self.template_id, fields)
        if missing:
            self._set_export_status("Заполните: " + ", ".join(missing) + ".", "error")
            return None
        return self._start_export([(self.template_id, fields, target, fmt)])

    def export_all(self, folder: str | Path, fmt: str | None = None
                   ) -> gc.BackgroundJob | None:
        """Пост, сторис и обложка альбома из текущих полей - в одну папку."""
        fmt = fmt or self.format_var.get()
        if fmt not in ("png", "jpeg"):
            fmt = "jpeg"                        # PDF у соцсетей не бывает
        folder = Path(folder)
        current = self.field_values()
        cur_labels = {s.key: s.label for s in self._text_specs()}
        tasks = []
        problems = []
        for tid in SOCIAL_TEMPLATES:
            saved = self.st["fields"].get(tid)
            fields = {k: str(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
            for spec in self._text_specs(tid):
                v = current.get(spec.key, "")
                if cur_labels.get(spec.key) == spec.label and v.strip():
                    fields[spec.key] = v
            fields = {s.key: fields.get(s.key, "") for s in self._text_specs(tid)}
            missing = self._missing_required(tid, fields)
            if missing:
                problems.append("%s: %s" % (self._titles[tid], ", ".join(missing)))
                continue
            target = unique_path(folder / export_file_name(tid, fields, fmt))
            tasks.append((tid, fields, target, fmt))
        if problems:
            self._set_export_status("Не заполнены обязательные поля - " +
                                    "; ".join(problems) + ".", "error")
            return None
        return self._start_export(tasks)

    def _start_export(self, tasks: list[tuple[str, dict[str, str], Path, str]]
                      ) -> gc.BackgroundJob | None:
        if self._export_job is not None and not self._export_job.finished:
            self._set_export_status("Экспорт уже идёт.", "warn")
            return None
        for _tid, _f, target, _fmt in tasks:
            problem = self._export_problem(target)
            if problem:
                self._set_export_status(problem, "error")
                return None
        photo_path, focus, zoom, face_frac = self.photo_path, self.focus, self.zoom, self.face
        font = self.st.get("display_font") or None
        titles = self._titles

        def work(report: gc.Reporter) -> list[Path]:
            written: list[Path] = []
            photo = None
            n = len(tasks)
            for i, (tid, fields, target, fmt) in enumerate(tasks):
                title = titles[tid]
                if photo_path is not None and photo is None:
                    report(i / n, "Загрузка снимка в полном размере…")
                    photo = _load_photo(photo_path, None)
                report.check()
                report((i + 0.15) / n, "%s: отрисовка…" % title)
                face = (focus_face_box(tid, photo.width, photo.height, focus, zoom, face_frac)
                        if photo is not None else None)
                with _RENDER_LOCK:
                    report.check()
                    img = poster.render(tid, fields, photo, face_box=face,
                                        display_font=font)
                report.check()
                report((i + 0.7) / n, "%s: запись %s…" % (title, target.name))
                written.append(poster.export(img, target, fmt))
                report((i + 1) / n, "%s: готово" % title)
            return written

        def progress(fraction: float | None, text: str) -> None:
            if fraction is not None:
                self.progress.configure(value=fraction)
            if text:
                self._set_export_status(text)

        def finish() -> None:
            self.export_btn.state(["!disabled"])
            self.export_all_btn.state(["!disabled"])
            self.cancel_btn.state(["disabled"])

        def done(paths: list[Path]) -> None:
            finish()
            self.last_export_paths = list(paths)
            self._remember_exported(self.last_export_paths)
            self.progress.configure(value=1.0)
            names = ", ".join(p.name for p in paths)
            self._set_export_status("Сохранено: %s" % names, "ok")
            self.reveal_btn.state(["!disabled"])
            self.ctx.log("Афиши: сохранено %s" % names, "ok")

        def error(exc: BaseException) -> None:
            finish()
            self.progress.configure(value=0.0)
            if isinstance(exc, (ValueError, OSError, gc.ImageLoadError,
                                poster.FontNotFoundError)):
                self._set_export_status("Экспорт не удался: %s" % exc, "error")
                self.ctx.record_error("экспорт афиши", exc)
                self.ctx.log("Афиши: экспорт не удался: %s" % exc, "error")
            else:
                self._set_export_status("Экспорт не удался: %s" % exc, "error")
                self.ctx.show_error("экспорт афиши", exc)

        def cancelled(_res: Any) -> None:
            finish()
            self.progress.configure(value=0.0)
            self._set_export_status("Экспорт отменён.", "warn")

        self.export_btn.state(["disabled"])
        self.export_all_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.progress.configure(value=0.0)
        self._set_export_status("Экспорт…")
        self._export_job = self._run(work, on_progress=progress, on_done=done,
                                     on_error=error, on_cancelled=cancelled,
                                     name="poster-export")
        if self._export_job is None:
            finish()
        return self._export_job

    def _cancel_export(self) -> None:
        if self._export_job is not None:
            self._export_job.cancel()

    def _reveal_export(self) -> None:
        if self.last_export_paths:
            self.ctx.reveal(self.last_export_paths[-1])

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def _run(self, fn: Callable[[gc.Reporter], Any], **kwargs: Any
             ) -> gc.BackgroundJob | None:
        """ctx.run_background, который молчит, пока программа закрывается."""
        try:
            return self.ctx.run_background(fn, **kwargs)
        except RuntimeError:
            if self.ctx.closing:
                return None
            raise

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
        self._wheel_off()
        for job in (self._preview_job, self._export_job, self._thumb_job, self._strip_job):
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
    return PosterTab(parent, ctx).frame


if __name__ == "__main__":          # pragma: no cover - разработка вкладки
    sys.exit(gc.run_standalone(sys.modules[__name__]))
