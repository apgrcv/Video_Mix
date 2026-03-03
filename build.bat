@echo off
title VideoMixer - Build Tool

echo ========================================
echo   VideoMixer - Build Tool
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)

echo [1/3] Installing PyInstaller...
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    pip install pyinstaller
)

echo [2/3] Building...
echo.
pyinstaller --onefile --windowed --add-binary "bin\ffmpeg;." --add-binary "bin\ffprobe;." --name "VideoMixer" main.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Build Complete!
echo ========================================
echo.
echo Output: dist\VideoMixer.exe
echo.
echo IMPORTANT: Send bin\ folder together with VideoMixer.exe
echo.
pause
