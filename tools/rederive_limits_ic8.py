"""BUILD IC-8 (D10; from rederive_limits_ic7.py, IC-7 D11): the planning table re-derived from the live call logs on record, per
invocation — never edits code.
Every call log is read through the tree's one reader (NR.read_records, run by run). A call is keyed by its call_id (V41's key — run id,
label, attempt, time — goes); a legacy line (no call_id: IC-7's and earlier) is one call by its run, label, time and duration wherever it
is kept (a staged folder's copy, a checkpoints_archive copy).
A track's seconds outside its calls are computed per invocation: the invocation's wall seconds less the sum of that invocation's own
calls. Never floored (V41d goes): a negative value is an accounting error, named and excluded; a missing wall time is "missing", not 0.
  IC-8 runs: an invocation's wall seconds are its invocations record's "finished" seconds (time.perf_counter); its calls are the call-log
    lines that name it.
  Legacy runs (IC-7's and earlier): a console's "completed in" belongs to the invocation whose calls fall inside that console's own run,
    by the call log's times — a console with clock lines gives each track's window (NR.legacy_windows: the block's end less the track's
    "s left", plus its "completed in"); one with none gives its run's window (its run id's time to the sum of its tracks' "completed in"
    plus RUN_SLACK_S; a track runs once in a run). A call of the run in no window or in two makes the run's value for its track
    "ambiguous", named and excluded. A run whose calls are on record with no console and no invocations record is named as lacking (a
    Block 2 folder read alone lacks Block 1's console).
Only an invocation that completed is a measure (a FAILED or TIMED OUT run's seconds are listed and not used), as IC-7's tool read only
"completed in". The table itself (planning tokens, floor rate, planning and first-line seconds) is IC-7's derivation on the deduplicated
samples; the reserves and tails come from the per-invocation values. The shipped profiles are kept as shipped (decided 11/18: Shane's
choice; t_promises_ic8's trigger is a capacity line's "a new maximum"); --diff prints the differences.
Usage: python3 -I -B rederive_limits_ic8.py [run folder ...] [--src <the tree's src>] [--write evidence.json] [--diff]"""
import datetime, glob, hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rederive_limits_ic7 as R7  # noqa: E402 — SPEC, BLOCK_CALLS, key_of, is_live, the table's derivation and profiles(), as shipped

RUN_SLACK_S = 120.0          # a console with no clock lines: its run's window ends this long after the sum of its tracks' seconds
TRACKS = ("oracle", "architect", "muse", "landing")
FOLDER_TRACK = {"oracle-harness": "oracle", "architect-harness": "architect", "muse-harness": "muse", "landing-harness": "landing"}
CALLING = ("oracle", "architect", "muse")             # the hub makes no model call
DECISION = "kept as shipped (decided 11/18)"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_nr(src):
    """The tree's requirement module (its one record reader) from its src root."""
    sys.path.insert(0, str(Path(src)))
    import naipc_requirements as NR  # noqa: E402
    if not hasattr(NR, "read_records"):
        raise SystemExit(f"{src}: its naipc_requirements has no read_records — this tool reads records through BUILD IC-8's reader")
    return NR


def run_windows(txt, NR, root_name):
    """A console with no clock lines (before IC-6): each pipeline run's window — from its run id's time (the pipeline's start, to the
    second) to the sum of its tracks' seconds plus RUN_SLACK_S — one per track the run ran."""
    out, k, cur, mode, seen = [], 0, None, None, {}
    titles = {v: t for t, v in NR.TRACK_TITLES.items()}
    end_rx = re.compile(r"(✅|❌) (" + "|".join(re.escape(v) for v in NR.TRACK_TITLES.values())
                        + r") (?:completed in|FAILED \(exit code -?\d+\) after|TIMED OUT after) ([\d.]+)s")
    for i, ln in enumerate(txt.splitlines(), 1):
        m = re.search(r"Mode: (?:CLEAN \(Block 1\)|CURVEBALL \(Block 2\))", ln)
        if m:
            mode = 2 if "Block 2" in m.group(0) else 1
        m = re.search(r"Run id: (\d{8}T\d{6}Z)", ln)
        if m:
            k += 1
            cur = {"run": k, "run_id": m.group(1), "block": mode, "tracks": [],
                   "start": datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()}
            out.append(cur)
            continue
        m = end_rx.search(ln)
        if m and cur is not None:
            cur["tracks"].append((titles[m.group(2)], float(m.group(3)), "completed" if m.group(1) == "✅" else
                                  ("timed out" if "TIMED OUT" in ln else "failed"), i))
    ws = []
    for r in out:
        end = r["start"] + sum(s for _t, s, _o, _i in r["tracks"]) + RUN_SLACK_S if r["start"] is not None else None
        for t, s, o, i in r["tracks"]:
            seen[t] = seen.get(t, 0) + 1
            ws.append({"id": f"legacy:{root_name}/console.txt:{r['run']}:{t}", "number": seen[t], "track": t, "start": r["start"], "end": end,
                       "seconds": s, "outcome": o, "line": i, "run": r["run"], "run_id": r["run_id"], "block": r["block"],
                       "how": "its run's window (the console has no clock line)"})
    return [w for w in ws if w["start"] is not None]


