# -*- mode: python ; coding: utf-8 -*-
"""Одна спека на две платформы: Windows (onedir) и macOS (.app).

    pyinstaller --noconfirm --clean cr2app.spec
    python build_local.py            # то же самое плюс отчёт о результате

Windows  ->  dist/CR2 Converter/CR2 Converter.exe  +  dist/CR2 Converter/_internal/
macOS    ->  dist/CR2 Converter/  (сырой onedir)  и  dist/CR2 Converter.app

Оконный exe - это всё приложение «Медиа-инструменты ЮИ РУДН»: конвертация и
вкладки tab_enhance / tab_cull / tab_poster с движками enhance, cull, poster,
brand.  Консольный - только конвертер (cr2_convert.py).  Проверить готовую
сборку без окна:  "CR2 Converter.exe" --self-test отчёт.txt  (см. app.py).

Помните: когда PyInstaller получает .spec, ВСЕ ключи командной строки, кроме
--noconfirm, --clean, --distpath, --workpath, --upx-dir и --log-level,
игнорируются.  Менять поведение сборки нужно здесь, а не флагами.

--------------------------------------------------------------------------
ПОЧЕМУ ЗДЕСЬ ЕСТЬ КОПИЯ cr2_gui.pyw -> cr2_gui.py
--------------------------------------------------------------------------
Точка входа — app.py, он делает `import cr2_gui`.  Анализатор PyInstaller ходит
по графу импортов средствами importlib, а importlib признаёт .pyw исходником
ТОЛЬКО на Windows: SOURCE_SUFFIXES == ['.py', '.pyw'] при os.name == 'nt' и
['.py'] везде ещё.  То есть на macOS в сборку не попали бы ни cr2_gui, ни
tkinter, ни cr2_core — и PyInstaller сообщил бы об этом одной строкой ERROR в
warn-*.txt, завершившись с кодом 0.  Приложение собралось бы и не запустилось.

Поэтому перед анализом сюда, в рабочую папку сборки, кладётся ТОЧНАЯ копия
cr2_gui.pyw под именем cr2_gui.py, и её каталог добавляется в pathex.  Сам
cr2_gui.pyw не переименовывается: на него ссылаются .bat/.js-запускалки,
тесты и README.  Копия создаётся заново при каждой сборке, так что разойтись
с оригиналом она не может.
"""

import os
import re
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# --------------------------------------------------------------------------
# Кто мы и где мы
# --------------------------------------------------------------------------

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

ROOT = Path(SPECPATH).resolve()          # SPECPATH подставляет PyInstaller
BUILD_DIR = Path(workpath).resolve()     # workpath == build/<имя спеки>

APP_NAME = "CR2 Converter"
BUNDLE_ID = "com.cr2converter.app"


def _resolve_version():
    """Версия сборки: из переменной окружения APP_VERSION, иначе 1.0.0.

    CI вычисляет её из тега («Resolve version» в build.yml) и передаёт сюда
    через env.  Раньше здесь стояла зашитая строка, и КАЖДЫЙ релиз с любого
    тега выпускал бандл с версией 1.0.0 — отличить v1.0.0 от v1.1.0 после
    распаковки было нечем.
    """
    raw = (os.environ.get("APP_VERSION") or "1.0.0").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    return raw or "1.0.0"


def _plist_version(raw):
    """CFBundleShortVersionString/CFBundleVersion: 1-3 числа через точку.

    Apple других форм не принимает, а запасное значение CI для сборки без тега
    выглядит как «0.0.0+abc1234» — плюс там недопустим и валит проверку при
    нотаризации.  '0.0.0+abc1234' -> '0.0.0', '1.1.0-rc.2' -> '1.1.0'.
    """
    head = re.split(r"[+-]", raw, maxsplit=1)[0]
    parts = [p for p in head.split(".") if p.isdigit()][:3]
    return ".".join(parts) if parts else "1.0.0"


APP_VERSION = _resolve_version()          # полная строка: показывать людям
PLIST_VERSION = _plist_version(APP_VERSION)   # очищенная: только для Info.plist

ENTRY = ROOT / "app.py"
GUI_PYW = ROOT / "cr2_gui.pyw"
CORE = ROOT / "cr2_core.py"
CLI_ENTRY = ROOT / "cr2_convert.py"

APP_NAME_CLI = "%s CLI" % APP_NAME

