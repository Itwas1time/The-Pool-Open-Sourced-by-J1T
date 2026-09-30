"""One-time Windows setup for The Pool. Run SETUP_THE_POOL.bat (it asks for admin).

local project's offline firewall blocks python.exe and pythonw.exe from all network
traffic. The Pool therefore runs as its own copy of the interpreter:
  runtime\\thepool.exe       the window (copy of pythonw.exe)
  runtime\\thepool-cli.exe   the command line, used by pool.bat (copy of python.exe)
The local project rules are never touched. Firewall rules are added for the two copies only:
  - inbound: the Pool's TCP and UDP ports, from the local subnet only
  - outbound: every public internet address blocked (the Pool stays on your own wire)
It also adds "The Pool" shortcuts (desktop + Start menu, with the Pool's icon) and an
on-demand scheduled task "ThePool" that `pool.bat start` uses to run the Pool with no
window, outside whatever shell asked for it. The task has no trigger: never at logon.
And it gives Claude Code and Codex on this machine the Pool as native tools (an MCP
server named "pool", as agent "claude" and "codex"); Codex's config.toml is backed up first.
Run it again after updating Python or moving this folder (paths are stored exactly).
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
BASE = Path(sys.base_prefix)
EXES = {"thepool.exe": "pythonw.exe", "thepool-cli.exe": "python.exe"}
PUBLIC_INTERNET = ["1.0.0.0-9.255.255.255", "11.0.0.0-126.255.255.255", "128.0.0.0-169.253.255.255",
                   "169.255.0.0-172.15.255.255", "172.32.0.0-192.167.255.255", "192.169.0.0-223.255.255.255"]


def copy_runtime():
    RUNTIME.mkdir(exist_ok=True)
    for new, original in EXES.items():
        shutil.copy2(BASE / original, RUNTIME / new)
    for dll in BASE.glob("*.dll"):
        shutil.copy2(dll, RUNTIME / dll.name)
    (RUNTIME / "pyvenv.cfg").write_text(f"home = {BASE}\ninclude-system-site-packages = false\n"
                                        f"version = {sys.version.split()[0]}\n", encoding="utf-8")
    print(f"Copied Python {sys.version.split()[0]} from {BASE} into {RUNTIME}")


def add_firewall_rules():
    cfg = json.loads((HERE / "pool_config.json").read_text(encoding="utf-8"))
    tcp, udp = int(cfg.get("tcp_port", 50505)), int(cfg.get("udp_port", 50506))
    public = ",".join(f"'{r}'" for r in PUBLIC_INTERNET)
    lines = ["$ErrorActionPreference = 'Stop'",
             "Remove-NetFirewallRule -Group 'The Pool' -ErrorAction SilentlyContinue"]
    for exe in EXES:
        prog = str(RUNTIME / exe)
        common = f"-Group 'The Pool' -Program '{prog}' -Profile Any"
        lines += [
            f"New-NetFirewallRule -DisplayName 'The Pool - {exe} TCP in' {common} -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {tcp} -RemoteAddress LocalSubnet | Out-Null",
            f"New-NetFirewallRule -DisplayName 'The Pool - {exe} UDP in' {common} -Direction Inbound -Action Allow "
            f"-Protocol UDP -LocalPort {udp} -RemoteAddress LocalSubnet | Out-Null",
            f"New-NetFirewallRule -DisplayName 'The Pool - {exe} no internet' {common} -Direction Outbound "
            f"-Action Block -RemoteAddress @({public}) | Out-Null",
        ]
    lines.append("Get-NetFirewallRule -Group 'The Pool' | ForEach-Object { '  ' + $_.DisplayName }")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)],
                            capture_output=True, text=True)
    print(result.stdout.rstrip())
    if result.returncode != 0:
        raise SystemExit(f"Firewall rules failed:\n{result.stderr}")
    print("Firewall rules added (local project's own rules were not changed).")


def powershell(script):
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode != 0:
        raise SystemExit(f"Failed:\n{result.stderr}")


def add_task_and_shortcuts():
    exe, script, icon = RUNTIME / "thepool.exe", HERE / "pool.py", HERE / "assets" / "thepool.ico"
    powershell(f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{script}" serve' -WorkingDirectory '{HERE}'
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -Priority 7
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'ThePool' -Action $action -Settings $settings -Principal $principal -Force `
    -Description 'The Pool with no window. Started on demand by pool.bat start; never at logon.' | Out-Null
'  scheduled task ThePool (on demand only, no trigger)'
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs'))) {{
    $path = Join-Path $dir 'The Pool.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = '{exe}'
    $link.Arguments = '"{script}"'
    $link.WorkingDirectory = '{HERE}'
    $link.IconLocation = '{icon},0'
    $link.Description = 'The Pool: wired messenger between your machines'
    $link.Save()
    '  shortcut ' + $path
}}
""")


