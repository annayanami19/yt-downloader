#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YT DOWNLOAD — GUI (antarmuka grafis untuk yt_download.py)

Fitur:
  - Tempel URL video/playlist -> Cek Info -> pilih format (MP4/MP3 + resolusi)
  - Progres realtime: bar total (playlist), bar per-file, kecepatan & ETA
  - URL watch+list: pilih cakupan "Video ini saja" / "Seluruh playlist"
  - Batalkan unduhan di tengah jalan (tidak dicatat sebagai gagal)
  - Tab Riwayat Gagal: pilih video -> unduh ulang (maks MAX_RETRIES/video)
  - Queue unduhan manual: tambahkan banyak URL ke queue (format/resolusi
    boleh berbeda per item) lalu jalankan semuanya dalam SATU klik —
    diproses berurutan tanpa perlu satu-satu
  - Bersihkan riwayat gagal, Install prasyarat, Update yt-dlp,
    Buka folder unduhan, Uninstall — semuanya tanpa konsol
  - Semua log aktivitas tampil di tab Log (log harian tetap ditulis yt_download)

Arsitektur:
  - Logika unduh/prasyarat 100% dari yt_download.py (dipakai ulang, tanpa duplikasi)
  - Pekerjaan berat jalan di threading.Thread; UI di-update lewat queue.Queue
    yang dipolling dengan after() — aman dari aturan single-thread Tkinter
  - Progres yt-dlp masuk lewat callback (core.download_video(progress_cb=...))

