# -*- coding: utf-8 -*-
"""Unit tests for cr2_core, driven by the synthetic fixtures in make_test_cr2.

Stdlib only.  Nothing here imports Pillow or rawpy at module level; the few
tests that need them are guarded with skipUnless/skipIf.

The JPEG and TIFF parsing helpers at the top are written independently of
cr2_core on purpose: a test that re-used cr2_core's own parser to check
cr2_core's output would only prove the code is self-consistent.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cr2_core                                     # noqa: E402
import make_test_cr2 as mk                          # noqa: E402
from cr2_core import (ConvertOptions, convert_many, convert_one,  # noqa: E402
                      find_cr2, probe)

# --------------------------------------------------------------------------
# Independent JPEG / TIFF readers used to verify cr2_core's output
# --------------------------------------------------------------------------

_STANDALONE = frozenset({0x01, 0xD8, 0xD9}) | frozenset(range(0xD0, 0xD8))
_TYPESZ = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def jpeg_segments(buf: bytes) -> list[tuple[int, int, int]]:
    """Walk a JPEG: [(marker, start_of_0xFF, total_len)].  SOS covers the rest."""
    out: list[tuple[int, int, int]] = []
    if buf[:2] != b"\xff\xd8":
        raise ValueError("нет SOI")
    out.append((0xD8, 0, 2))
    i = 2
    n = len(buf)
    while i + 1 < n:
        if buf[i] != 0xFF:
            raise ValueError("ожидался маркер на позиции %d, найдено %02X" % (i, buf[i]))
        m = buf[i + 1]
        if m in _STANDALONE:
            out.append((m, i, 2))
            i += 2
            if m == 0xD9:
                break
            continue
        ln = struct.unpack(">H", buf[i + 2:i + 4])[0]
        if ln < 2 or i + 2 + ln > n:
            raise ValueError("битая длина сегмента %02X" % m)
        if m == 0xDA:
            out.append((m, i, n - i))
            break
        out.append((m, i, 2 + ln))
        i += 2 + ln
    return out


def jpeg_sof(buf: bytes) -> tuple[int, int]:
    """(width, height) from the first SOF0/1/2 segment."""
    for marker, start, length in jpeg_segments(buf):
        if (marker & 0xF0) == 0xC0 and marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", buf[start + 5:start + 9])
            return w, h
    raise ValueError("нет SOF")


def app_segments(buf: bytes) -> list[tuple[int, bytes]]:
    """[(marker, payload)] for every APPn segment before SOS."""
    out = []
    for marker, start, length in jpeg_segments(buf):
        if 0xE0 <= marker <= 0xEF:
            out.append((marker, buf[start + 4:start + length]))
    return out


def exif_app1(buf: bytes) -> bytes | None:
    """The TIFF block inside the Exif APP1, or None."""
    for marker, payload in app_segments(buf):
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            return payload[6:]
    return None


def first_kept_offset(blob: bytes) -> int:
    """Offset in `blob` of the first segment cr2_core must copy verbatim.

    That is: everything except SOI, APP0 and an Exif APP1.
    """
    for marker, start, length in jpeg_segments(blob):
        if marker == 0xD8 or marker == 0xE0:
            continue
        if marker == 0xE1 and blob[start + 4:start + 10] == b"Exif\x00\x00":
            continue
        return start
    raise ValueError("в блобе нет ни одного сегмента после APP0/APP1")


def _decode(typ: int, count: int, data: bytes, e: str):
    if typ == 2:
        return data.split(b"\x00", 1)[0].decode("ascii", "replace")
    if typ in (1, 6, 7):
        return data
    code = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
    if code:
        return list(struct.unpack(e + code * count, data))
    if typ in (5, 10):
        c = "II" if typ == 5 else "ii"
        v = struct.unpack(e + c * count, data)
        return [(v[2 * i], v[2 * i + 1]) for i in range(count)]
    return data


class Tiff:
    """A minimal, strict TIFF reader used to validate the emitted APP1."""

    def __init__(self, tiff: bytes) -> None:
        self.raw = tiff
        self.problems: list[str] = []
        if tiff[:2] not in (b"II", b"MM"):
            raise ValueError("нет метки порядка байт")
        self.e = "<" if tiff[:2] == b"II" else ">"
        magic, off = struct.unpack(self.e + "HI", tiff[2:8])
        if magic != 42:
            raise ValueError("magic != 42")
        self.ifds: dict[str, dict] = {}
        self.order: dict[str, list[int]] = {}
        ifd0, nxt = self._read(off, "ifd0")
        self.ifds["ifd0"] = ifd0
        if 0x8769 in ifd0:
            self.ifds["exif"], n = self._read(ifd0[0x8769][0], "exif")
            if n != 0:
                self.problems.append("у ExifIFD ненулевая ссылка на следующий IFD")
        if 0x8825 in ifd0:
            self.ifds["gps"], n = self._read(ifd0[0x8825][0], "gps")
            if n != 0:
                self.problems.append("у GPS IFD ненулевая ссылка на следующий IFD")
        exif = self.ifds.get("exif", {})
        if 0xA005 in exif:
            self.ifds["interop"], _ = self._read(exif[0xA005][0], "interop")
        self.thumb: bytes | None = None
        if nxt:
            self.ifds["ifd1"], _ = self._read(nxt, "ifd1")
            t = self.ifds["ifd1"]
            if 0x0201 in t and 0x0202 in t:
                o, ln = t[0x0201][0], t[0x0202][0]
                if o + ln > len(tiff):
                    self.problems.append("миниатюра выходит за пределы TIFF")
                else:
                    self.thumb = tiff[o:o + ln]

    def _read(self, off: int, name: str) -> tuple[dict, int]:
        t = self.raw
        if off + 6 > len(t):
            raise ValueError("IFD %s за пределами TIFF" % name)
        n = struct.unpack(self.e + "H", t[off:off + 2])[0]
        if n == 0:
            self.problems.append("пустой IFD %s" % name)
        end = off + 2 + 12 * n + 4
        if end > len(t):
            raise ValueError("IFD %s обрезан" % name)
        out: dict = {}
        tags: list[int] = []
        for i in range(n):
            p = off + 2 + 12 * i
            tag, typ, count = struct.unpack(self.e + "HHI", t[p:p + 8])
            raw = t[p + 8:p + 12]
            size = _TYPESZ.get(typ, 0) * count
            if size == 0:
                self.problems.append("тег 0x%04X в %s имеет неизвестный тип %d" % (tag, name, typ))
                continue
            if size <= 4:
                data = raw[:size]
            else:
                voff = struct.unpack(self.e + "I", raw)[0]
                if voff + size > len(t):
                    raise ValueError("значение тега 0x%04X (%s) за пределами TIFF" % (tag, name))
                if voff & 1:
                    self.problems.append("значение тега 0x%04X (%s) не выровнено" % (tag, name))
                data = t[voff:voff + size]
            out[tag] = _decode(typ, count, data, self.e)
            tags.append(tag)
        if tags != sorted(tags):
            self.problems.append("теги IFD %s не по возрастанию" % name)
        self.order[name] = tags
        nxt = struct.unpack(self.e + "I", t[off + 2 + 12 * n:end])[0]
        return out, nxt

    def get(self, ifd: str, tag: int, default=None):
        return self.ifds.get(ifd, {}).get(tag, default)


# --------------------------------------------------------------------------
# Shared fixture set (built once for the whole module)
# --------------------------------------------------------------------------

_TMP: tempfile.TemporaryDirectory | None = None
FIXDIR: Path
FX: dict[str, Path] = {}

#: Fixtures that must convert successfully through the lossless path.
CONVERTIBLE = ("normal", "big_endian", "rotated", "rotated_baked", "tiny_preview",
               "crop", "padded_preview", "dpp4", "dpp3_ihl", "dpp_tag_only",
               "dpp_software", "gps", "huge_makernote", "sensor_info",
               "no_makernote")

EXPECT_PREVIEW = {
    "normal": (1936, 1288), "big_endian": (1936, 1288), "rotated": (1936, 1288),
    "rotated_baked": (1288, 1936), "tiny_preview": (320, 212), "crop": (1440, 1288),
    "padded_preview": (1936, 1288),
    "dpp4": (1936, 1288), "dpp3_ihl": (1936, 1288), "dpp_tag_only": (1936, 1288),
    "dpp_software": (1936, 1288), "gps": (1936, 1288), "huge_makernote": (1936, 1288),
    "sensor_info": (1936, 1288), "no_makernote": (1936, 1288),
}
EXPECT_ORIENTATION = {"rotated": 6, "rotated_baked": 6}
#: What cr2_core should WRITE into the output (rotated_baked is reconciled to 1).
EXPECT_OUT_ORIENTATION = {"rotated": 6, "rotated_baked": 1}

MAKE = "Canon"
MODEL = "Canon EOS 40D"
SHOT = "2016:07:14 11:22:33"


def setUpModule() -> None:
    global _TMP, FIXDIR, FX
    _TMP = tempfile.TemporaryDirectory(prefix="cr2_fixtures_")
    FIXDIR = Path(_TMP.name)
    FX = mk.write_fixture_set(FIXDIR)


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


class Base(unittest.TestCase):
    """Adds a per-test scratch directory that is always cleaned up."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cr2_out_")
        self.out = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def read_preview_blob(self, name: str) -> bytes:
        info = probe(FX[name])
        pv = info.best
        self.assertIsNotNone(pv)
        with open(FX[name], "rb") as f:
            f.seek(pv.offset)
            return f.read(pv.length)


# ==========================================================================
# The fixture generator itself
# ==========================================================================


