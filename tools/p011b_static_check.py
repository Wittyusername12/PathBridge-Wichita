#!/usr/bin/env python3
"""P0.11b T1′ — static check of the 13 harness files: P0.11a's 27 checks plus the chat and fresh-run checks.

Usage:  python -B p011b_static_check.py --root "C:\\Users\\shane\\OneDrive\\Documents"
        (the folder that holds oracle-harness, architect-harness, muse-harness and landing-harness)
Exit 0 = every check passed; 1 = a check failed (each failure names file and line); 2 = a file is missing.
Reads only; writes nothing.
"""
import argparse, ast, re, sys
from pathlib import Path

FILES = {
    "oracle-harness": ["oracle_harness.py", "template.html.j2", "test_local.py"],
    "architect-harness": ["architect_harness.py", "architect_template.html.j2", "test_local.py"],
    "muse-harness": ["muse_harness.py", "muse_template.html.j2", "test_local.py"],
    "landing-harness": ["landing_harness.py", "landing_template.html.j2", "pipeline.py", "test_local.py"],
}
PLANNED_NEGATIVE = {("landing-harness/test_local.py",
                     '("Model label in boot line", "claude opus 5.5" in html and "opus 4.6" not in html),')}
TEMPLATES = ["oracle-harness/template.html.j2", "architect-harness/architect_template.html.j2",
             "muse-harness/muse_template.html.j2", "landing-harness/landing_template.html.j2"]
HARNESSES = ["oracle-harness/oracle_harness.py", "architect-harness/architect_harness.py",
             "muse-harness/muse_harness.py", "landing-harness/landing_harness.py"]
