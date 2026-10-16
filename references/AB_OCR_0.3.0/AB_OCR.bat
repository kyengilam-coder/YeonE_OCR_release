@echo off
cd /d "%~dp0"
python -X utf8 -m ab_ocr gui
if errorlevel 1 pause
