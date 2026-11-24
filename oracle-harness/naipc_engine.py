"""naipc_engine.py — the shared fit & priority engine (BUILD B1: P1.1–P1.3, P1.5–P1.7). Pure Python, deterministic, no model call.

The table is the challenge's sidecar <challenge-id>.engine.json beside the contract (D1), typed on the day with the contract and checked
here against the real records (validate_spec). For every person (a preset: a named user of the contract, or a preset labeled
illustrative) and every option set (one endpoint's records):
  - gates exclude and never score (D2): not closed (D5: the record's own status, the announcement, an alert — the Architect's matcher,
    copied verbatim below), then the set's gates in order (in_set, age_range, free_above_zero, open_now, bool); a failed gate excludes,
    an unknown one ("to confirm": an attribute the person's preset does not give, a value the record does not give in a readable form,
    hours that do not parse, a closure word in the record's own text) keeps the option out of the ranking and lists it;
  - factors score: points = max × a term from 0 to 1 by one of four published forms (bool, ratio, inverse, match), kept in tenths, so
    the displayed score is exactly the sum of the displayed components;
  - ties: the set's tie-breakers (factor points, highest first), then the record id.
Every outcome carries its trace (the gate, the field, the record's value, the person's value). Nothing outside the table changes a
score. A record's text fields can be read through an annotation that quotes the record's own words (validated: the quote must occur
in that record), for limits the data states only in text ("Serves families with children only").
Never raises from the entry points (run, for_oracle): a problem is a line in result["problems"] (the console prints them).
The copies in each harness folder are byte-identical to src/naipc_engine.py (tests/install_module.py)."""
import html as _html
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import contextlib
import naipc_requirements as NR
import naipc_registry as _REG     # BUILD IC-8 (D1): the one sentence finder (the registry sits beside each copy of this module)

_NAMES_RAW = [False]              # BUILD IC-8 (D12, F29): names_mode — the engine's texts name fields and endpoints as written


def field_text(name):
    """BUILD IC-8 (D12): a data field's name as the engine's texts print it — in a reader's words (REG.field_words: servedAges →
    "served ages"); within names_mode(raw=True), as written."""
    return str(name) if _NAMES_RAW[0] else _REG.field_words(name)


def endpoint_text(ep):
    """BUILD IC-8 (D12): an endpoint as the engine's texts print it (REG.endpoint_words); within names_mode(raw=True), as written."""
    return str(ep) if _NAMES_RAW[0] else _REG.endpoint_words(ep)


def alert_text(ep, aid, payloads=None):
    """BUILD IC-8 (D12): an alert as the engine's texts name it — its record's name (REG.record_name) and its endpoint's words; within
    names_mode(raw=True), IC-7's "the alert <id> (<endpoint>)"."""
    if _NAMES_RAW[0]:
        return f"the alert {aid} ({ep})"
    rec = next((a for a in (NR.records((payloads or {}).get(ep)) or []) if isinstance(a, dict) and str(a.get("id", "")) == str(aid)), None)
    return f"the alert {_REG.record_name(rec, ep) if rec is not None else aid} ({endpoint_text(ep)})"


def fields_in_words(text, fields):
    """BUILD IC-8 (D12): a text of the spec's (a derived threshold's why) with each coded field name of the data in words (field_text) —
    each matched exactly, the longest first; within names_mode(raw=True), as written."""
    fs = sorted({f for f in fields or () if isinstance(f, str) and f and not re.fullmatch(r"[a-z]+", f)}, key=len, reverse=True)
    if _NAMES_RAW[0] or not isinstance(text, str) or not fs:
        return text
    return re.sub(r"(?<![\w-])(" + "|".join(re.escape(f) for f in fs) + r")(?![\w-])", lambda m: field_text(m.group(1)), text)


@contextlib.contextmanager
def names_mode(raw=False):
    """BUILD IC-8 (F29): with raw, the engine's texts name every field, endpoint and alert as written — the rationale's request with the
    switch off is IC-7's, byte for byte; the pages always read the words."""
    old = _NAMES_RAW[0]
    _NAMES_RAW[0] = bool(raw)
    try:
        yield
    finally:
        _NAMES_RAW[0] = old

SCHEMA_VERSION = 1
SPEC_SUFFIX = ".engine.json"
TOP_N = 3                       # the worked cases show each set's top 3 (D4)
GATE_RULES = ("in_set", "age_range", "free_above_zero", "open_now", "bool")
FACTOR_FORMS = ("bool", "ratio", "inverse", "match")
BASES = ("stated", "assumed", "illustrative")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# ═══ D5: copied verbatim from architect_harness.py (t_engine_b1 asserts each copy equals the Architect's by AST; build 2 moves the
# Architect onto these) ═══
ALERT_TITLE_KEYS = ("headline", "title", "name")


ALERT_DETAIL_KEYS = ("summary", "description")


def _first_text(rec: Dict[str, Any], keys) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


STATUS_KEYS = ("status", "operatingstatus", "facilitystatus")


CLOSURE_WORDS = ('closed', 'offline', 'shut down', 'suspended', 'closure', 'not operating', 'shuttered', 'out of service')


DATA_CLOSED = set(CLOSURE_WORDS) | {"permanently closed", "inactive"}


AT_RISK = {"at risk"}


def _norm_status(v) -> str:
    return " ".join(v.lower().replace("_", " ").replace("-", " ").split()) if isinstance(v, str) else ""


def record_status(rec: Dict[str, Any]):
    """D6: (status, closed_by, closed_reason, at_risk) from a record's own status — a key named status, operatingStatus or
    facilityStatus (case-insensitive) whose value, lower-cased with '_' and '-' read as spaces, is in the closure vocabulary (the
    matcher's eight words + "permanently closed", "inactive") → closed by the data; "at risk" → open and tagged; anything else open."""
    for k, v in rec.items():
        if str(k).lower() in STATUS_KEYS:
            s = _norm_status(v)
            if s in DATA_CLOSED:
                return "closed", "data", f"the data: status {v}", False
            if s in AT_RISK:
                return "open", None, None, True
    return "open", None, None, False


_JOIN_END = re.compile(r"(?:[,(/&–—-]|\b(?:a|an|the|and|or|of|by|to|in|at|for|with|from|on|is|are|was|were|as))\s*$", re.I)


def _item_lines(text: str) -> str:
    """Review F2: the announcement as the matcher's segments read it. An announcement typed one item per line (D2's text file) has no
    sentence end between its lines, so a line that starts a new item — an upper-case letter, a digit or a bullet, after a blank line or
    after a line that does not end mid-phrase (a comma, a dash, 'by', 'of', 'the' …) — is joined with '. ', which the splitter reads as a
    sentence end; any other line break is a wrapped line, joined with a space, so it segments as its one-line text does. Text without
    a line break is returned as it is (D6: the matching itself unchanged)."""
    if "\n" not in (text or ""):
        return text or ""
    out, blank = "", False
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.strip()
        if not s:
            blank = bool(out)
            continue
        if not out:
            out = s
        elif blank or (re.match(r"[A-Z0-9•*–—-]", s) and not _JOIN_END.search(out)):
            out = out.rstrip() + ". " + s
        else:
            out = out.rstrip() + " " + s
        blank = False
    return out


def apply_curveball(resources: List[Dict], curveball_text: str) -> Tuple[List[Dict], str]:
    """
    Apply curveball to resource data. Only marks a resource as closed if BOTH
    the resource name AND a closure keyword (closed, offline, shut down, etc.)
    appear in the SAME segment of the curveball text. This prevents false
    positives where a resource is merely mentioned (e.g., "211 Kansas overloaded")
    but not actually closed.
    Returns (modified_resources, alert_html).
    """
    if not curveball_text:
        return resources, ""

    closed_names = []
    # BUILD IC-5 (D5): the one status decision (status_decisions) — closed_now closes the record; scheduled and conditional put a note on
    # it (status_note), never a closure; every other state changes nothing (IC-4's rule here was two words of a name as a substring of a
    # segment holding a closure word as a substring)
    dec = status_decisions({i: r["name"] for i, r in enumerate(resources) if r.get("status") != "closed" and isinstance(r.get("name"), str)},
                           curveball_text, among={i: r["name"] for i, r in enumerate(resources) if isinstance(r.get("name"), str)})
    # BUILD IC-5 (D5b): every resource's name is read (one the data closed still holds its name); the open ones are decided
    for i, d in sorted(dec.items()):
        r = resources[i]
        if d["state"] == "closed_now":
            r["status"] = "closed"
            r["closed_by"] = "announcement"   # P0.7 (D6): only these closures are called the curveball's
            r["closed_reason"] = curveball_text
            if r["name"] not in closed_names:
                closed_names.append(r["name"])
        elif status_note(d):
            r["status_note"] = status_note(d) + f": “{quote_of(d['sentence'])}”"

    # P0.7 (D11): the names and the announcement are text — escaped here, because the template prints this alert as markup
    alert_html = ""
    if closed_names:
        alert_html = (
            f"<strong>⚠ Update:</strong> {_html.escape(', '.join(closed_names))} "
            f"{'has' if len(closed_names) == 1 else 'have'} been marked as closed by the announcement. "
            f"Affected resources are excluded from default results. "
            f"Toggle 'Show closed' to view them."
        )
    else:
        # Generic curveball alert if no specific resource was identified
        alert_html = f"<strong>⚠ Update:</strong> {_html.escape(curveball_text[:200])}"

    return resources, alert_html


# ═══ small helpers ═══
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _norm(s) -> str:
    return " ".join(str(s).casefold().split())


def _vals(v):
    """A field's value as a list of normalized strings (a string → one; a list → its strings); None when it is neither."""
    if isinstance(v, str):
        return [_norm(v)]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return [_norm(x) for x in v]
    return None


