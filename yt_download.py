#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YT DOWNLOAD — Unduh video tunggal / playlist YouTube (MP4 atau MP3).

Fitur:
  - Menu utama interaktif: 1 Install prasyarat · 2 Mulai unduh · 3 Update yt-dlp
    · 4 Bersihkan riwayat gagal · 5 Uninstall · 0 Keluar
  - Auto-install prasyarat yang belum ada (yt-dlp via pip, ffmpeg portable)
  - Uninstall prasyarat bersih (script, downloads/, log/ tetap ada)
  - Terima URL video tunggal (watch / youtu.be / shorts / live) MAUPUN URL playlist
  - URL watch yang menyertakan list= ditanya: video ini saja atau seluruh playlist
  - Pilih FORMAT dulu (Video / Audio MP3 192 kbps), lalu resolusi bila video
  - Playlist diunduh SERIAL satu per satu; video tunggal tanpa prefix nomor urut
  - Unduh ulang video gagal (maks MAX_RETRIES percobaan per video)
  - Update yt-dlp satu klik (YouTube sering berubah; yt-dlp lama rawan HTTP 403)
  - Bersihkan riwayat video gagal (failed-videos.json) tanpa menyentuh hasil unduhan
  - Navigasi kembali: ketik 'k' / '0' di prompt mana pun

Pintasan CLI (opsional):
  python yt_download.py --setup       # = menu 1
  python yt_download.py --uninstall   # = menu 3
  python yt_download.py <URL>         # langsung unduh video/playlist, lewati menu

Launcher Windows: klik ganda YT-DOWNLOAD.bat (CLI) atau YT-DOWNLOAD-GUI.bat (GUI).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_FILE = BASE_DIR / "prereq-state.json"
TOOLS_DIR = BASE_DIR / "tools"
FFMPEG_DIR = TOOLS_DIR / "ffmpeg"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOG_DIR = BASE_DIR / "log"
LOG_FILE = LOG_DIR / "download.log"
FAILED_FILE = BASE_DIR / "failed-videos.json"

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Format audio-only -> postprocessor FFmpegExtractAudio menghasilkan MP3 192 kbps
AUDIO_FMT = "bestaudio/best"

# Kode menu resolusi VIDEO -> (label, format yt-dlp).
# Selector DASH-first: format progresif (gabungan) YouTube sering dibatasi/403,
# jadi gabungan video+audio terpisah (di-merge jadi mp4 oleh ffmpeg) diutamakan;
# format progresif & 'best' hanya fallback.
VIDEO_RESOLUTIONS = {
    "1": ("360p", "bestvideo[height<=360]+bestaudio/best[height<=360]/best"),
    "2": ("480p", "bestvideo[height<=480]+bestaudio/best[height<=480]/best"),
    "3": ("720p", "bestvideo[height<=720]+bestaudio/best[height<=720]/best"),
    "4": ("1080p", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
    "5": ("Best (tertinggi tersedia)", "bestvideo+bestaudio/best"),
}
DEFAULT_RESOLUTION = "3"  # 720p
BACK_INPUTS = {"k", "0"}  # tombol kembali di semua prompt
MAX_RETRIES = 3  # maksimal percobaan unduh ulang per video gagal


# ---------------------------------------------------------------------------
# Helper umum
# ---------------------------------------------------------------------------

def is_back(answer: str) -> bool:
    """True jika input user berarti 'kembali'."""
    return answer.strip().lower() in BACK_INPUTS


def format_size(num: float | None) -> str:
    """Format byte ke tampilan ramah (KiB/MiB/GiB)."""
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def format_speed(speed: float | None) -> str:
    return f"{format_size(speed)}/s" if speed else ""


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def safe_name(name: str) -> str:
    """Bersihkan nama untuk dipakai sebagai nama folder/file."""
    cleaned = "".join(c for c in name if c not in '<>:"/\\|?*').strip()
    return (cleaned[:120] or "playlist").strip()


def log_line(message: str) -> None:
    """Tulis satu baris log ke log/download.log (tidak gagal bila folder terkunci)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Manifest prasyarat (prereq-state.json)
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    """Baca manifest prasyarat yang diinstall oleh script."""
    if MANIFEST_FILE.exists():
        try:
            return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"installed": {}}


def save_manifest(manifest: dict) -> None:
    MANIFEST_FILE.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Penyimpanan video gagal (failed-videos.json) untuk fitur unduh ulang
# ---------------------------------------------------------------------------

def load_failed() -> list[dict]:
    """Baca daftar video yang gagal (untuk fitur unduh ulang)."""
    if FAILED_FILE.exists():
        try:
            data = json.loads(FAILED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_failed(failed_list: list[dict]) -> None:
    FAILED_FILE.write_text(
        json.dumps(failed_list, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def add_failed_entry(
    failed_list: list[dict],
    playlist: str,
    index: int,
    title: str,
    url: str,
    fmt: str,
    kind: str = "playlist",
) -> None:
    """Tambahkan video gagal ke daftar (hindari duplikat berdasarkan URL).

    kind: 'playlist' (hasil unduhan serial, ada prefix NN) atau
          'single'   (unduhan video tunggal, tanpa prefix, di root downloads/).
    """
    if any(e.get("url") == url for e in failed_list):
        return
    failed_list.append(
        {
            "playlist": playlist,
            "index": index,
            "title": title,
            "url": url,
            "fmt": fmt,
            "kind": kind,
        }
    )


def remove_failed_entry(failed_list: list[dict], url: str) -> None:
    """Hapus video dari daftar gagal (setelah berhasil diunduh ulang)."""
    failed_list[:] = [e for e in failed_list if e.get("url") != url]


# ---------------------------------------------------------------------------
# Deteksi prasyarat
# ---------------------------------------------------------------------------

def ytdlp_installed() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def ytdlp_version() -> str | None:
    """Versi yt-dlp terpasang, atau None bila belum terinstall."""
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except ImportError:
        return None


def ffmpeg_path() -> str | None:
    """Path ffmpeg yang bisa dipakai: dari PATH sistem, atau portable di tools/ffmpeg/."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidate = FFMPEG_DIR / exe
    return str(candidate) if candidate.exists() else None


def portable_ffmpeg_exists() -> bool:
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    return (FFMPEG_DIR / exe).exists()


def ffprobe_path() -> str | None:
    """Path ffprobe yang bisa dipakai: PATH sistem atau portable di tools/ffmpeg/."""
    system = shutil.which("ffprobe")
    if system:
        return system
    exe = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
    candidate = FFMPEG_DIR / exe
    return str(candidate) if candidate.exists() else None


def portable_ffprobe_exists() -> bool:
    exe = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
    return (FFMPEG_DIR / exe).exists()


def yt_dlp_ffmpeg_location() -> str | None:
    """Direktori ffmpeg/ffprobe portable untuk opsi ffmpeg_location yt-dlp.
    Kembalikan None bila keduanya di PATH sistem (yt-dlp mencarinya sendiri)."""
    if portable_ffmpeg_exists() and portable_ffprobe_exists():
        return str(FFMPEG_DIR)
    return None


# ---------------------------------------------------------------------------
# Install prasyarat (menu 1 / --setup)
# ---------------------------------------------------------------------------

def run_pip(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args])


