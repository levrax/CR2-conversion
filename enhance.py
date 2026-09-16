# -*- coding: utf-8 -*-
"""enhance - автоулучшение и фильтры для репортажных кадров.

Conservative, DOCUMENTARY tone correction plus a small set of tasteful filters
for the press team's event photos.  This is an official university photo set,
so the rules are strict and deliberate:

  * no skin retouching, no sky replacement, no "AI", no local contrast tricks
    that make a face look unnatural - every operation here is a GLOBAL curve,
    a per-channel white-balance gain or a saturation change;
  * every automatic decision is CLAMPED: a low-key stage shot is never
    stretched to a flat grey, a warm tungsten hall stays warm, and the share of
    newly blown highlights is capped - in the whole frame (_CLIP_ADD_MAX) and
    separately on skin (_SKIN_CLIP_ADD_MAX), so a lit face does not go white;
  * strength 0 returns the input exactly, and strength scales every component
    smoothly (black point, white point, exposure, white balance, vibrance).

THE PIPELINE (fixed order, all on float32 RGB in 0..1, sRGB-encoded):

    auto_enhance:  white balance (LINEAR light, per-channel gain LUT)
                -> tone curve on luma (black/white point, highlight shoulder,
                   exposure curve), applied as a hue-preserving luma ratio
                -> vibrance (muted colours more, skin hues protected)
    filter:        one entry of FILTERS, blended with its input by strength.

Statistics are measured once on a stride-subsampled copy (~400k pixels), and
the curves are then applied in horizontal strips, so an 18 Mpx frame never
needs more than a few tens of megabytes of temporaries and a preview-sized image
gets the same decisions as the full-size one.

MEASURED on a real event (Canon EOS 550D, all 288 frames of the team's own dark
raw render; faces are 622 Haar boxes; the camera's JPEG of the same frames for
reference), at the default strength 0.6:

                              raw render   auto 0.6    camera JPEG
    mean luma                    60.6        91.1         99.1
    luma >= 250                  0.86 %      0.94 %       -
    face core mean luma          74.7       117.2        128.2
    face cores still dark (<60)  203          30           -
    face cores newly >= 2 % blown  -           8 (the camera JPEG clips all 8 harder)

A speaker lit against a dark red curtain: face core luma 77 -> 142, 0.2 % blown
(camera JPEG: 123, 0 %); before the skin cap it was 156 and 4.9 % blown.

The constants below were tuned on those frames, not guessed.

FILES.  process_file() reads JPEG/PNG/TIFF and CR2 (the embedded full-size JPEG,
via cr2_core), bakes the EXIF orientation into the pixels and writes
Orientation=1 (never a double rotation), preserves the rest of the EXIF, writes
atomically (unique temp name + os.replace, via cr2_core._atomic_write), refuses
to overwrite its source and refuses to write into the source folder unless the
caller explicitly allows it.  process_many() fans that out over a thread pool:
numpy and Pillow release the GIL for the heavy work, so no processes (and no
freeze_support concerns) are needed.

Nothing in this module touches Tk.  Workers produce arrays and PIL Images; the
GUI thread wraps them.
"""

from __future__ import annotations

import io
import logging
import os
import struct
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image

import cr2_core

__all__ = [
    "SUPPORTED_EXTS",
    "Params",
    "Filter",
    "FILTERS",
    "FileResult",
    "filter_choices",
    "auto_enhance",
    "apply_filter",
    "process",
    "preview",
    "load_image",
    "to_float",
    "to_image",
    "process_file",
    "process_many",
    "plan_outputs",
]

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

#: Extensions process_file() / process_many() accept as input.
SUPPORTED_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".cr2")

# --------------------------------------------------------------------------
# Tunables (measured on real frames - change only with new measurements)
# --------------------------------------------------------------------------

_LUT_SIZE = 4096
#: Rows per processing strip: bounds the temporaries for an 18 Mpx frame.
_STRIP = 384
#: Pixels in the statistics sample.
_SAMPLE_PIXELS = 400_000

_LUMA = (0.299, 0.587, 0.114)            # Rec.601 on sRGB values, as elsewhere in the app
_LUMA_LIN = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)  # Rec.709, linear light

# Black point: percentile, and a hard ceiling so a hazy frame is cleaned up but
# a legitimately dark tone is never crushed.
_BP_PCT = 0.1
_BP_MAX = 0.035
# White point: percentile and the maximum stretch gain.  The cap is what keeps
# a low-key scene (whose brightest pixel is honestly grey) from being stretched.
_WP_PCT = 99.7
_STRETCH_MAX = 1.5
# Highlight shoulder: above the knee, stretched values roll off into 1.0
# instead of being hard-clipped.
_KNEE = 0.80
# Exposure: the median luma is moved toward the band [_MID_LO, _MID_HI] with a
# finite-slope curve y = x(1+a)/(1+ax).  Inside the band nothing moves.
_MID_LO = 0.46
_MID_HI = 0.58
_LIFT_MAX = 2.0                          # slope at black = 1 + a  <= 3
_DARKEN_MAX = -0.15                      # a high-key frame is darkened only slightly
# Never add more than this share of pixels at or above _CLIP_LEVEL luma.
_CLIP_LEVEL = 250.0 / 255.0
_CLIP_ADD_MAX = 0.003
# ...and never add more than this share of clipped SKIN-coloured pixels, where
# clipping means the red channel: skin is far redder than its luma says.  A lit
# face is 2-3 % of the frame, so the whole-frame cap alone let it blow out
# (measured: a speaker against a dark red curtain went from 0 % to 4-9 % clipped
# face core).  0.01 was chosen on 288 frames / 622 faces at strength 0.6: newly
# blown face cores 14 -> 8 (the rest are faces the camera JPEG clips harder),
# mean face core luma 121 -> 117 (camera JPEG 128), dark faces 28 -> 30.
_SKIN_CLIP_ADD_MAX = 0.01
_SKIN_MIN_SHARE = 0.002                  # fewer skin pixels than this: no skin rule
_BACKOFF_STEPS = 7                       # bisection steps when a cap is hit (1/128)
# White balance: fraction of the estimated correction applied, and a hard cap
# on any channel gain, so a warm indoor scene stays warm.
_WB_AMOUNT = 0.5
_WB_MAX_GAIN = 1.10
# Vibrance: maximum saturation boost for a fully muted colour.
_VIBRANCE = 0.22
_SKIN_PROTECT = 0.8

_EXIF_HEADER = b"Exif\x00\x00"


# --------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------


def _check_img(img: np.ndarray) -> np.ndarray:
    """Validate an H x W x 3 image and return it as float32 (no copy if already)."""
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError("ожидается изображение H x W x 3, получено %r" % (a.shape,))
    if a.dtype != np.float32:
        a = a.astype(np.float32)
    return a


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v)


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