GUI_COMMON = ROOT / "gui_common.py"
CULL = ROOT / "cull.py"
FONTS_DIR = ROOT / "fonts"

for required in (ENTRY, GUI_PYW, CORE, CLI_ENTRY, GUI_COMMON):
    if not required.is_file():
        raise SystemExit("cr2app.spec: не найден обязательный файл %s" % required)

# --------------------------------------------------------------------------
# Копия cr2_gui.pyw под импортируемым именем (см. шапку файла)
# --------------------------------------------------------------------------

ALIAS_DIR = BUILD_DIR / "_gui_alias"
ALIAS_DIR.mkdir(parents=True, exist_ok=True)
ALIAS_PY = ALIAS_DIR / "cr2_gui.py"
shutil.copyfile(GUI_PYW, ALIAS_PY)

# --------------------------------------------------------------------------
# Значок.
#
# ВНИМАНИЕ: icon=None НЕ означает «без значка».  PyInstaller подставляет
# СВОЙ bootloader/images/icon-windowed.ico (или icon-console.ico консольному
# exe) — building/api.py:598-604 — и собранные файлы уходят пользователю с
# логотипом PyInstaller.  Подавляет значок только строка "NONE"
# (api.py:786), поэтому ниже у обоих EXE стоит `icon=ICON or "NONE"`.
#
# BUNDLE такой возможности не даёт вовсе: osx.py зовёт normalize_icon_type(),
# а та падает с FileNotFoundError на пути "NONE".  Значит на macOS выбор
# только один: либо положить assets/app.icns, либо .app и его плитка в Dock
# будут носить значок PyInstaller.
# --------------------------------------------------------------------------

_ico = ROOT / "assets" / "app.ico"
_icns = ROOT / "assets" / "app.icns"
ICON = None
if IS_WIN and _ico.is_file():
    ICON = str(_ico)
elif IS_MAC and _icns.is_file():
    ICON = str(_icns)

# Значок пропал -> выпуск останавливаем.  Без этой проверки сборка молча
# уходит зелёной, а пользователь получает .app с логотипом PyInstaller и два
# exe вовсе без значка - и узнаёт об этом только из Finder.
# Переменную читаем НАПРЯМУЮ: REQUIRE_FULL определяется много ниже (см. блок
# про rawpy/numpy/PIL), а этот блок должен отработать до сборки графа.
if ICON is None and os.environ.get("CR2_REQUIRE_FULL_BUILD"):
    raise SystemExit(
        "cr2app.spec: нет файла значка (%s).\n"
        "Без него BUNDLE подставит bootloader/images/icon-windowed.icns "
        "(osx.py), и .app уйдёт с логотипом PyInstaller, а оба exe останутся "
        "вовсе без значка.  Выпуск остановлен."
        % (_icns if IS_MAC else _ico)
    )

# --------------------------------------------------------------------------
# Двоичные файлы и данные зависимостей
# --------------------------------------------------------------------------


def _safe(fn, package):
    """collect_* для пакета, которого может не быть в окружении сборки.

    Pillow и rawpy нужны ТОЛЬКО собранному приложению (см. requirements.txt);
    из исходников программа работает и без них, в режиме извлечения.  Спека
    не должна падать, если сборку запускают в урезанном окружении — пусть
    получится приложение без соответствующего режима.
    """
    try:
        return list(fn(package))
    except Exception as exc:
        print("cr2app.spec: %s('%s') пропущен: %s" % (fn.__name__, package, exc))
        return []


binaries = []
datas = []

# rawpy -> LibRaw.  На Windows это rawpy/raw_r.dll + vcomp140.dll, на macOS
# rawpy/libraw_r*.dylib и rawpy/.dylibs/*.dylib (lcms2, jasper, jpeg).
# Анализ зависимостей PyInstaller 6 обычно подбирает их и сам; строка ниже —
# страховка на случай, если очередной релиз rawpy сменит раскладку колеса.
binaries += _safe(collect_dynamic_libs, "rawpy")

# numpy: свой хук PyInstaller уже делает collect_dynamic_libs + delvewheel,
# но повтор безвреден и защищает от сборки с отключёнными хуками.
binaries += _safe(collect_dynamic_libs, "numpy")

# Pillow: нативные библиотеки лежат внутри PIL/ (и в PIL.libs на Windows).
binaries += _safe(collect_dynamic_libs, "PIL")