def _show(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "none"
    if _num(v):
        return f"{v:,}" if isinstance(v, int) else f"{v:g}"
    return "not given" if v is None else str(v)


def tenths(x: float) -> int:
    """Points in tenths, half up (never banker's rounding): the page shows tenths/10, and the score is the sum of the tenths."""
    return int(math.floor(x * 10 + 0.5 + 1e-9))


def pts(t: int) -> str:
    return f"{t // 10}.{t % 10}"


# ═══ age ranges and hours ═══
def age_bounds(v):
    """'18+' → (18, 200); '18-24' → (18, 24); 'all' / 'any' → (0, 200); 'under 18' → (0, 17); '65 and over' → (65, 200); an int → (n, n);
    anything else → None."""
    if _num(v):
        return (int(v), int(v))
    if not isinstance(v, str):
        return None
    s = _norm(v)
    if s in ("all", "any", "all ages", "any age"):
        return (0, 200)
    m = re.fullmatch(r"(\d{1,3})\s*\+", s) or re.fullmatch(r"(\d{1,3})\s*(?:and|&)\s*(?:over|older|up)", s)
    if m:
        return (int(m.group(1)), 200)
    m = re.fullmatch(r"(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})", s)
    if m and int(m.group(1)) <= int(m.group(2)):
        return (int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(?:under|below)\s*(\d{1,3})", s)
    if m:
        return (0, int(m.group(1)) - 1)
    return None


DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?"
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?"
_RANGE = re.compile(_TIME + r"\s*(?:-|–|—|to)\s*" + _TIME)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _days(s):
    s = s.strip(" :")
    if not s or s in ("daily", "every day", "7 days", "7 days a week"):
        return set(range(7))
    if s in ("weekdays", "weekday"):
        return {0, 1, 2, 3, 4}
    if s in ("weekends", "weekend"):
        return {5, 6}
    out = set()
    for part in re.split(r"/|&|\band\b", s):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(_DAY + r"\s*(?:-|–|—|to)\s*" + _DAY, part)
        if m:
            a, b = DAYS[m.group(1)], DAYS[m.group(2)]
            out |= set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
            continue
        m = re.fullmatch(_DAY, part)
        if m:
            out.add(DAYS[m.group(1)])
            continue
        return None
    return out


def _mins(h, m, ap):
    h, m = int(h), int(m or 0)
    if not (1 <= h <= 12 and 0 <= m < 60):
        return None
    return (h % 12 + (12 if ap == "p" else 0)) * 60 + m


def parse_hours(text):
    """('always', []) | ('rules', [(days, start minute, end minute)]) | (None, why). '24/7', 'Mon-Fri 9:00 AM - 4:00 PM',
    'Mon-Fri 8a-7p, Sat 8a-1p', 'Tue/Thu 10am-2pm', 'Daily 7a-7p'. Never raises."""
    if not isinstance(text, str) or not text.strip():
        return None, "no hours given"
    low = _norm(text)
    if re.fullmatch(r"(?:open\s*)?(?:24\s*/\s*7|24\s*hours?(?:\s*a\s*day)?(?:,?\s*7\s*days(?:\s*a\s*week)?)?|always open)", low):
        return "always", []
    rules = []
    for seg in re.split(r"[;,]", low):
        seg = seg.strip()
        if not seg:
            continue
        m = _RANGE.search(seg)
        if not m:
            return None, f"no time range in \"{seg}\""
        days = _days(seg[:m.start()])
        a, b = _mins(m.group(1), m.group(2), m.group(3)), _mins(m.group(4), m.group(5), m.group(6))
        if days is None or a is None or b is None or seg[m.end():].strip():
            return None, f"\"{seg}\" not read"
        rules.append((days, a, b))
    return ("rules", rules) if rules else (None, "no hours given")


def open_at(text, when: datetime):
    """(True | False | None, detail) for an hours field at `when`."""
    kind, rules = parse_hours(text)
    if kind is None:
        return None, f"hours not read: \"{text}\"" if isinstance(text, str) and text.strip() else "no hours given"
    if kind == "always":
        return True, f"open 24/7 (\"{text}\")"
    wd, t = when.weekday(), when.hour * 60 + when.minute
    for days, a, b in rules:
        if (a < b and wd in days and a <= t < b) or (a >= b and ((wd in days and t >= a) or ((wd - 1) % 7 in days and t < b))):
            return True, f"open at {DAY_NAMES[wd]} {when:%H:%M} (\"{text}\")"
    return False, f"closed at {DAY_NAMES[wd]} {when:%H:%M} (\"{text}\")"


def as_of(env=None):
    """(datetime, its source, problem or None): NAIPC_AS_OF (ISO; a time with a zone, e.g. ...Z, is turned into local time) when set and
    readable, else the run's clock (local). An unreadable NAIPC_AS_OF is named in the problem, never dropped silently."""
    env = os.environ if env is None else env
    v = (env.get("NAIPC_AS_OF") or "").strip()
    now = datetime.now().replace(second=0, microsecond=0)
    if v:
        try:
            d = datetime.fromisoformat(v)
            return (d.astimezone().replace(tzinfo=None) if d.tzinfo is not None else d), "NAIPC_AS_OF", None
        except ValueError:
            return now, "the run's clock", f"NAIPC_AS_OF {v!r} is not an ISO time — the run's clock is used"
    return now, "the run's clock", None


# ═══ the sidecar ═══
def spec_path_for(contract_path) -> Path:
    p = Path(contract_path)
    return p.with_name(p.stem + SPEC_SUFFIX)


def load_spec(contract_path):
    """(spec, None) or (None, why) — the sidecar beside the contract. Never raises."""
    if not contract_path:
        return None, "no contract (NAIPC_REQUIREMENTS not set)"
    p = spec_path_for(contract_path)
    if not p.is_file():
        return None, f"no engine sidecar ({p.name}) beside the contract"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, f"{p.name} is not readable JSON ({type(e).__name__})"
    return (d, None) if isinstance(d, dict) else (None, f"{p.name}: the top level must be an object")


def _field_values(recs, field, ann=None):
    vals = []
    for r in recs:
        a = (ann or {}).get(str(r.get("id")), {}).get(field)
        if a is not None:
            vals.append(a["value"])
        elif field in r:
            vals.append(r[field])
    return vals


TYPE_OK = {"str": lambda v: isinstance(v, str), "num": _num, "bool": lambda v: isinstance(v, bool),
           "strs": lambda v: _vals(v) is not None, "num_or_list": lambda v: _num(v) or isinstance(v, list)}
TYPE_WORDS = dict(str="text", num="a number", bool="true/false", strs="text or a list of text", num_or_list="a number or a list")


def _need(P, where, recs, field, kind, ann=None, soft=None, was=None):
    """A field a rule reads must be given by at least one record or annotation, with the type the rule needs in at least one — a
    misspelt field is a problem even where the rule lets a record without it pass (review: if_missing must not hide a typo).
    B2 (review): `soft` is given only at Block 2 — there a field no record holds now can be a record gone at the curveball (the
    sidecar was written from Block 1's data, where the same field held by none can only be a typo and still stops the set): it is
    named in `soft` and the set runs, every record read as not giving the field (a gate: its own reading of a missing value — pass
    under if_missing "pass", otherwise to confirm; a factor: 0). `was` (field → bool), when Block 1's data is at hand, tells the
    two apart exactly: a field Block 1's records never held is a typo and stops the set at Block 2 too."""
    if not isinstance(field, str) or not field:
        P.append(f"{where}: a field name is required")
        return
    vals = _field_values(recs, field, ann)
    if not vals:
        msg = f"{where}: no record has {field}" + (" and no annotation gives it" if ann is not None else "")
        if soft is not None and (was is None or was(field)):
            soft.append(msg + " now (Block 2) — every record read as not giving it")
        elif soft is not None:                # B2: Block 1's data held it nowhere either — a typo, at Block 2 too
            P.append(msg + " — nor did any at Block 1 (a typo?)")
        else:
            P.append(msg)
        return
    if not any(TYPE_OK[kind](v) for v in vals):
        P.append(f"{where}: {field} is not {TYPE_WORDS[kind]} in any record")


def _users(contract):
    return {u.get("id"): u for u in (contract or {}).get("named_users") or [] if isinstance(u, dict) and isinstance(u.get("id"), str)}


def quote_in(q, text) -> bool:
    """The record's (or the contract's) own words: the whole field, or at least two words found whole in it (a lone letter never)."""
    if not (isinstance(q, str) and q.strip() and isinstance(text, str)):
        return False
    nq, nt = _norm(q), _norm(text)
    return nq == nt or (len(nq.split()) >= 2 and re.search(r"(?<!\w)" + re.escape(nq) + r"(?!\w)", nt) is not None)


def _read_fields(s):
    """Every record field a set's rules read (gates, factors, its check field)."""
    out = set()
    for x in list(s.get("gates") or []) + list(s.get("factors") or []):
        if isinstance(x, dict):
            out |= {v for v in [x.get("field"), x.get("range_top")] + list(x.get("fields") or []) if isinstance(v, str)}
    return out


def _own_words(recs, rid, items, w, P, what, gone=None, base_ids=None):
    """Annotations or exclusions keyed by record id → (good {rid: {key: entry}}, stale {rid: [note]}). A missing record or a malformed
    entry is a problem (the set does not run); a quote that is no longer the record's own words holds that record to confirm (review:
    one changed sentence at the curveball must not drop the whole set, nor rank the record on a reading its words no longer support)."""
    good, stale = {}, {}
    if not isinstance(items, dict):
        P.append(f"{w}.{what}: an object keyed by record id")
        return good, stale
    for rid, entry in items.items():
        rec = next((r for r in recs if str(r.get("id")) == str(rid)), None)
        if rec is None:
            if gone is not None and (base_ids is None or str(rid) in base_ids):   # B2 (review): a record gone at the curveball
                gone.append(f"{w}.{what}.{rid}: no record has this id now — not applied")
            elif gone is not None:            # B2: Block 1's data never held it either — a typo
                P.append(f"{w}.{what}.{rid}: no record has this id — nor did any at Block 1 (a typo?)")
            else:
                P.append(f"{w}.{what}.{rid}: no record has this id")
            continue
        own = " ".join([v for v in rec.values() if isinstance(v, str)]            # B2: a list of text is the record's own words too
                       + [x for v in rec.values() if isinstance(v, list) for x in v if isinstance(x, str)])
        pairs = entry.items() if what == "annotations" and isinstance(entry, dict) else [("exclude", entry)]
        for f, a in pairs:
            if not isinstance(a, dict) or not isinstance(a.get("quote"), str) or ("value" not in a if what == "annotations" else not isinstance(a.get("why"), str)):
                P.append(f"{w}.{what}.{rid}" + (f".{f}" if what == "annotations" else "") + ": {quote, " + ("value}" if what == "annotations" else "why}"))
            elif not quote_in(a["quote"], own):
                stale.setdefault(str(rid), []).append(f"the record no longer says \"{a['quote']}\"")
            else:
                good.setdefault(str(rid), {})[f] = a
    return good, stale


def _validate_set(s, i, payloads, P, seen, block2=False, base=None):
    w, p0 = f"option_sets[{i}]", len(P)
    if not isinstance(s, dict):
        P.append(f"{w}: must be an object")
        return None
    sid = s.get("id")
    if not (isinstance(sid, str) and SLUG.match(sid)) or sid in seen:
        P.append(f"{w}.id: a unique lowercase slug is required, got {sid!r}")
    elif isinstance(sid, str):
        seen.add(sid)
    ep = s.get("endpoint")
    if not isinstance(ep, str) or ep not in (payloads or {}):
        P.append(f"{w}.endpoint: {ep!r} is not among this run's data ({', '.join(sorted(payloads or {})) or 'none'})")
        return None
    recs = [r for r in NR.records(payloads[ep]) if isinstance(r, dict)]
    if not recs:
        P.append(f"{w}.endpoint: {ep} has no records")
        return None
    for k in ("gates", "factors", "tie_breakers"):
        if not isinstance(s.get(k) or [], list):
            P.append(f"{w}.{k}: a list")
            return None
    gone = [] if block2 else None      # B2 (review): what the data no longer holds is named only at Block 2; at Block 1 it is a typo (B1's)
    b1 = [r for r in NR.records(base[ep]) if isinstance(r, dict)] if (block2 and isinstance(base, dict) and ep in base) else None
    base_ids = {str(r.get("id")) for r in b1} if b1 is not None else None
    was = (lambda f: _held_at(b1, f, s)) if b1 is not None else None
    ann, stale = _own_words(recs, None, s.get("annotations") or {}, w, P, "annotations", gone, base_ids)
    excl, stale2 = _own_words(recs, None, s.get("exclude") or {}, w, P, "exclude", gone, base_ids)
    for rid, notes in stale2.items():
        stale.setdefault(rid, []).extend(notes)
    read = _read_fields(s)
    for rid, fs in ann.items():
        for f in fs:
            if f not in read:
                P.append(f"{w}.annotations.{rid}.{f}: no gate or factor of this set reads {f}")
    _need(P, f"{w}.name_field", recs, s.get("name_field"), "str")
    if s.get("check_field") is not None:
        _need(P, f"{w}.check_field", recs, s.get("check_field"), "str")
    ref = s.get("referral")
    if ref is not None and not (isinstance(ref, dict) and isinstance(ref.get("field"), str) and isinstance(ref.get("is"), bool)):
        P.append(f"{w}.referral: {{field: <a yes/no field>, is: true | false}}")
    pg = s.get("pages")                                                                          # B2: the pages a set is shown on
    if pg is not None and not (isinstance(pg, list) and pg and all(x in PAGES for x in pg)):
        P.append(f"{w}.pages: a list of {', '.join(PAGES)}")
    ct = s.get("contact")                                                                        # B2: the plan's phone, hours, address
    if ct is not None:
        if not isinstance(ct, dict):
            P.append(f"{w}.contact: an object, e.g. {{phone: <field>, hours: <field>}}")
        else:
            for k, f in ct.items():
                if not isinstance(f, str) or not any(f in r for r in recs):
                    P.append(f"{w}.contact.{k}: no record has {f!r}")
    sh = s.get("show")                                                                           # B2: record fields the page shows as they are
    if sh is not None and not (isinstance(sh, list) and all(isinstance(f, str) and any(f in r for r in recs) for f in sh)):
        P.append(f"{w}.show: a list of fields its records hold")
    for j, g in enumerate(s.get("gates") or []):
        wg = f"{w}.gates[{j}]"
        rule = g.get("rule") if isinstance(g, dict) else None
        if rule not in GATE_RULES:
            P.append(f"{wg}.rule: one of {', '.join(GATE_RULES)}, got {rule!r}")
            continue
        if "when" in g and not (isinstance(g["when"], dict) and isinstance(g["when"].get("attr"), str) and "is" in g["when"]):
            P.append(f"{wg}.when: {{attr: <the person's attribute>, is: <its value>}}")
        if rule == "in_set":
            _need(P, wg, recs, g.get("field"), "strs", ann, gone, was)
            av = g.get("any_values")
            if av is not None and not (isinstance(av, list) and all(isinstance(x, str) for x in av)):
                P.append(f"{wg}.any_values: a list of text, e.g. [\"all\"]")
        elif rule in ("age_range", "open_now"):
            _need(P, wg, recs, g.get("field"), "str", soft=gone, was=was)
        elif rule == "bool":
            _need(P, wg, recs, g.get("field"), "bool", soft=gone, was=was)
            if not isinstance(g.get("want"), bool):
                P.append(f"{wg}.want: true or false")
        else:
            fs = g.get("fields")
            if not (isinstance(fs, list) and 1 <= len(fs) <= 2):
                P.append(f"{wg}.fields: one or two numeric fields")
            else:
                for f in fs:
                    _need(P, wg, recs, f, "num", soft=gone, was=was)
        if rule in ("in_set", "age_range") and not isinstance(g.get("attr"), str):
            P.append(f"{wg}.attr: the person's attribute it reads is required")
    holds, held_stale = _holds(recs, s.get("holds"), w, P, gone, {g.get("id") for g in s.get("gates") or [] if isinstance(g, dict)}, base_ids)
    ph = _placeholders(s, w, P)
    total, fids = 0.0, set()
    for j, f in enumerate(s.get("factors") or []):
        wf = f"{w}.factors[{j}]"
        form = f.get("form") if isinstance(f, dict) else None
        if form not in FACTOR_FORMS:
            P.append(f"{wf}.form: one of {', '.join(FACTOR_FORMS)}, got {form!r}")
            continue
        if not (isinstance(f.get("id"), str) and SLUG.match(f["id"])) or f.get("id") in fids:
            P.append(f"{wf}.id: a unique lowercase slug is required")
        elif isinstance(f.get("id"), str):
            fids.add(f["id"])
        mx = f.get("max")
        if not (_num(mx) and mx > 0):
            P.append(f"{wf}.max: a number above 0")
        else:
            total += mx
        if not (isinstance(f.get("why"), str) and f["why"].strip()):
            P.append(f"{wf}.why: one line saying why this factor counts")
        if form == "bool":
            _need(P, wf, recs, f.get("field"), "bool", soft=gone, was=was)
            if not isinstance(f.get("want"), bool):
                P.append(f"{wf}.want: true or false")
        elif form == "match":
            _need(P, wf, recs, f.get("field"), "strs", ann, gone, was)
            if not isinstance(f.get("attr"), str):
                P.append(f"{wf}.attr: the person's attribute it matches is required")
        else:
            if not (_num(f.get("ref")) and f["ref"] > 0):
                P.append(f"{wf}.ref: a reference number above 0")
            if f.get("range_top"):
                _need(P, wf, recs, f.get("range_top"), "str", soft=gone, was=was)
                if not isinstance(f.get("minus_attr"), str):
                    P.append(f"{wf}.minus_attr: the person's attribute subtracted from the range's top")
            else:
                fs = f.get("fields") or ([f.get("field")] if f.get("field") else [])
                if not (isinstance(fs, list) and 1 <= len(fs) <= 2):
                    P.append(f"{wf}: one or two numeric fields (field, or fields)")
                else:
                    for x in fs:
                        _need(P, wf, recs, x, "num_or_list" if len(fs) == 1 else "num", soft=gone, was=was)
    if abs(total - 100) > 1e-9:
        P.append(f"{w}.factors: the max points must sum to 100, got {total:g}")
    for t in s.get("tie_breakers") or []:
        if t not in fids:
            P.append(f"{w}.tie_breakers: {t!r} is not a factor of this set")
    if len(P) > p0:
        return None
    P.extend(gone or [])
    P.extend(held_stale)
    for rid, notes in stale.items():           # a stale reading: named in the problems, the record held to confirm, the set runs
        P.append(f"{w}: record {rid} held to confirm — " + "; ".join(notes))
    return {"spec": s, "recs": recs, "ann": ann, "excl": excl, "stale": stale, "holds": holds, "ph": ph}


PH_READS = ("no cap", "not given")


def _placeholders(s, w, P):
    """B2: {field: {value, reads_as, why}} — a value the data uses as a placeholder, not a reading (KanCare Extension's slotsAvailable
    999). "not given": read as missing by every rule; "no cap": a capacity with no limit — only as the capacity a free_above_zero gate or
    a ratio factor reads first (it passes; full points). The page shows the value, how it is read and why."""
    ph = s.get("placeholders")
    if ph is None:
        return {}
    if not isinstance(ph, dict):
        P.append(f"{w}.placeholders: an object keyed by field")
        return {}
    caps = {(g.get("fields") or [None])[0] for g in s.get("gates") or [] if isinstance(g, dict) and g.get("rule") == "free_above_zero"}
    caps |= {((f.get("fields") or [f.get("field")]) or [None])[0] for f in s.get("factors") or []
             if isinstance(f, dict) and f.get("form") == "ratio" and not f.get("range_top")}
    out = {}
    for fld, e in ph.items():
        wp = f"{w}.placeholders.{fld}"
        if not (isinstance(e, dict) and (_num(e.get("value")) or isinstance(e.get("value"), str)) and e.get("reads_as") in PH_READS
                and isinstance(e.get("why"), str) and e["why"].strip()):
            P.append(f"{wp}: {{value: <a number or text>, reads_as: {' | '.join(PH_READS)}, why: <the reason, shown on the page>}}")
        elif fld not in _read_fields(s):
            P.append(f"{wp}: no gate or factor of this set reads {fld}")
        elif e["reads_as"] == "no cap" and fld not in caps:
            P.append(f"{wp}: \"no cap\" is read only as the capacity of a free_above_zero gate or a ratio factor (their first field)")
        else:
            out[fld] = {"value": e["value"], "reads_as": e["reads_as"], "why": e["why"]}
    return out


def _ph_hits(ph, r):
    """{field: {reads_as, why, v}} for the record's fields that hold a declared placeholder value."""
    out = {}
    for fld, e in (ph or {}).items():
        v = r.get(fld)
        if fld in r and not isinstance(v, bool) and type(v) in (int, float, str) and (v == e["value"] if (_num(v) and _num(e["value"]))
                                                                                         else v == e["value"] and isinstance(v, str)):
            out[fld] = {"reads_as": e["reads_as"], "why": e["why"], "v": v}
    return out


def _ph_view(r, hits):
    """The record as the rules read it: a "not given" placeholder is missing."""
    return {k: v for k, v in r.items() if not (k in hits and hits[k]["reads_as"] == "not given")} if hits else r


def _no_cap(fld, h):
    return f"{field_text(fld)} {_show(h['v'])} in the data, read as no cap — assumed: {h['why']}"


def _held_at(b1, field, s):
    """B2: whether Block 1's records of this endpoint held a field — in a record, or by an annotation of a record Block 1 held."""
    ids = {str(r.get("id")) for r in b1}
    return any(field in r for r in b1) or any(isinstance(e, dict) and field in e and str(rid) in ids for rid, e in (s.get("annotations") or {}).items())


def _holds(recs, items, w, P, gone, gate_ids, base_ids=None):
    """B2: {record id: {gate id: {quote, why}}} — a record whose own words offer what a gate checks (unit-004: "NEXTenant contributes
    deposit assistance") is to confirm where that gate would put it out, never a pass. Returns (good, notes). A malformed entry or an
    unknown gate id stops the set (a problem); a record gone is named at Block 2 (a typo at Block 1); a quote the record no longer
    holds is named and not applied (the gate decides as written)."""
    good, notes = {}, []
    if items is None:
        return good, notes
    if not isinstance(items, dict):
        P.append(f"{w}.holds: an object keyed by record id")
        return good, notes
    for rid, entry in items.items():
        rec = next((r for r in recs if str(r.get("id")) == str(rid)), None)
        if not isinstance(entry, dict) or not entry:
            P.append(f"{w}.holds.{rid}: {{<gate id>: {{quote, why}}}}")
            continue
        for gid, h in entry.items():
            if gid not in gate_ids:
                P.append(f"{w}.holds.{rid}.{gid}: no gate of this set has the id {gid!r}")
            elif not (isinstance(h, dict) and isinstance(h.get("quote"), str) and h["quote"].strip() and isinstance(h.get("why"), str) and h["why"].strip()):
                P.append(f"{w}.holds.{rid}.{gid}: {{quote, why}}")
            elif rec is None:
                if gone is not None and (base_ids is None or str(rid) in base_ids):
                    gone.append(f"{w}.holds.{rid}: no record has this id now — not applied")
                elif gone is not None:
                    P.append(f"{w}.holds.{rid}: no record has this id — nor did any at Block 1 (a typo?)")
                else:
                    P.append(f"{w}.holds.{rid}: no record has this id")
            else:
                own = " ".join([v for v in rec.values() if isinstance(v, str)] + [x for v in rec.values() if isinstance(v, list) for x in v if isinstance(x, str)])
                if quote_in(h["quote"], own):
                    good.setdefault(str(rid), {})[gid] = {"quote": h["quote"], "why": h["why"]}
                else:
                    notes.append(f"{w}.holds.{rid}.{gid}: the record no longer says \"{h['quote']}\" — not applied")
    return good, notes


def held(g, h, o, d):
    """A gate's outcome with the record's hold: a fail becomes to confirm, quoting the record (the same words in naipc_engine.js)."""
    if o == "fail" and h:
        return "confirm", f"{d}; the record's own words: \"{h['quote']}\" — {h['why']}"
    return o, d


def _validate_preset(pr, i, users, P, pids, served):
    w, p0 = f"presets[{i}]", len(P)
    if not isinstance(pr, dict):
        P.append(f"{w}: must be an object")
        return None
    pid, nu = pr.get("id"), pr.get("named_user")
    if not (isinstance(pid, str) and SLUG.match(pid)) or pid in pids:
        P.append(f"{w}.id: a unique lowercase slug is required, got {pid!r}")
    elif isinstance(pid, str):
        pids.add(pid)
    if nu is not None and not isinstance(nu, str):
        P.append(f"{w}.named_user: a named user's id or null")
        return None
    u = users.get(nu) if nu is not None else None
    if nu is not None and u is None:
        P.append(f"{w}.named_user: {nu!r} is not a named user of the contract")
    if nu is not None and nu in served:
        P.append(f"{w}.named_user: {nu} already has a preset (one per named user)")
    if nu is not None:
        served.add(nu)
    if nu is None and pr.get("illustrative") is not True:
        P.append(f"{w}: a preset with no named user must say \"illustrative\": true")
    if nu is None and users:
        P.append(f"{w}: an illustrative preset only when the brief names no users (it names {', '.join(users)})")
    attrs = pr.get("attrs")
    if not isinstance(attrs, dict) or not attrs:
        P.append(f"{w}.attrs: an object of the person's attributes")
        attrs = {}
    for a, spec_a in attrs.items():
        wa = f"{w}.attrs.{a}"
        if not isinstance(spec_a, dict) or "value" not in spec_a or spec_a.get("basis") not in BASES:
            P.append(f"{wa}: {{value, basis: stated | assumed | illustrative}}")
            continue
        if spec_a["value"] is None:
            P.append(f"{wa}: a value is required — an unknown attribute is left out, never null")
            continue
        b = spec_a["basis"]
        if b == "illustrative" and nu is not None:
            P.append(f"{wa}: \"illustrative\" is only for a preset with no named user")
        elif b == "assumed" and not (isinstance(spec_a.get("reason"), str) and spec_a["reason"].strip()):
            P.append(f"{wa}: an assumed attribute needs its reason")
        elif b == "stated":
            frm = spec_a.get("from")
            if u is None:
                P.append(f"{wa}: \"stated\" needs the named user it is stated for")
            elif frm == "age":
                if a != "age":
                    P.append(f"{wa}: only the age itself is stated from the age — a value derived from it is assumed, with its reason")
                elif u.get("age") is None or spec_a["value"] != u.get("age"):
                    P.append(f"{wa}: the contract's age for {nu} is {u.get('age')}, not {spec_a['value']!r}")
            elif frm in ("role", "quote"):
                if not quote_in(spec_a.get("quote"), u.get(frm)):
                    P.append(f"{wa}: the quote must be the contract's own words — its whole {frm} for {nu}, or two words or more of it")
            else:
                P.append(f"{wa}.from: age, role or quote")
    return pr if len(P) == p0 else None


def _block2(announcement) -> bool:
    return isinstance(announcement, str) and bool(announcement.strip())


def validate_spec(spec, contract, payloads, block2=False, base=None):
    """Every problem as 'path: reason', and the parts that can run: (problems, sets, presets). A set or a preset with a problem does not
    run (the others do); a problem at the top runs nothing; a sidecar no rule can read is a problem, never a crash (review). B2: block2
    (the announcement given — the harnesses' own test, CURVEBALL_TEXT set) names, without stopping the set, an annotated record or a
    field the data no longer holds (_need); `base` (Block 1's payloads, when the harness has them) keeps a typo a typo at Block 2."""
    P = []
    if not isinstance(spec, dict):
        return ["engine: the sidecar's top level must be an object"], [], []
    if spec.get("schema_version") != SCHEMA_VERSION:
        P.append(f"schema_version: must be {SCHEMA_VERSION}, got {spec.get('schema_version')!r}")
    cid = ((contract or {}).get("challenge") or {}).get("id")
    if cid and spec.get("challenge") != cid:
        P.append(f"challenge: {spec.get('challenge')!r} differs from the contract's {cid!r}")
    for k in ("option_sets", "presets"):
        if not isinstance(spec.get(k) or [], list):
            P.append(f"{k}: a list")
    if P:
        return P, [], []
    sets, seen = [], set()
    for i, s in enumerate(spec.get("option_sets") or []):
        try:
            v = _validate_set(s, i, payloads, P, seen, block2, base)
        except Exception as e:  # noqa: BLE001
            P.append(f"option_sets[{i}]: not readable ({type(e).__name__}: {str(e)[:80]})")
            v = None
        if v:
            sets.append(v)
    users, presets, pids, served = _users(contract), [], set(), set()
    for i, pr in enumerate(spec.get("presets") or []):
        try:
            v = _validate_preset(pr, i, users, P, pids, served)
        except Exception as e:  # noqa: BLE001
            P.append(f"presets[{i}]: not readable ({type(e).__name__}: {str(e)[:80]})")
            v = None
        if v:
            presets.append(v)
    return P, sets, presets


# ═══ closures (D5) ═══
_SEG = r'\d+[.)]\s*|[.;!]\s+'          # the matcher's own split (apply_curveball)


def _frags(name) -> set:
    w = str(name).lower().split()
    return {f"{w[i]} {w[i + 1]}" for i in range(len(w) - 1)}


_NEGATION = re.compile(r"(?:\bno longer|\bnot|\bnever|\bisn['\u2019]t|\baren['\u2019]t|\bwasn['\u2019]t|\breopen\w*|\bback (?:open|online)|"
                       r"\blifted|\bended)\s+(?:\w+\s+){0,2}$", re.I)


_REOPEN_NEGATED = re.compile(r"(?:\bnot|\bnever|n['\u2019]t)\s+(?:\w+\s+)?$", re.I)
_PAST_CLOSURE = re.compile(r"\bafter\s+(?:being|having\s+been|(?:it|they)\s+(?:was|were|had\s+been))\s+$", re.I)


def _negates(low: str, at: int) -> bool:
    """BUILD IC-4 Phase 7 (review b2): whether the closure word at `at` of the lower-cased text is negated — "no longer closed", "is not
    offline", "reopened after being closed" (a past closure: "after being" just before it); a reopening that is itself negated ("has not
    reopened and remains closed", "never reopened") negates nothing."""
    w0 = max(0, at - 40)
    w = low[w0:at]
    if _PAST_CLOSURE.search(w):
        return True
    m = _NEGATION.search(w)
    if not m:
        return False
    if re.match(r"\s*(?:reopen|back\b|lifted|ended)", m.group(0)) and _REOPEN_NEGATED.search(low[max(0, w0 + m.start() - 24):w0 + m.start()]):
        return False
    return True


def _negated(seg: str, kw: str) -> bool:
    """BUILD IC-4 (D3): every place a closure word stands in the segment is negated ("no longer closed", "is not offline", "reopened after
    being closed" is not: the word stands after "reopened" only when nothing closes it again)."""
    low = seg.lower()
    at = [m.start() for m in re.finditer(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low)]
    return bool(at) and all(_negates(low, a) for a in at)      # IC-4 Phase 7 (review b2): the one negation rule


# ═══ BUILD IC-5 (D5): one status decision per record — status_decisions, read by every page (the Architect's apply_curveball, the
# engine's closures_by_text and closures; the Oracle's playbook through them). Generated from ic5_dev/d5/status_draft.py, the rules
# tests/fixtures/status_ic5.json was run on (ConText's shape: Harkema et al. 2009). ═══
NAME_ALIASES = {      # reviewed: a record's other name, as the data's own name states it (each read in the fixtures, 2026-11-14)
    "Second Light (formerly Multi-Agency Center)": ("Multi-Agency Center",),
    "UI Health (University of Illinois Hospital)": ("University of Illinois Hospital",)}
_S_STOP = {"of", "the", "at", "and", "for", "a", "an", "in", "on", "to", "&", "/", "-", "–", "—", "formerly"}
_S_TRIGGER = re.compile(r"\b(?:closed|closes|close|closing|closure|closures|offline|shut\s+down|shutting\s+down|shut|suspended|suspends|"
                     r"suspend|not\s+operating|out\s+of\s+service|shuttered)\b", re.I)
_S_PSEUDO = re.compile(r"\bclosed-door\b|\bclose\s+to\b|\bclose\s+the\s+gap\b|\bclose\s+contact\b", re.I)
_S_REOPEN = re.compile(r"\b(?:reopen|reopens|reopened|reopening)\b", re.I)
_S_OPEN_NOW = re.compile(r"\b(?:is|are|remains|remain|stays|stay)\s+open\b(?!\s+door)|\bopen\s+(?:today|again|now)\b", re.I)
_S_NEG = re.compile(r"\b(?:no\s+longer|not|never|isn't|is\s+not|wasn't|hasn't|has\s+not|have\s+not)\s+(?:been\s+|being\s+)?$", re.I)
_S_MODAL = re.compile(r"\b(?:may|might|could|would|if|expected\s+to|plans?\s+to|planning\s+to|likely\s+to|considering|possibly)\b", re.I)
_S_FUTURE = re.compile(r"\b(?:will|'ll|scheduled\s+to|going\s+to|starting|beginning|as\s+of\s+(?:tomorrow|monday|tuesday|wednesday|thursday|"
                    r"friday|saturday|sunday))\b", re.I)
_S_HABIT = re.compile(r"\b(?:daily|nightly|every\s+(?:day|night|evening)|each\s+(?:day|night)|on\s+(?:sundays|mondays|tuesdays|wednesdays|"
                   r"thursdays|fridays|saturdays|weekends))\b", re.I)
_S_TERMINATE = re.compile(r"\s*(?:;|\bbut\b|\bhowever\b|\bwhile\b|\bwhereas\b|\buntil\b)\s*", re.I)
_S_FUTURE_AFTER = re.compile(r"\b(?:tomorrow|next\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                          r"later\s+(?:today|this\s+week)|this\s+(?:weekend|coming))\b", re.I)     # after the closure word: still to come
_S_SOON_AFTER = re.compile(r"\b(?:tonight|today|this\s+(?:evening|afternoon))\b", re.I)   # still to come only after a form not past
# ("closes tonight"); "is closed today" and "offline … tonight" are now
_S_NOW_NOUN = re.compile(r"\b(?:is|are|was|were|has|have|had)\b|\beffective\s+(?:immediately|now|today)\b|\bimmediate(?:ly)?\b|"
                      r"\bannounc(?:ed|es)\b|\bin\s+effect\b", re.I)      # a closure noun said of now: a verb of state or an immediacy word
_S_NOT_PAST = re.compile(r"(?:close|closes|closing|suspend|suspends|shutting\s+down)$", re.I)   # a closure word in a form that is not past
_S_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:[AaPp]\.[Mm]\.|[AaPp][Mm]\b)"     # "5 PM", "5:30 pm", "5 p.m." (a sentence's stop after PM is not the time's)
_S_DAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:,\s+(?:January|February|March|April|May|June|July|August|" \
      r"September|October|November|December)\s+\d{1,2})?"
