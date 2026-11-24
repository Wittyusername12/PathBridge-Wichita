#!/usr/bin/env python3
"""P0.11a T3 — one live generation check per track, outside the real output/ and checkpoints/ folders.

  python -B p011a_t3.py oracle --harness "C:\\Users\\shane\\OneDrive\\Documents\\oracle-harness"
  python -B p011a_t3.py muse --harness "C:\\Users\\shane\\OneDrive\\Documents\\muse-harness" --oracle-checkpoints "C:\\Users\\shane\\OneDrive\\Documents\\oracle-harness\\checkpoints"

oracle: re-runs only the master analysis from the harness's saved data_loading checkpoint, in a work folder.
muse:   runs both Muse calls against the recorded Wichita fixtures (served locally by fixture_server.py), from a copy.
Work folders: %USERPROFILE%\\naipc-staging\\p011a\\t3-<track>-<UTC stamp> (t3.log holds the full output).
Prints each model call's measurement line (model, stop_reason, output tokens, duration), the exit code and the time.
Exit 0 = the harness succeeded and wrote its output; 1 = it failed (the named reason is in the output); 2 = setup missing.
"""
import argparse, datetime, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def workdir(track):
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    w = home / "naipc-staging" / "p011a" / f"t3-{track}-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
    w.mkdir(parents=True)
    return w


def run(cmd, cwd):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    t = time.monotonic()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    took = time.monotonic() - t
    out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout + p.stderr)          # Muse's console forces colour codes
    (Path(cwd) / "t3.log").write_text(out, encoding="utf-8")
    return p.returncode, took, [l.strip() for l in out.splitlines() if " · stop=" in l], out


def report(track, w, code, took, lines, out, ok):
    print(f"T3 {track}: exit {code}, {took:.1f} s, work folder {w}")
    for l in lines:
        print(f"  {l}")
    if not ok:
        tail = [l for l in out.splitlines() if l.strip()][-12:]
        print("  last lines of the output:", *[f"    {l[:200]}" for l in tail], sep="\n")
    print(f"T3 {track}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def oracle(a):
    h = Path(a.harness); cp = h / "checkpoints" / "data_loading.json"
    if not (h / "oracle_harness.py").is_file() or not cp.is_file():
        print(f"missing {h / 'oracle_harness.py'} or {cp}"); return 2
    w = workdir("oracle"); (w / "checkpoints").mkdir()
    shutil.copy2(cp, w / "checkpoints" / "data_loading.json")
    code, took, lines, out = run([sys.executable, "-B", str(h / "oracle_harness.py"), "run",
                                  "--api-url", "http://127.0.0.1:9/", "--stage", "master_analysis"], w)
    return report("oracle", w, code, took, lines, out, code == 0 and (w / "checkpoints" / "master_analysis.json").is_file())


def muse(a):
    sys.path.insert(0, str(HERE))
    from fixture_server import start_in_thread
    h = Path(a.harness)
    if not (h / "muse_harness.py").is_file() or not Path(a.oracle_checkpoints).is_dir():
        print(f"missing {h / 'muse_harness.py'} or {a.oracle_checkpoints}"); return 2
    w = workdir("muse")
    for n in ("muse_harness.py", "muse_template.html.j2"):
        shutil.copy2(h / n, w / n)
    srv, url, eps = start_in_thread(HERE / "fixtures")
    try:
        code, took, lines, out = run([sys.executable, "-B", str(w / "muse_harness.py"), "run", "--api-url", url,
                                      "--endpoints", ",".join(eps), "--oracle-dir", a.oracle_checkpoints], w)
    finally:
        srv.shutdown(); srv.server_close()
    return report("muse", w, code, took, lines, out, code == 0 and (w / "output" / "index.html").is_file())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="track", required=True)
    o = sub.add_parser("oracle"); o.add_argument("--harness", required=True)
    m = sub.add_parser("muse"); m.add_argument("--harness", required=True); m.add_argument("--oracle-checkpoints", required=True)
    a = ap.parse_args()
    sys.exit(oracle(a) if a.track == "oracle" else muse(a))
