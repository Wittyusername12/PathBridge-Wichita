#!/usr/bin/env python3
"""P0.11a fixture API server: serves recorded challenge endpoints from a folder of <endpoint>.json files at both
/<endpoint> (the Oracle's URL shape, oracle_harness.py:445) and /<endpoint>/ (Architect 67, Muse 300); GET / lists them.
127.0.0.1 only. Usage: python -B fixture_server.py --dir fixtures [--port 8765]    (Ctrl+C stops it)
P0.8b r2 (2026-10-22): refuses to start when something already answers on the port (exit 2, with the netstat/taskkill hint) —
on Windows, HTTPServer's SO_REUSEADDR lets a second server bind a port a forgotten server still holds, and the old one keeps
answering (found at R6d: a 10/19 server served the pre set to a pre-flight meant for post); the banner's second line names each
alert endpoint's status and ids, so the set being served is visible at a glance (a count never identifies a version)."""
import argparse, http.server, json, socket, sys, threading
from pathlib import Path


def port_answers(port, host="127.0.0.1"):
    """True when something already accepts connections on host:port (a forgotten server, or another program)."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def alert_lines(folder):
    """One line per endpoint whose payload carries an 'alerts' list: '<name>: <status> — N alert(s): <ids>'."""
    out = []
    for p in sorted(Path(folder).glob("*.json")):
        d = json.loads(p.read_bytes().decode("utf-8-sig"))
        if isinstance(d, dict) and isinstance(d.get("alerts"), list):
            ids = ", ".join(str(a.get("id")) for a in d["alerts"] if isinstance(a, dict))
            out.append(f"  {p.stem}: {d.get('status')} — {len(d['alerts'])} alert(s): {ids}")
    return out


def make_server(folder, port=0):
    folder = Path(folder)
    data = {p.stem: p.read_bytes() for p in sorted(folder.glob("*.json"))}
    for name, raw in data.items():
        json.loads(raw)                                   # every fixture must be JSON

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ep = self.path.split("?")[0].strip("/").split("/")[-1] if self.path.strip("/") else ""
            body = json.dumps({"endpoints": sorted(data)}).encode() if ep == "" else data.get(ep)
            if body is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), H), sorted(data)


def start_in_thread(folder, port=0):
    srv, eps = make_server(folder, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/", eps


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--dir", required=True); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if port_answers(a.port):
        print(f"REFUSED: something already answers on http://127.0.0.1:{a.port}/ — a fixture server (or another program) is still running"
              f" and would keep answering. Find it:   netstat -ano | findstr \":{a.port} \"   then close that window or:   taskkill /PID <pid> /F")
        sys.exit(2)
    srv, eps = make_server(a.dir, a.port)
    print(f"Serving {len(eps)} endpoints at http://127.0.0.1:{a.port}/  ->  --endpoints {','.join(eps)}")
    for line in alert_lines(a.dir):
        print(line)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
