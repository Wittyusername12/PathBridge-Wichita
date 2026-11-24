"""BUILD IC-4 (§7 R4) — R8's page (IC-3's live Block 2 of 11/12, run 20261112T045318Z, its Block 1 base run 20261112T041820Z) rebuilt from the
checkpoints this machine holds, with the installed tree: `oracle_harness.py run --stage html_assembly --curveball <the announcement>` in
<root>/oracle-harness, under this Python, with no model call. It prints what the record names: the integrity line; F1's figure linked (the
waitlist's "8-12" a span of the registry, a mention of the organizers' text); the veto (BRIDGES closed by the announcement on the page); the
change panel's rows (the figures that moved); the code names and "violation" on the page (none).
First it checks that the checkpoints are that run's — checkpoints/update_reply_20261112T045318Z.txt, the four stage files the page is built
from, Block 1's base (checkpoints/block1/master_analysis.json and meta.json) and the change list (landing-harness/runs/<challenge>/
changes_20261112T045318Z.json); if any is missing it says so and stops with nothing changed ("skipped").
The child process gets NAIPC_REQUIREMENTS (Wichita's contract in <root>/landing-harness/requirements), NAIPC_CHALLENGE, NAIPC_RUN_ID (the
run's own id), NAIPC_CHANGES (the change list) and ANTHROPIC_BASE_URL set to a closed local port with no API key passed on — so no call
could leave the machine even by mistake; no other NAIPC_ variable is passed. It writes what the stage writes: oracle-harness/output/
index.html and the checkpoints the stage rebuilds (registry.json, integrity_numerals.json, html_assembly.json).
Usage: python -I -B r4_rebuild_ic4.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import html, json, os, re, subprocess, sys
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
RUN, B1RUN, WI = "20261112T045318Z", "20261112T041820Z", "wichita-2026-04-18"
OH = ROOT / "oracle-harness"
CK = OH / "checkpoints"
CHG = ROOT / "landing-harness" / "runs" / WI / f"changes_{RUN}.json"
need = ([CK / f"update_reply_{RUN}.txt", CK / "curveball.json"] + [CK / f"{s}.json" for s in ("data_loading", "master_analysis", "financial_model",
                                                                                              "deliverables")]
        + [CK / "block1" / "master_analysis.json", CK / "block1" / "meta.json", CHG])
missing = [str(p.relative_to(ROOT)) for p in need if not p.is_file()]
if missing:
    print(f"R4 skipped: the checkpoints are not run {RUN}'s (missing: {', '.join(missing)}); nothing changed")
    sys.exit(0)
cb = json.loads(CK.joinpath("curveball.json").read_text(encoding="utf-8"))
ann = (cb.get("data") or cb).get("announcement") or ""
env = {k: v for k, v in os.environ.items() if not k.startswith("NAIPC_") and k != "ANTHROPIC_API_KEY"}
env.update(NAIPC_REQUIREMENTS=str(ROOT / "landing-harness" / "requirements" / f"{WI}.json"), NAIPC_CHALLENGE=WI, NAIPC_RUN_ID=RUN,
           NAIPC_CHANGES=str(CHG), ANTHROPIC_BASE_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8")
print(f"R4: the checkpoints are run {RUN}'s (Block 2; its base {B1RUN}) — rebuilding the page (--stage html_assembly; the API's address a "
      "closed port, no key passed)")
p = subprocess.run([sys.executable, "-B", "oracle_harness.py", "run", "--api-url", "http://127.0.0.1:9/", "--stage", "html_assembly",
                    "--curveball", ann], cwd=OH, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
for ln in p.stdout.splitlines():
    if re.search(r"integrity|completed|Error|error", ln):
        print("  " + ln.strip()[:300])
page = OH / "output" / "index.html"
h = page.read_text(encoding="utf-8") if page.is_file() else ""
text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", h))))
line = re.findall(r"Data integrity: [^<]*", h)
f1 = re.search(r'<span class="prov" data-prov="q\.[0-9a-f]{5,7}"[^>]*title="[^"]*shelter-009 operatorNotes[^"]*"[^>]*>8-12</span>', h)
veto = re.search(r"BRIDGES[^.<]{0,120}(?:closed|no vacancy)", text)
moved = re.findall(r'<tr data-reg="', h)          # the Block 2 table of what moved: one row per figure (the template's data-reg rows)
sys.path.insert(0, str(OH))
try:
    import naipc_registry as REG  # noqa: E402
    names = sorted({n for n, _c in REG.code_names(h)})
except Exception as ex:  # noqa: BLE001
    names = [f"(the scan could not run: {type(ex).__name__})"]
print(f"R4 exit {p.returncode}")
print(f"R4 line: {html.unescape(line[0]).strip() if line else '(no line on the page)'}")
print(f"R4 F1's figure linked: {bool(f1)}")
print(f"R4 the veto on the page: {bool(veto)}")
print(f"R4 the change panel's rows: {len(moved)}")
print(f"R4 code names and 'violation' on the page: {names or 'none'}")
sys.exit(p.returncode)