_S_TIME = re.compile(r"\bat\s+(" + _S_CLOCK + r")|\b(" + _S_DAY + r")\b")
_S_NAME_TOK = re.compile(r"[A-Za-z0-9]+(?:[.'’&-][A-Za-z0-9]+)*")    # the navigator's own short-name tokens (architect_harness._S_NAME_TOK)
_S_SHAPE = re.compile(r"\d|[A-Z][^A-Z]*[A-Z]")                     # a digit, or two capitals or more (architect_harness._S_SHAPE)
_ENTITY_WORD = re.compile(r"\b[A-Z][A-Z]{3,}\b")   # BUILD IC-5 (D5b): a word of four or more capitals (IC-4 D3's veto_forms rule, its one
# definition now): it names every record whose name holds it — one site under two names in two endpoints (Wichita's BRIDGES: the
# shelter's "BRIDGES (Wichita Children's Home)" and the program "BRIDGES Transitional Housing" of the agency "Wichita Children's Home")


def _s_sentences(text):
    """Sentences and numbered items — BUILD IC-8 (D1, one finder): the registry's split_sentences (never after an abbreviation, an
    initial or a numbered item's mark; never inside a token), each item's mark stripped ("2. Shelter B closes." → "Shelter B closes.");
    empty pieces removed."""
    out = [re.sub(r"^\d{1,3}[.)]\s+", "", p.strip()) for p in _REG.split_sentences(text or "")]
    return [p for p in out if p.strip()]


def _s_rx_name(s):
    """The pattern of a name as the data writes it: its words, any spaces between (none needed beside a mark such as "/"), either
    apostrophe; word boundaries outside."""
    toks = s.split()
    out = ""
    for i, w in enumerate(toks):
        if i:
            out += r"\s*" if not re.search(r"\w", toks[i - 1]) or not re.search(r"\w", w) else r"\s+"
        out += "".join("['’]" if ch in "'’" else re.escape(ch) for ch in w)
    return r"(?<![\w-])" + out + r"(?![\w-])"


def _s_as_written(s, how):
    """[(pattern, how)]: the name with its own capitals, and all in capitals (a heading)."""
    f = [(re.compile(_s_rx_name(s)), how)]
    if s.upper() != s:
        f.append((re.compile(_s_rx_name(s.upper())), how + " (in capitals)"))
    return f


def name_forms(names):
    """key → [(pattern, how)]: the whole name and the name before a parenthesis as the data writes them (its capitals, or all in
    capitals), each reviewed alias, each two-word piece with the name's own capitals that no other name holds, each one-word short name
    of a proper name's shape no other site holds, and each word of four or more capitals in the name (_ENTITY_WORD), whatever number of
    names hold it (BUILD IC-5 D5b: one site under two names, as the veto reads it)."""
    low = {k: str(n).lower() for k, n in names.items()}
    out, short_seen = {}, {}
    for k, n in names.items():
        n = str(n)
        f = _s_as_written(n, "its name")
        for al in NAME_ALIASES.get(n, ()):                       # a reviewed alias (NAME_ALIASES), as written
            f += _s_as_written(al, f"the reviewed alias {al!r}")
        head = re.split(r"\s*\(", n, 1)[0].strip()
        if head != n and len(head) >= 4:
            f += _s_as_written(head, "its name before the parenthesis")
        words = [w for w in re.findall(r"[^\s/]+", head)]
        for a, b in zip(words, words[1:]):
            if a.lower() in _S_STOP or b.lower() in _S_STOP:
                continue
            piece = f"{a} {b}"
            if not any(piece.lower() in low[o] for o in names if low[o] != low[k]) and piece.lower() != n.lower():   # a record of the same name (one site in two endpoints) is the same site
                f.append((re.compile(_s_rx_name(piece)), f"the piece {piece!r}"))
        for t in dict.fromkeys(_S_NAME_TOK.findall(head)):
            if _S_SHAPE.search(t):
                short_seen.setdefault(t, set()).add(k)
        out[k] = f
    for t, ks in short_seen.items():
        if len({low[x] for x in ks}) == 1:                      # one site's name (its records in two endpoints share it)
            for k in ks:
                out[k].append((re.compile(r"(?<![\w.'’-])" + re.escape(t) + r"(?![\w-]|\.\w)"), f"the short name {t!r}"))
    ent = {}
    for k, n in names.items():                   # BUILD IC-5 (D5b): a word in capitals names every record whose name holds it
        for t in dict.fromkeys(_ENTITY_WORD.findall(str(n))):
            ent.setdefault(t, []).append(k)
    for t, ks in ent.items():
        for k in ks:
            if not any(h == f"the short name {t!r}" for _rx, h in out[k]):
                out[k].append((re.compile(r"(?<![\w.'’-])" + re.escape(t) + r"(?![\w-]|\.\w)"),
                               f"the word in capitals {t!r}" + (" (one site under two names)" if len({low[x] for x in ks}) > 1 else "")))
    return out


def shared_pieces(names):
    """BUILD IC-5 (D5b): [(pattern, keys)] — each two-word piece (a name's own capitals), and each one-word token of a proper name's
    shape before a parenthesis (D5's short names), that two names or more hold (one site in two endpoints is one name): it names none of
    them, and a sentence that holds it may mean any (to confirm). A word of four or more capitals is not one: it names every holder
    (_ENTITY_WORD: one site under two names). BUILD IC-5 (D5c): nor is a piece that is a record's own name — its whole name, its name
    before a parenthesis, a reviewed alias: a name, read by the name rules ("Northside Hall" inside "Northside Hall Annex" names neither)."""
    low = {k: str(n).lower() for k, n in names.items()}
    own = set()
    for n in names.values():
        n = str(n)
        own |= {n.strip().lower(), re.split(r"\s*\(", n, 1)[0].strip().lower()} | {a.lower() for a in NAME_ALIASES.get(n, ())}
    held = {}
    for k, n in names.items():
        head = re.split(r"\s*\(", str(n), 1)[0].strip()
        words = re.findall(r"[^\s/]+", head)
        for a, b in zip(words, words[1:]):
            if a.lower() not in _S_STOP and b.lower() not in _S_STOP:
                held.setdefault(f"{a} {b}", set()).add(k)
        for t in dict.fromkeys(_S_NAME_TOK.findall(head)):
            if _S_SHAPE.search(t) and not _ENTITY_WORD.fullmatch(t):
                held.setdefault(t, set()).add(k)
    return [(re.compile(_s_rx_name(p) if " " in p else r"(?<![\w.'’-])" + re.escape(p) + r"(?![\w-]|\.\w)"), sorted(ks, key=str))
            for p, ks in held.items() if len({low[k] for k in ks}) > 1 and p.lower() not in own]


def names_in(clause, forms):
    """[(start, end, key, how)] — each record named in the clause; where names overlap, the longest span names it."""
    found = []
    for k, fs in forms.items():
        for rx, how in fs:
            for m in rx.finditer(clause):
                found.append((m.start(), m.end(), k, how))
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    keep = []
    for s, e, k, how in found:
        if any(s >= s2 and e <= e2 and (e2 - s2) > (e - s) for s2, e2, _k2, _h2 in found):
            continue                                            # inside a longer name ("Northside Hall" of "Northside Hall Annex")
        if any(k == k2 for _s, _e, k2, _h in keep):
            continue
        keep.append((s, e, k, how))
    return keep


def _s_time_after(clause, at):
    m = _S_TIME.search(clause, at)
    if m:
        return m.group(1) or m.group(2)
    m = _S_TIME.search(clause)                                     # "Starting Monday, …": the day before the trigger
    return (m.group(1) or m.group(2)) if m else None


def _s_state(clause, trig):
    """The state one closure word gives, read in its clause."""
    before = clause[:trig.start()]
    if _S_NEG.search(before) and not re.match(r"not\s+operating", trig.group(0), re.I):
        return "negated", None
    if _S_HABIT.search(clause):
        return "hours", None
    if trig.group(0).lower() in ("closure", "closures") and not _S_NOW_NOUN.search(clause) and not _S_FUTURE_AFTER.search(clause[trig.end():]):
        return "unresolved", None
    if _S_MODAL.search(before):
        return "conditional", None
    after = clause[trig.end():]
    if (_S_FUTURE.search(before) or _S_FUTURE_AFTER.search(after)
            or _S_NOT_PAST.match(trig.group(0)) and (re.search(r"\bat\s+" + _S_CLOCK, after) or _S_SOON_AFTER.search(after))):
        return "scheduled", _s_time_after(clause, trig.end())     # "will close", "closes at 5 PM" (not "closed at 9 AM": it happened)
    return "closed_now", None


def status_decisions(names, text, as_of=None, among=None):
    """key → {"state", "time"?, "sentence", "trigger"?, "named_by"}: one decision per record the text names with a status word; a
    record named with none, or not named, has no entry. BUILD IC-5 (D5b): `among` is every record's name the text may mean (the data's
    whole set, the closed records too): a name's forms, and whether another name holds a piece of it, are read from all of them; only
    `names`' records are decided."""
    pool = {**{k: n for k, n in (among or {}).items() if isinstance(n, str)}, **names}
    forms, shared = name_forms(pool), shared_pieces(pool)
    out = {}
    for sent in _s_sentences(_item_lines(text)):        # an announcement typed one item per line reads as its items
        if _S_PSEUDO.search(sent):
            sent_ = _S_PSEUDO.sub(lambda m: " " * len(m.group(0)), sent)
        else:
            sent_ = sent
        inside = [(m.start(), m.end()) for fs in forms.values() for rx, _h in fs for m in rx.finditer(sent_)]
        clauses, at = [], 0
        for m in _S_TERMINATE.finditer(sent_):                       # a terminator inside a name ("Until Dawn …") splits nothing
            if any(s_ < m.end() and m.start() < e_ for s_, e_ in inside):
                continue
            clauses.append(sent_[at:m.start()])
            at = m.end()
        clauses = [c for c in clauses + [sent_[at:]] if c.strip()]
        prev_keys = []
        for cl in clauses:
            named = names_in(cl, forms)
            keys = [k for _s, _e, k, _h in named] or prev_keys
            trigs = list(_S_TRIGGER.finditer(cl))
            reop = [m for m in _S_REOPEN.finditer(cl) if not _S_NEG.search(cl[:m.start()])]
            open_now = _S_OPEN_NOW.search(cl)
            if not named and not trigs and not reop:
                continue
            for k in keys:
                pos = next(((s, e) for s, e, k2, _h in named if k2 == k), None)
                mine = []
                for t in trigs:                                   # the closure words that belong to this record: the nearest name
                    if not named:
                        mine.append(t)
                        continue
                    left = [x for x in named if x[1] <= t.start()]
                    near = max(left, key=lambda x: x[1]) if left else min(named, key=lambda x: x[0])
                    if any(x[2] == k and (x[0], x[1]) == (near[0], near[1]) for x in named):
                        mine.append(t)                    # the word belongs to a span; every record that span names owns it
                dec = None
                if mine:
                    states = [_s_state(cl, t) + (t.group(0),) for t in mine]
                    st, tm, tw = next((s for s in states if s[0] != "closed_now"), states[0])
                    if reop and st == "closed_now":
                        fut = any(re.search(r"\b(?:will|'ll)\s+$", cl[:m.start()], re.I) or re.match(r"reopens\b", m.group(0), re.I) for m in reop)
                        st = "closed_now" if fut else "reopened"
                        tm = _s_time_after(cl, reop[0].end()) if st == "reopened" else None
                    if open_now and st == "closed_now" and re.search(r"\b(?:was|were)\b", cl[:mine[0].start()], re.I):
                        st, tm = "reopened", None
                    dec = {"state": st, **({"time": tm} if tm else {}), "trigger": tw}
                elif reop:
                    dec = {"state": "reopened", **({"time": _s_time_after(cl, reop[0].end())} if _s_time_after(cl, reop[0].end()) else {})}
                if dec is not None:
                    dec.update(sentence=sent, named_by=next((h for _s, _e, k2, h in named if k2 == k), "the clause before"))
                    if k not in out or out[k]["state"] != dec["state"]:     # BUILD IC-5 (D5b): the same state again keeps its first sentence
                        out[k] = dec
            if trigs:
                # BUILD IC-5 (D5b, D5c): a piece other names share and no record has for its own name — each sharing site the clause
                # does not name: to confirm, wherever it stands (B1's E-13: "Family Promise North closed tonight" flags South)
                for rx_, ks_ in shared:
                    if rx_.search(cl):
                        for k in ks_:
                            if k not in keys and k not in out:
                                out[k] = {"state": "unresolved", "trigger": trigs[0].group(0), "sentence": sent,
                                          "named_by": "a piece of its name that other sites' names share"}
            prev_keys = keys
    return {k: d for k, d in out.items() if k in names}       # BUILD IC-5 (D5b): every name read, only the records asked decided


