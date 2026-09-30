@echo off
rem One-time setup (asks for admin): gives The Pool its own copy of Python (runtime\thepool.exe)
rem and firewall rules for that copy only. local project's firewall rules are never changed.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
cd /d "%~dp0"
python "%~dp0setup_pool.py"
pause
