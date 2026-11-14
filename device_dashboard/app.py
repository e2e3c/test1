from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'change-me-in-production-1234567890')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
DATABASE = os.path.join(os.path.dirname(__file__), 'monitor.db')


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            hostname TEXT,
            ip_address TEXT,
            os_name TEXT,
            status TEXT DEFAULT 'offline',
            last_seen TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(device_id) REFERENCES devices(id)
        )
    ''')

    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ('admin', generate_password_hash('admin123')),
        )
    conn.commit()
    conn.close()


@app.before_request
def ensure_db():
    init_db()


def login_required(view):
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session['user'] = user['username']
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    total_devices = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
    online_devices = conn.execute("SELECT COUNT(*) FROM devices WHERE status = 'online'").fetchone()[0]
    recent_events = conn.execute('''
        SELECT events.*, devices.name as device_name
        FROM events
        JOIN devices ON devices.id = events.device_id
        ORDER BY events.id DESC
        LIMIT 8
    ''').fetchall()
    devices = conn.execute('SELECT * FROM devices ORDER BY id DESC LIMIT 5').fetchall()
    conn.close()
    return render_template('dashboard.html', total_devices=total_devices, online_devices=online_devices, recent_events=recent_events, devices=devices)


@app.route('/devices', methods=['GET', 'POST'])
@login_required
def devices():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        hostname = request.form.get('hostname', '').strip()
        ip_address = request.form.get('ip_address', '').strip()
        os_name = request.form.get('os_name', '').strip()
        notes = request.form.get('notes', '').strip()

        if not name:
            flash('Device name is required', 'error')
            return redirect(url_for('devices'))

        conn = get_db()
        conn.execute(
            'INSERT INTO devices (name, hostname, ip_address, os_name, status, last_seen, notes) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (name, hostname or None, ip_address or None, os_name or None, 'online', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), notes or '')
        )
        device_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'INSERT INTO events (device_id, event_type, message) VALUES (?, ?, ?)',
            (device_id, 'registered', f"Device {name} registered in the dashboard.")
        )
        conn.commit()
        conn.close()
        flash('Device added successfully', 'success')
        return redirect(url_for('devices'))

    conn = get_db()
    rows = conn.execute('SELECT * FROM devices ORDER BY id DESC').fetchall()
    conn.close()
    return render_template('devices.html', devices=rows)


@app.route('/devices/<int:device_id>', methods=['GET', 'POST'])
@login_required
def device_detail(device_id):
    conn = get_db()
    device = conn.execute('SELECT * FROM devices WHERE id = ?', (device_id,)).fetchone()
    if not device:
        conn.close()
        return redirect(url_for('devices'))

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'ping':
            conn.execute(
                'INSERT INTO events (device_id, event_type, message) VALUES (?, ?, ?)',
                (device_id, 'ping', f"Ping sent to {device['hostname'] or device['name']} at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
            )
            conn.execute(
                'UPDATE devices SET status = ?, last_seen = ? WHERE id = ?',
                ('online', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), device_id)
            )
            conn.commit()
            flash('Ping sent successfully', 'success')
        elif action == 'mark_offline':
            conn.execute(
                'UPDATE devices SET status = ?, last_seen = ? WHERE id = ?',
                ('offline', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), device_id)
            )
            conn.execute(
                'INSERT INTO events (device_id, event_type, message) VALUES (?, ?, ?)',
                (device_id, 'status', 'Device marked offline manually.')
            )
            conn.commit()
            flash('Device marked offline', 'success')
        elif action == 'mark_online':
            conn.execute(
                'UPDATE devices SET status = ?, last_seen = ? WHERE id = ?',
                ('online', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), device_id)
            )
            conn.execute(
                'INSERT INTO events (device_id, event_type, message) VALUES (?, ?, ?)',
                (device_id, 'status', 'Device marked online manually.')
            )
            conn.commit()
            flash('Device marked online', 'success')

        conn.close()
        return redirect(url_for('device_detail', device_id=device_id))

    events = conn.execute('SELECT * FROM events WHERE device_id = ? ORDER BY id DESC LIMIT 20', (device_id,)).fetchall()
    conn.close()
    return render_template('device_detail.html', device=device, events=events)


@app.route('/api/devices', methods=['GET'])
@login_required
def api_devices():
    conn = get_db()
    rows = conn.execute('SELECT * FROM devices ORDER BY id DESC').fetchall()
    conn.close()
    return {'devices': [dict(r) for r in rows]}


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), debug=False)
