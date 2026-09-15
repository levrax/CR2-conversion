#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cr2_convert — командный интерфейс к ядру cr2_core.

Запуск:

    python cr2_convert.py ПАПКА_ИЛИ_ФАЙЛЫ [опции]

Что делает программа: достаёт из CR2 встроенное превью JPEG (у современных
камер оно полного размера) и кладёт его рядом как .jpg, копируя байты без
пересжатия.  Это кадр в том виде, как его отрисовала сама камера.
Рецепт Canon DPP программа применить НЕ может — см. --help.

Здесь нет ни одной строчки разбора CR2: весь разбор, извлечение превью и
сборка EXIF живут в cr2_core.  Этот файл занимается только аргументами,
выводом в консоль Windows и кодами возврата.

Коды возврата:
    0   всё успешно
    1   были ошибки конвертации
    2   неверные аргументы командной строки
    130 прервано пользователем (Ctrl+C)
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Sequence

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130

# Ядро лежит рядом со скриптом.  При запуске через drag-and-drop текущий
# каталог — это каталог перетащенных файлов, а не каталог скрипта, поэтому
# полагаться на cwd нельзя.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# ---------------------------------------------------------------------------
# Безопасный вывод в консоль Windows
# ---------------------------------------------------------------------------
#
# sys.stdout.encoding в Windows бывает разным:
#   * реальная консоль (Python 3.6+)  -> 'utf-8' (через WriteConsoleW);
#   * вывод перенаправлен в файл/пайп -> кодировка локали, на русской системе
#     обычно 'cp1251', в старых сборках 'cp866';
#   * PYTHONIOENCODING / chcp могут поменять это на что угодно, вплоть до
#     'ascii' (например, при запуске из планировщика задач).
# Ни cp866, ни cp1251 не содержат ни «×» (U+00D7), ни «→» (U+2192), ни «…».
# Поэтому символы выбираются ПОСЛЕ проверки кодировки потока, а сама запись
# дополнительно защищена от UnicodeEncodeError.

