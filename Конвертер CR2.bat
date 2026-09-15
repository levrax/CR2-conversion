@echo off
rem ===========================================================================
rem  Konverter CR2 - zapusk graficheskogo okna (cr2_gui.pyw).
rem
rem  Etot fayl namerenno soderzhit tolko ASCII: cmd.exe chitaet .bat v tekushchey
rem  OEM-kodovoy stranitse (866/437/...), poetomu lyuboy russkiy tekst v tele
rem  .bat lomaetsya pri drugoy lokali. Vse soobshcheniya na russkom pechataet
rem  Python, a ne cmd. "chcp 65001" nuzhen dlya putey s kirillitsey.
rem ===========================================================================
setlocal enableextensions

rem  chcp 65001 i PYTHONIOENCODING=utf-8 imeyut smysl TOLKO vmeste: esli chcp
rem  ne srabotal, konsol ostalas v cp866, a Python uzhe pishet utf-8 - i ves
rem  russkiy tekst prevrashchaetsya v krakozyabry. Smotrim kod vozvrata chcp.
chcp 65001 >nul 2>&1
if errorlevel 1 (set "PYTHONIOENCODING=") else (set "PYTHONIOENCODING=utf-8")
title CR2 v JPEG - bez poter

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "GUI=%APPDIR%\cr2_gui.pyw"
set "PYTHONUTF8=1"

cd /d "%APPDIR%" 2>nul
if errorlevel 1 goto :nodir
if not exist "%GUI%" goto :nogui
if not exist "%APPDIR%\cr2_core.py" goto :nogui

rem --- 1) pythonw.exe: okno bez konsoli (osnovnoy variant) -------------------
rem  "start" vozvrashchaet 0, kak tolko protsess SOZDAN, poetomu oshibka
rem  importa (net tcl/tk, skopirovan ne ves komplekt) proshla by kak uspeh:
rem  ni okna, ni soobshcheniya, errorlevel 0. Snachala korotkaya proverka
rem  tem zhe interpretatorom - cmd ee dozhidaetsya i vidit nastoyashchiy kod.
set "PYW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%I"
if defined PYW (
    call :preflight "%PYW%"
    if errorlevel 1 goto :startfail
    start "CR2 v JPEG" "%PYW%" "%GUI%"
    if errorlevel 1 goto :startfail
    goto :done
)

rem --- 2) pyw.exe / py.exe: shtatnyy launcher Python dlya Windows ------------
set "PYWL="
for /f "delims=" %%I in ('where pyw.exe 2^>nul') do if not defined PYWL set "PYWL=%%I"
if defined PYWL (
    call :preflight "%PYWL%" -3
    if errorlevel 1 goto :startfail
    start "CR2 v JPEG" "%PYWL%" -3 "%GUI%"
    if errorlevel 1 goto :startfail
    goto :done
)

rem --- 3) python.exe: konsol ostaetsya vidimoy, oshibki budut na ekrane ------
call :findpython
if defined PY goto :runconsole
goto :nopython

:runconsole
echo pythonw.exe ne nayden - zapusk cherez python.exe.
echo (Okno konsoli ostanetsya otkrytym: v nem budut vidny oshibki.)
echo.
"%PY%" %PYARG% "%GUI%"
if errorlevel 1 goto :runfail
goto :done

rem --- 4) fonovyy zapusk ne udalsya: povtoryaem vidimo, chtoby pokazat prichinu
:startfail
echo Ne udalos zapustit GUI v fonovom rezhime (bez konsoli).
echo Zapuskaem vidimo, chtoby na ekrane byla vidna prichina...
echo.
call :findpython
if not defined PY goto :nopython
"%PY%" %PYARG% "%GUI%"
if errorlevel 1 goto :runfail
goto :done

:findpython
set "PY="
set "PYARG="
for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY for /f "delims=" %%I in ('where py.exe 2^>nul') do if not defined PY (set "PY=%%I" & set "PYARG=-3")
goto :eof

:nodir
echo.
echo OSHIBKA: ne udalos pereyti v papku programmy:
echo   %APPDIR%
echo Skopiruyte papku na lokalnyy disk (ne zapuskayte iz arhiva .zip).
echo.
pause
exit /b 2

:nogui
echo.
echo OSHIBKA: ne nayden cr2_gui.pyw ili cr2_core.py v papke:
echo   %APPDIR%
echo Polozhite "Konverter CR2.bat" ryadom s cr2_gui.pyw, cr2_core.py i cr2_convert.py.
echo.
echo Bez GUI mozhno rabotat iz komandnoy stroki:
echo   python cr2_convert.py PAPKA
echo ili peretashchit papku na "Peretashchi syuda CR2.bat".
echo.
pause
exit /b 2

:nopython
echo.
echo OSHIBKA: Python ne nayden (net ni pythonw.exe, ni python.exe, ni py.exe).
echo Ustanovite Python 3.9 ili novee s https://www.python.org/downloads/
echo Pri ustanovke obyazatelno vklyuchite galochku "Add python.exe to PATH".
echo.
pause
exit /b 9009

:runfail
set "RC=%ERRORLEVEL%"
echo.
echo Programma zavershilas s oshibkoy (kod %RC%).
echo Tekst oshibki - vyshe. Chastye prichiny:
echo   - ne ustanovlen tkinter (pereustanovite Python s komponentom "tcl/tk");
echo   - ne ustanovlen Pillow: python -m pip install Pillow
echo.
pause
exit /b %RC%

:preflight
rem  Proveryaem to, chto padaet na urovne importa (tkinter, cr2_core), I zaodno
rem  to, chto sam cr2_gui.pyw voobshche razbiraetsya. Bez vtoroy proverki lyubaya
rem  SyntaxError vnutri GUI prohodila kak uspeh: "start" vozvrashchaet 0, kak
rem  tolko protsess SOZDAN, - ni okna, ni soobshcheniya, errorlevel 0.
rem  compile() tolko razbiraet tekst i nichego ne pishet na disk.
rem  Put k interpretatoru obyazatelno v kavychkah: "Program Files".
"%~1" %~2 -c "import io,sys,tkinter,cr2_core; compile(io.open(sys.argv[1],encoding='utf-8').read(),sys.argv[1],'exec')" "%GUI%"
exit /b %ERRORLEVEL%

:done
endlocal
exit /b 0
