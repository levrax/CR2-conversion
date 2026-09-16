# -*- coding: utf-8 -*-
"""poster - фирменные афиши и посты пресс-службы Юридического института РУДН.

Берёт готовый кадр (JPEG из конвертера) и собирает из него носитель по
брендбуку: афишу A3, пост, сторис, обложку альбома, итоги мероприятия,
цитату спикера.

ДИЗАЙН-СИСТЕМА
--------------
Брендбук называет ровно два графических элемента:

  1. ХАЛФТОН        - фото превращается в растр из точек (brand.halftone).
  2. ПРЯМОУГОЛЬНИКИ - «основная и единственная геометрическая фигура».

Поэтому в схеме шаблона нет ни кругов, ни скруглений, ни линий под углом:
любой блок - прямоугольник, выровненный по осям.  Каждый нарисованный блок
записывается в отчёт отрисовки, и тест проверяет, что вне объявленных блоков
нет ни одного пикселя, кроме фона.

Цвета и правило 60/30/10 - см. brand.py.  usage_report() меряет реальную
долю площади каждого цвета на готовом изображении, check_60_30_10() выдаёт
предупреждения по-русски.  Фотография в баланс не входит: она измеряется
отдельно, а 60/30/10 проверяется на остальной площади листа.

ШРИФТЫ
------
    заголовки  Morfin Sans (брендбук).  Лицензия автора - «Completely free
               font / Credit is highly appreciated»: ни слова о праве
               распространять файл.  Поэтому в репозитории и в сборке его НЕТ.
               Движок берёт Morfin Sans, если пользователь указал файл или
               шрифт установлен, иначе Oswald Bold (SIL OFL, в папке fonts/).
    текст      Radiant Alt (брендбук) - коммерческая и БЕЗ кириллицы, для
               русского текста непригодна в принципе.  Замена - Fira Sans
               Extra Condensed (SIL OFL, в папке fonts/).

render() всегда сообщает, какие гарнитуры реально использованы
(RenderReport.fonts), и кладёт предупреждение «заголовок набран заменой»,
если брендовый шрифт не найден.

Публичный API
-------------
    TEMPLATES, list_templates(), template_fields(), example_fields()
    render(template_id, fields, photo, *, face_box=None, ...) -> PIL.Image
    render_report(image) -> RenderReport | None
    usage_report(image, photo_boxes=None) -> dict[str, float]
    check_60_30_10(report) -> list[str]
    contrast_pairs(template_id) -> list[TextPair]
    resolve_font(role, user_path=None, ...) -> FontChoice
    cover_crop_box(...), export(image, path, format=None) -> Path
"""

from __future__ import annotations

import copy
import math
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

import brand
from brand import CREAM, CRIMSON, GRAPHITE, SAND

__all__ = [
    "TEMPLATES", "FieldSpec", "FontChoice", "TextPair", "DrawnBlock",
    "RenderReport", "FontNotFoundError",
    "list_templates", "template_fields", "example_fields", "contrast_pairs",
    "resolve_font", "bundled_fonts_dir", "user_fonts_dir", "system_font_dirs",
    "cover_crop_box", "render", "render_report", "usage_report",
    "check_60_30_10", "export", "color",
]

APP_ID = "CR2Converter"          # то же имя папки настроек, что у cr2_gui

Box = tuple[int, int, int, int]


# --------------------------------------------------------------------------- #
#  ЦВЕТ                                                                        #
# --------------------------------------------------------------------------- #

_COLOR_NAMES: dict[str, str] = {
    "cream": CREAM, "крем": CREAM, "кремовый": CREAM,
    "graphite": GRAPHITE, "графит": GRAPHITE,
    "crimson": CRIMSON, "бордовый": CRIMSON, "краплак": CRIMSON,
    "sand": SAND, "песок": SAND, "песочный": SAND,
}


def color(name: str) -> str:
    """'crimson' | 'графит' | '#9D091C' -> '#9D091C'.  Только цвета бренда."""
    key = str(name or "").strip().lower()
    if key in _COLOR_NAMES:
        return _COLOR_NAMES[key]
    if key.startswith("#"):
        hx = "#" + key[1:].upper()
        brand.hex_to_rgb(hx)
        if hx in brand.PALETTE.values():
            return hx
    raise ValueError(f"Цвет {name!r} не входит в палитру бренда: "
                     f"{', '.join(brand.PALETTE)}")


# --------------------------------------------------------------------------- #
#  ШРИФТЫ                                                                      #
# --------------------------------------------------------------------------- #

class FontNotFoundError(RuntimeError):
    """Ни одна подходящая гарнитура не найдена."""


@dataclass(frozen=True)
class FontChoice:
    """Какая гарнитура реально использована для роли."""
    role: str                   # 'display' | 'text'
    path: str
    family: str                 # что реально в файле, например 'Oswald Bold'
    brand_family: str           # что требует брендбук
    is_substitute: bool
    source: str                 # 'user' | 'user_dir' | 'bundled' | 'system'

    @property
    def label(self) -> str:
        """Подпись для интерфейса."""
        if not self.is_substitute:
            return f"{self.brand_family} (фирменная)"
        return f"{self.family} вместо {self.brand_family}"


# Кандидаты по ролям: (семейство, признаки в имени файла, брендовая ли).
# Имена сравниваются в нижнем регистре без пробелов, дефисов и подчёркиваний,
# поэтому 'Morfin Sans Regular.otf' и 'MorfinSans-Regular.ttf' равноценны
# (и на регистрозависимых томах macOS/Linux тоже).
_ROLE_BRAND: dict[str, str] = {"display": "Morfin Sans", "text": "Radiant Alt"}
_ROLE_CANDIDATES: dict[str, list[tuple[str, tuple[str, ...], bool]]] = {
    "display": [
        ("Morfin Sans", ("morfinsans",), True),
        ("Oswald Bold", ("oswald", "bold"), False),
    ],
    "text": [
        ("Radiant Alt", ("radiantalt",), True),
        ("Fira Sans Extra Condensed", ("firasansextracondensed", "regular"), False),
    ],
}
_BUNDLED_FILES: dict[str, str] = {
    "Oswald Bold": "Oswald-Bold.ttf",
    "Fira Sans Extra Condensed": "FiraSansExtraCondensed-Regular.ttf",
}
_FONT_EXT = (".ttf", ".otf", ".ttc")


