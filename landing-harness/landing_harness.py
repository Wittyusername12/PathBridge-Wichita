#!/usr/bin/env python3
"""
Landing Page Harness — NAIPC Competition
Generates the unified "Full Stack" hub page linking Oracle, Architect, and Muse.

Usage:
    python landing_harness.py run \
        --oracle-dir ../oracle-harness/checkpoints \
        --oracle-url https://ctrl-alt-defeat.netlify.app \
        --architect-url https://ctrl-alt-defeat-arch.netlify.app \
        --muse-url https://ctrl-alt-defeat-muse.netlify.app

No Claude API calls — purely deterministic. Reads Oracle checkpoints for stats.
"""

import argparse
import html as _html
import io
import json
import os
import re
import sys
import time
from pathlib import Path

# Fix Windows encoding
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from jinja2 import Environment, FileSystemLoader

try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False

HARNESS_DIR = Path(__file__).parent
OUTPUT_DIR = HARNESS_DIR / "output"
if str(HARNESS_DIR.resolve()) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR.resolve()))
import naipc_requirements as NR  # noqa: E402 — P0.6: the facts layer, the label, short money (one byte-identical copy per folder)
import naipc_registry as REG  # noqa: E402 — BUILD B3 (D2): the Oracle's figures by its registry (one byte-identical copy per folder)
REG.admit_readers(NR)   # BUILD IC-5 (D1 e): the contract and the coverage map, wherever this process reads them
REG.set_admit_rewrite(NR.ic5_prompt())   # BUILD IC-5 (D1 f): the rewrite only with this build's prompt switch on
NR.safe_stdio()   # P0.6 red team RT-3: printing that cannot raise on any platform (the block above covers Windows only)


def _oracle_b1_ma(oracle_path):
    """BUILD IC-8 (D3): Block 1's analysis the Oracle's chain takes at Block 2 — its checkpoints/block1, when this run's curveball record
    (the Oracle's curveball.json of this run) exists and names that Block 1 run; else None (Block 1, or no record of this run)."""
    rid = os.environ.get("NAIPC_RUN_ID", "").strip()
    if not rid:
        return None
    rec, _w = NR.read_curveball_json(Path(oracle_path) / NR.CURVEBALL_FILE, os.environ.get("NAIPC_CHALLENGE", "").strip() or None, rid)
    if not isinstance(rec, dict):
        return None
    meta, _w = NR.read_json(Path(oracle_path) / NR.BLOCK1_DIR / NR.BLOCK1_META)
    if not isinstance(meta, dict) or (rec.get("block1_run_id") and meta.get("run_id") != rec.get("block1_run_id")):
        return None
    d, _w = NR.read_json(Path(oracle_path) / NR.BLOCK1_DIR / "master_analysis.json")
    data = REG.admit(d, "saved", "block1 master_analysis.json").get("data") if isinstance(d, dict) else None
    return data if isinstance(data, dict) else None


TOOK_SOURCE = {"base": "base", "default": "default", "generic": "default", "omit": "candidate"}   # BUILD IC-8 (d16d): a link's record source


def _drop_removed(reg, raw, removed):
    """BUILD IC-8 (d16d; RECORD 2): `raw` (an Oracle field with its tokens) less each sentence the Oracle's integrity check removed from it
    (its registry's integrity.removed for the field) — a sentence matched by the key the Oracle's page gave it (REG.sentence_key of the
    sentence as resolved there) or by its plain words. → (the text, [(the key's first 12, its reason)] for each sentence left out)."""
    if not (isinstance(raw, str) and raw.strip() and removed):
        return raw, []
    keys = {str(r.get("key")): r for r in removed if r.get("key")}
    words = {REG.plain_sentence(str(r.get("sentence") or "")): r for r in removed}
    out, gone, last = [], [], 0
    for a, b in REG.sentence_spans(raw):
        h = reg.resolve(raw[a:b], mode="html")[0] if reg is not None else raw[a:b]
        k, w = REG.sentence_key(h), REG.plain_sentence(h)
        r = keys.get(k) or words.get(w)
        if r is None:
            continue
        out.append(raw[last:a])
        last = b
        rule = "token_names_nothing" if "[?]" in str(r.get("sentence")) else str((r.get("rules") or ["unknown"])[0])
        gone.append((k[:12], rule))
    out.append(raw[last:])
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip(), gone