_GRID = np.linspace(0.0, 1.0, _LUT_SIZE, dtype=np.float64)


def _lut_index(x: np.ndarray) -> np.ndarray:
    """Nearest LUT index for values in 0..1 (clamped)."""
    idx = x * (_LUT_SIZE - 1)
    np.clip(idx, 0, _LUT_SIZE - 1, out=idx)
    idx += 0.5
    return idx.astype(np.intp)


def _apply_lut(x: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Evaluate a 1-D LUT (sampled on _GRID) at x; nearest sample, 12-bit."""
    return lut.take(_lut_index(x))


def _curve_lut(pts: Sequence[tuple[float, float]]) -> np.ndarray:
    """Piecewise-linear curve through control points, as a float32 LUT."""
    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    return np.interp(_GRID, xs, ys).astype(np.float32)


# The hot path works on three CONTIGUOUS planes, not on H x W x 3 views: numpy's
# element-wise loops are 2-3x faster on contiguous data, and a.max(axis=2) over
# a 3-long axis is ~20x slower than two np.maximum calls (measured).
Planes = list  # [r, g, b]: H x W contiguous float32 arrays

_F0, _F1, _F2 = (np.float32(c) for c in _LUMA)
_ONE = np.float32(1.0)


def _planes(strip: np.ndarray) -> Planes:
    return [np.ascontiguousarray(strip[..., c]) for c in range(3)]


def _put(strip: np.ndarray, planes: Planes) -> None:
    for c in range(3):
        strip[..., c] = planes[c]


def _luma3(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    y = r * _F0
    y += g * _F1
    y += b * _F2
    return y


def _luma(a: np.ndarray) -> np.ndarray:
    return _luma3(a[..., 0], a[..., 1], a[..., 2])


def _max3(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    m = np.maximum(r, g)
    return np.maximum(m, b, out=m)


def _min3(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    m = np.minimum(r, g)
    return np.minimum(m, b, out=m)


def _strips(h: int) -> Iterable[tuple[int, int]]:
    for y0 in range(0, h, _STRIP):
        yield y0, min(h, y0 + _STRIP)


def _sample(a: np.ndarray) -> np.ndarray:
    """Deterministic stride subsample with about _SAMPLE_PIXELS pixels."""
    h, w = a.shape[:2]
    step = max(1, int(np.ceil(np.sqrt(h * w / _SAMPLE_PIXELS))))
    return a[::step, ::step]


#: Channel values above this are rolled off toward 1.0 rather than clipped.
_GAMUT_KNEE = 0.90
#: Saturation may lift a channel no higher than this (or its own value).
_SAT_CEILING = np.float32(0.96)


def _soft_max(m: np.ndarray, floor: np.ndarray) -> np.ndarray:
    """Shoulder for a channel maximum: identity below _GAMUT_KNEE, -> 1 above.

    Never lower than `floor` (the pixel's own original maximum), so a channel
    that was already at 255 stays there and nothing gets darker than it was.
    """
    head = 1.0 - _GAMUT_KNEE
    rolled = _GAMUT_KNEE + head * (1.0 - np.exp(-(m - _GAMUT_KNEE) / head))
    return np.maximum(np.where(m > _GAMUT_KNEE, rolled, m), floor)


def _luma_curve_planes(pl: Planes, lut: np.ndarray) -> np.ndarray:
    """Apply a tone LUT to luma and carry it to RGB as a ratio, in place.

    Scaling R, G and B by the same factor keeps hue and saturation (no colour
    shift, unlike a per-channel curve).  Where that would push the brightest
    channel past _GAMUT_KNEE, the colour is desaturated toward the new luma so
    that channel rolls off smoothly toward 1.0 instead of piling up at 255 - a
    saturated red jacket keeps its texture.  Luma, and therefore the tone the
    curve asked for, is preserved exactly.

    Returns:
        The new luma plane.
    """
    r, g, b = pl
    y = _luma3(r, g, b)
    y2 = _apply_lut(y, lut)
    tiny = y < 1e-6
    np.maximum(y, np.float32(1e-6), out=y)
    ratio = np.divide(y2, y, out=y)
    for c in pl:
        c *= ratio
    if tiny.any():                     # pure black: ratio is meaningless there
        for c in pl:
            c[tiny] = y2[tiny]
    mx = _max3(r, g, b)
    over = mx > _GAMUT_KNEE
    if over.any():
        yy = y2[over]
        m = mx[over]
        want = np.maximum(_soft_max(m, np.minimum(m / ratio[over], 1.0)), yy)
        k = np.clip((want - yy) / np.maximum(m - yy, 1e-6), 0.0, 1.0)
        for c in pl:
            c[over] = yy + (c[over] - yy) * k
    for c in pl:
        np.clip(c, 0.0, 1.0, out=c)
    return y2


def _saturate_planes(pl: Planes, y: np.ndarray, factor: np.ndarray | float) -> None:
    """Scale chroma around luma `y` by `factor`, in place, gamut-limited.

    `factor` is a scalar, or a plane whose values are all >= 1 (vibrance).  A
    boost may lift the top channel only up to _SAT_CEILING (or its own value,
    if already higher) and push the bottom one only down to 0, so saturation
    never manufactures new 255s or 0s.  A scalar factor <= 1 cannot leave the
    gamut and is applied as it is.
    """
    r, g, b = pl
    f: np.ndarray | np.float32
    if np.ndim(factor) == 0 and float(factor) <= 1.0:
        f = np.float32(factor)
    else:
        mx = _max3(r, g, b)
        mn = _min3(r, g, b)
        lim = np.maximum(mx, _SAT_CEILING)
        lim -= y
        lim /= np.maximum(mx - y, np.float32(1e-6))
        lo = y / np.maximum(y - mn, np.float32(1e-6))
        np.minimum(lim, lo, out=lim)
        np.minimum(lim, np.float32(factor) if np.ndim(factor) == 0 else factor, out=lim)
        f = np.maximum(lim, _ONE, out=lim)
    for c in pl:
        c -= y
        c *= f
        c += y
        np.clip(c, 0.0, 1.0, out=c)


def _apply_luma_curve(strip: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """H x W x 3 wrapper around _luma_curve_planes; returns a new array."""
    pl = _planes(strip)
    _luma_curve_planes(pl, lut)
    return np.stack(pl, axis=2)


# --------------------------------------------------------------------------
# Auto-enhance
# --------------------------------------------------------------------------


_SRGB_TO_LINEAR_LUT = _srgb_to_linear(_GRID).astype(np.float32)


def _wb_gains(sample: np.ndarray, strength: float) -> np.ndarray:
    """Per-channel LINEAR-light gains from a grey-world / white-patch hybrid.

    Grey world alone is fooled by a dominant colour (a red curtain); white
    patch alone by one coloured lamp.  Their geometric mean is steadier, and the
    confidence drops when the two disagree.  Only _WB_AMOUNT of the estimated
    correction is applied and each gain is capped at _WB_MAX_GAIN, so the mood
    of a tungsten-lit hall survives: this nudges a cast, it does not "fix" it.
    """
    # A 12-bit LUT instead of a float64 pow over every sample: statistics do
    # not need more precision, and this was most of a preview's run time.
    lin = _SRGB_TO_LINEAR_LUT.take(_lut_index(sample.reshape(-1, 3))).astype(np.float64)
    mx = lin.max(axis=1)
    lum = lin @ _LUMA_LIN
    ok = (mx < 0.95) & (lum > 0.01)
    if int(ok.sum()) < 500:
        return np.ones(3)
    px, lum = lin[ok], lum[ok]
    grey = px.mean(axis=0)
    top = px[lum >= np.percentile(lum, 98.0)].mean(axis=0)

    def norm(v: np.ndarray) -> np.ndarray:
        v = np.maximum(v, 1e-6)
        return v / float(v @ _LUMA_LIN)

    g_n, t_n = norm(grey), norm(top)
    disagreement = float(np.max(np.abs(np.log(g_n / t_n))))
    confidence = float(np.clip(1.0 - disagreement / 0.6, 0.3, 1.0))
    illum = norm(np.sqrt(g_n * t_n))
    gains = np.exp(-np.log(illum) * _WB_AMOUNT * confidence * strength)
    gains = np.clip(gains, 1.0 / _WB_MAX_GAIN, _WB_MAX_GAIN)
    gains /= float(gains @ _LUMA_LIN)          # a neutral grey keeps its brightness
    return gains


def _wb_luts(gains: np.ndarray) -> list[np.ndarray]:
    """Per-channel sRGB->sRGB LUTs applying a linear gain with a white roll-off.

    The gain fades out over the top of the range, so a clipped white stays
    exactly white (neutral) and a channel near 1.0 is not pushed into clipping.
    """
    lin = _srgb_to_linear(_GRID)
    fade = _smoothstep(0.7, 1.0, lin)
    luts = []
    for g in gains:
        out = lin * (g + (1.0 - g) * fade)
        luts.append(_linear_to_srgb(out).astype(np.float32))
    return luts


def _levels_lut(bp: float, gain: float) -> np.ndarray:
    """(x - bp) * gain with a smooth shoulder instead of a hard clip at 1."""
    x = (_GRID - bp) * gain
    xmax = (1.0 - bp) * gain
    if xmax > 1.0 + 1e-9:
        s = (xmax - _KNEE) / (1.0 - _KNEE)
        t = np.clip((x - _KNEE) / (xmax - _KNEE), 0.0, 1.0)
        rolled = _KNEE + (1.0 - _KNEE) * (1.0 - (1.0 - t) ** s)
        x = np.where(x > _KNEE, rolled, x)
    return np.clip(x, 0.0, 1.0)


def _exposure_a(m: float, strength: float) -> float:
    """Curve parameter `a` moving median `m` toward the mid band, clamped."""
    if m < 1e-3:
        return _LIFT_MAX * strength
    if m < _MID_LO:
        target = m + (_MID_LO - m) * strength
    elif m > _MID_HI:
        target = m + (_MID_HI - m) * strength
    else:
        return 0.0
    a = (target - m) / (m * (1.0 - target))
    return float(np.clip(a, _DARKEN_MAX, _LIFT_MAX))


def _skin_mask(rgb: np.ndarray) -> np.ndarray:
    """Skin-coloured pixels: R >= G >= B, hue ~8..45 degrees, visibly saturated.

    Deliberately loose (beige walls and wood pass too): it only has to contain
    the faces, because it is used to hold the tone curve back, never to edit.
    """
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    spread = r - b
    hue = (g - b) / np.maximum(spread, 1e-4)
    return ((r >= g) & (g >= b) & (spread >= 0.06) & (spread >= 0.12 * r)
            & (hue >= 0.13) & (hue <= 0.75))


def _tone_lut(y: np.ndarray, strength: float,
              skin: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """Build the full tone LUT from sample luma `y`, honouring the clip caps.

    `skin` is (luma, brightest channel) of the skin-coloured sample pixels.  If
    the curve would blow more than _CLIP_ADD_MAX of the frame - or more than
    _SKIN_CLIP_ADD_MAX of the skin - the whole correction is backed off (all
    components together, so the balance between them does not change) just
    enough that it does not.  Skin is
    predicted exactly as _luma_curve_planes will render it: scaled by the luma
    ratio, then through the channel shoulder.
    """
    ys = np.sort(y.ravel())
    n = ys.size
    if skin is not None and skin[0].size < max(200, _SKIN_MIN_SHARE * n):
        skin = None
    if skin is not None:
        skin_y = np.maximum(skin[0].astype(np.float64), 1e-6)
        skin_max = skin[1].astype(np.float64)
        skin_idx = _lut_index(skin[0].astype(np.float32))
        skin_before = float(np.mean(skin_max >= _CLIP_LEVEL))

    def pct(p: float) -> float:
        return float(ys[min(n - 1, int(p / 100.0 * n))])

    bp_full = min(pct(_BP_PCT), _BP_MAX)
    wp = pct(_WP_PCT)
    gain_full = float(np.clip(1.0 / max(wp - bp_full, 1e-3), 1.0, _STRETCH_MAX))
    idx = _lut_index(ys)
    clipped_before = float(np.mean(ys >= _CLIP_LEVEL))

    def curve(backoff: float) -> np.ndarray | None:
        """The LUT at strength * backoff, or None if it breaks a clip cap."""
        s = strength * backoff
        lut = _levels_lut(bp_full * s, gain_full ** s)
        m = float(np.median(lut.take(idx)))
        a = _exposure_a(m, s)
        if a != 0.0:
            lut = lut * (1.0 + a) / (1.0 + a * lut)
        after = float(np.mean(lut.take(idx) >= _CLIP_LEVEL))
        if after - clipped_before > _CLIP_ADD_MAX:
            return None
        if skin is not None:
            scaled = skin_max * (lut.take(skin_idx) / skin_y)
            skin_after = _soft_max(scaled, np.minimum(skin_max, 1.0))
            if float(np.mean(skin_after >= _CLIP_LEVEL)) - skin_before > _SKIN_CLIP_ADD_MAX:
                return None
        return lut.astype(np.float32)

    full = curve(1.0)
    if full is not None:
        return full
    # Back off as little as the caps allow: clipping grows with strength, so
    # bisect for the strongest curve that still honours them.
    best, lo, hi = None, 0.0, 1.0
    for _ in range(_BACKOFF_STEPS):
        mid = 0.5 * (lo + hi)
        cand = curve(mid)
        if cand is None:
            hi = mid
        else:
            best, lo = cand, mid
    return best if best is not None else _GRID.astype(np.float32)


def _vibrance_planes(pl: Planes, y: np.ndarray, amount: float) -> None:
    """Boost muted colours more than saturated ones; protect skin hues. In place.

    Skin is detected the cheap, robust way: R >= G >= B with a hue between
    roughly 8 and 45 degrees.  Those pixels get only (1 - _SKIN_PROTECT) of the
    boost, so faces do not turn orange.
    """
    r, g, b = pl
    mx = _max3(r, g, b)
    sat = mx - _min3(r, g, b)
    sat /= np.maximum(mx, np.float32(1e-4))
    skin = g - b
    skin /= np.maximum(r - b, np.float32(1e-4))      # hue 0..1 == 0..60 degrees when ordered
    skin -= np.float32(0.45)
    np.abs(skin, out=skin)
    skin *= np.float32(-1.0 / 0.33)
    skin += _ONE
    np.clip(skin, 0.0, 1.0, out=skin)
    skin *= (r >= g) & (g >= b)                      # skin weight 0..1
    skin *= np.float32(-_SKIN_PROTECT)
    skin += _ONE                                     # 1 - protect * skin
    np.subtract(_ONE, sat, out=sat)
    sat *= sat
    sat *= skin
    sat *= np.float32(amount)
    sat += _ONE                                      # factor = 1 + boost
    _saturate_planes(pl, y, sat)


def _enhance_plan(a: np.ndarray, strength: float, white_balance: bool,
                  vibrance: bool) -> tuple[list[np.ndarray] | None, np.ndarray, float]:
    """Measure once: (white-balance LUTs or None, tone LUT, vibrance amount)."""
    sample = np.ascontiguousarray(_sample(a))
    wb_luts = None
    if white_balance:
        gains = _wb_gains(sample, strength)
        if np.max(np.abs(gains - 1.0)) > 1e-4:
            wb_luts = _wb_luts(gains)
            sample = np.stack([_apply_lut(sample[..., c], wb_luts[c]) for c in range(3)], axis=2)
    y = _luma(sample)
    mask = _skin_mask(sample)
    skin_px = sample[mask]
    tone = _tone_lut(y, strength, (y[mask], skin_px.max(axis=1)) if skin_px.size else None)
    return wb_luts, tone, (_VIBRANCE * strength if vibrance else 0.0)


def _enhance_inplace(a: np.ndarray, strength: float, white_balance: bool = True,
                     vibrance: bool = True) -> None:
    """auto_enhance, writing the result back into `a` strip by strip."""
    wb_luts, tone, vib = _enhance_plan(a, strength, white_balance, vibrance)
    for y0, y1 in _strips(a.shape[0]):
        s = a[y0:y1]
        pl = _planes(s)
        if wb_luts is not None:
            pl = [_apply_lut(pl[c], wb_luts[c]) for c in range(3)]
        y = _luma_curve_planes(pl, tone)
        if vib > 0.0:
            _vibrance_planes(pl, y, vib)
        _put(s, pl)


def auto_enhance(img: np.ndarray, strength: float = 0.6, *,
                 white_balance: bool = True, vibrance: bool = True) -> np.ndarray:
    """Conservative documentary auto-correction of one frame.

    Args:
        img: H x W x 3 float32 RGB in 0..1 (sRGB-encoded, as decoded from JPEG).
        strength: 0..1; 0 returns an exact copy of the input, and every
            component (black/white point, exposure, white balance, vibrance)
            scales smoothly with it.  Values outside 0..1 are clamped.
        white_balance: nudge a colour cast in linear light (clamped, partial).
        vibrance: gently saturate muted colours, protecting skin hues.

    Returns:
        A new float32 array of the same shape, values in 0..1.
    """
    a = _check_img(img)
    out = np.array(a, dtype=np.float32, copy=True)
    strength = _clamp01(strength)
    if strength <= 0.0:
        return out
    _enhance_inplace(out, strength, white_balance, vibrance)
    return out


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Region:
    """Where a strip sits in the full frame (filters that depend on position)."""

    y0: int
    height: int
    width: int
    ctx: Any = None


FilterFn = Callable[[np.ndarray, _Region], np.ndarray]


@dataclass(frozen=True)
class Filter:
    """One entry of the filter registry.

    fn(strip, region) receives a float32 strip of the frame (a view it must
    not modify) and returns the fully filtered strip; blending with strength is
    done by apply_filter().  prepare(height, width), when present, runs once per
    frame and its result arrives as region.ctx.
    """

    id: str
    title: str             # RUSSIAN, shown in the UI
    description: str       # RUSSIAN, one line, for a tooltip
    fn: FilterFn
    prepare: Callable[[int, int], Any] | None = None


_LIFT_LUT = _curve_lut([(0, 0.0), (0.03, 0.05), (0.2, 0.28), (0.5, 0.56), (0.8, 0.83), (1, 1)])
_CONTRAST_LUT = _curve_lut([(0, 0), (0.25, 0.18), (0.5, 0.5), (0.75, 0.82), (1, 1)])
_BW_LUT = _curve_lut([(0, 0), (0.25, 0.21), (0.5, 0.51), (0.75, 0.81), (1, 1)])
_WARM_R = _curve_lut([(0, 0.015), (0.5, 0.545), (1, 1)])
_WARM_B = _curve_lut([(0, 0), (0.5, 0.46), (1, 0.975)])


def _f_none(a: np.ndarray, _r: _Region) -> np.ndarray:
    return a


def _take3(a: np.ndarray, lut: np.ndarray) -> Planes:
    return [_apply_lut(np.ascontiguousarray(a[..., c]), lut) for c in range(3)]


def _f_shadows(a: np.ndarray, _r: _Region) -> np.ndarray:
    """Lift shadows, keep highlights - hue-preserving (luma ratio)."""
    return _apply_luma_curve(a, _LIFT_LUT)


def _f_contrast(a: np.ndarray, _r: _Region) -> np.ndarray:
    pl = _take3(a, _CONTRAST_LUT)
    _saturate_planes(pl, _luma3(*pl), 1.10)
    return np.stack(pl, axis=2)


def _f_bw(a: np.ndarray, _r: _Region) -> np.ndarray:
    g = _apply_lut(_luma3(*_planes(a)), _BW_LUT)
    return np.stack([g, g, g], axis=2)


def _f_warm(a: np.ndarray, _r: _Region) -> np.ndarray:
    pl = _planes(a)
    return np.stack([_apply_lut(pl[0], _WARM_R), pl[1], _apply_lut(pl[2], _WARM_B)], axis=2)


def _f_vignette(a: np.ndarray, r: _Region) -> np.ndarray:
    """Soft darkening toward the corners (about -35 % at the very corner)."""
    ys = (np.arange(r.y0, r.y0 + a.shape[0], dtype=np.float32) + 0.5) / r.height * 2.0 - 1.0
    xs = (np.arange(r.width, dtype=np.float32) + 0.5) / r.width * 2.0 - 1.0
    rad = np.sqrt(ys[:, None] ** 2 + xs[None, :] ** 2) / np.float32(np.sqrt(2.0))
    mask = (1.0 - 0.35 * _smoothstep(0.35, 1.0, rad) ** 1.2).astype(np.float32)
    return a * mask[..., None]


def _registry(items: Sequence[Filter]) -> dict[str, Filter]:
    return {f.id: f for f in items}


#: Filter registry, in UI order.  "none" is always first.
#:
#: Deliberately short: these are official university photos.  "Film + grain"
#: (mottled skin at 1:1), "Cold" (lilac-grey skin) and "Matte" (lifted blacks on
#: frames that are already flat) were tried on real event frames and removed.
#: A saved preset naming a removed filter falls back to "none" in the UI.
FILTERS: dict[str, Filter] = _registry([
    Filter("none", "Без фильтра", "Кадр без стилизации", _f_none),
    Filter("shadows", "Поднять тени", "Высветлить тёмные места, не трогая света", _f_shadows),
    Filter("contrast", "Контраст", "Плотнее тени и света, чуть насыщеннее цвет", _f_contrast),
    Filter("bw", "Чёрно-белый", "Монохром с мягким S-контрастом", _f_bw),
    Filter("warm", "Тёплый", "Лёгкий сдвиг в тёплые тона", _f_warm),
    Filter("vignette", "Виньетка", "Мягкое затемнение к краям кадра", _f_vignette),
])


def filter_choices() -> list[tuple[str, str]]:
    """[(id, Russian title)] in UI order."""
    return [(f.id, f.title) for f in FILTERS.values()]


def _filter_inplace(a: np.ndarray, filter_id: str, strength: float) -> None:
    flt = FILTERS[filter_id]
    h, w = a.shape[:2]
    ctx = flt.prepare(h, w) if flt.prepare is not None else None
    s = np.float32(strength)
    for y0, y1 in _strips(h):
        strip = a[y0:y1]
        res = flt.fn(strip, _Region(y0, h, w, ctx))
        if strength >= 1.0:
            strip[...] = res
        else:
            strip += (res - strip) * s
    np.clip(a, 0.0, 1.0, out=a)


def apply_filter(img: np.ndarray, filter_id: str, strength: float = 1.0) -> np.ndarray:
    """Apply one registered filter, blended with the input by `strength`.

    Args:
        img: H x W x 3 float32 RGB in 0..1.
        filter_id: a key of FILTERS.
        strength: 0..1 (clamped); 0 returns an exact copy of the input.

    Raises:
        ValueError: unknown filter_id.
    """
    if filter_id not in FILTERS:
        raise ValueError("неизвестный фильтр: %r" % (filter_id,))
    a = _check_img(img)
    out = np.array(a, dtype=np.float32, copy=True)
    strength = _clamp01(strength)
    if strength <= 0.0 or filter_id == "none":
        return out
    _filter_inplace(out, filter_id, strength)
    return out


# --------------------------------------------------------------------------
# The combined pipeline
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    """Everything that decides how a frame is processed.

    Accepted wherever a `params` argument appears; a plain mapping with the
    same keys works too (see Params.of).
    """

    enhance_strength: float = 0.6
    filter_id: str = "none"
    filter_strength: float = 1.0
    white_balance: bool = True
    vibrance: bool = True

    def validated(self) -> "Params":
        """Clamped copy; raises ValueError for an unknown filter id."""
        if self.filter_id not in FILTERS:
            raise ValueError("неизвестный фильтр: %r" % (self.filter_id,))
        return replace(self,
                       enhance_strength=_clamp01(float(self.enhance_strength)),
                       filter_strength=_clamp01(float(self.filter_strength)),
                       white_balance=bool(self.white_balance),
                       vibrance=bool(self.vibrance))

    @classmethod
    def of(cls, params: "Params | Mapping[str, Any] | None") -> "Params":
        """Normalise a Params, a mapping of its fields, or None (defaults)."""
        if params is None:
            return cls().validated()
        if isinstance(params, Params):
            return params.validated()
        known = set(cls.__dataclass_fields__)
        extra = set(params) - known
        if extra:
            raise ValueError("неизвестные параметры: %s" % ", ".join(sorted(extra)))
        return cls(**dict(params)).validated()

    def describe(self) -> str:
        """Short Russian summary for status lines."""
        parts = []
        if self.enhance_strength > 0:
            parts.append("автоулучшение %d %%" % round(self.enhance_strength * 100))
        if self.filter_id != "none" and self.filter_strength > 0:
            parts.append("фильтр «%s» %d %%" % (FILTERS[self.filter_id].title,
                                                round(self.filter_strength * 100)))
        return ", ".join(parts) or "без изменений"


def _process_inplace(a: np.ndarray, p: Params) -> None:
    if p.enhance_strength > 0.0:
        _enhance_inplace(a, p.enhance_strength, p.white_balance, p.vibrance)
    if p.filter_id != "none" and p.filter_strength > 0.0:
        _filter_inplace(a, p.filter_id, p.filter_strength)


def process(img: np.ndarray, *, enhance_strength: float = 0.6, filter_id: str = "none",
            filter_strength: float = 1.0, white_balance: bool = True,
            vibrance: bool = True) -> np.ndarray:
    """Auto-enhance, then filter - always in that order.

    Returns a new float32 array; the input is never modified.  With
    enhance_strength=0 and filter "none" (or filter_strength=0) the result is an
    exact copy of the input.
    """
    p = Params(enhance_strength, filter_id, filter_strength, white_balance, vibrance).validated()
    out = np.array(_check_img(img), dtype=np.float32, copy=True)
    _process_inplace(out, p)
    return out


def to_float(im: Image.Image) -> np.ndarray:
    """PIL Image -> H x W x 3 float32 in 0..1 (16-bit greyscale kept at full depth)."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        g = np.asarray(im, dtype=np.float32) / np.float32(65535.0)
        np.clip(g, 0.0, 1.0, out=g)
        return np.repeat(g[..., None], 3, axis=2)
    if im.mode != "RGB":
        im = im.convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    a /= np.float32(255.0)
    return a


def to_image(a: np.ndarray) -> Image.Image:
    """float32 0..1 -> 8-bit RGB PIL Image (rounded, strip by strip)."""
    a = _check_img(a)
    out = np.empty(a.shape, dtype=np.uint8)
    for y0, y1 in _strips(a.shape[0]):
        s = a[y0:y1] * np.float32(255.0)
        s += np.float32(0.5)
        np.clip(s, 0.0, 255.0, out=s)
        out[y0:y1] = s.astype(np.uint8)
    return Image.fromarray(out, "RGB")


def preview(img: Image.Image | np.ndarray, params: Params | Mapping[str, Any] | None = None,
            max_side: int = 1400) -> Image.Image:
    """Fast UI path: downscale to max_side, process, return an 8-bit PIL Image.

    Safe to call from a worker thread (it touches no Tk object); the Tk thread
    wraps the returned Image in ImageTk.PhotoImage.  Decisions (curves, white
    balance) are measured on the downscaled frame and match the full-size ones
    closely, because they come from percentiles, not from pixel counts.
    """
    p = Params.of(params)
    if isinstance(img, np.ndarray):
        im = to_image(img)
    else:
        im = img
    if max_side and max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
    a = to_float(im)
    if a.base is not None or not a.flags.writeable:
        a = np.array(a, copy=True)
    _process_inplace(a, p)
    return to_image(a)


# --------------------------------------------------------------------------
# Loading: orientation and EXIF
# --------------------------------------------------------------------------

_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,     # PIL rotates CCW: 270 CCW == 90 CW
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


@dataclass
class _Loaded:
    image: Image.Image                     # oriented, pixels final
    exif: bytes | None = None              # b"Exif\0\0" + TIFF, Orientation already 1
    icc: bytes | None = None
    notes: list[str] = field(default_factory=list)


def _tiff_ifd(tiff: bytearray, off: int, e: str) -> tuple[list[tuple[int, int, int, int]], int]:
    """[(entry_pos, tag, type, count)] and the position of the next-IFD pointer."""
    if off < 8 or off + 2 > len(tiff):
        raise ValueError("IFD за пределами EXIF")
    n = struct.unpack_from(e + "H", tiff, off)[0]
    end = off + 2 + 12 * n
    if n > 1024 or end + 4 > len(tiff):
        raise ValueError("IFD повреждён")
    out = []
    for i in range(n):
        pos = off + 2 + 12 * i
        tag, typ, count = struct.unpack_from(e + "HHI", tiff, pos)
        out.append((pos, tag, typ, count))
    return out, end


def _patch_exif(payload: bytes, width: int, height: int) -> bytes:
    """Rewrite EXIF in place: Orientation=1, pixel dimensions, no thumbnail link.

    Everything else stays byte-identical, which is exactly why this is done in
    place rather than by re-serialising: Canon's MakerNote stores offsets
    relative to the TIFF header, so any re-layout silently corrupts it (and
    Pillow's own Exif.tobytes() raises on real 550D files).  The IFD1 link is
    zeroed because the embedded thumbnail still shows the unprocessed,
    unrotated frame; the bytes stay, unreferenced.

    Raises:
        ValueError: the block is not a parseable TIFF.
    """
    body = payload[6:] if payload.startswith(_EXIF_HEADER) else payload
    tiff = bytearray(body)
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        raise ValueError("нет заголовка TIFF")
    e = "<" if tiff[:2] == b"II" else ">"
    if struct.unpack_from(e + "H", tiff, 2)[0] != 42:
        raise ValueError("неверная сигнатура TIFF")
    ifd0, nxt = _tiff_ifd(tiff, struct.unpack_from(e + "I", tiff, 4)[0], e)
    exif_off = 0
    for pos, tag, typ, count in ifd0:
        if tag == 0x0112 and typ == 3 and count >= 1:
            struct.pack_into(e + "H", tiff, pos + 8, 1)
        elif tag == 0x8769 and typ in (4, 13) and count >= 1:
            exif_off = struct.unpack_from(e + "I", tiff, pos + 8)[0]
    struct.pack_into(e + "I", tiff, nxt, 0)
    if exif_off:
        entries, _ = _tiff_ifd(tiff, exif_off, e)
        for pos, tag, typ, count in entries:
            if tag in (0xA002, 0xA003) and count == 1:
                v = width if tag == 0xA002 else height
                if typ == 3 and v <= 0xFFFF:
                    struct.pack_into(e + "HH", tiff, pos + 8, v, 0)
                elif typ == 4:
                    struct.pack_into(e + "I", tiff, pos + 8, v)
    out = _EXIF_HEADER + bytes(tiff)
    if len(out) > 65533:
        raise ValueError("EXIF больше 64 КБ")
    return out


def _orient_generic(im: Image.Image, notes: list[str]) -> tuple[Image.Image, int]:
    """Bake EXIF orientation into pixels; return (image, orientation applied).

    Double-rotation guard: if Orientation says 5..8 but the pixel array's
    aspect (portrait vs landscape) is ALREADY the opposite of the recorded
    PixelX/YDimension, some editor rotated the pixels and left the tag behind.
    Rotating again would put the picture on its side, so the pixels are kept as
    they are.  The aspect, not the exact size, is compared: a draft-decoded
    preview is smaller than the recorded size but must decide the same way.
    """
    try:
        exif = im.getexif()
        orientation = int(exif.get(0x0112, 1) or 1)
        sub = exif.get_ifd(0x8769)
        pw, ph = int(sub.get(0xA002, 0) or 0), int(sub.get(0xA003, 0) or 0)
    except Exception:
        return im, 1
    if orientation not in _TRANSPOSE:
        return im, 1
    w, h = im.size
    if (orientation in (5, 6, 7, 8) and pw and ph and pw != ph and w != h
            and (w > h) != (pw > ph)):
        notes.append("поворот уже применён к пикселям — повторно не поворачивается")
        return im, 1
    return im.transpose(_TRANSPOSE[orientation]), orientation


def _rgb_icc(icc: bytes | None) -> bytes | None:
    """The profile if it describes RGB data, else None.

    The output is always RGB.  A CMYK or grey profile copied from the source
    would make colour-managed viewers read RGB pixels as CMYK or grey.  The
    colour space signature sits at bytes 16..20 of the ICC header.
    """
    if not icc or len(icc) < 20 or bytes(icc[16:20]) != b"RGB ":
        return None
    return bytes(icc)


def _load_cr2(src: Path, keep_exif: bool, max_side: int | None = None) -> _Loaded:
    info = cr2_core.probe(src)
    preview_ = info.best
    if preview_ is None:
        raise ValueError(info.error or "в CR2 нет встроенного JPEG")
    f, view, ifd0, exif_entries, blob, icc, _xmp, _thumb = cr2_core._open_for_convert(
        src, info, preview_)
    try:
        im = Image.open(io.BytesIO(blob))
        if max_side:
            im.draft("RGB", (max_side, max_side))
        im.load()
        orientation, reconciled = cr2_core._reconcile_orientation(
            info.orientation, preview_.width, preview_.height,
            info.raw_width, info.raw_height, preview_.source)
        loaded = _Loaded(im, icc=_rgb_icc(icc))
        if reconciled:
            loaded.notes.append("поворот уже применён к пикселям превью — повторно не поворачивается")
        if orientation in _TRANSPOSE:
            loaded.image = im.transpose(_TRANSPOSE[orientation])
        if keep_exif and max_side is None:
            w, h = loaded.image.size
            try:
                app1, dropped = cr2_core._build_exif_app1(
                    view, ifd0, exif_entries, w, h, 1, None, cr2_core.ConvertOptions())
                loaded.exif = app1[4:]
                if dropped:
                    loaded.notes.append("из EXIF удалено: %s" % ", ".join(dropped))
            except cr2_core.Cr2Error as exc:
                loaded.notes.append("EXIF не перенесён (%s)" % exc)
        return loaded
    finally:
        f.close()


def _load(src: Path, keep_exif: bool, max_side: int | None = None) -> _Loaded:
    if src.suffix.lower() in cr2_core.CR2_EXTS:
        return _load_cr2(src, keep_exif, max_side)
    notes: list[str] = []
    payload: bytes | None = None
    # Everything that may need the file handle (TIFF tags, EXIF) happens inside
    # the with-block, so the source is closed deterministically - an open handle
    # on Windows would block the user from renaming or deleting the original.
    with Image.open(src) as im:
        if max_side and im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))
        im.load()
        raw_exif = im.info.get("exif")
        icc = im.info.get("icc_profile") or None
        oriented, _applied = _orient_generic(im, notes)
        if keep_exif and max_side is None:
            if isinstance(raw_exif, (bytes, bytearray)) and raw_exif:
                payload = bytes(raw_exif)
            else:
                try:
                    ex = im.getexif()
                    if len(ex):
                        payload = ex.tobytes()
                except Exception as exc:      # Pillow cannot serialise some MakerNotes
                    notes.append("EXIF не перенесён (%s)" % exc)
    loaded = _Loaded(oriented, icc=_rgb_icc(icc), notes=notes)
    if keep_exif and max_side is None:
        if payload:
            w, h = oriented.size
            try:
                loaded.exif = _patch_exif(payload, w, h)
            except (ValueError, struct.error) as exc:
                # Unpatched EXIF could carry a stale Orientation: dropping it is
                # the only way to be sure the output is not rotated twice.
                notes.append("EXIF не перенесён (%s)" % exc)
    return loaded


