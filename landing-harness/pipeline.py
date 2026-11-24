#!/usr/bin/env python3
"""
Pipeline — Run all four NAIPC harnesses in sequence.

Runs: Oracle → Architect → Muse → Landing
Automatically cascades checkpoints between tracks.
Pass --curveball once and it propagates to all four harnesses.
A Block 1 run first archives the Oracle's, the Architect's and the Muse's old checkpoints (--keep-checkpoints reuses them).

Usage (Block 1 — clean run):
    python pipeline.py \
        --api-url https://aipromptchamp.com/api/challenge/wichita/ \
        --endpoints endpoint1,endpoint2,...

Usage (Block 2 — curveball):
    python pipeline.py \
        --api-url https://aipromptchamp.com/api/challenge/wichita/ \
        --endpoints endpoint1,endpoint2,... \
        --curveball "BREAKING: New facility closure..."

Usage (P0.2 — with the challenge contract; the endpoints and the API URL come from it):
    python -B pipeline.py --requirements requirements/kc-2026-03-28.json
    python -B pipeline.py --requirements requirements/kc-2026-03-28.json --api-url http://127.0.0.1:8765/   (a dry run)
    python -B pipeline.py --requirements requirements/kc-2026-03-28.json --preflight-only                    (check only)
    python -B pipeline.py --no-requirements --api-url <url> --endpoints <a,b,...>                           (April behaviour)

P0.7 (the curveball as a diff): Block 1 snapshots every endpoint (runs/<challenge>/block1.json); Block 2 diffs every endpoint
against it into one change list (runs/<challenge>/changes_<run id>.json, exported as NAIPC_CHANGES) that every track reads.
    python -B pipeline.py --requirements <contract> --curveball-file <announcement.txt | sidecar.json>      (Block 2)
    --budget <whole dollars>   the Oracle's budget override, in either block
    --curveball-full           Block 2 regenerates the Oracle's and the Muse's output instead of updating Block 1's

Directory defaults assume this script lives in landing-harness/ and sibling
directories follow Shane's layout. Override with --oracle-harness-dir etc.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))
import naipc_requirements as NR   # P0.2: the challenge contract (one byte-identical copy in every harness folder)
import naipc_registry as REG      # BUILD B3 (P0.5, D2): the Oracle's registry and the cross-page check (one copy in every harness folder)
REG.admit_readers(NR)   # BUILD IC-5 (D1 e): the contract and the coverage map, wherever this process reads them
REG.set_admit_rewrite(NR.ic5_prompt())   # BUILD IC-5 (D1 f): the rewrite only with this build's prompt switch on

# ── Default directory layout ──
# All four harnesses live under OneDrive\Documents on Shane's machine
DEFAULT_DIRS = {
    "oracle": Path(r"C:\Users\shane\OneDrive\Documents\oracle-harness"),
    "architect": Path(r"C:\Users\shane\OneDrive\Documents\architect-harness"),
    "muse": Path(r"C:\Users\shane\OneDrive\Documents\Muse-harness"),
    "landing": SCRIPT_DIR,  # This script lives in landing-harness/
}

# Netlify URLs (stable across runs)
DEFAULT_URLS = {
    "oracle": "https://ctrl-alt-defeat.netlify.app",
    "architect": "https://ctrl-alt-defeat-arch.netlify.app",
    "muse": "https://ctrl-alt-defeat-muse.netlify.app",
    "landing": "https://ctrl-alt-defeat-hub.netlify.app",
}


def run_harness(name: str, cmd: list, cwd: Path, timeout: int = 600, track: str | None = None) -> bool:
    """Run a harness subprocess. Returns True on success. BUILD IC-6 (D3): `track` — the block's clock is printed as it starts."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  Dir: {cwd}")
    print(f"  Cmd: {' '.join(cmd[:4])}...")
    w_ = NR.block_words(track) if track else None
    if w_:
        print(f"  {w_}")

    # BUILD IC-8 (D8): this track's invocation — allocated before the child starts ("started" in its invocations record), passed to it,
    # and finished with its outcome and seconds by time.perf_counter (a clock step cannot move them; "completed in" prints them)
    cp_ = Path(cwd) / "checkpoints"
    # BUILD IC-8 (P4.3a; F24): a re-run of a track whose page is this run's built page keeps that page and its files first (the hub is
    # rebuilt from the others every time: never kept)
    if track and track != "landing" and NR.page_is_this_runs(Path(cwd) / "output", track, NR.run_id())[0]:
        ver_ = NR.keep_version(cwd, track)
        if ver_ is not None:
            print(f"  Re-run of a built track: its published page and files kept in {NR.VERSIONS_DIR}/{Path(ver_).name}/ — put back if this "
                  "re-run does not end built")
    inv = NR.allocate_invocation(cp_, track) if track else None
    env_ = dict(os.environ, NAIPC_INVOCATION_ID=inv["id"], NAIPC_INVOCATION_NUMBER=str(inv["number"])) if inv else None
    t0 = time.perf_counter()
    outcome, ok = "could not start", False
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            timeout=timeout,
            env=env_,
            # No shell=True — avoids Windows escaping issues with special chars
            # in curveball text (em-dashes, apostrophes, percent signs)
        )
        elapsed = time.perf_counter() - t0
        outcome = f"exit {result.returncode}"

        if result.returncode == 0:
            print(f"\n  ✅ {name} completed in {elapsed:.1f}s")
            ok = True
        else:
            print(f"\n  ❌ {name} FAILED (exit code {result.returncode}) after {elapsed:.1f}s")

    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        outcome = "timed out"
        print(f"\n  ❌ {name} TIMED OUT after {elapsed:.0f}s (limit: {timeout}s)")
    except FileNotFoundError as e:
        elapsed = None
        print(f"\n  ❌ {name} could not start: {e}")
    if inv:
        NR.finish_invocation(cp_, track, inv, outcome, elapsed)
        NR.keep_invocation_status(cp_, inv)
        if track != "landing" and not NR.page_is_this_runs(Path(cwd) / "output", track, NR.run_id())[0]:
            line_ = NR.restore_version(cwd, track, inv)      # BUILD IC-8 (P4.3a; F24): None without a version of this run
            if line_:
                print(f"\n  {line_}")
        RUN_INVOCATIONS.append({"track": track, "invocation_id": inv["id"], "invocation_number": inv["number"], "outcome": outcome})
    return ok


# ═══ BUILD IC-7 (D7, D10): each track's page this run's; the re-run of one track inside its block ═══
TRACKS4 = ("oracle", "architect", "muse", "landing")
RUN_INVOCATIONS = []        # BUILD IC-8 (P4.3a; F24): each track invocation this command started — the re-run's line in the run's record
RESUME_CALLS = {1: {"oracle": ("oracle/master", "oracle/deliverables"), "architect": ("architect/rationale",), "muse": ("muse/call1", "muse/call2")},
                2: {"oracle": ("oracle/update",), "architect": ("architect/rationale",), "muse": ("muse/update",)}}


def runs_from(resume, track):
    """BUILD IC-7 (D10): True when this track runs — every track in a run, the named one and those after it in a re-run."""
    return resume is None or TRACKS4.index(track) >= TRACKS4.index(resume["from"])


