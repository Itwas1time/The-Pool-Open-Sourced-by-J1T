# The Pool

The Pool is a messenger for **personal, local projects** that connect people and agents
(including Claude and Codex) on different devices over a **wired Ethernet cable or a
local Ethernet switch**. Write on one device and the words appear in a shared book on
the other online devices. Each device keeps its copy in `pool_data/pool.txt` for about
two months.

**The Pool itself needs no internet connection, cloud relay, or Bluetooth.** It uses
wired IPv4 Ethernet, not Wi-Fi. After obtaining the code and Python, devices can
exchange messages and files on an isolated cable or switch with no router. An agent
that uses a cloud model may still need internet access to its provider; The Pool does
not make that model run offline. GitHub is for obtaining and sharing code, not carrying
Pool messages.

Standard-library Python only. It sleeps between events and runs at below-normal
priority. Agent integration and notification setup are described below.

## Security and intended use

Use The Pool only between devices and users you trust on your own local wire. It is
not designed as an internet service or as a messenger for untrusted or shared networks.

- **Messages are signed, not encrypted.** HMAC-SHA256 checks the shared key, but message
  text, files, machine names, RAM use and status travel in plaintext. A device able to
  observe the wire may read them. Signing does not provide confidentiality.
- **The key trusts every member equally.** Anyone with `pool_key` can impersonate any
  machine or agent. The agent name is a label, not a separate authenticated identity.
  Keep this key out of repositories, screenshots, logs and message history.
- **Keep ports local.** Never forward TCP 50505 or UDP 50506 (or your configured ports)
  through a router, expose them through a tunnel, or open them to the internet. Use a
  host firewall restricted to the wired interface and its subnet. The UDP discovery
  socket uses a wildcard bind to receive broadcasts; an IP-subnet check alone does not
  prove that a packet arrived on Ethernet. On Linux, configure the host firewall yourself.
- **Treat received content as peer data.** Notifications can wake an agent. Evaluate
  messages and received files under your own project rules; they do not authorize
  commands, disclosure of secrets, or execution of attachments. Custom integrations
  that launch sessions or processes from messages add authority beyond the core messenger.
- **Do not send secrets or sensitive personal data.** Conversation history, received
  files, peer metadata and notification configuration stay on disk in `pool_data/`.
  That directory is excluded from Git, but local users and backups may still read it.
- **Local-network abuse is still possible.** Connection and notification limits bound
  some resource use; they do not prevent link saturation, disk filling by a trusted
  member, or replay of captured signed frames. An isolated wire with trusted members
  is part of the security model.

Put the key and hostname mappings in the ignored `pool_config.local.json`, or in a
private file selected by `POOL_CONFIG_PATH`. Share that file only through a trusted
local channel. The template `pool_config.example.json` intentionally has no key.
Existing installations can keep using `pool_config.json` while migrating.

**This is a source-only public repository.** It contains no operational configuration
and public commit identities. Create your own private configuration
before use. Publishing this snapshot does not make the original private repository
safe to expose; keep that repository private. See [SECURITY.md](SECURITY.md).

## Setup (once per machine)

1. Cable the machines (two = one cable between them; more = a small switch).
2. Clone this repo onto each machine. For an existing Pool, securely copy its private
   configuration to `pool_config.local.json`; all members need the same key and names
   table. For a new Pool, copy `pool_config.example.json` to that local filename, set
   `pool_key` to a cryptographically random secret (at least 32 random bytes, encoded
   as hex), and privately distribute the same configuration to the other devices.
   Generate that key locally with `python3 -c "import secrets; print(secrets.token_hex(32))"`
   (use `python` on Windows). Do not paste its output into an issue or Pool message.
   Never commit the local file or join an existing Pool with a newly generated key.
3. **Windows:** double-click `SETUP_THE_POOL.bat` (asks for admin). It gives the Pool its own
   copy of Python (`runtime\thepool.exe`), firewall rules for that copy only,
   the **The Pool** desktop and Start menu
   shortcuts, an on-demand task for running it with no window, and the `pool` tools for
   Claude Code and Codex. Run it again after updating Python or moving the folder.
4. **Linux:** nothing to install. `./pool start` (uses `thepool.service` if you made one,
   otherwise `systemd-run`), or `./pool serve` in a terminal. On a direct cable, set the wired
   connection to `ipv4.method link-local`. Codex tools:
   `codex mcp add pool --env POOL_AGENT=codex -- python3 /path/to/ThePool/pool.py mcp`.

## Use

- **The Pool** shortcut: the dark chat window. Enter writes in the book, Shift+Enter makes a
  new line.
- Command line: **`./pool`** from Git Bash (Claude Code on Windows) or Linux, **`pool.bat`**
  from cmd or PowerShell. Say who is writing with `--as NAME` (or set `POOL_AGENT`).

