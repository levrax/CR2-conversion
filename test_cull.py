# -*- coding: utf-8 -*-
"""Тесты cull.py - отбор кадров: серии, отрезки события, флаги, экспорт.

Только синтетические кадры, созданные во временной папке: ни одной настоящей
фотографии, ни одного пути пользователя.  Время съёмки пишется в EXIF так же,
как его пишет камера (DateTimeOriginal + SubSecTimeOriginal), и нарочно с
неверным годом - модуль обязан смотреть только на порядок и паузы.

Поиск лиц в большинстве тестов выключен (use_faces=False): на синтетике лиц
нет, а результат не должен зависеть от того, стоит ли OpenCV на машине CI.
Отдельный тест прячет cv2 целиком и проверяет режим "без лиц".
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError as exc:                       # pragma: no cover
    raise unittest.SkipTest("нужны numpy и Pillow: %s" % exc)

import cull  # noqa: E402

BASE = datetime(2011, 4, 23, 21, 0, 0)           # неверные часы камеры - как в жизни
SIZE = (480, 320)
#: Заметка scan(), когда пул процессов упал и разбор доделан в потоках.
PROCESS_FALLBACK = "Параллельные процессы недоступны"


def scene(seed: int, blur: float = 0.0, size: tuple[int, int] = SIZE) -> Image.Image:
    """Кадр с резкими краями: случайные цветные блоки + сетка тонких линий.

    Разные seed - разные «сцены» (далёкие aHash и гистограммы); один seed с
    разным blur - «серия» почти одинаковых кадров разной резкости.
    """
    rng = np.random.default_rng(seed)
    w, h = size
    blocks = rng.integers(0, 256, size=(8, 12, 3), dtype=np.uint8)
    im = Image.fromarray(blocks).resize((w, h), Image.NEAREST)
    a = np.asarray(im).copy()
    step = int(rng.integers(9, 15))
    a[::step, :, :] = 255 - a[::step, :, :]
    a[:, ::step, :] = 255 - a[:, ::step, :]
    im = Image.fromarray(a)
    if blur > 0:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    return im


def save_frame(path: Path, im: Image.Image, t: datetime) -> Path:
    """JPEG с временем съёмки в EXIF, как у камеры (включая сотые доли секунды)."""
    exif = Image.Exif()
    stamp = t.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x0132] = stamp
    sub = exif.get_ifd(0x8769)
    sub[36867] = stamp
    sub[37521] = "%02d" % (t.microsecond // 10000)
    im.save(path, quality=92, exif=exif)
    return path


class Shoot:
    """Синтетическая съёмка из трёх отрезков, разделённых паузами по 10 минут.

    Отрезок A: серия из 5 кадров одной сцены (размытие 2.0, 0, 1.0, 3.0, 1.5 -
               резкий второй), затем два одиночных кадра.
    Отрезок B: три одиночных кадра, один из них сильно размыт (флаг «нерезкий»).
    Отрезок C: два одиночных кадра.

    Имена нарочно не совпадают с порядком съёмки (_MG_ и IMG_ вперемешку).
    """

    BURST_BLUR = (2.0, 0.0, 1.0, 3.0, 1.5)

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.files: dict[str, Path] = {}
        t = BASE
        n = 100
        for k, blur in enumerate(self.BURST_BLUR):
            name = ("_MG_%04d.jpg" if k % 2 else "IMG_%04d.jpg") % n
            self.files["burst%d" % k] = save_frame(folder / name, scene(1, blur),
                                                   t + timedelta(milliseconds=300 * k))
            n += 1
        for k, seed in enumerate((2, 3)):
            self.files["a_single%d" % k] = save_frame(
                folder / ("IMG_%04d.jpg" % n), scene(seed), t + timedelta(seconds=40 + 40 * k))
            n += 1
        t = BASE + timedelta(minutes=10)
        for k, (seed, blur) in enumerate(((4, 0.0), (5, 9.0), (6, 0.0))):
            key = "b_soft" if blur else "b%d" % k
            self.files[key] = save_frame(folder / ("_MG_%04d.jpg" % n), scene(seed, blur),
                                         t + timedelta(seconds=40 * k))
            n += 1
        t = BASE + timedelta(minutes=20)
        for k, seed in enumerate((7, 8)):
            self.files["c%d" % k] = save_frame(folder / ("IMG_%04d.jpg" % n), scene(seed),
                                               t + timedelta(seconds=40 * k))
            n += 1


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cull_test_")
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()


class TestScanShoot(_TempCase):

    def setUp(self) -> None:
        super().setUp()
        self.shoot = Shoot(self.photos)
        self.before = snapshot(self.photos)
        self.res = cull.scan(self.photos, use_faces=False, workers=4)

    def by_name(self, key: str) -> cull.ImageRecord:
        path = self.shoot.files[key]
        return next(r for r in self.res.images if r.path.name == path.name)

    def test_scan_never_touches_the_photo_folder(self):
        self.assertEqual(snapshot(self.photos), self.before)

    def test_every_file_gets_exactly_one_record(self):
        self.assertEqual(len(self.res.images), len(self.shoot.files))
        self.assertEqual([r.capture_order for r in self.res.images],
                         list(range(len(self.res.images))))
        self.assertEqual(self.res.mode, cull.MODE_NO_FACES)
        self.assertEqual(self.res.time_basis, "время съёмки")

    def test_capture_order_follows_the_clock_not_the_file_name(self):
        names = [r.path.name for r in self.res.images]
        want = [self.shoot.files[k].name for k in
                ["burst0", "burst1", "burst2", "burst3", "burst4", "a_single0", "a_single1",
                 "b0", "b_soft", "b2", "c0", "c1"]]
        self.assertEqual(names, want)
        self.assertNotEqual(names, sorted(names))
        self.assertAlmostEqual(self.res.images[1].t_rel, 0.3, places=2)
        self.assertAlmostEqual(self.res.images[-1].t_rel, 1240.0, places=2)

    def test_burst_of_five_yields_one_representative_the_sharpest(self):
        ids = {self.by_name("burst%d" % k).burst_id for k in range(5)}
        self.assertEqual(len(ids), 1)
        burst = self.res.bursts[ids.pop()]
        self.assertEqual(burst.size, 5)
        rep = self.res.images[burst.representative]
        self.assertEqual(rep.path.name, self.shoot.files["burst1"].name)   # blur 0
        # The rest stay reachable, ordered sharpest-first by blur radius.
        members = [m.path.name for m in self.res.burst_members(burst.id)]
        want = [self.shoot.files["burst%d" % k].name for k in (1, 2, 4, 0, 3)]
        self.assertEqual(members, want)
        sugg = cull.suggest(self.res, None)
        self.assertEqual(sum(1 for r in sugg if r.burst_id == burst.id), 1)

    def test_singles_are_their_own_bursts(self):
        self.assertEqual(len(self.res.bursts), 8)

    def test_segments_come_from_the_pauses(self):
        self.assertEqual(len(self.res.segments), 3)
        seg_of = lambda k: self.by_name(k).segment_id           # noqa: E731
        self.assertEqual({seg_of("burst%d" % k) for k in range(5)} | {seg_of("a_single1")}, {0})
        self.assertEqual({seg_of("b0"), seg_of("b_soft"), seg_of("b2")}, {1})
        self.assertEqual({seg_of("c0"), seg_of("c1")}, {2})

    def test_suggestions_cover_every_segment_even_when_few(self):
        for n in (3, 4, 5, 8):
            with self.subTest(n=n):
                picks = cull.suggest(self.res, n)
                self.assertEqual(len(picks), n)
                self.assertEqual({r.segment_id for r in picks}, {0, 1, 2})
                self.assertTrue(all(r.is_representative for r in picks))

    def test_flags_sink_but_never_remove(self):
        soft = self.by_name("b_soft")
        self.assertIn("soft", soft.flags)
        self.assertIn("нерезкий", soft.flags_ru)
        everything = cull.suggest(self.res, None)
        self.assertIn(soft, everything)
        self.assertEqual(everything[-1], soft)          # the only flagged frame: last
        # Segment B gets one pick out of three: it must not be the soft frame.
        picks = cull.suggest(self.res, 3)
        self.assertNotIn(soft, picks)
        order = cull.full_order(self.res)
        self.assertEqual(sorted(r.capture_order for r in order),
                         list(range(len(self.res.images))))

    def test_suggest_is_capped_and_never_invents_frames(self):
        self.assertEqual(len(cull.suggest(self.res, 1000)), len(self.res.bursts))
        self.assertEqual(cull.suggest(self.res, 0), [])

    def test_thumbnails_are_plain_pil_images(self):
        res = cull.scan([self.shoot.files["c0"]], use_faces=False, thumb_px=96)
        th = res.images[0].thumbnail
        self.assertIsInstance(th, Image.Image)
        self.assertLessEqual(max(th.size), 96)

    def test_process_pool_gives_the_same_answer(self):
        res = cull.scan(self.photos, use_faces=False, workers=2, executor="process")
        # The pool must really have worked: a broken pool falls back to threads
        # with the same answer, which would hide a spawn-unsafe worker.
        self.assertFalse(any(PROCESS_FALLBACK in n for n in res.notes), res.notes)
        self.assertEqual([r.path.name for r in res.images],
                         [r.path.name for r in self.res.images])
        self.assertEqual([r.burst_id for r in res.images],
                         [r.burst_id for r in self.res.images])


class TestRobustness(_TempCase):

    def test_unreadable_file_keeps_a_row_and_sinks(self):
        save_frame(self.photos / "IMG_0001.jpg", scene(1), BASE)
        save_frame(self.photos / "IMG_0002.jpg", scene(2), BASE + timedelta(seconds=30))
        (self.photos / "IMG_0003.jpg").write_bytes(b"\xff\xd8 not a jpeg at all")
        res = cull.scan(self.photos, use_faces=False, workers=2)
        self.assertEqual(len(res.images), 3)
        bad = next(r for r in res.images if r.path.name == "IMG_0003.jpg")
        self.assertIn("unreadable", bad.flags)
        self.assertTrue(bad.error)
        self.assertEqual(cull.suggest(res, None)[-1], bad)
        self.assertTrue(any("прочитать" in n for n in res.notes))

    def test_without_clock_order_falls_back_to_file_numbers(self):
        for k in (3, 1, 2):
            scene(k).save(self.photos / ("IMG_%04d.jpg" % k), quality=90)
        res = cull.scan(self.photos, use_faces=False, workers=1)
        self.assertEqual([r.path.name for r in res.images],
                         ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"])
        self.assertEqual(res.time_basis, "порядок файлов")
        self.assertIsNone(res.images[0].t_rel)

    def test_two_jpegs_with_one_stem_are_both_kept(self):
        for name, seed in (("EVENT_1.jpg", 1), ("EVENT_1.jpeg", 2), ("EVENT_2.jpg", 3)):
            save_frame(self.photos / name, scene(seed), BASE + timedelta(seconds=30 * seed))
        items = cull._collect(self.photos)
        self.assertEqual(sorted(p.name for p, _raw in items),
                         ["EVENT_1.jpeg", "EVENT_1.jpg", "EVENT_2.jpg"])
        res = cull.scan(self.photos, use_faces=False, workers=2)
        self.assertEqual(sorted(r.path.name for r in res.images),
                         ["EVENT_1.jpeg", "EVENT_1.jpg", "EVENT_2.jpg"])

    def test_collect_from_a_list_keeps_extra_raws_too(self):
        items = cull._collect([Path("a/IMG_1.CR2"), Path("a/IMG_1.jpg"), Path("a/img_1.cr2"),
                               Path("b/IMG_1.jpg")])
        got = [(p.as_posix(), raw.as_posix() if raw else None) for p, raw in items]
        if sys.platform == "win32":
            # One file under two spellings on a case-insensitive volume: kept once.
            self.assertEqual(got, [("a/IMG_1.jpg", "a/IMG_1.CR2"), ("b/IMG_1.jpg", None)])
        else:
            self.assertEqual(got, [("a/IMG_1.jpg", "a/IMG_1.CR2"), ("a/img_1.cr2", None),
                                   ("b/IMG_1.jpg", None)])

    def test_empty_folder(self):
        res = cull.scan(self.photos, use_faces=False)
        self.assertEqual(res.images, [])
        self.assertEqual(cull.suggest(res, 30), [])

    def test_raw_jpeg_pair_is_analysed_once_and_cr2_alone_is_read(self):
        import make_test_cr2
        save_frame(self.photos / "IMG_0001.jpg", scene(1), BASE)
        make_test_cr2.make_cr2(self.photos / "IMG_0001.CR2", preview_size=(480, 320),
                               raw_size=(480, 320), datetime_original="2011:04:23 21:00:00")
        make_test_cr2.make_cr2(self.photos / "IMG_0002.CR2", preview_size=(480, 320),
                               raw_size=(480, 320), datetime_original="2011:04:23 21:05:00")
        res = cull.scan(self.photos, use_faces=False, workers=2)
        self.assertEqual([r.path.name for r in res.images], ["IMG_0001.jpg", "IMG_0002.CR2"])
        self.assertEqual(res.images[0].pair_path.name, "IMG_0001.CR2")
        self.assertEqual(res.images[1].error, "")
        self.assertAlmostEqual(res.images[1].t_rel, 300.0, places=3)


class TestProcessPoolFailure(_TempCase):

    def test_broken_process_pool_falls_back_to_threads(self):
        from concurrent.futures.process import BrokenProcessPool
        for k in range(6):
            save_frame(self.photos / ("IMG_%04d.jpg" % k), scene(k),
                       BASE + timedelta(seconds=20 * k))

        class DeadPool:
            def __init__(self, *a, **kw) -> None:
                pass

            def submit(self, *a, **kw):
                raise BrokenProcessPool("нет процессов")

            def shutdown(self, *a, **kw) -> None:
                pass

        with mock.patch.object(cull, "ProcessPoolExecutor", DeadPool):
            res = cull.scan(self.photos, use_faces=False, workers=3, executor="process")
        self.assertEqual(len(res.images), 6)
        self.assertFalse(res.cancelled)
        self.assertTrue(any("потоках" in n for n in res.notes))

    def test_unknown_executor_is_an_error(self):
        with self.assertRaises(ValueError):
            cull.scan(self.photos, executor="gpu")


class TestWithoutOpenCV(_TempCase):

    def test_hidden_cv2_means_mode_without_faces(self):
        save_frame(self.photos / "IMG_0001.jpg", scene(1), BASE)
        save_frame(self.photos / "IMG_0002.jpg", scene(1, 2.0), BASE + timedelta(seconds=1))
        with mock.patch.dict(sys.modules, {"cv2": None}):
            # A fresh copy of the module, imported while cv2 cannot be imported.
            spec = importlib.util.spec_from_file_location("cull_no_cv2", HERE / "cull.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["cull_no_cv2"] = mod
            try:
                spec.loader.exec_module(mod)
                self.assertEqual(mod.face_backend()[0], mod.MODE_NO_FACES)
                res = mod.scan(self.photos, workers=2)          # use_faces=None: auto
            finally:
                sys.modules.pop("cull_no_cv2", None)
        self.assertEqual(res.mode, "без лиц")
        self.assertTrue(any("OpenCV" in n for n in res.notes))
        self.assertTrue(all(r.face_box is None for r in res.images))
        self.assertFalse(any("no_face" in r.flags for r in res.images))
        self.assertEqual(len(res.bursts), 1)
        self.assertEqual(res.images[res.bursts[0].representative].path.name, "IMG_0001.jpg")
        with mock.patch.object(cull, "_cascade_path", return_value=None):
            self.assertEqual(cull.find_faces(scene(1)), [])


class TestFindFaces(unittest.TestCase):

    def test_no_faces_in_abstract_blocks_and_fractions_otherwise(self):
        self.assertEqual(cull.find_faces(scene(3, size=(1200, 800))), [])
        fake = [(300, 100, 60, 60), (40, 40, 30, 30)]
        with mock.patch.object(cull, "_cascade_path", return_value="x"),                 mock.patch.object(cull, "_detect_faces", return_value=fake),                 mock.patch.object(cull, "_plausible_face", return_value=True):
            faces = cull.find_faces(scene(3, size=(1296, 864)))
        # The picture is shrunk to 648 px for analysis; fractions do not care.
        self.assertEqual(len(faces), 2)
        x, y, w, h = faces[0]
        self.assertAlmostEqual(x, 300 / 648, places=3)
        self.assertAlmostEqual(w, 60 / 648, places=3)
        self.assertAlmostEqual(h, 60 / 432, places=3)


class TestCancel(_TempCase):

    def setUp(self) -> None:
        super().setUp()
        for k in range(24):
            save_frame(self.photos / ("IMG_%04d.jpg" % k), scene(k),
                       BASE + timedelta(seconds=20 * k))

    def test_cancel_from_report_stops_early(self):
        for executor in ("thread", "process"):
            with self.subTest(executor=executor):
                ev = threading.Event()
                calls: list[tuple[int, int]] = []

                def report(done: int, total: int) -> None:
                    calls.append((done, total))
                    if done >= 3:
                        ev.set()

                res = cull.scan(self.photos, use_faces=False, workers=2, executor=executor,
                                report=report, cancel_event=ev)
                self.assertTrue(res.cancelled)
                self.assertLess(len(res.images), 24)
                self.assertGreaterEqual(len(res.images), 3)
                self.assertEqual(calls[0][1], 24)
                self.assertTrue(any("Отменено" in n for n in res.notes))
                self.assertFalse(any(PROCESS_FALLBACK in n for n in res.notes), res.notes)

    def test_cancel_before_start_reads_nothing(self):
        ev = threading.Event()
        ev.set()
        for workers in (1, 4):
            with self.subTest(workers=workers):
                res = cull.scan(self.photos, use_faces=False, workers=workers, cancel_event=ev)
                self.assertTrue(res.cancelled)
                self.assertEqual(res.images, [])

    def test_report_counts_every_file(self):
        calls: list[tuple[int, int]] = []
        res = cull.scan(self.photos, use_faces=False, workers=3,
                        report=lambda d, t: calls.append((d, t)))
        self.assertFalse(res.cancelled)
        self.assertEqual(calls[-1], (24, 24))
        self.assertEqual([d for d, _ in calls], list(range(1, 25)))


class TestExport(_TempCase):

    def setUp(self) -> None:
        super().setUp()
        self.a = save_frame(self.photos / "IMG_0001.jpg", scene(1), BASE)
        self.b = save_frame(self.photos / "IMG_0002.jpg", scene(2), BASE)
        other = self.root / "other"
        other.mkdir()
        self.c = save_frame(other / "IMG_0001.jpg", scene(3), BASE)   # same name as a
        self.out = self.root / "out"

    def test_copies_and_never_moves(self):
        before = snapshot(self.photos)
        items = cull.export_selection([self.a, self.b], self.out)
        self.assertEqual(snapshot(self.photos), before)
        self.assertTrue(all(not i.error for i in items))
        for item in items:
            self.assertTrue(item.src.exists())
            self.assertEqual(item.dst.read_bytes(), item.src.read_bytes())
            self.assertFalse(item.renamed)

    def test_never_overwrites_and_resolves_collisions(self):
        self.out.mkdir()
        (self.out / "IMG_0001.jpg").write_bytes(b"already here")
        (self.out / "img_0001_2.JPG").write_bytes(b"also here")
        items = cull.export_selection([self.a, self.c, self.a], self.out)
        self.assertEqual(len(items), 2)                               # a listed twice
        self.assertEqual((self.out / "IMG_0001.jpg").read_bytes(), b"already here")
        self.assertEqual((self.out / "img_0001_2.JPG").read_bytes(), b"also here")
        names = [i.dst.name for i in items]
        self.assertEqual(len({n.lower() for n in names}), 2)
        self.assertNotIn("img_0001.jpg", {n.lower() for n in names})
        self.assertNotIn("img_0001_2.jpg", {n.lower() for n in names})
        self.assertTrue(all(i.renamed for i in items))
        self.assertEqual(items[0].dst.read_bytes(), self.a.read_bytes())
        self.assertEqual(items[1].dst.read_bytes(), self.c.read_bytes())

    def test_second_export_to_same_folder_adds_copies(self):
        cull.export_selection([self.a], self.out)
        second = cull.export_selection([self.a], self.out)
        self.assertEqual(second[0].dst.name, "IMG_0001_2.jpg")
        self.assertEqual(len(list(self.out.iterdir())), 2)

    def test_move_is_refused(self):
        with self.assertRaises(ValueError):
            cull.export_selection([self.a], self.out, mode="move")
        self.assertTrue(self.a.exists())
        self.assertFalse(self.out.exists())

    def test_export_into_the_photo_folder_is_refused(self):
        before = snapshot(self.photos)
        with self.assertRaises(ValueError):
            cull.export_selection([self.a], self.photos)
        self.assertEqual(snapshot(self.photos), before)

    def test_missing_source_is_reported_not_fatal(self):
        items = cull.export_selection([self.photos / "nope.jpg", self.a], self.out)
        self.assertTrue(items[0].error)
        self.assertIsNone(items[0].dst)
        self.assertEqual(items[1].dst.name, "IMG_0001.jpg")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["IMG_0001.jpg"])


class TestSelectionFile(_TempCase):

    def test_round_trip_with_cyrillic_paths(self):
        folder = self.root / "Съёмка"
        folder.mkdir()
        a = save_frame(folder / "IMG_0001.jpg", scene(1), BASE)
        target = self.root / "отбор" / "выбор.json"
        cull.save_selection(target, [a], note="награждение")
        self.assertEqual([p.resolve() for p in cull.load_selection(target)], [a.resolve()])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["IMG_0001.jpg"])
        self.assertIn(b"\xd0", target.read_bytes())                 # UTF-8, not \\u escapes

    def test_refuses_to_write_into_the_photo_folder(self):
        a = save_frame(self.photos / "IMG_0001.jpg", scene(1), BASE)
        with self.assertRaises(ValueError):
            cull.save_selection(self.photos / "sel.json", [a])
        self.assertEqual(sorted(p.name for p in self.photos.iterdir()), ["IMG_0001.jpg"])

    def test_foreign_json_is_rejected(self):
        p = self.root / "x.json"
        p.write_text('{"files": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            cull.load_selection(p)


def fake_raw(name: str, t: float, *, sharp: float, ahash: int, look: int,
             faces: list | None = None) -> dict:
    """Измерения одного кадра в том виде, в каком их отдаёт cull._analyse_one.

    look - номер «вида»: у разных видов гистограммы не пересекаются, а подписи
    ортогональны, так что похожими кадры делают только ahash и время.
    """
    sig = np.zeros(cull.SIGNATURE_PX ** 2, np.float32)
    sig[look] = 1.0
    hist = np.zeros(96)
    hist[look % 96] = 1.0
    return {"path": "shoot/%s.jpg" % name, "error": "", "t": t, "w": 648, "h": 432,
            "lapvar": 5000.0, "frame_sharp": sharp, "ahash": ahash, "signature": sig,
            "hist": hist, "faces": faces or []}


def far_hash(k: int) -> int:
    """64-битный хэш: у разных k далеко друг от друга (случайные биты)."""
    return int(np.random.default_rng(1000 + k).integers(0, 2 ** 63))


def flip_bits(h: int, n: int) -> int:
    """Тот же хэш с n изменёнными младшими битами (расстояние Хэмминга n)."""
    return h ^ ((1 << n) - 1)


class TestGroupingAndSuggestionRules(unittest.TestCase):
    """Правила серий и предложения на измерениях без файлов."""

    def build(self, raw: list, mode: str = cull.MODE_NO_FACES) -> cull.CullResult:
        return cull._build_result(raw, {r["path"]: None for r in raw}, mode, [])

    def test_hand_held_reframe_within_seconds_is_one_burst(self):
        base = far_hash(0)
        raw = [fake_raw("a", 0.0, sharp=1.0, ahash=base, look=1),
               fake_raw("b", 0.8, sharp=1.2, ahash=flip_bits(base, 18), look=2),
               fake_raw("c", 1.9, sharp=0.9, ahash=flip_bits(base, 15), look=3),
               # the same distance, but 3 s later: a new view, not the same moment
               fake_raw("d", 4.9, sharp=1.1, ahash=base, look=4),
               # 1 s later, but a different picture altogether
               fake_raw("e", 5.9, sharp=1.1, ahash=far_hash(5), look=5)]
        self.assertEqual(cull._hamming(raw[1]["ahash"], raw[2]["ahash"]), 3)
        self.assertEqual(cull._hamming(raw[2]["ahash"], raw[3]["ahash"]), 15)
        self.assertGreater(cull._hamming(raw[3]["ahash"], raw[4]["ahash"]), 22)
        res = self.build(raw)
        self.assertEqual([len(b.members) for b in res.bursts], [3, 1, 1])

    def test_no_second_pick_seconds_after_the_first_while_the_segment_has_more(self):
        raw = [fake_raw("a", 0.0, sharp=3.0, ahash=far_hash(1), look=1),
               fake_raw("b", 5.0, sharp=2.0, ahash=far_hash(2), look=2),
               fake_raw("c", 120.0, sharp=1.0, ahash=far_hash(3), look=3)]
        res = self.build(raw)
        self.assertEqual(len(res.bursts), 3)
        self.assertEqual({r.path.stem for r in cull.suggest(res, 2)}, {"a", "c"})
        # Nothing vanishes: every representative is there when asked for.
        self.assertEqual(len(cull.suggest(res, None)), 3)
        self.assertEqual(len(cull.suggest(res, 3)), 3)

    def test_slots_a_repetitive_segment_cannot_fill_go_elsewhere(self):
        raw = [fake_raw("s1_%d" % k, 3.0 * k, sharp=2.0 + k, ahash=far_hash(k), look=k)
               for k in range(4)]
        raw += [fake_raw("s2_%d" % k, 1000.0 + 100.0 * k, sharp=1.0 + k,
                         ahash=far_hash(10 + k), look=10 + k) for k in range(3)]
        res = self.build(raw)
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(len(res.bursts), 7)
        picks = cull.suggest(res, 3)
        by_seg = [sum(1 for r in picks if r.segment_id == s) for s in (0, 1)]
        self.assertEqual(by_seg, [1, 2])
        times = sorted(r.t_rel for r in picks)
        self.assertTrue(all(b - a > cull.NEAR_DUP_S for a, b in zip(times, times[1:])))

    def test_a_detector_miss_does_not_hand_the_burst_to_the_softer_twin(self):
        face = [{"box": (200, 100, 60, 60), "ten_n": 0.08, "clip_hi": 0.0, "mean": 120.0}]
        h = far_hash(1)
        raw = [fake_raw("soft_with_face", 0.0, sharp=0.5, ahash=h, look=1, faces=face),
               fake_raw("sharp_missed", 0.3, sharp=2.0, ahash=h, look=1),
               fake_raw("mid_missed", 0.6, sharp=1.0, ahash=h, look=1)]
        res = self.build(raw, cull.MODE_FACES)
        self.assertEqual(len(res.bursts), 1)
        rep = res.images[res.bursts[0].representative]
        self.assertEqual(rep.path.stem, "sharp_missed")
        self.assertTrue(all("no_face" not in r.flags for r in res.images))
        self.assertTrue(all(r.face_box is not None for r in res.images))

    def test_a_sign_on_a_beige_card_is_not_a_face(self):
        skin = np.zeros((40, 40, 3), np.uint8)
        skin[...] = (224, 172, 140)
        grey = np.full((40, 40, 3), 128, np.uint8)
        self.assertTrue(cull._plausible_face(skin, 0.12))
        self.assertFalse(cull._plausible_face(skin, 0.59))       # printed letters
        self.assertFalse(cull._plausible_face(grey, 0.12))       # a board


class TestAllocation(unittest.TestCase):

    def test_every_segment_first_then_proportional(self):
        q = cull._allocate([17, 11, 14, 2, 19, 1, 16], 14)
        self.assertEqual(sum(q), 14)
        self.assertTrue(all(x >= 1 for x in q))
        self.assertLessEqual(q[5], 1)
        self.assertLessEqual(max(q), 3)

    def test_fewer_picks_than_segments_spread_over_the_event(self):
        q = cull._allocate([5, 5, 5, 5, 5, 5], 2)
        self.assertEqual(sum(q), 2)
        self.assertEqual(q.index(1) < 3, True)
        self.assertEqual(q[3:].count(1), 1)

    def test_never_above_capacity(self):
        self.assertEqual(cull._allocate([1, 2], 10), [1, 2])


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Имя -> (размер, mtime_ns) для каждого файла папки: проверка «ничего не тронуто»."""
    out = {}
    for p in sorted(folder.iterdir()):
        st = p.stat()
        out[p.name] = (st.st_size, st.st_mtime_ns)
    return out


if __name__ == "__main__":
    unittest.main()
