#!/usr/bin/env python3
"""P0.11b deploy — puts the chat proxy on Cloudflare Workers (free plan) through the REST API. Standard library only.

  Check (read-only):  python -B p011b_deploy.py --check  --account <ACCOUNT_ID>
  Deploy DEV:         python -B p011b_deploy.py --deploy dev  --account <ACCOUNT_ID> --context-file <...\\test_run\\output\\chat-context>
  Deploy PROD:        python -B p011b_deploy.py --deploy prod --account <ACCOUNT_ID>
  Kill switch:        python -B p011b_deploy.py --kill on|off --account <ACCOUNT_ID>       (PROD; --target dev for DEV)
  Delete DEV:         python -B p011b_deploy.py --delete dev --account <ACCOUNT_ID>
  Verify what is live: python -B p011b_deploy.py --verify dev|prod --account <ACCOUNT_ID>   (read-only; compares the live build to worker\\chat-proxy.mjs)
  Replace the key:    add --set-key to --deploy
  Save the token:     python -B p011b_deploy.py --save-token --account <ACCOUNT_ID>     (once; --forget-token deletes it)

The Cloudflare API token is read from the token store if one was saved (%LOCALAPPDATA%\\naipc\\cloudflare_token — local, not
in OneDrive; readable by your Windows user), otherwise asked for in a hidden prompt; --save-token verifies it with Cloudflare
before writing it. The Anthropic chat key is only ever asked for in a hidden prompt. Neither is printed or logged — only the
length and shape of what was read. Every request is logged as METHOD path -> status. Exit: 0 ok · 1 failed after a write (named; the next step is
printed) · 2 refused, nothing changed (bad token, a PROD module carrying DEV context, a bad context file or key format).
Endpoints and shapes: the Cloudflare API reference, verified 2026-09-28 (tracker §14, BUILD P0.11b RECORD 4)."""
import argparse, getpass, hashlib, json, os, re, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

API = os.environ.get("P011B_CF_API", "https://api.cloudflare.com/client/v4")          # tests point this at a mock
CHECK_SECONDS = float(os.environ.get("P011B_CHECK_SECONDS", "60"))                     # workers.dev propagation window
WORKERS = {"prod": "naipc-chat", "dev": "naipc-chat-dev"}
ORIGINS = {"oracle": "https://ctrl-alt-defeat.netlify.app", "architect": "https://ctrl-alt-defeat-arch.netlify.app",
           "muse": "https://ctrl-alt-defeat-muse.netlify.app"}
DEV_ORIGIN = "http://127.0.0.1:8766"
RATE = {"prod": ("10011", 120, 60), "dev": ("10012", 120, 60)}     # namespace_id, limit, period (Decision, RECORD 4)
COMPAT = "2026-08-06"   # = the date T12 runs the Worker at under workerd (Miniflare 4.20260730.0); tested flags = deployed flags
MAIN = "chat-proxy.mjs"
KEY_NAME = "ANTHROPIC_CHAT_KEY"
NULL_LINE = "const DEV_CONTEXT_TEXT = null; // @@DEV_CONTEXT@@ (DEV builds only; PROD keeps null)"
DEFAULT_MODULE = Path(__file__).resolve().parent.parent / "worker" / MAIN
SECRETS = []                                                       # values to redact from any message
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) p011b-check/1.0"   # Cloudflare's Browser Integrity Check on workers.dev answers 403 / 1010 to Python-urllib (R3, 9/28)


def default_store():
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "naipc" / "cloudflare_token" if base else Path.home() / ".naipc" / "cloudflare_token"


def token_shape(t):
    kind = ("account token, cfat_" if t.startswith("cfat_") else "user token, cfut_" if t.startswith("cfut_") else
            "legacy-format token" if re.fullmatch(r"[A-Za-z0-9_-]{40}", t) else "UNRECOGNIZED shape")
    return f"{len(t)} chars, {kind}"


class Refused(Exception):
    """Exit 2: nothing was changed."""