def resume_setup(args):
    """BUILD IC-7 (D10): --resume <run id> --from <track> — the run's record (runs/<challenge>/run_<run id>.json, beside the snapshot)
    read back, and the run's own arguments in place of the command line's (the curveball's text included; --block-seconds stays this
    command's). Exits 1, naming why, when --from is missing or the record is missing or unreadable."""
    if not args.from_track:
        print("  ❌ --resume needs --from oracle|architect|muse|landing; nothing was archived or run")
        sys.exit(1)
    root = DEFAULT_DIRS["landing"] / NR.RUNS_DIR
    hits = sorted(root.glob("*/" + NR.RUN_FILE.format(rid=args.resume))) if root.is_dir() else []
    if len(hits) != 1:
        print(f"  ❌ --resume {args.resume}: " + (f"no record of that run under {root}" if not hits else f"{len(hits)} records of that run")
              + " — nothing was archived or run")
        sys.exit(1)
    rec, why = REG.admit(NR.read_run_record(hits[0].parent, args.resume), "saved", "the run record")   # BUILD IC-7 Phase 4 (IC-5 D1)
    if rec is None:
        print(f"  ❌ --resume {args.resume}: {why} — nothing was archived or run")
        sys.exit(1)
    a = rec.get("args") or {}
    for k in ("api_url", "endpoints", "requirements", "no_requirements", "budget", "curveball_full", "coverage", "oracle_harness_dir",
              "architect_harness_dir", "muse_harness_dir", "oracle_url", "architect_url", "muse_url", "landing_url"):
        given = getattr(args, k, None)
        if k in ("api_url", "endpoints", "requirements", "oracle_harness_dir", "architect_harness_dir", "muse_harness_dir") and given:
            continue            # where the API answers now and where the folders are: this command's when it names them
        if k in a:
            setattr(args, k, a[k])
    args.curveball, args.curveball_file, args.keep_checkpoints, args.preflight_only = rec.get("curveball") or "", "", True, False
    print(f"  Re-run: run {rec['run_id']} (Block {rec.get('block')}), from its record {hits[0]}")
    return {"rec": rec, "runs_dir": hits[0].parent, "from": args.from_track}


def resume_plan(block, start):
    """BUILD IC-7 (D10): (seconds, parts) — the plans of the calls of the track named and the tracks after it in this block, their own
    time outside the calls, and the hub's reserve (NR.CALL_PROFILES, NR.BLOCK_TAIL_S, NR.BLOCK_RESERVES_S)."""
    need, parts = 0.0, []
    for t in TRACKS4[TRACKS4.index(start):]:
        for k in RESUME_CALLS.get(block, {}).get(t, ()):
            s_ = float(NR.CALL_PROFILES[k]["planning_s"])
            need += s_
            parts.append(f"{k} {s_:.0f} s")
        need += float(NR.BLOCK_TAIL_S.get(block, {}).get(t, 0))
    need += float(NR.BLOCK_RESERVES_S.get(block, {}).get("landing", 0))
    return need, parts


def ensure_track_page(dirs, track, run_id, why_not_run=None):
    """BUILD IC-7 (D7, I5): after a track's harness ran — or did not — a page that is not this run's built page becomes this run's
    not-built page (NR.not_built_page: its files moved to output_prev/<their run>/; the harness's handler writes it when it can, a killed
    harness cannot). A track that did not run gets a status saying why. The hub's not-built page lists each track's line. True when the
    page is this run's built page."""
    out = Path(dirs[track]) / "output"
    ok, _w = NR.page_is_this_runs(out, track, run_id)
    if ok:
        return True
    cp = Path(dirs[track]) / "checkpoints"
    if why_not_run:
        NR.write_track_status(cp, track, "failed", why=why_not_run, error_class="NotRun")
        why = why_not_run
    else:
        doc, w_ = REG.admit(NR.read_track_status(cp, run_id), "saved", NR.TRACK_STATUS)     # BUILD IC-7 Phase 4 (IC-5 D1): its why, a page's
        if doc is not None and doc.get("state") == "failed":
            why = doc.get("why") or "it stopped with an error"
        elif doc is not None and doc.get("state") == "started":
            why = "its harness stopped before it finished"
        elif doc is not None:
            why = "its page was not written"
        else:
            why = w_ or "no status file of this run"
    lines = None
    if track == "landing":
        lines = list(REG.admit(NR.track_lines({t: Path(dirs[t]) / "checkpoints" for t in ("oracle", "architect", "muse")}, run_id),
                               "saved", "the tracks' status files"))       # BUILD IC-7 Phase 4 (IC-5 D1): the hub's not-built page's list
        lines.append(f"Hub — not built this run: {why}")
    NR.not_built_page(out, track, run_id, why=why, lines=lines)
    return False


def deploy_line(dirs, run_id, stale=()):
    """BUILD IC-7 (D7): step 2 of the next steps — which pages are this run's built pages, and each not-built page named, to deploy too
    so its site does not show an earlier run."""
    built = [t for t in TRACKS4 if NR.page_is_this_runs(Path(dirs[t]) / "output", t, run_id)[0]]
    nb = [t for t in TRACKS4 if t not in built]
    tail = ""
    if nb:
        tail = (f" — built this run: {', '.join(built) or 'none'}; " + "; ".join(f"{t}: its not-built page — deploy it too, so its site does "
                                                                                 "not show an earlier run" for t in nb))
    if stale:
        tail += f" (another run's page left: {', '.join(s for s, _r in stale)} — do not deploy it)"
    print(f"    2. Deploy each output/ folder to its Netlify site{tail}")


def archive_checkpoints(harness_dir: Path, stamp: str):
    """P0.11b fresh-run hygiene: move everything in <harness>/checkpoints/ into <harness>/checkpoints_archive/<stamp>/,
    so a new challenge never reuses an old analysis (a plain Oracle run reuses every cached stage). Nothing is deleted.
    Returns (items moved, destination or None)."""
    src = harness_dir / "checkpoints"
    items = sorted(src.iterdir()) if src.is_dir() else []
    if not items:
        return 0, None
    dest = harness_dir / "checkpoints_archive" / stamp
    n = 1
    while dest.exists():
        dest = harness_dir / "checkpoints_archive" / f"{stamp}-{n}"
        n += 1
    dest.mkdir(parents=True)
    for p in items:
        shutil.move(str(p), str(dest / p.name))
    return len(items), dest


def resolve_contract(args):
    """P0.2: load and validate the challenge contract before anything else (exit 1 with every problem); derive the
    endpoints and the API URL from it (explicit flags win and are reported); export NAIPC_REQUIREMENTS and NAIPC_CHALLENGE
    to every harness. --no-requirements keeps the April behaviour, loudly. Returns the contract, or None."""
    if args.requirements and args.no_requirements:
        print("  ❌ --requirements and --no-requirements are mutually exclusive")
        sys.exit(1)
    if not args.requirements and not args.no_requirements:
        print("  ❌ a challenge contract is required: --requirements requirements\\<challenge-id>.json")
        print("     (or --no-requirements to run the April way, with --api-url and --endpoints)")
        sys.exit(1)
    if args.no_requirements:
        if args.preflight_only:
            print("  ❌ --preflight-only needs --requirements (it checks the contract's endpoints)")
            sys.exit(1)
        if not args.api_url or not args.endpoints:
            print("  ❌ --no-requirements needs --api-url and --endpoints")
            sys.exit(1)
        os.environ.pop("NAIPC_REQUIREMENTS", None)
        os.environ.pop("NAIPC_CHALLENGE", None)
        print("  ⚠ --no-requirements: running WITHOUT a challenge contract (the April behaviour) — no validation, no pre-flight")
        return None
    path = Path(args.requirements)
    try:
        doc = REG.admit(NR.load(path), "data", "contract")   # BUILD IC-5 (D1)
    except NR.RequirementsError as e:
        print(f"  ❌ requirements: {path} — {len(e.problems)} problem(s); nothing was archived or run")
        for p in e.problems:
            print(f"     {p}")
        sys.exit(1)
    names = NR.endpoint_names(doc)
    if args.endpoints:
        print(f"  ⚠ explicit --endpoints overrides the contract's {len(names)} endpoint(s)")
    else:
        args.endpoints = ",".join(names)
    if args.api_url:
        print(f"  ⚠ explicit --api-url {args.api_url} overrides the contract's {NR.api_base(doc)}")
    else:
        args.api_url = NR.api_base(doc)
    os.environ["NAIPC_REQUIREMENTS"] = str(path.resolve())
    os.environ["NAIPC_CHALLENGE"] = doc["challenge"]["id"]
    return doc