def load_oracle_checkpoints(oracle_dir: str) -> dict:
    """Load Oracle checkpoint JSONs for the venture pitch."""
    result = {}
    oracle_path = Path(oracle_dir)

    ma_path = oracle_path / "master_analysis.json"
    if ma_path.exists():
        with open(ma_path, encoding="utf-8-sig") as f:
            data = REG.admit(json.load(f), "saved", ma_path.name).get("data", {})   # BUILD IC-5 (D1): a file a run wrote
        result["program_name"] = data.get("program_name", "")
        result["brand_html"] = data.get("brand_html", "")
        result["domain"] = data.get("domain", "")
        result["city_name"] = data.get("city_name", "")
        result["badge_text"] = data.get("badge_text", "")
        result["hero_lead"] = data.get("hero_lead", "")
        result["solution_body"] = data.get("solution_body", "")
        result["domain_label"] = data.get("domain_label") or ""   # P0.6 (D3)
        # BUILD B3 (D2): the Oracle's text holds [[id]] tokens — each becomes the Oracle's registry figure; with no registry of this run, a
        # sentence holding one is left out (named here)
        reg, rwhy = REG.read_oracle(oracle_path)
        result["_reg"] = reg
        # BUILD IC-8 (D3): each takes THE unit rule's decision the Oracle's page takes for the same field (the same registry, the same
        # rule), once, here: as written when its dead sentences may go; else Block 1's (Block 2), else the Oracle's default for it, else
        # the prose default — the plain words below and the lead's spans (hero_lead_raw) both from the decided text
        b1_ = None
        # BUILD IC-8 (d16d; RECORD 2): the Oracle page's own decisions, as its registry records them — a field's unit link (integrity.units)
        # and each sentence its check removed (integrity.removed: a dead token, or a residual the hub's unit rule never sees); a registry
        # with no "units" (IC-7 wrote it) leaves the unit rule on a dead token, as before
        integ_ = (getattr(reg, "integrity", None) or {}) if reg is not None else {}
        units_ = {u.get("field"): u for u in (integ_.get("units") or []) if isinstance(u, dict)}       # the last decision per field
        decided = result["_decided"] = {}
        for k in ("badge_text", "hero_lead", "solution_body"):
            v_ = result.get(k)
            u_ = units_.get(k)
            if u_ and u_.get("took") in ("base", "default", "omit", "generic"):
                b1_ = b1_ if b1_ is not None else (_oracle_b1_ma(oracle_path) or {})
                took_ = u_["took"]
                result[k] = {"base": b1_.get(k) if isinstance(b1_.get(k), str) else "", "omit": "", "generic": REG.UNIT_DEFAULT,
                             "default": REG.oracle_default(k, NR.clean_text(result.get("program_name")))}[took_]
                decided[k] = {"disposition": u_.get("disposition"), "reason": u_.get("reason"), "source": TOOK_SOURCE[took_], "removed": []}
                print(f"  the Oracle's {k}: the Oracle's page took "
                      + {"base": "Block 1's", "default": "its default", "omit": "nothing", "generic": "the prose default"}[took_]
                      + f" ({u_.get('reason')}) — so does this page")
                continue
            gone_ = []
            if units_ or integ_.get("units") is not None:
                v_, gone_ = _drop_removed(reg, v_, [r for r in integ_.get("removed") or [] if isinstance(r, dict) and r.get("field") == k])
                result[k] = v_
                if gone_:
                    print(f"  the Oracle's {k}: {len(gone_)} sentence(s) its page left out ({', '.join(sorted({g[1] for g in gone_}))}) — "
                          "left out here too")
            why_ = REG.unit_why(reg, v_) if isinstance(v_, str) and v_.strip() and integ_.get("units") is None else None
            decided[k] = {"disposition": "kept_valid", "reason": "within", "source": "candidate", "removed": gone_}
            if why_:
                b1_ = b1_ if b1_ is not None else (_oracle_b1_ma(oracle_path) or {})
                bv_ = b1_.get(k)
                text_, d_, r_, took_ = REG.unit_chain(
                    why_, lambda: bv_ if isinstance(bv_, str) and bv_.strip() and not REG.unit_why(reg, bv_) else None,
                    REG.oracle_default(k, NR.clean_text(result.get("program_name"))), False, REG.UNIT_DEFAULT)
                result[k] = text_
                decided[k] = {"disposition": d_, "reason": r_, "source": TOOK_SOURCE.get(took_, "candidate"), "removed": []}   # IC-8 (d16d)
                print(f"  the Oracle's {k}: its sentences cannot go alone ({why_}) — "
                      + {"base": "Block 1's", "default": "the Oracle's default for it", "generic": "the prose default"}.get(took_, took_)
                      + ", as on the Oracle's page")
        result["hero_lead_raw"] = result["hero_lead"]     # BUILD IC-4 (D3): its tokens, for the lead this page prints as spans
        gone = 0
        for k in ("badge_text", "hero_lead", "solution_body"):
            result[k], n = REG.resolve_tree(reg, result.get(k))
            gone += n
        if gone:
            print(f"  the Oracle's text: {gone} sentence(s) with a figure left out — "
                  + (f"no registry of this run ({rwhy})" if reg is None else "a token in each names nothing (the unit rule let it go)"))

    fm_path = oracle_path / "financial_model.json"
    if fm_path.exists():
        with open(fm_path, encoding="utf-8-sig") as f:
            data = REG.admit(json.load(f), "saved", fm_path.name).get("data", {})   # BUILD IC-5 (D1)
        kpis = data.get("kpis") if isinstance(data.get("kpis"), dict) else {}
        reg = result.get("_reg")          # BUILD B3 (D1, D2): the SROI only from the registry, and only when it is at least 1
        e = reg.get("sroi") if reg is not None else None
        result["sroi_display"] = e["shown"] if e and e["value"] >= 1 else ""
        if e and e["value"] < 1:
            print(f"  the Oracle's SROI is {e['shown']} (below 1) — left out of this page; the Oracle's page shows it")
        result["cpb_y1"] = data.get("cpb_y1", 0)
        result["beneficiaries"] = data.get("beneficiaries", [])
        result["contract"] = kpis.get("contract")                 # P0.6 (D2): None when the model has no figure
        result["contract_source"] = kpis.get("contract_source")   # override | brief | proposed | cost_plan (absent before P0.6)
        result["unit_plural"] = kpis.get("unit_plural") or ""
        result["unit_singular"] = kpis.get("unit_singular") or ""

    return result


