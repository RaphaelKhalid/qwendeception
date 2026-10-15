"""Tiny live-monitor server: serves the OLED dashboard and the event log.

    python monitor/server.py [--port 8787] [--results results]
"""

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent


def make_handler(results: Path):
    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/events":
                since = int(parse_qs(u.query).get("since", ["0"])[0])
                path = results / "events.jsonl"
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
                out = []
                for l in lines[since:]:
                    try:
                        out.append(json.loads(l))
                    except json.JSONDecodeError:
                        out.append({"msg": "(unparsable line)", "emoji": "❔"})
                return self._send(json.dumps({"next": len(lines), "events": out}).encode(), "application/json")
            self.send_error(404)

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--results", default=str(HERE.parent / "results"))
    a = ap.parse_args()
    print(f"monitor on http://localhost:{a.port}  (results: {a.results})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Path(a.results))).serve_forever()
