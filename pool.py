"""The Pool: a tiny messenger between your own machines over wired Ethernet.

Run the same folder on every machine. Machines find each other by UDP
broadcast on their wired Ethernet ports only (Wi-Fi, VPN and virtual adapters
are never used), share a short status every 30 s, and send prompts or files
to each other over TCP on that same wire. Everything received is
saved as a plain file in pool_data/inbox and announced by one line in
pool_data/NEW.txt, so a small watcher script knows there is something to read.

  START_THE_POOL.bat                  open the window
  pool.bat serve                      run with no window
  pool.bat peers                      list known machines
  pool.bat send NAME "text"           send a prompt (NAME can be 'all'; text '-' reads stdin)
  pool.bat sendfile NAME PATH         send a file (NAME can be 'all')

On Windows the Pool runs as runtime\\thepool.exe / thepool-cli.exe (made once by
SETUP_THE_POOL.bat) so local project's firewall block on python.exe is never involved.
On Linux: python3 pool.py [same arguments].

Standard library only. It sleeps between events and runs at below-normal
priority, so it never competes with a game or anything else doing real work.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "pool_config.json"
DATA = HERE / "pool_data"

NAME = (os.environ.get("POOL_NAME") or socket.gethostname()).lower()
ANNOUNCE_EVERY = 30        # seconds between "I'm here" broadcasts
ONLINE_WINDOW = 90         # a machine counts as online if heard from this recently
MAX_MESSAGE = 20_000_000   # bytes on the wire, about 15 MB of file
STATUS_LIMIT = 500         # characters of my_status.txt shared with the others

KEY = b""
TCP_PORT = 50505
UDP_PORT = 50506
wired = []                 # IPv4Interface list: this machine's connected wired Ethernet ports

peers = {}                 # name -> {"ip", "port", "last_seen", "ram", "status"}
lock = threading.Lock()
received_count = 0         # bumps on every stored message so the window knows to refresh


def load_config():
    global KEY, TCP_PORT, UDP_PORT
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({"pool_key": secrets.token_hex(16), "tcp_port": 50505,
                                      "udp_port": 50506}, indent=2), encoding="utf-8")
        print(f"Created {CONFIG} - copy this same file to every machine in the pool.")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    KEY = cfg["pool_key"].encode()
    TCP_PORT = int(cfg.get("tcp_port", 50505))
    UDP_PORT = int(cfg.get("udp_port", 50506))


# ---- wire format: one line, "<hmac> <json>\n", signed with the shared pool key ----

def pack(body):
    raw = json.dumps(body, separators=(",", ":"))
    sig = hmac.new(KEY, raw.encode(), hashlib.sha256).hexdigest()
    return f"{sig} {raw}\n".encode()


def unpack(data):
    """Return the message dict, or None if it was not signed with our pool key."""
    sig, _, raw = data.decode("utf-8", "replace").strip().partition(" ")
    good = hmac.new(KEY, raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), good.encode()):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ---- small helpers ----

def write_file(path, data):
    """Temp file, flush to disk, rename: a watcher never sees half a file."""
    tmp = DATA / (path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log(line):
    path = DATA / "log.txt"
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, DATA / "log.old.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


def safe(text, limit=60):
    """File-name safe: letters, digits, - _ . only; no path tricks."""
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text))
    return keep.strip("._")[:limit] or "x"


def ago_text(seconds):
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds)}s ago"


def is_online(info):
    return time.time() - (info.get("last_seen") or 0) < ONLINE_WINDOW


def my_status():
    try:
        return (DATA / "my_status.txt").read_text(encoding="utf-8", errors="replace").strip()[:STATUS_LIMIT]
    except OSError:
        return ""


def ram_percent():
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tf", "af", "tv", "av", "ax")]
            m = MemoryStatus()
            m.length = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.load)
        with open("/proc/meminfo") as f:
            info = dict(line.split(":", 1) for line in f)
        total, avail = int(info["MemTotal"].split()[0]), int(info["MemAvailable"].split()[0])
        return round(100 * (total - avail) / total)
    except Exception:
        return None


def lower_priority():
    """Below-normal CPU priority: The Pool always yields to the game."""
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


# ---- who is out there (peers.json is the monitoring file other scripts read) ----

def load_peers():
    try:
        saved = json.loads((DATA / "peers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with lock:
        for name, info in saved.items():
            peers[name] = {k: info.get(k) for k in ("ip", "port", "last_seen", "ram", "status")}


def save_peers():
    with lock:
        snapshot = {name: dict(info, online=is_online(info),
                               last_seen_text=f"{datetime.fromtimestamp(info.get('last_seen') or 0):%Y-%m-%d %H:%M:%S}")
                    for name, info in peers.items()}
        try:
            write_file(DATA / "peers.json", json.dumps(snapshot, indent=2).encode())
        except OSError:
            pass  # a reader had it open; the next save fixes it


def find_wired():
    """IPv4 addresses on physical, connected, wired Ethernet ports. Wi-Fi is never used."""
    found = []
    try:
        if os.name == "nt":
            import subprocess
            ps = ("Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and $_.PhysicalMediaType -eq '802.3' }"
                  " | ForEach-Object { Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4"
                  " -ErrorAction SilentlyContinue } | ForEach-Object { $_.IPAddress + '/' + $_.PrefixLength }")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                                 text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            found = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            import fcntl
            import struct
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                for name in os.listdir("/sys/class/net"):
                    port = Path("/sys/class/net", name)
                    if (port / "wireless").exists() or (port / "phy80211").exists() or not (port / "device").exists():
                        continue
                    if (port / "operstate").read_text().strip() != "up":
                        continue
                    req = struct.pack("256s", name.encode()[:15])
                    try:
                        ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])    # SIOCGIFADDR
                        mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])  # SIOCGIFNETMASK
                    except OSError:
                        continue  # port is up but has no IPv4 address yet
                    found.append(f"{ip}/{mask}")
    except Exception as e:
        log(f"could not list network ports: {e}")
    return [ipaddress.IPv4Interface(x) for x in found]


def watch_wired():
    """Re-check the wired ports now and then (cable plugged in or pulled out)."""
    global wired
    while True:
        time.sleep(600 if wired else 120)
        now = find_wired()
        if now != wired:
            wired = now
            log("wired ports: " + (", ".join(map(str, now)) or "none - plug in a network cable"))


def wired_source_for(ip):
    """Which of my wired addresses reaches this IP, or None if it is not on my wire."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return next((str(w.ip) for w in list(wired) if addr in w.network), None)


