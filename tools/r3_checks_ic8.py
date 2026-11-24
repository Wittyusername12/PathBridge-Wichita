"""BUILD IC-8 (§7 R3; from r3_checks_ic7.py) — the checks with no model call, on the installed tree, in this window's environment:
  1. the switch table: each switch of NR.SWITCH_REACH and IC-8's NAIPC_IC8_PROMPT, on or off here (on unless set to 0, off, false or no,
     any case; a switch off turns off every switch built on it: NAIPC_IC6_PROMPT IC-7's and IC-8's, NAIPC_IC7_PROMPT IC-8's), with the
     calls each reaches — NR.SWITCH_REACH's rows as the tree declares them; IC-8's three are Q-SWITCH's (BUILD_PROMPT_IC8 §0), the reach
     tests/t_switches_ic8.py measured (SW-3), held here as IC8_REACH: the tree's NR.SWITCH_REACH has no IC-8 row;
  2. the display policy: REG.DISPLAY_POLICY, each display limit's destination, unit, drafting target and its source, hard maximum,
     owner, whether an excerpt may be made, the constant its target reads and its version (REG.POLICY_VERSION);
  3. the three changed request lines, rendered with NAIPC_IC8_PROMPT on and off — each harness and state in a process of its own (the
     switch is read once, when the module loads), in a new folder beside the four (<root>/naipc-r3-ic8-<UTC stamp>: copies of the
     Oracle's, the Muse's and the Architect's code, and the Wichita contract with its engine sidecar):
       oracle/update — the diagram's kind line (diagram_update_words, on the kind line the update's request holds);
       muse/update — the tagline line (MUSE_TAGLINE_LINE) and the strategic schema the update shows (NR.schema_block of shown_schema):
         its two taglines' entries, and the rest of it compared;
       architect/rationale — its whole request on the fixtures' Wichita pre data (tools/fixtures), the engine's clock set to the
         challenge's day (NAIPC_AS_OF), the call replaced by a capture: the model is never called (the window's key is not passed to
         the render, the API's address there is a closed local port, and the capture takes the request before any client exists);
     each part that differs, printed (- off: IC-7's request, + on: this build's), and what stays the same named;
  4. a dry invocation allocation: NR.allocate_invocation twice for one run of the Muse, in the copy's own checkpoints folder, the
     record read back through NR.read_records — two numbers, two ids.
The four folders are read, never written.
--staged (Claude's side, after the four folders' logs are staged read only): rederive_limits_ic8.py on the staged folders and the
tree's live fixtures (tests/fixtures/live: their consoles give the legacy windows), every line of it printed; then whether any measured
value was floored; then NR.read_records on each staged admission file and admit_findings.jsonl, run by run, with the windows of the
consoles that name the run.
Usage: python -I -B r3_checks_ic8.py --root "%USERPROFILE%\\OneDrive\\Documents"
       python3 -I -B r3_checks_ic8.py --staged <folder> [<folder> ...] [--src <the tree's src>]"""
import contextlib, difflib, io, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WI = "wichita-2026-04-18"
RENDER_RUN = "20260418T150000Z"           # the render's run id: the challenge's day (no run of Shane's has it)
AS_OF = "2026-04-18T10:00:00"             # the engine's clock in the render (no zone: read as written on every machine)
DATA = HERE / "fixtures" / WI / "pre"
CLOSED = "http://127.0.0.1:9"             # the render's API address: a local port nothing serves
IC8_REACH = ("oracle/update", "muse/update", "architect/rationale")      # Q-SWITCH; tests/t_switches_ic8.py SW-3 measured it
RUN_VARS = ("NAIPC_RUN_ID", "NAIPC_REQUIREMENTS", "NAIPC_CHALLENGE", "NAIPC_URL_SLASH", "NAIPC_RERUN_AFTER_END", "NAIPC_CHANGES",
            "NAIPC_BLOCK_END", "NAIPC_BLOCK", "NAIPC_ACCEPT", "NAIPC_AS_OF", "NAIPC_INVOCATION_ID", "NAIPC_INVOCATION_NUMBER",
            "CURVEBALL_TEXT", "CONTRACT_OVERRIDE")
