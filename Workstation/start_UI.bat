@echo off
cd /d "%~dp0"
CALL activate smoke
python -m config_UI.main
PAUSE
