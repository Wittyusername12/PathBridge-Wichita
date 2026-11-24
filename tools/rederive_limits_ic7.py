"""BUILD IC-7 (D11; a copy of rederive_limits_ic6.py, IC-6 D1, D4): the planning table re-derived from the live call logs on record — never edits code.
Reads every checkpoints/call_log.jsonl and checkpoints_archive/*/call_log.jsonl under the run folders given (default: the tree's
tests/fixtures/live — R6's p1b1 and p1b2, IC-3's r6 and r8). A line is live when it has a run id, the model claude-opus-5-5 and an output
count other than the mock's 1 or 42; lines are de-duplicated by (run id, label, attempt, time). Its profile: the line's own "profile" key
(IC-6 writes it); for an earlier line, its harness and label — and for the Oracle's update the limit it asked (128,000: the keyed edits of
IC-4 D11 on; else the whole-field reply), the one rule that reads a parameter, and only for lines written before IC-6.
Per profile: the completed samples and the censored ones (stop not end_turn: its ×1.5 is a floor, never a size); planning tokens =
ceil(1.5 × the largest of both); the floor rate = the slowest completed line of at least 30 s; planning seconds = the larger of planning
tokens ÷ floor rate and 1.5 × the longest completed duration; first line = the longest t_first_line, else the longest first text + 30 s.
A call with no live line is "unmeasured": its planning tokens are its limit, its seconds come from its harness's stated rate at run time.
The reserves of the later tracks per block: the ceiling to 10 s of the track's planning seconds plus 1.5 × its longest measured time outside
calls on record (the run's console, "<track> completed in N s", less that run's calls of the track — the run named by the console's "Run
id:"), at least 10 s; the tails: 1.5 × each calling track's own longest time outside its calls, to the 10 s above, at least 10 s.
SPEC holds each call's fixed fields (Q-LIMITS: its limit, effort, stream mode, form, stage); the measured fields come from the samples.
Usage: python3 -I -B rederive_limits_ic7.py [run folder ...] [--write evidence.json]   the table from the logs (written with --write)
       python3 -I -B rederive_limits_ic7.py --literal evidence.json                     NR.CALL_PROFILES's literal, from that file's samples
       python3 -I -B rederive_limits_ic7.py [run folder ...] --diff                      the table against the tree's NR.CALL_PROFILES"""
import glob, hashlib, json, math, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACK = {"oracle-harness": "oracle", "muse-harness": "muse", "architect-harness": "architect"}
LABEL_KEY = {("oracle", "master"): "oracle/master", ("oracle", "deliverables"): "oracle/deliverables", ("oracle", "repair"): "oracle/repair",
             ("oracle", "call"): "oracle/diagram_repair", ("muse", "call1"): "muse/call1", ("muse", "call2"): "muse/call2",
             ("muse", "update"): "muse/update", ("muse", "repair"): "muse/repair", ("architect", "rationale"): "architect/rationale"}
CONSOLE_NAME = {"oracle": "Oracle (Business Case)", "architect": "Architect (Resource Navigator)", "muse": "Muse (Outreach Campaign)",
                "landing": "Landing (Full Stack Hub)"}