def announce(only_to=None):
    """Broadcast "I'm here" out of every wired port (or answer one machine directly)."""
    data = pack({"kind": "hello", "from": NAME, "port": TCP_PORT, "ram": ram_percent(), "status": my_status()})
    if only_to:
        sends = [(wired_source_for(only_to), only_to)]
    else:
        sends = [(str(w.ip), str(w.network.broadcast_address)) for w in list(wired)]
    for src, dst in sends:
        if not src:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((src, 0))
                s.sendto(data, (dst, UDP_PORT))
        except OSError:
            pass


def announce_loop():
    while True:
        announce()
        save_peers()
        time.sleep(ANNOUNCE_EVERY)


def listen_udp(sock):
    while True:
        try:
            data, (ip, _) = sock.recvfrom(65535)
        except OSError:
            time.sleep(1)
            continue
        if not wired_source_for(ip):
            continue  # not from my wire (Wi-Fi or anything else): ignore
        msg = unpack(data)
        if not msg or msg.get("kind") != "hello" or not msg.get("from") or msg["from"] == NAME:
            continue
        name = str(msg["from"])
        with lock:
            old = peers.get(name)
            is_new = old is None or old.get("ip") != ip or not is_online(old)
            peers[name] = {"ip": ip, "port": int(msg.get("port") or TCP_PORT), "last_seen": time.time(),
                           "ram": msg.get("ram"), "status": str(msg.get("status") or "")[:STATUS_LIMIT]}
        save_peers()
        if is_new:
            log(f"{name} is online at {ip}")
            announce(only_to=ip)  # so it learns about us now instead of in 30 s


# ---- receiving ----

def store(msg):
    """Save a received message into inbox/ and add one line to NEW.txt."""
    global received_count
    sender = safe(msg.get("from", "unknown"), 40)
    if msg.get("kind") == "file":
        kind, data, label = "file", base64.b64decode(msg.get("data", "")), safe(msg.get("name", "file"))
    else:
        kind, data, label = "prompt", str(msg.get("text", "")).encode("utf-8"), "prompt.txt"
    with lock:
        while True:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            path = DATA / "inbox" / f"{stamp}_from-{sender}_{label}"
            if not path.exists():
                break
            time.sleep(0.002)
        write_file(path, data)
        for attempt in range(5):
            try:
                with open(DATA / "NEW.txt", "a", encoding="utf-8") as f:
                    f.write(f"{stamp}\t{sender}\t{kind}\t{path}\n")
                break
            except PermissionError:
                time.sleep(0.1)
        received_count += 1
    log(f"got {kind} from {sender}: {path.name}")