def load_image(path: str | Path, max_side: int | None = None) -> Image.Image:
    """Open a JPEG/PNG/TIFF/CR2 as an oriented 8-bit RGB PIL Image (for the UI).

    Args:
        path: the photo.  A CR2 yields its embedded full-size camera JPEG.
        max_side: when given, JPEG decoding uses DCT scaling (draft mode) and the
            result is shrunk to fit - several times faster for previews.

    Raises:
        OSError / ValueError / cr2_core.Cr2Error: the file cannot be read.
    """
    loaded = _load(Path(path), keep_exif=False, max_side=max_side)
    im = loaded.image
    if im.mode.startswith("I"):
        # 16-bit grey: scaled to 8 bits, not clipped (convert("RGB") would turn
        # everything above 255 white), and Pillow cannot shrink "I;16" at all.
        im = to_image(to_float(im))
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.BILINEAR, reducing_gap=2.0)
    return im


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


@dataclass
class FileResult:
    """Outcome of processing one file."""

    src: Path
    dst: Path | None = None
    ok: bool = False
    skipped: bool = False
    message: str = ""        # RUSSIAN, one line, user-facing
    width: int = 0
    height: int = 0
    seconds: float = 0.0


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _same_file(a: Path, b: Path) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return cr2_core._dst_key(a) == cr2_core._dst_key(b)