def preflight(api_url: str, names: list, expected: dict):
    """P0.2 + P0.6 (D8): GET every endpoint at the API base with the harnesses' own client (requests), in BOTH URL shapes, then
    choose the ONE shape every harness fetches through NR.endpoint_url: <ep> when every endpoint answers it, else <ep>/ when every
    endpoint answers that (NAIPC_URL_SLASH=1 exported to the harnesses and printed), else stop. A failed fetch inside a harness is
    only recorded (the Architect renders an empty tool), so a wrong or unreachable API, a server that answers neither shape for
    every endpoint, or a fixture server on another challenge's folder must be caught here. Every payload that carries an 'alerts'
    list is printed in the fixture server's own line format (found by the payload's shape, never by an endpoint's name), so the
    alert set in use is visible — a count never identifies a version. Returns (rows, ok)."""
    import requests
    base = api_url.rstrip("/")
    rows = []
    for ep in names:
        row = {"endpoint": ep, "expected": expected.get(ep), "records": None, "keys": "", "shapes": {}, "error": "", "alerts": None,
               "payloads": {}}
        for shape, slash in (("plain", False), ("slash", True)):
            url = NR.endpoint_url(base, ep, slash=slash)
            st, err, note = None, "", ""
            try:
                r = requests.get(url, timeout=15)
                st = r.status_code
                if r.history:
                    note = f"redirected to {r.url}"
                if st != 200:
                    err = f"HTTP {st}"
                else:
                    payload = r.json()
                    if row["records"] is None:
                        if isinstance(payload, dict):
                            row["keys"] = ", ".join(list(payload)[:6])
                            row["records"] = len(NR.records(payload))     # P0.7 (D5): the one envelope rule of every harness
                            if isinstance(payload.get("alerts"), list):
                                al = payload["alerts"]
                                row["alerts"] = (payload.get("status"), len(al), ", ".join(str(a.get("id")) for a in al if isinstance(a, dict)))
                        elif isinstance(payload, list):
                            row["keys"], row["records"] = "(list)", len(NR.records(payload))
            except ValueError:
                err = "not JSON"
            except Exception as e:  # noqa: BLE001 — every failure is named in the table
                err = type(e).__name__
            row["shapes"][shape] = {"status": st, "error": err, "note": note}
            if not err and st == 200:
                row["payloads"][shape] = payload                     # P0.7 (D1): kept for Block 1's snapshot / Block 2's diff
        rows.append(row)
    shape = ("plain" if all(not r["shapes"]["plain"]["error"] for r in rows) else
             "slash" if all(not r["shapes"]["slash"]["error"] for r in rows) else None)
    for r in rows:
        r["shape_used"] = shape
        r["payload"] = r["payloads"].get(shape) if shape else None
    for r in rows:
        p, s = r["shapes"]["plain"], r["shapes"]["slash"]
        if shape is None:
            if p["error"] and s["error"]:
                r["error"] = p["error"] if p["error"] == s["error"] else f"{p['error']} without the slash; {s['error']} with it"
            elif p["error"]:
                r["error"] = f"{p['error']} without the slash (200 with it)"
            elif s["error"]:
                r["error"] = f"{s['error']} with the slash (200 without it)"
    ok = shape is not None
    use = {"plain": "<ep>", "slash": "<ep>/"}.get(shape)
    print(f"\n  Pre-flight: {api_url} — {len(rows)} endpoint(s), both URL shapes probed" + (f"; every harness uses {use}" if use else ""))
    print(f"    {'endpoint':<22} {'<ep>':<9} {'<ep>/':<9} {'records':<8} {'expected':<9} keys")
    for r in rows:
        cell = lambda v: str(v["status"] if v["status"] is not None else v["error"])
        rec = "-" if r["records"] is None else str(r["records"])
        exp = "-" if r["expected"] is None else str(r["expected"])
        off = "   <- differs from expected" if r["records"] is not None and r["expected"] is not None and r["records"] != r["expected"] else ""
        print(f"    {r['endpoint']:<22} {cell(r['shapes']['plain']):<9} {cell(r['shapes']['slash']):<9} {rec:<8} {exp:<9} {r['keys'] or r['error']}{off}")
        notes = [f"{'<ep>' if sh == 'plain' else '<ep>/'} {v['note']}" for sh, v in r["shapes"].items() if v["note"]]
        if notes:
            print(f"    {'':<22} {'; '.join(notes)}")
    for r in sorted((r for r in rows if r["alerts"] is not None), key=lambda r: r["endpoint"]):   # the fixture server's line format
        print(f"  {r['endpoint']}: {r['alerts'][0]} — {r['alerts'][1]} alert(s): {r['alerts'][2]}")
    if shape == "plain":
        os.environ.pop("NAIPC_URL_SLASH", None)
        print(f"  ✅ pre-flight: all {len(rows)} endpoint(s) answered at <ep> — every harness uses <ep>")
    elif shape == "slash":
        os.environ["NAIPC_URL_SLASH"] = "1"
        print("  using <ep>/ — the server does not answer <ep> for every endpoint; NAIPC_URL_SLASH=1 exported to every harness")
        print(f"  ✅ pre-flight: all {len(rows)} endpoint(s) answered at <ep>/ — every harness uses <ep>/")
    else:
        bad = [r for r in rows if r["error"]]
        print(f"  ❌ pre-flight: {len(bad)} of {len(rows)} endpoint(s) failed — first: {bad[0]['endpoint']} ({bad[0]['error']})")
        print("  ⚠ no single URL shape answers for every endpoint (<ep> and <ep>/ were both tried) — every harness needs one shape")
    return rows, ok


def contract_expected(contract) -> dict:
    return {e["name"]: e.get("records_expected") for e in contract["api"]["endpoints"]}


def facts_line(contract) -> str:
    """P0.6 (D1): the banner's facts line — the label, then each fact as the brief states it or "none stated"."""
    F = NR.facts(contract)
    money = lambda v: f"${v:,}" if v is not None else "none stated"
    return (f"  Facts: {F['label']} · contract {money(F['contract'])}"
            + ("" if F["contract"] is not None else " (the Oracle proposes one, labelled as ours)")
            + f" · pilot {str(F['pilot_days']) + ' days' if F['pilot_days'] else 'none stated'}"
            + f" · marketing reserve {money(F['marketing'])} · unit {F['unit_plural']}"
            + ("" if F["unit_stated"] else " (none stated)"))


def _rel(p: Path, root: Path) -> str:
    try:
        return str(Path(p).relative_to(root))
    except ValueError:
        return str(p)


def take_snapshot(contract, rows, run_id, landing_dir: Path):
    """P0.7 (D1), Block 1: every endpoint's payload as the pre-flight read it, in the URL shape every harness uses, written to
    runs/<challenge>/block1.json after the run id is set and before the Oracle. A failed write is printed and never stops Block 1.
    Returns the payloads ({} when there is no contract)."""
    if contract is None:
        print("  Snapshot: not taken (--no-requirements: no challenge contract)")
        return {}
    cid = contract["challenge"]["id"]
    payloads = {r["endpoint"]: r["payload"] for r in rows}
    path = NR.snapshot_path(landing_dir, cid)
    why = NR.write_snapshot(path, cid, run_id, rows[0]["shape_used"] if rows else None, payloads)
    if why is None:
        print(f"  Snapshot: {_rel(path, landing_dir)} — {len(payloads)} endpoint(s)")
    else:
        print(f"  ⚠ Snapshot NOT written ({why}) — at Block 2 the change list falls back to the Oracle's Block 1 data")
    return payloads


