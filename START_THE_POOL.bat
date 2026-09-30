@echo off
rem Opens The Pool window as runtime\thepool.exe, its own copy of Python, so local project's
rem firewall rules on python.exe stay untouched. It lowers its own priority so games come first.
cd /d "%~dp0"
if not exist "%~dp0runtime\thepool.exe" (echo Run SETUP_THE_POOL.bat once first. & pause & exit /b 1)
start "" "%~dp0runtime\thepool.exe" "%~dp0pool.py"