def hub_contract():
    """P0.6 (D1, D6): the contract pipeline.py exported (NAIPC_REQUIREMENTS), or None — never raises (the hub is built regardless)."""
    if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
        return None
    try:
        return REG.admit(NR.from_env(), "data", "contract")   # BUILD IC-5 (D1)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ contract not used ({type(e).__name__}: {str(e)[:160]})")
        return None


def hub_budget(oracle: dict, F: dict):
    """P0.6 (D2): (amount, source) of the budget the Oracle's financial model used; a model written before P0.6 (no contract_source)
    counts as the brief's only when its amount is the brief's; (None, None) with no figure — never a practice event's "$500K"."""
    amt = oracle.get("contract")
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or amt <= 0:
        return None, None
    src = oracle.get("contract_source")
    if src not in NR.BUDGET_SOURCES:
        src = "brief" if F.get("contract") is not None and round(amt) == F["contract"] else None
    return int(round(amt)), src


def load_summary(path: Path, what: str) -> dict:
    """P0.6 (D6): a track's run-stamped summary, used only when it is this run's (its run_id = NAIPC_RUN_ID, which pipeline.py sets);
    a missing, unreadable or other run's file → {} and a printed reason, so its counts are left out — never replaced by another number."""
    if not path.is_file():
        print(f"  ⚠ {what} not found: {path} — its counts are left out")
        return {}
    try:
        data = REG.admit(json.loads(path.read_bytes().decode("utf-8-sig")), "saved", path.name)   # BUILD IC-5 (D1)
    except (OSError, ValueError) as e:
        print(f"  ⚠ {what} unreadable ({type(e).__name__}) — its counts are left out")
        return {}
    if not isinstance(data, dict):
        print(f"  ⚠ {what} is not a JSON object — its counts are left out")
        return {}
    rid = os.environ.get("NAIPC_RUN_ID", "").strip()
    if rid and data.get("run_id") != rid:
        print(f"  ⚠ {what} is from run {data.get('run_id') or '(no run id)'}, not this run ({rid}) — its counts are left out")
        return {}
    print(f"  ✓ {what}: " + (f"this run's ({rid})" if rid else "run id not checked (NAIPC_RUN_ID unset — a standalone run)"))
    return data


HUB_UPDATE_MODES = {"applied": "The Oracle updated Block 1's analysis for the curveball with one call; Python recomputed every figure of its "
                               "financial model.",
                    "full": "The Oracle regenerated its analysis at the curveball (--curveball-full)."}


def _shown(v) -> str:
    return "—" if v is None else str(v)


def hub_curveball_panel(path):
    """P0.7 (D7, D12, D4): the panel "What changed at the curveball", from the Oracle's checkpoints/curveball.json only when its run id
    is this run's (NAIPC_RUN_ID, which pipeline.py sets) and its challenge this run's. The hub reads no change list and recomputes
    nothing: the endpoints, the alerts and the moved figures are the Oracle's record as written. Returns the panel's values as plain
    text (the template escapes each), or None with the reason printed. Never raises."""
    rid = os.environ.get("NAIPC_RUN_ID", "").strip()
    if not rid:
        print("  Curveball panel: none — NAIPC_RUN_ID is not set, so no curveball record can be taken as this run's")
        return None
    rec, why = REG.admit(NR.read_curveball_json(path, os.environ.get("NAIPC_CHALLENGE", "").strip() or None, rid), "saved", NR.CURVEBALL_FILE)   # BUILD IC-5 (D1)
    if rec is None:
        print(f"  Curveball panel: none — {why}")
        return None
    try:
        st = str(rec.get("update") or "")
        moved_any = bool([m for m in rec.get("moved") or [] if isinstance(m, dict)])   # review F8: --budget moves figures on this path too
        mode = HUB_UPDATE_MODES.get(st) or (
            "The Oracle's curveball update was declined by the model, so nothing of it was applied: its analysis is Block 1's."
            if st.startswith("failed: declined") else      # BUILD IC-4 (D11): a refusal, said as the Oracle's page says it
            ("The Oracle's curveball update did not complete: its analysis is Block 1's" + (", and the figures below moved for the reason each names."
             if moved_any else ", and so are its figures.")) if st.startswith("failed") else
            "The Oracle wrote its analysis fresh at the curveball: there was no Block 1 analysis to start from." if st.startswith("no base") else "")
        endpoints = [NR.endpoint_text(e) for e in rec.get("changed_endpoints") or [] if isinstance(e, dict)]
        unchanged = rec.get("unchanged")
        endpoints.append(f"{len(unchanged)} endpoint(s) unchanged" if isinstance(unchanged, list) else
                         "No Block 1 snapshot was found, so the data could not be compared: the announcement is the only change known."
                         if rec.get("snapshot") else   # the record's snapshot source is None when no change list of this run reached the Oracle
                         "No change list of this run was found, so the data could not be compared: the announcement is the only change known.")
        moved = [{"label": _shown(m.get("label")), "block1": _shown(m.get("block1")), "block2": _shown(m.get("block2")),
                  "because": ", ".join(str(b) for b in m.get("because") or [])} for m in rec.get("moved") or [] if isinstance(m, dict)]
        fm = rec.get("facts_moved") if isinstance(rec.get("facts_moved"), dict) else {}      # BUILD IC-4 (D3, F13): by group
        facts = [f"{_shown(m.get('label'))}: {_shown(m.get('block1'))} → {_shown(m.get('block2'))}" for g in ("data", "python")
                 for m in fm.get(g) or [] if isinstance(m, dict)]
        panel = {"first": NR.first_sentence(rec.get("announcement")), "mode": mode, "endpoints": endpoints,
                 "facts": facts, "facts_line": (f"Figures of the data that moved: {len(fm.get('data') or [])}; Python's figures from the records that "
                                                f"moved: {len(fm.get('python') or [])} (Block 1's registry against this run's)." if fm.get("compared")
                                                else ""),
                 "alerts": [NR.alert_line(*a) for a in rec.get("alerts") or [] if isinstance(a, (list, tuple)) and len(a) == 4],
                 "moved": moved,
                 "no_moved": ("No figure of the Oracle's financial model differs from Block 1's." if rec.get("moved_compared") else
                              "There are no Block 1 figures to compare with: every figure on the Oracle page is this run's.")}
    except Exception as e:  # noqa: BLE001 — a malformed record never stops the hub
        print(f"  Curveball panel: none — the curveball record is not usable ({type(e).__name__})")
        return None
    fm_ = rec.get("facts_moved") if isinstance(rec.get("facts_moved"), dict) else {}
    print(f"  ✓ Curveball panel: this run's curveball record (update {st or '?'}) — {len(rec.get('changed_endpoints') or [])} changed "
          f"endpoint(s), {len(panel['alerts'])} alert change(s) (added, removed, changed in text), {len(fm_.get('data') or [])} data figure(s) "
          f"and {len(fm_.get('python') or [])} of Python's moved (the two registries), {len(moved)} financial-model figure(s) moved")
    return panel