def make_change_list(contract, rows, run_id, landing_dir: Path, oracle_dir: Path, curveball, announced):
    """P0.7 (D1, D2), Block 2: diff every endpoint of this run's pre-flight against Block 1's snapshot — runs/<challenge>/block1.json,
    else the Oracle's checkpoints/block1/data_loading.json when its meta names this challenge, else none (the announcement only,
    printed loudly; Block 2 never stops for it) — into runs/<challenge>/changes_<run id>.json, print the summary and export
    NAIPC_CHANGES. Returns (change list, this run's payloads)."""
    cid = contract["challenge"]["id"]
    new = {r["endpoint"]: r["payload"] for r in rows}
    names = [r["endpoint"] for r in rows]
    spath = NR.snapshot_path(landing_dir, cid)
    snap, why = REG.admit(NR.read_snapshot(spath, cid), "data", "block1 snapshot")   # BUILD IC-5 (D1)
    if snap is not None:   # review F6: a snapshot older than the Oracle's Block 1 base is an earlier Block 1's (this Block 1's write failed)
        _o, bmeta, _w = REG.admit(NR.block1_data(oracle_dir / "checkpoints", cid), "data", "block1 data")   # BUILD IC-5 (D1)
        brun, srun = str((bmeta or {}).get("run_id") or ""), str(snap.get("run_id") or "")
        if brun and srun and brun > srun:
            snap, why = None, f"it is Block 1 run {srun}, older than the Oracle's Block 1 base (run {brun})"
    if snap is not None:
        old, b1_run, source = snap["payloads"], snap.get("run_id"), {"path": str(spath), "source": _rel(spath, landing_dir)}
        print(f"  Snapshot: {_rel(spath, landing_dir)} (Block 1 run {b1_run}, taken {snap.get('taken_at')})")
    else:
        old, meta, why2 = REG.admit(NR.block1_data(oracle_dir / "checkpoints", cid), "data", "block1 data")   # BUILD IC-5 (D1)
        if old is not None:
            b1_run = meta.get("run_id")
            source = {"path": str(oracle_dir / "checkpoints" / NR.BLOCK1_DIR / "data_loading.json"), "source": "the Oracle's Block 1 data"}
            print(f"  snapshot: the Oracle's Block 1 data (run {b1_run}) — {_rel(spath, landing_dir)}: {why}")
        else:
            b1_run, source = None, {"path": None, "source": "none"}
            print(f"  ⚠⚠ no Block 1 snapshot: the change list holds the announcement only ({_rel(spath, landing_dir)}: {why}; {why2})")
    entries = NR.diff_snapshots(old, new, names) if old is not None else None
    doc = NR.make_changes(cid, run_id, b1_run, source, curveball, entries, NR.contract_curveball_flags(contract), announced)
    for line in doc["summary"]:
        print(line)
    cpath = NR.changes_path(landing_dir, cid, run_id)
    why = NR.write_changes(cpath, doc)
    if why is None:
        os.environ["NAIPC_CHANGES"] = str(cpath)
        print(f"  Change list: {_rel(cpath, landing_dir)} (exported as NAIPC_CHANGES)")
    else:
        os.environ.pop("NAIPC_CHANGES", None)
        print(f"  ⚠ Change list NOT written ({why}) — the harnesses get the announcement only")
    return doc, new


def compare_oracle_data(oracle_dir: Path, payloads: dict, what: str):
    """P0.7 (D1): after the Oracle step — does the Oracle's own data_loading equal what the pre-flight read? Endpoint by endpoint, by
    value (the API could change between the two fetches)."""
    if not payloads:
        return
    try:
        dl = REG.admit(json.loads((oracle_dir / "checkpoints" / "data_loading.json").read_bytes().decode("utf-8-sig")), "data", "data_loading.json")   # BUILD IC-5 (D1)
        raw = dl.get("data", {}).get("raw", {})
    except Exception as e:  # noqa: BLE001 — reported, never raised
        print(f"  ⚠ {what} vs the Oracle's data: not compared ({type(e).__name__})")
        return
    differ = [ep for ep in payloads if ep not in raw or not NR.same_payload(payloads[ep], raw[ep])]
    if not differ:
        print(f"  {what} = the Oracle's data ({len(payloads)}/{len(payloads)})")
    else:
        print(f"  ⚠ {what} ≠ the Oracle's data for {len(differ)} of {len(payloads)}: {', '.join(differ)} — the API changed between the "
              "pre-flight and the Oracle's fetch, or the Oracle could not read it")