def status_note(d) -> str:
    """BUILD IC-5 (D5): a scheduled or conditional decision's note on its record — "Scheduled to close at 5 PM, per the announcement"."""
    st_ = (d or {}).get("state")
    if st_ == "scheduled":
        if not d.get("time"):
            return "Scheduled to close, per the announcement"
        pre_ = "at " if re.match(r"\d", d["time"]) else "on "         # a clock time, or a day (an f-string holds no backslash: Python 3.11)
        return f"Scheduled to close {pre_}{d['time']}, per the announcement"
    if st_ == "conditional":
        return "May close, per the announcement"
    if st_ == "unresolved":         # BUILD IC-5 (D5b): a closure the text does not settle for this site (a shared piece, a closure noun)
        return "To confirm, per the announcement"
    return ""


def closures_by_text(names: Dict[Any, str], text: str, among=None) -> Dict[Any, str]:
    """key → the sentence that closes it now: BUILD IC-5 (D5) status_decisions' closed_now — the one decision every page reads (IC-4's
    matcher, two words of a name as a substring of a segment that holds a closure word as a substring, is gone: "disclosed" closed)."""
    if not (text or "").strip():
        return {}
    return {k: d["sentence"] for k, d in status_decisions({k: n for k, n in names.items() if isinstance(n, str)}, text, among=among).items()
            if d["state"] == "closed_now"}        # BUILD IC-5 (D5b): `among` as in status_decisions


# ═══ BUILD IC-4 (D3): the operational veto — an announcement sentence that names a record by the data's own name and says it is
# closed, offline, suspended, full or has no vacancy stops every page from offering it; both assertions print ═══
VETO_PHRASES = ("no vacancy", "no vacancies", "at full occupancy", "at full capacity", "at capacity", "is full", "are full", "now full",
                "suspended", "offline", "closed", "shut down", "not operating", "out of service", "shuttered")
VETO_CLOSURE = {"suspended", "offline", "closed", "shut down", "not operating", "out of service", "shuttered"}   # BUILD IC-5 (D5): decided by
# status_decisions — the veto takes one only where the record's decision is closed_now


def veto_forms(names: Dict[Any, str]) -> Dict[Any, List[str]]:
    """key → the forms a sentence names the record by: its whole name, its name before a parenthesis ("HARBOR (Northside Family
    Home)" → "HARBOR"), and a word of four or more capital letters in its name (HARBOR, BEACON) — matched as written. A near name, a
    lower-case word ("bridges between services") or a two-word piece shared with other names never names a record."""
    out = {}
    for k, n in names.items():
        if not isinstance(n, str) or not n.strip():
            continue
        f = {n.strip()}
        head = re.split(r"\s*\(", n, 1)[0].strip()
        if len(head) >= 4:
            f.add(head)
        f |= set(_ENTITY_WORD.findall(n))          # BUILD IC-5 (D5b): the one definition (name_forms reads it too)
        out[k] = sorted(f, key=len, reverse=True)
    return out


