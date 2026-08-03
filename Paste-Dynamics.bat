@echo off
cd /d "%~dp0"
python run_gui.py
if errorlevel 1 pause
