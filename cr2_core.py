# -*- coding: utf-8 -*-
"""cr2_core - ядро конвертера Canon CR2 -> JPEG (без потерь, как снято).

Pure-stdlib core for extracting the embedded JPEG preview from Canon CR2 files
and writing it out as a standalone .jpg.

WHAT THIS TOOL ACTUALLY PRODUCES (read this before changing anything):

    CR2 -> a full-size JPEG that looks exactly the way the CR2 looks in a
    viewer: the CAMERA's own rendering (as-shot Picture Style, white balance,
    contrast), copied out losslessly and fast.

It does NOT apply Canon DPP edits, and it never can: a DPP recipe is an opaque
parameter set that only Canon's own rendering engine can execute.  No
pure-Python tool renders it.

The original product idea - "DPP refreshes the embedded full-size preview on
Save, so copying the preview bytes reproduces the DPP-edited look" - is REFUTED,
and the refutation is stated here plainly so nobody re-adds the claim:

  * Canon DPP 3.x has a File-menu command "Add thumbnail to image and save"
    that is SEPARATE from plain "Save" - which proves plain Save does not
    refresh the embedded imagery.  DPP 4.x has no such command at all.
  * Saving a recipe only appends/replaces a "CANON OPTIONAL DATA" trailer at
    EOF; the TIFF body of the CR2 (IFD0 preview, IFD1 thumbnail, EXIF) is left
    byte-identical.  Users report Explorer/FastStone/IrfanView thumbnails and
    EXIF Orientation staying unchanged after a DPP rotate + save.
  * DPP's own edit-reflecting preview, when it exists at all, lives INSIDE the
    VRD trailer in an IHLData block (0xffff00f5, IHL record tag 4) - invisible
    to every standard TIFF/EXIF reader, undocumented, and not guaranteed to
    track the current recipe.  It is therefore an OPT-IN source here
    (ConvertOptions.prefer_dpp_preview) and, when it is used as a last-resort
    fallback because nothing else is usable, that is reported in the result
    message.  It is never selected silently.

PREVIEW SIZE - measured, not assumed.  The often-repeated warning that "the CR2
preview is only half resolution" is true for older bodies (30D 1728x1152,
40D 1936x1288, 450D 2256x1504) and FALSE for 550D/600D-era bodies, which embed a
preview at the full raw dimensions (measured on 155 EOS 550D files: every one
carries a 5184x3456 IFD0 JPEG, i.e. the full 18 MP, ~2.0-2.5 MB).  Never assume a
ratio - always read the SOF, which is what _validate_candidate() does.  The
lossless extraction path is therefore the correct default product for such
bodies: a full-size 18 MP JPEG with zero recompression.

DESTINATION AND TEMP-FILE POLICY (one design, not four patches):

  * Names are resolved ONCE, up front, single-threaded, by plan_destinations():
    input order decides everything, so a plan is deterministic and independent
    of thread scheduling.
  * A destination collision between two DIFFERENT sources is never allowed to
    lose a file.  It is de-duplicated deterministically: <stem>_<parent folder>,
    then <stem>_2, _3, ..., then a stable hash of the source's absolute path
    (and that hash suffixed with _2, _3, ... if need be).  The rename is always
    explained in the Russian note attached to the result.  The only sources that
    are allowed to share a destination are literally the same file listed twice
    - which cannot lose data, because both runs write the same bytes.
  * A file that is ALREADY on disk is a different question and is answered by
    ConvertOptions.overwrite: refused with a clear Russian message when False.
    The check is made twice - early (cheap) and again inside the destination
    lock at replace time (race-free).
  * Every write goes to <name>.jpg.<random hex>.tmp in the destination folder
    and is os.replace()d into place.  The token is per-attempt, so two threads
    can never share a temp file; the name is truncated rather than grown, so if
    the OS accepts the destination it accepts the temp file too; and the
    '.jpg.<hex>.tmp' shape is preserved even when truncated so sweep_stale_tmp()
    can still recognise its own orphans.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import secrets
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "CR2_EXTS",
    "plan_destinations",
    "validate_suffix",
    "Preview",
    "Cr2Info",
    "Result",
    "ConvertOptions",
    "probe",
    "find_cr2",
    "convert_one",
    "convert_many",
    "has_pillow",
    "has_rawpy",
]

# No basicConfig here: this module can be imported by a console-less GUI.
log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

CR2_EXTS: tuple[str, ...] = (".cr2",)

# --------------------------------------------------------------------------
# TIFF / CR2 constants
# --------------------------------------------------------------------------

# TIFF 6.0 types 1..12 plus the newer 13..18.  Unknown types must be SKIPPED,
# not treated as fatal (TIFF 6.0 sec.2 explicitly instructs this).
_TYPE_SIZE: dict[int, int] = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
    11: 4, 12: 8, 13: 4, 14: 2, 15: 8, 16: 8, 17: 8, 18: 8,
}

T_IMAGE_WIDTH = 0x0100
T_IMAGE_LENGTH = 0x0101
T_BITS_PER_SAMPLE = 0x0102
T_COMPRESSION = 0x0103
T_PHOTOMETRIC = 0x0106
T_MAKE = 0x010F
T_MODEL = 0x0110
T_STRIP_OFFSETS = 0x0111          # == PreviewImageStart in CR2 IFD0
T_ORIENTATION = 0x0112
T_SAMPLES_PER_PIXEL = 0x0115
T_ROWS_PER_STRIP = 0x0116
T_STRIP_BYTE_COUNTS = 0x0117      # == PreviewImageLength in CR2 IFD0
T_XRES = 0x011A
T_YRES = 0x011B
T_PLANAR_CONFIG = 0x011C
T_RES_UNIT = 0x0128
T_SOFTWARE = 0x0131
T_DATETIME = 0x0132
T_ARTIST = 0x013B
T_THUMB_OFFSET = 0x0201           # JPEGInterchangeFormat
T_THUMB_LENGTH = 0x0202           # JPEGInterchangeFormatLength
T_XMP = 0x02BC
T_COPYRIGHT = 0x8298
T_ICC_PROFILE = 0x8773
T_EXIF_IFD = 0x8769
T_GPS_IFD = 0x8825
T_MAKERNOTE = 0x927C
T_DATETIME_ORIGINAL = 0x9003
T_CREATE_DATE = 0x9004
T_EXIF_IMAGE_W = 0xA002           # PixelXDimension
T_EXIF_IMAGE_H = 0xA003           # PixelYDimension
T_INTEROP_IFD = 0xA005
T_CR2_SLICE = 0xC640

# Canon MakerNote tags we care about.
MN_PREVIEW_INFO = 0x00B6          # PreviewImageInfo (300D-era)
MN_SENSOR_INFO = 0x00E0           # SensorInfo (true raw geometry)
MN_VRD_OFFSET = 0x00D0            # VRDOffset -> a DPP recipe exists

# Tags that describe the CR2 strip layout / raw data.  They are meaningless -
# and actively harmful - inside a JPEG APP1, because a reader would follow
# 0x0111 into the file and render noise.  Always dropped from the rebuilt EXIF.
_DROP_FROM_IFD0: frozenset[int] = frozenset({
    0x00FE,  # NewSubfileType
    T_IMAGE_WIDTH, T_IMAGE_LENGTH,   # described the CR2 preview strip, not us
    T_BITS_PER_SAMPLE, T_COMPRESSION, T_PHOTOMETRIC,
    T_STRIP_OFFSETS, T_SAMPLES_PER_PIXEL, T_ROWS_PER_STRIP,
    T_STRIP_BYTE_COUNTS, T_PLANAR_CONFIG,
    0x0140,  # ColorMap
    0x014A,  # SubIFDs
    T_THUMB_OFFSET, T_THUMB_LENGTH,  # recomputed for our own thumbnail
    T_XMP,                            # moved to its own APP1
    0x83BB,  # IPTC
    0x8649,  # PhotoshopIRB
    T_ICC_PROFILE,                    # moved to APP2
    0x828D, 0x828E,                   # CFA tags
    0xC5D8, 0xC5E0, 0xC5E1, T_CR2_SLICE,   # Canon raw-only
    0xC5D9, 0xC6C5, 0xC6DC,
})

# JPEG markers with no length word.
_STANDALONE_MARKERS: frozenset[int] = frozenset({0x00, 0x01, 0xD8}) | frozenset(range(0xD0, 0xD8))

# VRD / DPP recipe trailer signatures.
_VRD_SIG = b"CANON OPTIONAL DATA\x00"          # 20 bytes
_IHL_SIG = b"IHL Created Optional Item Data\x00\x00"  # 32 bytes
_VRD_BLOCK_EDIT = 0xFFFF00F4      # DPP 1.x-3.x VRD recipe
_VRD_BLOCK_IHL = 0xFFFF00F5       # IHL data (edited thumb/preview live here)
_VRD_BLOCK_XMP = 0xFFFF00F6
_VRD_BLOCK_EDIT4 = 0xFFFF00F7     # DPP 4.x DR4 recipe

_MAX_IFD_ENTRIES = 4096
_MAX_VALUE_BYTES = 1 << 20        # never read a "value" larger than 1 MiB
_EXIF_TIFF_BUDGET = 65527         # 65533 payload cap minus the 6-byte "Exif\0\0"
_MAX_IFD_DEPTH = 6


class Cr2Error(Exception):
    """Raised for structurally invalid / out-of-bounds CR2 data."""


# --------------------------------------------------------------------------
# Bounds-checked file view
# --------------------------------------------------------------------------


class _View:
    """Seek/read wrapper that NEVER reads outside the file.

    Every accessor raises Cr2Error rather than returning short or wrapped data,
    so a corrupt CR2 fails loudly at the parse boundary instead of silently
    producing nonsense offsets.
    """

    __slots__ = ("_f", "size", "endian")

    def __init__(self, f: io.BufferedReader, size: int, endian: str = "<") -> None:
        self._f = f
        self.size = size
        self.endian = endian

    def read_at(self, off: int, n: int) -> bytes:
        """Read exactly n bytes at off, or raise Cr2Error."""
        if off < 0 or n < 0 or off + n > self.size:
            raise Cr2Error(
                "чтение за пределами файла: offset=%d length=%d size=%d" % (off, n, self.size)
            )
        self._f.seek(off)
        data = self._f.read(n)
        if len(data) != n:
            raise Cr2Error("короткое чтение: ожидалось %d, получено %d" % (n, len(data)))
        return data

    def u16(self, off: int, endian: str | None = None) -> int:
        return struct.unpack((endian or self.endian) + "H", self.read_at(off, 2))[0]

    def u32(self, off: int, endian: str | None = None) -> int:
        return struct.unpack((endian or self.endian) + "I", self.read_at(off, 4))[0]


@dataclass(frozen=True)
class _Entry:
    """One 12-byte IFD entry with its value located but not necessarily read."""

    tag: int
    typ: int
    count: int
    raw: bytes          # the 4 value-or-offset bytes, verbatim
    endian: str

    @property
    def nbytes(self) -> int:
        return _TYPE_SIZE.get(self.typ, 0) * self.count

    @property
    def inline(self) -> bool:
        # Inline iff count * sizeof(type) <= 4; decided by the TYPE, never the tag.
        return self.nbytes <= 4

    @property
    def value_offset(self) -> int:
        if self.inline:
            return -1
        return struct.unpack(self.endian + "I", self.raw)[0]

    def data(self, view: _View) -> bytes:
        """Return the entry's full payload bytes."""
        n = self.nbytes
        if n == 0:
            return b""
        if n > _MAX_VALUE_BYTES:
            raise Cr2Error("подозрительно большое значение тега 0x%04X: %d байт" % (self.tag, n))
        if self.inline:
            # Inline values are LEFT-justified in the 4-byte field.
            return self.raw[:n]
        return view.read_at(self.value_offset, n)

    def ints(self, view: _View) -> list[int]:
        """Decode the entry as a list of integers (empty for non-integer types)."""
        code = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 13: "I",
                16: "Q", 17: "q", 18: "Q"}.get(self.typ)
        if code is None:
            return []
        data = self.data(view)
        n = struct.calcsize(code)
        cnt = len(data) // n
        if cnt == 0:
            return []
        return list(struct.unpack(self.endian + code * cnt, data[: cnt * n]))

    def text(self, view: _View) -> str:
        """Decode an ASCII entry, dropping the trailing NUL(s)."""
        if self.typ not in (2, 129):
            return ""
        data = self.data(view)
        return data.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def _read_ifd(view: _View, off: int, endian: str | None = None) -> tuple[list[_Entry], int]:
    """Read one IFD: entry count, entries, next-IFD offset.

    Returns (entries, next_ifd_offset).  Hard-bounds-checked against file size.
    """
    e = endian or view.endian
    if off < 8 or off + 6 > view.size:
        raise Cr2Error("некорректное смещение IFD: %d" % off)
    n = view.u16(off, e)
    if n == 0 or n > _MAX_IFD_ENTRIES:
        raise Cr2Error("некорректное число записей IFD: %d" % n)
    end = off + 2 + 12 * n + 4
    if end > view.size:
        raise Cr2Error("IFD выходит за пределы файла (нужно %d, размер %d)" % (end, view.size))
    blob = view.read_at(off + 2, 12 * n)
    entries: list[_Entry] = []
    for i in range(n):
        p = 12 * i
        tag, typ, count = struct.unpack(e + "HHI", blob[p:p + 8])
        if typ not in _TYPE_SIZE:
            # TIFF 6.0: skip fields with an unexpected type, do not abort.
            log.debug("пропуск тега 0x%04X с неизвестным типом %d", tag, typ)
            continue
        if count > _MAX_VALUE_BYTES:
            log.debug("пропуск тега 0x%04X с абсурдным count=%d", tag, count)
            continue
        entries.append(_Entry(tag, typ, count, blob[p + 8:p + 12], e))
    nxt = view.u32(off + 2 + 12 * n, e)
    return entries, nxt


