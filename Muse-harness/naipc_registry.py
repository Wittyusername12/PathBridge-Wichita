"""BUILD B3 (P0.4, P0.5) — the provenance registry and the integrity check, shared by the four harnesses: standard library only, one
byte-identical copy in each folder (tests/install_module.py).

The registry holds every figure a page shows: its value, unit, kind (MEASURED, DERIVED, EXTERNAL, MODELED), source or formula with its
inputs, assumption, class, window and cohort, and its one shown form. A page cites a figure by token: [[<id>]]. A number the model
proposes itself, which no id holds, is cited as [[model:<number>|<what it is>]] and registered MODELED with those words. Python
replaces each token with its shown form — on a page inside <span class="prov" data-prov="<id>">, in plain text as the bare form.
The checker reads a built page and counts every number in its visible text as cited, exempt or untraced; the sanity rules name a
residual across cohorts or windows, a unit word that contradicts its figure, and a token that names nothing. Nothing here raises on
bad input: a reader returns (None, reason), a resolver returns its findings.

BUILD IC-2 — the writer's forms, resolved here and nowhere else: [[q:<where>|<the number as the text writes it>]] (a figure quoted from
the organizers' text — a record's text field, the brief, the announcement — found by Python in that text and registered MEASURED, shown
in the text's own form); [[calc:<op>|<id a>|<id b>]] (a comparison Python computes from two registry figures, kind the weaker of
theirs; refused, and its sentence removed, when it cannot be computed or is false as named); [[nf:<the text>|<kind>]] (a declared
non-figure: shown as written and read as exempt, refused when it reads as a quantity). A quote or a declaration that is refused stays on
the page as the writer typed it and counts as having no link; its sentence is flagged.

BUILD IC-3 — one reader of quantities, quantities(): every piece of code that asks whether a stretch of text is a quantity, and what it
is, in digits or in words, asks it; shape, value and role are separate answers, and an expression no rule settles is "undecided" (listed,
never dropped). Its rules have a version (READER_VERSION) and a name each (rule_key)."""
import datetime
import hashlib
import html as _html
import unicodedata as _ud     # BUILD IC-8 (D1): the shown text is NFC
import json
import math
import os
import re
import sys
from collections import Counter          # BUILD IC-8 (d12c): the page's pass counts what it put in words
from html.parser import HTMLParser       # BUILD IC-8 (D11): the leak check reads the page as a reader does
from pathlib import Path

SCHEMA = 1
REGISTRY_FILE = "registry.json"
KINDS = ("MEASURED", "DERIVED", "EXTERNAL", "MODELED")          # strongest first: a computed figure takes its weakest input's kind
KIND_RANK = {k: i for i, k in enumerate(KINDS)}
CLASSES = ("stock", "flow", "rate", "duration", "money", "count")
FMTS = ("count", "num", "money", "money_short", "pct", "pct100", "ratio", "per_dollar", "year")   # B3 Phase 4: a year has no thousands comma
SENTINEL = ""            # stands for a token that names nothing (or a malformed one) until its sentence is removed
_TOK_ID = r"[A-Za-z_](?:[\w.:|%/-]*[\w%])?"
_TOK_IDLIKE = r"[A-Za-z][\w-]*[_.:][\w.:|%/-]*[\w%]"      # an id by its shape (a "_", "." or ":" in it), never a plain word
# BUILD IC-5 (D3, class T): a token's syntax left in text no resolver read — a whole token, an opening or a closing left alone (each with
# the id right against it, a word's end its end), a bare pair of brackets beside a space or a tag, the sentinel (an id-shaped word against it:
# it stands for either bracket); never a JSON array (its brackets hold no letter-led id, and its pairs stand against a digit or a quote)
_TOK_REM = re.compile(r"\[\[\s*[A-Za-z_][^\[\]<>\"\n]{0,160}?\s*\]\]"
                      r"|\[\[" + _TOK_ID + r"|" + _TOK_ID + r"\]\]"
                      r"|\ue000" + _TOK_IDLIKE + r"|" + _TOK_IDLIKE + r"\ue000"
                      r"|\[\[(?=\s|$|<)|(?:^|(?<=\s)|(?<=>))\]\]|\ue000")
# one gap: a run of remnants with the spaces and tabs beside them (never a line break) — strip_tokens closes it up where it stood
_TOK_GAP = re.compile(r"[ \t]*(?:" + _TOK_REM.pattern + r")(?:[ \t]*(?:" + _TOK_REM.pattern + r"))*[ \t]*")


def strip_tokens(t, note=None):
    """BUILD IC-5 (D3, class T): text with every token remnant out (_TOK_REM: a token the resolver never read, a half one with its id,
    a bare pair of brackets, the sentinel), each passed to note(remnant). Each gap is closed up where it stood and nowhere else: nothing
    at a line's start or end, after an opening bracket or before punctuation; one space between words that had one. The model's words
    only (out_text's text, a registry label, the diagram's source) — never code or a JSON document: its brackets are its own (finalize
    never calls it). Not text → as it is."""
    if not isinstance(t, str) or not ("[[" in t or "]]" in t or SENTINEL in t):
        return t

    def gone(m):
        if note is not None:
            for r in _TOK_REM.finditer(m.group(0)):
                note(r.group(0))
        g, a, b = m.group(0), m.start(), m.end()
        before, after = (t[a - 1] if a else "\n"), (t[b] if b < len(t) else "\n")
        if before in "\r\n([" or after in "\r\n.,;:!?)]":
            return ""
        return " " if g[:1] in " \t" or g[-1:] in " \t" else ""
    return _TOK_GAP.sub(gone, t)
TOKEN_RE = re.compile(r"\[\[([^\[\]]{1,240}?)\]\]")
PROPOSAL_ID = re.compile(r"(?:b1\.)?model\.(?:\d+|[0-9a-f]{6,40})")   # BUILD IC-4 (D1): a proposal's id — IC-3's model.<n> or this build's model.<hash>
ALIASES = {"COI_3YR": "coi_3yr"}  # P0.3's cost-of-inaction token, accepted for one build (B3, D2)
KIND_WORDS = {"MEASURED": "measured in the data", "DERIVED": "computed by Python from the data", "EXTERNAL": "an outside source",
              "MODELED": "an assumption"}
UNIT_NOUNS = {"person": "person", "people": "person", "persons": "person", "family": "family", "families": "family",   # B3 (Phase 4, review b
              "household": "household", "households": "household", "patient": "person", "patients": "person",           # F4): the unit FAMILY —
              "beneficiary": "person", "beneficiaries": "person", "resident": "person", "residents": "person",           # every word for one
              "client": "person", "clients": "person", "youth": "person", "student": "person", "students": "person",     # person is one unit;
              "participant": "person", "participants": "person", "individual": "person", "individuals": "person"}        # family, household differ
# BUILD IC-2 (D2–D4): the writer's forms
QUOTE_MAX = 40                     # a quote's text: the number as the source writes it, with at most a few words around it
QUOTE_PLACES = ("brief", "announcement")
# BUILD IC-3 (D3): QUOTE_WORDS is gone — a quote in words is read by the one reader (quantities()), any number words it reads
SHAPE_WORDS = {"range": "a range", "ratio": "a ratio", "bound": "a bound", "multiplier": "a multiplier", "recurrence": "a recurrence", "single": "a hedged figure"}
CALC_OPS = {"diff": "a − b", "pct_of": "a as a percent of b", "pct_less": "how far a is below b, as a percent of b",
            "pct_more": "how far a is above b, as a percent of b", "times": "a ÷ b", "pp": "the gap between two percentages, in points"}
NF_KINDS = {"age": "an age", "year": "a year", "date": "a date", "time": "a time", "name": "a name", "code": "a code",
            "phone": "a phone or service number"}
DECLARED_WHY = "declared by the writer: "      # + the kind's words — the reason the checker reads on a declared non-figure
FORM_PREFIXES = ("model:", "q:", "calc:", "nf:", "count:")     # BUILD IC-4 (D13): + how many items a list of the page shows


# ═══ numbers: the one parser and the one formatter ═══
_NUM_TEXT = re.compile(r"^\s*([-−]?)\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)\s*(%|k|m|b|thousand|million|billion)?\s*$", re.I)
_MULT = {"": 1.0, "%": 0.01, "k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def parse_number(v):
    """A number as written → float, or None: 750000, "750,000", "$1.2M", "$1.2 million", "750K", "38%" (→ 0.38), "−4". Never raises."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if not isinstance(v, str):
        return None
    m = _NUM_TEXT.match(v.replace(" ", " "))
    if not m:
        return None
    try:
        x = float(m.group(2).replace(",", "")) * _MULT[(m.group(3) or "").lower()]
    except ValueError:
        return None
    return -x if m.group(1) else x


def _trim(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def money_short(n):
    """'$500K', '$1.25M', '$999' — the form of naipc_requirements.money_short (the P&L, the budget)."""
    if n is None or not math.isfinite(n):
        return ""
    n = round(n)
    sign, a = ("-" if n < 0 else ""), abs(n)
    if a >= 1_000_000:
        return f"{sign}${_trim(a / 1_000_000)}M"
    if a >= 1_000:
        return f"{sign}${_trim(a / 1_000)}K"
    return f"{sign}${a}"


def sig2(v):
    """Two significant figures, never a nonzero value shown as 0: 0.193 → '0.19', 0.04 → '0.040', 3.21 → '3.2', 12.4 → '12'."""
    if v is None or not math.isfinite(v):
        return ""
    if v == 0:
        return "0"
    s, a = ("-" if v < 0 else ""), abs(v)
    if a >= 10:
        return s + f"{math.floor(a + 0.5):,}"
    d = max(1, 1 - math.floor(math.log10(a)))
    out = f"{a:.{d}f}"
    if a < 1 <= float(out):              # B3 (Phase 4, review a F7): 0.9951 is "0.995", never "1.00" — a ratio below 1 stays below 1
        out = f"{math.floor(a * 1000) / 1000:.3f}"
    return s + (f"{math.floor(a + 0.5):,}" if float(out) >= 10 else out)


def _whole(v):
    return f"{math.floor(abs(v) + 0.5):,}"


def fmt_value(v, fmt):
    """The one shown form of a value in a format (FMTS). Not a number → ''."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return ""
    neg = "-" if v < 0 else ""
    if fmt == "count":
        return neg + _whole(v)
    if fmt == "money":
        a = abs(v)
        return f"{neg}${_whole(a)}" if a >= 100 or float(a).is_integer() else f"{neg}${a:,.2f}"
    if fmt == "money_short":
        return money_short(v)
    if fmt == "pct":
        return f"{neg}{_trim(abs(v) * 100)}%"
    if fmt == "pct100":
        return f"{neg}{_trim(abs(v))}%"
    if fmt == "ratio":
        return f"{sig2(v)} : 1"
    if fmt == "per_dollar":
        return f"{neg}${abs(v):,.2f}"
    if fmt == "year":
        return f"{neg}{math.floor(abs(v) + 0.5)}"
    a = abs(v)                                                       # "num"
    return neg + (_whole(a) if float(a).is_integer() else f"{a:,.2f}".rstrip("0").rstrip("."))


def _words(field):
    """A field name's words, lower case: camelCase and snake_case split ("lifeExpectancy" → life, expectancy; "fee_usd" → fee, usd)."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(field or ""))
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", s) if w}


KEY_WORDS = {"id", "zip", "code", "tract", "stop", "number", "fips", "geoid", "area"}       # a whole-number field that names a record
MONEY_WORDS = {"rent", "rents", "cost", "costs", "usd", "price", "prices", "fee", "fees", "income", "wage", "wages", "salary", "salaries", "budget",
               "dollar", "dollars", "spend", "spending", "revenue", "payment", "payments"}
RATE_WORDS = {"rate", "share", "ratio", "margin", "proportion", "fraction"}


def guess_fmt(field, value):
    """A data field's format when the sidecar names none, by the field's own words (B3 Phase 4, review a F6 — never a substring:
    "lifeExpectancy" is no fee): a year (a year word, a whole 1800–2200) without a comma; a share 0–1 by a rate or percent word, a margin
    or rate from −1 to 1 too, as a percentage; a percent word above 1 as written; a money word as money; else a count or a number."""
    w = _words(field)
    if w & {"year", "yr"} and float(value).is_integer() and 1800 <= value <= 2200:
        return "year"
    if isinstance(value, float) and ((0 <= value <= 1 and w & (RATE_WORDS | {"pct", "percent"})) or (-1 <= value < 0 and w & RATE_WORDS)):
        return "pct"
    if w & {"pct", "percent"}:
        return "pct100"
    if w & MONEY_WORDS:
        return "money"
    return "count" if float(value).is_integer() else "num"


# ═══ BUILD IC-4 (D3): the fact key — what a figure is, apart from its id ═══
FACT_TIME_FIELDS = ("asOf", "updatedAt", "lastUpdated", "timestamp", "reportDate")


def as_of_of(rec=None, payload=None):
    """BUILD IC-4 (D3): the one rule for a figure's as-of — the record's own time field, else its endpoint's (the envelope), in
    FACT_TIME_FIELDS' order: {"value", "from"}; None when neither holds one (the caller then names the fetch)."""
    for where, obj in (("the record", rec), ("the endpoint", payload)):
        if isinstance(obj, dict):
            for f in FACT_TIME_FIELDS:
                v = obj.get(f)
                if isinstance(v, str) and v.strip():
                    return {"value": v.strip(), "from": f"{where}'s {f}"}
    return None


# record_name: a record's name by one rule, for a fact's entity and for a reader — defined once, beside the reader's words (BUILD IC-8
# d11c: d11a defined it a second time, record_name(ep, rec), which replaced IC-4's record_name(rec) here for every caller)


def fact_key(fact):
    """BUILD IC-4 (D3): the key every value of one fact shares — the entity (its endpoint and id, or what it is), the measure, the unit,
    the scope, the as-of and the scenario, as one string; a quoted figure's claim with them (D1's identity of a quote: two quantities
    of one text are two facts). The entity's name is shown, never keyed: a renamed record is the same record."""
    if not isinstance(fact, dict):
        return None
    ent = {k: v for k, v in (fact.get("entity") or {}).items() if k != "name"}
    asof = fact.get("as_of")
    asof = asof.get("value") if isinstance(asof, dict) else asof
    return json.dumps([ent, fact.get("measure"), fact.get("unit") or "", fact.get("scope"), asof, fact.get("scenario"), fact.get("claim")],
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


CATEGORY_WORDS = (   # BUILD IC-4 (D7): one table of categories in plain words — every page and the run's report, in this order
    ("data", "from the data"), ("announcement", "from the announcement"), ("external", "from an outside source"),
    ("assumption", "the writer's assumption"), ("python", "computed by Python"), ("design", "the program's own design"),
    ("scenario", "a scenario's starting point"), ("constant", "a comparison constant"), ("disagree", "sources disagree"),
    ("unavailable", "a source did not load"),
    ("changed", "changed since Block 1"), ("none", "no source found"), ("not_quantity", "not a quantity"))
PY_BLOCK = "Python's words and counts, from the data"        # BUILD IC-4 (D7): whose a page region's numbers are (data-prov-block)
DESIGN_BLOCK = "the program's own design: the published table's rules and the questions it asks"
ORG_BLOCK = "the organizers' words (the brief)"
BLOCK_CATEGORY = {DESIGN_BLOCK: "design", ORG_BLOCK: "data"}    # any other block: computed by Python
_D4_KEYS = {"design commitment": "design", "scenario starting point": "scenario", "not a quantity": "not_quantity",
            "comparison constant": "constant", "the writer's assumption": "assumption",      # BUILD IC-5 (D6)
            "structural count, checked": "python", "structural count, does not match": "disagree"}    # IC-4 Phase 7 (review b4): the
# sentence's count against Python's count of its list — two sources that disagree, never a figure Python computed


def category_counts(chk, registry=None, model_only=False):
    """BUILD IC-4 (D7): each number of a checked page (check_page's numerals) by the table's category — {key: n}; model_only: the
    model's prose only (the Oracle's W). The undecided and the vague are not counted here (each has its own list)."""
    out = {}
    for x in (chk or {}).get("numerals") or []:
        if model_only and x.get("author") not in ("model", None):
            continue
        st, key = x.get("status"), None
        if st == "cited" and x.get("id"):
            e = registry.get(x["id"]) if registry is not None else None
            if e is None:
                key = "none"
            elif registry.disagreeing(e["id"]):
                key = "disagree"
            elif e.get("stale") or e.get("kind") == "STALE":
                key = "changed"
            elif e.get("kind") == "EXTERNAL":
                key = "external"
            elif e.get("kind") == "MODELED":
                key = "assumption"
            elif e.get("kind") == "DERIVED":
                key = "python"
            elif e.get("quoted") and e.get("where") == "announcement":
                key = "announcement"
            elif str(e["id"]).split(".")[0] in (getattr(registry, "unavailable", None) or set()):
                key = "unavailable"
            else:
                key = "data"
        elif st == "cited":
            key = BLOCK_CATEGORY.get(x.get("block"), "python")
        elif st == "authored":
            key = "data" if x.get("by") == "org" else "python"
        elif st == "listed":
            key = _D4_KEYS.get(x.get("category"))
        elif st == "exempt" and x.get("role") in ("name", "marker", "position", "idiom"):
            key = "not_quantity"
        elif st in ("untraced", "unattributed"):     # IC-4 Phase 7 (review b8): a Block 1 mention the data no longer holds, by its status
            key = {"source unavailable": "unavailable"}.get(x.get("stale"), "changed") if x.get("stale") else "none"
        if key:
            out[key] = out.get(key, 0) + 1
    return out


def category_parts(counts, notes=None):
    """BUILD IC-4 (D7): the nonzero categories in the table's order, in plain words ("161 from the data; 7 from an outside source");
    notes: {category: words} printed beside it in parentheses (IC-4 Phase 7, review c3: "35 computed by Python (6 of them counts of a
    list, checked)", so the clause's sums can be told from the line's)."""
    notes = notes or {}
    return "; ".join(f"{counts[k]:,} {w}" + (f" ({notes[k]})" if notes.get(k) else "") for k, w in CATEGORY_WORDS if counts.get(k))


def category_line(counts, notes=None):
    """BUILD IC-4 (D7): "By category: …" — the nonzero categories in the table's order, in plain words; "" when none. A clause without its
    stop: the caller ends the sentence (D8: a join keeps one terminal stop)."""
    parts = category_parts(counts, notes)
    return ("By category: " + parts) if parts else ""


LINE_SLOT = "<!--naipc:integrity-line-->"   # BUILD IC-4 (D7): where a page other than the Oracle's prints its integrity line


def page_line(chk, registry=None):
    """BUILD IC-4 (D7): the integrity line of a page other than the Oracle's, from its own text — every number the one reader read on it,
    by the table's category (category_counts over the whole page); the numbers it could not read as a number or a name named apart.
    One or two sentences, each with its stop."""
    c = category_counts(chk, registry)
    n, k = sum(c.values()), len((chk or {}).get("undecided") or [])
    more = f" {k:,} more could not be read as a number or a name." if k else ""
    if not n:
        return "Data integrity: no number read on this page." + more
    return f"Data integrity: of {n:,} numbers read on this page, " + category_parts(c) + "." + more


def place_page_line(html_text, registry=None, exempter=None):
    """BUILD IC-4 (D7): a page other than the Oracle's with its integrity line at the template's slot (LINE_SLOT, inside an element whose
    own counts are exempt), computed from the page's own text by the one reader (check_page, with the data's exempter) and the one
    registry (the Oracle's of this run). Returns (html, line); a page with no slot comes back as it is, with None."""
    html_text, line, _chk = _placed_line(html_text, registry, exempter)
    return html_text, line


def place_page_line_numerals(html_text, registry, exempter, path, run_id):
    """BUILD IC-5 (D9): an authored page's line placed as place_page_line places it, and its numerals — each with its author and status,
    the marks' record (the served page holds no mark) — written to `path` (write_numerals), so the line can be recomputed. Returns
    (html, line, why the numerals file was not written, or None)."""
    html_text, line, chk = _placed_line(html_text, registry, exempter)
    return html_text, line, (write_numerals(path, chk, run_id) if chk is not None else "the page has no line slot")


def _placed_line(html_text, registry, exempter):
    if LINE_SLOT not in html_text:
        return html_text, None, None
    chk = check_page(html_text, exempter, registry)
    line = page_line(chk, registry)
    return html_text.replace(LINE_SLOT, esc(line), 1), line, chk


NUMERALS_FILE = "integrity_numerals.json"     # BUILD IC (D2): an authored page's numerals, in its checkpoints (the Muse's since IC-5 D9)


def write_numerals(path, chk, run_id):
    """BUILD IC (D2), the one writer since BUILD IC-5 (D9): every numeral of a built page with its author and status — the claim its line
    makes, numeral by numeral (no marks) — and how many expressions it could not read. None, or why it was not written. Never raises."""
    try:
        doc = {"schema": 1, "run_id": run_id, "authored": bool(chk.get("authored")), "by_author": chk.get("by_author") or [],
               "numerals": chk.get("numerals") or [], "undecided": len(chk.get("undecided") or [])}
        p = Path(path)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes((json.dumps(doc, ensure_ascii=False, indent=0) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def line_of_numerals(doc, registry=None):
    """BUILD IC-5 (D9): an authored page's integrity line from its numerals file (the served page holds no mark) — page_line over the
    file's numerals and its count of expressions not read."""
    doc = doc if isinstance(doc, dict) else {}
    return page_line({"numerals": doc.get("numerals") or [], "undecided": [None] * int(doc.get("undecided") or 0)}, registry)


def inline_svg(svg):
    """BUILD IC-4 (D7, D8): an SVG for a page's body — its XML declaration and doctype dropped (inside HTML they are not markup: the
    reader read the declaration's "1.0" as a number on every page with a QR code)."""
    return re.sub(r"(?s)^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!DOCTYPE[^>]*>\s*)?", "", svg) if isinstance(svg, str) else svg


def page_line_of(html_text):
    """BUILD IC-4 (D7): the integrity line a page other than the Oracle's prints (its data-integrity-line element), or None."""
    m = re.search(r'<div class="integrity-line" data-integrity-line[^>]*>(.*?)</div>', html_text or "", re.S)
    return _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None


def oracle_raw(checkpoints_dir):
    """BUILD IC-4 (D7): the data of the Oracle's run (its checkpoints/data_loading.json's raw), for a page's exempter and the one
    registry's data ids by grammar; None when it is not readable."""
    try:
        d = admit(json.loads((Path(checkpoints_dir) / "data_loading.json").read_text(encoding="utf-8")), "data", "data_loading.json")   # BUILD IC-5 (D1)
        raw = (d.get("data") or {}).get("raw") if isinstance(d, dict) else None
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def as_of_words(asof):
    """BUILD IC-4 (D3, D7): a fact's as-of in plain words for a page — "as of <the record's or the endpoint's own time>", else "fetched in
    this run" / "made in this run" with the run's time in words (never the run's id); "" with none."""
    if not isinstance(asof, dict) or not asof.get("value"):
        return ""
    fr, v = str(asof.get("from") or ""), str(asof["value"])
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z", v)      # a run's id: its time in words
    if fr.startswith(("fetched ", "made ")):
        return fr + (f", {m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]} UTC" if m else "")
    if m:
        return f"as of {m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]} UTC"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?", v)
    if m:
        return f"as of {m[1]} {m[2]}" + (" UTC" if m[3] == "Z" else (f", UTC{m[3]}" if m[3] else ""))
    return f"as of {v}"


_CODE_RX = re.compile(r"\b[\w-]+\.(?:py|js|mjs|j2)\b|\bnaipc_\w+|\b\w+_harness\b|\b(?:NR|REG|ENG)\.\w+|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
                      r"|\b[a-z_][a-z0-9_]*\(\)|(?i:\bviolations?\b)"
                      r"|\[\s*(?:ESTIMATE|BENCHMARK)\b")     # BUILD IC-4 run turn (R6): a prompt's tag of one word (CHALLENGE_DATA has its "_")
_CREDITED = {"Chart.js"}            # a library's product name the page credits — not this program's code


def _reader_texts(html_text):
    """The text a judge reads on a page — its text without script, style, noscript, template, comments or head (the diagram's source,
    drawn in the browser, left out) — and every title attribute, each unescaped (BUILD IC-4's reading in code_names; BUILD IC-5 D2 (h):
    one reading for code_names and page_leaks)."""
    h = re.sub(r"(?is)<!--.*?-->", " ", html_text or "")
    h = re.sub(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1>", " ", h)
    h = re.sub(r'(?is)<pre class="mermaid"[^>]*>.*?</pre>', " ", h)
    h = re.sub(r"(?is)<head\b.*?</head>", " ", h)
    return [_html.unescape(re.sub(r"<[^>]+>", " ", h))] + [_html.unescape(t) for t in re.findall(r'\stitle="([^"]*)"', h)]


def code_names(html_text):
    """BUILD IC-4 (D7, done-test 6): every code name or "violation" in the text a judge reads — the reader's text (no script, style,
    comment or head; the diagram's source, drawn in the browser, left out) and every title attribute: [(name, its context)]."""
    texts = _reader_texts(html_text)
    out = []
    for t in texts:
        for m in _CODE_RX.finditer(t):
            if m.group(0) not in _CREDITED:
                out.append((m.group(0), " ".join(t[max(0, m.start() - 80):m.end() + 60].split())))
    return out


def page_leaks(html_text):
    """BUILD IC-5 (D2 h): what a judge would read on a page that is this program's own and not the page's — every code name or
    "violation" (code_names) and every tag of the model's (a bracket of any form, then a tag word, any case: _TAG_START) in the reader's
    text and title attributes: {"code_names": [[name, context]], "tags": [[tag, context]]}. The pipeline writes them to the hub's
    checkpoints/leaks.json; under NAIPC_ACCEPT=1 one fails the run."""
    tags = []
    for t in _reader_texts(html_text):
        for m in _TAG_START.finditer(t):
            tags.append([m.group(0), " ".join(t[max(0, m.start() - 80):m.end() + 60].split())])
    return {"code_names": [list(x) for x in code_names(html_text)], "tags": tags}


# ═══ BUILD IC-8 (D11, D12, I7): the words a reader meets for a data field, an endpoint and a record — and the leak check that reads
#      what a reader sees ═══
FIELD_ACRONYMS = {"id": "ID", "ids": "IDs", "hud": "HUD", "coc": "CoC", "zip": "ZIP", "er": "ER", "sroi": "SROI", "kpi": "KPI", "va": "VA",
                  "ada": "ADA", "snap": "SNAP", "tanf": "TANF", "wic": "WIC", "lgbtq": "LGBTQ", "vash": "VASH", "usd": "USD", "yoy": "YoY",
                  "dcf": "DCF", "api": "API", "url": "URL", "pct": "percent", "ft": "full-time"}
NAME_FIELDS = ("name", "title", "label", "providerName", "programName", "siteName")     # D12: a record's name, in this order


def field_words(name):
    """BUILD IC-8 (D12): a data field's name in words — the one function for every text that names a data field to a reader. camelCase,
    snake_case and kebab-case split and lower-cased, the acronyms of FIELD_ACRONYMS in their own form (made-up names): needsSampleReview →
    "needs sample review", sampleCountAge12 → "sample count age 12", unitPriceUsd → "unit price USD", yoyGrowthPct → "YoY growth percent"."""
    out = []
    for part in re.split(r"[_\s\-]+", str(name or "")):
        for w in re.findall(r"[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+[a-z]{1,3}(?![a-z\d])|\d+", part):
            out.append(FIELD_ACRONYMS.get(w.lower(), w.lower()))
    return " ".join(out)


def endpoint_words(slug):
    """BUILD IC-8 (D12): an endpoint's slug in words — sample-feed-status → "sample feed status" (a made-up slug)."""
    return field_words(str(slug or "").strip("/").rpartition("/")[2])


def record_name(rec, ep=None):
    """BUILD IC-4 (D3); IC-8 (D12, d11c): a record's name by one rule, for a fact's entity and for a reader — its first present text field
    of NAME_FIELDS (BUILD_PROMPT_IC8 D12's list, in its order), then any other field whose name ends in "Name" (IC-4's rule), then an
    alert's headline, its white space collapsed. None present: "<endpoint in
    words> record" when the endpoint is given (a reader's words), else None (a fact's entity has no name). On the three challenges'
    fixtures no record holds two of these fields: the order changes no name IC-4's rule gave (t_defs_ic8 DF-3)."""
    if isinstance(rec, dict):
        keys = list(NAME_FIELDS) + [k for k in rec if isinstance(k, str) and k.endswith("Name") and k not in NAME_FIELDS] + ["headline"]
        for k in keys:
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())
    return f"{endpoint_words(ep)} record" if ep else None


_ID_FORM = re.compile(r"(?=[\w-]*[A-Za-z])(?=[\w-]*\d)[\w-]+")       # a record id a reader would read as one: a letter and a digit, no space


_HTML_SPELL = {"'": "(?:'|&#39;|&#x27;|&apos;)", '"': '(?:"|&quot;|&#34;|&#x22;)', "&": "&(?:amp;|#38;|#x26;)", "<": "&(?:lt;|#60;|#x3[cC];)",
               ">": "&(?:gt;|#62;|#x3[eE];)"}


def _html_text_rx(s):
    """BUILD IC-8 (P4.4, p44w): a regex for text `s` as HTML holds it — each character that HTML escapes in any of its spellings (the
    Oracle's _esc writes an apostrophe &#x27;, REG.esc &#39;), every other character as itself."""
    return "".join(_HTML_SPELL.get(c) or re.escape(c) for c in str(s))


def _vocab_rx(terms):
    ts = sorted({t for t in terms if t}, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in ts) + r")(?![\w-])") if ts else None


class ReaderNames:
    """BUILD IC-8 (D11, D12): a run's data in a reader's words. `ids`: every record id of the data — each endpoint's records by its record
    key ("id" when every record holds a distinct one; an endpoint of one record has none), an id of id form (_ID_FORM: a letter and a
    digit) — → its record's name (record_name); `endpoints`: every endpoint slug → its words; `fields`: every field name that is not one
    plain lowercase word (the 13 of Wichita's 88 that are — address, count, … — are words on a page) → its words (field_words). `vocab`
    holds the three sets the leak check matches exactly at word boundaries; find() lists a text's terms; rewrite() and words() put each
    term of a text in its words (a record's id → its name, an endpoint → its words, a field → its words: d12c, one pass)."""
    CLASSES = ("ids", "endpoints", "fields")

    def __init__(self, raw, records=None, before=None):
        """before (BUILD IC-8 d12c): Block 1's data at Block 2 — a record, an endpoint or a field the update removed keeps its words (a
        change list names it: "removed: <its id>"); this run's names first."""
        self.ids, self.endpoints, self.fields = {}, {}, {}
        recs_of = records or (lambda p: p.get("data") if isinstance(p, dict) and isinstance(p.get("data"), list) else [])
        for data in (raw, before):
            for ep, payload in (data or {}).items():
                if not isinstance(ep, str) or not ep:
                    continue
                self.endpoints.setdefault(ep, endpoint_words(ep))
                try:
                    recs = [r for r in (recs_of(payload) or []) if isinstance(r, dict)]
                except Exception:  # noqa: BLE001
                    recs = []
                keys = set(k for k in payload if isinstance(k, str)) if isinstance(payload, dict) else set()
                for r in recs:
                    keys.update(k for k in r if isinstance(k, str))
                for k in keys:
                    if not re.fullmatch(r"[a-z]+", k):
                        self.fields.setdefault(k, field_words(k))
                vals = [r.get("id") for r in recs]
                if len(recs) > 1 and all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in vals) and len({str(v) for v in vals}) == len(vals):
                    for r in recs:
                        if _ID_FORM.fullmatch(str(r["id"])):
                            self.ids.setdefault(str(r["id"]), record_name(r, ep))
        # BUILD IC-8 (P4.4, p44e): the vocabulary leaves out an endpoint slug of one plain lowercase word (the fields' rule: such a
        # word is a word on a page — 'pantries', 'transit'); the endpoints map keeps every slug
        self._voc = {c: {t_: w_ for t_, w_ in getattr(self, c).items() if not (c == "endpoints" and re.fullmatch(r"[a-z]+", t_))}
                     for c in self.CLASSES}
        self._rx = {c: _vocab_rx(self._voc[c]) for c in self.CLASSES}
        self._term = {}                 # BUILD IC-8 (d12c): term → (class, words); a term of two classes is its first class's
        for c in self.CLASSES:
            for t_, w_ in self._voc[c].items():
                self._term.setdefault(t_, (c, w_))
        self._all = _vocab_rx(self._term)

    @property
    def vocab(self):
        return {c: set(self._voc[c]) for c in self.CLASSES}

    def find(self, text):
        """[(class, term, start, end)] of every vocab term in `text`, exactly at word boundaries (a hyphen is part of a word)."""
        out = []
        for c in self.CLASSES:
            rx = self._rx[c]
            if rx is not None and isinstance(text, str):
                out += [(c, m.group(1), m.start(), m.end()) for m in rx.finditer(text)]
        return sorted(out, key=lambda x: x[2])

    def rewrite(self, text, html=False, wrap=True, marks=False):
        """BUILD IC-8 (D11, D12; d12c): a text in a reader's words, in one pass — each vocab term (the longest first; a term of two classes
        is its first class's: ids, endpoints, fields) put in its words: a record id its record's name, an endpoint and a coded field their
        words. An id beside its own record's name ("<id> <name>", "<id> — <name>", "<id>: <name>", "<name> (<id>)") gives the name once
        (BUILD_PROMPT_IC8 D6's strip, in the page's form; the name exactly as recorded). html: the text is a page's HTML between tags —
        each word put in escaped, and one that holds a digit inside <span data-num-exempt="…"> (a name such as "211 Kansas / United Way"
        or a field such as "foster homelessness age 17" is no figure: F20, F28). marks (BUILD IC-8 P4.4, p44n: the Oracle's page, whose
        authors are marks): a record's name — the data's words — is put in inside an org run, whatever run the id stood in; an endpoint's
        and a field's words are Python's and stay in it. → (text, Counter {class: terms put in words, "echo": an id's name given once})."""
        n = Counter()
        if not isinstance(text, str) or not text or self._all is None or not self._all.search(text):
            return text, n
        rx_id = self._rx["ids"]
        if rx_id is not None:
            ws = rf"[\s{MARK_CHARS}]*"   # BUILD IC-8 (P4.4, p44w2): an author's run may open or close in the white space (P4.find-echo-marks)
            sep = (rf"{ws}(?:(?:[—–:\-]|&mdash;|&ndash;|&#8212;|&#8211;){ws})?" if html else rf"{ws}(?:[—–:\-]{ws})?")
            for rid in sorted({m.group(1) for m in rx_id.finditer(text)}, key=len, reverse=True):
                nm = self.ids.get(rid)
                if not nm:
                    continue
                nm_ = _html_text_rx(nm) if html else re.escape(nm)   # BUILD IC-8 (P4.4, p44w): in HTML, the name whatever its escaper's spelling
                pat = re.compile(rf"(?<![\w-]){re.escape(rid)}(?![\w-]){sep}{nm_}(?!\w)|{nm_}{ws}\({ws}{re.escape(rid)}{ws}\)")
                # the id, then the match's own marks in their order: only text goes, every run stays balanced (p44w2)
                text, k = pat.subn(lambda m_: rid + "".join(c for c in m_.group(0) if c in MARK_CHARS), text)
                n["echo"] += k

        def rep(m):
            c, w = self._term[m.group(1)]
            n[c] += 1
            org = marks and c == "ids"      # BUILD IC-8 (P4.4, p44n): the organizers' words in their run (P4.find-name-author)
            if not html:
                return mark("org", w) if org else w
            s = f'<span data-num-exempt="{NUM_EXEMPT_WORDS[c]}">{esc(w)}</span>' if wrap and re.search(r"\d", w) else esc(w)
            return mark("org", s) if org else s
        return self._all.sub(rep, text), n

    def words(self, text):
        """A text in a reader's words (rewrite, plain): what a template's `| words` and Python's page texts call; the page's pass
        (reader_page) calls rewrite on the page's HTML."""
        return self.rewrite(text)[0]


NUM_EXEMPT_WORDS = {"ids": "a record's name", "endpoints": "an endpoint's name", "fields": "a data field's name"}   # BUILD IC-8 (d12c)

READER_ATTRS = ("title", "alt", "aria-label", "placeholder")
ZONE_NAMES = {     # BUILD IC-8 (D11): per page, the provenance zones a template may mark — data-zone="provenance" data-zone-name="<name>" —
    #                only tables, lists of sources, the figure registry, the integrity details and the data-flow diagram; never a paragraph,
    #                a sentence of a list, a change note, a headline or an explanation. Model text is escaped and cannot make one.
    "oracle": ("figure-registry", "integrity-details", "data-sources", "source-note", "method-table", "data-flow"),
    "architect": ("data-sources", "source-note", "method-table"),
    "muse": ("sources", "source-note"),
    "landing": ("data-flow", "source-note")}
# BUILD IC-8 (D11): per page, each embedded data block a script draws — {block: (the key paths it shows, the key paths it never shows: ids,
# field names, the engine's spec, values used as keys)}; a path of a block in neither is unmapped (named, never guessed). A path:
# ".key.key[]" (a list's items "[]"), glob "*" for a key that names data (a record id, an attribute, a set).
READER_JSON = {
    "architect": {
        "gh-bundle": (
            (".checklist.label", ".checklist.items[].name", ".checklist.items[].category", ".checklist.items[].help",
             ".checklist.items[].notes", ".checklist.items[].blockers[]", ".checklist.items[].unlocks[]", ".closures[].name",
             ".closures[].why", ".closures[].by", ".closures[].segment", ".closures[].veto", ".intake.plan[].title",
             ".intake.steps[].label", ".intake.steps[].options[].label", ".intake.steps[].derive[].why", ".navigator.label",
             ".navigator.rows[].name", ".navigator.rows[].free_note", ".navigator.rows[].window", ".navigator.rows[].closed.*",
             ".presets[].name", ".presets[].named_user", ".presets[].who", ".presets[].quote", ".presets[].attrs[].*",
             ".rationale.*.sentences[]", ".readiness.label", ".readiness.rows[].name", ".readiness.rows[].barriers[]",
             ".readiness.steps[].label", ".readiness.steps[].options[].label", ".sets[].label", ".sets[].records[].name",
             ".sets[].records[].check", ".sets[].records[].contact.*", ".sets[].records[].ann.*", ".sets[].records[].holds.*",
             ".sets[].records[].hours.*.text", ".sets[].records[].ph.*", ".sets[].records[].pre[][]", ".sets[].records[].show.*"),
            (".challenge", ".status", ".clock.fixed", ".checklist.endpoint", ".checklist.fields.*", ".checklist.for_sets.*",
             ".checklist.items[].id", ".checklist.items[].needs[]", ".checklist.items[].opens[]", ".checklist.keys.*", ".closures[].id",
             ".closures[].set", ".intake.plan[].call", ".intake.plan[].sets[]", ".intake.steps[].attr", ".intake.steps[].derive[].attr",
             ".intake.steps[].id", ".intake.steps[].kind", ".intake.steps[].type", ".intake.steps[].sets[]",
             ".intake.steps[].options[].attrs.*", ".intake.steps[].options[].value", ".intake.steps[].options[].value[]",
             ".navigator.free_fields[]", ".navigator.lag_field", ".navigator.set", ".navigator.window_field", ".navigator.rows[].id",
             ".presets[].id", ".presets[].person.*", ".rationale.*.top[]", ".rationale.as_of", ".rationale.model",
             ".readiness.barriers_field", ".readiness.documents[]", ".readiness.documents_for", ".readiness.set", ".readiness.storage_key",
             ".readiness.rows[].id", ".readiness.steps[].attr", ".readiness.steps[].id", ".readiness.steps[].kind",
             ".readiness.steps[].options[].attrs.*", ".readiness.steps[].options[].value[]", ".sets[].id", ".sets[].spec.*",
             ".sets[].records[].id", ".sets[].records[].fl[]", ".sets[].records[].raw.*", ".sets[].records[].hours.*.kind",
             ".sets[].table.*")),
        "app-data": (
            (".area_label", ".crisis_events[].category", ".crisis_events[].date", ".crisis_events[].detail", ".crisis_events[].name",
             ".crisis_events[].severity", ".curveball_alert", ".derived_tiles[].label", ".derived_tiles[].endpoint",
             ".derived_tiles[].records[]", ".derived_tiles[].field", ".derived_tiles[].minus", ".domain_hints.*", ".domain_label",
             ".filter_hints.*", ".resources[].attrs.*", ".resources[].category", ".resources[].closed_by", ".resources[].location.*",
             ".resources[].name", ".resources[].status", ".resources[].status_note", ".snapshot_asof"),
            (".crisis_events[].id", ".crisis_events[].endpoint", ".crisis_events[].kind", ".crisis_events[].type",
             ".derived_tiles[].compare_field", ".resources[].id", ".resources[].search_text", ".resources[].source_endpoint",
             ".resources[].tags[]"),
            # BUILD IC-8 (d12e): the organizers' own words — an announcement's closure is its reason (naipc_engine: closed_reason =
            # curveball_text); d12e: the tiles' script prints field and minus (architect_template.html.j2's derived tiles)
            (".resources[].closed_reason",))},
    "oracle": {
        "bn-data": (("[].labels[]",), ("[].id",)),
        "CHART_DATA": (("*.labels[]", "*.datasets[].label"), ("*.datasets[].backgroundColor", "*.datasets[].borderColor",
                                                             "*.datasets[].pointBackgroundColor", "*.datasets[].stack", "*.datasets[].type")),
        "SIM_MODEL": ((".cost_labels[]",), (".cost_colors[]",)),
        "SIM_L": ((".*",), ()),
        "window.CHAT": ((), (".*",))},
    "muse": {"CALC_MODEL": ((), (".*",)), "window.CHAT": ((), (".*",))},
    "landing": {}}
CANDIDATE_RX = (("kebab id", re.compile(r"\b[a-z]+(?:-[a-z]+)*-\d{2,5}\b")), ("camelCase", re.compile(r"\b[a-z]{2,}[A-Z][A-Za-z0-9]*\b")))
CANDIDATE_ALLOW = frozenset({"covid-19"})      # real words of the candidates' form (F14's corpus); a candidate is a measure, never a failure


class _ReaderText(HTMLParser):
    """BUILD IC-8 (D11): a page as a reader reads it — its text and reader-visible attributes: narrative, inside a provenance zone the
    page's list allows, or the organizers' own words (an element the template marks data-verbatim: a quote stays as written, counted
    apart — d12c); the head's description, og and twitter contents; the browser-drawn diagram's quoted labels and tooltips (d12c: drawn,
    a reader reads them; the walk that would read them is cut, §9); and each embedded data block's text."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, zones):
        super().__init__(convert_charrefs=True)
        self.zones_ok, self.stack, self.skip, self.zone, self.head = set(zones), [], 0, 0, 0
        self.verb, self.mer, self._mbuf = 0, 0, []
        self.narr, self.zoned, self.verbatim, self.zones_seen, self.zones_refused, self.blocks, self._sc = [], [], [], [], [], [], None

    def _put(self, where, t):
        (self.zoned if self.zone else self.verbatim if self.verb else self.narr).append((where, t))

    def _attrs(self, tag, a):
        for k in READER_ATTRS:
            if a.get(k):
                self._put(f"{tag}@{k}", a[k])

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "head":
            self.head += 1
        if tag == "meta":
            n = a.get("name") or a.get("property") or ""
            if (n == "description" or n.startswith(("og:", "twitter:"))) and a.get("content"):
                self.narr.append((f"meta {n}", a["content"]))
            return
        if tag in self.VOID:
            if not self.skip and not self.head:
                self._attrs(tag, a)
            return
        z = False
        if a.get("data-zone") == "provenance":
            nm = a.get("data-zone-name", "")
            z = nm in self.zones_ok
            (self.zones_seen if z else self.zones_refused).append(nm or "(no name)")
        v = "data-verbatim" in a
        mer = tag == "pre" and "mermaid" in a.get("class", "").split()
        sk = tag in ("script", "style", "noscript", "template")
        if tag == "script":
            self._sc = [a.get("id") or "", a.get("type") or "", []]
        self.stack.append((tag, z, sk, v, mer))
        self.skip += sk
        self.zone += z
        self.verb += v
        self.mer += mer
        if not self.skip and not self.head:
            self._attrs(tag, a)

    def handle_endtag(self, tag):
        if tag == "head":
            self.head = max(0, self.head - 1)
        if tag == "script" and self._sc is not None:
            self.blocks.append((self._sc[0], self._sc[1], "".join(self._sc[2])))
            self._sc = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _t, z, sk, v, mer in reversed(self.stack[i:]):
                    if mer and self.mer == 1:       # the diagram's source closes: its quoted labels and tooltips, read
                        if not self.skip and not self.head:
                            for lab in re.findall(r'"([^"]*)"', "".join(self._mbuf)):
                                if lab.strip():
                                    self._put("diagram label", lab)
                        self._mbuf = []
                    self.zone -= z
                    self.skip -= sk
                    self.verb -= v
                    self.mer -= mer
                del self.stack[i:]
                break

    def handle_data(self, d):
        if self._sc is not None:
            self._sc[2].append(d)
        elif self.mer:
            self._mbuf.append(d)
        elif not self.skip and not self.head and d.strip():
            self._put("text", d)


def _json_blocks(blocks, names=()):
    """The embedded data a script draws: (block name, its object) — each <script type="application/json" id>, and each "var NAME = {…};"
    line or window.CHAT of an inline script whose NAME `names` lists (the templates' tojson insertions; a script's own tables are not
    data); text that does not parse is named."""
    out, bad = [], []
    for bid, typ, txt in blocks:
        if typ == "application/json" and bid:
            try:
                out.append((bid, json.loads(txt)))
            except ValueError:
                bad.append(bid)
            continue
        for m in re.finditer(r"(?m)^\s*var ([A-Z_]+) = (\{.*\});\s*$", txt):
            if m.group(1) not in names:
                continue
            try:
                out.append((m.group(1), json.loads(m.group(2))))
            except ValueError:
                pass
        m = re.search(r"window\.CHAT=(\{.*?\});", txt, re.S)
        if m and "window.CHAT" in names:
            try:
                out.append(("window.CHAT", json.loads(m.group(1))))
            except ValueError:
                pass
    return out, bad


def _json_strings(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _json_strings(v, f"{path}.{k}")
    elif isinstance(o, list):
        for v in o:
            yield from _json_strings(v, path + "[]")
    elif isinstance(o, str):
        yield path, o


def _path_in(path, globs):
    """A key path against the map's patterns: each its literal text, "*" any run of characters (d11b: fnmatch read "[]" as a class)."""
    return any(re.fullmatch(re.escape(g).replace(r"\*", ".*"), path) for g in globs)


def reader_leaks(html_text, names, track):
    """BUILD IC-8 (D11, I7): what a reader of `track`'s page meets that is this program's data's identifier — a record id, an endpoint slug
    or a coded field name (`names`: ReaderNames of the run's data), matched exactly at word boundaries in the page's text and its
    reader-visible attributes (title, alt, aria-label, placeholder; the head's description, og and twitter contents), in the browser-drawn
    diagram's quoted labels and tooltips (d12c) and in every string of each embedded data block a script shows (READER_JSON). Inside a
    provenance zone of the page's list (ZONE_NAMES) such a term is allowed and counted; inside the organizers' own words (data-verbatim,
    d12c) it is theirs, counted apart; anywhere else it is narrative. → {"narrative": {class: [[term, where, context]]}, "zone": {class:
    n}, "verbatim": {class: n}, "candidates": [[form, term, context]] (lowercase kebab ids with digits and camelCase words that are not
    vocab: a measure), "zones": the zones found, "zones_refused": data-zone marks not in the page's list (their text read as narrative),
    "unmapped": a block or a key path READER_JSON does not class}."""
    p = _ReaderText(ZONE_NAMES.get(track, ()))
    p.feed(html_text or "")
    p.close()
    out = {"narrative": {c: [] for c in ReaderNames.CLASSES}, "zone": {c: 0 for c in ReaderNames.CLASSES},
           "verbatim": {c: 0 for c in ReaderNames.CLASSES}, "candidates": [], "zones": sorted(set(p.zones_seen)),
           "zones_refused": p.zones_refused, "unmapped": []}
    vocab = set().union(*names.vocab.values())

    def scan(where, t, kind="narrative"):
        for c, term, a, b in names.find(t):
            if kind == "narrative":
                out["narrative"][c].append([term, where, " ".join(t[max(0, a - 70):b + 50].split())])
            else:
                out[kind][c] += 1
        if kind == "narrative":
            for form, rx in CANDIDATE_RX:
                for m in rx.finditer(t):
                    if m.group(0) not in vocab and m.group(0).lower() not in CANDIDATE_ALLOW:
                        out["candidates"].append([form, m.group(0), " ".join(t[max(0, m.start() - 50):m.end() + 40].split())])
    for where, t in p.narr:
        scan(where, t)
    for where, t in p.zoned:
        scan(where, t, "zone")
    for where, t in p.verbatim:
        scan(where, t, "verbatim")
    maps = READER_JSON.get(track, {})
    blocks, bad = _json_blocks(p.blocks, set(maps))
    out["unmapped"] += [f"{b}: not parsed" for b in bad]
    for bid, obj in blocks:
        if bid not in maps:
            out["unmapped"].append(f"{bid}: no key map")
            continue
        show, skip, verb = (tuple(maps[bid]) + ((),))[:3]       # BUILD IC-8 (d12e): a third tuple, the organizers' own words
        for path, s in _json_strings(obj):
            if _path_in(path, verb):
                scan(f"json {bid}{path}", s, "verbatim")
            elif _path_in(path, show):
                scan(f"json {bid}{path}", s)
            elif not _path_in(path, skip):
                out["unmapped"].append(f"{bid}{path}")
    out["unmapped"] = sorted(set(out["unmapped"]))
    return out


def leak_counts(found):
    """BUILD IC-8 (D11): reader_leaks' counts — {"narrative": {class: n}, "zone": {class: n}, "verbatim": {class: n}, "candidates": n,
    "unmapped": n}."""
    return {"narrative": {c: len(v) for c, v in found["narrative"].items()}, "zone": dict(found["zone"]),
            "verbatim": dict(found.get("verbatim") or {}), "candidates": len(found["candidates"]), "unmapped": len(found["unmapped"])}


# ═══ BUILD IC-8 (D11, D12; d12c): the page's pass — what a reader meets, in a reader's words ═══
_RP_TAG = re.compile(r"""<!--.*?-->|<!\[CDATA\[.*?\]\]>|<![^>]*>|<\?[^>]*>|<(/?)([A-Za-z][A-Za-z0-9:_-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>""", re.S)
_RP_ATTR = re.compile(r"""([^\s"'>/=]+)(?:(\s*=\s*)("[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")
_RP_LABEL = re.compile(r"(&quot;|&#34;|&#x22;|\")(.*?)\1", re.S)     # a diagram label or tooltip in the page's source, as it is escaped


def _rp_attrs(attrs):
    """A start tag's attributes: {name: value unescaped} (a name without a value: "")."""
    out = {}
    for m in _RP_ATTR.finditer(attrs or ""):
        v = m.group(3) or ""
        if v[:1] in ("'", '"'):
            v = v[1:-1]
        out.setdefault(m.group(1).lower(), _html.unescape(v))
    return out


def _rp_attr_words(attrs, names, keys, n):
    """The attributes' text with the values of `keys` in a reader's words (re-escaped only where a term was put in words)."""
    def one(m):
        k, eq, v = m.group(1), m.group(2), m.group(3)
        if not eq or k.lower() not in keys or not v:
            return m.group(0)
        q = v[0] if v[0] in ("'", '"') else ""
        new, c = names.rewrite(_html.unescape(v[1:-1] if q else v))
        if not c:
            return m.group(0)
        n.update(c)
        return f"{k}{eq}{q or chr(34)}{_html.escape(new, quote=True)}{q or chr(34)}"
    return _RP_ATTR.sub(one, attrs)


def _rp_label_words(raw, names, n, marks=False):
    """A diagram label or tooltip as the page's source holds it (Mermaid's codes, then HTML-escaped): each term put in its words,
    Mermaid-coded and escaped as the label's own text is; an id beside its own record's name gives the name once (as rewrite). BUILD
    IC-8 (P4.4, p44n2): marks — a record's name in an org run (the Oracle's page); the echo reads the name in either spelling of a quote."""
    if names._all.search(raw) is None:
        return raw
    t = raw
    if names._rx["ids"] is not None:
        for rid in sorted({m.group(1) for m in names._rx["ids"].finditer(t)}, key=len, reverse=True):
            nm = names.ids.get(rid)
            if nm:
                nm_ = _html_text_rx(out_text(nm, "mermaid_label"))
                t, k = re.subn(rf"(?<![\w-]){re.escape(rid)}(?![\w-])\s*(?:[—–:\-]\s*)?{nm_}(?!\w)"
                               rf"|{nm_}\s*\(\s*{re.escape(rid)}\s*\)", rid, t)
                n["echo"] += k

    def rep(m):
        c, w = names._term[m.group(1)]
        n[c] += 1
        s_ = _html.escape(out_text(w, "mermaid_label"), quote=True)
        return mark("org", s_) if marks and c == "ids" else s_
    return names._all.sub(rep, t)


def reader_page(html_text, names, marks=False):
    """BUILD IC-8 (D11, D12; d12c): the page a reader meets, in a reader's words — every text a reader sees that D11's check reads as
    narrative (the text between tags; the reader-visible attributes: title, alt, aria-label, placeholder; the head's description, og and
    twitter contents; the browser-drawn diagram's quoted labels and tooltips) has each record id, endpoint slug and coded field name put
    in its words (ReaderNames.rewrite: a record's name, the endpoint's and the field's words; an id beside its own name gives the name
    once; in HTML a word with a digit inside data-num-exempt). Kept as written: a provenance zone (data-zone="provenance": there the ids
    are the lineage), the organizers' own words (data-verbatim: a quote stays a quote), scripts and styles (an embedded data block's
    strings are put in words where Python builds them), noscript and template, and the head's other contents. Every other byte is kept.
    Each harness calls it after its notes are placed (place_notes) and before its integrity line is read; a page's text is the
    model's, the organizers' or Python's — this pass reads none of that, only the run's vocabulary. marks (P4.4, p44n): the Oracle's
    page — each record's name put in inside an org run (ReaderNames.rewrite). → (html, Counter {"ids", "endpoints", "fields", "echo"})."""
    n = Counter()
    if names is None or not isinstance(html_text, str) or not html_text or getattr(names, "_all", None) is None:
        return html_text, n
    out, pos, stack = [], 0, []
    st = {"skip": 0, "zone": 0, "verb": 0, "mer": 0, "head": 0, "svg": 0, "fo": 0}
    L = len(html_text)
    while pos < L:
        m = _RP_TAG.search(html_text, pos)
        end = m.start() if m else L
        if end > pos:
            seg = html_text[pos:end]
            if not (st["skip"] or st["head"] or st["zone"] or st["verb"]):
                if st["mer"]:
                    seg = _RP_LABEL.sub(lambda q: q.group(1) + _rp_label_words(q.group(2), names, n, marks) + q.group(1), seg)
                else:     # d12e: no span inside an SVG's text (outside a foreignObject), an option, a select, a textarea or a title
                    bare = (st["svg"] and not st["fo"]) or (stack and stack[-1][0] in ("option", "select", "textarea", "title"))
                    seg, k = names.rewrite(seg, html=True, wrap=not bare, marks=marks)
                    n.update(k)
            out.append(seg)
        if not m:
            break
        tag, pos = m.group(0), m.end()
        if m.group(2) is None:                  # a comment, a doctype, a processing instruction
            out.append(tag)
            continue
        name = m.group(2).lower()
        if m.group(1) == "/":
            if name == "head":
                st["head"] = max(0, st["head"] - 1)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    for _nm, f in stack[i:]:
                        for k_, v_ in f.items():
                            st[k_] -= v_
                    del stack[i:]
                    break
            out.append(tag)
            continue
        a = _rp_attrs(m.group(3))
        if name == "head":
            st["head"] += 1
        if name in ("script", "style"):          # raw text: copied as it is, to its end tag
            e = re.compile(rf"</{name}\s*>", re.I).search(html_text, pos)
            stop = e.end() if e else L
            out.append(tag + html_text[pos:stop])
            pos = stop
            continue
        if name == "meta":
            mn = a.get("name") or a.get("property") or ""
            if (mn == "description" or mn.startswith(("og:", "twitter:"))) and a.get("content"):
                g0 = m.start(3) - m.start(0)
                tag = tag[:g0] + _rp_attr_words(m.group(3), names, ("content",), n) + tag[m.end(3) - m.start(0):]
            out.append(tag)
            continue
        f = {"skip": int(name in ("noscript", "template")), "zone": int(a.get("data-zone") == "provenance"),
             "verb": int("data-verbatim" in a), "mer": int(name == "pre" and "mermaid" in (a.get("class") or "").split()),
             "svg": int(name == "svg"), "fo": int(name == "foreignobject")}
        void = name in _ReaderText.VOID or tag.rstrip().endswith("/>")
        if not void:
            stack.append((name, f))
            for k_, v_ in f.items():
                st[k_] += v_
        live = not (st["skip"] or st["head"] or st["zone"] or st["verb"]) and not (void and (f["zone"] or f["verb"]))
        if live and m.group(3):
            g0 = m.start(3) - m.start(0)
            tag = tag[:g0] + _rp_attr_words(m.group(3), names, READER_ATTRS, n) + tag[m.end(3) - m.start(0):]
        out.append(tag)
    return "".join(out), n


def reader_words_line(n):
    """BUILD IC-8 (d12c): the console's line for the page's pass — "" when it put nothing in words."""
    parts = [f"{n[k]} {w}" for k, w in (("ids", "record id(s)"), ("endpoints", "endpoint name(s)"), ("fields", "field name(s)")) if n.get(k)]
    if not parts:
        return ""
    return ("Reader words: " + ", ".join(parts) + " put in a reader's words"
            + (f" ({n['echo']} id(s) beside their own record's name given the name once)" if n.get("echo") else ""))


def esc(s):
    """BUILD IC-4 (D3): text escaped as the templates' "| e" escapes it (markupsafe: & < > ' " → &amp; &lt; &gt; &#39; &#34;) — the one
    form for the words a page prints around a registry span."""
    return (str(s or "").replace("&", "&amp;").replace(">", "&gt;").replace("<", "&lt;").replace("'", "&#39;").replace('"', "&#34;"))


def span_if(reg, fid, text):
    """BUILD IC-4 (D3): a figure a page prints — its registry span when the registry holds `fid` with that shown form (the one registry),
    else its text, escaped. reg may be None (no registry of this run)."""
    if text and reg is not None and reg.get(fid) and _norm_ws(reg.shown(fid)) == _norm_ws(str(text)):
        return reg.span(fid)
    return esc(text)


ADDED_FILE = "registry_added.json"       # BUILD IC-4 (D3): the figures a page registered beyond the Oracle's registry


def write_added(path, reg, base_ids, page):
    """BUILD IC-4 (D3): the figures a page registered beyond the Oracle's registry of this run (ids not in base_ids: its comparisons, the
    data ids it read by grammar), each with its fact, to `path` — for the pipeline's checks (merge_added). None, or why not written."""
    try:
        add = {k: e for k, e in reg.entries.items() if k not in base_ids}
        facts = fill_units({k: reg.fact(e) for k, e in add.items()})
        doc = {"schema": SCHEMA, "page": page, "challenge": reg.challenge, "run_id": reg.run_id,
               "entries": out_tree([dict(add[k], fact=facts[k]) for k in sorted(add)], "plain")}    # BUILD IC-5 (D2 c): a writer
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as ex:  # noqa: BLE001
        return f"{type(ex).__name__}: {str(ex)[:120]}"


def merge_added(reg, path):
    """BUILD IC-4 (D3): a page's additions (write_added) into the registry — of this challenge and run only; an id the registry holds is
    kept as it is (the Oracle's). Returns (the number added, None) or (0, why). Never raises."""
    try:
        d = admit(json.loads(Path(path).read_bytes().decode("utf-8-sig")), "saved", Path(path).name)   # BUILD IC-5 (D1): the one way in
    except FileNotFoundError:
        return 0, "no additions file"
    except Exception as ex:  # noqa: BLE001
        return 0, f"not readable ({type(ex).__name__})"
    if not isinstance(d, dict) or d.get("schema") != SCHEMA or not isinstance(d.get("entries"), list):
        return 0, "not a schema-1 additions file"
    if d.get("challenge") != reg.challenge or d.get("run_id") != reg.run_id:
        return 0, f"of run {d.get('run_id')} ({d.get('challenge')}), not this registry's"
    n = 0
    for e in d["entries"]:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] not in reg.entries and e.get("kind") in KINDS:
            reg.entries[e["id"]] = dict(e)      # BUILD IC-5 (D1, D2): its words as admit gave them (kind saved), printed as words
            n += 1
    return n, None


def fill_units(facts):
    """BUILD IC-4 (D3): a measure's unit in one form — facts {id → fact} grouped by their key with the unit set aside; in a group where
    every fact that names a unit names the same one, a fact that names none takes it, with the id that gave it (`unit_from`, the first
    in id order). A group whose facts name two units keeps both: two facts. Changes the facts in place and returns them."""
    groups = {}
    for k in sorted(facts):
        f = facts[k]
        if isinstance(f, dict):
            groups.setdefault(fact_key(dict(f, unit="")), []).append(k)
    for ks in groups.values():
        named = [(k, facts[k].get("unit")) for k in ks if facts[k].get("unit")]
        if named and len({u for _k, u in named}) == 1:
            for k in ks:
                if not facts[k].get("unit"):
                    facts[k]["unit"], facts[k]["unit_from"] = named[0][1], named[0][0]
    return facts


def _sum_fields(recs, fld):
    """The fields a sum:<endpoint>.<field> id reads: (field, None) when a record holds that name, else (a, b) of "<a>-<b>"."""
    if not any(fld in x for x in recs) and "-" in fld:
        a, _, b = fld.partition("-")
        return a, b
    return fld, None


def _sum_value(rec, a, b):
    """A record's part of a sum: a − b (or a), or None when either is not a number (the record is not read)."""
    x, y = rec.get(a), (rec.get(b) if b else 0)
    return x - y if all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in (x, y)) else None


# ═══ the registry ═══
def _clean(s, n=300):
    return re.sub(r"\s+", " ", str(s)).strip()[:n] if s is not None else None


# BUILD IC-4 run turn (R6; §7 row 6's class) gave the prompts' tags one conversion (tag_words), applied where an entry's words were
# written and read and over each page's context at render (T12). BUILD IC-5 (BUILD_PROMPT_IC5.md, "T12's place") keeps it only as
# out_text's tag step: admit gives every way in the canonical form, an entry keeps its words as they arrive, and every output prints them
# as words — out_text where a builder or a writer makes its text, the model's author filters, finalize's tag step on every printed value.
# PROMPT_TAGS: the tags the prompts define (t_tags_ic4 reads them from the harnesses' request text).
PROMPT_TAGS = ("BENCHMARK", "CHALLENGE_DATA", "ESTIMATE")


# ═══ BUILD IC-5 (D1, I1 "one way in"): REG.admit — every model reply and every file a run wrote enters here ═══
ADMIT_KINDS = ("reply", "saved", "diagram", "data")
DIAGRAM_FIELDS = ("mermaid_code",)          # a reply's fields that are a diagram's source: their tags are removed, never put in words
_ADMIT = {"endpoints": None, "log": [], "rewrite": True}
_ADMIT_LOG_MAX = 5000
# every attribution form the prompts define (and the variants a model writes): any case, "_" "-" or a space inside the word, in [ ], 【 】,
# ［ ］ or a doubled [[ ]]; the canonical internal forms are "[CHALLENGE_DATA: a, b]", "[CHALLENGE_DATA]", "[ESTIMATE]", "[ESTIMATE: c]"
_TAG_WORD = r"(?P<w>challenge[\s_-]*data|estimated?|benchmark|source)\b"
_TAG_FORMS = (re.compile(r"\[\[\s*" + _TAG_WORD + r"(?P<c>[^\[\]]*)\]\]", re.I),                 # a doubled [[…]] closes with ]]
              re.compile(r"(?:\[|【|［)\s*" + _TAG_WORD + r"(?P<c>[^\[\]【】［］]*)(?:\]|】|］)", re.I))   # a single one with one
_TAG_CANON = re.compile(r"\[CHALLENGE_DATA(?:: [^\[\]]+)?\]|\[ESTIMATE(?:: [^\[\]]+)?\]")
_CONFIDENCE = re.compile(r"\b(low|medium|high)\b(?:\s*[–—-]\s*(low|medium|high)\b)?(?!\s*[–—-]\s*\w)", re.I)


def _confidence(c):
    """BUILD IC-5 (D1, D2 c): an estimate tag's confidence, read from its own words — one reading for admit and out_text: "medium",
    "MEDIUM confidence", "low–medium" (a range with "-", "–" or "—"), the level in lower case and a range with "–"; None when its words
    name none ("based on HUD rates"; a level joined to a word by a dash, "high-cost", "low-income", is no confidence)."""
    m = _CONFIDENCE.search(c or "")
    return (m.group(1).lower() + ("–" + m.group(2).lower() if m.group(2) else "")) if m else None


def _tag_kind(w):
    """BUILD IC-5 (D1, D2 c): a tag's word as the recognizer names it → its kind — one reading for admit and out_text: "data", "estimate",
    "benchmark" or "source" (the separators the recognizer allows — "_", "-", a space, or none — set aside)."""
    k = re.sub(r"[\s_-]+", "", (w or "").lower())
    return _TAG_KINDS.get(k, k)


_TAG_KINDS = {"challengedata": "data", "estimated": "estimate"}    # BUILD IC-5 (D11): a tag word's other spellings → its kind (the chat's too)
_UNRECOGNIZED = re.compile(r"(?<!\[)\[\s*([A-Z][A-Z_ -]{2,40}?)\s*(?:[:,][^\[\]]{0,80})?\](?!\])")
# BUILD IC-5 Phase 4 (the replays, D4 on the Muse's page): an attribution form a request defines with no tag word — the Muse asks for each
# persona's evidence as {"stat": …, "label": "<what it measures> [<endpoint-name>]"} (muse_harness.py, generate_strategic): read as the data
# tag it is when every name in it is an endpoint of the run ("-", "_" or a space between its words); other bracketed words stay as they
# are written (a footnote "[1]", "[sic]", a canonical tag), and a link's text ("[a](…)") or a token is never one
_EP_BRACKET = re.compile(r"(?<!\[)\[\s*([A-Za-z0-9][A-Za-z0-9 _-]{0,60}(?:\s*[,;]\s*[A-Za-z0-9][A-Za-z0-9 _-]{0,60})*?)\s*\](?![\](])")


def _ep_key(n):
    """An endpoint's name as admit compares it: lower case, "-" between its words ("Shelter Inventory", "shelter_inventory")."""
    return re.sub(r"[\s_-]+", "-", str(n).strip().lower())


def _canon_endpoints(m, where):
    """One bracketed name (or a list of names) → "[CHALLENGE_DATA: <names as written>]" when every one is an endpoint of the run, recorded
    (converted); anything else exactly as written."""
    names = [x.strip() for x in re.split(r"[,;]", m.group(1)) if x.strip()]
    eps = {_ep_key(e) for e in _ADMIT["endpoints"] or ()}
    if not names or not all(_ep_key(n) in eps for n in names):
        return m.group(0)
    _admit_note("converted", where, m.group(0))
    return f"[CHALLENGE_DATA: {', '.join(names)}]"


_READERS = (("load", "contract"), ("load_map", "coverage map"), ("load_spec", "engine sidecar"))


def admit_readers(*modules):
    """BUILD IC-5 (D1 e): the shared modules' file readers that their composite functions call inside — NR.load (the contract: from_env,
    page_context), NR.load_map (the coverage map: map_for_check, page_context) and ENG.load_spec (the engine sidecar: for_oracle) — wrapped
    in this process so that what they read passes admit (kind data). Each is wrapped once (a second call changes nothing); a reader that
    raises still raises (NR.load's RequirementsError). A composite function's result is Python's construction — it can hold Python's
    marks — and is never admitted itself. Returns the names wrapped by this call."""
    out = []
    for mod in modules:
        for name, where in _READERS:
            f = getattr(mod, name, None)
            if not callable(f) or getattr(f, "_admitted", False):
                continue

            def w(*a, _f=f, _where=where, **k):
                return admit(_f(*a, **k), "data", _where)
            w._admitted, w.__name__, w.__doc__, w.__wrapped__ = True, name, f.__doc__, f
            setattr(mod, name, w)
            out.append(f"{getattr(mod, '__name__', '?')}.{name}")
    return out


def set_admit_rewrite(on):
    """BUILD IC-5 (D1 f): whether admit rewrites the tags of a reply or a saved file (this build's prompt switch, NR.ic5_prompt(): every run
    file sets it when it loads). Off, admit does what strip_pua did — the model is sent back its own text as the older builds sent it; an
    explicit kind "diagram" still loses its tags (the page's diagram, in both modes). Returns the setting."""
    _ADMIT["rewrite"] = bool(on)
    return _ADMIT["rewrite"]


def set_endpoints(names):
    """BUILD IC-5 (D1): the run's endpoint names, which admit checks a data tag's names against (None: not known, not checked)."""
    _ADMIT["endpoints"] = None if names is None else {str(n).strip().lower() for n in names if str(n).strip()}


def admit_findings(clear=False):
    """BUILD IC-5 (D1): what admit did since the last clear — [{kind, where, text}]; kind is converted, confidence_kept, unknown_endpoint,
    removed_from_diagram or unrecognized_bracket."""
    out = list(_ADMIT["log"])
    if clear:
        _ADMIT["log"].clear()
    return out


# ═══ BUILD IC-5 (D2, I2–I4 "one way out"): the page environment, finalize_page, the author filters, out_text ═══
class AuthorshipError(RuntimeError):
    """A template value printed with no declared author while NAIPC_STRICT_AUTHORS=1 (every test sets it; a run never does)."""


class Html(str):
    """BUILD IC-5 (D2): text that is HTML already — escaped by a filter, or a builder's (REG.BUILDERS). Jinja's `| e` and finalize_page
    pass it as it is (the __html__ protocol markupsafe and Jinja read). A plain page's filters return it; it carries no author mark."""
    __slots__ = ()

    def __html__(self):
        return str(self)


_PAGE = {"template": None, "problems": [], "tags": [], "names": None}   # one render's template, authorship problems and tags met at
#                                                                         output; BUILD IC-8 (D12): the page's ReaderNames
OUT_CONTEXTS = ("html_text", "attr", "json_in_script", "mermaid_label", "plain", "request", "entry")   # BUILD IC-5 (D13): "request"; Phase 4: "entry"


def _tpl_line():
    """The template line being printed (the caller's frame of finalize or a filter), for a problem's message."""
    try:
        t, f = _PAGE["template"], sys._getframe(2)
        while f is not None and not f.f_code.co_filename.endswith(".j2"):
            f = f.f_back
        return f" (template line {t.get_corresponding_lineno(f.f_lineno)})" if t is not None and f is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _is_undefined(v):
    return any(c.__name__ == "Undefined" and c.__module__.startswith("jinja2") for c in type(v).__mro__)


def _tag_words_of(w, c):
    """One tag form's word and content (as admit's recognizer names them) → the page's words, no brackets."""
    w = _tag_kind(w)
    c = (c or "").strip().lstrip(":：,-–—").strip()
    if w == "data":
        names = [x.strip() for x in re.split(r"[,;]", c) if x.strip()]      # BUILD IC-5 (D2 c): as written — the page's badges and tables
        return f"data: {', '.join(names)}" if names else "data"
    if w == "estimate":
        cf = _confidence(c)                 # BUILD IC-5 (D2 c): the same reading as admit's (a leftover form gets the same words)
        return f"estimate, {cf} confidence" if cf else "estimate"
    if w == "benchmark":
        return f"benchmark: {c}" if c else "benchmark"
    return f"source: {c}" if c else "source"


_TAG_ANY = re.compile(r"\[\[\s*" + _TAG_WORD + r"(?P<c>[^\[\]]*)\]\]|(?:\[|【|［)\s*" + _TAG_WORD.replace("(?P<w>", "(?P<wb>")
                      + r"(?P<cb>[^\[\]【】［］]*)(?:\]|】|］)", re.I)
def _cut_starts(pieces):
    """BUILD IC-5 (D3, class L): a pattern for a word cut short after its first piece ("c", "ch", … each piece a letter or a class)."""
    out = ""
    for p in reversed(pieces[1:]):
        out = "(?:" + p + out + ")?"
    return pieces[0] + out


# BUILD IC-5 (D2 c): a tag a cut left open — at most 80 characters of a tag's own words (no bracket, no quote, no < or >, no line or
# sentence's end) running to the text's end, a line's end or the markup a builder put after it, with the space and an open parenthesis before it.
# Never inside an attribute's value (a quote ends the words first). _TAG_START: a tag's start no rule reads — recorded, left as it is.
# BUILD IC-5 (D3, class L): a cut inside the tag's word or right after its bracket ("[CH", "[ESTIMAT", "[") — the word's first letters,
# or none, at the text's very end only: valid code, JSON and HTML never end with an open bracket (finalize's step leaves a script byte
# for byte), and before markup or a line's end a bracket is a builder's own.
_TAG_CUT_WORD = "(?:" + "|".join(_cut_starts(w) for w in (list("challenge") + [r"[\s_-]*"] + list("dat"), list("estimat"),
                                                          list("benchmar"), list("sourc"))) + ")?"     # _TAG_WORD's spellings, cut short
_TAG_OPEN = re.compile(r"\s*(?:\(\s*)?(?:\[\[|\[|【|［)\s*(?:" + _TAG_WORD.replace("(?P<w>", "(?:")
                       + r"(?:(?!\.\s)[^\[\]【】［］<>\"'\r\n]){0,80}(?=<|\r?\n|$)|" + _TAG_CUT_WORD + r"(?=\s*\Z))", re.I)
_TAG_START = re.compile(r"(?:\[\[|\[|【|［)\s*" + _TAG_WORD.replace("(?P<w>", "(?:"), re.I)


def chat_tags_table():
    """BUILD IC-5 (D11): D1's tag forms for the pages' chat reader (window.CHAT.tags; generated here, never typed twice): `any` —
    _TAG_ANY's pattern in JavaScript's syntax, `confidence` — _CONFIDENCE's, `kinds` — _TAG_KINDS, `hold` — how many characters after
    an unclosed bracket a stream holds back (a tag's words are at most 80; a word that starts none is shown at 60)."""
    js = lambda p: p.replace("(?P<", "(?<")          # noqa: E731 — Python's named group → JavaScript's
    return {"version": 1, "any": js(_TAG_ANY.pattern), "confidence": js(_CONFIDENCE.pattern), "kinds": dict(_TAG_KINDS), "hold": 60}


def _tag_sub(s, note=None, only=None):
    """s with every tag form in the page's words: "([ESTIMATE])" → "(estimate)", "([ESTIMATE], a" → "(estimate, a", a bare tag → "(…)".
    `note(tag)` is called for each tag met. Words carry no character HTML escapes (letters, digits, spaces, a colon, a comma, a dash).
    BUILD IC-5 (D1): `only` — the kinds to convert (admit's word-only kinds: benchmark, source); a tag of another kind is left exactly as
    written, its parentheses too, and no cut tag is dropped."""
    def one(m):
        if note:
            note(m.group(0))
        return _tag_words_of(m.group("w") or m.group("wb"), m.group("c") if m.group("w") else m.group("cb"))

    def kept(t):
        return only is not None and _tag_kind(t.group("w") or t.group("wb")) not in only
    out = re.sub(r"\(\s*(" + _TAG_ANY.pattern + r")\s*\)", lambda m: m.group(0) if kept(_TAG_ANY.match(m.group(1)))
                 else "(" + one(_TAG_ANY.match(m.group(1))) + ")", s, flags=re.I)
    out = re.sub(r"\(\s*(" + _TAG_ANY.pattern + r")", lambda m: m.group(0) if kept(_TAG_ANY.match(m.group(1)))
                 else "(" + one(_TAG_ANY.match(m.group(1))), out, flags=re.I)
    out = _TAG_ANY.sub(lambda m: m.group(0) if kept(m) else "(" + one(m) + ")", out)
    if only is not None:
        return out
    if note:
        for m in _TAG_OPEN.finditer(out):
            note(m.group(0).strip())
    out = _TAG_OPEN.sub("", out)
    if note:
        for m in _TAG_START.finditer(out):         # BUILD IC-5 (D2 b): a tag still at output that no rule could read is recorded too
            note(m.group(0))
    return out


def out_text(s, context="html_text"):
    """BUILD IC-5 (D2 c): the one output conversion — every tag form becomes the page's words ("(data: shelter inventory)", "(estimate)",
    "(estimate, medium confidence)", "(benchmark: x)"; a data tag's endpoint names as the contract writes them, the form the page's
    badges and data-sources table print: the contract holds no titles), a tag a cut left open at the text's end dropped; then the context's form: html_text and attr escaped (REG.esc),
    json_in_script a JSON string with <, >, &, U+2028 and U+2029 escaped (as Jinja's tojson), mermaid_label with Mermaid's #35; and
    #quot; codes, plain as it is, request (BUILD IC-5 D13) its tags in words and its tokens kept — the model's earlier text shown back
    in a request; entry (BUILD IC-5 Phase 4) the same, for a registry entry's words — build_registry's resolver reads their tokens later.
    Not text → its str. BUILD IC-5 (D15): mermaid_label is one line, with Mermaid's codes for # " < > & ` (# first: the
    codes hold it) — 11.17.2 under strict draws a label's "<b>" or "<img>" as elements and a label in backticks as Markdown."""
    if context not in OUT_CONTEXTS:
        raise ValueError(f"out_text: context {context!r} is not one of {OUT_CONTEXTS}")
    t = s if isinstance(s, str) else ("" if s is None else str(s))
    if "[" in t or "【" in t or "［" in t:
        t = _tag_sub(t)
    if context in ("request", "entry"):        # BUILD IC-5 (D13): the model's earlier text shown back in a request — its tags its words,
        # its tokens kept (the request's own notation: the reply writes figures as tokens). Phase 4 (the replays): "entry" — a registry
        # entry's words before build_registry's resolver reads their tokens (a token dropped here drops the figure the model cited)
        return t
    t = strip_tokens(t)                        # BUILD IC-5 (D3, class T): a token no resolver read never reaches a page
    if context in ("html_text", "attr"):
        return esc(t)
    if context == "json_in_script":
        return (json.dumps(t, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    if context == "mermaid_label":
        t = " ".join(t.split())
        for a_, b_ in (("#", "#35;"), ('"', "#quot;"), ("<", "#60;"), (">", "#62;"), ("&", "#38;"), ("`", "#96;")):
            t = t.replace(a_, b_)
        return t
    return t


def out_tree(value, context="plain"):
    """BUILD IC-5 (D2 c): out_text over a writer's document — each string of a dict's values and a list's or a tuple's items in the
    context's form; anything else as it is (a writer's JSON is plain dicts and lists: REG.WRITERS)."""
    if isinstance(value, str):
        return out_text(value, context)
    if isinstance(value, dict):
        return {k: out_tree(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [out_tree(v, context) for v in value]
    if isinstance(value, tuple):
        return tuple(out_tree(v, context) for v in value)
    return value


# BUILD IC-5 (D2 c): the writers whose files carry the model's words — each writes them through out_text (out_tree over its document);
# tests/t_words_ic5.py reads this list and each one's code. A chat context's document is made by chat_fields (its sha256 names the file
# and the page's context id), and write_chat_context writes its bytes.
WRITERS = (("oracle-harness/oracle_harness.py", "chat_fields", "the Oracle's chat context document (write_chat_context writes its bytes)"),
           ("muse-harness/muse_harness.py", "chat_fields", "the Muse's chat context document (write_chat_context writes its bytes)"),
           ("architect-harness/architect_harness.py", "chat_fields", "the Architect's chat context document (write_chat_context writes it)"),
           ("architect-harness/architect_harness.py", "help_view", "the navigator's data: the explanations in its bundle"),
           ("naipc_registry.py", "write_added", "a page's additions file (registry_added.json)"),
           ("landing-harness/pipeline.py", "integrity_check", "the facts report (runs/<challenge>/facts_<run id>.json)"))

# BUILD IC-5 (D2 f, I4): the builders — every function whose result a page prints `| built` or `*_html` (traced from the four templates by
# ic5_dev/d2/built_roots.py and built_producers_ic5.py, then read), nested functions included. Each one's interpolated values are escaped or
# proved (tests/t_builders_ic5.py reads them; a value that is neither is a reviewed row of tests/builders_allow_ic5.json). An HTML string
# built anywhere else is counted against a baseline. (file, function, what it builds)
BUILDERS = (
    ("naipc_registry.py", "Registry.span", "a figure's span: its id and title escaped (the title's words through out_text), its kind one of "
                                           "KINDS (checked at add and at read) and its class from the kind"),
    ("naipc_registry.py", "Registry.resolve", "a token's span, a declaration's span or a refused token's text: escaped; the rule Python's name"),
    ("naipc_registry.py", "Registry.quote_html", "the organizers' words escaped, each quantity its mention's span"),
    ("naipc_registry.py", "span_if", "a registry span, or the text escaped"),
    ("naipc_registry.py", "resolve_for_track", "a text's tokens as spans (html) in words its caller escaped"),
    ("naipc_registry.py", "resolve_track_text", "the same for one text of a track's reply"),
    ("naipc_registry.py", "resolve_track_value", "the same over a reply's values"),
    ("naipc_registry.py", "inline_svg", "an SVG without its XML declaration (the QR codes: segno's output for Python's URL)"),
    ("naipc_registry.py", "_place_notes", "BUILD IC-6 (D9): the notes on figures whose sources disagree and each reference to one — every "
                                          "number an int (:d), each note's words escaped (esc), the section's words Python's constants (esc)"),
    ("naipc_requirements.py", "section_attrs", "a section's coverage attributes, each value escaped (quote=True)"),
    ("naipc_requirements.py", "page_context", "the coverage panel's attributes (section_attrs)"),
    ("naipc_requirements.py", "clean_brand_html", "the brand's name, its markup limited to the brand's own tags"),
    ("naipc_requirements.py", "escape_markup", "the model's markup rule: the narrative's attribute-free tags kept, any other tag dropped, < and > escaped"),
    ("naipc_requirements.py", "join_clauses", "clauses joined as they are (each its builder's HTML or escaped text)"),
    ("naipc_engine.py", "apply_curveball", "the Architect's alert: <strong> and the closed names or the announcement escaped"),
    ("oracle-harness/oracle_harness.py", "build_template_context", "the Oracle's page values built in place (the SROI sentences, the brief's "
                                                                   "quote, the cost of inaction, the diagram's source escaped), and its "
                                                                   "fix_narrative and fix_text"),
    ("oracle-harness/oracle_harness.py", "cite_badges", "the source badges: an endpoint's name (the tag's [a-zA-Z0-9_-] names, or a known "
                                                        "name escaped) and the tag's words escaped"),
    ("oracle-harness/oracle_harness.py", "_quoted", "a quoted segment: its words escaped and marked by their author"),
    ("oracle-harness/oracle_harness.py", "_reason_block", "an engine reason in its provenance span"),
    ("oracle-harness/oracle_harness.py", "reason_html", "an engine reason: Python's frame and the organizers' sentence, escaped"),
    ("oracle-harness/oracle_harness.py", "_esc_label", "an entry's label, source, formula or assumption: words escaped, each part marked"),
    ("oracle-harness/oracle_harness.py", "_esc_model", "the model's words escaped and marked"),
    ("oracle-harness/oracle_harness.py", "_esc_org", "the organizers' words escaped and marked"),
    ("oracle-harness/oracle_harness.py", "_assumption_html", "an assumption change's value: its registry span, or its words escaped"),
    ("oracle-harness/oracle_harness.py", "registry_rows", "the registry table's spans"),
    ("oracle-harness/oracle_harness.py", "registry_check_rows", "the sidecar checks' spans"),
    ("oracle-harness/oracle_harness.py", "honesty_section", "What could prove us wrong"),
    ("oracle-harness/oracle_harness.py", "incident_section", "incident mode at the curveball"),
    ("oracle-harness/oracle_harness.py", "kpi_framework_section", "the KPI framework"),
    ("oracle-harness/oracle_harness.py", "renewal_gates_section", "the renewal gates"),
    ("oracle-harness/oracle_harness.py", "rubric_section", "the rubric map"),
    ("oracle-harness/oracle_harness.py", "prioritization_section", "the prioritization block"),
    ("oracle-harness/oracle_harness.py", "prio_attrs", "the prioritization block's coverage attributes"),
    ("oracle-harness/oracle_harness.py", "use_of_funds_section", "the use of funds"),
    ("oracle-harness/oracle_harness.py", "youth_section", "the youth handoffs"),
    ("oracle-harness/oracle_harness.py", "bottleneck_section", "the bottlenecks"),
    ("oracle-harness/oracle_harness.py", "sensitivity_grid", "the sensitivity grid"),
    ("oracle-harness/oracle_harness.py", "early_warning_cells", "the risk register's early-warning cells"),
    ("oracle-harness/oracle_harness.py", "deliverables_view", "the deliverables' headlines, KPIs and warnings"),
    ("oracle-harness/oracle_harness.py", "update_words_locked", "the update's words through the lock"),
    ("oracle-harness/oracle_harness.py", "curveball_section", "the curveball section's changed values (_assumption_html)"),
    ("oracle-harness/oracle_harness.py", "closing_statement", "the closing statement's spans"),
    ("oracle-harness/oracle_harness.py", "generate_qr_svg", "the QR code (segno's SVG for Python's URL)"),
    ("muse-harness/muse_harness.py", "resolve_muse_text", "the Muse's page variant: the model's words through the markup rule and out_text, "
                                                          "then its tokens as spans"),
    ("muse-harness/muse_harness.py", "oracle_business_line", "the Oracle's business line: its spans"),
    ("muse-harness/muse_harness.py", "build_context", "the Muse's page values built in place (the calls sentence)"),
    ("muse-harness/muse_harness.py", "main", "the curveball block's announcement (quote_html, or escaped)"),
    ("muse-harness/muse_harness.py", "generate_qr_svg", "the QR code (segno's SVG for Python's URL)"),
    ("architect-harness/architect_harness.py", "help_view", "the Get Help tab's figures (span_if) and its closed records' lines"),
    ("architect-harness/architect_harness.py", "build_context", "the Architect's page values built in place: the data inventory, the totals, "
                                                                "the curveball block, the QR code"),
    ("landing-harness/landing_harness.py", "main", "the hub's pitch and figures"),
    ("landing-harness/landing_harness.py", "lead_html", "the Oracle's lead: words through out_text, tokens as spans, cut"),
    ("landing-harness/landing_harness.py", "hub_curveball_panel", "the hub's curveball panel (quote_html, or escaped)"),
    ("landing-harness/landing_harness.py", "generate_qr_svg", "the QR code (segno's SVG for Python's URL)"),
)


def _tag_step(t):
    """finalize_page's last step on a value it passes: a tag still in it becomes words (out_text's), each one recorded with the template
    line (page_tags) — a run never shows a tag; a test fails on any."""
    if "[" not in t and "【" not in t and "［" not in t:
        return t
    line = []
    out = _tag_sub(t, note=lambda tag: line.append(tag))
    if line:
        where = _tpl_line()
        _PAGE["tags"].extend(f"{x[:60]}{where}" for x in line)
    return out


def _make_finalize(authored):
    def finalize_page(v):
        """BUILD IC-5 (D2 b): every runtime output expression passes here before it is printed. An Undefined prints nothing; a number is
        Python's (marked so on an authored page); a value with __html__ (Jinja's `| e` and `| tojson`, an Html) and a value an author
        filter marked pass; any other text nobody declared is converted (out_text) and escaped, marked "undeclared" on an authored page
        and listed (page_problems) — an AuthorshipError in a test (NAIPC_STRICT_AUTHORS=1). Every value it passes goes through the tag
        step."""
        if _is_undefined(v):
            return ""
        if isinstance(v, (bool, int, float)):
            return mark("py", str(v)) if authored else str(v)
        if hasattr(v, "__html__"):
            return _tag_step(str(v.__html__()))
        t = str(v)
        if not t:
            return t
        if authored and wrapped(t):
            return _tag_step(t)
        if os.environ.get("NAIPC_STRICT_AUTHORS") == "1":
            raise AuthorshipError(f"an output expression with no declared author printed {unmark(t)[:80]!r}{_tpl_line()}")
        t, probs = balance(out_text(t, "html_text"))
        _PAGE["problems"].append(f"undeclared (escaped): {unmark(t)[:60]!r}{_tpl_line()}")
        _PAGE["problems"].extend(f"undeclared: {p_} in {unmark(t)[:60]!r}{_tpl_line()}" for p_ in probs)
        return mark("undeclared", t) if authored else t
    finalize_page.__name__ = "finalize_page" if authored else "finalize_page_plain"
    return finalize_page


finalize_page = _make_finalize(True)
finalize_page_plain = _make_finalize(False)


def _author_filter(author, escape, authored=True):
    """The templates' author filters: `| org`, `| py`, `| model`, `| label` escape as `| e` does, then mark (authored) or return Html
    (plain); `| org_html`, `| model_html`, `| label_html` and `| built` (a builder's HTML, its inner parts carrying their own marks) only
    mark or pass. BUILD IC-5 (D2 c): `| model` and `| label` print the model's words, so their tags become words as they are escaped
    (out_text, html_text); `| org` and `| py` only escape — a tag there is finalize's record (the organizers' and Python's text holds none)."""
    words = author in ("model", "label")

    def f(v):
        if _is_undefined(v):
            return ""
        t = ((str(v.__html__()) if hasattr(v, "__html__") else (out_text(str(v), "html_text") if words else esc(str(v)))) if escape
             else str(v))
        if not authored:
            return Html(t)
        t, probs = balance(t)
        _PAGE["problems"].extend(f"{author}: {p_} in {unmark(t)[:60]!r}{_tpl_line()}" for p_ in probs)
        return mark(author, t)
    f.__name__ = author if escape else f"{author}_html"
    return f


_FILTER_SPEC = (("org", "org", True), ("py", "py", True), ("model", "model", True), ("label", "label", True), ("org_html", "org", False),
                ("model_html", "model", False), ("label_html", "label", False), ("built", "py", False))
AUTHOR_FILTERS = {n: _author_filter(a, e, True) for n, a, e in _FILTER_SPEC}
AUTHOR_FILTERS_PLAIN = {n: _author_filter(a, e, False) for n, a, e in _FILTER_SPEC}


# ── BUILD IC-8 (D12, I7): Python's texts in a reader's words ──
def reader_html(text, names, html=False, marks=False):
    """A text of Python's in a reader's words (ReaderNames.rewrite) between its tags, never inside one — an attribute keeps the id a
    script reads (data-*, href); a value with __html__ keeps its type, its words escaped (html: d12c)."""
    if names is None or not isinstance(text, str) or not text:
        return text
    parts = re.split(r"(<[^<>]*>)", text)
    out = "".join(p if i % 2 else names.rewrite(p, html=html, marks=marks)[0] for i, p in enumerate(parts))
    return Html(out) if isinstance(text, Html) else out


def set_page_names(names):
    """The page's ReaderNames (Registry.names()), set before a page is built and rendered; None clears them."""
    _PAGE["names"] = names


def set_measure_names(names):
    """BUILD IC-8 (P4.4, p44v; P4.find-measure-class): the run's names — the ReaderNames its page pass uses — set once the run's data and
    the Oracle's registry are loaded, before any text is fitted: shown_text puts each record id, endpoint slug and coded field name in its
    reader words, as the page pass will, so a limited field is measured on the text its reader is shown. None clears them. → names"""
    _PAGE["measure_names"] = names
    return names


def page_words(text, marks=False):
    """A text of Python's in the current page's words (set_page_names); as it is when none are set. marks (BUILD IC-8 P4.4, p44n2): a
    record's name in an org run (the Oracle's page)."""
    return reader_html(text, _PAGE.get("names"), marks=marks)


def _words_filter(v, marks=False):
    """The templates' `| words`, before an escaping author filter (py, org, model, label): a value of Python's in a reader's words — a
    record id its record's name, an endpoint and a coded field their words (BUILD IC-8 D12); a plain str is text (the names as written:
    the filter after it escapes them), a value with __html__ HTML. Never on the model's or the organizers' text. A Python-built HTML
    text that a non-escaping filter prints takes `| words_html`."""
    if _is_undefined(v) or _PAGE.get("names") is None:
        return v
    if hasattr(v, "__html__"):
        return Html(reader_html(str(v.__html__()), _PAGE["names"], html=True, marks=marks))
    return reader_html(v, _PAGE["names"], marks=marks) if isinstance(v, str) else v


def _words_html_filter(v, marks=False):
    """BUILD IC-8 (P4.4, p44w): `| words_html`, before `| built` or an *_html filter — a Python-built HTML text (a str or an Html: its
    parts escaped by its builder) in a reader's words between its tags, each name put in escaped (reader_html, html=True). `| words` on
    such a text put a record's name in as written, and `| built` printed it: markup from the data reached the page (P4.find-words-html)."""
    if _is_undefined(v) or _PAGE.get("names") is None:
        return v
    if hasattr(v, "__html__"):
        v = str(v.__html__())
    return Html(reader_html(v, _PAGE["names"], html=True, marks=marks)) if isinstance(v, str) else v


def _words_marked(v):
    """BUILD IC-8 (P4.4, p44n): `| words` of the authored set (the Oracle's page): a record's name in an org run."""
    return _words_filter(v, marks=True)


def _words_html_marked(v):
    """BUILD IC-8 (P4.4, p44n): `| words_html` of the authored set (the Oracle's page): a record's name in an org run."""
    return _words_html_filter(v, marks=True)


AUTHOR_FILTERS["words"], AUTHOR_FILTERS_PLAIN["words"] = _words_marked, _words_filter
AUTHOR_FILTERS["words_html"], AUTHOR_FILTERS_PLAIN["words_html"] = _words_html_marked, _words_html_filter


def page_env(env_cls, loader, authored=True, build=None, **kw):
    """BUILD IC-5 (D2 a): every harness's Jinja environment — env_cls is Jinja's Environment (this module imports no Jinja).
    BUILD IC-8 (d16c): `build` — the harness's NR.BUILD_ID, the one build literal — is the templates' naipc_build (a global; their head
    prints {{ naipc_build | e }}).
    autoescape=False on purpose: finalize_page escapes every undeclared text and the author filters escape theirs, so no value is ever
    escaped twice; finalize_page (authored: the Oracle's page, whose numbers the check reads by author marks) or finalize_page_plain (a
    page read by its blocks: the Muse's, the Architect's, the hub's — no mark is ever printed there); the author filters to match."""
    env = env_cls(loader=loader, autoescape=False, finalize=finalize_page if authored else finalize_page_plain, **kw)
    env.filters.update(AUTHOR_FILTERS if authored else AUTHOR_FILTERS_PLAIN)
    if build:
        env.globals["naipc_build"] = build
    return env


def page_template(env, name):
    """BUILD IC-5 (D2 a): the template `name` of `env`, with this render's problems and tags cleared (one page, any number of renders)."""
    t = env.get_template(name)
    _PAGE["template"] = t
    _PAGE["problems"].clear()
    _PAGE["tags"].clear()
    return t


def page_problems():
    """The authorship problems of the page's renders since page_template (finalize's and the filters')."""
    return list(_PAGE["problems"])


def page_tags():
    """The tags finalize met at output since page_template, each with its template line (a run shows words; a test fails on any)."""
    return list(_PAGE["tags"])


# ── BUILD IC-5 (D7, I7; G13): checks in stages ──
ADMITTED, RESOLVED, RENDERED = "admitted", "resolved", "rendered"
STAGES = (ADMITTED, RESOLVED, RENDERED)       # in order: through admit; tokens resolved after every producing call; a rendered page
_STAGE = {}            # id(object) → (the object, its stage): the object is held, so its id names it while it is labelled


class StageError(RuntimeError):
    """A check called on an object before the stage it reads — raised before the check keeps or drops anything."""


def set_stage(obj, stage):
    """Label obj with a stage of STAGES; returns obj. A stage never goes back (an earlier label leaves a later one as it is)."""
    if stage not in STAGES:
        raise ValueError(f"set_stage: {stage!r} is not one of {STAGES}")
    cur = _STAGE.get(id(obj))
    if cur is None or cur[0] is not obj or STAGES.index(stage) >= STAGES.index(cur[1]):
        _STAGE[id(obj)] = (obj, stage, getattr(obj, "_version", None))    # BUILD IC-5 Phase 4 (N9 T19): a registry's version with it
    return obj


def stage_of(obj):
    """obj's stage, or None when it carries none."""
    cur = _STAGE.get(id(obj))
    return cur[1] if cur is not None and cur[0] is obj else None


def require(obj, stage, check):
    """BUILD IC-5 (D7): obj, when it is at `stage` or later; else StageError naming the check and the stage it found. BUILD IC-5 Phase 4
    (N9 T19: "Reject premature/stale inputs before checks can report success or discard content"): a registry required resolved (or
    later) must be the registry it was labelled at — an entry added since makes it stale, and the check is refused the same way."""
    got = stage_of(obj)
    if got is None or STAGES.index(got) < STAGES.index(stage):
        raise StageError(f"{check} reads an object {stage}; this one is {got or 'unlabelled'}")
    cur = _STAGE.get(id(obj))
    v0, v1 = (cur[2] if len(cur) > 2 else None), getattr(obj, "_version", None)
    if STAGES.index(stage) >= STAGES.index(RESOLVED) and v0 is not None and v1 is not None and v1 != v0:
        raise StageError(f"{check} reads a registry {stage} at its version {v0}; it has changed since (version {v1}): a stale registry")
    return obj


def outside_tokens(value, fn):
    """BUILD IC-5 (D2, I3): fn applied to each string of `value` with its [[…]] tokens held as written — a token is never seen by fn (a q:
    token's quote stays as typed, matched against its source); dicts (values only) and lists are walked, anything else returned as it is.
    The tokens are held in private-use placeholders (no model text holds one: admit removes them), so a tag around a token stays whole."""
    if isinstance(value, str):
        if "[[" not in value:
            return fn(value)
        held = []

        def _hold(m):
            held.append(m.group(0))
            return f"\ue001{len(held) - 1}\ue002"
        t = fn(TOKEN_RE.sub(_hold, value))
        return re.sub("\ue001(\\d+)\ue002", lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held) else "", t)
    if isinstance(value, dict):
        return {k: outside_tokens(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [outside_tokens(v, fn) for v in value]
    return value


def write_admit_findings(cp_dir, run_id_=None):
    """BUILD IC-5 (D1): this process's admit findings appended to <checkpoints>/admit_findings.jsonl (the pattern of
    decode_findings.jsonl), then cleared. Never raises; returns how many were written."""
    rows = admit_findings(clear=True)
    if not rows:
        return 0
    try:
        p = Path(cp_dir) / "admit_findings.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                inv_n = os.environ.get("NAIPC_INVOCATION_NUMBER", "").strip()     # BUILD IC-8 (D8): the invocation's identity
                f.write(json.dumps(dict(r, run_id=run_id_, invocation_id=os.environ.get("NAIPC_INVOCATION_ID", "").strip() or None,
                                        invocation_number=int(inv_n) if inv_n.isdigit() else None, schema_version=2), ensure_ascii=False) + "\n")
        return len(rows)
    except OSError:
        return 0


ADMIT_DISPOSITIONS = ("converted", "confidence_kept", "unknown_endpoint", "removed_from_diagram", "unrecognized_bracket")
ADMIT_WORDS = {"converted": "tag(s) rewritten in their one form", "confidence_kept": "estimate tag(s) with their confidence kept",
               "unknown_endpoint": "data tag name(s) that name no endpoint of this run",
               "removed_from_diagram": "tag(s) removed from the diagram's source", "unrecognized_bracket": "bracketed word(s) kept as typed"}


def admit_counts(cp_dir, run_id_=None, scope="run"):
    """BUILD IC-5 (D1): this run's admit findings by kind — {kind: n} from <checkpoints>/admit_findings.jsonl (rows of `run_id_`; every
    row when None). A row counts only when its kind is one of ADMIT_DISPOSITIONS; no text is returned. Never raises. BUILD IC-8 (D9):
    read through the one reader (naipc_requirements.read_records) in `scope` — "run", "active invocation", "invocation:<id>"."""
    out = {}
    try:
        import naipc_requirements as _NR    # beside this module in every folder, as the requirements module imports this one
        p = Path(cp_dir) / "admit_findings.jsonl"
        rows = ([x for _i, x in _NR._record_lines(p)["parsed"]] if run_id_ is None and scope == "run"
                else _NR.read_records(p, scope, rid=run_id_, strict=False).lines)
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        if r.get("kind") in ADMIT_DISPOSITIONS:
            out[r["kind"]] = out.get(r["kind"], 0) + 1
    return out


def admit_line(cp_dir, run_id_, track):
    """BUILD IC-8 (D9, I6): the console's line of the way in, by kind, with its scope — the active invocation's findings, and the run's
    when they differ (a re-run's run holds both invocations' rows); rows that name no invocation (IC-7's) are said to be ambiguous, never
    counted as the active invocation's (IC-7 printed the run's 22 as the track's, where each invocation had 11). "" when the run has none."""
    try:
        import naipc_requirements as _NR
        p = Path(cp_dir) / "admit_findings.jsonl"
        ra = _NR.read_records(p, "active invocation", rid=run_id_, strict=False)
        rr = _NR.read_records(p, "run", rid=run_id_, strict=False)
    except Exception:  # noqa: BLE001
        return ""

    def tally(rows):
        return {k: n for k, n in ((k, sum(1 for r in rows if r.get("kind") == k)) for k in ADMIT_DISPOSITIONS) if n}
    wr = admit_words(tally(rr.lines))
    if not wr:
        return ""
    if not ra.invocation:
        return f"tags in the model's replies as read in ({track}), {rr.words}: {wr} — no invocation recorded (ambiguous)"
    wa = admit_words(tally(ra.lines))
    line = f"tags in the model's replies as read in ({track}), {ra.words}: {wa or 'none'}"
    if wa != wr:
        line += f"; {rr.words}: {wr}" + (f" ({len(ra.ambiguous)} of them name no invocation: ambiguous)" if ra.ambiguous else "")
    return line


def admit_words(counts):
    """BUILD IC-5 (D1): admit_counts in plain words, in ADMIT_DISPOSITIONS' order ("" when none)."""
    c = counts if isinstance(counts, dict) else {}
    return "; ".join(f"{c[k]} {ADMIT_WORDS[k]}" for k in ADMIT_DISPOSITIONS if isinstance(c.get(k), int) and c[k] > 0)


def _admit_note(kind, where, text):
    if len(_ADMIT["log"]) < _ADMIT_LOG_MAX:
        _ADMIT["log"].append({"kind": kind, "where": str(where)[:80], "text": str(text)[:80]})


def _canon_tag(m, where, observe=True):
    """One recognized tag → its canonical internal form, or plain words for the forms no page links (a benchmark, a source).
    BUILD IC-5 (D1 c): a source tag is always its words — the form never depends on whether this process knows the endpoints; a rewrite is
    recorded once (converted, or confidence_kept for an estimate's confidence carried into its canonical form); `observe` (kind "reply":
    where model text first enters) also records a data tag naming no endpoint of the run (unknown_endpoint; the tag is kept canonical)."""
    w = _tag_kind(m.group("w"))
    c = m.group("c").strip().lstrip(":：,-–—").strip()
    conf = False
    if w == "data":
        names = [x.strip() for x in re.split(r"[,;]", c) if x.strip()]
        out = f"[CHALLENGE_DATA: {', '.join(names)}]" if names else "[CHALLENGE_DATA]"
        eps = _ADMIT["endpoints"]
        if observe and eps is not None:
            for n in names:
                if n.lower() not in eps and not any(n.lower().startswith(e) for e in eps):
                    _admit_note("unknown_endpoint", where, n)
    elif w == "estimate":
        cf = _confidence(c)
        if cf:
            out = f"[ESTIMATE: {cf}]"
            conf = True
        else:
            out = f"[ESTIMATE: {c[:40].strip()}]" if c else "[ESTIMATE]"
    elif w == "benchmark":
        out = f"(benchmark: {c})" if c else "(benchmark)"
    else:                                   # "source": no prompt defines it; its words, whatever it names
        out = f"(source: {c})" if c else "(source)"
    if out != m.group(0):
        _admit_note("confidence_kept" if conf else "converted", where, m.group(0))
    return out


_MD_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_CODE = re.compile(r"`+([^`\n]*?)`+")
_MD_HEAD = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_MD_STRAY = re.compile(r"\*\*|__|`")


def _markdown_words(t, where):
    """BUILD IC-5 (D3, class M): the model's Markdown set down to its words — bold or strong markers (** or __ around words), code ticks,
    an ATX heading's hashes at a line's start (a hashtag, "#Tag", is no heading), then a marker left alone — recorded once per text
    (markdown_removed)."""
    if not ("*" in t or "_" in t or "`" in t or "#" in t):
        return t
    u = _MD_BOLD.sub(lambda m: m.group(2), t)
    u = _MD_CODE.sub(lambda m: m.group(1), u)
    u = _MD_HEAD.sub("", u)
    u = re.sub(r"[ \t]{2,}", " ", _MD_STRAY.sub("", u))
    if u != t:
        _admit_note("markdown_removed", where, t)
    return u


def _admit_str(s, kind, where):
    s = _PUA.sub("", s if type(s) is str else str(s))
    if kind == "data":
        return s
    if kind == "diagram":
        def gone(m):
            _admit_note("removed_from_diagram", where, m.group(0))
            return ""
        for rx in _TAG_FORMS:
            s = re.sub(r"[ \t]?" + rx.pattern, gone, s, flags=re.I)
        return s
    if not _ADMIT["rewrite"]:                # BUILD IC-5 (D1 f): this build's prompt switch off — strip_pua's work only
        return s
    t, observe = s, kind == "reply"          # BUILD IC-5 (D1 c): observations where model text first enters
    if "[" in s or "【" in s or "［" in s:
        # BUILD IC-5 (D1, D2 c): the word-only kinds through the one conversion, which reads a tag inside parentheses ("([BENCHMARK])" →
        # "(benchmark)"; was "((benchmark))"); then the canonical forms of the data and estimate tags
        t = _tag_sub(t, note=lambda tag: _admit_note("converted", where, tag), only=("benchmark", "source"))
        for rx in _TAG_FORMS:
            t = rx.sub(lambda m: _canon_tag(m, where, observe), t)
        if _ADMIT["endpoints"] and "[" in t:       # BUILD IC-5 Phase 4 (the replays): the Muse's "[<endpoint-name>]"
            t = _EP_BRACKET.sub(lambda m: _canon_endpoints(m, where), t)
    if observe and "[" in t:
        for m in _UNRECOGNIZED.finditer(_TAG_CANON.sub("", t)):
            _admit_note("unrecognized_bracket", where, m.group(0))
    return t


def admit(value, kind="reply", where=""):
    """BUILD IC-5 (D1, I1): the one way in. Every model reply ("reply"), every file a run wrote ("saved"), a diagram's source ("diagram")
    and the organizers' data, brief and announcement ("data") pass here before any harness reads them. Private-use code points go
    (strip_pua's job, unchanged for "data", which is otherwise returned as it is: the organizers' text is not rewritten). For a reply or a
    saved file every attribution form becomes its canonical internal form ([CHALLENGE_DATA: a, b], [ESTIMATE], [ESTIMATE: medium]) or,
    for the forms no page links (a benchmark, a source), plain words; a diagram's source (kind "diagram", or a DIAGRAM_FIELDS
    field of a reply) loses every tag. Each rewrite and each bracketed upper-case word it does not know is recorded (admit_findings).
    Dicts (their keys too), lists and tuples are walked; strings come back as plain str (never a subclass: no trust rides through);
    anything else as it is. admit(admit(x)) == admit(x). Never raises.
    BUILD IC-5 (D7): the dict or list it returns — or a reader's (value, why) pair's value — is labelled ADMITTED (stage_of)."""
    out = _admit(value, kind, where)
    if kind == "reply":             # BUILD IC-8 (D14, I8): a reply's parser path — where it came in
        try:
            import naipc_requirements as _NR
            _NR.mark_path(_NR.path_id("reply", where or "reply"))
        except Exception:  # noqa: BLE001
            pass
    for v in (out if isinstance(out, tuple) else (out,)):
        if isinstance(v, (dict, list)):
            set_stage(v, ADMITTED)
    return out


def _admit(value, kind="reply", where="", prose=True):
    """admit's walk (BUILD IC-5 D7: the public admit labels its result). BUILD IC-5 (D3c, class M): a reply's string values are the
    model's prose — its Markdown set down to words (_markdown_words) — but a key, a saved file and a diagram's source (a DIAGRAM_FIELDS
    key, or an edit whose id names one: its "value") are never prose."""
    if kind not in ADMIT_KINDS:
        raise ValueError(f"admit: kind {kind!r} is not one of {ADMIT_KINDS}")
    try:
        if isinstance(value, str):
            t = _admit_str(value, kind, where)
            return _markdown_words(t, where) if kind == "reply" and prose and _ADMIT["rewrite"] else t
        if isinstance(value, dict):
            code = "op" in value and str(value.get("id") or "").removeprefix("f.") in DIAGRAM_FIELDS   # an edit line of a diagram's source
            #                                                                   (its id as the review set writes a field's: "f.mermaid_code")
            out = {}
            for k, v in value.items():
                kk = _admit_str(k, "data" if kind == "data" else "reply", where) if isinstance(k, str) else k
                sub = "diagram" if kind in ("reply", "saved") and kk in DIAGRAM_FIELDS and _ADMIT["rewrite"] else kind
                out[kk] = _admit(v, sub, f"{where}.{kk}" if where else kk, prose=prose and not (code and kk == "value"))
            return out
        if isinstance(value, list):
            return [_admit(v, kind, where, prose) for v in value]
        if isinstance(value, tuple):
            return tuple(_admit(v, kind, where, prose) for v in value)
        return value
    except Exception:  # noqa: BLE001 — the way in never stops a run: the input with its private-use code points removed, as before
        return strip_pua(value)


# BUILD IC-5 Phase 4 (the replays): an id's shape in Python's words — a run of letters, digits, "_", ".", ":" and "-" that starts with a
# letter and ends with a letter or a digit (Registry.name_ids decides whether it is an id of the registry)
_PY_ID_RUN = re.compile(r"(?<![\w.:-])[A-Za-z][\w.:-]*[A-Za-z0-9](?![\w-])")


class Registry:
    """id → entry. Data figures are read by grammar on demand from this run's payloads (set_data):
       <endpoint>.<field> (a one-record endpoint), <endpoint>.<record id>.<field>, <endpoint>.count — MEASURED;
       sum:<endpoint>.<field> and sum:<endpoint>.<a>-<b> over every record — DERIVED."""

    def __init__(self, challenge=None, run_id=None, block=None):
        self.challenge, self.run_id, self.block = challenge, run_id, block
        self.entries = {}
        self.problems = []           # 'path: reason' lines (a duplicate id, a value that is not a number)
        self.notes = []              # what resolution did that a reader should know (a case-insensitive match, an alias)
        self._raw, self._records = {}, None
        self._version = 0            # BUILD IC-5 Phase 4 (N9 T19): each entry added counts — a check reads the registry it was resolved at
        self.tags = {}               # data id → {class, window, cohort, unit, fmt, label} (the sidecar's tags)
        self._model_ids = {}         # (number as written, what) → its id: one id per proposal, however often it is resolved
        self.sources = {}            # BUILD IC-2 (D2): the organizers' texts a quote may name besides the data — "brief", "announcement"
        self.integrity = None
        self.checks = []             # the sidecar's checks: a figure computed from records vs the data's own aggregate, each block
        self.block1_note = None      # at Block 2: why Block 1's registry is not read (None when it is)
        self._ex = None              # BUILD IC-3 (D3): the run's own names, for the reader of a quote's text (made once from the data)
        self.mentions = []           # BUILD IC-3 (D6): the list of mentions (add_mentions), in the order the request shows them
        self._mention_at = {}        # (where the text sits, start, end) → the mention's id: a quote written out that names one is that entry
        self.stale = {}              # BUILD IC-3 (D11): Block 1's mentions this run's list no longer holds — id → {words, status, now, …}
        self.id_alias = {}           # BUILD IC-4 (D2): an id this run reads as another — a mention's IC-3 id (from its sentence) → its claim
                                     # id; a Block 1 mention re-anchored by its context → this run's id (lookup reads it after the exact id)
        self.review = []             # BUILD IC-4 (D2): Block 1's cited claims unchanged in value whose sentence changed — for the update,
                                     # never a flag
        self.unavailable = set()     # BUILD IC-4 (D2): endpoints that did not load this run (the harness sets it): "source unavailable"
        self.reanchor = {}           # BUILD IC-4 (D2): every Block 1 mention's status at Block 2 — id → {status, now_id, …}
        self.sealed_counts = set()   # BUILD IC-8 (D2): the tracks whose count namespace is sealed (seal_counts), saved with the registry
        self.count_problems = []     # BUILD IC-8 (D2): live, a count after its seal or a list counted again with another number
        self.counted = {}            # BUILD IC-4 (D13): the page's lists counted so far — field name → its page name (add_counts)
        self.authority = {}          # BUILD IC-4 (D3): the contract's authority — a figure → its entry (metric, words, endpoint, as_of)
        self.aliases = {}            # BUILD IC-4 (D2, D3): the contract's aliases — "<endpoint>.<old field>" → its entry (old, new, why)
        self.fetched = None          # BUILD IC-4 (D3): what a figure with no time field is as of — {"value", "from"}; the run's fetch
        self.made = None             # BUILD IC-4 (D3): what a figure that reads no endpoint is as of — the run it was made in
        self._unbound = {}           # BUILD IC-4 (D2): a data id whose record holds no such field and no alias maps it → why
        self.block1_ids = set()      # BUILD IC-4 (D2): the ids Block 1's registry holds — a field it cited is a binding this run must keep

    # ── building ──
    def add(self, id, value, kind, fmt="num", unit="", label="", source=None, formula=None, inputs=None, assumption=None, cls=None,
            window=None, cohort=None, verified=None, quote=None, block=None, novalue=None, label_model=None, fact=None):
        """Register one figure; returns the entry, or None (named in problems) when the id is taken or the value is not a number.
        BUILD IC-4 (D3): `fact` — the parts of the figure's fact its builder knows (a sum's records, Block 1's as-of); Registry.fact
        completes it.
        BUILD IC-3 (D3): `novalue` — the shown form of a quoted expression that has no single value (a range, a ratio, a bound, a
        multiplier, a recurrence, a hedged figure): registered with value None and that form; calc refuses it.
        BUILD IC-3 (ledger step 24): `label_model` — the model's own words inside the label ("" when the label is all Python's): the page
        marks each part of the label by its author (oracle_harness._esc_label); kept in registry.json."""
        if not isinstance(id, str) or not id.strip():
            self.problems.append(f"registry: an id must be text, got {id!r}")
            return None
        if id in self.entries:
            self.problems.append(f"registry: {id}: registered twice — the first kept")
            return None
        v = value if isinstance(value, (int, float)) and not isinstance(value, bool) else (None if novalue else parse_number(value))
        if novalue and v is None:
            pass
        elif v is None or not math.isfinite(v):
            self.problems.append(f"registry: {id}: {value!r} is not a number — not registered")
            return None
        if kind not in KINDS:
            self.problems.append(f"registry: {id}: kind {kind!r} is not one of {', '.join(KINDS)} — not registered")
            return None
        fmt = fmt if fmt in FMTS else "num"
        e = {"id": id, "value": v, "shown": fmt_value(v, fmt) if v is not None else str(novalue), "fmt": fmt, "unit": unit or "", "kind": kind, "label": _clean(label, 200) or id,
             "source": _clean(source), "formula": _clean(formula), "inputs": list(inputs or []),
             "assumption": _clean(assumption, 600),        # BUILD IC-5 (D2): an entry's words as they arrive (admitted); printed as words
             "class": cls if cls in CLASSES else None, "window": _clean(window, 120), "cohort": _clean(cohort, 160)}
        if kind == "EXTERNAL":
            ver = verified if isinstance(verified, dict) else {}
            ok = ver.get("verified") == "yes" and all(isinstance(ver.get(k), str) and ver[k].strip() for k in ("url", "quote", "checked"))
            e["verified_against_primary"] = "yes" if ok else "no"
            if ok:
                e.update(url=ver["url"].strip(), quote=_clean(ver["quote"], 400), checked=ver["checked"].strip())
        if quote:
            e["quote"] = _clean(quote, 400)
        if block:
            e["block"] = block
        if label_model is not None:
            e["label_model"] = _clean(label_model, 200) or ""
        if isinstance(fact, dict) and fact:
            e["fact"] = dict(fact)
        self.entries[id] = e
        self._version = getattr(self, "_version", 0) + 1     # BUILD IC-5 Phase 4 (N9 T19): the registry's version
        return e

    # ── BUILD IC-4 (D3): the fact ──
    def set_facts(self, authority=None, aliases=None, fetched=None, made=None):
        """The contract's authority (a figure → its metric) and aliases (an old field path → the new), what a figure of the data with no
        time field is as of (default: fetched in this run) and what a figure that reads no endpoint is (default: made in this run)."""
        self.authority = {a["figure"]: a for a in authority or [] if isinstance(a, dict) and isinstance(a.get("figure"), str)}
        self.aliases = {a["old"]: a for a in aliases or [] if isinstance(a, dict) and isinstance(a.get("old"), str)
                        and isinstance(a.get("new"), str)}
        if isinstance(fetched, dict):
            self.fetched = fetched
        if isinstance(made, dict):
            self.made = made

    def _rec_of(self, ep, rid):
        """(the record of an endpoint by its key, the endpoint's payload) — (None, payload) when the record is not there."""
        recs = self._recs(ep) or []
        k = self.record_key(ep)
        rec = next((x for x in recs if k and rid is not None and str(x.get(k)) == str(rid)), None)
        return rec, self._raw.get(ep)

    def _fact_parts(self, base, e):
        """BUILD IC-4 (D3): what an entry's id and its own fields say its fact is — {entity, measure, scope} and, for a quoted figure, its
        unit and its claim: a mention by its place (the record and its field, the brief or the announcement) and its claim; a quote the
        writer wrote that is no mention by its place and its words; a data id by its record, its endpoint (a count) or the records a
        sum reads; any other figure by its id (Python's, the writer's, the brief's, an outside source's). Registry.fact gives each part
        its one form and the as-of."""
        where = e.get("where")
        if e.get("quoted") and isinstance(where, str) and where:
            if not e.get("mention"):
                return {"entity": {"quote": where}, "measure": f"(quoted) {e.get('written') or e.get('shown')}", "scope": "the organizers' text"}
            p = {"unit": _claim_unit(e["claim"]) if isinstance(e.get("claim"), str) else None, "claim": [e.get("claim"), e.get("occ") or 0]}
            if where in QUOTE_PLACES:
                return dict(p, entity={"text": where}, measure=f"the {where} (quoted)", scope=f"the {where}'s text",
                            as_of={"value": f"the {where}", "from": f"the {where} as given"})
            parts = where.split(".")
            return dict(p, entity={"endpoint": parts[0], "id": ".".join(parts[1:-1]) or None}, measure=f"{parts[-1]} (quoted)",
                        scope="the record's text")
        if base.startswith("sum:"):
            ep, _, fld = base[4:].partition(".")
            recs = self._recs(ep) or []
            a, b = _sum_fields(recs, fld)
            k = self.record_key(ep)
            read = [str(x.get(k)) if k and x.get(k) is not None else str(i + 1) for i, x in enumerate(recs) if _sum_value(x, a, b) is not None]
            return {"entity": {"endpoint": ep, "records": read}, "measure": f"{a} \u2212 {b}" if b else a}
        parts = base.split(".")
        if parts[0] in self._raw and len(parts) in (2, 3):
            if len(parts) == 2 and parts[1] == "count":
                return {"entity": {"endpoint": parts[0]}, "measure": "records", "scope": "the endpoint's records"}
            if len(parts) == 2:
                return {"entity": {"endpoint": parts[0], "id": None}, "measure": parts[1], "scope": "one record"}
            return {"entity": {"endpoint": parts[0], "id": parts[1]}, "measure": parts[2], "scope": "one record"}
        c = e.get("calc") if isinstance(e.get("calc"), dict) else {}
        pa, pb = str(c.get("a") or "").split("."), str(c.get("b") or "").split(".")
        if c.get("op") == "diff" and len(pa) == 3 and len(pb) == 3 and pa[:2] == pb[:2] and pa[0] in self._raw:
            # a comparison of two fields of one record is a fact of that record ("slotsAvailable − activeEnrollment")
            return {"entity": {"endpoint": pa[0], "id": pa[1]}, "measure": f"{pa[2]} \u2212 {pb[2]}", "scope": "one record"}
        kind, src = e.get("kind"), str(e.get("source") or "")
        if kind == "EXTERNAL":
            return {"entity": {"source": src or base}, "measure": base, "scope": "an outside source",
                    "as_of": {"value": e.get("checked") or "not stated", "from": "the date its source was checked"}}
        if src.startswith("the brief"):
            return {"entity": {"text": "brief"}, "measure": base, "scope": "the brief", "as_of": {"value": "the brief", "from": "the brief as given"}}
        return {"entity": {"named": base}, "measure": base, "scope": "the writer's assumption" if kind == "MODELED" else "computed by Python"}

    def disagreeing(self, fid):
        """BUILD IC-4 (D3, D7): the sidecar's checks that find this figure unequal to another — [(the other's id, its entry)]."""
        out = []
        ids = {fid} | set(self.same_figure(fid))      # IC-4 Phase 7 (review b10): a mention stating a sidecar figure is that figure
        for c in self.checks or []:
            if isinstance(c, dict) and not c.get("equal") and (c.get("entry") in ids or c.get("against") in ids):
                other = c.get("against") if c.get("entry") in ids else c.get("entry")
                oe = self.get(other)
                if oe and other not in ids and other not in [o for o, _x in out]:
                    out.append((other, oe))
        for other in self.metric_disagree(fid):        # IC-4 Phase 7 (review b7): the contract's authority maps both to one metric
            oe = self.get(other)
            if oe and other not in [o for o, _x in out]:
                out.append((other, oe))
        return out

    def same_figure(self, fid):
        """IC-4 Phase 7 (review b10): the sidecar's figures a mention states — a figure of the same record (its endpoint and id) with the
        same value that a sidecar check names. [] for any other entry."""
        e = self.get(fid)
        if not e or not e.get("mention"):
            return []
        vals = {e["value"]} if isinstance(e.get("value"), (int, float)) and not isinstance(e.get("value"), bool) else set()
        try:            # a ratio or a range states its numbers in its claim ("17 of 87": 17 and 87)
            vals |= {x for x in (json.loads(e.get("claim") or "null") or [])[1:8] if isinstance(x, (int, float)) and not isinstance(x, bool)}
        except Exception:  # noqa: BLE001
            pass
        if not vals:
            return []
        ep, _d, rest = str(e.get("where") or "").partition(".")
        rid = rest.rpartition(".")[0]
        out = []
        for c in self.checks or []:
            for side in ((c.get("entry"), c.get("against")) if isinstance(c, dict) else ()):
                se = self.get(side)
                ent = ((se or {}).get("fact") or {}).get("entity") or {}
                if (se and not se.get("mention") and ent.get("endpoint") == ep and str(ent.get("id")) == rid and rid
                        and se.get("value") in vals and se["id"] not in out):
                    out.append(se["id"])
        return out

    def metric_disagree(self, fid):
        """IC-4 Phase 7 (review b7, D3): the figures the contract's authority maps to the same metric as this one whose values are not
        one value rounded (consistent_shown) — one fact, two values. [] when the authority names none."""
        if not self.authority:
            return []
        by = {}
        for fig, a in self.authority.items():
            ent = self.get(fig.partition("#")[0]) if isinstance(fig, str) and not fig.startswith("q:") else None
            if ent is not None and isinstance(a, dict) and a.get("metric"):
                by.setdefault(a["metric"], []).append(ent["id"])
        e = self.get(fid)
        if e is None:
            return []
        m = next((k for k, ids in by.items() if e["id"] in ids), None)
        if m is None:
            return []
        return [o for o in by[m] if o != e["id"] and (self.get(o) or {}).get("shown") is not None
                and not consistent_shown([e.get("shown"), self.get(o).get("shown")])]

    def disagree_parts(self, a_id, b_id):
        """BUILD IC-4 (D3, D7): two figures whose sources disagree — each one's as-of in plain words (as_of_words) and who speaks: the
        contract's authority when it names either side's metric, else no one ("no source is named over the other": no precedence is
        invented). The one form every page prints."""
        fa, fb = ((self.fact(self.get(i)) if self.get(i) else {}) for i in (a_id, b_id))
        au = fa.get("authority") or fb.get("authority")
        return {"a_asof": as_of_words(fa.get("as_of")), "b_asof": as_of_words(fb.get("as_of")), "authority": bool(au),
                "speaks": (f"the contract names {endpoint_words(au.get('endpoint'))} as the source for {field_words(au.get('metric'))}" if au
                           else "no source is named over the other")}           # BUILD IC-8 (D12): in words

    def _disagree_one(self, x):
        """BUILD IC-4 (D7; IC-6 D9: one method, the title's and the note's): a side of a conflict — its shown value, its label, its as-of
        in plain words."""
        w = as_of_words(self.fact(x).get("as_of"))
        return f"{x['shown']} ({self.reader_label(x)}" + (f", {w}" if w else "") + ")"      # BUILD IC-8 (D12): its reader's label

    def _disagree_title(self, e):
        """BUILD IC-4 (D7): both values of a figure whose sources disagree, each with its as-of in plain words, and who speaks
        (disagree_parts) — the title of the marker beside it."""
        others = self.disagreeing(e["id"])
        sp = self.disagree_parts(e["id"], others[0][0])["speaks"] if others else "no source is named over the other"
        return out_text("sources disagree: " + self._disagree_one(e) + " against " + "; ".join(self._disagree_one(oe) for _o, oe in others)
                        + " — " + sp, "plain")

    def pair_note(self, a_id, b_id):
        """BUILD IC-6 (D9): one conflict — two figures — as a page's note: "Sources disagree: <a> against <b> — <who speaks>.", each side
        its value, label and as-of (_disagree_one), who speaks as disagree_parts says it; plain words, no mark. None when either is no
        entry."""
        ea, eb = self.get(a_id), self.get(b_id)
        if not ea or not eb:
            return None
        return unmark(out_text("Sources disagree: " + self._disagree_one(ea) + " against " + self._disagree_one(eb) + " — "
                               + self.disagree_parts(a_id, b_id)["speaks"] + ".", "plain"))

    def mention_at(self, where, words=None):
        """BUILD IC-4 (D3): this run's one mention at a place (with those words, white space set aside), for a page that prints a
        record's text as it is; None when there is none or more than one."""
        ms = [e for e in self.entries.values() if e.get("mention") and e.get("where") == where
              and (words is None or _norm_ws(str(e.get("written") or "")) == _norm_ws(str(words)))]
        return ms[0] if len(ms) == 1 else None

    def quote_html(self, where, text):
        """BUILD IC-4 (D3, D1): the organizers' words at a place (a record's field, the brief, the announcement) as a page prints them,
        escaped, each quantity the one reader finds in them the span of this run's mention at that place with the same claim — the k-th
        of a claim in the text that occurrence's mention when the place holds more than one — and only when the printed words are the
        mention's shown form (shows); any other quantity stays text. Never raises."""
        if not isinstance(text, str) or not text:
            return esc(text)
        try:
            if self._ex is None:
                self._ex = Exempter.from_data(self._raw, self._records) if self._records is not None else Exempter()
            ms = {}
            for e in self.entries.values():
                if e.get("mention") and e.get("where") == where:
                    ms.setdefault(e.get("claim"), []).append(e)
            out, last, seen = [], 0, {}
            for x in quantities(text, names=self._ex):
                if x["role"] != "quantity":
                    continue
                claim = claim_key(x, text[x["end"]:x["end"] + 80])
                cands = sorted(ms.get(claim) or [], key=lambda c: c.get("occ") or 0)
                k = seen.get(claim, 0)
                seen[claim] = k + 1
                e = cands[0] if len(cands) == 1 else next((c for c in cands if (c.get("occ") or 0) == k), None)
                words = text[x["start"]:x["end"]]
                if e is None or not shows(e, words):
                    continue
                out += [esc(text[last:x["start"]]), self.span(e, words)]
                last = x["end"]
            return "".join(out) + esc(text[last:])
        except Exception:  # noqa: BLE001
            return esc(text)

    def _sum_entity(self, ep, read):
        """BUILD IC-4 (D3): a sum's entity and scope in one form, whoever built it (a sum:<endpoint>.<field> id, the engine's named sums,
        a capacity set's totals): the records it reads — "all" when it reads every record of the endpoint, else their ids, sorted — and
        "Σ over <k> of <n> records"."""
        n = len(self._recs(ep) or [])
        ids = sorted({str(x) for x in read}) if isinstance(read, (list, tuple)) else None
        k = n if read == "all" else len(ids or [])
        return {"endpoint": ep, "records": "all" if n and k == n else ids}, f"\u03a3 over {k} of {n} records"

    def fact(self, e):
        """BUILD IC-4 (D3): the entry's fact — {entity, measure, unit, scope, as_of, scenario}, and a quoted figure's claim: the parts its
        builder gave (a sum's records, a text figure's record, an alias's field), else what its id says (_fact_parts); then one form
        for each part, whoever built it — a record by its endpoint and id (a one-record endpoint's id its record key's value, or none)
        and its name by record_name; a sum's records and scope by _sum_entity — and the as-of by one rule: the record's own time field,
        else its endpoint's, else the fetch of the run (as_of_of; a sum or a count reads its endpoint's); a figure that reads no
        endpoint, the run it was made in. The measure is the
        contract's metric where `authority` names the figure (the authority's entry beside it, with the part of a quote it names).
        Scenario: "Block 1" for Block 1's copies (Block 1's fact taken whole when it has one), "modeled" for the writer's assumptions
        and what is computed from them, else "this run". Never raises."""
        try:
            iid = str(e.get("id") or "")
            given = e.get("fact") if isinstance(e.get("fact"), dict) else {}
            if iid.startswith("b1.") and isinstance(given.get("entity"), dict) and given["entity"]:
                return dict(given, scenario="Block 1")
            base = iid[3:] if iid.startswith("b1.") else iid
            p = dict(self._fact_parts(base, e))
            p.update({k: v for k, v in given.items() if v not in (None, "", {})})
            fetched = self.fetched or {"value": self.run_id, "from": "fetched in this run"}
            ent, asof = dict(p.get("entity") or {}), p.get("as_of")
            ep = ent.get("endpoint")
            if ep:
                if "records" in ent:
                    ent, p["scope"] = self._sum_entity(ep, ent["records"])
                    asof = asof or as_of_of(None, self._raw.get(ep))
                elif "id" in ent:
                    recs = self._recs(ep) or []
                    if ent["id"] is None and len(recs) == 1:
                        k = self._rkey(ep, recs)
                        ent["id"] = str(recs[0].get(k)) if k and recs[0].get(k) is not None else None
                        rec = recs[0]
                    else:
                        rec = self._rec_of(ep, ent["id"])[0] if ent["id"] is not None else None
                    ent["name"] = ent.get("name") or record_name(rec)
                    asof = asof or as_of_of(rec, self._raw.get(ep))
                else:
                    asof = asof or as_of_of(None, self._raw.get(ep))
            modeled = e.get("kind") == "MODELED" or any((self.entries.get(i) or {}).get("kind") == "MODELED" for i in e.get("inputs") or [])
            made = self.made or {"value": self.run_id, "from": "made in this run"}
            f = {"entity": ent, "measure": p.get("measure"), "unit": p.get("unit") or e.get("unit") or "", "scope": p.get("scope"),
                 "as_of": asof or (fetched if ep else made),
                 "scenario": p.get("scenario") or ("Block 1" if iid.startswith("b1.") else ("modeled" if modeled else "this run"))}
            if p.get("claim") is not None:
                f["claim"] = p["claim"]
            a, part = self.authority.get(base), None
            if a is None and e.get("quoted"):
                for fig, x in self.authority.items():
                    plain, _, prt = fig.partition("#")
                    w_, _, written = plain[2:].partition("|") if plain.startswith("q:") else ("", "", "")
                    if written and w_.strip() == str(e.get("where") or "") and _norm_quote(written) == _norm_quote(str(e.get("written") or "")):
                        a, part = x, prt or None
                        break
            if a:
                f["measure"] = a.get("metric")
                f["authority"] = {"metric": a.get("metric"), "words": a.get("words"), "endpoint": a.get("endpoint"),
                                  "as_of_rule": a.get("as_of"), "part": part}
            return f
        except Exception as ex:  # noqa: BLE001 — live, a fact that cannot be read names why, never stops a page (§1's rule)
            if strict():         # BUILD IC-8 (d11c; F17): under NAIPC_STRICT=1 it raises — a fact Python cannot read is Python's error
                raise
            return {"entity": {"named": str(e.get("id"))}, "measure": str(e.get("id")), "unit": e.get("unit") or "", "scope": None,
                    "as_of": None, "scenario": None, "why": type(ex).__name__}

    def _name_of(self, ent):
        """The name of an entity given by its endpoint and id (the record's, by record_name), or None."""
        if isinstance(ent, dict) and ent.get("endpoint") and ent.get("id") is not None:
            return record_name(self._rec_of(ent["endpoint"], ent["id"])[0])
        return None

    def set_data(self, raw, records_fn, tags=None):
        """This run's payloads (endpoint → payload) and the envelope rule (naipc_requirements.records), for data ids by grammar."""
        self._raw = raw if isinstance(raw, dict) else {}
        self._records = records_fn
        self.tags = dict(tags or {})

    def _recs(self, ep):
        if ep not in self._raw or self._records is None:
            return None
        try:
            r = self._records(self._raw[ep])
        except Exception:  # noqa: BLE001
            return None
        return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else None

    def _rkey(self, ep, recs):
        """BUILD IC-3 (D6): record_key for the quote, the mentions and the stale list — None for a one-record endpoint whose key would be
        a field other than "id" (its first text field read as an id: the field was never offered to quote, found by t_mentions_ic3)."""
        k = self.record_key(ep)
        return None if len(recs) == 1 and k != "id" else k

    def record_key(self, ep):
        """The field that names one record of an endpoint in [[<endpoint>.<record id>.<field>]]: "id" when every record has a distinct
        one, else the first field (in the records' order) whose plain values are present and distinct in every record; None without."""
        recs = self._recs(ep) or []
        if not recs:
            return None
        keys = ["id"] + [k for k in dict.fromkeys(k for r in recs for k in r) if k != "id"]
        for k in keys:
            vals = [r.get(k) for r in recs]
            if not (all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in vals) and len({str(v) for v in vals}) == len(vals)):
                continue
            if k == "id" or all(isinstance(v, str) for v in vals) or _words(k) & KEY_WORDS:   # a count that happens to differ is no key
                return k
        return None

    def _data_entry(self, id):
        """A data id by grammar → a new entry (cached), or None."""
        tag = self.tags.get(id) or {}

        def mk(value, kind, fmt, formula=None, inputs=None, source=None, lab=None):     # B3 (Phase 4, review d F9): a label in words
            return self.add(id, value, kind, fmt=tag.get("fmt") or fmt, unit=tag.get("unit", ""), label=tag.get("label") or lab or id,
                            source=source or id, formula=formula, inputs=inputs, cls=tag.get("class"), window=tag.get("window"),
                            cohort=tag.get("cohort"), assumption=tag.get("assumption"))
        if id.startswith("sum:"):
            ep, _, fld = id[4:].partition(".")
            recs = self._recs(ep)
            if not recs or not fld:
                return None
            a, b = _sum_fields(recs, fld)                       # BUILD IC-4 (D3): one rule for the sum and its fact
            vals = [v for v in (_sum_value(x, a, b) for x in recs) if v is not None]
            if not vals:
                return None
            what = f"{a} − {b}" if b else a
            return mk(sum(vals), "DERIVED", "count" if all(float(t).is_integer() for t in vals) else "num",
                      formula=f"Σ ({what}) over {len(vals)} of {len(recs)} {ep} records", inputs=[ep], lab=f"{what}, summed over the {ep} records")
        parts = id.split(".")
        if len(parts) == 2:
            ep, fld = parts
            recs = self._recs(ep)
            if recs is None:
                return None
            if fld == "count":
                if ep in self.unavailable or (not recs and getattr(self, "block", None) == 2
                                              and any(str(i).startswith(ep + ".") for i in self.block1_ids)):
                    return None         # IC-4 Phase 7 (review b9, D2): an endpoint that did not load has no count — never 0
                return mk(len(recs), "MEASURED", "count", source=f"{ep}: the records it returned", lab=f"the {ep} records")
            if len(recs) == 1:
                fld2 = self._bound_field(id, ep, recs[0], fld)
                v = recs[0].get(fld2) if fld2 else None
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tag = (self.tags.get(f"{ep}.{fld2}") or tag) if fld2 != fld else tag     # IC-4 (D3): an alias reads the new field's tags
                    return self._aliased(mk(v, "MEASURED", guess_fmt(fld2, v), lab=f"{fld2} ({ep})"), ep, fld, fld2)
            return None
        if len(parts) == 3:
            ep, rid, fld = parts
            recs = self._recs(ep) or []
            k = self.record_key(ep)          # B3 (Phase 4, review a F3): "id", else the endpoint's unique key (a zip, an area code)
            rec = next((r for r in recs if k and str(r.get(k)) == rid), None)
            fld2 = self._bound_field(id, ep, rec, fld) if rec else None
            v = rec.get(fld2) if rec and fld2 else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tag = (self.tags.get(f"{ep}.{rid}.{fld2}") or tag) if fld2 != fld else tag     # IC-4 (D3): an alias reads the new field's tags
                return self._aliased(mk(v, "MEASURED", guess_fmt(fld2, v), lab=f"{fld2} of {rid} ({ep})"), ep, fld, fld2)
        return None

    def _bound_field(self, id, ep, rec, fld):
        """BUILD IC-4 (D2, D3): the field a cited data id reads in its record — its own name, else the new name an `aliases` entry of
        the contract gives it (the binding the operator confirmed); None when the record holds neither — never rebound silently, and
        named an unresolved binding when Block 1 cited the id."""
        if not isinstance(rec, dict):
            return None
        if fld in rec:
            return fld
        a = self.aliases.get(f"{ep}.{fld}")
        new = a["new"].partition(".")[2] if a and a["new"].partition(".")[0] == ep else None
        if new and new in rec:
            return new
        if id in self.block1_ids:           # a field Block 1 cited, gone this run: named; any other is an unknown id, as before
            self._unbound[id] = (f"an unresolved binding: Block 1 cited {id}; {ep} holds no field {fld!r} in this record this run"
                                 + (f" (the contract's alias names {a['new']!r}, which it does not hold either)" if a else
                                    " and the contract's aliases map it to nothing"))
        return None

    def _aliased(self, e, ep, old, new):
        """The entry of a data id read through an alias carries it (its fact's measure the new field)."""
        if e and old != new:
            e["alias"] = {"old": f"{ep}.{old}", "new": f"{ep}.{new}", "why": (self.aliases.get(f"{ep}.{old}") or {}).get("why")}
            e["fact"] = dict(e.get("fact") or {}, measure=new)
            self.notes.append(f"{e['id']}: read through the contract's alias {ep}.{old} \u2192 {ep}.{new}")
        return e

    def get(self, id):
        """The entry of an id (a registered one, or a data id read by grammar), or None."""
        if not isinstance(id, str):
            return None
        return self.entries.get(id) or self._data_entry(id)

    def name_ids(self, fields=("label", "source", "formula", "assumption")):
        """BUILD IC-5 Phase 4 (the replays: D4, "0 unreviewed hits on every page of every replay"): an entry's own words — Python's label,
        source, formula and assumption — name a figure of this registry by its label, never by its id. This build's code writes words
        (its runs name none: T-j-4); an older build's saved words — the financial model checkpoint of IC-3 or IC-4 a rebuild reads
        (--stage html_assembly: R6's Block 1, IC-3's r6 and r8) and Block 1's registry read in Block 2 — wrote ids ("coi_y1 + coi_y2 +
        coi_y3", "citing ext.wage_chw_median"), which a figure's title prints. Each figure keeps the words of the build that computed it
        (IC-3's cost of inaction is its ER visits); only its ids become words. An id here: a run of letters, digits, "_", ".", ":" and
        "-" (_PY_ID_RUN) holding "_", "." or ":" that is an entry of this registry with a label of its own — never the entry's own id,
        and never the data's own address (a data id — an endpoint of the run first — or a sum:), which the contract's words name.
        Returns [(entry id, field, the id named)]."""
        eps = {str(k).lower() for k in (getattr(self, "_raw", None) or {})}
        out = []
        for e in list(self.entries.values()):
            for f in fields:
                t = e.get(f)
                if not isinstance(t, str) or not any(c in t for c in "_.:"):
                    continue

                def one(m, e=e, f=f):
                    w = m.group(0)
                    o = self.entries.get(w)
                    lab = str((o or {}).get("label") or "").strip()
                    if (o is None or w == e.get("id") or not any(c in w for c in "_.:") or w.lower().startswith("sum:")
                            or w.split(".")[0].lower() in eps or not lab or lab == w or "[[" in lab):
                        return w
                    out.append((e.get("id"), f, w))
                    return lab
                u = _PY_ID_RUN.sub(one, t)
                if u != t:
                    e[f] = u
        return out

    def lookup(self, inner):
        """A token's inside → (entry, note) or (None, why). Exact id, then an alias, then a data id, then a unique case-insensitive
        match; model:<number>|<what> registers a MODELED proposal."""
        t = inner.strip()
        if t.lower().startswith("model:"):
            num, _, what = t[6:].partition("|")
            v, what, s = parse_number(num), re.sub(r"\s+", " ", what).strip(), num.strip()
            if v is None or not what:
                return None, f"a proposal needs a number and what it is: [[{t[:60]}]]"
            key = (s, proposal_label_key(what[:120]))       # BUILD IC-4 (D1): the label as its id reads it
            if key in self._model_ids:
                return self.entries[self._model_ids[key]], "a proposal of the model"
            fmt = "money" if "$" in s else ("pct" if s.endswith("%") else ("count" if float(v).is_integer() else "num"))
            pid = proposal_id(what, s, self.entries)        # BUILD IC-4 (D1): from its label, so it keeps its id across blocks
            e = self.add(pid, v, "MODELED", fmt=fmt, label=what[:120], source="the model's own proposal, in its words")
            if e:
                e["written"] = s            # the number as written: a saved registry rebuilds the proposal's id from it (read())
                self._model_ids[key] = pid
            return (e, "a proposal of the model") if e else (None, "the proposal could not be registered")
        low_t = t.lower()                   # BUILD IC-2 (D1): every form resolves here — one path for every reader
        mid_ = _mention_id_of(t)            # BUILD IC-3 (D6): a mention's id — the dot form in any letter case, or [[q:a1b2c]]
        if mid_ is not None and mid_ not in self.entries and mid_ in self.stale:
            # D11 (review b #3): Block 1's mention that this run's data no longer holds — named, with no value and never tied to a
            # figure of this run, so a site that asks what a token names (a baseline, an evidence item, a headline) keeps it, and the
            # page shows Block 1's words, flagged (resolve)
            return self._stale_entry(mid_), "a mention of Block 1's data that this run's data no longer holds"
        if _MENTION_COLON.fullmatch(low_t):                         # [[q:a1b2c]] is [[q.a1b2c]] — an id, never a quote
            if mid_ in self.entries:
                return self.entries[mid_], f"[[{t[:20]}]] read as [[{mid_}]] (a mention's id)"
            if self.id_alias.get(mid_) in self.entries:          # BUILD IC-4 (D2): the colon form of an aliased id
                return self.entries[self.id_alias[mid_]], f"[[{t[:20]}]] read as [[{self.id_alias[mid_]}]] (the same claim of the organizers' text)"
            near = self.nearest_id(mid_)                            # review a #6, b #5: an id not on the list is D4's unknown id
            return None, f"no figure named {mid_!r} (written [[{t[:20]}]])" + (f" (the nearest id: {near})" if near else "")
        if low_t.startswith("count:"):                  # BUILD IC-4 (D13): how many items a list of the page shows
            name = t[6:].strip()
            tr_, _d, ls_ = name.partition(".")          # BUILD IC-8 (D2): a track's list read before its counts are sealed is Python's error
            if ls_ and tr_ in TRACK_COUNTS and ls_ in TRACK_COUNTS[tr_] and tr_ not in self.sealed_counts:
                raise StageError(f"count.{tr_}.{ls_} read before the {tr_}'s counts are sealed (D2: count the final membership, seal, "
                                 "then resolve)")
            e = self.entries.get(f"count.{name}")
            if e is not None:
                return e, None
            return None, (f"no list named {name[:40]!r} counted on this page (the lists: {', '.join(COUNT_LISTS)})" if name not in COUNT_LISTS
                          else f"the page's {COUNT_NAMES[name]} are not counted here: a count is printed when the page is built")
        if low_t.startswith("q:"):
            return self._quote(t[2:])
        if low_t.startswith("calc:"):
            return self._calc(t[5:])
        if low_t.startswith("nf:"):
            return None, "a declared non-figure names no figure"
        al_ = self.id_alias.get(mid_) if mid_ is not None and mid_ not in self.entries else None
        if al_ and al_ in self.entries:                          # BUILD IC-4 (D2): an IC-3 id or a re-anchored Block 1 id
            return self.entries[al_], f"[[{t[:20]}]] read as [[{al_}]] (the same claim of the organizers' text)"
        if t in ALIASES:
            e = self.get(ALIASES[t])
            return (e, f"[[{t}]] read as [[{ALIASES[t]}]]") if e else (None, f"no figure {ALIASES[t]} in this run")
        e = self.get(t)
        if e:
            return e, None
        if t in self._unbound:                  # BUILD IC-4 (D2): a field gone with no alias — named, never rebound
            return None, self._unbound[t]
        low = [k for k in self.entries if k.lower() == t.lower()]
        if len(low) == 1:
            return self.entries[low[0]], f"[[{t}]] read as [[{low[0]}]] (letter case)"
        if re.search(r"[.!?]\s+[A-Z]", t):     # BUILD IC-3 (D4): the brackets swallowed a sentence's end
            return None, f"a token whose inside holds a sentence's end ({t[:60]!r}) — its brackets took in text"
        near = self.nearest_id(t)               # BUILD IC-3 (D4): the nearest real id in the reason
        return None, f"no figure named {t[:80]!r}" + (f" (the nearest id: {near})" if near else "")

    def nearest_id(self, t, limit=3):
        """BUILD IC-3 (D4): the registered or data id nearest to `t` by edit distance (at most `limit` edits, or a third of its length),
        or None — for the reason a token naming nothing gives."""
        cands = set(self.entries)
        for ep in self._raw:
            recs = self._recs(ep) or []
            cands.add(f"{ep}.count")
            k = self.record_key(ep)
            for r in recs[:200]:
                for f, v in r.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        cands.add(f"{ep}.{f}" if len(recs) == 1 else f"{ep}.{r.get(k)}.{f}")
        best, bd = None, max(1, min(limit, len(t) // 3))
        for c in cands:
            if abs(len(c) - len(t)) > bd:
                continue
            d = _edits(t.lower(), c.lower(), bd)
            if d <= bd and (best is None or d < best[0]):
                best = (d, c)
        return best[1] if best else None

    # ── BUILD IC-2: the writer's forms ──
    def _quote_texts(self, where):
        """[(where the text sits, the text)] that `where` names — a record's text field, that field in every record of an endpoint, the
        brief, the announcement — or (None, why). A list of strings is one text (its items joined)."""
        w = where.strip()
        if w.lower() in QUOTE_PLACES:
            t = self.sources.get(w.lower())
            return ([(f"the {w.lower()}", t)], None) if isinstance(t, str) and t.strip() else (None, f"no {w.lower()} in this run")
        parts = w.split(".")
        if len(parts) < 2 or not all(parts):
            return None, f"{w[:60]!r} names no text (where is <endpoint>.<record id>.<field>, <endpoint>.<field>, brief or announcement)"
        ep, fld, rid = parts[0], parts[-1], ".".join(parts[1:-1]) or None
        recs = self._recs(ep)
        if recs is None:
            return None, f"no endpoint {ep!r} in this run"
        k = self._rkey(ep, recs)              # IC-3 (D6): a one-record endpoint's first text field is no record id
        if rid is not None:
            rec = next((r for r in recs if k and str(r.get(k)) == rid), None)
            if rec is None:
                return None, f"no record {rid!r} in {ep}"
            pool = [(rid, rec)]
        else:
            pool = [(str(r.get(k)) if k and r.get(k) is not None else str(i + 1), r) for i, r in enumerate(recs)]
        out = []
        for rid_, r in pool:
            v = r.get(fld)
            if isinstance(v, str) and v.strip():
                out.append((f"{ep} {rid_} {fld}", v))
            elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                out.append((f"{ep} {rid_} {fld}", " · ".join(v)))
        if not out:
            return None, f"{w[:60]}: not a text field" + ("" if rid else " of any record")
        return out, None

    def add_counts(self, shown, extra=None):
        """BUILD IC-4 (D13): how many items each list of the page shows — {list's field name: count} — registered as count.<list>,
        DERIVED (Python counts the items the page shows, after every filter), format count, so [[count:<list>]] prints it. A list that is
        no page list, or a count already registered, is skipped. Returns the entries registered."""
        out = []
        extra = extra or {}       # IC-4 Phase 7 (review c16): any other list field of a reply's schema — {field: its name in words}
        for name, n in (shown or {}).items():
            if (name not in COUNT_LISTS and name not in extra) or not isinstance(n, int):
                continue
            if f"count.{name}" in self.entries:                 # BUILD IC-8 (D2): a recount must agree — raised strict, recorded live
                if self.entries[f"count.{name}"].get("value") != n:
                    _count_problem(self, f"count.{name} counted again as {n} (first {self.entries[f'count.{name}'].get('value')})")
                continue
            words = COUNT_NAMES.get(name) or extra[name]
            e = self.add(f"count.{name}", n, "DERIVED", fmt="count",
                         label=f"the {words} this page shows" if name in COUNT_LISTS else f"the {words} the analysis lists",
                         formula="the items its list shows, after every filter" if name in COUNT_LISTS else "the items of the analysis's list",
                         cls="count")
            if e:
                e["counted"] = True          # its title says what it is: counted on this page, not computed from the data (title)
                out.append(e)
                self.counted[name] = words
        return out

    def add_mentions(self):
        """BUILD IC-3 (D6): register this run's list of mentions and return it — every expression the one reader reads as a quantity in
        the data's text fields (the fields text_fields_with_numbers takes, its exclusions kept: _text_field_ok), the brief as the request
        shows it and the announcement; each a quoted entry like a quote's (its whole expression shown as the source writes it, hedge
        included; a value only for a single exact value) under an id from its content: "q." and five characters of a hash of the place,
        the sentence, the expression's words and which of those words in the sentence it is — seven where two would share five. So a
        rebuild, Block 2 and the other pages get the same id, and a changed sentence a new one. Order: the announcement, the brief, then
        the data in its order. Needs set_data and the sources first. Returns [{id, where, label, sentence, words}]."""
        if self._ex is None:
            self._ex = Exempter.from_data(self._raw, self._records) if self._records is not None else Exempter()
        places = [(w, f"the {w}", self.sources.get(w)) for w in ("announcement", "brief") if isinstance(self.sources.get(w), str)]
        for ep in self._raw:
            recs = self._recs(ep) or []
            key = self._rkey(ep, recs)            # a one-record endpoint with no "id" has no record id: its fields are <endpoint>.<field>
            for i, r in enumerate(recs):
                rid = str(r.get(key)) if key and r.get(key) is not None else str(i + 1)
                for fld, v in r.items():
                    if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                        v = " · ".join(v)
                    if isinstance(v, str) and v.strip() and _text_field_ok(fld, key):
                        places.append((f"{ep}.{rid}.{fld}" if key else f"{ep}.{fld}", f"{ep} {rid} {fld}", v))
        found = []
        for where, lab, t in places:
            if not isinstance(t, str) or not t.strip():
                continue
            for x in quantities(t, names=self._ex):
                if x["role"] != "quantity":
                    continue
                sent = _debracket(_sentence_around(t, x["start"], x["end"], n=400))
                words = re.sub(r"\s+", " ", x["words"]).strip()
                skey = _mention_key(sent)           # IC-3 (review b #2): the id read the sentence — kept only for IC-3's id, an alias (D2)
                k3 = sum(1 for y in found if y["where"] == where and y["skey"] == skey and y["words"] == words)
                legacy = hashlib.sha1(f"{where}|{skey}|{words}|{k3}".encode("utf-8")).hexdigest()
                tail = t[x["end"]:x["end"] + 80]
                claim = claim_key(x, tail)          # BUILD IC-4 (D2): the id reads the place and the claim, never the sentence
                loose, uw = claim_loose(x, tail)
                k = sum(1 for y in found if y["where"] == where and y["claim"] == claim)
                h = hashlib.sha1(f"{where}|{claim}|{k}".encode("utf-8")).hexdigest()
                ctx = [_debracket(t[max(0, x["start"] - CTX_CHARS):x["start"]]), _debracket(t[x["end"]:x["end"] + CTX_CHARS])]
                found.append({"where": where, "label": lab, "x": x, "sentence": sent, "skey": skey, "words": words, "hash": h,
                              "legacy": legacy, "claim": claim, "loose": loose, "unit_words": uw, "occ": k, "ctx": ctx})
        short, short3 = {}, {}
        for y in found:
            short.setdefault(y["hash"][:5], set()).add(y["hash"])
            short3.setdefault(y["legacy"][:5], set()).add(y["legacy"])
        of_n = {}
        for y in found:
            of_n[(y["where"], y["claim"])] = of_n.get((y["where"], y["claim"]), 0) + 1
        out = []
        for y in found:
            mid = "q." + (y["hash"][:5] if len(short[y["hash"][:5]]) == 1 else y["hash"][:7])
            old_id = "q." + (y["legacy"][:5] if len(short3[y["legacy"][:5]]) == 1 else y["legacy"][:7])
            x = y["x"]
            single = x.get("shape") == "single" and not x.get("hedge") and not x.get("op") and x.get("value") is not None
            fmt = "pct" if (x.get("pct") or x.get("_frac")) else ("money" if x.get("money") else "num")
            who = "the brief's" if y["where"] == "brief" else ("the announcement's" if y["where"] == "announcement" else "the record's")
            e = self.add(mid, x["value"] if single else None, "MEASURED", fmt=fmt, label=_debracket(f"in {who} own words · {y['label']}"),
                         source=_debracket(y["label"]), quote=y["sentence"], novalue=None if single else y["words"])
            if not e:
                continue
            e.update(shown=y["words"], shown_as=y["words"], quoted=True, where=y["where"], written=y["words"], holders=1, mention=True,
                     shape=x.get("shape") or "as written", hedge=x.get("hedge"), op=x.get("op"), role=x.get("role"),
                     claim=y["claim"], loose=y["loose"], unit_words=y["unit_words"], occ=y["occ"], of_n=of_n[(y["where"], y["claim"])],
                     ctx=y["ctx"], ic3_id=old_id)
            self._mention_at[(y["label"], x["start"], x["end"])] = mid
            out.append({"id": mid, "where": y["where"], "label": y["label"], "sentence": y["sentence"], "words": y["words"]})
        ids = {m["id"] for m in out}
        for e in (self.entries[m["id"]] for m in out):
            old_id = e.get("ic3_id")
            if old_id and old_id != e["id"] and old_id not in ids:   # BUILD IC-4 (D2): an IC-3 id a reply or a Block 1 text cites reads as the
                self.id_alias[old_id] = e["id"]                       # claim's id (an IC-3 id that is another mention's new id is not aliased)
        self.mentions = out
        return out

    def _place_text(self, where):
        """BUILD IC-4 (D2): the text of a mention's place on this run's data — the brief or the announcement as this run holds it, or the
        record's field (a list of strings joined as add_mentions joins it; for an endpoint with no record key, the field of every record,
        as add_mentions names them all <endpoint>.<field>) — or None when the record or the field is no longer there."""
        def text_of(v):
            if isinstance(v, list) and v and all(isinstance(z, str) for z in v):
                v = " · ".join(v)
            return v if isinstance(v, str) and v.strip() else None
        where = str(where or "")
        if where in QUOTE_PLACES:
            return text_of(self.sources.get(where))
        parts = where.split(".")
        if len(parts) < 2 or parts[0] not in self._raw:
            return None
        recs = self._recs(parts[0]) or []
        key = self._rkey(parts[0], recs)
        if len(parts) == 2:
            got = [t for t in (text_of(r.get(self._alias_field(parts[0], r, parts[1]))) for r in recs) if t] if not key else []
            return " · ".join(got) if got else None
        rid, fld = ".".join(parts[1:-1]), parts[-1]
        rec = next((r for r in recs if key and str(r.get(key)) == rid), None)
        return text_of(rec.get(self._alias_field(parts[0], rec, fld))) if isinstance(rec, dict) else None

    def _aliased_place(self, where):
        """IC-4 Phase 7 (review a7, D2): a mention's place on this run's data — <endpoint>.<record id>.<field> (or <endpoint>.<field>)
        with the field a confirmed alias renamed, when the record holds the new name and not the old; else the place as it is."""
        parts = str(where or "").split(".")
        if len(parts) < 2 or parts[0] in QUOTE_PLACES or parts[0] not in self._raw or not self.aliases:
            return where
        ep, fld = parts[0], parts[-1]
        recs = self._recs(ep) or []
        key = self._rkey(ep, recs)
        rec = (next((r for r in recs if key and str(r.get(key)) == ".".join(parts[1:-1])), None) if len(parts) > 2
               else (recs[0] if recs else None))
        new = self._alias_field(ep, rec, fld)
        return ".".join(parts[:-1] + [new]) if new != fld else where

    def _alias_field(self, ep, rec, fld):
        """IC-4 Phase 7 (review a7, D2): the field a mention's place reads — its own name, else the new name a confirmed alias of the
        contract gives it (as a data id's binding, _bound_field)."""
        if not isinstance(rec, dict) or fld in rec:
            return fld
        a = self.aliases.get(f"{ep}.{fld}")
        new = a["new"].partition(".")[2] if a and a["new"].partition(".")[0] == ep else None
        return new if new and new in rec else fld

    def _unavailable(self, ep):
        """BUILD IC-4 (D2): an endpoint that did not load this run — named by the data step's errors (self.unavailable), absent from this
        run's data, or with no record that holds a field (an empty list, {})."""
        if ep in QUOTE_PLACES:
            return False
        if ep in self.unavailable or ep not in self._raw:
            return True
        return not any(isinstance(r, dict) and r for r in (self._recs(ep) or []))

    def _claim_of_b1(self, e):
        """BUILD IC-4 (D2): a Block 1 mention's claim — {claim, loose, unit_words, ctx}: as stored by this build's add_mentions, or, for an
        IC-3 entry (its id came from its sentence; no claim stored), its words read again inside its own sentence by the one reader. An
        IC-3 entry whose words the reader does not read there as a quantity has none (None)."""
        if e.get("claim") and e.get("loose") is not None:
            return {"claim": e["claim"], "loose": e["loose"], "unit_words": list(e.get("unit_words") or []), "ctx": e.get("ctx") or ["", ""]}
        sent, words = str(e.get("quote") or ""), re.sub(r"\s+", " ", str(e.get("written") or e.get("shown") or "")).strip()
        for x in quantities(sent, names=self._ex):
            if x.get("role") == "quantity" and re.sub(r"\s+", " ", x["words"]).strip() == words:
                tail = sent[x["end"]:x["end"] + 80]
                loose, uw = claim_loose(x, tail)
                return {"claim": claim_key(x, tail), "loose": loose, "unit_words": uw,
                        "ctx": [sent[max(0, x["start"] - CTX_CHARS):x["start"]], sent[x["end"]:x["end"] + CTX_CHARS]]}
        return None

    REANCHOR_MIN, REANCHOR_MARGIN = 0.5, 0.15     # BUILD IC-4 (D2): the context picks one of several equal claims only by a clear margin

    def add_stale_mentions(self, b1_entries, b1_raw=None):
        """BUILD IC-4 (D2; was IC-3's D11) — at Block 2, each mention of Block 1's list re-anchored in its place on this run's data, by its
        claim, never by its sentence. The value, the hedge, the bound and the per part match exactly; the unit by its words (claim_loose)
        when the strict key does not; only the context (32 characters each side) is compared loosely. Statuses:
          unchanged — one claim of this run in the place matches: the only one, its context not contradicting Block 1's (a shared content
                      word, or no words on one side); or, among several, the one whose context is nearest by a clear margin (or is
                      Block 1's exactly). Its id is Block 1's: this run's entry takes it ("the id holds"), and an entry of this run whose
                      own id is a Block 1 id held or retired gets a fresh one (never reused).
          changed — the place is there and no claim of it matches (the field's text now is shown);
          ambiguous — several claims match and the context picks none, or the only one's context shares nothing with Block 1's;
          gone — the record or the field is no longer in the data;
          source unavailable — the endpoint did not load this run (_unavailable): never "changed", never zero.
        An unchanged claim whose sentence changed is listed in self.review — for the update, never a flag. Every status but unchanged is
        in self.stale: a token naming it resolves to Block 1's words in a no-link span, flagged and counted in U (resolve), never tied to
        another record that holds the same number. self.reanchor holds every Block 1 mention's status. `b1_raw`: Block 1's payloads (its
        base's data_loading), whose place texts the one-candidate check reads; without them, Block 1's sentence. Returns the stale list."""
        norm = lambda z: re.sub(r"\s+", " ", str(z or "")).strip()   # noqa: E731
        b1p = None
        if isinstance(b1_raw, dict) and b1_raw and self._records is not None:
            b1p = Registry(self.challenge, None, 1)
            b1p.set_data(b1_raw, self._records)
            b1p.sources = dict(self.sources)
        live = [mid for mid, e in self.entries.items() if e.get("mention")]
        by_strict, by_loose = {}, {}
        for mid in live:
            e = self.entries[mid]
            by_strict.setdefault((e.get("where"), e.get("claim")), []).append(mid)
            by_loose.setdefault((e.get("where"), e.get("loose")), []).append(mid)
        picks = {}                                   # Block 1 id → (status, this run's id or None, score, why)
        for k, e in sorted((b1_entries or {}).items()):
            if not (isinstance(e, dict) and e.get("mention")):
                continue
            where = self._aliased_place(str(e.get("where") or ""))     # IC-4 Phase 7 (review a7): a place renamed by a confirmed alias
            c1 = self._claim_of_b1(e)
            if self._unavailable(where.split(".")[0]):
                picks[k] = ("source unavailable", None, 0, "the endpoint did not load")
                continue
            if self._place_text(where) is None:
                picks[k] = ("gone", None, 0, "the record or the field is no longer in the data")
                continue
            if c1 is None:
                picks[k] = ("changed", None, 0, "Block 1's words are not read as a quantity in Block 1's own sentence")
                continue
            cands = list(by_strict.get((where, c1["claim"]), []))
            how = "the same claim"
            if not cands:
                uw = set(c1["unit_words"])
                cands = [c for c in by_loose.get((where, c1["loose"]), [])
                         if unit_words_match(c1["claim"], uw, self.entries[c].get("claim"), self.entries[c].get("unit_words"))]
                how = "the same claim, its unit written another way"
            if not cands:
                picks[k] = ("changed", None, 0, "no claim of the place matches")
                continue
            ctx1 = c1["ctx"] or ["", ""]
            exact = [c for c in cands if [norm(z) for z in (self.entries[c].get("ctx") or ["", ""])] == [norm(z) for z in ctx1]]
            if k in exact or len(exact) == 1:
                picks[k] = ("unchanged", k if k in exact else exact[0], 2.0, how + ", in the same words")
                continue
            if len(cands) == 1:
                # one candidate: the same claim unless the place now shares no word with Block 1's place (the field replaced, the
                # figure kept by chance) — Block 1's place text from Block 1's data, else Block 1's sentence; the claim's own unit and per set aside
                drop = _claim_unit_per(c1["claim"]) | _claim_unit_per(self.entries[cands[0]].get("claim"))
                p1 = (b1p._place_text(where) if b1p is not None and where not in QUOTE_PLACES else None) or e.get("quote") or ""
                a, b = _ctx_words([p1], drop), _ctx_words([self._place_text(where) or ""], drop)
                fa, fb = (set().union(*_ctx_sides(c_, drop)) for c_ in (ctx1, self.entries[cands[0]].get("ctx") or ["", ""]))
                if fa and fb and not _ctx_share(fa, fb):      # IC-4 Phase 7 (review a3, D2): the figure's own words are not Block 1's —
                    u1 = _claim_unit(c1["claim"])               # does Block 1's sentence survive in the place with another value of its unit?
                    kept = next((c2 for c2, e2 in self.entries.items() if c2 != cands[0] and e2.get("mention") and e2.get("where") == where
                                 and e2.get("claim") != self.entries[cands[0]].get("claim") and _claim_unit(e2.get("claim")) == u1
                                 and _ctx_share(fa, set().union(*_ctx_sides(e2.get("ctx") or ["", ""], drop | _claim_unit_per(e2.get("claim")))))),
                                None)
                    if kept:            # the original changed; the equal figure elsewhere is a newcomer — never a silent transfer
                        picks[k] = ("ambiguous", None, 0, "Block 1's sentence now holds another value; the equal figure elsewhere in the place "
                                                          "is not taken for it")
                        continue
                if not a or not b or _ctx_share(a, b):
                    picks[k] = ("unchanged", cands[0], 1.0, how)
                else:
                    picks[k] = ("ambiguous", None, 0, "the place still holds the figure, but none of its words are those of Block 1's text")
                continue
            drop = _claim_unit_per(c1["claim"])
            sc = sorted((((_ctx_score(ctx1, self.entries[c].get("ctx") or ["", ""], drop | _claim_unit_per(self.entries[c].get("claim"))) or 0.0), c)
                         for c in cands), reverse=True)
            if sc[0][0] >= self.REANCHOR_MIN and sc[0][0] - sc[1][0] >= self.REANCHOR_MARGIN:
                picks[k] = ("unchanged", sc[0][1], sc[0][0], how + ", told apart by the words around it")
            else:
                picks[k] = ("ambiguous", None, 0, "the place holds the figure more than once and the words around it do not say which")
        # one this-run claim answers one Block 1 mention: two picks of one claim keep the clearly nearer, else neither
        by_now = {}
        for k, (st, c, sc_, _w) in picks.items():
            if st == "unchanged":
                by_now.setdefault(c, []).append((sc_, k))
        for c, ks in by_now.items():
            if len(ks) > 1:
                ks.sort(reverse=True)
                keep = ks[0][1] if ks[0][0] - ks[1][0] >= self.REANCHOR_MARGIN else None
                for _s, k in ks:
                    if k != keep:
                        picks[k] = ("ambiguous", None, 0, "another of Block 1's mentions matches the same figure as nearly")
        # the ids: an unchanged claim keeps Block 1's id; an entry of this run whose own id is a Block 1 id held or retired gets a fresh one
        carry = {c: k for k, (st, c, _s, _w) in picks.items() if st == "unchanged"}
        held = set(carry.values()) | {k for k, p in picks.items() if p[0] != "unchanged"}
        final = dict(carry)
        used = set(final.values()) | held | {i for i in self.entries if i not in live}
        for mid in live:
            if mid in final:
                continue
            new = mid
            if mid in used:
                e = self.entries[mid]
                h = hashlib.sha1(f"{e.get('where')}|{e.get('claim')}|{e.get('occ')}|fresh".encode("utf-8")).hexdigest()
                n = 5
                while "q." + h[:n] in used or "q." + h[:n] in final.values():
                    n += 2
                new = "q." + h[:n]
            final[mid] = new
            used.add(new)
        if any(final[m] != m for m in live):
            ren = {m: final[m] for m in live}
            self.entries = {ren.get(i, i): e for i, e in self.entries.items()}
            for i, e in self.entries.items():
                if e.get("mention"):
                    e["id"] = i
            for m in self.mentions or []:
                m["id"] = ren.get(m["id"], m["id"])
            self._mention_at = {a: ren.get(b, b) for a, b in self._mention_at.items()}
            self.id_alias = {a: ren.get(b, b) for a, b in self.id_alias.items()}
        self.id_alias = {a: b for a, b in self.id_alias.items() if a not in self.entries and a not in held}
        out = []
        for k, (st, c, _s, why) in sorted(picks.items()):
            e = (b1_entries or {})[k]
            where = str(e.get("where") or "")
            self.reanchor[k] = {"id": k, "status": st, "now_id": final.get(c) if c else None, "where": where, "why": why}
            if st == "unchanged":
                b = self.entries[final[c]]
                if norm(b.get("quote")) != norm(e.get("quote")) or norm(b.get("shown")) != norm(e.get("shown")):
                    self.review.append({"id": final[c], "b1_id": k, "words": b.get("shown") or "", "b1_words": e.get("shown") or "",
                                        "where": where, "before": norm(e.get("quote")), "after": norm(b.get("quote"))})
                continue
            place, now_ = self._place_text(where), None
            if st in ("changed", "ambiguous") and place:
                now_ = _debracket(norm(place))
                now_ = now_ if len(now_) <= 400 else now_[:399].rsplit(" ", 1)[0] + "…"
            d = {"id": k, "words": e.get("shown") or e.get("written") or "", "sentence": e.get("quote") or "", "where": where,
                 "label": e.get("source") or where, "status": st, "now": now_, "why": why}
            self.stale[k] = d
            out.append(d)
        return out

    def _stale_entry(self, mid):
        """BUILD IC-3 (D11; review b #3): a stale mention as lookup gives it — Block 1's words, no value, kind STALE; never put in
        self.entries (no figure of this run is tied to it)."""
        d = self.stale[mid]
        return {"id": mid, "kind": "STALE", "value": None, "shown": d["words"], "shown_as": d["words"], "label": d["label"],
                "source": d["label"], "quote": d["sentence"], "stale": True, "status": d["status"], "unit": "", "fmt": "num"}

    def cited_mentions(self, text):
        """BUILD IC-3 (D6, D11; review b #9c, #15): the mentions a text cites by id, in order, once each — the dot form in any letter
        case and the colon form; this run's or Block 1's stale ones. The one reading of "which mentions does this reply cite" (the
        update's <changed_mentions>, the deliverables call's list). A written-out quote is not one: it is read again where it says, on
        this run's data (and asking would register it — lookup's side effect — while a request is built)."""
        out = []
        for m in TOKEN_RE.finditer(text or ""):
            mid_ = _mention_id_of(m.group(1))
            if mid_ and (mid_ in self.entries or mid_ in self.stale) and mid_ not in out:
                out.append(mid_)
        return out

    def _stale_why(self, d):
        """BUILD IC-4 (D2): the reason, by status, in plain words."""
        st = d.get("status")
        what = (f"{d['label']} now reads: \u201c{d['now']}\u201d" if st == "changed" and d.get("now") else f"{d['label']} changed" if st == "changed"
                else f"{d['label']}: {d.get('why') or 'the figure cannot be told apart'}" + (f"; it now reads: \u201c{d['now']}\u201d" if d.get("now") else "")
                if st == "ambiguous"
                else f"{d['label']} did not load this run" if st == "source unavailable" else f"{d['label']} is no longer in the data")
        return "As of Block 1's data — " + what + ": counted as a number with no link"

    def _quote(self, rest):
        """[[q:<where>|<written>]] → (entry, note) or (None, why).
        BUILD IC-3 (D3) — a quote that holds: found, read, and not vouched for.
        FOUND: the written words in the text `where` names, set against the text as today (white space, letter case, the thousands comma
        and the dash's form set aside; number boundaries kept, _quote_rx) — any words the one reader reads there, digits or words: no
        spelling gate. Three refusals, each with its own words: the place does not exist; the text does not hold the words (or holds them
        only where they are no quantity — "8" in "Section 8"); more than one expression of the text fits and they differ ("name the
        record" across records, "quote more of it" in one text). THE WHOLE EXPRESSION: the written words are matched to the expression of
        the source that holds them as the reader reads that text ("third" in "Roughly every third caller" shows "Roughly every third";
        "35%" in "About 35% of exits" shows "About 35%"; "8" in "8-12" shows "8-12"; never "one" of "one hundred") — its hedge is the
        source's, printed by Python. READ: the entry stores the shape and, for a single exact value, the value; a range, a ratio, a bound,
        a multiplier, a recurrence and any hedged figure have no value (calc refuses them). ACROSS RECORDS: one entry when every record's
        expression is the same, naming how many hold it; record order never picks. A numeric field named by mistake resolves as that
        field's data id."""
        where, sep, written = rest.partition("|")
        # IC-2 review a F3: the sentence's own punctuation at either edge ("21," "21.") is no part of the number quoted
        where, written = where.strip(), strip_pua(re.sub(r"\s+", " ", written)).strip().strip(".,;:").strip()
        if not sep or not where or not written:
            return None, "a quote is q:<where>|<the number as the text writes it>"         # review a F1: a reason holds no brackets
        if len(written) > QUOTE_MAX:
            return None, f"a quote is at most {QUOTE_MAX} characters"
        qid = "q." + hashlib.sha1(f"{where}|{_norm_quote(written)}".encode("utf-8")).hexdigest()[:10]
        if qid in self.entries:
            return self.entries[qid], "a figure quoted from the organizers' text"
        texts, why = self._quote_texts(where)
        if texts is None:                                                   # refusal 1: the place does not exist
            parts = where.split(".")
            v = parse_number(written)
            if v is not None and len(parts) in (2, 3) and parts[0] in self._raw:   # a numeric field of this run's data named by mistake
                e = self.get(where)                                                 # (review a F12: never a proposal or a benchmark)
                if e and e.get("value") is not None and abs(e["value"] - v) <= max(1e-9, abs(v) * 1e-9):
                    return e, f"q:{where}|… read as {where} (a numeric field)"
            return None, why
        if self._ex is None:
            self._ex = Exempter.from_data(self._raw, self._records) if self._records is not None else Exempter()
        wv = {round(x["value"], 9) for x in quantities(written) if x.get("value") is not None}      # the written words' own values
        rx = _quote_rx(written)
        fits, part = [], None          # fits: (label, text, expression, the matched words) — the expression None where the words stand
        for lab, t in texts:           # in no quantity: a name, a marker, a position, an idiom, a vague or undecided word (shown as written)
            ms = list(rx.finditer(t))
            if not ms:
                continue
            xs = quantities(t, names=self._ex)
            for m in ms:
                x = next((x for x in xs if x["start"] <= m.start() and m.end() <= x["end"]), None)
                if x is None or x["role"] != "quantity":
                    fits.append((lab, t, None, m, x))
                    continue
                ev = {round(x[k], 9) for k in ("value", "lo", "hi", "num", "den") if x.get(k) is not None}
                whole = _norm_quote(t[m.start():m.end()]) == _norm_quote(x["words"])
                if not (whole or not wv or (wv & ev)):        # "one" is not "one hundred": the words must be a whole value of the expression
                    part = part or x["words"]
                    continue
                fits.append((lab, t, x, m, x))
        qkeys = {(_norm_quote(f[2]["words"]), f[2].get("shape"), _lc(f[2].get("hedge")), _lc(f[2].get("op"))) for f in fits if f[2] is not None}
        if qkeys:      # (ledger step 40) the data's names hide a title's digits from the count ("Emergency: 1,000 lbs Fresh Produce", an
            plain_ = {}                                                     # alert's title the announcement repeats): words that are no
            def _same_figure(f):                                            # quantity only because a data name holds them, and read without
                if f[2] is not None:                                        # the names are the same expression as a quantity that fits, are
                    return False                                            # that quantity — one figure, not a second expression. An age, a
                xp = next((y for y in plain_.setdefault(f[1], quantities(f[1]))   # marker or an idiom still fits as itself ("Ages 16-21"
                           if y["start"] <= f[3].start() and f[3].end() <= y["end"]), None)   # beside "capped at 21": refused)
                return (xp is not None and xp["role"] == "quantity"
                        and (_norm_quote(xp["words"]), xp.get("shape"), _lc(xp.get("hedge")), _lc(xp.get("op"))) in qkeys)
            fits = [f for f in fits if not _same_figure(f)]
        if not fits:                                                        # refusal 2: the text does not hold the words
            return None, (f"{where} holds “{written}” only as a part of a larger number (“{part}”)" if part else f"{where} does not hold “{written}”")
        key = lambda f: ((_norm_quote(f[2]["words"]), f[2].get("shape"), _lc(f[2].get("hedge")), _lc(f[2].get("op"))) if f[2] is not None   # noqa: E731
                         else (_norm_quote(f[1][f[3].start():f[3].end()]), "as written"))
        kinds = list(dict.fromkeys(key(f) for f in fits))
        if len(kinds) > 1:                                                  # refusal 3: more than one expression fits, and they differ
            across = len({f[0] for f in fits}) > 1
            seen = list({key(f): f for f in fits}.values())[:3]
            shown_ = "; ".join("“" + re.sub(r"\s+", " ", f[2]["words"] if f[2] is not None else (f[4]["words"] if f[4] is not None   # ledger 27:
                                                                           else f[1][f[3].start():f[3].end()])) + "”" for f in seen)   # its holder
            return None, (f"“{written}” fits {len(kinds)} different expressions in {where} ({shown_}) — "
                          + ("name the record" if across else "quote more of it"))
        lab, t, x, m, xr = fits[0]
        holders = len({f[0] for f in fits})
        mid = self._mention_at.get((lab, x["start"], x["end"])) if holders == 1 and x is not None else None
        if mid and mid in self.entries:     # BUILD IC-3 (D6): a quote written out and a picked id that name one expression are one entry
            return self.entries[mid], f"a figure quoted from the organizers' text — the mention [[{mid}]]"
        if x is None:          # no quantity: the source's own words, shown as it writes them, with no value (its role named)
            form = re.sub(r"\s+", " ", t[m.start():m.end()]).strip()
            x = {"start": m.start(), "end": m.end(), "words": form, "shape": None, "hedge": None, "op": None, "value": None,
                 "role": (xr or {}).get("role") or "no number", "rule": (xr or {}).get("rule")}
        form = re.sub(r"\s+", " ", x["words"]).strip()
        single = x.get("shape") == "single" and not x.get("hedge") and not x.get("op") and x.get("value") is not None
        fmt = "pct" if (x.get("pct") or x.get("_frac")) else ("money" if x.get("money") else "num")
        sentence = _debracket(_sentence_around(t, x["start"], x["end"]))    # the source's own brackets never read as a token in the span's
        #                                                                     title or the registry listing (review a F10: runs of three too)
        who = "the brief's" if where.lower() == "brief" else ("the announcement's" if where.lower() == "announcement" else "the record's")
        e = self.add(qid, x["value"] if single else None, "MEASURED", fmt=fmt,
                     label=_debracket(f"in {who} own words · {lab}" + (f" (and {holders - 1} more)" if holders > 1 else "")),
                     source=_debracket(lab), quote=sentence, novalue=None if single else form)
        if not e:
            return None, "the quote could not be registered"
        e.update(shown=form, shown_as=form, quoted=True, where=where, written=written, holders=holders, shape=x.get("shape") or "as written",
                 hedge=x.get("hedge"), op=x.get("op"), role=x.get("role"))
        return e, "a figure quoted from the organizers' text"


    def _calc(self, rest):
        """[[calc:<op>|<id a>|<id b>]] → (entry, note) or (None, why): the comparison Python computes from two registry figures (never
        a proposal), kind the weaker of theirs, under an id made from what it names. Refused when an input names nothing, a divisor is
        0, the comparison is false as named (pct_less when a is not below b; pct_more when it is not above), or a difference joins two
        figures of different class, window or cohort (the residual rule's own test)."""
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) != 3 or not all(parts):
            return None, "a comparison is calc:<op>|<id a>|<id b>"
        op, a, b = parts[0].lower(), parts[1], parts[2]
        if op not in CALC_OPS:
            return None, f"{parts[0][:20]!r} is not one of {', '.join(CALC_OPS)}"
        ents = []
        for x in (a, b):
            if x.lower().startswith(FORM_PREFIXES) or PROPOSAL_ID.fullmatch(x.lower()):
                return None, f"{x[:40]!r}: a comparison takes two registry figures, never a proposal or another form"
            e, why = self.lookup(x)
            if e is None:
                return None, f"{x[:60]!r}: {why}"
            if PROPOSAL_ID.fullmatch(e["id"]):          # review a F9: Model.16 is model.16 — a proposal, whatever its case (IC-4: either form)
                return None, f"{x[:40]!r}: a comparison takes two registry figures, never a proposal"
            if e.get("stale"):                                          # IC-3 (D11): Block 1's words, no value of this run
                return None, f"{x[:40]!r}: {why}"
            if e.get("value") is None:                                  # BUILD IC-3 (D3): no single value, no arithmetic
                return None, (f"{x[:40]!r} is {SHAPE_WORDS.get(e.get('shape')) or 'the source’s words with no value'} "
                              f"(“{e.get('shown')}”): it does not enter a comparison")
            ents.append(e)
        ea, eb = ents
        if ea["id"] == eb["id"]:
            return None, "a comparison of a figure with itself"
        da, db = _dim(ea), _dim(eb)
        if da[0] != db[0] or (da[1] and db[1] and da[1] != db[1]):
            return None, (f"{ea['id']} ({da[0]}{' of ' + da[1] if da[1] else ''}) and {eb['id']} ({db[0]}{' of ' + db[1] if db[1] else ''}) "
                          "are not the same kind of quantity")
        cid = f"calc.{op}." + hashlib.sha1(f"{ea['id']}|{eb['id']}".encode("utf-8")).hexdigest()[:8]
        if cid in self.entries:
            return self.entries[cid], "a comparison Python computed"
        va, vb = ea["value"], eb["value"]
        pcts = ("pct", "pct100")
        if op in ("diff", "pp") and _tags_differ(ea, eb, either=True):     # review b #9: one side's class, window or cohort is enough
            return None, (f"{ea['id']} and {eb['id']} are not the same class, window or cohort — a difference joins only figures of the "
                          "same people, place and period (pct_of, pct_less, pct_more and times compare across them)")
        if op == "diff" and ea["fmt"] in pcts and eb["fmt"] in pcts:
            return None, "two percentages differ in points: pp"
        if op == "pp" and not (ea["fmt"] in pcts and eb["fmt"] in pcts):
            return None, "pp takes two percentages"
        if op in ("pct_of", "pct_less", "pct_more", "times") and vb == 0:
            return None, f"{eb['id']} is 0: no comparison divides by it"
        if op == "pct_less" and not va < vb:
            return None, f"pct_less: {ea['shown']} is not below {eb['shown']}"
        if op == "pct_more" and not va > vb:
            return None, f"pct_more: {ea['shown']} is not above {eb['shown']}"
        shown_as = None
        if op == "diff":
            mon = ("money", "money_short")
            v = va - vb
            fmt = (ea["fmt"] if ea["fmt"] == eb["fmt"] else ("money" if ea["fmt"] in mon and eb["fmt"] in mon else "num"))   # review a F5
            formula = f"{ea['shown']} − {eb['shown']}"
        elif op == "pct_of":
            v, fmt, formula = round(va / vb, 3), "pct", f"{ea['shown']} ÷ {eb['shown']}"
        elif op == "pct_less":
            v, fmt, formula = round((vb - va) / vb, 3), "pct", f"({eb['shown']} − {ea['shown']}) ÷ {eb['shown']}"
        elif op == "pct_more":
            v, fmt, formula = round((va - vb) / vb, 3), "pct", f"({ea['shown']} − {eb['shown']}) ÷ {eb['shown']}"
        elif op == "times":
            q_ = va / vb
            s2 = sig2(q_) if math.isfinite(q_) else ""
            if not s2:                                                 # review a F8: an overflow or a value sig2 cannot show
                return None, f"times: {ea['shown']} ÷ {eb['shown']} cannot be shown in two significant figures"
            v, fmt, formula = q_, "num", f"{ea['shown']} ÷ {eb['shown']}"
            shown_as = s2                                              # 0.0018 shows as "0.0018", never "0"
        else:                                                          # pp
            pa, pb = (va * 100 if ea["fmt"] == "pct" else va), (vb * 100 if eb["fmt"] == "pct" else vb)
            v, fmt = round(pa - pb, 1), "num"
            shown_as, formula = f"{_trim(v)} points", f"{ea['shown']} − {eb['shown']}"
        kind = max((ea["kind"], eb["kind"], "DERIVED"), key=lambda k: KIND_RANK[k])    # review a F13: computed by Python — DERIVED at best
        ext = next((x for x in (ea, eb) if x["kind"] == "EXTERNAL"), None)
        ver = ({"verified": ext.get("verified_against_primary"), "url": ext.get("url"), "quote": ext.get("quote"), "checked": ext.get("checked")}
               if kind == "EXTERNAL" and ext and all(x.get("verified_against_primary") == "yes" for x in (ea, eb) if x["kind"] == "EXTERNAL") else None)
        e = self.add(cid, v, kind, fmt=fmt, label=f"{CALC_OPS[op]}: {ea.get('label') or ea['id']} against {eb.get('label') or eb['id']}",
                     source="computed by Python from the two figures the writer named", formula=f"{formula} = " + (shown_as or fmt_value(v, fmt)),
                     inputs=[ea["id"], eb["id"]], verified=ver)
        if not e:
            return None, "the comparison could not be registered"
        e["calc"] = {"op": op, "a": ea["id"], "b": eb["id"]}
        if shown_as:
            e.update(shown=shown_as, shown_as=shown_as)
        return e, "a comparison Python computed"

    def is_token(self, inner):
        """BUILD IC-2 (D1): a [[…]] is a token — a form (proposal, quote, comparison, declaration) or an id that names a figure. The
        one test every site uses that must tell a token from other double brackets (the diagram's own [[…]] shapes)."""
        t = (inner or "").strip()
        return bool(_FORM_GRAMMAR.fullmatch(t)) or self.lookup(t)[0] is not None    # review a F7: a form needs its own "|" parts

    def resolves(self, inner):
        """BUILD IC-2 (D1): the token's text resolves on a page — a figure, a quote or comparison that holds, or a declaration that is
        no quantity. A site that needs a figure (a baseline, a card's stat) uses lookup instead: a declaration names none."""
        t = (inner or "").strip()
        if t.lower().startswith("nf:"):
            body, sep, kind = t[3:].rpartition("|")
            return bool(sep) and nf_refusal(body, kind) is None
        return self.lookup(t)[0] is not None

    def shown(self, id):
        e = self.get(id)
        return e["shown"] if e else ""

    # ── BUILD IC-8 (D12): a reader's words for the run's data ──
    def names(self):
        """The run's data in a reader's words (ReaderNames of this registry's data, and at Block 2 of Block 1's: set_before), kept until
        the data changes."""
        b = getattr(self, "_before_raw", None)
        key = (id(self._raw), id(self._records), id(b))
        if getattr(self, "_names_key", None) != key:
            self._names, self._names_key = ReaderNames(self._raw, self._records, before=b), key
        return self._names

    def set_before(self, raw):
        """BUILD IC-8 (d12c): Block 1's data at Block 2 — a record or an endpoint the update removed keeps its words (names())."""
        self._before_raw = raw if isinstance(raw, dict) else None

    def _place_rx(self):
        n = self.names()
        if getattr(self, "_place_key", None) != self._names_key:
            eps = "|".join(re.escape(x) for x in sorted(n.endpoints, key=len, reverse=True))
            ids = "|".join(re.escape(x) for x in sorted(n.ids, key=len, reverse=True))
            self._place = re.compile(rf"(?<![\w-])({eps})[ .]({ids})[ .]([A-Za-z_][\w]*)(?![\w-])") if eps and ids else None
            self._place_key = self._names_key
        return self._place

    def reader_label(self, e):
        """BUILD IC-8 (D12): an entry's label as a reader reads it — the fallback "<field> of <record id> (<endpoint>)" as "<field in
        words> — <record's name>"; a one-record endpoint's "<field> (<endpoint>)" as "<field in words> — <endpoint in words>"; a record's
        place "<endpoint> <record id> <field>" (or with dots) as "<record's name> (<field in words>)"; every other record id, endpoint
        and coded field in words (ReaderNames.words). The raw label stays the entry's: its lineage (Registry.lineage)."""
        lab = (e or {}).get("label") or (e or {}).get("id") or ""
        n = self.names()
        m = re.fullmatch(r"(\w+) of ([\w-]+) \(([\w-]+)\)", lab)
        if m and m[2] in n.ids:
            return f"{field_words(m[1])} — {n.ids[m[2]]}"
        m = re.fullmatch(r"(\w+) \(([\w-]+)\)", lab)
        if m and m[2] in n.endpoints:
            return f"{field_words(m[1])} — {n.endpoints[m[2]]}"
        rx = self._place_rx()
        if rx is not None:
            lab = rx.sub(lambda x: f"{n.ids.get(x[2], x[2])} ({field_words(x[3])})", lab)
        return n.words(lab)

    def lineage(self, e):
        """BUILD IC-8 (D12): a figure's lineage as recorded — its raw label, then its formula or source: what the figure registry (a
        provenance zone) shows after the reader's label; never in a narrative text or a title."""
        bits = [(e or {}).get("label") or (e or {}).get("id") or ""]
        if (e or {}).get("formula"):
            bits.append(e["formula"])
        elif (e or {}).get("source") and e.get("source") != bits[0]:
            bits.append(e["source"])
        return " · ".join(b for b in bits if b)

    # ── tokens ──
    def title(self, e):
        if e.get("quoted"):         # BUILD IC-2 (D2): a quote's title holds where Python found it and the sentence it stands in
            return " · ".join(b for b in ("QUOTED — the organizers' own words, found by Python where the writer said", self.reader_label(e),
                                          f"“{e['quote']}”" if e.get("quote") else "") if b)   # IC-2 Phase 5: no [:300] — it cut the sentence
            #                                                         a quote is shown with ("…roughly a third of "); _sentence_around bounds it
        bits = [f"{e['kind']} — {'counted by Python on this page' if e.get('counted') else KIND_WORDS[e['kind']]}", self.reader_label(e)]   # IC-4 (D13)
        # BUILD IC-8 (D12): no formula or source in a title — the lineage names records, endpoints and fields: the figure registry shows it
        if e["kind"] == "EXTERNAL":
            bits.append("verified against its primary source" if e.get("verified_against_primary") == "yes" else "not verified against its primary source")
        return out_text(" · ".join(b for b in bits if b), "plain")[:300]      # BUILD IC-5 (D2 c): the entry's words as words

    def span(self, id_or_entry, text=None, caveat=True):
        """<span class="prov …" data-prov="id" data-kind="K" title="…">shown</span> (+ the unverified marker for EXTERNAL; + BUILD IC-4
        (D7) "sources disagree" beside a figure whose sources disagree, unless caveat is False: a sentence that states it itself)."""
        e = id_or_entry if isinstance(id_or_entry, dict) else self.get(id_or_entry)
        if not e:
            return _html.escape(text or "")
        cls = "prov" + {"MODELED": " prov-m", "EXTERNAL": " prov-x"}.get(e["kind"], "")
        out = (f'<span class="{cls}" data-prov="{_html.escape(e["id"], quote=True)}" data-kind="{e["kind"]}" '
               f'title="{_html.escape(self.title(e), quote=True)}">{_html.escape(text if text is not None else e["shown"])}</span>')
        if e["kind"] == "EXTERNAL" and e.get("verified_against_primary") != "yes":
            out += '<sup class="unv" data-num-exempt="marker" title="not verified against its primary source">&#8224;</sup>'
        if isinstance(e.get("partial"), str) and e["partial"]:     # BUILD IC-4 (D6): counted on part of the material outcomes — every print
            out += (f' <sup class="pt" data-num-exempt="marker" data-partial title="{_html.escape(e["partial"], quote=True)}">partial</sup>')
        others = self.disagreeing(e["id"]) if caveat else []
        if others:     # BUILD IC-4 (D7): the caveat beside the claim it qualifies, both values in its title; BUILD IC-6 (D9): its figures
            #            in data-sd — the figure (the sidecar figure a mention states) and each it disagrees with — read by place_notes,
            #            which moves it after its sentence as "[note N]" (a page never prints this sup)
            own = (sorted(self.same_figure(e["id"])) or [e["id"]])[0]
            sd = json.dumps({"f": own, "o": sorted(o for o, _x in others if o != own)}, ensure_ascii=False, separators=(",", ":"))
            out += (f' <sup class="sd" data-num-exempt="marker" data-sd="{_html.escape(sd, quote=True)}" '
                    f'title="{_html.escape(self._disagree_title(e), quote=True)}">sources disagree</sup>')
        return out

    def resolve(self, text, mode="html"):
        """Every token in `text` → its figure: html (a span; the bare form inside a tag's attribute) or plain. A token naming nothing,
        or a stray '[[' / ']]', → SENTINEL (remove_marked takes its sentence out). Returns (text, events)."""
        if not isinstance(text, str):
            return text, []
        if "[[" not in text and "]]" not in text:
            return text, self._near_misses(text)          # BUILD IC-3 (D4): a text with no token may still hold a near-miss
        events, tags = [], [(m.start(), m.end()) for m in re.finditer(r"<[^<>]*>", text)] if mode == "html" else []

        def in_tag(i):
            return any(a < i < b for a, b in tags)

        def refused(shown, rule, why, inside):
            """BUILD IC-2 (D6): a quote or a declaration that does not hold — the writer's text as typed (its number counts as having no
            link), in a neutral span the sanity rules flag; plain text, or inside a tag, the text alone."""
            if mode != "html":
                return shown
            if inside:
                return _html.escape(shown, quote=True)
            return (f'<span class="nolink" data-refused="{rule}" title="{_html.escape(self.names().words(_debracket(why)), quote=True)}">'
                    f'{_html.escape(shown)}</span>')

        def sub(m):
            tok, t = m.group(0)[:80], m.group(1).strip()
            low, inside = t.lower(), in_tag(m.start())
            spaced = [{"token": tok, "rule": "note", "why": f"a token with spaces inside its brackets — read as [[{t[:60]}]]"}] if t != m.group(1) else []
            if re.match(r"(?:q|calc|nf)\s*:", low) and not _FORM_GRAMMAR.fullmatch(t) and not _MENTION_COLON.fullmatch(low):   # IC-3 (D6)
                # IC-2 review a F1: a quote or a declaration with no "|", a comparison without its three parts — a malformed token,
                # removed with its sentence in every mode (never its where-text as prose). IC-3 (D4): the parts it has, and needs
                need = 3 if low.startswith("calc") else 2
                return SENTINEL, [{"token": tok, "rule": "malformed", "why": f"a form with the wrong number of parts ({t.count('|') + 1} of {need}: "
                                                                            "q:<where>|<written>, calc:<op>|<id a>|<id b>, nf:<the text>|<kind>)"}]
            if low.startswith("nf:"):           # BUILD IC-2 (D4): a declared non-figure — no registry entry; its text, read as exempt
                body, sep, kind = t[3:].rpartition("|")
                body = strip_pua(body.strip())                      # no author mark or hold from the writer's text
                why = nf_refusal(body, kind)
                if why is None:
                    k = kind.strip().lower()
                    ev = [{"token": tok, "rule": "declared", "text": body, "kind": k}]
                    if mode == "html" and not inside:
                        r = (f'<span class="nf" data-num-exempt="{_html.escape(DECLARED_WHY + NF_KINDS[k], quote=True)}" '
                             f'title="{_html.escape("Not a figure — declared by the writer: " + NF_KINDS[k], quote=True)}">{_html.escape(body)}</span>')
                    else:
                        r = _html.escape(body, quote=True) if mode == "html" else body
                    return r, ev
                return (refused(body, "declared", f"Declared not a figure, refused ({why}): counted as a number with no link", inside),
                        [{"token": tok, "rule": "declared_refused", "why": why, "text": body}])
            mid_ = _mention_id_of(t)                      # IC-3 (review b #9c): either form, any letter case
            st_ = self.stale.get(mid_) if mid_ else None
            if st_ and mid_ not in self.entries:          # BUILD IC-3 (D11): Block 1's mention that this run's data no longer holds
                why_ = self._stale_why(st_)
                return (refused(st_["words"], "stale", why_, inside),
                        [{"token": tok, "rule": "stale_mention", "id": st_["id"], "why": why_, "text": st_["words"], "status": st_["status"]}])
            e, note = self.lookup(t)
            if e is None:
                if low.startswith("q:") and not _MENTION_COLON.fullmatch(low):   # IC-3 (review a #6): a colon id is an id, never a quote
                    # BUILD IC-2 (D6): the number stays as typed, with no link; its sentence is flagged
                    written = strip_pua(re.sub(r"\s+", " ", t[2:].partition("|")[2]).strip() or t[2:].strip())
                    return (refused(written, "quote", f"Quoted, but not found where the writer said ({note}): counted as a number with no link",
                                    inside), [{"token": tok, "rule": "quote_refused", "why": note, "text": written}])
                return SENTINEL, [{"token": tok, "rule": "calc" if low.startswith("calc:") else "unknown", "why": note}]
            ev = list(spaced)
            if note and (not low.startswith(("q:", "calc:")) or _MENTION_COLON.fullmatch(low)):   # IC-3 (D6): [[q:xxxxx]] read and noted
                ev.append({"token": tok, "rule": "note", "why": note, "id": e["id"]})
            ev.append({"token": tok, "rule": "cited", "id": e["id"]})
            if mode == "html" and not inside:
                return self.span(e), ev
            return (_html.escape(e["shown"], quote=True) if mode == "html" else
                    e["shown"] + (" (partial)" if isinstance(e.get("partial"), str) and e["partial"] else "")), ev   # BUILD IC-4 (D6)
        parts, last, pos, nms = [], 0, 0, []
        for m in TOKEN_RE.finditer(text):       # BUILD IC-2 (D6): each event knows where its replacement sits in the result
            if m.start() < last:                 # (a token whose start the previous quote's tail took in: never, as tails hold no "[[")
                continue
            seg = text[last:m.start()]
            rep, evs = sub(m)
            # BUILD IC-3 (D3, ledger step 36): a quote's whole expression shows once — the writer's words beside the token that repeat
            # its lead or its tail are left out (a note each), a lead mid-sentence starts lowercase; the span keeps the whole expression
            cited = next((self.entries.get(x_["id"]) for x_ in evs if x_.get("rule") == "cited"), None)
            skip = 0
            if cited is not None and cited.get("quoted") and not in_tag(m.start()):
                lead, skip, lower = dup_edges(seg, cited.get("shown") or "", text[m.end():])
                left_out = ([seg[lead:].strip()] if lead is not None else []) + ([text[m.end():m.end() + skip].strip()] if skip else [])
                if lead is not None:
                    seg = seg[:lead]                    # the repeated words go; what stood before them (a space, "(", a tag) stays
                if lower:
                    shown_ = cited["shown"][:1].lower() + cited["shown"][1:]
                    rep = self.span(cited, shown_) if mode == "html" else shown_
                if left_out:
                    evs.append({"token": m.group(0)[:80], "rule": "note", "id": cited["id"],
                                "why": "the writer's own " + " and ".join(f"“{w}”" for w in left_out) + " beside the quote left out — "
                                       "the quote prints the source's words"})
            # BUILD IC-3 (D7): a rate prints whole — its value with its per part ("14 per 10,000 residents") — unless the writer's own
            # words right after the token state that per part (D1's rate rule: then they complete it, and the value prints alone)
            rated = next((self.get(x_["id"]) for x_ in evs if x_.get("rule") == "cited"), None) if cited is None or not cited.get("quoted") else None
            wr = whole_rate(rated) if rated is not None and not in_tag(m.start()) else None
            if wr and not per_follows(rated, text[m.end():m.end() + 120]):
                rep = self.span(rated, wr) if mode == "html" else wr
            # IC-3 (ledger step 27): a near-miss is looked for in the writer's own words only — the result's spans carry Python's titles
            # (a quoted source sentence, debracketed: "[ [budget] ]"), which are no token of the writer's
            nms += [dict(x, at=pos + x["at"]) for x in self._near_misses(seg)]
            parts.append(seg)
            pos += len(seg)
            for e_ in evs:
                e_["at"] = pos
            events += evs
            parts.append(rep)
            pos += len(rep)
            last = m.end() + skip
        nms += [dict(x, at=pos + x["at"]) for x in self._near_misses(text[last:])]
        parts.append(text[last:])
        out = "".join(parts)
        events += nms
        if "[[" in out or "]]" in out:
            strays = [m_.start() for m_ in re.finditer(r"\[\[|\]\]", out)]
            events.append({"token": out[strays[0]:][:40], "rule": "malformed", "why": "a token with no closing or opening brackets",
                           "at": strays[0]})                 # IC-2 (D6): where it sits, as every other event
            for e_ in events:                                # review a F1: each pair becomes one SENTINEL — every later event moves back
                if isinstance(e_.get("at"), int):
                    e_["at"] -= sum(1 for p_ in strays if p_ < e_["at"])
            out = re.sub(r"\[\[|\]\]", SENTINEL, out)
        return out, events

    def _near_misses(self, text):
        """BUILD IC-3 (D4): a near-miss of a token — a form or a known id in one bracket, anything in double curly braces — is not read:
        its words stay as typed; each is an event where it sits (field_findings: rule "near_miss", flagged and kept)."""
        out = []
        for nm in _NEAR_MISS.finditer(text):
            inner = (nm.group(1) or nm.group(2) or "").strip()
            if nm.group(1) is not None and not (re.match(r"(?i)(?:q|calc|nf|model)\s*:", inner) or self.get(inner) is not None):
                continue
            out.append({"token": nm.group(0)[:80], "rule": "near_miss", "at": nm.start(),
                        "why": f"{nm.group(0)[:60]}: a token's near-miss form (one bracket or curly braces) — not read; its words stay as typed"})
        return out

    def plain(self, text):
        """Plain text with every token resolved; a sentence with a token naming nothing is left out."""
        t, _ev = self.resolve(text, mode="plain")
        return remove_marked(t, [], plain=True)[0] if isinstance(t, str) and SENTINEL in t else t

    # ── the file ──
    def to_doc(self, integrity=None):
        facts = fill_units({k: self.fact(e) for k, e in self.entries.items()})      # BUILD IC-4 (D3): every entry's fact, its unit in one form
        return {"schema": SCHEMA, "challenge": self.challenge, "run_id": self.run_id, "block": self.block,
                "written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "entries": [dict(self.entries[k], fact=facts[k]) for k in sorted(self.entries)],
                "problems": list(self.problems), "notes": list(self.notes),
                "checks": list(self.checks), "integrity": integrity,
                # BUILD IC-4 (D3, D2): how this registry reads an id it does not hold — every reader resolves as the Oracle does
                "id_alias": dict(self.id_alias), "stale": dict(self.stale), "unavailable": sorted(self.unavailable),
                "sealed_counts": sorted(getattr(self, "sealed_counts", ()) or ())}      # BUILD IC-8 (D2): read back sealed

    def write(self, path, integrity=None):
        """None, or the reason the file was not written (UTF-8, temp file + os.replace). Never raises."""
        p = Path(path)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes((json.dumps(self.to_doc(integrity), indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
            os.replace(tmp, p)
            return None
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"


def read(path, challenge=None, run_id=None):
    """(Registry, None) or (None, why it is not used) — the Oracle's checkpoints/registry.json of this challenge and run. Never raises."""
    try:
        d = admit(json.loads(Path(path).read_bytes().decode("utf-8-sig")), "saved", Path(path).name)   # BUILD IC-5 (D1); BUILD IC (D1): a registry another run wrote is an input
    except FileNotFoundError:
        return None, "no registry file"
    except Exception as e:  # noqa: BLE001
        return None, f"not readable ({type(e).__name__})"
    if not isinstance(d, dict) or d.get("schema") != SCHEMA or not isinstance(d.get("entries"), list):
        return None, "not a schema-1 registry"
    if challenge is not None and d.get("challenge") != challenge:
        return None, f"of {d.get('challenge')!r}, not {challenge!r}"
    if run_id is not None and d.get("run_id") != run_id:
        return None, f"written by run {d.get('run_id')}, this run is {run_id}"
    r = Registry(d.get("challenge"), d.get("run_id"), d.get("block"))
    for e in d["entries"]:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and e.get("kind") in KINDS and (isinstance(e.get("value"), (int, float))
                                                                                         or (e.get("value") is None and e.get("shape") and isinstance(e.get("shown_as"), str))):
            r.entries[e["id"]] = dict(e, shown=e["shown_as"] if isinstance(e.get("shown_as"), str) and e["shown_as"]   # IC-2: a quote's
                                      else fmt_value(e["value"], e.get("fmt", "num")))                                  # own form, "N points"
            #                                         BUILD IC-5 (D1, D2): its words as admit gave them (kind saved), printed as words
    r.integrity = d.get("integrity")
    r.checks = [c for c in d.get("checks") or [] if isinstance(c, dict)]
    # BUILD IC-4 (D3, D2): the ids the Oracle reads as another, and Block 1's mentions this run no longer holds — as it resolves them
    r.id_alias = {k: v for k, v in (d.get("id_alias") or {}).items() if isinstance(k, str) and isinstance(v, str)}
    r.unavailable = {x for x in d.get("unavailable") or [] if isinstance(x, str)}
    r.sealed_counts = {x for x in d.get("sealed_counts") or [] if isinstance(x, str)}      # BUILD IC-8 (D2)
    r.stale = {k: v for k, v in (d.get("stale") or {}).items()
               if isinstance(k, str) and isinstance(v, dict) and all(isinstance(v.get(x), str) for x in ("words", "label", "sentence", "status"))}
    r._model_ids = {(e["written"], proposal_label_key(e["label"][:120])): k for k, e in r.entries.items()   # B3 (Phase 4, review a F5): the proposals resolve
                    if k.startswith("model.") and isinstance(e.get("written"), str) and isinstance(e.get("label"), str)}
    return r, None


# ═══ BUILD IC-2: the writer's forms — the helpers ═══
_DASHES = "-–—−"
# a form in its own grammar (review a F7): a proposal, a quote and a declaration have one "|", a comparison two
# BUILD IC-3 (D4): a near-miss form — "[q:…]", "[es_free_beds]" (one bracket around a form or a known id), "{{q:…}}"
_NEAR_MISS = re.compile(r"(?<!\[)\[\s*([^\[\]<>]{1,160}?)\s*\](?!\])|\{\{\s*([^{}<>]{1,160}?)\s*\}\}")
_FORM_GRAMMAR = re.compile(r"(?is)\s*(?:(?:model|q|nf)\s*:[^|]*\|.*|calc\s*:[^|]*\|[^|]*\|[^|]*)")


def _edits(a, b, cap):
    """BUILD IC-3 (D4): Levenshtein distance between a and b, stopping once it passes `cap` (cap + 1 then)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        lo = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            lo = min(lo, cur[j])
        if lo > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _debracket(s):
    """Text Python writes beside a page's tokens (a quote's sentence, its label, a refusal's reason) never holds [[ or ]]: each bracket
    of a run is parted from the next (review a F10: "]]]" too)."""
    return re.sub(r"\](?=\])", "] ", re.sub(r"\[(?=\[)", "[ ", s or ""))


def debracket_strings(v):
    """BUILD IC-3 (D4): the organizers' own texts (the run's data, the brief, the announcement) with every string debracketed where they
    enter the run — a source sentence never holds a token's brackets in a request, a title or a quote's text. Keys and every other value
    unchanged; a text with no [[ or ]] comes back as it was."""
    if isinstance(v, str):
        return _debracket(v) if ("[[" in v or "]]" in v) else v
    if isinstance(v, list):
        return [debracket_strings(x) for x in v]
    if isinstance(v, dict):
        return {k: debracket_strings(x) for k, x in v.items()}
    return v


def _tags_differ(ea, eb, either=False):
    """Two figures of different class, window or cohort — the residual rule's test (both tagged: B3) and a comparison's difference's
    (`either`: one side tagged is enough, the other's tags unknown; review b #9)."""
    ka, kb = _tagkey(ea), _tagkey(eb)
    if ka == kb:
        return False
    return (any(ka) or any(kb)) if either else (any(ka) and any(kb))


def _dim(e):
    """A figure's kind of quantity for a comparison (D3): (money, a percentage, a ratio, social value per dollar or a count; its unit or
    None). Two figures whose units are both stated and differ are not compared: money by what follows "USD" ("USD per person" is not
    "USD"; review a F5), a count by its unit noun ("people", "days"); an endpoint's record count by its endpoint's records. A unit not
    stated compares with any of the same kind."""
    f, u = e.get("fmt") or "num", (e.get("unit") or "").strip().lower()
    if f.startswith("money") or u.startswith("usd"):
        tail = u[3:].strip() if u.startswith("usd") else u
        return "money", (tail or None)
    if f in ("pct", "pct100"):
        return "a percentage", None
    if f == "ratio":
        return "a ratio", None
    if f == "per_dollar":
        return "social value per dollar", None
    m = re.fullmatch(r"([\w-]+)\.count", e.get("id") or "")
    if m and not u:                       # review a F5: 8 programs and 10 shelters are not one kind of thing
        return "a count", f"{m.group(1)} records"
    return "a count", (UNIT_NOUNS.get(u, u) or None)


def _norm_quote(w):
    """A quote's text as its id sees it: white space collapsed, letter case and the thousands comma set aside, one dash."""
    t = re.sub(r"\s+", " ", w or "").strip().lower()
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)
    return re.sub("[" + _DASHES + "]", "-", t)


def _quote_rx(w):
    """The regular expression that finds a quote's text in its source: digits as written, a thousands comma optional only where one can
    stand ("47000" and "47,000" are one; "21" is not "2,1"), any run of white space, any dash, letter case set aside. A quote that begins
    or ends with a digit stands at number boundaries ("21" is in "16-21" and "capped at 21.", not in "2021", "210", "1,210", "21.5",
    "21,000", "24/21", "21%" or "$21"; review a F3) — nor before a magnitude letter ("500" and "$500" are not in "$500K"; IC-2 Phase 5)."""
    s = re.sub(r"\s+", " ", w).strip()
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)            # the writer's own thousands commas: optional below, wherever they can stand
    out, j = [], 0
    while j < len(s):
        c = s[j]
        if c.isdigit():
            k = j
            while k < len(s) and s[k].isdigit():
                k += 1
            run = s[j:k]
            for i, d in enumerate(run):
                left = len(run) - i - 1
                out.append(re.escape(d) + (",?" if left and left % 3 == 0 else ""))
            j = k
            continue
        if c == " ":
            out.append(r"\s+")
        elif c in _DASHES:
            out.append(r"\s*[" + _DASHES + r"]\s*")
        else:
            out.append(re.escape(c))
        j += 1
    lead = r"(?<![\d$])(?<!\d[.,/:])" if s[:1].isdigit() else (r"\b" if s[:1].isalpha() else "")    # "age 21" is not in "page 21"
    tail = (r"(?!\d)(?![.,/:]\d)(?!\s?(?:%|percent\b))(?![KMB]\b)" if s[-1:].isdigit() else (r"\b" if s[-1:].isalpha() else ""))
    return re.compile(lead + "".join(out) + tail, re.I)


def _sentence_around(t, a, b, n=200):
    """The sentence of `t` that holds [a, b): from the last stop or line break before it to the first after it, cut to about n
    characters at word boundaries."""
    lo = max([t.rfind(c, 0, a) for c in ("\n", ". ", "! ", "? ", "; ")] + [-1])
    lo = lo + 1 if lo >= 0 else 0
    his = [x for x in (t.find(c, b) for c in ("\n", ". ", "! ", "? ", "; ")) if x >= 0]
    hi = (min(his) + 1) if his else len(t)
    s = re.sub(r"\s+", " ", t[lo:hi]).strip()
    if len(s) > n:
        mid = max(0, (a - lo) - n // 2)
        cut = s[mid:mid + n]
        s = ("…" if mid else "") + cut[cut.find(" ") + 1 if mid else 0:].rsplit(" ", 1)[0] + "…"
    return s


# BUILD IC-3 (D3, ledger step 36): a quote's whole expression shows once. The writer quoting under IC-2's rule typed the hedge, a bound's
# words and the per part itself, beside the token ("close to [[q:…|4 in 10]]", "up to [[q:…|$5,000]] a year"); Python now prints the
# source's whole expression, so the page read "close to Close to 4 in 10". The span keeps the whole expression (done-test 5, row 4's
# detector); the writer's words that repeat its edges are left out, and a lead that stands mid-sentence starts lowercase.
_EDGE_WORDS = 4
_EDGE_FUNCTION = {"to", "at", "of", "in", "on", "by", "for", "per", "than", "and", "or", "a", "an", "the", "with", "from"}   # never a repeat alone


def _number_span(shown):
    """Where the number of a quote's whole expression stands in it, as the one reader reads the expression: (start, end, the reader's
    item) from its first number part to its last ("Close to [4 in 10]", "up to [$5,000]/year", "[14] per 10,000 residents"); a
    recurrence's number is its last word ("Roughly every [third]"). None when the reader finds no quantity there."""
    x = next((y for y in quantities(shown or "") if y["role"] == "quantity"), None)
    if x is None:
        return None
    if x.get("parts"):
        return x["parts"][0][0], x["parts"][-1][1], x
    k = (shown or "").rstrip().rfind(" ") + 1
    return (k, len((shown or "").rstrip()), x) if x.get("shape") == "recurrence" else (x["start"], x["end"], x)


def _words_rx(ws):
    return r"\s+".join(map(re.escape, ws))


def dup_edges(before, shown, after):
    """BUILD IC-3 (D3, ledger step 36): where the writer's own words beside a quote's token repeat the quote's whole expression —
    (lead, tail, lower): `lead` the index in `before` where its last words repeat words of the expression's lead (the words before its
    number: a hedge, a bound's words, "every"; the lead's first or last words, the longest run first), None when they do not; `tail`
    how many characters at the start of `after` repeat words of the expression's tail (the words after its number: its first or last
    words), or its per part said another way ("/year" for "a year", "per year", "each year"); `lower` whether the expression's first
    letter is lowercased — its first word is a lead word, and the token stands mid-sentence (the writer's text before it, tags and
    marks set aside, ends neither a sentence nor a clause with a colon). Plain words only: a repeat across a tag or a mark stays."""
    sp = _number_span(shown)
    if sp is None or not isinstance(before, str) or not isinstance(after, str):
        return None, 0, False
    lw, tw = shown[:sp[0]].split(), shown[sp[1]:].split()
    lead_word = (sp[2].get("hedge") or sp[2].get("op") or "").strip()     # the reader's own hedge or bound words: the only ones lowercased
    # IC-3 (review a #1): the printed text is the writer's words, then the expression — a repeat is the writer's LAST words equal to
    # the expression's FIRST words, and only a run that holds the reader's whole hedge or bound (or the whole lead when it has none):
    # "Roughly [[…]]" before "Roughly every third"; never a lone function word ("to" before "Close to 4 in 10", "at" before "at least
    # 20", "than" before "more than $900"). After the token, the expression then the writer's words — a repeat is the writer's FIRST
    # words equal to the expression's LAST words ("residents" after "…per 10,000 residents"), or its whole per part with its number;
    # never a lone "per" ("per page" after "$15 per copy"). The first version took any run from either end of either edge.
    hw = lead_word.split()
    m0 = len(hw) if hw and [w.lower() for w in lw[:len(hw)]] == [w.lower() for w in hw] else len(lw)
    lead = None
    for k in range(min(len(lw), _EDGE_WORDS), max(m0, 1) - 1, -1):
        m = re.search(r"(?i)(?<![\w$])" + _words_rx(lw[:k]) + r"\s+$", before)
        if m:
            lead = m.start()
            break
    tail = 0
    runs = [tw[-k:] for k in range(min(len(tw), _EDGE_WORDS), 0, -1)]
    runs += [tw[:k] for k in range(min(len(tw), _EDGE_WORDS), 1, -1) if any(re.search(r"\d", w) for w in tw[:k])]
    for run in runs:
        if len(run) == 1 and run[0].lower() in _EDGE_FUNCTION:
            continue
        m = re.match(r"(?i)\s*" + _words_rx(run) + r"(?![\w])", after)
        if m:
            tail = m.end()
            break
    if not tail:                                            # a per part said another way: "/year" beside the writer's "a year"
        pm = re.search(r"(?:/|\bper\s+)([a-z]+)$", shown[sp[1]:].strip(), re.I)
        base = _Q_TIME_BASE.get(pm.group(1).lower()) if pm else None
        if base:
            alts = "|".join(sorted((w for w, b_ in _Q_TIME_BASE.items() if b_ == base), key=len, reverse=True))
            m = re.match(r"(?i)\s*(?:/\s*|(?:per|a|an|each|every)\s+)(?:" + alts + r")(?![\w])", after)
            tail = m.end() if m else 0
    rest = re.sub(r"<[^<>]*>", "", strip_pua(before[:lead] if lead is not None else before)).rstrip()
    # (ledger step 40) only a lead the reader calls a hedge or a bound ("About", "Close to", "More than") — never a name ("Maya, 19")
    lower = (bool(lead_word) and shown.lower().startswith(lead_word.lower()) and lead_word[:1].isupper() and lead_word[1:2].islower()
             and bool(rest) and not re.search(r"[.!?:]$", rest))
    return lead, tail, lower


# a phone: the checker's own phone pattern, or a three-digit service line (N11, 988) — never any three digits (review a F4, b #11)
_NF_SERVICE = re.compile(r"[2-9]\s*-?\s*1\s*-?\s*1|9\s*-?\s*8\s*-?\s*8")
_NF_QUANT = re.compile(r"(?i)\b(?:over|about|around|nearly|almost|roughly|approximately|some|more\s+than|less\s+than|fewer\s+than|up\s+to|"
                       r"at\s+least|at\s+most|under|above|below)\s+\d|\d+\s*(?:of|in|out\s+of|per|for\s+every)\s+\d")


def _nf_name_ok(t):
    """A name or a code as the checker's own name rule reads one (_name_forms): each number joined to letters ("B12", "I-35", "501(c)(3)")
    or beside a capitalized word ("Route 66", "HB 2021", "7 Shelter") — never a number before a lowercase word ("12 shelters", "6-week",
    "60 displaced residents"), and never a quantifier or a share ("over 700", "1 in 5")."""
    if _NF_QUANT.search(t) or re.search(r"\d\s*[" + _DASHES + r"]?\s*[a-z]{2,}", t):
        return False
    w = t.split()
    for i, x in enumerate(w):
        if not re.search(r"\d", x):
            continue
        joined = bool(re.search(r"[A-Za-z]", x))                    # "B12", "I-35", "501(c)(3)", "2b"
        beside = (i > 0 and w[i - 1][:1].isupper()) or (i + 1 < len(w) and w[i + 1][:1].isupper())
        if not (joined or beside):
            return False
    return True


def nf_refusal(text, kind):
    """None when [[nf:<text>|<kind>]] is a declared non-figure Python accepts, else why it reads as a quantity (BUILD IC-2, D4; review a
    F4 and b #11): a kind on the list; digits; at most 40 characters; no $ or %, decimal, thousands comma, or unit noun anywhere after a
    number; an age a whole number to 120 (or a range of two); a year 1800–2100; a date or a time read by the checker's own patterns; a
    phone the checker's phone pattern or a three-digit service line (211, 311 … 911, 988); a name or a code by the checker's name rule."""
    t, k = re.sub(r"\s+", " ", text or "").strip(), (kind or "").strip().lower()
    if k not in NF_KINDS:
        return f"{(kind or '')[:20]!r} is not a kind on the list ({', '.join(NF_KINDS)})"
    if not re.search(r"\d", t):
        return "it holds no digit — write it plainly"
    if len(t) > QUOTE_MAX:
        return f"longer than {QUOTE_MAX} characters"
    if "$" in t or "%" in t:
        return "a money or percent sign"
    if re.search(r"\d,\d{3}(?!\d)", t):
        return "a thousands comma"
    if re.search(r"\d\.\d", t) and k not in ("date", "time", "phone"):
        return "a decimal"
    nouns = "|".join(sorted(UNIT_NOUNS, key=len, reverse=True)) + r"|beds|calls|units|dollars|percent|sites|shelters|visits|days|weeks|months|years"
    if re.search(r"\d[^\d]*\b(?:" + nouns + r")\b", t, re.I) and k not in ("date", "time"):     # anywhere after a number (review a F4)
        return "a unit noun after a number"
    if k == "age":
        nums = [int(x) for x in re.findall(r"\d+", t)]
        ok = re.fullmatch(r"\d{1,3}(?:\s*[" + _DASHES + r"]\s*\d{1,3})?", t) and all(0 < x <= 120 for x in nums)
        return None if ok else "an age is a whole number to 120, or a range of two"
    if k == "year":
        ys = [int(x) for x in re.findall(r"\d{4}", t)]
        ok = re.fullmatch(r"\d{4}(?:\s*[" + _DASHES + r"]\s*\d{2,4})?", t) and ys and all(1800 <= y <= 2100 for y in ys)
        return None if ok else "a year is 1800 to 2100"
    if k in ("date", "time", "phone"):          # BUILD IC-3 (D1): read by the one reader — every digit inside its date, time or phone
        xs = [x for x in quantities(t) if rule_key(x["rule"]) == {"date": "a date", "time": "a time", "phone": "a phone number"}[k]]
        ok = all(any(x["start"] <= i < x["end"] for x in xs) for i, c in enumerate(t) if c.isdigit())
        if k == "phone":
            return None if ok or _NF_SERVICE.fullmatch(t) else "a phone is a phone number or a three-digit service line (211, 311 … 911, 988)"
        return None if ok else f"it does not read as {NF_KINDS[k]}"
    if not re.search(r"[A-Za-z]", t):
        return f"{NF_KINDS[k]} holds a letter"
    if _nf_name_ok(t):              # IC-3 (review a #7): "211 Kansas", "7 Shelter", "Studio 54" — the checker's name rule (a number joined
        return None                 # to letters or beside a capitalized word, no quantifier, unit or share) reads a name, as IC-2 accepted
    q = [x for x in quantities(t) if x["role"] in ("quantity", "undecided")]     # BUILD IC-3 (D1): the one reader, for what the rule refuses
    if q:
        return f"“{q[0]['words']}” reads as a quantity ({rule_key(q[0]['rule'])}) — {NF_KINDS[k]} is a number joined to letters or beside a capitalized word"
    return None if _nf_name_ok(t) else (f"{NF_KINDS[k]} is a number joined to letters or beside a capitalized word — not a count of "
                                        "something, a share or an estimate")


_MENTION_COLON = re.compile(r"q:\s*[0-9a-f]{5}(?:[0-9a-f]{2})?\s*")   # BUILD IC-3 (D6): a mention's id written with a colon
_MENTION_DOT = re.compile(r"q\.[0-9a-f]{5}(?:[0-9a-f]{2})?")


def _lc(s):
    return s.lower() if isinstance(s, str) else s


def _mention_id_of(t):
    """BUILD IC-3 (D6; review a #6, b #3, #5, #9c): the mention id a token's inside names — the dot form in any letter case, or the colon
    form — lowered; None for any other inside."""
    low = (t or "").strip().lower()
    if _MENTION_COLON.fullmatch(low):
        return "q." + low[2:].strip()
    return low if _MENTION_DOT.fullmatch(low) else None


CTX_CHARS = 32       # BUILD IC-4 (D2): the context kept each side of a mention's expression (the W3C TextQuoteSelector's prefix/suffix)
_UNIT_IRREG = {"people": "person", "children": "child", "men": "man", "women": "woman", "feet": "foot", "teeth": "tooth", "mice": "mouse",
               "geese": "goose", "data": "data", "series": "series", "species": "species", "news": "news"}


def _unit_one(u):
    """BUILD IC-4 (D2): a unit or per noun in its singular form, lowered — "weeks" and "week" are one claim's unit. None stays None."""
    if not isinstance(u, str) or not u.strip():
        return None
    w = re.sub(r"\s+", " ", u.strip().lower()).split(" ")
    last = w[-1]
    if last in _UNIT_IRREG:
        one = _UNIT_IRREG[last]
    elif re.search(r"[^aeiou]ies$", last):
        one = last[:-3] + "y"
    elif re.search(r"(?:ss|x|z|ch|sh)es$", last):
        one = last[:-2]
    elif last.endswith("s") and not last.endswith(("ss", "us", "is")):
        one = last[:-1]
    else:
        one = last
    return " ".join(w[:-1] + [one])


def _numkey(v):
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, 9)


_TIME_ADJ = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year", "annual": "year", "annually": "year",
             "hourly": "hour", "nightly": "night", "quarterly": "quarter"}
_UNIT_SKIP = frozenset(("unique", "total", "separate", "distinct", "individual", "consecutive", "straight"))
_TAIL_STOP = frozenset("""a an the of in on at to for from by with and or but nor per as is are was were be been being has have had do does did
    that which who whom whose this these those it its their there than then so if not no into onto over under across within without during
    before after about around between through each every any all some more less fewer most least such only just also still very""".split())
_CTX_STOP = frozenset("""the and for are was were with that this from into onto than then they them their its has have had not but any all each
    per via who which what when where while also only just still more most less such very over under about after before across within
    without during between through those these there here our your his her been being can may will would should could does did done
    one two""".split())


def _tail_words(tail, n=3):
    """BUILD IC-4 (D2): the words right after an expression as a claim reads them — after a joining hyphen ("90-day", "8-12-week"), up to
    n lowercase words, stopping at a function word, a capitalised word, a number or a sign — and the noun of a "/<noun>" right after
    them ("80-120 contacts/day"). Returns (words, slash noun or None)."""
    s, words = tail or "", []
    m = re.match(r"[ \t]*[-\u2010\u2011\u2013][ \t]*(?=[A-Za-z])", s)
    pos = m.end() if m else 0
    while len(words) < n:
        m = re.match(r"[ \t]*([A-Za-z]+(?:['\u2019-][A-Za-z]+)*)", s[pos:])
        if not m or m.group(1)[:1].isupper() or m.group(1).lower() in _TAIL_STOP:
            break
        words.append(m.group(1).lower())
        pos += m.end()
    m = re.match(r"[ \t]*/[ \t]*([A-Za-z]+)", s[pos:])
    return words, (m.group(1).lower() if m else None)


def _claim_parts(x, tail=None):
    """BUILD IC-4 (D2): (the claim's unit, its per part, its unit words) — the unit is the first word after the expression that is not a
    time adjective or one of _UNIT_SKIP, singular; a time adjective is the per part ("80-120 unique daily contacts" is 80-120 contacts per
    day, as "80-120 contacts/day" is); the unit words (singular) are what the loose match compares. With no tail, the reader's own unit
    words (unit, unit2) are read."""
    words, slash = _tail_words(tail) if tail is not None else ([w.lower() for w in (x.get("unit"), x.get("unit2")) if isinstance(w, str) and w], None)
    per = _unit_one(x.get("per"))
    kept = []
    for w in words:
        if w in _TIME_ADJ:
            per = per or _TIME_ADJ[w]
        else:
            kept.append(w)
    if per is None and slash:
        per = _unit_one(slash)
    unit = next((_unit_one(w) for w in kept if w not in _UNIT_SKIP), None)
    return unit, per, sorted({_unit_one(w) for w in kept})


def claim_key(x, tail=None):
    """BUILD IC-4 (D2): what a quantity of the organizers' text claims, as one string — its shape, its value or values, its unit (singular),
    its hedge and bound, its per part — never its sentence. A mention's id comes from its place and this key, so text rewritten around an
    unchanged figure leaves the id as it was (F1: "Waitlist runs 8-12 weeks" → "8-12 week waitlist remains …"), and a changed value, unit,
    hedge or per part gives another. `tail`: the text after the expression (its unit words, _claim_parts)."""
    unit, per, _w = _claim_parts(x, tail)
    return json.dumps([x.get("shape"), _numkey(x.get("value")), _numkey(x.get("lo")), _numkey(x.get("hi")), _numkey(x.get("num")),
                       _numkey(x.get("den")), x.get("op"), (x.get("hedge") or "").strip().lower() or None, unit,
                       per, _numkey(x.get("per_value")), bool(x.get("money")), bool(x.get("pct"))],
                      separators=(",", ":"), ensure_ascii=False)


def claim_loose(x, tail=None):
    """BUILD IC-4 (D2): the claim with its unit set aside, and its unit words — two claims match loosely when these keys are equal and their
    unit words share one (or neither has any): "80-120 unique daily contacts" and "80-120 contacts/day"; "17 of 87 emergency shelter beds"
    and "17 of 87 shelter beds". The value, the hedge, the bound and the per part still match exactly."""
    _u, per, words = _claim_parts(x, tail)
    return json.dumps([x.get("shape"), _numkey(x.get("value")), _numkey(x.get("lo")), _numkey(x.get("hi")), _numkey(x.get("num")),
                       _numkey(x.get("den")), x.get("op"), (x.get("hedge") or "").strip().lower() or None,
                       per, _numkey(x.get("per_value")), bool(x.get("money")), bool(x.get("pct"))],
                      separators=(",", ":"), ensure_ascii=False), words


def unit_words_match(claim_a, words_a, claim_b, words_b):
    """BUILD IC-4 (D2): two claims whose loose keys are equal hold the same unit — the unit (claim key's 9th item) of one is among the
    other's unit words ("17 of 87 emergency shelter beds" and "17 of 87 shelter beds": "shelter"), or neither has a unit word. A word
    after the unit that is no part of it never makes the match ("4 in 10 participants lose contact" and "4 in 10 graduates lose contact"
    share "lose" and "contact" but not their unit: no match)."""
    wa, wb = set(words_a or []), set(words_b or [])
    if not wa and not wb:
        return True
    ua, ub = _claim_unit(claim_a), _claim_unit(claim_b)
    return bool((ua and ua in wb) or (ub and ub in wa))


def _ctx_words(ctx, drop=()):
    """BUILD IC-4 (D2): the content words of a mention's context (prefix and suffix), singular, lowered — stop words, words under three
    letters and the claim's own unit words (`drop`) set aside."""
    out = set()
    for w in re.findall(r"[A-Za-z]{3,}", " ".join(str(c or "") for c in (ctx or []))):
        lw = w.lower()
        if lw not in _CTX_STOP:
            one = _unit_one(lw)
            if one not in drop and lw not in drop:
                out.add(one)
    return out


def _claim_unit(claim):
    """BUILD IC-4 (D2): a claim key's unit (its 9th item), or None."""
    try:
        u = json.loads(claim)[8]
        return u if isinstance(u, str) and u else None
    except Exception:  # noqa: BLE001
        return None


def _claim_unit_per(claim):
    """BUILD IC-4 (D2): a claim key's unit and per part (claim_key's 9th and 10th items) — the words a context comparison sets aside."""
    try:
        v = json.loads(claim)
        return {w for w in (v[8], v[9]) if isinstance(w, str) and w}
    except Exception:  # noqa: BLE001
        return set()


_CLAUSE_END = re.compile(r"[.;!?:\u00b7\u2014](?:\s|$)")


def _ctx_sides(ctx, drop=()):
    """BUILD IC-4 (D2): a context's content words before and after the expression, each side cut at the nearest clause end (". ", "; ",
    ": ", "·", "—") — the words that say what the figure is about."""
    pre, suf = (list(ctx or []) + ["", ""])[:2]
    pre_parts = _CLAUSE_END.split(str(pre or ""))
    return _ctx_words([pre_parts[-1]], drop), _ctx_words([_CLAUSE_END.split(str(suf or ""))[0]], drop)


def _ctx_score(c1, c2, drop=()):
    """BUILD IC-4 (D2): how alike two contexts are (0 to 1) — the share of content words in common before the expression and after it
    (each side's Jaccard index, averaged over the sides that hold words); None when neither context holds a word."""
    a, b = _ctx_sides(c1, drop), _ctx_sides(c2, drop)
    js = [len(x & y) / len(x | y) for x, y in zip(a, b) if x or y]
    return sum(js) / len(js) if js else None


def _ctx_share(a, b):
    """BUILD IC-4 (D2): two contexts' content words share one — equal, or one the start of the other at four letters or more ("wait" and
    "waitlist"; a word cut at the window's edge)."""
    return any(x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x))) for x in a for y in b)


# ═══ BUILD IC-4 (D1): one identity per item — assigned once by Python, carried across blocks, retired never reused ═══
ITEM_KINDS = {"risks": "risk", "phases": "phase", "evidence_cards": "card", "comparison_rows": "row", "sroi_outcomes": "outcome",
              "staff_roles": "role", "kpis": "kpi", "failure_modes": "fm", "early_warnings": "ew", "triggers": "trigger", "judge_qa": "qa"}
ANALYSIS_ITEM_FIELDS = ("risks", "phases", "evidence_cards", "comparison_rows", "sroi_outcomes", "staff_roles")
DELIVERABLE_ITEM_FIELDS = ("kpis", "failure_modes", "early_warnings", "triggers", "judge_qa")
# BUILD IC-4 (D13): the lists a [[count:<list>]] may name — the reply schemas' list fields — and each list's name on the page
COUNT_LISTS = ANALYSIS_ITEM_FIELDS + DELIVERABLE_ITEM_FIELDS
COUNT_NAMES = {"risks": "risks", "phases": "phases", "evidence_cards": "evidence cards", "comparison_rows": "today-versus-proposed rows",
               "sroi_outcomes": "outcomes", "staff_roles": "staff roles", "kpis": "KPIs", "failure_modes": "failure modes",
               "early_warnings": "early warnings", "triggers": "triggers", "judge_qa": "judges' questions"}
ITEM_MATCH_MIN, ITEM_MATCH_MARGIN = 0.5, 0.15    # a rewritten item keeps its id only when its words are the nearest by a clear margin


def _item_text(item):
    """An item's words as exact matching reads them: every text value (a list item may be a string), spacing collapsed, lowered."""
    if isinstance(item, str):
        vals = [item]
    elif isinstance(item, dict):
        vals = [str(v) for _k, v in sorted(item.items()) if isinstance(v, str)]
    else:
        return ""
    return re.sub(r"\s+", " ", " | ".join(vals)).strip().lower()


def _item_words(item):
    """An item's content words as the near match reads them: its text with every token and tag set aside (a token's id may change
    between blocks), stop words and words under three letters left out."""
    t = TOKEN_RE.sub(" ", _item_text(item))
    t = re.sub(r"<[^>]+>", " ", t)
    return frozenset(w for w in re.findall(r"[a-z][a-z'\u2019-]{2,}", t) if w not in _CTX_STOP)


def _item_num(i, kind):
    m = re.fullmatch(re.escape(kind) + r"\.(\d+)", str(i or ""))
    return int(m.group(1)) if m else 0


def assign_item_ids(kind, n, used=()):
    """n new ids <kind>.<k>, numbered after the highest number of that kind in `used` (ids held and tombstones): never reused."""
    top = max([_item_num(u, kind) for u in used] or [0])
    return [f"{kind}.{top + j + 1}" for j in range(n)]


def carry_item_ids(kind, old_items, old_ids, new_items, used=()):
    """BUILD IC-4 (D1): Block 2's items given Block 1's ids — matching by text only assigns ids, each with its status:
      kept      the same words as a Block 1 item (one to one, in order);
      amended   the most alike of Block 1's items left (content words, Jaccard ≥ ITEM_MATCH_MIN), by ITEM_MATCH_MARGIN over the next
                both ways (the item over its second choice, and over the other items wanting the same Block 1 item);
      ambiguous alike, but not by a clear margin — a new id, never a transfer;
      added     like none — a new id, the next free number of its kind.
    A Block 1 item no item matches is retired: a tombstone (its id, its kind, what removed it). Returns (ids, statuses, tombstones)."""
    old = [(i, it) for i, it in zip(old_ids or [], old_items or [])]
    n = len(new_items or [])
    ids, st = [None] * n, [None] * n
    free = list(range(len(old)))
    for i, it in enumerate(new_items or []):
        t = _item_text(it)
        j = next((j for j in free if _item_text(old[j][1]) == t), None)
        if j is not None:
            ids[i], st[i] = old[j][0], "kept"
            free.remove(j)
    sc = {}
    for i in (i for i in range(n) if ids[i] is None):
        a = _item_words(new_items[i])
        for j in free:
            b = _item_words(old[j][1])
            sc[(i, j)] = len(a & b) / len(a | b) if (a | b) else 0.0
    for i in sorted({i for i, _j in sc}):
        mine = sorted(((v, j) for (i2, j), v in sc.items() if i2 == i), reverse=True)
        if not mine or mine[0][0] < ITEM_MATCH_MIN:
            continue
        v, j = mine[0]
        rivals = sorted((v2 for (i2, j2), v2 in sc.items() if j2 == j and i2 != i), reverse=True)
        if (len(mine) > 1 and v - mine[1][0] < ITEM_MATCH_MARGIN) or (rivals and v - rivals[0] < ITEM_MATCH_MARGIN):
            st[i] = "ambiguous"
            continue
        ids[i], st[i] = old[j][0], "amended"
    for i in range(n):
        if st[i] == "amended":
            free = [j for j in free if old[j][0] != ids[i]]
    fresh = assign_item_ids(kind, sum(1 for x in ids if x is None), set(used) | {o[0] for o in old} | {x for x in ids if x})
    for i in range(n):
        if ids[i] is None:
            ids[i], st[i] = fresh.pop(0), st[i] or "added"
    tomb = [{"id": old[j][0], "kind": kind, "removed_by": "the update: none of its items is this one"} for j in free]
    return ids, st, tomb


def analysis_item_ids(ma, base=None):
    """BUILD IC-4 (D1): {"ids": {field: [id per item]}, "status": {field: [...]}, "tombstones": [...]} for an analysis's lists
    (ANALYSIS_ITEM_FIELDS): its own `item_ids` when they fit its lists; else, at Block 2, carried from `base` (Block 1's analysis, whose
    ids are its own or assigned in its order) by carry_item_ids; else assigned in order. Deterministic: a rebuild gives the same ids."""
    ma = ma if isinstance(ma, dict) else {}
    own = ma.get("item_ids") if isinstance(ma.get("item_ids"), dict) else {}
    b = analysis_item_ids(base) if isinstance(base, dict) else None
    out = {"ids": {}, "status": {}, "tombstones": list((own.get("_tombstones") if isinstance(own.get("_tombstones"), list) else []))}
    for f in ANALYSIS_ITEM_FIELDS:
        lst = ma.get(f) if isinstance(ma.get(f), list) else []
        kind = ITEM_KINDS[f]
        mine = own.get(f)
        if isinstance(mine, list) and len(mine) == len(lst) and all(isinstance(x, str) and x.startswith(kind + ".") for x in mine) \
                and len(set(mine)) == len(mine):
            out["ids"][f], out["status"][f] = list(mine), ["held"] * len(lst)    # stored at parse: the statuses were the carry's
            continue
        if b is not None:
            bl = base.get(f) if isinstance(base.get(f), list) else []
            used = [t["id"] for t in b["tombstones"] if isinstance(t, dict)]
            ids, st, tomb = carry_item_ids(kind, bl, b["ids"][f], lst, used)
            out["ids"][f], out["status"][f] = ids, st
            out["tombstones"] += tomb
        else:
            out["ids"][f], out["status"][f] = assign_item_ids(kind, len(lst)), ["new"] * len(lst)
    return out


def item_ids_doc(ids_info):
    """The form an analysis stores its ids in (`item_ids`): field → ids, and `_tombstones`, kept apart from the items. The statuses of a
    carry (kept, amended, added, ambiguous) are the update's report, not the analysis's: an update that changes nothing leaves the
    analysis exactly Block 1's (t_oracle7's T-or7-3 and T-or7-5 caught the first form, which stored them)."""
    d = {f: list(v) for f, v in ids_info["ids"].items()}
    d["_tombstones"] = list(ids_info["tombstones"])
    return d


def reply_item_ids(reply):
    """BUILD IC-4 (D1): the deliverables reply's ids — every entry of each list in the reply's order (valid or not: a check that leaves
    one out retires its id, it never renumbers the rest), and the thesis."""
    reply = reply if isinstance(reply, dict) else {}
    out = {f: [f"{ITEM_KINDS[f]}.{i + 1}" for i in range(len(reply.get(f) or []))] if isinstance(reply.get(f), list) else []
           for f in DELIVERABLE_ITEM_FIELDS}
    out["thesis"] = ["thesis.1"] if isinstance(reply.get("thesis"), dict) else []
    return out


def risk_refs(reply, risk_ids):
    """BUILD IC-4 (D1): each early warning's risk as an id, read at parse — its index read against the list it was written against
    (`risk_ids`: that list's ids, in its order); None where the index names no risk."""
    out = []
    for w in (reply or {}).get("early_warnings") or [] if isinstance((reply or {}).get("early_warnings"), list) else []:
        ri = parse_number(w.get("risk")) if isinstance(w, dict) else None
        out.append(risk_ids[int(ri)] if ri is not None and float(ri).is_integer() and 0 <= ri < len(risk_ids) else None)
    return out



# ═══ BUILD IC-6 (D12): the Muse's items — D1's rule (assigned once by Python in the stored order, carried, never reused) ═══
# Each kind with its own letter, so no Muse id is also an Oracle id (the Oracle's phase.1 and kpi.1; the Muse's phase.f1 and kpi.k1).
MUSE_ITEM_KINDS = {"strategic.personas": ("persona", "p"), "strategic.channel_plan": ("channel", "c"), "strategic.calendar": ("phase", "f"),
                   "strategic.kpis": ("kpi", "k"), "creative.social_posts": ("post", "s")}


def _muse_num(i, kind, letter):
    """The number of a Muse id of this kind ("persona.p3" → 3), else 0."""
    s = str(i or "")
    head = kind + "." + letter
    tail = s[len(head):] if s.startswith(head) else ""
    return int(tail) if tail.isascii() and tail.isdigit() and not tail.startswith("0") else 0


def assign_muse_ids(field, n, used=()):
    """n new ids of a Muse list's kind (MUSE_ITEM_KINDS[field]), numbered after the highest number of that kind in `used` (ids held and
    retired): never reused."""
    kind, letter = MUSE_ITEM_KINDS[field]
    top = max([_muse_num(u, kind, letter) for u in used] or [0])
    return [f"{kind}.{letter}{top + j + 1}" for j in range(n)]


def muse_item_ids(lists, stored=None):
    """({field: [id per item]}, [the fields whose ids were assigned here]) for the Muse's lists (`lists`: {field: its items}, the campaign
    as stored): a field's stored ids when they fit its list (one per item, the kind's form, none twice), else assigned in the stored order
    (a Block 1 written before IC-6: once, at load). Never from the model's text or a reply's positions."""
    out, assigned = {}, []
    for f, (kind, letter) in MUSE_ITEM_KINDS.items():
        lst = lists.get(f) if isinstance(lists, dict) and isinstance(lists.get(f), list) else []
        mine = stored.get(f) if isinstance(stored, dict) else None
        if (isinstance(mine, list) and len(mine) == len(lst) and len(set(mine)) == len(mine)
                and all(_muse_num(x, kind, letter) > 0 for x in mine)):
            out[f] = list(mine)
        else:
            out[f] = assign_muse_ids(f, len(lst))
            assigned.append(f)
    return out, assigned


def changed_marks(reg):
    """BUILD IC-6 (D12): (the mention ids the curveball changed, removed or could not load — the registry's stale list — and the data
    figures that moved since Block 1: an id whose Block 1 copy b1.<id> holds another value) — the update's review order, rule (1), read
    from this run's registry (the Oracle's update_review_set and the Muse's muse_review_set); (set(), set()) with none."""
    if reg is None:
        return set(), set()
    hot = set(getattr(reg, "stale", {}) or {}) | {x for r in (getattr(reg, "review", None) or []) if isinstance(r, dict)
                                                   for x in (r.get("id"), r.get("b1_id")) if x}
    moved = set()
    for k, e in list((getattr(reg, "entries", None) or {}).items()):
        if not str(k).startswith("b1."):
            continue
        now = reg.get(k[3:])
        if now is not None and now.get("value") is not None and now.get("value") != (e or {}).get("value"):
            moved.add(k[3:].lower())
    return hot, moved


def cites_changed(reg, texts, marks=None):
    """BUILD IC-6 (D12): True when one of `texts` cites a mention of changed_marks — by the registry's own reading (cited_mentions) or
    by a token whose id names a figure that moved: the token as the resolver reads it (TOKEN_RE, the grammar), its id the part of its
    inside before "|", letter case set aside. The one reading of the update's review order, rule (1): the Oracle's update_review_set
    and the Muse's muse_review_set both call it (Phase 3, p3sites: the Oracle's own copy of the rule and the two copies of a second
    token grammar, its _TOKEN_HEAD and this module's _TOKEN_ID, are gone)."""
    if reg is None:
        return False
    hot, moved = marks if marks is not None else changed_marks(reg)
    for t in texts:
        if not isinstance(t, str) or "[[" not in t:
            continue
        if any(k in hot for k in reg.cited_mentions(t)) or any(m.group(1).split("|", 1)[0].strip().lower() in moved
                                                               for m in TOKEN_RE.finditer(t)):
            return True
    return False


def proposal_label_key(what):
    """BUILD IC-4 (D1): a proposal's label as its id reads it — letter case, spacing and punctuation set aside."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(what or "").lower())).strip()


def proposal_id(what, written, entries=()):
    """BUILD IC-4 (D1): a [[model:]] proposal's id — "model." and six characters of a hash of its label (proposal_label_key), so the same
    label keeps its id across blocks (a new number is the same assumption, changed) and a new label is a new assumption. A second number
    under one label in one run gets "model." and a hash of the label and that number. Was: the next free model.<n> in the order met."""
    k = proposal_label_key(what)
    h = hashlib.sha1(k.encode("utf-8")).hexdigest()
    pid = "model." + h[:6]
    if pid in entries:
        h2 = hashlib.sha1(f"{k}|{str(written).strip()}".encode("utf-8")).hexdigest()
        n = 6
        while "model." + h2[:n] in entries:
            n += 2
        pid = "model." + h2[:n]
    return pid


def _mention_key(sent):
    """BUILD IC-3 (D6; review b #2): the sentence as a mention's id reads it — its edge stops and separators set aside. _sentence_around
    keeps the boundary it stopped at ("…right now;" before "; the remainder", "…right now." at the text's end), so the id changed when
    only what followed the sentence did (a status record's alert at the curveball: a correct Block 1 citation shown as stale)."""
    return re.sub(r"^[\s.;:,!?…]+|[\s.;:,!?…]+$", "", sent or "")


# BUILD IC-3 (D11; review b #4): resolve's events whose text the page shows as the writer typed it, with no link — flagged, counted in U —
# by the word the details use; one map for every consumer that lists them (the diagram's typed figures and its details)
REFUSAL_EVENTS = {"quote_refused": "quote", "declared_refused": "declared", "stale_mention": "stale"}


def _text_field_ok(fld, key):
    """BUILD IC-2 (D2) / IC-3 (D6): whether a record's field is a text to quote from — names, ids, ZIPs, addresses, phones, dates, hours
    and routes left out (they are exempt or no figures), and the record's own id field. One rule for the field list and the mentions."""
    return not (NAME_KEYS.search(fld) or fld.lower() == "id" or fld == key
                or re.search(r"(?i)zip|address|phone|url|date|time|updated|hours|email|website|tract|fips|geoid|route|(?:^|[a-z_])id$|ids$", fld))


def text_fields_with_numbers(raw, records_fn, exempter=None, limit=30):
    """BUILD IC-2 (D2): the data's text fields that hold numbers the checker would count — [{endpoint, field, records, of, rid, number}],
    names, ids, ZIPs, addresses, phones, dates and hours left out (they are exempt or not figures). What the prompt lists to quote from.
    BUILD IC-3 (D1): what the checker would count is what the one reader calls a quantity — a field whose only quantity is in words
    ("about a third of callers") is offered too; the example is one in digits where the field has one."""
    ex = exempter or Exempter()
    out = {}
    for ep, payload in (raw or {}).items():
        try:
            recs = [r for r in records_fn(payload) if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            continue
        reg_ = Registry()
        reg_.set_data({ep: payload}, records_fn)
        key = reg_._rkey(ep, recs)          # IC-3 (D6): a one-record endpoint's first text field is no record id
        for r in recs:
            for fld, v in r.items():
                if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    v = " · ".join(v)
                if not isinstance(v, str) or not _text_field_ok(fld, key):   # the record's own id (a census tract on KC) is no figure to
                    continue                                                  # quote; a route a name (_text_field_ok: one rule, IC-3 D6)
                nums = [x["words"] for x in quantities(v, names=ex) if x["role"] == "quantity"]
                if not nums:
                    continue
                dig = next((w.strip(" ,.;:") for w in nums if re.search(r"\d", w)), nums[0].strip(" ,.;:"))   # review b #13: an example in digits
                d = out.setdefault((ep, fld), {"endpoint": ep, "field": fld, "records": 0, "of": len(recs), "rid": None, "number": None})
                if d["number"] is None and dig:
                    d.update(rid=str(r.get(key)) if key and r.get(key) is not None else None, number=dig)
                d["records"] += 1
    return list(out.values())[:limit]


# ═══ BUILD IC (D1, D2): authorship marks ═══
# A run of page text sits between its author's opening mark and the one closing mark, put where the text becomes HTML (the Oracle's
# fix_narrative, its escaping helpers, its template's author filters and finalize) and read by scan(); render_html strips every mark
# before the page is written. The code points are disjoint from every private-use code point the tree holds — U+E000 (SENTINEL),
# U+E001–U+E004 (oracle_harness's holds) and the vendored Mermaid's (U+E006–U+E020, U+E131, U+E237) — tests/t_registry_ic.py.
AUTHORS = ("org", "py", "model", "label", "undeclared")
MARK_OPEN = {"org": "\ue0a1", "py": "\ue0a2", "model": "\ue0a3", "label": "\ue0a4", "undeclared": "\ue0a5"}
MARK_CLOSE = "\ue0af"
_OPEN_OF = {v: k for k, v in MARK_OPEN.items()}
MARK_CHARS = "".join(MARK_OPEN.values()) + MARK_CLOSE
_MARK_RE = re.compile("[" + MARK_CHARS + "]")


def mark(author, s):
    """s as one run of `author`'s text ("" stays "")."""
    if author not in MARK_OPEN:
        raise ValueError(f"no author {author!r}")
    s = "" if s is None else str(s)
    return MARK_OPEN[author] + s + MARK_CLOSE if s else s


_BUILD_REF = r"(?:\bBUILD\b|\bB[0-9]\b|\bIC-[0-9]|\bIC \(|\bP0\.[0-9]|\breview [a-e]\b|\bPhase [0-9]|\bNR\.|\bREG\.|\.py\b)"
_BUILD_NOTE = re.compile(r"/\*(?!\s*===)(?:(?!\*/).)*?" + _BUILD_REF + r"(?:(?!\*/).)*?\*/"           # IC-4 Phase 7 (review c21)
                         r"|(?<![:\w\\/'\"])//[^\n]*?" + _BUILD_REF + r"[^\n]*"
                         r"|<!--(?!naipc:)(?:(?!-->).)*?" + _BUILD_REF + r"(?:(?!-->).)*?-->", re.S)
_SCRIPT_STYLE = re.compile(r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.S | re.I)


def strip_build_notes(html_text):
    """IC-4 Phase 7 (review c21, D7): a page as written — every comment that holds a build reference (BUILD, B3, IC-4, P0.7, a review's
    letter, a phase, NR., REG., a .py file) dropped from its scripts and styles, and every such HTML comment; a section marker
    "/* === … === */" stays (the pages' parts are found by it), and so does every other comment and every byte of code."""
    if not isinstance(html_text, str) or not html_text:
        return html_text
    out = _SCRIPT_STYLE.sub(lambda m: m.group(1) + _BUILD_NOTE.sub(lambda c: "" if c.group(0).startswith(("/*", "//")) else c.group(0),
                                                                    m.group(3)) + m.group(4), html_text)
    return re.sub(r"<!--(?!naipc:)(?:(?!-->).)*?" + _BUILD_REF + r"(?:(?!-->).)*?-->", "", out, flags=re.S)


def unmark(s):
    """s with every mark removed (anything that is not text, as it is)."""
    return _MARK_RE.sub("", s) if isinstance(s, str) and s else s


def has_marks(s):
    return isinstance(s, str) and bool(_MARK_RE.search(s))


_PUA = re.compile("[\ue000-\uf8ff]")


def strip_pua(v):
    """BUILD IC (D1, "Inputs"): v with every private-use character (U+E000–U+F8FF) removed — called where text enters a harness (the
    data, the brief, the announcement and change list, a reply, the sidecar). The marks, SENTINEL and the harness's holds are
    private-use code points: an input holding one could fake an author, a hold or a removal. Strings, lists, tuples and dicts (their
    keys too) are walked; any other value is returned as it is. Never raises."""
    if isinstance(v, str):
        return _PUA.sub("", v)
    if isinstance(v, list):
        return [strip_pua(x) for x in v]
    if isinstance(v, tuple):
        return tuple(strip_pua(x) for x in v)
    if isinstance(v, dict):
        return {(strip_pua(k) if isinstance(k, str) else k): strip_pua(x) for k, x in v.items()}
    return v


def wrapped(s):
    """True when s is one run: it opens with a mark whose closing mark is its last character."""
    if not isinstance(s, str) or len(s) < 2 or s[0] not in _OPEN_OF or s[-1] != MARK_CLOSE:
        return False
    depth = 0
    for i, c in enumerate(s):
        if c in _OPEN_OF:
            depth += 1
        elif c == MARK_CLOSE:
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False


def balance(s):
    """(s with its marks balanced, problems): a closing mark with no opening one is dropped; a mark left open is closed at the end."""
    if not has_marks(s):
        return s, []
    out, depth, probs = [], 0, []
    for c in s:
        if c in _OPEN_OF:
            depth += 1
        elif c == MARK_CLOSE:
            if depth == 0:
                probs.append("a closing mark with no opening one (dropped)")
                continue
            depth -= 1
        out.append(c)
    if depth:
        probs.append(f"{depth} mark(s) left open (closed at the end)")
        out.append(MARK_CLOSE * depth)
    return "".join(out), probs


def runs(s):
    """[(start, end, author)] of s's text between marks (positions in s; author None outside every mark) — for tests and tools."""
    out, stack, last = [], [], 0
    for m in _MARK_RE.finditer(s or ""):
        if m.start() > last:
            out.append((last, m.start(), stack[-1] if stack else None))
        c = m.group(0)
        if c == MARK_CLOSE:
            if stack:
                stack.pop()
        else:
            stack.append(_OPEN_OF[c])
        last = m.end()
    if last < len(s or ""):
        out.append((last, len(s), stack[-1] if stack else None))
    return out


# ═══ text: tags, sentences, removal ═══
_TAG = re.compile(r"<!--.*?-->|<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^<>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
_ATTR = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(?:=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+)))?")
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
RAW_TEXT = {"script", "style", "noscript", "template", "title", "textarea"}
_ABBREV = re.compile(r"(?:\b(?:St|Dr|Mr|Mrs|Ms|Mt|Ft|Rd|No|vs|etc|Inc|Co|Corp|Ltd|Jr|Sr|Ave|Blvd|approx|e\.g|i\.e|U\.S|a\.m|p\.m)|\b[A-Z])\.$")   # BUILD IC-8 (D1): the hub's abbreviations too (one splitter)
_ITEM_MARK = re.compile(r"(?:^|[:;]\s+)\s*\d{1,3}$")   # BUILD IC-8 (D1, d1l): a numbered item's mark before its stop — the sentence's only
#                                                       words so far, or after a colon or a semicolon; its stop ends no sentence


_TAG_AT = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>")
BLOCK_TAGS = {"p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "dd", "dt", "table", "thead", "tbody",
              "tfoot", "blockquote", "section", "article", "header", "footer", "figure", "figcaption", "caption", "pre", "br", "hr"}
CELL_TAGS = {"td", "th"}


def sentence_spans(s):
    """(start, end) of each sentence of an HTML string, split after . ! ? that are followed by white space (outside tags), not after
    a common abbreviation or a lone capital. B3 (Phase 4, review b F1): a block element's edge also ends a sentence, whatever follows
    it — before its opening tag, after its closing tag or a <br> — so a finding in one list item or paragraph removes that item, never
    the whole list (the model writes <li>…</li><li> with no space between); a table cell's sentence stops inside the cell's own tags,
    so a removal empties the cell and the table keeps its columns."""
    spans, start, i, n = [], 0, 0, len(s)

    def cut(at):
        nonlocal start
        if at > start:
            spans.append((start, at))
            start = at
    while i < n:
        c = s[i]
        if c == "[" and s.startswith("[[", i):                          # BUILD IC-8 (D1): a stop inside a token ends no sentence —
            m = TOKEN_RE.match(s, i)                                    # sentence ends are sought in the literal segments only
            if m is not None:
                i = m.end()
                continue
        if c == "<":
            if s.startswith("<!--", i):                                 # a comment is skipped whole
                e = s.find("-->", i)
                i = n if e < 0 else e + 3
                continue
            m = _TAG_AT.match(s, i)
            if m is not None:
                name = m.group(2).lower()
                if name in BLOCK_TAGS:
                    cut(m.end() if (m.group(1) or name in VOID) else i)
                elif name in CELL_TAGS:
                    cut(i if m.group(1) else m.end())
                i = m.end()
                continue
        if c in ".!?":
            j = i + 1
            while j < n and s[j] in "\"')”’":
                j += 1
            k, edge = j, False
            while k < n and s[k] == "<":                               # closing tags right after the stop belong to the sentence
                m = _TAG_AT.match(s, k)
                if m is None or not m.group(1) or m.group(2).lower() in CELL_TAGS:
                    break                                               # (a cell's own tags stay with the cell)
                k = m.end()
                if m.group(2).lower() in BLOCK_TAGS:
                    edge = True                                         # a block's close ends the sentence whatever follows
                    break
            nxt = _TAG_AT.match(s, k) if k < n and s[k] == "<" else None
            if (k >= n or s[k].isspace() or edge or (nxt is not None and nxt.group(2).lower() in BLOCK_TAGS | CELL_TAGS)) \
                    and not _ABBREV.search(re.sub(r"<[^<>]*>", "", s[start:i + 1])[-12:]) \
                    and not (c == "." and _ITEM_MARK.search(re.sub(r"<[^<>]*>", "", s[start:i]))):    # IC-8 (D1): "2." is no end
                cut(k)
                i = k
                continue
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


def sentence_at(s, pos):
    """BUILD IC (D4): the HTML of the sentence of s that holds index pos ("" when none)."""
    for a, b in sentence_spans(s or ""):
        if a <= pos < b:
            return s[a:b]
    return ""


# ═══ BUILD IC-8 (D1, I2): the shown text and the one owner of every display limit ═══
# What the model wrote is stored whole; a display limit is applied once, at its destination, to the text a reader sees (Q-UNIT: code
# points of the stored text with every token resolved to its plain figure, the markup's tags removed, the escape step's own &lt; and &gt;
# turned back), by fit_shown, with a recorded outcome. It never cuts inside a sentence: it keeps the longest run of whole leading
# sentences within the hard maximum, or an excerpt where the policy allows one and the first sentence alone is over it, or the chain.
def split_sentences(text):
    """The one sentence splitter's pieces of a text (sentence_spans: a stop inside a token, after an abbreviation or a lone capital, or a
    numbered item's mark ends none), each as written; [] for a text with no characters."""
    if not isinstance(text, str) or not text.strip():
        return []
    return [text[a:b] for a, b in sentence_spans(text) if text[a:b].strip()]


def shown_text(stored, reg=None, track=None):
    """The words a reader sees of a stored text: its tokens resolved plain on the registry (a sentence whose token names nothing left
    out, as the page leaves it out; with no registry every token's remnant out), the model's source tags in the words the page prints
    (_tag_sub: finalize's and | model's words — BUILD IC-8 P4.4, p44v), the escape step's own &lt; and &gt; back to < and >, tags
    removed, the registry's marks removed, each record id, endpoint slug and coded field name in its reader words when the run's names
    are set (set_measure_names: the page pass's names — p44v), white space collapsed, NFC. Not text → "". The text a limited field's
    hard maximum is measured on is the text its reader is shown (N12 A7: render, then validate)."""
    if not isinstance(stored, str):
        return ""
    t = stored
    if "[[" in t or "]]" in t or SENTINEL in t:
        if reg is not None:
            try:
                t = resolve_track_text(reg, t, track, html=False)[0]
            except Exception:  # noqa: BLE001 — measured as the page would leave it: no token shown
                t = strip_tokens(t)
        t = strip_tokens(t)
    if "[" in t or "【" in t or "［" in t:      # BUILD IC-8 (P4.4, p44v): a source tag in the words the page prints
        t = _tag_sub(t)
    t = t.replace("&lt;", "<").replace("&gt;", ">")
    t = _TAG.sub("", t)
    t = unmark(t)
    nm_ = _PAGE.get("measure_names")
    if nm_ is not None and t:            # BUILD IC-8 (P4.4, p44v): an id, a slug and a coded field in the words the page pass prints
        t = nm_.rewrite(t)[0]
    return _ud.normalize("NFC", " ".join(t.split()))


def measure(shown):
    """Q-UNIT: a shown text's length in code points."""
    return len(shown) if isinstance(shown, str) else 0


# Each row: the destination, the unit, the target and its source (the request's own words), the hard maximum and its owner, whether an
# excerpt is allowed (long prose only), the constant it replaces (SH-11 reads its value), the version. The chains are the call sites':
# each passes its own (the field's base, its default), since only the site knows them.
DISPLAY_POLICY = {
    "muse.og_description": {"dest": "head", "unit": "code points", "target": 150, "target_src": "the Muse's Call 1 request: MAX 150 characters",
                            "hard": 200, "owner": "Q-OG", "excerpt": False, "const": "muse_harness:MuseStrategic.og_description max_length=150",
                            "version": "ic8.1"},
    "muse.short_tagline": {"dest": "header", "unit": "code points", "target": 80, "target_src": "the Muse's Call 1 request: MAX 80 characters",
                           "hard": 120, "owner": "Q-TAGLINE", "excerpt": False, "const": "muse_harness:MuseStrategic.short_tagline max_length=80",
                           "version": "ic8.1"},
}
POLICY_VERSION = "ic8.1"
FITS = []          # every fit of this process: {policy, fit, reason, measure, dest, track, source} — the dispositions read them (D3)


def _excerpt(stored, reg, track, hard):
    """The first sentence cut at a word, outside tokens, with "…", its shown text within hard; "" when no word fits."""
    first = (split_sentences(stored) or [stored])[0]
    cuts = [m.start() for m in re.finditer(r"\s+", first) if not any(a < m.start() < b for a, b in
                                                                    ((t.start(), t.end()) for t in TOKEN_RE.finditer(first)))]
    for at in reversed(cuts):
        cand = first[:at].rstrip(" ,;:—–-") + "…"
        if measure(shown_text(cand, reg, track)) <= hard:
            return cand
    return ""


def _whole_leading(stored, reg, track, hard):
    """The longest run of whole leading sentences of stored whose shown text is within hard ("" when the first alone is over it)."""
    keep = ""
    for k, s in enumerate(split_sentences(stored)):
        cand = (keep + s) if keep else s
        if measure(shown_text(cand, reg, track)) > hard:
            break
        keep = cand
    return keep.rstrip()


def _ends_whole(text):
    """A text that ends at a sentence end (its last literal character a stop, closing quotes and tags after it allowed)."""
    t = re.sub(r"(?:<[^<>]*>|[\s\"')\]”’])+$", "", text or "")
    t = TOKEN_RE.sub("x", t)
    return t[-1:] in ".!?…"


def fit_shown(stored, reg, policy, dest=None, track=None, chain=None, count_bound=False, incomplete=False, source="candidate",
              unit_rule=False, unit=None):
    """D1: (the stored text to show, its disposition, a fixed reason) for one display destination. policy: a DISPLAY_POLICY key or a row.
    Within the hard maximum: the text as it is (reason "over_target" when over the target). Over it: the longest run of whole leading
    sentences within it ("trimmed", "whole_sentences"); where the policy allows an excerpt and the first sentence alone is over it, that
    sentence cut at a word with "…" ("trimmed", "excerpt"). incomplete (a stored value a cut damaged, known from its admission record):
    its whole leading sentences only ("trimmed", "base_incomplete"). Otherwise the chain: (source, text) pairs, or (source, text, opts),
    tried in order, each fitted the same way — a "base" source gives rejected_kept_base, any other fallback_default. Nothing left:
    ("", "omitted", "no_chain"). count_bound: the text read a count (recorded, for D2's seal). Every call is recorded in FITS, with `unit`
    (BUILD IC-8 D3: the unit that owns this display — its record is the unit's, Dispositions.fit) and the fit's key (_fit_key: its hard
    maximum and its stored text, hashed; no text is kept); a fit no unit owns that changes its text is a unit of its own (fit_units)."""
    pol = DISPLAY_POLICY[policy] if isinstance(policy, str) else dict(policy)
    key = policy if isinstance(policy, str) else pol.get("key", "?")
    hard, target = pol["hard"], pol.get("target")

    def done(text, fit, reason, n):
        FITS.append({"policy": key, "fit": fit, "reason": reason, "measure": n, "dest": dest or pol.get("dest"), "track": track,
                     "source": source, "count_bound": bool(count_bound), "unit": unit, "tkey": _fit_key(hard, stored, chain)})
        return text, fit, reason
    if unit_rule and isinstance(stored, str) and stored.strip():     # BUILD IC-8 (D3): printed as it is — the rule first
        why_ = unit_why(reg, stored, track)
        if why_:
            r_ = fit_shown(None, reg, pol | {"key": key}, dest, track, chain, count_bound, False, source, unit_rule)
            FITS.pop()
            return done(r_[0], r_[1], f"{why_}:{r_[2]}" if r_[1] != "omitted" else f"{why_}_no_chain", measure(shown_text(r_[0], reg, track)))
    if isinstance(stored, str) and stored.strip():
        if incomplete:
            t = stored
            if not _ends_whole(t):
                ss = split_sentences(t)
                t = "".join(ss[:-1]).rstrip() if len(ss) > 1 else ""
            t = _whole_leading(t, reg, track, hard) if t else ""
            if t:
                return done(t, "trimmed", "base_incomplete", measure(shown_text(t, reg, track)))
        else:
            n = measure(shown_text(stored, reg, track))
            if n <= hard:
                return done(stored, "kept_valid", "over_target" if target and n > target else "within", n)
            t = _whole_leading(stored, reg, track, hard)
            if t:
                return done(t, "trimmed", "whole_sentences", measure(shown_text(t, reg, track)))
            if pol.get("excerpt"):
                t = _excerpt(stored, reg, track, hard)
                if t:
                    return done(t, "trimmed", "excerpt", measure(shown_text(t, reg, track)))
    for c in chain or ():
        src_, txt = c[0], c[1]
        opts = c[2] if len(c) > 2 else {}
        if not isinstance(txt, str) or not txt.strip():
            continue
        t, fit, reason = fit_shown(txt, reg, pol | {"key": key}, dest, track, None, count_bound, bool(opts.get("incomplete")), src_)
        FITS.pop()
        if t:
            return done(t, "rejected_kept_base" if src_ == "base" else "fallback_default", f"{src_}:{reason}", measure(shown_text(t, reg, track)))
    return done("", "omitted", "no_chain", 0)


def _row(hard, const, excerpt=True, target=None, owner="D1: the site's own constant, now on the shown text"):
    return {"dest": "body", "unit": "code points", "target": target, "target_src": None, "hard": hard, "owner": owner, "excerpt": excerpt,
            "const": const, "version": POLICY_VERSION}


DISPLAY_POLICY.update({        # BUILD IC-8 (d1c): one row per kind of text; hard None = the caller's constant (fit_cap)
    "oracle.domain_label": _row(60, "oracle_harness.py domain_label_value: [:60]"),
    "oracle.update.why": _row(None, "oracle_harness.py UPDATE_WHY_CHARS"),
    "oracle.update.entry": _row(None, "oracle_harness.py take_entries: its rule's caps"),
    "oracle.update.impact": _row(None, "oracle_harness.py UPDATE_IMPACT_CHARS"),
    "oracle.update.text": _row(None, "oracle_harness.py take_text and text_ok: their caps"),
    "oracle.update.answer": _row(None, "oracle_harness.py UPDATE_ANSWER_CHARS: a brief answer, now fitted on the page (ORACLE_FIELD_FITS)"),
    "oracle.financial.role": _row(80, "oracle_harness.py stage_financial_model: a staff role [:80]"),
    "oracle.financial.category": _row(120, "oracle_harness.py stage_financial_model: an outcome's category [:120]"),
    "oracle.financial.why": _row(300, "oracle_harness.py stage_financial_model: a material outcome's why, cut at 300"),
    "oracle.financial.source_words": _row(200, "oracle_harness.py stage_financial_model: a proxy value's source words, cut at 200"),
    "oracle.financial.source": _row(400, "oracle_harness.py stage_financial_model: an outcome's source [:400]"),
    "oracle.financial.partial": _row(600, "oracle_harness.py stage_financial_model: the partial note, cut at 600"),
    "oracle.diagram.text": _row(None, "oracle_harness.py _dg_text: its caps (labels 60, edges 40, tips 240)"),
    "oracle.diagram.tip": _row(None, "oracle_harness.py DIAGRAM_TIP_CHARS"),
    "oracle.derived.label": _row(None, "oracle_harness.py DERIVED_LABEL_CHARS"),
    "oracle.derived.why": _row(None, "naipc_requirements.py NAME_CHARS × 3 (p07_plain_derived)"),
    "oracle.evidence.title": _row(60, "oracle_harness.py build_template_context: an evidence card's title [:60]"),
    "oracle.integrity.sentence": _row(None, "oracle_harness.py integrity_summary: [:200] and [:160]"),
    "oracle.fund": _row(None, "oracle_harness.py FUND_CAPS, and a fund line cut at 40"),
    "oracle.use_of_funds.role": _row(80, "oracle_harness.py use_of_funds_section: a role cut at 80"),
    "oracle.chat.phase": _row(200, "oracle_harness.py build_chatbot_context: a phase's content [:200]"),   # BUILD IC-8 (d1c3)
    "oracle.deliverables": _row(None, "oracle_harness.py DLV_CAPS[field] (× 2 where it sliced)",
                                owner="D1: target DLV_CAPS[f] (the request's \"at most N characters\"), hard twice that (_dlv_fit)"),
    "oracle.headlines": _row(320, "oracle_harness.py DLV_CAPS[\"headline\"] × 2", excerpt=False, target=160,
                             owner="D1: the request's 160 (\"at most 160 characters\"), the code's own × 2 tolerance, on the shown text"),
    "muse.update.impact": _row(None, "muse_harness.py MUSE_IMPACT_CHARS"),
    "hub.lead": _row(None, "landing_harness.py lead_html: its limit (500, 220)"),
    "hub.hero_lead": _row(220, "landing_harness.py main: the tagline, _truncate(hero_lead, 220)"),
    "edit.why": _row(None, "naipc_requirements.py EDIT_WHY_CHARS"),
})


DISPLAY_POLICY["page.preview"] = {      # BUILD IC-8 (D7, Q-OG): every page's head preview, one rule
    "dest": "head", "unit": "code points", "target": 150, "target_src": "the Muse's Call 1 request: MAX 150 characters (Q-OG: the one target)",
    "hard": 200, "owner": "Q-OG", "excerpt": False,
    "const": "the four templates' og:description (the hub's pitch | striptags: 773 characters on R7's page, V11; the Muse's | model)",
    "version": POLICY_VERSION}


def page_preview(text, reg=None, html=False, drop_class=None, chain=None, track=None):
    """BUILD IC-8 (D7, Q-OG): a page's preview — one shown text for the head's description, og:description and twitter:description,
    through fit_shown ("page.preview": target 150, hard 200, whole leading sentences, no excerpt; the chain where the first sentence alone
    is over it). `html`: the text is the page's markup — its marks removed, an <a> of class `drop_class` dropped whole (the Oracle's source
    badges), its tags removed, its entities decoded once, a space before punctuation closed. → fit_shown's (text, disposition, reason);
    the template escapes the shown text once (| e), never | model (which escapes & again, V59)."""
    t = text if isinstance(text, str) else ""
    if html and t:
        t = unmark(t) if has_marks(t) else t
        if drop_class:
            t = re.sub(r"<a\b[^>]*\bclass=\"[^\"]*\b" + re.escape(drop_class) + r"\b[^\"]*\"[^>]*>.*?</a>", " ", t, flags=re.S | re.I)
        t = _html.unescape(re.sub(r"<[^>]*>", " ", t))
        t = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(t.split()))
    return fit_shown(t, reg, "page.preview", "head", track=track, chain=chain)


def fit_row(key, hard=None, target=None):
    """D1: DISPLAY_POLICY[key] with its key — its hard maximum the caller's constant where the row has none, the smaller of the two where
    both are given; its target the caller's where given (a row of many fields: the deliverables' DLV_CAPS[f]). fit_cap reads it; a
    finalizer that records the fit's outcome passes the row to fit_shown."""
    row = dict(DISPLAY_POLICY[key], key=key)
    if hard is not None:
        row["hard"] = hard if row.get("hard") is None else min(row["hard"], hard)
    if target is not None:
        row["target"] = target
    return row


def fit_cap(text, key, hard, reg=None, track=None, chain=None, dest=None, target=None):
    """D1: fit_text with the row's hard maximum taken from the caller's own constant (a row whose hard is None), and the caller's target
    where given; the text as it is when it is not text or has no characters and no chain is given."""
    if not (isinstance(text, str) and text.strip()) and not chain:
        return text if isinstance(text, str) else ""
    return fit_shown(text, reg, fit_row(key, hard, target), dest, track, chain)[0]


def fit_text(text, policy, reg=None, track=None, chain=None, dest=None):
    """D1: fit_shown's text alone (a site with no disposition of its own); a text with no characters and no chain is returned as it is,
    unrecorded."""
    if not (isinstance(text, str) and text.strip()) and not chain:
        return text
    return fit_shown(text, reg, policy, dest, track, chain)[0]


def _fit_key(hard, stored, chain=None):
    """BUILD IC-8 (D3): a fit's identity in this process — its hard maximum and its stored text (the chain's texts when it has none),
    hashed: a fit made again on the same text at the same destination (a second render, a record computed twice) is the same unit. No
    text is kept."""
    t = stored if isinstance(stored, str) and stored.strip() else "\x1f".join(str(c[1]) for c in chain or () if len(c) > 1)
    return hashlib.sha256(f"{hard}\x00{t}".encode("utf-8", "surrogatepass")).hexdigest()[:10]


def _fit_source(fit, reason, source=None):
    """BUILD IC-8 (D3): a fit's source as its record names it — base for Block 1's text (a chain's base link; a damaged base kept to its
    whole leading sentences), default for a default link, else the text's own (`source`; candidate when none)."""
    if fit == "rejected_kept_base" or reason == "base_incomplete" or str(reason).startswith("base:"):
        return "base"
    if fit == "fallback_default":
        return "default"
    return source or "candidate"


def fit_units(disp, track, since=0):
    """BUILD IC-8 (D1, D3): each display fit of this process (FITS[since:], of `track` or of none) that changed its text — trimmed, a
    chain's link, omitted — and that no unit owns (fit_shown's unit), as a unit of its own: <track>.fit.<its policy>.<its key> — one per
    text and destination (_fit_key). A fit that keeps its text whole adds none: the item it shows is its unit. → the number added."""
    n = 0
    for f in FITS[since:]:
        if f.get("fit") == "kept_valid" or f.get("unit") or f.get("track") not in (None, track):
            continue
        u = f"{track}.fit.{str(f.get('policy')).removeprefix(track + '.')}.{f.get('tkey') or 'nokey'}"
        if disp.has(u):
            continue
        disp.expect(u)
        disp.add(u, f["fit"], f["reason"], _fit_source(f["fit"], f["reason"], f.get("source")))
        n += 1
    return n


# ═══ BUILD IC-6 (D9, G10): a note after the sentence ═══
# REG.span's "sources disagree" beside a figure whose sources disagree (G10: "the facility records show only 8 sources disagree free
# emergency-shelter beds") becomes a reference after the sentence and a note at the end of the page's main content. The marker is the
# span's own sup: its figures in data-sd — {"f": the figure (the sidecar figure a mention states), "o": each figure it disagrees with}
# — and both values in its title (Registry._disagree_title). A conflict is a pair, its key the sorted ids of its two figures: one note
# per pair, with both values (Registry.pair_note). place_notes moves the markers, once per page, on the page as written. (Not a
# private-use marker in the text: U+E001-U+E004 are the Oracle's holds, and a marker in the text would survive every plain-text form of
# a span — NR.clean_text keeps private-use characters: the chat context, the brief panel, the FAQ — where the sup keeps IC-5's words.
# The model's and the data's text reach a page only escaped, so no input writes a sup; with the registry, a marker's pair that is not
# two of this run's figures that disagree is dropped and named.)
NOTES_SLOT = "<!--naipc:sd-notes-->"       # where a page prints its notes (the end of its main content; the four templates hold it)
NOTES_HEAD = "Notes on figures whose sources disagree"
NOTES_EXEMPT = "the notes repeat figures shown above, each with both sources' values"
NOTES_FILE = "page_notes.json"             # the page's notes, by count and key (checkpoints; no words)
_SD_SUP = re.compile(r' ?<sup class="sd"(?P<a>[^<>]*)>sources disagree</sup>')
_SD_ESCAPED = re.compile(r'(?:\\u003c|\\x3c|&lt;|&#60;)sup class=(?:\\\\"|\\"|&quot;|&#34;)sd\b', re.I)
_SD_IN_ATTR = re.compile(r' ?(?:&lt;|&#60;)sup class=(?:&quot;|&#34;)sd(?:&quot;|&#34;)(?:(?!&gt;|&#62;).)*(?:&gt;|&#62;)sources disagree'
                         r'(?:&lt;|&#60;)/sup(?:&gt;|&#62;)', re.S)
_NOTES_RAW = re.compile(r"<(script|style|template|textarea)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.S | re.I)
_TAG_AT_END = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>$")
_A_OPEN, _A_CLOSE = re.compile(r"<a\b", re.I), re.compile(r"</a\s*>", re.I)


def _notes_at(s, a, b):
    """Where the references of the sentence s[a:b] go: after its last text — before the block and cell tags that close it (a <br> too)
    and the white space before them — and past the </a> of a link still open there (a link holds no link)."""
    k = b
    while k > a:
        t = s[a:k].rstrip()
        m = _TAG_AT_END.search(t)
        if m and m.group(2).lower() in BLOCK_TAGS | CELL_TAGS:
            k = a + m.start()
            continue
        k = a + len(t)
        break
    seg = s[a:k]
    if len(_A_OPEN.findall(seg)) > len(_A_CLOSE.findall(seg)):
        m = _A_CLOSE.search(s, k)
        k = m.end() if m else k
    return k


def _note_words(title):
    t = " ".join(str(title or "").split())
    t = (t[:1].upper() + t[1:]) if t else "Sources disagree."
    return t if t.endswith((".", "!", "?")) else t + "."


def place_notes(page_html, reg=None):
    """BUILD IC-6 (D9, G10): every "sources disagree" marker of a page (REG.span's sup) moved out of its sentence, on the page as rendered
    — before its integrity line reads it and before it is written (each harness; an authored page's marks stay as they are) — the marker removed,
    and after the end of its sentence (sentence_spans on the page's text: never inside a <script>, <style>, <template>, <textarea> or a
    comment, never inside a tag), inside its element, "[note N]" in full size (a link, role doc-noteref, data-num-exempt "marker") — once
    per note per sentence, the notes numbered by first appearance, one note per distinct conflict (data-sd); at the page's NOTES_SLOT one
    list (role doc-endnotes), each note both values with their as-of and who speaks (the marker's title) and a link back to the statement.
    A marker inside a raw-text element or a comment is removed and named; an escaped one in a script is named (a test fails on either).
    With `reg`, a marker whose ids are not this run's figures that disagree is removed and named. Returns (html, info): info {"notes":
    [{n, key, text, refs}], "refs", "dropped": [{where, why}], "slot": the slot was found, "placed": where the list went without one,
    "error"}. Never raises: on an error the page comes back as it was, the error named."""
    info = {"notes": [], "refs": 0, "dropped": [], "slot": False, "placed": None, "error": None}
    if not isinstance(page_html, str) or not page_html:
        return page_html, info
    try:
        return _place_notes(page_html, reg, info), info
    except Exception as e:  # noqa: BLE001 — the page as it was, the error named (the console and the record say it)
        info.update(notes=[], refs=0, error=f"{type(e).__name__}: {str(e)[:200]}")
        return page_html, info


def _sd_pairs(at, reg, info):
    """A marker's conflicts: [(key, first side, other side)] — each key the sorted ids of a pair; [] when none checks."""
    try:
        d = json.loads(at.get("data-sd") or "")
        f, os_ = str(d["f"]), [str(x) for x in d["o"]]
    except (ValueError, KeyError, TypeError):
        info["dropped"].append({"where": "the page's text", "why": "a marker whose figures cannot be read (an older page's form): its title stands for its note"})
        return [("title:" + (at.get("title") or ""), None, None)]
    out = []
    for o in os_:
        if reg is not None and not (reg.get(f) and reg.get(o) and (o in {x for x, _e in reg.disagreeing(f)}
                                                                     or f in {x for x, _e in reg.disagreeing(o)})):
            info["dropped"].append({"where": "the page's text", "why": f"{f} against {o}: not two of this run's figures whose sources disagree"})
            continue
        out.append((json.dumps(sorted((f, o))), f, o))
    return out


def _place_notes(page, reg, info):
    keyn, count = {}, {}

    def notes_of(m):
        at = _attrs(m.group("a"))
        ns = []
        for key, f, o in _sd_pairs(at, reg, info):
            if key not in keyn:
                keyn[key] = len(info["notes"]) + 1
                text = (reg.pair_note(f, o) if reg is not None and f is not None else None) or at.get("title") or ""
                info["notes"].append({"n": keyn[key], "key": key, "pair": sorted((f, o)) if f is not None else None, "text": text, "refs": 0})
            ns.append(keyn[key])
        return ns

    def in_attrs(m):                  # a marker written into an attribute (escaped): removed and named
        t = m.group(0)
        if not _SD_ESCAPED.search(t):
            return t
        n_ = len(_SD_IN_ATTR.findall(t))
        info["dropped"].append({"where": f"inside an attribute of a <{m.group(2).lower()}>",
                                "why": f"{n_} marker(s), removed" if n_ else "a marker's start, named (its form not whole)"})
        return _SD_IN_ATTR.sub("", t)

    def in_text(region):
        if "sup class=" in region and ("&lt;sup" in region or "&#60;sup" in region):
            region = _TAG_AT.sub(in_attrs, region)
        sups = list(_SD_SUP.finditer(region))
        if not sups:
            return region
        parts, marks, last, pos = [], [], 0, 0
        for m in sups:
            parts.append(region[last:m.start()])
            pos += m.start() - last
            ns = notes_of(m)
            if ns:
                marks.append((pos, ns))
            last = m.end()
        parts.append(region[last:])
        s = "".join(parts)
        if not marks:
            return s
        # an authored page holds its authors' marks (private-use code points) until it is written: its sentences are found on its text
        # without them ("beds.\ue0af</p>" ends at its stop), each place mapped back — a reference goes after a run's closing mark
        keep = [i for i, ch in enumerate(s) if not _MARK_RE.match(ch)] if _MARK_RE.search(s) else None
        c = "".join(s[i] for i in keep) if keep is not None else s
        if keep is not None:
            pre, n_ = [0] * (len(s) + 1), 0
            for i, ch in enumerate(s):
                pre[i] = n_
                n_ += 0 if _MARK_RE.match(ch) else 1
            pre[len(s)] = n_
            marks = [(pre[p], ns) for p, ns in marks]
        spans = sentence_spans(c) or [(0, len(c))]
        at_ = {}
        for p, ns in marks:           # the sentence that holds the figure the marker follows (the character before it)
            sp = next(((a, b) for a, b in spans if a < p <= b), None) or next(((a, b) for a, b in spans if a <= p < b), spans[-1])
            k = _notes_at(c, *sp)
            lst = at_.setdefault((keep[k] if k < len(keep) else len(s)) if keep is not None else k, [])
            lst += [n for n in ns if n not in lst]
        ins = []
        for k in sorted(at_):
            h = ""
            for n in at_[k]:
                count[n] = count.get(n, 0) + 1
                h += (f' <a href="#sd-note-{n:d}" id="sd-ref-{n:d}-{count[n]:d}" role="doc-noteref" class="sd-ref" '
                      f'data-num-exempt="marker">[note {n:d}]</a>')
            ins.append((k, h))
        for k, h in reversed(ins):
            s = s[:k] + h + s[k:]
        return s

    pieces, last = [], 0
    for rm in _NOTES_RAW.finditer(page):
        pieces.append(in_text(page[last:rm.start()]))
        raw, kind = rm.group(0), (rm.group(1) or "").lower()
        where = f"inside a <{kind}>" if kind else "inside a comment"
        n_lit = len(_SD_SUP.findall(raw))
        if n_lit:
            raw = _SD_SUP.sub("", raw)
            info["dropped"].append({"where": where, "why": f"{n_lit} marker(s), removed"})
        n_esc = len(_SD_ESCAPED.findall(raw))
        if n_esc:
            info["dropped"].append({"where": where, "why": f"{n_esc} escaped marker(s), named (not removed)"})
        pieces.append(raw)
        last = rm.end()
    pieces.append(in_text(page[last:]))
    out = "".join(pieces)
    for nt in info["notes"]:
        nt["refs"] = count.get(nt["n"], 0)
    info["refs"] = sum(count.values())
    sec = ""
    if info["notes"]:
        sec = (f'<section role="doc-endnotes" class="sd-notes" aria-labelledby="sd-notes-h" data-cov-skip '
               f'data-num-exempt="{esc(NOTES_EXEMPT)}"><h2 id="sd-notes-h">{esc(NOTES_HEAD)}</h2><ol>'
               + "".join(f'<li id="sd-note-{nt["n"]:d}">{esc(_note_words(nt["text"]))} '
                         f'<a href="#sd-ref-{nt["n"]:d}-1" class="sd-back">Back to the statement</a></li>' for nt in info["notes"])
               + "</ol></section>")
    if NOTES_SLOT in out:
        info["slot"] = True
        out = out.replace(NOTES_SLOT, sec, 1).replace(NOTES_SLOT, "")
    elif sec:
        m = None
        for pat in (r"</main\s*>", r"<footer\b", r"</body\s*>"):
            ms = [x for x in re.finditer(pat, out, re.I)]
            if ms:
                m = ms[0] if pat != r"</body\s*>" else ms[-1]
                info["placed"] = "before " + {"</main\\s*>": "</main>", "<footer\\b": "the first <footer>", "</body\\s*>": "</body>"}[pat]
                break
        out = (out[:m.start()] + sec + out[m.start():]) if m else out + sec
        info["placed"] = info["placed"] or "at the page's end"
    return out


def notes_words(info):
    """BUILD IC-6 (D9): the console's line for a page's notes — "" when the page has none and nothing was dropped."""
    if not isinstance(info, dict):
        return ""
    if info.get("error"):
        return f"⚠ notes: not placed ({info['error']}) — the page keeps its \"sources disagree\" markers"
    n, r, d = len(info.get("notes") or []), info.get("refs") or 0, info.get("dropped") or []
    if not n and not d:
        return ""
    s = (f"notes: {n} note{'' if n == 1 else 's'} on figures whose sources disagree ({r} reference{'' if r == 1 else 's'})"
         + ("" if info.get("slot") or not n else f"; the page has no notes slot — the list placed {info.get('placed')}"))
    return s + "".join(f"; ⚠ {x['why']} ({x['where']})" for x in d[:4]) + (f"; ⚠ {len(d) - 4} more" if len(d) > 4 else "")


def write_page_notes(cp_dir, info, run_id_=None):
    """BUILD IC-6 (D9): checkpoints/page_notes.json — the page's notes by number, key and reference count, the dropped markers and the
    slot (counts and the registry's ids only: no words). None, or why it was not written. Never raises."""
    try:
        doc = {"schema": 1, "run_id": run_id_, "notes": [{"n": x["n"], "pair": x.get("pair"), "refs": x["refs"]}
                                                        for x in (info or {}).get("notes") or []],
               "refs": (info or {}).get("refs") or 0, "dropped": [{"where": x["where"], "why": x["why"]} for x in (info or {}).get("dropped") or []],
               "slot": bool((info or {}).get("slot")), "placed": (info or {}).get("placed"), "error": (info or {}).get("error")}
        p = Path(cp_dir) / NOTES_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes((json.dumps(doc, ensure_ascii=False, indent=0) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def plain_sentence(frag):
    """A sentence's plain words: marks and tags removed, entities decoded, white space collapsed — what a reader sees."""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^<>]*>", " ", unmark(frag or "")))).strip()


def sentence_key(frag):
    """BUILD IC (D4): the key of a sentence in the override file — sha256 of its plain words, so any change to the words is a new key."""
    import hashlib
    return hashlib.sha256(plain_sentence(frag).encode("utf-8")).hexdigest()


def _unbalanced(fragment):
    """The tags of a removed fragment that must stay so the rest stays balanced: an opening tag whose close is outside it, a closing
    tag whose opening is outside it — in their order."""
    out, stack = [], []
    for m in _TAG.finditer(fragment):
        if m.group(0).startswith("<!--"):
            continue
        close, name = m.group(1) == "/", (m.group(2) or "").lower()
        if name in VOID or m.group(0).endswith("/>"):
            continue
        if not close:
            stack.append((name, m.group(0)))
        elif stack and stack[-1][0] == name:
            stack.pop()
        else:
            out.append(m.group(0))
    return "".join(out) + "".join(t for _n, t in stack)


def remove_marked(s, positions, plain=False):
    """Remove each sentence that holds a SENTINEL or one of `positions` (indexes into s); keep the tags the rest needs. Returns
    (text, removed sentences as plain text)."""
    if not isinstance(s, str):
        return s, []
    pos = sorted(set(positions) | {i for i, c in enumerate(s) if c == SENTINEL})
    if not pos:
        return s, []
    spans = sentence_spans(s)
    kill = [sp for sp in spans if any(sp[0] <= p < sp[1] for p in pos)]
    out, removed, last = [], [], 0
    for a, b in kill:
        out.append(s[last:a])
        frag = s[a:b]
        removed.append(re.sub(r"\s+", " ", re.sub(r"<[^<>]*>", "", frag.replace(SENTINEL, "[?]"))).strip())
        out.append(_unbalanced(frag))
        last = b
    out.append(s[last:])
    t = re.sub(r"[ \t]{2,}", " ", "".join(out)).replace(SENTINEL, "")
    return t.strip() if plain else t, removed


# ═══ BUILD IC-3 (D1): the one reader of quantities ═══
# Every piece of code that asks whether a stretch of text is a quantity, and what it is, asks quantities(). It splits the text into
# pieces that keep their place, reads digits and number words, composes them ("one hundred and five", "twenty-one", "3.2 million",
# "$500K", "a dozen", "half a million", "two thirds", "one and a half", "thirty (30)"), reads the frames around what it composed (a range,
# a ratio "N in M" / "N of M", a bound, a multiplier, a recurrence "every third", a hedge, a unit noun, a "per" part), keeps the longest
# expression, and decides its role last. Shape, value and role are separate answers; an expression no rule settles is "undecided" — listed,
# never dropped, never valued. Standard library only; no nested repeat over optional white space (D1: it must be fast and must stop).
#
# The number words and their values follow Microsoft Recognizers-Text's English definitions (Patterns/English/English-Numbers.yaml,
# CardinalNumberMap, OrdinalNumberMap, RoundNumberMap; commit da7edcff59f669b2a460ab9d400e36298f0d658e), under its licence:
#   MIT License — Copyright (c) Microsoft Corporation. All rights reserved.
#   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the
#   "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge,
#   publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
#   subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or
#   substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
#   NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
#   COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# Not taken (each a test that must fail if it creeps in): "few" as 3 and "dozens" as 12 (Duckling); "at least" as a strict "more than" and
# "up to" as a strict "less than" (CQE); a range's upper end left out ("[20,30)", Recognizers' range specs); "cents" read as a percent word.

READER_VERSION = "ic6-1"      # BUILD IC-6 (D11 d): the anaphoric counts — "both", "the two figures", "one of them", "one" heading a clause
#                               ("ic5-1", BUILD IC-5 D6: Unicode words, lang="es", date ranges, the pronoun, the counts)
_Q_UNITS = {"zero": 0, "naught": 0, "nought": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
            "eighteen": 18, "nineteen": 19}
_Q_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_Q_ROUND = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}
_Q_ROUND_PL = {k + "s": v for k, v in _Q_ROUND.items()}
_Q_ROUND.update({"mln": 10 ** 6, "bln": 10 ** 9, "tln": 10 ** 12})
_Q_DOZEN = {"dozen": 12, "dozens": 12, "dz": 12, "dzs": 12, "doz": 12, "dozs": 12}
_Q_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
          "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17,
          "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60,
          "seventieth": 70, "eightieth": 80, "ninetieth": 90, "hundredth": 100, "thousandth": 1000, "millionth": 10 ** 6,
          "billionth": 10 ** 9, "trillionth": 10 ** 12}
_Q_DENOM = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fourth": 4, "fourths": 4, "fifth": 5,
            "fifths": 5, "sixth": 6, "sixths": 6, "seventh": 7, "sevenths": 7, "eighth": 8, "eighths": 8, "ninth": 9, "ninths": 9,
            "tenth": 10, "tenths": 10, "hundredth": 100, "hundredths": 100, "thousandth": 1000, "thousandths": 1000,
            "millionth": 10 ** 6, "millionths": 10 ** 6, "billionth": 10 ** 9, "billionths": 10 ** 9, "trillionth": 10 ** 12,
            "trillionths": 10 ** 12}
_Q_MULT_WORDS = {"twice": 2, "thrice": 3, "double": 2, "triple": 3, "quadruple": 4}
# (ledger step 22) the verb forms ("costs tripled": D1 — a multiplier as a verb is undecided unless what it multiplies follows) and the
# "-fold" words ("a tenfold rise") — each had been read as nothing, an expression that vanished
_Q_MULT_WORDS.update({f + x: v for f, v in (("doubl", 2), ("tripl", 3), ("quadrupl", 4)) for x in ("ed", "es", "ing")})
_Q_MULT_WORDS.update({"halved": 0.5, "halving": 0.5})
_Q_MULT_WORDS.update({w + "fold": v for w, v in list(_Q_UNITS.items()) + list(_Q_TENS.items()) if v >= 2})
_Q_MULT_WORDS.update({"hundredfold": 100, "thousandfold": 1000})
_Q_VULGAR = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8, "⅙": 1 / 6, "⅚": 5 / 6,
             "⅐": 1 / 7, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875, "⅑": 1 / 9, "⅒": 0.1}
_Q_MAG = {"k": 1e3, "K": 1e3, "M": 1e6, "MM": 1e6, "B": 1e9, "b": 1e9, "bn": 1e9, "mn": 1e6}
# hedges (shape unchanged) and bounds (shape "bound"), each as written; longest first. "over" is a bound only when no time unit follows
# its number ("over 700 people"; "over three years" is a period); "minimum"/"maximum" state a threshold value, not a bound
_Q_HEDGES = ("approximately", "approx.", "roughly", "nearly", "almost", "around", "about", "close to", "some", "an estimated",
             "estimated", "just over", "just under", "well over", "more or less", "circa", "~", "≈")    # IC-3 (review a #10): "circa"
_Q_BOUNDS = ("less than or equal to", "greater than or equal to", "more than or equal to", "fewer than or equal to", "equal to or less than",
             "equal to or more than", "equal to or greater than", "equal to or fewer than", "at least or equal to", "bigger or equal than",
             "larger or equal than", "higher or equal than", "smaller or equal than", "less or equal than", "lower or equal than",
             "larger than", "bigger than", "higher than", "lower than", "smaller than", "exceeds", "exceeded", "exceed", "surpasses",
             "surpassed", "surpass", "no more than", "no fewer than", "no less than", "not more than", "not less than", "as many as", "as few as",
             "as much as", "as high as", "as low as", "up to",                                    # IC-3 (review a #10)
             "at least", "at most", "more than", "less than", "fewer than", "greater than", "upwards of", "in excess of", "under",
             "below", "above", "over", "exceeding", "≥", "≤", ">=", "<=", ">", "<")
_Q_BOUND_SUFFIX = ("and more", "and less", "and fewer", "or more", "or fewer", "or less", "and over", "and up", "and above", "or above", "or older", "or younger", "and older",
                   "and younger", "or greater", "plus")
_Q_TIME_UNITS = {"second", "seconds", "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs", "day", "days", "week", "weeks",
                 "month", "months", "mo", "year", "years", "yr", "yrs", "quarter", "quarters", "decade", "decades", "night", "nights",
                 "weekday", "weekdays", "lifetime"}
# BUILD IC-3 (ledger step 36): each time unit's base form — a per part said two ways is one ("/yr" and "a year"; dup_edges)
_Q_TIME_BASE = {w: {"min": "minute", "mins": "minute", "hr": "hour", "hrs": "hour", "mo": "month", "yr": "year", "yrs": "year",
                    "weekdays": "weekday"}.get(w, w[:-1] if w.endswith("s") and w[:-1] in _Q_TIME_UNITS else w) for w in _Q_TIME_UNITS}
# (ledger step 26) D1's twelve label words and the words the frozen labels call labels ("Zones 2 and 3", "pilot day one": markers, D12),
# with their kin; kept after step 25's restriction to the twelve broke T-rd-4 (§6 done-test 6: nothing a label calls a marker is counted)
_Q_LABEL_HEADS = ("Year", "Years", "Phase", "Phases", "Tier", "Tiers", "Step", "Steps", "Stage", "Stages", "Block", "Blocks", "Option",
                  "Options", "Gate", "Gates", "Section", "Sections", "Rule", "Rules", "Appendix", "Table", "Tables", "Level", "Levels",
                  "Zone", "Zones", "Day", "Days", "Week", "Weeks", "Month", "Months", "Quarter", "Quarters", "Wave", "Waves", "Round",
                  "Rounds", "Part", "Parts", "Item", "Items", "Chapter", "Figure", "Exhibit")
# (ledger step 26) a name with a head word — the frozen labels call "Route 11" a name (D12); a code's head too (Type 2, Form 990, Title IX).
# "Grade", added at step 22 with no label behind it, is out again
_Q_NAME_HEADS = ("Route", "Highway", "Hwy", "Interstate", "HB", "SB", "Station", "Suite", "Room", "Ward", "District", "Precinct", "Type",
                 "Form", "Title", "Article", "Building", "Bldg", "Apt", "Unit", "Lot", "Bus", "Pier", "Gate")
_Q_IDIOMS = (  # a closed list, matched as whole words, case set aside; each is no quantity (D1 "idiom")
    "one-stop", "one stop shop", "first-come", "first come", "first-served", "first served", "first-time", "first time", "first-hand",
    "firsthand", "at first", "for the first time", "first and foremost", "second chance", "second-chance", "second-guess", "second nature",
    "second opinion", "double-check", "double check", "double-count", "double count", "double-counting", "double-booked", "double-edged",
    "single out", "single-handedly", "one another", "no one", "each one", "every one", "one by one", "one-on-one", "one on one", "one-off",
    "one-to-one", "one to one",                                                          # (ledger step 22: the hyphen frame reads N-to-M)
    "one-time", "one-way", "one way or another", "the one that", "the one who", "the one which", "this one", "that one", "which one",
    "any one", "no single", "not a single", "a single one", "third-party", "third party", "third parties", "half-hearted", "zero-tolerance",
    "zero tolerance", "zero-sum", "at once", "once again", "on the one hand", "the latter two", "the former two")
_Q_VAGUE = ("dozens of", "hundreds of", "thousands of", "millions of", "a few", "a handful of", "several", "many", "most", "a majority of",
            "a minority of", "numerous", "countless", "a couple of")
_Q_HOUSEHOLD = {"adult", "adults", "parent", "parents", "men", "women", "man", "woman", "mother", "mothers", "father", "fathers",
                "person", "people", "family", "families", "household", "households", "occupancy", "room", "rooms", "bed", "beds"}
_Q_REF_NOUNS = {"figures", "figure", "measures", "numbers", "rates", "shares", "sets", "groups", "sides", "options", "estimates",
                "ratios", "totals", "counts", "sources", "approaches", "of them", "are", "differ", "measure"}
_Q_FUNC = {"the", "a", "an", "of", "in", "to", "and", "or", "for", "by", "with", "at", "on", "from", "per", "as", "is", "are", "was",
           "were", "be", "than", "that", "this", "these", "those", "its", "their", "our", "your", "his", "her", "each", "every", "it"}
_Q_COUNTED = {"people", "persons", "residents", "families", "households", "beds", "calls", "units", "visits", "patients", "clients", "youth",
              "students", "participants", "individuals", "children", "adults", "veterans", "seniors", "meals", "pounds", "lbs", "homes",
              "slots", "sites", "shelters", "staff", "dollars", "cases", "contacts", "referrals", "placements", "applications", "members"}
_Q_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
             "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec")
_Q_DAYS = r"(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)(?:day)?|weekdays?|weekends?"

_Q_BH = _Q_BOUNDS + _Q_HEDGES
# IC-3 (ledger step 20): the reader's own number words as patterns, built from its lists — every structure rule reads a number in words as
# it reads one in digits ("aged nineteen" as "aged 19", "Week twelve" as "Week 12"); no rule keeps a hand-written subset (t_static_ic3 T-st-7)
_Q_UNIT19 = "|".join(sorted((k for k, v in _Q_UNITS.items() if 1 <= v <= 9), key=len, reverse=True))     # a compound's unit: one … nine
_Q_CARD99 = (r"(?:(?:" + "|".join(sorted(_Q_TENS, key=len, reverse=True)) + r")(?:-(?:" + _Q_UNIT19 + r"))?|"
             + "|".join(sorted(_Q_UNITS, key=len, reverse=True)) + r")")                                 # zero … ninety-nine in words
_Q_ROUND_RX = "|".join(sorted(_Q_ROUND, key=len, reverse=True))
_Q_WORDS_N = (r"(?:" + _Q_CARD99 + r"|" + _Q_ROUND_RX + r")\b(?:[\s-]+(?:and\s+)?(?:" + _Q_CARD99 + r"|" + _Q_ROUND_RX
              + r")\b)*")                                                                              # a cardinal in words, any size
_Q_NUM_WORDS = (set(_Q_UNITS) | set(_Q_TENS) | set(_Q_ROUND) | set(_Q_ROUND_PL) | set(_Q_ORD) | set(_Q_DENOM) | set(_Q_MULT_WORDS)
                | set(_Q_DOZEN) | {"single", "both"})                                                   # every word the reader reads as a number
_Q_HAS_NUM = re.compile(r"\d|\b(?:" + "|".join(sorted(_Q_NUM_WORDS, key=len, reverse=True)) + r")\b", re.I)
_Q_IDIOM_RX = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(x) for x in sorted(_Q_IDIOMS, key=len, reverse=True)) + r")(?![\w-])")
_Q_START = (set(_Q_UNITS) | set(_Q_TENS) | set(_Q_ROUND) | set(_Q_ROUND_PL) | set(_Q_ORD) | set(_Q_DENOM) | set(_Q_MULT_WORDS)
            | {"a", "an", "half", "quarter", "single", "both", "every", "minus", "negative", "dozen", "dozens"}
            | {p.split(" ")[0].rstrip(".") for p in _Q_HEDGES + _Q_BOUNDS if p[0].isalpha()})
_Q_START_SYM = {"$", "-", "−", "~", "≈", "<", ">", "≥", "≤"}
_QT = re.compile(r"(?P<fig>⟦⟧)|(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)|(?P<vul>[½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅐⅛⅜⅝⅞⅑⅒])|(?P<word>[^\W\d_]+(?:['’][^\W\d_]+)?)"
                 r"|(?P<sp>\s+)|(?P<sym>.)", re.S)


class _Tok:
    __slots__ = ("k", "s", "e", "t", "lw")

    def __init__(self, k, s, e, t):
        self.k, self.s, self.e, self.t, self.lw = k, s, e, t, t.lower()


def _q_tokens(text, fixed):
    """The text as pieces that keep their place; a fixed span (a cited figure) is one piece of kind "fig"."""
    out, i, fx = [], 0, sorted(fixed or ())
    j = 0
    while i < len(text):
        while j < len(fx) and fx[j][1] <= i:
            j += 1
        if j < len(fx) and fx[j][0] <= i < fx[j][1]:
            out.append(_Tok("fig", fx[j][0], fx[j][1], text[fx[j][0]:fx[j][1]]))
            i = fx[j][1]
            continue
        lim = fx[j][0] if j < len(fx) else len(text)
        m = _QT.match(text, i)
        k = m.lastgroup
        e = min(m.end(), lim) if k != "fig" else m.end()
        out.append(_Tok(k, i, e, text[i:e]))
        i = e
    return out


def _skip_sp(toks, i):
    while i < len(toks) and toks[i].k == "sp":
        i += 1
    return i


def _word_at(toks, i):
    i = _skip_sp(toks, i)
    return (toks[i].lw if i < len(toks) and toks[i].k == "word" else None), i


_PH_INDEX = {}


def _phrase_at(toks, i, phrases):
    """The longest phrase of `phrases` (lower case, words parted by single spaces) that starts at token i, as (phrase, end token)."""
    best = None
    key = id(phrases)
    if key not in _PH_INDEX:
        idx = {}
        for ph in phrases:
            idx.setdefault(ph.split(" ")[0][:1] if not ph[0].isalpha() else ph.split(" ")[0].rstrip("."), []).append(ph)
        _PH_INDEX[key] = (phrases, idx)
    t0 = toks[i] if i < len(toks) else None
    if t0 is None:
        return None
    cands = _PH_INDEX[key][1].get(t0.lw if t0.k == "word" else t0.t[:1], ())
    for ph in cands:
        ws, k, ok = ph.split(" "), i, True
        for n, w in enumerate(ws):
            if n:
                if k >= len(toks) or toks[k].k != "sp":
                    ok = False
                    break
                k += 1
            if k >= len(toks):
                ok = False
                break
            t = toks[k]
            if t.k in ("word", "sym") and t.lw == w:
                k += 1
            elif t.k == "word" and w.endswith(".") and t.lw == w[:-1] and k + 1 < len(toks) and toks[k + 1].t == ".":
                k += 2                                                     # IC-3 (review a #10): "approx." — the word, then its stop
            elif t.k == "sym" and w.startswith(t.t) and len(w) > 1:       # "approx." is a word and a stop
                if k + 1 < len(toks) and toks[k].t + toks[k + 1].t == w:
                    k += 2
                else:
                    ok = False
                    break
            else:
                ok = False
                break
        if ok and (best is None or k > best[1]):
            best = (ph, k)
    return best


def _compose_words(toks, i):
    """A number written in words from token i: (value, end token, is_ordinal) or None. Units, tens joined to a unit by a hyphen, a space or a
    spaced hyphen ("twenty-one", "fifty - two"), hundreds with "and", thousands and beyond, dozens ("three hundred and two dozens" is 324),
    an ordinal last word ("twenty-first", "three trillionth"), decimal digits after "point", and "and" + a fraction ("one hundred and three
    quarters" is 100.75; "two million and three fifths" is 2,600,000; "one billion and a half million" is 1,000,500,000)."""
    total, cur, k, used, last_end, ordinal, last_add, big = 0, 0, i, False, i, False, 0, 0
    n = len(toks)
    while k < n:
        t = toks[k]
        if t.k != "word":
            break
        w = t.lw
        if w in _Q_UNITS or w in _Q_TENS:
            v = _Q_UNITS.get(w, _Q_TENS.get(w))
            if used and cur % 100 >= 20 and cur % 10 == 0 and v < 10 and last_add >= 20:
                cur += v
            elif not used or (cur % 100 == 0 and last_add >= 100) or (cur == 0 and big):
                cur += v
            else:
                break
            used, last_end, last_add = True, k + 1, v
        elif w in _Q_DOZEN and used:
            cur = cur - last_add + last_add * 12
            last_end, last_add = k + 1, 0
        elif w in _Q_ROUND and used:
            if w == "hundred":
                cur = (cur or 1) * 100
                last_add = 100
            else:
                total += (cur or 1) * _Q_ROUND[w]
                big = max(big, _Q_ROUND[w])
                cur, last_add = 0, _Q_ROUND[w]
            last_end = k + 1
        elif w in _Q_ORD and used and ((_Q_ORD[w] < 10 and cur % 10 == 0 and cur % 100 >= 20 and last_add >= 20)
                                       or (_Q_ORD[w] < 100 and (last_add >= 100 or (big and cur == 0)) and cur % 100 == 0)):
            cur += _Q_ORD[w]
            last_end, ordinal = k + 1, True
            break
        elif w in ("hundredth", "thousandth", "millionth", "billionth", "trillionth") and used:
            r = _Q_ORD[w]
            if r == 100:
                cur = (cur or 1) * 100
            else:
                total, cur = total + (cur or 1) * r, 0
            last_end, ordinal = k + 1, True
            break
        else:
            break
        m = k + 1
        if m < n and toks[m].k == "sym" and toks[m].t == "-" and m + 1 < n and toks[m + 1].k == "word":
            k = m + 1
            continue
        if m + 2 < n and toks[m].k == "sp" and toks[m + 1].t == "-" and toks[m + 2].k == "sp" and m + 3 < n and toks[m + 3].k == "word" \
                and toks[m + 3].lw in _Q_UNITS and last_add >= 20 and last_add < 100:
            k = m + 3                                                   # "fifty - two"
            continue
        if m + 2 < n and toks[m].t == "," and big and last_add >= 1000 and toks[m + 1].k == "sp" and toks[m + 2].k == "word" \
                and (toks[m + 2].lw in _Q_UNITS or toks[m + 2].lw in _Q_TENS) and toks[m + 2].lw != "zero":
            k = m + 2                                                   # "one million, two thousand and three" (CLDR's verbose style)
            continue
        if m < n and toks[m].k == "sp":
            m2 = m + 1
            if m2 < n and toks[m2].k == "word" and toks[m2].lw in ("and",) and m2 + 2 < n and toks[m2 + 2].lw in _Q_ORD and _Q_ORD[toks[m2 + 2].lw] < 100 \
                    and (last_add >= 100 or big) and cur % 100 == 0:
                k = m2 + 2                                              # "two thousand and fifth"
                continue
            if m2 < n and toks[m2].k == "word" and toks[m2].lw == "and" and (last_add >= 100 or big) and (cur % 100 == 0):
                m3 = _skip_sp(toks, m2 + 1)
                if m3 < n and toks[m3].k == "word" and (toks[m3].lw in _Q_UNITS or toks[m3].lw in _Q_TENS) and toks[m3].lw != "zero" \
                        and not (m3 + 2 < n and toks[m3 + 1].k == "sp" and toks[m3 + 2].lw in _Q_DENOM):
                    k = m3
                    continue
                break
            if m2 < n and toks[m2].k == "word" and (toks[m2].lw in _Q_UNITS or toks[m2].lw in _Q_TENS or toks[m2].lw in _Q_ROUND
                                                    or toks[m2].lw in _Q_DOZEN or toks[m2].lw in ("hundredth", "thousandth", "millionth", "billionth", "trillionth")
                                                    or (toks[m2].lw in _Q_ORD and _Q_ORD[toks[m2].lw] < 10 and last_add >= 20)
                                                    or (toks[m2].lw in _Q_ORD and _Q_ORD[toks[m2].lw] < 100 and (last_add >= 100 or big) and cur % 100 == 0)):
                if (toks[m2].lw in _Q_UNITS or toks[m2].lw in _Q_TENS) and not (last_add >= 100 or big or (last_add >= 20 and cur % 10 == 0 and toks[m2].lw in _Q_UNITS)):
                    break                                               # "one two": two numbers
                k = m2
                continue
        break
    if not used:
        return None
    value, j = total + cur, last_end
    if ordinal:
        return value, j, True
    # decimal digits after "point": "two hundred point zero three", "nine point nought four", "two hundred point seventy-one"
    k = _skip_sp(toks, j)
    if k < n and k > j and toks[k].lw == "point":
        k2, digits = _skip_sp(toks, k + 1), ""
        while k2 < n and toks[k2].k == "word" and (toks[k2].lw in _Q_UNITS and _Q_UNITS[toks[k2].lw] < 10 or toks[k2].lw in ("oh",)):
            digits += str(_Q_UNITS.get(toks[k2].lw, 0))
            last = k2
            k2n = _skip_sp(toks, k2 + 1)
            if k2n == k2 + 1:
                break
            k2 = k2n
        if not digits and k2 < n and toks[k2].k == "word" and (toks[k2].lw in _Q_TENS or toks[k2].lw in _Q_UNITS):
            c = _compose_words(toks, k2)
            if c is not None and c[0] < 100:
                digits, last = str(int(c[0])), c[1] - 1
        if digits:
            value, j = float(f"{int(value)}.{digits}"), last + 1
    # "and" + a fraction: "and a half", "and three quarters", "and two thirds", "and a thirtieth" [+ a round word]
    k = _skip_sp(toks, j)
    if k < n and k > j and toks[k].lw == "and":
        f = _fraction_words(toks, _skip_sp(toks, k + 1))
        if f is not None:
            fv, fj = f
            k5 = _skip_sp(toks, fj)
            if k5 < n and k5 > fj and toks[k5].k == "word" and toks[k5].lw in _Q_ROUND and toks[k5].lw != "hundred":
                r = _Q_ROUND[toks[k5].lw]
                value = (total + fv * r) if total else (cur + fv) * r
                return value, k5 + 1, False
            value = value + fv * (big if (big and cur == 0) else 1)
            return value, fj, False
    return value, j, False


def _fraction_words(toks, k):
    """A fraction in words at token k: "a half", "one half", "a quarter", "three quarters", "two thirds", "a thirtieth", "five eighths" —
    (value, end token) or None."""
    n = len(toks)
    if k >= n or toks[k].k != "word":
        return None
    w = toks[k].lw
    if w in ("a", "an", "one"):
        k2 = _skip_sp(toks, k + 1)
        if w == "one" and k2 < n and toks[k2].k == "sym" and toks[k2].t == "-":
            k2 = _skip_sp(toks, k2 + 1)                                 # "one-eighth", "one - fourth"
        if k2 < n and toks[k2].k == "word":
            d = _denom_of(toks, k2)
            if d is not None:
                return 1 / d[0], d[1]
        if w != "one":
            return None
    c = _compose_words(toks, k)                                         # "one hundred thirty-two fifths" falls through to here
    if c is None or c[2]:
        return None
    k2 = c[1]
    if k2 + 2 < n and toks[k2].k == "sp" and toks[k2 + 1].t == "-" and toks[k2 + 2].k == "sp":
        k2 = k2 + 1                                                     # "one - fourth"
    if k2 < n and toks[k2].k in ("sp", "sym") and toks[k2].t.strip() in ("", "-"):
        k3 = _skip_sp(toks, k2 + 1) if toks[k2].t.strip() == "-" else k2 + 1
        if k3 < n and toks[k3].k == "word":
            d = _denom_of(toks, k3)
            if d is not None and ((toks[d[1] - 1].lw.endswith("s") and c[0] > 1) or c[0] == 1):   # "twenty-three fifths"; "one fifth"; never "one hundred sixth"
                # (ledger 14: the plural is the denominator's LAST word — "three twenty-firsts")
                return c[0] / d[0], d[1]
    return None


def _denom_of(toks, k):
    """A denominator word at token k (half, third(s), quarter(s), an ordinal from the fourth, "twenty-first"): (denominator, end) or None."""
    w = toks[k].lw
    if w in _Q_DENOM:
        return _Q_DENOM[w], k + 1
    base = w[:-1] if w.endswith("s") and w[:-1] in _Q_ORD else w
    if base in _Q_ORD and _Q_ORD[base] >= 4:
        return _Q_ORD[base], k + 1
    if w in _Q_TENS:
        c = _compose_words(toks, k)
        if c is not None and c[2]:
            return c[0], c[1]
        # (ledger 14) "twenty-firsts", "thirty-seconds": a compound ordinal in the plural — "three twenty-firsts" had read 3 and 20
        if k + 2 < len(toks) and toks[k + 1].k == "sym" and toks[k + 1].t == "-" and toks[k + 2].k == "word":
            p = toks[k + 2].lw
            if p.endswith("s") and _Q_ORD.get(p[:-1], 10) < 10:
                return _Q_TENS[w] + _Q_ORD[p[:-1]], k + 3
    return None


def _num_value(t):
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _new(text, s, e, **kw):
    d = {"start": s, "end": e, "words": text[s:e], "shape": None, "value": None, "lo": None, "hi": None, "num": None, "den": None,
         "op": None, "hedge": None, "unit": None, "per": None, "per_value": None, "role": None, "rule": None, "kind": None,
         "fixed": [], "money": False, "pct": False, "parts": []}     # IC-3 (D2): parts — the spans of the values a frame joins
    d.update(kw)
    return d


# ── names and structure by pattern, read before any number (each a rule with its name) ──
_QN = [
    # (ledger 14) a URL: its digits and its %XX escapes are no numbers ("…%20and%20…", "?q=30%25")
    ("a URL", re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")),
    ("a date", re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+Z?)?\b|\b\d{4}-(?:0[1-9]|1[0-2])\b(?!-\d)|\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/\d{2,4}\b"
                          r"|\b(?:" + "|".join(_Q_MONTHS) + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?(?![\d%])(?:\s*[–—-]\s*(?:(?:" + "|".join(_Q_MONTHS)
                          + r")\.?\s+)?\d{1,2}(?![\d%]))?(?:,\s*\d{4})?|\b\d{1,2}\s+(?:" + "|".join(_Q_MONTHS) + r")\b(?:\s+\d{4})?"
                          r"|\b(?:" + "|".join(_Q_MONTHS) + r")\.?\s+\d{4}\b|\b\d{4}-Q[1-4](?:-Q[1-4])?\b"
                          # IC-3 (review a #3): IC-2's bare month/day — after "on", "since", "until", "through", "before", "after",
                          # "between" or a weekday, or with a day of 13 to 31 ("by 6/30", "the 6/30 deadline"); "1/3" stays a fraction
                          r"|(?i:\b(?:on|since|until|through|before|after|between|(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s+)"
                          r"(?P<v>(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))\b(?![%/]|\.\d)"   # the item: the date alone ("v"), not its cue
                          r"|(?<![\d/.])(?:0?[1-9]|1[0-2])/(?:1[3-9]|2\d|3[01])\b(?![%/]|\.\d)")),
    # (a bare "a" or "p" only joined to its digits, "8a-4p": after a space it is a word — "Year 1 a Python constant", "3 a week"; ledger 13)
    ("a time", re.compile(r"(?i)\b\d{1,2}(?::\d{2})?(?:\s*(?:a\.?m\.?|p\.?m\.?)|[ap])(?![a-z])(?:\s*[–—-]\s*\d{1,2}(?::\d{2})?(?:\s*(?:a\.?m\.?|p\.?m\.?)|[ap])(?![a-z]))?"
                          r"|\b\d{1,2}:\d{2}(?!\d)(?:\s*(?:[ap]\.?m\.?))?|\b24/7\b|\b24-hour\b|\b(?:" + _Q_DAYS + r")\s+\d{1,2}(?::\d{2})?\s*[–—-]\s*\d{1,2}(?::\d{2})?\b")),
    ("a year", re.compile(r"(?i:\b(?:in|by|since|from|until|through|during|before|after|for)\s+)(?P<v>(?:18|19|20)\d{2})(?![\d%,]|\.\d|-\d)"   # IC-3
                          r"|(?<![\d$.,/-])(?:18|19|20)\d{2}(?![\d%]|[.,/]\d|-\d)(?!\s+(?:" + "|".join(sorted(_Q_COUNTED, key=len, reverse=True))
                          + r")\b)")),     # (review a #4): IC-2's cue — "In 2024 families waited" is a year; "of" left out ("a total of 2024 calls")
    ("a ZIP", re.compile(r"(?i)(?:\bzip(?:s|\s+codes?)?:?\s+(?:\d{5}(?:\s*(?:,|and|or)\s*))*\d{5}\b)|\b\d{5}(?=\s*(?:,|and|or)\s*\d{5}\b)|(?<=,\s)\d{5}\b(?![\d,])|\b\d{5}(?=\s+(?:area|neighborhood)\b)")),
    ("a phone number", re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\b1-8\d\d-\d{3}-\d{4}\b|\b9-1-1\b")),
    ("a year range, a fiscal year or a quarter", re.compile(r"(?<![\d$.,])(?:FY\s?)?(?:19|20)\d{2}\s*[–—-]\s*(?:(?:19|20)\d{2}|\d{2})(?![\d%])|\bFY\s?\d{2,4}\b|\bQ[1-4]\b|\bH[12]\b")),
    ("an address", re.compile(r"\b\d{2,5}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Pl|Place|Park|Pkwy|Ct|Court)\b\.?")),
    ("a street name", re.compile(r"\b\d{1,3}(?:st|nd|rd|th)\s+(?:Street|St|Avenue|Ave|Terrace|Ter)\b")),
    ("a numbered name", re.compile(r"(?m)^\s*\d{1,4}\s+-\s+(?=[A-Z]|\d{1,3}(?:st|nd|rd|th)\b)")),
    ("a unit type", re.compile(r"\b\d-(?:bedroom|bed|bath|br|BR)\b")),
    # (ledger 14) an IP address; three or more digit groups joined by dashes ("+1-222-2222-2222": a range has two ends)
    ("a code or id", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b(?!\.\d)|(?<![\w.+-])\+?\d+(?:-\d+){2,}(?![\d%-]|\.\d)"
                                r"|\b501\(c\)\(\d\)|\b4\d\d\([a-z]\)|\bSOC\s+\d{2}-\d{4}\b|\b\d+\s+(?:CFR|U\.S\.C\.)\s+[\d.()a-z§-]+|#\d+(?:-\d+)*(?![\d%]|\.\d)"
                                r"|(?:\bCFR|\bU\.S\.C\.)\s+§?\s*\d+[\d.()a-z-]*"     # IC-3 (review a #4): IC-2's "CFR 578.3", "U.S.C. 11302"
                                r"|\b(?![Yy]ears?-|[Pp]hases?-|[Aa]ges?-|[Dd]ays?-|[Tt]iers?-|[Ss]teps?-|[Zz]ones?-|[Ww]eeks?-|[Mm]onths?-)(?!(?i:to|in|of|by|under|over|out|per|and|or|up)-)"
                                r"[A-Za-z][A-Za-z0-9]*-[A-Za-z]{0,3}\d+(?:-\d+)*(?![\d%]|\.\d)(?:[A-Za-z]+\b)?"
                                r"|\b[A-Z][A-Za-z]*\d+(?:\.\d+)?[A-Za-z0-9]*\b|\b[A-Za-z]+\d+[A-Za-z]+[A-Za-z0-9]*\b|\bK-12\b")),
    # BUILD IC-3 Phase 4 (the v2 labels, tests/fixtures/quantity_labels_v2_forms.json): an identifier's digits — a word with an underscore
    # and a digit ("ops_cost_y1", "block1_analysis"), or one lowercase letter joined to digits ("b1", "y1", "u2014"; never "x2", a multiplier)
    ("an identifier (a field's, a tag's or an id's name)", re.compile(r"(?<![\w])(?=\w*_)(?=\w*\d)[A-Za-z_]\w*(?![\w])"
                                                                      r"|(?<![\w#$.,-])[a-wyz]\d{1,6}(?![\w.,%])")),
    # a "#" with three to eight hex digits: a colour ("#1e1145") or a document's number ("#311")
    ("a '#' code (a colour, a document's number)", re.compile(r"#[0-9A-Fa-f]{3,8}(?![\w])")),
]
# (ledger step 20) an age's number, at either end of a range, in digits or in words; in digits a whole number with no percent after it
# ("age 17 to 42%": the age is "age 17", the 42% a quantity)
_Q_AGE_N = r"(?:\d{1,3}(?![\d%]|\.\d)|" + _Q_CARD99 + r"\b)"
_Q_AGE = re.compile(r"(?i)\b(?:ages?|aged)(?:\s+|-)" + _Q_AGE_N + r"(?:\s*(?:[–—-]|to|and|or|through)\s*" + _Q_AGE_N
                    + r")?(?:\s+or\s+(?:younger|older|under|over|above|below))?\+?")
_Q_LABEL_N = r"(?:\d{1,2}[a-z]?|" + _Q_CARD99 + r")\b(?!\s*%|[.,]\d|\s*percent\b)"   # (ledger step 20) a label's number: to 99 in digits and in words alike;
#                                                                  IC-3 (review a #9): never a percent's, a decimal's or a thousands group's first digits;
#                                                                  (ledger step 22) with a letter ("Phase 2b", "Tier 1a" — read 2e9, 1 a.m.)
_Q_LABEL = re.compile(r"\b(?:" + "|".join(_Q_LABEL_HEADS) + r")(?:\s+|-)" + _Q_LABEL_N
                      + r"(?:\s*(?:,|and|or|to|through|–|—|-)\s*(?:and\s+)?" + _Q_LABEL_N + r")*", re.I)
# (ledger 14) an exponent: listed as undecided, never read into two numbers ("1e10" had given a code "1e" and a quantity "10")
_Q_EXP = re.compile(r"(?<![\w.])[-−]?\d+(?:\.\d+)?(?:[eE][+-]?\d+\b|\^[-+]*\d+|\s*[x×]\s*10\^[-+]*\d+)")
_Q_RUNIN_GAP = 400        # IC-3 (review a #2): the most characters between two markers of one run-in list
_Q_RUNIN = re.compile(r"(?<![\w.])\((\d{1,2})\)|(?<![\w.(])(\d{1,2})\)(?=\s)")
_Q_NAMEHEAD = re.compile(r"\b(?:" + "|".join(_Q_NAME_HEADS) + r")\s+\d+[A-Za-z]?\b")
# (ledger step 22) a three-digit service line — the set _NF_SERVICE holds (N11 and 988) — after a verb that reaches it ("Call 988 or 911",
# "dial 211") or before a line's noun ("the 988 line"): a phone (D1 roles: name), not a count
# BUILD IC-5 (D6): a date range written with a word — "June 1 to 21", "August 1 through 29" — one date span (_q_structure checks the end
# is above the start, at most 31, with no counted thing after it); Spanish dates — "del 1 al 21 de junio", "1 de junio de 2026"
_Q_DATE_TO = re.compile(r"\b(?:" + "|".join(_Q_MONTHS) + r")\.?\s+(?P<a>\d{1,2})(?:st|nd|rd|th)?\s+(?:to|through|thru|until|till)\s+"
                        r"(?P<b>\d{1,2})(?:st|nd|rd|th)?(?![\d%]|[.,]\d)(?:,\s*\d{4})?")
_Q_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "setiembre", "octubre", "noviembre",
            "diciembre")
_Q_FECHA = re.compile(r"(?i)\bdel\s+(?P<a>\d{1,2})\s+al\s+(?P<b>\d{1,2})\s+de\s+(?:" + "|".join(_Q_MESES) + r")\b(?:\s+del?\s+\d{4})?"
                      r"|\b(?P<d>\d{1,2})\s+de\s+(?:" + "|".join(_Q_MESES) + r")\b(?:\s+del?\s+\d{4})?")
# BUILD IC-5 (D6): inside lang="es" a number with "." or "," between digit groups is one span whose meaning a locale decides (CLDR es
# writes 1.000 and 2,5; es-419 1,000 and 2.5) — never read as two numbers or as 1.0
_Q_SEP_ES = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d]|[.,]\d)|(?<![\d.,])\d+,\d+(?![\d]|[.,]\d)")
# BUILD IC-5 (D6): the bounded Spanish lexicon, read inside lang="es" only ("un" and "una" are English "a": never read alone)
_Q_ES_U = {"uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
           "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18,
           "diecinueve": 19, "veinte": 20, "veintiuno": 21, "veintidós": 22, "veintidos": 22, "veintitrés": 23, "veintitres": 23,
           "veinticuatro": 24, "veinticinco": 25, "veintiséis": 26, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
           "veintinueve": 29}
_Q_ES_T = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
_Q_ES_U_RX = "|".join(sorted(_Q_ES_U, key=len, reverse=True))
_Q_ES_99 = (r"(?:(?:" + "|".join(_Q_ES_T) + r")(?:\s+y\s+(?:uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve))?|" + _Q_ES_U_RX + r")")
_Q_ES_999 = r"(?:cien|ciento(?:\s+" + _Q_ES_99 + r")?|" + _Q_ES_99 + r")"
_Q_ES_NUM = re.compile(r"(?i)\b(?:(?:" + _Q_ES_999 + r"\s+)?mil(?:\s+" + _Q_ES_999 + r")?|" + _Q_ES_999 + r")\b")


def _es_value(words):
    """BUILD IC-5 (D6): a cardinal in Spanish words → its value ("treinta y dos" 32, "ciento veinte" 120, "dos mil" 2000)."""
    total, part = 0, 0
    for w in words.lower().split():
        if w == "mil":
            total, part = total + (part or 1) * 1000, 0
        elif w in ("cien", "ciento"):
            part += 100
        elif w in _Q_ES_T:
            part += _Q_ES_T[w]
        elif w in _Q_ES_U:
            part += _Q_ES_U[w]
    return total + part


def _in_es(s, e, es):
    return any(a <= s and e <= b for a, b in es or ())


_Q_SVC = r"(?<![\d$.,])(?:[2-9]11|988)(?![\d%]|[.,]\d)"
_Q_SERVICE = re.compile(r"(?i)\b(?:call|calls|called|calling|dial|dials|dialed|dialing|text|texts|texted|texting|contact|reach|via"
                        r"|llame\s+al|llama\s+al|llamar\s+al|marque(?:\s+el)?|marca(?:\s+el)?|marcar(?:\s+el)?)\s+"    # BUILD IC-5 (D6)
                        r"(?:the\s+)?(" + _Q_SVC + r"(?:\s*(?:,|or|and|/)\s*" + _Q_SVC + r")*)"
                        r"|(" + _Q_SVC + r")(?=\s+(?:line|lifeline|hotline|helpline|crisis|operator|dispatch|referrals?|call\s+cent(?:er|re))\b)")


_Q_LINE_ENUM = re.compile(r"(?m)^[ \t]*(\d{1,3})\.(?=[ \t]+\S)")     # BUILD IC-3 Phase 4: a line's "N." (_q_structure)


def _q_structure(text, names, es=()):
    """[(start, end, role, rule)] — the stretches that are names or structure by pattern, read first. `names` is an object with spans(text)
    (the run's own names, ids and ZIPs from its data) or None."""
    out = []
    for why, rx in _QN:              # IC-3 (review a #3, #4): a pattern's group "v", when it took part, is the item — its cue word is context
        out += [((m.start("v"), m.end("v")) if "v" in rx.groupindex and m.group("v") is not None else (m.start(), m.end())) + ("name", why)
                for m in rx.finditer(text)
                if not (why == "a time" and re.fullmatch(r"\d{1,2}:\d{2}", m.group(0)) and re.search(r"(?i)\bratio", text[max(0, m.start() - 16):m.end() + 16]))]
    out += [(m.start(), m.end(), "undecided", "an exponent (not read)") for m in _Q_EXP.finditer(text)]
    low = text.lower()
    for ph in _Q_VAGUE:                                                   # listed, never valued (D1 "vague")
        for m in re.finditer(r"(?<![\w-])" + re.escape(ph) + r"(?![\w-])", low):
            if ph == "most" and re.search(r"\bat\s+$", low[max(0, m.start() - 4):m.start()]):
                continue
            out.append((m.start(), m.end(), "vague", f"a vague quantity (\"{ph}\")"))
    out += [(m.start(), m.end(), "name", "an age after the word age") for m in _Q_AGE.finditer(text)]
    # BUILD IC-5 (D6): a date range's end belongs to its date; Spanish dates; inside lang="es" a separator a locale decides
    for m in _Q_DATE_TO.finditer(text):
        if int(m.group("a")) < int(m.group("b")) <= 31 and not re.match(r"(?i)\s+(?:" + "|".join(sorted(_Q_COUNTED | _Q_TIME_UNITS, key=len, reverse=True))
                                                                       + r")\b", text[m.end():m.end() + 40]):
            out.append((m.start(), m.end(), "name", "a date range (Month D to D)"))
    for m in _Q_FECHA.finditer(text):
        if m.group("d") is not None or int(m.group("a")) < int(m.group("b")) <= 31:
            out.append((m.start(), m.end(), "name", "a date in Spanish (del D al D de mes, D de mes)"))
    for a_, b_ in es or ():
        out += [(m.start(), m.end(), "undecided", "separator unclear (a number with '.' or ',' between digits in Spanish text: CLDR es and "
                 "es-419 write them opposite ways)") for m in _Q_SEP_ES.finditer(text, a_, b_)]
    for m in _Q_SERVICE.finditer(text):                                     # (ledger step 22) each service line of the phrase
        g = 1 if m.group(1) else 2
        out += [(m.start(g) + n.start(), m.start(g) + n.end(), "name", "a phone's service line (211 … 988)") for n in re.finditer(_Q_SVC, m.group(g))]
    out += [(m.start(), m.end(), "name", "a name with a number") for m in _Q_NAMEHEAD.finditer(text)
            if not re.match(r"(?i)(?:type|unit|gate)\s", m.group(0)) or text[m.start():m.start() + 1].isupper()]
    if names is not None:                    # the run's own names, ids and ZIPs (Exempter.name_spans: its patterns are this reader's)
        sp = names.name_spans(text)
        out += [(a, b, "name", why) for a, b, why in sp if not re.search(r"[%$]", text[a:b])]
    # labels: the label word and every number joined to it — a number followed by a counted thing leaves the label ("in Year 2 and 3 more staff")
    for m in _Q_LABEL.finditer(text):
        s, e = m.start(), m.end()
        parts = list(re.finditer(r"\b" + _Q_LABEL_N, m.group(0), re.I))
        keep_end = e
        head = m.group(0)
        first_after = text[s + parts[0].end():s + parts[0].end() + 24] if parts else ""
        if head[:1].islower() and re.match(r"\s+[a-z]", first_after) and not re.match(r"\s+(?:and|or|to|through)\b", first_after):
            continue                                                   # "tracks four bottlenecks": a verb and a count, no label
        for p in parts[1:]:
            after = text[s + p.end():s + p.end() + 24]
            mm = re.match(r"\s+(more|additional|extra|new|other|FTEs?|" + "|".join(sorted(_Q_COUNTED, key=len, reverse=True)) + r"|[a-z]{3,}s)\b", after)
            # IC-3 (review a #9): a counted noun of the reader's own list ends the label too ("Year 2 and 3 staff", "and 40 youth")
            if mm and mm.group(1) not in ("was", "has", "does", "this", "thus", "its", "his", "hers", "ours", "theirs", "yes", "plus", "less", "unless"):
                keep_end = s + p.start()
                break
        kept = [p for p in parts if s + p.end() <= keep_end]
        out.append((s, s + kept[-1].end() if kept else keep_end, "marker", "a label's number"))
    # run-in list markers: "(1) … (2) …" or "1) … 2) …" in one text, from 1, each one more than the last, at least two
    # IC-3 (review a #2): every such list of the text, each kept to one stretch — a marker more than _Q_RUNIN_GAP characters after the
    # last one starts no item of it ("A score of (1) is low. … Next section: (2) staff are hired" stays as it reads); the first version
    # kept only the text's longest list, so a second list's markers counted and one list chained into another section's numbers
    ms = [(m.start(), m.end(), int(m.group(1) or m.group(2))) for m in _Q_RUNIN.finditer(text)]
    seqs, seq = [], []
    for st, en, v in ms:
        if v == 1:
            seq = [(st, en)]
            seqs.append(seq)
        elif seq and v == len(seq) + 1 and st - seq[-1][1] <= _Q_RUNIN_GAP:
            seq.append((st, en))
    for sq in seqs:
        if len(sq) >= 2:
            out += [(st, en, "marker", "a run-in list marker") for st, en in sq]
    # BUILD IC-3 Phase 4 (the v2 labels): a numbered list's item numbers — "N." at a line's start, a list from 1, each one more than the
    # last, at least two (every such list of the text; one "N." line alone stays what the reader reads)
    lists, cur = [], None
    for m in _Q_LINE_ENUM.finditer(text):
        v = int(m.group(1))
        if v == 1:
            cur = [(m.start(1), m.end(1))]
            lists.append(cur)
        elif cur is not None and v == len(cur) + 1:
            cur.append((m.start(1), m.end(1)))
    for lst in lists:
        if len(lst) >= 2:
            out += [(st, en, "marker", "a numbered list's item number") for st, en in lst]
    return out


def quantities(text, fixed=(), names=None, es=()):
    """BUILD IC-3 (D1): every numeric expression of `text` — [{start, end, words, shape, value, lo, hi, num, den, op, hedge, unit, per,
    per_value, role, rule, kind, fixed, money, pct}], in order. `fixed`: (start, end) spans that are cited figures (a registry span on a
    page, a masked token in a label source): each is one piece, never read inside, and an expression may be framed around it ("Nine of
    the [10] facilities", "[14] per 10,000 residents"). Roles: quantity, marker, name, position, idiom, vague, undecided; a per part read
    after a fixed span has role "per_part" (the caller decides whether that figure's own denominator is the same). Never raises."""
    if not isinstance(text, str) or not text:
        return []
    try:
        return _quantities(text, fixed, names, es)
    except Exception as e:  # noqa: BLE001 — the reader never raises: the whole text is one undecided stretch, said with its reason
        return [_new(text, 0, len(text), role="undecided", rule=f"not read: {type(e).__name__}")]


def _overlaps(a, b, spans):
    return any(x < b and a < y for x, y, *_r in spans)


def _quantities(text, fixed, names, es=()):
    struct = _q_structure(text, names, es)
    out, covered = [], []
    for s, e, role, rule in sorted(struct, key=lambda x: (x[0], -(x[1] - x[0]))):
        if e <= s or (covered and s < covered[-1][1]):      # IC-3 (ledger 13): a stretch that overlaps an earlier one at all is dropped whole
            continue                                         # (the earlier start wins, the longer on a tie); covered stays in order, apart
        if role not in ("marker", "vague") and not _Q_HAS_NUM.search(text, s, e):
            continue
        if any(fs < e and s < fe for fs, fe in fixed or ()) and not (role == "marker" or rule == "an age after the word age"):
            continue
        out.append(_new(text, s, e, role=role, rule=rule))
        covered.append((s, e))
    toks = _q_block(_q_tokens(text, fixed), covered)
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.k in ("blk", "sp") or (t.k == "word" and t.lw not in _Q_START) or (t.k == "sym" and t.t not in _Q_START_SYM):
            i += 1
            continue
        got = _read_at(text, toks, i, fixed)
        if got is None:
            i += 1
            continue
        x, j = got
        out.append(x)
        i = max(i + 1, j)
    if es:      # BUILD IC-5 (D6): inside lang="es" the English number words are not read; the Spanish lexicon is
        out = [d for d in out if not (d["role"] is None and _in_es(d["start"], d["end"], es) and not re.search(r"\d", d["words"]))]
        for a_, b_ in es:
            for m in _Q_ES_NUM.finditer(text, a_, b_):
                if _overlaps(m.start(), m.end(), covered) or any(fs < m.end() and m.start() < fe for fs, fe in fixed or ()):
                    continue
                w_ = m.group(0)
                if w_.lower().split()[0] in ("cien", "ciento") and re.search(r"(?i)\bpor\s+$", text[max(0, m.start() - 8):m.start()]):
                    continue            # "por ciento": the percent sign's words, no hundred
                if w_.lower() == "uno" and re.match(r"(?i)\s+de\b", text[m.end():m.end() + 6]):
                    out.append(_new(text, m.start(), m.end(), shape="single", value=1, role="undecided",
                                    rule="uno de (a count or any one of them, as \"one of the\")"))
                    continue
                out.append(_new(text, m.start(), m.end(), shape="single", value=_es_value(w_), role="quantity",
                                rule="a quantity in Spanish words (lang=\"es\")"))
    out.sort(key=lambda d: (d["start"], d["end"]))
    for d in out:
        if d["role"] is None:
            _role(text, d, toks)
    return out


def _q_block(toks, covered):
    """The pieces with each structure stretch made one piece of kind "blk": a frame stops at it, as at the end of the text."""
    if not covered:
        return toks
    cov = sorted(covered)
    out, c = [], 0
    for t in toks:
        while c < len(cov) and cov[c][1] <= t.s:
            c += 1
        if c < len(cov) and cov[c][0] < t.e and t.s < cov[c][1]:
            if out and out[-1].k == "blk" and out[-1].e >= cov[c][0] and out[-1].s >= cov[c][0] - 0 and out[-1].s < cov[c][1]:
                out[-1].e = max(out[-1].e, t.e)
                continue
            out.append(_Tok("blk", t.s, t.e, ""))
            continue
        out.append(t)
    return out


def _atom(text, toks, i, fixed):
    """One composed value at token i: dict(s, e, value, kind, money, pct, ordinal, mult, denom, frac, fig, words) and the next token, or None."""
    t = toks[i]
    if t.k == "fig":
        return {"s": t.s, "e": t.e, "value": None, "fig": True}, i + 1
    if t.k == "vul":
        return {"s": t.s, "e": t.e, "value": _Q_VULGAR[t.t], "frac": True}, i + 1
    if t.k == "sym" and t.t == "$" and i + 1 < len(toks) and toks[i + 1].k == "num":
        a, j = _atom(text, toks, i + 1, fixed)
        a.update(s=t.s, money=True)
        return a, j
    if t.k == "num":
        v = _num_value(t.t)
        a = {"s": t.s, "e": t.e, "value": v}
        j = i + 1
        # a slash fraction "3/4", "1/3" — never a date ("6/30": its second number 13 to 31, or a date word before it)
        if j + 1 < len(toks) and toks[j].t == "/" and toks[j + 1].k == "num" and re.fullmatch(r"\d+", t.t) and re.fullmatch(r"\d+", toks[j + 1].t):
            b = _num_value(toks[j + 1].t)
            if _is_fraction(v, b, text, t.s):
                a.update(e=toks[j + 1].e, value=v / b, frac=True, slash=True)
                return a, j + 2
        k0 = _skip_sp(toks, j)                                             # a mixed number: "2 1/4"
        if k0 > j and k0 + 2 < len(toks) and toks[k0].k == "num" and toks[k0 + 1].t == "/" and toks[k0 + 2].k == "num" and re.fullmatch(r"\d+", t.t):
            n1, d1 = _num_value(toks[k0].t), _num_value(toks[k0 + 2].t)
            if n1 is not None and d1 and n1 < d1 <= 16:
                a.update(e=toks[k0 + 2].e, value=v + n1 / d1, frac=True)
                return a, k0 + 3
        if k0 > j and k0 < len(toks) and toks[k0].k == "word" and toks[k0].t in ("k", "K", "M", "B") and (k0 + 1 >= len(toks) or toks[k0 + 1].k != "word"):
            nxt = text[toks[k0].e:toks[k0].e + 2]
            if not re.match(r"[\w-]", nxt):                                # "2.33 k", "1,674,200.5 M": a magnitude letter apart
                a.update(e=toks[k0].e, value=(v or 0) * _Q_MAG[toks[k0].t if toks[k0].t != "k" else "k"], mag=True)
                return a, k0 + 1
        if j < len(toks) and toks[j].k == "word" and toks[j].lw in _Q_DOZEN:   # "3dz"
            a.update(e=toks[j].e, value=(v or 0) * 12)
            return a, j + 1
        if k0 > j and k0 < len(toks) and toks[k0].k == "word" and toks[k0].lw in _Q_DOZEN and k0 == j + 1:   # "3 dozens", "3 dzs"
            a.update(e=toks[k0].e, value=(v or 0) * 12)
            return a, k0 + 1
        if j < len(toks) and toks[j].k == "word":                        # a letter joined to the digits: 500K, 3x, 21st, 1m, 10k
            w = toks[j].t
            if w in _Q_MAG or (w.lower() in ("k", "bn", "mn") and len(w) <= 2):
                a.update(e=toks[j].e, value=(v or 0) * _Q_MAG.get(w, _Q_MAG.get(w.lower(), 1)), mag=True)
                j += 1
            elif w == "m":
                a.update(e=toks[j].e, undecided="m: million or metre")
                j += 1
            elif w.lower() == "x":
                a.update(e=toks[j].e, mult=True)
                j += 1
            elif w.lower() in ("st", "nd", "rd", "th") and v is not None and float(v).is_integer():
                a.update(e=toks[j].e, ordinal=True)
                j += 1
            elif w.lower() in ("s",) and v is not None and 1000 <= v <= 2100:
                a.update(e=toks[j].e, decade=True)
                j += 1
            elif w.lower() in _Q_TIME_UNITS or w.lower() in ("mi", "lbs", "lb", "ft", "km", "kg"):
                a["joined_unit"] = w.lower()
            else:
                a.update(e=toks[j].e, joined=True)                         # "3B2", "12abc": a code, decided by role
                j += 1
        if j + 1 < len(toks) and toks[j].k == "sp" and toks[j].t in (" ", "\u00a0", "\u202f") and toks[j + 1].k == "sym" and toks[j + 1].t == "%":
            j += 1                                                         # (ledger step 22) "38 %": the sign after one space is its own
        if j < len(toks) and toks[j].k == "sym" and toks[j].t == "%":
            a.update(e=toks[j].e, pct=True)
            j += 1
        else:                                                              # "3.2 million", "30 thousand", "20 percent"
            k = _skip_sp(toks, j)
            if j + 1 < len(toks) and toks[j].k == "sym" and toks[j].t == "-" and toks[j + 1].k == "word" and toks[j + 1].lw in _Q_ROUND:
                a.update(e=toks[j + 1].e, value=(v or 0) * _Q_ROUND[toks[j + 1].lw])
                j = j + 2
            elif k < len(toks) and toks[k].k == "word" and toks[k].lw in _Q_ROUND and k == j + 1:
                a.update(e=toks[k].e, value=(v or 0) * _Q_ROUND[toks[k].lw])
                j = k + 1
                a["value"], j = _more_after_round(toks, j, a, _Q_ROUND[toks[k].lw])
            elif k < len(toks) and toks[k].k == "word" and toks[k].lw in ("percent", "percents", "percentage", "pct") and k == j + 1:
                a.update(e=toks[k].e, pct=True)
                j = k + 1
        return a, j
    if t.k == "word":
        w = t.lw
        if w in ("single", "both"):
            return {"s": t.s, "e": t.e, "value": 1 if w == "single" else 2, "wordnum": True, "quantifier": w}, i + 1
        if w in ("a", "an") or w == "one" or w in _Q_UNITS or w in _Q_TENS or w in _Q_DENOM or w in _Q_MULT_WORDS or w in _Q_ORD or w in _Q_ROUND_PL or w == "dozen":
            return _word_atom(text, toks, i)
    return None


def _word_atom(text, toks, i):
    t = toks[i]
    w = t.lw
    # "a"/"an"/"one" + a denominator, a round word, or "dozen": "a third", "a hundred", "a dozen", "half a million"
    if w in ("a", "an"):
        k = _skip_sp(toks, i + 1)
        if k < len(toks) and toks[k].k == "word" and k == i + 2 and toks[k].lw in _Q_TENS:   # "a twenty-first": a fraction when nothing follows
            c = _compose_words(toks, k)
            if c is not None and c[2] and (c[1] >= len(toks) or not re.match(r"\s+[a-z]", text[toks[c[1] - 1].e:toks[c[1] - 1].e + 3])):
                return {"s": t.s, "e": toks[c[1] - 1].e, "value": 1 / c[0], "frac": True}, c[1]
        if k < len(toks) and toks[k].k == "word" and k == i + 2:
            nw = toks[k].lw
            if nw in _Q_DENOM and nw not in ("half", "halves") and not _next_is(toks, k, "-"):
                return {"s": t.s, "e": toks[k].e, "value": 1 / _Q_DENOM[nw], "frac": True}, k + 1
            if nw in ("half",) and not _next_is(toks, k, "-"):
                return {"s": t.s, "e": toks[k].e, "value": 0.5, "frac": True}, k + 1
            if nw in _Q_ROUND or nw == "dozen":
                v = 12 if nw == "dozen" else _Q_ROUND[nw]
                return {"s": t.s, "e": toks[k].e, "value": v}, k + 1
            if nw == "couple" and _peek_word(toks, k + 1) == "of":
                return None
        return None
    if w in ("half", "quarter"):
        # "half a million", "half a dozen", "half million", "quarter million", "half-time" (a staffing fraction), "half of", "half" alone
        f = 0.5 if w == "half" else 0.25
        k = _skip_sp(toks, i + 1)
        if k + 2 < len(toks) and toks[k].lw == "a" and (toks[_skip_sp(toks, k + 1)].lw in _Q_ROUND or toks[_skip_sp(toks, k + 1)].lw == "dozen"):
            kk = _skip_sp(toks, k + 1)
            return {"s": t.s, "e": toks[kk].e, "value": f * (12 if toks[kk].lw == "dozen" else _Q_ROUND[toks[kk].lw])}, kk + 1
        if k < len(toks) and k == i + 2 and toks[k].lw in _Q_ROUND and toks[k].lw != "hundred":
            return {"s": t.s, "e": toks[k].e, "value": f * _Q_ROUND[toks[k].lw]}, k + 1
        if w == "quarter":
            return {"s": t.s, "e": t.e, "value": None, "denom_only": True}, i + 1
        return {"s": t.s, "e": t.e, "value": 0.5, "frac": True}, i + 1
    if w in _Q_MULT_WORDS:
        return {"s": t.s, "e": t.e, "value": _Q_MULT_WORDS[w], "mult": True, "multword": True}, i + 1
    if w in _Q_ROUND_PL or w == "dozens":
        return {"s": t.s, "e": t.e, "value": None, "vague_plural": True}, i + 1
    if w == "dozen":
        return {"s": t.s, "e": t.e, "value": 12}, i + 1
    if w in _Q_ORD and w not in _Q_UNITS:
        return {"s": t.s, "e": t.e, "value": _Q_ORD[w], "ordinal": True, "ordword": True}, i + 1
    if w in _Q_DENOM:                                                   # "quarters", "thirds" with no numerator: decided by role
        return {"s": t.s, "e": t.e, "value": None, "denom_only": True}, i + 1
    comp = _compose_words(toks, i)
    if comp is None:
        return None
    v, j, ordinal = comp
    a = {"s": t.s, "e": toks[j - 1].e, "value": v, "ordinal": ordinal, "ordword": ordinal, "wordnum": True}
    kp = _skip_sp(toks, j)
    if not ordinal and kp == j + 1 and kp < len(toks) and toks[kp].lw in ("percent", "percents", "percentage"):
        a.update(e=toks[kp].e, pct=True)
        return a, kp + 1
    if not ordinal:
        fw = _fraction_words(toks, i)
        if fw is not None and fw[1] > j and not (toks[fw[1] - 1].lw in ("second", "seconds")):
            nxt = text[toks[fw[1] - 1].e:toks[fw[1] - 1].e + 3]
            if toks[fw[1] - 1].lw.endswith("s") or v == 1 or not re.match(r"\s+[a-z]", nxt):
                a.update(e=toks[fw[1] - 1].e, value=fw[0], frac=True)
                return a, fw[1]
    # "thirty (30) days": a number in words and the same number in digits in parentheses — one expression
    k5 = _skip_sp(toks, j)
    if k5 + 2 < len(toks) and toks[k5].t == "(" and toks[k5 + 1].k == "num" and toks[k5 + 2].t == ")" and _num_value(toks[k5 + 1].t) == v:
        a.update(e=toks[k5 + 2].e)
        return a, k5 + 3
    return a, j


def _next_is(toks, k, sym):
    return k + 1 < len(toks) and toks[k + 1].k == "sym" and toks[k + 1].t == sym


def _peek_word(toks, i):
    k = _skip_sp(toks, i)
    return toks[k].lw if k < len(toks) and toks[k].k == "word" else None


def _read_at(text, toks, i, fixed):
    """The longest expression starting at token i (a hedge or bound word may come first), or None."""
    t = toks[i]
    hedge, op, start_i = None, None, i
    # a hedge or a bound word before a number
    ph = _phrase_at(toks, i, _Q_BH)
    if ph is not None and ph[0] in (">", "<", ">=", "<=", "≥", "≤", "~", "≈") and t.s > 0 and not (text[t.s - 1].isspace() or text[t.s - 1] in "(["):
        ph = None                                                       # "<strong>12", "</b>One", "\"<2–4": a tag or a placeholder, not a bound
    if ph is not None and ph[0] == "about" and (_word_before(toks, i) is not None and toks[_word_before(toks, i)].lw in ("how", "what")):
        ph = None                                                       # (ledger 14) "How about 2 in 5": a suggestion, no hedge
    if ph is not None:
        k = _skip_sp(toks, ph[1])
        if k < len(toks) and (toks[k].k in ("num", "fig", "vul") or (toks[k].k == "sym" and toks[k].t == "$") or
                              (toks[k].k == "sym" and toks[k].t in "-−" and k + 1 < len(toks) and toks[k + 1].k == "num") or (toks[k].k == "word" and _atom(text, toks, k, fixed) is not None)):
            # IC-3 (ledger 14): no guard on "a"/"an" — _atom reads "a dozen", "a hundred", "a third" and nothing else ("a year" is None),
            # and the guard had dropped the bound of "more than a dozen", "fewer than a thousand", "nearly a hundred"
            a0 = _atom(text, toks, k + 1 if toks[k].t in "-−" and toks[k].k == "sym" else k, fixed)
            if a0 is not None:
                if ph[0] in _Q_BOUNDS:
                    op = ph[0]
                else:
                    hedge = ph[0]
                i = k
    ev = i
    if hedge is None and op is None and ph is not None and ph[0] in _Q_HEDGES and _skip_sp(toks, ph[1]) < len(toks) and toks[_skip_sp(toks, ph[1])].lw == "every":
        hedge, ev = ph[0], _skip_sp(toks, ph[1])
    if toks[ev].k == "word" and toks[ev].lw == "every":                 # a recurrence: "every third", "every two weeks", "every 6 months"
        t_ev = toks[ev]
        k = _skip_sp(toks, ev + 1)
        if k < len(toks):
            if toks[k].k == "word" and toks[k].lw == "other":
                return _new(text, t.s, toks[k].e, shape="recurrence", role="quantity", rule="every other", den=2,
                            hedge=hedge and text[t.s:t_ev.s].strip()), k + 1
            a = _atom(text, toks, k, fixed)
            if a is not None and a[0].get("value") is not None:
                at, j = a
                d = _new(text, t.s, at["e"], shape="recurrence", den=at["value"], hedge=(text[t.s:t_ev.s].strip() or None) if hedge else None)
                j = _attach_tail(text, toks, j, d)
                d["role"], d["rule"] = "quantity", "a recurrence (\"every third\")"
                return d, j
    neg_s = None
    if toks[i].k == "sym" and toks[i].t in ("-", "−") and i + 1 < len(toks) and toks[i + 1].k == "num" and (toks[i].s == 0 or text[toks[i].s - 1] in " (=\n\t<>≤≥~≈"):
        # (ledger 14: a bound sign may stand right before the sign, "<-30")
        neg_s, i = toks[i].s, i + 1
    elif toks[i].k == "word" and toks[i].lw in ("minus", "negative"):
        k_ = _skip_sp(toks, i + 1)
        if k_ == i + 2 and k_ < len(toks) and (toks[k_].k == "num" or toks[k_].lw in _Q_UNITS or toks[k_].lw in _Q_TENS):
            a_ = _atom(text, toks, k_, fixed)
            nw = re.match(r"\s+([a-z]+)", text[toks[a_[1] - 1].e:toks[a_[1] - 1].e + 20]) if a_ else None
            pw = re.search(r"([A-Za-z]+)\W*$", text[max(0, toks[i].s - 24):toks[i].s])
            if toks[i].lw == "minus" or nw is None or nw.group(1) in ("over", "point", "and", "or", "percent") or (pw is None or pw.group(1).lower() in _Q_FUNC | {"equals", "be"}):
                neg_s, i = toks[i].s, k_                                # "negative 3 consecutive years": an adjective, no sign
    a = _atom(text, toks, i, fixed)
    if a is None:
        return None
    at, j = a
    if neg_s is not None:
        at = dict(at, s=neg_s, value=(-at["value"] if at.get("value") is not None else None), neg=True)
    s0 = toks[start_i].s if (hedge or op) else at["s"]
    d = _new(text, s0, at["e"], parts=[[at["s"], at["e"]]])
    if hedge:
        d["hedge"] = text[toks[start_i].s:toks[_skip_sp(toks, start_i)].s if False else toks[start_i].s + len(hedge)]
        d["hedge"] = text[s0:at["s"]].strip()
    if op:
        d["op"] = text[s0:at["s"]].strip()
    _fill(d, at)
    # a range: X (-|–|—|to|through) Y, "between X and Y", "from X to Y"; a ratio: X in Y, X out of Y, X of [the] Y
    k = j
    sep = None
    if k < len(toks) and toks[k].k == "sym" and toks[k].t in ("-", "–", "—", "~"):
        sep, k2 = toks[k].t, k + 1
        # IC-3 (ledger step 22): a frame word joined by hyphens frames as it does between spaces — "30-to-40 percent", "one-in-five",
        # "1-in-3", "two-out-of-three" (D1: "A hyphen does not make a word a name"); "one-to-one" is on the idiom list
        if toks[k].t == "-" and k + 2 < len(toks) and toks[k + 1].k == "word" and toks[k + 1].lw in ("to", "through", "in", "of") \
                and toks[k + 2].k == "sym" and toks[k + 2].t == "-":
            sep, k2 = toks[k + 1].lw, k + 3
        elif toks[k].t == "-" and k + 4 < len(toks) and toks[k + 1].lw == "out" and toks[k + 2].t == "-" and toks[k + 3].lw == "of" \
                and toks[k + 4].t == "-":
            sep, k2 = "out of", k + 5
        k2 = _skip_sp(toks, k2)
    else:
        k1 = _skip_sp(toks, k)
        if k1 < len(toks) and toks[k1].k == "sym" and toks[k1].t in ("–", "—", "-") and k1 > k:
            sep, k2 = toks[k1].t, _skip_sp(toks, k1 + 1)
        elif k1 < len(toks) and toks[k1].k == "word" and toks[k1].lw in ("to", "through", "thru", "and", "or", "in", "of", "over") and k1 > k:
            sep, k2 = toks[k1].lw, _skip_sp(toks, k1 + 1)
            if sep == "out":
                pass
        elif k1 + 2 < len(toks) and toks[k1].lw == "out" and _peek_word(toks, k1 + 1) == "of":
            sep, k2 = "out of", _skip_sp(toks, _skip_sp(toks, k1 + 1) + 1)
        else:
            k2 = None
    if sep is None and k < len(toks) and toks[k].k == "sym" and toks[k].t == ":" and k + 1 < len(toks) and toks[k + 1].k == "num":
        sep, k2 = ":", k + 1                                            # "a 1:15 staff ratio" (a time was read first, by its pattern)
    if sep is not None and k2 is not None and k2 < len(toks):
        kk = k2
        if sep == "of" and toks[kk].lw == "the":
            kk = _skip_sp(toks, kk + 1)
        b = _atom(text, toks, kk, fixed) if kk < len(toks) else None
        if b is not None:
            bt, jb = b
            kb = _word_before(toks, start_i) if sep == "and" else None   # IC-3 (ledger step 12): "X and Y" is a range only after
            ok = (sep != "and" or (kb is not None and toks[kb].lw == "between")) and _frame_ok(text, toks, sep, at, bt, jb, d)   # "between"
            if ok:
                shape = "ratio" if sep in ("in", "of", "out of", "over", ":") else "range"
                if sep == "and":                                        # the frame word is the range's own: without it "20 and 30"
                    d.update(start=toks[kb].s)                          # reads as two counts
                d.update(end=bt["e"], words=text[d["start"]:bt["e"]], shape=shape)
                if shape == "range":
                    d.update(lo=at.get("value"), hi=bt.get("value"), value=None)
                    if bt.get("pct"):
                        d["pct"] = True
                    if bt.get("money") or at.get("money"):
                        d["money"] = True
                else:
                    d.update(num=at.get("value"), den=bt.get("value"), value=None)
                if bt.get("fig"):
                    d["fixed"].append([bt["s"], bt["e"]])
                d["parts"].append([bt["s"], bt["e"]])
                j = jb
    if d["shape"] == "range":                                          # "1-2 of the 16 units": a range of a whole
        k7 = _skip_sp(toks, j)
        if k7 < len(toks) and toks[k7].lw == "of" and k7 > j:
            k8 = _skip_sp(toks, k7 + 1)
            if k8 < len(toks) and toks[k8].lw == "the":
                k8 = _skip_sp(toks, k8 + 1)
            c_ = _atom(text, toks, k8, fixed) if k8 < len(toks) else None
            if c_ is not None and (c_[0].get("fig") or (c_[0].get("value") is not None and d["hi"] is not None and c_[0]["value"] >= d["hi"])):
                d.update(end=c_[0]["e"], words=text[d["start"]:c_[0]["e"]], shape="ratio", num=None, den=c_[0].get("value"))
                d["parts"].append([c_[0]["s"], c_[0]["e"]])
                j = c_[1]
    if op or at.get("plus") or d.get("op"):
        d["shape"] = "bound" if d["shape"] in (None, "single") else d["shape"]
    # a bound after the number: "150+", "65+", "or more", "and over", "or younger"
    # (ledger 14) a bound after a ratio or a range keeps that shape ("2 in 5 or more" is a ratio with its bound, as "more than 2 in 5" is)
    if j < len(toks) and toks[j].k == "sym" and toks[j].t == "+" and (j + 1 >= len(toks) or toks[j + 1].k != "num"):
        d.update(end=toks[j].e, words=text[d["start"]:toks[j].e], shape="bound" if d["shape"] in (None, "single") else d["shape"], op="+")
        j += 1
    else:
        ph2 = _phrase_at(toks, _skip_sp(toks, j), _Q_BOUND_SUFFIX) if j < len(toks) and not d.get("op") else None   # (ledger 14: one bound —
        # "larger than twenty and less or equal than thirty five" had read "larger than twenty and less" as one expression)
        if ph2 is not None and _skip_sp(toks, j) == j + 1 and not re.match(r"\s+than\b", text[toks[ph2[1] - 1].e:toks[ph2[1] - 1].e + 6]) \
                and not (ph2[0] == "plus" and re.match(r"\s+(?:\d|[a-z]+\b)", text[toks[ph2[1] - 1].e:toks[ph2[1] - 1].e + 12]) and
                         re.match(r"\s+(?:\d|" + _Q_WORDS_N + ")", text[toks[ph2[1] - 1].e:toks[ph2[1] - 1].e + 24])):   # (ledger step 20)
            d.update(end=toks[ph2[1] - 1].e, words=text[d["start"]:toks[ph2[1] - 1].e], shape="bound" if d["shape"] in (None, "single") else d["shape"], op=ph2[0])
            j = ph2[1]
    if d["shape"] is None:
        d["shape"] = "multiplier" if at.get("mult") else "single"
    if at.get("mult") or d["shape"] == "multiplier":
        d["shape"] = "multiplier" if d["shape"] in ("single", "multiplier") else d["shape"]
    # "N times" after a number: a multiplier ("2.5 times the cost")
    w2, k6 = _word_at(toks, j)
    if w2 == "times" and d["shape"] == "single" and k6 == j + 1 and not at.get("ordinal"):
        d.update(end=toks[k6].e, words=text[d["start"]:toks[k6].e], shape="multiplier")
        j = k6 + 1
    # (ledger step 22) "3-fold", "ten-fold": a multiplier, as "tenfold" and "3x" ("3" had been read as a single 3)
    if d["shape"] == "single" and j + 1 < len(toks) and toks[j].k == "sym" and toks[j].t == "-" and toks[j + 1].k == "word" \
            and toks[j + 1].lw == "fold" and not at.get("ordinal"):
        d.update(end=toks[j + 1].e, words=text[d["start"]:toks[j + 1].e], shape="multiplier")
        j += 2
    if op == "over":
        u, ku = _word_at(toks, j)
        if u in _Q_TIME_UNITS and not re.match(r"\s+old\b", text[toks[ku].e:toks[ku].e + 6]):   # "over three years": a period, not a bound
            d.update(start=at["s"], words=text[at["s"]:d["end"]], op=None, shape="single" if d["shape"] == "bound" else d["shape"])
    j = _attach_tail(text, toks, j, d)
    if at.get("fig") and d["shape"] == "single" and not d.get("per"):
        return None if not d["hedge"] and not d["op"] else (d, j)
    return d, max(j, i + 1)


def _word_before(toks, i):
    """The index of the word just before token i (spaces skipped), or None when no word stands there ("between" of "between X and Y")."""
    k = i - 1
    while k >= 0 and toks[k].k == "sp":
        k -= 1
    return k if k >= 0 and toks[k].k == "word" else None


def _frame_ok(text, toks, sep, a, b, jb, d):
    """A range or a ratio is read only between two numbers of one kind: "8-12", "80 to 120", "$12-15", "2-5%", "4 in 10", "17 of 87",
    "one in three", "Nine of the [10]"; never "$10000 in 3 years", "12 in six boxes", "8 in 5" (Recognizers' own cases)."""
    if a.get("fig") and b.get("fig"):
        return sep in ("-", "–", "—", "to", "through")
    av, bv = a.get("value"), b.get("value")
    if a.get("ordinal") or b.get("ordinal") or a.get("vague_plural") or b.get("vague_plural"):
        return False
    if a.get("denom_only") or b.get("denom_only"):
        return False
    if sep == ":":
        return av is not None and bv not in (None, 0) and not a.get("money") and not b.get("money")
    if sep == "over":
        nxt, _k = _word_at(toks, jb)
        return (av is not None and bv not in (None, 0) and not (a.get("money") or a.get("pct") or b.get("money") or b.get("pct"))
                and nxt not in _Q_TIME_UNITS and not a.get("ordinal") and not b.get("ordinal"))
    if sep in ("in", "out of", "of"):
        if a.get("money") or a.get("pct") or b.get("money") or b.get("pct") or a.get("mult") or b.get("mult"):
            return False
        nxt, _k = _word_at(toks, jb)
        if nxt in _Q_TIME_UNITS:
            return False
        if sep == "of" and a.get("wordnum") and av == 1:
            k0 = -1
            for k0_ in range(len(toks)):
                if toks[k0_].s == a["s"]:
                    k0 = k0_
                    break
            kb = k0 - 1
            while kb >= 0 and toks[kb].k == "sp":
                kb -= 1
            if kb >= 0 and toks[kb].lw in ("to", "into", "from", "in", "as", "by", "with", "at", "for", "under"):
                return False                                               # "routes each case to one of three tracks"
        if b.get("fig"):
            return sep == "of" and av is not None
        if av is None or bv is None or av > bv or bv == 0:
            return False
        if sep == "of" and av == bv:
            return False
        return True
    if sep == "or":                                                     # "two or three": neighbours only
        if av is None or bv is None:
            return False
        return av < bv and bv - av <= 2 and bv < 100 and (a.get("wordnum") == b.get("wordnum"))
    # "and" comes here only after "between" (the caller checks the word before): a range's own rules below
    # a range: both numbers, the second larger, the same kind (a % or $ may stand on the second only: "2-5%", "$12-15")
    if av is None or bv is None:
        return False
    if a.get("frac") or b.get("frac"):
        return False
    if bv <= av:
        return False
    if sep in ("-", "–", "—") and (a.get("wordnum") or b.get("wordnum")):
        return False
    if sep in ("-", "–", "—") and re.match(r"\s*(?:=|equals\b)", text[b["e"]:b["e"] + 8]):
        return False                                                    # "8 - 12 = −4": a sum
    if a.get("pct") and not b.get("pct"):
        return False
    return True


def _fill(d, at):
    if at.get("fig"):
        d["fixed"].append([at["s"], at["e"]])
    d["value"] = at.get("value")
    d["money"] = bool(at.get("money"))
    d["pct"] = bool(at.get("pct"))
    for k in ("ordinal", "ordword", "mult", "multword", "frac", "vague_plural", "denom_only", "wordnum", "joined", "decade", "undecided",
              "mag", "joined_unit"):
        if at.get(k):
            d["_" + k] = at[k]
    if at.get("pct") and d["value"] is not None:
        d["value"] = d["value"] / 100.0


def _attach_tail(text, toks, j, d):
    """The unit noun and the "per" part beside the expression: "[N] unique contacts per weekday", "$15 per copy", "$234/month",
    "[14] per 10,000 residents". Returns the index after what the expression took — the "/unit" or the per part, with its number — so
    the loop does not read it again (IC-3, ledger step 12: "14 per 10,000 residents" gave a second quantity "10,000"); j when it took
    neither."""
    k = j
    if k < len(toks) and toks[k].k == "sym" and toks[k].t == "/" and k + 1 < len(toks) and toks[k + 1].k == "word" and toks[k + 1].lw not in ("or",):
        d.update(per=toks[k + 1].lw, end=toks[k + 1].e, words=text[d["start"]:toks[k + 1].e])
        return k + 2
    w, kw = _word_at(toks, k)
    if d.get("_joined_unit"):
        d["unit"] = d["_joined_unit"]
    if w is not None and w not in _Q_FUNC and w not in ("per", "times") and kw == k + 1 and not d.get("unit"):
        d["unit"] = w
        k = kw + 1
        if w == "-" or toks[kw].t[:1].isupper() and not toks[kw].t.isupper():
            d["unit"] = None if toks[kw].t[:1].isupper() else w
        w2, kw2 = _word_at(toks, k)
        if w2 not in (None, "per") and w2 not in _Q_FUNC and kw2 == k + 1 and toks[kw2].t[:1].islower() and d["unit"]:
            d["unit2"] = w2
    elif k < len(toks) and toks[k].k == "sym" and toks[k].t == "-" and k + 1 < len(toks) and toks[k + 1].k == "word":
        d["unit"] = toks[k + 1].lw                                     # "six-month", "30-day", "3x-rent", "half-time"
        d["_hyphen_unit"] = True
        k = k + 2
    # the per part: "per <number>? <noun>" directly after the expression or after its unit noun; "per <number>" with no noun after it
    # ("14 per 10,000.") is a per part too — its number is the expression's, never a second quantity
    for kk in (j, k):
        w3, k3 = _word_at(toks, kk)
        if w3 == "per" and k3 == kk + (1 if kk < len(toks) and toks[kk].k == "sp" else 0):
            k4 = _skip_sp(toks, k3 + 1)
            if k4 < len(toks):
                pv = None
                a = _atom(text, toks, k4, ()) if toks[k4].k in ("num", "word") and toks[k4].lw not in ("a", "an") else None
                if a is not None and a[0].get("value") is not None and not a[0].get("ordinal"):
                    pv, k5 = a[0]["value"], _skip_sp(toks, a[1])
                else:
                    k5 = k4
                noun = k5 < len(toks) and toks[k5].k == "word" and (pv is None or toks[k5].lw not in _Q_FUNC)
                if noun or pv is not None:
                    last = k5 if noun else a[1] - 1
                    if kk == j:
                        d.update(end=toks[last].e, words=text[d["start"]:toks[last].e])
                    d.update(per=" ".join(x for x in ((f"{pv:g}" if pv is not None else ""), (toks[k5].lw if noun else "")) if x), per_value=pv,
                             per_span=[toks[k3].s, toks[last].e], per_num_span=([toks[k4].s, toks[a[1] - 1].e] if pv is not None else None))
                    return last + 1
            break
    return j


def _role(text, d, toks):
    """The role, last: name, marker, position, idiom, vague, undecided or quantity — each with the rule that decided it."""
    s, e = d["start"], d["end"]
    words = text[s:e]
    low = words.lower()
    before = text[max(0, s - 40):s]
    after = text[e:e + 40]
    lb = before.lower()
    la = after.lower()
    if d.get("_undecided"):
        d.update(role="undecided", rule=d["_undecided"])
        return
    if d["fixed"] and d["shape"] == "single" and not d.get("per"):
        d.update(role="quantity", rule="a cited figure")
        return
    if d["fixed"] and d.get("per") and any(fs == d["start"] for fs, _fe in d["fixed"]):
        d.update(role="per_part", rule="the per part of a cited figure (its own when the registry gives that figure this denominator)")
        return
    # vague: "dozens of", "hundreds of"
    if d.get("_vague_plural"):
        if re.match(r"\s+of\b", after) or d["shape"] == "single":
            d.update(role="vague", rule="a vague magnitude (\"hundreds of\")", value=None)
            return
    # idioms: a closed list, by the words around
    ctx = (before[-24:] + words + after[:24]).lower()
    rel = len(before[-24:])
    for m in _Q_IDIOM_RX.finditer(ctx):
        if m.start() <= rel < m.end() or m.start() < rel + len(words) <= m.end():
            d.update(role="idiom", rule=f"an idiom (\"{m.group(0)}\")")
            return
    # a word compound: "one-stop", "documents-first", "single-adult", "zero-income"
    m_h = re.match(r"-([a-z]+)", after.lower())
    if d.get("_ordword") and re.search(r"[a-z]-$", before.lower()) and not re.search(r"(?:" + "|".join(_Q_TENS) + r")-$", before.lower()):
        d.update(role="idiom", rule="a word-hyphen-ordinal compound (\"documents-first\")")
        return
    w0 = low.split()[0] if low.split() else low
    if w0 in ("one", "single", "zero") and m_h and m_h.group(1) not in _Q_TIME_UNITS and not d.get("hedge") and not d.get("op"):
        d.update(role="idiom", rule=f"a {w0}-<word> compound (\"{w0}-{m_h.group(1)}\")")
        return
    if w0 == "single" and (re.match(r"\s+(?:[a-z]+-)?(?:" + "|".join(_Q_HOUSEHOLD) + r")\b", la) or m_h and m_h.group(1) in _Q_HOUSEHOLD):
        d.update(role="idiom", rule="a household type (\"single adults\")")
        return
    # ordinals
    if d.get("_ordinal"):
        _ordinal_role(text, d, before, after)
        return
    if d.get("_denom_only"):
        if re.search(r"\b(?:per|each|this|last|next|every|a|the|first|second|third|fourth|fiscal)\s+$", lb) or re.match(r"\s*(?:of|ly)\b", la) is None:
            d.update(role="idiom", rule="a period (\"per quarter\")" if "quarter" in low else "a denominator with no number")
            return
    if d.get("_decade"):
        d.update(role="name", rule="a decade")
        return
    if d.get("_joined"):
        d.update(role="name", rule="a code (digits joined to letters)")
        return
    w = low
    # "one": a pronoun, "the one", "one of"
    if w == "one" and not d.get("hedge") and not d.get("op") and d["shape"] == "single":
        r = _one_role(text, d, before, after)
        if r:
            d.update(role=r[0], rule=r[1])
            return
    if w in ("both",):      # BUILD IC-6 (D11 d): "both" names the two the text has named — before a noun ("Both navigators", IC-5 D6's
        #                     undecided anaphoric count), alone ("Both are partial", IC-4's frozen label d0037) or before "of": never a new count
        d.update(role="idiom", rule="an anaphoric count (\"both\": the two the text names — no new count)")
        return
    if w == "single":
        if re.match(r"\s+(?:[a-z]+\s+)?[a-z]{3,}", la) and not re.match(r"\s+(?:out|handedly|of|and|or|in|to)\b", la):
            d.update(role="quantity", rule="single, before a counted thing (one)")
            return
        d.update(role="undecided", rule="single, with no counted thing after it")
        return
    # a capitalized number word inside a proper name: "Second Light", "Housing First" (handled for ordinals above)
    # an anaphoric count: "the two figures", "the two are"
    if re.search(r"\bthe\s+$", lb) and w in ("two", "three", "both") and re.match(r"\s+(?:" + "|".join(sorted(_Q_REF_NOUNS, key=len, reverse=True)) + r")\b", la):
        d.update(role="idiom", rule="an anaphoric count (\"the two figures\": the ones the text names — no new count)")   # BUILD IC-6 (D11 d):
        return                                                                          # IC-4's frozen label d0036; IC-3 read it undecided
    # (ledger step 22) every form of double, triple, quadruple and halve ("costs tripled"); "twice", "thrice" and the "-fold" words always multiply
    if d.get("_multword") and w in _Q_MULT_WORDS and w not in ("twice", "thrice") and not w.endswith("fold") \
            and not re.match(r"\s+(?:the|its|their|our|in|from|to)\b|-", after.lower()):
        d.update(role="undecided", rule=f"{w}: a multiplier only before what it multiplies")
        return
    if w == "half" and d["shape"] == "single":
        if re.match(r"-(?:time|day|year|hour)\b", la) or re.match(r"\s+(?:of|a|an|the)\b", la) or d.get("op") or d.get("hedge"):
            d.update(role="quantity", rule="a fraction")
            return
        d.update(role="undecided", rule="half, with nothing it is half of")
        return
    d.update(role="quantity", rule="a quantity")


def _ordinal_role(text, d, before, after):
    w = d["words"].lower()
    lb, la = before.lower(), after.lower()
    if re.match(r"\s+(?:Street|St|Avenue|Ave|Terrace|Floor)\b", after) and not d.get("_ordword"):
        d.update(role="name", rule="a street or floor name")
        return
    if re.match(r"[\s-]+(?:percentiles?|centiles?)\b", la):            # (ledger step 22) "the 90th percentile": a point of a distribution,
        d.update(role="quantity", rule="a percentile (a point of a distribution)")   # a measure (D1: an ordinal is not an exemption)
        return
    if d.get("_ordword"):
        if re.match(r"\s+(?:to|from)\s+(?:the\s+)?last\b", la):        # (ledger 14) "the second to last": counted from the end, no number
            d.update(role="idiom", rule="a position counted from the end (\"the second to last\")")
            return
        cap = d["words"][:1].isupper()
        sent_start = not before.strip() or re.search(r"[.!?:;]\s*$|<[^>]*>\s*$|\n\s*$", before) is not None
        nxt = re.match(r"\s+([A-Za-z][\w']*)", after)
        prv = re.search(r"([A-Za-z][\w']*)\s+$", before)
        if cap and nxt and nxt.group(1)[:1].isupper() and not nxt.group(1).isupper() is None:
            pass
        if cap and nxt and nxt.group(1)[:1].isupper() and (not sent_start or True) and nxt.group(1).lower() not in _Q_FUNC:
            d.update(role="name", rule="a number word in a proper name (\"Second Light\")")
            return
        if cap and prv and prv.group(1)[:1].isupper() and not sent_start:
            d.update(role="name", rule="a number word in a proper name (\"Housing First\")")
            return
        if re.match(r"\s*,", after):
            if sent_start:
                d.update(role="marker", rule="a run-in enumerator in words (\"First, …\")")
            else:
                d.update(role="position", rule="an enumerator in words (\"and third, …\")")
            return
        if not re.search(r"\b(?:the|a|an|its|their|his|her|our|your|this|that|each|every|any|no|whose)\s+$", lb) and re.match(r"\s+[a-z]{2,}", after) \
                and not re.match(r"\s+(?:of)\b", la) and w == "first":
            d.update(role="idiom", rule="an ordinal used as an adverb (\"who first sees\")")
            return
        if w in ("second", "seconds") and re.search(r"\d\s*$", before):
            d.update(role="quantity", rule="seconds (a unit of time)")
            return
        if re.match(r"\s+(?:time|times)\b", la) or re.match(r"\s+(?:contact|sight|glance|place|hand)\b", la):
            d.update(role="idiom", rule="an ordinal in a set phrase (\"first contact\")")
            return
        if re.match(r"\s+(?:⟦⟧\s+)?(?:\d+\s+|(?:few|" + _Q_WORDS_N + r")\s+)?(?:days?|weeks?|months?|years?|quarters?|hours?|decades?)\b", la):   # (ledger 20)
            d.update(role="idiom", rule="an ordinal time window (\"the first 90 days\")")
            return
        if not re.match(r"\s+[a-z]", after) or re.match(r"\s+(?:to|in|on|at|by|for|from|with|and|or|is|are|was|were|of)\b", la):
            if re.search(r"\b(?:the|a|an)\s+$", lb):
                d.update(role="position", rule="an ordinal that orders")
                return
            d.update(role="idiom", rule="an ordinal used as an adverb (\"comes first\")")
            return
        d.update(role="position", rule="an ordinal that orders")
        return
    d.update(role="position", rule="an ordinal that orders (\"3rd\")")


_Q_ONE_ADJ = ("next|last|first|second|third|other|same|only|right|wrong|new|old|latest|final|previous|current|original|best|worst|little|"
              "big|small|good|bad|older|newer|bigger|smaller")
_Q_ONE_NOUN = re.compile(r"\s*%|\s+(?:" + "|".join(sorted(_Q_COUNTED | _Q_TIME_UNITS | _Q_HOUSEHOLD | _Q_NUM_WORDS | {"percent", "more", "additional"},
                                                       key=len, reverse=True)) + r")\b")


def _one_role(text, d, before, after):
    lb, la = before.lower(), after.lower()
    if re.search(r"\b(?:the|this|that|which)\s+$", lb) and not re.match(r"\s+(?:[a-z]+\s+)?(?:of|in)\b", la):
        return "idiom", "the one (a pronoun: Recognizers-Text's own filter)"
    # BUILD IC-5 (D6): the Penn Treebank's rule — after a determiner and an adjective "one" heads its noun phrase ("the next one", "a new
    # one"): a pronoun, unless a counted thing, a unit or a number follows ("the next one hundred families")
    if (re.search(r"\b(?:the|this|that|which|each|every|any|another|a|an|no)\s+(?:" + _Q_ONE_ADJ + r")\s+$", lb)
            and not re.match(r"\s+(?:[a-z]+\s+)?(?:of|in)\b", la) and not _Q_ONE_NOUN.match(la)):
        return "idiom", "one as a pronoun (the Penn Treebank's rule: \"the next one\")"
    if re.match(r"\s+of\b", la):
        if re.search(r"\b(?:to|into|from|in|as|by|with|at|for)\s+$", lb):
            return "idiom", "one of (any one of a set)"
        if re.match(r"\s+of\s+(?:them|us)\b|\s+of\s+(?:these|those)\s*(?:[.,;:!?)]|$)", la):     # BUILD IC-6 (D11 d): "one of them"
            return "idiom", "an anaphoric count (\"one of them\": one of the set the text names — no new count)"
        if re.match(r"\s+of\s+(?:the|these|those|its|their|our|his|her|them|us)\b", la):
            return "undecided", "one of the … (a count or any one of them)"
        return None
    # BUILD IC-6 (D11 d): "one" heading a clause with no "that", after a copula — "the default fallback, adult shelters, is one youth
    # frequently refuse" (IC-4's frozen label d0312): a noun, an adverb in -ly, then a verb in its plain form (no -s, -ed or -ing) that agrees
    # with no singular "one <noun>" — one [that] youth refuse: a pronoun. The -ly adverb shows the clause ("is one person only" stays a count)
    if re.search(r"\b(?:is|was|remains|remained|becomes|became)\s+$", lb):
        m_ = re.match(r"\s+[a-z]+\s+(?!only\b)[a-z]{2,}ly\s+([a-z]+)\b", la)
        if m_ and not re.search(r"(?:s|ed|ing)$", m_.group(1)) and m_.group(1) not in _Q_FUNC:
            return "idiom", "one as a pronoun before a clause (\"is one youth frequently refuse\": one [that] youth refuse — no count)"
    if re.match(r"\s*(?:[.,;:!?)]|$)", after) or re.match(r"\s+(?:over|exists|is|was|that|which|who|on|in|at|when|if|but|and|or)\b", la):
        if re.search(r"\b(?:below|above|under|over|than|to|from|of|exceeds?|is|equals?|at)\s+$", lb) or not before.strip():
            return None
        return "idiom", "one as a pronoun (\"hand one over\")"
    return None


def _is_fraction(a, b, text, at):
    """N/M is a fraction when M is 2 to 100, N is below M, and it is no date: a date word or a weekday before it, or M 13 to 31 with N 1 to
    12 ("by 6/30"), makes it one."""
    if a is None or not b or not (2 <= b <= 100) or a >= b:
        return False
    if 1 <= a <= 12 and 13 <= b <= 31:
        return False
    if re.search(r"(?i)\b(?:on|since|until|through|before|after|between|by|due|deadline|(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s+$", text[max(0, at - 24):at]):
        return False
    return True


def _more_after_round(toks, j, a, last_round):
    """After "3 hundred" or "4 thousand": "and 21", "and one", "3 hundred and 21" — the rest of a number written with digits and words."""
    v = a["value"]
    cur_round = last_round
    while True:
        k = _skip_sp(toks, j)
        if k >= len(toks) or k == j:
            break
        if toks[k].lw == "and" and cur_round >= 1000:
            f = _fraction_words(toks, _skip_sp(toks, k + 1))
            if f is not None:
                v += f[0] * cur_round
                a["e"], j = toks[f[1] - 1].e, f[1]
                break
        if toks[k].lw == "and":
            k2 = _skip_sp(toks, k + 1)
            if k2 < len(toks) and toks[k2].k == "num" and re.fullmatch(r"[1-9]\d?", toks[k2].t) and cur_round >= 100 \
                    and not (_peek_word(toks, k2 + 1) in _Q_ROUND):
                v += _num_value(toks[k2].t)
                a["e"], j = toks[k2].e, k2 + 1
                continue
            if k2 < len(toks) and toks[k2].k == "word" and (toks[k2].lw in _Q_UNITS or toks[k2].lw in _Q_TENS) and cur_round >= 100:
                c = _compose_words(toks, k2)
                if c is not None and c[0] < 100:
                    v += c[0]
                    a["e"], j = toks[c[1] - 1].e, c[1]
                    continue
            break
        if toks[k].k == "num" and re.fullmatch(r"\d{1,3}", toks[k].t):
            k3 = _skip_sp(toks, k + 1)
            if k3 < len(toks) and k3 == k + 2 and toks[k3].k == "word" and toks[k3].lw in _Q_ROUND and _Q_ROUND[toks[k3].lw] < cur_round:
                cur_round = _Q_ROUND[toks[k3].lw]
                v += _num_value(toks[k].t) * cur_round
                a["e"], j = toks[k3].e, k3 + 1
                continue
        break
    return v, j


def rule_key(rule):
    """A rule's name for the ledger: its words before any example in parentheses ("an idiom (\"one-stop\")" → "an idiom")."""
    return re.sub(r"\s*\(.*$", "", rule or "").strip() or (rule or "")



# ═══ the checker ═══
# BUILD IC-3 (D1): NUM_RE, its word tables and _whole_value are gone — every count is the one reader's (quantities())
_AGE_IN_ID = re.compile(r"(?i)age_?(\d{1,2})(?!\d)")
_SENT_END = re.compile(r"[.!?](?=\s|$)")


# BUILD IC-3 (D1): EXEMPT_PATTERNS (and _MONTH, _COUNT_NOUNS) are gone — the one reader's own patterns name dates, times, phones,
# years, codes, labels and ages (_QN, _Q_LABEL, _Q_AGE); Exempter keeps the run's own names (name_spans)
# BUILD IC-3 (ledger step 38): a thousands comma ("1,000") is no separator — split there, "Emergency: 1,000 lbs" gave the part
# "000 lbs …" and so the name "000", which the reader then found inside "12,000"
_SEP = re.compile(r"\s+[/|—–-]\s+|\s*(?:(?<!\d),|,(?!\d{3}(?!\d))|[;:()])\s*")


def _name_forms(n):
    """The forms of a digit-bearing name in the data that the text may use (B3, Phase 4, review b F3): the whole name, each part
    between separators ("211 Kansas / United Way" → "211 Kansas"), and the digit word beside a capitalized word ("Station 7",
    "7 Shelter"); a bare three-digit name (an endpoint's number, "311") as itself. Each is matched as whole words."""
    n = re.sub(r"\s+", " ", n).strip()
    if re.fullmatch(r"\d{3}", n):
        return {n}
    if not re.search(r"\d", n) or not re.search(r"[A-Za-z]", n) or len(n) < 3:
        return set()
    out = {n}
    for part in _SEP.split(n):
        part = part.strip()
        if not (re.search(r"\d", part) and re.search(r"[A-Za-z]", part) and len(part) >= 3):
            continue
        out.add(part)
        m3 = re.match(r"(\d{3})\s+[A-Za-z]", part)      # B3 (run turn, the second R4): a name that begins with a three-digit number is
        if m3:                                          # known by that number alone ("211 Kansas / United Way" → "211"; B2's live
            out.add(m3.group(1))                        # lesson of 11/03 — a correct evidence sentence was removed for "through 211")
        w = part.split()
        for i, x in enumerate(w):
            if re.search(r"\d", x):
                if i + 1 < len(w) and w[i + 1][:1].isupper():
                    out.add(f"{x} {w[i + 1]}")
                if i > 0 and w[i - 1][:1].isupper():
                    out.add(f"{w[i - 1]} {x}")
    return out


NAME_KEYS = re.compile(r"(?i)(?:name|title)$|^(?:provider|site|program|property|facility|organization|agency|operatingagency|ownerormanager)$")


class Exempter:
    """The run's own non-figures: the ZIPs, record ids and digit-bearing names its data holds, beside the fixed patterns."""

    def __init__(self, zips=(), record_ids=(), names=()):
        self.zips = {z for z in zips if isinstance(z, str) and re.fullmatch(r"\d{5}", z)}
        self.ids = sorted({r for r in record_ids if isinstance(r, str) and re.search(r"\d", r)}, key=len, reverse=True)
        self.names = sorted({x for n in names if isinstance(n, str) for x in _name_forms(n)}, key=len, reverse=True)[:600]
        nn = [x for x in self.names if not re.search(r"[%$]", x)]       # BUILD IC-3 (D1): a "name" that holds a % or a $ states a figure
        # BUILD IC-3 (ledger step 38): a name never stands inside a number — not after a digit and a comma or a point ("8,311" holds no
        # "311", the endpoint 311-calls' name), not before a comma or a point and a digit
        self._rx_n = (re.compile(r"(?<![\w])(?<!\d[,.])(?:" + "|".join(re.escape(x) for x in self.ids + nn) + r")(?![\w])(?![,.]\d)")
                      if (self.ids or nn) else None)

    @classmethod
    def from_data(cls, raw, records_fn):
        zips, ids, names = set(), set(), set()

        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif isinstance(v, str):
                k = key.lower()
                if "zip" in k or "address" in k:
                    zips.update(re.findall(r"\b\d{5}\b", v))
                if k == "id":
                    ids.add(v)
                if NAME_KEYS.search(k):         # BUILD IC (D6): "programNotes", "propertyNotes" and a headline are prose, not names
                    names.add(v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and "zip" in key.lower():
                zips.add(str(int(v)).zfill(5))
        for ep, payload in (raw or {}).items():
            names.update(re.findall(r"(?<!\d)\d{3}(?!\d)", str(ep)))     # B3 (Phase 4, review b F3): an endpoint's number is a name —
            try:                                                        # "311-health" makes 311 the city's line, not a count
                walk(records_fn(payload))
            except Exception:  # noqa: BLE001
                continue
        return cls(zips, ids, names)

    def name_spans(self, text):
        """BUILD IC-3 (D1): (start, end, reason) of the run's own names, record ids and ZIPs in plain text — what quantities() reads as
        names beside its own patterns. A name that holds a % or a $ (an alert's title, "… Reduced 40%") is left out: it states a figure."""
        out = [(m.start(), m.end(), "a record id or a name in the data") for m in self._rx_n.finditer(text)] if self._rx_n else []
        out += [(m.start(), m.end(), "a ZIP in the data") for m in re.finditer(r"\b\d{5}\b", text) if m.group(0) in self.zips]
        return out



def _attrs(s):
    """A tag's attributes, each value unescaped once and without marks (BUILD IC, D6: a block's reason was read raw, so its "&#x27;" was
    escaped again where the details print it; a counter's data-target holds a mark while the page is being checked)."""
    return {m.group(1).lower(): _html.unescape(unmark(m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None
                                                       else m.group(4) or ""))
            for m in _ATTR.finditer(s or "")}


_WORD_PART_AFTER = re.compile(r"(?:</(?:span|a|b|i|em|strong|sup|sub|small|mark|code|q|abbr|time|data)>)*-[A-Za-z]")


def scan(html_text, exempter=None, body_only=True):
    """Walk an HTML string. Returns (text, chunks, counters): `text` the visible text (raw text elements and the head skipped; a space
    between chunks), each chunk {start, end, src (index into html_text), prov, block, exempt, strict, sec}; each counter
    {value, prov, strict, sec, src}."""
    text, chunks, counters, stack, astack = [], [], [], [], []
    pos, n, skip_until, in_body = 0, len(html_text), None, not body_only or "<body" not in html_text.lower()
    total = 0

    list_items = {}                  # BUILD IC-4 (D4): each list element's id → its items (direct <li>), for a typed count's check
    blk = [0]                        # BUILD IC-4 (D4): the block elements' boundaries met so far — the text between two is one block

    def state():
        st = {"prov": None, "block": None, "exempt": None, "strict": None, "sec": None, "counter": False, "refused": None, "li": None,
              "lang": None, "basis": None,          # BUILD IC-5 (D6): the nearest lang; a modeled figure's stated basis (data-basis-of)
              "li_n": list_items, "blk": blk[0]}
        for _name, a in stack:
            if "_li" in a:                     # BUILD IC-4 (D4): the innermost list item holding this text — (list id, item index)
                st["li"] = a["_li"]
            if "data-prov" in a:
                st["prov"] = a["data-prov"]
            if "data-prov-block" in a:
                st["block"] = a["data-prov-block"]
            if "data-num-exempt" in a:
                st["exempt"] = a["data-num-exempt"]
            if "data-strict" in a:
                st["strict"] = a["data-strict"]
            if a.get("lang"):                  # BUILD IC-5 (D6): the innermost element's lang decides
                st["lang"] = a["lang"]
            if "data-basis-of" in a:
                st["basis"] = a["data-basis-of"]
            if "data-refused" in a:            # BUILD IC-2 (D6): a quote or a declaration Python refused — (rule, why)
                st["refused"] = (a["data-refused"], a.get("title") or "")
            if a.get("id"):
                st["sec"] = a["id"]
            if "counter" in (a.get("class") or "").split():
                st["counter"] = True
        return st
    def segment(seg, src0, trail=True):
        """One stretch of text between tags: its marks move the author stack (BUILD IC, D2) — never into `text`; in the body, each piece
        between marks is a chunk with its author (None where no mark is open: a page with no marks reads as before)."""
        nonlocal total
        parts, last = [], 0
        for mm in _MARK_RE.finditer(seg):
            if mm.start() > last:
                parts.append((seg[last:mm.start()], src0 + last, astack[-1] if astack else None))
            c = mm.group(0)
            if c == MARK_CLOSE:
                if astack:
                    astack.pop()
            else:
                astack.append(_OPEN_OF[c])
            last = mm.end()
        if last < len(seg):
            parts.append((seg[last:], src0 + last, astack[-1] if astack else None))
        if not in_body or not "".join(_html.unescape(p) for p, _s, _a in parts).strip():
            return
        st = state()
        for p, s0, au in parts:
            t = _html.unescape(p)
            if t:
                chunks.append(dict(st, author=au, start=total, end=total + len(t), src=s0, raw=p))
                text.append(t)
                total += len(t)
        if trail:
            text.append(" ")
            total += 1
    for m in _TAG.finditer(html_text):
        seg = html_text[pos:m.start()]
        if seg and skip_until is None:
            # BUILD IC-4 (D8): a span followed by a hyphenated word part stays joined ("8-12-week", "90-day"): no space at that boundary
            segment(seg, pos, trail=not (m.group(1) == "/" and _WORD_PART_AFTER.match(html_text, m.end())))
        pos = m.end()
        tag = m.group(0)
        if tag.startswith("<!--"):
            continue
        close, name = m.group(1) == "/", (m.group(2) or "").lower()
        if skip_until is not None:
            if close and name == skip_until:
                skip_until = None
            continue
        if not close and name == "body":
            in_body = True
        if not close and name in RAW_TEXT:
            if not tag.endswith("/>"):
                skip_until = name
            continue
        if close:
            if name in _BLOCK_TAGS:            # BUILD IC-4 (D4): a block's end
                blk[0] += 1
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == name:
                    del stack[k:]
                    break
            continue
        a = _attrs(m.group(3))
        if "counter" in (a.get("class") or "").split() and "data-target" in a and in_body:
            st = state()
            counters.append({"value": parse_number(a.get("data-target")), "raw": a.get("data-target") or "", "prov": a.get("data-prov"),
                             "strict": st["strict"], "sec": st["sec"], "src": m.start()})
        if name in VOID or tag.endswith("/>"):
            continue
        if name in _BLOCK_TAGS:               # BUILD IC-4 (D4): a block's start
            blk[0] += 1
        if name in ("ol", "ul"):              # BUILD IC-4 (D4): a list element, its items counted as they open
            a = dict(a, _list=len(list_items))
            list_items[a["_list"]] = 0
        elif name == "li":
            lid = next((x[1]["_list"] for x in reversed(stack) if "_list" in x[1]), None)
            if lid is not None:
                a = dict(a, _li=(lid, list_items[lid]))
                list_items[lid] += 1
        stack.append((name, a))
    seg = html_text[pos:]
    if seg and skip_until is None:
        segment(seg, pos, trail=False)
    return "".join(text), chunks, counters


def _per_belongs(x, registry, pid):
    """BUILD IC-3 (D1 "Structure"): a rate's "per" part right after a cited figure belongs to that figure when the figure's registry
    entry states the same denominator ("[14] per 10,000 residents" beside a figure whose unit is "per 10,000 residents"; the unit read by
    the one reader, so "per ten thousand residents" is the same). A per part with no number of its own has nothing to count."""
    if x.get("per_value") is None:
        return True
    e = registry.get(pid) if registry is not None and pid else None
    unit = (e or {}).get("unit") or ""
    if not re.search(r"(?i)\bper\b", unit):
        return False
    dens = [q.get("per_value") if q.get("per_value") is not None else q.get("value") for q in quantities(unit) if q["role"] == "quantity"]
    return any(d is not None and abs(d - x["per_value"]) <= 1e-9 * max(1.0, abs(d)) for d in dens)


_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "td", "th",
               "section", "article", "header", "footer", "nav", "main", "aside", "blockquote", "details", "summary", "figure", "figcaption",
               "pre", "form", "fieldset", "legend", "br", "hr"}
_LIST_NOUNS = {"kpi": "kpi", "kpis": "kpi", "risk": "risk", "risks": "risk", "phase": "phase", "phases": "phase", "failure mode": "failure mode",
               "failure modes": "failure mode", "question": "question", "questions": "question"}
_LIST_NOUN_RX = re.compile(r"\s+(?:(?:key|core|named|listed|distinct|separate|tracked)\s+)?(kpis|risks|phases|failure modes|questions)\b", re.I)
# IC-4 Phase 7 (review b3): the list's noun is the count's own head — plural (a count of 2 to 50), at most one closed word between; a
# count of a part of the list ("Four of the KPIs", "Year 2 adds two KPIs", "the first three phases") is no count of the list
_SUBSET_BEFORE = re.compile(r"(?i)\b(?:add|adds|added|adding|another|more|new|further|extra|additional|first|last|top|remaining|other|"
                            r"of)\s+$")
_INLINE_NOUN_RX = re.compile(r"\s+(?:[a-z][a-z-]*\s+){0,2}?(?:steps|questions|phases|stages|parts|items|options|things|ways|reasons|"
                             r"pillars|priorities|components|elements|pieces|moves|tracks|goals|levers|actions|commitments|features|tools|"
                             r"principles|rules|tests|checks|criteria)\b", re.I)
# BUILD IC-5 (D6): "three things:" — the nouns that name a list grow by the general ones; "any noun" read a value after a colon as a list
# on the corpus ("the four emergency-type shelters: 17 → 8", "the two registries: Block 1's, saved with its page, and …": counts that do
# not match), so a noun of the world stays a count of the world, as IC-4's frozen "Three shocks" holds
_LIST_WHAT = {"kpi": "the KPIs this page shows", "risk": "the risks this page shows", "phase": "the phases this page shows",
              "failure mode": "the failure modes this page shows", "question": "the questions this page shows"}


_PERIOD_RX = re.compile(r"(?i)\s+(?:day|night|week|weekend|month|year|hour|minute|morning|evening|afternoon|quarter|semester|term|shift|"
                        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b")


def typed_category(x, text, chunk_after=None, lists=None, names=None):
    """BUILD IC-4 (D4): the category of a quantity the one reader read in the writer's prose with no link (role quantity; x its item,
    text the text it sits in) — ("scenario starting point" | "design commitment" | "structural count, checked" | "structural count, does
    not match", why, {says, holds, list}) or None (it stays U). chunk_after(pos): the scanner's chunk at the first text after pos (its
    "li": the list item it sits in, "li_n": the items of every list) — a list element that follows a colon; lists: the page's lists after
    every filter ({"kpi": n, …}). Decided on the frozen labels (quantity_labels_v2.json); see patch_p3a's docstring."""
    words, v = re.sub(r"\s+", " ", str(x.get("words") or "")).strip().lower(), x.get("value")
    a, b = x["start"], x["end"]
    tail = text[b:b + 40]
    if (re.match(r"\s*(?:CFR|C\.F\.R\.|U\.S\.C\.|USC)\b", tail)
            or re.search(r"(?:\bCFR|C\.F\.R\.|U\.S\.C\.|\bUSC|\u00a7)\s*$", text[max(0, a - 10):a])):
        return "not a quantity", "a citation (a title or section of the Code of Federal Regulations or the U.S. Code)", None
    cap = re.search(r"\b[A-Z][\w'-]*$", text[a:b])
    if cap and names and any(len(n) > len(cap.group(0)) and text.startswith(n, a + cap.start()) for n in names):
        return "not a quantity", "a name of the data's own that the expression's word begins", None
    if cap and re.search(r"[a-z]\s+$", text[max(0, a + cap.start() - 3):a + cap.start()]) and re.match(r"\s+[A-Z][a-z]", tail):
        return "not a quantity", "a capitalized number word inside a name (\u201cevery Second Light\u201d)", None
    if words == "every one" and re.match(r"\s+of\b", tail):
        return "not a quantity", "\u201cevery one of\u201d: all of them", None
    if words == "every other" and not _PERIOD_RX.match(tail):
        return "not a quantity", "\u201cevery other\u201d before a noun that is no period of time: all the others", None
    if v == 0 and re.search(r"(?i)\bfrom\s*$", text[max(0, a - 12):a]) and not re.search(     # IC-4 Phase 7 (review b13): a change from zero is a fact
            r"(?i)\b(?:grew|grow|grows|grown|growing|rose|rise|rises|risen|rising|increased|increase|went|climbed|jumped|expanded|fell|"
            r"dropped|declined|up|down)\b[^.;:]*\bfrom\s*$", text[max(0, a - 60):a]):
        return "scenario starting point", "a scenario's starting point (from zero)", None
    if (((words == "a single" or (words == "single" and re.search(r"(?i)\ba\s+$", text[max(0, a - 4):a])))
         and re.match(r"(?i)\s+(?:[a-z][a-z-]*\s+){0,2}?[a-z][a-z-]*\b", text[b:b + 40]))   # BUILD IC-5 (D6): "a single <noun>" — the
    #                                                program's own design (every quantity-role "single" of the frozen v2 labels is one)
            or (v == 1 and words == "one" and re.match(r"\s+named\b", text[b:b + 12]))):
        return "design commitment", "the program's own design (a single one, one named)", None
    if (words == "single" and not re.search(r"(?i)\ba\s+$", text[max(0, a - 4):a])     # IC-4 Phase 7 (review b13): "a single" is a count
            and re.match(r"\s+(?:[a-z][a-z-]*\s+){0,2}?[a-z][a-z-]*s\b", text[b:b + 40])):
        return "not a quantity", "\u201csingle\u201d before a plural noun: an adjective (one-off), no count", None
    if (v == 1 and words == "one" and re.search(r"(?i)\bat\s+$", text[max(0, a - 6):a])        # IC-4 (D4): the labelled case only
            and re.match(r"\s+[a-z][a-z-]*[^s\W]\b", tail)):
        return "ambiguous", "\u201cone\u201d after \u201cat\u201d before a singular noun (\u201cat one shelter\u201d): a certain one, or a count", None
    if (re.fullmatch(r"(?:below|under|above|over|less than|more than|at least|at most|exceeds?|greater than|lower than|higher than)\s+"
                     r"(?:one|1(?:\.0+)?)(?::1)?", words)
            and re.search(r"(?i)\bSROI\b|social return|return on (?:the )?investment|\bratio\b|break[- ]even", text[max(0, a - 160):b + 160])):
        return "comparison constant", "the ratio's break-even (1): a comparison constant, no quantity of the program's", None   # BUILD IC-5 (D6)
    if x.get("shape") != "single" or x.get("hedge") or x.get("op") or not isinstance(v, (int, float)) or v != int(v) or not 2 <= v <= 50:
        return None
    end = next((m.start() for m in _SENT_END.finditer(text, b)), len(text))
    colon = text.find(":", b, end + 1)
    c0, c1 = (chunk_after(a), chunk_after(colon)) if chunk_after and colon != -1 else (None, None)
    if c0 and c1 and c0.get("blk") != c1.get("blk"):     # a colon of another block (a heading after a list's line): not this count's
        colon = -1
    if colon != -1 and not text[b:colon].strip().endswith(("http", "https")):
        ch = chunk_after(colon + 1) if chunk_after else None
        if ch and ch.get("li") and ch["li"][1] == 0 and ch["start"] > colon and not text[colon + 1:ch["start"]].strip():
            # the list element the colon introduces: its first item begins right after the colon, in a chunk of its own (never the
            # list item the count sits in)
            n = (ch.get("li_n") or {}).get(ch["li"][0])
            what = "the list that follows"
        elif _INLINE_NOUN_RX.match(text, b):                             # the items written out after the colon, to the sentence's end —
            rest = text[colon + 1:end].strip()                           # only for a noun that names a list ("four questions: …"; never
            items = [s for s in re.split(r",\s*", rest) if s.strip()]    # "three shocks: …", a count of things in the world)
            if len(items) >= 2 and re.search(r"\s(?:and|or)\s", items[-1]) and not re.match(r"(?:and|or)\s", items[-1].strip()):
                items[-1:] = re.split(r"\s+(?:and|or)\s+", items[-1], maxsplit=1)      # BUILD IC-5 (D6): "a, b and c" — the last "and" ends it
            n = len(items) if len(items) >= 2 else (2 if re.search(r"\b(?:and|or)\b", rest) else None)
            what = "the items written after the colon"
        else:
            n = None
        if n:
            ok = n == int(v)
            return (("structural count, checked", f"a count of {what}, checked by Python: {n}") if ok else
                    ("structural count, does not match", f"says {int(v)}; {what} holds {n}")) + ({"says": int(v), "holds": n, "list": what},)
    m = _LIST_NOUN_RX.match(text, b)
    if m and _SUBSET_BEFORE.search(text[max(0, a - 16):a]):
        m = None            # IC-4 Phase 7 (review b3): a count of a part of the list
    key = _LIST_NOUNS.get(m.group(1).lower()) if m else None
    if key and lists and lists.get(key) is not None:
        n = int(lists[key])
        ok = n == int(v)
        return ((("structural count, checked", f"a count of {_LIST_WHAT[key]}, checked by Python: {n}") if ok else
                 ("structural count, does not match", f"says {int(v)}; {_LIST_WHAT[key]}: {n}")) + ({"says": int(v), "holds": n, "list": _LIST_WHAT[key]},))
    return None


def _basis_share(x, text):
    """BUILD IC-5 (D6): a fraction or a multiple of what follows it ("half of the ETV annual cap", "twice the rate", "50% of the cap") — in a
    modeled figure's stated basis, the writer's assumption."""
    v, b = x.get("value"), x["end"]
    ref = re.match(r"\s+(?:of\s+)?(?:the|a|an|its|their|this|that)\b", text[b:b + 14])
    return bool(ref) and (x.get("shape") == "multiplier" or (isinstance(v, (int, float)) and 0 < v < 1 and not x.get("pct"))
                          or (x.get("pct") and re.match(r"\s+of\b", text[b:b + 6])))


def numbers(text, chunks, exempter=None, authored=None, registry=None, lists=None):
    """Every quantity of scanned text with its status: cited (id or block), exempt (why), untraced, and where it sits.
    BUILD IC-3 (D1, D2): each item is one EXPRESSION of the one reader (quantities()), in digits or in words — "8-12", "about a third",
    "Nine of the [10]" — not each numeral. A registry span (a run of chunks under one data-prov) is one cited item and one piece the reader
    frames around; a frame around a cited figure that holds a number of the writer's own ("Nine of the [10]") counts that number; a "per"
    part after a cited figure belongs to it only when its registry entry states the same denominator (_per_belongs; `registry`) — then
    its number is an item set aside by that rule; otherwise it is the writer's own quantity.
    The author and the chunk decide as before (BUILD IC, D2): exempt by mark; cited by a registry span; a refused form's text — untraced,
    whatever its shape; text nobody declared — "unattributed"; the organizers' words or a registry label — "authored" (by org / label); a
    block Python computed — cited by the block; anything else not the model's — "authored" by Python. In the model's text the reader's
    role decides: a quantity is untraced (U), an undecided expression is "undecided" (listed apart, never in W), a vague one "vague"
    (listed), a name, a marker, a position or an idiom "exempt" with its rule as the reason. Each item carries its author, shape, rule and
    role."""
    if authored is None:
        authored = any(ch.get("author") for ch in chunks)
    starts = [ch["start"] for ch in chunks]

    def chunk_at(pos):                                       # the chunk holding pos (a binary search: the standard library's bisect is
        lo, hi = 0, len(starts)                              # not on the module's import list)
        while lo < hi:
            mid = (lo + hi) // 2
            if starts[mid] <= pos:
                lo = mid + 1
            else:
                hi = mid
        k = lo - 1
        return chunks[k] if 0 <= k < len(chunks) and chunks[k]["start"] <= pos < max(chunks[k]["end"], chunks[k]["start"] + 1) else None

    def src_of(ch, pos, t):
        off = pos - ch["start"]
        if ch["raw"] != text[ch["start"]:ch["end"]]:          # entities in the segment: find the expression in the raw text near its place
            k = ch["raw"].find(t[:12], max(0, off - 12))
            off = k if k >= 0 else off
        return ch["src"] + max(0, off)

    def chunk_after(pos):                                    # BUILD IC-4 (D4): the first chunk holding text at or after pos
        k = next((i for i, c in enumerate(chunks) if c["end"] > pos and text[max(c["start"], pos):c["end"]].strip()), None)
        return chunks[k] if k is not None else None

    runs = []                                                # the registry spans: (start, end, first chunk)
    for ch in chunks:
        if ch.get("prov") and not ch.get("exempt") and not (ch.get("counter") and text[ch["start"]:ch["end"]].strip() == "0"):
            if runs and runs[-1][2]["prov"] == ch["prov"] and runs[-1][1] == ch["start"]:      # one span split only by author marks
                runs[-1] = (runs[-1][0], ch["end"], runs[-1][2])
                continue
            runs.append((ch["start"], ch["end"], ch))
    fixed = [(a, b) for a, b, _c in runs]
    es = []                                                  # BUILD IC-5 (D6): the stretches of Spanish text (lang="es", any region)
    for ch in chunks:
        if str(ch.get("lang") or "").lower().split("-")[0] == "es":
            if es and es[-1][1] >= ch["start"] - 1:
                es[-1] = (es[-1][0], max(es[-1][1], ch["end"]))
            else:
                es.append((ch["start"], ch["end"]))
    out = []
    for a, b, ch in runs:
        au = ch.get("author") or ("py" if authored else None)
        t = text[a:b]
        out.append({"text": t, "at": a, "end": b, "sec": ch["sec"], "strict": ch["strict"], "src": ch["src"], "author": au, "status": "cited",
                    "id": ch["prov"], "shape": None, "rule": "a cited figure", "role": "quantity"})
    for x in quantities(text, fixed=fixed, names=exempter, es=es):
        s, e = x["start"], x["end"]
        inside = [(fa, fb) for fa, fb in fixed if s <= fa and fb <= e]
        ch = chunk_at(s)
        if ch is None:
            continue
        au = ch.get("author") or ("py" if authored else None)
        role, rule = x["role"], rule_key(x["rule"])
        num = {"text": x["words"], "at": s, "end": e, "sec": ch["sec"], "strict": ch["strict"], "src": src_of(ch, s, x["words"]), "author": au,
               "shape": x.get("shape"), "rule": rule, "role": role, "_x": x}
        if inside:                                           # a frame around a cited figure: what in it is the writer's own
            pid = next((r[2]["prov"] for r in runs if (r[0], r[1]) == inside[0]), None)
            if role == "per_part":
                if x.get("per_value") is None:
                    continue                                 # "[14] per resident": the figure's unit word — the part holds no number
                pa, pb = x.get("per_num_span") or (s, e)
                ch2 = chunk_at(pa) or ch
                num.update(text=text[pa:pb], at=pa, end=pb, src=src_of(ch2, pa, text[pa:pb]))
                if _per_belongs(x, registry, pid):           # IC-3 (ledger step 16): the figure's own denominator is an item set aside by
                    rule = "a per part whose denominator the figure's registry entry states"      # its rule, never dropped without one
                    num["rule"] = rule
                else:
                    num.update(rule="a per part whose denominator the figure's registry entry does not state", role="quantity")
                    role = "quantity"
                au = ch2.get("author") or ("py" if authored else None)
                num["author"] = au
                ch = ch2
            elif not any(x.get(k) is not None for k in ("value", "lo", "hi", "num", "den")):
                continue                                     # "about [27%]": the cited figure is the item; its frame adds no number
            else:
                own = [pp for pp in x.get("parts") or [] if not any(fa <= pp[0] and pp[1] <= fb for fa, fb in fixed)]
                if own:                                      # the writer's own value is the item ("one" of "one of [12]")
                    pa, pb = own[0]
                    num.update(text=text[pa:pb], at=pa, end=pb, src=src_of(ch, pa, text[pa:pb]))
                num["rule"] = "a number of the writer's own framed around a cited figure"
                role = "quantity"
        if ch.get("counter") and x["words"].strip() == "0":
            num.update(status="exempt", why="a counter's placeholder")
        elif ch["exempt"]:
            num.update(status="exempt", why=ch["exempt"])
        elif ch.get("refused"):          # IC-2 (D6; review a F2): a refused quote or declaration's number counts in U, whatever its shape
            num.update(status="untraced", why=ch["refused"][1], _refused=True)
            if ch["refused"][0] == "stale":     # IC-4 Phase 7 (review b8): its status, read from its reason (Registry._stale_why's words)
                num["stale"] = "source unavailable" if "did not load this run" in ch["refused"][1] else "changed"
        # IC-3 (ledger step 29): the author decides before the role, as IC-2's order (IC D2 rule 4: "a label run is the label's before any
        # pattern") — text nobody declared stays "not attributed" whatever it holds; the reader's role decides in the model's text only
        elif authored and au == "undeclared":
            num.update(status="unattributed")
        elif authored and au in ("org", "label"):
            num.update(status="authored", by=au)
        elif ch["block"] and not (authored and au == "model"):
            num.update(status="cited", block=ch["block"])
        elif authored and au != "model":
            num.update(status="authored", by="py")
        elif role not in ("quantity", "undecided", "vague"):
            num.update(status="exempt", why=rule)
        elif role == "undecided":
            num.update(status="undecided", why=rule)
        elif role == "vague":
            num.update(status="vague", why=rule)
        elif ch.get("basis") and role == "quantity" and not inside and _basis_share(x, text):
            be_ = registry.get(ch["basis"]) if registry is not None else None     # BUILD IC-5 Phase 4 (the replays, D4): by its label
            num.update(status="listed", category="the writer's assumption",       # BUILD IC-5 (D6, IC-3's "half"): a fraction or multiple
                       why="a share or multiple of a named figure in the stated basis of " + (str((be_ or {}).get("label") or "").strip()
                                                                                             or "a modeled figure")
                       + " (a modeled figure: the writer's assumption)")
        else:
            num.update(status="untraced")
            cat = (typed_category(x, text, chunk_after, lists, getattr(exempter, "names", None)) if role == "quantity" and not inside
                   else None)
            if cat and cat[0] == "ambiguous":                                # BUILD IC-4 (D4): listed with its own count (the undecided list)
                num.update(status="undecided", category=cat[0], why=cat[1])
            elif cat and cat[0] != "structural count, does not match":      # BUILD IC-4 (D4): listed with its category, not in U
                num.update(status="listed", category=cat[0], why=cat[1])
            elif cat:                                                         # a typed count its list does not hold: listed apart, flagged
                num.update(status="listed", category=cat[0], why=cat[1], flag=cat[2])
        out.append(num)
    out.sort(key=lambda d: (d["at"], d["end"]))
    # B3 (run turn R4, finding 1): a number that restates the age of a figure cited in its own sentence ("29% by nineteen" beside
    # [[…fosterHomelessnessAge19]]) names which figure it is — not a new figure. Live, a correct evidence sentence was removed for it.
    for x in out:
        if x["status"] != "untraced" or x.get("_refused") or x.get("shape") != "single":
            continue
        v = (x.get("_x") or {}).get("value")
        if v is None or v != int(v) or not 0 < v < 130 or (x.get("_x") or {}).get("pct") or (x.get("_x") or {}).get("money"):
            continue
        a = max((m.end() for m in _SENT_END.finditer(text, 0, x["at"])), default=0)
        e = next((m.start() for m in _SENT_END.finditer(text, x["at"])), len(text))
        ages = {int(m.group(1)) for y in out if y["status"] == "cited" and y.get("id") and a <= y["at"] < e
                for m in _AGE_IN_ID.finditer(y["id"])}
        if int(v) in ages:
            x.update(status="exempt", why="the age of a figure cited in its sentence")
    for x in out:
        x.pop("_x", None)
    return out


def check_page(html_text, exempter=None, registry=None, lists=None):
    """The integrity check of one built page: {n, m, cited_ids, cited_blocks, exempt, untraced: [...], violations: [...]} — n of m
    numbers traced (m = traced + untraced), a violation being an untraced number inside a data-strict block, a counter without
    data-prov, a stray token, or a registry span whose text is not the registry's shown form.
    BUILD IC-3 (D2): the items are the one reader's expressions (numbers()); the result gains the undecided and the vague expressions of
    the model's prose (each with its sentence), what each rule set aside on this page ("rules"), the reader's version, and each item's
    shape, rule and role in "numerals"; the words listed apart (IC's quantity_words) are gone — they are in the count."""
    text, chunks, counters = scan(html_text, exempter)
    authored = any(ch.get("author") for ch in chunks)
    nums = numbers(text, chunks, exempter, authored=authored, registry=registry, lists=lists)
    cited = [x for x in nums if x["status"] == "cited"]
    untr = [x for x in nums if x["status"] in ("untraced", "unattributed")]
    viol = []
    for x in untr:
        if x["strict"]:
            viol.append({"rule": "strict", "where": x["strict"], "text": x["text"], "context": _ctx(text, x["at"])})
    for c in counters:
        if c["prov"]:
            cited.append({"text": c["raw"], "status": "cited", "id": c["prov"], "sec": c["sec"]})
            if registry is not None:
                e = registry.get(c["prov"])
                if e is None or c["value"] is None or abs(e["value"] - c["value"]) > max(1e-6, abs(e["value"]) * 1e-6):
                    viol.append({"rule": "differs", "where": c["sec"] or "counter", "text": c["raw"],
                                 "context": f"counter {c['prov']}: the registry holds {e['shown'] if e else 'no such figure'}"})
        else:
            untr.append({"text": c["raw"], "status": "untraced", "sec": c["sec"], "strict": c["strict"]})
            viol.append({"rule": "strict" if c["strict"] else "counter", "where": c["sec"] or "counter", "text": c["raw"],
                         "context": "a counter with no data-prov"})
    for mt in re.finditer(r"\[\[[^\[\]]{0,240}\]\]|\[\[|\]\]|" + SENTINEL, text):     # one violation per stray token
        viol.append({"rule": "token", "where": "page", "text": mt.group(0)[:60], "context": _ctx(text, mt.start())})
    if registry is not None:
        for pid, shown in spans(html_text):
            e = registry.get(pid)
            if e is None or not shows(e, shown):            # IC-3 (ledger step 36): the one test of a span's text
                viol.append({"rule": "differs", "where": pid, "text": shown[:40],
                             "context": f"the registry holds {e['shown'] if e else 'no such figure'}"})
    ids, blocks = {}, {}
    for x in cited:
        if x.get("id"):
            ids[x["id"]] = ids.get(x["id"], 0) + 1
        else:
            blocks[x["block"]] = blocks.get(x["block"], 0) + 1
    by_author = {}
    for x in nums:
        k = (x.get("author") or "none", x["status"])
        by_author[k] = by_author.get(k, 0) + 1
    numerals = [{"text": x["text"].strip(), "author": x.get("author"), "status": x["status"], "id": x.get("id"), "block": x.get("block"),
                 "by": x.get("by"), "why": x.get("why"), "sec": x.get("sec"), "strict": x.get("strict"), "context": _ctx(text, x["at"]),
                 "shape": x.get("shape"), "rule": x.get("rule"), "role": x.get("role"), "category": x.get("category")}
                for x in nums]                                   # BUILD IC (D2): the claim, item by item; IC-3: its shape, rule and role
    rules = {}
    for x in nums:                                               # IC-3 (D2): what each rule set aside on this page
        if x["status"] == "exempt":
            rules[x.get("why") or "?"] = rules.get(x.get("why") or "?", 0) + 1
    listed = lambda st: [{"text": x["text"].strip(), "sec": x.get("sec"), "rule": x.get("why") or x.get("rule"), "context": _ctx(text, x["at"]),   # noqa: E731
                          "sentence": _sentence_around(text, x["at"], x["end"])} for x in nums if x["status"] == st and x.get("author") in ("model", None)]
    return {"n": len(cited), "m": len(cited) + len(untr), "cited_ids": ids, "cited_blocks": blocks, "forms": _forms_of(text, chunks),
            "exempt": sum(1 for x in nums if x["status"] == "exempt"),
            "untraced": [{"text": x["text"], "sec": x.get("sec"), "author": x.get("author"), "status": x.get("status"),
                          "context": _ctx(text, x["at"]) if "at" in x else ""} for x in untr],
            "violations": viol, "authored": authored, "numerals": numerals, "reader_version": READER_VERSION, "rules": rules,
            "undecided": listed("undecided"), "vague": listed("vague"),
            # BUILD IC-4 (D4): the typed quantities listed by category (not in U), and the typed counts their list does not hold (in U)
            "listed": [{"text": x["text"].strip(), "category": x.get("category"), "why": x.get("why"), "sec": x.get("sec"),
                        "sentence": _sentence_around(text, x["at"], x["end"])} for x in nums if x["status"] == "listed"],
            "typed_counts": [dict(x["flag"], text=x["text"].strip(), sec=x.get("sec"), sentence=_sentence_around(text, x["at"], x["end"]))
                             for x in nums if x.get("flag")],
            "by_author": [{"author": a, "status": st, "n": c} for (a, st), c in sorted(by_author.items())],
            # BUILD IC (review b F6): the animated counters — registry figures Python placed (their own text is a placeholder, exempt)
            "counters": [{"id": c["prov"], "text": c["raw"], "sec": c["sec"]} for c in counters if c["prov"]]}


def _forms_of(text, chunks):
    """BUILD IC-2 (D4, D6): the writer's declarations and refused forms on a scanned page, each once (a run of chunks under one span), and
    the quotes and comparisons it cites outside the page's apparatus — {"declared": [{text, kind, sec, context}], "refused": [{text, rule,
    why, sec, context}], "cited": {id: spans}}."""
    out, prev, cited = {"declared": [], "refused": []}, None, {}
    for ch in chunks:
        if not ch.get("exempt") and str(ch.get("prov") or "").startswith(("q.", "calc.")):   # the writer's quotes and comparisons on the
            cited[ch["prov"]] = cited.get(ch["prov"], 0) + 1                                # page, a quote in words too (it has no numeral)
        ex_ = ch.get("exempt") or ""
        if ex_.startswith(DECLARED_WHY):
            key = ("declared", ex_)
        elif ch.get("refused"):
            key = ("refused",) + tuple(ch["refused"])
        else:
            prev = None
            continue
        if prev is not None and prev[0] == key and prev[1]["end"] == ch["start"]:     # one span split only by author marks
            prev[1]["end"] = ch["end"]
            continue
        rec = {"start": ch["start"], "end": ch["end"], "sec": ch.get("sec"), "key": key}
        out[key[0]].append(rec)
        prev = (key, rec)
    for k in out:
        out[k] = [dict({"text": text[r["start"]:r["end"]].strip(), "sec": r["sec"], "context": _ctx(text, r["start"])},
                       **({"kind": r["key"][1][len(DECLARED_WHY):]} if k == "declared" else {"rule": r["key"][1], "why": r["key"][2]}))
                  for r in out[k]]
    out["cited"] = cited
    return out


_SPAN = re.compile(r'<span\b(?=[^<>]*\bclass="prov[ "])[^<>]*\bdata-prov="([^"]+)"[^<>]*>([^<]*)</span>')   # a registry span (never a counter)


def spans(html_text):
    """(id, text) of every registry span of a page — for the pipeline's cross-page check."""
    return [(_html.unescape(unmark(m.group(1))), _html.unescape(unmark(m.group(2)))) for m in _SPAN.finditer(html_text or "")]


def _norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def shows(e, text):
    """BUILD IC-3 (ledger step 36): whether a registry span's text is its entry's shown form — white space set aside; a quote's first
    letter may stand lowercase (a lead word mid-sentence: Registry.resolve); a rate's whole form (D7: whole_rate). The one test of a
    span's text: check_page, the pipeline's cross-page check and the tests read it."""
    if not isinstance(e, dict):
        return False
    a, b = _norm_ws(text), _norm_ws(e.get("shown"))
    return a == b or (bool(e.get("quoted")) and a == b[:1].lower() + b[1:]) or (whole_rate(e) is not None and a == _norm_ws(whole_rate(e)))


# ═══ BUILD IC-4 (D3): the check across pages ═══
_SHOWN_ONE = re.compile(r"\s*([\u2212-])?\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*([KMB])?\s*(%)?\s*")


def shown_interval(text):
    """BUILD IC-4 (D3): (lo, hi) — the actual values a shown figure could have been rounded from: "8" 7.5 to 8.5, "$500K" 499,500 to
    500,500, "12.5%" 12.45 to 12.55 (in the unit shown); None when the text is not one number (a range, a ratio, a quote's words)."""
    m = _SHOWN_ONE.fullmatch(str(text or ""))
    if not m:
        return None
    mul = {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(4) or "", 1.0)
    v = float(m.group(2).replace(",", "") + ("." + m.group(3) if m.group(3) else "")) * mul * (-1 if m.group(1) else 1)
    half = 0.5 * 10 ** -(len(m.group(3)) if m.group(3) else 0) * mul
    return v - half, v + half


_PER_TAIL = re.compile(r"\s+per\s+[\d,.]+(?:\s+[A-Za-z][\w-]*){0,3}\s*$|\s+per\s+[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,2}\s*$", re.I)
# IC-4 Phase 7 (review b6): a rate's per part ("14 per 10,000 residents") — the figure is its number; one fact's unit is one unit


def consistent_shown(texts):
    """BUILD IC-4 (D3): whether shown values are complete or consistent duplicates — one actual value, rounded as each is shown, gives
    them all (XBRL: "values that are consistent with having been rounded from a single actual value"); a text that is not one number
    must be the same words."""
    ivs = [shown_interval(t) or shown_interval(_PER_TAIL.sub("", _norm_ws(str(t)))) for t in texts]   # IC-4 Phase 7 (review b6)
    if ivs and all(iv is not None for iv in ivs):
        return max(a for a, _b in ivs) <= min(b for _a, b in ivs) + 1e-9
    return len({_norm_ws(str(t)).lower() for t in texts}) <= 1


def fact_words(f):
    """BUILD IC-4 (D3): a fact in words for the console and the run's report — the measure, of what, its scope, its unit, its as-of, and
    its scenario when it is not this run."""
    if not isinstance(f, dict):
        return "no fact"
    ent = f.get("entity") or {}
    if ent.get("name"):
        who = ent["name"]
    elif ent.get("endpoint") and ent.get("id"):
        who = f"{ent['endpoint']} {ent['id']}"
    elif ent.get("endpoint"):
        who = f"the {ent['endpoint']} records"
    elif ent.get("text"):
        who = f"the {ent['text']}"
    else:
        who = ent.get("quote") or ent.get("source") or ent.get("named") or "?"
    asof = f.get("as_of") if isinstance(f.get("as_of"), dict) else {}
    return (f"{f.get('measure')} of {who}" + (f" ({f['scope']})" if f.get("scope") else "") + (f", {f['unit']}" if f.get("unit") else "")
            + (f", as of {asof['value']} ({asof.get('from')})" if asof.get("value") else "")
            + (f", {f['scenario']}" if f.get("scenario") not in (None, "this run") else ""))


def facts_across(pages, reg, report=None):
    """BUILD IC-4 (D3): the check across pages. Every registry figure every page shows (spans), grouped by its fact key — Registry.fact,
    the unit in one form (fill_units over the registry's facts and the shown ones). A key shown by two spans or more (two ids, or two
    pages) holds consistent duplicates when one actual value, rounded as each is shown, gives them all (consistent_shown); else an
    inconsistent duplicate, named — it fails the run under --coverage fail. Beside it, what the data says of itself: the sidecar's
    checks whose two sides differ — "sources disagree", both values with their facts' sources and as-of, no precedence invented, unless
    the contract's authority names one side's metric (that side speaks for it) — and the data report's confirmed relations that fail
    (a violation, which fails the run) or cannot be read. pages: {page: html}; reg: the Oracle's registry of this run, with the run's
    data (set_data) and the contract's authority and aliases (set_facts); report: the data report (checkpoints/data_report.json) or
    None. Returns {"shown", "pages", "keys", "groups", "inconsistent", "disagree", "relations", "unknown", "console", "failed"}.
    Never raises."""
    out = {"shown": 0, "pages": {}, "keys": 0, "groups": [], "inconsistent": [], "disagree": [], "relations": [], "unknown": [],
           "console": [], "failed": False}
    try:
        facts = {k: reg.fact(e) for k, e in list(reg.entries.items())}
        items = []
        for page, html_text in (pages or {}).items():
            sp = spans(html_text)
            out["pages"][page] = len(sp)
            for pid, text in sp:
                e = reg.get(pid)
                if e is None:
                    out["unknown"].append({"page": page, "id": pid, "shown": _norm_ws(text)})
                    continue
                if e["id"] not in facts:
                    facts[e["id"]] = reg.fact(e)
                items.append({"page": page, "id": e["id"], "shown": _norm_ws(text)})
        fill_units(facts)
        out["shown"] = len(items)
        by = {}
        for it in items:
            by.setdefault(fact_key(facts[it["id"]]), []).append(it)
        out["keys"] = len(by)
        for k in sorted(by):
            seen = sorted({(i["page"], i["id"], i["shown"]) for i in by[k]})
            if len({(pg, i) for pg, i, _s in seen}) < 2:
                continue
            g = {"fact": fact_words(facts[seen[0][1]]), "ids": sorted({i for _pg, i, _s in seen}),
                 "shown": [{"page": pg, "id": i, "shown": s} for pg, i, s in seen], "consistent": consistent_shown([s for _pg, _i, s in seen])}
            out["groups"].append(g)
            if not g["consistent"]:
                out["inconsistent"].append(g)
        on = {}
        for it in items:
            on.setdefault(it["id"], set()).add(it["page"])
        for c in getattr(reg, "checks", []) or []:
            if not isinstance(c, dict) or c.get("equal"):
                continue
            sides = []
            for key in ("entry", "against"):
                e = reg.get(c.get(key))
                if e is None:
                    break
                f = facts.get(e["id"]) or reg.fact(e)
                sides.append({"id": e["id"], "value": e.get("value"), "shown": e.get("shown"), "fact": fact_words(f), "as_of": f.get("as_of"),
                              "source": e.get("source"), "authority": f.get("authority"), "pages": sorted(on.get(e["id"], ()))})
            if len(sides) != 2:
                continue
            au = next((s_ for s_ in sides if s_.get("authority")), None)
            out["disagree"].append({"category": "the contract's authority speaks" if au else "sources disagree", "entry": sides[0],
                                    "against": sides[1], "authority": au["authority"] if au else None,
                                    "speaks": au["id"] if au else None, "shown": bool(sides[0]["pages"] or sides[1]["pages"])})
        seen_m = {tuple(sorted((d["entry"]["id"], d["against"]["id"]))) for d in out["disagree"]}
        for fid in sorted({x for x in (getattr(reg, "authority", None) or {}) if isinstance(x, str) and not x.startswith("q:")}):
            for other in reg.metric_disagree(fid.partition("#")[0]):     # IC-4 Phase 7 (review b7): one metric, two values
                pair = tuple(sorted((reg.get(fid.partition("#")[0])["id"], other)))
                if pair in seen_m:
                    continue
                seen_m.add(pair)
                sides = []
                for i in pair:
                    e = reg.get(i)
                    f = facts.get(e["id"]) or reg.fact(e)
                    sides.append({"id": e["id"], "value": e.get("value"), "shown": e.get("shown"), "fact": fact_words(f), "as_of": f.get("as_of"),
                                  "source": e.get("source"), "authority": f.get("authority"), "pages": sorted(on.get(e["id"], ()))})
                au = next((s_ for s_ in sides if (s_.get("authority") or {}).get("endpoint")
                           and str(s_["id"]).startswith(f"{(s_.get('authority') or {}).get('endpoint')}.")), None)
                out["disagree"].append({"category": "the contract's authority speaks" if au else "sources disagree", "entry": sides[0],
                                        "against": sides[1], "authority": (au or sides[0])["authority"], "speaks": au["id"] if au else None,
                                        "shown": bool(sides[0]["pages"] or sides[1]["pages"])})
        for f_ in (report or {}).get("findings") or []:
            if isinstance(f_, dict) and f_.get("class") in ("violation", "cannot be checked"):
                out["relations"].append({"class": f_["class"], "what": f_.get("what"), "where": f_.get("where")})
        viol = [x for x in out["relations"] if x["class"] == "violation"]
        dis = [d for d in out["disagree"] if d["shown"]]
        out["console"].append(f"Facts across pages: {out['shown']} registry figure(s) shown on {len(out['pages'])} page(s) — {out['keys']} "
                              f"fact(s); {len(out['groups'])} shown by two spans or more, {len(out['inconsistent'])} inconsistent")
        for g in out["inconsistent"][:6]:
            out["console"].append("  \u26a0 one fact, two values: " + g["fact"] + " — "
                                  + "; ".join(f"{x['page']} {x['id']} shows {x['shown']}" for x in g["shown"][:4]))
        out["console"].append(f"Sources that disagree on a figure a page shows: {len(dis)}"
                              + (f" (and {len(out['disagree']) - len(dis)} on figures no page shows)" if len(out["disagree"]) > len(dis) else ""))
        for d in dis[:6]:
            a, b = d["entry"], d["against"]
            out["console"].append(f"  {d['category']}: {a['id']} {a['shown']} ({a['fact']}) against {b['id']} {b['shown']} ({b['fact']})"
                                  + (f" — the contract names {d['authority'].get('endpoint')} for {d['authority'].get('metric')}" if d["authority"] else ""))
        out["console"].append(f"Confirmed relations (the data report's): {len(viol)} fail, {len(out['relations']) - len(viol)} cannot be checked")
        for x in viol[:6]:
            out["console"].append(f"  \u26a0 {x['what']}")
        out["failed"] = bool(out["inconsistent"] or viol)
    except Exception as ex:  # noqa: BLE001 — a check that cannot run is said, never a failed pipeline
        out["console"].append(f"Facts across pages: not checked ({type(ex).__name__}: {str(ex)[:120]})")
    return out


def _per_den(text):
    """The denominator of the first rate the one reader reads in `text` ("1 per ten thousand residents" → 10000), or None."""
    q = next((x for x in quantities(text) if x["role"] == "quantity"), None)
    return None if q is None else q.get("per_value")


def whole_rate(e):
    """BUILD IC-3 (D7): "A rate prints whole" — a registry figure whose unit is a per part with a number ("per 10,000 residents") prints
    its value with it: "14 per 10,000 residents". None for any other entry (a per part with no number, "USD per visit", has nothing
    the writer could restate)."""
    unit = ((e or {}).get("unit") or "").strip()
    if not unit.lower().startswith("per ") or e.get("shown") is None or _per_den("1 " + unit) is None:
        return None
    return f"{e['shown']} {unit}"


def per_follows(e, after):
    """BUILD IC-3 (D7, D1's rate rule): whether the writer's text right after a rate's token states the rate's own per part, in any
    spelling ("per ten thousand residents" after a figure whose unit is "per 10,000 residents"); tags before it set aside."""
    after = re.sub(r"^(?:\s*<[^<>]*>)+", " ", after or "")
    if not re.match(r"\s*per\s", after, re.I):
        return False
    want, got = _per_den("1 " + ((e or {}).get("unit") or "").strip()), _per_den("1 " + after.strip())
    return want is not None and got is not None and abs(want - got) <= 1e-9 * max(1.0, abs(want))


_NOTE_TEXT = re.compile(r" ?\[note \d+\]")      # BUILD IC-7 (D9): a note link's text, as place_notes writes it
_CTX_TEXT = []                                   # (the page's text, its markers' spans, the text without them) — the last page read


def _ctx(text, at, n=60):
    """An excerpt: 60 characters each side of `at` in the page's text. BUILD IC-7 (D9, G11): cut from the text with the note links'
    text left out ("[note N]"), so a quoted excerpt never holds a marker; `at` moved by the markers before it."""
    if not _CTX_TEXT or _CTX_TEXT[0][0] is not text:
        _CTX_TEXT[:] = [(text, [(m.start(), m.end()) for m in _NOTE_TEXT.finditer(text)], _NOTE_TEXT.sub("", text))]
    _t, spans, clean = _CTX_TEXT[0]
    at = max(0, at - sum(min(e, at) - s for s, e in spans if s < at))
    a, b = max(0, at - n), min(len(clean), at + n)
    return ("…" if a else "") + re.sub(r"\s+", " ", clean[a:b]).strip() + ("…" if b < len(clean) else "")


def markers_outside_links(page_html):
    """BUILD IC-7 (D9, G11): how many "[note N]" texts a page prints outside a link (a marker quoted into an excerpt, or copied into
    prose) — the acceptance's notes row counts these, not only the links. Never raises: None when the page cannot be read."""
    import html.parser as _hp

    class P(_hp.HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.depth_a, self.skip, self.n = 0, 0, 0

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.depth_a += 1
            elif tag in ("script", "style", "template", "textarea"):
                self.skip += 1

        def handle_endtag(self, tag):
            if tag == "a" and self.depth_a:
                self.depth_a -= 1
            elif tag in ("script", "style", "template", "textarea") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.depth_a and not self.skip:
                self.n += len(_NOTE_TEXT.findall(data))
    try:
        p = P()
        p.feed(page_html or "")
        p.close()
        return p.n
    except Exception:  # noqa: BLE001
        return None


# ═══ the sanity rules on one resolved field (Python runs them before the page is built) ═══
_RESIDUAL = re.compile(r"(?P<a>\$?\d[\d,]*(?:\.\d+)?%?)\s*(?P<op>−|-|–|minus)\s*(?P<b>\$?\d[\d,]*(?:\.\d+)?%?)"
                       r"(?:\s*(?:=|equals|leaves|leaving)\s*(?P<c>[-−]?\$?\d[\d,]*(?:\.\d+)?%?))?", re.I)


def _tagkey(e):
    return (e.get("class"), e.get("window"), e.get("cohort")) if e else None


_REFUSED = re.compile(r'<span class="nolink" data-refused="(quote|declared|stale)" title="([^"]*)">([^<]*)</span>')   # IC-3 (D11): stale


def field_findings(html_text, registry, exempter=None, strict=None, unit_noun=None, events=None):
    """The violations of one resolved field (html with registry spans): a token naming nothing (SENTINEL); a residual whose result is
    not a registry figure (or is a proposal), or whose operands' tags differ; a unit word that contradicts a per-unit figure; and in a
    strict block (strict = its name) any untraced number. Each finding {rule, why, src} — src an index into html_text.
    BUILD IC-2 (D6): `events` (resolve's, with where each replacement sits) name a comparison that could not be computed ("calc", its
    sentence removed); a quote or a declaration that was refused is found by its span ("quote", "declared": flagged and kept)."""
    if not isinstance(html_text, str) or not html_text.strip():
        return []
    calc_at = {e_["at"]: e_.get("why") or "" for e_ in events or [] if e_.get("rule") == "calc" and isinstance(e_.get("at"), int)}
    near = [{"rule": "near_miss", "src": e_["at"], "why": e_.get("why") or "a token's near-miss form"} for e_ in events or []
            if e_.get("rule") == "near_miss" and isinstance(e_.get("at"), int)]           # BUILD IC-3 (D4): flagged and kept
    out = [({"rule": "calc", "why": f"a comparison Python could not compute, or false as named: {calc_at[i]}", "src": i} if i in calc_at
            else {"rule": "unknown", "why": "a token that names no figure", "src": i}) for i, c in enumerate(html_text) if c == SENTINEL]
    out += near
    refused_at = []
    for m in _REFUSED.finditer(html_text):
        out.append({"rule": m.group(1), "src": m.start(3), "why": f"{_html.unescape(m.group(3))}: {_html.unescape(m.group(2))}"})
        refused_at.append((m.start(), m.end()))
    text, chunks, _c = scan(html_text, exempter, body_only=False)
    nums = numbers(text, chunks, exempter, registry=registry)   # IC-3 (review a #12): with the registry, as check_page reads (a rate's own
    by_at = {x["at"]: x for x in nums}                          # denominator is set aside by both, never flagged by one)

    def num_at(i, s):
        lead = len(s) - len(s.lstrip("-−$"))      # IC-2 (U-r6): a result may carry a sign ("= -90", a diff the wrong way round)
        return next((by_at[i + k] for k in range(lead + 1) if by_at.get(i + k)), None)
    for m in _RESIDUAL.finditer(text):
        if not m.group("c") and m.group("op") not in ("−",) and m.group("op").lower() != "minus":
            continue            # B3 (Phase 4, review b F5): a hyphen or en dash between two figures with no result is a range ("17%–42%")
        xa, xb = num_at(m.start("a"), m.group("a")), num_at(m.start("b"), m.group("b"))
        if not xa or not xb or xa["status"] == "exempt" or xb["status"] == "exempt":
            continue
        ea = registry.get(xa.get("id")) if registry and xa.get("id") else None
        eb = registry.get(xb.get("id")) if registry and xb.get("id") else None
        if m.group("c"):
            xc = num_at(m.start("c"), m.group("c"))
            ec = registry.get(xc.get("id")) if registry and xc and xc.get("id") else None
            if not xc or xc["status"] != "cited" or (ec and PROPOSAL_ID.fullmatch(ec["id"]) and not ec["id"].startswith("b1.")):    # IC-2 (D3): a proposal, not any assumption
                out.append({"rule": "residual", "src": xa["src"],
                            "why": f"{m.group(0)}: a computed result that is not a registry figure — Python computes such a figure, "
                                   "or the sentence does not ship"})
                continue
            cc = (ec or {}).get("calc")         # IC-2 review a F6: a comparison as the result must be this difference, in this order
            if cc and not (cc.get("op") == "diff" and (cc.get("a"), cc.get("b")) == (xa.get("id"), xb.get("id"))):
                out.append({"rule": "residual", "src": xa["src"],
                            "why": f"{m.group(0)}: the result Python computed is {cc.get('op')} of {cc.get('a')} and {cc.get('b')}, "
                                   f"not {xa.get('id') or m.group('a')} − {xb.get('id') or m.group('b')}"})
                continue
        if ea and eb and _tags_differ(ea, eb):
            out.append({"rule": "residual", "src": xa["src"],
                        "why": f"{m.group(0)}: {ea['id']} ({', '.join(t for t in _tagkey(ea) if t)}) and {eb['id']} "
                               f"({', '.join(t for t in _tagkey(eb) if t)}) are not the same cohort, window or class"})
    if registry is not None:
        for x in nums:
            e = registry.get(x.get("id")) if x.get("id") else None
            if not e or not (e.get("unit") or "").lower().startswith(("usd per ", "per ")):
                continue
            want = UNIT_NOUNS.get((e["unit"].split()[-1] or "").lower())
            m = re.match(r"\s*(?:\w+\s+){0,2}?per\s+(\w+)", text[x["at"] + len(x["text"]):x["at"] + len(x["text"]) + 40], re.I)
            if not m:           # B3 (Phase 4, review b F4): the unit word before the figure too ("Cost per family: $X")
                m = re.search(r"\bper\s+(\w+)\s*[:,—–-]?\s*(?:(?:of|is|was|at|stands at|reaches|comes to)\s+)?(?:about\s+|roughly\s+)?\$?$",
                              text[max(0, x["at"] - 48):x["at"]], re.I)
            got = UNIT_NOUNS.get(m.group(1).lower()) if m else None
            if want and got and got != want:
                out.append({"rule": "unit", "src": x["src"], "why": f"{x['text']} per {m.group(1)}: {e['id']} is {e['unit']}"})
    if strict:
        out += [{"rule": "strict", "src": x["src"], "why": f"{x['text']}: a number in {strict} that is not a registry figure"}
                for x in nums if x["status"] == "untraced" and not any(a <= x["src"] < b for a, b in refused_at)]   # IC-2: flagged once
    return out


def apply_findings(html_text, findings):
    """Remove each finding's sentence; returns (text, removed: [{sentence, why}])."""
    if not findings:
        t, rem = remove_marked(html_text, [])
        return t, [{"sentence": r, "why": "a token that names no figure"} for r in rem]
    t, rem = remove_marked(html_text, [f["src"] for f in findings])
    whys = [f["why"] for f in findings]
    return t, [{"sentence": r, "why": "; ".join(dict.fromkeys(whys))[:300]} for r in rem]


# ═══ the other tracks' read of the Oracle (B3, D2) ═══
def read_oracle(oracle_dir, challenge=None):
    """(Registry, None) or (None, why): the Oracle's checkpoints/registry.json beside its master analysis — of this challenge, and of this
    pipeline run when NAIPC_RUN_ID names one (a track run on its own accepts the Oracle's latest). Never raises."""
    rid = os.environ.get("NAIPC_RUN_ID", "").strip() or None
    reg, why = read(Path(oracle_dir) / REGISTRY_FILE, challenge or os.environ.get("NAIPC_CHALLENGE", "").strip() or None, rid)
    if reg is not None and str(reg.block) == "2":       # BUILD IC-8 (d12c): at Block 2, Block 1's data beside it (its removed records'
        reg.set_before(oracle_raw(Path(oracle_dir) / "block1"))     # names: the Oracle's checkpoints/block1, NR.BLOCK1_DIR)
    return reg, why


def resolve_for_track(reg, text, html=False):
    """The Oracle's text as another track shows it (B3, D2): every token as its registry figure — plain, or as a span when html; with no
    registry of this run (reg None) every sentence holding a token is left out. Returns (text, sentences left out). Never raises."""
    if not isinstance(text, str) or ("[[" not in text and "]]" not in text):
        return text, 0
    if reg is None:
        t = TOKEN_RE.sub(SENTINEL, text).replace("[[", SENTINEL).replace("]]", SENTINEL)
    else:
        t = reg.resolve(text, mode="html" if html else "plain")[0]
    if SENTINEL in t:
        out, rem = remove_marked(t, [], plain=not html)
        return out, len(rem)
    return t, 0


def resolve_tree(reg, value, html=False):
    """resolve_for_track over a value's every string (lists and dicts walked); returns (value, sentences left out)."""
    if isinstance(value, str):
        return resolve_for_track(reg, value, html)
    if isinstance(value, list):
        out, n = [], 0
        for v in value:
            r, k = resolve_tree(reg, v, html)
            out.append(r)
            n += k
        return out, n
    if isinstance(value, dict):
        out, n = {}, 0
        for k_, v in value.items():
            r, k = resolve_tree(reg, v, html)
            out[k_] = r
            n += k
        return out, n
    return value, 0


# ═══ BUILD IC-4 (D16): the other tracks' own text under the same rules — the request parts the Oracle builds for them on its final
# registry, and each track's own text resolved on that registry (its own lists' counts its own) ═══
TRACK_PARTS_FILE = "track_parts.json"     # beside registry.json in the Oracle's checkpoints (written by the Oracle's track_parts)
MUSE_COUNT_LISTS = ("personas", "channel_plan", "calendar", "kpis", "social_posts")      # the Muse's replies' lists, as its page shows them
MUSE_COUNT_NAMES = {"personas": "personas", "channel_plan": "channels", "calendar": "campaign phases", "kpis": "KPIs",
                    "social_posts": "social posts"}
_TRACK_COUNT = re.compile(r"\[\[\s*count\s*:\s*([A-Za-z_]\w*)\s*\]\]", re.I)
TRACK_COUNTS = {"muse": MUSE_COUNT_NAMES}      # BUILD IC-8 (D2): a track whose [[count:<list>]] reads count.<track>.<list>, sealed first


def strict():
    """BUILD IC-8 (D2, F17): NAIPC_STRICT=1 — a check of Python's own order raises (the rehearsal, the replays, the tests); live, the unit's
    fallback is kept with its reason and the acceptance row fails."""
    return os.environ.get("NAIPC_STRICT", "").strip() == "1"


def seal_counts(reg, track, lists=None):
    """BUILD IC-8 (D2): the track's count namespace sealed — its counts registered from its final membership; saved with the registry."""
    if reg is not None:
        reg.sealed_counts.add(track)
    return reg


def _count_problem(reg, why):
    if strict():
        raise StageError(why)
    reg.count_problems.append(why)



# ═══ BUILD IC-7 (D4, G2): the number rules as lines — one set, each line with its precondition, read for each call. The Oracle's two texts
# are these lines as written (oracle_harness.py rebuilds _RULES_TEXT and _RULES_TEXT_IC4 from them, byte for byte: tests compare every
# Oracle request with IC-6's); each Muse call takes its own lines (number_rules_text with its view): the typed-number line only where its
# reply's schema types a field as a number, naming those fields; the count line naming its own lists; the name line without the Oracle's
# fields. Taken from the Oracle's literals at build time (ic7_dev/patch_reg7.py). ═══
RULES_HEAD = '<number_rules>\nA reader must be able to check where every quantity on this page comes from. So every quantity you write is a token, however you\nwould otherwise write it: in digits, as a number word ("three years"), a fraction ("a third"), a range, a rate or a multiplier\n("twice"). Python prints the value with its qualifier (a rate with its per part); write the unit after the token ("[[<id>]] days")\nwhen what it prints, in <registry> or <mentions>, shows none.'
RULES_FOOT = "</number_rules>"
RULE_LINES = (            # (name, IC-3's line, IC-4's line or None: the same)
    ('figure', '- A figure the data holds as a number: its id from <registry>.',
     None),
    ('mention', '- A quantity the records\' text, the brief or the announcement states: its id from <mentions>. Python prints it as the source writes\n  it, "about" and "up to" included. A quantity the list does not hold: [[q:<where>|<the words exactly as the text writes them>]],\n  <where> being <endpoint>.<record id>.<field> (<endpoint>.<field> when the endpoint is itself the record), brief or announcement.',
     None),
    ('calc', '- A comparison of two figures: [[calc:<op>|<id a>|<id b>]], <op> being diff (a − b), pct_of (a as a percent of b), pct_less or\n  pct_more (how far a is below or above b, as a percent of b), times (a ÷ b) or pp (the gap between two shares, printed as points).\n  Python computes it. Each of <id a> and <id b> is an id from <registry> or a mention that prints a plain number — never a number\n  of your own, a range, a ratio, a bound or a figure with a qualifier ("about", "up to"): Python refuses those, and the sentence goes.\n  diff and pp join figures of the same people, place and period; a comparison false as named is removed.',
     None),
    ('count', '- How many items your own list holds (the KPIs, the risks, the phases): no number — name the list ("the KPIs below", "each risk").',
     "- Where you say how many items a list holds, write [[count:<list>]], <list> being the list's field name in a reply's schema\n  (kpis, risks, phases, judge_qa, …): Python prints how many items the page shows."),
    ('own', '- A number of your own — a target, a threshold, a duration, a staffing level: [[model:<number>|<what it is>]], a number and never a\n  range (each end of a range is its own proposal). It shows as your assumption; a share is written as a percent ("[[model:3%|…]]").',
     '- A number of your own — a target, a threshold, a duration, a staffing level, a count in the program\'s own design (one named contact\n  per landlord): [[model:<number>|<what it is>]], a number and never a range (each end of a range is its own proposal). It shows as your assumption; a share is written as a percent ("[[model:3%|…]]").'),
    ('typed', "- A field the reply's schema types as a number (a count, a percent, a day, a target) holds a plain number, never a token: tokens go in\n  text.",
     None),
    ('nf', '- A name that has digits and is no quantity: write a year, a date, a time, an age after the word "age" and the data\'s own names\n  plainly; anything else as [[nf:<the text>|<kind>]], <kind> being age, year, date, time, name, code or phone.',
     None),
    ('name', "- A name you write — a staff role's title, an outcome's category — holds no quantity: the number goes in the field that holds it\n  (count, rate, duration_years) or in your prose.",
     None),
    ('none', '- If none of these fits, write the sentence without the quantity and say what is not known. A quantity in another spelling is\n  counted as having no link; a token that names nothing (a wrong id, a form with a part missing) takes its sentence off the page.',
     None),
)
COUNT_EXAMPLE = 'How many items a list holds: [[count:{}]] — Python prints how many items the page shows.'
MUSE_NAME_LINE = "- A name you write — a persona's name, a channel, a phase — holds no quantity: the number goes in your prose."
RULE_PRECONDITIONS = {"count": "the reply has lists a count may name (view count_lists)",
                      "typed": "the request sends or shows its reply's schema and that schema types a field as a number (view number_fields)",
                      "name": "the reply's own fields (the Oracle's: count, rate, duration_years; a Muse reply has none: the number goes in prose)"}


def number_rules_text(view=None, ic4=True):
    """BUILD IC-7 (D4): the <number_rules> block. view None — every line as written (the Oracle's requests; ic4: IC-4's list and own-number
    lines, as _RULES_TEXT_IC4). A view {"count_lists": [...], "number_fields": [...], "schema": "sent" | "shown" | None} — a Muse call's:
    the count line names its own lists ("in this reply's schema" when one is sent or shown) or goes when it has none; the typed-number line
    names its number-typed fields, or goes when it has none or no schema is sent or shown; the name line is MUSE_NAME_LINE."""
    out = []
    for name, t3, t4 in RULE_LINES:
        t = (t4 or t3) if ic4 else t3
        if view is not None:
            if name == "count":
                lists = list(view.get("count_lists") or [])
                if not lists:
                    continue
                where = "this reply's schema" if view.get("schema") else "this reply"
                t = (f"- Where you say how many items a list holds, write [[count:<list>]], <list> being the list's field name in {where}\n"
                     f"  ({', '.join(lists)}): Python prints how many items the page shows.")
            elif name == "typed":
                nums = list(view.get("number_fields") or [])
                if not nums or not view.get("schema"):
                    continue
                t = (f"- A field the reply's schema types as a number ({', '.join(nums)}) holds a plain number, never a token: tokens go in\n"
                     "  text.")
            elif name == "name":
                t = MUSE_NAME_LINE
        out.append(t)
    return RULES_HEAD + "\n" + "\n".join(out) + "\n" + RULES_FOOT


def rules_examples(parts):
    """BUILD IC-7 (D4): the examples the Oracle built for another track — its parts file's "examples" (IC-7), else read from the
    <examples> block inside its "rules" (a parts file written before IC-7). [] when none."""
    if not isinstance(parts, dict):
        return []
    ex = parts.get("examples")
    if isinstance(ex, list):
        return [x for x in ex if isinstance(x, str) and x.strip()]
    m = re.search(r"<examples>\n(.*?)\n</examples>", parts.get("rules") or "", re.S)
    return [re.sub(r"^\d+\.\s+", "", ln) for ln in (m.group(1).split("\n") if m else []) if re.match(r"^\d+\.\s+", ln)]


def examples_block(examples, view=None):
    """BUILD IC-7 (D4): the <examples> block for a call — a count example names one of the call's own lists (the middle one, as the
    Oracle's count_example) or goes when the call has none; "" with no example."""
    lists = list((view or {}).get("count_lists") or []) if view is not None else None
    kept = []
    for t in examples or []:
        m = _TRACK_COUNT.search(t)
        if m and lists is not None:
            if not lists:
                continue
            if m.group(1) not in lists:
                t = COUNT_EXAMPLE.format(lists[len(lists) // 2])
        kept.append(t)
    return ("\n<examples>\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(kept, 1)) + "\n</examples>") if kept else ""



def write_track_parts(checkpoints_dir, parts):
    """BUILD IC-4 (D16): the request parts into <checkpoints>/track_parts.json, atomically. None, or why it was not written."""
    try:
        p = Path(checkpoints_dir) / TRACK_PARTS_FILE
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes((json.dumps(parts, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def read_track_parts(oracle_dir, challenge=None):
    """BUILD IC-4 (D16): (parts, None) or (None, why) — the Oracle's track_parts.json of this challenge and of this pipeline run when
    NAIPC_RUN_ID names one, as read_oracle takes the registry. Never raises."""
    rid = os.environ.get("NAIPC_RUN_ID", "").strip() or None
    try:
        d = admit(json.loads((Path(oracle_dir) / TRACK_PARTS_FILE).read_bytes().decode("utf-8-sig")), "saved", TRACK_PARTS_FILE)   # BUILD IC-5 (D1)
    except FileNotFoundError:
        return None, "no request parts from the Oracle"
    except Exception as e:  # noqa: BLE001
        return None, f"not readable ({type(e).__name__})"
    if not isinstance(d, dict) or d.get("schema") != 1 or not all(isinstance(d.get(k), str) for k in ("registry", "mentions", "rules")):
        return None, "not a schema-1 parts file"
    ch = challenge or os.environ.get("NAIPC_CHALLENGE", "").strip() or None
    if ch is not None and d.get("challenge") != ch:
        return None, f"of {d.get('challenge')!r}, not {ch!r}"
    if rid is not None and d.get("run_id") != rid:
        return None, f"written by run {d.get('run_id')}, this run is {rid}"
    return d, None


def add_track_counts(reg, track, shown, names):
    """BUILD IC-4 (D16, D13): how many items each list of another track's page shows — {list: count} — registered as
    count.<track>.<list>, DERIVED, format count, so that track's [[count:<list>]] prints its own list's count (resolve_track_text reads
    it there). A list already counted is left as it is. Returns the entries registered."""
    out = []
    for name, n in (shown or {}).items():
        if name not in names or not isinstance(n, int) or isinstance(n, bool):
            continue
        key_ = f"count.{track}.{name}"
        if track in getattr(reg, "sealed_counts", ()):       # BUILD IC-8 (D2): after the seal — raised strict, recorded live
            _count_problem(reg, f"{key_} registered after the {track}'s counts were sealed")
            continue
        if key_ in reg.entries:
            if reg.entries[key_].get("value") != n:         # BUILD IC-8 (D2): V21's silent skip goes — a recount must agree
                _count_problem(reg, f"{key_} counted again as {n} (first {reg.entries[key_].get('value')})")
            continue
        e = reg.add(f"count.{track}.{name}", n, "DERIVED", fmt="count", label=f"the {names[name]} this page shows",
                    formula="the items its list shows, after every filter", cls="count")
        if e:
            e["counted"] = True          # its title says what it is: counted on this page, not computed from the data
            out.append(e)
    return out


def resolve_track_text(reg, text, track=None, html=False):
    """BUILD IC-4 (D16): another track's own text on the Oracle's registry of this run — [[count:<list>]] as that track's own count
    (count.<track>.<list>), every other token as the Oracle's page prints it (a span when html); a sentence whose token names nothing
    is left out; with no registry of this run (reg None) every sentence holding a token is. Returns (text, [the sentences left out, as
    plain text]). Never raises."""
    if not isinstance(text, str) or ("[[" not in text and "]]" not in text):
        return text, []
    try:
        if track and track in TRACK_COUNTS:         # BUILD IC-8 (D5): only a track whose counts are its own (the Muse's) reads them so
            text = _TRACK_COUNT.sub(lambda m: f"[[count:{track}.{m.group(1)}]]", text)
        if reg is None:
            t = TOKEN_RE.sub(SENTINEL, text).replace("[[", SENTINEL).replace("]]", SENTINEL)
        else:
            t = reg.resolve(text, mode="html" if html else "plain")[0]
        if SENTINEL in t:
            return remove_marked(t, [], plain=not html)
        return t, []
    except StageError:          # BUILD IC-8 (D2): Python's order, never the model's fault — the caller's unit decides (resolve_unit)
        raise
    except Exception:  # noqa: BLE001 — a text that cannot be resolved shows none of its tokens
        return remove_marked(TOKEN_RE.sub(SENTINEL, text).replace("[[", SENTINEL).replace("]]", SENTINEL), [], plain=not html)


# ═══ BUILD IC-8 (D2, D3, I3): one outcome per unit — the unit rule, its chain, the dispositions ═══
POINT_BACK = ("It", "Its", "This", "That", "These", "Those", "They", "Their", "Such", "Both", "The same", "Then")
UNIT_DEFAULT = "Not shown: a figure in this text does not check against this run's data."
ANSWER_DEFAULT = "This answer is not shown: a figure it cites is not in this run's data."
ORACLE_DEFAULTS = {"hero_title": "Strategic & Operational Blueprint", "hero_lead": "{program}: the strategic and operational blueprint.",
                   "badge_text": "Prepared for review", "summary_title": "Executive Summary", "crisis_title": "The Problem",
                   "solution_title": "Our Approach", "evidence_title": "Key Findings", "operations_title": "Operational Model",
                   "plan_title": "Implementation Plan", "financial_title": "Financial Analysis", "appendix_title": "Sources and Methodology",
                   "renewal_label": "Renewal Trigger"}
DISPOSITIONS = ("kept_valid", "replaced_valid", "added_valid", "trimmed", "rejected_kept_base", "fallback_default", "omitted")
DISPOSITIONS_SCHEMA = 1
# BUILD IC-8 (D3): an item's record from its edit status (the update's record, every track) — (disposition, reason, source, the line's op)
EDIT_OUTCOMES = {"kept": ("kept_valid", "kept", "base", "keep"), "replaced": ("replaced_valid", "replaced", "candidate", "replace"),
                 "added": ("added_valid", "added", "candidate", "add"),
                 "not applied": ("rejected_kept_base", "edit_not_applied", "base", None),
                 "not reviewed": ("kept_valid", "not_reviewed", "base", None), "removed": ("omitted", "removed", "candidate", "remove")}


def oracle_default(field, program=""):
    """BUILD IC-8 (D3): an Oracle field's own default (plain words; the page escapes them), or None."""
    t = ORACLE_DEFAULTS.get(field)
    return t.format(program=(program or "").strip() or "The program") if t else None


def resolve_track_text_marked(reg, text, track=None, html=False):
    """BUILD IC-8 (D2, D3): resolve_track_text with each token that names nothing left as SENTINEL in place (no sentence removed) — the
    unit rule reads which sentences would go. A StageError is raised (Python's order: the caller's unit decides)."""
    if not isinstance(text, str) or ("[[" not in text and "]]" not in text):
        return text
    if track and track in TRACK_COUNTS:             # BUILD IC-8 (D5): only a track whose counts are its own
        text = _TRACK_COUNT.sub(lambda m: f"[[count:{track}.{m.group(1)}]]", text)
    if reg is None:
        return TOKEN_RE.sub(SENTINEL, text).replace("[[", SENTINEL).replace("]]", SENTINEL)
    try:
        return reg.resolve(text, mode="html" if html else "plain")[0]
    except StageError:
        raise
    except Exception:  # noqa: BLE001 — a text that cannot be resolved shows none of its tokens
        return TOKEN_RE.sub(SENTINEL, text).replace("[[", SENTINEL).replace("]]", SENTINEL)


def unit_rule_why(t, spans, gone, subject=None):
    """BUILD IC-8 (D3): THE unit rule. `t` a text, `spans` its sentence spans, `gone` the indexes of the spans to leave out. They may go
    only if (a) a sentence with words is kept, (b) the next kept sentence with words does not open by pointing back (POINT_BACK), and
    (c) a kept sentence still names `subject`. A span with no words is no sentence. → None, or the rule that fails: "only_sentence",
    "next_points_back", "only_sentence_naming_subject"."""
    gone = {k for k in gone if 0 <= k < len(spans)}
    if not gone:
        return None
    words = [plain_sentence(t[a:b]).replace(SENTINEL, "").strip() for a, b in spans]
    keep = [k for k in range(len(spans)) if k not in gone and words[k]]
    if not keep:
        return "only_sentence"
    for k in sorted(gone):
        nxt = next((j for j in keep if j > k), None)
        lead = words[nxt] if nxt is not None else ""
        if lead and any(lead == w or lead.startswith(w + " ") or lead.startswith(w + ",") for w in POINT_BACK):
            return "next_points_back"
    if subject:
        rx = re.compile(r"(?i)\b" + re.escape(subject) + r"\b")
        if any(rx.search(words[k]) for k in gone) and not any(rx.search(words[k]) for k in keep):
            return "only_sentence_naming_subject"
    return None


def unit_why(reg, text, track=None, preseal=False):
    """BUILD IC-8 (D3): the unit rule on a stored text as a page resolves it — None (no sentence goes, or those that go may), else the
    failed rule's reason. A count read before its seal raises StageError (the caller decides). `preseal`: the rule read before the
    track's counts are sealed (a membership decision) — a count of one of its declared lists is read as a number, never as a hole."""
    if not isinstance(text, str) or ("[[" not in text and "]]" not in text):
        return None
    t0 = unmark(text) if has_marks(text) else text
    if preseal and track in TRACK_COUNTS:
        t0 = _TRACK_COUNT.sub(lambda m: "12" if m.group(1) in TRACK_COUNTS[track] else m.group(0), t0)
    t = resolve_track_text_marked(reg, t0, track)
    if SENTINEL not in t:
        return None
    sp = sentence_spans(t)
    return unit_rule_why(t, sp, {k for k, (a, b) in enumerate(sp) if SENTINEL in t[a:b]})


def resolve_unit(reg, text, track=None, html=False, base=None, default=None, subject=None):
    """BUILD IC-8 (D2, D3): one text under the unit rule → {text, omitted, chain, reason, source}. A sentence whose token names nothing
    goes when the rule allows it (chain False, its sentences in omitted); when it does not, chain True and reason the failed rule (the
    caller's chain decides the text). A StageError: under NAIPC_STRICT=1 it raises; live, the base resolved under the same rule (its raw
    tokens never reach a page), else the default — reason pipeline_order."""
    if not isinstance(text, str):
        return {"text": text, "omitted": [], "chain": False, "reason": "not_text", "source": "candidate"}
    try:
        t = resolve_track_text_marked(reg, text, track, html)
    except StageError:
        if strict():
            raise
        rb = resolve_unit(reg, base, track, html, None, default, subject) if isinstance(base, str) and base.strip() else None
        if rb is not None and not rb["chain"] and isinstance(rb["text"], str) and rb["text"].strip():
            return {"text": rb["text"], "omitted": [], "chain": True, "reason": "pipeline_order", "source": "base"}
        return {"text": default, "omitted": [], "chain": True, "reason": "pipeline_order", "source": "default"}
    if not isinstance(t, str) or SENTINEL not in t:
        return {"text": t, "omitted": [], "chain": False, "reason": "resolved", "source": "candidate"}
    spans = sentence_spans(t)
    why = unit_rule_why(t, spans, {k for k, (a, b) in enumerate(spans) if SENTINEL in t[a:b]}, subject)
    if why:
        return {"text": None, "omitted": [], "chain": True, "reason": why, "source": None}
    out, gone = remove_marked(t, [], plain=not html)
    return {"text": out, "omitted": gone, "chain": False, "reason": "token_names_nothing", "source": "candidate"}


def names_nothing(reg, text, track=None):
    """BUILD IC-8 (D3): whether a stored text holds a token that names nothing as `reg` reads it now — an entry whose name the text is
    goes whole (the deliverables' rule), never shown with a hole. A count the page prints later (the Oracle's held counts) is the caller's
    to read first (oracle_harness.held_as_numbers)."""
    if not isinstance(text, str) or ("[[" not in text and "]]" not in text):
        return False
    t = unmark(text) if has_marks(text) else text
    return SENTINEL in resolve_track_text_marked(reg, t, track)


def unit_chain(why, base=None, default=None, omittable=False, generic=None):
    """BUILD IC-8 (D3): THE chain once the unit rule failed (`why`), in its order — base() (Block 2: Block 1's text of the same field
    under this run's rule) → rejected_kept_base; the field's default → fallback_default; "" where the page shows the field's place
    without it (omittable) → omitted, "<rule>_no_chain"; the prose default (generic) → fallback_default. → (text, disposition, reason,
    took)."""
    b = base() if callable(base) else None
    if isinstance(b, str) and b.strip():
        return b, "rejected_kept_base", why, "base"
    if isinstance(default, str) and default.strip():
        return default, "fallback_default", why, "default"
    if omittable or not (isinstance(generic, str) and generic.strip()):
        return "", "omitted", f"{why}_no_chain", "omit"
    return generic, "fallback_default", why, "generic"


def resolve_track_units(reg, value, track=None, html=False, base_of=None, sink=None, path=(), default_of=None, omittable_of=None,
                        generic=None, decided=None):
    """BUILD IC-8 (D3; V26 a disposition path): resolve_track_value under the unit rule — each string through resolve_unit: a sentence
    whose token names nothing omitted when the rule allows it (omitted, token_names_nothing); when it does not, THE chain (unit_chain):
    base_of(path) → default_of(path) → "" when omittable_of(path) → generic. A StageError live: the same chain, reason pipeline_order;
    under NAIPC_STRICT=1 it raises. `sink` receives one {path, disposition, reason, n, took} per string that lost a sentence or took its
    chain; `decided` (such entries by path: another variant's sink) makes this variant take the link that one took. → the value."""
    if isinstance(value, str):
        d = decided.get(path) if isinstance(decided, dict) else None
        took = (d or {}).get("took")
        dflt = default_of(path) if callable(default_of) else None
        omit_ = bool(omittable_of(path)) if callable(omittable_of) else False

        def base_():
            b = base_of(path) if callable(base_of) else None
            if not (isinstance(b, str) and b.strip()):
                return None
            rb = resolve_unit(reg, b, track, html)
            return rb["text"] if not rb["chain"] and isinstance(rb["text"], str) and rb["text"].strip() else None
        links = ("base", "default", "omit", "generic")
        if took in links:
            for link in links[links.index(took):]:
                if link == "base":
                    b = base_()
                    if b:
                        return b
                elif link == "default" and isinstance(dflt, str) and dflt.strip():
                    return dflt
                elif link == "omit" and (omit_ or not generic):
                    return ""
                elif link == "generic" and generic:
                    return generic
            return ""
        r = resolve_unit(reg, value, track, html)
        if not r["chain"]:
            if r["omitted"] and isinstance(sink, list):
                sink.append({"path": path, "disposition": "omitted", "reason": "token_names_nothing", "n": len(r["omitted"]),
                             "took": "sentences"})
            return r["text"]
        if took == "sentences":
            return remove_marked(resolve_track_text_marked(reg, value, track, html), [], plain=not html)[0]
        if r["reason"] == "pipeline_order":
            text, disp_, reason, took_ = (r["text"] or "", "rejected_kept_base" if r["source"] == "base" else "fallback_default",
                                          "pipeline_order", r["source"])
        else:
            text, disp_, reason, took_ = unit_chain(r["reason"], base_, dflt, omit_, generic)
        if isinstance(sink, list):
            sink.append({"path": path, "disposition": disp_, "reason": reason, "n": 1, "took": took_})
        return text
    if isinstance(value, list):
        return [resolve_track_units(reg, v, track, html, base_of, sink, path + (i,), default_of, omittable_of, generic, decided)
                for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k_: resolve_track_units(reg, v, track, html, base_of, sink, path + (k_,), default_of, omittable_of, generic, decided)
                for k_, v in value.items()}
    return value


def _unit_words(unit):
    """A unit id in a content line's words: "oracle.headlines.youth-handoffs" → "headline youth-handoffs"."""
    p = unit.split(".")
    if len(p) >= 3 and p[1] == "headlines":
        return f"headline {'.'.join(p[2:])}"
    return ".".join(p[1:]) if len(p) > 1 else unit


class Dispositions:
    """BUILD IC-8 (D3, I3): one terminal record per unit — its disposition (DISPOSITIONS), a fixed reason code, the source (candidate,
    base or default) and the edit line's op when one applies. A second terminal record for a unit raises under NAIPC_STRICT=1; live, the
    first stands and the conflict is recorded (the acceptance's row C3 fails). Written to checkpoints/dispositions_<run id>.jsonl (a
    header, the records, a summary): ids, unit paths, reason codes and counts only — never a value or a text."""

    def __init__(self, build_id, track=None, run_id=None):
        self.build_id, self.track, self.run_id = build_id, track, run_id
        self.records, self.conflicts, self.expected = [], [], []
        self._by = {}

    def expect(self, unit):
        if unit not in self._by and unit not in self.expected:
            self.expected.append(unit)

    def has(self, unit):
        return unit in self._by

    def add(self, unit, disposition, reason, source=None, op=None, **extra):
        if disposition not in DISPOSITIONS:
            raise ValueError(f"Dispositions.add: {disposition!r} is not one of {DISPOSITIONS}")
        if unit in self._by:
            why = f"a second outcome for {unit}: {disposition} after {self._by[unit]['disposition']}"
            if strict():
                raise StageError(why)
            self.conflicts.append({"unit": unit, "disposition": disposition, "reason": reason, "first": self._by[unit]["disposition"]})
            return self._by[unit]
        r = {"unit": unit, "disposition": disposition, "reason": reason, "source": source or "candidate"}
        if op:
            r["op"] = op
        r.update({k: v for k, v in extra.items() if isinstance(v, (int, str, bool)) or v is None})
        self.records.append(r)
        self._by[unit] = r
        try:                            # BUILD IC-8 (D14, I8): the finalizer's branch — the outcome and its reason code
            import naipc_requirements as _NR
            _NR.mark_path(_NR.path_id("unit", disposition, reason), track=self.track or None)
        except Exception:  # noqa: BLE001
            pass
        return r

    def summary(self):
        c = {k: 0 for k in DISPOSITIONS}
        for r in self.records:
            c[r["disposition"]] += 1
        return {"units": len(self.records), "counts": c, "conflicts": len(self.conflicts),
                "missing": [u for u in self.expected if u not in self._by],
                "kept_from_block1": [r["unit"] for r in self.records if r["disposition"] == "rejected_kept_base"]}

    def content(self):
        """BUILD IC-8 (D3): the track status's "content" — counts only: the units, each disposition's count, the conflicts and the
        expected units that have no record."""
        s = self.summary()
        return {"units": s["units"], **s["counts"], "conflicts": s["conflicts"], "missing": len(s["missing"])}

    def fit(self, unit, fitted, valid="kept_valid", source=None, op=None, **extra):
        """BUILD IC-8 (D1, D3): a unit's one record from its display fit (fit_shown's (text, disposition, reason)) and the outcome its text
        had before the fit (`valid`: kept_valid, replaced_valid or added_valid — its edit's, or Block 1's): kept whole, the unit keeps
        `valid` with the fit's reason ("within", "over_target"); a trim, a chain's link or an omission is the unit's outcome, with the
        link's source (_fit_source)."""
        _t, d, r = fitted
        if d == "kept_valid":
            return self.add(unit, valid, r, source, op, **extra)
        return self.add(unit, d, r, _fit_source(d, r, source), op, **extra)

    def content_line(self, name):
        s = self.summary()
        c, kb = s["counts"], s["kept_from_block1"]
        return (f"Content — {name}: {c['omitted']} omitted, {len(kb)} kept from Block 1"
                + (f" ({', '.join(_unit_words(u) for u in kb)})" if kb else "")
                + f", {c['fallback_default']} defaults, {c['trimmed']} trimmed; units {s['units']}")

    def write(self, path, identity=None):
        """None, or why the file was not written (temp file + os.replace). BUILD IC-8 (D9): every line carries the invocation that wrote
        it, and the file keeps the run's other invocations' lines — a re-run of the track in its run adds its block and never replaces
        another invocation's (an invocation's own block is replaced by itself only)."""
        p = Path(path)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        inv_n = os.environ.get("NAIPC_INVOCATION_NUMBER", "").strip()     # BUILD IC-8 (D8): the invocation that wrote it
        inv_ = {"invocation_id": os.environ.get("NAIPC_INVOCATION_ID", "").strip() or None,
                "invocation_number": int(inv_n) if inv_n.isdigit() else None}
        head = {"schema": DISPOSITIONS_SCHEMA, "build": self.build_id, "track": self.track, "run_id": self.run_id, **inv_,
                "schema_version": 2, **{k: v for k, v in (identity or {}).items() if k != "invocation"}}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            keep, cur = [], None            # the other invocations' lines, as written (BUILD IC-8 D9: no record lost)
            if p.is_file():
                for ln in p.read_text(encoding="utf-8").splitlines():
                    try:
                        x = json.loads(ln)
                    except ValueError:
                        continue
                    if not isinstance(x, dict):
                        continue
                    if x.get("line") == "header":
                        cur = x.get("invocation_id")
                    if (x.get("invocation_id") or cur) != inv_["invocation_id"]:
                        keep.append(ln)
            lines = [dict(head, line="header")] + [dict(r, line="unit", **inv_) for r in self.records] + \
                    [dict(c, line="conflict", **inv_) for c in self.conflicts] + [dict(self.summary(), line="summary", **inv_)]
            tmp.write_text("".join(k_ + "\n" for k_ in keep) + "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                           encoding="utf-8")
            os.replace(tmp, p)
            return None
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"


def read_dispositions(path, scope="active invocation"):
    """BUILD IC-8 (D3): (summary, header) of a dispositions file — the summary line as written (None when the file has none). BUILD IC-8
    (D9): one invocation's block, read through naipc_requirements.read_records — `scope`'s (the folder's active invocation by default);
    a file whose lines name no invocation, its last block."""
    head = summ = None
    try:
        import naipc_requirements as _NR
        r = _NR.read_records(path, scope, strict=False)
        rows = r.lines if r.invocation else r.run_lines
    except Exception:  # noqa: BLE001
        return summ, head
    for x in rows:
        if x.get("line") == "header":
            head = x
        elif x.get("line") == "summary":
            summ = x
    return summ, head


def resolve_track_value(reg, value, track=None, html=False):
    """resolve_track_text over a value's every string (lists and dicts walked): (value, [the sentences left out])."""
    if isinstance(value, str):
        return resolve_track_text(reg, value, track, html)
    if isinstance(value, list):
        out, gone = [], []
        for v in value:
            r, g = resolve_track_value(reg, v, track, html)
            out.append(r)
            gone += g
        return out, gone
    if isinstance(value, dict):
        out, gone = {}, []
        for k_, v in value.items():
            r, g = resolve_track_value(reg, v, track, html)
            out[k_] = r
            gone += g
        return out, gone
    return value, []


# ═══ BUILD IC-3 (D10, Q-SWITCH): IC-2's text-field selection, frozen — NAIPC_IC3_PROMPT=0 sends IC-2's request bodies byte for
# byte (done-test 11), and IC-2 listed the data's text fields with its own number pattern. Copied from IC-2's module by line
# (ic3_dev: the freeze script), every name prefixed; used by the prompt's switch-off path only — never by the count, the quote or
# the reader (one reader, D1). ═══
_IC2_NUM_RE = re.compile(r"(?<![\w.$])(\$?)(\d[\d,]*(?:\.\d+)?)(%?)|(?<![\w])(eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
                    r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)"
                    r"(?:-(?:one|two|three|four|five|six|seven|eight|nine|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth))?(?![\w])", re.I)

_IC2_MONTH = (r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|"   # B3 (Phase 4,
          r"Nov(?:ember)?|Dec(?:ember)?)\b\.?")    # review b F13): a month is a whole word ("Marketing 12%" is no date)

_IC2_COUNT_NOUNS = tuple(UNIT_NOUNS) + ("beds", "calls", "units")     # BUILD IC (D6, as the prompt names them; review b F8: no singular "call",
                                                                   # "home", "case" — "2024 call volume" is a year)

_IC2_EXEMPT_PATTERNS = (
    # BUILD IC (D6, the N3 probes): a bare d/d is a fraction ("1/3 of intakes") unless a date cue stands beside it — a year after it,
    # "on", "since", "until", "through", "before", "after" or "between" before it, or a weekday name; or its second number is 13 to 31
    # (review b F8: "by 6/30", "the 6/30 deadline" — a fraction in thirtieths is no figure the prose writes)
    ("a date", re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+Z?)?\b|\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/\d{2,4}\b(?![%/])|"
                          r"(?i:\b(?:on|since|until|through|before|after|between|(?:mon|tues|wednes|thurs|fri|satur|sun)day,?)\s+)(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])\b(?![%/])|"
                          r"(?<![\d/.])(?:0?[1-9]|1[0-2])/(?:1[3-9]|2\d|3[01])\b(?![%/]|\.\d)|"
                          + _IC2_MONTH + r"\s+\d{1,2}(?![\d%])(?:,\s*\d{4})?|\b\d{1,2}\s+" + _IC2_MONTH + r"(?:\s+\d{4})?")),
    ("a time", re.compile(r"\b\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?\b|\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)(?![\w])|\b\d{1,2}[ap](?:\s*[-–]\s*\d{1,2}[ap])?\b|\b24/7\b", re.I)),
    ("a phone number", re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\b1-8\d\d-\d{3}-\d{4}\b")),
    ("a year", re.compile(r"(?i:\b(?:in|by|since|from|until|through|during|before|after|of|for)\s+)(?:19|20)\d{2}(?![\d%,]|\.\d)"   # IC (review b
                          r"(?:\s*[-–]\s*(?:19|20)?\d{2}(?![\d%]))?|"                                       # F8): after a cue, a year
                          r"(?<![\d$.,])(?:19|20)\d{2}(?![\d%,]|\.\d)(?!\s+(?:" + "|".join(sorted(_IC2_COUNT_NOUNS, key=len, reverse=True)) + r")\b)"
                          r"(?:\s*[-–]\s*(?:19|20)?\d{2}(?![\d%]))?")),        # BUILD IC (D6): "2040 beds" is a count
    ("a regulation or code", re.compile(r"\b\d+\s+(?:CFR|U\.S\.C\.)\s+[\d.()a-z§-]+|\bSOC\s+\d{2}-\d{4}\b|\b(?:CFR|U\.S\.C\.)\s+\d+[\d.()a-z-]*", re.I)),
    ("a label", re.compile(r"\b(?:Years?|Phases?|Tiers?|Steps?|Blocks?|Option|Gate|Section|Rule|Appendix|Table|Level)[\s-]\d{1,2}(?:\s*[-–]\s*\d{1,2})?\b"
                           r"|\b\d{1,2}(?:st|nd|rd|th)\b(?!\s+percentile)|\b(?:[Aa]ges?|[Aa]ged)\s+\d{1,2}(?![\d%]|\.\d)(?:\s*(?:[-–]|to|and)\s*\d{1,2}(?![\d%]|\.\d))?"
                           r"|\b(?:[Aa]ges?|[Aa]ged)\s+(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty)"
                           r"(?:-[a-z]+)?\b")),   # an age (in words too: "aged nineteen" — run turn R4)
    # B3 (run turn, the second R4): a numbered document ("Statistical Brief #311") and a compound name — a word, a hyphen, a number
    # ("P60-283", "age-21", "Status-001", "COVID-19") — are names, not figures; never before a % or a decimal
    ("a document or item number", re.compile(r"#\d+(?:-\d+)*(?![\d%]|\.\d)")),
    ("a code or compound name", re.compile(r"\b(?!(?i:to|in|of|by|under|over|out|per)-)[A-Za-z][A-Za-z0-9]*-\d+(?:-\d+)*(?![\d%]|\.\d)")),   # IC (D6): not "30-to-40", "one-in-5"
)                                                                   # bounds a cohort; a number with % after it is never an age (review b F13)

_IC2_SEP = re.compile(r"\s+[/|—–-]\s+|\s*[,;:()]\s*")

def _ic2_name_forms(n):
    """The forms of a digit-bearing name in the data that the text may use (B3, Phase 4, review b F3): the whole name, each part
    between separators ("211 Kansas / United Way" → "211 Kansas"), and the digit word beside a capitalized word ("Station 7",
    "7 Shelter"); a bare three-digit name (an endpoint's number, "311") as itself. Each is matched as whole words."""
    n = re.sub(r"\s+", " ", n).strip()
    if re.fullmatch(r"\d{3}", n):
        return {n}
    if not re.search(r"\d", n) or not re.search(r"[A-Za-z]", n) or len(n) < 3:
        return set()
    out = {n}
    for part in _IC2_SEP.split(n):
        part = part.strip()
        if not (re.search(r"\d", part) and re.search(r"[A-Za-z]", part) and len(part) >= 3):
            continue
        out.add(part)
        m3 = re.match(r"(\d{3})\s+[A-Za-z]", part)      # B3 (run turn, the second R4): a name that begins with a three-digit number is
        if m3:                                          # known by that number alone ("211 Kansas / United Way" → "211"; B2's live
            out.add(m3.group(1))                        # lesson of 11/03 — a correct evidence sentence was removed for "through 211")
        w = part.split()
        for i, x in enumerate(w):
            if re.search(r"\d", x):
                if i + 1 < len(w) and w[i + 1][:1].isupper():
                    out.add(f"{x} {w[i + 1]}")
                if i > 0 and w[i - 1][:1].isupper():
                    out.add(f"{w[i - 1]} {x}")
    return out

class _Ic2Exempter:
    """The run's own non-figures: the ZIPs, record ids and digit-bearing names its data holds, beside the fixed patterns."""

    def __init__(self, zips=(), record_ids=(), names=()):
        self.zips = {z for z in zips if isinstance(z, str) and re.fullmatch(r"\d{5}", z)}
        self.ids = sorted({r for r in record_ids if isinstance(r, str) and re.search(r"\d", r)}, key=len, reverse=True)
        self.names = sorted({x for n in names if isinstance(n, str) for x in _ic2_name_forms(n)}, key=len, reverse=True)[:600]
        self._rx = (re.compile(r"(?<![\w])(?:" + "|".join(re.escape(x) for x in self.ids + self.names) + r")(?![\w])")
                    if (self.ids or self.names) else None)

    @classmethod
    def from_data(cls, raw, records_fn):
        zips, ids, names = set(), set(), set()

        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif isinstance(v, str):
                k = key.lower()
                if "zip" in k or "address" in k:
                    zips.update(re.findall(r"\b\d{5}\b", v))
                if k == "id":
                    ids.add(v)
                if NAME_KEYS.search(k):         # BUILD IC (D6): "programNotes", "propertyNotes" and a headline are prose, not names
                    names.add(v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and "zip" in key.lower():
                zips.add(str(int(v)).zfill(5))
        for ep, payload in (raw or {}).items():
            names.update(re.findall(r"(?<!\d)\d{3}(?!\d)", str(ep)))     # B3 (Phase 4, review b F3): an endpoint's number is a name —
            try:                                                        # "311-health" makes 311 the city's line, not a count
                walk(records_fn(payload))
            except Exception:  # noqa: BLE001
                continue
        return cls(zips, ids, names)

    def spans(self, text):
        """(start, end, reason) of every exempt stretch of plain text."""
        out = []
        for why, rx in _IC2_EXEMPT_PATTERNS:
            out += [(m.start(), m.end(), why) for m in rx.finditer(text)]
        if self._rx:
            out += [(m.start(), m.end(), "a record id or a name in the data") for m in self._rx.finditer(text)]
        out += [(m.start(), m.end(), "a ZIP in the data") for m in re.finditer(r"\b\d{5}\b", text) if m.group(0) in self.zips]
        return out


def text_fields_ic2(raw, records_fn, limit=30):
    """BUILD IC-2 (D2): the data's text fields that hold numbers the checker would count — [{endpoint, field, records, of, rid, number}],
    names, ids, ZIPs, addresses, phones, dates and hours left out (they are exempt or not figures). What the prompt lists to quote from."""
    ex = _Ic2Exempter.from_data(raw, records_fn)
    out = {}
    for ep, payload in (raw or {}).items():
        try:
            recs = [r for r in records_fn(payload) if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            continue
        reg_ = Registry()
        reg_.set_data({ep: payload}, records_fn)
        key = reg_.record_key(ep)
        for r in recs:
            for fld, v in r.items():
                if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    v = " · ".join(v)
                if (not isinstance(v, str) or NAME_KEYS.search(fld) or fld.lower() == "id" or fld == key   # the record's own id (a census
                        or re.search(r"(?i)zip|address|phone|url|date|time|updated|hours|email|website|tract|fips|geoid|route|(?:^|[a-z_])id$|ids$", fld)):
                    continue                                                                  # tract on KC) is no figure to quote; a route a name
                sp = ex.spans(v)
                nums = [m for m in _IC2_NUM_RE.finditer(v) if not any(a <= m.start() < b for a, b, _w in sp)]
                if not nums:
                    continue
                dig = next((x.group(0).strip(" ,.;:") for x in nums if re.search(r"\d", x.group(0))), None)   # review b #13: an example in digits
                d = out.setdefault((ep, fld), {"endpoint": ep, "field": fld, "records": 0, "of": len(recs), "rid": None, "number": None})
                if d["number"] is None and dig:
                    d.update(rid=str(r.get(key)) if key and r.get(key) is not None else None, number=dig)
                d["records"] += 1
    return list(out.values())[:limit]
