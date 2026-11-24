#!/usr/bin/env python3
"""P0.11b R7 — serve a rendered page folder at http://127.0.0.1:8766, the one origin the DEV Worker accepts, for the browser
chat test. Standard library only; files from that folder only; reachable from this computer only.

  python -B p011b_localserve.py --dir "%USERPROFILE%\\OneDrive\\Documents\\oracle-harness\\test_run\\output"

Open http://127.0.0.1:8766/ (not "localhost": the browser would send a different Origin and the DEV Worker would refuse it).
Ctrl+C stops the server."""
import argparse, functools, http.server, json, re, sys, webbrowser
from pathlib import Path


def page_facts(folder):
    html = (folder / "index.html").read_text(encoding="utf-8", errors="replace")
    cfg = re.findall(r"<script>window\.CHAT=(\{.*?\});</script>", html)
    if len(cfg) != 1:
        return None, None, False
    c = json.loads(cfg[0])
    cid = str(c.get("context_id", ""))
    return c.get("proxy"), cid, (folder / "chat-context" / f"{cid}.json").is_file()


def main(argv=None):
    ap = argparse.ArgumentParser(description="P0.11b R7 local page server")
    ap.add_argument("--dir", required=True, help="a rendered page folder (index.html + chat-context\\)")
    ap.add_argument("--port", type=int, default=8766, help="8766 = the DEV Worker's allowed origin; change only for tests")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    folder = Path(a.dir).resolve()
    if not (folder / "index.html").is_file():
        print(f"no index.html in {folder}")
        return 2
    proxy, cid, has_ctx = page_facts(folder)
    if proxy is None:
        print("this page has no chat configuration (window.CHAT) — render it with the P0.11b harness first")
        return 2
    print(f"page proxy: {proxy}{'  <- DEV Worker' if 'naipc-chat-dev' in proxy else '  <- NOT the DEV Worker: re-render with set CHAT_PROXY_URL=<DEV URL>'}")
    print(f"context file: {'present' if has_ctx else 'MISSING'} ({cid[:16]}…)")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Serving {folder} at {url}  (Ctrl+C stops)", flush=True)
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
