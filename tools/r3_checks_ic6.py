"""BUILD IC-6 (§7 R3) — the checks with no model call. First the switch reach table (D8): from the installed module
(oracle-harness/naipc_requirements.py, read, never imported for a call) each of this build's switches, whether it is on in this window's
environment (on unless set to 0, off, false or no; NAIPC_IC6_PROMPT off turns all four off) and the calls it reaches (NR.SWITCH_REACH, the
reach t_switches_ic6 measured); and each Part 2 call's form as it would run now (NR.call_form). Then (D10) the harness's render of a saved
diagram, with no model call: the Oracle's folder this machine holds (its code,
its vendored files and its checkpoints — not its output) copied to a new folder <root>/naipc-r3-ic6-<UTC stamp>/oracle-harness, then
`oracle_harness.py run --stage diagram_generation` and `--stage html_assembly` there (with the run's own announcement when its checkpoints
are a Block 2's) — the API's address a closed local port, no key passed: a diagram the CLI cannot read asks no repair that could leave the
machine. The diagram stage finds the Mermaid CLI as D10 does (NR.find_tool: on PATH with Windows's PATHEXT, never in the working or the
harness's folder) and runs it by its absolute path with no shell. It prints the diagram stage's lines; whether the rebuilt page holds the
drawing inline (an <svg>) or says the browser draws it, with the page's own words why; and "The diagram as a list" with its count. The
four harness folders are read, never written. (G11: R6's Block 2 was drawn in the browser — the harness ran a bare "mmdc", which Windows
does not resolve to mmdc.cmd.)
Usage: python -I -B r3_checks_ic6.py --root "%USERPROFILE%\\OneDrive\\Documents\""""
import html, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

A = sys.argv[1:]
ROOT = Path(A[A.index("--root") + 1]) if "--root" in A else Path.home() / "OneDrive" / "Documents"
OH = ROOT / "oracle-harness"
CK = OH / "checkpoints"
sys.path.insert(0, str(OH))
import naipc_requirements as NR  # noqa: E402
print("R3 switches (D8; on unless set to 0, off, false or no):")
for sw, reach in NR.SWITCH_REACH.items():
    on_ = NR.ic6(sw) if sw in NR.IC6_SWITCHES else NR._IC6_BASE      # NAIPC_IC6_PROMPT: off with it, or with any older switch off
    print(f"  {sw}: {'on' if on_ else 'off'} — reaches {', '.join(reach) if reach else 'no call (D14 is cut: its name is kept)'}")
print("R3 the Part 2 calls' form as they run now: " + "; ".join(f"{k} {NR.call_form(k)}" for k in sorted(NR.LINES_SWITCH)))
need = [CK / f"{s}.json" for s in ("data_loading", "master_analysis", "financial_model", "diagram_generation", "prioritization", "deliverables")]
missing = [str(p.relative_to(ROOT)) for p in need if not p.is_file()]
if missing:
    print(f"R3 diagram skipped: the Oracle's checkpoints are incomplete (missing: {', '.join(missing)}); nothing changed")
    sys.exit(0)
try:
    cb = json.loads((CK / "curveball.json").read_text(encoding="utf-8")) if (CK / "curveball.json").is_file() else {}
except ValueError:
    cb = {}
run = cb.get("run_id") or ""
WORK = ROOT / f"naipc-r3-ic6-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
assert not WORK.exists(), WORK
CO = WORK / "oracle-harness"
shutil.copytree(OH, CO, ignore=shutil.ignore_patterns("output", "chat-context-archive", "__pycache__", "coverage"))
mc = json.loads((CO / "checkpoints/diagram_generation.json").read_text(encoding="utf-8")).get("data") or {}
print(f"R3 diagram: the Oracle's checkpoints copied to {CO} (the four folders are not written); the saved diagram: "
      f"{len(str(mc.get('code') or mc.get('mermaid') or ''))} characters of Mermaid code; rebuilding it with this build (no model call)")
env = {k: v for k, v in os.environ.items() if not k.startswith("NAIPC_") and k != "ANTHROPIC_API_KEY"}
wi = (cb.get("challenge") or "wichita-2026-04-18")
env.update(NAIPC_REQUIREMENTS=str(ROOT / "landing-harness" / "requirements" / f"{wi}.json"), NAIPC_CHALLENGE=wi,
           ANTHROPIC_BASE_URL="http://127.0.0.1:9", PYTHONIOENCODING="utf-8", **({"NAIPC_RUN_ID": run} if run else {}))
extra = ["--curveball", cb["announcement"]] if run and cb.get("announcement") else []
outs = []
for st in ("diagram_generation", "html_assembly"):
    p = subprocess.run([sys.executable, "-B", "oracle_harness.py", "run", "--api-url", "http://127.0.0.1:9/", "--stage", st] + extra, cwd=CO,
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    outs.append((st, p))
(WORK / "console.txt").write_text("\n".join(f"== {s}\n{p.stdout}\n--- stderr ---\n{p.stderr}" for s, p in outs), encoding="utf-8")
for s, p in outs:
    for ln in p.stdout.splitlines():
        if re.search(r"[Mm]ermaid|mmdc|diagram|SVG|svg|Error|error", ln):
            print("  " + ln.strip()[:260])
h = (CO / "output/index.html").read_text(encoding="utf-8") if (CO / "output/index.html").is_file() else ""
inline = bool(re.search(r'class="diagram-wrap"[^>]*>\s*<div style="display:contents"[^>]*>\s*<svg', h))
why = re.search(r"<p[^>]*data-diagram-browser[^>]*>(.*?)</p>", h, re.S)
lst = re.search(r'<details class="diagram-list"[^>]*>(.*?)</details>', h, re.S)
print(f"R3 diagram exits: {', '.join(f'{s} {p.returncode}' for s, p in outs)}")
print("R3 diagram: " + ("drawn inline (an <svg> on the page) — the Mermaid CLI ran" if inline else
                        "the browser draws it — the page says: " + (" ".join(html.unescape(re.sub(r'<[^>]+>', ' ', why.group(1))).split())
                                                                   if why else "(no words: a fault)")))
print(f"R3 the diagram as a list: {'present' if lst else 'absent'} ({len(re.findall(r'<li', lst.group(1))) if lst else 0} item(s))")
print(f"R3 the copy: {WORK}")
sys.exit(max(p.returncode for _s, p in outs))
