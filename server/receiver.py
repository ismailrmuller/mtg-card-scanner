#!/usr/bin/env python3
"""
MTG Card Scanner — Receiver Server
Run:  python receiver.py
Listens on http://0.0.0.0:8765

Endpoints:
  POST /upload        multipart/form-data; receives JPEG, saves + thumbnails
  GET  /status        JSON { received, last_file, uptime_s }
  GET  /              dark-theme HTML dashboard, auto-refreshes every 3 s
  GET  /scans/thumbs/<file>   serve thumbnail
"""

import http.server
import socketserver
import os
import json
import time
from pathlib import Path
from datetime import datetime
from io import BytesIO

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

PORT      = 8765
SCAN_DIR  = Path("scans")
THUMB_DIR = SCAN_DIR / "thumbs"
THUMB_SIZE = (200, 140)
START_TIME = time.time()

_stats = {"received": 0, "last_file": ""}


# ── Multipart parser ───────────────────────────────────────────────────────

def _parse_multipart(body: bytes, boundary: bytes):
    """Return (filename, jpeg_bytes) from a multipart body, or (None, None)."""
    delim = b"--" + boundary
    parts = body.split(delim)
    for part in parts[1:]:
        stripped = part.lstrip(b"\r\n")
        if stripped.startswith(b"--"):  # closing boundary
            break
        sep = part.find(b"\r\n\r\n")
        if sep == -1:
            continue
        headers_raw = part[2:sep]       # skip leading \r\n
        content = part[sep + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]

        filename = None
        for line in headers_raw.split(b"\r\n"):
            if b"Content-Disposition" in line and b"filename=" in line:
                idx = line.find(b'filename="')
                if idx != -1:
                    s = idx + 10
                    e = line.find(b'"', s)
                    if e > s:
                        filename = line[s:e].decode("utf-8", errors="replace")
        if filename and content:
            return filename, content
    return None, None


# ── Request handler ────────────────────────────────────────────────────────

class CardHandler(http.server.BaseHTTPRequestHandler):

    # ── routing ──────────────────────────────────────────────────────────

    def do_POST(self):
        if self.path == "/upload":
            self._upload()
        else:
            self.send_error(404)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self._index()
        elif p == "/status":
            self._status()
        elif p.startswith("/scans/thumbs/"):
            self._serve_file(THUMB_DIR / Path(p).name, "image/jpeg")
        elif p.startswith("/scans/"):
            self._serve_file(SCAN_DIR / Path(p).name, "image/jpeg")
        else:
            self.send_error(404)

    # ── handlers ─────────────────────────────────────────────────────────

    def _upload(self):
        ct = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ct:
            self.send_error(400, "Expected multipart/form-data"); return

        boundary = ""
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip(); break
        if not boundary:
            self.send_error(400, "Missing boundary"); return

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self.send_error(400, "Empty body"); return

        body = self.rfile.read(length)
        filename, jpeg = _parse_multipart(body, boundary.encode())
        if not filename or not jpeg:
            self.send_error(400, "Could not parse file"); return

        # Sanitise: strip any path component the client might inject
        filename = Path(filename).name
        if not filename.lower().endswith(".jpg"):
            filename += ".jpg"

        dest = SCAN_DIR / filename
        dest.write_bytes(jpeg)

        if HAS_PILLOW:
            try:
                img = Image.open(BytesIO(jpeg))
                img.thumbnail(THUMB_SIZE)
                img.save(str(THUMB_DIR / filename), "JPEG", quality=80)
            except Exception as exc:
                print(f"  thumbnail error: {exc}")

        _stats["received"] += 1
        _stats["last_file"] = filename
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}]  #{_stats['received']:>4}  {filename}  {len(jpeg)/1024:>7.1f} KB")

        self._text(200, "OK")

    def _status(self):
        data = {
            "received":  _stats["received"],
            "last_file": _stats["last_file"],
            "uptime_s":  int(time.time() - START_TIME),
        }
        self._json(200, data)

    def _index(self):
        uptime_s = int(time.time() - START_TIME)
        h, r = divmod(uptime_s, 3600)
        m, s = divmod(r, 60)
        uptime = f"{h:02d}:{m:02d}:{s:02d}"
        lf = _stats["last_file"]
        thumb_block = (
            f'<img src="/scans/thumbs/{lf}" alt="last scan" class="thumb">'
            if lf else
            '<p class="none">no scan yet</p>'
        )
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="3">
<title>MTG Card Scanner</title>
<style>
  :root {{
    --bg:#1a1a2e; --panel:#16213e; --accent:#e94560;
    --text:#eaeaea; --dim:#888; --border:#0f3460;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{
    background:var(--bg);color:var(--text);
    font-family:'Segoe UI',system-ui,sans-serif;
    display:flex;flex-direction:column;align-items:center;
    min-height:100vh;padding:2.5rem 1rem;
  }}
  h1{{font-size:2rem;letter-spacing:.03em}}
  h1 span{{color:var(--accent)}}
  .sub{{color:var(--dim);font-size:.88rem;margin:.35rem 0 2.5rem}}
  .grid{{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:1.5rem;width:100%;max-width:860px;
  }}
  .card{{
    background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:1.75rem;text-align:center;
  }}
  .big{{font-size:3.5rem;font-weight:700;color:var(--accent);line-height:1}}
  .label{{color:var(--dim);font-size:.82rem;margin-top:.4rem;text-transform:uppercase;letter-spacing:.06em}}
  .thumb{{max-width:100%;border-radius:8px;margin-top:.75rem;border:2px solid var(--border)}}
  .fname{{font-size:.72rem;color:var(--dim);margin-top:.5rem;word-break:break-all}}
  .none{{color:var(--dim);padding:1.5rem 0;font-size:.9rem}}
</style>
</head>
<body>
<h1>MTG Card <span>Scanner</span></h1>
<p class="sub">auto-refresh every 3 s &nbsp;&middot;&nbsp; uptime {uptime}</p>
<div class="grid">
  <div class="card">
    <div class="big">{_stats["received"]}</div>
    <div class="label">cards scanned</div>
  </div>
  <div class="card">
    {thumb_block}
    <div class="fname">{lf if lf else "&mdash;"}</div>
    <div class="label">last capture</div>
  </div>
</div>
</body>
</html>"""
        self._bytes(200, html.encode(), "text/html; charset=utf-8")

    def _serve_file(self, path: Path, mime: str):
        if not path.is_file():
            self.send_error(404); return
        self._bytes(200, path.read_bytes(), mime)

    # ── response helpers ──────────────────────────────────────────────────

    def _text(self, code, msg):
        self._bytes(code, msg.encode(), "text/plain")

    def _json(self, code, data):
        self._bytes(code, json.dumps(data).encode(), "application/json")

    def _bytes(self, code, data: bytes, mime: str):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass    # suppress BaseHTTPRequestHandler default log lines


# ── Server ─────────────────────────────────────────────────────────────────

class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    SCAN_DIR.mkdir(exist_ok=True)
    THUMB_DIR.mkdir(exist_ok=True)

    print("=" * 52)
    print("  MTG Card Scanner — Receiver")
    print(f"  http://0.0.0.0:{PORT}")
    print(f"  Saving to : {SCAN_DIR.resolve()}")
    print(f"  Thumbnails: {'enabled (Pillow)' if HAS_PILLOW else 'DISABLED  →  pip install Pillow'}")
    print("=" * 52)

    with ThreadingServer(("", PORT), CardHandler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print(f"\nShutdown — {_stats['received']} cards in this session")