# Q-LIMITS (D1): 128,000 on every streamed call that writes more than a few thousand tokens; 4,000 on the Architect's explanation and the
# three repairs (the Oracle's two are not streamed: at or under 21,333). Efforts as IC-5's. form: object — one JSON object, all or nothing;
# lines — JSON Lines, each complete line kept when cut; schema — a structured output's object; text — plain text.
SPEC = {
    "oracle/master": {"track": "oracle", "label": "master", "stream": True, "form": "object", "max_tokens": 128000, "effort": "high",
                      "stage": "master analysis"},
    "oracle/deliverables": {"track": "oracle", "label": "deliverables", "stream": True, "form": "object", "max_tokens": 128000,
                            "effort": "medium", "stage": "the Oracle's end"},
    "oracle/update": {"track": "oracle", "label": "update", "stream": True, "form": "lines", "max_tokens": 128000, "effort": "high",
                      "stage": "curveball update"},
    "oracle/update_whole": {"track": "oracle", "label": "update", "stream": True, "form": "object", "max_tokens": 128000, "effort": "high",
                            "stage": "curveball update"},
    "oracle/repair": {"track": "oracle", "label": "repair", "stream": False, "form": "schema", "max_tokens": 4000, "effort": "low",
                      "stage": "the Oracle's end"},
    "oracle/diagram_repair": {"track": "oracle", "label": "call", "stream": False, "form": "text", "max_tokens": 4000, "effort": "low",
                              "stage": "the Oracle's end"},
    "muse/call1": {"track": "muse", "label": "call1", "stream": True, "form": "object", "max_tokens": 128000, "effort": "medium",
                   "stage": "the Muse's stage"},
    "muse/call2": {"track": "muse", "label": "call2", "stream": True, "form": "object", "max_tokens": 128000, "effort": "medium",
                   "stage": "the Muse's stage"},
    "muse/update": {"track": "muse", "label": "update", "stream": True, "form": "object", "max_tokens": 128000, "effort": "medium",
                    "stage": "the Muse's stage"},
    "muse/repair": {"track": "muse", "label": "repair", "stream": True, "form": "schema", "max_tokens": 4000, "effort": "low",
                    "stage": "the Muse's stage"},
    "architect/rationale": {"track": "architect", "label": "rationale", "stream": True, "form": "schema", "max_tokens": 4000, "effort": "low",
                            "stage": "the rationale's deadline"},
}
# the calls each later track makes in each block (the reserves): the Muse's Block 1 strategy and creative, its Block 2 update; the hub none
BLOCK_CALLS = {1: {"architect": ["architect/rationale"], "muse": ["muse/call1", "muse/call2"], "landing": []},
               2: {"architect": ["architect/rationale"], "muse": ["muse/update"], "landing": []}}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def key_of(track, r):
    """A call-log line's profile key: its own "profile" (IC-6), else its harness and label (the Oracle's update by the limit it asked)."""
    if isinstance(r.get("profile"), str):
        return r["profile"]
    lab = r.get("label")
    if track == "oracle" and lab == "update":
        return "oracle/update" if r.get("max_tokens") == 128000 else "oracle/update_whole"
    return LABEL_KEY.get((track, lab))


def is_live(r):
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    out = u.get("output_tokens")
    return (bool(r.get("run_id")) and isinstance(out, int) and not isinstance(out, bool) and out not in (1, 42)
            and (r.get("model") or r.get("model_asked")) == "claude-opus-5-5")


def read_roots(roots):
    """(samples, consoles, {log file: sha256}) from staged run folders (each holding its harness folders and console.txt)."""
    samples, seen, files, consoles = [], set(), {}, []
    for root in roots:
        root = Path(root)
        logs = {}
        for h in sorted(root.iterdir()) if root.is_dir() else []:
            t = TRACK.get(h.name.lower())
            if t:
                logs[t] = sorted(glob.glob(str(h / "checkpoints" / "call_log.jsonl"))
                                 + glob.glob(str(h / "checkpoints_archive" / "*" / "call_log.jsonl")))
        block = 2 if (root / "oracle-harness" / "checkpoints" / "curveball.json").is_file() else 1     # the console's Mode line decides below
        run_ids = set()
        for track, fl in logs.items():
            for f in fl:
                files[f] = sha(f)
                for i, ln in enumerate(Path(f).read_text(encoding="utf-8").splitlines(), 1):
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or not is_live(r):
                        continue
                    k = (r["run_id"], r.get("label"), r.get("attempt"), r.get("at"))
                    if k in seen:
                        continue
                    seen.add(k)
                    u = r["usage"]
                    samples.append({"profile": key_of(track, r), "track": track, "label": r.get("label"), "run_id": r["run_id"],
                                    "at": r.get("at"), "max_tokens": r.get("max_tokens"), "effort": r.get("effort"),
                                    "output": u.get("output_tokens"), "thinking": r.get("thinking_tokens"), "input": u.get("input_tokens"),
                                    "t_end": r.get("t_end"), "t_first_text": r.get("t_first_text"), "t_first_line": r.get("t_first_line"),
                                    "stop": r.get("stop_reason"), "text_chars": r.get("text_chars"), "file": str(f), "line": i})
                    run_ids.add(r["run_id"])
        con = root / "console.txt"
        if con.is_file():
            txt = con.read_text(encoding="utf-8", errors="replace")
            times = {}
            for t, nm in CONSOLE_NAME.items():
                m = re.search(re.escape(nm) + r" completed in ([\d.]+)s", txt)
                if m:
                    times[t] = float(m.group(1))
            mode = re.search(r"Mode: (?:CLEAN \(Block 1\)|CURVEBALL \(Block 2\))", txt)
            rid = re.search(r"Run id: (\S+)", txt)
            consoles.append({"root": str(root), "block": (2 if "Block 2" in mode.group(0) else 1) if mode else block, "times": times,
                             "run_id": rid.group(1) if rid else None, "runs": sorted(run_ids), "console_sha256": sha(con)})
    samples.sort(key=lambda s: (s["at"] or "", s["profile"] or ""))
    return samples, consoles, files


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def outside_calls(samples, c, track):
    """A track's seconds outside its calls in one run on record: its console time less the seconds of its calls of that run (the console's
    own run id; a Block 2 folder's logs hold Block 1's lines too). None when the console has no time for the track."""
    if track not in c["times"]:
        return None
    calls = sum(s["t_end"] for s in samples if s["track"] == track and s["run_id"] == c.get("run_id") and _num(s["t_end"]))
    return max(0.0, c["times"][track] - calls)


