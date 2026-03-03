@echo off
title VideoMixer

echo ========================================
echo   VideoMixer - Video Merge Tool
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found!
    echo.
    echo Please install Python from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: During installation, check "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [OK] Python found

where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "bin\ffmpeg.exe" (
        echo [ERROR] FFmpeg not found!
        echo.
        echo Please download FFmpeg:
        echo   1. Go to https://www.gyan.dev/ffmpeg/builds/
        echo   2. Download ffmpeg-release-essentials.zip
        echo   3. Extract and copy ffmpeg.exe and ffprobe.exe to bin\ folder
        echo.
        pause
        exit /b 1
    )
)

echo [OK] FFmpeg found
echo.
echo Starting VideoMixer...
echo.

python main.py

echo.
echo ========================================
echo   Program exited
echo ========================================
pause
