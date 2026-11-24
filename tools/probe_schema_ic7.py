"""BUILD IC-7 (D14; §7 R4) — the schema probe: the strict wire of each of the Muse's two Block 1 calls, sent once with a short request
(effort low, max_tokens 2,000), through the installed tree's own path — the wire from muse_harness.muse_wire (Call 1's with the brief
block and the Oracle's parts, Call 2's with the parts: the variants a pipeline run on a contract sends), the request's output_config from
NR.output_config, the call through NR.model_call, as the harnesses make it. Paid: two calls of at most 2,000 output tokens each (the build
prompt's estimate: about $0.10 at most). For each call it reads: the API accepted the schema (no 400), the stop reason, the first event's
seconds (the grammar's compile included), and whether the reply fits its wire (the wire's own subset: type, properties, required,
additionalProperties false, items). The wire is linted first (NR.schema_problems): a wire the build refuses is not sent.
The key is read from the environment as the harnesses read it (ANTHROPIC_API_KEY) and never printed: only "set" or "not set". The call
log (one line per call: times, counts, the stop reason — never a prompt, a reply, a key or another header), the journals and
probe_result.json go to a new folder beside the four, <root>/naipc-r4-ic7-<UTC stamp>; nothing else is written, and the four folders are
read, never written. A 400, a reply that does not fit its wire, or a reply whose fit is not known (cut at its limit, refused, an error):
NAIPC_IC7_PROMPT=0 for the rest of the run turn, and ask Shane (§7 R4). Run it again the morning of each competition day with the
competition's key (the grammar's cache lasts 24 hours from last use; its scope across keys is not stated).
Usage: python -I -B probe_schema_ic7.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import hashlib, json, os, sys, threading, time
from pathlib import Path

LIMIT, EFFORT = 2000, "low"
SYSTEM = "Output ONLY valid JSON matching the schema."
PROMPT = ("This request checks a response format only. Fill every field with the shortest value its type allows: one item in each list, "
          "one word in each text, the whole number 1 in each number.")
VARIANTS = (("muse/call1", {"brief": True, "parts": True}, "Call 1's wire, with the brief block and the Oracle's parts"),
            ("muse/call2", {"parts": True}, "Call 2's wire, with the Oracle's parts"))


def fits(v, s, path=""):
    """A value against a wire schema's subset (type, properties, required, additionalProperties false, items) → [problems]. The same
    rules as the mock's (tests/lib/mock_anthropic.py fits; tests/t_tools_ic7.py holds the two to the same verdicts)."""
    t = s.get("type")
    tm = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t in tm and not isinstance(v, tm[t]):
        return [f"{path or '(top)'}: not {t}"]
    if t == "integer" and not (isinstance(v, int) and not isinstance(v, bool)):
        return [f"{path}: not an integer"]
    if t == "number" and not (isinstance(v, (int, float)) and not isinstance(v, bool)):
        return [f"{path}: not a number"]
    out = []
    if t == "object":
        props = s.get("properties") or {}
        out += [f"{path}.{k}: missing" for k in s.get("required") or [] if k not in v]
        if s.get("additionalProperties") is False:
            out += [f"{path}.{k}: not in the schema" for k in v if k not in props]
        for k, x in v.items():
            if k in props:
                out += fits(x, props[k], f"{path}.{k}")
    if t == "array":
        if isinstance(s.get("minItems"), int) and len(v) < s["minItems"]:
            out.append(f"{path}: fewer than {s['minItems']} items")
        for x in v:
            out += fits(x, s.get("items") or {}, path + "[]")
    return out


