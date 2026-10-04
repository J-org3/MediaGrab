@echo off
REM Local dev build for Windows. Same steps CI runs on tag push, but on
REM your machine: fetches ffmpeg, installs deps, packages MediaGrab.exe.

cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
)

echo Updating yt-dlp to the latest release (YouTube breaks old ones often)...
pip install --upgrade yt-dlp

if not exist assets\ffmpeg\windows\ffmpeg.exe (
    echo Fetching ffmpeg for Windows from BtbN/FFmpeg-Builds...
    mkdir assets\ffmpeg\windows 2>nul
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip' -OutFile ffmpeg_dl.zip"
    powershell -Command "Expand-Archive -Force ffmpeg_dl.zip ffmpeg_extract"
    for /f "delims=" %%D in ('powershell -Command "(Get-ChildItem -Recurse -Filter bin -Path ffmpeg_extract | Select-Object -First 1).FullName"') do set BIN=%%D
    copy /Y "%BIN%\ffmpeg.exe" assets\ffmpeg\windows\ >nul
    copy /Y "%BIN%\ffprobe.exe" assets\ffmpeg\windows\ >nul
    rmdir /s /q ffmpeg_extract
    del ffmpeg_dl.zip
)

echo Building MediaGrab.exe...
pyinstaller ^
  --name MediaGrab ^
  --onefile ^
  --windowed ^
  --icon assets\logo.ico ^
  --add-data "assets;assets" ^
  --collect-all yt_dlp ^
  --noconfirm ^
  main.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

copy /Y dist\MediaGrab.exe MediaGrab.exe >nul
echo.
echo Done: MediaGrab.exe is in this folder.
pause
