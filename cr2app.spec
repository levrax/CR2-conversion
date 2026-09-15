# -*- mode: python ; coding: utf-8 -*-
"""Одна спека на две платформы: Windows (onedir) и macOS (.app).

    pyinstaller --noconfirm --clean cr2app.spec
    python build_local.py            # то же самое плюс отчёт о результате

Windows  ->  dist/CR2 Converter/CR2 Converter.exe  +  dist/CR2 Converter/_internal/
macOS    ->  dist/CR2 Converter/  (сырой onedir)  и  dist/CR2 Converter.app

Помните: когда PyInstaller получает .spec, ВСЕ ключи командной строки, кроме
--noconfirm, --clean, --distpath, --workpath, --upx-dir и --log-level,
игнорируются.  Менять поведение сборки нужно здесь, а не флагами.

--------------------------------------------------------------------------
ПОЧЕМУ ЗДЕСЬ ЕСТЬ КОПИЯ cr2_gui.pyw -> cr2_gui.py
--------------------------------------------------------------------------
Точка входа — app.py, он делает `import cr2_gui`.  Анализатор PyInstaller
ходит по графу импортов средствами importlib, а importlib не считает .pyw
модулем (SOURCE_SUFFIXES = ['.py']).  Значит, оставь мы всё как есть, в сборку
не попал бы ни cr2_gui, ни tkinter, ни cr2_core — приложение собралось бы с
кодом возврата 0 и не запустилось.

Поэтому перед анализом сюда, в рабочую папку сборки, кладётся ТОЧНАЯ копия
cr2_gui.pyw под именем cr2_gui.py, и её каталог добавляется в pathex.  Сам
cr2_gui.pyw не переименовывается: на него ссылаются .bat/.js-запускалки,
тесты и README.  Копия создаётся заново при каждой сборке, так что разойтись
с оригиналом она не может.
"""

import os
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
APP_VERSION = "1.0.0"
BUNDLE_ID = "com.cr2converter.app"

ENTRY = ROOT / "app.py"
GUI_PYW = ROOT / "cr2_gui.pyw"
CORE = ROOT / "cr2_core.py"

for required in (ENTRY, GUI_PYW, CORE):
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
# Значок (необязателен: нет файла - нет значка, сборка не падает)
# --------------------------------------------------------------------------

_ico = ROOT / "assets" / "app.ico"
_icns = ROOT / "assets" / "app.icns"
ICON = None
if IS_WIN and _ico.is_file():
    ICON = str(_ico)
elif IS_MAC and _icns.is_file():
    ICON = str(_icns)

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

# Исходный .pyw кладём рядом как данные: это запасной путь загрузки в app.py
# (load_gui ищет cr2_gui.pyw в _MEIPASS, если `import cr2_gui` не удался).
datas += [(str(GUI_PYW), ".")]

# --------------------------------------------------------------------------
# Скрытые импорты
# --------------------------------------------------------------------------

hiddenimports = [
    "cr2_gui",      # берётся из копии в _gui_alias, см. шапку
    "cr2_core",     # cr2_gui импортирует его после sys.path.insert
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

pyz = PYZ(a.pure)

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
    icon=ICON,
    version=None,                      # это путь к ФАЙЛУ ресурса версии Windows
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# --------------------------------------------------------------------------
# macOS: настоящий .app
# --------------------------------------------------------------------------

if IS_MAC:
    app = BUNDLE(
        coll,                          # именно COLLECT: onefile + .app не бывает
        name="%s.app" % APP_NAME,
        icon=ICON,
        # Без явного идентификатора PyInstaller подставит имя приложения —
        # это не reverse-DNS, и подпись с нотаризацией на нём споткнутся.
        bundle_identifier=BUNDLE_ID,
        # Строка, и только строка: нестроковый CFBundleShortVersionString
        # роняет бандл на старте (PyInstaller #4466).
        version=APP_VERSION,
        info_plist={
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundleDisplayName": APP_NAME,
            # BUNDLE ставит это сам при console=False, но пусть будет явно.
            "NSHighResolutionCapable": True,
            # По документации необходим, чтобы окно рисовалось в retina.
            "NSPrincipalClass": "NSApplication",
            "NSRequiresAquaSystemAppearance": False,   # разрешить тёмную тему
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.photography",
            "NSHumanReadableCopyright": "CR2 Converter",
        },
    )