def vetoes(names: Dict[Any, str], text: str, among=None) -> Dict[Any, Dict[str, str]]:
    """key → {"sentence", "phrase"}: each record an announcement sentence names (veto_forms) and says, in the same clause, is closed,
    offline, suspended, full or has no vacancy (VETO_PHRASES), the phrase not negated ("no longer closed"). Within a clause the phrase
    belongs to the record named nearest it; a name inside a longer name of another record is that record's ("Northside Hall Annex").
    BUILD IC-5 (D5b): `among`, as status_decisions' — every record's name is read (a closed Annex's name is still the Annex's: its phrase
    is not handed to "Center of Hope" inside it), and only `names`' records are vetoed."""
    if not (text or "").strip():
        return {}
    pool = {**{k: n for k, n in (among or {}).items() if isinstance(n, str)}, **names}
    forms = veto_forms(pool)
    out = {}
    # BUILD IC-5 (D5): a closure word is the status decision's (status_decisions: one decision for every page) — the veto takes one only
    # where that decision is closed_now ("will be closed Saturday" is scheduled, "closed on Sundays" hours, "was closed yesterday and is
    # open today" reopened); the capacity phrases (no vacancy, full, at capacity) stay the veto's own
    dec_ = status_decisions({k: n for k, n in names.items() if isinstance(n, str)}, text, among=pool)
    for sent in re.split(r"(?<=[.!?])\s+|\s+\d+[.)]\s+", _item_lines(text)):
        for clause in re.split(r"\s*[;\u2014]\s*|,\s+(?:and|but|while)\s+", sent):   # IC-4 Phase 7 (review b2): a colon splits no clause
            # ("BRIDGES: no vacancy" — the name before it is the phrase's)
            low = clause.lower()
            hits = []           # (start, end, key) of each name form in the clause, as written
            for k, fs in forms.items():
                for f in fs:
                    for m in re.finditer(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", clause):
                        hits.append((m.start(), m.end(), k))
            hits = [h for h in hits if not any(o[0] <= h[0] and h[1] <= o[1] and (o[1] - o[0]) > (h[1] - h[0]) and o[2] != h[2] for o in hits)]
            if not hits:
                continue
            for ph in VETO_PHRASES:
                for m in re.finditer(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", low):
                    if _negates(low, m.start()):        # IC-4 Phase 7 (review b2): the one negation rule
                        continue
                    near = min(hits, key=lambda h: min(abs(h[0] - m.end()), abs(m.start() - h[1])))
                    group = _listed_with(near, hits, clause)   # IC-4 Phase 7 (review b2): the names listed with it share the phrase
                    for h in hits:          # every record named by the same form there (one entity in two endpoints)
                        if (h[0], h[1]) in group and (ph not in VETO_CLOSURE or (dec_.get(h[2]) or {}).get("state") == "closed_now"):
                            out.setdefault(h[2], {"sentence": sent.strip(), "phrase": ph})
    return {k: v for k, v in out.items() if k in names}        # BUILD IC-5 (D5b): only the records asked


_LIST_SEP = re.compile(r"^\s*(?:,\s*)?(?:(?:and|or|nor|&|plus|as well as)\s+)?$", re.I)


def _listed_with(near, hits, clause):
    """BUILD IC-4 Phase 7 (review b2): the spans of the names listed with `near` — joined to it only by commas, "and", "or", "nor", "&",
    "plus" or "as well as" ("BRIDGES and CrossRoads at Youthville have no vacancy"; "No vacancy at BRIDGES or CrossRoads") — the phrase
    is said of each; a name with other words between ("HARBOR is open and BRIDGES is full") is not in the list."""
    spans = sorted({(h[0], h[1]) for h in hits})
    top = [x for x in spans if not any(o != x and o[0] <= x[0] and x[1] <= o[1] for o in spans)]     # the longest names only
    i = top.index(next(x for x in top if x[0] <= near[0] and near[1] <= x[1]))
    lo = hi = i
    while lo > 0 and top[lo - 1][1] <= top[lo][0] and _LIST_SEP.match(clause[top[lo - 1][1]:top[lo][0]]):
        lo -= 1
    while hi + 1 < len(top) and top[hi][1] <= top[hi + 1][0] and _LIST_SEP.match(clause[top[hi][1]:top[hi + 1][0]]):
        hi += 1
    return {x for x in spans if any(k[0] <= x[0] and x[1] <= k[1] for k in top[lo:hi + 1])}


def _alert_texts(payloads):
    for ep, pl in (payloads or {}).items():
        if not NR.is_alerts(pl):
            continue
        for a in NR.records(pl):
            if isinstance(a, dict):
                t = ". ".join(x for x in (_first_text(a, ALERT_TITLE_KEYS), _first_text(a, ALERT_DETAIL_KEYS)) if x)
                if t:
                    yield ep, str(a.get("id", "")), t


def _word(w):
    return re.compile(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])")


_WORDS = [_word(w) for w in CLOSURE_WORDS]


def _sentence_with(text, m):
    i = m.start()
    a = text.rfind(". ", 0, i)
    b = text.find(". ", i)
    return text[(a + 2 if a >= 0 else 0): (b + 1 if b >= 0 else len(text))].strip()


def quote_of(seg):
    """BUILD IC-4 (D3): a closure's segment in the one form every page quotes — no end stop. The matcher's segments end where its split
    cut them (no stop); the veto's sentence, and a matcher's segment at the very end of a text, kept theirs, so a line ending after the
    quote printed two stops ('“HARBOR has no vacancy.”.')."""
    return re.sub(r"[\s.!?;:]+$", "", str(seg or "").strip())


def closures(sets, payloads, announcement, among=None):
    """(closed, flags): closed[(set id, record id)] = {by, why, segment}; flags[...] = {field, quote} — the record's own words holding a
    closure word as a whole word ("foreclosure" is not "closure"), or an alert naming it only by words it shares with other sites' names
    in its set: to confirm, never a closure ("closed Sundays" must not close a record open today; "Health Center closed" must not close
    every health center). BUILD IC-5 (D5c): `among` — every set the spec validated (a caller decides its page's sets, or an age gate's):
    each name is read against every record's name of them all, so every call decides a record the same way."""
    closed, flags, names = {}, {}, {}
    for s in sets:
        sid, nf = s["spec"]["id"], s["spec"]["name_field"]
        for r in s["recs"]:
            k = (sid, str(r.get("id", r.get(nf))))
            _st, by, why, _risk = record_status(r)
            if by == "data":
                closed[k] = {"by": "data", "why": why, "segment": ""}
            names[k] = r.get(nf)
            for f, v in r.items():
                if isinstance(v, str) and f not in (nf, "id") and k not in flags:
                    m = next((m for rx in _WORDS for m in [rx.search(v.casefold())] if m), None)
                    if m:
                        flags[k] = {"field": field_text(f), "quote": _sentence_with(v, m)}      # BUILD IC-8 (D12): in words
    open_names = {k: n for k, n in names.items() if k not in closed}
    every = dict(names)                           # BUILD IC-5 (D5c): the whole data's names, the sets this call does not decide too
    for s in among or []:
        sid, nf = s["spec"]["id"], s["spec"]["name_field"]
        for r in s["recs"]:
            every.setdefault((sid, str(r.get("id", r.get(nf)))), r.get(nf))
    for k, seg in closures_by_text(open_names, announcement, among=every).items():     # BUILD IC-5 (D5b): every record's name read
        closed[k] = {"by": "announcement", "why": "the announcement", "segment": quote_of(seg)}
    for k, d in status_decisions({k: n for k, n in open_names.items() if k not in closed and isinstance(n, str)}, announcement or "",
                                 among=every).items():
        if status_note(d):        # BUILD IC-5 (D5): scheduled or conditional — to confirm, never a closure
            note_ = {"field": "the announcement", "quote": f"{status_note(d)}: “{quote_of(d['sentence'])}”"}
            flags[k] = note_ if k not in flags else {"field": f"{flags[k]['field']}; the announcement", "quote": f"{flags[k]['quote']} — and {note_['quote']}"}
    for k, v in vetoes({k: n for k, n in open_names.items() if k not in closed}, announcement, among=every).items():   # IC-4 (D3): the veto
        closed[k] = {"by": "announcement", "why": "the announcement", "segment": quote_of(v["sentence"]), "veto": v["phrase"]}
    for ep, aid, t in _alert_texts(payloads):
        for k, seg in closures_by_text({k: n for k, n in open_names.items() if k not in closed}, t, among=every).items():
            mine = {f for f in _frags(names[k]) if f in seg.lower()}
            others = set().union(*[_frags(n) for k2, n in names.items() if k2 != k and k2[0] == k[0] and isinstance(n, str)] or [set()])
            if mine and mine <= others:
                flags.setdefault(k, {"field": alert_text(ep, aid, payloads), "quote": seg})       # BUILD IC-8 (D12): by its name
            else:
                closed[k] = {"by": "alert", "why": alert_text(ep, aid, payloads), "segment": quote_of(seg)}
        for k, d in status_decisions({k: n for k, n in open_names.items() if k not in closed and isinstance(n, str)}, t, among=every).items():
            if d["state"] == "unresolved":          # BUILD IC-5 (D5b): an alert naming a site only by a piece others share — to confirm
                flags.setdefault(k, {"field": alert_text(ep, aid, payloads), "quote": quote_of(d["sentence"])})
    return closed, flags


# ═══ one person, one set ═══
def _value(r, field, ann):
    a = ann.get(str(r.get("id")), {}).get(field)
    return (a["value"], a["quote"]) if a is not None else (r.get(field), None)


def _gate(g, r, person, ann, when, name, hits=None):
    """(outcome 'pass' | 'fail' | 'confirm', detail). A person's attribute the preset does not give is unknown: to confirm, never a
    pass and never a fail."""
    rule = g["rule"]
    if isinstance(g.get("when"), dict):
        wa = g["when"].get("attr")
        if wa not in person:
            o, d = _gate(dict(g, when=None), r, person, ann, when, name, hits)       # B2 (review): applied or not, does it pass?
            if o == "pass":
                return "pass", f"passes whether or not it applies ({name}'s {_h(wa)} not known): {d}"
            return "confirm", f"not known for {name} ({_h(wa)}), which decides whether this gate applies"
        if person[wa]["value"] != g["when"].get("is"):
            return "pass", f"does not apply ({name}'s {_h(wa)}: {_show(person[wa]['value'])})"
    if rule == "free_above_zero":
        h0 = (hits or {}).get(g["fields"][0])
        if h0 and h0["reads_as"] == "no cap":                       # B2: a declared placeholder for a capacity with no limit
            return "pass", f"no cap ({_no_cap(g['fields'][0], h0)})"
        vs = [r.get(f) for f in g["fields"]]
        if not all(_num(v) for v in vs):
            return "confirm", f"{' or '.join(field_text(x) for x in g['fields'])} not a number in the record"
        free = vs[0] - (vs[1] if len(vs) == 2 else 0)
        txt = f"{_show(free)} free ({field_text(g['fields'][0])} {_show(vs[0])}" + (f" − {field_text(g['fields'][1])} {_show(vs[1])})" if len(vs) == 2 else ")")
        return ("pass" if free > 0 else "fail"), txt
    if rule == "open_now":
        ok, d = open_at(r.get(g["field"]), when)
        return ("confirm" if ok is None else "pass" if ok else "fail"), d
    if rule == "bool":
        v = r.get(g["field"])
        if not isinstance(v, bool):
            return "confirm", f"{field_text(g['field'])} not given as yes/no"
        return ("pass" if v == g["want"] else "fail"), f"{field_text(g['field'])}: {_show(v)}"
    v, quote = _value(r, g["field"], ann)
    src = f" (the record's own words: \"{quote}\")" if quote else ""
    attr = g.get("attr")
    if rule == "in_set":
        if v is None and g.get("if_missing") == "pass":
            return "pass", f"no {field_text(g['field'])} limit in the record"
        rv = _vals(v)
        if rv is None:
            return "confirm", f"{field_text(g['field'])} not given in the record"
        if set(rv) & {_norm(x) for x in g.get("any_values") or []}:
            return "pass", f"{field_text(g['field'])} {_show(v)}{src}: open to anyone"
        if attr not in person:
            return "confirm", f"not known for {name} ({_h(attr)}) — {field_text(g['field'])}: {_show(v)}{src}"
        pv = _vals(person[attr]["value"]) or []
        return ("pass" if set(rv) & set(pv) else "fail"), f"{field_text(g['field'])} {_show(v)}{src}; {name}'s {_h(attr)}: {_show(person[attr]['value'])}"
    if rule == "age_range":
        b = age_bounds(v)
        if b is None:
            return "confirm", f"{field_text(g['field'])} \"{_show(v)}\" not read as an age range"
        if attr not in person or not _num(person[attr]["value"]):
            if b == (0, 200):                                               # B2 (review): every age is inside — no need to know it
                return "pass", f"{field_text(g['field'])} {_show(v)}: every age"
            return "confirm", f"not known for {name} ({_h(attr)}) — {field_text(g['field'])}: {_show(v)}"
        a = person[attr]["value"]
        return ("pass" if b[0] <= a <= b[1] else "fail"), f"{field_text(g['field'])} {_show(v)}; {name}'s {_h(attr)}: {_show(a)}"
    return "confirm", "rule not read"


def _factor(f, r, person, ann, hits=None):
    """(term 0–1, the value read, note)."""
    form = f["form"]
    if form == "ratio" and not f.get("range_top"):
        c0 = (f.get("fields") or [f.get("field")])[0]
        h0 = (hits or {}).get(c0)
        if h0 and h0["reads_as"] == "no cap":                       # B2: full points, the value shown as what it is
            return 1.0, "no cap", _no_cap(c0, h0)
    if form == "bool":
        v = r.get(f["field"])
        if not isinstance(v, bool):
            return 0.0, v, "not given as yes/no: 0"
        return (1.0 if v == f["want"] else 0.0), v, ""
    if form == "match":
        v, _q = _value(r, f["field"], ann)
        rv, attr = _vals(v), f["attr"]
        if attr not in person:
            return 0.0, v, f"the person's {_h(attr)} not known: 0"
        pv = _vals(person[attr]["value"]) or []
        return (1.0 if rv and set(rv) & set(pv) else 0.0), v, ""
    if f.get("range_top"):
        b = age_bounds(r.get(f["range_top"]))
        a = person.get(f["minus_attr"], {}).get("value")
        if b is None or not _num(a):
            return 0.0, None, "not computable: 0"
        val = min(b[1], 120) - a
    else:
        fs = f.get("fields") or [f["field"]]
        vs = [r.get(x) for x in fs]
        if len(fs) == 1 and isinstance(vs[0], list):
            vs = [len(vs[0])]
        if not all(_num(x) for x in vs):
            return 0.0, None, "not in the record: 0"
        val = vs[0] - (vs[1] if len(vs) == 2 else 0)
    ratio = val / f["ref"]
    term = min(1.0, max(0.0, ratio)) if form == "ratio" else min(1.0, max(0.0, 1 - ratio))
    return term, val, ""


def _person(pr):
    """The preset's attributes; one written as null is unknown (review: null must never pass a gate)."""
    return {a: s for a, s in (pr.get("attrs") or {}).items() if isinstance(s, dict) and s.get("value") is not None}


def _klass(kind, g, r):
    """The first failing gate's class, so each record is counted once (review): closed (by a closure, or not open now), full (no free
    place where there are places), other (not for this person: who the option serves, its own condition, no places at all)."""
    if kind == "closure" or (g and g["rule"] == "open_now"):
        return "closed"
    if g and g["rule"] == "free_above_zero" and len(g["fields"]) == 2 and _num(r.get(g["fields"][0])) and r.get(g["fields"][0]) > 0:
        return "full"
    return "other"


def run_case(s, pr, closed, flags, when):
    spec, recs, ann, excl, stale = s["spec"], s["recs"], s["ann"], s.get("excl") or {}, s.get("stale") or {}
    sid, nf = spec["id"], spec["name_field"]
    person, name = _person(pr), pr.get("name") or pr.get("label") or pr["id"]
    ranked, excluded, confirm, fits = [], [], [], 0
    for r in recs:
        rid = str(r.get("id", r.get(nf)))
        k, nm = (sid, rid), str(r.get(nf))
        out = [("Closed", "fail", f"closed by {closed[k]['why']}" + (f": \"{closed[k]['segment']}\"" if closed[k]["segment"] else ""), "closure", None)
               if k in closed else ("Not closed", "pass", "no closure in the data, the announcement or an alert", "closure", None)]
        if rid in excl:
            e = excl[rid]["exclude"]
            out.append(("Not in this set", "fail", f"{e['why']} — the record: \"{e['quote']}\"", "exclude", None))
        if k in flags and k not in closed:
            out.append(("The record's own words", "confirm", f"{flags[k]['field']}: \"{flags[k]['quote']}\"", "flag", None))
        for note in stale.get(rid, []):
            out.append(("The table's reading", "confirm", note, "stale", None))
        hr = (s.get("holds") or {}).get(rid) or {}
        hits = _ph_hits(s.get("ph"), r)
        rv = _ph_view(r, hits)
        for g in spec.get("gates") or []:
            o, d = held(g, hr.get(g.get("id")), *_gate(g, rv, person, ann, when, name, hits))
            out.append((g.get("label") or g["rule"], o, d, g["rule"], g))
        trace = [f"{lab}: {d} → {o}" for lab, o, d, _k, _g in out]
        # BUILD IC-4 (D3): the record fits this person whatever its state — no gate of the person's fit fails (the class the first-gate
        # count calls "not for this person"); a set serves a person when one of its records fits (the closure is counted first)
        fits += not any(o == "fail" and _klass(kd, g, r) == "other" for _lab, o, _d, kd, g in out)
        fails = [x for x in out if x[1] == "fail"]
        unk = [x for x in out if x[1] == "confirm"]
        row = {"id": rid, "name": nm, "trace": trace}
        chk = r.get(spec.get("check_field")) if spec.get("check_field") else None
        if isinstance(chk, str) and chk.strip():
            row["check"] = chk.strip()
        if fails:
            lab, _o, d, kind, g = fails[0]
            ex = dict(row, gate=lab, detail=d, klass=_klass(kind, g, r), closure=kind == "closure")
            if kind == "closure":       # BUILD IC-4 (D3): both assertions print — the data's own reading beside the closure, when it
                said = [x[2] for x in out if x[3] == "free_above_zero" and x[1] == "pass"]     # says the record has room
                if said:
                    ex["data_says"] = said[0]
                    ex["detail"] = NR.join_clauses([d, f"the {endpoint_text(spec['endpoint']) if spec.get('endpoint') else sid} records still read {said[0]}"])   # IC-4 (D8); IC-8 (D12)
            excluded.append(ex)
            continue
        if unk:
            confirm.append(dict(row, gate=unk[0][0], detail=NR.join_clauses(f"{x[0]}: {x[2]}" for x in unk)))   # IC-4 (D8): one stop
            continue
        comps = []
        for f in spec.get("factors") or []:
            term, val, note = _factor(f, rv, person, ann, hits)
            t = tenths(f["max"] * term)
            comps.append({"id": f["id"], "label": f.get("label") or f["id"], "tenths": t, "points": pts(t), "value": _show(val),
                          "reads": reads_text(f), "raw": val if _num(val) else None, "form": f["form"], "term": round(term, 6), "note": note})
        sc = sum(c["tenths"] for c in comps)
        ranked.append(dict(row, tenths=sc, score=pts(sc), components=comps))
    tb = spec.get("tie_breakers") or []

    def tie(x, t):
        c = next(c for c in x["components"] if c["id"] == t)
        if c["raw"] is not None and c["form"] in ("ratio", "inverse"):     # review: the record's value decides (31 free beats 10 free)
            return -c["raw"] if c["form"] == "ratio" else c["raw"]
        return -c["tenths"]
    ranked.sort(key=lambda x: (-x["tenths"], *[tie(x, t) for t in tb], x["id"]))
    ref = spec.get("referral")
    by_id = {str(r.get("id", r.get(nf))): r for r in recs}
    need_ref = sum(1 for x in ranked if isinstance(ref, dict) and by_id.get(x["id"], {}).get(ref.get("field")) == ref.get("is"))
    counts = {"records": len(recs), "eligible": len(ranked), "referral": need_ref if isinstance(ref, dict) else None,
              "full": sum(1 for x in excluded if x["klass"] == "full"), "closed": sum(1 for x in excluded if x["klass"] == "closed"),
              "other": sum(1 for x in excluded if x["klass"] == "other"), "confirm": len(confirm), "fits": fits}
    return {"set": sid, "ranked": ranked, "excluded": excluded, "confirm": confirm, "counts": counts}


# ═══ the published table ═══
def _h(a) -> str:
    """An attribute's name as a reader sees it (primary_audience → primary audience)."""
    return str(a).replace("_", " ")


def gate_text(g):
    r = g["rule"]
    t = {"in_set": f"{field_text(g.get('field'))} is one of the person's {_h(g.get('attr'))}" + (" (no limit stated → passes)" if g.get("if_missing") == "pass" else ""),
         "age_range": f"the person's {_h(g.get('attr'))} is inside {field_text(g.get('field'))}",
         "free_above_zero": " − ".join(field_text(x) for x in g.get("fields") or []) + " > 0",
         "open_now": f"open at the evaluation time, by {field_text(g.get('field'))}",
         "bool": f"{field_text(g.get('field'))} is {_show(g.get('want'))}"}.get(r, r)
    if isinstance(g.get("when"), dict):
        t += f", when the person's {_h(g['when'].get('attr'))} is {_show(g['when'].get('is'))}"
    return t


def reads_text(f):
    """What a factor's value is a reading of, as the published formula names it. B2 (the second live R4): a factor's label says what
    earns points, and the field it reads can measure the opposite — "No referral needed 0.0 (yes)" and "Participants who stay 19.0
    (0.12)" read backwards (0.12 is the share who lose contact) — so every component carries this text and every display names it
    beside the value: both pages and the rationale's input (naipc_engine.js readsText gives the same text; t_parity_b2 compares them)."""
    if f.get("range_top"):
        return f"top of {field_text(f['range_top'])} − the person's {_h(f['minus_attr'])}"
    fs = f.get("fields") or [f["field"]]
    return f"{field_text(fs[0])} − {field_text(fs[1])}" if len(fs) == 2 else field_text(fs[0])       # BUILD IC-8 (D12): in words


def factor_text(f):
    m, form = f["max"], f["form"]
    if form == "bool":
        return f"{m:g} if {field_text(f['field'])} is {_show(f['want'])}, else 0"
    if form == "match":
        return f"{m:g} if {field_text(f['field'])} includes the person's {_h(f['attr'])}, else 0"
    r = reads_text(f)                       # the formula's variable and the component's reading are one text
    v = f"({r})" if f.get("range_top") or len(f.get("fields") or [f["field"]]) == 2 else r
    return f"{m:g} × min(1, {v} / {f['ref']:g})" if form == "ratio" else f"{m:g} × max(0, 1 − {v} / {f['ref']:g})"


def source_text(f):
    if f.get("range_top"):
        return f"{field_text(f['range_top'])}, the person's {_h(f['minus_attr'])}"
    if f["form"] == "match":
        return f"{field_text(f['field'])}, the person's {_h(f['attr'])}"
    return ", ".join(field_text(x) for x in f.get("fields") or [f["field"]])       # BUILD IC-8 (D12): in words


def tie_text(f):
    lab = f.get("label") or f["id"]
    return f"{lab} (" + {"ratio": "the higher value first", "inverse": "the lower value first"}.get(f["form"], "its points") + ")"


def table(s):
    spec = s["spec"]
    fac = {f["id"]: f for f in spec.get("factors") or []}
    return {"id": spec["id"], "label": spec.get("label") or spec["id"], "endpoint": spec["endpoint"], "records": len(s["recs"]),
            "gates": [{"label": "Not closed", "text": "not closed by the record's own status, the announcement or an alert"}]
                     + ([{"label": "Not in this set", "text": "a record the table leaves out, in its own words (below)"}] if s.get("excl") else [])
                     + [{"label": g.get("label") or g["rule"], "text": gate_text(g) + _held_text(s, g) + _ph_text(s, _rule_fields(g))}
                        for g in spec.get("gates") or []],
            "factors": [{"id": f["id"], "label": f.get("label") or f["id"], "max": f"{f['max']:g}", "form": f["form"],
                         "source": source_text(f), "formula": factor_text(f) + _ph_text(s, _rule_fields(f)), "cap": f"{f['max']:g} points", "why": f["why"],
                         "ref": f.get("ref"), "want": f.get("want")} for f in spec.get("factors") or []],
            "tie_breakers": [tie_text(fac[t]) for t in spec.get("tie_breakers") or [] if t in fac] + ["the record id"],
            "annotations": [{"id": rid, "field": f, "value": _show(a["value"]), "quote": a["quote"]} for rid, fs in s["ann"].items() for f, a in fs.items()],
            "excluded": [{"id": rid, "name": _rec_name(s, rid), "why": e["exclude"]["why"], "quote": e["exclude"]["quote"]}
                         for rid, e in (s.get("excl") or {}).items()]}


def _rule_fields(x):
    return [v for v in [x.get("field"), x.get("range_top")] + list(x.get("fields") or []) if isinstance(v, str)]


def _ph_text(s, fields):
    ps = [(f, (s.get("ph") or {})[f]) for f in fields if f in (s.get("ph") or {})]
    return "".join(f"; {field_text(f)} {_show(e['value'])} reads as {e['reads_as']} — assumed: {e['why']}" for f, e in ps)


def _held_text(s, g):
    hs = [(rid, h[g.get("id")]) for rid, h in (s.get("holds") or {}).items() if g.get("id") in h]
    return ("; to confirm, not out, where the record's own words offer it: " + "; ".join(f"{_rec_name(s, rid)} (\"{h['quote']}\")" for rid, h in hs)) if hs else ""


def _rec_name(s, rid):
    nf = s["spec"]["name_field"]
    return next((str(r.get(nf)) for r in s["recs"] if str(r.get("id", r.get(nf))) == rid), rid)


def preset_view(pr, users, read=None):
    """A person's card. BUILD B3 (D13, §9): with `read` (the attributes the page's sets read), the card lists only those — the
    Oracle's card said "What the ranking reads" over attributes only the Architect's sets read."""
    u = users.get(pr.get("named_user")) or {}
    attrs = []
    for a, sa in (pr.get("attrs") or {}).items():
        if not isinstance(sa, dict) or sa.get("value") is None or (read is not None and a not in read):
            continue
        b = sa.get("basis")
        src = ({"age": "the brief's age", "role": f"the brief: \"{sa.get('quote')}\"", "quote": f"the brief: \"{sa.get('quote')}\""}.get(sa.get("from"), "the brief")
               if b == "stated" else f"assumed: {sa.get('reason')}" if b == "assumed" else "illustrative — not from the brief")
        attrs.append({"name": _h(a), "value": _show(sa.get("value")), "basis": b, "source": src})
    name = pr.get("name") or u.get("name") or pr.get("label") or pr["id"]
    who = (f"{name}" + (f", {u['age']}" if u.get("age") is not None else "") + (f" — {u.get('role')}" if u.get("role") else "")
           if u else (pr.get("label") or name))
    return {"id": pr["id"], "name": name, "named_user": pr.get("named_user"), "illustrative": pr.get("named_user") is None,
            "who": who, "quote": u.get("quote") or "", "attrs": attrs}


# ═══ the entry points ═══
def run(spec, contract, payloads, announcement="", when=None, when_src=None, page="oracle", base=None):
    """The whole result for one run (never raises): status, problems, the tables, each person's cases, the closures, the fairness rows.
    A case that cannot be ranked is a problem line, never the whole result (review). B2: only the sets shown on `page` (a set's
    optional "pages"; the Oracle's view leaves out the Architect's "A place to rent")."""
    try:
        extra = []
        if not when:
            when, when_src, p = as_of()
            extra = [p] if p else []
        problems, sets_all, presets = validate_spec(spec, contract, payloads, _block2(announcement), base)
        sets = [s for s in sets_all if _on_page(s["spec"], page)]
        problems = extra + problems
        if not sets:
            return {"status": "invalid", "problems": problems or ["no option set can run"], "sets": [], "presets": [], "closures": [],
                    "fairness": [], "as_of": f"{when:%a %d %b %Y, %H:%M}", "as_of_source": when_src}
        closed, flags = closures(sets, payloads, announcement, among=sets_all)       # BUILD IC-5 (D5c): every set's names read
        users = _users(contract)
        out_p, read = [], set(_reads(sets))
        for pr in presets:
            v = preset_view(pr, users, read)
            v["cases"] = []
            for s in sets:
                try:
                    v["cases"].append(run_case(s, dict(pr, name=v["name"]), closed, flags, when))
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{s['spec']['id']} for {pr.get('id')}: not ranked ({type(e).__name__}: {str(e)[:80]})")
            out_p.append(v)
        fair = [{"preset": p["id"], "name": p["name"], "set": c["set"], **c["counts"]} for p in out_p for c in p["cases"]]
        names = {(s["spec"]["id"], str(r.get("id", r.get(s["spec"]["name_field"])))): str(r.get(s["spec"]["name_field"])) for s in sets for r in s["recs"]}
        cl = [{"set": k[0], "id": k[1], "name": names.get(k, k[1]), **v} for k, v in sorted(closed.items())]
        return {"status": "ok", "problems": problems, "sets": [table(s) for s in sets], "presets": out_p, "closures": cl,
                "flags": [{"set": k[0], "id": k[1], "name": names.get(k, k[1]), **v} for k, v in sorted(flags.items())],
                "fairness": fair, "as_of": f"{when:%a %d %b %Y, %H:%M}", "as_of_source": when_src, "top_n": TOP_N}
    except Exception as e:  # noqa: BLE001 — a page is never lost to the engine
        return {"status": "error", "problems": [f"the engine stopped: {type(e).__name__}: {e}"], "sets": [], "presets": [], "closures": [],
                "fairness": []}


def reroute(before, after, n=TOP_N):
    """Per person and set of the BEFORE result (what Block 1's page showed): the options that left the top n, why, and what replaced
    them; a set Block 1 ranked that this run cannot rank is a row of its own (review: never a silent "nothing changed"). Never raises."""
    rows = []
    try:
        ap = {p["id"]: {c["set"]: c for c in p["cases"]} for p in after.get("presets") or []}
        labels = {t["id"]: t.get("label", t["id"]) for t in (before.get("sets") or []) + (after.get("sets") or [])}
        for p in before.get("presets") or []:
            for b in p["cases"]:
                c = ap.get(p["id"], {}).get(b["set"])
                top_b = [x["id"] for x in b["ranked"][:n]]
                nm = {x["id"]: x["name"] for x in b["ranked"]}
                if c is None:
                    if top_b:
                        rows.append({"preset": p["id"], "name": p["name"], "set": b["set"], "missing": True,
                                     "left": [{"id": i, "name": nm.get(i, i), "why": f"{labels.get(b['set'], b['set'])} could not be ranked on this run's data"} for i in top_b],
                                     "replaced_by": []})
                    continue
                top_a = [x["id"] for x in c["ranked"][:n]]
                left = [i for i in top_b if i not in top_a]
                if not left:
                    continue
                nm.update({x["id"]: x["name"] for x in c["ranked"] + c["excluded"] + c["confirm"]})
                why = {}
                for i in left:
                    ex = next((x for x in c["excluded"] if x["id"] == i), None)
                    cf = next((x for x in c["confirm"] if x["id"] == i), None)
                    rk = next(((k + 1, x) for k, x in enumerate(c["ranked"]) if x["id"] == i), None)
                    why[i] = ((ex["detail"] if ex.get("closure") else f"{ex['gate']}: {ex['detail']}") if ex else f"to confirm — {cf['detail']}" if cf
                              else f"now ranked {rk[0]} ({rk[1]['score']} points)" if rk else "no longer in the data")
                rows.append({"preset": p["id"], "name": p["name"], "set": b["set"], "missing": False,
                             "left": [{"id": i, "name": nm.get(i, i), "why": why[i]} for i in left],
                             "replaced_by": [{"id": i, "name": nm.get(i, i)} for i in top_a if i not in top_b]})
    except Exception:  # noqa: BLE001
        return rows
    return rows


def for_oracle(payloads, contract, contract_path, announcement="", base_result=None, base=None):
    """The Oracle's stage (D4, D6): the sidecar beside the contract on this run's data; in Block 2 also the reroute rows against Block 1's
    saved ranking (`base_result`: what Block 1's page showed — never Block 1's data re-ranked at this run's clock, review).
    'console' holds the lines the harness prints. Never raises."""
    spec, why = load_spec(contract_path)
    if spec is None:
        return {"status": "no sidecar", "problems": [why], "console": [f"prioritization model: not built — {why}"], "sets": [], "presets": []}
    when, src, p = as_of()
    res = run(spec, contract, payloads, announcement, when, src, base=base)
    if p:
        res["problems"] = [p] + list(res.get("problems") or [])
    con = [f"prioritization model: {len(res.get('sets') or [])} option set(s), {len(res.get('presets') or [])} person(s), "
           f"evaluated at {res.get('as_of')} ({res.get('as_of_source')})"]
    con += [f"engine sidecar: {x}" for x in res.get("problems") or []]
    con += [f"closed: {c['name']} ({c['set']}) — {c['why']}" for c in res.get("closures") or []]
    if base_result is not None and res.get("status") == "ok":
        if isinstance(base_result, dict) and base_result.get("status") == "ok":
            res["reroute"] = reroute(base_result, res)
            res["reroute_status"] = "ok"
        else:
            res["reroute_status"] = "Block 1 saved no ranking"
            con.append("prioritization model: no reroute list — Block 1 saved no ranking")
    res["console"] = con
    res["spec_file"] = spec_path_for(contract_path).name
    return res


def point_id(set_id, factor_id):
    """BUILD IC-3 (D7): a factor's points in the published table as a registry id — the harness registers one per factor of each set the
    Oracle's page shows (DERIVED, "the published prioritization table"), and the prompt prints it as `[[id]] = value`."""
    return f"prio.{set_id}.{factor_id}"


def table_points(spec, contract, payloads, announcement="", base=None):
    """BUILD IC-3 (D7): [(id, points, factor label, set label)] — every factor's points of each set the Oracle's page shows; [] when no
    set can run. The registry's entries and the prompt's lines come from here."""
    try:
        _P, sets, _pr = validate_spec(spec, contract, payloads, _block2(announcement), base)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in sets:
        if _on_page(s["spec"], "oracle"):
            lab = s["spec"].get("label") or s["spec"]["id"]
            out += [(point_id(s["spec"]["id"], f["id"]), float(f["max"]), f.get("label") or f["id"], lab) for f in s["spec"].get("factors") or []]
    return out


def prompt_text(spec, contract, payloads, announcement="", base=None, ids=False):
    """The published table as plain lines for the model's prompt ('' when no set can run): the narrative and the brief's answers then
    describe the table the page computes — never another one. BUILD IC-3 (D7), `ids`: Python owns the table's numbers — each set's
    record count and each factor's points are printed as `[[id]] = value`, the gates by their names, each factor with what it weighs;
    the model names the table and says what it weighs and why, and writes a point only as its id (Python prints the gates, the formulas
    and the points on the page)."""
    try:
        _P, sets, presets = validate_spec(spec, contract, payloads, _block2(announcement), base)
        sets = [s for s in sets if _on_page(s["spec"], "oracle")]        # B2: the Oracle's prompt describes the Oracle's tables
        if not sets:
            return ""
        if ids:
            out = ["The page's prioritization model is computed in Python from this published table and the API's records; no model call "
                   "ranks anything. Where the brief asks for a prioritization model, name this table and say what it weighs and why — Python "
                   "prints its gates, formulas and points on the page. A point is written only as its id:"]
            for s in sets:
                t = table(s)
                out.append(f"- {t['label']} ({t['endpoint']}: [[{t['endpoint']}.count]] records). Gates: "
                           + "; ".join(g["label"] for g in t["gates"]) + ". Factors: "
                           + "; ".join(f"[[{point_id(t['id'], f['id'])}]] = {f['max']} — the points for {f['label']} ({str(f['why']).split(':')[0].strip()})"
                                       for f in t["factors"]) + ".")
            users = _users(contract)
            named = [preset_view(p, users)["name"] for p in presets if p.get("named_user")]
            if named:
                out.append("The page shows worked cases for " + ", ".join(named) + ": their options ranked by this table.")
            return "\n".join(out)
        out = ["The page's prioritization model is computed in Python from this published table and the API's records; no model call "
               "ranks anything. Where the brief asks for a prioritization model, describe THIS table — its gates, factors and points — and "
               "no other:"]
        for s in sets:
            t = table(s)
            out.append(f"- {t['label']} ({t['endpoint']}, {t['records']} records). Gates: " + "; ".join(g["text"] for g in t["gates"])
                       + ". Factors: " + "; ".join(f"{f['label']}, {f['max']} points: {f['formula']}" for f in t["factors"]) + ".")
        users = _users(contract)
        named = [preset_view(p, users)["name"] for p in presets if p.get("named_user")]
        if named:
            out.append("The page shows worked cases for " + ", ".join(named) + ": their options ranked by this table.")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return ""


# ═══ BUILD B2: the Architect's page — the bundle (D1) and the sidecar's intake, checklist, navigator and readiness parts (D2, D4, D5,
# D9). Python validates and resolves everything a person's answers and the viewer's clock do not decide (closures, exclusions, flags,
# stale readings, annotations, parsed hours); naipc_engine.js ranks in the browser with the same rules (t_parity_b2 proves the two agree).
PAGES = ("oracle", "architect")


def _on_page(spec_set, page):
    """A set is on every page unless its optional "pages" list names others (B2: "A place to rent" is the Architect's alone)."""
    pg = spec_set.get("pages") if isinstance(spec_set, dict) else None
    return not isinstance(pg, list) or page in pg


def _hours_view(text):
    """An hours field as the browser reads it: always open, weekly rules [[days], start minute, end minute], or not read (with open_at's
    own words for that, which no clock changes)."""
    kind, rules = parse_hours(text)
    if kind == "always":
        return {"text": text, "kind": "always"}
    if kind == "rules":
        return {"text": text, "kind": "rules", "rules": [[sorted(d), a, b] for d, a, b in rules]}
    return {"text": text if isinstance(text, str) else None, "kind": None, "detail": open_at(text, datetime(2000, 1, 3))[1]}


def _pre_checks(s, rid, closed, flags):
    """The checks that come before a set's gates and depend on neither the person nor the clock (run_case's first entries, same words)."""
    k = (s["spec"]["id"], rid)
    out = [["Closed", "fail", f"closed by {closed[k]['why']}" + (f": \"{closed[k]['segment']}\"" if closed[k]["segment"] else ""), "closure"]
           if k in closed else ["Not closed", "pass", "no closure in the data, the announcement or an alert", "closure"]]
    if rid in (s.get("excl") or {}):
        e = s["excl"][rid]["exclude"]
        out.append(["Not in this set", "fail", f"{e['why']} — the record: \"{e['quote']}\"", "exclude"])
    if k in flags and k not in closed:
        out.append(["The record's own words", "confirm", f"{flags[k]['field']}: \"{flags[k]['quote']}\"", "flag"])
    for note in (s.get("stale") or {}).get(rid, []):
        out.append(["The table's reading", "confirm", note, "stale"])
    return out


def _set_bundle(s, closed, flags):
    spec = s["spec"]
    sid, nf = spec["id"], spec["name_field"]
    read = set(_read_fields(spec))
    if isinstance(spec.get("referral"), dict) and isinstance(spec["referral"].get("field"), str):
        read.add(spec["referral"]["field"])
    contact = spec.get("contact") if isinstance(spec.get("contact"), dict) else {}
    show = [f for f in (spec.get("show") or []) if isinstance(f, str)]
    recs = []
    for r in s["recs"]:
        rid = str(r.get("id", r.get(nf)))
        chk = r.get(spec.get("check_field")) if spec.get("check_field") else None
        recs.append({"id": rid, "name": str(r.get(nf)), "pre": _pre_checks(s, rid, closed, flags),
                     "raw": {f: r[f] for f in sorted(read) if f in r},
                     "ann": {f: {"value": a["value"], "quote": a["quote"]} for f, a in (s["ann"].get(str(r.get("id"))) or {}).items()},
                     "fl": sorted(f for f in read if isinstance(r.get(f), float)),        # JSON drops float-ness; _show words a float apart
                     "hours": {g["field"]: _hours_view(r.get(g["field"])) for g in spec.get("gates") or [] if g.get("rule") == "open_now"},
                     "check": chk.strip() if isinstance(chk, str) and chk.strip() else None,
                     "contact": {k2: r[f] for k2, f in contact.items() if isinstance(f, str) and r.get(f) not in (None, "", [])},
                     "show": {f: r[f] for f in show if r.get(f) not in (None, "", [])}})
        if (s.get("holds") or {}).get(rid):
            recs[-1]["holds"] = s["holds"][rid]
        hits = _ph_hits(s.get("ph"), r)
        if hits:
            recs[-1]["ph"] = hits
    keep = ("id", "label", "endpoint", "name_field", "gates", "factors", "tie_breakers", "referral", "check_field")
    return {"id": sid, "label": spec.get("label") or sid, "spec": {k2: spec[k2] for k2 in keep if k2 in spec}, "table": table(s),
            "records": recs}


def _reads(sets):
    """attr → [(set, field or None, how)] for every rule that reads a person attribute."""
    out = {}
    for s in sets:
        sp = s["spec"]
        for g in sp.get("gates") or []:
            if isinstance(g, dict) and isinstance(g.get("attr"), str):
                out.setdefault(g["attr"], []).append((s, g.get("field"), g.get("rule")))
            if isinstance(g, dict) and isinstance(g.get("when"), dict) and isinstance(g["when"].get("attr"), str):
                out.setdefault(g["when"]["attr"], []).append((s, None, "when"))
        for f in sp.get("factors") or []:
            if isinstance(f, dict) and isinstance(f.get("attr"), str):
                out.setdefault(f["attr"], []).append((s, f.get("field"), f.get("form")))
            if isinstance(f, dict) and isinstance(f.get("minus_attr"), str):
                out.setdefault(f["minus_attr"], []).append((s, None, "minus"))
    return out


def _held(s, field):
    """The normalized values a field holds in a set's records and annotations."""
    out = set()
    for v in _field_values(s["recs"], field, s["ann"]):
        out |= set(_vals(v) or [])
    return out


def _check_step(st, w, reads, set_ids, P):
    """One intake or readiness step: {id, label} and one of: sets (the option sets to show first), attr + type number (+ derive),
    attr + options [{label, value}] (+ multi, also). Its attribute must be one the table reads; an option's value must be a value the
    data holds for a rule that compares it (or the type a bool rule wants); a derived threshold says why."""
    if not isinstance(st, dict):
        P.append(f"{w}: must be an object")
        return None
    if not (isinstance(st.get("id"), str) and SLUG.match(st["id"])):
        P.append(f"{w}.id: a lowercase slug is required")
        return None
    if not (isinstance(st.get("label"), str) and st["label"].strip()):
        P.append(f"{w}.label: the question in plain words")
        return None
    if "sets" in st:
        if not isinstance(st["sets"], list):
            P.append(f"{w}.sets: option set ids of this page, got (not a list)")
            return None
        bad = [x for x in st["sets"] if x not in set_ids]
        keep = [x for x in st["sets"] if x in set_ids]
        if bad:                      # B2 (review): a set that did not run (a record gone at the curveball) is dropped, not the intake
            P.append(f"{w}.sets: {bad} not on this page (did not run) — left out")
        if not keep:
            P.append(f"{w}.sets: no option set of this page left")
            return None
        return dict(st, sets=keep, kind="sets")
    a = st.get("attr")
    if a not in reads:
        P.append(f"{w}.attr: no gate or factor of this page reads {a!r}")
        return None
    for x in st.get("also") or []:
        if x not in reads:
            P.append(f"{w}.also: no gate or factor of this page reads {x!r}")
            return None
    if st.get("type") == "number":
        for d_ in st.get("derive") or []:
            if not (isinstance(d_, dict) and d_.get("attr") in reads and (_num(d_.get("when_at_most")) or _num(d_.get("when_at_least")))
                    and isinstance(d_.get("why"), str) and d_["why"].strip()):
                P.append(f"{w}.derive: {{attr: <read by the table>, when_at_most | when_at_least: <a number>, why: <its source>}}")
                return None
        return dict(st, kind="number")
    opts = st.get("options")
    if not isinstance(opts, list) or not opts:
        P.append(f"{w}: options [{{label, value}}], or type number, or sets")
        return None
    for j, o in enumerate(opts):
        if not (isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip() and "value" in o):
            P.append(f"{w}.options[{j}]: {{label, value}}")
            return None
        vals_ = o["value"] if isinstance(o["value"], list) else [o["value"]]
        for v in vals_:
            ok, typed = False, False
            for s, field, how in reads[a]:
                if how == "when":         # the gate applies when the value equals its "is", and not otherwise: any value of that type
                    ok |= any(isinstance(g, dict) and isinstance(g.get("when"), dict) and g["when"].get("attr") == a
                              and type(g["when"].get("is")) is type(v) for g in s["spec"].get("gates") or [])
                elif how in ("in_set", "match") and isinstance(field, str):
                    typed |= isinstance(v, str)
                    ok |= isinstance(v, str) and (_norm(v) in _held(s, field) or any(
                        _norm(v) in {_norm(x) for x in (g.get("any_values") or []) if isinstance(x, str)}
                        for g in s["spec"].get("gates") or [] if isinstance(g, dict)))
                elif how in ("age_range", "minus"):
                    ok |= _num(v)
                elif how == "bool":
                    ok |= isinstance(v, bool)
            if not ok and typed:          # B2 (review): text no record holds now (a record gone at the curveball) — named, kept
                P.append(f"{w}.options[{j}].value: {v!r} is held by no record of this run for {a} — kept; it matches none")
            elif not ok:
                P.append(f"{w}.options[{j}].value: {v!r} is no value the data holds for {a}")
                return None
        ex = o.get("attrs")
        if ex is not None and not (isinstance(ex, dict) and all(k in reads for k in ex)):
            P.append(f"{w}.options[{j}].attrs: attributes the table reads, got {sorted(ex) if isinstance(ex, dict) else ex!r}")
            return None
    return dict(st, kind="options")


def _intake(it, sets, P, w="intake", only=None):
    """The intake's (or the readiness tracker's) steps, each checked against the rules of the sets it feeds (`only`: their ids)."""
    if it is None:
        return None
    if not isinstance(it, dict) or not isinstance(it.get("steps"), list) or not it["steps"]:
        P.append(f"{w}: {{steps: [...]}}")
        return None
    use = [s for s in sets if only is None or s["spec"]["id"] in only]
    reads, ids, steps, seen = _reads(use), [s["spec"]["id"] for s in sets], [], set()
    for i, st in enumerate(it["steps"]):
        v = _check_step(st, f"{w}.steps[{i}]", reads, ids, P)
        if v and v["id"] in seen:
            P.append(f"{w}.steps[{i}].id: {v['id']} given twice")
            v = None
        if v:
            seen.add(v["id"])
            if isinstance(v.get("derive"), list):      # BUILD IC-8 (D12): a threshold's why names the data's fields in words
                fs_ = {k for s in use for r in s["recs"] for k in r if isinstance(k, str)}
                v = dict(v, derive=[dict(d_, why=fields_in_words(d_["why"], fs_)) if isinstance(d_, dict) else d_ for d_ in v["derive"]])
            steps.append(v)
    if len(steps) != len(it["steps"]):
        return None
    plan = []
    for i, ps in enumerate(it.get("plan") or []):                  # D3: the plan's parts (the intake's own; the readiness has none)
        wp = f"{w}.plan[{i}]"
        if not (isinstance(ps, dict) and isinstance(ps.get("title"), str) and ps["title"].strip()):
            P.append(f"{wp}: {{title, sets | call}}")
            return None
        bad = [x for x in (ps.get("sets") or []) if x not in ids] + ([ps["call"]] if ps.get("call") is not None and ps["call"] not in ids else [])
        if bad:                      # B2 (review): a set that did not run is left out of the part, named; the plan stands
            P.append(f"{wp}: {bad} not on this page (did not run) — left out")
            ps = dict(ps, sets=[x for x in (ps.get("sets") or []) if x in ids])
            if ps.get("call") is not None and ps["call"] not in ids:
                ps.pop("call")
            if not (ps.get("sets") or ps.get("call") or ps.get("documents") or ps.get("documents_days")):
                continue
        if ps.get("call") is not None:
            cs = next(s for s in sets if s["spec"]["id"] == ps["call"])
            if not isinstance((cs["spec"].get("contact") or {}).get("phone"), str):
                P.append(f"{wp}.call: {ps['call']} has no contact phone field")
                return None
        plan.append(ps)
    return {"steps": steps, "plan": plan}


def _checklist(spec, payloads, P):
    """D4: every record of the checklist's endpoint, in unlock order (a document before what it unlocks; ties by processing days, the
    longest first, then id). A cycle is named and listed once nothing outside it is free, before the records it blocks; a key that names
    no record, or a set's token no record unlocks, is named and left out; an unlocks value that is not a list of text is named and read
    as none. Every record renders whatever the keys say."""
    cl = spec.get("checklist")
    if cl is None:
        return None
    w = "checklist"
    if not isinstance(cl, dict) or cl.get("endpoint") not in (payloads or {}):
        P.append(f"{w}.endpoint: an endpoint of this run's data, got {(cl or {}).get('endpoint') if isinstance(cl, dict) else cl!r}")
        return None
    recs = [r for r in NR.records(payloads[cl["endpoint"]]) if isinstance(r, dict)]
    fields = cl.get("fields") if isinstance(cl.get("fields"), dict) else {}
    nf = cl.get("name_field")
    for k, f in [("name_field", nf)] + sorted(fields.items()):
        if not isinstance(f, str) or not any(f in r for r in recs):
            P.append(f"{w}.{k if k == 'name_field' else 'fields.' + k}: no record has {f!r}")
            return None
    if "unlocks" not in fields or "days" not in fields:
        P.append(f"{w}.fields: unlocks and days are required (the order reads them)")
        return None
    by, dup = {}, []
    for r in recs:                   # B2 (review): keyed as the sets key records (id, else name); a repeated key is named and kept apart
        k0 = k = str(r.get("id", r.get(nf)))
        n = 2
        while k in by:
            k, n = f"{k0} ({n})", n + 1
        if k != k0:
            dup.append(k0)
        by[k] = r
    if dup:
        P.append(f"{w}: records share the id {', '.join(sorted(set(dup)))} — each listed, the later ones marked (2), (3)")
    ul = {}
    for rid, r in by.items():        # B2 (review): the unlocks field as a list of text; anything else named and read as none
        v = r.get(fields["unlocks"])
        if v is None or (isinstance(v, list) and all(isinstance(t, str) for t in v)):
            ul[rid] = list(v or [])
        else:
            P.append(f"{w}: {rid} {fields['unlocks']} is {type(v).__name__}, not a list of text — read as none")
            ul[rid] = [t for t in v if isinstance(t, str)] if isinstance(v, list) else []
    keys = {}
    uk = cl.get("unlock_keys") if isinstance(cl.get("unlock_keys"), dict) else {}
    for tok, e in uk.items():           # a key that names no record, or not by its own name, is named and left out of the order
        if not (isinstance(e, dict) and str(e.get("id")) in by and quote_in(e.get("quote"), str(by[str(e.get("id"))].get(nf)))):
            P.append(f"{w}.unlock_keys.{tok}: {{id: <a record of {cl['endpoint']}>, quote: <that record's own name>}} — left out of the order")
            continue
        keys[tok] = str(e["id"])
    toks = {t for rid in by for t in ul[rid]}
    fs_ = {}
    for sid, ts in (cl.get("for_sets") if isinstance(cl.get("for_sets"), dict) else {}).items():
        ts = ts if isinstance(ts, list) else []
        bad = [t for t in ts if t not in toks]
        if bad:
            P.append(f"{w}.for_sets.{sid}: {bad} unlocked by no record — left out")
        fs_[sid] = [t for t in ts if t in toks]
    need = {rid: set() for rid in by}                              # rid → the records that unlock it
    for rid in by:
        for t in ul[rid]:
            if keys.get(t) and keys[t] != rid:
                need[keys[t]].add(rid)
    order, left, cyc = [], {rid: set(n) for rid, n in need.items()}, set()
    dkey = fields["days"]

    def dk(x):                       # B2 (review): ties longest first — the slow ones start first (unknown days last), then the id
        return (-by[x].get(dkey) if _num(by[x].get(dkey)) else 1e9), x

    def reach(u):                                                  # the records u needs, directly or through others, among those left
        seen, todo = set(), [u]
        while todo:
            for v in left.get(todo.pop(), ()):
                if v in left and v not in seen:
                    seen.add(v)
                    todo.append(v)
        return seen
    while left:
        ready = sorted((rid for rid, n in left.items() if not n), key=dk)
        if ready:
            rid = ready[0]
            order.append(rid)
            del left[rid]
            for n in left.values():
                n.discard(rid)
            continue
        # no record is free: the first cycle whose members need nothing outside it (by days, then id) goes next, named; the records it
        # blocks then follow in order (review: a record downstream of a cycle is not "in" it)
        rs = {u: reach(u) for u in left}
        comps = sorted({frozenset([u] + [v for v in rs[u] if u in rs[v]]) for u in left if u in rs[u]}, key=lambda c: min(dk(x) for x in c))
        c = next(c for c in comps if all(v in c for u in c for v in left[u]))
        P.append(f"{w}: {', '.join(sorted(c))} unlock each other (a cycle) — listed after the records they do not need")
        for rid in sorted(c, key=dk):
            order.append(rid)
            cyc.add(rid)
            del left[rid]
        for n in left.values():
            n.difference_update(c)
    items = []
    for rid in order:
        r = by[rid]
        items.append({"id": rid, "name": str(r.get(nf)), **{k: r.get(f) for k, f in fields.items()}, "unlocks": ul[rid],
                      "needs": sorted(need[rid]), "opens": sorted({keys[t] for t in ul[rid] if keys.get(t) and keys[t] != rid}),
                      "in_cycle": rid in cyc})
    return {"endpoint": cl["endpoint"], "label": cl.get("label") or "Documents and benefits", "items": items, "for_sets": fs_, "keys": keys,
            "fields": dict(fields)}


def _navigator(spec, sets, P, closed=None):
    nv = spec.get("navigator")
    if nv is None:
        return None
    s = next((x for x in sets if x["spec"]["id"] == (nv or {}).get("set")), None) if isinstance(nv, dict) else None
    if s is None:
        P.append(f"navigator.set: an option set of this page, got {(nv or {}).get('set') if isinstance(nv, dict) else nv!r}")
        return None
    for k in ("window_field", "lag_field"):
        if not any(nv.get(k) in r for r in s["recs"]):
            P.append(f"navigator.{k}: no record of {s['spec']['endpoint']} has {nv.get(k)!r}")
            return None
    fr = nv.get("free_fields") or []
    if not (isinstance(fr, list) and 1 <= len(fr) <= 2 and all(any(f in r for r in s["recs"]) for f in fr)):
        P.append("navigator.free_fields: one or two numeric fields its records hold")
        return None
    nf = s["spec"]["name_field"]
    rows = []
    for r in s["recs"]:
        hits = _ph_hits(s.get("ph"), r)
        rv = _ph_view(r, hits)                                          # B2: a declared placeholder is read as the rules read it
        vs = [rv.get(f) for f in fr]
        h0 = hits.get(fr[0])
        rows.append({"id": str(r.get("id", r.get(nf))), "name": str(r.get(nf)), "window": r.get(nv["window_field"]), "lag": r.get(nv["lag_field"]),
                     "free": None if (h0 and h0["reads_as"] == "no cap") else (vs[0] - (vs[1] if len(vs) == 2 else 0)) if all(_num(v) for v in vs) else None,
                     "free_note": f"no cap ({field_text(fr[0])} {_show(h0['v'])} in the data)" if h0 and h0["reads_as"] == "no cap" else None,
                     "closed": (closed or {}).get((s["spec"]["id"], str(r.get("id", r.get(nf))))) or None})   # IC-4 Phase 7 (review c1, D3)
    return {"set": s["spec"]["id"], "label": nv.get("label") or "Youth transition navigator", "window_field": nv["window_field"],
            "lag_field": nv["lag_field"], "free_fields": fr, "rows": rows}


def _readiness(spec, sets, checklist, P):
    rd = spec.get("readiness")
    if rd is None:
        return None
    if not isinstance(rd, dict) or rd.get("set") not in [x["spec"]["id"] for x in sets]:
        P.append(f"readiness.set: an option set of this page, got {(rd or {}).get('set') if isinstance(rd, dict) else rd!r}")
        return None
    st = _intake({"steps": rd.get("steps")}, sets, P, w="readiness", only={rd["set"]})
    if st is None:
        return None
    docs = []
    tok = rd.get("documents_for")
    if tok is not None:
        if not checklist or not any(tok in (it.get("unlocks") or []) for it in checklist["items"]):
            P.append(f"readiness.documents_for: {tok!r} is unlocked by no checklist record — the tracker shows no documents")
        else:
            docs = [it["id"] for it in checklist["items"] if tok in (it.get("unlocks") or [])]
    s = next(x for x in sets if x["spec"]["id"] == rd["set"])
    bf = rd.get("barriers_field")
    if bf is not None and not any(bf in r for r in s["recs"]):
        P.append(f"readiness.barriers_field: no record of {s['spec']['endpoint']} has {bf!r}")
        return None
    nf = s["spec"]["name_field"]
    rows = [{"id": str(r.get("id", r.get(nf))), "name": str(r.get(nf)), "barriers": r.get(bf) if bf else None} for r in s["recs"]]
    return {"set": rd["set"], "label": rd.get("label") or "Housing readiness", "steps": st["steps"], "documents": docs,
            "documents_for": tok, "barriers_field": bf, "storage_key": f"naipc-readiness-{spec.get('challenge')}", "rows": rows}


def for_page(spec, contract, payloads, announcement="", page="architect", base=None):
    """The Architect page's bundle (never raises): the validated sets of this page with each record's resolved values, the presets, and
    the intake, checklist, navigator and readiness parts. status "ok" | "no intake" (the page shows no Get Help tab) | "invalid" |
    "error"; problems are console lines."""
    try:
        problems, sets_all, presets = validate_spec(spec, contract, payloads, _block2(announcement), base)
        sets = [s for s in sets_all if _on_page(s["spec"], page)]
        if not sets:
            return {"status": "invalid", "problems": problems or ["no option set can run"]}
        closed, flags = closures(sets, payloads, announcement, among=sets_all)       # BUILD IC-5 (D5c): every set's names read
        users = _users(contract)
        intake = _intake(spec.get("intake"), sets, problems)
        checklist = _checklist(spec, payloads, problems)
        navigator = _navigator(spec, sets, problems, closed)
        readiness = _readiness(spec, sets, checklist, problems)
        pv = []
        for pr in presets:
            v = preset_view(pr, users)
            pv.append(dict(v, person={a: s_ for a, s_ in (pr.get("attrs") or {}).items() if isinstance(s_, dict) and s_.get("value") is not None}))
        names = {(s["spec"]["id"], str(r.get("id", r.get(s["spec"]["name_field"])))): str(r.get(s["spec"]["name_field"])) for s in sets for r in s["recs"]}
        return {"status": "ok" if intake else "no intake", "problems": problems, "challenge": spec.get("challenge"), "top_n": TOP_N,
                "sets": [_set_bundle(s, closed, flags) for s in sets], "presets": pv, "intake": intake, "checklist": checklist,
                "navigator": navigator, "readiness": readiness,
                "closures": [{"set": k[0], "id": k[1], "name": names.get(k, k[1]), **v} for k, v in sorted(closed.items())]}
    except Exception as e:  # noqa: BLE001 — a page is never lost to the engine
        return {"status": "error", "problems": [f"the engine stopped: {type(e).__name__}: {e}"]}


# ═══ BUILD B3 (P0.4, D1) — the sidecar's registry section: named figures Python computes from the records, the tags of a one-record
# endpoint's fields (class, window, cohort, unit), figures a record states only in words, and checks of a figure against an aggregate ═══
REG_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,60}$")
REG_CLASSES = ("stock", "flow", "rate", "duration", "money", "count")
REG_FMTS = ("count", "num", "money", "money_short", "pct", "pct100", "ratio", "per_dollar")
REG_TAG_KEYS = ("class", "window", "cohort", "unit", "fmt", "label", "assumption")