def handle(conn, ip):
    with conn, conn.makefile("rb") as f:
        try:
            if conn.getsockname()[0] not in {str(w.ip) for w in list(wired)}:
                log(f"ignored a connection from {ip}: it did not come in on a wired port")
                return
            conn.settimeout(30)
            line = f.readline(MAX_MESSAGE + 1)
            msg = unpack(line) if line.endswith(b"\n") else None
            if msg is None:
                log(f"rejected a message from {ip} (wrong pool key or too big)")
                conn.sendall(b"ERR rejected: wrong pool key or too big\n")
                return
            store(msg)
            conn.sendall(b"OK\n")
        except Exception as e:
            log(f"error receiving from {ip}: {e}")
            try:
                conn.sendall(f"ERR {e}\n".encode())
            except OSError:
                pass


def serve_tcp(server):
    while True:
        try:
            conn, (ip, _) = server.accept()
        except OSError:
            time.sleep(1)
            continue
        threading.Thread(target=handle, args=(conn, ip), daemon=True).start()


# ---- sending ----

def send(target, body):
    """Send one message to a machine (or 'all' online ones). Returns [(machine, ok, detail)]."""
    body = dict(body, **{"from": NAME, "sent_at": datetime.now().isoformat(timespec="seconds")})
    data = pack(body)
    if len(data) > MAX_MESSAGE:
        return [(target, False, "too big (limit is about 15 MB)")]
    with lock:
        if target == "all":
            chosen = [(n, dict(i)) for n, i in sorted(peers.items()) if is_online(i)]
        else:
            chosen = [(target, dict(peers[target]))] if target in peers else []
    if not chosen:
        return [(target, False, "no machines online" if target == "all" else "unknown machine (not seen yet)")]
    results = []
    for name, info in chosen:
        source = wired_source_for(info["ip"])
        if not source:
            results.append((name, False, "not reachable on a wired port"))
            continue
        try:
            with socket.create_connection((info["ip"], info["port"]), timeout=5,
                                          source_address=(source, 0)) as s, s.makefile("rb") as f:
                s.settimeout(60)
                s.sendall(data)
                reply = f.readline(300).decode("utf-8", "replace").strip()
            results.append((name, reply == "OK", reply or "no reply"))
        except OSError as e:
            if getattr(e, "winerror", None) == 10013:
                results.append((name, False, "blocked by the firewall - use pool.bat or START_THE_POOL.bat, "
                                             "not python.exe"))
            else:
                results.append((name, False, str(e)))
    for name, ok, detail in results:
        log(f"sent {body.get('kind')} to {name}: {'OK' if ok else detail}")
    return results


def file_body(path):
    path = Path(path)
    return {"kind": "file", "name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}


# ---- start up ----

def start_node():
    global wired
    lower_priority()
    (DATA / "inbox").mkdir(parents=True, exist_ok=True)
    (DATA / "my_status.txt").touch(exist_ok=True)
    load_peers()
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if os.name == "nt":
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        tcp.bind(("0.0.0.0", TCP_PORT))
        tcp.listen(16)
        udp.bind(("0.0.0.0", UDP_PORT))
    except OSError as e:
        raise SystemExit(f"The Pool could not open ports {TCP_PORT}/{UDP_PORT} ({e}).\n"
                         "It is probably already running on this machine.")
    wired = find_wired()
    threading.Thread(target=watch_wired, daemon=True).start()
    threading.Thread(target=serve_tcp, args=(tcp,), daemon=True).start()
    threading.Thread(target=listen_udp, args=(udp,), daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()
    log(f"started as {NAME}; wired ports: {', '.join(map(str, wired)) or 'none'}")


def open_path(path):
    if os.name == "nt":
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(path)])


