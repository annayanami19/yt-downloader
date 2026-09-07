@echo off
rem ============================================================
rem  YT DOWNLOAD - launcher untuk yt_download.py (Windows)
rem  Base tetap Python; file ini hanya pembungkus praktis:
rem    YT-DOWNLOAD.bat              -> buka menu interaktif
rem    YT-DOWNLOAD.bat <URL>        -> langsung unduh video/playlist
rem    YT-DOWNLOAD.bat --setup      -> install prasyarat
rem    YT-DOWNLOAD.bat --uninstall  -> hapus prasyarat
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

where py >nul 2>nul
if %errorlevel%==0 goto :runpy

where python >nul 2>nul
if %errorlevel%==0 goto :runpython

echo.
echo   [X] Python tidak ditemukan di sistem ini.
echo       Install Python 3.9+ dari https://www.python.org/downloads/
echo       dan centang opsi "Add python.exe to PATH" saat instalasi,
echo       lalu jalankan lagi YT-DOWNLOAD.bat
goto :end

:runpy
py -3 yt_download.py %*
goto :end

:runpython
python yt_download.py %*

:end
echo.
pause
endlocal