def bundled_fonts_dir() -> Path:
    """Папка fonts/ рядом с модулем или внутри сборки PyInstaller."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "fonts"
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent / "fonts"


def user_fonts_dir() -> Path:
    """Папка пользовательских шрифтов в папке настроек программы.

    Windows: %APPDATA%/CR2Converter/fonts, macOS: ~/Library/Application
    Support/CR2Converter/fonts, Linux: $XDG_CONFIG_HOME/cr2converter/fonts.
    Каталог не создаётся.
    """
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or ""
        root = (Path(base) if base else home / "AppData" / "Roaming") / APP_ID
    elif sys.platform == "darwin":
        root = home / "Library" / "Application Support" / APP_ID
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or ""
        root = (Path(base) if base else home / ".config") / APP_ID.lower()
    return root / "fonts"


def system_font_dirs() -> list[Path]:
    """Системные папки шрифтов текущей ОС (существующие)."""
    home = Path.home()
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR") or r"C:\Windows"
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        dirs = [Path(windir) / "Fonts", Path(local) / "Microsoft" / "Windows" / "Fonts"]
    elif sys.platform == "darwin":
        dirs = [home / "Library" / "Fonts", Path("/Library/Fonts"),
                Path("/System/Library/Fonts")]
    else:
        dirs = [home / ".local" / "share" / "fonts", home / ".fonts",
                Path("/usr/local/share/fonts"), Path("/usr/share/fonts")]
    return [d for d in dirs if d.is_dir()]


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


_DIR_LISTING: dict[str, list[Path]] = {}


def _font_files(folder: Path, recursive: bool) -> list[Path]:
    """Файлы шрифтов в папке.  Рекурсивный обход (системные папки) кэшируется
    на время работы программы; папка пользователя читается заново каждый раз,
    чтобы только что положенный туда шрифт подхватился без перезапуска."""
    key = str(folder)
    if recursive and key in _DIR_LISTING:
        return _DIR_LISTING[key]
    files: list[Path] = []
    try:
        it = folder.rglob("*") if recursive else folder.iterdir()
        for p in it:
            if p.suffix.lower() in _FONT_EXT:
                files.append(p)
            if len(files) > 20000:
                break
    except OSError:
        pass
    files.sort()
    if recursive:
        _DIR_LISTING[key] = files
    return files


def _match(path: Path, tokens: tuple[str, ...]) -> bool:
    name = _norm(path.stem)
    return all(t in name for t in tokens)


def _loadable(path: Path) -> bool:
    try:
        ImageFont.truetype(str(path), 24)
        return True
    except Exception:
        return False


def _has_cyrillic(path: Path) -> bool:
    """Есть ли в шрифте кириллица: глиф «Ж» не совпадает с «нет глифа»."""
    try:
        f = ImageFont.truetype(str(path), 48)
        zh = f.getmask("Жж")
        missing = f.getmask("\U000F0000\U000F0001")
        return zh.size != missing.size or bytes(zh) != bytes(missing)
    except Exception:
        return False


def _family_name(path: Path) -> str:
    try:
        name, style = ImageFont.truetype(str(path), 24).getname()
        style = (style or "").strip()
        return f"{name} {style}".strip() if style and style.lower() != "regular" \
            else (name or path.stem)
    except Exception:
        return path.stem


def resolve_font(role: str, user_path: str | os.PathLike | None = None, *,
                 user_dir: Path | None = None,
                 bundled_dir: Path | None = None,
                 system_dirs: Sequence[Path] | None = None) -> FontChoice:
    """Найти файл гарнитуры для роли 'display' (заголовки) или 'text'.

    Порядок поиска:
      (a) файл, который пользователь выбрал сам (`user_path`);
      (b) папка шрифтов пользователя в папке настроек программы;
      (c) папка fonts/ программы (в сборке - внутри _MEIPASS);
      (d) системные шрифты.
    Сначала во всех местах ищется брендовая гарнитура, затем замена: Morfin
    Sans, установленная в систему, лучше Oswald из папки программы.
    Файл без кириллицы для русского текста не годится и пропускается.
    """
    if role not in _ROLE_CANDIDATES:
        raise ValueError(f"Неизвестная роль шрифта: {role!r}")
    brand_family = _ROLE_BRAND[role]

    if user_path:
        p = Path(user_path)
        if p.is_file() and _loadable(p) and _has_cyrillic(p):
            fam = _family_name(p)
            is_brand = any(_match(p, toks) or _norm(fam).startswith(toks[0])
                           for _, toks, b in _ROLE_CANDIDATES[role] if b)
            return FontChoice(role, str(p), fam, brand_family, not is_brand, "user")

    places: list[tuple[str, Path, bool]] = []
    places.append(("user_dir", user_dir if user_dir is not None else user_fonts_dir(), False))
    places.append(("bundled", bundled_dir if bundled_dir is not None else bundled_fonts_dir(), False))
    for d in (system_dirs if system_dirs is not None else system_font_dirs()):
        places.append(("system", Path(d), True))

    for family, tokens, is_brand in _ROLE_CANDIDATES[role]:
        for source, folder, recursive in places:
            if not folder.is_dir():
                continue
            if source == "bundled" and family in _BUNDLED_FILES:
                exact = folder / _BUNDLED_FILES[family]
                hits = [exact] if exact.is_file() else []
            else:
                hits = [p for p in _font_files(folder, recursive) if _match(p, tokens)]
            for p in hits:
                if _loadable(p) and _has_cyrillic(p):
                    return FontChoice(role, str(p), _family_name(p), brand_family,
                                      not is_brand, source)
    raise FontNotFoundError(
        f"Не найден шрифт для роли «{'заголовки' if role == 'display' else 'текст'}»: "
        f"нет ни {brand_family}, ни замены.  Переустановите программу "
        f"(папка fonts) или укажите файл шрифта.")


# --------------------------------------------------------------------------- #
#  ТИПОГРАФИКА                                                                 #
# --------------------------------------------------------------------------- #

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(path: str, size_px: float) -> ImageFont.FreeTypeFont:
    key = (path, max(1, int(round(size_px))))
    f = _FONT_CACHE.get(key)
    if f is None:
        if len(_FONT_CACHE) > 512:
            _FONT_CACHE.clear()
        f = ImageFont.truetype(path, key[1])
        _FONT_CACHE[key] = f
    return f


def _advance(s: str, font: ImageFont.FreeTypeFont, tracking_px: float) -> float:
    """Ширина набора с трекингом (после последнего знака трекинг не ставится)."""
    if not s:
        return 0.0
    if tracking_px == 0.0:
        return float(font.getlength(s))
    return float(sum(font.getlength(ch) for ch in s)) + tracking_px * (len(s) - 1)


def _ink_h(s: str, font: ImageFont.FreeTypeFont, tracking_px: float
           ) -> tuple[float, float]:
    """Горизонтальные границы чернил строки относительно пера: (left, right)."""
    if not s:
        return 0.0, 0.0
    first = font.getbbox(s[0], anchor="ls")
    if tracking_px == 0.0:
        bb = font.getbbox(s, anchor="ls")
        return float(min(bb[0], 0)), float(max(bb[2], font.getlength(s)))
    pen = _advance(s[:-1], font, tracking_px) + (tracking_px if len(s) > 1 else 0.0)
    last = font.getbbox(s[-1], anchor="ls")
    return float(min(first[0], 0)), float(max(pen + last[2], pen + font.getlength(s[-1])))


def _ink_v(s: str, font: ImageFont.FreeTypeFont) -> tuple[float, float]:
    """Вертикальные границы чернил относительно базовой линии: (top<0, bottom)."""
    if not s.strip():
        return 0.0, 0.0
    bb = font.getbbox(s, anchor="ls")
    return float(bb[1]), float(bb[3])


@dataclass
class TextLayout:
    """Результат подбора кегля: строки и их метрики."""
    lines: list[str]
    size: float
    font: ImageFont.FreeTypeFont
    tracking_px: float
    advance: float              # шаг строк, px
    ascent: float               # от верха чернил 1-й строки до её базовой линии
    height: float               # высота чернил всего блока
    width: float                # ширина чернил самой широкой строки
    fits: bool


def _hyphen_parts(word: str) -> list[str]:
    """«МЕЖДУНАРОДНО-ПРАВОВОЙ» -> ['МЕЖДУНАРОДНО-', 'ПРАВОВОЙ'] (дефис остаётся в строке)."""
    parts: list[str] = []
    cur = ""
    for i, ch in enumerate(word):
        cur += ch
        if ch == "-" and 0 < i < len(word) - 1:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    return parts


def _wrap(text: str, font: ImageFont.FreeTypeFont, tracking_px: float,
          width: float) -> list[str] | None:
    """Жадный перенос по словам; явные переводы строк сохраняются.

    Слово шире блока переносится после дефиса («МЕЖДУНАРОДНО-» / «ПРАВОВОЙ»),
    прежде чем fit_text станет уменьшать кегль.  None - если и так не влезает.
    """
    out: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            continue
        # (кусок, приклеен ли к предыдущему без пробела)
        tokens: list[tuple[str, bool]] = []
        for w in words:
            l, r = _ink_h(w, font, tracking_px)
            if r - l <= width:
                tokens.append((w, False))
                continue
            parts = _hyphen_parts(w)
            if len(parts) < 2:
                return None
            for k, part in enumerate(parts):
                pl, pr = _ink_h(part, font, tracking_px)
                if pr - pl > width:
                    return None
                tokens.append((part, k > 0))
        cur = ""
        for tok, glued in tokens:
            trial = (cur + tok if glued else f"{cur} {tok}") if cur else tok
            tl, tr = _ink_h(trial, font, tracking_px)
            if cur and tr - tl > width:
                out.append(cur)
                cur = tok
            else:
                cur = trial
        out.append(cur)
    return out


def _layout_at(text: str, path: str, size: float, box_w: float, box_h: float, *,
               tracking: float, line_height: float, max_lines: int
               ) -> TextLayout | None:
    f = _font(path, size)
    tr = tracking * size
    lines = _wrap(text, f, tr, box_w)
    if not lines or len(lines) > max_lines:
        return None
    top, _ = _ink_v(lines[0], f)
    ascent = max(-top, 0.0)
    advance = size * line_height
    _, bottom = _ink_v(lines[-1], f)
    height = ascent + advance * (len(lines) - 1) + max(bottom, 0.0)
    width = max(r - l for l, r in (_ink_h(ln, f, tr) for ln in lines))
    fits = height <= box_h + 0.01 and width <= box_w + 0.01
    return TextLayout(lines, size, f, tr, advance, ascent, height, width, fits)


def fit_text(text: str, font_path: str, box_w: float, box_h: float, *,
             max_size: float, min_size: float = 6.0, tracking: float = 0.0,
             line_height: float = 1.0, max_lines: int = 1) -> TextLayout:
    """Самый крупный кегль <= max_size, при котором ЧЕРНИЛА текста (вместе с
    диакритикой Й и выносными Д, Щ, Ц) помещаются в прямоугольник.

    Двоичный поиск по кеглю; если не влезает даже min_size - возвращается
    min_size с fits=False (рисование всё равно обрезается рамкой блока).
    """
    s = text.strip()
    lo_size = max(1.0, float(min_size))
    hi_size = max(lo_size, float(max_size))
    best = _layout_at(s, font_path, hi_size, box_w, box_h, tracking=tracking,
                      line_height=line_height, max_lines=max_lines)
    if best is not None and best.fits:
        return best
    low = _layout_at(s, font_path, lo_size, box_w, box_h, tracking=tracking,
                     line_height=line_height, max_lines=max_lines)
    if low is None or not low.fits:
        f = _font(font_path, lo_size)
        fallback = low or TextLayout([s], lo_size, f, tracking * lo_size,
                                     lo_size * line_height, lo_size, box_h + 1,
                                     box_w + 1, False)
        fallback.fits = False
        return fallback
    best = low
    a, b = lo_size, hi_size
    while b - a > 0.5:
        mid = 0.5 * (a + b)
        r = _layout_at(s, font_path, mid, box_w, box_h, tracking=tracking,
                       line_height=line_height, max_lines=max_lines)
        if r is not None and r.fits:
            a, best = mid, r
        else:
            b = mid
    return best


def _draw_layout(img: Image.Image, lay: TextLayout, box: Box, fill: str,
                 align: str, valign: str) -> None:
    """Рисует текст ВНУТРИ прямоугольника: всё, что вылезло бы, обрезается."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    region = img.crop(box)
    d = ImageDraw.Draw(region)
    if valign == "center":
        top = (bh - lay.height) / 2.0
    elif valign == "bottom":
        top = bh - lay.height
    else:
        top = 0.0
    baseline = top + lay.ascent
    rgb = brand.hex_to_rgb(fill)
    for ln in lay.lines:
        l, r = _ink_h(ln, lay.font, lay.tracking_px)
        if align == "center":
            x = (bw - (r - l)) / 2.0 - l
        elif align == "right":
            x = bw - r
        else:
            x = -l
        if lay.tracking_px == 0.0:
            d.text((x, baseline), ln, font=lay.font, fill=rgb, anchor="ls")
        else:
            for ch in ln:
                d.text((x, baseline), ch, font=lay.font, fill=rgb, anchor="ls")
                x += float(lay.font.getlength(ch)) + lay.tracking_px
        baseline += lay.advance
    img.paste(region, (x0, y0))


