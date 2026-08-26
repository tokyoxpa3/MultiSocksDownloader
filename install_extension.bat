@echo off
setlocal enabledelayedexpansion

set "SRC=%~dp0chrome_extension"
set "DST=%LOCALAPPDATA%\MultiSocksDownloader\chrome_extension"

echo ================================================
echo  MultiSocksDownloader - Chrome Extension Install
echo ================================================
echo.

echo [1/2] Copying extension to a stable location...
echo        %DST%
if exist "%DST%" rmdir /s /q "%DST%"
xcopy "%SRC%" "%DST%" /e /i /h /y >nul
if errorlevel 1 (
    echo ERROR: Failed to copy extension files.
    pause
    exit /b 1
)

echo [2/2] Opening Chrome extensions page...
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if defined CHROME (
    rem Chrome blocks chrome:// URLs passed on the command line, so open a
    rem plain window and type the address into the omnibox instead.
    start "" "%CHROME%" --new-window
    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0open_extensions.ps1"
) else (
    echo WARNING: Chrome was not found. Open chrome://extensions/ manually.
)

echo ================================================
echo  Next steps (in the Chrome extensions page):
echo    1. Turn ON "Developer mode"  (top-right)
echo    2. Click "Load unpacked"
echo    3. Select this folder:
echo       %DST%
echo ================================================
echo.
pause