#: Типографика, которой нет в однобайтных кодировках Windows: в cp866 нет ни
#: одного из этих символов, в cp1251 нет «×» и «→».  errors='replace' превратил
#: бы их в «?» прямо посреди русской фразы («по умолчанию ? рядом с
#: исходником»), поэтому подставляем ASCII-аналоги сами.
_PUNCT = {
    "«": '"', "»": '"', "„": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'", "—": "-", "–": "-", "‑": "-", "−": "-",
    "…": "...", "×": "x", "→": "->", "№": "N", "•": "*",
    " ": " ", "─": "-",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_TRANSLIT.update({k.upper(): v.capitalize() for k, v in _TRANSLIT.items() if v})
_TRANSLIT.update(_PUNCT)


def _translit(text: str) -> str:
    """Латиница вместо кириллицы — последний рубеж для ASCII-потоков."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


class _NullStream:
    """Заглушка: под pythonw.exe sys.stdout/sys.stderr равны None."""

    encoding = "ascii"

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


class Console:
    """Обёртка над потоком вывода, которая никогда не падает на кодировке."""

    def __init__(self, stream) -> None:
        self.stream = stream if stream is not None else _NullStream()
        # errors='replace' убирает исключение ещё на уровне io; если поток
        # этого не умеет (не TextIOWrapper) — ниже есть ручной запасной путь.
        try:
            self.stream.reconfigure(errors="replace")
        except Exception:
            pass
        self.encoding = (getattr(self.stream, "encoding", None) or "ascii")
        self.cyrillic = self._can("Я")
        # Кириллица в поток проходит, а типографика — нет: свернуть только её.
        # Иначе errors='replace' ставит «?» в середину читаемой русской фразы.
        fold = {ch: alt for ch, alt in _PUNCT.items() if not self._can(ch)}
        self.fold = str.maketrans(fold) if fold else None
        self.times = "\u00d7" if self._can("\u00d7") else "x"
        self.arrow = "\u2192" if self._can("\u2192") else "->"
        self.ell = "\u2026" if self._can("\u2026") else "~"
        self.rule = "\u2500" if self._can("\u2500") else "-"
        try:
            self.width = max(60, shutil.get_terminal_size(fallback=(100, 25)).columns - 1)
        except Exception:
            self.width = 99

    def _can(self, text: str) -> bool:
        try:
            text.encode(self.encoding)
            return True
        except (UnicodeError, LookupError, TypeError, ValueError):
            return False

    # -- файлоподобный интерфейс: argparse печатает справку прямо сюда -------
    def write(self, text: str) -> int:
        if not self.cyrillic:
            text = _translit(text)
        elif self.fold is not None:
            text = text.translate(self.fold)
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            safe = text.encode(self.encoding, "replace").decode(self.encoding, "replace")
            try:
                self.stream.write(safe)
            except Exception:
                return 0
        except (OSError, ValueError):
            return 0
        return len(text)

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:
            pass

    def line(self, text: str = "") -> None:
        self.write(text + "\n")
        self.flush()

    def fit(self, text: str, width: int | None = None) -> str:
        """Обрезать строку до ширины консоли, чтобы не было переносов."""
        w = self.width if width is None else width
        if len(text) <= w:
            return text
        return text[: max(1, w - 1)] + self.ell

    def dims(self, w: int, h: int) -> str:
        return "%d%s%d" % (w, self.times, h) if w and h else "-"


OUT = Console(sys.stdout)
ERR = Console(sys.stderr)


def _die(message: str, code: int = EXIT_USAGE) -> "NoReturn":  # type: ignore[valid-type]
    ERR.line(message)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Импорт ядра
# ---------------------------------------------------------------------------

try:
    from cr2_core import (
    plan_destinations,
    validate_suffix,  # noqa: E402
        CR2_EXTS,
        ConvertOptions,
        Cr2Info,
        Preview,
        Result,
        convert_many,
        convert_one,
        find_cr2,
        has_pillow,
        has_rawpy,
        probe,
    )
except ImportError as _exc:  # pragma: no cover - зависит от раскладки файлов
    _die("Не найден модуль cr2_core (%s).\n"
         "Положите cr2_convert.py в одну папку с cr2_core.py." % _exc, EXIT_USAGE)


_MODE_RU = {
    "lossless": "без перекодир.",
    "reencode": "перекодировано",
    "raw": "RAW-декодир.",
    "": "",
}


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return "%.1f МБ" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f КБ" % (n / 1024.0)
    return "%d Б" % n


def _fmt_secs(sec: float) -> str:
    if sec >= 60:
        return "%d мин %02d с" % (int(sec // 60), int(sec % 60))
    return "%.1f с" % sec


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Русский заголовок «Использование:» вместо английского «usage:»."""

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix="Использование: ")


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest,
                         default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        # Печатаем через Console: справка на русском не должна падать
        # на ASCII-потоке.
        parser.print_help(OUT)
        OUT.flush()
        raise SystemExit(EXIT_OK)


class _VersionAction(argparse.Action):
    """argparse.version пишет прямо в sys.stdout, минуя Console.

    Из-за этого «есть»/«нет» превращались в «????» на ASCII-потоке вместо
    транслитерации.  Печатаем через ту же обёртку, что и всю остальную справку.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest,
                         default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        OUT.line("cr2_convert %s (Python %d.%d.%d, Pillow: %s, rawpy: %s)"
                 % (__version__, sys.version_info[0], sys.version_info[1],
                    sys.version_info[2],
                    "есть" if has_pillow() else "нет",
                    "есть" if has_rawpy() else "нет"))
        raise SystemExit(EXIT_OK)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> "NoReturn":  # type: ignore[valid-type]
        ERR.line("Ошибка аргументов: %s" % message)
        self.print_usage(ERR)
        ERR.line("Подсказка: python cr2_convert.py --help")
        raise SystemExit(EXIT_USAGE)

    def exit(self, status: int = 0, message: str | None = None) -> "NoReturn":  # type: ignore[valid-type]
        if message:
            (OUT if status == EXIT_OK else ERR).write(message)
        raise SystemExit(status)


_EPILOG = """\
Примеры:
  python cr2_convert.py D:\\Фото\\Съёмка
  python cr2_convert.py D:\\Фото -o D:\\JPEG --no-recursive
  python cr2_convert.py IMG_0001.CR2 IMG_0002.CR2 --overwrite
  python cr2_convert.py "D:\\Фото\\*.CR2" --max-side 2048 --quality 90 --reencode
  python cr2_convert.py D:\\Фото --dry-run

Папку или файлы можно просто перетащить на «Перетащи сюда CR2.bat».

