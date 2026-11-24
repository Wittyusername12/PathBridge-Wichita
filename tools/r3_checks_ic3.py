"""BUILD IC-3 (§7 R3) — three checks with no model call, under the Python that runs the pipeline, on the installed tree (--root, the
folder that holds oracle-harness):
  probe   tools/sdk_usage_probe.py beside this file: two words — what the raw message_delta keeps of the thinking count, and what the
          SDK's own accumulator keeps (no client, no call)
  speed   the installed reader (oracle-harness/naipc_registry.quantities) on the page in oracle-harness/output/index.html — its text
          and its raw HTML — and on 175,000 adversarial characters, each in a fresh process with a 10 s limit (D12.12, done-test 22;
          the adversarial text is tests/t_reader_ic3.py's)
  pings   the installed call helper (oracle-harness/naipc_requirements.model_call) against a stream served here on 127.0.0.1 that
          starts and then sends only pings for 6 s, with an end time 1.5 s away: the timer ends it (EndTimeReached) in under 4 s, and
          the Oracle's retry predicate (oracle_harness.is_retryable) does not retry it (D5, done-test 15). The client is made for that
          local stream only, with a placeholder key; no environment key is read and nothing leaves the machine.
Prints one line per check and "R3: <n> of 3 PASS". Writes only to a temporary folder (the helper's call log), which it removes.
Usage: python -I -B r3_checks_ic3.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import http.server, json, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
OH = ROOT / "oracle-harness"
HERE = Path(__file__).resolve().parent
LIMIT = 10.0
ADV_PARTS = ["12345, " * 40 + "1234x ", "1-2-3-4-5-6-7-8-9-" * 30 + " ", "one " * 200 + "and ", "twenty-" * 100 + "x ", "1,1,1,1,1,1," * 60 + " ",
             "Years 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, " * 20 + "more staff ", "((((((1))))))" * 50 + " ", "$$$$$1 " * 50, "about about about about 3 " * 40,
             "8 - 12 = 8 - 12 = " * 40, "a third of a third of " * 50, "every every every third " * 40, "1/2/3/4/5/6 " * 60, "9-1-1-1-1-1-1 " * 40,
             "Mon-Fri 8am-4pm, " * 60, "between 1 and 2 and 3 and 4 and " * 40, "per per per 10,000 " * 40, "1st 2nd 3rd 4th " * 60, ".5.5.5.5.5 " * 60,
             "Route 66 " * 80, "aged 16 to 24 or 25 or 26 or " * 40, "    " * 500, "(1) a (2) b (3) c " * 60,
             "one hundred and one hundred and one hundred and " * 40, "ZIPs 64110, 64111, 64112, 64113, " * 40, "1 1 1 1 1 1 1 1 " * 60,
             "and and and and " * 60, "twenty one twenty one twenty one " * 40, "$1.2M-$3.4M-$5.6M " * 40]


def adversarial(n=175_000):
    s, i = "", 0
    while len(s) < n:
        s += ADV_PARTS[i % len(ADV_PARTS)]
        i += 1
    return s[:n]


def timed(text, page_text=False):
    code = ("import sys, time, json\nsys.path.insert(0, %r)\nimport naipc_registry as R\nt = sys.stdin.read()\n"
            + ("t = R.scan(t)[0]\n" if page_text else "")
            + "t0 = time.perf_counter(); r = R.quantities(t); print(json.dumps({'n': len(r), 's': round(time.perf_counter() - t0, 2)}))") % str(OH)
    try:
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code], input=text, capture_output=True, text=True, encoding="utf-8", timeout=LIMIT + 20)
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {"error": (p.stderr or "")[-160:]}
    except subprocess.TimeoutExpired:
        return {"error": "no answer in time"}


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Pings(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        head = (sse("message_start", {"type": "message_start", "message": {"id": "msg_local", "type": "message", "role": "assistant",
                                                                           "model": "local-stream", "content": [], "stop_reason": None,
                                                                           "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})
                + sse("content_block_start", {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
        try:
            self.wfile.write(head)
            self.wfile.flush()
            for _ in range(24):                   # 6 s of pings, one each 0.25 s
                time.sleep(0.25)
                self.wfile.write(sse("ping", {"type": "ping"}))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


rows = []
p = subprocess.run([sys.executable, "-I", "-B", str(HERE / "sdk_usage_probe.py")], capture_output=True, text=True, encoding="utf-8", timeout=120)
probe = p.stdout.strip()
rows.append(("probe", p.returncode == 0 and probe.startswith("raw:"), probe or (p.stderr or "")[-160:]))

page = OH / "output" / "index.html"
if page.is_file():
    ht = page.read_text(encoding="utf-8")
    sp = {"page text": timed(ht, page_text=True), "page html": timed(ht), "adversarial": timed(adversarial())}
    ok = all("s" in v and v["s"] < LIMIT for v in sp.values())
    rows.append(("speed", ok, f"oracle-harness/output/index.html ({len(ht.encode('utf-8')):,} B): "
                 + "; ".join(f"{k} {v.get('s', v.get('error'))}" + (" s" if "s" in v else "") for k, v in sp.items()) + f" (limit {LIMIT:g} s each)"))
else:
    rows.append(("speed", False, f"no page at {page}"))

sys.path.insert(0, str(OH))
try:
    import anthropic, httpx  # noqa: E401
    import naipc_requirements as NR
    import oracle_harness as O
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pings)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cl = anthropic.Anthropic(api_key="placeholder-local-stream", base_url=f"http://127.0.0.1:{srv.server_address[1]}", max_retries=0,
                             timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
    tmp = Path(tempfile.mkdtemp(prefix="naipc-r3-"))
    params = {"model": "local-stream", "max_tokens": 16, "messages": [{"role": "user", "content": "local stream"}]}
    t0, err = time.monotonic(), None
    try:
        NR.model_call(cl, params, label="r3", attempt=1, log_dir=tmp, clock=time.monotonic, end_at=t0 + 1.5, stream=True, timer=threading.Timer)
    except Exception as e:  # noqa: BLE001
        err = e
    took = time.monotonic() - t0
    line = json.loads((tmp / NR.CALL_LOG).read_text(encoding="utf-8").splitlines()[0]) if (tmp / NR.CALL_LOG).is_file() else {}
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    ok = type(err).__name__ == "EndTimeReached" and took < 4.0 and not O.is_retryable(err)
    rows.append(("pings", ok, f"{type(err).__name__} after {took:.1f} s ({line.get('ended_by')}); retried: {O.is_retryable(err) if err else '-'}"))
except Exception as e:  # noqa: BLE001
    rows.append(("pings", False, f"{type(e).__name__}: {str(e)[:160]}"))

for name, ok, what in rows:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<6} {what}")
print(f"R3: {sum(1 for _n, ok, _w in rows if ok)} of {len(rows)} PASS")
sys.exit(0 if all(ok for _n, ok, _w in rows) else 1)