# --------------------------------------------------------------------------- #
#  ФОТО: открытие и кадрирование                                               #
# --------------------------------------------------------------------------- #

def cover_crop_box(src_w: int, src_h: int, dst_w: int, dst_h: int, *,
                   focus: tuple[float, float] | None = None,
                   face_box: Sequence[float] | None = None,
                   face_fill: float = 0.0, max_zoom: float = 1.0,
                   head_y: float = 0.40) -> Box:
    """Кроп «на заполнение» (left, top, right, bottom) в пикселях исходника.

    focus     точка внимания в долях кадра (0..1); по умолчанию (0.5, 0.42).
    face_box  (left, top, right, bottom) лица в пикселях исходника.  Лицо
              целиком остаётся в кадре, если это геометрически возможно, а его
              центр встаёт на `head_y` высоты кропа - голову не режет.
    face_fill желаемая ширина лица в долях ширины блока: кроп приближается
              (не больше `max_zoom`), чтобы на лицо пришлось достаточно точек
              растра.  0 - не приближать.
    """
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        raise ValueError("cover_crop_box: размеры должны быть положительными")
    dst_ar = dst_w / dst_h
    if src_w / src_h > dst_ar:
        cw, ch = src_h * dst_ar, float(src_h)
    else:
        cw, ch = float(src_w), src_w / dst_ar

    fb = None
    if face_box is not None:
        l, t, r, b = (float(v) for v in face_box)
        l, r = sorted((max(0.0, min(src_w, l)), max(0.0, min(src_w, r))))
        t, b = sorted((max(0.0, min(src_h, t)), max(0.0, min(src_h, b))))
        if r - l >= 2 and b - t >= 2:
            fb = (l, t, r, b)

    if fb is not None:
        fw = fb[2] - fb[0]
        if face_fill > 0 and max_zoom > 1.0:
            want = fw / face_fill            # ширина кропа, при которой лицо = face_fill
            zoom = min(max(cw / max(want, 1.0), 1.0), float(max_zoom))
            # приближение не должно делать лицо шире 60 % кропа
            zoom = min(zoom, max(1.0, 0.6 * cw / max(fw, 1.0)))
            cw, ch = cw / zoom, ch / zoom
        cx = 0.5 * (fb[0] + fb[2])
        cy = 0.5 * (fb[1] + fb[3])
        x0 = cx - cw / 2.0
        y0 = cy - head_y * ch
        # лицо целиком внутри, если помещается
        if fb[2] - fb[0] <= cw:
            x0 = min(max(x0, fb[2] - cw), fb[0])
        if fb[3] - fb[1] <= ch:
            y0 = min(max(y0, fb[3] - ch), fb[1])
    else:
        fx, fy = focus if focus is not None else (0.5, 0.42)
        x0 = fx * src_w - cw / 2.0
        y0 = fy * src_h - ch / 2.0

    x0 = min(max(x0, 0.0), src_w - cw)
    y0 = min(max(y0, 0.0), src_h - ch)
    return (int(round(x0)), int(round(y0)),
            int(round(min(x0 + cw, src_w))), int(round(min(y0 + ch, src_h))))


