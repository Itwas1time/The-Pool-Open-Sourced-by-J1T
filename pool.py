"""The Pool: messages, files and notifications between your machines and their agents, over wired Ethernet.

Machines find each other by UDP broadcast on their wired Ethernet ports only
(Wi-Fi, VPN and virtual adapters are never used) and send messages and files
over TCP on that same wire. Code travels through git; the Pool carries the
message that names the repo, branch and commit.

Addresses: AGENT@MACHINE (codex@device1, claude@device4; a unique prefix
works: codex@minas), a bare MACHINE (whichever agent there reads first) or all.

  pool.bat send TO "text" [--subject S] [--git [REPO]]   send a message (text '-' reads stdin)
  pool.bat reply ID "text" [--git [REPO]]                answer a message in the same thread
  pool.bat read                                          print and take my new messages
  pool.bat wait [--timeout SECONDS]                      block until a message arrives, then read it
  pool.bat thread ID                                     the whole conversation
  pool.bat open                                          what I took but have not replied to yet
  pool.bat sendfile TO PATH                              send a file
  pool.bat peers                                         machines, their agents, their status
  pool.bat start | stop | status | serve                 the Pool itself (start = background, no window)
  pool.bat mcp                                           the same as tools for Claude Code and Codex (MCP)
  Add --as NAME (or set POOL_AGENT) to say which agent you are: claude, codex, ...

The "The Pool" shortcut opens the window. One Pool runs per machine; if a
background Pool is running, the window opens as a viewer onto it.

On Windows the Pool runs as runtime\\thepool.exe / thepool-cli.exe (made once by
SETUP_THE_POOL.bat) so local project's firewall block on python.exe is never involved.
On Linux: python3 pool.py [same arguments].

Standard library only. It sleeps between events and runs at below-normal
priority, so it never competes with a game or anything else doing real work.
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
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "pool_config.json"
DATA = HERE / "pool_data"
ASSETS = HERE / "assets"
VERSION = "4"
TASK = "ThePool"           # Windows scheduled task that runs the background Pool (made by setup)

HOST = socket.gethostname().lower()
NAME = HOST                # replaced in load_config() by POOL_NAME or the shared names map
AGENT = os.environ.get("POOL_AGENT", "")
ANNOUNCE_EVERY = 30        # seconds between "I'm here" broadcasts
ONLINE_WINDOW = 90         # a machine counts as online if heard from this recently
MAX_MESSAGE = 20_000_000   # bytes on the wire, about 15 MB of file
STATUS_LIMIT = 500         # characters of my_status.txt shared with the others

KEY = b""
TCP_PORT = 50505
UDP_PORT = 50506
NAMES = {}                 # hostname -> fleet name, from pool_config.json (same file on every machine)
wired = []                 # IPv4Interface list: this machine's connected wired Ethernet ports

peers = {}                 # name -> {"ip", "port", "last_seen", "ram", "status", "agents"}
lock = threading.Lock()
received_count = 0         # bumps on every stored message
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
    if not isinstance(cfg.get("pool_key"), str) or not cfg["pool_key"].strip():
        raise SystemExit("Set a nonempty shared pool_key in private local configuration.")
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


def clean_id(text):
    return "".join(c for c in str(text or "") if c.isalnum())[:16]


def agent_name(text):
    """claude, codex, ...; '' means no particular agent."""
    name = "".join(c for c in str(text or "").lower() if c.isalnum() or c in "-_").strip("-_")[:30]
    return "" if name in ("any", "read") else name


def canonical(name):
    """A machine's fleet name (hostnames are mapped by the shared names table)."""
    name = str(name or "").lower()
    return NAMES.get(name, name)


def addr(agent, machine):
    return f"{agent}@{machine}" if agent else machine


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


# ---- which agents use the Pool here (shared with the other machines in each hello) ----

_active_marked = {}


def mark_active(agent):
    now = time.time()
    if now - _active_marked.get(agent, 0) < 60:
        return
    _active_marked[agent] = now
    path = DATA / "agents.json"
    try:
        agents = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        agents = {}
    agents[agent] = now
    try:
        write_file(path, json.dumps(agents, indent=2).encode())
    except OSError:
        pass