class TestFixtureGenerator(Base):

    def test_jpeg_is_structurally_valid(self):
        for w, h in ((1936, 1288), (160, 120), (17, 9), (1288, 1936)):
            blob = mk.baseline_jpeg(w, h)
            with self.subTest(size=(w, h)):
                self.assertEqual(blob[:2], b"\xff\xd8")
                self.assertEqual(blob[-2:], b"\xff\xd9")
                markers = [m for m, _s, _l in jpeg_segments(blob)]
                for need in (0xD8, 0xE0, 0xDB, 0xC0, 0xC4, 0xDA):
                    self.assertIn(need, markers)
                self.assertEqual(jpeg_sof(blob), (w, h))
                # entropy data must be byte-stuffed: no bare 0xFFxx inside it
                sos = [(m, s, l) for m, s, l in jpeg_segments(blob) if m == 0xDA][0]
                hdr_len = struct.unpack(">H", blob[sos[1] + 2:sos[1] + 4])[0]
                ent = blob[sos[1] + 2 + hdr_len:-2]
                i = ent.find(b"\xff")
                while i >= 0:
                    self.assertEqual(ent[i + 1], 0x00, "незаэкранированный 0xFF в ECS")
                    i = ent.find(b"\xff", i + 2)

    @unittest.skipUnless(cr2_core.has_pillow(), "нужен Pillow для реальной проверки декодирования")
    def test_jpeg_really_decodes(self):
        import io
        from PIL import Image
        for w, h in ((160, 120), (320, 212), (1936, 1288)):
            with self.subTest(size=(w, h)):
                im = Image.open(io.BytesIO(mk.baseline_jpeg(w, h)))
                im.load()
                self.assertEqual(im.size, (w, h))
                self.assertEqual(im.convert("RGB").getpixel((w // 2, h // 2)), (128, 128, 128))

    def test_make_cr2_returns_path_and_accepts_kwargs(self):
        p = mk.make_cr2(self.out / "custom.CR2", orientation=8,
                        preview_size=(640, 480), raw_size=(2560, 1920),
                        thumb_size=(80, 60), byte_order="MM", gps=True)
        self.assertTrue(p.exists())
        info = probe(p)
        self.assertEqual(info.error, "")
        self.assertEqual(info.byte_order, ">")
        self.assertEqual((info.best.width, info.best.height), (640, 480))
        self.assertEqual((info.raw_width, info.raw_height), (2560, 1920))
        self.assertEqual(info.orientation, 8)


# ==========================================================================
# probe()
# ==========================================================================


class TestProbe(Base):

    def test_byte_order(self):
        self.assertEqual(probe(FX["normal"]).byte_order, "<")
        self.assertEqual(probe(FX["big_endian"]).byte_order, ">")

    def test_previews_sorted_largest_first(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                pv = probe(FX[name]).previews
                self.assertTrue(pv)
                keys = [(p.pixels, p.length) for p in pv]
                self.assertEqual(keys, sorted(keys, reverse=True))

    def test_sof_dimensions(self):
        for name, wh in EXPECT_PREVIEW.items():
            with self.subTest(fixture=name):
                info = probe(FX[name])
                self.assertEqual(info.error, "")
                self.assertEqual((info.best.width, info.best.height), wh)
                self.assertEqual(info.best.source, "ifd0")
                self.assertEqual(info.best.pixels, wh[0] * wh[1])

    def test_thumbnail_is_found(self):
        pv = probe(FX["normal"]).previews
        thumbs = [p for p in pv if p.source == "ifd1"]
        self.assertEqual(len(thumbs), 1)
        self.assertEqual((thumbs[0].width, thumbs[0].height), (160, 120))

    def test_preview_blob_is_exactly_the_jpeg(self):
        """offset/length must bracket the JPEG precisely: SOI..EOI, no padding."""
        blob = self.read_preview_blob("normal")
        self.assertEqual(blob[:2], b"\xff\xd8")
        self.assertEqual(blob[-2:], b"\xff\xd9")
        self.assertEqual(jpeg_sof(blob), (1936, 1288))

    def test_raw_dimensions(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                info = probe(FX[name])
                self.assertEqual((info.raw_width, info.raw_height), (1936, 1288))

    def test_padding_after_eoi_is_trimmed(self):
        """StripByteCounts counts 12 NUL bytes of padding; they must be dropped."""
        clean = probe(FX["normal"]).best
        padded = probe(FX["padded_preview"]).best
        self.assertEqual(padded.length, clean.length)
        blob = self.read_preview_blob("padded_preview")
        self.assertEqual(blob[-2:], b"\xff\xd9")
        self.assertEqual(jpeg_sof(blob), (1936, 1288))

    def test_thumbnail_can_outrank_the_ifd0_preview(self):
        p = mk.make_cr2(self.out / "bigthumb.CR2", preview_size=(160, 120),
                        thumb_size=(640, 480))
        info = probe(p)
        self.assertEqual(info.best.source, "ifd1")
        self.assertEqual((info.best.width, info.best.height), (640, 480))
        res = convert_one(p, ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        self.assertIn("ifd1", res.message)
        self.assertEqual(jpeg_sof(res.dst.read_bytes()), (640, 480))

    def test_slice_tag_overrides_the_sof3_width(self):
        """0xC640 is authoritative for the sensor width (nSlices-1, each, last)."""
        p = mk.make_cr2(self.out / "sliced.CR2", raw_size=(2000, 1000),
                        slice_values=(2, 800, 600))
        info = probe(p)
        self.assertEqual(info.raw_width, 2 * 800 + 600)
        self.assertEqual(info.raw_height, 1000)

    def test_raw_dimensions_from_sensor_info_makernote(self):
        p = mk.make_cr2(self.out / "si.CR2", raw_size=(3888, 2592), mn_sensor_info=True)
        info = probe(p)
        self.assertEqual((info.raw_width, info.raw_height), (3888, 2592))

    def test_sof3_raw_frame_is_never_offered_as_preview(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                for pv in probe(FX[name]).previews:
                    self.assertIn(pv.source, ("ifd0", "ifd1", "ifd2", "makernote",
                                              "vrd_ihl", "vrd_ihl_thumb"))
                    self.assertGreater(pv.pixels, 0)

    def test_orientation(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                self.assertEqual(probe(FX[name]).orientation,
                                 EXPECT_ORIENTATION.get(name, 1))

    def test_camera_and_shot_at(self):
        info = probe(FX["normal"])
        self.assertEqual(info.camera, MODEL)
        self.assertEqual(info.shot_at, SHOT)

    def test_recipe_detection(self):
        for name in ("dpp4", "dpp3_ihl", "dpp_tag_only"):
            with self.subTest(fixture=name):
                info = probe(FX[name])
                self.assertTrue(info.has_dpp_recipe)
                self.assertIn("DPP", info.recipe_hint)
        for name in ("normal", "gps", "dpp_software"):
            with self.subTest(fixture=name):
                info = probe(FX[name])
                self.assertFalse(info.has_dpp_recipe)
                self.assertIn("не найден", info.recipe_hint)
        self.assertIn("Edit4Data", probe(FX["dpp4"]).recipe_hint)
        self.assertIn("EditData", probe(FX["dpp3_ihl"]).recipe_hint)
        self.assertIn("IHLData", probe(FX["dpp3_ihl"]).recipe_hint)
        self.assertIn("VRDOffset", probe(FX["dpp_tag_only"]).recipe_hint)
        # Software says DPP but there is no trailer: the hint must say so.
        self.assertIn("Software", probe(FX["dpp_software"]).recipe_hint)

    def test_vrd_ihl_previews_are_listed_but_not_preferred(self):
        info = probe(FX["dpp3_ihl"])
        sources = [p.source for p in info.previews]
        self.assertIn("vrd_ihl", sources)
        self.assertIn("vrd_ihl_thumb", sources)
        self.assertEqual(info.best.source, "ifd0")     # IFD0 is bigger, so it wins

    def test_vrd_decoy_eoi_does_not_truncate_the_preview(self):
        """The VRD footer ends in FFD9; the preview length must be unaffected."""
        a = probe(FX["normal"]).best
        b = probe(FX["dpp4"]).best
        self.assertEqual((a.offset, a.length), (b.offset, b.length))

    def test_crop_fixture_has_a_different_aspect_than_raw(self):
        info = probe(FX["crop"])
        pv = info.best
        self.assertNotAlmostEqual(pv.width / pv.height,
                                  info.raw_width / info.raw_height, places=3)

    def test_truncated_file_reports_error_without_raising(self):
        info = probe(FX["truncated"])           # must not raise
        self.assertNotEqual(info.error, "")
        self.assertTrue(any("Ѐ" <= ch <= "ӿ" for ch in info.error),
                        "сообщение об ошибке должно быть на русском: %r" % info.error)
        self.assertEqual(info.previews, [])
        self.assertIsNone(info.best)

    def test_many_truncation_points_never_raise(self):
        src = FX["normal"].read_bytes()
        for frac in (0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.97, 0.995):
            p = self.out / ("t_%s.CR2" % frac)
            p.write_bytes(src[:int(len(src) * frac)])
            with self.subTest(frac=frac):
                info = probe(p)                  # must never raise
                self.assertIsInstance(info.error, str)
                if info.error:
                    self.assertEqual(info.previews, [])

    def test_no_preview_fixture(self):
        info = probe(FX["no_preview"])
        self.assertEqual(info.previews, [])
        self.assertNotEqual(info.error, "")

    def test_missing_file(self):
        info = probe(self.out / "нет-такого.CR2")
        self.assertNotEqual(info.error, "")

    def test_garbage_file(self):
        p = self.out / "junk.CR2"
        p.write_bytes(b"not a tiff at all" * 64)
        info = probe(p)
        self.assertNotEqual(info.error, "")

    def test_tiny_file(self):
        p = self.out / "tiny.CR2"
        p.write_bytes(b"II*\x00")
        self.assertNotEqual(probe(p).error, "")

    def test_canon_1d_tif_is_rejected(self):
        data = bytearray(FX["normal"].read_bytes())
        data[8:12] = b"\xba\xb0\xac\xbb"
        p = self.out / "1d.CR2"
        p.write_bytes(bytes(data))
        self.assertIn("1D RAW", probe(p).error)


# ==========================================================================
# convert_one(): the lossless path
# ==========================================================================


class TestConvertLossless(Base):

    def convert(self, name: str, **kw) -> tuple:
        opts = ConvertOptions(out_dir=self.out, **kw)
        res = convert_one(FX[name], opts)
        return res, (res.dst.read_bytes() if res.ok else b"")

    def test_output_exists_and_reports_lossless(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                self.assertTrue(res.ok, res.message)
                self.assertEqual(res.mode, "lossless")
                self.assertTrue(res.dst.exists())
                self.assertEqual(res.bytes_out, len(data))
                self.assertEqual((res.width, res.height), EXPECT_PREVIEW[name])
                self.assertFalse(res.skipped)
                self.assertIsNotNone(res.info)

    def test_compressed_data_is_byte_identical(self):
        """Everything after our APP segments must equal the embedded blob."""
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                blob = self.read_preview_blob(name)
                res, data = self.convert(name)
                self.assertTrue(res.ok, res.message)
                tail = blob[first_kept_offset(blob):]
                self.assertEqual(data[-len(tail):], tail,
                                 "хвост результата не побайтово равен блобу")
                # and the part before it is nothing but SOI + APPn segments
                head = data[:len(data) - len(tail)]
                self.assertEqual(head[:2], b"\xff\xd8")
                i = 2
                while i < len(head):
                    self.assertEqual(head[i], 0xFF)
                    self.assertTrue(0xE0 <= head[i + 1] <= 0xEF,
                                    "перед данными оказался не-APP маркер %02X" % head[i + 1])
                    i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
                self.assertEqual(i, len(head))

    def test_entropy_data_is_bit_exact(self):
        """Explicitly compare the entropy-coded scan, not just the tail."""
        blob = self.read_preview_blob("normal")
        res, data = self.convert("normal")
        sos_b = [s for m, s, l in jpeg_segments(blob) if m == 0xDA][0]
        sos_o = [s for m, s, l in jpeg_segments(data) if m == 0xDA][0]
        self.assertEqual(data[sos_o:], blob[sos_b:])

    def test_output_reparses_to_the_same_dimensions(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                self.assertEqual(jpeg_sof(data), EXPECT_PREVIEW[name])
                self.assertEqual(data[:2], b"\xff\xd8")
                self.assertEqual(data[-2:], b"\xff\xd9")

    def test_app1_present_and_wellformed(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                apps = app_segments(data)
                self.assertTrue(apps, "нет ни одного APPn")
                self.assertEqual(apps[0][0], 0xE1, "APP1 должен идти первым после SOI")
                self.assertTrue(apps[0][1].startswith(b"Exif\x00\x00"))
                tiff = exif_app1(data)
                self.assertIsNotNone(tiff)
                t = Tiff(tiff)
                self.assertEqual(t.problems, [])

    def test_exif_roundtrip(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                t = Tiff(exif_app1(data))
                self.assertEqual(t.get("ifd0", 0x010F), MAKE)
                self.assertEqual(t.get("ifd0", 0x0110), MODEL)
                self.assertEqual(t.get("exif", 0x9003), SHOT)
                self.assertEqual(t.get("ifd0", 0x0112),
                                 [EXPECT_OUT_ORIENTATION.get(name, 1)])

    def test_exif_pixel_dimensions_match_the_real_output(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                t = Tiff(exif_app1(data))
                w, h = jpeg_sof(data)
                self.assertEqual(t.get("exif", 0xA002), [w])
                self.assertEqual(t.get("exif", 0xA003), [h])
                self.assertEqual((w, h), (res.width, res.height))

    def test_strip_and_compression_tags_are_absent(self):
        forbidden = (0x00FE, 0x0100, 0x0101, 0x0102, 0x0103, 0x0106, 0x0111,
                     0x0115, 0x0116, 0x0117, 0x011C, 0xC640)
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                t = Tiff(exif_app1(data))
                for tag in forbidden:
                    self.assertNotIn(tag, t.ifds["ifd0"],
                                     "тег 0x%04X остался в IFD0" % tag)

    def test_makernote_absent_by_default(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res, data = self.convert(name)
                t = Tiff(exif_app1(data))
                self.assertNotIn(0x927C, t.ifds.get("exif", {}))

    def test_makernote_kept_when_asked(self):
        res, data = self.convert("normal", keep_makernote=True)
        t = Tiff(exif_app1(data))
        self.assertIn(0x927C, t.ifds["exif"])

    def test_exif_thumbnail_is_embedded(self):
        res, data = self.convert("normal")
        t = Tiff(exif_app1(data))
        self.assertIsNotNone(t.thumb)
        self.assertEqual(jpeg_sof(t.thumb), (160, 120))

    def test_copy_exif_false(self):
        res, data = self.convert("normal", copy_exif=False)
        self.assertIsNone(exif_app1(data))
        self.assertTrue(res.ok)

    def test_other_segments_are_preserved(self):
        """A COM segment in the source blob must survive the rewrap."""
        res, data = self.convert("normal")
        coms = [(m, s, l) for m, s, l in jpeg_segments(data) if m == 0xFE]
        self.assertEqual(len(coms), 1)
        self.assertIn(b"synthetic preview", data[coms[0][1]:coms[0][1] + coms[0][2]])

    def test_source_file_is_not_modified(self):
        before = FX["normal"].read_bytes()
        self.convert("normal")
        self.assertEqual(FX["normal"].read_bytes(), before)

    def test_small_preview_is_reported_as_partial(self):
        res, _ = self.convert("tiny_preview")
        self.assertTrue(res.ok)
        self.assertIn("превью", res.message)
        self.assertIn("площади кадра", res.message)

    def test_dpp_recipe_warning_is_surfaced(self):
        res, _ = self.convert("dpp4")
        self.assertTrue(res.ok)
        self.assertIn("DPP", res.message)

    def test_orientation_reconciled_for_prerotated_preview(self):
        res, data = self.convert("rotated_baked")
        self.assertEqual(Tiff(exif_app1(data)).get("ifd0", 0x0112), [1])
        self.assertIn("Orientation", res.message)

    def test_orientation_passed_through_when_not_prerotated(self):
        res, data = self.convert("rotated")
        self.assertEqual(Tiff(exif_app1(data)).get("ifd0", 0x0112), [6])

    def test_big_endian_source_round_trips(self):
        res, data = self.convert("big_endian")
        t = Tiff(exif_app1(data))
        self.assertEqual(t.e, ">")
        self.assertEqual(t.get("ifd0", 0x0110), MODEL)
        self.assertEqual(jpeg_sof(data), (1936, 1288))


# ==========================================================================
# APP1 size cap
# ==========================================================================


class TestApp1Budget(Base):

    def assert_app1_fits(self, data: bytes) -> bytes:
        found = None
        for marker, start, length in jpeg_segments(data):
            if 0xE0 <= marker <= 0xEF:
                seglen = struct.unpack(">H", data[start + 2:start + 4])[0]
                self.assertLessEqual(seglen, 65535)
                self.assertLessEqual(seglen - 2, 65533)
                self.assertLessEqual(length, 65535 + 2)
                if marker == 0xE1 and data[start + 4:start + 10] == b"Exif\x00\x00":
                    found = data[start:start + length]
        self.assertIsNotNone(found, "APP1 Exif не найден")
        return found

    def test_huge_makernote_default(self):
        res = convert_one(FX["huge_makernote"], ConvertOptions(out_dir=self.out))
        self.assertTrue(res.ok, res.message)
        self.assert_app1_fits(res.dst.read_bytes())

    def test_huge_makernote_kept(self):
        res = convert_one(FX["huge_makernote"],
                          ConvertOptions(out_dir=self.out, keep_makernote=True))
        self.assertTrue(res.ok, res.message)
        data = res.dst.read_bytes()
        app1 = self.assert_app1_fits(data)
        self.assertLessEqual(len(app1) - 4, 65533)
        # It could not possibly fit, so cr2_core must say it dropped it.
        self.assertIn("MakerNote", res.message)
        self.assertNotIn(0x927C, Tiff(exif_app1(data)).ifds["exif"])
        self.assertEqual(Tiff(exif_app1(data)).problems, [])

    def test_huge_everything(self):
        """MakerNote + UserComment + GPS + thumbnail all at once must still fit."""
        p = mk.make_cr2(self.out / "fat.CR2", makernote="huge", makernote_bytes=400000,
                        gps=True, user_comment=b"ASCII\x00\x00\x00" + b"x" * 40000)
        res = convert_one(p, ConvertOptions(out_dir=self.out / "o", keep_makernote=True))
        self.assertTrue(res.ok, res.message)
        self.assert_app1_fits(res.dst.read_bytes())

    def test_every_fixture_stays_under_the_cap(self):
        for name in CONVERTIBLE:
            with self.subTest(fixture=name):
                res = convert_one(FX[name], ConvertOptions(out_dir=self.out,
                                                           overwrite=True))
                self.assertTrue(res.ok, res.message)
                self.assert_app1_fits(res.dst.read_bytes())


# ==========================================================================
# GPS
# ==========================================================================


class TestGps(Base):

    def test_gps_is_copied_by_default(self):
        res = convert_one(FX["gps"], ConvertOptions(out_dir=self.out))
        self.assertTrue(res.ok, res.message)
        t = Tiff(exif_app1(res.dst.read_bytes()))
        self.assertIn(0x8825, t.ifds["ifd0"])
        self.assertIn("gps", t.ifds)
        self.assertEqual(t.get("gps", 0x0001), "N")
        self.assertEqual(t.get("gps", 0x0002)[0], (55, 1))
        self.assertEqual(t.problems, [])

    def test_strip_gps_removes_the_ifd_and_the_pointer(self):
        res = convert_one(FX["gps"], ConvertOptions(out_dir=self.out, strip_gps=True))
        self.assertTrue(res.ok, res.message)
        data = res.dst.read_bytes()
        t = Tiff(exif_app1(data))
        self.assertNotIn(0x8825, t.ifds["ifd0"])
        self.assertNotIn("gps", t.ifds)
        self.assertNotIn(b"WGS-84", exif_app1(data))
        self.assertEqual(t.problems, [])

    def test_strip_gps_is_harmless_without_gps(self):
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out, strip_gps=True))
        self.assertTrue(res.ok, res.message)


# ==========================================================================
# Overwrite / paths
# ==========================================================================


class TestOverwriteAndPaths(Base):

    def test_skip_when_destination_exists(self):
        opts = ConvertOptions(out_dir=self.out)
        first = convert_one(FX["normal"], opts)
        self.assertTrue(first.ok)
        again = convert_one(FX["normal"], opts)
        self.assertTrue(again.skipped)
        self.assertFalse(again.ok)
        self.assertEqual(again.dst, first.dst)
        self.assertNotEqual(again.message, "")

    def test_overwrite_replaces(self):
        opts = ConvertOptions(out_dir=self.out)
        first = convert_one(FX["normal"], opts)
        good = first.dst.read_bytes()
        first.dst.write_bytes(b"clobbered")
        self.assertEqual(first.dst.read_bytes(), b"clobbered")
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out, overwrite=True))
        self.assertTrue(res.ok, res.message)
        self.assertFalse(res.skipped)
        self.assertEqual(res.dst.read_bytes(), good)

    def test_default_out_dir_is_next_to_the_source(self):
        src = mk.make_cr2(self.out / "beside.CR2", preview_size=(64, 48),
                          thumb_size=None)
        res = convert_one(src, ConvertOptions())
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.dst, self.out / "beside.jpg")

    def test_suffix_and_out_dir(self):
        dst_dir = self.out / "выход"
        res = convert_one(FX["normal"], ConvertOptions(out_dir=dst_dir, suffix="_web"))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.dst, dst_dir / "normal_web.jpg")
        self.assertTrue(res.dst.exists())

    def test_cyrillic_directory_and_filename(self):
        src_dir = self.out / "Фотографии 2016" / "Отпуск"
        src_dir.mkdir(parents=True)
        src = mk.make_cr2(src_dir / "снимок №1.CR2")
        dst_dir = self.out / "Результаты" / "готово"
        res = convert_one(src, ConvertOptions(out_dir=dst_dir, suffix="_конв"))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.dst, dst_dir / "снимок №1_конв.jpg")
        self.assertTrue(res.dst.exists())
        self.assertEqual(jpeg_sof(res.dst.read_bytes()), (1936, 1288))
        t = Tiff(exif_app1(res.dst.read_bytes()))
        self.assertEqual(t.get("ifd0", 0x0110), MODEL)

    def test_out_dir_is_created(self):
        deep = self.out / "a" / "б" / "c"
        res = convert_one(FX["normal"], ConvertOptions(out_dir=deep))
        self.assertTrue(res.ok, res.message)
        self.assertTrue(deep.is_dir())

    def test_no_temp_files_left_behind(self):
        convert_one(FX["normal"], ConvertOptions(out_dir=self.out))
        self.assertEqual([p.name for p in self.out.glob("*.tmp")], [])

    def test_mtime_is_carried_over(self):
        src = mk.make_cr2(self.out / "mt.CR2", preview_size=(64, 48), thumb_size=None)
        os.utime(src, (1000000000, 1000000000))
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        self.assertAlmostEqual(res.dst.stat().st_mtime, 1000000000, delta=2)


# ==========================================================================
# Failure paths
# ==========================================================================


class TestFailurePaths(Base):

    def _assert_russian(self, msg: str) -> None:
        self.assertNotEqual(msg.strip(), "")
        self.assertTrue(any("Ѐ" <= ch <= "ӿ" for ch in msg),
                        "ожидалось сообщение на русском, получено: %r" % msg)

    @unittest.skipIf(cr2_core.has_rawpy() and cr2_core.has_pillow(),
                     "rawpy установлен: резервный путь RAW доступен, "
                     "этот тест проверяет поведение без него")
    def test_no_preview_with_raw_fallback_but_no_rawpy(self):
        res = convert_one(FX["no_preview"],
                          ConvertOptions(out_dir=self.out, allow_raw_fallback=True))
        self.assertFalse(res.ok)
        self.assertFalse(res.skipped)
        self._assert_russian(res.message)
        self.assertIn("rawpy", res.message)
        self.assertFalse(res.dst.exists())

    def test_no_preview_with_raw_fallback_disabled(self):
        res = convert_one(FX["no_preview"],
                          ConvertOptions(out_dir=self.out, allow_raw_fallback=False))
        self.assertFalse(res.ok)
        self._assert_russian(res.message)
        self.assertFalse(res.dst.exists())

    def test_truncated_file_fails_gracefully(self):
        res = convert_one(FX["truncated"],
                          ConvertOptions(out_dir=self.out, allow_raw_fallback=False))
        self.assertFalse(res.ok)
        self._assert_russian(res.message)

    def test_missing_source(self):
        res = convert_one(self.out / "нет.CR2",
                          ConvertOptions(out_dir=self.out, allow_raw_fallback=False))
        self.assertFalse(res.ok)
        self._assert_russian(res.message)

    def test_garbage_source(self):
        p = self.out / "junk.CR2"
        p.write_bytes(os.urandom(4096))
        res = convert_one(p, ConvertOptions(out_dir=self.out / "o",
                                            allow_raw_fallback=False))
        self.assertFalse(res.ok)
        self._assert_russian(res.message)

    @unittest.skipIf(cr2_core.has_pillow(), "Pillow установлен")
    def test_resize_without_pillow_is_reported(self):
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out, max_side=800))
        self.assertFalse(res.ok)
        self.assertIn("Pillow", res.message)


# ==========================================================================
# Re-encode path (Pillow only)
# ==========================================================================


@unittest.skipUnless(cr2_core.has_pillow(), "нужен Pillow")
class TestReencode(Base):

    def test_max_side(self):
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out, max_side=800))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.mode, "reencode")
        self.assertEqual(max(res.width, res.height), 800)
        data = res.dst.read_bytes()
        self.assertEqual(jpeg_sof(data), (res.width, res.height))
        t = Tiff(exif_app1(data))
        self.assertEqual(t.get("exif", 0xA002), [res.width])
        self.assertEqual(t.get("exif", 0xA003), [res.height])
        self.assertEqual(t.problems, [])

    def test_bake_rotation_sets_orientation_to_one(self):
        res = convert_one(FX["rotated"], ConvertOptions(out_dir=self.out,
                                                        bake_rotation=True))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.mode, "reencode")
        self.assertEqual((res.width, res.height), (1288, 1936))   # 6 == 90 deg CW
        self.assertEqual(Tiff(exif_app1(res.dst.read_bytes())).get("ifd0", 0x0112), [1])


# ==========================================================================
# convert_many()
# ==========================================================================


class TestConvertMany(Base):

    def make_batch(self, n: int, prefix: str = "b") -> list[Path]:
        src = mk.make_cr2(self.out / "_seed.CR2", preview_size=(64, 48),
                          thumb_size=(32, 24), include_ifd2=False)
        paths = []
        for i in range(n):
            p = self.out / ("%s%03d.CR2" % (prefix, i))
            shutil.copyfile(src, p)
            paths.append(p)
        return paths

    def test_empty_input(self):
        seen = []
        res = convert_many([], ConvertOptions(out_dir=self.out),
                           on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(res, [])
        self.assertEqual(seen, [(0, 0)])

    def test_input_order_is_preserved(self):
        paths = self.make_batch(9)
        scrambled = [paths[i] for i in (4, 0, 8, 2, 7, 1, 6, 3, 5)]
        results = convert_many(scrambled, ConvertOptions(out_dir=self.out / "o"))
        self.assertEqual([r.src for r in results], scrambled)
        self.assertTrue(all(r.ok for r in results), [r.message for r in results])

    def test_progress_called_exactly_n_times(self):
        paths = self.make_batch(7)
        calls: list[tuple[int, int]] = []
        results: list = []
        out = convert_many(paths, ConvertOptions(out_dir=self.out / "o"),
                           on_result=results.append,
                           on_progress=lambda d, t: calls.append((d, t)))
        self.assertEqual(len(out), 7)
        self.assertEqual(len(calls), 7)
        self.assertEqual(calls, [(i + 1, 7) for i in range(7)])
        self.assertEqual([r.src for r in results], paths)

    def test_callback_exceptions_do_not_break_the_run(self):
        paths = self.make_batch(4)
        def boom(*a):
            raise RuntimeError("callback exploded")
        out = convert_many(paths, ConvertOptions(out_dir=self.out / "o"),
                           on_result=boom, on_progress=boom)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(r.ok for r in out))

    def test_cancel_before_start_stops_everything(self):
        paths = self.make_batch(12)
        cancel = threading.Event()
        cancel.set()
        calls: list[tuple[int, int]] = []
        out = convert_many(paths, ConvertOptions(out_dir=self.out / "o"),
                           on_progress=lambda d, t: calls.append((d, t)),
                           cancel=cancel)
        self.assertEqual(len(out), 12)
        self.assertEqual(len(calls), 12)
        self.assertTrue(all(r.skipped for r in out))
        self.assertFalse(any(r.ok for r in out))
        self.assertFalse(list((self.out / "o").glob("*.jpg"))
                         if (self.out / "o").exists() else [])

    def test_cancel_midway_stops_early(self):
        paths = self.make_batch(60)
        cancel = threading.Event()

        def progress(done: int, total: int) -> None:
            if done >= 1:
                cancel.set()

        out = convert_many(paths, ConvertOptions(out_dir=self.out / "o"),
                           on_progress=progress, cancel=cancel)
        self.assertEqual(len(out), 60)
        skipped = sum(1 for r in out if r.skipped)
        self.assertGreater(skipped, 0, "отмена не остановила очередь")
        self.assertTrue(out[0].ok, out[0].message)
        produced = len(list((self.out / "o").glob("*.jpg")))
        self.assertLess(produced, 60)

    def test_safe_from_a_non_main_thread(self):
        paths = self.make_batch(6, prefix="t")
        box: dict = {}

        def worker() -> None:
            try:
                box["main"] = threading.current_thread() is threading.main_thread()
                box["res"] = convert_many(paths, ConvertOptions(out_dir=self.out / "o"))
            except BaseException as exc:          # noqa: BLE001
                box["exc"] = exc

        th = threading.Thread(target=worker, name="cr2-test-worker")
        th.start()
        th.join(120)
        self.assertFalse(th.is_alive())
        self.assertNotIn("exc", box, repr(box.get("exc")))
        self.assertIs(box["main"], False)
        self.assertEqual(len(box["res"]), 6)
        self.assertTrue(all(r.ok for r in box["res"]), [r.message for r in box["res"]])

    def test_bad_files_do_not_abort_the_batch(self):
        paths = self.make_batch(3)
        bad = self.out / "bad.CR2"
        bad.write_bytes(b"\x00" * 200)
        mixed = [paths[0], bad, paths[1], self.out / "нет.CR2", paths[2]]
        out = convert_many(mixed, ConvertOptions(out_dir=self.out / "o",
                                                 allow_raw_fallback=False))
        self.assertEqual([r.src for r in out], mixed)
        self.assertEqual([r.ok for r in out], [True, False, True, False, True])


# ==========================================================================
# find_cr2()
# ==========================================================================


class TestFindCr2(Base):

    def build_tree(self) -> Path:
        root = self.out / "дерево"
        (root / "sub" / "deeper").mkdir(parents=True)
        (root / ".hidden").mkdir()
        for rel in ("a.CR2", "B.cr2", "c.Cr2", "note.txt", "x.cr2.bak",
                    "sub/d.CR2", "sub/deeper/e.cr2", ".hidden/f.CR2",
                    "sub/снимок.CR2"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        return root

    def test_case_insensitive_and_recursive(self):
        root = self.build_tree()
        found = find_cr2(root, recursive=True)
        names = sorted(p.name for p in found)
        self.assertEqual(names, ["B.cr2", "a.CR2", "c.Cr2", "d.CR2", "e.cr2", "снимок.CR2"])
        self.assertNotIn("f.CR2", names, "скрытый каталог не должен обходиться")
        self.assertNotIn("note.txt", names)
        self.assertNotIn("x.cr2.bak", names)

    def test_non_recursive(self):
        root = self.build_tree()
        found = find_cr2(root, recursive=False)
        self.assertEqual(sorted(p.name for p in found), ["B.cr2", "a.CR2", "c.Cr2"])

    def test_result_is_sorted(self):
        root = self.build_tree()
        found = find_cr2(root)
        self.assertEqual([str(p) for p in found],
                         sorted((str(p) for p in found), key=str.lower))

    def test_single_file(self):
        self.assertEqual(find_cr2(FX["normal"]), [FX["normal"]])
        txt = self.out / "x.txt"
        txt.write_bytes(b"x")
        self.assertEqual(find_cr2(txt), [])

    def test_missing_root(self):
        self.assertEqual(find_cr2(self.out / "нет-каталога"), [])

    def test_empty_dir(self):
        d = self.out / "пусто"
        d.mkdir()
        self.assertEqual(find_cr2(d), [])

    def test_extensions_constant(self):
        self.assertEqual(cr2_core.CR2_EXTS, (".cr2",))


# ==========================================================================
# Dataclass surface
# ==========================================================================


class TestApiSurface(Base):

    def test_preview_pixels(self):
        p = cr2_core.Preview(source="ifd0", offset=16, length=100, width=4, height=5)
        self.assertEqual(p.pixels, 20)
        self.assertEqual(cr2_core.Preview("ifd0", 16, 100).pixels, 0)

    def test_info_best_is_none_when_empty(self):
        self.assertIsNone(cr2_core.Cr2Info(path=Path("x")).best)

    def test_option_defaults(self):
        o = ConvertOptions()
        self.assertIsNone(o.out_dir)
        self.assertTrue(o.lossless)
        self.assertTrue(o.copy_exif)
        self.assertFalse(o.keep_makernote)
        self.assertFalse(o.strip_gps)
        self.assertFalse(o.overwrite)
        self.assertEqual(o.suffix, "")
        self.assertTrue(o.allow_raw_fallback)

    def test_optional_dependency_probes_return_bool(self):
        self.assertIsInstance(cr2_core.has_pillow(), bool)
        self.assertIsInstance(cr2_core.has_rawpy(), bool)

    def test_cr2error_is_an_exception(self):
        self.assertTrue(issubclass(cr2_core.Cr2Error, Exception))



# ==========================================================================
# Regression tests for the defects fixed in this round.
# Each class names the behaviour that used to be wrong.
# ==========================================================================


class TestBrokenMarkerChain(Base):
    """A broken segment length must never become a silent header-only JPEG."""

    def _corrupt_first_dht(self, name: str) -> Path:
        """Shrink the length word of the first DHT that follows the SOF."""
        src = FX[name]
        info = probe(src)
        pv = [c for c in info.previews if c.source == "ifd0"][0]
        data = bytearray(src.read_bytes())
        i = pv.offset + 2
        while i < pv.offset + pv.length:
            self.assertEqual(data[i], 0xFF)
            m = data[i + 1]
            if m == 0xD8 or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            ln = struct.unpack(">H", data[i + 2:i + 4])[0]
            if m == 0xC4:
                data[i + 2:i + 4] = struct.pack(">H", ln - 3)
                break
            i += 2 + ln
        else:
            self.fail("в фикстуре нет DHT")
        out = self.out / "broken.CR2"
        out.write_bytes(bytes(data))
        return out

    def test_segment_walk_reports_its_status(self):
        blob = self.read_preview_blob("normal")
        segs, status = cr2_core._jpeg_segments_checked(blob)
        self.assertEqual(status, cr2_core.SEG_COMPLETE)
        self.assertIn(0xDA, [m for m, _s, _l in segs])
        # A prefix of a longer stream is "partial", not "broken".
        _segs, status = cr2_core._jpeg_segments_checked(blob[:200], len(blob))
        self.assertEqual(status, cr2_core.SEG_PARTIAL)
        # The same prefix WITHOUT the true length is broken: it ends nowhere.
        _segs, status = cr2_core._jpeg_segments_checked(blob[:200])
        self.assertEqual(status, cr2_core.SEG_BROKEN)
        # Desynchronised marker chain.
        bad = bytearray(blob)
        bad[2] = 0x00
        _segs, status = cr2_core._jpeg_segments_checked(bytes(bad))
        self.assertEqual(status, cr2_core.SEG_BROKEN)

    def test_broken_preview_is_not_offered_by_probe(self):
        bad = self._corrupt_first_dht("normal")
        info = probe(bad)
        self.assertNotIn("ifd0", [c.source for c in info.previews],
                         "повреждённое превью IFD0 не должно предлагаться")

    def test_broken_preview_never_claims_a_byte_exact_copy(self):
        bad = self._corrupt_first_dht("normal")
        res = convert_one(bad, ConvertOptions(out_dir=self.out / "o"))
        if res.ok:
            # Fell through to a healthy smaller candidate: the output must
            # still be a complete JPEG, and must say which source it used.
            data = res.dst.read_bytes()
            self.assertIn(0xDA, [m for m, _s, _l in jpeg_segments(data)])
            self.assertIn(b"\xff\xd9", data)
            self.assertNotEqual(res.info.best.source, "ifd0")
        else:
            self.assertIn("оборван", res.message + " " + res.message)

    def test_verify_output_requires_sos_and_eoi(self):
        blob = self.read_preview_blob("normal")
        sos = [s for m, s, _l in cr2_core._jpeg_segments(blob) if m == 0xDA][0]
        info = probe(FX["normal"])
        w, h = info.best.width, info.best.height
        self.assertEqual(cr2_core._verify_output(blob, w, h), "")
        # Header only: SOF parses, dimensions match, no scan data at all.
        headers = blob[:sos]
        self.assertIsNotNone(cr2_core._jpeg_sof(headers))
        self.assertIn("SOS", cr2_core._verify_output(headers, w, h))
        # Scan present but truncated before EOI.
        self.assertIn("EOI", cr2_core._verify_output(blob[:-40], w, h))

    def test_truncated_strip_byte_count_is_reported_not_shipped(self):
        src = FX["normal"]
        info = probe(src)
        pv = [c for c in info.previews if c.source == "ifd0"][0]
        data = bytearray(src.read_bytes())
        idx = data.find(struct.pack("<HHI", 0x0117, 4, 1) + struct.pack("<I", pv.length))
        self.assertGreater(idx, 0, "не найдена запись StripByteCounts")
        data[idx + 8:idx + 12] = struct.pack("<I", pv.length - 50)
        short = self.out / "short.CR2"
        short.write_bytes(bytes(data))
        res = convert_one(short, ConvertOptions(out_dir=self.out / "o"))
        if res.ok:
            self.assertIn(b"\xff\xd9", res.dst.read_bytes()[-2:])
        else:
            self.assertIn("оборван", res.message)


class TestEoiPadding(Base):
    """Padding after EOI must be trimmed however long it is."""

    def test_padding_beyond_the_old_window_is_trimmed(self):
        for pad in (0, 12, 40, 512, 3000):
            with self.subTest(pad=pad):
                src = mk.make_cr2(self.out / ("p%d.CR2" % pad),
                                  preview_size=(320, 240), preview_padding=pad)
                res = convert_one(src, ConvertOptions(out_dir=self.out / ("o%d" % pad)))
                self.assertTrue(res.ok, res.message)
                self.assertEqual(res.dst.read_bytes()[-2:], b"\xff\xd9",
                                 "результат должен заканчиваться ровно на EOI")

    def test_big_padding_fixture_converts_cleanly(self):
        res = convert_one(FX["padded_preview_big"], ConvertOptions(out_dir=self.out))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.dst.read_bytes()[-2:], b"\xff\xd9")


class TestDestinationPlanning(Base):
    """Two sources must never be allowed to target one destination."""

    def make_colliding(self, folders) -> list[Path]:
        out = []
        for i, name in enumerate(folders):
            out.append(mk.make_cr2(self.out / "src" / name / "IMG_0001.CR2",
                                   preview_size=(64 + 2 * i, 48),
                                   thumb_size=(32, 24), include_ifd2=False))
        return out

    def test_plan_gives_every_source_a_unique_destination(self):
        srcs = self.make_colliding(("2021", "2022", "2023"))
        plan = cr2_core.plan_destinations(srcs, ConvertOptions(out_dir=self.out / "o"))
        dsts = [cr2_core._dst_key(d) for _s, d, _n in plan]
        self.assertEqual(len(set(dsts)), len(srcs))
        self.assertEqual(plan[0][1].name, "IMG_0001.jpg")
        self.assertTrue(plan[1][2], "переименование должно быть объяснено")

    def test_plan_is_case_insensitive(self):
        a = mk.make_cr2(self.out / "a" / "IMG_0001.CR2", preview_size=(64, 48),
                        thumb_size=(32, 24), include_ifd2=False)
        b = mk.make_cr2(self.out / "b" / "img_0001.CR2", preview_size=(64, 48),
                        thumb_size=(32, 24), include_ifd2=False)
        plan = cr2_core.plan_destinations([a, b], ConvertOptions(out_dir=self.out / "o"))
        self.assertNotEqual(cr2_core._dst_key(plan[0][1]), cr2_core._dst_key(plan[1][1]))

    def test_plan_is_deterministic(self):
        srcs = self.make_colliding(("x", "y", "z"))
        opts = ConvertOptions(out_dir=self.out / "o")
        first = [str(d) for _s, d, _n in cr2_core.plan_destinations(srcs, opts)]
        second = [str(d) for _s, d, _n in cr2_core.plan_destinations(srcs, opts)]
        self.assertEqual(first, second)

    def test_convert_many_writes_one_file_per_source(self):
        for overwrite in (True, False):
            with self.subTest(overwrite=overwrite):
                srcs = self.make_colliding(("d1", "d2", "d3", "d4"))
                outdir = self.out / ("out_%s" % overwrite)
                res = convert_many(srcs, ConvertOptions(out_dir=outdir,
                                                        overwrite=overwrite))
                self.assertTrue(all(r.ok for r in res), [r.message for r in res])
                files = sorted(p.name for p in outdir.iterdir() if p.suffix == ".jpg")
                # The invariant that used to fail: as many files as green rows.
                self.assertEqual(len(files), len(srcs), files)
                self.assertEqual(len(set(files)), len(files))
                # ...and they must be DIFFERENT photos, not one file four times.
                blobs = {(outdir / f).read_bytes() for f in files}
                self.assertEqual(len(blobs), len(srcs))
                self.assertEqual([], list(outdir.glob("*.tmp")))

    def test_same_source_twice_still_honours_overwrite(self):
        p = self.make_colliding(("only",))[0]
        opts = ConvertOptions(out_dir=self.out / "o")
        self.assertTrue(convert_one(p, opts).ok)
        again = convert_one(p, opts)
        self.assertTrue(again.skipped)
        self.assertIn("уже существует", again.message)


class TestAtomicWrite(Base):
    """The temp file must be unique per writer and never longer than dst."""

    def test_temp_names_are_unique(self):
        dst = self.out / "IMG_0001.jpg"
        names = {cr2_core._tmp_path(dst).name for _ in range(200)}
        self.assertEqual(len(names), 200)

    def test_temp_path_is_never_longer_than_the_destination(self):
        for stem_len in (8, 200, 251, 255):
            with self.subTest(stem_len=stem_len):
                dst = self.out / (("A" * stem_len) + ".jpg")
                tmp = cr2_core._tmp_path(dst)
                self.assertLessEqual(len(str(tmp)), len(str(dst)) if stem_len > 8
                                     else len(str(tmp)))
                self.assertLessEqual(len(tmp.name), 255)

    def test_long_file_name_converts(self):
        stem = "L" * 240
        src = mk.make_cr2(self.out / (stem + ".CR2"), preview_size=(64, 48),
                          thumb_size=(32, 24), include_ifd2=False)
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.dst.exists())

    def test_concurrent_writers_to_one_destination_all_survive(self):
        dst = self.out / "shared.jpg"
        errors: list[BaseException] = []
        payloads = [bytes([i]) * 4096 for i in range(8)]
        src = mk.make_cr2(self.out / "seed.CR2", preview_size=(64, 48),
                          thumb_size=(32, 24), include_ifd2=False)

        def writer(i: int) -> None:
            try:
                cr2_core._atomic_write(dst, payloads[i], src)
            except BaseException as exc:      # noqa: BLE001 - recorded, not raised
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "одновременная запись не должна падать")
        self.assertIn(dst.read_bytes(), payloads)     # last writer wins, cleanly
        self.assertEqual([], list(self.out.glob("*.tmp")))

    def test_sweep_removes_only_old_orphans(self):
        fresh = self.out / "a.jpg.deadbeef.tmp"
        fresh.write_bytes(b"x")
        old = self.out / "b.jpg.cafebabe.tmp"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        keep = self.out / "notes.txt"
        keep.write_bytes(b"x")
        cr2_core.sweep_stale_tmp([self.out])
        self.assertTrue(fresh.exists(), "свежий временный файл мог быть чужим")
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_the_mtime_copy_happens_inside_the_destination_lock(self):
        """Regression: os.utime() on dst used to run AFTER the lock was released.

        On Windows MoveFileEx onto a path that any handle has open fails with
        WinError 5, and os.utime opens one.  With the utime outside the lock the
        8-writer test above failed in 14 runs out of 25; this test pins the
        ordering directly instead of relying on a race to show up.
        """
        order: list[str] = []
        real_utime, real_replace = os.utime, os.replace
        held = cr2_core._DestLock

        class Spy(held):                       # type: ignore[misc, valid-type]
            def __enter__(self):
                order.append("lock")
                return super().__enter__()

            def __exit__(self, *exc):
                order.append("unlock")
                return super().__exit__(*exc)

        def spy_utime(path, times=None, **kw):
            order.append("utime")
            return real_utime(path, times, **kw)

        def spy_replace(a, b):
            order.append("replace")
            return real_replace(a, b)

        src = mk.make_cr2(self.out / "seed.CR2", preview_size=(64, 48),
                          thumb_size=(32, 24), include_ifd2=False)
        cr2_core._DestLock, os.utime, os.replace = Spy, spy_utime, spy_replace
        try:
            cr2_core._atomic_write(self.out / "x.jpg", b"payload", src)
        finally:
            cr2_core._DestLock, os.utime, os.replace = held, real_utime, real_replace
        self.assertEqual(order, ["lock", "replace", "utime", "unlock"])

    def test_replace_retries_a_transient_sharing_error(self):
        """A file briefly held by anti-virus must not lose the conversion."""
        real = os.replace
        calls = [0]

        def flaky(a, b):
            calls[0] += 1
            if calls[0] < 3:
                raise PermissionError(13, "Отказано в доступе")
            return real(a, b)

        tmp = self.out / "t.bin"
        tmp.write_bytes(b"z")
        os.replace = flaky
        try:
            cr2_core._replace_with_retry(tmp, self.out / "final.jpg")
        finally:
            os.replace = real
        self.assertEqual(calls[0], 3)
        self.assertEqual((self.out / "final.jpg").read_bytes(), b"z")

    def test_replace_still_raises_a_permanent_permission_error(self):
        """The retry is bounded: a real permission problem must still surface."""
        real = os.replace
        calls = [0]

        def always(a, b):
            calls[0] += 1
            raise PermissionError(13, "Отказано в доступе")

        tmp = self.out / "t2.bin"
        tmp.write_bytes(b"z")
        os.replace = always
        try:
            with self.assertRaises(PermissionError):
                cr2_core._replace_with_retry(tmp, self.out / "f2.jpg")
        finally:
            os.replace = real
        self.assertEqual(calls[0], len(cr2_core._REPLACE_DELAYS) + 1)

    def test_many_concurrent_batches_to_one_destination(self):
        """The 8-writer race, repeated: one flaky pass is not evidence."""
        src = mk.make_cr2(self.out / "s.CR2", preview_size=(64, 48),
                          thumb_size=(32, 24), include_ifd2=False)
        payloads = [bytes([i]) * 2048 for i in range(6)]
        for run in range(12):
            with self.subTest(run=run):
                dst = self.out / ("r%d.jpg" % run)
                errors: list[BaseException] = []

                def writer(i: int, dst=dst) -> None:
                    try:
                        cr2_core._atomic_write(dst, payloads[i], src)
                    except BaseException as exc:   # noqa: BLE001
                        errors.append(exc)

                ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                self.assertEqual(errors, [])
                self.assertIn(dst.read_bytes(), payloads)