class Failed(Exception):
    """Exit 1: something was written, then a step failed."""


def safe(s):
    s = str(s)
    for v in SECRETS:
        if v:
            s = s.replace(v, "[redacted]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-[redacted]", s)


def log(method, path, status):
    print(f"  {method} {path} -> {status}")


def errors(env, status):
    parts = []
    for x in (env.get("errors") or []):
        if isinstance(x, dict):
            chain = " / ".join(f"{c.get('code')}: {c.get('message')}" for c in (x.get("error_chain") or []) if isinstance(c, dict))
            parts.append(f"{x.get('code')}: {x.get('message')}" + (f" -> {chain}" if chain else ""))
    return safe("; ".join(parts)[:400] or f"HTTP {status}")


class CF:
    def __init__(self, token, account):
        self.t, self.a = token, account

    def call(self, method, path, body=None, ctype="application/json"):
        data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + self.t, "User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            log(method, path, "network error")
            raise Failed(f"could not reach the Cloudflare API: {safe(getattr(e, 'reason', e))}")
        log(method, path, status)
        try:
            env = json.loads(raw) if raw else {}
        except ValueError:
            env = {}
        return status, env if isinstance(env, dict) else {}


def ok(status, env):
    return 200 <= status <= 299 and env.get("success") is True          # Cloudflare answers 201 Created for a new secret


def verify_token(cf):
    s, env = cf.call("GET", f"/accounts/{cf.a}/tokens/verify")                    # account-owned tokens first
    if ok(s, env) and (env.get("result") or {}).get("status") == "active":
        return "account"
    s2, env2 = cf.call("GET", "/user/tokens/verify")
    if ok(s2, env2) and (env2.get("result") or {}).get("status") == "active":
        return "user"
    raise Refused(f"the Cloudflare API token is not valid or not active ({errors(env, s)} | {errors(env2, s2)})")


def account_subdomain(cf):
    s, env = cf.call("GET", f"/accounts/{cf.a}/workers/subdomain")
    sub = (env.get("result") or {}).get("subdomain") if ok(s, env) else None
    if not sub:
        raise Refused(f"could not read the account's workers.dev subdomain ({errors(env, s)}) — check the account ID and the "
                      f"token's 'Workers Scripts: Edit' permission")
    return sub


def secret_names(cf, name):
    s, env = cf.call("GET", f"/accounts/{cf.a}/workers/scripts/{name}/secrets")
    if not ok(s, env):
        return None                                                   # the Worker does not exist (or is not visible)
    return [x.get("name") for x in (env.get("result") or []) if isinstance(x, dict)]


def module_text(path, target, context_file):
    p = Path(path)
    if not p.is_file():
        raise Refused(f"module not found: {p}")
    src = p.read_bytes().decode("utf-8")
    if src.count(NULL_LINE) != 1 or "// DEV build" in src:
        raise Refused(f"{p} is not the PROD build (the DEV marker line is missing, altered or already filled)")
    if target == "prod":
        if context_file:
            raise Refused("--context-file is for the DEV Worker only")
        return src
    if not context_file:
        raise Refused("--deploy dev needs --context-file (a chat-context\\<sha256>.json file from a rendered page)")
    cp = Path(context_file)
    if cp.is_dir():                                                  # the chat-context folder: use its one file
        found = sorted(cp.glob("*.json"))
        if len(found) != 1:
            raise Refused(f"{cp} holds {len(found)} .json files; expected exactly one chat-context file")
        cp = found[0]
    if not cp.is_file():
        raise Refused(f"context file not found: {cp}")
    raw = cp.read_bytes()
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Refused(f"{cp} is not JSON")
    if not (isinstance(doc, dict) and doc.get("schema_version") == 1 and doc.get("track") in ORIGINS and str(doc.get("context", "")).strip()):
        raise Refused(f"{cp} is not a chat-context file (schema_version 1, a track, a context)")
    if hashlib.sha256(raw).hexdigest() != cp.stem:
        raise Refused(f"{cp.name} is not named by its sha256 — use the file exactly as the harness wrote it")
    return src.replace(NULL_LINE, "const DEV_CONTEXT_TEXT = " + json.dumps(raw.decode("utf-8"), ensure_ascii=False) + "; // DEV build")


