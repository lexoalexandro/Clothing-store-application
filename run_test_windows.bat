@echo off
chcp 65001 >nul
python -m pip install flask pywebview
python app.py
pause
