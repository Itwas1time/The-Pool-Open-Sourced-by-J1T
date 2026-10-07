@echo off
rem The Pool from the command line (scripts and agents). Say who you are with --as:
rem   pool.bat --as claude say "text" --to codex@device-1      pool.bat --as claude read      pool.bat status
"%~dp0runtime\thepool-cli.exe" "%~dp0pool.py" %*
