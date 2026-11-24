"""BUILD IC-3 (§7 R4) — R4's page of 11/11 (IC-2's live Block 1, run 20261111T022742Z) rebuilt from the checkpoints this machine holds,
with the installed tree: `oracle_harness.py run --stage html_assembly` in <root>/oracle-harness, under this Python, with no model call.
The page shows the new count on the reply of 11/11 before any money is spent; its integrity line is printed, to set beside the record's.
First it checks that the checkpoints are that run's (checkpoints/deliverables_reply_20261111T022742Z.txt and the four stage files the
page is built from); if they are not, it says so and stops with nothing changed ("skipped").
The child process gets: NAIPC_REQUIREMENTS (Wichita's contract in <root>/landing-harness/requirements), NAIPC_CHALLENGE, NAIPC_RUN_ID
(the run's own id), and ANTHROPIC_BASE_URL set to a closed local port with no API key passed on — so no call could leave the machine
even by mistake; no other NAIPC_ variable is passed. Nothing of this reaches the window it runs in (the child's environment only).
It writes what the stage writes: oracle-harness/output/index.html and the checkpoints the stage rebuilds (registry.json,
integrity_numerals.json, html_assembly.json).
Usage: python -I -B r4_rebuild_ic3.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import html, json, os, re, subprocess, sys
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
RUN, WI = "20261111T022742Z", "wichita-2026-04-18"
OH = ROOT / "oracle-harness"
CK = OH / "checkpoints"
need = [CK / f"deliverables_reply_{RUN}.txt"] + [CK / f"{s}.json" for s in ("data_loading", "master_analysis", "financial_model", "deliverables")]
missing = [str(p.relative_to(OH)) for p in need if not p.is_file()]
if missing:
    print(f"R4 skipped: the checkpoints are not run {RUN}'s (missing: {', '.join(missing)}); nothing changed")
    sys.exit(0)
env = {k: v for k, v in os.environ.items() if not k.startswith("NAIPC_") and k != "ANTHROPIC_API_KEY"}
env.update(NAIPC_REQUIREMENTS=str(ROOT / "landing-harness" / "requirements" / f"{WI}.json"), NAIPC_CHALLENGE=WI, NAIPC_RUN_ID=RUN,
           ANTHROPIC_BASE_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8")
print(f"R4: the checkpoints are run {RUN}'s — rebuilding the page (--stage html_assembly; the API's address a closed port, no key passed)")
p = subprocess.run([sys.executable, "-B", "oracle_harness.py", "run", "--api-url", "http://127.0.0.1:9/", "--stage", "html_assembly"], cwd=OH,
                   env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
for ln in p.stdout.splitlines():
    if re.search(r"integrity|completed|Error|error", ln):
        print("  " + ln.strip()[:300])
page = OH / "output" / "index.html"
line = re.findall(r"Data integrity: [^<]*", page.read_text(encoding="utf-8")) if page.is_file() else []
reg = json.loads((CK / "registry.json").read_text(encoding="utf-8")) if (CK / "registry.json").is_file() else {}
rline = (reg.get("integrity") or {}).get("line")
print(f"R4 exit {p.returncode}")
print(f"R4 line: {html.unescape(line[0]).strip() if line else '(no line on the page)'}")
print(f"R4 the page's line is registry.json's: {bool(line) and html.unescape(line[0]).strip() == rline}")
sys.exit(p.returncode)
