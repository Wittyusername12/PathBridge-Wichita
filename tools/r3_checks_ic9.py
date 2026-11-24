"""BUILD IC-9 (D13, Q-PREFLIGHT) — R3: IC-8's checks (tools/r3_checks_ic8.py, run as they are), then the census of his state, printed to the
console, wired to IC-8's own reader (tools/rederive_limits_ic8.py read_roots over the four folders, through the tree's NR.read_records):
  the call logs it reads, split into current (<folder>/checkpoints/call_log.jsonl) and archived (<folder>/checkpoints_archive/*/
  call_log.jsonl) — files and lines; the live calls by call id (a legacy line by its run, label, time and duration), per track and
  profile; the console windows and the invocations records; each environment variable the pipeline and the harnesses read (by its name,
  read from the tree's code), "set" or "not set"; the browser locator (PLAYWRIGHT_BROWSERS_PATH set or not, the playwright package found or
  not); the installed package's hashes (each folder's code files, and each shared module's copies).
Names, counts and hashes only — never a value, a text or a personal file's name; it reads no file outside the four folders and writes
nothing. Its last line, "census-json: {…}", is what ic9_dev/census_compare_ic9.py reads (counts and hashes only).
Usage (his machine): python -I -B r3_checks_ic9.py --root "%USERPROFILE%\\OneDrive\\Documents"
       (a census alone):  python3 -I -B r3_checks_ic9.py --census-only --root <a folder holding the four> [--src <the tree's src>]"""
import ast, hashlib, importlib.util, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rederive_limits_ic8 as RD  # noqa: E402 — IC-8's reader (read_roots) and its folders' tracks

FOLDERS = ("oracle-harness", "architect-harness", "muse-harness", "landing-harness")
CODE = (".py", ".j2", ".js", ".css")                  # the package's code files in a folder (never a checkpoint, a page or a log)
SKIP = ("checkpoints", "checkpoints_archive", "output", "output_prev", "versions", "runs", "coverage", "chat-context-archive",
        "__pycache__", "test_output", "node_modules")
SHARED = ("naipc_registry.py", "naipc_requirements.py", "naipc_engine.py")


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _folder(root, name):
    """The folder of that name in root, any letter case (his Muse folder is "Muse-harness")."""
    return next((p for p in sorted(Path(root).iterdir()) if p.is_dir() and p.name.lower() == name), None) if Path(root).is_dir() else None


def env_names(dirs):
    """Every environment variable name the code of `dirs` reads (os.environ.get / [ ] / pop / in, os.getenv), from its source: sorted."""
    out = set()
    for f in sorted(p for d in dirs for p in Path(d).rglob("*.py")):
        if any(s in f.parts for s in SKIP + ("vendor",)):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            s = None
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args and isinstance(n.args[0], ast.Constant):
                f_ = n.func
                if f_.attr in ("get", "pop", "setdefault") and ast.unparse(f_.value) in ("os.environ", "environ"):
                    s = n.args[0].value
                elif f_.attr == "getenv" and ast.unparse(f_.value) == "os":
                    s = n.args[0].value
            elif isinstance(n, ast.Subscript) and ast.unparse(n.value) in ("os.environ", "environ") and isinstance(n.slice, ast.Constant):
                s = n.slice.value
            elif (isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops)
                  and any(ast.unparse(c) in ("os.environ", "environ") for c in n.comparators)):
                s = n.left.value
            if isinstance(s, str) and s.isupper() and s.replace("_", "").isalnum():
                out.add(s)
    return sorted(out)