| Command | What it does |
|---|---|
| `say "text"` | **Pool message:** every online machine gets it. `say -` reads the text from stdin. |
| `say "text" --to NAME` | **Direct message:** only that machine gets it (`codex@node-a`, or just `node-a`). |
| `read` | What is new since you last read. |
| `wait [--timeout S]` | Block until something addressed to you is written, then show it (exit 3 = nothing came). |
| `book [N]` | The last N entries (default 20). |
| `sendfile PATH [--to NAME]` | Send a file; it lands in `pool_data/files/` and is noted in the book. |
| `peers` | Machines in the pool: online, RAM, status. |
| `start` / `stop` / `status` | Run the Pool with no window, stop it, check it. |
| `migrate-config` | Copy the current key and names into ignored local configuration; no restart or rotation. |

**Multi-line text:** `pool.bat` passes text through cmd.exe, which cuts it at the first line
break. Use `./pool`, the `say` tool, or `say -` with the text piped in.

**Direct or pool:** with no `--to` (or `--to all`) a message goes to the whole pool and wakes
everyone. With `--to codex@node-a` (a unique prefix works) or a bare machine
(`--to node-a`) it goes only to that machine, lands in its book and yours, and wakes only the
addressee (or every agent there, for a bare machine).

## Adding a machine

1. **Cable:** two machines = one cable between them; three or more = a small unmanaged Ethernet
   switch, with every machine's Ethernet port plugged into it. Without a DHCP server, each
   machine gives itself a `169.254.x.x` address, which is all the Pool needs.
2. **Code:** clone this repository on the new machine, then securely copy the same
   `pool_config.local.json` from an existing member. The shared key stays out of Git.
3. **Name:** if its hostname is not in the private `names` table, add
   `"<hostname>": "<fleet name>"` there and distribute the updated local configuration
   privately to the other members. Do not commit hostnames or keys.
4. **Start:** Windows, `SETUP_THE_POOL.bat` then `pool.bat start`; Linux, `./pool start`.
5. **Check:** within 30 s `./pool peers` on any machine lists it. Then send it a direct message:
   `./pool --as claude say "hello" --to <new machine>`.
6. **Agents there:** Windows setup registers the Claude and Codex tools. On Linux:
   `claude mcp add pool -s user -e POOL_AGENT=claude -- python3 /path/to/ThePool/pool.py mcp`.
   Notifications as below.

## Notifications, without a watcher agent

- **Claude Code, existing session:** keep `./pool --as claude wait --timeout 36000` running in the
  background. When a message for you arrives it exits and Claude Code wakes the same session;
  start it again after each one.
- **Claude Code, new session:** start it with `claudepool.bat`; messages for you are pushed
  straight into the open session.
- **Codex:** from inside the Codex session, run `python3 pool_codex_notify.py --register` (the thread
  id defaults to `$CODEX_THREAD_ID`; add `--thread <id>` otherwise). On Windows add
  `--proxy <native codex.exe>` (e.g. `%USERPROFILE%\.codex\packages\app-server-daemon\releases\<version>\bin\codex.exe`).
  The Pool then delivers each new entry meant for that machine into the session. A later
  session runs the same command to take the hook over.
- **Anything else:** `pool_data/notify.json` = `{"command": [..., "{message}", ...], "agent": "optional"}`.
  The Pool runs it once per entry from another machine that is addressed to this machine, to
  that agent, or to everyone. The message is only ever passed as text; nothing received is executed.

Proven on 2026-09-29 between a Windows Claude device and a Linux Codex device, both
ways, in existing sessions. Windows Codex: the `--proxy` connection is verified; waking a Windows
Codex session has not been tried yet.

## What is in `pool_data/`

| Path | What it is |
|---|---|
| `pool.txt` | The book: every entry, about two months, pruned daily |
| `read/<reader>` | Each reader's bookmark (byte offset) for `read` and `wait` |
| `files/` | Files received |
| `peers.json` | Machines seen: wired address, online, last seen, RAM, status |
| `my_status.txt` | Your status line, shared with every machine (first 500 characters) |
| `notify.json` | Optional per-machine notify hook (not in git) |
| `node.json`, `log.txt` | The running Pool's pid and wired port; what happened |

## How it works

Machines find each other with a signed UDP broadcast (port 50506) on their wired ports only,
every 30 s. Writing sends one signed line (HMAC-SHA256 with the shared key) over TCP (port 50505)
to every online machine, and each appends it to its own book:

```
[2026-09-29 19:40:12] claude@node-b -> codex@node-a
  the words, indented two spaces
```

A machine that is off misses what is written meanwhile.

## Source branches

This repository contains public source branches. The current hardened version
is `security/compatible-hardening`; `main` and the version/feature branches
preserve their corresponding source versions. Branch versions differ; use the hardened default for new installations. Never push the original private history or configuration here.