# Данные Pillow: .binmode/ключи форматов и служебные файлы пакета.  Кодеки
# изображений — это .pyd/_imaging*, они уже в binaries; сюда попадает мелочь
# вроде PIL/*.json.  Tcl/Tk НЕ трогаем: hook-_tkinter.py собирает _tcl_data и
# _tk_data сам и обрывает СБОРКУ, если не смог.
datas += _safe(collect_data_files, "PIL")

# --- Шрифты афиш: fonts/ -> <сборка>/fonts (poster.bundled_fonts_dir) -------
#
# Репозиторий публичный, а сборка - это распространение шрифта.  Поэтому в
# сборку идёт не «всё из fonts/», а только шрифт, рядом с которым лежит его
# лицензия <Семейство>-OFL.txt (SIL OFL разрешает распространение).  Шрифт без
# лицензии останавливает сборку: так в релиз не уедет, например, Morfin Sans,
# который кто-то положил в fonts/ для себя - его лицензия («Completely free
# font») права распространять явно не даёт, и программа берёт его только из
# папки шрифтов пользователя.
_FONT_EXTS = (".ttf", ".otf")


def _collect_fonts():
    if not FONTS_DIR.is_dir():
        if os.environ.get("CR2_REQUIRE_FULL_BUILD"):
            raise SystemExit("cr2app.spec: нет папки %s - афиши в сборке "
                             "остались бы без шрифтов." % FONTS_DIR)
        print("cr2app.spec: папки fonts/ нет, шрифты не собраны")
        return []
    out = []
    for font in sorted(FONTS_DIR.iterdir()):
        if font.suffix.lower() not in _FONT_EXTS:
            continue
        if "morfin" in font.name.lower():
            raise SystemExit(
                "cr2app.spec: %s в fonts/.  Лицензия Morfin Sans не разрешает "
                "распространение явно - такой шрифт в сборку не кладётся.  "
                "Уберите файл из fonts/ (пользователь указывает его во "
                "вкладке «Афиши»)." % font.name)
        licence = FONTS_DIR / ("%s-OFL.txt" % font.stem.split("-")[0])
        if not licence.is_file():
            raise SystemExit(
                "cr2app.spec: у шрифта %s нет файла лицензии %s.  Без лицензии, "
                "разрешающей распространение, шрифт в сборку не кладётся."
                % (font.name, licence.name))
        out += [(str(font), "fonts"), (str(licence), "fonts")]
    return sorted(set(out))


datas += _collect_fonts()

# --- OpenCV: каскад Хаара для поиска лиц во вкладке «Отбор» ------------------
#
# Хук cv2 из pyinstaller-hooks-contrib собирает модуль и его библиотеки, но
# НЕ папку cv2/data с каскадами: cull тогда молча работал бы «без лиц».  Кладём
# ровно тот файл, который читает cull.HAAR_FILE (все каскады вместе - почти 10 МБ).
# Имя берём из cull.py текстом: импортировать cull здесь - значит тянуть numpy
# в процесс спеки.
_haar = re.search(r'^HAAR_FILE\s*=\s*"([^"]+)"', CULL.read_text(encoding="utf-8"),
                  re.M) if CULL.is_file() else None
HAAR_FILE = _haar.group(1) if _haar else None
_cascade = (_safe(lambda pkg: collect_data_files(pkg, includes=["data/" + HAAR_FILE]),
                  "cv2") if HAAR_FILE else [])
datas += _cascade

# Исходный .pyw кладём рядом как данные: это запасной путь загрузки в app.py
# (load_gui ищет cr2_gui.pyw в _MEIPASS, если `import cr2_gui` не удался).
datas += [(str(GUI_PYW), ".")]

# --------------------------------------------------------------------------
# Скрытые импорты
# --------------------------------------------------------------------------

hiddenimports = [
    "cr2_gui",      # берётся из копии в _gui_alias, см. шапку
    "cr2_core",     # cr2_gui импортирует его после sys.path.insert
    # Вкладки и их движки сюда НЕ вписаны: cr2_gui импортирует tab_* явными
    # операторами import (см. _import_tab_module), анализатор их видит, а
    # проверка ниже падает, если это когда-нибудь сломается.
]

# --------------------------------------------------------------------------
# Что выбрасываем
# --------------------------------------------------------------------------

