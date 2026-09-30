"""The Pool: a shared book your machines write in and talk through, over wired Ethernet.

Like Tom Riddle's diary: write in it on one machine and the words appear in the book
on every other machine. Each machine keeps the whole conversation in one plain text
file, pool_data/pool.txt, for about two months. People and agents (Claude, Codex)
use it the same way.

  pool.bat say "text" [--to codex@minas]   write in the book (every online machine gets it)
  pool.bat read                            what is new since I last read
  pool.bat wait [--timeout SECONDS]        block until someone writes, then show it
  pool.bat book [N]                        the last N entries (default 20)
  pool.bat sendfile PATH [--to NAME]       send a file (saved in pool_data/files/)
  pool.bat peers                           machines in the pool
  pool.bat start | stop | status | serve   the Pool itself (start = in the background, no window)
  pool.bat mcp                             the same as tools for Claude Code and Codex
  --as NAME (or set POOL_AGENT) says who is writing: claude, codex, ...  The window writes as operator.

Wired Ethernet only: Wi-Fi, VPN and virtual adapters are never used. On Windows it runs
as runtime\\thepool.exe / thepool-cli.exe (made once by SETUP_THE_POOL.bat) so local project's
firewall block on python.exe is never involved. On Linux: python3 pool.py [same arguments].
Standard library only; it sleeps between events and runs at below-normal priority.
"""
import base64
import ctypes
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "pool_config.json"
DATA = HERE / "pool_data"
BOOK = DATA / "pool.txt"
ASSETS = HERE / "assets"
TASK = "ThePool"           # Windows scheduled task that runs the Pool with no window (made by setup)

HOST = socket.gethostname().lower()
NAME = HOST                # replaced in load_config() by POOL_NAME or the shared names table
AGENT = os.environ.get("POOL_AGENT", "")
KEEP_DAYS = 60             # the book keeps about two months
ANNOUNCE_EVERY = 30        # seconds between "I'm here" broadcasts
ONLINE_WINDOW = 90         # a machine counts as online if heard from this recently
MAX_MESSAGE = 20_000_000   # bytes on the wire, about 15 MB of file
STATUS_LIMIT = 500         # characters of my_status.txt shared with the others

KEY = b""
TCP_PORT = 50505
UDP_PORT = 50506
NAMES = {}                 # hostname -> fleet name, from pool_config.json (same file on every machine)
wired = []                 # IPv4Interface list: this machine's connected wired Ethernet ports

peers = {}                 # name -> {"ip", "port", "last_seen", "ram", "status"}
lock = threading.Lock()
book_lock = threading.Lock()
alive = threading.Event()  # set while this process is the machine's Pool
sockets = []
node_mode, started_at = "", ""


def load_config():
    global KEY, TCP_PORT, UDP_PORT, NAMES, NAME
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({"pool_key": secrets.token_hex(16), "tcp_port": 50505,
                                      "udp_port": 50506, "names": {}}, indent=2), encoding="utf-8")
        print(f"Created {CONFIG} - copy this same file to every machine in the pool.")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    KEY = cfg["pool_key"].encode()
    TCP_PORT = int(cfg.get("tcp_port", 50505))
    UDP_PORT = int(cfg.get("udp_port", 50506))
    NAMES = {str(k).lower(): str(v).lower() for k, v in cfg.get("names", {}).items()}
    NAME = (os.environ.get("POOL_NAME") or NAMES.get(HOST, HOST)).lower()


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
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


# ---- small helpers ----

def write_file(path, data):
    """Temp file, flush to disk, rename: nobody ever sees half a file."""
    tmp = DATA / f"{path.name}.{os.getpid()}-{threading.get_ident()}.part"
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


def agent_name(text):
    return "".join(c for c in str(text or "").lower() if c.isalnum() or c in "-_").strip("-_")[:30]


def canonical(name):
    """A machine's fleet name (hostnames are mapped by the shared names table)."""
    name = str(name or "").lower()
    return NAMES.get(name, name)


def addr(who, machine):
    return f"{who}@{machine}" if who else machine


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
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


# ---- the book: one plain text file, an entry is "[time] who@machine -> to" then indented lines ----

