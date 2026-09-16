# -*- coding: utf-8 -*-
"""cull - отбор кадров съёмки: один кадр на серию и покрытие всего события.

WHAT THIS MODULE IS FOR.  After an event the press team has ~300 frames.  The
expensive manual work is not "look at 300 pictures"; it is opening every burst of
2-7 near-identical frames, zooming to 100 % and picking the sharpest one, and then
making sure the end of the event (awards, group photo) is not forgotten.  This
module does exactly that and nothing more:

    scan()      reads every frame once (reduced-size decode), measures subject
                sharpness and exposure, groups bursts and splits the event into
                time segments;
    suggest()   one representative per burst, with a per-segment quota so the
                whole event is covered; flagged frames sink, never vanish;
    full_order() every single frame, suggestions first - nothing is hidden;
    export_selection() / save_selection() / load_selection().

READ-ONLY ON THE PHOTOS.  Source files are only ever opened for reading.  Nothing
here deletes, moves or renames anything.  export_selection() COPIES into a folder
that must not be a source folder, and never overwrites an existing file;
save_selection() refuses to write its JSON into a folder holding the photos.

WHY THE RANKING IS NOT "SORT BY SHARPNESS".  Measured on a real event shoot
(288 frames, Canon EOS 550D, 74 minutes): a plain sharpness top-30
took 24 frames from the first 8.5 minutes, covered 5 five-minute stretches of the
event, held 12 near-duplicates from the same bursts and 8 dark faces.  Sharpness
is comparable only inside one scene (same light, same distance), so:

  * it decides WITHIN a burst (same scene by construction) and WITHIN a segment;
  * it never competes across the event - segments get a quota instead.

THE CAMERA CLOCK IS WRONG.  A body with a flat clock battery reports a date years
off.  Absolute dates are never used or shown; only the ORDER of frames and the
GAPS between them are.  Capture order is (DateTimeOriginal + SubSecTimeOriginal,
file number), not file name: the same folder mixes IMG_ and _MG_ names (sRGB vs
Adobe RGB), and name order puts the whole _MG_ half after the IMG_ half.

FACES ARE OPTIONAL.  OpenCV is imported lazily.  With it, Haar cascades from
cv2.data.haarcascades find faces and sharpness is measured on the subject's face.
Without it (or without the cascade file) every frame uses a centre-weighted
whole-frame measure, and CullResult.mode says "без лиц".

PARALLELISM.  Threads by default: decode (Pillow), numpy and OpenCV all release
the GIL for the heavy parts.  Measured on the 288-frame set, 16 cores - see
DEFAULT_EXECUTOR below.  A process pool is available (executor="process"); its
worker _analyse_one() is module-level, imports nothing GUI-related and works in a
frozen app provided the entry point calls multiprocessing.freeze_support().

MEASURED on that shoot (288 frames, 74 minutes; top-30 suggestions, Haar faces):
                                        sharpness sort   suggest()
    frames from the first 8.5 min            24/30          6/30
    five-minute stretches covered            5              13 of the 14 with frames
    most frames from one 5-min stretch       20             4
    extra frames from an already-used burst  12             0
    picks shot within 10 s of another pick   -              0
    "dark face" flagged                      8              0
    segments of the event covered            -              16/16, incl. the group
                                                            photo 7 minutes after the rest
Without OpenCV the coverage numbers are the same, but 24 of the 30 picks change
and the face checks (dark / blown face) are gone.
Still NOT solved: expression, closed eyes, composition, the same person shot
again a minute later; Haar misses profiles and tilted heads and takes some signs
and posters for faces (a skin-colour and texture veto removes most).

CALLBACKS.  report(done, total) is invoked from the thread that called scan(),
never from a pool thread; a Tk GUI should push it onto a queue.Queue.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import (FIRST_COMPLETED, BrokenExecutor, Executor, Future,
                                 ProcessPoolExecutor, ThreadPoolExecutor, wait)
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from PIL import Image, ImageOps

__all__ = [
    "MODE_FACES",
    "MODE_NO_FACES",
    "FLAG_LABELS",
    "FLAG_PENALTY",
    "face_backend",
    "find_faces",
    "ImageRecord",
    "Burst",
    "Segment",
    "CullResult",
    "ExportItem",
    "scan",
    "suggest",
    "full_order",
    "export_selection",
    "save_selection",
    "load_selection",
]

# --------------------------------------------------------------------------
# Constants.  Values marked "measured" were fitted on the real 288-frame shoot.
# --------------------------------------------------------------------------

JPEG_EXTS: tuple[str, ...] = (".jpg", ".jpeg")
RAW_EXTS: tuple[str, ...] = (".cr2",)

MODE_FACES = "лица"
MODE_NO_FACES = "без лиц"

#: Pillow picks the largest DCT scale whose output is still >= this size:
#: 5184x3456 -> 1/8 -> 648x432.  Asking for (512, 512) silently gives 1/4, 2x cost.
DRAFT_REQUEST = (256, 256)
#: Every analysis buffer is brought to this long side, so sharpness numbers of
#: frames with different native sizes stay comparable.
ANALYSIS_LONG_SIDE = 648

TILE = 64
TILE_MIN_STD = 10.0          # flatter tiles are sensor noise, and noise has the
                             # HIGHEST normalised gradient energy there is
CENTRE_SIGMA = 0.30          # centre weight: exp(-d^2 / 2 sigma^2), d in frame units
CENTRE_FLOOR = 0.25          # ...never below this, a subject off-centre still counts
TOP_TILES = 3

MIN_FACE_PX = 24             # Haar minSize in the 648 px buffer
SUBJECT_MIN_PX = 32          # measured: below this the face core is mostly noise
SUBJECT_CENTRE_W = 0.6       # subject = argmax(area * (1 - w * distance from centre))
FACE_SIZE_EXPONENT = 0.596   # default when too few faces to refit per shoot
HAAR_FILE = "haarcascade_frontalface_alt2.xml"

BURST_GAP_S = 8.0            # measured: gap that still counts as "same moment"
BURST_AHASH = 12             # aHash Hamming distance that counts as "same view"
BURST_HIST = 0.88            # colour-histogram intersection that counts as "same view"
BURST_AHASH_NO_TIME = 6      # stricter rule when the clock is unknown
BURST_QUICK_GAP_S = 2.5      # measured: a hand-held reframe on the same speaker 0.3-2 s
BURST_QUICK_AHASH = 22       # later reaches aHash 14-21; unrelated frames that close
                             # in time were >= 25 apart (or >= 3 s apart)

SEGMENT_BREAK_S = 180.0      # a pause this long always starts a new part of the event
SEGMENT_TARGET_S = 300.0     # longer segments are split at their largest pause...
SEGMENT_MIN_GAP_S = 30.0     # ...if that pause is at least this long...
SEGMENT_MIN_FRAMES = 3       # ...and both halves keep this many frames
SEGMENT_FRAMES_NO_TIME = 20  # without a clock: equal chunks of about this many frames

DIVERSITY_AHASH = 12         # a look-alike of an earlier pick waits: aHash distance...
DIVERSITY_AHASH_NEAR = 16    # ...or a looser aHash distance within DIVERSITY_NEAR_S
DIVERSITY_NEAR_S = 60.0
DIVERSITY_CORR = 0.6         # ...or correlation of 24x24 gray signatures (measured:
                             # 54 of 870 same-segment representative pairs reach it)
NEAR_DUP_S = 10.0            # ...or shot within this many seconds in the same segment.
                             # A look-alike is taken only when a segment has nothing else.
SIGNATURE_PX = 24
SKIN_MIN = 0.30              # measured: a Haar "face" with less skin-coloured core is a
                             # whiteboard, a poster or a sculpture (46 of 438 boxes)
FACE_MAX_TEN_N = 0.40        # measured: real face cores reach 0.28 (390 boxes); printed
                             # letters on a skin-coloured card gave 0.59
QUOTA_EXPONENT = 0.5         # segment weight = bursts ** exponent (see _allocate)

DARK_FACE_MEAN = 45.0        # mean RGB level inside the face box
BLOWN_FACE_CLIP = 0.02       # share of face pixels at >= 250
NOTHING_SHARP_LAPVAR = 120.0  # max tile Laplacian variance
SOFT_MAD_K = 2.5             # robust z below the segment median that counts as soft

#: Flag -> Russian label for the UI.  Flags are advice, never a verdict.
FLAG_LABELS: dict[str, str] = {
    "unreadable": "файл не читается",
    "soft": "нерезкий",
    "blown_face": "пересвет на лице",
    "dark_face": "тёмное лицо",
    "no_face": "лицо не найдено",
}

#: How far a flag sinks a frame.  Order within a burst or segment is
#: (sum of penalties, then sharpness), so any flag sinks below every clean frame.
FLAG_PENALTY: dict[str, int] = {
    "unreadable": 100,
    "soft": 4,
    "blown_face": 2,
    "dark_face": 2,
    "no_face": 1,
}

#: Measured, 288 x 5184x3456 JPEGs, 16-core Windows 11, Python 3.12:
#:   with faces (Haar)   threads 7.4 s   processes 8.1 s   (16 workers)
#:                       threads 10.6 s  processes 10.7 s  (8 workers)
#:   without faces       threads 3.2 s   processes 2.9 s   (16 workers)
#: Per frame on one core: 59 ms reduced decode, 185 ms Haar, 8 ms sharpness.
#: Threads are within 1.1x either way and need no spawn, no pickling of
#: results and no freeze_support() in a frozen app, so they are the default.
DEFAULT_EXECUTOR = "thread"

_SELECTION_FORMAT = "cr2-cull-selection"
_SELECTION_VERSION = 1


# --------------------------------------------------------------------------
# Public data
# --------------------------------------------------------------------------


@dataclass
class ImageRecord:
    """Everything scan() learned about one frame.

    face_box is (x, y, w, h) as FRACTIONS of the upright image, or None.
    t_rel is seconds since the first frame of the shoot (None: no clock);
    it is deliberately relative - the camera clock is wrong in absolute terms.
    """

    path: Path
    capture_order: int = 0
    t_rel: float | None = None
    burst_id: int = 0
    burst_rank: int = 1              # 1 = the burst's representative
    segment_id: int = 0
    sharpness: float = 0.0           # unified log-scale score, higher = sharper
    sharpness_basis: str = ""        # "лицо" | "кадр" | ""
    segment_pct: float = 0.0         # 0..1 percentile of sharpness in its segment
    flags: tuple[str, ...] = ()
    face_box: tuple[float, float, float, float] | None = None
    pair_path: Path | None = None    # the CR2 of a RAW+JPEG pair
    width: int = 0                   # analysis buffer size (upright)
    height: int = 0
    error: str = ""
    ahash: int | None = field(default=None, repr=False)   # 8x8 average hash
    signature: np.ndarray | None = field(default=None, repr=False)  # 24x24 gray, zero-mean unit
    thumbnail: Image.Image | None = field(default=None, repr=False)

    @property
    def flags_ru(self) -> list[str]:
        """Flags as Russian labels."""
        return [FLAG_LABELS.get(f, f) for f in self.flags]

    @property
    def penalty(self) -> int:
        """Sum of FLAG_PENALTY over the flags; 0 for a clean frame."""
        return sum(FLAG_PENALTY.get(f, 0) for f in self.flags)

    @property
    def is_representative(self) -> bool:
        return self.burst_rank == 1


@dataclass
class Burst:
    """Frames shot back to back of the same view.  members are capture orders."""

    id: int
    members: list[int]
    representative: int
    segment_id: int = 0

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class Segment:
    """A stretch of the event between pauses.  Times are relative seconds."""

    id: int
    first: int                       # capture order of the first frame
    last: int                        # capture order of the last frame
    t_start: float | None
    t_end: float | None
    burst_ids: list[int] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return self.last - self.first + 1


@dataclass
class CullResult:
    """Output of scan().  images are in capture order: images[i].capture_order == i."""

    images: list[ImageRecord] = field(default_factory=list)
    bursts: list[Burst] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    mode: str = MODE_NO_FACES
    time_basis: str = ""             # "время съёмки" | "порядок файлов"
    elapsed_s: float = 0.0
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)

    def burst_members(self, burst_id: int) -> list[ImageRecord]:
        """Every frame of a burst, representative first, then by sharpness rank."""
        b = self.bursts[burst_id]
        return sorted((self.images[i] for i in b.members), key=lambda r: r.burst_rank)

    def representatives(self) -> list[ImageRecord]:
        """One frame per burst, in capture order."""
        return [self.images[b.representative] for b in self.bursts]


@dataclass
class ExportItem:
    """One file handled by export_selection()."""

    src: Path
    dst: Path | None = None
    renamed: bool = False
    error: str = ""


# --------------------------------------------------------------------------
# Face detector (lazy, optional)
# --------------------------------------------------------------------------

_tls = threading.local()


def _cascade_path() -> str | None:
    """Path of the Haar cascade inside the cv2 wheel, or None if unusable."""
    try:
        import cv2  # noqa: F401  (lazy on purpose: OpenCV is optional)
    except Exception:
        return None
    try:
        path = os.path.join(cv2.data.haarcascades, HAAR_FILE)
    except Exception:
        return None
    return path if os.path.isfile(path) else None


def face_backend() -> tuple[str, str]:
    """(mode, Russian note).  Decides once, in the caller's thread, whether faces work."""
    try:
        import cv2
    except Exception:
        return MODE_NO_FACES, "OpenCV не установлен: резкость оценивается по центру кадра."
    path = _cascade_path()
    if path is None:
        return MODE_NO_FACES, "В OpenCV нет файла каскада лиц: резкость по центру кадра."
    try:
        if cv2.CascadeClassifier(path).empty():
            return MODE_NO_FACES, "Каскад лиц не загрузился: резкость по центру кадра."
    except Exception as exc:
        return MODE_NO_FACES, "Каскад лиц не загрузился (%s): резкость по центру кадра." % exc
    return MODE_FACES, "Лица ищет OpenCV %s (каскады Хаара)." % cv2.__version__