EXCLUDES = [
    # тяжёлое и заведомо ненужное
    "matplotlib", "scipy", "pandas", "sympy",
    "IPython", "jupyter", "notebook",
    # инструменты разработки
    "pytest", "nose", "setuptools", "pip", "wheel",
    "tkinter.test", "tkinter.tix", "lib2to3", "pydoc_data",
    # другие тулкиты (PIL может тянуть их по графу)
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    # хвосты numpy
    "numpy.f2py", "numpy.distutils",
    # то, что просачивается из «грязного» site-packages этой машины
    "win32com", "win32evtlog", "win32evtlogutil",
    # ВНИМАНИЕ: unittest, numpy.testing и doctest НЕ исключены намеренно.
    # Некоторые библиотеки импортируют их на этапе загрузки, и поломка
    # вылезет только в запущенном окне, где трассировке некуда деться.
]

# --------------------------------------------------------------------------
# Анализ
# --------------------------------------------------------------------------

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT), str(ALIAS_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    # 0, а не 2: -OO вырезает assert и строки документации во ВСЕХ вложенных
    # пакетах, включая numpy и Pillow. Экономия — единицы мегабайт, риск —
    # отказ стороннего кода, который на assert рассчитывает.
    optimize=0,
)

# Пропущенный скрытый импорт PyInstaller пишет в warn-*.txt и продолжает
# сборку с кодом 0 — то есть молча выпускает неработающее приложение.
# Проверяем сами и падаем громко.
_collected = {name for name, _path, _typ in a.pure}
for _must in ("cr2_gui", "cr2_core"):
    if _must not in _collected:
        raise SystemExit(
            "cr2app.spec: модуль %r не попал в сборку.\n"
            "Смотрите %s\n"
            "Обычная причина: рядом со спекой нет cr2_gui.pyw / cr2_core.py."
            % (_must, Path(workpath) / ("warn-%s.txt" % specnm))
        )


# Pillow/rawpy/numpy нужны СОБРАННОМУ приложению: доустановить их внутрь
# бандла пользователь не может.  Из исходников программа работает и без них,
# поэтому локальная сборка «на посмотреть» не обязана падать — а вот выпуск
# обязан.  CR2_REQUIRE_FULL_BUILD=1 выставляет CI.
REQUIRE_FULL = bool(os.environ.get("CR2_REQUIRE_FULL_BUILD"))


def _require_runtime_deps(collected, which):
    if not REQUIRE_FULL:
        return
    for _must in ("rawpy", "numpy", "PIL"):
        if not any(n == _must or n.startswith(_must + ".") for n in collected):
            raise SystemExit(
                "cr2app.spec: зависимость %r отсутствует в окружении сборки "
                "(%s).\nВ готовом дистрибутиве её доустановить нельзя — "
                "сборка остановлена.\n"
                "python -m pip install -r requirements.txt\n"
                "Подробности: %s"
                % (_must, which,
                   Path(workpath) / ("warn-%s.txt" % specnm))
            )


_require_runtime_deps(_collected, "оконная часть")

# Вкладки: из исходников любая может отсутствовать (оболочка покажет панель с
# причиной), а выпуск без них - это другой продукт под тем же именем.
TAB_GRAPH = ("gui_common", "tab_enhance", "tab_cull", "tab_poster",
             "enhance", "cull", "poster", "brand", "cv2")
# cv2 хук собирает в режиме module_collection_mode='py': его модули лежат в
# сборке файлами .py (a.datas), а не в архиве PYZ (a.pure).
_collected_as_files = {dest.replace("\\", "/").split("/")[0]
                       for dest, _src, _typ in a.datas
                       if dest.replace("\\", "/").endswith("/__init__.py")}