def _open_photo(photo: Any, hint_px: int) -> tuple[Image.Image, float]:
    """Открыть фото ТОЛЬКО на чтение.  -> (RGB после EXIF-разворота, масштаб).

    Масштаб - во сколько раз загруженное меньше полноразмерного (для
    пересчёта face_box, заданного в пикселях полного кадра).  JPEG
    декодируется в уменьшенном масштабе DCT (draft), если блок меньше кадра.
    """
    if isinstance(photo, np.ndarray):
        arr = photo
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        return Image.fromarray(np.ascontiguousarray(arr[..., :3]).astype(np.uint8), "RGB"), 1.0
    if isinstance(photo, Image.Image):
        return ImageOps.exif_transpose(photo).convert("RGB"), 1.0
    with Image.open(Path(photo)) as im:
        raw_w, raw_h = im.size
        try:
            im.draft("RGB", (hint_px, hint_px))
        except Exception:
            pass
        im.load()
        out = ImageOps.exif_transpose(im).convert("RGB")
    swapped = raw_w != raw_h and (out.width > out.height) != (raw_w > raw_h)
    full_w = raw_h if swapped else raw_w
    return out, out.width / float(full_w)


def _placeholder_photo(w: int, h: int) -> Image.Image:
    """Заглушка без фото: спокойный вертикальный градиент."""
    ramp = np.linspace(200, 70, max(h, 1), dtype=np.float32)[:, None]
    arr = np.repeat(np.repeat(ramp, max(w, 1), axis=1)[..., None], 3, axis=2)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


# --------------------------------------------------------------------------- #
#  ШАБЛОНЫ                                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FieldSpec:
    """Редактируемое поле шаблона."""
    key: str
    label: str
    kind: str = "text"          # 'text' | 'photo'
    required: bool = False
    multiline: bool = False
    example: str = ""


_F: dict[str, FieldSpec] = {
    "headline": FieldSpec("headline", "Заголовок", required=True,
                          example="Искусство судебной речи"),
    "subline": FieldSpec("subline", "Подзаголовок",
                         example="Открытая лекция для студентов и магистрантов"),
    "date": FieldSpec("date", "Дата", required=True, example="12.12"),
    "time": FieldSpec("time", "Время", example="18:00"),
    "venue": FieldSpec("venue", "Место", example="ГК, ауд. 374"),
    "note": FieldSpec("note", "Примечание", example="регистрация обязательна"),
    "photo": FieldSpec("photo", "Фотография", kind="photo", required=True),
    "quote": FieldSpec("quote", "Цитата", required=True, multiline=True,
                       example="Суд убеждают не громкостью голоса, "
                               "а точностью каждого слова"),
    "author": FieldSpec("author", "Автор цитаты", required=True,
                        example="Анна Петрова"),
}


def _fields(*keys: str, **overrides: dict) -> list[FieldSpec]:
    out = []
    for k in keys:
        spec = _F[k]
        if k in overrides:
            spec = FieldSpec(**{**spec.__dict__, **overrides[k]})
        out.append(spec)
    return out


def _swatches(x: float, y: float, w: float, h: float, n: int = 5,
              colors: Sequence[str] = ("crimson", "sand", "graphite")) -> dict:
    return {"type": "swatches", "rect": [x, y, w, h], "n": n, "gap": 0.35,
            "colors": list(colors)}


def _t(field_key: str, rect: list[float], *, font: str, color_: str, on: str,
       max_size: float, min_size: float = 0.012, max_lines: int = 1,
       upper: bool = False, tracking: float = 0.0, line_height: float = 1.0,
       align: str = "left", valign: str = "top") -> dict:
    return {"type": "text", "field": field_key, "rect": rect, "font": font,
            "color": color_, "on": on, "max_size": max_size, "min_size": min_size,
            "max_lines": max_lines, "upper": upper, "tracking": tracking,
            "line_height": line_height, "align": align, "valign": valign}


def _img(rect: list[float], *, face_fill: float = 0.28, max_zoom: float = 2.5,
         dot_display_px: float = 3.5, preserve_detail: float = 0.85) -> dict:
    return {"type": "image", "field": "photo", "rect": rect,
            "face_fill": face_fill, "max_zoom": max_zoom,
            "halftone": {"ink": "crimson", "paper": "cream", "angle": 45.0,
                         "dot_display_px": dot_display_px,
                         "preserve_detail": preserve_detail, "unsharp": 0.5}}


# Все координаты - доли холста [x, y, w, h].  Порядок блоков = порядок рисования.
# Композиция: кремовое поле, ряд образцов-прямоугольников, бордовый заголовок
# прописными, халфтон-фото, графитовая плашка с датой/временем/местом.
# Графитовая плашка несёт вторичные 30 % - без неё графита выходит 0.6 %.