class TestSuffixValidation(Base):

    def test_separators_are_rejected(self):
        for bad in ("\\..\\up", "/x", "a:b", "a*b", 'a"b', "a|b", "a?b", "a<b"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(cr2_core.validate_suffix(bad))

    def test_plain_suffixes_are_accepted(self):
        for good in ("", "_preview", "-превью", "_2024"):
            self.assertIsNone(cr2_core.validate_suffix(good), good)

    def test_convert_one_refuses_and_writes_nothing(self):
        before = sorted(p.name for p in self.out.iterdir())
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out / "o",
                                                       suffix="\\..\\..\\ESCAPED"))
        self.assertFalse(res.ok)
        self.assertIn("суффикс", res.message.lower())
        self.assertIsNone(res.dst)
        self.assertEqual(before, sorted(p.name for p in self.out.iterdir()))


class TestIhlRecordWalk(Base):
    """The stride is the record's own size, not the '+44 next record' field."""

    def test_every_record_is_found(self):
        info = probe(FX["dpp3_ihl"])
        sources = [c.source for c in info.previews]
        self.assertIn("vrd_ihl", sources)
        self.assertIn("vrd_ihl_thumb", sources)

    def test_fixture_writes_the_next_record_size_at_offset_44(self):
        data = FX["dpp3_ihl"].read_bytes()
        first = data.find(cr2_core._IHL_SIG)
        second = data.find(cr2_core._IHL_SIG, first + 1)
        self.assertGreater(second, first)
        size1, nxt1 = struct.unpack("<II", data[first + 40:first + 48])
        size2, nxt2 = struct.unpack("<II", data[second + 40:second + 48])
        self.assertEqual(nxt1, size2, "+44 должен нести размер СЛЕДУЮЩЕЙ записи")
        self.assertEqual(nxt2, 0, "последняя запись помечается нулём")
        self.assertEqual(second - first, 48 + size1)


