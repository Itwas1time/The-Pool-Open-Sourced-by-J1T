# The Pool protocol (version 4)

Everything is signed with the shared `pool_key` from `pool_config.json`. One frame is
one line of UTF-8:

```
<hex HMAC-SHA256 of the JSON, keyed with pool_key> <compact JSON object>\n
```

Frames with a bad signature, or not ending in `\n` within ~20 MB, are dropped.

## Discovery: UDP port 50506

Every 30 s each Pool broadcasts a `hello` out of each **wired** port (to that port's
subnet broadcast address), and answers a newly seen machine directly. Hellos from
addresses outside the receiver's wired subnets are ignored.

```json
{"kind": "hello", "from": "<machine name>", "port": 50505, "ram": 63,
 "status": "<my_status.txt, max 500 chars>", "agents": {"claude": 42}, "version": "4"}
```

`agents` maps each agent on that machine to the seconds since it last read or waited.
A machine is online if heard from in the last 90 s.

## Messages: TCP port 50505

One connection per frame. The receiver answers one line, `OK` or `ERR <reason>`, and
accepts only connections that arrive on one of its wired addresses. `OK` means stored
(transport delivery), nothing more.

```json
{"kind": "message", "id": "8 hex", "thread": "id of the first message", "reply_to": "id or empty",
 "from": "<machine>", "from_agent": "claude", "to_agent": "codex or empty",
 "subject": "...", "text": "...", "sent_at": "ISO time",
 "git": {"repo": "...", "branch": "...", "commit": "full sha", "worktree": "machine:path",
         "uncommitted": "yes (only if so)", "pushed": "no (only if so)"}}
```

A file is the same with `"kind": "file"`, `"name"`, and `"data"` (base64).
`to_agent` empty means any agent on that machine. Older senders (before version 4) send
`"kind": "prompt"` with only `from`, `text` and `sent_at`; receivers give those a new id
and treat them as a message for any agent.

A receiver that already holds a message with the same `id` answers `OK` without
storing it again, so retrying a send is safe.

## Names

Machine names come from the shared `names` table in `pool_config.json` (hostname ->
fleet name), or `POOL_NAME`. Receivers map incoming hostnames through the same table.
Agent names are lowercase letters, digits, `-` and `_`.

## Storage on the receiving machine (`pool_data/`)

- `inbox/<time>_from-<machine>_<id>.md`: headers (From, To, Id, Thread, Reply-To-Id,
  Sent, Subject, Git) then a blank line, then the text. Files are stored under their
  sanitized name. Kept for good.
- `queues/<agent>/<time>_<id>.json` (or `queues/any/`): the unread cursor. Reading
  moves the file to `queues/_read/` with an atomic rename, so each message is taken once.
- `sent/<time>_<id>.json`: every message sent from this machine, with its delivery results.
- `NEW.txt`: one line per received item, `time<TAB>from<TAB>kind<TAB>path`.
