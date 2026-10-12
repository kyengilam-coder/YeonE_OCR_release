@echo off
rem 더블클릭용. .pyw 연결이 안 된 PC 에서는 이 파일을 더블클릭하십시오.
cd /d "%~dp0"
python YeonE_OCR.pyw
if errorlevel 1 pause