def _find(entries: Sequence[_Entry], tag: int) -> _Entry | None:
    for ent in entries:
        if ent.tag == tag:
            return ent
    return None


def _int1(view: _View, entries: Sequence[_Entry], tag: int, default: int = 0) -> int:
    ent = _find(entries, tag)
    if ent is None:
        return default
    try:
        vals = ent.ints(view)
    except Cr2Error:
        return default
    return vals[0] if vals else default


def _str1(view: _View, entries: Sequence[_Entry], tag: int) -> str:
    ent = _find(entries, tag)
    if ent is None:
        return ""
    try:
        return ent.text(view)
    except Cr2Error:
        return ""


# --------------------------------------------------------------------------
# JPEG marker scanning (ALWAYS big-endian, regardless of the TIFF byte order)
# --------------------------------------------------------------------------


def _jpeg_sof(buf: bytes, start: int = 0, length: int | None = None) -> dict[str, Any] | None:
    """Parse the first SOF of a JPEG stream.

    Returns {'w','h','precision','ncomp','process','comps'} or None.
    SOF test per ExifTool: (M & 0xF0) == 0xC0 and (M == 0xC0 or M & 0x03),
    which accepts C0..CF except DHT(C4), JPGA(C8) and DAC(CC).
    """
    end = len(buf) if length is None else min(len(buf), start + length)
    p = start
    if buf[p:p + 2] != b"\xff\xd8":
        return None
    p += 2
    while p + 2 <= end:
        if buf[p] != 0xFF:
            p += 1
            continue
        while p < end and buf[p] == 0xFF:   # eat runs of 0xFF fill bytes
            p += 1
        if p >= end:
            return None
        m = buf[p]
        p += 1
        if m in _STANDALONE_MARKERS:
            continue
        if m == 0xD9 or m == 0xDA:
            # EOI / SOS: entropy data follows, stop before we hit false markers.
            return None
        if p + 2 > end:
            return None
        seglen = struct.unpack(">H", buf[p:p + 2])[0]
        if seglen < 2 or p + seglen > end:
            return None
        if (m & 0xF0) == 0xC0 and (m == 0xC0 or (m & 0x03)):
            if seglen < 8:
                return None
            prec = buf[p + 2]
            h, w = struct.unpack(">HH", buf[p + 3:p + 7])
            ncomp = buf[p + 7]
            comps: list[tuple[int, int, int]] = []
            if seglen >= 8 + 3 * ncomp:
                for i in range(ncomp):
                    cid = buf[p + 8 + 3 * i]
                    samp = buf[p + 9 + 3 * i]
                    comps.append((cid, samp >> 4, samp & 0x0F))
            return {"w": w, "h": h, "precision": prec, "ncomp": ncomp,
                    "process": m - 0xC0, "comps": comps}
        p += seglen
    return None


#: Outcome of a marker-chain walk, see _jpeg_segments_checked().
SEG_COMPLETE = "complete"   # the walk reached SOS or EOI: the chain is sound
SEG_PARTIAL = "partial"     # the buffer ran out before SOS, but the stream is longer
SEG_BROKEN = "broken"       # desynchronised or a segment length that cannot fit


def _jpeg_segments_checked(blob: bytes,
                           total_len: int | None = None) -> tuple[list[tuple[int, int, int]], str]:
    """Walk a JPEG and return ((marker, seg_start, seg_total_len)..., status).

    Stops at SOS: everything from the SOS header to EOI is entropy-coded data
    and must be copied verbatim (it contains stuffed 0xFF00 and RSTn bytes).
    The SOS entry's length covers the rest of the buffer.

    The status is the whole point of this function: a bare `break` on a bad
    segment length silently returns a PREFIX of the stream, and a caller that
    splices that prefix emits a JPEG with no scan data at all.  Callers must
    therefore distinguish three cases:

      * SEG_COMPLETE - the walk ended at SOS (0xDA) or EOI (0xD9);
      * SEG_PARTIAL  - `blob` is only a prefix of a longer stream (pass
        `total_len` to say so) and the walk simply ran out of bytes;
      * SEG_BROKEN   - the marker chain is desynchronised, or a segment
        declares a length that does not fit the stream.  The returned list is
        a prefix and must NOT be treated as the whole image.

    Args:
        blob: the bytes available (may be a prefix of the stream).
        total_len: true length of the stream when `blob` is only a prefix.

    Returns:
        (segments, status)
    """
    out: list[tuple[int, int, int]] = []
    n = len(blob)
    full = n if total_len is None else max(n, total_len)
    truncated_buffer = n < full

    def ran_out() -> str:
        return SEG_PARTIAL if truncated_buffer else SEG_BROKEN

    i = 2
    while i + 1 < n:
        if blob[i] != 0xFF:
            return out, SEG_BROKEN          # marker chain desynchronised
        j = i
        while j < n and blob[j] == 0xFF:
            j += 1
        if j >= n:
            return out, ran_out()
        m = blob[j]
        start = j - 1  # keep exactly one 0xFF as the marker prefix
        if m == 0xD9:
            out.append((m, start, 2))
            return out, SEG_COMPLETE
        if m in _STANDALONE_MARKERS:
            out.append((m, start, 2))
            i = j + 1
            continue
        if j + 3 > n:
            return out, ran_out()
        seglen = struct.unpack(">H", blob[j + 1:j + 3])[0]
        if seglen < 2 or j + 1 + seglen > full:
            return out, SEG_BROKEN          # length cannot fit the stream at all
        if j + 1 + seglen > n:
            return out, ran_out()           # fits the stream, not this buffer
        if m == 0xDA:
            out.append((m, start, n - start))
            return out, SEG_COMPLETE
        out.append((m, start, 2 + seglen))
        i = j + 1 + seglen
    # Fell off the end without ever reaching SOS or EOI.
    return out, ran_out()


def _jpeg_segments(blob: bytes) -> list[tuple[int, int, int]]:
    """Segments only; see _jpeg_segments_checked() for the completeness status."""
    return _jpeg_segments_checked(blob)[0]


#: How far back from the end of a declared preview region we look for the EOI.
#: Writers pad after EOI for sector/word alignment (16, 256, 512, 1024 bytes are
#: all seen in the wild), so a 32-byte window silently failed to trim and the
#: "byte-exact" output did not end at FFD9.  The search always stays INSIDE the
#: declared region, so the VRD trailer's decoy FFD9 is still out of reach.
_EOI_WINDOW = 4096


def _find_eoi_tail(blob: bytes, search: int = _EOI_WINDOW) -> int:
    """Return the index just past a trailing FFD9, or -1.

    Some writers pad after EOI; we tolerate that padding and report the real
    end so the caller can trim.  We never scan the WHOLE file backwards for
    FFD9 - a VRD trailer's footer also ends in FFD9 and that is exactly the
    trap the research warns about.
    """
    tail_start = max(0, len(blob) - search)
    idx = blob.rfind(b"\xff\xd9", tail_start)
    return -1 if idx < 0 else idx + 2


# --------------------------------------------------------------------------
# Public dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preview:
    """One embedded JPEG candidate found inside the CR2."""

    source: str        # 'ifd0' | 'ifd1' | 'ifd2' | 'makernote' | 'vrd_ihl' | ...
    offset: int
    length: int
    width: int = 0     # 0 if unknown
    height: int = 0    # 0 if unknown

    @property
    def pixels(self) -> int:
        """Pixel count of the preview (0 when the dimensions are unknown)."""
        return self.width * self.height


#: Preview sources described by the TIFF/EXIF/MakerNote specs.  Everything else
#: (currently only DPP's own IHL images) is opt-in, never the default.
_DOC_PREVIEW_SOURCES: frozenset[str] = frozenset({"ifd0", "ifd1", "ifd2", "makernote"})


@dataclass
class Cr2Info:
    """Everything probe() could learn about one CR2 file."""

    path: Path
    byte_order: str = "<"
    previews: list[Preview] = field(default_factory=list)
    raw_width: int = 0
    raw_height: int = 0
    orientation: int = 1
    camera: str = ""
    shot_at: str = ""
    has_dpp_recipe: bool = False
    recipe_hint: str = ""
    software: str = ""
    error: str = ""
    raw_subsampled: bool = False     # mRAW/sRAW: raw_* is not the full sensor

    @property
    def best(self) -> Preview | None:
        """Largest preview from a DOCUMENTED IFD source, or None.

        previews is sorted purely by pixel count, so a DPP-written IHL preview
        that happens to be larger than the IFD0 preview used to win silently -
        contradicting _scan_ihl's own "never silently prefer them" contract and
        exporting a differently-cropped image while the status line still said
        "правки DPP не применены".  IHL candidates stay reachable through
        best_dpp / previews, and are used here only when nothing else is usable
        (better a small DPP preview than no image at all).

        That last case is a FALLBACK, not a preference, and convert_one() says
        so in the result message - see the "пригодного превью камеры в файле
        нет" warning.  There is no code path in which an IHL image is exported
        without the user either asking for it (prefer_dpp_preview) or being
        told about it.
        """
        for p in self.previews:                    # already sorted largest-first
            if p.source in _DOC_PREVIEW_SOURCES:
                return p
        return self.previews[0] if self.previews else None

    @property
    def best_dpp(self) -> Preview | None:
        """The largest preview DPP itself wrote into the VRD IHL block, or None."""
        for p in self.previews:
            if p.source.startswith("vrd_ihl"):
                return p
        return None


@dataclass
class Result:
    """Outcome of converting one file."""

    src: Path
    dst: Path | None = None
    ok: bool = False
    skipped: bool = False
    mode: str = ""         # 'lossless' | 'reencode' | 'raw' | ''
    width: int = 0
    height: int = 0
    bytes_out: int = 0
    message: str = ""      # RUSSIAN, one line, user-facing
    info: Cr2Info | None = None
    #: Which embedded blob was actually exported ('ifd0', 'ifd1', 'vrd_ihl', ...),
    #: or '' when nothing was (raw fallback, error).  Callers must read it from
    #: HERE and never re-derive it from info.best: under prefer_dpp_preview the
    #: exported image is deliberately NOT info.best.  Appended last on purpose,
    #: so no positional construction of Result can shift.
    source: str = ""


@dataclass
class ConvertOptions:
    """Knobs for convert_one / convert_many.

    None of these makes the tool apply a DPP recipe - nothing here can.  They
    only choose HOW the camera's own embedded JPEG is delivered.
    """

    out_dir: Path | None = None      # None -> next to the source file
    quality: int = 95
    max_side: int = 0                # 0 = no downscale
    lossless: bool = True            # extract the embedded JPEG unchanged (no recompression)
    bake_rotation: bool = False      # apply EXIF orientation to pixels (needs Pillow)
    copy_exif: bool = True
    keep_makernote: bool = False
    strip_gps: bool = False
    overwrite: bool = False
    suffix: str = ""                 # appended to the stem
    min_preview_ratio: float = 0.4   # preview px / raw px below this -> not "full size"
    allow_raw_fallback: bool = True
    # Opt in to the undocumented IHL image DPP wrote into the VRD trailer.  It
    # MAY reflect the recipe; nothing guarantees it does, and it is usually
    # smaller and differently cropped.  Off by default, and never auto-selected.
    prefer_dpp_preview: bool = False


#: Characters that must never reach a file name (Windows reserves all of them).
_BAD_SUFFIX_CHARS = '\\/:*?"<>|'


def validate_suffix(suffix: str) -> str | None:
    """Return a Russian error message for an unusable suffix, else None.

    The suffix is concatenated straight into the destination NAME, and pathlib
    then re-parses any separator inside it - so an unvalidated suffix can write
    outside the chosen output folder or silently create directory trees.  The
    CLI used to own this check; it lives here so the GUI and every programmatic
    caller are covered by the same rule.
    """
    s = suffix or ""
    if any(ch in s for ch in _BAD_SUFFIX_CHARS):
        return ("суффикс содержит недопустимые для имени файла символы: "
                "\\ / : * ? \" < > |")
    if any(ord(ch) < 32 for ch in s):
        return "суффикс содержит управляющие символы"
    if s and s != s.strip(" .") and s.strip(" ."):
        return "суффикс не может начинаться или заканчиваться точкой или пробелом"
    return None


# --------------------------------------------------------------------------
# Optional-dependency probes
# --------------------------------------------------------------------------


def has_pillow() -> bool:
    """True if Pillow can be imported (checked lazily, never at import time)."""
    try:
        import PIL  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def has_rawpy() -> bool:
    """True if rawpy can be imported (checked lazily, never at import time)."""
    try:
        import rawpy  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# DPP recipe (VRD / DR4) trailer detection
# --------------------------------------------------------------------------


@dataclass
class _VrdInfo:
    present: bool = False
    start: int = 0
    total: int = 0
    dpp4: bool = False
    dpp3: bool = False
    has_ihl: bool = False
    ihl_preview: tuple[int, int] | None = None   # (offset, length) of an IHL JPEG
    ihl_thumb: tuple[int, int] | None = None


