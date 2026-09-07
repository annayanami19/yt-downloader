@echo off
setlocal EnableExtensions
rem ============================================================
rem  YT DOWNLOAD - Launcher GUI (klik ganda untuk membuka aplikasi)
rem  - Deteksi Python: py -3 dulu, fallback python
rem  - Auto-install customtkinter (tampilan GUI) sekali saja
rem  - Jalankan GUI tanpa jendela konsol (pythonw bila tersedia)
rem ============================================================
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [X] Python 3 tidak ditemukan di komputer ini.
  echo     Install dari https://www.python.org/downloads/
  echo     Saat install, centang "Add Python to PATH".
  pause
  exit /b 1
)

rem --- Pastikan customtkinter tersedia untuk GUI (sekali saja) ---
%PY% -c "import customtkinter" >nul 2>&1
if errorlevel 1 (
  echo Menyiapkan tampilan GUI sekali saja: menginstall customtkinter...
  %PY% -m pip install -U customtkinter
)

rem --- Cari pythonw.exe (GUI tanpa jendela konsol) ---
set "PYEXE="
for /f "delims=" %%i in ('%PY% -c "import sys; print(sys.executable)"') do set "PYEXE=%%i"
set "PYDIR=%PYEXE%\.."
if exist "%PYDIR%\pythonw.exe" (
  start "" "%PYDIR%\pythonw.exe" "%~dp0yt_gui.py"
) else (
  %PY% "%~dp0yt_gui.py"
)
endlocal
