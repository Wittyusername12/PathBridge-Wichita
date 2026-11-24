"""BUILD IC-7 (§7 R3) — the checks with no model call, on the installed tree, in this window's environment:
  1. the switch table: each switch of NR.SWITCH_REACH on or off here (on unless set to 0, off, false or no; NAIPC_IC6_PROMPT off turns
     IC-6's switches and IC-7's off with it, an older switch off turns them all off) and the calls it reaches (NR.SWITCH_REACH: the reach
     tests/t_switches_ic7.py measured, evidence/switch_reach_ic7.json);
  2. each strict wire's counts (NR.wire_counts) and the build's lint (NR.schema_problems): Call 1's four variants (the brief block with
     and without, the Oracle's parts with and without) and Call 2's two — properties, lists, optional 0, unions 0, empty objects 0,
     keywords outside the allowlist 0, the lint's problems 0;
  3. the rule lines each Muse call receives with NAIPC_IC7_PROMPT on (REG.number_rules_text on the call's contract view: Call 1, Call 2,
     the update);
  4. the instruction blocks' diff against IC-6's: Call 1's and Call 2's requests rendered by this build with NAIPC_IC7_PROMPT on and
     off (off is IC-6's request, byte for byte: tests/t_switches_ic7.py), each in a process of its own, in a new folder beside the four
     (<root>/naipc-r3-ic7-<UTC stamp>: a copy of the Muse's code, R7's Oracle checkpoints from tools/fixtures/r7_oracle, the fixtures'
     Wichita pre data read from tools/fixtures, the contract) — the model is never called (the call is replaced by a capture: no key is
     read, nothing leaves the machine) — and every line that differs, printed.
The four folders are read, never written.
Usage: python -I -B r3_checks_ic7.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import contextlib, difflib, hashlib, io, json, os, shutil, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
WI = "wichita-2026-04-18"
R7_RUN = "20261116T235259Z"
R7_ORACLE = HERE / "fixtures" / "r7_oracle"
DATA = HERE / "fixtures" / WI / "pre"
RUN_VARS = ("NAIPC_RUN_ID", "NAIPC_REQUIREMENTS", "NAIPC_CHALLENGE", "NAIPC_URL_SLASH", "NAIPC_RERUN_AFTER_END", "NAIPC_CHANGES",
            "NAIPC_BLOCK_END", "NAIPC_BLOCK", "NAIPC_ACCEPT", "CURVEBALL_TEXT", "CONTRACT_OVERRIDE", "ANTHROPIC_API_KEY")
IGNORE = shutil.ignore_patterns("output", "output_prev", "checkpoints*", "coverage", "runs", "chat-context*", "__pycache__", "test_output")
# Call 2's prompt reads Call 1's personas, handle and lead: a fixed stand-in (the same in both renders, so it adds no difference)
STAND_IN = SimpleNamespace(personas=[SimpleNamespace(name="Persona", age=40, occupation="occupation", location="location",
                                                     barriers=["barrier"], channels=["channel"])], brand_handle="@handle", hero_lead="lead")


class Captured(Exception):
    pass


def render(work, out):
    """(the --render mode, in its own process) Call 1's and Call 2's requests as this tree writes them with this process's switches —
    {key: {system, user, wire}} into `out`; the model is never called."""
    for v in RUN_VARS:
        os.environ.pop(v, None)
    os.environ.update(NAIPC_RUN_ID=R7_RUN, NAIPC_CHALLENGE=WI, NAIPC_REQUIREMENTS=str(work / "requirements" / f"{WI}.json"))
    sys.path.insert(0, str(work / "muse-harness"))
    with contextlib.redirect_stdout(io.StringIO()):
        import muse_harness as MH
        NR, REG = MH.NR, MH.REG
        doc = MH._brief_contract()
        eps = NR.endpoint_names(doc) if doc else []
        want = {p.stem: json.loads(p.read_bytes().decode("utf-8-sig")) for p in sorted(DATA.glob("*.json"))}
        raw = {ep: want[ep] for ep in eps if ep in want}
        REG.set_endpoints(eps)
        domain = MH.detect_domain(raw)
        octx = MH.load_oracle_checkpoints(str(work / "oracle-harness" / "checkpoints"))
        MH._BRIEF.update(doc=doc, facts=MH.muse_pipeline_facts(len(eps)), raw=None, answers={})
        reqs = {}

        def capture(name, key, api_key, system, user, wire, parts=True, pop=()):
            reqs[key] = {"system": system, "user": user, "wire": wire, "parts": bool(parts)}
            raise Captured(key)
        MH._call_admitted = capture
        for fn, args in ((MH.generate_strategic, (None, MH._summarize_api_data(raw), domain, octx, "")),
                         (MH.generate_creative, (None, STAND_IN, domain, octx, ""))):
            try:
                fn(*args)
            except Captured:
                pass
    Path(out).write_text(json.dumps({"ic7": NR.ic7_prompt(), "requests": reqs}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def main(argv):
    if "--render" in argv:
        i = argv.index("--render")
        return render(Path(argv[i + 1]), Path(argv[i + 2]))
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path.home() / "OneDrive" / "Documents"
    muse = next((root / x for x in ("muse-harness", "Muse-harness") if (root / x / "muse_harness.py").is_file()), None)
    req = root / "landing-harness" / "requirements" / f"{WI}.json"
    if muse is None or not req.is_file():
        print(f"R3 not run: {'no muse_harness.py' if muse is None else 'no contract ' + str(req)} under {root}")
        return 2
    sys.path.insert(0, str(muse))
    with contextlib.redirect_stdout(io.StringIO()):
        import muse_harness as MH
    NR, REG = MH.NR, MH.REG
    print("R3 switches (on unless set to 0, off, false or no; NAIPC_IC6_PROMPT off turns IC-6's switches and IC-7's off with it):")
    for sw, reach in NR.SWITCH_REACH.items():
        on_ = NR.ic7_prompt() if sw == "NAIPC_IC7_PROMPT" else NR.ic6(sw) if sw in NR.IC6_SWITCHES else NR._IC6_BASE
        print(f"  {sw}: {'on' if on_ else 'off'} — reaches {', '.join(reach) if reach else 'no call (its name is kept)'}")
    bad = 0
    print("R3 the strict wires (NR.wire_counts; the build's lint, NR.schema_problems):")
    for k, kw in (("muse/call1", dict(brief=True, parts=True)), ("muse/call1", dict(brief=False, parts=True)),
                  ("muse/call1", dict(brief=True, parts=False)), ("muse/call1", dict(brief=False, parts=False)),
                  ("muse/call2", dict(parts=True)), ("muse/call2", dict(parts=False))):
        w = MH.muse_wire(k, **kw)
        c, probs = NR.wire_counts(w), NR.schema_problems(w)
        bad += bool(probs) + bool(c["optional"] or c["union"] or c["empty_objects"] or c["keywords_outside"])
        h = hashlib.sha256(json.dumps(w, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        print(f"  {k} ({', '.join(f'{a} {b}' for a, b in kw.items())}): sha256 {h}; {c['properties']} properties, {c['lists']} lists; optional "
              f"{c['optional']}, unions {c['union']}, empty objects {c['empty_objects']}, keywords outside the allowlist "
              f"{c['keywords_outside']}; the lint: {len(probs)} problem(s)" + (f" — {'; '.join(probs[:4])}" if probs else ""))
    print("R3 the rule lines each Muse call receives with NAIPC_IC7_PROMPT on (its contract's view; the Oracle's examples follow them):")
    for k in ("muse/call1", "muse/call2", "muse/update"):
        print(f"  {k}:")
        for ln in REG.number_rules_text(MH.CONTRACTS[k]["rules_view"], ic4=True).splitlines():
            print(f"    {ln}")
    work = root / f"naipc-r3-ic7-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    if work.exists():
        print(f"R3 the diff not run: {work} exists")
        return 2
    shutil.copytree(muse, work / "muse-harness", ignore=IGNORE)
    (work / "muse-harness" / "checkpoints").mkdir()
    shutil.copytree(R7_ORACLE, work / "oracle-harness" / "checkpoints")
    (work / "requirements").mkdir()
    shutil.copy2(req, work / "requirements" / req.name)
    got, exits = {}, {}
    for state in ("1", "0"):
        env = {k: v for k, v in os.environ.items() if k not in RUN_VARS}
        env.update(NAIPC_IC7_PROMPT=state, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        out = work / f"requests_ic7_{'on' if state == '1' else 'off'}.json"
        p = subprocess.run([sys.executable] + (["-I"] if sys.flags.isolated else []) + ["-B", str(Path(__file__).resolve()), "--render",
                                                                                        str(work), str(out)],
                           cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        exits[state] = p.returncode
        if p.returncode != 0 or not out.is_file():
            print(f"R3 the render with NAIPC_IC7_PROMPT={state} failed (exit {p.returncode}): {(p.stderr or p.stdout)[-400:]}")
            return 1
        got[state] = json.loads(out.read_text(encoding="utf-8"))
    print(f"R3 the instruction blocks: rendered in {work} with NAIPC_IC7_PROMPT on (this build) and off (IC-6's request); the model "
          "not called. The lines that differ (- IC-6's, + this build's):")
    if got["1"].get("ic7") is not True or got["0"].get("ic7") is not False:
        print(f"R3 the renders' switch states are not on and off: {got['1'].get('ic7')}, {got['0'].get('ic7')} — a switch this window "
              "sets turns IC-7 off (the table above)")
        bad += 1
    for k in ("muse/call1", "muse/call2"):
        a, b = (got["0"]["requests"] or {}).get(k) or {}, (got["1"]["requests"] or {}).get(k) or {}
        for part in ("system", "user"):
            d = [ln for ln in difflib.unified_diff((a.get(part) or "").splitlines(), (b.get(part) or "").splitlines(), lineterm="", n=0)
                 if not ln.startswith(("---", "+++", "@@"))]
            print(f"  {k} {part}: " + (f"{len(d)} line(s)" if d else "the same"))
            for ln in d:
                print(f"    {ln[:1]} {ln[1:]}")
        w = b.get("wire")
        print(f"  {k} the wire: " + (("sent — sha256 " + hashlib.sha256(json.dumps(w, sort_keys=True, ensure_ascii=False).encode("utf-8"))
                                       .hexdigest()[:16]) if w else "not sent") + "; IC-6's: " + ("sent" if a.get("wire") else "not sent"))
    print(f"R3 the copy: {work}")
    print("R3 " + ("PASS: every wire lints clean, and the renders ran" if not bad else f"FAIL: {bad} finding(s) above — ask Shane before R4"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
