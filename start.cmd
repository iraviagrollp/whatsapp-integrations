@echo off
rem Start the WhatsApp inbox service. Leave the window open while it is in use.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\serve.py
pause
