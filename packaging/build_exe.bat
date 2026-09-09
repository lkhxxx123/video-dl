@echo off
REM ============================================================
REM Video downloader - portable exe build script (run on dev box)
REM Prereq: Python 3.10+ with requirements.txt installed
REM Output: dist\<app folder>\  (zip the folder to distribute)
REM NOTE: keep this file ASCII-only (cmd parses it in ANSI codepage)
REM ============================================================
setlocal
cd /d "%~dp0.."

echo [1/4] Installing pyinstaller...
pip show pyinstaller >nul 2>&1 || pip install pyinstaller || goto :err

echo [2/4] Downloading Chromium via playwright...
python -m playwright install chromium || goto :err

echo [3/4] Running PyInstaller...
pyinstaller --noconfirm packaging\downloader.spec || goto :err

REM resolve dist folder (name is non-ASCII, use wildcard)
set "DIST="
for /d %%D in ("dist\*") do set "DIST=%%D"
if "%DIST%"=="" (
    echo ERROR: dist folder not found
    goto :err
)

echo [4/4] Assembling runtime (Chromium + ffmpeg)...
set "PWDIR=%LOCALAPPDATA%\ms-playwright"
if not exist "%PWDIR%" (
    echo ERROR: playwright browsers not found at %PWDIR%
    goto :err
)
REM copy only the browser revisions this playwright version needs
python packaging\collect_runtime.py "%PWDIR%" "%DIST%\runtime\browsers" || goto :err

if exist runtime\tools\ffmpeg.exe (
    xcopy /E /I /Q /Y runtime\tools "%DIST%\runtime\tools\" >nul || goto :err
) else (
    echo.
    echo NOTE: runtime\tools has no ffmpeg.exe / ffprobe.exe
    echo       Copy both exes into "%DIST%\runtime\tools\"
    echo       (download: https://www.gyan.dev/ffmpeg/builds/ essentials)
)

echo.
echo ============================================================
echo Build OK: %DIST%\
echo Distribute: zip the whole folder, user runs the exe inside
echo First run: put key.txt next to the exe (or use AI config in UI)
echo Unsigned exe triggers SmartScreen warning - More info - Run anyway
echo ============================================================
goto :eof

:err
echo.
echo BUILD FAILED (see errors above)
exit /b 1
