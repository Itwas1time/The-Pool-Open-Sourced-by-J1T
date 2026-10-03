#!/usr/bin/env python3
"""Local outage watcher for trusted personal Pool devices. See outage/README.md."""
import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0
DOCKET = Path(r"C:\tmp\docket")
HOME = Path(os.environ.get("OUTAGE_HOME") or (DOCKET / "outage" if WIN else Path.home() / "outage"))
POOL_DIR = Path(os.environ.get("POOL_DIR") or (str(Path.home() / "ThePool") if WIN else Path.home() / "ThePool"))
PROBE_URLS = ["https://github.com", "https://api.anthropic.com", "https://api.openai.com"]
CURL = r"C:\Windows\System32\curl.exe" if WIN else "curl"
SOL_LAUNCH = DOCKET / "sol_launch_v3.cmd"
CLAUDE_EXE_MATCH = r"\.local\bin\claude.exe"
POOL_AS = "outage"
TRIGGER = "REBIRTH"
DRY = False
POOL_SELF = False
MACHINE = ""


# ---------------------------------------------------------------- small helpers

def now():
    return datetime.now()


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def parse_time(text, base=None):
    """ISO, 'HH:MM' (next occurrence) or 'now'."""
    text = str(text or "").strip()
    if not text or text == "now":
        return now()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        h, m = map(int, text.split(":"))
        t = (base or now()).replace(hour=h, minute=m, second=0, microsecond=0)
        return t if t > (base or now()) else t + timedelta(days=1)
    if text.startswith("+") and text[-1] in "smh":
        n = float(text[1:-1])
        return now() + timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600}[text[-1]])
    return datetime.fromisoformat(text)


def p(name):
    return HOME / name


def log(line):
    text = f"[{iso()}] {'DRY ' if DRY else ''}{line}"
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(p("watch.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(text, flush=True)
        except Exception:
            pass


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    return read_json(p("state.json"), {"phase": "idle"})


def save_state(st):
    write_json(p("state.json"), st)


def machine_name():
    cfg = read_json(POOL_DIR / "pool_config.json", {})
    host = socket.gethostname().lower()
    return (cfg.get("names") or {}).get(host, host)


def fleet_machines():
    cfg = read_json(POOL_DIR / "pool_config.json", {})
    return set((cfg.get("names") or {}).values()) or {"device4", "device1", "device3", "device2"}


def boot_time():
    try:
        if WIN:
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0
        for line in open("/proc/stat", encoding="ascii"):
            if line.startswith("btime"):
                return float(line.split()[1])
    except Exception:
        pass
    return 0.0


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- the Pool

def pool_argv(*args):
    if WIN:
        return [str(POOL_DIR / "runtime" / "thepool-cli.exe"), str(POOL_DIR / "pool.py"), *args]
    return [sys.executable or "python3", str(POOL_DIR / "pool.py"), *args]


def pool_running():
    try:
        r = subprocess.run(pool_argv("status"), capture_output=True, text=True, timeout=60, creationflags=NOWIN)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_pool():
    """The Pool is the rebirth's voice: after a reboot it may be down. Start it only the sanctioned way."""
    if pool_running():
        return True
    if DRY:
        log("WOULD START the Pool: pool start (it is not running)")
        return False
    try:
        r = subprocess.run(pool_argv("start"), capture_output=True, text=True, timeout=120, creationflags=NOWIN)
        log(f"pool start rc={r.returncode}: {(r.stdout or r.stderr).strip()[:200]}")
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log(f"pool start failed: {e}")
        return False


def pool_say(text, to=""):
    """One line in the book. Dry run (or --pool-self): only to outage@<this machine> (own book, wakes nobody)."""
    if DRY or POOL_SELF:
        text, to = (f"{'DRY-RUN' if DRY else 'TEST'} (would go to {to or 'the whole pool'}): {text}",
                    f"{POOL_AS}@{MACHINE}")
    argv = pool_argv("--as", POOL_AS, "say", text) + (["--to", to] if to else [])
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for attempt in range(3):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env, creationflags=NOWIN)
            out = (r.stdout or r.stderr).strip().replace("\n", " ")[:300]
            log(f"pool say -> {to or 'pool'} rc={r.returncode}: {out}")
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError) as e:
            log(f"pool say failed: {e}")
        time.sleep(10)
    return False