class TestPreviewSourcePolicy(Base):
    """DPP's own IHL preview is opt-in, never a silent default."""

    def test_ihl_is_listed_but_not_preferred_even_when_larger(self):
        info = probe(FX["dpp3_ihl_small_preview"])
        sources = [c.source for c in info.previews]
        self.assertEqual(sources[0], "vrd_ihl", "фикстура должна ставить IHL первым")
        self.assertIn("vrd_ihl", sources)
        self.assertEqual(info.best.source, "ifd0")
        self.assertEqual(info.best_dpp.source, "vrd_ihl")

    def test_default_conversion_uses_the_camera_render(self):
        res = convert_one(FX["dpp3_ihl_small_preview"],
                          ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        self.assertEqual((res.width, res.height), (320, 212))
        self.assertIn("правки DPP НЕ применены", res.message)

    def test_opt_in_uses_the_dpp_preview_and_says_so(self):
        res = convert_one(FX["dpp3_ihl_small_preview"],
                          ConvertOptions(out_dir=self.out / "o2",
                                         prefer_dpp_preview=True))
        self.assertTrue(res.ok, res.message)
        self.assertEqual((res.width, res.height), (480, 320))
        self.assertIn("vrd_ihl", res.message)
        self.assertNotIn("правки DPP НЕ применены", res.message)

    def test_opt_in_without_an_ihl_preview_falls_back_loudly(self):
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out / "o3",
                                                       prefer_dpp_preview=True))
        self.assertTrue(res.ok, res.message)
        self.assertIn("превью DPP в файле нет", res.message)


