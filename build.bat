@echo off
REM ==========================================================================
REM  CNCTD.STOCK - Build-Skript
REM  Erzeugt dist\CNCTD.STOCK.exe (portabel) und, falls Inno Setup 6
REM  installiert ist, zusaetzlich installer\Output\CNCTD.STOCK_Setup.exe
REM ==========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Erstelle virtuelle Umgebung ...
    py -3.11 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :error
)
set PY=.venv\Scripts\python.exe

echo [2/5] Installiere Abhaengigkeiten ...
%PY% -m pip install --quiet --upgrade pip
%PY% -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :error

echo [3/5] Erzeuge Icon und UI-Grafiken ...
%PY% tools\make_icon.py || goto :error
%PY% tools\make_ui_assets.py || goto :error

echo [4/5] Baue portable EXE mit PyInstaller ...
%PY% -m PyInstaller --noconfirm --clean --noconsole --onefile ^
    --name "CNCTD.STOCK" ^
    --icon "assets\cnctd_stock.ico" ^
    --version-file "version_info.txt" ^
    --add-data "assets;assets" ^
    main.py
if errorlevel 1 goto :error

echo [5/5] Installer (optional) ...
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC (
    "%ISCC%" "installer\cnctd_stock.iss"
    if errorlevel 1 goto :error
) else (
    echo      Inno Setup 6 nicht gefunden - Installer wird uebersprungen.
    echo      Download: https://jrsoftware.org/isdl.php
)

echo.
echo Fertig!  Portable EXE: dist\CNCTD.STOCK.exe
if defined ISCC echo          Installer:     installer\Output\CNCTD.STOCK_Setup.exe
exit /b 0

:error
echo.
echo *** Build fehlgeschlagen ***
exit /b 1