def curveball_summary(dirs, run_id, changes):
    """P0.7 (D8): the end-of-run lines of a Block 2 — the change list's one line, the Oracle's update status and the figures that
    moved (from its checkpoints/curveball.json of this run), the Muse's update status (its muse_summary.json of this run)."""
    print("\n  Curveball (Block 2):")
    if changes is None:
        print("    change list: none")
    else:
        eps = changes.get("endpoints")
        if eps is None:
            print("    change list: the announcement only (no Block 1 snapshot)")
        else:
            ch = [ep for ep, e in eps.items() if e.get("status") == "changed"]
            print(f"    change list: {len(ch)} endpoint(s) changed ({', '.join(ch) or 'none'}), "
                  f"{sum(1 for e in eps.values() if e.get('status') == 'unchanged')} unchanged")
    cid = os.environ.get("NAIPC_CHALLENGE", "").strip() or None
    cb, why = REG.admit(NR.read_curveball_json(dirs["oracle"] / "checkpoints" / NR.CURVEBALL_FILE, cid, run_id), "saved", NR.CURVEBALL_FILE)   # BUILD IC-5 (D1)
    if cb is None:
        print(f"    Oracle: no curveball record of this run ({why})")
    else:
        print(f"    Oracle: update {cb.get('update')}")
        moved = cb.get("moved") or []
        print(f"    figures moved: {len(moved)}" + ("".join(f"\n      {m.get('label')}: {m.get('block1')} → {m.get('block2')} ({', '.join(m.get('because') or [])})"
                                                      for m in moved[:12])))
    try:
        ms = REG.admit(json.loads((dirs["muse"] / "checkpoints" / "muse_summary.json").read_bytes().decode("utf-8-sig")), "saved", "muse_summary.json")   # BUILD IC-5 (D1)
        st = ms.get("update") if ms.get("run_id") == run_id else None
        print(f"    Muse: update {st}" if st else "    Muse: no update record of this run")
    except Exception:  # noqa: BLE001
        print("    Muse: no update record of this run")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # P0.3 (D11): a cp1252 console or pipe never stops the run on a character
    parser = argparse.ArgumentParser(
        description="NAIPC Full Stack Pipeline — Oracle → Architect → Muse → Landing"
    )
    parser.add_argument("--api-url", default=None, help="Base API URL (with --requirements, default: the contract's base_url + prefix)")
    parser.add_argument("--endpoints", default=None, help="Comma-separated endpoint names (with --requirements, default: the contract's endpoints)")
    parser.add_argument("--requirements", default="", help="The challenge contract, e.g. requirements\\kc-2026-03-28.json (P0.2)")
    parser.add_argument("--no-requirements", action="store_true",
                        help="Run WITHOUT a contract (the April behaviour; needs --api-url and --endpoints)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Check every contract endpoint at the API base, print the table, and exit (nothing runs)")
    parser.add_argument("--curveball", default="", help="Curveball narrative text (passed to all tracks)")
    parser.add_argument("--curveball-file", default="",
                        help="P0.7 (D2): the announcement from a file — a <challenge-id>.curveball.json sidecar, or a text file "
                             "(UTF-8, UTF-16 or cp1252); starts Block 2 like --curveball")
    parser.add_argument("--budget", default=None,
                        help="P0.7 (D8): the Oracle's budget override in whole dollars, in either block (every figure uses it)")
    parser.add_argument("--curveball-full", action="store_true",
                        help="P0.7 (D3, D7): in Block 2, regenerate the Oracle's analysis and the Muse's campaign instead of updating Block 1's")
    parser.add_argument("--keep-checkpoints", action="store_true",
                        help="Reuse existing Oracle/Architect/Muse checkpoints (a Block 1 run archives them by default)")
    parser.add_argument("--coverage", choices=("warn", "fail"), default="warn",
                        help="P0.3 coverage gate: warn (report every gap, exit 0; the event-day setting) or fail (exit 1 when a gap remains, decided only after every harness ran)")

    # Harness directory overrides
    parser.add_argument("--block-seconds", type=int, default=None,
                        help="BUILD IC-6 (D3): the block's whole time from the pipeline's start, in seconds (default 3600 for Block 1, 900 for "
                             "Block 2: the 20-minute window less the uploads and checks); every call ends by it, less the later tracks' reserves")
    parser.add_argument("--resume", default="", metavar="RUN_ID",
                        help="BUILD IC-7 (D10): re-run one track of run RUN_ID inside its block — its own id, clock and inputs (the run's "
                             "record, runs/<challenge>/run_<RUN_ID>.json); nothing archived, no snapshot or change list; then the hub and the checks")
    parser.add_argument("--from", dest="from_track", choices=TRACKS4, default=None,
                        help="BUILD IC-7 (D10): with --resume, the track to run again, and every track after it")
    parser.add_argument("--oracle-harness-dir", default="", help="Path to oracle-harness/")
    parser.add_argument("--architect-harness-dir", default="", help="Path to architect-harness/")
    parser.add_argument("--muse-harness-dir", default="", help="Path to Muse-harness/")

    # Netlify URL overrides
    parser.add_argument("--oracle-url", default=DEFAULT_URLS["oracle"])
    parser.add_argument("--architect-url", default=DEFAULT_URLS["architect"])
    parser.add_argument("--muse-url", default=DEFAULT_URLS["muse"])
    parser.add_argument("--landing-url", default=DEFAULT_URLS["landing"])

    args = parser.parse_args()
    resume = resume_setup(args) if args.resume else None       # BUILD IC-7 (D10): the run's own arguments, from its record
    if args.from_track and not args.resume:
        print("  ❌ --from needs --resume <run id>; nothing was archived or run")
        sys.exit(1)

    # ── The challenge contract (P0.2): validated before anything else runs ──
    contract = resolve_contract(args)
    if args.preflight_only:
        rows, ok = preflight(args.api_url, [e for e in args.endpoints.split(",") if e.strip()], contract_expected(contract))
        print("  (--preflight-only: nothing was archived or run)")
        sys.exit(0 if ok else 1)

    # ── P0.7 (D2, D8): the announcement as an input and the budget override — both checked before anything runs ──
    curveball_obj, announced = None, None
    if args.curveball_file:
        if args.curveball:
            print("  ❌ --curveball and --curveball-file together — give one; nothing was archived or run")
            sys.exit(1)
        curveball_obj, note, problems = REG.admit(NR.curveball_input(args.curveball_file, contract), "data", "announcement file")   # BUILD IC-5 (D1)
        if problems:
            print(f"  ❌ curveball file: {args.curveball_file} — {len(problems)} problem(s); nothing was archived or run")
            for p in problems:
                print(f"     {p}")
            sys.exit(1)
        args.curveball = curveball_obj["text"]
        if Path(args.curveball_file).suffix.lower() == ".json":
            announced = list(curveball_obj.get("api_changed") or [])
        print(f"  Curveball file: {note}")
    elif args.curveball:
        curveball_obj = {"kind": "VERBATIM", "source": "--curveball", "announced_at": None, "text": args.curveball, "tests": [],
                         "api_changed": []}
    budget = None
    if args.budget is not None:
        b = str(args.budget).strip()
        if not (b.isascii() and b.isdigit()) or int(b) <= 0:
            print(f"  ❌ --budget {args.budget!r}: a positive whole number of dollars, digits only; nothing was archived or run")
            sys.exit(1)
        budget = int(b)
    if args.curveball_full and not (args.curveball or args.curveball_file):
        print("  ⚠ --curveball-full has no effect without a curveball (Block 1)")
    if resume is not None:          # BUILD IC-7 (D10): the run's own announcement, as its record keeps it
        curveball_obj = resume["rec"].get("curveball_obj") or curveball_obj
        announced = resume["rec"].get("announced")

    # Resolve directories
    dirs = {
        "oracle": Path(args.oracle_harness_dir) if args.oracle_harness_dir else DEFAULT_DIRS["oracle"],
        "architect": Path(args.architect_harness_dir) if args.architect_harness_dir else DEFAULT_DIRS["architect"],
        "muse": Path(args.muse_harness_dir) if args.muse_harness_dir else DEFAULT_DIRS["muse"],
        "landing": DEFAULT_DIRS["landing"],
    }

    # Verify all directories exist before starting
    for name, d in dirs.items():
        if not d.exists():
            print(f"  ❌ {name} directory not found: {d}")
            print(f"     Override with --{name}-harness-dir")
            sys.exit(1)

    py = sys.executable  # Same Python that's running this script
    pipeline_start = time.time()
    is_curveball = bool(args.curveball)
    # BUILD IC-6 (D3, I3): one clock for the block — its end exported to every harness (separate processes: epoch seconds); every call
    # ends by it, less the reserves of the tracks after it (NR.block_end_for)
    block_n = 2 if is_curveball else 1
    block_s = args.block_seconds if args.block_seconds is not None else (900 if is_curveball else 3600)
    if block_s <= 0:
        print(f"  ❌ --block-seconds {block_s}: a positive number of seconds; nothing was archived or run")
        sys.exit(1)
    os.environ["NAIPC_BLOCK_END"] = f"{pipeline_start + block_s:.3f}"
    os.environ["NAIPC_BLOCK"] = str(block_n)
    if resume is not None:          # BUILD IC-7 (D10): the run's own block and end; --block-seconds a new end from now, said on the hub
        block_n = int(resume["rec"]["block"])
        os.environ["NAIPC_BLOCK"] = str(block_n)
        if args.block_seconds is None:
            os.environ["NAIPC_BLOCK_END"] = f"{float(resume['rec']['block_end']):.3f}"
        else:
            os.environ["NAIPC_RERUN_AFTER_END"] = "1"
        block_s = int(float(os.environ["NAIPC_BLOCK_END"]) - pipeline_start)
        need, parts = resume_plan(block_n, resume["from"])
        left = float(os.environ["NAIPC_BLOCK_END"]) - time.time()
        print(f"  Re-run of run {resume['rec']['run_id']} from the {resume['from']}: its plan needs {need:.0f} s ({'; '.join(parts) or 'the hub only'}),"
              f" the block has {left:.0f} s left")
        if left < need:
            print(f"  ⛔ refused: the plan does not fit the time left — nothing was run (--block-seconds <n> sets a new end from now)")
            sys.exit(1)

    # Verify API key is set (Oracle and Muse both need it)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ❌ ANTHROPIC_API_KEY environment variable not set!")
        print("     Oracle and Muse need it for Claude API calls.")
        print("     Set it with: set ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    print(f"\n{'#'*60}")
    print(f"  NAIPC Full Stack Pipeline")
    print(f"  Mode: {'CURVEBALL (Block 2)' if is_curveball else 'CLEAN (Block 1)'}")
    print(f"  Block clock: {block_s:,} s — the block ends at "
          f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(pipeline_start + block_s))} ({pipeline_start + block_s:.3f}); every call "
          "ends by it, less the later tracks' reserves")      # BUILD IC-6 (D3)
    print(f"  API:  {args.api_url}")
    print(f"  Endpoints: {args.endpoints}")
    if is_curveball:
        print(f"  Curveball: {args.curveball[:80]}...")
    if budget is not None:
        print(f"  Budget override: ${budget:,} — every figure uses it")      # P0.7 (D8)
    if contract is None:
        print("  Contract: NONE (--no-requirements)")
    else:
        print(f"  Contract: {contract['challenge']['id']} ({os.environ['NAIPC_REQUIREMENTS']})")
        print(facts_line(contract))                                      # P0.6 (D1)
        print("  Tracks: " + " · ".join(NR.tracks_summary(contract)) + f" — {len(NR.item_ids(contract))} items")
        # P0.3 (D11): the coverage map beside the contract — checked, reported, never a reason to stop
        mpath = NR.map_path_for(os.environ["NAIPC_REQUIREMENTS"])
        if not mpath.is_file():
            print(f"  ⚠ Coverage map: none beside the contract ({mpath.name}) — every item will report UNMAPPED")
        else:
            cmap, mprobs = NR.load_map(mpath, contract)
            if mprobs:
                print(f"  ⚠ Coverage map INVALID ({mpath.name}, {len(mprobs)} problem(s)) — the run goes on without it:")
                for p in mprobs:
                    print(f"      {p}")
            else:
                print(f"  Coverage map: {mpath.name} — {NR.map_summary(cmap, contract)[0]}")
        print(f"  Coverage and integrity policy: {args.coverage}")
        if contract["curveball"] is None:
            print("  Curveball slot: empty")
        else:
            print(f"  Curveball slot: FILLED ({contract['curveball'].get('kind')})")
            if not is_curveball:
                print("  ⚠⚠ The contract's curveball slot is FILLED on a Block 1 run — the curveball is unknown until it drops"
                      " (Decision 9/27). Check the file before going on.")
    # P0.6 (D6): one competition label for the four pages — COMPETITION_LABEL when already set, else the contract's ("NAIPC" without)
    if os.environ.get("COMPETITION_LABEL", "").strip():
        print(f"  Label: {os.environ['COMPETITION_LABEL'].strip()} (COMPETITION_LABEL already set — kept)")
    else:
        os.environ["COMPETITION_LABEL"] = NR.competition_label(contract)
        print(f"  Label: {os.environ['COMPETITION_LABEL']} (exported as COMPETITION_LABEL)")
    print(f"{'#'*60}")

    # ── Pre-flight (P0.2): every contract endpoint must answer before anything is archived or run ──
    if contract is not None:
        rows, ok = preflight(args.api_url, [e for e in args.endpoints.split(",") if e.strip()], contract_expected(contract))
        if not ok:
            print("  ⛔ nothing was archived or run — start the fixture server on the right folder, or check the API URL")
            sys.exit(1)

    # ── Fresh run (P0.11b / P0.12): a Block 1 run never reuses an old analysis (P0.7: the Muse's Block 1 base too) ──
    if resume is not None:
        print("  Re-run: nothing archived (the run's own checkpoints stand; the failed attempt's files are kept)")
    elif not is_curveball and not args.keep_checkpoints:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        for name in ("oracle", "architect", "muse"):
            n, dest = archive_checkpoints(dirs[name], stamp)
            print(f"  Fresh run: {name} checkpoints " + (f"archived — {n} item(s) moved to {dest}" if n else "— none to archive"))
    elif not is_curveball:
        print("  ⚠ --keep-checkpoints: existing checkpoints are reused (cached Oracle stages will not regenerate)")

    # P0.3 (D4, D11): one run id for this run, in both blocks — every page and manifest carries it, so a stale one shows
    run_id = resume["rec"]["run_id"] if resume is not None else time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())   # IC-7 (D10)
    os.environ["NAIPC_RUN_ID"] = run_id
    print(f"  Run id: {run_id}")
    NR.paths_home(dirs["landing"] / "checkpoints", "pipeline")     # BUILD IC-8 (D14, I8): this run's path markers
    NR.mark_path(f"block.{block_n}")
    if resume is not None:
        NR.mark_path(f"resume.from_{resume['from']}")
    # BUILD IC-7 (D10): the run's record — its id, block, end, challenge, contract and arguments (the curveball's text; never the key)
    cid_ = contract["challenge"]["id"] if contract is not None else None
    runs_dir = NR.snapshot_path(dirs["landing"], cid_).parent if cid_ else dirs["landing"] / NR.RUNS_DIR / "no-contract"
    if resume is None:
        rp_ = NR.write_run_record(runs_dir, {"run_id": run_id, "block": block_n, "block_end": float(os.environ["NAIPC_BLOCK_END"]),
                                             "block_seconds": block_s, "challenge": cid_, "contract": os.environ.get("NAIPC_REQUIREMENTS"),
                                             "args": {k: v for k, v in vars(args).items() if k not in ("resume", "from_track")},
                                             "curveball": args.curveball, "curveball_obj": curveball_obj, "announced": announced,
                                             "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pipeline_start))})
        if rp_ is not None:
            print(f"  Run record: {_rel(rp_, dirs['landing'])} — a track re-run inside this block: python pipeline.py --resume {run_id} --from <track>")
    else:
        NR.add_run_line(resume["runs_dir"], run_id, {"from": resume["from"], "block_seconds": args.block_seconds})
        # BUILD IC-8 (D8): the failed invocation's status is its own track_status_<run id>_inv<n>.json, kept when it ended (run_harness)

    # ── P0.7 (D1): Block 1 snapshots every endpoint; Block 2 diffs every endpoint against it into one change list ──
    changes, payloads = None, {}
    if resume is not None:          # BUILD IC-7 (D10): no snapshot, no change list — the run's own are read
        cpath_ = NR.changes_path(dirs["landing"], cid_, run_id) if (is_curveball and cid_) else None
        if cpath_ is not None and cpath_.is_file():
            os.environ["NAIPC_CHANGES"] = str(cpath_)
            print(f"  Change list: the run's own, {_rel(cpath_, dirs['landing'])}")
        else:
            os.environ.pop("NAIPC_CHANGES", None)
    elif not is_curveball:
        os.environ.pop("NAIPC_CHANGES", None)
        payloads = take_snapshot(contract, rows if contract is not None else [], run_id, dirs["landing"])
    elif contract is None:
        os.environ.pop("NAIPC_CHANGES", None)
        print("  Change list: not made (--no-requirements) — the harnesses get the announcement only")
    else:
        changes, payloads = make_change_list(contract, rows, run_id, dirs["landing"], dirs["oracle"], curveball_obj, announced)

    # ── Step 1: Oracle ──
    # Set ARCHITECT_URL env var so Oracle template renders ZIP→Architect deep links
    os.environ["ARCHITECT_URL"] = args.architect_url
    oracle_cmd = [
        py, "oracle_harness.py", "run",
        "--api-url", args.api_url,
        "--endpoints", args.endpoints,
    ]
    if is_curveball:
        oracle_cmd += ["--force", "--curveball", args.curveball]
        if args.curveball_full:
            oracle_cmd += ["--curveball-full"]                       # P0.7 (D3): regenerate instead of updating Block 1's analysis
    if budget is not None:
        oracle_cmd += ["--budget", str(budget)]                      # P0.7 (D8): both blocks

    if runs_from(resume, "oracle"):
        if not run_harness("Oracle (Business Case)", oracle_cmd, dirs["oracle"], timeout=int(NR.oracle_window(block_n)[2]),
                           track="oracle"):          # BUILD IC-8 (D17): Block 1's window from the profiles; Block 2's 900
            print("\n  ⛔ Oracle failed — aborting pipeline (downstream tracks need Oracle checkpoints)")
            # BUILD IC-7 (D7): every track's page is this run's — the Oracle's not-built page, the others' "not run", the hub's listing all
            ensure_track_page(dirs, "oracle", run_id)
            for t_ in ("architect", "muse", "landing"):
                ensure_track_page(dirs, t_, run_id, why_not_run="not run: the business case failed")
            deploy_line(dirs, run_id)
            stale_check(dirs, run_id)
            sys.exit(1)
        ensure_track_page(dirs, "oracle", run_id)
        compare_oracle_data(dirs["oracle"], payloads, "Snapshot" if not is_curveball else "This run's pre-flight data")   # P0.7 (D1)

    # ── Step 2: Architect ──
    # Set cross-track URL env vars so Architect template renders integration links
    os.environ["ORACLE_URL"] = args.oracle_url
    os.environ["MUSE_URL"] = args.muse_url
    # Set DEPLOY_URL so Architect generates a QR code pointing to its own Netlify site
    os.environ["DEPLOY_URL"] = args.architect_url
    architect_cmd = [
        py, "architect_harness.py", "run",
        "--api-url", args.api_url,
        "--endpoints", args.endpoints,
    ]
    if is_curveball:
        architect_cmd += ["--curveball", args.curveball]
    # P0.6 (D3): the program identity the Oracle wrote this run (with --keep-checkpoints, the cached one of this challenge)
    ident_path = dirs["oracle"] / "checkpoints" / NR.IDENTITY_FILE
    ident, why = REG.admit(NR.read_identity(ident_path, os.environ.get("NAIPC_CHALLENGE", "").strip() or None), "saved", NR.IDENTITY_FILE)   # BUILD IC-5 (D1)
    if ident is None:
        print(f"  Program identity not handed to the Architect — {why}")
    elif ident.get("run_id") == run_id:
        architect_cmd += ["--brand-config", str(ident_path)]
        print(f"  Program: {ident['program_name']} ({ident.get('source')})")
    elif args.keep_checkpoints and contract is not None and ident.get("challenge") == contract["challenge"]["id"]:
        architect_cmd += ["--brand-config", str(ident_path)]
        print(f"  Program: {ident['program_name']} ({ident.get('source')}, cached — written by run {ident.get('run_id')})")
    else:
        print(f"  Program identity not handed to the Architect — written by run {ident.get('run_id')}, this run is {run_id}")
    # P0.7 (D7): the Oracle's curveball record of THIS run (the derived figures the Architect draws beside an aggregate)
    if is_curveball:
        cb_path = dirs["oracle"] / "checkpoints" / NR.CURVEBALL_FILE
        cb, why = REG.admit(NR.read_curveball_json(cb_path, os.environ.get("NAIPC_CHALLENGE", "").strip() or None), "saved", NR.CURVEBALL_FILE)   # BUILD IC-5 (D1)
        if cb is None:
            print(f"  Curveball record not handed to the Architect — {why}")
        elif cb.get("run_id") == run_id:
            architect_cmd += ["--curveball-json", str(cb_path)]
            print(f"  Curveball record: {NR.CURVEBALL_FILE} handed to the Architect (update {cb.get('update')})")
        else:
            print(f"  Curveball record not handed to the Architect — written by run {cb.get('run_id')}, this run is {run_id}")

    architect_cmd += ["--oracle-dir", str(dirs["oracle"] / "checkpoints")]     # BUILD IC-4 (D3): the Oracle's registry, the one registry
    if runs_from(resume, "architect"):
        if not run_harness("Architect (Resource Navigator)", architect_cmd, dirs["architect"], timeout=300, track="architect"):   # B2 (D6)
            print("\n  ⚠ Architect failed — continuing with Oracle + Muse + Landing")
        ensure_track_page(dirs, "architect", run_id)          # BUILD IC-7 (D7)

    # ── Step 3: Muse ──
    oracle_cp_dir = dirs["oracle"] / "checkpoints"
    muse_cmd = [
        py, "muse_harness.py", "run",
        "--api-url", args.api_url,
        "--endpoints", args.endpoints,
        "--oracle-dir", str(oracle_cp_dir),
        "--architect-url", args.architect_url,
        "--oracle-url", args.oracle_url,
    ]
    if is_curveball:
        muse_cmd += ["--curveball", args.curveball]
        if args.curveball_full:
            muse_cmd += ["--curveball-full"]                         # P0.7 (D7)

    if runs_from(resume, "muse"):
        if not run_harness("Muse (Outreach Campaign)", muse_cmd, dirs["muse"], timeout=900, track="muse"):
            print("\n  ⚠ Muse failed — continuing with Landing")
        ensure_track_page(dirs, "muse", run_id)               # BUILD IC-7 (D7)

    # ── Step 4: Landing ──
    architect_cp_dir = dirs["architect"] / "checkpoints"
    landing_cmd = [
        py, "landing_harness.py", "run",
        "--oracle-dir", str(oracle_cp_dir),
        "--architect-dir", str(architect_cp_dir),
        "--muse-dir", str(dirs["muse"] / "checkpoints"),                # P0.6 (D6): the Muse's run-stamped summary
        "--endpoints", args.endpoints,
        "--oracle-url", args.oracle_url,
        "--architect-url", args.architect_url,
        "--muse-url", args.muse_url,
        "--landing-url", args.landing_url,
    ]

    if not run_harness("Landing (Full Stack Hub)", landing_cmd, dirs["landing"], timeout=60, track="landing"):
        print("\n  ⚠ Landing failed")
    ensure_track_page(dirs, "landing", run_id)                # BUILD IC-7 (D7)
    if resume is not None:          # BUILD IC-8 (P4.3a; F24, RS-3): the re-run's line closed with the invocations it gave each track
        sel_ = NR.selected_invocations({t: dirs[t] / "checkpoints" for t in TRACKS4}, run_id)
        NR.close_run_line(resume["runs_dir"], run_id, [dict(x, selected=sel_.get(x["track"])) for x in RUN_INVOCATIONS])
        for x in RUN_INVOCATIONS:
            n_ = sel_.get(x["track"])
            print(f"  Re-run: {x['track']} invocation {x['invocation_number']} ({x['outcome']}) — published: "
                  + (f"invocation {n_}" if n_ is not None else "none (its not-built page)"))

    # ── Coverage (P0.3, D7): this run's three track manifests; the exit decision waits until every harness has run ──
    coverage_failed = False
    if contract is None:
        print("\n  Coverage: not checked (--no-requirements)")
    else:
        lines, coverage_failed = NR.coverage_summary(contract, {t: dirs[t] / "coverage" for t in NR.TRACKS}, run_id, policy=args.coverage)
        print()
        for line in lines:
            print(f"  {line}")

    # ── Integrity (BUILD B3, P0.5, D2): the Oracle's checker and its violations; then every page's registry figures against the Oracle's
    # registry — a difference prints and follows the policy (fail: exit 1 after every page was built; warn: exit 0) ──
    integrity_failed = integrity_check(dirs, run_id, contract)

    # ── Summary ──
    total = time.time() - pipeline_start
    minutes = int(total // 60)
    seconds = int(total % 60)
    if is_curveball:
        curveball_summary(dirs, run_id, changes)                     # P0.7 (D8)
    print(f"\n{'#'*60}")
    print(f"  Pipeline complete: {minutes}m {seconds}s total")
    print(f"{'#'*60}")
    print(f"\n  Output locations:")
    print(f"    Oracle:    {dirs['oracle'] / 'output' / 'index.html'}")
    print(f"    Architect: {dirs['architect'] / 'output' / 'index.html'}")
    print(f"    Muse:      {dirs['muse'] / 'output' / 'index.html'}")
    print(f"    Landing:   {dirs['landing'] / 'output' / 'index.html'}")
    print(f"\n  This build (each track's status file):")         # BUILD IC-6 (D7, I6)
    for ln_ in REG.admit(NR.track_lines({t: dirs[t] / "checkpoints" for t in ("oracle", "architect", "muse")}, run_id),
                         "saved", "the tracks' status files"):     # read back from files: the one way in (IC-5 D1)
        print(f"    {ln_}")
    for ln_ in NR.content_totals({t: dirs[t] / "checkpoints" for t in ("oracle", "architect", "muse", "landing")}, run_id):   # IC-8 (D3, d16d)
        print(f"    {ln_}")
    stale = stale_check(dirs, run_id)
    print(f"\n  Next steps:")
    print(f"    1. Verify each output in the browser")
    deploy_line(dirs, run_id, stale)                          # BUILD IC-7 (D7): the not-built pages named
    print(f"    3. Hard-refresh live URLs (Ctrl+F5) to bust cache")
    if coverage_failed:
        print("\n  ❌ exit 1: --coverage fail and a coverage gap remains (every page above was still built)")
    if integrity_failed and args.coverage == "fail":   # BUILD B3 (D2): said whether or not coverage failed too
        print("\n  ❌ exit 1: --coverage fail and the integrity check flagged a figure (every page above was still built)")
    leak_failed = False
    if NR.accept_mode():               # BUILD IC-5 (D2 h): the tests' runs and the acceptance script; a live run goes on
        n_, lwhy_ = leak_state(dirs, run_id)
        leak_failed = n_ is None or n_ > 0
        if leak_failed:
            print(f"\n  ❌ exit 1: NAIPC_ACCEPT=1 and " + (f"{n_} code name(s) or tag(s) of the model's on the pages (the hub's checkpoints/"
                                                         f"{LEAKS_FILE})" if n_ is not None else f"the leak check did not run ({lwhy_})")
                  + " — every page above was still built")
    stale_failed = bool(stale) and NR.accept_mode()     # BUILD IC-6 (D7): a page of another run fails a test's or the acceptance's run
    if stale_failed:
        print(f"\n  ❌ exit 1: NAIPC_ACCEPT=1 and {len(stale)} page(s) of another run — every page above was still built")
    if coverage_failed or (integrity_failed and args.coverage == "fail") or leak_failed or stale_failed:
        sys.exit(1)


LEAKS_FILE = "leaks.json"     # BUILD IC-5 (D2 h): in the hub's checkpoints — integrity_check writes it, leak_state reads it


def stale_check(dirs, run_id):
    """BUILD IC-6 (D7): each page's naipc-run against this run's id — a page of another run is named: "⚠ <track>: output/index.html is
    from run <x>, not this run — do not deploy it". Returns [(track, its run)]."""
    pages = {t: dirs[t] / "output" / "index.html" for t in ("oracle", "architect", "muse", "landing")}
    stale = NR.stale_pages(pages, run_id)
    for t, r in stale:
        print(f"  ⚠ {t}: output/index.html is from run {r}, not this run — do not deploy it")
    return stale


def leak_state(dirs, run_id):
    """BUILD IC-5 (D2 h): (the number of leak findings of this run, None) from the hub's checkpoints/leaks.json, or (None, why) when the
    check did not run for this run (no file, another run's, unreadable)."""
    d, why = REG.admit(NR.read_json(Path(dirs["landing"]) / "checkpoints" / LEAKS_FILE), "saved", LEAKS_FILE)
    if not isinstance(d, dict):
        return None, why or "no leaks report"
    if d.get("run_id") != run_id:
        return None, f"the leaks report is another run's ({d.get('run_id')})"
    return (d["found"], None) if isinstance(d.get("found"), int) else (None, "the leaks report holds no count")


def integrity_check(dirs, run_id, contract=None):
    """BUILD B3 (P0.5, D2): print the Oracle's integrity line and violations, then check every page's registry spans against the Oracle's
    registry of this run; the Muse's, Architect's and hub's own counts print here (their page lines come with P4). True when anything
    was flagged. Never raises.
    BUILD IC-4 (D3): the registry is read with this run's data (the Oracle's data checkpoint: data ids by grammar resolve on every page,
    the one registry) and the contract's authority and aliases; then the check across pages (REG.facts_across) — its lines here, the
    whole of it in runs/<challenge>/facts_<run id>.json; an inconsistent duplicate or a confirmed relation that fails is flagged."""
    try:
        cid = os.environ.get("NAIPC_CHALLENGE", "").strip() or None
        reg, why = REG.read(dirs["oracle"] / "checkpoints" / REG.REGISTRY_FILE, cid, run_id)
        if reg is None:
            print(f"\n  Integrity: not checked — the Oracle's registry of this run is not readable ({why})")
            return False
        dl_, _w = REG.admit(NR.read_json(dirs["oracle"] / "checkpoints" / "data_loading.json"), "data", "data_loading.json")       # BUILD IC-5 (D1); BUILD IC-4 (D3): the one registry —
        raw_ = ((dl_ or {}).get("data") or {}).get("raw") if isinstance(dl_, dict) else None   # its entries and this run's data by grammar
        if isinstance(raw_, dict):
            reg.set_data(raw_, NR.records)
        reg.set_facts((contract or {}).get("authority"), (contract or {}).get("aliases"))
        for t in ("architect", "muse", "landing"):          # BUILD IC-4 (D3): what each page registered beyond the Oracle's registry
            n_, w_ = REG.merge_added(reg, Path(dirs[t]) / "checkpoints" / REG.ADDED_FILE)
            if n_:
                print(f"  Integrity: {n_} figure(s) the {t} page registered (its checkpoints/{REG.ADDED_FILE})")
        integ = reg.integrity or {}
        viol = integ.get("violations") or []
        print(f"\n  Integrity (Oracle): {integ.get('line') or 'no line'} — {len(viol)} flagged")
        for v in viol[:12]:
            print(f"      ⚠ {v.get('rule')}: {v.get('where')} — {str(v.get('context') or '')[:160]}")
        differ, pages, leaks = 0, {}, {}
        for t in ("oracle", "architect", "muse", "landing"):
            page = Path(dirs[t]) / "output" / "index.html"
            ok_, why_ = NR.page_is_this_runs(page.parent, t, run_id)      # BUILD IC-7 (D7, I5): this run's built pages only
            if not ok_:
                print(f"  Integrity, the check across pages and the leak scan: skipped: {t} ({why_})")
                continue
            if not page.is_file():
                continue
            html = page.read_text(encoding="utf-8", errors="replace")
            pages[t] = html
            sp = REG.spans(html)
            bad = [(pid, shown) for pid, shown in sp if not REG.shows(reg.get(pid), shown)]     # IC-3 (ledger step 36): the one test
            differ += len(bad)
            line = f"  Integrity ({t}): {len(sp)} registry figure(s) shown, {len(bad)} differ"
            if t != "oracle":                 # BUILD IC-4 (D7): the page's own line, as the page prints it
                c = REG.check_page(html)
                line += f"; {c['n']} of {c['m']} numbers traced; its line: {REG.page_line_of(html) or 'none on the page'}"
            print(line)
            aw_ = REG.admit_line(Path(dirs[t]) / "checkpoints", run_id, t)   # BUILD IC-5 (D1): the way in, by kind; IC-8 (D9): its scope
            if aw_:
                print(f"      {aw_}")
            lk_ = REG.page_leaks(html)     # BUILD IC-5 (D2 h): the code names (IC-4 run turn R6: §7's row 6 on the spot) and the model's tags
            leaks[t] = lk_                 # a judge would read — written to the hub's checkpoints/leaks.json below
            cn_ = lk_["code_names"]
            if cn_:
                print(f"      ⚠ {t}: {len(cn_)} code name(s) or prompt tag(s) a judge would read — {', '.join(sorted({n for n, _c in cn_}))[:160]}")
            if lk_["tags"]:
                print(f"      ⚠ {t}: {len(lk_['tags'])} tag(s) of the model's a judge would read — "
                      f"{', '.join(sorted({n for n, _c in lk_['tags']}))[:160]}")
            for pid, shown in bad[:6]:
                e = reg.get(pid)
                print(f"      ⚠ {t}: {pid} shows {shown!r} — the registry holds {e['shown'] if e else 'no such figure'}")
        lpath = Path(dirs["landing"]) / "checkpoints" / LEAKS_FILE       # BUILD IC-5 (D2 h): the leak findings of this run, every page's
        n_leaks = sum(len(v["code_names"]) + len(v["tags"]) for v in leaks.values())
        lwhy = NR.write_json(lpath, {"run_id": run_id, "challenge": cid, "accept": NR.accept_mode(), "found": n_leaks, "pages": leaks})
        print(f"  Leaks (code names, the model's tags) on the pages: {n_leaks} — {_rel(lpath, dirs['landing'])}" if lwhy is None
              else f"  ⚠ the leaks report was not written ({lwhy})")
        rep_, _w = REG.admit(NR.read_json(dirs["oracle"] / "checkpoints" / "data_report.json"), "saved", "data_report.json")   # BUILD IC-5 (D1)
        fx = REG.facts_across(pages, reg, rep_ if isinstance(rep_, dict) else None)       # BUILD IC-4 (D3): the check across pages
        print()
        for ln in fx["console"]:
            print(f"  {ln}")
        if cid:
            fpath = NR.snapshot_path(dirs["landing"], cid).parent / f"facts_{run_id}.json"
            fwhy = NR.write_json(fpath, REG.out_tree(dict(fx, challenge=cid, run_id=run_id), "plain"))    # BUILD IC-5 (D2 c): a writer
            print(f"  Facts across pages: {_rel(fpath, dirs['landing'])}" if fwhy is None else f"  ⚠ the facts report was not written ({fwhy})")
        return bool(viol) or differ > 0 or bool(fx.get("failed"))
    except Exception as e:  # noqa: BLE001 — a check that cannot run is said, never a failed pipeline
        print(f"\n  Integrity: not checked ({type(e).__name__}: {str(e)[:120]})")
        return False


if __name__ == "__main__":
    main()