def _scan_vrd(view: _View) -> _VrdInfo:
    """Locate and shallow-parse a "CANON OPTIONAL DATA" trailer at EOF.

    ExifTool identifies the trailer by matching the LAST 0x40 bytes of the file
    against the signature; the payload size is a BIG-endian int32 in both the
    0x1c header and the 0x40 footer, and total = size + 0x5c.
    """
    out = _VrdInfo()
    if view.size < 0x5C:
        return out
    try:
        footer = view.read_at(view.size - 0x40, 0x40)
    except Cr2Error:
        return out
    if footer[:20] != _VRD_SIG:
        return out
    size = struct.unpack(">I", footer[0x14:0x18])[0]
    total = size + 0x5C
    if total <= 0 or total > view.size:
        return out
    start = view.size - total
    try:
        header = view.read_at(start, 0x1C)
    except Cr2Error:
        return out
    if header[:20] != _VRD_SIG:
        return out
    if struct.unpack(">I", header[0x18:0x1C])[0] != size:
        return out

    out.present = True
    out.start = start
    out.total = total

    # Walk the block list.  Block = BE int32 type + BE int32 length + payload.
    pos = 0x1C
    limit = total - 0x40
    guard = 0
    while pos + 8 <= limit and guard < 256:
        guard += 1
        try:
            btype, blen = struct.unpack(">II", view.read_at(start + pos, 8))
        except Cr2Error:
            break
        data_off = pos + 8
        if blen < 0 or data_off + blen > limit:
            break
        if btype == _VRD_BLOCK_EDIT4:
            out.dpp4 = True
        elif btype == _VRD_BLOCK_EDIT:
            out.dpp3 = True
        elif btype == _VRD_BLOCK_IHL:
            out.has_ihl = True
            _scan_ihl(view, start + data_off, blen, out)
        pos = data_off + blen
    return out


def _scan_ihl(view: _View, block_off: int, block_len: int, out: _VrdInfo) -> None:
    """Walk IHL records inside a 0xffff00f5 block (all little-endian).

    Record header is 48 bytes starting with the IHL signature; tag at +36,
    data size at +40, next-record size at +44, payload at +48.
    IHL tag 3 = ThumbnailImage, tag 4 = PreviewImage - these are DPP's own
    edit-reflecting images, but nothing documents that they track the CURRENT
    recipe, so we surface them as candidates and never silently prefer them
    (Cr2Info.best enforces that; ConvertOptions.prefer_dpp_preview opts in).

    The stride is `48 + size`, i.e. the record's OWN payload length from +40 -
    the same field the bounds check and the extracted JPEG already use.  The
    +44 field describes the NEXT record, so stepping by it desynchronises the
    walk the moment two records differ in size: the signature check then fails
    and every remaining record is dropped.  It is kept only as a "more records
    follow" hint.
    """
    pos = 0
    guard = 0
    while pos + 48 <= block_len and guard < 64:
        guard += 1
        try:
            rec = view.read_at(block_off + pos, 48)
        except Cr2Error:
            return
        if rec[:32] != _IHL_SIG:
            return
        tag, size, nxt = struct.unpack("<III", rec[36:48])
        data_off = block_off + pos + 48
        if size <= 0 or pos + 48 + size > block_len:
            return
        if tag == 4:
            out.ihl_preview = (data_off, size)
        elif tag == 3:
            out.ihl_thumb = (data_off, size)
        pos += 48 + size
        if nxt == 0:
            return          # +44 == 0 marks the last record


# --------------------------------------------------------------------------
# MakerNote (Canon) - shallow parse only
# --------------------------------------------------------------------------


def _parse_makernote(view: _View, ent: _Entry) -> tuple[list[_Entry], str] | None:
    """Parse the Canon MakerNote as a bare IFD at its own absolute offset.

    The Canon MakerNote starts directly with a 12-byte-entry IFD (no signature
    header) and its internal offsets are relative to the TIFF header, which for
    a CR2 is file offset 0.  ExifTool sets ByteOrder => 'Unknown' for Canon, so
    if the entry count looks absurd in the file's byte order we retry with the
    other endianness.
    """
    if ent.inline:
        return None
    off = ent.value_offset
    for endian in (view.endian, ">" if view.endian == "<" else "<"):
        try:
            n = view.u16(off, endian)
        except Cr2Error:
            return None
        if 0 < n <= 512:
            try:
                entries, _ = _read_ifd(view, off, endian)
            except Cr2Error:
                continue
            return entries, endian
    return None


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------


def _validate_candidate(view: _View, source: str, off: int, length: int) -> Preview | None:
    """Check one embedded blob is a real JPEG and read its true SOF dimensions.

    Requires SOI (FFD8) and a parsable SOF.  A trailing EOI is looked for only
    in the last few bytes of the declared region (never by scanning the whole
    file backwards, which would land inside the VRD trailer's fake FFD9).
    """
    if length <= 4 or off <= 0 or off + length > view.size:
        return None
    head_len = min(length, 65536)
    win = min(length, _EOI_WINDOW)
    try:
        head = view.read_at(off, head_len)
        tail = view.read_at(off + length - win, win)
    except Cr2Error:
        return None
    if head[:2] != b"\xff\xd8":
        return None
    sof = _jpeg_sof(head)
    if sof is None or sof["w"] <= 0 or sof["h"] <= 0:
        return None
    # A SOF alone is not enough: _jpeg_sof stops at the first frame header, so
    # it happily validates a stream whose marker chain falls apart afterwards.
    # Splicing such a blob produces a header-only JPEG no viewer can open, so
    # reject the candidate here and let probe() fall through to ifd1/ifd2/raw.
    _segs, status = _jpeg_segments_checked(head, length)
    if status == SEG_BROKEN:
        log.debug("%s: цепочка маркеров JPEG повреждена (offset=%d len=%d)",
                  source, off, length)
        return None
    # Only a viewable frame counts as a preview: SOF0 baseline, SOF1 extended
    # sequential, SOF2 progressive.  SOF3 (lossless Huffman) is how the CR2
    # stores the raw CFA data in IFD3 - it parses as a "SOF" but is not an
    # image any viewer can show, so it must never be picked as a preview.
    if sof["process"] not in (0, 1, 2):
        log.debug("%s: кадр SOF%d не является просматриваемым JPEG", source, sof["process"])
        return None
    # Trim any padding that follows the EOI so the copied bytes end exactly at FFD9.
    real_len = length
    eoi = _find_eoi_tail(tail)
    if eoi >= 0:
        # `win` drives BOTH the tail read and this arithmetic - they must never
        # drift apart, or real_len is computed against the wrong base offset.
        real_len = length - win + eoi
    else:
        # No EOI near the end: keep the declared length but note it stays usable
        # only if the entropy data is intact; the SOF already parsed, so ship it.
        log.debug("%s: EOI не найден в хвосте блока (offset=%d len=%d)", source, off, length)
    if real_len <= 4 or off + real_len > view.size:
        return None
    return Preview(source=source, offset=off, length=real_len,
                   width=sof["w"], height=sof["h"])


def _raw_dims_from_ifd3(view: _View, entries: Sequence[_Entry]) -> tuple[int, int, bool]:
    """Derive sensor dimensions from IFD3's lossless-JPEG SOF3.

    IFD3 carries NO ImageWidth/ImageLength, so the only source is the SOF3
    header, and for an ordinary full RAW the SOF width must be multiplied by the
    component count:
        sensor_width  = sof_width  * ncomp
        sensor_height = sof_height * vertical_sampling_factor
    Cross-checked against 0xC640 (nSlices-1, width_each, width_last).

    mRAW/sRAW frames are SUBSAMPLED and break both rules - dcraw guards the
    multiplication with `if (!(jh.sraw || (jh.clrs & 1))) width *= clrs;`, and
    the 0xC640 slice widths are then in encoded units, so the cross-check is
    wrong too.  We detect subsampling exactly as dcraw does, from the first
    component's sampling factors, and return the SOF dimensions untouched plus a
    flag saying the numbers describe a reduced recording, not the full sensor.

    Returns:
        (width, height, subsampled)
    """
    so = _find(entries, T_STRIP_OFFSETS)
    sc = _find(entries, T_STRIP_BYTE_COUNTS)
    if so is None or sc is None:
        return 0, 0, False
    try:
        off = so.ints(view)[0]
        ln = sc.ints(view)[0]
    except (Cr2Error, IndexError):
        return 0, 0, False
    if off <= 0 or ln <= 0 or off + ln > view.size:
        return 0, 0, False
    try:
        head = view.read_at(off, min(ln, 4096))
    except Cr2Error:
        return 0, 0, False
    sof = _jpeg_sof(head)
    if sof is None:
        return 0, 0, False
    ncomp = max(1, sof["ncomp"])
    vsf = max((c[2] for c in sof["comps"]), default=1) or 1
    # dcraw: sraw = ((h_samp * v_samp) - 1) & 3 on the FIRST component.
    sraw = False
    if sof["comps"]:
        h_samp, v_samp = sof["comps"][0][1] or 1, sof["comps"][0][2] or 1
        sraw = bool(((h_samp * v_samp) - 1) & 3)
    if sraw or ncomp == 3:
        # Subsampled recording: neither the *ncomp/*vsf arithmetic nor the
        # 0xC640 slice widths apply.  Report what the frame really holds.
        return sof["w"], sof["h"], True
    w = sof["w"] * ncomp
    h = sof["h"] * vsf

    slice_ent = _find(entries, T_CR2_SLICE)
    if slice_ent is not None:
        try:
            v = slice_ent.ints(view)
        except Cr2Error:
            v = []
        if len(v) >= 3:
            # v[0] is the slice count MINUS ONE.
            expect = v[0] * v[1] + v[2]
            if expect > 0:
                if expect != w:
                    log.debug("0xC640 даёт ширину %d, SOF3 даёт %d", expect, w)
                w = expect
    return w, h, False


def _sensor_visible(view: _View, mn: Sequence[_Entry]) -> tuple[int, int]:
    """Visible raw dimensions from Canon MakerNote 0x00E0 SensorInfo.

    int16 array, 1-based word index: [1]=SensorWidth [2]=SensorHeight
    [5]=Left [6]=Top [7]=Right [8]=Bottom.  visible = right-left+1 x bottom-top+1.
    """
    ent = _find(mn, MN_SENSOR_INFO)
    if ent is None:
        return 0, 0
    try:
        v = ent.ints(view)
    except Cr2Error:
        return 0, 0
    if len(v) < 9:
        return 0, 0
    left, top, right, bottom = v[5], v[6], v[7], v[8]
    w = right - left + 1
    h = bottom - top + 1
    if 0 < w <= 40000 and 0 < h <= 40000:
        return w, h
    return 0, 0


