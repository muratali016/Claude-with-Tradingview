@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  Trading Copilot — Chromium launcher (CDP mode)
REM ============================================================

set CHROME_EXE=C:\Users\xxxx\chrome.exe
set DATA_DIR=%LOCALAPPDATA%\ChromeCDP

echo.
echo Chrome exe  : %CHROME_EXE%
echo Data dir    : %DATA_DIR%
echo Debug port  : 9222
echo.

if not exist "%CHROME_EXE%" (
  echo [!] Chromium not found at the path above. Edit this .bat and fix CHROME_EXE.
  pause
  exit /b 1
)

REM --- Kill any leftover chromium.exe / chrome.exe processes so the new one
REM     can actually grab the user-data-dir lock. Silently ignores "not found".
echo Killing any existing Chrome/Chromium processes...
taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM chromium.exe >nul 2>&1
timeout /t 1 /nobreak >nul

REM --- Launch foreground so any startup error is visible in this window.
echo Launching Chromium...
"%CHROME_EXE%" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%DATA_DIR%" ^
  --no-first-run ^
  --no-default-browser-check ^
  --disable-features=RendererCodeIntegrity ^
  "https://www.tradingview.com/chart/"

echo.
echo Chromium exited. (This is normal once you close the browser window.)
pause