@echo off
rem ===========================================================================
rem  Peretashchi syuda CR2 - konvertatsiya peretashchennyh faylov i papok.
rem
rem  Peretashchite myshkoy fayly .CR2 ili papku na etot .bat.
rem
rem  VNIMANIE: Provodnik beret put v kavychki TOLKO esli v nem est probel
rem  (PathQuoteSpaces). Poetomu imya papki s simvolom ^& ili ^^ razrushaetsya
rem  vneshnim cmd.exe RANSHE, chem vypolnitsya pervaya stroka etogo .bat:
rem  %1, %~1 i %* uzhe soderzhat obrezannyy put. Vnutri .bat eto ne
rem  ispravimo - ispolzuyte "Peretashchi syuda CR2.js" (on poluchaet
rem  argumenty ot wscript.exe, bez cmd) libo komandnuyu stroku s kavychkami.
rem
rem  Fayl namerenno soderzhit tolko ASCII (sm. kommentariy v "Konverter CR2.bat"):
rem  ves russkiy tekst pechataet Python, a "chcp 65001" delaet bezopasnymi puti
rem  s kirillitsey.
rem ===========================================================================
setlocal enableextensions

rem  chcp 65001 i PYTHONIOENCODING=utf-8 imeyut smysl TOLKO vmeste: esli chcp
rem  ne srabotal, konsol ostalas v cp866, a Python uzhe pishet utf-8 - i ves
rem  russkiy tekst prevrashchaetsya v krakozyabry. Poetomu smotrim kod vozvrata
rem  chcp, i pri neudache ostavlyaem Pythonu ego sobstvennuyu kodirovku:
rem  cr2_convert sam podstavlyaet ASCII tam, gde simvola net v kodovoy stranitse.
chcp 65001 >nul 2>&1
if errorlevel 1 (set "PYTHONIOENCODING=") else (set "PYTHONIOENCODING=utf-8")
title CR2 -^> JPEG

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "CLI=%APPDIR%\cr2_convert.py"
set "PYTHONUTF8=1"

cd /d "%APPDIR%" 2>nul

if not exist "%CLI%" goto :nocli
if "%~1"=="" goto :nodrop

set "PY="
set "PYARG="
for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY for /f "delims=" %%I in ('where py.exe 2^>nul') do if not defined PY (set "PY=%%I" & set "PYARG=-3")
if not defined PY goto :nopython

"%PY%" %PYARG% "%CLI%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0"   echo [OK] Gotovo.
if "%RC%"=="1"   echo [!] Est oshibki - smotrite spisok vyshe.
if "%RC%"=="2"   echo [!] Nevernye argumenty ili net faylov .CR2.
if "%RC%"=="2"   echo     Esli v imeni papki est ^& ili ^^, peretaskivanie na .bat
if "%RC%"=="2"   echo     teryaet chast puti. Peretashchite tu zhe papku na
if "%RC%"=="2"   echo     "Peretashchi syuda CR2.js" - on peredaet put celikom,
if "%RC%"=="2"   echo     libo zapustite iz komandnoy stroki:
if "%RC%"=="2"   echo       python cr2_convert.py "PAPKA"
if "%RC%"=="130" echo [!] Prervano polzovatelem.
echo.
pause
exit /b %RC%

:nodrop
echo.
echo Peretashchite na etot fayl papku ili fayly .CR2 (drag and drop).
echo.
echo Chto poluchitsya: ryadom s kazhdym .CR2 lyazhet .jpg - vstroennoe prevyu
echo polnogo razmera, skopirovannoe bez pereszhatiya. Kartinka takaya, kakoy ee
echo otrisovala kamera: stil izobrazheniya, balans belogo, kontrast - kak snyato.
echo Pravki Canon DPP eta programma primenit NE mozhet - ih umeet otrisovat
echo tolko sama Canon DPP: "Konvertirovat i sohranit" ili "Paketnaya obrabotka".
echo.
echo Esli v imeni papki est ^& ili ^^, Provodnik teryaet chast puti pri
echo peretaskivanii na .bat - peretaskivayte na "Peretashchi syuda CR2.js".
echo.
echo Mozhno i iz komandnoy stroki:
echo   python cr2_convert.py PAPKA [optsii]
echo   python cr2_convert.py --help
echo.
pause
exit /b 2

:nocli
echo.
echo OSHIBKA: ne nayden fayl cr2_convert.py v papke:
echo   %APPDIR%
echo Polozhite "Peretashchi syuda CR2.bat" ryadom s cr2_convert.py i cr2_core.py.
echo.
pause
exit /b 2

:nopython
echo.
echo OSHIBKA: Python ne nayden (net ni python.exe, ni py.exe v PATH).
echo Ustanovite Python 3.9 ili novee s https://www.python.org/downloads/
echo Pri ustanovke obyazatelno vklyuchite galochku "Add python.exe to PATH".
echo.
pause
exit /b 9009