def _num_v(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def data_value(payloads, ref):
    """A data id by the registry's grammar → its number, or None: <endpoint>.<field> (a one-record endpoint),
    <endpoint>.<record id>.<field>, <endpoint>.count."""
    if not isinstance(ref, str):
        return None
    parts = ref.split(".")
    if len(parts) not in (2, 3) or parts[0] not in (payloads or {}):
        return None
    recs = [r for r in NR.records(payloads[parts[0]]) if isinstance(r, dict)]
    if len(parts) == 2:
        if parts[1] == "count":
            return len(recs)
        return recs[0].get(parts[1]) if len(recs) == 1 and _num_v(recs[0].get(parts[1])) else None
    rec = next((r for r in recs if str(r.get("id")) == parts[1]), None)
    return rec.get(parts[2]) if rec and _num_v(rec.get(parts[2])) else None


def _rec_text(rec):
    return " ".join(str(v) for v in rec.values() if isinstance(v, str)) if isinstance(rec, dict) else ""


def _quotes_ok(q, text):
    """A quote, or a list of quotes any one of which is the record's own words."""
    qs = q if isinstance(q, list) else [q]
    return any(quote_in(x, text) for x in qs)


def registry_section(spec, payloads, block2=False):
    """{problems, entries, tags, checks, notes} from the sidecar's optional "registry" section on this run's data (D1). An entry marked
    "when": "curveball" is computed only at Block 2. Every problem is 'registry…: reason'; an entry with a problem is not computed (the
    others are). A quote may be one string or a list (any one found). At Block 2 (B2's block rule) a classification quote the record no
    longer holds is a note and the entry computes; a text figure whose words are gone is dropped with a note. Never raises."""
    out = {"problems": [], "entries": [], "tags": {}, "checks": [], "notes": []}
    sec = (spec or {}).get("registry") if isinstance(spec, dict) else None
    if sec is None:
        return out
    P = out["problems"]
    if not isinstance(sec, dict):
        P.append("registry: an object")
        return out
    try:
        tags = sec.get("fields") or {}
        if not isinstance(tags, dict):
            P.append("registry.fields: an object of data id → tags")
            tags = {}
        for ref, tg in tags.items():
            w = f"registry.fields.{ref}"
            if not isinstance(tg, dict):
                P.append(f"{w}: an object")
                continue
            if data_value(payloads, ref) is None:
                P.append(f"{w}: no number at this id in the data")
                continue
            bad = [k for k in tg if k not in REG_TAG_KEYS + ("request",)]    # IC-3 (D7; step 56): "request" — the words the switch-on request
                                                                             # prints in place of a label's count; the page keeps the label's
            if bad:
                P.append(f"{w}: unknown key(s) {', '.join(bad)}")
            if tg.get("class") not in (None,) + REG_CLASSES or tg.get("fmt") not in (None,) + REG_FMTS:
                P.append(f"{w}: class must be one of {', '.join(REG_CLASSES)}; fmt one of {', '.join(REG_FMTS)}")
                continue
            out["tags"][ref] = {k: tg[k] for k in REG_TAG_KEYS if isinstance(tg.get(k), str) and tg[k].strip()}
        known = {}                        # id → (value, tags) of the named figures, for the formulas that follow

        def tag_of(ref):
            if ref in known:
                return known[ref][1]
            return out["tags"].get(ref) or {}

        def val_of(ref):
            if ref in known:
                return known[ref][0]
            return data_value(payloads, ref)

        for i, tf in enumerate(sec.get("text_figures") or []):
            w = f"registry.text_figures[{i}]"
            if not isinstance(tf, dict) or not REG_SLUG.match(str(tf.get("id", ""))):
                P.append(f"{w}: an object with an id (a slug)")
                continue
            if tf.get("when") == "curveball" and not block2:
                continue
            ep, rid, q, v = tf.get("endpoint"), tf.get("record"), tf.get("quote"), tf.get("value")
            recs = [r for r in NR.records((payloads or {}).get(ep)) if isinstance(r, dict)] if ep in (payloads or {}) else []
            rec = next((r for r in recs if str(r.get("id")) == str(rid)), None)
            if rec is None:
                P.append(f"{w}: record {rid!r} is not in {ep!r}")
                continue
            qs = q if isinstance(q, list) else [q]
            hit = next((x for x in qs if quote_in(x, _rec_text(rec))), None)
            if not _num_v(v) or not all(isinstance(x, str) and re.search(r"(?<![\d.])" + re.escape(f"{v:g}") + r"(?![\d])", x) for x in qs):
                P.append(f"{w}: each quote must hold the value {v!r}")
                continue
            if hit is None:
                if block2:
                    out["notes"].append(f"{w} ({tf['id']}): not used at the curveball — {rid}'s words no longer hold it")
                else:
                    P.append(f"{w}: the quote must be the record's own words")
                continue
            q = hit
            tg = {k: tf[k] for k in REG_TAG_KEYS if isinstance(tf.get(k), str) and tf[k].strip()}
            known[tf["id"]] = (v, tg)
            out["entries"].append(dict(tg, id=tf["id"], value=v, kind="MEASURED", source=f"{ep} {rid}: “{q}”", quote=q,
                                       inputs=[f"{ep}.{rid}"],     # BUILD IC-4 (D3): the record the text sits in, the figure its measure
                                       fact={"entity": {"endpoint": ep, "id": str(rid)}, "measure": tf["id"], "scope": "the record's text"}))

        def ev(x, w, refs, sums=None):
            """(value, formula text, class/window/unit tags) of an expression, or raise ValueError with the reason. BUILD IC-4 (D3):
            each sum it reads goes into `sums` (endpoint, records, what) — a named figure's entity."""
            if isinstance(x, str):
                v = val_of(x)
                if v is None:
                    raise ValueError(f"{x!r} is not a figure of this run's data or of this section")
                refs.append(x)
                return v, x, tag_of(x)
            if isinstance(x, dict) and isinstance(x.get("sum"), dict):
                s = x["sum"]
                ep, ids, f, mfield = s.get("endpoint"), s.get("records"), s.get("field"), s.get("minus")
                recs = [r for r in NR.records((payloads or {}).get(ep)) if isinstance(r, dict)] if ep in (payloads or {}) else None
                if recs is None:
                    raise ValueError(f"sum: no endpoint {ep!r}")
                pick = recs if ids == "all" else [r for r in recs if str(r.get("id")) in {str(i) for i in (ids or [])}]
                if ids != "all" and len(pick) != len(ids or []):
                    raise ValueError(f"sum: record(s) {sorted(set(map(str, ids or [])) - {str(r.get('id')) for r in pick})} not in {ep}")
                tot = 0
                for r in pick:
                    a, b = r.get(f), (r.get(mfield) if mfield else 0)
                    if not (_num_v(a) and _num_v(b)):
                        raise ValueError(f"sum: {r.get('id')}: {f}{' or ' + mfield if mfield else ''} is not a number")
                    tot += a - b
                refs.append(ep)
                what = f"{f} − {mfield}" if mfield else f
                if sums is not None:
                    sums.append({"endpoint": ep, "records": "all" if ids == "all" else [str(r.get("id")) for r in pick], "what": what})
                names = "all records" if ids == "all" else ", ".join(map(str, ids))
                return tot, f"Σ({what}) over {names} of {ep}", {}
            if isinstance(x, dict) and x.get("op") in ("+", "-", "*", "/") and isinstance(x.get("args"), list) and len(x["args"]) >= 2:
                vals = [ev(a, w, refs, sums) for a in x["args"]]
                op = x["op"]
                if op in ("+", "-"):
                    for k in ("class", "window", "unit"):
                        seen = {t.get(k) for _v, _f, t in vals if t.get(k)}
                        if len(seen) > 1 and not (k == "unit" and x.get("units_equal")):
                            raise ValueError(f"adds or subtracts across {k}es: {', '.join(sorted(seen))}" if k == "class" else
                                             f"adds or subtracts across {k}s: {', '.join(sorted(seen))}")
                v = vals[0][0]
                for vv, _f, _t in vals[1:]:
                    if op == "+":
                        v += vv
                    elif op == "-":
                        v -= vv
                    elif op == "*":
                        v *= vv
                    else:
                        if vv == 0:
                            raise ValueError("divides by zero")
                        v /= vv
                sym = {"+": " + ", "-": " − ", "*": " × ", "/": " ÷ "}[op]
                return v, "(" + sym.join(f for _v, f, _t in vals) + ")", next((t for _v, _f, t in vals if t), {})
            raise ValueError("a formula is a figure's id, {\"sum\": {...}} or {\"op\": \"+|-|*|/\", \"args\": [...]}")

        for i, en in enumerate(sec.get("entries") or []):
            w = f"registry.entries[{i}]"
            if not isinstance(en, dict) or not REG_SLUG.match(str(en.get("id", ""))):
                P.append(f"{w}: an object with an id (a slug)")
                continue
            if en.get("when") == "curveball" and not block2:
                continue
            w = f"registry.entries[{i}] ({en['id']})"
            if en["id"] in known:
                P.append(f"{w}: the id is taken")
                continue
            if en.get("class") not in (None,) + REG_CLASSES or en.get("fmt") not in (None,) + REG_FMTS:
                P.append(f"{w}: class must be one of {', '.join(REG_CLASSES)}; fmt one of {', '.join(REG_FMTS)}")
                continue
            bad_q = []
            for rid, q in (en.get("quotes") or {}).items():
                rec = next((r for ep in (payloads or {}) for r in NR.records(payloads[ep]) if isinstance(r, dict) and str(r.get("id")) == str(rid)), None)
                if rec is None or not _quotes_ok(q, _rec_text(rec)):
                    bad_q.append(str(rid))
            if bad_q and not block2:
                P.append(f"{w}: quotes not found in their records: {', '.join(bad_q)}")
                continue
            if bad_q:
                out["notes"].append(f"{w}: at the curveball the words quoted for {', '.join(bad_q)} are gone from the records; the selection is Block 1's")
            refs, sums = [], []
            try:
                v, ftext, _tg = ev(en.get("formula"), w, refs, sums)
            except ValueError as e:
                P.append(f"{w}: {e}")
                continue
            tg = {k: en[k] for k in REG_TAG_KEYS if isinstance(en.get(k), str) and en[k].strip()}
            known[en["id"]] = (v, tg)
            # BUILD IC-4 (D3): a figure that is one sum is a fact of the records it reads (the registry gives its scope and as-of);
            # any other is the sidecar's named figure (the registry's own rule)
            one = sums[0] if len(sums) == 1 and isinstance(en.get("formula"), dict) and "sum" in en["formula"] else None
            out["entries"].append(dict(tg, id=en["id"], value=v, kind="DERIVED", formula=ftext, inputs=list(dict.fromkeys(refs)),
                                       quotes=en.get("quotes") or {},
                                       fact={"entity": {"endpoint": one["endpoint"], "records": one["records"]}, "measure": one["what"]}
                                       if one else None))
            for c in en.get("checks") or []:
                cv = val_of(c)
                if cv is None:
                    P.append(f"{w}: check {c!r} is not a figure of this run")
                    continue
                out["checks"].append({"entry": en["id"], "against": c, "entry_value": v, "against_value": cv, "equal": abs(cv - v) < 1e-9})
    except Exception as e:  # noqa: BLE001 — a section no rule can read is a problem, never a crash
        P.append(f"registry: not readable ({type(e).__name__}: {str(e)[:80]})")
    return out


# ═══ BUILD B3 (D5, D6): the bottleneck dashboard and the age protocol — Python only, from the sidecar's sets, this run's records and the
# engine's own cases (the same validate_spec, closures and run result the prioritization model shows). Never raises. ═══
def _cap_gate(spec):
    return next((g for g in spec.get("gates") or [] if isinstance(g, dict) and g.get("rule") == "free_above_zero"), None)


def _restricts(g, r, spec):
    """The label of a gate that limits who a record's free units can serve, or None: an audience or condition list (unless it is open
    to anyone), an age range that is not every age, a yes/no requirement, a referral the set names."""
    rule = g.get("rule")
    v = r.get(g.get("field")) if g.get("field") else None
    if rule == "in_set":
        if v is None:
            return None if g.get("if_missing") == "pass" else g.get("label") or g["id"]
        vals = _vals(v) or []
        return None if set(vals) & {_norm(x) for x in g.get("any_values") or []} else (g.get("label") or g["id"])
    if rule == "age_range":
        b = age_bounds(v)
        return None if b == (0, 200) else (g.get("label") or g["id"])
    if rule == "bool" and isinstance(v, bool) and v != g.get("want"):
        return g.get("label") or g["id"]
    return None


def bottlenecks(spec, contract, payloads, res, announcement="", base=None):
    """BUILD B3 (D5, P2.6): per option set with a capacity gate (free_above_zero) shown on the Oracle — each record's listed, in-use and
    free capacity (a one-field gate such as a clinic's daily capacity: listed = free), whether it is closed (its status, the announcement
    or an alert — the engine's closures), the gates that limit who its free units can serve (none, the referral, an audience, an age, the
    site's own condition), and which of the brief's people can use it now (the engine's cases). Jam points: listed capacity with nothing
    free; free units none of the brief's people can use now; closed records — each with its record id and the gate's or closure's words.
    {status, sets: [{id, label, endpoint, fields, one_field, rows, totals}], jams, problems}. Never raises."""
    out = {"status": "ok", "sets": [], "jams": [], "problems": []}
    try:
        _P, sets_all, _pre = validate_spec(spec, contract, payloads, _block2(announcement), base)
        sets = [s for s in sets_all if _on_page(s["spec"], "oracle")]
        closed = {(str(c.get("set")), str(c.get("id"))): c for c in (res or {}).get("closures") or []}
        # IC-4 Phase 7 (review b11; step d): the engine's own closures beside the run result's — a rebuild from a checkpoint written
        # before the veto shows it, as a live run of this build does; the result's own entries stand (step g: a dict display — the keys
        # are (set id, record id) tuples, which a keyword unpacking refuses)
        eng_closed = {k: dict(v, set=k[0], id=k[1]) for k, v in closures(sets, payloads, announcement, among=sets_all)[0].items()}
        closed = {**eng_closed, **closed}
        reach = {}
        people = [p.get("name") or p.get("id") for p in (res or {}).get("presets") or []]
        why_not = {}    # B3 (Phase 4, review c F2): who can use a record is the engine's own per-person result — ranked, or to confirm
        for p in (res or {}).get("presets") or []:   # (the record's own words may allow it); who cannot, with the gate that left them out
            pn = p.get("name") or p.get("id")
            for c in p.get("cases") or []:
                for x in c.get("ranked") or []:
                    reach.setdefault((str(c.get("set")), str(x.get("id"))), []).append(pn)
                for x in c.get("confirm") or []:
                    reach.setdefault((str(c.get("set")), str(x.get("id"))), []).append(f"{pn} (to confirm: {x.get('gate')})")
                for x in c.get("excluded") or []:
                    why_not.setdefault((str(c.get("set")), str(x.get("id"))), []).append(
                        f"{pn}: {x.get('gate')}" + (f" ({str(x.get('detail'))[:80]})" if x.get("detail") else ""))
        for s in sets:
            sp = s["spec"]
            g = _cap_gate(sp)
            f = [x for x in (g or {}).get("fields") or [] if isinstance(x, str)]
            if not f:
                continue
            nf, ref = sp["name_field"], sp.get("referral")
            rows, tot = [], {"listed": 0, "in_use": 0, "free": 0, "no_cap": 0, "closed_free": 0}
            for r in s["recs"]:
                rid = str(r.get("id", r.get(nf)))
                hits = _ph_hits(s.get("ph"), r)
                a, b = r.get(f[0]), (r.get(f[1]) if len(f) > 1 else 0)
                no_cap = f[0] in hits and hits[f[0]]["reads_as"] == "no cap"
                if not no_cap and not (_num(a) and _num(b)):
                    out["problems"].append(f"{sp['id']}: {rid}: {' or '.join(f)} not a number — left out of the totals")
                    continue
                free = None if no_cap else (a - b if len(f) > 1 else a)
                lim = [lab for lab in (_restricts(x, r, sp) for x in sp.get("gates") or [] if isinstance(x, dict) and x is not g
                                       and x.get("rule") != "open_now") if lab]
                if isinstance(ref, dict) and r.get(ref.get("field")) == ref.get("is"):
                    lim.insert(0, "A referral first")
                k = (sp["id"], rid)
                row = {"id": rid, "name": str(r.get(nf) or rid), "listed": None if no_cap else a, "in_use": (b if len(f) > 1 else None),
                       "free": None if free is None else max(free, 0), "over": (-free if isinstance(free, (int, float)) and free < 0 else 0),
                       "no_cap": hits[f[0]]["why"] if no_cap else None, "closed": closed[k].get("why") if k in closed else None,
                       "limits": lim, "reach": reach.get(k, [])}
                rows.append(row)
                if no_cap:
                    tot["no_cap"] += 1
                else:
                    tot["listed"] += a
                    tot["in_use"] += b if len(f) > 1 else 0
                    tot["free"] += max(free, 0)
                    if k in closed:     # IC-4 Phase 7 (review b11, D3): a closed or vetoed record's free places — in the data's total, not offered
                        tot["closed_free"] += max(free, 0)
                if k in closed:
                    out["jams"].append({"set": sp["id"], "id": rid, "name": row["name"], "kind": "closed", "why": f"closed by {closed[k].get('why')}"})
                elif not no_cap and a > 0 and free <= 0:
                    out["jams"].append({"set": sp["id"], "id": rid, "name": row["name"], "kind": "full",
                                        "why": f"{' − '.join(field_text(x) for x in f)} = {free:g}" + (f" (more in use than listed, by {-free:g})" if free < 0 else "")})
                elif (no_cap or free > 0) and people and not row["reach"]:
                    out["jams"].append({"set": sp["id"], "id": rid, "name": row["name"], "kind": "unreachable",
                                        "why": "free now, but none of the brief's people can use it — " + (NR.join_clauses(why_not.get(k) or lim) or "no gate named")})
            out["sets"].append({"id": sp["id"], "label": sp.get("label") or sp["id"], "endpoint": sp["endpoint"], "fields": f,
                                "one_field": len(f) == 1, "rows": rows, "totals": tot, "people": people})
        if not out["sets"]:
            out["status"] = "no capacity gate"
    except Exception as e:  # noqa: BLE001 — a page is never lost to the dashboard
        out.update(status="error", problems=out["problems"] + [f"the dashboard stopped: {type(e).__name__}: {str(e)[:80]}"])
    return out


def age_protocol(spec, contract, payloads, res=None, announcement="", base=None):
    """BUILD B3 (D6, P2.5): the age cliffs of the Oracle's age-gated sets — every age where a record's age range begins, or where one
    ends (the age after its last), and at each such age, per set, the records that serve it, their free capacity and which records
    begin or end there. A range that does not parse ("varies") is named and left out. {status, ages: [{age, sets: {id: {records, free,
    begin, end}}}], sets: [{id, label}], problems}. Never raises."""
    out = {"status": "ok", "ages": [], "sets": [], "problems": []}
    try:
        _P, sets_all, _pre = validate_spec(spec, contract, payloads, _block2(announcement), base)
        sets = [s for s in sets_all if _on_page(s["spec"], "oracle") and any(isinstance(g, dict) and g.get("rule") == "age_range"
                                                                        for g in s["spec"].get("gates") or [])]
        spans, cliffs = {}, set()
        shut = ({(str(c.get("set")), str(c.get("id"))) for c in (res or {}).get("closures") or []}
                | set(closures(sets, payloads, announcement, among=sets_all)[0]))      # IC-4 Phase 7 (review b11; step d): the result's and the engine's
        for s in sets:
            sp = s["spec"]
            ag = next(g for g in sp["gates"] if isinstance(g, dict) and g.get("rule") == "age_range")
            cg = _cap_gate(sp)
            f = [x for x in (cg or {}).get("fields") or [] if isinstance(x, str)]
            nf = sp["name_field"]
            out["sets"].append({"id": sp["id"], "label": sp.get("label") or sp["id"], "field": ag.get("field")})
            for r in s["recs"]:
                rid = str(r.get("id", r.get(nf)))
                b = age_bounds(r.get(ag.get("field")))
                if b is None:
                    out["problems"].append(f"{sp['id']}: {rid}: {ag.get('field')} {r.get(ag.get('field'))!r} is not an age range — left out")
                    continue
                hits = _ph_hits(s.get("ph"), r)
                if f and f[0] in hits and hits[f[0]]["reads_as"] == "no cap":
                    free = None
                elif f and all(_num(r.get(x)) for x in f):
                    free = max(r[f[0]] - (r[f[1]] if len(f) > 1 else 0), 0)
                else:
                    free = 0
                spans.setdefault(sp["id"], []).append((b[0], b[1], rid, free, (sp["id"], rid) in shut))   # IC-4 Phase 7 (review b11): closed
                if b[0] > 0:
                    cliffs.add(b[0])
                if b[1] < 200:
                    cliffs.add(b[1] + 1)
        for a in sorted(cliffs):
            row = {"age": a, "sets": {}}
            for sid, sp_ in spans.items():
                inside = [x for x in sp_ if x[0] <= a <= x[1]]
                frees = [x[3] for x in inside]
                row["sets"][sid] = {"records": len(inside), "free": sum(v for v in frees if v is not None),
                                    "closed": sum(1 for x in inside if x[4]),               # IC-4 Phase 7 (review b11): closed or vetoed
                                    "closed_free": sum(x[3] for x in inside if x[4] and x[3] is not None),     # and their free places
                                    "no_cap": sum(1 for v in frees if v is None), "begin": [x[2] for x in sp_ if x[0] == a],
                                    "end": [x[2] for x in sp_ if x[1] + 1 == a]}
            out["ages"].append(row)
        if not sets:
            out["status"] = "no age-gated set"
    except Exception as e:  # noqa: BLE001
        out.update(status="error", problems=out["problems"] + [f"the age protocol stopped: {type(e).__name__}: {str(e)[:80]}"])
    return out


FIELD_GUIDE = """ENGINE SIDECAR — <challenge-id>.engine.json beside the contract (typed on the day with the contract, about ten minutes, the API's
records open). Validate it before Block 1:  python -B naipc_engine.py validate <contract.json> <data>   (<data>: a folder of the API's
JSON, one file per endpoint named <endpoint>.json; or checkpoints/data_loading.json; or --api-url <url> --endpoints a,b,c). A field or an
annotated record id the data does not hold stops that set at Block 1 (a typo); at Block 2 it is named and the set runs on what is left
when Block 1's data held it (the pipeline's runs/<challenge>/block1.json), and stops the set when Block 1's never did.
{"schema_version": 1, "challenge": "<the contract's challenge id>",
 "option_sets": [            one per endpoint whose records are options for a person: beds, clinics, pantries, programs, doors
   {"id": "<slug>", "label": "<plain words: A bed tonight>", "endpoint": "<endpoint>", "name_field": "<the record's name field>",
    "referral": {"field": "<a yes/no field>", "is": true},                                    optional: counted in the fairness check
    "annotations": {"<record id>": {"<field>": {"value": ..., "quote": "<the record's own words>"}}},  optional: a limit stated in text
    "holds": {"<record id>": {"<gate id>": {"quote": "<the record's own words>", "why": "<what to confirm>"}}},  optional (B2): where a
             record's words offer what the gate checks (deposit assistance), to confirm instead of out — never a pass
    "placeholders": {"<field>": {"value": 999, "reads_as": "no cap" | "not given", "why": "<shown on the page>"}},  optional (B2): a
             value the data uses as a placeholder; "no cap" only for the capacity a free_above_zero gate or a ratio factor reads first
    "gates": [               an option must pass every one; "label" optional
      {"rule": "in_set", "field": "<text or list>", "attr": "<the person's list>", "any_values": ["all"], "if_missing": "pass"},
      {"rule": "age_range", "field": "<'18+', '18-24', 'all'>", "attr": "age"},
      {"rule": "free_above_zero", "fields": ["<capacity>", "<in use>"]}     (or one field: a free count),
      {"rule": "open_now", "field": "<hours: '24/7', 'Mon-Fri 9:00 AM - 4:00 PM', 'Mon-Fri 8a-5p, Sat 8a-1p'>"},
      {"rule": "bool", "field": "<yes/no>", "want": true, "when": {"attr": "youth", "is": true}}],
    "factors": [             max points sum to exactly 100; every factor says why it counts
      {"id": "<slug>", "label": "...", "form": "bool", "field": "<yes/no>", "want": true, "max": 20, "why": "<urgency | fit | ...: ...>"},
      {"form": "ratio", "fields": ["<a>", "<b>"] (a − b) or "field": "<a number, or a list: its length>", "ref": <full points at>, ...},
      {"form": "inverse", "field": "<lower is better: wait weeks, a dropout rate>", "ref": <0 points at>, ...},
      {"form": "match", "field": "<text or list>", "attr": "<the person's list>", ...},
      {"form": "ratio", "range_top": "<an age range field>", "minus_attr": "age", "ref": 5, ...}   (years left before the age limit)],
    "tie_breakers": ["<factor id>", "..."],
    "pages": ["architect"],                                    optional (B2): the pages that show the set; default every page
    "contact": {"phone": "<field>", "hours": "<field>", "address": "<field>"},   optional (B2): what the plan prints to call or go
    "show": ["<field>", "..."]}],                              optional (B2): record fields the page shows word for word
 "intake": {"steps": [       optional (B2): the Architect's Get Help tab; each step feeds attributes the page's sets read
   {"id": "who", "label": "<the question>", "attr": "<attr>", "options": [{"label": "...", "value": <a value the data holds>,
                                                                          "attrs": {"<another attr>": <value>}}]},
   {"id": "age", "label": "...", "attr": "age", "type": "number", "min": 12, "max": 110,
    "derive": [{"attr": "youth", "when_at_most": 24, "why": "<the data's own limit>"}]},
   {"id": "need", "label": "...", "sets": ["<set id>", "..."], "multi": true},
   {"id": "known", "label": "...", "attr": "conditions", "multi": true, "options": [...]}],
  "plan": [{"title": "Tonight", "sets": ["<set id>"]}, {"title": "Next", "sets": [...], "documents": 3},
           {"title": "Tomorrow", "call": "<a set with contact.phone>", "documents_days": true}]},
 "checklist": {"endpoint": "<endpoint>", "name_field": "<field>", "label": "...",     optional (B2)
   "fields": {"unlocks": "<list field>", "days": "<number field>", "category": "...", "fee": "...", "blockers": "...", "help": "...", "notes": "..."},
   "unlock_keys": {"<token>": {"id": "<record id>", "quote": "<that record's own name>"}},
   "for_sets": {"<set id>": ["<token>", "..."]}},
 "navigator": {"set": "<set id>", "window_field": "<age range field>", "lag_field": "<field>", "free_fields": ["<a>", "<b>"]},  optional (B2)
 "readiness": {"set": "<set id>", "documents_for": "<token>", "barriers_field": "<field>", "steps": [<as the intake's>]},  optional (B2)
 "registry": {                optional (B3): the figures the pages cite by name, computed by Python from the records
   "fields": {"<endpoint>.<field>": {"class": "stock|flow|rate|duration|money|count", "window": "<the period it counts>",
              "cohort": "<who it counts>", "unit": "...", "fmt": "count|num|money|money_short|pct|pct100", "label": "..."}},
   "text_figures": [{"id": "<slug>", "endpoint": "<ep>", "record": "<id>", "quote": "<the record's words, holding the number>",
                     "value": 4, "unit": "...", "class": "...", "window": "...", "when": "curveball"}],   when: only at Block 2
   "entries": [{"id": "<slug>", "label": "...", "unit": "...", "fmt": "count", "class": "...", "window": "...", "cohort": "...",
                "formula": {"sum": {"endpoint": "<ep>", "records": ["<id>", ...] | "all", "field": "<a>", "minus": "<b>"}}
                        or {"op": "+|-|*|/", "args": ["<figure id>", {...}], "units_equal": "<why, when the units differ>"},
                "assumption": "<what the selection or the formula assumes>", "quotes": {"<record id>": "<its own words>"},
                "checks": ["<an aggregate id the data also gives>"]}]},   + and − refuse operands of different classes or windows
 "presets": [                one per named user the engine serves; illustrative ones only when the brief names none
   {"id": "<slug>", "named_user": "<the contract's id>", "attrs": {
      "age": {"value": 19, "basis": "stated", "from": "age"},
      "<attr>": {"value": ["..."], "basis": "stated", "from": "role" | "quote", "quote": "<words in that field of the contract>"},
      "<attr>": {"value": ["..."], "basis": "assumed", "reason": "<why — printed on the page>"}}},
   {"id": "<slug>", "named_user": null, "illustrative": true, "label": "Illustrative: ...",
    "attrs": {"<attr>": {"value": "...", "basis": "illustrative"}}}]}"""


def _load_data(args):
    """The data a validation reads: a folder of <endpoint>.json, a data_loading checkpoint, or --api-url with --endpoints."""
    if args and args[0] == "--api-url":
        import requests                                   # the harness's own dependency; only this path needs it
        url = args[1]
        eps = args[args.index("--endpoints") + 1].split(",") if "--endpoints" in args else []
        return {e.strip(): requests.get(NR.endpoint_url(url, e.strip()), timeout=15).json() for e in eps if e.strip()}
    p = Path(args[0])
    if p.is_dir():
        return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(p.glob("*.json"))}
    d = json.loads(p.read_text(encoding="utf-8"))
    return (d.get("data") or {}).get("raw") or d.get("raw") or {}