def metadata(target, disabled):
    ns, limit, period = RATE[target]
    b = [{"type": "plain_text", "name": "ALLOWED_ORIGINS", "text": json.dumps(ORIGINS if target == "prod" else {})},
         {"type": "plain_text", "name": "CHAT_DISABLED", "text": "1" if disabled else "0"},
         {"type": "ratelimit", "name": "CHAT_RL", "namespace_id": ns, "simple": {"limit": limit, "period": period}}]
    if target == "dev":
        b.append({"type": "plain_text", "name": "DEV_ORIGIN", "text": DEV_ORIGIN})
    return {"main_module": MAIN, "compatibility_date": COMPAT, "bindings": b, "keep_bindings": ["secret_text"]}


def upload(cf, name, module, meta):
    boundary = "p011b" + uuid.uuid4().hex
    parts = [("metadata", None, "application/json", json.dumps(meta).encode()),
             (MAIN, MAIN, "application/javascript+module", module.encode("utf-8"))]
    body = b""
    for n, fn, ct, data in parts:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"' + (f'; filename="{fn}"' if fn else "") +
                 f"\r\nContent-Type: {ct}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    s, env = cf.call("PUT", f"/accounts/{cf.a}/workers/scripts/{name}", body, f"multipart/form-data; boundary={boundary}")
    if not ok(s, env):
        raise Failed(f"the upload of {name} failed ({errors(env, s)}); the Worker was not changed by this step")


def ask_key():
    key = getpass.getpass(f"Anthropic chat-workspace key for {KEY_NAME} (input hidden): ").strip()
    if not (key.startswith("sk-ant-") and len(key) >= 40):
        raise Refused("that does not look like an Anthropic API key (sk-ant-…); nothing was sent")
    SECRETS.append(key)
    print(f"  chat key: entered ({len(key)} chars, sk-ant- prefix)")
    return key


