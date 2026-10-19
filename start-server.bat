@echo off
setlocal
cd /d "%~dp0server"
if not exist ".venv\Scripts\python.exe" (
  echo Chua cai dat. Hay chay install.bat truoc.
  pause
  exit /b 1
)
set PYTHONUTF8=1
echo Viet Dub server: http://127.0.0.1:8765/health  (dong cua so nay de dung server)
".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8765
echo.
echo Server da dung. Xem thong bao loi o tren.
pause
