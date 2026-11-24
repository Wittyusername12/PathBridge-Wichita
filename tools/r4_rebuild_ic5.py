"""BUILD IC-5 (§7 R4) — R6's Block 1 (the IC-4 run turn's pair 1, Block 1: run 20261114T045417Z, Wichita) rebuilt from the checkpoints
this machine holds, with the installed tree, IN A COPY: <root>/oracle-harness (its code, its vendored files and its checkpoints — not its
output) is copied to a new folder <root>/naipc-r4-ic5-<UTC stamp>/oracle-harness, and `oracle_harness.py run --stage html_assembly` runs
there, under this Python, with no model call. The four harness folders are read, never written: R5's live Block 2 starts from R6's
Block 1 as IC-4 wrote it — a rebuild in place would rewrite checkpoints/block1/ (that base) with this tree's registry, since `run` ends
in _block_end (read in oracle_harness.py, 11/15), and the page R6 recorded.
First it checks that the checkpoints are that run's — checkpoints/deliverables_reply_20261114T045417Z.txt and the stage files the page is
built from; if any is missing it says so and stops with nothing changed ("skipped").
The child process gets NAIPC_REQUIREMENTS (Wichita's contract in <root>/landing-harness/requirements, read only), NAIPC_CHALLENGE,
NAIPC_RUN_ID (the run's own id) and ANTHROPIC_BASE_URL set to a closed local port with no API key passed on — so no call could leave the
machine even by mistake; no other NAIPC_ variable is passed. It prints: the run's exit; the integrity line; the prompts' tags on the page
(R6's recorded page held CHALLENGE_DATA ×28 and [ESTIMATE ×8; this build writes their words); the registry's line naming the ids in the
older build's saved words (IC-4's checkpoint: "… named by their labels", expected); the code names and "violation" on the page (none);
and the folder to judge — §7's acceptance reads it with `--rebuilt oracle-harness` (row 6 on the page this rebuild wrote).
Usage: python -I -B r4_rebuild_ic5.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import html, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
RUN, WI = "20261114T045417Z", "wichita-2026-04-18"
OH = ROOT / "oracle-harness"
CK = OH / "checkpoints"
need = [CK / f"deliverables_reply_{RUN}.txt"] + [CK / f"{s}.json" for s in ("data_loading", "master_analysis", "financial_model",
                                                                            "diagram_generation", "prioritization", "deliverables")]
missing = [str(p.relative_to(ROOT)) for p in need if not p.is_file()]
if missing:
    print(f"R4 skipped: the checkpoints are not run {RUN}'s (missing: {', '.join(missing)}); nothing changed")
    sys.exit(0)
WORK = ROOT / f"naipc-r4-ic5-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
assert not WORK.exists(), WORK
CO = WORK / "oracle-harness"
shutil.copytree(OH, CO, ignore=shutil.ignore_patterns("output", "chat-context-archive", "__pycache__"))
print(f"R4: the checkpoints are run {RUN}'s (Block 1) — copied with the harness to {CO} (the four folders are not written); rebuilding "
      "the page there (--stage html_assembly; the API's address a closed port, no key passed)")
env = {k: v for k, v in os.environ.items() if not k.startswith("NAIPC_") and k != "ANTHROPIC_API_KEY"}
env.update(NAIPC_REQUIREMENTS=str(ROOT / "landing-harness" / "requirements" / f"{WI}.json"), NAIPC_CHALLENGE=WI, NAIPC_RUN_ID=RUN,
           ANTHROPIC_BASE_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8")
p = subprocess.run([sys.executable, "-B", "oracle_harness.py", "run", "--api-url", "http://127.0.0.1:9/", "--stage", "html_assembly"],
                   cwd=CO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
(WORK / "console.txt").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
named = [ln.strip() for ln in p.stdout.splitlines() if "named by their labels" in ln]
for ln in p.stdout.splitlines():
    if re.search(r"integrity|completed|Error|error", ln):
        print("  " + ln.strip()[:300])
page = CO / "output" / "index.html"
h = page.read_text(encoding="utf-8") if page.is_file() else ""
line = re.findall(r"Data integrity: [^<]*", h)
sys.path.insert(0, str(CO))
try:
    import naipc_registry as REG  # noqa: E402
    names = sorted({n for n, _c in REG.code_names(h)})
except Exception as ex:  # noqa: BLE001
    names = [f"(the scan could not run: {type(ex).__name__})"]
print(f"R4 exit {p.returncode}")
print(f"R4 line: {html.unescape(line[0]).strip() if line else '(no line on the page)'}")
print(f"R4 the prompts' tags on the page: CHALLENGE_DATA ×{h.count('CHALLENGE_DATA')}, [ESTIMATE ×{h.count('[ESTIMATE')} (R6's recorded page: ×28, ×8)")
print(f"R4 the registry's ids named in the older build's words: {named[0][:200] if named else 'none'}")
print(f"R4 code names and 'violation' on the page: {names or 'none'}")
print(f"R4 the folder to judge (§7, accept_ic5.py --b1 <it> --rebuilt oracle-harness): {WORK}")
sys.exit(p.returncode)