def book_trigger(st):
    """Scan new book entries (read-only, own offset) for 'REBIRTH <machine>' / 'REBIRTH all' from a fleet writer,
    written at or after armed_from. Returns the author or ''."""
    book = POOL_DIR / "pool_data" / "pool.txt"
    try:
        size = book.stat().st_size
    except OSError:
        return ""
    off = int(st.get("book_offset") or 0)
    if off > size:  # the book was pruned
        off = 0
    if size == off:
        return ""
    with open(book, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    data = data[:data.rfind(b"\n") + 1]
    st["book_offset"] = off + len(data)
    since = st.get("armed_from") or ""
    fleet = fleet_machines()
    entry = []
    found = ""
    for line in data.decode("utf-8", "replace").splitlines() + ["["]:
        if line.startswith("[") and entry:
            head, body = entry[0], [x[2:] if x.startswith("  ") else x for x in entry[1:]]
            m = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ([a-z0-9_-]+)@([a-z0-9_.-]+)", head)
            first = (body[0].strip() if body else "")
            if m and first in (f"{TRIGGER} {MACHINE}", f"{TRIGGER} all"):
                stamp, who, mach = m.group(1).replace(" ", "T"), m.group(2), m.group(3)
                if mach in fleet and stamp >= since[:19] and not (who == POOL_AS and mach == MACHINE):
                    found = f"{who}@{mach} at {stamp}"
            entry = []
        if line.startswith("[") or entry:
            entry.append(line)
    return found


def relaunch_triggers(st):
    """Read new complete Pool entries. The book supplies only an authorized trigger, never an argv."""
    book = POOL_DIR / "pool_data" / "pool.txt"
    try:
        size = book.stat().st_size
    except OSError:
        return [], int(st.get("relaunch_offset") or 0)
    off = int(st.get("relaunch_offset") or 0)
    if off > size:  # pruned book
        off = 0
    if off == size:
        return [], off
    with open(book, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    data = data[:data.rfind(b"\n") + 1]
    entries = []
    entry = []
    entry_start = off
    pos = off
    for raw in data.splitlines(keepends=True) + [b"["]:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("[") and entry:
            if len(entry) == 1 and raw == b"[":
                return entries, entry_start  # a header written before its body; reread it next time
            head = entry[0]
            first = entry[1][2:].strip() if len(entry) > 1 and entry[1].startswith("  ") else ""
            match = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ([a-z0-9_-]+)@([a-z0-9_.-]+)", head)
            if (match and first == f"RELAUNCH {MACHINE}"
                    and (match.group(2), match.group(3)) in (("operator", "device4"), ("claude", "device4"))):
                identity = hashlib.sha256((head + "\n" + first).encode()).hexdigest()[:16]
                entries.append((identity, f"{match.group(2)}@{match.group(3)}"))
            entry = []
        if line.startswith("[") or entry:
            if not entry:
                entry_start = pos
            entry.append(line)
        pos += len(raw)
    return entries, off + len(data)


# ---------------------------------------------------------------- the judged-run rule (device4)

RUN_ACTIVE_FILE = Path(os.environ.get("OUTAGE_RUN_ACTIVE") or (DOCKET / "RUN_ACTIVE"))


def run_active():
    """Operator box rule: while C:/tmp/docket/RUN_ACTIVE exists a judged game runs and NOTHING is launched on device4.
    In-process only (no child process): the flag counts while its run dir (bughunt/run_<n>) was written in the last
    10 min, or, if the run dir is unknown, while the flag is younger than 6 h. A flag left behind after the run ended
    (a deaf operator cannot remove it) does not hold the rebirth forever. Returns '' or the reason."""
    if not WIN:
        return ""
    try:
        text = RUN_ACTIVE_FILE.read_text(encoding="utf-8", errors="replace")
        age = time.time() - RUN_ACTIVE_FILE.stat().st_mtime
    except OSError:
        return ""
    m = re.search(r"run[ _](\d+)", text)
    d = DOCKET / "bughunt" / f"run_{m.group(1)}" if m else None
    if d is not None and d.is_dir():
        try:
            newest = max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=0.0)
        except OSError:
            newest = time.time()
        if time.time() - newest < 600:
            return f"judged run alive ({text.strip()[:80]}; {d.name} written {int(time.time() - newest)} s ago)"
        return ""
    return f"RUN_ACTIVE present ({text.strip()[:80]}), run dir unknown" if age < 6 * 3600 else ""


# ---------------------------------------------------------------- probes

def connectivity_hint():
    """Windows' own verdict (NCSI), read in-process: True = internet access. No child process, no socket of ours."""
    import ctypes

    class Hint(ctypes.Structure):
        _fields_ = [("level", ctypes.c_int), ("cost", ctypes.c_int), ("near_limit", ctypes.c_ubyte),
                    ("over_limit", ctypes.c_ubyte), ("roaming", ctypes.c_ubyte)]
    h = Hint()
    if ctypes.windll.iphlpapi.GetNetworkConnectivityHint(ctypes.byref(h)) != 0:
        return None
    return h.level in (3, 4)  # internet access, constrained internet access


def probe(probe_file=None):
    """True when the internet answers: any HTTP status from any probe URL (HEAD, no secrets). While a judged run is
    live on device4, Windows' own connectivity verdict is read in-process instead (the box rule: launch nothing)."""
    if probe_file:
        try:
            return Path(probe_file).read_text(encoding="utf-8").strip().lower() == "up"
        except OSError:
            return False
    if WIN and run_active():
        try:
            hint = connectivity_hint()
            if hint is not None:
                return hint
        except Exception:
            pass
    for url in PROBE_URLS:
        try:
            r = subprocess.run([CURL, "-s", "-o", os.devnull, "-I", "--max-time", "10", "-w", "%{http_code}", url],
                               capture_output=True, text=True, timeout=20, creationflags=NOWIN)
            code = (r.stdout or "").strip()
            if code.isdigit() and code != "000":
                return True
        except FileNotFoundError:
            import urllib.request
            try:
                urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10)
                return True
            except Exception as e:
                if getattr(e, "code", None):
                    return True  # an HTTP error is still an answer
        except (OSError, subprocess.SubprocessError):
            pass
    return False


# ---------------------------------------------------------------- device4 process views

def win_processes(name, match):
    """[(pid, created, commandline)] of processes called `name` whose command line matches regex `match`."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, "
          "$_.CreationDate.ToString('s'), ($_.CommandLine -replace '[\\r\\n|]', ' ') }" % name)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=90, creationflags=NOWIN)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"process list failed: {e}")
        return None
    rows = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].strip().isdigit() and re.search(match, parts[2], re.I):
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def sol_lanes_alive():
    """Sol lanes alive now: {name: entry} parsed from their launcher's command line."""
    if not WIN:
        return {}
    rows = win_processes("cmd.exe", r"sol_launch_v3\.cmd")
    if rows is None:
        return None
    lanes = {}
    for pid, created, cmd in rows:
        e = parse_launcher(cmd)
        if e:
            e.update(launcher_pid=pid, launcher_created=created)
            lanes[e["name"]] = e
    return lanes