if REQUIRE_FULL:
    _lost = [m for m in TAB_GRAPH
             if m not in _collected and m not in _collected_as_files]
    if _lost:
        raise SystemExit(
            "cr2app.spec: в сборку не попали модули вкладок: %s.\n"
            "python -m pip install -r requirements.txt\nПодробности: %s"
            % (", ".join(_lost), Path(workpath) / ("warn-%s.txt" % specnm)))
    # opencv-python / opencv-contrib-python пишут в ту же папку cv2/, что и
    # -headless: в сборку уедет файл того, кто ставился последним, и проверка
    # каскада ниже этого не заметит (каскады есть у всех трёх).
    from importlib import metadata as _metadata
    _cv2_dists = sorted(set(_metadata.packages_distributions().get("cv2", ())))
    _foreign_cv2 = [d for d in _cv2_dists
                    if d.lower().replace("_", "-") != "opencv-python-headless"]
    if _foreign_cv2:
        raise SystemExit(
            "cr2app.spec: кроме opencv-python-headless в окружении стоит %s - "
            "какой из них cv2 в сборке, решил порядок установки.  Соберите в "
            "чистом venv:  python -m pip install -r requirements.txt"
            % ", ".join(_foreign_cv2))
    if not _cascade:
        raise SystemExit(
            "cr2app.spec: в пакете cv2 нет каскада %s - поиск лиц в сборке не "
            "работал бы.  Нужен opencv-python-headless<5 (см. requirements.txt)."
            % HAAR_FILE)

# OpenCV-плагин видео на FFmpeg (opencv_videoio_ffmpeg*.dll, 30 МБ).  Хук cv2
# собирает его как библиотеку, но это подгружаемый по требованию модуль
# cv2.VideoCapture: программа видео не открывает, а cv2.pyd от него не зависит.
a.binaries = [entry for entry in a.binaries
              if not os.path.basename(entry[0]).lower().startswith("opencv_videoio_ffmpeg")]

pyz = PYZ(a.pure)

