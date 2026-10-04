"""
MediaGrab - GUI frontend for yt-dlp
Downloads video/audio from supported platforms, keeps a thumbnail gallery
of everything you've pulled down, and runs as a single packaged .exe.

yt-dlp is used as a Python library (pip package, not a separate .exe) and
a static ffmpeg binary is bundled inside assets/ffmpeg/ so the compiled
exe needs nothing next to it at runtime.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import yt_dlp
from PIL import Image

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

def writable_root() -> Path:
    """Folder next to the exe/script where history.json and out/ live."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(*parts) -> Path:
    """Folder holding bundled read-only assets (logo, ffmpeg binaries).

    In a PyInstaller onefile build these are unpacked into a temp dir at
    sys._MEIPASS; in dev mode / onedir builds they sit next to the script.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return base.joinpath(*parts)


ROOT = writable_root()
ASSETS_DIR = resource_path("assets")
FFMPEG_PLATFORM = "windows" if os.name == "nt" else "linux"
FFMPEG_DIR = resource_path("assets", "ffmpeg", FFMPEG_PLATFORM)
FFMPEG_BIN = FFMPEG_DIR / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
DEFAULT_OUTPUT_DIR = ROOT / "out"
HISTORY_FILE = ROOT / "history.json"
LOGO_PNG = ASSETS_DIR / "logo.png"

DEFAULT_OUTPUT_DIR.mkdir(exist_ok=True)

THUMB_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".flac", ".wav"}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

ACCENT = "#00A878"       # jade green
ACCENT_HOVER = "#00885F"
NAVY = "#0F231D"         # near-black jade background
CARD_BG = "#183A30"

# "Segoe UI" is the native Windows UI font - Tk silently falls back to its
# platform default on OSes where it isn't installed, so this is safe
# cross-platform.
FONT_FAMILY = "Segoe UI"
AUDIO_FORMATS = ["mp3", "wav", "opus", "m4a"]
VIDEO_QUALITIES = ["best", "1080p", "720p", "480p"]


# --------------------------------------------------------------------------- #
# Download history persistence
# --------------------------------------------------------------------------- #

class History:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.items = []

    def save(self):
        try:
            self.path.write_text(
                json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def add(self, media_path: str, thumb_path: str | None, kind: str, date: str | None = None):
        entry = {
            "media": media_path,
            "thumb": thumb_path,
            "kind": kind,
            "date": date or datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
        # de-dupe on media path
        self.items = [i for i in self.items if i["media"] != media_path]
        self.items.insert(0, entry)
        self.save()

    def remove(self, media_path: str):
        self.items = [i for i in self.items if i["media"] != media_path]
        self.save()

    def valid_items(self):
        """Drop entries whose file no longer exists on disk."""
        alive = [i for i in self.items if Path(i["media"]).exists()]
        if len(alive) != len(self.items):
            self.items = alive
            self.save()
        return self.items


# --------------------------------------------------------------------------- #
# yt-dlp worker (runs in a background thread, streams progress via a queue)
# --------------------------------------------------------------------------- #

class _CancelledByUser(Exception):
    """Raised from inside a yt-dlp progress hook to abort a running job."""


class DownloadWorker(threading.Thread):
    def __init__(self, url: str, mode: str, quality: str, out_dir: Path, event_q: queue.Queue):
        super().__init__(daemon=True)
        self.url = url
        self.mode = mode            # "video" | "audio"
        # video: "best" | "1080p" | "720p" | "480p"
        # audio: "mp3" | "wav" | "opus" | "m4a"  (output format, not bitrate)
        self.quality = quality
        self.out_dir = out_dir
        self.q = event_q
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _audio_format(self) -> str:
        return self.quality if self.quality in AUDIO_FORMATS else "mp3"

    def _progress_hook(self, d: dict):
        if self._cancelled:
            raise _CancelledByUser("cancelled by user")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else 0.0
            self.q.put(("progress", pct))
            speed = d.get("speed")
            speed_txt = f"{speed / 1_000_000:.2f} MB/s" if speed else "..."
            self.q.put(("log", f"{pct:5.1f}%  {speed_txt}"))
        elif d.get("status") == "finished":
            self.q.put(("log", f"Downloaded, post-processing: {Path(d.get('filename', '')).name}"))

    def _build_opts(self) -> dict:
        out_template = str(self.out_dir / "%(title).150B [%(id)s].%(ext)s")
        opts = {
            "outtmpl": out_template,
            "writethumbnail": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [self._progress_hook],
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
            # YouTube's SABR-only streaming experiment blocks direct format
            # URLs on some clients (HTTP 403). Forcing android+web clients
            # avoids it - see https://github.com/yt-dlp/yt-dlp/issues/12482
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if FFMPEG_BIN.exists():
            opts["ffmpeg_location"] = str(FFMPEG_BIN)

        if self.mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"].insert(0, {
                "key": "FFmpegExtractAudio",
                "preferredcodec": self._audio_format(),
                "preferredquality": "0",  # best available for the chosen format
            })
        else:
            if self.quality == "best":
                opts["format"] = "bestvideo+bestaudio/best"
            else:
                height = self.quality.replace("p", "")
                opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            opts["merge_output_format"] = "mp4"

        return opts

    def run(self):
        if not FFMPEG_BIN.exists():
            self.q.put(("log", "WARNING: bundled ffmpeg not found - merging/audio extraction will fail."))

        opts = self._build_opts()
        self.q.put(("log", f"> yt-dlp ({self.mode}, quality={self.quality}) {self.url}"))

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
        except _CancelledByUser:
            self.q.put(("cancelled", None))
            return
        except yt_dlp.utils.DownloadError as exc:
            if self._cancelled:
                self.q.put(("cancelled", None))
            else:
                self.q.put(("error", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
            self.q.put(("error", f"Unexpected error: {exc}"))
            return

        final_path = Path(ydl.prepare_filename(info))
        if self.mode == "audio":
            final_path = final_path.with_suffix("." + self._audio_format())

        thumb_path = None
        for ext in THUMB_EXTS:
            candidate = final_path.with_suffix(ext)
            if candidate.exists():
                thumb_path = str(candidate)
                break

        self.q.put(("done", {"media": str(final_path), "thumb": thumb_path, "kind": self.mode}))


# --------------------------------------------------------------------------- #
# Gallery row widget - rectangular, file-manager style (icon + name + meta)
# --------------------------------------------------------------------------- #

class GalleryRow(ctk.CTkFrame):
    ICON_SIZE = (42, 42)

    def __init__(self, master, entry: dict, on_open, on_show, on_rename, on_delete):
        super().__init__(master, fg_color=CARD_BG, corner_radius=8, height=58)
        self.entry = entry
        self.on_open = on_open
        self.on_show = on_show
        self.on_rename = on_rename
        self.on_delete = on_delete

        self.grid_columnconfigure(1, weight=1)

        self.thumb_label = ctk.CTkLabel(self, text="", image=self._load_thumb())
        self.thumb_label.grid(row=0, column=0, rowspan=2, padx=(10, 12), pady=8)

        name = Path(entry["media"]).name
        display = name if len(name) <= 58 else name[:55] + "..."
        self.name_label = ctk.CTkLabel(
            self, text=display, font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            anchor="w", justify="left",
        )
        self.name_label.grid(row=0, column=1, sticky="ew", pady=(9, 0))

        kind_tag = "AUDIO" if entry.get("kind") == "audio" else "VIDEO"
        date = entry.get("date", "")
        subtitle = f"{kind_tag}   \u00B7   {date}" if date else kind_tag
        self.subtitle_label = ctk.CTkLabel(
            self, text=subtitle, font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            anchor="w", text_color="gray60",
        )
        self.subtitle_label.grid(row=1, column=1, sticky="ew", pady=(0, 9))

        for widget in (self, self.thumb_label, self.name_label, self.subtitle_label):
            widget.bind("<Double-Button-1>", lambda e: self.on_open(self.entry))
            widget.bind("<Button-3>", self._context_menu)

    def _load_thumb(self):
        thumb = self.entry.get("thumb")
        try:
            if thumb and Path(thumb).exists():
                img = Image.open(thumb)
            else:
                img = Image.open(LOGO_PNG)
            img.thumbnail(self.ICON_SIZE)
            canvas = Image.new("RGBA", self.ICON_SIZE, (0, 0, 0, 0))
            offset = (
                (self.ICON_SIZE[0] - img.width) // 2,
                (self.ICON_SIZE[1] - img.height) // 2,
            )
            canvas.paste(img, offset)
            return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=self.ICON_SIZE)
        except (OSError, ValueError):
            return None

    def _context_menu(self, event):
        import tkinter as tk

        menu = tk.Menu(
            self, tearoff=0, bg=CARD_BG, fg="white", activebackground=ACCENT,
            font=(FONT_FAMILY, 10), bd=0,
        )
        menu.add_command(label="Open", command=lambda: self.on_open(self.entry))
        menu.add_command(label="Show in folder", command=lambda: self.on_show(self.entry))
        menu.add_command(label="Rename", command=lambda: self.on_rename(self.entry))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.on_delete(self.entry))
        menu.tk_popup(event.x_root, event.y_root)


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

class MediaGrabApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("MediaGrab - Video & Audio Downloader")
        self.geometry("1180x680")
        self.minsize(980, 560)

        icon_path = ASSETS_DIR / "logo.ico"
        if os.name == "nt" and icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.history = History(HISTORY_FILE)
        self.event_q: queue.Queue = queue.Queue()
        self.worker: DownloadWorker | None = None
        self.mode_var = ctk.StringVar(value="video")
        self.quality_var = ctk.StringVar(value="best")
        self.out_dir = DEFAULT_OUTPUT_DIR

        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        self._build_main_panel()
        self._build_gallery_panel()
        self._refresh_gallery()

        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- #
    # Left panel: URL input, mode, path, log
    # ---------------------------------------------------------------- #

    def _build_main_panel(self):
        panel = ctk.CTkFrame(self, fg_color="transparent")
        panel.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)
        panel.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(1, weight=1)

        if LOGO_PNG.exists():
            logo_img = Image.open(LOGO_PNG)
            logo_ctk = ctk.CTkImage(light_image=logo_img, dark_image=logo_img, size=(44, 44))
            ctk.CTkLabel(header, text="", image=logo_ctk).grid(row=0, column=0, padx=(0, 10))

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            title_frame, text="MediaGrab",
            font=ctk.CTkFont(family=FONT_FAMILY, size=22, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title_frame, text="by J-org3",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color="gray60",
        ).pack(side="left", padx=(6, 0), pady=(9, 0))

        info_btn = ctk.CTkButton(
            header, text="i", width=28, height=28, corner_radius=14,
            fg_color=NAVY, hover_color=ACCENT, command=self._show_info,
        )
        info_btn.grid(row=0, column=2, sticky="e")

        # URL entry
        ctk.CTkLabel(
            panel, text="Video / audio URL", anchor="w", font=ctk.CTkFont(family=FONT_FAMILY),
        ).grid(row=1, column=0, sticky="ew", pady=(4, 2))
        self.url_entry = ctk.CTkEntry(
            panel, placeholder_text="Paste link here", height=42,
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
        )
        self.url_entry.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        # Mode toggle
        mode_frame = ctk.CTkFrame(panel, fg_color="transparent")
        mode_frame.grid(row=3, column=0, sticky="ew", pady=(0, 12))

        self.video_btn = ctk.CTkButton(
            mode_frame, text="\u25B6  Video", width=140, height=38,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda: self._set_mode("video"),
        )
        self.video_btn.pack(side="left", padx=(0, 8))

        self.audio_btn = ctk.CTkButton(
            mode_frame, text="\U0001F50A  Audio", width=140, height=38,
            fg_color=NAVY, hover_color=ACCENT,
            command=lambda: self._set_mode("audio"),
        )
        self.audio_btn.pack(side="left", padx=(0, 20))

        self.quality_label = ctk.CTkLabel(
            mode_frame, text="Quality", font=ctk.CTkFont(family=FONT_FAMILY),
        )
        self.quality_label.pack(side="left", padx=(0, 6))
        self.quality_menu = ctk.CTkOptionMenu(
            mode_frame, variable=self.quality_var,
            values=VIDEO_QUALITIES,
            fg_color=NAVY, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(family=FONT_FAMILY),
        )
        self.quality_menu.pack(side="left")

        # Save path
        ctk.CTkLabel(
            panel, text="Save path", anchor="w", font=ctk.CTkFont(family=FONT_FAMILY),
        ).grid(row=4, column=0, sticky="ew", pady=(0, 2))
        path_frame = ctk.CTkFrame(panel, fg_color="transparent")
        path_frame.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        path_frame.grid_columnconfigure(0, weight=1)

        self.path_entry = ctk.CTkEntry(path_frame, height=36, font=ctk.CTkFont(family=FONT_FAMILY))
        self.path_entry.insert(0, str(self.out_dir))
        self.path_entry.configure(state="readonly")
        self.path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        ctk.CTkButton(
            path_frame, text="Browse", width=90, height=36,
            fg_color=NAVY, hover_color=ACCENT, command=self._browse_path,
            font=ctk.CTkFont(family=FONT_FAMILY),
        ).grid(row=0, column=1)

        # Download button + progress
        self.download_btn = ctk.CTkButton(
            panel, text="Download", height=46,
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._start_download,
        )
        self.download_btn.grid(row=6, column=0, sticky="ew", pady=(4, 10))

        self.progress = ctk.CTkProgressBar(panel)
        self.progress.set(0)
        self.progress.grid(row=7, column=0, sticky="ew", pady=(0, 4))

        self.status_label = ctk.CTkLabel(
            panel, text="Idle", anchor="w", text_color="gray70",
            font=ctk.CTkFont(family=FONT_FAMILY),
        )
        self.status_label.grid(row=8, column=0, sticky="ew", pady=(0, 10))

        # Log box
        panel.grid_rowconfigure(9, weight=1)
        self.log_box = ctk.CTkTextbox(panel, fg_color=NAVY, font=ctk.CTkFont(size=11, family="Consolas"))
        self.log_box.grid(row=9, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

    def _show_info(self):
        messagebox.showinfo(
            "About MediaGrab",
            "Download videos and audio from supported platforms with quality "
            "selection, straight to a folder you choose. Every completed "
            "download shows up in the gallery on the right - double click to "
            "open it like a normal file.",
        )

    def _set_mode(self, mode: str):
        self.mode_var.set(mode)
        if mode == "video":
            self.video_btn.configure(fg_color=ACCENT)
            self.audio_btn.configure(fg_color=NAVY)
            self.quality_label.configure(text="Quality")
            self.quality_menu.configure(values=VIDEO_QUALITIES)
            self.quality_var.set("best")
        else:
            self.video_btn.configure(fg_color=NAVY)
            self.audio_btn.configure(fg_color=ACCENT)
            self.quality_label.configure(text="Format")
            self.quality_menu.configure(values=AUDIO_FORMATS)
            self.quality_var.set("mp3")

    def _browse_path(self):
        chosen = filedialog.askdirectory(initialdir=str(self.out_dir))
        if chosen:
            self.out_dir = Path(chosen)
            self.path_entry.configure(state="normal")
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, chosen)
            self.path_entry.configure(state="readonly")

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---------------------------------------------------------------- #
    # Download lifecycle
    # ---------------------------------------------------------------- #

    def _start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a link first.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A download is already running.")
            return

        self.progress.set(0)
        self.status_label.configure(text="Starting...")
        self.download_btn.configure(text="Cancel", command=self._cancel_download)

        self.worker = DownloadWorker(
            url=url,
            mode=self.mode_var.get(),
            quality=self.quality_var.get(),
            out_dir=self.out_dir,
            event_q=self.event_q,
        )
        self.worker.start()

    def _cancel_download(self):
        if self.worker:
            self.worker.cancel()
        self.status_label.configure(text="Cancelling...")

    def _reset_download_button(self):
        self.download_btn.configure(text="Download", command=self._start_download)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.event_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self.progress.set(payload / 100)
                    self.status_label.configure(text=f"Downloading... {payload:.1f}%")
                elif kind == "error":
                    self._append_log(f"ERROR: {payload}")
                    self.status_label.configure(text="Failed")
                    self._reset_download_button()
                    messagebox.showerror("Download failed", payload)
                elif kind == "cancelled":
                    self.status_label.configure(text="Cancelled")
                    self._reset_download_button()
                elif kind == "done":
                    self.progress.set(1)
                    self.status_label.configure(text="Done")
                    self._reset_download_button()
                    if payload.get("media"):
                        self.history.add(payload["media"], payload.get("thumb"), payload["kind"])
                        self._refresh_gallery()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ---------------------------------------------------------------- #
    # Gallery
    # ---------------------------------------------------------------- #

    def _build_gallery_panel(self):
        outer = ctk.CTkFrame(self, fg_color=NAVY, corner_radius=12)
        outer.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=16)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        gallery_header = ctk.CTkFrame(outer, fg_color="transparent")
        gallery_header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        gallery_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            gallery_header, text="Gallery",
            font=ctk.CTkFont(family=FONT_FAMILY, size=16, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            gallery_header, text="\u21BB", width=28, height=28, corner_radius=14,
            fg_color=CARD_BG, hover_color=ACCENT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
            command=self._refresh_gallery,
        ).grid(row=0, column=1, sticky="e")

        self.gallery_scroll = ctk.CTkScrollableFrame(
            outer, fg_color="transparent", scrollbar_button_color=ACCENT,
        )
        self.gallery_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))

    def _refresh_gallery(self):
        """Re-scan the save folder against history.json and redraw the
        gallery. Also fixes the display if files were deleted by hand -
        valid_items() drops any entry whose file no longer exists.
        """
        for child in self.gallery_scroll.winfo_children():
            child.destroy()

        items = self.history.valid_items()
        videos = [i for i in items if i.get("kind") != "audio"]
        audios = [i for i in items if i.get("kind") == "audio"]

        self._render_gallery_section("Videos", videos)
        ctk.CTkFrame(self.gallery_scroll, fg_color=CARD_BG, height=1).pack(
            fill="x", padx=4, pady=12
        )
        self._render_gallery_section("Audio", audios)

    def _render_gallery_section(self, title: str, entries: list[dict]):
        ctk.CTkLabel(
            self.gallery_scroll, text=title,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color="gray70", anchor="w",
        ).pack(fill="x", padx=4, pady=(2, 6))

        if not entries:
            ctk.CTkLabel(
                self.gallery_scroll, text="Nothing here yet.",
                font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                text_color="gray50", anchor="w",
            ).pack(fill="x", padx=8, pady=(0, 4))
            return

        for entry in entries:
            row = GalleryRow(
                self.gallery_scroll, entry,
                on_open=self._open_entry,
                on_show=self._show_in_folder,
                on_rename=self._rename_entry,
                on_delete=self._delete_entry,
            )
            row.pack(fill="x", padx=4, pady=3)

    def _open_entry(self, entry: dict):
        path = entry["media"]
        if not Path(path).exists():
            messagebox.showerror("Missing file", "That file no longer exists.")
            self.history.remove(path)
            self._refresh_gallery()
            return
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            webbrowser.open(Path(path).as_uri())

    def _show_in_folder(self, entry: dict):
        path = Path(entry["media"])
        if os.name == "nt":
            subprocess.run(["explorer", "/select,", str(path)])
        else:
            subprocess.run(["xdg-open", str(path.parent)])

    def _rename_entry(self, entry: dict):
        import tkinter.simpledialog as sd

        old_path = Path(entry["media"])
        new_stem = sd.askstring("Rename", "New file name:", initialvalue=old_path.stem)
        if not new_stem:
            return
        new_path = old_path.with_name(new_stem + old_path.suffix)
        try:
            old_path.rename(new_path)
        except OSError as exc:
            messagebox.showerror("Rename failed", str(exc))
            return
        self.history.remove(entry["media"])
        self.history.add(
            str(new_path), entry.get("thumb"), entry.get("kind", "video"), entry.get("date")
        )
        self._refresh_gallery()

    def _delete_entry(self, entry: dict):
        if not messagebox.askyesno("Delete", f"Delete {Path(entry['media']).name}?"):
            return
        for key in ("media", "thumb"):
            p = entry.get(key)
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                except OSError:
                    pass
        self.history.remove(entry["media"])
        self._refresh_gallery()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            self.worker.cancel()
        self.destroy()


if __name__ == "__main__":
    app = MediaGrabApp()
    app.mainloop()
