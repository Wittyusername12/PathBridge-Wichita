#!/usr/bin/env python3
"""P0.11b page scanner — chat-on pages through the proxy (R6 on Shane's machine, and live pages). Standard library only.
  python -B p011b_page_scan.py --root "C:\\Users\\shane\\OneDrive\\Documents"   (the four output\\index.html pages)
  python -B p011b_page_scan.py PAGE.html ...                                   (a chat page's chat-context\\ folder sits beside it)
  python -B p011b_page_scan.py https://ctrl-alt-defeat.netlify.app/ ...        (live: the context file is fetched from the same site)
Exit 0 = every page passes; 1 = a named failure; 2 = a page could not be read. Reads only; writes nothing."""
import argparse, hashlib, json, re, sys, urllib.parse, urllib.request
from pathlib import Path

READER_SHA256 = "1df1ad396edcdba94d9c8e28c687d825a353740996456479f848be03db5f78e4"   # BUILD IC-5 (D11): the reader's md() — IC-3's and IC-4's was 5861a83305f96cf1…
TRACKS = {"oracle-harness": "oracle", "architect-harness": "architect", "muse-harness": "muse", "landing-harness": None}
BAD = ["api.anthropic.com", "x-api-key", "anthropic-dangerous-direct-browser-access", "CHATBOT_KEY", "CHATBOT_CONTEXT",
       "initChatbot", "data-chat-key"]
MARK_S, MARK_E = "/* === CHAT READER (P0.11b)", "/* === END CHAT READER === */\n"
CFG = re.compile(r"<script>window\.CHAT=(\{.*?\});</script>")


def get(src):
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) p011b-check/1.0", "Cache-Control": "no-cache"})   # a browser-style UA: Cloudflare's BIC blocks Python-urllib
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    return Path(src).read_bytes()


def sibling(src, rel):
    return urllib.parse.urljoin(src, rel) if re.match(r"https?://", src) else str(Path(src).parent / rel)


def scan(src, track="auto"):
    """Return (page_chars, [named reasons]); an empty list means the page passes."""
    html = get(src).decode("utf-8", "replace")
    html = html.replace("\r\n", "\n").replace("\r", "\n")   # line endings are the OS's serialization (Windows write_text -> CRLF), not the page's content
    why = []
    if html.count("sk-ant"):
        why.append(f"key-shaped text x{html.count('sk-ant')}")
    why += [f"direct Anthropic code: {b}" for b in BAD if b in html]
    cfgs = CFG.findall(html)
    has_reader = MARK_S in html
    if track == "auto":
        track = json.loads(cfgs[0]).get("track") if len(cfgs) == 1 else (None if not cfgs and not has_reader else "?")
    if track is None:                                   # the hub: no chat expected
        if cfgs or has_reader:
            why.append("chat code on a page without chat")
        return len(html), why
    s, e = html.find(MARK_S), html.find(MARK_E)
    if s < 0 or e < s:
        why.append("reader block missing")
    elif hashlib.sha256(html[s:e + len(MARK_E)].encode("utf-8")).hexdigest() != READER_SHA256:
        why.append("reader block modified")
    if len(cfgs) != 1:
        why.append(f"window.CHAT count {len(cfgs)}")
        return len(html), why
    cfg = json.loads(cfgs[0])
    cid = str(cfg.get("context_id", ""))
    if cfg.get("track") != track:
        why.append(f"window.CHAT track {cfg.get('track')!r}, expected {track!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        why.append("context_id is not 64 lowercase hex")
        return len(html), why
    try:
        doc = get(sibling(src, f"chat-context/{cid}.json"))
    except Exception:                                   # noqa: BLE001
        why.append("context file missing")
        return len(html), why
    if hashlib.sha256(doc).hexdigest() != cid:
        why.append("context file digest mismatch")
    if len(doc) > 96 * 1024:
        why.append("context file over 96 KiB")
    try:
        d = json.loads(doc)
        if not (d.get("schema_version") == 1 and d.get("track") == track and isinstance(d.get("context"), str) and d["context"].strip()):
            why.append("context file schema or track wrong")
    except ValueError:
        why.append("context file is not JSON")
    return len(html), why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--root")
    a = ap.parse_args()
    jobs = [(t, "auto") for t in a.targets]
    if a.root:
        jobs += [(str(Path(a.root) / d / "output" / "index.html"), tr) for d, tr in TRACKS.items()]
    if not jobs:
        ap.error("give --root or at least one file or URL")
    worst = 0
    for src, track in jobs:
        try:
            n, why = scan(src, track)
        except Exception as ex:                          # noqa: BLE001
            print(f"UNREADABLE  {src}: {type(ex).__name__}: {ex}")
            worst = max(worst, 2)
            continue
        print(f"{'PASS' if not why else 'FAIL'}  {src}  ({n:,} chars){'  -> ' + '; '.join(why) if why else ''}")
        worst = max(worst, 1 if why else 0)
    print(f"\nP0.11b page scan: {'PASS' if worst == 0 else 'FAIL'}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
