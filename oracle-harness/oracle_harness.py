#!/usr/bin/env python3
"""
NAIPC Oracle Track — Competition-Day Harness
=============================================
Single-script pipeline: fetch data → analyze → generate → render → deploy.

Usage:
    python oracle_harness.py run --api-url https://aipromptchamp.com/api
    python oracle_harness.py run --api-url URL --stage master_analysis  # re-run one stage
    python oracle_harness.py curveball --api-url URL                    # handle curveball
    python oracle_harness.py deploy                                     # deploy latest HTML

Requires:
    pip install anthropic jinja2 tenacity rich typer requests
    npm install -g @mermaid-js/mermaid-cli
    Set ANTHROPIC_API_KEY, NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID env vars.
"""
from __future__ import annotations

import json, os, re, subprocess, sys, tempfile, time, traceback, hashlib, html as _html, shutil, urllib.parse
import contextlib, copy, io   # P0.7: Block 1's base copied, attribution runs silenced
import threading             # BUILD IC-3 (D5): the timer that closes a silent stream at its end time
import inspect               # BUILD IC-4 (D9): a wrapped call's max_tokens and model, for its expected length
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
import typer
from jinja2 import Environment, FileSystemLoader, Undefined
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError   # P0.7 r2 (E3 a): a check's errors read as plain dicts (errors(include_url=False, include_input=False))
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table
from tenacity import Retrying, retry_if_exception

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import naipc_requirements as NR  # noqa: E402 — P0.3: the brief, the coverage map, the page attributes and the gate
import naipc_engine as ENG  # noqa: E402 — BUILD B1: the prioritization model (P1), Python only, from the challenge's engine sidecar
import naipc_registry as REG  # noqa: E402 — BUILD B3 (P0.4, P0.5): the provenance registry, the [[id]] tokens and the integrity check
REG.admit_readers(NR, ENG)   # BUILD IC-5 (D1 e): the contract, the coverage map and the engine sidecar, wherever this process reads them (was: BUILD IC (D1, review a F8)'s NR.load patch)
REG.set_admit_rewrite(NR.ic5_prompt())   # BUILD IC-5 (D1 f): the rewrite only with this build's prompt switch on
NR.safe_stdio()   # P0.6 red team RT-3: printing that cannot raise (a cp1252 pipe stopped this harness at its first '→')

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────
OPUS = "claude-opus-5-5"  # the only model constant (P0.11 D7)
TEMPLATE_PATH = Path(__file__).parent / "template.html.j2"
CHECKPOINT_DIR = Path("./checkpoints")
OUTPUT_DIR = Path("./output")
# P0.6 (D10, Rule 7): the vendored libraries, shipped beside this file — Chart.js inlined into the page, Mermaid copied beside it
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
CHARTJS_FILE, MERMAID_FILE = "chart.umd.js", "mermaid.min.js"
CHARTJS_SHA256 = "2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb"   # Chart.js 4.4.7 dist/chart.umd.js
MERMAID_SHA256 = "581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8"   # Mermaid 11.17.2 dist/mermaid.min.js

console = Console()
app = typer.Typer(help="NAIPC Oracle Track Harness")

# Defensive: strip whitespace from API key.
# Windows CMD 'set VAR=value && next' includes the space before && in the value,
# causing httpcore to reject it as "Illegal header value" → misleading "Connection error".
for _env_key in ("ANTHROPIC_API_KEY", "NETLIFY_AUTH_TOKEN"):
    if _env_key in os.environ:
        os.environ[_env_key] = os.environ[_env_key].strip()

# ─────────────────────────────────────────────────────────────────────
# PIPELINE RUNNER — DAG with checkpointing (Implementation Guide §1)
# ─────────────────────────────────────────────────────────────────────
_STAGE_REGISTRY: dict[str, dict] = {}
PURE_STAGES = ("prioritization",)   # BUILD B1 (review #9): no model call — computed when a `--stage` run finds no checkpoint

def stage(name: str, depends_on: list[str] | None = None):
    """Decorator to register a pipeline stage with dependencies."""
    def decorator(fn):
        _STAGE_REGISTRY[name] = {"fn": fn, "depends_on": depends_on or [], "name": name}
        return fn
    return decorator

class PipelineRunner:
    def __init__(self):
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.stages = dict(_STAGE_REGISTRY)
        self.results: dict[str, Any] = {}
        self.cost_input = self.cost_output = 0
        self.start_time = time.monotonic()
        self.failed: str | None = None

    def _topo_sort(self) -> list[str]:
        in_degree = {n: 0 for n in self.stages}
        children = defaultdict(list)
        for name, info in self.stages.items():
            for dep in info["depends_on"]:
                children[dep].append(name)
                in_degree[name] += 1
        queue = sorted(n for n, d in in_degree.items() if d == 0)
        order = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for child in sorted(children[node]):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        return order

    def save_checkpoint(self, name: str, data: Any):
        path = CHECKPOINT_DIR / f"{name}.json"
        payload = {"stage": name, "ts": datetime.now(timezone.utc).isoformat(), "data": data}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic

    def load_checkpoint(self, name: str) -> Any | None:
        path = CHECKPOINT_DIR / f"{name}.json"
        if path.exists():
            # BUILD IC (D1, review a F8): a checkpoint is an input too when a stage runs alone (R3 rebuilds from checkpoints b3 wrote)
            kind = "data" if name == "data_loading" else "saved"    # BUILD IC-5 (D1 c): the organizers' data, as the data stage admits it
            got = REG.admit(json.loads(path.read_text(encoding="utf-8"))["data"], kind, f"checkpoint {name}")   # BUILD IC-5 (D1)
            if name == "data_loading" and isinstance(got, dict) and isinstance(got.get("raw"), dict):
                REG.set_endpoints(list(got["raw"]))         # a resumed run checks a data tag's names, as the data stage does
            return got
        return None

    def run_one(self, name: str, force=False):
        info = self.stages[name]
        if not force:
            cached = self.load_checkpoint(name)
            if cached is not None:
                self.results[name] = cached
                console.print(f"  [dim]↩ {name} (cached)[/dim]")
                return cached
        # Load dependencies from cache or results
        deps = {}
        for dep in info["depends_on"]:
            deps[dep] = self.results.get(dep) or self.load_checkpoint(dep)
            if deps[dep] is None:
                raise RuntimeError(f"Dependency '{dep}' missing for stage '{name}'")
        t0 = time.monotonic()
        console.print(f"  [cyan]▶ {name}[/cyan]", end="")
        NR.progress_open()                 # BUILD IC-7 (D8): any line the stage prints closes this one first
        try:
            result = info["fn"](deps)
            elapsed = time.monotonic() - t0
            self.results[name] = result
            self.save_checkpoint(name, result)
            console.print(f"  [green]✓[/green] ({elapsed:.1f}s)")
            NR.progress_end()
            return result
        except Exception as e:
            elapsed = time.monotonic() - t0
            NR.say(f"  ✗ {type(e).__name__}: {e} ({elapsed:.1f}s)")          # BUILD IC-7 (D8): plain — markup ate "[...]" from the text
            raise

    def run_all(self, only: str | None = None, force_stages: set[str] | None = None):
        force_stages = force_stages or set()
        order = self._topo_sort()
        console.print(f"\n[bold]Pipeline: {' → '.join(order)}[/bold]\n")
        for name in order:
            if only and name != only:
                # Still load cached result for downstream deps
                cached = self.load_checkpoint(name)
                if cached:
                    self.results[name] = cached
                elif name in PURE_STAGES:      # BUILD B1 (review #9): checkpoints from before the stage existed — compute it (Python only)
                    try:
                        self.run_one(name, force=True)
                    except Exception:  # noqa: BLE001 — the stage never raises; a dependency it lacks is the stage's own report
                        pass
                continue
            try:
                self.run_one(name, force=(name in force_stages or name == only))
            except Exception as e_:
                self.failed = name
                self.failed_exc = e_           # BUILD IC-7 (D7): its plain words for the status (_track_end)
                console.print(f"\n[red bold]Pipeline stopped at '{name}'[/red bold]")
                traceback.print_exc()
                break
        # Always try to render partial HTML
        self._render_partial()

    def _render_partial(self):
        """Generate deployable HTML from whatever stages completed."""
        try:
            ctx = build_template_context(self.results)
            render_html(ctx)
            console.print(f"\n[green]✓ HTML written to {OUTPUT_DIR / 'index.html'}[/green]")
        except Exception as e:
            NR.say(f"\n⚠ Partial HTML render failed: {type(e).__name__}: {e}")       # BUILD IC-7 (D8)


# ─────────────────────────────────────────────────────────────────────
# ANTHROPIC CLIENT SETUP
# ─────────────────────────────────────────────────────────────────────
def get_anthropic_client():
    import anthropic, httpx
    # P0.11 D5/D8: the SDK never retries on its own, and a stalled stream fails after 120 s of silence
    # instead of the SDK's 600 s default (anthropic _constants.py:9).
    return anthropic.Anthropic(max_retries=0,
                               timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))

# BUILD B3 (D1): the benchmark sheet as EXTERNAL registry entries — (id, value, format, unit, what it is, the source as cited,
# verification). verification is None until a primary source is read (Phase 3's check): then {"verified": "yes", "url", "quote",
# "checked"}; the page marks every entry without one. The system prompt's sheet below is generated from this table.
BENCHMARKS_CHECKED = "2026-11-09"     # BUILD B3 (Phase 3): each benchmark checked against its primary source (WebSearch/WebFetch)


def _v(url, quote):
    """A benchmark's check: the primary source's address and its own words, on BENCHMARKS_CHECKED."""
    return {"verified": "yes", "url": url, "quote": quote, "checked": BENCHMARKS_CHECKED}


BENCHMARKS = (   # (id, value, fmt, unit, what it is, source, its check) — B3: the ER cost and the wages corrected to their primary sources
    ("ext.us_food_insecurity_2023", 0.135, "pct", "", "U.S. households food insecure, 2023",
     "USDA ERS, Household Food Security in the United States in 2023 (ERR-337)",
     _v("https://primary.ers.usda.gov/webdocs/publications/109896/err-337_summary.pdf",
        "The remaining 13.5 percent (18.0 million households) were food insecure.")),
    ("ext.us_poverty_rate_2023", 0.111, "pct", "", "U.S. official poverty rate, 2023", "Census Bureau, Poverty in the United States: 2023 (P60-283)",
     _v("https://census.gov/library/publications/2024/demo/p60-283.html",
        "In 2023, the official poverty rate fell 0.4 percentage points to 11.1 percent.")),
    ("ext.us_renters_cost_burdened_2023", 0.497, "pct", "", "U.S. renter households paying over 30% of income for housing, 2023",
     "Census Bureau, 2023 American Community Survey (its bulletin of Sept 2024)",
     _v("https://content.govdelivery.com/accounts/USCENSUS/bulletins/3b55028",
        "Over 21 million renter households spent more than 30% of their income on housing costs in 2023, representing nearly half "
        "(49.7%) of the 42.5 million renter households")),
    ("ext.us_uninsured_rate_2023", 0.08, "pct", "", "U.S. uninsured rate, 2023", "Census Bureau, Health Insurance Coverage in the United States: 2023",
     _v("https://www.census.gov/library/stories/2024/09/health-insurance-coverage.html",
        "The percentage of people without health insurance coverage in 2023 was 8%")),
    ("ext.us_uninsured_people_2023", 26400000, "count", "people", "people in the U.S. without health insurance, 2023",
     "Census Bureau, Health Insurance Coverage in the United States: 2023",
     _v("https://census.gov/content/dam/Census/library/visualizations/2024/comm/health-insurance-coverage.pdf",
        "26.4 million people were uninsured in 2023")),
    ("ext.er_visit_cost", 750, "money", "USD per visit", "average cost of a treat-and-release emergency department visit, 2021",
     "AHRQ HCUP Statistical Brief #311 (Sept 2024)",
     _v("https://hcup-us.ahrq.gov/reports/statbriefs/sb311-ED-visit-costs-2021.pdf", "with an average cost per visit of $750")),
    ("ext.wage_chw_median", 51850, "money", "USD a year", "community health workers' median pay, May 2025",
     "BLS Occupational Outlook Handbook (May 2025 wages)",
     _v("https://www.bls.gov/ooh/community-and-social-service/community-health-workers.htm",
        "The median annual wage for community health workers was $51,850 in May 2025.")),
    ("ext.wage_social_worker_median", 61780, "money", "USD a year", "social workers' median pay, May 2025",
     "BLS Occupational Outlook Handbook (May 2025 wages)",
     _v("https://www.bls.gov/OOH/community-and-social-service/social-workers.htm",
        "The median annual wage for social workers was $61,780 in May 2025.")),
    ("ext.wage_program_director_median", 80390, "money", "USD a year", "social and community service managers' median pay, May 2025",
     "BLS Occupational Outlook Handbook, SOC 11-9151 (May 2025 wages)",
     _v("https://www.bls.gov/OOH/management/social-and-community-service-managers.htm",
        "The median annual wage for social and community service managers was $80,390 in May 2025.")),
    ("ext.indirect_deminimis_rate", 0.15, "pct", "", "de minimis indirect cost rate, of modified total direct costs", "2 CFR 200.414(f) (eCFR, current)",
     _v("https://www.ecfr.gov/current/title-2/subtitle-A/chapter-II/part-200/subpart-E/subject-group-ECFRd93f2a98b1f6455/section-200.414",
        "may elect to charge a de minimis rate of up to 15 percent of modified total direct costs (MTDC)")),
    ("ext.us_people_with_sud_2023", 48500000, "count", "people", "people in the U.S. with a substance use disorder, 2023",
     "SAMHSA, 2023 National Survey on Drug Use and Health",
     _v("https://www.samhsa.gov/data/sites/default/files/NSDUH%202023%20Annual%20Release/2023-nsduh-main-highlights.pdf",
        "In 2023, 48.5 million people aged 12 or older (or 17.1%) had a substance use disorder (SUD) in the past year")),
)


def _benchmark_lines():
    """The system prompt's sheet. BUILD IC-3 (D7; step 56): a label that spells a count is printed, with the switch on, in words that
    spell none (_IC3_BENCH_WORDS); BENCHMARKS keeps IC-2's label, which the page shows and the switch-off system prompt prints (IC-2's
    byte for byte)."""
    words = _IC3_BENCH_WORDS if ic3_prompt() else {}
    return "\n".join(f"[[{b[0]}]] = {REG.fmt_value(b[1], b[2])} — {words.get(b[0], b[4])} | {b[5]}" for b in BENCHMARKS)


_IC3_BENCH_WORDS = {"ext.us_renters_cost_burdened_2023": "U.S. renter households cost-burdened by housing (the Census Bureau's measure), 2023"}


_IC2_OFF_WORDS = ("0", "off", "false", "no")


def ic2_prompt() -> bool:
    """BUILD IC-2 (D5): the switch — the writer's forms in the prompts (the <registry> block states them; three other sites point to it)
    unless NAIPC_IC2_PROMPT is 0, off, false or no (any case), which sends BUILD IC's prompts byte for byte. Read once, when the module
    loads (review b #14: one run, one answer — the pointing clauses below are built at load too). The resolver accepts the forms either way."""
    return _IC2_ON


_IC2_ON = os.environ.get("NAIPC_IC2_PROMPT", "").strip().lower() not in _IC2_OFF_WORDS


def ic3_prompt() -> bool:
    """BUILD IC-3 (D10, Q-SWITCH): the switch — this build's request wording (the ids Python owns, the list of mentions, the one set of
    rules, the examples) unless NAIPC_IC3_PROMPT is 0, off, false or no (any case), which sends IC-2's request bodies byte for byte (their
    text-field list too, by IC-2's frozen selection, REG.text_fields_ic2). Read once, when the module loads. The resolver and the count
    ignore it: every form resolves either way."""
    return _IC3_ON


_IC3_ON = os.environ.get("NAIPC_IC3_PROMPT", "").strip().lower() not in _IC2_OFF_WORDS


# IC-2 (D5): the pointing clauses — the system prompt's, the update's rule 8 and the deliverables' first lines name the forms <registry> lists
_IC2_RULE8 = ", or another form <registry> lists" if ic2_prompt() else ""
# review b #2: the stress case's share — every live run of 11/09 copied the prompt's "20% fewer" as digits (its one unlinked number); with
# the switch on, the sensitivity line names the comparison Python computes and the prompt's label of the stress figure states no digits
_IC2_STRESS = ("[[calc:pct_less|stress_beneficiaries_y1|beneficiaries_y1]] fewer reached" if ic2_prompt() else "20% fewer reached")
_IC2_STRESS_LABEL = ("Year-1 {p} served in Python's stress case (fewer reached)" if ic2_prompt() else "Year-1 {p} served if 20% fewer are reached")
# review b #3: a quote carried into Block 2 is checked again on this run's text
_IC2_RULE9 = (" A quote of the data's text is checked again on this run's data: where the changes name its record, amend the sentence "
              "that quotes it." if ic2_prompt() else "")
_FIGURE_RULE = ("Every figure in your text is a token that names it — never digits you type (one of these two, or another form the request's "
                "<registry> lists):" if ic2_prompt() else "Every figure in your text is a token that names it — never digits you type:")

# BUILD IC-3 (D10): the system prompt keeps two sentences — the reason, and that every quantity is a token; the forms are stated once,
# by number_rules, in each request after its data. The switch off: IC-2's lines, byte for byte.
_SYSTEM_FORMS = ("A reader must be able to check where every quantity on this page comes from. So every quantity you write is a token: "
                 "each request lists\nthe ids and the forms after its data (<registry>, <mentions>, <number_rules>)."
                 if ic3_prompt() else
                 _FIGURE_RULE + "\n[[<id>]] — a figure from the registry the request lists: the data's own numbers, figures Python computes, "
                 "the brief's facts and the\n  benchmarks below. Python replaces each token with the figure and its source, and counts every "
                 "number on the page that is not one.\n[[model:<number>|<what it is>]] — a number of your own that no id holds (a proposed "
                 "target, a phase's days): shown as your proposal.")

# BUILD IC-4 (D9; open item 6): no request asks to see reasoning (the vendor's refusals page) — beside an inference the writer says what it
# rests on, the switch on; IC-3's words with it off
_ESTIMATE_WORDS = "(say what it rests on)" if NR.ic4_prompt() else "(explain reasoning)"

# Common system prompt with benchmark reference sheet
# Per Implementation Guide §11: three-tier attribution protocol; BUILD B3 (D2): every figure a token
SYSTEM_PROMPT = f"""You are an expert cross-domain civic data analyst preparing a government
service contract proposal. Your methodology:
1. Never assume the domain — infer it from data
2. Distinguish what data SHOWS vs. what you INFER
3. Flag confidence levels (HIGH/MEDIUM/LOW) for all inferences

<attribution_rules>
{_SYSTEM_FORMS}
[CHALLENGE_DATA: endpoint-name] — the source badge of a claim from the provided JSON, citing the SPECIFIC endpoint
  Form: [CHALLENGE_DATA: <endpoint-name>], with the name exactly as the data section writes it
  For claims that cross-reference multiple endpoints: [CHALLENGE_DATA: <endpoint-name>, <endpoint-name>]
  The endpoint name must match the <endpoint name="..."> tags in the data section below.
  NEVER use bare [CHALLENGE_DATA] without specifying which endpoint — judges need to trace every number.
[ESTIMATE] — your inference {_ESTIMATE_WORDS}
If you cannot verify a source, say so. Do NOT fabricate statistics.
</attribution_rules>

<benchmark_reference_sheet>
National benchmarks — cite each by its token; the page shows its source and whether it was checked against its primary source.
{_benchmark_lines()}
</benchmark_reference_sheet>
"""

# ─────────────────────────────────────────────────────────────────────
# LLM CALL HELPERS with retry + cost tracking
# ─────────────────────────────────────────────────────────────────────
# P0.11 D5 — one retry owner per call site. The SDK never retries (max_retries=0 in
# get_anthropic_client); these wrappers decide by error class, and never retry a spend cap,
# a bad request, a refusal or a cut-off answer.
MASTER_STAGE_BUDGET_S = 840     # 900 s pipeline limit (pipeline.py) - 60 s for the Oracle's other stages
ORACLE_RATE = (108.8, "the C4 probe, 34,110 tokens in 313.5 s")   # BUILD IC-4 (D9): the stated rate when this machine's call log
#                                                                    has no finished line (NR.measured_rate); replaces the 500 s reserve
_clock = time.monotonic         # patched in tests
_sleep = time.sleep             # patched in tests
_PROCESS_T0 = _clock()          # P0.3 (D9): when this Oracle process started — the end time of a call with no stage of its own counts from here
# BUILD IC-6 (D1): every call's limit, effort and stream mode are its profile's (NR.CALL_PROFILES: "oracle/master", "oracle/deliverables",
# "oracle/update", "oracle/update_whole", "oracle/repair", "oracle/diagram_repair"); its start reads the profile's plan, never max_tokens (D2)


def is_retryable(exc: BaseException) -> bool:
    """True only for failures that an identical second request can fix."""
    import anthropic, httpx
    if isinstance(exc, IncompleteResponse):
        return True
    if isinstance(exc, GenerationError):
        return False
    if isinstance(exc, (anthropic.APIConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        if exc.status_code == 200:  # raised mid-stream from an SSE error event
            return err.get("type") in ("overloaded_error", "api_error")
        if exc.status_code >= 500:
            return True
        if exc.status_code == 429:
            details = err.get("details") if isinstance(err.get("details"), dict) else {}
            if details.get("error_code") == "enforced_spend_limit_reached":
                return False
            return exc.response.headers.get("retry-after") is not None
    return False


def _retry_wait(exc: BaseException, failed_attempts: int) -> float:
    """max(retry-after when 0 < RA <= 60, exponential 5 -> 30 s)."""
    base = min(30.0, 5.0 * 2 ** (failed_attempts - 1))
    try:
        ra = float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return base
    return max(ra, base) if 0 < ra <= 60 else base


ORACLE_END_S = 850              # BUILD IC-3 (D5): a call with no stage budget of its own ends by the Oracle's 900 s limit less 50 s
if NR.oracle_window()[:2] != NR.ORACLE_WINDOW_HAND[:2]:    # BUILD IC-8 (D17): Block 1's window, from the profiles — both attempts fit
    MASTER_STAGE_BUDGET_S, ORACLE_END_S = NR.oracle_window()[:2]   # (Block 2 and a run by hand keep 840 and 850, and a test's patch)
_STAGE_END: dict = {}           # BUILD IC-3 (D5): retry label → the clock time its stage budget ends (set by _retrying)


def _end_at(label: str | None = None) -> float:
    """BUILD IC-3 (D5): a call's end time — its stage's budget less what has run (_retrying's), else the Oracle's own limit. BUILD IC-6 (D3,
    I3): the earlier of that and the block's end less the later tracks' reserves and the Oracle's own time outside its calls
    (NR.block_end_for; none when the harness is run by hand)."""
    own = _STAGE_END.get(label) or (_PROCESS_T0 + ORACLE_END_S)
    blk = NR.block_end_for("oracle", _clock)
    return own if blk is None else min(own, blk)


def _retrying(label: str, attempts: int, budget_s: float | None = None, profile: str | None = None):
    """Decorator: tenacity owns the retries; the predicate above decides; a stage budget can stop them. BUILD IC-3 (D5): the stage's
    end time is set for the call (_end_at) — every attempt's end is the same budget's end. BUILD IC-4 (D9): every attempt starts only
    when it can finish — NR.start_check: the time left to its end time, less the wait before it, against its expected length; the first
    attempt of a call with a stage of its own begins that stage and starts. BUILD IC-6 (D2): the expected length is the plan of the call's
    profile (`profile`, its key: its planning tokens at the slowest rate on record or on this machine, else ORACLE_RATE) — never max_tokens."""
    def deco(fn):
        sig = inspect.signature(fn)

        def wrapper(*args, **kwargs):
            start = _clock()
            if budget_s is not None:
                _STAGE_END[label] = start + budget_s
            end = _end_at(label)
            ba = sig.bind_partial(*args, **kwargs)
            ba.apply_defaults()
            model = ba.arguments.get("model", OPUS)
            ok, note, _need = NR.start_check(profile, CHECKPOINT_DIR, model, ORACLE_RATE, left=end - _clock(), begins=budget_s is not None,
                                             stage_s=budget_s)
            if not ok:
                raise StageBudgetExceeded(f"{label}: {note}")
            if note:
                NR.say(f"    {label}: {note}")
            no_time = []

            def _stop(rs):
                if rs.attempt_number >= attempts:
                    return True
                ok_, note_, _n = NR.start_check(profile, CHECKPOINT_DIR, model, ORACLE_RATE, left=end - _clock(),
                                                wait=_retry_wait(rs.outcome.exception(), rs.attempt_number), retry=True)
                if not ok_:
                    no_time.append(note_)
                    return True
                return False

            def _before_sleep(rs):
                exc = rs.outcome.exception()
                console.print(f"  [yellow]↻ {label}: retry {rs.attempt_number} of {attempts - 1} "
                              f"after {type(exc).__name__}[/yellow]")

            try:
                return Retrying(retry=retry_if_exception(is_retryable), stop=_stop,
                                wait=lambda rs: _retry_wait(rs.outcome.exception(), rs.attempt_number),
                                sleep=lambda s: _sleep(s), before_sleep=_before_sleep,
                                reraise=True)(fn, *args, **kwargs)
            except Exception as exc:
                if no_time:
                    raise StageBudgetExceeded(f"{label}: {no_time[0]}") from exc
                raise
        wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
        return wrapper
    return deco


_ATTEMPT_N: dict = {}     # P0.7 (D10): attempts per call label, for the partial-answer file names


def _call_ident(label, profile, second=None):
    """BUILD IC-8 (D8): a model call's identity (NR.new_call: its call id, sequence, generation attempt and journal) as model_call's
    keyword arguments."""
    ident = NR.new_call(CHECKPOINT_DIR, label, profile, 2 if second is not None else 1)
    return {"journal": ident["journal"], "ident": ident}


def _attempt(label: str) -> int:
    _ATTEMPT_N[label] = max(_ATTEMPT_N.get(label, 0) + 1, NR.next_attempt(CHECKPOINT_DIR, label))   # BUILD IC-7 (D10): a re-run
    return _ATTEMPT_N[label]                                                                          # never overwrites a journal


def keep_partial(label: str, attempt: int, text: str):
    """P0.7 (D10): the text a failed call had received, kept as checkpoints/partial_<label>_<UTC stamp>_<attempt>.txt (outside
    output\\, never uploaded); printed; never raises — the caller re-raises the original error."""
    try:
        p, why = NR.save_partial(CHECKPOINT_DIR, label, attempt, text)
        if p is not None:
            NR.say(f"    partial answer kept: {p} ({len(text):,} characters)")
        elif why:
            NR.say(f"    ⚠ partial answer not kept — {why}")
    except Exception as e:  # noqa: BLE001
        NR.say(f"    ⚠ partial answer not kept — {type(e).__name__}")


def keep_reply(name: str, text) -> Optional[str]:
    """P0.7 r2 (E3 d): a Block 2 reply's complete text, kept the moment it arrives — before parsing — as checkpoints/<name>_<run id>.txt
    (UTF-8; outside output\\, never uploaded; the next pipeline Block 1 archives it with the checkpoints). Printed; never raises: a
    failed write is printed and the run goes on, and the record then names no file. Returns "checkpoints/<file>" or None."""
    try:
        rel = f"{name}_{NR.run_id()}.txt"
        t = text if isinstance(text, str) else ""
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        (CHECKPOINT_DIR / rel).write_bytes(t.encode("utf-8", errors="replace"))
        NR.say(f"    reply kept: checkpoints/{rel} ({len(t):,} characters)")
        return f"checkpoints/{rel}"
    except Exception as e:  # noqa: BLE001
        NR.say(f"    ⚠ the reply was not kept ({type(e).__name__}) — the run goes on")
        return None


def _loads_reply(text: str, where: str):
    """BUILD IC-3 (D4): every reply of a model through the one strict decoder (NR.strict_loads): NaN and Infinity refused as not JSON; a
    key given twice recorded (checkpoints/decode_findings.jsonl, read back into the page's record) with the last value kept."""
    obj, findings = NR.strict_loads(text, where)
    NR.record_decode_findings(CHECKPOINT_DIR, findings)
    obj = REG.admit(obj, "reply", where)        # BUILD IC-5 (D1, I1): the one way in — canonical tags, private-use code points out
    REG.write_admit_findings(CHECKPOINT_DIR, NR.run_id())
    return obj


def _message_text(msg) -> str:
    try:
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    except Exception:  # noqa: BLE001
        return ""


@_retrying(label="claude call", attempts=3, profile="oracle/diagram_repair")
def call_claude(prompt: str, model: str = OPUS) -> str:
    """Call Claude with adaptive thinking. Returns text content only. BUILD IC-6 (D1): the Mermaid repair's call (its one caller) — its
    limit, effort and stream mode are its profile's, "oracle/diagram_repair" (IC-5's caller asked 4,000 at low, not streamed)."""
    prof = NR.profile("oracle/diagram_repair")
    client = get_anthropic_client()
    t0 = time.monotonic()
    attempt = _attempt(prof["label"])
    response, _t = NR.model_call(client, {"model": model, "max_tokens": prof["max_tokens"], "output_config": {"effort": prof["effort"]},
                                          "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": prompt}]},
                                 label=prof["label"], attempt=attempt, log_dir=CHECKPOINT_DIR, clock=_clock, end_at=_end_at(),
                                 stream=prof["stream"], profile="oracle/diagram_repair",
                                 **_call_ident(prof["label"], "oracle/diagram_repair"))     # BUILD IC-6 (D6); IC-8 (D8): its identity
    try:
        return final_text(response, t0)
    except GenerationError:
        keep_partial(prof["label"], attempt, _message_text(response))   # P0.7 (D10)
        raise


def _stream_text(prompt: str, system: str, model: str, key: str, end_at: float | None = None, messages: list | None = None,
                 label: str | None = None, second: int | None = None) -> str:
    """One streamed call; P0.7 (D10): when it ends in any error after text began, that text is kept (keep_partial) and the error
    raised as before. BUILD IC-3 (D5): through NR.model_call — every event read (the text as text_stream gave it), one line per
    attempt in checkpoints/call_log.jsonl, the call ended at its end time (EndTimeReached: kept, never retried). BUILD IC-6 (D1): `key`
    names the call's profile — its limit, effort, stream mode and the call log's label are the profile's. BUILD IC-6 (D6): `messages` in
    place of the prompt (a continuation's three), `label` in place of the profile's (its "+c"); the text journaled as it arrives."""
    prof = NR.profile(key)
    label = label or prof["label"]
    client = get_anthropic_client()
    t0 = time.monotonic()
    attempt = _attempt(label)

    def dots(n):
        if n % 80 == 0:
            console.print("·", end="", style="dim")
            NR.progress_open()             # BUILD IC-7 (D8, G12): any line printed now closes this one first (NR.say)
    try:
        try:
            msg, text = NR.model_call(client, {"model": model, "max_tokens": prof["max_tokens"], "output_config": {"effort": prof["effort"]},
                                               "system": system,
                                               "messages": messages if messages is not None else [{"role": "user", "content": prompt}]},
                                      label=label, attempt=attempt, log_dir=CHECKPOINT_DIR, clock=_clock,
                                      end_at=end_at if end_at is not None else _end_at(), stream=prof["stream"], timer=threading.Timer,
                                      on_text=dots, profile=key, **_call_ident(label, key, second),      # IC-8 (D8): its identity
                                      second_attempt=second)          # BUILD IC-7 (D3): the flag and the error count
        except AssertionError as exc:          # anthropic lib/streaming/_messages.py:94 — the stream never started
            raise IncompleteResponse("the stream ended before the model started its answer") from exc
        NR.progress_close()                # BUILD IC-7 (D8): the newline after the progress dots, once
        return final_text(msg, t0)
    except Exception:
        keep_partial(label, attempt, (getattr(NR.model_call, "last", None) or (None, ""))[1])
        raise


# ── Streaming call — prevents idle connection timeout on long generations ──
@_retrying(label="master analysis", attempts=2, budget_s=MASTER_STAGE_BUDGET_S, profile="oracle/master")
def call_claude_streaming(prompt: str, system: str = SYSTEM_PROMPT, model: str = OPUS) -> str:
    """Call Claude with STREAMING to prevent idle connection timeouts.
    Anthropic docs: 'Avoid setting a large max_tokens value without using
    the streaming Messages API: Some networks may drop idle connections.'
    This is the ONLY safe way to make calls that generate >1000 tokens. BUILD IC-6 (D1): the master's profile, "oracle/master"."""
    return _stream_text(prompt, system, model, "oracle/master", end_at=_end_at("master analysis"))


# P0.7 (D3): the Block 2 update call — its own retry label, the stage budget. BUILD IC-6 (D1): its profile, "oracle/update_whole" (IC-4's
# switch off: the whole-field reply; IC-5 asked the master's 52,000 at high)
@_retrying(label="curveball update", attempts=2, budget_s=MASTER_STAGE_BUDGET_S, profile="oracle/update_whole")
def call_claude_update(prompt: str) -> str:
    return _stream_text(prompt, SYSTEM_PROMPT, OPUS, "oracle/update_whole", end_at=_end_at("curveball update"))


# P0.11 D3 — only a finished answer counts. On anthropic 0.89.0 a cut-off stream returns without
# raising (stop_reason None), so every call checks why the model stopped.
class GenerationError(RuntimeError):
    """A model call ended without complete output (named subclasses below)."""
class IncompleteResponse(GenerationError):
    """The stream ended before the model finished (stop_reason None)."""
class MaxTokensReached(GenerationError):
    """The answer hit max_tokens and is cut off."""
class ModelRefused(GenerationError):
    """The model refused (stop_reason refusal)."""
class UnexpectedStop(GenerationError):
    """pause_turn, tool_use or stop_sequence: not a finished answer for these calls."""
class ContextWindowExceeded(GenerationError):
    """The response filled the model's context window."""
class EmptyText(GenerationError):
    """The model finished but returned no text."""
class StageBudgetExceeded(GenerationError):
    """Not enough time left in the stage for another attempt."""


def _final_message(stream):
    """get_final_message(), naming a stream that never started instead of asserting."""
    try:
        return stream.get_final_message()
    except AssertionError as exc:  # anthropic lib/streaming/_messages.py:94
        raise IncompleteResponse("the stream ended before the model started its answer") from exc


def final_text(msg, t0: float | None = None) -> str:
    """Joined text of a finished answer, or a named GenerationError. Prints one measurement line."""
    reason = getattr(msg, "stop_reason", None)
    out_tokens = getattr(getattr(msg, "usage", None), "output_tokens", None)
    took = f"{time.monotonic() - t0:.1f}s" if t0 is not None else "?"
    th = ((getattr(NR.model_call, "last", None) or ({}, ""))[0] or {}).get("thinking_tokens", "not reported")   # BUILD IC-3 (D5)
    think = f"think={th} tok" if isinstance(th, int) else "think=not reported"
    console.print(f"    [dim]{getattr(msg, 'model', '?')} · stop={reason} · out={out_tokens} tok · {think} · {took}[/dim]")
    if reason is None:
        raise IncompleteResponse("the stream ended before the model finished (no stop_reason)")
    if reason == "max_tokens":
        raise MaxTokensReached(f"the answer hit max_tokens ({out_tokens} output tokens) and is cut off")
    if reason == "refusal":
        raise ModelRefused(f"the model refused (category: {getattr(getattr(msg, 'stop_details', None), 'category', None) or 'not given'})")   # IC-4
        # Phase 7 (review c19): the category only — the stop details' explanation never reaches the console or the chat
    if reason == "model_context_window_exceeded":
        raise ContextWindowExceeded("the response filled the model's context window")
    if reason != "end_turn":
        raise UnexpectedStop(f"stop_reason={reason} is not a finished answer for this call")
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    if not text:
        raise EmptyText("the model finished without any text")
    return text


def extract_json(text: str) -> str:
    """Extract a JSON object from text that may contain markdown fences or prose."""
    # Strip markdown code fences
    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text.strip())
    # Find outermost JSON object
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


# ─────────────────────────────────────────────────────────────────────
# PYDANTIC SCHEMAS — structured outputs for each section
# Per Implementation Guide §3: fine-grained for numbers, coarse for prose
# ─────────────────────────────────────────────────────────────────────
class KPI(BaseModel):
    label: str
    counter_target: int | None = None
    counter_format: str = "raw"  # "raw", "compact", "dollar"
    suffix: str = ""
    static_value: str | None = None
    delta: str
    delta_class: str = "good"

class EvidenceCard(BaseModel):
    title: str = Field(..., min_length=5)
    stat_value: str
    stat_class: str = ""  # "bad", "warn", "good", ""
    stat_label: str
    body: str = Field(..., min_length=20)
    source: str = Field(..., min_length=5)

class PLRow(BaseModel):
    label: str
    cells: list[str] = Field(..., min_length=3, max_length=3)
    css_class: str = ""  # "total" for summary rows
    colors: list[str] | None = None  # ["good","good","warn"] etc.

class Scenario(BaseModel):
    tab_label: str
    chart_title: str
    table_title: str
    description: str
    pl_rows: list[PLRow]
    footnote: str | None = None

class Phase(BaseModel):
    summary: str
    content: str  # HTML content
    timeline_label: str
    timeline_suffix: str = "of implementation plan"
    meter_pct: int = Field(ge=0, le=100)

class Risk(BaseModel):
    risk: str
    likelihood: str
    impact: str
    mitigation: str

class ComparisonRow(BaseModel):
    dimension: str  # e.g., "Service Coordination", "Language Access"
    today: str      # current fragmented state
    proposed: str   # what the program enables

class MasterAnalysis(BaseModel):
    """Output of the master analysis — the single source of truth."""
    domain: str
    program_name: str
    brand_html: str  # the program name with one word wrapped in <em>
    city_name: str
    badge_text: str
    hero_title: str
    hero_lead: str
    summary_title: str
    bluf_text: str
    decision_text: str
    crisis_title: str
    crisis_body: str
    crisis_human: str
    crisis_sources: str
    solution_title: str
    solution_body: str
    solution_population: str
    cost_of_inaction: str
    comparison_rows: list[ComparisonRow] = Field(default_factory=list, min_length=0, max_length=6)
    evidence_title: str
    evidence_cards: list[EvidenceCard] = Field(..., min_length=3, max_length=5)
    operations_title: str
    operations_description: str
    plan_title: str
    financial_title: str
    sensitivity_text: str
    renewal_label: str
    renewal_text: str
    appendix_title: str
    methodology: str
    ai_process: str
    assumptions: list[str] = Field(..., min_length=3, max_length=8)
    # Financial assumptions for Python computation
    contract_value: Optional[int] = None  # P0.6 (D2): no default the model sees; the brief's figure wins, else the model proposes one
    domain_label: Optional[str] = Field(None, description="2-4 words naming the policy area the data describes; no place names")
    contract_years: int = 3
    target_beneficiaries_y1: int = 10000
    growth_rate_y2: float = 0.2
    growth_rate_y3: float = 0.2
    staff_roles: list[dict] = Field(default_factory=list)
    ops_cost_y1: int = 80000
    tech_cost_y1: int = 45000
    outreach_cost_y1: int = 20000
    supplemental_grants_y2: int = 75000
    supplemental_grants_y3: int = 125000
    earned_revenue_y3: int = 50000
    indirect_rate: float = 0.15
    # SROI assumptions
    sroi_outcomes: list[dict] = Field(default_factory=list)
    deadweight: float = 0.15
    attribution: float = 0.10
    displacement: float = 0.05
    drop_off_rate: float = 0.10
    discount_rate: float = 0.035
    # Mermaid diagram code
    mermaid_code: str = ""
    # Implementation phases
    phases: list[Phase] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    # BUILD B1 (D4): the use of funds' words — Python computes every amount (stage_financial_model); lenient dicts, read by fund_words
    # and marketing_alloc, so a malformed entry is named and dropped, never a failed analysis
    fund_lines: list[dict] = Field(default_factory=list)
    marketing_allocation: list[dict] = Field(default_factory=list)

    @field_validator("domain", "program_name", "brand_html", "city_name", "domain_label")
    @classmethod
    def _names_as_words(cls, v):
        """BUILD IC-5 (D3, class T): the analysis's names — the program's name and brand, its domain and city, the domain label — cite no
        figure (no resolver reads them) and every page prints them, the others from the checkpoint and the identity file: set down to
        words once, at the parse (REG.out_text, plain — a tag as words, a token's syntax out)."""
        return REG.out_text(v, "plain") if isinstance(v, str) else v


ITEM_MODELS = (("evidence_cards", "EvidenceCard", 1), ("comparison_rows", "ComparisonRow", 0), ("phases", "Phase", 0), ("risks", "Risk", 0))


class MasterAnalysisShort(MasterAnalysis):
    """BUILD IC-5 (D3, class L): the analysis when fewer evidence cards pass their own model than the page asks for (three): the valid
    ones, named on the console — never a failed run. The schema the request shows is MasterAnalysis's, unchanged."""
    evidence_cards: list[EvidenceCard] = Field(..., min_length=1, max_length=5)


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD IC-7 (D1, D2, D3; I1–I4) — the Oracle's contracts, the master's admission and its second attempt
# ═══════════════════════════════════════════════════════════════════════════════
ORACLE_FREE_KEYS = {"staff_roles[]": ["role", "salary", "count", "benefits_pct", "salary_ref"],      # the keys its requests name for
                    "sroi_outcomes[]": ["category", "core", "material", "why", "unit_value_ref", "unit_value", "rate", "duration_years",
                                        "source"]}                                                    # its free-key lists (lenient dicts)


def _leaf_paths(schema, want):
    defs, out = schema.get("$defs") or {}, []

    def walk(n, path):
        n = NR._unoptional(n, defs)
        t = n.get("type")
        if t == "object" and n.get("properties"):
            for k, v in n["properties"].items():
                walk(v, f"{path}.{k}" if path else k)
        elif t == "array":
            walk(n.get("items") or {}, f"{path}[]")
        elif t in want:
            out.append(path)
    walk(schema, "")
    return out


def _oracle_view(key, schema_word="shown"):
    """The statement check's view of an Oracle request: its reply's schema shown (the master's, the items' forms); the typed-number rule's
    words are categories here (a count, a percent, a day, a target) — only a word naming a field the contract types as text is a crossing;
    the count rule may name any list the business case shows (REG.COUNT_LISTS: the analysis's and the deliverables'); the name rule the
    keys its requests name for the free-key lists."""
    s = MasterAnalysis.model_json_schema()
    lists = {}

    def walk(n, path):
        n = NR._unoptional(n, s.get("$defs") or {})
        if n.get("type") == "object":
            for k, v in (n.get("properties") or {}).items():
                walk(v, f"{path}.{k}" if path else k)
        elif n.get("type") == "array":
            lists[path] = (n.get("minItems", 0), n.get("maxItems"))
            walk(n.get("items") or {}, f"{path}[]")
    walk(s, "")
    return {"call": key, "schema": schema_word, "number_paths": _leaf_paths(s, ("integer", "number")), "text_paths": _leaf_paths(s, ("string",)),
            "count_lists": list(REG.COUNT_LISTS), "name_fields": sorted({k for v in ORACLE_FREE_KEYS.values() for k in v}), "lists": lists,
            "typed_words": "categories"}


CONTRACTS = {
    "oracle/master": {"form": "object", "page": "MasterAnalysis (MasterAnalysisShort when fewer than three evidence cards pass)",
                      "shown": "MasterAnalysis's schema (schema_shown; the diagram's form with the IC-5 switch on)",
                      "rules": "its normalizers as they are (money_value, domain_label_value, fin_inputs_values, funds_values), each counted; "
                               "then strict validation item by item (NR.admit): an invalid item left out, a field with a default given it, "
                               "an essential path to the second attempt (D3)",
                      "essential": "MasterAnalysis's required fields; an evidence card list left empty; assumptions under three",
                      "view": lambda: _oracle_view("oracle/master")},
    "oracle/deliverables": {"form": "lines (NAIPC_IC6_DELIVERABLES on), else one object", "page": "the deliverables' items (REG)",
                            "rules": "IC-6's record contracts, one line at a time: a line that fails is named and left out (D13 of IC-6)",
                            "view": lambda: _oracle_view("oracle/deliverables")},
    "oracle/update": {"form": "lines", "page": "the analysis's and the deliverables' items, by id", "rules": "NR.apply_edits' checks (IC-4)",
                      "view": lambda: _oracle_view("oracle/update")},
    "oracle/update_whole": {"form": "object", "page": "MasterAnalysis's amended fields", "rules": "NR.merge_amended's checks (P0.7)",
                            "view": lambda: _oracle_view("oracle/update_whole")},
    "oracle/repair": {"form": "schema", "page": "the brief's answers", "wire": lambda: NR.BRIEF_REPAIR_SCHEMA,
                      "rules": "each {id, answer} checked by id; an answer that fails is left out",
                      "view": lambda: dict(_oracle_view("oracle/repair", "sent"), count_lists=[], name_fields=[])},
    "oracle/diagram_repair": {"form": "text", "page": "the Mermaid code", "rules": "mmdc parses it; else the diagram is drawn as before",
                              "view": lambda: dict(_oracle_view("oracle/diagram_repair", None), count_lists=[], name_fields=[])},
}


def _master_validate(v):
    m = MasterAnalysisShort if isinstance(v, dict) and 0 < len(v.get("evidence_cards") or []) < 3 else MasterAnalysis
    try:
        return m.model_validate(v, strict=True), None
    except ValidationError as e:
        return None, [{"loc": list(er.get("loc") or ()), "type": er.get("type")} for er in e.errors(include_url=False, include_input=False)]


def master_admission(data, pin=None, brief_raw=None, attempt=1, side=None, record=False):
    """BUILD IC-7 (D2, I2, I3): the master's reply (the object after strict_loads, its out-of-schema keys taken out) → (obj, entries,
    errors). Its own normalizers as they are — money_value, domain_label_value, fin_inputs_values (a default named), funds_values — each
    change now an entry; the pinned identity; then NR.admit: strict validation (Q-STRICT) item by item — an invalid list item left out
    and named (IC-5's valid_items, generalized), a field with a default given it, MasterAnalysisShort when fewer than three evidence cards
    pass — and an essential path left invalid to `errors` (D3). `side` receives inputs_given, inputs_notes and the brief's answers."""
    side = side if side is not None else {}
    entries, d = [], data

    def num_same(a, b):
        return isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and float(a) == float(b)
    raw_cv, raw_dl = d.get("contract_value"), d.get("domain_label")
    d["contract_value"] = money_value(raw_cv)
    if raw_cv is not None and not num_same(raw_cv, d["contract_value"]):
        entries.append({"path": "contract_value", "rule": "normalized", "seen": NR._seen(raw_cv)})
    d["domain_label"] = domain_label_value(raw_dl)
    if raw_dl is not None and d["domain_label"] != raw_dl:
        entries.append({"path": "domain_label", "rule": "normalized", "seen": NR._seen(raw_dl)})
    before = {k: d.get(k) for k in FIN_INT_KEYS + FIN_RATE_KEYS if k in d}
    d, inputs_given, inputs_notes = fin_inputs_values(d)          # BUILD B3 (D1): one parser; a default named
    for k, v0 in before.items():
        if k not in d:
            entries.append({"path": k, "rule": "not written this run", "seen": NR._seen(v0)})
        elif not num_same(v0, d[k]):
            entries.append({"path": k, "rule": "normalized", "seen": NR._seen(v0)})
    for k_, why_ in inputs_notes.items():
        NR.say(f"    financial input {k_}: {why_} — Python's default {MasterAnalysis.model_fields[k_].default!r} is used")
    fl0, al0 = d.get("fund_lines"), d.get("marketing_allocation")
    d["fund_lines"], d["marketing_allocation"], fnotes = funds_values(fl0, al0)
    for n_ in fnotes:      # BUILD B1 (review #1): named, never a failed analysis
        NR.say(f"    use of funds: {n_}")
    for name, v0, v1 in (("fund_lines", fl0, d["fund_lines"]), ("marketing_allocation", al0, d["marketing_allocation"])):
        if v0 is not None and v0 != v1:
            entries.append({"path": name, "rule": "normalized", "seen": NR._seen(v0)})
    if pin:
        d, brief_raw = apply_identity(d, brief_raw, pin)
    side.update(inputs_given=inputs_given, inputs_notes=inputs_notes, brief_raw=brief_raw)
    not_asked = (["discount_rate"] if ic4_prompt() else []) + (["mermaid_code"] if NR.ic5_prompt() else [])
    gone = [k for k in before if k not in d]      # BUILD IC-7 (Phase 4): removed by the parser above and named there — once, not again
    obj, ents, errs = NR.admit(d, MasterAnalysis.model_json_schema(), _master_validate, {"not_asked": not_asked + gone})   # as missing
    entries += ents
    for e in ents:
        if e["rule"] == "item left out":
            NR.say(f"    analysis: left out an item of {e['path'][:-2]} — it did not pass its own model")
    if obj is not None and len(obj.evidence_cards) < 3:
        NR.say(f"    analysis: {len(obj.evidence_cards)} evidence card(s), fewer than the three the page asks for — the page shows "
               f"{len(obj.evidence_cards)}")
    if record:
        outcome = (("admitted" if not entries else "admitted, named") if obj is not None
                   else "to a second attempt" if attempt == 1 else "not admitted")
        NR.admission_record(CHECKPOINT_DIR, "oracle/master", entries, outcome, attempt)
        NR.say(NR.admission_line("oracle/master", entries, outcome))
    return obj, entries, errs


def parse_master(raw_response, pin=None, curveball_ctx=None, attempt=1):
    """BUILD IC-7 (D2): one master reply read — its JSON (the one strict decoder), the fields read outside its schema, then
    master_admission. → {result, brief_raw, cb_extra, diag_raw, inputs_given, inputs_notes, entries, errors}; a reply with errors is
    kept in checkpoints/master_analysis_raw[_<attempt>].txt."""
    out = {"result": None, "brief_raw": None, "cb_extra": {}, "diag_raw": None, "inputs_given": None, "inputs_notes": {}, "entries": [],
           "errors": []}
    try:
        data = _loads_reply(extract_json(raw_response), "master")   # BUILD IC-5 (D1): admitted inside; IC-3 (D4): strict
    except Exception as e:  # noqa: BLE001
        data = None
        NR.say(f"    ⚠ the master's reply is not JSON ({type(e).__name__}); response length {len(raw_response or '')} chars")
    if not isinstance(data, dict):
        out["errors"] = [{"path": "(the reply)", "expected": "one JSON object of its schema",
                          "wrote": "no JSON" if data is None else f"a {type(data).__name__}"}]
        NR.admission_record(CHECKPOINT_DIR, "oracle/master", [], "to a second attempt" if attempt == 1 else "not admitted", attempt)
    else:
        out["brief_raw"] = data.pop("brief_answers", None)        # P0.3b: never a schema field (D9)
        out["cb_extra"] = ({k: data.pop(k, None) for k in ("what_changed", "response_summary", "test_answers")}
                           if curveball_ctx else {})               # P0.7 (D3): the full path's section, outside the schema
        out["diag_raw"] = data.pop("diagram", None) if NR.ic5_prompt() else None     # BUILD IC-5 (D15): outside the schema
        side = {}
        obj, entries, errs = master_admission(data, pin=pin, brief_raw=out["brief_raw"], attempt=attempt, side=side, record=True)
        out.update(result=obj, entries=entries, errors=errs, inputs_given=side.get("inputs_given"),
                   inputs_notes=side.get("inputs_notes") or {}, brief_raw=side.get("brief_raw"))
    if out["errors"]:
        try:
            (CHECKPOINT_DIR / ("master_analysis_raw.txt" if attempt == 1 else f"master_analysis_raw_{attempt}.txt")).write_text(
                raw_response or "", encoding="utf-8")
        except OSError:
            pass
    return out


def call_master_second(prompt, n_errors):
    """BUILD IC-7 (D3): the master's second attempt — the first request plus the errors block, the same system prompt and profile, the
    stage's own end time (never a new budget); no retry of its own (the master has two attempts)."""
    return _stream_text(prompt, SYSTEM_PROMPT, OPUS, "oracle/master", end_at=_end_at("master analysis"), second=n_errors)


def master_with_second_attempt(full_prompt, raw_response, pin=None, curveball_ctx=None, call=None):
    """BUILD IC-7 (D3, I3, I4): the master's reply admitted, or — an essential path invalid, or no JSON — one second attempt: the first
    request plus <errors_of_the_first_attempt> (NR.second_request), started only when the start rule allows a retry (else
    NR.AdmissionFailed, plain words NR.NO_TIME_WORDS). → (parsed, the reply used). A second reply that still fails →
    NR.AdmissionFailed. IC-6 had no second attempt: its parse raised."""
    p = parse_master(raw_response, pin, curveball_ctx, attempt=1)
    if not p["errors"]:
        return p, raw_response
    NR.say(f"    ✗ the master's reply: {len(p['errors'])} field(s) the page cannot do without could not be used — "
           + ", ".join(x["path"] for x in p["errors"][:8]) + (" …" if len(p["errors"]) > 8 else ""))
    ok, note, _n = NR.start_check("oracle/master", CHECKPOINT_DIR, OPUS, ORACLE_RATE, left=_end_at("master analysis") - _clock(), retry=True)
    if not ok:
        raise NR.AdmissionFailed(f"master analysis: its reply could not be used and no time is left for a second attempt — {note}",
                                 plain=NR.NO_TIME_WORDS, errors=p["errors"])
    if note:
        NR.say(f"    master analysis second attempt: {note}")
    raw2 = (call or call_master_second)(NR.second_request(full_prompt, p["errors"]), len(p["errors"]))
    p2 = parse_master(raw2, pin, curveball_ctx, attempt=2)
    if p2["errors"]:
        raise NR.AdmissionFailed(f"master analysis: the reply could not be used after a second attempt ({len(p2['errors'])} field(s): "
                                 + ", ".join(x["path"] for x in p2["errors"][:8]) + ")", errors=p2["errors"])
    p2["second"] = True
    return p2, raw2


def valid_items(data):
    """BUILD IC-5 (D3, class L): the analysis's list items, each checked against its own model before the analysis is — an item whose
    fields fail (an evidence card's empty title) is left out and named, so one bad item never fails the whole analysis; a list left with
    no item is kept as it was (the analysis's own validation names it, as before); fewer than three evidence cards are validated by
    MasterAnalysisShort, and said. Never raises."""
    if not isinstance(data, dict):
        return data
    for f, mname, lo in ITEM_MODELS:
        items, model = data.get(f), globals()[mname]
        if not isinstance(items, list):
            continue
        keep, gone = [], []
        for i, it in enumerate(items):
            try:
                model.model_validate(it)
                keep.append(it)
            except Exception as ex:  # noqa: BLE001
                errs = getattr(ex, "errors", None)
                why = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in (errs() if callable(errs) else [])) or type(ex).__name__
                gone.append((i, why))
        if gone and len(keep) >= lo:
            data[f] = keep
            for i, why in gone:
                NR.say(f"    analysis: left out {f}[{i}] — {why[:160]}")
            if f == "evidence_cards" and len(keep) < 3:
                NR.say(f"    analysis: {len(keep)} evidence card(s), fewer than the three the page asks for — the page shows {len(keep)}")
    return data


# ─────────────────────────────────────────────────────────────────────
# STAGE 1: DATA LOADING — fetch and normalize API endpoints
# Per Architecture Doc: handle both {source, count, data} and {status, alerts}
# ─────────────────────────────────────────────────────────────────────
def _records(payload, ep=""):
    """(records, source) of one endpoint's payload. P0.7 (D5): the records by the one envelope rule of all four harnesses, NR.records —
    a list under data, alerts, items, results, records, facilities or resources → that list; {"data": {...}} → [that object]; any other
    object → [it]; a list → itself; anything else → [] (P0.6's rule read only data and alerts). Source: the envelope's "source" when the
    records sit in one, else ep. Never raises."""
    recs, env, _rule = NR.envelope_split(payload)
    return recs, (env.get("source", ep) if env else ep)


@stage("data_loading")
def stage_data_loading(deps: dict) -> dict:
    """Fetch all API endpoints, normalize JSON, create data package."""
    api_url = os.environ.get("API_URL", "")
    if not api_url:
        raise RuntimeError("API_URL environment variable not set")

    # Discover endpoints by trying common patterns
    endpoints_to_try = os.environ.get("API_ENDPOINTS", "").split(",")
    if not endpoints_to_try or endpoints_to_try == ['']:
        # Auto-discover: fetch the base URL and look for links,
        # or try standard NAIPC patterns
        console.print("  [dim]Auto-discovering endpoints...[/dim]")
        try:
            r = requests.get(api_url, timeout=10)
            if r.ok:
                # Try to parse as JSON index
                data = r.json()
                if isinstance(data, list):
                    endpoints_to_try = [str(e) for e in data]
                elif isinstance(data, dict) and "endpoints" in data:
                    endpoints_to_try = data["endpoints"]
        except Exception:
            pass

    raw_data = {}
    data_summary = []
    for ep in endpoints_to_try:
        ep = ep.strip().strip("/")
        if not ep:
            continue
        url = NR.endpoint_url(api_url, ep)  # P0.6 (D8): the one URL builder; NAIPC_URL_SLASH=1 when the server answers only <ep>/
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            payload = r.json()
            raw_data[ep] = payload

            # Normalize: extract the data array regardless of envelope (P0.6 review F9: one rule, _records; a "data" object is one record)
            records, source = _records(payload, ep)

            count = len(records)
            # Extract field names from first record
            fields = list(records[0].keys()) if records and isinstance(records[0], dict) else []
            data_summary.append({
                "endpoint": ep, "source": source, "count": count,
                "fields": fields, "url": url
            })
            console.print(f"    [green]✓[/green] {ep}: {count} records, {len(fields)} fields")
        except Exception as e:
            NR.say(f"    ✗ {ep}: {e}")                                     # BUILD IC-7 (D8)
            data_summary.append({"endpoint": ep, "error": str(e), "count": 0})

    # BUILD IC (D1): inputs lose private-use characters; IC-3 (D4): the data's own text never holds a token's brackets
    out = {"raw": REG.debracket_strings(REG.admit(raw_data, "data", "data")),                     # BUILD IC-5 (D1): the one way in
           "summary": REG.debracket_strings(REG.admit(data_summary, "data", "data summary"))}
    REG.set_endpoints(list(raw_data) if isinstance(raw_data, dict) else None)   # BUILD IC-5 (D1): a data tag's names are checked against them
    out["report"] = data_report_now(out)            # BUILD IC-4 (D5): before the first model call; at the curveball, against Block 1's
    return out


def _figure_of(reg, ref):
    """BUILD IC-4 (D5): a contract figure the data alone does not read — a figure of this run's registry (a set's free total, a derived
    figure) or a quote form, with an optional part of a ratio or a range (#num, #den, #lo, #hi). None when it names nothing."""
    ref, _, part = str(ref).partition("#")
    e = reg.get(ref) or reg.lookup(ref)[0]
    if not e:
        return None
    if part:
        try:
            v = json.loads(e.get("claim") or "null")
            return v[{"lo": 2, "hi": 3, "num": 4, "den": 5}[part]] if v else None
        except Exception:  # noqa: BLE001
            return None
    return e.get("value")


def data_report_now(dl: dict) -> dict:
    """BUILD IC-4 (D5): the data report on this run's data (NR.data_report) — the contract's relations checked, at Block 2 Block 1's data
    beside it; at most twelve lines to the console (violations first), the whole report to checkpoints/data_report.json, the candidate
    entries to <challenge>.draft.json beside the contract (never into it). Returns the counts by class. Never raises."""
    try:
        doc = _brief_contract()
        b1 = _cp_data(CHECKPOINT_DIR / NR.BLOCK1_DIR / "data_loading.json") if is_block2() else None
        reg_ = build_registry({"data_loading": dl})
        rep_ = NR.data_report(dl.get("raw") or {}, dl.get("summary") or [], doc, (b1 or {}).get("raw") if isinstance(b1, dict) else None,
                              lookup=lambda ref: _figure_of(reg_, ref))
        for ln in rep_.get("console") or []:
            NR.say(f"    {ln}")
        NR.write_json(CHECKPOINT_DIR / "data_report.json", rep_)
        cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
        if cpath and any((rep_.get("drafts") or {}).values()):
            dp = NR.write_drafts(rep_, cpath)
            if dp:
                NR.say(f"    data report: candidate entries for the contract in {Path(dp).name} — confirm with: python -B naipc_requirements.py "
                       f"confirm {Path(dp).name} --items <block>:<n>")
        return {k: sum(1 for f in rep_.get("findings") or [] if f.get("class") == k) for k in ("violation", "suspicion", "cannot be checked")}
    except Exception as e:  # noqa: BLE001 — a report that cannot be made never stops the run
        NR.say(f"    data report: not made ({type(e).__name__}: {str(e)[:120]})")
        return {}


# ─────────────────────────────────────────────────────────────────────
# STAGE 2: MASTER ANALYSIS — Claude Opus deep reasoning
# Per Implementation Guide §2: CO-STAR framework, domain-agnostic
# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────
# P0.3b (D9) — THE BRIEF'S ANSWERS: the contract's items in the prompt; brief_answers read outside the schema
# ─────────────────────────────────────────────────────────────────────
def _changes_from_env():
    """BUILD IC (D1): NR.changes_from_env() — the change list the pipeline wrote (the announcement, its tests, the records added and
    removed) — with every private-use character removed where it enters."""
    ch, why = NR.changes_from_env()
    return REG.admit(ch, "data", "change list"), why      # BUILD IC-5 (D1)


def _load_spec(cpath):
    """BUILD IC (D1): ENG.load_spec(cpath) — the engine sidecar (its registry labels and assumptions) — with every private-use character
    removed where it enters."""
    spec, why = ENG.load_spec(cpath)
    return REG.admit(spec, "data", "engine sidecar"), why      # BUILD IC-5 (D1)


def _brief_contract():
    """The contract pipeline.py exported, or None (then the master prompt is byte-identical to r2's). Never raises."""
    if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
        return None
    try:
        return REG.debracket_strings(REG.admit(NR.from_env(), "data", "contract"))   # BUILD IC-5 (D1)  # BUILD IC (D1): private-use characters out; IC-3 (D4): no [[ ]]
    except Exception as e:  # noqa: BLE001 — an unusable contract leaves the analysis as it was
        NR.say(f"    brief: not used — the contract is not usable ({type(e).__name__}: {str(e)[:160]})")
        return None


def pipeline_facts(n_endpoints: int, update: bool = False) -> list:
    """What this harness really does: the only method facts an 'AI use' answer may state (D9). The model is named through OPUS.
    P0.7 (D3): `update` adds the Block 2 update call's fact. BUILD IC-3 (review c #4): with the switch on the facts state no count —
    the model restates them in methodology and ai_process ("one structured analysis", "from one registry": R4's two unlinked "one"s),
    and no id holds them; the switch off keeps IC-2's words."""
    if ic3_prompt():
        out = ["every endpoint of the challenge API, fetched at run time and passed to the model as raw JSON",
               f"a structured analysis by {OPUS} at effort high: the narrative, the financial assumptions and these answers",
               "Python computes the financial model, the cost per beneficiary, the SROI and the cost of inaction from those assumptions; the page shows Python's figures",
               ("the workflow diagram: this analysis names its steps and their links; Python writes its Mermaid code and each step's tooltip "
                "from the registry's figures, and the page draws it" if NR.ic5_prompt() else      # BUILD IC-5 (D15)
                "the workflow diagram is the Mermaid code written in this analysis, drawn on the page"),
               f"after the financial model, a further call by {OPUS} at effort medium writes the KPI framework, the failure modes, the renewal "
               "gate, the thesis and the judge questions; Python checks every id, fund line, risk and day in it",
               "every quantity in the text is a token Python fills from the registry (each figure's value, kind and source); a checker reads the "
               "built page and counts how many of its quantities trace to that registry",
               "the page's chat answers questions from a context file built from this analysis, through a server-side proxy; no key is on the page"]
        if update:
            out.insert(2, f"at the curveball, an update call by {OPUS} at effort high over that analysis and a change list Python computed from "
                          "the data: what changed, the sections it amends with their reasons and any assumption changes; Python keeps only the "
                          "amendments whose reasons name real changes and recomputes every figure")
        return out
    out = [f"{n_endpoints} endpoint(s) fetched at run time from the challenge API and passed to the model as raw JSON",
           f"one structured analysis by {OPUS} at effort high: the narrative, the financial assumptions and these answers",
           "Python computes the financial model, the cost per beneficiary, the SROI and the cost of inaction from those assumptions; the page shows Python's figures",
           "the workflow diagram is the Mermaid code written in this analysis, drawn on the page",
           f"after the financial model, one more call by {OPUS} at effort medium writes the KPI framework, the failure modes, the renewal "
           "gate, the thesis and the judge questions; Python checks every id, fund line, risk and day in it",
           "every figure in the text is a token Python fills from one registry (each figure's value, kind and source); a checker reads the "
           "built page and counts how many of its numbers trace to that registry",
           "the page's chat answers questions from a context file built from this analysis, through a server-side proxy; no key is on the page"]
    if update:
        out.insert(2, f"at the curveball, one update call by {OPUS} at effort high over that analysis and a change list Python computed from "
                      "the data: what changed, the sections it amends with their reasons and any assumption changes; Python keeps only the "
                      "amendments whose reasons name real changes and recomputes every figure")
    return out


def _analysis_summary(ma: dict) -> str:
    """A compact plain-text summary of the analysis for the repair request (no markup, at most 9,000 characters)."""
    keys = ("program_name", "city_name", "domain", "summary_title", "bluf_text", "decision_text", "crisis_body", "solution_body",
            "solution_population", "operations_description", "plan_title", "sensitivity_text", "renewal_label", "renewal_text",
            "methodology", "ai_process")
    parts = [f"{k}: {NR.clean_text(ma.get(k))}" for k in keys if NR.clean_text(ma.get(k))]
    parts += [f"phase: {NR.clean_text(p.get('summary'))} — {NR.clean_text(p.get('content'))[:400]}" for p in ma.get("phases") or [] if isinstance(p, dict)]
    parts += [f"risk: {NR.clean_text(r.get('risk'))} — {NR.clean_text(r.get('mitigation'))}" for r in ma.get("risks") or [] if isinstance(r, dict)]
    parts += [f"{k}: {ma.get(k)}" for k in ("contract_value", "contract_years", "target_beneficiaries_y1", "ops_cost_y1", "tech_cost_y1",
                                            "outreach_cost_y1") if ma.get(k) is not None]
    return "\n".join(parts)[:9000]


def _brief_repair(doc: dict, missing: list, ma: dict, facts=None) -> dict:
    """ONE request for the missing answers (D9): a single attempt through get_anthropic_client() (max_retries=0), effort low,
    at most 4,000 tokens — not call_claude, whose decorator makes up to three attempts with no time budget. It carries the brief
    and the same true facts as the master prompt (the P0.3 review: without them an 'AI use' answer had nothing true to state)."""
    prof = NR.profile("oracle/repair")      # BUILD IC-6 (D1): its limit, effort and stream mode (IC-5's: 4,000 at low, not streamed)
    client = get_anthropic_client()
    t0 = time.monotonic()
    attempt = _attempt(prof["label"])
    response, _t = NR.model_call(client, {"model": OPUS, "max_tokens": prof["max_tokens"],
                                          "output_config": NR.output_config(prof["effort"], NR.BRIEF_REPAIR_SCHEMA),     # BUILD IC-4 (D12)
                                          "system": SYSTEM_PROMPT,
                                          "messages": [{"role": "user", "content": NR.brief_repair_prompt(doc, "oracle", missing,
                                                                                                          _analysis_summary(ma), facts)}]},
                                 label=prof["label"], attempt=attempt, log_dir=CHECKPOINT_DIR, clock=_clock, end_at=_end_at(),
                                 stream=prof["stream"], profile="oracle/repair",
                                 **_call_ident(prof["label"], "oracle/repair"))     # BUILD IC-6 (D6); IC-8 (D8): its identity
    try:
        text = final_text(response, t0)
    except GenerationError:
        keep_partial(prof["label"], attempt, _message_text(response))   # P0.7 (D10)
        raise
    got = _loads_reply(extract_json(text), "brief repair")   # BUILD IC-5 (D1): admitted inside   # BUILD IC (D1): no private-use characters; IC-3 (D4): strict
    answers, notes = NR.parse_brief_answers(got.get("brief_answers") if isinstance(got, dict) else got, missing)
    for n in notes:
        NR.say(f"    repair: {n}")
    return answers


def brief_answers_for(doc: dict, raw, ma: dict, facts=None) -> list:
    """brief_answers parsed by a helper that never raises; at most one repair for missing ids, only inside the time box; prints
    'brief answers: n of N (repair: k asked, j filled)'. Returns [{id, answer}] in contract order."""
    ids = [i for i, _ in NR.brief_items(doc, "oracle")]
    answers, notes = NR.parse_brief_answers(raw, ids)
    for n in notes:
        NR.say(f"    {n}")
    missing = [i for i in ids if i not in answers]
    asked = filled = 0
    if missing:
        ok_, note_, _need = NR.start_check("oracle/repair", CHECKPOINT_DIR, OPUS, ORACLE_RATE, left=_end_at() - _clock())   # IC-4 (D9), IC-6 (D2)
        if not ok_:
            NR.say(f"    brief answers: repair skipped — {note_}")
        else:
            asked = len(missing)
            try:
                got = _brief_repair(doc, missing, ma, facts)
                for i in missing:
                    if i in got:
                        answers[i] = got[i]
                        filled += 1
            except Exception as e:  # noqa: BLE001 — a failed repair leaves those rows "—"; the analysis stands
                NR.say(f"    brief answers: repair failed — {type(e).__name__}: {str(e)[:200]}")
    NR.say(f"    brief answers: {len(answers)} of {len(ids)} (repair: {asked} asked, {filled} filled)")
    return [{"id": i, "answer": answers[i]} for i in ids if i in answers]


# ─────────────────────────────────────────────────────────────────────
# P0.6 — the budget in use (D2), the true method facts (D4, D7), the program identity (D3)
# ─────────────────────────────────────────────────────────────────────
def budget_in_use(F: dict):
    """D2's first two sources, for the prompt: (amount, "override") from --budget (CONTRACT_OVERRIDE), else (amount, "brief") from
    the contract, else (None, None) — the model then proposes one; the financial model completes the order (proposal, cost plan)."""
    o = os.environ.get("CONTRACT_OVERRIDE", "").strip()
    if o:
        try:
            return int(o), "override"
        except ValueError:
            NR.say(f"    budget: CONTRACT_OVERRIDE {o!r} is not a whole number of dollars — ignored")
    if F.get("contract") is not None:
        return int(F["contract"]), "brief"
    return None, None


def facts_block(facts: list) -> str:
    """The true method facts in their own block when no contract carries them inside the brief block (P0.6, D4, D7)."""
    return ("<pipeline_facts>\nWhat this pipeline actually does (the only true facts about method and AI use — claim nothing beyond them):\n"
            + "\n".join(f"- {f}" for f in facts) + "\n</pipeline_facts>")


def money_value(v):
    """contract_value as the model wrote it → whole dollars or None, never raising (P0.6, D2: one malformed field must not fail the
    whole analysis): 750000, 750000.0, "750000", "$750,000", "$1.2M", "750K" → an int; null, "", anything else → None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(v)) if v == v and abs(v) < 1e15 else None
    if isinstance(v, str) and "%" not in v and "-" not in v and "−" not in v:   # B3 (D1): the registry's one parser ("1.2 million")
        x = REG.parse_number(v)
        if x is not None and abs(x) < 1e15:
            return int(round(x))
    return None


# BUILD B3 (D1): the master analysis's numeric financial inputs, read by the one parser before validation — a value the schema would
# refuse ("$45K", "1.2 million", "38%", null) must not fail the whole analysis, and a value Python cannot use becomes its default, named
FIN_INT_KEYS = ("target_beneficiaries_y1", "ops_cost_y1", "tech_cost_y1", "outreach_cost_y1", "supplemental_grants_y2", "supplemental_grants_y3",
                "earned_revenue_y3", "contract_years")
FIN_RATE_KEYS = ("growth_rate_y2", "growth_rate_y3", "indirect_rate", "deadweight", "attribution", "displacement", "drop_off_rate", "discount_rate")


def fin_inputs_values(data):
    """B3 (D1): (data, given, notes) — each numeric financial input as a number of the schema's type; one that does not parse, a count that
    is not positive or a share outside 0–1 (a growth rate above −1) is removed so the schema's default applies, its reason in `notes`
    (key → words). "20" written for a share is read as 20%. `given`: the inputs the reply holds after this. Never raises."""
    notes = {}
    for k in FIN_INT_KEYS + FIN_RATE_KEYS:
        if k not in data:
            continue
        raw_v = data.get(k)
        v = None if raw_v is None or isinstance(raw_v, (list, dict)) else REG.parse_number(raw_v)
        if v is not None and k in FIN_RATE_KEYS and 1 < abs(v) <= 100 and float(v).is_integer():
            v = v / 100                                  # a whole number written for a percentage ("20"); 1.5 stays 1.5 (and is refused as a share)
        bad = None
        if raw_v is None:
            bad = "the model wrote null"
        elif v is None:
            bad = f"the model's value {str(raw_v)[:30]!r} is not a number"
        elif k in ("target_beneficiaries_y1", "contract_years") and v < 1:
            bad = f"the model's value {str(raw_v)[:30]!r} is not a positive count"
        elif k in FIN_INT_KEYS and v < 0:
            bad = f"the model's value {str(raw_v)[:30]!r} is negative"
        elif k in ("growth_rate_y2", "growth_rate_y3") and v <= -1:
            bad = f"the model's value {str(raw_v)[:30]!r} is not a growth rate above −100%"
        elif k in FIN_RATE_KEYS and k not in ("growth_rate_y2", "growth_rate_y3") and not 0 <= v < 1:
            bad = f"the model's value {str(raw_v)[:30]!r} is not a share from 0 to 1"
        if bad:
            data.pop(k, None)
            notes[k] = bad
        else:
            data[k] = int(round(v)) if k in FIN_INT_KEYS else float(v)
    return data, sorted(k for k in FIN_INT_KEYS + FIN_RATE_KEYS if k in data), notes


def domain_label_value(v):
    """domain_label as the model wrote it → plain text of at most 60 characters, or None (P0.6, D3)."""
    if not isinstance(v, str):
        return None
    return REG.fit_text(NR.clean_text(v), "oracle.domain_label").strip() or None


def identity_pin(doc, F: dict):
    """D3 — the name this run must use: the contract's team block → in Block 2 only (CURVEBALL_TEXT set), Block 1's identity file
    when it names this challenge → None (the model names the program). Returns (pin or None, why); never raises."""
    name, brand = NR.team_identity(doc)
    if name:
        return {"program_name": name, "brand_html": brand, "domain_label": None, "source": "contract"}, "the contract's team block"
    if not os.environ.get("CURVEBALL_TEXT", "").strip():
        return None, "Block 1"
    chal = F.get("challenge") or os.environ.get("NAIPC_CHALLENGE", "").strip() or None
    if not chal:
        return None, "Block 2 without a contract: no challenge id to check an identity file against"
    got, why = REG.admit(NR.read_identity(CHECKPOINT_DIR / NR.IDENTITY_FILE, chal), "saved", NR.IDENTITY_FILE)   # BUILD IC-5 (D1)
    if not got:
        return None, f"Block 1's identity file not used — {why}"
    return ({"program_name": got["program_name"], "brand_html": got["brand_html"], "domain_label": got["domain_label"], "source": "block1"},
            f"Block 1's identity file, run {got['run_id']}")


def _swap_name(x, old: str, new: str):
    """(a copy of x with every exact occurrence of `old` in its text replaced by `new`, the number replaced). One regex pass tries the
    longer of the two strings first at each position, so `new` where it already stands is left alone even when one contains the
    other: 'Harbor Line Two' → 'Harbor Line' replaces the longer name; 'FreshRoute KC Plus' already in the text is not touched by
    'FreshRoute KC' → 'FreshRoute KC Plus'. (P0.6 red team RT-5: a protect-then-replace pass hid a model name that contains the
    pinned one, and left it in the text.)"""
    rx = re.compile("|".join(re.escape(t) for t in sorted({old, new}, key=len, reverse=True)))
    n = [0]

    def sub(m):
        if m.group(0) == old:
            n[0] += 1
            return new
        return m.group(0)

    def walk(v):
        if isinstance(v, str):
            return rx.sub(sub, v)
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, dict):
            return {k: walk(i) for k, i in v.items()}
        return v
    return walk(x), n[0]


def apply_identity(data: dict, brief_raw, pin: dict):
    """D3: the pinned name over the model's name fields; when the model still wrote another name, every exact occurrence of that
    string in the analysis's text and the brief answers becomes the pinned one — only for a name of two or more words or with a
    capital inside a word (a single plain word may be an ordinary word: skipped, and said). Printed with the count."""
    pinned = pin["program_name"]
    model_name = data.get("program_name").strip() if isinstance(data.get("program_name"), str) else ""
    model_brand = data.get("brand_html") if isinstance(data.get("brand_html"), str) else ""
    n = 0
    if model_name and model_name != pinned:
        if len(model_name.split()) >= 2 or re.search(r"[A-Za-z][A-Z]", model_name):
            if model_brand and pin.get("brand_html") and model_brand != pin["brand_html"]:
                data, k = _swap_name(data, model_brand, pin["brand_html"]); n += k
            data, k = _swap_name(data, model_name, pinned); n += k
            brief_raw, k = _swap_name(brief_raw, model_name, pinned); n += k
            NR.say(f"    identity: the model wrote {model_name!r}; replaced by {pinned!r} ({n} occurrence(s))")
        else:
            NR.say(f"    identity: the model wrote {model_name!r}, a single plain word: the name fields are pinned, the text is left as written")
    data["program_name"] = pinned
    if pin.get("brand_html"):
        data["brand_html"] = pin["brand_html"]
    elif NR.clean_text(data.get("brand_html") or "") != pinned:
        data["brand_html"] = pinned
    if pin.get("domain_label"):
        data["domain_label"] = pin["domain_label"]
    return data, brief_raw


# ─────────────────────────────────────────────────────────────────────
# P0.7 — BLOCK 2 CARRIES BLOCK 1 FORWARD (D3): one update call over Block 1's analysis and the change list; Python decides which
# amendments stand, recomputes every figure with the existing financial stage, and records what moved and why (curveball.json).
# ─────────────────────────────────────────────────────────────────────
NARRATIVE_FIELDS = ("badge_text", "hero_title", "hero_lead", "summary_title", "bluf_text", "decision_text", "crisis_title", "crisis_body",
                    "crisis_human", "crisis_sources", "solution_title", "solution_body", "solution_population", "cost_of_inaction",
                    "evidence_title", "operations_title", "operations_description", "plan_title", "financial_title", "sensitivity_text",
                    "renewal_label", "renewal_text", "appendix_title", "methodology", "ai_process",
                    "comparison_rows", "evidence_cards", "phases", "risks", "assumptions", "mermaid_code")
IDENTITY_FIELDS = ("program_name", "brand_html", "domain_label", "domain", "city_name")
FUNDS_FIELDS = ("fund_lines", "marketing_allocation")       # BUILD B1 (D6): Block 1's use-of-funds plan stands at the curveball
ASSUMPTION_FIELDS = ("target_beneficiaries_y1", "growth_rate_y2", "growth_rate_y3", "ops_cost_y1", "tech_cost_y1", "outreach_cost_y1",
                     "staff_roles", "supplemental_grants_y2", "supplemental_grants_y3", "earned_revenue_y3", "sroi_outcomes", "deadweight",
                     "attribution", "displacement", "drop_off_rate")
UPDATE_TAG = "<block1_analysis>"     # the update prompt's own block: the mock Messages API recognises the update call by it
BLOCK1_FILES = ("master_analysis", "financial_model", "data_loading", "prioritization", "registry",
                "deliverables")   # B1: the ranking; B3 (D1): the registry; B3 (D3): the deliverables' words stand at the curveball
UPDATE_FAILED_LINE = ("The analysis was not updated for the curveball (the update call did not complete); the data changes below are "
                      "computed from the API.")
UPDATE_DECLINED_LINE = ("The analysis was not updated for the curveball: the model declined the update, so nothing of its reply was applied "
                        "and no item was reviewed; the data changes below are computed from the API.")     # BUILD IC-4 (D11): a refusal
# P0.7 r2 (E2): each cap ONE constant, used by the cut or the list cap AND stated in the rule that asks for the value (both paths)
UPDATE_IMPACT_CHARS = 1200          # a what_changed impact (rule 2; the full path's block)
UPDATE_TEST_CHARS = 1200            # a test's words (rule 7; the full path's block)
UPDATE_ANSWER_CHARS = 1200          # a test's answer (rule 7; the full path's block)
UPDATE_SUMMARY_CHARS = 1500         # the response summary (rule 3; the full path's block)
# BUILD IC-8 (D1): the stored analysis's list fields a page shows under a display policy — (field, key) → (DISPLAY_POLICY row, its constant).
# Stored whole (Block 1, the full path and an edit alike); build_template_context fits each where the page shows it.
ORACLE_FIELD_FITS = {("brief_answers", "answer"): ("oracle.update.answer", UPDATE_ANSWER_CHARS)}
UPDATE_WHY_CHARS = 400              # an assumption change's why (rule 5)
DERIVED_LABEL_CHARS = 160           # a derived figure's label (rule 6) — R7's live label was 137 characters (P0.7 cut at 120, unmarked)
UPDATE_WHAT_CHANGED_MAX = 20        # what_changed entries used (rule 2; the full path's block)
UPDATE_TESTS_MAX = 10               # test answers used (rule 7; the full path's block)
UPDATE_DERIVED_MAX = 8              # derived figures used (rule 6)
# P0.7 r2 Phase 5 (C14): the top-level keys the update reply is asked for (the prompt's last line) — any other key is named, not used
UPDATE_REPLY_KEYS = ("what_changed", "response_summary", "amended", "because", "assumption_changes", "derived_figures", "test_answers",
                     "brief_answers")
# P0.7 r2 Phase 5 (E1, with C2 and C14): the two limits the merge enforces that no schema block shows — said in words, one sentence each;
# the review's #7: the reply-keys sentence also names an entry's own keys, reasons for a part not amended and a key given twice
UPDATE_ITEM_KEYS_LINE = "An item's keys are the ones its schema lists; any other key is not used, and the page says so."
UPDATE_REPLY_KEYS_LINE = ("Any other key — here, in an entry of a rule above or in because for a part not amended — is not used, a key given "
                          "twice in one object keeps only its last value, and the page says so.")
# the review's #2: rule 5's list inputs — the enforcement of its own sentence ("keep the keys and number types of Block 1's own items"), stated;
# N3: a key none of Block 1's items has is removed from the applied items (so "not used" is true); with no Block 1 items nothing is kept or named
UPDATE_LIST_ITEMS_LINE = "Where Block 1 has items, a value whose items do not keep them is not applied, and a key none of them has is not used."
# the review's #7 (b): each list entry's own keys (the forms rules 2, 5, 6 and 7 and the brief's answers ask for); any other key is named
ENTRY_KEYS = {"what_changed": ("ref", "impact"), "test_answers": ("test", "answer"), "assumption_changes": ("field", "new", "because", "why"),
              "derived_figures": ("label", "endpoint", "record_ids", "field", "minus_field", "compare_with"), "compare_with": ("endpoint", "field"),
              "brief_answers": ("id", "answer", "text", "because"), "brief_answers_value": ("answer", "text", "because")}
NOT_APPLIED_UPDATE = ("Python did not apply these parts of the update; where a part would have replaced Block 1's, Block 1's version "
                      "stands.")                                              # E3 (b): the update path
NOT_APPLIED_FULL = "Python did not use these parts of the reply."           # E3 (b): the --curveball-full and no-base paths
# E3 (a): the plain name of every property the amendable fields' schema declares (a <where> in a reason: "item 3's progress meter")
ORACLE_PROPERTY_NAMES = {
    "dimension": "dimension", "today": "current situation", "proposed": "proposed change",
    "title": "title", "stat_value": "figure", "stat_class": "figure's style", "stat_label": "figure's label", "body": "text", "source": "source",
    "summary": "summary", "content": "content", "timeline_label": "timeline label", "timeline_suffix": "timeline note", "meter_pct": "progress meter",
    "risk": "risk", "likelihood": "likelihood", "impact": "impact", "mitigation": "mitigation"}


def is_block2() -> bool:
    return bool(os.environ.get("CURVEBALL_TEXT", "").strip())


def curveball_object(changes) -> dict:
    """The announcement this run works from: the change list's curveball (the sidecar's fields), else the --curveball text alone."""
    cv = changes.get("curveball") if isinstance(changes, dict) else None
    if isinstance(cv, dict) and isinstance(cv.get("text"), str) and cv["text"].strip():
        return REG.debracket_strings(cv)                       # IC-3 (D4): the announcement's own text never holds a token's brackets
    return {"kind": "VERBATIM", "source": "--curveball", "announced_at": None, "text": REG._debracket(os.environ.get("CURVEBALL_TEXT", "").strip()),
            "tests": [], "api_changed": []}


def announcement_source(changes=None) -> str:
    """BUILD IC-2 (D2; review a F14): the announcement a quote may name — the text as given (--curveball) and the tests it states (the
    change list's curveball, which the update prompt shows the model as the announcement's tests), one per line. "" at Block 1."""
    text = REG._debracket(os.environ.get("CURVEBALL_TEXT", "").strip())       # IC-3 (D4): as the request prints it
    if not text:
        return ""
    if changes is None:
        changes = REG.admit(NR.changes_from_env(), "data", "change list")[0] or {}   # BUILD IC-5 (D1)
    tests = [t.strip() for t in curveball_object(changes).get("tests") or [] if isinstance(t, str) and t.strip()]
    return "\n".join([text] + tests)


def _cp_data(path):
    """A checkpoint file's "data" ({"stage", "ts", "data"}), or None; never raises. BUILD IC (D1): private-use characters removed."""
    d, _why = NR.read_json(path)
    return REG.admit(d.get("data"), "saved", str(path)) if isinstance(d, dict) else None   # BUILD IC-5 (D1)


def write_block1_base(challenge, run_id, done=()):
    """D3: after a Block 1 run whose master analysis completed — checkpoints/block1/ = byte copies of master_analysis.json,
    financial_model.json and data_loading.json, each only when its stage is among this run's results (`done`: a stage that failed this
    run leaves an older checkpoint file behind, which is never copied), and meta.json {challenge, run_id, written_at, files}; a stale
    copy of a file this run did not produce is removed (the folder is a copy, never the original). Block 2 reads it and never writes it;
    the pipeline's next Block 1 archives it with the rest of checkpoints/. Returns None or the reason; never raises."""
    base = CHECKPOINT_DIR / NR.BLOCK1_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
        files = []
        for name in BLOCK1_FILES:
            src, dst = CHECKPOINT_DIR / f"{name}.json", base / f"{name}.json"
            if name == "registry":       # BUILD B3 (D1): the page's registry — not a stage: copied when this run wrote it
                rg, _w = REG.read(src, challenge, run_id)
                done = set(done) | ({"registry"} if rg is not None else set())
            if name in done and src.is_file():
                tmp = base / f".{name}.json.{os.getpid()}.tmp"
                tmp.write_bytes(src.read_bytes())
                os.replace(tmp, dst)
                files.append(name)
            elif dst.exists():
                dst.unlink()
        why = NR.write_json(base / NR.BLOCK1_META, {"challenge": challenge, "run_id": run_id,
                                                     "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files})
        return why
    except Exception as e:  # noqa: BLE001 — a failed base never fails Block 1
        return f"{type(e).__name__}: {e}"


def load_block1_base(challenge, want_run=None, detail=None):
    """D3: Block 1's base of this challenge (and of the change list's Block 1 run when it names one) → ({"ma", "fin", "raw", "meta"},
    None) or (None, reason). The analysis must validate under today's schema (a base written by older code that no longer validates →
    None: the full path). Never raises. P0.7 r2 (E3 a): the reason reaches the page and the chat, so it is plain words with no local
    path and no exception name; `detail` (a dict, optional) receives the technical part for the console ({"why": …})."""
    base = CHECKPOINT_DIR / NR.BLOCK1_DIR
    meta, why = REG.admit(NR.read_json(base / NR.BLOCK1_META), "saved", NR.BLOCK1_META)   # BUILD IC-5 (D1): a file a run wrote
    if not isinstance(meta, dict):      # Phase 5 review #10: the folder path only in the console's detail, never in the reason
        nf = str(why or "").startswith("not found")
        if isinstance(detail, dict):
            detail["why"] = why if nf else f"checkpoints/{NR.BLOCK1_DIR}/{NR.BLOCK1_META}: {why or 'not an object'}"
        return None, ("no Block 1 analysis was saved" if nf else "the saved Block 1 analysis could not be read")
    if challenge and meta.get("challenge") != challenge:
        return None, f"the Block 1 base is of {meta.get('challenge')}, not {challenge}"
    if want_run and meta.get("run_id") != want_run:
        return None, f"the Block 1 base is of run {meta.get('run_id')}, the change list's Block 1 is run {want_run}"
    ma = _cp_data(base / "master_analysis.json")
    if not isinstance(ma, dict):
        return None, "the Block 1 base holds no readable master analysis"
    try:
        MasterAnalysis(**{k: v for k, v in ma.items() if k in MasterAnalysis.model_fields})
    except Exception as e:  # noqa: BLE001
        if isinstance(detail, dict):
            detail["why"] = type(e).__name__
        return None, "the Block 1 analysis no longer fits today's format"
    dl = _cp_data(base / "data_loading.json")
    fin = _cp_data(base / "financial_model.json")
    return {"ma": ma, "fin": fin if isinstance(fin, dict) else None, "raw": (dl or {}).get("raw") if isinstance(dl, dict) else None,
            "meta": meta}, None


def field_schema(model, field: str) -> dict:
    """P0.7 r2 (E3 a): one field's JSON schema with its model's $defs — the same stripped schema the update prompt shows (NR.schema_block)."""
    j = json.loads(NR.schema_block(model.model_json_schema(), [field]))
    return dict(j["properties"][field], **({"$defs": j["$defs"]} if "$defs" in j else {}))


def validation_reason(e, field, schema: dict, names: dict):
    """P0.7 r2 (E3 a): a check's failure → (the plain reason, the technical detail). A ValidationError's errors() are read as plain dicts
    (include_url=False, include_input=False: never the model's text), each location made relative to the field, and put in plain
    words by NR.plain_reason; pydantic's own messages (type, location, msg) are the detail — the record keeps it, no page shows it. Any
    other exception → "the value could not be checked", its name the detail."""
    if isinstance(e, ValidationError):
        errs = e.errors(include_url=False, include_input=False)
        rel = [dict(x, loc=list(x.get("loc") or ())[1:]) if list(x.get("loc") or ())[:1] == [field] else dict(x, loc=[]) for x in errs]
        detail = "; ".join(f"{x.get('type')} at {'.'.join(str(p) for p in (x.get('loc') or ())) or '(the value)'}: {x.get('msg')}" for x in errs)
        return NR.plain_reason(rel, schema, names), NR.cut(detail, NR.DETAIL_CHARS)
    return "the value could not be checked", type(e).__name__


def _field_check(base_ma: dict):
    """A per-field schema check: the Block 1 analysis with this one field replaced must validate. check(field, value) → None or
    (the plain reason, the technical detail) — NR.merge_amended's form (P0.7 r2, E3 a); check.value(field, value) → (the value as
    the schema stores it, None) or (None, (plain reason, detail)) — the schema coerces (a numeric string becomes a number), so what is
    kept is the stored form, as Block 1's model_dump() gives it."""
    clean = {k: v for k, v in base_ma.items() if k in MasterAnalysis.model_fields}

    def value(field, v):
        try:
            return MasterAnalysis(**dict(clean, **{field: v})).model_dump()[field], None
        except Exception as e:  # noqa: BLE001 — every failure named in plain words (E3 a)
            try:
                sch = field_schema(MasterAnalysis, field)
            except Exception:  # noqa: BLE001
                sch = {}
            return None, validation_reason(e, field, sch, ORACLE_PROPERTY_NAMES)

    def check(field, v):
        return value(field, v)[1]
    check.value = value
    return check


def escape_amended(field: str, value):
    """D11: an amendment is new text from an update reply — NR.escape_markup (one rule for the Oracle and the Muse): '<' and '>'
    escaped, the attribute-free tags Block 1's narrative uses (<strong> <em> <b> <i> <br> <p> <ul> <ol> <li>) restored; the diagram
    code is escaped whole (Mermaid reads the decoded text: '--&gt;' draws '-->')."""
    return NR.escape_markup(value, keep_tags=(field != "mermaid_code"))


def reply_outside(raw) -> int:
    """P0.7 r2 Phase 5 (C16): the characters of a reply outside the JSON object extract_json reads — the reply stripped, its outer code
    fences removed as extract_json removes them, then everything before the first '{' and after the last '}', stripped. 0 when none."""
    t = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip()) if isinstance(raw, str) else ""
    t = re.sub(r'\n?```\s*$', '', t.strip())
    s, e = t.find("{"), t.rfind("}")
    return len((t[:s] + t[e + 1:]).strip()) if s != -1 and e > s else 0


def parse_update_reply(raw: str):
    """The update reply as a dict, or (None, reason) — never raises."""
    try:
        got = _loads_reply(extract_json(raw), "update")   # BUILD IC-5 (D1): admitted inside   # BUILD IC (D1): no private-use characters; IC-3 (D4): strict
    except Exception as e:  # noqa: BLE001
        return None, f"the reply is not JSON ({type(e).__name__})"
    return (got, None) if isinstance(got, dict) else (None, f"the reply is a {type(got).__name__}, not an object")


def _drop(part, field, why, detail=""):
    """P0.7 r2 (E3): one part of the reply not applied — {part, field, why (plain words), detail (technical; the record only)}."""
    return {"field": field, "part": part, "why": why, "detail": NR.cut(str(detail or ""), NR.DETAIL_CHARS)}


def fixed_reason(f, reserve=None):
    """P0.7 r2 Phase 5 (review #4): the TRUE reason a field outside NARRATIVE_FIELDS is not amended — one per class of field (P0.7 said
    'a financial input or the budget — it changes only through assumption_changes' for all of them, false for the budget, the contract's
    length, the method parameters and, under the brief's reserve, Year 1 outreach)."""
    if f in IDENTITY_FIELDS:
        return "part of the program's identity — an update never changes it"
    if f in FUNDS_FIELDS:      # BUILD B1 (D6): the true reason for the two use-of-funds fields
        return "Block 1's use-of-funds plan — an update never changes it"
    if f in ("contract_value", "contract_years"):
        return "not an input an update may change"
    if f in ("indirect_rate", "discount_rate"):
        return "a method parameter — an update never changes it"
    if f == "outreach_cost_y1" and reserve:
        return "the brief's marketing reserve sets Year 1 outreach"
    return "a financial input — it changes only through an assumption change"


def list_items_check(new, ref_items):
    """P0.7 r2 Phase 5 (review #2): a list input's new items against Block 1's own items — rule 5 says they "keep the keys and number types
    of Block 1's own items", because the financial stage fills a missing key with its own default (a salary of 50,000, a unit value of 0).
    → (the 1-based number of the first item that does not keep them, or None; the technical detail; the keys no Block 1 item has, in the
    order met — merge_update names them and removes them from the applied items, N3). A key every Block 1 item holds must be in each new
    item, and a number (not true/false) wherever every Block 1 item holds one. No Block 1 items → nothing to keep (rule 5's line says
    "Where Block 1 has items")."""
    if not ref_items:
        return None, "", []
    need = set.intersection(*(set(x) for x in ref_items))
    num = {k for k in need if all(_is_num(x.get(k)) for x in ref_items)}
    known = set().union(*(set(x) for x in ref_items))
    extra = []
    for k_, it in enumerate(new):
        if not isinstance(it, dict):
            return k_ + 1, f"item {k_ + 1} is a {type(it).__name__}", []
        miss, nn = sorted(need - set(it)), sorted(k for k in num if k in it and not _is_num(it[k]))
        if miss or nn:
            return k_ + 1, "; ".join(x for x in ((f"item {k_ + 1} lacks {miss}" if miss else ""), (f"item {k_ + 1}: not a number {nn}" if nn else "")) if x), []
        extra += [str(k) for k in it if k not in known and str(k) not in extra]
    return None, "", extra


def with_item_ids(ma: dict, base_ma=None) -> dict:
    """BUILD IC-4 (D1): the analysis with its item ids stored apart from its items (`item_ids`: field → ids, their statuses, the
    tombstones) — assigned in its order at Block 1; at Block 2 carried from Block 1's analysis (`base_ma`): an item with Block 1's words
    keeps its id, a rewritten one keeps it when its words are the nearest by a clear margin, a new one takes the next free number, a
    removed one is retired (REG.carry_item_ids). Its own ids are set aside first when carrying (they were Block 1's)."""
    m = {k: v for k, v in ma.items() if k != "item_ids"} if isinstance(base_ma, dict) else ma
    ids_ = REG.analysis_item_ids(m, base_ma if isinstance(base_ma, dict) else None)
    if isinstance(base_ma, dict):        # IC-4 Phase 7 (review a8, D1): a carry by words is said — never decided silently
        n_ = {}
        for st_ in (ids_.get("status") or {}).values():
            for s_ in st_:
                n_[s_] = n_.get(s_, 0) + 1
        NR.say("    item ids carried from Block 1 by their words: " + (", ".join(f"{v} {k}" for k, v in sorted(n_.items())) or "no item")
               + f"; {len([t for t in ids_.get('tombstones') or [] if isinstance(t, dict)])} retired"
               + (" (an ambiguous item gets a new id, never Block 1's)" if n_.get("ambiguous") else ""))
    ma["item_ids"] = REG.item_ids_doc(ids_)
    return ma


def analysis_ids(ma: dict, doc=None) -> dict:
    """BUILD IC-4 (D1): this run's item ids of an analysis (REG.analysis_item_ids) — its own, stored at parse; at Block 2 an analysis
    written before IC-4 (no ids: the run turn's R4 rebuilds one) has them carried from Block 1's base."""
    base = None
    if is_block2():
        own = (ma or {}).get("item_ids") if isinstance((ma or {}).get("item_ids"), dict) else {}
        if not all(isinstance(own.get(f), list) and len(own[f]) == len((ma or {}).get(f) or []) for f in REG.ANALYSIS_ITEM_FIELDS):
            base = (load_block1_base((((doc if doc is not None else _brief_contract()) or {}).get("challenge") or {}).get("id"))[0] or {}).get("ma")
    return REG.analysis_item_ids(ma or {}, base)


def merge_update(base_ma: dict, reply: dict, refs: dict, reserve, doc):
    """D3 — Python decides what stands. Returns (merged analysis, info): narrative fields through NR.merge_amended (allowed, a real ref,
    the schema); the allowed assumption changes applied (the brief's reserve sets outreach; indirect_rate and discount_rate never
    change); brief answers replaced only for items whose new answer names a real ref. Every drop printed by name with its reason.
    P0.7 r2 (E2, E3): every part not applied is an entry of info["dropped"] — {part, field, why (plain), detail (technical)} — with
    its part (amendment, assumption change, brief answer, shape); a ref that is not text is not a ref of the list (NR.ref_check);
    brief_answers read in both forms Block 1 accepts (a list of {id, answer, because} or an object id → {answer, because}), each
    answer paired with its own reasons; an assumption change's why fitted by REG.fit_cap at UPDATE_WHY_CHARS (stated in rule 5).
    P0.7 r2 Phase 5 (C2): a kept amendment whose items carry keys their model does not declare (pydantic ignores them; the stored form
    drops them) → one entry naming those keys, the rest of the amendment applied. The independent review: each amendment escaped BEFORE
    its check (#1); a field outside the narrative refused with its class's TRUE reason (#4, fixed_reason); reasons given for a part not
    amended named (#7 c); a list input whose items do not keep Block 1's items' keys and number types not applied, a key no Block 1 item has
    named and removed from the applied items (#2, list_items_check; N3); an applied change's own extra keys and an accepted brief answer's
    named (#7 b)."""
    check = _field_check(base_ma)
    amended = reply.get("amended")
    if isinstance(amended, dict):   # D11: new text from the reply, escaped (Block 1's narrative tags restored) — Phase 5 review #1: BEFORE
        amended = {k: escape_amended(str(k), v) for k, v in amended.items()}   # the check, so what is checked is what is stored (the Muse's order)
    allowed = set(NARRATIVE_FIELDS)
    details = {}
    merged, kept, dropped0 = NR.merge_amended(base_ma, amended, reply.get("because"), allowed, refs, check, details=details)
    drops = []
    for f, why in dropped0:
        if f == "amended":
            drops.append(_drop("shape", f, why, details.get(f)))
        elif f in MasterAnalysis.model_fields and f not in allowed:     # identity, budget, method parameter, financial input (review #4)
            drops.append(_drop("amendment", f, fixed_reason(f, reserve)))
        else:
            drops.append(_drop("amendment", f, why, details.get(f)))
    for f, _rs in kept:
        stored, bad = check.value(f, merged[f])   # P0.7 r2 Phase 5 (C2): a key the item's model does not declare is gone from the stored
        xk = NR.extra_keys(merged[f], stored) if bad is None else []                   # form (pydantic ignores it) — named, the rest applied
        if xk:
            drops.append(_drop("amendment", f, NR.extra_keys_reason(xk), "keys its pydantic class does not declare: " + json.dumps(xk, ensure_ascii=False)))
    orphans = NR.orphan_reasons(reply.get("amended"), reply.get("because"))    # Phase 5 review #7 (c): reasons for a part not amended
    if orphans:
        drops.append(_drop("shape", "because", NR.orphan_reasons_reason(orphans), "keys: " + json.dumps(orphans, ensure_ascii=False)))
    changes_applied = []
    a_list = reply.get("assumption_changes")
    if a_list is not None and not isinstance(a_list, list):
        drops.append(_drop("shape", "assumption_changes", "not a list — nothing changed", f"a {type(a_list).__name__}"))
        a_list = []
    for j, ch in enumerate(a_list or []):
        try:
            if not isinstance(ch, dict):
                drops.append(_drop("assumption change", f"assumption_changes[{j}]", "not in the expected form", f"a {type(ch).__name__}"))
                continue
            f0 = ch.get("field")
            f = f0 if isinstance(f0, str) else None
            if f not in ASSUMPTION_FIELDS:
                if f in ("indirect_rate", "discount_rate"):
                    drops.append(_drop("assumption change", f, "not an assumption an update may change — a method parameter"))
                elif f in IDENTITY_FIELDS or f in ("contract_value", "contract_years"):     # Phase 5 review #4: the class's own reason
                    drops.append(_drop("assumption change", f, fixed_reason(f)))
                else:
                    drops.append(_drop("assumption change", NR.name_cut([f0], quote=not isinstance(f0, str)), "not an assumption an update may change"))
                continue
            if f == "outreach_cost_y1" and reserve:
                drops.append(_drop("assumption change", f, "the brief's marketing reserve sets Year 1 outreach"))
                continue
            rs, why = NR.ref_check(ch.get("because"), refs)
            if why:
                drops.append(_drop("assumption change", f, why))
                continue
            # D11 (review F11): a list input's text (an SROI outcome's category and source, a role's title) is new text from the reply,
            # escaped — Phase 5 review #1: BEFORE the check, as the amendments; numbers pass as they are
            new, bad = check.value(f, NR.escape_markup(ch.get("new")))      # the schema's stored form (a numeric string becomes a number)
            if bad:
                drops.append(_drop("assumption change", f, bad[0], bad[1]))
                continue
            ref_items = [x for x in base_ma.get(f) if isinstance(x, dict)] if isinstance(base_ma.get(f), list) else []
            bad_k, bad_d, item_x = list_items_check(new, ref_items) if isinstance(new, list) else (None, "", [])
            if bad_k:                                     # Phase 5 review #2: rule 5's own sentence, enforced — never the stage's defaults
                drops.append(_drop("assumption change", f, f"item {bad_k} does not keep the keys and number types of Block 1's items", bad_d))
                continue
            if item_x:                                    # N3: a key no Block 1 item has is REMOVED (named below): the stage, the page's change
                new = [{k: v for k, v in it.items() if str(k) not in item_x} for it in new]   # row and the record never hold it — "not used" true
            try:                                          # the existing financial stage must compute with it
                _fin_quiet(dict(merged, **{f: new}))
            except Exception as e:  # noqa: BLE001
                drops.append(_drop("assumption change", f, "the financial model cannot be computed with it", type(e).__name__))
                continue
            why_raw = ch.get("why")
            if why_raw is not None and not isinstance(why_raw, str):
                drops.append(_drop("assumption change", f, "its reason's words are not text — not shown", f"a {type(why_raw).__name__}"))
            own_x = [str(k) for k in ch if k not in ENTRY_KEYS["assumption_changes"]]          # review #7 (b): the change's own other keys
            if own_x:
                drops.append(_drop("assumption change", f, NR.extra_keys_reason(own_x), "keys: " + json.dumps(own_x, ensure_ascii=False)))
            if item_x:                                                       # review #2: an item key no Block 1 item has (removed above, N3)
                drops.append(_drop("assumption change", f, NR.extra_keys_reason(item_x),
                                   "item keys no Block 1 item has, removed from the applied items: " + json.dumps(item_x, ensure_ascii=False)))
            changes_applied.append({"field": f, "old": merged.get(f), "new": new, "because": rs,
                                    "why": REG.fit_cap(NR.clean_text(why_raw), "oracle.update.why", UPDATE_WHY_CHARS)})
            merged[f] = new
        except Exception as e:  # noqa: BLE001
            drops.append(_drop("assumption change", f"assumption_changes[{j}]", "not in a form Python can use", type(e).__name__))
    # the brief's answers: Block 1's stand unless the curveball changes one (with a reason)
    b_kept = []
    ans = {e["id"]: e["answer"] for e in (base_ma.get("brief_answers") or []) if isinstance(e, dict) and "id" in e}
    raw_ba = reply.get("brief_answers")
    if raw_ba is not None and not doc:
        drops.append(_drop("shape", "brief_answers", "there is no brief in this run — not used"))
    elif raw_ba is not None:
        ids = [i for i, _ in NR.brief_items(doc, "oracle")]
        new_ans, notes = NR.parse_brief_answers(raw_ba, ids, plain=True)
        pairs = ([(e.get("id"), e) for e in raw_ba if isinstance(e, dict)] if isinstance(raw_ba, list) else
                 list(raw_ba.items()) if isinstance(raw_ba, dict) else [])
        why_by_id, entry_by_id = {}, {}   # each accepted answer's OWN entry: the first of an id whose answer is text (parse_brief_answers' rule)
        for k, e in pairs:
            a = NR._answer_of(e) if isinstance(e, dict) else e
            i = NR.norm_id(k)
            if i not in why_by_id and isinstance(a, str) and a.strip():
                why_by_id[i] = e.get("because") if isinstance(e, dict) else None
                entry_by_id[i] = e if isinstance(e, dict) else {}
        own = ENTRY_KEYS["brief_answers"] if isinstance(raw_ba, list) else ENTRY_KEYS["brief_answers_value"]
        for i, a in new_ans.items():
            rs, why = NR.ref_check(why_by_id.get(i), refs)
            if why:
                drops.append(_drop("brief answer", i, why))
            else:
                ans[i] = a
                b_kept.append((i, rs))
                bx = [str(k) for k in entry_by_id.get(i) or {} if k not in own]     # Phase 5 review #7 (b): the entry's other keys
                if bx:
                    drops.append(_drop("brief answer", i, NR.extra_keys_reason(bx), "keys: " + json.dumps(bx, ensure_ascii=False)))
        drops += [_drop(n["part"], n["field"], n["why"], n["detail"]) for n in notes]
        merged["brief_answers"] = [{"id": i, "answer": ans[i]} for i in ids if i in ans]
    for d in drops:
        NR.say(f"    update: dropped {d['field']} — {d['why']}")
    for f, rs in kept:
        NR.say(f"    update: amended {f} ({', '.join(rs)})")
    for c in changes_applied:
        NR.say(f"    update: assumption {c['field']} {c['old']} → {c['new']} ({', '.join(c['because'])})")
    for f in ("bluf_text", "decision_text"):
        if f not in dict(kept):
            NR.say(f"    update: {f} is Block 1's (the update did not amend it)")
    info = {"amended": [{"field": f, "because": rs} for f, rs in kept], "dropped": drops,
            "assumption_changes": changes_applied, "brief_answers_changed": [{"id": i, "because": rs} for i, rs in b_kept]}
    return merged, info


def take_entries(reply: dict, key: str, cap: int, caps: dict, label: str, need=None):
    """P0.7 r2 (E2, E3): a list part of the reply (what_changed, test_answers) → (items, drops). At most `cap` entries are used — the rest
    are one entry of drops ("N more than <cap> — not used", the cap stated in the rule that asks for the list); an entry that is not an
    object, or whose given values are not text, or that is empty, is one entry each; a value over its cap (`caps`: {key: characters})
    is cut by NR.cut — marked, printed once (the caller runs once per run). A part that is not a list → one entry (shape).
    P0.7 r2 Phase 5 (C5): `need` ({key: reason}) — an entry without text under such a key is one entry with that reason (a test answer
    with no answer: the page could not show it). Review #7 (b): a kept entry's keys beyond `caps` → one entry naming them, the rest used."""
    v = reply.get(key)
    if v is None:
        return [], []
    if not isinstance(v, list):
        return [], [_drop("shape", key, "not a list — not used", f"a {type(v).__name__}")]
    items, drops = [], []
    for j, e in enumerate(v[:cap]):
        if not isinstance(e, dict) or any(e.get(k) is not None and not isinstance(e.get(k), str) for k in caps):
            drops.append(_drop("list", f"{key}[{j}]", "not in the expected form", f"a {type(e).__name__}" if not isinstance(e, dict) else "a value is not text"))
            continue
        clean = {k: NR.clean_text(e.get(k)) for k in caps}
        if not any(clean.values()):
            drops.append(_drop("list", f"{key}[{j}]", "empty — not used"))
            continue
        missing = [why for k, why in (need or {}).items() if not clean.get(k)]
        if missing:                                   # decided before any cut, so a part not used prints no "shortened" line
            drops.append(_drop("list", f"{key}[{j}]", missing[0]))
            continue
        items.append({k: REG.fit_cap(clean[k], "oracle.update.entry", n) for k, n in caps.items()})
        xk = [str(k) for k in e if k not in caps]    # Phase 5 review #7 (b): a kept entry's keys beyond its rule's form
        if xk:
            drops.append(_drop("list", f"{key}[{j}]", NR.extra_keys_reason(xk), "keys: " + json.dumps(xk, ensure_ascii=False)))
    if len(v) > cap:
        drops.append(_drop("list", key, f"{len(v) - cap} more than {cap} — not used"))
    return items, drops


def take_what_changed(reply: dict, refs: dict, label: str):
    """P0.7 r2 (E2, E3): what_changed → (kept [{ref, impact}], drops). At most UPDATE_WHAT_CHANGED_MAX entries are used (the rest one
    entry of drops); an entry that is not an object or whose ref or impact is not text, an empty one, one whose ref is not a change in
    the list (named, cut to NR.NAME_CHARS) and one with no impact are one entry each, each with its own reason — P0.7 printed the
    unknown refs and recorded nothing. The ref is matched, never cut; the impact is fitted by REG.fit_cap at UPDATE_IMPACT_CHARS (rule 2).
    Review #6: an entry with an impact and no ref → "it names no change in the list"; #7 (b): a kept entry's keys beyond ref and impact named."""
    v = reply.get("what_changed")
    if v is None:
        return [], []
    if not isinstance(v, list):
        return [], [_drop("shape", "what_changed", "not a list — not used", f"a {type(v).__name__}")]
    kept, drops = [], []
    for j, e in enumerate(v[:UPDATE_WHAT_CHANGED_MAX]):
        if not isinstance(e, dict) or any(e.get(k) is not None and not isinstance(e.get(k), str) for k in ("ref", "impact")):
            drops.append(_drop("list", f"what_changed[{j}]", "not in the expected form", f"a {type(e).__name__}" if not isinstance(e, dict) else "a value is not text"))
            continue
        r, impact = e.get("ref"), NR.clean_text(e.get("impact"))
        ref = r.strip().lower() if isinstance(r, str) else ""
        if not ref and not impact:
            drops.append(_drop("list", f"what_changed[{j}]", "empty — not used"))
        elif not ref:                                   # Phase 5 review #6: no ref given (absent, null, white space) — never "(null)"
            drops.append(_drop("list", f"what_changed[{j}]", "it names no change in the list"))
        elif ref not in refs:
            drops.append(_drop("list", f"what_changed[{j}]", f"its ref is not a change in the list ({NR.name_cut([r])})"))
        elif not impact:
            drops.append(_drop("list", f"what_changed[{j}]", "no impact given"))
        else:
            kept.append({"ref": refs[ref], "impact": REG.fit_cap(impact, "oracle.update.impact", UPDATE_IMPACT_CHARS)})
            xk = [str(k) for k in e if k not in ENTRY_KEYS["what_changed"]]          # review #7 (b): a kept entry's other keys
            if xk:
                drops.append(_drop("list", f"what_changed[{j}]", NR.extra_keys_reason(xk), "keys: " + json.dumps(xk, ensure_ascii=False)))
    if len(v) > UPDATE_WHAT_CHANGED_MAX:
        drops.append(_drop("list", "what_changed", f"{len(v) - UPDATE_WHAT_CHANGED_MAX} more than {UPDATE_WHAT_CHANGED_MAX} — not used"))
    return kept, drops


def take_text(reply: dict, key: str, cap: int, label: str):
    """P0.7 r2 (E2, E3): a text part of the reply (the response summary) → (text, drops): None → ""; text → NR.cut at `cap` (marked,
    printed once); anything else → one entry of drops (shape) — P0.7 made it "" silently."""
    v = reply.get(key)
    if v is None:
        return "", []
    if not isinstance(v, str):
        return "", [_drop("shape", key, "not text — not used", f"a {type(v).__name__}")]
    return REG.fit_cap(NR.clean_text(v), "oracle.update.text", cap), []


def take_derived(reply: dict):
    """P0.7 r2 (E2, E3): derived_figures → (the requests, drops): the objects among them, at most UPDATE_DERIVED_MAX (rule 6); each entry
    that is not an object, the overflow, and a part that is not a list are entries of drops — P0.7 dropped them silently. Review #7 (b): a
    used request's keys beyond rule 6's form (and compare_with's beyond its endpoint and field) → one entry naming them."""
    v = reply.get("derived_figures")
    if v is None:
        return [], []
    if not isinstance(v, list):
        return [], [_drop("shape", "derived_figures", "not a list — not used", f"a {type(v).__name__}")]
    drops = [_drop("list", f"derived_figures[{j}]", "not in the expected form", f"a {type(d).__name__}") for j, d in enumerate(v) if not isinstance(d, dict)]
    idx = [j for j, d in enumerate(v) if isinstance(d, dict)]
    for j in idx[:UPDATE_DERIVED_MAX]:              # Phase 5 review #7 (b): a request's keys beyond rule 6's form (compare_with's own too)
        d = v[j]
        form = {k: d[k] for k in ENTRY_KEYS["derived_figures"] if k in d}
        if isinstance(d.get("compare_with"), dict):
            form["compare_with"] = {k: d["compare_with"][k] for k in ENTRY_KEYS["compare_with"] if k in d["compare_with"]}
        xk = NR.extra_keys(d, form)
        if xk:
            drops.append(_drop("list", f"derived_figures[{j}]", NR.extra_keys_reason(xk), "keys: " + json.dumps(xk, ensure_ascii=False)))
    if len(idx) > UPDATE_DERIVED_MAX:
        drops.append(_drop("list", "derived_figures", f"{len(idx) - UPDATE_DERIVED_MAX} more than {UPDATE_DERIVED_MAX} — not used"))
    return [v[j] for j in idx[:UPDATE_DERIVED_MAX]], drops


_UPDATE_RULE8 = (   # BUILD IC-3 (D10): one set of rules — the update's rule 8 points to it with the switch on; IC-2's words with it off
    "8. Every quantity follows <number_rules> (ids recomputed on this run's data); [[b1.<id>]] is Block 1's value (only the b1 ids\n"
    "   <registry> lists). Never restate a figure as changed unless an input changed. A mention <changed_mentions> names is the\n"
    "   exception to rule 9: left as it is, it shows Block 1's words, flagged." if ic3_prompt() else
    "8. Every figure is a token, never digits you type: an id from <registry> (recomputed on this run's data), [[b1.<id>]] for Block 1's\n"
    "   value (only the b1 ids <registry> lists), or [[model:<number>|<what it is>]] for a number of your own" + _IC2_RULE8
    + "; never restate a figure as changed unless an input\n   changed.")


def update_prompt(base_ma, changes, cv, data_text, brief_doc, facts, lines: dict) -> str:
    """D3's update prompt: Block 1's analysis, the change list (capped), this run's data, the brief block with the announcement and its
    tests, the pinned identity and the master prompt's own budget lines. Event-neutral: every example describes a form."""
    b1 = {k: v for k, v in base_ma.items() if k != "brief_answers" and k in MasterAnalysis.model_fields}
    answers = [e for e in (base_ma.get("brief_answers") or []) if isinstance(e, dict)]
    brief = ("\n" + NR.brief_block(brief_doc, "oracle", facts, curveball=cv, update=True)) if brief_doc else ("\n" + facts_block(facts))
    reserve_rule = ("outreach_cost_y1 is the brief's marketing reserve and is set by Python — never change it." if lines.get("reserve") else
                    "outreach_cost_y1 may change only when a change in the list moves it.")
    return f"""<context>
This is BLOCK 2 of a two-block competition. In Block 1 you wrote the analysis in <block1_analysis> for this brief and its data. At the
curveball the organizers made the announcement below and the data changed; Python compared every endpoint record by record with Block 1's
snapshot (<changes>). UPDATE Block 1's analysis for what changed: keep everything the curveball does not touch exactly as it is, and
say what changed, by how much and why. A judge scores how you responded: a rethink of what the change touches, not a new submission.
{lines['budget_line']}{lines['reserve_line']}{lines['pilot_line']}{lines['unit_line']}{lines['budget_note']}{lines.get('block1_budget_line', '')}
ALL arithmetic will be computed in Python — output only qualitative changes and assumption values.
{lines['date_line']}
</context>{brief}{lines['identity_block']}

{UPDATE_TAG}
{json.dumps(b1, indent=1, ensure_ascii=False)}
</block1_analysis>

<block1_brief_answers>
{json.dumps(answers, indent=1, ensure_ascii=False)}
</block1_brief_answers>

{NR.changes_prompt_block(changes) if changes else NR.changes_prompt_block({"curveball": cv, "endpoints": None, "snapshot": {"source": "none"}})}
{"" if ic3_prompt() else lines.get("registry_block", "")}
<data>
{data_text}
</data>
{lines.get("after_data", "") if ic3_prompt() else ""}
<rules>
1. Cite every change by a ref from <changes>, exactly as written there: "announcement", an endpoint's name or a record's ref.
2. what_changed: one entry per change that matters for this program, at most {UPDATE_WHAT_CHANGED_MAX} entries: {{"ref": "<a ref>", "impact": "<1-2 plain
   sentences, at most {UPDATE_IMPACT_CHARS:,} characters: what it means for the program>"}}.
3. response_summary: 2-4 plain sentences on how the program responds, at most {UPDATE_SUMMARY_CHARS:,} characters.
4. amended: ONLY the narrative and structure fields the curveball changes, each as its complete new value of the same type as in Block 1's
   analysis. Allowed fields: {", ".join(NARRATIVE_FIELDS)}. Never {", ".join(IDENTITY_FIELDS)}, contract_value, contract_years,
   {", ".join(FUNDS_FIELDS)} (Block 1's use-of-funds plan) or a financial input. because: {{"<field>": ["<ref>", ...]}} for every amended field — a field without a real ref is dropped.
   Each amended field must validate against this schema — the one Block 1's analysis follows; a value that breaks it is not applied,
   and the page says so:
   {NR.schema_block(MasterAnalysis.model_json_schema(), NARRATIVE_FIELDS)}
   {UPDATE_ITEM_KEYS_LINE}
5. assumption_changes: a financial input changes ONLY here, and only when a change in the list moves it:
   {{"field": "<one of: {", ".join(ASSUMPTION_FIELDS)}>", "new": <value of the field's type>, "because": ["<ref>"], "why": "<one sentence,
   at most {UPDATE_WHY_CHARS:,} characters>"}}.
   indirect_rate and discount_rate are method parameters and never change. {reserve_rule}
   Each new value must validate against this schema — the one Block 1's analysis follows; a value that breaks it is not applied, and
   the page says so:
   {NR.schema_block(MasterAnalysis.model_json_schema(), ASSUMPTION_FIELDS)}
   A list input's items keep the keys and number types of Block 1's own items in <block1_analysis>: Python computes the financial model
   with the new value, and a value it cannot compute with is not applied.
   {UPDATE_LIST_ITEMS_LINE}
   When you change an input, also amend, in the same reply and with the same refs, every field of Block 1's analysis that states its
   Block 1 value — a narrative field through amended, the SROI table's or the staffing plan's own words through that input's change, a
   brief answer through brief_answers. The page lists every place that still states a Block 1 value this run changed.
6. derived_figures: at most {UPDATE_DERIVED_MAX}. When an aggregate in the data did not update but the records it summarises did, name the
   records for a figure Python computes in both blocks: {{"label": "<a short label, at most {DERIVED_LABEL_CHARS} characters — the records are
   listed beside it>", "endpoint": "<endpoint>", "record_ids": ["<record id>", ...] or "all", "field": "<numeric field>", "minus_field":
   "<numeric field, optional>", "compare_with": {{"endpoint": "<endpoint>", "field": "<field>"}} (optional)}}.
   Python shows the figure beside that aggregate with its own base — never as the aggregate's update. Do not compute it yourself.
7. test_answers: one {{"test": "<the test's words, at most {UPDATE_TEST_CHARS:,} characters>", "answer": "<2-4 plain sentences, at most
   {UPDATE_ANSWER_CHARS:,} characters>"}} per test the announcement states (none when it states none), at most {UPDATE_TESTS_MAX}.
{_UPDATE_RULE8}
9. Keep [CHALLENGE_DATA: endpoint-name] tags and every [[…]] token where Block 1's analysis uses them: Python writes in this run's
   figure.{_IC2_RULE9} Plain text in every new answer; HTML only where Block 1's field already holds it.
</rules>

Return ONLY one JSON object: {{"what_changed": [...], "response_summary": "...", "amended": {{...}}, "because": {{...}},
"assumption_changes": [...], "derived_figures": [...], "test_answers": [...], "brief_answers": [...]}}. {UPDATE_REPLY_KEYS_LINE} No markdown fences."""


# ─────────────────────────────────────────────────────────────────────
# BUILD B3 (P0.4, D1) — THE REGISTRY: every figure the page shows, built from the stage results
# ─────────────────────────────────────────────────────────────────────
COMPUTED_IDS = (   # (id, what it is) — the figures Python computes after the master analysis; the prompt lists them for the lock (D2)
    ("beneficiaries_y1", "{p} served in Year 1 (your target)"), ("beneficiaries_y2", "{p} served in Year 2"), ("beneficiaries_y3", "{p} served in Year 3"),
    ("cpb_y1", "cost per {s}, Year 1"), ("cpb_y2", "cost per {s}, Year 2"), ("cpb_y3", "cost per {s}, Year 3"),
    ("total_exp_y1", "total expenses, Year 1"), ("total_exp_y2", "Year 2"), ("total_exp_y3", "Year 3"),
    ("total_rev_y1", "total revenue, Year 1"), ("total_rev_y2", "Year 2"), ("total_rev_y3", "Year 3"),
    ("surplus_y1", "surplus, Year 1"), ("surplus_y2", "Year 2"), ("surplus_y3", "Year 3"),
    ("personnel_y1", "Year-1 personnel"), ("ops_y1", "operations"), ("tech_y1", "technology"), ("outreach_y1", "outreach"),
    ("indirect_y1", "indirect costs"), ("contingency_y1", "the budget less Year-1 expenses"), ("staff_count", "staff in your plan"),
    ("sroi", "the SROI ratio, as Python computes it (cite it even when the value is less than the cost)"),
    ("sroi_per_dollar", "social value for each dollar invested"),
    ("sroi_pv_benefits", "PV of the counted social value"), ("sroi_pv_cost", "PV of the cost over the P&L's years"),
    ("sroi_horizon_years", "the years over which outcomes are counted"),
    ("coi_y1", "cost of inaction, Year 1"), ("coi_y2", "Year 2"), ("coi_y3", "Year 3"), ("coi_3yr", "cost of inaction over the P&L's years"),
    ("coi_visits_y1", "avoidable ER visits, Year 1"), ("coi_visits_y2", "Year 2"), ("coi_visits_y3", "Year 3"),
    ("stress_beneficiaries_y1", _IC2_STRESS_LABEL), ("stress_cpb_y1", "cost per {s} in that stress case"),
)
_IC2_COMPUTED_WORDS = {   # BUILD IC-3 (D7): IC-2's words for the four that spelled a count — the switch off prints them
    "sroi": "the SROI ratio, as Python computes it (below 1 included)", "sroi_per_dollar": "social value per $1 invested",
    "sroi_pv_cost": "PV of the three years' cost", "coi_3yr": "cost of inaction over three years"}


def computed_ids():
    """COMPUTED_IDS as the requests print them: IC-3's words, or IC-2's with the switch off (BUILD IC-3, D7). BUILD IC-4 (D6): with the
    switch on, the cost of inaction comes from the outcomes — no ER-visit counts."""
    ids = COMPUTED_IDS if ic3_prompt() else tuple((i, _IC2_COMPUTED_WORDS.get(i, w)) for i, w in COMPUTED_IDS)
    return tuple(x for x in ids if not x[0].startswith("coi_visits_")) if ic4_prompt() else ids


_IC3_WORDS = {   # BUILD IC-3 (D7): (IC-2's words → IC-3's) in a request's fixed text, by call; applied with the switch on (ic3_words)
    "master": (("such as ext.wage_chw_median, when one does)", "such as ext.wage_chw_median, when a benchmark backs it)"),
               # review c #7: R4 restated "below one" three times from this line — said in words, the ledger's entry dropped
               ("shows the ratio it computes — below 1 included.", "shows the ratio it computes, even when the value is less than the cost."),
               ("the benchmark id that values one outcome (such as ext.er_visit_cost), when one does;",
                "the benchmark id that values an outcome (such as ext.er_visit_cost), when a benchmark backs it;")),
    "update": (("This is BLOCK 2 of a two-block competition.", "This is BLOCK 2 of the competition."),
               # a bound split across two lines is read as one expression across them: kept on one line, where the ledger holds it
               (f'"<2-4 plain sentences, at most\n   {UPDATE_ANSWER_CHARS:,} characters>"', f'"<2-4 plain sentences,\n   at most {UPDATE_ANSWER_CHARS:,} characters>"')),
}


_IC3_WORDS_D14 = ("shows the ratio it computes — below 1 included.",
                   "the benchmark id that values one outcome (such as ext.er_visit_cost), when one does;")   # BUILD IC-4 (D14): the old request's


# BUILD IC-4 (D14): the master's outcome request — IC-3's (the switch off, byte for byte: it stood in the prompt's f-string) and D14's
SROI_ASK_IC3 = ("- sroi_outcomes: list of {category, unit_value_ref, unit_value, rate, duration_years, source} for SROI computation — 4-5 monetizable\n"
                "  outcomes. unit_value_ref: the benchmark id that values one outcome (such as ext.er_visit_cost), when one does; otherwise unit_value\n"
                "  in whole dollars with its source. rate: outcomes per person served in a year, as a share (0.03 for 3%) — realistic, such as a 2-5%\n"
                "  avoidance rate for a costly event, not 50%. duration_years: how many years one outcome lasts (1 for a one-off avoided event).\n"
                "  Python counts each year's outcomes from that year's people served and shows the ratio it computes — below 1 included.")
MATERIAL_SENTENCE = ("The SROI Guide: \"A piece of information is material if missing it out of the SROI would misrepresent the "
                     "organisation's activities.\"")
OUTCOME_STATUS = ("valued", "material, not valued", "not material")     # BUILD IC-4 (D6): the materiality list's statuses, Python's words


def contract_outcomes(doc) -> list:
    """BUILD IC-4 (D6): the contract's core outcomes — [{id, words, quote}] (the brief's own outcome words, confirmed on the day); each id
    a plain string. [] when the contract confirms none."""
    out = []
    for o in (doc.get("outcomes") or []) if isinstance(doc, dict) else []:
        if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"].strip() and isinstance(o.get("words"), str) and o["words"].strip():
            out.append({"id": o["id"].strip(), "words": re.sub(r"\s+", " ", o["words"]).strip(),
                        "quote": re.sub(r"\s+", " ", str(o.get("quote") or "")).strip()})
    return out


def outcome_request_ic4(doc) -> str:
    """BUILD IC-4 (D14): the master's sroi_outcomes instruction with the switch on — the outcomes by materiality, in D14's steps; no example
    rate, no "monetizable", no count of outcomes; each outcome's fields named. The contract's core outcomes in their own block (the
    organizers' words, as the brief is)."""
    core = contract_outcomes(doc)
    lines = ["- sroi_outcomes: the program's outcomes, by materiality, in these steps:",
             "  1. Name the program's core outcomes: the contract's (in <core_outcomes>, each by its id), then what the brief's objectives name.",
             "  2. For each, say whether it is material. " + MATERIAL_SENTENCE,
             "  3. Value each material outcome with a proxy: a figure of the data (its id as unit_value_ref), a benchmark id from",
             "     <benchmark_reference_sheet> (unit_value_ref), or a value of your own (unit_value, in whole dollars) with its source — or",
             "     state it as material, not valued, and say why.",
             "  4. Give each a duration: the years an outcome lasts (duration_years).",
             "  Each item: {category, core, material, why, unit_value_ref, unit_value, rate, duration_years, source} — category: the outcome",
             "  in words; core: the id of the contract's core outcome it is, or \"\" for an outcome of the brief's objectives; material: true or",
             "  false; why: why it is not material, or why a material outcome is not valued (\"\" otherwise); rate: outcomes per person",
             "  served in a year, as a share written as a fraction; source: where the proxy's value comes from. Python lists every outcome",
             "  with its status (a core outcome you do not name is listed as material, not valued), computes the ratio from the valued ones",
             "  only, labels it partial when a material outcome is not valued, and computes the cost of inaction from the same list.",
             "<core_outcomes>"]
    lines += ([f"[{c['id']}] {c['words']}" + (f" (the brief: \"{c['quote']}\")" if c["quote"] and c["quote"] != c["words"] else "") for c in core]
              or ["The contract names no core outcomes: name them from the brief's objectives."])
    return "\n".join(lines + ["</core_outcomes>"])


def sroi_data_values(ma: dict, reg) -> dict:
    """BUILD IC-4 (D14): the outcomes valued by a figure of the data — {unit_value_ref: value} for each outcome whose unit_value_ref is a
    data id this run's data registry reads as a positive measured figure (a benchmark id is the financial stage's). {} otherwise."""
    out = {}
    if reg is None or not isinstance(ma, dict):
        return out
    bench = {b[0] for b in BENCHMARKS}
    for o in ma.get("sroi_outcomes") or []:
        ref = o.get("unit_value_ref") if isinstance(o, dict) else None
        ref = ref.strip().strip("[]").strip() if isinstance(ref, str) else ""
        if ref and ref not in bench:
            e = reg.get(ref)
            v = (e or {}).get("value")
            if e and e.get("kind") == "MEASURED" and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[ref] = v
    return out


def rate_of(v):
    """IC-4 Phase 7 (review b12): an outcome's rate as written — a number or a share (0.25, "25%"), or a fraction, as the request's words
    ("a share written as a fraction") invite: "1/4", "3/100", "1 in 4", "3 out of 100". None when it is none of them."""
    x = REG.parse_number(v)
    if x is not None:
        return x
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:/|in|out\s+of)\s*(\d+(?:\.\d+)?)\s*", v) if isinstance(v, str) else None
    return float(m.group(1)) / float(m.group(2)) if m and float(m.group(2)) > 0 else None


def material_of(o: dict):
    """BUILD IC-4 (D14): an outcome's `material` — True, False, or None when the reply does not say (a plain check of its value)."""
    m = o.get("material")
    if isinstance(m, bool):
        return m
    t = str(m).strip().lower() if isinstance(m, str) else ""
    return True if t in ("true", "yes", "material") else False if t in ("false", "no", "not material") else None


def ic3_words(text, call):
    """BUILD IC-3 (D7): `text` with _IC3_WORDS[call] applied — each IC-2 phrase must stand exactly once (t_requests_ic3 fails otherwise);
    one that does not is left as it is and named in the console. The switch off: `text` unchanged."""
    if not ic3_prompt():
        return text
    for old, new in _IC3_WORDS.get(call, ()):
        if call == "master" and old in _IC3_WORDS_D14 and ic4_prompt():
            continue          # BUILD IC-4 (D14): the outcome request these phrases stood in is D14's with the switch on
        if text.count(old) != 1:
            NR.say(f"    ⚠ IC-3 words ({call}): {old[:60]!r} stands {text.count(old)} times — left as it is")
            continue
        text = text.replace(old, new)
    return text


def _request_words():
    """BUILD IC-3 (D7; step 56): {id: {label|window|cohort: the request's words}} — the engine file's "request" keys (the fields', the
    named entries' and the text figures'): words that spell no count, which the switch-on request prints in place of the entry's own
    (the page keeps those, as IC-2 showed them). {} with no engine file."""
    cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    spec, _w = _load_spec(cpath) if cpath else (None, None)
    sec = (spec or {}).get("registry") if isinstance(spec, dict) else None
    if not isinstance(sec, dict):
        return {}
    out = {ref: dict(t["request"]) for ref, t in (sec.get("fields") or {}).items() if isinstance(t, dict) and isinstance(t.get("request"), dict)}
    for e in list(sec.get("entries") or []) + list(sec.get("text_figures") or []):
        if isinstance(e, dict) and isinstance(e.get("request"), dict) and isinstance(e.get("id"), str):
            out[e["id"]] = dict(e["request"])
    return out


def _words_of(e, rw):
    """The entry as a request prints it: with the switch off its own words (IC-2's); with the switch on the request's words where the
    engine file gives them (rw from _request_words: no count spelled) and a rate whole — its value with its per part (D7: "14 per 10,000
    residents")."""
    if not ic3_prompt():
        return e
    return dict(e, **rw.get(e["id"], {}), shown=REG.whole_rate(e) or e["shown"])


def build_registry(results: dict):
    """BUILD B3 (D1): this run's registry, built from the stage results — step 1 (after data_loading): the benchmarks (EXTERNAL), the
    brief's facts (MEASURED, with its words), the data by grammar, the sidecar's named entries and its checks; step 2 (after the
    financial model): the model's inputs, Python's constants and every computed figure. At Block 2 also Block 1's registry, as
    b1.<id>. Deterministic: each stage that needs it builds it again from the same results. Never raises on bad data (each problem
    is listed in reg.problems)."""
    dl = results.get("data_loading") or {}
    raw = dl.get("raw") or {}
    doc = _brief_contract()
    F = NR.facts(doc)
    chal = F.get("challenge") or os.environ.get("NAIPC_CHALLENGE", "").strip() or None
    reg = REG.Registry(chal, NR.run_id(), 2 if is_block2() else 1)
    for bid, v, fmt, unit, label, src, ver in BENCHMARKS:
        reg.add(bid, v, "EXTERNAL", fmt=fmt, unit=unit, label=label, source=src, verified=ver)
    budget, bsrc = budget_in_use(F)
    if bsrc == "brief":
        reg.add("budget", budget, "MEASURED", fmt="money_short", unit="USD", label=NR.BUDGET_SOURCES["brief"],
                source="the brief: \u201c" + str(F.get("contract_quote") or "") + "\u201d", quote=F.get("contract_quote"))
    elif bsrc == "override":
        reg.add("budget", budget, "MEASURED", fmt="money_short", unit="USD", label=NR.BUDGET_SOURCES["override"],
                source="the --budget override given at this run")
    if F["marketing"]:
        reg.add("reserve", F["marketing"], "MEASURED", fmt="money_short", unit="USD", label="the brief's marketing reserve",
                source="the brief: \u201c" + str(F.get("marketing_quote") or "") + "\u201d", quote=F.get("marketing_quote"))
    if F["pilot_days"]:
        reg.add("pilot_days", F["pilot_days"], "MEASURED", fmt="count", unit="days", label="the pilot's length",
                source="the brief: \u201c" + str(F.get("pilot_quote") or "") + "\u201d", quote=F.get("pilot_quote"))
    for c_ in constraint_figures(F):   # BUILD IC-3 (D7): the financial constraints' figures — the ids exist whatever the switch (as prio.)
        reg.add(**{k: v for k, v in c_.items() if k != "prompt_label"})      # BUILD IC-5 (D4c): a request's words are no entry's
    cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    spec, _w = _load_spec(cpath) if cpath else (None, None)
    sec = ENG.registry_section(spec, raw, block2=is_block2()) if spec else {"problems": [], "entries": [], "tags": {}, "checks": [], "notes": []}
    reg.set_data(raw, NR.records, sec.get("tags"))
    reg.set_facts((doc or {}).get("authority"), (doc or {}).get("aliases"))      # BUILD IC-4 (D3): the contract's metrics and renames
    reg.problems += sec.get("problems") or []
    reg.notes += sec.get("notes") or []
    for e in sec.get("entries") or []:
        reg.add(e["id"], e["value"], e["kind"], fmt=e.get("fmt") or ("count" if float(e["value"]).is_integer() else "num"),
                unit=e.get("unit"), label=e.get("label"), source=e.get("source"), formula=e.get("formula"), inputs=e.get("inputs"),
                assumption=e.get("assumption"), cls=e.get("class"), window=e.get("window"), cohort=e.get("cohort"), quote=e.get("quote"),
                fact=e.get("fact"))      # BUILD IC-4 (D3): the records a sum reads, or the record a text figure sits in
    reg.checks = list(sec.get("checks") or [])
    if spec:     # BUILD IC-3 (D7): each factor's points in the published table, an id the prompt prints and the writer cites (the
        #          resolver ignores the switch: the ids exist either way; the registry block's named figures leave them out — "prio.")
        for pid, pts, flab, slab in ENG.table_points(spec, doc, raw, os.environ.get("CURVEBALL_TEXT", ""),
                                                     block1_base(doc) if is_block2() else None):
            reg.add(pid, pts, "DERIVED", fmt="count", unit="points", label=f"{flab} — points in the published prioritization table ({slab})",
                    source="the published prioritization table")
    if spec:     # BUILD B3 (D5): each capacity set's totals — DERIVED from the records (the bottleneck dashboard shows them)
        for s in ENG.bottlenecks(spec, doc, raw, {}, os.environ.get("CURVEBALL_TEXT", "")).get("sets") or []:
            f, t_ = s["fields"], s["totals"]
            read = [x["id"] for x in s.get("rows") or [] if x.get("no_cap") is None]      # BUILD IC-4 (D3): the records the totals sum
            for key, what in (("listed", f[0]), ("in_use", f[1] if len(f) > 1 else None), ("free", None)):
                if key == "in_use" and what is None:
                    continue
                meas = what or (f"max({f[0]} \u2212 {f[1]}, 0)" if len(f) > 1 else f[0])
                reg.add(f"{s['id']}.{key}", t_[key], "DERIVED", fmt="count", label=f"{s['label']}: {key.replace('_', ' ')}", inputs=[s["endpoint"]],
                        fact={"entity": {"endpoint": s["endpoint"], "records": read}, "measure": meas},
                        formula=(f"Σ {what} over the {s['endpoint']} records" if what else f"Σ max({' − '.join(f)}, 0) over the {s['endpoint']} records"
                                 if len(f) > 1 else f"Σ {f[0]} over the {s['endpoint']} records"), source=s["endpoint"])
    eps = [(ep, len([r for r in NR.records(p) if isinstance(r, dict)])) for ep, p in raw.items()]
    if eps:
        reg.add("data.endpoint_count", sum(1 for _e, n in eps if n), "MEASURED", fmt="count", unit="endpoints",
                label="API endpoints with records this run", source="the data as fetched")
        reg.add("data.record_count", sum(n for _e, n in eps), "MEASURED", fmt="count", unit="records",
                label="records fetched this run", source="the data as fetched")
    for it in (results.get("financial_model") or {}).get("registry_items") or []:
        if isinstance(it, dict) and it.get("id"):
            if "label_model" not in it and str(it["id"]).startswith(("sroi.", "staff.")) and ": " in str(it.get("label") or ""):
                # IC-3 (ledger step 31): a financial model checkpoint written before IC-3 (the run turn's R4 rebuilds from one) names no
                # model words; its sroi./staff. labels are "<the model's words>: <Python's words>" (this stage's own templates), so
                # the model's words are the label up to its last ": " — Python's "value of one outcome" never counts as the model's
                it = dict(it, label_model=str(it["label"]).rsplit(": ", 1)[0])
            iid_ = str(it["id"])             # BUILD IC-4 (D7): Python's words for its own settings from the code that runs, not the checkpoint
            if iid_.startswith("const.") and iid_[6:] in FIN_CONSTANTS:
                it = dict(it, source=CONST_SOURCE, assumption=FIN_CONSTANTS[iid_[6:]][2])
            elif iid_ in ("cap.personnel_share", "coi_rate"):
                it = dict(it, source=CONST_SOURCE)
            e_ = reg.add(**{k: v for k, v in it.items() if k in ("id", "value", "kind", "fmt", "unit", "label", "source", "formula", "inputs",
                                                                  "assumption", "cls", "window", "cohort", "quote", "verified", "label_model")})
            if e_ is not None and isinstance(it.get("partial"), str) and it["partial"]:
                e_["partial"] = it["partial"]       # BUILD IC-4 (D6): a figure counted on the valued outcomes only says so where it prints
    # BUILD IC-2 (D2): the organizers' texts a quote may name besides the data — the brief's own words as the <brief> block prints them
    # (one function, NR.brief_source, for the prompt and the resolver) and the announcement as given
    reg.sources = {"brief": NR.brief_source(doc, "oracle") if doc else "", "announcement": announcement_source()}
    reg.add_mentions()          # BUILD IC-3 (D6): the list of mentions — ids from content, so every build of this run gets the same
    for e in list(reg.entries.values()):      # an entry's own words never carry a raw token (the model's source text may cite one)
        for f in ("label", "source", "assumption", "label_model"):      # IC-3 (ledger 24): the label's model words as the label is
            if isinstance(e.get(f), str) and ("[[" in e[f] or "]]" in e[f]):
                e[f] = REG.strip_tokens(reg.resolve(e[f], mode="plain")[0])   # BUILD IC-5 (D3, class T): a stray bracket goes with its id
    reg.unavailable = {str(s_.get("endpoint")) for s_ in (dl.get("summary") or []) if isinstance(s_, dict) and s_.get("error")}   # IC-4 (D2)
    if is_block2():
        b1, why = REG.read(CHECKPOINT_DIR / NR.BLOCK1_DIR / REG.REGISTRY_FILE, chal)
        reg.block1_note = None if b1 else why
        reg.block1_ids = set(b1.entries) if b1 else set()     # BUILD IC-4 (D2): a field Block 1 cited is a binding this run must keep
        b1dl = _cp_data(CHECKPOINT_DIR / NR.BLOCK1_DIR / "data_loading.json") if b1 else None
        reg.set_before(b1dl.get("raw") if isinstance(b1dl, dict) else None)    # BUILD IC-8 (d12c): a removed record keeps its name
        b1reg, b1parts = None, {}  # BUILD IC-4 (D3): a Block 1 registry with no facts (written before IC-4) — its facts by the same rules,
        if b1 and isinstance(b1dl, dict) and isinstance(b1dl.get("raw"), dict):      # on Block 1's own data, the sidecar's as at Block 1
            b1sec = ENG.registry_section(spec, b1dl["raw"], block2=False) if spec else {}
            b1parts = {x["id"]: x["fact"] for x in b1sec.get("entries") or [] if isinstance(x, dict) and isinstance(x.get("fact"), dict)}
            b1reg = REG.Registry(chal, b1.run_id, 1)
            b1reg.set_data(b1dl["raw"], NR.records, b1sec.get("tags"))
            b1reg.set_facts((doc or {}).get("authority"), (doc or {}).get("aliases"), {"value": b1.run_id, "from": "fetched in Block 1's run"},
                            {"value": b1.run_id, "from": "made in Block 1's run"})
        # BUILD IC-4 (D2): each Block 1 mention re-anchored by its claim in its place (unchanged ones keep their ids; the others are
        # changed, ambiguous, gone or source unavailable — flagged); Block 1's own data says what its places read then
        reg.add_stale_mentions(b1.entries if b1 else {}, b1dl.get("raw") if isinstance(b1dl, dict) else None)
        reg.b1_models = {k: {"label": e.get("label"), "shown": e.get("shown"), "value": e.get("value")}      # IC-4 Phase 7 (review a9, D1):
                         for k, e in (b1.entries if b1 else {}).items() if k.startswith("model.")}           # Block 1's assumptions, by id
        for k, e in sorted((b1.entries if b1 else {}).items()):
            if not k.startswith(("model.", "b1.", "ext.", "const.", "q.", "calc.", "count.") + CONSTRAINT_PREFIXES):     # IC-2 (D1): a quote's and a comparison's ids
                # (IC-4 Phase 7, review b5: a count is counted on each page — never one of Block 1's figures)
                # come from what they name, so Block 2 resolves Block 1's tokens to the same ids — never copied as b1.
                b1f = dict(e.get("fact") or (b1reg.fact(dict(e, fact=b1parts.get(k))) if b1reg else {}), scenario="Block 1")   # IC-4 (D3)
                if not b1f.get("as_of"):
                    b1f["as_of"] = {"value": b1.run_id, "from": "fetched in Block 1's run"}
                reg.add("b1." + k, e["value"], e["kind"], fmt=e.get("fmt"), unit=e.get("unit"), label="Block 1: " + str(e.get("label") or k), fact=b1f,
                        label_model=e.get("label_model"),         # IC-3 (ledger 24): None for a registry written before IC-3
                        source=f"Block 1's registry (run {b1.run_id})", formula=e.get("formula"), cls=e.get("class"),
                        window=e.get("window"), cohort=e.get("cohort"),     # B3 (Phase 4, review d F7): a checked figure stays checked
                        verified=({"verified": e.get("verified_against_primary"), "url": e.get("url"), "quote": e.get("quote"),
                                   "checked": e.get("checked")} if e.get("kind") == "EXTERNAL" else None))
    named = reg.name_ids()      # BUILD IC-5 Phase 4 (the replays, D4): Python's words name a figure by its label, never by its id
    said_ = frozenset(w for _e, _f, w in named)
    if named and said_ not in _NAMED_SAID:
        _NAMED_SAID.append(said_)
        NR.say(f"    registry: {len(named)} id(s) in Python's words named by their labels (a saved file's older words; this build's code "
               f"writes none): {', '.join(sorted(said_))[:300]}")
    REG.set_measure_names(reg.names())     # BUILD IC-8 (P4.4, p44v): the run's names from the data stage on — the measure reads the words
    #                                        the page pass prints (its page pass reads the registry's names(): the same data and Block 1's)
    return reg


_NAMED_SAID = []      # BUILD IC-5 Phase 4: each set of ids build_registry named, said once (a run builds its registry more than once)


def _tags_text(e):
    t = [x for x in (e.get("window"), e.get("cohort")) if x]
    return f" ({'; '.join(t)})" if t else ""


FORMS_TEXT_FIELDS_MAX = 30     # BUILD IC-2 (D2): the text fields listed to quote from


def registry_forms_lines(reg, raw, quotes=True) -> list:
    """BUILD IC-2 (D5): the <registry> block's lines for the writer's forms — a figure quoted from text (with the text fields of this run
    that hold numbers, at most FORMS_TEXT_FIELDS_MAX), a comparison Python computes and a declared non-figure — each example built from
    this run's data and registry (the first text field that holds a number; the first two named figures of one class), never named in
    the code."""
    if ic3_prompt():
        tf = REG.text_fields_with_numbers(raw, NR.records, REG.Exempter.from_data(raw, NR.records), limit=FORMS_TEXT_FIELDS_MAX)
    else:                   # BUILD IC-3 (D10): the switch off — IC-2's list byte for byte, by its own frozen selection
        tf = REG.text_fields_ic2(raw, NR.records, limit=FORMS_TEXT_FIELDS_MAX)
    q_ex = next((f"[[q:{t['endpoint']}.{t['rid']}.{t['field']}|{t['number']}]]" for t in tf if t.get("rid") and t.get("number")), None)
    named = [e for e in reg.entries.values() if e["kind"] in ("MEASURED", "DERIVED") and not e["id"].startswith(("b1.", "q.", "calc.", "prio.") + CONSTRAINT_PREFIXES)
             and any(REG._tagkey(e)) and e["fmt"] not in ("pct", "pct100") and e["value"]]
    pair = next(((a, b) for i, a in enumerate(named) for b in named[i + 1:]
                  if REG._tagkey(a) == REG._tagkey(b) and REG._dim(a) == REG._dim(b) and a["value"] != b["value"]), None)
    if pair and pair[0]["value"] < pair[1]["value"]:
        pair = (pair[1], pair[0])
    c_ex = f"[[calc:diff|{pair[0]['id']}|{pair[1]['id']}]]" if pair else None
    where = "<endpoint>.<record id>.<field>, <endpoint>.<field> (that field in any record), brief" + (
        " or announcement" if (getattr(reg, "sources", {}) or {}).get("announcement") else "")
    out = [] if not quotes else ["- A number the data or the brief states only in its text (a note, a description, the brief's own sentences): "
           "[[q:<where>|<the number exactly as that text writes it: digits, or a quantity in words such as \u201ca third\u201d>]]. <where> is "
           + where + ". Python checks that the text holds it and "
           "shows it with its sentence" + (f"; for example {q_ex}." if q_ex else ".") + " A quote the text does not hold stays as you typed it, "
           "counted as having no link."]
    if tf and quotes:
        out += ["  Text fields that hold numbers in this data:"] + [
            f"  {t['endpoint']}.{t['field']} ({t['records']} of {t['of']} record{'s' if t['of'] != 1 else ''})" for t in tf]
    out += ["- A comparison of two figures — never work one out yourself: [[calc:<op>|<id a>|<id b>]] (two ids from <registry>; never a "
            "number, a proposal or another token), with <op> one of diff (a − b), pct_of (a as a percent of b), pct_less / pct_more (how far "
            "a is below / above b, as a percent of b), times (a ÷ b), pp (the gap between two percentages: Python writes \u201cN points\u201d, so "
            "write no unit after it). diff and pp join only two figures of the same people, place and period; the others may compare two "
            "groups. Both figures are the same kind of quantity (two counts, two amounts of money, two percentages). Python computes it"
            + (f"; for example {c_ex}." if c_ex else ".") + " A comparison that is false as named, or of two figures that count different "
            "things, is removed with its sentence.",
            "- Not a figure, though it has digits. Write it plainly when it is a year, a date, a time, an age after the word \u201cage\u201d "
            "(\u201cat age 21\u201d), or one of the data's own names and ids. Anything else: [[nf:<the text>|<kind>]], the kind one of "
            "age (an age written another way), year, date, time, name (a route, a room, a ward), code (a law, a form or a model number), "
            "phone (a phone number, or a service line such as 211). A quantity is never declared this way: Python refuses one, and it "
            "counts as having no link."]
    return out


def _source_hedged(e) -> bool:
    """BUILD IC-3 (D3; review c #9): a named figure Python read from a record's words ("roughly 60 displaced", "80-120 daily contacts")
    whose expression there, as the one reader reads it, carries a qualifier or a bound, or is a range: as a named figure it would print
    the bare number; the request offers its mention instead, which prints the source's whole expression."""
    q = e.get("quote")
    if not isinstance(q, str) or not q.strip() or e.get("value") is None:
        return False
    v = float(e["value"])
    for x in REG.quantities(q):
        if x["role"] != "quantity":
            continue
        vals = [y for y in (x.get("value"), x.get("lo"), x.get("hi")) if isinstance(y, (int, float))]
        if any(abs(float(y) - v) < 1e-9 for y in vals):
            return bool(x.get("hedge") or x.get("op") or x.get("shape") == "range")
    return False


# BUILD IC-4 (D16): the final form's own two lines — the request's words (the model's input), named for the claims scan's skip
_TRACK_REG_WORDS = {"computed": "- The business case's figures, computed by Python from its analysis (cite them by id): ",
                    "benchmarks": "- Benchmarks (from outside sources): "}


def registry_prompt_block(reg, raw, F, quotes=True, final=False) -> str:
    """BUILD B3 (D2): the <registry> block of the master and update prompts — the id grammar, the named figures with their values, the
    one-record endpoints' fields, the figures Python computes afterwards (with what each is), and the data's own aggregates that do
    not match the records they summarise. BUILD IC-4 (D16): final — the block another track's requests get on the run's final registry
    (track_parts): the figures Python computed are listed with their values, a page's own counts are no figure of another page, and the
    benchmarks are listed (that track's system prompt holds no sheet)."""
    p, s = F["unit_plural"], F["unit_singular"]
    rw = _request_words() if ic3_prompt() else {}          # BUILD IC-3 (D7; step 56): the words each request prints (_words_of)
    computed_ = [i for i, _w in computed_ids()] + ["budget"] if final else []      # IC-4 (D16): listed on their own line, with values
    named = [e for e in reg.entries.values() if e["kind"] in ("MEASURED", "DERIVED")
             and not e["id"].startswith(("b1.", "q.", "calc.", "prio.") + CONSTRAINT_PREFIXES)   # IC-2: the writer's forms are no named figure; IC-3: nor the constraints' (D7)
             and not (ic3_prompt() and _source_hedged(e))   # IC-3 (review c #9): the source's qualifier or range — its mention prints it
             and not (final and (e["id"].startswith("count.") or e["id"] in computed_))]   # IC-4 (D16): another page's counts are its own
    counts, one = [], []
    for ep, payload in raw.items():
        recs = [r for r in NR.records(payload) if isinstance(r, dict)]
        if not recs:
            continue
        counts.append(f"[[{ep}.count]] = {len(recs)}")
        if len(recs) == 1:
            for fld, v in recs[0].items():
                e = reg.get(f"{ep}.{fld}") if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                if e:
                    e = _words_of(e, rw)
                    one.append(f"[[{e['id']}]] = {e['shown']}" + (f" — {e['label']}" if e["label"] != e["id"] else "") + _tags_text(e))
    lines = ["<registry>",
             "Every figure you write is a token. The ids:",
             "- The data, by grammar: [[<endpoint>.<field>]] (an endpoint with one record), [[<endpoint>.<record id>.<field>]], [[<endpoint>.count]]",
             "  (its records); a sum over every record: [[sum:<endpoint>.<field>]], or [[sum:<endpoint>.<a>-<b>]] (a − b per record, summed).",
             "- Record counts: " + ", ".join(counts)]
    keyed = [f"{ep} by {reg.record_key(ep)}" for ep in raw if reg.record_key(ep) and len([r for r in NR.records(raw[ep]) if isinstance(r, dict)]) > 1]
    if keyed:      # B3 (Phase 4, review a F3): the <record id> of each endpoint — its records' "id", or the field that names one (a zip)
        lines += ["- The <record id> of each endpoint (the field that names one record): " + "; ".join(keyed)]
    if one:
        lines += ["- One-record endpoints' fields:"] + ["  " + x for x in one]     # BUILD IC-8 (P4.2, I5): every field (IC-7: its first 80)
    if named:
        lines += ["- Named figures:"] + ["  " + f"[[{e['id']}]] = {e['shown']} — {e['label']}" + _tags_text(e)
                                         for e in (_words_of(x, rw) for x in named)]
        # BUILD IC-8 (P4.2, K1): every named figure — IC-7 listed the first 60 in the registry's insertion order, which is the order Python
        # first reads a figure (I10 restated); offered_ids reads the same list whole
    if final:       # BUILD IC-4 (D16): another track writes after the Oracle — these are computed, each with its value
        have_ = [i for i in computed_ if reg.get(i) is not None]
        if have_:
            lines += [_TRACK_REG_WORDS["computed"]
                      + "; ".join(f"[[{i}]] = {reg.shown(i)}" + (" (partial)" if isinstance(reg.get(i).get("partial"), str) and reg.get(i)["partial"]
                                                               else "") for i in have_)]
    else:
        lines += ["- Figures Python computes after you write (cite them; their values are computed from your inputs): "
                  + "; ".join(f"[[{i}]] {w.format(p=p, s=s)}" for i, w in computed_ids())
                  + (". The budget: [[budget]]" + (" (your contract_value)" if reg.get("budget") is None else "") + ".")]
    stale = [c for c in getattr(reg, "checks", []) if not c.get("equal")]
    if stale:
        lines += (["- The data's own aggregates that disagree with the records they summarise — cite the figure computed from the records;"
                   " where you name the aggregate too, say that the two sources disagree, never that either is not updated:"] if ic4_prompt() else
                  ["- The data's own aggregates that no longer match the records they summarise — cite the figure computed from the records and"
                   " name the aggregate as not updated:"])           # BUILD IC-4 (D11; open item 4): the switch off, IC-3's line
        lines += [f"  [[{c['entry']}]] = {reg.shown(c['entry'])} from the records; [[{c['against']}]] = {reg.shown(c['against'])}" for c in stale]
    b1_ = [e for e in reg.entries.values() if e["id"].startswith("b1.") and not e["id"].startswith("b1.prio.")
           and e["id"][3:] not in SROI_SENS_IDS]     # BUILD IC-4 (D6): the sensitivity pair is Python's page figures, offered in no request
    # IC-3 (D7): the
    #     table's points are the published table's own, offered in its block (switch on), never as Block 1's values
    if b1_:      # B3 (Phase 4, review b F7): Block 1's values exist only for the ids Block 1's registry held — listed, so none is guessed
        n_ = len(b1_) if ic3_prompt() else 80       # IC-3 (D7): every one with the switch on — IC-2 printed 80 and "and N more"
        lines += ["- Block 1's values (the only b1 ids): " + "; ".join(f"[[{e['id']}]] = {e['shown']}" for e in (_words_of(x, rw) for x in b1_[:n_]))
                  + (f"; and {len(b1_) - 80} more" if len(b1_) > n_ else "")]
    if ic3_prompt():        # BUILD IC-3 (D10): the forms are number_rules' (one function); the block lists ids only
        if final:           # BUILD IC-4 (D16): another track's system prompt holds no sheet — the benchmarks the registry holds, listed
            ext_ = [e for e in reg.entries.values() if e["kind"] == "EXTERNAL" and e["id"].startswith("ext.")]
            # the sheet's words (_IC3_BENCH_WORDS: a label that spells a count in words that spell none) — one set with the system prompt's
            lines += ([_TRACK_REG_WORDS["benchmarks"] + "; ".join(f"[[{e['id']}]] = {e['shown']} — {_IC3_BENCH_WORDS.get(e['id'], e['label'])}"
                                                                  for e in ext_)] if ext_ else []) + ["</registry>"]
        else:
            lines += ["- Benchmarks: the system prompt's sheet ([[ext.<id>]]).", "</registry>"]
        return "\n" + "\n".join(lines)
    if ic2_prompt():        # BUILD IC-2 (D5): the writer's forms, stated here and nowhere else; each example from this run's own data
        lines += registry_forms_lines(reg, raw, quotes=quotes)
    else:
        lines += ["- Not figures — write them as they are: an age (\"at age 21\"), a year, a date, a time, and the data's record ids and names."]
    lines += ["- Benchmarks: the system prompt's sheet ([[ext.<id>]]).",
              "- A number of your own that none of these holds: [[model:<number>|<what it is>]] — shown as your proposal."
              + (" A share of your own in a token is a percent — [[model:3%|…]], never [[model:0.03|…]] — even where a JSON field asks for 0.03."
                 if ic2_prompt() else ""),
              "A number written as digits is counted on the page as untraced. A sentence with a token naming nothing, or a difference of two",
              "figures that count different people, places or years (\"A − B = C\"), is removed from the page.",
              "</registry>"]
    return "\n" + "\n".join(lines)


MENTIONS_MAX = 150        # BUILD IC-3 (D6): the list shows up to this many; the rest are named, reachable written out


def changed_mentions_block(reg, base_ma) -> str:
    """BUILD IC-3 (D11): the update's <changed_mentions> — each mention Block 1's analysis cites that this run's list no longer holds:
    changed (the record and field still exist; their sentence now) or gone; the analysis's fields that cite it. "" when none."""
    cites, rv = {}, {}
    rmap = {}
    if ic4_prompt():     # IC-4 Phase 7 (review a4, D2): the review list — a figure unchanged in value whose sentence changed around it
        for r in getattr(reg, "review", None) or []:
            if isinstance(r, dict):
                rmap.update({x: r for x in (r.get("id"), r.get("b1_id")) if x})
    for path, text in _text_fields_of(base_ma):
        for k_ in reg.cited_mentions(text):           # IC-3 (review b #9c): either form, any letter case — the registry's one reading
            if k_ in reg.stale:
                cites.setdefault(k_, []).append(path)
            elif k_ in rmap:
                rv.setdefault(k_, []).append(path)
    if not cites and not rv:
        return ""
    lines = ["<changed_mentions>"] + (["Block 1's analysis cites quantities of the organizers' text that changed at the curveball. "
             + ("Replace each item that cites one" if ic4_prompt() else "Amend each field that cites one")      # BUILD IC-4 (D11)
             + ": cite this run's id from <mentions>, or say what is not known. A citation left as it is shows Block 1's words, flagged."]
                                      if cites else [])
    for k_, paths in cites.items():
        d = reg.stale[k_]
        lines.append(f"[[{k_}]] \u201c{d['words']}\u201d — {d['label']}: "
                     + (f"changed; it now reads \u201c{d['now']}\u201d" if d["status"] == "changed" and d.get("now") else
                        "changed" if d["status"] == "changed" else
                        f"ambiguous — {d.get('why') or 'the figure cannot be told apart'}" + (f"; the field now reads \u201c{d['now']}\u201d" if d.get("now") else "")
                        if d["status"] == "ambiguous" else
                        "its source did not load this run (no change in the data is known)" if d["status"] == "source unavailable" else
                        "gone (the record is no longer in the data)")
                     + " — cited in " + ", ".join(p_ + (" (rule 4: not amended here — it keeps Block 1's words, flagged)"
                                                        if re.split(r"[.\[]", p_)[0] in FUNDS_FIELDS + IDENTITY_FIELDS else "")
                                                   for p_ in dict.fromkeys(paths)))
    if rv:
        lines.append("The text around a figure you cite changed; the figure itself did not. Review each item that cites one: its "
                     "sentence may no longer say what the figure is about.")
        for k_, paths in rv.items():
            r = rmap[k_]
            lines.append(f"[[{k_}]] \u201c{r.get('words') or ''}\u201d — Block 1's text: \u201c{NR.cut(r.get('before') or '', 300)}\u201d; this "
                         f"run's: \u201c{NR.cut(r.get('after') or '', 300)}\u201d — cited in " + ", ".join(dict.fromkeys(paths)))
    return "\n".join(lines + ["</changed_mentions>"])


def _text_fields_of(o, path=""):
    """(path, text) of every string in an analysis (a dict, a list, nested), its path as the update names fields ("evidence_cards[2].body")."""
    if isinstance(o, str):
        yield path, o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from _text_fields_of(v, f"{path}.{k}" if path else str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _text_fields_of(v, f"{path}[{i}]")


def mentions_block(reg, only=None) -> str:
    """BUILD IC-3 (D6): the <mentions> block — each sentence once, with the ids of the quantities it states and what each prints; up to
    MENTIONS_MAX, the rest counted and pointed to the written-out form (never cut silently). `only`: the ids to list (the deliverables
    call: the mentions the analysis cited). "" when there is none. Its sentences are the organizers' (an evidence zone, D9)."""
    ms = [m for m in (getattr(reg, "mentions", None) or []) if only is None or m["id"] in only]
    if not ms:
        return ""
    shown, rest = ms[:MENTIONS_MAX], ms[MENTIONS_MAX:]
    groups = {}
    for m in shown:
        groups.setdefault((m["label"], m["sentence"]), []).append(m)
    lines = ["<mentions>", "The quantities the records' text, the brief and the announcement state, each sentence once. Cite one by its id: "
             "Python prints it as the source writes it, hedge and all."]
    for (lab, sent), g in groups.items():
        lines += [f"{lab}: \u201c{sent}\u201d", "  " + " · ".join(f"[[{m['id']}]] prints \u201c{m['words']}\u201d" for m in g)]
    if rest:
        lines.append(f"{len(rest)} more are not listed: quote one written out, as <number_rules> says.")   # D10: the form once
    return "\n".join(lines + ["</mentions>"])


# BUILD IC-7 (D4): the rules' lines live in naipc_registry.py (RULE_LINES, each with its precondition) — every harness has it, and each
# Muse call takes its own lines from them; the Oracle's requests take every line as written, so this text is IC-6's byte for byte
_RULES_TEXT = REG.number_rules_text(None, ic4=False)
# BUILD IC-4 (D13), with the switch on: the list line says what to write instead of what not to, and the own-number line names a count in
# the program's design; every other line IC-3's, byte for byte (the switch off sends _RULES_TEXT itself)
_RULES_IC3_LIST = ('- How many items your own list holds (the KPIs, the risks, the phases): no number — name the list ("the KPIs below", '
                   '"each risk").')
_RULES_IC4_LIST = ("- Where you say how many items a list holds, write [[count:<list>]], <list> being the list's field name in a reply's schema\n"
                   "  (kpis, risks, phases, judge_qa, …): Python prints how many items the page shows.")
_RULES_IC3_OWN = "a staffing level: [[model:<number>|<what it is>]], a number and never a\n  range (each end"
_RULES_IC4_OWN = ("a staffing level, a count in the program's own design (one named contact\n  per landlord): [[model:<number>|<what it is>]], "
                  "a number and never a range (each end")
assert _RULES_TEXT.count(_RULES_IC3_LIST) == 1 and _RULES_TEXT.count(_RULES_IC3_OWN) == 1, "IC-3's rules text changed: D13's lines no longer match"
_RULES_TEXT_IC4 = _RULES_TEXT.replace(_RULES_IC3_LIST, _RULES_IC4_LIST).replace(_RULES_IC3_OWN, _RULES_IC4_OWN)
COUNT_EXAMPLE = "How many items a list holds: [[count:{}]] — Python prints how many items the page shows."


def count_example(only=None):
    """BUILD IC-4 (D13): the one count example, its list a list field of the request's own reply — the deliverables' when the request lists
    the deliverables' mentions (`only`), else the analysis's — the middle one of that schema's lists (varied, as D10's examples)."""
    fields = REG.DELIVERABLE_ITEM_FIELDS if only is not None else tuple(f for f in MasterAnalysis.model_fields if f in REG.COUNT_LISTS)
    return COUNT_EXAMPLE.format(fields[len(fields) // 2]) if fields else None


def number_rules(reg, only=None, lists=None, out=None) -> str:
    """BUILD IC-3 (D10): the one set of rules — every form the resolver accepts (a registry id; a mention's id; a quote written out,
    q:; a comparison, calc:; a proposal, model:; a declared non-figure, nf:) stated once, in every request that asks for text (the
    master, the update, the deliverables), with <examples> built from this run's registry and list of mentions, each resolved on this
    run's registry before it is sent (one that does not resolve is left out and named in the console). No example names a challenge,
    an endpoint or a field in the code: each takes its ids from the run. BUILD IC-4 (D16): lists — another track's reply's lists, whose
    count example names one of them (None: this module's own, count_example)."""
    ex = []
    ex.append("A duration of your own, such as the target time to a first placement: [[model:3|weeks from intake to a first placement]] weeks.")
    # IC-3 (review b #10): from the mentions this request lists (`only`: the deliverables call's); one the source writes with a qualifier
    # first ("about", "up to"), else a range or ratio as the text writes it, else a plain one — so every request has its mention example
    ms_ = [m for m in (getattr(reg, "mentions", None) or []) if only is None or m["id"] in only]
    hedged = [m for m in ms_ if (reg.get(m["id"]) or {}).get("hedge")]
    shaped = [m for m in ms_ if m not in hedged and (reg.get(m["id"]) or {}).get("shape") not in (None, "single")]
    pick = hedged or shaped or ms_
    if pick:
        m = pick[len(pick) // 2]                      # not the first: varied (D10)
        ex.append(f"A figure the text states" + (" with its own qualifier" if hedged else " as a range or a ratio" if shaped else "")
                  + f": [[{m['id']}]] — cite the id; Python prints the source's words" + (", qualifier included." if hedged else "."))
    named = [e for e in reg.entries.values() if e["kind"] in ("MEASURED", "DERIVED") and e.get("value") and not e.get("quoted")
             and not e["id"].startswith(("b1.", "q.", "calc.", "prio.") + CONSTRAINT_PREFIXES) and any(REG._tagkey(e))]
    stem = lambda i_: re.sub(r"[-_.]?(?:low|high|min|max|lo|hi)$", "", i_)       # noqa: E731 — IC-3 (review b #10c): the two ends of one
    pairs = [(a, b) for i, a in enumerate(named) for b in named[i + 1:]       # range are no comparison to copy; the residual rule's own
             if REG._tagkey(a) == REG._tagkey(b) and REG._dim(a) == REG._dim(b) and a["fmt"] == b["fmt"] and a["value"] != b["value"]   # test:
             and stem(a["id"]) != stem(b["id"])]                                                            # the same class, window, cohort
    if pairs:
        a, b = pairs[len(pairs) // 2]                 # not the first pair (D10: on R4 the one diff shown was copied three times)
        if a["fmt"] in ("pct", "pct100"):
            op = "pp"
            a, b = (a, b) if a["value"] > b["value"] else (b, a)
        else:                                          # not always diff: the smaller as a share of the larger
            op = "pct_of"
            a, b = (a, b) if a["value"] < b["value"] else (b, a)
        ex.append(f"A comparison Python computes: [[calc:{op}|{a['id']}|{b['id']}]].")
    ex.append("A code with digits that counts nothing: [[nf:Form W-9|code]].")
    cx = ((count_example(only) if lists is None else COUNT_EXAMPLE.format(lists[len(lists) // 2]) if lists else None)
          if ic4_prompt() else None)       # BUILD IC-4 (D13): a count resolves only when the page is built — checked by name
    kept = []
    for t in ex:
        out_, evs = reg.resolve(t, mode="plain")
        bad = [e for e in evs if e.get("rule") not in ("cited", "declared", "note")]
        if REG.SENTINEL in out_ or bad:
            NR.say(f"    ⚠ number_rules: an example did not resolve on this run's registry — left out ({t[:60]!r})")
            continue
        kept.append(t)
    lists_ = REG.COUNT_LISTS if lists is None else tuple(lists)       # BUILD IC-4 (D16): another track's example names its own list
    if cx and lists_ and cx.split("[[count:", 1)[1].split("]]", 1)[0] in lists_:
        kept.append(cx)
    if out is not None:          # BUILD IC-7 (D4): the examples, for another track's own rule lines (track_parts' "examples")
        out.extend(kept)
    return (_RULES_TEXT_IC4 if ic4_prompt() else _RULES_TEXT) + ("\n<examples>\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(kept, 1))
                                                                + "\n</examples>" if kept else "")


def after_data_ic3(reg, raw, F, only=None, quotes=True) -> str:
    """BUILD IC-3 (D10, "Where it sits"): the <registry> block, <mentions>, the rules and the examples, after the data and before the
    instruction that asks for the output. "" with the switch off."""
    if not ic3_prompt():
        return ""
    return "\n".join(x for x in (registry_prompt_block(reg, raw, F, quotes=quotes).strip("\n"), mentions_block(reg, only), number_rules(reg, only))
                     if x) + "\n"


def track_parts(reg, raw, F) -> dict:
    """BUILD IC-4 (D16): what another track's requests get with the switch on — this module's three parts on the run's final registry:
    the registry block in its final form, the mentions, and the one set of rules (number_rules, its count example a list of that track's
    reply: REG.MUSE_COUNT_LISTS). Built in one place; written beside registry.json (REG.TRACK_PARTS_FILE) and read there by the Muse
    as it reads the registry (REG.read_track_parts)."""
    ex = []                       # BUILD IC-7 (D4): the examples as well, so each Muse call builds its own rule lines around them
    rules = number_rules(reg, lists=REG.MUSE_COUNT_LISTS, out=ex)
    return {"schema": 1, "challenge": reg.challenge, "run_id": reg.run_id, "block": reg.block,
            "registry": registry_prompt_block(reg, raw or {}, F, final=True).strip("\n"), "mentions": mentions_block(reg),
            "rules": rules, "examples": ex}


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# BUILD IC-4 (D11) — THE UPDATE AS KEYED EDITS (the switch on): every item of Block 1's analysis and deliverables, and the update's own
# slots, printed once with its id, kind, value and base; the model writes one JSON line per item; Python applies each line that checks
# (NR.apply_edits) and writes the record the whole-field merge writes, with each item's status. A cut reply keeps its complete lines; a
# refusal applies nothing. Decided before the edit (ledger 81-82).
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# BUILD IC-6 (D1): the edit update's limit and effort are its profile's, "oracle/update" (128,000 at high, as IC-4's Q-EFFORT set them)
UPDATE_FIRST_TEXT = (222.0, "IC-3's R8 update at effort high, its first text")   # D9: the stated first text; IC-6 (D2): + 30 s, its first
#                                                                                   line, used only when neither its profile nor this machine has one
EDIT_TAG = "<review_set>"           # the edit request's own block: the mock Messages API recognises the edit update by it
EDIT_INLINE_CHARS = 400             # a value longer than this (as JSON) is printed as a pointer into its Block 1 block
EDIT_LIST_FIELDS = {f: REG.ITEM_KINDS[f] for f in REG.ANALYSIS_ITEM_FIELDS}          # risks → risk … sroi_outcomes → outcome, staff_roles → role
EDIT_DLV_FIELDS = {f: REG.ITEM_KINDS[f] for f in REG.DELIVERABLE_ITEM_FIELDS}        # kpis → kpi … judge_qa → qa
EDIT_KIND_FIELD = {k: f for f, k in list(EDIT_LIST_FIELDS.items()) + list(EDIT_DLV_FIELDS.items())}
EDIT_TEXT_FIELDS = tuple(f for f in NARRATIVE_FIELDS if f not in EDIT_LIST_FIELDS and f not in ("assumptions", "mermaid_code"))
EDIT_INPUT_FIELDS = tuple(f for f in ASSUMPTION_FIELDS if f not in EDIT_LIST_FIELDS)          # the scalar inputs; outcomes and roles are items
EDIT_ADD_KINDS = tuple(EDIT_LIST_FIELDS.values()) + tuple(EDIT_DLV_FIELDS.values()) + ("impact", "derived")
EDIT_PARENTS = {"ew": ("risk",), "trigger": ("kpi",)}
EDIT_PARENT_REQUIRED = ("ew",)
EDIT_STATUS_WORDS = {"kept": "reviewed at the curveball and kept", "replaced": "amended at the curveball", "removed": "removed at the curveball",
                     "added": "added at the curveball", "not applied": "its edit at the curveball was not applied",
                     "not reviewed": "not reviewed at the curveball"}


def ic4_prompt() -> bool:
    """BUILD IC-4 (Q-SWITCH; D11-D16): the switch — this build's requests (the update as keyed edits, …) unless NAIPC_IC4_PROMPT is 0, off,
    false or no (any case), which sends IC-3's request bodies byte for byte (and keeps the whole-field merge); with NAIPC_IC3_PROMPT off,
    IC-2's (this build's wording builds on IC-3's). Read once, when the module loads. BUILD IC-4 (D12, D16): the shared module's switch
    (NR.ic4_prompt: the same two variables and words), which the Architect and the Muse read too."""
    return NR.ic4_prompt()


def _texts(v):
    """Every text value inside a value (a string, or the strings of a list or an object, at any depth)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _texts(x)
    elif isinstance(v, list):
        for x in v:
            yield from _texts(x)


def block1_deliverables():
    """BUILD IC-4 (D11): Block 1's deliverables as Block 1's run kept them (checkpoints/block1/deliverables.json): {"reply", "ids",
    "risk_refs", …} or None. Never raises."""
    b1 = _cp_data(CHECKPOINT_DIR / NR.BLOCK1_DIR / "deliverables.json")
    return b1 if isinstance(b1, dict) and isinstance(b1.get("reply"), dict) else None


def update_review_set(base_ma, dlv1=None, reg=None, cv=None, doc=None):
    """BUILD IC-4 (D11): every item of Block 1's analysis and deliverables and the update's own slots, each {"kind", "value", "base",
    "where"}, in Python's order — (1) the items whose text cites a mention the curveball changed, removed or could not load, or one on the
    registry's review list; (2) the response: update.summary, then test.<n> per test the announcement states; (3) the strategy: the BLUF,
    the decision, the thesis, each risk followed by its early warnings; (4) the rest: the other text fields, the phases, cards and rows, the
    list of assumptions, the inputs, the outcomes and roles, each KPI followed by the triggers that read it, the warnings no risk holds and
    the triggers no KPI holds, the failure modes, the judges' questions, the renewal gate, the headlines, the brief's answers, the diagram
    last. A whole field's id is f.<field> (the deliverables' d.renewal, d.headlines); a list item's is D1's. → (items, order)."""
    ma = base_ma if isinstance(base_ma, dict) else {}
    ids_a = REG.analysis_item_ids(ma)["ids"]
    reply = (dlv1 or {}).get("reply") if isinstance((dlv1 or {}).get("reply"), dict) else {}
    rf = deliverable_refs(dlv1, ma) if reply else {"ids": REG.reply_item_ids({}), "risk_refs": []}
    ids_d, rrefs = rf["ids"], list(rf.get("risk_refs") or [])
    items, seq = {}, []

    def put(i, kind, value, where):
        if i in items:
            return
        items[i] = {"kind": kind, "value": value, "base": NR.edit_base(value), "where": where}
        seq.append(i)

    def lst(src, f):
        return src.get(f) if isinstance(src.get(f), list) else []
    put("update.summary", "summary", "", ("update", "summary"))
    tests = [t.strip() for t in ((cv or {}).get("tests") or []) if isinstance(t, str) and t.strip()][:UPDATE_TESTS_MAX]
    for k, w in enumerate(tests, 1):
        put(f"test.{k}", "test", "", ("test", k, w))
    for f in ("bluf_text", "decision_text"):
        put(f"f.{f}", "text", ma.get(f) if isinstance(ma.get(f), str) else "", ("ma", f, None))
    if isinstance(reply.get("thesis"), dict):
        put("thesis.1", "thesis", reply["thesis"], ("dlv", "thesis", None))
    ews = lst(reply, "early_warnings")
    for j, r in enumerate(lst(ma, "risks")):
        rid = ids_a["risks"][j]
        put(rid, "risk", r, ("ma", "risks", j))
        for k, w in enumerate(ews):
            if k < len(rrefs) and rrefs[k] == rid and k < len(ids_d.get("early_warnings") or []):
                put(ids_d["early_warnings"][k], "ew", w, ("dlv", "early_warnings", k))
    for f in EDIT_TEXT_FIELDS:
        put(f"f.{f}", "text", ma.get(f) if isinstance(ma.get(f), str) else "", ("ma", f, None))
    for f in ("phases", "evidence_cards", "comparison_rows"):
        for j, x in enumerate(lst(ma, f)):
            put(ids_a[f][j], EDIT_LIST_FIELDS[f], x, ("ma", f, j))
    put("f.assumptions", "text list", lst(ma, "assumptions"), ("ma", "assumptions", None))
    for f in EDIT_INPUT_FIELDS:
        put(f"f.{f}", "input", ma.get(f), ("ma", f, None))
    for f in ("sroi_outcomes", "staff_roles"):
        for j, x in enumerate(lst(ma, f)):
            put(ids_a[f][j], EDIT_LIST_FIELDS[f], x, ("ma", f, j))
    trig = lst(reply, "triggers")
    for j, kp in enumerate(lst(reply, "kpis")):
        if j < len(ids_d.get("kpis") or []):
            put(ids_d["kpis"][j], "kpi", kp, ("dlv", "kpis", j))
            ind = str((kp or {}).get("indicator") or "").strip().lower() if isinstance(kp, dict) else ""
            for k, tg in enumerate(trig):
                if ind and isinstance(tg, dict) and str(tg.get("indicator") or "").strip().lower() == ind and k < len(ids_d.get("triggers") or []):
                    put(ids_d["triggers"][k], "trigger", tg, ("dlv", "triggers", k))
    for f in ("early_warnings", "triggers", "failure_modes", "judge_qa"):
        for j, x in enumerate(lst(reply, f)):
            if j < len(ids_d.get(f) or []):
                put(ids_d[f][j], EDIT_DLV_FIELDS[f], x, ("dlv", f, j))
    for key in ("renewal", "headlines"):
        if isinstance(reply.get(key), dict):
            put(f"d.{key}", key, reply[key], ("dlv", key, None))
    for e in (ma.get("brief_answers") or []):
        if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("answer"), str):
            put(f"brief.{e['id']}", "brief", e["answer"], ("brief", e["id"]))
    if has_diagram_data(ma):        # BUILD IC-5 (D15): the diagram as data — edited as data
        put("f.diagram", "diagram", ma["diagram"], ("ma", "diagram", None))
    else:
        put("f.mermaid_code", "diagram", ma.get("mermaid_code") if isinstance(ma.get("mermaid_code"), str) else "", ("ma", "mermaid_code", None))
    first = []
    if reg is not None:
        # IC-4 Phase 7 (review a5, D11): a mention the curveball changed, or a data figure that moved since Block 1 (Block 1's copy b1.<id>
        # holds another value), is a changed fact. BUILD IC-6 (p3sites): read by the registry module's one rule (REG.changed_marks once,
        # REG.cites_changed per item), which the Muse's review set calls too — this function's own copy and its token grammar are gone
        marks = REG.changed_marks(reg)
        first = [i for i in seq if REG.cites_changed(reg, list(_texts(items[i]["value"])), marks)]
        # a dependent pulled first comes after its parent (a warning after its risk, a trigger after its KPI)
        par_of = {}
        for j, w in enumerate(ews):
            if j < len(rrefs) and j < len(ids_d.get("early_warnings") or []) and rrefs[j]:
                par_of[ids_d["early_warnings"][j]] = rrefs[j]
        kpi_by_ind = {str((kp or {}).get("indicator") or "").strip().lower(): ids_d["kpis"][j] for j, kp in enumerate(lst(reply, "kpis"))
                      if isinstance(kp, dict) and j < len(ids_d.get("kpis") or [])}
        for k, tg in enumerate(trig):
            ind = str((tg or {}).get("indicator") or "").strip().lower() if isinstance(tg, dict) else ""
            if ind in kpi_by_ind and k < len(ids_d.get("triggers") or []):
                par_of[ids_d["triggers"][k]] = kpi_by_ind[ind]
        ordered = []
        for i in first:
            p_ = par_of.get(i)
            if p_ in items and p_ not in ordered:
                ordered.append(p_)
            if i not in ordered:
                ordered.append(i)
        first = ordered
    order = first + [i for i in seq if i not in first]
    return items, order


def _edit_value_text(v):
    """The value as the review set prints it: compact JSON."""
    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


def review_set_block(items, order) -> str:
    """BUILD IC-4 (D11): the request's <review_set> — one JSON line per item, in Python's order: {"id", "kind", "base", "value"}; a value
    longer than EDIT_INLINE_CHARS is printed as "see" a pointer into its Block 1 block (the analysis's field and item, the deliverables'),
    and a test's line carries the test's words."""
    out = [EDIT_TAG]
    for i in order:
        it = items[i]
        row = {"id": i, "kind": it["kind"], "base": it["base"]}
        w = it["where"]
        if w[0] == "test":
            row["test"] = w[2]
        txt = _edit_value_text(it["value"])
        if len(txt) <= EDIT_INLINE_CHARS:
            row["value"] = it["value"]
        else:
            row["see"] = ("<block1_analysis> " if w[0] == "ma" else "<block1_deliverables> " if w[0] == "dlv" else "<block1_brief_answers> ") + \
                (f"{w[1]}[{w[2]}]" if w[0] in ("ma", "dlv") and w[2] is not None else str(w[1]))
        out.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(out + ["</review_set>"])


def edit_validator(base_ma, dl_reply, items, refs, reserve, notes):
    """BUILD IC-4 (D11): validate(kind, value, op, id) for NR.apply_edits — the whole-field merge's own checks, one item at a time: a text
    field through the analysis's schema (escaped first, as an amendment); an item of a list through its field's schema with that item in
    Block 1's list; an input through the schema (the brief's reserve sets Year 1 outreach); an outcome's or a role's keys and number types
    those of Block 1's items (list_items_check); a deliverable as an object (its own checks run at render, as Block 1's); the response, a
    test's answer and a brief answer as plain text within their caps; an impact's ref a change of the list. A key an item's schema does not
    declare is dropped from what is stored and named in `notes` ({id: [keys]}). → (stored, None) or (None, (why, detail))."""
    check = _field_check(base_ma)
    ma = base_ma if isinstance(base_ma, dict) else {}

    def text_ok(v, cap, what):
        if not isinstance(v, str) or not NR.clean_text(v):
            return None, (f"{what} is not text", f"a {type(v).__name__}")
        return (REG.fit_cap(NR.clean_text(v), "oracle.update.text", cap) if cap else NR.clean_text(v)), None   # cap None: stored whole

    def v(kind, value, op, i):
        try:
            if kind == "diagram" and items[i]["where"][1] == "diagram":     # BUILD IC-5 (D15): Block 1's checks, stored as it is (G7)
                dd_, pr_ = diagram_data(value, f"the diagram ({i})")
                for w_, why_ in pr_:
                    NR.say(f"    curveball update: {i} — left out {w_} — {why_}")
                return (dd_, None) if dd_ else (None, ("a diagram is an object of nodes and edges with a node to draw", ""))
            if kind in ("text", "diagram", "text list"):
                f = items[i]["where"][1]
                if kind == "text list" and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                    return None, ("a list of plain sentences is its form", f"a {type(value).__name__}")
                if kind != "text list" and not isinstance(value, str):
                    return None, ("its value is not text", f"a {type(value).__name__}")
                return check.value(f, escape_amended(f, value))
            if kind == "input":
                f = items[i]["where"][1]
                if f == "outreach_cost_y1" and reserve:
                    return None, ("the brief's marketing reserve sets Year 1 outreach", "")
                return check.value(f, value)
            if kind in ("risk", "phase", "card", "row", "outcome", "role"):
                f = EDIT_KIND_FIELD[kind]
                if not isinstance(value, dict):
                    return None, (f"a {kind} is an object of its schema's keys", f"a {type(value).__name__}")
                xk = []
                if kind in ("outcome", "role"):      # a list input's item: Block 1's items' keys and number types (rule 7; list_items_check)
                    ref = [x for x in (ma.get(f) or []) if isinstance(x, dict)]
                    bad_k, bad_d, xk = list_items_check([value], ref)
                    if bad_k:
                        return None, ("it does not keep the keys and number types of Block 1's items", bad_d)
                    value = {k: x for k, x in value.items() if str(k) not in xk}     # N3: a key no Block 1 item has is not used
                    esc = NR.escape_markup(value)
                else:
                    esc = escape_amended(f, [value])[0]
                cur = copy.deepcopy(ma.get(f) if isinstance(ma.get(f), list) else [])
                at = items[i]["where"][2] if op == "replace" else len(cur)
                if op == "replace":
                    cur[at] = esc
                else:
                    cur.append(esc)
                stored, bad = check.value(f, cur)          # the item in Block 1's list, through the field's schema
                if bad:
                    return None, bad
                xk = xk + [k for k in NR.extra_keys(esc, stored[at]) if k not in xk]
                if xk:
                    notes.setdefault(i, []).extend(xk)
                return stored[at], None
            if kind in ("kpi", "fm", "ew", "trigger", "qa", "thesis", "renewal", "headlines"):
                if not isinstance(value, dict):
                    return None, ("its value is an object, as Block 1's", f"a {type(value).__name__}")
                return value, None      # BUILD IC-5 (D1 c): admitted with every edit line where the reply is decoded (run_update_edits)
            if kind == "summary":
                return text_ok(value, UPDATE_SUMMARY_CHARS, "the response summary")
            if kind == "test":
                return text_ok(value, UPDATE_ANSWER_CHARS, f"the answer of {i}")
            if kind == "brief":            # BUILD IC-8 (D1): an analysis field — stored whole; the page fits it (ORACLE_FIELD_FITS)
                return text_ok(value, None, f"the brief answer {i}")
            if kind == "impact":
                if not isinstance(value, dict):
                    return None, ("an impact is {\"ref\", \"impact\"}", f"a {type(value).__name__}")
                r_, txt = value.get("ref"), NR.clean_text(value.get("impact"))
                if not (isinstance(r_, str) and r_.strip().lower() in refs):
                    return None, (f"its ref is not a change in the list ({NR.name_cut([r_])})", "")
                if not txt:
                    return None, ("no impact given", "")
                return {"ref": refs[r_.strip().lower()], "impact": REG.fit_cap(txt, "oracle.update.impact", UPDATE_IMPACT_CHARS)}, None
            if kind == "derived":
                if not isinstance(value, dict):
                    return None, ("a derived figure is an object of rule 6's keys", f"a {type(value).__name__}")
                form = {k: value[k] for k in ENTRY_KEYS["derived_figures"] if k in value}
                if isinstance(value.get("compare_with"), dict):
                    form["compare_with"] = {k: value["compare_with"][k] for k in ENTRY_KEYS["compare_with"] if k in value["compare_with"]}
                return form, None
            return None, ("no check for this kind", kind)
        except Exception as e:  # noqa: BLE001 — a value Python cannot check is not applied
            return None, ("the value could not be checked", type(e).__name__)
    return v


EDIT_INPUT_KINDS = ("input", "outcome", "role")        # the financial model's inputs: applied as one group (rule 7)
EDIT_REMOVE_KINDS = tuple(EDIT_LIST_FIELDS.values()) + tuple(EDIT_DLV_FIELDS.values())   # a list's items; a field is replaced or kept
UPDATE_INPUTS_GROUP_WHY = "the financial model cannot be computed with the edited inputs — none of them is applied"
_CHANGES_SAME_IC4 = NR.CHANGES_SAME_IC4      # BUILD IC-4 (D16): the words in the shared module — the Muse's change list takes them too


def changes_block(c) -> str:
    """BUILD IC-4 (D11; open item 4): the change list as a request prints it — NR.changes_prompt_block; with the switch on, the unchanged
    endpoints' line says that their data is Block 1's and no more (IC-3's line went on "— an aggregate among them did not update", and the
    model then called an aggregate not updated). The switch off: IC-3's block byte for byte."""
    return NR.changes_prompt_block(c, same_words=_CHANGES_SAME_IC4) if ic4_prompt() else NR.changes_prompt_block(c)


def update_edits_prompt(base_ma, dlv1, items, order, changes, cv, data_text, brief_doc, facts, lines: dict) -> str:
    """BUILD IC-4 (D11): the edit update's request — the update request's context, Block 1's analysis, its brief answers and its
    deliverables, the change list, this run's data and its blocks, then <review_set> (every item, Python's order) and the rules, each
    limit stated from the constant its check uses. Event-neutral: every example describes a form."""
    b1 = {k: v for k, v in base_ma.items() if k != "brief_answers" and k in MasterAnalysis.model_fields}
    if has_diagram_data(base_ma):          # BUILD IC-5 (D15): the diagram as data, in mermaid_code's place
        b1 = {("diagram" if k == "mermaid_code" else k): (base_ma["diagram"] if k == "mermaid_code" else v) for k, v in b1.items()}
    answers = [e for e in (base_ma.get("brief_answers") or []) if isinstance(e, dict)]
    reply = (dlv1 or {}).get("reply") if isinstance((dlv1 or {}).get("reply"), dict) else {}
    brief = ("\n" + NR.brief_block(brief_doc, "oracle", facts, curveball=cv, update="edits")) if brief_doc else ("\n" + facts_block(facts))
    reserve_rule = ("outreach_cost_y1 is the brief's marketing reserve and is set by Python — never change it." if lines.get("reserve") else
                    "outreach_cost_y1 may change only when a change in the list moves it.")
    chg = changes_block(changes if changes else {"curveball": cv, "endpoints": None, "snapshot": {"source": "none"}})
    schema = NR.schema_block(MasterAnalysis.model_json_schema(), tuple(f for f, k in EDIT_LIST_FIELDS.items() if k not in ("outcome", "role")))
    return f"""<context>
This is BLOCK 2 of the competition. In Block 1 you wrote the analysis in <block1_analysis> and the deliverables in <block1_deliverables>
for this brief and its data. At the curveball the organizers made the announcement below and the data changed; Python compared every
endpoint record by record with Block 1's snapshot (<changes>). REVIEW every item of Block 1 against what changed: keep what the curveball
does not touch, replace what it changes, remove what it ends and add what it needs, one line for each item of <review_set>. A judge
scores how you responded: a rethink of what the change touches, not a new submission.
{lines['budget_line']}{lines['reserve_line']}{lines['pilot_line']}{lines['unit_line']}{lines['budget_note']}{lines.get('block1_budget_line', '')}
ALL arithmetic will be computed in Python — output only qualitative changes and assumption values.
{lines['date_line']}
</context>{brief}{lines['identity_block']}

{UPDATE_TAG}
{json.dumps(b1, indent=1, ensure_ascii=False)}
</block1_analysis>

<block1_brief_answers>
{json.dumps(answers, indent=1, ensure_ascii=False)}
</block1_brief_answers>

<block1_deliverables>
{json.dumps(reply, indent=1, ensure_ascii=False)}
</block1_deliverables>

{chg}
<data>
{data_text}
</data>
{lines.get("after_data", "")}
{review_set_block(items, order)}

<rules>
Reply with JSON Lines and nothing else: one JSON object on each line, no code fences, no prose, no line break inside an object. Write one
line for each item of <review_set>, in its order, then a line for each item you add. Python applies each line that checks. A line it
cannot apply is named on the page, and its item keeps Block 1's words; an item with no line is named on the page as not reviewed at the
curveball.
1. An item the curveball leaves as it is: {{"op": "keep", "id": "<its id>", "base": "<its base, as printed>"}}.
2. An item the curveball changes: {{"op": "replace", "id": "<its id>", "base": "<its base>", "value": <its complete new value, in its
   kind's form>, "because": ["<ref>", ...], "why": "<one sentence, at most {NR.EDIT_WHY_CHARS} characters>"}}. An item of a list that the
   curveball ends: {{"op": "remove", "id": "<its id>", "base": "<its base>", "because": ["<ref>", ...]}}; a field is replaced or kept.
3. A new item: {{"op": "add", "id": "new.<n>" (n counts from 1, each number once), "kind": "<one of: {", ".join(EDIT_ADD_KINDS)}>", "value":
   <its value>, "because": ["<ref>", ...]}}. An early warning names the risk it warns of — "parent": "<a risk's id, or the new.<n> of a
   risk added above it>"; a trigger may name its KPI the same way. Write an add after its parent's line.
4. because: refs from <changes>, exactly as written there — "announcement", an endpoint's name or a record's ref. A line whose reasons
   are not changes in the list is not applied.
5. The update's own items. update.summary: 2-4 plain sentences on how the program responds, at most {UPDATE_SUMMARY_CHARS:,} characters.
   test.<n>: the answer to that test, 2-4 plain sentences, at most {UPDATE_ANSWER_CHARS:,} characters. Kind impact — an add for each
   change that matters for this program, at most {UPDATE_WHAT_CHANGED_MAX}: {{"ref": "<a ref>", "impact": "<1-2 plain sentences,
   at most {UPDATE_IMPACT_CHARS:,} characters>"}}. Kind derived — an add, at most {UPDATE_DERIVED_MAX}: when an aggregate in the data and the
   records it summarises disagree, name the records for a figure Python computes in both blocks: {{"label": "<a short label,
   at most {DERIVED_LABEL_CHARS} characters — the records are listed beside it>", "endpoint": "<endpoint>", "record_ids": ["<record id>", ...] or
   "all", "field": "<numeric field>", "minus_field": "<numeric field, optional>", "compare_with": {{"endpoint": "<endpoint>", "field":
   "<field>"}} (optional)}}. Python shows the figure beside that aggregate with its own base: the two sources disagree, and neither is
   called out of date. Do not compute it yourself.
6. Each kind's form. text: a string (HTML only where Block 1's field holds it). text list: a list of strings. diagram: Mermaid code, a
   string. input: a value of the input's type in <block1_analysis>. outcome, role: an object with the keys and number types of Block 1's
   items. kpi, fm, ew, trigger, qa, thesis, renewal, headlines: an object of the form its Block 1 value has in <block1_deliverables>.
   summary, test, brief: plain text. risk, phase, card, row: an object of this schema, the one Block 1's analysis follows (a value that
   breaks it is not applied):
   {schema}
   {UPDATE_ITEM_KEYS_LINE}
7. Not items, so never changed by an update: {", ".join(IDENTITY_FIELDS)}, contract_value, contract_years, {", ".join(FUNDS_FIELDS)}
   (Block 1's use-of-funds plan) and the method parameters indirect_rate and discount_rate. Inputs (kinds input, outcome and role) change
   only when a change in the list moves them. {reserve_rule} Python computes the financial model with every input you change; when it cannot, none of them
   is applied, and the page says so. When you change an input, also replace, with the same refs, every item that states its Block 1
   value.
{_UPDATE_RULE8}
9. Keep [CHALLENGE_DATA: endpoint-name] tags and every [[…]] token where Block 1 uses them: Python writes in this run's
   figure.{_IC2_RULE9} Plain text in every new value; HTML only where Block 1's field already holds it.
</rules>

Return ONLY the JSON lines: one for each item of <review_set>, in its order, then one for each item you add."""


def _cut_words(e) -> str:
    """How a reply that is kept in part ended, in plain words. BUILD IC-6 (D6): NR.cut_kind's words — one classification for every cut."""
    return NR.cut_words(NR.cut_kind(e), e)


def call_update_edits(prompt, order=None):
    """BUILD IC-4 (D11, D9): the edit update's one call — its profile ("oracle/update": its limit and effort), the master's stage budget and
    its end time; each attempt started by the deadline rule for a call that keeps its partial (NR.start_check over a line-form profile: its
    first line must fit, IC-6 D2; UPDATE_FIRST_TEXT stated). → (text, how): "end_turn"; "cut: <how it ended>" — the text this call received, whose
    complete lines are applied (its limit, its end time, a dropped stream, any error after the text began); (None, "declined (<category>)")
    — a refusal, its text discarded (the vendor's refusals page: "treat any partial output as incomplete and discard it"). A second
    attempt only when no complete line came, the error is one an identical request can fix, and the rule allows it; anything else raises
    (the caller keeps Block 1's analysis, every item not reviewed). BUILD IC-6 (D6): through NR.line_call — the one path: after any cut
    but a refusal, the complete lines; then at most one continuation (a token or transport cut, NAIPC_IC6_CONTINUE on, its first line's
    time left), asked to go on from the next item of `order` (the review set's ids); its lines merged, a line for an id already written
    dropped and named. Its whole result is kept as call_update_edits.last."""
    label = "curveball update"
    _STAGE_END[label] = _clock() + MASTER_STAGE_BUDGET_S
    end = _end_at(label)            # BUILD IC-6 (D3): the earlier of the stage's end and the block's clock
    resume = NR.edit_resume(order)       # BUILD IC-6 (D12): the rule in the shared module — the Muse's update reads it too

    def refuse(words):
        raise StageBudgetExceeded(words)
    res = NR.line_call("oracle/update", lambda msgs, lab: _stream_text(None, SYSTEM_PROMPT, OPUS, "oracle/update", end_at=end, messages=msgs,
                                                                       label=lab),
                       prompt, end_at=end, clock=_clock, retryable=is_retryable, retry_wait=_retry_wait, sleep=_sleep, refuse=refuse,
                       log_dir=CHECKPOINT_DIR, model=OPUS, stated_rate=ORACLE_RATE, stated_first_text=UPDATE_FIRST_TEXT, begins=True,
                       stage_s=MASTER_STAGE_BUDGET_S, key_of=lambda o: str(o.get("id")) if o.get("id") is not None else None,
                       resume=resume, continue_ok=NR.ic6("NAIPC_IC6_CONTINUE"), stage_label=label)
    call_update_edits.last = res
    c_ = res.get("continuation")
    if c_:
        NR.say(f"    {label}: continuation — {c_['how']}" + (f"; {c_['lines']} line(s) more" if c_.get("label") else "")
               + (f"; {len(c_['dropped'])} written again, dropped" if c_.get("dropped") else ""))
    how = res["how"]
    if c_ and c_.get("label"):
        how += f"; continued: {c_['lines']} more line(s), the continuation's ending: {c_['how']}"
    return res["text"], how


def _assemble(old_list, old_ids, kind, by_id, adds, used):
    """One list after the edits, in Block 1's order — a removed item left out (its id a tombstone), a replaced item its new value, a kept
    or unreviewed item as it was — then the adds of its kind in the reply's order, each with the next free id of its kind (never a used
    or retired one). → (items, ids, tombstones, {new.<n>: its id}, the lines that changed the list)."""
    out, oid, tomb, assigned, changed = [], [], [], {}, []
    for j, x in enumerate(old_list):
        i = old_ids[j] if j < len(old_ids) else None
        a = by_id.get(i) if i else None
        if a is not None and a["op"] == "remove":
            tomb.append({"id": i, "kind": kind, "removed_by": f"the update's remove line (line {a['line']})"})
            changed.append(a)
            continue
        if a is not None and a["op"] == "replace":
            out.append(a["value"])
            oid.append(i)
            changed.append(a)
            continue
        out.append(x)
        oid.append(i)
    mine = [a for a in adds if a["kind"] == kind]
    for a, nid in zip(mine, REG.assign_item_ids(kind, len(mine), set(used) | {x for x in oid if x})):
        out.append(a["value"])
        oid.append(nid)
        assigned[a["id"]] = nid
        changed.append(a)
    return out, oid, tomb, assigned, changed


def edits_to_update(base_ma, dlv1, items, order, res, problems, partial, how, notes, n_read=0, inputs_ok=True):
    """BUILD IC-4 (D11): the applied lines → (the analysis after the edits, the record, the deliverables at Block 2). The record has the
    whole-field merge's form — amended (a text field replaced; a list any line changed), assumption_changes (an input replaced; an
    outcome's or a role's list changed), brief_answers_changed, what_changed (the impact adds), response_summary, test_answers,
    derived_requests (the derived adds), dropped (every line not applied, every problem of the reply, its partial last line, every key not
    used) — and "edits": how the reply ended, the review set's size, the lines read and applied, each item's status (kept, replaced,
    removed, added, not applied, not reviewed), the items not reviewed, each not-applied item's reason, each item's kind and first words.
    Groups: a list whose edits together break its schema is left as Block 1's, each of its lines named; `inputs_ok` False — the input
    lines are left out (the financial model could not be computed with them) and named. Ids: a kept or replaced item keeps its id; an add
    takes the next free number of its kind; a removed item is a tombstone; each early warning carries its risk's id."""
    ma0 = base_ma if isinstance(base_ma, dict) else {}
    merged = copy.deepcopy(ma0)
    check = _field_check(ma0)
    reply0 = (dlv1 or {}).get("reply") if isinstance((dlv1 or {}).get("reply"), dict) else {}
    status = dict(res["status"])
    why_not = {x["id"]: x["why"] for x in res["not_applied"] if x.get("id") in status}
    drops = [_drop("edit", x["id"] or f"line {x['line']}", x["why"], f"line {x['line']}: {x['detail']}".rstrip(": ")) for x in res["not_applied"]]
    drops += [_drop("edit", f"line {p['line']}", p["why"], p.get("detail") or "") for p in problems]
    if partial:
        drops.append(_drop("edit", "the last line", "the reply was cut inside its last line, so that line is not applied", f"{partial} characters"))
    drops += [_drop("edit", f"line {x['line']}", NR.extra_keys_reason(x["keys"]), "keys: " + json.dumps(x["keys"], ensure_ascii=False))
              for x in res["extra_keys"]]
    drops += [_drop("edit", i, NR.extra_keys_reason(ks), "keys its schema does not declare: " + json.dumps(ks, ensure_ascii=False))
              for i, ks in notes.items()]

    def refuse(group, why):
        for a in group:
            if a["op"] != "add":
                status[a["id"]] = "not applied"
                why_not[a["id"]] = why
        drops.append(_drop("edit", ", ".join(dict.fromkeys(a["id"] for a in group)), why))
    held = [a for a in res["applied"] if not inputs_ok and a["op"] != "keep"
            and ((items.get(a["id"]) or {}).get("kind") in EDIT_INPUT_KINDS or a.get("kind") in EDIT_INPUT_KINDS)]
    if held:
        refuse(held, UPDATE_INPUTS_GROUP_WHY)
    held_ids = {id(a) for a in held}
    applied = [a for a in res["applied"] if id(a) not in held_ids]
    by_id = {a["id"]: a for a in applied if a["op"] != "add"}
    adds = [a for a in applied if a["op"] == "add"]
    amended, a_changes = {}, []
    for i, a in by_id.items():
        it = items.get(i) or {}
        w = it.get("where") or (None, None)
        if a["op"] == "replace" and it.get("kind") in ("text", "text list", "diagram"):
            merged[w[1]] = a["value"]
            amended.setdefault(w[1], []).extend(a["because"])
        elif a["op"] == "replace" and it.get("kind") == "input":
            a_changes.append({"field": w[1], "old": ma0.get(w[1]), "new": a["value"], "because": a["because"],
                              "why": REG.fit_cap(a["why"], "oracle.update.why", UPDATE_WHY_CHARS) if a["why"] else ""})
            merged[w[1]] = a["value"]
    ids0 = REG.analysis_item_ids(ma0)
    used = {x for v in ids0["ids"].values() for x in v} | {t["id"] for t in ids0["tombstones"] if isinstance(t, dict)}
    new_ids, tombs, assigned = {}, list(ids0["tombstones"]), {}
    for f, kind in EDIT_LIST_FIELDS.items():
        old = ma0.get(f) if isinstance(ma0.get(f), list) else []
        out, oid, tomb, asg, changed = _assemble(old, ids0["ids"][f], kind, by_id, adds, used)
        if changed:
            stored, bad = check.value(f, out)        # the list as one group: its edits together must fit the schema (a length bound)
            if bad:
                refuse(changed, f"{SECTION_NAMES.get(f) or EXTRA_TEXT_NAMES.get(f) or f} with these edits does not fit the analysis's format "
                                f"({bad[0]}) — none of them is applied")
                new_ids[f] = list(ids0["ids"][f])
                continue
            out = stored
            merged[f] = out
            becs = list(dict.fromkeys(r for a in changed for r in a["because"]))
            if kind in ("outcome", "role"):
                a_changes.append({"field": f, "old": old, "new": out, "because": becs,
                                  "why": REG.fit_cap(" ".join(a["why"] for a in changed if a["why"]), "oracle.update.why", UPDATE_WHY_CHARS)})
            else:
                amended[f] = becs
        used |= set(oid)
        new_ids[f] = oid
        assigned.update(asg)
        tombs += tomb
        for nid in asg.values():
            status[nid] = "added"
    ans_changed = []
    for i, a in by_id.items():
        if (items.get(i) or {}).get("kind") == "brief" and a["op"] == "replace":
            bid = items[i]["where"][1]
            merged["brief_answers"] = [dict(e, answer=a["value"]) if isinstance(e, dict) and e.get("id") == bid else e
                                       for e in (merged.get("brief_answers") or [])]
            ans_changed.append({"id": bid, "because": a["because"]})
    sm = by_id.get("update.summary")
    summary = sm["value"] if sm and sm["op"] == "replace" else ""
    tests = [{"test": items[i]["where"][2], "answer": a["value"]} for i, a in by_id.items()
             if (items.get(i) or {}).get("kind") == "test" and a["op"] == "replace"]
    impacts = [a["value"] for a in adds if a["kind"] == "impact"]
    derived = [a["value"] for a in adds if a["kind"] == "derived"]
    if len(impacts) > UPDATE_WHAT_CHANGED_MAX:
        drops.append(_drop("list", "what_changed", f"{len(impacts) - UPDATE_WHAT_CHANGED_MAX} more than {UPDATE_WHAT_CHANGED_MAX} — not used"))
    if len(derived) > UPDATE_DERIVED_MAX:
        drops.append(_drop("list", "derived_figures", f"{len(derived) - UPDATE_DERIVED_MAX} more than {UPDATE_DERIVED_MAX} — not used"))
    dlv = None
    if reply0:       # the deliverables at Block 2: Block 1's reply with its edits applied, its ids and each warning's risk carried
        rf0 = deliverable_refs(dlv1, ma0)
        d_reply, d_ids, d_refs = copy.deepcopy(reply0), {}, []
        d_used = {x for v in rf0["ids"].values() for x in v}
        for f, kind in EDIT_DLV_FIELDS.items():
            old = reply0.get(f) if isinstance(reply0.get(f), list) else []
            out, oid, _tomb, asg, changed = _assemble(old, rf0["ids"].get(f) or [], kind, by_id, adds, d_used)
            d_used |= set(oid)
            for nid in asg.values():
                status[nid] = "added"
            if changed:
                d_reply[f] = out
            d_ids[f] = oid
            if f == "early_warnings":
                old_refs = dict(zip(rf0["ids"].get(f) or [], rf0.get("risk_refs") or []))
                par = {asg[a["id"]]: assigned.get(a.get("parent"), a.get("parent")) for a in adds if a["kind"] == "ew" and a["id"] in asg}
                b1_risks = list(ids0["ids"].get("risks") or [])
                for x in oid:        # IC-4 Phase 7 (review c17): a replaced warning that names another risk — its parent, or its value's risk
                    a = by_id.get(x)
                    if a and a["op"] == "replace" and x not in par:
                        nv, ov = a["value"] if isinstance(a["value"], dict) else {}, dict(zip(rf0["ids"].get(f) or [], old)).get(x) or {}
                        to = (a.get("parent") if isinstance(a.get("parent"), str) and (a.get("parent") in (new_ids.get("risks") or [])
                                                                                    or a.get("parent") in assigned)
                              else nv.get("risk") if isinstance(nv.get("risk"), str) and nv.get("risk") in (new_ids.get("risks") or [])
                              else b1_risks[nv["risk"]] if (isinstance(nv.get("risk"), int) and not isinstance(nv.get("risk"), bool)
                                                            and nv.get("risk") != (ov or {}).get("risk") and 0 <= nv["risk"] < len(b1_risks)) else None)
                        if to:
                            par[x] = assigned.get(to, to)
                d_refs = [par[x] if x in par else old_refs.get(x) for x in oid]
        for key, i in (("thesis", "thesis.1"), ("renewal", "d.renewal"), ("headlines", "d.headlines")):
            a = by_id.get(i)
            if a and a["op"] == "replace":
                d_reply[key] = a["value"]
        d_ids["thesis"] = ["thesis.1"] if isinstance(d_reply.get("thesis"), dict) else []
        # IC-4 Phase 7 (review a1, D1): a KPI the update amends keeps its id; the items that read it by its indicator (failure modes,
        # triggers, warnings, the thesis's tripwire) follow it — unless the update replaced them itself
        ren = {}
        for kid, kv in zip(rf0["ids"].get("kpis") or [], reply0.get("kpis") if isinstance(reply0.get("kpis"), list) else []):
            a = by_id.get(kid)
            if a and a["op"] == "replace" and isinstance(a["value"], dict) and isinstance(kv, dict):
                o_, n_ = str(kv.get("indicator") or "").strip(), str(a["value"].get("indicator") or "").strip()
                if o_ and n_ and o_.lower() != n_.lower():
                    ren[o_.lower()] = n_
        followed = []
        if ren:
            for f in ("failure_modes", "triggers", "early_warnings"):
                if isinstance(d_reply.get(f), list):
                    d_reply[f] = copy.deepcopy(d_reply[f])
                    for x, xid in zip(d_reply[f], d_ids.get(f) or []):
                        a = by_id.get(xid)
                        if isinstance(x, dict) and not (a and a["op"] == "replace") and str(x.get("indicator") or "").strip().lower() in ren:
                            x["indicator"] = ren[str(x["indicator"]).strip().lower()]
                            followed.append(xid)
            a = by_id.get("thesis.1")
            if isinstance(d_reply.get("thesis"), dict) and not (a and a["op"] == "replace"):
                d_reply["thesis"] = copy.deepcopy(d_reply["thesis"])
                tw = d_reply["thesis"].get("tripwire")
                if isinstance(tw, dict) and str(tw.get("indicator") or "").strip().lower() in ren:
                    tw["indicator"] = ren[str(tw["indicator"]).strip().lower()]
                    followed.append("thesis.1")
        if followed:
            NR.say(f"    update: {len(followed)} item(s) follow the KPI the update amended: {', '.join(followed[:8])}")
        dlv = {"status": "edited", "reply": d_reply, "reply_file": (dlv1 or {}).get("reply_file"), "ids": d_ids, "risk_refs": d_refs}
    merged["item_ids"] = REG.item_ids_doc({"ids": new_ids, "tombstones": tombs})
    words = {i: {"kind": items[i]["kind"], "words": NR.cut(" ".join(_texts(items[i]["value"])) or json.dumps(items[i]["value"], ensure_ascii=False), 80)}
             for i in order}
    counts = {k: sum(1 for v in status.values() if v == k) for k in ("kept", "replaced", "removed", "added", "not applied", "not reviewed")}
    info = {"amended": [{"field": f, "because": list(dict.fromkeys(rs))} for f, rs in amended.items()], "assumption_changes": a_changes,
            "brief_answers_changed": ans_changed, "what_changed": impacts[:UPDATE_WHAT_CHANGED_MAX], "response_summary": summary,
            "test_answers": tests, "derived_requests": derived[:UPDATE_DERIVED_MAX], "dropped": drops,
            "edits": {"how": how, "review_set": len(order), "lines": n_read, "applied": len(applied), "counts": counts, "status": status,
                      "not_reviewed": [i for i in order if status.get(i) == "not reviewed"], "why_not": why_not, "items": words}}
    return merged, info, dlv


def _edits_unread(items, order, how) -> dict:
    """The edit record of a reply that was not read (a failed call, a refusal): every item not reviewed."""
    return {"how": how, "review_set": len(order), "lines": 0, "applied": 0,
            "counts": {"kept": 0, "replaced": 0, "removed": 0, "added": 0, "not applied": 0, "not reviewed": len(order)},
            "status": {i: "not reviewed" for i in order}, "not_reviewed": list(order), "why_not": {},
            "items": {i: {"kind": items[i]["kind"], "words": NR.cut(" ".join(_texts(items[i]["value"])) or json.dumps(items[i]["value"], ensure_ascii=False), 80)}
                      for i in order}}


def run_update_edits(base, changes, cv, data_text, brief_doc, facts, lines, pin):
    """BUILD IC-4 (D11): the update as keyed edits (the switch on) — one call over every item; Python applies each line that checks and
    returns (analysis, curveball_update) in run_update's form, with "edits" (each item's status) and "deliverables" (Block 1's reply with
    its edits). Status "applied" when the reply was read — whole, or cut (its complete lines applied, the rest not reviewed, the record's
    edits.how saying how it ended); "failed: declined (<category>)" for a refusal (nothing applied); "failed: <reason>" for a call that
    failed or an analysis that does not validate. Block 1's analysis stands whenever the status is failed, every item not reviewed.
    Never raises."""
    refs = NR.curveball_refs(changes) if changes else {"announcement": "announcement"}
    dlv1 = block1_deliverables()
    items, order = update_review_set(base["ma"], dlv1, lines.get("reg"), cv, brief_doc)
    prompt = diagram_update_words(update_edits_prompt(base["ma"], dlv1, items, order, changes, cv, data_text, brief_doc, facts, lines),
                                  items)       # BUILD IC-5 (D15): the kind's form for a diagram held as data
    NR.say(f"    curveball update: keyed edits over {len(order)} items of Block 1 (run {base['meta'].get('run_id')}) — prompt {len(prompt):,} characters")
    info = {"what_changed": [], "response_summary": "", "derived_requests": [], "test_answers": [], "amended": [], "dropped": [],
            "assumption_changes": [], "brief_answers_changed": [], "reply_file": None, "deliverables": None}

    def unchanged(status, how):
        ma_ = copy.deepcopy(base["ma"])
        ma_["item_ids"] = REG.item_ids_doc(REG.analysis_item_ids(base["ma"]))
        return ma_, dict(info, status=status, edits=_edits_unread(items, order, how))
    try:
        raw, how = call_update_edits(prompt, order)
    except Exception as e:  # noqa: BLE001 — every GenerationError and transport failure: Block 1's analysis, every item not reviewed
        NR.say(f"    ⚠ curveball update failed — {type(e).__name__}: {str(e)[:200]}; the analysis is Block 1's, unchanged — no item was reviewed")
        return unchanged(f"failed: {type(e).__name__}", "failed")
    if raw is None:
        NR.say(f"    ⚠ curveball update {how} — its text is discarded and nothing is applied; the analysis is Block 1's — no item was reviewed")
        return unchanged(f"failed: {how}", how)
    info["reply_file"] = keep_reply("update_reply", raw)       # before parsing, so a reply that does not parse is kept too
    rows, problems, partial = REG.admit(NR.parse_edit_lines(raw, complete=(how == "end_turn")), "reply", "update edit lines")   # BUILD IC-5 (D1): the one way in
    notes = {}
    validate = edit_validator(base["ma"], (dlv1 or {}).get("reply") or {}, items, refs, lines.get("reserve"), notes)
    res = NR.apply_edits(rows, items, order, refs, validate, add_kinds=EDIT_ADD_KINDS, parent_kinds=EDIT_PARENTS,
                         parent_required=EDIT_PARENT_REQUIRED, remove_kinds=EDIT_REMOVE_KINDS,
                         because_optional=("summary", "test"))      # IC-4 Phase 7 (review c18): the update's own words
    n_read = len(rows) + len(problems) + (1 if partial else 0)
    for x in res["not_applied"]:          # a field that is no item (identity, budget, use of funds, method): the merge's TRUE reason
        fld = (x.get("id") or "")[2:] if str(x.get("id") or "").startswith("f.") else ""
        if fld and x["id"] not in items and fld in MasterAnalysis.model_fields:
            x["id"], x["why"] = fld, fixed_reason(fld, lines.get("reserve"))
    merged, minfo, dlv = edits_to_update(base["ma"], dlv1, items, order, res, problems, partial, how, notes, n_read=n_read)
    cont_ = ((getattr(call_update_edits, "last", None) or {}).get("continuation") or {})      # BUILD IC-6 (D6): named, never applied twice
    for k_ in cont_.get("dropped") or []:
        minfo["dropped"].append(_drop("edit", k_, "written again in the continuation", ""))
    if minfo["assumption_changes"]:
        try:
            _fin_quiet(merged)
        except Exception:  # noqa: BLE001 — the inputs as one group: none applied when the financial model cannot be computed with them
            merged, minfo, dlv = edits_to_update(base["ma"], dlv1, items, order, res, problems, partial, how, notes, n_read=n_read, inputs_ok=False)
    try:     # the edited analysis in the schema's stored form, as Block 1's model_dump() is (each item and list already passed on its own)
        ids_ = merged.get("item_ids")
        merged.update(MasterAnalysis(**{k: v for k, v in merged.items() if k in MasterAnalysis.model_fields}).model_dump())
        merged["item_ids"] = ids_
    except Exception as e:  # noqa: BLE001 — not expected: every item and list passed on its own
        NR.say(f"    ⚠ curveball update failed — the edited analysis does not validate ({type(e).__name__}); the analysis is Block 1's — no item was reviewed")
        return unchanged("failed: the edited analysis does not validate", "failed")
    info.update(minfo)
    info["deliverables"] = dlv
    g_ = base["ma"].get("inputs_given")
    if isinstance(g_, list):     # BUILD B3 (D1): an input the update changed is the model's now
        merged["inputs_given"] = sorted(set(g_) | {c.get("field") for c in info.get("assumption_changes") or [] if isinstance(c, dict)})
    for d in info["dropped"]:
        NR.say(f"    update: not applied {d['field']} — {d['why']}")
    n = info["edits"]["counts"]
    NR.say(f"    update: {n_read} line(s) read — kept {n['kept']}, replaced {n['replaced']}, removed {n['removed']}, added {n['added']}, "
           f"not applied {n['not applied']}, not reviewed {n['not reviewed']} of {len(order)}" + ("" if how == "end_turn" else f" (the reply: {how})"))
    if pin:
        merged["program_name"], merged["brand_html"] = pin["program_name"], pin.get("brand_html") or merged.get("brand_html")
    return merged, dict(info, status="applied")


def run_update(base, changes, cv, data_text, brief_doc, facts, lines, pin):
    """D3: ONE update call over Block 1's base; returns (analysis, curveball_update). A failed call or an unparseable reply → Block 1's
    analysis unchanged, status "failed: <reason>", ⚠ printed — never raised (the stage completes: a complete page, not a partial one).
    P0.7 r2 (E2, E3): the reply's text kept the moment it arrives, before parsing (checkpoints/update_reply_<run id>.txt, its name in
    the record); every part of the reply not applied — an amendment, an assumption change, a brief answer, a list's overflow or entry,
    a part of the wrong type — one entry of the record's `dropped`; every text cut marked (NR.cut) and printed once.
    Phase 5: a top-level key the update does not ask for (C14), text outside the reply's JSON object (C16) and a key given twice in one
    object (the review's #7 a — json keeps the last) are entries too ('the reply').
    BUILD IC-4 (Q-SWITCH, D11): with the switch on, the update is keyed edits (run_update_edits); off, this whole-field merge."""
    NR.mark_path("update.keyed_lines" if ic4_prompt() else "update.whole_object")     # BUILD IC-8 (D14): the update's protocol
    if ic4_prompt():
        return run_update_edits(base, changes, cv, data_text, brief_doc, facts, lines, pin)
    refs = NR.curveball_refs(changes) if changes else {"announcement": "announcement"}
    prompt = ic3_words(update_prompt(base["ma"], changes, cv, data_text, brief_doc, facts, lines), "update")   # IC-3 (D7)
    NR.say(f"    curveball update: one call over Block 1's analysis (run {base['meta'].get('run_id')}) — prompt {len(prompt):,} characters")
    info = {"what_changed": [], "response_summary": "", "derived_requests": [], "test_answers": [], "amended": [], "dropped": [],
            "assumption_changes": [], "brief_answers_changed": [], "reply_file": None}
    try:
        raw = call_claude_update(prompt)
    except Exception as e:  # noqa: BLE001 — every GenerationError and transport failure: Block 1's analysis, labelled
        NR.say(f"    ⚠ curveball update failed — {type(e).__name__}: {str(e)[:200]}; the analysis is Block 1's, unchanged")
        return copy.deepcopy(base["ma"]), dict(info, status=f"failed: {type(e).__name__}")
    info["reply_file"] = keep_reply("update_reply", raw)       # E3 (d): before parsing, so a reply that does not parse is kept too
    reply, why = parse_update_reply(raw)
    if reply is None:
        keep_partial("update-reply", 1, raw)
        NR.say(f"    ⚠ curveball update failed — {why}; the analysis is Block 1's, unchanged")
        return copy.deepcopy(base["ma"]), dict(info, status=f"failed: {why}")
    merged, minfo = merge_update(base["ma"], reply, refs, lines.get("reserve"), brief_doc)
    try:     # the merged analysis in the schema's stored form, as Block 1's model_dump() is (each field already passed on its own)
        merged.update(MasterAnalysis(**{k: v for k, v in merged.items() if k in MasterAnalysis.model_fields}).model_dump())
    except Exception as e:  # noqa: BLE001 — not expected: every kept field validated with Block 1's others
        NR.say(f"    ⚠ curveball update failed — the merged analysis does not validate ({type(e).__name__}); the analysis is Block 1's, unchanged")
        return copy.deepcopy(base["ma"]), dict(info, status="failed: the merged analysis does not validate")
    info.update(minfo)
    g_ = base["ma"].get("inputs_given")
    if isinstance(g_, list):     # BUILD B3 (D1): an input the update changed is the model's now
        merged["inputs_given"] = sorted(set(g_) | {c.get("field") for c in info.get("assumption_changes") or [] if isinstance(c, dict)})
    wc, d1 = take_what_changed(reply, refs, "what changed")
    info["response_summary"], d2 = take_text(reply, "response_summary", UPDATE_SUMMARY_CHARS, "the response summary")
    info["test_answers"], d3 = take_entries(reply, "test_answers", UPDATE_TESTS_MAX, {"test": UPDATE_TEST_CHARS, "answer": UPDATE_ANSWER_CHARS},
                                            "the curveball tests' answers", need={"answer": "no answer given"})
    info["derived_requests"], d4 = take_derived(reply)
    info["what_changed"] = wc
    unknown = [k for k in reply if k not in UPDATE_REPLY_KEYS]     # P0.7 r2 Phase 5 (C14): a top-level key the update does not ask for
    d0 = [_drop("shape", "reply", NR.unknown_keys_reason(unknown), "keys: " + json.dumps(unknown, ensure_ascii=False))] if unknown else []
    n_out = reply_outside(raw)                                      # Phase 5 (C16): text around the JSON object — never read by the merge
    d0 += [_drop("shape", "reply", NR.outside_text_reason(n_out, info["reply_file"]), "outside extract_json's object")] if n_out else []
    dups = REG.admit(NR.duplicate_keys(extract_json(raw)), "reply", "update keys")   # BUILD IC-5 (D1); Phase 5 review #7 (a): json keeps the last of a key given twice
    d0 += [_drop("shape", "reply", NR.duplicate_keys_reason(dups), "keys: " + json.dumps(dups, ensure_ascii=False))] if dups else []
    for d in d1 + d2 + d3 + d4 + d0:
        NR.say(f"    update: dropped {d['field']} — {d['why']}")
    info["dropped"] = minfo["dropped"] + d1 + d2 + d3 + d4 + d0
    NR.say(f"    update: kept {len(info['amended'])} amendment(s), {len(info['assumption_changes'])} assumption change(s), "
           f"{len(info['brief_answers_changed'])} brief answer(s); dropped {len(info['dropped'])}")
    if pin:
        merged["program_name"], merged["brand_html"] = pin["program_name"], pin.get("brand_html") or merged.get("brand_html")
    return merged, dict(info, status="applied")


@stage("master_analysis", depends_on=["data_loading"])
def stage_master_analysis(deps: dict) -> dict:
    """Run the master analysis: domain identification + full structured output."""
    data = deps["data_loading"]
    raw = data["raw"]

    # Build the data package for Claude
    data_text = ""
    for ep, payload in raw.items():
        data_text += f"\n<endpoint name=\"{ep}\">\n{json.dumps(payload, indent=2)}\n</endpoint>\n"

    # P0.6 (D1, D2, D4, D5): every Rule 4 value comes from the contract (NR.facts); a budget the brief does not state is the
    # model's proposal, shown as such; no example names a place, organization, ZIP, program or number from any practice event.
    brief_doc = _brief_contract()
    F = NR.facts(brief_doc)
    curveball_ctx = os.environ.get("CURVEBALL_TEXT", "")
    budget, budget_src = budget_in_use(F)
    reserve = F["marketing"]
    unit_p, unit_s = F["unit_plural"], F["unit_singular"]
    if budget is not None:
        contract_display = NR.money_short(budget)
        cap_base = budget - reserve if reserve and reserve < budget else budget
        cap_base_text = (f"{NR.money_short(cap_base)}, the {contract_display} budget minus the brief's {NR.money_short(reserve)} marketing reserve"
                         if cap_base != budget else f"the {contract_display} contract")
        budget_line = (f"You are preparing a {contract_display} government service contract proposal ("
                       + ("the brief's own figure" if budget_src == "brief" else "a budget override") + ").")
        cap_sentence = f"must NOT exceed ${int(cap_base * 0.48):,} (48% of {cap_base_text})."
        share = lambda lo, hi: f"{lo}-{hi}% ({NR.money_short(cap_base * lo / 100)}-{NR.money_short(cap_base * hi / 100)})"
        audience_budget = f"a {contract_display} contract"
        contract_value_line = f"{budget:,} (" + ("the brief's figure" if budget_src == "brief" else "the budget override") + ")"
    else:
        contract_display = "your proposed budget"
        cap_base_text = "your proposed contract_value" + (" minus the brief's marketing reserve" if reserve else "")
        budget_line = ("You are preparing a government service contract proposal. The brief states no contract value — propose the budget your "
                       "design needs as contract_value, in whole dollars; it will be shown as your estimate.")
        cap_sentence = f"must NOT exceed 48% of {cap_base_text}."
        share = lambda lo, hi: f"{lo}-{hi}%"
        audience_budget = "a contract proposal whose budget you propose"
        contract_value_line = "the budget you propose, in whole dollars (the brief states none)"
    outreach_text = (f"; outreach_cost_y1 is the brief's marketing reserve, {reserve:,} — Python sets it" if reserve
                     else f", outreach_cost_y1 {share(3, 5)}")
    reserve_line = (f"\nThe brief reserves {NR.money_short(reserve)} for marketing, trust-building and adoption (the brief: \"{F['marketing_quote']}\"): "
                    f"Python sets outreach_cost_y1 to exactly {reserve:,}; every other cost and the personnel cap are sized to the budget minus that reserve."
                    if reserve else "")
    pilot_line = (f"\nThe pilot runs {F['pilot_days']} days (the brief: \"{F['pilot_quote']}\"); timelines and phases fit inside it."
                  if F["pilot_days"] else "\nThe brief states no pilot length: any timeline you give is your own proposal — say so where you give one.")
    unit_line = (f"\nCount the people the program serves in the brief's unit: {unit_p} (one {unit_s})." if F["unit_stated"]
                 else "\nThe brief names no beneficiary unit: count 'beneficiaries' and say in the methodology what one is.")
    on = ic3_prompt()          # BUILD IC-3 (D7): with the switch on, Python's own numbers print as `[[id]] = value` (ledger step 52)
    on4 = ic4_prompt()         # BUILD IC-4 (D14, D6): the outcome request by materiality and the cost of inaction from the list
    reg_now = build_registry({"data_loading": data}) if on else None
    if on:
        ctx = context_lines_ic3(reg_now, F, curveball=bool(curveball_ctx))
        budget_line, reserve_line, pilot_line, unit_line = ctx["budget_line"], ctx["reserve_line"], ctx["pilot_line"], ctx["unit_line"]
        audience_budget = "this contract proposal"
        if budget is not None:
            contract_value_line = ("the budget in whole dollars, as a JSON number (Python uses "
                                   + ("the brief's figure" if budget_src == "brief" else "the budget override") + ", [[budget]])")
    day = F["date"] or datetime.now().date()
    date_line = (f"The competition day is {NR.date_long(day)}." if F["date"] else f"Today is {NR.date_long(day)}.") + \
        f" All program launch timelines start in {day.year} or later, never earlier."
    pin, pin_note = identity_pin(brief_doc, F)
    identity_block = ""
    if pin:
        identity_block = (f"\n<identity>\nThe program's name is fixed: {pin['program_name']}"
                          + (f" (brand: {pin['brand_html']})" if pin.get("brand_html") else "")
                          + ". Use exactly this name for program_name and everywhere you name the program; brand_html is this name with one "
                          "word wrapped in <em>. Do not rename the program.\n</identity>")
    NR.say(f"    identity: {pin['program_name']} ({pin['source']}: {pin_note})" if pin else f"    identity: the model names the program ({pin_note})")
    n_fetched = len([s for s in data.get("summary", []) if isinstance(s, dict) and "error" not in s])
    curveball_prompt = ""
    changes, cv, cb_full_reason = None, None, None
    if curveball_ctx:
        budget_note = (f"\nIMPORTANT: The contract budget is now {contract_display} (an override of the brief's figure). All financial references must use this figure."
                       if budget_src == "override" else "")
        if on:
            budget_note = ctx["budget_note"]
        # ── P0.7 (D3): Block 2 starts from Block 1's base — one update call — unless there is none or --curveball-full ──
        chal = F.get("challenge") or os.environ.get("NAIPC_CHALLENGE", "").strip() or None
        changes, cwhy = _changes_from_env()
        if changes is None:
            NR.say(f"    curveball: no change list for this run ({cwhy}) — the announcement alone")
        cv = curveball_object(changes)
        if pin:   # every Block 2 path writes this run's id into the identity file (the pipeline hands the name on only with it)
            why = NR.write_identity(CHECKPOINT_DIR / NR.IDENTITY_FILE, chal, NR.run_id(), pin["program_name"], pin.get("brand_html"),
                                    pin.get("domain_label"), pin["source"])
            if why:
                NR.say(f"    identity: {why}")
        if os.environ.get("CURVEBALL_FULL", "").strip() == "1":
            cb_full_reason = "full"
            NR.say("    curveball: --curveball-full — the analysis is regenerated with the change list (no update of Block 1's)")
        else:
            bdet = {}
            base, bwhy = load_block1_base(chal, (changes or {}).get("block1_run_id"), detail=bdet)
            if base is None:
                cb_full_reason = f"no base: {bwhy}"
                NR.say(f"    curveball: {bwhy}" + (f" ({bdet['why']})" if bdet.get("why") else "") + " — the full regeneration, with the change list in the prompt")
            else:
                b1_budget = ((base.get("fin") or {}).get("kpis") or {}).get("contract")   # review F1: Block 1 written for another budget
                b1_line = (f"\nBlock 1's analysis was written for a budget of {NR.money_short(b1_budget)}; the budget in use is now "
                           f"{contract_display}: amend every field that states Block 1's budget (because: \"announcement\")."
                           if isinstance(b1_budget, (int, float)) and not isinstance(b1_budget, bool) and budget is not None and b1_budget != budget else "")
                if on and b1_line:      # IC-3 (D7): Block 1's budget and this one by id, when both are registered
                    b1_line = context_lines_ic3(reg_now, F, True, b1_budget)["block1_budget_line"] or b1_line
                lines = {"budget_line": budget_line, "reserve_line": reserve_line, "pilot_line": pilot_line, "unit_line": unit_line,
                         "budget_note": budget_note, "date_line": date_line, "identity_block": identity_block, "reserve": reserve,
                         "block1_budget_line": b1_line, "reg": reg_now,    # BUILD IC-4 (D11): the review set's first group
                         "registry_block": registry_prompt_block(build_registry({"data_loading": data}), raw, F),   # B3 (D2): Block 2's ids
                         "after_data": (after_data_ic3(reg_now, raw, F) + (changed_mentions_block(reg_now, base["ma"]) + "\n" if
                                                                            changed_mentions_block(reg_now, base["ma"]) else "")) if on else ""}   # IC-3 (D10, D11)
                out, cb = run_update(base, changes, cv, data_text, brief_doc, pipeline_facts(n_fetched, update=True), lines, pin)
                if "edits" not in cb:                    # BUILD IC-4 (D11): the edit path stored its ids, line by line
                    with_item_ids(out, base["ma"])       # BUILD IC-4 (D1): Block 1's ids carried onto the updated analysis
                out["curveball_update"] = cb
                if on4:            # BUILD IC-4 (D14): an outcome valued by a figure of the data, read on this run's data
                    out["sroi_data_values"] = sroi_data_values(out, reg_now)
                src = pin["source"] if pin else "block1"
                why = NR.write_identity(CHECKPOINT_DIR / NR.IDENTITY_FILE, chal, NR.run_id(), out.get("program_name"), out.get("brand_html"),
                                        out.get("domain_label"), src)
                NR.say(f"    identity: {out.get('program_name')} ({src}) written to checkpoints/{NR.IDENTITY_FILE}" if why is None else f"    identity: {why}")
                return out
        chg = changes_block(changes if changes else {"curveball": cv, "endpoints": None,       # BUILD IC-4 (D11): the switch's words
                                                      "snapshot": {"source": "no change list of this run"}})
        curveball_prompt = f"""
<curveball>
MID-COMPETITION UPDATE — the announcement (verbatim): {curveball_ctx}{budget_note}
{chg}
This analysis is written fresh at the curveball, so the curveball must be visible and every change it makes must be said:
1. BLUF (bluf_text): name what the announcement and the change list describe and what it does to the program; when it adds people,
   say how many and from which source (the announcement, or a record ref of the change list).
2. POPULATION FUNNEL: when the curveball adds affected people, state the earlier count, the added count and the new total.
3. CRISIS NARRATIVE: cite the changed records by their refs as the change list writes them.
4. RISK REGISTER: include one risk the curveball creates, with the numbers the announcement or the data give.
5. IMPLEMENTATION PHASES: show how Phase 1 responds to it.
6. SENSITIVITY ANALYSIS: use the curveball as the stress case.
Also return, as top-level keys outside the schema (the page's "What changed at the curveball" section is built from them):
"what_changed": [{{"ref": "<a ref from the change list>", "impact": "<1-2 plain sentences, at most {UPDATE_IMPACT_CHARS:,} characters>"}}] (at most
{UPDATE_WHAT_CHANGED_MAX} entries), "response_summary": "<2-4 plain sentences, at most {UPDATE_SUMMARY_CHARS:,} characters>" and "test_answers":
[{{"test": "<the test's words, at most {UPDATE_TEST_CHARS:,} characters>", "answer": "<2-4 plain sentences, at most {UPDATE_ANSWER_CHARS:,} characters>"}}]
(one per test the announcement states, at most {UPDATE_TESTS_MAX}). Python uses only what fits these limits; the page names any part of these three it did not use.
</curveball>
"""

    funds_prompt = funds_prompt_lines(reserve, ids=on)      # BUILD B1 (D4): who runs each line; the reserve's allocation (IC-3 D7: its id)
    engine_block = engine_prompt_block(brief_doc, raw)      # BUILD B1: the published prioritization table, when it runs
    # P0.3b (D9): the brief block when pipeline.py exported a contract; P0.6 (D4, D7): the true facts about the method go into the
    # prompt with or without one (inside the brief block when there is a contract, in their own block when there is none)
    facts_now = pipeline_facts(n_fetched)
    registry_prompt = registry_prompt_block(build_registry({"data_loading": data}), raw, F)   # BUILD B3 (D2): the ids the lock cites
    registry_top = "" if on else registry_prompt            # BUILD IC-3 (D10): with the switch on, the block sits after the data
    after_data = after_data_ic3(reg_now, raw, F) if on else ""
    brief_prompt = ("\n" + NR.brief_block(brief_doc, "oracle", facts_now, curveball=cv)) if brief_doc else ("\n" + facts_block(facts_now))

    # BUILD IC-3 (D7): items 9 and 12 — the P&L's span and the deadweight by id with the switch on (the model copied "three years"
    # into its prose; "15% deadweight" is Python's default, which the model's own input replaces); the switch off keeps IC-2's text
    cq9 = ('9. COST OF INACTION: wherever you reference the cumulative cost of inaction (in bluf_text, decision_text, or\n   cost_of_inaction), cite [[coi_3yr]] — Python computes it from the P&L\'s people served. Example: "Without\n   intervention, an estimated [[coi_3yr]] in avoidable downstream costs will accumulate over [[plan.years]] years."\n' if on else '9. COST OF INACTION: wherever you reference the 3-year cost of inaction (in bluf_text, decision_text, or\n   cost_of_inaction), cite [[coi_3yr]] — Python computes it from the P&L\'s people served. Example: "Without\n   intervention, an estimated [[coi_3yr]] in avoidable downstream costs will accumulate over three years."\n')
    cq12a = ('12. COST-OF-INACTION MATH, YEAR BY YEAR: show the breakdown with Python\'s figures: "Year 1: [[coi_y1]],\n    Year 2: [[coi_y2]], Year 3: [[coi_y3]]; cumulative cost over [[plan.years]] years: [[coi_3yr]]" (each year\'s avoidable\n' if on else '12. COST-OF-INACTION 3-YEAR MATH: show the breakdown with Python\'s figures: "Year 1: [[coi_y1]],\n    Year 2: [[coi_y2]], Year 3: [[coi_y3]]; cumulative 3-year cost: [[coi_3yr]]" (each year\'s avoidable\n')
    cq12b = ('    of doing nothing. The SROI already applies deadweight ([[deadweight]]) to its benefit valuations. Applying\n' if on else '    of doing nothing. The SROI already applies 15% deadweight to its benefit valuations. Applying\n')
    cq12c = ("    Also: the cumulative cost spans the P&L's [[plan.years]] years — never name a longer span.\n" if on else '    Also: say "Cumulative 3-year cost" not "5-year cost" when showing 3 years of data.\n')
    # BUILD IC-4 (D6, D14): with the switch on, rules 9 and 12 say what the cost of inaction is now — Python's, from the valued outcomes;
    # the switch off: IC-3's rule 12 (the ER-visit counts), byte for byte as the prompt printed it
    cq12 = (cq12a + "    visits: [[coi_visits_y1]], [[coi_visits_y2]], [[coi_visits_y3]]). Compute none of them yourself.\n"
            "    CRITICAL: Do NOT apply any deadweight, severity multiplier, or conservative adjustment to the\n"
            "    cost-of-inaction total. Deadweight is an SROI concept that adjusts PROGRAM BENEFITS downward —\n"
            "    it does not belong in the cost-of-inaction, which represents the FULL unadjusted downstream cost\n"
            + cq12b + "    deadweight AGAIN to the cost-of-inaction would be methodological double-counting.\n" + cq12c)
    if on4:
        cq9 = ('9. COST OF INACTION: wherever you reference the cumulative cost of inaction (in bluf_text, decision_text, or\n'
               '   cost_of_inaction), cite [[coi_3yr]] — Python computes it from your valued outcomes (sroi_outcomes). Example: "Without the\n'
               '   program, outcomes valued at an estimated [[coi_3yr]] would not happen over [[plan.years]] years."\n')
        cq12 = ('12. COST-OF-INACTION MATH, YEAR BY YEAR: show the breakdown with Python\'s figures: "Year 1: [[coi_y1]],\n'
                '    Year 2: [[coi_y2]], Year 3: [[coi_y3]]; cumulative cost over [[plan.years]] years: [[coi_3yr]]". Each year\'s figure is\n'
                '    Python\'s, from your valued outcomes: the outcomes that year\'s people served would gain, times each outcome\'s value,\n'
                '    less deadweight ([[deadweight]]) — what would not happen without the program. Compute none of them yourself and adjust\n'
                '    none of them further. An outcome you state as material, not valued, is not in the figure: Python names it beside it.\n'
                + cq12c)
    sroi_ask = outcome_request_ic4(brief_doc) if on4 else SROI_ASK_IC3
    fc_block = constraints_block_ic3(reg_now, F) if on else f"""<financial_constraints>
CRITICAL — your staffing assumptions MUST fit within these bounds or the financial model will show deficits:
1. Total Year 1 personnel costs (salary × count × (1 + benefits_pct)) {cap_sentence}
   Size the team to the personnel cap: 1 Program Director ($75K-$78K), frontline staff like CHWs/navigators/coordinators ($45K-$51K each) and admin/data support ($38K-$45K) only as the cap allows. Benefits at 25-30%.
2. As shares of {cap_base_text}: ops_cost_y1 {share(12, 18)}, tech_cost_y1 {share(6, 10)}{outreach_text}.
3. target_beneficiaries_y1: derive it from the data — the {unit_p} this program can actually reach in Year 1 with this budget — counted in the brief's unit.
4. The base case P&L MUST show positive surplus in all 3 years. If your personnel assumptions are too high, REDUCE staff count — do NOT exceed the personnel cap.
5. In renewal_text and sustainability projections, cite the BASE CASE Year 3 cost per {unit_s} from the P&L as
   [[cpb_y3]] — never a more optimistic figure of your own.
6. In Key Assumptions, an outcome rate applies to the people the program serves each year ([[beneficiaries_y1]] in
   Year 1), NOT to the total affected population — the SROI counts outcomes for people the program actually SERVES. Write
   a rate of your own as [[model:<rate>|<what it is>]]; Python computes every count from it.
</financial_constraints>"""   # IC-3 (D7): the switch off — IC-2's block, verbatim
    prompt = f"""<context>
You are receiving raw JSON datasets from API endpoints for a civic challenge.
The domain is unknown — you must infer it from the data.
{budget_line}{reserve_line}{pilot_line}{unit_line}
ALL arithmetic will be computed in Python — output only qualitative assumptions and ranges.
{date_line}
</context>{brief_prompt}{identity_block}{engine_block}{registry_top}
{curveball_prompt}

<objective>
Analyze ALL provided data and return a comprehensive structured analysis that will be used
to generate a professional single-page HTML strategic blueprint. Your output must include:
1. Domain identification and program naming
2. Full narrative content for all 6 sections (exec summary, evidence, operations, financials, implementation, appendix)
3. Financial ASSUMPTIONS (salary ranges, cost categories, growth rates) — NOT computed totals
4. A Mermaid flowchart showing the operational workflow
5. Implementation phases with specific actions and timelines
6. Risk register with likelihood/impact/mitigation
Generate content that references SPECIFIC data points with [CHALLENGE_DATA: endpoint-name] tags.
</objective>

<content_quality_requirements>
CRITICAL for top scores — your narrative content must demonstrate DEEP problem understanding:

1. BENCHMARK COMPARISONS: In crisis_body, compare at least TWO local figures to national benchmarks
   from the reference sheet, both as tokens: "<what> of [[<data id>]] — above the national [[ext.<id>]]".
   This shows judges the problem is CONTEXTUALIZED.
   IMPORTANT: Only cite state-level statistics if they appear in the challenge data itself (by their ids).
   Do NOT fabricate or estimate state-level rates — use the national benchmarks from the reference
   sheet instead. An inaccurate state comparison is worse than no comparison.

2. ROOT CAUSE CONTEXT: In each evidence_card body, include ONE sentence explaining the structural/systemic
   cause behind the finding, not just the data point. Form: "This pattern mirrors [a mechanism documented
   nationally], where [what happens] within [a time span] of [the triggering event]." This shows the
   competitor understands the SYSTEM, not just the symptoms.

3. INTERSECTIONAL BARRIER ANALYSIS: In crisis_human, explicitly name the compound barriers the affected
   population faces: income poverty, transportation poverty, language barriers, and the crisis-specific
   barrier — explaining how each amplifies the others. Show you understand how the community EXPERIENCES
   the problem, not just how data DESCRIBES it.

4. NAMED SPECIFICITY: Reference specific facilities, neighborhoods, organizations, routes, and people-groups
   from the challenge data. Never use generic placeholders. When the data names an organization and the
   languages it serves, use those exact names as the data writes them.

5. BOLD FORMATTING FOR SCANNABILITY: In crisis_body, solution_body, and crisis_human, use HTML &lt;strong&gt;
   tags to highlight KEY STATISTICS AND NUMBERS ONLY. A judge scanning the bold text should see a trail of
   data points, not category labels.
   CORRECT (bold the figure's token + its unit): "&lt;strong&gt;[[<id>]] residents&lt;/strong&gt;", "&lt;strong&gt;[[<id>]] poverty rate&lt;/strong&gt;",
   "&lt;strong&gt;[[<id>]] median income&lt;/strong&gt;"
   WRONG (never bold these — they are category labels, not data):
   "&lt;strong&gt;Income poverty&lt;/strong&gt;", "&lt;strong&gt;Transportation poverty&lt;/strong&gt;", "&lt;strong&gt;Language barriers&lt;/strong&gt;",
   "&lt;strong&gt;Pillar 1&lt;/strong&gt;", "&lt;strong&gt;Funding cuts compound local losses&lt;/strong&gt;"
   If you find yourself bolding a phrase with no number in it, STOP — that phrase should not be bold.

6. CONCISENESS: Keep crisis_body to 150-200 words and solution_body to 150-200 words. Every sentence must
   carry a data point or specific claim. Cut adjectives, reduce subordinate clauses, let the bold numbers
   tell the story. The crisis_human paragraph (persona story) should be 3-4 sentences max.

7. POPULATION FUNNEL: In solution_population, include ONE sentence that explicitly bridges all population
   tiers, every count a token: "Of the <strong>[[<total id>]]</strong> people across [[<areas id>]] monitored
   areas, <strong>[[<affected id>]]</strong> live where the crisis hits directly; the program targets
   <strong>[[beneficiaries_y1]]</strong> in Year 1, scaling to [[beneficiaries_y3]] by Year 3."
   Never a share you worked out yourself: name the two counts. This prevents judges from seeing different
   population numbers in different sections and thinking they contradict each other.

8. GEOGRAPHIC CONSISTENCY: Count the unique geographic units the data uses (ZIP codes, neighborhoods,
   census tracts — whatever the endpoint that lists them uses) — that is the number of "monitored areas." Use this
   ONE number consistently everywhere (hero badge, BLUF, population funnel, evidence cards, implementation
   phases). If you reference a subset of priority/critical areas, explicitly label them as a subset: "of the
   [N] monitored areas, [X] are designated critical-priority." Do NOT use different counts in different
   sections — judges will flag any inconsistency.
   IMPORTANT: cite that count by its id — [[<endpoint>.count]] when one endpoint lists one record per area —
   and use the same token in every section. Never count or estimate it yourself.

{cq9}
10. DATA ACCURACY: Before writing any number, verify it against the actual endpoint data.
    - When a field holds a count per record (people affected, beds, calls, visits), cite the total by its id —
      [[sum:<endpoint>.<field>]] over every record, or a named figure from <registry> — and never add numbers
      yourself: Python computes every total, and a judge will verify each against the raw data.
    - Copy every ZIP code, id and name exactly as the endpoint data writes it. Never infer a place from a
      ZIP code's digits, and never transpose digits.

11. METHODOLOGY AND AI PROCESS TEXT: Do NOT state the number of API endpoints in your methodology
    or ai_process output. Python will generate the data inventory sentence (with correct endpoint
    count and per-endpoint record counts) and display it automatically. Your methodology should
    start with the QUALITATIVE analysis: how you cross-referenced the data, what convergence patterns
    you found, what benchmarks you used, and confidence levels. Do NOT write "Analysis integrates N
    endpoints" — that sentence will be generated by the system. In methodology and ai_process, state
    only the methods listed under "What this pipeline actually does" — never more steps, models or tools
    than those, and never call the data live or real-time: it was fetched at run time.

{cq12}
13. PERSONA DEMOGRAPHICS: When writing the persona narrative, cite the SPECIFIC figures the data gives
    (an income, a poverty or coverage rate) for the area the persona lives in, by their data ids
    ([[<endpoint>.<record id>.<field>]]). The persona's area figures must be the same ids the evidence
    cards and population funnel cite.

14. COST-OF-INACTION vs SROI METHODOLOGY: The cost-of-inaction and SROI models may use different
    assumptions (e.g., cost-of-inaction may assume repeated costly events per person while SROI models
    single averted events). If they differ, briefly state why in the cost-of-inaction section, in words
    (its figures by their tokens).
</content_quality_requirements>

<style>Analytical, executive-level. Every section title (summary_title, crisis_title, solution_title, evidence_title, operations_title,
financial_title, plan_title, appendix_title) states its section's finding as a claim with exactly one figure, written as its token
([[<id>]]) — never a label such as "Key Findings".</style>
<tone>Evidence-first, skeptical, civic-appropriate. No marketing hype.</tone>
<audience>City officials and program administrators evaluating {audience_budget}.</audience>

<data>
{data_text}
</data>
{after_data}
Return your analysis as valid JSON matching the MasterAnalysis schema. Include:
- program_name (a name of your own for this program; never one from an example), brand_html (the same name with one word wrapped
  in <em>), city_name, domain_label (2-4 words naming the policy area the data describes; no place names)
- contract_value: {contract_value_line}
- All narrative text fields (bluf_text, crisis_body, solution_body, etc.)
- comparison_rows: 4-5 rows contrasting the current fragmented state with your proposed solution. Each row has dimension (e.g. "Service Coordination"), today (what exists now — fragmented/missing), proposed (what the program enables). Reference specific data points.
- evidence_cards: 4 cards with stat_value (exactly one token: the card's figure, [[<id>]]), stat_class (bad/warn/good), stat_label,
  body (every figure a token), source
- mermaid_code: valid Mermaid flowchart TD syntax.
  DIAGRAM SIZE CONSTRAINT (critical): Maximum 10-12 nodes total. Maximum 3 nodes per horizontal row.
  Group individual partners into single nodes (e.g., "N Partner Sites", not each partner by name).
  Group ZIP codes or areas into tiers (e.g., one node "Critical Areas: [area] · [area] · [area]", not separate nodes).
  Keep node labels SHORT (2-3 words + at most one figure, as a token). Put detailed info in hover tooltips, not labels.
  Every figure in a label or a tooltip is a token ([[<id>]]); Python writes the figure in before the diagram is drawn.
  Good label: "Navigation Hub" — Bad label: "[Program Name] Navigation Hub CRM + Supply Dashboard"
  Good label: "N Partner Sites" — Bad label: "[Partner A] · [Partner B] · [Partner C] · [Partner D]"
  IMPORTANT: For outcome/result nodes (enrollments, avoided crises, follow-ups, benefits), do NOT include
  specific quantity targets in the label — use descriptive text only (e.g., "Benefit Enrollment" not "Benefit
  Enrollment N new/yr"). These quantities are in the SROI derivation table and may be adjusted during
  financial modeling. Data-source quantities from challenge data are fine in labels, as tokens (like "[[sum:<endpoint>.<field>]] calls").
  MUST include these classDef color definitions at the END of the diagram code:
  classDef input fill:#0d3b2e,stroke:#34d399,stroke-width:2px,color:#e9ecff
  classDef hub fill:#1e1145,stroke:#7C3AED,stroke-width:2px,color:#e9ecff
  classDef service fill:#0c2a3a,stroke:#22d3ee,stroke-width:2px,color:#e9ecff
  classDef outcome fill:#2d1f0f,stroke:#f59e0b,stroke-width:2px,color:#e9ecff
  classDef decision fill:#2d1f3d,stroke:#a78bfa,stroke-width:2px,color:#e9ecff
  Apply classes to nodes: data sources/intake nodes use :::input, central hub/dashboard nodes use :::hub, partner sites/distribution/area nodes use :::service, result/follow-up/outcome nodes use :::outcome, decision/triage diamonds use :::decision. Example: A["Intake Records"]:::input --> B{{"Intake Triage"}}:::decision --> C["Navigation Hub"]:::hub
  HOVER TOOLTIPS: After all node definitions and classDefs, add click statements for EVERY node with data-rich tooltip text. Syntax: click nodeId call tip() "tooltip text"
  For each node type, include specific challenge data in the tooltip:
  - Data source nodes: record count + key metric (e.g., click A call tip() "N records across N areas. Top: [area] +X%, [area] +Y%")
  - Partner/service nodes: hours, languages, capacity from the endpoint that lists the sites (e.g., click D call tip() "[days] [hours] | [languages] | [capacity] | [service]")
  - Area nodes: the area's key rates and size (e.g., click Z1 call tip() "[rate name]: X% | [rate name]: Y% | Pop: N")
  - Process nodes: what happens at this step (e.g., click H call tip() "Navigators check needs, documents and access, then match each person to the nearest open site")
  - Outcome nodes: SROI proxy value (e.g., click O1 call tip() "Target: N per year | $X average benefit per {unit_s}")
- phases: 3 implementation phases with HTML content. In phase descriptions, do NOT cite specific SROI-derived
  quantity targets (like "N new enrollments") — these quantities may be adjusted during financial modeling.
  Instead use operational language: "Launch the enrollment campaign at partner intake sites" without a specific number.
  You MAY cite challenge-data quantities and program-level targets, as tokens (like "[[sum:<endpoint>.<field>]] callers" or "[[beneficiaries_y1]] {unit_p}").
- risks: 4 operational risks
- Financial assumptions: staff_roles (list of {{role, salary, count, benefits_pct, salary_ref}} — salary_ref the benchmark id the
  salary rests on, such as ext.wage_chw_median, when one does), costs, revenue sources
{funds_prompt}
- sensitivity_text: A 2-3 sentence stress-test summary on Python's stress case — [[stress_beneficiaries_y1]] {unit_p} served
  ({_IC2_STRESS}) on the same Year-1 expenses ([[total_exp_y1]]) gives [[stress_cpb_y1]] per {unit_s}, against the base case's
  [[cpb_y1]]. Cite those tokens; compute no stress figure yourself.
{sroi_ask}

{fc_block}
"""

    # Stream the response to prevent idle connection timeout (~365s disconnect).
    # Streams plain JSON text; parsing and Pydantic validation follow below.
    sch_shown = (schema_shown(MasterAnalysis.model_json_schema(), drop=("discount_rate",) if on4 else ())   # IC-4 Phase 7
                 if on else MasterAnalysis.model_json_schema())          # (review b22, D6): the rate leaves the schema
    if NR.ic5_prompt():        # BUILD IC-5 (D15): the diagram as data — its form in mermaid_code's place, and D15's instruction
        sch_shown, prompt = diagram_schema_shown(sch_shown), diagram_ask_words(prompt)
    schema_json = json.dumps(sch_shown, indent=2)
    full_prompt = prompt + f"""

CRITICAL OUTPUT FORMAT: Return your COMPLETE analysis as a single valid JSON object.
The JSON must conform to this schema:

{schema_json}

Return ONLY the raw JSON object. No markdown code fences. No explanation before or after the JSON.
Start your response with {{ and end with }}."""

    full_prompt = ic3_words(full_prompt, "master")          # BUILD IC-3 (D7): the fixed text's numbers (the switch on)
    console.print("    [dim]Streaming from Opus (this takes several minutes)...[/dim]")
    raw_response = call_claude_streaming(full_prompt, model=OPUS)  # BUILD IC-6 (D1): its profile, "oracle/master" (IC-5: 52,000 at high)
    master_reply = keep_reply("master_reply", raw_response) if curveball_ctx else None   # P0.7 r2 (E3 d): Block 2's full and no-base paths

    # BUILD IC-7 (D2, D3): the reply admitted — its normalizers counted, strict validation item by item — or one second attempt that
    # carries the first's errors (master_with_second_attempt); IC-6's parse raised on any invalid field
    p_, raw_response = master_with_second_attempt(full_prompt, raw_response, pin, curveball_ctx)
    if p_.get("second") and curveball_ctx:
        master_reply = keep_reply("master_reply", raw_response)       # the reply used
    result, brief_raw, cb_extra, diag_raw = p_["result"], p_["brief_raw"], p_["cb_extra"], p_["diag_raw"]
    inputs_given, inputs_notes = p_["inputs_given"], p_["inputs_notes"]

    out = result.model_dump()
    out["inputs_given"], out["inputs_notes"] = inputs_given, inputs_notes   # BUILD B3 (D1): the registry names each default as Python's
    if NR.ic5_prompt():         # BUILD IC-5 (D15): the diagram as data, checked; with it the reply's own Mermaid code is not used
        dd_, dprobs_ = diagram_data(diag_raw)
        for w_, why_ in dprobs_:
            NR.say(f"    diagram: left out {w_} — {why_}")
        if dd_:
            if (out.get("mermaid_code") or "").strip():
                NR.say("    diagram: the reply's Mermaid code is not used — Python writes the diagram from its nodes and edges")
            out["diagram"], out["mermaid_code"] = dd_, ""
        elif (out.get("mermaid_code") or "").strip():
            NR.say("    diagram: the reply names no nodes and edges — its Mermaid code is drawn as the earlier builds drew it")
    if on4:            # BUILD IC-4 (D14): an outcome valued by a figure of the data, read on this run's data
        out["sroi_data_values"] = sroi_data_values(out, reg_now)
    # BUILD IC-4 (D1): the items' ids at parse — Block 1's order; the full path at Block 2 carries Block 1's when its base is readable
    with_item_ids(out, (load_block1_base(F.get("challenge") or os.environ.get("NAIPC_CHALLENGE", "").strip() or None)[0] or {}).get("ma")
                  if curveball_ctx else None)
    # P0.6 (D3): the program identity, written every run right after the analysis parses (Block 1 archives it with the checkpoints)
    src = pin["source"] if pin else "model"
    why = NR.write_identity(CHECKPOINT_DIR / NR.IDENTITY_FILE, F["challenge"] or os.environ.get("NAIPC_CHALLENGE", "").strip() or None,
                            NR.run_id(), out["program_name"], out["brand_html"], out.get("domain_label"), src)
    NR.say(f"    identity: {out['program_name']} ({src}) written to checkpoints/{NR.IDENTITY_FILE}" if why is None else f"    identity: {why}")
    if brief_doc:
        out["brief_answers"] = brief_answers_for(brief_doc, brief_raw, out, facts_now)  # kept on the checkpoint
    if curveball_ctx:   # P0.7 (D3): the full path ("full" or "no base: <reason>") records its own section parts
        refs = NR.curveball_refs(changes) if changes else {"announcement": "announcement"}
        wc, d1 = take_what_changed(cb_extra, refs, "what changed")                 # P0.7 r2 (E2, E3): the same caps and records as the update's
        summ, d2 = take_text(cb_extra, "response_summary", UPDATE_SUMMARY_CHARS, "the response summary")
        tests, d3 = take_entries(cb_extra, "test_answers", UPDATE_TESTS_MAX, {"test": UPDATE_TEST_CHARS, "answer": UPDATE_ANSWER_CHARS},
                                 "the curveball tests' answers", need={"answer": "no answer given"})
        for d in d1 + d2 + d3:
            NR.say(f"    curveball: not used — {d['field']}: {d['why']}")
        out["curveball_update"] = {"status": cb_full_reason or "full", "what_changed": wc, "response_summary": summ, "test_answers": tests,
                                   "derived_requests": [], "amended": [], "dropped": d1 + d2 + d3, "assumption_changes": [],
                                   "brief_answers_changed": [], "reply_file": master_reply}
    return out


# ─────────────────────────────────────────────────────────────────────
# STAGE 3: FINANCIAL MODEL — Python-computed arithmetic
# Per Implementation Guide §4: LLMs reason, Python computes
# ─────────────────────────────────────────────────────────────────────
def count_step(n) -> int:
    """P0.6 (D5): the rounding step for beneficiary counts — 100 at 12,000 (P0.3's fixed step), 10 from 1,000, 1 under 1,000."""
    return 10 ** max(0, len(str(abs(int(n)))) - 3)


def round_count(x, step: int) -> int:
    """x rounded to `step` the way P0.3 rounded to 100 (round(x, -2)), and never below 1: no scenario can reach 0 beneficiaries."""
    return max(1, int(round(x, -(len(str(step)) - 1))))


def nice_step(x) -> float:
    """The largest 1, 2, 2.5 or 5 × 10^k not above x (at least 1)."""
    import math
    if not x or x < 1:
        return 1
    e = 10 ** math.floor(math.log10(x))
    return max(m * e for m in (1, 2, 2.5, 5) if m * e <= x)


def sim_range(base, lo: float, hi: float, frac: float):
    """(min, max, step) for a simulator slider around `base`: step = nice_step(base × frac), a whole number (2.5 → 2: the slider counts
    people and dollars); min and max = base × lo and base × hi rounded to whole steps COUNTED FROM THE BASE, so the base is on the
    slider's grid — a browser snaps a range input's value to min + k × step, and a base off that grid opened the simulator away from the
    plan ($430,000 shown as $440,000 — P0.6 Phase 5, t_review6 RV-1). Min ≥ one step; max ≥ base + one step. P0.6 (D5)."""
    st = int(nice_step(base * frac))
    k_lo = max(0, min(int(round(base * (1 - lo) / st)), int(base // st) - 1))
    k_hi = max(1, int(round(base * (hi - 1) / st)))
    return int(base - k_lo * st), int(base + k_hi * st), st


CONST_SOURCE = "a setting of this model, not a measured figure"     # BUILD IC-4 (D7): the source line of Python's own settings, one form
# BUILD B3 (D1): Python's own constants of the financial model, in one table — (value, format, why); the registry lists each MODELED
FIN_CONSTANTS = {
    "personnel_growth_y2": (1.18, "num", "personnel cost in Year 2 = Year 1 × 1.18 (raises and added staff as the program grows) — a Python constant, not from the data"),
    "personnel_growth_y3": (1.33, "num", "personnel cost in Year 3 = Year 1 × 1.33 — the same basis"),
    "ops_growth_y2": (1.12, "num", "operations cost in Year 2 = Year 1 × 1.12 (more people served) — a Python constant"),
    "ops_growth_y3": (1.25, "num", "operations cost in Year 3 = Year 1 × 1.25 — the same basis"),
    "tech_share_y2": (0.67, "num", "technology cost in Year 2 = Year 1 × 0.67 (the build falls to upkeep) — a Python constant"),
    "tech_share_y3": (0.56, "num", "technology cost in Year 3 = Year 1 × 0.56 — the same basis"),
    "outreach_share_y2": (0.75, "num", "outreach in Year 2 = Year 1 × 0.75 (the launch campaign tapers) — a Python constant"),
    "outreach_share_y3": (0.60, "num", "outreach in Year 3 = Year 1 × 0.60 — the same basis"),
    "personnel_cap": (0.48, "pct", "personnel may take at most 48% of the budget less the marketing reserve, so the base case keeps a surplus — a Python guardrail"),
    "bull_revenue_y1": (1.30, "num", "the optimistic case's Year-1 revenue = base × 1.30 (supplemental grants, partnerships) — a Python scenario setting"),
    "bull_revenue_y2": (1.30, "num", "the optimistic case's Year-2 revenue = base × 1.30 — the same basis"),
    "bull_revenue_y3": (1.26, "num", "the optimistic case's Year-3 revenue = base × 1.26 — the same basis"),
    "bull_expense_later": (0.95, "num", "the optimistic case's Years 2–3 expenses = base × 0.95 (efficiency); Year 1 unchanged — a Python scenario setting"),
    "bull_reach": (1.3, "num", "the optimistic case's people served = base × 1.3 — a Python scenario setting"),
    "bear_revenue_y3": (0.95, "num", "the conservative case's Year-3 revenue = the contract × 0.95 (funding uncertainty); Years 1–2 the contract alone"),
    "bear_expense_y2": (0.92, "num", "the conservative case's Year-2 expenses = base × 0.92 (a smaller scope) — a Python scenario setting"),
    "bear_expense_y3": (0.86, "num", "the conservative case's Year-3 expenses = base × 0.86 — the same basis"),
    "bear_reach": (0.66, "num", "the conservative case's people served = base × 0.66 — a Python scenario setting"),
    "stress_reach": (0.8, "num", "the stress case: Year-1 people served × 0.8 (20% fewer reached), the same Year-1 expenses — a Python setting"),
    "coi_er_rate": (0.03, "pct", "the cost of inaction: 3% of the people served each year have one avoidable emergency room visit — a Python constant since P0.3, not from the data"),
}
CONST_LABELS = {   # B3 (Phase 4, review d F9): each constant's label in words (the registry, the honesty panel and the grid show it)
    **{f"{k}_{y}": f"Year-{y[1]} {w}, a multiple of Year 1" for k, w in (("personnel_growth", "personnel cost"), ("ops_growth", "operations cost"),
                                                                      ("tech_share", "technology cost"), ("outreach_share", "outreach cost"))
       for y in ("y2", "y3")},
    "personnel_cap": "the personnel cap, a share of the budget less the marketing reserve",
    **{f"bull_revenue_y{n}": f"the optimistic case's Year-{n} revenue, a multiple of the base" for n in (1, 2, 3)},
    "bull_expense_later": "the optimistic case's Years 2–3 expenses, a multiple of the base",
    "bull_reach": "the optimistic case's people served, a multiple of the base",
    "bear_revenue_y3": "the conservative case's Year-3 revenue, a multiple of the contract",
    **{f"bear_expense_y{n}": f"the conservative case's Year-{n} expenses, a multiple of the base" for n in (2, 3)},
    "bear_reach": "the conservative case's people served, a multiple of the base",
    "stress_reach": "the stress case's Year-1 people served, a multiple of the base",
    "coi_er_rate": "the share of people served with one avoidable emergency room visit a year",
}

# BUILD IC-3 (D7, ledger step 52): the financial constraints the master prompt states are Python's numbers — each a registry figure
# (build_registry, step 1: the ids exist whatever the switch, as prio. does), printed as `[[id]] = value` with the switch on. The switch
# off prints IC-2's sentences, frozen as IC-2 wrote them (stage_master_analysis). These tables feed the ids only.
PLAN_YEARS = 3               # the years the P&L computes (stage_financial_model's lists of three)
PROMPT_COST_SHARES = (       # (key, the input, low, high) — a cost line's guided share of cap.base
    ("ops", "ops_cost_y1", 0.12, 0.18), ("tech", "tech_cost_y1", 0.06, 0.10), ("outreach", "outreach_cost_y1", 0.03, 0.05))
COST_FIELD_WORDS = {"ops_cost_y1": "the Year-1 operations cost", "tech_cost_y1": "the Year-1 technology cost",     # BUILD IC-5 (D4): an
                    "outreach_cost_y1": "the Year-1 outreach cost"}       # input in a figure's label by its words, never its key
PROMPT_SIZING = (            # (key, what, low, high) — Python's salary guide for sizing the team
    ("director", "a Program Director's salary", 75000, 78000),
    ("frontline", "a frontline staff member's salary (community health worker, navigator, coordinator), each", 45000, 51000),
    ("admin", "an admin or data support salary", 38000, 45000))
PROMPT_BENEFITS = (0.25, 0.30)
CONSTRAINT_PREFIXES = ("cap.", "guide.", "plan.")    # their ids: offered in <financial_constraints>, never a named figure or Block 1's


def constraint_figures(F):
    """BUILD IC-3 (D7): [dict(id, value, kind, fmt, unit, label, …)] — the base of the cost limits and the personnel cap; each cost
    line's guided share (and, when the budget is known, its amount); Python's salary and benefits guide; the P&L's years. The outreach
    share only when the brief reserves no marketing budget (else outreach is the reserve). No label holds a number."""
    budget, _src = budget_in_use(F)
    reserve = F["marketing"]
    cap = FIN_CONSTANTS["personnel_cap"][0]
    g = "Python's guidance in the master prompt"
    out = [dict(id="plan.years", value=PLAN_YEARS, kind="MODELED", fmt="count", unit="years", label="the years the P&L covers",
                source="Python's financial model")]
    base = None
    if budget is not None:
        base = budget - reserve if reserve and reserve < budget else budget
        out.append(dict(id="cap.base", value=base, kind="DERIVED", fmt="money_short", unit="USD",
                        label="the base of the cost limits: " + ("the budget less the brief's marketing reserve" if base != budget else "the budget"),
                        formula="budget − reserve" if base != budget else "budget", source="Python's financial constraints"))
    out.append(dict(id="cap.personnel_share", value=cap, kind="MODELED", fmt="pct", label="the personnel cap, a share of the base of the cost limits",
                    source=CONST_SOURCE, assumption=FIN_CONSTANTS["personnel_cap"][2]))
    if base is not None:
        out.append(dict(id="cap.personnel_y1", value=int(base * cap), kind="MODELED", fmt="money", unit="USD", label="the Year-1 personnel cap",
                        formula="the base of the cost limits × the personnel cap's share", source="Python's financial constraints"))
    for key, field, lo, hi in PROMPT_COST_SHARES:
        if key == "outreach" and reserve:
            continue
        for end, v in (("min", lo), ("max", hi)):
            w = "low" if end == "min" else "high"
            out.append(dict(id=f"cap.{key}_{end}_share", value=v, kind="MODELED", fmt="pct",
                            label=f"the {w} end of the guided share for {COST_FIELD_WORDS.get(field, field)}", source=g,
                            prompt_label=f"the {w} end of {field}'s guided share"))    # BUILD IC-5 (D4c): a request's words stay IC-4's
            if base is not None:
                out.append(dict(id=f"cap.{key}_{end}", value=round(base * v), kind="MODELED", fmt="money_short", unit="USD",
                                label=f"the {w} end of the guided amount for {COST_FIELD_WORDS.get(field, field)}",
                                formula=f"the base of the cost limits × the {w} end of the guided share for {COST_FIELD_WORDS.get(field, field)}", source=g,
                                prompt_label=f"the {w} end of {field}'s guided amount"))    # BUILD IC-5 (D4c)
    for key, what, lo, hi in PROMPT_SIZING:
        for end, v in (("min", lo), ("max", hi)):
            out.append(dict(id=f"guide.{key}_{end}", value=v, kind="MODELED", fmt="money_short", unit="USD",
                            label=f"the {'low' if end == 'min' else 'high'} end of Python's guide for {what}", source=g))
    for end, v in zip(("min", "max"), PROMPT_BENEFITS):
        out.append(dict(id=f"guide.benefits_{end}", value=v, kind="MODELED", fmt="pct",
                        label=f"the {'low' if end == 'min' else 'high'} end of Python's guide for benefits, a share of salary", source=g))
    return out


def schema_shown(schema, drop=()):
    """BUILD IC-3 (D7): the schema as the master request shows it with the switch on — every numeric "default" left out. A default is
    Python's fallback for an input the reply leaves out or writes badly (fin_inputs_values names each); shown, it is a number of Python's
    the model can restate and cannot cite (ops_cost_y1's 80000 also contradicted the guided share). The resolver's ids for the inputs stay
    (FIN_INPUTS). A copy: the model's schema is unchanged."""
    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()
                    if not (k == "default" and isinstance(v, (int, float)) and not isinstance(v, bool))}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o
    out = walk(schema)
    for f in drop:              # IC-4 Phase 7 (review b22): a method constant of Python's is not a field the model is shown
        (out.get("properties") or {}).pop(f, None)
        if isinstance(out.get("required"), list) and f in out["required"]:
            out["required"] = [x for x in out["required"] if x != f]
    return out


def constraints_block_ic3(reg, F) -> str:
    """BUILD IC-3 (D7): the master prompt's <financial_constraints> with the switch on — Python's figures listed as `[[id]] = value —
    what it is`, the rules citing them by id; the numbers an instruction needs (the role-cost formula's 1) are in the instruction-number
    ledger. Every id comes from constraint_figures (registered by build_registry)."""
    figs = [f for f in constraint_figures(F) if reg.get(f["id"])]
    has = {f["id"] for f in figs}
    reserve = F["marketing"]
    base = "[[cap.base]]" if "cap.base" in has else ("your proposed contract_value" + (" minus the brief's marketing reserve" if reserve else ""))
    cap = ("[[cap.personnel_y1]] ([[cap.personnel_share]] of [[cap.base]])" if "cap.personnel_y1" in has
           else f"[[cap.personnel_share]] of {base}")

    def share(key):
        s = f"[[cap.{key}_min_share]] to [[cap.{key}_max_share]]"
        return s + (f" ([[cap.{key}_min]] to [[cap.{key}_max]])" if f"cap.{key}_min" in has else "")
    outreach = ("; outreach_cost_y1 is the brief's marketing reserve, [[reserve]] — Python sets it" if reserve
                else f", outreach_cost_y1 {share('outreach')}")
    lines = ["<financial_constraints>",
             "CRITICAL — your staffing assumptions MUST fit within these bounds or the financial model will show deficits. Python's figures:"]
    lines += [f"[[{f['id']}]] = {reg.get(f['id'])['shown']} — {f.get('prompt_label') or reg.get(f['id'])['label']}" for f in figs]   # BUILD IC-5 (D4c)
    lines += [f"1. Total Year 1 personnel costs (salary × count × (1 + benefits_pct)) must NOT exceed {cap}.",
              "   Size the team to the personnel cap: a Program Director ([[guide.director_min]] to [[guide.director_max]]), frontline staff like "
              "CHWs/navigators/coordinators ([[guide.frontline_min]] to [[guide.frontline_max]] each) and admin/data support ([[guide.admin_min]] "
              "to [[guide.admin_max]]) only as the cap allows. Benefits at [[guide.benefits_min]] to [[guide.benefits_max]].",
              f"2. As shares of {base}: ops_cost_y1 {share('ops')}, tech_cost_y1 {share('tech')}{outreach}.",
              f"3. target_beneficiaries_y1: derive it from the data — the {F['unit_plural']} this program can actually reach in Year 1 with this "
              "budget — counted in the brief's unit.",
              "4. The base case P&L MUST show positive surplus in every year. If your personnel assumptions are too high, REDUCE staff count — "
              "do NOT exceed the personnel cap.",
              f"5. In renewal_text and sustainability projections, cite the BASE CASE Year 3 cost per {F['unit_singular']} from the P&L as",
              "   [[cpb_y3]] — never a more optimistic figure of your own.",
              "6. In Key Assumptions, an outcome rate applies to the people the program serves each year ([[beneficiaries_y1]] in",
              "   Year 1), NOT to the total affected population — the SROI counts outcomes for people the program actually SERVES. Write",
              "   a rate of your own as a number of your own (<number_rules>); Python computes every count from it.",   # D10: the form once
              "</financial_constraints>"]
    return "\n".join(lines)


def context_lines_ic3(reg, F, curveball=False, b1_budget=None) -> dict:
    """BUILD IC-3 (D7): the <context> lines that state the budget, the reserve and the pilot with the switch on — each figure as
    `[[id]] = value`; the brief's own sentence is left to the <brief> block that prints it. Keys as stage_master_analysis's `lines`."""
    budget, bsrc = budget_in_use(F)
    unit_p, unit_s = F["unit_plural"], F["unit_singular"]
    have = lambda i: reg.get(i) is not None                      # noqa: E731
    pair = lambda i: f"[[{i}]] = {reg.get(i)['shown']}"          # noqa: E731
    out = {}
    if budget is not None and have("budget"):
        out["budget_line"] = ("You are preparing a government service contract proposal. Its budget: " + pair("budget") + " ("
                              + ("the brief's own figure" if bsrc == "brief" else "a budget override") + ").")
    else:
        out["budget_line"] = ("You are preparing a government service contract proposal. The brief states no contract value — propose the "
                              "budget your design needs as contract_value, in whole dollars; it will be shown as your estimate.")
    out["reserve_line"] = (f"\nThe brief reserves {pair('reserve')} for marketing, trust-building and adoption: Python sets outreach_cost_y1 to "
                           "exactly [[reserve]]; the other costs and the personnel cap are sized to the budget minus that reserve."
                           if F["marketing"] and have("reserve") else "")
    out["pilot_line"] = (f"\nThe pilot's length in days: {pair('pilot_days')}; timelines and phases fit inside it." if F["pilot_days"] and have("pilot_days")
                         else "\nThe brief states no pilot length: any timeline you give is your own proposal — say so where you give one.")
    out["unit_line"] = (f"\nCount the people the program serves in the brief's unit: {unit_p} (a {unit_s})." if F["unit_stated"]
                        else "\nThe brief names no beneficiary unit: count 'beneficiaries' and say in the methodology what one is.")
    out["budget_note"] = (f"\nIMPORTANT: The contract budget is now {pair('budget')} (an override of the brief's figure). All financial "
                          "references must use this figure." if curveball and bsrc == "override" and have("budget") else "")
    out["block1_budget_line"] = (f"\nBlock 1's analysis was written for a budget of {pair('b1.budget')}; the budget in use is now {pair('budget')}: "
                                 "amend every field that states Block 1's budget (because: \"announcement\")."
                                 if b1_budget is not None and have("b1.budget") and have("budget") else None)
    return out


def _unit_words(text, unit_p):
    """A label in the brief's unit: "people served" → "<the brief's plural> served" (B3, Phase 4, review d F9)."""
    return str(text).replace("people served", f"{unit_p} served") if unit_p else str(text)


FIN_ROLE_DEFAULTS = {"salary": (50000, "money"), "count": (1, "count"), "benefits_pct": (0.30, "pct")}   # a role's key not given → named
FIN_INPUTS = (   # the master analysis's financial inputs: (field, format, what it is) — registered MODELED: the model's, or Python's default
    ("target_beneficiaries_y1", "count", "the Year-1 target of people served"), ("growth_rate_y2", "pct", "growth in people served, Year 2"),
    ("growth_rate_y3", "pct", "growth in people served, Year 3"), ("ops_cost_y1", "money", "Year-1 operations cost"),
    ("tech_cost_y1", "money", "Year-1 technology cost"), ("outreach_cost_y1", "money", "Year-1 outreach cost"),
    ("supplemental_grants_y2", "money", "supplemental grants, Year 2"), ("supplemental_grants_y3", "money", "supplemental grants, Year 3"),
    ("earned_revenue_y3", "money", "earned revenue, Year 3"), ("indirect_rate", "pct", "the indirect cost rate"),
    ("deadweight", "pct", "SROI deadweight: what would have happened anyway"), ("attribution", "pct", "SROI attribution: the share others caused"),
    ("displacement", "pct", "SROI displacement: outcomes moved, not created"), ("drop_off_rate", "pct", "SROI drop-off: how much an outcome fades each year"),
    ("discount_rate", "pct", "the discount rate"))
SROI_MAX_DURATION = 10   # years an outcome may be counted (a longer stated duration is cut to this, named)
# BUILD IC-4 (D6): each benchmark's scope, population, place and year, from its own words and source line; an outcome's unit value binds
# only a money figure per unit — an average or a marginal cost (UNIT_SCOPES)
BENCHMARK_SCOPE = {
    "ext.us_food_insecurity_2023": ("a rate", "U.S. households", "the United States", 2023),
    "ext.us_poverty_rate_2023": ("a rate", "people in the U.S.", "the United States", 2023),
    "ext.us_renters_cost_burdened_2023": ("a rate", "U.S. renter households", "the United States", 2023),
    "ext.us_uninsured_rate_2023": ("a rate", "people in the U.S.", "the United States", 2023),
    "ext.us_uninsured_people_2023": ("a count", "people in the U.S.", "the United States", 2023),
    "ext.er_visit_cost": ("an average cost", "treat-and-release emergency department visits", "the United States", 2021),
    "ext.wage_chw_median": ("a median yearly pay", "community health workers", "the United States", 2025),
    "ext.wage_social_worker_median": ("a median yearly pay", "social workers", "the United States", 2025),
    "ext.wage_program_director_median": ("a median yearly pay", "social and community service managers", "the United States", 2025),
    "ext.indirect_deminimis_rate": ("a rate", "federal awards (modified total direct costs)", "the United States", None),
    "ext.us_people_with_sud_2023": ("a count", "people in the U.S.", "the United States", 2023),
}
UNIT_SCOPES = ("an average cost", "a marginal cost")
UNIT_COST_CAUTION = "an average cost: the SROI Guide (p. 51) — \u201cWhen you use unit costs be careful not to overstate the savings.\u201d"
# BUILD IC-4 (D6, Q-RATE): the analysis's class and its discount rate — a method constant of Python's with its source (the model's value is
# named and not used) — and the sensitivity pair beside it. Each sentence checked on 11/12 (BUILD_PROMPT_IC4 §8).
SROI_METHOD = "a social return on investment, by the SROI Guide's (2012) method"
SROI_RATE = 0.035
SROI_RATE_SOURCE = "the SROI Guide's rate, HM Treasury's Green Book public-sector rate"
SROI_RATE_SENS = (("discount_rate_low", 0.03), ("discount_rate_high", 0.07))
SROI_SENS_SOURCE = "the pair OMB Circular A-4 (2003) asks US federal regulatory analysis to report; not binding on a city program"
SROI_SENS_IDS = frozenset([rid for rid, _r in SROI_RATE_SENS] + [f"sroi_at_{round(r * 100)}pct" for _i, r in SROI_RATE_SENS])   # page figures


def _given(ma, key):
    """How an input reached the model: 'given' (the model's), 'default' (Python's — the model gave none) or the reason a value the model
    wrote was not used. BUILD B3 (D1): recorded when the analysis parsed (`inputs_given`, `inputs_notes`); a base written before B3
    records nothing — then 'given'."""
    g = ma.get("inputs_given")
    if not isinstance(g, list) or key in g:
        return "given"
    return (ma.get("inputs_notes") or {}).get(key) or "default"


def sroi_compute(rows, people, total_exp, net_adj, dr, drop):
    """D1 (B3): the SROI's present values — each year's outcomes from that year's people served (rate × people), each counted over its
    duration with drop-off from its second year, discounted from the year it occurs; the cost side the P&L's three years, discounted
    the same way. Returns (pv_benefits, pv_cost, last_year)."""
    pv_b, last = 0.0, 0
    for r in rows:
        for y, n in enumerate(people[:3]):
            for k in range(r["duration"]):
                pv_b += r["unit_value"] * r["rate"] * n * net_adj * (1 - drop) ** k / (1 + dr) ** (y + k + 1)
                last = max(last, y + k + 1)
    pv_c = sum(total_exp[y] / (1 + dr) ** (y + 1) for y in range(min(3, len(total_exp))))
    return pv_b, pv_c, last


@stage("financial_model", depends_on=["master_analysis"])
def stage_financial_model(deps: dict) -> dict:
    """Compute all financial projections from master analysis assumptions."""
    ma = deps["master_analysis"]
    C = {k: v[0] for k, v in FIN_CONSTANTS.items()}
    items = []                                   # BUILD B3 (D1): what the registry lists of this model (MODELED inputs, constants, figures)

    # --- The budget in use (P0.6, D2): --budget → the brief's contract → the model's proposal → the Year-1 cost plan ---
    F = NR.facts(_brief_contract())
    reserve = F["marketing"]
    budget, budget_src = budget_in_use(F)
    model_cv = money_value(ma.get("contract_value"))
    if budget_src == "brief" and model_cv is not None and model_cv != budget:
        NR.say(f"    budget: the model wrote contract_value {model_cv:,} — ignored; the brief states {budget:,}")
    if budget is None and model_cv:
        budget, budget_src = model_cv, "proposed"

    # --- Personnel ---
    # BUILD B3 (D1): each role's numbers read by the one parser ("$45K" → 45000; "30" benefits → 30%); a key the model left out, or one
    # that does not parse, is Python's default — named in the console and in the registry, never silent
    staff = []
    for i, r in enumerate(x for x in (ma.get("staff_roles") or []) if isinstance(x, dict)):
        role = {"role": REG.fit_text(re.sub(r"\s+", " ", str(r.get("role") or r.get("title") or "Staff role")).strip(), "oracle.financial.role"), "given": [],
                "salary_ref": r.get("salary_ref") if isinstance(r.get("salary_ref"), str) else None}
        for k, (dv, fmt_) in FIN_ROLE_DEFAULTS.items():
            v = REG.parse_number(r.get(k))
            if v is not None and k == "benefits_pct" and 1 < v <= 100:
                v = v / 100                                   # "30" written for 30%
            if v is None or v < 0:
                NR.say(f"    staff role {i + 1} ({role['role'][:40]}): {k} " + ("not given" if r.get(k) is None else f"{str(r.get(k))[:30]!r} is not a number")
                       + f" — Python's default {REG.fmt_value(dv, fmt_)}")
                v = dv
            else:
                role["given"].append(k)
            role[k] = int(v) if k != "benefits_pct" and float(v).is_integer() else v
        staff.append(role)
    personnel_y1 = sum(r["salary"] * r["count"] * (1 + r["benefits_pct"]) for r in staff)
    # Guardrail: cap personnel at 48% of the budget in use to ensure positive surplus (P0.6, D5: of the budget minus the brief's
    # marketing reserve when one is stated — the reserve is spent on outreach; no cap when the budget is the cost plan itself).
    if budget is not None:
        cap_base = budget - reserve if reserve and reserve < budget else budget
        max_personnel = int(cap_base * C["personnel_cap"])
        if personnel_y1 > max_personnel:
            console.print(f"    [yellow]⚠ Personnel ${personnel_y1:,} exceeds cap ${max_personnel:,} — scaling down[/yellow]")
            scale_factor = max_personnel / personnel_y1
            personnel_y1 = max_personnel
        else:
            scale_factor = 1.0
    else:
        scale_factor = 1.0
    # Growth: personnel grows ~15-18% year-over-year
    personnel = [round(personnel_y1), round(personnel_y1 * C["personnel_growth_y2"]), round(personnel_y1 * C["personnel_growth_y3"])]

    # --- Other costs ---
    ops = [ma.get("ops_cost_y1", 80000)]
    ops.append(round(ops[0] * C["ops_growth_y2"]))
    ops.append(round(ops[0] * C["ops_growth_y3"]))

    tech = [ma.get("tech_cost_y1", 45000)]
    tech.append(round(tech[0] * C["tech_share_y2"]))
    tech.append(round(tech[0] * C["tech_share_y3"]))

    # P0.6 (D5): the brief's marketing reserve IS Year-1 outreach (the root cause of Wichita's missing $100K line, 9/27)
    outreach = [reserve if reserve else ma.get("outreach_cost_y1", 20000)]
    if reserve and ma.get("outreach_cost_y1") not in (None, reserve):
        NR.say(f"    outreach: Year 1 = the brief's marketing reserve {reserve:,} (the model wrote {ma.get('outreach_cost_y1'):,})")
    outreach.append(round(outreach[0] * C["outreach_share_y2"]))
    outreach.append(round(outreach[0] * C["outreach_share_y3"]))

    # --- Direct costs + indirect ---
    rate = ma.get("indirect_rate", 0.15)
    direct = [personnel[i] + ops[i] + tech[i] + outreach[i] for i in range(3)]
    indirect = [round(d * rate) for d in direct]
    total_exp = [direct[i] + indirect[i] for i in range(3)]

    # --- Revenue ---
    if budget is None:   # P0.6 (D2): no override, no figure in the brief, none from the model → the Year-1 cost plan, rounded up
        budget, budget_src = -(-total_exp[0] // 10000) * 10000, "cost_plan"
        NR.say("    budget: the model proposed no contract_value — the Year-1 cost plan rounded up to $10,000 is shown as the proposed "
               "budget; the 48% personnel cap is not applied (there is no budget to cap against)")
    contract = budget
    NR.say(f"    budget: ${contract:,} — {budget_src}")
    grants = [0, ma.get("supplemental_grants_y2", 75000), ma.get("supplemental_grants_y3", 125000)]
    earned = [0, 0, ma.get("earned_revenue_y3", 50000)]
    total_rev = [contract + grants[i] + earned[i] for i in range(3)]

    # --- Surplus ---
    surplus = [total_rev[i] - total_exp[i] for i in range(3)]

    # --- Beneficiaries (P0.6, D5: rounded with a step that scales with the target — 100 at 12,000, 1 under 1,000 — never to 0) ---
    b1 = max(1, int(ma.get("target_beneficiaries_y1", 10000) or 1))
    gr2 = ma.get("growth_rate_y2", 0.20)
    gr3 = ma.get("growth_rate_y3", 0.18)
    step = count_step(b1)
    beneficiaries = [b1,
                     round_count(b1 * (1 + gr2), step),
                     round_count(b1 * (1 + gr2) * (1 + gr3), step)]

    # --- Cost per beneficiary ---
    cpb = [round(total_exp[i] / beneficiaries[i]) for i in range(3)]

    # --- SROI (D1, BUILD B3): the computed ratio, always with its derivation — no rescale to 4.0, no 3.2 stand-in. Each outcome row: a
    # unit value (a benchmark's when the model names one by unit_value_ref) × a rate per person served (or a Year-1 quantity read as
    # one) × that year's people served, over the outcome's duration with drop-off from its second year (the guide: drop-off "is only
    # calculated for outcomes that last more than one year"), discounted; the cost side the P&L's three years ---
    net_adj = (1 - ma.get("deadweight", 0.15)) * (1 - ma.get("attribution", 0.10)) * (1 - ma.get("displacement", 0.05))
    dr = SROI_RATE                         # BUILD IC-4 (D6): a method constant with its source, never the model's value
    dr_model = ma.get("discount_rate")
    drop = ma.get("drop_off_rate", 0.10)
    if ma.get("contract_years") not in (None, 3):
        NR.say(f"    SROI: contract_years {ma.get('contract_years')!r} is not used — the P&L and the SROI count its three years")
    bench = {b[0]: b for b in BENCHMARKS}
    sroi_in, sroi_notes = [], []
    if isinstance(dr_model, (int, float)) and not isinstance(dr_model, bool) and abs(dr_model - SROI_RATE) > 1e-12:
        sroi_notes.append(f"the model wrote a discount rate of {REG.fmt_value(dr_model, 'pct')} — not used: the rate is {SROI_RATE_SOURCE}")
    # BUILD IC-4 (D6, D14): with the switch on, one materiality list — every outcome the analysis proposed, with its status, and each core
    # outcome of the contract it did not name; only the valued ones are counted (the SROI, as before, and the cost of inaction)
    on4 = ic4_prompt()
    core = contract_outcomes(_brief_contract()) if on4 else []
    core_ids, mat_rows, unstated = {c["id"] for c in core}, [], 0
    data_vals = (ma.get("sroi_data_values") or {}) if on4 and isinstance(ma.get("sroi_data_values"), dict) else {}
    for i, o in enumerate(ma.get("sroi_outcomes") or []):
        if not isinstance(o, dict):
            sroi_notes.append(f"outcome {i + 1}: not an object — not used")
            continue
        cat = REG.fit_text(re.sub(r"\s+", " ", str(o.get("category") or "Outcome")).strip(), "oracle.financial.category")
        catw = REG.out_text(cat, "plain")      # BUILD IC-5 (D3, class T): the model's words in Python's notes, as words
        if on4:
            m_, cid = material_of(o), str(o.get("core") or "").strip()
            if cid and cid not in core_ids:
                sroi_notes.append(f"outcome {i + 1} ({catw}): core {cid[:40]!r} is none of the contract's core outcomes — listed as one of its own")
                cid = ""
            mrow = {"i": i, "outcome": cat, "core": cid or None, "why": REG.fit_text(re.sub(r"\s+", " ", str(o.get("why") or "")).strip(), "oracle.financial.why")}
            unstated += m_ is None
            if m_ is False and cid:     # IC-4 Phase 7 (review b20, D6): a core outcome of the contract is material whatever the reply says
                mat_rows.append(dict(mrow, status="material, not valued", by="py",
                                     why="a core outcome the contract names — the analysis called it not material"
                                     + (f": {mrow['why']}" if mrow["why"] else "")))
                continue
            if m_ is False:
                mat_rows.append(dict(mrow, status="not material", by="model" if mrow["why"] else "py",
                                     why=mrow["why"] or "the analysis judged it not material"))
                continue
            notes_at = len(sroi_notes)     # the notes of an outcome that is not valued become its why, never a note of the ratio
        ref = o.get("unit_value_ref")
        ref = ref.strip().strip("[]").strip() if isinstance(ref, str) else None
        if ref and ref in bench and (BENCHMARK_SCOPE.get(ref) or ("",))[0] not in UNIT_SCOPES:     # BUILD IC-4 (D6): the scope must fit
            sroi_notes.append(f"outcome {i + 1} ({catw}): unit_value_ref {ref!r} is {(BENCHMARK_SCOPE.get(ref) or ('no cost per unit',))[0]} "
                              "of its population, not a cost per outcome — not used; the outcome's own unit_value is")
            ref = None
        if ref and ref in bench:
            uv, uv_kind, uv_src = float(bench[ref][1]), "EXTERNAL", bench[ref][5] + (
                f" ({UNIT_COST_CAUTION})" if BENCHMARK_SCOPE[ref][0] == "an average cost" else "")
        elif on4 and ref and ref in data_vals:      # BUILD IC-4 (D14): a figure of the data, read by its id at the master stage
            uv, uv_kind, uv_src = float(data_vals[ref]), "MEASURED", f"the data: {ref}"
        else:
            if ref:
                sroi_notes.append(f"outcome {i + 1} ({catw}): unit_value_ref {ref[:60]!r} is no benchmark — its own unit_value is used")
            uv, uv_kind, uv_src = REG.parse_number(o.get("unit_value")), "MODELED", "the model's proxy value, citing: " + REG.fit_text(cite_words(str(o.get("source") or "no source")), "oracle.financial.source_words")   # IC-4 (D7)
        # B3 (Phase 4 fix): the outcome's rate is o_rate — named `rate` it overwrote the indirect cost rate set above, so the P&L's
        # label and the simulator showed the last outcome's rate as the indirect rate (KC: "Indirect (25%)" on costs computed at 15%)
        o_rate, how = rate_of(o.get("rate")), "a rate per person served (the model's)"      # IC-4 Phase 7 (review b12): a fraction read
        if o_rate is None and REG.parse_number(o.get("quantity")) is not None and beneficiaries[0] > 0:
            o_rate, how = REG.parse_number(o.get("quantity")) / beneficiaries[0], f"the model's Year-1 quantity {str(o.get('quantity'))[:20]} ÷ Year-1 people served"
        dur = REG.parse_number(o.get("duration_years"))
        dur_given = dur is not None
        if dur is None:
            dur = 1
            sroi_notes.append(f"outcome {i + 1} ({catw}): no duration given — Python's default of 1 year")
        dur = int(round(dur))
        if dur > SROI_MAX_DURATION:
            sroi_notes.append(f"outcome {i + 1} ({catw}): a duration of {dur} years is counted as {SROI_MAX_DURATION}")
            dur = SROI_MAX_DURATION
        if uv is None or uv <= 0 or o_rate is None or o_rate <= 0 or dur < 1:
            if on4:          # BUILD IC-4 (D6): material, not valued — the analysis's why, else Python's words for what is missing (its
                miss = [w for w, bad in (("no unit value", uv is None or uv <= 0), ("no rate", o_rate is None or o_rate <= 0),   # notes, which
                                         ("a duration under one year", dur < 1)) if bad]          # quote the analysis's words, stay notes)
                if mrow["why"]:
                    del sroi_notes[notes_at:]        # the analysis says why: the notes on a value it did not give are not needed
                mat_rows.append(dict(mrow, status="material, not valued", by="model" if mrow["why"] else "py", why=mrow["why"] or "; ".join(miss)))
                continue
            sroi_notes.append(f"outcome {i + 1} ({catw}): a unit value, a rate (or Year-1 quantity) and a duration of 1+ years are each needed — not used")
            continue
        if o_rate > 1:
            sroi_notes.append(f"outcome {i + 1} ({catw}): {o_rate:.2f} outcomes per person served (more than one each) — used as given")
        sroi_in.append({"n": len(sroi_in) + 1, "i": i, "category": cat, "unit_value": uv, "unit_kind": uv_kind, "unit_ref": ref if uv_kind == "EXTERNAL" else None,
                        "unit_source": uv_src, "rate": o_rate, "rate_how": how, "duration": dur, "dur_given": dur_given,
                        "source": REG.fit_text(str(o.get("source") or ""), "oracle.financial.source")})
        if on4:
            mat_rows.append(dict(mrow, status="valued", by="py", why="", sroi_n=len(sroi_in), unit_ref=ref if uv_kind != "MODELED" else None))
    if on4:
        named_ = {r_["core"] for r_ in mat_rows if r_.get("core")}
        mat_rows += [{"i": None, "outcome": c["words"], "core": c["id"], "status": "material, not valued", "by": "py", "why": "not proposed"}
                     for c in core if c["id"] not in named_]
        if unstated:
            sroi_notes.append("1 outcome did not say whether it is material — counted as material: the analysis proposed it" if unstated == 1 else
                              f"{unstated} outcomes did not say whether they are material — each counted as material: the analysis proposed it")
    sroi_partial = on4 and any(r_["status"] == "material, not valued" for r_ in mat_rows)
    for n_ in sroi_notes:
        NR.say(f"    SROI: {n_}")
    total_pv_benefits, total_pv_cost, sroi_last = sroi_compute(sroi_in, beneficiaries, total_exp, net_adj, dr, drop)
    sroi_ratio = (total_pv_benefits / total_pv_cost) if sroi_in and total_pv_cost > 0 else None
    sroi_sens = []                         # BUILD IC-4 (D6): the same outcomes and costs at each rate of the sensitivity pair
    for rid_, r_ in SROI_RATE_SENS:
        b_, c_, _l = sroi_compute(sroi_in, beneficiaries, total_exp, net_adj, r_, drop)
        if sroi_ratio is not None and c_ > 0:
            sroi_sens.append({"rate_id": rid_, "rate": r_, "id": f"sroi_at_{round(r_ * 100)}pct", "value": b_ / c_, "pv_b": b_, "pv_c": c_})
    if sroi_in and total_pv_cost <= 0:
        NR.say("    SROI: the present value of the cost is not above 0 — no SROI")
    sroi_capped = False                          # B3 (D1): nothing caps or replaces the ratio; the key stays for older readers

    # Build SROI derivation rows for transparent display (B3: Python's figures; each row's people per year and its outcomes)
    sroi_rows = []
    for r in sroi_in:
        q = [r["rate"] * n for n in beneficiaries[:3]]
        sroi_rows.append({"n": r["n"], "i": r["i"], "category": r["category"], "unit_value": r["unit_value"], "unit_kind": r["unit_kind"],
                          "unit_ref": r["unit_ref"], "rate": r["rate"], "rate_how": r["rate_how"], "duration": r["duration"],
                          "outcomes": q, "annual_y1": r["unit_value"] * q[0], "source": r["source"]})
    sroi_adjustments = {
        "deadweight": round(ma.get("deadweight", 0.15) * 100),
        "attribution": round(ma.get("attribution", 0.10) * 100),
        "displacement": round(ma.get("displacement", 0.05) * 100),
        "drop_off": round(drop * 100),
        "discount_rate": round(dr * 100, 1),
        "years": 3,
        "horizon": sroi_last,
    }
    sroi_pv_total = REG.fmt_value(total_pv_benefits, "money") if sroi_ratio is not None else ""
    sroi_pv_cost = REG.fmt_value(total_pv_cost, "money") if sroi_ratio is not None else ""
    sroi_display = REG.fmt_value(sroi_ratio, "ratio") if sroi_ratio is not None else ""

    # --- Build chart data ---
    def fmt_k(v):
        """The P&L's money (B3, D2; §9: "$1900K" read badly): the registry's money_short form — $425K, $1.9M; negatives in accounting
        parentheses."""
        if v > 0:
            return REG.money_short(v)
        elif v == 0:
            return "—"
        else:  # negative
            return f"({REG.money_short(abs(v))})"

    base_chart = {
        "labels": ["Year 1", "Year 2", "Year 3"],
        "stacked": True,
        "datasets": [
            {"label": "Personnel", "data": [round(p/1000) for p in personnel], "stack": "cost", "borderRadius": 6, "backgroundColor": "rgba(124,58,237,.6)"},
            {"label": "Ops & Transport", "data": [round(o/1000) for o in ops], "stack": "cost", "borderRadius": 6, "backgroundColor": "rgba(34,211,238,.5)"},
            {"label": "Technology", "data": [round(t/1000) for t in tech], "stack": "cost", "borderRadius": 6, "backgroundColor": "rgba(255,255,255,.15)"},
            {"label": "Outreach", "data": [round(o/1000) for o in outreach], "stack": "cost", "borderRadius": 6, "backgroundColor": "rgba(52,211,153,.4)"},
            {"label": f"Indirect ({round(rate*100)}%)", "data": [round(i/1000) for i in indirect], "stack": "cost", "borderRadius": 6, "backgroundColor": "rgba(251,191,36,.35)"},
            {"label": "Total Revenue", "data": [round(r/1000) for r in total_rev], "type": "line", "borderColor": "#34D399", "backgroundColor": "transparent", "borderWidth": 2.5, "pointRadius": 5, "pointBackgroundColor": "#34D399", "tension": 0.3, "order": -1},
        ]
    }

    # Bull scenario: supplemental grants + better partnerships
    # Revenue: 30% above base (Y1-Y2), 26% above base (Y3 — more conservative growth)
    # Expenses: Year 1 SAME as base (fixed infrastructure costs), Years 2-3 5% below base (efficiency gains)
    bull_rev = [round(total_rev[i] * m) for i, m in enumerate([C["bull_revenue_y1"], C["bull_revenue_y2"], C["bull_revenue_y3"]])]
    bull_exp = [total_exp[0]] + [round(total_exp[i] * C["bull_expense_later"]) for i in range(1, 3)]
    bull_surplus = [bull_rev[i] - bull_exp[i] for i in range(3)]
    bull_ben = [round_count(b * C["bull_reach"], step) for b in beneficiaries]  # P0.6 (D5): the target's step, never 0
    bull_cpb = [round(bull_exp[i] / bull_ben[i]) for i in range(3)]

    bull_chart = {
        "labels": ["Year 1", "Year 2", "Year 3"], "stacked": False,
        "datasets": [
            {"label": "Total Expenses", "data": [round(e/1000) for e in bull_exp], "borderRadius": 6, "backgroundColor": "rgba(124,58,237,.5)"},
            {"label": "Total Revenue", "data": [round(r/1000) for r in bull_rev], "type": "line", "borderColor": "#34D399", "backgroundColor": "transparent", "borderWidth": 2.5, "pointRadius": 5, "pointBackgroundColor": "#34D399", "tension": 0.3},
        ]
    }

    # Bear scenario: no supplemental funding, reduced service scope
    # Revenue: flat contract only, slight Y3 reduction (funding uncertainty)
    # Expenses: Year 1 SAME as base (fixed infrastructure), Years 2-3 progressively below (serving fewer people)
    bear_rev = [contract, contract, round(contract * C["bear_revenue_y3"])]
    bear_exp = [total_exp[0]] + [round(total_exp[i] * m) for i, m in zip(range(1, 3), [C["bear_expense_y2"], C["bear_expense_y3"]])]
    bear_surplus = [bear_rev[i] - bear_exp[i] for i in range(3)]
    bear_ben = [round_count(b * C["bear_reach"], step) for b in beneficiaries]  # P0.6 (D5): the target's step, never 0
    bear_cpb = [round(bear_exp[i] / bear_ben[i]) for i in range(3)]

    bear_chart = {
        "labels": ["Year 1", "Year 2", "Year 3"], "stacked": False,
        "datasets": [
            {"label": "Total Expenses", "data": [round(e/1000) for e in bear_exp], "borderRadius": 6, "backgroundColor": "rgba(251,191,36,.45)"},
            {"label": "Total Revenue", "data": [round(r/1000) for r in bear_rev], "type": "line", "borderColor": "#f43f5e", "backgroundColor": "transparent", "borderWidth": 2.5, "pointRadius": 5, "pointBackgroundColor": "#f43f5e", "tension": 0.3, "borderDash": [6, 3]},
        ]
    }

    # Build P&L rows
    def surplus_color(v): return "good" if v > 0 else "warn" if v == 0 else "bad"
    unit_p, unit_s = F["unit_plural"], F["unit_singular"]   # P0.6 (D1): the brief's unit

    base_pl = [
        {"label": NR.BUDGET_SOURCES[budget_src], "cells": [fmt_k(contract)]*3, "css_class": "", "colors": None},  # P0.6 (D2)
        {"label": "Supplemental Grants", "cells": [fmt_k(g) for g in grants], "css_class": "", "colors": None},
        {"label": "Earned Revenue", "cells": [fmt_k(e) for e in earned], "css_class": "", "colors": None},
        {"label": "Total Revenue", "cells": [fmt_k(r) for r in total_rev], "css_class": "total", "colors": None},
        {"label": "Personnel", "cells": [fmt_k(p) for p in personnel], "css_class": "", "colors": None},
        {"label": "Ops & Transport", "cells": [fmt_k(o) for o in ops], "css_class": "", "colors": None},
        {"label": "Technology", "cells": [fmt_k(t) for t in tech], "css_class": "", "colors": None},
        {"label": "Outreach (the brief's marketing reserve)" if reserve else "Outreach", "cells": [fmt_k(o) for o in outreach], "css_class": "", "colors": None},
        {"label": f"Indirect ({round(rate*100)}%)", "cells": [fmt_k(i) for i in indirect], "css_class": "", "colors": None},
        {"label": "Surplus", "cells": [fmt_k(s) for s in surplus], "css_class": "total", "colors": [surplus_color(s) for s in surplus]},
        {"label": f"Cost per {unit_s}", "cells": [REG.fmt_value(c, "money") for c in cpb], "css_class": "", "colors": None},
        {"label": f"{unit_p[:1].upper() + unit_p[1:]} served", "cells": [REG.fmt_value(b, "count") for b in beneficiaries], "css_class": "", "colors": None},
    ]

    bull_pl = [
        {"label": "Total Revenue", "cells": [fmt_k(r) for r in bull_rev], "css_class": "total", "colors": None},
        {"label": "Total Expenses", "cells": [fmt_k(e) for e in bull_exp], "css_class": "", "colors": None},
        {"label": "Surplus", "cells": [fmt_k(s) for s in bull_surplus], "css_class": "total", "colors": [surplus_color(s) for s in bull_surplus]},
        {"label": f"{unit_p[:1].upper() + unit_p[1:]} served", "cells": [REG.fmt_value(b, "count") for b in bull_ben], "css_class": "", "colors": None},
        {"label": f"Cost per {unit_s}", "cells": [REG.fmt_value(c, "money") for c in bull_cpb], "css_class": "", "colors": None},
    ]

    bear_pl = [
        {"label": "Total Revenue", "cells": [fmt_k(r) for r in bear_rev], "css_class": "total", "colors": None},
        {"label": "Total Expenses", "cells": [fmt_k(e) for e in bear_exp], "css_class": "", "colors": None},
        {"label": "Surplus", "cells": [fmt_k(s) for s in bear_surplus], "css_class": "total", "colors": [surplus_color(s) for s in bear_surplus]},
        {"label": f"{unit_p[:1].upper() + unit_p[1:]} served", "cells": [REG.fmt_value(b, "count") for b in bear_ben], "css_class": "", "colors": None},
        {"label": f"Cost per {unit_s}", "cells": [REG.fmt_value(c, "money") for c in bear_cpb], "css_class": "", "colors": None},
    ]

    # --- What-If Simulator config (Year 1 sensitivity explorer) ---
    # Derive fixed vs variable cost split from Year 1 actuals.
    # Fixed: personnel + technology (constant regardless of beneficiary count)
    # Variable: ops + outreach (scale linearly per beneficiary)
    import math
    sim_fixed = personnel[0] + tech[0]
    sim_var_per_ben = (ops[0] + outreach[0]) / max(beneficiaries[0], 1)
    # Back-solve social value per beneficiary so SROI matches at default position:
    # SROI = (ben × svpb) / total_costs → svpb = SROI × total_costs / ben
    sim_svpb = (sroi_ratio or 0) * total_exp[0] / max(beneficiaries[0], 1)
    # Slider ranges (P0.6, D5): derived from the base values — beneficiaries 5/12 to 20/12 of Year 1 in steps of 1/12, the budget
    # 0.6× to 1.5× in steps of 1/20, each step a 1-2-2.5-5 number — so 12,000 and $500,000 give P0.3's 5,000–20,000 by 1,000 and
    # $300,000–$750,000 by $25,000 exactly
    sim_ben_min, sim_ben_max, sim_ben_step = sim_range(beneficiaries[0], 5 / 12, 20 / 12, 1 / 12)
    sim_cv_min, sim_cv_max, sim_cv_step = sim_range(contract, 0.6, 1.5, 1 / 20)
    # Y-axis max: accommodate highest possible costs AND highest contract line
    sim_max_var = sim_var_per_ben * sim_ben_max
    sim_max_total = (sim_fixed + sim_max_var) * (1 + rate)
    sim_y_max = math.ceil(max(sim_max_total, sim_cv_max) / 50000) * 50000

    simulator = {
        "fixed_costs": sim_fixed,
        "variable_cost_per_ben": round(sim_var_per_ben, 10),
        "indirect_rate": rate,
        "social_value_per_ben": round(sim_svpb, 10),
        "has_sroi": sroi_ratio is not None,     # B3 (D1): no cap; the simulator shows the SROI only when the model has one
        "defaults": {"beneficiaries": beneficiaries[0], "contract": contract},
        "ranges": {
            "ben_min": sim_ben_min, "ben_max": sim_ben_max, "ben_step": sim_ben_step,
            "cv_min": sim_cv_min, "cv_max": sim_cv_max, "cv_step": sim_cv_step,
        },
        "y_max": sim_y_max,
        "cost_labels": ["Personnel", "Operations", "Technology", "Indirect"],
        "cost_colors": [
            "rgba(124,58,237,.7)",   # Personnel — purple (matches P&L chart)
            "rgba(34,211,238,.6)",   # Operations — cyan
            "rgba(255,255,255,.2)",  # Technology — white/grey
            "rgba(251,191,36,.45)", # Indirect — amber
        ],
        # Pre-computed Year 1 breakdown for chart initialization
        "y1_costs": {
            "personnel": personnel[0],
            "operations": ops[0],
            "technology": tech[0],
            "indirect": indirect[0],
        },
    }

    wage_ids = {b[0] for b in BENCHMARKS if b[0].startswith("ext.wage_")}
    wage_refs = [r["salary_ref"] for r in staff if r["salary_ref"] and r["salary_ref"].strip("[] ") in wage_ids]   # B3 (D1): a wage of the sheet
    # BUILD B1 (D4): the use of funds — Year 1's lines exactly as computed above (the roles to whole dollars by largest remainder, so
    # they sum to the personnel line), and the remainder named, so the lines sum to the budget by construction
    role_amt = whole_parts([r["salary"] * r["count"] * (1 + r["benefits_pct"]) * scale_factor for r in staff], personnel[0])
    use_of_funds = {"contract": contract, "contract_source": budget_src, "reserve": reserve, "rate_pct": round(rate * 100),
                    "scale": scale_factor,   # review #5: the personnel cap scaled each role (the page says so)
                    "total_exp": total_exp[0],
                    "roles": [{"role": r["role"], "count": r["count"], "amount": a} for r, a in zip(staff, role_amt)],
                    "lines": [{"key": "personnel", "amount": personnel[0]}, {"key": "operations", "amount": ops[0]},
                              {"key": "technology", "amount": tech[0]}, {"key": "outreach", "amount": outreach[0]},
                              {"key": "indirect", "amount": indirect[0]}, {"key": "contingency", "amount": contract - total_exp[0]}]}

    # BUILD B3 (D1): the registry items of this model — its inputs (the model's, or Python's default, named), Python's constants and
    # its figures; a computed figure's kind is its weakest input's (MODELED: every figure here rests on the model's inputs)
    unit_s, unit_p_ = F["unit_singular"], F["unit_plural"]
    for key, fmt, what in FIN_INPUTS:
        v = outreach[0] if key == "outreach_cost_y1" else ma.get(key)
        if key == "outreach_cost_y1" and reserve:
            items.append(dict(id=key, value=v, kind="MEASURED", fmt=fmt, unit="USD", label=_unit_words(what, unit_p_) + " (the brief's marketing reserve)",
                              source=f"the brief: \u201c{F['marketing_quote']}\u201d"))
        elif key == "discount_rate":         # BUILD IC-4 (D6): Python's method constant, with its source
            items.append(dict(id=key, value=SROI_RATE, kind="MODELED", fmt=fmt, label=_unit_words(what, unit_p_), source=SROI_RATE_SOURCE,
                              assumption=f"a method constant of this model ({SROI_METHOD}); the model's value is not used"))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            g = _given(ma, key)
            items.append(dict(id=key, value=v, kind="MODELED", fmt=fmt, label=_unit_words(what, unit_p_),
                              source="the model's proposal (master analysis)" if g == "given" else
                              "Python's default — the model gave none" if g == "default" else f"Python's default — {g}"))
    for k, (v, fmt, why) in FIN_CONSTANTS.items():
        if on4 and k == "coi_er_rate":
            continue           # BUILD IC-4 (D6): the cost of inaction comes from the outcomes — no ER-visit share
        items.append(dict(id=f"const.{k}", value=v, kind="MODELED", fmt=fmt, label=_unit_words(CONST_LABELS.get(k, k.replace("_", " ")), unit_p_),
                          source=CONST_SOURCE, assumption=why))
    bench_words = {b[0]: b[4] for b in BENCHMARKS}   # BUILD IC-5 Phase 4 (the replays, D4): a benchmark named by its words, never its id
    for i, r in enumerate(staff):
        ref_ = (r["salary_ref"] or "").strip("[] ")
        if ref_ and ref_ not in bench_words:
            NR.say(f"    staff role {i + 1} ({r['role'][:40]}): salary_ref {ref_[:60]!r} is none of the benchmarks — the page names no reference")
        cites = "" if not ref_ else f", citing {bench_words[ref_]}" if ref_ in bench_words else ", citing a reference that is none of the benchmarks"
        for k, (dv, fmt) in FIN_ROLE_DEFAULTS.items():
            items.append(dict(id=f"staff.{i + 1}.{k}", value=r[k], kind="MODELED", label=f"{r['role']}: {k.replace('_', ' ')}", label_model=r["role"],
                              fmt="num" if fmt == "count" and not float(r[k]).is_integer() else fmt,     # B3 (review a F7): 0.5 of a role is "0.5"
                              source=("the model's staffing plan" + (cites if k == "salary" else ""))
                              if k in r["given"] else "Python's default — the model gave none"))
    items.append(dict(id="staff_count", value=sum(r["count"] for r in staff), kind="MODELED", fmt="num", unit="staff",
                      label="staff in the plan", source="the model's staffing plan"))
    if budget_src in ("proposed", "cost_plan"):   # the brief's figure and an override are registered with the brief's facts (step 1)
        items.append(dict(id="budget", value=contract, kind="MODELED", fmt="money_short", unit="USD", label=NR.BUDGET_SOURCES[budget_src],
                          source={"proposed": "the model's proposal (the brief states no budget)",
                                  "cost_plan": "Python: the Year-1 cost plan rounded up to $10,000"}[budget_src]))
    figs = [("beneficiaries_y{}", beneficiaries, "count", unit_p_, f"{unit_p_} served in Year {{}}", "the Year-1 target, grown by the Year-2 and Year-3 growth rates"),
            ("cpb_y{}", cpb, "money", f"USD per {unit_s}", f"cost per {unit_s}, Year {{}}", "total expenses ÷ people served"),
            ("total_exp_y{}", total_exp, "money_short", "USD", "total expenses, Year {}", "personnel + operations + technology + outreach + indirect"),
            ("total_rev_y{}", total_rev, "money_short", "USD", "total revenue, Year {}", "the budget + grants + earned revenue"),
            ("surplus_y{}", surplus, "money_short", "USD", "surplus, Year {}", "revenue − expenses")]
    for pat, vals, fmt, unit, lab, formula in figs:
        for y, v in enumerate(vals[:3]):
            items.append(dict(id=pat.format(y + 1), value=v, kind="MODELED", fmt=fmt, unit=unit, label=lab.format(y + 1), formula=formula,
                              source="Python's financial model"))
    for key, v, lab in (("personnel_y1", personnel[0], "personnel, Year 1"), ("ops_y1", ops[0], "operations, Year 1"),
                        ("tech_y1", tech[0], "technology, Year 1"), ("outreach_y1", outreach[0], "outreach, Year 1"),
                        ("indirect_y1", indirect[0], "indirect costs, Year 1"), ("contingency_y1", contract - total_exp[0], "the budget less Year-1 expenses")):
        items.append(dict(id=key, value=v, kind="MODELED", fmt="money_short", unit="USD", label=lab, source="Python's financial model"))
    stress_b = round(beneficiaries[0] * C["stress_reach"])
    items.append(dict(id="stress_beneficiaries_y1", value=stress_b, kind="MODELED", fmt="count", unit=unit_p_,
                      label=f"Year-1 {unit_p_} served if 20% fewer are reached", formula=f"Year-1 {unit_p_} served × {round(C['stress_reach'] * 100)}% (the stress case's reach)", source="Python's stress case"))
    if stress_b > 0:
        items.append(dict(id="stress_cpb_y1", value=round(total_exp[0] / stress_b), kind="MODELED", fmt="money", unit=f"USD per {unit_s}",
                          label=f"Year-1 cost per {unit_s} if 20% fewer are reached", formula=f"Year-1 total expenses ÷ Year-1 {unit_p_} served in the stress case", source="Python's stress case"))
    for r in sroi_in:
        items.append(dict(id=f"sroi.{r['n']}.unit_value", value=r["unit_value"], kind=r["unit_kind"], fmt="money", unit="USD per outcome",
                          label=f"{r['category']}: value of one outcome", label_model=r["category"],   # IC-3 (ledger 24): the model's words
                          source=cite_words(r["unit_source"]),   # run turn R4: no raw tag (BUILD IC-5 Phase 4: its tokens kept for the resolver)
                          # B3 (the EXTERNAL check): a benchmark's value carries the benchmark's own check — never a dagger beside a checked figure
                          verified=bench[r["unit_ref"]][6] if r["unit_kind"] == "EXTERNAL" and r["unit_ref"] in bench else None))
        items.append(dict(id=f"sroi.{r['n']}.rate", value=r["rate"], kind="MODELED", fmt="pct" if r["rate"] <= 1 else "num",
                          label=f"{r['category']}: outcomes per {unit_s} served", label_model=r["category"], source=r["rate_how"]))
        items.append(dict(id=f"sroi.{r['n']}.duration", value=r["duration"], kind="MODELED", fmt="count", unit="years",
                          label=f"{r['category']}: years the outcome lasts", label_model=r["category"],
                          source="the model's" if r["dur_given"] else "Python's default of 1 year"))
    if sroi_ratio is not None:
        items += [dict(id="sroi", value=sroi_ratio, kind="MODELED", fmt="ratio", label="social return on investment: PV of social value ÷ PV of cost",
                       formula=f"{REG.fmt_value(total_pv_benefits, 'money')} ÷ {REG.fmt_value(total_pv_cost, 'money')}",
                       source="Python's SROI model on the model's outcomes"),
                  *[dict(id=s_["rate_id"], value=s_["rate"], kind="MODELED", fmt="pct", label="a discount rate of the sensitivity pair",
                         source=SROI_SENS_SOURCE) for s_ in sroi_sens],
                  *[dict(id=s_["id"], value=s_["value"], kind="MODELED", fmt="ratio", label=f"the SROI ratio at the sensitivity pair's {s_['rate']:.0%} rate",
                         formula=f"{REG.fmt_value(s_['pv_b'], 'money')} ÷ {REG.fmt_value(s_['pv_c'], 'money')}", inputs=[s_["rate_id"]],
                         source="Python's SROI model on the model's outcomes, the same costs") for s_ in sroi_sens],
                  dict(id="sroi_per_dollar", value=sroi_ratio, kind="MODELED", fmt="per_dollar", unit="USD of social value per USD 1",
                       label="social value per $1 invested", formula="the SROI ratio", source="Python's SROI model"),
                  dict(id="sroi_pv_benefits", value=total_pv_benefits, kind="MODELED", fmt="money", unit="USD",
                       label="present value of the counted social value", source="Python's SROI model"),
                  dict(id="sroi_pv_cost", value=total_pv_cost, kind="MODELED", fmt="money", unit="USD",
                       label="present value of the three years' cost", source="Python's SROI model"),
                  dict(id="sroi_horizon_years", value=sroi_last, kind="MODELED", fmt="count", unit="years",
                       label="the years over which outcomes are counted", source="Python's SROI model: the longest outcome's last year")]
    coi_l = None
    if on4:     # BUILD IC-4 (D6): for each year and each valued outcome, rate × that year's people served × unit value × (1 − deadweight)
        dw_ = ma.get("deadweight", 0.15)
        if sroi_in:
            ys_ = [round(sum(r["rate"] * beneficiaries[y] * r["unit_value"] for r in sroi_in) * (1 - dw_)) for y in range(3)]
            coi_l = {"coi_y1": ys_[0], "coi_y2": ys_[1], "coi_y3": ys_[2], "total": sum(ys_), "deadweight": dw_, "outcomes": len(sroi_in)}
        if coi_l and coi_l["total"] > 0:
            for y in range(3):
                items.append(dict(id=f"coi_y{y + 1}", value=coi_l[f"coi_y{y + 1}"], kind="MODELED", fmt="money_short", unit="USD",
                                  label=f"cost of inaction, Year {y + 1}",
                                  formula=f"Σ over the valued outcomes: rate × Year-{y + 1} {unit_p_} served × unit value × (1 − deadweight)",
                                  inputs=[f"beneficiaries_y{y + 1}", "deadweight"] + [f"sroi.{r['n']}.{k}" for r in sroi_in for k in ("rate", "unit_value")],
                                  source="Python's cost of inaction, from the valued outcomes"))
            items.append(dict(id="coi_3yr", value=coi_l["total"], kind="MODELED", fmt="money_short", unit="USD", label="cost of inaction over three years",
                              formula="the cost of inaction in Years 1, 2 and 3, summed", source="Python's cost of inaction, from the valued outcomes"))
    c = None if on4 else coi_numbers(beneficiaries)
    if c and c["total"] > 0:
        items.append(dict(id="coi_rate", value=c["rate"], kind="MODELED", fmt="pct", label=_unit_words("share of people served with one avoidable ER visit a year", F["unit_plural"]),
                          source=CONST_SOURCE, assumption=FIN_CONSTANTS["coi_er_rate"][2]))
        for y in range(3):
            items += [dict(id=f"coi_visits_y{y + 1}", value=c[f"y{y + 1}_visits"], kind="MODELED", fmt="count", unit="visits",
                           label=f"avoidable ER visits, Year {y + 1}", formula=f"Year-{y + 1} {unit_p_} served × the share with one avoidable ER visit a year",
                           source="Python's cost of inaction"),
                      dict(id=f"coi_y{y + 1}", value=c[f"coi_y{y + 1}"], kind="MODELED", fmt="money_short", unit="USD",
                           label=f"cost of inaction, Year {y + 1}", formula=f"Year-{y + 1} avoidable ER visits × the cost of one ER visit",
                           source="Python's cost of inaction")]
        items.append(dict(id="coi_3yr", value=c["total"], kind="MODELED", fmt="money_short", unit="USD", label="cost of inaction over three years",
                          formula="the cost of inaction in Years 1, 2 and 3, summed", source="Python's cost of inaction"))

    if sroi_partial:     # BUILD IC-4 (D6): never shown as complete — the words name each material outcome not valued
        nv_ = "; ".join(f"{r_['outcome']} ({r_['why']})" for r_ in mat_rows if r_["status"] == "material, not valued")
        part_ = f"partial — counted on the valued outcomes only; material, not valued: {nv_}"
        pids_ = {"sroi", "sroi_per_dollar", "sroi_pv_benefits", "coi_y1", "coi_y2", "coi_y3", "coi_3yr"} | {s_["id"] for s_ in sroi_sens}
        for it in items:
            if it.get("id") in pids_:
                it["partial"] = REG.fit_text(part_, "oracle.financial.partial")
    return {
        **({"materiality": mat_rows, "sroi_partial": sroi_partial, "coi": coi_l} if on4 else {}),     # BUILD IC-4 (D6): the switch on only
        "registry_items": items,
        "sroi_notes": sroi_notes,
        "sroi_ratio": sroi_ratio,
        "sroi_rows": sroi_rows,
        "sroi_sens": [{"rate_id": s_["rate_id"], "id": s_["id"]} for s_ in sroi_sens],     # BUILD IC-4 (D6): the sensitivity pair's rows
        "sroi_adjustments": sroi_adjustments,
        "sroi_capped": sroi_capped,
        "sroi_pv_total": sroi_pv_total,
        "sroi_pv_cost": sroi_pv_cost,
        "sroi_display": sroi_display,
        "cpb_y1": cpb[0],
        "beneficiaries": beneficiaries,
        "surplus": surplus,
        "chart_data": {"base": base_chart, "bull": bull_chart, "bear": bear_chart},
        "scenarios": {
            "base": {"tab_label": "Base Case", "chart_title": "3-Year Revenue and Cost Trajectory",
                     "table_title": "Base Case P&L", "pl_rows": base_pl,
                     "footnote": (f"Indirect: {round(rate*100)}% de minimis per 2 CFR 200.414. " if round(rate * 100) == 15 else  # P0.6 (D7)
                                  f"Indirect: {round(rate*100)}% of modified total direct costs. ")
                                 + ("Personnel based on BLS median wages." if staff and len(wage_refs) == len(staff) else      # B3 (review a F9):
                                    f"{len(wage_refs)} of {len(staff)} staff roles cite a BLS median wage." if wage_refs else "")},  # all, or how many
            "bull": {"tab_label": "Optimistic", "chart_title": "Optimistic Scenario",
                     "table_title": "Optimistic P&L", "pl_rows": bull_pl, "footnote": None},
            "bear": {"tab_label": "Conservative", "chart_title": "Conservative Scenario",
                     "table_title": "Conservative P&L", "pl_rows": bear_pl, "footnote": None},
        },
        "kpis": {
            "beneficiaries_y1": beneficiaries[0],
            "cpb_y1": cpb[0],
            "sroi": sroi_ratio,
            "total_exp_y1": total_exp[0],
            "contract": contract,
            "contract_source": budget_src,                 # P0.6 (D2): override | brief | proposed | cost_plan
            "contract_label": NR.BUDGET_SOURCES[budget_src],
            "marketing_reserve": reserve,
            "unit_plural": F["unit_plural"], "unit_singular": F["unit_singular"],
        },
        "simulator": simulator,
        "use_of_funds": use_of_funds,   # BUILD B1 (D4)
    }


# ─────────────────────────────────────────────────────────────────────
# STAGE 4: MERMAID DIAGRAM — generate, sanitize, render via mmdc
# Per Implementation Guide §6: auto-repair loop with 3 retries
# ─────────────────────────────────────────────────────────────────────
def fix_mermaid_syntax(code: str) -> str:
    """Apply known fixes to LLM-generated Mermaid."""
    code = re.sub(r'^```(?:mermaid)?\s*\n?', '', code.strip())
    code = re.sub(r'\n?```\s*$', '', code.strip())
    code = re.sub(r'\bend\b(\s*[\[\(\{"|\<>])', r'End\1', code)
    return code

MMDC_WHY = {"absent": "the diagram tool is not installed on the machine that built this page",
             "refused": "the diagram tool found lies in the program's own folder, so it was not run",
             "argument": "a folder name on the machine that built this page holds a character a command line could misread, so the "
                         "diagram tool was not run",
             "timeout": "the diagram tool took longer than 30 seconds", "error": "the diagram tool could not draw it",
             "time": "no time was left to draw it before the page was built"}     # BUILD IC-6 (D10): the page's words (no path)


def render_mermaid_svg(code: str, max_retries: int = 3, repair: bool = True, why: list = None) -> str | None:
    """Render Mermaid to SVG via mmdc. Returns SVG string or None. BUILD IC-5 (D15): repair=False for Python's own code (the diagram's
    data) — no syntax fix and no model call: mmdc draws it, or the page's browser does (the error named). BUILD IC-6 (D10, G11): mmdc by
    its absolute path (NR.find_tool: never from the working folder or the harness's), every element of its command checked against
    NR.TOOL_ARG_RE (else not run), shell=False; the diagram's text stays in the input file. `why` gets the page's words (MMDC_WHY) when
    no SVG comes back."""
    why = why if why is not None else []
    code = fix_mermaid_syntax(code) if repair else code
    tool, refused = NR.find_tool("mmdc", Path.cwd(), TEMPLATE_PATH.parent)
    if tool is None:
        k_ = "absent" if "not on this machine's PATH" in str(refused) else "refused"
        NR.say(f"    ⚠ diagram: {refused} — the page's browser draws it (vendor/mermaid.min.js beside the page; the CDN only if that file is missing)")
        why.append(MMDC_WHY[k_])
        return None
    config = {"theme": "dark", "themeVariables": {
        "primaryColor": "#3B82F6", "primaryBorderColor": "#60A5FA",
        "textColor": "#E2E8F0", "mainBkg": "#1E293B",
        "lineColor": "#94A3B8", "nodeBorder": "#475569",
        "fontFamily": "Plus Jakarta Sans, sans-serif", "fontSize": "16px"
    }, "flowchart": {"curve": "basis"}, "background": "transparent"}

    for attempt in range(max_retries + 1):
        with tempfile.TemporaryDirectory() as tmpdir:
            inp = Path(tmpdir) / "input.mmd"
            out = Path(tmpdir) / "output.svg"
            cfg = Path(tmpdir) / "config.json"
            inp.write_text(code, encoding="utf-8")
            cfg.write_text(json.dumps(config), encoding="utf-8")
            cmd = [tool, "-i", str(inp), "-o", str(out), "-b", "transparent", "--configFile", str(cfg), "--quiet"]
            bad = [a for a in cmd if not NR.TOOL_ARG_RE.match(a)]
            if bad:         # BUILD IC-6 (D10): a character outside the allowlist in any element — the tool is not run
                NR.say(f"    ⚠ diagram: the diagram tool was not run — {len(bad)} element(s) of its command hold a character outside "
                       f"letters, digits and _ : \\ / . space ~ - ({', '.join(repr(b) for b in bad[:2])}); the page's browser draws it")
                why.append(MMDC_WHY["argument"])
                return None
            try:
                result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and out.exists():
                    svg = out.read_text(encoding="utf-8")
                    # Clean SVG for inlining: remove XML declaration, fix dimensions
                    svg = re.sub(r'<\?xml[^>]*\?>\s*', '', svg)
                    svg = re.sub(r'(width|height)="\d+(\.\d+)?"', '', svg, count=2)
                    svg = svg.replace('style="', 'style="max-width:100%;height:auto;', 1)
                    return svg
                error = result.stderr.strip()
            except subprocess.TimeoutExpired:
                error = "mmdc timed out after 30 seconds"
            except (FileNotFoundError, OSError) as e_:
                console.print(f"  [yellow]⚠ mmdc could not be started ({type(e_).__name__}) — the browser draws the diagram with Mermaid (vendor/mermaid.min.js beside the page; the CDN only if that file is missing)[/yellow]")
                why.append(MMDC_WHY["absent"])
                return None

        if not repair:
            NR.say(f"    diagram: mmdc did not draw Python's code ({error[:80]}) — the page's browser draws it")
            why.append(MMDC_WHY["timeout" if "timed out" in error else "error"])
            return None
        if attempt < max_retries:
            console.print(f"  [yellow]Mermaid error (attempt {attempt+1}): {error[:80]}[/yellow]")
            # LLM repair
            try:
                code = call_claude(
                    f"Fix this Mermaid syntax error. Return ONLY the corrected Mermaid code.\n"
                    f"Error: {error}\nCode:\n```mermaid\n{code}\n```",
                    model=OPUS)        # BUILD IC-6 (D1): its profile, "oracle/diagram_repair" (4,000 at low, not streamed)
                code = fix_mermaid_syntax(code)
            except Exception:
                pass
    why.append(MMDC_WHY["error"])
    return None

_MM_ID = re.compile(r"\s*(?:[\w\-]+(?:[.:][\w.:\-]+)+|[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\s*")


def mermaid_resolve(reg, code):
    """B3 (Phase 4, review b F15): the diagram's tokens as plain figures — but Mermaid's own [[…]] shape (a subroutine node,
    B[["Navigation Hub"]], C[[Case Review]]) is kept: a [[…]] is a token only when it is a form (a proposal, a quote, a comparison, a
    declaration: BUILD IC-2, D1), names a figure, or has an id's shape (then one naming nothing is blanked and named, as before)."""
    if not isinstance(code, str):
        return code, []
    held = []

    def hold(m):
        inner = m.group(1)
        if reg.is_token(inner) or _MM_ID.fullmatch(inner):    # IC-2 (D1): before, a declaration (nf:) was kept raw as a shape
            return m.group(0)
        held.append(m.group(0))
        return f"\ue003{len(held) - 1}\ue004"
    t, ev = reg.resolve(REG.TOKEN_RE.sub(hold, code), mode="plain")
    if isinstance(t, str):
        t = re.sub(r"\ue003(\d+)\ue004", lambda m: held[int(m.group(1))], t)
    return t, ev


def diagram_typed(mermaid_code, reg, ex):
    """The workflow diagram's typed figures — (typed, events, resolved code): every quantity the one reader finds in its labels' own words
    (the tokens set aside), and each refused quote's or declaration's number as typed (IC-2, D6). BUILD IC-3 (ledger step 28): one
    function for the diagram stage and for the page's count, which reads it again from the model's own code when the page is built — a
    page rebuilt from older checkpoints (the run turn's R4, --stage html_assembly) counts them as this build's reader does."""
    import html as _h
    # B3 (Phase 4, review b F8): every label — quoted, or unquoted inside a node's brackets or an edge's bars ("A[24 sites]")
    labels_raw = " ".join(x for grp in re.findall(r'"([^"]*)"|\[([^\[\]"]*)\]|\(([^()"]*)\)|\{([^{}"]*)\}|\|([^|"]*)\|',
                                                 REG.TOKEN_RE.sub(" ", _h.unescape(mermaid_code))) for x in grp if x)
    # BUILD IC-3 (D1): the labels' typed quantities as the one reader reads them (words too); names, dates, markers set aside by its roles
    typed = [x["words"] for x in REG.quantities(labels_raw, names=ex) if x["role"] == "quantity"]
    code, ev = mermaid_resolve(reg, mermaid_code)
    # BUILD IC-2 (D6): a refused quote or declaration shows its number as typed — a typed figure of the diagram, like one with no token
    typed += [q["words"] for x in ev if x["rule"] in REG.REFUSAL_EVENTS for q in REG.quantities(x.get("text") or "")
              if q["role"] != "vague"]       # IC-3: a refused form's typed number counts, whatever its shape (as on the page); a stale
    #                                          mention's too (review b #4: REG.REFUSAL_EVENTS, the one map of what shows as typed)
    return typed, ev, code


def diagram_forms(ev) -> dict:
    """BUILD IC-2 (D4, D6): the diagram's declarations and refused forms, listed in the integrity details with the page's own (the diagram
    is Python's drawing: its labels are plain text, so the page's spans cannot list them). IC-3 (ledger step 28): one function for the
    stage and the page."""
    return {"declared": [{"text": x["text"], "kind": REG.NF_KINDS.get(x.get("kind"), x.get("kind")), "sec": "the workflow diagram",
                          "context": "a label of the workflow diagram"} for x in ev if x["rule"] == "declared"],
            "refused": [{"text": x["text"], "rule": REG.REFUSAL_EVENTS[x["rule"]], "why": x.get("why") or "",
                         "sec": "the workflow diagram", "context": "a label of the workflow diagram"} for x in ev if x["rule"] in REG.REFUSAL_EVENTS],
            "ids": sorted({x["id"] for x in ev if x["rule"] == "cited" and str(x.get("id") or "").startswith(("q.", "calc."))})}


# ── BUILD IC-5 (D15, Q-DIAGRAM): the diagram as data — the model names its steps and links; Python writes the Mermaid code ──
DIAGRAM_CLASSES = ("input", "hub", "service", "outcome", "decision")      # the five classes IC-4's prompt named, in its order
DIAGRAM_MAX_NODES = 12                                                    # the request's "Maximum 10-12 nodes total" — BUILD IC-8 (D4): no
#                                                                           position cap reads it (V12d); the rules' maximum is DIAGRAM_POLICY's
DIAGRAM_SIZE_WORDS = "DIAGRAM SIZE CONSTRAINT (critical): Maximum 10-12 nodes total. Maximum 3 nodes per horizontal row."
# BUILD IC-8 (D4, I5, Q-DIAGRAM): the ask (the master request's words, byte for byte; under the switch the update's kind line ends with
# them, V12c), the hard maximum of steps (G5), the written source's maximum (half of Mermaid's maxTextSize, 5e4 [V15]) and each class's
# word for a step left with none (V13c)
DIAGRAM_POLICY = {"ask": DIAGRAM_SIZE_WORDS, "hard": 16, "mermaid_max": 25000, "version": "ic8.1",
                  "words": {"input": "Input", "hub": "Hub", "service": "Service", "outcome": "Outcome", "decision": "Decision", "": "Step"}}
DIAGRAM_LABEL_CHARS, DIAGRAM_EDGE_CHARS, DIAGRAM_TIP_CHARS = 60, 40, 240
DIAGRAM_CLASSDEFS = ("classDef input fill:#0d3b2e,stroke:#34d399,stroke-width:2px,color:#e9ecff",      # IC-4's prompt's five, now Python's
                     "classDef hub fill:#1e1145,stroke:#7C3AED,stroke-width:2px,color:#e9ecff",
                     "classDef service fill:#0c2a3a,stroke:#22d3ee,stroke-width:2px,color:#e9ecff",
                     "classDef outcome fill:#2d1f0f,stroke:#f59e0b,stroke-width:2px,color:#e9ecff",
                     "classDef decision fill:#2d1f3d,stroke:#a78bfa,stroke-width:2px,color:#e9ecff")
DIAGRAM_SHOWN = {"type": "object", "properties": {      # the form the master request shows: plain strings, no value from the data
    "nodes": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"type": "string"},
                                                                          "class": {"type": "string"}, "tooltip_ref": {"type": "string"}},
                                         "required": ["id", "label", "class", "tooltip_ref"]}},
    "edges": {"type": "array", "items": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"},
                                                                          "label": {"type": "string"}},
                                         "required": ["from", "to", "label"]}}},
    "required": ["nodes", "edges"]}
DIAGRAM_ASK_START, DIAGRAM_ASK_END = "- mermaid_code: valid Mermaid flowchart TD syntax.\n", "- phases: 3 implementation phases with HTML content."
DIAGRAM_ASK = (      # the size rules and the label examples in IC-4's words (the instruction-number ledger holds them)
    "- diagram: the program's workflow as its steps and their links — Python writes the Mermaid code, draws it and writes each step's\n"
    "  tooltip from the registry's figures. Its form: {\"nodes\": [{\"id\": \"<a short id of your own>\", \"label\": \"<plain words: no\n"
    "  Mermaid syntax, no markup>\", \"class\": \"<input, hub, service, outcome or decision>\", \"tooltip_ref\": \"<the tokens of the\n"
    "  figures this step's tooltip lists, such as [[<id>]] [[<id>]], or empty>\"}], \"edges\": [{\"from\": \"<a node's id>\", \"to\":\n"
    "  \"<a node's id>\", \"label\": \"<a few plain words, or empty>\"}]}\n"
    "  " + DIAGRAM_SIZE_WORDS + "\n"                     # BUILD IC-8 (D4): the policy's ask, the same bytes
    "  Group individual partners into single nodes (e.g., \"N Partner Sites\", not each partner by name).\n"
    "  Group ZIP codes or areas into tiers (e.g., one node \"Critical Areas: [area] · [area] · [area]\", not separate nodes).\n"
    "  Keep node labels SHORT (2-3 words + at most one figure, as a token). Put detailed figures in the step's tooltip_ref, not its label.\n"
    "  Every figure in a label is a token ([[<id>]]); Python writes the figure in before the diagram is drawn.\n"
    "  Good label: \"Navigation Hub\" — Bad label: \"[Program Name] Navigation Hub CRM + Supply Dashboard\"\n"
    "  Good label: \"N Partner Sites\" — Bad label: \"[Partner A] · [Partner B] · [Partner C] · [Partner D]\"\n"
    "  IMPORTANT: For outcome/result nodes (enrollments, avoided crises, follow-ups, benefits), do NOT include\n"
    "  specific quantity targets in the label — use descriptive text only (e.g., \"Benefit Enrollment\" not \"Benefit\n"
    "  Enrollment N new/yr\"). These quantities are in the SROI derivation table and may be adjusted during\n"
    "  financial modeling. Data-source quantities from challenge data are fine in labels, as tokens (like \"[[sum:<endpoint>.<field>]] calls\").\n"
    "  Classes: input for data sources and intake, hub for the central hub or dashboard, service for partner sites, distribution and\n"
    "  areas, outcome for results, follow-ups and outcomes, decision for decision and triage steps (drawn as a diamond).\n"
    "  Tooltips: tooltip_ref lists, as tokens, the figures a step's tooltip shows — a data source's record count and key metric, a site's\n"
    "  capacity, an area's key rates and size, an outcome's SROI proxy value; Python writes each figure with its label. No click line,\n"
    "  classDef or style: Python writes the drawing.\n")
DIAGRAM_KIND_IC4 = "diagram: Mermaid code, a\n   string."
DIAGRAM_KIND_D15 = ("diagram: an object of the form Block 1's\n   diagram has in <block1_analysis> (its nodes and edges; Python writes "
                    "the Mermaid code).")


def has_diagram_data(ma) -> bool:
    """BUILD IC-5 (D15): whether an analysis names its diagram as data (the switch on) — then that data is its diagram, in both blocks."""
    return NR.ic5_prompt() and isinstance((ma or {}).get("diagram"), dict) and bool(ma["diagram"].get("nodes"))


def diagram_schema_shown(schema):
    """BUILD IC-5 (D15): the master schema as the request shows it — mermaid_code's place holds the diagram's form (DIAGRAM_SHOWN); a
    copy (the model's schema is unchanged)."""
    out = copy.deepcopy(schema)
    out["properties"] = {("diagram" if k == "mermaid_code" else k): (copy.deepcopy(DIAGRAM_SHOWN) if k == "mermaid_code" else v)
                         for k, v in (out.get("properties") or {}).items()}
    if isinstance(out.get("required"), list):
        out["required"] = [x for x in out["required"] if x != "mermaid_code"]
    return out


def diagram_ask_words(prompt):
    """BUILD IC-5 (D15): the master request's diagram instruction with the switch on — IC-4's Mermaid block (DIAGRAM_ASK_START up to
    DIAGRAM_ASK_END) replaced by DIAGRAM_ASK. Each marker must stand once, else the prompt is left as it is and that is named."""
    i, j = prompt.find(DIAGRAM_ASK_START), prompt.find(DIAGRAM_ASK_END)
    if prompt.count(DIAGRAM_ASK_START) != 1 or prompt.count(DIAGRAM_ASK_END) != 1 or j < i:
        NR.say("    ⚠ diagram: the master request's diagram instruction is not there once — left as it is")
        return prompt
    return prompt[:i] + DIAGRAM_ASK + prompt[j:]


def diagram_update_words(prompt, items):
    """BUILD IC-5 (D15): the keyed update's kind line for a diagram held as data (its review set's f.diagram); else as it is. BUILD IC-8
    (D4, V12c): under the switch the line ends with the master request's size line (DIAGRAM_POLICY's ask); off, IC-7's line."""
    if "f.diagram" not in (items or {}):
        return prompt
    if prompt.count(DIAGRAM_KIND_IC4) != 1:
        NR.say("    ⚠ diagram: the update request's kind line for the diagram is not there once — left as it is")
        return prompt
    return prompt.replace(DIAGRAM_KIND_IC4, DIAGRAM_KIND_D15 + (" " + DIAGRAM_POLICY["ask"] if NR.ic8_prompt() else ""))


def _dg_text(v, cap, where, default=None):
    """A diagram string as Python keeps it: plain text (its tags removed by admit, kind "diagram", each one recorded; its markup removed and
    its entities decoded by NR.clean_text; a pair of parentheses a removed tag filled goes with it; white space one space), cut at `cap`
    (REG.fit_cap: its disposition recorded; BUILD IC-8 P4.4, p44c: `default` the text when no whole sentence and no word-cut excerpt
    fits — a node's label its id). "" for no text."""
    if not isinstance(v, str):
        return ""
    t = NR.clean_text(REG.admit(v, "diagram", where))
    for e_ in ("( )", "()"):
        while e_ in t:
            t = t.replace(e_, "")
    t = " ".join(t.split())
    return REG.fit_cap(t, "oracle.diagram.text", cap, chain=[("default", default)] if default else None) if t else ""


def diagram_data(raw, where="diagram"):
    """BUILD IC-5 (D15): the diagram the model names, as Python keeps it — {"nodes": [{id, label, class, tooltip_ref}], "edges": [{from,
    to, label}]}, plain strings, unescaped (one canonical copy for both blocks, serialized per destination: G7). A node with no id or an
    id given before is left out (no link can name it); a class not of DIAGRAM_CLASSES is drawn with none; an edge whose end is no kept
    node, or one given before, is left out. BUILD IC-8 (D4): every other node is kept — no position cap (V12d: R7's update lost its two
    outcomes past the 12th step), a node with no label too (diagram_source gives it its class's word); the rules decide the drawing. → (the data, or None when no node is kept; [(what, why)], each named by the
    caller). Never raises."""
    probs = []
    if not isinstance(raw, dict):
        return None, ([(where, "not an object of nodes and edges")] if raw is not None else [])
    nodes, ids = [], set()
    for i, n in enumerate(raw.get("nodes") if isinstance(raw.get("nodes"), list) else []):
        w = f"{where}.nodes[{i}]"
        if not isinstance(n, dict):
            probs.append((w, "not an object"))
            continue
        nid = " ".join(str(n.get("id")).split()) if isinstance(n.get("id"), (str, int)) and not isinstance(n.get("id"), bool) else ""
        lab = _dg_text(n.get("label"), DIAGRAM_LABEL_CHARS, f"{w}.label", default=nid or None)     # P4.4 (p44c): never an empty box
        why = "no id" if not nid else f"its id {nid[:30]!r} is given before" if nid in ids else None     # BUILD IC-8 (D4)
        if why:
            probs.append((w, why))
            continue
        cls = str(n.get("class") or "").strip().lower() if isinstance(n.get("class"), str) else ""
        if cls and cls not in DIAGRAM_CLASSES:
            probs.append((f"{w}.class ({cls[:20]})", "not one of " + ", ".join(DIAGRAM_CLASSES) + " — drawn with no class"))
            cls = ""
        ids.add(nid)
        nodes.append({"id": nid, "label": lab, "class": cls,
                      "tooltip_ref": " ".join(n["tooltip_ref"].split()) if isinstance(n.get("tooltip_ref"), str) else ""})
    if not nodes:
        return None, probs + [(where, "no node to draw")]
    edges, seen = [], set()
    for i, e in enumerate(raw.get("edges") if isinstance(raw.get("edges"), list) else []):
        w = f"{where}.edges[{i}]"
        if not isinstance(e, dict):
            probs.append((w, "not an object"))
            continue
        a, b = (" ".join(str(e.get(k)).split()) if isinstance(e.get(k), (str, int)) and not isinstance(e.get(k), bool) else "" for k in ("from", "to"))
        lab = _dg_text(e.get("label"), DIAGRAM_EDGE_CHARS, f"{w}.label")
        why = (f"its end {(a if a not in ids else b)[:30]!r} is no node of the diagram" if a not in ids or b not in ids else
               "given before" if (a, b, lab) in seen else None)
        if why:
            probs.append((w, why))
            continue
        seen.add((a, b, lab))
        edges.append({"from": a, "to": b, "label": lab})
    return {"nodes": nodes, "edges": edges}, probs


def diagram_source(dd, reg, ex=None, outcomes=None):
    """BUILD IC-5 (D15): the Mermaid code Python writes from the diagram's data — its own node ids (n1, n2, … in the data's order), each
    label's tokens resolved (the registry's plain figures) and the label quoted and entity-encoded (REG.out_text, "mermaid_label"), a
    decision a diamond, the class of DIAGRAM_CLASSES, IC-4's classDefs, an accessible title and description, and for each node whose
    tooltip_ref names registry figures a click line whose tooltip is Python's words (each figure as the registry prints it, with its
    label; no "<" or ">": a tooltip is drawn as markup). The page's configuration is strict: no callback is bound and the tooltip shows.
    A decision is a diamond ({"…"}): a hexagon's "{{" is a template's mark to the acceptance detector (tests/lib/judged_ic5.py).
    BUILD IC-8 (D4, V13c): a node whose label names no word once resolved keeps its links and takes its class's word
    (DIAGRAM_POLICY["words"]), each one in `outcomes` ({node, disposition, reason}: figure_dropped_from_label) — never left out. → (code,
    typed, events, [(what, why)]): the labels' typed figures (tokens set aside, as diagram_typed reads the model's code) and every token's
    events."""
    typed, ev, notes, labels = [], [], [], {}

    def res(t):
        r, e_ = reg.resolve(t, mode="plain")
        ev.extend(e_ or [])
        r = r.replace(REG.SENTINEL, "") if isinstance(r, str) else ""
        return " ".join(REG.strip_tokens(r).split())

    def typed_of(t):
        return [x["words"] for x in REG.quantities(REG.TOKEN_RE.sub(" ", t), names=ex) if x["role"] == "quantity"] if t else []
    for n in dd.get("nodes") or []:
        typed += typed_of(n["label"])
        lab = res(n["label"])
        if not lab:          # BUILD IC-8 (D4, V13c): its class's word; its links stay
            lab = DIAGRAM_POLICY["words"].get(n.get("class") or "", DIAGRAM_POLICY["words"][""])
            notes.append((f"the diagram's node {n['id'][:30]!r}", f"its label names no word once resolved — shown as {lab!r}"))
            if outcomes is not None:
                outcomes.append({"node": n["id"], "disposition": "fallback_default", "reason": "figure_dropped_from_label"})
        labels[n["id"]] = lab
    kept = [n for n in dd.get("nodes") or [] if n["id"] in labels]
    pid = {n["id"]: f"n{k}" for k, n in enumerate(kept, 1)}
    out = ["flowchart TD", "  accTitle: The workflow diagram", "  accDescr: The program's steps and how they connect"]
    for n in kept:
        q = '"' + REG.out_text(labels[n["id"]], "mermaid_label") + '"'
        out.append(f"  {pid[n['id']]}" + ("{" + q + "}" if n["class"] == "decision" else "[" + q + "]") + (f":::{n['class']}" if n["class"] else ""))
    for e in dd.get("edges") or []:
        if e["from"] not in pid or e["to"] not in pid:
            continue
        typed += typed_of(e["label"])
        lab = res(e["label"]) if e["label"] else ""
        out.append(f"  {pid[e['from']]}" + (f' -->|"{REG.out_text(lab, "mermaid_label")}"| ' if lab else " --> ") + pid[e["to"]])
    out += list(DIAGRAM_CLASSDEFS)
    for n in kept:
        figs = []
        for tok in REG.TOKEN_RE.findall(n.get("tooltip_ref") or ""):
            ent = reg.lookup(tok)[0]
            if not ent:
                notes.append((f"the diagram's node {n['id'][:30]!r} tooltip ([[{tok[:40]}]])", "names no figure"))
                continue
            figs.append(" ".join(f"{res('[[' + tok + ']]')} {REG.out_text(ent.get('label') or '', 'plain')}".split()))
        if figs:
            tip = REG.fit_cap(" ".join(" · ".join(figs).replace("<", " ").replace(">", " ").split()), "oracle.diagram.tip", DIAGRAM_TIP_CHARS)
            out.append(f'  click {pid[n["id"]]} call tip() "{REG.out_text(tip, "mermaid_label")}"')
    typed += [q["words"] for x in ev if x["rule"] in REG.REFUSAL_EVENTS for q in REG.quantities(x.get("text") or "") if q["role"] != "vague"]
    return "\n".join(out) + "\n", typed, ev, notes


def diagram_rules(dd):
    """BUILD IC-8 (D4, I5): the diagram's rules by a standard-library traversal — (G1) ids unique and every edge's ends steps of the
    diagram; (G2) at least one input and one outcome; (G3) every step reaches an outcome (no dead end, no closed cycle without an exit);
    (G4) every outcome is reached from an input; (G5) at most DIAGRAM_POLICY["hard"] steps. A measure, not a rule: the steps no input
    reaches (R8, R9: "landlords" in both live diagrams). The verdict does not depend on the steps' order. → {"ok", "failed": [(rule,
    words)], "measures": {"unreached_from_input": [ids, in the data's order]}}."""
    nodes = [n for n in ((dd or {}).get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in ((dd or {}).get("edges") or []) if isinstance(e, dict)]
    ids = [n.get("id") for n in nodes]
    have, failed = set(ids), []
    if len(have) != len(ids) or any(e.get("from") not in have or e.get("to") not in have for e in edges):
        failed.append(("G1", "an id given twice, or a link whose end is no step"))
    cls = {n.get("id"): n.get("class") for n in nodes}
    ins, outs = [i for i in ids if cls.get(i) == "input"], [i for i in ids if cls.get(i) == "outcome"]
    if not ins or not outs:
        failed.append(("G2", "no input" if not ins else "no outcome"))
    fwd, back = {}, {}
    for e in edges:
        fwd.setdefault(e.get("from"), []).append(e.get("to"))
        back.setdefault(e.get("to"), []).append(e.get("from"))

    def reach(starts, adj):
        seen, todo = set(starts), list(starts)
        while todo:
            for y in adj.get(todo.pop(), ()):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        return seen
    to_out, from_in = reach(outs, back), reach(ins, fwd)
    dead = [i for i in ids if i not in to_out]
    if dead:
        failed.append(("G3", "a step that reaches no outcome: " + ", ".join(str(x)[:30] for x in dead[:4])))
    lost = [i for i in outs if i not in from_in]
    if lost:
        failed.append(("G4", "an outcome no input reaches: " + ", ".join(str(x)[:30] for x in lost[:4])))
    if len(ids) > DIAGRAM_POLICY["hard"]:
        failed.append(("G5", f"{len(ids)} steps, more than {DIAGRAM_POLICY['hard']}"))
    return {"ok": not failed, "failed": failed, "measures": {"unreached_from_input": [i for i in ids if i not in from_in]}}


def diagram_checks(dd, reg, ex=None):
    """BUILD IC-8 (D4): diagram_rules, and (M1) the source Python writes for it within DIAGRAM_POLICY["mermaid_max"] characters."""
    g = diagram_rules(dd)
    if dd and not any(r == "G1" for r, _w in g["failed"]):
        n_ = len(diagram_source(dd, reg, ex)[0])
        if n_ > DIAGRAM_POLICY["mermaid_max"]:
            g = dict(g, ok=False, failed=g["failed"] + [("M1", f"its written source is {n_:,} characters, more than {DIAGRAM_POLICY['mermaid_max']:,}")])
    return g


def diagram_outcome(g, base=None, op=None, block2=False):
    """BUILD IC-8 (D4): the diagram's one outcome — g: the checks of the diagram as written (diagram_checks); base: of Block 1's diagram
    (Block 2), or None; op: the update's status for f.diagram. → (what is drawn: "candidate", "base" or "steps"; its disposition; a fixed
    reason). An edit not applied: Block 1's stands. Pass: the whole graph (Block 1 added; Block 2 replaced or kept). Fail: Block 2 →
    Block 1's diagram re-finalized (rejected_kept_base, the failed rules); Block 1, a kept diagram that fails, or a Block 1 diagram that
    fails too → the step list (fallback_default)."""
    codes = "+".join(r for r, _w in g["failed"])
    if op == "not applied":
        return ("base", "rejected_kept_base", "edit_not_applied") if base is not None and base["ok"] else ("steps", "fallback_default", "edit_not_applied")
    if g["ok"]:
        return "candidate", ({"replaced": "replaced_valid"}.get(op, "kept_valid") if block2 else "added_valid"), "rules_pass"
    if block2 and base is not None and base["ok"] and op not in ("kept", "not reviewed"):
        return "base", "rejected_kept_base", codes
    return "steps", "fallback_default", codes


def _block1_ma():
    """BUILD IC-8 (D3, D4): Block 1's analysis as this run's update read it (the change list's Block 1 run; curveball_record reads it so),
    else {}."""
    changes_, _w = _changes_from_env()
    chal_ = os.environ.get("NAIPC_CHALLENGE", "").strip() or (changes_ or {}).get("challenge")
    b_, _w = load_block1_base(chal_, (changes_ or {}).get("block1_run_id"))
    return b_.get("ma") if isinstance(b_, dict) and isinstance(b_.get("ma"), dict) else {}


# ── BUILD IC-6 (D10, G11): the diagram as a list — for a reader that runs no script (a judge's reader, a screen reader): every step
# with its label (and its tooltip, where it has one) and every link "A → B (label)", read from the Mermaid code the page draws (Python's
# code from the diagram's data, or the model's code), each figure resolved as the drawing resolves it. The template prints it under the
# diagram, escaped, outside the integrity count (it repeats the diagram's words). R6's Block 2 diagram was drawn in the browser: mmdc
# was not found (on Windows a bare "mmdc" is not run — CreateProcess applies no PATHEXT; NR.find_tool resolves mmdc.cmd).
_MM_SKIP = re.compile(r"^\s*(?:%%|(?:flowchart|graph)\b|classDef\b|class\s|style\s|linkStyle\b|subgraph\b|end\s*$|accTitle\b|accDescr\b|"
                      r"direction\b)", re.I)
_MM_NODE_ID = re.compile(r"\s*([A-Za-z0-9_][A-Za-z0-9_-]*)")
_MM_SHAPES = (("(((", ")))"), ("([", "])"), ("[[", "]]"), ("[(", ")]"), ("((", "))"), ("{{", "}}"), ("[/", "/]"), ("[/", "\\]"),
              ("[\\", "\\]"), ("[\\", "/]"), ("[", "]"), ("(", ")"), ("{", "}"), (">", "]"))
_MM_LINK = re.compile(r"\s*(?:(?:--|==|-\.)\s*(?P<text>[^\s|>.=-][^|]*?)\s*(?:-{2,}>|={2,}>|\.-+>|-{3,}|={3,}|\.-)"
                      r"|(?P<arrow>[<ox]?(?:-{2,}|={2,}|-\.+-)[>ox]?)(?:\s*\|(?P<lab>[^|]*)\|)?)\s*")
_MM_ENT = {"quot": '"', "amp": "&", "lt": "<", "gt": ">", "nbsp": " "}


def _mm_words(t):
    t = re.sub(r"#(\d+|quot|amp|lt|gt|nbsp);", lambda m: chr(int(m.group(1))) if m.group(1).isdigit() else _MM_ENT[m.group(1)], t or "")
    return " ".join(re.sub(r"(?i)<br\s*/?>", " ", t).split())


def _mm_node(s, i):
    m = _MM_NODE_ID.match(s, i)
    if not m:
        return None
    nid, j, lab = m.group(1), m.end(), None
    for op, cl in _MM_SHAPES:
        if s.startswith(op, j):
            k = j + len(op)
            if k < len(s) and s[k] == '"':
                q = s.find('"', k + 1)
                e = s.find(cl, q + 1) if q >= 0 else -1
                lab = s[k + 1:q] if e >= 0 else None
            else:
                e = s.find(cl, k)
                lab = s[k:e] if e >= 0 else None
            if e < 0:
                return None
            j = e + len(cl)
            break
    m2 = re.match(r":::[\w-]+", s[j:])
    return nid, lab, j + (m2.end() if m2 else 0)


def mermaid_parts(code):
    """BUILD IC-6 (D10): a flowchart's steps and links as Mermaid reads them — ([{id, label, tip}] in order of first appearance,
    [(from id, to id, label)]): node shapes with quoted or bare labels, :::class, "&" groups, chains (A --> B --> C), links with |label|
    or "-- label -->", click tooltips; subgraph, class, style and comment lines skipped; a statement it cannot read is left out from
    there. Never raises."""
    nodes, order, edges = {}, [], []

    def add(nid, lab):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": None, "tip": ""}
            order.append(nid)
        if lab is not None and nodes[nid]["label"] is None:
            nodes[nid]["label"] = _mm_words(lab)
    for line in str(code or "").splitlines():
        if not line.strip() or _MM_SKIP.match(line):
            continue
        if re.match(r"\s*click\s", line):
            m = re.match(r"\s*click\s+([A-Za-z0-9_][A-Za-z0-9_-]*)", line)
            q = re.findall(r'"([^"]*)"', line)
            if m and q:
                add(m.group(1), None)
                nodes[m.group(1)]["tip"] = _mm_words(q[-1])
            continue
        parts, cur, inq = [], "", False
        for ch in line:
            if ch == '"':
                inq = not inq
            if ch == ";" and not inq:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        for st in parts:
            i, prev = 0, None
            while i < len(st) and st[i:].strip():
                group = []
                while True:
                    r = _mm_node(st, i)
                    if not r:
                        break
                    nid, lab, i = r
                    add(nid, lab)
                    group.append(nid)
                    m = re.match(r"\s*&\s*", st[i:])
                    if not m:
                        break
                    i += m.end()
                if not group:
                    break
                if prev:
                    edges += [(a, b, prev[1]) for a in prev[0] for b in group]
                m = _MM_LINK.match(st, i)
                if not m or m.end() == i or not (m.group("text") or m.group("arrow")):
                    break
                prev, i = (group, _mm_words((m.group("text") or m.group("lab") or "").strip().strip('"'))), m.end()
    return [nodes[k] for k in order], edges


def diagram_list(code, reg=None):
    """BUILD IC-6 (D10, G11): the diagram as a list — {"nodes": [{label, tip}], "edges": [{a, b, label}]} from the Mermaid code the page
    draws (mermaid_parts), each label's and tooltip's tokens resolved as the drawing resolves them (the registry's plain figures), plain
    text (the template escapes it); a step with no label shows its id, as Mermaid draws it. None when the code holds no step."""
    def res(t):
        if not t:
            return ""
        if reg is not None and "[[" in t:
            t, _e = reg.resolve(t, mode="plain")
            t = t.replace(REG.SENTINEL, "") if isinstance(t, str) else ""
        return " ".join(REG.unmark(REG.strip_tokens(t)).split())
    try:
        nodes, edges = mermaid_parts(_html.unescape(code or ""))
    except Exception:  # noqa: BLE001 — no list rather than a failed page
        return None
    if not nodes:
        return None
    lab = {n["id"]: res(n["label"]) or n["id"] for n in nodes}
    return {"nodes": [{"label": lab[n["id"]], "tip": res(n["tip"])} for n in nodes],
            "edges": [{"a": lab[a], "b": lab[b], "label": res(t)} for a, b, t in edges]}


@stage("diagram_generation", depends_on=["data_loading", "master_analysis", "financial_model"])
def stage_diagram_generation(deps: dict) -> dict:
    """Generate and render Mermaid diagram. BUILD B3 (D2): its tokens become plain figures before it is drawn (it runs after the financial
    model, whose figures they cite); "traced" when every figure in its labels came from a token (the page then counts it as one block)."""
    ma = deps["master_analysis"]
    if has_diagram_data(ma):        # BUILD IC-5 (D15): the analysis names its diagram as data — Python writes the code
        reg = build_registry(deps)
        ex = REG.Exempter.from_data((deps.get("data_loading") or {}).get("raw") or {}, NR.records)
        # BUILD IC-8 (D4, I5): the outcome first — the checks (G1-G5, M1) on the diagram as written; Block 2: the update's diagram, else
        # Block 1's re-finalized (rejected_kept_base); Block 1, or no passing base: the steps listed (fallback_default)
        dd, base_dd, op_ = ma["diagram"], None, None
        if is_block2():
            b_ = _block1_ma()
            base_dd = b_["diagram"] if has_diagram_data(b_) else None
            op_ = ((((ma.get("curveball_update") or {}).get("edits") or {}).get("status")) or {}).get("f.diagram")
        g = diagram_checks(dd, reg, ex)
        gb = diagram_checks(base_dd, reg, ex) if base_dd else None
        use, disp_, reason_ = diagram_outcome(g, gb, op_, is_block2())
        for r_, w_ in g["failed"]:
            NR.say(f"    diagram: rule {r_} fails — {w_}")
        if use == "base":
            NR.say("    diagram: Block 1's diagram is drawn — " + ("the update's edit was not applied" if reason_ == "edit_not_applied" else
                                                                  f"the update's fails {reason_}"))
        elif use == "steps":
            NR.say(f"    diagram: not drawn — the workflow is listed as steps ({reason_})")
        drawn = base_dd if use == "base" else dd
        unr = (gb if use == "base" else g)["measures"]["unreached_from_input"]
        if unr:
            NR.say(f"    diagram: {len(unr)} step(s) no input reaches (a measure): {', '.join(str(x)[:30] for x in unr[:6])}")
        node_out = []
        code, untraced, ev, notes = diagram_source(drawn, reg, ex, outcomes=node_out)
        for w_, why_ in notes:
            NR.say(f"    diagram: {w_} — {why_}")
        gone = [x["token"] for x in ev if x["rule"] in ("unknown", "malformed", "calc")]
        if gone:
            NR.say(f"    diagram: {len(gone)} token(s) name no figure — left blank: {', '.join(gone[:4])}")
        if untraced:
            NR.say(f"    diagram: {len(untraced)} figure(s) typed, not cited ({', '.join(untraced[:5])}) — counted as untraced on the page")
        left, why_ = 900 - 50 - (_clock() - _PROCESS_T0), []
        if use == "steps":
            svg = None
        elif left // 35 < 1:
            NR.say(f"    diagram: not rendered here — {left:.0f} s left of the Oracle's time; the page draws it in the browser")
            svg = None
            why_.append(MMDC_WHY["time"])
        else:
            svg = render_mermaid_svg(code, max_retries=0, repair=False, why=why_)
            n_svg = len(re.findall(r'<g\b[^>]*\bclass="node\b', svg or ""))
            if svg and n_svg != len(drawn.get("nodes") or []):      # BUILD IC-8 (D4): a silent one-step drawing is no drawing
                NR.say(f"    diagram: the drawing holds {n_svg} step(s), the data {len(drawn.get('nodes') or [])} — not used; the page's browser draws it")
                svg = None
        return {"svg": svg, "mermaid": code if svg is None and use != "steps" else None, "traced": not untraced, "untraced": untraced,
                "forms": diagram_forms(ev), "drawn_from": "data", "why": why_[0] if svg is None and why_ else None, "code": code,
                # BUILD IC-8 (D4): the outcome (ids and codes), what was drawn, and the steps each listed without a drawing
                "steps": use == "steps", "dd": drawn, "node_units": node_out,
                "outcome": {"disposition": disp_, "reason": reason_, "source": {"candidate": "candidate", "base": "base"}.get(use, "default"),
                            "op": op_},
                "rules": {"failed": [r_ for r_, _w in g["failed"]], "unreached_from_input": unr}}
    mermaid_code = REG.admit(ma.get("mermaid_code", ""), "diagram", "diagram")   # BUILD IC-5 (D1 f): the page's diagram, no tag (both modes)
    if not mermaid_code:
        return {"svg": None, "mermaid": None}
    reg = build_registry(deps)
    ex = REG.Exempter.from_data((deps.get("data_loading") or {}).get("raw") or {}, NR.records)
    untraced, ev, mermaid_code = diagram_typed(mermaid_code, reg, ex)
    gone = [x["token"] for x in ev if x["rule"] in ("unknown", "malformed", "calc")]       # IC-2 (D6): a comparison that failed too
    if gone:
        NR.say(f"    diagram: {len(gone)} token(s) name no figure — left blank: {', '.join(gone[:4])}")
    mermaid_code = REG.strip_tokens(mermaid_code)     # BUILD IC-5 (D3, class T): a refused token goes with its id (never the id alone),
    #                                                     and a token's half or a bare pair of brackets the model wrote goes too
    if untraced:
        NR.say(f"    diagram: {len(untraced)} figure(s) typed, not cited ({', '.join(untraced[:5])}) — counted as untraced on the page")
    # review F4: a diagram the curveball update amended is stored HTML-escaped for the page's <pre class="mermaid"> (D11; the browser
    # decodes it) — mermaid-cli reads the code itself, so it gets the decoded text; Block 1's code and the page's copy are unchanged
    import html as _html
    amended = {a.get("field") for a in ((ma.get("curveball_update") or {}).get("amended") or []) if isinstance(a, dict)}
    # B3 (Phase 4, review c F5): the renderer's tries (30 s each) fit what is left of the pipeline's 900 s Oracle limit, less 50 s for
    # html_assembly, deployment and a margin — the deliverables call before this stage may have used its whole time; with no time left
    # for one try the page draws the diagram in the browser (the mermaid code, as on a failed render)
    left = 900 - 50 - (_clock() - _PROCESS_T0)
    tries, why_ = min(4, int(left // 35)), []
    if tries < 1:
        NR.say(f"    diagram: not rendered here — {left:.0f} s left of the Oracle's time; the page draws it in the browser")
        svg = None
        why_.append(MMDC_WHY["time"])
    else:
        svg = render_mermaid_svg(_html.unescape(mermaid_code) if "mermaid_code" in amended else mermaid_code, max_retries=tries - 1, why=why_)
    forms = diagram_forms(ev)
    # IC-3 (ledger steps 17-18): the full list — the page's U counts every typed figure (the cut at 20 left 34, 14 and 13 of the fixture
    # diagrams out of U); the console line above shows five
    return {"svg": svg, "mermaid": mermaid_code if svg is None else None, "traced": not untraced, "untraced": untraced, "forms": forms,
            "why": why_[0] if svg is None and why_ else None,      # BUILD IC-6 (D10): the page's words when the browser draws it
            "code": _html.unescape(mermaid_code) if "mermaid_code" in amended else mermaid_code}      # the code it drew (the list's)


# ─────────────────────────────────────────────────────────────────────
# STAGE 5: HTML ASSEMBLY — Jinja2 rendering
# Per Implementation Guide §5 and Chart.js doc
# ─────────────────────────────────────────────────────────────────────
def build_chatbot_context(ma: dict, fin: dict, brief: str = "", F: dict = None, curveball: str = "", engine=None, funds=None) -> str:
    """Build a compact text summary of the analysis for the chatbot system prompt.
    P0.3 (D10): called with ma_fixed (the text the page shows); EVERY field is stripped of tags — the second render adds ZIP
    links the first does not, and a field left raw would make the two renders write two context files; plus the brief block.
    P0.6 (D1, D2): the budget with its source (the brief's words, an override or our proposal), the pilot as the brief states it
    (or "not stated"), the brief's unit — from the financial model and the contract only, never from the data (one file per run)."""
    def strip(html_str): return NR.clean_text(html_str if isinstance(html_str, str) else ("" if html_str is None else str(html_str)))
    F = F or NR.facts(None)
    k = fin.get("kpis", {}) or {}
    src = k.get("contract_source")
    if k.get("contract") is not None and src:
        money = f"{NR.BUDGET_SOURCES.get(src, 'Budget')}: ${k['contract']:,}" + (f' (the brief: "{F["contract_quote"]}")' if src == "brief" and F.get("contract_quote") else "")
    else:
        money = "Budget: not stated in the brief"
    lines = [
        f"Program: {strip(ma.get('program_name', 'Unknown'))}",
        f"City: {strip(ma.get('city_name', 'Unknown'))}",
        money,
        (f'Pilot: {F["pilot_days"]} days (the brief: "{F["pilot_quote"]}")' if F.get("pilot_days") else "Pilot: not stated in the brief"),
    ]
    if F.get("marketing"):
        lines.append(f'Marketing reserve: ${F["marketing"]:,} (the brief: "{F["marketing_quote"]}")')
    lines += [
        f"Beneficiary unit: {F['unit_plural']}" + ("" if F.get("unit_stated") else " (the brief names none)"),
        "",
        "EXECUTIVE SUMMARY:",
        strip(ma.get('bluf_text', '')),
        "",
        "CRISIS:",
        strip(ma.get('crisis_body', '')),
        strip(ma.get('crisis_human', '')),
        "",
        "SOLUTION:",
        strip(ma.get('solution_body', '')),
        strip(ma.get('solution_population', '')),
        "",
        "COST OF INACTION:",
        strip(ma.get('cost_of_inaction', '')),
        "",
        "KEY METRICS:",
    ]
    kpis = fin.get("kpis", {})
    lines.append(f"  {F['unit_plural'][:1].upper() + F['unit_plural'][1:]} in Year 1: {kpis.get('beneficiaries_y1', 0):,}")
    lines.append(f"  Cost per {F['unit_singular']}: {REG.fmt_value(kpis.get('cpb_y1', 0), 'money')}")
    lines.append(f"  SROI ratio: {REG.fmt_value(kpis['sroi'], 'ratio')}" + (" (below 1: less counted social value than cost, on the outcomes modeled)"
                 if kpis["sroi"] < 1 else "") if isinstance(kpis.get("sroi"), (int, float)) else "  SROI: not computed (no outcomes the model could use)")
    if fin.get("sroi_partial"):        # BUILD IC-4 (D6): the ratio is never shown as complete when the list says partial
        lines.append("    partial — material, not valued: " + "; ".join(f"{NR.clean_text(r_.get('outcome'))} ({NR.clean_text(r_.get('why'))})"
                                                                     for r_ in fin.get("materiality") or [] if r_.get("status") == "material, not valued"))
    lines.append("")
    lines.append("EVIDENCE FINDINGS:")
    for card in ma.get("evidence_cards", []):
        lines.append(f"  - {strip(card.get('title', ''))}: {strip(card.get('body', ''))} (source: {strip(card.get('source', ''))})")   # B3: never "[[ESTIMATE]]"
    lines.append("")
    lines.append("FINANCIAL MODEL (Base Case):")
    base_scenario = fin.get("scenarios", {}).get("base", {})
    for row in base_scenario.get("pl_rows", []):
        cells = row.get("cells", [])
        lines.append(f"  {strip(row.get('label', ''))}: {', '.join(strip(c) for c in cells)}")
    lines.append("")
    lines.append("SROI DERIVATION:")
    for row in fin.get("sroi_rows", []):     # B3 (D1): unit value × rate × each year's people served, over the outcome's duration
        lines.append(f"  {strip(row.get('category', ''))}: {strip(row.get('proxy', ''))} per outcome × {strip(row.get('rate', ''))} of people served"
                     f" ({strip(row.get('outcomes', ''))} outcomes in Years 1–3), lasting {strip(row.get('duration', ''))} year(s);"
                     f" Year 1 {strip(row.get('annual', ''))} (source: {strip(row.get('source', ''))})")
    lines.append(f"  PV Benefits: {fin.get('sroi_pv_total', '')}")
    lines.append(f"  PV Investment: {fin.get('sroi_pv_cost', '')}")
    lines.append(f"  Ratio: {fin.get('sroi_display', '')}")
    lines.append("")
    lines.append("IMPLEMENTATION:")
    for phase in ma.get("phases", []):
        lines.append(f"  {strip(phase.get('summary', ''))}: {REG.fit_cap(strip(phase.get('content', '')), 'oracle.chat.phase', 200)}")  # IC-8 (D1)
    lines.append("")
    lines.append("RISKS:")
    for risk in ma.get("risks", []):
        lines.append(f"  {strip(risk.get('risk', ''))}: {strip(risk.get('likelihood', ''))}/{strip(risk.get('impact', ''))} — {strip(risk.get('mitigation', ''))}")
    lines += engine_chat_lines(engine, funds)   # BUILD B1: the prioritization model and the use of funds, as the page shows them
    if brief:
        lines += ["", brief]
    if curveball:   # P0.7 (D3, D7): Block 2 only — the announcement, the change summary, the moved figures, the test answers
        lines += ["", curveball]

    return "\n".join(lines)

# ── P0.11b chat: the proxy's context file (the page carries only its digest; no key anywhere) ──
CHAT_PROXY_URL_DEFAULT = "https://naipc-chat.swaldrop2.workers.dev"


def deliverables_chat(dview, kf, bn, yo, rg, reg, hon=None, sg=None, rub=None, inc=None) -> str:
    """BUILD B3 (D13): the new sections in the chat context, resolved to plain text, and the registry's figures with their kinds — Phase
    3's too: the honesty panel, the sensitivity grid, the rubric map and (Block 2) incident mode."""
    t = lambda s: NR.clean_text(s if isinstance(s, str) else ("" if s is None else str(s)))   # noqa: E731
    out = []
    if kf.get("rows") or kf.get("spend"):
        out.append("PILOT KPI FRAMEWORK:")
        out += [f"  {t(r['indicator'])} ({r['type']}): baseline {t(r['baseline'])}, target {t(r['target'])} by day {t(r['day'])}, {t(r['cadence'])}, "
                f"owner {t(r['owner'])}, source {t(r['source'])}" for r in kf.get("rows") or []]
        out += [f"  Pilot spend, {t(s['label'])}: {t(s['amount'])}" for s in kf.get("spend") or []]
        if kf.get("spend_total"):
            out.append(f"  Pilot spend, total (the unallocated contingency left out): {t(kf['spend_total'])}")
    if bn.get("show"):
        out.append("BOTTLENECK DASHBOARD:")
        for s in bn["sets"]:
            out.append(f"  {t(s['label'])}: listed {t(s['totals'].get('listed'))}"     # B3 (review c F2): no "in use ," without the field
                       + (f", in use {t(s['totals'].get('in_use'))}" if s['totals'].get('in_use') else "") + f", free {t(s['totals'].get('free'))}")
        out += [f"  Jam point {t(j['id'])} ({t(j['name'])}): {j['kind']} — {t(j['why'])}" for j in bn.get("jams") or []]
    if yo.get("show"):
        out.append("YOUTH HANDOFFS (age protocol):")
        out += [f"  Age {a['age']}: " + "; ".join(f"{t(s['label'])} {c['records']} record(s), {c['free']} free" for s, c in zip(yo["sets"], a["cells"]))
                for a in yo.get("ages") or []]
        out += [f"  Failure at {t(m['where'])}: {t(m['what'])} Remedy: {t(m['remedy'])} (fund line {t(m['line'])}; indicator {t(m['indicator'])})"
                for m in yo.get("modes") or []]
    if rg.get("rows"):
        out.append("RENEWAL GATES:" + (f" at least {t(rg['gate']['x'])} of {rg['gate']['y']} indicators at target by day {t(rg['gate']['day'])}"
                                       if rg.get("gate") else ""))
        out += [f"  {t(r['indicator'])}: tier {r['tier']} ({t(r['source'])})" for r in rg["rows"]]
    hon, sg, rub, inc = hon or {}, sg or {}, rub or {}, inc or {}
    if sg.get("show"):
        out.append("SENSITIVITY GRID (each assumption ±20%, the financial model run again; largest swing first):")
        out += [f"  {t(r['label'])} {t(r['low_shown'])} / {t(r['high_shown'])}: "
                + "; ".join(f"{t(lbl['label'])} {t(c['low_shown'])} to {t(c['high_shown'])}" for lbl, c in zip(sg.get("figs") or [], r["cells"]))
                for r in (sg.get("rows") or [])[:8]]
    if hon.get("show"):
        out.append("WHAT COULD PROVE US WRONG:")
        if hon.get("thesis"):
            th = hon["thesis"]
            out.append(f"  Thesis: {t(th['claim'])} Would prove it wrong: {t(th['kill'])} Tripwire: if \"{t(th['indicator'])}\" is {th.get('direction', 'below')} "
                       f"{t(th['threshold'])} by day {t(th['day'])}: {t(th['response'])}")
        out += [f"  Judge question ({t(q['dimension'])}): {t(q['question'])} Answer: {t(q['answer'])}" for q in hon.get("qa") or [] if q.get("question")]
        out += [f"  Weakest assumption: {t(w['span'])} {t(w['label'])}" + (f" — {t(w['swing'])}" if w.get("swing") else "") for w in (hon.get("weak") or [])[:6]]
    if rub.get("show"):
        out.append("RUBRIC MAP (the brief's scoring dimensions → the sections that answer them):")
        out += [f"  {t(r['dimension'])}: " + (", ".join(t(l['label']) for l in r["links"]) or "no section this run") for r in rub["rows"]]
    if inc.get("show"):
        out.append("INCIDENT MODE (Block 2): figures that moved, Block 1 → Block 2:")
        out += [f"  {t(r['label'])}: {t(r['b1'])} → {t(r['b2'])} ({t(r['why'])})" for r in (inc.get("rows") or [])[:12]]
        out += [f"  Playbook, {when}: {t(s_['text'])} [{t(s_['ids'])}]" for when, key in (("now", "now"), ("next", "next"), ("within 24 hours", "day"))
                for s_ in inc.get(key) or []]
    # BUILD IC-5 (D11, G9): figures by their label, shown value and source in words — no registry id, no kind in capitals (words the
    # chat repeated); the kind as the pages' line names it
    kw_ = {"MEASURED": "from the data", "DERIVED": "computed by Python", "MODELED": "the writer's assumption", "EXTERNAL": "from an outside source"}
    out.append("FIGURES (every figure on the page: its label, its value, where it comes from):")
    out += [f"  {t(REG.unmark(str(e.get('label') or 'a figure')))}: {e['shown']} — {kw_.get(e['kind'], 'from the data')}"
            + (f" ({t(str(e['source']))})" if e.get("source") else "")
            + (", not verified against its primary source" if e.get("verified_against_primary") == "no" else "")
            for e in sorted(reg.entries.values(), key=lambda e: (REG.KIND_RANK.get(e["kind"], 9), e["id"]))
            if not e["id"].startswith(("const.", "staff.", "b1.") + CONSTRAINT_PREFIXES)][:160]   # IC-3 (D7): the constraints' as the constants
    return ("\n" + "\n".join(out)) if out else ""


def chat_fields(track: str, context_text: str, faq: list) -> dict:
    """Template fields for chat: the context document's exact bytes, its sha256 (the page's context_id and the
    file name the proxy fetches), the track, the proxy URL (env CHAT_PROXY_URL) and a static FAQ."""
    doc = json.dumps({"schema_version": 1, "track": track, "context": REG.out_text(context_text, "plain")},    # BUILD IC-5 (D2 c): a writer
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"chatbot_enabled": True, "chat_track": track, "chat_context_doc": doc,
            "chat_context_id": hashlib.sha256(doc).hexdigest(),
            "chat_proxy_url": os.environ.get("CHAT_PROXY_URL", CHAT_PROXY_URL_DEFAULT), "chat_faq": REG.out_tree(faq, "plain"),
            "chat_tags": REG.chat_tags_table()}          # BUILD IC-5 (D11): the tag table the chat reader reads


def write_chat_context(out_dir, ctx: dict):
    """Write <out_dir>/chat-context/<sha256>.json beside index.html, so the site upload carries it. P0.7 (D9): every OTHER context
    file in that folder first moves to <harness>/chat-context-archive/<UTC stamp>/ (outside output\\, never uploaded; nothing deleted),
    so the folder holds exactly this page's context; the count is printed only when something moved."""
    doc = ctx.get("chat_context_doc")
    if not doc:
        return None
    d = Path(out_dir) / "chat-context"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ctx['chat_context_id']}.json"
    moved, dest, problem = NR.archive_contexts(d, keep=p.name)
    if moved:
        NR.say(f"    chat context: {moved} older file(s) moved to {dest}")
    if problem:
        NR.say(f"    ⚠ chat context: older files not all moved ({problem}) — the folder may hold more than one")
    note = "" if p.is_file() else NR.context_size_note(doc)   # the second render writes the same bytes: said once
    if note:
        NR.say(f"    {note}")
    p.write_bytes(doc)
    return p


def build_oracle_faq(ma: dict, fin: dict, F: dict = None, reg=None) -> list:
    """3–5 static Q&As from the page's own data (no model call) — the fallback when chat is unavailable.
    P0.3 (D10): built from ma_fixed, every field through NR.clean_text — the chat context's own rule (tags removed, words kept).
    P0.6 (D1): the reach question in the brief's unit."""
    def strip(s):
        return NR.clean_text(REG.unmark(str(s or "")))    # BUILD IC (D1): cut and joined as plain text, then marked as the model's
    mm = lambda t: REG.mark("model", t)                    # noqa: E731
    F = F or NR.facts(None)
    faq = []
    name, city = strip(ma.get("program_name", "")), strip(ma.get("city_name", ""))
    sol = strip(ma.get("solution_body", "")).split(". ")[0].rstrip(".")
    if name:
        faq.append({"q": "What is this program?", "a": f"{mm(name)}{' in ' + mm(city) if city else ''}." + (f" {mm(sol)}." if sol else "")})
    s_ = (fin.get("kpis") or {}).get("sroi")
    if isinstance(s_, (int, float)):   # B3 (D1): the computed ratio, what it measures, and below 1 said so
        tail_ = (": the present value of the counted social value divided by the present value of the three years' cost, per the SROI "
                 "derivation on this page" + (" — below break-even, the outcomes modeled return less than the program costs." if s_ < 1 else "."))
        faq.append({"q": "What is the social return on investment?", "a": REG.fmt_value(s_, "ratio") + tail_,
                    # B3 (run turn, the second R4): Python's own figures here are registry spans, as everywhere on the page
                    **({"a_html": reg.span("sroi") + _esc(tail_)} if reg is not None and reg.get("sroi") else {})})
    k = fin.get("kpis", {}) or {}
    if k.get("beneficiaries_y1"):
        faq.append({"q": f"How many {F['unit_plural']} does it reach in Year 1?",
                    "a": f"{REG.fmt_value(k['beneficiaries_y1'], 'count')} {F['unit_plural']} at {REG.fmt_value(k.get('cpb_y1', 0), 'money')} per {F['unit_singular']}.",
                    **({"a_html": f"{reg.span('beneficiaries_y1')} {_esc(F['unit_plural'])} at {reg.span('cpb_y1')} per {_esc(F['unit_singular'])}."}
                       if reg is not None and reg.get("beneficiaries_y1") and reg.get("cpb_y1") else {})})
    risks = [mm(strip(r.get("risk", ""))) for r in (ma.get("risks") or []) if isinstance(r, dict) and r.get("risk")]
    if risks:
        faq.append({"q": "What are the main risks?", "a": NR.join_clauses(risks[:2] + ["the risk section shows the mitigations."])})   # IC-4 (D8)
    faq.append({"q": "Where do the numbers come from?", "a": "From the challenge's data endpoints and the financial model shown on this page."})
    return faq[:5]


def generate_qr_svg(url: str) -> str:
    """Generate a QR code as inline SVG. Dark modules on white background per
    Nielsen Norman Group guidance ('Do Not Invert QR-Code Colors'). 4-module
    quiet zone per DENSO WAVE spec. Falls back to empty string if segno missing."""
    try:
        import segno, io, re as _re
        qr = segno.make(url, error="m")  # Medium error correction (~15% restore)
        buf = io.BytesIO()
        qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
        svg = REG.inline_svg(buf.getvalue().decode("utf-8"))       # BUILD IC-4 (D7, D8): no XML declaration in the body
        # Strip XML declaration for inline HTML embedding
        svg = _re.sub(r'<\?xml[^?]*\?>\s*', '', svg)
        return svg
    except (ImportError, Exception):
        return ""


_SPAN_RE = re.compile(r'<span class="prov[^"]*" data-prov="[^"]*" data-kind="[A-Z]+" title="[^"]*">[^<]*</span>'
                      r'(?:<sup class="unv"[^>]*>&#8224;</sup>)?(?: <sup class="sd"[^>]*>sources disagree</sup>)?')   # IC-4 (D7): its markers


def _panel_html(h) -> str:
    """B3 (run turn R4, finding 2): a brief answer for the panel — P0.3's plain text (NR.clean_text) with its registry spans kept, so a
    cited figure stays cited (the panel showed every one as an untraced number); the rest escaped."""
    held = []

    def hold(m):
        held.append(m.group(0))
        return f"\ue003{len(held) - 1}\ue004"
    t = _esc(NR.clean_text(_SPAN_RE.sub(hold, h)))
    return re.sub(r"\ue003(\d+)\ue004", lambda m: held[int(m.group(1))], t)


def _brief_for_page(ma_fixed: dict):
    """(contract or None, {item id: answer}) for the panel and the chat (D8, D10): the corrected answers as ma_fixed holds them. They
    are cleaned exactly once, where they are shown (panel_rows, brief_chat_block): a second pass would strip text that only looked
    like a tag after the first pass decoded its entities."""
    doc = _brief_contract()
    if not doc:
        return None, {}
    ans = {}
    for e in ma_fixed.get("brief_answers") or []:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and NR.clean_text(REG.unmark(e.get("answer"))):   # IC (review a F5): marks are no words
            ans[e["id"]] = e["answer"]
    return doc, ans


def _oracle_present(fin: dict, ma_fixed: dict, prio=None, funds=None, b3=None) -> set:
    """The catalogue blocks this render shows (several render only on a condition, template.html.j2): panel links go only to these.
    BUILD B3 (D13): each new block only when it has content; the honesty panel, the rubric map and the sensitivity grid come in Phase 3."""
    names = {n for n, *_ in NR.SECTIONS["oracle"]}
    b3 = b3 or {}
    gone = {n for n, ok in (("sroi", fin.get("sroi_rows")), ("simulator", fin.get("simulator")), ("risks", ma_fixed.get("risks")),
                             ("renewal", ma_fixed.get("renewal_text")), ("prioritization", (prio or {}).get("show")),
                             ("use-of-funds", (funds or {}).get("show")),   # BUILD B1: the two new blocks render on a condition
                             ("bottlenecks", (b3.get("bottlenecks") or {}).get("show")), ("youth-handoffs", (b3.get("youth-handoffs") or {}).get("show")),
                             ("kpi-framework", (b3.get("kpi-framework") or {}).get("show")), ("renewal-gates", (b3.get("renewal-gates") or {}).get("show")),
                             ("honesty", b3.get("honesty")), ("rubric", b3.get("rubric")), ("sensitivity-grid", b3.get("sensitivity-grid"))) if not ok}
    return names - gone


# ── P0.6 (D9): one badge conversion for the narrative and the SROI rows ──
_CITE = re.compile(r'\[CHALLENGE_DATA:\s*([a-zA-Z0-9_-]+(?:\s*,\s*[a-zA-Z0-9_-]+)*)\]')
STAT_CLASSES = ("bad", "warn", "good")   # BUILD IC-5 Phase 4: an evidence card's figure styles (the master request's "stat_class (bad/warn/good)")
_CITE_BARE = re.compile(r'\[CHALLENGE_DATA\s*\]')
_CITE_ANY = re.compile(r'\[CHALLENGE_DATA((?:[^\[\]]|\[[^\[\]]*\])*)\]')     # any other form, with at most one inner [...]
_SOURCES_BADGE = '<a class="src-link" href="#sources" role="doc-noteref" title="View the data sources">\U0001f4ca challenge data</a>'


def cite_badges(text, known=(), unknown=None):
    """Every [CHALLENGE_DATA: a, b] tag → a source badge to the data-sources row #src-a (the page's click handler opens it); a bare
    [CHALLENGE_DATA] → a badge to the data-sources section. Used by fix_narrative and by the SROI rows' text columns (P0.6, D9).
    P0.6 review F7: any other form the model writes ("[CHALLENGE_DATA: demographics.povertyRate]", "[CHALLENGE_DATA: a; b]",
    "[CHALLENGE_DATA: system status]") becomes a badge too — to the row of the longest known endpoint name its words begin with
    (`known`: this page's data-sources row ids), else to the data-sources section — so no raw tag reaches the page."""
    if not isinstance(text, str) or "[CHALLENGE_DATA" not in text:
        return REG.out_text(text, "plain") if isinstance(text, str) else text   # BUILD IC-5 (D2 c): the tags that stay words — out_text
    known_l = {str(k).lower() for k in known if k}

    def _link(m):
        endpoints = [e.strip().lower() for e in m.group(1).strip().split(',')]
        first, display = endpoints[0], ', '.join(REG.endpoint_words(e) for e in endpoints)   # BUILD IC-8 (D12): in words; href keeps the slug
        if known_l and first not in known_l:      # BUILD B3 (D2; §9): no row to open — the sources section, and the details name it
            if unknown is not None:
                unknown.append(display)
            return (f'<a class="src-link" href="#sources" role="doc-noteref" title="View the data sources: {display}">'
                    f'\U0001f4ca {display}</a>')
        return (f'<a class="src-link" href="#src-{first}" data-src="{first}" '
                f'role="doc-noteref" title="View source: {display}">'
                f'\U0001f4ca {display}</a>')
    text = _CITE.sub(_link, text)
    text = _CITE_BARE.sub(_SOURCES_BADGE, text)
    # a known name matches when the tag's words begin with it — "-", "_" and spaces interchangeable — and no letter or digit follows
    names = sorted({str(k).lower() for k in known if k and re.sub(r"[-_\s]+", "", str(k))}, key=len, reverse=True)
    pats = [(n, re.compile(r"[-_\s]+".join(re.escape(w) for w in re.split(r"[-_\s]+", n) if w) + r"(?![a-z0-9])")) for n in names]
    def _loose(m):
        inner = m.group(1).strip().lstrip(":").strip()
        if not inner:
            return _SOURCES_BADGE
        low = inner.lower()
        ep = next((n for n, p in pats if p.match(low)), None)
        ep_h = _html.escape(ep, quote=True) if ep is not None else ""      # BUILD IC-5 (D2 f): a slug with a contract; escaped without one
        shown = _html.escape(inner, quote=True)
        if ep is None:
            return (f'<a class="src-link" href="#sources" role="doc-noteref" title="View the data sources: {shown}">'
                    f'\U0001f4ca {shown}</a>')
        return (f'<a class="src-link" href="#src-{ep_h}" data-src="{ep_h}" role="doc-noteref" title="View source: {shown}">'
                f'\U0001f4ca {shown}</a>')
    return REG.out_text(_CITE_ANY.sub(_loose, text), "plain")        # BUILD IC-5 (D2 c): the tags that stay words — out_text


def cite_words(text):
    """BUILD IC-4 (D7): the model's source tags in plain words, for text Python prints as a label or a source line (no link there), in
    the one form the pages print them (_plain_tags: REG.out_text — "(data: shelter inventory)", "(data)", "(estimate)", "(estimate, medium
    confidence)"). No tag of the request reaches the page as typed. BUILD IC-5 Phase 4 (the replays): a registry entry's words — out_text's
    "entry" form, its tags as words and its tokens kept for build_registry's resolver (its "plain" form drops a token no resolver has read:
    run here, before the resolver, it dropped the figures the model cited — R6's "average single-adult benefit over the -month receipt")."""
    if not isinstance(text, str):
        return text
    return REG.out_text(NR.clean_text(text), "entry")


def coi_text(total) -> str:
    """P0.6 r2: the cost of inaction as the page states it — whole dollars under $1,000,000 ("$48,400"), "$X.X million" from there (the
    form P0.3 always used; with P0.6's D5 a target from the data can be 200 people, and "$0.0 million" was printed for $48,400)."""
    return f"${total / 1_000_000:.1f} million" if total >= 1_000_000 else f"${int(round(total)):,}"


# ── P0.6 (D10, Rule 7): the vendored libraries ──
_VENDOR_SEEN = {}


def vendored_ok(name: str, sha: str) -> bool:
    """True when <harness>/vendor/<name> is present with the build's sha256; otherwise one printed warning per process (the page then
    loads the library from the CDN, which needs the venue's WiFi)."""
    p = VENDOR_DIR / name
    try:
        st = p.stat()
        key = (name, st.st_size, st.st_mtime_ns)
        if key not in _VENDOR_SEEN:
            _VENDOR_SEEN[key] = hashlib.sha256(p.read_bytes()).hexdigest() == sha
        ok = _VENDOR_SEEN[key]
    except OSError:
        ok = False
    if not ok and ("warned", name) not in _VENDOR_SEEN:
        _VENDOR_SEEN[("warned", name)] = True
        NR.say(f"    ⚠ vendor/{name} is missing or changed — the page loads it from the CDN instead (needs WiFi; Rule 7)")
    return ok


def vendored_text(name: str, sha: str):
    """The vendored file's text for inlining (Chart.js), or None when it is missing or changed (the template then uses the CDN tag)."""
    return (VENDOR_DIR / name).read_bytes().decode("utf-8") if vendored_ok(name, sha) else None


def copy_vendored_mermaid(out_dir) -> bool:
    """Copy vendor/mermaid.min.js byte for byte to <output>/vendor/ and check its sha256 after the copy (a text copy would gain CRLF
    on Windows). False (and a printed warning) when it cannot — the page then uses the CDN module."""
    dst = Path(out_dir) / "vendor" / MERMAID_FILE
    try:
        if not (dst.is_file() and hashlib.sha256(dst.read_bytes()).hexdigest() == MERMAID_SHA256):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((VENDOR_DIR / MERMAID_FILE).read_bytes())
        if hashlib.sha256(dst.read_bytes()).hexdigest() == MERMAID_SHA256:
            return True
        NR.say(f"    ⚠ output/vendor/{MERMAID_FILE} does not match after the copy — the diagram loads Mermaid from the CDN")
    except OSError as e:
        NR.say(f"    ⚠ could not copy vendor/{MERMAID_FILE} ({type(e).__name__}) — the diagram loads Mermaid from the CDN")
    return False


def closing_statement(ma_fixed: dict, fin: dict, F: dict):
    """P0.6 (D1, D2): "For <money>[ and <N> days], <program> reaches <n> <units> at $<cpb> per <unit>[, returns $<sroi> for every
    dollar invested], and builds …" — a budget the brief does not state reads "a proposed … budget"; no period without a pilot; no
    SROI clause without an SROI; None without a financial model."""
    k = fin.get("kpis") or {}
    if not k:
        return None
    money = NR.money_short(k.get("contract"))
    lead = (f"For a proposed {money} budget" if k.get("contract_source") in ("proposed", "cost_plan") else f"For {money}") if money else "For this budget"
    if F.get("pilot_days"):
        lead += f" and {F['pilot_days']} days"
    name = _html.escape(NR.clean_text(ma_fixed.get("program_name")) or "this program")
    out = (f"{lead}, {name} reaches {REG.fmt_value(k.get('beneficiaries_y1', 0), 'count')} {_html.escape(F['unit_plural'])} at "
           f"{REG.fmt_value(k.get('cpb_y1', 0), 'money')} per {_html.escape(F['unit_singular'])}")      # BUILD IC-5 (D2 f): the brief's words escaped
    if isinstance(k.get("sroi"), (int, float)) and k["sroi"] >= 1:    # B3 (D1): the registry's form; below 1 the SROI block says it
        out += f", returns {REG.fmt_value(k['sroi'], 'per_dollar')} in social value for every dollar invested"
    return out + ", and builds a coordination infrastructure that no single organization can replicate."


def coi_numbers(people):
    """The cost of inaction's arithmetic: FIN_CONSTANTS' coi_er_rate (3%) of each year's people served have one avoidable ER visit at
    the benchmark's cost per visit (ext.er_visit_cost). BUILD B3 (D1): each year's people come from the P&L (one source) — P0.3 grew
    Year 1 by its own 20%. `people`: the P&L's people served per year (a Year-1 number alone counts that year only).
    {y1_visits, y2_visits, y3_visits, coi_y1, coi_y2, coi_y3, total, rate, er_cost}, or None without people served.
    P0.7: one function for the page and for the curveball's moved figures."""
    ppl = list(people) if isinstance(people, (list, tuple)) else [people]
    ppl = [p for p in ppl[:3] if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0]
    if not ppl:
        return None
    rate = FIN_CONSTANTS["coi_er_rate"][0]
    er_cost = next(b[1] for b in BENCHMARKS if b[0] == "ext.er_visit_cost")
    visits = [round(p * rate) for p in ppl] + [0] * (3 - len(ppl))
    out = {"rate": rate, "er_cost": er_cost}
    for y in range(3):
        out[f"y{y + 1}_visits"], out[f"coi_y{y + 1}"] = visits[y], visits[y] * er_cost
    out["total"] = sum(out[f"coi_y{y + 1}"] for y in range(3))
    return out


# ── P0.7 (D3, D4): what moved Block 1 → Block 2, why, and the figures Python derives from the records ──
def headline_figures(fin) -> list:
    """The figures the page shows from the financial model, in page order: (key, label, shown value) — every base-case P&L cell,
    the SROI and the 3-year cost of inaction."""
    out = []
    if not isinstance(fin, dict) or not fin:
        return out
    rows = ((fin.get("scenarios") or {}).get("base") or {}).get("pl_rows") or []
    for i, row in enumerate(rows):
        for y, cell in enumerate((row.get("cells") or [])[:3]):
            out.append((f"pl{i}.y{y + 1}", f"{NR.clean_text(row.get('label'))} — Year {y + 1}", str(cell)))
    out.append(("sroi", "SROI", str(fin.get("sroi_display") or "")))
    c = (fin["coi"] if "coi" in fin else           # BUILD IC-4 (D6): the stage's cost of inaction from the list, when it computed one
         coi_numbers(fin.get("beneficiaries") or (fin.get("kpis") or {}).get("beneficiaries_y1")))   # B3 (D1): the P&L's people served
    out.append(("coi", "Cost of inaction (3 years)", REG.fmt_value(c["total"], "money_short") if c and c["total"] > 0 else "not computed"))   # B3: coi_3yr's form
    return out


def _fin_quiet(ma: dict) -> dict:
    """The existing financial stage on a copy of an analysis, its printing silenced — an attribution step, not the run's own model."""
    with contextlib.redirect_stdout(io.StringIO()):
        return stage_financial_model({"master_analysis": copy.deepcopy(ma)})


def moved_figures(base, fin2, update: dict):
    """D3: every figure that moved Block 1 → Block 2 with the change that moved it, attributed by recomputing step by step with the
    existing financial stage: Block 1's analysis under this run's budget (a difference = the budget), then each kept assumption change in
    turn (a difference = that change's refs). Returns (moved, note)."""
    if not base or not isinstance(base.get("fin"), dict) or not isinstance(fin2, dict) or not fin2:
        return [], None
    f1 = headline_figures(base["fin"])
    f2 = {k: v for k, _l, v in headline_figures(fin2)}
    lab2 = {k: lab for k, lab, _v in headline_figures(fin2)}
    status = str(update.get("status") or "")
    reasons = {}
    note = None
    if status == "applied" or status.startswith("failed"):
        k1, k2 = base["fin"].get("kpis") or {}, fin2.get("kpis") or {}
        # the first step's difference is the budget only when the budget in use changed; otherwise Block 1's own figures were computed
        # differently (another version or setting of the financial model) — said so, never called a curveball change
        budget_why = ("Block 1's figure recomputed (not a curveball change)"
                      if (k1.get("contract"), k1.get("contract_source")) == (k2.get("contract"), k2.get("contract_source")) else
                      "budget override" if k2.get("contract_source") == "override" else "the budget")
        cur = copy.deepcopy(base["ma"])
        prev = {k: v for k, _l, v in headline_figures(_fin_quiet(cur))}
        for k, _l, v in f1:
            if prev.get(k) != v:
                reasons.setdefault(k, []).append(budget_why)
        for ch in update.get("assumption_changes") or []:
            cur[ch["field"]] = ch["new"]
            now = {k: v for k, _l, v in headline_figures(_fin_quiet(cur))}
            for k, v in now.items():
                if prev.get(k) != v:
                    reasons.setdefault(k, [])
                    reasons[k] += [r for r in ch.get("because") or [] if r not in reasons[k]]
            prev = now
        off = [k for k in f2 if prev.get(k) != f2[k]]
        if off:
            note = f"{len(off)} figure{'' if len(off) == 1 else 's'} not reproduced by the step-by-step recomputation"
    else:
        reasons = {k: ["regenerated at the curveball (" + ("--curveball-full" if status == "full" else status) + ")"] for k, _l, _v in f1}
    # a row whose label changed between the blocks (the budget's source: "Contract (the brief)" → "Budget (override)") names both
    moved = [{"key": k, "label": lab2.get(k, lab) if lab2.get(k, lab) == lab else f"{lab2[k]} (Block 1: {lab})", "block1": v1, "block2": f2.get(k),
              "because": reasons.get(k) or ["not attributed"]}
             for k, lab, v1 in f1 if f2.get(k) != v1]
    return moved, note


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _match_record(rec, rid) -> bool:
    return isinstance(rec, dict) and any(k in rec and str(rec[k]) == str(rid) for k in ("id",) + NR.NAME_KEYS)


def derived_figures(requests, raw1, raw2):
    """D3/D4: each figure the update named — the sum of `field` over the named records (or "all"), minus the sum of `minus_field` when
    given, in Block 1's data and in this run's — computed by Python, with its base (the sum of `field`) and the compared aggregate's own
    values. An unknown record id, a missing or non-numeric field, or no Block 1 data drops the figure (named). Returns (figures, dropped).
    P0.7 r2 (E2, E3); BUILD IC-8 (D1, P4.4 p44c): the label fitted by REG.fit_cap at DERIVED_LABEL_CHARS (stated in rule 6): whole
    sentences, or an excerpt cut at a word; a label with no word that fits → "derived figure N (its label is too long to show)" (its
    chain) — and `label_full` kept; a label that is not text → "derived
    figure N (its label is not text)"; none given, '' or only white space → "derived figure N (no label given)" (Phase 5, pass 4: the name
    is Python's, and says so); every reason in plain words, a name the model wrote cut to NR.NAME_CHARS; a comparison that is
    not a number in the data stays ON the figure (`compare_note`: it is shown, without the comparison) — dropped is only a figure not
    computed: [(label, label_full, reason, detail)]. P0.7 r2 Phase 5 (C3): a compare_with that is not an object naming an endpoint and a
    field, and an endpoint a block's data does not hold, are compare_notes too (never ignored, never called "not a number")."""
    out, dropped = [], []
    for j, d in enumerate(requests or []):
        lab = d.get("label") if isinstance(d, dict) else None
        label_full = (NR.clean_text(lab) if isinstance(lab, str) else "") or (f"derived figure {j + 1}" + (" (no label given)" if lab is None or isinstance(lab, str) else " (its label is not text)"))
        label = REG.fit_cap(label_full, "oracle.derived.label", DERIVED_LABEL_CHARS,      # P4.4 (p44c): no word that fits → Python's name
                            chain=[("default", f"derived figure {j + 1} (its label is too long to show)")])
        try:
            minus = d.get("minus_field")                      # Phase 5 (C17): only None or "" mean "none given"; 0, false, [] are not a field name
            ep, field, minus, ids = d.get("endpoint"), d.get("field"), (None if minus in (None, "") else minus), d.get("record_ids")
            if not isinstance(ep, str) or not isinstance(field, str) or (minus is not None and not isinstance(minus, str)):
                raise ValueError("the update did not name the endpoint and the field to sum")
            epn = NR.name_cut([ep], quote=False)
            vals = []
            for which, raw in (("Block 1", raw1), ("Block 2", raw2)):
                if not isinstance(raw, dict) or ep not in raw:
                    raise ValueError(f"{which}'s data holds no endpoint named {NR.name_cut([ep])}")
                recs = [r for r in NR.records(raw[ep]) if isinstance(r, dict)]
                if ids == "all":
                    sel = recs
                elif isinstance(ids, list) and ids and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in ids):
                    sel = []
                    for rid in ids:
                        m = [r for r in recs if _match_record(r, rid)]
                        if len(m) != 1:
                            raise ValueError(f"record {NR.name_cut([rid])} " + ("is not in" if not m else f"matches {len(m)} records in")
                                             + f" {which}'s {epn}")
                        sel.append(m[0])
                else:
                    raise ValueError('the records were not named as a list of record ids or "all"')
                if not sel:
                    raise ValueError(f"{which}'s {epn} holds no records")
                for r in sel:
                    for f in (field, minus):
                        if f is not None and not _is_num(r.get(f)):
                            raise ValueError(f"{NR.name_cut([f])} is missing or not a number in a record of {epn}")
                vals.append((sum(r[field] - (r[minus] if minus else 0) for r in sel), sum(r[field] for r in sel), [str(r.get("id", "")) for r in sel]))
            fig = {"label": label, "label_full": label_full, "endpoint": ep, "field": field, "minus_field": minus,
                   "records": vals[1][2] if ids != "all" else "all", "n_records": len(vals[1][2]), "block1": vals[0][0], "block2": vals[1][0],
                   "base1": vals[0][1] if minus else None, "base2": vals[1][1] if minus else None, "compare": None}
            cw = d.get("compare_with")
            if cw is not None and not (isinstance(cw, dict) and isinstance(cw.get("endpoint"), str) and isinstance(cw.get("field"), str)):
                fig["compare_note"] = "shown without its comparison — the comparison was not named as an endpoint and a field"   # Phase 5 (C3)
            elif cw is not None:
                cvals, absent = [], None
                for which, raw in (("Block 1", raw1), ("Block 2", raw2)):
                    if absent is None and not (isinstance(raw, dict) and cw["endpoint"] in raw):
                        absent = which                    # Phase 5 (C3): an endpoint the data does not hold, said so (not "not a number")
                    nums = [r[cw["field"]] for r in NR.records((raw or {}).get(cw["endpoint"])) if isinstance(r, dict) and _is_num(r.get(cw["field"]))]
                    cvals.append(nums[0] if len(nums) == 1 else (sum(nums) if nums else None))
                if absent:
                    fig["compare_note"] = f"shown without its comparison — {absent}'s data holds no endpoint named {NR.name_cut([cw['endpoint']])}"
                elif None in cvals:
                    fig["compare_note"] = (f"shown without its comparison — the API's {NR.name_cut([cw['field']])} "
                                           f"({NR.name_cut([cw['endpoint']], quote=False)}) is not a number in the data")
                else:
                    fig["compare"] = {"endpoint": cw["endpoint"], "field": cw["field"], "block1": cvals[0], "block2": cvals[1]}
            out.append(fig)
        except ValueError as e:
            dropped.append((label, label_full, str(e), ""))
        except Exception as e:  # noqa: BLE001
            dropped.append((label, label_full, "not computable", type(e).__name__))
    return out, dropped


def _plain_tags(s) -> str:
    """The model's citation tags as plain words (the section prints text, never markup): [CHALLENGE_DATA: a] → (data: a)."""
    return REG.out_text(NR.clean_text(s), "plain")      # BUILD IC-5 (D2 c): the one conversion (its forms: naipc_registry.out_text)


def _resolve_update_texts(cb, results):
    """BUILD B3 (D2): the update's own words — the section, the chat and curveball.json — with every token as its plain figure from this
    run's registry; a sentence whose token names nothing is left out. (P2.9 renders them as registry spans.)"""
    keys = ("response_summary", "what_changed", "test_answers", "edits")      # BUILD IC-4 (D11): the edits' item words too
    if not isinstance(cb, dict) or "[[" not in json.dumps([cb.get(k) for k in keys], ensure_ascii=False):
        return cb
    reg = build_registry(results)
    cb = copy.deepcopy(cb)
    if isinstance(cb.get("response_summary"), str):          # BUILD IC-8 (D3): each under THE unit rule and chain (_plain_unit)
        cb["response_summary"] = _plain_unit(reg, cb["response_summary"])
    for w in cb.get("what_changed") or []:
        if isinstance(w, dict) and isinstance(w.get("impact"), str):
            w["impact"] = _plain_unit(reg, w["impact"])
    for a in cb.get("test_answers") or []:
        for f in ("test", "answer"):
            if isinstance(a, dict) and isinstance(a.get(f), str):
                a[f] = _plain_unit(reg, a[f], REG.ANSWER_DEFAULT if f == "answer" else None)
    for it in ((cb.get("edits") or {}).get("items") or {}).values() if isinstance(cb.get("edits"), dict) else ():
        if isinstance(it, dict) and isinstance(it.get("words"), str):      # BUILD IC-4 (D11): an item's first words, as the page names it
            it["words"] = _plain_unit(reg, it["words"], omittable=True)    # (none: the page names the item another way)
    return cb


FACT_SKIP = ("b1.", "q.", "model.", "calc.", "kpi.", "const.", "ext.", "renewal.", "tripwire.", "pilot_spend.", "prio.")


def facts_moved_of(results: dict) -> dict:
    """BUILD IC-4 (D3, F13): the facts that moved at the curveball, computed from the two registries — every figure of Block 1's registry
    (checkpoints/block1/registry.json) read again on this run's (build_registry: the data's by grammar, Python's from this run's records),
    grouped: "the data" (MEASURED: a field, a sum, a count) and "Python's figures" (DERIVED from the records: a set's listed, in-use and
    free totals …). The financial model's figures are moved_figures' (the record's "moved"). Never raises: {"data": [...], "python": [...],
    "compared": bool}, each item {id, label, block1, block2}."""
    out = {"data": [], "python": [], "compared": False}
    try:
        chal = os.environ.get("NAIPC_CHALLENGE", "").strip() or None
        b1, _w = REG.read(CHECKPOINT_DIR / NR.BLOCK1_DIR / REG.REGISTRY_FILE, chal)
        if b1 is None:
            return out
        now = build_registry(results)
        out["compared"] = True
        for k, e in sorted(b1.entries.items()):
            if k.startswith(FACT_SKIP) or e.get("kind") not in ("MEASURED", "DERIVED") or not isinstance(e.get("value"), (int, float)):
                continue
            e2 = now.get(k)
            v2 = (e2 or {}).get("value")
            if isinstance(v2, (int, float)) and abs(v2 - e["value"]) > 1e-9 * max(1.0, abs(e["value"])):
                out["data" if e["kind"] == "MEASURED" else "python"].append(
                    {"id": k, "label": REG.unmark(str(e2.get("label") or k)), "block1": e.get("shown") or REG.fmt_value(e["value"], e.get("fmt", "num")),
                     "block2": e2.get("shown") or REG.fmt_value(v2, e2.get("fmt", "num"))})
    except Exception as ex:  # noqa: BLE001
        out["why"] = type(ex).__name__
    return out


def curveball_record(results: dict):
    """D3: what this Block 2 did — for the page's section, the chat context, the console and the other tracks (checkpoints/
    curveball.json). None in Block 1. Pure: computing it twice gives the same record (the page renders twice per run)."""
    if not is_block2():
        return None
    ma = results.get("master_analysis") or {}
    fin = results.get("financial_model") or {}
    cb = _resolve_update_texts(ma.get("curveball_update") or {"status": "failed: the analysis did not complete"}, results)
    changes, _why = _changes_from_env()
    chal = os.environ.get("NAIPC_CHALLENGE", "").strip() or (changes or {}).get("challenge")
    base, _bwhy = load_block1_base(chal, (changes or {}).get("block1_run_id"))
    raw2 = (results.get("data_loading") or {}).get("raw")
    moved, note = moved_figures(base, fin, cb)
    facts_moved = facts_moved_of(results)              # BUILD IC-4 (D3, F13): from Block 1's registry and this run's
    derived, ddrop = derived_figures(cb.get("derived_requests"), (base or {}).get("raw"), raw2)
    cv = curveball_object(changes)
    eps = (changes or {}).get("endpoints")
    changed_eps = NR.endpoint_rows(changes)   # one rule for the Oracle's section, the Muse's block and the hub's panel
    return {"schema": NR.CHANGES_SCHEMA, "challenge": chal, "run_id": NR.run_id(),
            "block1_run_id": ((base or {}).get("meta") or {}).get("run_id") or (changes or {}).get("block1_run_id"),
            "update": cb.get("status"), "announcement": cv.get("text"), "announced_at": cv.get("announced_at"),
            "tests": [t for t in (cv.get("tests") or []) if isinstance(t, str)], "api_changed": (changes or {}).get("api_changed"),
            "summary": (changes or {}).get("summary") or [], "snapshot": ((changes or {}).get("snapshot") or {}).get("source"),
            "unchanged": [ep for ep, e in (eps or {}).items() if str(e.get("status", "")).startswith("unchanged")] if eps is not None else None,
            "changed_endpoints": changed_eps, "alerts": [list(a) for a in NR.alert_changes(changes)] if changes else [],
            "what_changed": cb.get("what_changed") or [], "response_summary": cb.get("response_summary") or "",
            "amended": cb.get("amended") or [], "dropped": cb.get("dropped") or [], "assumption_changes": cb.get("assumption_changes") or [],
            "brief_answers_changed": cb.get("brief_answers_changed") or [], "moved": moved, "moved_note": note, "facts_moved": facts_moved,
            "moved_compared": bool(base and isinstance(base.get("fin"), dict) and isinstance(fin, dict) and fin),
            "derived": derived, "derived_dropped": [{"label": a, "label_full": b, "why": c, "detail": d} for a, b, c, d in ddrop],
            "test_answers": cb.get("test_answers") or [], "reply_file": cb.get("reply_file"),   # P0.7 r2 (E2, E3 d)
            "edits": cb.get("edits") if isinstance(cb.get("edits"), dict) else None,      # BUILD IC-4 (D11): each item's status
            # review F1: Block 1's text on the paths that keep it (applied, failed) — where it still states a value this run changed
            "stale": stale_statements(base, ma, fin, cb) if str(cb.get("status") or "") == "applied" or str(cb.get("status") or "").startswith("failed") else []}


SECTION_NAMES = {"badge_text": "the hero badge", "hero_title": "the hero title", "hero_lead": "the hero lead", "summary_title": "the summary title",
                 "bluf_text": "the bottom line", "decision_text": "the decision", "crisis_title": "the problem's title", "crisis_body": "the problem",
                 "crisis_human": "the human story", "crisis_sources": "the problem's sources", "solution_title": "the approach's title",
                 "solution_body": "the approach", "solution_population": "the population funnel", "cost_of_inaction": "the cost of inaction",
                 "evidence_title": "the evidence title", "operations_title": "the operations title", "operations_description": "operations",
                 "plan_title": "the plan's title", "financial_title": "the financial title", "sensitivity_text": "the sensitivity analysis",
                 "renewal_label": "the renewal label", "renewal_text": "the renewal case", "appendix_title": "the appendix title",
                 "methodology": "the methodology", "ai_process": "the AI process", "comparison_rows": "the today-versus-proposed table",
                 "evidence_cards": "the evidence cards", "phases": "the implementation phases", "risks": "the risk register",
                 "assumptions": "the key assumptions", "mermaid_code": "the workflow diagram",
                 "diagram": "the workflow diagram"}          # BUILD IC-5 (D15): the diagram as data
EXTRA_TEXT_NAMES = {"brief_answers": "the brief's answers", "sroi_outcomes": "the SROI table", "staff_roles": "the staffing plan"}
ASSUMPTION_LABELS = {"target_beneficiaries_y1": "Year 1 beneficiaries", "growth_rate_y2": "Year 2 growth rate", "growth_rate_y3": "Year 3 growth rate",
                     "ops_cost_y1": "Year 1 operations cost", "tech_cost_y1": "Year 1 technology cost", "outreach_cost_y1": "Year 1 outreach cost",
                     "staff_roles": "staffing plan", "supplemental_grants_y2": "Year 2 supplemental grants",
                     "supplemental_grants_y3": "Year 3 supplemental grants", "earned_revenue_y3": "Year 3 earned revenue", "sroi_outcomes": "SROI outcomes",
                     "deadweight": "deadweight", "attribution": "attribution", "displacement": "displacement", "drop_off_rate": "drop-off rate"}
MONEY_INPUTS = ("ops_cost_y1", "tech_cost_y1", "outreach_cost_y1", "supplemental_grants_y2", "supplemental_grants_y3", "earned_revenue_y3")
# P0.7 r2 (E3 b): a page name for every other field an update can name and for every part of a reply that is not a field
ORACLE_FIELD_NAMES = {"program_name": "the program's name", "brand_html": "the program's styled name", "domain_label": "the policy area's label",
                      "domain": "the program's domain", "city_name": "the city's name", "contract_value": "the budget",
                      "contract_years": "the contract's length in years", "indirect_rate": "the indirect rate", "discount_rate": "the discount rate",
                      "fund_lines": "the use of funds' owners and timing", "marketing_allocation": "the marketing reserve's allocation"}   # BUILD B1
ORACLE_PART_NAMES = {"amended": "the amendments", "assumption_changes": "the assumption changes", "brief_answers": "the brief's answers",
                     "what_changed": "what changed", "test_answers": "the curveball tests' answers", "derived_figures": "the derived figures",
                     "response_summary": "the response summary", "reply": "the reply",     # "reply": Phase 5 (C14), its keys not asked for
                     "because": "the reasons"}              # Phase 5 review #7 (c): reasons for a part the update did not amend
_PART_ENTRY = re.compile(r"^(what_changed|test_answers|derived_figures|assumption_changes|brief_answers)\[(\d+)\]$")
_FIELD_PATH = re.compile(r"^(\w+)\[(\d+)\](?:\.(\w+))?$")


def field_place(f) -> str:
    """BUILD IC-5 (D4): a field of the analysis in the page's words — "evidence_cards[0].title" → "the evidence cards, entry 1 (its
    title)", "hero_lead" → "the hero lead"; a field no table names, its key's words ("_" read as a space)."""
    f = str(f or "")
    m = _FIELD_PATH.match(f)
    if m:
        base = SECTION_NAMES.get(m[1]) or EXTRA_TEXT_NAMES.get(m[1]) or ORACLE_PART_NAMES.get(m[1]) or m[1].replace("_", " ")
        return f"{base}, entry {int(m[2]) + 1}" + (f" (its {m[3].replace('_', ' ')})" if m[3] else "")
    for table in (SECTION_NAMES, EXTRA_TEXT_NAMES, ORACLE_FIELD_NAMES, ORACLE_PART_NAMES):
        if f in table:
            return table[f]
    return f.replace("_", " ")


def _words_q(w, i):
    return '"' + (w.get(i) or NR.name_cut([i], quote=False)) + '"'


# E3 (b): a record P0.7 wrote (no `part`) — each of its fixed reason forms in r2's plain words, never the stored technical text; a name
# inside one cut to NR.NAME_CHARS. Keyed by the form's fixed words; anything else → the last line.
P07_REASON_FORMS = (
    (re.compile(r"^does not validate"), lambda m, w: "the update's version did not fit the analysis's format"),
    (re.compile(r"^not a field an update may amend$"), lambda m, w: "not a part an update may amend"),
    (re.compile(r"^no reason given \(because\)$"), lambda m, w: "no change was named as its reason"),
    (re.compile(r"^unknown ref (.+)$", re.S), lambda m, w: f"its reason is not a change in the list ({NR.name_cut([m.group(1)], quote=False)})"),
    (re.compile(r"^no known ref in because$"), lambda m, w: "its reason names no change in the list"),
    (re.compile(r"^an identity field — never amended$"), lambda m, w: "part of the program's identity — an update never changes it"),
    (re.compile(r"^a financial input or the budget — it changes only through assumption_changes$"),   # review #4: p07_plain reads the
     lambda m, w: "not a part an update may amend"),                                                   # entry's field first (fixed_reason)
    (re.compile(r"^a \w+, not a list — nothing changed$"), lambda m, w: "not a list — nothing changed"),
    (re.compile(r"^not an object$"), lambda m, w: "not in the expected form"),
    (re.compile(r"^not an assumption a curveball may change \(a method parameter\)$"), lambda m, w: "not an assumption an update may change — a method parameter"),
    (re.compile(r"^not an assumption a curveball may change$"), lambda m, w: "not an assumption an update may change"),
    (re.compile(r"^the brief's marketing reserve sets Year-1 outreach$"), lambda m, w: "the brief's marketing reserve sets Year 1 outreach"),
    (re.compile(r"^the financial model cannot be computed with it"), lambda m, w: "the financial model cannot be computed with it"),
    (re.compile(r"^not usable \("), lambda m, w: "not in a form Python can use"),
    (re.compile(r"^a \w+, not an object — nothing amended$"), lambda m, w: "not given as named fields — nothing was amended"),
    (re.compile(r"^brief_answers\[\d+\]: not an object"), lambda m, w: "not in the expected form"),
    (re.compile(r"^brief_answers: unknown id (.+) — dropped$", re.S),
     lambda m, w: f"not an item of the brief (the item named is {NR.name_cut([m.group(1)], quote=False)})"),
    (re.compile(r"^brief_answers: ([^:]+): (?:null|empty) answer — dropped$"), lambda m, w: f"the answer to {_words_q(w, m.group(1))} is empty"),
    (re.compile(r"^brief_answers: ([^:]+): a second answer — dropped$"), lambda m, w: f"a second answer to {_words_q(w, m.group(1))} — the first is used"),
    (re.compile(r"^brief_answers: ([^:]+): \w+ answer — dropped$"), lambda m, w: f"the answer to {_words_q(w, m.group(1))} is not text"),
    (re.compile(r"^brief_answers: a \w+, not a list or an object — ignored$"), lambda m, w: "not a list or an object — Block 1's answers stand"),
)


def p07_plain(why, words=None, field=None) -> str:
    """E3 (b): a reason P0.7 stored (no `part` on its entry) in r2's plain words — P07_REASON_FORMS, else "Python could not use it".
    Phase 5 review #4: P0.7's one form for every field outside the narrative ('a financial input or the budget …') is given its field's
    TRUE reason (fixed_reason; a P0.7 record does not say whether a reserve was set)."""
    s = str(why or "")
    if s == "a financial input or the budget — it changes only through assumption_changes" and field in MasterAnalysis.model_fields:
        return fixed_reason(field)
    for rx, fn in P07_REASON_FORMS:
        m = rx.search(s)
        if m:
            return fn(m, words or {})
    return "Python could not use it"


def part_name(d, words=None, ed=None, marked=True) -> str:
    """E3 (b): the page name of a part of the reply that was not applied — by its `part` and field (r2's records) or, for an entry P0.7
    wrote (no `part`), by its field: SECTION_NAMES first ("assumptions" → "the key assumptions"), a contract item id → the brief's answer
    to "<item words>" (`words`: {item id: the contract's words}, looked up at render time). A name the model wrote → cut and quoted."""
    words = words or {}
    f, part = str((d or {}).get("field") or ""), (d or {}).get("part")
    if part == "edit":                    # BUILD IC-4 (D11): an item by its place and words, a field by its name — never an id
        return ", ".join(item_name(x.strip(), ed, words, marked) for x in f.split(", ")) if f else "a line of the update's reply"
    m = _PART_ENTRY.match(f)
    if m:
        k, j = m.group(1), int(m.group(2)) + 1
        return (f"assumption change {j}" if k == "assumption_changes" else f"the update's brief answer {j}" if k == "brief_answers"
                else f"{ORACLE_PART_NAMES[k]}, entry {j}")
    if part in ("shape", "list") and f in ORACLE_PART_NAMES:
        return ORACLE_PART_NAMES[f]
    if part == "brief answer" or (part is None and f in words):
        return f"the brief's answer to {_words_q(words, f)}"
    if part == "assumption change":
        if f in ASSUMPTION_LABELS:
            return "assumption " + ASSUMPTION_LABELS[f]
        if f in ("indirect_rate", "discount_rate"):
            return "assumption " + ORACLE_FIELD_NAMES[f]
        for table in (SECTION_NAMES, EXTRA_TEXT_NAMES, ORACLE_FIELD_NAMES):   # Phase 5 review #4: a field with a page name is named by it
            if f in table:
                return table[f]
        return f'the update\'s key "{NR.name_cut([f], quote=False)}"'
    for table in (SECTION_NAMES, EXTRA_TEXT_NAMES, ORACLE_FIELD_NAMES, ASSUMPTION_LABELS, ORACLE_PART_NAMES):
        if f in table:
            return table[f]
    return f'the update\'s key "{NR.name_cut([f], quote=False)}"'


def not_applied_parts(rec, words=None, page=False) -> list:
    """E3 (b, c): every part of the reply not applied — [(name, plain reason)] — the record's `dropped` (r2's plain `why`; P0.7's through
    P07_REASON_FORMS), then each derived figure not computed: on the page "not computed — <reason>" (the line "<label>: not computed —
    <reason>.", E3 b), in the chat "not computed: <reason>" (the line "<label> — not computed: <reason>", E3 c)."""
    out = []
    for d in (rec or {}).get("dropped") or []:
        if not isinstance(d, dict):
            continue
        why = str(d.get("why") or "") if d.get("part") else p07_plain(d.get("why"), words, d.get("field"))
        out.append((part_name(d, words, (rec or {}).get("edits"), marked=page), why))
    for d in (rec or {}).get("derived_dropped") or []:
        if isinstance(d, dict) and not str(d.get("why") or "").endswith("the figure is shown without it"):   # P0.7's compare form: on the figure
            out.append((str(d.get("label") or ""), ("not computed — " if page else "not computed: ")
                        + (str(d.get("why") or "") if "label_full" in d else p07_plain_derived(d.get("why")))))
    return out


def p07_plain_derived(why) -> str:
    """A derived figure's reason as P0.7 stored it (its own wording, which can hold the model's names uncapped) — cut to NAME_CHARS × 3."""
    return REG.fit_cap(" ".join(str(why or "").split()), "oracle.derived.why", NR.NAME_CHARS * 3)


def _numbers(v) -> list:
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [v]
    if isinstance(v, list):
        return [x for e in v for x in _numbers(e)]
    if isinstance(v, dict):
        return [x for e in v.values() for x in _numbers(e)]
    return []


def _shown_value(field: str, v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return ""
    if field in MONEY_INPUTS or field == "budget":
        return NR.money_short(v)
    if isinstance(v, float) and 0 < v < 1:
        return f"{round(v * 100, 2):g}%"
    return f"{v:,}" if float(v).is_integer() else f"{v:g}"


def _and(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def stale_statements(base, ma: dict, fin: dict, cb: dict) -> list:
    """Review F1: where text the update did not amend still states a Block 1 value that changed in this run — an input an assumption
    change moved, or the budget (an override given in Block 2). Python recomputes the financial model; the narrative is the model's, so a
    number Block 1's text states stays Block 1's unless the update amends that text (the update prompt asks it to). Each such place is
    named on the page, in the chat context and on the console: [{what, old, new, where: [section names]}]. The text read: every narrative
    field the update did not amend, the brief's answers it did not replace, and the SROI table's and the staffing plan's own words when
    those inputs did not change. [] without a Block 1 base. Pure; never raises."""
    try:
        if not base or not isinstance(base.get("ma"), dict) or not isinstance(ma, dict):
            return []
        amended = {a.get("field") for a in cb.get("amended") or [] if isinstance(a, dict)}
        changed = {c.get("field") for c in cb.get("assumption_changes") or [] if isinstance(c, dict)}
        replaced = {b.get("id") for b in cb.get("brief_answers_changed") or [] if isinstance(b, dict)}
        texts = {f: ma.get(f) for f in NARRATIVE_FIELDS if f not in amended}
        texts["brief_answers"] = [e.get("answer") for e in ma.get("brief_answers") or [] if isinstance(e, dict) and e.get("id") not in replaced]
        for f in ("sroi_outcomes", "staff_roles"):
            if f not in changed:
                texts[f] = ma.get(f)
        name = lambda k: SECTION_NAMES.get(k) or EXTRA_TEXT_NAMES.get(k, k)
        out = []
        for c in cb.get("assumption_changes") or []:
            if not isinstance(c, dict):
                continue
            f, old, new = c.get("field"), c.get("old"), c.get("new")
            if isinstance(old, (list, dict)):
                gone = [x for x in _numbers(old) if x not in set(_numbers(new))]
                toks = [t for x in gone for t in NR.stated_tokens(x)]
                old_s, new_s = ", ".join(_shown_value("", x) for x in gone[:3]), "this run's values"
            else:
                toks = NR.stated_tokens(old, money=f in MONEY_INPUTS)
                old_s, new_s = _shown_value(f, old), _shown_value(f, new)
            where = NR.stale_keys({k: v for k, v in texts.items() if k != f}, toks)
            if where:
                out.append({"what": ASSUMPTION_LABELS.get(f, f), "old": old_s, "new": new_s, "where": [name(k) for k in where]})
        k1 = (base.get("fin") or {}).get("kpis") or {}
        k2 = (fin or {}).get("kpis") or {}
        b1, b2 = k1.get("contract"), k2.get("contract")
        if all(isinstance(b, (int, float)) and not isinstance(b, bool) for b in (b1, b2)) and b1 != b2:
            where = NR.stale_keys(texts, NR.stated_tokens(b1, money=True))
            if where:
                out.append({"what": "budget", "old": NR.money_short(b1),
                            "new": NR.money_short(b2) + (" (the budget override)" if k2.get("contract_source") == "override" else ""),
                            "where": [name(k) for k in where]})
        return out
    except Exception:  # noqa: BLE001 — a record that cannot be read lists nothing; the figures are still Python's
        return []


def stale_line(s: dict) -> str:
    """One plain-text line of the section for a stale_statements entry (the template escapes it)."""
    w = s.get("where") or []
    return f"{_and(w)} still state{'s' if len(w) == 1 else ''} Block 1's {s['what']} ({s['old']}); the financial model uses {s['new']}."


def badge_sentence(text, limit=120) -> str:
    """The announcement's first sentence for the badge (at most `limit` characters) — NR.first_sentence, the one rule the Architect's
    change block and the hub's panel use too."""
    return NR.first_sentence(text, limit)


EDIT_PLACE = {"risk": "the risk register", "phase": "the implementation phases", "card": "the evidence cards", "row": "the today-versus-proposed table",
              "outcome": "the SROI table", "role": "the staffing plan", "kpi": "the KPI framework", "fm": "the failure modes", "ew": "the early warnings",
              "trigger": "the triggers", "qa": "the judges' questions"}
EDIT_HOW = {"cut: its limit": "the reply reached its length limit", "cut: its end time": "the reply reached the stage's end time",
            "cut: a dropped stream": "the reply's stream dropped"}


def update_line(rec, units=None):
    """BUILD IC-8 (D13): NR.update_status for this run's curveball record — its edits, the units given (the dispositions' records, or
    the headlines' outcomes before the page renders), the reply's add lines (the record's reply_file), the notes on what changed
    (what_changed) and the new derived figures (derived)."""
    rec = rec if isinstance(rec, dict) else {}
    rf = rec.get("reply_file")
    return NR.update_status(rec.get("edits"), units, NR.reply_add_kinds(CHECKPOINT_DIR.parent / rf) if isinstance(rf, str) and rf else None,
                            notes=len(rec.get("what_changed") or []), derived=len(rec.get("derived") or []))


def edits_line(ed, st, rec=None, units=None) -> str:
    """BUILD IC-4 (D11): how the update reviewed Block 1, from the record's edits — the counts by status, and how a cut reply ended. "" when
    the update did not run as edits or Block 1's analysis stands (the mode sentence says why). BUILD IC-8 (D13): the counts are
    update_status's line (`rec`: the curveball record; `units`: the headlines' outcomes)."""
    if not isinstance(ed, dict) or st != "applied":
        return ""
    n = ed.get("counts") or {}
    # one string: the claims scan reads a string whole (tests/lib/claims_lib7.py); D13: the counts are the status line's
    out = (f"The update reviewed Block 1's items one by one, each by its id, and amended what changed. "
           + update_line(dict(rec or {}, edits=ed), units)["line"])      # BUILD IC-8 (d15f): the line whole, as update_status made it
    if n.get("not applied"):
        out += " Each item not applied is named under Not applied."
    if n.get("not reviewed"):
        out += " Each item not reviewed at the curveball is named below."
    how = str(ed.get("how") or "")
    if how.startswith("cut"):
        out += (f" The reply was cut ({EDIT_HOW.get(how) or how[5:]}): "
                + ("each line it completed that passed the checks was applied" if n.get("not applied") else "every line it completed was applied")
                + ", and the items after the cut keep Block 1's words.")      # IC-4 Phase 7 (review a10)
    return out


def item_name(i, ed, words=None, marked=True) -> str:
    """BUILD IC-4 (D11): an item of the update's review set as a reader is told it — by its place on the page and its first words (the
    model's, marked on a page), never by its id; a field by its page name; a line's own problem by its line number. `ed` is the record's
    edits (each item's kind and words), `words` the contract's item words (a brief answer)."""
    i = str(i or "")
    it = ((ed or {}).get("items") or {}).get(i) or {}
    k, w = it.get("kind"), it.get("words") or ""
    if i.startswith("line "):
        return f"line {i[5:]} of the update's reply"
    if i == "the last line":
        return "the last line of the update's reply"
    if i.startswith("new."):
        return "an item the update's reply added"
    f = i[2:] if i.startswith(("f.", "d.")) else i
    if k in EDIT_PLACE:
        return f"{EDIT_PLACE[k]}: \u201c{REG.mark('model', w) if marked else w}\u201d"
    if k == "brief" or i.startswith("brief."):
        return f"the brief's answer to {_words_q(words or {}, i[6:])}"
    if k == "test" or i.startswith("test."):
        return f"the answer to the announcement's test {i.split('.')[-1]}"
    named = {"summary": "the response summary", "thesis": "the thesis", "renewal": "the renewal gate", "headlines": "the section headlines",
             "input": "assumption " + ASSUMPTION_LABELS.get(f, f), "text list": SECTION_NAMES.get("assumptions", "the key assumptions"),
             "diagram": SECTION_NAMES.get("mermaid_code", "the workflow diagram")}.get(k)
    if named:
        return named
    for table in (SECTION_NAMES, EXTRA_TEXT_NAMES, ORACLE_FIELD_NAMES):
        if f in table:
            return table[f]
    if f in ASSUMPTION_LABELS:
        return "assumption " + ASSUMPTION_LABELS[f]
    if i == "update.summary":
        return "the response summary"
    if i in ("thesis.1", "d.renewal", "d.headlines"):
        return {"thesis.1": "the thesis", "d.renewal": "the renewal gate", "d.headlines": "the section headlines"}[i]
    return "an item the update's reply named that is not on this page"


def edit_marks(ma) -> dict:
    """BUILD IC-4 (D11, Phase 4 item 6): the mark beside each item on the page — {"status", "words"} for an item not reviewed or whose line was
    not applied (EDIT_STATUS_WORDS), else None — from the analysis's update record, only when the edits were applied. {"lists": {field: [a
    mark or None, by the item's place]}, "ids": {a deliverable's id: mark}, "fields": {a text field: mark}}; empty lists without edits."""
    ma = ma if isinstance(ma, dict) else {}
    cb = ma.get("curveball_update") if isinstance(ma.get("curveball_update"), dict) else {}
    ed = cb.get("edits") if isinstance(cb.get("edits"), dict) else None
    out = {"lists": {}, "ids": {}, "fields": {}}
    if not ed or str(cb.get("status") or "") != "applied":
        return out
    st = ed.get("status") if isinstance(ed.get("status"), dict) else {}

    def mk(s):
        return {"status": s, "words": EDIT_STATUS_WORDS[s]} if s in ("not reviewed", "not applied") else None
    ids_ = REG.analysis_item_ids(ma)["ids"]
    for f in ("risks", "phases", "evidence_cards", "comparison_rows"):
        out["lists"][f] = [mk(st.get(i)) for i in ids_.get(f) or []]
    for i, s in st.items():
        if mk(s) and str(i).startswith("f."):
            out["fields"][i[2:]] = mk(s)
        elif mk(s):
            out["ids"][i] = mk(s)
    return out


def not_reviewed_names(ed, st, words=None) -> list:
    """BUILD IC-4 (D11): every item the update's reply gave no line, by its place on the page and its first words (Block 1's, the model's —
    marked) — only when the edits were applied (when Block 1's analysis stands, the mode sentence says no item was reviewed)."""
    if not isinstance(ed, dict) or st != "applied":
        return []
    return [item_name(i, ed, words) for i in ed.get("not_reviewed") or []]


def mode_sentence(st: str, chat: bool = False) -> str:
    """The section's mode sentence by the update's status. P0.7 r2 (E3 c): the chat context's status line is the same sentence, its
    positional words made true where it is read — the CURVEBALL block is the context's last block, so nothing is "below" it."""
    st = str(st or "")
    if st == "applied":
        return (f"The analysis {'on this page' if chat else 'below'} is Block 1's, updated by one call over it: Python kept only the amendments "
                "whose reasons name a real change and recomputed every figure of the financial model.")
    if st.startswith("failed: declined"):          # BUILD IC-4 (D11): a refusal — said as such, never as a call that did not complete
        return UPDATE_DECLINED_LINE.replace("the data changes below", "the data changes in this block") if chat else UPDATE_DECLINED_LINE
    if st.startswith("failed"):
        return UPDATE_FAILED_LINE.replace("the data changes below", "the data changes in this block") if chat else UPDATE_FAILED_LINE
    if st == "full":
        return ("The analysis was regenerated at the curveball (--curveball-full): every figure on this page is this run's; the comparison "
                f"with Block 1 is {'on the page' if chat else 'below'}.")
    return ("The analysis was written fresh at the curveball — there was no Block 1 analysis to start from (" + st.replace("no base: ", "") +
            "): every figure on this page is this run's.")


def item_words(doc) -> dict:
    """{item id: the contract's words} for the Oracle's items (E3 b: a brief answer is named by the brief's own words)."""
    try:
        return {i: NR.clean_text(t) for i, t in NR.brief_items(doc, "oracle")} if doc else {}
    except Exception:  # noqa: BLE001
        return {}


def _assumption_html(reg, rid, v):
    """BUILD IC (review a F7): an assumption change's value on the page — the registry figure when the run registered this value (Block 1's
    as b1.<field>, Block 2's as <field>), else the model's value as its words (marked as the model's, never as Python's)."""
    e = reg.get(rid) if reg is not None and isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if e is not None and abs(float(e["value"]) - float(v)) <= max(1e-9, abs(float(v)) * 1e-9):
        return reg.span(e)
    return _esc_model(NR.fmt_value(v, 80))


def curveball_section(rec, doc=None, reg=None, units=None):
    """The 'What changed at the curveball' section's rows, every value plain text (the template escapes each with | e).
    P0.7 r2 (E3 b): the replaced brief answers by the contract's words (`doc`); a "Not applied" block — every part of the reply not
    applied, by its name and plain reason, and every derived figure not computed — under the path's sentence; a derived figure shown
    without its comparison says so on its own line."""
    if not rec:
        return None
    st = str(rec.get("update") or "")
    # IC (D1; found by the canaries on the live replies): this section's lines are Python's sentences around other authors' words — the
    # brief's item words (the organizers'), an alert's title (the organizers') — marked here, where the line is composed for the page
    # only (the chat context and curveball.json compose their own lines from `rec`, unmarked)
    words = {k: (REG.mark("org", v) if isinstance(v, str) else v) for k, v in item_words(doc).items()}
    mode = mode_sentence(st)

    def money_or(v):
        return f"{v:,}" if _is_num(v) else str(v)
    endpoints = [NR.endpoint_text(e) for e in rec.get("changed_endpoints") or []]   # NR's one rule (no field sum on a page, D4)
    derived = []
    for d in rec.get("derived") or []:
        same = d.get("base1") == d.get("base2")
        if d.get("base2") is not None:
            val = (f"{money_or(d['block1'])} → {money_or(d['block2'])} of {money_or(d['base2'])}" if same else
                   f"{money_or(d['block1'])} of {money_or(d['base1'])} → {money_or(d['block2'])} of {money_or(d['base2'])}")
        else:
            val = f"{money_or(d['block1'])} → {money_or(d['block2'])}"
        how = f"{d['field']}" + (f" − {d['minus_field']}" if d.get("minus_field") else "") + f", {d['endpoint']}"
        recs = "all records" if d.get("records") == "all" else ", ".join(d.get("records") or [])
        comp = None
        if d.get("compare"):
            c = d["compare"]
            comp = (f"The API's {c['field']} ({c['endpoint']}) did not change at the curveball: {money_or(c['block2'])}" if c["block1"] == c["block2"]
                    else f"The API's {c['field']} ({c['endpoint']}) changed at the curveball: {money_or(c['block1'])} → {money_or(c['block2'])}")
        note = d.get("compare_note") or ""
        derived.append({"label": d["label"], "value": val, "how": how, "records": recs, "compare": comp,
                        "compare_note": (note[0].upper() + note[1:]) if note else ""})
    t_rows, t_other = NR.match_test_answers(rec.get("tests") or [], rec.get("test_answers") or [])   # review F5: matched by the words
    replaced = [f"the brief's answer to {_words_q(words, b.get('id'))} — because {', '.join(b.get('because') or [])}"
                for b in rec.get("brief_answers_changed") or [] if isinstance(b, dict)]
    not_applied = [f"{n}: {w}." for n, w in not_applied_parts(rec, words, page=True)]
    return {"mode": mode, "failed": st.startswith("failed"), "announcement": rec.get("announcement") or "",
            "announced_at": rec.get("announced_at") or "", "snapshot": rec.get("snapshot") or "",
            "endpoints": endpoints, "unchanged": rec.get("unchanged"),
            "alerts": [NR.alert_line(ep, kind, aid, REG.mark("org", title) if title and kind != "changed" else title)    # P4.4 (p44n): a
                       for ep, kind, aid, title in rec.get("alerts") or []],      # changed alert's words are Python's; its name, the page pass's
            "moved": [{"label": m["label"], "block1": m["block1"], "block2": m["block2"], "because": ", ".join(m["because"])} for m in rec.get("moved") or []],
            "moved_note": rec.get("moved_note"), "compared": bool(rec.get("moved_compared")), "derived": derived,
            "response": _plain_tags(rec.get("response_summary")),
            "what_changed": [{"ref": w["ref"], "impact": _plain_tags(w["impact"])} for w in rec.get("what_changed") or []],
            "amended": [{"name": SECTION_NAMES.get(a["field"], a["field"]), "because": ", ".join(a["because"])} for a in rec.get("amended") or []],
            "assumptions": [{"field": c["field"], "name": "assumption " + ASSUMPTION_LABELS.get(c["field"], NR.name_cut([c["field"]], quote=False)),
                             "old_h": _assumption_html(reg, "b1." + c["field"], c["old"]), "new_h": _assumption_html(reg, c["field"], c["new"]),
                             "old": NR.fmt_value(c["old"], 80), "new": NR.fmt_value(c["new"], 80), "because": ", ".join(c["because"]),
                             "why": c.get("why") or ""} for c in rec.get("assumption_changes") or []],   # Phase 5 (C1): its plain name
            "replaced": replaced,
            "not_applied": not_applied,
            "not_applied_sentence": NOT_APPLIED_UPDATE if st == "applied" else NOT_APPLIED_FULL,
            "tests": [{"test": t, "answer": _plain_tags(a)} for t, a in t_rows],
            "tests_other": [{"test": _plain_tags(a.get("test")), "answer": _plain_tags(a.get("answer"))} for a in t_other],
            "stale": [stale_line(x) for x in rec.get("stale") or [] if isinstance(x, dict)],
            "edits_line": edits_line(rec.get("edits"), st, rec, units),           # BUILD IC-4 (D11); IC-8 (D13)
            "not_reviewed": not_reviewed_names(rec.get("edits"), st, words)}


def update_words_locked(sec, rec, cbu, fix) -> Optional[dict]:
    """BUILD IC (D5): the update's own words on the page through the lock — the response summary, each 'what changed' note and each
    test answer: its tokens become registry spans (fix: the lock's path for a plain-text field, fix_text). `rec` holds them resolved
    to plain figures (_resolve_update_texts: the chat and curveball.json keep that form); `cbu` (the analysis's curveball_update) holds
    them as the model wrote them, in the same order. Each value the section shows is matched back to the words the model wrote by
    its resolved form; one that cannot be matched stays plain text, escaped. P2.9's promise, kept here."""
    if not sec or not isinstance(cbu, dict):
        return sec
    sec = dict(sec)
    raw_wc = [w for w in cbu.get("what_changed") or [] if isinstance(w, dict)]
    res_wc = [w for w in (rec or {}).get("what_changed") or [] if isinstance(w, dict)]
    raw_ta = [a for a in cbu.get("test_answers") or [] if isinstance(a, dict)]
    res_ta = [a for a in (rec or {}).get("test_answers") or [] if isinstance(a, dict)]

    def lock(raw, shown, field):
        return fix(raw, field) if isinstance(raw, str) and raw.strip() else _esc_model(shown)   # IC (review a F11): the model's words still
    if sec.get("response"):
        sec["response"] = lock(cbu.get("response_summary"), sec["response"], "curveball.response_summary")
    by_impact = {_plain_tags(w.get("impact")): w2.get("impact") for w, w2 in zip(res_wc, raw_wc)}
    sec["what_changed"] = [dict(w, impact=lock(by_impact.get(w.get("impact")), w.get("impact"), f"curveball.what_changed[{j}]"))
                           for j, w in enumerate(sec.get("what_changed") or [])]       # BUILD IC-8 (D3): one field name, one unit, each
    by_answer = {_plain_tags(a.get("answer")): a2.get("answer") for a, a2 in zip(res_ta, raw_ta)}
    sec["tests"] = [dict(t, answer=lock(by_answer.get(t.get("answer")), t.get("answer"), f"curveball.tests[{j}].answer") if t.get("answer") else "")
                    for j, t in enumerate(sec.get("tests") or [])]
    # IC (review a F4): a test the model named itself is its words too — through the lock, so a figure it cited stays a registry span
    by_test = {_plain_tags(a.get("test")): a2.get("test") for a, a2 in zip(res_ta, raw_ta)}
    sec["tests_other"] = [dict(t, answer=lock(by_answer.get(t.get("answer")), t.get("answer"), f"curveball.tests_other[{j}].answer"),
                               test_h=lock(by_test.get(t.get("test")), t.get("test"), f"curveball.tests_other[{j}].test"))
                          for j, t in enumerate(sec.get("tests_other") or [])]
    return sec


def curveball_chat_lines(rec, doc=None, units=None) -> list:
    """D7/D3: the Oracle's own lines in its chat context's CURVEBALL block — the update's status, the moved figures, the test answers.
    P0.7 r2 (E3 c): the status line is the page's mode sentence (never the raw status); right after it every part not applied
    (NR.not_applied_chat_lines: each ≤ NR.NOT_APPLIED_LINE_BYTES, ≤ NR.NOT_APPLIED_TOTAL_BYTES in all) and ONE line naming the brief
    answers this run replaced (≤ NR.REPLACED_LINE_BYTES) — before every longer line of the Oracle's own."""
    if not rec:
        return []
    words = item_words(doc)
    out = [mode_sentence(str(rec.get("update") or ""), chat=True)]
    el = edits_line(rec.get("edits"), str(rec.get("update") or ""), rec, units)      # BUILD IC-4 (D11): the review's counts; IC-8 (D13)
    if el:
        out.append(el)
    out += NR.not_applied_chat_lines(not_applied_parts(rec, words))
    rb = [f"{words.get(b.get('id')) or NR.name_cut([b.get('id')], quote=False)} (because {', '.join(b.get('because') or [])})"
          for b in rec.get("brief_answers_changed") or [] if isinstance(b, dict)]
    if rb:
        out.append(NR.cut(" ".join(("Brief answers replaced at the curveball (the answers under BRIEF are this run's): " + "; ".join(rb)).split()),
                          NR.REPLACED_LINE_BYTES, unit="bytes"))
    out += [f"Figure moved: {m['label']}: {m['block1']} → {m['block2']} (because: {', '.join(m['because'])})" for m in rec.get("moved") or []]
    out += [f"Derived from the records by Python: {d['label']}: {d['block1']} → {d['block2']}" + (f" of {d['base2']}" if d.get("base2") is not None else "")
            for d in rec.get("derived") or []]
    out += [f"Curveball test: {NR.clean_text(a.get('test'))} — answer: {_plain_tags(a.get('answer'))}" for a in rec.get("test_answers") or []]
    out += [f"Still as written for Block 1: {stale_line(x)}" for x in rec.get("stale") or [] if isinstance(x, dict)]   # review F1
    if rec.get("response_summary"):
        out.append(f"How the program responds (the analysis's words): {_plain_tags(rec['response_summary'])}")
    return out


def build_template_context(results: dict) -> dict:
    """Merge all pipeline outputs into the Jinja2 template context."""
    ma = results.get("master_analysis", {})
    fin = results.get("financial_model", {})
    diag = results.get("diagram_generation", {})
    data = results.get("data_loading", {})

    # Build KPIs from financial model — deltas computed from ACTUAL outputs, never hardcoded
    fin_kpis = fin.get("kpis", {})
    doc = _brief_contract()
    F = NR.facts(doc)                                 # P0.6 (D1): every Rule 4 value on this page comes from here
    unit_p, unit_s = F["unit_plural"], F["unit_singular"]
    Unit_p = unit_p[:1].upper() + unit_p[1:]
    actual_cpb = fin_kpis.get("cpb_y1", 0)

    # BUILD B3 (D1, D2): this page's registry — every figure the hero, the cards and the narrative show resolves here; the run's own
    # non-figures (ZIPs, record ids, names with digits) for the checker; what the lock removed or dropped, for the integrity details
    reg = build_registry(results)
    REG.set_page_names(reg.names())         # BUILD IC-8 (D12): this page's texts of Python's in a reader's words
    exempter = REG.Exempter.from_data(data.get("raw") or {}, NR.records)
    # IC-3 (ledger step 28): the diagram's typed figures read again from the model's own code by this build's reader (the stage's list is
    # its checkpoint's, which an older run wrote); None when the analysis holds no diagram code
    diag_typed_ = None
    if (diag.get("svg") or diag.get("mermaid")) and isinstance(ma.get("mermaid_code"), str) and ma.get("mermaid_code").strip():
        diag_typed_ = diagram_typed(ma["mermaid_code"], reg, exempter)
    elif (diag.get("svg") or diag.get("mermaid")) and has_diagram_data(ma):      # BUILD IC-5 (D15): the data's labels, read as the
        _c, _t, _e, _n = diagram_source(diag.get("dd") or ma["diagram"], reg, exempter)   # stage read them (IC-8 D4: the one drawn)
        diag_typed_ = (_t, _e, _c)
    integ = {"removed": [], "events": [], "dropped": [], "badges": [], "cited": {}, "flagged": [], "overrides_used": set()}
    overrides, ov_why = read_overrides(HARNESS_OVERRIDES)        # BUILD IC (D4): the one-minute override, by a sentence's key
    if ov_why:
        NR.say(f"    ⚠ integrity overrides: {ov_why} — not used")
    actual_sroi = fin_kpis.get("sroi")
    actual_sroi = actual_sroi if isinstance(actual_sroi, (int, float)) and not isinstance(actual_sroi, bool) else None

    # P0.6 (D1, D7): the Year-1 card in the brief's unit; the cost card with no invented benchmark; the pilot card only when the brief
    # states a pilot (with its own words) — the grid is auto-fit, so three cards fill the row. B3 (D2): each counter carries data-prov
    # and its registry form (the counter ends on it); D1: no SROI card without an SROI, and below 1 it says so in the "warn" class
    kpis = [
        {"label": f"{Unit_p} Served (Year 1)", "counter_target": fin_kpis.get("beneficiaries_y1", 0), "prov": "beneficiaries_y1",
         "shown": reg.shown("beneficiaries_y1"), "counter_format": "compact", "suffix": "", "static_value": None,
         "delta": f"Year-1 target ({unit_p})", "delta_class": "good"},
        {"label": f"Cost per {unit_s[:1].upper() + unit_s[1:]}", "counter_target": actual_cpb, "prov": "cpb_y1", "shown": reg.shown("cpb_y1"),
         "counter_format": "dollar", "suffix": "", "static_value": None,
         "delta_html": f"{reg.span('cpb_y1')} per {_html.escape(unit_s)} in Year 1", "delta_class": "good"},
    ]
    if actual_sroi is not None and reg.get("sroi"):
        kpis.append({"label": "Social Return on Investment", "counter_target": actual_sroi, "prov": "sroi", "shown": reg.shown("sroi"),
                     "counter_format": "ratio", "suffix": "", "static_value": None,
                     "delta_html": (f"{reg.span('sroi_per_dollar')} of counted social value per <span data-num-exempt=\"the ratio's unit\">$1</span>"
                                    " — less than it costs, on the outcomes modeled" if actual_sroi < 1 else
                                    f"<span data-num-exempt=\"the ratio's unit\">$1</span> invested returns {reg.span('sroi_per_dollar')} in social value"),
                     "delta_class": "warn" if actual_sroi < 1 else "good"})
    # P0.6 review F1: the what-if simulator's words come from the same sources as these cards and the P&L — the budget's short label
    # by its source (D2: "Proposed budget" for a proposal, never "Contract Value") and the brief's unit (D1)
    sim_labels = {"budget": NR.BUDGET_SHORT.get(fin_kpis.get("contract_source"), "Budget"), "served": f"{Unit_p} Served (Year 1)",
                  "cost": f"Cost per {unit_s[:1].upper() + unit_s[1:]}", "units": unit_p, "Units": Unit_p}
    if F["pilot_days"]:
        kpis.append({"label": "Pilot Length", "counter_target": F["pilot_days"], "prov": "pilot_days", "shown": reg.shown("pilot_days"),
                     "counter_format": "raw", "suffix": " Days", "static_value": None,
                     "delta_html": (f"The brief: <span data-prov-block=\"the brief's own words\">\u201c{_esc_org(F['pilot_quote'])}\u201d</span>"
                                    if F["pilot_quote"] else "The brief's pilot"), "delta_class": "good"})

    # Merge scenario descriptions from master analysis
    scenarios = fin.get("scenarios", {})
    for key in ["base", "bull", "bear"]:
        if key in scenarios and key in (ma.get("scenarios") or {}):
            scenarios[key]["description"] = _esc_model(ma["scenarios"][key].get("description", ""))   # BUILD IC-5 (D2 f): the model's words

    # Build data sources table from ingestion results
    # Per evaluation: descriptions must be human-readable, not raw endpoint names
    def describe_endpoint(ds):
        """Generate a human-readable description from field names."""
        fields = set(ds.get("fields", []))
        ep = ds.get("endpoint", "")
        # Heuristic descriptions based on common field patterns
        if {"povertyRate", "medianIncome"} & fields or "demographics" in ep:
            return "Census: poverty, income, demographics by area"
        if {"closedDate", "squareFt"} & fields or "closure" in ep:
            return "Facility/store closures with dates and impact"
        if {"coldStorage", "monthlyCapacity"} & fields or "pantri" in ep:
            return "Service locations, hours, capacity, languages"
        if {"foodDesert", "censusTract"} & fields or "atlas" in ep:
            return "USDA food desert status by census tract"
        if {"route", "frequency", "walkMinutes"} & fields or "transit" in ep:
            return "Transit routes and stops near service points"
        if {"severity", "title"} & fields or "alert" in ep:
            return "Supply/service status and emergency alerts"
        if {"priority", "weeklyLbs"} & fields or "harvest" in ep:
            return "Priority areas with delivery volumes"
        if {"category", "topIssue", "pctChange"} & fields or "311" in ep:
            return "311 service calls by area and category"
        if {"beds", "erCapacity", "payerMix"} & fields or "hospital" in ep:
            return "Hospital capacity, status, and payer mix"
        if {"capacityPerDay", "waitWeeks"} & fields or "clinic" in ep:
            return "Clinic capacity, wait times, specialties"
        if {"visits", "avgWaitMin"} & fields or "er-visit" in ep:
            return "Emergency room visit volumes and wait times"
        if {"conditionGroup", "urgency"} & fields or "displacement" in ep:
            return "Patient displacement by condition and urgency"
        # Fallback: use field names
        if fields:
            sample = ", ".join(sorted(fields)[:4])
            return f"Data including {sample}"
        return ds.get("source", ep)

    # P0.6 (D6): each row shows the contract's documented path (api.prefix + name) and the contract's own description; without a
    # contract, the path fetched and describe_endpoint's guess. "id" keeps the row's anchor (src-<endpoint>) every citation badge targets.
    documented = {}
    if isinstance(doc, dict):
        pre = "/" + str(doc.get("api", {}).get("prefix", "")).strip("/")
        documented = {e["name"]: (f"{pre.rstrip('/')}/{e['name']}", e.get("description") or "") for e in doc.get("api", {}).get("endpoints", [])
                      if isinstance(e, dict) and isinstance(e.get("name"), str)}
    data_sources = []
    for ds in data.get("summary", []):
        if ds.get("count", 0) > 0:
            name = ds["endpoint"]
            path, about = documented.get(name, (None, None))
            data_sources.append({
                "id": name,
                "endpoint": path or (urllib.parse.urlparse(ds.get("url") or "").path or f"/{name}"),
                "returns": about or describe_endpoint(ds),
                "records": ds["count"],
            })

    known_ids = [ds["id"] for ds in data_sources]      # P0.6 review F7: the rows a loose citation tag may point at

    # Post-process: inject actual computed values into Claude's narrative.
    # Claude writes the BLUF before the financial model runs, so its CPB estimate may differ.
    actual_cpb = fin_kpis.get("cpb_y1", 0)
    event_year = (F["date"] or datetime.now().date()).year     # P0.6 (D4)
    architect_url = os.environ.get("ARCHITECT_URL", "")
    # Extract known ZIP codes from raw data for inline Architect deep linking.
    # Only ZIPs that actually appear in the dataset will be linked — no false positives.
    known_zips = set()
    if architect_url:
        for ep_payload in data.get("raw", {}).values():
            for rec in _records(ep_payload)[0]:          # P0.6 review F2: the loader's own envelope rule (a bare list crashed here)
                if isinstance(rec, dict):
                    for k, v in rec.items():
                        if 'zip' in k.lower() and isinstance(v, str) and re.match(r'^\d{5}$', v):
                            known_zips.add(v)

    count_used, count_shown = set(), {}      # BUILD IC-4 (D13): the lists the page's texts count, and how many items each shows

    def count_now(n):
        """A held count is registered at once when the page's lists are counted already (a text fixed late: the headlines, the update's
        words); before that, when they are (see count_shown below)."""
        if count_shown and f"count.{n}" not in reg.entries and n in count_shown:
            reg.add_counts({n: count_shown[n]}, EXTRA_COUNT_WORDS)

    def _integ_snap():
        """BUILD IC-8 (D3): integ's lists, sets and citations, and the held counts — a text's effects, undone when its chain replaces it."""
        return ({k_: (list(v_) if isinstance(v_, list) else set(v_) if isinstance(v_, set) else
                      {kk: (list(vv) if isinstance(vv, list) else vv) for kk, vv in v_.items()} if isinstance(v_, dict) else v_)
                 for k_, v_ in integ.items()}, set(count_used))

    def _integ_restore(s_):
        integ.clear()
        integ.update(s_[0])
        count_used.clear()
        count_used.update(s_[1])

    def fix_narrative(text, field="", strict=None, model=True, chain=True, _ufield=None):
        """BUILD B3 (D2): one field of the model's text → the page's HTML. Its tokens are held while the year fix, the ZIP links and the
        source badges run (none of them may reach inside a token), then each becomes its registry span; the sanity rules run on the
        result and every sentence they name is removed and listed (integ). The cost-per-beneficiary and SROI rewrites are gone: the
        lock replaces them (the narrative cites Python's figures instead of typing its own).
        BUILD IC-8 (D3; V26 a disposition path): the sentences it would remove (a token naming nothing, a finding) go only under THE unit
        rule (REG.unit_rule_why); when it fails, THE chain (REG.unit_chain): Block 2 → Block 1's text of the same field fixed the same way
        (chain=False: None when it fails too) → the field's default (oracle_chain_links) → "" where the page shows the field's place without
        it → the prose default (REG.UNIT_DEFAULT); the outcome in integ["units"]. A text the chain replaces leaves no event, citation,
        flag, badge or held count behind (undone); its console lines stay. A field name fixed again in one render (a section's rows) is its
        next unit (<field>#<n>); _ufield: Block 1's text for the chain is fixed as the same unit."""
        if not isinstance(text, str) or not text:
            return text
        if _ufield is None:                         # D3: a field name fixed again in this render (a section's rows) is its next unit
            occ_ = integ.setdefault("occ", {})
            occ_[field] = occ_.get(field, 0) + 1
            ufield_ = field if occ_[field] == 1 else f"{field}#{occ_[field]}"
        else:
            ufield_ = _ufield
        snap_ = _integ_snap()                        # after the count: undoing this text keeps its place in the order
        held = []

        def _hold(m):
            held.append(m.group(0))
            return f"\ue001{len(held) - 1}\ue002"
        text = REG.TOKEN_RE.sub(_hold, text)
        if model:       # BUILD IC-5 (D2, I3): the model's markup — its narrative tags kept, every other tag dropped (words kept), '<' '>' escaped
            text = NR.escape_markup(text, keep_tags=True)
        # Fix forward-looking year: only when preceded by future-context words (P0.6, D4: the event's year, not a fixed 2025 → 2026)
        text = re.sub(r'(launch|begin|start|target|summer|by|before)\s+' + str(event_year - 1) + r'\b', r'\1 ' + str(event_year), text)
        # Inline Architect deep links: wrap ALL known ZIP codes (from the dataset) in
        # clickable links to the Architect Finder. Data-driven: only ZIPs that appear in the
        # actual API data get linked, preventing false positives on random 5-digit numbers.
        # Only when ARCHITECT_URL env var is set. Runs BEFORE citation badges.
        if architect_url and known_zips:
            _zip_alt = '|'.join(re.escape(z) for z in sorted(known_zips))
            def _zip_to_arch_link(m):
                zipcode = m.group(0)                                   # one of known_zips: five digits (^\d{5}$)
                return (f'<a class="arch-zip" href="{_html.escape(architect_url, quote=True)}?tab=finder&q={zipcode}" '
                        f'target="_blank" rel="noopener" title="Explore resources near {zipcode}">'
                        f'{zipcode}</a>')
            text = re.sub(r'\b(' + _zip_alt + r')\b', _zip_to_arch_link, text)
        # Transform [CHALLENGE_DATA: X] citation tags into clickable source badges (P0.6, D9: one function, also used by the SROI rows);
        # B3 (D2): a tag naming no endpoint of this run links to the sources section and is listed in the integrity details
        text = cite_badges(text, known_ids, integ["badges"])
        text = re.sub(r"\ue001(\d+)\ue002", lambda m: held[int(m.group(1))], text)
        text = hold_counts(text, count_used, count_now)       # BUILD IC-4 (D13): a page list's count waits for the page's lists
        text, ev = reg.resolve(text, mode="html")
        integ["events"] += [dict(e, field=field) for e in ev if e["rule"] in ("unknown", "malformed", "note")]
        integ["cited"].setdefault(field, []).extend(e["id"] for e in ev if e["rule"] == "cited")      # B3 (D8): the honesty panel's evidence
        found = REG.field_findings(text, reg, exempter, strict=strict, events=ev)     # IC-2 (D6): resolve's events name a failed comparison
        # BUILD IC (D4): a number in a strict block that is no registry figure, and a unit word that contradicts its figure, are flagged
        # and kept (their false-positive rate on a new subject is unknown; live, the strict rule removed two correct sentences and caught
        # nothing); a token naming nothing and the residual rule still remove. The override file moves a sentence either way by its key.
        cut_, flag_ = [], []

        def ov_of(key_):            # IC (review b F4): an override names a sentence by its key or by the prefix the console prints
            k_ = next((k for k in overrides if key_.startswith(k)), None)
            if k_:
                integ["overrides_used"].add(k_)
            return overrides.get(k_) if k_ else None
        for f_ in found:
            key_ = REG.sentence_key(REG.sentence_at(text, f_["src"]))
            ov_ = ov_of(key_)
            if ov_ and ov_["do"] == "keep" and f_["rule"] in TOKEN_RULES and key_ not in integ.setdefault("keep_refused", set()):
                integ["keep_refused"].add(key_)
                NR.say(f"    ⚠ integrity override {key_[:12]}…: `keep` does not apply — the sentence's token names no figure; removed")
            if ov_ and ov_["do"] == "remove" and f_["rule"] in FLAG_RULES:
                cut_.append(dict(f_, why=f"removed by the override: {ov_['why']}"))
            elif f_["rule"] in FLAG_RULES or (ov_ and ov_["do"] == "keep" and f_["rule"] not in TOKEN_RULES):
                flag_.append(dict(f_, key=key_, why=f_["why"] + (f" — kept by the override: {ov_['why']}" if ov_ and f_["rule"] not in FLAG_RULES else "")))
            else:
                cut_.append(f_)
        cut_keys_ = {REG.sentence_key(REG.sentence_at(text, f_["src"])) for f_ in cut_}
        flag_ = [f_ for f_ in flag_ if f_["key"] not in cut_keys_ and REG.SENTINEL not in REG.sentence_at(text, f_["src"])]   # IC (review b F2)
        seen_ = set()
        for f_ in flag_:                    # one flag per sentence and rule (two numbers in one sentence are one line, one key)
            if (f_["key"], f_["rule"]) in seen_:
                continue
            seen_.add((f_["key"], f_["rule"]))
            sent_ = REG.plain_sentence(REG.sentence_at(text, f_["src"]))
            integ["flagged"].append({"field": field, "rule": f_["rule"], "why": f_["why"], "sentence": sent_, "key": f_["key"]})
            NR.say(f"    integrity: flagged in {field} ({f_['rule']}; key {f_['key'][:12]}): {sent_[:120]}")
        if cut_ or REG.SENTINEL in text:
            # BUILD IC (D4): each sentence removed has its key, in the record and the console, so an override can name it; a `keep` on a
            # sentence whose token names no figure does not apply, and says so (the sentence has a hole in it)
            rm_at = {}
            for f_ in cut_:
                for a_, b_ in REG.sentence_spans(text):
                    if a_ <= f_["src"] < b_:
                        rm_at.setdefault(a_, REG.sentence_key(text[a_:b_]))
            for a_, b_ in REG.sentence_spans(text):
                if REG.SENTINEL in text[a_:b_] and a_ not in rm_at:
                    k_ = REG.sentence_key(text[a_:b_])
                    rm_at[a_] = k_
                    if (ov_of(k_) or {}).get("do") == "keep" and k_ not in integ.setdefault("keep_refused", set()):
                        integ["keep_refused"].add(k_)
                        NR.say(f"    ⚠ integrity override {k_[:12]}…: `keep` does not apply — the sentence's token names no figure; removed")
            # IC-2 (D6, §6): each removed sentence names the tokens that failed in it (resolve's events, by where they sit) — the live
            # run's acceptance tells a form named wrongly from a comparison false as named
            spans_ = {a_: b_ for a_, b_ in REG.sentence_spans(text)}
            toks_ = {a_: [{"token": e_["token"], "rule": e_["rule"], "why": e_.get("why") or ""} for e_ in ev
                          if e_["rule"] in ("unknown", "malformed", "calc") and isinstance(e_.get("at"), int) and a_ <= e_["at"] < spans_.get(a_, a_)]
                     for a_ in rm_at}
            # BUILD IC-8 (D3): THE unit rule on every sentence this removes; when it fails, THE chain
            sp_u = REG.sentence_spans(text)
            why_u = REG.unit_rule_why(text, sp_u, {k_ for k_, (a_, _b) in enumerate(sp_u) if a_ in rm_at})
            if why_u:
                _integ_restore(snap_)
                if not chain:
                    return None
                dflt_, omit_ = oracle_chain_links(field, prog_)
                out_, d_, r_, took_ = REG.unit_chain(
                    why_u, lambda: _base_fixed(field, strict, model, ufield_), REG.mark("py", _html.escape(dflt_)) if dflt_ else None, omit_,
                    REG.mark("py", _html.escape(REG.UNIT_DEFAULT)))
                integ.setdefault("units", []).append({"field": field, "ufield": ufield_, "disposition": d_, "reason": r_, "took": took_})
                NR.say(f"    integrity: {field} — its sentences cannot go alone ({why_u}): "
                       + {"base": "Block 1's text", "default": "its default", "omit": "left out", "generic": "the prose default"}[took_])
                return out_
            text, rem = REG.apply_findings(text, cut_)
            rules = sorted({f["rule"] for f in cut_}) or ["unknown"]
            keys_ = [rm_at[a_] for a_ in sorted(rm_at)] if len(rm_at) == len(rem) else [None] * len(rem)
            tok_l = [toks_[a_] for a_ in sorted(rm_at)] if len(rm_at) == len(rem) else [[]] * len(rem)
            integ["removed"] += [dict(r, field=field, ufield=ufield_, rules=rules, key=k_, tokens=t_)       # IC-8 (D3): its unit's field
                                 for r, k_, t_ in zip(rem, keys_, tok_l)]
            for r, k_ in zip(rem, keys_):
                NR.say(f"    integrity: removed from {field} ({', '.join(rules)}{f'; key {k_[:12]}' if k_ else ''}): {r['sentence'][:140]}")
        return REG.mark("model", text)      # BUILD IC (D1): the model's words, marked as its own where they become HTML

    def fix_text(text, field="", strict=None):
        """B3 (Phase 4, reviews c F3, b F2, d F12): one PLAIN-TEXT field of a reply (an indicator, a cadence, a basis, a fund line's
        words, a headline) → the page's HTML through the same lock: each part outside a token escaped, each token kept as the model wrote
        it (so a proposal's words register unescaped, once), then fix_narrative — its tokens become registry spans, its findings remove
        their sentences. A raw [[…]] never reaches the page."""
        if not isinstance(text, str) or not text:
            return _esc(text)
        parts, last = [], 0
        for m in REG.TOKEN_RE.finditer(text):
            parts += [_esc(text[last:m.start()]), m.group(0)]
            last = m.end()
        parts.append(_esc(text[last:]))
        return fix_narrative("".join(parts), field, strict)

    # BUILD B3 (D2): every text field through the lock; the hero's fields and the evidence cards are strict (an untraced number there
    # removes its sentence); an evidence card whose figure is not one token naming a figure is dropped and named; the workflow diagram's
    # code was drawn by its stage, its tokens here only as plain figures
    # BUILD IC-8 (D3): THE chain's first link for the Oracle's own texts — Block 2: Block 1's text of the same field (a list item's found by
    # its id, ANALYSIS_ITEM_FIELDS; a brief answer's by its own id; a list with no ids, or a bare string item, has none), prepared as the
    # page prepares it (an answer fitted); unit_of: a field's unit (a narrative field's is its item, f.<field>; a list item's its id; else
    # text.<field>, a position counted from 1)
    prog_ = NR.clean_text(ma.get("program_name")) or ""
    b1ma_ = (_block1_ma() or None) if is_block2() else None
    ids_now_ = analysis_ids(ma, doc)["ids"]
    ids_b1_ = REG.analysis_item_ids(b1ma_)["ids"] if b1ma_ else {}

    def base_text_of(field):
        m_ = re.fullmatch(r"(\w+)(?:\[(\d+)\](?:\.(\w+))?)?", str(field))
        if not (b1ma_ and m_):
            return None
        k_, i_, dk_ = m_.group(1), m_.group(2), m_.group(3)
        if i_ is None:
            return b1ma_.get(k_) if isinstance(b1ma_.get(k_), str) else None
        i_ = int(i_)
        if k_ == "brief_answers" and dk_:
            cur_ = (ma.get(k_) or [])[i_] if i_ < len(ma.get(k_) or []) else None
            aid_ = cur_.get("id") if isinstance(cur_, dict) else None
            it_ = next((x for x in b1ma_.get(k_) or [] if isinstance(x, dict) and aid_ is not None and x.get("id") == aid_), None)
        elif k_ in ids_now_ and dk_ and i_ < len(ids_now_[k_]) and ids_now_[k_][i_] in (ids_b1_.get(k_) or []):
            it_ = (b1ma_.get(k_) or [])[ids_b1_[k_].index(ids_now_[k_][i_])]
        else:
            return None                                   # a list with no ids, or a bare string item: no identity, no chain
        v_ = it_.get(dk_) if isinstance(it_, dict) else None
        if not isinstance(v_, str):
            return None
        return (REG.fit_cap(v_, *ORACLE_FIELD_FITS[(k_, dk_)], reg) if (k_, dk_) in ORACLE_FIELD_FITS     # as the page fits it (d1n)
                else v_)

    def _base_fixed(field, strict, model, ufield):
        b_ = base_text_of(field)
        return fix_narrative(b_, field, strict, model, chain=False, _ufield=ufield) if isinstance(b_, str) and b_.strip() else None

    def unit_of(field):
        f_ = str(field or "")
        m_ = re.fullmatch(r"(\w+)\[(\d+)\](?:\.(\w+))?", f_)
        if m_ and m_.group(1) in ids_now_:
            cur_, i_ = ids_now_[m_.group(1)], int(m_.group(2))
            return (cur_[i_] if i_ < len(cur_) else f"{m_.group(1)}.at{i_ + 1}") + (f".{m_.group(3)}" if m_.group(3) else "")
        if re.fullmatch(r"\w+", f_):
            return f"f.{f_}"
        return "text." + (re.sub(r"\[(\d+)\]", lambda m: f".{int(m.group(1)) + 1}", f_) or "unnamed")

    def _plain_twin(raw, field):
        """BUILD IC-8 (D3): a field's plain words, one decision with its page text — the link its unit took (integ["units"]: Block 1's
        text, its default, left out, the prose default), else the dead sentences out as the rule allowed (reg.plain). Never the raw text."""
        u_ = next((x for x in reversed(integ.get("units") or []) if x.get("field") == field), None)
        if u_ is None:
            return reg.plain(raw) if isinstance(raw, str) else ""
        if u_["took"] == "base":
            b_ = base_text_of(field)
            return reg.plain(b_) if isinstance(b_, str) else ""
        if u_["took"] == "default":
            return oracle_chain_links(field, prog_)[0] or ""
        return REG.UNIT_DEFAULT if u_["took"] == "generic" else ""
    ma_fixed = {}
    marks = edit_marks(ma)          # BUILD IC-4 (D11): each list item carries its mark (cb_mark) to its own row, card or phase
    ma_m = {k: ([dict(x, cb_mark=m_) if isinstance(x, dict) and m_ else x for x, m_ in zip(v, (marks["lists"][k] + [None] * len(v))[:len(v)])]
                if k in marks["lists"] and isinstance(v, list) else v) for k, v in ma.items()}
    for k, v in ma_m.items():
        if k == "mermaid_code" and isinstance(v, str):
            t_, _ev = mermaid_resolve(reg, v)
            ma_fixed[k] = t_.replace(REG.SENTINEL, "") if isinstance(t_, str) else t_
        elif k == "evidence_cards" and isinstance(v, list):
            cards = []
            for i, card in enumerate(v):
                if not isinstance(card, dict):
                    continue
                sv = str(card.get("stat_value") or "")
                toks, rest = REG.TOKEN_RE.findall(sv), REG.TOKEN_RE.sub("", sv)
                if len(toks) != 1 or re.search(r"\d", rest) or reg.lookup(toks[0])[0] is None:
                    why = (f"its figure's token {toks[0][:60]!r} names no figure" if len(toks) == 1 and not re.search(r"\d", rest)
                           else "its figure is not one token")
                    integ["dropped"].append({"what": f"evidence card {i + 1} ({REG.fit_text(NR.clean_text(card.get('title')), 'oracle.evidence.title')})",
                                             "why": why, "item": unit_of(f"evidence_cards[{i}]"),       # BUILD IC-8 (D3): its item's
                                             "reason": "card_figure_not_one_token"})                    # outcome
                    NR.say(f"    integrity: evidence card {i + 1} dropped — {why}")
                    continue
                c2 = {dk: fix_narrative(dv, f"evidence_cards[{i}].{dk}", strict="evidence") if isinstance(dv, str) else dv
                      for dk, dv in card.items() if dk != "stat_class"}
                # BUILD IC-5 Phase 4: the figure's style is printed inside a class attribute (template.html.j2) — one of the request's three
                # or none, never the model's text through the narrative fixer, whose quotes stay as written (NR.escape_markup: "quotes
                # are left as written … attributes escape them with | e"): the attribute's value ended at the model's quote
                sc_ = str(card.get("stat_class") or "").strip().lower()
                c2["stat_class"] = sc_ if sc_ in STAT_CLASSES else ""
                if not NR.clean_text(REG.unmark(c2.get("stat_value"))) or not NR.clean_text(REG.unmark(c2.get("title"))):   # IC (review a F5)
                    integ["dropped"].append({"what": f"evidence card {i + 1}", "why": "its title or figure was removed by the integrity check",
                                             "item": unit_of(f"evidence_cards[{i}]"), "reason": "card_emptied_by_integrity"})   # D3
                    NR.say(f"    integrity: evidence card {i + 1} dropped — its title or figure was removed")
                    continue
                cards.append(c2)
            ma_fixed[k] = cards
        elif isinstance(v, str):
            ma_fixed[k] = fix_narrative(v, k, strict="hero" if k in ("badge_text", "hero_title", "hero_lead") else None)
        elif isinstance(v, list):
            fixed_list = []
            for i, item in enumerate(v):
                if isinstance(item, str):
                    fixed_list.append(fix_narrative(item, f"{k}[{i}]"))
                elif isinstance(item, dict):
                    # Process string values inside dicts (comparison_rows, phases, risks, brief answers, the use of funds' words).
                    # BUILD IC (D1, found by t_oracle7's T-or7-3): an identifier is not prose — fix_narrative marks what it returns, and a
                    # marked id no longer matches its key (every brief answer showed "—" in the panel: _brief_for_page keys by id)
                    # BUILD IC-8 (D1): a field the page shows under a display policy (ORACLE_FIELD_FITS) is fitted here, with this run's
                    # registry, before the lock — both blocks, one path; stored whole
                    fixed_list.append({dk: fix_narrative(REG.fit_cap(dv, *ORACLE_FIELD_FITS[(k, dk)], reg) if (k, dk) in ORACLE_FIELD_FITS
                                                         else dv, f"{k}[{i}].{dk}")
                                       if isinstance(dv, str) and dk not in ID_KEYS else dv for dk, dv in item.items()})
                else:
                    fixed_list.append(item)
            ma_fixed[k] = fixed_list
        else:
            ma_fixed[k] = v
    if not NR.clean_text(REG.unmark(ma_fixed.get("hero_title"))):     # B3 (D2): a title the integrity check emptied → the neutral title (IC: marks are no words)
        ma_fixed["hero_title"] = "Strategic &amp; Operational Blueprint"

    # ── ENDPOINT COUNT: Template-level composition (research: "Debugging the
    # Methodology Inventory Prepend Bug"). The inventory sentence is a SEPARATE
    # template variable — never concatenated into Claude's text. The template
    # renders {{ data_inventory_sentence }} before {{ methodology }}. BUILD B3 (D2): the regex that rewrote the model's endpoint count,
    # the evidence-title override and the sensitivity rewrite are gone — the lock (the model cites [[data.endpoint_count]]) replaces them.
    actual_ep_count = len(data_sources)
    data_inventory_sentence = ""
    if actual_ep_count > 0:
        ep_parts = [f"{_html.escape(ds['id'])} ({ds['records']} records)" for ds in data_sources]     # BUILD IC-5 (D2 f): each name escaped
        total_records = sum(ds["records"] for ds in data_sources)
        ep_label = "API endpoint" if actual_ep_count == 1 else "API endpoints"
        data_inventory_sentence = (
            f"Analysis integrates {actual_ep_count} {ep_label} "
            f"({total_records} total records): {', '.join(ep_parts)}."
        )

    # ── COST OF INACTION (BUILD B3, D1/D2): built by Python from registry ids — each year's people served from the P&L, the rate a
    # named constant, the cost per visit a benchmark — and through the lock like any text; no token replacement, no regex fallback ──
    # BUILD IC-4 (D6): with the switch on, from the materiality list's valued outcomes; the material outcomes not valued named beside it
    # (each in its author's words: the analysis's through the lock, the contract's as the organizers', Python's as its own)
    mat_ctx = {}
    if ic4_prompt() and isinstance(fin.get("materiality"), list):
        def _mat_words(r_, k_):
            o_ = (fix_narrative(_html.escape(str(r_.get("outcome") or "")), f"materiality[{k_}].outcome") if r_.get("i") is not None
                  else REG.mark("org", _html.escape(str(r_.get("outcome") or ""))))
            w_ = str(r_.get("why") or "")
            return o_, (fix_narrative(_html.escape(w_), f"materiality[{k_}].why") if r_.get("by") == "model" and w_ else
                        REG.mark("py", _html.escape(w_)) if w_ else "")
        core_w = {c["id"]: c["words"] for c in contract_outcomes(doc)}
        rows_m, nv_m = [], []
        for k_, r_ in enumerate(fin["materiality"]):
            o_, w_ = _mat_words(r_, k_)
            if r_.get("core") and r_.get("i") is not None and r_["core"] in core_w:
                o_ += REG.mark("py", " — the contract's core outcome: ") + REG.mark("org", _html.escape(core_w[r_["core"]]))
            rows_m.append({"outcome": o_, "status": r_.get("status"), "status_key": OUTCOME_STATUS.index(r_["status"]) if r_.get("status") in OUTCOME_STATUS else -1,
                           "why": w_ or REG.mark("py", "—")})
            if r_.get("status") == "material, not valued":
                nv_m.append(o_ + REG.mark("py", " (") + w_ + REG.mark("py", ")"))
        nv_html = REG.mark("py", "; ").join(nv_m)
        if reg.get("coi_3yr"):
            coi_src = (f"Without the program, the outcomes it would produce do not happen. For each year, each valued outcome is counted among "
                       f"that year's {_html.escape(unit_p)} served (its rate × the people), times its value, less deadweight ([[deadweight]]). "
                       "Year 1 ([[beneficiaries_y1]] served): <strong>[[coi_y1]]</strong>. Year 2 ([[beneficiaries_y2]] served): "
                       "<strong>[[coi_y2]]</strong>. Year 3 ([[beneficiaries_y3]] served): <strong>[[coi_y3]]</strong>. "
                       "Cumulative over [[plan.years]] years: <strong>[[coi_3yr]]</strong>.")
            ma_fixed["cost_of_inaction"] = REG.mark("py", REG.unmark(fix_narrative(coi_src, "cost_of_inaction", model=False))
                                                    + (" Not in this figure — material, not valued: " + nv_html + "." if nv_m else ""))
            console.print("    [green]✓ COI section generated in Python from the materiality list[/green]")
        elif rows_m:
            ma_fixed["cost_of_inaction"] = REG.mark("py", "The cost of inaction is not computed: no material outcome is valued."
                                                    + (" Material, not valued: " + nv_html + "." if nv_m else ""))
        mat_ctx = {"materiality": rows_m, "sroi_partial": bool(fin.get("sroi_partial")) and bool(reg.get("sroi")),
                   "sroi_partial_line": REG.mark("py", "Partial: not counted, because not valued — ") + nv_html,
                   "materiality_intro": ("Every outcome the analysis proposed, and each core outcome the contract names, by materiality: "
                                         "material if missing it out would misrepresent what the program does (the SROI Guide). Only "
                                         "the valued outcomes are counted, in the ratio and in the cost of inaction.")}
    elif reg.get("coi_3yr"):
        coi_src = (f"Without coordinated intervention, an estimated [[coi_rate]] of the [[beneficiaries_y1]] {_html.escape(unit_p)} served in "
                   "Year 1 will have at least one avoidable ER visit a year — <strong>[[coi_visits_y1]] visits</strong> at "
                   "<strong>[[ext.er_visit_cost]]</strong> each. Year 1: [[coi_visits_y1]] × [[ext.er_visit_cost]] = <strong>[[coi_y1]]</strong>. "
                   "Year 2 ([[beneficiaries_y2]] served, [[coi_visits_y2]] visits): <strong>[[coi_y2]]</strong>. "
                   "Year 3 ([[beneficiaries_y3]] served, [[coi_visits_y3]] visits): <strong>[[coi_y3]]</strong>. "
                   "Cumulative <span data-num-exempt=\"a label: the contract's three years\">3</span>-year cost: <strong>[[coi_3yr]]</strong>.")
        # B3 (Phase 4, review a F2): the fixed sentence comparing this with the SROI ("the full cost-of-inaction accounts for repeat ER
        # utilization among the broader displaced population") is gone — B3's cost of inaction counts one visit for 3% of the P&L's own
        # people served, so the sentence contradicted the figures beside it (and named Chicago's population on every page)
        ma_fixed["cost_of_inaction"] = REG.mark("py", REG.unmark(fix_narrative(coi_src, "cost_of_inaction", model=False)))   # IC (review a F2): Python's words
        console.print("    [green]✓ COI section generated in Python from registry ids[/green]")

    prio = prioritization_section(results.get("prioritization"))          # BUILD B1 (D4, P2.2)
    funds = use_of_funds_section(fin.get("use_of_funds"), ma, F)     # BUILD B1 (D4, P2.3; review #2: the words as written, not ma_fixed)
    # B3 (Phase 4, review b F2): the fund lines' and the reserve's words through the lock — the page shows each resolved (*_h), the chat
    # and the sections that reuse them read the plain form; a raw [[…]] reaches neither
    # BUILD IC-8 (D3): each line's, row's and role's field named with its position (one unit each); its plain words one decision with the
    # page's (_plain_twin) — a role fell back to its raw text, its tokens with it
    for j_, ln_ in enumerate(funds.get("lines") or []):
        for f_ in ("owner", "cadence", "indicator"):
            ln_[f_ + "_h"] = fix_text(ln_[f_], f"fund_lines[{j_}].{f_}")
            ln_[f_] = _plain_twin(ln_[f_], f"fund_lines[{j_}].{f_}") or "not given"
    for j_, a_ in enumerate((funds.get("alloc") or {}).get("rows") or []):
        for f_ in ("item", "audience", "indicator"):
            a_[f_ + "_h"] = fix_text(a_[f_], f"marketing_allocation[{j_}].{f_}")
            a_[f_] = _plain_twin(a_[f_], f"marketing_allocation[{j_}].{f_}") or "not given"
    for i_, r_ in enumerate(funds.get("roles") or []):     # BUILD IC-3 (t_rewrites_ic3): a role's title through the lock as the lines'
        r_["role_h"] = fix_text(r_["role"], f"use_of_funds.roles[{i_}].role")     # words are — a token in it never shown raw
        r_["role"] = _plain_twin(r_["role"], f"use_of_funds.roles[{i_}].role") or "not given"
    for line in funds.get("notes") or []:
        NR.say(f"    use of funds: {line}")
    # BUILD B3 (D3–D7): the deliverables — the call's words checked against this run's registry; the Python parts from the engine
    eps_ = [ds["id"] for ds in data_sources]
    dview = deliverables_view(results.get("deliverables") or {}, reg, fin, ma, F, eps_)
    cpath_ = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    spec_ = _load_spec(cpath_)[0] if cpath_ else None
    ann_, b1_ = os.environ.get("CURVEBALL_TEXT", ""), (block1_base(doc) if is_block2() and doc else None)
    raw_ = data.get("raw") or {}
    bn = (bottleneck_section(ENG.bottlenecks(spec_, doc, raw_, results.get("prioritization") or {}, ann_, b1_), reg) if spec_
          else {"show": False, "why": "no engine sidecar for this challenge"})
    yo = (youth_section(ENG.age_protocol(spec_, doc, raw_, results.get("prioritization") or {}, ann_, b1_), dview, reg, fix_text) if spec_
          else {"show": False, "why": "no engine sidecar for this challenge"})
    kf = kpi_framework_section(dview, reg, F, funds, fix_text)
    rgates = renewal_gates_section(dview, reg, eps_, fix_text)
    for nm, sec_ in (("bottleneck dashboard", bn), ("youth handoffs", yo)):
        if not sec_.get("show"):
            NR.say(f"    {nm}: not on this page — {sec_.get('why')}")
    NR.say(f"    deliverables: {dview['status']} — {len(dview['kpis'])} indicator(s), {len(dview['failure_modes'])} failure mode(s), "
           f"renewal gate {'set' if dview['renewal'] else 'not set'}; {len(dview['dropped'])} entr(ies) left out")
    for w_, why_ in dview["dropped"]:       # BUILD IC-5 (D13): each left-out entry named (a source name or a confidence the checks refused)
        NR.say(f"      left out: {w_} — {why_}")
    # P0.3 (D10): the chat sees what the page shows; (D4, D8) the page's attributes, metas and brief panel
    brief_doc, brief_ans = _brief_for_page(ma_fixed)
    # P0.6 (D9): the SROI rows' markers become badges by the same function as the narrative's (never fix_narrative: its cost patterns
    # would rewrite a unit value such as "$45 per household"); the chat context reads these converted rows through clean_text
    # B3 (D1): the SROI derivation — each row's unit value, rate and duration as registry spans; the outcomes per year and the Year-1
    # value as Python computed them (the table's data-prov-block); the model's source words through the lock
    sroi_rows = []
    for r in fin.get("sroi_rows") or []:
        if not isinstance(r, dict) or "n" not in r:
            continue
        n_ = r["n"]
        sroi_rows.append({"category": fix_narrative(_html.escape(str(r.get("category") or "")), f"sroi_rows[{n_}].category"),   # IC-3: its
                          "kind": r.get("unit_kind"),          # tokens resolve as the source's do — never shown raw (t_rewrites_ic3)
                          "proxy": reg.span(f"sroi.{n_}.unit_value"), "rate": reg.span(f"sroi.{n_}.rate"), "duration": reg.span(f"sroi.{n_}.duration"),
                          "outcomes": " · ".join(REG.fmt_value(x, "count") for x in r.get("outcomes") or []),
                          "annual": REG.fmt_value(r.get("annual_y1"), "money"),
                          "source": fix_narrative(_html.escape(str(r.get("source") or "")), f"sroi_rows[{n_}].source"),
                          "basis_of": f"sroi.{n_}.unit_value" if r.get("unit_kind") == "MODELED" else None})   # BUILD IC-5 (D6): the cell
    #                                       that states a MODELED value's basis says so (data-basis-of): its shares are the writer's assumption
    fin_page = dict(fin, sroi_rows=sroi_rows)
    # P0.7 (D3, D7): Block 2's record — the section, the badge's first sentence and the chat context's CURVEBALL block (None in Block 1)
    cb_rec = curveball_record(results)
    cb_changes = _changes_from_env()[0] if cb_rec else None
    # BUILD B3 (D8, D9): the honesty panel (a judge question links only to a section this page shows), the risk register's early warnings,
    # incident mode, and each hero card's Block 1 value for the toggle (both registries)
    sg = sensitivity_grid(results, reg, F)                                   # BUILD B3 (D12): before the honesty panel, which it ranks
    if not sg.get("show"):
        NR.say(f"    sensitivity grid: not on this page — {sg.get('why')}")
    b3_secs = {"bottlenecks": bn, "youth-handoffs": yo, "kpi-framework": kf, "renewal-gates": rgates, "honesty": True,
               "sensitivity-grid": sg.get("show")}
    hon = honesty_section(reg, integ, dview, fix_text, doc, _oracle_present(fin, ma_fixed, prio, funds, dict(b3_secs, rubric=True)),   # review d F14
                          grid={"rows": sg["all_rows"]} if sg.get("show") else None)
    ew = early_warning_cells(dview, ma_fixed.get("risks") or [], reg, fix_text)
    inc = incident_section(reg, results, cb_rec)
    rub = rubric_section(doc, _oracle_present(fin, ma_fixed, prio, funds, dict(b3_secs, honesty=hon.get("show"), rubric=True)), bool(cb_rec))   # D11
    for k_ in kpis:
        if k_.get("prov") and reg.get("b1." + k_["prov"]):
            k_["b1_shown"] = reg.shown("b1." + k_["prov"])
    # BUILD IC-4 (D13): every list the page shows is built here — counted once (shown_lists), before the chat context and the appendix
    # read the registry; only the counts the texts cite are registered (a page that cites none keeps its registry as it was)
    # BUILD IC-8 (D5): Block 1's headlines, each section's base for the one-by-one check (Block 2)
    b1h_ = ((block1_deliverables() or {}).get("reply") or {}).get("headlines") if is_block2() else None
    b1h_ = {s_: h_ for s_, h_ in (b1h_ or {}).items() if s_ in HEADLINE_SECTIONS and isinstance(h_, str)}
    for h_ in list((dview.get("headlines_pending") or {}).values()) + list(b1h_.values()):      # BUILD IC-5 (D7): a pending
        hold_counts(h_, count_used, lambda n_: None)          # headline's count is one the page registers (IC-8 D5: Block 1's too)
    count_shown.update(shown_lists(dview, ma_fixed, sroi_rows, funds))
    reg.add_counts({n: count_shown[n] for n in sorted(count_used) if n in count_shown}, EXTRA_COUNT_WORDS)
    REG.set_stage(reg, REG.RESOLVED)       # BUILD IC-5 (D7, G13): every producing call and the final filtering done — counts exist
    check_headlines(dview, reg, b1h_, (((cb_rec or {}).get("edits") or {}).get("status") or {}).get("d.headlines"), is_block2())   # IC-8 D5
    cb_chat = NR.changes_chat_block(cb_changes or {"curveball": {"text": cb_rec.get("announcement"), "tests": cb_rec.get("tests")}, "summary": []},
                                    extra=curveball_chat_lines(cb_rec, brief_doc, dview.get("headline_units"))) if cb_rec else ""
    chat_ctx = build_chatbot_context(ma_fixed, fin_page, NR.brief_chat_block(brief_doc, "oracle", brief_ans) if brief_doc else "", F,
                                     curveball=cb_chat, engine=prio, funds=funds)
    chat_ctx = chat_ctx + deliverables_chat(dview, kf, bn, yo, rgates, reg, hon, sg, rub, inc)     # BUILD B3 (D13): the new sections, in plain words
    chat_ctx = REG.unmark(chat_ctx)     # BUILD IC (D1): marks never leave the process
    chat_ctx = count_marks(chat_ctx, reg, "plain")      # BUILD IC-4 (D13): a held count, plain
    cov = NR.page_context("oracle", answers=brief_ans, present=_oracle_present(fin, ma_fixed, prio, funds, dict(b3_secs, honesty=hon.get("show"),
                                                                                                        rubric=rub.get("show"))))   # BUILD IC-5 (D1 e): Python's, never admitted
    for r_ in ((cov.get("panel") or {}).get("rows") or []):   # B3 (run turn R4, finding 2): the panel's answers keep their registry spans
        h_ = brief_ans.get(r_.get("id")) if r_.get("answer_src") == "model" else None
        if h_:
            a_ = _panel_html(h_)
            if NR.clean_text(a_) == r_.get("answer"):          # the same words as P0.3's plain answer, or the plain answer stays
                r_["answer_html"] = a_
    if prio.get("show"):     # BUILD B1 (review #3): the coverage counts a named user only where the page shows that person's case
        prio["attrs"] = prio_attrs((cov.get("attrs") or {}).get("prioritization", ""),
                                   {p["named_user"] for p in prio["people"] if p.get("named_user")})

    ctx_ = {
        "CBM": marks,           # BUILD IC-4 (D11): the deliverables' marks by id, the text fields' by field
        "page_title": f"{ma_fixed.get('program_name', 'Blueprint')} — Strategic & Operational Blueprint",
        # BUILD IC-8 (D7, Q-OG): the head's preview — the hero lead as the page shows it (its source badges dropped), whole leading sentences
        "preview": REG.shown_text(REG.page_preview(ma_fixed.get("hero_lead") or "", reg, html=True, drop_class="src-link", chain=[
            ("default", f"{NR.clean_text(ma_fixed.get('program_name')) or 'The program'}: the strategic and operational blueprint.")])[0], reg),
        "brand_html": NR.clean_brand_html(ma_fixed.get("brand_html"), ma_fixed.get("program_name") or "Blueprint"),  # P0.6 (D3)
        "badge_text": ma_fixed.get("badge_text", ""),
        "hero_title": ma_fixed.get("hero_title", "Strategic & Operational Blueprint"),
        "hero_lead": ma_fixed.get("hero_lead", ""),
        "kpis": kpis,
        "summary_title": ma_fixed.get("summary_title", ""),
        "bluf_text": ma_fixed.get("bluf_text", ""),
        "decision_text": ma_fixed.get("decision_text", ""),
        "crisis_title": ma_fixed.get("crisis_title", "The Problem"),
        "crisis_body": ma_fixed.get("crisis_body", ""),
        "crisis_human": ma_fixed.get("crisis_human", ""),
        "crisis_sources": ma_fixed.get("crisis_sources", ""),
        "solution_title": ma_fixed.get("solution_title", "Our Approach"),
        "solution_body": ma_fixed.get("solution_body", ""),
        "solution_population": ma_fixed.get("solution_population", ""),
        "cost_of_inaction": ma_fixed.get("cost_of_inaction", ""),
        "program_name": ma_fixed.get("program_name", "Program"),
        "comparison_rows": ma_fixed.get("comparison_rows", []),
        "evidence_title": ma_fixed.get("evidence_title", ""),
        "evidence_cards": ma_fixed.get("evidence_cards", []),
        "evidence_dropped": sum(1 for d_ in integ["dropped"] if str(d_.get("what", "")).startswith("evidence card")),   # B3 (review b F9)
        "operations_title": ma_fixed.get("operations_title", ""),
        "operations_description": ma_fixed.get("operations_description", ""),
        "diagram_svg": diag.get("svg"),
        "diagram_mermaid": _html.escape(_html.unescape(diag.get("mermaid") or "")),   # BUILD IC-5 (D2 f): the model's source, escaped once
        "diagram_why": diag.get("why") or "",          # BUILD IC-6 (D10): why the browser draws it (words, no path); "" when not said
        "diagram_steps": bool(diag.get("steps")),      # BUILD IC-8 (D4): no drawing — the workflow listed as steps
        "diagram_list": diagram_list(diag.get("code") or diag.get("mermaid") or (diagram_source(ma["diagram"], reg)[0] if has_diagram_data(ma)
                                     else ma.get("mermaid_code") or ""), reg),     # BUILD IC-6 (D10): the diagram as a list (an older
        #                                                       checkpoint keeps no code when mmdc drew it: Python's code again, or the reply's)
        "financial_title": ma_fixed.get("financial_title", ""),
        "scenarios": scenarios,
        "chart_data": fin.get("chart_data", {}),
        "sroi_rows": sroi_rows,
        "sroi_adjustments": fin.get("sroi_adjustments", {}),
        "sroi_pv_total": reg.span("sroi_pv_benefits") if reg.get("sroi") else "",      # B3 (D1): registry spans; no "capped" note
        "sroi_pv_cost": reg.span("sroi_pv_cost") if reg.get("sroi") else "",
        "sroi_display": reg.span("sroi") if reg.get("sroi") else "",
        "sroi_below": actual_sroi is not None and actual_sroi < 1,
        "sroi_horizon": reg.span("sroi_horizon_years") if reg.get("sroi_horizon_years") else "",
        "sroi_notes": fin.get("sroi_notes") or [],
        "sroi_adj": {k: reg.span(k) for k in ("deadweight", "attribution", "displacement", "drop_off_rate", "discount_rate")},
        # BUILD IC-4 (D6): the analysis's class, the rate's source, and the sensitivity pair's rows (each a registry figure)
        "sroi_method": SROI_METHOD, "sroi_rate_source": SROI_RATE_SOURCE, "sroi_sens_source": SROI_SENS_SOURCE,
        **mat_ctx,            # BUILD IC-4 (D6): the materiality list, "partial" and its line — the switch on only
        "sroi_sens": [{"rate": reg.span(s_["rate_id"]), "ratio": reg.span(s_["id"])} for s_ in fin.get("sroi_sens") or []
                      if reg.get(s_.get("rate_id")) and reg.get(s_.get("id"))],
        # IC-3 (ledger 28): the typed figures and the forms read again here from the model's code (the stage's are its checkpoint's)
        "diagram_traced": (not diag_typed_[0]) if diag_typed_ is not None else bool(diag.get("traced")),
        "diagram_untraced": diag_typed_[0] if diag_typed_ is not None else (diag.get("untraced") or []),
        "diagram_forms": diagram_forms(diag_typed_[1]) if diag_typed_ is not None else (diag.get("forms") or {}),   # IC-2 (D4, D6)
        "pilot_stated": bool(F["pilot_days"]),        # B3 (done-test 5): a brief with no pilot — the plan says its timeline is its own
        "sensitivity_text": ma_fixed.get("sensitivity_text", ""),
        "plan_title": ma_fixed.get("plan_title", ""),
        "phases": ma_fixed.get("phases", []),
        "risks": ma_fixed.get("risks", []),
        "renewal_label": ma_fixed.get("renewal_label", "Renewal Trigger"),
        "renewal_text": ma_fixed.get("renewal_text", ""),
        "appendix_title": ma_fixed.get("appendix_title", ""),
        "data_sources": data_sources,
        "data_inventory_sentence": data_inventory_sentence,  # Python-generated, always correct
        "methodology": ma_fixed.get("methodology", ""),
        "ai_process": ma_fixed.get("ai_process", ""),
        "assumptions": list(ma_fixed.get("assumptions", [])),     # B3 (D1): no line claiming a 4.0 ratio — nothing rescales it
        "team_name": os.environ.get("TEAM_NAME", "Ctrl Alt Defeat"),
        "competition_label": NR.label_from_env(doc),          # P0.6 (D6): COMPETITION_LABEL, else the contract's, else "NAIPC"
        "chartjs_inline": vendored_text(CHARTJS_FILE, CHARTJS_SHA256),   # P0.6 (D10): Chart.js inlined; None → the CDN tag
        "mermaid_local": vendored_ok(MERMAID_FILE, MERMAID_SHA256),        # P0.6 (D10): render_html copies it beside the page
        "indirect_rate_pct": round(float((fin.get("simulator") or {}).get("indirect_rate", 0.15)) * 100),  # P0.6 (D7): the rate used
        # Closing statement — the "mic drop" computed from financial outputs (P0.6, D1, D2: every value from the run and the brief)
        "closing_statement": closing_statement(ma_fixed, fin, F),
        # Chatbot (P0.11b) — the page carries only the context digest; the proxy holds the key
        "chatbot_context": chat_ctx,
        **chat_fields("oracle", chat_ctx, build_oracle_faq(ma_fixed, fin, F, reg)),
        "cov": cov,  # P0.3
        "prio": prio,    # BUILD B1 (D4): the prioritization model
        "funds": funds,  # BUILD B1 (D4): the use of funds
        # QR code — predictable Netlify URL, falls back to empty if segno not installed
        "qr_svg": generate_qr_svg(os.environ.get("SITE_URL", "https://ctrl-alt-defeat.netlify.app")),
        # Curveball badge — set CURVEBALL_TEXT env var during Block 2 re-run
        "curveball_text": os.environ.get("CURVEBALL_TEXT", ""),
        "curveball_first": badge_sentence(os.environ.get("CURVEBALL_TEXT", "")),   # P0.7 (D3): the badge shows the first sentence
        "cb": update_words_locked(curveball_section(cb_rec, brief_doc, reg=reg, units=dview.get("headline_units")), cb_rec, ma.get("curveball_update"), fix_text),   # P0.7 (D3); IC (D5)
        # What-If Simulator — Year 1 sensitivity model for interactive exploration
        "simulator_model": fin.get("simulator", {}),
        "sim_labels": sim_labels,      # P0.6 review F1: its labels (the budget by source, the unit)
        # Architect deep link — set ARCHITECT_URL env var on competition day for Full Stack synergy
        "architect_url": os.environ.get("ARCHITECT_URL", ""),
        # BUILD B3 (P0.4, P0.5): the registry (its appendix table) and the integrity check — render_html fills "integrity" from the page
        "bottlenecks": bn, "youth": yo, "kpi_framework": kf, "renewal_gates": rgates, "dview": dview,   # BUILD B3 (D4–D7)
        "honesty": hon, "early_warnings": ew, "incident": inc,                                            # BUILD B3 (D8, D9)
        "_incident": lambda: incident_section(reg, results, cb_rec, quiet=True),     # BUILD IC-8 (d15e): made again after a render
        "sensitivity_grid": sg, "rubric": rub,                                                            # BUILD B3 (D12, D11)
        "headlines": {s_: fix_text(h_, f"headlines.{s_}") for s_, h_ in (dview.get("headlines") or {}).items()
                      if s_ in HEADLINE_SECTIONS},                                                         # BUILD B3 (D10)
        "toggle": bool(inc.get("show") and any(k_.get("b1_shown") and k_["b1_shown"] != reg.shown(k_["prov"]) for k_ in kpis)),   # review d F4
        "registry_rows": registry_rows(reg),
        "registry_problems": list(reg.problems),
        "registry_checks": registry_check_rows(reg),
        "integrity": None,
        "_reg": reg, "_exempter": exempter, "_integ": integ,
        "_units": {"ma": ma, "dlv": results.get("deliverables"), "cb_rec": cb_rec, "doc": doc, "diag": diag,     # BUILD IC-8 (D3)
                   "headlines": dview.get("headline_units"), "unit_of": unit_of},
    }
    # BUILD IC (D4; review b F5): a key that matched nothing is named — after every field the lock reads (the headlines and the update's
    # words go through fix_text inside the context above)
    for k_ in sorted(set(overrides) - integ["overrides_used"]):
        NR.say(f"    ⚠ integrity override {k_[:12]}…: matches no flagged or removed sentence of this run")
    return count_marks(ctx_, reg)      # BUILD IC-4 (D13): each held count printed, now that every list is counted


def page_lists(ctx: dict) -> dict:
    """BUILD IC-4 (D4): the lists this page shows, after every filter, by the noun a typed count names — what "four KPIs" is checked
    against (REG.typed_category). BUILD IC-4 (D13): read from shown_lists, the one definition [[count:<list>]] prints too."""
    n = shown_lists(ctx.get("dview"), ctx, ctx.get("sroi_rows"), ctx.get("funds"))
    return {"kpi": n["kpis"], "risk": n["risks"], "phase": n["phases"], "failure mode": n["failure_modes"], "question": n["judge_qa"]}


EXTRA_COUNT_WORDS = {f: f.replace("_", " ") for f, fi in MasterAnalysis.model_fields.items()        # IC-4 Phase 7 (review c16): the
                     if __import__("typing").get_origin(fi.annotation) is list and f not in REG.COUNT_LISTS}   # schema's other lists


def shown_lists(dv, ma, sroi_rows=None, funds=None) -> dict:
    """BUILD IC-4 (D4, D13): how many items each list of the page shows, after every filter, by its field name in a reply's schema
    (REG.COUNT_LISTS): the deliverables' lists from the page's view of them (dview), the analysis's from its fixed lists (the page context's
    lists are the same objects), the outcomes the SROI rows the page shows, the staff roles only where the use of funds is shown."""
    dv, ma, funds = dv or {}, ma or {}, funds or {}
    n = {f: len(dv.get(f) or []) for f in REG.DELIVERABLE_ITEM_FIELDS}
    n.update({f: len(ma.get(f) or []) for f in ("risks", "phases", "evidence_cards", "comparison_rows")})
    n["sroi_outcomes"] = len(sroi_rows or [])
    n["staff_roles"] = len(funds.get("roles") or []) if funds.get("show") else 0
    n.update({f: len(ma.get(f)) for f in EXTRA_COUNT_WORDS if isinstance(ma.get(f), list)})     # IC-4 Phase 7 (review c16)
    return n


COUNT_HOLD = ("\ue0b1", "\ue0b2")       # BUILD IC-4 (D13): a held count — private-use code points outside REG.MARK_CHARS; never leaves the process
_COUNT_TOKEN = re.compile(r"\[\[\s*count:\s*([A-Za-z_]{1,40})\s*\]\]", re.I)
_COUNT_HELD = re.compile(COUNT_HOLD[0] + r"([a-z_]{1,40})" + COUNT_HOLD[1])


def hold_counts(text, used, register):
    """BUILD IC-4 (D13): each [[count:<list>]] that names a page list (REG.COUNT_LISTS) → a held marker, printed once the page's lists are
    counted (count_marks); `used` gains the list and `register(list)` runs. Any other count token is left for resolve, where it names
    nothing and its sentence leaves the page (D4's unknown id)."""
    if not isinstance(text, str) or "count:" not in text.lower():
        return text

    def one(m):
        n = m.group(1).lower()
        if n not in REG.COUNT_LISTS and n not in EXTRA_COUNT_WORDS:      # IC-4 Phase 7 (review c16): any list of the schema
            return m.group(0)
        used.add(n)
        register(n)
        return f"{COUNT_HOLD[0]}{n}{COUNT_HOLD[1]}"
    return _COUNT_TOKEN.sub(one, text)


def count_marks(v, reg, mode="html"):
    """BUILD IC-4 (D13): every held count in a text — or in the strings of a dict, list or tuple, at any depth; a key starting "_" is the
    page's own object and is left as it is — printed through the registry: html, resolve's span (the bare form inside a tag's attribute);
    plain, the shown form. A string keeps its type (a markup string stays markup)."""
    if isinstance(v, str):
        if COUNT_HOLD[0] not in v:
            return v
        if mode == "plain":
            out = _COUNT_HELD.sub(lambda m: reg.resolve(f"[[count:{m.group(1)}]]", mode="plain")[0], v)
        else:
            out = reg.resolve(_COUNT_HELD.sub(lambda m: f"[[count:{m.group(1)}]]", v), mode="html")[0]
        return v.__class__(out) if type(v) is not str else out
    if isinstance(v, dict):
        return {k: (x if str(k).startswith("_") else count_marks(x, reg, mode)) for k, x in v.items()}
    if isinstance(v, list):
        return [count_marks(x, reg, mode) for x in v]
    if isinstance(v, tuple):
        return tuple(count_marks(x, reg, mode) for x in v)
    return v


# BUILD IC-8 (D3): the top-level fields whose place the template shows only when they have words ({% if … %}), and the identity fields
ORACLE_OMITTABLE = ("crisis_sources", "solution_population", "renewal_text") + IDENTITY_FIELDS


def oracle_chain_links(field, program=""):
    """BUILD IC-8 (D3): a text field's chain links after Block 1's — fix_narrative's field name → (its default as plain words, or None;
    omittable): a top-level field's own default (REG.oracle_default: the titles, the lead, the badge), or omittable when the template
    shows its place only with words; an evidence card's part: omittable (the card goes whole when its title or figure is emptied, as
    before); a brief answer and a curveball test's answer: REG.ANSWER_DEFAULT; a fund line's, an allocation row's and a role's words: "not
    given" (the words their plain form shows for none); anything else (None, False): the prose default, REG.UNIT_DEFAULT."""
    f = str(field or "")
    if re.fullmatch(r"\w+", f):
        return REG.oracle_default(f, program), f in ORACLE_OMITTABLE
    if re.match(r"evidence_cards\[\d+\]\.", f):
        return None, True
    if re.fullmatch(r"brief_answers\[\d+\]\.answer|curveball\.tests(?:_other)?\[\d+\]\.answer", f):
        return REG.ANSWER_DEFAULT, False
    if re.fullmatch(r"(?:fund_lines|marketing_allocation)\[\d+\]\.\w+|use_of_funds\.roles\[\d+\]\.role", f):
        return "not given", False
    return None, False


def _plain_unit(reg, text, default=None, omittable=False):
    """BUILD IC-8 (D3): an update text's plain words (the chat, curveball.json) under THE unit rule and chain — Block 2's own words, no
    base: the dead sentences out when the rule allows; else its default; else "" (omittable); else the prose default. The page's text is
    the same text through fix_narrative: the same rule decides."""
    if not isinstance(text, str):
        return text
    return REG.resolve_track_units(reg, text, None, False, None, None, (), default_of=lambda p: default,
                                   omittable_of=lambda p: omittable, generic=REG.UNIT_DEFAULT)


def held_as_numbers(text):
    """BUILD IC-8 (D3): a text with each count this page holds read as a number — hold_counts's own pattern and names (a page list's
    [[count:<list>]], printed once the page's lists are counted): a rule read before they are counted (REG.names_nothing on a KPI's
    indicator) sees no hole there. Any other count token stays as written: resolve reads it, and it names nothing."""
    if not isinstance(text, str) or "count:" not in text.lower():
        return text
    return _COUNT_TOKEN.sub(lambda m: "12" if m.group(1).lower() in REG.COUNT_LISTS or m.group(1).lower() in EXTRA_COUNT_WORDS
                            else m.group(0), text)


# ═══ BUILD IC-8 (D2, D3, I3): the Oracle's one outcome per unit ═══
ORACLE_DECOMPOSED = {"diagram": ("f.diagram", "f.mermaid_code"), "headlines": ("d.headlines",)}      # recorded by their parts when
#                                            the page decided them (D4: the diagram's outcome and its steps; D5: each headline member)
_ORACLE_CONTENT = {}        # finalize_oracle's counts (Dispositions.content) — the status file's "content" (_track_end)
_ORACLE_FINALIZED = set()   # the (run id, invocation id) whose outcomes this process wrote — a run renders its page twice


def _kept_moved(ctx, u_, status):
    """BUILD IC-8 (D2, F6): the kept items whose shown figure moved without an edit — a token of the item's text names a figure the
    curveball changed or one that moved since Block 1 (REG.cites_changed on this run's registry: the review set's own rule (1)). → ids."""
    reg = (ctx or {}).get("_reg")
    kept = {i for i, s in (status or {}).items() if s == "kept"}
    if reg is None or not kept:
        return set()
    items, _o = update_review_set(u_.get("ma"), u_.get("dlv"), None, None, u_.get("doc"))
    marks = REG.changed_marks(reg)
    return {i for i in kept if i in items and REG.cites_changed(reg, list(_texts(items[i].get("value"))), marks)}


def finalize_oracle(ctx):
    """BUILD IC-8 (D2, D3, I3): the Oracle's one outcome per unit — once per run and invocation, after the page's first full render (a run
    renders it twice: the html stage, then _render_partial, which writes the same bytes), so every display fit the page made, the
    integrity details' among them, is in REG.FITS. The units (ids and paths only, never a text):
      - the items — Block 2 with an edit record: each item of the update's review set and each item it added, by its status
        (REG.EDIT_OUTCOMES; this run's record, curveball_record — the one the console and checkpoints/curveball.json get); else (Block 1,
        or a Block 2 written whole): each item of this run's analysis and deliverables in the review set's order (update_review_set; its
        update./test. slots are an update's), added; an item ORACLE_DECOMPOSED names is recorded by its parts when the page decided
        them (the diagram: oracle.diagram and oracle.diagram.node.<id>, from the stage's outcome; the headlines: oracle.headlines.<section>,
        from check_headlines);
      - each display fit that changed its text and that no unit owns (REG.fit_units: trimmed, a chain's link, omitted — one unit per text
        and destination).
    Writes checkpoints/dispositions_<run id>.jsonl, prints the content line and fills _ORACLE_CONTENT. Under NAIPC_STRICT=1 an exception
    raises; live it is named and the page stands (§1). A context that is not the page's (no "_units") records nothing. → the
    Dispositions, or None."""
    u_ = (ctx or {}).get("_units")
    if not isinstance(u_, dict):
        return None
    key_ = (NR.run_id(), os.environ.get("NAIPC_INVOCATION_ID", "").strip())
    if key_ in _ORACLE_FINALIZED:
        return None
    try:
        disp = REG.Dispositions(NR.BUILD_ID, "oracle", NR.run_id())
        skip_ = set()
        dg_ = u_.get("diag") if isinstance(u_.get("diag"), dict) else {}
        o_ = dg_.get("outcome") if isinstance(dg_.get("outcome"), dict) else None
        if o_:              # BUILD IC-8 (D4): the diagram's outcome, and each step that took its class's word
            disp.expect("oracle.diagram")
            disp.add("oracle.diagram", o_["disposition"], o_["reason"], o_.get("source") or "candidate",
                     op=(REG.EDIT_OUTCOMES.get(o_.get("op")) or (None,) * 4)[3], item="f.diagram")
            for x in dg_.get("node_units") or []:
                u = f"oracle.diagram.node.{x['node']}"
                disp.expect(u)
                if not disp.has(u):
                    disp.add(u, x["disposition"], x["reason"], "default", item="f.diagram")
            skip_.update(ORACLE_DECOMPOSED["diagram"])
        if isinstance(u_.get("headlines"), list) and u_["headlines"]:      # BUILD IC-8 (D5): each headline member, one by one
            for x in u_["headlines"]:
                disp.expect(x["unit"])
                if not disp.has(x["unit"]):
                    disp.add(x["unit"], x["disposition"], x["reason"], x.get("source") or "candidate", item=x.get("item"))
            skip_.update(ORACLE_DECOMPOSED["headlines"])
        # BUILD IC-8 (D3; V26): the integrity step's outcomes (the context's "_integ") before the items — each chain a field took
        # (integ["units"]: its unit's outcome), each sentence it removed (<unit>.s.<key>: omitted, token_names_nothing or its rules), each
        # evidence card dropped (its item: omitted); an item keeps an outcome recorded here
        integ_ = (ctx or {}).get("_integ") or {}
        uo_ = u_.get("unit_of") if callable(u_.get("unit_of")) else (lambda f_: f"text.{f_}")
        for x in integ_.get("units") or []:
            u = f"oracle.{uo_(x.get('ufield') or x['field'])}"
            disp.expect(u)
            if not disp.has(u):
                disp.add(u, x["disposition"], x["reason"], {"base": "base", "default": "default", "generic": "default"}.get(x.get("took"), "candidate"))
        for k_, r in enumerate(integ_.get("removed") or [], 1):
            u = f"oracle.{uo_(r.get('ufield') or r.get('field'))}.s.{(r.get('key') or str(k_))[:12]}"
            rl_ = [x for x in (r.get("rules") or ["unknown"]) if isinstance(x, str)]
            disp.expect(u)
            if not disp.has(u):
                disp.add(u, "omitted", "token_names_nothing" if set(rl_) <= set(TOKEN_RULES) else "+".join(rl_)[:60], "candidate")
        for d in integ_.get("dropped") or []:
            if d.get("item") and not disp.has(f"oracle.{d['item']}"):
                disp.expect(f"oracle.{d['item']}")
                disp.add(f"oracle.{d['item']}", "omitted", d.get("reason") or "dropped", "candidate", item=d["item"])
        rec_ = u_.get("cb_rec") if isinstance(u_.get("cb_rec"), dict) else None
        ed_ = rec_.get("edits") if rec_ is not None and isinstance(rec_.get("edits"), dict) else None
        if ed_ is not None and isinstance(ed_.get("status"), dict) and ed_["status"]:
            moved_ = _kept_moved(ctx, u_, ed_["status"])           # BUILD IC-8 (D2, F6): kept, figure changed
            for iid, st_ in ed_["status"].items():
                if iid in skip_ or disp.has(f"oracle.{iid}"):      # D3: an outcome the integrity step gave it stands
                    continue
                d_, r_, s_, op_ = REG.EDIT_OUTCOMES.get(st_, REG.EDIT_OUTCOMES["not reviewed"])
                if st_ == "kept" and iid in moved_:
                    r_ = "figure_changed"
                disp.expect(f"oracle.{iid}")
                disp.add(f"oracle.{iid}", d_, r_, s_, op=op_, item=iid)
        else:
            _items, order_ = update_review_set(u_.get("ma"), u_.get("dlv"), None, None, u_.get("doc"))
            for iid in order_:
                if iid in skip_ or iid.startswith(("update.", "test.")) or disp.has(f"oracle.{iid}"):
                    continue
                disp.expect(f"oracle.{iid}")
                disp.add(f"oracle.{iid}", "added_valid", "whole", "candidate", item=iid)
        REG.fit_units(disp, "oracle")
        why_ = disp.write(CHECKPOINT_DIR / f"dispositions_{NR.run_id()}.jsonl", {"invocation": os.environ.get("NAIPC_INVOCATION_NUMBER")})
        NR.say(f"  {disp.content_line('Oracle')}" + (f" (dispositions not written: {why_})" if why_ else ""))
        _ORACLE_CONTENT.clear()
        _ORACLE_CONTENT.update(disp.content())
        _ORACLE_FINALIZED.add(key_)
        return disp
    except Exception as e:  # noqa: BLE001 — §1: live, the page stands and the record's loss is named
        if REG.strict():
            raise
        NR.say(f"  ⚠ the Oracle's dispositions not recorded ({type(e).__name__}) — the page stands")
        return None


def render_html(ctx: dict):
    """Render Jinja2 template with the given context."""
    # BUILD IC-5 (D2 a): the one environment (REG.page_env: finalize_page, the author filters; was BUILD IC (D1)'s default-deny here)
    env = REG.page_env(Environment, FileSystemLoader(str(TEMPLATE_PATH.parent)), authored=True, build=NR.BUILD_ID)    # IC-8 (d16c)
    template = REG.page_template(env, TEMPLATE_PATH.name)
    if ctx.get("_reg") is not None:
        REG.set_page_names(ctx["_reg"].names())     # BUILD IC-8 (D12): `| words` reads this run's names
    if ctx.get("mermaid_local") and ctx.get("diagram_mermaid") and not ctx.get("diagram_svg"):
        ctx = dict(ctx, mermaid_local=copy_vendored_mermaid(OUTPUT_DIR))   # P0.6 (D10): a failed copy → the CDN tag
    html, notes_ = REG.place_notes(template.render(**ctx), ctx.get("_reg"))   # BUILD IC-6 (D9): each "sources disagree" a note after
    #                                                                    its sentence — on the page as rendered, before the checker reads it
    names_ = ctx["_reg"].names() if ctx.get("_reg") is not None else None
    html, rw_ = REG.reader_page(html, names_, marks=True)      # BUILD IC-8 (d12c; P4.4 p44n: a name in the organizers' run) — before the checker
    reg = ctx.get("_reg")
    if reg is not None:      # BUILD B3 (P0.5): the checker reads the page as built; the line and its details render from what it found
        summary = None
        for _ in range(3):   # the line and the details are exempt, so the counts settle on the second render
            chk_ = REG.check_page(html, ctx.get("_exempter"), reg, page_lists(ctx))      # BUILD IC-4 (D4): the page's lists
            if chk_.get("authored") and not ctx.get("diagram_traced"):       # BUILD IC (D1's named exception): both drawing paths
                chk_ = dict(chk_, diagram_typed=[str(x) for x in ctx.get("diagram_untraced") or []])
            elif ctx.get("diagram_mermaid") and not ctx.get("diagram_svg") and not ctx.get("diagram_traced"):
                # B3 (Phase 4, review b F8): the browser-drawn diagram's code is exempt (its node ids and styles are no figures), but its
                # labels' typed figures are on the page — counted as untraced, as the console says, never left out of M
                typed_ = [str(x) for x in ctx.get("diagram_untraced") or []]
                chk_ = dict(chk_, m=chk_["m"] + len(typed_), untraced=list(chk_["untraced"]) + [
                    {"text": t_, "context": "the workflow diagram's labels (drawn in the browser)", "sec": "operations"} for t_ in typed_])
            df_ = ctx.get("diagram_forms") or {}
            if df_.get("declared") or df_.get("refused") or df_.get("ids"):      # IC-2 (D4, D6): every form listed, the diagram's too
                f0_ = chk_.get("forms") or {}
                chk_ = dict(chk_, forms={"declared": list(f0_.get("declared") or []) + list(df_.get("declared") or []),
                                         "refused": list(f0_.get("refused") or []) + list(df_.get("refused") or []),
                                         "cited": dict(f0_.get("cited") or {}), "diagram_ids": list(df_.get("ids") or [])})
            summary = integrity_summary(chk_, ctx.get("_integ") or {}, reg)
            # B3 (run turn R4, finding 5): the legend's † line only when a figure on the page carries the marker
            inc_ = ctx["_incident"]() if callable(ctx.get("_incident")) else ctx.get("incident")    # BUILD IC-8 (d15e): from the registry
            #                       the previous render filled (a writer's assumption is registered as its text renders)
            html2, notes_ = REG.place_notes(template.render(**dict(ctx, integrity=summary, incident=inc_,
                                                                   unv_shown='<sup class="unv" data-num-exempt="marker"' in html)),
                                            reg)       # BUILD IC-6 (D9): each render's notes placed before the checker reads it
            html2, rw_ = REG.reader_page(html2, names_, marks=True)        # BUILD IC-8 (d12c): each render in a reader's words
            if html2 == html:
                break
            html = html2
        summary["slots"] = slot_counts(chk_, reg, ctx.get("_integ") or {}, summary)    # BUILD IC-3 (D2): kept in the record too
        summary["decoding"] = REG.admit(NR.decode_findings(CHECKPOINT_DIR), "saved", "decode findings")   # BUILD IC-5 (D1); IC-3 (D4): a key given twice in a reply, the last kept
        REG.write_admit_findings(CHECKPOINT_DIR, NR.run_id())       # BUILD IC-5 (D1): what the way in did, into the record by kind
        summary["admit"] = REG.admit_counts(CHECKPOINT_DIR, NR.run_id(), "active invocation")     # BUILD IC-8 (D9): this invocation's
        summary["admit_line"] = REG.admit_words(summary["admit"])
        why = reg.write(CHECKPOINT_DIR / REG.REGISTRY_FILE, summary)
        NR.say(f"    integrity: {summary['line']}; {len(summary['violations'])} violation(s)"
               + ("" if why is None else f" — ⚠ checkpoints/{REG.REGISTRY_FILE} not written ({why})"))
        if ic4_prompt():            # BUILD IC-4 (D16): the other tracks' request parts, built on this run's final registry
            tw_ = REG.write_track_parts(CHECKPOINT_DIR, track_parts(reg, REG.oracle_raw(CHECKPOINT_DIR) or {}, NR.facts(_brief_contract())))
            if tw_ is not None:
                NR.say(f"    ⚠ checkpoints/{REG.TRACK_PARTS_FILE} not written ({tw_}): the Muse's requests go without the registry's parts")
        sl_ = summary["slots"]
        NR.say(f"    integrity: {sl_['offered_not_cited']} id(s) offered and not cited; {sl_['U']} quantit{'y' if sl_['U'] == 1 else 'ies'} with no "
               f"token; {sl_['unit']} unit word(s) that contradict their figure; {sl_['repeated']} id(s) cited more than once in a section")
        fn_ = summary.get("forms_n") or {}
        if any(fn_.values()):       # BUILD IC-2 (D6): the writer's forms on the page, counted
            NR.say(f"    integrity: the writer's forms — {fn_['quoted']} quoted from the organizers' text, {fn_['computed']} computed by "
                   f"Python, {fn_['declared']} declared not figures, {fn_['refused']} refused (on the page as typed, no link)")
        for v in summary["violations"][:12]:
            NR.say(f"    ⚠ integrity {v['rule']}: {v.get('where') or ''} — {str(v.get('context') or v.get('text') or '')[:160]}")
        why_n = REG.write_numerals(CHECKPOINT_DIR / NUMERALS_FILE, chk_, NR.run_id())   # BUILD IC (D2): the claim, numeral by numeral
        #                                                                         (BUILD IC-5 D9: the registry's one writer, the Muse's too)
        if why_n:
            NR.say(f"    ⚠ checkpoints/{NUMERALS_FILE} not written ({why_n})")
    _bal, probs = REG.balance(html)
    for p_ in sorted(set(REG.page_problems() + [f"the page: {x}" for x in probs])):
        NR.say(f"    ⚠ authorship: {p_}")
    for t_ in sorted(set(REG.page_tags())):          # BUILD IC-5 (D2 b): a tag finalize met at output (shown as words; a test fails on one)
        NR.say(f"    ⚠ a tag at output, shown as words: {t_}")
    out = OUTPUT_DIR / "index.html"
    if REG.reader_words_line(rw_):      # BUILD IC-8 (d12c): what the page's pass put in a reader's words
        NR.say(f"    {REG.reader_words_line(rw_)}")
    if REG.notes_words(notes_):         # BUILD IC-6 (D9): the page's notes, said and recorded
        NR.say(f"    {REG.notes_words(notes_)}")
    nw_ = REG.write_page_notes(CHECKPOINT_DIR, notes_, NR.run_id())
    if nw_:
        NR.say(f"    ⚠ checkpoints/{REG.NOTES_FILE} not written ({nw_})")
    out.write_text(REG.strip_build_notes(REG.unmark(html)), encoding="utf-8")       # BUILD IC (D1): no mark leaves the process
    # (IC-4 Phase 7, review c21: no build note in the page's scripts)
    write_chat_context(OUTPUT_DIR, ctx)  # P0.11b: the proxy's context file, uploaded with the page
    finalize_oracle(ctx)                 # BUILD IC-8 (D2, D3): one outcome per unit, once per run — after the page's every fit
    return out


def offered_ids(reg) -> set:
    """BUILD IC-3 (D2): the ids the <registry> block offers by name (registry_prompt_block's lists): each endpoint's count, a one-record
    endpoint's numeric fields, the named figures, the figures Python computes afterwards and the budget, Block 1's values, the benchmarks."""
    out = set()
    for ep, payload in (getattr(reg, "_raw", None) or {}).items():
        try:
            recs = [r for r in NR.records(payload) if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            continue
        if not recs:
            continue
        out.add(f"{ep}.count")
        if len(recs) == 1:
            out |= {f"{ep}.{f}" for f, v in recs[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    out |= {e["id"] for e in [e for e in reg.entries.values() if e["kind"] in ("MEASURED", "DERIVED")
                              and not e["id"].startswith(("b1.", "q.", "calc.", "prio.") + CONSTRAINT_PREFIXES)]}     # IC-8 (K1): whole, as listed
    out |= {i for i, _w in COMPUTED_IDS} | {"budget"} | {k for k in reg.entries if k.startswith(("b1.", "ext."))}
    if ic3_prompt():          # BUILD IC-3 (D7): the table's points and the constraints' figures are offered in their own blocks, as `[[id]] = value`
        out |= {k for k in reg.entries if k.startswith(("prio.",) + CONSTRAINT_PREFIXES)}
        out |= {m["id"] for m in (getattr(reg, "mentions", None) or [])[:MENTIONS_MAX]}      # D6: the list of mentions
    return out


def slot_counts(chk: dict, reg, integ: dict, summary: dict) -> dict:
    """BUILD IC-3 (D2): the console's four counts, in the terms of the slot-error rate — ids the prompt offered and the model did not cite;
    quantities in its prose with no token (U); unit words that contradict their figure (the unit rule's flags); ids it cited more than
    once in one section."""
    nums = chk.get("numerals") or []
    cited = [(x.get("sec"), x["id"]) for x in nums if x.get("author") == "model" and x["status"] == "cited" and x.get("id")]
    seen = {}
    for k in cited:
        seen[k] = seen.get(k, 0) + 1
    return {"offered_not_cited": len(offered_ids(reg) - {i for _s, i in cited}) if reg is not None else 0, "U": summary.get("U", 0),
            "unit": sum(1 for f in integ.get("flagged") or [] if f.get("rule") == "unit") + sum(1 for v in chk.get("violations") or [] if v.get("rule") == "unit"),
            "repeated": sum(1 for _k, n in seen.items() if n > 1)}


NUMERALS_FILE = REG.NUMERALS_FILE         # BUILD IC-5 (D9): the registry's name for it
FLAG_RULES = ("strict", "unit", "quote", "declared", "near_miss", "stale")   # IC-3 (D11): a mention Block 1 cited that the data no longer holds   # IC-3 (D4): a token's near-miss form, flagged and kept              # BUILD IC (D4): flagged and kept, never removed; IC-2 (D6): a quote the
#                                                                   text does not hold, a declaration that reads as a quantity (each typed)
ID_KEYS = ("id", "key", "line", "salary_ref", "unit_value_ref")   # BUILD IC (D1): identifiers in the analysis's lists — never through the lock
TOKEN_RULES = ("unknown", "malformed", "calc")                    # BUILD IC (D4): a token naming no figure — removed; `keep` does not apply;
#                                                                   IC-2 (D6): a comparison that cannot be computed or is false as named
HARNESS_OVERRIDES = Path(__file__).resolve().parent / "integrity_overrides.json"


def read_overrides(path) -> tuple:
    """BUILD IC (D4): the override file — {"<sha256 of a sentence's plain words, or its first 12+ characters as the console prints them>":
    {"do": "remove" | "keep", "why": "…"}} — as
    ({key: {do, why}}, None), or ({}, why it is not used). No file → ({}, None). Never raises."""
    p = Path(path)
    if not p.is_file():
        return {}, None
    try:
        d = REG.admit(json.loads(p.read_bytes().decode("utf-8-sig")), "data", p.name)   # BUILD IC-5 (D1): the operator's file, as given
    except Exception as e:  # noqa: BLE001
        return {}, f"{p.name} is not JSON ({type(e).__name__})"
    if not isinstance(d, dict):
        return {}, f"{p.name} is not an object of sentence keys"
    out, bad = {}, 0
    for k, v in d.items():
        if isinstance(k, str) and re.fullmatch(r"[0-9a-f]{12,64}", k) and isinstance(v, dict) and v.get("do") in ("remove", "keep"):   # IC (review b F4): the printed prefix too
            out[k] = {"do": v["do"], "why": str(v.get("why") or "no reason given")[:200]}
        else:
            bad += 1
    return out, (f"{bad} entr(y/ies) not of the form {{key: {{do: remove|keep, why}}}}" if bad else None)


def registry_rows(reg) -> list:
    """BUILD B3 (D1): the appendix's registry table — every entry by kind (strongest first) then id: its span, kind, what it is, its
    source or formula, its assumption and (EXTERNAL) whether it was checked against its primary source. Text escaped; the span is
    the registry's own HTML."""
    rows = []
    for e in sorted(reg.entries.values(), key=lambda e: (REG.KIND_RANK.get(e["kind"], 9), e["id"])):
        rows.append({"id": e["id"], "span": reg.span(e, caveat=False), "kind": e["kind"], "label": e.get("label") or e["id"],
                     "reader": reg.reader_label(e),          # BUILD IC-8 (D12): the reader's label first, the recorded one after it
                     "source": e.get("formula") or e.get("source") or "", "assumption": e.get("assumption") or "",
                     "unit": e.get("unit") or "", "tags": "; ".join(x for x in (e.get("class"), e.get("window"), e.get("cohort")) if x),
                     "verified": e.get("verified_against_primary"), "url": e.get("url") or "", "quote": e.get("quote") or "",
                     "checked": e.get("checked") or ""})
    return rows


def registry_check_rows(reg) -> list:
    """BUILD B3 (D1): the sidecar's checks — a figure computed from the records beside the data's own aggregate, each block; an
    aggregate that differs: sources disagree, both as-of, who speaks (IC-4, D3: no precedence invented)."""
    out = []
    for c in getattr(reg, "checks", []) or []:
        e, a = reg.get(c.get("entry")), reg.get(c.get("against"))
        if not e or not a:
            continue
        dp = reg.disagree_parts(e["id"], a["id"])          # BUILD IC-4 (D3, D7): both as-of, who speaks — no precedence invented
        out.append({"entry": reg.span(e, caveat=False), "entry_label": e.get("label") or e["id"], "formula": e.get("formula") or "",
                    "against": reg.span(a, caveat=False), "against_label": a.get("label") or a["id"], "against_id": a["id"], "equal": bool(c.get("equal")),
                    "against_quote": a.get("quote") or "", "entry_asof": dp["a_asof"], "against_asof": dp["b_asof"], "speaks": dp["speaks"]})
    return out


APPARATUS_REASONS = ("the registry's own listing (not counted: it lists every figure)", "the integrity details (they quote the page)",
                     "the integrity line's own counts",
                     "the chat's offline answers (they repeat the page's own words)")   # IC (review a F4): its copies lose their spans


def integrity_summary(chk: dict, integ: dict, reg) -> dict:
    """BUILD B3 (P0.5): what the checker found on the built page plus what the lock removed — the line, its details and every violation
    (the policy reads them: --coverage fail exits 1).
    BUILD IC (D3): on a page whose text carries its authors the line counts only the numbers in the model's prose — W, of which D come
    from the data, the brief or Python's computation (MEASURED, DERIVED; IC-2: a figure quoted from the organizers' text is MEASURED, a
    comparison Python computed takes its inputs' weakest kind), X from outside sources (EXTERNAL; V of them checked against their primary
    source), A are its own assumptions (MODELED), U have no link — and names any number nobody declared ("not attributed", a defect of
    the page). The details give every other number by author: registry figures Python placed, Python's tables and blocks, the
    organizers' words, Python's own counts, registry labels, not figures by reason, the page's own apparatus."""
    nums = chk.get("numerals") or []
    authored = bool(chk.get("authored"))
    kind_of = lambda i: ((reg.get(i) or {}).get("kind") if reg is not None and i else None)     # noqa: E731
    model_cited = [x for x in nums if x.get("author") == "model" and x["status"] == "cited" and x.get("id")]
    by_kind = {}
    for x in model_cited:
        k = kind_of(x["id"]) or "unknown"
        by_kind[k] = by_kind.get(k, 0) + 1
    unl = [x for x in nums if x.get("author") == "model" and x["status"] == "untraced"]
    una = [x for x in nums if x["status"] == "unattributed"]
    ext = [x for x in model_cited if kind_of(x["id"]) == "EXTERNAL"]
    xv = sum(1 for x in ext if (reg.get(x["id"]) or {}).get("verified_against_primary") == "yes")
    D, X, A = by_kind.get("MEASURED", 0) + by_kind.get("DERIVED", 0), by_kind.get("EXTERNAL", 0), by_kind.get("MODELED", 0)
    # BUILD IC (D1's named exception; reviews a F3, b F1): the workflow diagram's typed figures are the model's numbers with no link — the
    # diagram is Python's drawing (its code exempt, its figures resolved from tokens), so render_html hands them over apart
    diag = [{"text": str(t_), "sec": "the workflow diagram", "context": "typed in a label of the workflow diagram (its source holds no token)"}
            for t_ in chk.get("diagram_typed") or []] if authored else []
    U = len(unl) + by_kind.get("unknown", 0) + len(diag)
    W = D + X + A + U
    und = list(chk.get("undecided") or []) if authored else []          # BUILD IC-3 (D1, D2): listed, never in W, never vanished
    K = len(und)
    lst = list(chk.get("listed") or []) if authored else []            # BUILD IC-4 (D4): typed quantities listed by category, never in U
    L = len(lst)
    tcs = list(chk.get("typed_counts") or []) if authored else []     # BUILD IC-4 (D4): a typed count its list does not hold — flagged
    cc = REG.category_counts(chk, reg, model_only=True) if authored else {}   # BUILD IC-4 (D7): the table's categories, the model's prose
    notes = {}
    if authored:        # IC-4 Phase 7 (review c3): the clause holds what the line holds — the diagram's typed figures with no link — and
        if diag:        # says which of its numbers are the listed counts, so its sums can be told from the line's
            cc["none"] = cc.get("none", 0) + len(diag)
        mine = [x for x in nums if x.get("author") in ("model", None) and x.get("status") == "listed"]
        n_ok = sum(1 for x in mine if x.get("category") == "structural count, checked")
        n_bad = sum(1 for x in mine if x.get("category") == "structural count, does not match")
        if n_ok and cc.get("python"):
            notes["python"] = f"{n_ok:,} of them {'a count' if n_ok == 1 else 'counts'} of a list, checked against it"
        if n_bad and cc.get("disagree"):
            notes["disagree"] = f"{n_bad:,} of them {'a typed count its list does not hold' if n_bad == 1 else 'typed counts their list does not hold'}"
    if authored:
        line = (f"Data integrity: of {W:,} quantit{'y' if W == 1 else 'ies'} in the model's prose, in digits or in words, {D:,} "
                f"come{'s' if D == 1 else ''} from the data, the brief or Python's computation, {X:,} from outside sources, {A:,} "
                f"{'is its own assumption' if A == 1 else 'are its own assumptions'} (labeled), {U:,} ha{'s' if U == 1 else 've'} no link"
                + (f" · {len(una):,} not attributed (a defect of this page)" if una else "")
                + (f". {K:,} more could not be read as a number or a name — listed below" if K else "")
                + (f". {L:,} more {'is' if L == 1 else 'are'} typed and listed by category (a design commitment, a count of a list, a "
                   f"scenario's start, not a quantity)" if L else "")
                + (f"; {len(tcs):,} typed count{' its' if len(tcs) == 1 else 's their'} list does not hold — flagged below" if tcs else "")
                + (". " + REG.category_line(cc, notes) if cc else "") + ".")       # IC-4 Phase 7 (review c21): the line's own stop
    else:
        line = f"Data integrity: {chk['n']:,} of {chk['m']:,} numbers traced to the registry."
    py_spans = {}
    for x in nums:
        if x["status"] == "cited" and x.get("id") and x.get("author") != "model":
            k = kind_of(x["id"]) or "unknown"
            py_spans[k] = py_spans.get(k, 0) + 1
    for c in chk.get("counters") or []:          # IC (review b F6): the hero's counters — registry figures Python placed
        k = kind_of(c["id"]) or "unknown"
        py_spans[k] = py_spans.get(k, 0) + 1
    blocks = {}
    for x in nums:
        if x["status"] == "cited" and x.get("block"):
            blocks[x["block"]] = blocks.get(x["block"], 0) + 1
    count = lambda pred: sum(1 for x in nums if pred(x))       # noqa: E731
    not_fig, appar = {}, {}
    for x in nums:
        if x["status"] != "exempt":
            continue
        d_ = appar if x.get("why") in APPARATUS_REASONS else not_fig
        d_[x.get("why") or "?"] = d_.get(x.get("why") or "?", 0) + 1
    viol = [dict(v) for v in chk["violations"]]
    viol += [{"rule": "+".join(r.get("rules") or ["unknown"]), "where": r.get("field"), "text": "", "context": f"removed: {REG.fit_cap(r['sentence'], 'oracle.integrity.sentence', 200)} — {r['why']}"}
             for r in integ.get("removed") or []]
    viol += [{"rule": "strict", "where": d["what"], "text": "", "context": f"dropped: {d['why']}"} for d in integ.get("dropped") or []]
    # IC (review b F3): every flag is a violation for the policy — the page's own check reads sentences differently (a title with no stop
    # runs into the lead), so a flag the console printed must not depend on the page check finding it again
    viol += [{"rule": f["rule"], "where": f["field"], "text": "", "context": f"flagged and kept: {REG.fit_cap(f['sentence'], 'oracle.integrity.sentence', 200)} — {f['why']}"}
             for f in integ.get("flagged") or []]
    words = {"residual": "a difference of two figures whose result is no registry figure, or whose figures count different people, places or years",
             "unit": "a unit word that contradicts its figure", "unknown": "a token naming no figure", "malformed": "a token naming no figure",
             "strict": "a number in the hero or an evidence card that is no registry figure",
             "quote": "a quote the text it names does not hold (on the page as typed, with no link)",      # IC-2 (D6)
             "declared": "a declared non-figure that reads as a quantity (on the page as typed, with no link)",
             "calc": "a comparison Python could not compute, or false as named",
             "near_miss": "a token's near-miss form (one bracket or curly braces) — not read, its words as typed",
             "stale": "a quantity of Block 1's data that this run's data no longer holds (Block 1's words, with no link)"}   # IC-3 (review b #16)
    flagged_s = [f["sentence"] for f in integ.get("flagged") or []]
    page_flags = [{"rule": words.get(v["rule"], v["rule"]), "where": field_place(v.get("where")), "text": v.get("text") or "",   # BUILD IC-5 (D4)
                   "context": v.get("context") or ""}
                  for v in chk["violations"]
                  if not (v["rule"] == "strict" and v.get("text") and any(str(v["text"]).strip() in s_ for s_ in flagged_s))]   # listed below
    page_flags += [{"rule": "; ".join(words.get(x, x) for x in (r.get("rules") or ["unknown"])), "where": field_place(r.get("field")), "text": "",
                    "context": "a sentence removed"}
                   for r in integ.get("removed") or []]
    page_flags += [{"rule": words["strict"], "where": field_place(d["what"]), "text": "", "context": f"dropped: {d['why']}"} for d in integ.get("dropped") or []]
    page_flags += [{"rule": words.get(f["rule"], f["rule"]), "where": field_place(f["field"]), "text": "",
                    "context": f"flagged and kept — “{REG.fit_cap(f['sentence'], 'oracle.integrity.sentence', 160)}”"} for f in integ.get("flagged") or []]
    untr = ([{"text": x["text"], "sec": x.get("sec"), "context": x.get("context") or ""} for x in unl] + diag if authored else chk["untraced"])
    # BUILD IC-2 (D6): the writer's forms on the page — each quote with its source sentence, each comparison with its formula (by the
    # page's cited ids), each declaration with its kind and sentence, each refused quote or declaration with its reason (by the page's spans)
    cid_ = dict((chk.get("forms") or {}).get("cited") or {})            # by the page's spans: a quote in words has no numeral to count
    for i in (chk.get("forms") or {}).get("diagram_ids") or []:      # a quote or comparison only the diagram cites: listed, 0 spans on the page
        cid_.setdefault(i, 0)
    quoted = [{"shown": e["shown"], "where": e.get("label") or "", "sentence": e.get("quote") or "", "n": cid_[i], "id": i}
              for i in sorted(cid_) if i.startswith("q.") for e in [reg.get(i)] if e]
    computed = [{"shown": e["shown"], "label": e.get("label") or "", "formula": e.get("formula") or "", "kind": e["kind"], "n": cid_[i], "id": i}
                for i in sorted(cid_) if i.startswith("calc.") for e in [reg.get(i)] if e]
    forms_ = chk.get("forms") or {}
    unatt = [{"text": x["text"], "sec": x.get("sec"), "context": x.get("context") or ""} for x in una] if authored else []
    return {"n": (D + X + A) if authored else chk["n"], "m": W if authored else chk["m"], "line": line, "authored": authored,
            "W": W, "D": D, "X": X, "XV": xv, "A": A, "U": U, "N": len(una),
            "flags_page": page_flags,
            "by_kind": [{"kind": k, "n": by_kind[k]} for k in sorted(by_kind, key=lambda k: REG.KIND_RANK.get(k, 9))],
            "py_spans": [{"kind": k, "n": py_spans[k]} for k in sorted(py_spans, key=lambda k: REG.KIND_RANK.get(k, 9))],
            "org_n": count(lambda x: x["status"] == "authored" and x.get("by") == "org"),
            "py_n": count(lambda x: x["status"] == "authored" and x.get("by") == "py"),
            "label_n": count(lambda x: x["status"] == "authored" and x.get("by") == "label"),
            "not_figures": sorted(not_fig.items(), key=lambda kv: -kv[1]), "apparatus": sorted(appar.items(), key=lambda kv: -kv[1]),
            "ids": sorted(chk["cited_ids"].items()), "blocks": sorted(blocks.items() if authored else chk["cited_blocks"].items()),
            "exempt": chk["exempt"],
            "untraced": untr[:80], "untraced_total": len(untr), "unattributed": unatt[:40],   # IC (review b F14): listed apart from U
            # BUILD IC-3 (D2): the reader's version, what each of its rules set aside on this page, the undecided (K) and the vague
            # expressions of the model's prose with their sentences — IC's words list is gone: words are in W
            "K": K, "undecided": und[:80], "vague": list(chk.get("vague") or [])[:40] if authored else [],
            "L": L, "listed": lst[:80], "typed_counts": tcs[:40],          # BUILD IC-4 (D4)
            "vague_total": len(chk.get("vague") or []) if authored else 0, "reader_version": chk.get("reader_version") or "",
            "rules": sorted(((r, n) for r, n in (chk.get("rules") or {}).items() if r not in APPARATUS_REASONS), key=lambda kv: (-kv[1], kv[0])),
            "removed": [{"field": r.get("field"), "sentence": r["sentence"], "why": r["why"], "rules": r.get("rules") or ["unknown"],
                         "key": r.get("key"), **({"tokens": r["tokens"]} if r.get("tokens") else {})}
                        for r in integ.get("removed") or []],      # IC (D4): the key an override names; IC-2: the tokens that failed
            # BUILD IC-8 (d16d; RECORD 2): each field's unit decision on this page (integ["units"]: the link it took) — codes only; the
            # hub takes the same link for the Oracle's texts it shows (landing_harness.load_oracle_checkpoints)
            "units": [{"field": u.get("field"), "ufield": u.get("ufield"), "disposition": u.get("disposition"), "reason": u.get("reason"),
                       "took": u.get("took")} for u in integ.get("units") or [] if isinstance(u, dict)],
            "flagged": [dict(f) for f in integ.get("flagged") or []],
            "dropped": list(integ.get("dropped") or []), "badges": sorted(set(integ.get("badges") or [])),
            "notes": [f"{e.get('field')}: {e.get('why')}" for e in integ.get("events") or [] if e.get("rule") == "note"][:40],
            "quoted": quoted[:60], "computed": computed[:60], "declared": list(forms_.get("declared") or [])[:60],      # IC-2 (D6)
            "refused": list(forms_.get("refused") or [])[:60],
            "forms_n": {"quoted": len(quoted), "computed": len(computed), "declared": len(forms_.get("declared") or []),
                        "refused": len(forms_.get("refused") or [])},
            "problems": list(reg.problems), "violations": viol}


# ─────────────────────────────────────────────────────────────────────
# BUILD B1 — THE PRIORITIZATION MODEL (P1, P2.2) AND THE USE OF FUNDS (P2.3)
# ─────────────────────────────────────────────────────────────────────
FUND_KEYS = ("personnel", "operations", "technology", "outreach", "indirect", "contingency")
FUND_LINE_IDS = {"personnel": "personnel_y1", "operations": "ops_y1", "technology": "tech_y1", "outreach": "outreach_y1",
                 "indirect": "indirect_y1", "contingency": "contingency_y1"}    # BUILD IC-3 (D7): each line's Year-1 amount in the registry
FUND_CAPS = {"owner": 80, "release_cadence": 80, "leading_indicator": 140, "item": 80, "audience": 80}
ALLOC_MAX = 8


def whole_parts(parts, total):
    """Whole-dollar parts of `total`, in proportion to `parts` (largest remainder), summing to `total` exactly."""
    import math
    total = int(round(total))
    s = float(sum(parts))
    if not parts:
        return []
    raw = [p * total / s for p in parts] if s > 0 else [total / len(parts)] * len(parts)
    out = [int(math.floor(x)) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - out[i]))[:total - sum(out)]:
        out[i] += 1
    return out


def funds_prompt_lines(reserve, ids=False) -> str:
    """BUILD B1 (D4): the two Block 1 fields of the use of funds — the model names who and when; Python computes every amount.
    BUILD IC-3 (D7): `ids` (the switch on) — the reserve by its id, `[[reserve]] = value`; the entries' sizes are instruction numbers."""
    caps = FUND_CAPS
    if ids and reserve:
        return (funds_prompt_lines(None).split("\n")[0]
                + "\n- marketing_allocation: how the brief's marketing reserve, [[reserve]] = " + NR.money_short(reserve) + ", is spent — 3 to 6 entries "
                  "{\"item\": \"<what it buys, at most " + str(caps["item"]) + " characters>\", \"amount_usd\": <whole dollars>, \"audience\": "
                  "\"<who it reaches, at most " + str(caps["audience"]) + " characters>\", \"leading_indicator\": \"<the early signal, at most "
                  + str(caps["leading_indicator"]) + " characters>\"}; the amounts sum to exactly the reserve, [[reserve]].")
    lines = ("- fund_lines: who runs each line of the Year-1 use of funds — one entry per line key, exactly these: " + ", ".join(FUND_KEYS)
             + " — each {\"line\": \"<the key>\", \"owner\": \"<the accountable role or partner, at most " + str(caps["owner"])
             + " characters>\", \"release_cadence\": \"<when its money is released, at most " + str(caps["release_cadence"])
             + " characters>\", \"leading_indicator\": \"<the early signal that the line is working, at most " + str(caps["leading_indicator"])
             + " characters>\"}. Python computes every amount from your assumptions; write no amounts in fund_lines.")
    if reserve:
        lines += ("\n- marketing_allocation: how the brief's " + NR.money_short(reserve) + " marketing reserve is spent — 3 to 6 entries "
                  "{\"item\": \"<what it buys, at most " + str(caps["item"]) + " characters>\", \"amount_usd\": <whole dollars>, \"audience\": "
                  "\"<who it reaches, at most " + str(caps["audience"]) + " characters>\", \"leading_indicator\": \"<the early signal, at most "
                  + str(caps["leading_indicator"]) + " characters>\"}; the amounts sum to exactly " + f"{reserve:,}" + ".")
    else:
        lines += "\n- marketing_allocation: [] — the brief reserves no marketing budget."
    return lines


def block1_base(doc):
    """B2: Block 1's payloads for the engine at Block 2 (the pipeline's snapshot, else this harness's Block 1 base), so a field or a
    record the curveball removed is told from a typo in the sidecar; None when neither is there."""
    cid = ((doc or {}).get("challenge") or {}).get("id")
    return REG.admit(NR.block1_payloads(Path(_HERE).parent / "landing-harness", cid, CHECKPOINT_DIR), "data", "block1 payloads") if cid else None   # BUILD IC-5 (D1)


def engine_prompt_block(brief_doc, raw) -> str:
    """BUILD B1: the published prioritization table in the master prompt when the challenge's sidecar runs on this data, so the
    narrative and the brief's answers describe the table the page computes. '' otherwise."""
    cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
    spec, _why = _load_spec(cpath) if cpath else (None, None)
    with ENG.names_mode(raw=not NR.ic8_prompt()):      # BUILD IC-8 (P4.4, p44s): IC-7's texts with the IC-8 switch off, through any
        txt = (ENG.prompt_text(spec, brief_doc, raw, os.environ.get("CURVEBALL_TEXT", ""),  # switch it builds on (I10)
                               block1_base(brief_doc) if is_block2() else None,
                               ids=ic3_prompt())     # BUILD IC-3 (D7): the table's counts and points as ids (switch on)
               if spec is not None else "")                                                   # B2: Block 2's reading, as the page's
    return f"\n<prioritization_model>\n{txt}\n</prioritization_model>" if txt else ""


def _fund_text(v, n):        # BUILD IC-8 (D1): was _cut_clean — fitted on the shown text by the one owner
    s = NR.clean_text(v if isinstance(v, str) else "")
    s = re.sub(r"\s*\[CHALLENGE_DATA[^\]]*\]", "", s).strip()     # review: a source tag is not text a reader can use here
    return REG.fit_cap(s, "oracle.fund", n) if s else ""


def funds_values(fund_lines, alloc):
    """BUILD B1 (review #1): fund_lines and marketing_allocation in the one form the schema takes — a list of objects — whatever the reply
    wrote: an object keyed by line becomes that list, null an empty list, an entry that is not an object is named and left out. Never
    raises; the analysis never fails on these two fields."""
    notes = []

    def lst(v, name, keyed=None):
        if v is None:
            return []
        if isinstance(v, dict) and keyed:
            v = [dict(x, **{keyed: k}) if isinstance(x, dict) else x for k, x in v.items()]
        if not isinstance(v, list):
            notes.append(f"{name}: not a list — not used")
            return []
        out = []
        for i, x in enumerate(v):
            if isinstance(x, dict):
                out.append(x)
            else:
                notes.append(f"{name}[{i}]: not an object — not used")
        return out
    return lst(fund_lines, "fund_lines", "line"), lst(alloc, "marketing_allocation"), notes


def fund_words(entries):
    """fund_lines (a list of {line, owner, release_cadence, leading_indicator}, or an object line → {…}) → (key → words, console notes).
    A key that is not a line of the use of funds is named and not used; a line given twice keeps the first."""
    words, notes = {}, []
    if isinstance(entries, dict):
        entries = [dict(v, line=k) for k, v in entries.items() if isinstance(v, dict)]
    if not isinstance(entries, list):
        return words, (["fund_lines: not a list — no owners shown"] if entries else [])
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            notes.append(f"fund_lines[{i}]: not an object — not used")
            continue
        k = str(e.get("line", "")).strip().lower()
        if k not in FUND_KEYS:
            notes.append(f"fund_lines[{i}]: {REG.fit_cap(str(e.get('line')), 'oracle.fund', 40)!r} is not a line of the use of funds — not used")
            continue
        if k in words:
            notes.append(f"fund_lines[{i}]: {k} given twice — the first kept")
            continue
        words[k] = {f: _fund_text(e.get(f), FUND_CAPS[f]) for f in ("owner", "release_cadence", "leading_indicator")}
    return words, notes


def marketing_alloc(entries, reserve):
    """marketing_allocation → the reserve's items, checked against the reserve (a difference is a named row, never rescaled), or None."""
    if not reserve:
        return None, ([f"marketing_allocation: the brief reserves no marketing budget — {len(entries)} item(s) not shown"]
                      if isinstance(entries, list) and entries else [])
    items, notes = [], []
    for i, e in enumerate(entries if isinstance(entries, list) else []):
        amt = money_value(e.get("amount_usd")) if isinstance(e, dict) else None
        if not isinstance(e, dict) or amt is None or amt <= 0 or not _fund_text(e.get("item"), FUND_CAPS["item"]):
            notes.append(f"marketing_allocation[{i}]: needs an item and a whole-dollar amount above 0 — not used")
            continue
        if len(items) == ALLOC_MAX:
            notes.append(f"marketing_allocation: more than {ALLOC_MAX} items — item {i + 1} on not used")
            break
        items.append({"item": _fund_text(e.get("item"), FUND_CAPS["item"]), "amount_n": amt, "amount": f"${amt:,}",
                      "audience": _fund_text(e.get("audience"), FUND_CAPS["audience"]) or "not given",
                      "indicator": _fund_text(e.get("leading_indicator"), FUND_CAPS["leading_indicator"]) or "not given"})
    if not isinstance(entries, list) and entries:
        notes.append("marketing_allocation: not a list — not used")
    s = sum(x["amount_n"] for x in items)
    diff = reserve - s
    return {"rows": items, "sum_n": s, "sum": f"${s:,}", "reserve_n": reserve, "reserve": f"${reserve:,}", "diff_n": diff,
            "diff": f"${abs(diff):,}", "diff_label": "Not yet allocated" if diff > 0 else "Over the reserve by" if diff < 0 else "",
            "status": ("allocated in full" if diff == 0 and items else "no allocation given" if not items
                       else f"{'under' if diff > 0 else 'over'} by ${abs(diff):,} — shown as it was given, never rescaled")}, notes


def _esc(s):
    return _html.escape(str(s or ""), quote=True)


# ── BUILD IC (D1): text escaped where it becomes HTML, marked with its author (naipc_registry's marks; the checker reads them) ──
def _esc_org(s):
    """The organizers' words — the brief, the announcement, an alert's or a record's text, a name in the data."""
    return REG.mark("org", _esc(s))


def _esc_model(s):
    """The model's words that reach the page without the lock (a name it chose, a role it titled)."""
    return REG.mark("model", _esc(REG.out_text(s, "plain")))      # BUILD IC-5 (D2 c): the model's tags as words


MODEL_LABELS = ("model.", "sroi.", "staff.", "kpi.", "tripwire.")   # entries whose label holds the model's words (measured 11/10 by the
                                                                    # inventory: a proposal's words, an outcome, a role, an indicator)


def label_author(e, field="label") -> str:
    """Who wrote a registry entry's text (D1). A label: the model's when the entry was registered from its words — a proposal
    ([[model:<n>|<what>]] → model.<n>), an SROI outcome, a staff role, a KPI indicator, the tripwire (Block 1's b1. copies too) — else
    Python's or the sidecar's ("label"). A source: the organizers' when it quotes the brief ("the brief: “…”"), the model's when it gives
    its basis (a KPI target's), else "label"."""
    e = e or {}
    i = str(e.get("id") or "").removeprefix("b1.")
    if field == "source":
        src = str(e.get("source") or "")
        return "org" if src.startswith("the brief:") else ("model" if i.startswith("kpi.") else "label")
    if "label_model" in e:                      # IC-3 (ledger step 24): the registration names the model's words in the label ("" none)
        return "model" if e.get("label_model") else "label"
    return "model" if i.startswith(MODEL_LABELS) else "label"     # a registry written before IC-3: the id's prefix decides, as before


def _esc_label(s, e=None, field="label"):
    """A registry entry's label (or source, formula, assumption) — by its author: label_author(e, field).
    IC-3 (ledger step 24): a label that holds the model's words beside Python's ("<the model's category>: value of one outcome") marks
    each part by its author — Python's words were the model's, and this build counts a number word in them ("one")."""
    lm = (e or {}).get("label_model") if field == "label" else None
    if field == "label":                      # BUILD IC-8 (D12): a label in a reader's words (a source or a formula stays: a source note)
        s = REG.page_words(s, marks=True)        # BUILD IC-8 (P4.4, p44n2): a record's name in a label is the organizers' words
    t = _esc(REG.out_text(s, "plain"))        # BUILD IC-5 (D2 c): an entry's words as words (its model part converted alike, so found)
    if lm and label_author(e, field) == "model":
        lt = _esc(REG.out_text(lm, "plain"))
        k = t.find(lt)
        if k >= 0:
            return "".join(REG.mark(a, x) for a, x in (("label", t[:k]), ("model", lt), ("label", t[k + len(lt):])) if x)   # a label's own words
    return REG.mark(label_author(e, field), t)


# BUILD IC-5 (D2 a): the author filters, finalize and their problems moved into the registry (REG.page_env); these names stay for
# the tests that import them from this module
AuthorshipError = REG.AuthorshipError
_finalize = REG.finalize_page
AUTHOR_FILTERS = REG.AUTHOR_FILTERS
_MARK_PROBLEMS = REG._PAGE["problems"]
_tpl_line = REG._tpl_line


GRID_STEP = 0.20                  # D12: each MODELED input ±20%
GRID_FIGS = (("cpb_y1", "Year-1 cost per {s}"), ("surplus_y1", "Year-1 surplus"), ("sroi", "SROI"), ("coi_3yr", "3-year cost of inaction"))
GRID_SHOWN = 15                   # the table's rows; the rest that move a figure are counted, those that move none named in one line


def _grid_inputs(ma: dict, reg, fin: dict) -> list:
    """D12: every MODELED input the financial stage reads, with where it lives — the analysis's inputs (FIN_INPUTS), each staff role's
    salary, count and benefits, each used SROI outcome's rate, duration and (when the model's own) unit value, and Python's constants.
    [(registry id, where)]: where = ("ma", key) | ("staff", index in staff_roles, key) | ("sroi", index in sroi_outcomes, key) |
    ("const", key). An input whose value is 0 cannot move by a share of itself and is left out."""
    out = []
    for k, _f, _w in FIN_INPUTS:
        e = reg.get(k)
        if k == "discount_rate":       # BUILD IC-4 (D6): Python's method constant, not the analysis's input — its rows are the pair's
            continue
        if e and e["kind"] == "MODELED" and e["value"]:
            out.append((k, ("ma", k)))
    roles = [j for j, x in enumerate(ma.get("staff_roles") or []) if isinstance(x, dict)]
    for i, j in enumerate(roles):
        for k in FIN_ROLE_DEFAULTS:
            e = reg.get(f"staff.{i + 1}.{k}")
            if e and e["kind"] == "MODELED" and e["value"]:
                out.append((e["id"], ("staff", j, k)))
    for r in fin.get("sroi_rows") or []:
        if not isinstance(r, dict) or "i" not in r:
            continue
        for key, fld in (("rate", "rate"), ("duration", "duration_years"), ("unit_value", "unit_value")):
            e = reg.get(f"sroi.{r['n']}.{key}")
            if e and e["kind"] == "MODELED" and e["value"]:
                out.append((e["id"], ("sroi", r["i"], fld)))
    for k in FIN_CONSTANTS:
        e = reg.get(f"const.{k}")
        if e and e["value"]:
            out.append((e["id"], ("const", k)))
    return out


def _fin_at(ma: dict, changes) -> dict:
    """The financial stage itself (_fin_quiet) on a copy of the analysis with each (where, value) of `changes` set; a constant is set in
    FIN_CONSTANTS for the call and restored after it."""
    m, consts = copy.deepcopy(ma), {}
    for where, v in changes:
        if where[0] == "ma":
            m[where[1]] = v
        elif where[0] == "staff":
            m["staff_roles"][where[1]][where[2]] = v
        elif where[0] == "sroi":
            m["sroi_outcomes"][where[1]][where[2]] = v
        else:
            consts[where[1]] = v
    old = {k: FIN_CONSTANTS[k] for k in consts}
    try:
        for k, v in consts.items():
            FIN_CONSTANTS[k] = (v,) + tuple(old[k][1:])
        return _fin_quiet(m)
    finally:
        FIN_CONSTANTS.update(old)


def _grid_figs(fin: dict) -> dict:
    v = {it["id"]: it["value"] for it in fin.get("registry_items") or [] if isinstance(it, dict) and "id" in it}
    return {k: v.get(k) for k, _l in GRID_FIGS}


def _fmt_in(v, fmt):
    """An input's shown form: a count that is not whole (0.8 of a role) in plain numbers, never rounded to a whole one."""
    return REG.fmt_value(v, "num" if fmt == "count" and not float(v).is_integer() else (fmt or "num"))


def _grid_whole(e) -> bool:
    """A whole-number input: one of the analysis's whole-number keys, or an outcome's years (the stage rounds a duration to whole years)."""
    return e["id"] in FIN_INT_KEYS or e["id"].endswith(".duration")


def _grid_value(e, f):
    """An input's varied value: its registry value × f — a share kept within 0 and 1. B3 (Phase 4, review d F2): a whole-number input
    is passed whole, and when a fifth of it rounds back to itself it moves to the next whole value instead (a 2-year outcome → 1 and 3,
    never 2.4 that the stage reads as 2); an outcome's years never go below 1."""
    v = e["value"] * f
    if e.get("fmt") == "pct":
        v = min(max(v, 0.0), 1.0)
    if _grid_whole(e):
        b = int(round(e["value"]))
        v = int(round(v))
        if v == b and f != 1.0:
            v = b + (1 if f > 1 else -1)
        if e["id"].endswith(".duration"):
            v = max(v, 1)
    return v


def _grid_step(e) -> str:
    """The row's step as the page says it: ±20%, or ±1 for a whole-number input that a fifth would not move."""
    if _grid_whole(e) and int(round(e["value"] * (1 + GRID_STEP))) == int(round(e["value"])):
        return "±1 year" if e["id"].endswith(".duration") else "±1"
    return "±20%"


def sensitivity_grid(results: dict, reg, F: dict) -> dict:
    """BUILD B3 (D12, P2.12): each MODELED input ±20% → the Year-1 cost per person, the Year-1 surplus, the SROI and the 3-year cost of
    inaction, each computed by the financial stage itself on a copy (_fin_at → _fin_quiet); rows sorted by swing (the largest relative
    move of any of the four, high − low over the base); a 5 × 5 grid of the two largest drivers on the figure the largest moves most.
    Every figure in the registry's form for its id. Never raises: a failure leaves the section out, named in the console."""
    ma, fin = results.get("master_analysis") or {}, results.get("financial_model") or {}
    if not ma or not fin.get("registry_items"):
        return {"show": False, "why": "no financial model this run"}
    try:
        base = _grid_figs(fin)
        fmts = {k: (reg.get(k) or {}).get("fmt") or "num" for k, _l in GRID_FIGS}
        labels = {k: l.format(s=F["unit_singular"]) for k, l in GRID_FIGS}
        figs_on = [k for k, _l in GRID_FIGS if base.get(k) is not None]
        rows, still = [], []
        for iid, where in _grid_inputs(ma, reg, fin):
            e = reg.get(iid)
            lo_v, hi_v = _grid_value(e, 1 - GRID_STEP), _grid_value(e, 1 + GRID_STEP)
            lo, hi = _grid_figs(_fin_at(ma, [(where, lo_v)])), _grid_figs(_fin_at(ma, [(where, hi_v)]))
            moves = {k: (abs(hi[k] - lo[k]) / abs(base[k]) if base[k] else None) for k in figs_on if lo.get(k) is not None and hi.get(k) is not None}
            swing = max((m for m in moves.values() if m is not None), default=0.0)
            if swing < 1e-9:
                still.append(_esc_label(e.get("label") or iid, e))
                continue
            top = max((k for k in moves if moves[k] is not None), key=lambda k: moves[k])
            rows.append({"id": iid, "where": ":".join(str(x) for x in where), "where_t": where, "label": _esc_label(e.get("label") or iid, e), "swing": swing, "top": top,
                         "v_low": lo_v, "v_high": hi_v, "low_shown": _esc(_fmt_in(lo_v, e.get("fmt"))),
                         "high_shown": _esc(_fmt_in(hi_v, e.get("fmt"))), "base": reg.span(e),
                         "cells": [{"fig": k, "low": lo.get(k), "high": hi.get(k),
                                    "low_shown": _esc(REG.fmt_value(lo[k], fmts[k])) if lo.get(k) is not None else "—",
                                    "high_shown": _esc(REG.fmt_value(hi[k], fmts[k])) if hi.get(k) is not None else "—"} for k in figs_on],
                         "step": _grid_step(e),
                         "swing_html": ('<span data-prov-block="the sensitivity grid (Python\'s financial model on each varied input)">'
                                        f"{_grid_step(e)} moves {_esc(labels[top])} from {_esc(REG.fmt_value(lo[top], fmts[top]))} to "
                                        f"{_esc(REG.fmt_value(hi[top], fmts[top]))}</span>")})
        rows.sort(key=lambda r: (-r["swing"], r["id"]))
        for r in rows:
            r["swing_pct"] = f"{r['swing'] * 100:.0f}%"
        grid = None
        steps = (0.8, 0.9, 1.0, 1.1, 1.2)
        axis = [r for r in rows if len({_grid_value(reg.get(r["id"]), f_) for f_ in steps}) == 5]   # five distinct values on its axis
        if len(axis) >= 2:
            d1, d2 = axis[0], axis[1]
            fig = d1["top"]
            e1, e2 = reg.get(d1["id"]), reg.get(d2["id"])
            w1, w2 = d1["where_t"], d2["where_t"]
            cells = []
            for f2 in steps:
                line = []
                for f1 in steps:
                    vx, vy = _grid_value(e1, f1), _grid_value(e2, f2)
                    v = _grid_figs(_fin_at(ma, [(w1, vx), (w2, vy)])).get(fig)
                    line.append({"v": v, "vx": vx, "vy": vy, "shown": _esc(REG.fmt_value(v, fmts[fig])) if v is not None else "—",
                                 "base": f1 == 1.0 and f2 == 1.0})
                cells.append({"f": f2, "label": _esc(_fmt_in(_grid_value(e2, f2), e2.get("fmt"))), "cells": line})
            grid = {"fig": fig, "fig_label": _esc(labels[fig]), "x": d1, "y": d2,
                    "x_steps": [_esc(_fmt_in(_grid_value(e1, f1), e1.get("fmt"))) for f1 in steps], "rows": cells}
        return {"show": bool(rows), "why": "no MODELED input moves a headline figure" if not rows else None, "rows": rows[:GRID_SHOWN],
                "all_rows": rows, "more": max(0, len(rows) - GRID_SHOWN), "still": still, "grid": grid,
                "figs": [{"id": k, "label": _esc(labels[k]), "base": reg.span(k)} for k in figs_on]}
    except Exception as ex:  # noqa: BLE001
        NR.say(f"    sensitivity grid: not built — {type(ex).__name__}: {str(ex)[:120]}")
        return {"show": False, "why": f"the grid could not be computed ({type(ex).__name__})"}


RUBRIC_MAP = {   # BUILD B3 (D11): a brief's scoring dimension → the catalogue sections of this page that answer it ("curveball": Block 2's section)
    "Problem Understanding": ("summary", "evidence", "bottlenecks", "youth-handoffs", "data-sources"),
    "Solution Quality": ("operations", "prioritization", "financials", "use-of-funds", "sroi", "plan", "kpi-framework", "renewal-gates"),
    "Presentation & Polish": ("kpis", "integrity", "simulator", "sensitivity-grid", "registry"),
    "Adaptability": ("curveball", "sensitivity", "sensitivity-grid", "risks", "honesty"),
    "AI Mastery": ("ai-process", "methodology", "integrity", "registry"),
}


def rubric_section(doc, present, curveball_shown) -> dict:
    """BUILD B3 (D11, P2.11): the contract's scoring dimensions in its order, each with its descriptor, linked to the sections RUBRIC_MAP
    names that this page shows (`present`; the curveball section when this is Block 2); a dimension the table lacks → "not mapped"."""
    sc = (doc or {}).get("scoring") if isinstance((doc or {}).get("scoring"), dict) else {}
    dims = [d for d in sc.get("dimensions") or [] if isinstance(d, str) and d.strip()]
    if not dims:
        return {"show": False, "why": "the brief states no scoring dimensions"}
    sec = {n: (a, t) for n, a, t, *_ in NR.SECTIONS["oracle"]}
    rows = []
    for d in dims:
        names = RUBRIC_MAP.get(d)
        links = []
        for n in names or ():
            if n == "curveball":
                if curveball_shown:
                    links.append({"name": n, "anchor": "curveball", "label": "What changed at the curveball"})
            elif n in sec and n in present:
                links.append({"name": n, "anchor": sec[n][0], "label": sec[n][1]})
        rows.append({"dimension": d, "descriptor": str((sc.get("descriptors") or {}).get(d) or ""), "mapped": names is not None, "links": links})
    return {"show": True, "rows": rows, "scale": str(sc.get("scale") or "")}


HEADLINE_SECTIONS = ("bottlenecks", "youth-handoffs", "kpi-framework", "renewal-gates", "sensitivity-grid", "honesty")   # B3 (D10): the call's titles


HONESTY_FIELDS = ("summary_title", "bluf_text", "decision_text", "crisis_title", "crisis_body", "crisis_human", "crisis_sources",
                  "solution_title", "solution_body", "solution_population", "cost_of_inaction")   # D8: the summary's fields (01)
HONESTY_WEAK_SHOWN = 12        # D8: the weakest assumptions listed by name; every one is in the registry (the appendix)


def _ind_html(reg, ind, fix=None, field="") -> str:
    """D8: an indicator as the page shows it — a KPI framework name as written (its tokens through the lock when `fix` is given: B3
    Phase 4, review c F3); a registry id or token as its label and its figure."""
    m = REG.TOKEN_RE.fullmatch(ind or "")
    e = reg.lookup(m.group(1))[0] if m else reg.get(ind)
    return f"{_esc_label(reg.plain(e.get('label') or e['id']), e)} (now {reg.span(e)})" if e else (fix(ind, field) if fix else _esc_model(ind))   # IC (review a F7)


def honesty_section(reg, integ: dict, dview: dict, fix, doc, present=None, grid=None) -> dict:
    """BUILD B3 (D8, P2.8): "What could prove us wrong" — the strongest evidence (the MEASURED and DERIVED figures the evidence cards and
    the summary cite, with their sources), the weakest assumptions (the MODELED inputs — ranked by the sensitivity grid's swing when it
    ran, else unranked), the thesis with what would kill it and its tripwire, one judge question per scoring dimension in the brief's own
    words (each linked to its section when the section is on this page), the scenario triggers and every EXTERNAL anchor with its
    verified flag. Every text part escaped; every figure a registry span."""
    cited = integ.get("cited") or {}
    order = []
    for f, ids in cited.items():
        if f.startswith(("evidence_cards[", "comparison_rows[")) or f in HONESTY_FIELDS:
            order += [i for i in ids if i not in order]
    strong = []
    for i in order:
        e = reg.get(i)
        if e and e["kind"] in ("MEASURED", "DERIVED") and not e["id"].startswith("b1."):
            strong.append({"span": reg.span(e), "label": _esc_label(e.get("label"), e), "source": _esc_label(e.get("formula") or e.get("source") or "", e, "source" if not e.get("formula") else "label"), "kind": e["kind"]})
    ranked = bool(grid and grid.get("rows"))
    if ranked:
        rows = grid["rows"]
        weak = [{"span": reg.span(r["id"]), "label": _esc_label((reg.get(r["id"]) or {}).get("label") or r["id"], reg.get(r["id"])), "swing": r["swing_html"],
                 "basis": _esc_label((reg.get(r["id"]) or {}).get("source"), reg.get(r["id"]), "source")} for r in rows[:HONESTY_WEAK_SHOWN]]
        more = max(0, len(rows) - HONESTY_WEAK_SHOWN)
    else:
        ids = [k for k, *_ in FIN_INPUTS] + sorted(i for i in reg.entries if i.startswith(("sroi.", "const.", "staff.")))
        es = [e for e in (reg.get(i) for i in ids) if e and e["kind"] == "MODELED"]
        weak = [{"span": reg.span(e), "label": _esc_label(e["label"], e), "swing": "", "basis": _esc_label(e.get("source"), e, "source")} for e in es[:HONESTY_WEAK_SHOWN]]
        more = max(0, len(es) - HONESTY_WEAK_SHOWN)
    th = dview.get("thesis")
    thesis = None
    if th:
        thesis = {"claim": fix(th["claim"], "honesty.claim"), "kill": fix(th["kill"], "honesty.kill"), "indicator": _ind_html(reg, th["indicator"], fix, "honesty.thesis"),
                  "threshold": reg.span("tripwire.threshold"), "day": reg.span("tripwire.day"), "direction": th.get("direction", "below"), "response": fix(th["response"], "honesty.response")}
    sc = (doc or {}).get("scoring") if isinstance((doc or {}).get("scoring"), dict) else {}
    anchors = {n: a for n, a, *_ in NR.SECTIONS["oracle"]}
    titles = {n: t for n, _a, t, *_ in NR.SECTIONS["oracle"]}
    qa_by = {q["dimension"]: q for q in dview.get("judge_qa") or []}
    qa = []
    for d in sc.get("dimensions") or []:
        q = qa_by.get(d)
        on_page = bool(q) and (present is None or q["section"] in present)
        qa.append({"dimension": _esc_org(d), "descriptor": _esc_org((sc.get("descriptors") or {}).get(d)),
                   "question": fix(q["question"], "honesty.question") if q else "",
                   "answer": fix(q["answer"], "honesty.answer") if q else "",
                   "anchor": anchors.get(q["section"]) if on_page else None, "section": _esc(q["section"]) if q else "",
                   "section_title": _esc(titles.get(q["section"], q["section"])) if q else "", "absent": bool(q) and not on_page,
                   "id": q.get("id") if q else None})
    triggers = [{"scenario": fix(t["scenario"], "honesty.trigger"), "indicator": _ind_html(reg, t["indicator"], fix, "honesty.trigger"),
                 "threshold": fix(t["threshold"], "honesty.trigger"), "response": fix(t["response"], "honesty.trigger"), "id": t.get("id")}
                for t in dview.get("triggers") or []]
    ext = [{"span": reg.span(e), "label": _esc(e["label"]), "source": _esc(e.get("source")), "verified": e.get("verified_against_primary") == "yes",
            "url": _esc(e.get("url")), "quote": _esc(e.get("quote")), "checked": _esc(e.get("checked"))}
           for e in sorted(reg.entries.values(), key=lambda e: e["id"]) if e["kind"] == "EXTERNAL" and not e["id"].startswith("b1.")]
    return {"show": True, "strong": strong, "weak": weak, "more": more, "ranked": ranked, "thesis": thesis, "qa": qa, "triggers": triggers,
            "ext": ext, "words": _written(dview, "thesis"), "dims": bool(sc.get("dimensions"))}     # BUILD IC-6 (D13)


EW_NONE = "no early warning written for this risk"     # BUILD IC-4 (D1): coverage is said, never a bare dash


def early_warning_cells(dview: dict, risks: list, reg, fix) -> list:
    """BUILD B3 (D8): the risk register's early-warning column — the indicator (a KPI's name, or a registry figure as its label and
    value), the threshold and the cadence. BUILD IC-4 (D1): by the risk's id (dview["risk_ids"], this run's register in its order); a
    risk with no valid warning says so (EW_NONE)."""
    by = {}
    for w in dview.get("early_warnings") or []:
        by.setdefault(w["risk"], []).append(w)
    ids = list(dview.get("risk_ids") or [])
    out = []
    for i, _r in enumerate(risks or []):
        rid = ids[i] if i < len(ids) else None
        out.append(NR.join_clauses(f"{_ind_html(reg, w['indicator'], fix, 'risks.early_warning')} — {fix(w['threshold'], 'risks.early_warning')}"
                             + (f" ({fix(w['cadence'], 'risks.early_warning')})" if w["cadence"] else "") for w in by.get(rid) or []) or _esc(EW_NONE))
    return out


def _quoted(words, what, author="org"):
    """The records' or the engine's own words inside Python-built HTML: escaped, a block of their own for the integrity check.
    BUILD IC (D1): marked with their author — the organizers' (an alert, a record, the announcement) unless the caller says Python's."""
    return f'<span data-prov-block="{_esc(what)}">{REG.mark(author, _esc(words))}</span>' if words else ""


def incident_section(reg, results: dict, cb_rec: dict, quiet=False) -> dict:
    """BUILD B3 (D9, P2.9): incident mode at the curveball — every Block 1 figure of the registry against this run's (the two registries;
    the data's figures included, each with the change refs that moved it: the changed records it is computed from, or every change of an
    endpoint it is computed from whole; each stale aggregate beside the figure recomputed from records); a first-hours playbook built by
    Python from the closures, the engine's reroute rows and the change list's alerts (every step names its record ids; the order of hours
    is a proposal); an actions log labeled ILLUSTRATIVE, stamped from the announcement's time. Every text part escaped."""
    if not cb_rec:
        return {"show": False}
    changes, _w = _changes_from_env()
    eps = (changes or {}).get("endpoints") or {}
    changed = {ep: {str(r.get("ref")).lower(): str(r.get("ref")) for k in ("added", "removed", "changed") for r in (e.get(k) or [])}
               for ep, e in eps.items() if isinstance(e, dict) and e.get("status") in ("changed", "new endpoint")}
    changed = {ep: refs for ep, refs in changed.items() if refs}
    # the fields each changed record changed (None: the record was added or removed — every field of it counts)
    fields_of = {ep: {str(r.get("ref")).lower(): (set((r.get("fields") or {}).keys()) if k == "changed" else None)
                      for k in ("added", "removed", "changed") for r in (e.get(k) or [])} for ep, e in eps.items() if isinstance(e, dict)}
    add_rem = [str(r.get("ref")) for e in eps.values() if isinstance(e, dict) for k in ("added", "removed") for r in (e.get(k) or [])]
    raw = (results.get("data_loading") or {}).get("raw") or {}
    rec_ids = {ep: {str(r.get("id")) for r in NR.records(p) if isinstance(r, dict) and r.get("id") is not None} for ep, p in raw.items()}
    applied = [c for c in cb_rec.get("assumption_changes") or [] if isinstance(c, dict)]
    # B3 (Phase 4, review d F3): each financial figure credited to the change that moved it — Block 1's analysis replayed through the
    # kept changes one at a time with the financial stage itself (as moved_figures does for the headline figures)
    step_refs = {}
    try:
        b1ma = (load_block1_base(((_brief_contract() or {}).get("challenge") or {}).get("id"))[0] or {}).get("ma")
        if b1ma and applied:
            cur = copy.deepcopy(b1ma)
            prev = {it["id"]: it["value"] for it in _fin_quiet(cur).get("registry_items") or [] if isinstance(it, dict)}
            for ch in applied:
                cur[ch["field"]] = ch["new"]
                now_ = {it["id"]: it["value"] for it in _fin_quiet(cur).get("registry_items") or [] if isinstance(it, dict)}
                for k_, v_ in now_.items():
                    if prev.get(k_) != v_:
                        step_refs.setdefault(k_, [])
                        step_refs[k_] += [str(r) for r in ch.get("because") or [] if str(r) not in step_refs[k_]]
                prev = now_
    except Exception as ex:  # noqa: BLE001 — the attribution falls back to every kept change's refs, named in the console
        if not quiet:           # BUILD IC-8 (d15e): said once — the section is made again after each render
            NR.say(f"    incident mode: the step-by-step attribution was not computed ({type(ex).__name__})")
        step_refs = {}

    def _named(ref, text):
        return re.search(r"(?<![\w-])" + re.escape(ref) + r"(?![\w-])", text or "") is not None

    def moved_by(i, e):
        """The change refs that moved figure i: the changed records it is computed from — a record it reads by id, else the records its
        formula names, else every record of the endpoint — kept only when a field it reads changed (or the record came or went)."""
        if i == "data.record_count":
            return "the records added or removed: " + ", ".join(add_rem) if add_rem else "the data at the curveball"
        srcs = [str(x) for x in (e.get("inputs") or [])] if e else []
        srcs = srcs or [i[4:] if i.startswith("sum:") else i]
        formula = str((e or {}).get("formula") or "")
        refs = []
        for x in srcs:
            ep, _, rest = x.partition(".")
            ch = changed.get(ep)
            if not ch:
                continue
            parts = rest.split(".")
            rid = parts[0]
            if rid.lower() in ch:                       # one record, read by id (its field, when the id names one)
                fl = fields_of.get(ep, {}).get(rid.lower())
                if fl is None or len(parts) < 2 or parts[1] in fl:
                    refs.append(ch[rid.lower()])
            elif rid not in rec_ids.get(ep, ()):        # the endpoint whole: the records its formula names, else all; a field it reads changed
                named_any = any(_named(r, formula) for r in rec_ids.get(ep, set()) | set(ch.values()))
                for low, r in ch.items():
                    fl = fields_of.get(ep, {}).get(low)
                    if (not named_any or _named(r, formula)) and (fl is None or not formula or any(_named(f, formula) for f in fl)
                                                                   or (rid and rid in fl)):
                        refs.append(r)
        refs = list(dict.fromkeys(refs))
        if refs:
            return "the changed records: " + ", ".join(refs)
        mine = [c for c in applied if c.get("field") == i]
        if mine:
            return "the update's assumption change, because " + ", ".join(dict.fromkeys(str(r) for c in mine for r in c.get("because") or []))
        if e and e["kind"] in ("MEASURED", "DERIVED"):
            return "the data at the curveball"
        if step_refs.get(i):
            return "the financial model, recomputed after the update's assumption change (" + ", ".join(step_refs[i]) + ")"
        if step_refs:       # the replay ran and no kept change moved this figure: the budget or this run's own inputs did
            return "the financial model, recomputed on this run's inputs"
        because = list(dict.fromkeys(str(r) for c in applied for r in c.get("because") or []))
        return ("the financial model, recomputed after the update's assumption changes (" + ", ".join(because) + ")" if because
                else "the financial model, recomputed on this run's inputs")

    checks = [c for c in getattr(reg, "checks", []) or [] if isinstance(c, dict) and not c.get("equal")
              and reg.get(c.get("against")) and reg.get(c.get("entry"))]

    def stale_html(c):
        a = reg.get(c["against"])
        dp = reg.disagree_parts(c["entry"], a["id"])       # BUILD IC-4 (D3, D7): both as-of, who speaks — no precedence invented
        return {"against": reg.span(a, caveat=False), "against_id": _esc(a["id"]), "label": _esc_label(a.get("label") or a["id"], a),
                "quote": _quoted(a.get("quote"), "the record's own words"), "entry": reg.span(c["entry"], caveat=False), "entry_id": _esc(c["entry"]),
                "entry_asof": _esc(dp["a_asof"]), "against_asof": _esc(dp["b_asof"]), "speaks": _esc(dp["speaks"]), "authority": dp["authority"]}
    rows, has_b1 = [], any(k.startswith("b1.") for k in reg.entries)
    for k in sorted(k for k in list(reg.entries) if k.startswith("b1.")):
        b1, i = reg.get(k), k[3:]
        e = reg.get(i)
        if e is not None and abs(e["value"] - b1["value"]) < 1e-9:
            continue
        st_ = [stale_html(c) for c in checks if c["entry"] == i]      # BUILD IC-4 (D7): the row states the caveat — no marker in its cell
        rows.append({"id": _esc(i), "label": _esc_label((e or {}).get("label") or str(b1.get("label") or i).removeprefix("Block 1: "), e or b1),
                     "b1": reg.span(b1), "b2": reg.span(e, caveat=not st_) if e else
                     ("did not load this run" if i.split(".")[0] in (getattr(reg, "unavailable", None) or set()) else "not in this run's data"),
                     "why": _esc(moved_by(i, e) if e else "its endpoint did not load this run (a source did not load)"      # IC-4 Phase 7
                                 if i.split(".")[0] in (getattr(reg, "unavailable", None) or set())                          # (review b9)
                                 else "its record or field is gone from this run's data"),
                     "stale": st_,
                     "rank": 0 if (e or b1)["kind"] in ("MEASURED", "DERIVED") else 1})
    for i, m_ in sorted((getattr(reg, "b1_models", None) or {}).items()):     # IC-4 Phase 7 (review a9, D1): the writer's assumptions
        e = reg.get(i)                                                         # — the same label with a new number, or no longer cited
        if e is not None and e.get("value") == m_.get("value"):
            continue
        rows.append({"id": _esc(i), "label": _esc(str(m_.get("label") or i)), "b1": _esc(str(m_.get("shown") or "")),
                     "b2": reg.span(e) if e is not None else "no longer cited",
                     "why": _esc("the writer's assumption, changed at the curveball: the same label, a new number" if e is not None else
                                 "the writer's assumption, no longer cited: its label changed or the update dropped it"),
                     "stale": [], "rank": 1})
    if has_b1:   # B3 (Phase 4, review d F8): a named figure of the data new at Block 2 (Block 1's registry had none) — "—" → its value
        for i in sorted(k for k in reg.entries if "." not in k and f"b1.{k}" not in reg.entries):
            e = reg.get(i)
            if e["kind"] not in ("MEASURED", "DERIVED"):
                continue
            rows.append({"id": _esc(i), "label": _esc_label(e.get("label") or i, e), "b1": "— (not in Block 1)",
                         "b2": reg.span(e, caveat=not any(c["entry"] == i for c in checks)),      # BUILD IC-4 (D7): as above
                         "why": _esc(moved_by(i, e)), "stale": [stale_html(c) for c in checks if c["entry"] == i], "rank": 0})
    rows.sort(key=lambda r: (r["rank"], r["id"]))
    shown_stale = {c["entry"] for c in checks if any(r["id"] == _esc(c["entry"]) for r in rows)}
    stale = [stale_html(c) for c in checks if c["entry"] not in shown_stale]
    # the playbook: closures (now), reroutes (next), the other alerts and the stale aggregates (within 24 hours)
    prio = results.get("prioritization") or {}
    all_refs = {r for refs in changed.values() for r in refs}
    alerts = [(ep, aid, txt) for ep, aid, txt in ENG._alert_texts(raw) if aid.lower() in all_refs]
    groups = {}
    for c in prio.get("closures") or []:
        if not isinstance(c, dict):
            continue
        g = groups.setdefault(str(c.get("name")), {"name": str(c.get("name")), "ids": [], "why": [], "alerts": [], "sets": []})
        g["ids"].append(f"{c.get('id')} ({c.get('set')})")
        g["sets"].append(str(c.get("set")))           # BUILD IC-8 (D12): the log names its sets — the name is given beside them
        w_ = (str(c.get("why") or ""), str(c.get("segment") or ""))
        if w_ not in g["why"]:
            g["why"].append(w_)
    for _ep, aid, txt in alerts:     # an alert naming exactly one closed name is that closure's; one naming several is listed on its own
        hits = [g for g in groups.values() if ENG.closures_by_text({0: g["name"]}, txt)]
        if len(hits) == 1:
            hits[0]["alerts"].append(aid)
    now = [{"text": f"Stop referrals to {_esc_org(g['name'])}: closed by "
                    + NR.join_clauses(_esc(w) + (f": “{_quoted(s, 'the announcement or record’s own words')}”" if s else "") for w, s in g["why"])
                    + (f" — and the alert {_esc(', '.join(g['alerts']))}" if g["alerts"] else ""),
            "ids": _esc(", ".join(g["ids"] + g["alerts"]))} for g in groups.values()]
    nxt = []
    for r in prio.get("reroute") or []:
        left = NR.join_clauses(f"{_esc_org(x['name'])} ({_esc(x['id'])}) — {_reason_block(x['why'])}" for x in r.get("left") or [])   # IC-4: by its authors; one stop (D8)
        rep = ", ".join(f"{_esc_org(x['name'])} ({_esc(x['id'])})" for x in r.get("replaced_by") or [])
        nxt.append({"text": f"Reroute {_esc_org(r.get('name'))} ({_esc(r.get('set'))}): out of the top options — {left}" + (f"; now in them: {rep}" if rep else ""),
                    "ids": _esc(", ".join([x["id"] for x in r.get("left") or []] + [x["id"] for x in r.get("replaced_by") or []]))})
    used = {a for g in groups.values() for a in g["alerts"]}
    day = [{"text": f"Act on the alert {_esc(aid)} with its owner: " + _quoted(NR.cut(" ".join(txt.split()), 240), "the alert's own words"), "ids": _esc(aid)}   # IC (D7): never mid-word
           for _ep, aid, txt in alerts if aid not in used]
    day += [{"text": f"Sources disagree on {s_['label']}: {s_['against']} ({s_['against_asof']}) against the records' {s_['entry']} "
                     f"({s_['entry_asof']}) — " + (s_["speaks"] if s_["authority"] else "ask the publisher which holds"), "ids": s_["against_id"]}
            for s_ in [stale_html(c) for c in checks]]        # BUILD IC-4 (D3, D7): no precedence invented
    t0 = str(cb_rec.get("announced_at") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t0)
    base_min = int(m.group(1)) * 60 + int(m.group(2)) if m else None

    def stamp(k):
        return f"{(base_min + k) // 60 % 24:02d}:{(base_min + k) % 60:02d}" if base_min is not None else f"T+{k} min"
    log, k = [{"t": stamp(0), "text": "The announcement arrives" + (f": “{_quoted(NR.cut(NR.clean_text(cb_rec.get('announcement')), 120), 'the announcement')}”"
                                                                   if cb_rec.get("announcement") else "")}], 0
    if changed:
        k += 2
        log.append({"t": stamp(k), "text": "The change list is compared with Block 1's snapshot — "
                    + "; ".join(f"{_esc(ep)}: {_esc(', '.join(list(refs.values())[:8]))}" + (" …" if len(refs) > 8 else "") for ep, refs in changed.items())})
    for g in groups.values():
        k += 1
        log.append({"t": stamp(k), "text": f"{_esc_org(g['name'])} is removed from every ranking ({_esc(', '.join(g['sets']))})"})   # IC (D1): a record's name is the organizers'
    for r in prio.get("reroute") or []:
        k += 1
        log.append({"t": stamp(k), "text": f"{_esc_org(r.get('name'))} rerouted ({_esc(r.get('set'))}): " + _esc(", ".join(x["id"] for x in r.get("left") or [])) + " out"
                    + (", " + _esc(", ".join(x["id"] for x in r.get("replaced_by") or [])) + " in" if r.get("replaced_by") else "")})
    for row in [r for r in rows if r["rank"] == 0][:6]:
        k += 1
        log.append({"t": stamp(k), "text": f"{row['label']}: {row['b1']} → {row['b2']} ({row['why']})"})
    return {"show": True, "rows": rows, "stale": stale, "has_b1": has_b1, "b1_note": _esc(getattr(reg, "block1_note", None) or ""),
            "now": now, "next": nxt, "day": day, "log": log,
            # B3 (Phase 4, review d F6): the empty state says what was read — the prioritization model's closures (its option sets, when
            # the challenge has an engine sidecar) — never that the data, alerts or announcement hold none
            "has_engine": isinstance(prio, dict) and prio.get("status") == "ok"}


def _kpi_basis(k: dict) -> str:
    """BUILD IC-5 (D13): a KPI's basis as the page prints it — the model's sentences, then Python's canonical tags for its checked fields
    (basis_sources → "[CHALLENGE_DATA: a, b]", confidence → "[ESTIMATE: medium]"), which the page's one output path turns into its badges
    and words. Nothing the model typed becomes a badge here; without the fields (the switch off, an older reply) the basis as written.
    D13c: the KPI table (kpi_framework_section) and its target's registry entry (deliverables_view) read the one checked record through
    this function — the entry's source is what the registry table, each span's title, the chat context and the facts print."""
    b = str(k.get("basis") or "")
    tags = ([f"[CHALLENGE_DATA: {', '.join(k['basis_sources'])}]"] if k.get("basis_sources") else []) + \
           ([f"[ESTIMATE: {k['confidence']}]"] if k.get("confidence") else [])
    return " ".join([b] + tags).strip() if tags else b


def _basis_tags(text):
    """BUILD IC-5 (D13b): a KPI basis's data and estimate tags, read by the registry's recognizer (admit's: REG._TAG_FORMS, the kind by
    REG._tag_kind) and taken out of its prose → (the prose without them, [(kind, the tag's words, the tag as written)]). A tag that
    filled a pair of parentheses takes them with it; a benchmark's or a source's words stay (admit made them words). Not text → ("", [])."""
    if not isinstance(text, str) or not text:
        return "", []
    spans = sorted((m.start(), m.end(), REG._tag_kind(m.group("w")), m.group("c") or "", m.group(0))
                   for rx in REG._TAG_FORMS for m in rx.finditer(text) if REG._tag_kind(m.group("w")) in ("data", "estimate"))
    kept, found, last = [], [], 0
    for a, b, kind, c, raw in spans:
        if a < last:                        # a doubled [[…]] holds a single one: read once
            continue
        kept.append(text[last:a])
        found.append((kind, c.strip().lstrip(":：,-–—").strip(), raw))
        last = b
    kept.append(text[last:])
    t = "".join(kept)
    for e in ("( )", "()"):
        while e in t:
            t = t.replace(e, "")
    t = " ".join(t.replace("(, ", "(").split())
    for p in (".", ",", ";", ":", ")"):
        t = t.replace(" " + p, p)
    return t, found


def kpi_framework_section(view: dict, reg, F: dict, funds: dict, fix) -> dict:
    """BUILD B3 (D4, P2.4): the pilot KPI framework — the call's indicators (leading and lagging, each with its baseline, target, day,
    cadence, owner and source; the owner is the use-of-funds line's) and the pilot's spend: each Year-1 line × pilot days ÷ 365
    (MODELED: even spending — each line's release cadence says when its money goes out), with the total. Every figure a registry span."""
    owners = {ln["key"]: ln for ln in (funds.get("lines") or [])} if funds.get("show") else {}
    rows = []
    for k in view.get("kpis") or []:
        ln = owners.get(k["fund_line"]) or {}
        rows.append({"indicator": fix(k["indicator"], "kpis.indicator"), "type": k["type"],
                     "baseline": reg.span(k["baseline_id"]) if k["baseline_id"] else 'new — counted from Day <span data-num-exempt="a label: the pilot\'s first day">1</span>',
                     "target": reg.span(k["target_id"]) if k["target_id"] else "", "day": reg.span(k["day_id"]) if k.get("day_id") else "",
                     "cadence": fix(k["cadence"], "kpis.cadence"),
                     "owner": ln.get("owner_h") or _esc("not given"),
                     "line": _esc(ln.get("label") or k["fund_line"]), "source": _esc(k["source"]), "basis": fix(_kpi_basis(k), "kpis.basis"),
                     "id": k.get("id")})
    spend, total = [], None
    if F["pilot_days"] and funds.get("show"):
        amts = []
        for ln in funds["lines"]:
            if ln["key"] == "contingency":
                continue        # B3 (Phase 4, review c F7): the Year-1 surplus (or the rounding) is not spent — the pilot's spend leaves it out
            v = round(ln["amount_n"] * F["pilot_days"] / 365)
            e = reg.add(f"pilot_spend.{ln['key']}", v, "MODELED", fmt="money", unit="USD", label=f"the pilot's share: {ln['label']}",
                        formula=f"{ln['label']} (Year 1) × the pilot's days ÷ 365", source="Python: even spending; each line's release cadence says when its money goes out")
            amts.append(v)
            spend.append({"label": _esc(ln["label"]), "amount": reg.span(e) if e else _esc(REG.money_short(v)), "cadence": ln.get("cadence_h") or _esc(ln.get("cadence"))})
        e = reg.add("pilot_spend.total", sum(amts), "MODELED", fmt="money", unit="USD", label="the pilot's spend, every line but the unallocated contingency",
                    formula="Σ the lines' pilot shares", source="Python: even spending")
        total = reg.span(e) if e else ""
    by_id = {k.get("id"): k for k in view.get("kpis") or []}
    unread = [fix(by_id[i]["indicator"], "kpis.indicator") for i in view.get("kpi_unread") or [] if i in by_id]   # IC-4 (D1)
    return {"show": bool(rows) or bool(spend) or not _written(view, "kpis"), "rows": rows, "missing": view.get("kpi_missing") or [],
            "unread": unread, "words": _written(view, "kpis"),         # BUILD IC-6 (D13): a cut reply that never wrote the KPIs
            "status": _status_words(view.get("status")), "spend": spend, "spend_total": total,
            "pilot": reg.span("pilot_days") if F["pilot_days"] else "", "no_pilot": not F["pilot_days"]}


def bottleneck_section(b: dict, reg) -> dict:
    """BUILD B3 (D5, P2.6): the bottleneck dashboard from naipc_engine.bottlenecks — per set its records' listed, in-use and free capacity
    (Python, from the records), closures, the gates that limit who a free unit serves, who of the brief's people can use it now; the
    totals as DERIVED registry figures (<set>.listed, .in_use, .free); the jam points by record id; the chart's data (JSON, '</' escaped)."""
    if not isinstance(b, dict) or b.get("status") != "ok":
        return {"show": False, "why": (b or {}).get("status") or "not built"}
    sets, chart = [], []
    for s in b["sets"]:
        f, t = s["fields"], s["totals"]
        ids = {key: (reg.span(f"{s['id']}.{key}") if reg.get(f"{s['id']}.{key}") else _esc(f"{t[key]:,}")) for key in ("listed", "in_use", "free")
               if not (key == "in_use" and len(f) == 1)}      # the totals are registered with the data (build_registry)
        named = [e for e in reg.entries.values() if e["kind"] == "DERIVED" and s["endpoint"] in (e.get("inputs") or []) and "." not in e["id"]]
        rows = [{"id": _esc(r["id"]), "name": _esc_org(r["name"]), "listed": "" if r["listed"] is None else f"{r['listed']:,}",
                 "in_use": "" if r["in_use"] is None else f"{r['in_use']:,}", "free": "no cap" if r["free"] is None else f"{r['free']:,}",
                 "over": r["over"], "no_cap": _esc_label(r["no_cap"]), "closed": _esc(r["closed"]), "limits": _esc("; ".join(r["limits"]) or "none"),
                 "reach": _esc(", ".join(r["reach"]) or "none of them")} for r in s["rows"]]
        sets.append({"id": s["id"], "label": _esc(s["label"]), "endpoint": _esc(s["endpoint"]), "one_field": s["one_field"],
                     "fields": _esc(" − ".join(s["fields"])), "rows": rows, "totals": ids, "no_cap": t["no_cap"],
                     "closed_free": t.get("closed_free") or 0,         # IC-4 Phase 7 (review b11): of the free total, at sites closed now
                     "named": [{"span": reg.span(e), "label": _esc_label(e["label"], e)} for e in named], "people": _esc_org(", ".join(s.get("people") or []))})
        chart.append({"id": s["id"], "labels": [str(r["name"]) for r in s["rows"]],      # BUILD IC-8 (D12): the bars by their records' names
                      "in_use": [r["in_use"] if r["in_use"] is not None else 0 for r in s["rows"]],
                      "free": [r["free"] if r["free"] is not None else 0 for r in s["rows"]], "one_field": s["one_field"]})
    jams = [{"set": _esc(j["set"]), "id": _esc(j["id"]), "name": _esc_org(j["name"]), "kind": j["kind"], "why": _esc(j["why"])} for j in b["jams"]]   # IC (D1): names
    return {"show": bool(sets), "sets": sets, "jams": jams, "problems": [_esc(p) for p in b.get("problems") or []],
            "chart": chart}      # BUILD IC-5 (D2 e): the object; the template prints it | tojson | built (was a JSON string, only "</" escaped)


def youth_section(ap: dict, view: dict, reg, fix) -> dict:
    """BUILD B3 (D6, P2.5): the age protocol from naipc_engine.age_protocol (each cliff age: per set, the records serving it, their free
    capacity, which begin or end there) and the call's failure modes (where, what fails, its evidence as registry spans, the remedy, the
    fund line and the indicator). Absent without an age-gated set."""
    if not isinstance(ap, dict) or ap.get("status") != "ok":
        return {"show": False, "why": (ap or {}).get("status") or "not built"}
    sets = ap["sets"]
    ages = []
    for r in ap["ages"]:
        cells = []
        for s in sets:
            c = r["sets"].get(s["id"]) or {}
            cells.append({"records": c.get("records", 0), "free": c.get("free", 0), "no_cap": c.get("no_cap", 0),
                          "closed": c.get("closed", 0), "closed_free": c.get("closed_free", 0),     # IC-4 Phase 7 (review b11)
                          "begin": _esc(", ".join(c.get("begin") or [])), "end": _esc(", ".join(c.get("end") or []))})
        ages.append({"age": r["age"], "cells": cells})
    modes = [{"where": fix(m["where"], "failure_modes.where"), "what": fix(m["what"], "failure_modes.what"), "evidence": " · ".join(reg.span(i) for i in m["evidence"]),
              "remedy": fix(m["remedy"], "failure_modes.remedy"), "line": _esc(m["fund_line"]), "indicator": fix(m["indicator"], "failure_modes.indicator"),
              "id": m.get("id")}
             for m in view.get("failure_modes") or []]
    return {"show": True, "sets": [{"id": s["id"], "label": _esc(s["label"]), "field": _esc(s["field"])} for s in sets], "ages": ages,
            "modes": modes, "words": _written(view, "failure_modes"), "problems": [_esc(p) for p in ap.get("problems") or []]}   # IC-6 (D13)


def renewal_gates_section(view: dict, reg, endpoints, fix=None) -> dict:
    """BUILD B3 (D7, P2.7): "at least X of the Y indicators at target by Day N" (X and N the call's, Y the KPI framework's count), each
    indicator with its evidence tier, set by Python: C (estimated) when its baseline is an assumption (MODELED); otherwise A when it is
    re-read from data the city already publishes (an endpoint of this run), B when it is counted in the program's own records."""
    kp = view.get("kpis") or []
    if not kp:
        return {"show": not _written(view, "kpis"), "rows": [], "gate": None, "words": _written(view, "kpis")}     # BUILD IC-6 (D13)
    eps = {str(e) for e in endpoints or []}
    rows = []
    for k in kp:
        be = reg.get(k["baseline_id"]) if k["baseline_id"] else None
        tier = "C" if be and be["kind"] == "MODELED" else ("A" if k["source"] in eps else "B")
        rows.append({"indicator": fix(k["indicator"], "renewal.indicator") if fix else _esc(k["indicator"]), "type": k["type"],
                     "target": reg.span(k["target_id"]) if k["target_id"] else "",
                     "day": reg.span(k["day_id"]) if k.get("day_id") else "", "tier": tier, "source": _esc(k["source"])})
    rn = view.get("renewal")
    gate = None
    if rn:
        gate = {"x": reg.span("renewal.x"), "y": len(kp), "day": reg.span("renewal.day")}
    return {"show": True, "rows": rows, "gate": gate, "words": _written(view, "kpis")}      # BUILD IC-6 (D13)


def use_of_funds_section(uf, ma, F):
    """BUILD B1 (D4, P2.3; the review's #2, #5, #6): the financial model's Year 1 lines — they sum to the budget by construction when the
    costs fit, the last line what they leave over; when the costs exceed the budget no negative line is shown, the total says by how
    much — each line's owner, release cadence and leading indicator from the model's fund_lines as written ("not given" when missing),
    the reserve's allocation, and a note when the personnel cap scaled the roles. The notes are console lines (a judge reads the page)."""
    if not isinstance(uf, dict) or not uf.get("lines") or uf.get("contract") is None:
        return {"show": False, "notes": []}
    words, notes = fund_words(ma.get("fund_lines"))
    reserve, rate, c = uf.get("reserve"), uf.get("rate_pct"), uf["contract"]
    src, costs = uf.get("contract_source"), uf.get("total_exp") or 0
    phrase = {"brief": f"the brief's ${c:,}", "override": f"the ${c:,} budget set for this run", "proposed": f"our proposed ${c:,} budget",
              "cost_plan": f"our proposed ${c:,} budget (the Year-1 cost plan, ${costs:,.0f}, rounded up to $10,000)"}.get(src, f"the ${c:,} budget")
    labels = {"personnel": "Personnel", "operations": "Operations and transport", "technology": "Technology",
              "outreach": "Outreach — the brief's marketing reserve" if reserve else "Outreach", "indirect": f"Indirect ({rate}%)",
              "contingency": "Rounding up to the proposed budget" if src == "cost_plan" else "Contingency — the Year 1 surplus, not yet allocated"}
    lines, missing = [], 0
    for ln in uf["lines"]:
        k, amt = ln["key"], ln["amount"]
        if k == "contingency" and amt < 0:
            continue                                 # review #5: costs over the budget — the total row says by how much
        w = words.get(k) or {}
        missing += sum(1 for f in ("owner", "release_cadence", "leading_indicator") if not w.get(f))
        lines.append({"key": k, "label": labels.get(k, k), "amount_n": amt, "amount": f"${amt:,.0f}",
                      "owner": w.get("owner") or "not given", "cadence": w.get("release_cadence") or "not given",
                      "indicator": w.get("leading_indicator") or "not given"})
    roles = [{"role": REG.fit_text(NR.clean_text(r["role"]), "oracle.use_of_funds.role"), "count": r["count"], "amount_n": r["amount"], "amount": f"${r['amount']:,.0f}"}
             for r in uf.get("roles") or []]
    alloc, n2 = marketing_alloc(ma.get("marketing_allocation"), reserve)
    total = sum(x["amount_n"] for x in lines)
    over = total > c
    scale = uf.get("scale")
    scale_note = (f"The financial model caps personnel at 48% of the budget{' minus the marketing reserve' if reserve else ''}: each role above "
                  f"is scaled to {round(scale * 100)}% of its cost (salary × count, plus benefits)." if _num_ok(scale) and scale < 0.9995 else "")
    period = (f"The Base Case P&L's Year 1, funded by {phrase}: its costs line by line"
              + ("; the last line is its Year 1 surplus." if not over else ".")
              + (f" The brief's pilot runs {F['pilot_days']} days; the pilot's share of each line, at even spending, is in the KPI framework "
                 "(each line's release cadence says when its money goes out)." if F.get("pilot_days") else ""))
    return {"show": True, "lines": lines, "roles": roles, "total_n": total, "total": f"${total:,.0f}", "contract_n": c, "contract": f"${c:,}",
            "budget_label": NR.BUDGET_SOURCES.get(src, "Budget"), "budget_phrase": phrase, "over": over,
            "over_text": f"over {phrase} by ${total - c:,.0f}" if over else "", "scale_note": scale_note,
            "reserve": f"${reserve:,}" if reserve else "", "alloc": alloc, "period": period, "not_given": missing, "notes": notes + n2}


def _num_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def prio_attrs(attrs, shown):
    """BUILD B1 (review #3): the coverage attributes of the prioritization block with data-user kept only for the people whose worked case
    the page shows (the map names them; a preset dropped by a problem must not count as covered)."""
    def keep(m):
        ids = [u for u in m.group(1).split() if u in shown]
        return f' data-user="{" ".join(ids)}"' if ids else ""
    return re.sub(r' data-user="([^"]*)"', keep, attrs or "")


AS_OF_WORDS = {"the run's clock": "the time of this run", "NAIPC_AS_OF": "a time set for this run"}


_QUOTED_REASON = re.compile(r'^(closed by [^:"]+: )"(.*?)"((?:; .*)?)$', re.S)


def _reason_block(text):
    """BUILD IC-4 (D3, D8): an engine reason inside the prioritization model's block (the incident section's reroute rows) — Python's frame
    Python's, a closure's quoted sentence the organizers' (reason_html), as the prioritization section's rows print it."""
    return f'<span data-prov-block="{_esc("the prioritization model (Python, from the data)")}">{reason_html(text)}</span>' if text else ""


def reason_html(text):
    """BUILD IC-4 (D3, D8): an engine reason as the page's HTML — a closure's reason quotes the organizers' sentence ('closed by the
    announcement: "<the sentence>"; the <endpoint> records still read 1 free (…)'): Python's frame is Python's, the sentence
    the organizers' (their words, their numbers), its own end stop left out so the line the reason sits in keeps one stop. Any other reason
    is all Python's."""
    m = _QUOTED_REASON.match(str(text or ""))
    if not m:
        return REG.mark("py", _esc(text))
    seg = m.group(2).strip().rstrip(".;")
    return REG.mark("py", _esc(m.group(1) + "\u201c")) + _quoted(seg, "the announcement's sentence") + REG.mark("py", _esc("\u201d" + m.group(3)))


def _with_reason_html(rows, key):
    return [dict(x, **{key + "_h": reason_html(x.get(key))}) if isinstance(x, dict) else x for x in rows or []]


def prioritization_section(res):
    """BUILD B1 (D4, P2.2; the review's #4, #7): the page's view of the engine's result — the published tables, each person's worked cases
    (the top options with their breakdown, trace and the record's check text; the options to confirm; the exclusions, a closure in its
    own words), the fairness rows (each adds up to the set's records), the fewest-options line among the people a set serves, the
    closures and (Block 2) the reroute rows. Shown only when the engine ran (D1 as built: problems go to the console, never page text)."""
    if not isinstance(res, dict) or res.get("status") != "ok" or not res.get("sets"):
        return {"show": False}
    n = res.get("top_n") or 3
    label = {t["id"]: t["label"] for t in res["sets"]}
    people = []
    for p in res.get("presets") or []:
        people.append(dict(p, cases=[dict(c, label=label.get(c["set"], c["set"]), top=c["ranked"][:n], more=max(0, len(c["ranked"]) - n),
                                          excluded=_with_reason_html(c.get("excluded"), "detail"))       # IC-4: each reason by its authors
                                     for c in p["cases"]]))
    fair = [dict(r, set_label=label.get(r["set"], r["set"])) for r in res.get("fairness") or []]
    worst = []
    for sid, lab in label.items():
        # the people this set serves at all — BUILD IC-4 (D3): one of its records fits the person whatever its state (the engine's
        # fits); "not every record is not for this person" read a closed record that serves no one of them as serving them
        rows = [r for r in fair if r["set"] == sid and (r["fits"] > 0 if "fits" in r else r["other"] < r["records"])]
        if len(rows) >= 2:
            m = min(r["eligible"] for r in rows)
            worst.append({"set": lab, "eligible": m, "names": [r["name"] for r in rows if r["eligible"] == m]})
    closed = {}
    for c in res.get("closures") or []:
        closed.setdefault((c["name"], c["why"], c["segment"]), []).append(label.get(c["set"], c["set"]))
    return {"show": True, "as_of": res.get("as_of"), "as_of_source": AS_OF_WORDS.get(res.get("as_of_source"), res.get("as_of_source")),
            "sets": res["sets"], "people": people, "fairness": fair, "worst": worst, "top_n": n, "is_block2": is_block2(),
            "closures": [{"name": k[0], "why": k[1], "segment": k[2], "sets": v} for k, v in closed.items()],
            "reroute": [dict(r, set_label=label.get(r["set"], r["set"]), left=_with_reason_html(r.get("left"), "why"))
                        for r in res.get("reroute") or []],
            "reroute_status": res.get("reroute_status"), "attrs": ""}


def engine_chat_lines(prio, funds) -> list:
    """BUILD B1: the chat context's two blocks — what the page shows of the prioritization model and the use of funds."""
    out = []
    if prio and prio.get("show"):
        out += ["", f"PRIORITIZATION MODEL (Python ranks the options from a published table; no model call ranks anything; evaluated at "
                    f"{prio['as_of']}, {prio['as_of_source']}):"]
        for t in prio["sets"]:
            out.append(f"  {t['label']} ({t['endpoint']}): gates — " + "; ".join(g["text"] for g in t["gates"]) + "; factors — "
                       + "; ".join(f"{f['label']} {f['max']} points ({f['formula']})" for f in t["factors"]))
        for p in prio["people"]:
            for c in p["cases"]:
                top = ", ".join(f"{k + 1}. {x['name']} ({x['score']} points)" for k, x in enumerate(c["top"])) or "no option fits now"
                out.append(f"  {p['who']} — {c['label']}: {top} | eligible {c['counts']['eligible']} of {c['counts']['records']}")
        for c in prio["closures"]:
            out.append(f"  Closed: {c['name']} ({', '.join(c['sets'])}) — by {c['why']}" + (f": \"{c['segment']}\"" if c["segment"] else ""))
        for r in prio["reroute"]:
            out.append(f"  Rerouted at the curveball — {r['name']}, {r['set_label']}: " + "; ".join(f"{x['name']} left the top {prio['top_n']} ({x['why']})" for x in r["left"]))
    if funds and funds.get("show"):
        out += ["", f"USE OF FUNDS (the financial model's Year 1; {funds['budget_label']} {funds['contract']}):"]
        for ln in funds["lines"]:
            out.append(f"  {ln['label']}: {ln['amount']} — owner: {ln['owner']}; released: {ln['cadence']}; leading indicator: {ln['indicator']}")
        a = funds.get("alloc")
        if a:
            out.append(f"  The marketing reserve {a['reserve']}: " + ("; ".join(f"{x['item']} {x['amount']} ({x['audience']})" for x in a["rows"])
                                                                       or "no allocation given") + f" — {a['status']}")
    return out


@stage("prioritization", depends_on=["data_loading"])
def stage_prioritization(deps: dict) -> dict:
    """BUILD B1 (P1; D1–D6): the prioritization model — the challenge's engine sidecar on this run's data, Python only (no model call).
    In Block 2 also Block 1's saved data (no announcement, the same evaluation time) for the reroute rows. Never raises."""
    raw = (deps.get("data_loading") or {}).get("raw") or {}
    doc = _brief_contract()
    base_result = None
    if is_block2():        # BUILD B1 (review): the reroute compares with what Block 1's page showed — its saved ranking, never Block 1's
        meta, _w = REG.admit(NR.read_json(CHECKPOINT_DIR / NR.BLOCK1_DIR / NR.BLOCK1_META), "saved", NR.BLOCK1_META)    # data re-ranked at this run's clock
        chal = ((doc or {}).get("challenge") or {}).get("id")
        ok = isinstance(meta, dict) and (not chal or meta.get("challenge") == chal)
        base_result = (_cp_data(CHECKPOINT_DIR / NR.BLOCK1_DIR / "prioritization.json") if ok else None) or {}
    res = ENG.for_oracle(raw, doc, os.environ.get("NAIPC_REQUIREMENTS", "").strip(), announcement=os.environ.get("CURVEBALL_TEXT", ""),
                         base_result=base_result, base=block1_base(doc) if is_block2() else None)   # BUILD IC-5 (D1 e): its sidecar read admitted (REG.admit_readers)
    for line in res.get("console") or []:
        NR.say(f"    {line}")
    return res


# ─────────────────────────────────────────────────────────────────────
# BUILD B3 (D3, P2.1): THE DELIVERABLES CALL — one more call after Python's figures exist; every figure a token; checked in Python
# ─────────────────────────────────────────────────────────────────────
DELIVERABLES_TAG = "<deliverables_request>"     # the prompt's own block: the mock Messages API recognises the call by it
# BUILD IC-6 (D1): the call's limit and effort are its profile's, "oracle/deliverables" (IC-5: 12,000 at medium)
# BUILD IC-4 (D9): the call starts only when it can finish before the Oracle's end time (NR.start_check; DELIVERABLES_BY_S's 620 s held a
# 120 s stalled read that IC-3 D5's end time made obsolete — the call is ended at the end time whatever its reads do)
DLV_CAPS = {"indicator": 120, "basis": 200, "cadence": 60, "source": 60, "where": 80, "what": 400, "remedy": 400, "claim": 400, "kill": 300,
            "response": 300, "question": 300, "answer": 700, "threshold": 120, "scenario": 200, "headline": 160}
PROGRAM_RECORDS = "program records"


_DLV_FIGURES = (   # BUILD IC-3 (D10): the deliverables' first lines point to the one set of rules with the switch on; IC-2's with it off
    "Every quantity in your text follows <number_rules>\nbelow (a target, a threshold and a day are numbers in their own JSON fields, not tokens)."
    if ic3_prompt() else
    "Every figure is a token — [[<id>]] from the registry below, or\n[[model:<number>|<what it is>]] for a number of your own" + _IC2_RULE8
    + " — never digits you type (a target, a threshold and a day are numbers in their own\nfields, not tokens).")


def deliverables_prompt(deps: dict) -> str:
    """D3's request: the brief's items, the registry (Python's figures now with their values), the engine's result, the use-of-funds lines
    with their keys and owners, the risks with their index and the contract's scoring — and the JSON it asks for. Event-neutral."""
    ma, fin, prio = deps.get("master_analysis") or {}, deps.get("financial_model") or {}, deps.get("prioritization") or {}
    raw = (deps.get("data_loading") or {}).get("raw") or {}
    doc = _brief_contract()
    F = NR.facts(doc)
    reg = build_registry(deps)
    items = "\n".join(f"- {i}: {NR.clean_text(t)}" for i, t in NR.brief_items(doc, "oracle")) if doc else "- (no brief)"
    on = ic3_prompt()          # BUILD IC-3 (D7): each amount and count Python prints here by its id, `[[id]] = value`
    figs = "\n".join(f"[[{i}]] = {reg.shown(i)} — {w.format(p=F['unit_plural'], s=F['unit_singular'])}" for i, w in computed_ids() if reg.get(i))
    uf = fin.get("use_of_funds") or {}
    words, _n = fund_words(ma.get("fund_lines"))
    on5 = NR.ic5_prompt()      # BUILD IC-5 (D13): the model's earlier texts in words (out_text's "request": tags words, tokens kept)
    said = (lambda x: REG.out_text(x, "request")) if on5 else (lambda x: x)      # noqa: E731
    fund_id = lambda k: FUND_LINE_IDS.get(k) if on and reg.get(FUND_LINE_IDS.get(k)) else None      # noqa: E731
    funds = "\n".join(f"- {ln['key']}: " + (f"[[{fund_id(ln['key'])}]] = {reg.shown(fund_id(ln['key']))}" if fund_id(ln["key"])
                                            else REG.money_short(ln["amount"]))
                      + f" (Year 1) — owner: {said((words.get(ln['key']) or {}).get('owner')) or 'not given'}" for ln in uf.get("lines") or [])
    risks = "\n".join(f"[{i}] {said(NR.clean_text(r.get('risk')))} (likelihood {said(NR.clean_text(r.get('likelihood')))}, impact "
                      f"{said(NR.clean_text(r.get('impact')))})" for i, r in enumerate(ma.get("risks") or []) if isinstance(r, dict))
    # BUILD IC-5 (D13): a KPI's sources and confidence are fields; its basis is sentences — the switch off: IC-4's form, byte for byte
    kpi_basis = (f'"target_basis": "<why, in plain sentences, at most {DLV_CAPS["basis"]} characters>", "basis_sources": ["<an endpoint of '
                 f'this run>", ...], "confidence": "high" or "medium" or "low"' if on5
                 else f'"target_basis": "<why, at most {DLV_CAPS["basis"]} characters>"')
    fields_block = ("\nIn this reply, write sources in basis_sources and confidence in confidence; the prose fields hold sentences only."
                    if on5 else "")
    sc = (doc or {}).get("scoring") if isinstance((doc or {}).get("scoring"), dict) else {}
    dims = "\n".join(f"- {d}: {NR.clean_text((sc.get('descriptors') or {}).get(d))}" for d in sc.get("dimensions") or [])
    eng = []
    for t in prio.get("sets") or []:
        eng.append(f"- {t.get('label')} ({t.get('endpoint')}, its records: [[{t.get('endpoint')}.count]] = {t.get('records')})" if on and reg.get(f"{t.get('endpoint')}.count")
                   else f"- {t.get('label')} ({t.get('endpoint')}, {t.get('records')} records)")
    for c in prio.get("closures") or []:
        eng.append(f"- closed: {c.get('name')} ({c.get('set')}, {c.get('id')}) — {c.get('why')}")
    for p in prio.get("presets") or []:
        for c in p.get("cases") or []:
            top = ", ".join(f"{x.get('name')} ({x.get('id')})" for x in (c.get("ranked") or [])[:3]) or "none eligible"
            eng.append(f"- {p.get('name')}, {c.get('set')}: {top}")
    pilot = (f"The pilot runs [[pilot_days]] days (the brief: \"{F['pilot_quote']}\"): every target day is a day of the pilot, 1 to {F['pilot_days']}."
             if F["pilot_days"] else "The brief states no pilot length: every target day is a day of Year 1 (1 to 365), and the page says so.")
    if on and F["pilot_days"] and reg.get("pilot_days"):     # IC-3 (D7): the pilot by its id; the brief's sentence is the brief's
        pilot = (f"The pilot's length in days: [[pilot_days]] = {reg.shown('pilot_days')}. Every target day is a day of the pilot, from its first day "
                 "to day [[pilot_days]].")
    sections = ", ".join(n for n, *_ in NR.SECTIONS["oracle"])
    if on:       # BUILD IC-3 (D10): the rules, the registry block and the mentions the analysis cited, each with its sentence
        cited_q = set(reg.cited_mentions(json.dumps(ma, ensure_ascii=False)))     # IC-3 (review b #15): either form, any letter case
        dlv_reg = after_data_ic3(reg, raw, F, only=cited_q, quotes=False).rstrip("\n")
    else:
        dlv_reg = registry_prompt_block(reg, raw, F, quotes=False)
    return f"""{DELIVERABLES_TAG}
You wrote this run's analysis earlier. Python has since computed the financial model, the SROI, the cost of inaction and the prioritization
model. Write the brief's remaining deliverables as ONE JSON object. {_DLV_FIGURES}

<brief_items>
{items}
</brief_items>
{pilot}
{dlv_reg}
<python_figures>
{figs}
</python_figures>
<prioritization_model>
{chr(10).join(eng) or "- (no prioritization model this run)"}
</prioritization_model>
<use_of_funds>
{funds or "- (no use of funds this run)"}
</use_of_funds>
<risks>
{risks or "(no risks)"}
</risks>
<scoring>
{dims or "- (no scoring in the brief)"}
</scoring>

Return ONLY this JSON object (no markdown fences):
{{"kpis": [{{"indicator": "<what is counted, at most {DLV_CAPS['indicator']} characters>", "type": "leading" or "lagging",
   "baseline": "[[<id>]]" or "new", "target": <a number: a count, or a share from 0 to 1>, {kpi_basis},
   "target_day": <a day number>, "cadence": "<how often it is read>", "fund_line": "<one of: {', '.join(FUND_KEYS)}>",
   "source": "<an endpoint of this run, or {PROGRAM_RECORDS}>"}}] — at least two leading and two lagging indicators,
 "failure_modes": [{{"where": "<an age (such as age 21) or a handoff between two named services>", "what_fails": "<one sentence>",
   "evidence": ["[[<id>]]", ...], "remedy": "<one sentence>", "fund_line": "<a fund line key>", "indicator": "<one of your kpis' indicators>"}}],
 "renewal": {{"x": <how many of your kpis must be at target>, "day": <the day they are read>}},
 "thesis": {{"claim": "<one sentence>", "kill": "<the result that would prove it wrong>", "tripwire": {{"indicator": "<one of your kpis' indicators>",
   "threshold": <a number>, "direction": "below" or "above" (the side of the threshold that trips it), "day": <a day>, "response": "<what the program does if it trips>"}}}},
 "judge_qa": [{{"dimension": "<a scoring dimension, as written above>", "question": "<the hardest question a judge could ask on it>",
   "answer": "<2-3 sentences, figures as tokens>", "section": "<one of: {sections}>"}}] — one per scoring dimension,
 "early_warnings": [{{"risk": <a risk's index>, "indicator": "<one of your kpis' indicators, or a registry id>", "threshold": "<the level that warns>",
   "cadence": "<how often it is read>"}}],
 "triggers": [{{"scenario": "<what could happen>", "indicator": "<one of your kpis' indicators, or a registry id>", "threshold": "<the level>",
   "response": "<what the program does>"}}],
 "headlines": {{"<one of: {', '.join(HEADLINE_SECTIONS)}>": "<that section's title as a claim with one figure token, at most {DLV_CAPS['headline']} characters>"}}}}
   — one for each of those sections (the other sections' titles are the analysis's own)
Python checks every id, fund line, risk index, section name and day; an entry that fails is left out and the page says so.{fields_block}"""


@stage("deliverables", depends_on=["data_loading", "master_analysis", "financial_model", "prioritization"])
def stage_deliverables(deps: dict) -> dict:
    """BUILD B3 (D3): the deliverables call — OPUS, effort medium, at most 12,000 tokens, streamed, one attempt, only while the Oracle has run
    long enough to finish before the Oracle's end time (BUILD IC-4, D9). Its reply is kept (keep_reply) and checked in Python at render (deliverables_view). At the curveball
    Block 1's words stand (BLOCK1_FILES), their figures recomputed on this run's registry. Never raises: no reply → each section renders
    its Python part and says its words were not written this run."""
    try:
        if is_block2():
            b1 = _cp_data(CHECKPOINT_DIR / NR.BLOCK1_DIR / "deliverables.json")
            meta_, _w = REG.admit(NR.read_json(CHECKPOINT_DIR / NR.BLOCK1_DIR / NR.BLOCK1_META), "saved", NR.BLOCK1_META)       # B3 (Phase 4, review c F10): this challenge's
            cid_ = ((_brief_contract() or {}).get("challenge") or {}).get("id")
            if cid_ and isinstance(meta_, dict) and meta_.get("challenge") != cid_:
                NR.say(f"    deliverables: Block 1's words are of {meta_.get('challenge')}, not {cid_} — each section shows its Python part")
                return {"status": "no Block 1 deliverables", "reply": None}
            ed = ((deps.get("master_analysis") or {}).get("curveball_update") or {}).get("deliverables")
            if isinstance(ed, dict) and isinstance(ed.get("reply"), dict):     # BUILD IC-4 (D11): edited at the curveball, not carried
                NR.say("    deliverables: Block 1's words with the update's edits applied; their figures are this run's")
                NR.mark_path("deliverables.b2_edited")      # BUILD IC-8 (D14)
                return {"status": "edited", "reply": ed["reply"], "reply_file": ed.get("reply_file"), "ids": ed.get("ids"),
                        "risk_refs": ed.get("risk_refs")}
            if isinstance(b1, dict) and isinstance(b1.get("reply"), dict):
                NR.say("    deliverables: Block 1's words stand at the curveball; their figures are this run's")
                NR.mark_path("deliverables.b2_block1_stands")      # BUILD IC-8 (D14)
                return dict({"status": "block1", "reply": b1["reply"], "reply_file": b1.get("reply_file")}, **deliverable_refs(b1, None))
            NR.say("    deliverables: no Block 1 deliverables to carry forward — each section shows its Python part")
            return {"status": "no Block 1 deliverables", "reply": None}
        NR.mark_path("deliverables.keyed_lines" if NR.call_form("oracle/deliverables") == "lines" else "deliverables.whole_object")  # IC-8 (D14)
        if NR.call_form("oracle/deliverables") == "lines":      # BUILD IC-6 (D13): NAIPC_IC6_DELIVERABLES on — the items in lines; its
            return deliverables_lines(deps)                       # start rule is line_call's (the first line's time)
        ok_, note_, _need = NR.start_check("oracle/deliverables", CHECKPOINT_DIR, OPUS, ORACLE_RATE, left=_end_at() - _clock())   # IC-4 (D9), IC-6 (D2)
        if not ok_:
            NR.say(f"    deliverables: call skipped — {note_}")
            return {"status": "skipped: no time for the call", "reply": None}
        prompt = deliverables_prompt(deps)
        NR.say(f"    deliverables: one call — prompt {len(prompt):,} characters")
        raw = _stream_text(prompt, SYSTEM_PROMPT, OPUS, "oracle/deliverables")
        kept = keep_reply("deliverables_reply", raw)
        try:
            reply = _loads_reply(extract_json(raw), "deliverables")   # BUILD IC-5 (D1): admitted inside   # BUILD IC (D1); IC-3 (D4): strict
        except Exception as e:  # noqa: BLE001
            NR.say(f"    ⚠ deliverables: the reply is not JSON ({type(e).__name__}) — each section shows its Python part")
            return {"status": "not JSON", "reply": None, "reply_file": kept}
        if not isinstance(reply, dict):
            NR.say("    ⚠ deliverables: the reply is not a JSON object — each section shows its Python part")
            return {"status": "not a JSON object", "reply": None, "reply_file": kept}
        out_ = {"status": "ok", "reply": reply, "reply_file": kept}
        return dict(out_, **deliverable_refs(out_, deps.get("master_analysis")))
    except Exception as e:  # noqa: BLE001 — a failed call is a section that says so, never a lost page
        NR.say(f"    ⚠ deliverables: the call failed ({type(e).__name__}: {str(e)[:160]}) — each section shows its Python part")
        return {"status": f"failed: {type(e).__name__}", "reply": None}


# ═══ BUILD IC-6 (D13; W1): the deliverables item by item (NAIPC_IC6_DELIVERABLES) — line form through NR.line_call ═══
DLV_LISTS = ("kpis", "failure_modes", "early_warnings", "triggers", "judge_qa")    # the reply's lists, the KPIs first (the others read them)
DLV_FIELDS = ("renewal", "thesis", "headlines")      # its whole fields — the thesis too: deliverables_view reads it (D13's list names two)
DLV_IC5_ONE = "Write the brief's remaining deliverables as ONE JSON object."
DLV_IC5_FORM = "\n\nReturn ONLY this JSON object (no markdown fences):\n"
DLV_NOT_WRITTEN = "not written this run — the reply ended before it"


def dlv_key(o):
    """A deliverables line's key — "<list>.<n>" for a list's item, "field.<name>" for a field — or None (no such line)."""
    if not isinstance(o, dict):
        return None
    if o.get("list") in DLV_LISTS and isinstance(o.get("n"), int) and not isinstance(o.get("n"), bool) and o["n"] >= 1:
        return f"{o['list']}.{o['n']}"
    if o.get("field") in DLV_FIELDS:
        return f"field.{o['field']}"
    return None


def deliverables_lines_prompt(deps: dict) -> str:
    """BUILD IC-6 (D13): the deliverables request in lines — IC-5's request (deliverables_prompt: every block before the reply's form, byte for
    byte, but its one sentence on the form) with the reply as JSON Lines: one line per item, {"list", "n", "value"} — each value the form
    IC-5's object gives that list's entries (a KPI with its basis_sources and confidence, IC-5 D13), the KPIs first — then {"field",
    "value"} for the renewal gate, the thesis and the headlines."""
    full = deliverables_prompt(deps)
    head, sep, _tail = full.partition(DLV_IC5_FORM)
    if not sep or DLV_IC5_ONE not in head:
        raise ValueError("the deliverables request no longer holds IC-5's form sentences")
    head = head.replace(DLV_IC5_ONE, "Write the brief's remaining deliverables as JSON Lines, one item on each line.", 1)
    on5 = NR.ic5_prompt()
    kpi_basis = (f'"target_basis": "<why, in plain sentences, at most {DLV_CAPS["basis"]} characters>", "basis_sources": ["<an endpoint of '
                 f'this run>", ...], "confidence": "high" or "medium" or "low"' if on5
                 else f'"target_basis": "<why, at most {DLV_CAPS["basis"]} characters>"')
    fields_block = ("\nIn this reply, write sources in basis_sources and confidence in confidence; the prose fields hold sentences only."
                    if on5 else "")
    sections = ", ".join(n for n, *_ in NR.SECTIONS["oracle"])
    return head + f"""

Return ONLY JSON Lines (no markdown fences, no prose): one JSON object on each line, no line break inside an object, in this order — the
KPIs first, then each list in turn, then the three fields. Number each list's items from 1 ("n"); write each item once.
{{"list": "kpis", "n": <its number>, "value": {{"indicator": "<what is counted, at most {DLV_CAPS['indicator']} characters>", "type": "leading" or "lagging",
   "baseline": "[[<id>]]" or "new", "target": <a number: a count, or a share from 0 to 1>, {kpi_basis},
   "target_day": <a day number>, "cadence": "<how often it is read>", "fund_line": "<one of: {', '.join(FUND_KEYS)}>",
   "source": "<an endpoint of this run, or {PROGRAM_RECORDS}>"}}}} — at least two leading and two lagging indicators
{{"list": "failure_modes", "n": <its number>, "value": {{"where": "<an age (such as age 21) or a handoff between two named services>",
   "what_fails": "<one sentence>", "evidence": ["[[<id>]]", ...], "remedy": "<one sentence>", "fund_line": "<a fund line key>",
   "indicator": "<one of your kpis' indicators>"}}}}
{{"list": "early_warnings", "n": <its number>, "value": {{"risk": <a risk's index>, "indicator": "<one of your kpis' indicators, or a registry id>",
   "threshold": "<the level that warns>", "cadence": "<how often it is read>"}}}}
{{"list": "triggers", "n": <its number>, "value": {{"scenario": "<what could happen>", "indicator": "<one of your kpis' indicators, or a registry id>",
   "threshold": "<the level>", "response": "<what the program does>"}}}}
{{"list": "judge_qa", "n": <its number>, "value": {{"dimension": "<a scoring dimension, as written above>", "question": "<the hardest question a
   judge could ask on it>", "answer": "<2-3 sentences, figures as tokens>", "section": "<one of: {sections}>"}}}} — one per scoring dimension
{{"field": "renewal", "value": {{"x": <how many of your kpis must be at target>, "day": <the day they are read>}}}}
{{"field": "thesis", "value": {{"claim": "<one sentence>", "kill": "<the result that would prove it wrong>", "tripwire": {{"indicator": "<one of your
   kpis' indicators>", "threshold": <a number>, "direction": "below" or "above" (the side of the threshold that trips it), "day": <a day>,
   "response": "<what the program does if it trips>"}}}}}}
{{"field": "headlines", "value": {{"<one of: {', '.join(HEADLINE_SECTIONS)}>": "<that section's title as a claim with one figure token, at most
   {DLV_CAPS['headline']} characters>"}}}} — one for each of those sections (the other sections' titles are the analysis's own)
Python checks every id, fund line, risk index, section name and day; an entry that fails is left out and the page says so.{fields_block}"""


def deliverables_from_lines(rows):
    """BUILD IC-6 (D13): the reply's lines → (the object deliverables_view reads — each list in its items' numbers, each field —, the
    problems [(line, why)], the lists and fields never written). A line is an item when it names a list of DLV_LISTS with a whole number n
    from 1, or a field of DLV_FIELDS; a second line for the same item is named and not used (the first stands); any other line is named."""
    got, fields, probs = {}, {}, []
    for r in rows:
        o, n_ = r["edit"], r["line"]
        k = dlv_key(o)
        if k is None:
            probs.append((n_, "a list's item names one of " + ", ".join(DLV_LISTS) + " and its number n from 1; a field names one of "
                          + ", ".join(DLV_FIELDS) if ("list" in o or "field" in o) else "neither a list's item nor a field"))
            continue
        if "value" not in o:
            probs.append((n_, "no value"))
            continue
        if k.startswith("field."):
            if o["field"] in fields:
                probs.append((n_, f"a second line for the {o['field']} — the first stands"))
                continue
            fields[o["field"]] = o["value"]
            continue
        lst = got.setdefault(o["list"], {})
        if o["n"] in lst:
            probs.append((n_, f"a second line for {o['list']} item {o['n']} — the first stands"))
            continue
        lst[o["n"]] = o["value"]
    reply = {L_: [got[L_][n] for n in sorted(got[L_])] for L_ in DLV_LISTS if L_ in got}
    reply.update(fields)
    return reply, probs, [k for k in DLV_LISTS + DLV_FIELDS if k not in reply]


def _is_line_reply(raw, keys) -> bool:
    """BUILD IC-6 (D13): True when a line of the reply decodes to one object holding one of `keys` (the line form); anything else (one JSON
    object, IC-5's form; text that is no JSON) is read the IC-5 way. A probe of the form — its result a yes or no, never data."""
    for raw_line in (raw or "").split("\n"):
        s_ = raw_line.strip()
        if not s_.startswith("{"):
            continue
        try:
            o_, _f = NR.strict_loads(s_, "lines")
        except Exception:  # noqa: BLE001
            continue
        if isinstance(o_, dict) and any(k in o_ for k in keys):
            return True
    return False


def deliverables_lines(deps: dict) -> dict:
    """BUILD IC-6 (D13): the deliverables call in lines — NR.line_call over "oracle/deliverables" (its first line's time the start rule's need;
    one continuation after a token or transport cut, NAIPC_IC6_CONTINUE on), every complete item kept: Python assembles the object the
    code reads today (deliverables_view unchanged but for the lists never written, each named and its section's Python part shown). A reply
    that is one JSON object is read the IC-5 way. → the stage's result, IC-5's form, with "unwritten" and "lines" (how it ended, the items,
    the problems, the continuation)."""
    prompt = deliverables_lines_prompt(deps)
    NR.say(f"    deliverables: one call, item by item — prompt {len(prompt):,} characters")
    end = _end_at()

    def refuse(words):
        raise StageBudgetExceeded(words)

    def resume(objs):
        keys = [k for k in (dlv_key(o) for o in objs) if k]
        return (keys[-1] if keys else "the last complete line"), None, 1
    try:
        res = NR.line_call("oracle/deliverables", lambda msgs, lab: _stream_text(None, SYSTEM_PROMPT, OPUS, "oracle/deliverables", end_at=end,
                                                                                 messages=msgs, label=lab),
                           prompt, end_at=end, clock=_clock, retryable=is_retryable, retry_wait=_retry_wait, sleep=_sleep, refuse=refuse,
                           log_dir=CHECKPOINT_DIR, model=OPUS, stated_rate=ORACLE_RATE, key_of=dlv_key, resume=resume,
                           continue_ok=NR.ic6("NAIPC_IC6_CONTINUE"), stage_label="deliverables", continue_words=NR.CONTINUE_ITEM_WORDS)
    except StageBudgetExceeded as e:
        NR.say(f"    deliverables: call skipped — {e}")
        return {"status": "skipped: no time for the call", "reply": None}
    deliverables_lines.last = res
    raw, how, c_ = res["text"], res["how"], res.get("continuation")
    if c_:
        NR.say(f"    deliverables: continuation — {c_['how']}" + (f"; {c_['lines']} line(s) more" if c_.get("label") else "")
               + (f"; {len(c_['dropped'])} written again, dropped" if c_.get("dropped") else ""))
        if c_.get("label"):
            how += f"; continued: {c_['lines']} more line(s), the continuation's ending: {c_['how']}"
    if raw is None:
        NR.say(f"    ⚠ deliverables: {how} — nothing of its reply is used; each section shows its Python part")
        return {"status": f"failed: {how}", "reply": None}
    kept = keep_reply("deliverables_reply", raw)
    if not _is_line_reply(raw, ("list", "field")):        # one JSON object (IC-5's form), or no JSON at all: the IC-5 way
        try:
            reply = _loads_reply(extract_json(raw), "deliverables")
        except Exception as e:  # noqa: BLE001
            NR.say(f"    ⚠ deliverables: the reply is not JSON ({type(e).__name__}) — each section shows its Python part")
            return {"status": "not JSON", "reply": None, "reply_file": kept}
        if not isinstance(reply, dict):
            NR.say("    ⚠ deliverables: the reply is not a JSON object — each section shows its Python part")
            return {"status": "not a JSON object", "reply": None, "reply_file": kept}
        out_ = {"status": "ok", "reply": reply, "reply_file": kept, "form": "object"}
        return dict(out_, **deliverable_refs(out_, deps.get("master_analysis")))
    rows, problems, partial = REG.admit(NR.parse_edit_lines(raw, complete=(how == "end_turn")), "reply", "deliverables lines")   # IC-5 (D1)
    reply, probs, unwritten = deliverables_from_lines(rows)
    probs = [(p["line"], p["why"]) for p in problems] + probs + ([("last", "the reply was cut inside its last line, so that line is not used")]
                                                                   if partial else [])
    for ln, why in probs:
        NR.say(f"    deliverables: line {ln} not used — {why}")
    for k_ in (c_ or {}).get("dropped") or []:
        NR.say(f"    deliverables: {k_} written again in the continuation — the first stands")
    if unwritten and how != "end_turn":
        NR.say(f"    deliverables: not written this run (the reply: {how}) — {', '.join(unwritten)}")
    n_items = sum(len(v) for k, v in reply.items() if k in DLV_LISTS) + sum(1 for k in DLV_FIELDS if k in reply)
    out_ = {"status": "ok" if how == "end_turn" else "cut", "reply": reply, "reply_file": kept, "form": "lines",
            "unwritten": unwritten if how != "end_turn" else [],
            "lines": {"how": how, "items": n_items, "read": len(rows) + len(problems) + (1 if partial else 0),
                      "problems": [{"line": ln, "why": why} for ln, why in probs], "continuation": c_}}
    return dict(out_, **deliverable_refs(out_, deps.get("master_analysis")))


def _written(view, key) -> bool:
    """BUILD IC-6 (D13): whether the deliverables' words for `key` arrived — the reply came, and (a line reply that was cut) that list or field
    was written; else its section shows its Python part and says its words were not written this run."""
    return bool((view or {}).get("words")) and key not in ((view or {}).get("unwritten") or ())


def _nth(lst, i):
    """The i-th of a list, or None (an id list shorter than the reply's list)."""
    return lst[i] if isinstance(lst, list) and 0 <= i < len(lst) else None


def deliverable_refs(dl: dict, ma) -> dict:
    """BUILD IC-4 (D1): the deliverables' ids (every entry of each list in the reply's order, REG.reply_item_ids) and each early warning's
    risk as an id ("risk_refs"), read once against the risk list the reply was written against: this run's analysis for this run's reply;
    Block 1's base analysis for Block 1's reply (status "block1"). Kept when the result already holds them (a checkpoint of this build);
    computed for one written before IC-4. Never raises."""
    try:
        reply = (dl or {}).get("reply") if isinstance((dl or {}).get("reply"), dict) else None
        if reply is None:
            return {"ids": REG.reply_item_ids({}), "risk_refs": []}
        ids = dl.get("ids") if isinstance(dl.get("ids"), dict) else REG.reply_item_ids(reply)
        refs = dl.get("risk_refs")
        if not (isinstance(refs, list) and len(refs) == len(ids.get("early_warnings") or [])):
            if dl.get("status") == "block1" or ma is None:
                ma = (load_block1_base(((_brief_contract() or {}).get("challenge") or {}).get("id"))[0] or {}).get("ma") or {}
                refs = REG.risk_refs(reply, REG.analysis_item_ids(ma)["ids"]["risks"])
            else:
                refs = REG.risk_refs(reply, REG.analysis_item_ids(ma)["ids"]["risks"])
        return {"ids": ids, "risk_refs": refs}
    except Exception:  # noqa: BLE001
        return {"ids": REG.reply_item_ids((dl or {}).get("reply") or {}), "risk_refs": []}


def _status_words(st) -> str:
    """B3 (Phase 4, review c F8): the deliverables call's status as the page says it — plain words, never an exception's name."""
    st = str(st or "")
    return ("the call did not complete" if st.startswith("failed") else "the run was short of time, so the call was not made"
            if st.startswith("skipped") else "the reply could not be read" if st in ("not JSON", "not a JSON object") else
            "the reply ended early" if st == "cut" else            # BUILD IC-6 (D13): a line reply cut — its complete items kept
            "" if st in ("ok", "block1", "edited", "not run") else st)


def _dlv_fit(v, field, reg=None):
    """BUILD IC-8 (D1): a deliverables field at its destination — target DLV_CAPS[field] (the request's "at most N characters"), hard twice
    that (the code's own tolerance where IC-7 sliced at twice), whole leading sentences, an excerpt only past the hard maximum."""
    return REG.fit_cap(v, "oracle.deliverables", 2 * DLV_CAPS[field], reg=reg, target=DLV_CAPS[field])     # the page's figures (reg)


def _dlv_text(v, cap, reg=None):
    """A deliverables field's clean words under _dlv_fit's rule (`cap`: its DLV_CAPS value — the target; hard twice that), measured on
    the page's figures (`reg`: this run's registry)."""
    return REG.fit_cap(NR.clean_text(v), "oracle.deliverables", 2 * cap, reg=reg, target=cap) if isinstance(v, str) and NR.clean_text(v) else ""


def _dlv_day(v, F):
    """A target day as an int inside the pilot (or Year 1 without one), or None."""
    x = REG.parse_number(v)
    if x is None or not float(x).is_integer():
        return None
    x = int(x)
    top = F["pilot_days"] or 365
    return x if 1 <= x <= top else None


def _headline_check(h, reg):
    """BUILD IC-8 (D5): one headline member's rule (V23) — a claim with exactly one figure token (an nf: declaration beside it allowed;
    review a F11: it resolves when it is no quantity) and, once the page's counts exist, every token names a registry figure. → None, or
    the failed rule: "empty_words", "token_rule", "token_names_nothing". No track: a count token here is the Oracle's own page list."""
    if not isinstance(h, str) or not h.strip():
        return "empty_words"
    if len([x for x in REG.TOKEN_RE.findall(h) if not x.strip().lower().startswith("nf:")]) != 1:
        return "token_rule"
    if not all(reg.resolves(x) for x in REG.TOKEN_RE.findall(h)):
        return "token_names_nothing"
    return None


def headline_units(cand, base, reg, op=None, block2=False):
    """BUILD IC-8 (D5): the headlines one by one — each section's member (`cand`: {section: as written}) fitted at its destination
    (oracle.headlines: target 160, hard 320, no excerpt; the fit owned by its unit) and checked (_headline_check); a member that fails,
    or a section the update's set lacks, takes Block 1's headline for that section (`base`), fitted and checked the same way under this
    run's registry → rejected_kept_base with the failed rule; if that fails too, the section's own title (the page's, stored None) →
    fallback_default. A member that passes: Block 1 added; Block 2 by the d.headlines item's status (`op`). A fit that trimmed a
    member is its unit's outcome. → ({section: stored or None}, [{unit, disposition, reason, source, item}])."""
    shown, units = {}, []
    ok_d, ok_s = (REG.EDIT_OUTCOMES.get(op) or REG.EDIT_OUTCOMES["kept"])[0::2] if block2 else ("added_valid", "candidate")

    def fit(h, u, src):
        if not (isinstance(h, str) and h.strip()):
            return None, None, None
        return REG.fit_shown(h, reg, "oracle.headlines", track=None, source=src, unit=u)
    for s in [x for x in HEADLINE_SECTIONS if x in cand or x in (base or {})]:
        u = f"oracle.headlines.{s}"
        t, fd, fr = fit(cand.get(s), u, ok_s)
        why = "no_member" if s not in cand else ("empty_words" if not t else _headline_check(t, reg))
        if why is None:
            shown[s] = t
            units.append({"unit": u, "disposition": fd if fd in ("trimmed",) else ok_d, "reason": fr if fd == "trimmed" else "rules_pass",
                          "source": ok_s, "item": "d.headlines"})
            continue
        bt, bd, br = fit((base or {}).get(s), u, "base")
        if bt and _headline_check(bt, reg) is None:
            shown[s] = bt
            units.append({"unit": u, "disposition": "rejected_kept_base", "reason": why, "source": "base", "item": "d.headlines"})
        else:
            shown[s] = None
            units.append({"unit": u, "disposition": "fallback_default", "reason": why, "source": "default", "item": "d.headlines"})
    return shown, units


def check_headlines(dview, reg, base=None, op=None, block2=False):
    """BUILD IC-5 (D7, G13): the pending headlines checked once the registry is resolved (after the last reg.add_counts: a page list's
    count exists). Raises REG.StageError, before keeping or dropping any, when the registry is not resolved yet. BUILD IC-8 (D5): one by
    one (headline_units) — a member that fails takes Block 1's headline for its section (`base`: Block 1's members), else the section's
    own title, left out and named as before; the outcomes in dview["headline_units"] (finalize_oracle records them)."""
    REG.require(reg, REG.RESOLVED, "the headline check")
    shown, units = headline_units(dview.get("headlines_pending") or {}, base, reg, op, block2)
    for s, h in shown.items():
        if h is not None:
            dview["headlines"][s] = h
    for x in units:
        s, w = x["unit"].rsplit(".", 1)[-1], f"headlines.{x['unit'].rsplit('.', 1)[-1][:30]}"
        if x["disposition"] == "rejected_kept_base":
            NR.say(f"    deliverables: {w} — {x['reason']}: Block 1's headline for this section is shown")
        elif x["disposition"] == "fallback_default":
            dview["dropped"].append((w, "not a claim with one figure token naming a registry figure"))
            NR.say(f"    deliverables: left out {w} — not a claim with one figure token naming a registry figure ({x['reason']}); "
                   "the section's own title is shown")
    kb_ = [x["unit"].rsplit(".", 1)[-1] for x in units if x["disposition"] == "rejected_kept_base"]
    if block2 and kb_ and op == "replaced":
        NR.say(f"    deliverables: the headlines updated in part — Block 1's for {', '.join(kb_)}")
    dview["headline_units"] = units
    dview["headlines_pending"] = {}
    return dview


def deliverables_view(dl: dict, reg, fin: dict, ma: dict, F: dict, endpoints) -> dict:
    """BUILD B3 (D3–D8): the call's reply checked against this run — every id (a token must name a registry figure), fund line (a key the
    use of funds has), risk index, section name and day (inside the pilot, or Year 1 without one); an entry that fails is left out and
    named (`dropped`). Targets and thresholds become MODELED registry entries with the model's basis (kpi.<n>.target, renewal.x,
    renewal.day, tripwire.threshold). Never raises. {status, words (did the call's words arrive), kpis, failure_modes, renewal, thesis,
    judge_qa, early_warnings, triggers, headlines, dropped}."""
    reply = (dl or {}).get("reply") if isinstance((dl or {}).get("reply"), dict) else None
    status = str((dl or {}).get("status") or "not run")
    out = {"status": status, "words": reply is not None, "kpis": [], "failure_modes": [], "renewal": None, "thesis": None, "judge_qa": [],
           "early_warnings": [], "triggers": [], "headlines": {}, "headlines_pending": {}, "dropped": []}
    if reply is None:
        return out
    drop = out["dropped"]
    out["unwritten"] = [k for k in ((dl or {}).get("unwritten") or []) if k in DLV_LISTS + DLV_FIELDS]     # BUILD IC-6 (D13): a cut reply's
    for k_ in out["unwritten"]:                                                                         # parts never written, named
        drop.append((k_, DLV_NOT_WRITTEN))
    refs_ = deliverable_refs(dl, ma)                  # BUILD IC-4 (D1): every entry's id; each early warning's risk as an id
    rid_ = refs_["ids"]
    fund_keys = {ln["key"] for ln in ((fin.get("use_of_funds") or {}).get("lines") or []) if isinstance(ln, dict)}
    eps = {str(e) for e in endpoints or []}

    def tok_ok(s):
        toks = REG.TOKEN_RE.findall(s or "")
        return all(reg.resolves(x) for x in toks)        # review a F11: a declaration resolves when it is no quantity
    for i, k in enumerate(reply.get("kpis") or [] if isinstance(reply.get("kpis"), list) else []):
        w = f"kpis[{i}]"
        if not isinstance(k, dict):
            drop.append((w, "not an object"))
            continue
        ind, typ = _dlv_text(k.get("indicator"), DLV_CAPS["indicator"], reg=reg), str(k.get("type") or "").strip().lower()
        base, tgt, day = str(k.get("baseline") or "").strip(), REG.parse_number(k.get("target")), _dlv_day(k.get("target_day"), F)
        src, fl = str(k.get("source") or "").strip(), str(k.get("fund_line") or "").strip()
        bt = REG.TOKEN_RE.fullmatch(base)
        why = ("no indicator" if not ind else "its indicator cites a figure that names nothing" if REG.names_nothing(reg, held_as_numbers(ind)) else   # IC-8 (D3)
               "type is neither leading nor lagging" if typ not in ("leading", "lagging") else
               "its baseline is neither a registry figure nor \"new\"" if not (base.lower() == "new" or (bt and reg.lookup(bt.group(1))[0])) else
               "its target is not a number" if tgt is None or tgt < 0 else
               f"its target day is not a day of the {'pilot' if F['pilot_days'] else 'first year'}" if day is None else
               f"fund line {fl[:30]!r} is not a line of the use of funds" if fl not in fund_keys else
               f"source {src[:40]!r} is neither an endpoint of this run nor {PROGRAM_RECORDS}" if src not in eps and src.lower() != PROGRAM_RECORDS else None)
        if why:
            drop.append((f"{w} ({ind[:50] or '?'})", why))
            continue
        kid = _nth(rid_.get("kpis"), i) or f"kpi.{i + 1}"     # IC-4 (D1): its id, from its place in the reply
        bsrc, conf = [], None       # BUILD IC-5 (D13): its sources, each an endpoint of this run (else named), and its confidence
        basis_txt = k.get("target_basis") if isinstance(k.get("target_basis"), str) else ""
        if NR.ic5_prompt():
            for j, nm in enumerate(k.get("basis_sources") if isinstance(k.get("basis_sources"), list) else []):
                nm_ = str(nm).strip() if isinstance(nm, str) else ""
                if nm_ in eps and nm_ not in bsrc:
                    bsrc.append(nm_)
                elif nm_ not in bsrc:
                    drop.append((f"{w}.basis_sources[{j}] ({nm_[:40] or '?'})", "not an endpoint of this run"))
            c_ = str(k.get("confidence") or "").strip().lower()
            if c_ in ("high", "medium", "low"):
                conf = c_
            elif c_:
                drop.append((f"{w}.confidence ({c_[:20]})", "neither high, medium nor low"))
            # BUILD IC-5 (D13b): a tag the model wrote in target_basis takes the fields' route — the same candidates, the same checks,
            # each refusal named — and leaves the prose before its cap: one route from the reply to the page's badges (N9: "Legacy
            # attribution handled; semantic source/confidence structure retained; unknown forms dispositioned")
            basis_txt, b_tags = _basis_tags(basis_txt)
            for kind_, c_t, raw_ in b_tags:
                if kind_ == "data":
                    names_ = [x.strip() for x in c_t.replace(";", ",").split(",") if x.strip()]
                    if not names_:
                        drop.append((f"{w}.target_basis ({raw_[:40]})", "a data tag that names no endpoint"))
                    for nm_ in names_:
                        if nm_ in eps and nm_ not in bsrc:
                            bsrc.append(nm_)
                        elif nm_ not in eps:
                            drop.append((f"{w}.target_basis ({nm_[:40]})", "not an endpoint of this run"))
                    continue
                lv_ = REG._confidence(c_t)
                if lv_ not in ("high", "medium", "low"):
                    drop.append((f"{w}.target_basis ({raw_[:40]})", "neither high, medium nor low" if lv_ else "an estimate tag that names no confidence"))
                elif conf is None:
                    conf = lv_
                elif lv_ != conf:
                    drop.append((f"{w}.target_basis ({raw_[:40]})", f"its confidence field already says {conf}"))
        n = int(kid.split(".")[1])
        # B3 (Phase 4, review c F4): the target is written in its baseline's form (money, a percent, a count) when the baseline is a
        # registry figure — a percent baseline with a target above 1 is not read as a percent; else a fraction is a percent, a
        # non-whole number a number, a whole one a count
        bfmt = (reg.lookup(bt.group(1))[0] or {}).get("fmt") if bt else None
        fmt = (bfmt if bfmt and bfmt != "year" and not (bfmt == "pct" and tgt > 1) else
               "pct" if 0 < tgt <= 1 and not float(tgt).is_integer() else "count" if float(tgt).is_integer() else "num")
        # BUILD IC-5 (D13c): the target's registry entry holds the record the KPI table prints — the prose, then its checked fields as
        # Python's canonical tags (_kpi_basis); with the prose alone it lost the sources and the confidence (N9: "semantic
        # source/confidence structure retained"). The switch off: no field read, the prose as before
        e = reg.add(f"kpi.{n}.target", tgt, "MODELED", fmt=fmt, label=f"target: {reg.plain(ind)}", label_model=reg.plain(ind),
                    source="the analysis's target, its basis: "
                    + (_kpi_basis({"basis": _plain_unit(reg, _dlv_text(basis_txt, DLV_CAPS["basis"], reg=reg), omittable=True), "basis_sources": bsrc,
                                   "confidence": conf})
                       or "none given"))
        d_ = reg.add(f"kpi.{n}.day", day, "MODELED", fmt="count", unit="days", label=f"by day: {reg.plain(ind)}", label_model=reg.plain(ind),
                     source="the analysis's target day")
        out["kpis"].append({"n": n, "id": kid, "indicator": ind, "type": typ, "baseline_id": None if base.lower() == "new" else reg.lookup(bt.group(1))[0]["id"],
                            "target_id": e["id"] if e else None, "day": day, "day_id": d_["id"] if d_ else None, "cadence": _dlv_text(k.get("cadence"), DLV_CAPS["cadence"], reg=reg) or "not given",
                            "fund_line": fl, "source": PROGRAM_RECORDS if src.lower() == PROGRAM_RECORDS else src,
                            "basis": _dlv_text(basis_txt, DLV_CAPS["basis"], reg=reg), "basis_sources": bsrc, "confidence": conf})
    names = {x["indicator"].lower(): x for x in out["kpis"]}
    for i, f in enumerate(reply.get("failure_modes") or [] if isinstance(reply.get("failure_modes"), list) else []):
        w = f"failure_modes[{i}]"
        if not isinstance(f, dict):
            drop.append((w, "not an object"))
            continue
        ev = [x for x in (f.get("evidence") or []) if isinstance(x, str)] if isinstance(f.get("evidence"), list) else []
        ev_ok = [REG.TOKEN_RE.fullmatch(x.strip()) for x in ev]
        ev_ids = [reg.lookup(m.group(1))[0]["id"] for m in ev_ok if m and reg.lookup(m.group(1))[0]]
        fl, ind = str(f.get("fund_line") or "").strip(), _dlv_text(f.get("indicator"), DLV_CAPS["indicator"], reg=reg)
        why = ("no place where it fails" if not _dlv_text(f.get("where"), DLV_CAPS["where"], reg=reg) else
               "no evidence that names a registry figure" if not ev_ids else
               f"fund line {fl[:30]!r} is not a line of the use of funds" if fl not in fund_keys else
               f"indicator {ind[:40]!r} is none of the KPI framework's" if ind.lower() not in names else None)
        if why:
            drop.append((w, why))
            continue
        out["failure_modes"].append({"id": _nth(rid_.get("failure_modes"), i),
                                     "kpi": names[ind.lower()]["id"],     # IC-4 (D1): the KPI it reads, by id
                                     "where": _dlv_text(f.get("where"), DLV_CAPS["where"], reg=reg), "what": _dlv_fit(str(f.get("what_fails") or ""), "what", reg=reg),
                                     "evidence": ev_ids, "remedy": _dlv_fit(str(f.get("remedy") or ""), "remedy", reg=reg), "fund_line": fl,
                                     "indicator": names[ind.lower()]["indicator"]})
    rn = reply.get("renewal") if isinstance(reply.get("renewal"), dict) else None
    if rn is not None:
        y = len(out["kpis"])
        x, day = REG.parse_number(rn.get("x")), _dlv_day(rn.get("day"), F)
        why = ("there is no KPI to count" if y == 0 else f"X ({rn.get('x')!r}) is not a whole number from 1 to {y}" if x is None or not float(x).is_integer() or not 1 <= x <= y
               else f"its day is not a day of the {'pilot' if F['pilot_days'] else 'first year'}" if day is None else None)
        if why:
            drop.append(("renewal", why))
        else:
            reg.add("renewal.x", int(x), "MODELED", fmt="count", label="indicators that must be at target for renewal", source="the analysis's renewal gate")
            reg.add("renewal.day", day, "MODELED", fmt="count", unit="days", label="the day the renewal gate is read", source="the analysis's renewal gate")
            out["renewal"] = {"x": int(x), "y": y, "day": day}
    th = reply.get("thesis") if isinstance(reply.get("thesis"), dict) else None
    if th is not None:
        tw = th.get("tripwire") if isinstance(th.get("tripwire"), dict) else {}
        ind, thr, day = _dlv_text(tw.get("indicator"), DLV_CAPS["indicator"], reg=reg), REG.parse_number(tw.get("threshold")), _dlv_day(tw.get("day"), F)
        why = ("no claim" if not str(th.get("claim") or "").strip() else f"the tripwire's indicator {ind[:40]!r} is none of the KPI framework's"
               if ind.lower() not in names else "the tripwire's threshold is not a number" if thr is None else
               "the tripwire's day is outside the pilot" if day is None else None)
        if why:
            drop.append(("thesis", why))
        else:
            reg.add("tripwire.threshold", thr, "MODELED", fmt="pct" if 0 < thr <= 1 and not float(thr).is_integer() else "count",
                    label=f"the tripwire: {reg.plain(ind)}", label_model=reg.plain(ind), source="the analysis's tripwire")
            reg.add("tripwire.day", day, "MODELED", fmt="count", unit="days", label="the day the tripwire is read", label_model="",   # all Python's
                    source="the analysis's tripwire")
            dr_ = str(tw.get("direction") or "below").strip().lower()      # B3 (Phase 4, review d F11): the side that trips it
            out["thesis"] = {"claim": _dlv_fit(str(th["claim"]), "claim", reg=reg), "kill": _dlv_fit(str(th.get("kill") or ""), "kill", reg=reg),
                             "indicator": names[ind.lower()]["indicator"], "kpi": names[ind.lower()]["id"], "id": "thesis.1",
                             "response": _dlv_fit(str(tw.get("response") or ""), "response", reg=reg),
                             "direction": dr_ if dr_ in ("below", "above") else "below"}
    doc = _brief_contract()
    sc = (doc or {}).get("scoring") if isinstance((doc or {}).get("scoring"), dict) else {}
    dims = list(sc.get("dimensions") or [])
    secs = {n for n, *_ in NR.SECTIONS["oracle"]}
    for i, q in enumerate(reply.get("judge_qa") or [] if isinstance(reply.get("judge_qa"), list) else []):
        if not isinstance(q, dict):
            drop.append((f"judge_qa[{i}]", "not an object"))
            continue
        d, s = str(q.get("dimension") or "").strip(), str(q.get("section") or "").strip()
        why = (f"dimension {d[:40]!r} is not one of the brief's" if d not in dims else f"section {s[:40]!r} is not a section of this page" if s not in secs
               else "no question or answer" if not (str(q.get("question") or "").strip() and str(q.get("answer") or "").strip()) else None)
        if why:
            drop.append((f"judge_qa[{i}]", why))
        else:
            out["judge_qa"].append({"id": _nth(rid_.get("judge_qa"), i),
                                    "dimension": d, "question": _dlv_fit(str(q["question"]), "question", reg=reg), "answer": _dlv_fit(str(q["answer"]), "answer", reg=reg),
                                    "section": s})
    # BUILD IC-4 (D1): an early warning names its risk by id, read once against the list it was written against (deliverable_refs);
    # at Block 2 the id resolves in this run's register — an amended risk keeps its id, a removed one is a tombstone (was: Block 1's
    # risks re-found by their exact words, so an amended risk lost its warning — F2)
    aid_ = analysis_ids(ma, doc)
    live_r, dead_r = set(aid_["ids"]["risks"]), {t_["id"] for t_ in aid_["tombstones"] if isinstance(t_, dict)}
    dead_w = {t_["id"]: t_.get("removed_by") for t_ in aid_["tombstones"] if isinstance(t_, dict)}     # IC-4 Phase 7 (review a8)
    for key, lst in (("early_warnings", out["early_warnings"]), ("triggers", out["triggers"])):
        for i, x in enumerate(reply.get(key) or [] if isinstance(reply.get(key), list) else []):
            if not isinstance(x, dict):
                drop.append((f"{key}[{i}]", "not an object"))
                continue
            ind = _dlv_text(x.get("indicator"), DLV_CAPS["indicator"], reg=reg)
            rk = _nth(refs_["risk_refs"], i) if key == "early_warnings" else None
            tid = REG.TOKEN_RE.fullmatch(ind)
            ok_ind = ind.lower() in names or (tid is not None and reg.lookup(tid.group(1))[0] is not None) or reg.get(ind) is not None
            why = ((f"the risk it warns of ({rk}) is no longer on the list — {dead_w.get(rk) or 'removed by the update'}" if rk in dead_r
                    else f"risk index {x.get('risk')!r} names no risk")
                   if key == "early_warnings" and rk not in live_r
                   else f"indicator {ind[:40]!r} is no KPI and no registry figure" if not ok_ind else None)
            if why:
                drop.append((f"{key}[{i}]", why))
                continue
            lst.append({"id": _nth(rid_.get(key), i),
                        "risk": rk, "kpi": names[ind.lower()]["id"] if ind.lower() in names else None,
                        "indicator": names[ind.lower()]["indicator"] if ind.lower() in names else ind,
                        "threshold": _dlv_fit(str(x.get("threshold") or ""), "threshold", reg=reg), "cadence": _dlv_text(x.get("cadence"), DLV_CAPS["cadence"], reg=reg),
                        "scenario": _dlv_fit(str(x.get("scenario") or ""), "scenario", reg=reg), "response": _dlv_fit(str(x.get("response") or ""), "response", reg=reg)})
    hl = reply.get("headlines") if isinstance(reply.get("headlines"), dict) else {}
    for s, h in hl.items():
        if s in secs and s not in HEADLINE_SECTIONS:      # B3 (D10): an existing section's title is the master analysis's own
            drop.append((f"headlines.{str(s)[:30]}", "an existing section's title is the analysis's own; the call writes only the new sections'"))
        elif s not in secs or s not in HEADLINE_SECTIONS:
            drop.append((f"headlines.{str(s)[:30]}", "not a section of this page"))
        else:       # BUILD IC-8 (D5): each member as written — check_headlines decides it, one by one, once the page's counts exist
            out["headlines_pending"][s] = h if isinstance(h, str) else ""
    nl, ng = sum(1 for x in out["kpis"] if x["type"] == "leading"), sum(1 for x in out["kpis"] if x["type"] == "lagging")
    out["kpi_missing"] = [m for m, n_ in (("leading", nl), ("lagging", ng)) if n_ < 2]
    read_ = {w.get("kpi") for w in out["early_warnings"] + out["triggers"] if w.get("kpi")}     # BUILD IC-4 (D1): coverage is said
    out["kpi_unread"] = [k["id"] for k in out["kpis"] if k["id"] not in read_]
    out["risk_ids"] = list(aid_["ids"]["risks"])
    for w, why in drop:
        NR.say(f"    deliverables: left out {w} — {why}")
    return out


@stage("html_assembly", depends_on=["data_loading", "master_analysis", "financial_model", "diagram_generation", "prioritization", "deliverables"])
def stage_html_assembly(deps: dict) -> dict:
    """Assemble final HTML from all pipeline outputs. P0.6 review F2: the data is an input too — without it this stage rendered a page
    with "0 API Endpoints", and a failure of the later full render (which only prints a warning) left that page behind with exit 0."""
    ctx = build_template_context(deps)
    path = render_html(ctx)
    return {"path": str(path), "size_kb": round(path.stat().st_size / 1024, 1)}


# ─────────────────────────────────────────────────────────────────────
# STAGE 6: DEPLOYMENT — Netlify CLI
# Per Implementation Guide §10
# ─────────────────────────────────────────────────────────────────────
@stage("deployment", depends_on=["html_assembly"])
def stage_deployment(deps: dict) -> dict:
    """Deploy to Netlify and return live URL."""
    token = os.environ.get("NETLIFY_AUTH_TOKEN", "")
    site = os.environ.get("NETLIFY_SITE_ID", "")
    if not token or not site:
        console.print("  [yellow]⚠ Netlify credentials not set — skipping deploy[/yellow]")
        return {"url": f"file://{OUTPUT_DIR / 'index.html'}", "deployed": False}

    try:
        result = subprocess.run(
            ["netlify", "deploy", f"--dir={OUTPUT_DIR}", f"--auth={token}",
             f"--site={site}", "--json", "--no-build", "--prod"],
            capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            url = data.get("url", data.get("deploy_url", ""))
            console.print(f"  [green bold]🚀 LIVE: {url}[/green bold]")
            return {"url": url, "deployed": True}
        else:
            NR.say(f"  Deploy error: {result.stderr[:200]}")              # BUILD IC-7 (D8): a tool's text, plain
    except FileNotFoundError:
        console.print("  [yellow]⚠ netlify CLI not found[/yellow]")
    except Exception as e:
        NR.say(f"  Deploy failed: {type(e).__name__}: {e}")             # BUILD IC-7 (D8)
    return {"url": f"file://{OUTPUT_DIR / 'index.html'}", "deployed": False}


# ─────────────────────────────────────────────────────────────────────
# P0.7 (D3): BLOCK 1'S BASE AND BLOCK 2'S RECORD — after the stages of a run
# ─────────────────────────────────────────────────────────────────────
def _block_end(runner):
    """Block 1 (no curveball): write checkpoints/block1/ when the master analysis completed. Block 2: write checkpoints/curveball.json
    (this run's id) for the Architect, the Muse and the hub. Printed; never raises."""
    try:
        chal = os.environ.get("NAIPC_CHALLENGE", "").strip() or None
        if not is_block2():
            if "master_analysis" in runner.results:
                why = write_block1_base(chal, NR.run_id(), set(runner.results))
                NR.say(f"  Block 1 base: checkpoints/{NR.BLOCK1_DIR}/ written (run {NR.run_id()}) — the curveball update starts from it"
                       if why is None else f"  ⚠ Block 1 base not written ({why}) — at the curveball the analysis is regenerated instead")
            return
        rec = curveball_record(runner.results)
        why = NR.write_curveball_json(CHECKPOINT_DIR / NR.CURVEBALL_FILE, rec)
        if why:
            NR.say(f"  ⚠ curveball record not written ({why}) — the other tracks get no moved figures")
        else:
            NR.say(f"  curveball: update {rec['update']} · {len(rec['moved'])} figure(s) moved · {len(rec['derived'])} derived figure(s)"
                   f" → checkpoints/{NR.CURVEBALL_FILE}")
        for d in rec.get("derived_dropped") or []:
            NR.say(f"    derived figure not computed: {d['label']} — {d['why']}")
        for d in rec.get("derived") or []:
            if d.get("compare_note"):
                NR.say(f"    derived figure {d['label']}: {d['compare_note']}")
        if rec.get("moved_note"):
            NR.say(f"    ⚠ {rec['moved_note']}")
        for st_ in rec.get("stale") or []:   # review F1: Block 1's text that still states a value this run changed
            NR.say(f"    ⚠ still as written for Block 1: {stale_line(st_)}")
    except Exception as e:  # noqa: BLE001 — the page is already written; the record's failure is printed
        NR.say(f"  ⚠ curveball / Block 1 base step failed: {type(e).__name__}: {str(e)[:200]}")


# BUILD IC-6 (D7; the fix of 11/16): its own name — D7 first named it ORACLE_PART_NAMES, which replaced E3's dict of the reply's parts
# (above) at load, so E3's part_name raised KeyError 'what_changed' and Block 2's page failed (t_defs_ic6 T-df-4 reads the class)
ORACLE_KIND_PART_NAMES = {"input": "the financial model's inputs", "brief": "the brief's answers", "text": "the analysis's text",
                          "text list": "the analysis's text", "test": "the stress tests", "diagram": "the diagram",
                          "summary": "the response summary", "thesis": "the thesis", "renewal": "the renewal terms", "headlines": "the headlines"}


def oracle_part_name(kind):
    """A page part's name for an edit item's kind: a list's name as the page says it (REG.COUNT_NAMES), else ORACLE_KIND_PART_NAMES."""
    field = next((f for f, k in REG.ITEM_KINDS.items() if k == kind), None)
    return REG.COUNT_NAMES.get(field) if field else ORACLE_KIND_PART_NAMES.get(kind, kind)


def update_parts(edits):
    """BUILD IC-6 (D7): each page part's status from the edits' record — revised at the curveball (an item replaced, added or removed),
    removed at the curveball (every item removed), from Block 1 — its edit was not applied, from Block 1, not reviewed, reviewed,
    unchanged — and why (its items' counts)."""
    by = {}
    for i, it in (edits.get("items") or {}).items():
        by.setdefault(oracle_part_name(it.get("kind")), []).append((edits.get("status") or {}).get(i, "not reviewed"))
    out = []
    for part, sts in by.items():
        c = {s: sts.count(s) for s in sorted(set(sts))}
        if c.get("removed") and len(c) == 1:
            status = "removed at the curveball"
        elif any(c.get(s) for s in ("replaced", "added", "removed")):
            status = "revised at the curveball"
        elif c.get("not applied"):
            status = "from Block 1 — its edit was not applied"
        elif c.get("not reviewed"):
            status = "from Block 1, not reviewed"
        else:
            status = "reviewed, unchanged"
        out.append({"part": part, "status": status, "why": ", ".join(f"{n} {s}" for s, n in c.items())})
    return out


def oracle_update_status():
    """BUILD IC-6 (D7): Block 2's update for the status file, from this run's curveball record: its state (updated; partly updated — a cut
    left items not reviewed; declined; not updated), the edits' counts in words, each part's status. None with no record of this run."""
    rec, _why = NR.read_curveball_json(CHECKPOINT_DIR / NR.CURVEBALL_FILE, os.environ.get("NAIPC_CHALLENGE", "").strip() or None, NR.run_id())
    if not isinstance(rec, dict):
        return {"state": "not updated", "why": "no curveball record of this run"}
    st, e = str(rec.get("update") or ""), rec.get("edits") if isinstance(rec.get("edits"), dict) else None
    if st.startswith("failed: declined"):
        return {"state": "declined", "why": st[len("failed: "):]}
    if st.startswith("failed"):
        return {"state": "not updated", "why": "the update did not complete" + (f": {st[len('failed: '):]}" if st.startswith("failed: ") else "")}
    if st == "full" or st.startswith("no base"):
        return {"state": "updated", "summary": "the analysis written again at the curveball" if st == "full" else
                "the analysis written fresh: there was no Block 1 analysis", "why": None}
    if not e:
        return {"state": "updated", "summary": None, "why": None}
    c = e.get("counts") or {}
    us_ = update_line(rec, NR.disposition_units(CHECKPOINT_DIR / f"dispositions_{NR.run_id()}.jsonl"))   # BUILD IC-8 (D13)
    return {"state": "partly updated" if int(c.get("not reviewed") or 0) else "updated", "summary": us_["line"], "status_problems": us_["problems"],
            "counts": c, "parts": update_parts(e), "why": (f"the reply: {e.get('how')}" if e.get("how") not in (None, "end_turn") else None)}


def _track_end(runner):
    """BUILD IC-6 (D7, I6): checkpoints/track_status.json at the Oracle's end — "failed" (the stage that failed) or "built", the run id its
    page prints, and in Block 2 its update. Never raises."""
    try:
        page, b = NR.page_run_of(OUTPUT_DIR / "index.html"), 2 if is_block2() else 1
        if runner.failed:            # BUILD IC-7 (D7, W8): the failure's plain words, its class in error_class — no stage's code name
            exc_ = getattr(runner, "failed_exc", None)
            NR.write_track_status(CHECKPOINT_DIR, "oracle", "failed", why=NR.plain_why(exc_) if exc_ is not None else "it stopped with an error",
                                  page_run=page, block=b, error_class=type(exc_).__name__ if exc_ is not None else None,
                                  content=dict(_ORACLE_CONTENT) or None)        # BUILD IC-8 (D3): the page it left, counted
        else:
            NR.write_track_status(CHECKPOINT_DIR, "oracle", "built", update=oracle_update_status() if b == 2 else None, page_run=page, block=b,
                                  content=dict(_ORACLE_CONTENT) or None)        # BUILD IC-8 (D3)
    except Exception as e:  # noqa: BLE001
        NR.say(f"  ⚠ track status: {type(e).__name__}")


# ─────────────────────────────────────────────────────────────────────
# CLI COMMANDS
# ─────────────────────────────────────────────────────────────────────
@app.command()
def run(
    api_url: str = typer.Option(..., help="Base API URL"),
    stage_name: Optional[str] = typer.Option(None, "--stage", help="Run only this stage"),
    force: bool = typer.Option(False, help="Force re-run even if cached"),
    endpoints: Optional[str] = typer.Option(None, help="Comma-separated endpoint paths"),
    curveball_text: Optional[str] = typer.Option(None, "--curveball", help="The curveball announcement (Block 2 only): the update call and the page's 'What changed' section read it; the badge shows its first sentence"),
    budget: Optional[int] = typer.Option(None, "--budget", help="Override contract value (e.g., 350000 for $350K budget reduction)"),
    architect_url: Optional[str] = typer.Option(None, "--architect-url", help="Architect Netlify URL for ZIP deep links (overrides ARCHITECT_URL env var)"),
    curveball_full: bool = typer.Option(False, "--curveball-full", help="P0.7 (D3): in Block 2, regenerate the analysis instead of updating Block 1's"),
):
    """Run the full pipeline or a single stage."""
    os.environ["API_URL"] = api_url
    if endpoints:
        os.environ["API_ENDPOINTS"] = endpoints
    # Architect URL: CLI arg takes priority over env var
    if architect_url:
        os.environ["ARCHITECT_URL"] = architect_url
    # Curveball: only set when explicitly passed via CLI — prevents phantom badges from stale env vars
    if curveball_text:
        os.environ["CURVEBALL_TEXT"] = REG.admit(curveball_text, "data", "announcement")   # BUILD IC-5 (D1)    # BUILD IC (D1): the announcement loses private-use characters
    else:
        os.environ.pop("CURVEBALL_TEXT", None)  # Clear any stale env var
    # Budget override: for curveball scenarios that reduce the contract value
    if budget:
        os.environ["CONTRACT_OVERRIDE"] = str(budget)
    else:
        os.environ.pop("CONTRACT_OVERRIDE", None)
    if curveball_full and curveball_text:   # P0.7 (D3)
        os.environ["CURVEBALL_FULL"] = "1"
    else:
        os.environ.pop("CURVEBALL_FULL", None)

    console.print(Panel("[bold cyan]NAIPC Oracle Track Harness[/bold cyan]", subtitle="Ctrl Alt Defeat"))
    NR.write_track_status(CHECKPOINT_DIR, "oracle", "started", block=2 if is_block2() else 1)     # BUILD IC-6 (D7)
    runner = PipelineRunner()

    if stage_name:
        runner.run_all(only=stage_name, force_stages={stage_name})
    elif force:
        runner.run_all(force_stages=set(_STAGE_REGISTRY.keys()))
    else:
        runner.run_all()
    _block_end(runner)   # P0.7 (D3): Block 1's base / Block 2's curveball record
    _track_end(runner)   # BUILD IC-6 (D7): this run's status — built (Block 2: its update) or failed

    # Print summary
    console.print("\n[bold]Pipeline complete.[/bold]")
    html_path = OUTPUT_DIR / "index.html"
    if html_path.exists():
        console.print(f"  Output: {html_path} ({html_path.stat().st_size/1024:.1f} KB)")
        # Auto-open browser to local preview
        import webbrowser
        webbrowser.open(html_path.resolve().as_uri())

    # P0.3 (D7): the coverage gate reads the page as written; it prints, writes coverage\coverage.json, and never raises
    dl = runner.results.get("data_loading") or {}
    NR.gate("oracle", html_path, Path(__file__).resolve().parent / "coverage",
            fetch_errors=[s.get("endpoint") for s in dl.get("summary", []) if isinstance(s, dict) and "error" in s])

    if runner.failed:
        console.print(f"[red bold]Stage '{runner.failed}' failed: exiting with code 1 (see the error above).[/red bold]")
        raise typer.Exit(1)


@app.command()
def curveball(
    api_url: str = typer.Option(..., help="Base API URL"),
    endpoints: Optional[str] = typer.Option(None, help="Comma-separated endpoint paths"),
    curveball_text: Optional[str] = typer.Option(None, "--curveball", help="The announcement's text"),
    curveball_file: Optional[str] = typer.Option(None, "--curveball-file", help="The announcement from a file: a <challenge-id>.curveball.json sidecar or a text file"),
    budget: Optional[int] = typer.Option(None, "--budget", help="Override contract value (whole dollars)"),
):
    """P0.7 (D3): the Block 2 path on its own (standalone use — the pipeline runs `run --force --curveball`): Block 1's base data
    (checkpoints/block1/data_loading.json) is the old data and a fresh fetch the new; the same change list, update call and page.
    With no Block 1 base it says so and exits 1."""
    os.environ["API_URL"] = api_url
    if endpoints:
        os.environ["API_ENDPOINTS"] = endpoints
    console.print(Panel("[bold yellow]CURVEBALL HANDLER[/bold yellow]"))
    NR.write_track_status(CHECKPOINT_DIR, "oracle", "started", block=2)       # BUILD IC-6 (D7)
    doc = _brief_contract()
    chal = doc["challenge"]["id"] if doc else (os.environ.get("NAIPC_CHALLENGE", "").strip() or None)
    if not chal:   # review F12: on its own (no contract, no pipeline environment) — the Block 1 base names its own challenge
        m0, _w0 = REG.admit(NR.read_json(CHECKPOINT_DIR / NR.BLOCK1_DIR / NR.BLOCK1_META), "saved", NR.BLOCK1_META)   # BUILD IC-5 (D1)
        chal = m0.get("challenge") if isinstance(m0, dict) and isinstance(m0.get("challenge"), str) and m0.get("challenge") else None
        if chal:
            NR.say(f"  Challenge: {chal} (named by the Block 1 base — no contract given)")
    if curveball_text and curveball_file:
        console.print("[red]--curveball and --curveball-file together — give one.[/red]")
        raise typer.Exit(1)
    announced = None
    if curveball_file:
        cv, note, problems = REG.admit(NR.curveball_input(curveball_file, doc), "data", "announcement file")   # BUILD IC-5 (D1)
        if problems:
            console.print(f"[red]curveball file: {len(problems)} problem(s)[/red]")
            for pr in problems:
                NR.say(f"  {pr}")
            raise typer.Exit(1)
        NR.say(f"  Curveball file: {note}")
        if Path(curveball_file).suffix.lower() == ".json":
            announced = list(cv.get("api_changed") or [])
    elif curveball_text:
        cv = {"kind": "VERBATIM", "source": "--curveball", "announced_at": None, "text": curveball_text, "tests": [], "api_changed": []}
    else:
        console.print('[red]An announcement is required: --curveball "<text>" or --curveball-file <file>.[/red]')
        raise typer.Exit(1)
    old_raw, meta, why = REG.admit(NR.block1_data(CHECKPOINT_DIR, chal), "data", "block1 data")   # BUILD IC-5 (D1)
    if old_raw is None:
        console.print(f"[red]No Block 1 base ({why}) — run 'run' first (Block 1).[/red]")   # BUILD IC-7 (D7): its handler writes the not-built page
        raise typer.Exit(1)
    os.environ["NAIPC_RUN_ID"] = NR.run_id()
    if chal:
        os.environ["NAIPC_CHALLENGE"] = chal
    os.environ["CURVEBALL_TEXT"] = cv["text"]
    os.environ.pop("CURVEBALL_FULL", None)
    if budget:
        os.environ["CONTRACT_OVERRIDE"] = str(budget)
    else:
        os.environ.pop("CONTRACT_OVERRIDE", None)

    runner = PipelineRunner()
    runner.run_all(only="data_loading", force_stages={"data_loading"})
    if runner.failed:
        console.print(f"[red bold]Stage '{runner.failed}' failed.[/red bold]")
        raise typer.Exit(1)
    dl = runner.results.get("data_loading", {})
    names = [s.get("endpoint") for s in dl.get("summary", []) if isinstance(s, dict)]
    entries = NR.diff_snapshots(old_raw, dl.get("raw", {}), names)
    changes = NR.make_changes(chal, NR.run_id(), meta.get("run_id"), {"path": str(CHECKPOINT_DIR / NR.BLOCK1_DIR / "data_loading.json"),
                              "source": "the Oracle's Block 1 data"}, cv, entries, NR.contract_curveball_flags(doc), announced)
    for line in changes["summary"]:
        NR.say(line)
    cpath = CHECKPOINT_DIR / f"changes_{NR.run_id()}.json"
    why = NR.write_changes(cpath, changes)
    if why:
        NR.say(f"  ⚠ change list not written ({why}) — the update sees the announcement only")
    else:
        os.environ["NAIPC_CHANGES"] = str(cpath.resolve())

    console.print("\n[cyan]Updating the analysis for the curveball...[/cyan]")
    runner.run_all(force_stages={"master_analysis", "financial_model", "diagram_generation", "prioritization", "html_assembly", "deployment"})
    _block_end(runner)
    _track_end(runner)          # BUILD IC-6 (D7)

    # Auto-open browser to local preview
    html_path = OUTPUT_DIR / "index.html"
    if html_path.exists():
        import webbrowser
        webbrowser.open(html_path.resolve().as_uri())

    if runner.failed:
        console.print(f"[red bold]Stage '{runner.failed}' failed: exiting with code 1 (see the error above).[/red bold]")
        raise typer.Exit(1)


@app.command()
def deploy():
    """Deploy the latest HTML to Netlify."""
    runner = PipelineRunner()
    runner.run_all(only="deployment", force_stages={"deployment"})


if __name__ == "__main__":
    NR.paths_home(CHECKPOINT_DIR.resolve(), "oracle")       # BUILD IC-8 (D14, I8): this run's path markers
    try:
        app()
    except BaseException as _e:     # BUILD IC-6 (D7): the top-level handler — "failed" in the status file (best effort)
        NR.track_failed(CHECKPOINT_DIR, "oracle", _e)
        raise