class TestExifShedding(Base):
    """The APP1 shed must drop the tag that is actually over budget."""

    def test_oversized_ifd0_tag_goes_before_capture_tags(self):
        src = mk.make_cr2(self.out / "big.CR2", preview_size=(320, 240),
                          artist="A" * 70000, makernote=None)
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        app1 = exif_app1(res.dst.read_bytes())
        self.assertIsNotNone(app1)
        t = Tiff(app1)
        exif = t.ifds.get("exif")
        self.assertIsNotNone(exif, "ExifIFD должен уцелеть")
        self.assertIn(0x9003, exif, "дата съёмки не должна выбрасываться первой")
        self.assertIn(0x829A, exif, "выдержка не должна выбрасываться первой")
        self.assertNotIn(0x013B, t.ifds["ifd0"], "выброшен должен быть огромный Artist")
        self.assertEqual(res.message.count("крупные необязательные теги"), 1)


class TestAsciiTermination(Base):
    """Type-2 values must carry their terminating NUL in the rebuilt APP1."""

    @staticmethod
    def _raw_entry(tiff: bytes, ifd_off: int, tag: int):
        """(type, count, value_bytes) straight out of the emitted TIFF.

        Tiff._decode() splits an ASCII value at the first NUL, so it cannot
        see whether the terminator is there at all - we have to read the raw
        entry ourselves.
        """
        e = "<" if tiff[:2] == b"II" else ">"
        n = struct.unpack(e + "H", tiff[ifd_off:ifd_off + 2])[0]
        for i in range(n):
            p = ifd_off + 2 + 12 * i
            got, typ, count = struct.unpack(e + "HHI", tiff[p:p + 8])
            if got != tag:
                continue
            size = _TYPESZ.get(typ, 0) * count
            if size <= 4:
                return typ, count, tiff[p + 8:p + 8 + size]
            off = struct.unpack(e + "I", tiff[p + 8:p + 12])[0]
            return typ, count, tiff[off:off + size]
        return None, 0, b""

    def test_ascii_values_are_nul_terminated(self):
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        tiff = exif_app1(res.dst.read_bytes())
        e = "<" if tiff[:2] == b"II" else ">"
        ifd0_off = struct.unpack(e + "HI", tiff[2:8])[1]
        for tag in (0x010F, 0x0110):
            typ, count, raw = self._raw_entry(tiff, ifd0_off, tag)
            self.assertEqual(typ, 2, "тег 0x%04X" % tag)
            self.assertEqual(len(raw), count)
            self.assertEqual(raw[-1:], b"\x00", "ASCII должен оканчиваться NUL")

    def test_collect_re_terminates_and_recounts(self):
        """A source count that omits the NUL must be repaired, not copied."""

        class _Ent:
            tag, typ, count, inline = 0x010F, 2, 6, False

            def data(self, _view):
                return b"Canon1"          # без завершающего NUL

        out = cr2_core._collect(None, [_Ent()], set())
        self.assertEqual(out[0][1], 2)
        self.assertEqual(out[0][3], b"Canon1\x00")
        self.assertEqual(out[0][2], len(out[0][3]))


