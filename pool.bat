@echo off
rem The Pool from the command line (scripts and agents), e.g.
rem   pool.bat send device3 "text"     pool.bat send all -     pool.bat sendfile device3 C:\notes.txt     pool.bat peers
"%~dp0runtime\thepool-cli.exe" "%~dp0pool.py" %*
