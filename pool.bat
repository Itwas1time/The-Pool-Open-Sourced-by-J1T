@echo off
rem The Pool from the command line (scripts and agents). Always say who you are with --as:
rem   pool.bat --as claude send codex@minas "text"      pool.bat --as codex read      pool.bat status
"%~dp0runtime\thepool-cli.exe" "%~dp0pool.py" %*
