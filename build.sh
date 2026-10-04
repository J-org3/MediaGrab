#!/usr/bin/env bash
# Local dev build for Linux. Same steps CI runs on tag push, but on your
# machine: fetches ffmpeg, installs deps, packages the MediaGrab binary.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

echo "Installing dependencies..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "Updating yt-dlp to the latest release (YouTube breaks old ones often)..."
pip install --upgrade yt-dlp

if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "tkinter is missing. On Debian/Ubuntu: sudo apt-get install python3-tk"
    exit 1
fi

if [ ! -f assets/ffmpeg/linux/ffmpeg ]; then
    echo "Fetching ffmpeg for Linux from BtbN/FFmpeg-Builds..."
    mkdir -p assets/ffmpeg/linux
    curl -L -o ffmpeg_dl.tar.xz \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-lgpl.tar.xz"
    mkdir -p ffmpeg_extract
    tar -xf ffmpeg_dl.tar.xz -C ffmpeg_extract
    BIN_DIR=$(find ffmpeg_extract -type d -name bin | head -n1)
    cp "$BIN_DIR/ffmpeg" assets/ffmpeg/linux/
    cp "$BIN_DIR/ffprobe" assets/ffmpeg/linux/
    chmod +x assets/ffmpeg/linux/ffmpeg assets/ffmpeg/linux/ffprobe
    rm -rf ffmpeg_extract ffmpeg_dl.tar.xz
fi

echo "Building MediaGrab binary..."
pyinstaller \
  --name MediaGrab \
  --onefile \
  --add-data "assets:assets" \
  --collect-all yt_dlp \
  --noconfirm \
  main.py

cp dist/MediaGrab ./MediaGrab
chmod +x MediaGrab
echo
echo "Done: ./MediaGrab is ready to run."