TEMPLATES: dict[str, dict[str, Any]] = {
    "afisha_a3": {
        "title": "Афиша A3",
        "size": (3508, 4961), "dpi": 300,
        "display_px": 1240,          # как афишу видят: ~A3 с 1.5 м / превью
        "bg": "cream",
        "fields": _fields("headline", "subline", "date", "time", "venue", "note", "photo"),
        "blocks": [
            _swatches(0.065, 0.034, 0.30, 0.012, n=5),
            _t("headline", [0.065, 0.066, 0.87, 0.175], font="display",
               color_="crimson", on="cream", max_size=0.13, max_lines=3,
               upper=True, tracking=0.01, line_height=0.95),
            _t("subline", [0.065, 0.250, 0.87, 0.046], font="text",
               color_="graphite", on="cream", max_size=0.040, max_lines=2,
               line_height=1.15),
            _img([0.065, 0.305, 0.87, 0.385]),
            {"type": "rect", "rect": [0.0, 0.725, 1.0, 0.275], "fill": "graphite"},
            {"type": "rect", "rect": [0.065, 0.750, 0.012, 0.130], "fill": "crimson"},
            _t("date", [0.100, 0.745, 0.40, 0.130], font="display", color_="cream",
               on="graphite", max_size=0.20, valign="center"),
            _t("time", [0.540, 0.748, 0.395, 0.060], font="display", color_="sand",
               on="graphite", max_size=0.085, align="right"),
            _t("venue", [0.540, 0.822, 0.395, 0.050], font="text", color_="cream",
               on="graphite", max_size=0.045, max_lines=2, line_height=1.05,
               align="right", valign="center"),
            {"type": "rect", "rect": [0.065, 0.895, 0.87, 0.050], "fill": "crimson"},
            _t("note", [0.090, 0.903, 0.82, 0.034], font="text", color_="cream",
               on="crimson", max_size=0.030, max_lines=2, line_height=1.05, upper=True,
               tracking=0.06, valign="center"),
            {"type": "bands", "rect": [0.065, 0.960, 0.87, 0.010],
             "weights": [5, 1, 2, 1, 9, 1, 3],
             "colors": ["crimson", "graphite", "sand", "graphite", "cream",
                        "graphite", "crimson"]},
        ],
    },
    "post": {
        "title": "Пост 1080×1350",
        "size": (1080, 1350), "dpi": 72,
        "display_px": 360,           # пост в ленте на телефоне / превью
        "bg": "cream",
        "fields": _fields("headline", "subline", "date", "time", "venue", "note", "photo"),
        "blocks": [
            _swatches(0.075, 0.035, 0.30, 0.013, n=5),
            _t("headline", [0.075, 0.068, 0.85, 0.170], font="display",
               color_="crimson", on="cream", max_size=0.13, max_lines=3,
               upper=True, tracking=0.01, line_height=0.95),
            _t("subline", [0.075, 0.250, 0.85, 0.042], font="text",
               color_="graphite", on="cream", max_size=0.040, max_lines=2,
               line_height=1.1),
            _img([0.075, 0.305, 0.85, 0.405]),
            {"type": "rect", "rect": [0.0, 0.740, 1.0, 0.260], "fill": "graphite"},
            {"type": "rect", "rect": [0.075, 0.772, 0.014, 0.118], "fill": "crimson"},
            _t("date", [0.115, 0.772, 0.40, 0.118], font="display", color_="cream",
               on="graphite", max_size=0.22, valign="center"),
            _t("time", [0.545, 0.775, 0.38, 0.055], font="display", color_="sand",
               on="graphite", max_size=0.09, align="right"),
            _t("venue", [0.545, 0.843, 0.38, 0.045], font="text", color_="cream",
               on="graphite", max_size=0.050, max_lines=2, line_height=1.05,
               align="right", valign="center"),
            {"type": "rect", "rect": [0.075, 0.903, 0.85, 0.052], "fill": "crimson"},
            _t("note", [0.100, 0.911, 0.80, 0.036], font="text", color_="cream",
               on="crimson", max_size=0.034, max_lines=2, line_height=1.05, upper=True,
               tracking=0.06, valign="center"),
            {"type": "bands", "rect": [0.075, 0.966, 0.85, 0.008],
             "weights": [5, 1, 2, 1, 9, 1, 3],
             "colors": ["crimson", "graphite", "sand", "graphite", "cream",
                        "graphite", "crimson"]},
        ],
    },
    "stories": {
        "title": "Сторис 1080×1920",
        "size": (1080, 1920), "dpi": 72,
        "display_px": 390,           # полноэкранная сторис на телефоне
        "bg": "cream",
        # Безопасная зона: ~250 px сверху и снизу перекрывает интерфейс плеера.
        "fields": _fields("headline", "subline", "date", "time", "venue", "note", "photo"),
        "blocks": [
            _swatches(0.075, 0.105, 0.30, 0.010, n=5),
            _t("headline", [0.075, 0.140, 0.85, 0.150], font="display",
               color_="crimson", on="cream", max_size=0.15, max_lines=3,
               upper=True, tracking=0.01, line_height=0.95),
            _t("subline", [0.075, 0.300, 0.85, 0.035], font="text",
               color_="graphite", on="cream", max_size=0.045, max_lines=2,
               line_height=1.1),
            _img([0.075, 0.350, 0.85, 0.370]),
            {"type": "rect", "rect": [0.0, 0.745, 1.0, 0.255], "fill": "graphite"},
            {"type": "rect", "rect": [0.075, 0.765, 0.014, 0.080], "fill": "crimson"},
            _t("date", [0.115, 0.765, 0.40, 0.080], font="display", color_="cream",
               on="graphite", max_size=0.22, valign="center"),
            _t("time", [0.545, 0.767, 0.38, 0.038], font="display", color_="sand",
               on="graphite", max_size=0.10, align="right"),
            _t("venue", [0.545, 0.812, 0.38, 0.032], font="text", color_="cream",
               on="graphite", max_size=0.055, max_lines=2, line_height=1.05,
               align="right", valign="center"),
            {"type": "rect", "rect": [0.075, 0.852, 0.85, 0.034], "fill": "crimson"},
            _t("note", [0.100, 0.857, 0.80, 0.024], font="text", color_="cream",
               on="crimson", max_size=0.036, max_lines=2, line_height=1.05, upper=True,
               tracking=0.06, valign="center"),
        ],
    },
    "album_cover": {
        "title": "Обложка альбома 1080×1080",
        "size": (1080, 1080), "dpi": 72,
        "display_px": 400,           # обложка альбома в списке альбомов
        "bg": "cream",
        "fields": _fields("headline", "date", "venue", "photo"),
        "blocks": [
            _img([0.065, 0.065, 0.87, 0.500], face_fill=0.18),
            _t("headline", [0.065, 0.600, 0.87, 0.170], font="display",
               color_="crimson", on="cream", max_size=0.15, max_lines=2,
               upper=True, tracking=0.01, line_height=0.95, valign="center"),
            {"type": "rect", "rect": [0.0, 0.800, 1.0, 0.200], "fill": "graphite"},
            {"type": "rect", "rect": [0.0, 0.800, 1.0, 0.022], "fill": "crimson"},
            {"type": "rect", "rect": [0.065, 0.840, 0.016, 0.120], "fill": "crimson"},
            _t("date", [0.110, 0.840, 0.36, 0.120], font="display", color_="cream",
               on="graphite", max_size=0.20, valign="center"),
            _t("venue", [0.500, 0.840, 0.435, 0.120], font="text", color_="sand",
               on="graphite", max_size=0.060, max_lines=2, align="right",
               valign="center", line_height=1.1),
        ],
    },
    "summary": {
        "title": "Итоги мероприятия",
        "size": (1080, 1350), "dpi": 72,
        "display_px": 360,
        "bg": "cream",
        "fields": _fields("headline", "subline", "date", "venue", "note", "photo",
                          headline={"example": "Итоги недели юриста"},
                          subline={"example": "Спасибо всем, кто пришёл и спорил"},
                          note={"label": "Итог в цифрах",
                                "example": "3 дня · 12 спикеров · 400 участников"}),
        "blocks": [
            {"type": "rect", "rect": [0.0, 0.0, 1.0, 0.135], "fill": "graphite"},
            {"type": "text", "field": None, "literal": "ИТОГИ", "rect": [0.075, 0.030, 0.40, 0.075],
             "font": "display", "color": "sand", "on": "graphite", "max_size": 0.09,
             "min_size": 0.012, "max_lines": 1, "upper": True, "tracking": 0.08,
             "line_height": 1.0, "align": "left", "valign": "center"},
            _t("date", [0.525, 0.030, 0.40, 0.075], font="display", color_="cream",
               on="graphite", max_size=0.09, align="right", valign="center"),
            _t("headline", [0.075, 0.185, 0.85, 0.150], font="display",
               color_="crimson", on="cream", max_size=0.13, max_lines=2,
               upper=True, tracking=0.01, line_height=0.95),
            _img([0.075, 0.360, 0.85, 0.380]),
            {"type": "rect", "rect": [0.075, 0.755, 0.85, 0.055], "fill": "crimson"},
            _t("note", [0.100, 0.763, 0.80, 0.040], font="display", color_="cream",
               on="crimson", max_size=0.06, upper=True, tracking=0.02,
               valign="center"),
            _t("subline", [0.075, 0.815, 0.85, 0.035], font="text",
               color_="graphite", on="cream", max_size=0.040, max_lines=1),
            {"type": "rect", "rect": [0.0, 0.875, 1.0, 0.125], "fill": "graphite"},
            _t("venue", [0.075, 0.905, 0.60, 0.060], font="text", color_="cream",
               on="graphite", max_size=0.045, max_lines=2, line_height=1.05,
               valign="center"),
            _swatches(0.700, 0.925, 0.225, 0.022, n=3),
        ],
    },
    "quote": {
        "title": "Цитата спикера",
        "size": (1080, 1350), "dpi": 72,
        "display_px": 360,
        "bg": "cream",
        "fields": _fields("quote", "author", "subline", "date", "photo",
                          subline={"label": "Должность / событие",
                                   "example": "адвокат, выпускница ЮИ РУДН"}),
        "blocks": [
            _swatches(0.075, 0.040, 0.30, 0.013, n=5),
            _img([0.075, 0.080, 0.42, 0.330], face_fill=0.40, max_zoom=3.0),
            {"type": "rect", "rect": [0.535, 0.080, 0.390, 0.200], "fill": "crimson"},
            _t("author", [0.565, 0.105, 0.330, 0.150], font="display",
               color_="cream", on="crimson", max_size=0.10, max_lines=3,
               upper=True, tracking=0.01, line_height=0.95),
            _t("subline", [0.535, 0.300, 0.390, 0.110], font="text",
               color_="graphite", on="cream", max_size=0.042, max_lines=3,
               line_height=1.12, valign="bottom"),
            {"type": "rect", "rect": [0.075, 0.470, 0.085, 0.020], "fill": "crimson"},
            _t("quote", [0.075, 0.520, 0.85, 0.250], font="text", color_="graphite",
               on="cream", max_size=0.075, max_lines=6, line_height=1.12),
            {"type": "rect", "rect": [0.0, 0.805, 1.0, 0.195], "fill": "graphite"},
            _t("date", [0.075, 0.850, 0.40, 0.090], font="display", color_="sand",
               on="graphite", max_size=0.09, valign="center"),
            {"type": "bands", "rect": [0.525, 0.890, 0.40, 0.012],
             "weights": [5, 1, 2, 1, 6],
             "colors": ["crimson", "graphite", "sand", "graphite", "cream"]},
        ],
    },
}