def parse_launcher(cmd):
    """A lane entry from a launcher command line: ... sol_launch_v3.cmd <prompt> <logbase> [deliverable]."""
    tail = re.split(r"sol_launch_v3\.cmd\"?", cmd, maxsplit=1, flags=re.I)[-1]
    try:
        args = [a.strip('"') for a in shlex.split(tail, posix=False)]
    except ValueError:
        args = tail.split()
    if len(args) < 2:
        return None
    name = Path(args[1].replace("/", "\\") if WIN else args[1]).name
    return {"name": name, "kind": "sol_lane", "prompt": args[0], "logbase": args[1],
            "deliverable": args[2] if len(args) > 2 else "", "source": "auto"}


def claude_sessions():
    """Interactive Claude Code sessions alive now (not -p/--print)."""
    if not WIN:
        return []
    rows = win_processes("claude.exe", ".")
    if rows is None:
        return None
    out = []
    for pid, created, cmd in rows:
        if CLAUDE_EXE_MATCH.lower() not in cmd.lower():
            continue  # the Claude desktop app is also claude.exe; only Claude Code CLI sessions count
        if re.search(r"(^|\s)(-p|--print)(\s|$)", cmd):
            continue
        out.append({"pid": pid, "created": created, "cmd": cmd[:200]})
    return out


def launch_log_status(logbase):
    """(last status line, finished_ok) of a sol_launch_v3 launch log."""
    path = Path(str(logbase) + "_launch.log")
    try:
        lines = [x.strip() for x in path.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except OSError:
        return "", False
    last = lines[-1] if lines else ""
    return last, bool(re.search(r"\]\s+success\b", last))


# ---------------------------------------------------------------- exactly once

def done_dir(outage_id):
    return p("done-dry" if DRY else "done") / re.sub(r"[^A-Za-z0-9_.-]", "_", str(outage_id))


def claim(outage_id, name, info):
    """Create the stamp BEFORE launching. False = already claimed (never launch twice)."""
    d = done_dir(outage_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(info, claimed_at=iso(), dry_run=DRY), f, indent=2)
    return path


def stamp_result(path, **result):
    data = read_json(path, {})
    data.update(result, finished_at=iso())
    write_json(path, data)


# ---------------------------------------------------------------- rebirth actions

RELAUNCH_PROMPT = ("Read ~/outage/RESUME.md, run pool_codex_notify.py --register, "
                   "and answer operator@device4 on the Pool.")


def relaunch_once(line_id, writer):
    """Claim a Pool line before starting the machine's locally configured fresh session."""
    stamp = claim("relaunch", line_id, {"writer": writer, "machine": MACHINE})
    if not stamp:
        return  # scanner may reread after a crash; the launch must not repeat
    cfg = read_json(p("relaunch.json"), {})
    argv = cfg.get("argv")
    cwd = cfg.get("cwd")
    if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv)
            or not isinstance(cwd, str) or not Path(cwd).is_dir()):
        result = "FAILED: missing or invalid local relaunch.json argv/cwd"
    elif run_active():
        result = "FAILED: judged run active on device4"
    else:
        argv = [a.replace("{prompt_shell}", shlex.quote(RELAUNCH_PROMPT))
                .replace("{prompt}", RELAUNCH_PROMPT) for a in argv]
        try:
            if DRY:
                result = f"WOULD LAUNCH: {subprocess.list2cmdline(argv)} (cwd {cwd})"
            elif cfg.get("pid_from_stdout"):
                # tmux -P -F '#{pane_pid}' reports the Codex pane PID, not the short-lived tmux client.
                proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30,
                                      creationflags=NOWIN)
                if proc.returncode:
                    raise RuntimeError((proc.stderr or proc.stdout).strip()[:200] or f"rc={proc.returncode}")
                pid = int(proc.stdout.strip())
                if not pid_alive(pid):
                    raise RuntimeError(f"launched session pid {pid} is not alive")
                result = f"done: pid {pid}"
            else:
                proc = detached(argv, cwd=cwd)
                result = f"done: pid {proc.pid}"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            result = f"FAILED: {type(e).__name__}: {e}"
    stamp_result(stamp, result=result, argv=argv if isinstance(argv, list) else None, cwd=cwd)
    log(f"RELAUNCH {MACHINE} {result} ({writer})")
    pool_say(f"RELAUNCH {MACHINE} {result}", to="operator@device4")


def process_relaunch(st):
    entries, offset = relaunch_triggers(st)
    for line_id, writer in entries:
        relaunch_once(line_id, writer)
    st["relaunch_offset"] = offset
    save_state(st)