Что именно получается:
  Из CR2 извлекается ВСТРОЕННОЕ превью JPEG и сохраняется как .jpg. У камер
  последних поколений (например, Canon EOS 550D) это превью полного размера —
  ровно столько же пикселей, сколько в самом RAW. Байты копируются как есть,
  без повторного сжатия: потерь качества нет и работает это очень быстро.
  Картинка получается такой, какой её отрисовала сама камера: стиль
  изображения, баланс белого, контраст, шумоподавление — всё как снято.
  У старых камер встроенное превью бывает уменьшенным; программа показывает
  долю от полного кадра и предупреждает, если превью мелкое (см. --min-ratio).

Про Canon DPP — честно:
  Применить рецепт Canon DPP (кроп, поворот, экспозиция, стиль) эта программа
  НЕ может и не научится: формат рецепта закрыт, отрисовать его умеет только
  движок самой Canon. Если рецепт в файле найден, программа отметит это в
  выводе — и получить нужный вид можно лишь в самой Canon DPP, командами
  «Конвертировать и сохранить» (Convert and save) или «Пакетная обработка»
  (Batch process).
  Если рецепт не найден, это значит ровно одно: рецепта нет в самом файле.
  Утверждать, что снимок нигде не редактировали, программа не может.
"""


def build_parser() -> _Parser:
    p = _Parser(
        prog="cr2_convert.py",
        usage="python cr2_convert.py ПАПКА_ИЛИ_ФАЙЛЫ [опции]",
        description="CR2 в JPEG без потерь: из файла достаётся встроенное превью "
                    "(у современных камер — полного размера) и сохраняется как .jpg "
                    "без пересжатия. Картинка такая, как её отрисовала камера. "
                    "Рецепт Canon DPP программа применить не может.",
        epilog=_EPILOG,
        formatter_class=_Formatter,
        add_help=False,
    )
    p._positionals.title = "Аргументы"
    p._optionals.title = "Опции"

    p.add_argument(
        "paths", nargs="*", metavar="ПАПКА_ИЛИ_ФАЙЛЫ",
        help="одна или несколько папок, файлов .CR2 или масок (*.CR2). "
             "Папки обходятся рекурсивно.",
    )

    g_out = p.add_argument_group("Куда и как сохранять")
    g_out.add_argument("-o", "--out", metavar="ПАПКА", default=None,
                       help="папка для результатов (по умолчанию — рядом с исходником)")
    g_out.add_argument("--suffix", metavar="ТЕКСТ", default="",
                       help="приписать к имени файла, например --suffix _preview")
    g_out.add_argument("--overwrite", action="store_true",
                       help="перезаписывать уже существующие .jpg "
                            "(по умолчанию такие файлы пропускаются)")

    g_img = p.add_argument_group("Изображение")
    g_img.add_argument("-q", "--quality", type=int, metavar="1..100", default=95,
                       help="качество JPEG при перекодировании (по умолчанию 95). "
                            "Без перекодирования не используется.")
    g_img.add_argument("--max-side", type=int, metavar="ПИКСЕЛИ", default=0,
                       help="уменьшить так, чтобы большая сторона была не больше "
                            "указанной (0 — не уменьшать). Включает перекодирование, "
                            "нужна библиотека Pillow.")
    g_img.add_argument("--reencode", action="store_true",
                       help="принудительно пересжать превью через Pillow "
                            "(по умолчанию байты копируются как есть, без потерь)")
    g_img.add_argument("--prefer-dpp-preview", action="store_true",
                       help="брать превью, записанное самим DPP в блок IHLData "
                            "(если оно есть). Оно МОЖЕТ отражать правки DPP, но это "
                            "нигде не документировано, размер обычно заметно меньше "
                            "полного кадра, а кадрирование может отличаться. "
                            "Заменой рендеру из DPP это не является.")
    g_img.add_argument("--rotate", action="store_true",
                       help="повернуть пиксели по EXIF Orientation и выставить тег в 1 "
                            "(включает перекодирование, нужна Pillow)")

    g_meta = p.add_argument_group("Метаданные")
    g_meta.add_argument("--no-exif", action="store_true",
                        help="не переносить EXIF в готовый JPEG")
    g_meta.add_argument("--keep-makernote", action="store_true",
                        help="перенести Canon MakerNote как есть (внимание: "
                             "его внутренние смещения станут недействительными)")
    g_meta.add_argument("--strip-gps", action="store_true",
                        help="удалить координаты GPS из EXIF")

    g_scan = p.add_argument_group("Поиск файлов и скорость")
    g_scan.add_argument("--no-recursive", action="store_true",
                        help="не заходить во вложенные папки")
    g_scan.add_argument("--workers", type=int, metavar="N", default=0,
                        help="число параллельных потоков (0 — выбрать автоматически)")
    g_scan.add_argument("--no-raw-fallback", action="store_true",
                        help="не пытаться декодировать RAW через rawpy, если "
                             "встроенного превью нет")
    g_scan.add_argument("--min-ratio", type=float, metavar="ДОЛЯ", default=0.4,
                        help="доля площади кадра, ниже которой превью считается "
                             "неполноразмерным и об этом предупреждается "
                             "(по умолчанию 0.4)")

    g_misc = p.add_argument_group("Прочее")
    g_misc.add_argument("--dry-run", action="store_true",
                        help="ничего не записывать на диск: только прочитать файлы и "
                             "показать таблицу (размер превью, размер кадра, доля, "
                             "найден ли рецепт DPP)")
    g_misc.add_argument("-v", "--verbose", action="count", default=0,
                        help="подробный вывод (-vv — ещё подробнее)")
    g_misc.add_argument("--version", action=_VersionAction,
                        help="показать версию и выйти")
    g_misc.add_argument("-h", "--help", action=_HelpAction,
                        help="показать эту справку и выйти")
    return p


def options_from_args(args: argparse.Namespace) -> ConvertOptions:
    """Ровно одно место, где аргументы превращаются в ConvertOptions."""
    return ConvertOptions(
        out_dir=Path(args.out) if args.out else None,
        quality=args.quality,
        max_side=args.max_side,
        lossless=not args.reencode,
        bake_rotation=args.rotate,
        copy_exif=not args.no_exif,
        keep_makernote=args.keep_makernote,
        strip_gps=args.strip_gps,
        overwrite=args.overwrite,
        suffix=args.suffix,
        min_preview_ratio=args.min_ratio,
        allow_raw_fallback=not args.no_raw_fallback,
        prefer_dpp_preview=args.prefer_dpp_preview,
    )


def validate(args: argparse.Namespace, parser: _Parser) -> None:
    if not 1 <= args.quality <= 100:
        parser.error("--quality должно быть от 1 до 100 (получено %d)" % args.quality)
    if args.max_side < 0:
        parser.error("--max-side не может быть отрицательным (получено %d)" % args.max_side)
    if args.max_side and args.max_side < 32:
        parser.error("--max-side меньше 32 пикселей не имеет смысла (получено %d)" % args.max_side)
    if not 0.0 <= args.min_ratio <= 1.0:
        parser.error("--min-ratio должно быть от 0.0 до 1.0 (получено %s)" % args.min_ratio)
    if not 0 <= args.workers <= 64:
        parser.error("--workers должно быть от 0 до 64 (получено %d)" % args.workers)
    suffix_problem = validate_suffix(args.suffix)
    if suffix_problem:
        parser.error("--suffix: %s" % suffix_problem)
    if args.out:
        out = Path(args.out)
        if out.exists() and not out.is_dir():
            parser.error("путь из -o/--out существует, но это не папка: %s" % out)


# ---------------------------------------------------------------------------
# Сбор входных путей (папки, файлы, маски, drag-and-drop)
# ---------------------------------------------------------------------------


def _glob_pattern(text: str) -> str:
    r"""Экранировать литеральную часть пути, оставив хвост с масками как есть.

    Папки вида «Съёмка [2024]» встречаются постоянно, а glob считает [..]
    классом символов, поэтому такая папка не находится вообще. Экранируем
    только компоненты ДО первого, в котором есть '*' или '?': иначе
    glob.escape превратит '**' в '[*][*]' и сломает рекурсивные маски.

    Осознанные границы:
      * если '*' и '?' нет нигде, экранируется весь путь целиком, включая имя
        файла, — поэтому настоящий класс символов (IMG_[0-9].CR2) этой маской
        не найдётся. На такой случай в collect_inputs есть запасной проход по
        неэкранированному тексту;
      * скобки в папке ПОСЛЕ маски (D:\Фото\20*\Съёмка [2024]\*.CR2) не
        экранируются: какой компонент литеральный, а какой — маска, glob
        выразить не даёт. Обычные пути из Проводника так не выглядят.
    """
    drive, rest = os.path.splitdrive(text)
    parts = rest.replace("/", os.sep).split(os.sep)
    idx = len(parts)
    for i, part in enumerate(parts):
        if "*" in part or "?" in part:
            idx = i
            break
    head = drive + os.sep.join(parts[:idx])
    tail = os.sep.join(parts[idx:])
    if not head:
        return text
    return os.path.join(glob.escape(head), tail) if tail else glob.escape(head)


def collect_inputs(raw: Sequence[str], recursive: bool,
                   on_problem: "Callable[[Path, OSError], None] | None" = None,
                   ) -> tuple[list[Path], list[str]]:
    """Развернуть аргументы в список .CR2.

    Понимает: папку (рекурсивно через find_cr2), обычный файл, маску с * и ?,
    а также абсолютные пути, которые Проводник подставляет при перетаскивании.

    Returns:
        (файлы без дублей в порядке появления, список сообщений о проблемах)
    """
    files: list[Path] = []
    problems: list[str] = []
    seen: set[str] = set()

    def report(path: Path, exc: OSError) -> None:
        # Каталог, который не удалось прочитать, раньше просто исчезал из
        # выборки: итог показывал «ошибок 0», хотя часть файлов не видели.
        problems.append("каталог недоступен: %s (%s)" % (path, exc))
        if on_problem is not None:
            on_problem(path, exc)

    def add(path: Path, complain: bool) -> None:
        if path.suffix.lower() not in CR2_EXTS:
            if complain:
                problems.append("не файл CR2, пропущен: %s" % path)
            return
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
        except (OSError, ValueError):
            key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        files.append(path)

    for item in raw:
        # Проводник иногда оставляет кавычки, а хвостовой '\' у папки ломает
        # разбор командной строки Windows ("C:\Фото\" -> C:\Фото").
        text = item.strip().strip('"')
        if not text:
            continue
        p = Path(text)
        try:
            is_dir, is_file = p.is_dir(), p.is_file()
        except OSError as exc:
            problems.append("недоступен путь %s (%s)" % (text, exc))
            continue
        if is_dir:
            found = find_cr2(p, recursive=recursive, on_problem=report)
            if not found:
                problems.append("в папке нет файлов .CR2: %s" % p)
            for f in found:
                add(f, complain=False)
        elif is_file:
            add(p, complain=True)
        elif any(ch in text for ch in "*?["):
            pattern = _glob_pattern(text)
            found_raw = glob.glob(pattern, recursive=True)
            if not found_raw and pattern != text:
                # Экранированный вариант ничего не дал: возможно, скобки были
                # настоящим классом символов (IMG_[0-9].CR2), а не именем
                # папки. Пробуем текст как есть — хуже уже не будет.
                found_raw = glob.glob(text, recursive=True)
            matches = sorted(found_raw, key=str.lower)
            if not matches:
                if not any(ch in text for ch in "*?"):
                    # Обычный путь, в котором просто есть скобки.
                    problems.append("путь не найден: %s" % text)
                else:
                    problems.append("маске ничего не соответствует: %s" % text)
            for m in matches:
                mp = Path(m)
                if mp.is_dir():
                    for f in find_cr2(mp, recursive=recursive, on_problem=report):
                        add(f, complain=False)
                else:
                    add(mp, complain=False)
        else:
            problems.append("путь не найден: %s" % text)
    return files, problems


# ---------------------------------------------------------------------------
# Вывод хода работы
# ---------------------------------------------------------------------------


class Reporter:
    """Печатает по одной выровненной строке на файл."""

    def __init__(self, con: Console, files: Sequence[Path], suffix: str, verbose: int) -> None:
        self.con = con
        self.total = len(files)
        self.verbose = verbose
        self.done = 0
        self.converted = 0
        self.skipped = 0
        self.errors = 0
        self.bytes_out = 0
        self.dpp = 0
        self.failures: list[tuple[Path, str]] = []
        self.idx_w = len(str(max(1, self.total)))
        src_w = max((len(p.name) for p in files), default=12)
        dst_w = max((len(p.stem) + len(suffix) + 4 for p in files), default=12)
        self.src_w = min(34, max(12, src_w))
        self.dst_w = min(34, max(12, dst_w))

    def _name(self, text: str, width: int) -> str:
        if len(text) > width:
            text = text[: width - 1] + self.con.ell
        return text.ljust(width)

    def on_result(self, res: Result) -> None:
        self.done += 1
        head = "[%*d/%d]" % (self.idx_w, self.done, self.total)
        src = self._name(res.src.name, self.src_w)
        if res.info is not None and res.info.has_dpp_recipe:
            self.dpp += 1

        if res.ok:
            self.converted += 1
            self.bytes_out += res.bytes_out
            dst = self._name(res.dst.name if res.dst else "?", self.dst_w)
            line = "%s %s %s %s %11s %9s  %s" % (
                head, src, self.con.arrow, dst,
                self.con.dims(res.width, res.height),
                _fmt_size(res.bytes_out),
                _MODE_RU.get(res.mode, res.mode),
            )
            if res.info is not None and res.info.has_dpp_recipe:
                # «есть», а не просто «рецепт DPP»: это пометка о НАЛИЧИИ
                # рецепта в файле, а не о том, что он применён.
                line += "  [есть рецепт DPP]"
        elif res.skipped:
            self.skipped += 1
            line = "%s %s %s %s" % (head, src, "ПРОПУСК", res.message)
        else:
            self.errors += 1
            self.failures.append((res.src, res.message))
            line = "%s %s %s  %s" % (head, src, "ОШИБКА", res.message)

        self.con.line(self.con.fit(line))

        if self.verbose >= 1 and res.ok and res.message:
            self.con.line(self.con.fit("        %s" % res.message))
        if self.verbose >= 2 and res.info is not None:
            info = res.info
            if info.camera or info.shot_at:
                self.con.line(self.con.fit("        камера: %s; снято: %s"
                                           % (info.camera or "?", info.shot_at or "?")))
            if info.previews:
                parts = ", ".join("%s %s" % (c.source, self.con.dims(c.width, c.height))
                                  for c in info.previews[:4])
                self.con.line(self.con.fit("        превью: %s" % parts))
            if info.recipe_hint:
                self.con.line(self.con.fit("        %s" % info.recipe_hint))

    def on_progress(self, done: int, total: int) -> None:  # noqa: D401 - хук ядра
        """Ядро зовёт этот хук после каждого файла; строку печатает on_result."""
        return None


# ---------------------------------------------------------------------------
# Запуск конвертации
# ---------------------------------------------------------------------------


def _cancelled_result(path: Path) -> Result:
    return Result(src=path, skipped=True, message="Отменено пользователем")


def _run_pool(files: Sequence[Path], opts: ConvertOptions, workers: int,
              on_result: Callable[[Result], None],
              cancel: threading.Event) -> list[Result]:
    """То же, что convert_many, но с ЯВНО заданным числом потоков.

    convert_many сам выбирает min(8, cpu_count) и не принимает это числом,
    поэтому при указанном --workers N здесь поднимается собственный пул.
    Вся работа по-прежнему делается ядром: пул только зовёт convert_one.
    """
    results: list[Result] = []

    # Тот же план имён, что и в convert_many: без него два файла с одинаковым
    # именем из разных папок пишутся в один .jpg и затирают друг друга.
    plan = plan_destinations(files, opts)

    def task(path: Path, dst: Path, note: str) -> Result:
        if cancel.is_set():
            return _cancelled_result(path)
        try:
            res = convert_one(path, opts, dst=dst, cancel=cancel)
        except Exception as exc:  # поток не должен уронить процесс
            return Result(src=path, ok=False,
                          message="Непредвиденная ошибка: %s: %s" % (type(exc).__name__, exc))
        if note:
            res.message = "%s; %s" % (note, res.message) if res.message else note
        return res

    ex = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="cr2cli")
    try:
        futures = [(src, ex.submit(task, src, dst, note)) for src, dst, note in plan]
        # Источник берём из самого плана, а не из files[i]: иначе любое
        # расхождение порядка между plan_destinations и files приписало бы
        # ошибку чужому файлу.
        for src, fut in futures:
            if cancel.is_set() and not fut.done():
                fut.cancel()
            try:
                res = fut.result()
            except CancelledError:
                res = _cancelled_result(src)
            except Exception as exc:
                res = Result(src=src, ok=False,
                             message="Непредвиденная ошибка: %s: %s" % (type(exc).__name__, exc))
            results.append(res)
            on_result(res)
    except BaseException:
        # Второй Ctrl+C: не ждать очередь, иначе процесс «зависнет» до конца.
        cancel.set()
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return results


def run_convert(files: Sequence[Path], opts: ConvertOptions, workers: int,
                rep: Reporter, cancel: threading.Event) -> list[Result]:
    if workers > 0:
        return _run_pool(files, opts, workers, rep.on_result, cancel)
    return convert_many(files, opts, on_result=rep.on_result,
                        on_progress=rep.on_progress, cancel=cancel)


# ---------------------------------------------------------------------------
# Режим --dry-run
# ---------------------------------------------------------------------------


def run_dry(files: Sequence[Path], con: Console, opts: ConvertOptions,
            verbose: int, cancel: threading.Event) -> tuple[int, int, int]:
    """Только probe(): таблица без единой записи на диск.

    Returns:
        (ok, с рецептом DPP, с ошибками)
    """
    infos: list[Cr2Info] = []
    for p in files:
        if cancel.is_set():
            break
        infos.append(probe(p))

    name_w = min(34, max((len(i.path.name) for i in infos), default=12))
    name_w = max(name_w, len("Файл"))
    header = "%s  %-13s %-13s %5s  %-9s %s" % (
        "Файл".ljust(name_w), "Превью", "Кадр (RAW)", "Доля", "Рецепт", "Камера")
    con.line(header)
    con.line(con.rule * min(con.width, len(header)))

    ok = dpp = bad = 0
    for info in infos:
        name = info.path.name
        if len(name) > name_w:
            name = name[: name_w - 1] + con.ell
        name = name.ljust(name_w)
        best = info.best
        if info.error or best is None:
            bad += 1
            con.line(con.fit("%s  %s" % (name, info.error or "нет пригодного встроенного JPEG")))
            continue
        ok += 1
        raw_px = info.raw_width * info.raw_height
        share = "%d%%" % round(100.0 * best.pixels / raw_px) if raw_px and best.pixels else "?"
        recipe = "ЕСТЬ" if info.has_dpp_recipe else "нет"
        if info.has_dpp_recipe:
            dpp += 1
        con.line(con.fit("%s  %-13s %-13s %5s  %-9s %s" % (
            name,
            con.dims(best.width, best.height),
            con.dims(info.raw_width, info.raw_height),
            share, recipe, info.camera or "?")))
        if verbose >= 1:
            con.line(con.fit("        источник превью: %s, смещение %d, длина %s; снято: %s"
                             % (best.source, best.offset, _fmt_size(best.length),
                                info.shot_at or "?")))
            if len(info.previews) > 1:
                parts = ", ".join("%s %s" % (c.source, con.dims(c.width, c.height))
                                  for c in info.previews[:4])
                con.line(con.fit("        все превью: %s" % parts))
        if verbose >= 2 and info.recipe_hint:
            con.line(con.fit("        %s" % info.recipe_hint))
    return ok, dpp, bad


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _warn_missing_deps(args: argparse.Namespace, con: Console) -> None:
    needs_pillow = args.reencode or args.rotate or args.max_side > 0
    if needs_pillow and not has_pillow():
        con.line("Внимание: для --reencode/--rotate/--max-side нужна библиотека Pillow.")
        con.line("          Установите её:  python -m pip install Pillow")
        con.line("")


def _print_dpp_notice(con: Console, found: int) -> None:
    """Единственное место, где программа говорит про рецепт Canon DPP.

    Формулировки намеренно осторожные: применить рецепт нельзя, а его
    отсутствие ничего не доказывает о том, правили снимок или нет.
    """
    con.line("")
    if found:
        con.line("Рецепт Canon DPP найден в %d файл(ах). Применить его эта программа НЕ может:"
                 % found)
        con.line("формат рецепта закрыт, отрисовать правки умеет только движок самой Canon.")
        con.line("Чтобы получить именно вид из DPP: откройте эти снимки в Canon Digital Photo")
        con.line("Professional и выберите «Конвертировать и сохранить» (Convert and save)")
        con.line("либо «Пакетная обработка» (Batch process).")
    else:
        con.line("Рецептов Canon DPP в этих файлах не найдено. Это значит только то,")
        con.line("что рецепта нет в самих файлах, — и ничего не говорит о том,")
        con.line("правили снимки где-то ещё или нет.")


def _print_summary(con: Console, rep: Reporter, elapsed: float, cancelled: bool) -> None:
    con.line(con.rule * min(con.width, 60))
    if cancelled:
        con.line("ПРЕРВАНО пользователем (Ctrl+C).")
    con.line("Итог: сконвертировано %d, пропущено %d, ошибок %d (всего файлов %d)"
             % (rep.converted, rep.skipped, rep.errors, rep.total))
    con.line("Время: %s; записано: %s" % (_fmt_secs(elapsed), _fmt_size(rep.bytes_out)))
    # Печатается всегда, а не только при найденном рецепте: вопрос «а мои
    # правки в DPP применились?» задают именно тогда, когда рецепта нет.
    if rep.converted:
        _print_dpp_notice(con, rep.dpp)
    if rep.failures:
        con.line("")
        con.line("Не удалось (%d):" % len(rep.failures))
        for src, msg in rep.failures[:20]:
            # Здесь НЕ обрезаем: это тот самый текст, где написано, что делать.
            con.line("  %s: %s" % (src.name, msg))
        if len(rep.failures) > 20:
            con.line("  ... и ещё %d" % (len(rep.failures) - 20))


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not argv:
        parser.print_help(OUT)
        OUT.line("")
        OUT.line("Не указано ни одного файла или папки.")
        return EXIT_USAGE

    args = parser.parse_args(argv)
    validate(args, parser)

    if not args.paths:
        parser.error("не указано ни одного файла или папки")

    opts = options_from_args(args)
    _warn_missing_deps(args, OUT)

    files, problems = collect_inputs(args.paths, recursive=not args.no_recursive)
    for text in problems:
        # Пути не обрезаем: пользователю нужно видеть, какой именно путь не подошёл.
        ERR.line("Внимание: %s" % text)
    if not files:
        ERR.line("Не найдено ни одного файла .CR2.")
        return EXIT_USAGE if problems else EXIT_ERRORS

    OUT.line("Найдено файлов .CR2: %d" % len(files))
    if not args.dry_run:
        OUT.line("Из CR2 извлекается встроенное превью JPEG и сохраняется как .jpg.")
        OUT.line("Байты копируются без пересжатия: потерь качества нет, картинка")
        OUT.line("такая, какой её отрисовала камера. Размер превью виден в колонке ниже.")
    if args.verbose >= 1:
        where = str(opts.out_dir) if opts.out_dir else "рядом с исходными файлами"
        OUT.line("Куда: %s; режим: %s; потоков: %s"
                 % (where,
                    "перекодирование" if not opts.lossless or opts.max_side or opts.bake_rotation
                    else "без перекодирования",
                    args.workers or "авто"))
    OUT.line("")

    cancel = threading.Event()

    def on_sigint(signum, frame):  # noqa: ANN001 - подпись задана signal
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        OUT.line("")
        OUT.line("Отмена: дожидаемся текущих файлов... (ещё раз Ctrl+C — выйти сразу)")

    try:
        previous = signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError, AttributeError):
        previous = None

    started = time.monotonic()
    interrupted = False
    try:
        if args.dry_run:
            OUT.line("Проверка без записи (--dry-run): файлы только читаются,")
            OUT.line("на диск не создаётся и не изменяется ни один файл.")
            OUT.line("")
            ok, dpp, bad = run_dry(files, OUT, opts, args.verbose, cancel)
            elapsed = time.monotonic() - started
            OUT.line("")
            OUT.line(OUT.rule * min(OUT.width, 60))
            if cancel.is_set():
                OUT.line("ПРЕРВАНО пользователем (Ctrl+C).")
            OUT.line("Итог проверки: пригодных %d, с рецептом DPP %d, с ошибками %d "
                     "(всего файлов %d)" % (ok, dpp, bad, len(files)))
            OUT.line("Время: %s; записано: 0 Б (режим проверки, на диск ничего не писалось)"
                     % _fmt_secs(elapsed))
            _print_dpp_notice(OUT, dpp)
            if cancel.is_set():
                return EXIT_CANCELLED
            return EXIT_ERRORS if bad else EXIT_OK

        rep = Reporter(OUT, files, opts.suffix, args.verbose)
        try:
            run_convert(files, opts, args.workers, rep, cancel)
        except KeyboardInterrupt:
            interrupted = True
        elapsed = time.monotonic() - started
        OUT.line("")
        _print_summary(OUT, rep, elapsed, cancel.is_set() or interrupted)
        if cancel.is_set() or interrupted:
            return EXIT_CANCELLED
        return EXIT_ERRORS if rep.errors else EXIT_OK
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl+C вне цикла конвертации: без простыни traceback.
        OUT.line("")
        OUT.line("Прервано пользователем.")
        raise SystemExit(EXIT_CANCELLED)
    except BrokenPipeError:  # `| head` и подобное
        try:
            os.close(sys.stdout.fileno())
        except OSError:
            pass
        raise SystemExit(EXIT_ERRORS)