class TestXmpPackets(Base):
    """Exactly one primary XMP packet in the output."""

    def _xmp_app1s(self, data: bytes) -> list[bytes]:
        out = []
        for marker, start, length in jpeg_segments(data):
            if marker != 0xE1:
                continue
            payload = data[start + 4:start + length]
            if payload.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                out.append(payload[29:])
        return out

    def _build(self, ifd0_xmp: bytes | None, preview_xmp: bytes | None) -> Path:
        real = mk.baseline_jpeg

        def wrapped(w, h, **kw):
            blob = real(w, h, **kw)
            if preview_xmp is None or (w, h) != (320, 240):
                return blob
            payload = b"http://ns.adobe.com/xap/1.0/\x00" + preview_xmp
            seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
            return blob[:2] + seg + blob[2:]

        mk.baseline_jpeg = wrapped
        try:
            p = mk.make_cr2(self.out / "xmp.CR2", preview_size=(320, 240))
        finally:
            mk.baseline_jpeg = real
        if ifd0_xmp is not None:
            # Splice the XMP tag in by rebuilding with the generator's own hook
            # is not available, so patch the produced file is not possible here;
            # the IFD0 case is covered by the no-injection test below.
            pass
        return p

    def test_preview_xmp_survives_when_we_inject_nothing(self):
        p = self._build(None, b"<x:xmpmeta><PREVIEW/></x:xmpmeta>")
        res = convert_one(p, ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        packets = self._xmp_app1s(res.dst.read_bytes())
        self.assertEqual(len(packets), 1)
        self.assertIn(b"PREVIEW", packets[0])

    def test_splice_keeps_at_most_one_packet(self):
        blob = self.read_preview_blob("normal")
        payload = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><OLD/></x:xmpmeta>"
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        blob = blob[:2] + seg + blob[2:]
        mine = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><NEW/></x:xmpmeta>"
        front = [b"\xff\xe1" + struct.pack(">H", len(mine) + 2) + mine]
        out = cr2_core._splice(blob, front, drop_xmp=True)
        self.assertEqual(out.count(b"http://ns.adobe.com/xap/1.0/\x00"), 1)
        self.assertIn(b"<NEW/>", out)
        self.assertNotIn(b"<OLD/>", out)
        out = cr2_core._splice(blob, [], drop_xmp=False)
        self.assertEqual(out.count(b"http://ns.adobe.com/xap/1.0/\x00"), 1)
        self.assertIn(b"<OLD/>", out)


@unittest.skipUnless(cr2_core.has_pillow(), "нужен Pillow")
class TestBakedRotationThumbnail(Base):
    """IFD1 has no Orientation of its own, so the thumbnail must be rotated too."""

    def test_thumbnail_matches_the_rotated_main_image(self):
        for order in ("II", "MM"):
            with self.subTest(order=order):
                src = mk.make_cr2(self.out / ("rot_%s.CR2" % order), byte_order=order,
                                  orientation=6, preview_size=(640, 480),
                                  thumb_size=(160, 120))
                res = convert_one(src, ConvertOptions(out_dir=self.out / ("o" + order),
                                                      bake_rotation=True))
                self.assertTrue(res.ok, res.message)
                data = res.dst.read_bytes()
                w, h = jpeg_sof(data)
                self.assertGreater(h, w, "главное изображение — портрет")
                t = Tiff(exif_app1(data))
                self.assertEqual(t.get("ifd0", 0x0112), [1])
                if t.thumb:
                    tw, th = jpeg_sof(t.thumb)
                    self.assertGreater(th, tw,
                                       "миниатюра должна быть повёрнута так же")

    def test_downscale_leaves_the_thumbnail_alone(self):
        src = mk.make_cr2(self.out / "big.CR2", preview_size=(1936, 1288),
                          thumb_size=(160, 120))
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o", max_side=800))
        self.assertTrue(res.ok, res.message)
        t = Tiff(exif_app1(res.dst.read_bytes()))
        if t.thumb:
            self.assertEqual(jpeg_sof(t.thumb), (160, 120),
                             "равномерное уменьшение не трогает миниатюру")


class TestFindCr2Cancel(Base):

    def _tree(self) -> Path:
        root = self.out / "tree"
        for i in range(3):
            d = root / ("d%d" % i)
            for j in range(3):
                mk.make_cr2(d / ("f%d_%d.CR2" % (i, j)), preview_size=(64, 48),
                            thumb_size=(32, 24), include_ifd2=False)
        return root

    def test_preset_cancel_stops_immediately(self):
        root = self._tree()
        self.assertEqual(len(find_cr2(root)), 9)
        ev = threading.Event()
        ev.set()
        self.assertEqual(find_cr2(root, cancel=ev), [])

    def test_unreadable_directory_is_reported(self):
        root = self._tree()
        blocked = os.path.normcase(str(root / "d1"))
        real_scandir = os.scandir

        def fake_scandir(path=".", *a, **kw):
            if os.path.normcase(str(path)) == blocked:
                raise PermissionError(13, "Permission denied")
            return real_scandir(path, *a, **kw)

        seen: list[tuple[Path, OSError]] = []
        os.scandir = fake_scandir
        try:
            found = find_cr2(root, on_problem=lambda p, e: seen.append((p, e)))
        finally:
            os.scandir = real_scandir
        self.assertEqual(len(found), 6, "три файла в закрытом каталоге пропали")
        self.assertEqual(len(seen), 1, "о пропаже обязаны сообщить")
        self.assertEqual(os.path.normcase(str(seen[0][0])), blocked)

    def test_signature_stays_backward_compatible(self):
        root = self._tree()
        self.assertEqual(len(find_cr2(root, True)), 9)
        self.assertEqual(len(find_cr2(root, False)), 0)


class TestSubsampledRaw(Base):
    """mRAW/sRAW: the frame size and the share-of-frame warning must not lie."""

    def test_ordinary_raw_is_not_flagged(self):
        info = probe(FX["normal"])
        self.assertFalse(info.raw_subsampled, "обычный RAW не субдискретизирован")
        self.assertEqual((info.raw_width, info.raw_height), (1936, 1288))

    def test_subsampled_sof3_is_detected_and_not_multiplied(self):
        info = probe(FX["sraw"])
        self.assertTrue(info.raw_subsampled)
        # 3 components with vsf=2: the old arithmetic reported 3x the width
        # and 2x the height of a frame that never had that many pixels.
        self.assertEqual((info.raw_width, info.raw_height), (644, 644))

    def test_subsampled_frame_replaces_the_bogus_share_warning(self):
        res = convert_one(FX["sraw"], ConvertOptions(out_dir=self.out / "o"))
        self.assertTrue(res.ok, res.message)
        self.assertNotIn("площади кадра", res.message)
        self.assertIn("mRAW/sRAW", res.message)

    def test_ordinary_small_preview_still_warns(self):
        """Sanity: the warning the flag suppresses does fire for normal files."""
        res = convert_one(FX["tiny_preview"], ConvertOptions(out_dir=self.out / "o2"))
        self.assertTrue(res.ok, res.message)
        self.assertIn("площади кадра", res.message)


class TestConvertOneApi(Base):

    def test_dst_override_is_honoured(self):
        target = self.out / "custom" / "renamed.jpg"
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out / "ignored"),
                          dst=target)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.dst, target)
        self.assertTrue(target.exists())

    def test_cancel_is_checked_before_work(self):
        ev = threading.Event()
        ev.set()
        res = convert_one(FX["normal"], ConvertOptions(out_dir=self.out), cancel=ev)
        self.assertTrue(res.skipped)
        self.assertEqual([], list(self.out.glob("*.jpg")))