def read_roots(roots, NR):
    """→ {"calls": {key: sample}, "windows": [...], "records": {(track, run id): [invocation lines]}, "kept": {(track, run, n): block},
    "files": {path: sha256}, "consoles": [...], "notes": [...]} from staged run folders (each its harness folders and console.txt)."""
    wins, consoles, files, notes, calls, records, kept = [], [], {}, [], {}, {}, {}
    for root in roots:
        con = Path(root) / "console.txt"
        if not con.is_file():
            continue
        txt = con.read_text(encoding="utf-8", errors="replace")
        ws = NR.legacy_windows(txt)
        for w in ws:
            w["id"], w["how"] = f"legacy:{Path(root).name}/console.txt:{w['run']}:{w['track']}", "the track's clock line"
        if not ws:
            ws = run_windows(txt, NR, Path(root).name)
        wins += ws
        consoles.append({"root": str(root), "console_sha256": sha(con), "windows": len(ws)})
    for root in roots:
        for h in sorted(Path(root).iterdir()) if Path(root).is_dir() else []:
            t = FOLDER_TRACK.get(h.name.lower())
            if not t:
                continue
            for f in sorted((h / "checkpoints").glob("invocations_*.jsonl")) if (h / "checkpoints").is_dir() else []:
                files[str(f)] = sha(f)
                ri = NR.read_records(f, "run", strict=False)
                for x in ri.lines:
                    if x.get("invocation_id") and x not in records.setdefault((t, ri.rid), []):
                        records[(t, ri.rid)].append(x)
            for f in sorted((h / "checkpoints").glob("track_status_*_inv*.json")) if (h / "checkpoints").is_dir() else []:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    kept[(t, d.get("run_id"), d.get("invocation_number"))] = d.get("block")
                except (OSError, ValueError):
                    pass
            logs = sorted(glob.glob(str(h / "checkpoints" / "call_log.jsonl")) + glob.glob(str(h / "checkpoints_archive" / "*" / "call_log.jsonl")))
            for f in logs:
                files[f] = sha(f)
                for rid in NR.record_run_ids(f):
                    r = NR.read_records(f, "run", rid=rid, strict=False, windows=[w for w in wins if w["track"] == t])
                    notes += [f"{f}: line {i} refused — {why}" for i, why in r.refused]
                    notes += [f"{f}: line {d['line']} repeats line {d['first']}'s call id — the first kept" for d in r.duplicates]
                    for x in r.lines:
                        if not R7.is_live(x):
                            continue
                        key = (x["call_id"] if x["_call"] == "recorded"
                               else ("legacy", x.get("run_id"), x.get("label"), x.get("at"), x.get("t_end")))
                        if key in calls:
                            continue
                        u = x["usage"]
                        calls[key] = {"profile": R7.key_of(t, x), "track": t, "label": x.get("label"), "run_id": x["run_id"], "at": x.get("at"),
                                      "max_tokens": x.get("max_tokens"), "effort": x.get("effort"), "output": u.get("output_tokens"),
                                      "thinking": x.get("thinking_tokens"), "input": u.get("input_tokens"), "t_end": x.get("t_end"),
                                      "t_first_text": x.get("t_first_text"), "t_first_line": x.get("t_first_line"), "stop": x.get("stop_reason"),
                                      "text_chars": x.get("text_chars"), "file": str(f), "line": x["_line"], "call_id": x["call_id"],
                                      "invocation_id": x.get("invocation_id"), "invocation_number": x.get("invocation_number"),
                                      "invocation_how": x["_invocation"], "correlation": x["correlation"]}
    return {"calls": calls, "windows": wins, "records": records, "kept": kept, "files": files, "consoles": consoles, "notes": notes}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def per_invocation(rd):
    """Each invocation's seconds outside its calls → [{track, run_id, block, invocation, number, how, wall, calls, calls_s, outside,
    status: measured | accounting error | missing | ambiguous | not completed}], and the runs that lack a console or a record."""
    samples = list(rd["calls"].values())
    out, ic8 = [], set()
    for (t, rid), lines in sorted(rd["records"].items()):
        ic8.add((t, rid))
        for x in [y for y in lines if y.get("event") == "started"]:
            fin = next((y for y in lines if y.get("event") == "finished" and y.get("invocation_id") == x["invocation_id"]), None)
            mine = [s for s in samples if s["invocation_id"] == x["invocation_id"]]
            cs = round(sum(s["t_end"] for s in mine if _num(s["t_end"])), 3)
            wall = fin.get("seconds") if fin else None
            row = {"track": t, "run_id": rid, "block": rd["kept"].get((t, rid, x.get("invocation_number"))), "invocation": x["invocation_id"],
                   "number": x.get("invocation_number"), "how": "its invocations record", "wall": wall, "calls": len(mine), "calls_s": cs}
            if not _num(wall):
                row.update(outside=None, status="missing", why="no finished line with its seconds")
            elif not (fin.get("outcome") or "").startswith(("exit 0", "ended")):
                row.update(outside=None, status="not completed", why=fin.get("outcome"))
            else:
                o = round(wall - cs, 3)
                row.update(outside=o if o >= 0 else None, status="measured" if o >= 0 else "accounting error",
                           why=None if o >= 0 else f"{wall} s less {cs} s of calls is {o} s")
            out.append(row)
    for w in rd["windows"]:
        t, rid = w["track"], w["run_id"]
        if (t, rid) in ic8:
            continue
        mine = [s for s in samples if s["invocation_id"] == w["id"]]
        amb = [s for s in samples if s["track"] == t and s["run_id"] == rid and s["invocation_how"] == "ambiguous"]
        cs = round(sum(s["t_end"] for s in mine if _num(s["t_end"])), 3)
        row = {"track": t, "run_id": rid, "block": w.get("block"), "invocation": w["id"], "number": w.get("number"), "how": w.get("how"),
               "wall": w["seconds"], "calls": len(mine), "calls_s": cs}
        if w["outcome"] != "completed":
            row.update(outside=None, status="not completed", why=w["outcome"])
        elif amb:
            row.update(outside=None, status="ambiguous", why=f"{len(amb)} call(s) of the run in no window or in two")
        elif t in CALLING and not any(s["track"] == t and s["run_id"] == rid for s in samples):
            row.update(outside=None, status="missing", why="no live call of the run on record (its log not staged)")
        else:
            o = round(w["seconds"] - cs, 3)
            row.update(outside=o if o >= 0 else None, status="measured" if o >= 0 else "accounting error",
                       why=None if o >= 0 else f"{w['seconds']} s less {cs} s of calls is {o} s")
        out.append(row)
    have = {(r["track"], r["run_id"]) for r in out}
    lacking = sorted({(s["track"], s["run_id"]) for s in samples} - have)
    lack = [{"track": t, "run_id": rid, "calls": sum(1 for s in samples if s["track"] == t and s["run_id"] == rid)} for t, rid in lacking]
    return out, lack