def _encode_jpeg(im: Image.Image, quality: int, exif: bytes | None, icc: bytes | None) -> bytes:
    """Encode with Pillow; put our EXIF APP1 right after SOI, as Exif requires."""
    q = max(1, min(100, int(quality)))
    kw: dict[str, Any] = {"quality": q, "optimize": True}
    if q >= 95:
        kw["subsampling"] = 0
    if icc:
        kw["icc_profile"] = icc
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    data = buf.getvalue()
    if exif:
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
        data = cr2_core._splice(data, [app1])
    return data


def process_file(src: str | Path, dst: str | Path, *,
                 enhance_strength: float = 0.6,
                 filter_id: str = "none",
                 filter_strength: float = 1.0,
                 white_balance: bool = True,
                 vibrance: bool = True,
                 quality: int = 92,
                 keep_exif: bool = True,
                 overwrite: bool = False,
                 allow_source_dir: bool = False) -> FileResult:
    """Process one photo into a new JPEG.

    Never raises for a problem with the file itself - the Russian reason is in
    FileResult.message.  The source is opened read-only and is never modified.

    Args:
        src: JPEG, PNG, TIFF or CR2 (the embedded camera JPEG is used).
        dst: output path; must end in .jpg or .jpeg.
        enhance_strength, filter_id, filter_strength, white_balance, vibrance:
            see Params.
        quality: JPEG quality 1..100.
        keep_exif: carry the EXIF over (Orientation becomes 1, since the
            rotation is baked into the pixels; the stale thumbnail is dropped).
        overwrite: replace an existing dst (never src - that is always refused).
        allow_source_dir: permit writing into the folder that holds src.

    Raises:
        ValueError: unknown filter_id (a programming error, not a file problem).
    """
    t0 = time.perf_counter()
    p = Params(enhance_strength, filter_id, filter_strength, white_balance, vibrance).validated()
    src, dst = Path(src), Path(dst)
    res = FileResult(src=src, dst=dst)

    def done(message: str, *, ok: bool = False, skipped: bool = False) -> FileResult:
        res.ok, res.skipped, res.message = ok, skipped, message
        res.seconds = time.perf_counter() - t0
        return res

    if dst.suffix.lower() not in (".jpg", ".jpeg"):
        return done("Ошибка: результат сохраняется только в JPEG (.jpg)")
    if _same_file(src, dst):
        return done("Отказано: путь результата совпадает с исходным файлом — оригинал не перезаписывается")
    if not allow_source_dir and _same_dir(src.parent, dst.parent):
        return done("Отказано: запись в папку с оригиналами выключена — выберите другую папку")
    if not src.is_file():
        return done("Ошибка: файл не найден — %s" % src.name)
    if not overwrite and dst.exists():
        return done("Пропущен: файл уже существует — %s" % dst.name, skipped=True)

    try:
        loaded = _load(src, keep_exif)
        arr = to_float(loaded.image)
        if not arr.flags.writeable:
            arr = np.array(arr, copy=True)
        loaded.image = None  # type: ignore[assignment]   # free the decoded frame early
        _process_inplace(arr, p)
        out = to_image(arr)
        del arr
        res.width, res.height = out.size
        data = _encode_jpeg(out, quality, loaded.exif, loaded.icc)
    except MemoryError:
        return done("Ошибка: не хватило памяти для %s" % src.name)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop a batch
        log.debug("не удалось обработать %s", src, exc_info=True)
        return done("Ошибка: не удалось обработать %s — %s" % (src.name, exc))

    try:
        cr2_core._atomic_write(dst, data, src, overwrite=overwrite)
    except FileExistsError:
        return done("Пропущен: файл уже существует — %s" % dst.name, skipped=True)
    except OSError as exc:
        return done("Ошибка: не удалось записать %s — %s" % (dst.name, exc))

    parts = ["Готово: %dx%d, %s" % (res.width, res.height, p.describe())]
    if keep_exif and loaded.exif is None and not any("EXIF" in n for n in loaded.notes):
        parts.append("EXIF в исходном файле нет")
    parts.extend(loaded.notes)
    return done("; ".join(parts), ok=True)