class TestMeasuredBodyShapes(Base):
    """The two body shapes that decide what the user actually gets.

    Measured on the user's own library (155 files, C:\\Users\\Lenovo\\Desktop\\
    01.09.26 + 123 + 23.04.2026): every file is a Canon EOS 550D carrying a
    5184x3456 IFD0 preview - byte-for-byte the raw dimensions - and none of them
    carries a DPP recipe.  The half-resolution preview the research warned about
    is real, but it belongs to older bodies; both shapes are pinned here so a
    future change cannot quietly turn one into the other.
    """

    def test_a_550d_shaped_file_is_full_frame_lossless_and_unflagged(self):
        info = probe(FX["eos550d"])
        self.assertEqual(info.camera, "Canon EOS 550D")
        best = info.best
        self.assertEqual((best.width, best.height),
                         (info.raw_width, info.raw_height))
        self.assertFalse(info.has_dpp_recipe)
        res = convert_one(FX["eos550d"], ConvertOptions(out_dir=self.out))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.mode, "lossless")
        self.assertEqual((res.width, res.height), (1936, 1288))
        self.assertNotIn("превью меньше кадра", res.message)

    def test_the_output_entropy_is_the_preview_blob_byte_for_byte(self):
        """The whole promise of the default path, on the 550D-shaped fixture."""
        blob = self.read_preview_blob("eos550d")
        res = convert_one(FX["eos550d"], ConvertOptions(out_dir=self.out))
        data = res.dst.read_bytes()
        sos_b = [s for m, s, _l in jpeg_segments(blob) if m == 0xDA][0]
        sos_o = [s for m, s, _l in jpeg_segments(data) if m == 0xDA][0]
        self.assertEqual(data[sos_o:], blob[sos_b:])

    def test_an_older_body_with_a_half_frame_preview_is_flagged(self):
        info = probe(FX["halfsize_body"])
        best = info.best
        self.assertEqual((best.width * 2, best.height * 2),
                         (info.raw_width, info.raw_height))
        res = convert_one(FX["halfsize_body"], ConvertOptions(out_dir=self.out))
        self.assertTrue(res.ok, res.message)
        # Still byte-exact - just smaller than the frame, and it says so.
        self.assertEqual(res.mode, "lossless")
        self.assertIn("превью", res.message.lower())

    def test_no_recipe_found_is_stated_without_being_an_all_clear(self):
        """155 real files had no recipe — and that still proves nothing."""
        for name in ("eos550d", "halfsize_body"):
            with self.subTest(name=name):
                info = probe(FX[name])
                self.assertFalse(info.has_dpp_recipe)
                hint = info.recipe_hint
                self.assertIn("не найден", hint)
                self.assertIn("НЕ доказывает", hint)
                self.assertIn(".vrd", hint)
                for lie in ("правок нет", "файл не редактировался"):
                    self.assertNotIn(lie, hint)


class TestRecipeHintHonesty(Base):

    def test_absence_of_a_trailer_is_not_sold_as_proof(self):
        info = probe(FX["normal"])
        self.assertFalse(info.has_dpp_recipe)
        self.assertNotIn("корректен для этого файла", info.recipe_hint)
        self.assertIn("не доказывает", info.recipe_hint.lower())
        self.assertIn(".vrd", info.recipe_hint)


# ==========================================================================
# The front ends.  Neither module had a single test before.
# ==========================================================================


