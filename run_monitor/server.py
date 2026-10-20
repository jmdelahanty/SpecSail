"""
Local web server for the run monitor.

Serves static/index.html and three JSON endpoints computed from the bundle:
/data/run_log.json, /data/spectra.json and /data/library.json. Results are
cached until the run's log.jsonl changes (size or mtime), or for a few seconds
for the status row, so polling browsers don't re-read every spectrum.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from run_monitor import export

STATIC = Path(__file__).parent / "static"
STATUS_TTL_S = 5.0


class MonitorData:
    """Computes and caches the JSON payloads for one run (plus optional library runs)."""

    def __init__(self, run_dir, library_runs=None):
        self.run_dir = Path(run_dir)
        self.library_runs = list(library_runs) if library_runs else [("this run", self.run_dir)]
        self._lock = threading.Lock()
        self._cache = {}

    def _log_stamp(self):
        log = self.run_dir / "log.jsonl"
        try:
            st = log.stat()
        except OSError:
            return None
        sealed = (self.run_dir / "capture_context.json").exists()
        return (st.st_size, st.st_mtime_ns, sealed)

    def payload(self, name):
        builders = {
            "run_log": lambda: export.run_log_rows(self.run_dir),
            "spectra": lambda: export.spectra_rows(self.run_dir),
            "library": lambda: export.library_rows(self.library_runs),
        }
        if name not in builders:
            raise KeyError(name)
        stamp = self._log_stamp()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(name)
            fresh = hit and hit[0] == stamp and (name != "run_log" or now - hit[1] < STATUS_TTL_S)
            if fresh:
                return hit[2]
        body = json.dumps(builders[name](), separators=(",", ":")).encode()
        with self._lock:
            self._cache[name] = (stamp, now, body)
        return body


def make_handler(data):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/data/") and path.endswith(".json"):
                name = path[len("/data/"):-len(".json")]
                try:
                    body = data.payload(name)
                except KeyError:
                    self._send(404, b'{"error":"unknown dataset"}', "application/json")
                    return
                except Exception as exc:   # report, don't kill the server
                    self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                self._send(200, body, "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):   # keep the terminal quiet
            pass

    return Handler


def make_server(run_dir, library_runs=None, host="127.0.0.1", port=8765):
    data = MonitorData(run_dir, library_runs)
    return ThreadingHTTPServer((host, port), make_handler(data))
