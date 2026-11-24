#!/usr/bin/env python3
"""P0.11a T6 page scanner — no key and no chat remnants in built or live pages. Standard library only.

  python -B p011a_page_scan.py --root "C:\\Users\\shane\\OneDrive\\Documents"      (the four output\\index.html pages)
  python -B p011a_page_scan.py https://ctrl-alt-defeat.netlify.app/ page.html ...  (any URLs or files)
Exit 0 = every page clean; 1 = something found (named per page); 2 = a page could not be read.
"""
import argparse, re, sys, urllib.request
from pathlib import Path

TRACKS = ["oracle-harness", "architect-harness", "muse-harness", "landing-harness"]
MUST_BE_ZERO = ["sk-ant", "data-chat-key", 'class="ask-ai-btn"', "api.anthropic.com", "x-api-key",
                "anthropic-dangerous-direct-browser-access", "CHATBOT_KEY", "CHATBOT_CONTEXT", "initChatbot"]
CHAT_IDS = ["chatFab", "chatPanel", "chatClose", "chatMessages", "chatInput", "chatSend",
            "chat-toggle", "chatbot", "chat-head", "chat-body", "chat-q", "chat-send", "chat-tip"]


def read(src):
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "p011a-page-scan", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    return Path(src).read_text(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("targets", nargs="*"); ap.add_argument("--root")
    a = ap.parse_args()
    targets = list(a.targets) + ([str(Path(a.root) / t / "output" / "index.html") for t in TRACKS] if a.root else [])
    if not targets:
        ap.error("give --root or at least one file/URL")
    worst = 0
    for src in targets:
        try:
            html = read(src)
        except Exception as e:                                           # noqa: BLE001
            print(f"UNREADABLE  {src}: {type(e).__name__}: {e}"); worst = max(worst, 2); continue
        found = [f"{s} x{html.count(s)}" for s in MUST_BE_ZERO if s in html]
        found += [f'id="{i}"' for i in CHAT_IDS if re.search(rf'id=["\']{re.escape(i)}["\']', html)]
        print(f"{'CLEAN' if not found else 'FOUND'}  {src}  ({len(html):,} chars){'  -> ' + ', '.join(found) if found else ''}")
        worst = max(worst, 1 if found else 0)
    print(f"\nT6 page scan: {'PASS' if worst == 0 else 'FAIL'}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
