@echo off
rem Build the PP-OCRv6 bundle on Windows. See tools\build_ppocrv6_bundle.ps1
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_ppocrv6_bundle.ps1" %*
echo.
pause
