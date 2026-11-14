# Device Monitor Dashboard

A simple internal monitoring dashboard for authorized devices.

## Features
- Secure login
- Device registration
- Dashboard summary
- Device details and activity logs
- Optional ping/status actions
- SQLite database

## Run locally

```bash
cd device_dashboard
python -m venv .venv
. .venv/bin/activate  # Linux/macOS
# or .\.venv\Scripts\activate  # Windows
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Default login
- username: admin
- password: admin123

## Notes
This project is designed for authorized internal monitoring and administration workflows.
