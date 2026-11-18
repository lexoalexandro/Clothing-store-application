@echo off
chcp 65001 >nul
echo === 1/3 Installing required packages ===
python -m pip install flask pywebview pyinstaller
if errorlevel 1 goto fail

echo === 2/3 Building the app (takes 1-3 minutes) ===
python -m PyInstaller --noconfirm --onefile --windowed --name "Magazia_v1.8" --add-data "static;static" app.py
if errorlevel 1 goto fail

echo === 3/3 Done! ===
echo Your app is here: dist\Magazia_v1.8.exe
pause
exit /b 0

:fail
echo.
echo Something went wrong. Copy the red error text above and send it to Claude.
pause
exit /b 1