def sha16(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def main(argv):
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path.home() / "OneDrive" / "Documents"
    muse = next((root / n for n in ("muse-harness", "Muse-harness") if (root / n / "muse_harness.py").is_file()), None)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"R4 ANTHROPIC_API_KEY: {'set' if key.strip() else 'not set'}")
    if not key.strip():
        print("R4 not run: no key in the environment (set it as for a live run, then run this again)")
        return 2
    if muse is None:
        print(f"R4 not run: no muse_harness.py under {root}")
        return 2
    sys.path.insert(0, str(muse))
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        import muse_harness as MH            # the installed harness: its wire, its model constant, its module NR
    NR = MH.NR
    import anthropic
    import httpx
    if not NR.ic7_prompt():
        print("R4 not run: NAIPC_IC7_PROMPT is off in this window (or a switch it builds on is) — Calls 1 and 2 send no schema, so there "
              "is nothing to probe")
        return 2
    wires = []
    for k, kw, what in VARIANTS:
        w = MH.muse_wire(k, **kw)
        probs = NR.schema_problems(w)
        oc = NR.output_config(EFFORT, w)
        if probs or "format" not in oc:
            print(f"R4 not run: {what} " + (f"fails the build's lint: {'; '.join(probs[:5])}" if probs
                                             else "is not sent: NAIPC_IC4_PROMPT is off (output_config holds the effort only)"))
            return 2
        wires.append((k, w, what, oc))
    work = root / f"naipc-r4-ic7-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    if work.exists():
        print(f"R4 not run: {work} exists")
        return 2
    work.mkdir()
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
    key = None                                    # the client holds it; nothing here keeps or prints it
    print(f"R4 the API: {client.base_url.host}; the model: {MH.MODEL}; effort {EFFORT}, max_tokens {LIMIT:,} (the probe's, not the calls')")
    results = []
    for k, w, what, oc in wires:
        label = "probe_" + k.split("/")[1]
        c = NR.wire_counts(w)
        r = {"call": k, "wire": what, "wire_sha256_16": sha16(w), "properties": c["properties"], "lists": c["lists"], "accepted": None,
             "status": None, "error": None, "stop_reason": None, "first_event_s": None, "first_text_s": None, "end_s": None,
             "output_tokens": None, "thinking_tokens": None, "fits": None, "problems": []}
        text = ""
        try:
            msg, text = NR.model_call(client, {"model": MH.MODEL, "max_tokens": LIMIT, "output_config": oc, "system": SYSTEM,
                                               "messages": [{"role": "user", "content": PROMPT}]},
                                      label=label, attempt=1, log_dir=work, clock=time.monotonic, end_at=time.monotonic() + 300,
                                      stream=True, timer=threading.Timer, journal=work / f"journal_{label}_1.txt")
            r["accepted"] = True
            r["stop_reason"] = getattr(msg, "stop_reason", None)
        except anthropic.APIStatusError as e:
            body = e.body if isinstance(e.body, dict) else {}
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            r["status"] = e.status_code
            r["accepted"] = False if e.status_code == 400 else None
            r["error"] = f"{e.status_code} {err.get('type') or type(e).__name__}: {str(err.get('message') or '')[:300]}"
        except Exception as e:  # noqa: BLE001 — a dropped stream, a timeout, the end time: not known
            r["error"] = type(e).__name__
        rec = (getattr(NR.model_call, "last", None) or ({}, ""))[0] or {}
        u = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        r.update(first_event_s=rec.get("t_first_event"), first_text_s=rec.get("t_first_text"), end_s=rec.get("t_end"),
                 output_tokens=u.get("output_tokens"), thinking_tokens=rec.get("thinking_tokens"))
        if r["stop_reason"] == "end_turn":
            try:
                v = json.loads(text)
                r["problems"] = fits(v, w)
            except ValueError:
                r["problems"] = ["(the reply): not JSON"]
            r["fits"] = not r["problems"]
        results.append(r)
        acc = ("accepted" if r["accepted"] else "REFUSED — " + str(r["error"]) if r["accepted"] is False
               else "not known — " + str(r["error"]))
        fit = ("yes" if r["fits"] else "NO — " + "; ".join(r["problems"][:5]) if r["fits"] is False
               else f"not known (the reply ended {r['stop_reason'] or r['error']})")
        print(f"R4 {k}: {what} (sha256 {r['wire_sha256_16']}; {r['properties']} properties, {r['lists']} lists): the schema {acc}; "
              f"stop {r['stop_reason']}; first event {r['first_event_s']} s (the compile included); first text {r['first_text_s']} s; "
              f"end {r['end_s']} s; output {r['output_tokens']} tokens (thinking {r['thinking_tokens']}); fits its wire: {fit}")
    ok_all = all(r["accepted"] and r["fits"] for r in results)
    (work / "probe_result.json").write_text(json.dumps({"results": results, "pass": ok_all, "limit": LIMIT, "effort": EFFORT},
                                                       indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("R4 " + ("PASS: both schemas accepted and each reply fits its wire — NAIPC_IC7_PROMPT stays on" if ok_all else
                   "FAIL: set NAIPC_IC7_PROMPT=0 for the rest of the run turn and ask Shane (§7 R4)") + f"; the probe's folder: {work}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