def plan_outputs(paths: Iterable[str | Path], out_dir: str | Path,
                 suffix: str = "") -> list[tuple[Path, Path | None, str]]:
    """Unique output names for a batch, in input order, decided before any work.

    <stem><suffix>.jpg, then <stem><suffix>_2.jpg, _3, ... - skipping names
    already used in this batch AND files already on disk (nothing is ever
    overwritten).  The suffix is part of the planned name, so every file is
    written once, under its final name - never renamed afterwards.  A RAW+JPEG
    pair (IMG_0001.CR2 + IMG_0001.JPG) yields IMG_0001.jpg and IMG_0001_2.jpg;
    the very same file listed twice gets dst None.

    Returns:
        [(src, dst or None, Russian note or '')].
    """
    out = Path(out_dir)
    used: set[str] = set()
    seen: set[str] = set()
    plan: list[tuple[Path, Path | None, str]] = []
    for raw in paths:
        src = Path(raw)
        key = cr2_core.input_key(src)
        if key in seen:
            plan.append((src, None, "Пропущен: этот файл уже есть в списке"))
            continue
        seen.add(key)
        stem = src.stem + suffix
        base = out / (stem + ".jpg")
        cand, n = base, 1
        while cr2_core._dst_key(cand) in used or cand.exists():
            n += 1
            cand = base.with_name("%s_%d.jpg" % (stem, n))
        used.add(cr2_core._dst_key(cand))
        note = "" if cand == base else "имя %s занято — сохранено как %s" % (base.name, cand.name)
        plan.append((src, cand, note))
    return plan