def _ceil10(v):
    return int(math.ceil(round(v, 6) / 10.0) * 10)


def derive(samples, consoles):
    """The planning table {key: measured fields}, the reserves {block: {track: s}} — each later track's calls' planning seconds plus 1.5 ×
    its longest time outside calls on record, to the 10 s above, at least 10 — and the tails {block: {track: s}}: 1.5 × a track's own
    longest time outside its calls, to the 10 s above (D3: a track's calls end that much before the later tracks' reserves begin, so its
    own work after its last call never runs into them; the hub makes no call: none). A pure function of the samples and consoles."""
    table = {}
    for s in samples:
        t = table.setdefault(s["profile"], {"completed": [], "censored": [], "rates": [], "t_ends": [], "first_text": [], "first_line": [],
                                            "inputs": [], "runs": [], "cut_files": []})
        t["runs"].append(s["run_id"])
        if s["stop"] == "end_turn":
            t["completed"].append(s["output"])
            if _num(s["t_end"]):
                t["t_ends"].append(s["t_end"])
                if s["t_end"] >= 30:
                    t["rates"].append((round(s["output"] / s["t_end"], 2), s["output"], s["t_end"]))
        else:
            t["censored"].append(s["output"])
            t["cut_files"].append(f"{s['file']}:{s.get('line')}")
        if _num(s.get("input")):
            t["inputs"].append(s["input"])
        if _num(s["t_first_text"]):
            t["first_text"].append(s["t_first_text"])
        if _num(s.get("t_first_line")):
            t["first_line"].append(s["t_first_line"])
    out = {}
    for k, t in table.items():
        allv = t["completed"] + t["censored"]
        fl = min(t["rates"]) if t["rates"] else None
        m = {"planning_tokens": math.ceil(1.5 * max(allv)) if allv else None,
             "floor_rate": fl[0] if fl else None, "floor_sample": [fl[1], fl[2]] if fl else None,
             "longest_s": max(t["t_ends"]) if t["t_ends"] else None,
             "largest_input": max(t["inputs"]) if t["inputs"] else None,
             "completed": t["completed"], "censored": t["censored"],
             "censored_unresolved": [c for c in t["censored"] if not any(x > c for x in t["completed"])],
             "cut_files": t["cut_files"], "runs": sorted(set(t["runs"]))}
        if m["planning_tokens"] and m["floor_rate"]:
            m["planning_s"] = round(max(m["planning_tokens"] / m["floor_rate"], 1.5 * (m["longest_s"] or 0)), 1)
        else:
            m["planning_s"] = round(1.5 * m["longest_s"], 1) if m["longest_s"] else None
        m["first_line_s"] = (round(max(t["first_line"]), 1) if t["first_line"]
                             else round(max(t["first_text"]) + 30, 1) if t["first_text"] else None)
        out[k] = m
    reserves, tails = {}, {}
    for b, tracks in BLOCK_CALLS.items():
        reserves[b], tails[b] = {}, {}
        for track in ("oracle",) + tuple(tracks):
            outside = [o for o in (outside_calls(samples, c, track) for c in consoles if c["block"] == b) if o is not None]
            o_ = max(outside) if outside else 0.0
            if track in tracks:
                plan = sum((out.get(k) or {}).get("planning_s") or 0 for k in tracks[track])
                reserves[b][track] = max(10, _ceil10(plan + 1.5 * o_))
            if track != "landing":
                tails[b][track] = max(10, _ceil10(1.5 * o_))
    return out, reserves, tails


