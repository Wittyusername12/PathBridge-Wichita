"""BUILD IC-7 (D14; §7 R5) — the sampler: the Muse's Call 1, then Call 2 on its reply, N times (--n; 30 in the run turn), on a copy of R7's
Oracle checkpoints (tools/fixtures/r7_oracle: the five files the Muse reads, each bound by its sha256 from evidence/manifests/p2b1.json)
and the challenge API's data from the fixture server on Wichita `pre` (each endpoint's reply checked against
tools/fixtures/wichita-2026-04-18/pre; another set is refused). It uses the harness's own functions — fetch_endpoints, detect_domain,
load_oracle_checkpoints, _summarize_api_data, generate_strategic, generate_creative, call_claude and the admission (admit_reply) — in a copy
of the Muse's folder, with no pipeline; the one change is the attempt loop: each call is sent once (its first attempt), and a reply the
admission would send to a second attempt (D3) is recorded as such and not sent again.
Per sample, each call: its stop reason, output and input tokens, thinking tokens, first event, first text and end (seconds); D2's
entries by path and rule; whether D3 would have been taken (a field the page cannot do without, no JSON, a reply cut at its limit) or
the track lost (a refusal, another stop, an error the harness does not retry); each figure path's token form (kept, missed, no figure);
each text field's length (items and characters), against the recorded free replies (tools/fixtures/free_replies_ic7.json: IC-6's
requests — Nov 12, Nov 14 and R7's two; a measure: no source gives the strict schema's effect on long prose).
The summary: the failures and the exact one-sided 95% upper bound on the failure rate (Clopper–Pearson; zero failures in 30: 9.50%); the
largest size and time of each call against its profile, and what the largest of N bounds (its 90th percentile, with confidence 1 − 0.9^N:
95.8% at 30); each figure path's misses (more than 3 in 30: recorded, a wording matter, §9); the text lengths; and the contract's hash
(contract_hash: the wires, the page models, the rules, the rule lines, the profiles' limits and the request code — a later change to
it opens the §9 row "re-sample", t_promises_ic7).
It stops after the first sample that would have taken D3 or lost the track, or whose size or time passes its profile (R5's stop rule:
ask Shane); --keep-going draws all N. A dropped stream or a status the harness would retry stops it too, that sample not counted:
--work <its folder> continues. Paid (the build prompt's estimate: about $12.5–13.2 for 30 pairs). The key is read from the environment
as the harnesses read it and never printed ("set" or "not set"). Everything is written in a new folder beside the four,
<root>/naipc-r5-ic7-<UTC stamp>: the copy's checkpoints (each call's call-log line, journal, capacity and admission lines), samples.jsonl,
summary.json and meta.json. The four folders are read, never written.
Usage: python -I -B sample_contract_ic7.py --root "%USERPROFILE%\\OneDrive\\Documents" --n 30 [--api-url http://127.0.0.1:8765/]
       [--keep-going] [--work <a folder this tool made>]"""
import contextlib, copy, hashlib, inspect, io, json, math, os, re, shutil, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WI = "wichita-2026-04-18"
R7_RUN = "20261116T235259Z"
R7_ORACLE = HERE / "fixtures" / "r7_oracle"
ORACLE_FILES = {"master_analysis.json": "92793beac9de2f4eb1e2a6d685fd19398dc21ebb9f82c91045ce8d9d00b200de",
                "financial_model.json": "1ed9cf58583478c1fbf1a07a3ef15fea5e69ed86f0740d32fe2c705c0a852c9c",
                "registry.json": "f476e64a986ac8c95c1e36be33f56ba0ddf93f7a5bbe828be80a6df821ceb4a9",
                "track_parts.json": "2435a3b15303bc37d0cf372bec22ee32509a3ccca2a4b805ea374637174a8471",
                "data_loading.json": "24484233a735b58b736bd9cece6710e3a97e3d5f17b5bbd464b7c3a5f3110d7c"}
DATA = HERE / "fixtures" / WI / "pre"
FREE = HERE / "fixtures" / "free_replies_ic7.json"
KEYS = ("muse/call1", "muse/call2")
RUN_VARS = ("NAIPC_RUN_ID", "NAIPC_REQUIREMENTS", "NAIPC_CHALLENGE", "NAIPC_URL_SLASH", "NAIPC_RERUN_AFTER_END", "NAIPC_CHANGES",
            "NAIPC_BLOCK_END", "NAIPC_BLOCK", "NAIPC_ACCEPT", "CURVEBALL_TEXT", "CONTRACT_OVERRIDE")   # a run's own; the switches stay
