@echo off
setlocal
cd /d "%~dp0server"
where py >nul 2>nul
if errorlevel 1 (
  echo Khong thay Python launcher "py". Cai Python 3.11+ tu python.org va tick "Add python.exe to PATH".
  pause
  exit /b 1
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Can Python 3.11 tro len.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Cai thu vien that bai. Kiem tra ket noi Internet.
  pause
  exit /b 1
)
echo.
echo Xong. Chay start-server.bat de khoi dong server.
pause
