# Secure Lab C2 Demo

This is a benign local security-lab prototype for a small encrypted command-relay setup. It demonstrates:

- a TCP bot listener
- a dashboard with login and session cookies
- encrypted JSON transport with ChaCha20
- command dispatch to connected agents
- remote update payloads with SHA-256 verification

It is intentionally limited to harmless, local, lab-only commands such as `info`, `echo`, `ls`, `pwd`, `ping`, `sleep`, `download`, `upload`, and `screenshot`.

## Requirements

```bash
python -m pip install cryptography pillow
```

## Run the server

```bash
cd c:\Users\AYMAN\Desktop\test1
set C2_KEY=0123456789abcdef0123456789abcdef
python c2_server.py
```

The dashboard is available at:

- http://localhost:8080/login
- login: `admin`
- password: `pass`

The bot listener runs on:

- 0.0.0.0:5555

## Run the client

In a second terminal:

```bash
cd c:\Users\AYMAN\Desktop\test1
set C2_KEY=0123456789abcdef0123456789abcdef
python client.py
```

The client will connect, send a handshake, and begin heartbeats.

## Useful commands from the dashboard

- `info`
- `echo` with args `{ "text": "hello" }`
- `ls` with args `{ "path": "." }`
- `pwd`
- `sleep` with args `{ "seconds": 1 }`
- `download` with args `{ "path": "demo.txt" }`
- `upload` with args `{ "path": "demo.txt", "b64": "..." }`
- `shell` with args `{ "cmd": "echo hello" }`

## Notes

- The key must match between the server and agents.
- The transport is length-prefixed and encrypted using ChaCha20.
- Sessions use an HttpOnly cookie with an 8-hour TTL.
- Updates are verified using SHA-256 before they are applied.

This is designed as a local lab and is not intended for unauthorized access or hostile operations.