def _detect_faces(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Haar faces in a uint8 gray buffer.  One classifier per thread (not thread-safe)."""
    import cv2
    det = getattr(_tls, "cascade", None)
    if det is None:
        path = _cascade_path()
        det = cv2.CascadeClassifier(path) if path else None
        _tls.cascade = det
    if det is None or det.empty():
        return []
    boxes = det.detectMultiScale(cv2.equalizeHist(gray), scaleFactor=1.1,
                                 minNeighbors=5, minSize=(MIN_FACE_PX, MIN_FACE_PX))
    return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in boxes]


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------


def _tenengrad_n(a: np.ndarray) -> float:
    """Contrast-normalised Tenengrad: gradient energy / variance.

    Normalising is what makes a dim scene comparable with a bright one; raw
    gradient energy just measures how much contrast the scene contains.
    """
    a = a.astype(np.float32)
    gx = a[1:-1, 2:] - a[1:-1, :-2]
    gy = a[2:, 1:-1] - a[:-2, 1:-1]
    return float((gx * gx + gy * gy).mean()) / (float(a.var()) + 1e-6)


def _frame_stats(gray: np.ndarray) -> tuple[float, float]:
    """(max tile Laplacian variance, centre-weighted frame sharpness in log units).

    The frame measure: per TILE x TILE tile the normalised Tenengrad, tiles too
    flat to hold detail skipped (for them the ratio measures noise), weighted by
    closeness to the frame centre, mean of the best TOP_TILES.  Top tiles rather
    than the mean because a shallow-focus portrait is legitimately soft around
    the subject; centre weight because that is where photographers put it.
    """
    a = gray.astype(np.float32)
    H, W = a.shape
    lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:])
    gx = a[1:-1, 2:] - a[1:-1, :-2]
    gy = a[2:, 1:-1] - a[:-2, 1:-1]
    g2 = gx * gx + gy * gy
    ny, nx = (H - 2) // TILE, (W - 2) // TILE
    if ny == 0 or nx == 0:
        tn = float(g2.mean()) / (float(a.var()) + 1e-6)
        return float(lap.var()), math.log(max(tn, 1e-9))

    def tiles(x: np.ndarray) -> np.ndarray:
        return (x[:ny * TILE, :nx * TILE].reshape(ny, TILE, nx, TILE)
                .transpose(0, 2, 1, 3).reshape(ny * nx, TILE * TILE))

    lap_t, g2_t, a_t = tiles(lap), tiles(g2), tiles(a[1:-1, 1:-1])
    lapvar = float(lap_t.var(axis=1).max())
    var = a_t.var(axis=1)
    tn = g2_t.mean(axis=1) / (var + 1e-6)
    cy = (np.arange(ny) + 0.5) * TILE / (H - 2) - 0.5
    cx = (np.arange(nx) + 0.5) * TILE / (W - 2) - 0.5
    d2 = cy[:, None] ** 2 + cx[None, :] ** 2
    w = np.maximum(np.exp(-d2 / (2 * CENTRE_SIGMA ** 2)), CENTRE_FLOOR).ravel()
    ok = var >= TILE_MIN_STD ** 2
    if not ok.any():
        ok = var >= var.max()
    vals = np.log(np.maximum(tn[ok], 1e-9)) + np.log(w[ok])
    top = np.sort(vals)[-TOP_TILES:]
    return lapvar, float(top.mean())


def _skin_fraction(rgb: np.ndarray) -> float:
    """Share of pixels in the classic YCrCb skin box (Cr 133-173, Cb 77-127).

    Chroma only, so it holds in a dark render too.  Used to veto Haar boxes, not
    to find faces: every skin tone falls in the box, most boards and walls don't.
    """
    if rgb.size == 0:
        return 0.0
    a = rgb.reshape(-1, 3).astype(np.float32)
    cr = 128.0 + 0.5 * a[:, 0] - 0.418688 * a[:, 1] - 0.081312 * a[:, 2]
    cb = 128.0 - 0.168736 * a[:, 0] - 0.331264 * a[:, 1] + 0.5 * a[:, 2]
    return float(((cr >= 133) & (cr <= 173) & (cb >= 77) & (cb <= 127)).mean())


def _plausible_face(core_rgb: np.ndarray, ten_n: float) -> bool:
    """Veto for a Haar box: is its core skin, and skin-like in texture?

    Too little skin-coloured core: a board, a wall, a sculpture.  Skin-coloured
    but with edge energy no real face core reaches: printed letters on a beige
    card or a poster (FACE_MAX_TEN_N).
    """
    return _skin_fraction(core_rgb) >= SKIN_MIN and ten_n <= FACE_MAX_TEN_N


def _ahash(gray: np.ndarray) -> int:
    """8x8 average hash.  Measured best recall on same-moment pairs (74 %)."""
    a8 = np.asarray(Image.fromarray(gray).resize((8, 8), Image.BOX), dtype=np.float32)
    bits = (a8 > a8.mean()).ravel()
    return int(sum(1 << i for i, v in enumerate(bits) if v))


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _parse_time(dt: Any, subsec: Any = None) -> float | None:
    """EXIF 'YYYY:MM:DD HH:MM:SS' (+ sub-seconds) as naive seconds.

    Only differences of this number are ever used.  Computed without the local
    time zone, so a DST change cannot insert a fake one-hour pause.
    """
    if not dt:
        return None
    try:
        text = dt.decode("ascii", "replace") if isinstance(dt, bytes) else str(dt)
        stamp = datetime.strptime(text.strip().rstrip("\x00")[:19], "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    secs = (stamp - datetime(1970, 1, 1)).total_seconds()
    digits = re.sub(r"\D", "", subsec.decode("ascii", "replace")
                    if isinstance(subsec, bytes) else str(subsec or ""))
    if digits:
        secs += float("0." + digits)
    return secs


def _file_number(path: Path) -> int:
    """Camera frame counter from the name (IMG_0042 -> 42), -1 when absent."""
    found = re.findall(r"\d+", path.stem)
    return int(found[-1]) if found else -1


def _open_raw_preview(path: Path) -> tuple[Image.Image, float | None]:
    """Embedded camera JPEG of a CR2 via cr2_core, upright, plus its capture time."""
    import cr2_core
    info = cr2_core.probe(path)
    best = info.best
    if info.error or best is None:
        raise ValueError(info.error or "в CR2 нет встроенного JPEG")
    with open(path, "rb") as fh:
        fh.seek(best.offset)
        data = fh.read(best.length)
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", DRAFT_REQUEST)
    im.load()
    orient, _ = cr2_core._reconcile_orientation(
        info.orientation, best.width, best.height, info.raw_width, info.raw_height,
        best.source)
    method = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
              4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
              6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
              8: Image.Transpose.ROTATE_90}.get(orient)
    if method is not None:
        im = im.transpose(method)
    return im, _parse_time(info.shot_at)


def _measure_faces(rgb: np.ndarray, gray: np.ndarray) -> list[dict[str, Any]]:
    """Haar faces that pass the skin/texture veto, with core sharpness and exposure."""
    H, W = gray.shape
    faces: list[dict[str, Any]] = []
    for (x, y, w, h) in _detect_faces(gray):
        x, y = max(0, x), max(0, y)
        w, h = min(w, W - x), min(h, H - y)
        if w < MIN_FACE_PX or h < MIN_FACE_PX:
            continue
        # Central 60 % of the box only: the rim is hair, collar and wall,
        # whose contrast would dominate the normalisation.
        cx, cy, r = x + w / 2.0, y + h / 2.0, w * 0.3
        rows = slice(int(max(0, cy - r)), int(min(H, cy + r)))
        cols = slice(int(max(0, cx - r)), int(min(W, cx + r)))
        core = gray[rows, cols]
        if core.size < 100:
            continue
        core128 = np.asarray(Image.fromarray(core).resize((128, 128), Image.BILINEAR))
        ten_n = _tenengrad_n(core128)
        if not _plausible_face(rgb[rows, cols], ten_n):
            continue          # Haar false positive: board, poster, sign, sculpture
        box = rgb[y:y + h, x:x + w]
        faces.append({"box": (x, y, w, h), "ten_n": ten_n,
                      "clip_hi": float((box.max(axis=2) >= 250).mean()),
                      "mean": float(box.mean())})
    return faces


def find_faces(image: Image.Image) -> list[tuple[float, float, float, float]]:
    """Faces in an upright PIL image as (x, y, w, h) FRACTIONS, the subject first.

    The same detector and false-positive veto as scan(); the subject is the face
    the photographer framed (_pick_subject), the rest follow by size.  An empty
    list without OpenCV or its cascade.  Touches no Tk: safe in a worker thread.
    """
    if _cascade_path() is None:
        return []
    im = image.convert("RGB")
    if max(im.size) > ANALYSIS_LONG_SIDE:
        im = im.copy()
        im.thumbnail((ANALYSIS_LONG_SIDE, ANALYSIS_LONG_SIDE), Image.BILINEAR)
    rgb = np.asarray(im)
    gray = np.asarray(im.convert("L"))
    H, W = gray.shape
    faces = _measure_faces(rgb, gray)
    subject = _pick_subject(faces, W, H)
    faces.sort(key=lambda f: (f is not subject, -f["box"][2] * f["box"][3]))
    return [(x / W, y / H, w / W, h / H) for (x, y, w, h) in (f["box"] for f in faces)]


def _analyse_one(path_str: str, use_faces: bool, thumb_px: int = 0) -> dict[str, Any]:
    """Measure one frame.  Module-level and GUI-free: runs in threads or processes.

    Opens the file read-only.  Never raises: a broken file comes back with
    'error' set, so it still gets a row and nothing silently disappears.
    """
    out: dict[str, Any] = {"path": path_str, "error": ""}
    try:
        path = Path(path_str)
        if path.suffix.lower() in RAW_EXTS:
            im, t = _open_raw_preview(path)
        else:
            with Image.open(path) as src:
                exif = src.getexif()
                sub = exif.get_ifd(0x8769)
                t = _parse_time(sub.get(36867), sub.get(37521))
                if t is None:
                    t = _parse_time(exif.get(306), sub.get(37520))
                src.draft("RGB", DRAFT_REQUEST)
                im = ImageOps.exif_transpose(src)
                im.load()
        out["t"] = t
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) > ANALYSIS_LONG_SIDE:
            im = im.copy()
            im.thumbnail((ANALYSIS_LONG_SIDE, ANALYSIS_LONG_SIDE), Image.BILINEAR)
        rgb = np.asarray(im)
        gray = np.asarray(im.convert("L"))
        H, W = gray.shape
        out["w"], out["h"] = W, H
        out["lapvar"], out["frame_sharp"] = _frame_stats(gray)
        out["ahash"] = _ahash(gray)
        sig = np.asarray(Image.fromarray(gray).resize((SIGNATURE_PX, SIGNATURE_PX), Image.BOX),
                         dtype=np.float32).ravel()
        sig -= sig.mean()
        out["signature"] = sig / (float(np.linalg.norm(sig)) + 1e-6)
        hist = np.concatenate([np.bincount((rgb[:, :, c] >> 3).ravel(), minlength=32)
                               for c in range(3)]).astype(np.float64)
        out["hist"] = hist / max(hist.sum(), 1.0)

        out["faces"] = _measure_faces(rgb, gray) if use_faces else []
        if thumb_px > 0:
            th = im.copy()
            th.thumbnail((thumb_px, thumb_px), Image.BILINEAR)
            out["thumb"] = th
    except Exception as exc:          # one bad file must not stop the shoot
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _collect(folder_or_paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]]
             ) -> list[tuple[Path, Path | None]]:
    """(file to analyse, CR2 pair or None).  A folder is read non-recursively.

    RAW+JPEG pairs (same folder, same stem) are analysed once, through the
    JPEG; the CR2 rides along as pair_path so it can be exported with it.
    Nothing silently disappears: a second JPEG with the same stem (IMG_1.jpg
    and IMG_1.jpeg, or IMG_1.JPG and img_1.jpg on a case-sensitive volume) and
    a second CR2 are items of their own.  Order: first appearance of the stem.
    """
    if isinstance(folder_or_paths, (str, os.PathLike)):
        root = Path(folder_or_paths)
        if root.is_dir():
            with os.scandir(root) as it:
                candidates = [Path(e.path) for e in it
                              if not e.name.startswith(".") and e.is_file()]
        else:
            candidates = [root]
    else:
        candidates = [Path(p) for p in folder_or_paths]

    seen: set[str] = set()
    by_stem: dict[tuple[str, str], dict[str, list[Path]]] = {}
    order: list[tuple[str, str]] = []
    for p in candidates:
        ext = p.suffix.lower()
        if ext not in JPEG_EXTS and ext not in RAW_EXTS:
            continue
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        stem_key = (os.path.normcase(os.path.abspath(p.parent)), p.stem.lower())
        slot = by_stem.get(stem_key)
        if slot is None:
            slot = by_stem[stem_key] = {"jpeg": [], "raw": []}
            order.append(stem_key)
        slot["raw" if ext in RAW_EXTS else "jpeg"].append(p)
    out: list[tuple[Path, Path | None]] = []
    for k in order:
        jpegs, raws = by_stem[k]["jpeg"], by_stem[k]["raw"]
        if jpegs:
            out.append((jpegs[0], raws[0] if raws else None))
            out.extend((j, None) for j in jpegs[1:])
            out.extend((r, None) for r in raws[1:])
        else:
            out.extend((r, None) for r in raws)
    return out


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def _capture_order(raw: list[dict[str, Any]]) -> tuple[list[int], bool]:
    """Indices sorted into shooting order, and whether the clock was usable.

    Frames without a time borrow the time of their predecessor in file-number
    order, so a stray edited file lands next to its neighbours instead of at
    the start.  With fewer than half the frames timed, file order is used.
    """
    by_name = sorted(range(len(raw)), key=lambda i: (_file_number(Path(raw[i]["path"])),
                                                     Path(raw[i]["path"]).name.lower()))
    timed = sum(1 for r in raw if r.get("t") is not None)
    if timed * 2 < len(raw) or timed == 0:
        return by_name, False
    last = None
    eff: dict[int, float] = {}
    first_t = next(raw[i]["t"] for i in by_name if raw[i].get("t") is not None)
    for rank, i in enumerate(by_name):
        t = raw[i].get("t")
        if t is None:
            t = (last if last is not None else first_t) + 1e-3 * (rank + 1)
        else:
            last = t
        eff[i] = t
    return sorted(range(len(raw)), key=lambda i: (eff[i], _file_number(Path(raw[i]["path"])),
                                                  raw[i]["path"])), True


def _group_bursts(raw: list[dict[str, Any]], timed: bool) -> list[list[int]]:
    """Chain consecutive frames (already in capture order) into bursts.

    Close in TIME and similar in APPEARANCE.  Time alone merges different setups
    shot back to back; appearance alone merges the same podium an hour apart.
    Appearance is aHash OR histogram, because a burst where the subject moved a
    lot fails the hash but keeps its colours.  Within BURST_QUICK_GAP_S a looser
    aHash also joins: the photographer reframing the same speaker by hand.
    No clock: stricter appearance only.
    """
    groups: list[list[int]] = []
    for i, r in enumerate(raw):
        if not groups:
            groups.append([i])
            continue
        p = raw[i - 1]
        joined = False
        if not r["error"] and not p["error"]:
            ham = _hamming(r["ahash"], p["ahash"])
            inter = float(np.minimum(r["hist"], p["hist"]).sum())
            if timed:
                t0, t1 = p.get("t"), r.get("t")
                gap = (t1 - t0) if t0 is not None and t1 is not None else None
                close = gap is not None and 0 <= gap <= BURST_GAP_S
                quick = gap is not None and 0 <= gap <= BURST_QUICK_GAP_S
                joined = close and (ham <= BURST_AHASH or inter >= BURST_HIST
                                    or (quick and ham <= BURST_QUICK_AHASH))
            else:
                joined = ham <= BURST_AHASH_NO_TIME and inter >= BURST_HIST
        if joined:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _split_segments(times: list[float | None], bursts: list[list[int]],
                    timed: bool) -> list[tuple[int, int]]:
    """Segments as (first, last) capture orders, never cutting through a burst.

    From the GAPS only: a pause >= SEGMENT_BREAK_S always splits; a segment
    still longer than SEGMENT_TARGET_S is split at its longest pause of at least
    SEGMENT_MIN_GAP_S whose both sides keep SEGMENT_MIN_FRAMES frames, repeatedly.
    Without a clock the event is cut into equal chunks of whole bursts.
    """
    n = len(times)
    if n == 0:
        return []
    starts = {b[0] for b in bursts}           # a cut is allowed only before these
    if not timed:
        want = max(1, round(n / SEGMENT_FRAMES_NO_TIME))
        size = n / want
        cuts, next_cut = [], size
        for s in sorted(starts):
            if s >= next_cut and s > 0:
                cuts.append(s)
                next_cut = s + size
        bounds = [0] + cuts + [n]
        return [(bounds[k], bounds[k + 1] - 1) for k in range(len(bounds) - 1)]

    def gap(i: int) -> float:                  # pause before frame i
        a, b = times[i - 1], times[i]
        return (b - a) if a is not None and b is not None else 0.0

    cuts = {i for i in starts if i > 0 and gap(i) >= SEGMENT_BREAK_S}
    todo = sorted(cuts)
    bounds = [0] + todo + [n]
    segs = [(bounds[k], bounds[k + 1] - 1) for k in range(len(bounds) - 1)]
    out: list[tuple[int, int]] = []
    while segs:
        a, b = segs.pop(0)
        ta, tb = times[a], times[b]
        if ta is None or tb is None or tb - ta <= SEGMENT_TARGET_S:
            out.append((a, b))
            continue
        best, best_gap = None, SEGMENT_MIN_GAP_S
        for i in range(a + SEGMENT_MIN_FRAMES, b - SEGMENT_MIN_FRAMES + 2):
            if i in starts and gap(i) >= best_gap:
                best, best_gap = i, gap(i)
        if best is None:
            out.append((a, b))
        else:
            segs[0:0] = [(a, best - 1), (best, b)]
    return sorted(out)


def _pick_subject(faces: list[dict[str, Any]], W: int, H: int) -> dict[str, Any] | None:
    """The face the photographer framed, not simply the largest.

    Measured failure of largest-area: bystanders passing the lens and
    back-of-head slivers at the edge.  Rule: drop faces too small to measure,
    prefer faces not cut by the border, maximise area * (1 - w * centre distance).
    """
    cand = [f for f in faces if f["box"][2] >= SUBJECT_MIN_PX]
    if not cand:
        return None
    inner = [f for f in cand if not (f["box"][0] <= 2 or f["box"][1] <= 2
                                     or f["box"][0] + f["box"][2] >= W - 2
                                     or f["box"][1] + f["box"][3] >= H - 2)]
    half = math.hypot(0.5, 0.5)

    def weight(f: dict[str, Any]) -> float:
        x, y, w, h = f["box"]
        d = math.hypot((x + w / 2.0) / W - 0.5, (y + h / 2.0) / H - 0.5) / half
        return w * h * (1.0 - SUBJECT_CENTRE_W * min(d, 1.0))

    return max(inner or cand, key=weight)


def _spearman(a: Sequence[float], b: Sequence[float]) -> float:
    ra = np.argsort(np.argsort(np.asarray(a, dtype=np.float64)))
    rb = np.argsort(np.argsort(np.asarray(b, dtype=np.float64)))
    if ra.size < 3:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _fit_size_exponent(subjects: list[dict[str, Any]]) -> float:
    """k such that log(ten_n) - k*log(face width) does not RANK-correlate with width.

    Face sharpness is confounded by size (a close-up always looks sharper).  OLS
    over all faces over-corrects (measured k = 0.985, rho = -0.168); bisecting on
    the rank correlation of the subjects that will be compared gives |rho| < 0.01.
    """
    ws = np.array([f["box"][2] for f in subjects if f["ten_n"] > 0], float)
    ys = np.array([f["ten_n"] for f in subjects if f["ten_n"] > 0], float)
    if ws.size < 50:
        return FACE_SIZE_EXPONENT
    lw, ly = np.log(np.maximum(ws, 1.0)), np.log(ys)
    lo, hi = 0.0, 2.0
    if _spearman(ly - hi * lw, ws) > 0.0:
        return hi
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        if _spearman(ly - mid * lw, ws) > 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _percentiles(values: Sequence[float]) -> list[float]:
    """Mid-rank percentile of each value within the list, 0..1."""
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return []
    if v.size == 1:
        return [0.5]
    less = (v[None, :] < v[:, None]).sum(axis=1)
    equal = (v[None, :] == v[:, None]).sum(axis=1)
    return list(((less + 0.5 * (equal - 1)) / (v.size - 1)).clip(0, 1))


def _build_result(raw: list[dict[str, Any]], pairs: dict[str, Path | None],
                  mode: str, notes: list[str]) -> CullResult:
    """Turn per-file measurements into records, bursts, segments and flags."""
    res = CullResult(mode=mode, notes=list(notes))
    if not raw:
        res.time_basis = "время съёмки"
        return res
    order, timed = _capture_order(raw)
    raw = [raw[i] for i in order]
    res.time_basis = "время съёмки" if timed else "порядок файлов"
    if not timed:
        res.notes.append("В кадрах нет времени съёмки: серии и отрезки собраны "
                         "по порядку номеров файлов.")
    t0 = next((r["t"] for r in raw if r.get("t") is not None), None)

    # ---- sharpness ---------------------------------------------------------
    subjects: list[dict[str, Any] | None] = []
    for r in raw:
        subjects.append(None if r["error"] or mode != MODE_FACES
                        else _pick_subject(r.get("faces", []), r["w"], r["h"]))
    k = _fit_size_exponent([s for s in subjects if s])
    score: list[float | None] = [None] * len(raw)
    basis = [""] * len(raw)
    subj_vals = []
    for i, s in enumerate(subjects):
        if s is not None:
            score[i] = math.log(max(s["ten_n"], 1e-9)) - k * math.log(max(s["box"][2], 1))
            basis[i] = "лицо"
            subj_vals.append(score[i])
    frame_idx = [i for i, r in enumerate(raw) if score[i] is None and not r["error"]]
    if frame_idx:
        vals = np.array([raw[i]["frame_sharp"] for i in frame_idx], dtype=np.float64)
        if len(subj_vals) >= 5:
            # Rank-match onto the face scale: a calibration, not a measurement.
            pct = (np.argsort(np.argsort(vals)) + 0.5) / vals.size
            vals = np.quantile(np.asarray(subj_vals), pct)
        for i, v in zip(frame_idx, vals):
            score[i] = float(v)
            basis[i] = "кадр"
    floor = min((s for s in score if s is not None), default=0.0) - 1.0

    for ci, r in enumerate(raw):
        rec = ImageRecord(path=Path(r["path"]), capture_order=ci,
                          pair_path=pairs.get(r["path"]), error=r["error"],
                          width=r.get("w", 0), height=r.get("h", 0),
                          ahash=r.get("ahash"), signature=r.get("signature"),
                          thumbnail=r.get("thumb"))
        if r.get("t") is not None and t0 is not None:
            rec.t_rel = float(r["t"] - t0)
        rec.sharpness = float(score[ci]) if score[ci] is not None else floor
        rec.sharpness_basis = basis[ci]
        s = subjects[ci]
        if s is not None:
            x, y, w, h = s["box"]
            rec.face_box = (x / r["w"], y / r["h"], w / r["w"], h / r["h"])
        res.images.append(rec)

    # ---- bursts and segments -------------------------------------------------
    groups = _group_bursts(raw, timed)
    times = [raw[i].get("t") if timed else None for i in range(len(raw))]
    seg_bounds = _split_segments(times, groups, timed)
    seg_of = [0] * len(raw)
    for sid, (a, b) in enumerate(seg_bounds):
        for i in range(a, b + 1):
            seg_of[i] = sid
        res.segments.append(Segment(
            id=sid, first=a, last=b,
            t_start=res.images[a].t_rel, t_end=res.images[b].t_rel))
    for rec in res.images:
        rec.segment_id = seg_of[rec.capture_order]

    # ---- flags (need segments for the robust "soft" test) --------------------
    for seg in res.segments:
        members = res.images[seg.first:seg.last + 1]
        ok = [m for m in members if not m.error]
        pcts = _percentiles([m.sharpness for m in ok])
        for m, p in zip(ok, pcts):
            m.segment_pct = float(p)
        vals = np.array([m.sharpness for m in ok], dtype=np.float64)
        med = float(np.median(vals)) if vals.size else 0.0
        mad = float(np.median(np.abs(vals - med))) * 1.4826 if vals.size else 0.0
        for m in members:
            r = raw[m.capture_order]
            flags: list[str] = []
            if m.error:
                flags.append("unreadable")
            else:
                if r["lapvar"] < NOTHING_SHARP_LAPVAR or (
                        vals.size >= 6 and mad > 0 and m.sharpness < med - SOFT_MAD_K * mad):
                    flags.append("soft")
                s = subjects[m.capture_order]
                if mode == MODE_FACES:
                    if not r.get("faces"):
                        # Faces found but too small to measure (a group photo,
                        # the hall) are NOT "no face": nothing to warn about.
                        flags.append("no_face")
                    if s is not None and s["clip_hi"] > BLOWN_FACE_CLIP:
                        flags.append("blown_face")
                    if s is not None and s["mean"] < DARK_FACE_MEAN:
                        flags.append("dark_face")
            m.flags = tuple(flags)

    for bid, g in enumerate(groups):
        _share_burst_faces(res, raw, g)
        # Inside a burst the scene is the same, so raw sharpness is comparable -
        # but only on ONE basis.  When Haar found the face in some frames and
        # missed it in their twins, face and whole-frame scores would be compared
        # (and the only frame with a detection would win on the missing-face
        # flag alone), so such a burst is ranked on the whole-frame measure.
        mixed = len({res.images[i].sharpness_basis for i in g
                     if not res.images[i].error}) > 1

        def within(i: int) -> float:
            if mixed and not raw[i]["error"]:
                return float(raw[i]["frame_sharp"])
            return res.images[i].sharpness

        ranked = sorted(g, key=lambda i: (res.images[i].penalty, -within(i), i))
        for rank, i in enumerate(ranked, 1):
            res.images[i].burst_id = bid
            res.images[i].burst_rank = rank
        sid = seg_of[g[0]]
        res.bursts.append(Burst(id=bid, members=list(g), representative=ranked[0],
                                segment_id=sid))
        res.segments[sid].burst_ids.append(bid)
    return res


def _share_burst_faces(res: CullResult, raw: list[dict[str, Any]], members: list[int]) -> None:
    """A burst is one view: a face Haar found in one frame is there in its twins.

    Frames of the burst without a detection lose the "no_face" flag (so a
    detector miss cannot sink the sharpest frame) and borrow the face box of the
    best-scored frame that has one (so the poster tab can still frame the face).
    """
    with_face = [i for i in members if res.images[i].face_box is not None]
    if not with_face:
        return
    donor = max(with_face, key=lambda i: res.images[i].sharpness)
    for i in members:
        rec = res.images[i]
        if rec.error or rec.face_box is not None:
            continue
        rec.face_box = res.images[donor].face_box
        rec.flags = tuple(f for f in rec.flags if f != "no_face")


# --------------------------------------------------------------------------
# scan()
# --------------------------------------------------------------------------


def scan(folder_or_paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]], *,
         report: Callable[[int, int], None] | None = None,
         cancel_event: threading.Event | None = None,
         workers: int | None = None,
         executor: str = DEFAULT_EXECUTOR,
         use_faces: bool | None = None,
         thumb_px: int = 0) -> CullResult:
    """Analyse a shoot.  READ-ONLY on every input file.

    Args:
        folder_or_paths: a folder (read non-recursively) or an iterable of files.
            JPEG and CR2 are accepted; a RAW+JPEG pair is analysed once.
        report: called as report(done, total) from THIS thread after each file.
        cancel_event: checked between files; queued files are dropped and the
            result comes back with cancelled=True and only the finished frames.
        workers: pool size (default: CPU count, at most 16).
        executor: "thread" (default, see DEFAULT_EXECUTOR) or "process".
        use_faces: None = use OpenCV if it is importable; False = never.
        thumb_px: if > 0, every record carries a PIL thumbnail this big.
            A GUI must wrap it into ImageTk.PhotoImage on the Tk thread.

    Returns:
        CullResult with images in capture order.
    """
    if executor not in ("thread", "process"):
        raise ValueError("executor должен быть 'thread' или 'process', а не %r" % (executor,))
    started = time.perf_counter()
    items = _collect(folder_or_paths)
    pairs = {str(p): raw for p, raw in items}
    if use_faces is False:
        mode, note = MODE_NO_FACES, "Поиск лиц выключен: резкость по центру кадра."
    else:
        mode, note = face_backend()
    notes = [note]
    total = len(items)
    if workers is None:
        workers = min(16, os.cpu_count() or 4)
    workers = max(1, min(workers, total or 1))
    faces_on = mode == MODE_FACES
    raw: list[dict[str, Any]] = []
    cancelled = False

    def cancelled_now() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if workers == 1:
        for p, _ in items:
            if cancelled_now():
                cancelled = True
                break
            raw.append(_analyse_one(str(p), faces_on, thumb_px))
            if report is not None:
                report(len(raw), total)
    else:
        try:
            cancelled = _run_pool(executor, workers, items, faces_on, thumb_px,
                                  raw, total, report, cancelled_now)
        except BrokenExecutor:
            # A process pool that dies (a sandbox forbidding subprocesses, a
            # child killed for memory) must not cost the user the scan: finish
            # the remaining files in threads.  NB: a frozen app WITHOUT
            # freeze_support() does not end up here - it relaunches itself -
            # which is one more reason threads are the default.
            finished = {r["path"] for r in raw}
            rest = [it for it in items if str(it[0]) not in finished]
            notes.append("Параллельные процессы недоступны, разбор продолжен в потоках.")
            cancelled = _run_pool("thread", workers, rest, faces_on, thumb_px,
                                  raw, total, report, cancelled_now)
    if cancelled_now() and len(raw) < total:
        cancelled = True

    res = _build_result(raw, pairs, mode, notes)
    res.cancelled = cancelled
    if cancelled:
        res.notes.append("Отменено: разобрано %d из %d кадров." % (len(raw), total))
    bad = sum(1 for r in res.images if r.error)
    if bad:
        res.notes.append("Не удалось прочитать файлов: %d (они в конце списка)." % bad)
    res.elapsed_s = time.perf_counter() - started
    return res


def _run_pool(executor: str, workers: int, items: list[tuple[Path, Path | None]],
              faces_on: bool, thumb_px: int, raw: list[dict[str, Any]], total: int,
              report: Callable[[int, int], None] | None,
              cancelled_now: Callable[[], bool]) -> bool:
    """Analyse items in a pool, appending to raw.  Returns True if cancelled.

    Submits a bounded window, not everything: cancel then drops the rest at
    once instead of waiting for 300 queued decodes.  Callbacks run here, in the
    calling thread.  Raises BrokenExecutor if a process pool dies.
    """
    pool: Executor
    cv2_threads = None
    if executor == "process":
        pool = ProcessPoolExecutor(max_workers=workers, initializer=_process_init)
    else:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cull")
        cv2_threads = _limit_cv2_threads() if faces_on else None
    try:
        pending: set[Future[dict[str, Any]]] = set()
        queue = iter(items)
        window = workers * 2
        exhausted = False
        while True:
            while not exhausted and len(pending) < window and not cancelled_now():
                nxt = next(queue, None)
                if nxt is None:
                    exhausted = True
                    break
                pending.add(pool.submit(_analyse_one, str(nxt[0]), faces_on, thumb_px))
            if not pending:
                return cancelled_now()
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for fut in done:
                raw.append(fut.result())
                if report is not None:
                    report(len(raw), total)
            if cancelled_now():
                for fut in pending:
                    fut.cancel()
                return True
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        if cv2_threads is not None:
            _restore_cv2_threads(cv2_threads)


def _process_init() -> None:
    """Process-pool initializer: one OpenCV thread per process, no oversubscription."""
    try:
        import sys
        if "cv2" in sys.modules or _cascade_path():
            import cv2
            cv2.setNumThreads(1)
    except Exception:
        pass


def _limit_cv2_threads() -> int | None:
    """Our pool already uses every core; OpenCV's own pool on top oversubscribes."""
    try:
        import cv2
        old = cv2.getNumThreads()
        cv2.setNumThreads(1)
        return old
    except Exception:
        return None


def _restore_cv2_threads(old: int) -> None:
    try:
        import cv2
        cv2.setNumThreads(old)
    except Exception:
        pass


# --------------------------------------------------------------------------
# suggest() / full_order()
# --------------------------------------------------------------------------


def _similar(a: ImageRecord, b: ImageRecord) -> bool:
    """Same view, even if shot minutes apart (the speaker at the same board)."""
    if a.ahash is not None and b.ahash is not None:
        ham = _hamming(a.ahash, b.ahash)
        if ham <= DIVERSITY_AHASH:
            return True
        if (ham <= DIVERSITY_AHASH_NEAR and a.t_rel is not None and b.t_rel is not None
                and abs(a.t_rel - b.t_rel) <= DIVERSITY_NEAR_S):
            return True
    if a.signature is not None and b.signature is not None:
        return float(np.dot(a.signature, b.signature)) >= DIVERSITY_CORR
    return False


def _allocate(capacity: list[int], n: int) -> list[int]:
    """Split n picks over segments: at least one each (while n allows), the rest
    proportional to bursts ** QUOTA_EXPONENT, never above a segment's capacity.

    The square root is deliberate: a photographer who fires 40 bursts at the
    opening speech should get more of the opening, but not 80 % of the list.
    """
    S = len(capacity)
    quota = [0] * S
    if n <= 0 or S == 0:
        return quota
    live = [i for i in range(S) if capacity[i] > 0]
    if n < len(live):
        # Too few picks for every segment: spread them evenly over the event,
        # choosing the busiest segment in each stretch.
        step = len(live) / n
        for k in range(n):
            chunk = live[int(k * step):max(int((k + 1) * step), int(k * step) + 1)]
            quota[max(chunk, key=lambda i: capacity[i])] = 1
        return quota
    for i in live:
        quota[i] = 1
    weights = {i: capacity[i] ** QUOTA_EXPONENT for i in live}
    for _ in range(n - len(live)):
        # Webster/Sainte-Lague divisor method: each extra pick goes to the
        # segment whose weight per pick already given is the largest.
        open_ = [i for i in live if quota[i] < capacity[i]]
        if not open_:
            break
        i = max(open_, key=lambda j: (weights[j] / (quota[j] + 0.5), -j))
        quota[i] += 1
    return quota


def _near_duplicate(a: ImageRecord, b: ImageRecord) -> bool:
    """A look-alike (_similar) or a frame shot seconds apart in the same segment.

    The time rule catches what appearance misses: the photographer walking
    around the same speaker changes the background, not the moment.
    """
    if (a.segment_id == b.segment_id and a.t_rel is not None and b.t_rel is not None
            and abs(a.t_rel - b.t_rel) <= NEAR_DUP_S):
        return True
    return _similar(a, b)


def _distinct_count(pool: list[ImageRecord]) -> int:
    """How many frames of a best-first pool are not near-duplicates of a better one."""
    kept: list[ImageRecord] = []
    for r in pool:
        if not any(_near_duplicate(r, k) for k in kept):
            kept.append(r)
    return len(kept)


def suggest(result: CullResult, n: int | None = 30) -> list[ImageRecord]:
    """Ordered suggestions: one representative per burst, covering every segment.

    Which frames: every segment gets a quota (_allocate).  A segment's first pick
    is its best representative - clean before flagged, then sharpest within the
    segment.  Further picks never take a near-duplicate of anything already
    chosen (a look-alike, or a frame shot within NEAR_DUP_S in the same segment)
    while the segment still has something else: a 3-minute speech at one board
    yields one board shot, not three.  Quotas are split over the DISTINCT frames
    of each segment first, so the slots a repetitive segment cannot fill go to
    the rest of the event; look-alikes come in only when n asks for more.

    Order: fewer/lighter flags first; among equals, round by round (every
    segment's first pick, then second picks, ...) and in capture order inside a
    round.  Flags sink a frame; they never remove it: n=None returns every
    representative.  Burst members stay reachable via result.burst_members().
    """
    pools: list[list[ImageRecord]] = []
    for seg in result.segments:
        reps = [result.images[result.bursts[b].representative] for b in seg.burst_ids]
        reps.sort(key=lambda r: (r.penalty, -r.segment_pct, r.capture_order))
        pools.append(reps)
    total = sum(len(p) for p in pools)
    n = total if n is None else max(0, min(int(n), total))
    distinct = [_distinct_count(p) for p in pools]
    quota = _allocate(distinct, min(n, sum(distinct)))
    if n > sum(quota):
        spare = [len(p) - q for p, q in zip(pools, quota)]
        quota = [q + e for q, e in zip(quota, _allocate(spare, n - sum(quota)))]
    chosen: list[tuple[int, int, int, ImageRecord]] = []
    taken: list[ImageRecord] = []
    for rnd in range(max(quota, default=0)):
        for sid, pool in enumerate(pools):
            if quota[sid] <= rnd or not pool:
                continue
            if rnd == 0:
                pick = pool[0]
            else:
                pick = min(pool, key=lambda r: (
                    r.penalty,
                    any(_near_duplicate(r, t) for t in taken),
                    -r.segment_pct,
                    r.capture_order))
            pool.remove(pick)
            taken.append(pick)
            chosen.append((pick.penalty, rnd, pick.capture_order, pick))
    chosen.sort(key=lambda c: c[:3])
    return [c[3] for c in chosen]


def full_order(result: CullResult) -> list[ImageRecord]:
    """Every frame exactly once: all suggestions, then the other burst members
    (burst by burst, best first).  Nothing is ever left out."""
    reps = suggest(result, None)
    seen = {r.capture_order for r in reps}
    rest: list[ImageRecord] = []
    for r in reps:
        for m in result.burst_members(r.burst_id):
            if m.capture_order not in seen:
                seen.add(m.capture_order)
                rest.append(m)
    leftovers = [r for r in result.images if r.capture_order not in seen]
    return reps + rest + leftovers


# --------------------------------------------------------------------------
# Export and selection files
# --------------------------------------------------------------------------


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def export_selection(paths: Iterable[str | os.PathLike[str]],
                     out_dir: str | os.PathLike[str],
                     mode: str = "copy", *,
                     cancel_event: threading.Event | None = None,
                     report: Callable[[int, int], None] | None = None) -> list[ExportItem]:
    """COPY the chosen files into out_dir.  Never moves, never overwrites.

    A name already taken (on disk or earlier in this batch, compared
    case-insensitively) becomes name_2, name_3, ...  The destination file is
    created with exclusive mode, so a file that appears concurrently is not
    clobbered either.  out_dir must not be the folder of any source file.
    A half-written copy of OUR OWN making is removed if the copy fails.
    """
    if mode != "copy":
        raise ValueError("Поддерживается только копирование (mode='copy'); "
                         "исходные файлы не перемещаются.")
    srcs: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        p = Path(p)
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            srcs.append(p)
    out = Path(out_dir)
    for s in srcs:
        if _same_dir(s.parent, out):
            raise ValueError("Папка экспорта совпадает с папкой исходных фото: %s" % out)
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    items: list[ExportItem] = []
    for idx, src in enumerate(srcs):
        if cancel_event is not None and cancel_event.is_set():
            break
        item = ExportItem(src=src)
        items.append(item)
        n = 1
        while True:
            name = src.name if n == 1 else "%s_%d%s" % (src.stem, n, src.suffix)
            dst = out / name
            if name.lower() in used or dst.exists():
                n += 1
                continue
            try:
                with open(src, "rb") as fin:
                    try:
                        fout = open(dst, "xb")
                    except FileExistsError:
                        n += 1
                        continue
                    try:
                        with fout:
                            shutil.copyfileobj(fin, fout, 1024 * 1024)
                    except BaseException:
                        try:
                            dst.unlink()
                        except OSError:
                            pass
                        raise
                try:
                    shutil.copystat(src, dst)
                except OSError:
                    pass
                used.add(name.lower())
                item.dst, item.renamed = dst, n > 1
            except OSError as exc:
                item.error = "%s: %s" % (type(exc).__name__, exc)
            break
        if report is not None:
            report(idx + 1, len(srcs))
    return items


def save_selection(json_path: str | os.PathLike[str],
                   paths: Iterable[str | os.PathLike[str]], *,
                   note: str = "") -> Path:
    """Write the chosen files to a JSON file the CALLER chose.

    Refuses a location inside a folder that holds any of the chosen photos:
    photo folders are read-only for this program.  Written atomically.
    """
    target = Path(json_path)
    files = [Path(p) for p in paths]
    for f in files:
        if _same_dir(f.parent, target.parent):
            raise ValueError("Файл отбора нельзя сохранять в папку с фотографиями: %s"
                             % target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": _SELECTION_FORMAT, "version": _SELECTION_VERSION,
               "note": note, "files": [str(f.resolve()) for f in files]}
    tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, target)
    return target


def load_selection(json_path: str | os.PathLike[str]) -> list[Path]:
    """Read a file written by save_selection().  Missing photos are kept in the list."""
    with open(json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or payload.get("format") != _SELECTION_FORMAT:
        raise ValueError("Это не файл отбора кадров: %s" % json_path)
    return [Path(p) for p in payload.get("files", []) if isinstance(p, str)]