def process_many(paths: Sequence[str | Path], out_dir: str | Path,
                 params: Params | Mapping[str, Any] | None = None, *,
                 workers: int | None = None,
                 report: Callable[[int, int, FileResult], None] | None = None,
                 cancel_event: threading.Event | None = None,
                 quality: int = 92,
                 keep_exif: bool = True,
                 allow_source_dir: bool = False,
                 suffix: str = "") -> list[FileResult]:
    """Process a batch into out_dir on a thread pool.

    Args:
        paths: sources, in the order results are returned.
        out_dir: destination folder (created if needed).  Existing files in it
            are never overwritten - colliding names get _2, _3, ...
        params: Params or a mapping of its fields.
        workers: threads; default min(4, CPU count).  Each 18 Mpx frame needs
            roughly 350 MB while it is being processed.
        report: called as report(done, total, result) after every file, always
            from the thread that called process_many (never from a pool thread),
            so a GUI can forward it into its queue.
        cancel_event: checked before each file starts; files not yet started
            are returned as skipped with "Отменено".  A file already in progress
            is finished (its write is atomic either way).
        quality, keep_exif, allow_source_dir: see process_file.
        suffix: appended to every output stem (see plan_outputs).

    Returns:
        One FileResult per input path, in input order.

    Raises:
        ValueError: invalid params (unknown filter id or unknown keys).
    """
    p = Params.of(params)
    items = list(paths)
    total = len(items)
    plan = plan_outputs(items, out_dir, suffix)
    results: list[FileResult | None] = [None] * total
    if workers is None:
        workers = min(4, os.cpu_count() or 1)
    workers = max(1, int(workers))

    def job(i: int) -> FileResult:
        src, dst, note = plan[i]
        if dst is None:
            return FileResult(src=src, dst=None, skipped=True, message=note)
        r = process_file(src, dst, enhance_strength=p.enhance_strength, filter_id=p.filter_id,
                         filter_strength=p.filter_strength, white_balance=p.white_balance,
                         vibrance=p.vibrance, quality=quality, keep_exif=keep_exif,
                         overwrite=False, allow_source_dir=allow_source_dir)
        if note and r.ok:
            r.message = "%s; %s" % (r.message, note)
        return r

    # Jobs are handed to the pool only as slots free up, and always from THIS
    # thread after report() has run, so a cancel set inside report() (or by the
    # GUI in between) stops every file that has not started yet - no queue of
    # pre-submitted work runs on after the user pressed "Отмена".
    done = 0
    pending = list(range(total))
    pending.reverse()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enhance") as pool:
        running: dict[Future[FileResult], int] = {}
        while pending or running:
            while pending and len(running) < workers:
                if cancel_event is not None and cancel_event.is_set():
                    break
                i = pending.pop()
                running[pool.submit(job, i)] = i
            if not running:
                break                                  # cancelled: nothing in flight
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                i = running.pop(fut)
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001 - defensive: job() already catches
                    r = FileResult(src=plan[i][0], message="Ошибка: %s" % exc)
                results[i] = r
                done += 1
                if report is not None:
                    report(done, total, r)
    for i in reversed(pending):
        r = FileResult(src=plan[i][0], dst=None, skipped=True, message="Отменено")
        results[i] = r
        done += 1
        if report is not None:
            report(done, total, r)
    return [r for r in results if r is not None]
