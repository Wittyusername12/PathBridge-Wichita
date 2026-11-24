"""BUILD IC-6 (§7 R4) — R6's Block 2 (the IC-5 run turn's pair 1, Block 2: run 20261116T015538Z, Wichita) rebuilt from the checkpoints this
machine holds, with the installed tree, IN A COPY: the business case (the Oracle's page) and the hub. Copied to a new folder
<root>/naipc-r4-ic6-<UTC stamp>/: oracle-harness (its code, its vendored files and its checkpoints — not its output), landing-harness (its
code and requirements — not its output, its checkpoints or its runs), and the Architect's and the Muse's checkpoints. Then
`oracle_harness.py run --stage html_assembly --curveball <the run's own announcement>` runs in the copy, and `landing_harness.py run` over
the copies' checkpoints — no model call: the Oracle's API address is a closed local port and no API key is passed on; the hub makes none.
The four harness folders are read, never written (R6's Block 2 live starts again from p1b1, the IC-4 run turn's Block 1: a rebuild in place
would rewrite the checkpoints and the pages R6's Block 2 recorded).
First it checks that the checkpoints are that run's — the Oracle's curveball.json (its run id), update_reply_20261116T015538Z.txt and the
stage files the page is built from; if any is missing it says so and stops with nothing changed ("skipped").
Each child process gets NAIPC_REQUIREMENTS (Wichita's contract in <root>/landing-harness/requirements, read only), NAIPC_CHALLENGE and
NAIPC_RUN_ID (the run's own id: the hub takes the curveball record and the status files as this run's); no other NAIPC_ variable.
It prints: each exit; the Oracle's integrity line; the notes (D9: the "[note N]" references, the notes, a marker left in a sentence); the
diagram (D10: an inline <svg>, or the sentence that the browser draws it, and "The diagram as a list" with its steps); the hub's "This
build" lines (D7: R6's files were written before IC-6, so each track's line is built from that run's own files, and says so); and the
folder to judge: staged read only, then accept_ic6.py --b1 <p1b1> --b2 <the staged copy> --rebuilt oracle-harness,landing-harness.
Usage: python -I -B r4_rebuild_ic6.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import html, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
RUN, WI = "20261116T015538Z", "wichita-2026-04-18"


def folder(name):
    """A harness folder of the root, whatever its letter case (his Muse folder is "Muse-harness")."""
    return next((p for p in sorted(ROOT.iterdir()) if p.is_dir() and p.name.lower() == name), ROOT / name)


OH, LH, AH, MH = folder("oracle-harness"), folder("landing-harness"), folder("architect-harness"), folder("muse-harness")
CK = OH / "checkpoints"
need = [CK / "curveball.json", CK / f"update_reply_{RUN}.txt"] + [CK / f"{s}.json" for s in (
    "data_loading", "master_analysis", "financial_model", "diagram_generation", "prioritization", "deliverables")] + [
    AH / "checkpoints", MH / "checkpoints", LH / "landing_harness.py"]
missing = [str(p.relative_to(ROOT)) for p in need if not p.exists()]
try:
    cb_run = json.loads((CK / "curveball.json").read_text(encoding="utf-8")).get("run_id") if (CK / "curveball.json").is_file() else None
except ValueError:
    cb_run = None
if missing or cb_run != RUN:
    print(f"R4 skipped: the checkpoints are not run {RUN}'s Block 2 (" + (f"missing: {', '.join(missing)}" if missing else
                                                                     f"curveball.json names run {cb_run}") + "); nothing changed")
    sys.exit(0)
WORK = ROOT / f"naipc-r4-ic6-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
assert not WORK.exists(), WORK
CO, CL = WORK / "oracle-harness", WORK / "landing-harness"
shutil.copytree(OH, CO, ignore=shutil.ignore_patterns("output", "chat-context-archive", "__pycache__", "coverage"))
shutil.copytree(LH, CL, ignore=shutil.ignore_patterns("output", "checkpoints", "checkpoints_archive", "runs", "__pycache__"))
for src_, name in ((AH, "architect-harness"), (MH, "muse-harness")):
    shutil.copytree(src_ / "checkpoints", WORK / name / "checkpoints")
print(f"R4: the checkpoints are run {RUN}'s (Block 2) — copied with the Oracle's and the hub's code to {WORK} (the four folders are not "
      "written); rebuilding the business case (--stage html_assembly) and the hub there (the API's address a closed port, no key passed)")
ann = json.loads((CO / "checkpoints/curveball.json").read_text(encoding="utf-8")).get("announcement") or ""
env = {k: v for k, v in os.environ.items() if not k.startswith("NAIPC_") and k != "ANTHROPIC_API_KEY"}
env.update(NAIPC_REQUIREMENTS=str(LH / "requirements" / f"{WI}.json"), NAIPC_CHALLENGE=WI, NAIPC_RUN_ID=RUN,
           ANTHROPIC_BASE_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8")
po = subprocess.run([sys.executable, "-B", "oracle_harness.py", "run", "--api-url", "http://127.0.0.1:9/", "--stage", "html_assembly",
                     "--curveball", ann], cwd=CO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
pl = subprocess.run([sys.executable, "-B", "landing_harness.py", "run", "--oracle-dir", str(CO / "checkpoints"),
                     "--architect-dir", str(WORK / "architect-harness/checkpoints"), "--muse-dir", str(WORK / "muse-harness/checkpoints")],
                    cwd=CL, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
(WORK / "console.txt").write_text("\n".join(f"== {n}\n{p.stdout}\n--- stderr ---\n{p.stderr}" for n, p in (("oracle", po), ("hub", pl))),
                                  encoding="utf-8")
for ln in po.stdout.splitlines():
    if re.search(r"^\s*(integrity|notes|diagram|Mermaid)\b|completed|Error|error", ln):
        print("  " + ln.strip()[:300])
page = CO / "output" / "index.html"
h = page.read_text(encoding="utf-8") if page.is_file() else ""
hub = (CL / "output" / "index.html").read_text(encoding="utf-8") if (CL / "output" / "index.html").is_file() else ""
line = re.findall(r"Data integrity: [^<]*", h)
refs = re.findall(r'href="#sd-note-(\d+)"', h)
notes = re.findall(r'<li id="sd-note-(\d+)"', h)
lst = re.search(r'<details class="diagram-list"[^>]*>(.*?)</details>', h, re.S)
steps = len(re.findall(r"<li\b", lst.group(1))) if lst else 0
sys.path.insert(0, str(CO))
try:
    import naipc_registry as REG  # noqa: E402
    names = sorted({n for n, _c in REG.code_names(h)})
except Exception as ex:  # noqa: BLE001
    names = [f"(the scan could not run: {type(ex).__name__})"]
tb = re.search(r'id="this-build"[^>]*>(.*?)</section>', hub, re.S)
tb_lines = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", x)).split()) for x in re.findall(r"<li\b[^>]*>(.*?)</li>", tb.group(1), re.S)] if tb else []
print(f"R4 exits: the business case {po.returncode}, the hub {pl.returncode}")
print(f"R4 line: {html.unescape(line[0]).strip() if line else '(no line on the page)'}")
print(f"R4 notes (D9): {len(refs)} reference(s) to {len(set(notes))} note(s); every reference resolves: {set(refs) <= set(notes)}; "
      f"markers left in a sentence: {h.count('<sup class=' + chr(34) + 'sd' + chr(34))}")
print("R4 diagram (D10): " + ("an inline <svg>" if re.search(r'class="diagram-wrap"[^>]*>\s*<div style="display:contents"[^>]*>\s*<svg', h) else
                              "the browser draws it — the page says so" if "data-diagram-browser" in h else "(neither)")
      + f"; the diagram as a list: {'present' if lst else 'absent'} ({steps} item(s))")
print(f"R4 the hub's \"This build\" (D7): {len(tb_lines)} line(s)" + "".join(f"\n  - {x[:200]}" for x in tb_lines))
print(f"R4 code names and 'violation' on the business case: {names or 'none'}")
print(f"R4 the folder to judge (staged read only; accept_ic6.py --b2 <it> --rebuilt oracle-harness,landing-harness): {WORK}")
sys.exit(po.returncode or pl.returncode)