def generate_qr_svg(url: str) -> str:
    """Generate QR code SVG for the landing page URL."""
    if not HAS_SEGNO or not url:
        return ""
    try:
        qr = segno.make(url, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
        svg = REG.inline_svg(buf.getvalue().decode("utf-8"))       # BUILD IC-4 (D7, D8): no XML declaration in the body
        m = re.search(r'width="(\d+)" height="(\d+)"', svg)
        if m:
            svg = svg.replace(
                f'width="{m.group(1)}" height="{m.group(2)}"',
                f'width="{m.group(1)}" height="{m.group(2)}" viewBox="0 0 {m.group(1)} {m.group(2)}"'
            )
        return svg
    except Exception:
        return ""


def fmt_money(n: int) -> str:
    """Format contract value: 500000 → '$500K'."""
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"${n // 1_000}K"
    return f"${n:,}"


def fmt_num(n: int) -> str:
    """Format number: 12000 → '12,000'."""
    return f"{n:,}"


_TAG = re.compile(r"\s*\[CHALLENGE_DATA(?:[^\[\]]|\[[^\[\]]*\])*\]")
_COI = re.compile(r"\[{1,2}COI[_ ]?3[_ ]?YR\]{1,2}", re.IGNORECASE)


def plain_lead(text) -> str:
    """P0.6 review F5: the Oracle's hero_lead as plain text for this page — read from the raw master analysis, before the Oracle turns
    its [CHALLENGE_DATA: …] tags into badges, so the tags are removed here (the hub has no sources table), the cost-of-inaction token
    becomes the Oracle's own fallback words, and NR.clean_text removes markup. The template escapes what remains. Never raises."""
    if not isinstance(text, str):
        return ""
    return NR.clean_text(_COI.sub("avoidable downstream costs", _TAG.sub("", text)))


# BUILD IC-8 (D1): the hub's own cut (_cut_html, its _ABBREV_END) went with d1c — REG.fit_cap fits the lead on its shown text


def finalize_landing(checkpoint_dir, oracle):
    """BUILD IC-8 (d16d; RECORD 2: "the hub records no dispositions"): the hub's units — the Oracle's lead as this page takes it
    (landing.oracle.hero_lead: kept_valid within, or the Oracle page's unit link; each sentence the Oracle's page left out of it,
    landing.oracle.hero_lead.s.<key 12>, omitted with its reason), then each fit of this process that changed its text (REG.fit_units: the
    tagline, the preview). Written to checkpoints/dispositions_<run id>.jsonl with the content line. No Oracle lead (no Oracle folder):
    its unit is not recorded. Under NAIPC_STRICT=1 an exception raises; live it is named and the page stands. → the content or None."""
    try:
        disp = REG.Dispositions(NR.BUILD_ID, "landing", NR.run_id())
        d_ = ((oracle or {}).get("_decided") or {}).get("hero_lead")
        if d_ is not None or (oracle or {}).get("hero_lead") is not None:
            d_ = d_ or {"disposition": "kept_valid", "reason": "within", "source": "candidate", "removed": []}
            disp.expect("landing.oracle.hero_lead")
            disp.add("landing.oracle.hero_lead", d_["disposition"], d_["reason"], d_.get("source") or "candidate")
            for k_, why_ in d_.get("removed") or []:
                u_ = f"landing.oracle.hero_lead.s.{k_ or 'nokey'}"
                if not disp.has(u_):
                    disp.expect(u_)
                    disp.add(u_, "omitted", why_, "candidate")
        REG.fit_units(disp, "landing")
        why_ = disp.write(Path(checkpoint_dir) / f"dispositions_{NR.run_id()}.jsonl", {"invocation": os.environ.get("NAIPC_INVOCATION_NUMBER")})
        print(f"  {disp.content_line('Hub')}" + (f" (dispositions not written: {why_})" if why_ else ""))
        return disp.content()
    except Exception as e:  # noqa: BLE001 — §1: live, the page stands and the record's loss is named
        if REG.strict():
            raise
        print(f"  ⚠ the hub's dispositions not recorded ({type(e).__name__}) — the page stands")
        return None


def lead_html(reg, text, limit):
    """BUILD IC-4 (D3, D8): the Oracle's lead (plain_lead: its tags gone, its tokens kept) as this page's HTML — each figure its registry
    span (the one registry, resolved as HTML; the words around escaped), cut at the last sentence end within `limit` characters a
    reader sees, else at a word with "…". With no registry of this run every sentence holding a token is left out (resolve_for_track).
    Never raises."""
    if not isinstance(text, str) or not text.strip():
        return ""
    try:
        text = REG.fit_cap(text, "hub.lead", limit, reg)     # BUILD IC-8 (D1): the stored lead fitted on its shown text, then built
        parts = re.split(r"(\[\[[^\[\]]*\]\])", text)
        h_ = "".join(p if i % 2 else REG.out_text(p, "html_text") for i, p in enumerate(parts))    # BUILD IC-5 (D2 c): words, escaped
        out, _n = REG.resolve_for_track(reg, h_, html=True)
        return out
    except Exception:  # noqa: BLE001 — the plain text, escaped, as before IC-4
        return REG.out_text(REG.fit_cap(REG.resolve_for_track(None, text)[0], "hub.lead", limit), "html_text")


def infer_diagram_routing(endpoints: list) -> list:
    """The data-flow diagram's routing: P0.6 (D7) — every endpoint feeds all three tracks, because pipeline.py passes the same
    --endpoints to the Oracle, the Architect and the Muse (the diagram draws what each harness receives, nothing else).

    Returns list of dicts: [{"name": "<endpoint>", "tracks": ["oracle", "architect", "muse"]}, ...]
    """
    return [{"name": ep, "tracks": ["oracle", "architect", "muse"]} for ep in endpoints]


# Track metadata for the diagram (fixed positions and colors)
DIAGRAM_TRACKS = {
    "oracle": {"label": "Oracle", "x": 260, "color": "#8b5cf6", "rgba": "139,92,246"},
    "architect": {"label": "Architect", "x": 520, "color": "#22c55e", "rgba": "34,197,94"},
    "muse": {"label": "Muse", "x": 780, "color": "#ec4899", "rgba": "236,72,153"},
}


def main():
    parser = argparse.ArgumentParser(description="Landing Page — Full Stack Hub")
    sub = parser.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run")
    run_p.add_argument("--oracle-dir", default="", help="Path to Oracle checkpoint directory")
    run_p.add_argument("--architect-dir", default="", help="Path to Architect checkpoint directory")
    run_p.add_argument("--oracle-url", default="https://ctrl-alt-defeat.netlify.app", help="Oracle Netlify URL")
    run_p.add_argument("--architect-url", default="https://ctrl-alt-defeat-arch.netlify.app", help="Architect Netlify URL")
    run_p.add_argument("--muse-url", default="https://ctrl-alt-defeat-muse.netlify.app", help="Muse Netlify URL")
    run_p.add_argument("--landing-url", default="https://ctrl-alt-defeat-hub.netlify.app", help="Landing page Netlify URL (used for QR code)")
    run_p.add_argument("--program-name", default="", help="Override program name")
    run_p.add_argument("--endpoints", default="", help="Comma-separated endpoint names for the diagram (default: the contract's; none without one)")
    run_p.add_argument("--muse-dir", default="", help="Path to the Muse checkpoint directory (its run-stamped muse_summary.json)")
    run_p.add_argument("--accent-hex", default="#8b5cf6", help="Brand accent color")
    args = parser.parse_args()

    if args.cmd != "run":
        parser.print_help()
        return
    NR.paths_home(HARNESS_DIR / "checkpoints", "landing")       # BUILD IC-8 (D14, I8): this run's path markers

    t0 = time.time()
    print("=== Landing Page — Full Stack Hub ===")
    NR.write_track_status(HARNESS_DIR / "checkpoints", "landing", "started")     # BUILD IC-6 (D7)

    doc = hub_contract()                                    # P0.6 (D1, D6): the contract pipeline.py exported, or None
    F = NR.facts(doc)

    # Load Oracle checkpoints
    oracle = {}
    if args.oracle_dir:
        raw_path = args.oracle_dir.strip().strip('"').strip("'").rstrip('\\').rstrip('/')
        oracle_path = Path(raw_path)
        if oracle_path.exists():
            oracle = load_oracle_checkpoints(raw_path)
            print(f"  ✓ Oracle loaded: {oracle.get('program_name', '?')}")
        else:
            print(f"  ⚠ Oracle path not found: {oracle_path}")
    # BUILD IC-8 (P4.4, p44v): the run's names before any text is fitted (the Oracle's data, as the page pass reads it); the page pass
    # reads this object (P4.find-measure-class)
    raw0_ = REG.oracle_raw(oracle_path) if args.oracle_dir else None
    names_ = REG.set_measure_names(REG.ReaderNames(raw0_, NR.records, before=getattr(oracle.get("_reg"), "_before_raw", None)) if raw0_ else None)
    # P0.7 (D7, D12): "What changed at the curveball" — the Oracle's curveball.json of THIS run only (the hub reads no change list)
    cb_panel = hub_curveball_panel(Path(raw_path) / NR.CURVEBALL_FILE) if args.oracle_dir else None

    # P0.6 (D6): the Architect's and the Muse's counts only from this run's summaries (left out otherwise)
    architect, muse = {}, {}
    if args.architect_dir:
        arch_raw = args.architect_dir.strip().strip('"').strip("'").rstrip('\\').rstrip('/')
        architect = load_summary(Path(arch_raw) / "architect_summary.json", "Architect summary")
    if args.muse_dir:
        muse_raw = args.muse_dir.strip().strip('"').strip("'").rstrip('\\').rstrip('/')
        muse = load_summary(Path(muse_raw) / "muse_summary.json", "Muse summary")

    # Build context — P0.6 (D2): a figure the Oracle's model does not hold is left out, never "$500K", "4.0 : 1" or "12,000"
    program_name = args.program_name or oracle.get("program_name") or "Civic Navigator"
    program_name_html = NR.clean_brand_html(oracle.get("brand_html"), program_name)
    city = oracle.get("city_name", "")
    domain = REG.fit_text(oracle.get("domain_label") or oracle.get("domain"), "oracle.domain_label") or "Civic Access"   # IC-8 (D1)
    amt, src = hub_budget(oracle, F)
    contract = NR.money_short(amt) if amt else ""           # the hero number, in the counter's format (parseTarget)
    contract_label = NR.BUDGET_SHORT.get(src, "Budget")     # P0.6 (D2): one short vocabulary with the Oracle's simulator
    contract_meta = {"brief": "contract", "override": "budget (override)", "proposed": "proposed budget",
                     "cost_plan": "proposed budget"}.get(src, "budget")
    contract_phrase = (f"proposed {contract} budget" if src in ("proposed", "cost_plan") else f"{contract} {contract_meta}") if contract else ""
    sroi = oracle.get("sroi_display") or ""
    beneficiaries = oracle.get("beneficiaries") or []
    b0 = beneficiaries[0] if beneficiaries else None
    beneficiaries_y1 = fmt_num(int(b0)) if isinstance(b0, (int, float)) and not isinstance(b0, bool) and b0 > 0 else ""
    unit_p = oracle.get("unit_plural") or F["unit_plural"]
    unit_s = oracle.get("unit_singular") or F["unit_singular"]
    print(f"  Budget: {f'{contract} — {NR.BUDGET_SOURCES[src]}' if contract and src else (contract or 'none in the Oracle model — left out')}")

    # Build venture pitch paragraph from Oracle's data (P0.6: escaped names, the budget's source, the unit; no "live" claim)
    hero_lead = plain_lead(oracle.get("hero_lead", ""))     # P0.6 review F5: the raw analysis's tags never reach this page
    solution_body = oracle.get("solution_body", "")
    name_e = _html.escape(program_name)
    reg = oracle.get("_reg")          # BUILD B3 (D2): the Oracle's figures in this sentence are its registry spans
    if cb_panel:                      # BUILD IC-4 (D3, D1): the announcement's words, each quantity its mention's span
        cb_panel["first_h"] = reg.quote_html("announcement", cb_panel.get("first") or "") if reg is not None else REG.esc(cb_panel.get("first") or "")
    lead_raw = plain_lead(oracle.get("hero_lead_raw", ""))   # BUILD IC-4 (D3): the lead with its tokens, printed as spans (lead_html)
    budget_h, sroi_h = REG.span_if(reg, "budget", contract), REG.span_if(reg, "sroi", sroi)    # BUILD IC-4 (D3): wherever printed
    ben_h = REG.span_if(reg, "beneficiaries_y1", beneficiaries_y1)
    contract_h = _html.escape(contract_phrase).replace(_html.escape(contract), budget_h, 1) if contract_phrase else ""
    case_sentence = (f"The business case shows a <strong>{sroi_h}</strong> social return" if sroi else
                     "The business case is built" if (contract_phrase or beneficiaries_y1) else "")
    if case_sentence:
        if contract_phrase:
            case_sentence += f" on a <strong>{contract_h}</strong>"
        if beneficiaries_y1:
            case_sentence += f", serving <strong>{ben_h}</strong> {_html.escape(unit_p)} in Year 1"
        case_sentence += ". "
    if hero_lead and solution_body:
        # Use Oracle's own crisis framing + solution summary
        pitch = (
            f"<strong>{name_e}</strong> addresses a compounding civic crisis" + (f" in {_html.escape(city)}" if city else "") + ". "
            f"{lead_html(reg, lead_raw, 500)} "
            f"This submission presents three integrated artifacts — an investable business case (Oracle), "
            f"a working resident navigation tool (Architect), and a bilingual community outreach campaign (Muse) — "
            f"all built from the same challenge API data, fetched at run time, by a single competitor using Claude. "
            f"{case_sentence}"
            f"Same data. Same brand. Same story."
        )
    else:
        pitch = (
            f"<strong>{name_e}</strong> is a data-driven civic program presented as three integrated artifacts: "
            f"an investable business case (Oracle), a working resident navigation tool (Architect), "
            f"and a bilingual community outreach campaign (Muse). "
            f"All three were built from the challenge API data, fetched at run time, by a single competitor using Claude. "
            f"Same data. Same brand. Same story."
        )

    # QR code for the landing page itself
    qr_svg = generate_qr_svg(args.landing_url)

    # Build diagram routing from endpoints — P0.6 (D11): the contract's endpoints when --endpoints is not given, else none
    if args.endpoints.strip():
        endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    else:
        endpoints = NR.endpoint_names(doc) if doc else []
        print("  Endpoints: " + (f"the contract's {len(endpoints)}" if doc else "none (no --endpoints, no contract)"))
    diagram_endpoints = infer_diagram_routing(endpoints)
    ep_failed = sorted(set(endpoints) & set(getattr(oracle.get("_reg"), "unavailable", None) or ()))   # IC-4 Phase 7 (review b9): the
    ep_count = len(endpoints) - len(ep_failed)                                                            # endpoints that loaded
    print(f"  ✓ Diagram: {ep_count} endpoints, {sum(len(e['tracks']) for e in diagram_endpoints)} connections"
          + (f" — {len(ep_failed)} did not load this run: {', '.join(ep_failed)}" if ep_failed else ""))

    # P0.6 (D6): counts only from this run's summaries — never the endpoint count standing in for resources or areas
    rc, zc = architect.get("resource_count"), architect.get("area_count")   # P0.6 review F4: the Architect page's "Areas Monitored"
    resource_count = str(rc) if isinstance(rc, int) and not isinstance(rc, bool) and rc >= 0 else ""
    zip_count = str(zc) if isinstance(zc, int) and not isinstance(zc, bool) and zc > 0 else ""
    mp, mc = muse.get("personas"), muse.get("channels")
    muse_personas = mp if isinstance(mp, int) and not isinstance(mp, bool) and mp > 0 else None
    muse_channels = mc if isinstance(mc, int) and not isinstance(mc, bool) and mc > 0 else None
    if architect:
        print(f"  ✓ Using Architect data: {resource_count or '-'} resources, {zip_count or '-'} areas")

    ctx = {
        "program_name": program_name,
        "program_name_html": program_name_html,
        "tagline": REG.fit_text(hero_lead, "hub.hero_lead") if "hero_lead" in oracle else "A data-driven civic solution",   # printed with | e (F5)
        # BUILD IC-4 (D3, D8): the tagline as the page prints it — the lead's figures as spans, cut at a sentence end (lead_html)
        "tagline_html": lead_html(reg, lead_raw, 220) if "hero_lead" in oracle else "A data-driven civic solution",
        "budget_h": budget_h, "sroi_h": sroi_h, "ben_h": ben_h, "phrase_h": contract_h,
        "accent_hex": (args.accent_hex if isinstance(args.accent_hex, str) and args.accent_hex[:1] == "#" and len(args.accent_hex) in (4, 5, 7, 9)
                       and all(ch_ in "0123456789abcdefABCDEF" for ch_ in args.accent_hex[1:]) else "#8b5cf6"),   # BUILD IC-5 (D2 e): a colour, or the default
        "domain_label": domain,
        "oracle_url": args.oracle_url,
        "architect_url": args.architect_url,
        "muse_url": args.muse_url,
        "contract": contract,                    # "" → the stat and every budget clause are left out
        "contract_label": contract_label,        # P0.6 (D2): "Contract" | "Budget (override)" | "Proposed budget"
        "contract_meta": contract_meta,
        "contract_phrase": contract_phrase,
        "sroi": sroi,
        "beneficiaries_y1": beneficiaries_y1,
        "unit_plural": unit_p,
        "unit_singular": unit_s,
        "Unit_plural": unit_p[:1].upper() + unit_p[1:],
        "resource_count": resource_count,
        "zip_count": zip_count,
        "ep_count": ep_count,
        "ep_failed": ep_failed,          # IC-4 Phase 7 (review b9): named beside the count
        "muse_personas": muse_personas,
        "muse_channels": muse_channels,
        "pitch": pitch,
        "preview": REG.shown_text(REG.page_preview(pitch, reg, html=True)[0], reg),   # BUILD IC-8 (D7): the pitch's whole leading sentences
        "competition_label": NR.label_from_env(doc),   # P0.6 (D6): COMPETITION_LABEL, else the contract's, else "NAIPC"
        "qr_svg": qr_svg,
        "diagram_endpoints": diagram_endpoints,
        "diagram_tracks": DIAGRAM_TRACKS,
        "model_label": "claude opus 5.5",  # the hub boot line (P0.11 D7); one source for the label
        "term_oracle": contract_phrase or "business case",
        "cb_panel": cb_panel,                    # P0.7 (D7): None in Block 1, for another run's record, or without NAIPC_RUN_ID
        # BUILD IC-6 (D7, I6): each track's state this run, from its status file — main content, never success without a file
        "track_lines": REG.admit(NR.track_lines({"oracle": Path(raw_path) if args.oracle_dir else None,
                                                 "architect": Path(arch_raw) if args.architect_dir else None,
                                                 "muse": Path(muse_raw) if args.muse_dir else None}),
                                 "saved", "the tracks' status files"),        # read back from files: the one way in (IC-5 D1)
        "run_id": os.environ.get("NAIPC_RUN_ID", "").strip(),
    }
    # BUILD IC-7 (D7, I5): a card, a deck button and a link only for a track whose page is this run's built page (NR.page_is_this_runs:
    # its naipc-run, no not-built mark, its status "built"); else its line in their place. Run by hand (no run id): IC-6's links.
    rid_ = ctx["run_id"]
    dirs_ = {"oracle": args.oracle_dir, "architect": args.architect_dir, "muse": args.muse_dir}
    ctx["built"] = {t: (NR.page_is_this_runs(Path(d).parent / "output", t, rid_)[0] if d else False) if rid_ else True for t, d in dirs_.items()}
    ctx["line_of"] = dict(zip(("oracle", "architect", "muse"), ctx["track_lines"]))
    # BUILD IC-8 (P4.3a; F24, RS-3): the invocations this hub was built from — its own and each track's selected one (naipc-invocations)
    sel_ = NR.selected_invocations({"oracle": Path(raw_path) if args.oracle_dir else None, "architect": Path(arch_raw) if args.architect_dir else None,
                                    "muse": Path(muse_raw) if args.muse_dir else None}, rid_ or None)
    own_ = os.environ.get("NAIPC_INVOCATION_NUMBER", "").strip()
    ctx["invocations"] = ("; ".join([f"Hub {own_ or 'none'}"] + [f"{NR.TRACK_NAMES[t]} {n if n is not None else 'none'}" for t, n in sel_.items()])
                          if rid_ else "")

    # Render template
    env = REG.page_env(Environment, FileSystemLoader(str(HARNESS_DIR)), authored=False, build=NR.BUILD_ID)  # IC-5 (D2 a); IC-8 (d16c)
    env.globals["PB"] = {"python": REG.PY_BLOCK}      # BUILD IC-4 (D7): whose each region's numbers are — the page's line counts them
    template = REG.page_template(env, "landing_template.html.j2")
    html = template.render(**ctx)       # BUILD IC-5 (D2 b): finalize sees every printed value (T12's tag_tree(ctx) gone)
    for p_ in sorted(set(REG.page_problems())):          # BUILD IC-5 (D2 b): what finalize escaped or repaired, by template line
        NR.say(f"    ⚠ authorship: {p_}")
    for t_ in sorted(set(REG.page_tags())):              # a tag finalize met at output (shown as words; a test fails on one)
        NR.say(f"    ⚠ a tag at output, shown as words: {t_}")

    # BUILD IC-4 (D7): the page's integrity line, from its own text, the Oracle's registry of this run and the exempter of its data
    raw_ = REG.oracle_raw(oracle_path) if args.oracle_dir else None
    reg_ = oracle.get("_reg")
    if reg_ is not None and raw_:
        reg_.set_data(raw_, NR.records)
    html, notes_ = REG.place_notes(html, reg_)       # BUILD IC-6 (D9): each "sources disagree" a note after its sentence, before the line
    html, rw_ = REG.reader_page(html, names_)      # BUILD IC-8 (P4.4, p44v): the names the measure read
    if REG.reader_words_line(rw_):      # BUILD IC-8 (d12c): what a reader meets, in a reader's words — before the page's line
        print(f"  {REG.reader_words_line(rw_)}")
    html, pline = REG.place_page_line(html, reg_, REG.Exempter.from_data(raw_, NR.records) if raw_ else None)
    if pline:
        print(f"  {pline}")

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "index.html"
    if REG.notes_words(notes_):         # BUILD IC-6 (D9): the page's notes, said and recorded
        print(f"  {REG.notes_words(notes_)}")
    nw_ = REG.write_page_notes(HARNESS_DIR / "checkpoints", notes_, NR.run_id())
    if nw_:
        print(f"  ⚠ checkpoints/{REG.NOTES_FILE} not written ({nw_})")
    out_path.write_text(REG.strip_build_notes(html), encoding="utf-8")     # IC-4 Phase 7 (review c21): no build note in its scripts
    print(f"  ✓ Written to {out_path} ({len(html):,} bytes)")

    for ln_ in ctx["track_lines"]:
        print(f"  {ln_}")
    content_ = finalize_landing(HARNESS_DIR / "checkpoints", oracle)        # BUILD IC-8 (d16d; RECORD 2): the hub's units, recorded
    NR.write_track_status(HARNESS_DIR / "checkpoints", "landing", "built", page_run=NR.page_run_of(out_path),     # BUILD IC-6 (D7)
                          content=content_)
    elapsed = time.time() - t0
    print(f"=== Done in {elapsed:.1f}s ===")

    # Auto-open browser
    import webbrowser
    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    try:
        main()
    except BaseException as _e:     # BUILD IC-6 (D7): the top-level handler — "failed" in the status file (best effort)
        NR.track_failed(HARNESS_DIR / "checkpoints", "landing", _e)
        raise
