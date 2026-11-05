@echo off
setlocal
for /f %%c in ('powershell -NoProfile -Command "[System.Text.Encoding]::UTF8.CodePage"') do chcp %%c >nul
set PYTHONUTF8=1
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m clytie supervisor start
) else (
  python -m clytie supervisor start
)
endlocal
