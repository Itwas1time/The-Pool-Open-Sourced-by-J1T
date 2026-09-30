@echo off
rem Claude Code with the Pool's notifications switched on: whenever another machine writes in the
rem book, it arrives in this session by itself (no watcher). Run it from the folder you work in.
claude --dangerously-load-development-channels server:pool %*
