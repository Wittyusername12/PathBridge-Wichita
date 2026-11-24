#!/usr/bin/env python3
"""naipc_requirements — the P0.2 requirements contract, schema v1: load, validate, template, ids, merge_curveball.
Standard library only. One byte-identical copy lives in each of the four harness folders (the static check compares them).

  python -B naipc_requirements.py validate <challenge-id>.json      exit 0 valid · 1 invalid (every problem is listed)
  python -B naipc_requirements.py template questions|deliverables [--out <file>]
                                                                    a skeleton to fill on site; validate lists each <TODO>.
                                                                    --out writes the file as UTF-8 (no BOM, LF) and refuses an
                                                                    existing path — a redirected stdout on Windows is cp1252 (P0.8b)
  python -B naipc_requirements.py ids <challenge-id>.json           the item, user, winning and endpoint ids to cover
  python -B naipc_requirements.py map-template <challenge-id>.json --out <challenge-id>.map.json
                                                                    a coverage map to fill: every item, the section catalogue,
                                                                    a field guide (P0.3)
  python -B naipc_requirements.py validate-map <challenge-id>.map.json
                                                                    exit 0 valid (unmapped items listed as gaps) · 1 invalid
  python -B naipc_requirements.py coverage <track> <page.html> --requirements <challenge-id>.json
                                                                    the coverage check of one rendered page (reads only)
  python -B naipc_requirements.py draft-conditions <challenge-id>.json
                                                                    BUILD IC-4 (D15): a link and an acceptance condition per
                                                                    requirement, drafted from the brief's words into
                                                                    <challenge-id>.conditions.draft.json (never into the map)
  python -B naipc_requirements.py confirm-conditions <challenge-id>.conditions.draft.json --items users.<id>,winning.<id>,…
                                                                    the operator approves: the named drafts into the map, validated

pipeline.py exports NAIPC_REQUIREMENTS (the contract's absolute path) and NAIPC_CHALLENGE (its id) to every harness;
from_env() loads that contract. Schema: outputs/p02/requirements_schema_v1.md (plan 2026-09-28, amended after research
round N2 on 2026-09-29). A contract is one JSON file per challenge; keys starting with "_" are notes and are ignored.
P0.3 (2026-10-29): an item may carry "source" (scorecard | deliverable | mission); every question a track's mission asks must
be a "source": "mission" item; the coverage map <challenge-id>.map.json beside the contract names the page sections that answer
each item; each track page carries data-sec / data-req / data-user attributes and two metas, and coverage_check() measures it
with the standard library's html.parser (no bs4 on the event laptop).
P0.6 (2026-10-30): facts() — every Rule 4 value from the contract, None when the brief states none (null means never invent);
competition_label(), unit_forms(), money_short(), endpoint_url() (the one URL builder; NAIPC_URL_SLASH=1 adds the slash), the
program identity file (write_identity / read_identity, never raising) and clean_brand_html(); dates are written from MONTHS, never
strftime (Windows has no unpadded-day directive, and the month name follows the locale).
P0.7 (2026-11-01): the curveball as a diff — records() / envelope_split(), the one envelope rule of every harness (D5);
diff_snapshots(), Block 1's snapshot and Block 2's change list (write/read, never raising; temp file + os.replace), the summary
lines and the change list's prompt and chat renderings (D1); curveball_input(), the announcement from a sidecar or a text file
(D2); merge_amended(), Python's per-field merge of an update reply (D3, D7); archive_contexts(), one chat context per folder (D9);
save_partial(), a failed call's text kept (D10); escape_markup(), every new text of an update reply (D11); context_size_note(), the
chat Worker's 96 KiB; stated_tokens() / stale_keys(), where Block 1's text still states a value this run changed, and
match_test_answers(), the curveball's tests with the reply's answers (the independent review, 11/02). Nothing here raises: a
reader returns (None, reason), a writer the reason it did not write.
P0.7 r2 (2026-11-02): the update path never silent — cut(), one marked cut of a model's text in characters or UTF-8 bytes (E2);
schema_block(), the JSON schema of exactly the fields an update may change, its annotations removed (E1; it raises ValueError for a
field the schema lacks — a caller's constant, never data); plain_reason(), pydantic's validation errors (as plain dicts — this module
never imports pydantic) in plain words that name no key or value the model wrote (E3 a); ref_check(), the reasons an update gave for
one part; merge_amended() keeps a check's plain reason and its technical detail; parse_brief_answers(plain=True), its notes as
named parts; not_applied_chat_lines(), the chat context's "Not applied at the curveball" lines within their byte caps (E3 c)."""
import argparse, copy, datetime, hashlib, html, json, os, re, sys, uuid
from html.parser import HTMLParser
from pathlib import Path

SCHEMA_VERSION = 1
TRACKS = ("oracle", "architect", "muse")
KINDS = ("deliverables", "questions")
CONSTRAINT_KEYS = ("contract_usd", "opex_usd", "reserved_marketing_usd", "pilot_days", "beneficiary_unit", "required_assets")
REQUIRED_TOP = ("schema_version", "challenge", "constraints", "named_users", "tracks", "winning_looks_like", "scoring", "api", "team", "curveball")
OPTIONAL_TOP = ("schedule", "submission", "provenance", "authority", "relations", "aliases", "outcomes")   # BUILD IC-4 (D3, D6): the last four
# BUILD IC-4 (D3, D5, D6): what the operator confirms on the day (the data report drafts each into <challenge>.draft.json; `confirm` copies
# the items named into the contract). A figure is a data id by the registry's grammar (<endpoint>.<field>, <endpoint>.<record id>.<field>,
# <endpoint>.count, sum:<endpoint>.<field>, sum:<endpoint>.<a>-<b>) or a quote of a text's figure (q:<where>|<words>), with an optional
# part of a ratio or a range (num, den, lo, hi).
AUTHORITY_KEYS = ("metric", "words", "endpoint", "figure", "as_of")
AUTHORITY_AS_OF = ("the endpoint's own time field", "the fetch time")
RELATION_KEYS = ("id", "left", "op", "right", "words")
RELATION_OPS = ("<=", "<", "==", ">=", ">")
ALIAS_KEYS = ("old", "new", "why")
OUTCOME_KEYS = ("id", "words", "quote")
FIGURE_RX = re.compile(r"^(?:sum:[a-z0-9][a-z0-9_-]*\.[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)?|[a-z0-9][a-z0-9_-]*(?:\.[A-Za-z0-9_:-]+){1,2}|"
                       r"q:[^|\[\]]+\|[^\[\]]+|[a-z][a-z0-9_-]{0,60})(?:#(?:num|den|lo|hi))?$")   # IC-4 Phase 7 (review b17): a sidecar's named figure
SCORING_KEYS = ("dimensions", "descriptors", "scale", "scale_legend", "notes", "source")
SUBMISSION_KEYS = ("form_url", "deadline", "event_code")
CURVEBALL_KINDS = ("VERBATIM", "MODELED")
TODO = "<TODO>"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SOURCES = ("scorecard", "deliverable", "mission")                  # P0.3: an item's origin in the brief (absent = the track's kind)
MISSION_URL = re.compile(r"https?://\S+")                           # removed before the question scan: a "?" inside a URL is not a question
MISSION_Q = re.compile(r"[^.?!]+\?(?=[\s\"'”’)]|$)")       # a "?" counts when whitespace, a closing quote or bracket, or the end follows


class RequirementsError(Exception):
    """Raised by load(), merge_curveball() and from_env(); .problems lists every problem found."""
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _strip(x):
    return {k: v for k, v in x.items() if not str(k).startswith("_")} if isinstance(x, dict) else x


def _is_todo(v):
    return isinstance(v, str) and TODO in v


def _text(v):
    return isinstance(v, str) and v.strip() != ""


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _todos(x, path, out):
    """Every <TODO> placeholder anywhere (keys starting with '_' skipped), one problem each."""
    if isinstance(x, dict):
        for k, v in x.items():
            if not str(k).startswith("_"):
                _todos(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            _todos(v, f"{path}[{i}]", out)
    elif _is_todo(x):
        out.append(f"{path}: {TODO} placeholder — fill it from the brief (or null where the brief states nothing)")


def _norm_q(s):
    """Whitespace collapsed, the typographic apostrophe made plain: the form mission questions and items are compared in."""
    return re.sub(r"\s+", " ", str(s if s is not None else "").replace("’", "'")).strip()


def mission_questions(mission):
    """The questions a mission sentence asks (P0.3, D1): URLs removed first; a fragment counts only when its "?" is followed by
    whitespace, a closing quote or bracket, or the end, and when it holds a letter or digit. After an abbreviation ("St.", "U.S.")
    the fragment is the question's tail; inside quotes it may carry the words before the quote — an item matches either way."""
    if not _text(mission):
        return []
    return [q for q in (_norm_q(f) for f in MISSION_Q.findall(MISSION_URL.sub(" ", mission))) if re.search(r"\w", q)]


def validate(doc, stem=None):
    """Return every problem as 'path.to.field: reason' (an empty list means valid). A <TODO> value is reported once and
    skips the other checks on that value, so a template yields exactly one problem per placeholder."""
    P = []
    if not isinstance(doc, dict):
        return ["file: the top level must be a JSON object"]
    _todos(doc, "", P)
    d = _strip(doc)
    for k in d:
        if k not in REQUIRED_TOP and k not in OPTIONAL_TOP:
            P.append(f"{k}: unknown top-level key")
    for k in REQUIRED_TOP:
        if k not in d:
            P.append(f"{k}: required top-level key is missing")
    if "schema_version" in d and d["schema_version"] != SCHEMA_VERSION:
        P.append(f"schema_version: must be {SCHEMA_VERSION}, got {d['schema_version']!r}")
    ids = {}

    def claim(i, where):
        if i in ids:
            P.append(f"{where}: duplicate id {i!r} (first used at {ids[i]})")
        else:
            ids[i] = where

    # V2 challenge
    ch = _strip(d.get("challenge"))
    if "challenge" in d:
        if not isinstance(ch, dict):
            P.append("challenge: must be an object")
        else:
            cid = ch.get("id")
            if not _is_todo(cid):
                if not (isinstance(cid, str) and SLUG.match(cid)):
                    P.append(f"challenge.id: a lowercase slug is required, got {cid!r}")
                elif stem is not None and cid != stem:
                    P.append(f"challenge.id: {cid!r} must equal the file name {stem!r}")
            for k in ("title", "mission"):
                if not _is_todo(ch.get(k)) and not _text(ch.get(k)):
                    P.append(f"challenge.{k}: required (non-empty text)")
            if "day" in ch and ch["day"] is not None and not _is_todo(ch["day"]) and not (_int(ch["day"]) and ch["day"] >= 1):
                P.append(f"challenge.day: a positive int or null, got {ch['day']!r}")
    # V3 constraints
    cons = _strip(d.get("constraints"))
    if "constraints" in d:
        if not isinstance(cons, dict):
            P.append("constraints: must be an object")
        else:
            for k in cons:
                if k not in CONSTRAINT_KEYS:
                    P.append(f"constraints.{k}: unknown constraint (the six are {', '.join(CONSTRAINT_KEYS)})")
            for k in CONSTRAINT_KEYS:
                if k not in cons:
                    P.append(f"constraints.{k}: key required (null when the brief states none)")
                    continue
                v = _strip(cons[k])
                if v is None:
                    continue
                if not isinstance(v, dict) or "value" not in v or "quote" not in v:
                    P.append(f"constraints.{k}: null or {{value, quote}} required, got {cons[k]!r}")
                    continue
                q, val = v["quote"], v["value"]
                if not _is_todo(q) and not _text(q):
                    P.append(f"constraints.{k}.quote: the brief's words are required, got {q!r}")
                if _is_todo(val):
                    continue
                if k in ("contract_usd", "opex_usd", "reserved_marketing_usd") and not (_int(val) and val >= 0):
                    P.append(f"constraints.{k}.value: a non-negative int is required, got {val!r}")
                elif k == "pilot_days" and not (_int(val) and val > 0):
                    P.append(f"constraints.{k}.value: a positive int is required, got {val!r}")
                elif k == "beneficiary_unit" and not _text(val):
                    P.append(f"constraints.{k}.value: non-empty text is required, got {val!r}")
                elif k == "required_assets" and not (isinstance(val, list) and val and all(_text(a) for a in val)):
                    P.append(f"constraints.{k}.value: a non-empty list of names is required, got {val!r}")
    # V4 named users
    users = d.get("named_users")
    if "named_users" in d:
        if not isinstance(users, list):
            P.append("named_users: must be a list (empty when the brief names nobody)")
        else:
            for i, u in enumerate(users):
                u = _strip(u)
                w = f"named_users[{i}]"
                if not isinstance(u, dict):
                    P.append(f"{w}: must be an object"); continue
                uid = u.get("id")
                if not _is_todo(uid):
                    if not (isinstance(uid, str) and SLUG.match(uid)):
                        P.append(f"{w}.id: a lowercase slug is required, got {uid!r}")
                    else:
                        claim(uid, f"{w}.id")
                for k in ("name", "role"):
                    if not _is_todo(u.get(k)) and not _text(u.get(k)):
                        P.append(f"{w}.{k}: required (non-empty text)")
                if u.get("age") is not None and not _is_todo(u.get("age")) and not _int(u.get("age")):
                    P.append(f"{w}.age: an int or null, got {u.get('age')!r}")
    # V5 tracks — exactly three
    tr = _strip(d.get("tracks"))
    if "tracks" in d:
        if not isinstance(tr, dict):
            P.append("tracks: must be an object with exactly oracle, architect, muse")
        else:
            for t in tr:
                if t not in TRACKS:
                    P.append(f"tracks.{t}: unknown track (the tracks are exactly {', '.join(TRACKS)})")
            for t in TRACKS:
                x = _strip(tr.get(t))
                w = f"tracks.{t}"
                if t not in tr:
                    P.append(f"{w}: required track is missing"); continue
                if not isinstance(x, dict):
                    P.append(f"{w}: must be an object"); continue
                if x.get("kind") not in KINDS and not _is_todo(x.get("kind")):
                    P.append(f"{w}.kind: 'deliverables' or 'questions', got {x.get('kind')!r}")
                if not _is_todo(x.get("mission")) and not _text(x.get("mission")):
                    P.append(f"{w}.mission: required (non-empty text)")
                items = x.get("items")
                if not isinstance(items, list) or not items:
                    P.append(f"{w}.items: at least one item is required"); continue
                for j, it in enumerate(items):
                    it = _strip(it)
                    iw = f"{w}.items[{j}]"
                    if not isinstance(it, dict):
                        P.append(f"{iw}: must be an object"); continue
                    iid = it.get("id")
                    if not _is_todo(iid):
                        if not (isinstance(iid, str) and SLUG.match(iid)):
                            P.append(f"{iw}.id: a lowercase slug is required, got {iid!r}")
                        else:
                            claim(iid, f"{iw}.id")
                    if not _is_todo(it.get("text")) and not _text(it.get("text")):
                        P.append(f"{iw}.text: required (non-empty text)")
                    if "source" in it and not _is_todo(it.get("source")) and it.get("source") not in SOURCES:
                        P.append(f"{iw}.source: 'scorecard', 'deliverable' or 'mission' (or leave it out), got {it.get('source')!r}")
                # P0.3 (D1): every question the mission asks is a "source": "mission" item of this track
                if _text(x.get("mission")) and not _is_todo(x.get("mission")):
                    have = [_norm_q(i.get("text")) for i in (_strip(i) for i in items)
                            if isinstance(i, dict) and i.get("source") == "mission" and _text(i.get("text")) and not _is_todo(i.get("text"))]
                    for q in mission_questions(x.get("mission")):
                        if not any(h and (h.endswith(q) or q.endswith(h)) for h in have):
                            P.append(f'{w}.mission: question "{q}" is not an item (add it to {w}.items with "source": "mission")')
    # V6 winning looks like
    win = d.get("winning_looks_like")
    if "winning_looks_like" in d:
        if not isinstance(win, list):
            P.append("winning_looks_like: must be a list (empty when the brief has none)")
        else:
            for j, x in enumerate(win):
                x = _strip(x)
                w = f"winning_looks_like[{j}]"
                if not isinstance(x, dict):
                    P.append(f"{w}: must be an object"); continue
                wid = x.get("id")
                if not _is_todo(wid):
                    if not (isinstance(wid, str) and SLUG.match(wid)):
                        P.append(f"{w}.id: a lowercase slug is required, got {wid!r}")
                    else:
                        claim(wid, f"{w}.id")
                if not _is_todo(x.get("text")) and not _text(x.get("text")):
                    P.append(f"{w}.text: required (non-empty text)")
    # V7 api
    names = []
    api = _strip(d.get("api"))
    if "api" in d:
        if not isinstance(api, dict):
            P.append("api: must be an object")
        else:
            if not _is_todo(api.get("base_url")) and not str(api.get("base_url") or "").startswith("https://"):
                P.append(f"api.base_url: an https URL is required, got {api.get('base_url')!r}")
            if not _is_todo(api.get("prefix")) and not str(api.get("prefix") or "").startswith("/"):
                P.append(f"api.prefix: must start with '/', got {api.get('prefix')!r}")
            eps = api.get("endpoints")
            if not isinstance(eps, list) or not eps:
                P.append("api.endpoints: at least one endpoint is required")
            else:
                for j, e in enumerate(eps):
                    e = _strip(e)
                    w = f"api.endpoints[{j}]"
                    if not isinstance(e, dict):
                        P.append(f"{w}: must be an object"); continue
                    n = e.get("name")
                    if not _is_todo(n):
                        if not (isinstance(n, str) and SLUG.match(n)):
                            P.append(f"{w}.name: a lowercase slug is required, got {n!r}")
                        elif n in names:
                            P.append(f"{w}.name: duplicate endpoint {n!r}")
                        else:
                            names.append(n)
                    if not isinstance(e.get("curveball_updates"), bool):
                        P.append(f"{w}.curveball_updates: true or false is required, got {e.get('curveball_updates')!r}")
                    re_ = e.get("records_expected")
                    if re_ is not None and not _is_todo(re_) and not (_int(re_) and re_ >= 0):
                        P.append(f"{w}.records_expected: a non-negative int or null, got {re_!r}")
    # V7b scoring
    sc = _strip(d.get("scoring"))
    if "scoring" in d:
        if not isinstance(sc, dict):
            P.append("scoring: must be an object")
        else:
            for k in sc:
                if k not in SCORING_KEYS:
                    P.append(f"scoring.{k}: unknown key (the six are {', '.join(SCORING_KEYS)})")
            for k in SCORING_KEYS:
                if k not in sc:
                    P.append(f"scoring.{k}: required")
            dims = sc.get("dimensions")
            if "dimensions" in sc and not (isinstance(dims, list) and len(dims) == 5 and all(_text(x) for x in dims)):
                P.append(f"scoring.dimensions: exactly five dimension names, got {dims!r}")
            desc = _strip(sc.get("descriptors"))
            if "descriptors" in sc and isinstance(dims, list):
                if not isinstance(desc, dict) or set(desc) != set(dims) or not all(_text(v) for v in desc.values()):
                    P.append("scoring.descriptors: one non-empty descriptor per dimension, keyed by the dimension names")
            leg = _strip(sc.get("scale_legend"))
            if "scale_legend" in sc and not (isinstance(leg, dict) and set(leg) == {"1", "2", "3", "4", "5"} and all(_text(v) for v in leg.values())):
                P.append("scoring.scale_legend: labels for exactly \"1\" … \"5\"")
            for k in ("scale", "source"):
                if k in sc and not _is_todo(sc[k]) and not _text(sc[k]):
                    P.append(f"scoring.{k}: required (non-empty text)")
    # V7c submission
    if "submission" in d and d["submission"] is not None:
        sub = _strip(d["submission"])
        if not isinstance(sub, dict) or set(sub) != set(SUBMISSION_KEYS):
            P.append(f"submission: null or exactly {', '.join(SUBMISSION_KEYS)} (each may be null)")
        else:
            for k in SUBMISSION_KEYS:
                if sub[k] is not None and not isinstance(sub[k], str):
                    P.append(f"submission.{k}: text or null, got {sub[k]!r}")
    # V8 curveball
    if "curveball" in d and d["curveball"] is not None:
        P.extend(_curveball_problems(d["curveball"], names, "curveball"))
    # V10 (BUILD IC-4, D3/D5/D6): authority, relations, aliases, outcomes — each a list of objects with exactly its keys
    for blk, keys in (("authority", AUTHORITY_KEYS), ("relations", RELATION_KEYS), ("aliases", ALIAS_KEYS), ("outcomes", OUTCOME_KEYS)):
        if blk not in d or d[blk] is None:
            continue
        lst = _strip(d[blk])
        if not isinstance(lst, list):
            P.append(f"{blk}: must be a list (empty when none is confirmed)")
            continue
        for i, it in enumerate(lst):
            it, w = _strip(it), f"{blk}[{i}]"
            if not isinstance(it, dict) or set(it) != set(keys):
                P.append(f"{w}: an object with exactly {', '.join(keys)}")
                continue
            if blk == "authority":
                if not (isinstance(it["metric"], str) and SLUG.match(it["metric"])):
                    P.append(f"{w}.metric: a lowercase slug, got {it['metric']!r}")
                if not _text(it["words"]) or not _text(it["endpoint"]):
                    P.append(f"{w}: words and endpoint are non-empty text")
                if not (isinstance(it["figure"], str) and FIGURE_RX.match(it["figure"])):
                    P.append(f"{w}.figure: a data id or a quote form (see FIGURE_RX), got {it['figure']!r}")
                if it["as_of"] not in AUTHORITY_AS_OF:
                    P.append(f"{w}.as_of: one of {', '.join(AUTHORITY_AS_OF)}")
            elif blk == "relations":
                if not (isinstance(it["id"], str) and SLUG.match(it["id"])):
                    P.append(f"{w}.id: a lowercase slug, got {it['id']!r}")
                for side in ("left", "right"):
                    if not (isinstance(it[side], str) and FIGURE_RX.match(it[side])):
                        P.append(f"{w}.{side}: a data id or a quote form, got {it[side]!r}")
                if it["op"] not in RELATION_OPS:
                    P.append(f"{w}.op: one of {' '.join(RELATION_OPS)}")
                if not _text(it["words"]):
                    P.append(f"{w}.words: the relation in words (non-empty text)")
            elif blk == "aliases":
                for side in ("old", "new"):
                    if not (isinstance(it[side], str) and re.match(r"^[a-z0-9][a-z0-9_-]*\.[A-Za-z0-9_]+$", it[side])):
                        P.append(f"{w}.{side}: <endpoint>.<field>, got {it[side]!r}")
                if not _text(it["why"]):
                    P.append(f"{w}.why: non-empty text")
            else:
                if not (isinstance(it["id"], str) and SLUG.match(it["id"])):
                    P.append(f"{w}.id: a lowercase slug, got {it['id']!r}")
                else:
                    claim(it["id"], w)
                if not _text(it["words"]) or not _text(it["quote"]):
                    P.append(f"{w}: words and quote (the brief's own) are non-empty text")
    # V9 team
    if "team" in d:
        tm = _strip(d["team"])
        if not isinstance(tm, dict):
            P.append("team: must be an object ({program_name, brand_html}, each may be null)")
        else:
            for k in ("program_name", "brand_html"):
                if tm.get(k) is not None and not _is_todo(tm.get(k)) and not _text(tm.get(k)):
                    P.append(f"team.{k}: null or non-empty text, got {tm.get(k)!r}")
    return P


def _curveball_problems(cv, endpoint_names, where):
    P = []
    cv = _strip(cv)
    if not isinstance(cv, dict):
        return [f"{where}: null or an object"]
    if cv.get("kind") not in CURVEBALL_KINDS and not _is_todo(cv.get("kind")):
        P.append(f"{where}.kind: 'VERBATIM' or 'MODELED', got {cv.get('kind')!r}")
    if not _is_todo(cv.get("text")) and not _text(cv.get("text")):
        P.append(f"{where}.text: the announcement's text is required")
    tests = cv.get("tests", [])
    if not isinstance(tests, list) or not all(isinstance(x, str) for x in tests):
        P.append(f"{where}.tests: a list of text")
    ch = cv.get("api_changed", [])
    if not isinstance(ch, list):
        P.append(f"{where}.api_changed: a list of endpoint names")
    else:
        for j, a in enumerate(ch):
            if not _is_todo(a) and a not in endpoint_names:
                P.append(f"{where}.api_changed[{j}]: unknown endpoint {a!r} (not in api.endpoints)")
    return P


def parse(raw, label="file"):
    """Decode (UTF-8, BOM tolerated) and parse; problems name line and column. Returns (doc, problems)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return None, [f"{label}: not UTF-8 text (byte {e.start})"]
    if not text.strip():
        return None, [f"{label}: empty"]
    try:
        return json.loads(text), []
    except json.JSONDecodeError as e:
        return None, [f"{label}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}"]


def load(path):
    """Read, parse and validate a contract; raise RequirementsError with every problem; never return a partial one."""
    p = Path(path)
    if not p.is_file():
        raise RequirementsError([f"file: not found: {p}"])
    doc, problems = parse(p.read_bytes(), "file")
    if problems:
        raise RequirementsError(problems)
    problems = validate(doc, stem=p.stem)
    if problems:
        raise RequirementsError(problems)
    return doc


def from_env():
    """The contract pipeline.py exported (NAIPC_REQUIREMENTS), checked against NAIPC_CHALLENGE when that is set."""
    path = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    if not path:
        raise RequirementsError(["NAIPC_REQUIREMENTS: not set (run through pipeline.py --requirements <file>)"])
    doc = load(path)
    want = os.environ.get("NAIPC_CHALLENGE", "").strip()
    if want and doc["challenge"]["id"] != want:
        raise RequirementsError([f"NAIPC_CHALLENGE: {want!r} but the contract is {doc['challenge']['id']!r}"])
    return doc


def item_ids(doc):
    return [it["id"] for t in TRACKS for it in doc["tracks"][t]["items"]]


def item_source(item, kind):
    """An item's origin (P0.3): its "source", else the track's kind (questions → scorecard, deliverables → deliverable)."""
    s = item.get("source")
    return s if s in SOURCES else ("scorecard" if kind == "questions" else "deliverable")


def tracks_summary(doc):
    """Per track 'oracle questions ×4 + mission ×3' (the mission count only when there are mission items)."""
    out = []
    for t in TRACKS:
        its = doc["tracks"][t]["items"]
        m = sum(1 for it in its if it.get("source") == "mission")
        out.append(f"{t} {doc['tracks'][t]['kind']} ×{len(its) - m}" + (f" + mission ×{m}" if m else ""))
    return out


def user_ids(doc):
    return [u["id"] for u in doc.get("named_users") or []]


def winning_ids(doc):
    return [w["id"] for w in doc.get("winning_looks_like") or []]


def endpoint_names(doc):
    return [e["name"] for e in doc["api"]["endpoints"]]


def constraint(doc, key):
    """The constraint's value, or None when the brief states none."""
    if key not in CONSTRAINT_KEYS:
        raise KeyError(key)
    c = doc["constraints"].get(key)
    return None if c is None else c["value"]


def api_base(doc):
    return doc["api"]["base_url"].rstrip("/") + "/" + doc["api"]["prefix"].strip("/") + "/"


def merge_curveball(doc, sidecar):
    """A copy of the contract with its curveball slot filled from a sidecar ({schema_version, kind, source, announced_at,
    text, tests, api_changed}); refused (RequirementsError) when the sidecar is invalid or names an unknown endpoint."""
    sc = _strip(sidecar)
    problems = [] if isinstance(sc, dict) and sc.get("schema_version") == SCHEMA_VERSION else ["sidecar.schema_version: must be 1"]
    cv = {k: sc.get(k) for k in ("kind", "source", "announced_at", "text", "tests", "api_changed")} if isinstance(sc, dict) else sc
    problems += _curveball_problems(cv, endpoint_names(doc), "sidecar")
    if problems:
        raise RequirementsError(problems)
    out = copy.deepcopy(doc)
    out["curveball"] = cv
    problems = validate(out)
    if problems:
        raise RequirementsError(problems)
    return out


KC_SCORECARD = {
    "dimensions": ["Problem Understanding", "Solution Quality", "Presentation & Polish", "Adaptability", "AI Mastery"],
    "descriptors": {
        "Problem Understanding": "Did they actually understand the problem? Do they know who the users/customers are and what they need? Or did they build something generic?",
        "Solution Quality": "Is the solution good? Is the business plan sound? Is the product useful? Is the marketing strategy smart? Would this actually work?",
        "Presentation & Polish": "Is it well-built and clearly presented? Can you interact with the dashboard? Does the plan read well? Are materials production-ready?",
        "Adaptability": "How did they respond to the curveball? Did they rethink their approach or just bolt something on? (Score after Block 2)",
        "AI Mastery": "How effectively did they use AI? Did they direct it or just accept first outputs? Evidence of iteration, challenging AI, using it creatively?"},
    "scale": "circle one 1-5 per dimension per judge; TOTAL / 25 × 4 = / 100",
    "scale_legend": {"1": "Didn't address", "2": "Significant gaps", "3": "Adequate", "4": "Strong work", "5": "Exceptional"},
    "notes": "Adaptability is scored after Block 2; each judge adds 'What stood out? (one sentence)'; the panel is three industry experts and one non-human (AI) judge",
    "source": "Kansas_City_scorecard.png (KC Regional 2026 judge scorecard, March 28, 2026), read 2026-09-29; the same five dimensions appear on every 2026 brief"}


def template(kind):
    """A skeleton with every field present: <TODO> where the brief must be typed in; the scoring block pre-filled."""
    if kind not in KINDS:
        raise ValueError(kind)
    def track(t):
        n = 4 if kind == "questions" else 6
        tag = "q" if kind == "questions" else "d"
        return {"kind": kind, "title": TODO, "mission": TODO,
                "items": [{"id": f"{t}-{tag}{i + 1}", "text": TODO} for i in range(n)],
                "_note": "one item per " + ("judging question, verbatim" if kind == "questions" else "listed deliverable; rename ids to short slugs") + "; delete unused items. "
                         f"ALSO one item per question this track's mission asks and per explicit ask in it (\"how X works\", \"what Y proves\"), "
                         f"in the brief's words, with ids {t}-m1, {t}-m2 … and \"source\": \"mission\" — validate names every mission question "
                         "that is not yet an item"}
    return {
        "schema_version": SCHEMA_VERSION,
        "_note": f"Template ({kind}). Fill every {TODO} from the brief in its own words; use null where the brief states nothing; "
                 "save as <challenge-id>.json; then: python -B naipc_requirements.py validate <challenge-id>.json",
        "challenge": {"id": TODO, "event": "naipc-nationals-2026", "day": 1, "date": TODO, "city": TODO, "venue": TODO,
                      "title": TODO, "tagline": TODO, "mission": TODO},
        "constraints": {k: {"value": TODO, "quote": TODO} for k in CONSTRAINT_KEYS},
        "named_users": ([{"id": TODO, "name": TODO, "age": None, "role": TODO, "quote": TODO}] if kind == "deliverables" else []),
        "tracks": {t: track(t) for t in TRACKS},
        "winning_looks_like": ([{"id": "win-1", "text": TODO}] if kind == "deliverables" else []),
        "scoring": copy.deepcopy(KC_SCORECARD),
        "api": {"base_url": "https://aipromptchamp.com", "prefix": TODO,
                "endpoints": [{"name": TODO, "description": TODO, "records_expected": None, "curveball_updates": False}],
                "_note": "one entry per row of the brief's API table; name = the path's last segment"},
        "schedule": {"go_live": TODO, "curveball": TODO, "submissions_due": TODO},
        "submission": {"form_url": None, "deadline": TODO, "event_code": TODO},
        "team": {"program_name": None, "brand_html": None},
        "curveball": None,
        "provenance": {"brief_files": [TODO], "transcription": TODO, "modeled_fields": []},
    }


# ═══ P0.3 — the coverage gate: catalogue, map, page attributes, checker, manifest (D2–D7); the brief's answers (D9–D10) ═══

OK_CHARS, THIN_CHARS = 200, 40       # D5 judgment thresholds: a "static" block is OK at ≥ 200 visible characters, THIN at 40–199
PENDING = "PENDING — "               # the Oracle template's own defaults for an unfilled field: never counted as content
STATUS_RANK = ("OK", "THIN", "FIXED", "EMPTY", "MISSING")    # best first; an item takes the best status over its mapped sections
COVERED = ("OK", "THIN", "FIXED")
ANSWER_TRACKS = ("oracle", "muse")   # the model answers the brief on these pages; the Architect's panel shows the map's notes
# BUILD IC-4 (D15): requirements met, not merely linked — a map entry's link type and its acceptance condition, checked on the built page
LINK_TYPES = ("implements", "supports", "verifies")
CONDITION_KEYS = {"sentence": ("kind", "section", "user", "phrases"), "table": ("kind", "section", "columns"),
                  "figure": ("kind", "section", "figure"), "review": ("kind", "why"), "all": ("kind", "children"),
                  "languages": ("kind", "section", "langs")}      # BUILD IC-5 (D10): a part of the section in each language named
REQ_STATUS = ("PASS", "REVIEW", "FAIL")      # met · needs review (never green by itself) · not met
FIGURE_KINDS = ("MEASURED", "DERIVED", "EXTERNAL", "MODELED")      # the registry's kinds (naipc_registry.KINDS)
UNIT_TAGS = frozenset("p li td th dd dt div blockquote figcaption caption pre section article header footer main aside".split())
LABEL_TAGS = frozenset("h1 h2 h3 h4 h5 h6 th summary legend label option button".split())   # a heading's, a header cell's or a form
#                                                                                                control's words: never a sentence (D15)
# The section catalogue (D2) — per track, in page order: (name, anchor id on the page, label, description, kind).
# kind "static": generated per run and rendered server-side, measured by its own visible text; "js": drawn in the browser from
# the page's data, measured by data-sec-items (the count the template hands the script); "fixed": template text by design,
# reported FIXED (covered, listed apart). Existing page ids are kept; new anchors are sec-<name>.
SECTIONS = {
    "oracle": (
        ("kpis", "sec-kpis", "Headline KPIs", "the hero's metric cards; their values are drawn by the counter script", "js"),
        ("integrity", "sec-integrity", "Data integrity", "how many of the page's numbers trace to the registry, under the hero's cards (BUILD B3)", "static"),
        ("rubric", "sec-rubric", "Rubric map", "the brief's scoring dimensions, each linked to the sections that answer it (BUILD B3)", "static"),
        ("summary", "summary", "01 Executive Summary", "the recommendation, the decision requested, the problem, the approach, today vs proposed", "static"),
        ("cost-of-inaction", "sec-cost-of-inaction", "Cost of inaction", "what waiting costs, computed in Python (inside 01)", "static"),
        ("evidence", "evidence", "02 Data Analysis", "the cited evidence cards", "static"),
        ("bottlenecks", "sec-bottlenecks", "Bottleneck dashboard", "per facility, listed vs in use vs free, who can use it now, and the jam points by record id — Python, from the records (inside 02; BUILD B3)", "static"),
        ("youth-handoffs", "sec-youth-handoffs", "Youth handoffs", "the age protocol at each age cliff and the handoff failure modes with their remedies (inside 02; BUILD B3)", "static"),
        ("operations", "operations", "03 Delivery Model", "the operating model and its workflow diagram", "static"),
        ("prioritization", "sec-prioritization", "Prioritization model", "the published table, the gates, the worked cases for the brief's people and the fairness check, ranked in Python from the data (inside 03)", "static"),   # BUILD B1
        ("financials", "financials", "04 Financial Model", "the base, optimistic and conservative P&L", "static"),
        ("use-of-funds", "sec-use-of-funds", "Use of funds", "every Year-1 line with its owner, release cadence and leading indicator, and the marketing reserve's allocation (inside 04)", "static"),   # BUILD B1
        ("sroi", "sec-sroi", "SROI derivation", "each monetised outcome with its proxy, quantity and source (inside 04; absent when the SROI rows are cleared)", "static"),
        ("simulator", "simPanel", "What-if simulator", "sliders that recompute cost per beneficiary, surplus and SROI (inside 04)", "js"),
        ("sensitivity", "sec-sensitivity", "Sensitivity analysis", "the stress test (inside 04)", "static"),
        ("sensitivity-grid", "sec-sensitivity-grid", "Sensitivity grid", "each assumption ±20% and what it moves, computed by the financial model (inside 04; BUILD B3)", "static"),
        ("plan", "plan", "05 Delivery Plan", "the implementation phases", "static"),
        ("kpi-framework", "sec-kpi-framework", "Pilot KPI framework", "leading and lagging indicators with baseline, target, day, cadence, owner and source, and the pilot's spend (inside 05; BUILD B3)", "static"),
        ("risks", "sec-risks", "Key risks and mitigations", "the risk register (inside 05)", "static"),
        ("renewal", "sec-renewal", "Renewal / sustainability", "the renewal or sustainability case (inside 05)", "static"),
        ("renewal-gates", "sec-renewal-gates", "Renewal gates", "X of Y indicators at target by Day N, each indicator's evidence tier (inside 05; BUILD B3)", "static"),
        ("honesty", "sec-honesty", "What could prove us wrong", "the strongest evidence, the weakest assumptions, the thesis and its tripwire, the judge questions (06; BUILD B3)", "static"),
        ("data-sources", "sec-data-sources", "Data sources", "the API endpoints and their record counts (07 Appendix)", "static"),
        ("methodology", "sec-methodology", "Methodology", "how the analysis was done (07 Appendix)", "static"),
        ("ai-process", "sec-ai-process", "AI process", "how AI was used (07 Appendix)", "static"),
        ("assumptions", "sec-assumptions", "Key assumptions", "the stated assumptions (07 Appendix)", "static"),
        ("registry", "sec-registry", "The registry", "every figure on the page with its kind, its source or formula and its assumption (07 Appendix; BUILD B3)", "static"),
    ),
    "architect": (    # BUILD B2 (D8): the Get Help tab's five sections come first (the tab opens first); each renders only when the sidecar has it
        ("intake", "sec-intake", "Get Help: the intake", "the questions, from the engine sidecar's intake, and the presets for the brief's people (drawn by the script)", "js"),
        ("plan", "sec-plan", "Get Help: Your Plan", "tonight, next and tomorrow for this person, ranked in the browser by the engine's published table, with each breakdown and trace (drawn by the script)", "js"),
        ("checklist", "sec-checklist", "Get Help: documents and benefits", "every document record in unlock order, with its days, fee, blockers and who helps", "static"),
        ("navigator", "sec-navigator", "Get Help: youth transition navigator", "every transition program with its age window, intake lag, free slots and eligibility text", "static"),
        ("readiness", "sec-readiness", "Get Help: housing readiness tracker", "every rental property with its screening barriers word for word, ranked for the readiness steps checked", "static"),
        ("dashboard", "panel-dashboard", "Dashboard tab", "crisis KPIs, area scores and crisis events, drawn from the data", "js"),
        ("how-built", "sec-how-built", "How this tool was built", "the fixed process description (inside the Dashboard tab)", "fixed"),
        ("who-serves", "sec-who-serves", "Who this serves", "one sentence naming the users, chosen from the detected domain (inside how-built)", "static"),
        ("finder", "panel-finder", "Find Resources tab", "the searchable, filterable resource cards, drawn from the data", "js"),
        ("data", "panel-data", "Data Explorer tab", "the per-area table, charts and data sources, drawn from the data", "js"),
    ),
    "muse": (
        ("hero", "sec-hero", "Campaign headline", "the campaign headline, lead and headline stats", "static"),
        ("audiences", "sec-audiences", "01 Target Audiences", "the personas: barriers, channels, quotes and the data behind them", "static"),
        ("channels", "sec-channels", "02 Channel Strategy", "the channel matrix: audience, content, frequency, KPI, reach", "static"),
        ("calendar", "sec-calendar", "03 Launch Timeline", "the campaign phases and their milestones", "static"),
        ("creative", "sec-creative", "04 Creative Assets", "the social media posts", "static"),
        ("flyer", "print-flyer", "Bilingual flyer", "the printable English / Spanish community flyer", "static"),
        ("pitch", "sec-pitch", "Partner pitch", "the one-page partner recruitment pitch", "static"),
        ("kpis", "sec-kpis", "05 Success Metrics", "the campaign KPI targets", "static"),
        ("calculator", "sec-calculator", "06 Reach Calculator", "budget and partner sliders that recompute reach (drawn by the script)", "js"),
        ("synergy", "sec-synergy", "Full-stack synergy", "the fixed links to the other two tracks", "fixed"),
        ("how-built", "sec-how-built", "How this campaign was built", "the fixed process description", "fixed"),
        ("who-serves", "sec-who-serves", "Who this serves", "one generated sentence naming the audience (inside how-built)", "static"),
    ),
}
SCAN_BAD = ("api.anthropic.com", "x-api-key", "anthropic-dangerous-direct-browser-access", "CHATBOT_KEY", "CHATBOT_CONTEXT",
            "initChatbot", "data-chat-key", "sk-ant")        # strings the live page scan refuses (p011b_page_scan.py:12–13, 35)
MAP_FIELD_GUIDE = ("Coverage map: for every item of every track, list in \"sections\" the page sections (names from _catalogue) whose "
                   "content answers that item in substance; leave [] where nothing on the page answers it — it is reported as a gap, by id. "
                   "\"note\": one plain sentence that answers the item, shown on the page's \"How this answers the brief\" panel — the "
                   "Architect's only answer (it calls no model; write only what the tool really does); on the Oracle and Muse pages the "
                   "model writes the answer and a note shows only when the model gave none; null otherwise. users: for each named user, the "
                   "track and the sections built for that person (Rule 13). winning: each winning line's track and sections, as for a user. "
                   "Each entry may carry \"link\" (implements, supports or verifies) and \"condition\", its acceptance condition "
                   "(draft-conditions drafts them from the brief's words; the operator approves with confirm-conditions). Keys starting "
                   "with _ are notes. "
                   "Then: python -B naipc_requirements.py validate-map <this file>")


def catalogue(track):
    """name → {anchor, label, description, kind} for one track, in page order."""
    return {n: {"anchor": a, "label": l, "description": d, "kind": k} for n, a, l, d, k in SECTIONS[track]}


_PROGRESS = {"open": False}       # BUILD IC-7 (D8, G12): a progress line (the Oracle's dots) is open on the console


def progress_open():
    """BUILD IC-7 (D8): a harness printed part of a progress line (no newline yet)."""
    _PROGRESS["open"] = True


def progress_close():
    """BUILD IC-7 (D8, G12): end an open progress line with its newline — before any other line is printed. True when one was open."""
    if not _PROGRESS["open"]:
        return False
    _PROGRESS["open"] = False
    try:
        print("", flush=True)
    except Exception:  # noqa: BLE001
        pass
    return True


def progress_end():
    """BUILD IC-7 (D8): the progress line ended by its own last piece (a ✓ with its newline): closed, no newline added."""
    _PROGRESS["open"] = False


def say(line=""):
    """Print one line; never raises (a cp1252 stdout, a closed pipe). Brief and model text never goes through rich markup. BUILD IC-7
    (D8): an open progress line is closed first, so no line is printed inside it."""
    progress_close()
    s = str(line)
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(s.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:                                        # noqa: BLE001 — printing must never stop a harness
            pass
    except Exception:                                            # noqa: BLE001
        pass


# A tag is '<' or '</' followed directly by an HTML element's name (as HTML's own tokenizer starts one), never crossing another '<':
# a comparison sign in prose ("costs stay < 75% … SROI > 2.0") is text, not a tag (the P0.3 review found the first rule, "<[^>]*>",
# deleting everything between such a pair).
_TAG_NAMES = ("a|abbr|address|article|aside|audio|b|base|bdi|bdo|big|blockquote|body|br|button|canvas|caption|center|cite|code|col|"
              "colgroup|data|datalist|dd|del|details|dfn|dialog|div|dl|dt|em|embed|fieldset|figcaption|figure|font|footer|form|frame|"
              "frameset|h[1-6]|head|header|hr|html|i|iframe|img|input|ins|kbd|label|legend|li|link|main|map|mark|marquee|menu|meta|meter|"
              "nav|noscript|object|ol|optgroup|option|output|p|param|picture|pre|progress|q|rp|rt|ruby|s|samp|script|section|select|small|"
              "source|span|strike|strong|style|sub|summary|sup|svg|table|tbody|td|template|textarea|tfoot|th|thead|time|title|tr|track|tt|"
              "u|ul|var|video|wbr")
_BLOCK_TAG = re.compile(r"</?(?:br|p|div|li|ul|ol|tr|td|th|h[1-6]|table|section|blockquote)\b[^<>]*>", re.I)
_ANY_TAG = re.compile(r"<!--.*?-->|</?(?:" + _TAG_NAMES + r")\b[^<>]*>", re.I | re.S)


def clean_text(s):
    """Plain text for the panel, the Notes copy and the chat context (D8, D10): tags removed (block tags become a space), entities
    decoded, whitespace collapsed, and every string the live page scan refuses replaced by [removed]. Not text → ""."""
    if not isinstance(s, str):
        return ""
    t = html.unescape(_ANY_TAG.sub("", _BLOCK_TAG.sub(" ", s)))
    t = re.sub(r"\s+", " ", t).strip()
    for b in SCAN_BAD:
        t = re.sub(re.escape(b), "[removed]", t, flags=re.I)
    return t


# ── the map file (D3) ──

def map_path_for(contract_path):
    p = Path(contract_path)
    return p.with_name(p.stem + ".map.json")


def contract_path_for(map_path):
    p = Path(map_path)
    stem = p.name[:-len(".map.json")] if p.name.endswith(".map.json") else p.stem
    return p.with_name(stem + ".json")


def _entry_problems(v, track, where, with_note):
    v = _strip(v)
    if not isinstance(v, dict):
        return [f"{where}: an object {{\"sections\": [...]" + (", \"note\": text or null}" if with_note else ", \"track\": ...}") + f", got {type(v).__name__}"]
    P = []
    allowed = ("sections", "note", "link", "condition") if with_note else ("track", "sections", "link", "condition")   # IC-4 (D15)
    for k in v:
        if k not in allowed:
            P.append(f"{where}.{k}: unknown key (allowed: {', '.join(allowed)})")
    if not with_note:
        if v.get("track") is not None and v.get("track") not in TRACKS:
            return P + [f"{where}.track: one of {', '.join(TRACKS)} (or null when sections is empty), got {v.get('track')!r}"]
        track = v.get("track")
    secs = v.get("sections", [])
    if not isinstance(secs, list) or not all(isinstance(s, str) for s in secs):
        P.append(f"{where}.sections: a list of section names")
    elif secs and track is None:
        P.append(f"{where}.track: required when sections are listed")
    elif secs:
        names = [n for n, *_ in SECTIONS[track]]
        for s in secs:
            if s not in names:
                P.append(f"{where}.sections: unknown section {s!r} — the {track} catalogue: {', '.join(names)}")
    if with_note and v.get("note") is not None and not _text(v.get("note")):
        P.append(f"{where}.note: text or null, got {v.get('note')!r}")
    return P


def validate_map(m, doc):
    """Every problem of a coverage map against its contract ('path: reason'); an empty list means valid. Unmapped items are gaps
    (map_gaps), not problems."""
    if not isinstance(m, dict):
        return ["map: the top level must be a JSON object"]
    d, P = _strip(m), []
    for k in d:
        if k not in ("schema_version", "challenge", "tracks", "users", "winning"):
            P.append(f"{k}: unknown top-level key (a map has schema_version, challenge, tracks, users, winning)")
    if d.get("schema_version") != SCHEMA_VERSION:
        P.append(f"schema_version: must be {SCHEMA_VERSION}, got {d.get('schema_version')!r}")
    cid = doc["challenge"]["id"]
    if d.get("challenge") != cid:
        P.append(f"challenge: {d.get('challenge')!r} differs from the contract beside it ({cid!r})")
    tr = _strip(d.get("tracks", {}))
    if not isinstance(tr, dict):
        P.append("tracks: an object (track → item id → {sections, note})")
        tr = {}
    for t in tr:
        if t not in TRACKS:
            P.append(f"tracks.{t}: unknown track (the tracks are exactly {', '.join(TRACKS)})")
    for t in TRACKS:
        x = _strip(tr.get(t, {}))
        if not isinstance(x, dict):
            P.append(f"tracks.{t}: an object (item id → {{sections, note}})")
            continue
        ids = [it["id"] for it in doc["tracks"][t]["items"]]
        for k, v in x.items():
            if k not in ids:
                P.append(f"tracks.{t}.{k}: not an item of the {t} track (its items: {', '.join(ids)})")
            else:
                P += _entry_problems(v, t, f"tracks.{t}.{k}", True)
                P += _condition_entry_problems(v, t, f"tracks.{t}.{k}", user_ids(doc))
    for key, known, label in (("users", user_ids(doc), "named user"), ("winning", winning_ids(doc), "winning line")):
        x = _strip(d.get(key, {}))
        if not isinstance(x, dict):
            P.append(f"{key}: an object (id → {{track, sections}})")
            continue
        for k, v in x.items():
            if k not in known:
                P.append(f"{key}.{k}: not a {label} of the contract" + (f" (its ids: {', '.join(known)})" if known else " (it names none)"))
            else:
                P += _entry_problems(v, None, f"{key}.{k}", False)
                if isinstance(_strip(v), dict):
                    P += _condition_entry_problems(v, _strip(v).get("track"), f"{key}.{k}", user_ids(doc))
    return P


def _condition_entry_problems(v, track, where, users):
    """BUILD IC-4 (D15): an entry's link type and acceptance condition ('path: reason' each; none when it carries neither)."""
    v, P = _strip(v), []
    if not isinstance(v, dict):
        return P
    if "link" in v and v["link"] not in LINK_TYPES:
        P.append(f"{where}.link: one of {', '.join(LINK_TYPES)}, got {v['link']!r}")
    if "condition" in v:
        if track not in TRACKS:
            P.append(f"{where}.condition: the entry needs its track (the page its condition is checked on)")
        else:
            P += condition_problems(v["condition"], track, f"{where}.condition", users)
            secs = [s for s in v["sections"] if isinstance(s, str)] if isinstance(v.get("sections"), list) else []
            P += [f"{where}.condition: names the section {s!r}, which is not one of the entry's sections ({', '.join(secs) or 'none'}) — a "
                  "condition is checked in a section the requirement is linked to" for s in dict.fromkeys(condition_sections(v["condition"]))
                  if s not in secs]
    return P


def condition_problems(c, track, where, users, depth=0):
    """BUILD IC-4 (D15): every problem of one acceptance condition ('path: reason'); an empty list means valid."""
    c = _strip(c)
    if not isinstance(c, dict) or c.get("kind") not in CONDITION_KEYS:
        return [f"{where}: an object whose kind is one of {', '.join(CONDITION_KEYS)}"]
    k, P = c["kind"], []
    P += [f"{where}.{x}: unknown key (a {k} condition has {', '.join(CONDITION_KEYS[k])})" for x in c if x not in CONDITION_KEYS[k]]
    if k in ("sentence", "table", "figure", "languages"):
        names = [n for n, *_ in SECTIONS[track]]
        if c.get("section") not in names:
            P.append(f"{where}.section: a section of the {track} catalogue ({', '.join(names)}), got {c.get('section')!r}")
    if k == "sentence":
        if "user" in c and c["user"] not in users:
            P.append(f"{where}.user: a named user of the contract ({', '.join(users) or 'none'}), got {c['user']!r}")
        if not (isinstance(c.get("phrases"), list) and c["phrases"] and all(_text(p_) for p_ in c["phrases"])):
            P.append(f"{where}.phrases: a list of the brief's need phrases (each non-empty text)")
    elif k == "table":
        if not (isinstance(c.get("columns"), list) and c["columns"] and all(_text(x) for x in c["columns"])):
            P.append(f"{where}.columns: a list of the table's column names (each non-empty text)")
    elif k == "figure":
        if not _text(c.get("figure")):
            P.append(f"{where}.figure: a registry id, or a kind ({', '.join(FIGURE_KINDS)})")
    elif k == "languages":       # BUILD IC-5 (D10): two languages or more, each a primary language subtag (en, es)
        lg = c.get("langs")
        if not (isinstance(lg, list) and len(lg) >= 2 and all(isinstance(x, str) and re.fullmatch(r"[a-z]{2,3}", x) for x in lg)
                and len(set(lg)) == len(lg)):
            P.append(f"{where}.langs: two languages or more, each a primary language subtag (en, es), none twice")
    elif k == "review":
        if not _text(c.get("why")):
            P.append(f"{where}.why: what the operator reviews (non-empty text)")
    else:
        ch = c.get("children")
        if not (isinstance(ch, list) and len(ch) >= 2):
            P.append(f"{where}.children: two or more conditions (a compound criterion, split into its parts)")
        elif depth >= 3:
            P.append(f"{where}.children: nested more than three deep")
        else:
            for i, x in enumerate(ch):
                P += condition_problems(x, track, f"{where}.children[{i}]", users, depth + 1)
    return P


def map_sections(cmap, track, item_id):
    """The sections the map names for one item (empty when unmapped or when there is no map)."""
    t = ((cmap or {}).get("tracks", {}) or {}).get(track, {}) if isinstance(cmap, dict) else {}
    e = t.get(item_id) if isinstance(t, dict) else None
    secs = e.get("sections") if isinstance(e, dict) else None
    return [s for s in secs if isinstance(s, str)] if isinstance(secs, list) else []


def map_note(cmap, track, item_id):
    t = ((cmap or {}).get("tracks", {}) or {}).get(track, {}) if isinstance(cmap, dict) else {}
    e = t.get(item_id) if isinstance(t, dict) else None
    return e.get("note") if isinstance(e, dict) and _text(e.get("note")) else None


def map_gaps(cmap, doc):
    """Unmapped item ids per track, unmapped user ids, unmapped winning ids."""
    items = {t: [it["id"] for it in doc["tracks"][t]["items"] if not map_sections(cmap, t, it["id"])] for t in TRACKS}
    def unmapped(key, ids):
        x = (cmap or {}).get(key, {}) or {}
        return [i for i in ids if not (isinstance(x.get(i), dict) and x[i].get("sections"))]
    return {"items": items, "users": unmapped("users", user_ids(doc)), "winning": unmapped("winning", winning_ids(doc))}


def load_map(path, doc):
    """(map, problems): a map that does not parse or does not validate comes back with its problems; never raises."""
    p = Path(path)
    if not p.is_file():
        return None, [f"map: not found: {p}"]
    m, problems = parse(p.read_bytes(), "map")
    if problems:
        return None, problems
    return m, validate_map(m, doc)


def map_for_check(contract_path, doc):
    """(map to use or None, problems, the missing map's file name or None) for a coverage check. A map that is not there is its own
    state — every item UNMAPPED, the remedy map-template — not an invalid map (the P0.3 review); an invalid map is not used."""
    mpath = map_path_for(contract_path)
    if not mpath.is_file():
        return None, [], mpath.name
    cmap, problems = load_map(mpath, doc)
    return (None if problems else cmap), problems, None


def map_template(doc):
    """A map skeleton for a contract: every item with "sections": [] and "note": null, the catalogue and the field guide (DR#1: a
    skeleton without a field guide cannot be filled on the day)."""
    out = {"schema_version": SCHEMA_VERSION, "_note": MAP_FIELD_GUIDE, "challenge": doc["challenge"]["id"],
           "_catalogue": {t: [f"{n} — {l} — {d} [{k}]" for n, a, l, d, k in SECTIONS[t]] for t in TRACKS}, "tracks": {}}
    for t in TRACKS:
        out["tracks"][t] = {it["id"]: {"_brief": it["text"], "sections": [], "note": None} for it in doc["tracks"][t]["items"]}
    out["users"] = {u["id"]: {"_who": f"{u.get('name')}" + (f", {u['age']}" if u.get("age") is not None else "") + f" — {u.get('role')}",
                              "track": None, "sections": []} for u in doc.get("named_users") or []}
    out["winning"] = {w["id"]: {"_brief": w["text"], "track": None, "sections": []} for w in doc.get("winning_looks_like") or []}
    return out


def map_summary(cmap, doc):
    g = map_gaps(cmap, doc)
    parts = [f"{t} {len(doc['tracks'][t]['items']) - len(g['items'][t])} of {len(doc['tracks'][t]['items'])} items mapped" for t in TRACKS]
    nu, nw = len(user_ids(doc)), len(winning_ids(doc))
    return " · ".join(parts) + f" · users {nu - len(g['users'])} of {nu} · winning {nw - len(g['winning'])} of {nw}", g


# ── what a track page carries (D4, D8) ──

_RUN_ID = []


def run_id():
    """NAIPC_RUN_ID from pipeline.py, else this process's own UTC stamp, taken once (the Oracle renders twice in one process)."""
    env = os.environ.get("NAIPC_RUN_ID", "").strip()
    if env:
        return env
    if not _RUN_ID:
        _RUN_ID.append(datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return _RUN_ID[0]


def _attr(name, value):
    return f' {name}="{html.escape(value, quote=True)}"'


def section_attrs(doc, cmap, track):
    """section name → ' data-req="…" data-user="…"' (only the sections the map names for this track's items and users)."""
    req, usr = {}, {}
    for it in doc["tracks"][track]["items"]:
        for s in map_sections(cmap, track, it["id"]):
            req.setdefault(s, []).append(it["id"])
    for uid, e in (((cmap or {}).get("users") or {}).items()):
        if isinstance(e, dict) and e.get("track") == track and isinstance(e.get("sections"), list):
            for s in e["sections"]:
                usr.setdefault(s, []).append(uid)
    out = {}
    for n, *_ in SECTIONS[track]:
        a = (_attr("data-req", " ".join(req[n])) if n in req else "") + (_attr("data-user", " ".join(usr[n])) if n in usr else "")
        if a:
            out[n] = a
    return out


def panel_rows(doc, cmap, track, answers, present=None):
    """One row per item of the track, contract order (D8): the brief's words, the answer as plain text (the model's for the Oracle and
    Muse, else the map's note, else "—"), and links to the mapped sections that are on the page (present = their names; None = all)."""
    cat, kind, rows = catalogue(track), doc["tracks"][track]["kind"], []
    for it in doc["tracks"][track]["items"]:
        links = [{"name": s, "anchor": cat[s]["anchor"], "label": cat[s]["label"]} for s in map_sections(cmap, track, it["id"])
                 if s in cat and (present is None or s in present)]
        model = clean_text((answers or {}).get(it["id"], "")) if track in ANSWER_TRACKS else ""
        note = clean_text(map_note(cmap, track, it["id"]) or "")
        answer, src = (model, "model") if model else ((note, "note") if note else ("—", "none"))
        rows.append({"id": it["id"], "text": clean_text(it["text"]), "source": item_source(it, kind), "answer": answer,
                     "answer_src": src, "links": links})
    return rows


def page_context(track, answers=None, present=None):
    """Everything a track template needs for P0.3, from the contract pipeline.py exported (NAIPC_REQUIREMENTS) and the map beside
    it. Never raises: without a usable contract the page carries no data-req/data-user, no metas and no panel (D4)."""
    off = {"enabled": False, "attrs": {}, "panel": None}
    if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
        return dict(off, reason="no contract (NAIPC_REQUIREMENTS not set)")
    try:
        doc = from_env()
    except RequirementsError as e:
        return dict(off, reason=f"the contract is not usable: {e.problems[0] if e.problems else 'unknown problem'}")
    except Exception as e:                                       # noqa: BLE001
        return dict(off, reason=f"the contract could not be read: {type(e).__name__}: {e}")
    try:
        cmap, mprobs, _ = map_for_check(os.environ["NAIPC_REQUIREMENTS"].strip(), doc)  # a missing or invalid map is not used; the gate says which
        rows = panel_rows(doc, cmap, track, answers or {}, present)
        shown = any(r["answer_src"] != "none" or r["links"] for r in rows)
        t = doc["tracks"][track]
        return {"enabled": True, "challenge": doc["challenge"]["id"], "run_id": run_id(), "attrs": section_attrs(doc, cmap, track),
                "panel": {"title": "How this answers the brief", "track_title": clean_text(t["title"]), "mission": clean_text(t["mission"]),
                          "rows": rows} if shown else None,
                "map_problems": mprobs}
    except Exception as e:                                       # noqa: BLE001 — a page is never lost to the coverage code
        return dict(off, reason=f"page context failed: {type(e).__name__}: {e}")


# ── the checker (D5): html.parser, standard library ──

VOID = frozenset("area base br col embed hr img input keygen link meta param source track wbr".split())
EXCLUDED = frozenset(("script", "style", "template", "h1", "h2", "h3", "h4", "h5", "h6", "summary", "thead"))
P_CLOSERS = frozenset("address article aside blockquote details dialog div dl fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 "
                      "header hgroup hr main menu nav ol p pre section table ul".split())
P_SCOPE = frozenset("applet button caption html marquee object table td template th".split())
OPTIONAL_END = {"li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr"}, "td": {"td", "th", "tr"}, "th": {"td", "th", "tr"},
                "option": {"option", "optgroup"}}


class _Page(HTMLParser):
    """One pass over a page: the data-sec blocks and their own visible text, the metas, every id, the panel's rows and links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.secs, self.metas, self.ids, self.rows, self.links = [], [], {}, [], [], []
        self._uid = 0          # BUILD IC-4 (D15): a paragraph, list item or table cell's own number — its text is one unit

    def _pop_to(self, i):
        del self.stack[i:]

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "meta" and a.get("name") in ("naipc-challenge", "naipc-run"):
            self.metas[a["name"]] = a.get("content", "")
        if a.get("id"):
            self.ids.append(a["id"])
        while self.stack and self.stack[-1]["tag"] in OPTIONAL_END and tag in OPTIONAL_END[self.stack[-1]["tag"]]:
            self.stack.pop()                                     # an implied end tag (li, dt/dd, tr, td/th, option)
        if tag in P_CLOSERS:                                     # an open <p> in scope closes before a block element
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i]["tag"] in P_SCOPE:
                    break
                if self.stack[i]["tag"] == "p":
                    self._pop_to(i)
                    break
        if tag == "a" and "data-brief-link" in a:
            self.links.append({"name": a.get("data-brief-link", ""), "href": a.get("href", "")})
        if tag in VOID:
            return
        el = {"tag": tag, "sec": None, "skip": False, "row": None, "cell": None, "label": None, "table": None, "th": None, "uid": None,
              "lang": (a.get("lang") or "").strip().lower() or None}          # BUILD IC-5 (D10): an element's own language
        if tag == "a" and "data-brief-link" in a:                # a panel link: its words are one label of the row's "l" cell
            row, cell, _ = self._row_cell()
            if row is not None and cell == "l":
                self.rows[row]["labels"].append([])
                el["label"] = len(self.rows[row]["labels"]) - 1
        if "data-sec" in a:
            items = a.get("data-sec-items")
            self.secs.append({"name": a.get("data-sec", ""), "id": a.get("id", ""), "req": a.get("data-req", "").split(),
                              "user": a.get("data-user", "").split(), "items": int(items) if re.fullmatch(r"-?[0-9]+", (items or "").strip()) else None,
                              "text": [], "units": {}, "tables": [], "provs": [], "kinds": [], "langs": {}})
            el["sec"] = len(self.secs) - 1
        elif tag in EXCLUDED or "data-cov-skip" in a or "data-brief-panel" in a:
            el["skip"] = True
        s_ = el["sec"] if el["sec"] is not None else next((x["sec"] for x in reversed(self.stack) if x["sec"] is not None), None)
        if s_ is not None:             # BUILD IC-4 (D15): what a condition reads in its section
            if tag == "table":
                self.secs[s_]["tables"].append([])
                el["table"] = (s_, len(self.secs[s_]["tables"]) - 1)
            elif tag == "th":
                tb = next((x["table"] for x in reversed(self.stack) if x["table"] is not None), None)
                if tb is not None:
                    self.secs[tb[0]]["tables"][tb[1]].append([])
                    el["th"] = (tb[0], tb[1], len(self.secs[tb[0]]["tables"][tb[1]]) - 1)
            if a.get("data-prov"):
                self.secs[s_]["provs"].append(a["data-prov"])
            if a.get("data-kind"):
                self.secs[s_]["kinds"].append(a["data-kind"])
            if tag in UNIT_TAGS:
                self._uid += 1
                el["uid"] = self._uid
        if "data-brief-row" in a:
            self.rows.append({"id": a.get("data-brief-row", ""), "answer_src": a.get("data-brief-answer", ""), "q": [], "a": [], "l": [], "labels": []})
            el["row"] = len(self.rows) - 1
        if "data-brief-cell" in a:
            el["cell"] = a.get("data-brief-cell", "")
        self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                self._pop_to(i)
                return                                           # an end tag with no open element is ignored

    def _row_cell(self):
        """(row, cell, label) around the current position: the innermost panel row, its cell, and the panel link inside that cell."""
        row = cell = lab = None
        for el in reversed(self.stack):
            if cell is None and lab is None and el["label"] is not None:
                lab = el["label"]
            if cell is None and el["cell"]:
                cell = el["cell"]
            if el["row"] is not None:
                row = el["row"]
                break
        return row, cell, lab

    def handle_data(self, data):
        row, cell, lab = self._row_cell()                        # the panel's cells (they are never section text)
        if row is not None and cell == "l" and lab is not None:
            self.rows[row]["labels"][lab].append(data)
        elif row is not None and cell in ("q", "a", "l"):
            self.rows[row][cell].append(data)
        th = next((x["th"] for x in reversed(self.stack) if x["th"] is not None), None)
        if th is not None:                                       # BUILD IC-4 (D15): a header cell's words, whether or not they count as text
            self.secs[th[0]]["tables"][th[1]][th[2]].append(data)
        if data.strip().startswith(PENDING):
            return
        for el in reversed(self.stack):                          # the innermost block owns the text; an excluded element hides it
            if el["skip"]:
                return
            if el["sec"] is not None:
                self.secs[el["sec"]]["text"].append(data)
                lg = None                    # BUILD IC-5 (D10): the innermost lang inside the section (its own element's included)
                for x in reversed(self.stack):
                    if x.get("lang"):
                        lg = x["lang"]
                        break
                    if x is el:
                        break
                if lg:
                    self.secs[el["sec"]]["langs"].setdefault(lg, []).append(data)
                u = next((x["uid"] for x in reversed(self.stack) if x["uid"] is not None), None)
                if u is not None and not any(x["tag"] in LABEL_TAGS for x in self.stack):    # BUILD IC-4 (D15): the text of its
                    self.secs[el["sec"]]["units"].setdefault(u, []).append(data)             # paragraph, list item or cell; a heading's
                return                                                                       # or a header cell's is a label


def _collapse(parts):
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _links_cell(r):
    """A row's "Where" cell as plain text: its link labels, '; ' between them (the page shows one per line); without links, its text."""
    labels = [x for x in (_collapse(p) for p in r["labels"]) if x]
    return "; ".join(labels) if labels else _collapse(r["l"])


def parse_page(page_html):
    """The page's blocks (name → merged record), metas, ids, panel rows and links. Never raises on malformed markup: where the
    standard library's parser gives up (Python 3.11's HTMLParser raises on a marked section it does not know, e.g. '<![ x'), what was
    read before that point is kept and the stop is returned under "error" (the gate prints it)."""
    pp, err = _Page(), None
    try:
        pp.feed(page_html.replace("\r\n", "\n").replace("\r", "\n"))
        pp.close()
    except Exception as e:                                       # noqa: BLE001 — a page is measured as far as it can be read
        err = f"{type(e).__name__}: {e}"
    blocks = {}
    for s in pp.secs:
        b = blocks.setdefault(s["name"], {"name": s["name"], "count": 0, "ids": [], "req": [], "user": [], "items": None, "text": "",
                                          "units": [], "tables": [], "provs": [], "kinds": [], "langs": {}})   # IC-4 (D15); IC-5 (D10)
        b["count"] += 1
        b["ids"].append(s["id"])
        b["req"] += [x for x in s["req"] if x not in b["req"]]
        b["user"] += [x for x in s["user"] if x not in b["user"]]
        if s["items"] is not None:
            b["items"] = (b["items"] or 0) + s["items"]
        b["text"] = (b["text"] + " " + _collapse(s["text"])).strip()
        b["units"] += [u for u in (_collapse(x) for x in s["units"].values()) if u]
        b["tables"] += [[_collapse(h) for h in t] for t in s["tables"]]
        b["provs"] += [x for x in s["provs"] if x not in b["provs"]]
        b["kinds"] += [x for x in s["kinds"] if x not in b["kinds"]]
        for lg, parts in s["langs"].items():         # BUILD IC-5 (D10): each language's text in the section
            b["langs"][lg] = (b["langs"].get(lg, "") + " " + _collapse(parts)).strip()
    rows = [{"id": r["id"], "answer_src": r["answer_src"], "q": _collapse(r["q"]), "a": _collapse(r["a"]), "l": _links_cell(r)} for r in pp.rows]
    return {"blocks": blocks, "metas": pp.metas, "ids": pp.ids, "rows": rows, "links": pp.links, "error": err}


def section_status(kind, block):
    if block is None:
        return "MISSING"
    if kind == "fixed":
        return "FIXED"
    if kind == "js":
        return "OK" if (block["items"] or 0) >= 1 else "EMPTY"
    n = len(block["text"])
    return "OK" if n >= OK_CHARS else "THIN" if n >= THIN_CHARS else "EMPTY"


def coverage_check(doc, cmap, track, page_html, run_id=None, fetch_errors=None):
    """The coverage of one track page against the contract and its map (D5). Statuses per item: OK, THIN, FIXED, EMPTY, MISSING,
    UNMAPPED, or STALE (the page's metas are absent or name another challenge or run — then nothing counts as covered)."""
    pg = parse_page(page_html)
    cat, blocks = catalogue(track), pg["blocks"]
    sections = []
    for n, a, l, d, k in SECTIONS[track]:
        b = blocks.get(n)
        sections.append({"name": n, "kind": k, "present": b is not None, "count": b["count"] if b else 0,
                         "status": section_status(k, b), "chars": len(b["text"]) if b else 0, "items": b["items"] if b else None})
    by_name = {s["name"]: s for s in sections}
    cid = doc["challenge"]["id"]
    stale = []
    if "naipc-challenge" not in pg["metas"] or "naipc-run" not in pg["metas"]:
        stale.append("the page has no naipc-challenge / naipc-run metas (rendered without a contract, or by older code)")
    else:
        if pg["metas"]["naipc-challenge"] != cid:
            stale.append(f"the page is for challenge {pg['metas']['naipc-challenge']!r}, not {cid!r}")
        if run_id and pg["metas"]["naipc-run"] != run_id:
            stale.append(f"the page is from run {pg['metas']['naipc-run']!r}, not this run {run_id!r}")
    rows = {r["id"]: r for r in pg["rows"]}
    known_items = set(item_ids(doc))
    unknown = sorted({i for b in blocks.values() for i in b["req"] if i not in known_items} |
                     {u for b in blocks.values() for u in b["user"] if u not in set(user_ids(doc))})
    items, kind = [], doc["tracks"][track]["kind"]
    for it in doc["tracks"][track]["items"]:
        mapped = map_sections(cmap, track, it["id"])
        secs = []
        for s in mapped:
            b, info = blocks.get(s), by_name.get(s)
            st = "MISSING" if (b is None or info is None or it["id"] not in b["req"]) else info["status"]
            secs.append({"name": s, "status": st, "chars": info["chars"] if info else 0, "items": info["items"] if info else None,
                         "why": None if st != "MISSING" else ("not on the page" if b is None else "the block does not carry this item")})
        best = min((x["status"] for x in secs), key=STATUS_RANK.index) if secs else "UNMAPPED"
        status = "STALE" if stale else best
        answer = None
        if track in ANSWER_TRACKS:
            r = rows.get(it["id"])
            answer = "OK" if (r and r["answer_src"] == "model" and r["a"] and r["a"] != "—") else "NONE"
        covered = status in COVERED and answer != "NONE"
        if covered:
            reason = None
        elif status == "STALE":
            reason = "STALE: " + "; ".join(stale)
        elif status == "UNMAPPED":
            reason = "UNMAPPED: no section in the map"
        elif status in ("MISSING", "EMPTY"):
            reason = status + ": " + ", ".join(f"{x['name']} {x['status']}" + (f" ({x['why']})" if x["why"] else f" ({x['chars']} chars)" if x["status"] == "EMPTY" else "") for x in secs)
        else:
            reason = "no answer from the model"
        items.append({"id": it["id"], "text": it["text"], "source": item_source(it, kind), "sections": secs, "status": status,
                      "answer": answer, "covered": covered, "reason": reason})
    users = []
    for u in doc.get("named_users") or []:
        found = [b["name"] for b in blocks.values() if u["id"] in b["user"] and by_name.get(b["name"], {}).get("status") in COVERED]
        users.append({"id": u["id"], "name": u.get("name"), "found_in": found})
    winning = []
    for w in doc.get("winning_looks_like") or []:
        e = ((cmap or {}).get("winning") or {}).get(w["id"])
        if isinstance(e, dict) and e.get("track") == track:
            winning.append({"id": w["id"], "text": w["text"], "sections": {s: by_name.get(s, {}).get("status", "MISSING") for s in e.get("sections") or []}})
    ids = pg["ids"]
    dead = sorted({l["href"] for l in pg["links"] if not (l["href"].startswith("#") and l["href"][1:] in ids)})
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    c = {k: sum(1 for x in items if x["status"] == k) for k in ("OK", "THIN", "FIXED", "EMPTY", "MISSING", "UNMAPPED", "STALE")}
    counts = {"items": len(items), "ok": c["OK"], "thin": c["THIN"], "fixed": c["FIXED"], "empty": c["EMPTY"], "missing": c["MISSING"],
              "unmapped": c["UNMAPPED"], "stale": c["STALE"], "covered": sum(1 for x in items if x["covered"]),
              "answers": sum(1 for x in items if x["answer"] == "OK") if track in ANSWER_TRACKS else None}
    reqs, reverse, conds = [], [], has_conditions(cmap)
    if conds:          # BUILD IC-4 (D15): the forward check (each requirement by its condition) and the reverse check
        names = {u["id"]: u.get("name") or u["id"] for u in doc.get("named_users") or []}
        echoes = echo_words(doc)         # the brief's own words: quoted on the page, they are not its answer
        for key, kind_, rid, words, t, e in requirements_of(doc, cmap):
            if t != track:
                continue
            c = e.get("condition")
            carry = ("req", rid) if kind_ == "item" else ("user", rid) if kind_ == "user" else None    # a winning line: the map's link
            if not isinstance(c, dict):
                r = {"kind": None, "status": "FAIL", "why": "no acceptance condition"}
            elif stale:                          # a page of another challenge or run meets nothing (its items count as covered nowhere)
                r = {"kind": c.get("kind"), "status": "FAIL", "why": "STALE: " + "; ".join(stale)}
            else:
                r = evaluate_condition(c, blocks, names, carry, echoes)
            reqs.append({"key": key, "kind": kind_, "id": rid, "text": words, "link": e.get("link"), "status": r["status"], "condition": r})
            linked = [s for s in e.get("sections") or [] if isinstance(s, str)] + condition_sections(c)
            reverse += [f"{key}: it is linked to the section {s!r}, which is not on this page" for s in dict.fromkeys(linked) if s not in blocks]
        reverse += [f"a section of the page names {i!r}, which is no requirement of the contract" for i in unknown]
        counts.update(req_total=len(reqs), req_met=sum(1 for r in reqs if r["status"] == "PASS"),
                      req_review=sum(1 for r in reqs if r["status"] == "REVIEW"), req_failing=sum(1 for r in reqs if r["status"] == "FAIL"),
                      reverse=len(reverse))
    return {"schema_version": SCHEMA_VERSION, "challenge": cid, "track": track, "run_id": run_id, "page_metas": pg["metas"],
            "stale": stale, "items": items, "sections": sections, "unknown_ids": unknown, "users": users, "winning": winning,
            **({"conditions": True, "requirements": reqs, "reverse": reverse} if conds else {}),
            "fetch_errors": list(fetch_errors or []), "dead_links": dead, "duplicate_ids": dup_ids,
            "duplicate_blocks": sorted(n for n, b in blocks.items() if b["count"] > 1),
            "foreign_blocks": sorted(n for n in blocks if n not in cat), "panel_rows": pg["rows"], "parse_error": pg["error"], "counts": counts}


def has_conditions(cmap):
    """BUILD IC-4 (D15): whether a map carries acceptance conditions — then each requirement is checked by its condition, not its link."""
    if not isinstance(cmap, dict):
        return False
    ents = [e for t in (cmap.get("tracks") or {}).values() if isinstance(t, dict) for e in t.values()]
    ents += list((cmap.get("users") or {}).values()) + list((cmap.get("winning") or {}).values())
    return any(isinstance(e, dict) and isinstance(e.get("condition"), dict) for e in ents)


def requirements_of(doc, cmap):
    """BUILD IC-4 (D15): every requirement of the contract with its map entry — [(key, kind, id, the brief's words, track or None, entry)]:
    each item of each track, each named user, each winning line."""
    def entry(*path):
        x = cmap if isinstance(cmap, dict) else {}
        for p_ in path:
            x = x.get(p_) if isinstance(x, dict) else None
        return _strip(x) if isinstance(x, dict) else {}
    out = [(f"tracks.{t}.{it['id']}", "item", it["id"], it["text"], t, entry("tracks", t, it["id"])) for t in TRACKS for it in doc["tracks"][t]["items"]]
    for u in doc.get("named_users") or []:
        e = entry("users", u["id"])
        out.append((f"users.{u['id']}", "user", u["id"], f"{u.get('name')} — {u.get('role')}: \u201c{u.get('quote')}\u201d",
                    e.get("track") if e.get("track") in TRACKS else None, e))
    for w in doc.get("winning_looks_like") or []:
        e = entry("winning", w["id"])
        out.append((f"winning.{w['id']}", "winning", w["id"], w["text"], e.get("track") if e.get("track") in TRACKS else None, e))
    return out


def condition_sections(c):
    """The sections an acceptance condition names (its children's too)."""
    if not isinstance(c, dict):
        return []
    if c.get("kind") == "all":
        return [s for x in c.get("children") or [] for s in condition_sections(x)]
    return [c["section"]] if isinstance(c.get("section"), str) else []


_SENT_END = re.compile(r"(?<=[.!?])\s+")
_LANG_WORD = re.compile(r"[^\W\d_]+")       # BUILD IC-5 (D10): a word of any script ("tenía" one word)
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*")
ECHO_RUN = 6        # BUILD IC-4 (D15): six or more of the brief's words in their order are the brief speaking, not the page


def echo_words(doc):
    """BUILD IC-4 (D15): the brief's own words a page may quote — each named user's quote and each winning line, as lower-case words."""
    texts = [u.get("quote") for u in doc.get("named_users") or []] + [w.get("text") for w in doc.get("winning_looks_like") or []]
    return [[w.lower() for w in _WORD.findall(str(t).replace("\u2019", "'"))] for t in texts if isinstance(t, str) and t.strip()]


def _unecho(text, echoes, n=ECHO_RUN):
    """The text with every run of n or more words of one of the brief's texts, in that text's order, taken out."""
    t = str(text).replace("\u2019", "'")
    toks = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(t)]
    cut = [False] * len(toks)
    for q in echoes:
        grams = {tuple(q[i:i + n]) for i in range(len(q) - n + 1)}
        for i in range(len(toks) - n + 1):
            if tuple(x[2] for x in toks[i:i + n]) in grams:
                cut[i:i + n] = [True] * n
    if not any(cut):
        return t
    out, last = [], 0
    for (s0, e0, _w), c_ in zip(toks, cut):
        if c_:
            out.append(t[last:s0])
            last = e0
    return "".join(out) + t[last:]


def _has_words(words, text):
    """Whether the text holds the words as words (letter case aside; no letter or digit right before or after)."""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(str(words).strip()) + r"(?![A-Za-z0-9])", text, re.I) is not None


def _col(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def evaluate_condition(c, blocks, names, carry=None, echoes=()):
    """BUILD IC-4 (D15): one acceptance condition on a parsed page's blocks → {kind, status: PASS | REVIEW | FAIL, why[, children]}.
    sentence: a sentence of the section — read one paragraph, list item or table cell at a time — naming the user (by the contract's
    name), when the condition names one, and one of the brief's need phrases; table: a table of the section whose header cells hold every
    named column; figure: a registry figure of the named id, or of the named kind, in the section; review: never PASS; all: PASS only when
    every child passes, FAIL when one fails. carry: (attribute, id) — the section counts only when it carries the requirement's id (an
    item's data-req, a user's data-user); None for a winning line (its link is the map's sections). echoes: the brief's own words
    (echo_words) — a run of ECHO_RUN or more of them, in order, is taken out of a unit before its sentences are read."""
    k = c.get("kind") if isinstance(c, dict) else None
    if k == "review":
        return {"kind": k, "status": "REVIEW", "why": "needs review: " + str(c.get("why") or "")}
    if k == "all":
        ch = [evaluate_condition(x, blocks, names, carry, echoes) for x in c.get("children") or []]
        st = ("FAIL" if not ch or any(x["status"] == "FAIL" for x in ch) else "REVIEW" if any(x["status"] == "REVIEW" for x in ch) else "PASS")
        return {"kind": k, "status": st, "why": f"{sum(1 for x in ch if x['status'] == 'PASS')} of {len(ch)} parts met"
                + "".join(f"; part {i + 1} {x['status']}: {x['why']}" for i, x in enumerate(ch) if x["status"] != "PASS"), "children": ch}
    if k not in ("sentence", "table", "figure", "languages"):
        return {"kind": k, "status": "FAIL", "why": "not a condition"}
    b = blocks.get(c.get("section"))
    if b is None:
        return {"kind": k, "status": "FAIL", "why": f"the section {c.get('section')!r} is not on the page"}
    if carry and carry[1] not in (b.get(carry[0]) or []):
        return {"kind": k, "status": "FAIL", "why": f"the section {c.get('section')!r} does not carry {carry[1]!r} (its data-{carry[0]}): "
                                                   "the page was built from another map"}
    if k == "languages":        # BUILD IC-5 (D10): each language's part holds its own sentence (three words or more)
        parts = b.get("langs") or {}
        miss = [lg for lg in c.get("langs") or []
                if not any(len(_LANG_WORD.findall(s)) >= 3 for t_, v in parts.items() if t_ == lg or t_.startswith(lg + "-")
                           for s in _SENT_END.split(v))]
        if miss:
            return {"kind": k, "status": "FAIL", "why": f"the section {c['section']!r} holds no part in " + " or ".join(f'lang="{m}"' for m in miss)
                    + " with a sentence of its own (three words or more)"}
        return {"kind": k, "status": "PASS", "why": f"the section {c['section']!r} holds a part in each of " + ", ".join(c["langs"])}
    if k == "sentence":
        who = names.get(c.get("user"), c.get("user")) if c.get("user") else None
        ph = [str(p_) for p_ in c.get("phrases") or []]
        sents = [s.strip() for u in b.get("units") or [] for s in _SENT_END.split(_unecho(u, echoes)) if re.search(r"[A-Za-z0-9]", s)]
        if not sents:
            return {"kind": k, "status": "FAIL", "why": f"the section {c['section']!r} holds no sentence"}
        hit = next((s for s in sents if (who is None or _has_words(who, s)) and any(_has_words(p_, s) for p_ in ph)), None)
        if hit:
            return {"kind": k, "status": "PASS", "why": f"\u201c{hit[:160]}\u201d"}
        named = who is not None and any(_has_words(who, s) for s in sents)
        need = "; ".join(ph)
        return {"kind": k, "status": "FAIL", "why": (f"{who} is named in {c['section']!r}, but in no sentence with one of: {need}" if named else
                                                     f"no sentence of {c['section']!r} names {who}" if who else
                                                     f"no sentence of {c['section']!r} holds one of: {need}")}
    if k == "table":
        want = [_col(x) for x in c.get("columns") or []]
        for t in b.get("tables") or []:
            hs = [_col(h) for h in t]
            if all(any(h == w or h.startswith(w + " ") for h in hs) for w in want):
                return {"kind": k, "status": "PASS", "why": f"a table of {c['section']!r} with the columns: " + "; ".join(c["columns"])}
        return {"kind": k, "status": "FAIL", "why": f"no table of {c['section']!r} has the columns: " + "; ".join(c.get("columns") or [])}
    f = str(c.get("figure") or "")
    ok = f in (b.get("provs") or []) or (f.upper() in FIGURE_KINDS and f.upper() in (b.get("kinds") or []))
    return {"kind": k, "status": "PASS" if ok else "FAIL",
            "why": (f"{'the figure' if f.upper() not in FIGURE_KINDS else 'a figure of kind'} {f} in {c['section']!r}"
                    if ok else f"no {'figure ' + f if f.upper() not in FIGURE_KINDS else 'figure of kind ' + f} in {c['section']!r}")}


def gate_lines(res):
    """The gate's printed block (ASCII apart from the brief's own words and, under D15, the map's)."""
    c = res["counts"]
    ans = f"; answers {c['answers']}/{c['items']}" if c["answers"] is not None else ""
    out = [f"coverage ({res['track']}) {res['challenge']} run {res['run_id']}: {c['items']} items - {c['ok']} ok, {c['thin']} thin, "
           f"{c['fixed']} fixed, {c['empty']} empty, {c['missing']} missing, {c['unmapped']} unmapped" + (f", {c['stale']} stale" if c["stale"] else "")
           + f"{ans}; covered {c['covered']}/{c['items']}"]
    if res.get("parse_error"):
        out.append(f"  page: the parser stopped early - {res['parse_error'][:160]} (blocks after that point read MISSING)")
    for x in res["items"]:
        if not x["covered"]:
            out.append(f"  gap {x['id']}: {x['reason']} -- \"{x['text']}\"")
    if res["users"]:
        got = [u["id"] for u in res["users"] if u["found_in"]]
        out.append(f"  named users on this page: {len(got)} of {len(res['users'])}" + (f" ({', '.join(got)})" if got else ""))
    if res.get("conditions"):          # BUILD IC-4 (D15): the requirements by their conditions (the winning lines among them)
        rq, rv = res.get("requirements") or [], res.get("reverse") or []
        n = {s: sum(1 for r in rq if r["status"] == s) for s in REQ_STATUS}
        out.append(f"  requirements met (D15): {n['PASS']} of {len(rq)}; {n['REVIEW']} need review; {n['FAIL']} failing; "
                   f"reverse check: {len(rv)} failing")
        for r in rq:
            if r["status"] != "PASS":
                out.append(f"  {'needs review' if r['status'] == 'REVIEW' else 'FAIL'} {r['key']}: {r['condition']['why'][:220]}")
        out += [f"  reverse FAIL: {x[:220]}" for x in rv]
    else:
        for w in res["winning"]:
            out.append(f"  winning (advisory) {w['id']}: " + (", ".join(f"{s} {st}" for s, st in w["sections"].items()) or "no section"))
    for key, label in (("fetch_errors", "fetch errors"), ("unknown_ids", "ids the contract lacks"), ("dead_links", "panel links with no target"),
                       ("duplicate_blocks", "blocks rendered more than once"), ("foreign_blocks", "blocks not in the catalogue")):
        if res.get(key):
            out.append(f"  {label}: {', '.join(res[key])}")
    if res.get("map_missing"):
        out.append(f"  map: none beside the contract ({res['map_missing']}) - every item is UNMAPPED; write one with map-template, then validate-map")
    elif res.get("map_problems"):
        out.append(f"  map INVALID ({len(res['map_problems'])} problem(s)) - not used: {res['map_problems'][0]}")
    if not res.get("panel_rows") and (res.get("page_metas") or {}).get("naipc-challenge") and not res.get("parse_error"):
        out.append("  panel: not on this page - no item has an answer or a section link")   # page_context's rule (D8)
    return out


def notes_text(res, doc):
    """brief_notes.txt: the panel's plain text, for the submission form's Notes field (D6)."""
    t = doc["tracks"][res["track"]]
    lines = [f"How this answers the brief — {clean_text(t['title'])} ({doc['challenge']['id']})", ""]
    rows = {r["id"]: r for r in res["panel_rows"]}
    for i, it in enumerate(t["items"], 1):
        r = rows.get(it["id"])
        lines.append(f"{i}. {clean_text(it['text'])}")
        lines.append(f"   {r['a'] if r and r['a'] else '—'}")
        if r and r["l"]:
            lines.append(f"   On the page: {r['l']}")
    return "\n".join(lines) + "\n"


def write_manifest(res, doc, coverage_dir, page_bytes, page_name="output/index.html"):
    """<harness>/coverage/coverage.json and brief_notes.txt (D6) — never inside output\\ (the judged upload)."""
    d = Path(coverage_dir)
    d.mkdir(parents=True, exist_ok=True)
    man = dict(res, written_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), page=page_name,
               page_sha256=hashlib.sha256(page_bytes).hexdigest())
    (d / "coverage.json").write_bytes((json.dumps(man, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    (d / "brief_notes.txt").write_bytes(notes_text(res, doc).encode("utf-8"))
    return man


def gate(track, page_path, coverage_dir, fetch_errors=None):
    """The coverage gate inside a harness (D7), after the final page is written: never raises, never changes the page or the exit
    code. Prints the block, writes the manifest; returns the result (None when nothing was checked)."""
    try:
        if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
            say("coverage: not checked — no contract (NAIPC_REQUIREMENTS not set)")
            return None
        try:
            doc = from_env()
        except RequirementsError as e:
            say(f"coverage: not checked — the contract is not usable: {e.problems[0] if e.problems else '?'}")
            return None
        cmap, mprobs, mmissing = map_for_check(os.environ["NAIPC_REQUIREMENTS"].strip(), doc)
        page = Path(page_path).read_bytes()
        res = coverage_check(doc, cmap, track, page.decode("utf-8", "replace"), run_id=run_id(), fetch_errors=fetch_errors)
        res["map_problems"], res["map_missing"] = mprobs, mmissing
        write_manifest(res, doc, coverage_dir, page)
        for line in gate_lines(res):
            say(line)
        say(f"  manifest: {Path(coverage_dir) / 'coverage.json'} · notes: {Path(coverage_dir) / 'brief_notes.txt'}")
        return res
    except Exception as e:                                       # noqa: BLE001
        say(f"coverage: check failed — {type(e).__name__}: {e}")
        return None


def requirements_summary(doc, coverage_dirs, run_id):
    """BUILD IC-4 (D15): (lines, gaps, needs review) — the requirements met across the three pages' manifests of this run (each manifest
    for its own track); those linked to no page (a named user or a winning line whose map entry names no track) and those whose page left
    no manifest this run (not checked), both failing. Nothing when no manifest carries conditions."""
    rq, rv = [], []
    for t in TRACKS:
        f = Path(coverage_dirs[t]) / "coverage.json"
        try:
            m = json.loads(f.read_bytes().decode("utf-8-sig")) if f.is_file() else None
        except Exception:                                        # noqa: BLE001
            m = None
        if (isinstance(m, dict) and m.get("conditions") and m.get("run_id") == run_id and m.get("challenge") == doc["challenge"]["id"]
                and m.get("track") == t):
            rq += m.get("requirements") or []
            rv += m.get("reverse") or []
    if not rq and not rv:
        return [], [], []
    cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    cmap = map_for_check(cpath, doc)[0] if cpath else None
    allr, seen = (requirements_of(doc, cmap) if cmap else []), {r["key"] for r in rq}
    unlinked = [key for key, _k, _i, _w, t, _e in allr if t is None]
    unchecked = [key for key, _k, _i, _w, t, _e in allr if t is not None and key not in seen]
    met = sum(1 for r in rq if r["status"] == "PASS")
    review = [r["key"] for r in rq if r["status"] == "REVIEW"]
    failing = [r["key"] for r in rq if r["status"] == "FAIL"]
    lines = [f"  Requirements met (D15): {met} of {len(rq) + len(unlinked) + len(unchecked)}; {len(review)} need review; "
             f"{len(failing) + len(unlinked) + len(unchecked)} failing" + (f" (linked to no page: {', '.join(unlinked)})" if unlinked else "")
             + (f" (not checked, no manifest of this run from their page: {', '.join(unchecked)})" if unchecked else "")
             + (f"; reverse check: {len(rv)} failing" if rv else "")]
    lines += [f"    ✗ {r['key']}: {r['condition']['why'][:200]}" for r in rq if r["status"] == "FAIL"]
    lines += [f"    ? {k} — needs review" for k in review]
    lines += [f"    ✗ reverse: {x[:200]}" for x in rv]
    return lines, [f"req:{k}" for k in failing + unlinked + unchecked] + [f"reverse:{i}" for i in range(len(rv))], review


def coverage_summary(doc, coverage_dirs, run_id, policy="warn"):
    """pipeline.py's coverage block (D7) from the three manifests: (lines, failed). A manifest counts only when it carries this run's
    id and this contract's id. failed is True only under policy "fail" with a gap left (the caller decides the exit code)."""
    lines, gaps, total, covered = ["Coverage (P0.3):"], [], 0, 0
    users_found = set()
    for t in TRACKS:
        n_items = len(doc["tracks"][t]["items"])
        total += n_items
        f = Path(coverage_dirs[t]) / "coverage.json"
        m = None
        try:
            if f.is_file():
                m = json.loads(f.read_bytes().decode("utf-8-sig"))
        except Exception:                                        # noqa: BLE001
            m = None
        if not (isinstance(m, dict) and m.get("run_id") == run_id and m.get("challenge") == doc["challenge"]["id"] and m.get("track") == t):
            lines.append(f"  {t}: no coverage from {t} this run" + ("" if f.is_file() else f" ({f} not found)"))
            gaps += [it["id"] for it in doc["tracks"][t]["items"]]
            continue
        c = m["counts"]
        ans = f" · answers {c['answers']}/{c['items']}" if c.get("answers") is not None else ""
        lines.append(f"  {t}: {c['items']} items — {c['ok']} ok · {c['thin']} thin · {c['fixed']} fixed · {c['empty']} empty · "
                     f"{c['missing']} missing · {c['unmapped']} unmapped" + (f" · {c['stale']} stale" if c.get("stale") else "") + ans)
        covered += c["covered"]
        for x in m["items"]:
            if not x["covered"]:
                gaps.append(x["id"])
                lines.append(f"    ⚠ {x['id']}: {x['reason']} — \"{x['text']}\"")
        for u in m.get("users") or []:
            if u.get("found_in"):
                users_found.add(u["id"])
        if m.get("fetch_errors"):
            gaps += [f"fetch:{t}/{e}" for e in m["fetch_errors"]]
            lines.append(f"    ⚠ fetch errors ({t}): {', '.join(m['fetch_errors'])}")
        if m.get("map_missing"):
            lines.append(f"    ⚠ map: none beside the contract — every {t} item is UNMAPPED")
        elif m.get("map_problems"):
            lines.append(f"    ⚠ map INVALID, not used ({len(m['map_problems'])} problem(s)): {m['map_problems'][0]}")
    uids = user_ids(doc)
    if not uids:
        lines.append("  Rule 13: the brief names no users (satisfied)")
    else:
        missing = [u for u in uids if u not in users_found]
        lines.append(f"  Rule 13: {len(uids) - len(missing)} of {len(uids)} named users have a section built for them" + (f" — missing: {', '.join(missing)}" if missing else ""))
        gaps += [f"user:{u}" for u in missing]
    rq_lines, rq_gaps, rq_review = requirements_summary(doc, coverage_dirs, run_id)      # BUILD IC-4 (D15)
    lines += rq_lines
    gaps += rq_gaps
    item_gaps = [g for g in gaps if ":" not in g]
    other = [g for g in gaps if ":" in g]
    if not gaps:
        if rq_review:                    # BUILD IC-4 (D15): a requirement that needs review is never green by itself
            lines.append(f"⚠ coverage: {covered} of {total} items covered; {len(rq_review)} requirement(s) need the operator's review: "
                         + ", ".join(rq_review))
            return lines, False
        lines.append(f"✅ coverage: {covered} of {total} items covered")
        return lines, False
    head = f"{len(item_gaps)} of {total} items not covered — {', '.join(item_gaps)}" if item_gaps else f"{covered} of {total} items covered"
    if other:
        head += f"; {len(other)} other gap(s): {', '.join(other)}"
    if rq_review:
        head += f"; {len(rq_review)} requirement(s) need review"
    if policy == "fail":
        lines.append(f"❌ coverage FAIL: {head}")
        return lines, True
    lines.append(f"⚠ coverage (warn): {head}")
    return lines, False


# ── BUILD IC-4 (D15): the conditions drafted from the brief's words, approved by the operator ──
_NEED_STOP = frozenset("""a an the and or but if so of to in on at by for from with without into onto over under about as is are was were be been
being i i'm im me my mine we our you your he she they them their it its this that these those there here who whom whose which what when where
why how can could would should will shall may might must do does did don't doesn't didn't won't can't not no nor yet still maybe already just
also even ever know knew find found get got keep kept start started tell telling told figure need needs want go going come help make makes
made work works use reduce more less most best somewhere someone somebody something anything nothing everything people person thing things
way time week day night whether else behind none down next all one out up off back away toward towards""".split())


def need_phrases(text, limit=8):
    """BUILD IC-4 (D15): the words of the brief's own text a drafted condition looks for — those that are not function words (a draft: the
    operator edits it and approves it)."""
    out = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\u2019-]*", str(text or "")):
        lw = w.lower().strip("'\u2019")
        if lw in _NEED_STOP or (len(lw) < 3 and not (w.isupper() or w.isdigit())):
            continue
        if lw not in (x.lower() for x in out):
            out.append(w)
    return out[:limit]


def draft_conditions(doc, cmap):
    """BUILD IC-4 (D15): {requirement key: {"link", "condition"}} drafted from the brief's words. A requirement whose map entry names
    sections: a sentence condition on its first section — for a named user, the user and the need phrases of the user's own words; else
    the requirement's own words. One linked to no section: "needs review" (link it first)."""
    out = {}
    quotes = {u["id"]: u.get("quote") for u in doc.get("named_users") or []}
    for key, kind_, rid, words, t, e in requirements_of(doc, cmap):
        secs = [s for s in (e.get("sections") or []) if isinstance(s, str)]
        if t is None or not secs:
            out[key] = {"link": "implements", "condition": {"kind": "review", "why": "no page section is linked to it yet: link it (its track "
                                                                                       "and sections), then write its predicate"}}
            continue
        ph = need_phrases(quotes.get(rid) if kind_ == "user" else words) or [str(words)]
        out[key] = {"link": "implements", "condition": dict({"kind": "sentence", "section": secs[0]}, **({"user": rid} if kind_ == "user" else {}),
                                                            phrases=ph)}
    return out


def draft_conditions_path(contract_path):
    p = Path(contract_path)
    return p.with_name(p.name.replace(".json", ".conditions.draft.json"))


def write_condition_drafts(contract_path):
    """BUILD IC-4 (D15): the drafts into <challenge>.conditions.draft.json beside the contract — never into the map. (path, n, problems)."""
    doc = load(contract_path)
    cmap, problems, _missing = map_for_check(contract_path, doc)
    if problems:
        return None, 0, problems
    drafts = draft_conditions(doc, cmap)
    dp = draft_conditions_path(contract_path)
    tmp = dp.with_name(f".{dp.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"schema": 1, "map": map_path_for(contract_path).name, "drafts": drafts}, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, dp)
    return dp, len(drafts), []


def confirm_conditions(draft_path, keys):
    """BUILD IC-4 (D15): the operator approves — the named drafts' link and condition into the map beside the draft, validated, written only
    when valid. A user or a winning line must be linked to its track first. Returns (n confirmed, problems)."""
    dp = Path(draft_path)
    d = json.loads(dp.read_text(encoding="utf-8"))
    mp = dp.with_name(d.get("map") or dp.name.replace(".conditions.draft.json", ".map.json"))
    cp = contract_path_for(mp)
    doc = load(cp)
    m = json.loads(mp.read_text(encoding="utf-8"))
    n, P = 0, []
    for k in keys:
        dr = (d.get("drafts") or {}).get(k)
        parts = str(k).split(".")
        if not isinstance(dr, dict):
            P.append(f"{k}: no drafted condition by that key")
            continue
        node = m
        for p_ in parts[:-1]:
            node = node.setdefault(p_, {}) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            P.append(f"{k}: not a place in the map")
            continue
        e = node.setdefault(parts[-1], {"sections": [], "note": None} if parts[0] == "tracks" else {"track": None, "sections": []})
        if parts[0] != "tracks" and e.get("track") not in TRACKS:
            P.append(f"{k}: link it to its track and sections first (the page its condition is checked on)")
            continue
        e["link"], e["condition"] = dr.get("link"), dr.get("condition")
        n += 1
    P += validate_map(m, doc)
    if P:
        return 0, P
    tmp = mp.with_name(f".{mp.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, mp)
    return n, []


# ── the brief's answers (D9) and the chat's brief block (D10) ──

def norm_id(s):
    return str(s).strip().lower().replace("_", "-") if isinstance(s, (str, int)) else ""


def _answer_of(e):
    """An entry's answer: "answer" when it holds text, else "text" when the entry has one (a null "answer" beside a "text" keeps the
    text — the P0.3 review), else whatever "answer" holds (dropped and named by the caller)."""
    a = e.get("answer")
    return a if (isinstance(a, str) and a.strip()) or "text" not in e else e.get("text")


def parse_brief_answers(raw, item_ids_, plain=False):
    """{id: answer} from whatever the model returned under brief_answers, plus a note per thing dropped. Never raises: accepts a list
    of {id, answer} or {id, text}, or an object id → answer (or id → {answer}); ids are normalized (strip, lower, _ → -); unknown
    ids, duplicates and null, empty or non-text answers are dropped and named.
    P0.7 r2 (E3): plain=True (the update path) returns the notes as named parts — [{part, field, why, detail}]: `why` in plain words,
    `field` an item id, "brief_answers[j]" or "brief_answers" (an id the model wrote cut to NAME_CHARS), `detail` the default note;
    nothing for an absent key (absence is not a drop); an entry that names no item (no id, null, white space, an object's empty key) is
    "brief_answers[j]", "it names no item of the brief" (Phase 5 review #6 — never "null"). The default notes are Block 1's, unchanged."""
    known, out, notes, parts = list(item_ids_), {}, [], []
    pairs = []

    def note(text, part, field, why):
        notes.append(text)
        parts.append({"part": part, "field": field, "why": why, "detail": cut(text, DETAIL_CHARS)})
    if raw is None:
        return ({}, []) if plain else ({}, ["brief_answers: absent"])
    if isinstance(raw, list):
        for j, e in enumerate(raw):
            if isinstance(e, dict):
                pairs.append((j, e.get("id"), _answer_of(e)))
            else:
                note(f"brief_answers[{j}]: not an object ({type(e).__name__}) — dropped", "brief answer", f"brief_answers[{j}]", "not in the expected form")
    elif isinstance(raw, dict):
        for j, (k, v) in enumerate(raw.items()):
            pairs.append((j, k, _answer_of(v) if isinstance(v, dict) else v))
    else:
        note(f"brief_answers: a {type(raw).__name__}, not a list or an object — ignored", "shape", "brief_answers",
             "not a list or an object — Block 1's answers stand")
        return ({}, parts) if plain else ({}, notes)
    for j, k, v in pairs:
        i = norm_id(k)
        if not i:                # P0.7 r2 Phase 5 (review #6): an entry that names no item — no id, null, white space, an object's empty key
            note(f"brief_answers: unknown id {k!r} — dropped", "brief answer", f"brief_answers[{j}]", "it names no item of the brief")
        elif i not in known:
            note(f"brief_answers: unknown id {k!r} — dropped", "brief answer", name_cut([k], quote=False), "not an item of the brief")
        elif not (isinstance(v, str) and v.strip()):
            kind = 'null' if v is None else 'empty' if isinstance(v, str) else type(v).__name__
            note(f"brief_answers: {i}: {kind} answer — dropped", "brief answer", i,
                 "the answer is empty" if kind in ("null", "empty") else "the answer is not text")
        elif i in out:
            note(f"brief_answers: {i}: a second answer — dropped", "brief answer", i, "a second answer to the same item — the first is used")
        else:
            out[i] = v.strip()
    return (out, parts) if plain else (out, notes)


def brief_items(doc, track):
    return [(it["id"], it["text"]) for it in doc["tracks"][track]["items"]]


def _stated_constraints(doc):
    cons = [(k, doc["constraints"].get(k)) for k in CONSTRAINT_KEYS]
    return [(k, c) for k, c in cons if isinstance(c, dict) and c.get("value") is not None]


def _named_user_line(u):
    """One named user as the <brief> block prints it — BUILD IC-2 (D2): brief_source reads the same line."""
    return f"- {u['id']}: {u.get('name')}" + (f", {u['age']}" if u.get("age") is not None else "") + f" — {u.get('role')}: \"{u.get('quote')}\""


def _brief_context(doc, facts):
    """The lines both prompts carry after the mission and the items (D9): every stated constraint as value ("quote"), the named
    users, and what this pipeline really does (so an 'AI use' answer stays true)."""
    lines = []
    stated = _stated_constraints(doc)
    if stated:
        lines += ["", "Constraints the brief states (value and its words):"]
        lines += [f"- {k}: {json.dumps(c['value'], ensure_ascii=False)} (\"{c.get('quote', '')}\")" for k, c in stated]
    if doc.get("named_users"):
        lines += ["", "People the brief names:"]
        lines += [_named_user_line(u) for u in doc["named_users"]]
    if facts:
        lines += ["", "What this pipeline actually does (the only true facts about method and AI use — claim nothing beyond them):"]
        lines += [f"- {f}" for f in facts]
    return lines


def brief_source(doc, track):
    """BUILD IC-2 (D2): the organizers' words in the <brief> block, as the block prints them — the mission, each item's line, each stated
    constraint's own words and each named user's line — the one text a quote [[q:brief|…]] is found in. The block's own lines (its
    headings, Python's reading of a constraint's value, the pipeline's facts, the answer instructions) are not in it. "" with no brief."""
    if not isinstance(doc, dict) or track not in (doc.get("tracks") or {}):
        return ""
    lines = [doc["tracks"][track]["mission"]] + [f"- {i} — {txt}" for i, txt in brief_items(doc, track)]
    lines += [str(c.get("quote")) for _k, c in _stated_constraints(doc) if c.get("quote")]
    lines += [_named_user_line(u) for u in doc.get("named_users") or []]
    return "\n".join(lines)


def _answers_plain():
    """BUILD IC-5 (D14): the brief block's last rule for the answers — with this build's switch, plain sentences and no tags (no
    request's answers print through fix_narrative's badges: the Oracle's panel reads NR.clean_text, the Muse's | model); without it,
    IC-4's words byte for byte."""
    return "Plain text only: sentences, no tags." if ic5_prompt() else "Plain text only: no markdown and no HTML inside the answers (source tags as the system prompt asks are fine)."


def brief_block(doc, track, facts, curveball=None, update=False, wire=False):
    """The <brief> block for a generation prompt (D9): the track's mission verbatim, every item as 'id — text', then _brief_context.
    P0.7 (D2): with `curveball` (the change list's curveball object, Block 2 only) the announcement and the tests it states follow the
    items; with `update` (an update call over Block 1's output) brief_answers are asked only for the items the curveball changes."""
    t = doc["tracks"][track]
    lines = ["<brief>", "The competition brief for this track — the judges score against these exact items. Mission (verbatim):",
             t["mission"], "", "Items to answer (id — the brief's words):"]
    lines += [f"- {i} — {txt}" for i, txt in brief_items(doc, track)]
    if isinstance(curveball, dict) and isinstance(curveball.get("text"), str) and curveball["text"].strip():
        when = curveball.get("announced_at")
        lines += ["", "The curveball, announced " + (f"at {when} " if isinstance(when, str) and when.strip() else "")
                  + "during the event (verbatim):", curveball["text"].strip()]
        tests = [x for x in (curveball.get("tests") or []) if isinstance(x, str) and x.strip()]
        if tests:
            lines += ["", "What the curveball tests (the brief's words; each is answered on the page):"]
            lines += [f"- {x.strip()}" for x in tests]
    lines += _brief_context(doc, facts)
    if update == "edits":       # IC-4 Phase 7 (review c2): the edit request reviews each answer as an item of <review_set>
        lines += ["", "Block 1's answers to the items above are items of the review set (brief.<the item's id>): review each like any other "
                  "item — keep it, or replace it with 2–4 plain-text sentences and the refs from the change list that move it. Use only "
                  "numbers from the data or from this analysis. " + _answers_plain(), "</brief>"]       # BUILD IC-5 (D14)
        return "\n".join(lines)
    if update:
        lines += ["", "Block 1's answers to the items above stand unless the curveball changes them. Return a top-level key \"brief_answers\" "
                  "holding ONLY the items whose answer the curveball changes: {\"id\": \"<the item's id>\", \"answer\": \"<2–4 plain-text "
                  "sentences>\", \"because\": [\"<a ref from the change list>\"]}; leave every other item out (it keeps Block 1's answer). "
                  "Use only numbers from the data or from this analysis. " + _answers_plain(), "</brief>"]       # BUILD IC-5 (D14)
        return "\n".join(lines)
    lead = ("\"brief_answers\": a list" if wire else          # BUILD IC-7 (D5): the field the call's sent schema holds, by its name
            "In addition to every field of the schema, return a top-level key \"brief_answers\": a list")
    lines += ["", lead + " with exactly one object per item "
              "above, {\"id\": \"<the item's id>\", \"answer\": \"<2–4 plain-text sentences>\"}. Answer each item in its own terms (a question "
              "that names a period, an amount or a place is answered for that period, amount or place), use only numbers from the data or from "
              "this analysis, and keep the answers consistent with the rest of your output. " + _answers_plain(), "</brief>"]   # IC-5 (D14)
    return "\n".join(lines)


def brief_repair_prompt(doc, track, missing, summary, facts=None):
    """The single repair request for items the model did not answer (D9): the missing items, the brief (mission, constraints, named
    users, the pipeline's true facts — the generation prompt's own lines) and a compact plain-text summary."""
    items = [(i, txt) for i, txt in brief_items(doc, track) if i in missing]
    brief = ["<brief>", "Mission (verbatim):", doc["tracks"][track]["mission"]] + _brief_context(doc, facts) + ["</brief>"]
    return ("Your analysis of this brief is summarised below, but it gave no answer for some of the brief's items. Answer ONLY these items, "
            "in their own terms, 2–4 plain-text sentences each, using only facts from the brief and the summary.\n\n"
            "<items>\n" + "\n".join(f"- {i} — {txt}" for i, txt in items) + "\n</items>\n\n"
            + "\n".join(brief) + f"\n\n<summary>\n{summary}\n</summary>\n\n"
            "Return ONLY this JSON object: {\"brief_answers\": [{\"id\": \"<item id>\", \"answer\": \"<plain text>\"}]}")


def brief_chat_block(doc, track, answers):
    """BRIEF — QUESTIONS AND ANSWERS for the chat context (D10): each item's words and its plain-text answer, on labelled lines."""
    out = ["BRIEF — QUESTIONS AND ANSWERS (the competition's own items and this page's answers):"]
    for i, txt in brief_items(doc, track):
        out.append(f"- Q: {clean_text(txt)}")
        out.append(f"  A: {clean_text((answers or {}).get(i, '')) or '(no answer on this page)'}")
    return "\n".join(out)



# ═══ P0.6 — the facts layer (D1), the competition label (D6), the unit's forms, short money, the URL builder (D8), the program
# identity (D3). Every Rule 4 value a prompt or a page states comes through here; a fact the brief does not state is None (D2). ═══

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
UNIT_FORMS = {"families": "family", "people": "person", "persons": "person", "patients": "patient", "residents": "resident",
              "households": "household", "students": "student", "children": "child", "youth": "youth", "veterans": "veteran",
              "adults": "adult", "seniors": "senior", "individuals": "individual", "clients": "client", "participants": "participant",
              "tenants": "tenant", "workers": "worker", "neighbors": "neighbor", "members": "member", "women": "woman", "men": "man",
              "staff": "staff"}
_UNIT_PLURALS = {s: p for p, s in UNIT_FORMS.items() if p != "persons"}       # a unit given in the singular: "person" → "people"
_UNIT_IRREGULAR = {"alumni": "alumnus", "personnel": "personnel", "faculty": "faculty", "police": "police", "clergy": "clergy",
                   "cattle": "cattle", "livestock": "livestock", "sheep": "sheep", "deer": "deer", "fish": "fish"}   # plurals without an -s
_UNIT_LINKS = frozenset(("of", "in", "at", "with", "without", "for", "from", "on", "under", "across", "near", "by", "into", "through",
                         "within", "among", "after", "before", "over", "to", "outside", "inside", "around", "who", "whose", "that", "which"))
BUDGET_SOURCES = {     # D2: kpis.contract_source → the words every page, prompt and chat context uses for the budget
    "override": "Budget (override)",
    "brief": "Contract (the brief)",
    "proposed": "Proposed budget — our estimate; the brief states no contract value",
    "cost_plan": "Proposed budget — equal to the Year-1 cost plan",
}
BUDGET_SHORT = {       # D2: the same sources as a short label beside a number (the hub's hero stat, the Oracle's simulator); else "Budget"
    "override": "Budget (override)",
    "brief": "Contract",
    "proposed": "Proposed budget",
    "cost_plan": "Proposed budget",
}
IDENTITY_FILE = "program_identity.json"     # D3: <oracle-harness>/checkpoints/program_identity.json
IDENTITY_SCHEMA = 1
IDENTITY_KEYS = ("challenge", "run_id", "program_name", "brand_html", "domain_label", "source", "written_at")


def _given(v):
    """A value the contract really states: not None, not a <TODO> placeholder, not blank text."""
    return v is not None and not _is_todo(v) and not (isinstance(v, str) and not v.strip())


def _stated(doc, key):
    """(value, quote) of a stated constraint, or (None, None); never raises (a malformed entry reads as not stated)."""
    cons = doc.get("constraints") if isinstance(doc, dict) else None
    c = cons.get(key) if isinstance(cons, dict) else None
    if isinstance(c, dict) and _given(c.get("value")):
        q = c.get("quote")
        return c["value"], (q if isinstance(q, str) and _given(q) else None)
    return None, None


def event_date(doc):
    """challenge.date as a datetime.date, or None (no contract, a placeholder, or not YYYY-MM-DD)."""
    try:
        return datetime.date.fromisoformat(str(doc["challenge"]["date"]).strip())
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def date_long(d):
    """'April 18, 2026': the month from MONTHS, the day as a plain number — never strftime (Windows has no unpadded-day directive,
    and the month name follows the locale). '' for anything that is not a date."""
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}" if isinstance(d, datetime.date) else ""


def competition_label(doc):
    """D6: 'NAIPC <city> Regional · <Month D, YYYY>' for a regional event slug, 'NAIPC National Championship · <date>' for a national
    one, 'NAIPC · <date>' otherwise, 'NAIPC' with no contract; the date part is left out when the contract has no valid date."""
    if not isinstance(doc, dict):
        return "NAIPC"
    ch = doc.get("challenge") if isinstance(doc.get("challenge"), dict) else {}
    ev = str(ch.get("event") or "").lower()
    city = ch.get("city").strip() if isinstance(ch.get("city"), str) and _given(ch.get("city")) else None
    if "national" in ev:
        head = "NAIPC National Championship"
    elif "regional" in ev:
        head = f"NAIPC {city} Regional" if city else "NAIPC Regional"
    else:
        head = "NAIPC"
    day = date_long(event_date(doc))
    return f"{head} · {day}" if day else head


def label_from_env(doc=None):
    """The competition label a harness prints: COMPETITION_LABEL when the pipeline (or Shane) set it, else the contract's (D6)."""
    v = os.environ.get("COMPETITION_LABEL", "").strip()
    return v or competition_label(doc)


def safe_stdio():
    """Printing that cannot raise (P0.6 red team RT-3): on Windows, stdout and stderr write UTF-8 (the console, a redirect or a pipe —
    as the Muse and the hub have done since r2); anywhere, a character the stream's encoding lacks is replaced, never raised (a
    harness on a cp1252 pipe stopped at its first '→'). Each harness calls it before it prints; safe to call twice; never raises."""
    for st in (sys.stdout, sys.stderr):
        try:
            if hasattr(st, "reconfigure"):
                st.reconfigure(errors="replace", **({"encoding": "utf-8"} if sys.platform == "win32" else {}))
        except Exception:  # noqa: BLE001 — a stream that cannot be reconfigured keeps its own settings
            pass


def _unit_word(word):
    """(plural, singular) of one word: the table, the plurals without an -s, then the -ies / -es / -s rules; a word that looks
    singular gets its plural ('parish' → 'parishes'). The word's own capital is kept."""
    w = word.lower()

    def cap(x):
        return x[:1].upper() + x[1:] if word[:1].isupper() else x
    if w in UNIT_FORMS:
        return word, cap(UNIT_FORMS[w])
    if w in _UNIT_PLURALS:
        return cap(_UNIT_PLURALS[w]), word
    if w in _UNIT_IRREGULAR:
        return word, cap(_UNIT_IRREGULAR[w])
    if w == "alumnus":
        return cap("alumni"), word
    if re.search(r"[^aeiou]ies$", w):
        return word, cap(w[:-3] + "y")
    if re.search(r"(?:ss|x|z|ch|sh)es$", w):
        return word, cap(w[:-2])
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return word, cap(w[:-1])
    plural = w[:-1] + "ies" if re.search(r"[^aeiou]y$", w) else w + "es" if re.search(r"(?:s|x|z|ch|sh)$", w) else w + "s"
    return cap(plural), word


def _plural_like(word):
    """A word that reads as a plural noun: in the table or the plurals without an -s, or ending in -s (not -ss, -us, -is) with no
    apostrophe ('Mary's' is not a plural)."""
    w = word.lower()
    if "'" in w or "\u2019" in w:
        return False
    return w in UNIT_FORMS or w in _UNIT_IRREGULAR or (w.endswith("s") and not w.endswith(("ss", "us", "is")))


def unit_forms(unit):
    """(plural, singular) of the brief's beneficiary unit, in the brief's own words, inflecting one head word: the first plural
    followed by a preposition, a relative word or an -ing word ('people experiencing homelessness' → 'person experiencing
    homelessness', 'families in crisis' → 'family in crisis'), else the last plural ('low-income working families' → '… family'),
    else the last word, by the table, the plurals without an -s ('alumni' → 'alumnus') and the -ies / -es / -s rules. A unit
    given in the singular gets its plural ('family' → 'families'); no unit → ('beneficiaries', 'beneficiary'). The brief's
    capitalisation is kept. (P0.6 red team RT-2: the rule on the last word made 'homelessnesses' and 'alumnis'.)"""
    if not isinstance(unit, str) or not _given(unit):
        return "beneficiaries", "beneficiary"
    words = re.sub(r"\s+", " ", unit.strip()).split(" ")
    head = next((k for k in range(len(words) - 1) if _plural_like(words[k]) and (words[k + 1].lower() in _UNIT_LINKS or
                 (words[k + 1].lower().endswith("ing") and len(words[k + 1]) > 4))), None)
    if head is None:
        head = next((k for k in range(len(words) - 1, -1, -1) if _plural_like(words[k])), len(words) - 1)
    plural, singular = _unit_word(words[head])
    return " ".join(words[:head] + [plural] + words[head + 1:]), " ".join(words[:head] + [singular] + words[head + 1:])


def _trim(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def money_short(n):
    """The compact form for labels: 500000 → '$500K', 1250000 → '$1.25M', 999 → '$999'; '' for None or anything not a number."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return ""
    n = round(n)
    sign, a = ("-" if n < 0 else ""), abs(n)
    if a >= 1_000_000:
        return f"{sign}${_trim(a / 1_000_000)}M"
    if a >= 1_000:
        return f"{sign}${_trim(a / 1_000)}K"
    return f"{sign}${a}"


def facts(doc):
    """D1 — the facts layer: the contract's stated values with the brief's own words, None where the brief states none (D2).
    Keys: contract, contract_quote, pilot_days, pilot_quote, marketing, marketing_quote, unit_plural, unit_singular, unit_stated,
    label, date (datetime.date or None), city, challenge. Never raises; no contract → every fact None, the unit 'beneficiaries'."""
    d = doc if isinstance(doc, dict) else {}
    out = {}
    for name, qname, key, ok in (("contract", "contract_quote", "contract_usd", lambda v: _int(v) and v >= 0),
                                 ("pilot_days", "pilot_quote", "pilot_days", lambda v: _int(v) and v > 0),
                                 ("marketing", "marketing_quote", "reserved_marketing_usd", lambda v: _int(v) and v >= 0)):
        v, q = _stated(d, key)
        out[name], out[qname] = (v, q) if ok(v) else (None, None)
    u, _ = _stated(d, "beneficiary_unit")
    out["unit_plural"], out["unit_singular"] = unit_forms(u if isinstance(u, str) else None)
    out["unit_stated"] = isinstance(u, str) and _given(u)
    out["label"] = competition_label(doc)
    out["date"] = event_date(d) if d else None
    ch = d.get("challenge") if isinstance(d.get("challenge"), dict) else {}
    out["city"] = ch.get("city").strip() if isinstance(ch.get("city"), str) and _given(ch.get("city")) else None
    out["challenge"] = ch.get("id") if isinstance(ch.get("id"), str) and _given(ch.get("id")) else None
    return out


def team_identity(doc):
    """D3: the contract's own program name and brand (its 'team' block) — (name, brand_html) or (None, None)."""
    t = doc.get("team") if isinstance(doc, dict) else None
    if isinstance(t, dict) and isinstance(t.get("program_name"), str) and _given(t["program_name"]):
        b = t.get("brand_html")
        return t["program_name"].strip(), (b.strip() if isinstance(b, str) and _given(b) else None)
    return None, None


def endpoint_url(base, ep, slash=None):
    """D8 — the one URL builder: '<base>/<ep>', or '<base>/<ep>/' when NAIPC_URL_SLASH=1 (the pre-flight exports it when the server
    answers only that shape); `slash` True/False overrides the environment (the pre-flight probes both shapes)."""
    if slash is None:
        slash = os.environ.get("NAIPC_URL_SLASH", "").strip() == "1"
    return f"{str(base).rstrip('/')}/{str(ep).strip('/')}" + ("/" if slash else "")


_EM_SPLIT = re.compile(r"(<em>|</em>)", re.I)


def clean_brand_html(s, name=None):
    """A program brand that is safe to print unescaped: only <em> and </em> survive (balanced, no attributes); every other tag is
    removed with its words kept and the rest is escaped. Not text → the escaped `name` (or '')."""
    if not isinstance(s, str) or not s.strip():
        return html.escape(name or "")
    s = s.replace("\x00", "").replace("\x01", "")
    tags = [t.lower() for t in _EM_SPLIT.findall(s)]
    if tags and tags == ["<em>", "</em>"] * (len(tags) // 2):           # balanced, in order: kept through two placeholder characters
        s = re.sub(r"<em>", "\x00", s, flags=re.I)
        s = re.sub(r"</em>", "\x01", s, flags=re.I)
    t = html.escape(clean_text(s), quote=True)                          # every other tag removed (words kept), entities decoded, escaped
    return t.replace("\x00", "<em>").replace("\x01", "</em>") or html.escape(name or "")


def write_identity(path, challenge, run_id, program_name, brand_html, domain_label, source):
    """D3: write the program identity file (UTF-8, temp file + os.replace). Returns None, or the reason it was not written — never
    raises (a failed write must not stop the Oracle)."""
    p = Path(path)
    doc = {"schema": IDENTITY_SCHEMA, "challenge": challenge, "run_id": run_id, "program_name": program_name, "brand_html": brand_html,
           "domain_label": domain_label, "source": source,
           "written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as e:                                  # noqa: BLE001 — reported, never raised
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"not written: {type(e).__name__}: {e}"


def read_identity(path, challenge=None):
    """D3: read a program identity file → (dict, None), or (None, reason) — never raises. Read as utf-8-sig (a BOM is accepted); the
    file must be schema 1 with a non-empty program_name; with `challenge` it must name that challenge. Keys: IDENTITY_KEYS (a value
    that is not text reads as None)."""
    try:
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            return None, f"not found: {path}"
        except OSError as e:
            return None, f"not readable: {type(e).__name__}"
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            return None, f"not UTF-8 text (byte {e.start})"
        try:
            d = json.loads(text)
        except ValueError as e:
            return None, f"invalid JSON at line {getattr(e, 'lineno', '?')}, column {getattr(e, 'colno', '?')}"
        if not isinstance(d, dict):
            return None, "not a JSON object"
        if d.get("schema") != IDENTITY_SCHEMA:
            return None, f"schema {d.get('schema')!r}, expected {IDENTITY_SCHEMA}"
        name = d.get("program_name")
        if not isinstance(name, str):
            return None, "no program_name" if name is None else "program_name is not text"
        if not name.strip():
            return None, "program_name is empty"
        if challenge is not None and d.get("challenge") != challenge:
            return None, f"challenge {d.get('challenge')!r}, not {challenge!r}"
        out = {k: (d.get(k).strip() if isinstance(d.get(k), str) and d.get(k).strip() else None) for k in IDENTITY_KEYS}
        return out, None
    except Exception as e:                                  # noqa: BLE001 — e.g. RecursionError on absurd nesting
        return None, f"unreadable: {type(e).__name__}"


# ═══ P0.7 — the curveball as a diff: one envelope rule (D5), Block 1's snapshot and Block 2's change list (D1), the announcement as
# an input (D2), Python's per-field merge of an update reply (D3, D7), one chat context per folder (D9), a failed call's text (D10).
# Nothing here raises: a reader returns (None, reason), a writer returns the reason it did not write (or None). ═══

LIST_KEYS = ("data", "alerts", "items", "results", "records", "facilities", "resources")   # D5: the record list, first match wins
NAME_KEYS = ("name", "title", "providerName", "facility", "organization")                 # D1: name-like keys that can match records
STAMP_KEYS = frozenset(("lastUpdated", "last_updated", "updatedAt", "updated_at", "timestamp", "generatedAt", "generated_at",
                        "fetchedAt", "fetched_at"))                                          # D1: exact names only, never a substring
_ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
ABSENT = {"$absent": True}          # a field one block does not hold, in a change list's [old, new] pair
UNREADABLE = "$unreadable"          # {UNREADABLE: reason} stands for a payload that could not be read
SNAPSHOT_SCHEMA = 1
CHANGES_SCHEMA = 1
RUNS_DIR = "runs"                   # <landing-harness>/runs/<challenge id>/ (never inside output\, never uploaded)
SNAPSHOT_FILE = "block1.json"
BLOCK1_DIR = "block1"               # D3: <harness>/checkpoints/block1/ — the Block 1 base an update call starts from
BLOCK1_META = "meta.json"
CURVEBALL_FILE = "curveball.json"   # D3: <oracle-harness>/checkpoints/curveball.json — what the Oracle's Block 2 did, for the other tracks
CONTEXT_ARCHIVE = "chat-context-archive"
CHANGE_CAP = 12                     # records listed per endpoint and kind in a prompt; the rest are counted, never dropped silently
CHAT_CHANGES_LIMIT = 12000          # bytes of a chat context's CURVEBALL block (the Worker refuses a context over 96 KiB); P0.7 r2 (E3 c):
                                    # sized on R7's three live blocks by changes_chat_block's own count (5,484 / 5,670 / 2,226; the
                                    # Oracle's 6,447 with r2's lines) — r2's lines at their caps add at most ≈ 4,300
NAME_CHARS = 60                     # P0.7 r2 (E3): a name the model wrote (an unknown key, a ref, an id) is shown cut to this many characters
DETAIL_CHARS = 1000                 # P0.7 r2 (E3 a): a dropped part's technical detail, kept in the record only (never on a page)
NOT_APPLIED_LINE_BYTES = 400        # P0.7 r2 (E3 c): one "Not applied at the curveball" line of a chat context
NOT_APPLIED_TOTAL_BYTES = 3000      # P0.7 r2 (E3 c): those lines together; past it one line says how many more the page lists
REPLACED_LINE_BYTES = 1200          # P0.7 r2 (E3 c): the Oracle's one "Brief answers replaced at the curveball" line
NOT_APPLIED_CHAT = "Not applied at the curveball (Block 1's version stands where it had one): "
CHANGE_STATUSES = ("changed", "unchanged", "new endpoint", "not fetched this run", "unreadable")   # + "unchanged (only <key> changed)"
_MISSING = object()


def _utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope_split(payload):
    """D5 — the one envelope rule: (records, envelope, rule). A list under data, then alerts, then items, results, records, facilities,
    resources → that list, the other top-level keys its envelope; {"data": {…}} → that one object, the other keys its envelope; any
    other object → itself as ONE record with no envelope (a flat object: its own stamps are fields of that record); a list → itself;
    anything else → no records. Never raises."""
    if isinstance(payload, dict):
        for k in LIST_KEYS:
            if isinstance(payload.get(k), list):
                return payload[k], {a: b for a, b in payload.items() if a != k}, f"list under {k}"
        if isinstance(payload.get("data"), dict):
            return [payload["data"]], {a: b for a, b in payload.items() if a != "data"}, "object under data"
        return [payload], {}, "flat object"
    if isinstance(payload, list):
        return payload, {}, "bare list"
    return [], {}, "no records"


def records(payload):
    """D5: the records of one endpoint's payload — the list every harness, the pre-flight's count and the diff read."""
    return envelope_split(payload)[0]


def is_alerts(payload):
    """D5: an alert-shaped payload (its records sit in an "alerts" list) — never a resource endpoint, whatever it is named."""
    return envelope_split(payload)[2] == "list under alerts"


def _norm(v):
    """D1's one normalisation: an integer-valued float is the integer (1.0 = 1); booleans stay booleans; keys become text."""
    if v is None or isinstance(v, (bool, str, int)):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return str(v)


def canon(v):
    """A value as canonical JSON (keys sorted, D1's normalisation) — order, indentation, CRLF and a BOM never count as change."""
    return json.dumps(_norm(v), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def same_payload(a, b):
    """True when two payloads are equal by value (D1's normalisation); never raises."""
    try:
        return canon(a) == canon(b)
    except Exception:  # noqa: BLE001 — e.g. RecursionError: not provably equal
        return False


def _stamp_value(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return v == v and v >= 0                            # epoch seconds or milliseconds (NaN is not one)
    return isinstance(v, str) and bool(_ISO_STAMP.match(v.strip()))


def _field_diff(a, b):
    out = {}
    for k in sorted(set(a) | set(b), key=str):
        va, vb = a.get(k, _MISSING), b.get(k, _MISSING)
        if va is _MISSING or vb is _MISSING or canon(va) != canon(vb):
            out[k] = (va, vb)
    return out


def _split_stamps(fd):
    """(stamps, the rest) of a field diff: a stamp is a key named exactly as in STAMP_KEYS whose values are date-times or epochs."""
    st = {k: p for k, p in fd.items() if k in STAMP_KEYS and all(x is _MISSING or _stamp_value(x) for x in p)}
    return st, {k: p for k, p in fd.items() if k not in st}


def _jv(v):
    return dict(ABSENT) if v is _MISSING else v


def _match_rule(old, new):
    """D1: 'id' when every record of both blocks is an object holding a unique scalar id, else the first name-like key unique in both,
    else 'position'."""
    if not all(isinstance(r, dict) for r in old + new) or not (old or new):
        return "position"
    for key in ("id",) + NAME_KEYS:
        good = True
        for recs in (old, new):
            vals = [r.get(key) for r in recs]
            if any(v is None or isinstance(v, (dict, list, bool)) for v in vals) or len({canon(v) for v in vals}) != len(vals):
                good = False
                break
        if good:
            return key
    return "position"


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _diff_endpoint(ep, old, new):
    o_recs, o_env, o_rule = envelope_split(old)
    n_recs, n_env, n_rule = envelope_split(new)
    match = _match_rule(o_recs, n_recs)
    if match == "position":
        pairs = [(f"{ep}#{i + 1}", o_recs[i] if i < len(o_recs) else _MISSING, n_recs[i] if i < len(n_recs) else _MISSING)
                 for i in range(max(len(o_recs), len(n_recs)))]
    else:
        om = {canon(r[match]): r for r in o_recs}
        nm = {canon(r[match]): r for r in n_recs}
        pairs = [(str(r[match]), r, nm.get(canon(r[match]), _MISSING)) for r in o_recs]
        pairs += [(str(r[match]), _MISSING, r) for r in n_recs if canon(r[match]) not in om]
    added, removed, changed, rec_stamps = [], [], [], set()
    for ref, a, b in pairs:
        if a is _MISSING:
            added.append({"ref": ref, "record": b})
        elif b is _MISSING:
            removed.append({"ref": ref, "record": a})
        elif isinstance(a, dict) and isinstance(b, dict):
            st, rest = _split_stamps(_field_diff(a, b))
            rec_stamps.update(st)
            if rest:
                changed.append({"ref": ref, "fields": {k: [_jv(x), _jv(y)] for k, (x, y) in rest.items()}})
        elif canon(a) != canon(b):
            changed.append({"ref": ref, "fields": {"(value)": [a, b]}})
    env_st, env_rest = _split_stamps(_field_diff(o_env, n_env))
    if o_rule == n_rule == "no records" and canon(old) != canon(new):
        env_rest["(payload)"] = (old, new)                    # a JSON scalar has no records; its change is still a change
    shape = [o_rule, n_rule] if o_rule != n_rule else None
    nums = set()                       # D1: numeric fields that changed — in a changed record, or held by an added or removed record
    for c in changed:
        nums.update(k for k, (x, y) in c["fields"].items() if _is_num(x) or _is_num(y))
    for side in [e["record"] for e in added] + [e["record"] for e in removed]:
        if isinstance(side, dict):
            nums.update(k for k, v in side.items() if _is_num(v))
    sums = {}
    for f in sorted(nums, key=str):
        so = [r[f] for r in o_recs if isinstance(r, dict) and _is_num(r.get(f))]
        sn = [r[f] for r in n_recs if isinstance(r, dict) and _is_num(r.get(f))]
        whole = all(isinstance(_norm(x), int) for x in so + sn)
        s_old, s_new = round(sum(so), 6), round(sum(sn), 6)
        sums[str(f)] = {"old": int(s_old) if whole else s_old, "new": int(s_new) if whole else s_new,
                        "old_records": len(so), "new_records": len(sn), "integer_valued": whole}
    stamps = sorted(set(env_st) | rec_stamps, key=str)
    if shape or env_rest or added or removed or changed:
        status = "changed"
    elif stamps:
        status = "unchanged (only " + ", ".join(stamps) + " changed)"
    else:
        status = "unchanged"
    return {"status": status, "match": match, "record_rule": [o_rule, n_rule], "shape_changed": shape,
            "records": [len(o_recs), len(n_recs)], "alerts": n_rule == "list under alerts",
            "added": added, "removed": removed, "changed": changed,
            "envelope": {k: [_jv(x), _jv(y)] for k, (x, y) in env_rest.items()}, "stamps": stamps, "sums": sums}


def _unreadable_reason(p):
    return p.get(UNREADABLE) if isinstance(p, dict) and isinstance(p.get(UNREADABLE), str) else None


def diff_snapshots(old, new, requested=None):
    """D1 — Block 1's payloads against this run's, endpoint by endpoint: {endpoint: entry} in the order requested (default: new's),
    then the endpoints only Block 1 had ("not fetched this run"). An entry's status is changed · unchanged · "unchanged (only <key>
    changed)" · new endpoint · not fetched this run · unreadable; a compared entry records the match rule (id, a name-like key or
    position), the records added, removed and changed (field → [old, new]), the envelope keys that changed, the stamps and the sum of
    every numeric field that changed (booleans excluded). Never raises."""
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    names = [str(n) for n in (requested if requested is not None else list(new))]
    out = {}
    for ep in names:
        try:
            if ep not in new or _unreadable_reason(new[ep]) is not None:
                out[ep] = {"status": "unreadable", "reason": (_unreadable_reason(new.get(ep)) if ep in new else "no payload this run")}
            elif ep not in old:
                out[ep] = {"status": "new endpoint", "records": [0, len(records(new[ep]))], "alerts": is_alerts(new[ep])}
            elif _unreadable_reason(old[ep]) is not None:
                out[ep] = {"status": "unreadable", "reason": "Block 1's snapshot holds no readable payload: " + _unreadable_reason(old[ep])}
            else:
                out[ep] = _diff_endpoint(ep, old[ep], new[ep])
        except Exception as e:  # noqa: BLE001 — one endpoint that cannot be compared never stops the others
            out[ep] = {"status": "unreadable", "reason": f"not comparable ({type(e).__name__})"}
    for ep in old:
        if str(ep) not in out:
            out[str(ep)] = {"status": "not fetched this run"}
    return out


# ═══ BUILD IC-4 (D5): the data report — at T+0 and at the curveball, standard library only ═══
REPORT_CONSOLE_MAX = 12          # the console's lines: violations first, then the suspicions that touch a page's figures
PLACEHOLDER_VALUES = (999, 9999, 99999, -1, -99, -999)
_CAP_WORDS = ("capacity", "online", "available", "slots", "total", "beds", "units", "seats")
_USE_WORDS = ("inuse", "use", "used", "enrollment", "enrolled", "occupied", "active")


def _words_of(name):
    """A field's name as words: camelCase and separators split, lowered ("seatsOpenToday" → seats, open, today)."""
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", str(name or "")) if w]


def _is_use(f):
    """A field that counts a use (a camelCase name holding "InUse" or "Enrollment"): one of _USE_WORDS in its joined words."""
    return any(w in "".join(_words_of(f)) for w in _USE_WORDS)


def _is_cap(f):
    """A field that counts a capacity (a name holding "Online", "Available", "Total" or a unit noun): a word of _CAP_WORDS, and no
    use word."""
    return any(w in _words_of(f) for w in _CAP_WORDS) and not _is_use(f)


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _kind(v):
    return ("missing" if v is None or (isinstance(v, str) and not v.strip()) else "bool" if isinstance(v, bool) else "number" if _isnum(v)
            else "text" if isinstance(v, str) else "list" if isinstance(v, list) else "object" if isinstance(v, dict) else type(v).__name__)


def _profile(ep, payload):
    """One endpoint: its records, each field's kinds, missing values and range; its key and duplicates; candidate placeholders."""
    recs = [r for r in records(payload) if isinstance(r, dict)]
    fields = {}
    for r in recs:
        for f in r:
            fields.setdefault(f, [])
    for f in fields:
        fields[f] = [r.get(f) for r in recs]
    prof = {"endpoint": ep, "records": len(recs), "fields": {}, "placeholders": [], "duplicates": []}
    for f, vals in fields.items():
        kinds = sorted({_kind(v) for v in vals})
        nums = [v for v in vals if _isnum(v)]
        prof["fields"][f] = {"kinds": kinds, "missing": sum(1 for v in vals if _kind(v) == "missing"),
                             "range": [min(nums), max(nums)] if nums else None}
        for i, v in enumerate(vals):
            if _isnum(v) and v in PLACEHOLDER_VALUES:
                prof["placeholders"].append({"record": str(recs[i].get("id", i + 1)), "field": f, "value": v, "why": f"{v} is a common placeholder"})
    for r in recs:     # a capacity of 0 with a use above 0 in the same record
        uses = [f for f in r if _isnum(r[f]) and _is_use(f)]
        caps = [f for f in r if _isnum(r[f]) and _is_cap(f)]
        for c in caps:
            for u in uses:
                if r[c] == 0 and r[u] > 0:
                    prof["placeholders"].append({"record": str(r.get("id", "?")), "field": c, "value": 0,
                                                 "why": f"{c} is 0 while {u} is {r[u]}: a capacity of 0 with a use above 0"})
    ids = [str(r.get("id")) for r in recs if r.get("id") is not None]
    prof["duplicates"] = sorted({i for i in ids if ids.count(i) > 1})
    prof["key"] = "id" if ids and len(ids) == len(recs) and not prof["duplicates"] else None
    return prof


def _sums(profiles, raw):
    """{(endpoint, field or "a-b"): (sum, words)} over every multi-record endpoint's numeric fields and its capacity − use pairs."""
    out = {}
    for ep, payload in raw.items():
        recs = [r for r in records(payload) if isinstance(r, dict)]
        if len(recs) < 2:
            continue
        nf = sorted({f for r in recs for f in r if _isnum(r.get(f))})
        for f in nf:
            out[(ep, f)] = (sum(r[f] for r in recs if _isnum(r.get(f))), set(_words_of(f)))
        for a in nf:
            for b in nf:
                if a != b and _is_cap(a) and _is_use(b):
                    v = sum(r[a] - r[b] for r in recs if _isnum(r.get(a)) and _isnum(r.get(b)))     # IC-4 Phase 7 (review b19): as the registry
                    out[(ep, f"{a}-{b}")] = (v, set(_words_of(a)) | {"free", "available"})
    return out


def figure_value(ref, raw):
    """BUILD IC-4 (D3, D5): a contract figure (FIGURE_RX) read on `raw` → a number, or None. sum:<ep>.<f> and sum:<ep>.<a>-<b> (each record's
    a − b, as the registry reads it); <ep>.count; <ep>.<field> of a one-record endpoint; <ep>.<record id>.<field>; a quote's figure is read by the
    registry, not here (None)."""
    part = None
    if "#" in str(ref):
        ref, part = str(ref).rsplit("#", 1)
    ref = str(ref)
    if ref.startswith("q:"):
        return None
    if ref.startswith("sum:"):
        ep, _, f = ref[4:].partition(".")
        recs = [r for r in records(raw.get(ep)) if isinstance(r, dict)] if ep in raw else []
        a, _, b = f.partition("-")
        if not recs:
            return None
        if b:          # IC-4 Phase 7 (review b19): each record's a − b as the registry reads a sum: id — the figure the page prints
            return sum(r[a] - r[b] for r in recs if _isnum(r.get(a)) and _isnum(r.get(b)))
        return sum(r[a] for r in recs if _isnum(r.get(a)))
    parts = ref.split(".")
    ep = parts[0]
    recs = [r for r in records(raw.get(ep)) if isinstance(r, dict)] if ep in raw else []
    if len(parts) == 2:
        if parts[1] == "count":
            return len(recs)
        return recs[0].get(parts[1]) if len(recs) == 1 and _isnum(recs[0].get(parts[1])) else None
    if len(parts) == 3:
        r = next((r for r in recs if str(r.get("id")) == parts[1]), None)
        return r.get(parts[2]) if r and _isnum(r.get(parts[2])) else None
    return None


def _holds(a, op, b):
    return {"<=": a <= b, "<": a < b, "==": a == b, ">=": a >= b, ">": a > b}[op]


def data_report(raw, summary=None, contract=None, block1_raw=None, lookup=None):
    """BUILD IC-4 (D5): the report on this run's data — before the first model call, and again at the curveball. Each endpoint: loaded or
    not (the data step's error), its records, its fields' kinds, missing values and ranges, its key and duplicates, candidate placeholders
    (flagged, never deleted). Across endpoints: a one-record endpoint's figure against the sum of a multi-record endpoint's records (a field
    sharing a word with it), the same entity in two endpoints (a name, or its part before a parenthesis, in both). The contract's
    confirmed relations checked. Three classes: violation (a confirmed relation fails), suspicion (a candidate rule fails — the operator's
    to confirm or drop), cannot be checked (a relation whose figure is not read). At the curveball (block1_raw): the field-path diff
    (added, removed, retyped), removed→added pairs whose values overlap (rename candidates), the endpoints that did not load.
    `lookup(ref)` reads a figure the data alone does not (a quote form, a figure Python derives such as a set's free total — the
    registry's), else such a relation cannot be checked. Returns {"endpoints",
    "findings": [{"class", "what", "where"}], "drafts": {"relations", "aliases", "authority", "outcomes"}, "console": [≤ REPORT_CONSOLE_MAX
    lines]}. BUILD IC-4 (D6): the outcomes — one draft per winning line the contract does not yet confirm as a core outcome (the brief's
    own words, id out-<the line's id>; the contract holds no objectives field).
    Never raises."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"endpoints": [], "findings": [], "drafts": {"relations": [], "aliases": [], "authority": [], "outcomes": []}, "console": []}
    try:
        conf_ = [o for o in (contract.get("outcomes") or []) if isinstance(o, dict)] if isinstance(contract, dict) else []
        done_ = {str(o.get(k) or "").strip().lower() for o in conf_ for k in ("id", "words", "quote")} - {""}
        for w in (contract.get("winning_looks_like") or []) if isinstance(contract, dict) else []:      # BUILD IC-4 (D6): an id of its
            if isinstance(w, dict) and isinstance(w.get("id"), str) and SLUG.match(w["id"]) and _text(w.get("text")):   # own (ids are one
                t_, i_ = re.sub(r"\s+", " ", w["text"]).strip(), f"out-{w['id']}"                                      # namespace)
                if i_ not in done_ and t_.lower() not in done_:
                    out["drafts"]["outcomes"].append({"id": i_, "words": t_, "quote": t_})
        errs = {str(x.get("endpoint")): str(x.get("error")) for x in summary or [] if isinstance(x, dict) and x.get("error")}
        profs = {}
        for ep, why in errs.items():
            out["endpoints"].append({"endpoint": ep, "loaded": False, "why": why})
            out["findings"].append({"class": "suspicion", "what": f"{ep} did not load ({why[:120]}): its figures are \"source unavailable\", never zero",
                                    "where": ep})
        for ep, payload in raw.items():
            pr = _profile(ep, payload)
            profs[ep] = pr
            out["endpoints"].append(dict(pr, loaded=True))
            if pr["records"] == 0:
                out["findings"].append({"class": "suspicion", "what": f"{ep} loaded with no records: its figures are \"source unavailable\"", "where": ep})
            for ph in pr["placeholders"]:
                out["findings"].append({"class": "suspicion", "what": f"a candidate placeholder: {ph['why']}", "where": f"{ep}.{ph['record']}.{ph['field']}"})
            for d in pr["duplicates"]:
                out["findings"].append({"class": "suspicion", "what": f"two records share the id {d}", "where": ep})
        sums = _sums(profs, raw)
        for ep, payload in raw.items():         # a summary's figure against the sum of records sharing a word with it
            recs = [r for r in records(payload) if isinstance(r, dict)]
            if len(recs) != 1:
                continue
            for f, v in recs[0].items():
                if not _isnum(v) or isinstance(v, float) and not float(v).is_integer():
                    continue
                words = set(_words_of(f))
                fw = words - {"total", "now", "count", "num", "current"}
                free_like = bool(words & {"available", "free", "open", "vacant", "remaining"})
                cands = []      # (words in common, the kind fits, endpoint, field) — one candidate per summary figure, never a guess
                for (ep2, f2), (sv, w2) in sums.items():
                    if ep2 == ep or not (fw & w2):
                        continue
                    diff = "-" in f2
                    if not diff and not _is_cap(f2):
                        continue                         # a use is no total and no free count
                    cands.append((len(fw & w2), diff == free_like, ep2, f2, sv))
                cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
                if not cands or (len(cands) > 1 and cands[0][:2] == cands[1][:2]):
                    continue                             # none, or two as good: the report does not guess
                _n, _k, ep2, f2, sv = cands[0]
                if sv == v:
                    continue
                rel = {"id": re.sub(r"[^a-z0-9-]+", "-", f"{ep}-{f}-vs-{ep2}-{f2}".lower()).strip("-")[:80], "left": f"{ep}.{f}",
                       "op": "<=", "right": f"sum:{ep2}.{f2}", "words": f"{ep}'s {f} is at most the {ep2} records' {f2.replace('-', ' − ')}"}
                out["drafts"]["relations"].append(rel)
                m_ = re.sub(r"[^a-z0-9-]+", "-", f"{f}".lower()).strip("-")[:40] or "figure"     # IC-4 Phase 7 (review b18): one metric,
                for fig_ in (f"{ep}.{f}", f"sum:{ep2}.{f2}"):                                    # two figures — the records named to speak
                    out["drafts"]["authority"].append({"metric": m_, "figure": fig_, "endpoint": ep2, "as_of": AUTHORITY_AS_OF[0],
                                                       "words": f"{f}: {ep}'s figure and the {ep2} records' {f2.replace('-', ' − ')} summed are one "
                                                                f"metric; the draft names the {ep2} records to speak — the operator confirms or changes it"})
                out["findings"].append({"class": "suspicion", "what": f"{ep}.{f} = {v} against the {ep2} records' {f2.replace('-', ' − ')} summed: {sv}",
                                        "where": f"{ep}.{f}", "touches_page": True})
        names = {}           # the same entity across endpoints
        for ep, payload in raw.items():
            for r in records(payload):
                if not isinstance(r, dict):
                    continue
                for f, v in r.items():
                    if isinstance(v, str) and re.search(r"(?i)name$", f) and v.strip():
                        names.setdefault(re.split(r"\s*\(", v, 1)[0].strip().lower(), set()).add(f"{ep}.{r.get('id', '?')}")
                        for w in re.findall(r"\b[A-Z][A-Z]{3,}\b", v):      # a name's word in capitals (BRIDGES), as the veto reads it
                            names.setdefault(w, set()).add(f"{ep}.{r.get('id', '?')}")
        out["same_entity"] = sorted([sorted(v) for v in names.values() if len({x.split('.')[0] for x in v}) > 1])
        for rel in (contract or {}).get("relations") or []:        # the contract's confirmed relations
            a, b = (figure_value(rel[x], raw) for x in ("left", "right"))
            a = lookup(rel["left"]) if a is None and lookup else a
            b = lookup(rel["right"]) if b is None and lookup else b
            if a is None or b is None:
                out["findings"].append({"class": "cannot be checked", "what": f"{rel['id']}: {rel['words']} — a figure is not read this run", "where": rel["id"]})
            elif not _holds(a, rel["op"], b):
                out["findings"].append({"class": "violation", "what": f"{rel['id']}: {rel['words']} — {a} {rel['op']} {b} fails", "where": rel["id"],
                                        "touches_page": True})
        if isinstance(block1_raw, dict):           # at the curveball: the field paths, the rename candidates
            for ep in sorted(set(block1_raw) | set(raw)):
                p1 = _profile(ep, block1_raw[ep]) if ep in block1_raw else None
                p2 = profs.get(ep)
                if p1 and not p2:
                    out["findings"].append({"class": "suspicion", "what": f"{ep} was in Block 1's data and did not load this run", "where": ep})
                    continue
                if not p1 or not p2:
                    continue
                f1, f2 = set(p1["fields"]), set(p2["fields"])
                for f in sorted(f2 - f1):
                    out["findings"].append({"class": "suspicion", "what": f"a field added: {ep}.{f}", "where": f"{ep}.{f}"})
                for f in sorted(f1 - f2):
                    out["findings"].append({"class": "suspicion", "what": f"a field removed: {ep}.{f} (a figure citing it is an unresolved binding "
                                            "unless the contract's aliases map it)", "where": f"{ep}.{f}", "touches_page": True})
                for f in sorted(f1 & f2):
                    k1 = [k for k in p1["fields"][f]["kinds"] if k != "missing"]
                    k2 = [k for k in p2["fields"][f]["kinds"] if k != "missing"]
                    if k1 and k2 and k1 != k2:
                        out["findings"].append({"class": "suspicion", "what": f"retyped: {ep}.{f} {'/'.join(k1)} → {'/'.join(k2)}", "where": f"{ep}.{f}"})
                r1 = [r for r in records(block1_raw[ep]) if isinstance(r, dict)]
                r2 = [r for r in records(raw[ep]) if isinstance(r, dict)]
                one = (len(f1 - f2) == 1 and len(f2 - f1) == 1        # IC-4 Phase 7 (review b16): the one field lost and the one gained,
                       and [k for k in p1["fields"][sorted(f1 - f2)[0]]["kinds"] if k != "missing"]        # of one kind — a rename candidate
                       == [k for k in p2["fields"][sorted(f2 - f1)[0]]["kinds"] if k != "missing"])        # though the values moved
                for old in sorted(f1 - f2):
                    v1 = [str(r.get(old)) for r in r1 if r.get(old) is not None]
                    for new in sorted(f2 - f1):
                        v2 = [str(r.get(new)) for r in r2 if r.get(new) is not None]
                        if v1 and v2 and (len(set(v1) & set(v2)) >= max(1, len(set(v1)) // 2) or one):
                            out["drafts"]["aliases"].append({"old": f"{ep}.{old}", "new": f"{ep}.{new}",
                                                             "why": f"removed and added at the curveball; {len(set(v1) & set(v2))} values in common"})
                            out["findings"].append({"class": "suspicion", "what": f"a rename candidate: {ep}.{old} → {ep}.{new}", "where": f"{ep}.{old}",
                                                    "touches_page": True})
        order = {"violation": 0, "suspicion": 1, "cannot be checked": 2}
        top = sorted(out["findings"], key=lambda x: (order[x["class"]], not x.get("touches_page")))
        n = {k: sum(1 for x in out["findings"] if x["class"] == k) for k in order}
        out["console"] = [f"data report: {len(raw)} endpoint(s) loaded, {len(errs)} not; {n['violation']} violation(s), {n['suspicion']} "
                          f"suspicion(s), {n['cannot be checked']} relation(s) that cannot be checked"]
        # BUILD IC-5 (D12, G17): the lines list every finding the count line counts (R6 counted 3 suspicions and listed 2), in the same
        # order — violations, the suspicions that touch a page's figures, the rest; past the console's lines, the rest by number and file
        room = REPORT_CONSOLE_MAX - 1
        shown = top if len(top) <= room else top[:room - 1]
        out["console"] += [f"  {x['class']}: {x['what']}" for x in shown]
        if len(shown) < len(top):
            out["console"].append(f"  … and {len(top) - len(shown)} more in checkpoints/data_report.json")
    except Exception as e:  # noqa: BLE001 — a report that cannot be made is said, never a stopped run
        out["console"] = [f"data report: not made ({type(e).__name__}: {str(e)[:120]})"]
    return out


def write_drafts(report, contract_path):
    """BUILD IC-4 (D5): the report's candidate entries into <challenge>.draft.json beside the contract — never into the contract: an
    unconfirmed entry is a suspicion. Returns the path written, or None. Never raises."""
    try:
        p = Path(contract_path)
        dp = p.with_name(p.name.replace(".json", ".draft.json"))
        tmp = dp.with_name(f".{dp.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"schema": 1, "contract": p.name, "drafts": report.get("drafts") or {}}, indent=1, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, dp)
        return dp
    except Exception:  # noqa: BLE001
        return None


def confirm(draft_path, items):
    """BUILD IC-4 (D5): `naipc_requirements.py confirm <draft> --items <block>:<n>,…` — copies the named draft entries into the contract
    beside the draft (relations[2], aliases[0] …), validates the result and writes it only when valid. Returns (n confirmed, problems)."""
    dp = Path(draft_path)
    d = json.loads(dp.read_text(encoding="utf-8"))
    cp = dp.with_name(d.get("contract") or dp.name.replace(".draft.json", ".json"))
    doc = json.loads(cp.read_text(encoding="utf-8"))
    n = 0
    for it in items:
        blk, _, i = str(it).partition(":")
        lst = ((d.get("drafts") or {}).get(blk) or [])
        if not i.isdigit() or int(i) >= len(lst):
            return 0, [f"{it}: no draft entry {blk}[{i}]"]
        doc.setdefault(blk, [])
        if lst[int(i)] not in doc[blk]:
            doc[blk].append(lst[int(i)])
            n += 1
    probs = validate(doc, cp.name[:-5])
    if probs:
        return 0, probs
    tmp = cp.with_name(f".{cp.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, cp)
    return n, []


def api_changed_of(entries):
    """D1: the endpoints the diff found changed, new or unreadable (a missing or unreadable payload counts — never assumed current)."""
    return [ep for ep, e in (entries or {}).items() if e.get("status") in ("changed", "new endpoint", "unreadable")]


def _fmt(v, n=40):
    if isinstance(v, dict) and v == ABSENT:
        return "(absent)"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(_norm(round(v, 6)) if isinstance(v, float) else v)
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def fmt_value(v, n=40):
    """A value as the change list prints it: text cut at `n` characters with "…", numbers plain, an absent side "(absent)"."""
    return _fmt(v, n)


def _counts(e):
    parts = [f"+{len(e['added'])}" if e.get("added") else "", f"−{len(e['removed'])}" if e.get("removed") else "",
             f"~{len(e['changed'])}" if e.get("changed") else ""]
    parts = [p for p in parts if p]
    return " ".join(parts) + " records" if parts else "no record changes"


def summary_line(ep, e):
    """D1's console line for an endpoint that changed: '  <ep>: +a −r ~c records · <key> <old> → <new> · sums <field> <old> → <new>'
    (stamps stay off the line; only integer-valued sums are printed — a sum is data for the update call, never a page figure)."""
    line = f"  {ep}: {_counts(e)}"
    if e.get("shape_changed"):
        line += f" · shape {e['shape_changed'][0]} → {e['shape_changed'][1]}"
    for k, (a, b) in (e.get("envelope") or {}).items():
        line += f" · {k} {_fmt(a)} → {_fmt(b)}"
    ints = [(f, s) for f, s in (e.get("sums") or {}).items() if s.get("integer_valued")]
    if ints:
        line += " · sums " + ", ".join(f"{f} {_fmt(s['old'])} → {_fmt(s['new'])}" for f, s in ints)
    return line


def signals_line(diff, announced, contract):
    """D1: the three signals on one line — the diff's api_changed, the announcement's (None = not stated) and the contract's
    curveball_updates flags — ending "(agree)" or "(differ: <endpoint> not in <signal>; …)"."""
    def L(s):
        return ", ".join(sorted(s)) if s else "none"
    given = ([("the diff", set(diff))] if diff is not None else []) + [("the contract", set(contract or []))] \
        + ([("the announcement", set(announced))] if announced is not None else [])
    union = set().union(*(s for _, s in given)) if given else set()
    gaps = []
    for ep in sorted(union):
        missing = [n for n, s in given if ep not in s]
        if missing:
            gaps.append(f"{ep} not in " + " or ".join(missing))
    tail = "(agree)" if not gaps else "(differ: " + "; ".join(gaps) + ")"
    d = L(diff) if diff is not None else "not computed (no Block 1 snapshot)"
    a = L(announced) if announced is not None else "not stated"
    return f"  api_changed: the diff {d} · the announcement's {a} · the contract expected {L(contract or [])} {tail}"


def changes_summary(entries, announced=None, contract=None):
    """D1's printed summary: one line per endpoint that changed, a line for every endpoint neither changed nor unchanged, then
    '  <k> endpoint(s) unchanged', then the three-signal line. `entries` None = no Block 1 snapshot (the announcement only)."""
    if entries is None:
        return ["  no Block 1 snapshot: the change list holds the announcement only", signals_line(None, announced, contract)]
    lines = [summary_line(ep, e) for ep, e in entries.items() if e.get("status") == "changed"]
    for ep, e in entries.items():
        st = e.get("status", "")
        if st.startswith("unchanged (only"):
            lines.append(f"  {ep}: {st}")
        elif st == "new endpoint":
            lines.append(f"  {ep}: new endpoint ({(e.get('records') or [0, 0])[1]} records)")
        elif st in ("not fetched this run", "unreadable"):
            lines.append(f"  {ep}: {st}" + (f" — {e['reason']}" if e.get("reason") else ""))
    lines.append(f"  {sum(1 for e in entries.values() if e.get('status') == 'unchanged')} endpoint(s) unchanged")
    lines.append(signals_line(api_changed_of(entries), announced, contract))
    return lines


def contract_curveball_flags(doc):
    """The endpoints the contract expects to change at the curveball (curveball_updates true)."""
    try:
        return [e["name"] for e in doc["api"]["endpoints"] if e.get("curveball_updates") is True]
    except (KeyError, TypeError):
        return []


def make_changes(challenge, run_id, block1_run_id, snapshot, curveball, entries, contract_flags=None, announced=None):
    """D1 + D2: the change list — {schema, challenge, run_id (Block 2's), block1_run_id, snapshot {path, source}, curveball (the
    sidecar's fields with api_changed from the diff), api_changed, announced_api_changed (a sidecar's own list; None for a text
    announcement), contract_curveball_updates, endpoints, summary, written_at}. `entries` None = no snapshot (the announcement only)."""
    cv = dict(curveball) if isinstance(curveball, dict) else None
    announced = list(announced) if isinstance(announced, list) else None
    diff = api_changed_of(entries) if entries is not None else None
    if cv is not None:
        cv["api_changed"] = list(diff or [])
    return {"schema": CHANGES_SCHEMA, "challenge": challenge, "run_id": run_id, "block1_run_id": block1_run_id,
            "snapshot": snapshot, "curveball": cv, "api_changed": diff, "announced_api_changed": announced,
            "contract_curveball_updates": list(contract_flags or []), "endpoints": entries,
            "summary": changes_summary(entries, announced, contract_flags), "written_at": _utc_iso()}


def _write_json(path, doc):
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes((json.dumps(doc, indent=1, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as e:  # noqa: BLE001 — reported, never raised
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"not written: {type(e).__name__}: {e}"


def _read_json(path):
    """(object, None) or (None, reason) — utf-8-sig, never raises."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, f"not found: {path}"
    except Exception as e:  # noqa: BLE001
        return None, f"not readable: {type(e).__name__}"
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except UnicodeDecodeError as e:
        return None, f"not UTF-8 text (byte {e.start})"
    except Exception as e:  # noqa: BLE001 — invalid JSON, absurd nesting
        return None, f"invalid JSON ({type(e).__name__})"


def read_json(path):
    """(object, None) or (None, reason) — any JSON file this module's readers accept (utf-8-sig); never raises."""
    return _read_json(path)


def write_json(path, doc):
    """None or the reason a JSON file was not written (UTF-8, temp file + os.replace); never raises."""
    return _write_json(path, doc)


def snapshot_path(landing_dir, challenge):
    return Path(landing_dir) / RUNS_DIR / str(challenge) / SNAPSHOT_FILE


def changes_path(landing_dir, challenge, run_id):
    return Path(landing_dir) / RUNS_DIR / str(challenge) / f"changes_{run_id}.json"


def write_snapshot(path, challenge, run_id, shape, payloads):
    """D1: Block 1's snapshot {schema 1, challenge, run_id, shape, taken_at, payloads} (UTF-8, temp file + os.replace) → None or the
    reason it was not written; never raises (a failed write never stops Block 1)."""
    return _write_json(path, {"schema": SNAPSHOT_SCHEMA, "challenge": challenge, "run_id": run_id, "shape": shape,
                              "taken_at": _utc_iso(), "payloads": payloads})


def read_snapshot(path, challenge):
    """D1: (snapshot, None) or (None, why it is ignored) — schema 1, this challenge, payloads an object; never raises."""
    d, why = _read_json(path)
    if d is None:
        return None, why
    if not isinstance(d, dict) or d.get("schema") != SNAPSHOT_SCHEMA:
        return None, "not a schema-1 snapshot"
    if d.get("challenge") != challenge:
        return None, f"a snapshot of {d.get('challenge')!r}, not {challenge!r}"
    if not isinstance(d.get("payloads"), dict):
        return None, "no payloads"
    return d, None


def block1_data(cp_dir, challenge):
    """D1's second source: the Oracle's Block 1 base — <checkpoints>/block1/data_loading.json when its meta.json names this challenge.
    Returns ({endpoint: payload}, meta, None) or (None, None, reason); never raises."""
    base = Path(cp_dir) / BLOCK1_DIR
    meta, why = _read_json(base / BLOCK1_META)
    if meta is None:
        return None, None, f"no Oracle Block 1 base ({why})"
    if not isinstance(meta, dict) or meta.get("challenge") != challenge:
        return None, None, f"the Oracle's Block 1 base is of {meta.get('challenge') if isinstance(meta, dict) else None!r}, not {challenge!r}"
    dl, why = _read_json(base / "data_loading.json")
    raw = dl.get("data", {}).get("raw") if isinstance(dl, dict) and isinstance(dl.get("data"), dict) else None
    if not isinstance(raw, dict):
        return None, None, f"the Oracle's Block 1 data unreadable ({why or 'no raw payloads'})"
    return raw, meta, None


def block1_payloads(landing_dir, challenge, cp_dir=None):
    """BUILD B2: Block 1's payloads ({endpoint: payload}) for the engine at Block 2 — the pipeline's snapshot (runs/<challenge>/block1.json),
    else the Oracle's Block 1 base (<cp_dir>/block1/data_loading.json); None when neither is readable. Never raises."""
    try:
        snap, _why = read_snapshot(snapshot_path(landing_dir, challenge), challenge)
        if snap is not None:
            return snap["payloads"]
        if cp_dir is not None:
            raw, _meta, _why2 = block1_data(cp_dir, challenge)
            return raw
    except Exception:  # noqa: BLE001 — a missing base only means the engine cannot tell a typo from a record gone
        return None
    return None


def write_changes(path, doc):
    return _write_json(path, doc)


def read_changes(path, challenge=None, run_id=None):
    """(change list, None) or (None, why it is ignored): schema 1, and when given, this challenge and this run; never raises."""
    d, why = _read_json(path)
    if d is None:
        return None, why
    if not isinstance(d, dict) or d.get("schema") != CHANGES_SCHEMA:
        return None, "not a schema-1 change list"
    if challenge is not None and d.get("challenge") != challenge:
        return None, f"a change list of {d.get('challenge')!r}, not {challenge!r}"
    if run_id is not None and d.get("run_id") != run_id:
        return None, f"a change list of run {d.get('run_id')}, this run is {run_id}"
    return d, None


def changes_from_env():
    """D1: the change list pipeline.py exported (NAIPC_CHANGES), used only when its run_id is NAIPC_RUN_ID and its challenge is
    NAIPC_CHALLENGE — (doc, None) or (None, reason); a Block 1 run has none. Never raises."""
    p = os.environ.get("NAIPC_CHANGES", "").strip()
    if not p:
        return None, "NAIPC_CHANGES not set"
    rid = os.environ.get("NAIPC_RUN_ID", "").strip() or None
    if rid is None:
        return None, "NAIPC_RUN_ID not set — a change list is used only by the run that made it"
    return read_changes(p, os.environ.get("NAIPC_CHALLENGE", "").strip() or None, rid)


def read_curveball_json(path, challenge=None, run_id=None):
    """D3/D7: the Oracle's checkpoints/curveball.json → (doc, None) or (None, why it is ignored); never raises."""
    d, why = _read_json(path)
    if d is None:
        return None, why
    if not isinstance(d, dict) or d.get("schema") != CHANGES_SCHEMA:
        return None, "not a schema-1 curveball file"
    if challenge is not None and d.get("challenge") != challenge:
        return None, f"of {d.get('challenge')!r}, not {challenge!r}"
    if run_id is not None and d.get("run_id") != run_id:
        return None, f"written by run {d.get('run_id')}, this run is {run_id}"
    return d, None


def write_curveball_json(path, doc):
    return _write_json(path, doc)


def curveball_input(path, doc=None):
    """D2 — the announcement from a file: (curveball, note, problems). A .json file is a sidecar merged through merge_curveball (the
    contract `doc` is required); any other file is the announcement's own text — a UTF-16 byte-order mark (FF FE / FE FF) → UTF-16,
    else utf-8-sig, else cp1252 (the note names the encoding used); line ends become LF; a NUL character, or nothing but white space,
    is refused. The curveball is {kind, source, announced_at, text, tests, api_changed}. Never raises."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None, "", [f"curveball file: not found: {p}"]
    except Exception as e:  # noqa: BLE001
        return None, "", [f"curveball file: not readable ({type(e).__name__})"]
    if p.suffix.lower() == ".json":
        if doc is None:
            return None, "", ["curveball file: a .json sidecar needs the challenge contract (pipeline.py --requirements; a harness run on its own reads NAIPC_REQUIREMENTS) — or give the announcement as a text file"]
        sc, problems = parse(raw, "sidecar")
        if problems:
            return None, "", problems
        try:
            merged = merge_curveball(doc, sc)
        except RequirementsError as e:
            return None, "", e.problems
        except Exception as e:  # noqa: BLE001
            return None, "", [f"sidecar: not usable ({type(e).__name__})"]
        cv = dict(merged["curveball"])
        return cv, (f"sidecar {p.name} — {cv.get('kind')}, {len(cv.get('text') or '')} characters, {len(cv.get('tests') or [])} test(s), "
                    f"announced api_changed: {', '.join(cv.get('api_changed') or []) or 'none'}"), []
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text, enc = raw.decode("utf-16"), "UTF-16 " + ("LE" if raw[:2] == b"\xff\xfe" else "BE")
        except UnicodeDecodeError:
            return None, "", ["curveball file: a UTF-16 byte-order mark but not UTF-16 text"]
    else:
        try:
            text, enc = raw.decode("utf-8-sig"), "UTF-8" + (" with a BOM" if raw[:3] == b"\xef\xbb\xbf" else "")
        except UnicodeDecodeError:
            try:
                text, enc = raw.decode("cp1252"), "cp1252 (not UTF-8)"
            except UnicodeDecodeError:
                return None, "", ["curveball file: not text in UTF-8, UTF-16 or cp1252"]
    if "\x00" in text:
        return None, "", ["curveball file: holds a NUL character — not a text file (saved as UTF-16 without its byte-order mark?)"]
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None, "", ["curveball file: empty — the announcement's text is required"]
    cv = {"kind": "VERBATIM", "source": p.name, "announced_at": None, "text": text, "tests": [], "api_changed": []}
    return cv, f"text {p.name} — {enc}, {len(text)} characters", []


def curveball_refs(changes):
    """D3/D7: the refs an update reply may cite — "announcement", every endpoint the diff found changed, new or unreadable, and every
    added, removed or changed record's ref. {lowercased ref: ref}."""
    refs = {"announcement": "announcement"}
    for ep, e in ((changes or {}).get("endpoints") or {}).items():
        if e.get("status") in ("changed", "new endpoint", "unreadable"):
            refs[ep.lower()] = ep
            for kind in ("added", "removed", "changed"):
                for r in e.get(kind) or []:
                    refs[str(r.get("ref")).lower()] = str(r.get("ref"))
    return refs


def name_cut(values, quote=True, n=None):
    """P0.7 r2 (E3): names the model wrote (unknown refs, keys, ids) as one plain string cut to NAME_CHARS — each as JSON writes it
    when `quote` (so "" and a number stay visible), else a single name as text. The caller escapes it for a page."""
    vals = list(values or [])
    if not quote and len(vals) == 1 and isinstance(vals[0], str):
        s = vals[0]
    else:
        s = ", ".join(json.dumps(v, ensure_ascii=False, default=str) for v in vals)
    return cut(" ".join(s.split()), n or NAME_CHARS)


def ref_check(because, refs):
    """P0.7 r2: the reasons an update gave for one part → (the refs as the change list writes them, None) or ([], the plain reason).
    A text or a list; each item must be a ref of the change list (`refs`: {lowercased: ref}, as curveball_refs gives) — an item that
    is not text is not a ref of the list (P0.7 left it out silently). One rule for amendments, assumption changes and brief answers."""
    items = [] if because is None else (list(because) if isinstance(because, list) else [because])
    if not items:
        return [], "no change was named as its reason"
    unknown = [x for x in items if not (isinstance(x, str) and x.strip().lower() in refs)]
    if unknown:
        return [], f"its reason is not a change in the list ({name_cut(unknown)})"
    return [refs[x.strip().lower()] for x in items], None


# P0.7 r2 Phase 5 (C4): `because` given as anything but an object keyed by part — the reasons cannot be matched to a part
BECAUSE_NOT_BY_PART = "the reasons were not given part by part — none names a change for it"


def _keys_head(keys):
    ks = [str(k) for k in keys or []]
    return ks, name_cut(ks[:3]) + (f" and {len(ks) - 3} more" if len(ks) > 3 else "")


def extra_keys(raw, stored):
    """P0.7 r2 Phase 5 (C2): the keys of the objects in `raw` (a part of an update reply) that `stored` (its stored form, the model's
    model_dump) lacks — keys a typed item does not declare, which pydantic ignores and its stored form drops. Objects and lists are
    walked in parallel (lists item by item); a free-key dict keeps every key, so it yields none; a key the stored form adds (a default)
    is not reported. Distinct, in the order met; never raises."""
    out = []

    def walk(r, s):
        if isinstance(r, dict) and isinstance(s, dict):
            for k, v in r.items():
                if k not in s:
                    if k not in out:
                        out.append(k)
                else:
                    walk(v, s[k])
        elif isinstance(r, list) and isinstance(s, list):
            for a, b in zip(r, s):
                walk(a, b)
    try:
        walk(raw, stored)
    except Exception:  # noqa: BLE001
        pass
    return out


def extra_keys_reason(keys):
    """P0.7 r2 Phase 5 (C2): the plain reason for the keys extra_keys found — 'its key "<k>" is not in the format — not used; the rest is
    applied' (several: 'its keys … are …'; past three: '… and <n> more'); the names as JSON writes them, cut to NAME_CHARS together."""
    ks, head = _keys_head(keys)
    return (f"its key {head} is" if len(ks) == 1 else f"its keys {head} are") + " not in the format — not used; the rest is applied"


def outside_text_reason(n, kept):
    """P0.7 r2 Phase 5 (C16): the plain reason for text outside an update reply's JSON object — '<n> characters of text outside its JSON
    object — not used', and ' (the reply file keeps them)' when the reply file was written (E3 d). One rule for the Oracle and the Muse."""
    return f"{n:,} characters of text outside its JSON object — not used" + (" (the reply file keeps them)" if kept else "")


def unknown_keys_reason(keys):
    """P0.7 r2 Phase 5 (C14): the plain reason for top-level keys of an update reply that the update does not ask for — 'its key "<k>" is
    not one the update asks for — not used' (several: 'its keys … are not ones …'; past three: '… and <n> more')."""
    ks, head = _keys_head(keys)
    return (f"its key {head} is not one" if len(ks) == 1 else f"its keys {head} are not ones") + " the update asks for — not used"


def duplicate_keys(text):
    """P0.7 r2 Phase 5 (review #7 a): the keys given more than once in ONE object of a JSON text — json.loads keeps the last value of each,
    silently. At any depth, in lists too; the same key in two objects is not a duplicate; distinct, in the order json finishes reading each
    object (an inner object before the one holding it); [] when the text is not JSON or not text. Never raises.
    BUILD IC-3 (D4): found by the one strict decoder (strict_loads) — one reading of a key given twice, wherever it is asked."""
    try:
        _obj, found = strict_loads(text)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in found:
        if x["key"] not in out:
            out.append(x["key"])
    return out


def duplicate_keys_reason(keys):
    """P0.7 r2 Phase 5 (review #7 a): 'its key "<k>" is given more than once in one object — only the last is used' (several: 'its keys … are
    given more than once in one object — only the last of each is used'; past three: '… and <n> more')."""
    ks, head = _keys_head(keys)
    return (f"its key {head} is given more than once in one object — only the last is used" if len(ks) == 1 else
            f"its keys {head} are given more than once in one object — only the last of each is used")


def orphan_reasons(amended, because):
    """P0.7 r2 Phase 5 (review #7 c): the keys of `because` (an object) that name no key of `amended` (an object) — reasons given for a part
    the update did not amend; [] when either is not an object (a `because` that is not an object is merge_amended's C4 case)."""
    if not isinstance(amended, dict) or not isinstance(because, dict):
        return []
    have = {str(k) for k in amended}
    return [str(k) for k in because if str(k) not in have]


def orphan_reasons_reason(keys):
    """P0.7 r2 Phase 5 (review #7 c): 'its key "<k>" names no part the update amended — not used' (several: 'its keys … name no part …')."""
    ks, head = _keys_head(keys)
    return f"its key {head} names no part the update amended — not used" if len(ks) == 1 else f"its keys {head} name no part the update amended — not used"


def _keep_detail(details, field, detail):
    if isinstance(details, dict) and detail:
        details[field] = cut(str(detail), DETAIL_CHARS)


def merge_amended(base, amended, because, allowed, refs, check=None, details=None):
    """D3, D7 — Python decides which amendments stand. Returns (merged, kept, dropped): a deep copy of `base` with each amended field
    that is allowed, names at least one reason, names only known refs (`refs`: {lowercased: ref}, as curveball_refs gives) and passes
    `check(field, value)`; kept = [(field, [refs])], dropped = [(field, reason)] — every other field is the base's, unchanged. A field
    may be dotted once ("strategic.personas"). Never raises.
    P0.7 r2 (E3 a): `check` returns None (valid), a plain reason, or (plain reason, technical detail); every reason here is in plain
    words; a key that is not allowed is named cut to NAME_CHARS; `details` (a dict, optional) receives {field: technical detail}.
    P0.7 r2 Phase 5 (C4): reasons given, but not as an object keyed by part (a list, a text) → each allowed amendment's reason is
    BECAUSE_NOT_BY_PART (true: they were given, and cannot be matched), the shape in its detail; None or an object → as before."""
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    kept, dropped = [], []
    if not isinstance(amended, dict):
        if amended not in (None, {}):
            dropped.append(("amended", "not given as named fields — nothing was amended"))
            _keep_detail(details, "amended", f"a {type(amended).__name__}, not an object")
        return merged, kept, dropped
    not_by_part = because is not None and not isinstance(because, dict)
    because_type = type(because).__name__
    because = because if isinstance(because, dict) else {}
    allowed = set(allowed or ())
    for field, value in amended.items():
        f = str(field)
        try:
            if f not in allowed:
                dropped.append((name_cut([f], quote=False), "not a part an update may amend"))
                continue
            rs, why = ref_check(because.get(f), refs)
            if why:
                dropped.append((f, BECAUSE_NOT_BY_PART if not_by_part else why))
                if not_by_part:
                    _keep_detail(details, f, f"because is a {because_type}, not an object")
                continue
            res = check(f, value) if check else None
            if res:
                why, det = res if isinstance(res, tuple) else (str(res), "")
                dropped.append((f, str(why)))
                _keep_detail(details, f, det)
                continue
            if "." in f:
                top, sub = f.split(".", 1)
                if not isinstance(merged.get(top), dict):
                    dropped.append((f, "Block 1 has no such part"))
                    continue
                merged[top][sub] = value
            else:
                merged[f] = value
            kept.append((f, rs))
        except Exception as e:  # noqa: BLE001 — one malformed amendment never stops the others
            dropped.append((name_cut([f], quote=False), "not in a form Python can use"))
            _keep_detail(details, name_cut([f], quote=False), type(e).__name__)
    return merged, kept, dropped


# ═══ P0.7 r2 — the update path never silent: one marked cut (E2), the schema an update may change (E1), plain reasons (E3) ═══

_CUT_TRAIL = re.compile(r"[\s,;:—\-…]+$")
_CUT_OPEN = (("[", re.compile(r"\["), "]"), ("<", re.compile(r"<(?=[A-Za-z/!])"), ">"))


def _cut_len(s, unit):
    return len(s.encode("utf-8")) if unit == "bytes" else len(s)


def _cut_prefix(text, budget, unit):
    """The longest prefix of `text` within `budget` (characters, or UTF-8 bytes without splitting a character), moved back to its
    last white space when it ends inside a word (no white space: kept at that length), then back before an open '[' or tag '<',
    then its trailing , ; : — - white space and '…' removed."""
    if budget <= 0:
        return ""
    p = text.encode("utf-8")[:budget].decode("utf-8", "ignore") if unit == "bytes" else text[:budget]
    if len(p) < len(text) and not text[len(p)].isspace() and not p[-1:].isspace():
        i = max((k for k, ch in enumerate(p) if ch.isspace()), default=-1)
        if i > 0:
            p = p[:i]
    while True:
        at = None
        for _o, rx, close in _CUT_OPEN:
            opens = [m.start() for m in rx.finditer(p)]
            if opens and p.find(close, opens[-1]) < 0:
                at = opens[-1] if at is None else min(at, opens[-1])
        if at is None:
            break
        p = p[:at]
    return _CUT_TRAIL.sub("", p)


_CLAUSE_STOP = re.compile(r"[.!?][\u201d\"\u2019')\]]*$")


def join_clauses(parts, sep="; "):
    """BUILD IC-4 (D8): parts joined by sep — except after a part that ends a sentence (a terminal stop, maybe inside its closing quote
    or bracket, maybe before closing tags): there one space, and the next part begins with a capital. One terminal stop at every join:
    "nothing. Handoffs", not "nothing.; Handoffs". The JS engine's joinClauses is its twin."""
    out = ""
    for p in parts:
        p = str(p)
        if not out:
            out = p
        elif _CLAUSE_STOP.search(re.sub(r"(?:\s|<[^>]+>)+$", "", out)):
            out = out.rstrip() + " " + p[:1].upper() + p[1:]
        else:
            out += sep + p
    return out


def cut(text, n, what=None, unit="chars"):
    """P0.7 r2 (E2): one marked cut of a model's text. The text unchanged when its length ≤ n (unit "chars", or "bytes": UTF-8
    bytes); else cut at the last white space that leaves room for the marks (no white space: at that length), never inside an open
    '[' or '<' (it backs off to before it), trailing , ; : — - removed, then "…" (never "……"), then ")" for each "(" the cut left
    open — "(a, b, c…)". The result's length ≤ n always ("" when not even "…" fits); a byte cut never splits a character. With
    `what`, one line is printed: "    <what> shortened: <length> → <length> characters" (or bytes) — callers give `what` only where
    the cut runs once per run. Not text → ""."""
    if not isinstance(text, str):
        return ""
    unit = "bytes" if unit == "bytes" else "chars"
    size = _cut_len(text, unit)
    if size <= n:
        return text
    res, budget = "", n - _cut_len("…", unit)
    while budget >= 0:
        p = _cut_prefix(text, budget, unit)
        cand = p + "…" + ")" * max(0, p.count("(") - p.count(")"))
        over = _cut_len(cand, unit) - n
        if over <= 0:
            res = cand
            break
        budget -= over
    if what:
        say(f"    {what} shortened: {size:,} → {_cut_len(res, unit):,} {'bytes' if unit == 'bytes' else 'characters'}")
    return res


_SCHEMA_NOTES = ("title", "description", "examples", "default")
_SCHEMA_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SCHEMA_VALUES = ("enum", "const", "required")


def _schema_strip(node):
    """A schema object without its annotations; a key of a properties or $defs map is a NAME (EvidenceCard's FIELD `title`), kept."""
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in _SCHEMA_NOTES:
            continue
        if k in _SCHEMA_NAME_MAPS and isinstance(v, dict):
            out[k] = {name: _schema_strip(sub) for name, sub in v.items()}
        elif k in _SCHEMA_LISTS and isinstance(v, list):
            out[k] = [_schema_strip(x) for x in v]
        elif k in _SCHEMA_VALUES or not isinstance(v, dict):
            out[k] = v
        else:
            out[k] = _schema_strip(v)
    return out


def _schema_refs(node, acc):
    if isinstance(node, dict):
        r = node.get("$ref")
        if isinstance(r, str) and r.startswith("#/$defs/"):
            acc.add(r[len("#/$defs/"):])
        for v in node.values():
            _schema_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            _schema_refs(v, acc)
    return acc


def schema_block(schema, fields):
    """P0.7 r2 (E1): the JSON schema of exactly `fields` of `schema` (a dict as pydantic's model_json_schema() returns it), as compact
    JSON {"properties": {field: …} in the order given, "$defs": {…} every definition they reference, transitively, in sorted name
    order — no "$defs" key when none}, with the annotations title, description, examples and default removed from every schema object
    (never a key of a properties or $defs map). A field the schema lacks → ValueError naming it (the fields are a caller's constants)."""
    props, defs = (schema or {}).get("properties") or {}, (schema or {}).get("$defs") or {}
    missing = [f for f in fields if f not in props]
    if missing:
        raise ValueError("schema_block: the schema has no field " + ", ".join(repr(f) for f in missing))
    need, todo = set(), _schema_refs([props[f] for f in fields], set())
    while todo:
        name = todo.pop()
        if name not in need and name in defs:
            need.add(name)
            todo |= _schema_refs(defs[name], set()) - need
    doc = {"properties": {f: _schema_strip(props[f]) for f in fields}}
    if need:
        doc["$defs"] = {name: _schema_strip(defs[name]) for name in sorted(need)}
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def _sch_resolve(node, defs):
    seen = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and seen < 50:
        node, seen = defs.get(node["$ref"].split("/")[-1], {}), seen + 1
    return node if isinstance(node, dict) else {}


def _sch_branches(node, defs):
    node = _sch_resolve(node, defs)
    return [node] + [_sch_resolve(x, defs) for k in ("anyOf", "oneOf", "allOf") for x in node.get(k) or [] if isinstance(x, dict)]


def _sch_props(node, defs):
    out = {}
    for b in _sch_branches(node, defs):
        if isinstance(b.get("properties"), dict):
            out.update(b["properties"])
    return out


def _sch_items(node, defs):
    for b in _sch_branches(node, defs):
        if isinstance(b.get("items"), dict):
            return b["items"]
    return {}


def _poss(name):
    return name + ("'" if name.endswith("s") else "'s")


def _where(loc, schema, names):
    """A location in plain words: an item index → "item N" (1-based), a property the schema declares at that point → its plain name
    from `names`; any other key — one the model wrote, or one with no plain name — → "an entry", and nothing below it is named."""
    defs = (schema or {}).get("$defs") or {}
    node, pieces = schema or {}, []
    for el in loc or ():
        if isinstance(el, int) and not isinstance(el, bool):
            pieces.append(f"item {el + 1}")
            node = _sch_items(node, defs)
        elif isinstance(el, str) and el in _sch_props(node, defs) and isinstance(names.get(el), str) and names.get(el):
            pieces.append(names[el])
            node = _sch_props(node, defs)[el]
        else:
            pieces.append(None)
            break
    if not pieces:
        return ""

    def chain(ps):
        s = " ".join(_poss(p) for p in ps[:-1]) + (" " if len(ps) > 1 else "") + ps[-1]
        return s if ps[0].startswith("item ") else "the " + s
    if pieces[-1] is None:
        return "an entry" + (" in " + chain(pieces[:-1]) if len(pieces) > 1 else "")
    return chain(pieces)


def _num(v):
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(v)
    except Exception:  # noqa: BLE001
        return "?"


_KIND_WORDS = (("int", "a whole number"), ("float", "a number"), ("string", "text"), ("str", "text"), ("list", "a list"), ("tuple", "a list"),
               ("set", "a list"), ("dict", "an object"), ("model_attributes", "an object"), ("model", "an object"), ("dataclass", "an object"))


def _one_reason(e, schema, names):
    e = e if isinstance(e, dict) else {}
    w = _where(e.get("loc"), schema, names)
    s = w or "the update's version"
    t, ctx = str(e.get("type") or ""), e.get("ctx") if isinstance(e.get("ctx"), dict) else {}
    if t == "too_long":
        return f"{s} has {_num(ctx.get('actual_length'))} items; at most {_num(ctx.get('max_length'))} are allowed"
    if t == "too_short":
        return f"{s} has {_num(ctx.get('actual_length'))} items; at least {_num(ctx.get('min_length'))} are needed"
    if t == "missing":
        return f"{s} is missing"
    if t == "string_too_short":
        return f"{s} is shorter than {_num(ctx.get('min_length'))} characters"
    if t == "string_too_long":
        return f"{s} is longer than {_num(ctx.get('max_length'))} characters"
    if t == "greater_than_equal":
        return f"{s} is below {_num(ctx.get('ge'))}"
    if t == "less_than_equal":
        return f"{s} is above {_num(ctx.get('le'))}"
    if t == "value_error":
        return f"{s} is not valid"
    if t == "int_from_float":
        return f"{s} is not a whole number"
    m = re.fullmatch(r"(.+?)_(?:type|parsing)", t)
    if m:
        for pre, word in _KIND_WORDS:
            if m.group(1) == pre:
                return f"{s} is not {word}"
    return f"{s} does not fit the format"


def plain_reason(errors, schema, names):
    """P0.7 r2 (E3 a): validation errors — pydantic's errors(include_url=False, include_input=False) as plain dicts {type, loc, msg,
    ctx}, each `loc` relative to the part checked — in plain words, against that part's `schema` (its $defs included) and `names`
    (each declared property's plain name): too_long → "<where> has N items; at most M are allowed", too_short, missing,
    string_too_short / string_too_long, greater_than_equal / less_than_equal, int_from_float and the *_type / *_parsing kinds
    ("is not a whole number | a number | text | a list | an object"), value_error ("is not valid"), any other type "does not fit the
    format" (the type itself only in the caller's detail). <where> empty → "the update's version". Several errors → the first two
    joined, then "and N more". It never repeats a key or a value the model wrote: an unnamed key is "an entry". Never raises."""
    try:
        errs = [e for e in (errors or []) if isinstance(e, dict)]
        if not errs:
            return "the update's version does not fit the format"
        r = "; ".join(_one_reason(e, schema, names or {}) for e in errs[:2])
        return r + (f"; and {len(errs) - 2} more" if len(errs) > 2 else "")
    except Exception:  # noqa: BLE001
        return "the update's version does not fit the format"


def not_applied_chat_lines(entries, line_bytes=NOT_APPLIED_LINE_BYTES, total_bytes=NOT_APPLIED_TOTAL_BYTES):
    """P0.7 r2 (E3 c): a chat context's lines for the parts not applied — [(name, plain reason)] → "Not applied at the curveball
    (Block 1's version stands where it had one): <name> — <reason>", each on one line cut to `line_bytes`, together within
    `total_bytes` (each line's UTF-8 bytes + 1, changes_chat_block's count); past it one line "Not applied at the curveball: <n> more
    part(s) — each is listed on the page".
    P0.7 r2 Phase 5: the name is cut first to its share of the line — the line's bytes less the prefix, " — " and up to 100 bytes of the
    reason — so a line cut to its cap still names its part (a 160-character label of multi-byte letters with no space would otherwise
    be cut back to the prefix's last space, naming nothing)."""
    out, size = [], 0
    entries = list(entries or [])
    pre = len(NOT_APPLIED_CHAT.encode("utf-8")) + len(" — ".encode("utf-8"))
    for k, (name, why) in enumerate(entries):
        nm, wy = " ".join(str(name).split()), " ".join(str(why).split())
        nm = cut(nm, max(line_bytes - pre - min(len(wy.encode("utf-8")), 100), 1), unit="bytes")
        ln = cut(" ".join(f"{NOT_APPLIED_CHAT}{nm} — {wy}".split()), line_bytes, unit="bytes")
        b = len(ln.encode("utf-8")) + 1
        if size + b > total_bytes:
            out.append(f"Not applied at the curveball: {len(entries) - k} more part(s) — each is listed on the page")
            break
        out.append(ln)
        size += b
    return out


def _cut(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _fmt_pair(a, b, n):
    """Two values of a changed field for a prompt line. Long texts show the part that differs (their common start and end elided
    with "…", 30 characters of context kept), so a change at the end of a long note is visible; anything else as _fmt."""
    if not (isinstance(a, str) and isinstance(b, str) and (len(a) > n or len(b) > n)):
        return _fmt(a, n), _fmt(b, n)
    p, m = 0, min(len(a), len(b))
    while p < m and a[p] == b[p]:
        p += 1
    s = 0
    while s < m - p and a[-1 - s] == b[-1 - s]:
        s += 1
    lo = max(0, p - 30)

    def win(x):
        hi = min(len(x), len(x) - s + 30)
        seg = " ".join(x[lo:hi].split())
        if len(seg) > n:
            seg, hi = seg[:n - 1], len(x) + 1          # cut here: the trailing "…" below marks it
        return ("…" if lo > 0 else "") + seg + ("…" if hi < len(x) or hi == len(x) + 1 else "")
    return win(a), win(b)


CHANGES_SAME_IC4 = "Endpoints that did not change (their data is Block 1's): "     # BUILD IC-4 (D11, D16; open items 4 and 5): the switch
#     on — the unchanged endpoints' line says their data is Block 1's and no more, in the Oracle's change list and in the Muse's


def changes_prompt_block(changes, cap=CHANGE_CAP, value_chars=160, same_words=None):
    """D1/D3: the change list as an update prompt reads it — the announcement and its tests, then per changed endpoint the records
    added, removed and changed (at most `cap` of each, the rest counted: "N more changed records not listed"), the envelope and the
    integer-valued sums, then the unchanged and other endpoints. Every ref is quoted as the reply must cite it. BUILD IC-4 (D11):
    `same_words` — the unchanged endpoints' line's words, before their names (None: IC-3's, which ends "an aggregate among them did not
    update")."""
    if not isinstance(changes, dict):
        return "<changes>\n(no change list)\n</changes>"
    out = ["<changes>", "What changed at the curveball, computed by Python: the announcement, then every endpoint of this run compared "
           "record by record with Block 1's snapshot. Cite a change by its ref in quotes — \"announcement\", an endpoint's name or a "
           "record's ref — exactly as written here."]
    snap = changes.get("snapshot") or {}
    out.append(f"Block 1's snapshot: {snap.get('source') or 'none'} (Block 1 run {changes.get('block1_run_id') or 'unknown'}).")
    cv = changes.get("curveball") or {}
    if cv.get("text"):
        out += ["", f"Announcement (ref \"announcement\"; {cv.get('kind') or 'VERBATIM'}, from {cv.get('source') or 'the event'}):",
                str(cv["text"]).strip()]
        tests = [t for t in (cv.get("tests") or []) if isinstance(t, str) and t.strip()]
        if tests:
            out += ["", "What the announcement says it tests:"] + [f"- {t.strip()}" for t in tests]
    eps = changes.get("endpoints")
    if eps is None:
        out += ["", ("No Block 1 snapshot was found" if (snap.get("source") or "none") == "none" else f"There is no comparison ({snap.get('source')})")
                + ": the data could not be compared, so the announcement is the only change known. Treat no endpoint as unchanged."]
        out.append("</changes>")
        return "\n".join(out)
    changed = [(ep, e) for ep, e in eps.items() if e.get("status") == "changed"]
    out += ["", "Endpoints that changed:" if changed else "Endpoints that changed: none (the data is as in Block 1)."]
    for ep, e in changed:
        r0, r1 = e.get("records") or [0, 0]
        out.append(f"- \"{ep}\": {_counts(e)} (Block 1 {r0} → Block 2 {r1} records; matched by {e.get('match')})")
        if e.get("shape_changed"):
            out.append(f"  the payload's shape changed: {e['shape_changed'][0]} → {e['shape_changed'][1]}")
        for k, (a, b) in (e.get("envelope") or {}).items():
            out.append(f"  envelope {k}: {_fmt(a, value_chars)} → {_fmt(b, value_chars)}")
        if e.get("stamps"):
            out.append(f"  time stamps that changed (not a change of data): {', '.join(e['stamps'])}")
        for kind, verb in (("added", "added"), ("removed", "removed")):
            lst = e.get(kind) or []
            for r in lst[:cap]:
                out.append(f"  {verb} \"{r['ref']}\": {_cut(json.dumps(r.get('record'), ensure_ascii=False, default=str), value_chars * 2)}")
            if len(lst) > cap:
                out.append(f"  {len(lst) - cap} more {verb} records not listed")
        lst = e.get("changed") or []
        for r in lst[:cap]:
            parts = ["{} {} → {}".format(k, *_fmt_pair(a, b, value_chars)) for k, (a, b) in (r.get("fields") or {}).items()]
            out.append(f"  changed \"{r['ref']}\": " + "; ".join(parts))
        if len(lst) > cap:
            out.append(f"  {len(lst) - cap} more changed records not listed")
        ints = [(f, s) for f, s in (e.get("sums") or {}).items() if s.get("integer_valued")]
        if ints:
            out.append("  sums over the records, Block 1 → Block 2 (data for your reasoning, not page figures): "
                       + "; ".join(f"{f} {_fmt(s['old'])} → {_fmt(s['new'])} ({s['new_records']} records)" for f, s in ints))
    same = [ep for ep, e in eps.items() if e.get("status") == "unchanged" or str(e.get("status", "")).startswith("unchanged (only")]
    if same:
        out += ["", (same_words if same_words is not None else
                     "Endpoints that did not change (their data is Block 1's — an aggregate among them did not update): ") + ", ".join(same)]
    other = [(ep, e) for ep, e in eps.items() if e.get("status") in ("new endpoint", "not fetched this run", "unreadable")]
    for ep, e in other:
        out.append(f"- \"{ep}\": {e['status']}" + (f" — {e['reason']}" if e.get("reason") else ""))
    out.append("</changes>")
    return "\n".join(out)


ALERT_TEXT_FIELDS = ("headline", "title", "name", "summary", "detail", "description", "message", "text", "body")


def alert_changes(changes):
    """D5/D7: the alerts added and removed at the curveball — [(endpoint, "added"|"removed"|"changed", id, title)] from every alert-shaped
    endpoint of the change list (by shape, never by name). BUILD IC-4 (D3, F13): and each alert whose text changed ("changed", its title
    the fields whose words changed) — R8's fifth alert change, an alert whose summary was rewritten, was not counted."""
    out = []
    for ep, e in ((changes or {}).get("endpoints") or {}).items():
        if not e.get("alerts"):
            continue
        for kind in ("added", "removed"):
            for r in e.get(kind) or []:
                rec = r.get("record") if isinstance(r.get("record"), dict) else {}
                title = next((rec[k] for k in ("headline", "title", "name") if isinstance(rec.get(k), str) and rec[k].strip()), "")
                out.append((ep, kind, str(rec.get("id", r.get("ref"))), title))
        for r in e.get("changed") or []:
            tf = [k for k in (r.get("fields") or {}) if k in ALERT_TEXT_FIELDS]
            if tf:
                out.append((ep, "changed", str(r.get("ref")), f"its {' and '.join(tf)} changed"))
    return out


def endpoint_rows(changes):
    """D3/D7: the endpoints of a change list that changed, are new or are unreadable, in the form the Oracle's curveball.json records
    them — [{endpoint, status, added, removed, changed (counts), records [Block 1, Block 2], envelope {key: [old, new]}}]; [] without a
    snapshot. Never raises."""
    out = []
    for ep, e in (((changes or {}).get("endpoints") or {}).items()):
        if isinstance(e, dict) and e.get("status") in ("changed", "new endpoint", "unreadable"):
            out.append({"endpoint": ep, "status": e.get("status"), "added": len(e.get("added") or []), "removed": len(e.get("removed") or []),
                        "changed": len(e.get("changed") or []), "records": e.get("records"),
                        "envelope": {k: v for k, v in (e.get("envelope") or {}).items()}})
    return out


def endpoint_text(row):
    """A page's line for one endpoint row (endpoint_rows' form): '<endpoint>: 2 added, 1 changed record(s); status normal →
    emergency', or '<endpoint>: <status>'. Never a field sum — D4: a sum is data for the update call and the console, never a page
    figure. Plain text (the caller escapes it)."""
    import naipc_registry as _REG       # BUILD IC-8 (D12): the endpoint and an envelope field in a reader's words
    ep = _REG.endpoint_words(row.get("endpoint"))
    if row.get("status") != "changed":
        return f"{ep}: {row.get('status')}"
    parts = [f"{n} {w}" for n, w in ((row.get("added"), "added"), (row.get("removed"), "removed"), (row.get("changed"), "changed")) if n]
    env = "; ".join(f"{_REG.field_words(k)} {fmt_value(a)} → {fmt_value(b)}" for k, (a, b) in (row.get("envelope") or {}).items())
    return f"{ep}: " + (", ".join(parts) + " record(s)" if parts else "no record changes") + (f"; {env}" if env else "")


def alert_line(ep, kind, aid, title):
    """A page's line for one alert change (alert_changes' tuple): 'added: <title> (supply alerts)'. Plain text (the caller escapes it).
    BUILD IC-8 (D12): an added or removed alert by its title (its record's name), the endpoint in words. BUILD IC-8 (P4.4, p44n): a
    changed alert by its id — 'changed: <id> — its summary changed (system status)' — which each page's pass (REG.reader_page) puts in
    its record's name; its title slot holds what changed, never a name (before: the alert unnamed on three pages)."""
    import naipc_registry as _REG
    if kind == "changed":
        return f"changed: {aid} — " + (clean_text(title) if title else "its text changed") + f" ({_REG.endpoint_words(ep)})"
    return f"{kind}: " + (clean_text(title) if title else "an alert") + f" ({_REG.endpoint_words(ep)})"


NARRATIVE_TAGS = ("strong", "em", "b", "i", "br", "p", "ul", "ol", "li")
_TAG_LIKE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)(/?)>")


def escape_markup(value, keep_tags=True):
    """D11: new text from an update reply carries no markup of its own — no tag, script or handler of the reply reaches the page.
    With keep_tags (narrative text): the attribute-free tags the pages' narrative uses (<strong> <em> <b> <i> <br> <p> <ul> <ol> <li>)
    stay, every other tag — one with attributes (<span class="…">, <a href="…">) or another name (<h4>, <script>) — is dropped with
    its text kept (review F3: shown escaped, a tag read as visible text), and any other '<' or '>' is escaped. Without keep_tags (the
    diagram's code): every '<' and '>' escaped, nothing dropped. '&' and quotes are left as written (they start no markup in a text
    node; attributes escape them with | e). Lists and objects are walked; other values returned as they are. Never raises."""
    def esc(s):
        if not keep_tags:
            return s.replace("<", "&lt;").replace(">", "&gt;")
        out, i, opened = [], 0, []
        for m in _TAG_LIKE.finditer(s):
            out.append(s[i:m.start()].replace("<", "&lt;").replace(">", "&gt;"))
            i = m.end()
            name, attrs = m.group(2).lower(), (m.group(3) + m.group(4)).strip()
            if name not in NARRATIVE_TAGS or attrs.rstrip("/").strip():
                continue                    # a tag with attributes or another name: dropped, the text around it kept
            if name == "br":
                out.append("<br>")
            elif m.group(1):                # a closing tag stays only when it closes a tag kept here (none is left dangling)
                if name in opened:
                    while opened:
                        top = opened.pop()
                        out.append(f"</{top}>")
                        if top == name:
                            break
            elif not attrs.endswith("/"):
                opened.append(name)
                out.append(f"<{name}>")
        out.append(s[i:].replace("<", "&lt;").replace(">", "&gt;"))
        out += [f"</{t}>" for t in reversed(opened)]   # every tag kept is closed within its own text
        return "".join(out)

    def walk(v):
        if isinstance(v, str):
            return esc(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(value)


def stated_tokens(v, money=False):
    """Review F1: how a page's text states the number v — the tokens a stale-statement check looks for. An integer of at least 10 as
    '1,200' and '1200' (a money amount of at least 1,000 also as '$1.2K' / '$500K', the pages' short form); a rate between 0 and 1 as
    '20%'. Anything else (a smaller integer, text, a list): no token — too ambiguous to look for. Never raises."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return []
    if isinstance(v, float) and 0 < v < 1:
        return [f"{round(v * 100, 2):g}%"]
    if float(v).is_integer() and abs(v) >= 10:
        n = int(v)
        toks = [f"{n:,}"] + ([str(n)] if f"{n:,}" != str(n) else [])
        return toks + ([money_short(n)] if money and abs(n) >= 1000 else [])
    return []


def _token_re(tok):
    if tok.endswith("%"):
        return re.compile(r"(?<![\d.])" + re.escape(tok[:-1]) + r"\s?%")
    if tok.startswith("$"):
        return re.compile(r"(?<![\w$.])" + re.escape(tok) + r"(?!\w|\.\d)", re.I)
    return re.compile(r"(?<![\d,.])" + re.escape(tok) + r"(?!\d|[.,]\d|\s?[KkMmBb]\b)")


def _strings(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    return []


def stale_keys(texts, tokens):
    """Review F1: the keys of `texts` ({key: a string, or a list or object whose strings are read}) whose text states one of `tokens` as a
    whole number — not inside a longer number, a decimal or a $…K amount: '200' is found in '200 people' and '$200', not in '2 CFR
    200.414', '1,200' or '$200K'. In key order; never raises."""
    pats = [_token_re(t) for t in tokens or [] if isinstance(t, str) and t]
    if not pats:
        return []
    out = []
    for k, v in (texts or {}).items():
        try:
            s = " ".join(_strings(v))
            if any(p.search(s) for p in pats):
                out.append(k)
        except Exception:  # noqa: BLE001
            continue
    return out


_LIST_MARK = re.compile(r"^\s*(?:\(?\d{1,2}[.):]\)?|[-*•–—])\s*")


def _test_key(s):
    t = _LIST_MARK.sub("", clean_text(s).lower())
    return " ".join(re.findall(r"[a-z0-9]+", t))


def match_test_answers(tests, answers):
    """Review F5: each of the curveball's tests with the reply's answer to it, matched by the test's words with case, punctuation,
    spacing and a leading list number or bullet ignored; a reply that shortens or extends the words still matches when one starts with
    the other's first 40 characters (at least 15 compared). Each answer is used once. Returns ([(test, answer or "")], [the answers
    that name no test]). Never raises."""
    keys = [(j, _test_key(a.get("test")), a) for j, a in enumerate(answers or [])
            if isinstance(a, dict) and isinstance(a.get("answer"), str) and a["answer"].strip()]
    rows, used = [], set()
    for t in tests or []:
        tk, hit = _test_key(t), None
        for j, ak, a in keys:
            if j in used or not ak or not tk:
                continue
            n = min(40, len(tk), len(ak))
            if ak == tk or (n >= 15 and (ak.startswith(tk[:n]) or tk.startswith(ak[:n]))):
                hit = (j, a)
                break
        if hit:
            used.add(hit[0])
        rows.append((t, hit[1]["answer"] if hit else ""))
    return rows, [a for j, _k, a in keys if j not in used]


def first_sentence(text, limit=120):
    """D3, D7: an announcement's first sentence for a badge or a panel (at most `limit` characters). BUILD IC-8 (D1, one finder): the
    registry's split_sentences decides where a sentence ends (never after an abbreviation, an initial or a numbered item's mark); whole
    sentences are taken until at least 20 characters (a short first one, "Update.", takes the next, as the old rule's floor did); cut at a
    word with '…' when longer than `limit` (the organizers' words). Plain text (the caller escapes it)."""
    import naipc_registry as _REG    # the registry sits beside this module in every folder (tests/install_module.py)
    t = " ".join(str(text or "").split())
    s = ""
    for x in (x.strip() for x in _REG.split_sentences(t)):
        s = f"{s} {x}".strip() if x else s
        if len(s) >= 20:
            break
    s = s or t
    if len(s) > limit:
        s = s[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return s


def changes_chat_block(changes, extra=(), limit=CHAT_CHANGES_LIMIT):
    """D7: a chat context's CURVEBALL block — the announcement, the change summary, the alerts added and removed, then the caller's
    own lines (`extra`: closures, moved figures, test answers), cut to `limit` bytes with "(N more lines not listed)"."""
    if not isinstance(changes, dict):
        return ""
    lines = ["CURVEBALL — WHAT CHANGED (Block 1 → Block 2; the data changes are computed by Python from the API):"]
    cv = changes.get("curveball") or {}
    if cv.get("text"):
        lines.append(f"Announcement: {clean_text(cv['text'])}")
    for t in cv.get("tests") or []:
        if isinstance(t, str) and t.strip():
            lines.append(f"Test stated by the announcement: {clean_text(t)}")
    lines += [f"Data: {ln.strip()}" for ln in (changes.get("summary") or [])]
    for ep, kind, aid, title in alert_changes(changes):
        lines.append(f"Alert {kind} ({ep}): {aid}" + (f" — {clean_text(title)}" if title else ""))
    lines += [str(x) for x in extra if str(x).strip()]
    out, size = [], 0
    for i, ln in enumerate(lines):
        b = len(ln.encode("utf-8")) + 1
        if size + b > limit - 40:
            out.append(f"({len(lines) - i} more lines not listed)")
            break
        out.append(ln)
        size += b
    return "\n".join(out)


def archive_contexts(ctx_dir, keep=None, stamp=None):
    """D9 — before a context file is written: move every other *.json in <output>/chat-context/ to <output's parent>/
    chat-context-archive/<UTC stamp>/ (outside output\\, so never uploaded; nothing is deleted). `keep` is the name about to be
    written. Returns (moved, destination or None, problem or None); never raises."""
    d, moved, dest = Path(ctx_dir), 0, None
    try:
        files = sorted(p for p in d.glob("*.json") if p.is_file() and p.name != keep) if d.is_dir() else []
        if not files:
            return 0, None, None
        root = d.parent.parent / CONTEXT_ARCHIVE
        st = stamp or _utc_stamp()
        dest, n = root / st, 1
        while dest.exists():
            dest, n = root / f"{st}-{n}", n + 1
        dest.mkdir(parents=True)
        for p in files:
            os.replace(p, dest / p.name)
            moved += 1
        return moved, dest, None
    except Exception as e:  # noqa: BLE001 — a failed move is printed by the caller, never raised
        return moved, dest, f"{type(e).__name__}: {e}"


CHAT_CONTEXT_MAX = 96 * 1024   # the chat Worker's CONTEXT_BYTES (worker/chat-proxy.mjs:15): a larger context is refused (context_too_large)


def context_size_note(doc) -> str:
    """§9 (P0.3 RECORD 5) — a chat context over the chat Worker's limit is still written (the page and its static answers stay whole);
    this says so on the console, "" when it fits. One rule for the three tracks' write_chat_context."""
    n = len(doc or b"")
    if n <= CHAT_CONTEXT_MAX:
        return ""
    return (f"⚠ chat context: {n:,} bytes, over the chat Worker's 96 KiB ({CHAT_CONTEXT_MAX:,} bytes) — the live chat will refuse it and "
            "the page's chat shows its static answers")


def save_partial(cp_dir, label, attempt, text):
    """D10 — a failed call's text: <checkpoints>/partial_<label>_<run id>_<attempt>.txt (UTF-8; BUILD IC-6 D11 a: named by the run —
    NAIPC_RUN_ID, else the process's own stamp — not by the time of the save, so the file says which run's call it was). Returns (path,
    None), (None, None) for an empty text, or (None, reason); never raises."""
    if not isinstance(text, str) or not text:
        return None, None
    try:
        d = Path(cp_dir)
        d.mkdir(parents=True, exist_ok=True)
        base = f"partial_{re.sub(r'[^A-Za-z0-9_+-]+', '-', str(label))}_{re.sub(r'[^A-Za-z0-9_-]+', '-', str(run_id()))}_{attempt}"
        p, n = d / f"{base}.txt", 1
        while p.exists():
            p, n = d / f"{base}-{n}.txt", n + 1
        p.write_bytes(text.encode("utf-8", errors="replace"))
        return p, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


# ═══ BUILD IC-3 (D4): replies decoded strictly ═══
class NotJSON(ValueError):
    """A reply that is not JSON as the standard defines it (NaN, Infinity), or not JSON at all."""


def strict_loads(text, where="reply"):
    """BUILD IC-3 (D4): the one decoder for every reply of a model — (object, findings). As json.loads, with two differences: NaN, Infinity
    and -Infinity are refused as not JSON (NotJSON); a key given twice in one object is a finding {rule: "key_twice", where, key, path}
    — the last value still stands, as json.loads keeps it, and the caller records the finding where the page's record says so."""
    findings, path = [], []

    def hook(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                findings.append({"rule": "key_twice", "where": where, "key": k,
                                 "why": f"the key {k!r} is given twice in one object of the {where} reply — the last kept"})
            seen[k] = v
        return seen

    def no_const(c):
        raise NotJSON(f"{c} is not JSON (the {where} reply)")
    try:
        obj = json.loads(text, object_pairs_hook=hook, parse_constant=no_const)
    except NotJSON:
        raise
    return obj, findings


def record_decode_findings(cp_dir, findings, run_id_=None):
    """BUILD IC-3 (D4): append each decoding finding, with the run's id, to <checkpoints>/decode_findings.jsonl (UTF-8); printed; never
    raises. The page's record reads this run's lines back."""
    if not findings:
        return
    try:
        d = Path(cp_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "decode_findings.jsonl", "a", encoding="utf-8") as f:
            for x in findings:
                f.write(json.dumps(dict(x, run_id=run_id_ or run_id()), ensure_ascii=False) + "\n")
                say(f"    ⚠ decoding: {x.get('why')}")
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ decoding findings not kept — {type(e).__name__}")


def decode_findings(cp_dir, run_id_=None):
    """This run's decoding findings, from <checkpoints>/decode_findings.jsonl ([] when none); never raises."""
    rid = run_id_ or run_id()
    out = []
    try:
        for line in (Path(cp_dir) / "decode_findings.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if isinstance(x, dict) and x.get("run_id") == rid:
                out.append(x)
    except (OSError, ValueError):
        pass
    return out


# ═══ BUILD IC-3 (D5): the call is observed, and ends ═══
class EndTimeReached(RuntimeError):
    """A model call passed its end time (the stage's budget less what has run): ended with its partial text kept; never retried."""


CALL_LOG = "call_log.jsonl"


# ═══ BUILD IC-6 (D1, I1): one profile per call — its limit, effort, stream mode, form and planning amount ═══
# Written by tools/rederive_limits_ic6.py --literal from evidence/call_samples_ic6.json (every live call-log line on record, each with its
# log file's hash); nothing is read from disk at run time; tests/t_capacity_ic6.py derives it again from the samples and fails when it is
# stale. Fixed (Q-LIMITS): track, label (the call log's), stream, form ("object" one JSON object, "lines" JSON Lines, "schema" a structured
# output, "text"), max_tokens (128,000 on every long streamed call; 4,000 on the Architect's explanation and the three repairs), effort,
# stage. Measured: planning_tokens — ceil(1.5 × the largest output on record, a cut one counted as a floor); floor_rate — the slowest
# finished line of at least 30 s, floor_sample its tokens and seconds; planning_s — the larger of planning tokens ÷ floor rate and 1.5 ×
# the longest finished line (longest_s); first_line_s — its longest t_first_line, else its longest first text + 30 s; largest_input;
# unmeasured — no live line: its limit is its plan, its seconds come from its harness's stated rate. all_or_nothing: every form but
# lines. basis: the runs, the samples, the evidence file's hash. Every call site reads its profile (tests/t_profiles_ic6.py's lint), and
# the start rule its plan, never max_tokens (D2). BLOCK_RESERVES_S: D3's reserves of the later tracks, from the same records;
# BLOCK_TAIL_S: each calling track's own time outside its calls (×1.5, to the 10 s above), which its calls end before the reserves.
CALL_PROFILES_EVIDENCE = {'file': 'evidence/call_samples_ic7.json', 'sha256_16': '6a948ca40dd35547', 'lines': 24, 'logs': 17, 'derived_by': 'tools/rederive_limits_ic7.py --literal'}
CALL_PROFILES = {
    'oracle/master': {
        'track': 'oracle', 'label': 'master', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'high',
        'stage': 'master analysis', 'planning_tokens': 57143, 'floor_rate': 110.63, 'floor_sample': [30832, 278.703],
        'planning_s': 516.5, 'longest_s': 338.937, 'first_line_s': 153.2, 'largest_input': 43481, 'unmeasured': False,
        'basis': {'runs': ['20261112T041820Z', '20261114T045417Z', '20261116T235259Z'], 'completed': [33791, 38095, 30832], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'oracle/master'},
    'oracle/deliverables': {
        'track': 'oracle', 'label': 'deliverables', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'medium',
        'stage': "the Oracle's end", 'planning_tokens': 13881, 'floor_rate': 113.82, 'floor_sample': [9164, 80.516],
        'planning_s': 122.0, 'longest_s': 80.516, 'first_line_s': 38.0, 'largest_input': 11771, 'unmeasured': False,
        'basis': {'runs': ['20261112T041820Z', '20261114T045417Z', '20261116T235259Z'], 'completed': [9164, 9254, 8492], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'oracle/deliverables'},
    'oracle/update': {
        'track': 'oracle', 'label': 'update', 'stream': True, 'form': 'lines', 'max_tokens': 128000, 'effort': 'high',
        'stage': 'curveball update', 'planning_tokens': 79199, 'floor_rate': 127.67, 'floor_sample': [52799, 413.563],
        'planning_s': 620.3, 'longest_s': 413.563, 'first_line_s': 244.8, 'largest_input': 74203, 'unmeasured': False,
        'basis': {'runs': ['20261116T015538Z', '20261116T231250Z'], 'completed': [43901, 52799], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': False, 'key': 'oracle/update'},
    'oracle/update_whole': {
        'track': 'oracle', 'label': 'update', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'high',
        'stage': 'curveball update', 'planning_tokens': 69576, 'floor_rate': 128.56, 'floor_sample': [46384, 360.796],
        'planning_s': 541.2, 'longest_s': 360.796, 'first_line_s': 252.0, 'largest_input': 59518, 'unmeasured': False,
        'basis': {'runs': ['20261112T045318Z'], 'completed': [46384], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'oracle/update_whole'},
    'oracle/repair': {
        'track': 'oracle', 'label': 'repair', 'stream': False, 'form': 'schema', 'max_tokens': 4000, 'effort': 'low',
        'stage': "the Oracle's end", 'planning_tokens': 4000, 'floor_rate': None, 'floor_sample': None, 'planning_s': None,
        'longest_s': None, 'first_line_s': None, 'largest_input': None, 'unmeasured': True,
        'basis': {'runs': [], 'completed': [], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'}, 'all_or_nothing': True,
        'key': 'oracle/repair'},
    'oracle/diagram_repair': {
        'track': 'oracle', 'label': 'call', 'stream': False, 'form': 'text', 'max_tokens': 4000, 'effort': 'low',
        'stage': "the Oracle's end", 'planning_tokens': 4000, 'floor_rate': None, 'floor_sample': None, 'planning_s': None,
        'longest_s': None, 'first_line_s': None, 'largest_input': None, 'unmeasured': True,
        'basis': {'runs': [], 'completed': [], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'}, 'all_or_nothing': True,
        'key': 'oracle/diagram_repair'},
    'muse/call1': {
        'track': 'muse', 'label': 'call1', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'medium',
        'stage': "the Muse's stage", 'planning_tokens': 15366, 'floor_rate': 105.33, 'floor_sample': [6450, 61.235],
        'planning_s': 145.9, 'longest_s': 92.36, 'first_line_s': 82.0, 'largest_input': 19583, 'unmeasured': False,
        'basis': {'runs': ['20261112T041820Z', '20261114T045417Z', '20261116T235259Z'], 'completed': [6450, 9227, 10244, 9055], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'muse/call1'},
    'muse/call2': {
        'track': 'muse', 'label': 'call2', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'medium',
        'stage': "the Muse's stage", 'planning_tokens': 7481, 'floor_rate': 102.03, 'floor_sample': [3799, 37.234], 'planning_s': 73.3,
        'longest_s': 45.672, 'first_line_s': 43.9, 'largest_input': 14730, 'unmeasured': False,
        'basis': {'runs': ['20261112T041820Z', '20261114T045417Z'], 'completed': [3799, 4987], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'muse/call2'},
    'muse/update': {
        'track': 'muse', 'label': 'update', 'stream': True, 'form': 'object', 'max_tokens': 128000, 'effort': 'medium',
        'stage': "the Muse's stage", 'planning_tokens': 30180, 'floor_rate': 124.77, 'floor_sample': [13097, 104.968],
        'planning_s': 241.9, 'longest_s': 158.157, 'first_line_s': 84.2, 'largest_input': 38336, 'unmeasured': False,
        'basis': {'runs': ['20261112T045318Z', '20261116T015538Z', '20261116T231250Z'], 'completed': [13097, 20120], 'censored': [16000], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'muse/update'},
    'muse/repair': {
        'track': 'muse', 'label': 'repair', 'stream': True, 'form': 'schema', 'max_tokens': 4000, 'effort': 'low',
        'stage': "the Muse's stage", 'planning_tokens': 4000, 'floor_rate': None, 'floor_sample': None, 'planning_s': None,
        'longest_s': None, 'first_line_s': None, 'largest_input': None, 'unmeasured': True,
        'basis': {'runs': [], 'completed': [], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'}, 'all_or_nothing': True,
        'key': 'muse/repair'},
    'architect/rationale': {
        'track': 'architect', 'label': 'rationale', 'stream': True, 'form': 'schema', 'max_tokens': 4000, 'effort': 'low',
        'stage': "the rationale's deadline", 'planning_tokens': 1916, 'floor_rate': None, 'floor_sample': None, 'planning_s': 17.2,
        'longest_s': 11.5, 'first_line_s': 32.3, 'largest_input': 3523, 'unmeasured': False,
        'basis': {'runs': ['20261112T041820Z', '20261112T045318Z', '20261114T045417Z', '20261116T015538Z', '20261116T231250Z', '20261116T235259Z'], 'completed': [1045, 806, 1086, 1277, 1254, 1123], 'censored': [], 'evidence_sha256': '6a948ca40dd35547'},
        'all_or_nothing': True, 'key': 'architect/rationale'},
}
BLOCK_RESERVES_S = {1: {'architect': 30, 'muse': 230, 'landing': 10}, 2: {'architect': 30, 'muse': 260, 'landing': 10}}
BLOCK_TAIL_S = {1: {'oracle': 40, 'architect': 10, 'muse': 10}, 2: {'oracle': 50, 'architect': 20, 'muse': 10}}


def profile(key):
    """BUILD IC-6 (D1, I1): a call's profile — a copy of CALL_PROFILES[key] (a KeyError for a key with none: every call has one)."""
    return copy.deepcopy(CALL_PROFILES[key])


def _prompt_chars(params):
    """BUILD IC-6 (D1): the request's text length — its system prompt's and every message's text, in characters (a count, never the text)."""
    def n(c):
        if isinstance(c, str):
            return len(c)
        if isinstance(c, list):
            return sum(len((b.get("text") if isinstance(b, dict) else getattr(b, "text", None)) or "") for b in c)
        return 0
    return n(params.get("system")) + sum(n(m.get("content")) for m in params.get("messages") or [] if isinstance(m, dict))


# ═══ BUILD IC-6 (D3, I3): one clock for the block ═══
TRACK_ORDER = ("oracle", "architect", "muse", "landing")


def _wall():
    """The wall clock in epoch seconds — pipeline.py exports the block's end in it (the harnesses are separate processes); patched in tests."""
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def block_number():
    """NAIPC_BLOCK as pipeline.py exported it: 1 or 2, else None."""
    v = os.environ.get("NAIPC_BLOCK", "").strip()
    return int(v) if v in ("1", "2") else None


def _block_end_epoch():
    raw = os.environ.get("NAIPC_BLOCK_END", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _later(track, b):
    """{track: reserve s} of the tracks after `track` in block b (with no block named: the larger of each track's two reserves)."""
    later = TRACK_ORDER[TRACK_ORDER.index(track) + 1:]
    if b in BLOCK_RESERVES_S:
        return {t: BLOCK_RESERVES_S[b][t] for t in later}
    return {t: max(BLOCK_RESERVES_S[x][t] for x in BLOCK_RESERVES_S) for t in later}


def _tail(track, b):
    if b in BLOCK_TAIL_S:
        return BLOCK_TAIL_S[b].get(track, 0)
    return max(BLOCK_TAIL_S[x].get(track, 0) for x in BLOCK_TAIL_S)


def block_end_for(track, clock):
    """BUILD IC-6 (D3, I3): the time on `clock` (the harness's own monotonic clock) by which this track's calls must end — the block's end
    (NAIPC_BLOCK_END, epoch seconds: pipeline.py's start + --block-seconds, by default 3,600 s for Block 1 and 900 s for Block 2) less the
    reserves of the tracks after it (BLOCK_RESERVES_S) and this track's own time outside its calls (BLOCK_TAIL_S: its work after its last
    call must not run into a later track's reserve either). None when NAIPC_BLOCK_END is unset or not a number — a harness run by hand
    keeps its stage ends."""
    end = _block_end_epoch()
    if end is None:
        return None
    b = block_number()
    return clock() + (end - _wall()) - sum(_later(track, b).values()) - _tail(track, b)


# ═══ BUILD IC-6 (D7, I6): what each track holds — its status file, the hub's line, the pipeline's check ═══
TRACK_STATUS = "track_status.json"
TRACK_NAMES = {"oracle": "Business case", "architect": "Navigator", "muse": "Campaign", "landing": "Hub"}
UPDATE_STATES = ("updated", "partly updated", "not updated", "declined", "not run")
PART_STATUSES = ("revised at the curveball", "reviewed, unchanged", "from Block 1, not reviewed", "from Block 1 — its edit was not applied",
                 "removed at the curveball")        # and D12's "stale: names <x>, revised at the curveball"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_track_status(cp_dir, track, state, update=None, page_run=None, why=None, block=None, error_class=None, content=None):
    """BUILD IC-6 (D7, I6): <checkpoints>/track_status.json — this run's id, its block, the track, its state ("started" first, "built"
    after its page is written, "failed" by its top-level handler: a killed harness leaves "started"), when, the run id its page prints,
    and in Block 2 its update {"state" (UPDATE_STATES), "summary", "counts", "parts": [{"part", "status" (PART_STATUSES), "why"}],
    "why"}. BUILD IC-8 (D3): `content` — the track's dispositions counted (REG.Dispositions.content: counts only), which the run's
    content line sums (content_totals). Written whole (a temporary file, then os.replace). Returns the document, or None (said) when it
    was not written; never raises."""
    try:
        doc = {"schema": "naipc.track_status.v1", "run_id": run_id(), "block": block or block_number(), "track": track, "state": state,
               "at": _now_iso(), "page_run": page_run}
        if why:
            doc["why"] = why
        if error_class:                 # BUILD IC-7 (D7): the error's class, for the record and the console — never printed on a page
            doc["error_class"] = error_class
        doc["build"] = BUILD_ID         # BUILD IC-7 (D12): which build wrote it
        inv_ = invocation(cp_dir, track) if state == "started" else invocation()   # BUILD IC-8 (D8): this invocation's id and number (a
        #                                  harness run alone: allocated at its start; pipeline.py's own "not run" status names none)
        doc["invocation_id"], doc["invocation_number"], doc["schema_version"] = inv_.get("id"), inv_.get("number"), RECORD_SCHEMA
        if os.environ.get("NAIPC_RERUN_AFTER_END", "").strip() == "1":      # BUILD IC-7 (D10): pipeline.py --resume … --block-seconds
            doc["rerun"] = "after the block's end"
        if update is not None:
            doc["update"] = update
        if content is not None:         # BUILD IC-8 (D3)
            doc["content"] = content
        d = Path(cp_dir)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (TRACK_STATUS + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, d / TRACK_STATUS)
        return doc
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ track status not written — {type(e).__name__}")
        return None


def read_track_status(cp_dir, rid=None):
    """(the document, None) when <cp_dir>/track_status.json is this run's (`rid`, else NAIPC_RUN_ID; with neither, any run's), else
    (None, the reason in plain words) — missing, unreadable, not an object, another run's: never taken as success."""
    rid = rid or os.environ.get("NAIPC_RUN_ID", "").strip() or None
    p = Path(cp_dir) / TRACK_STATUS
    if not p.is_file():
        return None, "no status file of this run"
    try:
        doc = json.loads(p.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as e:
        return None, f"its status file is unreadable ({type(e).__name__})"
    if not isinstance(doc, dict) or doc.get("state") not in ("started", "built", "failed"):
        return None, "its status file is not a status"
    if rid and doc.get("run_id") != rid:
        return None, f"its status file is from run {doc.get('run_id') or '(none)'}, not this run"
    return doc, None


def track_failed(cp_dir, track, exc):
    """BUILD IC-6 (D7): the top-level handler's write, best effort — "failed", unless the harness exited cleanly (exit 0) or its status
    is not this run's "started" (its page already built, or a failure already written). BUILD IC-7 (D7, W8): `why` in plain words
    (plain_why: one table, FAIL_WORDS), the error's class in `error_class` (the record and the console only, never a page), and this
    run's not-built page in place of the track's output (not_built_page: every file there that is not this run's built page moved to
    output_prev/<its run>/). Never raises."""
    try:
        code = getattr(exc, "code", None)
        if isinstance(exc, SystemExit) and code in (0, None):
            return
        doc, _w = read_track_status(cp_dir, run_id())
        if doc is not None and doc.get("state") != "started":
            return
        cls = type(exc).__name__ + (f" (exit {code})" if isinstance(exc, SystemExit) else "")
        write_track_status(cp_dir, track, "failed", why=plain_why(exc), block=(doc or {}).get("block"), error_class=cls)   # BUILD IC-7 (D7)
        if track != "landing":          # the hub's not-built page lists every track's line: pipeline.py writes it (it knows the folders)
            not_built_page(Path(cp_dir).parent / "output", track, run_id(), why=plain_why(exc))
    except Exception:  # noqa: BLE001
        pass


def page_run_of(html_path):
    """The run id a page prints (its <meta name="naipc-run" content="…">), or None."""
    try:
        t = Path(html_path).read_text(encoding="utf-8", errors="replace")[:30000]
    except OSError:
        return None
    m = re.search(r'<meta\s+name="naipc-run"\s+content="([^"]*)"', t)
    return m.group(1) if m else None


UPDATE_NOTE_KINDS = ("impact",)          # BUILD IC-8 (D13): an update's add line that is a note on what changed (both tracks' kind)
UPDATE_DERIVED_KINDS = ("derived",)       # one that is a new derived figure (the Oracle's)
UPDATE_CRISIS_KINDS = ("crisis_post",)    # one that is a crisis post (the Muse's)


def _n(k, one, many=None):
    return f"{k} {one if k == 1 else (many or one + 's')}"


def update_status(edits, units=None, adds=None, notes=0, derived=0, crisis=None):
    """BUILD IC-8 (D13): the one status of an update by keyed edits — {"line", "counts", "problems"}. The line: "<n> Block 1 items
    reviewed: <k> kept, <u> updated[, <p> updated in part][, <r> removed][, <x> not applied — Block 1's stands]. <a> new items added[,
    including <c> crisis posts]." and, where the update added notes or derived figures, "<m> notes on what changed[; <d> new derived
    figures]." — n the review set; an item the edits replaced is updated, updated in part (its units — `units`, the dispositions'
    records — hold Block 1's text and the update's), or not applied (every unit Block 1's); x counts the items not applied and not
    reviewed (Block 1's stands for both); a the items added, c the crisis posts among them (`crisis`, else the reply's add lines of kind
    crisis_post), m the notes and d the derived figures the update kept (`notes`, `derived`). Asserted before the line is made: k + u + p +
    r + x = n, c ≤ a, a + m + d = the reply's add lines (`adds`: their kinds, reply_add_kinds; None: not asserted, said so). A failed
    assert: live, the problem is named and the line prints the counts as they are (accept_ic8's row D2 fails); under NAIPC_STRICT=1 it
    raises."""
    e = edits if isinstance(edits, dict) else {}
    st = e.get("status") if isinstance(e.get("status"), dict) else {}
    cnt = e.get("counts") if isinstance(e.get("counts"), dict) else {}
    by_item = {}
    for u_ in units or []:
        if isinstance(u_, dict) and u_.get("item") is not None:
            by_item.setdefault(str(u_["item"]), set()).add(str(u_.get("source") or ""))
    k = u = p = r = x = a = 0
    if st:
        for item, s in st.items():
            if s == "kept":
                k += 1
            elif s == "replaced":
                srcs = by_item.get(str(item)) or set()
                if srcs and "candidate" not in srcs:
                    x += 1
                elif "candidate" in srcs and srcs & {"base", "default"}:
                    p += 1
                else:
                    u += 1
            elif s == "removed":
                r += 1
            elif s == "added":
                a += 1
            else:                       # not applied, not reviewed: Block 1's stands
                x += 1
    else:                               # a record with counts alone (IC-7's files): no item statuses, no units
        k, u, r = (int(cnt.get(w) or 0) for w in ("kept", "replaced", "removed"))
        x, a = int(cnt.get("not applied") or 0) + int(cnt.get("not reviewed") or 0), int(cnt.get("added") or 0)
    n = e.get("review_set") if isinstance(e.get("review_set"), int) and not isinstance(e.get("review_set"), bool) else k + u + p + r + x
    m, d = int(notes or 0), int(derived or 0)
    c = int(crisis) if isinstance(crisis, int) and not isinstance(crisis, bool) else (
        sum(int(adds.get(kk) or 0) for kk in UPDATE_CRISIS_KINDS) if adds is not None else 0)
    probs = []
    if k + u + p + r + x != n:
        probs.append(f"kept {k} + updated {u} + updated in part {p} + removed {r} + not applied {x} = {k + u + p + r + x}, not the {n} reviewed")
    if c > a:
        probs.append(f"{c} crisis posts but {a} new items added")
    if adds is not None and a + m + d != sum(int(v or 0) for v in adds.values()):
        probs.append(f"added {a} + notes {m} + derived figures {d} = {a + m + d}, not the reply's {sum(int(v or 0) for v in adds.values())} add lines")
    if probs and os.environ.get("NAIPC_STRICT", "").strip() == "1":
        raise ValueError("update_status: " + "; ".join(probs))
    line = (f"{n} Block 1 item{'' if n == 1 else 's'} reviewed: {k} kept, {u} updated" + (f", {p} updated in part" if p else "")
            + (f", {r} removed" if r else "") + (f", {x} not applied — Block 1's stands" if x else "") + ". "
            + f"{_n(a, 'new item')} added" + (f", including {_n(c, 'crisis post')}" if c else "") + ".")
    tail = [w for w in ((_n(m, "note") + " on what changed") if m else "", (_n(d, "new derived figure")) if d else "") if w]
    if tail:
        line += " " + "; ".join(tail) + "."
    return {"line": line, "counts": {"n": n, "kept": k, "updated": u, "updated in part": p, "removed": r, "not applied": x, "added": a,
                                     "crisis": c, "notes": m, "derived": d, "add lines": (sum(int(v or 0) for v in adds.values())
                                                                                          if adds is not None else None)},
            "problems": probs + ([] if adds is not None else ["the reply's add lines were not read: a + m + d not asserted"])}


def reply_add_kinds(path):
    """BUILD IC-8 (D13): the kinds of a keyed reply's add lines ({"op": "add", "kind": …}, one JSON object a line) — {kind: n}; None
    when the file cannot be read. A line that does not parse is not counted (the edits record names it)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    out = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("op") == "add":
            kk = str(o.get("kind") or "")
            out[kk] = out.get(kk, 0) + 1
    return out


def disposition_units(path, scope="active invocation"):
    """BUILD IC-8 (D13): a dispositions file's unit records of one invocation (read_records; the file's last block when its lines name
    none): [{unit, item, disposition, source, …}]; [] when it cannot be read."""
    try:
        r_ = read_records(path, scope, strict=False)
        rows = r_.lines if r_.invocation else r_.run_lines
    except Exception:  # noqa: BLE001
        return []
    return [x for x in rows if isinstance(x, dict) and x.get("unit")]


def track_line(track, doc, why=None):
    """BUILD IC-6 (D7, I6): one track's line, from its status file — "<name> — <what it holds>"; no file, another run's or an unreadable
    one: "<name> — status unavailable: <why>", never success."""
    name = TRACK_NAMES.get(track, track)
    noun = name.lower()
    if doc is None:
        return f"{name} — status unavailable: {why or 'no status file of this run'}"
    tail = " (from the files this run wrote before IC-6: no status file)" if doc.get("legacy") else ""
    st = doc.get("state")
    if doc.get("rerun"):                # BUILD IC-7 (D10): a re-run given a new end after the block's own
        tail += f" — re-run {doc['rerun']}"
    if st == "started":
        return f"{name} — not built this run: its harness started and did not finish" + tail
    if st == "failed":
        return f"{name} — failed this run" + (f" ({doc['why']})" if doc.get("why") else "") + tail
    u = doc.get("update") if isinstance(doc.get("update"), dict) else None
    if not u:
        return f"{name} — built" + tail
    us, w, summ = u.get("state"), u.get("why"), u.get("summary")
    if us == "updated":
        return f"{name} — updated at the curveball" + (f": {summ}" if summ else "") + tail
    if us == "partly updated":
        return f"{name} — partly updated at the curveball" + (f": {summ}" if summ else "") + (f" ({w})" if w else "") + tail
    if us == "declined":
        return f"{name} — not updated: the model declined the update" + (f" ({w})" if w else "") + f"; this is Block 1's {noun}" + tail
    if us == "not run":
        return f"{name} — not updated: the update did not run" + (f" ({w})" if w else "") + f"; this is Block 1's {noun}" + tail
    return f"{name} — not updated: this is Block 1's {noun}" + (f" ({w})" if w else "") + tail


def legacy_track_doc(track, cp_dir, rid):
    """BUILD IC-6 (D7): a status built from the files a run written before IC-6 left — the Oracle's curveball.json ("update"), the
    Muse's muse_summary.json ("update"), the Architect's architect_summary.json — when they are this run's; else None."""
    try:
        d = Path(cp_dir)
        f = {"oracle": CURVEBALL_FILE, "muse": "muse_summary.json", "architect": "architect_summary.json"}.get(track)
        if not f or not (d / f).is_file():
            return None
        raw = json.loads((d / f).read_bytes().decode("utf-8-sig"))
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and "run_id" not in raw else raw
        if not isinstance(data, dict) or (rid and data.get("run_id") != rid):
            return None
        doc = {"run_id": data.get("run_id"), "track": track, "state": "built", "legacy": True}
        st = str(data.get("update") or "")
        if track in ("oracle", "muse") and st:
            if st in ("applied", "full") or st.startswith("no base"):
                e = data.get("edits") if isinstance(data.get("edits"), dict) else None
                doc["update"] = {"state": "updated", "summary": update_status(e)["line"] if e else None}      # BUILD IC-8 (D13)
            elif st.startswith("failed: declined"):
                doc["update"] = {"state": "declined", "why": st[len("failed: "):]}
            else:
                doc["update"] = {"state": "not updated", "why": "the update did not complete" + (f": {st[len('failed: '):]}"
                                                                                                if st.startswith("failed: ") else "")}
        return doc
    except Exception:  # noqa: BLE001
        return None


def track_lines(dirs, rid=None):
    """Each track's line for this run, from its status file (else a pre-IC-6 run's own files, said so; else status unavailable)."""
    out = []
    for t in ("oracle", "architect", "muse"):
        cp = dirs.get(t)
        if cp is None:
            out.append(track_line(t, None, "its checkpoints were not given"))
            continue
        doc, why = read_track_status(cp, rid)
        if doc is None and why == "no status file of this run":
            doc = legacy_track_doc(t, cp, rid or os.environ.get("NAIPC_RUN_ID", "").strip() or None)
        out.append(track_line(t, doc, why))
    return out


def content_totals(dirs, rid=None):
    """BUILD IC-8 (D3): the run's content totals with their denominators. BUILD IC-8 (P4.3a; F24, RS-2): every total names its scope —
    first the published pages', each track's "content" from its current status (the selected invocation: its dispositions summed, counts
    only): "Content — published, each track's current invocation of run <rid>: O omitted, K kept from Block 1, D defaults, T trimmed;
    units N (Oracle a, Architect b, Muse c of invocation 2, Hub d of invocation 2)" (a track's invocation named past its first; before
    P4.3a the line said "run" and held these sums); a track whose status holds none is named on a second line (its units are not in N).
    Then, when a track recorded more than one invocation in the run, every invocation's records summed from the dispositions files
    (read_records, scope "run"): "Content — all invocations of run <rid>: …; units M (…, Muse c' in 2 invocations, …)". → lines.
    BUILD IC-8 (d16d; RECORD 2): the tracks `dirs` names, in the pipeline's order — the hub ("landing") records its units too."""
    names = {"oracle": "Oracle", "architect": "Architect", "muse": "Muse", "landing": "Hub"}
    rid_ = rid or os.environ.get("NAIPC_RUN_ID", "").strip() or None
    tracks = [x for x in ("oracle", "architect", "muse", "landing") if x in dirs]
    got, none_, nums = {}, [], {}
    for t in tracks:
        cp = dirs.get(t)
        doc, _why = read_track_status(cp, rid) if cp is not None else (None, "its checkpoints were not given")
        c = (doc or {}).get("content") if isinstance(doc, dict) else None
        if isinstance(c, dict) and isinstance(c.get("units"), int) and not isinstance(c.get("units"), bool):
            got[t], nums[t] = c, doc.get("invocation_number")
        else:
            none_.append(names[t])

    def tot(k):
        return sum(int(c.get(k) or 0) for c in got.values())

    def part(t, c):
        n = nums.get(t)
        return f"{names[t]} {c['units']}" + (f" of invocation {n}" if isinstance(n, int) and not isinstance(n, bool) and n > 1 else "")
    out = [f"Content — published, each track's current invocation of run {rid_ or '(no run id)'}: {tot('omitted')} omitted, "
           f"{tot('rejected_kept_base')} kept from Block 1, {tot('fallback_default')} defaults, {tot('trimmed')} trimmed; units {tot('units')} ("
           + ", ".join(part(t, c) for t, c in got.items()) + ")"]
    if none_:
        out.append(f"Content — not recorded this run: {', '.join(none_)}")
    allr = {}
    for t in tracks:
        cp = dirs.get(t)
        f = Path(cp) / f"dispositions_{_safe_rid(rid_)}.jsonl" if cp is not None and rid_ else None
        if f is None or not f.is_file():
            continue
        try:
            r_ = read_records(f, "run", rid=rid_, strict=False)
        except Exception:  # noqa: BLE001 — a file the reader cannot read stays out of the sum, as a status without content does
            continue
        allr[t] = ([x for x in r_.lines if x.get("line") == "unit"], len({x.get("invocation_id") for x in r_.lines if x.get("invocation_id")}))
    if any(n > 1 for _u, n in allr.values()):
        def tot2(k):
            return sum(1 for u, _n in allr.values() for x in u if x.get("disposition") == k)
        out.append(f"Content — all invocations of run {rid_}: {tot2('omitted')} omitted, {tot2('rejected_kept_base')} kept from Block 1, "
                   f"{tot2('fallback_default')} defaults, {tot2('trimmed')} trimmed; units {sum(len(u) for u, _n in allr.values())} ("
                   + ", ".join(f"{names[t]} {len(u)}" + (f" in {n} invocations" if n > 1 else "") for t, (u, n) in allr.items()) + ")")
    return out


def stale_pages(pages, rid):
    """BUILD IC-6 (D7): [(track, the run its page prints)] for every page of another run — a page that prints no run id is not judged."""
    return [(t, r) for t, p in pages.items() for r in [page_run_of(p)] if r is not None and rid and r != rid]


def block_words(track):
    """BUILD IC-6 (D3): pipeline.py's line as a harness starts — the time left in the block and when this track's calls must end; None
    with no block end."""
    end = _block_end_epoch()
    if end is None:
        return None
    b = block_number()
    left = end - _wall()
    if track == "landing":
        return f"Block {b or '?'} clock: {left:.0f} s left — the hub makes no model call"
    later, tail = _later(track, b), _tail(track, b)
    res = ", ".join(f"{('the hub' if t == 'landing' else 'the ' + t.capitalize())} {s} s" for t, s in later.items())
    return (f"Block {b or '?'} clock: {left:.0f} s left; this track's calls end within {max(0.0, left - sum(later.values()) - tail):.0f} s "
            f"(the later tracks' reserves: {res}; its own time outside its calls: {tail} s)")


# ═══ BUILD IC-4 (Q-SWITCH; D11-D16): the one switch every harness reads ═══
SWITCH_OFF_WORDS = ("0", "off", "false", "no")
_IC4_REQUESTS = all(os.environ.get(v, "").strip().lower() not in SWITCH_OFF_WORDS for v in ("NAIPC_IC4_PROMPT", "NAIPC_IC3_PROMPT"))


_IC5_REQUESTS = all(os.environ.get(v, "").strip().lower() not in SWITCH_OFF_WORDS
                    for v in ("NAIPC_IC5_PROMPT", "NAIPC_IC4_PROMPT", "NAIPC_IC3_PROMPT", "NAIPC_IC2_PROMPT"))


# ═══ BUILD IC-6 (D10, G11): a command-line tool, found and called with no character a shell could read ═══
TOOL_ARG_RE = re.compile(r"^[A-Za-z0-9_:\\/. ~-]+$")   # every element of a tool's command: letters, digits and _ : \ / . space ~ - (a
#   Windows short name's ~); no character cmd.exe reads — a .cmd runs in a shell whatever the arguments (CERT/CC VU#123335)


def find_tool(name, cwd=None, harness_dir=None):
    """BUILD IC-6 (D10, G11): a command-line tool's absolute path — shutil.which (on Windows PATHEXT finds mmdc.cmd, and "the current
    directory is always prepended to the path"), refused when the path, as found or resolved, lies inside the working folder or the
    harness's folder (a copy beside the code is never run). → (path, None), or (None, why) — why names the path for the console."""
    import shutil as _sh
    found = _sh.which(name)
    if not found:
        return None, f"{name} is not on this machine's PATH"
    ap = Path(os.path.abspath(found))
    for d in (cwd if cwd is not None else os.getcwd(), harness_dir):
        if d is None:
            continue
        dd = Path(os.path.abspath(d))
        for p_ in (ap, ap.resolve()):
            if p_ == dd or dd in p_.parents or dd.resolve() in p_.parents:
                return None, f"{name} was found at {ap}, inside {dd} — refused: a tool is never run from the working folder or the harness's folder"
    return str(ap), None


# ═══ BUILD IC-6 (D8): a switch per call ═══
IC6_SWITCHES = ("NAIPC_IC6_MUSE_UPDATE", "NAIPC_IC6_DELIVERABLES", "NAIPC_IC6_MASTER", "NAIPC_IC6_CONTINUE")
_IC6_BASE = all(os.environ.get(v, "").strip().lower() not in SWITCH_OFF_WORDS
                for v in ("NAIPC_IC6_PROMPT", "NAIPC_IC5_PROMPT", "NAIPC_IC4_PROMPT", "NAIPC_IC3_PROMPT", "NAIPC_IC2_PROMPT"))
_IC6 = {v: _IC6_BASE and os.environ.get(v, "").strip().lower() not in SWITCH_OFF_WORDS for v in IC6_SWITCHES}


def ic6(name):
    """BUILD IC-6 (D8): one of this build's switches — on unless set to 0, off, false or no (any case); NAIPC_IC6_PROMPT off turns all
    four off, and so does any older switch off (NAIPC_IC5_PROMPT … NAIPC_IC2_PROMPT: these requests build on IC-5's), as _IC5_REQUESTS
    does. Read once, when the module loads. A KeyError for a name that is no switch."""
    return _IC6[name]


# BUILD IC-6 (D8, I7): each switch's declared reach — the calls whose request it may change. tests/t_switches_ic6.py captures every
# request of both blocks with each switch off alone and fails when a measured reach differs from this, either way; a switch is named as
# a way back only from evidence/switch_reach_ic6.json. The older switches' reach is what that file measures (G8: NAIPC_IC5_PROMPT=0
# left the Muse's update request as it was).
# D14 (the master in lines) is cut (§9's cut order: first): NAIPC_IC6_MASTER reaches no call while it is cut — its name kept, so a run
# turn that sets it changes nothing (measured); tests/t_promises_ic6.py holds the cut's promise check and its trigger
SWITCH_REACH = {"NAIPC_IC6_MUSE_UPDATE": ("muse/update",), "NAIPC_IC6_DELIVERABLES": ("oracle/deliverables",),
                "NAIPC_IC6_MASTER": (), "NAIPC_IC6_CONTINUE": ("the continuation requests",),
                "NAIPC_IC6_PROMPT": ("muse/update", "oracle/deliverables", "the continuation requests", "muse/call1", "muse/call2"),
                # BUILD IC-7 (Q-SWITCH): IC-7's switch builds on _IC6_BASE, so NAIPC_IC6_PROMPT off turns it off too (the Muse's Calls 1
                # and 2 are then IC-6's); NAIPC_IC7_PROMPT reaches the Muse's three requests and nothing else (tests/t_switches_ic7.py)
                "NAIPC_IC7_PROMPT": ("muse/call1", "muse/call2", "muse/update")}
# BUILD IC-6 (D8; D12–D14): the calls Part 2 makes line-form under their own switch — each step adds its call when it is built
LINES_SWITCH = {"muse/update": "NAIPC_IC6_MUSE_UPDATE",      # D12: the Muse's update as keyed edits
                "oracle/deliverables": "NAIPC_IC6_DELIVERABLES"}   # D13: the deliverables item by item


def call_form(key):
    """BUILD IC-6 (D8): a call's form as it runs — "lines" for a Part 2 call while its switch is on (LINES_SWITCH), else its profile's."""
    sw = LINES_SWITCH.get(key)
    return "lines" if sw and ic6(sw) else CALL_PROFILES[key]["form"]


# ═══ BUILD IC-7 (D1–D5, D7, D10): one contract per call — the switch, the strict wire and its lint, the admission, the second attempt's
# errors, the statement check, a failed track's page, the run's record. Standard library only (T1″): the harnesses hold pydantic. ═══
BUILD_ID = "IC-8"           # BUILD IC-8 (d16c): the one build literal — BUILD_META is made from it, and each harness gives it to page_env,
#                             whose templates print {{ naipc_build | e }} (IC-7 wrote it in six places: these two lines and the four templates)
BUILD_META = f'<meta name="naipc-build" content="{BUILD_ID}">'
NOT_BUILT_META = '<meta name="naipc-page" content="not-built">'
_IC7_REQUESTS = _IC6_BASE and os.environ.get("NAIPC_IC7_PROMPT", "").strip().lower() not in SWITCH_OFF_WORDS


_IC8_REQUESTS = _IC7_REQUESTS and os.environ.get("NAIPC_IC8_PROMPT", "").strip().lower() not in SWITCH_OFF_WORDS


ORACLE_WINDOW_HAND = (840.0, 850.0, 900.0)      # BUILD IC-8 (D17): Block 2 and a harness run by hand — IC-7's window, as it was


def oracle_window(block=None):
    """BUILD IC-8 (D17): the Oracle's window in seconds — (the master's stage, the Oracle's own end, the pipeline's timeout for the
    Oracle). Block 1: the stage is 2 × the master's planning seconds + 60 s (the longest wait the Oracle's _retry_wait returns), so a
    first attempt and its second both fit; the end is the stage + the deliverables' planning seconds + 10 s, and the timeout the end + 50 s
    (IC-7's 850 − 840 and 900 − 850). Block 2, and a harness run by hand: ORACLE_WINDOW_HAND. Every call's end is still the earlier of its
    own and the block's clock (block_end_for). `block`: 1 or 2 (an int or its digit), else NAIPC_BLOCK — the pipeline sets it for each
    child, a resume from the run's own record: block_number reads it)."""
    b = int(str(block).strip()) if str(block).strip() in ("1", "2") else block_number()
    if b != 1:
        return ORACLE_WINDOW_HAND
    stage = 2 * float(profile("oracle/master")["planning_s"]) + 60.0
    end = stage + float(profile("oracle/deliverables")["planning_s"]) + 10.0
    return (round(stage, 1), round(end, 1), round(end + 50.0, 1))


def ic8_prompt() -> bool:
    """BUILD IC-8 (Q-SWITCH): IC-8's request changes — unless NAIPC_IC8_PROMPT is 0, off, false or no (any case), or a switch it builds on
    is (NAIPC_IC7_PROMPT and the older ones): then IC-7's request bodies, byte for byte. Read once, when the module loads. Python's side
    (the shown text, the finalizer, the dispositions, the pages) has no switch."""
    return _IC8_REQUESTS


def ic7_prompt() -> bool:
    """BUILD IC-7 (Q-SWITCH): the Muse's Calls 1 and 2 under the strict wire (D5) and the Muse's three calls with their own rule lines
    (D4) — unless NAIPC_IC7_PROMPT is 0, off, false or no (any case), or a switch it builds on is (NAIPC_IC6_PROMPT and the older ones,
    as _IC6_BASE reads them): then IC-6's request bodies, byte for byte. Read once, when the module loads. Python's side (the admission,
    the second attempt, the pages, the hub, the console, the re-run) has no switch."""
    return _IC7_REQUESTS


# ── D5: the wire ──
WIRE_KEYS = ("type", "properties", "required", "additionalProperties", "items", "description", "$defs", "$ref", "minItems")
WIRE_TYPES = ("object", "array", "string", "integer", "number", "boolean")
WIRE_LIMITS = {"optional": 24, "union": 16}     # the structured-outputs page's stated limits (fetched 11/17); this build sends 0 of each
_QUOTED = re.compile(r"(?<![A-Za-z0-9])'[^'\n]+'(?![A-Za-z0-9])|\"[^\"\n]*\"|\be\.g\.|\bfor example\b|\bsuch as\b", re.I)


def _deref(node, defs):
    n = 0
    while isinstance(node, dict) and "$ref" in node and n < 50:
        node = defs.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
        n += 1
    return node if isinstance(node, dict) else {}


def _unoptional(node, defs):
    """Optional[X] (anyOf [X, null]) → X; any other node as it is, its $ref followed."""
    node = _deref(node, defs)
    alts = node.get("anyOf")
    if isinstance(alts, list):
        keep = [_deref(a, defs) for a in alts if _deref(a, defs).get("type") != "null"]
        if len(keep) == 1:
            return keep[0]
    return node


def wire_schema(page_schema, drop=(), objects=None, descriptions=None, add=None, min_one=()):
    """BUILD IC-7 (D5): a reply's strict wire schema, built by Python from its page model's JSON schema (model_json_schema()) — never
    the SDK's transform. Every object closed (additionalProperties false), every property required; Optional[X] sent as X (Python keeps
    the default); inlined, no $ref; no bound, default, format, pattern, const, enum or title (Python keeps every bound) but minItems 1 on
    the lists named in `min_one`. `drop`: paths left off the wire (a field the request does not ask for: Python's default fills it);
    `objects`: {path of a free-key dict: [its keys]}, sent as an object of those text properties; `descriptions`: {path: form-only
    words} (no other description is sent); `add`: {top-level name: wire node}, added and required (a field asked outside the page model:
    brief_answers). A path is "a.b" for a property, "a[]" for a list's items. ValueError on a node the wire cannot send."""
    defs = page_schema.get("$defs") or {}
    objects, descriptions, drop, min_one = dict(objects or {}), dict(descriptions or {}), set(drop or ()), set(min_one or ())

    def desc(n, path):
        return dict(n, description=descriptions[path]) if path in descriptions else n

    def build(node, path):
        if path in objects:
            keys = list(objects[path])
            return desc({"type": "object", "properties": {k: desc({"type": "string"}, f"{path}.{k}") for k in keys},
                         "required": list(keys), "additionalProperties": False}, path)
        node = _unoptional(node, defs)
        t = node.get("type")
        if t == "object":
            props = {}
            for k, v in (node.get("properties") or {}).items():
                p = f"{path}.{k}" if path else k
                if p not in drop:
                    props[k] = build(v, p)
            if not props:
                raise ValueError(f"wire_schema: {path or '(top)'} is an object with no property the wire can send (a free-key dict "
                                 "needs its keys in `objects`)")
            return desc({"type": "object", "properties": props, "required": list(props), "additionalProperties": False}, path)
        if t == "array":
            out = {"type": "array", "items": build(node.get("items") or {}, f"{path}[]")}
            if path in min_one:
                out["minItems"] = 1
            return desc(out, path)
        if t in ("string", "integer", "number", "boolean"):
            return desc({"type": t}, path)
        raise ValueError(f"wire_schema: {path or '(top)'} has no type the wire can send ({t!r})")

    out = build(page_schema, "")
    for k, v in (add or {}).items():
        out["properties"][k] = v
        out["required"].append(k)
    return out


def schema_problems(wire, numbers=()):
    """BUILD IC-7 (D5): what in a wire schema the API or this build refuses — [] when nothing. It reads structure, never text: a property
    named title, description, type, items, format or pattern is a property (a key of a `properties` map), never a keyword. Refused: a
    keyword outside WIRE_KEYS; additionalProperties other than false; minItems other than 0 or 1; a type outside WIRE_TYPES; a union;
    an object with empty properties; a property not required (an optional one); a required name with no property; a $ref to no
    definition; a description that quotes an example ('…', "…", e.g., for example, such as) or holds a number outside `numbers` (the
    contract's own bounds); more optional properties or unions than the page's limits (each counted)."""
    probs, counts = [], {"optional": 0, "union": 0}
    defs = wire.get("$defs") if isinstance(wire, dict) and isinstance(wire.get("$defs"), dict) else {}
    allowed = {str(x) for x in numbers}

    def walk(n, path, depth):
        where = path or "(top)"
        if depth > 60:
            probs.append(f"{where}: nested too deep (a $ref cycle?)")
            return
        if not isinstance(n, dict):
            probs.append(f"{where}: not a schema node")
            return
        for k in n:
            if k not in WIRE_KEYS:
                probs.append(f"{where}: the keyword {k!r} is not sent")
        if "$ref" in n:
            ref = str(n["$ref"]).rsplit("/", 1)[-1]
            if ref not in defs:
                probs.append(f"{where}: $ref to no definition ({ref})")
                return
            walk(defs[ref], path, depth + 1)
            return
        if "additionalProperties" in n and n["additionalProperties"] is not False:
            probs.append(f"{where}: additionalProperties is not false")
        if "minItems" in n and n["minItems"] not in (0, 1):
            probs.append(f"{where}: minItems {n['minItems']!r} (only 0 or 1 is sent)")
        t = n.get("type")
        if isinstance(t, list) or any(k in n for k in ("anyOf", "oneOf", "allOf")):
            counts["union"] += 1
            probs.append(f"{where}: a union")
        elif t not in WIRE_TYPES:
            probs.append(f"{where}: the type {t!r}")
        d = n.get("description")
        if d is not None:
            if not isinstance(d, str):
                probs.append(f"{where}: a description that is not text")
            else:
                if _QUOTED.search(d):
                    probs.append(f"{where}: the description quotes an example")
                for num in re.findall(r"\d+(?:[.,]\d+)*", d):
                    if num not in allowed:
                        probs.append(f"{where}: the description holds the number {num}")
        if t == "object":
            props = n.get("properties")
            if not isinstance(props, dict) or not props:
                probs.append(f"{where}: an object with empty properties")
                props = props if isinstance(props, dict) else {}
            if n.get("additionalProperties") is not False:
                probs.append(f"{where}: an object without additionalProperties false")
            req = n.get("required") if isinstance(n.get("required"), list) else []
            for k in props:
                if k not in req:
                    counts["optional"] += 1
                    probs.append(f"{(path + '.') if path else ''}{k}: optional")
            for k in req:
                if k not in props:
                    probs.append(f"{where}: required {k!r} has no property")
            for k, v in props.items():
                walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif t == "array":
            walk(n.get("items"), f"{path}[]", depth + 1)
    walk(wire, "", 0)
    if counts["optional"] > WIRE_LIMITS["optional"] or counts["union"] > WIRE_LIMITS["union"]:
        probs.append(f"over the page's limits: {counts} against {WIRE_LIMITS}")
    return probs


def wire_counts(wire):
    """BUILD IC-7 (D5, R3): a wire schema's counts — properties, lists, the optional ones and the unions (each 0 here), empty objects,
    keywords outside the allowlist."""
    c = {"properties": 0, "lists": 0, "optional": 0, "union": 0, "empty_objects": 0, "keywords_outside": 0}

    def walk(n, depth=0):
        if not isinstance(n, dict) or depth > 60:
            return
        c["keywords_outside"] += sum(1 for k in n if k not in WIRE_KEYS)
        if any(k in n for k in ("anyOf", "oneOf", "allOf")) or isinstance(n.get("type"), list):
            c["union"] += 1
        if n.get("type") == "object":
            props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
            if not props:
                c["empty_objects"] += 1
            req = n.get("required") if isinstance(n.get("required"), list) else []
            c["properties"] += len(props)
            c["optional"] += sum(1 for k in props if k not in req)
            for v in props.values():
                walk(v, depth + 1)
        elif n.get("type") == "array":
            c["lists"] += 1
            walk(n.get("items"), depth + 1)
    walk(wire)
    return c


# ── D2: the admission ──
ADMIT_RULES = ("null→text", "null→list", "null→default", "missing→text", "missing→list", "numeric text→number", "number→text", "item→text",
               "cut", "items over the maximum",
               "extra key", "key twice (the last kept)", "missing (Python's default)", "not written this run", "item left out",
               "list short of the asked count", "normalized")
ADMISSION_FILE = "admission_{rid}.jsonl"


def _seen(v):
    return ("null" if v is None else "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float)
            else "str" if isinstance(v, str) else "array" if isinstance(v, list) else "object" if isinstance(v, dict) else type(v).__name__)


def admit_walk(raw, page_schema, display_numbers=(), null_text=(), null_list=(), null_default=(), cut=None, text_items=(), objects=None,
               not_asked=(), missing_text=(), extra="drop", max_cut=True):
    """BUILD IC-7 (D2): a reply's declared conversions, before strict validation → (value, entries, findings). Each entry is
    {path, rule, seen} (seen: the JSON type the reply wrote — never a value). The rules, each only where its path declares it:
      null→text, null→list   null on a path in `null_text` / `null_list` (the paths whose models made null "" or [] before IC-7);
      null→default           null on an Optional path in `null_default` (Python writes its own value: the taglines);
      missing→text, missing→list   a missing key on a path in `missing_text` (the models that filled a missing key with "" or [] before
                             IC-7: KPI, DataPoint, PartnerPitch) — filled the same way, now counted;
      numeric text→number    a numeric text on a number path (exact, finite, whole for an int; never a bool);
      number→text            a JSON number on a declared display-number path (`display_numbers`): str() of it (as IC-6's reach
                             validator wrote it), counted unlinked on the page;
      item→text              a number or a bool as an item of a declared text list (`text_items`); a null item there is left out;
      cut                    a text over its maxLength cut at a word boundary, on a path in `cut` (the taglines' soft truncation);
      items over the maximum a list over its maxItems keeps its first items (Q-LIST);
      extra key              a key no schema lists (model objects; the declared keys of a free-key dict, `objects`): left out, named;
      not written this run   an asked, non-required field missing (Python's default; `not_asked` paths are filled without a word).
    `extra`: "drop" (the default: an extra key is left out and named) or "keep" (kept, not named: a caller that names them itself —
    the Muse's update).
    `max_cut`: True (the default: a list over its maxItems keeps its first items, Q-LIST — Calls 1 and 2, the master) or False (the list
    left whole for strict validation to refuse — the Muse's update, whose check refuses a part over its maximum and keeps Block 1's;
    BUILD IC-7 Phase 4).
    `findings`: [{loc, type}] that strict validation cannot see — a declared key of a free-key dict missing, an item of a text list that
    is no text — resolved by admit() like validation errors. Paths as wire_schema's ("a.b", "a[]"). Never raises."""
    import math
    defs = page_schema.get("$defs") or {}
    entries, findings = [], []
    cut, objects = dict(cut or {}), dict(objects or {})
    sets = {k: set(v or ()) for k, v in (("display", display_numbers), ("null_text", null_text), ("null_list", null_list),
                                          ("null_default", null_default), ("text_items", text_items), ("not_asked", not_asked),
                                          ("missing_text", missing_text))}

    def note(path, rule, v, **kw):
        entries.append(dict({"path": path, "rule": rule, "seen": _seen(v)}, **kw))

    def walk(v, node, path, loc):
        raw_node = _deref(node, defs)
        nullable = isinstance(raw_node.get("anyOf"), list) and any(_deref(a, defs).get("type") == "null" for a in raw_node["anyOf"])
        node = _unoptional(node, defs)
        t = node.get("type")
        if v is None:
            if path in sets["null_text"] and t == "string":
                note(path, "null→text", v)
                return ""
            if path in sets["null_list"] and t == "array":
                note(path, "null→list", v)
                return []
            if nullable and path in sets["null_default"]:
                note(path, "null→default", v)
            return v
        if path in objects and isinstance(v, dict):
            keys = list(objects[path])
            out = {}
            for k, x in v.items():
                if k in keys:
                    out[k] = walk(x, {"type": "string"}, f"{path}.{k}", loc + [k])
                else:
                    note(f"{path}.<extra key>", "extra key", x)
            for k in keys:
                if k not in out:
                    findings.append({"loc": loc + [k], "type": "missing"})
            return out
        if t == "object" and isinstance(v, dict) and not node.get("properties"):
            ap = node.get("additionalProperties")      # a free-key dict no contract declares: each value walked by its schema; a
            if isinstance(ap, dict):                   # dict of any values (list[dict]: additionalProperties true) kept as it is —
                return {k: walk(x, ap, f"{path}.{k}", loc + [k]) for k, x in v.items()}   # never a model with every key extra
            return v
        if t == "object" and isinstance(v, dict):
            props = node.get("properties") or {}
            req = set(node.get("required") or [])
            out = {}
            for k, x in v.items():
                if k in props:
                    out[k] = walk(x, props[k], f"{path}.{k}" if path else k, loc + [k])
                elif extra == "keep":
                    out[k] = x
                else:
                    note(f"{path}.<extra key>" if path else "<extra key>", "extra key", x)
            for k in props:
                p = f"{path}.{k}" if path else k
                if k not in v and p in sets["missing_text"]:
                    kt = _unoptional(props[k], defs).get("type")
                    note(p, "missing→list" if kt == "array" else "missing→text", None)
                    out[k] = [] if kt == "array" else ""
                elif k not in v and k not in req and p not in sets["not_asked"]:
                    note(p, "not written this run", None)
            return out
        if t == "array" and isinstance(v, list):
            it, ip, out = node.get("items") or {}, f"{path}[]", []
            mx = node.get("maxItems")
            if max_cut and isinstance(mx, int) and not isinstance(mx, bool) and len(v) > mx:     # BUILD IC-7 Phase 4: the caller's contract
                note(path, "items over the maximum", v, left_out=len(v) - mx)
                v = v[:mx]
            for i, x in enumerate(v):
                if ip in sets["text_items"]:
                    if x is None:
                        findings.append({"loc": loc + [i], "type": "null item"})
                        out.append(x)
                        continue
                    if isinstance(x, (int, float, bool)):
                        note(ip, "item→text", x)
                        out.append(str(x))
                        continue
                    if not isinstance(x, str):
                        findings.append({"loc": loc + [i], "type": "not text"})
                        out.append(x)
                        continue
                out.append(walk(x, it, ip, loc + [i]))
            return out
        if t == "string":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and path in sets["display"] and math.isfinite(float(v)):
                note(path, "number→text", v)
                return str(v)
            return v          # BUILD IC-8 (D1, F1): the admission cuts no text — a display limit is the page's, on its shown text
        if t in ("integer", "number") and isinstance(v, str):
            s = v.strip()
            if t == "integer" and re.fullmatch(r"[+-]?\d{1,15}", s):
                note(path, "numeric text→number", v)
                return int(s)
            if t == "number" and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", s) and math.isfinite(float(s)):
                note(path, "numeric text→number", v)
                return float(s)
        return v
    try:
        return walk(raw, page_schema, "", []), entries, findings
    except Exception:  # noqa: BLE001 — the walk never decides: strict validation names what it could not read
        return raw, entries, findings


def _path_of(loc):
    """A validation location (('personas', 2, 'age')) as a contract path ("personas[].age")."""
    out = ""
    for x in loc:
        if isinstance(x, int) and not isinstance(x, bool):
            out += "[]"
        else:
            out += (f".{x}" if out else str(x))
    return out or "(the reply)"


def _expected(node, defs, path, forms):
    node = _unoptional(node, defs)
    t = node.get("type")
    base = {"string": "text", "integer": "a whole number", "number": "a number", "boolean": "true or false",
            "array": "a list", "object": "an object with its fields"}.get(t, "a value of its schema")
    if t in ("integer", "number") and (node.get("minimum") is not None or node.get("maximum") is not None):
        base += f" from {node.get('minimum', '…')} to {node.get('maximum', '…')}"
    if t == "array" and node.get("minItems"):
        base += f" of at least {node['minItems']} item" + ("s" if node["minItems"] != 1 else "")
    if t == "string" and node.get("maxLength"):
        base += f" of at most {node['maxLength']} characters"
    return base + (f", in the form {forms[path]}" if path in forms else "")


def _wrote(v):
    """What the reply wrote at a path, for the second attempt's request only (never logged, never printed)."""
    if v is _MISSING:
        return "nothing (the field is missing)"
    if isinstance(v, list):
        return f"a list of {len(v)} item" + ("s" if len(v) != 1 else "")
    if isinstance(v, dict):
        return "an object"
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 160 else s[:157] + "…"


def _locate(value, loc, page_schema, objects):
    """Walk a validation location through the value and the schema → (steps, the value there or _MISSING, the schema node there).
    Each step: {kind: "prop" | "item", container, key, required, path}. A name in the location that is no property of the node (a union's
    member) is passed over."""
    defs = page_schema.get("$defs") or {}
    node, cur, steps, path = page_schema, value, [], ""
    for x in loc:
        n = _unoptional(node, defs)
        if isinstance(x, int) and not isinstance(x, bool) and n.get("type") == "array":
            steps.append({"kind": "item", "container": cur if isinstance(cur, list) else None, "key": x, "path": path + "[]"})
            path += "[]"
            node = n.get("items") or {}
            cur = cur[x] if isinstance(cur, list) and 0 <= x < len(cur) else _MISSING
            continue
        if isinstance(x, str) and path in objects:
            steps.append({"kind": "prop", "container": cur if isinstance(cur, dict) else None, "key": x, "required": True,
                          "path": f"{path}.{x}"})
            path, node = f"{path}.{x}", {"type": "string"}
            cur = cur.get(x, _MISSING) if isinstance(cur, dict) else _MISSING
            continue
        props = n.get("properties") or {}
        if isinstance(x, str) and x in props:
            p = f"{path}.{x}" if path else x
            steps.append({"kind": "prop", "container": cur if isinstance(cur, dict) else None, "key": x,
                          "required": x in set(n.get("required") or []), "path": p})
            path, node = p, props[x]
            cur = cur.get(x, _MISSING) if isinstance(cur, dict) else _MISSING
            continue
        if isinstance(x, str) and isinstance(n.get("additionalProperties"), dict):
            p = f"{path}.{x}"
            steps.append({"kind": "prop", "container": cur if isinstance(cur, dict) else None, "key": x, "required": True, "path": p})
            path, node = p, n["additionalProperties"]
            cur = cur.get(x, _MISSING) if isinstance(cur, dict) else _MISSING
            continue
        # a union member's tag, or a location past the schema: passed over
    return steps, cur, node


def admit(raw, page_schema, validate, rules=None, forms=None):
    """BUILD IC-7 (D2, I2, I3): one reply's admission → (obj, entries, errors).
    `validate(value)` is the harness's strict validator → (obj, None) or (None, [{loc, type}]) (pydantic's errors as plain dicts, no
    input). `rules`: admit_walk's keywords. Steps: the declared conversions (admit_walk); then strict validation; each error resolved
    from the deepest point of its location up — a field that is not required takes its model's default ("not written this run",
    named); else the innermost list item that holds it is left out ("item left out", named); else the path is essential and goes to
    `errors` (D3: [{path, expected, wrote}]; `wrote` is the model's own value, for the second attempt's request only). Repeated until the
    reply validates or an essential error is left; a list left with fewer items than its minimum fails validation and so goes to D3 only
    when it is essential (its own default otherwise). `forms`: {path: the token form the contract states}, for `expected`. The
    entries name paths and rules only. Never raises: a validator that raises is an essential error of the whole reply."""
    rules, forms = dict(rules or {}), dict(forms or {})
    objects = dict(rules.get("objects") or {})
    defs = page_schema.get("$defs") or {}
    value, entries, findings = admit_walk(raw, page_schema, **{k: v for k, v in rules.items() if k in (
        "display_numbers", "null_text", "null_list", "null_default", "cut", "text_items", "objects", "not_asked", "missing_text")})
    pending = [dict(f) for f in findings]
    for _round in range(200):
        errs = pending
        pending = []
        if not errs:
            try:
                obj, errs = validate(value)
            except Exception as e:  # noqa: BLE001
                return None, entries, [{"path": "(the reply)", "expected": "an object of its schema", "wrote": type(e).__name__}]
            if obj is not None:
                return obj, entries, []
        drops, defaults, essential = {}, [], []
        for e in errs or []:
            loc = list(e.get("loc") or [])
            steps, there, node = _locate(value, loc, page_schema, objects)
            act = None
            for st in reversed(steps):
                if st["kind"] == "prop" and not st["required"] and st["container"] is not None and st["key"] in st["container"]:
                    act = ("default", st)
                    break
                if st["kind"] == "item" and st["container"] is not None and 0 <= st["key"] < len(st["container"]):
                    act = ("drop", st)
                    break
            if act is None:
                p = _path_of(loc)
                essential.append({"path": p, "expected": _expected(node, defs, p, forms), "wrote": _wrote(there), "type": e.get("type")})
            elif act[0] == "default":
                defaults.append(act[1])
            else:
                drops.setdefault(id(act[1]["container"]), (act[1]["container"], act[1]["path"], set()))[2].add(act[1]["key"])
        if essential:
            seen_, uniq = set(), []
            for x in essential:
                if x["path"] not in seen_:
                    seen_.add(x["path"])
                    uniq.append(x)
            return None, entries, uniq
        changed = False
        for st in defaults:
            if st["key"] in st["container"]:
                note_v = st["container"].pop(st["key"])
                entries.append({"path": st["path"], "rule": "not written this run", "seen": _seen(note_v)})
                changed = True
        for cont, p, idxs in drops.values():
            for i in sorted(idxs, reverse=True):
                if 0 <= i < len(cont):
                    gone = cont.pop(i)
                    entries.append({"path": p, "rule": "item left out", "seen": _seen(gone)})
                    changed = True
        if not changed:
            return None, entries, [{"path": _path_of(list(e.get("loc") or [])), "expected": "a value of its schema", "wrote": "",
                                    "type": e.get("type")} for e in (errs or [])][:20] or [
                {"path": "(the reply)", "expected": "an object of its schema", "wrote": ""}]
    return None, entries, [{"path": "(the reply)", "expected": "an object of its schema", "wrote": "too many errors"}]


def admission_counts(entries):
    """{rule: count} of a reply's admission entries."""
    c = {}
    for e in entries or []:
        c[e.get("rule")] = c.get(e.get("rule"), 0) + 1
    return c


def admission_line(call, entries, outcome):
    """BUILD IC-7 (D2): the console's line — the call, its outcome and the counts by rule; paths and rules only (plain, through say)."""
    c = admission_counts(entries)
    return f"    admission ({call}): {outcome}" + (" — " + ", ".join(f"{n} {r}" for r, n in sorted(c.items())) if c else ", nothing converted")


def admission_record(cp_dir, call, entries, outcome, attempt=1):
    """BUILD IC-7 (D2): one line per admission in <checkpoints>/admission_<run id>.jsonl — the run, the call, its attempt, the outcome,
    the counts by rule, and each entry's path, rule and the JSON type seen (and how many items a cut list left out); never a value or a
    reply. Never raises."""
    try:
        d = Path(cp_dir)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"run_id": run_id(), "call": call, "attempt": attempt, "outcome": outcome, "counts": admission_counts(entries),
               "generation_attempt": 2 if attempt == 2 else 1, "call_id": last_call_id(call),       # BUILD IC-8 (D8)
               **{k: v for k, v in identity(cp_dir).items() if k != "run_id"},
               "entries": [{k: e[k] for k in ("path", "rule", "seen", "left_out", "short") if k in e} for e in entries or []],
               "at": _now_iso()}
        with open(d / ADMISSION_FILE.format(rid=re.sub(r"[^A-Za-z0-9_-]+", "-", str(run_id()))), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def admission_lines(cp_dir, rid=None):
    """BUILD IC-7 (D12 row 7b): this run's admission records, in order ([] when none); never raises."""
    rid = rid or run_id()
    try:            # BUILD IC-8 (D9): through the one reader — the run's lines, each upcast (a legacy line's call id and correlation)
        return read_records(Path(cp_dir) / ADMISSION_FILE.format(rid=re.sub(r"[^A-Za-z0-9_-]+", "-", str(rid))), "run", rid=rid,
                            strict=False).lines
    except (OSError, ValueError):
        return []


def errors_block(errors):
    """BUILD IC-7 (D3): the block a second attempt adds at the end of its user message — each error of the first attempt with its path,
    the type and form it must have (from the contract) and what the reply wrote (the model's own text: in this request only, never
    logged). `errors`: [{path, expected, wrote}]."""
    lines = ["<errors_of_the_first_attempt>",
             "Your first reply did not pass these checks. Each line names a field, what it must be, and what the reply wrote:"]
    for e in errors:
        lines.append(f"- {e.get('path')}: must be {e.get('expected')}; the reply wrote {e.get('wrote')}")
    lines += ["Write the whole reply again with these fields corrected.", "</errors_of_the_first_attempt>"]
    return "\n".join(lines)


class AdmissionFailed(RuntimeError):
    """BUILD IC-7 (D3, I3): a reply whose essential paths stayed invalid — after its second attempt, or with no time left for one. `plain`:
    the words the track's status and its not-built page print; `errors`: [{path, expected, wrote}] (never logged)."""

    def __init__(self, msg, plain=None, errors=None):
        super().__init__(msg)
        self.plain = plain or "its model reply could not be used after a second attempt"
        self.errors = list(errors or [])


NO_TIME_WORDS = "its model reply could not be used, and no time was left for a second attempt"


def second_request(user, errors):
    """BUILD IC-7 (D3, I4): the second attempt's user message — the first's, then the errors block (never ending on an assistant turn;
    the system prompt and the schema unchanged)."""
    return f"{user}\n\n{errors_block(errors)}"


# ── D1, D4: the statement check ──
_TYPED_LINE = re.compile(r"- A field the reply's schema types as a number \(([^)]*)\) holds a plain number")
_COUNT_LINE = re.compile(r"- Where you say how many items a list holds, write \[\[count:<list>\]\], <list> being the list's field name in "
                         r"([^\n(]*)\n?\s*\(([^)]*)\)")
_NAME_LINE = re.compile(r"- A name you write[^\n]*\n?[^\n]*?the number goes in (?:the field that holds it\s*\(([^)]*)\)|your prose)")
_COUNT_TOKEN = re.compile(r"\[\[\s*count\s*:\s*([A-Za-z_]\w*)\s*\]\]")
_ASKED = re.compile(r'"([a-z_]+)":\s*(?:an?\s+)?array of (\d+)(?:-(\d+))?')
_ASKED_IN = re.compile(r"\b([a-z_]+) \((?:array of )?(\d+)-(\d+) (?:strings|objects)")
_SCHEMA_WORD = re.compile(r"\bschema\b", re.I)
_DATA_BLOCKS = ("civic_data", "registry", "mentions", "muse_block1", "oracle_context", "personas", "brand", "changes", "review_set",
                "block1_analysis", "block1_deliverables", "data", "api_data", "curveball")


def _statements_only(text):
    """A request without its data blocks (the data, the registry, Block 1's output, the change list): what remains is what the request
    itself says. The rules and their <examples> stay."""
    t = text or ""
    for tag in _DATA_BLOCKS:
        t = re.sub(rf"<{tag}>.*?</{tag}>", f"<{tag}/>", t, flags=re.S)
    return t


def _last(p):
    return re.split(r"[.\[\]]+", p.strip(".[]"))[-1] if p else p


def statement_problems(text, view):
    """BUILD IC-7 (D1, D4, I1): a request's statements about its reply read against its call's contract view — [] when they agree. A
    narrow, deterministic check of the statement kinds the requests make (not a reader of prose):
      schema       "schema" said in a request that sends or shows none (view["schema"] None);
      typed line   the typed-number rule in a request whose reply has no number-typed field, or naming a field the contract types as
                   text (G2: "a target" against kpis[].target) or no field of the reply;
      count line   the count rule naming a list that is not one of the reply's (view["count_lists"]); a [[count:<list>]] example the same;
      name line    the name rule naming a field the reply does not have (view["name_fields"]);
      counts       "<field>": array of N[-M] and <field> (N-M strings|objects) outside the contract's bounds (view["lists"]).
    view: {"call", "schema": "sent" | "shown" | None, "number_paths": [...], "text_paths": [...], "count_lists": [...],
    "name_fields": [...], "lists": {path or name: (min, max)}}."""
    probs = []
    t = _statements_only(text)
    nums = list(view.get("number_paths") or [])
    texts = list(view.get("text_paths") or [])
    num_last = {_last(p) for p in nums}
    text_by_last = {}
    for p in texts:
        text_by_last.setdefault(_last(p), []).append(p)
    if view.get("schema") is None:
        for m in _SCHEMA_WORD.finditer(t):
            a = max(0, m.start() - 50)
            probs.append({"kind": "schema", "why": "the request says schema but sends or shows none",
                          "at": t[a:m.end() + 30].replace("\n", " ")})
    for m in _TYPED_LINE.finditer(t):
        if not nums:
            probs.append({"kind": "typed line", "why": "a typed-number rule in a request whose reply has no number-typed field"})
        for w in [x.strip() for x in m.group(1).split(",") if x.strip()]:
            word = re.sub(r"^(?:a|an|the|each|every)\s+", "", w).strip().replace("'s ", " ").split()[-1]
            if word in text_by_last and word not in num_last:
                probs.append({"kind": "typed line", "why": f"the typed-number rule names {w!r}, which the contract types as text "
                              f"({', '.join(text_by_last[word])})"})
            elif nums and word not in num_last and view.get("typed_words", "fields") == "fields":
                probs.append({"kind": "typed line", "why": f"the typed-number rule names {w!r}, no number-typed field of this reply"})
    own = set(view.get("count_lists") or [])
    for m in _COUNT_LINE.finditer(t):
        for w in [x.strip(" .…") for x in m.group(2).split(",") if x.strip(" .…")]:
            if w not in own:
                probs.append({"kind": "count line", "why": f"the count rule names {w!r}, not a list of this reply"})
    for m in _COUNT_TOKEN.finditer(t):
        if m.group(1) not in own:
            probs.append({"kind": "count example", "why": f"[[count:{m.group(1)}]] names a list that is not this reply's"})
    fields = set(view.get("name_fields") or [])
    for m in _NAME_LINE.finditer(t):
        for w in [x.strip() for x in (m.group(1) or "").split(",") if x.strip()]:
            if w not in fields:
                probs.append({"kind": "name line", "why": f"the name rule names the field {w!r}, which this reply does not have"})
    lists = dict(view.get("lists") or {})
    by_last = {}
    for k, v in lists.items():
        by_last.setdefault(_last(k), []).append(v)
    for m in list(_ASKED.finditer(t)) + list(_ASKED_IN.finditer(t)):
        name, lo = m.group(1), int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else lo
        for (mn, mx) in by_last.get(name, []):
            if lo < (mn or 0) or (mx is not None and hi > mx):
                probs.append({"kind": "counts", "why": f"{name}: asked {lo}" + (f"-{hi}" if hi != lo else "")
                              + f", the contract accepts {mn}-{mx if mx is not None else 'any'}"})
    return probs


# ── D7: a failure in plain words; this run's not-built page ──
FAIL_WORDS = {"AdmissionFailed": "its model reply could not be used after a second attempt",
              "StageBudgetExceeded": "its time ran out", "EndTimeReached": "its time ran out", "TimeoutError": "its time ran out",
              "IncompleteResponse": "its model reply did not arrive whole", "MaxTokensReached": "its model reply was cut at its limit",
              "ModelRefused": "the model declined to answer", "Refused": "the model declined to answer",
              "SystemExit": "its harness stopped", "KeyboardInterrupt": "it was stopped by hand"}


def plain_why(exc):
    """BUILD IC-7 (D7, W8): the plain words for a failure — the error's own plain words when it carries them (AdmissionFailed), else
    FAIL_WORDS by its class, else "it stopped with an error"; never a class name or an error's text."""
    p = getattr(exc, "plain", None)
    if isinstance(p, str) and p.strip():
        return p.strip()
    return FAIL_WORDS.get(type(exc).__name__, "it stopped with an error")


def page_is_this_runs(out_dir, track, rid):
    """BUILD IC-7 (D7, I5): (True, None) when <out_dir>/index.html prints this run's id, is no not-built page, and the track's status
    (<out_dir>/../checkpoints) calls it this run's "built"; else (False, why in plain words)."""
    page = Path(out_dir) / "index.html"
    if not page.is_file():
        return False, "no page"
    pr = page_run_of(page)
    if pr != rid:
        return False, f"its page is from run {pr or '(none)'}"
    try:
        if NOT_BUILT_META in page.read_text(encoding="utf-8", errors="replace")[:4000]:
            return False, "its page is this run's not-built page"
    except OSError:
        return False, "its page is unreadable"
    doc, why = read_track_status(Path(out_dir).parent / "checkpoints", rid)
    if doc is None:
        return False, why
    if doc.get("state") != "built":
        return False, f"its status is {doc.get('state')}"
    return True, None


def _label_now():
    try:
        return label_from_env(from_env())
    except Exception:  # noqa: BLE001
        return label_from_env(None)


def not_built_page(out_dir, track, rid, why=None, label=None, lines=None):
    """BUILD IC-7 (D7, I5): this run's page for a track that was not built. Every file of <out_dir> moves first to
    <out_dir>/../output_prev/<the run its page prints, else "unknown">/ (kept beside the output, never deployed with it; a name taken
    gets -2, -3 …); then a static index.html — no template: naipc-run this run, naipc-build BUILD_ID, naipc-page not-built, the competition
    label, one plain sentence (`why`, plain words — never an error's class) and, for the hub, `lines` (each track's line). A track whose
    page is this run's "built" is left alone; this run's not-built page is written again in place. Returns the page's path, or None
    (said); never raises."""
    try:
        out = Path(out_dir)
        ok, _w = page_is_this_runs(out, track, rid)
        if ok:
            return None
        page = out / "index.html"
        try:        # BUILD IC-8 (P4.3a; F24): this run's published page that a version holds — a re-run that did not end built — is left
            #          for pipeline.py's restore (restore_version), never moved or replaced
            vf_, vm_ = version_of(out.parent, rid)
            if vm_ and page.is_file() and vm_["files"].get("output/index.html") == _sha256_file(page):
                say(f"    {track}: this run's published page (invocation {vm_['invocation_number']}) stays — kept in {VERSIONS_DIR}/{vf_.name}/, "
                    "pipeline.py puts its files back")
                return None
        except Exception:  # noqa: BLE001
            pass
        again = False
        try:
            again = page.is_file() and page_run_of(page) == rid and NOT_BUILT_META in page.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            again = False
        if out.is_dir() and any(out.iterdir()) and not again:
            prev = re.sub(r"[^A-Za-z0-9_-]+", "-", page_run_of(page) or "unknown")
            dest, n = out.parent / "output_prev" / prev, 1
            while dest.exists():
                n += 1
                dest = out.parent / "output_prev" / f"{prev}-{n}"
            dest.mkdir(parents=True)
            for q in list(out.iterdir()):
                os.replace(q, dest / q.name)
            own_ = prev == re.sub(r"[^A-Za-z0-9_-]+", "-", str(rid))        # BUILD IC-8 (P4.3a): this run's own page, said so
            say(f"    {track}: the files of run {prev} moved to output_prev/{dest.name}/ ("
                + ("this run's, of an invocation that did not end built" if own_ else "not this run's") + ": never deploy them)")
        out.mkdir(parents=True, exist_ok=True)
        name = TRACK_NAMES.get(track, track)
        lab = label or _label_now()
        sent = f"{name} — not built this run" + (f": {why}." if why else ".")
        body = "".join(f"<li>{html.escape(str(x))}</li>" for x in (lines or []))
        doc = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
               "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
               f"<meta name=\"naipc-run\" content=\"{html.escape(str(rid), quote=True)}\">{BUILD_META}{NOT_BUILT_META}"
               f"<title>{html.escape(lab)} — {html.escape(name)}</title>"
               "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.5;"
               "color:#111;background:#fff}</style>"
               f"</head><body><h1>{html.escape(lab)}</h1><p>{html.escape(sent)}</p>"
               + (f"<ul>{body}</ul>" if body else "") + "</body></html>\n")
        tmp = out / "index.html.tmp"
        tmp.write_text(doc, encoding="utf-8")
        os.replace(tmp, page)
        say(f"    {track}: this run's not-built page written (output/index.html) — {sent}")
        return page
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ {track}: the not-built page was not written ({type(e).__name__})")
        return None


# ── D10: the run's record ──
RUN_FILE = "run_{rid}.json"


def write_run_record(runs_dir, rec):
    """BUILD IC-7 (D10): <runs>/<challenge>/run_<run id>.json — the run id, its block, the block's end (epoch seconds), the challenge, the
    contract's path and the pipeline's arguments (the curveball text included; never the key), with the re-runs' lines; written whole
    (a temporary file, then os.replace). Returns its path or None (said)."""
    try:
        d = Path(runs_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / RUN_FILE.format(rid=re.sub(r"[^A-Za-z0-9_-]+", "-", str(rec["run_id"])))
        tmp = d / (p.name + ".tmp")
        tmp.write_text(json.dumps(dict(rec, schema="naipc.run.v1", resumes=list(rec.get("resumes") or [])), ensure_ascii=False, indent=1)
                       + "\n", encoding="utf-8")
        os.replace(tmp, p)
        return p
    except Exception as e:  # noqa: BLE001
        say(f"  ⚠ the run's record was not written ({type(e).__name__}) — a re-run of this run will not be possible")
        return None


def read_run_record(runs_dir, rid):
    """BUILD IC-7 (D10): (the record, None) of run `rid` under <runs>/<challenge>/, else (None, why in plain words)."""
    p = Path(runs_dir) / RUN_FILE.format(rid=re.sub(r"[^A-Za-z0-9_-]+", "-", str(rid)))
    if not p.is_file():
        return None, f"no record of run {rid} ({p})"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"the record of run {rid} is unreadable ({type(e).__name__})"
    if not isinstance(doc, dict) or doc.get("run_id") != rid or doc.get("schema") != "naipc.run.v1":
        return None, f"the record of run {rid} is not a run record"
    return doc, None


def add_run_line(runs_dir, rid, line):
    """BUILD IC-7 (D10): one line added to the run's record (a re-run: when, from which track). Never raises."""
    doc, _w = read_run_record(runs_dir, rid)
    if doc is not None:
        doc["resumes"] = list(doc.get("resumes") or []) + [dict(line, at=_now_iso())]
        write_run_record(runs_dir, doc)


def close_run_line(runs_dir, rid, invocations):
    """BUILD IC-8 (P4.3a; F24, RS-3): the run record's last re-run line closed with the invocations it gave each track — [{"track",
    "invocation_id", "invocation_number", "outcome", "selected"}] (selected: the invocation number the track's current status names when it
    is built, else None): ids, counters and run_harness's fixed outcome words only. Never raises."""
    try:
        doc, _w = read_run_record(runs_dir, rid)
        if doc is not None and doc.get("resumes"):
            res = list(doc["resumes"])
            res[-1] = dict(res[-1], invocations=[{k: x.get(k) for k in ("track", "invocation_id", "invocation_number", "outcome", "selected")}
                                                 for x in invocations or []])
            doc["resumes"] = res
            write_run_record(runs_dir, doc)
    except Exception as e:  # noqa: BLE001
        say(f"  ⚠ the re-run's line was not closed in the run's record ({type(e).__name__})")


def selected_invocations(dirs, rid=None):
    """BUILD IC-8 (P4.3a; F24, RS-3): each track's selected invocation — the number its current status of this run names when it is built,
    else None — {track: n} over `dirs` ({track: its checkpoints folder, or None})."""
    out = {}
    for t, cp in dirs.items():
        doc, _w = read_track_status(cp, rid) if cp is not None else (None, None)
        n = (doc or {}).get("invocation_number")
        out[t] = n if (doc or {}).get("state") == "built" and isinstance(n, int) and not isinstance(n, bool) else None
    return out


def accept_mode() -> bool:
    """BUILD IC-5 (D2 h): NAIPC_ACCEPT=1 — the tests' runs and the acceptance script: a code name or a tag of the model's on a page (the
    pipeline's leak check, the hub's checkpoints/leaks.json), or a leak check that did not run, fails the run (exit 1, after every page
    was built). Unset in a live run: the findings are printed and written, and the run goes on. Read at each call."""
    return os.environ.get("NAIPC_ACCEPT", "").strip() == "1"


def ic5_prompt() -> bool:
    """BUILD IC-5 (Q-SWITCH): this build's requests — D13–D16, and D1's rewrite of the model's text that a request sends back (Block 1's
    analysis in the update) — unless NAIPC_IC5_PROMPT is 0, off, false or no (any case), or a switch it builds on is (NAIPC_IC4_PROMPT,
    NAIPC_IC3_PROMPT, NAIPC_IC2_PROMPT): then the older build's request bodies, byte for byte. Read once, when the module loads."""
    return _IC5_REQUESTS


def ic4_prompt() -> bool:
    """BUILD IC-4 (Q-SWITCH): this build's requests — the Oracle's update as keyed edits (D11), the navigator's explanation as a list under a
    structured output (D12), … — unless NAIPC_IC4_PROMPT is 0, off, false or no (any case), or IC-3's switch NAIPC_IC3_PROMPT is (IC-4's
    wording builds on IC-3's): then IC-3's request bodies, byte for byte. Read once, when the module loads; every harness reads this one."""
    return _IC4_REQUESTS


# BUILD IC-4 (D12): the brief repair's reply form (brief_repair_prompt's last line) as a structured output's schema — the Oracle and the
# Muse send it with the switch on; closed and fully required at every level, strings only, no enum or key from the data, no constraint
BRIEF_REPAIR_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["brief_answers"],
                       "properties": {"brief_answers": {"type": "array", "items": {
                           "type": "object", "additionalProperties": False, "required": ["id", "answer"],
                           "properties": {"id": {"type": "string"}, "answer": {"type": "string"}}}}}}


def output_config(effort, schema=None):
    """BUILD IC-4 (D12): a request's output_config — the effort, and with a schema the structured output ({"type": "json_schema",
    "schema": …}) when the switch is on; without one, or with the switch off, the effort alone (IC-3's)."""
    return dict({"effort": effort}, **({"format": {"type": "json_schema", "schema": schema}} if schema and ic4_prompt() else {}))


def _thinking_tokens(usage):
    """The API's own count of thinking tokens ("usage.output_tokens_details.thinking_tokens", sent on the final message_delta [VERIFIED 11/11:
    Anthropic's release notes, 27 May 2026]), or the words "not reported" — never 0 for a missing count, never an estimate."""
    d = (usage or {}).get("output_tokens_details") if isinstance(usage, dict) else None
    v = d.get("thinking_tokens") if isinstance(d, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else "not reported"


JOURNAL_FLUSH_S = 2.0          # BUILD IC-6 (D6): the journal is flushed at least this often, and at the end


def next_attempt(cp_dir, label, rid=None):
    """BUILD IC-7 (D10): the next attempt number of a call — one more than the highest this run's journals (journal_<label>_<run id>_<n>.txt)
    and call-log lines hold for the label, so a re-run of a track in the same run never overwrites the failed attempt's files; 1 when
    none. Never raises."""
    rid = rid or run_id()
    hi = 0
    try:
        pat = re.compile(r"journal_" + re.escape(re.sub(r"[^A-Za-z0-9_+-]+", "-", str(label))) + "_"
                         + re.escape(re.sub(r"[^A-Za-z0-9_-]+", "-", str(rid))) + r"_(\d+)\.txt$")
        for p in Path(cp_dir).glob("journal_*.txt"):
            m = pat.match(p.name)
            if m:
                hi = max(hi, int(m.group(1)))
        for r in _call_log_lines(cp_dir):
            if r.get("label") == label and r.get("run_id") == rid and isinstance(r.get("attempt"), int):
                hi = max(hi, r["attempt"])
    except Exception:  # noqa: BLE001
        pass
    return hi + 1


def journal_path(cp_dir, label, attempt):
    """BUILD IC-6 (D6, D11 a): <checkpoints>/journal_<label>_<run id>_<attempt>.txt — one call's text as it arrived."""
    return Path(cp_dir) / (f"journal_{re.sub(r'[^A-Za-z0-9_+-]+', '-', str(label))}_{re.sub(r'[^A-Za-z0-9_-]+', '-', str(run_id()))}"
                           f"_{attempt}.txt")


# ═══ BUILD IC-8 (D8, I6): call and invocation identity — random ids and counters only ═══
INVOCATIONS_FILE = "invocations_{rid}.jsonl"
RECORD_SCHEMA = 2                      # the identity fields' schema: every record line this build writes carries it
_CALLS = {"seq": {}, "last": {}}       # this process: the call sequence per invocation; the last call id by label and by profile


def _safe_rid(rid):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(rid))


def _append_jsonl(path, rec):
    """One JSON line appended (the folder made); None, or why it was not written (said). Never raises."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return None
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ {Path(path).name} not written — {type(e).__name__}")
        return type(e).__name__


def invocations_path(cp_dir, rid=None):
    return Path(cp_dir) / INVOCATIONS_FILE.format(rid=_safe_rid(rid or run_id()))


def invocation_lines(cp_dir, rid=None):
    """BUILD IC-8 (D8): the parsed lines of <cp_dir>/invocations_<run id>.jsonl (a line that is not a JSON object skipped)."""
    try:
        raw = invocations_path(cp_dir, rid).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out = []
    for ln in raw.splitlines():
        try:
            x = json.loads(ln)
        except ValueError:
            continue
        if isinstance(x, dict):
            out.append(x)
    return out


def allocate_invocation(cp_dir, track, rid=None, how="pipeline"):
    """BUILD IC-8 (D8): a new invocation of `track` in this run — invocation_id (uuid4 hex) and invocation_number (one more than the
    highest in <cp_dir>/invocations_<run id>.jsonl) — with its "started" line appended (the ids, the track, how it was started, when).
    → {"id", "number"}. Never raises: a record that cannot be written is said, and the ids stand."""
    rid = rid or run_id()
    n = 1 + max([x["invocation_number"] for x in invocation_lines(cp_dir, rid) if isinstance(x.get("invocation_number"), int)
                 and not isinstance(x.get("invocation_number"), bool)] or [0])
    inv = {"id": uuid.uuid4().hex, "number": n}
    _append_jsonl(invocations_path(cp_dir, rid), {"event": "started", "run_id": rid, "track": track, "invocation_id": inv["id"],
                                                   "invocation_number": n, "how": how, "at": _now_iso(), "schema_version": RECORD_SCHEMA})
    return inv


def finish_invocation(cp_dir, track, inv, outcome, seconds, rid=None):
    """BUILD IC-8 (D8): the invocation's "finished" line — its outcome and the child's seconds by time.perf_counter (3 places)."""
    return _append_jsonl(invocations_path(cp_dir, rid), {"event": "finished", "run_id": rid or run_id(), "track": track,
                                                          "invocation_id": inv["id"], "invocation_number": inv["number"],
                                                          "outcome": outcome, "seconds": None if seconds is None else round(float(seconds), 3),
                                                          "at": _now_iso(), "schema_version": RECORD_SCHEMA})


def keep_invocation_status(cp_dir, inv):
    """BUILD IC-8 (D8): <cp_dir>/track_status_<run id>_inv<n>.json — the invocation's status as it ended, written once and never changed
    (only when the current status is this invocation's). Its run's id in the name (d8b): a Block 2 run's invocation 1 shares the folder
    with Block 1's. None, or why it was not kept."""
    p = Path(cp_dir) / TRACK_STATUS
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"no status of this invocation ({type(e).__name__})"
    if not isinstance(doc, dict) or doc.get("invocation_id") != inv["id"]:
        return "the status is not this invocation's"
    dst = Path(cp_dir) / f"track_status_{_safe_rid(doc.get('run_id') or run_id())}_inv{inv['number']}.json"
    if dst.exists():
        return "kept already"
    try:
        with open(dst, "x", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return None
    except OSError as e:
        return type(e).__name__


# ═══ BUILD IC-8 (P4.3a; F24, I6): a re-run's versions — the published bundle kept at a re-run's start, put back when it does not end built ═══
VERSIONS_DIR = "versions"
_VERSION_SKIP = re.compile(r"^(?:journal_.*\.txt|partial_.*\.txt|track_status_.+_inv\d+\.json|.*\.tmp|.*\.lock)$")


def _sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def version_files(track_dir):
    """BUILD IC-8 (P4.3a): a track folder's published bundle — the files an invocation writes in place, relative to the folder (posix form):
    every file of output/ and coverage/, and every file of checkpoints/ that is not a record (RECORD_FILES), a call's journal or partial
    text, an invocation's kept status or a temporary file."""
    d = Path(track_dir)
    out = []
    for sub in ("output", "checkpoints", "coverage"):
        base = d / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not (sub == "checkpoints" and (record_kind(f)[0] or _VERSION_SKIP.match(f.name))):
                out.append(f.relative_to(d).as_posix())
    return out


def version_of(track_dir, rid=None):
    """BUILD IC-8 (P4.3a): the newest version of run `rid` in <track>/versions/ — (its folder, its manifest), the highest invocation number
    whose manifest reads; (None, None) when the run has none."""
    rid = rid or run_id()
    best = (None, None)
    base = Path(track_dir) / VERSIONS_DIR
    for m in (sorted(base.glob(f"{_safe_rid(rid)}_inv*/manifest.json")) if base.is_dir() else []):
        try:
            doc = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n = doc.get("invocation_number") if isinstance(doc, dict) else None
        if (isinstance(n, int) and not isinstance(n, bool) and doc.get("run_id") == rid and isinstance(doc.get("files"), dict)
                and (best[1] is None or n > best[1]["invocation_number"])):
            best = (m.parent, doc)
    return best


def keep_version(track_dir, track, rid=None):
    """BUILD IC-8 (P4.3a; F24): at a re-run's start — pipeline.py run_harness, a track whose page is this run's built page — the published
    bundle (version_files) copied to <track>/versions/<run id>_inv<n>/ (n: the invocation the current status names) with manifest.json:
    the track, the run, that invocation's id and number, each file's sha256 (ids, paths and hashes only). Kept once per invocation (a later
    re-run of the same published invocation finds it); outside output/, never deployed. → the version's folder, or None (said). Never
    raises."""
    try:
        rid = rid or run_id()
        d = Path(track_dir)
        st, _w = read_track_status(d / "checkpoints", rid)
        n = (st or {}).get("invocation_number")
        if st is None or st.get("state") != "built" or not isinstance(n, int) or isinstance(n, bool):
            return None
        dest = d / VERSIONS_DIR / f"{_safe_rid(rid)}_inv{n}"
        if (dest / "manifest.json").is_file():
            return dest
        import shutil as _sh
        tmp = d / VERSIONS_DIR / f".{dest.name}.{os.getpid()}.tmp"
        _sh.rmtree(tmp, ignore_errors=True)
        files = {}
        for rel in version_files(d):
            q = tmp / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(d / rel, q)
            files[rel] = _sha256_file(q)
        (tmp / "manifest.json").write_text(json.dumps({"schema": "naipc.version.v1", "track": track, "run_id": rid,
                                                        "invocation_id": st.get("invocation_id"), "invocation_number": n, "kept_at": _now_iso(),
                                                        "files": files, "schema_version": RECORD_SCHEMA}, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")
        _sh.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        mark_path("rerun.version_kept")
        return dest
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ {track}: its published files were not kept before the re-run ({type(e).__name__}) — a re-run that does not end built "
            "cannot put them back")
        return None


def restore_version(track_dir, track, inv, rid=None):
    """BUILD IC-8 (P4.3a; F24): after a re-run that did not end built (pipeline.py run_harness: the track's page is not this run's built page
    once the child ended) — this run's newest version (version_of) put back file by file: a bundle file the re-run changed or made is moved
    first to <track>/output_prev/<run id>-inv<the re-run's number>/<its path> (the re-run's, kept, never deployed), then the version's copy
    goes in its place; the current status, part of the bundle, is again the version's invocation's. → the console's line, or None when the
    run has no version (the re-run's own outcome stands: its not-built page). Never raises."""
    try:
        rid = rid or run_id()
        folder, man = version_of(track_dir, rid)
        if folder is None:
            return None
        import shutil as _sh
        d = Path(track_dir)
        aside = d / "output_prev" / f"{_safe_rid(rid)}-inv{(inv or {}).get('number')}"
        moved = put = bad = 0
        for rel in sorted(set(version_files(d)) | set(man["files"])):
            cur, want = d / rel, man["files"].get(rel)
            if cur.is_file() and want is not None and _sha256_file(cur) == want:
                continue
            if cur.is_file():
                q = aside / rel
                q.parent.mkdir(parents=True, exist_ok=True)
                os.replace(cur, q)
                moved += 1
            if want is not None:
                src_ = folder / rel
                if not src_.is_file() or _sha256_file(src_) != want:
                    bad += 1
                    continue
                cur.parent.mkdir(parents=True, exist_ok=True)
                _sh.copy2(src_, cur)
                put += 1
        mark_path("rerun.restored")
        return (f"⚠ {TRACK_NAMES.get(track, track)}: invocation {(inv or {}).get('number')} of run {rid} did not end built — invocation "
                f"{man['invocation_number']}'s page and files stay published ({put} file(s) put back from {VERSIONS_DIR}/{folder.name}/"
                + (f"; the re-run's {moved} file(s) kept in output_prev/{aside.name}/, never deploy them" if moved else "")
                + (f"; ⚠ {bad} file(s) of the version differ from its manifest and were not put back" if bad else "") + ")")
    except Exception as e:  # noqa: BLE001
        return f"⚠ {track}: its published files were not put back ({type(e).__name__}) — its page may be the re-run's"


def invocation(cp_dir=None, track=None):
    """BUILD IC-8 (D8): this process's invocation {"id", "number"} — NAIPC_INVOCATION_ID and NAIPC_INVOCATION_NUMBER as pipeline.py
    passed them; a harness run alone allocates its own at its first need (allocate_invocation, how "alone": `track`, else its
    checkpoints folder's track) and sets both for the rest of the process; {} when there is neither."""
    i_, n_ = os.environ.get("NAIPC_INVOCATION_ID", "").strip(), os.environ.get("NAIPC_INVOCATION_NUMBER", "").strip()
    if i_ and n_.isdigit():
        return {"id": i_, "number": int(n_)}
    if cp_dir is None:
        return {}
    cp_dir = Path(cp_dir).resolve()     # BUILD IC-8 (P4.4, p44h): the folder as it is now — the exit hook below writes into it, whatever
    tr = track or re.sub(r"-harness$", "", cp_dir.parent.name.lower())      # the working folder is at exit (it resolved 'checkpoints' then)
    inv = allocate_invocation(cp_dir, tr, how="alone")
    os.environ["NAIPC_INVOCATION_ID"], os.environ["NAIPC_INVOCATION_NUMBER"] = inv["id"], str(inv["number"])
    import atexit, time as _t      # BUILD IC-8 (D8, d8b): alone, the harness finishes its own invocation as run_harness does for a child —
    t0_, rid_ = _t.perf_counter(), run_id()     # its "finished" line with its seconds (perf counter) and its kept status, at its exit

    def _finish():
        import contextlib as _cl     # BUILD IC-8 (P4.4, p44h): what the hook says (a record it cannot write) goes to stderr — after the
        with _cl.redirect_stdout(sys.stderr):    # program's own output, never as stdout's last line (a caller reads that)
            finish_invocation(cp_dir, tr, inv, "ended (run alone)", _t.perf_counter() - t0_, rid=rid_)
            keep_invocation_status(cp_dir, inv)
    atexit.register(_finish)
    return inv


def identity(cp_dir=None, track=None):
    """BUILD IC-8 (D8): the identity fields a record line of this invocation carries — run_id, invocation_id, invocation_number,
    schema_version: random ids and counters only."""
    inv = invocation(cp_dir, track)
    return {"run_id": run_id(), "invocation_id": inv.get("id"), "invocation_number": inv.get("number"), "schema_version": RECORD_SCHEMA}


def new_call(cp_dir, label, profile=None, generation_attempt=1):
    """BUILD IC-8 (D8): one model call's identity, before its request — call_id (uuid4 hex); call_sequence (one more than the last of
    this invocation: this process's counter, started from the call log's lines of the invocation); generation_attempt (1, or 2 for
    IC-7's second attempt); retry_of_call_id (on the second: the last call id of the same label in this process); its journal,
    journal_<label>_<run id>_i<invocation number>_c<call sequence>.txt. → the identity, its "journal" a path."""
    inv = invocation(cp_dir)
    key = inv.get("id") or "-"
    if key not in _CALLS["seq"]:
        _CALLS["seq"][key] = max([r["call_sequence"] for r in _call_log_lines(cp_dir) if r.get("invocation_id") == inv.get("id")
                                  and isinstance(r.get("call_sequence"), int)] or [0])
    _CALLS["seq"][key] += 1
    seq = _CALLS["seq"][key]
    ga = 2 if generation_attempt == 2 else 1
    ident = {"call_id": uuid.uuid4().hex, "call_sequence": seq, "generation_attempt": ga,
             "retry_of_call_id": _CALLS["last"].get(("label", label)) if ga == 2 else None,
             "invocation_id": inv.get("id"), "invocation_number": inv.get("number"), "schema_version": RECORD_SCHEMA,
             "journal": Path(cp_dir) / (f"journal_{re.sub(r'[^A-Za-z0-9_+-]+', '-', str(label))}_{_safe_rid(run_id())}"
                                        f"_i{inv.get('number') or 0}_c{seq}.txt")}
    _CALLS["last"][("label", label)] = ident["call_id"]
    if profile:
        _CALLS["last"][("profile", profile)] = ident["call_id"]
    return ident


def last_call_id(profile):
    """BUILD IC-8 (D8): this process's last call id of a profile (the admission record's call_id), or None."""
    return _CALLS["last"].get(("profile", profile))


CALL_IDENTITY = ("call_id", "call_sequence", "generation_attempt", "retry_of_call_id", "invocation_id", "invocation_number", "schema_version")
MOCK_IDENTITY_HEADER = "X-Naipc-Call"       # BUILD IC-8 (D14, F30): the strict mock's key of an exchange — test-only


def mock_identity_headers(label, profile=None, ident=None):
    """BUILD IC-8 (D14, F30): the call's identity for the strict mock — {"X-Naipc-Call": "<label>;i<invocation>;c<sequence>;g<generation>"}
    only when NAIPC_MOCK_IDENTITY=1 (the rehearsal and the tests set it; live never does) and the call has an identity (NR.new_call);
    else {} and no header is sent. <label>: the call's profile key ("oracle/master"), with the label's own suffix after the profile's
    label when it has one (a continuation's "+c": "muse/call1+c"), or "#<label>" when the label is another; a call with no profile, its
    label. Ids and counters only — never a value, a text or a key."""
    if os.environ.get("NAIPC_MOCK_IDENTITY", "").strip() != "1" or not isinstance(ident, dict):
        return {}
    lab = str(label)
    if isinstance(profile, str) and profile in CALL_PROFILES:
        base = str(CALL_PROFILES[profile].get("label") or "")
        lab = profile + (lab[len(base):] if base and lab.startswith(base) else "#" + lab)

    def _int(v, d):
        return v if isinstance(v, int) and not isinstance(v, bool) else d
    return {MOCK_IDENTITY_HEADER: f"{lab};i{_int(ident.get('invocation_number'), 0)};c{_int(ident.get('call_sequence'), 0)}"
                                  f";g{_int(ident.get('generation_attempt'), 1)}"}


# ═══ BUILD IC-8 (D14, I8): path markers — each parser path and finalizer branch a run takes, by id, in checkpoints/paths_<run id>.jsonl ═══
PATHS_RX = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
PATHS_KEYS = ("path", "run_id", "invocation_id", "invocation_number", "schema_version")
_PATHS = {"home": None, "track": None, "seen": set(), "pending": []}


def path_id(*parts):
    """A path id from code-made parts: each lower-cased, every run of other characters one "_", "/" and "." separators — ("oracle/master",
    "stop", "end_turn") → "oracle.master.stop.end_turn"; path_id(path_id(x)) == path_id(x) (d14c2). Ids only: a segment is cut at 40
    characters."""
    out = []
    for p in parts:
        for q in re.split(r"[/.]", str(p)):
            q = re.sub(r"[^a-z0-9_]+", "_", q.strip().lower()).strip("_")[:40]
            if q:
                out.append(q)
    return ".".join(out)


def paths_path(cp_dir, rid=None):
    return Path(cp_dir) / f"paths_{_safe_rid(rid or run_id())}.jsonl"


def _write_path(pid_, cp_):
    inv_n = os.environ.get("NAIPC_INVOCATION_NUMBER", "").strip()
    _append_jsonl(paths_path(cp_), {"path": pid_, "run_id": run_id(), "invocation_id": os.environ.get("NAIPC_INVOCATION_ID", "").strip() or None,
                                    "invocation_number": int(inv_n) if inv_n.isdigit() else None, "schema_version": RECORD_SCHEMA})


def paths_home(cp_dir, track):
    """BUILD IC-8 (D14): this process's checkpoints folder and track for mark_path — set by each harness's and the pipeline's entry point
    (a test that calls a harness's functions sets none: its marks stay in memory, _PATHS["pending"]); the marks made before it are written
    now."""
    _PATHS["home"], _PATHS["track"] = Path(cp_dir), str(track)
    pend, _PATHS["pending"] = _PATHS["pending"], []
    for pid_ in pend:
        try:
            _write_path(pid_, _PATHS["home"])
        except Exception:  # noqa: BLE001
            pass


def mark_path(path, cp_dir=None, track=None):
    """BUILD IC-8 (D14, I8): the run reached `path` — its id "<track>.<path>" (track: given; None, the process's; False, none: the path
    holds it) in path_id's form, written once per process to <checkpoints>/paths_<run id>.jsonl (PATHS_KEYS: the id, the run's id, the
    invocation's id and number, the schema version — ids only, F26). No folder known yet: kept until paths_home. Returns the id; never
    raises."""
    try:
        tr = _PATHS["track"] if track is None else track
        pid_ = path_id(tr, path) if tr else path_id(path)
        if not PATHS_RX.match(pid_) or pid_ in _PATHS["seen"]:
            return pid_
        _PATHS["seen"].add(pid_)
        cp_ = cp_dir or _PATHS["home"]
        if cp_ is None:
            _PATHS["pending"].append(pid_)
        else:
            _write_path(pid_, cp_)
        return pid_
    except Exception:  # noqa: BLE001
        return None


def read_paths(dirs, rid=None):
    """BUILD IC-8 (D14): the path ids in the folders' paths files (of run `rid`, else every run's) — a set; a line that does not parse or
    holds no id in path_id's form is not read."""
    out = set()
    for d in dirs:
        for f in sorted(Path(d).glob(f"paths_{_safe_rid(rid)}.jsonl" if rid else "paths_*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    x = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(x, dict) and isinstance(x.get("path"), str) and PATHS_RX.match(x["path"]):
                    out.add(x["path"])
    return out


# ═══ BUILD IC-8 (D9, I6): one record reader — every record file read one way, every total named by its scope ═══
class DuplicateRecordKeyError(ValueError):
    """BUILD IC-8 (D9): a record file holds one declared-unique key twice — read_records raises it when strict (strict=True, else
    NAIPC_STRICT=1); live, the first line stands and the duplicate is recorded (Records.duplicates: the acceptance's row B1 fails)."""


RECORD_FILES = (("call_log", re.compile(r"^call_log\.jsonl$")), ("capacity", re.compile(r"^capacity_(.+)\.jsonl$")),
                ("admission", re.compile(r"^admission_(.+)\.jsonl$")), ("admit_findings", re.compile(r"^admit_findings\.jsonl$")),
                ("dispositions", re.compile(r"^dispositions_(.+)\.jsonl$")), ("invocations", re.compile(r"^invocations_(.+)\.jsonl$")),
                ("paths", re.compile(r"^paths_(.+)\.jsonl$")))
PER_CALL_RECORDS = ("call_log", "capacity", "admission")     # one line per model call: its call_id is the declared-unique key
RECORD_SCOPES = ("run", "active invocation", "invocation:<id>")
LEGACY_PROOF_S = 60.0     # a legacy admission written at most this long after its call's end is that call's, when no other call ends then
WINDOW_SLACK_S = 1.5      # a console's clock is rounded to the second and a record's time truncated to it
TRACK_TITLES = {"oracle": "Oracle (Business Case)", "architect": "Architect (Resource Navigator)", "muse": "Muse (Outreach Campaign)",
                "landing": "Landing (Full Stack Hub)"}      # pipeline.py's run_harness names, as its console prints them


def record_kind(path):
    """BUILD IC-8 (D9): (kind, the run id the file's name holds or None) of a record file — RECORD_FILES; (None, None) for another."""
    n = Path(path).name
    for k, rx in RECORD_FILES:
        m = rx.match(n)
        if m:
            return k, (m.group(1) if rx.groups else None)
    return None, None


def _record_lines(path):
    """BUILD IC-8 (D9): a record file's lines as read — {"parsed": [(line number, object)], "refused": [(line number, why)],
    "quarantined": (line number, why) or None, "read": the lines read, "error": why the file was not read, or None}. The piece after the
    last newline is a write cut short: quarantined and named, never parsed. read = parsed + refused, asserted."""
    out = {"parsed": [], "refused": [], "quarantined": None, "read": 0, "error": None}
    try:
        raw = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError as e:
        out["error"] = type(e).__name__
        return out
    pieces = raw.split("\n")
    tail = pieces.pop()
    for i, ln in enumerate(pieces, 1):
        if not ln.strip():
            out["refused"].append((i, "a blank line"))
            continue
        try:
            x = json.loads(ln)
        except ValueError:
            out["refused"].append((i, "not JSON"))
            continue
        if isinstance(x, dict):
            out["parsed"].append((i, x))
        else:
            out["refused"].append((i, f"a JSON {type(x).__name__}, not an object"))
    out["read"] = len(pieces)
    if tail:
        out["read"] += 1
        out["quarantined"] = (out["read"], "a partial last line, with no newline after it: a write cut short — quarantined")
        out["refused"].append(out["quarantined"])
    if out["read"] != len(out["parsed"]) + len(out["refused"]):
        raise AssertionError(f"{Path(path).name}: {out['read']} lines read, not {len(out['parsed'])} parsed + {len(out['refused'])} refused")
    return out


def _epoch(iso):
    """'2026-11-18T04:58:17Z' → seconds since the epoch; None for another form."""
    try:
        return datetime.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def active_invocation(folder, rid=None):
    """BUILD IC-8 (D9): the active invocation of a track's checkpoints folder in a run — {"id", "number"}: this process's own
    (NAIPC_INVOCATION_ID) when the folder's invocations record holds it, else the one the folder's current status names (the selected
    invocation: BUILD IC-8 P4.3a — a re-run that did not end built leaves it on the invocation whose page is published), else the
    record's highest started; {} when the record holds none (a legacy folder: which invocation is active cannot be told)."""
    started = [x for x in invocation_lines(folder, rid) if x.get("event") == "started" and isinstance(x.get("invocation_id"), str)]
    env_ = os.environ.get("NAIPC_INVOCATION_ID", "").strip()
    mine = [x for x in started if env_ and x["invocation_id"] == env_]
    if not mine and started:            # BUILD IC-8 (P4.3a; F24): the selected invocation — the one the folder's current status names
        st_, _w = read_track_status(folder, rid)
        sel_ = (st_ or {}).get("invocation_id")
        mine = [x for x in started if sel_ and x["invocation_id"] == sel_]
    if not mine and started:
        mine = [max(started, key=lambda x: x["invocation_number"] if isinstance(x.get("invocation_number"), int) else 0)]
    return {"id": mine[0]["invocation_id"], "number": mine[0].get("invocation_number")} if mine else {}


def legacy_windows(console, track=None):
    """BUILD IC-8 (D9, D10): a console's track runs as windows of time, for the records that name no invocation (IC-7's). Each pipeline
    run in the text ("Block clock: … the block ends at … (<epoch>)") gives each track it ran a start — the block's end less the track's
    "Block <b> clock: <n> s left" — and an end: the start plus its "completed in <s>s" (or a FAILED or TIMED OUT line's seconds).
    → [{"id": "legacy:console:<run k>:<track>", "number": the track's k-th run in the text, "track", "start", "end", "seconds",
    "outcome", "line", "run_id" (the run's "Run id:"), "block" (its "Mode:" line: 1, 2, or None)}], `track`'s only when named. A run
    with no block line, or a track with no clock or end line, gives no window."""
    titles = {v: k for k, v in TRACK_TITLES.items()}
    sec, mode = {}, None             # BUILD IC-8 (D10): the console run's id and block — "Mode:" comes before its clock line, "Run id:" after
    end_rx = re.compile(r"(✅|❌) (" + "|".join(re.escape(v) for v in TRACK_TITLES.values())
                        + r") (?:completed in|FAILED \(exit code -?\d+\) after|TIMED OUT after) ([\d.]+)s")
    out, block_end, run_k, cur, seen = [], None, 0, None, {}
    for i, ln in enumerate(str(console or "").splitlines(), 1):
        m = re.search(r"Mode: (?:CLEAN \(Block 1\)|CURVEBALL \(Block 2\))", ln)
        if m:
            mode = 2 if "Block 2" in m.group(0) else 1
        m = re.search(r"Block clock: [\d,]+ s — the block ends at \S+ \((\d+(?:\.\d+)?)\)", ln)
        if m:
            block_end, run_k, cur = float(m.group(1)), run_k + 1, None
            sec[run_k], mode = {"block": mode, "run_id": None}, None
            continue
        m = re.search(r"Run id: (\d{8}T\d{6}Z)", ln)
        if m and run_k in sec:
            sec[run_k]["run_id"] = m.group(1)
        if ln.strip() in titles:
            cur = {"track": titles[ln.strip()], "start": None}
            continue
        m = re.search(r"Block (?:\d+|\?) clock: (-?\d+) s left", ln)
        if m and cur is not None and block_end is not None:
            cur["start"] = block_end - float(m.group(1))
            continue
        m = end_rx.search(ln)
        if m and cur is not None and cur["start"] is not None and titles[m.group(2)] == cur["track"]:
            t_, secs = cur["track"], float(m.group(3))
            seen[t_] = seen.get(t_, 0) + 1
            out.append({"id": f"legacy:console:{run_k}:{t_}", "number": seen[t_], "track": t_, "start": round(cur["start"], 3),
                        "end": round(cur["start"] + secs, 3), "seconds": secs, "line": i, "run": run_k,
                        "outcome": "completed" if m.group(1) == "✅" else ("timed out" if "TIMED OUT" in ln else "failed")})
            cur = None
    for w in out:
        w.update(sec.get(w["run"]) or {"block": None, "run_id": None})
    return [w for w in out if track is None or w["track"] == track]


def record_run_ids(path):
    """BUILD IC-8 (D10): the run ids a record file's lines hold (sorted) — a log that holds several runs is read run by run."""
    return sorted({x["run_id"] for _i, x in _record_lines(path)["parsed"] if isinstance(x.get("run_id"), str) and x["run_id"]})


def _legacy_call(kind, x, calls):
    """BUILD IC-8 (D9): a legacy capacity or admission line's call when the call log beside it proves it — the one call-log line of its
    run and profile that starts when the capacity line says, or whose end falls within LEGACY_PROOF_S before the admission: its call id;
    else None (ambiguous)."""
    prof = x.get("profile") if kind == "capacity" else x.get("call")
    if not isinstance(prof, str) or not prof:
        return None
    mine = [c for c in calls if c.get("run_id") == x.get("run_id")
            and (c.get("profile") == prof if isinstance(c.get("profile"), str) else c.get("label") == prof.split("/")[-1])]
    if kind == "capacity":
        hits = [c for c in mine if c.get("at") == x.get("at")]
    else:
        t = _epoch(x.get("at"))
        hits = [c for c in mine if t is not None and _epoch(c.get("at")) is not None and _seconds(c.get("t_end"))
                and t - LEGACY_PROOF_S <= _epoch(c.get("at")) + float(c["t_end"]) <= t + WINDOW_SLACK_S]
    return hits[0]["call_id"] if len(hits) == 1 else None


def _in_window(x, kind, windows):
    """BUILD IC-8 (D9): the one window (legacy_windows) that holds a line's time — a call-log line by its start and end, another line by
    its time; None when none or more than one does."""
    t0 = _epoch(x.get("at"))
    if t0 is None:
        return None
    t1 = t0 + float(x["t_end"]) if kind == "call_log" and _seconds(x.get("t_end")) else t0
    hits = [w for w in windows if w["start"] - WINDOW_SLACK_S <= t0 and t1 <= w["end"] + WINDOW_SLACK_S]
    return hits[0] if len(hits) == 1 else None


def _upcast(kind, fname, i, x, calls, windows, head_inv):
    """BUILD IC-8 (D9): a record line as read_records gives it — a copy with "_file" and "_line"; for a call's record its call id (a
    legacy line's "legacy:<file>:<line>", or the call the call log proves) and "_call" (recorded, its own line, proved by the call log,
    ambiguous; None for a record that is not a call's); "_invocation" (recorded, by its block's header, by its time, ambiguous); and
    "correlation": recorded, proved or ambiguous."""
    y = dict(x, _file=fname, _line=i)
    if kind in PER_CALL_RECORDS:
        if isinstance(x.get("call_id"), str) and x["call_id"]:
            y["_call"] = "recorded"
        else:
            y["call_id"], y["_call"] = f"legacy:{fname}:{i}", ("its own line" if kind == "call_log" else "ambiguous")
            c = _legacy_call(kind, x, calls) if kind != "call_log" and calls else None
            if c is not None:
                y["call_id"], y["_call"] = c, "proved by the call log"
    else:
        y["_call"] = None
    if isinstance(x.get("invocation_id"), str) and x["invocation_id"]:
        y["_invocation"] = "recorded"
    elif head_inv:
        y["invocation_id"], y["_invocation"] = head_inv, "by its block's header"
    else:
        w = _in_window(x, kind, windows) if windows else None
        if w is not None:
            y["invocation_id"], y["invocation_number"], y["_invocation"] = w["id"], w.get("number"), "by its time"
        else:
            y["_invocation"] = "ambiguous"
    y["correlation"] = ("ambiguous" if "ambiguous" in (y["_call"], y["_invocation"])
                        else "recorded" if y["_call"] in (None, "recorded") and y["_invocation"] == "recorded" else "proved")
    return y


def _record_key(kind, y):
    """BUILD IC-8 (D9): a line's declared-unique key, or None — a call's id (PER_CALL_RECORDS), an invocation's event, a disposition's
    header, unit or summary within its invocation."""
    if kind in PER_CALL_RECORDS:
        return ("call_id", y.get("call_id"))
    if kind == "invocations" and y.get("invocation_id"):
        return ("invocation event", y.get("invocation_id"), y.get("event"))
    if kind == "dispositions" and y.get("line") in ("header", "unit", "summary") and y.get("invocation_id"):
        return ("disposition", y.get("invocation_id"), y.get("line"), y.get("unit"))
    return None


class Total:
    """BUILD IC-8 (D9): a count read from a record file with its scope's words — `n`, `words`, and `ambiguous`: the run's lines whose
    invocation is not recorded (in no invocation's count, never merged into one). str() names the scope."""
    __slots__ = ("n", "words", "ambiguous")

    def __init__(self, n, words, ambiguous=0):
        self.n, self.words, self.ambiguous = int(n), str(words), int(ambiguous)

    def __str__(self):
        return f"{self.n} {self.words}" + (f" ({self.ambiguous} more of the run name no invocation: ambiguous)" if self.ambiguous else "")

    __repr__ = __str__


class Records:
    """BUILD IC-8 (D9): read_records' result — `lines` (the scope's, each upcast: _upcast), `ambiguous` (the run's lines whose invocation
    is not recorded, for an invocation's scope), `run_lines`, the scope's `words`, its `invocation` ({} for the run, or none on record),
    the file's accounting (`read` = `parsed` + len(`refused`), `quarantined`, `error`) and the `duplicates` kept out."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def total(self, pred=None):
        """The scope's lines (those `pred` keeps) as a Total: its count with the scope's words, and the run's ambiguous lines apart."""
        return Total(sum(1 for x in self.lines if pred is None or pred(x)), self.words,
                     sum(1 for x in self.ambiguous if pred is None or pred(x)))

    def tally(self, key, pred=None):
        """{a value of `key`: n} over the scope's lines (those `pred` keeps)."""
        out = {}
        for x in self.lines:
            if pred is None or pred(x):
                out[x.get(key)] = out.get(x.get(key), 0) + 1
        return out

    def groups(self):
        """{an invocation's id, or "ambiguous": [its lines]} over the run's lines — never two invocations in one."""
        out = {}
        for x in self.run_lines:
            out.setdefault("ambiguous" if x.get("_invocation") == "ambiguous" else x.get("invocation_id"), []).append(x)
        return out


def _record_label(path):
    """BUILD IC-8 (D9): a record file's name as a legacy line's id gives it — with its track's folder when it sits in a harness's
    checkpoints folder ("Muse-harness/call_log.jsonl"), so a legacy id is unique across a run's folders."""
    p = Path(path)
    return f"{p.parent.parent.name}/{p.name}" if p.parent.name == "checkpoints" and p.parent.parent.name else p.name


def read_records(path, scope="run", *, rid=None, strict=None, windows=None):
    """BUILD IC-8 (D9, I6): the one reader of a record file (RECORD_FILES) → Records.
    - Every line read: read = parsed + refused; a partial last line quarantined and named (_record_lines).
    - A legacy line (no call_id: IC-7's) is upcast (_upcast): its call id "legacy:<file>:<line>", its call "ambiguous" unless the call
      log beside it proves it (_legacy_call); its invocation "ambiguous" unless `windows` (legacy_windows: a console's track runs) holds
      its time in exactly one. A disposition line without its own invocation takes its block's header's.
    - A declared-unique key twice (_record_key): strict (`strict`, else NAIPC_STRICT=1) raises DuplicateRecordKeyError; live the first
      stands and the duplicate is recorded (Records.duplicates).
    - `scope`: "run" — the lines of run `rid` (else the run the file's name holds, else this process's); "active invocation" — the
      folder's (active_invocation); "invocation:<id>". Every total names it (Records.total)."""
    p = Path(path)
    kind, name_rid = record_kind(p)
    rid = rid or name_rid or run_id()
    st = strict if strict is not None else os.environ.get("NAIPC_STRICT", "").strip() == "1"
    acc = _record_lines(p)
    calls = None
    if kind in ("capacity", "admission") and (p.parent / CALL_LOG).is_file():
        calls = [_upcast("call_log", _record_label(p.parent / CALL_LOG), i, x, None, None, None)
                 for i, x in _record_lines(p.parent / CALL_LOG)["parsed"]]
    ups, head_inv, seen, dups = [], None, {}, []
    for i, x in acc["parsed"]:
        if kind == "dispositions" and x.get("line") == "header":
            head_inv = x.get("invocation_id") if isinstance(x.get("invocation_id"), str) else None
        y = _upcast(kind, _record_label(p), i, x, calls, windows, head_inv)
        k = _record_key(kind, y)
        if k is not None and k in seen:
            if st:
                raise DuplicateRecordKeyError(f"{p.name}: line {i} repeats line {seen[k]}'s {k[0]} {list(k[1:])}")
            dups.append({"key": list(k), "line": i, "first": seen[k]})
            continue
        if k is not None:
            seen[k] = i
        ups.append(y)
    run_lines = [y for y in ups if (y.get("run_id") or name_rid) in (rid, None)]
    if scope == "run":
        inv, lines, amb, words = {}, run_lines, [], f"in run {rid}"
    else:
        if scope == "active invocation":
            inv = active_invocation(p.parent, rid)
        elif isinstance(scope, str) and scope.startswith("invocation:") and len(scope) > len("invocation:"):
            iid = scope[len("invocation:"):]
            inv = {"id": iid, "number": next((y.get("invocation_number") for y in run_lines if y.get("invocation_id") == iid
                                              and isinstance(y.get("invocation_number"), int)), None)}
        else:
            raise ValueError(f"read_records: the scope {scope!r} is not one of {RECORD_SCOPES}")
        lines = [y for y in run_lines if inv and y.get("invocation_id") == inv["id"]]
        amb = [y for y in run_lines if y.get("_invocation") == "ambiguous"]
        nm = (str(inv["number"]) if inv.get("number") is not None else inv["id"][:12]) if inv else ""
        words = ((f"in the active invocation ({nm}) of run {rid}" if inv else f"in the active invocation of run {rid} (none on record)")
                 if scope == "active invocation" else f"in invocation {nm} of run {rid}")
    return Records(path=str(p), kind=kind, scope=scope, rid=rid, words=words, invocation=inv, lines=lines, ambiguous=amb,
                   run_lines=run_lines, read=acc["read"], parsed=len(acc["parsed"]), refused=acc["refused"],
                   quarantined=acc["quarantined"], error=acc["error"], duplicates=dups)


def model_call(client, params, *, label, attempt, log_dir, clock, end_at=None, stream=True, timer=None, on_text=None, profile=None,
               journal=None, second_attempt=None, ident=None):
    """BUILD IC-3 (D5): the one helper for every model call. `params` are the Messages API's (model, max_tokens, output_config, system,
    messages) and go out unchanged. stream=True reads EVERY event (for event in stream:, the text deltas appended as text_stream did,
    the message_delta's usage kept as the API sent it, model_dump()); stream=False makes one messages.create with the end time as its
    timeout. Each attempt writes one line to <log_dir>/call_log.jsonl: the label and attempt; the model the API named; effort and
    max_tokens as sent; the SDK's version and the anthropic-version header's value; seconds to the first event, to the thinking block's
    start and end, to the first text, to the end; the longest gap between two events and which two; the number of text deltas; the stop
    reason; the usage; thinking_tokens (the API's number, or "not reported"); the error's class; how the call ended. Never a prompt, a
    reply, a key or any other header. The end time: inside the loop the clock is read on each event; `timer` (a factory, threading.Timer)
    closes the stream at the end time when no event comes. Either way EndTimeReached; the caller keeps the partial text and does not
    retry it. Returns (the final message, its text). BUILD IC-6 (D6): `journal`, a path — each text delta is written to it as it
    arrives (flushed at least every JOURNAL_FLUSH_S and at the end; a reply that is not streamed, whole), so a process killed mid-call
    leaves the text it had; a journal that cannot be written is said once and the call goes on. BUILD IC-6 (D1): `profile`, the call's profile key — the line gains counts only: the
    key, its planning tokens, the request's text length (prompt_chars) and the seconds to the first newline of the text (t_first_line; a
    reply that is not streamed arrives whole, so its first line at its end)."""
    t0 = clock()
    rec = {"label": label, "attempt": attempt, "run_id": run_id(),     # IC-3 (review c #3): which run (pipeline.py's id, else the process's), and
           "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),      # when — Block 2's log holds Block 1's lines
           "model": None, "model_asked": params.get("model"),
           "effort": (params.get("output_config") or {}).get("effort"), "max_tokens": params.get("max_tokens"),
           "sdk": getattr(client, "_version", None), "anthropic_version": None, "stream": bool(stream),
           "t_first_event": None, "t_thinking_start": None, "t_thinking_end": None, "t_first_text": None, "t_end": None,
           "longest_gap": None, "text_deltas": 0, "stop_reason": None, "refusal_category": None, "usage": None, "thinking_tokens": "not reported",
           "error": None, "ended_by": None, "end_time_s": None if end_at is None else round(end_at - t0, 3)}
    prof_ = CALL_PROFILES.get(profile) if isinstance(profile, str) else None         # BUILD IC-6 (D1): counts only
    rec.update(profile=profile, planning_tokens=(prof_ or {}).get("planning_tokens"), prompt_chars=_prompt_chars(params), t_first_line=None)
    if second_attempt is not None:      # BUILD IC-7 (D3): a second attempt that carries the first's errors — the flag and their count only
        rec.update(second_attempt=True, errors_first=int(second_attempt))
    if ident:           # BUILD IC-8 (D8): the call's identity (NR.new_call) — ids and counters; its journal by its file name
        rec.update({k: ident.get(k) for k in CALL_IDENTITY}, journal=Path(ident["journal"]).name if ident.get("journal") else None)
    xh = mock_identity_headers(label, profile, ident)      # BUILD IC-8 (D14, F30): test-only — {} unless NAIPC_MOCK_IDENTITY=1
    try:
        rec["anthropic_version"] = (getattr(client, "default_headers", None) or {}).get("anthropic-version")
    except Exception:  # noqa: BLE001
        pass
    text, msg, cut = [], None, []
    jw = {"f": None, "t": t0}
    if journal is not None:
        try:
            Path(journal).parent.mkdir(parents=True, exist_ok=True)
            jw["f"] = open(journal, "w", encoding="utf-8", errors="replace")
        except Exception as e_:  # noqa: BLE001
            say(f"    ⚠ the journal was not opened — {type(e_).__name__}; the call goes on")

    def _journal(piece, now_):
        if jw["f"] is None or not piece:
            return
        try:
            jw["f"].write(piece)
            if now_ - jw["t"] >= JOURNAL_FLUSH_S:
                jw["f"].flush()
                jw["t"] = now_
        except Exception as e_:  # noqa: BLE001
            say(f"    ⚠ the journal stopped — {type(e_).__name__}; the call goes on")
            jw["f"] = None
    try:
        if not stream:
            kw = dict(params)
            if xh:                      # BUILD IC-8 (D14): the identity header beside the body (the body unchanged)
                kw["extra_headers"] = xh
            if end_at is not None:      # IC-3 (review c #13): the client's own limits (read 120 s) stand inside the end time — the first
                left = max(1.0, end_at - clock())   # version gave the whole time left as one timeout, so a stalled call ran to its end time
                # Phase 8 (T1″ "naipc_requirements.py imports the standard library only", failed in the delivery rehearsal on an `import
                # httpx` here): the client's own Timeout class, each of its limits capped at the time left; no import
                t_ = getattr(client, "timeout", None)
                ph_ = ("connect", "read", "write", "pool")
                if all(isinstance(getattr(t_, a, None), (int, float)) for a in ph_):
                    kw["timeout"] = type(t_)(**{a: min(float(getattr(t_, a)), left) for a in ph_})
                else:
                    kw["timeout"] = min(float(t_), left) if isinstance(t_, (int, float)) and not isinstance(t_, bool) else left
            try:
                msg = client.messages.create(**kw)
            except Exception as e_:     # a timeout at or past the end time is the end time's, never retried (D5)
                if end_at is not None and clock() >= end_at - 0.5 and "Timeout" in type(e_).__name__:
                    rec["ended_by"] = "the end time (the plain call's timeout)"
                    raise EndTimeReached(f"{label}: past its end time ({end_at - t0:.0f} s after the call began) — ended") from e_
                raise
            now = clock() - t0
            rec.update(t_first_event=round(now, 3), t_end=round(now, 3), model=getattr(msg, "model", None),
                       stop_reason=getattr(msg, "stop_reason", None),
                       refusal_category=_refusal_category(getattr(msg, "stop_details", None)))     # BUILD IC-4 (D9)
            u = getattr(msg, "usage", None)
            rec["usage"] = u.model_dump() if hasattr(u, "model_dump") else None
            rec["thinking_tokens"] = _thinking_tokens(rec["usage"])
            text = [b.text for b in getattr(msg, "content", None) or [] if getattr(b, "type", None) == "text"]
            if "\n" in "".join(text):       # BUILD IC-6 (D1): a reply that is not streamed arrives whole — its first line at its end
                rec["t_first_line"] = rec["t_end"]
            _journal("".join(text), clock())        # BUILD IC-6 (D6)
            rec["ended_by"] = "the answer"
            return msg, "".join(text)
        with client.messages.stream(**params, **({"extra_headers": xh} if xh else {})) as s:     # BUILD IC-8 (D14): the body unchanged
            tm = None
            if timer is not None and end_at is not None:
                def _cut():
                    cut.append(clock())
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001
                        pass
                tm = timer(max(0.0, end_at - clock()), _cut)
                tm.daemon = True
                tm.start()
            last_t, last_k, gap, think_i = t0, "the request", None, None
            try:
                for ev in s:
                    if ev.type in ("text", "thinking", "signature", "citation", "input_json"):
                        continue                    # the SDK's own events, built from the one just read: no time of their own
                    now = clock()
                    k = ev.type
                    if k == "content_block_delta":
                        k = f"content_block_delta:{getattr(ev.delta, 'type', '?')}"
                    elif k == "content_block_start":
                        k = f"content_block_start:{getattr(ev.content_block, 'type', '?')}"
                    g = now - last_t
                    if gap is None or g > gap[0]:
                        gap = (g, last_k, k)
                    last_t, last_k = now, k
                    if rec["t_first_event"] is None:
                        rec["t_first_event"] = round(now - t0, 3)
                    if ev.type == "message_start":
                        rec["model"] = getattr(ev.message, "model", None)
                    elif ev.type == "content_block_start" and getattr(ev.content_block, "type", None) == "thinking":
                        think_i = ev.index
                        if rec["t_thinking_start"] is None:
                            rec["t_thinking_start"] = round(now - t0, 3)
                    elif ev.type == "content_block_stop" and think_i is not None and ev.index == think_i:
                        rec["t_thinking_end"] = round(now - t0, 3)
                        think_i = None
                    elif ev.type == "content_block_delta" and getattr(ev.delta, "type", None) == "text_delta":
                        text.append(ev.delta.text)
                        _journal(ev.delta.text, now)        # BUILD IC-6 (D6): written as it arrives
                        rec["text_deltas"] += 1
                        if rec["t_first_text"] is None:
                            rec["t_first_text"] = round(now - t0, 3)
                        if rec["t_first_line"] is None and "\n" in (ev.delta.text or ""):     # BUILD IC-6 (D1): the first complete line
                            rec["t_first_line"] = round(now - t0, 3)
                        if on_text is not None:
                            on_text(rec["text_deltas"])
                    elif ev.type == "message_delta":
                        u = getattr(ev, "usage", None)
                        rec["usage"] = u.model_dump() if hasattr(u, "model_dump") else None
                        rec["thinking_tokens"] = _thinking_tokens(rec["usage"])
                        rec["stop_reason"] = getattr(ev.delta, "stop_reason", None)
                        rec["refusal_category"] = _refusal_category(getattr(ev.delta, "stop_details", None))   # BUILD IC-4 (D9)
                    if end_at is not None and now > end_at:
                        rec["ended_by"] = "the end time (read in the loop)"
                        raise EndTimeReached(f"{label}: past its end time ({end_at - t0:.0f} s after the call began) — ended, its text kept")
                if gap is not None:
                    rec["longest_gap"] = {"s": round(gap[0], 3), "after": gap[1], "before": gap[2]}
                msg = s.get_final_message()
            except EndTimeReached:
                raise
            except Exception:
                if cut:
                    rec["ended_by"] = "the end time (the timer closed the stream)"
                    raise EndTimeReached(f"{label}: no event came before its end time ({end_at - t0:.0f} s after the call began) — the stream was "
                                         "closed, its text kept")
                raise
            finally:
                if tm is not None:
                    tm.cancel()
        rec["ended_by"] = rec["ended_by"] or "the end of the stream"
        return msg, "".join(text)
    except BaseException as e:
        rec["error"] = type(e).__name__
        raise
    finally:
        rec["t_end"] = round(clock() - t0, 3)
        rec["text_chars"] = sum(len(x) for x in text)
        if jw["f"] is not None:
            try:
                jw["f"].flush()
                jw["f"].close()
            except Exception:  # noqa: BLE001
                pass
        _write_call_log(log_dir, rec)
        model_call.last = (rec, "".join(text))
        nm_ = profile if isinstance(profile, str) and profile else str(label)     # BUILD IC-8 (D14, I8): the call's path — how it ended
        tr_ = False if isinstance(profile, str) and profile else None
        if rec.get("error") == "EndTimeReached":
            mark_path(path_id(nm_, "ended", "end_time"), cp_dir=log_dir, track=tr_)
        elif rec.get("error"):
            mark_path(path_id(nm_, "error", rec["error"]), cp_dir=log_dir, track=tr_)
        else:
            mark_path(path_id(nm_, "stop", rec.get("stop_reason") or "none"), cp_dir=log_dir, track=tr_)
        if second_attempt is not None:
            mark_path(path_id(nm_, "second_attempt"), cp_dir=log_dir, track=tr_)
        if profile:
            capacity_line(log_dir, rec)             # BUILD IC-6 (D4, I5)


CAPACITY_SHARE = 2 / 3          # BUILD IC-6 (D4): SRE's "nearly problematic" as this project's policy — the inverse of its ×1.5 rule
CAPACITY_COHORT = 1.25          # an input this much past the largest on record is a new cohort, unmeasured


def capacity_line(log_dir, rec):
    """BUILD IC-6 (D4, I5): after each call with a profile, one line — printed, and appended to <log_dir>/capacity_<run id>.jsonl — of
    counts only: its output against the profile's planning tokens, its seconds against the planning seconds, its input against the
    largest input on record (each as a share), and the time left at its start (its end time less its start). A warning when its
    seconds passed two-thirds of that time ("nearly problematic"), when its output passed the planning tokens (a new maximum:
    tools/rederive_limits_ic6.py over the staged logs, then the profiles), or when its input passed 1.25 × the largest on record (a new
    cohort the profile has not measured). Never raises; returns the row (None for a call with no profile)."""
    try:
        p = CALL_PROFILES.get(rec.get("profile"))
        if not p:
            return None
        u = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        out, inp, t, left = u.get("output_tokens"), u.get("input_tokens"), rec.get("t_end"), rec.get("end_time_s")
        pt, ps, li = p.get("planning_tokens"), p.get("planning_s"), p.get("largest_input")
        warn = []
        if _seconds(t) and _seconds(left) and left > 0 and t > CAPACITY_SHARE * left:
            warn.append(f"it ran {t:.0f} s, past two-thirds of the {left:.0f} s left at its start")
        if _seconds(out) and _seconds(pt) and out > pt and not p.get("unmeasured"):
            warn.append(f"its output {out:,} passed its planning tokens {pt:,} — a new maximum: re-derive the profiles "
                        "(tools/rederive_limits_ic6.py over the staged logs)")
        if _seconds(inp) and _seconds(li) and li > 0 and inp > CAPACITY_COHORT * li:
            warn.append(f"its input {inp:,} passed 1.25 × the largest on record ({li:,}) — a new cohort, unmeasured")
        share = lambda a, b: round(a / b, 3) if _seconds(a) and _seconds(b) and b > 0 else None  # noqa: E731
        row = {**{k: rec.get(k) for k in CALL_IDENTITY},        # BUILD IC-8 (D8): the call's identity, as its call-log line holds it
               "run_id": rec.get("run_id"), "at": rec.get("at"), "profile": rec.get("profile"), "label": rec.get("label"),
               "attempt": rec.get("attempt"), "stop_reason": rec.get("stop_reason"), "output": out, "planning_tokens": pt,
               "output_share": share(out, pt), "seconds": t, "planning_s": ps, "seconds_share": share(t, ps), "input": inp,
               "largest_input": li, "input_share": share(inp, li), "left_at_start": left, "warnings": warn}
        pct = lambda v: f"{v:.0%}" if isinstance(v, float) else "unmeasured"  # noqa: E731
        say(f"    capacity: {rec.get('profile')} — output {out if out is not None else '?'} of {pt:,} planned ({pct(row['output_share'])}), "
            f"{t if t is not None else '?'} s of {ps if ps is not None else 'unmeasured'} s ({pct(row['seconds_share'])}), input "
            f"{inp if inp is not None else '?'} of {li if li is not None else 'unmeasured'} on record ({pct(row['input_share'])})")
        for w in warn:
            say(f"    ⚠ capacity: {rec.get('profile')}: {w}")
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"capacity_{re.sub(r'[^A-Za-z0-9_-]+', '-', str(rec.get('run_id') or 'run'))}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ the capacity line was not written — {type(e).__name__}")
        return None


def _refusal_category(sd):
    """BUILD IC-4 (D9): a refusal's category — the API's label only (a dict or the SDK's object), never its explanation; None without."""
    c = sd.get("category") if isinstance(sd, dict) else getattr(sd, "category", None)
    return c if isinstance(c, str) and c else None


def _write_call_log(log_dir, rec):
    try:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        say(f"    ⚠ the call log was not written — {type(e).__name__}")


# ═══ BUILD IC-4 (D9): an attempt starts only when it can finish ═══
MEASURED_MIN_S = 30.0    # a call log line is a rate only when it ran at least this long: a shorter line is mostly the time before its first
#                          event and its first text (the Architect's 806- and 1,045-token lines took 7.8 and 8.9 s), and the mock's lines
#                          (42 tokens in under a second) are no rate at all


def _call_log_lines(log_dir):
    """BUILD IC-4 (D9): the parsed lines of <log_dir>/call_log.jsonl — the harness's calls since the last Block 1; a line that is not a
    JSON object is skipped. BUILD IC-8 (D9): read as every record file is (_record_lines: a partial last line quarantined)."""
    return [x for _i, x in _record_lines(Path(log_dir) / CALL_LOG)["parsed"]]


def _seconds(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v >= 0


# ═══ BUILD IC-6 (D2, I2): the start rule reads the plan — never max_tokens ═══
def _line_of(r, prof):
    """BUILD IC-6 (D2): whether a call-log line is this profile's call — its own "profile" key (IC-6 writes it); for a line written before
    IC-6, its label, and for the Oracle's update the limit it asked (128,000: IC-4's keyed edits; else the whole-field reply) — the one
    rule that reads a parameter, for lines with no key only. A log is one harness's, so there a label names one call."""
    if isinstance(r.get("profile"), str):
        return r["profile"] == prof.get("key")
    if r.get("label") != prof.get("label"):
        return False
    if prof.get("key") in ("oracle/update", "oracle/update_whole"):
        return (r.get("max_tokens") == 128000) == (prof.get("key") == "oracle/update")
    return True


def _call_lines(prof, log_dir, model):
    """This call's lines in <log_dir>/call_log.jsonl by `model` (the model asked, else the one the API named)."""
    return [r for r in _call_log_lines(log_dir) if _line_of(r, prof) and (r.get("model_asked") or r.get("model")) == model]


def _finished(r):
    """A finished line — stop end_turn, no error, output tokens above 0, its seconds a number: (output tokens, seconds), else None."""
    u = r.get("usage")
    out, t = (u.get("output_tokens") if isinstance(u, dict) else None), r.get("t_end")
    if r.get("stop_reason") == "end_turn" and not r.get("error") and _seconds(out) and out > 0 and _seconds(t):
        return int(out), float(t)
    return None


def measured_call_rate(prof, log_dir, model):
    """BUILD IC-6 (D2): this machine's writing rate for this call — the slowest finished line of this call in the current log that ran at
    least MEASURED_MIN_S: (tokens a second, its basis in words), or None. (IC-4's measured_rate read every call's lines, so one call's
    rate set another's need.)"""
    rates = [(o / t, o, t) for o, t in filter(None, (_finished(r) for r in _call_lines(prof, log_dir, model))) if t >= MEASURED_MIN_S]
    if not rates:
        return None
    rate, o, t = min(rates)
    return rate, f"this machine's call log: the slowest of {len(rates)} finished line(s) of this call, {o:,} tokens in {t:.1f} s"


def attempt_need(prof, log_dir, model, stated_rate=None, *, stated_first_text=None):
    """BUILD IC-6 (D2, I2): an attempt's expected length {"s": seconds, "basis": words, "rate"} — from the call's profile (`prof`: its key
    or the profile), never from its max_tokens: a raised limit changes no decision. A reply that is all or nothing (object, schema,
    text): the profile's planning tokens at the slowest of its floor rate on record and this machine's slowest finished line of this call
    (measured_call_rate); then the larger of that and 1.5 × the longest recorded duration of this call (on record and on this machine).
    With no rate: 1.5 × that duration (the Architect: every line under 30 s), else the planning tokens at `stated_rate` = (tokens a
    second, its source) — a call with no sample, its plan its limit (unmeasured). A line-form reply (its complete lines kept when cut):
    its first line — the longest of the profile's first_line_s and this machine's lines of this call (t_first_line, else the first text
    + 30 s; Perplexity: "time-to-first-text alone is insufficient"); with neither, `stated_first_text` = (seconds, its source) + 30 s."""
    p = profile(prof) if isinstance(prof, str) else prof
    mine = _call_lines(p, log_dir, model)
    if (call_form(p["key"]) if p.get("key") in CALL_PROFILES else p.get("form")) == "lines":     # BUILD IC-6 (D8): its form as it runs
        cands = [(float(p["first_line_s"]), "its profile: the longest first line on record")] if _seconds(p.get("first_line_s")) else []
        for r in mine:
            if _seconds(r.get("t_first_line")):
                cands.append((float(r["t_first_line"]), "this machine's call log: a first line of this call"))
            elif _seconds(r.get("t_first_text")):
                cands.append((float(r["t_first_text"]) + 30.0, "this machine's call log: a first text of this call + 30 s"))
        if cands:
            s, b = max(cands)
        elif stated_first_text is not None:
            s, b = float(stated_first_text[0]) + 30.0, f"the stated first text + 30 s: {stated_first_text[1]}"
        else:
            raise ValueError(f"{p.get('key')}: a line-form call with no first line on record and no stated first text")
        return {"s": s, "basis": f"its first line after {s:.0f} s — {b}; every complete line before its end time is kept", "rate": None}
    fin = [x for x in (_finished(r) for r in mine) if x]
    durs = ([float(p["longest_s"])] if _seconds(p.get("longest_s")) else []) + [t for _o, t in fin]
    longest = max(durs) if durs else None
    s_dur = 1.5 * longest if longest is not None else None
    rates = []
    if _seconds(p.get("floor_rate")) and p["floor_rate"] > 0:
        fs = p.get("floor_sample") or [None, None]
        rates.append((float(p["floor_rate"]), "the slowest finished line on record of this call"
                      + (f", {fs[0]:,} tokens in {fs[1]:.1f} s" if _seconds(fs[0]) and _seconds(fs[1]) else "")))
    m = measured_call_rate(p, log_dir, model)
    if m:
        rates.append(m)
    tokens = int(p["planning_tokens"])
    what = f"{tokens:,} tokens" + (", unmeasured: its limit," if p.get("unmeasured") else "")
    if rates:
        rate, b = min(rates, key=lambda x: x[0])
        s, basis = tokens / rate, f"{what} at {rate:.1f} tokens a second — {b}"
    elif s_dur is not None:
        return {"s": s_dur, "basis": f"1.5 × its longest recorded {longest:.1f} s (no line of this call ran {MEASURED_MIN_S:.0f} s)",
                "rate": None}
    elif stated_rate is not None:
        rate = float(stated_rate[0])
        s, basis = tokens / rate, f"{what} at {rate:.1f} tokens a second — the stated rate: {stated_rate[1]}"
    else:
        raise ValueError(f"{p.get('key')}: no rate on record or on this machine, no duration and no stated rate")
    if s_dur is not None and s_dur > s:
        s, basis = s_dur, basis + f"; 1.5 × its longest recorded {longest:.1f} s"
    return {"s": s, "basis": basis, "rate": rate}


def start_check(prof, log_dir, model, stated_rate=None, *, left, wait=0.0, begins=False, retry=False, stage_s=None, stated_first_text=None):
    """BUILD IC-4 (D9), the deadline-aware start, over a profile (BUILD IC-6 D2): (may start, words or None, need). An attempt that begins
    its stage always starts — not starting is the failure this rule exists to prevent — and the words then say so when its expected
    length is longer than the whole stage, or (BUILD IC-6 D3) than the time to its end time `left` when the block's clock ends it first.
    Any other attempt starts only when the time left to its end time, less the wait before it, is at least its expected length
    (attempt_need: the profile's plan, never max_tokens); otherwise the words begin "no time for another attempt" (a retry) or "no time
    for the call" (a call's first attempt)."""
    need = attempt_need(prof, log_dir, model, stated_rate, stated_first_text=stated_first_text)
    if begins:
        if stage_s is not None and need["s"] > stage_s:
            return True, (f"its expected {need['s']:.0f} s ({need['basis']}) is longer than its whole {stage_s:.0f} s stage — it starts, "
                          "and its end time ends it"), need
        if need["s"] > left and (stage_s is None or left < stage_s - 0.5):
            return True, (f"its expected {need['s']:.0f} s ({need['basis']}) is longer than the {max(0.0, left):.0f} s to its end time (the "
                          "block's end less the later tracks' reserves) — it starts, and its end time ends it"), need
        return True, None, need
    have = left - wait
    if have >= need["s"]:
        return True, None, need
    what = "no time for another attempt" if retry else "no time for the call"
    after = f" after the {wait:.0f} s wait" if wait else ""
    return False, f"{what} — {max(0.0, have):.0f} s left{after}; an attempt needs {need['s']:.0f} s ({need['basis']})", need


# ═══ BUILD IC-6 (D6, I4): one recovery path ═══
CUT_WORDS = {"tokens": "its limit", "clock": "its end time", "transport": "a dropped stream", "error": "an error after its text began"}
_TRANSPORT = {"IncompleteResponse", "APIConnectionError", "APITimeoutError", "TransportError", "TimeoutException", "ReadTimeout",
              "ReadError", "RemoteProtocolError", "NetworkError", "ConnectError", "WriteError", "ConnectionResetError", "BrokenPipeError"}


def cut_kind(exc, rec=None):
    """BUILD IC-6 (D6, I4): how a call ended that did not finish — "refusal" (ModelRefused, or the stop "refusal"), "clock"
    (EndTimeReached), "tokens" (MaxTokensReached, or the stop "max_tokens"), "transport" (IncompleteResponse, a connection or transport
    error, a read timeout, an error event mid-stream: the SDK's status 200), "error" (any other). Read from the exception's class names
    (each harness defines its own GenerationError classes; this module imports none) and the call log's line `rec`."""
    names = {c.__name__ for c in type(exc).__mro__} if exc is not None else set()
    stop = (rec or {}).get("stop_reason")
    if "ModelRefused" in names or stop == "refusal":
        return "refusal"
    if "EndTimeReached" in names:
        return "clock"
    if "MaxTokensReached" in names or stop == "max_tokens":
        return "tokens"
    if names & _TRANSPORT or ("APIStatusError" in names and getattr(exc, "status_code", None) == 200):
        return "transport"
    return "error"


def cut_words(kind, exc=None):
    """A cut in plain words, as the Oracle's record has said them since IC-4 (an error names its class)."""
    w = CUT_WORDS.get(kind, kind)
    return f"{w} ({type(exc).__name__})" if kind == "error" and exc is not None else w


def complete_lines(text):
    """BUILD IC-6 (D6): a cut reply's complete lines — everything up to its last newline ("" when none came)."""
    t = text if isinstance(text, str) else ""
    i = t.rfind("\n")
    return t[:i + 1] if i >= 0 else ""


def _line_objects(text):
    """[(the line, its object or None)] for each line that is not blank or a code fence — the strict decoder; a line that does not decode
    to one object, or gives a key twice, has None (the caller's parser names it)."""
    out = []
    for raw in (text or "").split("\n"):
        s = raw.strip()
        if not s or s.startswith("```"):
            continue
        try:
            o, finds = strict_loads(s, "lines")
        except Exception:  # noqa: BLE001
            o, finds = None, True
        out.append((raw, o if isinstance(o, dict) and not finds else None))
    return out


CONTINUE_WORDS = ("Your reply stopped after the line for {last}. Continue with the lines after it, in the same order and form, starting with "
                  "{next}; number new items from new.{k}. Do not write a line you already wrote.")
CONTINUE_ITEM_WORDS = ("Your reply stopped after the line for {last}. Continue with the lines after it, in the same order and form. Do not "
                       "write a line you already wrote.")      # BUILD IC-6 (D13): a reply of items with no new ids (the deliverables)


def line_call(prof, send, prompt, *, end_at, clock, retryable, retry_wait, sleep, refuse, log_dir, model, stated_rate=None,
              stated_first_text=None, begins=False, stage_s=None, key_of=None, resume=None, continue_ok=True, stage_label="the call",
              say_=None, continue_words=None):
    """BUILD IC-6 (D6, I4): the one path for a line-form call — every complete line kept, whatever cut it.
    `send(messages, label)` makes one call (the harness's own: its client, its stop-reason check, its partial kept) and returns the
    reply's text, or raises; NR.model_call.last holds the call's (record, text) either way. `refuse(words)` raises the harness's own
    no-time error. One attempt (started by start_check: `begins` — it begins its stage); a second only when no complete line came, the
    error is one `retryable` accepts and its need still fits; after any cut but a refusal, the complete lines; after a refusal, nothing
    ("treat any partial output as incomplete and discard it"). Then at most one continuation — the cut was "tokens" or "transport",
    `continue_ok` (NAIPC_IC6_CONTINUE), and the time left covers the profile's first line: the original user message (the same bytes), an
    assistant message holding the complete lines, and a user message (CONTINUE_WORDS, from `resume(kept objects)` → (the last line's
    key, the next key or None, the next new number)); same model, effort and profile; its own log line (label <label>+c). The assistant
    turn is never the last message (a prefill, which Opus 4.6 and later reject). The merge: a continuation line whose key (`key_of`) the
    first reply already wrote is dropped and named; a continuation that fails or brings no line leaves the kept lines as they were.
    → {"text", "how" ("end_turn" | "cut: <words>" | "declined (<category>)"), "kind", "kept" (complete lines kept), "continuation"
    (None, or {"how", "lines", "dropped", "label", "words"})}. Raises when no complete line came and no retry is left."""
    say_ = say_ or say
    p = profile(prof) if isinstance(prof, str) else prof
    first = [{"role": "user", "content": prompt}]
    attempt, wait, last_err = 0, 0.0, None
    while True:
        attempt += 1
        ok, note, _n = start_check(p, log_dir, model, stated_rate, left=end_at - clock(), wait=wait, begins=begins and attempt == 1,
                                   retry=attempt > 1, stage_s=stage_s, stated_first_text=stated_first_text)
        if not ok:
            refuse(f"{stage_label}: {note}")
        if note:
            say_(f"    {stage_label}: {note}")
        if wait:
            say_(f"  ↻ {stage_label}: retry 1 of 1 after {last_err}")
            sleep(wait)
        before = getattr(model_call, "last", None)
        try:
            return {"text": send(first, p["label"]), "how": "end_turn", "kind": None, "kept": None, "continuation": None}
        except Exception as e:  # noqa: BLE001 — every ending: classified, its complete lines kept (a refusal's never)
            now = getattr(model_call, "last", None)
            rec, got = now if now is not None and now is not before else ({}, "")
            kind = cut_kind(e, rec)
            if kind == "refusal":
                return {"text": None, "how": f"declined ({(rec or {}).get('refusal_category') or 'no category given'})", "kind": kind,
                        "kept": 0, "continuation": None}
            kept = complete_lines(got)
            if kept.strip():
                err = e
                break
            if not retryable(e) or attempt >= 2:
                raise
            wait, last_err = retry_wait(e, attempt), type(e).__name__
    objs = [o for _r, o in _line_objects(kept)]
    res = {"text": kept, "how": "cut: " + cut_words(kind, err), "kind": kind, "kept": len(objs), "continuation": None}
    if kind not in ("tokens", "transport"):
        return res
    if not continue_ok:
        res["continuation"] = {"how": "not asked: NAIPC_IC6_CONTINUE is off", "lines": 0, "dropped": []}
        return res
    need = attempt_need(p, log_dir, model, stated_rate, stated_first_text=stated_first_text)
    left = end_at - clock()
    if left < need["s"]:
        res["continuation"] = {"how": f"not asked: {max(0.0, left):.0f} s left; its first line needs {need['s']:.0f} s", "lines": 0,
                               "dropped": []}
        return res
    good = [o for o in objs if o is not None]
    last, nxt, k = resume(good) if resume else ((key_of(good[-1]) if key_of and good else "the last complete line"), None, 1)
    words = (continue_words or CONTINUE_WORDS).format(last=last, next=nxt or "the next line you add", k=k)   # D13: a call's own words
    msgs = first + [{"role": "assistant", "content": kept.rstrip("\n")}, {"role": "user", "content": words}]
    label = p["label"] + "+c"
    before = getattr(model_call, "last", None)
    try:
        ctext, chow = send(msgs, label), "end_turn"
        ctext = ctext if ctext.endswith("\n") else ctext + "\n"
    except Exception as e:  # noqa: BLE001
        now = getattr(model_call, "last", None)
        rec, got = now if now is not None and now is not before else ({}, "")
        ck = cut_kind(e, rec)
        ctext = "" if ck == "refusal" else complete_lines(got)
        chow = (f"declined ({(rec or {}).get('refusal_category') or 'no category given'})" if ck == "refusal"
                else f"failed before its text began ({type(e).__name__})" if not (got or "").strip()     # BUILD IC-6 (11/16): no text
                else "cut: " + cut_words(ck, e))                                                    # came, so no cut of a text
    seen = {key_of(o) for o in good} if key_of else set()
    add, dropped = [], []
    for raw, o in _line_objects(ctext):
        k_ = key_of(o) if (key_of and o is not None) else None
        if k_ is not None and k_ in seen:
            dropped.append(k_)
            continue
        if k_ is not None:
            seen.add(k_)
        add.append(raw)
    res["text"] = kept + "".join(r + "\n" for r in add)
    res["continuation"] = {"how": chow, "lines": len(add), "dropped": dropped, "label": label, "words": words}
    return res


def edit_resume(order):
    """BUILD IC-6 (D6, D12): line_call's `resume` for an edit reply over a review set (`order`: its ids in Python's order) — from the kept
    objects, (the last line's id, the review set's next id — None once an add was written: the review part is done —, the next new
    number). The Oracle's update and the Muse's read it (one rule)."""
    order_ = list(order or [])

    def resume(objs):
        ids = [str(o.get("id")) for o in objs if o.get("id") is not None]
        rev = [i for i in ids if i in order_]
        nxt = None
        if rev and not any(str(o.get("op")) == "add" for o in objs):
            j = order_.index(rev[-1]) + 1
            nxt = order_[j] if j < len(order_) else None
        news = [int(m.group(1)) for i in ids for m in [_EDIT_NEW_RX.fullmatch(i)] if m]
        return (ids[-1] if ids else "the last complete line"), nxt, (max(news) + 1 if news else 1)
    return resume


# ═══ BUILD IC-4 (D11): the update as keyed edits — one line per item, written by the model, applied by Python ═══
EDIT_OPS = ("keep", "replace", "add", "remove")
EDIT_KEYS = ("op", "id", "base", "value", "because", "why", "kind", "parent")
EDIT_WHY_CHARS = 400


def _fit_why(text):
    """BUILD IC-8 (D1): an edit line's reason fitted by the registry's one owner (row edit.why, EDIT_WHY_CHARS on the shown text)."""
    import naipc_registry as _REG    # the registry sits beside this module in every folder (tests/install_module.py)
    return _REG.fit_cap(text, "edit.why", EDIT_WHY_CHARS)
_EDIT_BASE_RX = re.compile(r"[0-9a-fA-F]{8}")
_EDIT_NEW_RX = re.compile(r"new\.([1-9][0-9]{0,3})")


def edit_base(value):
    """BUILD IC-4 (D11): the 8 hex characters Python prints for an item's current value — the start of the sha256 of its canonical JSON
    (keys sorted, no spaces, every character as itself). A line's base must equal it: the line was written against this value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]


def parse_edit_lines(text, complete=True):
    """BUILD IC-4 (D11): a reply of JSON Lines → (lines, problems, partial). Each line is read on its own by the one strict decoder: one
    object is an edit ({"line": its number, 1-based, "edit": the object}); a blank line is skipped; a code-fence line is named and skipped
    (the lines inside a fence are read); anything else — two objects on one line, a trailing comma, prose, a key given twice — is one
    problem named by its line number, never applied. `complete` False (the reply was cut: its limit, a dropped stream, its end time): a last
    line the reply did not end with a newline is the partial last line — dropped whatever it holds (it may decode by chance), and returned
    as `partial` (its characters) so the record can name it. Never raises."""
    rows = (text if isinstance(text, str) else "").split("\n")
    partial = None
    if not complete and rows and rows[-1].strip():
        partial = len(rows[-1])
        rows = rows[:-1]
    lines, problems = [], []
    for n, raw in enumerate(rows, 1):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("```"):
            problems.append({"line": n, "why": "a code fence, not an edit line (the lines inside it are read)", "detail": cut(s, 40)})
            continue
        try:
            obj, finds = strict_loads(s, "update")
        except Exception as e:  # noqa: BLE001 — every line that is not one JSON value is named, the rest read on
            problems.append({"line": n, "why": "not one JSON object, so not applied", "detail": f"{type(e).__name__}: {cut(str(e), 80)}"})
            continue
        if finds:
            problems.append({"line": n, "why": "a key is given twice in the line, so it is not applied",
                             "detail": "keys: " + json.dumps(sorted({f["key"] for f in finds}), ensure_ascii=False)})
            continue
        if not isinstance(obj, dict):
            problems.append({"line": n, "why": f"a JSON {type(obj).__name__}, not an object, so not applied", "detail": ""})
            continue
        lines.append({"line": n, "edit": obj})
    return lines, problems, partial


def apply_edits(lines, items, order, refs, validate, add_kinds=(), parent_kinds=None, parent_required=(), remove_kinds=None,
                because_optional=()):
    """BUILD IC-4 (D11): the lines applied to the review set, in the reply's order. `items`: {id: {"kind", "value", "base"}} (base =
    edit_base(value), as the request printed it); `order`: the review set's ids in Python's order; `refs`: the change list's refs
    (ref_check's form); `validate(kind, value, op, item_id)` → (the value as stored, None) or (None, (why, detail)) — the caller's check
    of the item's schema; `add_kinds`: the kinds an add may create; `parent_kinds`: {kind: (the kinds its parent may be)}; `parent_required`:
    the kinds an add must name a parent for (an early warning's risk); `remove_kinds`: the kinds a remove may end (the items of a list;
    None: any).
    A line is applied only when it is the one line for its id, its op is one of EDIT_OPS, its id is an item of the review set (an add's is
    new.<n>), its base is the item's current base (letter case aside), its reasons are changes in the list (replace, remove, add), its value
    validates (replace, add), and an add's kind is allowed and its parent is a live item of the right kind (or an add applied earlier in the
    reply). Every other line is named with its reason; two lines for one id are both named and neither is applied (ids do not make edits
    commute). A key the line form does not have is named and the line applied without it.
    Returns {"applied": [{line, op, id, kind, value, because, why, parent}], "not_applied": [{line, id, why, detail}], "extra_keys":
    [{line, keys}], "status": {review-set id: kept | replaced | removed | not applied | not reviewed}}."""
    parent_kinds = parent_kinds or {}
    applied, nap, extra = [], [], []
    count = {}
    for ln in lines:
        e = ln["edit"]
        if isinstance(e.get("id"), str):
            count[e["id"]] = count.get(e["id"], 0) + 1
    added, removed, touched = {}, set(), {}
    for ln in lines:
        e, n = ln["edit"], ln["line"]
        op, i = e.get("op"), e.get("id")
        xk = [str(k) for k in e if k not in EDIT_KEYS]
        if xk:
            extra.append({"line": n, "keys": xk})

        def no(why, detail=""):
            nap.append({"line": n, "id": i if isinstance(i, str) else None, "why": why, "detail": cut(str(detail or ""), DETAIL_CHARS)})
            if isinstance(i, str):
                touched.setdefault(i, "not applied")
        if op not in EDIT_OPS:
            no("its op is not one of keep, replace, add, remove", f"op: {name_cut([op])}")
            continue
        if not isinstance(i, str) or not i.strip():
            no("it names no id")
            continue
        if count.get(i, 0) > 1:
            no("two lines for this item — neither is applied", f"id: {name_cut([i])}")
            continue
        why_ = e.get("why")
        why = _fit_why(clean_text(why_)) if isinstance(why_, str) else ""      # BUILD IC-8 (D1): fitted on its shown text
        if op == "add":
            m = _EDIT_NEW_RX.fullmatch(i)
            kind, par = e.get("kind"), e.get("parent")
            if not m:
                no("an add's id is new.<n>")
                continue
            if kind not in add_kinds:
                no(f"an add of a kind the review set does not take ({name_cut([kind])})")
                continue
            if par is not None or kind in parent_required:
                ok_k = parent_kinds.get(kind, ())
                live = isinstance(par, str) and ((par in items and items[par]["kind"] in ok_k and par not in removed)   # IC-4 Phase 7
                                                 or (added.get(par) in ok_k))          # (review a2): a parent that is no string is named
                if not live:
                    no(f"its parent is no live {' or '.join(ok_k) or 'item'} of the review set" if par is not None
                       else f"an add of kind {kind} names its parent ({' or '.join(ok_k)})",
                       f"parent: {name_cut([par if isinstance(par, str) else json.dumps(par, ensure_ascii=False)[:60]])}" if par is not None else "")
                    continue
            rs, bad = ref_check(e.get("because"), refs)
            if bad:
                no(bad)
                continue
            if "value" not in e:
                no("an add with no value")
                continue
            val, err = validate(kind, e["value"], "add", i)
            if err:
                no(err[0], err[1])
                continue
            added[i] = kind
            applied.append({"line": n, "op": op, "id": i, "kind": kind, "value": val, "because": rs, "why": why, "parent": par})
            continue
        if i not in items:
            no("no item of the review set has this id", f"id: {name_cut([i])}")
            continue
        b = e.get("base")
        if not (isinstance(b, str) and _EDIT_BASE_RX.fullmatch(b.strip())):
            no("its base is not the 8 hex characters printed for the item")
            continue
        if b.strip().lower() != items[i]["base"]:
            no("its base is not the item's current value (the line was written against another value)",
               f"base {b.strip()} against {items[i]['base']}")
            continue
        kind = items[i]["kind"]
        if op == "keep":
            applied.append({"line": n, "op": op, "id": i, "kind": kind, "value": items[i]["value"], "because": [], "why": why, "parent": None})
            touched[i] = "kept"
            continue
        rs, bad = ref_check(e.get("because"), refs)
        if bad and kind in because_optional and op == "replace" and not e.get("because"):
            rs, bad = [], None          # IC-4 Phase 7 (review c18): the update's own words (no Block 1 value) need no because
        if bad:
            no(bad)
            continue
        if op == "remove":
            if remove_kinds is not None and kind not in remove_kinds:
                no("only an item of a list can be removed — this item is replaced or kept", f"kind {kind}")
                continue
            removed.add(i)
            applied.append({"line": n, "op": op, "id": i, "kind": kind, "value": None, "because": rs, "why": why, "parent": None})
            touched[i] = "removed"
            continue
        if "value" not in e:
            no("a replace with no value")
            continue
        val, err = validate(kind, e["value"], "replace", i)
        if err:
            no(err[0], err[1])
            continue
        applied.append({"line": n, "op": op, "id": i, "kind": kind, "value": val, "because": rs, "why": why, "parent": e.get("parent")})
        touched[i] = "replaced"
    status = {i: touched.get(i, "not reviewed") for i in order}
    return {"applied": applied, "not_applied": nap, "extra_keys": extra, "status": status}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="NAIPC requirements contract (schema v1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("file")
    t = sub.add_parser("template"); t.add_argument("kind", choices=KINDS)
    t.add_argument("--out", default=None, help="write the template to this new file as UTF-8 (no BOM, LF) instead of stdout; an existing file is refused")
    i = sub.add_parser("ids"); i.add_argument("file")
    mt = sub.add_parser("map-template", help="a coverage map skeleton for a contract (P0.3)"); mt.add_argument("file")
    mt.add_argument("--out", required=True, help="the new map file, named <challenge-id>.map.json beside the contract; UTF-8, no BOM; an existing file is refused")
    vm = sub.add_parser("validate-map", help="check a coverage map against the contract beside it (P0.3)"); vm.add_argument("map")
    cv = sub.add_parser("coverage", help="the coverage check of one rendered track page (P0.3; reads only)")
    cv.add_argument("track", choices=TRACKS); cv.add_argument("page"); cv.add_argument("--requirements", required=True)
    cv.add_argument("--run-id", default=None, help="the run the page must carry (default: any run)")
    cf = sub.add_parser("confirm", help="BUILD IC-4 (D5): copy draft entries into the contract beside the draft, validated")
    cf.add_argument("draft"); cf.add_argument("--items", required=True, help="comma-separated <block>:<n>, such as relations:0,aliases:1")
    dc = sub.add_parser("draft-conditions", help="BUILD IC-4 (D15): a link and an acceptance condition per requirement, drafted from the "
                                                 "brief's words into <challenge>.conditions.draft.json beside the contract (never into the map)")
    dc.add_argument("file")
    cc = sub.add_parser("confirm-conditions", help="BUILD IC-4 (D15): copy the named drafted conditions into the map, validated")
    cc.add_argument("draft"); cc.add_argument("--items", required=True, help="comma-separated requirement keys, such as users.maya,winning.win-1")
    a = ap.parse_args(argv)
    if a.cmd == "draft-conditions":
        dp, n, probs = write_condition_drafts(a.file)
        if probs:
            print(f"NOT DRAFTED: the map beside the contract is invalid ({len(probs)} problem(s)): {probs[0]}")
            return 1
        print(f"DRAFTED: {n} requirement(s) -> {dp} (the map is unchanged; approve with confirm-conditions {dp.name} --items <keys>)")
        return 0
    if a.cmd == "confirm-conditions":
        n, probs = confirm_conditions(a.draft, [x.strip() for x in a.items.split(",") if x.strip()])
        if probs:
            print(f"NOT CONFIRMED: {len(probs)} problem(s) — the map is unchanged")
            for p_ in probs:
                print(f"  {p_}")
            return 1
        print(f"CONFIRMED: {n} condition(s) copied into the map beside {a.draft}")
        return 0
    if a.cmd == "confirm":
        n, probs = confirm(a.draft, [x.strip() for x in a.items.split(",") if x.strip()])
        if probs:
            print(f"NOT CONFIRMED: {len(probs)} problem(s) — the contract is unchanged")
            for p_ in probs:
                print(f"  {p_}")
            return 1
        print(f"CONFIRMED: {n} entr(ies) copied into the contract beside {a.draft}")
        return 0
    if a.cmd == "template":
        doc = template(a.kind)
        if a.out:
            p = Path(a.out)
            if p.exists():
                print(f"REFUSED: {p} already exists — choose a new file name (nothing was written)")
                return 1
            if not p.parent.is_dir():
                print(f"REFUSED: the folder does not exist: {p.parent} (nothing was written)")
                return 1
            data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            p.write_bytes(data)
            todos = []
            _todos(doc, "", todos)
            print(f"{p}: {len(data)} bytes written (UTF-8); {len(todos)} {TODO} placeholder(s) to fill, then: python -B naipc_requirements.py validate {p}")
            return 0
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    if a.cmd == "validate-map":
        mp = Path(a.map)
        cp = contract_path_for(mp)
        try:
            doc = load(cp)
        except RequirementsError as e:
            print(f"INVALID: {mp} — the contract beside it ({cp.name}) is not usable: {len(e.problems)} problem(s)")
            for p in e.problems:
                print(f"  {p}")
            return 1
        cmap, problems = load_map(mp, doc)
        if problems:
            print(f"INVALID: {mp} — {len(problems)} problem(s)")
            for p in problems:
                print(f"  {p}")
            return 1
        line, g = map_summary(cmap, doc)
        print(f"VALID: {doc['challenge']['id']} map — {line}")
        gaps = [i for t in TRACKS for i in g["items"][t]]
        if gaps:
            print(f"  unmapped items (gaps): {', '.join(gaps)}")
        if g["users"]:
            print(f"  named users with no section (Rule 13 gaps): {', '.join(g['users'])}")
        return 0
    try:
        doc = load(a.requirements if a.cmd == "coverage" else a.file)
    except RequirementsError as e:
        print(f"INVALID: {a.requirements if a.cmd == 'coverage' else a.file} — {len(e.problems)} problem(s)")
        for p in e.problems:
            print(f"  {p}")
        return 1
    if a.cmd == "validate":
        print(f"VALID: {doc['challenge']['id']} — tracks " + ", ".join(tracks_summary(doc))
              + f" · {len(user_ids(doc))} named user(s) · {len(endpoint_names(doc))} endpoint(s) · curveball "
              + ("FILLED" if doc["curveball"] else "empty"))
        return 0
    if a.cmd == "map-template":
        p, want = Path(a.out), f"{doc['challenge']['id']}.map.json"
        if p.name != want:
            print(f"REFUSED: name the map {want} — the harnesses look for it beside the contract by that name (nothing was written)")
            return 1
        if p.exists():
            print(f"REFUSED: {p} already exists — choose a new file name (nothing was written)")
            return 1
        if not p.parent.is_dir():
            print(f"REFUSED: the folder does not exist: {p.parent} (nothing was written)")
            return 1
        data = (json.dumps(map_template(doc), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        p.write_bytes(data)
        print(f"{p}: {len(data)} bytes written (UTF-8); {len(item_ids(doc))} item(s) and {len(user_ids(doc))} named user(s) to map, "
              f"the section catalogue and a field guide inside; then: python -B naipc_requirements.py validate-map {p}")
        return 0
    if a.cmd == "coverage":
        page = Path(a.page)
        if not page.is_file():
            print(f"coverage: page not found: {page}")
            return 2
        cmap, problems, missing = map_for_check(a.requirements, doc)
        res = coverage_check(doc, cmap, a.track, page.read_bytes().decode("utf-8", "replace"), run_id=a.run_id)
        res["map_problems"], res["map_missing"] = problems, missing
        for line in gate_lines(res):
            say(line)
        gaps = res["counts"]["covered"] < res["counts"]["items"] or any(res.get(k) for k in ("fetch_errors", "unknown_ids", "dead_links"))
        return 1 if gaps else 0
    print(json.dumps({"challenge": doc["challenge"]["id"], "items": item_ids(doc), "users": user_ids(doc),
                      "winning": winning_ids(doc), "endpoints": endpoint_names(doc)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