def census(root, src, code_dirs=None):
    """The census of the four folders under `root` — counts, names and hashes (see the docstring). `src`: the tree whose reader reads
    the logs; `code_dirs`: the code whose environment names are read (the four folders on his machine; the tree's src here)."""
    root = Path(root)
    NR = RD.load_nr(src)
    logs, pkg, shared = {}, {}, {}
    for name in FOLDERS:
        d = _folder(root, name)
        t = RD.FOLDER_TRACK[name]
        cur = [d / "checkpoints/call_log.jsonl"] if d is not None and (d / "checkpoints/call_log.jsonl").is_file() else []
        arc = sorted(d.glob("checkpoints_archive/*/call_log.jsonl")) if d is not None else []
        n_lines = lambda fs: sum(sum(1 for x in f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()) for f in fs)  # noqa: E731
        logs[t] = {"current": len(cur), "current_lines": n_lines(cur), "archived": len(arc), "archived_lines": n_lines(arc)}
        files = sorted(p for p in (d.rglob("*") if d is not None else []) if p.is_file() and p.suffix in CODE
                       and not any(s in p.relative_to(d).parts for s in SKIP))
        pkg[t] = {"files": len(files), "sha256": hashlib.sha256("".join(f"{p.relative_to(d).as_posix()}\0{_sha(p)}\n" for p in files)
                                                                    .encode()).hexdigest()[:16] if files else None}
        for m in SHARED:
            if d is not None and (d / m).is_file():
                shared.setdefault(m, {})[t] = _sha(d / m)[:16]
    rd = RD.read_roots([root], NR)
    calls = {}
    for _k, c in rd["calls"].items():
        calls.setdefault(c["track"], {}).setdefault(c["profile"] or c["label"] or "?", 0)
        calls[c["track"]][c["profile"] or c["label"] or "?"] += 1
    dirs = code_dirs or [d for d in (_folder(root, n) for n in FOLDERS) if d is not None]
    env = {n: ("set" if os.environ.get(n) not in (None, "") else "not set") for n in env_names(dirs)}
    pw = importlib.util.find_spec("playwright") is not None
    return {"logs": logs, "calls": {t: dict(sorted(v.items())) for t, v in sorted(calls.items())}, "calls_total": len(rd["calls"]),
            "windows": len(rd["windows"]), "invocation_records": len(rd["records"]),
            "invocation_lines": sum(len(v) for v in rd["records"].values()), "refused_or_repeated": len(rd["notes"]),
            "env": env, "browser": {"PLAYWRIGHT_BROWSERS_PATH": "set" if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else "not set",
                                    "playwright package": "found" if pw else "not found"},
            "package": pkg, "shared": shared}


def print_census(c):
    lg = c["logs"]
    print(f"R3 census: {c['calls_total']} live calls (by call id) from {sum(v['current'] + v['archived'] for v in lg.values())} logs "
          f"({sum(v['current'] for v in lg.values())} current, {sum(v['archived'] for v in lg.values())} archived); "
          f"{c['windows']} console window(s); {c['invocation_records']} invocations record(s), {c['invocation_lines']} line(s)")
    for t, v in lg.items():
        print(f"  {t:<9} logs: {v['current']} current ({v['current_lines']} line(s)), {v['archived']} archived ({v['archived_lines']} line(s)); "
              f"calls: {json.dumps(c['calls'].get(t, {}), ensure_ascii=False)}")
    print(f"  refused or repeated lines (IC-8's reader): {c['refused_or_repeated']}")
    print("  environment (by name): " + "; ".join(f"{k} {v}" for k, v in c["env"].items()))
    print(f"  browser locator: PLAYWRIGHT_BROWSERS_PATH {c['browser']['PLAYWRIGHT_BROWSERS_PATH']}; the playwright package "
          f"{c['browser']['playwright package']}")
    for t, v in c["package"].items():
        print(f"  package {t:<9} {v['files']} code file(s), sha256 {v['sha256']}")
    for m, v in c["shared"].items():
        print(f"  shared {m}: {len(v)} cop(y/ies), {len(set(v.values()))} hash(es) — {sorted(set(v.values()))}")
    print("census-json: " + json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv):
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else None
    src = Path(argv[argv.index("--src") + 1]) if "--src" in argv else None
    if root is None:
        print("R3 census: --root <the folder holding the four> is needed")
        return 2
    rc = 0
    if "--census-only" not in argv:
        import r3_checks_ic8 as R8         # IC-8's checks, as they are
        rc = R8.main([a for a in argv if a != "--census-only"])
    code = [src] if src is not None else None
    if src is None:            # his machine: the tree is the four folders' own copy (the Oracle's naipc_requirements.py beside it)
        d = _folder(root, "oracle-harness")
        src = d if d is not None and (d / "naipc_requirements.py").is_file() else HERE.parent / "src"
    print_census(census(root, src, code))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
