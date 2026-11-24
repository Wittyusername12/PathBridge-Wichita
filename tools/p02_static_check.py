#!/usr/bin/env python3
"""P0.2 T1″ — T1′ (p011b_static_check.py, its 51 checks unchanged, run from this folder) plus the requirements-contract checks.

Usage:  python -B p02_static_check.py --root "C:\\Users\\shane\\OneDrive\\Documents"
Exit 0 = every check passed; 1 = a check failed; 2 = a file is missing. Reads only; writes nothing.
r2 (2026-10-19): the P0.11b check runs as a child with PYTHONIOENCODING=utf-8 — on Windows a child writing to a pipe uses the ANSI
code page, which cannot encode the prime in "T1′" — and its stderr is printed; a missing summary line fails by name.
P0.8b (2026-10-22): six fixture-set checks — each tools/fixtures/<challenge-id>/<pre|post> folder beside this file holds its count of
.json files and its alert endpoint carries the ids that make it the pre or the post version (a set labelled by its contents, not by
its folder name — the P0.8 Chicago set was the post file under "pre").
P0.3 (2026-10-29): STDLIB_OK gains html, hashlib and datetime (the module's new imports: the page parser, the page's sha256, the
manifest's time); per track, the template's data-sec blocks and their ids = the module's section catalogue; the three committed
maps VALID against their contracts; the golden pages beside this file (coverage_golden/: small pages, CRLF copies and the coverage
recorded for each) re-checked by the installed module — this machine's html.parser must give the build's statuses; a contract,
sidecar or map that is not JSON, or a module that does not import, is a named FAIL row (exit 1), never a traceback.
P0.6 (2026-10-30): one row per vendored library file (Rule 7, D10) — present, its size and its sha256 the npm pack bytes of Chart.js
4.4.7 dist/chart.umd.js (the Oracle and the Architect inline it) and Mermaid 11.17.2 dist/mermaid.min.js (copied beside the Oracle's
page); a missing or changed file is a named FAIL row (the page would load it from the CDN instead).
P0.7 (2026-11-01): one row per P0.7 anchor (D12) — the module's records() and diff_snapshots() (each run on a two-line probe), the
pipeline's Block 1 snapshot, its Block 2 change list and NAIPC_CHANGES (their order around the run id and the Oracle), the three track
harnesses reading the change list (NR.changes_from_env), the hub reading only the Oracle's curveball.json, and NR.archive_contexts
before the write in each write_chat_context; a file that does not parse, or a module without the function, is a named FAIL row."""
import argparse, ast, hashlib, importlib.util, json, os, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACKS = ("oracle-harness", "architect-harness", "muse-harness", "landing-harness")
STDLIB_OK = {"argparse", "copy", "json", "os", "re", "sys", "pathlib", "html", "hashlib", "datetime",   # P0.3: + html, hashlib, datetime
             "shutil",      # BUILD IC-6 (D10): NR.find_tool — shutil.which resolves mmdc.cmd by PATHEXT (the standard library)
             "math",        # BUILD IC-7 (D2): NR.admit_walk — math.isfinite (a converted number is finite; the standard library)
             "atexit", "time", "uuid",      # BUILD IC-8 (D8): NR.invocation — its exit hook, its seconds, its random ids
             "contextlib",                  # BUILD IC-8 (D12; P4.4 p44h): ENG.names_mode; the exit hook's words sent to stderr
             "collections", "unicodedata"}  # BUILD IC-8 (d12c; D1): the registry's Counter; the shown text's NFC
# BUILD IC-8 (P4.4, p44t3): the build's own modules — each in every harness folder beside the others, imported inside functions
BUILD_OWN = {"naipc_requirements", "naipc_registry", "naipc_engine"}
CONTRACTS = ("wichita-2026-04-18", "kc-2026-03-28", "chicago-2026-04-04")
# P0.3: the three track templates whose data-sec blocks must equal the module's section catalogue; the golden pages beside this file
TEMPLATES = {"oracle": "oracle-harness/template.html.j2", "architect": "architect-harness/architect_template.html.j2",
             "muse": "muse-harness/muse_template.html.j2"}
GOLDEN = HERE / "coverage_golden"
# P0.6 (D10, D12): the vendored library files — (size, sha256) of the npm pack bytes (chart.js@4.4.7, mermaid@11.17.2; finding 8)
VENDORED = {
    "oracle-harness/vendor/chart.umd.js": (205615, "2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb"),
    "oracle-harness/vendor/mermaid.min.js": (3572661, "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"),
    "architect-harness/vendor/chart.umd.js": (205615, "2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb"),
}
# P0.8b: the fixture sets beside this check — (files, alert endpoint, status, alert ids) that make each set the pre or the post version
FIXTURES = {
    ("wichita-2026-04-18", "pre"):  (7, "system-status", "strained", ["status-001", "status-002"]),
    ("wichita-2026-04-18", "post"): (7, "system-status", "emergency", ["status-001", "status-010", "status-011", "status-012"]),
    ("kc-2026-03-28", "pre"):       (8, "supply-alerts", "normal", ["sa-001", "sa-002"]),
    ("kc-2026-03-28", "post"):      (8, "supply-alerts", "emergency", ["sa-001", "sa-002", "sa-003", "sa-004"]),
    ("chicago-2026-04-04", "pre"):  (7, "supply-alerts", "strained", ["alert-001", "alert-002", "alert-003"]),
    ("chicago-2026-04-04", "post"): (7, "supply-alerts", "emergency", ["alert-001", "alert-004", "alert-005"]),
}


def code_strings(src):
    """String constants in pipeline.py's code, docstrings excluded."""
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def template_blocks(text):
    """P0.3: (data-sec name, id) of every tag in a template's source that carries data-sec."""
    out = []
    for tag in re.findall(r'<[a-zA-Z][^<>]*\bdata-sec="[^"]*"[^<>]*>', text):
        i = re.search(r'\bid="([^"]*)"', tag)
        out.append((re.search(r'\bdata-sec="([^"]*)"', tag).group(1), i.group(1) if i else None))
    return out


