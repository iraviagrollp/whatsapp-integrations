@echo off
rem Restart the WhatsApp inbox and the reports service, so changes to either take effect.
rem Both run as administrator (their scheduled tasks), so Windows asks for permission first.

rem Not yet administrator: start this same file again as administrator, and stop here.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart.ps1"