def probe(path: str | Path) -> Cr2Info:
    """Inspect a CR2 file and return everything we can learn about it.

    Never raises for a corrupt or non-CR2 file: the problem is reported in
    Cr2Info.error (Russian).  Reads only what it needs - the file is never
    slurped into memory.

    Args:
        path: path to a .CR2 file.

    Returns:
        Cr2Info with previews sorted largest-first.
    """
    p = Path(path)
    info = Cr2Info(path=p)
    try:
        size = p.stat().st_size
    except OSError as exc:
        info.error = "Не удалось открыть файл: %s" % exc
        return info
    if size < 32:
        info.error = "Файл слишком мал для CR2 (%d байт)" % size
        return info

    try:
        with open(p, "rb") as f:
            view = _View(f, size)
            hdr = view.read_at(0, 16)

            bo = hdr[0:2]
            if bo == b"II":
                view.endian = "<"
            elif bo == b"MM":
                view.endian = ">"
            else:
                info.error = "Это не TIFF/CR2: неизвестный порядок байт %r" % bo
                return info
            info.byte_order = view.endian

            magic, ifd0_off = struct.unpack(view.endian + "HI", hdr[2:8])
            if magic != 42:
                info.error = "Это не TIFF/CR2: неверная сигнатура (%d вместо 42)" % magic
                return info

            # EOS-1D / 1Ds write a .TIF raw with this signature at byte 8.
            # It looks like a Canon TIFF but is NOT a CR2 - reject it explicitly.
            if hdr[8:12] == b"\xba\xb0\xac\xbb":
                info.error = "Это Canon 1D RAW (.TIF), а не CR2 — формат не поддерживается"
                return info

            # ExifTool tolerates ifd0_off > 16 (PhotoMechanic rewrites CR2 that
            # way) and only looks for the 'CR' signature when offset >= 16.
            if ifd0_off < 8 or ifd0_off + 6 > size:
                info.error = "Некорректное смещение IFD0: %d" % ifd0_off
                return info
            if ifd0_off >= 16 and hdr[8:10] != b"CR":
                log.debug("нет магии 'CR' в байтах 8-9: %r (обрабатываем как обычный TIFF)", hdr[8:10])
            raw_ifd_off = struct.unpack(view.endian + "I", hdr[12:16])[0]

            # ---- walk the main IFD chain, guarding against loops -------------
            ifds: list[list[_Entry]] = []
            visited: set[int] = set()
            nxt = ifd0_off
            while nxt and nxt not in visited and len(ifds) < 16:
                visited.add(nxt)
                try:
                    entries, nxt = _read_ifd(view, nxt)
                except Cr2Error as exc:
                    log.debug("обрыв цепочки IFD: %s", exc)
                    break
                ifds.append(entries)
            if not ifds:
                info.error = "Не удалось прочитать IFD0"
                return info

            ifd0 = ifds[0]
            info.orientation = _int1(view, ifd0, T_ORIENTATION, 1) or 1
            if not 1 <= info.orientation <= 8:
                info.orientation = 1
            info.software = _str1(view, ifd0, T_SOFTWARE)
            make = _str1(view, ifd0, T_MAKE)
            model = _str1(view, ifd0, T_MODEL)
            if model.lower().startswith(make.lower()) and make:
                info.camera = model
            else:
                info.camera = (make + " " + model).strip()

            # ---- sub-IFDs ----------------------------------------------------
            exif_entries: list[_Entry] = []
            mn_entries: list[_Entry] = []
            ent = _find(ifd0, T_EXIF_IFD)
            if ent is not None:
                try:
                    exif_off = ent.ints(view)[0]
                    exif_entries, _ = _read_ifd(view, exif_off)
                except (Cr2Error, IndexError) as exc:
                    log.debug("ExifIFD недоступен: %s", exc)
            if exif_entries:
                info.shot_at = (_str1(view, exif_entries, T_DATETIME_ORIGINAL)
                                or _str1(view, exif_entries, T_CREATE_DATE))
                mn_ent = _find(exif_entries, T_MAKERNOTE)
                if mn_ent is not None:
                    try:
                        parsed = _parse_makernote(view, mn_ent)
                    except Cr2Error:
                        parsed = None
                    if parsed:
                        mn_entries = parsed[0]
            if not info.shot_at:
                info.shot_at = _str1(view, ifd0, T_DATETIME)

            # ---- raw dimensions ---------------------------------------------
            # Prefer MakerNote SensorInfo's VISIBLE area: that is what the
            # camera actually renders, and it is the honest denominator when
            # comparing the preview against "full size".
            vis_w, vis_h = _sensor_visible(view, mn_entries)
            raw_ifd: list[_Entry] = []
            if len(ifds) >= 4:
                raw_ifd = ifds[3]
            elif raw_ifd_off and raw_ifd_off + 6 <= size:
                try:
                    raw_ifd, _ = _read_ifd(view, raw_ifd_off)
                except Cr2Error:
                    raw_ifd = []
            sof_w, sof_h, sraw = (_raw_dims_from_ifd3(view, raw_ifd)
                                  if raw_ifd else (0, 0, False))
            info.raw_subsampled = sraw
            if sraw and sof_w and sof_h:
                # mRAW/sRAW: SensorInfo still describes the FULL sensor, so it
                # is the wrong denominator for "is the preview full size?".
                info.raw_width, info.raw_height = sof_w, sof_h
            elif vis_w and vis_h:
                info.raw_width, info.raw_height = vis_w, vis_h
            else:
                info.raw_width, info.raw_height = sof_w, sof_h

            # ---- DPP recipe trailer -----------------------------------------
            vrd = _scan_vrd(view)
            mn_vrd_offset = _int1(view, mn_entries, MN_VRD_OFFSET, 0)
            info.has_dpp_recipe = bool(vrd.present or mn_vrd_offset)
            info.recipe_hint = _recipe_hint(vrd, mn_vrd_offset, info.software)

            # ---- collect every embedded JPEG candidate -----------------------
            cands: list[Preview] = []

            # IFD0: StripOffsets/StripByteCounts == PreviewImageStart/Length.
            cands.extend(_candidates_from_strip(view, ifd0, "ifd0"))
            # IFD1: thumbnail via 0x0201/0x0202 only (2 entries, no dim tags).
            if len(ifds) >= 2:
                cands.extend(_candidates_from_thumb(view, ifds[1], "ifd1"))
                cands.extend(_candidates_from_strip(view, ifds[1], "ifd1"))
            # IFD2: usually an UNCOMPRESSED RGB image (Compression=1) and thus
            # not a JPEG at all - but a few bodies (5D, G9) set Compression=6,
            # so we still validate the blob and keep it only if it really is one.
            if len(ifds) >= 3:
                cands.extend(_candidates_from_strip(view, ifds[2], "ifd2"))
            # Canon MakerNote 0x00B6 PreviewImageInfo (300D-era; rare on CR2).
            cands.extend(_candidates_from_makernote(view, mn_entries))
            # DPP's own preview inside the VRD IHL block, if any.
            if vrd.ihl_preview:
                c = _validate_candidate(view, "vrd_ihl", *vrd.ihl_preview)
                if c:
                    cands.append(c)
            if vrd.ihl_thumb:
                c = _validate_candidate(view, "vrd_ihl_thumb", *vrd.ihl_thumb)
                if c:
                    cands.append(c)

            # De-duplicate by (offset, length), then sort largest-first.
            seen: set[tuple[int, int]] = set()
            uniq: list[Preview] = []
            for c in cands:
                key = (c.offset, c.length)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(c)
            uniq.sort(key=lambda c: (c.pixels, c.length), reverse=True)
            info.previews = uniq

            if not info.previews:
                info.error = "В файле не найдено ни одного пригодного встроенного JPEG"
    except Cr2Error as exc:
        info.error = "Повреждённый CR2: %s" % exc
    except OSError as exc:
        info.error = "Ошибка чтения файла: %s" % exc
    except Exception as exc:  # last-resort: probe() must never raise
        log.exception("probe(%s) упал неожиданно", p)
        info.error = "Непредвиденная ошибка разбора: %s: %s" % (type(exc).__name__, exc)
    return info


def _candidates_from_strip(view: _View, entries: Sequence[_Entry], source: str) -> list[Preview]:
    so = _find(entries, T_STRIP_OFFSETS)
    sc = _find(entries, T_STRIP_BYTE_COUNTS)
    if so is None or sc is None:
        return []
    try:
        offs = so.ints(view)
        lens = sc.ints(view)
    except Cr2Error:
        return []
    out: list[Preview] = []
    for off, ln in zip(offs, lens):
        c = _validate_candidate(view, source, off, ln)
        if c:
            out.append(c)
    return out


def _candidates_from_thumb(view: _View, entries: Sequence[_Entry], source: str) -> list[Preview]:
    to = _find(entries, T_THUMB_OFFSET)
    tl = _find(entries, T_THUMB_LENGTH)
    if to is None or tl is None:
        return []
    try:
        off = to.ints(view)[0]
        ln = tl.ints(view)[0]
    except (Cr2Error, IndexError):
        return []
    c = _validate_candidate(view, source, off, ln)
    return [c] if c else []


def _candidates_from_makernote(view: _View, mn: Sequence[_Entry]) -> list[Preview]:
    """Canon MakerNote 0x00B6 PreviewImageInfo.

    The IFD declares Count=12 int32u (48 bytes) but only 6 int32u are real -
    ExifTool notes the size is "wrong by a factor of 2".  The first word gives
    the true block size in BYTES, so we trust that, not the IFD count.
    Layout from the block start: +0 size, +4 quality, +8 length, +12 width,
    +16 height, +20 start (file offset).
    """
    ent = _find(mn, MN_PREVIEW_INFO)
    if ent is None or ent.inline:
        return []
    off = ent.value_offset
    try:
        blk = view.read_at(off, 24)
    except Cr2Error:
        return []
    e = ent.endian
    try:
        _size, _quality, plen, _pw, _ph, pstart = struct.unpack(e + "6I", blk)
    except struct.error:
        return []
    c = _validate_candidate(view, "makernote", pstart, plen)
    return [c] if c else []


