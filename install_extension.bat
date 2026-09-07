@echo off
setlocal enabledelayedexpansion

set "SRC=%~dp0chrome_extension"

echo ================================================
echo  MultiSocksDownloader - Chrome Extension Install
echo ================================================
echo.

if not exist "%SRC%\manifest.json" (
    echo ERROR: Extension folder not found:
    echo        %SRC%
    echo        Make sure this script is in the same folder as
    echo        MultiSocksDownloader.exe and chrome_extension\.
    pause
    exit /b 1
)

echo Opening Chrome extensions page...
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
echo       %SRC%
echo ================================================
echo.
pause