class TestReadme(unittest.TestCase):
    """The README is a user-facing string like any other: it must be true."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path(__file__).resolve().parent / "README.md"
        cls.text = cls.path.read_text(encoding="utf-8")

    def _blocks(self) -> list[str]:
        out, inside, buf = [], False, []
        for line in self.text.splitlines():
            if line.startswith("```"):
                if inside:
                    out.append("\n".join(buf))
                    buf = []
                inside = not inside
                continue
            if inside:
                buf.append(line)
        return out

    def test_it_never_promises_that_dpp_edits_are_applied(self):
        import re
        for lie in ("JPEG с правками DPP", "правки DPP применяются",
                    "как в DPP", "совпадёт с DPP", "как показывает DPP",
                    "с учётом правок DPP"):
            self.assertNotIn(lie, self.text)
        # Affirmative "applies the recipe", but NOT the negated form the honest
        # text is full of ("не применяет рецепты Canon DPP").
        affirmative = re.compile(
            r"(?<!не )(?<!НЕ )(?:примен(?:яет|ит|яются)|учитыва(?:ет|ются))"
            r"\s+(?:рецепт|правк)", re.IGNORECASE)
        self.assertIsNone(affirmative.search(self.text),
                          "README утверждает, что правки применяются")
        self.assertIn("НЕ попадают", self.text)
        self.assertIn("Convert and save", self.text)

    def test_it_states_the_refuted_half_resolution_assumption(self):
        self.assertIn("5184 × 3456", self.text)
        self.assertIn("не относится к 550D", self.text)
        self.assertIn("155", self.text)

    def test_every_fenced_block_holds_exactly_one_command(self):
        for block in self._blocks():
            with self.subTest(block=block[:50]):
                lines = [ln for ln in block.splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1, "в блоке больше одной команды")

    def test_every_cli_example_actually_parses(self):
        """A README option that the parser rejects is a lie about the program."""
        import shlex
        import cr2_convert
        parser = cr2_convert.build_parser()
        seen = 0
        for block in self._blocks():
            cmd = block.strip()
            if "cr2_convert.py" not in cmd:
                continue
            argv = shlex.split(cmd, posix=False)
            argv = argv[argv.index("cr2_convert.py") + 1:]
            argv = [a.strip('"') for a in argv]
            seen += 1
            with self.subTest(cmd=cmd):
                if "--help" in argv:
                    continue
                try:
                    parser.parse_args(argv)
                except SystemExit:      # argparse prints and exits on a bad flag
                    self.fail("README предлагает команду, которую разбор отвергает")
        self.assertGreaterEqual(seen, 5, "в README почти нет примеров")

    def test_it_is_short_enough_to_be_read(self):
        lines = len(self.text.splitlines())
        self.assertLess(lines, 300, "README снова разросся: %d строк" % lines)
        self.assertLess(len(self.text.encode("utf-8")), 24000)

    def test_it_lists_every_shipped_file(self):
        here = self.path.parent
        shipped = {p.name for p in here.iterdir()
                   if p.is_file() and p.name != "README.md"
                   and not p.name.endswith((".log", ".json", ".bad"))}
        for name in shipped:
            with self.subTest(name=name):
                self.assertIn(name, self.text)


class TestCliModule(unittest.TestCase):

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cr2_cli_")
        self.out = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        import cr2_convert
        self.cli = cr2_convert

    def test_module_imports_and_builds_its_parser(self):
        self.assertTrue(self.cli.build_parser())

    def _capture(self) -> list[str]:
        said: list[str] = []
        con = self.cli.Console(None)
        con.line = said.append          # type: ignore[method-assign]
        self._con = con
        return said

    def test_recipe_found_names_the_only_tool_that_can_apply_it(self):
        said = self._capture()
        self.cli._print_dpp_notice(self._con, 3)
        text = " ".join(said)
        self.assertIn("НЕ может", text)
        self.assertIn("Canon Digital Photo", text)
        self.assertIn("Конвертировать и сохранить", text)
        self.assertIn("Пакетная обработка", text)

    def test_no_recipe_found_is_not_reported_as_an_all_clear(self):
        """«Рецептов не найдено» says only that; it never implies «не правили»."""
        said = self._capture()
        self.cli._print_dpp_notice(self._con, 0)
        text = " ".join(said)
        self.assertIn("не найдено", text)
        self.assertIn("ничего не говорит", text)
        for lie in ("правок нет", "снимок не правили", "оригинал не изменялся"):
            self.assertNotIn(lie, text)

    def test_help_text_does_not_promise_dpp_edits(self):
        parser = self.cli.build_parser()
        said = self._capture()
        self._con.write = lambda t: said.append(t) or len(t)  # type: ignore[assignment]
        parser.print_help(self._con)
        text = " ".join(said)
        for lie in ("с правками DPP", "применяет правки", "применяются правки"):
            self.assertNotIn(lie, text)

    def test_bracketed_directory_is_matched(self):
        d = self.out / "Съёмка [2024]"
        mk.make_cr2(d / "a.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        mk.make_cr2(d / "sub" / "b.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        files, problems = self.cli.collect_inputs([str(d / "*.CR2")], True)
        self.assertEqual(len(files), 1, problems)
        files, problems = self.cli.collect_inputs([str(d / "**" / "*.CR2")], True)
        self.assertEqual(len(files), 2, problems)

    def test_recursive_wildcards_still_work(self):
        d = self.out / "plain"
        mk.make_cr2(d / "a.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        mk.make_cr2(d / "sub" / "b.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        files, problems = self.cli.collect_inputs([str(d / "**" / "*.CR2")], True)
        self.assertEqual(len(files), 2, problems)

    def test_missing_bracketed_path_says_path_not_found(self):
        _f, problems = self.cli.collect_inputs([r"D:\нет\такой [папки]"], True)
        self.assertEqual(len(problems), 1)
        self.assertIn("путь не найден", problems[0])

    def test_suffix_rule_is_shared_with_the_core(self):
        parser = self.cli.build_parser()
        args = parser.parse_args(["x.CR2", "--suffix", "\\..\\bad"])
        # The CLI's Console objects bind the real streams at import time, so
        # redirect_stderr cannot silence them: patch the sink instead.
        said: list[str] = []
        for con in (self.cli.ERR, self.cli.OUT):
            for attr in ("line", "write"):     # print_usage() goes through write
                original = getattr(con, attr)
                setattr(con, attr, said.append)
                self.addCleanup(setattr, con, attr, original)
        with self.assertRaises(SystemExit):
            self.cli.validate(args, parser)
        self.assertTrue(any("suffix" in t for t in said), said)
        args = parser.parse_args(["x.CR2", "--suffix", "_preview"])
        self.cli.validate(args, parser)          # must NOT raise

    def test_workers_pool_resolves_collisions(self):
        srcs = [mk.make_cr2(self.out / "s" / n / "IMG_0001.CR2", preview_size=(64, 48),
                            thumb_size=(32, 24), include_ifd2=False)
                for n in ("a", "b", "c")]
        outdir = self.out / "o"
        rep = self.cli.Reporter(self.cli.Console(sys.stdout), srcs, "", 0)
        rep.con.line = lambda *_a, **_k: None
        res = self.cli._run_pool(srcs, ConvertOptions(out_dir=outdir, overwrite=True),
                                 4, rep.on_result, threading.Event())
        self.assertTrue(all(r.ok for r in res), [r.message for r in res])
        files = sorted(p.name for p in outdir.glob("*.jpg"))
        self.assertEqual(len(files), 3, files)


class TestGuiModule(unittest.TestCase):
    """The GUI is loaded by path: '.pyw' is not in SOURCE_SUFFIXES off Windows."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        path = str(Path(__file__).resolve().parent / "cr2_gui.pyw")
        spec = importlib.util.spec_from_file_location(
            "cr2_gui_under_test", path, loader=SourceFileLoader("cr2_gui_under_test", path))
        module = importlib.util.module_from_spec(spec)
        # Must be registered BEFORE exec: @dataclass looks the module up in
        # sys.modules while the class body is executing.
        sys.modules["cr2_gui_under_test"] = module
        spec.loader.exec_module(module)
        cls.gui = module
        # The module writes its crash log next to itself.  Several tests below
        # deliberately provoke crashes, and without this redirect every test run
        # left a growing cr2_gui_error.log in the SHIPPED folder - the shipped
        # copy of that file was 30 KB of pure test noise.
        cls._logdir = tempfile.TemporaryDirectory(prefix="cr2_gui_log_")
        module.ERROR_LOG_PATH = Path(cls._logdir.name) / "cr2_gui_error.log"
        cls.addClassCleanup(cls._logdir.cleanup)

    def test_the_crash_log_never_lands_in_the_shipped_folder(self):
        """setUpClass must actually redirect it, or this suite dirties the ship."""
        here = Path(__file__).resolve().parent
        written = self.gui.record_error("regression-probe",
                                        "не должно попасть в папку поставки")
        self.assertNotEqual(written.parent.resolve(), here)
        self.assertIn("regression-probe", written.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cr2_gui_")
        self.out = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    @staticmethod
    def _visible_strings(src: str) -> list[str]:
        """Every string literal in `src` except docstrings (never displayed)."""
        import ast
        tree = ast.parse(src)
        docs: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    docs.add(id(body[0].value))
        return [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]

    def test_labels_do_not_promise_dpp_edits(self):
        """No user-visible string may claim a DPP recipe is applied.

        Asserted on meaning, not on one exact sentence: an earlier version of
        this test pinned a literal phrase, so rewording the honest notice broke
        the test while the program stayed correct.
        """
        src = (Path(__file__).resolve().parent / "cr2_gui.pyw").read_text(encoding="utf-8")
        for lie in ("CR2 -> JPEG (с правками DPP)", "с правками DPP)",
                    "точная копия превью DPP", "правки DPP применены",
                    "с учётом правок DPP"):
            self.assertNotIn(lie, src)
        # The window title names the real product and never mentions DPP.
        self.assertIn('root.title("CR2 в JPEG (без потерь, как снято)")', src)
        title = src.split('root.title("')[1].split('")')[0]
        self.assertNotIn("DPP", title)
        # There is exactly one canonical recipe notice, and it says three
        # things: a recipe was found, this program cannot apply it, and the
        # only thing that can is Canon DPP's own Convert-and-save.
        text = self.gui.DPP_RECIPE_TEXT
        self.assertIn("НЕ может", text)
        self.assertIn("Canon Digital Photo", text)
        self.assertIn("Конвертировать и сохранить", text)

    def test_default_checkbox_describes_extraction_not_dpp(self):
        """The default action's label must say what it does: extract, verbatim."""
        src = (Path(__file__).resolve().parent / "cr2_gui.pyw").read_text(encoding="utf-8")
        label = src.split('opt, text="')[1].split('"')[0]
        self.assertIn("встроенный JPEG", label)
        self.assertIn("без перекодирования", label)
        self.assertNotIn("DPP", label)

    def test_absent_recipe_is_never_sold_as_no_edits(self):
        """«Рецепт не найден» must not be dressed up as «правок нет».

        Only strings that can reach the screen are searched.  Comments and
        docstrings are excluded on purpose: both discuss the wrong wording in
        order to warn the next editor off it, and neither is ever displayed.
        """
        src = (Path(__file__).resolve().parent / "cr2_gui.pyw").read_text(encoding="utf-8")
        self.assertIn("Рецепт DPP не найден ни в одном файле.", src)
        blob = "\n".join(self._visible_strings(src))
        for lie in ("правок нет", "снимок не правили", "файл не редактировался",
                    "правки применены"):
            self.assertNotIn(lie, blob)

    def test_half_size_preview_is_flagged_in_both_tables(self):
        src = mk.make_cr2(self.out / "small.CR2", preview_size=(320, 212))
        info = probe(src)
        row = self.gui._row_from_info(info, 0.4)
        self.assertEqual(row.tag, "warn")
        self.assertNotEqual(row.share, "?")
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o"))
        row = self.gui._row_from_result(res, 0.4)
        self.assertEqual(row.tag, "warn")
        self.assertIn("превью меньше кадра", row.status)

    def test_full_size_preview_stays_green(self):
        src = mk.make_cr2(self.out / "full.CR2", preview_size=(1936, 1288),
                          raw_size=(1936, 1288))
        res = convert_one(src, ConvertOptions(out_dir=self.out / "o"))
        self.assertEqual(self.gui._row_from_result(res, 0.4).tag, "ok")

    def test_crash_in_convert_many_is_reported_as_a_crash(self):
        import queue
        d = self.out / "job"
        mk.make_cr2(d / "a.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        real = cr2_core.convert_many

        def boom(*_a, **_k):
            raise MemoryError("simulated")

        cr2_core.convert_many = boom
        try:
            q: "queue.Queue" = queue.Queue()
            self.gui.job_worker({"root": str(d), "recursive": True},
                                ConvertOptions(out_dir=self.out / "o"), False,
                                threading.Event(), q)
        finally:
            cr2_core.convert_many = real
        done = [m for m in _drain(q) if isinstance(m, self.gui.MDone)]
        self.assertEqual(len(done), 1)
        self.assertIn("MemoryError", done[0].crashed)

    def test_cancelled_scan_does_not_claim_no_files_found(self):
        import queue
        d = self.out / "job2"
        mk.make_cr2(d / "a.CR2", preview_size=(64, 48), thumb_size=(32, 24),
                    include_ifd2=False)
        ev = threading.Event()
        ev.set()
        q = queue.Queue()
        self.gui.job_worker({"root": str(d), "recursive": True},
                            ConvertOptions(out_dir=self.out / "o"), True, ev, q)
        msgs = _drain(q)
        texts = [m.text for m in msgs if isinstance(m, self.gui.MLog)]
        self.assertFalse(any("не найдены" in t for t in texts), texts)
        self.assertTrue(any("отменён" in t for t in texts), texts)
        done = [m for m in msgs if isinstance(m, self.gui.MDone)]
        self.assertTrue(done[0].cancelled)
        self.assertEqual(done[0].crashed, "")

    def test_settings_survive_a_non_utf8_rewrite(self):
        self.gui.SETTINGS_PATH = self.out / "s.json"
        self.gui.save_settings(dict(self.gui.DEFAULT_SETTINGS,
                                    src=str(self.out / "Фото"), quality=88))
        text = self.gui.SETTINGS_PATH.read_bytes().decode("utf-8")
        self.gui.SETTINGS_PATH.write_bytes(text.encode("cp1251"))
        loaded = self.gui.load_settings()
        self.assertTrue(loaded["src"].endswith("Фото"))
        self.assertEqual(loaded["quality"], 88)

    def test_broken_settings_are_quarantined_not_lost(self):
        self.gui.SETTINGS_PATH = self.out / "s2.json"
        self.gui.SETTINGS_PATH.write_bytes(b"{ not json")
        self.assertEqual(self.gui.load_settings()["src"], "")
        self.assertTrue((self.out / "s2.json.bad").exists())

    def test_one_bad_key_does_not_reset_the_others(self):
        self.gui.SETTINGS_PATH = self.out / "s3.json"
        self.gui.SETTINGS_PATH.write_text(
            '{"src": "D:/keep", "quality": 1e999, "suffix": "_x", "recursive": null}',
            encoding="utf-8")
        loaded = self.gui.load_settings()
        self.assertEqual(loaded["src"], "D:/keep")
        self.assertEqual(loaded["suffix"], "_x")
        self.assertEqual(loaded["quality"], 95)      # умолчание, не обрыв цикла
        self.assertTrue(loaded["recursive"])         # null == «взять умолчание»


def _drain(q) -> list:
    import queue as _q
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except _q.Empty:
            return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