def _main(argv):
    if argv[:1] == ["guide"]:
        NR.say(FIELD_GUIDE)
        return 0
    if len(argv) < 3 or argv[0] != "validate":
        NR.say("usage: python -B naipc_engine.py validate <contract.json> <data folder | data_loading.json | --api-url URL --endpoints a,b>\n"
               "       python -B naipc_engine.py guide")
        return 2
    try:
        doc, payloads = NR.load(argv[1]), _load_data(argv[2:])
    except Exception as e:  # noqa: BLE001
        NR.say(f"not checked: {type(e).__name__}: {e}")
        return 2
    spec, why = load_spec(argv[1])
    if spec is None:
        NR.say(f"not checked: {why}")
        return 1
    res = run(spec, doc, payloads)
    reg = registry_section(spec, payloads)                 # BUILD B3 (D1): the registry section on the same data
    res["problems"] = list(res["problems"]) + reg["problems"]
    for p in res["problems"]:
        NR.say(f"  {p}")
    if spec.get("registry") is not None:
        NR.say(f"  registry: {len(reg['entries'])} named figure(s), {len(reg['tags'])} tagged field(s)"
               + "".join(f"; {c['entry']} {c['entry_value']:g} vs {c['against']} {c['against_value']:g}" for c in reg["checks"]))
    if res["status"] != "ok":
        NR.say(f"NOT OK: {len(res['problems'])} problem(s); no option set can run")
        return 1
    for p in res["presets"]:
        NR.say(f"  {p['who']}: " + " | ".join(f"{c['set']} → " + (f"{c['ranked'][0]['name']} ({c['ranked'][0]['score']})" if c["ranked"] else "none eligible")
                                             for c in p["cases"]))
    NR.say(("OK" if not res["problems"] else f"OK with {len(res['problems'])} problem(s) — the sets and presets named above do not run")
           + f": {len(res['sets'])} option set(s), {len(res['presets'])} preset(s), evaluated at {res['as_of']} ({res['as_of_source']})")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    NR.safe_stdio()
    raise SystemExit(_main(sys.argv[1:]))
