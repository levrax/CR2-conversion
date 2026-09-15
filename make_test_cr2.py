# -*- coding: utf-8 -*-
"""make_test_cr2.py - synthetic CR2 fixture generator (stdlib only).

Builds structurally valid Canon CR2 files for testing cr2_core without needing
a real camera file, Pillow, rawpy or any other third-party package.

What is real here
-----------------
* The TIFF container is real: correct byte order mark, magic 42, the Canon
  'CR' signature at byte 8, a proper IFD0 -> IFD1 -> IFD2 -> IFD3 chain, real
  12-byte IFD entries in ascending tag order, real out-of-line value pools with
  word alignment, real ExifIFD / GPSIFD / MakerNote sub-IFDs.
* The embedded previews are REAL BASELINE JPEGs, hand-assembled here:
  SOI / APP0-JFIF / DQT (the two Annex K quantisation tables) / SOF0 /
  DHT (the four Annex K Huffman tables) / SOS / entropy-coded data / EOI.
  The entropy data is genuine Huffman-coded MCU data - every MCU codes a DC
  difference of 0 (category 0) and an immediate EOB for each of the three
  components - so the stream decodes to a flat neutral-grey image of exactly
  the declared size.  Byte stuffing (0xFF -> 0xFF 0x00) is applied, the final
  byte is padded with 1-bits, and the MCU count matches ceil(w/8)*ceil(h/8)
  for the 4:4:4 (1x1) sampling declared in SOF0/SOS.  These files open in any
  conformant decoder.
* The IFD3 "raw" blob is a lossless-JPEG (SOF3) header exactly like a real
  CR2's sensor data: it is deliberately NOT a viewable frame, which is what
  lets the tests prove that cr2_core never offers it as a preview.
* The DPP trailer is a real "CANON OPTIONAL DATA" VRD trailer with a matching
  0x1C header and 0x40 footer, the big-endian size in both places, and real
  0xFFFF00F4 / 0xFFFF00F5 / 0xFFFF00F7 blocks, including IHL records.

What is NOT real: the pixel content (flat grey), the MakerNote tag values
(plausible shapes, arbitrary numbers) and the sensor geometry.

Public API
----------
    make_cr2(path, **variant_kwargs) -> Path
    baseline_jpeg(width, height) -> bytes
    write_fixture_set(directory) -> dict[str, Path]

    python make_test_cr2.py <directory>
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = ["make_cr2", "baseline_jpeg", "lossless_raw_blob", "write_fixture_set",
           "FIXTURES"]


# ==========================================================================
# JPEG: real baseline encoder for a flat image
# ==========================================================================

_ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)

# ITU T.81 Annex K.1 quantisation tables, natural (row-major) order.
_QUANT_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
_QUANT_CHROMA = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)

# Annex K.3 Huffman tables: (BITS[1..16], HUFFVAL).
_DC_LUMA_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_LUMA_VALS = tuple(range(12))
_DC_CHROMA_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_CHROMA_VALS = tuple(range(12))

_AC_LUMA_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMA_VALS = (
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
    0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
)
_AC_CHROMA_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMA_VALS = (
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
    0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0, 0x15, 0x62, 0x72, 0xD1,
    0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44,
    0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74,
    0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A,
    0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
)


def _canonical_codes(bits: Sequence[int], vals: Sequence[int]) -> dict[int, tuple[int, int]]:
    """Build {symbol: (code, bitlength)} per ITU T.81 Annex C."""
    if sum(bits) != len(vals):
        raise ValueError("Huffman BITS/HUFFVAL mismatch: %d vs %d" % (sum(bits), len(vals)))
    codes: dict[int, tuple[int, int]] = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (code, length)
            k += 1
            code += 1
        code <<= 1
    return codes


_DC_LUMA_CODES = _canonical_codes(_DC_LUMA_BITS, _DC_LUMA_VALS)
_AC_LUMA_CODES = _canonical_codes(_AC_LUMA_BITS, _AC_LUMA_VALS)
_DC_CHROMA_CODES = _canonical_codes(_DC_CHROMA_BITS, _DC_CHROMA_VALS)
_AC_CHROMA_CODES = _canonical_codes(_AC_CHROMA_BITS, _AC_CHROMA_VALS)


def _bits_of(entry: tuple[int, int]) -> str:
    code, length = entry
    return format(code, "0%db" % length)


def _zz(table: Sequence[int]) -> bytes:
    """Reorder a natural-order 8x8 table into zigzag order for a DQT segment."""
    return bytes(table[i] for i in _ZIGZAG)


def _seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _entropy_flat(mcu_count: int) -> bytes:
    """Huffman-coded data for `mcu_count` flat 4:4:4 MCUs.

    Each MCU is Y, Cb, Cr; each block codes DC size 0 (difference 0 - so every
    block keeps the running DC predictor at 0) followed by EOB.  All quantised
    coefficients are therefore 0, which dequantises to a DC of 0 and, after the
    +128 level shift, to a uniform 128 in every component: neutral mid-grey.
    """
    mcu = (_bits_of(_DC_LUMA_CODES[0]) + _bits_of(_AC_LUMA_CODES[0x00])
           + (_bits_of(_DC_CHROMA_CODES[0]) + _bits_of(_AC_CHROMA_CODES[0x00])) * 2)
    bits = mcu * mcu_count
    pad = (-len(bits)) % 8
    bits += "1" * pad
    nbytes = len(bits) // 8
    raw = int(bits, 2).to_bytes(nbytes, "big") if nbytes else b""
    return raw.replace(b"\xff", b"\xff\x00")   # byte stuffing


def baseline_jpeg(width: int, height: int, *, comment: bytes | None = None) -> bytes:
    """Hand-build a complete, decodable baseline JPEG of exactly width x height.

    4:4:4 sampling, 3 components, Annex K tables, flat neutral-grey content.
    """
    if width <= 0 or height <= 0 or width > 65535 or height > 65535:
        raise ValueError("недопустимый размер JPEG: %dx%d" % (width, height))
    out = bytearray(b"\xff\xd8")
    out += _seg(0xE0, b"JFIF\x00" + bytes([1, 1, 0]) + struct.pack(">HH", 1, 1) + bytes([0, 0]))
    if comment:
        out += _seg(0xFE, comment)
    out += _seg(0xDB, bytes([0x00]) + _zz(_QUANT_LUMA) + bytes([0x01]) + _zz(_QUANT_CHROMA))
    sof = bytes([8]) + struct.pack(">HH", height, width) + bytes([3])
    sof += bytes([1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1])
    out += _seg(0xC0, sof)
    dht = b""
    for cls, tid, bits, vals in ((0, 0, _DC_LUMA_BITS, _DC_LUMA_VALS),
                                 (1, 0, _AC_LUMA_BITS, _AC_LUMA_VALS),
                                 (0, 1, _DC_CHROMA_BITS, _DC_CHROMA_VALS),
                                 (1, 1, _AC_CHROMA_BITS, _AC_CHROMA_VALS)):
        dht += bytes([(cls << 4) | tid]) + bytes(bits) + bytes(vals)
    out += _seg(0xC4, dht)
    out += _seg(0xDA, bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0]))
    mcus = ((width + 7) // 8) * ((height + 7) // 8)
    out += _entropy_flat(mcus)
    out += b"\xff\xd9"
    return bytes(out)


def lossless_raw_blob(raw_width: int, raw_height: int, ncomp: int = 2,
                      vsf: int = 1) -> bytes:
    """A CR2-style SOF3 lossless-JPEG blob for IFD3.

    cr2_core derives the sensor geometry as sof_w * ncomp x sof_h * vsf, and it
    must REFUSE to treat an SOF3 frame as a viewable preview.
    """
    if raw_width % ncomp or raw_height % vsf:
        raise ValueError("raw %dx%d не делится на ncomp=%d / vsf=%d"
                         % (raw_width, raw_height, ncomp, vsf))
    sof_w = raw_width // ncomp
    sof_h = raw_height // vsf
    frame = bytes([14]) + struct.pack(">HH", sof_h, sof_w) + bytes([ncomp])
    for i in range(ncomp):
        frame += bytes([i + 1, (1 << 4) | vsf, 0])
    out = bytearray(b"\xff\xd8")
    out += _seg(0xC4, bytes([0x00]) + bytes(_DC_LUMA_BITS) + bytes(_DC_LUMA_VALS))
    out += _seg(0xC3, frame)
    sos = bytes([ncomp]) + b"".join(bytes([i + 1, 0]) for i in range(ncomp)) + bytes([1, 0, 0])
    out += _seg(0xDA, sos)
    out += bytes(256)          # placeholder "sensor data"
    out += b"\xff\xd9"
    return bytes(out)


# ==========================================================================
# TIFF / CR2 assembly
# ==========================================================================

T_IMAGE_WIDTH = 0x0100
T_IMAGE_LENGTH = 0x0101
T_BITS_PER_SAMPLE = 0x0102
T_COMPRESSION = 0x0103
T_PHOTOMETRIC = 0x0106
T_MAKE = 0x010F
T_MODEL = 0x0110
T_STRIP_OFFSETS = 0x0111
T_ORIENTATION = 0x0112
T_SAMPLES_PER_PIXEL = 0x0115
T_ROWS_PER_STRIP = 0x0116
T_STRIP_BYTE_COUNTS = 0x0117
T_XRES = 0x011A
T_YRES = 0x011B
T_PLANAR = 0x011C
T_RES_UNIT = 0x0128
T_SOFTWARE = 0x0131
T_DATETIME = 0x0132
T_ARTIST = 0x013B
T_THUMB_OFFSET = 0x0201
T_THUMB_LENGTH = 0x0202
T_COPYRIGHT = 0x8298
T_EXIF_IFD = 0x8769
T_GPS_IFD = 0x8825
T_EXPOSURE_TIME = 0x829A
T_FNUMBER = 0x829D
T_ISO = 0x8827
T_EXIF_VERSION = 0x9000
T_DATETIME_ORIGINAL = 0x9003
T_CREATE_DATE = 0x9004
T_FOCAL_LENGTH = 0x920A
T_MAKERNOTE = 0x927C
T_USER_COMMENT = 0x9286
T_EXIF_IMAGE_W = 0xA002
T_EXIF_IMAGE_H = 0xA003
T_LENS_MODEL = 0xA434
T_CR2_SLICE = 0xC640

MN_PREVIEW_INFO = 0x00B6
MN_VRD_OFFSET = 0x00D0
MN_SENSOR_INFO = 0x00E0

_VRD_SIG = b"CANON OPTIONAL DATA\x00"
_IHL_SIG = b"IHL Created Optional Item Data\x00\x00"
_VRD_BLOCK_EDIT = 0xFFFF00F4
_VRD_BLOCK_IHL = 0xFFFF00F5
_VRD_BLOCK_EDIT4 = 0xFFFF00F7

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


class _Ptr:
    """Placeholder for a sub-IFD pointer resolved after the layout pass."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