def profiles(table, evidence_sha=None):
    """NR.CALL_PROFILES: SPEC's fixed fields with the measured ones; a call with no live line is unmeasured (its limit its planning)."""
    out = {}
    for k, sp in SPEC.items():
        m = table.get(k)
        p = dict(sp)
        if m:
            p.update(planning_tokens=m["planning_tokens"], floor_rate=m["floor_rate"], floor_sample=m["floor_sample"], planning_s=m["planning_s"],
                     longest_s=m["longest_s"], first_line_s=m["first_line_s"], largest_input=m["largest_input"], unmeasured=False,
                     basis={"runs": m["runs"], "completed": m["completed"], "censored": m["censored"], "evidence_sha256": evidence_sha})
        else:
            p.update(planning_tokens=sp["max_tokens"], floor_rate=None, floor_sample=None, planning_s=None, longest_s=None, first_line_s=None,
                     largest_input=None, unmeasured=True, basis={"runs": [], "completed": [], "censored": [], "evidence_sha256": evidence_sha})
        p["all_or_nothing"] = p["form"] != "lines"
        p["key"] = k
        out[k] = p
    return out


def literal(evidence_path):
    """The literal text of NR.CALL_PROFILES, NR.CALL_PROFILES_EVIDENCE and NR.BLOCK_RESERVES_S, from the evidence file's own samples."""
    ev = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    h = sha(evidence_path)[:16]
    table, reserves, tails = derive(ev["samples"], ev["consoles"])
    prof = profiles(table, h)
    lines = ["CALL_PROFILES_EVIDENCE = " + repr({"file": "evidence/call_samples_ic7.json", "sha256_16": h, "lines": len(ev["samples"]),
                                                "logs": len(ev["files"]), "derived_by": "tools/rederive_limits_ic7.py --literal"}),
             "CALL_PROFILES = {"]
    for k, p in prof.items():
        lines.append(f"    {k!r}: {{")
        cur = "       "
        for f, v in p.items():
            item = f" {f!r}: {v!r},"
            if len(cur) + len(item) > 136 and cur.strip():
                lines.append(cur.rstrip())
                cur = "       "
            cur += item
        lines.append(cur.rstrip().rstrip(",") + "},")
    lines.append("}")
    lines.append("BLOCK_RESERVES_S = " + repr({b: v for b, v in reserves.items()}))
    lines.append("BLOCK_TAIL_S = " + repr({b: v for b, v in tails.items()}))
    return "\n".join(lines) + "\n"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if "--literal" in argv:
        ev = Path(argv[argv.index("--literal") + 1])
        sys.stdout.write(literal(ev))
        return 0
    write = Path(argv[argv.index("--write") + 1]) if "--write" in argv else None
    if write is not None and str(write) in args:
        args.remove(str(write))
    roots = [Path(a) for a in args] or sorted(p for p in (HERE.parent / "tests/fixtures/live").iterdir() if p.is_dir())
    samples, consoles, files = read_roots(roots)
    table, reserves, tails = derive(samples, consoles)
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(json.dumps({"what": "BUILD IC-7 (D11): the live call-log lines on record (every staged log: the fixtures, R6, R7) and the planning table they give",
                                     "roots": [str(r) for r in roots], "files": files, "samples": samples, "consoles": consoles,
                                     "table": table, "reserves": reserves, "tails": tails}, indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        print(f"written: {write} ({sha(write)[:16]})")
    print(f"{len(samples)} live lines from {len(files)} logs")
    for k in sorted(table):
        t = table[k]
        print(f"  {k:<24} completed {t['completed']} censored {t['censored']} → {t['planning_tokens']} tokens; floor {t['floor_rate']}; "
              f"{t['planning_s']} s; first line {t['first_line_s']}; unresolved cuts {t['censored_unresolved']}")
    print("  unmeasured:", sorted(k for k in SPEC if k not in table))
    print("  reserves:", json.dumps(reserves))
    print("  tails:", json.dumps(tails))
    if "--diff" in argv:
        sys.path.insert(0, str(HERE.parent / "src"))
        import naipc_requirements as NR  # noqa: E402
        mine = profiles(table)
        have = getattr(NR, "CALL_PROFILES", {}) or {}
        n = 0
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
        print("  the records equal the shipped profiles" if not n else f"  {n} difference(s): re-derive before the next build (Shane's choice)")
        return 1 if n else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
