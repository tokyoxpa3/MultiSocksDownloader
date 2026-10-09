@echo off
setlocal enableextensions
pushd "%~dp0."

REM ===========================================================================
REM  MultiSocksDownloader - Windows standalone build
REM
REM  This file is intentionally PURE ASCII (7-bit), and must stay that way.
REM  cmd.exe parses .bat files with the system ANSI codepage (950 / Big5 on
REM  this machine). If you add Chinese (or any non-ASCII) text and save the
REM  file as UTF-8, cmd.exe mis-reads those bytes as bogus commands and the
REM  build silently stops partway. If you really must add non-ASCII text,
REM  save this file in the ANSI codepage instead of UTF-8.
REM
REM  Build environment: .venv (Python 3.11 + nuitka + PySide6 + libtorrent).
REM  This script calls .venv\Scripts\python.exe explicitly, so it works
REM  whether or not the virtualenv has been activated.
REM ===========================================================================

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [0/6] Checking build interpreter...
"%PY%" -c "import PySide6, nuitka, libtorrent" >nul 2>nul
if errorlevel 1 (
    echo   [ERROR] Interpreter is missing PySide6 / nuitka / libtorrent.
    echo           Interpreter: %PY%
    echo           Fix with:    %PY% -m pip install -r requirements.txt nuitka
    goto :fail
)
"%PY%" -c "import sys; print('  interpreter:', sys.executable)"

echo [1/6] Installing / updating dependencies...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [2/6] Nuitka compile (yt-dlp excluded on purpose, see bundle_ytdlp.py)...
REM  Why yt-dlp is excluded from Nuitka:
REM    * yt-dlp is 1045 modules = 92% of the whole build (971 are extractors).
REM      Compiling them is slow and buys almost nothing.
REM    * extractors change often; keeping yt-dlp external means updating it is
REM      a folder swap, not a full re-package of the exe.
REM  Three flags matter, dropping any one breaks the build:
REM    --nofollow-import-to=yt_dlp          : do not compile yt-dlp.
REM    --no-deployment-flag=excluded-module-usage
REM        Without it Nuitka hard-errors with
REM        "Module 'yt_dlp' was actively excluded from Nuitka compilation".
REM    the --include-* list below
REM        Nuitka no longer analyses yt-dlp's imports, so the stdlib modules
REM        yt-dlp needs at runtime must be listed explicitly, otherwise the
REM        packaged exe dies with ModuleNotFoundError.
"%PY%" -m nuitka --standalone --windows-console-mode=disable --windows-icon-from-ico=app_icon.ico --enable-plugin=pyside6 --include-package=libtorrent --nofollow-import-to=yt_dlp --no-deployment-flag=excluded-module-usage --include-module=getpass --include-module=optparse --include-module=hashlib --include-package=http --include-package=xml --include-module=hmac --include-module=secrets --include-package=ctypes --include-module=uuid MultiSocksDownloader.py
if errorlevel 1 goto :fail

echo [3/6] Bundling external yt-dlp as sourceless .pyc...
"%PY%" bundle_ytdlp.py
if errorlevel 1 goto :fail

echo [4/6] Fetching ffmpeg (LGPL essentials)...
"%PY%" fetch_ffmpeg.py
if errorlevel 1 goto :fail

echo [5/6] Copying ffmpeg into dist...
if not exist "MultiSocksDownloader.dist\ffmpeg" mkdir "MultiSocksDownloader.dist\ffmpeg"
copy /Y "ffmpeg\ffmpeg.exe" "MultiSocksDownloader.dist\ffmpeg\ffmpeg.exe" >nul
if errorlevel 1 goto :fail
if exist "ffmpeg\LICENSE.txt" copy /Y "ffmpeg\LICENSE.txt" "MultiSocksDownloader.dist\ffmpeg\LICENSE.txt" >nul
echo   ffmpeg copied to MultiSocksDownloader.dist\ffmpeg

echo [6/6] Copying runtime data folders and trimming test modules...
REM  yt_dlp_plugins: ships yt-dlp plugins (external extractors / post-processors).
REM  Optional: yt-dlp auto-loads this folder when it is present.
if exist "yt_dlp_plugins" (
    xcopy /E /I /Y "yt_dlp_plugins" "MultiSocksDownloader.dist\yt_dlp_plugins" >nul
    echo   yt_dlp_plugins copied
) else (
    echo   yt_dlp_plugins not found - skipped
)

REM  locale: *.json translation files read by the i18n module.
if exist "locale" (
    xcopy /E /I /Y "locale" "MultiSocksDownloader.dist\locale" >nul
    echo   locale copied
) else (
    echo   locale not found - skipped
)

REM  --include-package=ctypes drags in ctypes' own test suite. Those .pyd files
REM  are never imported at runtime, so drop them to keep the dist tidy.
for %%F in (_ctypes_test.pyd _testbuffer.pyd _testcapi.pyd _testinternalcapi.pyd) do (
    if exist "MultiSocksDownloader.dist\%%F" del /Q "MultiSocksDownloader.dist\%%F"
)
if exist "MultiSocksDownloader.dist\ctypes\test" rd /S /Q "MultiSocksDownloader.dist\ctypes\test"

echo.
echo Build OK. Output folder: MultiSocksDownloader.dist
popd
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED (errorlevel %errorlevel%).
popd
endlocal
exit /b 1