def install_ytdlp() -> bool:
    if ytdlp_installed():
        print("  ✔ yt-dlp sudah terinstall.")
        return True
    print("  ⏳ Menginstall yt-dlp via pip...")
    try:
        result = run_pip(["install", "-U", "yt-dlp"])
    except OSError as exc:
        print(f"  ✖ Tidak bisa menjalankan pip: {exc}")
        return False
    if result.returncode == 0 and ytdlp_installed():
        print("  ✔ yt-dlp berhasil diinstall.")
        return True
    print("  ✖ Gagal menginstall yt-dlp. Coba manual: pip install -U yt-dlp")
    return False


def download_file_with_progress(url: str, dest: Path) -> bool:
    """Unduh file dengan indikator persentase (untuk ffmpeg portable)."""
    print(f"  ⏳ Mengunduh {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "yt-download/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(
                        f"\r  {pct:3d}% ({downloaded // 1024 // 1024} MB)",
                        end="",
                        flush=True,
                    )
        print()
        tmp.rename(dest)
        return True
    except Exception as exc:  # noqa: BLE001 - tangani semua error jaringan
        print(f"\n  ✖ Gagal mengunduh ffmpeg: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def extract_from_zip(zip_path: Path, wanted_suffixes: tuple[str, ...]) -> bool:
    """Ekstrak file yang berakhiran wanted_suffixes dari zip ke tools/ffmpeg/."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if member.endswith(wanted_suffixes):
                    target = FFMPEG_DIR / Path(member).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✖ Gagal mengekstrak ffmpeg: {exc}")
        return False


def install_ffmpeg() -> bool:
    """Pastikan ffmpeg DAN ffprobe tersedia (yt-dlp butuh keduanya untuk postprocessing)."""
    if ffmpeg_path() and ffprobe_path():
        print("  ✔ ffmpeg & ffprobe sudah tersedia.")
        return True

    if not sys.platform.startswith("win"):
        print("  ℹ️ ffmpeg/ffprobe belum lengkap. Instal manual lalu jalankan ulang:")
        print("       • macOS:  brew install ffmpeg")
        print("       • Linux:  sudo apt install ffmpeg   (atau paket sejenis)")
        return False

    print("  ⏳ ffmpeg/ffprobe belum lengkap — mengunduh versi portable (Windows)...")
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TOOLS_DIR / "ffmpeg-release-essentials.zip"
    if not download_file_with_progress(FFMPEG_ZIP_URL, zip_path):
        return False

    print("  ⏳ Mengekstrak ffmpeg & ffprobe...")
    ok = extract_from_zip(zip_path, ("bin/ffmpeg.exe", "bin/ffprobe.exe"))
    zip_path.unlink(missing_ok=True)
    if not ok:
        return False

    if portable_ffmpeg_exists() and portable_ffprobe_exists():
        print("  ✔ ffmpeg & ffprobe portable siap di tools/ffmpeg/.")
        return True
    print("  ✖ ffmpeg/ffprobe belum lengkap setelah instalasi.")
    return False


def setup_prereqs() -> None:
    """Menu 1 — Install prasyarat yang belum ada."""
    print("\n" + "=" * 44)
    print("  INSTALL PRASYARAT")
    print("=" * 44)
    manifest = load_manifest()
    installed = manifest.setdefault("installed", {})

    # yt-dlp
    yt_before = ytdlp_installed()
    yt_ok = install_ytdlp()
    if yt_ok and not yt_before:
        installed["yt-dlp"] = "pip"      # benar-benar diinstall oleh script
    else:
        installed.pop("yt-dlp", None)    # sudah ada di sistem -> bukan tanggung jawab script

    # ffmpeg + ffprobe (yt-dlp butuh keduanya untuk postprocessing / MP3)
    ff_ok = install_ffmpeg()
    if ff_ok and portable_ffmpeg_exists() and portable_ffprobe_exists():
        installed["ffmpeg"] = "portable"  # diunduh oleh script -> bisa dihapus
    else:
        installed.pop("ffmpeg", None)

    save_manifest(manifest)

    print("\n  Ringkasan:")
    print(f"    • yt-dlp : {'✔ terinstall' if ytdlp_installed() else '✖ belum'}")
    print(f"    • ffmpeg : {'✔ tersedia' if ffmpeg_path() else '✖ belum'}")
    print(f"    • ffprobe: {'✔ tersedia' if ffprobe_path() else '✖ belum (wajib utk merge/MP3)'}")
    print("  Kembali ke menu utama...")


# ---------------------------------------------------------------------------
# Uninstall prasyarat (menu 3 / --uninstall)
# ---------------------------------------------------------------------------

def uninstall_prereqs() -> None:
    """Menu 3 — Hapus bersih prasyarat yang DIINSTALL OLEH SCRIPT. Script tetap ada."""
    print("\n" + "=" * 44)
    print("  UNINSTALL PRASYARAT")
    print("=" * 44)
    manifest = load_manifest()
    installed = manifest.get("installed", {})

    if not installed:
        print("  ℹ️ Tidak ada fitur atau dependensi yang terinstall.")
        print("     Tidak ada yang perlu di-uninstall.")
        input("  Tekan Enter untuk kembali ke menu utama...")
        return

    print("  Daftar yang akan dihapus:")
    for name, kind in installed.items():
        print(f"    • {name} ({kind})")

    answer = input("  Lanjut uninstall? (Y/n) — 'k'/'0' = batal: ").strip().lower()
    if is_back(answer) or answer not in ("", "y", "yes"):
        print("  Dibatalkan. Kembali ke menu utama...")
        return

    if "yt-dlp" in installed:
        print("  ⏳ Menghapus yt-dlp (pip uninstall)...")
        try:
            run_pip(["uninstall", "-y", "yt-dlp"])
        except OSError as exc:
            print(f"  ✖ Gagal uninstall yt-dlp: {exc}")

    if "ffmpeg" in installed:
        print("  ⏳ Menghapus ffmpeg portable (tools/ffmpeg/)...")
        shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
        if TOOLS_DIR.exists() and not any(TOOLS_DIR.iterdir()):
            TOOLS_DIR.rmdir()

    MANIFEST_FILE.unlink(missing_ok=True)
    print("  ✅ Prasyarat yang diinstall script sudah dihapus.")
    print("  ✅ Script, folder downloads/ & log/ TETAP ADA.")
    input("  Tekan Enter untuk kembali ke menu utama...")


# ---------------------------------------------------------------------------
# Update yt-dlp (menu 3)
# ---------------------------------------------------------------------------

def update_ytdlp() -> None:
    """Menu 3 — Perbarui yt-dlp ke versi terbaru.

    YouTube sering mengubah cara penyajian videonya; yt-dlp yang kedaluwarsa
    bisa mulai gagal dengan HTTP 403. Menu ini memanggil `pip install -U`.
    """
    print("\n" + "=" * 44)
    print("  UPDATE YT-DLP")
    print("=" * 44)

    current = ytdlp_version()
    if current is None:
        print("  ⚠️ yt-dlp belum terinstall. Silakan pilih menu 1 (Install) dulu.")
        input("  Tekan Enter untuk kembali ke menu utama...")
        return
    print(f"  Versi terpasang : {current}")

    print("  ⏳ Memeriksa & memperbarui via pip (jaringan)...")
    try:
        result = run_pip(["install", "-U", "yt-dlp"])
    except OSError as exc:
        print(f"  ✖ Tidak bisa menjalankan pip: {exc}")
        print("     Coba manual: pip install -U yt-dlp")
        input("  Tekan Enter untuk kembali ke menu utama...")
        return

    latest = ytdlp_version()
    if result.returncode == 0 and latest:
        if latest != current:
            print(f"  ✔ yt-dlp diperbarui: {current} → {latest}")
        else:
            print(f"  ✔ yt-dlp sudah versi terbaru ({latest}).")
        log_line(f"UPDATE-YTDLP | {current} -> {latest}")
    else:
        print("  ✖ Gagal memperbarui yt-dlp. Coba manual: pip install -U yt-dlp")
        if latest and latest != current:
            print(f"     (Versi terpasang masih {latest})")
    print("  Kembali ke menu utama...")


# ---------------------------------------------------------------------------
# Prompt interaktif & deteksi URL
# ---------------------------------------------------------------------------

def ask_continue(prompt: str) -> bool | None:
    """Prompt Y/n. Kembalikan None jika user memilih kembali ('k'/'0')."""
    answer = input(f"  {prompt} (Y/n): ").strip().lower()
    if is_back(answer):
        return None
    return answer in ("", "y", "yes")


def choose_resolution() -> str | None:
    """Menu pilih resolusi VIDEO, default 720p. None = kembali ke pemanggil.

    Dipanggil dari choose_format(): None berarti kembali ke pilihan format.
    """
    while True:
        print("\n  Pilih resolusi unduhan:")
        for key, (label, _fmt) in VIDEO_RESOLUTIONS.items():
            mark = "  ← default (Enter)" if key == DEFAULT_RESOLUTION else ""
            print(f"    [{key}] {label}{mark}")
        print("    [0] Kembali ke pilihan format   (atau ketik 'k')")
        choice = input(f"  Pilihan [{DEFAULT_RESOLUTION}]: ").strip()
        if not choice:
            choice = DEFAULT_RESOLUTION
        if is_back(choice):
            return None
        if choice in VIDEO_RESOLUTIONS:
            return VIDEO_RESOLUTIONS[choice][1]
        print("  ✖ Pilihan tidak valid. Coba lagi.")


def choose_format() -> str | None:
    """Pilih FORMAT dulu: Video (lanjut resolusi) atau Audio MP3.

    Return string format yt-dlp; None = user memilih kembali ke menu utama.
    """
    while True:
        print("\n  Pilih format unduhan:")
        print("    [1] Video — pilih resolusi   ← default (Enter)")
        print("    [2] Audio saja (MP3 192 kbps)")
        print("    [0] Kembali ke menu utama   (atau ketik 'k')")
        choice = input("  Pilihan [1]: ").strip().lower()
        if not choice or choice == "1":
            res = choose_resolution()
            if res is None:
                continue  # kembali ke pilihan format, bukan keluar
            return res
        if is_back(choice):
            return None
        if choice == "2":
            return AUDIO_FMT
        print("  ✖ Pilihan tidak valid. Coba lagi.")


def is_youtube_url(url: str) -> bool:
    """Cek kasar: URL YouTube berbentuk video ATAU playlist.

    Ditolak: URL channel/@handle dan halaman lain yang tidak dikenali.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return False
    if "youtu.be" in host:
        return True  # bentuk baku short link: youtu.be/<video-id>
    path = parsed.path or "/"
    query = parse_qs(parsed.query)
    return (
        "v" in query
        or "list" in query
        or path == "/playlist"
        or path.startswith(("/watch", "/shorts/", "/live/"))
    )


def detect_url_kind(url: str) -> str:
    """Klasifikasi URL YouTube: 'playlist' atau 'video'.

    Catatan: URL watch yang membawa list= SEKALIGUS tidak lewat sini —
    ditangani is_watch_with_playlist() dulu di dispatcher.
    """
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except ValueError:
        return "video"
    if "list" in query or "playlist" in parsed.path:
        return "playlist"
    return "video"


def is_watch_with_playlist(url: str) -> bool:
    """True untuk URL watch?v=...&list=... (halaman video dalam sebuah playlist)."""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except ValueError:
        return False
    return "v" in query and "list" in query


def input_youtube_url() -> str | None:
    """Minta URL YouTube (video tunggal atau playlist). None = kembali ke menu utama."""
    while True:
        url = input(
            "\n  Masukkan URL video/playlist YouTube (ketik 'k'/'0' untuk kembali): "
        ).strip()
        if is_back(url):
            return None
        if not url:
            continue
        if is_youtube_url(url):
            return url
        print("  ⚠️ URL tidak terlihat seperti video/playlist YouTube.")
        print("     Contoh video   : https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        print("                      https://youtu.be/dQw4w9WgXcQ")
        print("     Contoh playlist: https://www.youtube.com/playlist?list=PL...")
        again = ask_continue("  Coba masukkan lagi?")
        if again is None or not again:
            return None


# ---------------------------------------------------------------------------
# Unduhan
# ---------------------------------------------------------------------------

def make_progress_hook(cb=None):
    """Buat hook progres yt-dlp.

    cb(d) dipanggil untuk tiap event progres (dict mentah dari yt-dlp) —
    dipakai GUI untuk menampilkan progres tanpa konsol. Bila cb None
    (mode CLI), hook mencetak persentase/kecepatan/ETA ke terminal.
    """
    def hook(d: dict) -> None:
        if cb is not None:
            cb(d)
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0
            eta = d.get("eta")
            eta_str = f" · ETA {eta}s" if eta is not None else ""
            print(
                f"\r    {pct:5.1f}% · {format_size(done)}/{format_size(total)}"
                f" · {format_speed(d.get('speed'))}{eta_str}   ",
                end="",
                flush=True,
            )
        elif d.get("status") == "finished":
            print()
    return hook


def progress_hook(d: dict) -> None:
    """Hook progres CLI (dipertahankan untuk kompatibilitas)."""
    make_progress_hook(None)(d)


def build_outtmpl(index: int | None) -> str:
    """Template nama file yt-dlp. index=None -> tanpa prefix nomor (video tunggal)."""
    if index is None:
        return "%(title)s.%(ext)s"
    return f"{index:02d}_%(title)s.%(ext)s"


def download_video(
    url: str, out_dir: Path, index: int | None, fmt: str, progress_cb=None
) -> bool:
    """Unduh SATU video. index=None untuk video tunggal (tanpa prefix NN).

    progress_cb opsional: fungsi cb(dict-progres yt-dlp) untuk GUI;
    None = progres dicetak ke konsol seperti biasa.
    """
    import yt_dlp

    opts: dict = {
        "format": fmt,
        "outtmpl": str(out_dir / build_outtmpl(index)),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # progres tampil lewat progress_hook saja
        "progress_hooks": [make_progress_hook(progress_cb)],
    }
    loc = yt_dlp_ffmpeg_location()
    if loc:
        # Beri tahu yt-dlp lokasi ffmpeg/ffprobe portable agar tidak perlu PATH
        opts["ffmpeg_location"] = loc
    if fmt == AUDIO_FMT:
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        # Merge video+audio jadi satu file mp4 (DASH/1080p+ butuh merge)
        opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return True
    except yt_dlp.utils.DownloadError as exc:
        # Kegagalan normal unduhan (video privat/terhapus/tidak ada format) -> skip
        print(f"\n  ✖ Gagal mengunduh: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - error tak terduga tetap diteruskan ke loop
        print(f"\n  ✖ Error tak terduga: {exc}")
        traceback.print_exc()
        return False


def fetch_video_info(url: str) -> dict:
    """Ambil metadata SATU video (tanpa menyeret playlist)."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "video",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "",
    }


def fetch_playlist_info(url: str) -> tuple[str, list[dict]]:
    """Ambil judul playlist & daftar video (flat, cepat)."""
    import yt_dlp

    opts = {"extract_flat": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title") or "playlist"
    entries = [e for e in (info.get("entries") or []) if e]
    return title, entries


# ---------------------------------------------------------------------------
# Alur unduh
# ---------------------------------------------------------------------------

def ensure_prereqs() -> bool:
    """Pastikan yt-dlp siap; tawarkan install bila belum. True = boleh lanjut."""
    if ytdlp_installed():
        return True
    print("  ⚠️ Prasyarat belum terinstall. Silakan pilih menu 1 (Install) dulu.")
    answer = ask_continue("  Mau install sekarang?")
    if answer is None or not answer:
        print("  Kembali ke menu utama...")
        return False
    setup_prereqs()
    if not ytdlp_installed():
        print("  ✖ Prasyarat belum lengkap. Kembali ke menu utama...")
        return False
    return True


def warn_ffmpeg_missing(fmt: str) -> None:
    """Peringatan bila ffmpeg/ffprobe tidak lengkap.

    Semua mode kini berpotensi butuh ffmpeg: video = merge stream DASH,
    MP3 = ekstraksi audio — jadi cukup satu bentuk peringatan.
    """
    if ffmpeg_path() and ffprobe_path():
        return
    print("  ⚠️ ffmpeg/ffprobe tidak terdeteksi lengkap — mode ini kemungkinan MEMBUTUHKAN ffmpeg")
    print("     (merge video+audio / ekstrak MP3). Install dulu via menu 1.")


def run_single_download(url: str) -> None:
    """Alur unduh SATU video — hasil langsung di downloads/ tanpa prefix nomor."""
    try:
        info = fetch_video_info(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✖ Gagal membaca info video: {exc}")
        print("    Periksa URL / koneksi internet, lalu coba lagi.")
        return

    title = info["title"]
    meta_bits = []
    if info.get("uploader"):
        meta_bits.append(info["uploader"])
    if info.get("duration"):
        meta_bits.append(format_duration(info["duration"]))
    meta_str = f" ({' · '.join(meta_bits)})" if meta_bits else ""
    print(f"\n  🎬 Video ditemukan: \"{title}\"{meta_str}")

    fmt = choose_format()
    if fmt is None:
        print("  Kembali ke menu utama...")
        return

    warn_ffmpeg_missing(fmt)

    out_dir = DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    label = "audio MP3" if fmt == AUDIO_FMT else "video"
    confirm = ask_continue(f"  Lanjut unduh {label} \"{title}\" ke \"{out_dir}\"?")
    if confirm is None or not confirm:
        print("  Dibatalkan. Kembali ke menu utama...")
        return

    # Pakai URL watch kanonik (memutus kaitan list= bila ada); fallback URL input
    video_url = (
        f"https://www.youtube.com/watch?v={info['id']}" if info["id"] else url
    )

    print("\n  🚀 Mengunduh...")
    start = time.time()
    success = download_video(video_url, out_dir, None, fmt)
    elapsed = time.time() - start

    log_line(f"SINGLE | {'OK' if success else 'GAGAL'} | {title} | {url}")

    failed_store = load_failed()
    if success:
        remove_failed_entry(failed_store, url)
        remove_failed_entry(failed_store, video_url)
        save_failed(failed_store)
        print("\n" + "=" * 44)
        print(f"  ✅ Selesai dalam {format_duration(elapsed)}")
        print(f"  📁 Hasil tersimpan di: {out_dir}")
        print("=" * 44)
    else:
        add_failed_entry(failed_store, "", 0, title, url, fmt, kind="single")
        save_failed(failed_store)
        print("\n" + "=" * 44)
        print("  ✖ Video gagal diunduh dan sudah dicatat ke daftar gagal.")
        print("     Coba lagi lewat menu 2 → 'Unduh ulang yang gagal'.")
        print("=" * 44)
    print("  Kembali ke menu utama...")


def run_playlist_download(url: str) -> None:
    """Alur unduh SELURUH playlist secara serial (baseline v1)."""
    try:
        playlist_title, entries = fetch_playlist_info(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✖ Gagal mengambil daftar playlist: {exc}")
        print("    Periksa URL / koneksi internet, lalu coba lagi.")
        return

    if not entries:
        print("  ℹ️ Playlist kosong atau tidak bisa dibaca.")
        return

    print(f"\n  📂 Playlist \"{playlist_title}\" berisi {len(entries)} video.")

    fmt = choose_format()
    if fmt is None:
        print("  Kembali ke menu utama...")
        return

    warn_ffmpeg_missing(fmt)

    out_dir = DOWNLOADS_DIR / safe_name(playlist_title)
    out_dir.mkdir(parents=True, exist_ok=True)

    confirm = ask_continue(f"  Lanjut unduh {len(entries)} video ke \"{out_dir}\"?")
    if confirm is None or not confirm:
        print("  Dibatalkan. Kembali ke menu utama...")
        return

    # Unduh serial (satu per satu)
    print(f"\n  🚀 Mengunduh {len(entries)} video secara serial...")
    start = time.time()
    ok_count, fail_count = 0, 0
    failed_list: list[str] = []
    failed_store = load_failed()

    for i, entry in enumerate(entries, start=1):
        video_id = entry.get("id")
        if not video_id:
            print(f"\n  [{i}/{len(entries)}] ⚠️ Video tidak tersedia (di-skip).")
            fail_count += 1
            log_line(f"{playlist_title} | {i}/{len(entries)} | GAGAL | (no id)")
            continue
        title = entry.get("title") or f"video-{video_id}"
        print(f"\n  [{i}/{len(entries)}] 📥 {title}")
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        success = download_video(video_url, out_dir, i, fmt)
        if success:
            ok_count += 1
            remove_failed_entry(failed_store, video_url)
        else:
            fail_count += 1
            failed_list.append(f"{i}. {title}")
            add_failed_entry(failed_store, playlist_title, i, title, video_url, fmt)
        log_line(f"{playlist_title} | {i}/{len(entries)} | {'OK' if success else 'GAGAL'} | {title} | {video_url}")
    save_failed(failed_store)

    elapsed = time.time() - start
    print("\n" + "=" * 44)
    print(f"  ✅ Selesai: {ok_count} berhasil · {fail_count} gagal · total {format_duration(elapsed)}")
    if failed_list:
        print("  Video yang gagal:")
        for item in failed_list:
            print(f"    • {item}")
    print(f"  📁 Hasil tersimpan di: {out_dir}")
    print("=" * 44)
    print("  Kembali ke menu utama...")


def choose_download_scope(url: str) -> str | None:
    """Untuk URL watch yang membawa list=: tanya video ini saja atau seluruh playlist.

    Return 'video' | 'playlist' | None (kembali ke menu utama).
    """
    print("\n  ℹ️ URL ini adalah halaman video yang sekaligus bagian dari sebuah playlist.")
    while True:
        print("  Unduh apa?")
        print("    [1] Hanya video ini          ← default (Enter)")
        print("    [2] Seluruh playlist")
        print("    [0] Kembali ke menu utama   (atau ketik 'k')")
        choice = input("  Pilihan [1]: ").strip()
        if not choice or choice == "1":
            return "video"
        if is_back(choice):
            return None
        if choice == "2":
            return "playlist"
        print("  ✖ Pilihan tidak valid. Coba lagi.")


def run_download(url: str) -> None:
    """Dispatcher: kenali jenis URL lalu route ke alur single / playlist."""
    print("\n" + "=" * 44)
    print("  MULAI UNDUH")
    print("=" * 44)

    if not ensure_prereqs():
        return

    scope = detect_url_kind(url)
    if scope == "video" or is_watch_with_playlist(url):
        if is_watch_with_playlist(url):
            picked = choose_download_scope(url)
            if picked is None:
                print("  Kembali ke menu utama...")
                return
            scope = picked
        else:
            scope = "video"

    if scope == "video":
        run_single_download(url)
    else:
        run_playlist_download(url)


def retry_failed() -> None:
    """Sub-opsi 'Mulai unduh': unduh ulang video yang gagal (maks MAX_RETRIES percobaan).

    Entri 'single' diunduh ulang ke downloads/ root tanpa prefix;
    entri playlist ke folder playlist-nya dengan prefix seperti biasa.
    Entri lama (tanpa field kind) diperlakukan sebagai playlist.
    """
    print("\n" + "=" * 44)
    print("  UNDUH ULANG VIDEO GAGAL")
    print("=" * 44)

    failed_list = load_failed()
    if not failed_list:
        print("  ℹ️ Tidak ada video gagal yang tercatat untuk diunduh ulang.")
        print("     (Video gagal dicatat di failed-videos.json saat unduhan sebelumnya.)")
        input("  Tekan Enter untuk kembali...")
        return

    if not ytdlp_installed():
        print("  ⚠️ Prasyarat belum terinstall. Silakan pilih menu 1 (Install) dulu.")
        input("  Tekan Enter untuk kembali...")
        return

    print(f"\n  Daftar {len(failed_list)} video yang gagal (dari log unduhan):")
    for n, entry in enumerate(failed_list, start=1):
        kind = entry.get("kind") or "playlist"
        idx = entry.get("index")
        head = f"{idx}. " if kind != "single" and idx else ""
        source = "(single)" if kind == "single" or not entry.get("playlist") else entry["playlist"]
        print(f"    [{n}] {head}{entry.get('title', '?')}")
        print(f"        {source} · {entry.get('url', '?')}")

    print("\n  Pilih nomor yang mau diunduh ulang (pisahkan koma, contoh: 1,3,5)")
    print("  atau ketik 'all' untuk semua — 'k'/'0' = kembali ke menu utama")
    choice = input("  Pilihan: ").strip().lower()
    if is_back(choice):
        print("  Kembali ke menu utama...")
        return

    if choice == "all":
        selected = list(range(len(failed_list)))
    else:
        selected = []
        for part in choice.replace(" ", "").split(","):
            if part.isdigit():
                n = int(part)
                if 1 <= n <= len(failed_list):
                    selected.append(n - 1)
        selected = list(dict.fromkeys(selected))  # hapus duplikat, jaga urutan
        if not selected:
            print("  ✖ Pilihan tidak valid. Kembali ke menu utama...")
            return

    selected_entries = [failed_list[idx] for idx in selected]
    print(
        f"\n  🚀 Mengunduh ulang {len(selected_entries)} video "
        f"(maks {MAX_RETRIES} percobaan per video)..."
    )
    start = time.time()
    ok_count, fail_count = 0, 0
    success_urls: set[str] = set()
    still_failed: list[dict] = []

    for entry in selected_entries:
        title = entry.get("title") or "?"
        url = entry.get("url") or ""
        fmt = entry.get("fmt") or VIDEO_RESOLUTIONS[DEFAULT_RESOLUTION][1]
        kind = entry.get("kind") or "playlist"
        playlist_label = entry.get("playlist") or ""
        if kind == "single" or not playlist_label:
            out_dir = DOWNLOADS_DIR
            dl_index: int | None = None
        else:
            out_dir = DOWNLOADS_DIR / safe_name(playlist_label)
            dl_index = entry.get("index") or 0
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n  🔄 {title}")
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"    Percobaan {attempt}/{MAX_RETRIES}...")
            if download_video(url, out_dir, dl_index, fmt):
                success = True
                break
        if success:
            ok_count += 1
            success_urls.add(url)
            log_line(f"UNDUH-ULANG | OK | {title} | {url}")
        else:
            fail_count += 1
            still_failed.append(entry)
            log_line(f"UNDUH-ULANG | GAGAL ({MAX_RETRIES}x) | {title} | {url}")

    # Simpan ulang daftar gagal: hapus yang berhasil, pertahankan sisanya
    failed_list[:] = [e for e in failed_list if e.get("url") not in success_urls]
    save_failed(failed_list)

    elapsed = time.time() - start
    print("\n" + "=" * 44)
    print(
        f"  ✅ Selesai: {ok_count} berhasil diunduh ulang · {fail_count} tetap gagal · "
        f"{format_duration(elapsed)}"
    )
    if still_failed:
        print("  Video yang masih gagal (bisa dicoba lagi lain kali):")
        for e in still_failed:
            print(f"    • {e.get('title', '?')}")
    print("=" * 44)
    print("  Kembali ke menu utama...")


# ---------------------------------------------------------------------------
# Bersihkan riwayat video gagal (menu 4)
# ---------------------------------------------------------------------------

def clear_failed_history() -> None:
    """Menu 4 — Kosongkan riwayat video gagal (hapus failed-videos.json).

    Hanya riwayat kegagalan yang dihapus; hasil unduhan di downloads/
    TIDAK disentuh.
    """
    print("\n" + "=" * 44)
    print("  BERSIHKAN RIWAYAT VIDEO GAGAL")
    print("=" * 44)

    failed_list = load_failed()
    if not failed_list:
        print("  ℹ️ Tidak ada riwayat video gagal. Semua sudah bersih.")
        input("  Tekan Enter untuk kembali ke menu utama...")
        return

    print(f"  Daftar {len(failed_list)} riwayat gagal yang akan dihapus:")
    for n, entry in enumerate(failed_list, start=1):
        kind = entry.get("kind") or "playlist"
        idx = entry.get("index")
        head = f"{idx}. " if kind != "single" and idx else ""
        source = (
            "(single)"
            if kind == "single" or not entry.get("playlist")
            else entry["playlist"]
        )
        print(f"    [{n}] {head}{entry.get('title', '?')}")
        print(f"        {source} · {entry.get('url', '?')}")

    answer = input("  Hapus semua riwayat ini? (y/N) — 'k'/'0' = batal: ").strip().lower()
    if is_back(answer) or answer not in ("y", "yes"):
        print("  Dibatalkan. Kembali ke menu utama...")
        return

    try:
        FAILED_FILE.unlink(missing_ok=True)
    except OSError as exc:
        print(f"  ✖ Gagal menghapus {FAILED_FILE.name}: {exc}")
        input("  Tekan Enter untuk kembali ke menu utama...")
        return

    log_line(f"BERSIH-RIWAYAT | {len(failed_list)} entri riwayat gagal dihapus")
    print(f"  ✅ {len(failed_list)} riwayat gagal sudah dihapus.")
    print("     (Hasil unduhan di downloads/ TIDAK disentuh.)")
    print("  Kembali ke menu utama...")


# ---------------------------------------------------------------------------
# Menu utama
# ---------------------------------------------------------------------------

def print_menu() -> None:
    print("\n" + "=" * 44)
    print("   YT DOWNLOAD — Unduh Video & Playlist")
    print("=" * 44)
    print("   1. Install prasyarat")
    print("   2. Mulai unduh")
    print("   3. Update yt-dlp")
    print("   4. Bersihkan riwayat gagal")
    print("   5. Uninstall")
    print("   0. Keluar")
    print("-" * 44)


def menu_loop() -> None:
    while True:
        print_menu()
        choice = input("  Pilih menu [1/2/3/4/5/0]: ").strip()
        if choice == "1":
            setup_prereqs()
        elif choice == "2":
            failed_count = len(load_failed())
            print("\n" + "-" * 44)
            print("  MULAI UNDUH — pilih jenis:")
            print("    [1] Unduh baru (video tunggal atau playlist)")
            print(f"    [2] Unduh ulang yang gagal ({failed_count} video gagal terdata)")
            print("    [0] Kembali ke menu utama")
            sub = input("  Pilihan [1/2/0]: ").strip().lower()
            if sub == "1":
                url = input_youtube_url()
                if url is None:
                    continue  # kembali ke menu utama
                run_download(url)
            elif sub == "2":
                retry_failed()
            else:
                continue  # kembali ke menu utama
        elif choice == "3":
            update_ytdlp()
        elif choice == "4":
            clear_failed_history()
        elif choice == "5":
            uninstall_prereqs()
        elif choice == "0":
            print("  Sampai jumpa! 👋")
            break
        else:
            print("  ✖ Pilihan tidak valid. Pilih 1, 2, 3, 4, 5, atau 0.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Unduh video tunggal ATAU seluruh playlist YouTube "
            "sebagai video MP4 atau audio MP3."
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="URL YouTube video tunggal atau playlist (opsional, lewati menu utama)",
    )
    parser.add_argument("--setup", action="store_true", help="Pintasan menu 1: install prasyarat")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Pintasan menu 3: hapus prasyarat (script tetap ada)",
    )
    args = parser.parse_args()

    if args.setup:
        setup_prereqs()
    elif args.uninstall:
        uninstall_prereqs()
    elif args.url:
        run_download(args.url)
    else:
        menu_loop()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C, atau stdin habis (Ctrl+Z/Ctrl+D) -> keluar dengan rapi
        print("\n  Dibatalkan user. Sampai jumpa! 👋")
