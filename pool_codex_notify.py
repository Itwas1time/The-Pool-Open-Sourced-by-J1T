#!/usr/bin/env python3
"""Deliver a Pool entry to an existing Codex session, without a watcher.

Register once from that session:
  python3 pool_codex_notify.py --thread <id> --register

The Pool's receive hook then runs this command once per remote entry. It uses
the running Codex app-server's Unix WebSocket and turn/start.toolOutput, so the
entry reaches an active turn or wakes an idle session as tool output. No new
session, model process, shell command, or API key is created. Requires Codex's
experimental toolOutput API (verified with CLI/app-server 0.159.2 on Linux).
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import time
import uuid


class CodexRPC:
    """Small, bounded WebSocket JSON-RPC client for the local control socket."""

    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, path=None, timeout=10):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.buffer = b""
        self.rid = 0
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        path = path or home / "app-server-control/app-server-control.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            while b"\r\n\r\n" not in self.buffer:
                self.buffer += self._read(4096)
                if len(self.buffer) > 65536:
                    raise RuntimeError("Codex handshake too large")
            raw, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            headers = raw.decode().split("\r\n")
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            fields = dict(line.split(":", 1) for line in headers[1:] if ":" in line)
            accept = next((v.strip() for k, v in fields.items()
                           if k.lower() == "sec-websocket-accept"), "")
            if " 101 " not in headers[0] or accept != expected:
                raise RuntimeError("Codex rejected the WebSocket handshake")
            self.call("initialize", {
                "clientInfo": {"name": "thepool_notify", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.sock.close()
            raise

    def _read(self, size):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex notification timed out")
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(size)
        if not chunk:
            raise RuntimeError("Codex control connection closed")
        return chunk

    def exact(self, size):
        while len(self.buffer) < size:
            self.buffer += self._read(min(max(4096, size - len(self.buffer)), 65536))
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def send(self, value, opcode=1):
        payload = json.dumps(value).encode() if isinstance(value, dict) else value
        size = len(payload)
        if size > self.MAX_BYTES:
            raise ValueError("Notification too large")
        length = (bytes([0x80 | size]) if size < 126 else
                  bytes([0x80 | 126]) + struct.pack("!H", size) if size < 65536 else
                  bytes([0x80 | 127]) + struct.pack("!Q", size))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode]) + length + mask + masked)

    def receive(self):
        fragments = bytearray()
        while True:
            first, second = self.exact(2)
            opcode, size = first & 15, second & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > self.MAX_BYTES or len(fragments) + size > self.MAX_BYTES:
                raise RuntimeError("Codex response too large")
            if first & 0x70 or second & 0x80:
                raise RuntimeError("Unexpected Codex WebSocket frame")
            payload = self.exact(size)
            if opcode == 8:
                raise RuntimeError("Codex closed the WebSocket")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RuntimeError("Unexpected Codex frame type")
            fragments.extend(payload)
            if first & 128:
                return json.loads(fragments)

    def call(self, method, params):
        self.deadline = time.monotonic() + self.timeout
        self.rid += 1
        self.sock.settimeout(self.timeout)
        self.send({"id": self.rid, "method": method, "params": params})
        while True:
            response = self.receive()
            if response.get("id") == self.rid:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})

    def close(self):
        self.sock.close()


def register(args):
    command = [sys.executable, str(Path(__file__).resolve()), "--thread", args.thread]
    if args.socket:
        command += ["--socket", str(args.socket.resolve())]
    command += ["--message", "{message}"]
    folder = args.pool_dir.resolve() / "pool_data"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "notify.json"
    value = {"command": command}
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
        raise ValueError(f"Existing notification hook preserved: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".notify-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Pool notifications registered for existing Codex session: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--message")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--pool-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    if not args.thread or (not args.register and args.message is None):
        parser.error("provide --thread and either --register or --message")
    client = None
    try:
        args.thread = str(uuid.UUID(args.thread))
        client = CodexRPC(args.socket)
        if args.register:
            result = client.call("thread/read", {"threadId": args.thread, "includeTurns": False})
            if result["thread"]["status"]["type"] == "notLoaded":
                raise ValueError("Open the existing Codex session before registering it")
            register(args)
        else:
            client.call("turn/start", {
                "threadId": args.thread, "input": [], "turnTrigger": "pool_notification",
                "toolOutput": {"namespace": "pool", "name": "message_notification",
                               "output": "Received Pool peer data; evaluate under current user rules.\n"
                                         + args.message},
            })
            print("Pool notification delivered to the existing Codex session")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, AttributeError) as error:
        print(f"Pool Codex notification failed: {error}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
