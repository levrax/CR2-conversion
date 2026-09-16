# -*- coding: utf-8 -*-
"""brand - фирменный слой пресс-службы Юридического института РУДН.

Палитра, правило 60/30/10, контраст по WCAG, настоящий амплитудный (AM)
печатный растр и дуотон в линейном свете.  Только numpy + Pillow: ни OpenCV,
ни scipy, ни rawpy - модуль дёшево ложится в сборку PyInstaller и не рисует
текст (текст и шрифты - забота poster.py).

ПРАВИЛО 60/30/10 (из брендбука)
-------------------------------
    60 %  КРЕМ     #FFFAE9  основной: фон листа, крупные поля, «бумага» растра.
    30 %  ГРАФИТ   #3C3C3C  вторичный: плашки с данными, текст на креме.
    10 %  КРАПЛАК  #9D091C  акцент: заголовок, краска растра, одна плашка.
          ПЕСОК    #F5E09C  второй акцент, внутри тех же 10 %.  Только как
                            ПОЛЕ под графитом/краплаком или текст на тёмном;
                            песок на креме - 1.25:1, его не видно.

    Доля считается по ПЛОЩАДИ, а не по числу элементов.

ГЕОМЕТРИЯ
---------
    Только прямоугольники.  Брендбук называет прямоугольник основной и
    единственной фигурой системы.  Круглая точка растра - это не фигура
    макета, а печатный артефакт; других кругов модуль не рисует.

ЧТО ТАКОЕ ЭТОТ РАСТР
--------------------
    Честный AM-растр: центры точек стоят на решётке, повёрнутой на `angle`, с
    шагом `pitch`, а ПЛОЩАДЬ точки следует за тоном.  Упорядоченный дизеринг,
    Флойд-Стейнберг и порог так не умеют - они меняют, КАКИЕ пиксели горят, а
    не размер точки.  Точность площади проверяется численно: см. self_check().

ТОНАЛЬНАЯ КАРТА (исправлено)
----------------------------
    Прототип переводил тон в краску через `gamma=1.8`.  Средне-серый (sRGB 128)
    получал 0.16 краски вместо ~0.84, и афиша печаталась бледно-розовой вместо
    плотной бордовой.  Теперь тон идёт через CIE L*: растр подбирает такое
    покрытие, чтобы ВОСПРИНИМАЕМАЯ светлота отпечатанного поля (краска и
    бумага смешиваются в линейном свете, закон Мюррея-Дэвиса) совпала со
    светлотой исходника.  Средне-серый даёт 0.84 - см. coverage_for_srgb().
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Literal, Sequence, Union

import numpy as np
from PIL import Image, ImageFilter

__all__ = [
    "CREAM", "GRAPHITE", "CRIMSON", "SAND", "PALETTE", "PALETTE_RU",
    "USAGE_RULE", "FORBIDDEN_PAIRS", "WCAG_BODY", "WCAG_LARGE",
    "hex_to_rgb", "hex_to_lin", "srgb_to_linear", "linear_to_srgb",
    "relative_luminance", "contrast_ratio", "lstar_from_y", "y_from_lstar",
    "coverage_from_rho", "rho_from_coverage", "rho_from_coverage_aa",
    "coverage_for_lstar", "coverage_for_srgb", "tone_levels",
    "tone_to_coverage", "screen_alpha", "duotone", "halftone",
    "pitch_from_dots_across", "pitch_from_lpi", "pitch_for_display",
    "resize_linear", "screen_accuracy", "self_check",
]

# --------------------------------------------------------------------------- #
#  ПАЛИТРА                                                                     #
# --------------------------------------------------------------------------- #

CREAM: str = "#FFFAE9"      # основной   -- 60 %
GRAPHITE: str = "#3C3C3C"   # вторичный  -- 30 %
CRIMSON: str = "#9D091C"    # акцентный  -- в пределах 10 %
SAND: str = "#F5E09C"       # акцентный  -- в пределах 10 %

PALETTE: dict[str, str] = {
    "cream": CREAM,
    "graphite": GRAPHITE,
    "crimson": CRIMSON,
    "sand": SAND,
}

#: Русские названия ролей - для сообщений пользователю.
PALETTE_RU: dict[str, str] = {
    "cream": "кремовый",
    "graphite": "графит",
    "crimson": "бордовый",
    "sand": "песочный",
    "accent": "акценты (бордовый + песочный)",
    "photo": "фото",
    "other": "прочее",
}

#: Целевая доля площади по ролям цвета (доли макета без фотографии).
USAGE_RULE: dict[str, float] = {
    "cream": 0.60,
    "graphite": 0.30,
    "accent": 0.10,          # бордовый + песочный вместе
}

#: WCAG 2.2: минимальный контраст для основного текста (AA) и крупного.
WCAG_BODY: float = 4.5
WCAG_LARGE: float = 3.0

#: Пары из палитры, которые выглядят «по-брендовому», но нечитаемы.
#: (текст, фон) -> измеренный контраст WCAG 2.2.  Обе не проходят даже 3:1.
FORBIDDEN_PAIRS: dict[tuple[str, str], float] = {
    (SAND, CREAM): 1.25,
    (GRAPHITE, CRIMSON): 1.31,
}


# --------------------------------------------------------------------------- #
#  sRGB <-> линейный свет, L*                                                  #
# --------------------------------------------------------------------------- #

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b) в 0..255."""
    s = str(h).strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"Некорректный HEX-цвет: {h!r}")
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        raise ValueError(f"Некорректный HEX-цвет: {h!r}") from None