IGNORE = shutil.ignore_patterns("output", "output_prev", "checkpoints*", "coverage", "runs", "chat-context*", "__pycache__", "test_output")


def sha16(obj):
    b = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    return hashlib.sha256(b).hexdigest()[:16]


def contract_hash(MH):
    """The contract of the two calls, as data, hashed (sha256, first 16): the wires (each variant), the page models' schemas, the Muse's
    admission rules and counts, the target's forms, each call's rule lines, the profiles' limits, the model, and the code that writes
    the requests and admits the replies (its source text). The same on every machine with the same files and pins."""
    NR, REG = MH.NR, MH.REG
    doc = {"model": MH.MODEL,
           "wires": dict({f"muse/call1 brief={b} parts={p}": MH.muse_wire("muse/call1", b, p) for b in (False, True) for p in (False, True)},
                         **{f"muse/call2 parts={p}": MH.muse_wire("muse/call2", False, p) for p in (False, True)}),
           "pages": {k: MH.CONTRACTS[k]["page"].model_json_schema() for k in KEYS},
           "rules": {k: MH.MUSE_RULES[k] for k in KEYS}, "list_asked": MH.LIST_ASKED, "engagement": list(MH.ENGAGEMENT),
           "forms": [MH.TARGET_FORM, MH.TARGET_FORM_NO_PARTS],
           "rule_lines": {k: REG.number_rules_text(MH.CONTRACTS[k]["rules_view"], ic4=True) for k in KEYS},
           "profiles": {k: {f: NR.CALL_PROFILES[k][f] for f in ("max_tokens", "effort", "stream", "form")} for k in KEYS},
           "code": {f"{f.__module__}.{f.__name__}": inspect.getsource(f) for f in (
               MH.generate_strategic, MH.generate_creative, MH._wire_desc, MH.muse_wire, MH.admit_reply, MH.calendar_instruction,
               MH.oracle_context_block, MH.muse_rules_parts, MH._extract_json, MH.call_claude, NR.brief_block, NR.wire_schema, NR.admit,
               NR.admit_walk, NR.output_config, REG.examples_block, REG.rules_examples, REG.number_rules_text)}}
    return sha16(doc)


def values_at(data, path):
    """The values at a path ("kpis[].target", "partner_pitch.data_points[].stat") of a reply."""
    cur = [data]
    for part in path.split("."):
        many = part.endswith("[]")
        name = part[:-2] if many else part
        nxt = []
        for c in cur:
            v = c.get(name) if isinstance(c, dict) else None
            if many:
                nxt += v if isinstance(v, list) else []
            elif v is not None:
                nxt.append(v)
        cur = nxt
    return cur


def token_forms(data, paths, token_re):
    """Each figure path's values by form: kept (a token, and no digit outside one), missed (a digit outside any token, or a JSON
    number), no figure (neither)."""
    out = {}
    for p in paths:
        c = {"kept": 0, "missed": 0, "no figure": 0}
        for v in values_at(data, p):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                c["missed"] += 1
            elif isinstance(v, str):
                bare = re.search(r"\d", token_re.sub("", v))
                c["missed" if bare else "kept" if token_re.search(v) else "no figure"] += 1
        out[p] = c
    return out


def text_lengths(data, wire):
    """Each text path of the wire: [items, characters] in the reply."""
    out = {}

    def walk(v, s, path):
        t = s.get("type")
        if t == "object" and isinstance(v, dict):
            for k, sub in (s.get("properties") or {}).items():
                walk(v.get(k), sub, f"{path}.{k}" if path else k)
        elif t == "array" and isinstance(v, list):
            for x in v:
                walk(x, s.get("items") or {}, f"{path}[]")
        elif t == "string" and isinstance(v, str):
            c = out.setdefault(path, [0, 0])
            c[0] += 1
            c[1] += len(v)
    walk(data, wire, "")
    return out


def binom_cdf(k, n, p):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def upper_bound(k, n, alpha=0.05):
    """The exact one-sided upper (1 − alpha) bound on a rate with k failures in n (Clopper–Pearson): the p at which P(X ≤ k) = alpha."""
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