def detached(argv, cwd=None):
    """Start a process that does not hold the watcher (the watcher supervises it but may exit first)."""
    kw = dict(cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not WIN:
        return subprocess.Popen(argv, start_new_session=True, **kw)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | NOWIN
    try:  # leave the scheduled task's job, so ending the task never ends a reborn lane
        return subprocess.Popen(argv, creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **kw)


def reborn_prompt(entry, st):
    """The lane's own brief with a short rebirth preamble in front (a new file; the original is never touched)."""
    src = Path(entry["prompt"])
    brief = src.read_text(encoding="utf-8", errors="replace")
    pre = [
        f"# REBIRTH after the internet outage ({st.get('offline_since', '?')} -> {st.get('online_at', '?')}, "
        f"outage {st.get('outage_id')})",
        "",
        "This lane was cut by the internet outage (Codex lost its connection) and is relaunched once by the outage "
        "watcher. Nothing else about the brief changes. Before you continue:",
        f"1. Read the tail of your previous run: {entry['logbase']}_launch.log and the newest "
        f"{entry['logbase']}_attempt*.log (what you did, what you pushed, where you stopped).",
        f"2. `git fetch` and compare your worktree with your remote branch"
        + (f" ({entry['branch']}" + (f", worktree {entry['worktree']}" if entry.get('worktree') else "") + ")"
           if entry.get("branch") else " (named in the brief below)")
        + ". Push any commit that failed to push during the outage.",
        "3. If the deliverable already carries its final Status: line and the branch is pushed, verify that and stop."
        " Otherwise continue from where it stopped. Never redo finished, pushed work.",
        "",
        "--- the original brief follows ---",
        "",
    ]
    out = p("reborn") / re.sub(r"[^A-Za-z0-9_.-]", "_", st.get("outage_id", "x")) / (entry["name"] + ".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(pre) + brief, encoding="utf-8")
    return out


def rebirth_sol_lane(entry, st, children, pending=None):
    last, ok = launch_log_status(entry["logbase"])
    if ok:
        return f"skip {entry['name']}: its launch log already ends in success ({last[:80]})"
    alive = sol_lanes_alive() or {}
    if entry["name"] in alive:
        if pending is not None:
            pending.append(entry)
        return (f"skip {entry['name']}: its launcher is still alive (pid {alive[entry['name']]['launcher_pid']}); "
                f"followed for 30 min: reborn once if it then ends without success")
    if not Path(entry["prompt"]).exists():
        return f"FAILED {entry['name']}: prompt file missing {entry['prompt']}"
    stamp = claim(st["outage_id"], "lane_" + entry["name"], entry)
    if not stamp:
        return f"skip {entry['name']}: already reborn for this outage (stamp)"
    logbase = f"{entry['logbase']}_reborn"
    prompt = reborn_prompt(entry, st)
    if DRY:
        argv = ["cmd.exe", "/c", str(SOL_LAUNCH), str(prompt), logbase] + ([entry["deliverable"]] if entry.get("deliverable") else [])
        stamp_result(stamp, result="dry-run", argv=argv)
        return f"WOULD LAUNCH {entry['name']}: {subprocess.list2cmdline(argv)}  (cwd {DOCKET})"
    argv = ["cmd.exe", "/c", str(SOL_LAUNCH), str(prompt), logbase] + ([entry["deliverable"]] if entry.get("deliverable") else [])
    try:
        proc = detached(argv, cwd=str(DOCKET))
    except OSError as e:
        stamp_result(stamp, result=f"launch failed: {e}")
        return f"FAILED {entry['name']}: {e}"
    children.append((entry["name"], proc, logbase))
    stamp_result(stamp, result="launched", pid=proc.pid, argv=argv)
    return f"LAUNCHED {entry['name']} pid {proc.pid}: {subprocess.list2cmdline(argv)}"


def notify_command(message):
    hook = read_json(POOL_DIR / "pool_data" / "notify.json", None)
    if not hook or not hook.get("command"):
        return None
    return [str(a).replace("{message}", message) for a in hook["command"]]


def rebirth_codex_wake(entry, st, children):
    stamp = claim(st["outage_id"], "wake_" + entry["name"], entry)
    if not stamp:
        return f"skip wake {entry['name']}: already done for this outage (stamp)"
    msg = (f"REBIRTH {MACHINE}: the internet is back ({st.get('online_at')}; offline since {st.get('offline_since')}). "
           f"Read your RESUME note ({p('RESUME.md')}), push anything that failed to push, and continue your work.")
    argv = notify_command(msg)
    if not argv:
        result = "no notify.json hook here"
    elif DRY:
        stamp_result(stamp, result="dry-run", argv=argv)
        return f"WOULD WAKE the Codex session via notify.json: {subprocess.list2cmdline(argv)[:300]}"
    else:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120, creationflags=NOWIN)
            result = f"rc={r.returncode} {(r.stdout or r.stderr).strip()[:200]}"
            if r.returncode == 0:
                stamp_result(stamp, result="woken: " + result)
                return f"WOKE the Codex session ({result})"
        except (OSError, subprocess.SubprocessError) as e:
            result = f"failed: {e}"
    fb = entry.get("fallback")
    if not fb:
        stamp_result(stamp, result=f"wake failed, no fallback: {result}")
        return f"FAILED wake {entry['name']} ({result}); no fallback in resume.json"
    if DRY:
        stamp_result(stamp, result="dry-run fallback", argv=fb)
        return f"wake not possible ({result}); WOULD LAUNCH fallback: {subprocess.list2cmdline(fb)}"
    try:
        proc = detached(fb, cwd=entry.get("cwd"))
        children.append((entry["name"], proc, ""))
        stamp_result(stamp, result=f"wake failed ({result}); fallback launched pid {proc.pid}", argv=fb)
        return f"wake failed ({result}); LAUNCHED fallback pid {proc.pid}"
    except OSError as e:
        stamp_result(stamp, result=f"wake failed ({result}); fallback failed: {e}")
        return f"FAILED wake and fallback {entry['name']}: {e}"


def rebirth_command(entry, st, children):
    stamp = claim(st["outage_id"], "cmd_" + entry["name"], entry)
    if not stamp:
        return f"skip {entry['name']}: already done for this outage (stamp)"
    if DRY:
        stamp_result(stamp, result="dry-run")
        return f"WOULD LAUNCH {entry['name']}: {subprocess.list2cmdline(entry['argv'])}"
    try:
        proc = detached(entry["argv"], cwd=entry.get("cwd"))
    except OSError as e:
        stamp_result(stamp, result=f"launch failed: {e}")
        return f"FAILED {entry['name']}: {e}"
    children.append((entry["name"], proc, ""))
    stamp_result(stamp, result="launched", pid=proc.pid)
    return f"LAUNCHED {entry['name']} pid {proc.pid}"


def open_operator_terminal(st, old):
    """ONE new Claude Code terminal for the operator (device4)."""
    lease = (f"OPERATOR LEASE (outage {st['outage_id']}): the operator is the Claude Code session the outage watcher "
             f"opened at {iso()} (window title 'Pool operator reborn').\n"
             f"Any OLDER operator session that wakes after {st.get('online_at')} is RETIRED: it posts ONE line "
             f"'operator (old session) retired by the outage lease' to operator@device4 and makes no further tool calls.\n"
             f"Old interactive Claude sessions alive at rebirth (no ack within the wait): "
             f"{', '.join(str(o['pid']) for o in old) or 'none'}\n")
    reborn = make_reborn_operator_note(st, old)
    cmdfile = p("operator_reborn.cmd")
    wt = [r"C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\wt.exe", "-w", "new", "--title", "Pool operator reborn",
          "-d", r"C:\Users\operator", "cmd.exe", "/k", str(cmdfile)]
    if DRY:
        return f"WOULD OPEN ONE operator terminal: {subprocess.list2cmdline(wt)}  (lease + {reborn.name} written in the dry-run home)"
    (p("OPERATOR_LEASE.txt")).write_text(lease, encoding="utf-8")
    try:
        detached(wt)
        return f"OPENED ONE operator terminal (wt.exe): {cmdfile}"
    except OSError as e:
        try:
            detached(["cmd.exe", "/c", "start", "Pool operator reborn", "cmd.exe", "/k", str(cmdfile)])
            return f"OPENED ONE operator terminal (cmd start; wt failed: {e})"
        except OSError as e2:
            return f"FAILED to open the operator terminal: {e}; {e2}"


def make_reborn_operator_note(st, old):
    rep = read_json(p("rebirth_report.json"), {})
    resume = read_json(st.get("resume_path") or p("resume.json"), {})
    lines = [
        f"# REBORN OPERATOR (written by the outage watcher at {iso()})",
        "",
        f"Outage {st.get('outage_id')}: offline since {st.get('offline_since')} ({st.get('offline_reason', '')}), "
        f"internet back {st.get('online_at')}.",
        f"Old interactive Claude sessions alive without an ack: {', '.join(str(o['pid']) for o in old) or 'none'}. "
        "If one of them wakes, it retires itself by C:/tmp/docket/outage/OPERATOR_LEASE.txt. Never stop it yourself "
        "while it holds a live game or lane as a child; ask operator.",
        "",
        "## Do, in order",
        "1. Read C:/tmp/docket/TAKEOVER.md and C:/tmp/docket/OUTAGE_PLAN.md (section 'After the outage').",
        "2. Restart the watchers of TAKEOVER section 4 ONCE. Before each, check for a live copy from the old session "
        "(`ps -ef | grep -E 'pool_wake|luna_whirlpool'`, guard_20gb in PowerShell): pool_wake.sh kills older listeners "
        "itself; do not start a second luna_whirlpool.sh or guard while one is alive.",
        "3. Check C:/tmp/docket/RUN_ACTIVE and game_slot before any launch. A judged run started before the outage "
        "may still be playing under the old session.",
        "4. The Codex lanes below were relaunched by the watcher (it posts 'REBORN-LANE <name> finished' to "
        "operator@device4 when each ends). Read their deliverables as usual; stop one only with stop_lane.py.",
        "5. Relaunch the Claude agents (integrator, Sonnet/Opus) the RESUME note lists, then post ONE line "
        "'OPERATOR REBORN' to the Pool.",
        "",
        "## RESUME note (C:/tmp/docket/outage/RESUME.md)",
        "",
    ]
    try:
        lines.append(p("RESUME.md").read_text(encoding="utf-8"))
    except OSError:
        lines.append("(none written)")
    lines += ["", "## resume.json note", "", str(resume.get("note", "(none)")), "",
              "## What the watcher did", ""] + [f"- {x}" for x in rep.get("actions", [])]
    path = p("REBORN_OPERATOR.md")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rebirth_operator(st, ack_wait, actions):
    stamp_name = "operator"
    if (done_dir(st["outage_id"]) / (stamp_name + ".json")).exists():
        return "skip operator: already handled for this outage (stamp)"
    alive = claude_sessions()
    if alive is None:
        return "operator: could not list processes; nothing opened (operator/operator: see OUTAGE_PLAN.md)"
    ack = p("ack") / (st["outage_id"] + ".operator")
    try:
        since = datetime.fromisoformat(str(st.get("offline_since"))).timestamp()
    except ValueError:
        since = 0.0

    def acked():  # an ack counts only when written after the outage began (a 01:30 ack proves nothing about 03:00)
        try:
            return ack.stat().st_mtime >= since
        except OSError:
            return False
    if alive:
        pids = ", ".join(str(a["pid"]) for a in alive)
        if not acked():
            deadline = now() + timedelta(seconds=ack_wait)
            pool_say(f"operator@device4: the internet is back (outage {st['outage_id']}). If you read this, run "
                     f"`python C:/tmp/docket/tools/ops/outage/outage_watch.py ack` NOW. No ack by "
                     f"{deadline:%H:%M} = this session is deaf and ONE new operator terminal opens (OPERATOR_LEASE.txt).",
                     to="operator@device4")
            log(f"operator: interactive Claude alive ({pids}); waiting {ack_wait}s for an ack")
            while now() < deadline and not acked():
                touch_lock()
                time.sleep(min(15, max(1, ack_wait)))
        if acked():
            stamp = claim(st["outage_id"], stamp_name, {"result": "acked", "alive": alive})
            return f"operator ACKED ({ack.read_text(encoding='utf-8').strip()[:80]}); no new session"
        alive = claude_sessions() or alive
    stamp = claim(st["outage_id"], stamp_name, {"alive": alive})
    if not stamp:
        return "skip operator: already handled for this outage (stamp)"
    rep = read_json(p("rebirth_report.json"), {})
    rep["actions"] = actions
    write_json(p("rebirth_report.json"), rep)
    res = open_operator_terminal(st, alive)
    stamp_result(stamp, result=res)
    return ("operator: " + ("no interactive session alive; " if not alive else
                            f"session(s) {', '.join(str(a['pid']) for a in alive)} gave no ack (deaf); ") + res)


# ---------------------------------------------------------------- the rebirth

def resume_entries(st):
    resume = read_json(st.get("resume_path") or p("resume.json"), {})
    entries = [dict(e) for e in resume.get("entries", []) if isinstance(e, dict) and e.get("name")]
    skip = set(resume.get("skip", []))
    names = {e["name"] for e in entries}
    prompts = {str(e.get("prompt", "")).replace("/", "\\").lower() for e in entries if e.get("prompt")}
    if resume.get("auto_lanes", True):
        for name, e in sorted((st.get("lanes_at_outage") or {}).items()):
            if name in names or name in skip or str(e.get("prompt", "")).replace("/", "\\").lower() in prompts:
                continue
            entries.append(e)
    return [e for e in entries if e["name"] not in skip], resume


def do_rebirth(st, args, reason):
    log(f"REBIRTH begins ({reason})")
    ensure_pool()
    entries, resume = resume_entries(st)
    online_line = (f"ONLINE {MACHINE} {now():%H:%M} (offline since {str(st.get('offline_since', '?'))[11:16]}, "
                   f"{reason}): every agent woken by this, read your machine's RESUME note, push what failed to push, "
                   f"and continue.")
    if not st.get("online_posted"):
        st["online_posted"] = pool_say(online_line)
        save_state(st)
    held = run_active()
    if held:
        log(f"HOLDING every launch: {held}")
        pool_say(f"REBIRTH {MACHINE} holding: {held}. Lanes and the operator terminal start when it ends.",
                 to="operator@device4")
        while run_active():
            touch_lock()
            time.sleep(60)
        log("judged run over: launches proceed")
    children, actions, pending = [], [], []
    for e in entries:
        kind = e.get("kind", "sol_lane")
        try:
            if kind == "sol_lane":
                actions.append(rebirth_sol_lane(e, st, children, pending))
            elif kind == "codex_wake":
                actions.append(rebirth_codex_wake(e, st, children))
            elif kind == "command":
                actions.append(rebirth_command(e, st, children))
            else:
                actions.append(f"skip {e['name']}: unknown kind {kind}")
        except Exception as ex:  # one broken entry never stops the others
            actions.append(f"FAILED {e.get('name')}: {type(ex).__name__}: {ex}")
        log(actions[-1])
    if WIN and resume.get("operator", True):
        actions.append(rebirth_operator(st, args.ack_wait, list(actions)))
        log(actions[-1])
    write_json(p("rebirth_report.json"), {"outage_id": st.get("outage_id"), "at": iso(), "reason": reason,
                                         "dry_run": DRY, "actions": actions})
    summary = "; ".join(a.split(":")[0] for a in actions) or "nothing to relaunch"
    pool_say(f"REBIRTH {MACHINE} done ({len(actions)} actions): {summary[:600]}. Report: {p('rebirth_report.json')}",
             to="operator@device4")
    st["phase"] = "done"
    st["rebirth_at"] = iso()
    save_state(st)
    supervise(children, pending, st, standing=args.standing)


def supervise(children, pending=(), st=None, standing=False):
    """Stay alive while reborn lanes run, and tell the operator when each ends (its own task notification died
    with the old session). A lane whose launcher was still retrying at ONLINE is followed for 30 min: if it then
    ends without success, it is reborn once (same stamp)."""
    pending = list(pending)
    follow_until = time.time() + 1800
    last_book_read = 0.0
    while children or (pending and time.time() < follow_until):
        touch_lock()
        if standing and time.monotonic() - last_book_read >= 60:
            process_relaunch(load_state())
            last_book_read = time.monotonic()
        if pending and not run_active():
            alive = sol_lanes_alive()
            for e in list(pending):
                if alive is None or e["name"] in alive:
                    continue
                pending.remove(e)
                msg = rebirth_sol_lane(e, st, children)
                log("follow-up: " + msg)
                if not msg.startswith("skip"):
                    pool_say(f"REBORN-LANE {msg[:300]}", to="operator@device4")
        for item in list(children):
            name, proc, logbase = item
            rc = proc.poll()
            if rc is None:
                continue
            children.remove(item)
            last, ok = launch_log_status(logbase) if logbase else ("", rc == 0)
            pool_say(f"REBORN-LANE {name} finished rc={rc} ({'success' if ok else 'not success'}): {last[:160]}",
                     to="operator@device4")
        time.sleep(30)


# ---------------------------------------------------------------- single instance

LOCK = None


def take_lock():
    global LOCK
    path = p("watch.lock")
    HOME.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            LOCK = path
            return True
        except FileExistsError:
            try:
                pid = int(path.read_text().strip() or 0)
                stale = (not pid_alive(pid)) or time.time() - path.stat().st_mtime > 600
            except (OSError, ValueError):
                stale = True
            if not stale:
                return False
            try:
                path.unlink()
            except OSError:
                pass
    return False


def touch_lock():
    if LOCK:
        try:
            os.utime(LOCK, None)
        except OSError:
            pass


def drop_lock():
    if LOCK:
        try:
            if LOCK.read_text().strip() == str(os.getpid()):
                LOCK.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- commands

def cmd_arm(args):
    st = load_state()
    frm = parse_time(args.frm)
    until = parse_time(args.until) if args.until else frm + timedelta(hours=12)
    oid = args.id or f"outage-{frm:%Y%m%d}"
    book = POOL_DIR / "pool_data" / "pool.txt"
    st = {"phase": "armed", "outage_id": oid, "armed_at": iso(), "armed_from": iso(frm), "armed_until": iso(until),
          "boot_at_arm": boot_time(), "book_offset": book.stat().st_size if book.exists() else 0,
          "relaunch_offset": book.stat().st_size if book.exists() else 0,
          "resume_path": str(Path(args.resume)) if args.resume else str(p("resume.json")), "machine": MACHINE}
    save_state(st)
    log(f"ARMED {oid}: watching from {st['armed_from']} until {st['armed_until']} (if no outage begins by then)")
    print(json.dumps(st, indent=2))


def cmd_disarm(args):
    st = load_state()
    st["phase"] = "disarmed"
    st["disarmed_at"] = iso()
    save_state(st)
    log("DISARMED")


def cmd_ack(args):
    st = load_state()
    oid = st.get("outage_id") or "none"
    d = p("ack")
    d.mkdir(parents=True, exist_ok=True)
    (d / (oid + ".operator")).write_text(f"acked {iso()} by pid {os.getppid()}", encoding="utf-8")
    log(f"ACK operator for {oid}")
    print(f"ack written for {oid}; phase {st.get('phase')}")


def cmd_status(args):
    st = load_state()
    print(json.dumps(st, indent=2))
    entries, resume = resume_entries(st)
    print(f"resume: {st.get('resume_path') or p('resume.json')}  note: {str(resume.get('note', ''))[:200]}")
    for e in entries:
        print(f"  entry {e.get('kind', 'sol_lane')} {e['name']}  {e.get('prompt', '') or e.get('argv', '')}")
    d = p("done") / str(st.get("outage_id", ""))
    if d.exists():
        for f in sorted(d.iterdir()):
            print(f"  stamp {f.name}: {read_json(f, {}).get('result', 'claimed')}")


def cmd_preview(args):
    """Read-only: what a rebirth would do right now (no stamps, no Pool lines, no launches)."""
    st = load_state()
    print(f"machine {MACHINE}  phase {st.get('phase')}  outage {st.get('outage_id')}  from {st.get('armed_from')} "
          f"until {st.get('armed_until')}")
    alive = sol_lanes_alive() if WIN else {}
    if WIN and alive is not None:
        st = dict(st, lanes_at_outage=dict(st.get("lanes_at_outage") or {}, **alive))
    entries, resume = resume_entries(st)
    print(f"resume.json: {st.get('resume_path') or p('resume.json')} ({'found' if resume else 'MISSING'})"
          f"  auto_lanes={resume.get('auto_lanes', True)}  skip={resume.get('skip', [])}")
    print(f"RESUME.md: {'found' if p('RESUME.md').exists() else 'MISSING'}")
    for e in entries:
        kind = e.get("kind", "sol_lane")
        if kind == "sol_lane":
            last, ok = launch_log_status(e["logbase"])
            state = ("ALIVE now (would be relaunched only if it dies in the outage)" if e["name"] in (alive or {})
                     else "finished: success (skip)" if ok else "would RELAUNCH")
            print(f"  sol_lane {e['name']} [{e.get('source', 'resume')}]: {state}; prompt "
                  f"{'ok' if Path(e['prompt']).exists() else 'MISSING'} {e['prompt']}; last log: {last[:90]}")
        elif kind == "codex_wake":
            argv = notify_command("test")
            print(f"  codex_wake {e['name']}: notify.json {'ok: ' + str(argv[:3]) if argv else 'MISSING'}; "
                  f"fallback {e.get('fallback') or 'none'}")
        else:
            print(f"  {kind} {e['name']}: {e.get('argv')}")
    if WIN and resume.get("operator", True):
        cs = claude_sessions()
        print(f"  operator: interactive Claude Code sessions alive now: "
              f"{', '.join(str(c['pid']) for c in cs) if cs else 'none'} (after the outage: alive -> ack request, "
              f"no ack in {int(args.ack_wait)} s or none alive -> ONE new terminal)")
    return 0


def cmd_rebirth(args):
    st = load_state()
    if st.get("phase") in (None, "idle"):
        st = {"phase": "offline", "outage_id": args.id or f"manual-{now():%Y%m%d-%H%M}"}
    elif st.get("phase") in ("armed", "expired", "disarmed") and not args.force:
        print(f"phase {st.get('phase')}: no outage seen, so a rebirth now would use up this outage's stamps and end the "
              f"watcher. Use 'preview' to look; '--force' only if you mean it.")
        return 2
    if not take_lock():
        print("a watcher is running here: post 'REBIRTH <machine>' on the Pool instead, it acts on that")
        return 2
    try:
        st.setdefault("offline_since", st.get("offline_since") or "unknown")
        st["online_at"] = st.get("online_at") or iso()
        do_rebirth(st, args, "by hand")
    finally:
        drop_lock()
    return 0


def cmd_run(args):
    if not take_lock():
        log("another watcher holds the lock; exiting")
        return 0
    try:
        return watch(args)
    finally:
        drop_lock()


def watch(args):
    st = load_state()
    if st.get("phase") not in ("armed", "offline", "online") and not args.standing:
        log(f"not armed (phase {st.get('phase')}); nothing to watch")
        return 0
    if "relaunch_offset" not in st:
        book = POOL_DIR / "pool_data" / "pool.txt"
        st["relaunch_offset"] = book.stat().st_size if book.exists() else 0
        save_state(st)
    log(f"watcher up (pid {os.getpid()}, phase {st['phase']}, outage {st.get('outage_id')})")
    fails = oks = 0
    last_snap = 0.0
    while True:
        touch_lock()
        st = load_state()
        phase = st.get("phase")
        process_relaunch(st)
        if phase not in ("armed", "offline", "online"):
            if args.standing:
                # The relaunch scan reads each new entry, including inert REBIRTH lines, once per minute.
                # Outside the armed outage window there are no network probes.
                time.sleep(60)
                continue
            log(f"phase {phase}: watcher exits")
            return 0
        t = now()
        if phase == "armed" and t < datetime.fromisoformat(st["armed_from"]):
            time.sleep(min(60, max(1, (datetime.fromisoformat(st["armed_from"]) - t).total_seconds())))
            continue
        if phase == "armed" and t > datetime.fromisoformat(st["armed_until"]):
            st["phase"] = "expired"
            save_state(st)
            log("no outage began before armed_until: expired")
            if args.standing:
                continue
            return 0
        trig = book_trigger(st)
        save_state(st)
        if trig and phase in ("armed", "offline"):
            st["offline_since"] = st.get("offline_since") or "none seen"
            st.setdefault("lanes_at_outage", st.get("lanes_seen") or {})
            st["online_at"] = iso()
            st["phase"] = "online"
            save_state(st)
            do_rebirth(st, args, f"Pool {TRIGGER} from {trig}")
            if args.standing:
                continue
            return 0
        boot = boot_time()
        if (phase == "armed" and st.get("boot_at_arm") and boot - float(st["boot_at_arm"]) > 120
                and boot > datetime.fromisoformat(st["armed_from"]).timestamp()):
            st.update(phase="offline", offline_since=iso(datetime.fromtimestamp(boot_time())),
                      offline_reason="this machine rebooted after arming")
            save_state(st)
            log("OFFLINE: rebooted after arming; every session and lane on this machine died")
            phase = "offline"
        up = probe(args.probe_file)
        if phase == "armed":
            if WIN and time.time() - last_snap > args.snapshot_every and not run_active():
                snap = sol_lanes_alive()
                if snap is not None:
                    st["lanes_seen"] = snap
                    st["lanes_seen_at"] = iso()
                    save_state(st)
                last_snap = time.time()
            fails = 0 if up else fails + 1
            if fails >= args.offline_after:
                snap = sol_lanes_alive() if (WIN and not run_active()) else None if WIN else {}
                st.update(phase="offline", offline_since=iso(t - timedelta(seconds=args.interval * (fails - 1))),
                          offline_reason=f"{fails} failed probes in a row",
                          lanes_at_outage=snap if snap is not None else (st.get("lanes_seen") or {}))
                save_state(st)
                log(f"OFFLINE since {st['offline_since']}: Sol lanes alive now: "
                    f"{', '.join(st['lanes_at_outage']) or 'none'}")
        elif phase == "offline":
            oks = oks + 1 if up else 0
            if oks >= args.online_after:
                st.update(phase="online", online_at=iso())
                st.setdefault("lanes_at_outage", st.get("lanes_seen") or {})
                save_state(st)
                log(f"ONLINE at {st['online_at']} after {oks} good probes")
                do_rebirth(st, args, "internet back")
                if args.standing:
                    continue
                return 0
        elif phase == "online":  # a crash between ONLINE and the rebirth: finish it (stamps keep it once)
            do_rebirth(st, args, "resumed after a watcher restart")
            if args.standing:
                continue
            return 0
        time.sleep(args.interval)


def main(argv):
    global HOME, DRY, MACHINE, TRIGGER, POOL_SELF
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=None, help="state folder (default C:/tmp/docket/outage or ~/outage)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trigger-word", default=TRIGGER, help=argparse.SUPPRESS)
    ap.add_argument("--pool-self", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("arm")
    a.add_argument("--from", dest="frm", default="now")
    a.add_argument("--until", default=None)
    a.add_argument("--id", default=None)
    a.add_argument("--resume", default=None)
    sub.add_parser("disarm")
    sub.add_parser("ack")
    sub.add_parser("status")
    for name in ("run", "rebirth", "preview"):
        r = sub.add_parser(name)
        r.add_argument("--probe-file", default=None)
        r.add_argument("--interval", type=float, default=60)
        r.add_argument("--standing", action="store_true", help="keep reading Pool triggers after the outage window")
        r.add_argument("--offline-after", type=int, default=3)
        r.add_argument("--online-after", type=int, default=3)
        r.add_argument("--ack-wait", type=float, default=900)
        r.add_argument("--snapshot-every", type=float, default=600)
        r.add_argument("--id", default=None)
        r.add_argument("--force", action="store_true", help="rebirth: run even though no outage was seen")
    args = ap.parse_args(argv)
    if args.home:
        HOME = Path(args.home)
    DRY = args.dry_run
    POOL_SELF = args.pool_self
    if (DRY or POOL_SELF) and not args.home:
        ap.error("--dry-run/--pool-self need their own --home (a test folder): it must never touch the real state or stamps")
    TRIGGER = args.trigger_word
    MACHINE = machine_name()
    return {"arm": cmd_arm, "disarm": cmd_disarm, "ack": cmd_ack, "status": cmd_status,
            "run": cmd_run, "rebirth": cmd_rebirth, "preview": cmd_preview}[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
