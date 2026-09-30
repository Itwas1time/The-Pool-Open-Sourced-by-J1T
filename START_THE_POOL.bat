@echo off
rem Opens The Pool window. It lowers its own priority so games always come first.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0pool.py") || (start "" pyw "%~dp0pool.py")
