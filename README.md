# YT DOWNLOAD — Kit Arsip v2.1

> Kit bersih **YT DOWNLOAD**: alat unduh video tunggal / playlist YouTube
> sebagai video MP4 atau audio MP3 — base Python, launcher Windows.
> Versi: **2.1** (2026-08-24) · Diperbarui: **2026-09-06** (mode GUI +
> Queue + jeda anti-bot) · Status: implementasi selesai & teruji.

---

## 1. Isi kit

| File | Fungsi |
|---|---|
| `yt_download.py` | Script inti — seluruh logika unduh/prasyarat + mode menu CLI |
| `yt_gui.py` | **Mode GUI** (CustomTkinter) — klik-kanan URL, Queue, jeda anti-bot |
| `YT-DOWNLOAD.bat` | Launcher CLI Windows — double-click untuk mulai |
| `YT-DOWNLOAD-GUI.bat` | Launcher GUI Windows — double-click, tanpa konsol |
| `requirements.txt` | Dependensi pip (`yt-dlp` + `customtkinter`) |

Folder ini **self-contained**: tidak perlu file lain dari luar. Artefak runtime
(`downloads/`, `log/`, `tools/`, `prereq-state.json`, `failed-videos.json`)
akan dibuat otomatis oleh script saat dipakai.

## 2. Prasyarat

- **Python 3.9+** (Windows: centang *"Add python.exe to PATH"* saat install).
- Sisanya di-install otomatis dari dalam aplikasi (menu `1`):
  `yt-dlp` via pip + `ffmpeg`/`ffprobe` portable untuk Windows.

## 3. Mulai cepat

1. Double-click **`YT-DOWNLOAD-GUI.bat`** (mode grafis, disarankan) atau
   **`YT-DOWNLOAD.bat`** (mode menu teks).
2. Klik `🧰 Install Prasyarat` sekali saja (GUI) / pilih `1` (CLI).
3. Tempel URL → pilih format (Video/MP3) → `⬇ Mulai Unduh`; atau kumpulkan
   banyak URL di tab `📋 Queue` → `▶ Jalankan Queue`.

### Mode GUI — fitur utama

- Klik-kanan kolom URL: **Paste / Copy / Pilih Semua** (+ Ctrl+A).
- **📋 Queue**: kumpulkan banyak URL (format/resolusi boleh beda per item),
  unduh semuanya dalam SATU klik; gagalan masuk Riwayat Gagal otomatis.
- **Jeda anti-bot antar video** (khusus queue): acak 1–2× nilai dasar yang
  dipilih (5–60 dtk). Minimal 5 dtk & mekanisme acak terkunci — mengikuti
  rekomendasi wiki yt-dlp untuk sesi tamu (5–10 dtk antar video; limit
  guest ±300 video/jam, di bawah itu rawan CAPTCHA).
- `🧹 Bersihkan Tampilan`: kosongkan URL/log/progres sekali klik — file di
  disk tidak disentuh.

### Pintasan CLI (opsional)

```text
YT-DOWNLOAD.bat                     :: buka menu interaktif
YT-DOWNLOAD.bat <URL>               :: langsung unduh video/playlist
YT-DOWNLOAD.bat --setup             :: install prasyarat (= menu 1)
YT-DOWNLOAD.bat --uninstall         :: hapus prasyarat yang diinstall script
python yt_download.py <URL>         :: tanpa launcher, langsung via Python
```

URL yang diterima: `watch?v=…`, `youtu.be/…`, `/shorts/…`, `/live/…`,
dan `playlist?list=…`. URL watch yang membawa `list=` akan ditanya:
hanya video ini, atau seluruh playlist.

## 4. Menu utama

```text
1. Install prasyarat        — pasang yt-dlp + ffmpeg portable yang belum ada
2. Mulai unduh              — unduh baru / unduh ulang yang gagal
3. Update yt-dlp            — perbarui yt-dlp via pip  ⭐ penting!
4. Bersihkan riwayat gagal  — hapus failed-videos.json (downloads/ aman)
5. Uninstall                — hapus prasyarat yang diinstall script
0. Keluar
```

Di semua prompt, ketik **`k`** atau **`0`** untuk kembali/batal.

## 5. Hasil unduhan

- Playlist → `downloads/<judul-playlist>/01_<judul>.mp4` (serial, satu per satu).
- Video tunggal → `downloads/<judul>.mp4` (tanpa prefix nomor).
- MP3 → `<judul>.mp3` 192 kbps.
- Log harian → `log/download.log`; kegagalan tercatat di `failed-videos.json`
  dan bisa diunduh ulang lewat menu `2` (maks 3 percobaan/video).

## 6. Troubleshooting

| Gejala | Solusi |
|---|---|
| Unduhan gagal **HTTP 403** di tengah jalan (biasanya semua video ikut gagal) | YouTube mengubah mekanismenya & yt-dlp-mu kedaluwarsa → jalankan **menu `3` (Update yt-dlp)** |
| "Python tidak ditemukan" saat buka .bat | Install Python 3.9+, centang *Add to PATH*, lalu buka ulang |
| Merge/MP3 error, minta ffmpeg | Jalankan menu `1` (ffmpeg portable Windows diunduh otomatis) |
| Ada sisa `.part` / daftar gagal menumpuk | File `.part` boleh dihapus manual; riwayat gagal dibersihkan lewat menu `4` |

> Pengalaman terdokumentasi: insiden 403 massal
> terjadi karena yt-dlp `2026.7.4` kedaluwarsa dan tuntas hanya dengan update ke
> `2026.8.19`. Kalau mendadak semua unduhan gagal, menu 3 adalah jawaban pertama.

## 7. Catatan arsip

- Versi kit: **v2.1** (tambahan menu Update yt-dlp & Bersihkan riwayat gagal).
- **Pembaruan 2026-09-06:** sinkron `yt_download.py` terbaru + tambah mode GUI
  (`yt_gui.py`, `YT-DOWNLOAD-GUI.bat`) dengan Queue + jeda anti-bot.
- Untuk versi berikutnya, buat folder baru (mis. `KIT-ARSIP-v2.2/`) — jangan
  menimpa arsip lama agar jejak versinya rapi.
