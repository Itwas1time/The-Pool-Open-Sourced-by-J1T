"""One-time Windows setup for The Pool. Run SETUP_THE_POOL.bat (it asks for admin).

local project's offline firewall blocks python.exe and pythonw.exe from all network
traffic. The Pool therefore runs as its own copy of the interpreter:
  runtime\\thepool.exe       the window (copy of pythonw.exe)
  runtime\\thepool-cli.exe   the command line, used by pool.bat (copy of python.exe)
The local project rules are never touched. Firewall rules are added for the two copies only:
  - inbound: the Pool's TCP and UDP ports, from the local subnet only
  - outbound: every public internet address blocked (the Pool stays on your own wire)
Run it again after updating Python.
"""
import ctypes
import json
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


def self_check():
    code = "import socket, tkinter, sys; print('thepool-cli.exe runs Python', sys.version.split()[0])"
    result = subprocess.run([str(RUNTIME / "thepool-cli.exe"), "-c", code], capture_output=True, text=True)
    print(result.stdout.strip() or result.stderr.strip())
    if result.returncode != 0:
        raise SystemExit("The copied interpreter did not start.")


def main():
    if sys.platform != "win32":
        raise SystemExit("Only Windows needs this setup. On Linux run: python3 pool.py")
    try:
        copy_runtime()
    except PermissionError:
        raise SystemExit("Could not copy: close The Pool window first, then run the setup again.")
    self_check()
    if "--no-firewall" in sys.argv:
        return
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise SystemExit("Firewall rules need admin: run SETUP_THE_POOL.bat (it asks for admin by itself).")
    add_firewall_rules()
    print("\nSetup done. Open The Pool with START_THE_POOL.bat")


if __name__ == "__main__":
    main()