def connect_agents():
    """The Pool as native tools: MCP server 'pool' for Claude Code (agent claude) and Codex (agent codex)."""
    exe, script = RUNTIME / "thepool-cli.exe", HERE / "pool.py"
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "pool", "-s", "user"], capture_output=True, text=True)
        r = subprocess.run([claude, "mcp", "add", "pool", "-s", "user", "-e", "POOL_AGENT=claude", "--",
                            str(exe), str(script), "mcp"], capture_output=True, text=True)
        print("  Claude Code: 'pool' tools added (new sessions see them)" if r.returncode == 0 else
              f"  Claude Code: could not add the tools: {(r.stderr or r.stdout).strip()[:300]}")
    else:
        print("  Claude Code not found on PATH - skipped")
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not codex_home.exists():
        print("  Codex not found (no .codex folder) - skipped")
        return
    cfg = codex_home / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    kept, skipping = [], False
    for line in text.splitlines():
        if line.strip().startswith("["):
            skipping = line.strip() in ("[mcp_servers.pool]", "[mcp_servers.pool.env]")
        if not skipping:
            kept.append(line)
    block = ["", "[mcp_servers.pool]", f"command = '{exe}'", f"args = ['{script}', 'mcp']",
             "startup_timeout_sec = 30", "tool_timeout_sec = 130", "", "[mcp_servers.pool.env]",
             'POOL_AGENT = "codex"']
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_name("config.toml.before-pool"))
    cfg.write_text(newline.join(kept).rstrip() + newline + newline.join(block) + newline, encoding="utf-8",
                   newline="")
    print(f"  Codex: 'pool' tools added to {cfg} (previous copy: config.toml.before-pool)")


def stop_running_pool():
    """The runtime files cannot be replaced while the Pool runs from them."""
    cli = RUNTIME / "thepool-cli.exe"
    if cli.exists():
        result = subprocess.run([str(cli), str(HERE / "pool.py"), "stop"], capture_output=True, text=True)
        return "stopped" in result.stdout
    return False


def self_check():
    code = "import socket, tkinter, sys; print('thepool-cli.exe runs Python', sys.version.split()[0])"
    result = subprocess.run([str(RUNTIME / "thepool-cli.exe"), "-c", code], capture_output=True, text=True)
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode != 0:
        raise SystemExit("The copied interpreter did not start.")


def main():
    if sys.platform != "win32":
        raise SystemExit("Only Windows needs this setup. On Linux run: python3 pool.py")
    was_running = stop_running_pool()
    try:
        copy_runtime()
    except PermissionError:
        raise SystemExit("Could not copy: close The Pool window first, then run the setup again.")
    self_check()
    add_task_and_shortcuts()
    connect_agents()
    if "--no-firewall" not in sys.argv:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise SystemExit("Firewall rules need admin: run SETUP_THE_POOL.bat (it asks for admin by itself).")
        add_firewall_rules()
    if was_running:
        subprocess.run([str(RUNTIME / "thepool-cli.exe"), str(HERE / "pool.py"), "start"])
    print("\nSetup done. Open The Pool from its desktop or Start menu shortcut; agents use pool.bat or the "
          "'pool' tools.")


if __name__ == "__main__":
    main()
