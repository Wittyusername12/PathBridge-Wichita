"""BUILD IC-6 (§7 R5) — the continuation probe: one paid call cut at its limit, then its continuation, through the installed tree's own
path (naipc_requirements.line_call over naipc_requirements.model_call, as the Oracle's and the Muse's updates call it). About $0.07.
The key is read from the environment as the harnesses read it (ANTHROPIC_API_KEY) and never printed: only "set" or "not set".
The call: asked for 400 JSON lines ({"id": "n<k>", "word": …}, k = 1 to 400, in order), effort low, max_tokens 1,500 — cut at its limit
long before line 400. NR.line_call keeps its complete lines and asks one continuation: the original user message (the same bytes), an
assistant message holding the complete lines, and a user message (NR.CONTINUE_WORDS: go on from the next id) — never a prefill. The
continuation has the same limit, so it may be cut in its turn; its complete lines count.
It passes when: the first call stopped at its limit with complete lines kept; the continuation's request was accepted (it brought text:
end_turn, or cut at its own limit); its first new line is the one after the last kept (n<k+1>); and no id is written twice (none dropped as
a repeat, none twice in the merged lines). Else it says which failed — and §7 R5: NAIPC_IC6_CONTINUE=0 for the rest of the run turn.
Its call log (two lines: probe, probe+c — times, counts, the stop reason; never a prompt, a reply, a key or another header) and the two
journals go to a new folder beside the four: <root>/naipc-r5-ic6-<UTC stamp>; nothing else is written.
Usage: python -I -B probe_continue_ic6.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import json, os, re, sys, threading, time
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
OH = ROOT / "oracle-harness"
sys.path.insert(0, str(OH))
import naipc_requirements as NR  # noqa: E402

KEY = os.environ.get("ANTHROPIC_API_KEY", "")
print(f"R5 ANTHROPIC_API_KEY: {'set' if KEY.strip() else 'not set'}")
if not KEY.strip():
    print("R5 not run: no key in the environment (set it as for a live run, then run this again)")
    sys.exit(2)
import anthropic  # noqa: E402
import httpx  # noqa: E402

MODEL = re.search(r'^OPUS = "([^"]+)"', (OH / "oracle_harness.py").read_text(encoding="utf-8"), re.M).group(1)
N, LIMIT = 400, 1500
SYSTEM = "Output ONLY JSON Lines — one JSON object on each line, no markdown, no explanation, no code fences."
PROMPT = (f"Write {N} lines. Line k (k = 1 to {N}, in order) is one JSON object with two keys: \"id\", the text \"n\" followed by k "
          "(\"n1\", \"n2\", …), and \"word\", an English word of five letters. Nothing else.")
PROFILE = {"key": "probe/continue", "label": "probe", "form": "lines", "first_line_s": 60.0, "planning_tokens": LIMIT, "max_tokens": LIMIT,
           "effort": "low", "stream": True}
WORK = ROOT / f"naipc-r5-ic6-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
assert not WORK.exists(), WORK
WORK.mkdir()
client = anthropic.Anthropic(api_key=KEY, max_retries=0, timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
KEY = None                                   # the client holds it; nothing here keeps or prints it


class MaxTokensReached(RuntimeError):        # NR.cut_kind reads the class's name: "tokens"
    pass


class ModelRefused(RuntimeError):            # "refusal": its lines are never kept
    pass


ATT = {}


def send(msgs, label):
    ATT[label] = ATT.get(label, 0) + 1
    msg, text = NR.model_call(client, {"model": MODEL, "max_tokens": LIMIT, "output_config": NR.output_config("low"), "system": SYSTEM,
                                       "messages": msgs}, label=label, attempt=ATT[label], log_dir=WORK, clock=time.monotonic,
                              end_at=time.monotonic() + 300, stream=True, timer=threading.Timer,
                              journal=WORK / f"journal_{label.replace('+', '-')}_{ATT[label]}.txt")
    if msg.stop_reason == "max_tokens":
        raise MaxTokensReached(f"the answer hit max_tokens ({LIMIT})")
    if msg.stop_reason == "refusal":
        raise ModelRefused("declined")
    return text


def resume(objs):
    ids = [str(o.get("id")) for o in objs if o.get("id") is not None]
    last = ids[-1] if ids else "the last complete line"
    m = re.fullmatch(r"n(\d+)", last)
    return last, (f"n{int(m.group(1)) + 1}" if m else None), 1


def refuse(words):
    raise RuntimeError(words)


res = NR.line_call(PROFILE, send, PROMPT, end_at=time.monotonic() + 600, clock=time.monotonic, retryable=lambda e: False,
                   retry_wait=lambda e, a: 0.0, sleep=time.sleep, refuse=refuse, log_dir=WORK, model=MODEL, begins=True,
                   key_of=lambda o: str(o.get("id")) if o.get("id") is not None else None, resume=resume, continue_ok=True,
                   stage_label="the probe")
first_ids = []
for ln in (res.get("text") or "").splitlines():
    try:
        o = json.loads(ln)
    except ValueError:
        continue
    if isinstance(o, dict) and o.get("id") is not None:
        first_ids.append(str(o["id"]))
c = res.get("continuation") or {}
kept = res.get("kept") or 0
last_kept = first_ids[kept - 1] if 0 < kept <= len(first_ids) else None
new_ids = first_ids[kept:]
want_next = resume([{"id": last_kept}])[1] if last_kept else None
checks = {
    "the first call stopped at its limit, its complete lines kept": res.get("kind") == "tokens" and kept > 0,
    "the continuation's request accepted (it brought text)": bool(c.get("label")) and (c.get("how") == "end_turn" or str(c.get("how")).startswith("cut:"))
    and c.get("lines", 0) > 0,
    "its first new line is the one after the last kept": bool(new_ids) and new_ids[0] == want_next,
    "no line written twice": not c.get("dropped") and len(first_ids) == len(set(first_ids)),
}
lines_ = [json.loads(x) for x in (WORK / "call_log.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
    if (WORK / "call_log.jsonl").is_file() else []
print(f"R5 the first call: {res.get('how')} — {kept} complete line(s) kept, the last {last_kept}")
print(f"R5 the continuation: {c.get('how') or 'not asked'} — {c.get('lines', 0)} line(s) more, the first {new_ids[0] if new_ids else None} "
      f"(the one after {last_kept}: {want_next}); written again and dropped: {len(c.get('dropped') or [])}")
print("R5 the call log: " + "; ".join(f"{r.get('label')} stop {r.get('stop_reason')}, {(r.get('usage') or {}).get('output_tokens')} output tokens, "
                                       f"{r.get('t_end')} s" for r in lines_))
for k, ok in checks.items():
    print(f"R5 {'PASS' if ok else 'FAIL'} {k}")
ok_all = all(checks.values())
print("R5 " + ("PASS: the continuation works — NAIPC_IC6_CONTINUE stays on" if ok_all else
               "FAIL: set NAIPC_IC6_CONTINUE=0 for the rest of the run turn (§7 R5)") + f"; the probe's folder: {WORK}")
(WORK / "probe_result.json").write_text(json.dumps({"checks": checks, "kept": kept, "last_kept": last_kept, "continuation": {
    k: c.get(k) for k in ("how", "lines", "dropped", "label")}, "first_new": new_ids[0] if new_ids else None, "pass": ok_all}, indent=1) + "\n",
    encoding="utf-8")
sys.exit(0 if ok_all else 1)