def srgb_to_linear(a: np.ndarray | Sequence[float] | float) -> np.ndarray:
    """Передаточная функция sRGB, точная (не степень 2.2).  Вход в 0..1."""
    a = np.asarray(a, np.float32)
    return np.where(a <= 0.04045, a / 12.92,
                    ((a + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(a: np.ndarray | Sequence[float] | float) -> np.ndarray:
    """Обратная к :func:`srgb_to_linear`, с ограничением в [0, 1]."""
    a = np.clip(np.asarray(a, np.float32), 0.0, 1.0)
    return np.where(a <= 0.0031308, a * 12.92,
                    1.055 * a ** (1.0 / 2.4) - 0.055).astype(np.float32)


def hex_to_lin(h: str) -> np.ndarray:
    """'#RRGGBB' -> тройка в линейном свете, float32[3]."""
    return srgb_to_linear(np.array(hex_to_rgb(h), np.float32) / 255.0)


# LUT на 256 входов: в 3 раза быстрее формулы на каждый пиксель, ошибка 1.5e-7,
# потому что вход и так 8-битный.
_LIN8 = srgb_to_linear(np.arange(256, dtype=np.float32) / 255.0)
_LUMW = np.float32([0.2126, 0.7152, 0.0722])


def relative_luminance(h: str) -> float:
    """Относительная яркость Y цвета '#RRGGBB' (WCAG 2.2 / sRGB)."""
    return float(hex_to_lin(h) @ _LUMW)


def contrast_ratio(fg: str, bg: str) -> float:
    """Контраст по WCAG 2.2 между двумя HEX-цветами, 1..21."""
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _luminance_y(img_u8: np.ndarray) -> np.ndarray:
    """Яркость Y (линейный свет) 8-битного sRGB-массива HxWx3."""
    return (_LIN8[img_u8[..., 0]] * _LUMW[0]
            + _LIN8[img_u8[..., 1]] * _LUMW[1]
            + _LIN8[img_u8[..., 2]] * _LUMW[2])


_EPS = 216.0 / 24389.0          # 0.008856
_KAPPA = 24389.0 / 27.0         # 903.3


def lstar_from_y(y: np.ndarray | float) -> np.ndarray:
    """CIE L* (0..100) из линейной яркости Y (0..1)."""
    y = np.asarray(y, np.float32)
    return np.where(y > _EPS, 116.0 * np.cbrt(np.maximum(y, _EPS)) - 16.0,
                    _KAPPA * y).astype(np.float32)


def y_from_lstar(l: np.ndarray | float) -> np.ndarray:
    """Обратная к :func:`lstar_from_y`: CIE L* -> линейная яркость Y."""
    l = np.asarray(l, np.float32)
    f = (l + 16.0) / 116.0
    return np.where(l > _KAPPA * _EPS, f * f * f, l / _KAPPA).astype(np.float32)


# --------------------------------------------------------------------------- #
#  ГЕОМЕТРИЯ ТОЧКИ -- точная площадь перекрытия и обратная функция            #
# --------------------------------------------------------------------------- #

def coverage_from_rho(rho: np.ndarray | float) -> np.ndarray:
    """Точная площадь краски круглых точек радиуса R на квадратной решётке с
    шагом p, как функция rho = R / p.

    До rho = 0.5 точки не касаются, покрытие pi*rho^2 (максимум pi/4 = 78.54 %).
    Дальше каждая точка заходит к соседям четырьмя сегментами; при
    rho <= 1/sqrt(2) сегменты лежат целиком в круге соседа, поэтому вычесть их
    один раз - точно.  Сплошная заливка ровно при rho = 1/sqrt(2).
    """
    rho = np.asarray(rho, np.float64)
    safe = np.maximum(rho, 0.5)
    seg = (safe ** 2 * np.arccos(np.clip(0.5 / safe, -1.0, 1.0))
           - 0.5 * np.sqrt(np.maximum(safe ** 2 - 0.25, 0.0)))
    c = np.pi * rho ** 2 - 4.0 * np.where(rho > 0.5, seg, 0.0)
    return np.clip(c, 0.0, 1.0)


_RHO_MAX = 1.0 / math.sqrt(2.0)
_RHO_LUT = np.linspace(0.0, _RHO_MAX, 4096)
_COV_LUT = np.maximum.accumulate(coverage_from_rho(_RHO_LUT))


def rho_from_coverage(c: np.ndarray | float) -> np.ndarray:
    """Обратная к :func:`coverage_from_rho` (идеальная жёсткая кромка)."""
    return np.interp(c, _COV_LUT, _RHO_LUT).astype(np.float32)


# Сглаженная кромка меняет площадь точки, и в области перекрытия аналитическая
# поправка pi*aa^2/12 неверна (кромка обрезана границей ячейки соседа).  Поэтому
# закон «покрытие -> радиус» для реально рисуемой кромки обращается численно:
# среднее alpha по ОДНОЙ ячейке и есть отрисованное покрытие, и оно зависит
# только от rho и k = pitch/aa.  Одна таблица на k покрывает все кадры.
_Q_BINS = 4096
_Q_N = 1024
_qg = (np.arange(_Q_N, dtype=np.float64) + 0.5) / _Q_N - 0.5
_qdu, _qdv = np.meshgrid(_qg, _qg, indexing="ij")
_qh, _qe = np.histogram(np.sqrt(_qdu * _qdu + _qdv * _qdv).ravel(),
                        bins=_Q_BINS, range=(0.0, _RHO_MAX + 1e-4))
_Q_CENTRE = (_qe[:-1] + _qe[1:]) * 0.5
_Q_WEIGHT = _qh / _qh.sum()
del _qg, _qdu, _qdv, _qh, _qe


@lru_cache(maxsize=64)
def _aa_radius_lut(k_milli: int) -> tuple[np.ndarray, np.ndarray]:
    """Таблица «отрисованное покрытие -> rho» для растра с pitch/aa = k."""
    k = k_milli / 1000.0
    rho = np.linspace(0.0, _RHO_MAX + 1.0 / k, 2048)
    a = np.clip((rho[:, None] - _Q_CENTRE[None, :]) * k + 0.5, 0.0, 1.0)
    cov = np.maximum.accumulate(a @ _Q_WEIGHT)
    return cov.astype(np.float64), rho.astype(np.float64)


def rho_from_coverage_aa(c: np.ndarray | float, pitch: float,
                         aa: float = 1.0) -> np.ndarray:
    """Покрытие -> R/pitch с поправкой на реально рисуемую сглаженную кромку."""
    cov, rho = _aa_radius_lut(int(round(pitch / aa * 1000.0)))
    return np.interp(c, cov, rho).astype(np.float32)


# --------------------------------------------------------------------------- #
#  ТОН -> ПОКРЫТИЕ КРАСКОЙ (через CIE L*)                                      #
# --------------------------------------------------------------------------- #

def coverage_for_lstar(lstar: np.ndarray | float, *, paper: str = CREAM,
                       ink: str = CRIMSON) -> np.ndarray:
    """Какое покрытие нужно, чтобы поле растра имело светлоту `lstar`.

    Отпечатанное поле смешивает краску и бумагу в ЛИНЕЙНОМ свете (закон
    Мюррея-Дэвиса):  Y = (1 - c) * Y_бумаги + c * Y_краски.  Нужную светлоту
    переводим в Y и решаем относительно c.  Светлота вне диапазона
    [L*_краски, L*_бумаги] недостижима и прижимается к краю: темнее краски -
    сплошная краска, светлее бумаги - чистая бумага.
    """
    y_paper = relative_luminance(paper)
    y_ink = relative_luminance(ink)
    y = y_from_lstar(np.clip(np.asarray(lstar, np.float32), 0.0, 100.0))
    c = (y_paper - y) / max(y_paper - y_ink, 1e-6)
    return np.clip(c, 0.0, 1.0).astype(np.float32)


def coverage_for_srgb(value: int | float, *, paper: str = CREAM,
                      ink: str = CRIMSON) -> float:
    """Целевое покрытие для нейтрально-серого sRGB-кода 0..255 без уровней.

    coverage_for_srgb(128) == 0.84 для краплака на креме: sRGB 128 это
    L* 53.6, а поле такой светлоты требует 84 % площади краски.
    """
    y = float(srgb_to_linear(float(value) / 255.0))
    return float(coverage_for_lstar(lstar_from_y(y), paper=paper, ink=ink))


Box = tuple[float, float, float, float]
Levels = Union[Literal["auto", "none"], tuple[float, float]]


def _box_slice(box: Box, w: int, h: int) -> tuple[slice, slice] | None:
    l, t, r, b = (float(v) for v in box)
    x0 = int(max(0, min(w, math.floor(min(l, r)))))
    x1 = int(max(0, min(w, math.ceil(max(l, r)))))
    y0 = int(max(0, min(h, math.floor(min(t, b)))))
    y1 = int(max(0, min(h, math.ceil(max(t, b)))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return slice(y0, y1), slice(x0, x1)


def tone_levels(lstar: np.ndarray, *, face_box: Box | None = None,
                lo_pct: float = 0.5, hi_pct: float = 99.5,
                face_mid: float = 60.0, face_high: float = 92.0,
                frame_mid: float = 52.0, frame_high: float = 97.0
                ) -> tuple[float, float]:
    """Уровни (lo, hi) в единицах L*: lo станет L* 0, hi - L* 100.

    С лицом (`face_box` = (left, top, right, bottom) в пикселях массива):
    медиана светлоты лица ставится в `face_mid`, её 97-й процентиль - в
    `face_high`.  Так лицо получает середину тонового диапазона растра и
    читается даже на тёмном вечернем кадре.  Усиление ограничено 0.8..3.0,
    чтобы плоское или крошечное лицо не разорвало весь кадр.

    Без лица: то же по всему кадру - медиана в `frame_mid`, процентиль
    `hi_pct` в `frame_high`; чёрная точка не выше процентиля `lo_pct`.
    """
    h, w = lstar.shape
    if face_box is not None:
        sl = _box_slice(face_box, w, h)
        if sl is not None:
            f = lstar[sl]
            step = max(1, int(math.sqrt(f.size / 20000.0)))
            f = f[::step, ::step]
            f_mid, f_hi = (float(v) for v in np.percentile(f, [50.0, 97.0]))
            gain = (face_high - face_mid) / max(f_hi - f_mid, 1e-3)
            gain = float(np.clip(gain, 0.8, 3.0))
            offset = face_mid - gain * f_mid
            return (-offset / gain, (100.0 - offset) / gain)
    # Без лица: та же двухточечная схема по всему кадру.  Медиана кадра идёт
    # в frame_mid, верхний процентиль - почти в белое.  Растяжка «1-й..99-й
    # процентиль в 0..100» на вечерних кадрах этой команды (две трети пикселей
    # темнее sRGB 75) заливала краской весь нижний план сплошной плашкой.
    step = max(1, int(math.sqrt(lstar.size / 250000.0)))
    f_lo, f_mid, f_hi = (float(v) for v in np.percentile(
        lstar[::step, ::step], [lo_pct, 50.0, hi_pct]))
    gain = (frame_high - frame_mid) / max(f_hi - f_mid, 1e-3)
    gain = float(np.clip(gain, 0.8, 3.0))
    offset = frame_mid - gain * f_mid
    # чёрная точка не выше нижнего процентиля: глубокие тени остаются тенями
    offset = max(offset, -gain * f_lo)
    return (-offset / gain, (100.0 - offset) / gain)


def tone_to_coverage(
    img_u8: np.ndarray,
    *,
    paper: str = CREAM,
    ink: str = CRIMSON,
    levels: Levels = "auto",
    face_box: Box | None = None,
    max_ink: float = 0.96,
    unsharp: float = 0.0,
) -> np.ndarray:
    """Фотография (HxWx3 uint8 sRGB) -> требуемое покрытие краской, float32.

    1. Светлота каждого пикселя: CIE L* от линейной яркости.
    2. Уровни: `levels='auto'` - по лицу, если передан `face_box`, иначе по
       робастным процентилям кадра (см. :func:`tone_levels`); `'none'` - без
       уровней; или явная пара (lo, hi) в L*.
    3. Покрытие, при котором растровое поле имеет ровно эту светлоту
       (:func:`coverage_for_lstar`).
    4. `max_ink` оставляет просветы бумаги в самых глубоких тенях, чтобы они
       читались как растр, а не как сплошная плашка.
    5. `unsharp` - нерезкое маскирование канала покрытия ПЕРЕД растрированием
       (обычная допечатная практика), возвращает глаза и губы на крупном шаге.
    """
    arr = np.asarray(img_u8)
    if arr.ndim != 3 or arr.shape[2] < 3 or arr.dtype != np.uint8:
        raise ValueError("tone_to_coverage: ожидается массив HxWx3 uint8")
    lstar = lstar_from_y(_luminance_y(arr[..., :3]))

    if isinstance(levels, str):
        if levels == "none":
            lo, hi = 0.0, 100.0
        elif levels == "auto":
            lo, hi = tone_levels(lstar, face_box=face_box)
        else:
            raise ValueError("tone_to_coverage: levels - 'auto', 'none' или (lo, hi)")
    else:
        lo, hi = (float(v) for v in levels)
    if (lo, hi) != (0.0, 100.0):
        lstar = (lstar - np.float32(lo)) * np.float32(100.0 / max(hi - lo, 1e-3))

    c = coverage_for_lstar(lstar, paper=paper, ink=ink)

    if unsharp > 0.0:
        blur = np.asarray(
            Image.fromarray((c * 255.0 + 0.5).astype(np.uint8))
            .filter(ImageFilter.GaussianBlur(2.0)), np.float32) / 255.0
        c = c + np.float32(unsharp) * (c - blur)
    np.clip(c, 0.0, float(np.clip(max_ink, 0.0, 1.0)), out=c)
    return c.astype(np.float32)


# --------------------------------------------------------------------------- #
#  РАСТР                                                                       #
# --------------------------------------------------------------------------- #

def _cell_tone_grid(cov: np.ndarray, pitch: float, angle_deg: float
                    ) -> tuple[np.ndarray, int, int]:
    """Среднее покрытие внутри каждой ячейки растра (усреднение по площади).

    Возвращает (tone[nj, ni], i0, j0); ячейка (i, j) лежит в [j - j0, i - i0].
    """
    h, w = cov.shape
    th = math.radians(angle_deg)
    ct, st = math.cos(th), math.sin(th)

    k = max(1, int(round(pitch)))
    sw, sh = max(1, w // k), max(1, h // k)
    small = np.asarray(
        Image.fromarray(cov.astype(np.float32), "F").resize((sw, sh), Image.BOX),
        np.float32)
    sx, sy = sw / w, sh / h

    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], np.float64)
    u = (corners[:, 0] * ct + corners[:, 1] * st) / pitch
    v = (-corners[:, 0] * st + corners[:, 1] * ct) / pitch
    i0, i1 = int(math.floor(u.min())) - 1, int(math.ceil(u.max())) + 1
    j0, j1 = int(math.floor(v.min())) - 1, int(math.ceil(v.max())) + 1

    ii = np.arange(i0, i1 + 1, dtype=np.float32) + 0.5
    jj = np.arange(j0, j1 + 1, dtype=np.float32) + 0.5
    xc = pitch * (ii[None, :] * ct - jj[:, None] * st)
    yc = pitch * (ii[None, :] * st + jj[:, None] * ct)

    fx = np.clip(xc * sx - 0.5, 0, max(sw - 1.001, 0.0))
    fy = np.clip(yc * sy - 0.5, 0, max(sh - 1.001, 0.0))
    x0 = fx.astype(np.int32)
    y0 = fy.astype(np.int32)
    x1 = np.minimum(x0 + 1, sw - 1)
    y1 = np.minimum(y0 + 1, sh - 1)
    tx, ty = fx - x0, fy - y0
    tone = (small[y0, x0] * (1 - tx) * (1 - ty)
            + small[y0, x1] * tx * (1 - ty)
            + small[y1, x0] * (1 - tx) * ty
            + small[y1, x1] * tx * ty)
    return tone.astype(np.float32), i0, j0


def screen_alpha(
    cov: np.ndarray,
    pitch: float,
    angle_deg: float = 45.0,
    *,
    preserve_detail: float = 0.0,
    aa: float = 1.0,
    rows: int = 512,
) -> np.ndarray:
    """Поле покрытия -> альфа краски по пикселям (AM-растр), float32.

    `preserve_detail` в [0, 1]: 0 - классический AM, одна ровная точка на
    ячейку по СРЕДНЕМУ ячейки; 1 - как в RIP, кромка точки следует локальному
    тону, поэтому глаза и губы остаются читаемыми.  Сглаживание аналитическое.
    """
    h, w = cov.shape
    pd = float(np.clip(preserve_detail, 0.0, 1.0))
    th = math.radians(angle_deg)
    ct, st = np.float32(math.cos(th)), np.float32(math.sin(th))
    inv_p = np.float32(1.0 / pitch)
    p32 = np.float32(pitch)
    inv_aa = np.float32(1.0 / aa)

    if pd < 1.0:
        tone, i0, j0 = _cell_tone_grid(cov, pitch, angle_deg)
        if pd <= 0.0:
            r_cell = (rho_from_coverage_aa(tone, pitch, aa) * p32).astype(np.float32)

    out = np.empty((h, w), np.float32)
    x = np.arange(w, dtype=np.float32)
    ux = x * (ct * inv_p)
    vx = x * (-st * inv_p)

    for ya in range(0, h, rows):
        yb = min(ya + rows, h)
        y = np.arange(ya, yb, dtype=np.float32)
        u = ux[None, :] + (y * (st * inv_p))[:, None]
        v = vx[None, :] + (y * (ct * inv_p))[:, None]
        fi = np.floor(u)
        fj = np.floor(v)
        du = u - fi - np.float32(0.5)
        dv = v - fj - np.float32(0.5)
        r = np.sqrt(du * du + dv * dv) * p32

        if pd <= 0.0:
            radius = r_cell[fj.astype(np.int32) - j0, fi.astype(np.int32) - i0]
        elif pd >= 1.0:
            radius = rho_from_coverage_aa(cov[ya:yb], pitch, aa) * p32
        else:
            t_cell = tone[fj.astype(np.int32) - j0, fi.astype(np.int32) - i0]
            t = t_cell * np.float32(1.0 - pd) + cov[ya:yb] * np.float32(pd)
            radius = rho_from_coverage_aa(t, pitch, aa) * p32
        np.clip((radius - r) * inv_aa + np.float32(0.5), 0.0, 1.0,
                out=out[ya:yb])
    return out


# --------------------------------------------------------------------------- #
#  ДУОТОН                                                                      #
# --------------------------------------------------------------------------- #

def _duotone_lut(dark: str, light: str, n: int = 1024) -> np.ndarray:
    """uint8[n, 3]: рампа от `dark` к `light`, смешение в ЛИНЕЙНОМ свете."""
    d = hex_to_lin(dark)
    l = hex_to_lin(light)
    s = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    ramp = d[None, :] * (1.0 - s) + l[None, :] * s
    return (linear_to_srgb(ramp) * 255.0 + 0.5).astype(np.uint8)


def duotone(img: Image.Image | np.ndarray, dark: str = CRIMSON,
            light: str = CREAM) -> Image.Image:
    """Фото -> двухцветная рампа `dark` (тени) .. `light` (света).

    Смешение в линейном свете: наивный lerp кодов sRGB даёт грязно-коричневые
    полутона.  Принимает PIL-изображение, HxWx3 uint8 или HxW float в [0, 1]
    (готовая тональная карта: 0 - тёмный, 1 - светлый).  Возвращает RGB.
    """
    arr = np.asarray(img.convert("RGB") if isinstance(img, Image.Image) else img)
    if arr.ndim == 2 and arr.dtype.kind == "f":
        t = np.clip(arr.astype(np.float32), 0.0, 1.0)
    else:
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        t = lstar_from_y(_luminance_y(arr[..., :3])) / 100.0
        np.clip(t, 0.0, 1.0, out=t)
    lut = _duotone_lut(dark, light)
    idx = (t * (lut.shape[0] - 1) + 0.5).astype(np.uint16)
    return Image.fromarray(lut[idx], "RGB")


# --------------------------------------------------------------------------- #
#  ПУБЛИЧНЫЙ ХАЛФТОН                                                           #
# --------------------------------------------------------------------------- #

def halftone(
    img: Image.Image | np.ndarray,
    *,
    pitch: float,
    angle: float = 45.0,
    ink: str = CRIMSON,
    paper: str = CREAM,
    mode: Literal["duotone", "mono"] = "duotone",
    preserve_detail: float = 0.85,
    aa: float = 1.0,
    levels: Levels = "auto",
    face_box: Box | None = None,
    max_ink: float = 0.96,
    unsharp: float = 0.5,
) -> Image.Image:
    """Фотография -> фирменный растр (AM-точка).

    pitch
        Расстояние между центрами точек, в ПИКСЕЛЯХ этого изображения.
        Выводите его из размера показа: :func:`pitch_for_display`.
    angle
        Угол растра, градусы.  45 - как в брендбуке, наименее навязчивый.
    ink, paper
        HEX-цвета.  По умолчанию краплак на креме.
    mode
        'duotone' - RGB, `paper` вне точек, `ink` в точках, смешение в
        линейном свете; 'mono' - 'L', чёрные точки на белом (маска).
    preserve_detail
        0 - классический AM, 1 - кромка следует локальному тону (лица).
    levels, face_box, max_ink, unsharp
        Тональная карта, см. :func:`tone_to_coverage`.  `face_box` =
        (left, top, right, bottom) в пикселях `img`.
    """
    arr = np.asarray(img.convert("RGB") if isinstance(img, Image.Image) else img)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("halftone: ожидается RGB-изображение")
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if not pitch > 1.0:
        raise ValueError("halftone: шаг растра должен быть больше 1 пикселя")
    if mode not in ("duotone", "mono"):
        raise ValueError("halftone: mode должен быть 'duotone' или 'mono'")

    cov = tone_to_coverage(arr, paper=paper, ink=ink, levels=levels,
                           face_box=face_box, max_ink=max_ink, unsharp=unsharp)
    alpha = screen_alpha(cov, pitch, angle, preserve_detail=preserve_detail, aa=aa)

    if mode == "mono":
        return Image.fromarray(((1.0 - alpha) * 255.0 + 0.5).astype(np.uint8), "L")
    lut = _duotone_lut(ink, paper)                   # alpha 0 = бумага, 1 = краска
    idx = ((1.0 - alpha) * (lut.shape[0] - 1) + 0.5).astype(np.uint16)
    return Image.fromarray(lut[idx], "RGB")


# --------------------------------------------------------------------------- #
#  ШАГ РАСТРА И РЕСАЙЗ В ЛИНЕЙНОМ СВЕТЕ                                        #
# --------------------------------------------------------------------------- #

def pitch_from_dots_across(block_width_px: float, dots_across: float) -> float:
    """Сколько точек поперёк блока -> шаг в пикселях."""
    return float(block_width_px) / float(dots_across)


def pitch_from_lpi(dpi: float, lpi: float) -> float:
    """Линиатура (линий на дюйм) -> шаг в пикселях при данном dpi."""
    return float(dpi) / float(lpi)


def pitch_for_display(canvas_width_px: float, display_width_px: float, *,
                      dot_display_px: float = 4.0,
                      min_pitch: float = 2.5) -> float:
    """Шаг растра, при котором точки ВИДНЫ там, где макет реально смотрят.

    Пост 1080 px в ленте показывается шириной ~360 px, то есть втрое мельче.
    Шаг, красивый на 100 %, там сливается в розовую заливку, и фирменная
    фактура пропадает ровно там, где её видит большинство.  Поэтому шаг
    задаётся в пикселях ПОКАЗА: `dot_display_px` пикселей на период при
    ширине макета `display_width_px`, и пересчитывается в пиксели холста.

        pitch_for_display(1080, 360)  -> 12.0 px
        pitch_for_display(3508, 1170) -> 12.0 px  (A3 на 300 dpi ~ 2.1 lpmm)
    """
    if display_width_px <= 0 or canvas_width_px <= 0:
        raise ValueError("pitch_for_display: размеры должны быть положительными")
    scale = float(canvas_width_px) / float(display_width_px)
    return max(float(min_pitch), float(dot_display_px) * max(scale, 1.0))


def resize_linear(img: Image.Image | np.ndarray, size: tuple[int, int],
                  resample: int = Image.LANCZOS) -> Image.Image:
    """Ресайз в ЛИНЕЙНОМ свете.  Обязателен для уменьшения растра.

    Обычный resize усредняет коды sRGB: 50-процентный растр, уменьшенный в
    4 раза, теряет 18 % яркости.  Здесь - 0 %.
    """
    arr = np.asarray(img.convert("RGB") if isinstance(img, Image.Image) else img)
    lin = _LIN8[arr[..., :3]] if arr.dtype == np.uint8 else \
        srgb_to_linear(arr.astype(np.float32) / 255.0)
    small = np.stack(
        [np.asarray(Image.fromarray(np.ascontiguousarray(lin[..., c]), "F")
                    .resize(size, resample), np.float32) for c in range(3)],
        axis=-1)
    return Image.fromarray((linear_to_srgb(small) * 255.0 + 0.5).astype(np.uint8),
                           "RGB")


# --------------------------------------------------------------------------- #
#  САМОПРОВЕРКА                                                                #
# --------------------------------------------------------------------------- #

def screen_accuracy(pitches: Sequence[float] = (4.0, 8.0, 20.0),
                    tones: Sequence[float] = (0.10, 0.25, 0.50, 0.75, 0.95),
                    size: int = 600) -> dict[tuple[float, float], float]:
    """Отрисованное покрытие на плоском поле: {(шаг, запрошено): измерено}.

    Это численное доказательство того, что площадь точки следует за тоном.
    """
    out: dict[tuple[float, float], float] = {}
    for pitch in pitches:
        for c in tones:
            field = np.full((size, size), c, np.float32)
            out[(float(pitch), float(c))] = float(
                screen_alpha(field, pitch, 45.0).mean())
    return out


def self_check(verbose: bool = True) -> int:
    """Проверяет геометрию точки, тон и цветовую математику.  -> число провалов."""
    fails = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += not ok
        if verbose:
            print(f"  [{'OK  ' if ok else 'FAIL'}] {label}"
                  + (f"  {detail}" if detail else ""))

    if verbose:
        print("\n=== ПАЛИТРА И КОНТРАСТ (WCAG 2.2) ===")
        for fg, bg in ((CRIMSON, CREAM), (GRAPHITE, CREAM), (CREAM, CRIMSON),
                       (CREAM, GRAPHITE), (SAND, GRAPHITE), (SAND, CRIMSON),
                       (SAND, CREAM), (GRAPHITE, CRIMSON)):
            print(f"    {fg} на {bg}: {contrast_ratio(fg, bg):5.2f}:1")

    if verbose:
        print("\n=== 1. ГЕОМЕТРИЯ ТОЧКИ ===")
    check("coverage(0) == 0", abs(float(coverage_from_rho(0.0))) < 1e-9)
    check("coverage(0.5) == pi/4",
          abs(float(coverage_from_rho(0.5)) - math.pi / 4) < 1e-9)
    check("coverage(1/sqrt2) == 1", abs(float(coverage_from_rho(_RHO_MAX)) - 1.0) < 1e-6)
    grid = np.linspace(0, 1, 101)
    err = float(np.abs(coverage_from_rho(rho_from_coverage(grid)) - grid).max())
    check("обратная функция точна", err < 2e-3, f"{err:.2e}")

    if verbose:
        print("\n=== 2. ВОСПРОИЗВЕДЕНИЕ ТОНА (плоское поле) ===")
    acc = screen_accuracy()
    worst = max(abs(got - c) for (_, c), got in acc.items())
    if verbose:
        for pitch in sorted({p for p, _ in acc}):
            print("      шаг %4.1f px: " % pitch + "  ".join(
                f"{c:.2f}->{got:.3f}" for (p, c), got in acc.items() if p == pitch))
    check("ошибка площади точки < 1.5 %", worst < 0.015, f"{worst * 100:.2f} %")

    if verbose:
        print("\n=== 3. ТОНАЛЬНАЯ КАРТА ПО L* ===")
    mid = coverage_for_srgb(128)
    check("средне-серый (sRGB 128) -> ~0.84 краски", abs(mid - 0.84) < 0.02,
          f"{mid:.3f}")
    grey = np.full((64, 64, 3), 128, np.uint8)
    got = float(tone_to_coverage(grey, levels="none", max_ink=1.0).mean())
    check("tone_to_coverage без уровней совпадает", abs(got - mid) < 1e-3,
          f"{got:.3f}")
    ramp = np.repeat(np.linspace(0, 255, 256).astype(np.uint8)[None, :, None], 3, 2)
    cov = tone_to_coverage(ramp, levels="none", max_ink=1.0)[0]
    check("покрытие монотонно убывает со светлотой",
          bool(np.all(np.diff(cov) <= 1e-6)))
    # светлота поля растра, измеренная на отрисованном растре, против цели
    field = np.full((480, 480), mid, np.float32)
    alpha = float(screen_alpha(field, 8.0, 45.0).mean())
    y = (1 - alpha) * relative_luminance(CREAM) + alpha * relative_luminance(CRIMSON)
    dl = abs(float(lstar_from_y(y)) - float(lstar_from_y(srgb_to_linear(128 / 255))))
    check("светлота отпечатанного поля = светлоте серого (dL* < 1)", dl < 1.0,
          f"dL* = {dl:.2f}")

    if verbose:
        print("\n=== 4. ДУОТОН И API ===")
    img = duotone(np.linspace(0, 1, 256, dtype=np.float32)[None, :].repeat(8, 0))
    check("duotone -> RGB нужного размера", img.mode == "RGB" and img.size == (256, 8))
    probe = np.random.default_rng(12).integers(0, 256, (200, 300, 3)).astype(np.uint8)
    src = probe.copy()
    d_img = halftone(probe, pitch=8.0)
    m_img = halftone(probe, pitch=8.0, mode="mono")
    check("halftone duotone -> RGB, mono -> L",
          d_img.mode == "RGB" and m_img.mode == "L" and d_img.size == (300, 200))
    check("исходный массив не изменён", np.array_equal(probe, src))
    if verbose:
        print(f"\nПровалено проверок: {fails}")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if self_check() else 0)