def count_new():
    try:
        with open(DATA / "NEW.txt", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


# ---- the window ----

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"The Pool - {NAME}")
    root.geometry("900x600")
    try:
        start_node()
    except SystemExit as e:
        root.withdraw()
        messagebox.showerror("The Pool", str(e))
        root.destroy()
        return

    header = ttk.Label(root, text="")
    header.pack(anchor="w", padx=8, pady=(8, 2))
    machines = ttk.Treeview(root, columns=("ip", "seen", "ram", "status"), height=6)
    for col, title, width in (("#0", "Machine", 150), ("ip", "Address", 120), ("seen", "Last seen", 120),
                              ("ram", "RAM used", 80), ("status", "Status", 380)):
        machines.heading(col, text=title)
        machines.column(col, width=width, stretch=(col == "status"))
    machines.pack(fill="x", padx=8)

    ttk.Label(root, text="Inbox (newest first, double-click to open)").pack(anchor="w", padx=8, pady=(10, 2))
    inbox = tk.Listbox(root, height=12)
    inbox.pack(fill="both", expand=True, padx=8)
    inbox.bind("<Double-Button-1>", lambda _e: inbox.curselection() and
               open_path(DATA / "inbox" / inbox.get(inbox.curselection()[0])))

    compose = ttk.Frame(root)
    compose.pack(fill="x", padx=8, pady=(8, 0))
    ttk.Label(compose, text="To:").grid(row=0, column=0, sticky="w")
    target = ttk.Combobox(compose, width=22, state="readonly")
    target.grid(row=0, column=1, sticky="w", padx=4)
    text = tk.Text(compose, height=4, wrap="word")
    text.grid(row=1, column=0, columnspan=5, sticky="ew", pady=4)
    compose.columnconfigure(4, weight=1)
    note = ttk.Label(root, text="")
    note.pack(anchor="w", padx=8, pady=(0, 8))
    outcome = {"text": "", "clear": False}  # written by send threads, shown by refresh()

    def send_in_background(body, what):
        who = target.get()
        if not who:
            note.config(text="Pick a machine first.")
            return
        note.config(text=f"Sending {what} to {who}...")

        def work():
            results = send(who, body)
            outcome["clear"] = body["kind"] == "prompt" and all(ok for _, ok, _ in results)
            outcome["text"] = "   ".join(f"{n}: {'delivered' if ok else 'FAILED - ' + d}" for n, ok, d in results)
        threading.Thread(target=work, daemon=True).start()

    def send_prompt():
        body = text.get("1.0", "end").strip()
        if body:
            send_in_background({"kind": "prompt", "text": body}, "prompt")

    def send_file():
        path = filedialog.askopenfilename(parent=root)
        if path:
            send_in_background(file_body(path), Path(path).name)

    ttk.Button(compose, text="Send prompt", command=send_prompt).grid(row=0, column=2, padx=4)
    ttk.Button(compose, text="Send file...", command=send_file).grid(row=0, column=3, padx=4)

    shown = {"count": -1}

    def refresh():
        now = time.time()
        port = ", ".join(str(w.ip) for w in wired) or "NONE - plug in a network cable"
        header.config(text=f"This machine: {NAME}     Wired port: {port}     Shared status: {DATA / 'my_status.txt'}")
        with lock:
            rows = sorted((name, dict(info)) for name, info in peers.items())
        machines.delete(*machines.get_children())
        for name, info in rows:
            ago = now - (info.get("last_seen") or 0)
            seen = ago_text(ago) if ago < ONLINE_WINDOW else f"offline ({ago_text(ago)})"
            ram = f"{info['ram']}%" if info.get("ram") is not None else "?"
            machines.insert("", "end", text=name, values=(info.get("ip"), seen, ram,
                                                          (info.get("status") or "").replace("\n", " | ")))
        target["values"] = ["all"] + [name for name, _ in rows]
        if received_count != shown["count"]:
            shown["count"] = received_count
            inbox.delete(0, "end")
            for name in sorted(os.listdir(DATA / "inbox"), reverse=True)[:300]:
                inbox.insert("end", name)
        waiting = count_new()
        root.title(f"The Pool - {NAME}" + (f"   ({waiting} waiting in NEW.txt)" if waiting else ""))
        if outcome["text"]:
            note.config(text=outcome["text"])
            if outcome["clear"]:
                text.delete("1.0", "end")
            outcome.update(text="", clear=False)
        root.after(1500, refresh)

    refresh()
    root.mainloop()


def main(argv):
    global wired
    load_config()
    cmd = argv[0] if argv else "gui"
    if cmd == "gui":
        run_gui()
    elif cmd == "serve":
        start_node()
        print(f"The Pool is running as {NAME} (Ctrl+C to stop). Data: {DATA}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    elif cmd == "peers":
        load_peers()
        now = time.time()
        for name, info in sorted(peers.items()):
            ago = now - (info.get("last_seen") or 0)
            state = "online " if ago < ONLINE_WINDOW else "offline"
            print(f"{name:20} {state}  {info.get('ip') or '?':16} seen {ago_text(ago):>8}   "
                  f"ram {info.get('ram')}%   {next(iter((info.get('status') or '').splitlines()), '')}")
        if not peers:
            print("No machines seen yet (is The Pool running here and on the others?)")
    elif cmd in ("send", "sendfile") and len(argv) == 3:
        wired = find_wired()
        load_peers()
        who = argv[1].lower()
        if cmd == "send":
            body = {"kind": "prompt", "text": sys.stdin.read() if argv[2] == "-" else argv[2]}
        else:
            body = file_body(argv[2])
        results = send(who, body)
        for name, ok, detail in results:
            print(f"{name}: {'delivered' if ok else 'FAILED - ' + detail}")
        return 0 if all(ok for _, ok, _ in results) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
