# -*- coding: utf-8 -*-
"""Сборка приложения одной командой на текущей ОС.

    python build_local.py              обычная сборка
    python build_local.py --no-clean   не стирать build/ (быстрее при отладке)
    python build_local.py --check      только проверки, без сборки

Что делает: проверяет окружение, зовёт PyInstaller с cr2app.spec и печатает,
что получилось, где лежит и сколько весит.  Пишет ровно в две папки рядом со
спекой — build/ и dist/ — и никуда больше.

Кросс-сборки не существует: Windows-приложение собирается только на Windows,
.app — только на macOS.  Это ограничение PyInstaller, а не скрипта.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "cr2app.spec"


def _app_name() -> str:
    """Имя приложения читаем ИЗ СПЕКИ, а не дублируем здесь.

    Второй экземпляр строки «CR2 Converter» рано или поздно разойдётся с первым,
    и скрипт начнёт искать в dist папку, которой нет, сообщая о провале удачной
    сборки.  Поэтому единственный источник истины — APP_NAME в cr2app.spec.
    """
    # Якорь и кавычки в шаблоне обязательны: startswith("APP_NAME") ловил бы и
    # строку APP_NAME_CLI, а она в спеке задана через подстановку
    # ("%s CLI" % APP_NAME) — скрипт искал бы в dist папку с «%s CLI» в имени.
    # Сейчас APP_NAME стоит в файле раньше, но порядок строк — не гарантия.
    pattern = re.compile(r'''^APP_NAME\s*=\s*["'](.+?)["']\s*$''')
    try:
        for line in SPEC.read_text(encoding="utf-8").splitlines():
            found = pattern.match(line)
            if found:
                return found.group(1)
    except Exception:
        pass
    return "CR2 Converter"


APP_NAME = _app_name()

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


# --------------------------------------------------------------------------
# Мелочи
# --------------------------------------------------------------------------


# Вывод перенаправляют в файл (build.log), а кодировка файла берётся из локали,
# а не из консоли.  На системе, где локаль не умеет кириллицу, print() свалился
# бы с UnicodeEncodeError и убил сборку на ровном месте — заменяем непечатаемое.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:                                   # pragma: no cover
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def human(n: int) -> str:
    x = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if x < 1024 or unit == "ГБ":
            return "%.1f %s" % (x, unit) if unit != "Б" else "%d Б" % int(x)
        x /= 1024
    return "%.1f ГБ" % x


def tree_size(path: Path) -> tuple[int, int]:
    """(суммарный размер, число файлов). Символические ссылки не разыменовываем."""
    total = 0
    count = 0
    if path.is_file():
        return path.stat().st_size, 1
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            f = Path(dirpath) / name
            try:
                if f.is_symlink():
                    continue
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def biggest(path: Path, limit: int = 8) -> list[tuple[str, int]]:
    """Самые тяжёлые элементы первого уровня — чтобы понимать, из чего вес."""
    items = []
    if not path.is_dir():
        return items
    for child in path.iterdir():
        size, _ = tree_size(child)
        items.append((child.name, size))
    items.sort(key=lambda p: p[1], reverse=True)
    return items[:limit]


# --------------------------------------------------------------------------
# Проверки перед сборкой
# --------------------------------------------------------------------------


def preflight() -> int:
    problems = 0

    say("Python      : %s" % sys.version.split()[0])
    say("Платформа   : %s" % sys.platform)
    say("Папка       : %s" % ROOT)

    for name in ("app.py", "cr2app.spec", "cr2_gui.pyw", "cr2_core.py",
                 "cr2_convert.py"):          # cr2_convert.py — вход для CLI-exe
        if not (ROOT / name).is_file():
            say("НЕТ ФАЙЛА   : %s" % name)
            problems += 1

    try:
        import PyInstaller
        say("PyInstaller : %s" % PyInstaller.__version__)
    except ImportError:
        say("PyInstaller : не установлен  ->  python -m pip install pyinstaller")
        problems += 1

    try:
        import _tkinter
        say("Tcl/Tk      : %s / %s" % (_tkinter.TCL_VERSION, _tkinter.TK_VERSION))
    except ImportError:
        say("Tcl/Tk      : ОТСУТСТВУЕТ — этот Python собран без tkinter, "
            "окно собрать не получится")
        problems += 1

    # Pillow, rawpy и numpy для запуска из исходников необязательны, но в
    # дистрибутиве их доустановить уже нельзя: собираем — значит нужны все.
    # Поэтому это ПРОБЛЕМА, а не заметка: раньше скрипт печатал «в сборке
    # пропадёт режим …», тут же сообщал «Проверка пройдена» и возвращал 0,
    # то есть спокойно выпускал дистрибутив с половиной возможностей.
    for mod, why in (("PIL", "поворот и уменьшение"),
                     ("numpy", "нужен rawpy"),
                     ("rawpy", "проявка RAW без встроенного JPEG")):
        try:
            __import__(mod)
            say("%-12s: есть" % mod)
        except ImportError:
            say("%-12s: НЕТ — в сборке пропадёт режим «%s». "
                "python -m pip install -r requirements.txt" % (mod, why))
            problems += 1

    return problems


def check_clean_env(strict: bool) -> int:
    """Изолировано ли окружение сборки.

    Сборка вне venv утягивает в бандл всё, что лежит в глобальном
    site-packages (pywin32, yaml, pyreadline3 — программа не импортирует ни
    одного из них), и результат перестаёт побайтово соответствовать тому, что
    собирает CI.  Список EXCLUDES в спеке этот мусор не догоняет: он ведётся
    руками и уже отстал.

    Для СЕБЯ собирать так можно: лишние мегабайты ничего не ломают.  Поэтому по
    умолчанию это громкое предупреждение, а ошибкой становится только под
    --strict-env, который стоит включать для сборок «на раздачу».
    """
    dirty = ""
    if sys.prefix == sys.base_prefix:
        dirty = "сборка идёт НЕ в виртуальном окружении"
    else:
        try:
            text = (Path(sys.prefix) / "pyvenv.cfg").read_text(
                encoding="utf-8", errors="replace").lower()
        except OSError:
            text = ""
        if "include-system-site-packages = true" in text:
            dirty = "venv создан с --system-site-packages, изоляции нет"

    say("venv        : %s" % ("НЕТ" if dirty else "да"))
    if not dirty:
        return 0

    say("              %s." % dirty)
    say("              В бандл попадёт лишнее из глобального site-packages, и")
    say("              он перестанет совпадать с тем, что собирает CI.")
    say("              python -m venv .venv")
    say("              %s" % (".venv\\Scripts\\activate" if IS_WIN
                              else "source .venv/bin/activate"))
    say("              python -m pip install -r requirements.txt pyinstaller")
    if strict:
        say("              --strict-env: считаем это ошибкой.")
        return 1
    say("              (не ошибка: для сборки «на раздачу» добавьте --strict-env)")
    return 0


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------


def build(clean: bool, log_level: str) -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--log-level", log_level]
    if clean:
        cmd.append("--clean")
    cmd.append(str(SPEC))

    say()
    say("Команда     : %s" % " ".join(cmd))
    say("-" * 70)
    started = time.time()
    rc = subprocess.call(cmd, cwd=str(ROOT))
    took = time.time() - started
    say("-" * 70)
    say("PyInstaller завершился с кодом %d за %.0f с" % (rc, took))
    return rc


def check_macos_plist(app: Path) -> int:
    """Info.plist собранного .app: 0 — годен, 1 — бандл нельзя раздавать.

    Проверка существует потому, что «сборка прошла успешно» тут ничего не
    доказывает.  LSBackgroundOnly=True делает .app фоновым агентом: ни значка
    в Dock, ни строки меню, окно Tk нельзя вынести на передний план — с точки
    зрения пользователя приложение по двойному щелчку просто ничего не делает,
    и в журнале при этом пусто, потому что процесс стартовал нормально.
    Значение туда попадает само, от console=True у консольного exe в том же
    COLLECT, так что ловить это нужно именно постфактум, в файле.
    """
    import plistlib

    plist = app / "Contents" / "Info.plist"
    try:
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
    except Exception as exc:
        say("Info.plist  : не прочитан (%s)" % exc)
        return 1

    bad = 0
    if data.get("LSBackgroundOnly"):
        say("Info.plist  : LSBackgroundOnly=true — бандл запустится ФОНОВЫМ")
        say("              агентом: ни значка в Dock, ни меню, окно не поднять.")
        bad = 1
    if not data.get("NSHighResolutionCapable"):
        say("Info.plist  : нет NSHighResolutionCapable — окно будет мыльным")
        bad = 1
    # icon=None у BUNDLE не значит «без значка»: osx.py подставляет
    # bootloader/images/icon-windowed.icns, и бандл уходит с чужим логотипом.
    if str(data.get("CFBundleIconFile", "")).startswith("icon-windowed"):
        say("Info.plist  : CFBundleIconFile=%s — это значок PyInstaller,"
            % data.get("CFBundleIconFile"))
        say("              а не приложения. Нужен assets/app.icns.")
        bad = 1
    say("Версия бандла: %s (CFBundleShortVersionString)"
        % data.get("CFBundleShortVersionString"))
    if not bad:
        say("Info.plist  : в порядке")
    return bad


def report() -> int:
    """Что получилось. Возвращает 0, если ожидаемый результат на месте."""
    dist = ROOT / "dist"
    targets: list[tuple[str, Path]] = []

    if IS_WIN:
        folder = dist / APP_NAME
        targets.append(("папка приложения", folder))
        targets.append(("исполняемый файл", folder / ("%s.exe" % APP_NAME)))
    elif IS_MAC:
        targets.append(("бандл", dist / ("%s.app" % APP_NAME)))
        targets.append(("исполняемый файл",
                        dist / ("%s.app" % APP_NAME) / "Contents" / "MacOS" / APP_NAME))
    else:
        targets.append(("папка приложения", dist / APP_NAME))

    say()
    say("=" * 70)
    missing = 0
    for label, path in targets:
        if not path.exists():
            say("%-18s: НЕТ  %s" % (label, path))
            missing += 1
            continue
        size, count = tree_size(path)
        extra = " (%d файлов)" % count if path.is_dir() else ""
        say("%-18s: %s" % (label, path))
        say("%-18s  %s%s" % ("", human(size), extra))

    main_dir = dist / APP_NAME
    internal = main_dir / "_internal"
    if internal.is_dir():
        say()
        say("Из чего вес (_internal):")
        for name, size in biggest(internal):
            say("    %-28s %10s" % (name, human(size)))

    say("=" * 70)
    if missing:
        say("Результат неполный: проверьте %s"
            % (ROOT / "build" / "cr2app" / "warn-cr2app.txt"))
        return 1

    if IS_MAC:
        bad = check_macos_plist(dist / ("%s.app" % APP_NAME))
        if bad:
            return bad

    say()
    if IS_WIN:
        say("Запуск: \"%s\"" % (main_dir / ("%s.exe" % APP_NAME)))
        say("Раздавать пользователю нужно ВСЮ папку \"%s\" целиком," % APP_NAME)
        say("а не один .exe: рядом с ним лежит _internal со всей начинкой.")
        say("При первом запуске SmartScreen скажет «неизвестный издатель» —")
        say("файл не подписан; «Подробнее» -> «Выполнить в любом случае».")
    elif IS_MAC:
        say("Запуск: open \"%s\"" % (dist / ("%s.app" % APP_NAME)))
        say("Бандл подписан только ad-hoc, нотаризации нет: после скачивания")
        say("macOS его заблокирует. Системные настройки -> Конфиденциальность")
        say("и безопасность -> «Всё равно открыть», либо одной командой:")
        say("    xattr -dr com.apple.quarantine \"%s\""
            % (dist / ("%s.app" % APP_NAME)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Сборка конвертера CR2 на текущей ОС.")
    ap.add_argument("--no-clean", action="store_true",
                    help="не удалять кэш build/ перед сборкой")
    ap.add_argument("--check", action="store_true",
                    help="только проверить окружение, не собирать")
    ap.add_argument("--log-level", default="INFO",
                    choices=["TRACE", "DEBUG", "INFO", "WARN", "DEPRECATION", "ERROR"])
    ap.add_argument("--strict-env", action="store_true",
                    help="считать ошибкой сборку вне изолированного venv "
                         "(для сборок, которые пойдут людям)")
    args = ap.parse_args()

    say("=" * 70)
    problems = preflight()
    problems += check_clean_env(args.strict_env)
    say("=" * 70)

    if problems:
        say("Сборка невозможна: неустранённых проблем — %d" % problems)
        return 2
    if args.check:
        say("Проверка пройдена. Для сборки запустите без --check.")
        return 0

    rc = build(clean=not args.no_clean, log_level=args.log_level)
    if rc != 0:
        say("Сборка не удалась. Полный протокол — выше, предупреждения — в")
        say("%s" % (ROOT / "build" / "cr2app" / "warn-cr2app.txt"))
        return rc
    return report()


if __name__ == "__main__":
    sys.exit(main())