Entry = list  # [tag, typ, count, bytes | _Ptr]


def _ifd_total(entries: Sequence[Entry]) -> int:
    """Serialized size of one IFD plus its word-aligned value pool."""
    total = 2 + 12 * len(entries) + 4
    for _tag, _typ, _count, val in entries:
        if isinstance(val, _Ptr):
            continue
        n = len(val)
        if n > 4:
            total += n + (n & 1)
    return total


def _ser_ifd(entries: Sequence[Entry], start: int, nxt: int, e: str,
             offsets: dict[str, int]) -> bytes:
    ents = sorted(entries, key=lambda x: x[0])
    n = len(ents)
    pool_base = start + 2 + 12 * n + 4
    head = bytearray(struct.pack(e + "H", n))
    pool = bytearray()
    for tag, typ, count, val in ents:
        if isinstance(val, _Ptr):
            field = struct.pack(e + "I", offsets[val.name])
        elif len(val) <= 4:
            field = bytes(val).ljust(4, b"\x00")
        else:
            field = struct.pack(e + "I", pool_base + len(pool))
            pool += val
            if len(pool) & 1:
                pool += b"\x00"
        head += struct.pack(e + "HHI", tag, typ, count) + field
    head += struct.pack(e + "I", nxt)
    return bytes(head) + bytes(pool)