def write_in_book(stamp, who, machine, to, text):
    head = f"[{stamp}] {addr(who, machine)}" + (f" -> {to}" if to else "")
    body = "\n".join("  " + line for line in (str(text).splitlines() or [""]))
    DATA.mkdir(parents=True, exist_ok=True)
    with book_lock, open(BOOK, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{head}\n{body}\n")  # one write per entry


def last_entries(n):
    try:
        with open(BOOK, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines(keepends=True)
    except OSError:
        return ""
    heads = [i for i, line in enumerate(lines) if line.startswith("[")]
    return "".join(lines[heads[-n]:]) if len(heads) >= n else "".join(lines[heads[0]:]) if heads else ""


def new_entries(who, first_time="history"):
    """What was written since this reader last read, and move its bookmark to the end.
    A reader with no bookmark gets the last 20 entries ('history') or nothing ('skip')."""
    mark = DATA / "read" / (agent_name(who) or "anyone")
    try:
        size = BOOK.stat().st_size
    except OSError:
        size = 0
    try:
        start = min(int(mark.read_text()), size)
    except (OSError, ValueError):
        start = None
    if start is None:
        text = last_entries(20) if first_time == "history" else ""
        end = size
    else:
        with open(BOOK, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        data = data[:data.rfind(b"\n") + 1]  # never stop half-way through an entry
        text, end = data.decode("utf-8", "replace"), start + len(data)
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(str(end))
    return text


def prune_book():
    """Keep about two months: drop entries older than KEEP_DAYS and move every bookmark back to match."""
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    with book_lock:
        try:
            data = BOOK.read_bytes()
        except OSError:
            return
        cut = 0
        for line in data.splitlines(keepends=True):
            if line.startswith(b"[") and line[1:11].decode("ascii", "replace") >= cutoff:
                break
            cut += len(line)
        if cut == 0:
            return
        try:
            write_file(BOOK, data[cut:])
        except OSError:
            return  # someone had the book open; try again tomorrow
    for mark in (DATA / "read").glob("*"):
        try:
            mark.write_text(str(max(0, int(mark.read_text()) - cut)))
        except (OSError, ValueError):
            pass
    log(f"book pruned: {cut} bytes older than {cutoff} removed")


# ---- who is out there (peers.json is the monitoring file other scripts read) ----

def load_peers():
    try:
        saved = json.loads((DATA / "peers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with lock:
        for name, info in saved.items():
            name = canonical(name)
            entry = {k: info.get(k) for k in ("ip", "port", "last_seen", "ram", "status")}
            old = peers.get(name)
            if old is None or (entry.get("last_seen") or 0) >= (old.get("last_seen") or 0):
                peers[name] = entry


def save_peers():
    with lock:
        snapshot = {name: dict(info, online=is_online(info),
                               last_seen_text=f"{datetime.fromtimestamp(info.get('last_seen') or 0):%Y-%m-%d %H:%M:%S}")
                    for name, info in peers.items()}
        try:
            write_file(DATA / "peers.json", json.dumps(snapshot, indent=2).encode())
        except OSError:
            pass  # a reader had it open; the next save fixes it


def peers_text():
    now = time.time()
    rows = []
    with lock:
        items = sorted(peers.items())
    for name, info in items:
        ago = now - (info.get("last_seen") or 0)
        status = next(iter((info.get("status") or "").splitlines()), "")
        rows.append(f"{name:16} {'online ' if ago < ONLINE_WINDOW else 'offline'}  {info.get('ip') or '?':16} "
                    f"seen {ago_text(ago):>8}   ram {info.get('ram')}%" + (f"   {status}" if status else ""))
    return "\n".join(rows) or "No other machine seen yet."


def find_wired():
    """IPv4 addresses on physical, connected, wired Ethernet ports. Wi-Fi is never used."""
    found = []
    try:
        if os.name == "nt":
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
    while alive.is_set():
        time.sleep(600 if wired else 120)
        now = find_wired()
        if now != wired:
            wired = now
            write_node()
            log("wired ports: " + (", ".join(map(str, now)) or "none - plug in a network cable"))


def wired_source_for(ip):
    """Which of my wired addresses reaches this IP, or None if it is not on my wire."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return next((str(w.ip) for w in list(wired) if address in w.network), None)


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
    pruned_on = None
    while alive.is_set():
        announce()
        save_peers()
        if pruned_on != datetime.now().date():
            pruned_on = datetime.now().date()
            prune_book()
        time.sleep(ANNOUNCE_EVERY)


def listen_udp(sock):
    while True:
        try:
            data, (ip, _) = sock.recvfrom(65535)
        except OSError:
            if sock.fileno() == -1:
                return  # closed by finish_node()
            time.sleep(1)
            continue
        if not wired_source_for(ip):
            continue  # not from my wire (Wi-Fi or anything else): ignore
        msg = unpack(data)
        if not msg or msg.get("kind") != "hello" or not msg.get("from"):
            continue
        name = canonical(msg["from"])
        if name == NAME:
            continue
        try:
            port = int(msg.get("port") or TCP_PORT)
        except (TypeError, ValueError):
            continue
        with lock:
            old = peers.get(name)
            is_new = old is None or old.get("ip") != ip or not is_online(old)
            peers[name] = {"ip": ip, "port": port, "last_seen": time.time(), "ram": msg.get("ram"),
                           "status": str(msg.get("status") or "")[:STATUS_LIMIT]}
        save_peers()
        if is_new:
            log(f"{name} is online at {ip}")
            announce(only_to=ip)  # so it learns about us now instead of in 30 s


# ---- receiving: everything goes into the book ----

def receive(msg):
    """Write a received message (or file) into this machine's book."""
    machine = safe(canonical(msg.get("from", "unknown")), 40)
    who = agent_name(msg.get("who") or msg.get("from_agent"))  # from_agent: sent by a v4 Pool
    to = str(msg.get("to") or msg.get("to_agent") or "")[:80]
    stamp = str(msg.get("sent_at") or "").replace("T", " ")[:19] or f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    text = str(msg.get("text") or "")
    if msg.get("kind") == "file":
        folder = DATA / "files"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y%m%d-%H%M%S}_{machine}_{safe(msg.get('name', 'file'))}"
        write_file(path, base64.b64decode(msg.get("data", "")))
        text = f"(file) {msg.get('name', 'file')}  saved at {path}" + (f"\n{text}" if text else "")
    write_in_book(stamp, who, machine, to, text)
    if machine != NAME:
        notify_here(f"New in the Pool book from {addr(who, machine)}" + (f" to {to}" if to else "") +
                    f":\n{text[:1500]}\n(Read the book with the pool read tool or: pool read)")


def notify_here(message):
    """The notification, no watcher needed: run this machine's own notify command from
    pool_data/notify.json, e.g. {"command": ["codex", "queue", "--thread", "<id>", "--message", "{message}"]}
    to wake a Codex session. The words only ever go in as text; nothing received is executed."""
    try:
        command = json.loads((DATA / "notify.json").read_text(encoding="utf-8"))["command"]
        args = [str(a).replace("{message}", message) for a in command]
    except (OSError, ValueError, KeyError, TypeError):
        return

    def run():
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if r.returncode:
                log(f"notify command failed: {(r.stderr or r.stdout).strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            log(f"notify command failed: {e}")
    threading.Thread(target=run, daemon=True).start()


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
            receive(msg)
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
            if server.fileno() == -1:
                return  # closed by finish_node()
            time.sleep(1)
            continue
        threading.Thread(target=handle, args=(conn, ip), daemon=True).start()


# ---- writing: into my book and every online machine's book ----

def mention(to):
    """Tidy a --to mention: 'codex@minas' -> 'codex@device1'."""
    who, _, machine = str(to or "").strip().lower().rpartition("@")
    if not machine:
        return ""
    machine = canonical(machine)
    with lock:
        known = list(peers) + [NAME]
    matches = [n for n in known if n.startswith(machine)]
    machine = machine if machine in known or len(matches) != 1 else matches[0]
    return addr(agent_name(who), machine)


def deliver(name, info, data):
    source = wired_source_for(info["ip"])
    if not source:
        return name, False, "not reachable on a wired port"
    try:
        with socket.create_connection((info["ip"], info["port"]), timeout=5,
                                      source_address=(source, 0)) as s, s.makefile("rb") as f:
            s.settimeout(60)
            s.sendall(data)
            reply = f.readline(300).decode("utf-8", "replace").strip()
        return name, reply == "OK", reply or "no reply"
    except OSError as e:
        if getattr(e, "winerror", None) == 10013:
            return name, False, "blocked by the firewall - use pool.bat or the shortcut, not python.exe"
        return name, False, str(e)


def say(text, to="", who="", path=None):
    """Write in the book here and on every online machine. Returns [(machine, ok, detail)]."""
    body = {"kind": "file" if path else "chat", "from": NAME, "who": agent_name(who), "to": mention(to),
            "text": text, "sent_at": datetime.now().isoformat(timespec="seconds")}
    if path:
        body.update(name=Path(path).name, data=base64.b64encode(Path(path).read_bytes()).decode())
    data = pack(body)
    if len(data) > MAX_MESSAGE:
        return [("", False, "too big (limit is about 15 MB)")]
    receive(dict(body))  # my own copy of the book
    with lock:
        targets = [(n, dict(i)) for n, i in sorted(peers.items()) if is_online(i)]
    results = [deliver(name, info, data) for name, info in targets]
    for name, ok, detail in results:
        if not ok:
            log(f"could not deliver to {name}: {detail}")
    return results


def said_text(results):
    if not results:
        return "Written in this machine's book; no other machine is online."
    if results[0][0] == "":
        return f"Not written: {results[0][2]}"
    return "Written. " + "   ".join(f"{n}: {'delivered' if ok else 'NOT delivered - ' + d}" for n, ok, d in results)


def prepare_to_write():
    """A command-line or MCP writer borrows the running Pool's view of the wire and the machines."""
    global wired
    DATA.mkdir(parents=True, exist_ok=True)
    load_peers()
    wired = (wired_from_node() if node_running() else []) or find_wired()


def split_entries(text):
    entries, current = [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("[") and current:
            entries.append("".join(current))
            current = []
        current.append(line)
    return entries + (["".join(current)] if current else [])


def author(entry):
    return entry.split("] ", 1)[-1].split("\n")[0].split(" -> ")[0].strip()


def wait_new(who, timeout):
    """The notification: block until someone else writes something new, then return it (your own
    writes never wake you). In Claude Code, run it in the background: the session wakes when it exits."""
    end = time.time() + max(0.0, float(timeout))
    me = addr(agent_name(who) or "anyone", NAME)
    if not (DATA / "read" / (agent_name(who) or "anyone")).exists():
        new_entries(who, first_time="skip")  # a first-time waiter starts at the end of the book
    got = []
    while True:
        got += split_entries(new_entries(who))
        if any(author(e) != me for e in got) or time.time() >= end:
            return "".join(got) if any(author(e) != me for e in got) else ""
        time.sleep(2)


# ---- start, stop, status: one Pool per machine ----

def node_running():
    """True if a Pool already holds this machine's ports (window or background)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", UDP_PORT))
            return False
        except OSError:
            return True


def read_node():
    try:
        return json.loads((DATA / "node.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_node():
    info = {"pid": os.getpid(), "mode": node_mode, "started": started_at, "wired": [str(w) for w in wired]}
    try:
        write_file(DATA / "node.json", json.dumps(info, indent=2).encode())
    except OSError:
        pass


def wired_from_node():
    return [ipaddress.IPv4Interface(x) for x in read_node().get("wired", [])]


def stop_requested():
    return (DATA / "STOP").exists()


def start_node(mode):
    global wired, node_mode, started_at
    lower_priority()
    DATA.mkdir(parents=True, exist_ok=True)
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
        tcp.close()
        udp.close()
        raise SystemExit(f"The Pool could not open ports {TCP_PORT}/{UDP_PORT} ({e}).\n"
                         "It is probably already running on this machine.")
    sockets[:] = [tcp, udp]
    node_mode, started_at = mode, datetime.now().isoformat(timespec="seconds")
    try:
        (DATA / "STOP").unlink()  # a leftover stop request must not stop this new Pool
    except FileNotFoundError:
        pass
    wired = find_wired()
    write_node()
    alive.set()
    threading.Thread(target=watch_wired, daemon=True).start()
    threading.Thread(target=serve_tcp, args=(tcp,), daemon=True).start()
    threading.Thread(target=listen_udp, args=(udp,), daemon=True).start()
    threading.Thread(target=announce_loop, daemon=True).start()
    log(f"started as {NAME} ({mode}); wired ports: {', '.join(map(str, wired)) or 'none'}")


def finish_node():
    """Close the ports and clean up (a stop was requested or the window closed)."""
    alive.clear()
    for s in sockets:
        s.close()
    for name in ("STOP", "node.json"):
        try:
            (DATA / name).unlink()
        except OSError:
            pass
    log(f"stopped ({node_mode})")


def serve_forever():
    start_node("background")
    print(f"The Pool is running as {NAME} with no window (Ctrl+C or 'pool.bat stop' ends it). Book: {BOOK}")
    try:
        while not stop_requested():
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finish_node()


def start_background():
    """Start a windowless Pool that outlives whoever asked: Task Scheduler on Windows, systemd on Linux."""
    if node_running():
        return 0, f"The Pool is already running on {NAME} ({read_node().get('mode', '?')})."
    if os.name == "nt":
        cmd = ["schtasks", "/Run", "/TN", TASK]
        missing = f"The '{TASK}' scheduled task is missing: run SETUP_THE_POOL.bat once."
    elif shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "cat", "thepool.service"],
                                                      capture_output=True).returncode == 0:
        cmd, missing = ["systemctl", "--user", "start", "thepool.service"], "systemctl could not start thepool.service."
    elif shutil.which("systemd-run"):
        cmd = ["systemd-run", "--user", "--collect", "--quiet", "--unit", "thepool", sys.executable,
               str(HERE / "pool.py"), "serve"]
        missing = "systemd-run could not start it."
    else:
        return 1, "No systemd here: run 'python3 pool.py serve' in a terminal you keep open."
    result = subprocess.run(cmd, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode != 0:
        return 1, f"{missing} {(result.stderr or result.stdout).strip()[:300]}"
    for _ in range(40):
        time.sleep(0.5)
        if node_running():
            return 0, f"The Pool is running in the background on {NAME}."
    return 1, f"It did not start - see {DATA / 'log.txt'}"


def stop_node():
    if not node_running():
        return 0, "The Pool is not running."
    (DATA / "STOP").touch()
    for _ in range(40):
        time.sleep(0.5)
        if not node_running():
            return 0, "The Pool stopped."
    return 1, "It is still running after 20 s."


def show_status():
    running = node_running()
    node = read_node() if running else {}
    load_peers()
    ports = node.get("wired") if running else [str(w) for w in find_wired()]
    online = sorted(n for n, i in peers.items() if is_online(i)) if running else []
    size = BOOK.stat().st_size if BOOK.exists() else 0
    first = ""
    if size:
        with open(BOOK, encoding="utf-8", errors="replace") as f:
            first = f.readline()[1:11]
    return (0 if running else 1), "\n".join([
        f"The Pool on {NAME}: running ({node.get('mode', '?')}, pid {node.get('pid', '?')}, "
        f"since {node.get('started', '?')})" if running else f"The Pool on {NAME}: not running",
        f"wired port: {', '.join(ports or []) or 'none - plug in a network cable'}",
        f"machines online: {', '.join(online) or 'none'}",
        f"book: {BOOK} ({size // 1024} KB" + (f", since {first})" if first else ")")])


def open_path(path):
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---- MCP server: the same as native tools in Claude Code and Codex ----

def mcp_tools():
    s = {"type": "string"}

    def tool(name, description, props, required):
        return {"name": name, "description": description,
                "inputSchema": {"type": "object", "properties": props, "required": required}}
    return [
        tool("say", "Write in the Pool's book: every machine online gets it. Optional 'to' names who it is for "
             "(codex@device1, claude@device4, or a machine).", {"text": s, "to": s}, ["text"]),
        tool("read", "What was written in the book since you last read.", {}, []),
        tool("wait", "Block until someone writes something new (up to timeout_seconds, default 50, max 110), "
             "then return it.", {"timeout_seconds": {"type": "number"}}, []),
        tool("peers", "Machines in the pool: online or not, RAM use, status.", {}, []),
    ]


def call_tool(name, args, who):
    if name == "say":
        prepare_to_write()
        return said_text(say(args["text"], args.get("to", ""), who))
    if name == "read":
        return new_entries(who) or "Nothing new in the book."
    if name == "wait":
        timeout = min(max(float(args.get("timeout_seconds") or 50), 0), 110)
        return wait_new(who, timeout) or f"Nothing new in {int(timeout)} s."
    if name == "peers":
        load_peers()
        return peers_text()
    raise ValueError(f"unknown tool {name}")


def push_to_session(who, respond):
    """The notification, no watcher needed: when someone else writes in the book, push it into the running
    Claude Code session as a channel event (Claude started with: claude --dangerously-load-development-channels
    server:pool). read still shows the same entries, so nothing is lost if the session ignores pushes."""
    me = addr(agent_name(who) or "anyone", NAME)
    seen = BOOK.stat().st_size if BOOK.exists() else 0
    while True:
        time.sleep(2)
        size = BOOK.stat().st_size if BOOK.exists() else 0
        if size <= seen:
            seen = min(seen, size)  # the book was pruned
            continue
        with open(BOOK, "rb") as f:
            f.seek(seen)
            chunk = f.read(size - seen)
        chunk = chunk[:chunk.rfind(b"\n") + 1]
        seen += len(chunk)
        others = [e for e in split_entries(chunk.decode("utf-8", "replace")) if author(e) != me]
        if others:
            respond({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                     "params": {"content": "New in the Pool book:\n" + "".join(others).rstrip(),
                                "meta": {"machine": NAME}}})


def mcp_serve(who):
    """MCP over stdio: one JSON-RPC message per line; tool calls run in threads so a wait blocks nothing."""
    sys.stdin.reconfigure(encoding="utf-8")
    out = sys.stdout
    out.reconfigure(encoding="utf-8")
    write_lock = threading.Lock()
    me = addr(agent_name(who) or "anyone", NAME)
    guide = (f"The Pool is a shared book between operator's machines (wired). You are {me}. say writes in it and "
             "every machine online gets it; read shows what is new; wait blocks until someone writes. Say who "
             "a message is for with 'to'. Code goes through GitHub: name the commit, never paste code. "
             "Never put secrets in the book.")

    def respond(message):
        with write_lock:
            out.write(json.dumps(message) + "\n")
            out.flush()

    def run_tool(rid, params):
        try:
            text, error = call_tool(params.get("name"), params.get("arguments") or {}, who), False
        except Exception as e:
            text, error = f"{type(e).__name__}: {e}", True
        respond({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}],
                                                          "isError": error}})

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        rid, method, params = request.get("id"), request.get("method"), request.get("params") or {}
        if rid is None:
            continue  # notifications need no answer
        if method == "initialize":
            respond({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}, "experimental": {"claude/channel": {}}},
                "serverInfo": {"name": "the-pool", "version": "5"}, "instructions": guide}})
            if "claude" in str((params.get("clientInfo") or {}).get("name", "")).lower():
                threading.Thread(target=push_to_session, args=(who, respond), daemon=True).start()
        elif method == "tools/list":
            respond({"jsonrpc": "2.0", "id": rid, "result": {"tools": mcp_tools()}})
        elif method == "tools/call":
            threading.Thread(target=run_tool, args=(rid, params), daemon=True).start()
        elif method == "ping":
            respond({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            respond({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


# ---- the window: a dark chat box, with the painting as icon and banner ----

PALETTE = {"bg": "#120f1a", "panel": "#1b1626", "field": "#241d33", "line": "#2e2640", "fg": "#ece6f4",
           "muted": "#9a90b0", "gold": "#f2a65a", "blue": "#7cc4ef", "sel": "#463766"}


def dark_theme(root):
    from tkinter import ttk
    c = PALETTE
    root.configure(bg=c["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"], troughcolor=c["panel"],
                    selectbackground=c["sel"], selectforeground=c["fg"], insertcolor=c["fg"], arrowcolor=c["gold"],
                    font=("Segoe UI", 10))
    style.configure("TButton", background=c["sel"], foreground=c["fg"], padding=(14, 5), relief="flat",
                    borderwidth=0)
    style.map("TButton", background=[("pressed", c["gold"]), ("active", "#5a4885")],
              foreground=[("pressed", c["bg"])])
    style.configure("TCombobox", fieldbackground=c["field"], background=c["field"], foreground=c["fg"], padding=4)
    style.configure("Vertical.TScrollbar", background=c["field"], troughcolor=c["panel"], borderwidth=0)
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["sel"])
    style.configure("Muted.TLabel", foreground=c["muted"])


def window_chrome(root):
    """The painting as the window/taskbar icon, and a dark Windows title bar."""
    import tkinter as tk
    try:
        icons = [tk.PhotoImage(file=str(ASSETS / name)) for name in ("thepool.png", "thepool-32.png")]
        root.iconphoto(True, *icons)
        root.pool_icons = icons  # keep a reference or Tk drops the images
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = (ctypes.c_void_p,)
        hwnd = ctypes.c_void_p(user32.GetParent(root.winfo_id()))
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: current builds, then early Windows 10
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37)  # repaint the frame now
    except Exception:
        pass


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    global wired

    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ThePool")  # own taskbar icon
        except Exception:
            pass
    root = tk.Tk()
    root.title(f"The Pool - {NAME}")
    root.geometry("900x720")
    root.minsize(640, 520)
    dark_theme(root)
    c = PALETTE
    viewer = node_running()  # a background Pool already runs here: this window only shows it
    if viewer:
        wired = wired_from_node()
    else:
        try:
            start_node("window")
        except SystemExit as e:
            root.withdraw()
            messagebox.showerror("The Pool", str(e))
            root.destroy()
            return
    window_chrome(root)

    banner = tk.Canvas(root, height=150, bg=c["bg"], highlightthickness=0)
    banner.pack(fill="x")
    try:
        banner.art = tk.PhotoImage(file=str(ASSETS / "banner.png"))
        banner.create_image(0, 0, anchor="nw", image=banner.art)
    except tk.TclError:
        pass
    for dx, colour in ((2, "#000000"), (0, "#fff1de")):
        banner.create_text(30 + dx, 70 + dx, anchor="w", text="The Pool", fill=colour, font=("Georgia", 30, "bold"))
    subtitle = banner.create_text(33, 112, anchor="w", text="", fill="#d9cdef", font=("Segoe UI", 10))

    body = ttk.Frame(root, padding=(18, 0, 18, 14))
    body.pack(fill="both", expand=True)
    machines = ttk.Label(body, text="", style="Muted.TLabel")
    machines.pack(anchor="w", pady=(0, 6))

    pane = ttk.Frame(body)
    pane.pack(fill="both", expand=True)
    book = tk.Text(pane, wrap="word", bg=c["panel"], fg=c["fg"], relief="flat", highlightthickness=0,
                   padx=12, pady=10, font=("Segoe UI", 10), spacing1=1, state="disabled", cursor="arrow")
    scroll = ttk.Scrollbar(pane, orient="vertical", command=book.yview)
    book.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    book.pack(side="left", fill="both", expand=True)
    book.tag_configure("head", foreground=c["gold"], font=("Segoe UI Semibold", 9), spacing1=8)
    book.tag_configure("mine", foreground=c["blue"], font=("Segoe UI Semibold", 9), spacing1=8)

    compose = ttk.Frame(body)
    compose.pack(fill="x", pady=(10, 0))
    ttk.Label(compose, text="To").grid(row=0, column=0, sticky="w")
    target = ttk.Combobox(compose, width=26)
    target.set("everyone")
    target.grid(row=0, column=1, sticky="w", padx=8)
    text = tk.Text(compose, height=3, wrap="word", bg=c["field"], fg=c["fg"], insertbackground=c["gold"],
                   relief="flat", highlightthickness=1, highlightbackground=c["line"], highlightcolor=c["gold"],
                   padx=10, pady=8, font=("Segoe UI", 10))
    text.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
    compose.columnconfigure(4, weight=1)
    note = ttk.Label(body, text="Enter writes in the book, Shift+Enter makes a new line.", style="Muted.TLabel")
    note.pack(anchor="w", pady=(6, 0))
    outcome = {"text": ""}  # written by send threads, shown by refresh()
    shown = {"size": 0, "targets": None}

    def show(chunk):
        book.configure(state="normal")
        for line in chunk.splitlines(keepends=True):
            if line.startswith("["):
                author = line.split("] ", 1)[-1].split(" -> ")[0].strip()
                book.insert("end", line, "mine" if author == NAME or author.endswith("@" + NAME) else "head")
            else:
                book.insert("end", line)
        book.configure(state="disabled")
        book.see("end")

    def write(**kwargs):
        who = target.get().strip()
        to = "" if who in ("", "everyone") else who

        def work():
            outcome["text"] = said_text(say(to=to, who="operator", **kwargs))
        threading.Thread(target=work, daemon=True).start()

    def send_text(_event=None):
        message = text.get("1.0", "end").strip()
        if message:
            text.delete("1.0", "end")
            write(text=message)
        return "break"

    def send_file():
        path = filedialog.askopenfilename(parent=root)
        if path:
            write(text="", path=path)

    text.bind("<Return>", send_text)
    text.bind("<Shift-Return>", lambda _e: None)
    ttk.Button(compose, text="Write", command=send_text).grid(row=0, column=2, padx=(0, 6))
    ttk.Button(compose, text="Send file...", command=send_file).grid(row=0, column=3)

    def close():
        if not viewer:
            finish_node()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)

    def refresh():
        global wired
        if viewer:
            load_peers()
            wired = wired_from_node()
            node = "background Pool running" if node_running() else "background Pool STOPPED"
        elif stop_requested():
            close()
            return
        else:
            node = "running in this window"
        port = ", ".join(str(w.ip) for w in wired) or "no cable - plug one in"
        banner.itemconfig(subtitle, text=f"{NAME}     wired {port}     {node}")
        now = time.time()
        with lock:
            rows = sorted((name, dict(info)) for name, info in peers.items())
        machines.config(text="Machines:  " + ("     ".join(
            f"{n} ({'online, RAM ' + str(i.get('ram')) + '%' if now - (i.get('last_seen') or 0) < ONLINE_WINDOW else 'offline'})"
            for n, i in rows) or "none seen yet"))
        targets = ("everyone", *(n for n, _ in rows))
        if targets != shown["targets"]:
            shown["targets"] = targets
            target["values"] = targets
        size = BOOK.stat().st_size if BOOK.exists() else 0
        if size < shown["size"]:  # the book was pruned: start over
            book.configure(state="normal")
            book.delete("1.0", "end")
            book.configure(state="disabled")
            shown["size"] = 0
        if size > shown["size"]:
            if shown["size"] == 0:
                show(last_entries(300))
            else:
                with open(BOOK, "rb") as f:
                    f.seek(shown["size"])
                    show(f.read(size - shown["size"]).decode("utf-8", "replace"))
            shown["size"] = size
        if outcome["text"]:
            note.config(text=outcome["text"])
            outcome["text"] = ""
        root.after(1500, refresh)

    refresh()
    text.focus_set()
    root.mainloop()


# ---- command line ----

def main(argv):
    import argparse
    load_config()
    if not argv or argv[0] == "gui":
        run_gui()
        return 0
    ap = argparse.ArgumentParser(prog="pool", description="The Pool: a shared book your machines talk through.")
    ap.add_argument("--as", dest="who", default=AGENT, help="who is writing: claude, codex, ... ($POOL_AGENT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name, *args):
        p = sub.add_parser(name)
        p.add_argument("--as", dest="who", default=argparse.SUPPRESS)
        for a in args:
            p.add_argument(a)
        return p
    command("say", "text").add_argument("--to", default="")
    command("read")
    command("wait").add_argument("--timeout", type=float, default=600)
    command("book").add_argument("n", nargs="?", type=int, default=20)
    command("sendfile", "path").add_argument("--to", default="")
    for name in ("peers", "start", "stop", "status", "serve", "mcp"):
        command(name)
    args = ap.parse_args(argv)
    who = args.who

    if args.cmd == "serve":
        serve_forever()
        return 0
    if args.cmd == "mcp":
        mcp_serve(who)
        return 0
    DATA.mkdir(parents=True, exist_ok=True)
    if args.cmd in ("start", "stop", "status"):
        code, text = {"start": start_background, "stop": stop_node, "status": show_status}[args.cmd]()
        print(text)
        return code
    if args.cmd == "peers":
        load_peers()
        print(peers_text())
        return 0
    if args.cmd in ("say", "sendfile"):
        prepare_to_write()
        if args.cmd == "say":
            results = say(sys.stdin.read() if args.text == "-" else args.text, args.to, who)
        else:
            results = say("", args.to, who, path=args.path)
        print(said_text(results))
        return 0 if all(ok for _, ok, _ in results) else 1
    if args.cmd == "read":
        print(new_entries(who) or "Nothing new in the book.", end="")
        return 0
    if args.cmd == "wait":
        text = wait_new(who, args.timeout)
        print(text or f"Nothing new in {int(args.timeout)} s.", end="" if text else "\n")
        return 0 if text else 3
    if args.cmd == "book":
        print(last_entries(max(1, args.n)) or "The book is empty.", end="")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