def _template(template_id: str) -> dict[str, Any]:
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise ValueError(f"Нет шаблона {template_id!r}.  Доступны: "
                         f"{', '.join(TEMPLATES)}") from None


def list_templates() -> list[tuple[str, str]]:
    """[(id, название по-русски), ...] в порядке показа."""
    return [(k, v["title"]) for k, v in TEMPLATES.items()]


def template_fields(template_id: str) -> list[FieldSpec]:
    """Редактируемые поля шаблона (в порядке формы)."""
    return list(_template(template_id)["fields"])


def example_fields(template_id: str) -> dict[str, str]:
    """Пример заполнения текстовых полей (по нему проверяются шаблоны)."""
    return {f.key: f.example for f in _template(template_id)["fields"]
            if f.kind == "text" and f.example}


# --------------------------------------------------------------------------- #
#  ОТЧЁТ ОТРИСОВКИ                                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TextPair:
    """Пара «текст на фоне» и её контраст по WCAG 2.2."""
    field: str
    fg: str
    bg: str
    ratio: float
    required: float             # 4.5 для основного текста, 3.0 для заголовков
    body: bool

    @property
    def ok(self) -> bool:
        return self.ratio >= self.required and (self.fg, self.bg) not in brand.FORBIDDEN_PAIRS


@dataclass(frozen=True)
class DrawnBlock:
    """Один нарисованный прямоугольник: плашка, фото или рамка текста."""
    kind: str                   # 'rect' | 'photo' | 'text'
    box: Box                    # (x0, y0, x1, y1), x1/y1 не включительно
    fill: str | None            # цвет для 'rect', цвет текста для 'text'
    field: str | None = None


@dataclass
class RenderReport:
    """Что реально получилось: гарнитуры, блоки, контраст, предупреждения."""
    template_id: str
    size: tuple[int, int]
    dpi: int
    fonts: dict[str, FontChoice]
    blocks: list[DrawnBlock] = field(default_factory=list)
    text_pairs: list[TextPair] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pitch_px: float = 0.0

    @property
    def photo_boxes(self) -> list[Box]:
        return [b.box for b in self.blocks if b.kind == "photo"]

    @property
    def substituted(self) -> list[str]:
        """Роли, набранные заменой: ['display', 'text']."""
        return [r for r, f in self.fonts.items() if f.is_substitute]


def render_report(image: Image.Image) -> RenderReport | None:
    """Отчёт, приложенный render() к изображению (или None)."""
    rep = image.info.get("poster_report")
    return rep if isinstance(rep, RenderReport) else None


def contrast_pairs(template_id: str) -> list[TextPair]:
    """Все пары «цвет текста / цвет фона», которые использует шаблон."""
    out = []
    for b in _template(template_id)["blocks"]:
        if b["type"] != "text":
            continue
        fg, bg = color(b["color"]), color(b["on"])
        body = b["font"] == "text"
        out.append(TextPair(b.get("field") or "literal", fg, bg,
                            round(brand.contrast_ratio(fg, bg), 2),
                            brand.WCAG_BODY if body else brand.WCAG_LARGE, body))
    return out


# --------------------------------------------------------------------------- #
#  ОТРИСОВКА                                                                   #
# --------------------------------------------------------------------------- #

def _px(rect: Sequence[float], W: int, H: int) -> Box:
    """[x, y, w, h] в долях -> (x0, y0, x1, y1).  Округляются КРАЯ, не ширины,
    иначе между соседними прямоугольниками вылезают щели в 1 px."""
    x, y, w, h = (float(v) for v in rect)
    x0, y0 = int(round(x * W)), int(round(y * H))
    x1, y1 = int(round((x + w) * W)), int(round((y + h) * H))
    x0, x1 = max(0, min(W, x0)), max(0, min(W, x1))
    y0, y1 = max(0, min(H, y0)), max(0, min(H, y1))
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def _fill_rect(img: Image.Image, box: Box, fill: str, report: RenderReport) -> None:
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    img.paste(brand.hex_to_rgb(fill), box)       # paste - без сглаживания и без +1
    report.blocks.append(DrawnBlock("rect", box, fill))


def _draw_swatches(img: Image.Image, b: dict, box: Box, report: RenderReport) -> None:
    x0, y0, x1, y1 = box
    cols = [color(c) for c in b.get("colors", ["crimson", "sand", "graphite"])]
    n = int(b.get("n", len(cols)))
    gap = float(b.get("gap", 0.35))
    unit = (x1 - x0) / (n + (n - 1) * gap)
    for i in range(n):
        a = x0 + i * unit * (1.0 + gap)
        _fill_rect(img, (int(round(a)), y0, int(round(a + unit)), y1),
                   cols[i % len(cols)], report)


def _draw_bands(img: Image.Image, b: dict, box: Box, report: RenderReport) -> None:
    x0, y0, x1, y1 = box
    weights = [float(v) for v in b.get("weights", [1, 1, 1])]
    cols = [color(c) for c in b.get("colors", ["crimson", "sand", "graphite"])]
    total = sum(weights) or 1.0
    acc = 0.0
    for i, w in enumerate(weights):
        a = x0 + acc / total * (x1 - x0)
        acc += w
        c = x0 + acc / total * (x1 - x0)
        _fill_rect(img, (int(round(a)), y0, int(round(c)), y1),
                   cols[i % len(cols)], report)