def derive(rd):
    """(the table, the reserves, the tails, the per-invocation rows, the lacking runs): the table as IC-7's tool derives it from the
    samples; the reserves and tails from each block's measured invocations (1.5 × the longest time outside calls, to the 10 s above,
    at least 10 — IC-7's rule, its input per invocation now)."""
    samples = sorted(rd["calls"].values(), key=lambda s: (s["at"] or "", s["profile"] or ""))
    table, _r, _t = R7.derive(samples, [])
    rows, lack = per_invocation(rd)
    reserves, tails = {}, {}
    for b, tracks in R7.BLOCK_CALLS.items():
        reserves[b], tails[b] = {}, {}
        for track in ("oracle",) + tuple(tracks):
            o_ = max([r["outside"] for r in rows if r["track"] == track and r["block"] == b and r["status"] == "measured"] or [0.0])
            if track in tracks:
                plan = sum((table.get(k) or {}).get("planning_s") or 0 for k in tracks[track])
                reserves[b][track] = max(10, R7._ceil10(plan + 1.5 * o_))
            if track != "landing":
                tails[b][track] = max(10, R7._ceil10(1.5 * o_))
    return table, reserves, tails, rows, lack


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    src = Path(argv[argv.index("--src") + 1]) if "--src" in argv else HERE.parent / "src"
    write = Path(argv[argv.index("--write") + 1]) if "--write" in argv else None
    for v in (src, write):
        if v is not None and str(v) in args:
            args.remove(str(v))
    NR = load_nr(src)
    roots = [Path(a) for a in args] or sorted(p for p in (HERE.parent / "tests/fixtures/live").iterdir() if p.is_dir())
    rd = read_roots(roots, NR)
    table, reserves, tails, rows, lack = derive(rd)
    print(f"{len(rd['calls'])} live calls (by call id) from {sum(1 for f in rd['files'] if f.endswith('call_log.jsonl'))} logs; "
          f"{len(rd['windows'])} console window(s); {len(rd['records'])} invocations record(s)")
    for k in sorted(table):
        t = table[k]
        print(f"  {k:<24} completed {t['completed']} censored {t['censored']} → {t['planning_tokens']} tokens; floor {t['floor_rate']}; "
              f"{t['planning_s']} s; first line {t['first_line_s']}; unresolved cuts {t['censored_unresolved']}")
    print("  unmeasured:", sorted(k for k in R7.SPEC if k not in table))
    print("  time outside calls, per invocation:")
    for r in sorted(rows, key=lambda r: (r["run_id"] or "", TRACKS.index(r["track"]), r["number"] or 0)):
        sc = f"invocation {r['number']} of run {r['run_id']} (Block {r['block'] if r['block'] is not None else '?'})"
        if r["status"] == "measured":
            print(f"    {r['track']:<9} {r['outside']:.3f} s in {sc}: {r['wall']} s less {r['calls']} call(s), {r['calls_s']} s — {r['how']}")
        else:
            print(f"    {r['track']:<9} {r['status']} in {sc}: {r['why']} — excluded")
    for x in lack:
        print(f"  lacking: run {x['run_id']}: {x['calls']} {x['track']} call(s) on record with no console and no invocations record — "
              "not measured here")
    for n in rd["notes"]:
        print(f"  note: {n}")
    print("  reserves:", json.dumps(reserves))
    print("  tails:", json.dumps(tails))
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(json.dumps({"what": "BUILD IC-8 (D10): the live calls on record by call id, each invocation's time outside its calls, "
                                             "and the planning table they give", "decision": DECISION,
                                     "roots": [str(r) for r in roots], "files": rd["files"], "consoles": rd["consoles"],
                                     "samples": sorted(rd["calls"].values(), key=lambda s: (s["at"] or "", s["profile"] or "")),
                                     "invocations": rows, "lacking": lack, "notes": rd["notes"], "table": table, "reserves": reserves,
                                     "tails": tails}, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        print(f"written: {write} ({sha(write)[:16]})")
    if "--diff" in argv:
        mine, have, n = R7.profiles(table), getattr(NR, "CALL_PROFILES", {}) or {}, 0
        for k in sorted(set(mine) | set(have)):
            a, b = mine.get(k) or {}, have.get(k) or {}
            for f in ("max_tokens", "effort", "stream", "form", "planning_tokens", "floor_rate", "floor_sample", "planning_s", "longest_s",
                      "first_line_s", "largest_input", "unmeasured"):
                if a.get(f) != b.get(f):
                    n += 1
                    print(f"  DIFF {k}.{f}: the shipped profile {b.get(f)!r}, the records {a.get(f)!r}")
        for nm, mine_ in (("BLOCK_RESERVES_S", reserves), ("BLOCK_TAIL_S", tails)):
            rs = getattr(NR, nm, None)
            if rs != mine_:
                n += 1
                print(f"  DIFF {nm}: shipped {rs!r}, the records {mine_!r}")
        print("  the records equal the shipped profiles" if not n else
              f"  {n} difference(s): the shipped profiles are {DECISION} — a change is Shane's choice, from an evidence file, never by hand")
        return 1 if n else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