Launcher Windows: klik ganda YT-DOWNLOAD-GUI.bat (auto-install customtkinter).
"""

from __future__ import annotations

import contextlib
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# Saat dijalankan via pythonw (tanpa konsol), stdout/stderr = None.
# Arahkan ke devnull supaya print() internal library tidak pernah error.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _ensure_customtkinter() -> bool:
    """Pastikan customtkinter tersedia; install sendiri bila belum ada."""
    try:
        import customtkinter  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "customtkinter"],
            check=False,
        )
        import customtkinter  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


if not _ensure_customtkinter():
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "Gagal menyiapkan tampilan GUI (customtkinter).\n"
            "Jalankan manual di CMD:\n\n    pip install -U customtkinter\n\n"
            "lalu buka lagi YT-DOWNLOAD-GUI.bat",
            "YT DOWNLOAD — GUI",
            0x10,
        )
    except Exception:  # noqa: BLE001
        pass
    sys.exit(1)

import customtkinter as ctk  # noqa: E402
from tkinter import Menu, messagebox  # noqa: E402

import yt_download as core  # noqa: E402

REFRESH_MS = 80  # interval polling queue event -> UI

# Jeda anti-bot khusus queue — rekomendasi wiki yt-dlp untuk sesi tamu (tanpa
# cookies): 5-10 dtk antar video; di bawah itu rawan soft-block/CAPTCHA
# (limit guest +-300 video/jam). Batas minimal TIDAK bisa diubah dari UI.
QUEUE_DELAY_MIN_S = 5.0
QUEUE_DELAY_CHOICES = ["5", "8", "10", "15", "20", "30", "60"]  # nilai dasar (dtk)


class UserCancel(Exception):
    """Dilempar di dalam progress hook utk membatalkan unduhan berjalan."""


class QueueWriter:
    """Stream penulisan -> queue event "log"/"status".

    Dipakai utk redirect_stdout() saat memanggil fungsi CLI (setup/update)
    di worker thread, supaya print()-nya tampil di tab Log GUI.
    """

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> None:
        for part in re.split(r"[\r\n]", s):
            text = part.strip()
            if not text:
                continue
            if re.match(r"^\d+\s*%", text):  # progres unduhan ffmpeg (murni %)
                self.q.put(("status", f"⏳ Mengunduh ffmpeg portable... {text}"))
            else:
                self.q.put(("log", text))

    def flush(self) -> None:
        pass


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()
        self._info_cache: dict | None = None  # hasil Cek Info utk URL sama
        self._failed_rows: list[dict] = []
        self._queue_items: list[dict] = []  # antrean URL tambahan manual

        self.title("YT DOWNLOAD — Unduh Video & Playlist YouTube")
        self.geometry("960x730")
        self.minsize(880, 640)
        self._center(960, 730)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_download_card()
        self._build_tabs()
        self._build_footer()

        self.after(REFRESH_MS, self._poll)
        self.refresh_status()
        self.refresh_failed_list()
        self.refresh_queue_list()
        threading.excepthook = self._thread_excepthook
        self.log("Selamat datang! Tempel URL lalu klik Mulai Unduh — atau "
                 "➕ Tambah ke Queue untuk mengunduh banyak URL sekaligus.")

    # ------------------------------------------------------------------
    # Konstruksi UI
    # ------------------------------------------------------------------

    def _center(self, w: int, h: int) -> None:
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{max((sh - h) // 2 - 20, 0)}")

    def _build_header(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        head.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            left, text="⬇ YT DOWNLOAD", font=("Segoe UI", 24, "bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Unduh video tunggal atau seluruh playlist YouTube — MP4 / MP3",
            font=("Segoe UI", 12),
            text_color=("#6b7280", "#9ca3af"),
        ).pack(anchor="w")

        chips = ctk.CTkFrame(head, fg_color="transparent")
        chips.grid(row=0, column=1, sticky="ne")
        self.chip_ytdlp = ctk.CTkLabel(
            chips, text="yt-dlp: memeriksa...", corner_radius=8,
            fg_color=("#e5e7eb", "#374151"), font=("Segoe UI", 11, "bold"),
        )
        self.chip_ytdlp.pack(anchor="e", pady=(0, 4), padx=4)
        self.chip_ffmpeg = ctk.CTkLabel(
            chips, text="ffmpeg: memeriksa...", corner_radius=8,
            fg_color=("#e5e7eb", "#374151"), font=("Segoe UI", 11, "bold"),
        )
        self.chip_ffmpeg.pack(anchor="e", padx=4)

    def _build_download_card(self) -> None:
        card = ctk.CTkFrame(self, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=18, pady=8)
        card.grid_columnconfigure(0, weight=1)
        inner = 18

        # Baris URL
        url_row = ctk.CTkFrame(card, fg_color="transparent")
        url_row.grid(row=0, column=0, sticky="ew", padx=inner, pady=(inner, 6))
        url_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(
            url_row,
            placeholder_text=(
                "Tempel URL di sini — contoh: https://www.youtube.com/watch?v=... "
                "atau https://www.youtube.com/playlist?list=..."
            ),
            height=38,
            font=("Segoe UI", 13),
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda _e: self.on_check_info())
        self._build_url_menu()
        self.btn_info = ctk.CTkButton(
            url_row, text="Cek Info", width=110, height=38, command=self.on_check_info
        )
        self.btn_info.grid(row=0, column=1)

        # Info video/playlist
        self.info_label = ctk.CTkLabel(
            card,
            text="📎 Info akan tampil di sini setelah URL dicek (opsional sebelum unduh).",
            justify="left", anchor="w", wraplength=860, font=("Segoe UI", 12),
            text_color=("#374151", "#d1d5db"),
        )
        self.info_label.grid(row=1, column=0, sticky="ew", padx=inner)

        # Cakupan (hanya utk URL watch yang membawa list=)
        self.scope_seg = ctk.CTkSegmentedButton(
            card,
            values=["🎬 Video ini saja", "📂 Seluruh playlist"],
            height=32,
            font=("Segoe UI", 12),
        )
        self.scope_seg.set("🎬 Video ini saja")

        # Baris format + aksi
        fmt_row = ctk.CTkFrame(card, fg_color="transparent")
        fmt_row.grid(row=3, column=0, sticky="ew", padx=inner, pady=(10, 0))
        self.fmt_seg = ctk.CTkSegmentedButton(
            fmt_row,
            values=["🎬 Video (MP4)", "🎵 Audio (MP3)"],
            height=34, font=("Segoe UI", 12, "bold"),
            command=self._on_fmt_change,
        )
        self.fmt_seg.set("🎬 Video (MP4)")
        self.fmt_seg.grid(row=0, column=0, padx=(0, 10))

        ctk.CTkLabel(
            fmt_row, text="Resolusi:", font=("Segoe UI", 12)
        ).grid(row=0, column=1, padx=(0, 6))
        self._res_labels = [
            core.VIDEO_RESOLUTIONS[k][0] if k != "5" else "Best (tertinggi)"
            for k in ("1", "2", "3", "4", "5")
        ]
        self._res_fmt = {
            core.VIDEO_RESOLUTIONS[k][0] if k != "5" else "Best (tertinggi)":
                core.VIDEO_RESOLUTIONS[k][1]
            for k in ("1", "2", "3", "4", "5")
        }
        self.res_menu = ctk.CTkOptionMenu(
            fmt_row, values=self._res_labels, height=34, width=170,
            font=("Segoe UI", 12),
        )
        self.res_menu.set("720p")
        self.res_menu.grid(row=0, column=2, padx=(0, 12))

        self.btn_queue_add = ctk.CTkButton(
            fmt_row, text="➕ Tambah ke Queue", width=150, height=38,
            command=self.on_add_to_queue,
        )
        self.btn_queue_add.grid(row=0, column=3, padx=(0, 8))
        self.btn_download = ctk.CTkButton(
            fmt_row, text="⬇  Mulai Unduh", width=150, height=38,
            font=("Segoe UI", 14, "bold"),
            fg_color=("#16a34a", "#15803d"), hover_color=("#22c55e", "#16a34a"),
            command=self.on_start_download,
        )
        self.btn_download.grid(row=0, column=4, padx=(0, 8))
        self.btn_cancel = ctk.CTkButton(
            fmt_row, text="Batalkan", width=100, height=38,
            fg_color=("#9ca3af", "#4b5563"), hover_color=("#6b7280", "#6b7280"),
            state="disabled", command=self.on_cancel,
        )
        self.btn_cancel.grid(row=0, column=5)

        # Status + progress
        self.status_label = ctk.CTkLabel(
            card, text="Siap.", anchor="w", justify="left", wraplength=860,
            font=("Segoe UI", 12),
        )
        self.status_label.grid(row=4, column=0, sticky="ew", padx=inner, pady=(10, 2))

        bars = ctk.CTkFrame(card, fg_color="transparent")
        bars.grid(row=5, column=0, sticky="ew", padx=inner, pady=(2, inner))
        bars.grid_columnconfigure(0, weight=1)
        bars.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(bars, text="Total", font=("Segoe UI", 11), width=44,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.bar_total = ctk.CTkProgressBar(bars, height=14)
        self.bar_total.grid(row=0, column=1, sticky="ew", padx=(2, 8))
        self.bar_total.set(0)
        self.lbl_total = ctk.CTkLabel(bars, text="—", font=("Segoe UI", 11), width=210,
                                      anchor="w")
        self.lbl_total.grid(row=0, column=2, sticky="w")

        ctk.CTkLabel(bars, text="File", font=("Segoe UI", 11), width=44,
                     anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.bar_file = ctk.CTkProgressBar(bars, height=14)
        self.bar_file.grid(row=1, column=1, sticky="ew", padx=(2, 8), pady=(4, 0))
        self.bar_file.set(0)
        self.lbl_file = ctk.CTkLabel(bars, text="—", font=("Segoe UI", 11), width=210,
                                     anchor="w")
        self.lbl_file.grid(row=1, column=2, sticky="w", pady=(4, 0))

    def _build_tabs(self) -> None:
        tabs = ctk.CTkTabview(self, corner_radius=14)
        tabs.grid(row=2, column=0, sticky="nsew", padx=18, pady=8)
        self.tabs = tabs

        # --- Tab Riwayat Gagal ---
        tab_g = tabs.add("⚠ Riwayat Gagal")
        tab_g.grid_columnconfigure(0, weight=1)
        tab_g.grid_rowconfigure(0, weight=1)

        self.fail_scroll = ctk.CTkScrollableFrame(tab_g, fg_color="transparent")
        self.fail_scroll.grid(row=0, column=0, sticky="nsew")

        btns_g = ctk.CTkFrame(tab_g, fg_color="transparent")
        btns_g.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.btn_fail_reload = ctk.CTkButton(
            btns_g, text="🔄 Muat ulang", width=110, command=self.refresh_failed_list
        )
        self.btn_fail_reload.pack(side="left")
        self.btn_fail_retry_sel = ctk.CTkButton(
            btns_g, text="⬇ Unduh ulang terpilih", width=170,
            command=self.on_retry_selected, state="disabled",
        )
        self.btn_fail_retry_sel.pack(side="left", padx=(8, 0))
        self.btn_fail_retry_all = ctk.CTkButton(
            btns_g, text="⬇ Unduh ulang semua", width=160,
            command=self.on_retry_all, state="disabled",
        )
        self.btn_fail_retry_all.pack(side="left", padx=(8, 0))
        self.btn_fail_clear = ctk.CTkButton(
            btns_g, text="🗑 Bersihkan riwayat", width=150,
            fg_color=("#dc2626", "#991b1b"), hover_color=("#ef4444", "#b91c1c"),
            command=self.on_clear_history, state="disabled",
        )
        self.btn_fail_clear.pack(side="right")

        # --- Tab Log ---
        tab_l = tabs.add("📜 Log")
        tab_l.grid_columnconfigure(0, weight=1)
        tab_l.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            tab_l, font=("Consolas", 12), wrap="word", state="disabled"
        )
        self.log_box.grid(row=0, column=0, sticky="nsew")

        # --- Tab Queue ---
        tab_q = tabs.add("📋 Queue")
        tab_q.grid_columnconfigure(0, weight=1)
        tab_q.grid_rowconfigure(0, weight=1)

        self.queue_scroll = ctk.CTkScrollableFrame(tab_q, fg_color="transparent")
        self.queue_scroll.grid(row=0, column=0, sticky="nsew")

        # Baris pengaturan jeda anti-bot (khusus queue)
        delay_row = ctk.CTkFrame(tab_q, fg_color="transparent")
        delay_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            delay_row, text="⏱ Jeda antar video:", font=("Segoe UI", 12)
        ).pack(side="left")
        self.queue_delay_menu = ctk.CTkOptionMenu(
            delay_row, values=QUEUE_DELAY_CHOICES, height=30, width=100,
            font=("Segoe UI", 12),
        )
        self.queue_delay_menu.set(QUEUE_DELAY_CHOICES[0])
        self.queue_delay_menu.pack(side="left", padx=(6, 8))
        ctk.CTkLabel(
            delay_row,
            text="dtk — diacak 1–2× per video. Minimal 5 dtk sesuai rekomendasi "
                 "yt-dlp (sesi tamu); jeda acak & batas minimal tetap, tidak bisa diubah.",
            font=("Segoe UI", 11), anchor="w",
            text_color=("#6b7280", "#9ca3af"),
        ).pack(side="left")

        btns_q = ctk.CTkFrame(tab_q, fg_color="transparent")
        btns_q.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.btn_queue_run = ctk.CTkButton(
            btns_q, text="▶ Jalankan Queue (0)", width=180, height=34,
            font=("Segoe UI", 12, "bold"),
            fg_color=("#16a34a", "#15803d"), hover_color=("#22c55e", "#16a34a"),
            command=self.on_run_queue, state="disabled",
        )
        self.btn_queue_run.pack(side="left")
        self.btn_queue_clear = ctk.CTkButton(
            btns_q, text="🗑 Kosongkan Queue", width=150,
            fg_color=("#dc2626", "#991b1b"), hover_color=("#ef4444", "#b91c1c"),
            command=self.on_clear_queue, state="disabled",
        )
        self.btn_queue_clear.pack(side="right")

    def _build_footer(self) -> None:
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=18, pady=(2, 14))

        self.btn_setup = ctk.CTkButton(
            foot, text="🧰 Install Prasyarat", width=150, command=self.on_setup
        )
        self.btn_setup.pack(side="left")
        self.btn_update = ctk.CTkButton(
            foot, text="🔄 Update yt-dlp", width=140, command=self.on_update_ytdlp
        )
        self.btn_update.pack(side="left", padx=(8, 0))
        self.btn_open = ctk.CTkButton(
            foot, text="📁 Buka Folder Unduhan", width=180,
            command=self.on_open_downloads,
        )
        self.btn_open.pack(side="left", padx=(8, 0))
        self.btn_clear_screen = ctk.CTkButton(
            foot, text="🧹 Bersihkan Tampilan", width=170,
            command=self.on_clear_screen,
        )
        self.btn_clear_screen.pack(side="left", padx=(8, 0))

        self.btn_uninstall = ctk.CTkButton(
            foot, text="Uninstall prasyarat", width=140,
            fg_color=("#9ca3af", "#374151"), hover_color=("#6b7280", "#4b5563"),
            text_color=("#111827", "#d1d5db"), command=self.on_uninstall,
        )
        self.btn_uninstall.pack(side="right")

    # ------------------------------------------------------------------
    # Util UI
    # ------------------------------------------------------------------

    def log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _indeterminate(self, on: bool) -> None:
        if on:
            for bar in (self.bar_total, self.bar_file):
                bar.configure(mode="indeterminate")
                bar.start()  # API CustomTkinter: tanpa argumen (bukan ttk.start(ms))
        else:
            for bar in (self.bar_total, self.bar_file):
                bar.stop()
                bar.configure(mode="determinate")
                bar.set(0)

    def _set_busy(self, on: bool, task: str = "") -> None:
        self.busy = on
        state = "disabled" if on else "normal"
        for btn in (
            self.btn_download, self.btn_info, self.btn_setup, self.btn_update,
            self.btn_uninstall, self.btn_fail_reload, self.btn_fail_clear,
            self.btn_fail_retry_sel, self.btn_fail_retry_all,
            self.btn_queue_run, self.btn_queue_clear, self.queue_delay_menu,
        ):
            btn.configure(state=state)
        self.btn_cancel.configure(state="normal" if on else "disabled")
        if on:
            self.set_status(f"⏳ {task}")
            self._indeterminate(True)
        else:
            self.btn_cancel.configure(state="disabled")
            self._indeterminate(False)
        self._update_fail_buttons_state()
        self._update_queue_buttons_state()

    def _update_fail_buttons_state(self) -> None:
        if self.busy:
            return
        has = bool(self._failed_rows)
        self.btn_fail_retry_all.configure(
            state="normal" if has else "disabled")
        self.btn_fail_clear.configure(state="normal" if has else "disabled")
        any_checked = any(r["var"].get() for r in self._failed_rows)
        self.btn_fail_retry_sel.configure(
            state="normal" if any_checked else "disabled")

    # ------------------------------------------------------------------
    # Polling queue -> UI (dipanggil di main thread)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                try:
                    self._handle_event(ev)
                except Exception:  # noqa: BLE001 - 1 event gagal tak boleh mematikan UI
                    self.log("✖ Error pembaruan UI:\n" + traceback.format_exc())
        except queue.Empty:
            pass
        self.after(REFRESH_MS, self._poll)

    def report_callback_exception(self, exc, val, tb) -> None:  # type: ignore[override]
        """Error callback Tkinter (terlihat tidak apa-apa di pythonw) -> tab Log."""
        try:
            detail = "".join(traceback.format_exception(exc, val, tb))
            self.log(f"✖ UI error tak tertangani: {val!r}\n{detail}")
        except Exception:  # noqa: BLE001
            pass

    def _thread_excepthook(self, args: threading.ExceptHookArgs) -> None:
        """Thread yang mati di luar wrapper worker -> catat ke tab Log."""
        try:
            self.q.put(("log", f"✖ Thread error tak tertangani: {args.exc_value!r}"))
        except Exception:  # noqa: BLE001
            pass

    def _handle_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self.log(ev[1])
        elif kind == "status":
            self.set_status(ev[1])
        elif kind == "info_video":
            up, dur, title = ev[1], ev[2], ev[3]
            bits = [b for b in (up, core.format_duration(dur) if dur else "") if b]
            meta = f" ({' · '.join(bits)})" if bits else ""
            self.info_label.configure(text=f"🎬 Video ditemukan: “{title}”{meta}")
            self.tabs.set("📜 Log")
        elif kind == "info_playlist":
            title, count = ev[1], ev[2]
            self.info_label.configure(
                text=f"📂 Playlist “{title}” — berisi {count} video."
            )
            self.tabs.set("📜 Log")
        elif kind == "show_scope":
            self.scope_seg.grid(row=2, column=0, sticky="w", padx=18, pady=(8, 0))
        elif kind == "hide_scope":
            self.scope_seg.grid_forget()
        elif kind == "video_start":
            i, total, title = ev[1], ev[2], ev[3]
            self.lbl_total.configure(text=f"Video {i}/{total}")
            self.set_status(f"⏳ [{i}/{total}] {title}")
        elif kind == "item_start":
            i, total, title = ev[1], ev[2], ev[3]
            self.lbl_total.configure(text=f"Item {i}/{total}")
            self.set_status(f"⏳ [{i}/{total}] {title}")
        elif kind == "video_done":
            i, total, title, ok = ev[1], ev[2], ev[3], ev[4]
            mark = "✅" if ok else "❌"
            self.log(f"{mark} [{i}/{total}] {title}")
        elif kind == "overall":
            self.bar_total.set(ev[1])
        elif kind == "file_progress":
            _, pct, done, total, speed, eta = ev
            self.bar_file.set(pct)
            bits = [f"{pct*100:.1f}%", f"{done}/{total}"]
            if speed:
                bits.append(speed)
            if eta is not None:
                bits.append(f"ETA {core.format_duration(eta)}")
            self.lbl_file.configure(text=" · ".join(bits))
            if pct:
                self.bar_total.set(max(self.bar_total.get(), 0))  # jaga mode determinate
        elif kind == "file_reset":
            self.bar_file.set(0)
            self.lbl_file.configure(text="—")
        elif kind == "summary":
            text, is_error = ev[1], ev[2]
            self.set_status(text)
            self.log(text)
            (messagebox.showwarning if is_error else messagebox.showinfo)(
                "YT DOWNLOAD", text
            )
        elif kind == "failed_refresh":
            self.refresh_failed_list()
        elif kind == "queue_advance":
            url = ev[1]
            self._queue_items = [it for it in self._queue_items if it["url"] != url]
            self.refresh_queue_list()
        elif kind == "status_refresh":
            self.refresh_status()
        elif kind == "worker_done":
            self._set_busy(False)
            self.set_status("Siap.")
            self.refresh_status()

    # ------------------------------------------------------------------
    # Worker helpers (threading)
    # ------------------------------------------------------------------

    def _start_worker(self, task: str, target) -> bool:
        if self.busy:
            messagebox.showinfo(
                "Sedang sibuk",
                "Ada proses yang sedang berjalan.\nTunggu sampai selesai atau klik Batalkan.",
            )
            return False
        self.cancel_event.clear()
        self._set_busy(True, task)

        def wrap():
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - laporkan apa pun ke GUI
                self.q.put(("log", "✖ Error tak terduga:\n" + traceback.format_exc()))
                self.q.put(("summary", f"Terjadi error: {exc}", True))
            finally:
                self.q.put(("worker_done",))

        threading.Thread(target=wrap, daemon=True).start()
        return True

    def _progress_cb(self):
        """Callback progres yt-dlp utk GUI + titik pembatalan."""

        def cb(d: dict) -> None:
            if self.cancel_event.is_set():
                raise UserCancel()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = (done / total) if total else 0.0
                self.q.put((
                    "file_progress", pct,
                    core.format_size(done), core.format_size(total),
                    core.format_speed(d.get("speed")), d.get("eta"),
                ))
            elif d.get("status") == "finished":
                self.q.put(("file_progress", 1.0, "", "", "", None))

        return cb

    def _get_url(self) -> str | None:
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning(
                "URL kosong",
                "Tempel dulu URL video/playlist YouTube ke kolom URL.",
            )
            return None
        if not core.is_youtube_url(url):
            messagebox.showwarning(
                "URL tidak dikenali",
                "URL tidak terlihat seperti video/playlist YouTube.\n\n"
                "Contoh video   : https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
                "                  https://youtu.be/dQw4w9WgXcQ\n"
                "Contoh playlist: https://www.youtube.com/playlist?list=PL...",
            )
            return None
        return url

    def _resolve_scope(self, url: str) -> str:
        """'video' | 'playlist' — dari pilihan cakupan / jenis URL."""
        if core.is_watch_with_playlist(url):
            return "playlist" if "Seluruh playlist" in self.scope_seg.get() else "video"
        return core.detect_url_kind(url)

    def _on_fmt_change(self, value: str) -> None:
        """Resolusi hanya relevan untuk mode Video; matikan saat MP3."""
        self.res_menu.configure(state="disabled" if "MP3" in value else "normal")

    def _picked_format(self) -> str:
        if "MP3" in self.fmt_seg.get():
            return core.AUDIO_FMT
        return self._res_fmt.get(self.res_menu.get(), core.VIDEO_RESOLUTIONS["3"][1])

    # ------------------------------------------------------------------
    # Aksi user
    # ------------------------------------------------------------------

    def on_check_info(self) -> None:
        url = self._get_url()
        if url is None:
            return
        if not core.ytdlp_installed():
            messagebox.showwarning(
                "Prasyarat belum ada",
                "yt-dlp belum terinstall di komputer ini.\n"
                "Klik '🧰 Install Prasyarat' dulu, lalu coba lagi.",
            )
            return
        if core.is_watch_with_playlist(url):
            self.q.put(("show_scope",))
        else:
            self.q.put(("hide_scope",))
        self._start_worker("Mengambil info...", lambda: self._worker_info(url))

    def on_start_download(self) -> None:
        url = self._get_url()
        if url is None:
            return
        if not core.ytdlp_installed():
            if messagebox.askyesno(
                "Prasyarat belum ada",
                "yt-dlp belum terinstall di komputer ini.\nInstall sekarang?",
            ):
                self._start_worker("Menginstall prasyarat...", self._worker_setup)
            return

        scope = self._resolve_scope(url)
        if core.is_watch_with_playlist(url):
            self.q.put(("show_scope",))
        else:
            self.q.put(("hide_scope",))
        fmt = self._picked_format()

        if not (core.ffmpeg_path() and core.ffprobe_path()):
            self.q.put((
                "log",
                "⚠️ ffmpeg/ffprobe tidak terdeteksi lengkap — mode ini kemungkinan "
                "MEMBUTUHKAN ffmpeg (merge video+audio / ekstrak MP3). "
                "Gunakan tombol 'Install Prasyarat' bila unduhan gagal.",
            ))

        if scope == "playlist":
            self._start_worker("Mengambil daftar playlist...", lambda: self._worker_playlist(url, fmt))
        else:
            self._start_worker("Menyiapkan unduhan...", lambda: self._worker_single(url, fmt))

    def on_cancel(self) -> None:
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")
        self.set_status("⏳ Membatalkan setelah potongan file selesai...")

    def on_setup(self) -> None:
        self._start_worker("Menginstall prasyarat...", self._worker_setup)

    def on_update_ytdlp(self) -> None:
        self._start_worker("Memperbarui yt-dlp...", self._worker_update)

    def on_open_downloads(self) -> None:
        core.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(core.DOWNLOADS_DIR))  # noqa: S606 - Windows Explorer

    def on_clear_screen(self) -> None:
        """Bersihkan tampilan (URL, info, log, progres) — file di disk TIDAK disentuh."""
        self.url_entry.delete(0, "end")
        self.info_label.configure(
            text="📎 Info akan tampil di sini setelah URL dicek (opsional sebelum unduh)."
        )
        self.scope_seg.grid_forget()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.set_status("Siap.")
        self.bar_total.set(0)
        self.bar_file.set(0)
        self.lbl_total.configure(text="—")
        self.lbl_file.configure(text="—")
        self.log("🧹 Tampilan dibersihkan — file log/riwayat/unduhan di disk tidak disentuh.")

    # ------------------------------------------------------------------
    # Kolom URL: menu klik-kanan & clipboard
    # ------------------------------------------------------------------

    def _build_url_menu(self) -> None:
        """CTkEntry tidak punya menu klik-kanan bawaan — pasang sendiri."""
        self._url_menu = Menu(self, tearoff=0)
        self._url_menu.add_command(label="Paste", command=self._paste_to_url)
        self._url_menu.add_command(label="Copy", command=self._copy_from_url)
        self._url_menu.add_command(label="Pilih Semua", command=self._select_all_url)
        self.url_entry.bind("<Button-3>", self._show_url_menu)
        self.url_entry.bind("<Control-a>", self._on_ctrl_a)

    def _show_url_menu(self, event) -> None:
        try:
            self._url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._url_menu.grab_release()
        return "break"

    def _on_ctrl_a(self, _event=None) -> str:
        self._select_all_url()
        return "break"  # cegah perilaku bawaan Entry

    def _select_all_url(self) -> None:
        self.url_entry.focus()
        try:
            self.url_entry.select_range(0, "end")
            self.url_entry.icursor("end")
        except AttributeError:  # versi CTkEntry lama: lewat entry internal
            self.url_entry._entry.select_range(0, "end")
            self.url_entry._entry.icursor("end")

    def _paste_to_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except Exception:  # noqa: BLE001 - clipboard kosong/bukan teks
            return
        if not text:
            return
        try:
            self.url_entry.delete("sel.first", "sel.last")  # timpa teks terseleksi
        except Exception:  # noqa: BLE001 - tidak ada seleksi
            pass
        self.url_entry.insert("insert", text)

    def _copy_from_url(self) -> None:
        try:
            text = self.url_entry.selection_get()
        except Exception:  # noqa: BLE001 - tidak ada teks terseleksi
            text = ""
        if not text.strip():
            text = self.url_entry.get()
        text = text.strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def on_uninstall(self) -> None:
        installed = core.load_manifest().get("installed", {})
        if not installed:
            messagebox.showinfo(
                "Uninstall",
                "Tidak ada prasyarat yang diinstall oleh aplikasi ini.\n"
                "Tidak ada yang perlu di-uninstall.",
            )
            return
        listing = "\n".join(f"• {name} ({kind})" for name, kind in installed.items())
        if not messagebox.askyesno(
            "Uninstall prasyarat",
            "Hapus komponen berikut?\n\n" + listing +
            "\n\nCatatan: folder downloads/ & log/ TIDAK disentuh.",
        ):
            return
        self._start_worker("Meng-uninstall prasyarat...", self._worker_uninstall)

    def on_retry_selected(self) -> None:
        picked = [r["entry"] for r in self._failed_rows if r["var"].get()]
        if not picked:
            messagebox.showinfo("Unduh ulang", "Centang dulu video yang mau diunduh ulang.")
            return
        self._start_worker(
            f"Unduh ulang {len(picked)} video...",
            lambda: self._worker_retry(picked),
        )

    def on_retry_all(self) -> None:
        for r in self._failed_rows:
            r["var"].set(True)
        self._update_fail_buttons_state()
        self.on_retry_selected()

    def on_clear_history(self) -> None:
        failed = core.load_failed()
        if not failed:
            messagebox.showinfo("Bersihkan riwayat", "Riwayat gagal sudah kosong.")
            return
        if not messagebox.askyesno(
            "Bersihkan riwayat",
            f"Hapus {len(failed)} riwayat video gagal?\n\n"
            "Hasil unduhan di downloads/ TIDAK disentuh.",
        ):
            return
        self._start_worker("Membersihkan riwayat...", self._worker_clear_history)

    # ------------------------------------------------------------------
    # Worker implementations (thread)
    # ------------------------------------------------------------------

    def _worker_info(self, url: str) -> None:
        try:
            if core.detect_url_kind(url) == "playlist" and not core.is_watch_with_playlist(url):
                title, entries = core.fetch_playlist_info(url)
                self._info_cache = {"url": url, "kind": "playlist"}
                self.q.put(("info_playlist", title, len(entries)))
            else:
                info = core.fetch_video_info(url)
                self._info_cache = {"url": url, "kind": "video", "info": info}
                self.q.put(("info_video", info.get("uploader", ""),
                            info.get("duration") or 0, info["title"]))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", f"✖ Gagal membaca info: {exc}"))
            self.q.put(("summary",
                        "Gagal membaca info dari URL tersebut.\n"
                        "Periksa URL / koneksi internet, lalu coba lagi.", True))

    def _worker_single(self, url: str, fmt: str) -> None:
        # Pakai cache Cek Info bila URL sama (hemat 1x fetch metadata)
        info = None
        if self._info_cache and self._info_cache.get("url") == url \
                and self._info_cache.get("kind") == "video":
            info = self._info_cache.get("info")
        if info is None:
            try:
                info = core.fetch_video_info(url)
            except Exception as exc:  # noqa: BLE001
                self.q.put(("log", f"✖ Gagal membaca info video: {exc}"))
                self.q.put(("summary",
                            "Gagal membaca info video.\n"
                            "Periksa URL / koneksi internet, lalu coba lagi.", True))
                return
        title = info["title"]
        self.q.put(("info_video", info.get("uploader", ""),
                    info.get("duration") or 0, title))

        video_url = (
            f"https://www.youtube.com/watch?v={info['id']}" if info.get("id") else url
        )
        label = "audio MP3" if fmt == core.AUDIO_FMT else "video"
        self.q.put(("status", f"⏳ Mengunduh {label}: {title}"))
        core.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        start = time.time()
        ok = core.download_video(video_url, core.DOWNLOADS_DIR, None, fmt, self._progress_cb())
        elapsed = time.time() - start

        core.log_line(f"SINGLE | {'OK' if ok else 'GAGAL'} | {title} | {url}")
        failed_store = core.load_failed()
        if self.cancel_event.is_set():
            self.q.put(("log", "🚫 Unduhan dibatalkan user."))
            self.q.put(("summary", "Unduhan dibatalkan.", False))
            return
        if ok:
            core.remove_failed_entry(failed_store, url)
            core.remove_failed_entry(failed_store, video_url)
            core.save_failed(failed_store)
            self.q.put(("summary",
                        f"✅ Selesai dalam {core.format_duration(elapsed)}\n"
                        f"📁 Hasil tersimpan di: {core.DOWNLOADS_DIR}", False))
        else:
            core.add_failed_entry(failed_store, "", 0, title, url, fmt, kind="single")
            core.save_failed(failed_store)
            self.q.put(("log", "❌ Video gagal diunduh — dicatat ke Riwayat Gagal."))
            self.q.put(("summary",
                        "❌ Video gagal diunduh.\n"
                        "Coba lagi lewat tab '⚠ Riwayat Gagal' → Unduh ulang.", True))
        self.q.put(("failed_refresh",))

    def _worker_playlist(self, url: str, fmt: str) -> None:
        try:
            playlist_title, entries = core.fetch_playlist_info(url)
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", f"✖ Gagal mengambil daftar playlist: {exc}"))
            self.q.put(("summary",
                        "Gagal mengambil daftar playlist.\n"
                        "Periksa URL / koneksi internet, lalu coba lagi.", True))
            return
        if not entries:
            self.q.put(("summary", "Playlist kosong atau tidak bisa dibaca.", True))
            return
        self.q.put(("info_playlist", playlist_title, len(entries)))

        out_dir = core.DOWNLOADS_DIR / core.safe_name(playlist_title)
        out_dir.mkdir(parents=True, exist_ok=True)
        total = len(entries)
        self.q.put(("status", f"⏳ Mengunduh {total} video secara serial..."))

        start = time.time()
        ok_count = fail_count = 0
        failed_store = core.load_failed()
        for i, entry in enumerate(entries, start=1):
            if self.cancel_event.is_set():
                break
            video_id = entry.get("id")
            if not video_id:
                fail_count += 1
                core.log_line(f"{playlist_title} | {i}/{total} | GAGAL | (no id)")
                continue
            title = entry.get("title") or f"video-{video_id}"
            self.q.put(("video_start", i, total, title))
            self.q.put(("file_reset",))
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            ok = core.download_video(video_url, out_dir, i, fmt, self._progress_cb())
            self.q.put(("video_done", i, total, title, ok))
            if ok:
                ok_count += 1
                core.remove_failed_entry(failed_store, video_url)
            elif self.cancel_event.is_set():
                break
            else:
                fail_count += 1
                core.add_failed_entry(failed_store, playlist_title, i, title, video_url, fmt)
            core.log_line(
                f"{playlist_title} | {i}/{total} | {'OK' if ok else 'GAGAL'} | {title} | {video_url}"
            )
            self.q.put(("overall", (i) / total))
        core.save_failed(failed_store)

        if self.cancel_event.is_set():
            self.q.put((
                "summary",
                f"🚫 Dibatalkan. Sementara itu {ok_count} video sudah terunduh.",
                False,
            ))
        else:
            self.q.put((
                "summary",
                f"✅ Selesai: {ok_count} berhasil · {fail_count} gagal · "
                f"total {core.format_duration(time.time() - start)}\n"
                f"📁 Hasil tersimpan di: {out_dir}",
                fail_count > 0,
            ))
        self.q.put(("failed_refresh",))

    def _worker_retry(self, entries: list[dict]) -> None:
        self.q.put((
            "status",
            f"⏳ Mengunduh ulang {len(entries)} video (maks {core.MAX_RETRIES}x/video)...",
        ))
        start = time.time()
        ok_count = fail_count = 0
        success_urls: set[str] = set()
        failed_list = core.load_failed()

        for n, entry in enumerate(entries, start=1):
            if self.cancel_event.is_set():
                break
            title = entry.get("title") or "?"
            url = entry.get("url") or ""
            fmt = entry.get("fmt") or core.VIDEO_RESOLUTIONS[core.DEFAULT_RESOLUTION][1]
            kind = entry.get("kind") or "playlist"
            playlist_label = entry.get("playlist") or ""
            if kind == "single" or not playlist_label:
                out_dir = core.DOWNLOADS_DIR
                dl_index: int | None = None
            else:
                out_dir = core.DOWNLOADS_DIR / core.safe_name(playlist_label)
                dl_index = entry.get("index") or 0
            out_dir.mkdir(parents=True, exist_ok=True)
            self.q.put(("video_start", n, len(entries), title))
            self.q.put(("file_reset",))

            ok = False
            for attempt in range(1, core.MAX_RETRIES + 1):
                if self.cancel_event.is_set():
                    break
                self.q.put(("status", f"⏳ [{n}/{len(entries)}] {title} — percobaan {attempt}/{core.MAX_RETRIES}"))
                if core.download_video(url, out_dir, dl_index, fmt, self._progress_cb()):
                    ok = True
                    break
            self.q.put(("video_done", n, len(entries), title, ok))
            if ok:
                ok_count += 1
                success_urls.add(url)
                core.log_line(f"UNDUH-ULANG | OK | {title} | {url}")
            elif self.cancel_event.is_set():
                break
            else:
                fail_count += 1
                core.log_line(f"UNDUH-ULANG | GAGAL ({core.MAX_RETRIES}x) | {title} | {url}")
            self.q.put(("overall", n / len(entries)))

        failed_list[:] = [e for e in failed_list if e.get("url") not in success_urls]
        core.save_failed(failed_list)

        if self.cancel_event.is_set():
            self.q.put(("summary", f"🚫 Dibatalkan. {ok_count} video sudah terunduh ulang.", False))
        else:
            self.q.put((
                "summary",
                f"✅ Selesai: {ok_count} berhasil diunduh ulang · {fail_count} tetap gagal · "
                f"{core.format_duration(time.time() - start)}",
                fail_count > 0,
            ))
        self.q.put(("failed_refresh",))

    def _worker_clear_history(self) -> None:
        count = len(core.load_failed())
        try:
            core.FAILED_FILE.unlink(missing_ok=True)
        except OSError as exc:
            self.q.put(("summary", f"Gagal menghapus riwayat: {exc}", True))
            return
        core.log_line(f"BERSIH-RIWAYAT | {count} entri riwayat gagal dihapus")
        self.q.put(("log", f"🗑 {count} riwayat gagal dihapus (downloads/ aman)."))
        self.q.put(("summary", f"✅ {count} riwayat gagal sudah dihapus.", False))

    def _worker_setup(self) -> None:
        with contextlib.redirect_stdout(QueueWriter(self.q)):
            core.setup_prereqs()
        self.q.put(("status_refresh",))

    def _worker_update(self) -> None:
        if core.ytdlp_version() is None:
            self.q.put(("summary",
                        "yt-dlp belum terinstall.\nGunakan tombol 'Install Prasyarat' dulu.", True))
            return
        with contextlib.redirect_stdout(QueueWriter(self.q)):
            core.update_ytdlp()
        self.q.put(("status_refresh",))

    def _worker_uninstall(self) -> None:
        manifest = core.load_manifest()
        installed = manifest.get("installed", {})
        if "yt-dlp" in installed:
            self.q.put(("log", "⏳ Menghapus yt-dlp (pip uninstall)..."))
            try:
                core.run_pip(["uninstall", "-y", "yt-dlp"])
            except OSError as exc:
                self.q.put(("log", f"✖ Gagal uninstall yt-dlp: {exc}"))
        if "ffmpeg" in installed:
            self.q.put(("log", "⏳ Menghapus ffmpeg portable (tools/ffmpeg/)..."))
            shutil.rmtree(core.FFMPEG_DIR, ignore_errors=True)
            if core.TOOLS_DIR.exists() and not any(core.TOOLS_DIR.iterdir()):
                core.TOOLS_DIR.rmdir()
        core.MANIFEST_FILE.unlink(missing_ok=True)
        core.log_line("UNINSTALL | prasyarat diinstall script sudah dihapus")
        self.q.put(("summary",
                    "✅ Prasyarat yang diinstall aplikasi sudah dihapus.\n"
                    "Folder downloads/ & log/ TETAP ADA.", False))
        self.q.put(("status_refresh",))

    # ------------------------------------------------------------------
    # Refresh data tampilan
    # ------------------------------------------------------------------

    def refresh_status(self) -> None:
        version = core.ytdlp_version()
        if version:
            self.chip_ytdlp.configure(
                text=f"yt-dlp {version} ✔", text_color="#4ade80"
            )
        else:
            self.chip_ytdlp.configure(
                text="yt-dlp: belum ada ✖", text_color="#f87171"
            )
        if core.ffmpeg_path() and core.ffprobe_path():
            self.chip_ffmpeg.configure(text="ffmpeg ✔", text_color="#4ade80")
        else:
            self.chip_ffmpeg.configure(text="ffmpeg ✖", text_color="#fbbf24")

    def refresh_failed_list(self) -> None:
        for w in self.fail_scroll.winfo_children():
            w.destroy()
        self._failed_rows.clear()
        failed = core.load_failed()
        if not failed:
            ctk.CTkLabel(
                self.fail_scroll,
                text="✨ Tidak ada video gagal.\nVideo yang gagal diunduh akan tercatat di sini "
                     "dan bisa diunduh ulang.",
                justify="center", text_color=("#6b7280", "#9ca3af"),
            ).pack(pady=24)
        for n, entry in enumerate(failed, start=1):
            kind = entry.get("kind") or "playlist"
            source = (
                "(single)" if kind == "single" or not entry.get("playlist")
                else entry.get("playlist", "")
            )
            row = ctk.CTkFrame(self.fail_scroll, corner_radius=10)
            row.pack(fill="x", padx=4, pady=4)
            var = ctk.BooleanVar(value=False)
            cb = ctk.CTkCheckBox(row, text="", width=28, variable=var,
                                 command=self._update_fail_buttons_state)
            cb.pack(side="left", padx=(10, 4), pady=10)
            texts = ctk.CTkFrame(row, fg_color="transparent")
            texts.pack(side="left", fill="x", expand=True, pady=8)
            ctk.CTkLabel(
                texts, text=f"{n}. {entry.get('title', '?')}",
                font=("Segoe UI", 12, "bold"), anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                texts, text=f"{source} · {entry.get('url', '?')}",
                font=("Segoe UI", 11), anchor="w",
                text_color=("#6b7280", "#9ca3af"),
            ).pack(anchor="w")
            self._failed_rows.append({"var": var, "entry": entry})
        self._update_fail_buttons_state()


    # ------------------------------------------------------------------
    # Queue unduhan manual
    # ------------------------------------------------------------------

    def _fmt_label(self, fmt: str) -> str:
        """Label ringkas utk item queue dari kode format yt-dlp."""
        if fmt == core.AUDIO_FMT:
            return "🎵 Audio (MP3)"
        for label, f in self._res_fmt.items():
            if f == fmt:
                return f"🎬 Video ({label})"
        return f"🎬 Video ({fmt})"

    def on_add_to_queue(self) -> None:
        """Tambahkan URL + format terpilih ke queue (tanpa akses network)."""
        url = self._get_url()
        if url is None:
            return
        if core.is_watch_with_playlist(url):
            self.q.put(("show_scope",))
        else:
            self.q.put(("hide_scope",))
        if any(it["url"] == url for it in self._queue_items):
            messagebox.showwarning(
                "Tambah ke Queue",
                "URL ini sudah ada di queue.\n"
                "Hapus dulu entri lamanya bila mau mengganti format.",
            )
            return
        scope = self._resolve_scope(url)
        fmt = self._picked_format()
        label = self._fmt_label(fmt)
        if scope == "playlist":
            label += " · 📂 seluruh playlist"
        self._queue_items.append({"url": url, "fmt": fmt, "scope": scope, "label": label})
        self.url_entry.delete(0, "end")
        self.refresh_queue_list()
        self.log(f"➕ Masuk queue ({len(self._queue_items)} item): {url}")

    def on_remove_from_queue(self, url: str) -> None:
        self._queue_items = [it for it in self._queue_items if it["url"] != url]
        self.refresh_queue_list()

    def on_clear_queue(self) -> None:
        if not self._queue_items:
            messagebox.showinfo("Kosongkan Queue", "Queue sudah kosong.")
            return
        note = (
            "\n\nCatatan: item yang sedang diproses tetap dijalankan sampai selesai."
            if self.busy else ""
        )
        if not messagebox.askyesno(
            "Kosongkan Queue",
            f"Hapus {len(self._queue_items)} URL dari queue?{note}",
        ):
            return
        self._queue_items.clear()
        self.refresh_queue_list()
        self.log("🗑 Queue dikosongkan.")

    def on_run_queue(self) -> None:
        if not self._queue_items:
            messagebox.showinfo(
                "Jalankan Queue",
                "Queue masih kosong.\n"
                "Tambahkan URL dulu lewat tombol '➕ Tambah ke Queue'.",
            )
            return
        if not core.ytdlp_installed():
            if messagebox.askyesno(
                "Prasyarat belum ada",
                "yt-dlp belum terinstall di komputer ini.\nInstall sekarang?",
            ):
                self._start_worker("Menginstall prasyarat...", self._worker_setup)
            return
        if not (core.ffmpeg_path() and core.ffprobe_path()):
            self.q.put((
                "log",
                "⚠️ ffmpeg/ffprobe tidak terdeteksi lengkap — item queue kemungkinan "
                "MEMBUTUHKAN ffmpeg (merge video+audio / ekstrak MP3). "
                "Gunakan tombol 'Install Prasyarat' bila unduhan gagal.",
            ))
        base_delay = float(self.queue_delay_menu.get())
        self._start_worker("Menjalankan queue...", lambda: self._worker_queue(base_delay))

    def refresh_queue_list(self) -> None:
        for w in self.queue_scroll.winfo_children():
            w.destroy()
        if not self._queue_items:
            ctk.CTkLabel(
                self.queue_scroll,
                text="📋 Queue kosong.\n\n"
                     "Tempel URL lalu klik '➕ Tambah ke Queue' — ulangi untuk URL lain "
                     "(format/resolusi boleh berbeda per item),\n"
                     "lalu klik '▶ Jalankan Queue' untuk mengunduh semuanya sekaligus.",
                justify="center", text_color=("#6b7280", "#9ca3af"),
            ).pack(pady=24)
        for n, item in enumerate(self._queue_items, start=1):
            row = ctk.CTkFrame(self.queue_scroll, corner_radius=10)
            row.pack(fill="x", padx=4, pady=4)
            ctk.CTkLabel(
                row, text=f"{n}.", font=("Segoe UI", 12, "bold")
            ).pack(side="left", padx=(10, 2))
            texts = ctk.CTkFrame(row, fg_color="transparent")
            texts.pack(side="left", fill="x", expand=True, pady=8)
            ctk.CTkLabel(
                texts, text=item["url"], font=("Segoe UI", 12, "bold"), anchor="w",
            ).pack(anchor="w")
            ctk.CTkLabel(
                texts, text=item["label"], font=("Segoe UI", 11), anchor="w",
                text_color=("#6b7280", "#9ca3af"),
            ).pack(anchor="w")
            ctk.CTkButton(
                row, text="✖", width=36, height=30,
                fg_color=("#9ca3af", "#4b5563"), hover_color=("#6b7280", "#6b7280"),
                command=lambda u=item["url"]: self.on_remove_from_queue(u),
            ).pack(side="right", padx=10)
        self._update_queue_buttons_state()

    def _update_queue_buttons_state(self) -> None:
        if self.busy:
            return
        has = bool(self._queue_items)
        self.btn_queue_run.configure(
            text=f"▶ Jalankan Queue ({len(self._queue_items)})",
            state="normal" if has else "disabled",
        )
        self.btn_queue_clear.configure(state="normal" if has else "disabled")

    def _anti_bot_wait(self, base_delay: float) -> bool:
        """Jeda acak antar video queue: 1-2x nilai dasar, minimal QUEUE_DELAY_MIN_S.

        Tidur dipecah per 0.25 dtk supaya tombol Batalkan tetap responsif.
        False = user membatalkan saat jeda.
        """
        delay = max(random.uniform(base_delay, base_delay * 2), QUEUE_DELAY_MIN_S)
        end = time.time() + delay
        while True:
            if self.cancel_event.is_set():
                return False
            remain = end - time.time()
            if remain <= 0:
                return True
            self.q.put(("status", f"⏸ Jeda anti-bot: menunggu {remain:.0f} dtk..."))
            time.sleep(min(0.25, remain))

    def _worker_queue(self, base_delay: float) -> None:
        # Snapshot; URL yang ditambahkan saat queue berjalan masuk run berikutnya.
        items = list(self._queue_items)
        total = len(items)
        self.q.put(("status", f"⏳ Menjalankan queue ({total} item)..."))
        start = time.time()
        ok_items = fail_items = 0   # per item queue
        vid_ok = vid_fail = 0       # per video (entri playlist ikut terhitung)
        download_no = 0             # jeda anti-bot: video PERTAMA langsung jalan
        failed_store = core.load_failed()

        for n, item in enumerate(items, start=1):
            if self.cancel_event.is_set():
                break
            url, fmt, scope = item["url"], item["fmt"], item["scope"]
            self.q.put(("item_start", n, total, url))
            self.q.put(("file_reset",))

            if scope == "playlist":
                try:
                    playlist_title, entries = core.fetch_playlist_info(url)
                except Exception as exc:  # noqa: BLE001
                    self.q.put(("log", f"✖ [Queue {n}/{total}] Gagal mengambil playlist: {exc}"))
                    core.add_failed_entry(failed_store, "", 0, url, url, fmt, kind="single")
                    core.log_line(f"QUEUE | GAGAL | (playlist) | {url}")
                    vid_fail += 1
                    fail_items += 1
                    self.q.put(("queue_advance", url))
                    continue
                out_dir = core.DOWNLOADS_DIR / core.safe_name(playlist_title)
                out_dir.mkdir(parents=True, exist_ok=True)
                self.q.put(("info_playlist", playlist_title, len(entries)))
                item_ok = True
                for i, entry in enumerate(entries, start=1):
                    if self.cancel_event.is_set():
                        break
                    video_id = entry.get("id")
                    if not video_id:
                        item_ok = False
                        vid_fail += 1
                        continue
                    title = entry.get("title") or f"video-{video_id}"
                    self.q.put(("status",
                                f"⏳ [Queue {n}/{total}] ({i}/{len(entries)}) {title}"))
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                    download_no += 1
                    if download_no > 1 and not self._anti_bot_wait(base_delay):
                        break
                    ok = core.download_video(video_url, out_dir, i, fmt, self._progress_cb())
                    self.q.put((
                        "log",
                        f"{'✅' if ok else '❌'} [Queue {n}/{total}] "
                        f"({i}/{len(entries)}) {title}",
                    ))
                    if ok:
                        vid_ok += 1
                        core.remove_failed_entry(failed_store, video_url)
                    else:
                        item_ok = False
                        vid_fail += 1
                        if self.cancel_event.is_set():
                            break
                        core.add_failed_entry(failed_store, playlist_title, i, title, video_url, fmt)
                    core.log_line(
                        f"QUEUE | {playlist_title} | {i}/{len(entries)} | "
                        f"{'OK' if ok else 'GAGAL'} | {title} | {video_url}"
                    )
                    self.q.put(("overall", (n - 1 + i / max(len(entries), 1)) / total))
                if self.cancel_event.is_set():
                    item_ok = False
            else:
                info = None
                if (self._info_cache and self._info_cache.get("url") == url
                        and self._info_cache.get("kind") == "video"):
                    info = self._info_cache.get("info")
                if info is None:
                    try:
                        info = core.fetch_video_info(url)
                    except Exception as exc:  # noqa: BLE001
                        self.q.put(("log", f"✖ [Queue {n}/{total}] Gagal membaca info video: {exc}"))
                if info is None:
                    core.add_failed_entry(failed_store, "", 0, url, url, fmt, kind="single")
                    core.log_line(f"QUEUE | SINGLE | GAGAL | (info gagal) | {url}")
                    vid_fail += 1
                    fail_items += 1
                    self.q.put(("queue_advance", url))
                    continue
                title = info.get("title") or url
                video_url = (
                    f"https://www.youtube.com/watch?v={info['id']}" if info.get("id") else url
                )
                label = "audio MP3" if fmt == core.AUDIO_FMT else "video"
                self.q.put(("status", f"⏳ [Queue {n}/{total}] Mengunduh {label}: {title}"))
                core.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
                download_no += 1
                if download_no > 1 and not self._anti_bot_wait(base_delay):
                    ok = False
                else:
                    ok = core.download_video(video_url, core.DOWNLOADS_DIR, None, fmt, self._progress_cb())
                core.log_line(f"QUEUE | SINGLE | {'OK' if ok else 'GAGAL'} | {title} | {url}")
                if ok:
                    vid_ok += 1
                    core.remove_failed_entry(failed_store, url)
                    core.remove_failed_entry(failed_store, video_url)
                else:
                    vid_fail += 1
                    if not self.cancel_event.is_set():
                        core.add_failed_entry(failed_store, "", 0, title, url, fmt, kind="single")
                item_ok = ok

            if item_ok:
                ok_items += 1
            else:
                fail_items += 1
            self.q.put(("overall", n / total))
            self.q.put(("queue_advance", url))

        core.save_failed(failed_store)
        skipped = total - (ok_items + fail_items)
        if self.cancel_event.is_set():
            text = (f"🚫 Queue dibatalkan. {ok_items} item selesai "
                    f"({vid_ok} video berhasil).")
            if skipped:
                text += f" {skipped} item belum diproses."
        else:
            text = (f"✅ Queue selesai: {ok_items} item berhasil · {fail_items} gagal · "
                    f"{vid_ok} video terunduh · {core.format_duration(time.time() - start)}")
        self.q.put(("summary", text, fail_items > 0))
        self.q.put(("failed_refresh",))


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Dibatalkan user. Sampai jumpa! 👋")