def _modal_color(img: Image.Image, box: Box) -> tuple[int, int, int]:
    region = np.asarray(img.crop(box))
    step = max(1, int(math.sqrt(region.shape[0] * region.shape[1] / 40000.0)))
    px = region[::step, ::step].reshape(-1, 3)
    keys = (px[:, 0].astype(np.int64) << 16) | (px[:, 1].astype(np.int64) << 8) | px[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    k = int(vals[int(np.argmax(counts))])
    return (k >> 16) & 255, (k >> 8) & 255, k & 255


def _draw_text(img: Image.Image, b: dict, box: Box, text: str,
               fonts: dict[str, FontChoice], report: RenderReport) -> None:
    W = img.width
    role = b["font"]
    fg, bg = color(b["color"]), color(b["on"])
    body = role == "text"
    pair = TextPair(b.get("field") or "literal", fg, bg,
                    round(brand.contrast_ratio(fg, bg), 2),
                    brand.WCAG_BODY if body else brand.WCAG_LARGE, body)
    if not pair.ok:
        raise ValueError(f"Шаблон {report.template_id}: текст {fg} на {bg} - "
                         f"контраст {pair.ratio:.2f}:1 ниже {pair.required}:1")
    report.text_pairs.append(pair)
    if _modal_color(img, box) != brand.hex_to_rgb(bg):
        report.warnings.append(
            f"Поле «{pair.field}»: фон под текстом не {bg} - контраст не гарантирован.")

    s = text.upper() if b.get("upper") else text
    lay = fit_text(s, fonts[role].path, box[2] - box[0], box[3] - box[1],
                   max_size=float(b["max_size"]) * W,
                   min_size=float(b.get("min_size", 0.012)) * W,
                   tracking=float(b.get("tracking", 0.0)),
                   line_height=float(b.get("line_height", 1.0)),
                   max_lines=int(b.get("max_lines", 1)))
    if not lay.fits:
        report.warnings.append(
            f"Поле «{pair.field}» не помещается в свой блок и обрезано - сократите текст.")
    elif _cap_display_px(lay, report.template_id, W) < MIN_CAP_DISPLAY_PX:
        report.warnings.append(
            f"Поле «{pair.field}» пришлось набрать слишком мелко - на экране его не "
            f"прочитать.  Сократите текст (например, «ГК, ауд. 374» вместо полного адреса).")
    _draw_layout(img, lay, box, fg, b.get("align", "left"), b.get("valign", "top"))
    report.blocks.append(DrawnBlock("text", box, fg, pair.field))


#: Высота прописной буквы на экране зрителя (display_px шаблона), ниже которой
#: текст уже не читается: на посте 360 px в ленте это кегль ~10 px.  Замерено:
#: примеры шаблонов дают не меньше 8.7 px, «Главный корпус, ул. Миклухо-Маклая,
#: 6, аудитория 374» в одну строку места - 4.3 px (серая полоска).
MIN_CAP_DISPLAY_PX = 7.0


def _cap_display_px(lay: TextLayout, template_id: str, canvas_w: int) -> float:
    """Высота прописной «Н» набранного текста в пикселях экрана зрителя."""
    cap = -float(lay.font.getbbox("Н", anchor="ls")[1])
    tpl = TEMPLATES.get(template_id, {})
    return cap * float(tpl.get("display_px", canvas_w)) / max(canvas_w, 1)


def _draw_image(img: Image.Image, b: dict, box: Box, photo: Any,
                face_box: Sequence[float] | None, tpl: dict,
                report: RenderReport) -> None:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    fb_block = None
    if photo is None:
        src = _placeholder_photo(bw, bh)
        report.warnings.append("Фотография не выбрана - вместо неё заглушка.")
        crop = (0, 0, bw, bh)
    else:
        src, scale = _open_photo(photo, int(max(bw, bh) * 1.6))
        fb_src = None
        if face_box is not None:
            fb_src = tuple(float(v) * scale for v in face_box)
        crop = cover_crop_box(src.width, src.height, bw, bh, face_box=fb_src,
                              face_fill=float(b.get("face_fill", 0.0)),
                              max_zoom=float(b.get("max_zoom", 1.0)))
        if fb_src is not None:
            s = bw / max(crop[2] - crop[0], 1)
            fb_block = ((fb_src[0] - crop[0]) * s, (fb_src[1] - crop[1]) * s,
                        (fb_src[2] - crop[0]) * s, (fb_src[3] - crop[1]) * s)
    part = src.crop(crop)
    if part.size != (bw, bh):
        part = part.resize((bw, bh), Image.LANCZOS, reducing_gap=3.0)

    ht = b.get("halftone")
    if ht is None:
        out = part
    else:
        pitch = brand.pitch_for_display(img.width, float(tpl["display_px"]),
                                        dot_display_px=float(ht.get("dot_display_px", 3.5)))
        report.pitch_px = pitch
        out = brand.halftone(part, pitch=pitch, angle=float(ht.get("angle", 45.0)),
                             ink=color(ht.get("ink", "crimson")),
                             paper=color(ht.get("paper", "cream")),
                             preserve_detail=float(ht.get("preserve_detail", 0.85)),
                             unsharp=float(ht.get("unsharp", 0.5)),
                             face_box=fb_block)
    img.paste(out, (x0, y0))
    report.blocks.append(DrawnBlock("photo", box, None, "photo"))


def render(template_id: str, fields: Mapping[str, Any] | None, photo: Any = None, *,
           face_box: Sequence[float] | None = None,
           display_font: str | os.PathLike | None = None,
           text_font: str | os.PathLike | None = None,
           size: tuple[int, int] | None = None) -> Image.Image:
    """Собрать носитель по шаблону.

    template_id   ключ TEMPLATES ('afisha_a3', 'post', 'stories', 'album_cover',
                  'summary', 'quote').
    fields        {'headline': ..., 'date': ..., ...} - см. template_fields().
                  Пустые необязательные поля просто не рисуются.
    photo         путь к JPEG/PNG, PIL.Image или HxWx3 uint8.  Файл открывается
                  только на чтение.  None - заглушка и предупреждение.
    face_box      (left, top, right, bottom) лица в пикселях полноразмерного
                  кадра после EXIF-разворота: кроп не режет голову, а уровни
                  растра ставятся по лицу.
    display_font, text_font
                  файл шрифта, выбранный пользователем (см. resolve_font).
    size          другой размер холста (для быстрого предпросмотра).

    Возвращает RGB-изображение; отчёт - render_report(image): какие
    гарнитуры реально использованы, блоки, пары контраста, предупреждения.
    """
    tpl = _template(template_id)
    values = {k: ("" if v is None else str(v)) for k, v in (fields or {}).items()}
    for spec in tpl["fields"]:
        if spec.kind == "text" and spec.required and not values.get(spec.key, "").strip():
            raise ValueError(f"Не заполнено обязательное поле «{spec.label}».")

    W, H = (int(v) for v in (size or tpl["size"]))
    if W < 64 or H < 64:
        raise ValueError(f"Слишком маленький холст: {W}×{H}")

    fonts = {"display": resolve_font("display", display_font),
             "text": resolve_font("text", text_font)}
    report = RenderReport(template_id, (W, H), int(tpl.get("dpi", 72)), fonts)
    if display_font and fonts["display"].source != "user":
        report.warnings.append(f"Файл шрифта заголовков не подошёл "
                               f"(нет файла или кириллицы): {Path(display_font).name}")
    if fonts["display"].is_substitute:
        report.warnings.append(f"Заголовок набран заменой: {fonts['display'].family} "
                               f"вместо {fonts['display'].brand_family}.")
    if fonts["text"].is_substitute:
        report.warnings.append(f"Основной текст набран заменой: {fonts['text'].family} "
                               f"вместо {fonts['text'].brand_family} "
                               f"(у Radiant Alt нет кириллицы).")

    img = Image.new("RGB", (W, H), brand.hex_to_rgb(color(tpl.get("bg", "cream"))))
    for b in copy.deepcopy(tpl["blocks"]):
        box = _px(b["rect"], W, H)
        kind = b["type"]
        if kind == "rect":
            _fill_rect(img, box, color(b["fill"]), report)
        elif kind == "swatches":
            _draw_swatches(img, b, box, report)
        elif kind == "bands":
            _draw_bands(img, b, box, report)
        elif kind == "image":
            _draw_image(img, b, box, photo, face_box, tpl, report)
        elif kind == "text":
            text = b.get("literal") if b.get("field") is None else values.get(b["field"], "")
            if text and str(text).strip():
                _draw_text(img, b, box, str(text), fonts, report)
        else:
            raise ValueError(f"Неизвестный тип блока {kind!r} - в системе только прямоугольники")

    img.info["poster_report"] = report
    img.info["dpi"] = (report.dpi, report.dpi)
    return img


# --------------------------------------------------------------------------- #
#  60/30/10                                                                    #
# --------------------------------------------------------------------------- #

def usage_report(image: Image.Image, photo_boxes: Iterable[Box] | None = None, *,
                 tolerance: int = 12) -> dict[str, float]:
    """Измеренная доля площади холста по цветам бренда.

    Ключи: cream, graphite, crimson, sand, photo, other (сумма = 1.0) и
    photo_ink - доля краски внутри фото (для справки).  photo_boxes по
    умолчанию берутся из отчёта render().  Пиксель относится к цвету, если
    сумма модулей разностей каналов <= tolerance; сглаженные края букв
    попадают в other.
    """
    arr = np.asarray(image.convert("RGB"))
    H, W = arr.shape[:2]
    if photo_boxes is None:
        rep = render_report(image)
        photo_boxes = rep.photo_boxes if rep else []
    photo_mask = np.zeros((H, W), bool)
    for x0, y0, x1, y1 in photo_boxes:
        photo_mask[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] = True

    total = float(H * W)
    a16 = arr.astype(np.int16)
    out: dict[str, float] = {}
    claimed = photo_mask.copy()
    for name in ("cream", "graphite", "crimson", "sand"):
        ref = np.array(brand.hex_to_rgb(brand.PALETTE[name]), np.int16)
        hit = (np.abs(a16 - ref).sum(axis=2) <= tolerance) & ~claimed
        out[name] = float(hit.sum()) / total
        claimed |= hit
    out["photo"] = float(photo_mask.sum()) / total
    out["other"] = max(0.0, 1.0 - sum(out.values()))

    if photo_mask.any():
        ref_ink = np.array(brand.hex_to_rgb(CRIMSON), np.int16)
        ref_paper = np.array(brand.hex_to_rgb(CREAM), np.int16)
        pix = a16[photo_mask]
        d_ink = np.abs(pix - ref_ink).sum(axis=1).astype(np.float32)
        d_paper = np.abs(pix - ref_paper).sum(axis=1).astype(np.float32)
        out["photo_ink"] = float((d_paper / np.maximum(d_ink + d_paper, 1.0)).mean())
    else:
        out["photo_ink"] = 0.0
    return out


#: Допуски вокруг 60/30/10 - доли площади макета БЕЗ фотографии.
BALANCE_LIMITS: dict[str, tuple[float, float]] = {
    "cream": (0.45, 0.75),
    "graphite": (0.20, 0.40),
    "accent": (0.04, 0.20),
}


def check_60_30_10(report: Mapping[str, float]) -> list[str]:
    """Сверка с правилом 60/30/10.  Пустой список - баланс в норме.

    Фотография из баланса исключается: доли считаются от площади макета без
    неё, поэтому большое фото не даёт ложных срабатываний.
    """
    photo = float(report.get("photo", 0.0))
    layout = max(1e-6, 1.0 - photo)
    share = {
        "cream": float(report.get("cream", 0.0)) / layout,
        "graphite": float(report.get("graphite", 0.0)) / layout,
        "accent": (float(report.get("crimson", 0.0)) + float(report.get("sand", 0.0))) / layout,
    }
    target = {"cream": 60, "graphite": 30, "accent": 10}
    warnings: list[str] = []
    if photo > 0.75:
        warnings.append(f"Фото занимает {photo * 100:.0f} % листа - фирменным цветам "
                        f"почти не осталось места.")
    for key, (lo, hi) in BALANCE_LIMITS.items():
        v = share[key]
        name = brand.PALETTE_RU[key]
        if v < lo:
            warnings.append(f"Мало цвета «{name}»: {v * 100:.0f} % вместо ~{target[key]} %.")
        elif v > hi:
            warnings.append(f"Слишком много цвета «{name}»: {v * 100:.0f} % вместо ~{target[key]} %.")
    return warnings


# --------------------------------------------------------------------------- #
#  ЭКСПОРТ                                                                     #
# --------------------------------------------------------------------------- #

_A3_MM = (297.0, 420.0)


def _new_temp_path(folder: Path, fmt: str) -> str:
    """Создать пустой временный файл рядом с целью (эксклюзивно) и вернуть путь."""
    for _attempt in range(100):
        cand = folder / f".poster_{os.getpid()}_{secrets.token_hex(6)}.{fmt}"
        try:
            with open(cand, "xb"):
                pass
        except FileExistsError:
            continue
        return str(cand)
    raise OSError(f"Не удалось создать временный файл в {folder}")


def export(image: Image.Image, path: str | os.PathLike, format: str | None = None, *,
           dpi: int | None = None) -> Path:
    """Сохранить готовый носитель: 'png', 'jpeg' (качество 95, 4:4:4) или
    'pdf' (страница A3, изображение по центру на кремовом поле).

    Формат по умолчанию берётся из расширения.  Запись атомарная: сначала
    временный файл рядом, потом замена - недописанный файл не останется.
    """
    target = Path(path)
    fmt = (format or target.suffix.lstrip(".")).lower()
    fmt = {"jpg": "jpeg", "jpe": "jpeg"}.get(fmt, fmt)
    if fmt not in ("png", "jpeg", "pdf"):
        raise ValueError(f"Формат {fmt!r} не поддерживается: только PNG, JPEG, PDF")
    rep = render_report(image)
    dpi = int(dpi or (rep.dpi if rep else image.info.get("dpi", (72, 72))[0]) or 72)
    rgb = image.convert("RGB")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Не tempfile.mkstemp: тот создаёт файл с правами 0600, os.replace их
    # сохраняет, и на macOS афиша в общей папке не читалась бы другими
    # пользователями.  open("xb") даёт обычные права по umask.
    tmp = _new_temp_path(target.parent, fmt)
    try:
        if fmt == "png":
            rgb.save(tmp, "PNG", dpi=(dpi, dpi), optimize=False)
        elif fmt == "jpeg":
            rgb.save(tmp, "JPEG", quality=95, subsampling=0, optimize=True,
                     dpi=(dpi, dpi))
        else:
            W, H = rgb.size
            pw_mm, ph_mm = _A3_MM if H >= W else _A3_MM[::-1]
            # страница нужной пропорции, изображение вписано целиком
            page_w = max(W, int(math.ceil(H * pw_mm / ph_mm)))
            page_h = max(H, int(math.ceil(page_w * ph_mm / pw_mm)))
            page_w = int(round(page_h * pw_mm / ph_mm))
            page_w, page_h = max(page_w, W), max(page_h, H)
            page = Image.new("RGB", (page_w, page_h), brand.hex_to_rgb(CREAM))
            page.paste(rgb, ((page_w - W) // 2, (page_h - H) // 2))
            resolution = page_w / (pw_mm / 25.4)
            page.save(tmp, "PDF", resolution=resolution)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


if __name__ == "__main__":        # pragma: no cover - ручная проверка
    for tid, title in list_templates():
        im = render(tid, example_fields(tid), None)
        rep = render_report(im)
        use = usage_report(im)
        print(f"{title:<28} {im.size}  "
              + "  ".join(f"{k} {v * 100:4.1f}%" for k, v in use.items())
              + f"  -> {check_60_30_10(use) or 'баланс в норме'}")
        for w in rep.warnings if rep else []:
            print("    !", w)