# --------------------------------------------------------------------------
# Второй вход: консольный cr2_convert.py
# --------------------------------------------------------------------------
# Окно (console=False) не имеет ни stdout, ни кода возврата, который увидит
# скрипт, поэтому собранное приложение нечем проверить автоматически и нечем
# встроить в чужой пакетный файл.  Рядом с окном кладётся консольный exe с той
# же начинкой: он же служит дымовым тестом сборки (см. BUILD.md), он же даёт
# пользователю пакетный режим без установки Python.
#
# Это ОТДЕЛЬНЫЙ Analysis, а не второй скрипт в первом: список скриптов внутри
# одного Analysis PyInstaller склеивает и выполняет подряд в одном процессе.
b = Analysis(
    [str(CLI_ENTRY)],
    pathex=[str(ROOT), str(ALIAS_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["cr2_core"],     # cr2_gui консольной версии не нужен
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

_collected_cli = {name for name, _path, _typ in b.pure}
if "cr2_core" not in _collected_cli:
    raise SystemExit(
        "cr2app.spec: cr2_core не попал в консольную сборку. Смотрите %s"
        % (Path(workpath) / ("warn-%s.txt" % specnm))
    )
_require_runtime_deps(_collected_cli, "консольная часть")

pyz_cli = PYZ(b.pure)

# --------------------------------------------------------------------------
# Ресурс версии Windows
# --------------------------------------------------------------------------
# Без него вкладка «Свойства -> Подробно» у обоих exe ПУСТАЯ: ни версии, ни
# названия продукта.  После распаковки архива отличить одну сборку от другой
# нечем, а неподписанный файл без единого поля версии вдобавок хуже выглядит
# для SmartScreen и эвристик антивирусов.
#
# filevers/prodvers — только числа (4 штуки), поэтому туда идёт очищенная
# PLIST_VERSION; строковые поля показывают APP_VERSION целиком, вместе с
# «+abc1234» у сборок без тега.

VERSION_GUI = None
VERSION_CLI = None

if IS_WIN:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    def _version_numbers():
        parts = [int(p) for p in PLIST_VERSION.split(".")]
        while len(parts) < 4:
            parts.append(0)
        return tuple(parts[:4])

    def _win_version_resource(exe_name, description):
        nums = _version_numbers()
        return VSVersionInfo(
            ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F,
                              flags=0x0, OS=0x40004, fileType=0x1,
                              subtype=0x0, date=(0, 0)),
            kids=[
                # 0409 = en-US, 04B0 = 1200 = Unicode; кириллица в значениях
                # хранится в UTF-16 и от кодовой страницы не зависит.
                StringFileInfo([StringTable("040904B0", [
                    StringStruct("CompanyName", APP_NAME),
                    StringStruct("FileDescription", description),
                    StringStruct("FileVersion", APP_VERSION),
                    StringStruct("InternalName", exe_name),
                    StringStruct("LegalCopyright", APP_NAME),
                    StringStruct("OriginalFilename", "%s.exe" % exe_name),
                    StringStruct("ProductName", APP_NAME),
                    StringStruct("ProductVersion", APP_VERSION),
                ])]),
                VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
            ],
        )

    VERSION_GUI = _win_version_resource(
        APP_NAME, "Медиа-инструменты ЮИ РУДН")
    VERSION_CLI = _win_version_resource(
        APP_NAME_CLI, "Конвертер CR2 в JPEG без потерь (консоль)")

# --------------------------------------------------------------------------
# Исполняемый файл (onedir на обеих платформах)
# --------------------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,             # onedir: содержимое уходит в COLLECT
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX выключен осознанно: на macOS PyInstaller его и так не применяет
    # (ломает .dylib и подпись), на Windows пропускает всё с Control Flow
    # Guard (то есть python312.dll и большинство .pyd), а цена — рост числа
    # ложных срабатываний антивирусов на и без того неподписанном файле.
    upx=False,
    console=False,                     # оконное приложение, консоли нет
    # Консоли нет, значит необработанному исключению негде напечататься.
    # False => PyInstaller покажет трассировку в окне сообщения.
    disable_windowed_traceback=False,
    # main() в cr2_gui не читает sys.argv, перетаскивание файлов на значок
    # приложению не нужно — эмуляцию argv не включаем.
    argv_emulation=False,
    # None = текущая архитектура. universal2 здесь недостижим: rawpy 0.27.1
    # публикует для macOS только колёса macosx_11_0_arm64, x86_64 нет вовсе,
    # и сборка упала бы на IncompatibleBinaryArchError.
    target_arch=None,
    codesign_identity=os.environ.get("CODESIGN_IDENTITY") or None,
    entitlements_file=None,
    # "NONE" (строка!), а не None: None заставляет PyInstaller подставить
    # СВОЙ значок, см. комментарий в разделе «Значок» выше.
    icon=ICON or "NONE",
    version=VERSION_GUI,               # None везде, кроме Windows
    contents_directory="_internal",
)

exe_cli = EXE(
    pyz_cli,
    b.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME_CLI,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                      # смысл этого файла - вывод в консоль
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("CODESIGN_IDENTITY") or None,
    entitlements_file=None,
    icon=ICON or "NONE",
    version=VERSION_CLI,
    contents_directory="_internal",
)

# Оба exe в одной папке: библиотеки, Tcl/Tk и питон у них общие, вторая копия
# 70 МБ никому не нужна.  Совпадающие записи COLLECT отбрасывает сам.
#
# ПОРЯДОК EXE ЗДЕСЬ ЗНАЧИМ.  COLLECT.__init__ перебирает аргументы и делает
# `self.console = arg.console` БЕЗ break (PyInstaller building/api.py:1118-
# 1127), то есть наследует настройки ПОСЛЕДНЕГО EXE — заодно target_arch,
# codesign_identity и entitlements_file.  Когда последним шёл консольный
# exe_cli, BUNDLE получал console=True и писал в Info.plist
# LSBackgroundOnly=True: .app стартовал фоновым агентом без значка в Dock и
# без возможности вынести окно на передний план.  Оконный exe идёт последним.
#
# На CFBundleExecutable порядок аргументов не влияет, но влияет ИМЯ.  Цепочка
# такая: COLLECT сортирует свой TOC по имени назначения (api.py:1147-1148), а
# BUNDLE выбирает exename первым EXECUTABLE из полученного TOC (osx.py:124-129)
# — собственная сортировка BUNDLE идёт строкой НИЖЕ этого выбора, то есть
# спасает именно сортировка в COLLECT.  Сейчас "CR2 Converter" — префикс
# "CR2 Converter CLI" и потому идёт первым; проверка стоит перед COLLECT.
# contents_directory COLLECT берёт у ПЕРВОГО EXE (отдельный цикл с break,
# api.py:1111-1116), у обоих он "_internal", так что перестановка безопасна.
# Имя оконного exe ОБЯЗАНО сортироваться раньше консольного (см. ниже про
# CFBundleExecutable).  Переименуйте консольную часть во что-нибудь вроде
# "CR2 Console" — и .app молча начнёт запускать консольную программу вместо
# окна: двойной щелчок «ничего не делает», а в журнале при этом пусто, потому
# что процесс стартовал штатно.  Ломается только macOS, но падаем везде:
# поймать это на Windows дешевле, чем в релизной сборке.
if sorted([APP_NAME, APP_NAME_CLI])[0] != APP_NAME:
    raise SystemExit(
        "cr2app.spec: имя консольного exe %r сортируется раньше оконного "
        "%r. BUNDLE возьмёт его в CFBundleExecutable, и .app будет "
        "запускать консоль вместо окна. Переименуйте так, чтобы оконное "
        "имя шло первым по алфавиту."
        % (APP_NAME_CLI, APP_NAME)
    )

coll = COLLECT(
    exe_cli,
    exe,
    a.binaries,
    a.datas,
    b.binaries,
    b.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# --------------------------------------------------------------------------
# macOS: настоящий .app
# --------------------------------------------------------------------------

# LSMinimumSystemVersion: не выдуманная цифра, а самая новая macOS, под которую
# собраны вложенные нативные библиотеки.  Колёса OpenCV 4.13+ есть только как
# macosx_13_0_arm64 и macosx_14_0_x86_64, numpy 2.5 на macOS 14+ ставится из
# macosx_14_0_*.  Зашитое «11.0» пускало бы .app на систему, где cv2.so или
# numpy не загрузятся: окно открылось бы, а вкладки показали бы панель ошибки.
# Берём из тегов колёс, которые реально стоят в окружении сборки.
MACOS_FLOOR = (11, 0)      # ниже не поддерживает сам Python 3.12 с python.org


def _macos_minimum():
    import platform
    from importlib import metadata
    arch = platform.machine()
    tag_re = re.compile(r"macosx_(\d+)_(\d+)_(\w+)")
    by_module = metadata.packages_distributions()
    need = MACOS_FLOOR
    for module in ("PIL", "numpy", "rawpy", "cv2"):
        for dist_name in by_module.get(module, ()):
            try:
                wheel = metadata.distribution(dist_name).read_text("WHEEL") or ""
            except Exception:           # noqa: BLE001 - нет метаданных: не знаем
                continue
            # Колесо с несколькими тегами годится для самого старого из них.
            found = [(int(m.group(1)), int(m.group(2)))
                     for m in tag_re.finditer(wheel)
                     if m.group(3) in (arch, "universal2")]
            if found and min(found) > need:
                need = min(found)
                print("cr2app.spec: %s (%s) требует macOS %d.%d"
                      % (dist_name, module, need[0], need[1]))
    return "%d.%d" % need


if IS_MAC:
    MACOS_MINIMUM = _macos_minimum()
    app = BUNDLE(
        coll,                          # именно COLLECT: onefile + .app не бывает
        name="%s.app" % APP_NAME,
        icon=ICON,
        # Без явного идентификатора PyInstaller подставит имя приложения —
        # это не reverse-DNS, и подпись с нотаризацией на нём споткнутся.
        bundle_identifier=BUNDLE_ID,
        # Строка, и только строка: нестроковый CFBundleShortVersionString
        # роняет бандл на старте (PyInstaller #4466).
        version=PLIST_VERSION,
        info_plist={
            "CFBundleShortVersionString": PLIST_VERSION,
            "CFBundleVersion": PLIST_VERSION,
            "CFBundleDisplayName": APP_NAME,
            # Явно и безусловно, а НЕ «BUNDLE поставит сам при console=False».
            # BUNDLE берёт console у COLLECT, а тот наследует его у последнего
            # переданного EXE (см. комментарий у COLLECT выше).  При
            # console=True osx.py:638 пишет LSBackgroundOnly=True, и .app
            # запускается фоновым агентом — без значка в Dock, без строки меню,
            # окно нельзя активировать.  Спековый info_plist накладывается
            # ПОСЛЕ умолчаний (osx.py:644 info_plist_dict.update), поэтому
            # написанное здесь выигрывает при любом порядке аргументов.
            "LSBackgroundOnly": False,
            # Тоже обязательно явно: ветку с NSHighResolutionCapable osx.py
            # выполняет только при console=False, на неё полагаться нельзя.
            "NSHighResolutionCapable": True,
            # По документации необходим, чтобы окно рисовалось в retina.
            "NSPrincipalClass": "NSApplication",
            "NSRequiresAquaSystemAppearance": False,   # разрешить тёмную тему
            "LSMinimumSystemVersion": MACOS_MINIMUM,
            "LSApplicationCategoryType": "public.app-category.photography",
            "NSHumanReadableCopyright": "CR2 Converter",
        },
    )