def _recipe_hint(vrd: _VrdInfo, mn_vrd_offset: int, software: str) -> str:
    """Build the Russian, honest explanation of what we found (or didn't).

    Two rules govern the wording, and both were violated by earlier revisions:

    * "No recipe found" is a statement about THIS FILE'S TRAILER and nothing
      else.  It is not an all-clear about edits: it does not prove the file was
      never opened in DPP, and it does not prove nobody edited it.  So the text
      says what was checked and stops there.
    * "Recipe found" must never imply the tool will honour it.  It cannot -
      rendering a recipe requires Canon's own engine - so the message names the
      only two things that can: DPP's "Конвертировать и сохранить"
      (Convert and save) or its "Пакетная обработка" (Batch process).
    """
    _CHECKED = ("Проверено только одно: трейлер «CANON OPTIONAL DATA» в конце "
                "файла и тег MakerNote 0x00D0 (VRDOffset). Отдельные файлы "
                "рецептов (.vrd/.dr4) рядом с CR2 не проверяются.")
    _NO_CLAIM = ("Это НЕ доказывает, что файл не открывали в DPP, и не означает, "
                 "что правок не было: рецепт мог быть сохранён отдельным файлом "
                 "(.vrd/.dr4), правки могли быть не сохранены, либо трейлер "
                 "записан нестандартно.")
    if not vrd.present and not mn_vrd_offset:
        if software and "photo professional" in software.lower():
            return ("Рецепт DPP в файле не найден, но тег Software содержит «%s». "
                    "%s %s" % (software, _CHECKED, _NO_CLAIM))
        return "Рецепт DPP в файле не найден. %s %s" % (_CHECKED, _NO_CLAIM)

    if vrd.dpp4:
        who = "DPP 4.x (блок Edit4Data/DR4)"
    elif vrd.dpp3:
        who = "DPP 1.x–3.x (блок EditData/VRD)"
    elif vrd.present:
        who = "DPP (тип блока не распознан)"
    else:
        who = "DPP (по тегу MakerNote 0x00D0 VRDOffset)"

    parts = [
        "Найден рецепт %s." % who,
        "ВАЖНО: эта программа НЕ умеет применять рецепт DPP — рецепт может "
        "выполнить только сам движок Canon. DPP при сохранении не обновляет "
        "встроенный JPEG в CR2, он лишь дописывает рецепт в конец файла, "
        "поэтому извлечённый JPEG показывает съёмочные настройки камеры, "
        "а правки DPP (кроп, поворот, стиль, ББ, экспозиция) в него НЕ входят.",
        "Наличие трейлера означает «файл побывал в DPP», а не «в нём есть "
        "значимые правки»: DPP пишет трейлер даже при сбросе всех настроек.",
    ]
    if vrd.has_ihl:
        parts.append(
            "В трейлере есть блок IHLData с собственным превью DPP — оно, "
            "возможно, отражает правки, но это нигде не документировано и "
            "не гарантировано, поэтому автоматически оно не выбирается: "
            "включите его явно (--prefer-dpp-preview / галочка «Брать превью DPP»)."
        )
    parts.append(
        "Получить именно вид из DPP можно ТОЛЬКО в самой Canon DPP: "
        "«Файл → Конвертировать и сохранить» для одного снимка или "
        "«Пакетная обработка» для папки."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# find_cr2()
# --------------------------------------------------------------------------


def _is_hidden(entry: os.DirEntry[str]) -> bool:
    """True for hidden/system directories we should not descend into."""
    name = entry.name
    if name.startswith("."):
        return True
    if name in ("$RECYCLE.BIN", "System Volume Information"):
        return True
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    FILE_ATTRIBUTE_HIDDEN = 0x2
    FILE_ATTRIBUTE_SYSTEM = 0x4
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def find_cr2(root: str | Path, recursive: bool = True, *,
             cancel: threading.Event | None = None,
             on_problem: Callable[[Path, OSError], None] | None = None) -> list[Path]:
    """Find every .CR2 under root (case-insensitive), sorted.

    Skips hidden and system directories.  Handles Cyrillic paths (the file
    system encoding is UTF-8 on Windows for os.scandir).  A single file passed
    as root is returned as a one-element list when it has a CR2 extension.

    Two things this function must NOT do silently:

    * Swallow an unreadable directory.  A permission-denied or offline subfolder
      used to vanish from the batch with no row, no message and no change to any
      count - the run then reported "готово 460 из 460, ошибок 0" while 40
      photos were never seen.  `on_problem` is how the caller learns.
    * Ignore the cancel event.  The walk is the one uninterruptible phase of a
      job: pointing the tool at a whole drive used to freeze "Отмена" for
      minutes.

    Args:
        root: directory (or a single file) to scan.
        recursive: descend into sub-directories.
        cancel: checked per directory and periodically inside big directories;
            on cancel the walk stops and returns what it found so far.
        on_problem: called as (path, exc) for every directory or entry that
            could not be read.

    Returns:
        Sorted list of paths.
    """
    base = Path(root)
    out: list[Path] = []

    def problem(path: Path, exc: OSError) -> None:
        log.debug("пропуск %s: %s", path, exc)
        if on_problem is not None:
            try:
                on_problem(path, exc)
            except Exception:
                log.exception("on_problem упал на %s", path)

    try:
        is_file, is_dir = base.is_file(), base.is_dir()
    except OSError as exc:
        # A disconnected UNC root raises instead of returning False.
        problem(base, exc)
        return out
    if is_file:
        if base.suffix.lower() in CR2_EXTS:
            out.append(base)
        return out
    if not is_dir:
        return out

    stack: list[Path] = [base]
    seen_dirs: set[str] = set()
    while stack:
        if cancel is not None and cancel.is_set():
            break
        cur = stack.pop()
        try:
            key = os.path.normcase(os.path.realpath(cur))
        except OSError:
            key = os.path.normcase(str(cur))
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        try:
            with os.scandir(cur) as it:
                n = 0
                for entry in it:
                    n += 1
                    # A single directory can hold hundreds of thousands of
                    # entries on a slow share: a per-directory check alone
                    # would still stall Отмена for minutes.
                    if n % 512 == 0 and cancel is not None and cancel.is_set():
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not _is_hidden(entry):
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if os.path.splitext(entry.name)[1].lower() in CR2_EXTS:
                                out.append(Path(entry.path))
                    except OSError as exc:
                        problem(Path(entry.path), exc)
                        continue
        except (OSError, PermissionError) as exc:
            problem(cur, exc)
            continue
    out.sort(key=lambda p: str(p).lower())
    return out


# --------------------------------------------------------------------------
# EXIF rebuilding
# --------------------------------------------------------------------------


def _ifd_size(entries: Sequence[list[Any]]) -> int:
    return 2 + 12 * len(entries) + 4


def _pool_size(entries: Sequence[list[Any]]) -> int:
    total = 0
    for _tag, typ, count, val in entries:
        n = _TYPE_SIZE.get(typ, 1) * count
        if n > 4:
            total += n + (n & 1)   # every out-of-line value is word-aligned
    return total


def _build_tiff(ifd0: list[list[Any]],
                exif: list[list[Any]],
                gps: list[list[Any]],
                interop: list[list[Any]],
                thumb: bytes | None,
                endian: str) -> bytes:
    """Emit a complete, self-contained little TIFF for an APP1 payload.

    Inline-vs-out-of-line depends ONLY on (type, count) and never on offsets,
    so the size of every IFD plus its value pool is known before any offset is
    assigned.  That makes offset rewriting a single forward layout pass with no
    iteration: size -> place -> back-patch the sub-IFD pointers (all LONG/1 and
    therefore inline, so their size cannot change) -> emit.
    """
    e = endian

    def ptr(tag: int) -> list[Any]:
        return [tag, 4, 1, b"\x00\x00\x00\x00"]

    ifd0 = [list(x) for x in ifd0 if x[0] not in (T_EXIF_IFD, T_GPS_IFD)]
    exif = [list(x) for x in exif if x[0] != T_INTEROP_IFD]
    gps = [list(x) for x in gps]
    interop = [list(x) for x in interop]

    if exif or interop:
        ifd0.append(ptr(T_EXIF_IFD))
    if gps:
        ifd0.append(ptr(T_GPS_IFD))
    if interop:
        exif.append(ptr(T_INTEROP_IFD))

    ifd1: list[list[Any]] = []
    if thumb:
        rat = struct.pack(e + "II", 72, 1)
        ifd1 = [
            [T_COMPRESSION, 3, 1, struct.pack(e + "H", 6)],
            [T_XRES, 5, 1, rat],
            [T_YRES, 5, 1, rat],
            [T_RES_UNIT, 3, 1, struct.pack(e + "H", 2)],
            [T_THUMB_OFFSET, 4, 1, b"\x00\x00\x00\x00"],
            [T_THUMB_LENGTH, 4, 1, struct.pack(e + "I", len(thumb))],
        ]

    for lst in (ifd0, exif, gps, interop, ifd1):
        lst.sort(key=lambda x: x[0])   # TIFF 6.0 requires ascending tag order

    layout: dict[str, tuple[int, list[list[Any]]]] = {}
    pos = 8
    for name, lst in (("ifd0", ifd0), ("exif", exif), ("gps", gps),
                      ("interop", interop), ("ifd1", ifd1)):
        if not lst and name != "ifd0":
            continue
        layout[name] = (pos, lst)
        pos += _ifd_size(lst) + _pool_size(lst)
        if pos & 1:
            pos += 1
    thumb_off = pos

    def setptr(lst: list[list[Any]], tag: int, value: int) -> None:
        for ent in lst:
            if ent[0] == tag:
                ent[3] = struct.pack(e + "I", value)

    if "exif" in layout:
        setptr(ifd0, T_EXIF_IFD, layout["exif"][0])
    if "gps" in layout:
        setptr(ifd0, T_GPS_IFD, layout["gps"][0])
    if "interop" in layout:
        setptr(exif, T_INTEROP_IFD, layout["interop"][0])
    if ifd1:
        setptr(ifd1, T_THUMB_OFFSET, thumb_off)

    out = bytearray(b"II" if e == "<" else b"MM")
    out += struct.pack(e + "HI", 42, 8)
    for name in ("ifd0", "exif", "gps", "interop", "ifd1"):
        if name not in layout:
            continue
        start, lst = layout[name]
        if len(out) != start:
            raise Cr2Error("сбой раскладки EXIF: %s at %d, ожидалось %d" % (name, len(out), start))
        pool_base = start + _ifd_size(lst)
        ent_buf = bytearray(struct.pack(e + "H", len(lst)))
        pool = bytearray()
        for tag, typ, count, val in lst:
            n = _TYPE_SIZE.get(typ, 1) * count
            if n <= 4:
                ent_buf += struct.pack(e + "HHI", tag, typ, count) + bytes(val).ljust(4, b"\x00")
            else:
                voff = pool_base + len(pool)
                ent_buf += struct.pack(e + "HHI", tag, typ, count) + struct.pack(e + "I", voff)
                pool += bytes(val)
                if len(pool) & 1:
                    pool += b"\x00"
        # Only IFD0 may chain to IFD1; Exif/GPS/Interop next-IFD MUST be 0.
        nxt = layout["ifd1"][0] if (name == "ifd0" and "ifd1" in layout) else 0
        ent_buf += struct.pack(e + "I", nxt)
        out += ent_buf + pool
        if len(out) & 1:
            out += b"\x00"
    if thumb:
        if len(out) != thumb_off:
            raise Cr2Error("сбой раскладки миниатюры EXIF")
        out += thumb
    return bytes(out)


def _collect(view: _View, entries: Sequence[_Entry],
             drop: frozenset[int] | set[int]) -> list[list[Any]]:
    """Read entries into [tag, type, count, value_bytes] quadruples."""
    out: list[list[Any]] = []
    for ent in entries:
        if ent.tag in drop:
            continue
        try:
            data = ent.data(view)
        except Cr2Error:
            continue
        if len(data) != _TYPE_SIZE.get(ent.typ, 1) * ent.count:
            continue
        if ent.typ == 2:
            # Exif 2.3 requires the count of an ASCII value to INCLUDE the
            # terminating NUL.  Sources written without it used to be copied
            # verbatim, and since the value pool is only word-aligned, an even
            # unterminated string sits directly against the next value: a reader
            # that does strlen() on the field then reports two tags glued
            # together ("Canon1Canon EOS 40D").  Re-terminate and recount.
            if not data or data[-1:] != b"\x00":
                data += b"\x00"
            out.append([ent.tag, 2, len(data), data])
            continue
        out.append([ent.tag, ent.typ, ent.count, data])
    return out


def _build_exif_app1(view: _View,
                     ifd0_entries: Sequence[_Entry],
                     exif_entries: Sequence[_Entry],
                     out_w: int,
                     out_h: int,
                     orientation: int,
                     thumb: bytes | None,
                     opts: ConvertOptions) -> tuple[bytes, list[str]]:
    """Build a fresh APP1 Exif segment from the CR2's own metadata.

    All offsets in the emitted TIFF are relative to its own 'II'/'MM' byte
    (which lands at output file offset 12), never copied from the CR2.

    MakerNote is DROPPED by default and that is deliberate: Canon's MakerNote
    is a nested IFD whose out-of-line value offsets are relative to the CR2's
    TIFF base, and the bytes those offsets point at live OUTSIDE the declared
    UNDEFINED byte count.  Copying the blob verbatim into a new container makes
    every string/array/rational inside it point at unrelated JPEG entropy data.
    keep_makernote=True therefore copies a knowingly-broken blob and is only
    there for users who want the raw bytes for forensic recovery.

    Returns:
        (app1_bytes, dropped) where dropped is a list of Russian labels for the
        optional blocks that had to be removed to fit the 64 KB APP1 cap.
    """
    e = view.endian
    dropped: list[str] = []

    drop0 = set(_DROP_FROM_IFD0) | {T_EXIF_IFD, T_GPS_IFD, T_ORIENTATION}
    ifd0 = _collect(view, ifd0_entries, drop0)
    # Orientation is written by us: either the reconciled value, or 1 when the
    # rotation has been baked into the pixels.
    ifd0.append([T_ORIENTATION, 3, 1, struct.pack(e + "H", max(1, min(8, orientation)))])

    drop_exif = {T_INTEROP_IFD, T_EXIF_IMAGE_W, T_EXIF_IMAGE_H}
    if not opts.keep_makernote:
        drop_exif.add(T_MAKERNOTE)
    exif = _collect(view, exif_entries, drop_exif)
    # These two MUST equal the real output pixel dimensions.
    exif.append([T_EXIF_IMAGE_W, 4, 1, struct.pack(e + "I", out_w)])
    exif.append([T_EXIF_IMAGE_H, 4, 1, struct.pack(e + "I", out_h)])
    if not _has_tag(exif, 0x9000):
        exif.append([0x9000, 7, 4, b"0230"])          # ExifVersion
    if not _has_tag(exif, 0xA000):
        exif.append([0xA000, 7, 4, b"0100"])          # FlashpixVersion
    if not _has_tag(exif, 0x9101):
        exif.append([0x9101, 7, 4, b"\x01\x02\x03\x00"])  # ComponentsConfiguration

    gps: list[list[Any]] = []
    if not opts.strip_gps:
        ent = _find(ifd0_entries, T_GPS_IFD)
        if ent is not None:
            try:
                gps_off = ent.ints(view)[0]
                gps_entries, _ = _read_ifd(view, gps_off)
                gps = _collect(view, gps_entries, set())
            except (Cr2Error, IndexError):
                gps = []

    interop: list[list[Any]] = []
    ent = _find(exif_entries, T_INTEROP_IFD)
    if ent is not None:
        try:
            iop_off = ent.ints(view)[0]
            iop_entries, _ = _read_ifd(view, iop_off)
            interop = _collect(view, iop_entries, set())
        except (Cr2Error, IndexError):
            interop = []

    # Shed optional blocks, biggest-first, until the TIFF fits the APP1 cap.
    # Exif has NO legal multi-segment continuation, so the only way out is to
    # drop whole entries (never truncate a value, never write two Exif APP1s).
    shed: list[tuple[str, Callable[[], None]]] = [
        ("MakerNote", lambda: _remove_tag(exif, T_MAKERNOTE)),
        ("миниатюра", lambda: None),           # handled via the thumb variable
        ("GPS", lambda: gps.clear()),
        ("Interop", lambda: interop.clear()),
        ("PrintIM", lambda: _remove_tag(exif, 0xC4A5)),
        ("UserComment", lambda: _remove_tag(exif, 0x9286)),
    ]
    step = 0
    while True:
        try:
            tiff = _build_tiff(ifd0, exif, gps, interop, thumb, e)
        except Cr2Error:
            raise
        if len(tiff) <= _EXIF_TIFF_BUDGET:
            break
        if step >= len(shed):
            # Last resort: drop the largest remaining out-of-line values.
            # `and` used to short-circuit here, so the Exif IFD was drained one
            # capture tag at a time (shot date, exposure, aperture...) while the
            # oversized IFD0 tag that actually caused the overflow survived.
            if not _drop_largest(ifd0, exif, gps, interop):
                raise Cr2Error("EXIF не помещается в APP1 даже после удаления блоков")
            if "крупные необязательные теги" not in dropped:
                dropped.append("крупные необязательные теги")
            continue
        label, action = shed[step]
        step += 1
        if label == "миниатюра":
            if thumb:
                thumb = None
                dropped.append(label)
            continue
        before = len(tiff)
        action()
        try:
            after = len(_build_tiff(ifd0, exif, gps, interop, thumb, e))
        except Cr2Error:
            after = before
        if after < before:
            dropped.append(label)

    payload = b"Exif\x00\x00" + tiff
    if len(payload) + 2 > 65535:
        raise Cr2Error("переполнение APP1: %d байт" % (len(payload) + 2))
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload, dropped


def _has_tag(lst: Sequence[list[Any]], tag: int) -> bool:
    return any(x[0] == tag for x in lst)


def _remove_tag(lst: list[list[Any]], tag: int) -> None:
    for i, x in enumerate(lst):
        if x[0] == tag:
            del lst[i]
            return


def _drop_largest(*lists: list[list[Any]]) -> bool:
    """Drop the single largest out-of-line value ACROSS all the given IFDs.

    Scanning every list together is what keeps the shed honest: whichever IFD
    is really over budget loses its biggest value first.  Mandatory tags are
    safe by construction - every one the builder synthesises (Orientation,
    ExifVersion, ComponentsConfiguration, FlashpixVersion, PixelX/YDimension)
    is 4 bytes or fewer and therefore inline, and the Exif/GPS/Interop pointer
    entries are created inside _build_tiff on its own copies.

    Returns:
        True if something was dropped.
    """
    best: tuple[list[list[Any]], int] | None = None
    best_n = 4
    for lst in lists:
        for i, (_tag, typ, count, _val) in enumerate(lst):
            n = _TYPE_SIZE.get(typ, 1) * count
            if n > best_n:
                best_n, best = n, (lst, i)
    if best is None:
        return False
    del best[0][best[1]]
    return True


# --------------------------------------------------------------------------
# JPEG assembly
# --------------------------------------------------------------------------


def _icc_app2(profile: bytes) -> list[bytes]:
    """Chunk an ICC profile into APP2 segments (1-based index, 14-byte overhead)."""
    out: list[bytes] = []
    limit = 65533 - 14
    chunks = [profile[i:i + limit] for i in range(0, len(profile), limit)] or [b""]
    total = len(chunks)
    if total > 255:
        return []
    for i, chunk in enumerate(chunks, 1):
        payload = b"ICC_PROFILE\x00" + bytes([i, total]) + chunk
        out.append(b"\xff\xe2" + struct.pack(">H", len(payload) + 2) + payload)
    return out


_XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"


def _splice(blob: bytes, front: Sequence[bytes], drop_xmp: bool = False,
            segments: Sequence[tuple[int, int, int]] | None = None) -> bytes:
    """SOI + our own segments + every original segment except APP0/APP1-Exif.

    The preview's own APP0 (JFIF) and APP1 (Exif) are stripped because we write
    replacements; an APP1 whose payload is XMP is kept UNLESS `drop_xmp` says we
    are writing our own XMP packet (XMP Part 3 allows exactly one primary packet
    per JPEG, and the CR2's IFD0 copy is the file-level authority).  DQT/DHT/DRI/
    SOF/COM/APP2(ICC)/APP13 are preserved verbatim in their original relative
    order, and everything from the SOS header onward (entropy data + EOI) is
    copied byte-for-byte: that is what makes this path bit-exact.

    Args:
        blob: the source JPEG stream.
        front: segments to inject right after SOI.
        drop_xmp: discard the source's own XMP APP1 (we inject a replacement).
        segments: a pre-computed walk of `blob`, to avoid walking it twice.
    """
    out = bytearray(b"\xff\xd8")
    for seg in front:
        out += seg
    segs = _jpeg_segments(blob) if segments is None else segments
    for marker, start, length in segs:
        if marker == 0xD8:
            continue
        if marker in (0xE0, 0xE1):
            # Wide enough to see the whole 29-byte XMP namespace, not just Exif.
            payload = blob[start + 4:start + min(length, 33)]
            if marker == 0xE1 and not payload.startswith(b"Exif\x00\x00"):
                if drop_xmp and payload.startswith(_XMP_NS):
                    continue                        # our own packet replaces it
                out += blob[start:start + length]   # XMP or similar: keep it
            continue
        out += blob[start:start + length]
        if marker == 0xDA:
            break
    return bytes(out)


# --------------------------------------------------------------------------
# Orientation reconciliation
# --------------------------------------------------------------------------


def _reconcile_orientation(exif_orientation: int,
                           prev_w: int, prev_h: int,
                           raw_w: int, raw_h: int,
                           preview_source: str = "") -> tuple[int, bool]:
    """Decide the orientation value to write, avoiding a double rotation.

    EXIF orientations 5..8 transpose the displayed aspect ratio: the pixels are
    stored one way and a conformant viewer rotates them at display time.  Some
    writers, however, store an ALREADY-ROTATED preview while leaving
    Orientation at 6 or 8.  If we pass that value through, the viewer rotates a
    second time and the picture ends up on its side.

    The tell is the aspect ratio: compare the preview's stored orientation
    (landscape vs portrait) against the raw sensor array's.  If the preview is
    portrait while the sensor array is landscape and EXIF claims a 90/270
    rotation, the rotation is already baked into the preview's pixels - so we
    write Orientation=1 instead.

    Returns:
        (orientation_to_write, reconciled_flag)
    """
    if preview_source.startswith("vrd_ihl"):
        # DPP writes its IHL image already rotated and cropped, so the
        # aspect-ratio heuristic below would fire for the wrong reason (a crop
        # to a different aspect, not a baked rotation).  Say "normal" outright.
        return 1, exif_orientation != 1
    if exif_orientation not in (5, 6, 7, 8):
        return (exif_orientation if 1 <= exif_orientation <= 8 else 1), False
    if not (prev_w and prev_h and raw_w and raw_h):
        return exif_orientation, False
    if prev_w == prev_h or raw_w == raw_h:
        return exif_orientation, False
    prev_landscape = prev_w > prev_h
    raw_landscape = raw_w > raw_h
    if prev_landscape != raw_landscape:
        # Stored pixels already carry the rotation: do NOT rotate again.
        return 1, True
    return exif_orientation, False


# --------------------------------------------------------------------------
# convert_one()
# --------------------------------------------------------------------------


def _dst_path(src: Path, opts: ConvertOptions) -> Path:
    base = Path(opts.out_dir) if opts.out_dir else src.parent
    dst = base / (src.stem + (opts.suffix or "") + ".jpg")
    # Belt and braces: validate_suffix() already rejects separators, so this can
    # only fire if a caller bypassed it.  Never let a name push the write out of
    # the destination folder.
    if os.path.normpath(str(dst.parent)) != os.path.normpath(str(base)):
        raise ValueError("суффикс уводит запись за пределы папки назначения")
    return dst


def _dst_key(dst: Path) -> str:
    """Canonical key for destination comparison (NTFS is case-insensitive)."""
    try:
        return os.path.normcase(os.path.abspath(str(dst)))
    except (OSError, ValueError):
        return os.path.normcase(str(dst))


def plan_destinations(paths: Iterable[str | Path],
                      opts: ConvertOptions) -> list[tuple[Path, Path, str]]:
    """Resolve every source to a UNIQUE destination, before any work starts.

    _dst_path() maps src -> out_dir/(stem + suffix + '.jpg'), which throws the
    source's folder away.  Canon bodies roll the frame counter back to IMG_0001
    constantly, so "convert these shoot folders into one output folder" maps
    several sources onto ONE destination as a matter of routine - and two pool
    threads then race on the same file.  Resolving the whole plan here, once,
    single-threaded and in input order, makes the names deterministic and
    independent of thread scheduling.

    THE COLLISION POLICY, in one place (see also the module docstring):

    A collision between two DIFFERENT sources is de-duplicated, never dropped
    and never silently merged.  Candidate names are tried in this fixed order:

        1. <stem><suffix>.jpg                      - the natural name;
        2. <stem>_<parent folder name>.jpg         - most informative;
        3. <stem>_2.jpg, _3.jpg, ... _999.jpg      - short and predictable;
        4. <stem>_<12 hex of sha1(abs source path)>.jpg, then that name with
           _2, _3, ... appended.

    Step 4 cannot run out: the hash is derived from the source path itself, so
    two distinct sources get distinct names, and the trailing counter closes
    even a hash collision.  The only way two entries end up on ONE destination
    is if the very same file was listed twice - which cannot lose data, because
    both conversions write the same bytes (and the second is then refused by the
    overwrite check anyway).  Every rename is explained in the note.

    The decision is intra-batch only - a file that is already on disk is a
    different matter and is handled by convert_one's overwrite check, which runs
    both early and again under the destination lock at replace time.

    Both parallel drivers (convert_many here and cr2_convert's --workers pool)
    MUST call this, or the path that skips it stays broken.

    Args:
        paths: the sources, in input order.
        opts: conversion options (out_dir and suffix decide the names).

    Returns:
        [(src, dst, note)] in input order; note is a Russian explanation of a
        rename, or '' when the natural name was free.
    """
    plan: list[tuple[Path, Path, str]] = []
    used: dict[str, Path] = {}
    for raw in paths:
        src = Path(raw)
        try:
            base = _dst_path(src, opts)
        except ValueError:
            # Bad suffix: convert_one reports it per file; keep the plan aligned.
            plan.append((src, Path(str(src) + ".jpg"), ""))
            continue
        note = ""
        if _dst_key(base) not in used:
            cand = base
        else:
            other = used[_dst_key(base)]
            cand = _free_name(base, src, used)
            if _dst_key(cand) == _dst_key(base):
                # The identical file listed twice: one output, not a lost file.
                note = "Этот файл указан в списке дважды — результат будет один"
            else:
                note = ("Имя %s уже занято файлом %s — результат сохранён как %s"
                        % (base.name, other, cand.name))
        used[_dst_key(cand)] = src
        plan.append((src, cand, note))
    return plan


def _free_name(base: Path, src: Path, used: dict[str, Path]) -> Path:
    """First unused candidate for `base`; see plan_destinations' policy block.

    Guaranteed to terminate with a name that is free, unless `src` itself
    already owns `base` - i.e. the same file was listed twice, in which case the
    natural name is returned and both entries genuinely describe one output.
    """
    if used.get(_dst_key(base)) == src:
        return base                       # the identical source, listed twice

    def candidates() -> Iterable[Path]:
        parent = src.parent.name
        if parent and parent not in (".", ".."):
            yield base.with_name("%s_%s%s" % (base.stem, parent, base.suffix))
        for n in range(2, 1000):
            yield base.with_name("%s_%d%s" % (base.stem, n, base.suffix))
        # Deterministic, per-source and effectively unbounded: distinct sources
        # hash differently, and the counter closes even a hash collision.
        digest = hashlib.sha1(_dst_key(src).encode("utf-8", "replace")).hexdigest()[:12]
        yield base.with_name("%s_%s%s" % (base.stem, digest, base.suffix))
        for n in range(2, len(used) + 3):
            yield base.with_name("%s_%s_%d%s" % (base.stem, digest, n, base.suffix))

    for attempt in candidates():
        if _dst_key(attempt) not in used:
            return attempt
    raise AssertionError("не удалось подобрать уникальное имя для %s" % base)  # pragma: no cover


#: `<name>.<something>.tmp` written by _atomic_write; used by the stale sweep.
_TMP_RE = re.compile(r"^.+\.jpg\.[0-9a-f]+\.tmp$", re.IGNORECASE)


def _tmp_path(dst: Path) -> Path:
    """A per-attempt temp path that is never LONGER than the destination.

    Three requirements are met at once; they used to pull against each other.

    * Uniqueness.  The name must identify ONE writer.  The old name was
      `dst.name + '.<pid>.tmp'`, constant across the threads of one process: two
      workers aimed at the same destination opened the SAME file and the
      `except: unlink()` cleanup then deleted the other thread's in-flight temp.
      A random per-attempt token makes every write its own file, which is also
      what lets the cleanup delete it unconditionally - it is provably ours.
    * Length.  Appending ~13 characters can push a perfectly legal destination
      past the 255-character NTFS component limit (or past MAX_PATH when long
      paths are disabled), so the read and the encode succeed and only the write
      fails.  Truncating the base instead of growing it keeps the invariant
      "if the OS accepts dst, it accepts the temp file too".
    * Recognisability.  The truncation must not eat the '.jpg', or the result no
      longer matches _TMP_RE and sweep_stale_tmp() can never clean up after a
      hard kill.  So the STEM is what gets shortened; the shape stays
      `<stem>.jpg.<hex>.tmp` at every length.
    """
    tag = ".%s.tmp" % secrets.token_hex(4)          # fixed 13 characters
    ext = dst.suffix or ""                          # '.jpg' for every real dst
    keep = 255 - len(ext) - len(tag)                # NTFS component limit
    if len(str(dst)) + len(tag) > 250:              # near MAX_PATH: do not grow
        keep = min(keep, len(dst.name) - len(ext) - len(tag))
    return dst.with_name(dst.stem[:max(1, keep)] + ext + tag)


def sweep_stale_tmp(dirs: Iterable[Path], max_age: float = 3600.0) -> int:
    """Best-effort removal of .tmp files orphaned by a hard kill.

    _atomic_write cleans up after every in-Python exception, but a taskkill or a
    power loss between write and replace leaves the temp file next to the user's
    photos forever - nothing else ever looks at it again.

    Deliberately conservative, because a concurrent run of the converter (the
    project ships both a CLI and a GUI, and two drag-and-drop .bat invocations
    are entirely normal) must never have its in-flight temp deleted: only files
    matching `<name>.jpg.<hex>.tmp`, only ones older than `max_age`, and any
    failure to delete is ignored rather than propagated.

    Returns:
        Number of files removed.
    """
    now = time.time()
    removed = 0
    for d in {Path(x) for x in dirs}:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not _TMP_RE.match(e.name):
                continue
            try:
                if now - e.stat().st_mtime < max_age:
                    continue          # may belong to a live writer
                os.unlink(e.path)
                removed += 1
                log.info("удалён осиротевший временный файл %s", e.path)
            except OSError:
                pass                  # locked by a live writer, or already gone
    return removed


#: In-flight destinations, so two threads never os.replace() onto one path.
_dst_locks: dict[str, tuple[threading.Lock, list[int]]] = {}
_dst_locks_guard = threading.Lock()


class _DestLock:
    """Serialise writers aimed at the same destination path.

    A unique temp name stops two threads from trampling one another's temp
    file, but on Windows two concurrent MoveFileEx calls onto the SAME
    destination still collide (measured: 4 of 8 writers failed with
    "Отказано в доступе").  plan_destinations() makes that impossible for the
    batch drivers, but convert_one is public API and a caller may fan it out
    itself, so close the window here as well.  Entries are reference-counted
    and removed when the last writer leaves, so the dict cannot grow without
    bound over a long-running session.
    """

    __slots__ = ("key", "lock")

    def __init__(self, dst: Path) -> None:
        self.key = _dst_key(dst)

    def __enter__(self) -> "_DestLock":
        with _dst_locks_guard:
            entry = _dst_locks.get(self.key)
            if entry is None:
                entry = (threading.Lock(), [0])
                _dst_locks[self.key] = entry
            entry[1][0] += 1
            self.lock = entry[0]
        self.lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.lock.release()
        with _dst_locks_guard:
            entry = _dst_locks.get(self.key)
            if entry is not None:
                entry[1][0] -= 1
                if entry[1][0] <= 0:
                    _dst_locks.pop(self.key, None)


#: os.replace retry schedule, in seconds.  Anti-virus and Windows Search both
#: open a freshly written file for a few milliseconds; a single MoveFileEx that
#: lands in that window returns WinError 5 and would otherwise lose the file.
_REPLACE_DELAYS = (0.01, 0.03, 0.08, 0.2)


def _replace_with_retry(tmp: Path, dst: Path) -> None:
    """os.replace with a short bounded retry on transient Windows sharing errors.

    Only PermissionError is retried, and only four times over ~0.3 s total: a
    genuine permission problem (read-only folder, someone's photo viewer holding
    the JPEG open forever) still surfaces, just a third of a second later.
    """
    for delay in _REPLACE_DELAYS:
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(tmp, dst)               # last attempt: let the error propagate


def _atomic_write(dst: Path, data: bytes, src: Path, overwrite: bool = True) -> None:
    """Write to a unique .tmp next to dst, os.replace, then copy the mtime.

    `overwrite=False` re-checks the destination INSIDE the lock, immediately
    before the replace, and raises FileExistsError instead of clobbering.  The
    early check in convert_one() is a cheap courtesy that avoids doing the work;
    this one is the check that is actually race-free, and it is what stops two
    concurrent callers (convert_one is public API and may be fanned out by a
    caller that skipped plan_destinations) from destroying each other's output.

    The mtime copy runs INSIDE the same lock.  os.utime() opens a handle on the
    destination, and on Windows MoveFileEx onto a path that anybody holds open
    fails with WinError 5; with the utime outside the lock, 8 writers aimed at
    one destination failed in 14 runs out of 25 (measured).  Everything that
    touches `dst` has to be serialised, not just the replace itself.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dst)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        with _DestLock(dst):
            if not overwrite and dst.exists():
                raise FileExistsError(2, "файл уже существует", str(dst))
            _replace_with_retry(tmp, dst)
            try:
                st = src.stat()
                os.utime(dst, (st.st_atime, st.st_mtime))
            except OSError as exc:
                log.debug("не удалось перенести mtime на %s: %s", dst, exc)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()          # provably OUR file: the token is unique
        except OSError:
            pass
        raise


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def convert_one(src: str | Path, opts: ConvertOptions, *,
                dst: Path | None = None,
                cancel: threading.Event | None = None) -> Result:
    """Convert one CR2 to JPEG.

    What comes out, in every mode, is the CAMERA's rendering of the frame - the
    as-shot Picture Style, white balance and contrast, i.e. exactly what the CR2
    looks like in a viewer.  No mode applies a Canon DPP recipe; only Canon's own
    engine can do that ("Конвертировать и сохранить" / "Пакетная обработка").

    Modes (the strings are Result.mode and are part of the public contract):
      * 'lossless'  - the embedded JPEG's compressed segments are copied
                      verbatim (bit-exact entropy data), rewrapped in a freshly
                      built APP1 Exif (+ APP2 ICC when present).  Nothing is
                      decoded and nothing is recompressed.  On bodies that embed
                      a full-resolution preview (550D/600D and later) this yields
                      the full-size image; older bodies embed a smaller one and
                      that is reported, never silently upscaled.  Default
                      whenever no resize and no bake_rotation is requested.
      * 'reencode'  - decodes and re-compresses the SAME embedded JPEG; needed
                      for max_side / bake_rotation.  Requires Pillow.  Not
                      bit-exact, and it is said so in the message.
      * 'raw'       - last resort when there is no usable embedded JPEG:
                      rawpy/LibRaw builds a new image from the sensor data.
                      That is neither the camera's own render nor a DPP render,
                      and it is said so in the message.

    Never raises: every failure is reported in Result.message (Russian).

    Args:
        src: path to the .CR2 file.
        opts: conversion options.
        dst: pre-resolved destination from plan_destinations().  Batch callers
            MUST pass it: without a plan, two sources with the same stem in
            different folders map to one destination and race each other.
            Defaults to _dst_path(src, opts) for single-file callers.
        cancel: checked between stages so a cancelled batch stops promptly.

    Returns:
        Result describing what happened.
    """
    src = Path(src)
    res = Result(src=src)

    err = validate_suffix(opts.suffix)
    if err:
        # _dst_path would otherwise let the suffix redirect the write outside
        # the destination folder, or create directory trees nobody asked for.
        res.message = "Некорректный суффикс: %s" % err
        return res

    if _cancelled(cancel):
        res.skipped = True
        res.message = "Отменено пользователем"
        return res

    info = probe(src)
    res.info = info

    if dst is None:
        try:
            dst = _dst_path(src, opts)
        except ValueError as exc:
            res.message = "Некорректный суффикс: %s" % exc
            return res
    res.dst = dst

    if dst.exists() and not opts.overwrite:
        res.skipped = True
        res.message = "Пропущен: файл уже существует — %s (включите перезапись)" % dst.name
        return res

    warn: list[str] = []

    preview = info.best
    if opts.prefer_dpp_preview:
        dpp_preview = info.best_dpp
        if dpp_preview is not None:
            preview = dpp_preview
        elif preview is not None:
            warn.append("превью DPP в файле нет — сохранён рендер камеры")
    if preview is None or info.error:
        if info.has_dpp_recipe:
            warn.append("правки DPP НЕ применены — эта программа не умеет применять "
                        "рецепт (см. подсказку о рецепте)")
        return _convert_raw_fallback(src, dst, opts, info, res, warn)

    res.source = preview.source
    from_dpp = preview.source.startswith("vrd_ihl")
    if from_dpp and not opts.prefer_dpp_preview:
        # Cr2Info.best only reaches an IHL image when there is NO documented
        # preview at all.  That is a fallback, not a choice, and it must be said
        # out loud - the file otherwise looks like an ordinary export while
        # actually carrying DPP's own undocumented, possibly cropped picture.
        warn.append("пригодного превью камеры в файле нет — взято собственное "
                    "превью DPP из трейлера (%s): источник недокументированный, "
                    "кадрирование и размер могут отличаться" % preview.source)
    if info.has_dpp_recipe:
        if from_dpp:
            if opts.prefer_dpp_preview:
                warn.append("по вашему выбору экспортировано собственное превью DPP "
                            "(%s) — оно может отражать правки, но это не "
                            "гарантировано; размер и кадрирование могут отличаться "
                            "от остальных файлов" % preview.source)
        else:
            warn.append("правки DPP НЕ применены — эта программа не умеет применять "
                        "рецепт; сохранён рендер камеры (см. подсказку о рецепте)")

    # "Full size"? - compare against the raw pixel count, never a hard-coded
    # ratio: 20D/30D/40D/450D write a half-linear preview by design while the
    # 600D and later write a full-resolution one.  Two cases where the
    # comparison is meaningless and the warning would be pure noise:
    #   * a DPP preview is legitimately small and often cropped;
    #   * an mRAW/sRAW frame records fewer pixels than the sensor has.
    raw_px = info.raw_width * info.raw_height
    if from_dpp:
        pass
    elif info.raw_subsampled:
        warn.append("mRAW/sRAW: доля кадра не вычисляется — записанный кадр "
                    "меньше полного сенсора")
    elif raw_px and preview.pixels:
        ratio = preview.pixels / raw_px
        if ratio < opts.min_preview_ratio:
            warn.append(
                "превью %dx%d — это лишь %d%% площади кадра %dx%d (полного JPEG в файле нет)"
                % (preview.width, preview.height, round(ratio * 100),
                   info.raw_width, info.raw_height)
            )

    if _cancelled(cancel):
        res.skipped = True
        res.message = "Отменено пользователем"
        return res

    need_resize = bool(opts.max_side > 0 and max(preview.width, preview.height) > opts.max_side)
    need_reencode = need_resize or opts.bake_rotation

    if need_reencode or not opts.lossless:
        return _convert_reencode(src, dst, opts, info, preview, res, warn)
    return _convert_lossless(src, dst, opts, info, preview, res, warn)


def _open_for_convert(src: Path, info: Cr2Info, preview: Preview):
    """Context helper: open the file and return (view, ifd0, exif, extras)."""
    f = open(src, "rb")
    try:
        size = os.fstat(f.fileno()).st_size
        view = _View(f, size, info.byte_order)
        hdr = view.read_at(0, 8)
        ifd0_off = struct.unpack(view.endian + "I", hdr[4:8])[0]
        ifd0, nxt = _read_ifd(view, ifd0_off)
        exif_entries: list[_Entry] = []
        ent = _find(ifd0, T_EXIF_IFD)
        if ent is not None:
            try:
                exif_entries, _ = _read_ifd(view, ent.ints(view)[0])
            except (Cr2Error, IndexError):
                exif_entries = []
        icc = b""
        icc_ent = _find(ifd0, T_ICC_PROFILE)
        if icc_ent is not None:
            try:
                icc = icc_ent.data(view)
            except Cr2Error:
                icc = b""
        xmp = b""
        xmp_ent = _find(ifd0, T_XMP)
        if xmp_ent is not None:
            try:
                xmp = xmp_ent.data(view)
            except Cr2Error:
                xmp = b""
        blob = view.read_at(preview.offset, preview.length)
        # A small EXIF thumbnail, taken from IFD1 when it is not the image we
        # are exporting and it fits comfortably in the APP1 budget.
        thumb = b""
        # When we export DPP's own IHL preview, DPP's own IHL thumbnail is the
        # one that matches those pixels; otherwise use the camera's IFD1 one.
        wanted = ("vrd_ihl_thumb", "ifd1") if preview.source.startswith("vrd_ihl") else ("ifd1",)
        for prefix in wanted:
            for cand in info.previews:
                if cand.source.startswith(prefix) and cand.offset != preview.offset:
                    if cand.length < 60000:
                        try:
                            thumb = view.read_at(cand.offset, cand.length)
                        except Cr2Error:
                            thumb = b""
                    break
            if thumb:
                break
        return f, view, ifd0, exif_entries, blob, icc, xmp, thumb
    except BaseException:
        f.close()
        raise


def _verify_output(data: bytes, want_w: int, want_h: int) -> str:
    """Re-parse the produced JPEG; return '' when it matches, else a RU reason.

    A SOF and the right dimensions are NOT enough.  _jpeg_sof() returns as soon
    as it finds the frame header, so it validates a stream that carries no scan
    data at all - which is exactly what a truncated preview or a broken segment
    length produces.  The output must therefore also contain a real SOS and an
    EOI that follows it.
    """
    sof = _jpeg_sof(data)
    if sof is None:
        return "результат не разбирается как JPEG (нет SOF)"
    if want_w and want_h and (sof["w"] != want_w or sof["h"] != want_h):
        return ("размеры результата %dx%d не совпадают с ожидаемыми %dx%d"
                % (sof["w"], sof["h"], want_w, want_h))
    if data[:2] != b"\xff\xd8":
        return "результат не начинается с SOI"
    # Find the SOS by walking the chain.  A naive `b"\xff\xda" in data` is NOT
    # safe: those two bytes occur inside APP1/DHT payloads of streams that have
    # no scan segment whatsoever.
    segs, status = _jpeg_segments_checked(data)
    sos_start = -1
    for marker, start, _length in segs:
        if marker == 0xDA:
            sos_start = start
            break
    if sos_start < 0:
        return ("результат оборван: в JPEG нет маркера SOS — сжатые данные "
                "не скопированы" + ("" if status == SEG_COMPLETE else " (цепочка маркеров повреждена)"))
    # Scanning our OWN product backwards for FFD9 is safe (it is not the CR2,
    # so the VRD trailer's decoy EOI cannot be reached).  Anchoring on the last
    # two bytes would be wrong: a legitimately padded preview keeps its padding
    # after the EOI and still decodes fine.
    eoi = data.rfind(b"\xff\xd9")
    if eoi <= sos_start:
        return "результат оборван: после SOS нет маркера EOI"
    return ""


def _finish(res: Result, dst: Path, data: bytes, mode: str,
            w: int, h: int, head: str, warn: Sequence[str],
            dropped: Sequence[str], src: Path,
            overwrite: bool = True) -> Result:
    problem = _verify_output(data, w, h)
    if problem:
        res.ok = False
        res.message = "Ошибка: %s" % problem
        return res
    try:
        _atomic_write(dst, data, src, overwrite=overwrite)
    except FileExistsError:
        # Someone created the destination between convert_one's early check and
        # this replace.  Report it exactly like the early check does, and leave
        # the existing file alone.
        res.ok = False
        res.skipped = True
        res.message = "Пропущен: файл уже существует — %s (включите перезапись)" % dst.name
        return res
    except OSError as exc:
        res.ok = False
        res.message = "Не удалось записать %s: %s" % (dst.name, exc)
        return res
    res.ok = True
    res.mode = mode
    res.width, res.height = w, h
    res.bytes_out = len(data)
    parts = [head]
    if dropped:
        parts.append("из EXIF удалено: %s" % ", ".join(dropped))
    parts.extend(warn)
    res.message = "; ".join(p for p in parts if p)
    return res


def _convert_lossless(src: Path, dst: Path, opts: ConvertOptions, info: Cr2Info,
                      preview: Preview, res: Result, warn: list[str]) -> Result:
    """Byte-exact path: rewrap the preview's own compressed segments."""
    try:
        f, view, ifd0, exif_entries, blob, icc, xmp, thumb = _open_for_convert(src, info, preview)
    except (OSError, Cr2Error) as exc:
        res.ok = False
        res.message = "Не удалось прочитать превью: %s" % exc
        return res
    try:
        segs, status = _jpeg_segments_checked(blob)
        if status != SEG_COMPLETE:
            # A bare `break` in the walk used to return a PREFIX of the stream,
            # and splicing that prefix produced a header-only .jpg which was
            # then announced as a byte-exact copy.  Refuse instead.
            res.ok = False
            res.message = ("Превью повреждено: цепочка маркеров JPEG обрывается "
                           "до сжатых данных — побайтовая копия невозможна")
            return res

        orientation, reconciled = _reconcile_orientation(
            info.orientation, preview.width, preview.height,
            info.raw_width, info.raw_height, preview.source)
        if reconciled:
            warn.append("поворот уже применён к пикселям превью — EXIF Orientation "
                        "выставлен в 1, чтобы не повернуть изображение дважды")
            # IFD1 carries no Orientation of its own, so every reader applies
            # IFD0's - which we have just forced to 1.  A thumbnail still stored
            # in the camera's unrotated orientation would then render sideways,
            # with a transposed aspect ratio, next to an already-rotated main
            # image.  This path must not decode anything (that is the whole
            # point of it), so the honest move is to drop a thumbnail whose
            # aspect disagrees with the picture it is supposed to represent.
            tsof = _jpeg_sof(thumb) if thumb else None
            if tsof and tsof["w"] != tsof["h"] and preview.width != preview.height:
                if (tsof["w"] > tsof["h"]) != (preview.width > preview.height):
                    thumb = b""
                    warn.append("миниатюра EXIF убрана: она хранится в исходной "
                                "ориентации и рядом с уже повёрнутым кадром "
                                "показывалась бы боком")

        front: list[bytes] = []
        dropped: list[str] = []
        if opts.copy_exif:
            try:
                app1, dropped = _build_exif_app1(
                    view, ifd0, exif_entries, preview.width, preview.height,
                    orientation, thumb or None, opts)
                front.append(app1)
            except Cr2Error as exc:
                log.debug("EXIF не собран для %s: %s", src, exc)
                warn.append("EXIF не удалось собрать (%s) — записан без метаданных" % exc)
        # Exactly one primary XMP packet per JPEG (XMP Part 3).  When we inject
        # the CR2's own IFD0 packet - the file-level authority, and the only copy
        # consistent with the EXIF we just rebuilt - the preview's own (possibly
        # stale) packet must go; when we inject nothing, it must stay.
        wrote_xmp = False
        if xmp and len(xmp) + 31 <= 65533:
            payload = _XMP_NS + xmp
            front.append(b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload)
            wrote_xmp = True
        blob_has_icc = any(
            m == 0xE2 and blob[st + 4:st + 16] == b"ICC_PROFILE\x00"
            for m, st, _l in segs)
        if icc and not blob_has_icc:
            front.extend(_icc_app2(icc))

        data = _splice(blob, front, drop_xmp=wrote_xmp, segments=segs)
    finally:
        f.close()

    # Guarantee the promise: everything after our injected segments must be a
    # byte-identical copy of the original compressed stream from the first
    # non-APP0/APP1 marker onward.
    head = ("Без перекодирования: встроенный JPEG скопирован как есть "
            "(рендер камеры), %dx%d" % (preview.width, preview.height))
    if preview.source != "ifd0":
        head += " (источник: %s)" % preview.source
    return _finish(res, dst, data, "lossless", preview.width, preview.height,
                   head, warn, dropped, src, overwrite=opts.overwrite)


def _convert_reencode(src: Path, dst: Path, opts: ConvertOptions, info: Cr2Info,
                      preview: Preview, res: Result, warn: list[str]) -> Result:
    """Pillow path: needed for max_side and/or bake_rotation."""
    if not has_pillow():
        res.ok = False
        res.message = ("Для изменения размера или запекания поворота нужна библиотека Pillow. "
                       "Установите её командой:  python -m pip install Pillow")
        return res
    try:
        from PIL import Image, JpegImagePlugin
    except Exception as exc:  # defensive: has_pillow() said yes
        res.ok = False
        res.message = "Pillow не импортируется: %s" % exc
        return res

    try:
        f, view, ifd0, exif_entries, blob, icc, xmp, thumb = _open_for_convert(src, info, preview)
    except (OSError, Cr2Error) as exc:
        res.ok = False
        res.message = "Не удалось прочитать превью: %s" % exc
        return res

    try:
        orientation, reconciled = _reconcile_orientation(
            info.orientation, preview.width, preview.height,
            info.raw_width, info.raw_height, preview.source)
        if reconciled:
            warn.append("поворот уже применён к пикселям превью — повторный поворот не выполняется")
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
        except Exception as exc:
            res.ok = False
            res.message = "Pillow не смог декодировать превью: %s" % exc
            return res

        # Capture the source subsampling BEFORE any resize/transpose: after
        # those, im.format is None and 'keep' would raise.
        try:
            sampling = JpegImagePlugin.get_sampling(im)
        except Exception:
            sampling = -1

        out_orientation = orientation
        if opts.bake_rotation and orientation != 1:
            transposes = {
                2: Image.Transpose.FLIP_LEFT_RIGHT,
                3: Image.Transpose.ROTATE_180,
                4: Image.Transpose.FLIP_TOP_BOTTOM,
                5: Image.Transpose.TRANSPOSE,
                6: Image.Transpose.ROTATE_270,   # PIL rotates CCW: 270 CCW == 90 CW
                7: Image.Transpose.TRANSVERSE,
                8: Image.Transpose.ROTATE_90,
            }
            op = transposes.get(orientation)
            if op is not None:
                im = im.transpose(op)
                # IFD1 carries no Orientation of its own, so every reader applies
                # IFD0's (now 1) to the thumbnail: leaving the camera's original
                # blob there renders the thumbnail sideways, with a transposed
                # aspect ratio, next to a correctly rotated main image.
                if thumb:
                    try:
                        t = Image.open(io.BytesIO(thumb))
                        t.load()
                        t = t.transpose(op)
                        if t.mode not in ("RGB", "L"):
                            t = t.convert("RGB")
                        tb = io.BytesIO()
                        t.save(tb, "JPEG", quality=70, optimize=True)
                        thumb = tb.getvalue()
                    except Exception as exc:
                        # Better no thumbnail at all than a wrongly rotated one.
                        log.debug("миниатюра не повёрнута (%s), убрана из EXIF", exc)
                        thumb = b""
            # Pixels now carry the rotation: the tag must say "normal", or a
            # compliant viewer rotates a second time.
            out_orientation = 1

        if opts.max_side > 0 and max(im.size) > opts.max_side:
            scale = opts.max_side / float(max(im.size))
            new_size = (max(1, int(round(im.size[0] * scale))),
                        max(1, int(round(im.size[1] * scale))))
            im = im.resize(new_size, Image.Resampling.LANCZOS)

        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out_w, out_h = im.size

        save_kw: dict[str, Any] = {
            "quality": max(1, min(100, int(opts.quality))),
            "optimize": True,
            "progressive": False,
        }
        if sampling in (0, 1, 2):
            save_kw["subsampling"] = sampling
        if icc:
            save_kw["icc_profile"] = icc

        buf = io.BytesIO()
        try:
            im.save(buf, "JPEG", **save_kw)
        except Exception as exc:
            res.ok = False
            res.message = "Ошибка кодирования JPEG: %s" % exc
            return res
        encoded = buf.getvalue()

        # Pillow always writes a JFIF APP0 first; Exif requires APP1 to be the
        # first marker after SOI, so re-splice our own segments to the front.
        front: list[bytes] = []
        dropped: list[str] = []
        if opts.copy_exif:
            try:
                app1, dropped = _build_exif_app1(
                    view, ifd0, exif_entries, out_w, out_h,
                    out_orientation, thumb or None, opts)
                front.append(app1)
            except Cr2Error as exc:
                warn.append("EXIF не удалось собрать (%s)" % exc)
        wrote_xmp = False
        if xmp and len(xmp) + 31 <= 65533:
            payload = _XMP_NS + xmp
            front.append(b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload)
            wrote_xmp = True
        # Pillow does not emit XMP today, but it round-trips info["xmp"] in
        # recent versions - keep the same one-packet rule here as well.
        data = _splice(encoded, front, drop_xmp=wrote_xmp)
    finally:
        f.close()

    head = "С перекодированием (Pillow, качество %d): %dx%d" % (opts.quality, out_w, out_h)
    warn.append("это НЕ побайтовая копия — встроенный JPEG камеры пересжат")
    return _finish(res, dst, data, "reencode", out_w, out_h, head, warn, dropped, src,
                   overwrite=opts.overwrite)


def _convert_raw_fallback(src: Path, dst: Path, opts: ConvertOptions, info: Cr2Info,
                          res: Result, warn: list[str]) -> Result:
    """Decode the raw sensor data with rawpy - only when no preview exists."""
    why = info.error or "в файле нет пригодного встроенного JPEG"
    if not opts.allow_raw_fallback:
        res.ok = False
        res.message = "Не удалось сконвертировать: %s (резервное декодирование RAW отключено)" % why
        return res
    if not has_rawpy() or not has_pillow():
        missing = []
        if not has_rawpy():
            missing.append("rawpy")
        if not has_pillow():
            missing.append("Pillow")
        res.ok = False
        res.message = (
            "Не удалось сконвертировать: %s. Для резервного декодирования RAW нужны "
            "%s — установите командой:  python -m pip install %s. "
            "Учтите: декодирование RAW строит изображение заново из данных сенсора "
            "и НЕ содержит правок DPP (и вообще не равно встроенному превью камеры)."
            % (why, " и ".join(missing), " ".join(missing))
        )
        return res
    try:
        import rawpy  # local import: never at module import time
        from PIL import Image
    except Exception as exc:
        res.ok = False
        res.message = "Не удалось загрузить rawpy/Pillow: %s" % exc
        return res

    try:
        with rawpy.imread(str(src)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,     # default False == daylight WB: wrong colours
                no_auto_bright=True,    # default False == automatic exposure stretch
                output_bps=8,
                output_color=rawpy.ColorSpace.sRGB,
            )
        im = Image.fromarray(rgb)
        if opts.max_side > 0 and max(im.size) > opts.max_side:
            scale = opts.max_side / float(max(im.size))
            im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))),
                           Image.Resampling.LANCZOS)
        out_w, out_h = im.size
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=max(1, min(100, int(opts.quality))),
                subsampling=0, optimize=True)
        data = buf.getvalue()
    except Exception as exc:
        res.ok = False
        res.message = "Резервное декодирование RAW не удалось: %s: %s" % (type(exc).__name__, exc)
        return res

    warn.append("изображение построено заново из данных сенсора: это НЕ встроенное превью "
                "камеры и в нём НЕТ правок DPP; цвет и яркость могут отличаться от DPP")
    head = "Резервное декодирование RAW (rawpy): %dx%d" % (out_w, out_h)
    return _finish(res, dst, data, "raw", out_w, out_h, head, warn, [], src,
                   overwrite=opts.overwrite)


