#!/usr/bin/env python3
"""P0.11a T1 — static check of the 13 harness files.

Usage:  python -B p011a_static_check.py --root "C:\\Users\\shane\\OneDrive\\Documents"
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

    # claude-opus-5-5 exactly at the two planned constants
    hits = [(f, i, line.strip()) for f in text for i, line in enumerate(text[f].split("\n"), 1) if "claude-opus-5-5" in line]
    planned = sorted(h[0] for h in hits if h[2].startswith(('OPUS = "claude-opus-5-5"', 'MODEL = "claude-opus-5-5"')))
    check("claude-opus-5-5 only at OPUS (Oracle) and MODEL (Muse)",
          len(hits) == 2 and planned == ["muse-harness/muse_harness.py", "oracle-harness/oracle_harness.py"],
          "; ".join(f"{f}:{i}" for f, i, _ in hits))

    # AST: every anthropic.Anthropic(...) has max_retries=0; no temperature/thinking in messages.create/stream
    for f in ("oracle-harness/oracle_harness.py", "muse-harness/muse_harness.py"):
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
    n900 = text["landing-harness/pipeline.py"].count("timeout=900")
    check("pipeline.py: timeout=900 exactly twice (Oracle, Muse)", n900 == 2, f"found {n900}")

    width = max(len(r[0]) for r in results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok or detail.endswith('client(s)') else ''}")
    failed = [r for r in results if not r[1]]
    print(f"\nT1: {len(results)} checks, {len(results) - len(failed)} PASS, {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
