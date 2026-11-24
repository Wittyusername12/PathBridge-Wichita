#!/usr/bin/env python3
"""P0.11b T4 smoke — three exchanges through a chat Worker, sent exactly as the page sends them (requests of 1, 3 and 5
messages: the proxy accepts only odd, user-first conversations). Standard library only; holds no key and prints none.

  python -B p011b_smoke.py --url https://naipc-chat-dev.swaldrop2.workers.dev --context-file "<...>\\test_run\\output\\chat-context"

Per exchange: HTTP status, request id, time to first text, total time, stop_reason, input/output tokens, cut off?
Exit 0 = all three ended with end_turn · 1 = a named failure · 2 = bad arguments."""
import argparse, codecs, hashlib, json, sys, time, urllib.error, urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) p011b-check/1.0"   # workers.dev blocks Python-urllib (Cloudflare 1010)

QUESTIONS = ["In one sentence, what is this program?",
             "What is its social return on investment, and where does that number come from?",
             "Name one risk and how it is mitigated."]


def exchange(url, origin, cid, messages):
    body = json.dumps({"context_id": cid, "messages": messages}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Origin": origin, "User-Agent": UA})
    res = {"status": None, "rid": None, "build": None, "ttft": None, "total": None, "stop": None, "in": None, "out": None, "text": "", "error": None}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res["status"], res["rid"], res["build"] = r.status, r.headers.get("X-Request-ID"), r.headers.get("X-Build")
            dec, buf, done = codecs.getincrementaldecoder("utf-8")(), "", False
            while not done:
                chunk = r.read1(4096)
                final = not chunk
                buf += dec.decode(chunk, final=final)
                keep = not final and buf.endswith("\r")
                s = (buf[:-1] if keep else buf).replace("\r\n", "\n").replace("\r", "\n")
                frames = s.split("\n\n")
                rest = frames.pop()
                if final and rest.strip():
                    frames.append(rest)
                    rest = ""
                buf = rest + ("\r" if keep else "")
                for f in frames:
                    ev, data = "", []
                    for line in f.split("\n"):
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                    if not data:
                        continue
                    try:
                        o = json.loads("\n".join(data))
                    except ValueError:
                        continue
                    t = o.get("type")
                    if ev == "error" or t == "error":
                        res["error"] = (o.get("error") or {}).get("code") or "stream_error"
                        done = True
                    elif t == "message_start":
                        res["in"] = ((o.get("message") or {}).get("usage") or {}).get("input_tokens")
                    elif t == "content_block_delta" and (o.get("delta") or {}).get("type") == "text_delta":
                        if res["ttft"] is None:
                            res["ttft"] = time.monotonic() - t0
                        res["text"] += o["delta"].get("text", "")
                    elif t == "message_delta":
                        res["stop"] = (o.get("delta") or {}).get("stop_reason")
                        res["out"] = (o.get("usage") or {}).get("output_tokens")
                    elif t == "message_stop":
                        done = True
                if final:
                    done = True
    except urllib.error.HTTPError as e:
        res["status"], res["rid"], res["build"] = e.code, e.headers.get("X-Request-ID"), e.headers.get("X-Build")
        try:
            res["error"] = (json.loads(e.read()).get("error") or {}).get("code")
        except ValueError:
            res["error"] = "non-JSON error body"
        ra = e.headers.get("Retry-After")
        if ra:
            res["error"] += f" (Retry-After {ra} s)"
    except (urllib.error.URLError, OSError) as e:
        res["error"] = f"unreachable: {getattr(e, 'reason', e)}"
    res["total"] = time.monotonic() - t0
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="P0.11b T4 smoke (three exchanges through a chat Worker)")
    ap.add_argument("--url", required=True, help="the Worker URL, e.g. https://naipc-chat-dev.swaldrop2.workers.dev")
    ap.add_argument("--context-file", help="the chat-context folder (or its JSON file) the Worker serves; its sha256 is the context_id")
    ap.add_argument("--context-id", help="the context_id instead of a file")
    ap.add_argument("--origin", default="http://127.0.0.1:8766", help="the Origin header (DEV: http://127.0.0.1:8766)")
    a = ap.parse_args(argv)
    if a.context_file:
        cp = Path(a.context_file)
        if cp.is_dir():                                               # the chat-context folder: use its one file
            found = sorted(cp.glob("*.json"))
            if len(found) != 1:
                print(f"{cp} holds {len(found)} .json files; expected exactly one")
                return 2
            cp = found[0]
        cid = hashlib.sha256(cp.read_bytes()).hexdigest()
    elif a.context_id and len(a.context_id) == 64:
        cid = a.context_id
    else:
        print("give --context-file (or a 64-hex --context-id)")
        return 2
    print(f"T4 smoke: {a.url}  origin {a.origin}  context {cid[:16]}…")
    messages, ok = [], True
    for i, q in enumerate(QUESTIONS, 1):
        messages.append({"role": "user", "content": q})
        r = exchange(a.url, a.origin, cid, messages)
        cut = r["stop"] == "max_tokens"
        ttft = f"{r['ttft']:.2f} s" if r["ttft"] is not None else "-"
        print(f"  exchange {i} ({len(messages)} msg): HTTP {r['status']} · build {r['build']} · id {r['rid']} · TTFT {ttft} · total {r['total']:.2f} s · "
              f"stop {r['stop']} · tokens in {r['in']} / out {r['out']} · cut off: {'YES' if cut else 'no'}"
              + (f" · ERROR {r['error']}" if r["error"] else ""))
        if r["status"] != 200 or r["error"] or r["stop"] != "end_turn" or not r["text"].strip():
            ok = False
            break
        print(f"      answer: {r['text'][:140].replace(chr(10), ' ')}{'…' if len(r['text']) > 140 else ''}")
        messages.append({"role": "assistant", "content": r["text"]})
    print(f"\nT4 smoke: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