# --------------------------------------------------------------------------
# convert_many()
# --------------------------------------------------------------------------


def convert_many(paths: Iterable[str | Path],
                 opts: ConvertOptions,
                 on_result: Callable[[Result], None] | None = None,
                 on_progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> list[Result]:
    """Convert many CR2 files in parallel, returning results in INPUT order.

    Safe to call from a worker thread; touches no GUI and mutates no globals.
    The callbacks are invoked from the calling thread, one at a time, in input
    order, so a GUI can simply push the objects onto a queue.Queue.

    Args:
        paths: iterable of CR2 paths.
        opts: conversion options (shared, read-only).
        on_result: called once per file with its Result.
        on_progress: called after each file with (done, total).
        cancel: threading.Event; checked between items.  Items already running
            are allowed to finish; everything still queued is dropped.

    Returns:
        List of Result, same length and order as paths.
    """
    items = [Path(p) for p in paths]
    total = len(items)
    results: list[Result] = []
    if total == 0:
        if on_progress:
            on_progress(0, 0)
        return results

    # Resolve every destination BEFORE anything is submitted: two sources with
    # the same stem in different folders otherwise map to one file and two
    # threads race on it (last writer wins, both rows still green).
    plan = plan_destinations(items, opts)
    sweep_stale_tmp(dst.parent for _src, dst, _note in plan)

    workers = min(8, (os.cpu_count() or 4))

    def task(path: Path, dst: Path, note: str) -> Result:
        if cancel is not None and cancel.is_set():
            return Result(src=path, skipped=True, message="Отменено пользователем")
        try:
            res = convert_one(path, opts, dst=dst, cancel=cancel)
        except Exception as exc:  # a worker must never propagate
            log.exception("convert_one(%s) упал", path)
            return Result(src=path, ok=False,
                          message="Непредвиденная ошибка: %s: %s" % (type(exc).__name__, exc))
        if note:
            res.message = "%s; %s" % (note, res.message) if res.message else note
        return res

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cr2conv") as ex:
        # The note travels with its own plan row.  Looking it up in a dict keyed
        # by str(src) silently attached ONE note to every occurrence of a source
        # listed more than once - including the row whose name was NOT renamed.
        futures = [ex.submit(task, src, dst, note) for src, dst, note in plan]
        for i, fut in enumerate(futures):
            # Cancel is honoured BETWEEN items: whatever is still queued is
            # dropped, whatever is already running is allowed to finish (the
            # task function itself re-checks the event at entry).
            if cancel is not None and cancel.is_set() and not fut.done():
                fut.cancel()
            if fut.cancelled():
                res = Result(src=items[i], skipped=True, message="Отменено пользователем")
            else:
                res = _safe_future(fut, items[i])
            results.append(res)
            if on_result:
                try:
                    on_result(res)
                except Exception:
                    log.exception("on_result упал на %s", items[i])
            if on_progress:
                try:
                    on_progress(i + 1, total)
                except Exception:
                    log.exception("on_progress упал")
        if cancel is not None and cancel.is_set():
            ex.shutdown(wait=False, cancel_futures=True)
    return results


def _safe_future(fut: Any, path: Path) -> Result:
    from concurrent.futures import CancelledError
    try:
        return fut.result()
    except CancelledError:
        return Result(src=path, skipped=True, message="Отменено пользователем")
    except Exception as exc:
        return Result(src=path, ok=False,
                      message="Непредвиденная ошибка: %s: %s" % (type(exc).__name__, exc))