def _mk(e: str):
    """Return small entry constructors bound to a byte order."""

    def asc(tag: int, s: str) -> Entry:
        b = s.encode("ascii", "replace") + b"\x00"
        return [tag, 2, len(b), b]

    def shorts(tag: int, *vals: int) -> Entry:
        return [tag, 3, len(vals), b"".join(struct.pack(e + "H", v) for v in vals)]

    def longs(tag: int, *vals: int) -> Entry:
        return [tag, 4, len(vals), b"".join(struct.pack(e + "I", v) for v in vals)]

    def rats(tag: int, *pairs: tuple[int, int]) -> Entry:
        return [tag, 5, len(pairs), b"".join(struct.pack(e + "II", a, b) for a, b in pairs)]

    def undef(tag: int, data: bytes) -> Entry:
        return [tag, 7, len(data), data]

    def byt(tag: int, data: bytes) -> Entry:
        return [tag, 1, len(data), data]

    return asc, shorts, longs, rats, undef, byt


def _vrd_trailer(kind: str, ihl_preview: bytes | None = None,
                 ihl_thumb: bytes | None = None) -> tuple[bytes, dict[str, int]]:
    """Build a real CANON OPTIONAL DATA trailer.

    Returns (trailer_bytes, {'ihl_preview': offset_within_trailer, ...}).
    """
    blocks = bytearray()
    marks: dict[str, int] = {}

    def block(btype: int, payload: bytes) -> None:
        blocks.extend(struct.pack(">II", btype, len(payload)))
        blocks.extend(payload)

    if kind == "dpp4":
        block(_VRD_BLOCK_EDIT4, b"DR4\x00" + bytes(60))
    elif kind == "dpp3":
        block(_VRD_BLOCK_EDIT, b"VRD\x00" + bytes(60))
    elif kind == "unknown":
        block(0xFFFF00FE, bytes(32))
    else:
        raise ValueError("неизвестный тип трейлера: %r" % kind)

    if ihl_preview is not None or ihl_thumb is not None:
        recs = bytearray()
        items = []
        if ihl_thumb is not None:
            items.append((3, ihl_thumb, "ihl_thumb"))
        if ihl_preview is not None:
            items.append((4, ihl_preview, "ihl_preview"))
        for i, (tag, payload, name) in enumerate(items):
            # +40 = size of THIS record's payload, +44 = size of the NEXT
            # record's payload (0 on the last one).  Do not write this record's
            # own size into +44: that hides a wrong stride in the reader.
            nxt = len(items[i + 1][1]) if i + 1 < len(items) else 0
            recs.extend(_IHL_SIG)
            recs.extend(b"\x00\x00\x00\x00")
            recs.extend(struct.pack("<III", tag, len(payload), nxt))
            marks[name] = 0x1C + 8 + len(blocks) + len(recs)   # patched below
            recs.extend(payload)
        block(_VRD_BLOCK_IHL, bytes(recs))

    size = len(blocks)
    header = _VRD_SIG + b"\x00\x01\x00\x00" + struct.pack(">I", size)
    assert len(header) == 0x1C, len(header)
    # ExifTool reads the payload size from the BIG-endian int32 at 0x14 of the
    # 0x40-byte footer and at 0x18 of the 0x1C-byte header; both must agree.
    footer = bytearray(_VRD_SIG + struct.pack(">I", size) + b"\x00\x00\x00\x00")
    footer.extend(bytes(0x40 - len(footer)))
    footer[-2:] = b"\xff\xd9"     # the decoy EOI real DPP trailers end with
    assert len(footer) == 0x40
    return bytes(header) + bytes(blocks) + bytes(footer), marks