# (label, regex, flags, scope) — expected ZERO matches in scope
ZERO = [
    ("4-series Opus ID", r"claude-opus-4", re.I, "all"),
    ("4-series Sonnet ID", r"claude-sonnet-4", re.I, "all"),
    ("hub label 'opus 4.6'", r"opus 4\.6", re.I, "all"),
    ("Instructor", r"instructor", re.I, "all"),
    ("call_structured", r"call_structured", 0, "all"),
    ("get_instructor_client", r"get_instructor_client", 0, "all"),
    ("chatbot_api_key", r"chatbot_api_key", 0, "all"),
    ("CHATBOT_KEY", r"CHATBOT_KEY", 0, "all"),
    ("data-chat-key", r"data-chat-key", 0, "all"),
    ("x-api-key header", r"x-api-key", re.I, "all"),
    ("browser-access header", r"anthropic-dangerous-direct-browser-access", re.I, "all"),
    ("api.anthropic.com in templates/Architect/Muse", r"api\.anthropic\.com", re.I,
     TEMPLATES + ["architect-harness/architect_harness.py", "muse-harness/muse_harness.py"]),
    ("requests.post in Muse", r"requests\.post", 0, ["muse-harness/muse_harness.py"]),
    ("key-shaped string", r"sk-ant-[A-Za-z0-9_-]{20,}", 0, "all"),
    ("initChatbot in templates", r"initChatbot", 0, TEMPLATES),
    ("CHATBOT_CONTEXT in templates", r"CHATBOT_CONTEXT", 0, TEMPLATES),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    root = Path(ap.parse_args().root)
    text, missing = {}, []
    for d, names in FILES.items():
        for n in names:
            p = root / d / n
            if p.is_file():
                text[f"{d}/{n}"] = p.read_text(encoding="utf-8")
            else:
                missing.append(str(p))
    if missing:
        print("MISSING:", *missing, sep="\n  ")
        return 2
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    for label, rx, flags, scope in ZERO:
        files = list(text) if scope == "all" else scope
        hits = [f"{f}:{i}" for f in files for i, line in enumerate(text[f].split("\n"), 1)
                if re.search(rx, line, flags) and not (f, line.strip()) in PLANNED_NEGATIVE]
        check(f"zero: {label}", not hits, ", ".join(hits[:8]))
    # the one planned occurrence: the hub test asserts the old label is ABSENT from the page (P0.11 A8)
    neg = [f"{f}:{i}" for f, t in text.items() for i, line in enumerate(t.split("\n"), 1) if (f, line.strip()) in PLANNED_NEGATIVE]
    check("planned negative assertion present exactly once (hub test: 'opus 4.6' not in html)", len(neg) == 1, ", ".join(neg))

    # claude-opus-5-5 exactly at the planned constants (BUILD B2, D6: the Architect's RATIONALE_MODEL is the third)
    hits = [(f, i, line.strip()) for f in text for i, line in enumerate(text[f].split("\n"), 1) if "claude-opus-5-5" in line]
    planned = sorted(h[0] for h in hits if h[2].startswith(('OPUS = "claude-opus-5-5"', 'MODEL = "claude-opus-5-5"', 'RATIONALE_MODEL = "claude-opus-5-5"')))
    check("claude-opus-5-5 only at OPUS (Oracle), MODEL (Muse) and RATIONALE_MODEL (Architect)",
          len(hits) == 3 and planned == ["architect-harness/architect_harness.py", "muse-harness/muse_harness.py", "oracle-harness/oracle_harness.py"],
          "; ".join(f"{f}:{i}" for f, i, _ in hits))

    # AST: every anthropic.Anthropic(...) has max_retries=0; no temperature/thinking in messages.create/stream (B2: + the Architect)
    for f in ("oracle-harness/oracle_harness.py", "muse-harness/muse_harness.py", "architect-harness/architect_harness.py"):
        tree = ast.parse(text[f])
        clients, bad = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                fn = node.func
                if fn.attr == "Anthropic" and isinstance(fn.value, ast.Name) and fn.value.id == "anthropic":
                    mr = [k for k in node.keywords if k.arg == "max_retries"]
                    clients.append(bool(mr) and isinstance(mr[0].value, ast.Constant) and mr[0].value.value == 0)
                if fn.attr in ("create", "stream") and isinstance(fn.value, ast.Attribute) and fn.value.attr == "messages":
                    bad += [f"{f}:{node.lineno} {k.arg}" for k in node.keywords if k.arg in ("temperature", "thinking")]
        check(f"max_retries=0 on every anthropic.Anthropic() in {f}", bool(clients) and all(clients), f"{len(clients)} client(s)")
        check(f"no temperature/thinking in messages calls ({f})", not bad, ", ".join(bad))

    for f in HARNESSES:
        check(f"webbrowser.open present in {f}", "webbrowser.open(" in text[f])
    n900 = text["landing-harness/pipeline.py"].count("timeout=900")     # BUILD IC-8 (D17): the Oracle's from NR.oracle_window
    check("pipeline.py: timeout=900 once (the Muse), the Oracle's NR.oracle_window(block_n)[2]",
          n900 == 1 and "timeout=int(NR.oracle_window(block_n)[2])" in text["landing-harness/pipeline.py"], f"found {n900}")

    # ── P0.11b T1′: chat through the proxy + fresh runs ──
    import hashlib
    READER_SHA256 = "1df1ad396edcdba94d9c8e28c687d825a353740996456479f848be03db5f78e4"   # BUILD IC-5 (D11): the reader's md() — IC-3's and IC-4's was 5861a83305f96cf1…
    for f, track in (("oracle-harness/oracle_harness.py", "oracle"), ("architect-harness/architect_harness.py", "architect"),
                     ("muse-harness/muse_harness.py", "muse")):
        t = text[f]
        check(f"chat_fields + write_chat_context defined in {f}", "def chat_fields(" in t and "def write_chat_context(" in t)
        check(f"context file written beside index.html once in {f}", t.count("write_chat_context(OUTPUT_DIR, ctx)") == 1)
        check(f"chat track '{track}' passed to chat_fields in {f}", f'chat_fields("{track}"' in t)
        check(f"no chatbot_enabled False in {f}", '"chatbot_enabled": False' not in t)
    for f in TEMPLATES[:3]:
        t = text[f]
        s, e = t.find("/* === CHAT READER (P0.11b)"), t.find("/* === END CHAT READER === */\n")
        block = t[s:e + len("/* === END CHAT READER === */\n")] if 0 <= s < e else ""
        check(f"reader byte-identical to the reference in {f}", hashlib.sha256(block.encode("utf-8")).hexdigest() == READER_SHA256)
        check(f"window.CHAT config exactly once in {f}", t.count('<script>window.CHAT={"proxy":') == 1)
        check(f"'unavailable' note and FAQ in {f}", 'id="chatUnavailable"' in t and 'id="chatFaq"' in t)
    hub = text["landing-harness/landing_template.html.j2"]
    check("no chat in the hub template", "window.CHAT" not in hub and "CHAT READER" not in hub)
    p = text["landing-harness/pipeline.py"]
    check("pipeline.py: archive step before the Oracle, --keep-checkpoints, curveball unchanged",
          "def archive_checkpoints(" in p and '"--keep-checkpoints"' in p and "if not is_curveball and not args.keep_checkpoints:" in p
          and p.index("archive_checkpoints(dirs[name], stamp)") < p.index('oracle_cmd = [') and 'oracle_cmd += ["--force", "--curveball", args.curveball]' in p)
    o = text["oracle-harness/oracle_harness.py"]
    # IC-4 (D9): the 500 s reserve became the deadline-aware start (the stated rate 108.8 tokens a second and NR.start_check at every attempt)
    # BUILD IC-6 (D1, D2): the master's limit and effort are its profile's (NR.CALL_PROFILES "oracle/master"; no literal 52,000 at its call)
    # and each attempt starts by its plan (the stage decorator's NR.start_check with the profile); the stated rate stays for a call with no
    # sample
    check("Oracle r1 budget: the master's limit is its profile's (oracle/master, no literal 52,000); every attempt started by its plan",
          bool(re.search(r'@_retrying\(label="master analysis",[^\n]*profile="oracle/master"\)', o))
          and '_stream_text(prompt, system, model, "oracle/master"' in o and not re.search(r"\b52_?000\b", o)
          and bool(re.search(r"ORACLE_RATE\s*=\s*\(108\.8,", o)) and "NR.start_check(profile" in o)

    width = max(len(r[0]) for r in results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok or detail.endswith('client(s)') else ''}")
    failed = [r for r in results if not r[1]]
    print(f"\nT1′: {len(results)} checks, {len(results) - len(failed)} PASS, {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