def agent_ages():
    """{agent: seconds since it last read or waited}, for agents active in the last day."""
    try:
        agents = json.loads((DATA / "agents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {a: int(now - float(t)) for a, t in agents.items() if now - float(t) < 86400}


def agents_text(ages):
    return ", ".join(f"{a} {ago_text(s)}" for a, s in sorted((ages or {}).items(), key=lambda x: x[1]))


# ---- who is out there (peers.json is the monitoring file other scripts read) ----

def load_peers():
    try:
        saved = json.loads((DATA / "peers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    with lock:
        for name, info in saved.items():
            name = canonical(name)
            entry = {k: info.get(k) for k in ("ip", "port", "last_seen", "ram", "status", "agents", "version")}
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
    data = pack({"kind": "hello", "from": NAME, "port": TCP_PORT, "ram": ram_percent(), "status": my_status(),
                 "agents": agent_ages(), "version": VERSION})
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
    while alive.is_set():
        announce()
        save_peers()
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
        ages = msg.get("agents") if isinstance(msg.get("agents"), dict) else {}
        try:
            port = int(msg.get("port") or TCP_PORT)
            agents = {agent_name(a): int(s) for a, s in ages.items() if agent_name(a)}
        except (TypeError, ValueError):
            continue
        with lock:
            old = peers.get(name)
            is_new = old is None or old.get("ip") != ip or not is_online(old)
            peers[name] = {"ip": ip, "port": port, "last_seen": time.time(), "ram": msg.get("ram"),
                           "status": str(msg.get("status") or "")[:STATUS_LIMIT], "agents": agents,
                           "version": str(msg.get("version") or "")[:10]}
        save_peers()
        if is_new:
            log(f"{name} is online at {ip}")
            announce(only_to=ip)  # so it learns about us now instead of in 30 s


# ---- receiving: inbox/ keeps everything, queues/<agent>/ says who it is for ----

def header(meta):
    lines = [f"From: {addr(meta.get('from_agent'), meta.get('from'))}",
             f"To: {addr(meta.get('to_agent'), meta.get('to'))}",
             f"Id: {meta.get('id')}", f"Thread: {meta.get('thread')}"]
    if meta.get("reply_to"):
        lines.append(f"Reply-To-Id: {meta['reply_to']}")
    lines.append(f"Sent: {meta.get('sent') or '?'}")
    if meta.get("subject"):
        lines.append(f"Subject: {meta['subject']}")
    if meta.get("git"):
        lines.append("Git: " + "  ".join(f"{k}={v}" for k, v in meta["git"].items()))
    if meta.get("kind") == "file":
        lines.append(f"File: {meta.get('path')}")
    return "\n".join(lines)


def store(msg):
    """Save a received message or file into inbox/, queue it for its agent, add a NEW.txt line."""
    global received_count
    sender = safe(canonical(msg.get("from", "unknown")), 40)
    is_file = msg.get("kind") == "file"
    mid = clean_id(msg.get("id")) or secrets.token_hex(4)
    git = msg.get("git")
    meta = {"id": mid, "thread": clean_id(msg.get("thread")) or mid, "reply_to": clean_id(msg.get("reply_to")),
            "from": sender, "from_agent": agent_name(msg.get("from_agent")),
            "to": NAME, "to_agent": agent_name(msg.get("to_agent")),
            "kind": "file" if is_file else "message", "subject": str(msg.get("subject") or "")[:200],
            "sent": str(msg.get("sent_at") or "")[:30], "received": datetime.now().isoformat(timespec="seconds"),
            "git": {str(k)[:20]: str(v)[:300] for k, v in git.items()} if isinstance(git, dict) else None}
    if is_file:
        data, label = base64.b64decode(msg.get("data", "")), safe(msg.get("name", "file"))
    else:
        data, label = None, f"{mid}.md"
    with lock:
        if msg.get("id"):  # a sender retrying after a timeout must not create a second copy
            for earlier in (DATA / "queues").glob(f"*/*_{mid}.json"):
                log(f"duplicate {mid} from {sender} ignored (already stored)")
                try:
                    return json.loads(earlier.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return meta
        (DATA / "inbox").mkdir(parents=True, exist_ok=True)
        while True:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            path = DATA / "inbox" / f"{stamp}_from-{sender}_{label}"
            if not path.exists():
                break
            time.sleep(0.002)
        meta["path"] = str(path)
        if data is None:
            data = (header(meta) + "\n\n" + str(msg.get("text", ""))).encode("utf-8")
        write_file(path, data)
        queue = DATA / "queues" / (meta["to_agent"] or "any")
        queue.mkdir(parents=True, exist_ok=True)
        write_file(queue / f"{stamp}_{mid}.json", json.dumps(meta, indent=2).encode())
        for attempt in range(5):
            try:
                with open(DATA / "NEW.txt", "a", encoding="utf-8") as f:
                    f.write(f"{stamp}\t{sender}\t{meta['kind']}\t{path}\n")
                break
            except PermissionError:
                time.sleep(0.1)
        received_count += 1
    log(f"got {meta['kind']} {mid} from {addr(meta['from_agent'], sender)} for {meta['to_agent'] or 'any agent'}")
    return meta


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
            if server.fileno() == -1:
                return  # closed by finish_node()
            time.sleep(1)
            continue
        threading.Thread(target=handle, args=(conn, ip), daemon=True).start()


# ---- sending ----

def resolve_machine(machine):
    """Fleet name for a machine; a unique prefix works (minas -> device1)."""
    machine = canonical(machine)
    if machine in ("all", NAME):
        return machine
    with lock:
        known = list(peers)
    if machine in known:
        return machine
    matches = [n for n in known + [NAME] if n.startswith(machine)]
    return matches[0] if len(matches) == 1 else machine


def split_address(to):
    """'codex@minas' -> ('codex', 'device1'); 'minas' -> ('', 'device1')."""
    agent, _, machine = str(to).strip().lower().rpartition("@")
    return agent_name(agent), resolve_machine(machine)


def send(target, body):
    """Send one message to a machine (or 'all' online ones). Returns [(machine, ok, detail)]."""
    body = dict(body, **{"from": NAME, "sent_at": datetime.now().isoformat(timespec="seconds")})
    data = pack(body)
    if len(data) > MAX_MESSAGE:
        return [(target, False, "too big (limit is about 15 MB)")]
    if target == NAME:
        store(body)  # an agent on this same machine: no network needed
        return [(NAME, True, "OK")]
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
        log(f"sent {body.get('kind')} {body.get('id', '')} to {name}: {'OK' if ok else detail}")
    return results


def file_body(path):
    path = Path(path)
    return {"kind": "file", "name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}


def git_info(repo):
    """Repo, branch and commit to put on a message, so the receiver fetches exactly that code."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else ""
    commit = git("rev-parse", "HEAD")
    if not commit:
        raise ValueError(f"{repo} is not a git repository")
    info = {"repo": git("config", "--get", "remote.origin.url") or str(Path(repo).resolve()),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": commit,
            "worktree": f"{NAME}:{git('rev-parse', '--show-toplevel')}"}
    if git("status", "--porcelain", "--untracked-files=no"):
        info["uncommitted"] = "yes"
    if not git("branch", "-r", "--contains", commit):
        info["pushed"] = "no"
    return info


def git_warnings(git):
    notes = []
    if git and git.get("pushed") == "no":
        notes.append("warning: that commit is not pushed, so the other machine cannot fetch it")
    if git and git.get("uncommitted") == "yes":
        notes.append("warning: the repo has uncommitted changes that are not in that commit")
    return notes


def record_sent(body, to, results):
    rec = {k: v for k, v in body.items() if k != "data"}
    rec.update(to=to, sent=datetime.now().isoformat(timespec="seconds"),
               results=[[n, ok, d] for n, ok, d in results])
    try:
        (DATA / "sent").mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        write_file(DATA / "sent" / f"{stamp}_{body['id']}.json", json.dumps(rec, indent=2).encode())
    except OSError:
        pass


def send_message(to, text="", subject="", reply_to="", thread="", agent="", git=None, path=None):
    """Send a message (or a file, with path) to AGENT@MACHINE, MACHINE or all. Returns (id, results)."""
    to_agent, machine = split_address(to)
    mid = secrets.token_hex(4)
    body = {"kind": "message", "id": mid, "thread": thread or mid, "reply_to": reply_to,
            "from_agent": agent_name(agent), "to_agent": to_agent, "subject": subject, "text": text}
    if git:
        body["git"] = git
    if path:
        body.update(file_body(path))
    results = send(machine, body)
    record_sent(body, addr(to_agent, machine), results)
    return mid, results


def sent_report(mid, results, git=None):
    lines = [f"id {mid}: " + "   ".join(f"{n} {'delivered' if ok else 'FAILED - ' + d}" for n, ok, d in results)]
    return "\n".join(lines + git_warnings(git))


# ---- reading: each message is taken once, by one agent ----

def take(agent):
    """Claim and return this agent's new messages: its own queue, then anything for 'any agent'."""
    agent = agent_name(agent) or "agent"
    queues = DATA / "queues"
    mine, done = queues / agent, queues / "_read"
    mine.mkdir(parents=True, exist_ok=True)
    done.mkdir(parents=True, exist_ok=True)
    mark_active(agent)
    shared = queues / "any"
    for p in sorted(shared.glob("*.json")) if shared.exists() else []:
        try:
            os.replace(p, mine / p.name)  # atomic: if two agents race, one gets it
        except OSError:
            pass
    taken = []
    for p in sorted(mine.glob("*.json")):
        claimed = done / p.name
        try:
            os.replace(p, claimed)  # atomic: only one reader ever gets a message
            meta = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta["read_by"] = agent
        try:
            write_file(claimed, json.dumps(meta, indent=2).encode())
        except OSError:
            pass
        taken.append(meta)
    return taken


def wait_for(agent, timeout):
    end = time.time() + max(0.0, float(timeout))
    while True:
        got = take(agent)
        if got or time.time() >= end:
            return got
        time.sleep(2)


def render(meta):
    if meta.get("kind") == "file":
        return header(meta)
    try:
        return Path(meta["path"]).read_text(encoding="utf-8", errors="replace")
    except (OSError, KeyError):
        return header(meta) + "\n\n(message file missing)"


def render_many(messages, agent):
    if not messages:
        return f"No new messages for {addr(agent_name(agent) or 'agent', NAME)}."
    parts = [f"=== {i} of {len(messages)} ===\n{render(m)}" for i, m in enumerate(messages, 1)]
    return "\n\n".join(parts) + "\n\n(answer with: reply <Id> \"text\")"


def find_message(mid):
    mid = clean_id(mid)
    for p in (DATA / "queues").glob(f"*/*_{mid}.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return None


def reply_message(mid, text, agent="", git=None):
    meta = find_message(mid)
    if not meta:
        return mid, [(mid, False, "no received message with that id")]
    subject = meta.get("subject") or ""
    if subject and not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    return send_message(addr(meta.get("from_agent"), meta.get("from")), text, subject=subject,
                        reply_to=meta["id"], thread=meta.get("thread") or meta["id"], agent=agent, git=git)


def open_work(agent):
    """Messages this agent took but has not replied to yet: what to pick back up after a restart."""
    agent = agent_name(agent) or "agent"
    answered = set()
    for p in (DATA / "sent").glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("from_agent") == agent and rec.get("reply_to"):
            answered.add(rec["reply_to"])
    rows = []
    for p in sorted((DATA / "queues" / "_read").glob("*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("read_by") == agent and meta.get("id") not in answered:
            rows.append(f"{meta.get('id')}  from {addr(meta.get('from_agent'), meta.get('from'))}  "
                        f"thread {meta.get('thread')}  sent {meta.get('sent')}  {meta.get('subject') or '(no subject)'}")
    if not rows:
        return f"Nothing open for {addr(agent, NAME)}: every message you took has a reply."
    return f"Taken by {addr(agent, NAME)} and not replied to yet:\n" + "\n".join(rows) + "\n(see one with: thread <Id>)"


def thread_text(mid):
    mid = clean_id(mid)
    items = []
    for p in (DATA / "queues").glob("*/*.json"):
        try:
            items.append(("in", json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    for p in (DATA / "sent").glob("*.json"):
        try:
            items.append(("out", json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    tid = next((m.get("thread") for _, m in items if m.get("id") == mid), mid)
    chosen = sorted(((d, m) for d, m in items if m.get("thread") == tid), key=lambda x: x[1].get("sent") or "")
    if not chosen:
        return f"No messages found for {mid}."
    out = []
    for direction, m in chosen:
        if direction == "in":
            out.append(render(m))
        else:
            meta = dict(m, **{"from": NAME, "to_agent": "", "path": m.get("name")})
            delivered = "   ".join(f"{n} {'delivered' if ok else 'FAILED - ' + str(d)}" for n, ok, d in m.get("results", []))
            out.append(header(meta) + f"\nDelivered: {delivered}\n\n{m.get('text', '')}")
    return f"Thread {tid}: {len(chosen)} message(s)\n\n" + "\n\n----\n\n".join(out)


def peers_text():
    now = time.time()
    rows = []
    with lock:
        items = sorted(peers.items())
    for name, info in items:
        ago = now - (info.get("last_seen") or 0)
        state = "online " if ago < ONLINE_WINDOW else "offline"
        status = next(iter((info.get("status") or "").splitlines()), "")
        rows.append(f"{name:16} {state}  v{info.get('version') or '<4'}  {info.get('ip') or '?':16} "
                    f"seen {ago_text(ago):>8}   ram {info.get('ram')}%"
                    f"   agents: {agents_text(info.get('agents')) or 'none'}" + (f"   status: {status}" if status else ""))
    rows.append(f"{NAME:16} (this machine)  v{VERSION}   agents: {agents_text(agent_ages()) or 'none'}")
    return "\n".join(rows)


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


def prepare_to_send():
    """A command-line or MCP sender borrows the running Pool's view of the wire and the machines."""
    global wired
    DATA.mkdir(parents=True, exist_ok=True)
    load_peers()
    wired = (wired_from_node() if node_running() else []) or find_wired()


def stop_requested():
    return (DATA / "STOP").exists()


def start_node(mode):
    global wired, node_mode, started_at
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
    print(f"The Pool is running as {NAME} with no window (Ctrl+C or 'pool.bat stop' ends it). Data: {DATA}")
    try:
        while not stop_requested():
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finish_node()


def start_background():
    """Start a windowless Pool that outlives the caller (Task Scheduler on Windows, systemd/setsid on Linux)."""
    if node_running():
        return 0, f"The Pool is already running on {NAME} ({read_node().get('mode', '?')})."
    if os.name == "nt":
        result = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            return 1, f"The '{TASK}' scheduled task is missing: run SETUP_THE_POOL.bat once."
    else:
        cmd = [sys.executable, str(HERE / "pool.py"), "serve"]
        if shutil.which("systemd-run"):
            subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--unit", "thepool", *cmd])
        else:
            subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        if node_running():
            return 0, f"The Pool is running in the background on {NAME}."
    return 1, f"It did not start - see {DATA / 'log.txt'}"


def ensure_node():
    """Receiving needs the Pool running here; start it in the background if it is not."""
    return "" if node_running() else start_background()[1]


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
    queues = DATA / "queues"
    waiting = {q.name: len(list(q.glob("*.json"))) for q in queues.iterdir() if q.is_dir() and q.name != "_read"} \
        if queues.exists() else {}
    lines = [f"The Pool on {NAME}: running ({node.get('mode', '?')}, pid {node.get('pid', '?')}, "
             f"since {node.get('started', '?')})" if running else f"The Pool on {NAME}: not running",
             f"wired port: {', '.join(ports or []) or 'none - plug in a network cable'}",
             f"machines online: {', '.join(online) or 'none'}",
             "unread: " + (", ".join(f"{q} {n}" for q, n in sorted(waiting.items()) if n) or "none")]
    return (0 if running else 1), "\n".join(lines)


def open_path(path):
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


def count_new():
    try:
        with open(DATA / "NEW.txt", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


# ---- MCP server: the same commands as native tools in Claude Code and Codex ----

def pool_command():
    return str(HERE / "pool.bat") if os.name == "nt" else f"python3 {HERE / 'pool.py'}"


def agent_guide(agent):
    me = addr(agent_name(agent) or "agent", NAME)
    return (f"The Pool links operator's machines over a wired cable. You are {me}. Use it to hand work to agents on "
            "other machines and to get their results back.\n"
            "- peers: which machines are online and which agents are active on them.\n"
            "- send to agent@machine (codex@device1, claude@device4; a unique prefix like codex@minas works), "
            "a bare machine (whichever agent there reads first) or all.\n"
            "- Code never travels in messages: commit and push, then send with git_repo so the message names repo, "
            "branch and commit; the receiver fetches exactly that commit.\n"
            "- A task message says what to do, where the code is, and what to report back. The receiver answers "
            "with reply (same thread) when done or blocked.\n"
            "- read returns your new messages; wait blocks until one arrives. A message is taken once: after you "
            "read it, it is yours to handle. After a restart, open lists what you took but have not replied to.\n"
            "- A claim counts only once the task's sender (or the other instance) says yes in the thread; until "
            "then no overlapping edits or heavy jobs. 'delivered' only means stored: a task is done when its "
            "thread says so.\n"
            f"- In Claude Code, for long waits run in the background: {pool_command()} wait --as "
            f"{agent_name(agent) or 'agent'} --timeout 3600 (you are notified when it exits).\n"
            "- Never put secrets in messages.")


def mcp_tools():
    s, n = {"type": "string"}, {"type": "number"}

    def tool(name, description, props, required):
        return {"name": name, "description": description,
                "inputSchema": {"type": "object", "properties": props, "required": required}}
    return [
        tool("send", "Send a message to an agent on another machine (or this one). to = agent@machine "
             "(codex@device1; a unique prefix like codex@minas works), a bare machine (any agent there) or "
             "'all'. Code goes through git: pass git_repo (a local repo path) so the message carries repo, branch "
             "and commit.", {"to": s, "text": s, "subject": s, "git_repo": s}, ["to", "text"]),
        tool("reply", "Answer a received message by its Id: same thread, back to the agent that sent it.",
             {"id": s, "text": s, "git_repo": s}, ["id", "text"]),
        tool("read", "Take and return your new messages (each message is taken once, by one agent).", {}, []),
        tool("wait", "Block until a message for you arrives or timeout_seconds pass (default 50, max 110), "
             "then return it.", {"timeout_seconds": n}, []),
        tool("peers", "Machines in the pool: online or not, wired address, RAM use, active agents, status.", {}, []),
        tool("thread", "The whole conversation (sent and received) that a message Id belongs to.", {"id": s}, ["id"]),
        tool("open", "Messages you took but have not replied to yet: check after a restart to pick work back up.",
             {}, []),
        tool("send_file", "Send a file to an agent or machine; it lands in their inbox and they are notified.",
             {"to": s, "path": s, "subject": s}, ["to", "path"]),
        tool("status", "Whether the Pool runs on this machine, its wired port, who is online, unread counts.", {}, []),
    ]


def call_tool(name, args, agent):
    """Returns (text, is_error)."""
    started = ensure_node()
    prefix = (started + "\n") if started else ""
    if name in ("send", "reply", "send_file"):
        prepare_to_send()
        git = git_info(args["git_repo"]) if args.get("git_repo") else None
        if name == "send":
            mid, results = send_message(args["to"], args["text"], args.get("subject", ""), agent=agent, git=git)
        elif name == "reply":
            mid, results = reply_message(args["id"], args["text"], agent=agent, git=git)
        else:
            mid, results = send_message(args["to"], args.get("subject", ""), args.get("subject", ""),
                                        agent=agent, path=args["path"])
        return prefix + sent_report(mid, results, git), not any(ok for _, ok, _ in results)
    if name == "read":
        return prefix + render_many(take(agent), agent), False
    if name == "wait":
        timeout = min(max(float(args.get("timeout_seconds") or 50), 0), 110)
        got = wait_for(agent, timeout)
        return prefix + (render_many(got, agent) if got else f"Nothing arrived in {int(timeout)} s."), False
    if name == "peers":
        load_peers()
        return prefix + peers_text(), False
    if name == "thread":
        return prefix + thread_text(args["id"]), False
    if name == "open":
        return prefix + open_work(agent), False
    if name == "status":
        return prefix + show_status()[1], False
    return f"unknown tool {name}", True


def mcp_serve(agent):
    """MCP over stdio: one JSON-RPC message per line. Tool calls run in threads so a wait never blocks others."""
    sys.stdin.reconfigure(encoding="utf-8")
    out = sys.stdout
    out.reconfigure(encoding="utf-8")
    write_lock = threading.Lock()

    def respond(message):
        with write_lock:
            out.write(json.dumps(message) + "\n")
            out.flush()

    def run_tool(rid, params):
        try:
            text, is_error = call_tool(params.get("name"), params.get("arguments") or {}, agent)
        except Exception as e:
            text, is_error = f"{type(e).__name__}: {e}", True
        respond({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}],
                                                          "isError": is_error}})

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        rid, method, params = request.get("id"), request.get("method"), request.get("params") or {}
        if rid is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            respond({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "the-pool", "version": VERSION},
                "instructions": agent_guide(agent)}})
        elif method == "tools/list":
            respond({"jsonrpc": "2.0", "id": rid, "result": {"tools": mcp_tools()}})
        elif method == "tools/call":
            threading.Thread(target=run_tool, args=(rid, params), daemon=True).start()
        elif method == "ping":
            respond({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            respond({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


# ---- the window: dark, with the painting as icon and banner ----

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
                    selectbackground=c["sel"], selectforeground=c["fg"], insertcolor=c["fg"],
                    font=("Segoe UI", 10))
    style.configure("Treeview", background=c["panel"], fieldbackground=c["panel"], foreground=c["fg"],
                    rowheight=26, borderwidth=0)
    style.map("Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["fg"])])
    style.configure("Treeview.Heading", background=c["field"], foreground=c["muted"], relief="flat",
                    font=("Segoe UI Semibold", 9))
    style.map("Treeview.Heading", background=[("active", c["sel"])])
    style.configure("TButton", background=c["sel"], foreground=c["fg"], padding=(14, 5), relief="flat",
                    borderwidth=0)
    style.map("TButton", background=[("pressed", c["gold"]), ("active", "#5a4885")],
              foreground=[("pressed", c["bg"])])
    style.configure("TCombobox", fieldbackground=c["field"], background=c["field"], foreground=c["fg"],
                    arrowcolor=c["gold"], padding=4, insertcolor=c["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["sel"])
    style.configure("Section.TLabel", foreground=c["gold"], font=("Segoe UI Semibold", 9))
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
    root.geometry("980x720")
    root.minsize(780, 580)
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

    body = ttk.Frame(root, padding=(18, 2, 18, 14))
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="MACHINES", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
    machines = ttk.Treeview(body, columns=("ip", "seen", "ram", "agents", "status"), height=5)
    for col, title, width in (("#0", "Machine", 150), ("ip", "Address", 125), ("seen", "Last seen", 120),
                              ("ram", "RAM", 60), ("agents", "Agents", 200), ("status", "Status", 260)):
        machines.heading(col, text=title, anchor="w")
        machines.column(col, width=width, stretch=(col == "status"), anchor="w")
    machines.tag_configure("online", foreground=c["blue"])
    machines.tag_configure("offline", foreground=c["muted"])
    machines.pack(fill="x")

    ttk.Label(body, text="INBOX   newest first, double-click to open", style="Section.TLabel").pack(
        anchor="w", pady=(14, 4))
    inbox = tk.Listbox(body, height=9, bg=c["panel"], fg=c["fg"], selectbackground=c["sel"],
                       selectforeground=c["fg"], highlightthickness=0, borderwidth=0, activestyle="none",
                       font=("Segoe UI", 10))
    inbox.pack(fill="both", expand=True)
    inbox.bind("<Double-Button-1>", lambda _e: inbox.curselection() and
               open_path(DATA / "inbox" / inbox.get(inbox.curselection()[0])))

    ttk.Label(body, text="SEND   to a machine, or agent@machine", style="Section.TLabel").pack(anchor="w", pady=(14, 4))
    compose = ttk.Frame(body)
    compose.pack(fill="x")
    ttk.Label(compose, text="To").grid(row=0, column=0, sticky="w")
    target = ttk.Combobox(compose, width=28)
    target.grid(row=0, column=1, sticky="w", padx=8)
    text = tk.Text(compose, height=4, wrap="word", bg=c["field"], fg=c["fg"], insertbackground=c["gold"],
                   relief="flat", highlightthickness=1, highlightbackground=c["line"], highlightcolor=c["gold"],
                   padx=10, pady=8, font=("Segoe UI", 10))
    text.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
    compose.columnconfigure(4, weight=1)
    note = ttk.Label(body, text="", style="Muted.TLabel")
    note.pack(anchor="w", pady=(8, 0))
    outcome = {"text": "", "clear": False}  # written by send threads, shown by refresh()

    def send_in_background(what, **kwargs):
        who = target.get().strip()
        if not who:
            note.config(text="Pick a machine first (or type agent@machine).")
            return
        note.config(text=f"Sending {what} to {who}...")

        def work():
            mid, results = send_message(who, agent="operator", **kwargs)
            outcome["clear"] = "path" not in kwargs and all(ok for _, ok, _ in results)
            outcome["text"] = sent_report(mid, results)
        threading.Thread(target=work, daemon=True).start()

    def send_prompt():
        message = text.get("1.0", "end").strip()
        if message:
            send_in_background("message", text=message)

    def send_file():
        path = filedialog.askopenfilename(parent=root)
        if path:
            send_in_background(Path(path).name, path=path)

    ttk.Button(compose, text="Send message", command=send_prompt).grid(row=0, column=2, padx=(0, 6))
    ttk.Button(compose, text="Send file...", command=send_file).grid(row=0, column=3)

    def close():
        if not viewer:
            finish_node()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    shown = {"inbox": None, "targets": None}

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
        machines.delete(*machines.get_children())
        for name, info in rows:
            ago = now - (info.get("last_seen") or 0)
            online = ago < ONLINE_WINDOW
            ram = f"{info['ram']}%" if info.get("ram") is not None else "?"
            machines.insert("", "end", text=name, tags=("online" if online else "offline",),
                            values=(info.get("ip"), ago_text(ago) if online else f"offline ({ago_text(ago)})", ram,
                                    agents_text(info.get("agents")), (info.get("status") or "").replace("\n", " | ")))
        targets = ("all", *(name for name, _ in rows),
                   *(f"{a}@{name}" for name, info in rows for a in sorted(info.get("agents") or {})))
        if targets != shown["targets"]:
            shown["targets"] = targets
            target["values"] = targets
        try:
            stamp = (DATA / "inbox").stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != shown["inbox"]:
            shown["inbox"] = stamp
            inbox.delete(0, "end")
            for name in sorted(os.listdir(DATA / "inbox"), reverse=True)[:300] if stamp else []:
                inbox.insert("end", name)
        waiting = count_new()
        root.title(f"The Pool - {NAME}" + (f"   ({waiting} waiting in NEW.txt)" if waiting else ""))
        if outcome["text"]:
            note.config(text=outcome["text"])
            if outcome["clear"]:
                text.delete("1.0", "end")
            outcome.update(text="", clear=False)
        root.after(2000, refresh)

    refresh()
    root.mainloop()


# ---- command line ----

def main(argv):
    import argparse
    load_config()
    if not argv or argv[0] == "gui":
        run_gui()
        return 0
    ap = argparse.ArgumentParser(prog="pool", description="The Pool: messages between your machines' agents.")
    ap.add_argument("--as", dest="agent", default=AGENT, help="which agent you are: claude, codex, ... ($POOL_AGENT)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name, *args):
        p = sub.add_parser(name)
        p.add_argument("--as", dest="agent", default=argparse.SUPPRESS)
        for a in args:
            p.add_argument(a)
        return p
    p = command("send", "to", "text")
    p.add_argument("--subject", default="")
    p.add_argument("--git", nargs="?", const=".", default=None, metavar="REPO")
    p = command("reply", "id", "text")
    p.add_argument("--git", nargs="?", const=".", default=None, metavar="REPO")
    command("read")
    command("wait").add_argument("--timeout", type=float, default=600)
    command("thread", "id")
    command("open")
    command("sendfile", "to", "path").add_argument("--subject", default="")
    for name in ("peers", "start", "stop", "status", "serve", "mcp"):
        command(name)
    args = ap.parse_args(argv)
    agent = args.agent

    if args.cmd == "serve":
        serve_forever()
        return 0
    if args.cmd == "mcp":
        mcp_serve(agent)
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
    if args.cmd in ("send", "reply", "sendfile"):
        prepare_to_send()
        git = None
        if getattr(args, "git", None):
            try:
                git = git_info(args.git)
            except ValueError as e:
                print(e)
                return 2
        if args.cmd == "send":
            message = sys.stdin.read() if args.text == "-" else args.text
            mid, results = send_message(args.to, message, args.subject, agent=agent, git=git)
        elif args.cmd == "reply":
            message = sys.stdin.read() if args.text == "-" else args.text
            mid, results = reply_message(args.id, message, agent=agent, git=git)
        else:
            mid, results = send_message(args.to, args.subject, args.subject, agent=agent, path=args.path)
        print(sent_report(mid, results, git))
        return 0 if results and all(ok for _, ok, _ in results) else 1
    started = ensure_node()
    if started:
        print(started)
    if args.cmd == "read":
        print(render_many(take(agent), agent))
        return 0
    if args.cmd == "wait":
        got = wait_for(agent, args.timeout)
        print(render_many(got, agent) if got else f"Nothing arrived in {int(args.timeout)} s.")
        return 0 if got else 3
    if args.cmd == "thread":
        print(thread_text(args.id))
        return 0
    if args.cmd == "open":
        print(open_work(agent))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