def make_cr2(path: str | Path, *,
             byte_order: str = "II",
             orientation: int = 1,
             preview_size: tuple[int, int] | None = (1936, 1288),
             raw_size: tuple[int, int] = (1936, 1288),
             thumb_size: tuple[int, int] | None = (160, 120),
             preview_padding: int = 0,
             include_ifd2: bool = True,
             include_slice_tag: bool = True,
             slice_values: tuple[int, int, int] | None = None,
             make: str = "Canon",
             model: str = "Canon EOS 40D",
             software: str = "Firmware Version 1.1.1",
             artist: str = "Test Rig",
             datetime_original: str = "2016:07:14 11:22:33",
             lens: str = "EF50mm f/1.4 USM",
             iso: int = 400,
             makernote: str | None = "small",
             makernote_bytes: int = 120000,
             mn_vrd_offset: bool = False,
             mn_sensor_info: bool = False,
             gps: bool = False,
             vrd: str | None = None,
             vrd_ihl: bool = False,
             ihl_preview_size: tuple[int, int] = (480, 320),
             raw_ncomp: int = 0,
             raw_vsf: int = 1,
             user_comment: bytes | None = None,
             truncate: float | int | None = None) -> Path:
    """Write one synthetic CR2 fixture and return its path.

    Args:
        path: destination file.
        byte_order: 'II' (little) or 'MM' (big).
        orientation: EXIF Orientation written into IFD0 (1..8).
        preview_size: (w, h) of the IFD0 full-size preview, or None for a file
            with no IFD0 preview at all.
        raw_size: (w, h) of the simulated sensor array, encoded in IFD3's SOF3.
        thumb_size: (w, h) of the IFD1 thumbnail, or None to omit it.
        preview_padding: NUL bytes appended after the preview's EOI and counted
            in StripByteCounts, the way several writers pad the slot.
        include_ifd2: emit an IFD2 holding an uncompressed (non-JPEG) image.
        include_slice_tag: emit 0xC640 (CR2 slice info) in IFD3.
        slice_values: explicit (nSlices-1, width_each, width_last) for 0xC640;
            defaults to a single slice consistent with raw_size.
        makernote: None, 'small', or 'huge' (a MakerNote far past the APP1 cap).
        makernote_bytes: payload size used by makernote='huge'.
        mn_vrd_offset: write MakerNote 0x00D0 VRDOffset (recipe hint, no trailer).
        mn_sensor_info: write MakerNote 0x00E0 SensorInfo matching raw_size.
        gps: emit a GPS IFD.
        vrd: None | 'dpp3' | 'dpp4' | 'unknown' - append a DPP recipe trailer.
        vrd_ihl: add an IHLData block carrying DPP's own preview/thumbnail.
        user_comment: bytes for EXIF UserComment (0x9286).
        truncate: float in (0, 1) -> keep that fraction of the file; int -> keep
            that many bytes.  Produces a deliberately damaged fixture.

    Returns:
        The path written.
    """
    path = Path(path)
    if byte_order not in ("II", "MM"):
        raise ValueError("byte_order должен быть 'II' или 'MM'")
    e = "<" if byte_order == "II" else ">"
    asc, shorts, longs, rats, undef, byt = _mk(e)

    # ---- binary blobs first, so every offset is known up front -----------
    body = bytearray(b"\x00" * 16)          # TIFF header placeholder

    def add(blob: bytes) -> int:
        if len(body) & 1:
            body.append(0)
        off = len(body)
        body.extend(blob)
        return off

    preview_off = preview_len = 0
    if preview_size:
        pv = baseline_jpeg(preview_size[0], preview_size[1], comment=b"synthetic preview")
        pv += b"\x00" * max(0, preview_padding)
        preview_off, preview_len = add(pv), len(pv)
    thumb_off = thumb_len = 0
    if thumb_size:
        tb = baseline_jpeg(thumb_size[0], thumb_size[1])
        thumb_off, thumb_len = add(tb), len(tb)
    raw_blob = lossless_raw_blob(raw_size[0], raw_size[1],
                                 *( (raw_ncomp, raw_vsf) if raw_ncomp else () ))
    raw_off, raw_len = add(raw_blob), len(raw_blob)
    ifd2_off_blob = ifd2_len = 0
    if include_ifd2:
        rgb = bytes(range(256)) * 3          # plainly not a JPEG
        ifd2_off_blob, ifd2_len = add(rgb), len(rgb)

    # ---- IFD entry lists --------------------------------------------------
    pw, ph = preview_size if preview_size else (0, 0)

    ifd0: list[Entry] = [
        asc(T_MAKE, make),
        asc(T_MODEL, model),
        asc(T_SOFTWARE, software),
        asc(T_DATETIME, datetime_original),
        asc(T_ARTIST, artist),
        asc(T_COPYRIGHT, "(c) test"),
        shorts(T_ORIENTATION, orientation),
        rats(T_XRES, (72, 1)),
        rats(T_YRES, (72, 1)),
        shorts(T_RES_UNIT, 2),
        [T_EXIF_IFD, 4, 1, _Ptr("exif")],
    ]
    if preview_size:
        ifd0 += [
            longs(T_IMAGE_WIDTH, pw),
            longs(T_IMAGE_LENGTH, ph),
            shorts(T_BITS_PER_SAMPLE, 8, 8, 8),
            shorts(T_COMPRESSION, 6),
            shorts(T_PHOTOMETRIC, 6),
            longs(T_STRIP_OFFSETS, preview_off),
            shorts(T_SAMPLES_PER_PIXEL, 3),
            longs(T_ROWS_PER_STRIP, ph),
            longs(T_STRIP_BYTE_COUNTS, preview_len),
            shorts(T_PLANAR, 1),
        ]
    if gps:
        ifd0.append([T_GPS_IFD, 4, 1, _Ptr("gps")])

    exif: list[Entry] = [
        undef(T_EXIF_VERSION, b"0221"),
        rats(T_EXPOSURE_TIME, (1, 125)),
        rats(T_FNUMBER, (56, 10)),
        shorts(T_ISO, iso),
        asc(T_DATETIME_ORIGINAL, datetime_original),
        asc(T_CREATE_DATE, datetime_original),
        rats(T_FOCAL_LENGTH, (50, 1)),
        asc(T_LENS_MODEL, lens),
        # Deliberately WRONG: cr2_core must overwrite these with the real dims.
        longs(T_EXIF_IMAGE_W, 1),
        longs(T_EXIF_IMAGE_H, 1),
    ]
    if user_comment:
        exif.append(undef(T_USER_COMMENT, user_comment))

    mn: list[Entry] = []
    if makernote:
        mn = [
            shorts(0x0001, 46, 2, 0, 0, 0, 1, 0, 0),      # CanonCameraSettings
            asc(0x0006, "IMG:EOS 40D JPEG"),               # ImageType
            asc(0x0007, software),                         # FirmwareVersion
            longs(0x0008, 1234),                           # FileNumber
            asc(0x0009, "Test Owner"),                     # OwnerName
        ]
        if makernote == "huge":
            n = max(2, makernote_bytes // 2)
            mn.append([0x4001, 3, n, bytes(2 * n)])        # ColorData-like blob
        if mn_vrd_offset:
            mn.append(longs(MN_VRD_OFFSET, 0x1000))
        if mn_sensor_info:
            vals = [34, raw_size[0] + 64, raw_size[1] + 32, 1, 1,
                    32, 16, 32 + raw_size[0] - 1, 16 + raw_size[1] - 1]
            mn.append([MN_SENSOR_INFO, 3, len(vals),
                       b"".join(struct.pack(e + "H", v) for v in vals)])
        mn_total = _ifd_total(mn)
        exif.append([T_MAKERNOTE, 7, mn_total, _Ptr("makernote")])

    gps_ifd: list[Entry] = []
    if gps:
        gps_ifd = [
            byt(0x0000, bytes([2, 3, 0, 0])),                     # GPSVersionID
            asc(0x0001, "N"),
            rats(0x0002, (55, 1), (45, 1), (2136, 100)),
            asc(0x0003, "E"),
            rats(0x0004, (37, 1), (37, 1), (200, 100)),
            byt(0x0005, bytes([0])),
            rats(0x0006, (156, 1)),
            asc(0x0012, "WGS-84"),
        ]

    ifd1: list[Entry] = [shorts(T_COMPRESSION, 6), rats(T_XRES, (72, 1)),
                         rats(T_YRES, (72, 1)), shorts(T_RES_UNIT, 2)]
    if thumb_size:
        ifd1 += [longs(T_THUMB_OFFSET, thumb_off), longs(T_THUMB_LENGTH, thumb_len)]

    ifd2: list[Entry] = []
    if include_ifd2:
        ifd2 = [
            longs(T_IMAGE_WIDTH, 256), longs(T_IMAGE_LENGTH, 1),
            shorts(T_BITS_PER_SAMPLE, 8, 8, 8),
            shorts(T_COMPRESSION, 1),                 # uncompressed RGB
            shorts(T_PHOTOMETRIC, 2),
            longs(T_STRIP_OFFSETS, ifd2_off_blob),
            shorts(T_SAMPLES_PER_PIXEL, 3),
            longs(T_ROWS_PER_STRIP, 1),
            longs(T_STRIP_BYTE_COUNTS, ifd2_len),
        ]

    ifd3: list[Entry] = [
        shorts(T_COMPRESSION, 6),
        longs(T_STRIP_OFFSETS, raw_off),
        longs(T_STRIP_BYTE_COUNTS, raw_len),
    ]
    if include_slice_tag:
        sv = slice_values if slice_values else (0, raw_size[0], raw_size[0])
        ifd3.append(shorts(T_CR2_SLICE, *sv))

    # ---- layout pass ------------------------------------------------------
    order: list[tuple[str, list[Entry]]] = [("ifd0", ifd0), ("exif", exif)]
    if gps_ifd:
        order.append(("gps", gps_ifd))
    if mn:
        order.append(("makernote", mn))
    order.append(("ifd1", ifd1))
    if ifd2:
        order.append(("ifd2", ifd2))
    order.append(("ifd3", ifd3))

    offsets: dict[str, int] = {}
    cur = len(body) + (len(body) & 1)
    for name, ents in order:
        offsets[name] = cur
        cur += _ifd_total(ents)
        cur += cur & 1

    chain = {"ifd0": "ifd1", "ifd1": "ifd2" if ifd2 else "ifd3", "ifd2": "ifd3"}
    for name, ents in order:
        if len(body) & 1:
            body.append(0)
        assert len(body) == offsets[name], (name, len(body), offsets[name])
        nxt = offsets.get(chain.get(name, ""), 0)
        body.extend(_ser_ifd(ents, offsets[name], nxt, e, offsets))

    # ---- TIFF header ------------------------------------------------------
    body[0:2] = byte_order.encode("ascii")
    body[2:4] = struct.pack(e + "H", 42)
    body[4:8] = struct.pack(e + "I", offsets["ifd0"])
    body[8:12] = b"CR\x02\x00"
    body[12:16] = struct.pack(e + "I", offsets["ifd3"])

    # ---- DPP recipe trailer ----------------------------------------------
    if vrd:
        ihl_prev = baseline_jpeg(*ihl_preview_size) if vrd_ihl else None
        ihl_thumb = baseline_jpeg(160, 120) if vrd_ihl else None
        trailer, _marks = _vrd_trailer(vrd, ihl_prev, ihl_thumb)
        body.extend(trailer)

    data = bytes(body)
    if truncate is not None:
        keep = int(len(data) * truncate) if isinstance(truncate, float) else int(truncate)
        data = data[:max(0, keep)]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ==========================================================================
# Fixture set
# ==========================================================================

#: name -> kwargs for make_cr2.  Names are used as <name>.CR2.
FIXTURES: dict[str, dict[str, Any]] = {
    "normal": {},
    "big_endian": {"byte_order": "MM"},
    "rotated": {"orientation": 6},
    "rotated_baked": {"orientation": 6, "preview_size": (1288, 1936)},
    "tiny_preview": {"preview_size": (320, 212)},
    "no_preview": {"preview_size": None, "thumb_size": None},
    "truncated": {"truncate": 0.45},
    "crop": {"preview_size": (1440, 1288)},
    "padded_preview": {"preview_padding": 12},
    "dpp4": {"vrd": "dpp4"},
    "dpp3_ihl": {"vrd": "dpp3", "vrd_ihl": True},
    # IFD0 preview SMALLER than DPP's own IHL preview: the only layout that can
    # tell "we prefer documented sources" apart from "the biggest one wins".
    "dpp3_ihl_small_preview": {"vrd": "dpp3", "vrd_ihl": True,
                               "preview_size": (320, 212)},
    # Padding past the old 32-byte EOI search window.
    "padded_preview_big": {"preview_padding": 600},
    "dpp_tag_only": {"mn_vrd_offset": True},
    "dpp_software": {"software": "Digital Photo Professional 3.14.41.0"},
    "gps": {"gps": True},
    "huge_makernote": {"makernote": "huge"},
    "sensor_info": {"mn_sensor_info": True},
    # mRAW/sRAW-style IFD3: 3 components with subsampling, plus a SensorInfo
    # that still describes the FULL sensor.
    "sraw": {"raw_ncomp": 3, "raw_vsf": 2, "raw_size": (1932, 1288),
             "mn_sensor_info": True, "preview_size": (1932, 644)},
    "no_makernote": {"makernote": None},
    # --- the two real-world body shapes, measured ---------------------------
    # eos550d: the user's own camera.  All 155 of their files were probed with
    # cr2_core.probe: every one is a Canon EOS 550D whose IFD0 preview is
    # 5184x3456 - exactly the raw dimensions - and not one contains a DPP
    # recipe.  Scaled down here, the proportions are what matter.
    "eos550d": {"model": "Canon EOS 550D",
                "preview_size": (1936, 1288), "raw_size": (1936, 1288)},
    # halfsize_body: a 30D/40D/450D-era file, where the embedded preview really
    # is half the frame.  This is the case the README warns about, and the case
    # the min_preview_ratio warning exists for.
    "halfsize_body": {"model": "Canon EOS 40D",
                      "preview_size": (968, 644), "raw_size": (1936, 1288)},
}


def write_fixture_set(directory: str | Path) -> dict[str, Path]:
    """Write every fixture in FIXTURES into `directory`; return {name: path}."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, kw in FIXTURES.items():
        out[name] = make_cr2(d / (name + ".CR2"), **kw)
    return out


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print("usage: python make_test_cr2.py <directory>", file=sys.stderr)
        return 2
    paths = write_fixture_set(argv[1])
    for name, p in sorted(paths.items()):
        print("%-16s %9d  %s" % (name, p.stat().st_size, p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