def golden_view(res):
    """P0.3: the parts of a coverage result a golden page pins — per block status, characters, items, count; per item status,
    answer, covered; the stale reasons' count; the counts; the panel rows' sources and text lengths; the link and id findings."""
    v = {"sections": {s["name"]: [s["status"], s["chars"], s["items"], s["count"]] for s in res["sections"]},
         "items": {x["id"]: [x["status"], x["answer"], x["covered"]] for x in res["items"]},
         "stale": len(res["stale"]), "counts": res["counts"],
         "rows": [[r["id"], r["answer_src"], len(r["q"]), len(r["a"]), len(r["l"])] for r in res["panel_rows"]],
         "findings": {k: res[k] for k in ("unknown_ids", "dead_links", "duplicate_ids", "duplicate_blocks", "foreign_blocks")}}
    return json.loads(json.dumps(v))


def first_diff(want, got, path=""):
    """P0.3: the first difference between two golden views, as 'path: expected …, got …' (empty when equal)."""
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            d = first_diff(want.get(k), got.get(k), f"{path}.{k}" if path else k)
            if d:
                return d
        return ""
    return "" if want == got else f"{path}: expected {json.dumps(want)[:70]}, got {json.dumps(got)[:70]}"


def off_only_spans(src):
    """BUILD IC-3 (Phase 8; the row "one function states the rules" first ran in the delivery rehearsal and failed on six sites, each of
    them IC-2's text for the switch off — the row exempted only registry_forms_lines): the character spans of the Oracle's source that no
    request with the switch on holds — the `else` of `X if ic3_prompt() else Y` (or of a name a function binds to ic3_prompt()), a
    function's statements after `if ic3_prompt(): … return`, and every docstring. [(start, end)], as character offsets."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    starts = [0]
    for ln_ in lines:
        starts.append(starts[-1] + len(ln_))

    def at(lineno, col):            # ast's columns count UTF-8 bytes; the source holds "—" and "…"
        return starts[lineno - 1] + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8", errors="ignore"))

    def span(n):
        return (at(n.lineno, n.col_offset), at(n.end_lineno, n.end_col_offset))

    def is_switch(t, names):
        return ((isinstance(t, ast.Call) and getattr(t.func, "id", getattr(t.func, "attr", None)) == "ic3_prompt" and not t.args)
                or (isinstance(t, ast.Name) and t.id in names))
    out = []

    def body_of(stmts, names):
        for k, st in enumerate(stmts):
            if (isinstance(st, ast.If) and is_switch(st.test, names) and not st.orelse and st.body
                    and isinstance(st.body[-1], ast.Return) and k + 1 < len(stmts)):
                out.append((span(stmts[k + 1])[0], span(stmts[-1])[1]))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                out.append(span(b[0]))
            names = {t.id for st in ast.walk(node) if isinstance(st, ast.Assign) and is_switch(st.value, set())
                     for t in st.targets if isinstance(t, ast.Name)} if not isinstance(node, ast.Module) else set()
            body_of(b, names)
            for sub in ast.walk(node):
                if isinstance(sub, ast.IfExp) and is_switch(sub.test, names):
                    out.append(span(sub.orelse))
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # a redirected stdout on Windows is cp1252: never crash on a prime
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    root = Path(ap.parse_args().root)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")   # the child writes to a pipe; on Windows that is cp1252 unless told
    p = subprocess.run([sys.executable, "-B", str(HERE / "p011b_static_check.py"), "--root", str(root)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    print(p.stdout.rstrip())
    if p.stderr.strip():
        print("  p011b_static_check.py stderr:")
        for line in p.stderr.rstrip().splitlines():
            print(f"    {line}")
    if p.returncode == 2:
        return 2
    t1 = [l for l in p.stdout.splitlines() if l.startswith("T1′:")]
    if not t1:
        print(f"\nT1″: FAILED — the P0.11b check (T1′) did not finish (exit {p.returncode}, no summary line); see its stderr above")
        return 1
    t1_total = int(t1[0].split()[1])
    t1_pass = int(t1[0].split()[3])
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    copies = {t: root / t / "naipc_requirements.py" for t in TRACKS}
    present = {t: q.is_file() for t, q in copies.items()}
    hashes = {hashlib.sha256(q.read_bytes()).hexdigest() for t, q in copies.items() if present[t]}
    check("naipc_requirements.py in all four folders, byte-identical", all(present.values()) and len(hashes) == 1,
          "missing: " + ", ".join(t for t, v in present.items() if not v) if not all(present.values()) else f"{len(hashes)} distinct")
    mod = copies["landing-harness"]
    if mod.is_file():
        imps = set()
        for n in ast.walk(ast.parse(mod.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                imps |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imps.add((n.module or "").split(".")[0])
        check("naipc_requirements.py imports the standard library only, and the build's own modules beside it (BUILD IC-8)",
              imps <= STDLIB_OK | BUILD_OWN, ", ".join(sorted(imps - STDLIB_OK - BUILD_OWN)))
    else:
        check("naipc_requirements.py imports the standard library only, and the build's own modules beside it (BUILD IC-8)", False,
              "module missing")
    pipe = (root / "landing-harness" / "pipeline.py").read_text(encoding="utf-8")
    i_contract, i_pre = pipe.find("contract = resolve_contract(args)"), pipe.find("# ── Pre-flight (P0.2)")
    i_archive, i_oracle = pipe.find("archive_checkpoints(dirs[name], stamp)"), pipe.find("oracle_cmd = [")
    check("pipeline.py: the contract is resolved, then pre-flighted, before the archive and the first harness",
          -1 < i_contract < i_pre < i_archive < i_oracle, f"positions {i_contract}, {i_pre}, {i_archive}, {i_oracle}")
    names = set()
    req = root / "landing-harness" / "requirements"
    for c in CONTRACTS:
        f = req / f"{c}.json"
        if f.is_file():
            try:
                names |= {e["name"] for e in json.loads(f.read_bytes().decode("utf-8-sig"))["api"]["endpoints"]}
            except (ValueError, KeyError, TypeError):
                pass                                    # P0.3: not JSON / not a contract — the contracts row below names it
    lits = [s for s in code_strings(pipe) if "aipromptchamp.com/api" in s or s in names or ("," in s and set(s.split(",")) <= names)]
    check("pipeline.py code names no endpoint and no challenge-API URL (they come from the contract)", bool(names) and not lits, "; ".join(repr(s)[:60] for s in lits[:5]) or ("no contracts found" if not names else ""))
    pre = pipe[pipe.find("def preflight("):pipe.find("def contract_expected(")] if "def preflight(" in pipe else ""
    check("pipeline.py pre-flight uses requests (the harnesses' own client)", "import requests" in pre and "requests.get(" in pre)
    ok, detail, M, merr = True, [], None, "module missing"
    if mod.is_file():
        try:                                            # P0.3: a module that does not import fails the rows that need it, by name
            s = importlib.util.spec_from_file_location("naipc_requirements", mod); M = importlib.util.module_from_spec(s); s.loader.exec_module(M)
        except Exception as e:                          # noqa: BLE001
            M, merr = None, f"the module does not import: {type(e).__name__}: {str(e)[:100]}"
    if M is not None:
        for c in CONTRACTS:
            sc = req / f"{c}.curveball.json"
            try:
                d = M.load(req / f"{c}.json")
                if sc.is_file():
                    M.merge_curveball(d, json.loads(sc.read_bytes().decode("utf-8-sig")))
            except M.RequirementsError as e:
                ok = False; detail.append(f"{c}: {e.problems[:2]}")
            except ValueError as e:                     # P0.3: a sidecar that is not JSON (or not UTF-8) — named, no traceback
                ok = False; detail.append(f"{c}: {sc.name} is not JSON ({type(e).__name__}: {str(e)[:80]})")
    else:
        ok, detail = False, [merr]
    check("the three installed contracts validate and their sidecars merge", ok, "; ".join(detail))
    for (cid, ph), (n, ep, status, ids) in FIXTURES.items():            # P0.8b: the fixture sets, labelled by their contents
        d = HERE / "fixtures" / cid / ph
        files = sorted(p.name for p in d.glob("*.json")) if d.is_dir() else None
        doc = {}
        if files and f"{ep}.json" in files:
            try:
                doc = json.loads((d / f"{ep}.json").read_bytes().decode("utf-8-sig"))
            except ValueError:
                doc = {}
        got_ids = [a.get("id") for a in doc["alerts"]] if isinstance(doc, dict) and isinstance(doc.get("alerts"), list) else None
        got = (None if files is None else len(files), doc.get("status") if isinstance(doc, dict) else None, got_ids)
        check(f"fixtures/{cid}/{ph}: {n} files; {ep} is the {ph} version ({status}: {', '.join(ids)})", got == (n, status, ids),
              "folder missing" if files is None else f"{got[0]} files; {ep} status {got[1]!r}, ids {got[2]}")
    # P0.3 — the section catalogue in each template; the three maps; the golden pages (D12)
    for t, rel in TEMPLATES.items():
        f, name = root / rel, f"{rel}: data-sec blocks and their ids = the section catalogue"
        if not f.is_file():
            check(name, False, "template missing")
        elif M is None or not hasattr(M, "SECTIONS"):
            check(name, False, merr if M is None else "the module has no section catalogue (SECTIONS)")
        else:
            got = template_blocks(f.read_text(encoding="utf-8"))
            want = [(n, a) for n, a, *_ in M.SECTIONS[t]]
            twice = sorted({n for n, _ in got if [g[0] for g in got].count(n) > 1})
            miss, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
            check(f"{name} ({len(want)})", not (miss or extra or twice),
                  "; ".join(x for x in (f"missing {miss}" if miss else "", f"not in the catalogue {extra}" if extra else "", f"twice {twice}" if twice else "") if x))
    mok, mdet = M is not None and hasattr(M, "load_map"), []
    if mok:
        for c in CONTRACTS:
            try:
                d = M.load(req / f"{c}.json")
            except M.RequirementsError:
                mok = False; mdet.append(f"{c}: the contract does not load (see the contracts row)")
                continue
            _, probs = M.load_map(req / f"{c}.map.json", d)
            if probs:
                mok = False; mdet.append(f"{c}.map.json: {probs[0]}" + (f" (+{len(probs) - 1} more)" if len(probs) > 1 else ""))
    else:
        mdet.append(merr if M is None else "the module has no map support (load_map)")
    check("the three committed maps are VALID against their contracts", mok, "; ".join(mdet))
    try:
        G = json.loads((GOLDEN / "expected.json").read_bytes().decode("utf-8-sig"))
        cases = G["cases"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        G, cases = None, []
        check("coverage_golden: the golden pages and expected.json are present", False, f"{type(e).__name__}: {str(e)[:100]}")
    gdoc = gmap = gerr = None
    if G is not None:
        try:
            if M is None:
                raise RuntimeError(merr)
            gdoc = M.load(GOLDEN / G["contract"])
            gmap, gprobs = M.load_map(GOLDEN / G["map"], gdoc)
            if gprobs:
                raise RuntimeError(f"the golden map is not valid: {gprobs[0]}")
        except Exception as e:                          # noqa: BLE001 — every golden row fails by name instead
            gerr = f"{type(e).__name__}: {str(e)[:100]}"
    for case in cases:
        name = f"coverage golden {case.get('page')} ({case.get('label')}): as recorded"
        if gerr:
            check(name, False, gerr)
            continue
        try:
            page = (GOLDEN / case["page"]).read_bytes().decode("utf-8")
            got = golden_view(M.coverage_check(gdoc, gmap, case["track"], page, run_id=G["run_id"]))
            diff = first_diff(case["expect"], got)
            check(name, not diff, diff)
        except Exception as e:                          # noqa: BLE001
            check(name, False, f"{type(e).__name__}: {str(e)[:100]}")
    for rel, (size, sha) in VENDORED.items():                           # P0.6 (D12): the vendored files, byte for byte
        f = root / rel
        try:
            b = f.read_bytes()
            got = (len(b), hashlib.sha256(b).hexdigest())
            check(f"{rel}: present, {size:,} bytes, sha256 {sha[:16]}", got == (size, sha), f"{got[0]:,} bytes, sha256 {got[1][:16]}")
        except OSError as e:
            check(f"{rel}: present, {size:,} bytes, sha256 {sha[:16]}", False, f"missing ({type(e).__name__})")
    # P0.7 (D12): one row per P0.7 anchor — never a traceback: a module without the function or a file that does not parse fails by name
    for fn, name, probe, want in (
            ("records", "naipc_requirements.py: records() reads every envelope — a bare list, {items}, {alerts} (P0.7 D5)",
             lambda: [M.records([{"id": "a"}]), M.records({"items": [{"id": "b"}]}), M.records({"status": "x", "alerts": [{"id": "c"}]})],
             [[{"id": "a"}], [{"id": "b"}], [{"id": "c"}]]),
            ("diff_snapshots", "naipc_requirements.py: diff_snapshots() finds a record added at the curveball (P0.7 D1)",
             lambda: [(lambda e: [e.get("status"), [x.get("ref") for x in e.get("added") or []]])(
                 M.diff_snapshots({"ep": {"alerts": [{"id": "a1"}]}}, {"ep": {"alerts": [{"id": "a1"}, {"id": "a2"}]}}).get("ep") or {})],
             [["changed", ["a2"]]])):
        if M is None or not hasattr(M, fn):
            check(name, False, merr if M is None else f"the module has no {fn}() — not the P0.7 module")
            continue
        try:
            got = probe()
            check(name, got == want, f"got {json.dumps(got, default=str)[:90]}")
        except Exception as e:                          # noqa: BLE001
            check(name, False, f"{type(e).__name__}: {str(e)[:100]}")
    i_rid, i_or = pipe.find('os.environ["NAIPC_RUN_ID"] = run_id'), pipe.find("oracle_cmd = [")
    i_snap, i_cl = pipe.find("payloads = take_snapshot("), pipe.find("changes, payloads = make_change_list(")

    def body(fname):
        i = pipe.find(f"\ndef {fname}(")
        return pipe[i:pipe.find("\ndef ", i + 1)] if i > -1 else ""
    snap_b, cl_b = body("take_snapshot"), body("make_change_list")
    check("pipeline.py: Block 1 snapshots every endpoint after the run id is set, before the Oracle (P0.7 D1)",
          -1 < i_rid < i_snap < i_or and "NR.write_snapshot(" in snap_b,
          f"positions {i_rid}, {i_snap}, {i_or}" + ("" if "NR.write_snapshot(" in snap_b else "; no take_snapshot() writing NR.write_snapshot"))
    check("pipeline.py: Block 2 diffs every endpoint against the snapshot into one change list, before the Oracle (P0.7 D1)",
          -1 < i_rid < i_cl < i_or and "NR.diff_snapshots(" in cl_b and "NR.write_changes(" in cl_b,
          f"positions {i_rid}, {i_cl}, {i_or}" + ("" if "NR.diff_snapshots(" in cl_b and "NR.write_changes(" in cl_b else
                                                  "; no make_change_list() calling NR.diff_snapshots and NR.write_changes"))
    i_w, i_x = cl_b.find("NR.write_changes("), cl_b.find('os.environ["NAIPC_CHANGES"] = ')
    i_pop = pipe.rfind('os.environ.pop("NAIPC_CHANGES", None)', 0, i_snap) if i_snap > -1 else -1
    check("pipeline.py: NAIPC_CHANGES exported once the change list is written, cleared before Block 1's snapshot (P0.7 D1)",
          -1 < i_w < i_x and -1 < i_rid < i_pop < i_snap, f"write {i_w}, export {i_x}; run id {i_rid}, clear {i_pop}, snapshot {i_snap}")
    HARN = {"oracle-harness": "oracle_harness.py", "architect-harness": "architect_harness.py", "muse-harness": "muse_harness.py",
            "landing-harness": "landing_harness.py"}
    trees, srcs = {}, {}
    for t, n in HARN.items():
        try:
            srcs[t] = (root / t / n).read_text(encoding="utf-8")
            trees[t] = ast.parse(srcs[t])
        except (OSError, SyntaxError, ValueError) as e:
            trees[t] = f"{n} does not parse ({type(e).__name__}: {str(e)[:80]})"

    def nr_calls(node, attr):
        return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr
                and isinstance(c.func.value, ast.Name) and c.func.value.id == "NR"]
    for t in TRACKS[:3]:
        tr = trees[t]
        check(f"{HARN[t]}: reads this run's change list (NR.changes_from_env, NAIPC_CHANGES) (P0.7 D7)",
              not isinstance(tr, str) and bool(nr_calls(tr, "changes_from_env")), tr if isinstance(tr, str) else "no NR.changes_from_env() call")
    tr = trees["landing-harness"]
    hub = "landing_harness.py (the hub): reads only the Oracle's curveball.json, never the change list (P0.7 D12)"
    if isinstance(tr, str):
        check(hub, False, tr)
    else:
        lit = [s for s in code_strings(srcs["landing-harness"]) if "NAIPC_CHANGES" in s]
        check(hub, bool(nr_calls(tr, "read_curveball_json")) and not nr_calls(tr, "changes_from_env") and not lit,
              "; ".join(x for x in ("" if nr_calls(tr, "read_curveball_json") else "no NR.read_curveball_json() call",
                                    "it calls NR.changes_from_env()" if nr_calls(tr, "changes_from_env") else "",
                                    "it names NAIPC_CHANGES" if lit else "") if x))
    for t in TRACKS[:3]:
        tr, name = trees[t], f"{HARN[t]}: write_chat_context archives the other contexts before writing (NR.archive_contexts, P0.7 D9)"
        if isinstance(tr, str):
            check(name, False, tr)
            continue
        fn = next((n for n in ast.walk(tr) if isinstance(n, ast.FunctionDef) and n.name == "write_chat_context"), None)
        arc = nr_calls(fn, "archive_contexts") if fn else []
        wr = [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
              and c.func.attr in ("write_bytes", "write_text")] if fn else []
        ok = bool(arc and wr) and any(k.arg == "keep" for k in arc[0].keywords) and arc[0].lineno < min(wr)
        check(name, ok, "no write_chat_context()" if fn is None else "no NR.archive_contexts(…, keep=…) before the write" if not ok else "")
    # P0.7 r2 (E6): the update prompts state the schema of the fields an update may change — a call to NR.schema_block inside the prompt
    # builder (E1: computed at call time from the model the merge validates with; no limit typed into a prompt)
    for t, fname in (("oracle-harness", "update_prompt"), ("muse-harness", "muse_update_prompt")):
        tr, name = trees[t], f"{HARN[t]}: the update prompt states the schema of the fields it may change (NR.schema_block, P0.7 r2 E1)"
        if isinstance(tr, str):
            check(name, False, tr)
            continue
        fn = next((n for n in ast.walk(tr) if isinstance(n, ast.FunctionDef) and n.name == fname), None)
        check(name, bool(fn is not None and nr_calls(fn, "schema_block")), f"no {fname}()" if fn is None else f"no NR.schema_block() call in {fname}()")
    # BUILD B1 (D7): the engine module beside the Oracle — the standard library and naipc_requirements only — and every engine sidecar
    # beside the contracts valid against its challenge's staged fixture set (pre), checked by the installed engine itself
    eng = root / "oracle-harness" / "naipc_engine.py"
    name = ("oracle-harness: naipc_engine.py present; imports the standard library, the build's own modules (naipc_requirements; BUILD IC-8: "
            "naipc_registry) and (its validate command) requests (BUILD B1)")
    if eng.is_file():
        imps = set()
        for n in ast.walk(ast.parse(eng.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                imps |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imps.add((n.module or "").split(".")[0])
        extra = imps - STDLIB_OK - BUILD_OWN - {"math", "typing", "requests"}
        check(name, not extra, ", ".join(sorted(extra)))
    else:
        check(name, False, "missing")
    name = "the engine sidecars validate against their challenges' staged fixtures (pre), by the installed engine (BUILD B1)"
    try:      # importlib is the module's own import (line 23): a local import here would shadow it for all of main()
        sys.path.insert(0, str(root / "oracle-harness"))
        spec_ = importlib.util.spec_from_file_location("naipc_engine_t1", eng)
        ENG = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(ENG)
        det, n_ok = [], 0
        for sc in sorted((root / "landing-harness" / "requirements").glob("*.engine.json")):
            cid = sc.name[:-len(".engine.json")]
            fx = HERE / "fixtures" / cid / "pre"
            data = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(fx.glob("*.json"))}
            doc = ENG.NR.load(root / "landing-harness" / "requirements" / f"{cid}.json")
            probs, sets, _pres = ENG.validate_spec(json.loads(sc.read_text(encoding="utf-8")), doc, data)
            if probs or not sets:
                det.append(f"{cid}: {probs[:1] or ['no set runs']}")
            else:
                n_ok += 1
        check(name, n_ok >= 1 and not det, "; ".join(det) or ("no engine sidecar found" if not n_ok else ""))
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {str(e)[:120]}")
    # BUILD B2 (D1, D7): the browser engine beside the Architect, inlined into its page; the Architect's engine copy; its bundle builds
    name = "architect-harness: naipc_engine.js present, ASCII, no '</' (the page inlines it) (BUILD B2)"
    js = root / "architect-harness" / "naipc_engine.js"
    if js.is_file():
        jb = js.read_bytes()
        check(name, jb.isascii() and b"</" not in jb and b"function rankAll(bundle, attrs, now, name)" in jb,
              "not ASCII" if not jb.isascii() else ("holds '</'" if b"</" in jb else "no rankAll"))
    else:
        check(name, False, "missing")
    name = "architect-harness: naipc_engine.py byte-identical to oracle-harness's (BUILD B2)"
    ae = root / "architect-harness" / "naipc_engine.py"
    check(name, ae.is_file() and eng.is_file() and ae.read_bytes() == eng.read_bytes(), "missing" if not ae.is_file() else "differs")
    name = "the Architect's Get Help bundle builds from the staged fixtures (pre) for every challenge with an engine intake (BUILD B2)"
    try:
        det, n_ok = [], 0
        for sc in sorted((root / "landing-harness" / "requirements").glob("*.engine.json")):
            cid = sc.name[:-len(".engine.json")]
            spec = json.loads(sc.read_text(encoding="utf-8"))
            if not spec.get("intake"):
                continue
            fx = HERE / "fixtures" / cid / "pre"
            data = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(fx.glob("*.json"))}
            doc = ENG.NR.load(root / "landing-harness" / "requirements" / f"{cid}.json")
            b = ENG.for_page(spec, doc, data)
            if b.get("status") != "ok" or b.get("problems"):
                det.append(f"{cid}: {b.get('status')} {(b.get('problems') or [''])[:1]}")
            else:
                n_ok += 1
        check(name, n_ok >= 1 and not det, "; ".join(det) or ("no sidecar with an intake" if not n_ok else ""))
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {str(e)[:120]}")
    # BUILD B3 (P0.4, P0.5): the registry module beside the requirements module in all four folders — byte-identical, the standard library
    # only; the Wichita sidecar's registry section valid on the staged fixtures before and after the curveball; the Oracle's catalogue
    rcopies = {t: root / t / "naipc_registry.py" for t in TRACKS}
    rpresent = {t: q.is_file() for t, q in rcopies.items()}
    rhashes = {hashlib.sha256(q.read_bytes()).hexdigest() for t, q in rcopies.items() if rpresent[t]}
    check("naipc_registry.py in all four folders, byte-identical (BUILD B3)", all(rpresent.values()) and len(rhashes) == 1,
          "missing: " + ", ".join(t for t, v in rpresent.items() if not v) if not all(rpresent.values()) else f"{len(rhashes)} distinct")
    rmod = rcopies["oracle-harness"]
    if rmod.is_file():
        rimps = set()
        for n in ast.walk(ast.parse(rmod.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                rimps |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                rimps.add((n.module or "").split(".")[0])
        check("naipc_registry.py imports the standard library only, and the build's own modules beside it (BUILD B3; IC-8)",
              rimps <= STDLIB_OK | {"math"} | BUILD_OWN, ", ".join(sorted(rimps - STDLIB_OK - {"math"} - BUILD_OWN)))
    else:
        check("naipc_registry.py imports the standard library only, and the build's own modules beside it (BUILD B3; IC-8)", False,
              "module missing")
    name = "the Wichita sidecar's registry section validates on the staged fixtures, pre and post, by the installed engine (BUILD B3)"
    try:
        sc = root / "landing-harness" / "requirements" / "wichita-2026-04-18.engine.json"
        spec = json.loads(sc.read_text(encoding="utf-8"))
        det = []
        for fset in ("pre", "post"):
            fx = HERE / "fixtures" / "wichita-2026-04-18" / fset
            data = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(fx.glob("*.json"))}
            sec = ENG.registry_section(spec, data, block2=fset == "post")
            ids = {e["id"] for e in sec.get("entries") or []}
            if sec.get("problems") or "es_free_beds" not in ids:
                det.append(f"{fset}: {(sec.get('problems') or ['es_free_beds not computed'])[:1]}")
        check(name, not det, "; ".join(det))
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {str(e)[:120]}")
    name = "the Oracle's section catalogue: 27 sections, each with its template block (BUILD B3)"
    check(name, M is not None and hasattr(M, "SECTIONS") and len(M.SECTIONS["oracle"]) == 27,
          merr if M is None else f"{len(M.SECTIONS['oracle'])} sections")
    # BUILD IC (D1): the author marks in the registry module; every output expression of the Oracle template declares its author
    name = "naipc_registry.py: the five author marks (U+E0A1–U+E0A5, close U+E0AF) and strip_pua (BUILD IC)"
    try:
        rtext = rcopies["oracle-harness"].read_text(encoding="utf-8")
        want_ = 'MARK_OPEN = {"org": "\\ue0a1", "py": "\\ue0a2", "model": "\\ue0a3", "label": "\\ue0a4", "undeclared": "\\ue0a5"}'
        check(name, want_ in rtext and 'MARK_CLOSE = "\\ue0af"' in rtext and "def strip_pua(" in rtext, "" if want_ in rtext else "the marks differ")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {str(e)[:120]}")
    name = "the Oracle template: every output expression ends in an author filter; no macro, call, filter or set block, include (BUILD IC)"
    try:
        from jinja2 import Environment, nodes
        tree_ = Environment().parse((root / "oracle-harness" / "template.html.j2").read_text(encoding="utf-8"))
        authors_ = {"org", "py", "model", "label", "org_html", "model_html", "label_html", "built"}
        bad_ = [c.lineno for o in tree_.find_all(nodes.Output) for c in o.nodes
                if not isinstance(c, nodes.TemplateData) and not (isinstance(c, nodes.Filter) and c.name in authors_)]
        forb_ = [x.lineno for k in (nodes.Macro, nodes.CallBlock, nodes.FilterBlock, nodes.AssignBlock, nodes.Include, nodes.Extends,
                                    nodes.Import, nodes.FromImport) for x in tree_.find_all(k)]
        check(name, not bad_ and not forb_, f"lines {(bad_ + forb_)[:6]}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {str(e)[:120]}")
    # BUILD IC-2 (D5): the <registry> block states each writer's form behind the switch, read in one function; the three other prompt
    # sites point to it (the forms themselves are spelled out only in the block)
    try:
        osrc_ = (root / "oracle-harness" / "oracle_harness.py").read_text(encoding="utf-8")
        blk_ = osrc_[osrc_.find("def registry_forms_lines("):osrc_.find("def registry_prompt_block(")]
        name = "the Oracle's <registry> block states each form — a quote, a comparison, a declaration — behind NAIPC_IC2_PROMPT, read in one function (BUILD IC-2)"
        miss_ = [f for f in ("[[q:<where>|", "[[calc:<op>|<id a>|<id b>]]", "[[nf:<the text>|") if f not in blk_]
        check(name, not miss_ and "def ic2_prompt(" in osrc_ and osrc_.count('"NAIPC_IC2_PROMPT"') == 1, ", ".join(miss_) or "the switch")
        name = "the three other prompt sites point to the block: the system prompt, the update's rule 8, the deliverables' first lines (BUILD IC-2)"
        # IC-3 (D10): the update's rule 8 and the deliverables' first lines are constants with IC-2's words for the switch off
        # (_UPDATE_RULE8, _DLV_FIGURES), each joining _IC2_RULE8; with the switch on they point to <number_rules>
        check(name, "(one of these two, or another form the request's \"\n                \"<registry> lists):" in osrc_
              and osrc_.count("+ _IC2_RULE8") >= 2 and "_UPDATE_RULE8 = (" in osrc_ and "_DLV_FIGURES = (" in osrc_
              and '_IC2_RULE8 = ", or another form <registry> lists" if ic2_prompt() else ""' in osrc_, "a pointing clause is missing")
    except Exception as e:  # noqa: BLE001
        check("the Oracle's <registry> block and its three pointing clauses (BUILD IC-2)", False, f"{type(e).__name__}: {str(e)[:120]}")
    # BUILD IC-3 (§5 Phase 8): this build's rows — one reader of numbers, one call helper, the request check's result for these bytes,
    # one set of rules
    try:
        osrc_ = (root / "oracle-harness" / "oracle_harness.py").read_text(encoding="utf-8")
        rsrc_ = (root / "oracle-harness" / "naipc_registry.py").read_text(encoding="utf-8")
        qsrc_ = (root / "oracle-harness" / "naipc_requirements.py").read_text(encoding="utf-8")
        check("one reader of numbers: naipc_registry.quantities is defined once; the harness defines no number pattern of its own (BUILD IC-3)",
              rsrc_.count("\ndef quantities(") == 1 and "NUM_RE = re.compile" not in osrc_ and "REG.quantities(" in osrc_)
        calls_ = {t: (root / t / f).read_text(encoding="utf-8") for t, f in (("oracle-harness", "oracle_harness.py"), ("architect-harness", "architect_harness.py"),
                                                                             ("muse-harness", "muse_harness.py"))}
        direct_ = [t for t, c in calls_.items() if re.search(r"\.messages\.(?:create|stream)\(", c)]
        check("every model call goes through the one helper (naipc_requirements.model_call): no harness calls the SDK itself (BUILD IC-3)",
              not direct_ and "def model_call(" in qsrc_, ", ".join(direct_))
        check("the client keeps max_retries=0; no thinking parameter and no sampling parameter is sent (BUILD IC-3)",
              "max_retries=0" in osrc_ and not re.search(r"(?<![\w.])(?:thinking|temperature|top_p|top_k)\s*=", qsrc_ + osrc_),
              "a retry, thinking or sampling setting")
        rq_ = HERE / "request_check_ic3.json"
        rqd_ = json.loads(rq_.read_text(encoding="utf-8")) if rq_.is_file() else {}
        check("the check on the request bodies passed on this build's own capture, for these bytes of oracle_harness.py (BUILD IC-3)",
              rqd_.get("pass") == rqd_.get("total") and rqd_.get("harness_sha256") == hashlib.sha256((root / "oracle-harness" / "oracle_harness.py").read_bytes()).hexdigest(),
              f"{rqd_.get('pass')}/{rqd_.get('total')}; recorded for {str(rqd_.get('harness_sha256'))[:12]}" if rqd_ else "tools/request_check_ic3.json missing")
        if '_RULES_TEXT = """' in osrc_:      # a tree before IC-7: the harness's own literal states the rules
            rules_ = osrc_[osrc_.find('_RULES_TEXT = """'):osrc_.find('</number_rules>"""')]
            one_, r_stray_ = True, []
        else:   # BUILD IC-7 (D4): the rules are stated once, in naipc_registry.py; oracle_harness.py takes _RULES_TEXT from that function
            r_lo_, r_hi_ = rsrc_.find("RULES_HEAD = "), rsrc_.find("\ndef number_rules_text(")
            r_end_ = rsrc_.find("\n\n\ndef ", r_hi_ + 1) if r_hi_ >= 0 else -1
            rules_ = rsrc_[r_lo_:r_hi_] if 0 <= r_lo_ < r_hi_ else ""
            one_ = ("_RULES_TEXT = REG.number_rules_text(None, ic4=False)" in osrc_ and rsrc_.count("\ndef number_rules_text(") == 1
                    and r_end_ > r_hi_)
            r_off_ = off_only_spans(rsrc_)
            r_stray_ = [m.start() for m in re.finditer(r"\[\[(?:q|calc|nf|model):<", rsrc_)
                        if not (r_lo_ <= m.start() < r_end_ or any(a_ <= m.start() < b_ for a_, b_ in r_off_))]
        grammar_ = [m.start() for m in re.finditer(r"\[\[(?:q|calc|nf|model):<", osrc_)]
        lo_, hi_ = osrc_.find('_RULES_TEXT = """'), osrc_.find('</number_rules>"""')
        fl_ = (osrc_.find("def registry_forms_lines("), osrc_.find("def registry_prompt_block("))
        off_ = off_only_spans(osrc_)       # IC-3 (Phase 8): IC-2's own lines for the switch off, found by their place in the code
        own_ = [(osrc_.find("_RULES_IC3_OWN = "), osrc_.find("assert _RULES_TEXT.count(_RULES_IC3_LIST)"))]   # IC-4 (Phase 8): D13's two
        # forms of the rules' own-number line (the switch off, IC-3's; on, IC-4's design counts) — the rules' own constants, not a stray site
        stray_ = [g for g in grammar_ if not (lo_ <= g <= hi_ or fl_[0] <= g <= fl_[1] or any(a_ <= g < b_ for a_, b_ in off_ + own_))]
        check("one function states the rules: number_rules (_RULES_TEXT) holds every form; no other site spells a form's grammar but IC-2's "
              "own lines for the switch off (BUILD IC-3)", all(f in rules_ for f in ("[[q:<where>|", "[[calc:<op>|", "[[model:<number>|", "[[nf:<the text>|"))
              and osrc_.count("def number_rules(") == 1 and one_ and not stray_ and not r_stray_,
              f"{len(stray_) + len(r_stray_)} stray" + ("" if one_ else "; the harness does not take its rules from the registry's one function"))
    except Exception as e:  # noqa: BLE001
        check("BUILD IC-3's rows", False, f"{type(e).__name__}: {str(e)[:120]}")
    # BUILD IC-4 (§5 Phase 8): this build's rows — the registry one module, ids from it, the calls and their formats, the schemas, no code
    # name, the request check (tools/request_check_ic4.json, beside IC-3's), the parser's salvage
    try:
        import ast as ast_
        rsrc_ = (root / "oracle-harness" / "naipc_registry.py").read_bytes()
        copies_ = {t: (root / t / "naipc_registry.py").read_bytes() for t in ("oracle-harness", "architect-harness", "muse-harness", "landing-harness")}
        mods_ = set()
        for n_ in ast_.walk(ast_.parse(rsrc_.decode("utf-8"))):
            if isinstance(n_, ast_.Import):
                mods_ |= {a.name.split(".")[0] for a in n_.names}
            elif isinstance(n_, ast_.ImportFrom) and n_.module and not n_.level:
                mods_.add(n_.module.split(".")[0])
        std_ = getattr(sys, "stdlib_module_names", set()) | {"__future__"}
        check("the registry module byte-identical in the four folders, and standard library only, the build's own modules beside it "
              "(BUILD IC-4; IC-8)", all(b == rsrc_ for b in copies_.values()) and bool(std_) and mods_ <= std_ | BUILD_OWN,
              f"differ: {[t for t, b in copies_.items() if b != rsrc_]}; not standard: {sorted(mods_ - std_ - BUILD_OWN)}")
        osrc_ = (root / "oracle-harness" / "oracle_harness.py").read_text(encoding="utf-8")
        rtxt_ = rsrc_.decode("utf-8")
        check("ids from the one module: the registry defines each identity function once; the Oracle takes its ids and its warnings' risks "
              "from them and spells no position id of its own (BUILD IC-4)",
              all(rtxt_.count(f"\ndef {f}(") == 1 for f in ("analysis_item_ids", "reply_item_ids", "carry_item_ids", "item_ids_doc"))
              and "REG.analysis_item_ids(" in osrc_ and "def deliverable_refs(" in osrc_
              and not re.search(r'(?<!\) or )f"(?:risk|ew|trigger|fm|qa|card|row|phase|kpi)\.\{(?:i|j|k|n_?)\s*\+\s*1\}"', osrc_))
        # (a position id only as the fallback after the stored id: "_nth(<ids>, i) or f"kpi.{i + 1}"", the audit's named site)

        def code_of(t_):        # a file's code with its docstrings set aside
            tree_ = ast_.parse(t_)
            for n_ in ast_.walk(tree_):
                if isinstance(n_, (ast_.Module, ast_.FunctionDef, ast_.ClassDef, ast_.AsyncFunctionDef)) and n_.body \
                        and isinstance(n_.body[0], ast_.Expr) and isinstance(getattr(n_.body[0], "value", None), ast_.Constant) \
                        and isinstance(n_.body[0].value.value, str):
                    n_.body[0].value.value = ""
            return ast_.unparse(tree_)
        hs_ = {t: code_of((root / t / f).read_text(encoding="utf-8")) for t, f in (("oracle-harness", "oracle_harness.py"),
                                                                                  ("architect-harness", "architect_harness.py"),
                                                                                  ("muse-harness", "muse_harness.py"))}
        qsrc_ = (root / "oracle-harness" / "naipc_requirements.py").read_text(encoding="utf-8")
        own_fmt_ = [t for t, c in hs_.items() if "'json_schema'" in c or '"json_schema"' in c]     # a string of its own (model_json_schema is a name)
        n_oc_ = sum(c.count("NR.output_config(") for c in hs_.values())
        check("the structured output only on the classed calls: built in naipc_requirements.output_config, asked by three calls (the Oracle's "
              "brief repair, the navigator, the Muse's repair); no harness writes a format of its own (BUILD IC-4)",
              qsrc_.count("\ndef output_config(") == 1 and not own_fmt_ and n_oc_ == 3, f"own: {own_fmt_}; calls: {n_oc_}")
        sch_ = {}
        for f_, nm_ in ((root / "oracle-harness" / "naipc_requirements.py", "BRIEF_REPAIR_SCHEMA"), (root / "architect-harness" / "architect_harness.py",
                                                                                                    "RATIONALE_SCHEMA"),
                        (root / "architect-harness" / "architect_harness.py", "RATIONALE_SCHEMA_IC5")):      # BUILD IC-5 (D16): the ids' schema
            for n_ in ast_.parse(f_.read_text(encoding="utf-8")).body:
                if isinstance(n_, ast_.Assign) and any(isinstance(t_, ast_.Name) and t_.id == nm_ for t_ in n_.targets):
                    sch_[nm_] = ast_.literal_eval(n_.value)
        bad_ = []

        def walk_(o, w):
            if isinstance(o, dict):
                for k_ in ("enum", "const", "default", "examples", "example"):
                    if k_ in o:
                        bad_.append(f"{w}: {k_}")
                if o.get("type") == "object":
                    props_ = o.get("properties") or {}
                    if o.get("additionalProperties") is not False or sorted(o.get("required") or []) != sorted(props_):
                        bad_.append(f"{w}: additionalProperties false and every property required")
                    bad_.extend(f"{w}: property {p_!r}" for p_ in props_ if str(p_).lower() in ("reasoning", "thinking", "trace"))
                for k_, v_ in o.items():
                    walk_(v_, f"{w}.{k_}")
            elif isinstance(o, list):
                for i_, v_ in enumerate(o):
                    walk_(v_, f"{w}[{i_}]")
        for nm_, sc_ in sch_.items():
            walk_(sc_, nm_)
        check("no schema with a data value or a forbidden property name: the three shipped schemas (BUILD IC-5 D16: the navigator's with "
              "the ids it names), at every level (BUILD IC-4)",
              len(sch_) == 3 and not bad_, "; ".join(bad_[:3]) or f"schemas read: {sorted(sch_)}")
        sys.path.insert(0, str(root / "oracle-harness"))
        import naipc_registry as reg_mod_          # standard library only (row above)
        import naipc_requirements as rq_mod_
        sample_ = "<p>oracle_harness.py FIN_CONSTANTS NR.x REG.y a violation</p>"
        found_ = {n_ for n_, _c in reg_mod_.code_names(sample_)}
        check("no code name on a page: the settings' source line is plain words; the page scan finds D7's names in a sample and none in "
              "plain words (the mock's pages: t_install_ic4, the acceptance script) (BUILD IC-4)",
              'CONST_SOURCE = "a setting of this model, not a measured figure"' in osrc_ and len(found_) >= 4
              and not reg_mod_.code_names("<p>a setting of this model, not a measured figure</p>"), f"found in the sample: {sorted(found_)}")
        rq4_ = HERE / "request_check_ic4.json"
        rq4d_ = json.loads(rq4_.read_text(encoding="utf-8")) if rq4_.is_file() else {}
        moved_ = sorted(f for f, h in (rq4d_.get("sha256") or {}).items()
                        if not (root / f).is_file() or hashlib.sha256((root / f).read_bytes()).hexdigest() != h)
        check("the request check: this build's checks on the request bodies passed on its own capture, for these bytes of the three "
              "harnesses and the requirements and registry modules (BUILD IC-4)",
              bool(rq4d_) and rq4d_.get("pass") == rq4d_.get("total") and len(rq4d_.get("sha256") or {}) == 5 and not moved_,
              f"{rq4d_.get('pass')}/{rq4d_.get('total')}; other bytes: {moved_}" if rq4d_ else "tools/request_check_ic4.json missing")
        cut_ = '{"op": "keep", "id": "f.bluf_text", "base": "0123abcd"}\n{"op": "keep", "id": "f.decision_text", "base": "4567ef01"}\n{"op": "rep'
        ln_, pr_, part_ = rq_mod_.parse_edit_lines(cut_, complete=False)
        check("the update parser's salvage on a cut reply: every complete line kept, the cut last line dropped (BUILD IC-4)",
              [x["edit"]["id"] for x in ln_] == ["f.bluf_text", "f.decision_text"] and not pr_ and part_ == len('{"op": "rep'),
              f"lines {[x['edit'].get('id') for x in ln_]}, problems {len(pr_)}, partial {part_}")
    except Exception as e:  # noqa: BLE001
        check("BUILD IC-4's rows", False, f"{type(e).__name__}: {str(e)[:120]}")
    width = max(len(r[0]) for r in results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}")
    failed = [r for r in results if not r[1]]
    total, passed = t1_total + len(results), t1_pass + len(results) - len(failed)
    print(f"\nT1″: {total} checks, {passed} PASS, {total - passed} FAIL")
    return 0 if p.returncode == 0 and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