class NotAdmitted(Exception):
    """A first attempt the harness would not admit: kind "D3" (its second attempt would be sent) or "lost" (the harness raises)."""
    def __init__(self, key, kind, why):
        super().__init__(f"{key}: {kind} — {why}")
        self.key, self.kind, self.why = key, kind, why


class TransportStop(Exception):
    """A failure an identical request can fix (the harness retries it): the sample is not counted, and the sampler stops."""


def main(argv):
    def arg(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    root = Path(arg("--root", str(Path.home() / "OneDrive" / "Documents")))
    n = int(arg("--n", "30"))
    api_url = arg("--api-url", "http://127.0.0.1:8765/")
    keep_going = "--keep-going" in argv
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"R5 ANTHROPIC_API_KEY: {'set' if key.strip() else 'not set'}")
    if not key.strip():
        print("R5 not run: no key in the environment (set it as for a live run, then run this again)")
        return 2
    muse = next((root / x for x in ("muse-harness", "Muse-harness") if (root / x / "muse_harness.py").is_file()), None)
    req = root / "landing-harness" / "requirements" / f"{WI}.json"
    bad = [f for f, h in ORACLE_FILES.items() if not (R7_ORACLE / f).is_file() or hashlib.sha256((R7_ORACLE / f).read_bytes()).hexdigest() != h]
    if muse is None or not req.is_file() or bad:
        print("R5 not run: " + "; ".join(x for x in (f"no muse_harness.py under {root}" if muse is None else "",
                                                       f"no contract {req}" if not req.is_file() else "",
                                                       f"R7's Oracle checkpoints missing or not R7's: {', '.join(bad)}" if bad else "") if x))
        return 2
    work = Path(arg("--work")) if "--work" in argv else root / f"naipc-r5-ic7-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    cont = "--work" in argv
    if cont and not (work / "meta.json").is_file():
        print(f"R5 not run: {work} is not a folder this tool made (no meta.json)")
        return 2
    if not cont:
        if work.exists():
            print(f"R5 not run: {work} exists")
            return 2
        shutil.copytree(muse, work / "muse-harness", ignore=IGNORE)
        (work / "muse-harness" / "checkpoints").mkdir()
        (work / "oracle-harness" / "checkpoints").mkdir(parents=True)
        for f in ORACLE_FILES:
            shutil.copy2(R7_ORACLE / f, work / "oracle-harness" / "checkpoints" / f)
        (work / "requirements").mkdir()
        shutil.copy2(req, work / "requirements" / req.name)
    for v in RUN_VARS:
        os.environ.pop(v, None)
    os.environ.update(NAIPC_RUN_ID=R7_RUN, NAIPC_CHALLENGE=WI, NAIPC_REQUIREMENTS=str(work / "requirements" / req.name))
    sys.path.insert(0, str(work / "muse-harness"))
    with contextlib.redirect_stdout(io.StringIO()):
        import muse_harness as MH                     # the copy of the installed harness: its functions, its module NR and REG
    NR, REG = MH.NR, MH.REG
    if not NR.ic7_prompt():
        print("R5 not run: NAIPC_IC7_PROMPT is off in this window (or a switch it builds on is) — the sampler samples IC-7's contract")
        return 2
    ch = contract_hash(MH)
    meta = {"contract_sha256_16": ch, "n": n, "challenge": WI, "oracle_run": R7_RUN, "api_url": api_url, "model": MH.MODEL,
            "oracle_files": ORACLE_FILES, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if cont:
        old = json.loads((work / "meta.json").read_text(encoding="utf-8"))
        if old.get("contract_sha256_16") != ch:
            print(f"R5 not run: the contract changed since {work}'s samples ({old.get('contract_sha256_16')} → {ch}): start a new folder")
            return 2
        meta = dict(old, n=n)
    (work / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(f"R5 the contract's hash: {ch}; the work folder: {work} (the four folders are not written)")

    doc = MH._brief_contract()
    endpoints = NR.endpoint_names(doc) if doc else []
    with contextlib.redirect_stdout(io.StringIO()):
        raw = MH.fetch_endpoints(api_url, endpoints)
    want = {p.stem: json.loads(p.read_bytes().decode("utf-8-sig")) for p in sorted(DATA.glob("*.json"))}
    off = sorted(ep for ep in endpoints if raw.get(ep) != want.get(ep)) + sorted(set(want) - set(endpoints))
    if not endpoints or off or MH.FETCH_ERRORS:
        print(f"R5 not run: the API at {api_url} is not the fixture server on Wichita pre — {', '.join(off or MH.FETCH_ERRORS) or 'no endpoint'} "
              "differ(s) from tools/fixtures/wichita-2026-04-18/pre (start it as R5s says, then run this again)")
        return 2
    print(f"R5 the data: {len(endpoints)} endpoints from {api_url}, each equal to tools/fixtures/{WI}/pre")
    REG.set_endpoints(endpoints)
    domain = MH.detect_domain(raw)
    with contextlib.redirect_stdout(io.StringIO()):
        octx = MH.load_oracle_checkpoints(str(work / "oracle-harness" / "checkpoints"))
    if not isinstance(octx.get("_parts"), dict) or octx.get("_reg") is None:
        print("R5 not run: the Oracle's registry or request parts were not read from R7's checkpoints (NAIPC_IC4_PROMPT off?)")
        return 2
    summary_text = MH._summarize_api_data(raw)
    MH._BRIEF.update(doc=doc, facts=MH.muse_pipeline_facts(len(endpoints) - len(MH.FETCH_ERRORS)), raw=None, answers={})
    free = json.loads(FREE.read_text(encoding="utf-8")) if FREE.is_file() else {}
    figs = {k: list(MH.MUSE_RULES[k]["display_numbers"]) for k in KEYS}
    prof = {k: NR.profile(k) for k in KEYS}
    cur = {}

    def call_line():
        rec = (getattr(NR.model_call, "last", None) or ({}, ""))[0] or {}
        u = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        return {"stop_reason": rec.get("stop_reason"), "output_tokens": u.get("output_tokens"), "input_tokens": u.get("input_tokens"),
                "thinking_tokens": rec.get("thinking_tokens"), "first_event_s": rec.get("t_first_event"),
                "first_text_s": rec.get("t_first_text"), "end_s": rec.get("t_end"), "error": rec.get("error"),
                **{k: rec.get(k) for k in ("call_id", "invocation_number", "call_sequence", "generation_attempt")}}   # BUILD IC-8 (D9)

    def one_attempt(name, ckey, api_key, system, user, wire, parts=True, pop=()):
        """The harness's attempt loop, its first attempt only (BUILD IC-7 D14): the same request, call and admission."""
        c = cur.setdefault(ckey, {})
        c["request"] = {"system_sha16": sha16(system), "user_chars": len(user), "wire_sha16": sha16(wire) if wire else None, "parts": bool(parts)}
        MH._require_budget(name, ckey)
        try:
            text = MH.call_claude(api_key, system, user, ckey, fmt=wire)
        except Exception as e:  # noqa: BLE001
            c.update(call_line())
            if isinstance(e, MH.MaxTokensReached):
                raise NotAdmitted(ckey, "D3", "a reply cut at its output limit") from e
            if MH.is_retryable(e):
                raise TransportStop(f"{ckey}: {type(e).__name__} (the harness would retry it)") from e
            raise NotAdmitted(ckey, "lost", f"{type(e).__name__}: {str(e)[:160]}") from e
        c.update(call_line())
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                data = MH._extract_json(text)
        except Exception as e:  # noqa: BLE001
            raise NotAdmitted(ckey, "D3", f"the reply is not JSON ({type(e).__name__})") from e
        if not isinstance(data, dict):
            raise NotAdmitted(ckey, "D3", f"the reply is a {type(data).__name__}, not one object")
        c["figures"] = token_forms(data, figs[ckey], REG.TOKEN_RE)
        c["text"] = text_lengths(data, wire or {})
        popped = {k: data.pop(k, None) for k in pop}
        obj, entries, errs = MH.admit_reply(ckey, data, 1, decode=MH._LAST_DECODE, parts=parts)
        by = {}
        for e in entries:
            k = f"{e.get('path')}: {e.get('rule')}"
            by[k] = by.get(k, 0) + 1
        c["entries"] = by
        if obj is None:
            raise NotAdmitted(ckey, "D3", f"{len(errs)} field(s) the page cannot do without: "
                              + ", ".join(str(x.get("path")) for x in errs[:6]))
        c["outcome"] = "admitted, named" if entries else "admitted"
        return obj, popped

    MH._call_admitted = one_attempt           # the one change: the first attempt only (no second attempt is sent)
    sfile = work / "samples.jsonl"
    done = [json.loads(x) for x in sfile.read_text(encoding="utf-8").splitlines() if x.strip()] if sfile.is_file() else []
    stop = None
    for i in range(len(done) + 1, n + 1):
        MH._STAGE_T0 = None                    # each sample is a stage of its own (840 s, as the harness's)
        cur.clear()
        s = {"n": i, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print(f"R5 sample {i} of {n}", flush=True)
        try:
            st = MH.generate_strategic(key, summary_text, domain, octx, "")
            MH.generate_creative(key, st, domain, octx, "")
        except NotAdmitted as e:
            cur.setdefault(e.key, {}).update(outcome=f"{e.kind}: {e.why}", d3=e.kind == "D3", lost=e.kind == "lost")
        except TransportStop as e:
            print(f"R5 stopped: {e} — this sample is not counted; run again with --work \"{work}\" to go on")
            stop = "transport"
            break
        except Exception as e:  # noqa: BLE001 — anything else the harness raises would have failed the track
            k_ = "muse/call2" if "muse/call1" in cur and cur["muse/call1"].get("outcome", "").startswith("admitted") else "muse/call1"
            cur.setdefault(k_, {}).update(outcome=f"lost: {type(e).__name__}: {str(e)[:160]}", lost=True)
        for k in KEYS:
            c = cur.get(k)
            if c is None:
                s[k] = {"not run": "Call 1's first attempt was not admitted"}
                continue
            c.setdefault("d3", False)
            c.setdefault("lost", False)
            p = prof[k]
            over = []
            if isinstance(c.get("output_tokens"), int) and c["output_tokens"] > p["planning_tokens"]:
                over.append(f"output {c['output_tokens']:,} > planned {p['planning_tokens']:,}")
            if isinstance(c.get("end_s"), (int, float)) and isinstance(p.get("planning_s"), (int, float)) and c["end_s"] > p["planning_s"]:
                over.append(f"{c['end_s']} s > planned {p['planning_s']} s")
            c["over_profile"] = over
            s[k] = c
        with open(sfile, "a", encoding="utf-8") as f:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
        done.append(s)
        line = []
        for k in KEYS:
            c = s[k]
            if "not run" in c:
                line.append(f"{k} not run")
                continue
            miss = [p for p, v in (c.get("figures") or {}).items() if v["missed"]]
            line.append(f"{k} {c.get('stop_reason')} {c.get('output_tokens')} out ({c.get('thinking_tokens')} thinking) {c.get('end_s')} s, "
                        f"{c.get('outcome')}" + (f", {sum((c.get('entries') or {}).values())} named" if c.get("entries") else "")
                        + (f"; a token missed on {', '.join(miss)}" if miss else "") + (f"; OVER ITS PROFILE: {'; '.join(c['over_profile'])}"
                                                                                         if c.get("over_profile") else ""))
        print(f"R5 sample {i}: " + " | ".join(line), flush=True)
        bad_ = [k for k in KEYS if "not run" not in s[k] and (s[k]["d3"] or s[k]["lost"] or s[k]["over_profile"])]
        if bad_ and not keep_going:
            stop = f"sample {i}: " + "; ".join(f"{k} {s[k].get('outcome')}" + (f" ({'; '.join(s[k]['over_profile'])})" if s[k]["over_profile"] else "")
                                               for k in bad_)
            print(f"R5 STOP (§7 R5's stop rule): {stop} — ask Shane; the samples so far are in {sfile}")
            break

    # ── the summary ──
    m = len(done)
    fails = [x for x in done if any("not run" not in x[k] and (x[k]["d3"] or x[k]["lost"]) for k in KEYS)]
    summ = {"contract_sha256_16": ch, "n_asked": n, "n_drawn": m, "stopped": stop, "failures": len(fails),
            "failure_bound_95": upper_bound(len(fails), m), "calls": {}, "work": str(work)}
    print(f"R5 summary: {m} sample(s) of {n} asked; the contract's hash {ch}")
    ub = summ["failure_bound_95"]
    print(f"R5 failures (a first attempt not admitted: D3 or a lost track): {len(fails)} of {m}"
          + (f" — the exact one-sided 95% upper bound on the rate: {ub:.2%}" if ub is not None else ""))
    for k in KEYS:
        rows = [x[k] for x in done if "not run" not in x[k]]
        p = prof[k]
        if not rows:
            print(f"R5 {k}: no sample")
            continue
        outs = [r["output_tokens"] for r in rows if isinstance(r.get("output_tokens"), int)]
        ends = [r["end_s"] for r in rows if isinstance(r.get("end_s"), (int, float))]
        firsts = [r["first_text_s"] for r in rows if isinstance(r.get("first_text_s"), (int, float))]
        ents = {}
        for r in rows:
            for e, c_ in (r.get("entries") or {}).items():
                ents[e] = ents.get(e, 0) + c_
        fig = {}
        for r in rows:
            for path, v in (r.get("figures") or {}).items():
                f_ = fig.setdefault(path, {"samples_missed": 0, "kept": 0, "missed": 0, "no figure": 0})
                f_["samples_missed"] += 1 if v["missed"] else 0
                for x in ("kept", "missed", "no figure"):
                    f_[x] += v[x]
        txt = {}
        for r in rows:
            for path, (items, chars) in (r.get("text") or {}).items():
                t_ = txt.setdefault(path, [0, 0])
                t_[0] += items
                t_[1] += chars
        cov = 1 - 0.9 ** len(outs) if outs else None
        summ["calls"][k] = {"n": len(rows), "d3": sum(r["d3"] for r in rows), "lost": sum(r["lost"] for r in rows),
                            "largest_output": max(outs) if outs else None, "planning_tokens": p["planning_tokens"],
                            "largest_end_s": max(ends) if ends else None, "planning_s": p["planning_s"],
                            "largest_first_text_s": max(firsts) if firsts else None, "first_line_s": p.get("first_line_s"),
                            "p90_confidence": cov, "entries": ents, "figures": fig, "text": txt,
                            "over_profile": [r["over_profile"] for r in rows if r["over_profile"]]}
        print(f"R5 {k}: {len(rows)} sample(s); D3 would have been taken {summ['calls'][k]['d3']}, lost {summ['calls'][k]['lost']}; "
              f"the largest output {max(outs) if outs else '?'} of {p['planning_tokens']:,} planned; the longest {max(ends) if ends else '?'} s "
              f"of {p['planning_s']} s planned; the latest first text {max(firsts) if firsts else '?'} s (its profile's first line "
              f"{p.get('first_line_s')} s)" + (f"; the largest of {len(outs)} bounds the 90th percentile with {cov:.1%} confidence" if cov else ""))
        print(f"R5 {k} admission entries over the samples: " + (", ".join(f"{e} {c_}" for e, c_ in sorted(ents.items())) or "none"))
        for path, f_ in fig.items():
            flag = f_["samples_missed"] > 3 * len(rows) / 30
            print(f"R5 {k} figure {path}: a token missed in {f_['samples_missed']} of {len(rows)} sample(s) (items: kept {f_['kept']}, missed "
                  f"{f_['missed']}, no figure {f_['no figure']})" + (" — more than 3 in 30: recorded, a wording matter (§9), not a stop" if flag else ""))
        base = (free.get("calls") or {}).get(k) or {}
        for path, (items, chars) in sorted(txt.items()):
            b = base.get(path)
            mean = chars / items if items else 0
            bm = (b["chars"] / b["items"]) if b and b.get("items") else None
            print(f"R5 {k} text {path}: {items} item(s), {mean:.0f} characters each on average" +
                  (f"; the recorded free replies {bm:.0f} ({mean / bm:.2f}×)" if bm else "; not in the recorded free replies"))
    (work / "summary.json").write_text(json.dumps(summ, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    over_any = any(summ["calls"].get(k, {}).get("over_profile") for k in KEYS)
    if stop is None and not fails and not over_any and m == n:
        print(f"R5 PASS: {m} pairs, no first attempt sent to D3 or lost, every size and time inside its profile; the summary: {work / 'summary.json'}")
        return 0
    print(f"R5 {'STOPPED' if stop else 'DONE with findings'}: " + (stop or f"{len(fails)} failure(s); over a profile: {over_any}")
          + f" — ask Shane before R6; the summary: {work / 'summary.json'}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
