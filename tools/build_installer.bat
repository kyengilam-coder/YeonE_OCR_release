@echo off
rem Build the Windows installer (prepares work\windows-release\venv if needed). See tools\build_installer.ps1
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1" %*
echo.
pause