def http(method, url, headers=None):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def post_checks(url, origin, disabled, build):
    """Wait until BOTH /health (ok, the expected kill-switch state, the uploaded build) and a preflight pass — a new address or version
    reaches Cloudflare's edge nodes over some seconds, and consecutive requests can land on different nodes (R4/R8, 9/28)."""
    want = {"ok": True, "disabled": disabled, "build": build}
    deadline, last = time.time() + CHECK_SECONDS, "no answer yet"
    while True:
        try:
            s, _, b = http("GET", url + "/health")
            got = json.loads(b) if s == 200 else None
            last = f"health {s} {got}" if s != 200 else f"health {got}"
            if got == want:
                p, h, _ = http("OPTIONS", url + "/chat", {"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
                last = f"health ok, preflight {p} allow-origin {h.get('access-control-allow-origin')!r}"
                if p == 204 and h.get("access-control-allow-origin") == origin:
                    log("GET", "/health", f"200 {{ok: true, disabled: {str(disabled).lower()}, build: {build}}}")
                    log("OPTIONS", "/chat", "204 from " + origin)
                    return
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = type(e).__name__
        if time.time() >= deadline:
            raise Failed(f"{url} did not pass health + preflight within {CHECK_SECONDS:.0f} s (last: {last}); expected build {build}; "
                         f"wait a minute and run --verify")
        time.sleep(min(3.0, max(0.2, CHECK_SECONDS / 20)))


def worker_build(text):
    m = re.search(r'const BUILD = "([0-9a-f]{12})";', text)
    if not m:
        raise Refused("the module carries no BUILD stamp (rebuild it with worker/build_worker.py)")
    return m.group(1)


def worker_url(name, sub):
    return os.environ.get("P011B_WORKER_URL") or f"https://{name}.{sub}.workers.dev"


def deploy(cf, target, module_path, context_file, disabled, set_key, no_prompt):
    name = WORKERS[target]
    module = module_text(module_path, target, context_file)                       # refusals before any network write
    kind = verify_token(cf)
    sub = account_subdomain(cf)
    print(f"  token: {kind} token, active · workers.dev subdomain: {sub}")
    have = secret_names(cf, name)
    key = None
    if set_key or not have or KEY_NAME not in have:
        if no_prompt:
            raise Refused(f"{name} has no {KEY_NAME} yet and --no-prompt was given; nothing was changed")
        key = ask_key()                                                           # asked and checked before the upload
    upload(cf, name, module, metadata(target, disabled))
    if key:
        s, env = cf.call("PUT", f"/accounts/{cf.a}/workers/scripts/{name}/secrets", {"name": KEY_NAME, "text": key, "type": "secret_text"})
        key = None
        if not ok(s, env):
            raise Failed(f"setting {KEY_NAME} on {name} failed ({errors(env, s)}); run --deploy {target} --set-key")
    names = secret_names(cf, name) or []
    if KEY_NAME not in names:
        raise Failed(f"{name} was uploaded but {KEY_NAME} is not set; run --deploy {target} --set-key")
    s, env = cf.call("POST", f"/accounts/{cf.a}/workers/scripts/{name}/subdomain", {"enabled": True})
    if not ok(s, env):
        raise Failed(f"enabling {name} on workers.dev failed ({errors(env, s)})")
    url = worker_url(name, sub)
    build = worker_build(module)
    post_checks(url, DEV_ORIGIN if target == "dev" else ORIGINS["oracle"], disabled, build)
    print(f"\nOK: {name} is live at {url} (chat {'OFF — kill switch on' if disabled else 'on'}, build {build})")
    return 0


def verify(cf, target, module_path):
    """Read-only: is the live Worker the build in the local module file, and does it answer health + a preflight?"""
    name = WORKERS[target]
    p = Path(module_path)
    if not p.is_file():
        raise Refused(f"module not found: {p}")
    local = worker_build(p.read_bytes().decode("utf-8"))
    verify_token(cf)
    sub = account_subdomain(cf)
    url = worker_url(name, sub)
    s, _, b = http("GET", url + "/health")
    got = json.loads(b) if s == 200 else {}
    log("GET", "/health", f"{s} {got}")
    if not got.get("ok"):
        raise Failed(f"{url}/health did not answer ok (HTTP {s})")
    if got.get("build") != local:
        raise Failed(f"{name}: the live build is {got.get('build')}, the local module is {local} — run --deploy {target} to update it")
    post_checks(url, DEV_ORIGIN if target == "dev" else ORIGINS["oracle"], bool(got.get("disabled")), local)
    print(f"\nOK: {name} is live at {url}, build {local}, chat {'OFF (kill switch on)' if got.get('disabled') else 'on'} (nothing was changed)")
    return 0


def check(cf):
    kind = verify_token(cf)
    sub = account_subdomain(cf)
    print(f"  token: {kind} token, active · workers.dev subdomain: {sub}")
    for target, name in WORKERS.items():
        names = secret_names(cf, name)
        state = "not deployed" if names is None else f"deployed · {KEY_NAME} {'set' if KEY_NAME in names else 'NOT set'}"
        print(f"  {target:<4} {name:<15} {state:<40} {worker_url(name, sub)}")
    print("\nOK: read-only check complete (nothing was changed)")
    return 0


def delete_dev(cf):
    verify_token(cf)
    name = WORKERS["dev"]
    s, env = cf.call("DELETE", f"/accounts/{cf.a}/workers/scripts/{name}")
    if not (200 <= s <= 299 and (not env or env.get("success") is True)):
        raise Failed(f"deleting {name} failed ({errors(env, s)})")
    print(f"\nOK: {name} deleted (the PROD Worker was not touched)")
    return 0


def read_token(store):
    if store.is_file():
        t = store.read_bytes().decode("utf-8").strip()
        print(f"  token: from {store} ({token_shape(t)})")
        return t
    t = getpass.getpass("Cloudflare API token (input hidden): ").strip()
    print(f"  token: entered ({token_shape(t)})")
    return t


def save_token(store, account):
    t = getpass.getpass("Cloudflare API token to save (input hidden): ").strip()
    print(f"  token: entered ({token_shape(t)})")
    if not t:
        raise Refused("no token entered")
    SECRETS.append(t)
    kind = verify_token(CF(t, account))                                          # Refused (nothing stored) if invalid
    store.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(store), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(t)
    print(f"\nOK: {kind} token verified and saved to {store} (readable by your Windows user; --forget-token deletes it)")
    return 0


def forget_token(store):
    if store.is_file():
        store.unlink()
        print(f"\nOK: token store deleted ({store})")
    else:
        print(f"\nOK: no token store at {store}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="P0.11b chat-proxy deploy (Cloudflare Workers REST API)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--save-token", action="store_true", help="verify a token with Cloudflare and save it for later runs")
    g.add_argument("--forget-token", action="store_true", help="delete the saved token")
    g.add_argument("--check", action="store_true", help="read-only: token, subdomain, which Workers exist")
    g.add_argument("--deploy", choices=["dev", "prod"])
    g.add_argument("--kill", choices=["on", "off"], help="on = chat off (CHAT_DISABLED=1); off = chat back on")
    g.add_argument("--delete", choices=["dev"], help="delete the DEV Worker (PROD is never deleted by this tool)")
    g.add_argument("--verify", choices=["dev", "prod"], help="read-only: the live build vs the local module, health, preflight")
    ap.add_argument("--account", required=True, help="the Cloudflare account ID (32 hex characters)")
    ap.add_argument("--target", choices=["dev", "prod"], default="prod", help="which Worker --kill acts on")
    ap.add_argument("--context-file", help="DEV only: a rendered page's chat-context folder (or the JSON file in it)")
    ap.add_argument("--module", default=str(DEFAULT_MODULE), help="the built chat-proxy.mjs (default: worker\\chat-proxy.mjs)")
    ap.add_argument("--set-key", action="store_true", help="ask for the chat key and set it even if one is set")
    ap.add_argument("--no-prompt", action="store_true", help="never ask for the chat key (fail instead)")
    ap.add_argument("--token-file", help="where the token is saved/read (default: %%LOCALAPPDATA%%\\naipc\\cloudflare_token)")
    a = ap.parse_args(argv)
    store = Path(a.token_file) if a.token_file else default_store()
    try:
        if not re.fullmatch(r"[0-9a-f]{32}", a.account):
            raise Refused("--account must be the 32-character account ID (Workers & Pages → Account details)")
        if a.deploy or a.kill:
            target = a.deploy or a.target
            module_text(a.module, target, a.context_file if target == "dev" else None)   # validate before asking for anything
        if a.forget_token:
            return forget_token(store)
        if a.save_token:
            return save_token(store, a.account)
        token = read_token(store)
        if not token:
            raise Refused("no token entered")
        SECRETS.append(token)
        cf = CF(token, a.account)
        if a.check:
            return check(cf)
        if a.verify:
            return verify(cf, a.verify, a.module)
        if a.delete:
            return delete_dev(cf)
        if a.deploy:
            return deploy(cf, a.deploy, a.module, a.context_file, False, a.set_key, a.no_prompt)
        name = WORKERS[a.target]
        print(f"Kill switch {a.kill}: chat {'OFF' if a.kill == 'on' else 'ON'} for {name}")
        return deploy(cf, a.target, a.module, a.context_file if a.target == "dev" else None, a.kill == "on", False, True)
    except Refused as e:
        print(f"\nREFUSED: {safe(e)}\nNothing was changed.")
        return 2
    except Failed as e:
        print(f"\nFAILED: {safe(e)}")
        return 1
    finally:
        SECRETS.clear()


if __name__ == "__main__":
    sys.exit(main())