IGNORE = shutil.ignore_patterns("output", "output_prev", "checkpoints*", "coverage", "runs", "versions", "chat-context*", "__pycache__",
                                "test_output")
HARNESS = {"oracle": ("oracle-harness", "oracle_harness"), "muse": ("muse-harness", "muse_harness"),
           "architect": ("architect-harness", "architect_harness")}
TAGLINES = ("short_tagline", "og_description")
FOLDER_TRACK = {"oracle-harness": "oracle", "architect-harness": "architect", "muse-harness": "muse", "landing-harness": "landing"}


class Captured(Exception):
    pass


def _quiet():
    return contextlib.redirect_stdout(io.StringIO())


def render(work, track, out):
    """(the --render mode, in its own process) the lines `track`'s changed request holds with this process's switches, as JSON into
    `out`; the model is never called."""
    for v in [k for k in os.environ if k in RUN_VARS or k.startswith("ANTHROPIC_")]:
        os.environ.pop(v, None)
    os.environ.update(NAIPC_RUN_ID=RENDER_RUN, NAIPC_CHALLENGE=WI, NAIPC_AS_OF=AS_OF, ANTHROPIC_BASE_URL=CLOSED,
                      NAIPC_REQUIREMENTS=str(work / "requirements" / f"{WI}.json"))
    folder, mod = HARNESS[track]
    sys.path.insert(0, str(work / folder))
    got = {}
    with _quiet():
        H = __import__(mod)
        NR = H.NR
        if track == "oracle":
            got["the diagram's kind line"] = H.diagram_update_words(H.DIAGRAM_KIND_IC4, {"f.diagram": None})
        elif track == "muse":
            got["the tagline line"] = H.MUSE_TAGLINE_LINE
            got["the strategic schema"] = NR.schema_block(H.shown_schema(H.MuseStrategic), H.MUSE_STRATEGIC_FIELDS)
        else:
            REG, ENG = H.REG, H.ENG
            doc = H._contract_doc()
            eps = NR.endpoint_names(doc) if doc else []
            raw = {p.stem: json.loads(p.read_bytes().decode("utf-8-sig")) for p in sorted(DATA.glob("*.json")) if p.stem in eps}
            spec, why = REG.admit(ENG.load_spec(os.environ["NAIPC_REQUIREMENTS"]), "data", "engine sidecar")
            bundle = ENG.for_page(spec, doc, raw, "", page="architect", base=None)
            req = {}

            def capture(api_key, system_prompt, user_prompt, model=None, fmt=None):
                req.update(system=system_prompt, user=user_prompt, schema=fmt)
                raise Captured("architect/rationale")
            H.call_claude = capture
            os.environ["ANTHROPIC_API_KEY"] = "r3: no call"      # the request is built only when this is set; the capture sends nothing
            try:
                H._rationale(spec, doc, raw, "", bundle)
            except Captured:
                pass
            os.environ.pop("ANTHROPIC_API_KEY", None)
            got.update({"endpoints": len(raw), "bundle": bundle.get("status"), "spec": why or "read", **req})
    st = getattr(NR, "ic8_prompt", None)
    Path(out).write_text(json.dumps({"ic8": st() if st else None, "track": track, "lines": got}, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    return 0


def parts(text):
    """A request's text as the diff reads it: its lines, a paragraph (a long line not indented) read sentence by sentence."""
    out = []
    for ln in (text or "").splitlines():
        out += ([x for x in re.split(r"(?<=[.:]) (?=[A-Z\"(<#])", ln) if x != ""] or [ln]) if len(ln) > 400 and not ln[:1].isspace() \
            else [ln]
    return out


def diff(a, b):
    return [ln for ln in difflib.unified_diff(a, b, lineterm="", n=0) if not ln.startswith(("---", "+++", "@@"))]


def show(rows, ind="    "):
    for ln in rows:
        sign, body = ln[:1], ln[1:]
        for i, x in enumerate(body.splitlines() or [""]):
            print(f"{ind}{sign if i == 0 else ' '} {x}")


def policy_line(k, p):
    """One display limit of REG.DISPLAY_POLICY, each field as the tree holds it ("none" where it holds no value)."""
    v = {f: ("none" if p.get(f) is None else p.get(f)) for f in ("dest", "unit", "target", "target_src", "hard", "owner", "const", "version")}
    return (f"  {k}: {v['dest']}, in {v['unit']}; target {v['target']}" + (f" ({v['target_src']})" if p.get("target_src") else "")
            + f"; hard maximum {v['hard']}; owner {v['owner']}; excerpt {'yes' if p.get('excerpt') else 'no'}; reads {v['const']}; "
              f"version {v['version']}")


def staged(argv):
    """--staged: the staged logs through rederive_limits_ic8.py and NR.read_records (Claude's side)."""
    src = Path(argv[argv.index("--src") + 1]) if "--src" in argv else HERE.parent / "src"
    roots = [Path(a) for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--src")]
    roots = [r for r in roots if r.is_dir()]
    live = HERE.parent / "tests" / "fixtures" / "live"
    fix = sorted(p for p in live.iterdir() if p.is_dir()) if live.is_dir() else []
    if not roots:
        print("R3 staged: no staged folder given")
        return 2
    sys.path.insert(0, str(HERE))
    import rederive_limits_ic8 as R8
    NR = R8.load_nr(src)
    cons = [p for r in roots + fix for p in [r / "console.txt"] if p.is_file()]
    wins = [w for p in cons for w in NR.legacy_windows(p.read_text(encoding="utf-8", errors="replace"))]
    print(f"R3 staged: {len(roots)} folder(s) staged; the tree's live fixtures: {len(fix)} folder(s); {len(cons)} console(s), "
          f"{len(wins)} window(s)")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        e = R8.main([str(r) for r in roots + fix] + ["--src", str(src), "--diff"])
    print(f"R3 rederive_limits_ic8.py (the staged folders and the tree's live fixtures, --diff; exit {e}):")
    for ln in buf.getvalue().splitlines():
        print(f"  {ln}")
    _t, _r, _tl, rows, _lack = R8.derive(R8.read_roots(roots + fix, NR))
    meas = [r for r in rows if r["status"] == "measured"]
    off = [(r["track"], r["run_id"], r["number"]) for r in meas if r["outside"] != round(r["wall"] - r["calls_s"], 3) or r["outside"] < 0]
    print(f"R3 nothing floored: {len(meas)} measured value(s), each its wall seconds less its calls' seconds, none below 0" if not off else
          f"R3 floored or below 0: {off}")
    print("R3 read_records (each run read with the windows of the consoles that name it):")
    bad, n_files = len(off), 0
    for root in roots:
        for h in sorted(p for p in root.iterdir() if p.is_dir() and p.name.lower() in FOLDER_TRACK):
            tr = FOLDER_TRACK[h.name.lower()]
            for f in sorted((h / "checkpoints").glob("admission_*.jsonl")) + sorted((h / "checkpoints").glob("admit_findings.jsonl")):
                n_files += 1
                rel = f.relative_to(root).as_posix()
                for rid in NR.record_run_ids(f):
                    r = NR.read_records(f, "run", rid=rid, strict=False, windows=[w for w in wins if w["track"] == tr])
                    bad += len(r.refused)
                    amb = sum(1 for x in r.lines if x.get("_invocation") == "ambiguous")
                    if f.name.startswith("admission_"):
                        split = {}
                        for g, ls in r.groups().items():
                            k = "ambiguous" if g == "ambiguous" else f"invocation {ls[0].get('invocation_number')}"
                            split[k] = split.get(k, 0) + len(ls)
                        proved = sum(1 for x in r.lines if x.get("_call") == "proved by the call log")
                        print(f"  {rel}: run {rid}: {r.total().n} admission(s) — "
                              + ", ".join(f"{k}: {v}" for k, v in sorted(split.items())) + f"; each one's call proved by the call log: "
                              f"{proved} of {len(r.lines)}; refused: {len(r.refused)}")
                    else:
                        print(f"  {rel}: run {rid}: {r.total().n} row(s) — in an invocation: {len(r.lines) - amb}; ambiguous: {amb} "
                              f"(a row holds no time and no call); refused: {len(r.refused)}")
    print(f"R3 staged: {n_files} record file(s) read" + ("" if not bad else f"; {bad} problem(s) above") +
          " — compare each line with the expected output")
    return 0 if n_files and not bad else 1


def main(argv):
    if "--render" in argv:
        i = argv.index("--render")
        return render(Path(argv[i + 1]), argv[i + 2], Path(argv[i + 3]))
    if "--staged" in argv:
        return staged([a for a in argv if a != "--staged"])
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path.home() / "OneDrive" / "Documents"
    have = {t: next((root / x for x in (f, f[:1].upper() + f[1:]) if (root / x / f"{m}.py").is_file()), None)
            for t, (f, m) in HARNESS.items()}
    hub = root / "landing-harness"
    req = hub / "requirements" / f"{WI}.json"
    eng = hub / "requirements" / f"{WI}.engine.json"
    miss = [t for t, p in have.items() if p is None] + [str(p) for p in (req, eng) if not p.is_file()]
    if miss or not (hub / "naipc_requirements.py").is_file():
        print(f"R3 not run: {', '.join(miss) or 'no naipc_requirements.py in landing-harness'} under {root}")
        return 2
    sys.path.insert(0, str(hub))
    with _quiet():
        import naipc_requirements as NR
        import naipc_registry as REG
    print("R3 switches (on unless set to 0, off, false or no, any case; a switch off turns off every switch built on it):")
    for sw, reach in NR.SWITCH_REACH.items():
        on_ = NR.ic7_prompt() if sw == "NAIPC_IC7_PROMPT" else NR.ic6(sw) if sw in NR.IC6_SWITCHES else NR._IC6_BASE
        print(f"  {sw}: {'on' if on_ else 'off'} — reaches {', '.join(reach) if reach else 'no call (its name is kept)'}")
    print(f"  NAIPC_IC8_PROMPT: {'on' if NR.ic8_prompt() else 'off'} — reaches {', '.join(IC8_REACH)}")
    bad = 0
    pol = getattr(REG, "DISPLAY_POLICY", None) or {}
    print(f"R3 the display policy (REG.DISPLAY_POLICY, version {getattr(REG, 'POLICY_VERSION', None)}):")
    for k, p in pol.items():
        print(policy_line(k, p))
    bad += not pol
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    work = root / f"naipc-r3-ic8-{stamp}"
    if work.exists():
        print(f"R3 the renders not run: {work} exists")
        return 2
    for t, p in have.items():
        shutil.copytree(p, work / HARNESS[t][0], ignore=IGNORE)
    (work / "requirements").mkdir()
    for p in (req, eng):
        shutil.copy2(p, work / "requirements" / p.name)
    got = {}
    for state in ("1", "0"):
        env = {k: v for k, v in os.environ.items() if k not in RUN_VARS and not k.startswith("ANTHROPIC_")}
        env.update(NAIPC_IC8_PROMPT=state, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        for t in HARNESS:
            out = work / f"render_{t}_{'on' if state == '1' else 'off'}.json"
            p = subprocess.run([sys.executable] + (["-I"] if sys.flags.isolated else []) + ["-B", str(Path(__file__).resolve()), "--render",
                                                                                            str(work), t, str(out)],
                               cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            if p.returncode != 0 or not out.is_file():
                print(f"R3 the render of {t} with NAIPC_IC8_PROMPT={state} failed (exit {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
                return 1
            got[(t, state)] = json.loads(out.read_text(encoding="utf-8"))
    states = {k: v.get("ic8") for k, v in got.items()}
    print(f"R3 the three changed request lines (- off: IC-7's request, + on: this build's), rendered in {work} with NAIPC_IC8_PROMPT on "
          "and off; the model not called:")
    if any(states[(t, "1")] is not True or states[(t, "0")] is not False for t in HARNESS):
        print(f"R3 the renders' switch states are not on and off: {sorted((t, s, v) for (t, s), v in states.items())} — a switch this "
              "window sets turns IC-8 off (the table above)")
        bad += 1
    L = {k: v["lines"] for k, v in got.items()}
    d = diff(L[("oracle", "0")]["the diagram's kind line"].splitlines(), L[("oracle", "1")]["the diagram's kind line"].splitlines())
    print(f"  oracle/update — the diagram's kind line: {len(d)} line(s)" if d else "  oracle/update — the diagram's kind line: the same")
    show(d)
    bad += not d
    d = diff([L[("muse", "0")]["the tagline line"]], [L[("muse", "1")]["the tagline line"]])
    print("  muse/update — the tagline line:" + ("" if d else " the same"))
    show(d)
    bad += not d
    s0, s1 = (json.loads(L[("muse", s)]["the strategic schema"]) for s in ("0", "1"))
    t0, t1 = ({k: x["properties"].pop(k, None) for k in TAGLINES} for x in (s0, s1))
    same = s0 == s1
    print("  muse/update — the strategic schema the update shows, its two taglines (the rest of it: "
          + ("the same)" if same else "differs)") + ":")
    d = diff([f"{k}: {json.dumps(t0[k], ensure_ascii=False, sort_keys=True)}" for k in TAGLINES],
             [f"{k}: {json.dumps(t1[k], ensure_ascii=False, sort_keys=True)}" for k in TAGLINES])
    show(d)
    bad += (not d) + (not same)
    a0, a1 = L[("architect", "0")], L[("architect", "1")]
    d = diff(parts(a0.get("user")), parts(a1.get("user")))
    sys_same, sch_same = a0.get("system") == a1.get("system"), a0.get("schema") == a1.get("schema")
    print(f"  architect/rationale — the request on the fixtures' Wichita pre data ({a1.get('endpoints')} endpoints, the engine's bundle "
          f"{a1.get('bundle')}), its clock {AS_OF}: {len(d)} part(s) differ; its system {'the same' if sys_same else 'differs'}, its "
          f"schema {'the same' if sch_same else 'differs'}:")
    show(d)
    bad += (not d) + (not a1.get("user")) + (not sys_same) + (not sch_same)
    cp = work / HARNESS["muse"][0] / "checkpoints"
    cp.mkdir(parents=True, exist_ok=True)
    i1, i2 = (NR.allocate_invocation(cp, "muse", rid=stamp, how="r3 dry allocation") for _ in range(2))
    rr = NR.read_records(NR.invocations_path(cp, stamp), "run", rid=stamp, strict=True)
    ok = ([i1["number"], i2["number"]] == [1, 2] and i1["id"] != i2["id"] and all(re.fullmatch(r"[0-9a-f]{32}", x["id"]) for x in (i1, i2))
          and [x.get("event") for x in rr.lines] == ["started", "started"] and not rr.refused)
    print(f"R3 a dry invocation allocation (the Muse, run {stamp}, in {cp}): invocation {i1['number']} id {i1['id']}; invocation "
          f"{i2['number']} id {i2['id']}; the record read back: {len(rr.lines)} line(s), "
          f"{', '.join(str(x.get('event')) for x in rr.lines)}; refused {len(rr.refused)}" + ("" if ok else " — not as allocated"))
    bad += not ok
    print(f"R3 the copy: {work}")
    print("R3 " + ("PASS: the display policy read, the renders ran with the switch on and off, each of the three requests' lines differs "
                   "and the rest stays the same, and the dry allocation numbered two invocations" if not bad else
                   f"FAIL: {bad} finding(s) above — ask Shane before R4"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
