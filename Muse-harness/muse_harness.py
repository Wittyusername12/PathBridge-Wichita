#!/usr/bin/env python3
"""
Muse Track Harness — NAIPC Competition
Generates a Marketing Playbook (single HTML page) from the challenge API data using Claude.

Usage:
    python muse_harness.py run \
        --api-url https://aipromptchamp.com/api/challenge/ \
        --endpoints pantries,transit,demographics,store-closures,... \
        --oracle-dir ./checkpoints \
        --architect-url https://ctrl-alt-defeat-arch.netlify.app \
        --api-key sk-ant-...

The harness makes two Claude API calls:
  Call 1 (Strategic): personas, channel plan, calendar, KPIs
  Call 2 (Creative): social posts, flyer copy, partner pitch (with personas from Call 1 injected)

Oracle checkpoint JSONs (master_analysis.json, financial_model.json) are consumed
for cross-track consistency if --oracle-dir is provided. Without them, the harness
derives everything from the API data independently.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Fix Windows encoding: force UTF-8 so Rich and Unicode chars work in redirected output
if sys.platform == "win32":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field, model_validator, field_validator
from pydantic import ValidationError   # P0.7 r2 (E3 a): a check's errors read as plain dicts (NR.plain_reason)

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import naipc_requirements as NR  # noqa: E402 — P0.3: the brief, the coverage map, the page attributes and the gate
import naipc_registry as REG  # noqa: E402 — BUILD B3 (D2): the Oracle's figures by its registry (one byte-identical copy per folder)
REG.admit_readers(NR)   # BUILD IC-5 (D1 e): the contract and the coverage map, wherever this process reads them
REG.set_admit_rewrite(NR.ic5_prompt())   # BUILD IC-5 (D1 f): the rewrite only with this build's prompt switch on
NR.safe_stdio()   # P0.6 red team RT-3: printing that cannot raise on any platform (the block above covers Windows only)

# ── Optional Dependencies (graceful degradation) ─────────────────────────────
try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from rich.console import Console
    console = Console(force_terminal=True)  # Avoid legacy Windows renderer
except ImportError:
    class _FallbackConsole:
        def print(self, *a, **kw):
            text = str(a[0]) if a else ""
            text = re.sub(r'\[.*?\]', '', text)
            print(text, **{k: v for k, v in kw.items() if k != 'style'})
    console = _FallbackConsole()


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════
HARNESS_DIR = Path(__file__).parent
OUTPUT_DIR = HARNESS_DIR / "output"
MODEL = "claude-opus-5-5"  # the only model constant (P0.11 D7): marketing copy + Spanish translations
MAX_RETRIES = 2  # attempts per call, including one re-ask for output that does not validate (P0.11 D5)
FETCH_ERRORS: List[str] = []  # P0.3 (finding 5): endpoints whose fetch failed this run, reported by the coverage gate


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic Models — Structured Output Schema
# Every field has description= for Claude's benefit when generating JSON
# ═══════════════════════════════════════════════════════════════════════════════

class Persona(BaseModel):
    emoji: str = Field(description="Single emoji representing this persona")
    name: str = Field(description="Realistic first and last name appropriate to demographics")
    age: int = Field(description="A whole number of years", ge=0, le=120)     # BUILD IC-7 (Q-AGE): 0–120 (18–80 had no stated source)
    occupation: str = Field(description="Job title or life situation")
    location: str = Field(description="ZIP code and city/neighborhood")
    barriers: List[str] = Field(description="3-5 access barriers this person faces")
    channels: List[str] = Field(description="2-4 communication channels that reach this person")
    quote: str = Field(description="First-person quote expressing their frustration or need")
    data_evidence: List[Dict[str, str]] = Field(default_factory=list, description="3-4 specific data points from the API grounding this persona, each with 'stat' (formatted number) and 'label' (what it measures + source endpoint)")


class ChannelPlan(BaseModel):
    channel: str = Field(description="Communication channel name")
    audience: str = Field(description="Which persona(s) this reaches")
    content_type: str = Field(description="Type of content for this channel")
    frequency: str = Field(description="How often content is published")
    kpi: str = Field(description="Key performance indicator for this channel")
    reach: str = Field(description="Estimated monthly reach, e.g. '15,000/mo'")

    @field_validator('reach', mode='before')
    @classmethod
    def _coerce_reach_to_string(cls, v):
        """Claude occasionally returns reach as int (15000) instead of str ('15,000/mo').
        Coerce any numeric value to its string representation before type validation."""
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return str(v)
        return v


class CalendarPhase(BaseModel):
    phase_name: str = Field(description="Phase name, e.g. 'Partner Onboarding'")
    month_label: str = Field(description="Time range, e.g. 'Weeks 1-4'")
    milestones: List[str] = Field(description="4-6 specific milestones")
    deliverables: List[str] = Field(default_factory=list, description="2-3 deliverables produced")


class SocialPost(BaseModel):
    """Social media post mockup. Has a model_validator to handle LLM returning
    null for optional-feeling fields like image_headline (not every post has images)."""
    platform: str = Field(description="Platform label: 'Photo-Sharing Platform', 'Microblog Platform', or 'Community Feed'")
    card_class: str = Field(description="CSS class: 'photo', 'micro', or 'feed'")
    location: str = Field(default="", description="City/region shown in post header")
    language: str = Field(default="English", description="'English', 'Bilingual EN/ES', or 'Spanish'")
    image_headline: str = Field(default="", description="Bold headline overlay on image area, e.g. 'FOUND CARE IN 48 HOURS'")
    image_desc: str = Field(default="", description="Short 3-6 word caption below headline, e.g. 'Austin Community Health Center'. NOT a photo description — this text is displayed visually.")
    text: str = Field(description="Main post body text with emoji")
    hashtags: List[str] = Field(default_factory=list, description="3-5 hashtags including program name")
    likes: str = Field(default="0", description="Simulated like count")
    comments: str = Field(default="0", description="Simulated comment count")
    shares: str = Field(default="0", description="Simulated share count")

    @model_validator(mode="before")
    @classmethod
    def coerce_nulls(cls, data):
        """LLMs return null for 'not applicable' fields. Coerce to safe defaults."""
        if isinstance(data, dict):
            # Convert None → "" for all string-typed fields
            str_fields = ["platform", "card_class", "location", "language",
                          "image_headline", "image_desc", "text", "likes", "comments", "shares"]
            for f in str_fields:
                if f in data and data[f] is None:
                    data[f] = ""
            # Ensure hashtags is a list (LLM might return None or omit it)
            if "hashtags" not in data or data["hashtags"] is None:
                data["hashtags"] = []
            # Ensure hashtags entries are strings (LLM might include None in the list)
            if isinstance(data.get("hashtags"), list):
                data["hashtags"] = [str(h) for h in data["hashtags"] if h is not None]
        return data


class FlyerContent(BaseModel):
    headline_en: str = Field(description="English headline, e.g. 'Need Food? We Can Help.'")
    headline_es: str = Field(description="Spanish translation of headline")
    subheadline_en: str = Field(default="", description="English subheadline with key value prop")
    subheadline_es: str = Field(default="", description="Spanish translation of subheadline")
    body_en: str = Field(description="2-3 sentence English body copy")
    body_es: str = Field(description="Spanish translation of body")
    bullets_en: List[str] = Field(default_factory=list, description="3-5 English bullet points of key services")
    bullets_es: List[str] = Field(default_factory=list, description="Spanish translations of bullets")
    cta_en: str = Field(default="", description="English call to action")
    cta_es: str = Field(default="", description="Spanish translation of CTA")
    contact_info: str = Field(default="", description="Contact line")

    @model_validator(mode="before")
    @classmethod
    def coerce_nulls(cls, data):
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if v is None and isinstance(k, str):
                    data[k] = "" if not k.startswith("bullets") else []
        return data


class DataPoint(BaseModel):
    stat: str = Field(description="Formatted statistic, e.g. '47,300' or '$500K'")
    label: str = Field(description="What the stat measures, e.g. 'Residents Displaced'")

    @model_validator(mode="before")
    @classmethod
    def coerce_nulls(cls, data):
        if isinstance(data, dict):
            for k in ["stat", "label"]:
                if data.get(k) is None:
                    data[k] = ""
        return data


class PartnerPitch(BaseModel):
    headline: str = Field(description="Partner recruitment headline")
    problem: str = Field(description="2-3 sentence problem statement for partners")
    solution: str = Field(description="2-3 sentence solution description")
    benefits: List[str] = Field(default_factory=list, description="5-7 specific partnership benefits")
    data_points: List[DataPoint] = Field(default_factory=list, description="3-5 key statistics")

    @model_validator(mode="before")
    @classmethod
    def coerce_nulls(cls, data):
        if isinstance(data, dict):
            for k in ["headline", "problem", "solution"]:
                if data.get(k) is None:
                    data[k] = ""
            for k in ["benefits", "data_points"]:
                if data.get(k) is None:
                    data[k] = []
        return data


class KPI(BaseModel):
    emoji: str = Field(description="Single emoji icon for this metric")
    target: str = Field(description="Target value, e.g. '100K' or '12,000'")
    metric: str = Field(description="Metric name, e.g. 'Campaign Impressions'")
    timeframe: str = Field(description="When, e.g. 'Year 1' or 'Month 3'")

    @model_validator(mode="before")
    @classmethod
    def coerce_nulls(cls, data):
        if isinstance(data, dict):
            for k in ["emoji", "target", "metric", "timeframe"]:
                if data.get(k) is None:
                    data[k] = ""
        return data


class MuseStrategic(BaseModel):
    """Call 1 output: strategic layer."""
    # BUILD IC-7 (Q-LIST, D2.5): each list's minimum is 1 — a list short of the count the request asks for keeps what passed, the
    # shortfall named in the admission record (LIST_ASKED); IC-6's minimums (3, 4, 3, 4) stay the update's shown bounds with the switch off
    personas: List[Persona] = Field(min_length=1, max_length=5)
    channel_plan: List[ChannelPlan] = Field(min_length=1, max_length=8)
    calendar: List[CalendarPhase] = Field(min_length=1, max_length=4)
    kpis: List[KPI] = Field(min_length=1, max_length=8)
    hero_title: str = Field(description="Campaign hero headline with <em> tags on key numbers")
    hero_lead: str = Field(description="1-2 sentence campaign summary")
    hero_stats: List[Dict[str, str]] = Field(description="3-4 stats with 'value' and 'label' keys")
    brand_handle: str = Field(description="Social media handle, e.g. '@FoodBridgeKC'")
    persona_text: str = Field(description="One sentence describing who the campaign serves")
    # NEW: distinct slots for header tagline and social preview (research-validated architecture)
    # BUILD IC-8 (D1, I2): stored whole — no bound, no validator that cuts or writes one. The page fits each once, on its shown text
    # (build_context: REG.fit_shown, DISPLAY_POLICY "muse.short_tagline" and "muse.og_description"); None stays None until that chain.
    # Old checkpoints still validate: the model only lost bounds. The update's shown schema keeps IC-7's with the switch off (F12).
    short_tagline: Optional[str] = Field(
        default=None,
        description="Timeless brand promise, 5-12 words, max 80 chars"
    )
    og_description: Optional[str] = Field(
        default=None,
        description="Social media preview hook, max 150 chars, front-loaded value"
    )


class MuseCreative(BaseModel):
    """Call 2 output: creative layer."""
    social_posts: List[SocialPost] = Field(min_length=1, max_length=6)       # BUILD IC-7 (Q-LIST): 1 (IC-6: 3)
    flyer: FlyerContent
    partner_pitch: PartnerPitch



# ═══════════════════════════════════════════════════════════════════════════════
# BUILD IC-7 (D1, D2, D5; I1, I2) — one contract per call. Each holds the reply's form and page model, its wire (Calls 1 and 2), its
# declared admission rules by path (D2: read against the recorded replies, tests/t_corpus_ic7.py), the paths a request states as
# number-typed, the lists a count may name, and the view the statement check reads (NR.statement_problems). The page models stay as
# they are read (templates, chat context, muse_block1.json, the update); the wire is Python's own (NR.wire_schema), never the SDK's.
# ═══════════════════════════════════════════════════════════════════════════════
IC6_SHOWN_MINS = {"personas": 3, "channel_plan": 4, "calendar": 3, "kpis": 4, "social_posts": 3}    # IC-6's page bounds: the update's
IC6_SHOWN_AGE = (18, 80, "Age between 18-80")                                                     # shown schema with the switch off
LIST_ASKED = {"personas": 3, "channel_plan": 5, "calendar": 3, "kpis": 6, "hero_stats": 3, "social_posts": 3}   # the counts Calls 1, 2 ask
ENGAGEMENT = ("social_posts[].likes", "social_posts[].comments", "social_posts[].shares")     # not asked with the parts (Q-REQ, G6)
FREE_KEYS_1 = {"personas[].data_evidence[]": ["stat", "label"], "hero_stats[]": ["value", "label"]}   # the page reads these keys only
TARGET_FORM = "<a token, as <number_rules> says: a figure of the data by its id, or a number of your own as [[model:<number>|<what it is>]]>"
TARGET_FORM_NO_PARTS = "<a number with its unit, as text>"
_SP_TEXT = ("platform", "card_class", "location", "language", "image_headline", "image_desc", "text", "likes", "comments", "shares")
_FL_TEXT = ("headline_en", "headline_es", "subheadline_en", "subheadline_es", "body_en", "body_es", "cta_en", "cta_es", "contact_info")
MUSE_RULES = {
    # the models' own before-validators, now counted (muse_harness.py: ChannelPlan reach, KPI, SocialPost, FlyerContent, DataPoint,
    # PartnerPitch); the display-number paths of Q-FIG; the taglines' soft truncation; the free keys the page reads
    "muse/call1": {"display_numbers": ["kpis[].target", "channel_plan[].reach", "personas[].data_evidence[].stat", "hero_stats[].value"],
                   "null_text": ["channel_plan[].reach", "kpis[].emoji", "kpis[].target", "kpis[].metric", "kpis[].timeframe"],
                   "missing_text": ["kpis[].emoji", "kpis[].target", "kpis[].metric", "kpis[].timeframe"],
                   "null_list": [], "null_default": ["short_tagline", "og_description"],
                   "cut": {}, "objects": FREE_KEYS_1, "text_items": []},      # BUILD IC-8 (D1, F1): the admission cuts nothing
    "muse/call2": {"display_numbers": ["partner_pitch.data_points[].stat"],
                   "null_text": ([f"social_posts[].{f}" for f in _SP_TEXT] + [f"flyer.{f}" for f in _FL_TEXT]
                                 + ["partner_pitch.headline", "partner_pitch.problem", "partner_pitch.solution",
                                    "partner_pitch.data_points[].stat", "partner_pitch.data_points[].label"]),
                   "missing_text": ["partner_pitch.headline", "partner_pitch.problem", "partner_pitch.solution", "partner_pitch.benefits",
                                    "partner_pitch.data_points", "partner_pitch.data_points[].stat", "partner_pitch.data_points[].label"],
                   "null_list": ["social_posts[].hashtags", "flyer.bullets_en", "flyer.bullets_es", "partner_pitch.benefits",
                                 "partner_pitch.data_points"],
                   "null_default": [], "cut": {}, "objects": {}, "text_items": ["social_posts[].hashtags[]"]},
}


def _wire_desc(key, parts):
    """The wire's form-only descriptions (D5): each figure field's token form, the age's unit — no example value, no number."""
    tok = "a token, as the number rules say" if parts else "a number from the data, as text"
    if key == "muse/call1":
        return {"kpis[].target": ("the target as text: a token, as the number rules say — a figure of the data by its id, or a number of "
                                  "your own" if parts else "the target as text, a number with its unit"),
                "personas[].age": "a whole number of years",
                "personas[].data_evidence[].stat": tok + ", for a figure of the data",
                "hero_stats[].value": tok + ", for a figure", "channel_plan[].reach": "the estimated reach, as text"}
    return {"partner_pitch.data_points[].stat": tok + ", for a figure of the data"}


def muse_wire(key, brief=False, parts=True):
    """BUILD IC-7 (D5): the strict wire of Call 1 or Call 2 — every property required, unasked fields off (Q-REQ: the engagement counts
    with the parts), the free-key lists as explicit objects, brief_answers [{id, answer}] on Call 1's wire whenever its brief block is
    sent; no bound (Python keeps every one)."""
    if key == "muse/call1":
        return NR.wire_schema(MuseStrategic.model_json_schema(), objects=FREE_KEYS_1, descriptions=_wire_desc(key, parts),
                              add={"brief_answers": NR.BRIEF_REPAIR_SCHEMA["properties"]["brief_answers"]} if brief else None)
    if key == "muse/call2":
        return NR.wire_schema(MuseCreative.model_json_schema(), drop=ENGAGEMENT if parts else (), descriptions=_wire_desc(key, parts))
    raise KeyError(key)


def page_to_wire(key, page, parts=True):
    """BUILD IC-7 (D5): a page model's dump as its wire value (the round trip's first half): the asked fields only, an absent tagline as
    text, a free-key item's page keys only."""
    w = muse_wire(key, parts=parts)

    def take(v, s):
        t = s.get("type")
        if t == "object" and isinstance(v, dict):
            return {k: take(v.get(k), sub) for k, sub in s["properties"].items()}
        if t == "array" and isinstance(v, list):
            return [take(x, s["items"]) for x in v]
        if t == "string":
            return "" if v is None else v
        return v
    return take(page, w)


def wire_to_page(key, wire):
    """BUILD IC-7 (D5): a wire reply as the page models take it — brief_answers set aside (read outside the schema, as before), the
    explicit {stat, label} and {value, label} objects as the page's dicts (the same JSON), the unasked fields left to Python's
    defaults. Nearly the identity, by construction of the wire."""
    out = dict(wire) if isinstance(wire, dict) else wire
    if isinstance(out, dict):
        out.pop("brief_answers", None)
    return out


def _strict(model):
    """A strict validator for NR.admit: (obj, None) or (None, [{loc, type}]) — never the input (include_input=False)."""
    def v(x):
        try:
            return model.model_validate(x, strict=True), None
        except ValidationError as e:
            return None, [{"loc": list(er.get("loc") or ()), "type": er.get("type")} for er in e.errors(include_url=False, include_input=False)]
    return v


def _paths_of(schema, want):
    """The paths of a page schema whose leaf type is in `want` ("personas[].age")."""
    defs, out = schema.get("$defs") or {}, []

    def walk(n, path, d=0):
        n = NR._unoptional(n, defs)
        t = n.get("type")
        if t == "object" and n.get("properties"):
            for k, v in n["properties"].items():
                walk(v, f"{path}.{k}" if path else k, d + 1)
        elif t == "array":
            walk(n.get("items") or {}, f"{path}[]", d + 1)
        elif t in want:
            out.append(path)
    walk(schema, "")
    return out


def _bounds_of(schema):
    defs, out = schema.get("$defs") or {}, {}

    def walk(n, path):
        n = NR._unoptional(n, defs)
        if n.get("type") == "object":
            for k, v in (n.get("properties") or {}).items():
                walk(v, f"{path}.{k}" if path else k)
        elif n.get("type") == "array":
            out[path] = (n.get("minItems", 0), n.get("maxItems"))
            walk(n.get("items") or {}, f"{path}[]")
    walk(schema, "")
    return out


def _view(key, schema_word):
    """The statement check's view of a Muse call (NR.statement_problems)."""
    ms = {"muse/call1": [MuseStrategic], "muse/call2": [MuseCreative], "muse/update": [MuseStrategic, MuseCreative]}[key]
    nums, texts, lists = [], [], {}
    for m in ms:
        s = m.model_json_schema()
        nums += _paths_of(s, ("integer", "number"))
        texts += _paths_of(s, ("string",))
        lists.update(_bounds_of(s))
    rv = CONTRACTS[key]["rules_view"]
    return {"call": key, "schema": schema_word, "number_paths": nums, "text_paths": texts, "count_lists": list(rv["count_lists"]),
            "name_fields": [], "lists": lists, "typed_words": "fields"}


CONTRACTS = {
    "muse/call1": {"form": "schema", "page": MuseStrategic, "wire": lambda brief=False, parts=True: muse_wire("muse/call1", brief, parts),
                   "rules": MUSE_RULES["muse/call1"], "forms": {"kpis[].target": "a token for a figure or your own number"},
                   "essential": "the required fields of MuseStrategic and its items (an item's own: the item is left out)",
                   "rules_view": {"count_lists": ["personas", "channel_plan", "calendar", "kpis"], "number_fields": ["a persona's age"],
                                  "schema": "sent"},
                   "view": lambda sent=True: _view("muse/call1", "sent" if sent else None), "numbers": ()},
    "muse/call2": {"form": "schema", "page": MuseCreative, "wire": lambda parts=True: muse_wire("muse/call2", False, parts),
                   "rules": MUSE_RULES["muse/call2"], "not_asked_with_parts": list(ENGAGEMENT),
                   "essential": "the required fields of MuseCreative and its items (an item's own: the item is left out)",
                   "rules_view": {"count_lists": ["social_posts"], "number_fields": [], "schema": "sent"},
                   "view": lambda sent=True: _view("muse/call2", "sent" if sent else None), "numbers": ()},
    "muse/update": {"form": "lines", "page": None, "shown": "the page models' schema (NR.schema_block: no annotation)",
                    "rules": "each line's value: Calls 1 and 2's display numbers, nulls and missing keys (no cut, no free-key drop: the "
                             "update's own checks name those); then its item's model, strict; a line that fails is not applied, named",
                    "rules_view": {"count_lists": ["personas", "channel_plan", "calendar", "kpis", "social_posts"],
                                   "number_fields": ["a persona's age"], "schema": "shown"},
                    "view": lambda: _view("muse/update", "shown")},
    "muse/repair": {"form": "schema", "page": None, "wire": lambda: NR.BRIEF_REPAIR_SCHEMA,
                    "rules": "the brief's answers: each {id, answer} checked by id (muse_brief_answers); an answer that fails is left out",
                    "rules_view": None, "view": lambda: {"call": "muse/repair", "schema": "sent", "number_paths": [], "text_paths": [],
                                                         "count_lists": [], "name_fields": [], "lists": {}, "typed_words": "fields"}},
}


def _ic6_bounds(schema):
    """The page schema with IC-6's bounds (the update's shown schema with the switch off: IC-6's request, byte for byte)."""
    s = json.loads(json.dumps(schema))
    for k, n in IC6_SHOWN_MINS.items():
        if k in (s.get("properties") or {}):
            s["properties"][k]["minItems"] = n
    per = ((s.get("$defs") or {}).get("Persona") or {}).get("properties", {}).get("age")
    if isinstance(per, dict):
        per["minimum"], per["maximum"], per["description"] = IC6_SHOWN_AGE
    return s


def shown_schema(model):
    """BUILD IC-7 (D6 as found, I7): the schema the update shows — the page model's own with the switch on; IC-6's bounds with it off."""
    s = model.model_json_schema()
    if model is MuseStrategic and not NR.ic8_prompt():      # BUILD IC-8 (D1, F12): IC-7's tagline bounds, frozen
        s["properties"].update(json.loads(json.dumps(MUSE_TAGLINE_SCHEMA_IC7)))
    return s if NR.ic7_prompt() else _ic6_bounds(s)


_LAST_DECODE: List[dict] = []          # the last reply's decoding findings (a key given twice), for its admission entries


def admit_reply(key, data, attempt=1, decode=(), parts=True):
    """BUILD IC-7 (D2, I2, I3): one reply of Call 1 or Call 2 → (the page model or None, entries, essential errors): the declared
    conversions, strict validation item by item, each outcome named (NR.admit); a list short of the count its request asks for keeps
    what passed and is named; a key the reply gave twice is named. The console's line and checkpoints/admission_<run id>.jsonl say it
    (paths and rules, never a value)."""
    c = CONTRACTS[key]
    rules = dict(c["rules"], not_asked=c.get("not_asked_with_parts", ()) if parts else ())
    obj, entries, errs = NR.admit(data, c["page"].model_json_schema(), _strict(c["page"]), rules, c.get("forms"))
    entries = [{"path": "(the reply)", "rule": "key twice (the last kept)", "seen": "object"} for _ in decode or ()] + entries
    if obj is not None:
        for lst, n in LIST_ASKED.items():
            got = getattr(obj, lst, None)
            if isinstance(got, list) and len(got) < n:
                entries.append({"path": lst, "rule": "list short of the asked count", "seen": "array", "short": n - len(got)})
    outcome = (("admitted" if not entries else "admitted, named") if obj is not None
               else "to a second attempt" if attempt == 1 else "not admitted")
    NR.admission_record(HARNESS_DIR / "checkpoints", key, entries, outcome, attempt)
    NR.say(NR.admission_line(key, entries, outcome))
    return obj, entries, errs


# the update's values (D2: "plus the update lines' values on the same paths")
_UPDATE_ENTRIES: List[dict] = []
_KIND_FIELD = {k: f for f, (k, _l) in REG.MUSE_ITEM_KINDS.items()}
_KIND_FIELD["crisis_post"] = "creative.social_posts"


def update_walk(field, value, item=False):
    """BUILD IC-7 (D2): an update value's declared conversions on Calls 1 and 2's paths — display numbers, nulls and missing keys that
    the models filled — counted in _UPDATE_ENTRIES; extra keys kept (the update names them), no cut: neither a tagline's (the update
    refuses an over-long tagline) nor a list's over its maximum (BUILD IC-7 Phase 4: Q-LIST's cut was applied here, silently, in the
    object form — the update's check refuses such a part, its reason named, and Block 1's version stands, as IC-6's did). `field`
    "strategic.<f>" or "creative.<f>"; `item`: the value is one item of that list. Never raises."""
    try:
        top, sub = field.split(".", 1)
        key = "muse/call1" if top == "strategic" else "muse/call2"
        model = MuseStrategic if top == "strategic" else MuseCreative
        r = MUSE_RULES[key]
        sch = model.model_json_schema()
        one = {"type": "object", "properties": {sub: sch["properties"][sub]}, "$defs": sch.get("$defs") or {}}
        v, ents, _f = NR.admit_walk({sub: [value] if item else value}, one, extra="keep", max_cut=False,
                                    **{k: r[k] for k in ("display_numbers", "null_text", "null_list", "missing_text")})
        _UPDATE_ENTRIES.extend(dict(e, path=f"{top}.{e['path']}") for e in ents)
        out = v.get(sub, value) if isinstance(v, dict) else value
        return (out[0] if isinstance(out, list) and out else value) if item else out
    except Exception:  # noqa: BLE001 — the walk never decides; the value's own check does
        return value




# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Data Fetching (shared pattern with Architect)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_endpoints(api_url: str, endpoints: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Fetch all API endpoints and return {endpoint_name: parsed_json}."""
    results = {}
    for ep in endpoints:
        url = NR.endpoint_url(api_url, ep)  # P0.6 (D8): the one URL builder (<ep>, or <ep>/ when NAIPC_URL_SLASH=1)
        console.print(f"  [cyan]→ Fetching {url}[/cyan]")
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            results[ep] = r.json()
            count = len(NR.records(results[ep]))   # P0.7 (D5): the one envelope rule — an alerts list or a flat object is counted, never "?"
            console.print(f"    [green]✓ {ep}: {count} records[/green]")
        except Exception as e:
            NR.say(f"    ✗ {ep}: {e}")          # BUILD IC-7 (D8): plain — rich markup ate "[...]" from an error's text
            results[ep] = {"source": ep, "count": 0, "data": []}
            FETCH_ERRORS.append(ep)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Oracle Checkpoint Loading
# ═══════════════════════════════════════════════════════════════════════════════

def load_oracle_checkpoints(oracle_dir: str) -> Dict[str, Any]:
    """Load Oracle's checkpoint JSONs for cross-track consistency.
    Returns a dict with program_name, domain, city_name, financial metrics, etc."""
    result = {}
    oracle_path = Path(oracle_dir)

    # Load master_analysis.json
    ma_path = oracle_path / "master_analysis.json"
    if ma_path.exists():
        with open(ma_path, encoding="utf-8-sig") as f:
            ma = REG.admit(json.load(f), "saved", ma_path.name)   # BUILD IC-5 (D1): a file a run wrote
        data = ma.get("data", ma)
        result["program_name"] = data.get("program_name", "")
        result["brand_html"] = data.get("brand_html", "")
        result["domain"] = data.get("domain", "")
        result["city_name"] = data.get("city_name", "")
        result["hero_title"] = data.get("hero_title", "")
        result["hero_lead"] = data.get("hero_lead", "")
        result["crisis_human"] = data.get("crisis_human", "")
        result["solution_body"] = data.get("solution_body", "")
        result["badge_text"] = data.get("badge_text", "")
        result["domain_label"] = data.get("domain_label") or ""  # P0.6 (D3): the Oracle's domain label — the Muse's badge when set
        # Extract key stats from badge_text
        result["phases"] = data.get("phases", [])
        result["risks"] = data.get("risks", [])
        # BUILD B3 (D2): the Oracle's text holds [[id]] tokens — each becomes the Oracle's registry figure; with no registry of this run,
        # a sentence holding one is left out (named here), so no token reaches this page or the Muse's prompt
        reg, rwhy = REG.read_oracle(oracle_path, NR.facts(_BRIEF["doc"]).get("challenge"))
        result["_reg"] = reg
        result["_reg_base"] = set(reg.entries) if reg is not None else None    # IC-4 Phase 7 (review b1): the Oracle's own figures
        if NR.ic4_prompt():         # BUILD IC-4 (D16): the request parts the Oracle built on that registry — the Muse's requests take them
            result["_parts"], pwhy = (REG.read_track_parts(oracle_path, NR.facts(_BRIEF["doc"]).get("challenge")) if reg is not None
                                      else (None, "no registry of this run"))
            if result["_parts"] is None:
                console.print(f"    [yellow]⚠ the registry's request parts: {pwhy} — the Muse's requests go without them[/yellow]")
            result["_raw_oracle"] = REG.oracle_raw(oracle_path)      # the data the registry was built on: its ids by grammar resolve
        gone = 0
        for k in ("hero_title", "hero_lead", "crisis_human", "solution_body", "badge_text", "phases", "risks"):
            result[k], n = REG.resolve_tree(reg, result.get(k))
            gone += n
        if gone:
            console.print(f"    [yellow]⚠ the Oracle's text: {gone} sentence(s) with a figure left out — no registry of this run ({rwhy})[/yellow]")
        console.print(f"    [green]✓ master_analysis.json: {result['program_name']}[/green]")

    # Load financial_model.json
    fm_path = oracle_path / "financial_model.json"
    if fm_path.exists():
        with open(fm_path, encoding="utf-8-sig") as f:
            fm = REG.admit(json.load(f), "saved", fm_path.name)   # BUILD IC-5 (D1)
        data = fm.get("data", fm)
        kpis = data.get("kpis") if isinstance(data.get("kpis"), dict) else {}
        result["sroi_ratio"] = data.get("sroi_ratio", 0)
        result["cpb_y1"] = data.get("cpb_y1", 0)
        result["beneficiaries"] = data.get("beneficiaries", [])
        result["contract"] = kpis.get("contract")                 # P0.6 (D2): None when the model has no figure — never $0
        result["contract_source"] = kpis.get("contract_source")   # override | brief | proposed | cost_plan (absent before P0.6)
        result["unit_plural"] = kpis.get("unit_plural") or ""
        result["unit_singular"] = kpis.get("unit_singular") or ""
        # BUILD B3 (D1, D2): the SROI only from the Oracle's registry, and only when it is at least 1 (below 1 the Oracle's page labels it)
        reg = result.get("_reg")
        e = reg.get("sroi") if reg is not None else None
        result["sroi_display"] = e["shown"] if e and e["value"] >= 1 else ""
        if e and e["value"] < 1:
            NR.say(f"    financial_model.json: the SROI is {e['shown']} (below 1) — left out of this page; the Oracle's page shows it")   # IC-7 (D8)
        console.print(f"    [green]✓ financial_model.json: SROI {result.get('sroi_display') or 'left out'}[/green]")

    return result


def oracle_budget(oracle_ctx: Dict, F: Dict):
    """P0.6 (D2): (amount, source) of the budget the Oracle's financial model used — the source one of NR.BUDGET_SOURCES; a model
    written before P0.6 (no contract_source) counts as the brief's only when its amount is the brief's. (None, None) with no figure."""
    amt = oracle_ctx.get("contract")
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or amt <= 0:
        return None, None
    src = oracle_ctx.get("contract_source")
    if src not in NR.BUDGET_SOURCES:
        src = "brief" if F.get("contract") is not None and round(amt) == F["contract"] else None
    return int(round(amt)), src


def oracle_business_line(oracle_ctx: Dict, F: Dict) -> Optional[str]:
    """P0.6 (D2): the Muse's Oracle card — the budget with its source ("proposed" when the brief states none), the SROI and the
    3-year plan, from the Oracle's financial model; None without one (the template then names the sections, no figures).
    BUILD B3 (D2): HTML — the budget and the SROI as the Oracle's registry spans (the pipeline checks every page's spans against it),
    every word escaped; without a registry of this run, no figure."""
    import html as _h
    reg = oracle_ctx.get("_reg")
    amt, src = oracle_budget(oracle_ctx, F)
    sroi = oracle_ctx.get("sroi_display") or ""
    be = reg.get("budget") if reg is not None else None
    if not (be and amt) and not sroi:
        return None
    parts = []
    if be and amt:
        m = reg.span(be)
        parts.append({"brief": f"{m} contract", "override": f"{m} budget (override)", "proposed": f"Proposed {m} budget (our estimate)",
                      "cost_plan": f"Proposed {m} budget (= the Year-1 cost plan)"}.get(src, f"{m} budget"))
    if sroi:
        parts.append(f"{reg.span('sroi')} SROI")
    parts.append(_h.escape("3-year plan"))
    return ", ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Domain Detection
# ═══════════════════════════════════════════════════════════════════════════════

def detect_domain(raw_endpoints: Dict[str, Any]) -> Dict[str, str]:
    """Infer the civic domain from endpoint names and data content."""
    ep_names = set(k.lower() for k in raw_endpoints.keys())

    # Food access indicators
    food_kw = {'pantries', 'food-atlas', 'store-closures', 'harvest', 'snap'}
    # Healthcare indicators
    health_kw = {'hospitals', 'clinics', 'er-visits', 'patient-displacement', '311-health'}
    # Workforce indicators
    work_kw = {'employers', 'jobs', 'training', 'workforce', 'unemployment'}

    food_score = len(ep_names & food_kw)
    health_score = len(ep_names & health_kw)
    work_score = len(ep_names & work_kw)

    if not (food_score or health_score or work_score):  # P0.6 (D3): no keyword family matched — never "Food Access" by default
        return {"domain": "Community Services", "label": "Community Services", "crisis_noun": "service gaps",
                "beneficiary_noun": "beneficiaries", "resource_noun": "services"}
    if health_score > food_score and health_score >= work_score:
        return {"domain": "Healthcare Access", "label": "Healthcare", "crisis_noun": "facility closures",
                "beneficiary_noun": "patients", "resource_noun": "clinics and hospitals"}
    elif work_score > food_score:
        return {"domain": "Workforce Development", "label": "Workforce", "crisis_noun": "layoffs",
                "beneficiary_noun": "workers", "resource_noun": "training centers and employers"}
    else:
        return {"domain": "Food Security & Grocery Access", "label": "Food Access", "crisis_noun": "store closures",
                "beneficiary_noun": "families", "resource_noun": "pantries and distribution sites"}


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4: Claude API Calls (Two-Call Strategy)
# ═══════════════════════════════════════════════════════════════════════════════

def _summarize_api_data(raw_endpoints: Dict[str, Any]) -> str:
    """A compact text summary of the API data for Claude's context. P0.7 (D5): every endpoint by the one envelope rule (NR.records:
    a list under data or alerts or the other record keys, a bare list, an object as one record) — its real record count, its first
    three records, and the envelope's own scalar fields (such as an alerts payload's status), so no shape reads as "(0 records)"."""
    parts = []
    for ep_name, payload in raw_endpoints.items():
        recs, env, _rule = NR.envelope_split(payload)
        parts.append(f"Endpoint: {ep_name} ({len(recs)} records)")
        scal = {k: v for k, v in (env or {}).items() if not isinstance(v, (list, dict))}
        if scal:
            parts.append(f"  envelope: {json.dumps(scal, default=str)[:200]}")
        for rec in recs[:3]:
            parts.append(f"  {json.dumps(rec, default=str)[:400]}")
    text = "\n".join(parts)
    return text             # BUILD IC-8 (P4.2): whole — IC-7 cut the summary at 12,000 characters, dropping the later endpoints silently


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


_ATTEMPT_N: Dict[str, int] = {}   # P0.7 (D10): attempts per call label, for the partial-answer file names


def keep_partial(label: str, attempt: int, text: str) -> None:
    """P0.7 (D10): the text a failed call had received → checkpoints/partial_<label>_<UTC stamp>_<attempt>.txt (outside output\\,
    never uploaded); printed; never raises — the caller re-raises the original error."""
    try:
        p, why = NR.save_partial(HARNESS_DIR / "checkpoints", label, attempt, text)
        if p is not None:
            NR.say(f"    partial answer kept: {p} ({len(text):,} characters)")
        elif why:
            NR.say(f"    ⚠ partial answer not kept — {why}")
    except Exception as e:  # noqa: BLE001
        NR.say(f"    ⚠ partial answer not kept — {type(e).__name__}")


def keep_reply(name: str, text) -> Optional[str]:
    """P0.7 r2 (E3 d): the update reply's complete text, kept the moment it arrives — before parsing — as
    checkpoints/<name>_<run id>.txt (UTF-8; outside output\\, never uploaded; the next pipeline Block 1 archives it with the
    checkpoints). Printed; never raises: a failed write is printed and the run goes on, and the record then names no file. Returns
    "checkpoints/<file>" or None."""
    try:
        rel = f"{name}_{NR.run_id()}.txt"
        t = text if isinstance(text, str) else ""
        cp = HARNESS_DIR / "checkpoints"
        cp.mkdir(parents=True, exist_ok=True)
        (cp / rel).write_bytes(t.encode("utf-8", errors="replace"))
        NR.say(f"    reply kept: checkpoints/{rel} ({len(t):,} characters)")
        return f"checkpoints/{rel}"
    except Exception as e:  # noqa: BLE001
        NR.say(f"    ⚠ the reply was not kept ({type(e).__name__}) — the run goes on")
        return None


def _call_ident(label, profile, second=None):
    """BUILD IC-8 (D8): a model call's identity (NR.new_call: its call id, sequence, generation attempt and journal) as model_call's
    keyword arguments."""
    ident = NR.new_call(HARNESS_DIR / "checkpoints", label, profile, 2 if second is not None else 1)
    return {"journal": ident["journal"], "ident": ident}


def call_claude(api_key: str, system_prompt: str, user_prompt: str, profile: str, model: str = MODEL, fmt: dict | None = None,
                messages: list | None = None, label: str | None = None, second: int | None = None) -> str:
    """One streamed Claude call: returns the finished answer's text or raises a named error. BUILD IC-4 (D12): `fmt`, a JSON schema, is
    sent as output_config.format with the switch on (NR.output_config); without it, or with the switch off, the effort alone.
    The caller's loop owns retries (P0.11 D5); the SDK itself never retries (max_retries=0).
    P0.7 (D10): the streamed text is kept; when the call ends in any error after text began, it is saved (keep_partial).
    BUILD IC-6 (D1): `profile` names the call ("muse/call1", "muse/call2", "muse/update", "muse/repair") — its limit, effort, stream mode
    and the call log's label are its profile's (IC-5's CALL_MAX_TOKENS = 16000 served the first three). BUILD IC-6 (D12, D6): `messages` in
    place of the prompt (a continuation's three), `label` in place of the profile's (its "+c"); without them the request is as before.
    BUILD IC-7 (D3): `second` — a second attempt's count of the first attempt's errors, for the call log (counts only)."""
    prof = NR.profile(profile)
    label = label or prof["label"]
    if not HAS_ANTHROPIC:
        raise RuntimeError("the anthropic package is not installed (pip install anthropic)")
    import httpx
    import threading
    client = anthropic.Anthropic(api_key=api_key, max_retries=0,
                                 timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
    t0 = time.monotonic()
    attempt = _ATTEMPT_N[label] = max(_ATTEMPT_N.get(label, 0) + 1, NR.next_attempt(HARNESS_DIR / "checkpoints", label))   # IC-7 (D10)
    try:            # BUILD IC-3 (D5): the one helper — every event read, a line in checkpoints/call_log.jsonl, ended at the stage's end time
        try:
            msg, _text = NR.model_call(client, {"model": model, "max_tokens": prof["max_tokens"],
                                                "output_config": NR.output_config(prof["effort"], fmt),
                                                "system": system_prompt,
                                                "messages": messages if messages is not None else [{"role": "user", "content": user_prompt}]},
                                       label=label, attempt=attempt, log_dir=HARNESS_DIR / "checkpoints", clock=_clock,
                                       end_at=_clock() + _budget_left(), stream=prof["stream"], timer=threading.Timer, profile=profile,
                                       **_call_ident(label, profile, second),     # BUILD IC-6 (D6); IC-8 (D8): its identity
                                       second_attempt=second)          # BUILD IC-7 (D3): the flag and the error count
        except AssertionError as exc:          # anthropic lib/streaming/_messages.py:94 — the stream never started
            raise IncompleteResponse("the stream ended before the model started its answer") from exc
        return final_text(msg, t0)
    except Exception:
        keep_partial(label, attempt, (getattr(NR.model_call, "last", None) or (None, ""))[1])
        raise


def _extract_json(text: str) -> dict:
    """Extract JSON from Claude's response, handling markdown code fences."""
    # Try to find JSON block in code fences
    m = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    obj, dfind = NR.strict_loads((m.group(1) if m else text).strip(), "muse")      # BUILD IC-3 (D4): the one strict decoder
    NR.record_decode_findings(HARNESS_DIR / "checkpoints", dfind)
    _LAST_DECODE[:] = list(dfind)                      # BUILD IC-7 (D2): a key given twice is named in the reply's admission
    obj = REG.admit(obj, "reply", "muse")        # BUILD IC-5 (D1, I1): the one way in (this harness had no input cleaner)
    REG.write_admit_findings(HARNESS_DIR / "checkpoints", NR.run_id())
    return obj


# P0.11 D5 — the generation loops below own the retries; the SDK never retries (max_retries=0).
MUSE_STAGE_BUDGET_S = 840     # 900 s pipeline limit (pipeline.py) - 60 s margin
MUSE_RATE = (80.9, "N1 4.4, the Muse's rate")   # BUILD IC-4 (D9): the stated rate when this machine's call log has no finished line
#                                                 (NR.measured_rate); replaces the 210 s reserve
_clock = time.monotonic       # patched in tests
_sleep = time.sleep           # patched in tests
_STAGE_T0: Optional[float] = None


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



def _budget_left() -> float:
    """The seconds to the Muse's end time: its stage's budget less what has run (its first call begins the stage). BUILD IC-6 (D3, I3):
    never past the block's end less the hub's reserve and the Muse's own time outside its calls (NR.block_end_for; none by hand)."""
    global _STAGE_T0
    if _STAGE_T0 is None:
        _STAGE_T0 = _clock()
    left = MUSE_STAGE_BUDGET_S - (_clock() - _STAGE_T0)
    blk = NR.block_end_for("muse", _clock)
    return left if blk is None else min(left, blk - _clock())


def _require_budget(label: str, profile: str, cause: Optional[BaseException] = None, wait: float = 0.0, retry: bool = False) -> None:
    """Stop before an attempt that cannot finish inside the Muse stage budget. BUILD IC-4 (D9): NR.start_check — the time left, less the
    wait before a retry, against the call's expected length; the Muse's first call begins the stage. BUILD IC-6 (D2): the expected length
    is the plan of the call's profile (`profile`: its planning tokens at the slowest rate on record or on this machine, else MUSE_RATE) —
    never its max_tokens (at 128,000 IC-4's rule would have refused call 2: 128,000 ÷ 102.03 = 1,255 s)."""
    begins = _STAGE_T0 is None
    left = _budget_left()
    ok, note, _need = NR.start_check(profile, HARNESS_DIR / "checkpoints", MODEL, MUSE_RATE, left=left, wait=wait, begins=begins,
                                     retry=retry, stage_s=MUSE_STAGE_BUDGET_S)
    if not ok:
        raise StageBudgetExceeded(f"{label}: {note}") from cause
    if note:
        NR.say(f"    {label}: {note}")


# ═══════════════════════════════════════════════════════════════════════════════
# P0.3b (D9) — the brief's answers: the contract's items in Call 1, brief_answers read outside the schema
# ═══════════════════════════════════════════════════════════════════════════════
_BRIEF: Dict[str, Any] = {"doc": None, "facts": [], "raw": None, "answers": {}}   # set by main(); empty = no contract
# BUILD IC-6 (D1): the repair's limit, effort and stream mode are its profile's, "muse/repair" (IC-5's: 4,000 at low, streamed)
BRIEF_REPAIR_SYSTEM = ("You are an expert civic marketing strategist. Output ONLY valid JSON — no markdown, no explanation, "
                       "no code fences.")


def _brief_contract():
    """The contract pipeline.py exported, or None (then every prompt is unchanged). Never raises."""
    if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
        return None
    try:
        return REG.admit(NR.from_env(), "data", "contract")   # BUILD IC-5 (D1)
    except Exception as e:  # noqa: BLE001
        NR.say(f"    brief: not used — the contract is not usable ({type(e).__name__}: {str(e)[:160]})")
        return None


def muse_pipeline_facts(n_endpoints: int) -> list:
    """What this harness really does: the only method facts an 'AI use' answer may state (D9). The model is named through MODEL."""
    return [f"{n_endpoints} challenge API endpoint(s) fetched at run time from the challenge API and summarised for the model",
            f"two structured calls to {MODEL} at effort medium: the strategy (personas, channels, calendar, KPIs, these answers), then the "
            "creative (social posts, the bilingual flyer, the partner pitch), each validated against a schema",
            "the reach calculator is fixed arithmetic on the channel plan, computed in Python and in the browser, not by the model",
            "the page's chat answers questions from a context file built from this campaign, through a server-side proxy; no key is on the page"]


def _muse_summary(strategic, creative) -> str:
    """A compact plain-text summary of the campaign for the repair request (no markup, at most 9,000 characters)."""
    s = strategic.model_dump()
    c = creative.model_dump() if creative is not None else {}
    parts = [f"{k}: {NR.clean_text(s.get(k))}" for k in ("hero_title", "hero_lead", "persona_text", "short_tagline") if NR.clean_text(s.get(k))]
    parts += [f"persona: {p.get('name')}, {p.get('age')}, {p.get('occupation')} — barriers: {'; '.join(map(str, p.get('barriers') or []))}; "
              f"channels: {', '.join(map(str, p.get('channels') or []))}" for p in s.get("personas") or []]
    parts += [f"channel: {x.get('channel')} — {x.get('audience')}; {x.get('content_type')}; {x.get('frequency')}; KPI {x.get('kpi')}; reach {x.get('reach')}"
              for x in s.get("channel_plan") or []]
    parts += [f"phase: {x.get('phase_name')} ({x.get('month_label')}) — {'; '.join(map(str, x.get('milestones') or []))}" for x in s.get("calendar") or []]
    parts += [f"kpi: {x.get('metric')} {x.get('target')} ({x.get('timeframe')})" for x in s.get("kpis") or []]
    parts += [f"post ({x.get('platform')}): {NR.clean_text(x.get('text'))}" for x in c.get("social_posts") or []]
    if c.get("flyer"):
        parts.append(f"flyer: {c['flyer'].get('headline_en')} / {c['flyer'].get('headline_es')}")
    if c.get("partner_pitch"):
        parts.append(f"partner pitch: {c['partner_pitch'].get('headline')} — {'; '.join(map(str, c['partner_pitch'].get('benefits') or []))}")
    return "\n".join(parts)[:9000]


def muse_brief_answers(api_key: str, strategic, creative) -> dict:
    """brief_answers from Call 1, parsed by a helper that never raises; at most ONE repair request after Call 2, only when it can finish
    inside the stage budget (BUILD IC-4, D9: NR.start_check; never a re-ask of Call 1); prints 'brief answers: n of N (repair: k asked,
    j filled)'. Returns {item id: answer}."""
    doc = _BRIEF["doc"]
    ids = [i for i, _ in NR.brief_items(doc, "muse")]
    answers, notes = NR.parse_brief_answers(_BRIEF["raw"], ids)
    for n in notes:
        NR.say(f"    {n}")
    missing = [i for i in ids if i not in answers]
    asked = filled = 0
    if missing:
        ok_, note_, _need = NR.start_check("muse/repair", HARNESS_DIR / "checkpoints", MODEL, MUSE_RATE, left=_budget_left())
        if not ok_:
            NR.say(f"    brief answers: repair skipped — {note_}")
        else:
            asked = len(missing)
            try:
                got = _extract_json(call_claude(api_key, BRIEF_REPAIR_SYSTEM, NR.brief_repair_prompt(doc, "muse", missing, _muse_summary(strategic, creative),
                                                                                                    _BRIEF["facts"]),
                                                "muse/repair", fmt=NR.BRIEF_REPAIR_SCHEMA))
                rep, notes2 = NR.parse_brief_answers(got.get("brief_answers") if isinstance(got, dict) else got, missing)
                for n in notes2:
                    NR.say(f"    repair: {n}")
                for i in missing:
                    if i in rep:
                        answers[i] = rep[i]
                        filled += 1
            except Exception as e:  # noqa: BLE001 — a failed repair leaves those rows "—"; the page is built regardless
                NR.say(f"    brief answers: repair failed — {type(e).__name__}: {str(e)[:200]}")
    NR.say(f"    brief answers: {len(answers)} of {len(ids)} (repair: {asked} asked, {filled} filled)")
    return answers


_SPAN = re.compile(r"\b(day|week|wk|month)s?\.?\s*(\d+)(?:\s*(?:-|–|—|to|through)\s*(?:(?:day|week|wk|month)s?\.?\s*)?(\d+))?", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "wk": 7, "month": 30}


def calendar_instruction(pilot_days, shown=None) -> str:
    """P0.6 (D1): Call 1's calendar line — the phases span the brief's pilot when it states one; no fixed weeks otherwise. BUILD IC-4
    (D16, as IC-3's D7 gave the Oracle's requests): with the Oracle's parts in the request (`shown`: the registry's pilot_days as it
    prints), the pilot by its id — Python's number stands in its [[id]] = value pair, never typed into the instruction."""
    if pilot_days and shown:
        return ("The pilot's length in days: [[pilot_days]] = " + shown + "; the calendar's 3 phases (for example Partner Onboarding, Soft "
                "Launch, Full Campaign) run in order from its first day and end by day [[pilot_days]]; label each phase's span in days "
                "(\"Days A-B\") or weeks (\"Weeks A-B\", no week that starts after day [[pilot_days]]), and no phase may start after day "
                "[[pilot_days]].")
    if pilot_days:
        return (f"The brief sets a {pilot_days}-day pilot: the calendar's 3 phases (for example Partner Onboarding, Soft Launch, Full "
                f"Campaign) run in order from day 1 and end by day {pilot_days}; label each phase's span in days (\"Days A-B\") or weeks "
                f"(\"Weeks A-B\", week {-(-pilot_days // 7)} at most), and no phase may start after day {pilot_days}.")
    return ("The brief states no pilot length: the calendar's 3 phases (for example Partner Onboarding, Soft Launch, Full Campaign) run "
            "in order from day 1; label each phase's span in days (\"Days A-B\") or weeks (\"Weeks A-B\").")


def calendar_check(strategic, pilot_days) -> str:
    """BUILD IC-5 (D7, G13): calendar_fit on the campaign's phase labels once its words are resolved (resolve_muse_text's plain
    variant); REG.StageError before it reads any label when they are not."""
    REG.require(strategic, REG.RESOLVED, "calendar_fit")
    return calendar_fit([c.month_label for c in strategic.calendar], pilot_days)


def calendar_fit(labels, pilot_days) -> str:
    """P0.6 (D1): one printed line — whether the model's phase labels fit inside the brief's pilot. A label's last day, week or
    month must start by the pilot's last day ("Weeks 9-12" against 60 days: week 12 starts on day 78 → a warning); a label with no
    day, week or month number is named as not checked."""
    labels = [str(x) for x in labels]
    shown = " · ".join(labels)
    if not pilot_days:
        return f'calendar: the brief states no pilot length — the heading reads "Campaign Calendar" ({shown})'
    late, unread = [], []
    for lab in labels:
        m = _SPAN.search(lab)
        if not m:
            unread.append(lab)
            continue
        unit, last = m.group(1).lower(), int(m.group(3) or m.group(2))
        if (last - 1) * _UNIT_DAYS[unit] + 1 > pilot_days:
            late.append(lab)
    if not late and not unread:
        return f"calendar: {len(labels)} phase(s) inside the {pilot_days}-day pilot ({shown})"
    out = []
    if late:
        out.append(f"⚠ calendar: {', '.join(repr(x) for x in late)} run(s) past the {pilot_days}-day pilot — the heading says "
                   f"{pilot_days}-Day")
    if unread:
        out.append(f"⚠ calendar: {', '.join(repr(x) for x in unread)} — no day, week or month number to check against the "
                   f"{pilot_days}-day pilot")
    return "; ".join(out)


def oracle_context_block(oracle_ctx: Dict, domain_info: Dict) -> str:
    """Call 1's <oracle_context> block (empty without the Oracle's analysis) — also the curveball update's (P0.7, D7)."""
    program_name = oracle_ctx.get("program_name", f"{domain_info['label']} Navigator")
    city_name = oracle_ctx.get("city_name", "the city")
    F = NR.facts(_BRIEF["doc"])                               # P0.6 (D1): the brief's own facts (all None without a contract)
    unit_p = oracle_ctx.get("unit_plural") or F["unit_plural"]
    unit_s = oracle_ctx.get("unit_singular") or F["unit_singular"]
    amt, src = oracle_budget(oracle_ctx, F)                   # P0.6 (D2): the figure in use and its source, never "$0"
    ben = oracle_ctx.get("beneficiaries") or []
    cpb = oracle_ctx.get("cpb_y1")
    sroi_txt = oracle_ctx.get("sroi_display") or "N/A"
    e_sroi = oracle_ctx["_reg"].get("sroi") if NR.ic4_prompt() and oracle_ctx.get("_reg") is not None and sroi_txt != "N/A" else None
    if e_sroi and isinstance(e_sroi.get("partial"), str) and e_sroi["partial"]:     # BUILD IC-4 (D6, D16; open item 21): "partial",
        p_ = e_sroi["partial"]                       # as every print of the figure carries it — the registry's words, which begin "partial"
        sroi_txt += f" ({p_})" if p_.startswith("partial") else f" (partial — {p_})"
    ben_txt = f"{ben[0]:,}" if ben and isinstance(ben[0], (int, float)) and not isinstance(ben[0], bool) else "N/A"
    cpb_txt = f"${cpb}" if isinstance(cpb, (int, float)) and not isinstance(cpb, bool) and cpb else "N/A"
    budget_txt = (f"${amt:,} — {NR.BUDGET_SOURCES[src]}" if src else f"${amt:,}") if amt else "not in the Oracle's financial model"
    if not oracle_ctx.get("crisis_human"):
        return ""
    return f"""
<oracle_context>
Program Name: {program_name}
Domain: {oracle_ctx.get('domain', domain_info['domain'])}
City: {city_name}
Key Crisis: {oracle_ctx.get('badge_text', 'N/A')}
Human Story: {oracle_ctx.get('crisis_human', 'N/A')[:500]}
SROI: {sroi_txt}
Year 1 {unit_p}: {ben_txt}
Cost per {unit_s}: {cpb_txt}
Budget: {budget_txt}
</oracle_context>"""


def muse_rules_parts(oracle_ctx: Dict, key: str | None = None) -> str:
    """BUILD IC-4 (D16): the three parts the Oracle built on its final registry of this run — <registry>, <mentions>, <number_rules> —
    for the Muse's two Block 1 calls and its update, after the data and before the instruction, the switch on; "" with it off or without
    the Oracle's parts (load_oracle_checkpoints names why). BUILD IC-7 (D4, G2): with NAIPC_IC7_PROMPT on and the call's key, the rules
    are that call's own (REG.number_rules_text on its contract's rules_view: the typed-number line only where its reply's schema types
    a field as a number, the count line naming its own lists, the name line without the Oracle's fields) and the Oracle's examples
    (REG.rules_examples: the parts file's, or read from its rules' <examples> block), a count example naming one of the call's lists."""
    p = oracle_ctx.get("_parts") if NR.ic4_prompt() else None
    if not isinstance(p, dict):
        return ""
    rules = p.get("rules")
    if key is not None and NR.ic7_prompt():
        v = CONTRACTS[key]["rules_view"]
        rules = REG.number_rules_text(v, ic4=True) + REG.examples_block(REG.rules_examples(p), v)
    return "\n".join(x for x in (p.get("registry"), p.get("mentions"), rules) if isinstance(x, str) and x.strip()) + "\n"


def generate_strategic(api_key: str, api_data_summary: str, domain_info: Dict,
                       oracle_ctx: Dict, curveball: str = "") -> MuseStrategic:
    """Call 1: Generate strategic layer (personas, channels, calendar, KPIs)."""
    program_name = oracle_ctx.get("program_name", f"{domain_info['label']} Navigator")
    F = NR.facts(_BRIEF["doc"])                               # P0.6 (D1): the brief's own facts (all None without a contract)

    # Build Oracle context block if available (P0.7: one function, shared with the curveball update)
    oracle_block = oracle_context_block(oracle_ctx, domain_info)

    curveball_block = f"\n<curveball>\n{curveball}\n</curveball>" if curveball else ""
    parts = muse_rules_parts(oracle_ctx, "muse/call1")      # BUILD IC-4 (D16); IC-7 (D4): this call's own rule lines
    stat_w = "<a token for a figure of the data, as <number_rules> says>" if parts else "<a number copied from the data>"
    value_w = "<a token for a figure, as <number_rules> says>" if parts else "<a number from the data>"
    og_w = "<the problem, with one figure of the data as its token>" if parts else "<the problem, with one number from the data>"
    reg_ = oracle_ctx.get("_reg") if parts else None       # BUILD IC-4 (D16): the pilot by its id beside the parts (calendar_instruction)
    calendar_rule = calendar_instruction(F["pilot_days"], reg_.shown("pilot_days") if reg_ is not None and reg_.get("pilot_days") else None)
    ic7 = NR.ic7_prompt()          # BUILD IC-7 (D5): the wire, and the instruction block's three edits (off: IC-6's words)
    age_t = "age (a whole number of years)" if ic7 else "age"
    kpi_t = f'target (in the form "{TARGET_FORM if parts else TARGET_FORM_NO_PARTS}")' if ic7 else "target"

    system = """You are an expert civic marketing strategist. Generate a structured community outreach campaign grounded in real demographic data. Output ONLY valid JSON matching the schema exactly — no markdown, no explanation, no code fences. Use clear, accessible US community-outreach language. For Spanish content, use US community-outreach Spanish (use 'carro' not 'coche'). Write for 6th-grade reading level in English."""

    user = f"""Generate the STRATEGIC layer of a community outreach campaign.
{oracle_block}
<civic_data>
{api_data_summary}
</civic_data>
{curveball_block}
{parts}<instructions>
Generate JSON with these exact fields:
- "personas": array of 3 persona objects, each with: emoji, name, {age_t}, occupation, location, barriers (3-5 strings), channels (2-4 strings), quote, data_evidence (array of 3-4 objects with "stat" and "label" — specific numbers from the API data that ground this persona, in the form {{"stat":"{stat_w}","label":"<what it measures> [<endpoint-name>]"}})
- "channel_plan": array of 5-6 objects, each with: channel, audience, content_type, frequency, kpi, reach
- "calendar": array of 3 phases, each with: phase_name, month_label, milestones (4-6 strings), deliverables (2-3 strings)
- "kpis": array of 6 objects, each with: emoji, {kpi_t}, metric, timeframe
- "hero_title": campaign headline with HTML <em> tags on key numbers/phrases
- "hero_lead": 1-2 sentence campaign summary
- "short_tagline": Timeless brand promise, 5-12 words, MAX 80 characters. Form: "Connecting <the people served> to <what the program gives them>." Must NOT contain numbers, place names, or the same wording as hero_title or hero_lead. Focus on the enduring mission, not the specific crisis.
- "og_description": Social media preview hook, MAX 150 characters. Front-load the most important information in the first 90 characters so platforms that clip still show the core message. Form: "{og_w}. <what the program does about it>." Must NOT copy short_tagline verbatim. Create curiosity or urgency.
- CRITICAL ANTI-OVERLAP RULE: hero_title, short_tagline, hero_lead, and og_description must each serve a DISTINCT purpose. Do NOT paraphrase one field into another. hero_title is dramatic with numbers; short_tagline is timeless without specifics; hero_lead describes the situation; og_description is a click-worthy social hook.
- "hero_stats": array of 3-4 objects with "value" and "label" keys (e.g. {{"value":"{value_w}","label":"<what it counts>"}})
- "brand_handle": social media handle like "@{program_name.replace(' ', '')}"
- "persona_text": one sentence about who the campaign serves

Ground ALL personas in the data. Each persona's barriers must come from what the data measures (income, vehicle access, language, distance or the like). Channel choices must be realistic for the audience — if they lack vehicle access, digital + community partners outperform print distribution. {calendar_rule}
</instructions>"""
    if _BRIEF["doc"]:  # P0.3b (D9): the brief block in Call 1 only; with no contract the prompt is unchanged
        user += "\n" + NR.brief_block(_BRIEF["doc"], "muse", _BRIEF["facts"], wire=ic7)      # BUILD IC-7 (D5): its field by name

    wire = muse_wire("muse/call1", brief=bool(_BRIEF["doc"]), parts=bool(parts)) if ic7 else None     # BUILD IC-7 (D5)
    result, popped = _call_admitted("Call 1 (Strategic)", "muse/call1", api_key, system, user, wire, parts=bool(parts), pop=("brief_answers",))
    _BRIEF["raw"] = popped.get("brief_answers")          # P0.3b: read outside the schema (D9)
    console.print(f"    [green]✓ {len(result.personas)} personas, {len(result.channel_plan)} channels, {len(result.kpis)} KPIs[/green]")
    return result


def _second_start(name, key, cause, errs):
    """BUILD IC-7 (D3): a second attempt starts only when the start rule allows a retry (the profile's plan, retry=True); else the track
    fails in plain words (NR.AdmissionFailed, NO_TIME_WORDS)."""
    try:
        _require_budget(f"{name} second attempt", key, cause=cause, retry=True)
    except StageBudgetExceeded as e:
        raise NR.AdmissionFailed(f"{name}: its reply could not be used and no time is left for a second attempt — {e}",
                                 plain=NR.NO_TIME_WORDS, errors=errs) from e


def _call_admitted(name, key, api_key, system, user, wire, parts=True, pop=()):
    """BUILD IC-7 (D2, D3; I3, I4): one Block 1 call's attempts → (the page model, {popped field: value}). A reply is admitted (NR.admit
    through admit_reply); when an essential path stays invalid — or the reply is no JSON, or was cut at its limit — the second attempt is
    the first request plus <errors_of_the_first_attempt> at the end of the user message (never the same request again: IC-6 re-sent
    it), started only when the start rule allows a retry. A transport error that an identical request can fix is retried identically, as
    before (is_retryable). MAX_RETRIES counts every attempt. The console's lines are plain (NR.say, D8)."""
    errs = None
    for attempt in range(MAX_RETRIES):
        if attempt == 0:
            _require_budget(name, key)
        console.print(f"  [cyan]{name} — attempt {attempt + 1}" + (" (the first attempt's errors carried)" if errs else "") + "[/cyan]")
        u = user if errs is None else NR.second_request(user, errs)
        try:
            raw = call_claude(api_key, system, u, key, fmt=wire, second=len(errs) if errs is not None else None)
        except Exception as e:
            NR.say(f"    ✗ Attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            if isinstance(e, MaxTokensReached) and errs is None and attempt < MAX_RETRIES - 1:
                errs = [{"path": "(the reply)", "expected": "one whole JSON object", "wrote": "a reply cut at its output limit"}]
                _second_start(name, key, e, errs)
                continue
            if not is_retryable(e) or attempt == MAX_RETRIES - 1:
                raise
            _require_budget(f"{name} retry", key, cause=e, wait=_retry_wait(e, attempt + 1), retry=True)   # IC-4 (D9): the wait counted
            _sleep(_retry_wait(e, attempt + 1))
            continue
        try:
            data = _extract_json(raw)
        except Exception as e:  # noqa: BLE001 — no JSON: the whole reply is the error
            data = None
            NR.say(f"    ✗ Attempt {attempt + 1}: the reply is not JSON ({type(e).__name__})")
        popped = {}
        if isinstance(data, dict):
            for k in pop:
                popped[k] = data.pop(k, None)
            obj, _entries, e2 = admit_reply(key, data, attempt + 1, decode=_LAST_DECODE, parts=parts)
        else:
            obj, e2 = None, [{"path": "(the reply)", "expected": "one JSON object of its schema",
                              "wrote": "no JSON" if data is None else f"a {type(data).__name__}"}]
            NR.admission_record(HARNESS_DIR / "checkpoints", key, [], "to a second attempt" if attempt == 0 else "not admitted", attempt + 1)
        if obj is not None:
            return obj, popped
        NR.say(f"    ✗ Attempt {attempt + 1}: {len(e2)} field(s) the page cannot do without could not be used — "
               + ", ".join(x["path"] for x in e2[:8]) + (" …" if len(e2) > 8 else ""))
        if attempt == MAX_RETRIES - 1:
            raise NR.AdmissionFailed(f"{name}: the reply could not be used after {MAX_RETRIES} attempts ({len(e2)} field(s): "
                                     + ", ".join(x["path"] for x in e2[:8]) + ")", errors=e2)
        errs = e2
        _second_start(name, key, None, errs)
    raise NR.AdmissionFailed(f"{name}: no attempt left")


def generate_creative(api_key: str, strategic: MuseStrategic, domain_info: Dict,
                      oracle_ctx: Dict, curveball: str = "") -> MuseCreative:
    """Call 2: Generate creative layer (social posts, flyer, partner pitch).
    Personas from Call 1 are injected for consistency."""
    program_name = oracle_ctx.get("program_name", f"{domain_info['label']} Navigator")

    # Serialize personas for context injection
    personas_ctx = "\n".join([
        f"- {p.name}, {p.age}, {p.occupation} in {p.location}. Barriers: {', '.join(p.barriers)}. Channels: {', '.join(p.channels)}."
        for p in strategic.personas
    ])

    curveball_block = f"\n<curveball>\n{curveball}\nInclude one emergency/crisis-response social post.\n</curveball>" if curveball else ""
    parts = muse_rules_parts(oracle_ctx, "muse/call2")      # BUILD IC-4 (D16); IC-7 (D4): this call's own rule lines
    real_w = "cite the data's figures by their tokens, as <number_rules> says" if parts else "use specific numbers from the data"
    eng_f = "" if parts else ', likes, comments, shares (as formatted strings like "1,204")'    # IC-4 Phase 7 (review c5): no invented
    eng_r = "" if parts else ", include realistic engagement metrics (hundreds to thousands, not millions)"   # counts with the parts

    system = """You are a bilingual civic marketing copywriter. Generate actual marketing materials — not descriptions of materials. Output ONLY valid JSON matching the schema exactly — no markdown, no code fences. All copy must be concrete, specific, and grounded in the personas and data provided. For Spanish, use clear accessible US community-outreach Spanish."""

    user = f"""Generate the CREATIVE layer of the {program_name} outreach campaign.

<personas>
{personas_ctx}
</personas>

<brand>
Program: {program_name}
Handle: {strategic.brand_handle}
Hero: {strategic.hero_lead}
</brand>
{curveball_block}
{parts}<instructions>
Generate JSON with these exact fields:
- "social_posts": array of 3 posts. Each MUST have: platform (one of "Photo-Sharing Platform", "Microblog Platform", "Community Feed"), card_class (one of "photo", "micro", "feed"), location, language ("English", "Bilingual EN/ES", or "Spanish"), image_headline, image_desc, text (with emoji, specific data points, real partner names from the data), hashtags (3-5 including program hashtag){eng_f}.
  - Post 1: Photo-sharing (card_class "photo"), English, with image overlay
  - Post 2: Microblog (card_class "micro"), Bilingual EN/ES
  - Post 3: Community Feed (card_class "feed"), English, partner spotlight
- "flyer": object with headline_en, headline_es, subheadline_en, subheadline_es, body_en, body_es, bullets_en (3-5 strings), bullets_es (matching translations), cta_en, cta_es, contact_info
- "partner_pitch": object with headline, problem (2-3 sentences), solution (2-3 sentences), benefits (5-7 strings), data_points (array of 3-5 objects with "stat" and "label")

Make social posts feel REAL — {real_w}, reference actual partner organizations{eng_r}. Flyer must be scannable at arm's length. Partner pitch should emphasize concrete benefits, not vague promises.
</instructions>"""

    wire = muse_wire("muse/call2", parts=bool(parts)) if NR.ic7_prompt() else None      # BUILD IC-7 (D5)
    result, _popped = _call_admitted("Call 2 (Creative)", "muse/call2", api_key, system, user, wire, parts=bool(parts))
    console.print(f"    [green]✓ {len(result.social_posts)} posts, flyer ({len(result.flyer.bullets_en)} bullets), pitch ({len(result.partner_pitch.benefits)} benefits)[/green]")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# P0.7 (D7) — Block 2 carries Block 1's campaign forward: one update call; Python decides which amendments stand
# ═══════════════════════════════════════════════════════════════════════════════
MUSE_BLOCK1 = "muse_block1.json"
MUSE_UPDATE_TAG = "<muse_block1>"      # the update prompt's own block: the mock Messages API recognises the Muse's update by it
MUSE_UPDATE_FAILED_LINE = ("The campaign was not updated for the curveball (the update call did not complete): this is Block 1's campaign; "
                           "the data changes below are computed from the API.")
CARD_CLASSES = ("photo", "micro", "feed")
MUSE_AMENDABLE = (tuple(f"strategic.{f}" for f in MuseStrategic.model_fields if f != "brand_handle")
                  + tuple(f"creative.{f}" for f in MuseCreative.model_fields))
MUSE_FIELD_NAMES = {"strategic.personas": "the personas", "strategic.channel_plan": "the channel plan", "strategic.calendar": "the calendar",
                    "strategic.kpis": "the KPIs", "strategic.hero_title": "the campaign headline", "strategic.hero_lead": "the campaign lead",
                    "strategic.hero_stats": "the headline figures", "strategic.persona_text": "who the campaign serves",
                    "strategic.short_tagline": "the tagline", "strategic.og_description": "the social preview",
                    "creative.social_posts": "the social posts", "creative.flyer": "the flyer", "creative.partner_pitch": "the partner pitch"}
MUSE_STRATEGIC_FIELDS = tuple(k.split(".", 1)[1] for k in MUSE_AMENDABLE if k.startswith("strategic."))   # rule 4's two schema blocks (E1)
MUSE_CREATIVE_FIELDS = tuple(k.split(".", 1)[1] for k in MUSE_AMENDABLE if k.startswith("creative."))
# P0.7 r2 (E2): each cap ONE constant, used by the cut or the list cap AND stated in the rule that asks for the value
MUSE_IMPACT_CHARS = 600            # a what_changed impact (rule 2) — P0.7 cut it here with no mark
MUSE_WHAT_CHANGED_MAX = 20         # what_changed entries used (rule 2)
MUSE_CRISIS_MAX = 3                # crisis posts used (rule 3)
MUSE_POSTS_SHOWN = 6               # social posts the page shows, the crisis posts first (rule 3) — MuseCreative's own maximum
MUSE_TAGLINE_FIELDS = ("short_tagline", "og_description")   # E2 (finding 21): checked against the schema's maxLength BEFORE the validator
MUSE_NOT_APPLIED = ("Python did not apply these parts of the update; where a part would have replaced Block 1's, Block 1's version "
                    "stands.")                                # E3 (b): the Muse's "Not applied" block (the update path)
# E3 (b): a page name for every part of the reply that is not a field, and for the handle ("reply": Phase 5 (C14), its keys not asked for)
MUSE_PART_NAMES = {"amended": "the amendments", "crisis_posts": "the crisis posts", "creative.social_posts": "the social posts",
                   "what_changed": "what changed", "strategic.brand_handle": "the campaign's handle", "reply": "the reply",
                   "because": "the reasons"}              # Phase 5 review #7 (c): reasons for a part the update did not amend
# P0.7 r2 Phase 5 (C14): the top-level keys the update reply is asked for (the prompt's last line) — any other key is named, not used;
# (E1 with C2 and C14): the two limits the merge enforces that no schema block shows, said in words (the Oracle's sentences; the review's
# #7: an entry's own keys, reasons for a part not amended, a key given twice)
MUSE_REPLY_KEYS = ("what_changed", "crisis_posts", "amended", "because")
MUSE_ITEM_KEYS_LINE = "An item's keys are the ones its schema lists; any other key is not used, and the page says so."
MUSE_REPLY_KEYS_LINE = ("Any other key — here, in an entry of a rule above or in because for a part not amended — is not used, a key given "
                        "twice in one object keeps only its last value, and the page says so.")
# the review's #3 and #5: the taglines' two limits the schema block does not show — the stored length ('<' and '>' are stored as four
# characters each) and null (refused: the validator would write one from the campaign lead, unmarked — C6)
MUSE_TAGLINE_LINE_IC7 = ("In strategic.short_tagline and strategic.og_description each '<' or '>' counts as four characters toward the limit, and "
                         "null is not applied — Block 1's stands.")
# BUILD IC-8 (D1, Q-SWITCH): under the switch the line says what Python now measures (the shown text: a token as the figure it prints)
MUSE_TAGLINE_LINE_IC8 = ("In strategic.short_tagline and strategic.og_description a token counts as the figure it prints, and null or an empty "
                         "text is not applied — Block 1's stands.")
MUSE_TAGLINE_LINE = MUSE_TAGLINE_LINE_IC8 if NR.ic8_prompt() else MUSE_TAGLINE_LINE_IC7
# BUILD IC-8 (D1, F12): IC-7's shown schema of the two taglines, frozen (read from IC-7's MuseStrategic.model_json_schema() on 11/19) —
# sent with the switch off, so that request stays IC-7's byte for byte
MUSE_TAGLINE_SCHEMA_IC7 = {
    "short_tagline": {"anyOf": [{"maxLength": 80, "type": "string"}, {"type": "null"}], "default": None,
                      "description": "Timeless brand promise, 5-12 words, max 80 chars", "title": "Short Tagline"},
    "og_description": {"anyOf": [{"maxLength": 150, "type": "string"}, {"type": "null"}], "default": None,
                       "description": "Social media preview hook, max 150 chars, front-loaded value", "title": "Og Description"}}
MUSE_WHAT_CHANGED_KEYS = ("ref", "impact")         # the review's #7 (b): a what_changed entry's own keys (rule 2's form)
# the review's #7 (b), the same class: the free-key items (MuseStrategic declares them Dict[str, str], so pydantic keeps every key) — the keys
# the page reads (muse_template.html.j2: a headline figure's value and label, a data-evidence item's stat and label; the chat reads neither),
# ONE constant each, used by the merge (any other key removed and named) and stated in rule 4 (MUSE_FREE_KEYS_LINE)
MUSE_HERO_STAT_KEYS = ("value", "label")
MUSE_EVIDENCE_KEYS = ("stat", "label")
MUSE_FREE_KEYS_LINE = ("An item of free keys keeps only the keys the page reads — " + " and ".join(f'"{k}"' for k in MUSE_HERO_STAT_KEYS)
                       + " in strategic.hero_stats, " + " and ".join(f'"{k}"' for k in MUSE_EVIDENCE_KEYS)
                       + " in a persona's data_evidence; any other key is not used, and the page says so.")
# E3 (a): the plain name of every property the amendable models declare (a <where> in a reason: "item 2's age")
MUSE_PROPERTY_NAMES = {
    "emoji": "emoji", "name": "name", "age": "age", "occupation": "occupation", "location": "location", "barriers": "barriers",
    "channels": "channels", "quote": "quote", "data_evidence": "data evidence", "channel": "channel", "audience": "audience",
    "content_type": "content type", "frequency": "frequency", "kpi": "measure", "reach": "reach", "phase_name": "phase name",
    "month_label": "time range", "milestones": "milestones", "deliverables": "deliverables", "target": "target", "metric": "metric",
    "timeframe": "timeframe", "platform": "platform", "card_class": "card style", "language": "language", "image_headline": "image headline",
    "image_desc": "image caption", "text": "text", "hashtags": "hashtags", "likes": "likes", "comments": "comments", "shares": "shares",
    "headline_en": "English headline", "headline_es": "Spanish headline", "subheadline_en": "English subheadline",
    "subheadline_es": "Spanish subheadline", "body_en": "English text", "body_es": "Spanish text", "bullets_en": "English bullet points",
    "bullets_es": "Spanish bullet points", "cta_en": "English call to action", "cta_es": "Spanish call to action",
    "contact_info": "contact line", "headline": "headline", "problem": "problem", "solution": "solution", "benefits": "benefits",
    "data_points": "data points", "stat": "figure", "label": "label"}
_MUSE_ENTRY = re.compile(r"^(crisis_posts|what_changed|creative\.social_posts)\[(\d+)\]$")


def write_muse_block1(strategic, creative, answers, raw_answers=None, item_ids=None) -> str:
    """D7: Block 1's campaign for the curveball update — checkpoints/muse_block1.json {schema, challenge, run_id, written_at,
    strategic, creative, brief_answers} (temp file + os.replace); the pipeline's Block 1 archive moves it. Never raises (returns the
    line to print). BUILD IC-6 (D12): item_ids — each list item's id, Python's, in the stored order (REG.muse_item_ids)."""
    doc = {"schema": 1, "challenge": os.environ.get("NAIPC_CHALLENGE", "").strip() or None, "run_id": NR.run_id(),
           "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "strategic": strategic.model_dump(),
           "creative": creative.model_dump(), "brief_answers": dict(answers or {})}
    doc["item_ids"] = REG.muse_item_ids(muse_lists(doc["strategic"], doc["creative"]), item_ids)[0]   # BUILD IC-6 (D12): Python's ids;
    #                                       BUILD IC-8 (D2): finalize_muse's, a left-out item's gap kept (muse_item_ids keeps ids that fit)
    if raw_answers:      # IC-4 Phase 7 (review c4): the answers as written, their tokens kept — Block 2 resolves them on its own registry
        doc["brief_answers_written"] = dict(raw_answers)
    why = NR.write_json(HARNESS_DIR / "checkpoints" / MUSE_BLOCK1, doc)
    return (f"✓ Block 1 campaign: checkpoints/{MUSE_BLOCK1} (run {doc['run_id']}) — the curveball update starts from it" if why is None else
            f"⚠ Block 1 campaign not written ({why}) — at the curveball the two calls run again")


def load_muse_block1(challenge, want_run=None, detail=None):
    """D7: Block 1's campaign of this challenge (and of the change list's Block 1 run when it names one) → ({strategic, creative,
    brief_answers, meta}, None) or (None, reason). It must validate under today's models. Never raises. P0.7 r2 (E3 a): the reason
    reaches the page and the chat, so it is plain words with no local path and no exception name; `detail` (a dict, optional)
    receives the technical part for the console ({"why": …})."""
    f = HARNESS_DIR / "checkpoints" / MUSE_BLOCK1
    if not f.is_file():                 # Phase 5 review #10: the folder path only in the console's detail, never in the reason
        if isinstance(detail, dict):
            detail["why"] = f"not found: checkpoints/{MUSE_BLOCK1}"
        return None, "no Block 1 campaign was saved"
    d, why = NR.read_json(f)
    d = REG.admit(d, "saved", MUSE_BLOCK1) if isinstance(d, dict) else d     # BUILD IC-5 (D1): a file a run wrote is an input
    if not isinstance(d, dict):
        if isinstance(detail, dict):
            detail["why"] = f"checkpoints/{MUSE_BLOCK1}: {why or 'not an object'}"
        return None, "the saved Block 1 campaign could not be read"
    if d.get("schema") != 1:
        return None, "the Block 1 campaign is not in a format this code reads"
    if challenge and d.get("challenge") != challenge:
        return None, f"the Block 1 campaign is of {d.get('challenge')}, not {challenge}"
    if want_run and d.get("run_id") != want_run:
        return None, f"the Block 1 campaign is of run {d.get('run_id')}, the change list's Block 1 is run {want_run}"
    try:
        s, c = MuseStrategic(**d["strategic"]), MuseCreative(**d["creative"])
    except Exception as e:  # noqa: BLE001
        if isinstance(detail, dict):
            detail["why"] = type(e).__name__
        return None, "the Block 1 campaign no longer fits today's format"
    ids_, assigned_ = REG.muse_item_ids(muse_lists(s.model_dump(), c.model_dump()), d.get("item_ids"))   # BUILD IC-6 (D12): Block 1's ids,
    #   or — a Block 1 written before IC-6 — assigned here once, in the stored order (Block 1's file is not rewritten; the update's record keeps them)
    return {"strategic": s, "creative": c, "brief_answers": d.get("brief_answers") if isinstance(d.get("brief_answers"), dict) else {},
            "brief_answers_written": d.get("brief_answers_written") if isinstance(d.get("brief_answers_written"), dict) else None,
            "meta": {k: d.get(k) for k in ("challenge", "run_id", "written_at")},
            "item_ids": ids_, "ids_source": "assigned at load" if assigned_ else "stored"}, None


def _safe_post(p: Dict[str, Any]) -> Dict[str, Any]:
    """A post's card_class is printed inside a class attribute: only the three known classes reach the page (D11)."""
    p = dict(p)
    if p.get("card_class") not in CARD_CLASSES:
        p["card_class"] = "micro"
    return p


def muse_model_schema(model, fields=None) -> dict:
    """P0.7 r2 (E1, E3 a): a model's stripped schema over `fields` (every field of the model when None) — NR.schema_block, the form
    the update prompt shows; {"properties": …, "$defs": …}."""
    return json.loads(NR.schema_block(model.model_json_schema(), list(fields or model.model_fields)))


def muse_field_schema(key: str) -> dict:
    """E3 (a): one amendable key's schema ("strategic.<f>" / "creative.<f>") with its model's $defs — the part a check names."""
    top, sub = key.split(".", 1)
    j = muse_model_schema(MuseStrategic if top == "strategic" else MuseCreative, [sub])
    return dict(j["properties"][sub], **({"$defs": j["$defs"]} if "$defs" in j else {}))


def _max_length(sch) -> Optional[int]:
    """A string schema's maxLength (at its root or in an anyOf branch — an optional field), else None."""
    for n in [sch] + list((sch or {}).get("anyOf") or []):
        if isinstance(n, dict) and isinstance(n.get("maxLength"), int):
            return n["maxLength"]
    return None


def muse_reason(e, field, schema: dict):
    """P0.7 r2 (E3 a): a check's failure → (the plain reason, the technical detail), as the Oracle's validation_reason: a
    ValidationError's errors() read as plain dicts (include_url=False, include_input=False: never the model's text), each location
    made relative to the part checked (`field` — None when the part is the model itself, a crisis post), in plain words by
    NR.plain_reason; pydantic's own messages are the detail (the record keeps it, no page shows it). Any other exception → "the value
    could not be checked", its name the detail."""
    if isinstance(e, ValidationError):
        errs = e.errors(include_url=False, include_input=False)
        if field is None:
            rel = [dict(x, loc=list(x.get("loc") or ())) for x in errs]
        else:
            rel = [dict(x, loc=list(x.get("loc") or ())[1:]) if list(x.get("loc") or ())[:1] == [field] else dict(x, loc=[]) for x in errs]
        detail = "; ".join(f"{x.get('type')} at {'.'.join(str(p) for p in (x.get('loc') or ())) or '(the value)'}: {x.get('msg')}" for x in errs)
        return NR.plain_reason(rel, schema, MUSE_PROPERTY_NAMES), NR.cut(detail, NR.DETAIL_CHARS)
    return "the value could not be checked", type(e).__name__


def _mdrop(part, field, why, detail=""):
    """P0.7 r2 (E3): one part of the reply not applied — {part, field, why (plain words), detail (technical; the record only)}."""
    return {"field": field, "part": part, "why": why, "detail": NR.cut(str(detail or ""), NR.DETAIL_CHARS)}


def muse_update_prompt(base, changes, cv, api_summary: str, oracle_block: str, parts: str = "") -> str:
    """D7's update prompt: Block 1's campaign, the change list (capped), this run's data and the Oracle's context. Event-neutral.
    BUILD IC-4 (D16): parts — the registry's three parts (muse_rules_parts), after the data and before the rules, the switch on."""
    b1 = {"strategic": base["strategic"].model_dump(), "creative": base["creative"].model_dump()}
    sw = {"same_words": NR.CHANGES_SAME_IC4} if NR.ic4_prompt() else {}       # BUILD IC-4 (D16; open item 5)
    eng_c = "" if parts else ", likes, comments, shares"        # IC-4 Phase 7 (review c5): no invented engagement counts with the parts
    chg = (NR.changes_prompt_block(changes, **sw) if changes else
           NR.changes_prompt_block({"curveball": cv, "endpoints": None, "snapshot": {"source": "none"}}, **sw))
    crisis_w = ("use only names from the data, <changes> or Block 1's campaign, and write every quantity as <number_rules> says" if parts
                else "use only names and numbers from the data, <changes> or Block 1's campaign")
    return f"""<context>
This is BLOCK 2 of a two-block competition. In Block 1 you wrote the community outreach campaign in <muse_block1> for this data. At the
curveball the organizers made the announcement below and the data changed; Python compared every endpoint record by record with Block 1's
snapshot (<changes>). UPDATE the campaign for what changed: keep everything the curveball does not touch exactly as it is.
</context>
{oracle_block}
{MUSE_UPDATE_TAG}
{json.dumps(b1, indent=1, ensure_ascii=False)}
</muse_block1>

{chg}

<civic_data>
{api_summary}
</civic_data>

{parts}<rules>
1. Cite every change by a ref from <changes>, exactly as written there: "announcement", an endpoint's name or a record's ref.
2. what_changed: one entry per change that matters for the campaign, at most {MUSE_WHAT_CHANGED_MAX} entries: {{"ref": "<a ref>", "impact": "<1-2
   plain sentences, at most {MUSE_IMPACT_CHARS:,} characters>"}}.
3. crisis_posts: one to {MUSE_CRISIS_MAX} social posts that respond to the curveball, each with the fields of Block 1's social posts (platform,
   card_class "photo", "micro" or "feed", location, language, image_headline, image_desc, text, hashtags{eng_c});
   {crisis_w}. Each post must validate as an item of
   creative.social_posts in rule 4's schema; a post that does not is not used, and the page says so. The page shows
   {MUSE_POSTS_SHOWN} social posts at most, the crisis posts first.
4. amended: ONLY the fields the curveball changes, each as its complete new value of the same type as in Block 1's campaign, keyed
   "strategic.<field>" or "creative.<field>". Allowed keys: {", ".join(MUSE_AMENDABLE)}. Never strategic.brand_handle.
   because: {{"<key>": ["<ref>", ...]}} for every amended key — a key without a real ref is dropped.
   Each amended key must validate against its schema below — the one Block 1's campaign follows; a value that breaks it is not
   applied, and the page says so:
   strategic.<field>: {NR.schema_block(shown_schema(MuseStrategic), MUSE_STRATEGIC_FIELDS)}
   creative.<field>: {NR.schema_block(shown_schema(MuseCreative), MUSE_CREATIVE_FIELDS)}
   {MUSE_ITEM_KEYS_LINE}
   {MUSE_FREE_KEYS_LINE}
   {MUSE_TAGLINE_LINE}
5. Plain text in every new value; HTML only where Block 1's field already holds it.
</rules>

Return ONLY one JSON object: {{"what_changed": [...], "crisis_posts": [...], "amended": {{...}}, "because": {{...}}}}. {MUSE_REPLY_KEYS_LINE} No markdown fences."""


def _free_keys_out(field, value):
    """Phase 5 review #7 (b): a kept amendment's free-key items — the keys the page does not read (MUSE_HERO_STAT_KEYS of a headline figure,
    MUSE_EVIDENCE_KEYS of a persona's data-evidence item) → (the value without them, those keys distinct in the order met); any other field
    → (value, []). New lists and objects (the reply's own are never changed). Never raises."""
    out = []

    def keep_only(items, keep):
        if not isinstance(items, list):
            return items
        res = []
        for it in items:
            if isinstance(it, dict):
                for k in it:
                    if k not in keep and str(k) not in out:
                        out.append(str(k))
                res.append({k: v for k, v in it.items() if k in keep})
            else:
                res.append(it)
        return res
    try:
        if field == "strategic.hero_stats":
            return keep_only(value, MUSE_HERO_STAT_KEYS), out
        if field == "strategic.personas" and isinstance(value, list):
            return [dict(p, data_evidence=keep_only(p["data_evidence"], MUSE_EVIDENCE_KEYS)) if isinstance(p, dict) and "data_evidence" in p else p
                    for p in value], out
    except Exception:  # noqa: BLE001 — not expected: the check validated exactly this value
        return value, []
    return value, []


def muse_field_check(s0, c0, field, value, raw=None):
    """P0.7 r2 (E2, E3 a; review #3; Phase 5 C6) — one amendable field's check, merge_muse_update's (BUILD IC-6 D12: moved here unchanged,
    shared with the edit validator): a tagline set to null refused; a tagline longer than its schema's maxLength on the value AS WRITTEN
    (`raw`), then as stored ('<' and '>' four characters each) — BEFORE the validator can cut it; then the campaign's model with Block 1's
    other fields (`s0`, `c0`). → None, or (the plain reason, the technical detail)."""
    top, sub = field.split(".", 1)
    try:
        sch = muse_field_schema(field)
    except Exception:  # noqa: BLE001
        sch = {}
    if sub in MUSE_TAGLINE_FIELDS and (value is None or (isinstance(value, str) and not value.strip())):   # BUILD IC-8 (D1): null or
        return "the update gave no text for it", ("None" if value is None else "an empty text")      # empty — Block 1's stands
    # BUILD IC-8 (D1): no refusal on a tagline's length — a Block 2 preview is fitted on its shown text at the page, as Block 1's is
    try:            # BUILD IC-7 (D2, Q-STRICT): strict — the declared conversions came first (update_walk)
        (MuseStrategic if top == "strategic" else MuseCreative).model_validate(dict(s0 if top == "strategic" else c0, **{sub: value}), strict=True)
        return None
    except Exception as e:  # noqa: BLE001 — every failure named in plain words (E3 a)
        return muse_reason(e, sub, sch)


def merge_muse_update(base, reply: Dict[str, Any], refs: Dict[str, str]):
    """D7 — Python decides what stands: each amended "strategic.<f>" / "creative.<f>" through NR.merge_amended (allowed, a real ref,
    the Muse's own model with Block 1's other fields), escaped (D11); one to MUSE_CRISIS_MAX crisis posts, each validated as a
    SocialPost and escaped, put first in the social posts (MUSE_POSTS_SHOWN at most); every drop printed by name. Returns (strategic,
    creative, info). P0.7 r2 (E2, E3): every part not applied, discarded or replaced is an entry of info["dropped"] — {part, field, why
    (plain words), detail (technical; the record only)}: an amendment (a tagline longer than its schema's maxLength, read on the model's
    own text, refused BEFORE the validator can cut it; any other failure in plain words by NR.plain_reason — every value escaped before
    its check, so what is checked is what is stored), a kept amended post or a kept crisis post whose card style the page does not have
    (shown as a micro post), a crisis post that is not an object or does not validate, the crisis posts past MUSE_CRISIS_MAX, the
    update's own amended posts left out by the page's six. Block 1's posts left out are printed, as P0.7 did. The totals line is run_muse_update's (after what_changed).
    P0.7 r2 Phase 5: a kept amendment or a kept crisis post with keys its model does not declare → one entry naming them, the rest
    applied (C2); a tagline amended to null is not applied — Block 1's stands — rather than replaced by the validator's own (C6). The
    independent review: a tagline's length read on the RAW value (E2), one within it as written but over it as stored ('<' and '>' four
    characters each) refused with that reason (#3); reasons given for a part not amended named (#7 c); a kept headline figure's or data-evidence
    item's keys the page does not read removed and named with the amendment's C2 keys in one entry (#7 b, _free_keys_out)."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()
    raw_am = reply.get("amended") if isinstance(reply.get("amended"), dict) else {}   # as the model wrote it (review #3: the length E2 reads)

    def check(field, value):           # BUILD IC-6 (D12): the field check moved to muse_field_check, unchanged — the edit validator's too
        return muse_field_check(s0, c0, field, value, raw_am.get(field))
    amended, replaced_cls = reply.get("amended"), []
    if isinstance(amended, dict):   # D11: escaped (and a post's class made safe) BEFORE the check, so what is checked is what is stored
        esc = {}
        for k, v in amended.items():
            if str(k) == "creative.social_posts" and isinstance(v, list):
                ev = NR.escape_markup(v)
                replaced_cls = [i for i, x in enumerate(ev) if isinstance(x, dict) and x.get("card_class") not in CARD_CLASSES]
                esc[k] = update_walk(str(k), [_safe_post(x) if isinstance(x, dict) else x for x in ev])      # BUILD IC-7 (D2)
            elif str(k) in MUSE_AMENDABLE:
                esc[k] = update_walk(str(k), NR.escape_markup(v))                                             # BUILD IC-7 (D2)
            else:
                esc[k] = NR.escape_markup(v)
        amended = esc
    details = {}
    merged, kept, dropped0 = NR.merge_amended({"strategic": s0, "creative": c0}, amended, reply.get("because"),
                                              set(MUSE_AMENDABLE), refs, check, details=details)
    drops = []
    for f, why in dropped0:
        if f == "amended":
            drops.append(_mdrop("shape", f, why, details.get(f)))
        elif f == "strategic.brand_handle":
            drops.append(_mdrop("amendment", f, "the program's handle — an update never changes it"))
        else:
            drops.append(_mdrop("amendment", f, why, details.get(f)))
    for f, _rs in kept:     # Phase 5 (C2): a key an item's model does not declare is gone from the stored form — named, the rest applied
        top, sub = f.split(".", 1)
        try:
            stored = (MuseStrategic if top == "strategic" else MuseCreative)(**dict(s0 if top == "strategic" else c0, **{sub: merged[top][sub]})).model_dump()[sub]
            xk = NR.extra_keys(merged[top][sub], stored)
        except Exception:  # noqa: BLE001 — not expected: the check validated exactly this value
            xk = []
        merged[top][sub], fk = _free_keys_out(f, merged[top][sub])   # review #7 (b): a free-key item's keys the page does not read, removed
        keys = xk + [k for k in fk if k not in xk]                   # ONE entry per field: C2's keys first, then the free keys
        if keys:
            drops.append(_mdrop("amendment", f, NR.extra_keys_reason(keys),
                                "; ".join(x for x in (("keys its pydantic class does not declare: " + json.dumps(xk, ensure_ascii=False)) if xk else "",
                                                      ("keys the page does not read, removed: " + json.dumps(fk, ensure_ascii=False)) if fk else "") if x)))
    if "creative.social_posts" in dict(kept):      # E2: a kept post shown as micro — its card style replaced
        drops += [_mdrop("amendment", f"creative.social_posts[{i}]", "its card style is not one the page has — shown as a micro post")
                  for i in replaced_cls]
    orphans = NR.orphan_reasons(reply.get("amended"), reply.get("because"))    # Phase 5 review #7 (c): reasons for a part not amended
    if orphans:
        drops.append(_mdrop("shape", "because", NR.orphan_reasons_reason(orphans), "keys: " + json.dumps(orphans, ensure_ascii=False)))
    posts = []
    raw_posts = reply.get("crisis_posts")
    if isinstance(raw_posts, list):
        try:
            sp_schema = muse_model_schema(SocialPost)
        except Exception:  # noqa: BLE001
            sp_schema = {}
        for j, p in enumerate(raw_posts[:MUSE_CRISIS_MAX]):
            if not isinstance(p, dict):
                drops.append(_mdrop("crisis post", f"crisis_posts[{j}]", "not in the expected form", f"a {type(p).__name__}"))
                continue
            try:
                sp = SocialPost(**_safe_post(p)).model_dump()
            except Exception as e:  # noqa: BLE001
                why, det = muse_reason(e, None, sp_schema)
                drops.append(_mdrop("crisis post", f"crisis_posts[{j}]", why, det))
                continue
            if p.get("card_class") not in CARD_CLASSES:
                drops.append(_mdrop("crisis post", f"crisis_posts[{j}]", "its card style is not one the page has — shown as a micro post"))
            xk = NR.extra_keys(p, sp)                     # Phase 5 (C2): the post is shown; its keys SocialPost does not declare are named
            if xk:
                drops.append(_mdrop("crisis post", f"crisis_posts[{j}]", NR.extra_keys_reason(xk),
                                    "keys its pydantic class does not declare: " + json.dumps(xk, ensure_ascii=False)))
            posts.append(NR.escape_markup(sp))
        if len(raw_posts) > MUSE_CRISIS_MAX:
            drops.append(_mdrop("list", "crisis_posts", f"{len(raw_posts) - MUSE_CRISIS_MAX} more than three — not used"))
    elif raw_posts is not None:
        drops.append(_mdrop("shape", "crisis_posts", "not a list — not used", f"a {type(raw_posts).__name__}"))
    if posts:
        before = merged["creative"]["social_posts"]
        merged["creative"]["social_posts"] = (posts + list(before))[:MUSE_POSTS_SHOWN]
        left = len(posts) + len(before) - MUSE_POSTS_SHOWN
        if left > 0 and "creative.social_posts" in dict(kept):     # E2 (finding 13): the update's own posts left out — recorded
            drops.append(_mdrop("list", "creative.social_posts", f"{left} of the update's posts not shown — the page shows six at most"))
        elif left > 0:
            NR.say(f"    update: {left} of Block 1's posts not shown (the page shows six posts at most)")
    s, c = MuseStrategic(**merged["strategic"]), MuseCreative(**merged["creative"])   # the stored form (each part already validated)
    for d in drops:
        NR.say(f"    update: dropped {d['field']} — {d['why']}")
    for f, rs in kept:
        NR.say(f"    update: amended {f} ({', '.join(rs)})")
    return s, c, {"amended": [{"field": f, "because": rs} for f, rs in kept], "dropped": drops, "crisis_posts": len(posts)}


def muse_take_what_changed(reply: dict, refs: dict):
    """P0.7 r2 (E2, E3): what_changed → (kept [{ref, impact}], drops) — the Oracle's one rule (take_what_changed): at most
    MUSE_WHAT_CHANGED_MAX entries (the rest one entry of drops); an entry that is not an object or whose ref or impact is not text, an
    empty one, one whose ref is not a change in the list (named, cut to NR.NAME_CHARS) and one with no impact are one entry each, each
    with its own reason — P0.7 printed "an unknown ref" for all of them and recorded nothing. The impact is kept whole (BUILD
    IC-8 D1: finalize_muse fits it at its destination, MUSE_IMPACT_CHARS its hard maximum). Review #6: no ref → "it names no change in the list"; #7 (b): a kept entry's
    keys beyond ref and impact named."""
    v = reply.get("what_changed")
    if v is None:
        return [], []
    if not isinstance(v, list):
        return [], [_mdrop("shape", "what_changed", "not a list — not used", f"a {type(v).__name__}")]
    kept, drops = [], []
    for j, e in enumerate(v[:MUSE_WHAT_CHANGED_MAX]):
        if not isinstance(e, dict) or any(e.get(k) is not None and not isinstance(e.get(k), str) for k in ("ref", "impact")):
            drops.append(_mdrop("list", f"what_changed[{j}]", "not in the expected form", f"a {type(e).__name__}" if not isinstance(e, dict) else "a value is not text"))
            continue
        r, impact = e.get("ref"), NR.clean_text(e.get("impact"))
        ref = r.strip().lower() if isinstance(r, str) else ""
        if not ref and not impact:
            drops.append(_mdrop("list", f"what_changed[{j}]", "empty — not used"))
        elif not ref:                                   # Phase 5 review #6: no ref given (absent, null, white space) — never "(null)"
            drops.append(_mdrop("list", f"what_changed[{j}]", "it names no change in the list"))
        elif ref not in refs:
            drops.append(_mdrop("list", f"what_changed[{j}]", f"its ref is not a change in the list ({NR.name_cut([r])})"))
        elif not impact:
            drops.append(_mdrop("list", f"what_changed[{j}]", "no impact given"))
        else:
            kept.append({"ref": refs[ref], "impact": impact})        # BUILD IC-8 (D1): whole — finalize_muse fits it at its destination
            xk = [str(k) for k in e if k not in MUSE_WHAT_CHANGED_KEYS]               # review #7 (b): a kept entry's other keys
            if xk:
                drops.append(_mdrop("list", f"what_changed[{j}]", NR.extra_keys_reason(xk), "keys: " + json.dumps(xk, ensure_ascii=False)))
    if len(v) > MUSE_WHAT_CHANGED_MAX:
        drops.append(_mdrop("list", "what_changed", f"{len(v) - MUSE_WHAT_CHANGED_MAX} more than {MUSE_WHAT_CHANGED_MAX} — not used"))
    return kept, drops


def run_muse_update(api_key: str, base, changes, cv, api_summary: str, oracle_block: str, parts: str = "", reg=None):
    """D7: ONE update call over Block 1's campaign; returns (strategic, creative, info). A failed call, an unparseable reply or a
    merged campaign that does not validate → Block 1's campaign, status "failed: <reason>", ⚠ printed — never raised.
    P0.7 r2 (E2, E3): the reply's text kept the moment it arrives, before parsing (checkpoints/muse_update_reply_<run id>.txt, its
    name in info["reply_file"] → muse_summary.json); every part of the reply not applied one entry of info["dropped"]; every text
    cut marked (NR.cut) and printed once. Phase 5: a top-level key the update does not ask for (C14), text outside the reply's first
    fenced block (C16) and a key given twice in one object (the review's #7 a) are entries too ('the reply').
    BUILD IC-6 (D12, D8): with NAIPC_IC6_MUSE_UPDATE on (NR.call_form "lines") the update is keyed edits (run_muse_update_edits; `reg`,
    this run's registry, orders its review set); off, this request and merge — IC-5's, byte for byte but max_tokens."""
    NR.mark_path("update.keyed_lines" if NR.call_form("muse/update") == "lines" else "update.whole_object")   # BUILD IC-8 (D14)
    if NR.call_form("muse/update") == "lines":
        return run_muse_update_edits(api_key, base, changes, cv, api_summary, oracle_block, parts, reg)
    refs = NR.curveball_refs(changes) if changes else {"announcement": "announcement"}
    info = {"status": "", "what_changed": [], "amended": [], "dropped": [], "crisis_posts": 0, "reply_file": None}
    system = ("You are an expert civic marketing strategist updating an outreach campaign for a mid-competition announcement. Output ONLY "
              "valid JSON — no markdown, no explanation, no code fences.")
    prompt = muse_update_prompt(base, changes, cv, api_summary, oracle_block, parts)
    NR.say(f"    curveball update: one call over Block 1's campaign (run {base['meta'].get('run_id')}) — prompt {len(prompt):,} characters")
    raw = None
    for attempt in range(MAX_RETRIES):
        try:
            _require_budget("the curveball update" + (" retry" if attempt else ""), "muse/update", retry=bool(attempt))   # IC-4 (D9): after its wait
            console.print(f"  [cyan]Curveball update — attempt {attempt + 1}[/cyan]")
            raw = call_claude(api_key, system, prompt, "muse/update")
            break
        except Exception as e:  # noqa: BLE001
            if attempt < MAX_RETRIES - 1 and is_retryable(e):
                _sleep(_retry_wait(e, attempt + 1))
                continue
            NR.say(f"    ⚠ curveball update failed — {type(e).__name__}: {str(e)[:200]}; the campaign is Block 1's, unchanged")
            return base["strategic"], base["creative"], dict(info, status=f"failed: {type(e).__name__}")
    return _muse_object_update(base, raw, refs, info)


def _muse_object_update(base, raw, refs, info):
    """D7's reply read — IC-5's whole-field update (BUILD IC-6 D12: moved from run_muse_update unchanged; also a reply to the edit
    request that is one JSON object, or no JSON at all): the reply kept before parsing, parsed (_extract_json), merged (merge_muse_update),
    what_changed taken, its own problems named (C14, C16, review #7 a). → (strategic, creative, info), as run_muse_update."""
    info["reply_file"] = keep_reply("muse_update_reply", raw)     # E3 (d): before parsing, so a reply that does not parse is kept too
    try:
        reply = _extract_json(raw)
        if not isinstance(reply, dict):
            raise ValueError(f"a {type(reply).__name__}, not an object")
    except Exception as e:  # noqa: BLE001
        keep_partial("update-reply", 1, raw or "")
        NR.say(f"    ⚠ curveball update failed — the reply is not usable JSON ({type(e).__name__}); the campaign is Block 1's, unchanged")
        return base["strategic"], base["creative"], dict(info, status=f"failed: the reply is not JSON ({type(e).__name__})")
    _UPDATE_ENTRIES.clear()         # BUILD IC-7 Phase 4 (D2): the object form's declared conversions, recorded as the lines' are (:2307)
    try:
        s, c, minfo = merge_muse_update(base, reply, refs)
    except Exception as e:  # noqa: BLE001 — not expected: every kept part validated with Block 1's others
        NR.admission_record(HARNESS_DIR / "checkpoints", "muse/update", list(_UPDATE_ENTRIES), "not applied", 1)       # BUILD IC-7 (D2)
        NR.say(f"    ⚠ curveball update failed — the merged campaign does not validate ({type(e).__name__}); the campaign is Block 1's")
        return base["strategic"], base["creative"], dict(info, status="failed: the merged campaign does not validate")
    NR.admission_record(HARNESS_DIR / "checkpoints", "muse/update", list(_UPDATE_ENTRIES), "amendments checked", 1)    # BUILD IC-7 (D2)
    NR.say(NR.admission_line("muse/update", list(_UPDATE_ENTRIES), "amendments checked"))
    info.update(minfo)
    info["what_changed"], d1 = muse_take_what_changed(reply, refs)
    unknown = [k for k in reply if k not in MUSE_REPLY_KEYS]       # P0.7 r2 Phase 5 (C14): a top-level key the update does not ask for
    d0 = [_mdrop("shape", "reply", NR.unknown_keys_reason(unknown), "keys: " + json.dumps(unknown, ensure_ascii=False))] if unknown else []
    fence = re.search(r'```(?:json)?\s*\n?(.*?)```', raw or "", re.DOTALL)   # Phase 5 (C16): _extract_json reads the first fenced block only
    n_out = len(((raw or "")[:fence.start()] + (raw or "")[fence.end():]).strip()) if fence else 0
    d0 += [_mdrop("shape", "reply", NR.outside_text_reason(n_out, info["reply_file"]), "outside _extract_json's fenced block")] if n_out else []
    dups = REG.admit(NR.duplicate_keys(fence.group(1).strip() if fence else (raw or "").strip()), "reply", "update keys")   # BUILD IC-5 (D1); review #7 (a): the text _extract_json read
    d0 += [_mdrop("shape", "reply", NR.duplicate_keys_reason(dups), "keys: " + json.dumps(dups, ensure_ascii=False))] if dups else []
    for d in d1 + d0:
        NR.say(f"    update: dropped {d['field']} — {d['why']}")
    info["dropped"] = minfo["dropped"] + d1 + d0
    NR.say(f"    update: {info['crisis_posts']} crisis post(s), {len(info['amended'])} amendment(s) kept; dropped {len(info['dropped'])}")
    return s, c, dict(info, status="applied")


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD IC-6 (D12; G1, G3, W4, W5) — the update as keyed edits (NAIPC_IC6_MUSE_UPDATE): the Oracle's form (IC-4 D11) over the campaign
# ═══════════════════════════════════════════════════════════════════════════════
MUSE_WHOLE_IDS = {"m.hero_title": "strategic.hero_title", "m.hero_lead": "strategic.hero_lead", "m.short_tagline": "strategic.short_tagline",
                  "m.og_description": "strategic.og_description", "m.persona_text": "strategic.persona_text",
                  "m.hero_stats": "strategic.hero_stats", "m.flyer": "creative.flyer", "m.pitch": "creative.partner_pitch"}
MUSE_WHOLE_KINDS = {"strategic.hero_title": "text", "strategic.hero_lead": "text", "strategic.persona_text": "text",
                    "strategic.short_tagline": "tagline", "strategic.og_description": "tagline", "strategic.hero_stats": "hero_stats",
                    "creative.flyer": "flyer", "creative.partner_pitch": "pitch"}
MUSE_LIST_KIND = {f: k for f, (k, _l) in REG.MUSE_ITEM_KINDS.items()}     # "strategic.personas" → "persona"
MUSE_KIND_MODEL = {"persona": Persona, "channel": ChannelPlan, "phase": CalendarPhase, "kpi": KPI, "post": SocialPost, "crisis_post": SocialPost}
MUSE_ADD_KINDS = ("crisis_post", "impact", "persona", "channel", "phase", "kpi", "post")
MUSE_REMOVE_KINDS = tuple(MUSE_LIST_KIND.values())       # a list's items; a whole field is replaced or kept
MUSE_KIND_NOUN = {"persona": "persona", "channel": "channel", "phase": "phase", "kpi": "KPI", "post": "post", "crisis_post": "crisis post",
                  "impact": "what-changed entry"}
MUSE_NAME_KEYS = {"persona": "name", "channel": "channel", "phase": "phase_name"}   # the stale check: the names other items may repeat
MUSE_EDIT_INLINE_CHARS = 240       # a review-set value longer than this is printed as a pointer into <muse_block1>
MUSE_EDIT_SYSTEM = ("You are an expert civic marketing strategist updating an outreach campaign for a mid-competition announcement. Output "
                    "ONLY JSON Lines — one JSON object on each line, no markdown, no explanation, no code fences.")
MUSE_UPDATE_DECLINED_LINE = ("The campaign was not updated for the curveball: the model declined the update, so nothing of its reply was "
                             "applied and no item was reviewed; this is Block 1's campaign, and the data changes below are computed from the API.")
MUSE_EDIT_STATUS_WORDS = {"not reviewed": "not reviewed at the curveball", "not applied": "its edit at the curveball was not applied"}


def muse_lists(s_dump, c_dump):
    """{field: its items} — the Muse's lists (REG.MUSE_ITEM_KINDS) of a campaign as stored (model_dump dicts)."""
    out = {}
    for f in REG.MUSE_ITEM_KINDS:
        top, sub = f.split(".", 1)
        v = (s_dump if top == "strategic" else c_dump).get(sub)
        out[f] = list(v) if isinstance(v, list) else []
    return out


def _mtexts(v):
    """Every text value inside a value (a string, or the strings of a list or an object, at any depth)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _mtexts(x)
    elif isinstance(v, list):
        for x in v:
            yield from _mtexts(x)


def _plain_words(s):
    """A text as words: a token's words kept (its id set aside), tags and markup set aside, spacing collapsed."""
    t = REG.TOKEN_RE.sub(lambda m: " " + (m.group(1).split("|", 1)[1] if "|" in m.group(1) else "") + " ", str(s or ""))
    return " ".join(re.sub(r"<[^>]+>", " ", t).split())


def muse_review_set(base, reg=None):
    """BUILD IC-6 (D12): every item of Block 1's campaign by id — {"kind", "value", "base" (NR.edit_base of the value as <muse_block1>
    prints it), "where": (field, its index in the list or None)} — and Python's order: (1) the items whose tokens name a figure the
    curveball changed (REG.cites_changed on this run's registry), then (2) the rest; each group in the campaign's order — the personas, the
    channels, the phases, the KPIs, the strategic fields, the posts, the flyer, the pitch (each persona before the channels and posts). The
    list items' ids are base["item_ids"] (Block 1's, or assigned once at load); a whole field's is MUSE_WHOLE_IDS's. → (items, order)."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()
    ids = REG.muse_item_ids(muse_lists(s0, c0), base.get("item_ids"))[0]
    whole = {v: k for k, v in MUSE_WHOLE_IDS.items()}
    items, seq = {}, []
    for top, model, d in (("strategic", MuseStrategic, s0), ("creative", MuseCreative, c0)):
        for sub in model.model_fields:
            f = f"{top}.{sub}"
            if f in REG.MUSE_ITEM_KINDS:
                for j, x in enumerate(d.get(sub) or []):
                    items[ids[f][j]] = {"kind": MUSE_LIST_KIND[f], "value": x, "base": NR.edit_base(x), "where": (f, j)}
                    seq.append(ids[f][j])
            elif f in whole:
                items[whole[f]] = {"kind": MUSE_WHOLE_KINDS[f], "value": d.get(sub), "base": NR.edit_base(d.get(sub)), "where": (f, None)}
                seq.append(whole[f])
    marks = REG.changed_marks(reg)
    first = [i for i in seq if REG.cites_changed(reg, list(_mtexts(items[i]["value"])), marks)]
    return items, first + [i for i in seq if i not in first]


def muse_review_set_block(items, order) -> str:
    """BUILD IC-6 (D12): the request's <review_set> in the Oracle's form (review_set_block) — one JSON line per item, in Python's order:
    {"id", "kind", "base", "value"}; a value longer than MUSE_EDIT_INLINE_CHARS printed as "see" a pointer into <muse_block1>."""
    out = ["<review_set>"]
    for i in order:
        it = items[i]
        row = {"id": i, "kind": it["kind"], "base": it["base"]}
        f, j = it["where"]
        if len(json.dumps(it["value"], ensure_ascii=False, separators=(", ", ": "))) <= MUSE_EDIT_INLINE_CHARS:
            row["value"] = it["value"]
        else:
            row["see"] = f"<muse_block1> {f}" + (f"[{j}]" if j is not None else "")
        out.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(out + ["</review_set>"])


def muse_update_edits_prompt(base, items, order, changes, cv, api_summary: str, oracle_block: str, parts: str = "") -> str:
    """BUILD IC-6 (D12): the edit update's request — IC-5's context, Block 1's campaign, the change list, this run's data and the
    registry's parts, then <review_set> and the rules: one line per item, keep / replace / remove / add, each with its own because; the
    caps and the schema blocks IC-5's update stated (each from the constant its check uses). Event-neutral."""
    b1 = {"strategic": base["strategic"].model_dump(), "creative": base["creative"].model_dump()}
    sw = {"same_words": NR.CHANGES_SAME_IC4} if NR.ic4_prompt() else {}
    eng_c = "" if parts else ", likes, comments, shares"
    chg = (NR.changes_prompt_block(changes, **sw) if changes else
           NR.changes_prompt_block({"curveball": cv, "endpoints": None, "snapshot": {"source": "none"}}, **sw))
    crisis_w = ("use only names from the data, <changes> or Block 1's campaign, and write every quantity as <number_rules> says" if parts
                else "use only names and numbers from the data, <changes> or Block 1's campaign")
    return f"""<context>
This is BLOCK 2 of a two-block competition. In Block 1 you wrote the community outreach campaign in <muse_block1> for this data. At the
curveball the organizers made the announcement below and the data changed; Python compared every endpoint record by record with Block 1's
snapshot (<changes>). REVIEW every item of the campaign against what changed: keep what the curveball does not touch, replace what it
changes, remove what it ends and add what it needs, one line for each item of <review_set>.
</context>
{oracle_block}
{MUSE_UPDATE_TAG}
{json.dumps(b1, indent=1, ensure_ascii=False)}
</muse_block1>

{chg}

<civic_data>
{api_summary}
</civic_data>

{parts}{muse_review_set_block(items, order)}

<rules>
Reply with JSON Lines and nothing else: one JSON object on each line, no code fences, no prose, no line break inside an object. Write one
line for each item of <review_set>, in its order, then a line for each item you add. Python applies each line that checks. A line it
cannot apply is named on the page, and its item keeps Block 1's words; an item with no line is named on the page as not reviewed at the
curveball.
0. You may begin with one line naming the items you mean to change: {{"op": "scope", "ids": ["<id>", ...], "why": "<one sentence>"}}.
   Python compares it with your lines and records any difference; it never rejects a line by it.
1. An item the curveball leaves as it is: {{"op": "keep", "id": "<its id>", "base": "<its base, as printed>"}}.
2. An item the curveball changes: {{"op": "replace", "id": "<its id>", "base": "<its base>", "value": <its complete new value, in its
   kind's form>, "because": ["<ref>", ...], "why": "<one sentence, at most {NR.EDIT_WHY_CHARS} characters>"}}. An item of a list (kinds
   persona, channel, phase, kpi, post) that the curveball ends: {{"op": "remove", "id": "<its id>", "base": "<its base>", "because":
   ["<ref>", ...]}}; a whole field is replaced or kept.
3. A new item: {{"op": "add", "id": "new.<n>" (n counts from 1, each number once), "kind": "<one of: {", ".join(MUSE_ADD_KINDS)}>",
   "value": <its value>, "because": ["<ref>", ...]}}.
4. because: refs from <changes>, exactly as written there — "announcement", an endpoint's name or a record's ref. Every line that
   replaces, removes or adds carries its own because; a line whose reasons are not changes in the list is not applied.
5. Kind crisis_post — one to {MUSE_CRISIS_MAX} social posts that respond to the curveball, each with the fields of Block 1's social posts
   (platform, card_class "photo", "micro" or "feed", location, language, image_headline, image_desc, text, hashtags{eng_c}); {crisis_w}.
   The page shows {MUSE_POSTS_SHOWN} social posts at most, the crisis posts first. Kind impact — one for each change that matters for the
   campaign, at most {MUSE_WHAT_CHANGED_MAX}: {{"ref": "<a ref>", "impact": "<1-2 plain sentences, at most {MUSE_IMPACT_CHARS:,} characters>"}}.
6. Each kind's form. persona, channel, phase, kpi, post, crisis_post: an object of the schema of an item of its list below
   (strategic.personas, strategic.channel_plan, strategic.calendar, strategic.kpis, creative.social_posts). text: a string. tagline: a
   string. hero_stats: the list strategic.hero_stats. flyer: the object creative.flyer. pitch: the object creative.partner_pitch. A value
   that breaks its schema is not applied, and the page says so; a list whose lines together break its bounds (minItems, maxItems) stays
   Block 1's, and the page names each of its lines. The schemas — the ones Block 1's campaign follows:
   strategic.<field>: {NR.schema_block(shown_schema(MuseStrategic), MUSE_STRATEGIC_FIELDS)}
   creative.<field>: {NR.schema_block(shown_schema(MuseCreative), MUSE_CREATIVE_FIELDS)}
   {MUSE_ITEM_KEYS_LINE}
   {MUSE_FREE_KEYS_LINE}
   {MUSE_TAGLINE_LINE}
7. Plain text in every new value; HTML only where Block 1's field already holds it.
</rules>

Return ONLY the JSON lines: one for each item of <review_set>, in its order, then one for each item you add."""


def muse_edit_validator(base, refs, notes):
    """BUILD IC-6 (D12): validate(kind, value, op, id) for NR.apply_edits — IC-5's checks, one item at a time, every value escaped (and a
    post's card style made safe) BEFORE its check, so what is checked is what is stored: a list item through its own model (Persona,
    ChannelPlan, CalendarPhase, KPI, SocialPost); a whole field through muse_field_check (the tagline's limits on the value as written,
    then the campaign's model with Block 1's other fields); an impact's ref a change of the list, its words whole (finalize_muse fits them). A key
    an item's model does not declare, a free-key item's key the page does not read (C2, review #7 b) and a card style the page does not have
    are named in `notes` ({id: {"keys": [...], "card": bool}}) and the rest stored. → (stored, None) or (None, (why, detail))."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()

    def note(i, keys=(), card=False):
        n = notes.setdefault(i, {"keys": [], "card": False})
        n["keys"] += [k for k in keys if k not in n["keys"]]
        n["card"] = n["card"] or card

    def v(kind, value, op, i):
        try:
            if kind == "impact":
                if not isinstance(value, dict):
                    return None, ('an impact is {"ref", "impact"}', f"a {type(value).__name__}")
                r_, txt = value.get("ref"), NR.clean_text(value.get("impact"))
                if not (isinstance(r_, str) and r_.strip().lower() in refs):
                    return None, (f"its ref is not a change in the list ({NR.name_cut([r_])})", "")
                if not txt:
                    return None, ("no impact given", "")
                xk = [str(k) for k in value if k not in MUSE_WHAT_CHANGED_KEYS]
                if xk:
                    note(i, xk)
                return {"ref": refs[r_.strip().lower()], "impact": txt}, None      # BUILD IC-8 (D1): whole — finalize_muse fits it
            if kind in MUSE_KIND_MODEL:
                noun = MUSE_KIND_NOUN[kind]
                if not isinstance(value, dict):
                    return None, (f"a {noun} is an object of its schema's keys", f"a {type(value).__name__}")
                esc = NR.escape_markup(value)
                card = kind in ("post", "crisis_post") and esc.get("card_class") not in CARD_CLASSES
                if kind in ("post", "crisis_post"):
                    esc = _safe_post(esc)
                model = MUSE_KIND_MODEL[kind]
                esc = update_walk(_KIND_FIELD[kind], esc, item=True)        # BUILD IC-7 (D2): the declared conversions, counted
                try:
                    stored = model.model_validate(esc, strict=True).model_dump()      # BUILD IC-7 (Q-STRICT)
                except Exception as e:  # noqa: BLE001 — every failure named in plain words (E3 a)
                    return None, muse_reason(e, None, muse_model_schema(model))
                xk = NR.extra_keys(esc, stored)
                fk = []
                if kind == "persona":
                    (stored,), fk = _free_keys_out("strategic.personas", [stored])
                if xk or fk or card:
                    note(i, xk + [k for k in fk if k not in xk], card)
                return stored, None
            f = MUSE_WHOLE_IDS.get(i)
            if f is None:
                return None, ("no check for this kind", str(kind))
            esc = update_walk(f, NR.escape_markup(value))          # BUILD IC-7 (D2): the declared conversions, counted
            bad = muse_field_check(s0, c0, f, esc, value)
            if bad:
                return None, bad
            top, sub = f.split(".", 1)
            stored = (MuseStrategic if top == "strategic" else MuseCreative).model_validate(dict(s0 if top == "strategic" else c0, **{sub: esc}),
                                                                                         strict=True).model_dump()[sub]     # IC-7 (Q-STRICT)
            xk = NR.extra_keys(esc, stored)
            stored, fk = _free_keys_out(f, stored)
            if xk or fk:
                note(i, xk + [k for k in fk if k not in xk])
            return stored, None
        except Exception as e:  # noqa: BLE001 — a value Python cannot check is not applied
            return None, ("the value could not be checked", type(e).__name__)
    return v


def call_muse_update_lines(api_key: str, system: str, prompt: str, order=None):
    """BUILD IC-6 (D12, D6): the edit update's one call through NR.line_call — its profile ("muse/update": line form while
    NAIPC_IC6_MUSE_UPDATE is on, its first line's time the start rule's need), the Muse's stage and the block's clock; after any cut but a
    refusal the complete lines; one continuation (a token or transport cut, NAIPC_IC6_CONTINUE on, its first line's time left), asked to go
    on from the next item of `order`. → (text, how) as the Oracle's call_update_edits; (None, "declined (<category>)") for a refusal.
    Raises when no complete line came and no retry is left. Its whole result is kept as call_muse_update_lines.last."""
    label = "the curveball update"
    begins = _STAGE_T0 is None                     # the update is the Muse's first call in Block 2: it begins the stage
    end = _clock() + _budget_left()

    def refuse(words):
        raise StageBudgetExceeded(words)
    res = NR.line_call("muse/update", lambda msgs, lab: call_claude(api_key, system, None, "muse/update", messages=msgs, label=lab), prompt,
                       end_at=end, clock=_clock, retryable=is_retryable, retry_wait=_retry_wait, sleep=_sleep, refuse=refuse,
                       log_dir=HARNESS_DIR / "checkpoints", model=MODEL, stated_rate=MUSE_RATE, begins=begins, stage_s=MUSE_STAGE_BUDGET_S,
                       key_of=lambda o: str(o.get("id")) if o.get("id") is not None else None, resume=NR.edit_resume(order),
                       continue_ok=NR.ic6("NAIPC_IC6_CONTINUE"), stage_label=label)
    call_muse_update_lines.last = res
    c_ = res.get("continuation")
    if c_:
        NR.say(f"    {label}: continuation — {c_['how']}" + (f"; {c_['lines']} line(s) more" if c_.get("label") else "")
               + (f"; {len(c_['dropped'])} written again, dropped" if c_.get("dropped") else ""))
    how = res["how"]
    if c_ and c_.get("label"):
        how += f"; continued: {c_['lines']} more line(s), the continuation's ending: {c_['how']}"
    return res["text"], how


def _muse_assemble(old, old_ids, kind, field, by_id, adds, used):
    """One list after the edits, in Block 1's order — a removed item left out (its id a tombstone), a replaced item its new value, a kept or
    unreviewed item as it was — then the adds of its kind in the reply's order, each with the next free id of its kind (never a used or
    retired one: REG.assign_muse_ids). → (items, ids, tombstones, {new.<n>: its id}, the lines that changed the list)."""
    out, oid, tomb, assigned, changed = [], [], [], {}, []
    for j, x in enumerate(old):
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
    for a, nid in zip(mine, REG.assign_muse_ids(field, len(mine), set(used) | {x for x in oid if x})):
        out.append(a["value"])
        oid.append(nid)
        assigned[a["id"]] = nid
        changed.append(a)
    return out, oid, tomb, assigned, changed


def muse_stale(items, order, by_id, status):
    """BUILD IC-6 (D12; W4, W5): after the edits, every item whose Block 1 words stand (kept, not reviewed, its edit not applied) and whose
    text names, as whole words, a persona, a channel or a phase the update renamed or removed → [{"id", "names": [Block 1's name, …]}], in
    the review set's order. The check changes nothing: the item is marked "stale: names <x>, revised at the curveball" where it is shown."""
    gone = []
    for i, a in by_id.items():
        it = items.get(i) or {}
        key = MUSE_NAME_KEYS.get(it.get("kind"))
        if not key or status.get(i) not in ("replaced", "removed") or not isinstance(it.get("value"), dict):
            continue
        old = _plain_words(it["value"].get(key))
        new = _plain_words(a["value"].get(key)) if a["op"] == "replace" and isinstance(a.get("value"), dict) else ""
        if old and (a["op"] == "remove" or new.lower() != old.lower()) and old not in gone:
            gone.append(old)
    out = []
    for i in order if gone else []:
        if status.get(i) not in ("kept", "not reviewed", "not applied"):
            continue
        txt = " ".join(_plain_words(t) for t in _mtexts(items[i]["value"]))
        hit = [x for x in gone if re.search(r"(?<!\w)" + re.escape(x) + r"(?!\w)", txt, re.I)]
        if hit:
            out.append({"id": i, "names": hit})
    return out


def muse_item_name(i, items) -> str:
    """BUILD IC-6 (D12): an item of the review set by its place, in Python's words — a whole field by its page name (MUSE_FIELD_NAMES), a
    list item as "Block 1's <noun> <its number in Block 1's list>"; a new item "the update's new item <new.n>"; a line by its number."""
    s = str(i or "")
    if s in MUSE_WHOLE_IDS:
        return MUSE_FIELD_NAMES[MUSE_WHOLE_IDS[s]]
    it = (items or {}).get(s) or {}
    w = it.get("where")
    at = it.get("at") or ((w[1] + 1) if isinstance(w, (list, tuple)) and len(w) > 1 and isinstance(w[1], int) else None)   # the review set's
    if at:                                                                            # item, or the record's ("at": its number from 1)
        return f"Block 1's {MUSE_KIND_NOUN.get(it.get('kind'), it.get('kind'))} {at}"
    if s.startswith("new."):
        return f"the update's new item {s}"
    if s == "what_changed":
        return "what changed"
    if s.startswith("line "):
        return f"{s} of the reply"
    if s == "the last line":
        return "the reply's last line"
    return f'the update\'s id "{NR.name_cut([s], quote=False)}"'


def muse_edits_to_update(base, items, order, res, problems, partial, how, notes, n_read=0, scope=None, meant=(), add_kind=None):
    """BUILD IC-6 (D12): the applied lines → (strategic, creative, info) — the campaign after the edits and the record: amended (a whole field
    replaced; a list any line changed), crisis_posts (how many shown first), what_changed (the impact adds), dropped (every line not
    applied, every problem of the reply, its partial last line, every key not used — each with its page name), and "edits": how the reply
    ended, the review set's size, the lines read and applied, each item's status (kept, replaced, removed, added, not applied, not
    reviewed), the items not reviewed, each not-applied item's reason, each item's kind and first words, the ids after the update and the
    tombstones, where the ids came from, the scope line's comparison with the ids the lines meant to change (`meant`: every replace,
    remove and add line's id, applied or not) and the stale items. Groups: a list whose edits together break its
    bounds stays Block 1's, each of its lines named (the Oracle's rule). The posts: the crisis posts first (at most MUSE_CRISIS_MAX), then
    the posts after their edits, MUSE_POSTS_SHOWN at most — a post left out is named (the update's) or printed (Block 1's), its id
    retired. BUILD IC-8 (D3, I3): "not_used" — the proposed list: each add line no page item came from, once, by its line id (the first
    entry of an id) — a crisis post past MUSE_CRISIS_MAX or an impact past MUSE_WHAT_CHANGED_MAX (list_cap), an add of a group the update
    refused or an add line not applied (edit_not_applied); its kind the line's (`add_kind`: {line id: kind})."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()
    ids0 = REG.muse_item_ids(muse_lists(s0, c0), base.get("item_ids"))[0]
    status = dict(res["status"])
    why_not = {x["id"]: x["why"] for x in res["not_applied"] if x.get("id") in status}

    def drop(field, why, detail=""):
        d = _mdrop("edit", field, why, detail)
        d["name"] = ", ".join(muse_item_name(x.strip(), items) for x in str(field).split(", ")) if field else "the reply"
        return d
    drops = [drop(x["id"] or f"line {x['line']}", x["why"], f"line {x['line']}: {x['detail']}".rstrip(": ")) for x in res["not_applied"]]
    drops += [drop(f"line {p['line']}", p["why"], p.get("detail") or "") for p in problems]
    if partial:
        drops.append(drop("the last line", "the reply was cut inside its last line, so that line is not applied", f"{partial} characters"))
    drops += [drop(f"line {x['line']}", NR.extra_keys_reason(x["keys"]), "keys: " + json.dumps(x["keys"], ensure_ascii=False))
              for x in res["extra_keys"]]
    for i, n_ in notes.items():
        if n_["keys"]:
            drops.append(drop(i, NR.extra_keys_reason(n_["keys"]), "keys its schema does not declare or the page does not read: "
                              + json.dumps(n_["keys"], ensure_ascii=False)))
        if n_["card"]:
            drops.append(drop(i, "its card style is not one the page has — shown as a micro post"))

    def refuse(group, why):
        for a in group:
            if a["op"] != "add":
                status[a["id"]] = "not applied"
                why_not[a["id"]] = why
        drops.append(drop(", ".join(dict.fromkeys(a["id"] for a in group)), why))
    applied = list(res["applied"])
    by_id = {a["id"]: a for a in applied if a["op"] != "add"}
    adds = [a for a in applied if a["op"] == "add"]
    S, C = dict(s0), dict(c0)            # a field is replaced whole, never changed in place: Block 1's dicts stay as they were
    amended = {}
    for i, a in by_id.items():               # a whole field: its new value (each checked with Block 1's other fields)
        w = (items.get(i) or {}).get("where") or (None, None)
        if a["op"] == "replace" and w[0] and w[1] is None:
            top, sub = w[0].split(".", 1)
            (S if top == "strategic" else C)[sub] = a["value"]
            amended[w[0]] = list(a["because"])
    used = {x for v in ids0.values() for x in v}
    new_ids, tombs, assigned, refused_ids = {}, [], {}, set()
    crisis_all = [a for a in adds if a["kind"] == "crisis_post"]
    crisis = crisis_all[:MUSE_CRISIS_MAX]
    if len(crisis_all) > MUSE_CRISIS_MAX:
        drops.append(drop(", ".join(a["id"] for a in crisis_all[MUSE_CRISIS_MAX:]),
                          f"{len(crisis_all) - MUSE_CRISIS_MAX} crisis post(s) more than {MUSE_CRISIS_MAX} — not used"))
    n_crisis = 0
    for f, kind in MUSE_LIST_KIND.items():
        top, sub = f.split(".", 1)
        D = S if top == "strategic" else C
        old = list((s0 if top == "strategic" else c0).get(sub) or [])
        out, oid, tomb, asg, changed = _muse_assemble(old, ids0[f], kind, f, by_id, adds, used)
        cr_ids = REG.assign_muse_ids(f, len(crisis), set(used) | set(oid)) if kind == "post" else []

        def candidate(lst, lid):
            if kind != "post":
                return lst, lid
            return ([a["value"] for a in crisis] + lst)[:MUSE_POSTS_SHOWN], (cr_ids + lid)[:MUSE_POSTS_SHOWN]
        cand, cid = candidate(out, oid)
        if changed or (kind == "post" and crisis):
            try:
                (MuseStrategic if top == "strategic" else MuseCreative)(**dict(D, **{sub: cand}))
                ok = True
            except Exception as e:  # noqa: BLE001 — the list as one group: its edits together break its bounds
                ok, (why_g, _det) = False, muse_reason(e, sub, muse_field_schema(f))
            if not ok and changed:
                refuse(changed, f"{MUSE_FIELD_NAMES[f]} with these edits does not fit the campaign's format ({why_g}) — none of them is applied")
                refused_ids |= {a["id"] for a in changed}
                out, oid, tomb, asg, changed = old, list(ids0[f]), [], {}, []
                cand, cid = candidate(out, oid)
        if kind == "post":
            n_crisis = len(crisis)
            left = [(x, i_) for x, i_ in zip(([a["value"] for a in crisis] + out)[MUSE_POSTS_SHOWN:], (cr_ids + oid)[MUSE_POSTS_SHOWN:])]
            mine = [i_ for _x, i_ in left if i_ not in ids0[f]]
            if mine:
                drops.append(drop(", ".join(mine), f"{len(mine)} of the update's posts not shown — the page shows {MUSE_POSTS_SHOWN} at most"))
            if len(left) > len(mine):
                NR.say(f"    update: {len(left) - len(mine)} of Block 1's posts not shown (the page shows six posts at most)")
            tomb += [{"id": i_, "kind": kind, "removed_by": f"not shown: the page shows {MUSE_POSTS_SHOWN} posts at most"} for _x, i_ in left]
            for a, nid in zip(crisis, cr_ids):
                assigned[a["id"]] = nid
                if nid in cid:
                    status[nid] = "added"
        D[sub] = cand
        used |= set(oid) | set(cr_ids)
        new_ids[f] = cid
        tombs += tomb
        for a_id, nid in asg.items():
            assigned[a_id] = nid
            if nid in cid:
                status[nid] = "added"
        if changed:
            amended[f] = list(dict.fromkeys(r for a in changed for r in a["because"]))
    impacts_all = [a["value"] for a in adds if a["kind"] == "impact"]
    unused = ({a["id"] for a in crisis_all[MUSE_CRISIS_MAX:]}             # lines applied but not used: past a cap
              | {a["id"] for a in [a_ for a_ in adds if a_["kind"] == "impact"][MUSE_WHAT_CHANGED_MAX:]})
    if len(impacts_all) > MUSE_WHAT_CHANGED_MAX:
        drops.append(drop("what_changed", f"{len(impacts_all) - MUSE_WHAT_CHANGED_MAX} more than {MUSE_WHAT_CHANGED_MAX} — not used"))
    not_used, seen_nu = [], set()                         # BUILD IC-8 (D3, I3): the proposed list's lines no page item came from

    def nu_(i, kind, reason):
        if isinstance(i, str) and i and i not in seen_nu:
            seen_nu.add(i)
            not_used.append({"id": i, "kind": kind, "reason": reason})
    for a in crisis_all[MUSE_CRISIS_MAX:]:
        nu_(a["id"], "crisis_post", "list_cap")
    for a in [a_ for a_ in adds if a_["kind"] == "impact"][MUSE_WHAT_CHANGED_MAX:]:
        nu_(a["id"], "impact", "list_cap")
    for a in adds:
        if a["id"] in refused_ids:
            nu_(a["id"], a["kind"], "edit_not_applied")
    for x in res["not_applied"]:
        if x.get("id") in (add_kind or {}):
            nu_(x["id"], (add_kind or {}).get(x["id"]), "edit_not_applied")
    s, c = MuseStrategic(**S), MuseCreative(**C)          # the stored form (each part already validated; the caller catches)
    stale = muse_stale(items, order, by_id, status)
    sc = None
    if scope is not None:     # D12: the plan is a hypothesis — compared with the lines, never used to reject one
        named = [str(x) for x in (scope.get("ids") if isinstance(scope.get("ids"), list) else [])]
        did = list(dict.fromkeys(str(x) for x in meant or ()))
        sc = {"line": scope.get("line"), "ids": named, "why": NR.cut(NR.clean_text(scope.get("why")) or "", NR.EDIT_WHY_CHARS, what="the scope line's reason"),
              "changed_not_named": [i for i in did if i not in named], "named_not_changed": [i for i in named if i not in did]}
    counts = {k: sum(1 for v in status.values() if v == k) for k in ("kept", "replaced", "removed", "added", "not applied", "not reviewed")}
    words = {i: {"kind": items[i]["kind"], "at": (items[i]["where"][1] + 1) if items[i]["where"][1] is not None else None,
                 "words": NR.cut(" ".join(_mtexts(items[i]["value"])) or json.dumps(items[i]["value"], ensure_ascii=False), 80)} for i in order}
    info = {"amended": [{"field": f, "because": rs} for f, rs in amended.items()], "dropped": drops, "crisis_posts": n_crisis,
            "what_changed": impacts_all[:MUSE_WHAT_CHANGED_MAX],
            "edits": {"form": "lines", "how": how, "review_set": len(order), "lines": n_read,
                      "applied": sum(1 for a in applied if a["id"] not in refused_ids and a["id"] not in unused),
                      "counts": counts, "status": status, "not_reviewed": [i for i in order if status.get(i) == "not reviewed"],
                      "why_not": why_not, "items": words, "ids": new_ids, "tombstones": tombs, "added_ids": assigned,
                      "ids_source": base.get("ids_source") or "stored", "scope": sc, "stale": stale, "not_used": not_used}}
    for d in drops:
        NR.say(f"    update: not applied {d['field']} — {d['why']}")
    return s, c, info


def _muse_edits_unread(items, order, how, base) -> dict:
    """The edit record of a reply that was not read (a failed call, a refusal): every item not reviewed, Block 1's ids."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()
    return {"form": "lines", "how": how, "review_set": len(order), "lines": 0, "applied": 0,
            "counts": {"kept": 0, "replaced": 0, "removed": 0, "added": 0, "not applied": 0, "not reviewed": len(order)},
            "status": {i: "not reviewed" for i in order}, "not_reviewed": list(order), "why_not": {},
            "items": {i: {"kind": items[i]["kind"], "at": (items[i]["where"][1] + 1) if items[i]["where"][1] is not None else None,
                          "words": NR.cut(" ".join(_mtexts(items[i]["value"])) or json.dumps(items[i]["value"], ensure_ascii=False), 80)}
                      for i in order},
            "ids": REG.muse_item_ids(muse_lists(s0, c0), base.get("item_ids"))[0], "tombstones": [], "added_ids": {},
            "ids_source": base.get("ids_source") or "stored", "scope": None, "stale": []}


def _is_edit_reply(raw) -> bool:
    """BUILD IC-6 (D12): True when a line of the reply decodes to one edit object (a JSON object with an "op") — the line form; anything
    else (one JSON object, IC-5's form, fenced or not; text that is no JSON) is read the IC-5 way."""
    for raw_line in (raw or "").split("\n"):
        s_ = raw_line.strip()
        if not s_.startswith("{"):
            continue
        try:
            o_, _f = NR.strict_loads(s_, "lines")
        except Exception:  # noqa: BLE001
            continue
        if isinstance(o_, dict) and "op" in o_:
            return True
    return False


def run_muse_update_edits(api_key: str, base, changes, cv, api_summary: str, oracle_block: str, parts: str = "", reg=None):
    """BUILD IC-6 (D12): the update as keyed edits — one line-form call over every item of Block 1's campaign; Python applies each line that
    checks (NR.apply_edits and the Muse's validators) and returns (strategic, creative, info) in run_muse_update's form, with "edits" (each
    item's status, the ids after it). Status "applied" when the reply was read — whole, or cut (its complete lines applied, the rest not
    reviewed, edits.how saying how it ended); "failed: declined (<category>)" for a refusal (nothing applied); "failed: <reason>" for a call
    that failed or a campaign that does not validate — then Block 1's campaign stands, every item not reviewed. A reply that is one JSON
    object (IC-5's form) is read the IC-5 way (_muse_object_update). Never raises."""
    refs = NR.curveball_refs(changes) if changes else {"announcement": "announcement"}
    items, order = muse_review_set(base, reg)
    prompt = muse_update_edits_prompt(base, items, order, changes, cv, api_summary, oracle_block, parts)
    NR.say(f"    curveball update: keyed edits over {len(order)} items of Block 1's campaign (run {base['meta'].get('run_id')}"
           + (", its ids assigned at load" if base.get("ids_source") == "assigned at load" else "") + f") — prompt {len(prompt):,} characters")
    info = {"status": "", "what_changed": [], "amended": [], "dropped": [], "crisis_posts": 0, "reply_file": None}

    def unchanged(status, how):
        return base["strategic"], base["creative"], dict(info, status=status, edits=_muse_edits_unread(items, order, how, base))
    try:
        console.print("  [cyan]Curveball update — keyed edits[/cyan]")
        raw, how = call_muse_update_lines(api_key, MUSE_EDIT_SYSTEM, prompt, order)
    except Exception as e:  # noqa: BLE001 — every GenerationError and transport failure: Block 1's campaign, every item not reviewed
        NR.say(f"    ⚠ curveball update failed — {type(e).__name__}: {str(e)[:200]}; the campaign is Block 1's, unchanged — no item was reviewed")
        return unchanged(f"failed: {type(e).__name__}", "failed")
    if raw is None:
        NR.say(f"    ⚠ curveball update {how} — its text is discarded and nothing is applied; the campaign is Block 1's — no item was reviewed")
        return unchanged(f"failed: {how}", how)
    if not _is_edit_reply(raw):         # one JSON object (IC-5's form), or no JSON at all: the IC-5 way, its words and records
        NR.say("    curveball update: the reply is not in lines — read the IC-5 way (one JSON object)")
        return _muse_object_update(base, raw, refs, dict(info, form="object"))
    info["reply_file"] = keep_reply("muse_update_reply", raw)       # before parsing, so a reply that does not parse is kept too
    rows, problems, partial = REG.admit(NR.parse_edit_lines(raw, complete=(how == "end_turn")), "reply", "update edit lines")   # IC-5 (D1)
    scope = None
    if rows and rows[0]["edit"].get("op") == "scope":     # D12: an optional first line — what the reply means to change
        r0 = rows.pop(0)
        scope = dict(r0["edit"], line=r0["line"])
    notes = {}
    _UPDATE_ENTRIES.clear()
    res = NR.apply_edits(rows, items, order, refs, muse_edit_validator(base, refs, notes), add_kinds=MUSE_ADD_KINDS,
                         remove_kinds=MUSE_REMOVE_KINDS)
    NR.admission_record(HARNESS_DIR / "checkpoints", "muse/update", list(_UPDATE_ENTRIES), "lines checked", 1)    # BUILD IC-7 (D2)
    NR.say(NR.admission_line("muse/update", list(_UPDATE_ENTRIES), "lines checked"))
    n_read = len(rows) + len(problems) + (1 if partial else 0) + (1 if scope is not None else 0)
    meant = [r["edit"]["id"] for r in rows if r["edit"].get("op") in ("replace", "remove", "add") and isinstance(r["edit"].get("id"), str)]
    try:
        s, c, minfo = muse_edits_to_update(base, items, order, res, problems, partial, how, notes, n_read=n_read, scope=scope, meant=meant,
                                           add_kind={r["edit"]["id"]: r["edit"].get("kind") for r in rows if r["edit"].get("op") == "add"
                                                     and isinstance(r["edit"].get("id"), str)})          # BUILD IC-8 (D3): the lines' kinds
    except Exception as e:  # noqa: BLE001 — not expected: every item and list passed on its own
        NR.say(f"    ⚠ curveball update failed — the edited campaign does not validate ({type(e).__name__}); the campaign is Block 1's — no item was reviewed")
        return unchanged("failed: the edited campaign does not validate", "failed")
    cont_ = ((getattr(call_muse_update_lines, "last", None) or {}).get("continuation") or {})      # D6: named, never applied twice
    for k_ in cont_.get("dropped") or []:
        d_ = _mdrop("edit", k_, "written again in the continuation")
        d_["name"] = muse_item_name(k_, items)
        minfo["dropped"].append(d_)
    info.update(minfo)
    e_ = info["edits"]
    n = e_["counts"]
    NR.say(f"    update: {n_read} line(s) read — kept {n['kept']}, replaced {n['replaced']}, removed {n['removed']}, added {n['added']}, "
           f"not applied {n['not applied']}, not reviewed {n['not reviewed']} of {len(order)}" + ("" if how == "end_turn" else f" (the reply: {how})"))
    for st_ in e_["stale"]:
        NR.say(f"    update: stale — {muse_item_name(st_['id'], items)} names {', '.join(st_['names'])}, revised at the curveball")
    if e_["scope"] and (e_["scope"]["changed_not_named"] or e_["scope"]["named_not_changed"]):
        NR.say(f"    update: the scope line and the lines differ — changed, not named: {len(e_['scope']['changed_not_named'])}; "
               f"named, not changed: {len(e_['scope']['named_not_changed'])}")
    return s, c, dict(info, status="applied")


def muse_update_parts(edits):
    """BUILD IC-6 (D12, D7's words): each part of the campaign's status from the edits' record — a list by its items (revised at the
    curveball: an item replaced, added or removed; removed at the curveball: every item removed; from Block 1 — its edit was not applied;
    from Block 1, not reviewed; reviewed, unchanged), a whole field by its own — and why (its items' counts, its stale items)."""
    e = edits if isinstance(edits, dict) else {}
    st = e.get("status") if isinstance(e.get("status"), dict) else {}
    stale = {x.get("id") for x in e.get("stale") or [] if isinstance(x, dict)}
    by = {}
    for i, s_ in st.items():
        f = MUSE_WHOLE_IDS.get(i) or next((f_ for f_, k_ in MUSE_LIST_KIND.items() if str(i).startswith(k_ + ".")), None)
        if f:
            by.setdefault(MUSE_FIELD_NAMES.get(f, f), []).append((s_, i in stale))
    out = []
    for part, sts in by.items():
        ss = [s_ for s_, _x in sts]
        c = {s_: ss.count(s_) for s_ in sorted(set(ss))}
        if c.get("removed") and len(c) == 1:
            status = "removed at the curveball"
        elif any(c.get(s_) for s_ in ("replaced", "added", "removed")):
            status = "revised at the curveball"
        elif c.get("not applied"):
            status = "from Block 1 — its edit was not applied"
        elif c.get("not reviewed"):
            status = "from Block 1, not reviewed"
        else:
            status = "reviewed, unchanged"
        n_st = sum(1 for _s, x in sts if x)
        why = ", ".join(f"{n_} {s_}" for s_, n_ in c.items()) + (f"; {n_st} stale" if n_st else "")
        out.append({"part": part, "status": status, "why": why})
    return out


def muse_item_marks(info) -> dict:
    """BUILD IC-6 (D12): the mark beside each list item on the page, by its place in the list as shown — {"status", "words" (an item not
    reviewed or whose edit was not applied: MUSE_EDIT_STATUS_WORDS), "stale" (the names it repeats, joined)} or None — from the update's
    record, only when the edits were applied. {field: [mark or None]}."""
    e = (info or {}).get("edits") if isinstance((info or {}).get("edits"), dict) else None
    if not e or str((info or {}).get("status") or "") != "applied":
        return {}
    st = e.get("status") or {}
    stale = {x["id"]: ", ".join(x.get("names") or []) for x in e.get("stale") or [] if isinstance(x, dict) and x.get("id")}
    out = {}
    for f, ids in (e.get("ids") or {}).items():
        row = []
        for i in ids:
            w = MUSE_EDIT_STATUS_WORDS.get(st.get(i), "")
            row.append({"status": st.get(i), "words": w, "stale": stale.get(i, "")} if (w or stale.get(i)) else None)
        out[f] = row
    return out


def muse_update_line(info):
    """BUILD IC-8 (D13): NR.update_status for this run's update — its edits, this invocation's dispositions (finalize_muse writes them
    before the page), the reply's add lines (info's reply_file), the notes on what changed (what_changed) and its crisis posts."""
    info = info if isinstance(info, dict) else {}
    rf = info.get("reply_file")
    return NR.update_status(info.get("edits"), NR.disposition_units(HARNESS_DIR / "checkpoints" / f"dispositions_{NR.run_id()}.jsonl"),
                            NR.reply_add_kinds(HARNESS_DIR / rf) if isinstance(rf, str) and rf else None,
                            notes=len(info.get("what_changed") or []), crisis=int(info.get("crisis_posts") or 0))


def muse_mode_sentence(info: Dict[str, Any], changes, chat: bool = False) -> str:
    """The section's mode sentence by the update's status. P0.7 r2 (E3 c): the chat context's status line is the same sentence, its
    positional words made true where it is read — the CURVEBALL block is the context's last block, so nothing is "below" it."""
    st = str((info or {}).get("status") or "")
    eps = (changes or {}).get("endpoints")
    e_ = (info or {}).get("edits") if isinstance((info or {}).get("edits"), dict) else None
    if st == "applied" and e_:          # BUILD IC-6 (D12): the keyed edits — the sentence by the counts
        n_nr = int((e_.get("counts") or {}).get("not reviewed") or 0)
        return (f"The campaign {'on this page' if chat else 'below'} is Block 1's, reviewed item by item by one call over it. "
                f"{muse_update_line(info)['line']} Python applied only the lines that check, each replacement, "      # BUILD IC-8 (D13)
                "removal and addition with a reason that names a real change, crisis-response posts first."
                + (f" The reply ended early ({e_.get('how')}): {n_nr} item(s) after its last complete line keep Block 1's words, each marked "
                   "not reviewed." if n_nr and e_.get("how") not in (None, "end_turn") else ""))
    if st.startswith("failed: declined"):      # BUILD IC-6 (D12): a refusal, said as such — never as a call that did not complete
        return MUSE_UPDATE_DECLINED_LINE.replace("the data changes below", "the data changes in this block") if chat else MUSE_UPDATE_DECLINED_LINE
    if st == "applied":
        n_posts = int((info or {}).get("crisis_posts") or 0)   # review F9: the posts this update added, not the ones it was asked for
        return (f"The campaign {'on this page' if chat else 'below'} is Block 1's, updated by one call over it: Python kept only the "
                "amendments whose reasons name a real change"
                + (f" and added {n_posts} crisis-response post(s)." if n_posts else "; the update added no crisis-response post."))
    if st.startswith("failed"):
        return MUSE_UPDATE_FAILED_LINE.replace("the data changes below", "the data changes in this block") if chat else MUSE_UPDATE_FAILED_LINE
    if st == "full":
        return ("The campaign was written fresh at the curveball (--curveball-full): both calls had the announcement"
                + (" and Python's change list." if eps is not None else "."))
    return ("The campaign was written fresh at the curveball — there was no Block 1 campaign to start from (" + st.replace("no base: ", "", 1)
            + "): both calls had the announcement" + (" and Python's change list." if eps is not None else "."))


def muse_part_name(d) -> str:
    """E3 (b): the page name of a part of the update that was not applied — MUSE_PART_NAMES and MUSE_FIELD_NAMES by its field, an
    entry by its number ("crisis post 2", "the social posts, post 1", "what changed, entry 3"); any other key → the update's key
    "<name, cut to 60>"."""
    if isinstance(d, dict) and d.get("name"):       # BUILD IC-6 (D12): an edit's part, named where it was recorded (muse_item_name)
        return str(d["name"])
    f = str((d or {}).get("field") or "")
    m = _MUSE_ENTRY.match(f)
    if m:
        k, j = m.group(1), int(m.group(2)) + 1
        return (f"crisis post {j}" if k == "crisis_posts" else f"the social posts, post {j}" if k == "creative.social_posts"
                else f"what changed, entry {j}")
    for table in (MUSE_PART_NAMES, MUSE_FIELD_NAMES):
        if f in table:
            return table[f]
    return f'the update\'s key "{NR.name_cut([f], quote=False)}"'


def muse_not_applied(info) -> list:
    """E3 (b, c): every part of the update not applied — [(name, plain reason)], in the record's order."""
    return [(muse_part_name(d), str(d.get("why") or "")) for d in (info or {}).get("dropped") or [] if isinstance(d, dict)]


def curveball_view(info: Dict[str, Any], changes, cv) -> Dict[str, Any]:
    """The Muse page's "What changed at the curveball" block, every value plain text (the template escapes each with | e).
    P0.7 r2 (E3 b): a "Not applied" block — every part of the update not applied, by its name and plain reason — under its sentence."""
    eps = (changes or {}).get("endpoints")
    mode = muse_mode_sentence(info, changes)
    e_ = info.get("edits") if isinstance(info.get("edits"), dict) and str(info.get("status") or "") == "applied" else None   # BUILD IC-6 (D12)
    rows = [NR.endpoint_text(r) for r in NR.endpoint_rows(changes)]   # the Oracle's and the hub's rule: counts, never a field sum (D4)
    if eps is not None:
        rows.append(f"{sum(1 for e in eps.values() if str(e.get('status', '')).startswith('unchanged'))} endpoint(s) unchanged")
    return {"mode": mode, "failed": str(info.get("status") or "").startswith("failed"), "first": NR.first_sentence((cv or {}).get("text")),
            "endpoints": rows, "compared": eps is not None, "what_changed": info.get("what_changed") or [],
            "snapshot": ((changes or {}).get("snapshot") or {}).get("source") or "",   # "" = no change list of this run reached the Muse
            "alerts": [NR.alert_line(*a) for a in NR.alert_changes(changes)] if changes else [],
            "amended": [{"name": MUSE_FIELD_NAMES.get(a["field"], a["field"]), "because": ", ".join(a["because"])} for a in info.get("amended") or []],
            "crisis_posts": int(info.get("crisis_posts") or 0),
            "not_applied": [f"{n}: {w}." for n, w in muse_not_applied(info)], "not_applied_sentence": MUSE_NOT_APPLIED,
            # BUILD IC-6 (D12): each part's status (D7's words) and every stale item — the keyed edits only
            "parts": [f"{p['part']}: {p['status']} ({p['why']})" for p in muse_update_parts(e_)] if e_ else [],
            "stale": [{"where": muse_item_name(x.get("id"), e_.get("items")), "names": ", ".join(x.get("names") or [])}
                      for x in e_.get("stale") or [] if isinstance(x, dict)] if e_ else []}


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5: QR Code Generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_qr_svg(url: str) -> str:
    """Generate an SVG QR code for the given URL."""
    if not HAS_SEGNO or not url:
        return ""
    try:
        qr = segno.make(url, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
        svg = REG.inline_svg(buf.getvalue().decode("utf-8"))       # BUILD IC-4 (D7, D8): no XML declaration in the body
        # Add viewBox for responsive sizing
        m = re.search(r'width="(\d+)" height="(\d+)"', svg)
        if m:
            svg = svg.replace(
                f'width="{m.group(1)}" height="{m.group(2)}"',
                f'width="{m.group(1)}" height="{m.group(2)}" viewBox="0 0 {m.group(1)} {m.group(2)}"'
            )
        return svg
    except Exception as e:
        NR.say(f"  QR generation failed: {type(e).__name__}: {e}")       # BUILD IC-7 (D8): plain
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5b: Campaign Reach Calculator Model
# ═══════════════════════════════════════════════════════════════════════════════

def parse_reach(s: str) -> float:
    """Extract a numeric reach value from Claude's human-readable strings.
    Handles: '15,000/mo', '5,000 families', '20K', '~8,000', '10,000-15,000'."""
    s = s.lower().replace(',', '').strip()
    # Strip trailing words
    s = re.sub(r'(~|per\s+\w+|/mo|/month|/week|families|people|residents|views|impressions)\s*$',
               '', s, flags=re.IGNORECASE).strip()
    # Handle ranges by averaging
    if '-' in s and not s.startswith('-'):
        parts = s.split('-', 1)
        v1 = _parse_single(parts[0].strip())
        v2 = _parse_single(parts[1].strip())
        return (v1 + v2) / 2
    return _parse_single(s)


def _parse_single(s: str) -> float:
    """Parse a single number with optional K/M suffix."""
    s = re.sub(r'[^0-9km.]', '', s.lower())
    if not s:
        return 0
    if s.endswith('k'):
        try: return float(s[:-1]) * 1000
        except: return 0
    if s.endswith('m'):
        try: return float(s[:-1]) * 1_000_000
        except: return 0
    try: return float(s)
    except: return 0


# Keywords for classifying channels — checked against CHANNEL NAME ONLY (not audience)
# Digital-first: if channel name matches any digital keyword, it's budget-sensitive
# Otherwise check community keywords; default to digital if neither matches
_DIGITAL_KEYWORDS = {'instagram', 'facebook', 'twitter', 'social', 'radio', 'tv',
    'television', 'digital', 'online', 'sms', 'text', 'whatsapp', 'email', 'google',
    'youtube', 'tiktok', 'snapchat', 'podcast', 'website', 'web', 'ads', 'media'}
_COMMUNITY_KEYWORDS = {'flyer', 'flyers', 'poster', 'door', 'church', 'partner',
    'event', 'community', 'volunteer', 'canvass', 'bulletin', 'newsletter',
    'pantry', 'clinic', 'library', 'school', 'bus stop', 'transit'}


_MARKUP = lambda s: REG.out_text(NR.escape_markup(s, keep_tags=True), "plain")   # noqa: E731 — BUILD IC-5 (D2, I3): the one markup rule,
#                                                     the Oracle's too; (D2 c) then the tags in words — the page's variant is the model's HTML


def _staged(t):
    """BUILD IC-5 (D7): resolve_muse_text's three results — the page's variant, the plain one — labelled RESOLVED (both modes: with the
    switch off its step holds the tokens as written); returns them."""
    s_, c_, plain_ = t
    for o_ in (s_, c_) + tuple(plain_):
        REG.set_stage(o_, REG.RESOLVED)
    return t


# BUILD IC-8 (D3): THE chain by field kind for the Muse's texts (a path in a campaign part's dump; a list position as an int)
MUSE_ITEM_FIELDS = {"strategic.personas": ("name",), "strategic.channel_plan": ("channel",), "strategic.calendar": ("phase_name",),
                    "strategic.kpis": ("metric", "target"), "creative.social_posts": ("text",)}       # the item goes whole, before the counts
MUSE_PROSE = {("strategic", "persona_text"), ("creative", "flyer", "body_en"), ("creative", "flyer", "body_es"),
              ("creative", "partner_pitch", "problem"), ("creative", "partner_pitch", "solution")}      # REG.UNIT_DEFAULT
MUSE_ELEMENT_LISTS = (("strategic", "personas", "*", "barriers"), ("strategic", "personas", "*", "channels"),
                      ("strategic", "personas", "*", "data_evidence"), ("strategic", "calendar", "*", "milestones"),
                      ("strategic", "calendar", "*", "deliverables"), ("strategic", "hero_stats"),
                      ("creative", "social_posts", "*", "hashtags"), ("creative", "flyer", "bullets_en"), ("creative", "flyer", "bullets_es"),
                      ("creative", "partner_pitch", "benefits"), ("creative", "partner_pitch", "data_points"))   # an element leaves its list


def _element_of(top, path):
    """BUILD IC-8 (D3): (its list's path in the `top` part's dump, its position) when `path` lies in an element of a list with no ids of
    its own (MUSE_ELEMENT_LISTS), else None."""
    p = (top,) + tuple(path)
    for pat in MUSE_ELEMENT_LISTS:
        n = len(pat)
        if len(p) > n and isinstance(p[n], int) and all(a == b or (b == "*" and isinstance(a, int)) for a, b in zip(p[:n], pat)):
            return tuple(p[1:n]), p[n]
    return None


def muse_text_kind(top, path):
    """BUILD IC-8 (D3): a Muse text's kind for THE chain after its default — "element", "item", "prose" or "omit" (a short label, the
    quote: the template shows its place without it)."""
    p = (top,) + tuple(path)
    if _element_of(top, path):
        return "element"
    if len(p) == 4 and isinstance(p[2], int) and p[3] in MUSE_ITEM_FIELDS.get(f"{p[0]}.{p[1]}", ()):
        return "item"
    return "prose" if p in MUSE_PROSE else "omit"


def _at(d, path):
    for p in path:
        d = d[p] if isinstance(d, (list, dict)) and (isinstance(d, dict) and p in d or isinstance(d, list) and isinstance(p, int) and p < len(d)) else None
    return d


def _muse_base_of(base, edits, top, html):
    """BUILD IC-8 (D3): the unit rule's chain for a Muse text (Block 2) — path (in the `top` campaign part's dump) → Block 1's text of the
    same field: of the same item, found by its id (a list field: this run's ids, edits["ids"], against Block 1's), or the whole field's;
    the page's variant gets the markup rule as the page's own (outside_tokens). A string inside an item's own list (a bullet, a barrier:
    no id of its own) has no chain — no identity by position. None in Block 1."""
    if base is None:
        return None
    b_dump = base[top].model_dump()
    b_ids = REG.muse_item_ids(muse_lists(base["strategic"].model_dump(), base["creative"].model_dump()), base.get("item_ids"))[0]
    now_ids = (edits or {}).get("ids") or {}

    def get(path):
        if not path:
            return None
        f = f"{top}.{path[0]}"
        if f in b_ids:
            if len(path) < 3 or not isinstance(path[1], int):
                return None
            cur = now_ids.get(f) or []
            iid = cur[path[1]] if path[1] < len(cur) else None
            if iid is None or iid not in b_ids[f]:
                return None
            v, rest = (b_dump.get(path[0]) or [])[b_ids[f].index(iid)], path[2:]
        else:
            v, rest = b_dump.get(path[0]), path[1:]
        if any(isinstance(p, int) for p in rest):
            return None
        for p in rest:
            v = v.get(p) if isinstance(v, dict) else None
        if not isinstance(v, str):
            return None
        return REG.outside_tokens(v, _MARKUP) if html else v
    return get


def muse_text_unit(top, path, ids_now):
    """BUILD IC-8 (D3): a Muse text's unit id — muse.<top>.<field>[.<item>].<key…>: a list item named by its id's own part (persona.p1 →
    p1), never by its position."""
    parts, f = [top, str(path[0])], f"{top}.{path[0]}"
    rest = list(path[1:])
    if f in ids_now and rest and isinstance(rest[0], int):
        lst = ids_now.get(f) or []
        rest[0] = lst[rest[0]].partition(".")[2] if rest[0] < len(lst) else f"at{rest[0]}"
    return "muse." + ".".join(parts + [str(p) for p in rest])


def resolve_muse_text(oracle_ctx: Dict, strategic, creative, info=None, base=None, units=None, defaults=None, ids=None):
    """BUILD IC-4 (D16): the Muse's own replies cite the Oracle's registry of this run. With the switch on: each of its lists' counts
    registered for this page (count.muse.<list>, REG.add_track_counts), then every text field resolved — spans for the page, plain words
    for the calculator, the chat and the brief's answers, and for the update's what-changed lines (printed as text); the page's meta
    description and tagline print plain words. A sentence whose token names nothing is left out, each named here. Returns (strategic,
    creative, (strategic, creative) in plain words). With the switch off, or with no registry of this run, the replies as written."""
    reg = oracle_ctx.get("_reg")
    if not NR.ic4_prompt() or reg is None:     # BUILD IC-5 (D2, I3): the page's variant passes the markup rule in this mode too
        try:
            return _staged((MuseStrategic.model_validate(REG.outside_tokens(strategic.model_dump(), _MARKUP)),
                            MuseCreative.model_validate(REG.outside_tokens(creative.model_dump(), _MARKUP)), (strategic, creative)))
        except Exception as e:  # noqa: BLE001 — a reply that no longer validates: its words escaped whole on the page
            NR.say(f"    ⚠ the Muse's text: the markup rule left a field that does not validate ({type(e).__name__}) — every tag escaped")
            esc_ = lambda v: REG.outside_tokens(v, lambda s: REG.out_text(NR.escape_markup(s, keep_tags=False), "plain"))   # noqa: E731
            return _staged((MuseStrategic.model_validate(esc_(strategic.model_dump())), MuseCreative.model_validate(esc_(creative.model_dump())),
                            (strategic, creative)))
    # the registry read back holds what the Oracle registered: given the data it was built on, its ids by grammar ([[<endpoint>.count]],
    # a record's field) resolve, as on the Architect's page
    if oracle_ctx.get("_raw_oracle") and not reg._raw:
        reg.set_data(oracle_ctx["_raw_oracle"], NR.records)
        reg.set_facts((_BRIEF["doc"] or {}).get("authority"), (_BRIEF["doc"] or {}).get("aliases"))
    # BUILD IC-8 (D2): the counts were registered from the final membership and sealed by finalize_muse, before any text resolves
    # BUILD IC-8 (D3): every text under THE unit rule and THE chain by field kind — Block 1's same field (by item id), the field's
    # default (`defaults`: finalize_muse's, by item id), "" (a short label, the quote, an element), the prose default. The plain words
    # decide; the page's variant follows them (`decided`): one decision, recorded once, in `units` ({unit, disposition, reason, n, took})
    edits_ = (info or {}).get("edits") if isinstance((info or {}).get("edits"), dict) else {}
    sink_s, sink_c = [], []
    dpath = {tuple(MUSE_WHOLE_WORDS[i][0].split(".")) + ((MUSE_WHOLE_WORDS[i][1],) if MUSE_WHOLE_WORDS[i][1] else ()): v
             for i, v in (defaults or {}).items() if i in MUSE_WHOLE_WORDS and isinstance(v, str) and v.strip()}

    def default_of(top, html):
        def f(path):
            d = dpath.get((top,) + tuple(path))
            return (REG.outside_tokens(d, _MARKUP) if html else d) if d else None
        return f

    def omittable_of(top):
        return lambda path: muse_text_kind(top, path) != "prose"
    G = REG.UNIT_DEFAULT
    sp = REG.resolve_track_units(reg, strategic.model_dump(), "muse", False, _muse_base_of(base, edits_, "strategic", False), sink_s, (),
                                 default_of("strategic", False), omittable_of("strategic"), G)
    cp = REG.resolve_track_units(reg, creative.model_dump(), "muse", False, _muse_base_of(base, edits_, "creative", False), sink_c, (),
                                 default_of("creative", False), omittable_of("creative"), G)
    sh = REG.resolve_track_units(reg, REG.outside_tokens(strategic.model_dump(), _MARKUP), "muse", True,          # BUILD IC-5 (D2, I3)
                                 _muse_base_of(base, edits_, "strategic", True), None, (), default_of("strategic", True),
                                 omittable_of("strategic"), G, {x["path"]: x for x in sink_s})
    ch = REG.resolve_track_units(reg, REG.outside_tokens(creative.model_dump(), _MARKUP), "muse", True,
                                 _muse_base_of(base, edits_, "creative", True), None, (), default_of("creative", True),
                                 omittable_of("creative"), G, {x["path"]: x for x in sink_c})
    for k in ("short_tagline", "og_description"):      # the plain words; finalize_muse's fits decide the shown text
        sh[k] = sp.get(k)
    ids_now = ids or (edits_.get("ids") if base is not None else REG.muse_item_ids(muse_lists(strategic.model_dump(), creative.model_dump()))[0]) or {}
    # D3: an element the chain left out leaves its list, in both variants (the same one: one decision); its unit is the element's
    found, gone_el = [], {}
    for top, sink, dumps in (("strategic", sink_s, (sp, sh)), ("creative", sink_c, (cp, ch))):
        for x in sink:
            if top == "strategic" and x["path"] and x["path"][0] in ("short_tagline", "og_description"):
                continue                                                              # (their outcome is their fit)
            el = _element_of(top, x["path"]) if x.get("took") == "omit" else None
            if el is None:
                found.append(dict(x, unit=muse_text_unit(top, x["path"], ids_now)))
            elif (top, el) not in gone_el:
                gone_el[(top, el)] = dumps
                found.append(dict(x, path=el[0] + (el[1],), unit=muse_text_unit(top, el[0] + (el[1],), ids_now)))
    for (top, (lp, k)), dumps in sorted(gone_el.items(), key=lambda t: (t[0][0], str(t[0][1][0]), -t[0][1][1])):
        for d_ in dumps:
            lst = _at(d_, lp)
            if isinstance(lst, list) and k < len(lst):
                del lst[k]
    for k_, w in enumerate((info or {}).get("what_changed") or [], 1):     # the notes: new text, no base
        if isinstance(w, dict) and isinstance(w.get("impact"), str):
            sk_ = []
            w["impact"] = REG.resolve_track_units(reg, w["impact"], "muse", False, None, sk_, (), None, None, G)
            found += [dict(x, unit=f"muse.what_changed.{k_}.text") for x in sk_]
    for x in found:
        NR.say(f"    ⚠ the Muse's text: {x['unit']} — " + (f"{x['n']} sentence(s) left out ({x['reason']})" if x.get("took") == "sentences" else
                                                         {"base": "Block 1's words", "default": "its default", "omit": "left out",
                                                          "generic": "the prose default"}.get(x.get("took"), x["disposition"])
                                                         + f" ({x['reason']})"))
    if isinstance(units, list):
        units.extend(found)
    for d_ in (ch, cp):     # IC-4 Phase 7 (review c5): no engagement count on a post — the data holds none, so the card shows none
        for p_ in (d_.get("social_posts") or []) if isinstance(d_, dict) else []:
            if isinstance(p_, dict):
                p_.update(likes="", comments="", shares="")
    _write_added(oracle_ctx)
    try:
        return _staged((MuseStrategic.model_validate(sh), MuseCreative.model_validate(ch),
                        (MuseStrategic.model_validate(sp), MuseCreative.model_validate(cp))))
    except Exception as e:  # noqa: BLE001 — the plain words then stand on the page too (no token is left raw)
        NR.say(f"    ⚠ the Muse's text: its fields with the registry's spans did not validate ({type(e).__name__}) — plain words on the page")
        ps_, pc_ = MuseStrategic.model_validate(sp), MuseCreative.model_validate(cp)
        return _staged((ps_, pc_, (ps_, pc_)))


# BUILD IC-8 (D2, I4): the words a required slot is read by — a list item's key (MUSE_WORDS) and a whole field's (MUSE_WHOLE_WORDS: its
# path and the key inside it, or None for a text field)
MUSE_WORDS = {"strategic.personas": "name", "strategic.channel_plan": "channel", "strategic.calendar": "phase_name", "strategic.kpis": "metric",
              "creative.social_posts": "text"}
MUSE_WHOLE_WORDS = {"m.hero_title": ("strategic.hero_title", None), "m.hero_lead": ("strategic.hero_lead", None),
                    "m.persona_text": ("strategic.persona_text", None), "m.flyer": ("creative.flyer", "headline_en"),
                    "m.pitch": ("creative.partner_pitch", "headline")}
MUSE_ADD_FIELD = {"crisis_post": "creative.social_posts", "post": "creative.social_posts", "persona": "strategic.personas",
                  "channel": "strategic.channel_plan", "phase": "strategic.calendar", "kpi": "strategic.kpis", "impact": "what_changed"}
_MUSE_ITEM_IDS = {}       # BUILD IC-8 (D2): Block 1's ids as finalize_muse kept them (a left-out item's gap kept) — stored with Block 1


def _has_words(v):
    """BUILD IC-8 (D2, I4): a text with a letter or a digit outside its tags (a token prints a figure: words)."""
    return isinstance(v, str) and bool(re.search(r"[^\W_]", re.sub(r"<[^>]*>", " ", v)))


def muse_empty_words(strategic, creative, base, edits, ids_b1, defaults, dead=None):
    """BUILD IC-8 (D2, I4): a required slot is never empty — before the counts. A list item whose words (MUSE_WORDS) are empty: Block 2,
    one the update replaced, takes Block 1's item of the same id when that has words (rejected_kept_base); else it leaves its list
    (omitted) and its id leaves the ids (Block 2: edits["ids"]; Block 1: `ids_b1` — the others keep their names). A whole field with no
    words (MUSE_WHOLE_WORDS): Block 2 replaced — Block 1's value (rejected_kept_base); else its default (`defaults`, by item id:
    fallback_default); else omitted. Never by position: a list whose ids do not fit it is left as it is. The campaign is changed in
    place. → {item id: (disposition, "empty_words", source)}. BUILD IC-8 (D3): `dead(item, list)` — the unit rule on its name and figure
    (MUSE_ITEM_FIELDS; REG.unit_why preseal): an item whose own fails is handled the same way, before the counts, its reason the rule's
    (Block 1's kept: rejected_kept_base; left out: <rule>_no_chain)."""
    out = {}
    status = (edits or {}).get("status") or {}
    ids_now = (edits or {}).get("ids") if base is not None else ids_b1
    if not isinstance(ids_now, dict):
        return out
    b_ids = (REG.muse_item_ids(muse_lists(base["strategic"].model_dump(), base["creative"].model_dump()), base.get("item_ids"))[0]
             if base is not None else {})
    for f, key in MUSE_WORDS.items():
        top, sub = f.split(".", 1)
        obj = strategic if top == "strategic" else creative
        lst, ids_f = list(getattr(obj, sub) or []), list(ids_now.get(f) or [])
        if len(ids_f) != len(lst):
            continue
        b_lst = list(getattr(base[top], sub) or []) if base is not None else []
        b_items = {i: b_lst[k] for k, i in enumerate(b_ids.get(f) or []) if k < len(b_lst)}
        keep, keep_ids = [], []
        for x, i in zip(lst, ids_f):
            why_ = "empty_words" if not _has_words(getattr(x, key, None)) else (dead(x, f) if dead else None)
            if why_ is None:
                keep.append(x)
                keep_ids.append(i)
            elif (base is not None and status.get(i) == "replaced" and i in b_items and _has_words(getattr(b_items[i], key, None))
                  and not (dead(b_items[i], f) if dead else None)):
                keep.append(b_items[i])
                keep_ids.append(i)
                out[i] = ("rejected_kept_base", why_, "base")
            else:
                out[i] = ("omitted", why_ if why_ == "empty_words" else f"{why_}_no_chain", "candidate")
        if len(keep) != len(lst) or any(a is not b for a, b in zip(keep, lst)):
            setattr(obj, sub, keep)
            ids_now[f] = keep_ids
    for iid, (fp, key) in MUSE_WHOLE_WORDS.items():
        top, sub = fp.split(".", 1)
        obj = strategic if top == "strategic" else creative
        v = getattr(obj, sub, None)
        if _has_words(getattr(v, key, None) if key else v):
            continue
        bv = getattr(base[top], sub, None) if base is not None else None
        if base is not None and status.get(iid) == "replaced" and _has_words(getattr(bv, key, None) if key else bv):
            setattr(obj, sub, bv.model_copy(deep=True) if key and hasattr(bv, "model_copy") else bv)
            out[iid] = ("rejected_kept_base", "empty_words", "base")
        elif (defaults or {}).get(iid):
            if key and v is not None:
                setattr(v, key, defaults[iid])
            elif not key:
                setattr(obj, sub, defaults[iid])
            out[iid] = ("fallback_default", "empty_words", "default")
        else:
            out[iid] = ("omitted", "empty_words", "candidate")
    return out


def _base_cut_paths(run_id):
    """BUILD IC-8 (D2, F2): the paths Block 1's admission record (checkpoints/admission_<its run id>.jsonl) names with rule "cut" — a
    value IC-7's admission cut, stored so. A file that cannot be read names none."""
    out = set()
    if not run_id:
        return out
    # BUILD IC-8 (D9): through the one reader, the run's lines — IC-7's name no invocation, and IC-8's admission cuts nothing
    for rec_ in NR.read_records(HARNESS_DIR / "checkpoints" / f"admission_{run_id}.jsonl", "run", rid=run_id, strict=False).lines:
        for e in rec_.get("entries") or []:
            if isinstance(e, dict) and e.get("rule") == "cut" and isinstance(e.get("path"), str):
                out.add(e["path"])
    return out


def _muse_tagline_fits(reg_, strategic, plain, base_strategic, update_info, base_run_id, program_name):
    """BUILD IC-8 (D1, D2): the preview's and the tagline's fits — {field: (stored text, disposition, reason)} — each fitted once, on its
    shown text, with its chain (Block 1's value — a value IC-7 cut known from Block 1's admission record, F2 —, the campaign lead, the
    default). build_context and finalize_muse read the same result."""
    b1s_ = (base_strategic.model_dump() if hasattr(base_strategic, "model_dump") else
            base_strategic if isinstance(base_strategic, dict) else {})       # load_muse_block1 gives a MuseStrategic
    lead_ = (plain[0].hero_lead if plain else None) or strategic.hero_lead
    unit_ = NR.facts(_BRIEF.get("doc")).get("unit_plural") or "the people it serves"     # the contract's unit words
    # BUILD IC-8 (D2, F2): a Block 1 value IC-7 cut is known from Block 1's admission record; a tagline the update did not replace IS
    # that value — it keeps its whole leading sentences, or takes the chain (reason base_incomplete)
    cut_ = _base_cut_paths(base_run_id) if b1s_ else set()
    st_ = ((update_info.get("edits") or {}).get("status") or {}) if isinstance(update_info, dict) else {}    # the items' edit status

    def _kept_base(f):
        return bool(b1s_) and f in cut_ and st_.get(f"m.{f}") != "replaced"
    og_ = REG.fit_shown(strategic.og_description, reg_, "muse.og_description", track="muse", unit="muse.strategic.og_description", unit_rule=True,
                                incomplete=_kept_base("og_description"), source="base" if _kept_base("og_description") else "candidate",
                                chain=[c for c in (("base", b1s_.get("og_description"), {"incomplete": "og_description" in cut_}),
                                                   ("lead", lead_), ("default", f"{program_name} — community outreach campaign.")) if c[1]])
    tg_ = REG.fit_shown(strategic.short_tagline, reg_, "muse.short_tagline", track="muse", unit="muse.strategic.short_tagline", unit_rule=True,
                                incomplete=_kept_base("short_tagline"), source="base" if _kept_base("short_tagline") else "candidate",
                                chain=[c for c in (("base", b1s_.get("short_tagline"), {"incomplete": "short_tagline" in cut_}),
                                                   ("default", f"Connecting {unit_} to {program_name}.")) if c[1]])
    return {"og_description": og_, "short_tagline": tg_}


def muse_whole_edits(base, strategic, creative, info=None):
    """BUILD IC-8 (D14's finding; D2, D3): the whole-object update's record in the keyed edits' form — {"status": {item id: kept,
    replaced, added or removed}, "ids": {list field: its ids in the page's order}, "tombstones": [Block 1's posts the page's six left
    out], "form"} — made from Block 1's campaign and the page's, never from the reply's words: a list item at its place equal to Block 1's
    (NR.edit_base) kept, else replaced; past Block 1's end, added (new ids, never reused); the crisis posts first in the posts, added;
    Block 1's posts pushed past the six, not shown; a list made shorter, its Block 1 items removed; a whole field (MUSE_WHOLE_IDS) kept or
    replaced. So both update forms reach the finalizer with one record."""
    s0, c0 = base["strategic"].model_dump(), base["creative"].model_dump()
    s1, c1 = strategic.model_dump(), creative.model_dump()
    old_l, new_l = muse_lists(s0, c0), muse_lists(s1, c1)
    b1_ids = REG.muse_item_ids(old_l, base.get("item_ids"))[0]
    n_cr = max(0, int((info or {}).get("crisis_posts") or 0))
    status, ids, tomb = {}, {}, []
    for f, b1f in b1_ids.items():
        old, cur = old_l.get(f) or [], new_l.get(f) or []
        off = min(n_cr, len(cur)) if f == "creative.social_posts" else 0
        fresh = iter(REG.assign_muse_ids(f, len(cur), used=b1f))
        out = []
        for j, v in enumerate(cur):
            k = j - off
            if j < off or k >= len(old):
                nid = next(fresh)
                status[nid] = "added"
            else:
                nid = b1f[k]
                status[nid] = "kept" if NR.edit_base(v) == NR.edit_base(old[k]) else "replaced"
            out.append(nid)
        ids[f] = out
        for k in range(max(0, len(cur) - off), len(old)):
            if f == "creative.social_posts":
                tomb.append({"id": b1f[k], "removed_by": "not shown — the page shows six posts at most"})
            else:
                status[b1f[k]] = "removed"
    for iid, path in MUSE_WHOLE_IDS.items():
        top, sub = path.split(".", 1)
        a, b = (s0 if top == "strategic" else c0).get(sub), (s1 if top == "strategic" else c1).get(sub)
        status[iid] = "kept" if NR.edit_base(a) == NR.edit_base(b) else "replaced"
    return {"status": status, "ids": ids, "tombstones": tomb, "form": "whole object"}


def finalize_muse(oracle_ctx: Dict, strategic, creative, info=None, base=None, answers_of=None, program_name=None):
    """BUILD IC-8 (D2, I1): the Muse's one finalization, the same for both blocks — (1) the counts from the final membership (the lists
    the page shows; the posts as published, MUSE_POSTS_SHOWN at most), (2) sealed, (3) every text resolved (resolve_muse_text), (4) the
    brief's answers, which read those counts: Block 2 (base) — Block 1's answers as written, re-resolved on this run's registry (else its
    answers as resolved); Block 1 — answers_of(plain), the brief call's answers. Returns (strategic, creative, plain, answers, the
    answers as written). No other function registers or seals the Muse's counts or resolves its texts or answers.
    BUILD IC-8 (D2, I4): first the membership — a required slot left empty takes its chain before the counts (muse_empty_words)."""
    reg = oracle_ctx.get("_reg")
    prog_ = program_name or oracle_ctx.get("program_name") or "Community Navigator"
    edits_ = (info or {}).get("edits") if isinstance((info or {}).get("edits"), dict) else {}
    if base is not None and not edits_.get("status"):   # BUILD IC-8 (D14's finding): the whole-object update, in the keyed form —
        edits_ = muse_whole_edits(base, strategic, creative, info)     # its record made from the page (muse_whole_edits)
        info = dict(info or {}, edits=edits_)       # this function's own copy: the caller's record and its status line keep their form
    ids_b1 = REG.muse_item_ids(muse_lists(strategic.model_dump(), creative.model_dump()))[0] if base is None else None
    if NR.ic4_prompt() and reg is not None and oracle_ctx.get("_raw_oracle") and not reg._raw:     # the rule reads ids by grammar
        reg.set_data(oracle_ctx["_raw_oracle"], NR.records)
        reg.set_facts((_BRIEF["doc"] or {}).get("authority"), (_BRIEF["doc"] or {}).get("aliases"))

    def dead_(x, f):                 # BUILD IC-8 (D3): an item whose name or figure fails the unit rule goes before the counts
        for k_ in MUSE_ITEM_FIELDS.get(f, ()):
            w_ = getattr(x, k_, None)
            r_ = REG.unit_why(reg, w_, "muse", preseal=True) if isinstance(w_, str) else None
            if r_:
                return r_
        return None
    defaults_ = {"m.hero_title": prog_, "m.hero_lead": f"{prog_} — community outreach campaign.", "m.flyer": prog_, "m.pitch": prog_}
    empty_ = muse_empty_words(strategic, creative, base, edits_, ids_b1, defaults_,
                              dead=dead_ if (NR.ic4_prompt() and reg is not None) else None)
    for i_, (d_, _r, _s) in empty_.items():
        NR.say(f"    the Muse's {i_}: no words — " + {"rejected_kept_base": "Block 1's stands", "fallback_default": "its default",
                                                      "omitted": "left out"}[d_])
    _MUSE_ITEM_IDS.clear()
    _MUSE_ITEM_IDS.update(ids_b1 or {})
    ids_ = (edits_.get("ids") if base is not None and edits_.get("ids") else ids_b1) or {}
    if NR.ic4_prompt() and reg is not None:
        REG.add_track_counts(reg, "muse", {"personas": len(strategic.personas), "channel_plan": len(strategic.channel_plan),
                                           "calendar": len(strategic.calendar), "kpis": len(strategic.kpis),
                                           "social_posts": min(len(creative.social_posts), MUSE_POSTS_SHOWN)}, REG.MUSE_COUNT_NAMES)
        REG.seal_counts(reg, "muse")
    text_units, ans_units = [], {}
    strategic, creative, plain = resolve_muse_text(oracle_ctx, strategic, creative, info, base=base, units=text_units, defaults=defaults_,
                                                   ids=ids_)
    raw = None
    if base is not None:
        answers = dict(base.get("brief_answers") or {})
        if base.get("brief_answers_written"):           # Block 1's answers stand: the rule's chain is Block 1's resolved answer
            answers = resolve_muse_answers(oracle_ctx, dict(base["brief_answers_written"]), dict(base.get("brief_answers") or {}), ans_units)
    elif answers_of is not None:
        raw = answers_of(plain)
        answers = resolve_muse_answers(oracle_ctx, raw, None, ans_units)
    else:
        answers = {}
    # 5. one outcome per unit (BUILD IC-8 D3, I3 — ids and paths only, never a text): the preview and the tagline one unit each, its
    # outcome its fit's when the fit changed the text, else its edit's (Block 1: added); every other item by the edit record's status
    # (Block 1: each item the campaign holds, added); each brief answer by its sentences (muse.brief.<id>.s<k>: Block 1 answered, Block 2
    # Block 1's kept); each note on what changed fitted here, at its destination (muse.update.impact — its parsers keep it whole); then
    # any other fit of this process that changed its text (REG.fit_units)
    disp = REG.Dispositions(NR.BUILD_ID, "muse", os.environ.get("NAIPC_RUN_ID", "").strip() or None)
    status_ = dict(edits_.get("status") or {})
    ids_ = (edits_.get("ids") if base is not None and edits_.get("ids") else ids_b1) or {}
    field_of = {i: f_ for f_, lst_ in ids_.items() for i in lst_}
    gone_ = {t.get("id"): t for t in edits_.get("tombstones") or [] if isinstance(t, dict)} if base is not None else {}
    if base is None or not status_:        # Block 1 (or an update whose record names no item): every item the campaign holds, added
        status_ = {i: "added" for i in [x for v in ids_.values() for x in v] + list(MUSE_WHOLE_IDS)}
        status_.update({i: "added" for i in empty_ if i not in status_})
    pub_ids = [i for lst_ in ids_.values() for i in lst_]           # D3 (I5): the records follow the page — its lists in their order
    order_ = [i for i in dict.fromkeys(pub_ids) if i in status_] + [i for i in status_ if i not in set(pub_ids)]
    line_of = {nid: lid for lid, nid in (edits_.get("added_ids") or {}).items()} if base is not None else {}
    fits_ = _muse_tagline_fits(oracle_ctx.get("_reg"), strategic, plain, (base or {}).get("strategic") if base is not None else None,
                               info if base is not None else None, ((base or {}).get("meta") or {}).get("run_id") if base is not None else None,
                               program_name or oracle_ctx.get("program_name") or "The program")

    moved_ = set()      # BUILD IC-8 (D2, F6): a kept item whose token's figure moved since Block 1 — "kept, figure changed"
    if base is not None and oracle_ctx.get("_reg") is not None and any((s or "kept") == "kept" for s in status_.values()):
        its_, _o = muse_review_set(base, oracle_ctx.get("_reg"))
        mk_ = REG.changed_marks(oracle_ctx.get("_reg"))
        moved_ = {i for i, s in status_.items() if (s or "kept") == "kept" and i in its_
                  and REG.cites_changed(oracle_ctx.get("_reg"), list(_mtexts(its_[i]["value"])), mk_)}

    def outcome_(iid):
        if base is None:
            return "added_valid", "whole", "candidate", None
        o_ = REG.EDIT_OUTCOMES.get(status_.get(iid) or "kept", REG.EDIT_OUTCOMES["kept"])
        return (o_[0], "figure_changed", o_[2], o_[3]) if iid in moved_ and (status_.get(iid) or "kept") == "kept" else o_
    for iid, f_ in (("m.og_description", fits_["og_description"]), ("m.short_tagline", fits_["short_tagline"])):
        d_, _r, s_, op_ = outcome_(iid)
        disp.expect(muse_unit(iid, field_of))
        disp.fit(muse_unit(iid, field_of), f_, d_, s_, op_, item=iid)
    # D3 (V26): each text that lost a sentence — one unit per sentence (<field>.s<k>) — or took its chain (the field's own outcome,
    # recorded before the items: a whole field's chain stands for it)
    for x in text_units:
        for u_ in ([f"{x['unit']}.s{j}" for j in range(1, max(1, x["n"]) + 1)] if x.get("took") == "sentences" else [x["unit"]]):
            if not disp.has(u_):
                disp.expect(u_)
                disp.add(u_, x["disposition"], x["reason"], {"rejected_kept_base": "base", "fallback_default": "default"}.get(x["disposition"], "candidate"))
    for iid in order_:
        u = muse_unit(iid, field_of)
        disp.expect(u)
        if disp.has(u):
            continue
        it_ = line_of.get(iid, iid)                     # an added item named by its edit line's id ("new.1"), its unit by Python's id
        d_, r_, s_, op_ = outcome_(iid)
        if iid in empty_:                               # D2 (I4): no words — its chain (muse_empty_words)
            disp.add(u, *empty_[iid], op=op_, item=it_)
        elif iid in gone_ and "not shown" in str(gone_[iid].get("removed_by") or ""):
            disp.add(u, "omitted", "list_cap", "candidate", op=op_, item=it_)
        else:
            disp.add(u, d_, r_, s_, op=op_, item=it_)
    for iid, t_ in gone_.items():                        # a post the page's six left out that no status names
        u = muse_unit(iid, field_of)
        if not disp.has(u) and "not shown" in str(t_.get("removed_by") or ""):
            disp.expect(u)
            disp.add(u, "omitted", "list_cap", "candidate", item=line_of.get(iid, iid))
    for x in edits_.get("not_used") or []:              # D3 (I3): an add line no page item came from — the proposed list, whole
        f_ = MUSE_ADD_FIELD.get(x.get("kind"))
        u = f"muse.{f_}.{x['id']}" if f_ else f"muse.update.add.{x['id']}"
        if not disp.has(u):
            disp.expect(u)
            disp.add(u, "omitted", x.get("reason") or "edit_not_applied", "candidate", op="add", item=x["id"])
    for qid, a_ in (answers or {}).items():
        ss_ = [s for s in (REG.split_sentences(a_) if isinstance(a_, str) else []) if s.strip()]
        ch_ = next((x for x in ans_units.get(qid) or [] if x.get("took") != "sentences"), None)   # D3: the answer took its chain
        gone_n = sum(x["n"] for x in ans_units.get(qid) or [] if x.get("took") == "sentences")
        if not ss_:
            disp.expect(f"muse.brief.{qid}")
            disp.add(f"muse.brief.{qid}", "omitted", (ch_ or {}).get("reason") or "empty_words", "candidate", item=qid)
        for k_ in range(1, len(ss_) + 1):
            disp.expect(f"muse.brief.{qid}.s{k_}")
            if ch_ is not None:
                disp.add(f"muse.brief.{qid}.s{k_}", ch_["disposition"], ch_["reason"], "base" if ch_.get("took") == "base" else "default", item=qid)
            else:
                disp.add(f"muse.brief.{qid}.s{k_}", *(("kept_valid", "block1_answer", "base") if base is not None else
                                                      ("added_valid", "answered", "candidate")), item=qid)
        for j_ in range(1, gone_n + 1):              # D3: a sentence the rule let go — its own unit
            disp.expect(f"muse.brief.{qid}.x{j_}")
            disp.add(f"muse.brief.{qid}.x{j_}", "omitted", "token_names_nothing", "candidate", item=qid)
    for k_, w in enumerate((info or {}).get("what_changed") or [], 1):
        if isinstance(w, dict) and isinstance(w.get("impact"), str):
            f_ = REG.fit_shown(w["impact"], None, REG.fit_row("muse.update.impact", MUSE_IMPACT_CHARS), track="muse",
                               unit=f"muse.what_changed.{k_}")
            w["impact"] = f_[0]             # its resolved words (resolve_muse_text), fitted once — the page and the chat read them
            disp.expect(f"muse.what_changed.{k_}")
            disp.fit(f"muse.what_changed.{k_}", f_, "added_valid", "candidate", "add")
    REG.fit_units(disp, "muse")
    rid_ = disp.run_id or "norun"
    why_ = disp.write(HARNESS_DIR / "checkpoints" / f"dispositions_{rid_}.jsonl", {"invocation": os.environ.get("NAIPC_INVOCATION_NUMBER")})
    NR.say(f"  {disp.content_line('Muse')}" + (f" (dispositions not written: {why_})" if why_ else ""))
    _MUSE_CONTENT.clear()
    _MUSE_CONTENT.update(disp.content())        # the status file's "content": counts only
    return strategic, creative, plain, answers, raw


def muse_unit(iid, field_of=None):
    """BUILD IC-8 (D3): an item's unit id — muse.<its field's path>.<its id's own part> (persona.p1 of strategic.personas →
    muse.strategic.personas.p1; an added new.4 of creative.social_posts → muse.creative.social_posts.new.4), a whole field muse.<its path>
    (m.hero_title → muse.strategic.hero_title); never a position."""
    if iid in MUSE_WHOLE_IDS:
        return f"muse.{MUSE_WHOLE_IDS[iid]}"
    f_ = (field_of or {}).get(iid) or {k: f for f, k in MUSE_LIST_KIND.items()}.get(iid.partition(".")[0])   # IC-8 (D2): a left-out item
    if f_:
        return f"muse.{f_}.{iid if iid.startswith('new.') else iid.partition('.')[2]}"
    return f"muse.{iid}"


_MUSE_CONTENT = {}      # BUILD IC-8 (D3): this invocation's dispositions, counted (Dispositions.content) — the track's status reads it


def _write_added(oracle_ctx: Dict):
    """IC-4 Phase 7 (review b1, D3): the figures the Muse registered beyond the Oracle's registry of this run (its counts, assumptions and
    computed figures) → checkpoints/registry_added.json, for the pipeline's check across pages (REG.merge_added)."""
    reg, base = oracle_ctx.get("_reg"), oracle_ctx.get("_reg_base")
    if reg is None or base is None:
        return
    why = REG.write_added(HARNESS_DIR / "checkpoints" / REG.ADDED_FILE, reg, base, "muse")
    if why:
        NR.say(f"    ⚠ the Muse's own figures were not written for the check across pages: {why}")


def resolve_muse_answers(oracle_ctx: Dict, answers, base_answers=None, sink=None):
    """BUILD IC-4 (D16): the brief's answers in plain words on the Oracle's registry of this run (the panel prints text), the switch on.
    BUILD IC-8 (D3): each under THE unit rule — a dead token's sentence goes when the rule allows; else the chain: Block 1's answer to the
    same item (`base_answers`, as written), resolved here under the same rule; else REG.ANSWER_DEFAULT. `sink` ({item id: [...]})
    receives each answer's outcome ({path, disposition, reason, n, took})."""
    reg = oracle_ctx.get("_reg")
    if not NR.ic4_prompt() or reg is None or not answers:
        return answers
    out = {}
    for qid, a in answers.items():
        sk_ = []
        b_ = (base_answers or {}).get(qid)
        out[qid] = REG.resolve_track_units(reg, a, "muse", False, (lambda p, b_=b_: b_) if isinstance(b_, str) else None, sk_, (),
                                           lambda p: REG.ANSWER_DEFAULT, lambda p: False, REG.ANSWER_DEFAULT)
        for x in sk_:
            NR.say(f"    ⚠ the Muse's brief answer {qid}: " + (f"{x['n']} sentence(s) left out ({x['reason']})" if x.get("took") == "sentences"
                   else {"base": "Block 1's answer", "default": "the answer's default"}.get(x.get("took"), x["disposition"]) + f" ({x['reason']})"))
        if isinstance(sink, dict) and sk_:
            sink[qid] = sk_
    _write_added(oracle_ctx)         # IC-4 Phase 7 (review b1)
    return out


def build_calculator_model(strategic: 'MuseStrategic') -> Dict[str, Any]:
    """Build the Campaign Reach Calculator model from Claude's channel plan: per-channel cap → digital floor → corridor normalization.
    BUILD IC-4 (D6, F12): a scenario tool — every number here is a setting of this tool, no outside source binds one; the CPA is not
    bounded (the clamp and its anchors are gone)."""
    MAX_PER_CHANNEL = 15_000  # Prevent single outlier from dominating
    digital_reach = 0
    community_reach = 0
    for ch in strategic.channel_plan:
        val = min(parse_reach(ch.reach), MAX_PER_CHANNEL)
        ch_name = ch.channel.lower()
        is_digital = any(kw in ch_name for kw in _DIGITAL_KEYWORDS)
        is_community = any(kw in ch_name for kw in _COMMUNITY_KEYWORDS)
        if is_digital:
            digital_reach += val
        elif is_community:
            community_reach += val
        else:
            digital_reach += val

    total = digital_reach + community_reach

    # A minimum 30% digital ratio — a setting of this tool
    MIN_DIGITAL_RATIO = 0.30
    if total > 0 and digital_reach / total < MIN_DIGITAL_RATIO:
        digital_reach = total * MIN_DIGITAL_RATIO
        community_reach = total * (1 - MIN_DIGITAL_RATIO)

    # Normalize to a 25K-50K corridor — a setting of this tool
    total = digital_reach + community_reach
    MIN_BASE, MAX_BASE = 25_000, 50_000
    if total < 100:
        digital_reach, community_reach = 10_000, 23_000
    elif total < MIN_BASE:
        r = MIN_BASE / total
        digital_reach *= r
        community_reach *= r
    elif total > MAX_BASE:
        r = MAX_BASE / total
        digital_reach *= r
        community_reach *= r

    return {
        "digital_base": round(digital_reach),
        "community_base": round(community_reach),
        "base_budget": 1000,
        "base_partners": 5,
        "budget_exp": 0.6,
        "qr_rate": 0.03,
        "digital_enroll_rate": 0.0003,
        "community_enroll_rate": 0.001,
        "referrals_per_partner": 1,
        "defaults": {"budget": 1000, "partners": 5},
        "ranges": {
            "budget_min": 500, "budget_max": 5000, "budget_step": 100,
            "partner_min": 2, "partner_max": 15, "partner_step": 1,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6: Context Building + Template Rendering
# ═══════════════════════════════════════════════════════════════════════════════

# ── P0.11b chat: the proxy's context file (the page carries only its digest; no key anywhere) ──
CHAT_PROXY_URL_DEFAULT = "https://naipc-chat.swaldrop2.workers.dev"


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
    file in that folder first moves to <harness>/chat-context-archive/<UTC stamp>/ (outside output\\, never uploaded; nothing
    deleted); the count is printed only when something moved."""
    doc = ctx.get("chat_context_doc")
    if not doc:
        return None
    d = Path(out_dir) / "chat-context"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ctx['chat_context_id']}.json"
    moved, dest, problem = NR.archive_contexts(d, keep=p.name)
    if moved:
        NR.say(f"  chat context: {moved} older file(s) moved to {dest}")
    if problem:
        NR.say(f"  ⚠ chat context: older files not all moved ({problem}) — the folder may hold more than one")
    note = "" if p.is_file() else NR.context_size_note(doc)
    if note:
        NR.say(f"  {note}")
    p.write_bytes(doc)
    return p


def build_muse_chat_context(ctx: dict) -> str:
    """The Muse chat's facts: program, city, personas, channel plan and KPIs. domain_label is left out (without the Oracle's
    label it is a keyword guess over the endpoint names, P0.6 D3)."""
    p = str(ctx.get("program_name") or "this program")
    c = str(ctx.get("city_name") or "")
    lines = [f"Campaign context: this outreach campaign is for {p}" + (f" in {c}." if c else "."), "", "PERSONAS:"]
    for x in ctx.get("personas") or []:
        lines.append(f"- {x.get('name', '')}, {x.get('age', '')}, {x.get('occupation', '')} ({x.get('location', '')}). "
                     f"Barriers: {'; '.join(map(str, x.get('barriers') or []))}. Reached via: {', '.join(map(str, x.get('channels') or []))}. "
                     f"Says: \"{x.get('quote', '')}\"")
    lines += ["", "CHANNEL PLAN:"]
    for x in ctx.get("channel_plan") or []:
        lines.append(f"- {x.get('channel', '')}: audience {x.get('audience', '')}; content {x.get('content_type', '')}; "
                     f"{x.get('frequency', '')}; KPI {x.get('kpi', '')}; reach {x.get('reach', '')}")
    lines += ["", "KPIs:"]
    for x in ctx.get("kpis") or []:
        lines.append(f"- {x.get('metric', '')}: {x.get('target', '')} ({x.get('timeframe', '')})")
    if _BRIEF["doc"]:  # P0.3 (D10): the brief's items and this page's answers, as the panel shows them
        lines += ["", NR.brief_chat_block(_BRIEF["doc"], "muse", _BRIEF["answers"])]
    if ctx.get("cb_chat"):   # P0.7 (D7): Block 2 — the announcement, the change summary, the alerts, what the update changed
        lines += ["", ctx["cb_chat"]]
    return "\n".join(lines)


def build_muse_faq(ctx: dict) -> list:
    """Static Q&As from the page's own content (no model call)."""
    faq = []
    ps = [str(x.get("name")) for x in ctx.get("personas") or [] if x.get("name")]
    if ps:
        faq.append({"q": "Who is this campaign for?", "a": "Its personas: " + ", ".join(ps) + "."})
    ch = [str(x.get("channel")) for x in ctx.get("channel_plan") or [] if x.get("channel")]
    if ch:
        faq.append({"q": "Which channels does it use?", "a": ", ".join(ch) + "."})
    ks = [f"{x.get('metric')} ({x.get('target', '')}, {x.get('timeframe', '')})" for x in ctx.get("kpis") or [] if x.get("metric")]
    if ks:
        faq.append({"q": "How is success measured?", "a": "; ".join(ks[:4]) + ".", "key": "kpis"})   # BUILD IC-5 (D9): its page HTML by key
    faq.append({"q": "Where is the business case?", "a": "On the Oracle page; the resource finder is on the Architect page."})
    return faq


def build_muse_chat(ctx: dict) -> dict:
    return chat_fields("muse", build_muse_chat_context(ctx), build_muse_faq(ctx))


def build_context(strategic: MuseStrategic, creative: MuseCreative,
                  oracle_ctx: Dict, domain_info: Dict,
                  architect_url: str, oracle_url: str,
                  qr_svg: str, api_key: str = "",
                  team_name: str = "Ctrl Alt Defeat",
                  competition_label: str = "NAIPC",
                  data_inventory: str = "",
                  accent_hex: str = "#8b5cf6",
                  endpoints_fetched: Optional[int] = None,
                  curveball_used: bool = False, cb: Optional[Dict[str, Any]] = None, cb_chat: str = "",
                  calls_sentence: str = "", howbuilt_curveball: str = "", plain=None, base_strategic=None, update_info=None,
                  base_run_id=None) -> Dict[str, Any]:
    """Assemble the complete Jinja2 context from strategic + creative outputs. P0.6: the label from COMPETITION_LABEL or the contract
    (D6), the badge from the Oracle's domain label when set (D3), the calendar heading from the brief's pilot (D1), the Oracle card from
    its financial model with the budget's source (D2), the counted endpoints and the curveball flag for the method text (D6, D7)."""
    F = NR.facts(_BRIEF["doc"])
    program_name = oracle_ctx.get("program_name", f"{domain_info['label']} Navigator")
    program_name_html = NR.clean_brand_html(oracle_ctx.get("brand_html"), program_name)  # P0.6: only <em> survives (printed |safe)

    # Tagline + OG description: distinct schema fields (research-validated architecture)
    # The visible header div at line 399 and the print flyer at line 580 both read
    # 'tagline' which now points at short_tagline. The og:description meta at line 8
    # reads og_description directly.

    # BUILD IC-8 (D1, Q-OG, Q-TAGLINE; d2d): each fitted once, on its shown text, with its chain (_muse_tagline_fits — finalize_muse records
    # the same fit's outcome); shown as its shown text (no token, no tag)
    reg_ = oracle_ctx.get("_reg")
    fits_ = _muse_tagline_fits(reg_, strategic, plain, base_strategic, update_info, base_run_id, program_name)
    og_, tg_ = fits_["og_description"][0], fits_["short_tagline"][0]
    ctx = {
        # Brand
        "program_name": program_name,
        "program_name_html": program_name_html,
        "tagline": REG.shown_text(tg_, reg_, "muse"),
        "og_description": REG.shown_text(og_, reg_, "muse"),
        "preview": REG.shown_text(og_, reg_, "muse"),        # BUILD IC-8 (D7): the head's preview is the og_description's shown text
        "accent_hex": (accent_hex if isinstance(accent_hex, str) and accent_hex[:1] == "#" and len(accent_hex) in (4, 5, 7, 9)
                       and all(ch_ in "0123456789abcdefABCDEF" for ch_ in accent_hex[1:]) else "#8b5cf6"),   # BUILD IC-5 (D2 e): a colour, or the default
        "domain_label": REG.fit_text(oracle_ctx.get("domain_label"), "oracle.domain_label") or domain_info["label"],   # P0.6 (D3): the
        #                                    Oracle's label (BUILD IC-8 D1: fitted where it is shown), else keyword rules
        "domain_label_title": ("Domain label from the Oracle track's analysis of the same data (Claude)" if oracle_ctx.get("domain_label")
                               else "Domain label from keyword rules over the endpoint names"),
        "city_name": oracle_ctx.get("city_name", ""),
        # Hero
        "hero_title": strategic.hero_title,
        "hero_lead": strategic.hero_lead,
        "hero_stats": strategic.hero_stats,
        # Strategic sections
        "personas": [p.model_dump() for p in strategic.personas],
        "channel_plan": [c.model_dump() for c in strategic.channel_plan],
        "calendar": [c.model_dump() for c in strategic.calendar],
        "kpis": [k.model_dump() for k in strategic.kpis],
        # Creative sections
        "social_posts": [s.model_dump() for s in creative.social_posts],
        "flyer": creative.flyer.model_dump(),
        "partner_pitch": creative.partner_pitch.model_dump(),
        # Cross-track
        "brand_handle": strategic.brand_handle,
        "architect_url": architect_url,
        "oracle_url": oracle_url,
        # Meta
        "persona_text": strategic.persona_text,
        "data_inventory": data_inventory,
        "team_name": team_name,
        "competition_label": competition_label,
        "qr_svg": qr_svg,
        "chatbot_enabled": True,
        "calc_model": build_calculator_model((plain or (strategic, creative))[0]),     # BUILD IC-4 (D16): plain words
        "calendar_heading": f"{F['pilot_days']}-Day Campaign Calendar" if F["pilot_days"] else "Campaign Calendar",  # P0.6 (D1)
        "oracle_business_line": oracle_business_line(oracle_ctx, F),                                                 # P0.6 (D2)
        "endpoints_fetched": endpoints_fetched,                                                                        # P0.6 (D6)
        "curveball_used": bool(curveball_used),                                                                        # P0.6 (D7)
        "cb": cb,                                    # P0.7 (D7): "What changed at the curveball" (None in Block 1)
        "cb_chat": cb_chat,                          # P0.7 (D7): the chat context's CURVEBALL block
        "crisis_posts": int((cb or {}).get("crisis_posts") or 0),   # the first N social posts answer the curveball
        "calls_sentence": calls_sentence or "makes two structured calls to <strong>Claude</strong> (Anthropic API)",   # P0.7 (D11)
        "howbuilt_curveball": howbuilt_curveball,    # P0.7 (D11): step 5, what ran at the curveball
    }
    ps_ = (plain or (strategic, creative))[0]        # BUILD IC-4 (D16): the chat and its questions read plain words
    ctx.update(build_muse_chat(dict(ctx, personas=[p.model_dump() for p in ps_.personas], channel_plan=[c.model_dump() for c in ps_.channel_plan],
                                    kpis=[k.model_dump() for k in ps_.kpis])))
    # BUILD IC-5 (D9, G14): the page's quick answer on success prints each KPI target as its card prints it — the page variant's fields
    # (resolve_muse_text: the model's words escaped, a target's token as its span), never the plain words again without their spans
    kq_ = [f"{k_.get('metric')} ({k_.get('target', '')}, {k_.get('timeframe', '')})" for k_ in ctx.get("kpis") or [] if k_.get("metric")]
    for it_ in ctx.get("chat_faq") or []:
        if isinstance(it_, dict) and it_.get("key") == "kpis" and kq_:
            it_["a_html"] = "; ".join(kq_[:4]) + "."
    # BUILD IC-5 (D9, G14): a persona's age the brief states (its named user, by name) printed as the brief's registered figure — the
    # Oracle's one mention at the brief with that value whose sentence names the person; else the model's number, as before
    reg_ = oracle_ctx.get("_reg")
    users_ = {str(u_.get("name") or "").strip().lower(): u_ for u_ in ((_BRIEF["doc"] or {}).get("named_users") or [])
              if isinstance(u_, dict) and isinstance(u_.get("age"), int) and not isinstance(u_.get("age"), bool)}
    for p_, pl_ in zip(ctx.get("personas") or [], ps_.personas):
        u_ = users_.get(str(pl_.name).strip().lower())
        if reg_ is None or not u_ or pl_.age != u_["age"]:
            continue
        rx_ = re.compile(r"(?<!\w)" + re.escape(str(u_["name"]).strip()) + r"(?!\w)")
        hit_ = [e_ for e_ in reg_.entries.values() if e_.get("mention") and e_.get("where") == "brief" and e_.get("value") == u_["age"]
                and rx_.search(str(e_.get("quote") or ""))]
        if len(hit_) == 1:
            p_["age_html"] = reg_.span(hit_[0])
    # BUILD IC-5 (D10, G16): a social post whose language is Spanish prints its words in lang="es" (its media words, text and tags);
    # a bilingual post holds both languages in one text with no marked parts — no lang of its own (the page's English)
    for sp_, spl_ in zip(ctx.get("social_posts") or [], (plain or (strategic, creative))[1].social_posts):
        if str(spl_.language or "").strip().lower() == "spanish":
            sp_["lang"] = "es"
    ctx["cov"] = NR.page_context("muse", answers=_BRIEF["answers"])  # P0.3 (D4, D8): every Muse block renders unconditionally; BUILD IC-5 (D1 e): Python's, never admitted
    REG.write_admit_findings(HARNESS_DIR / "checkpoints", NR.run_id())   # BUILD IC-5 (D1): every finding of this run written before the page
    return ctx


MUSE_SUMMARY = "muse_summary.json"   # P0.6 (D6): <Muse-harness>/checkpoints/muse_summary.json, read by the hub


def write_muse_summary(strategic, creative, ctx: dict, update: Optional[str] = None, dropped=None, reply_file=None, edits=None) -> str:
    """P0.6 (D6): checkpoints/muse_summary.json for the hub — this run's id and the counts the hub shows (personas, channels), so
    the hub leaves out a summary from another run. UTF-8, temp file + os.replace; never raises (returns the line to print).
    P0.7 r2 (E3 d): new keys beside the old — `dropped` (Block 2's parts not applied: {part, field, why, detail}) and `reply_file`
    (the kept update reply), both None in Block 1, as `update` is."""
    p = HARNESS_DIR / "checkpoints" / MUSE_SUMMARY
    doc = {"schema": 1, "run_id": NR.run_id(), "challenge": os.environ.get("NAIPC_CHALLENGE", "").strip() or None,
           "program_name": ctx.get("program_name"), "personas": len(strategic.personas), "channels": len(strategic.channel_plan),
           "phases": len(strategic.calendar), "social_posts": len(creative.social_posts), "languages": ["en", "es"],
           "calendar_heading": ctx.get("calendar_heading"), "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "update": update,   # P0.7 (D8): Block 2's update status for the pipeline's end summary (None in Block 1)
           "dropped": dropped, "reply_file": reply_file}   # P0.7 r2 (E3 d)
    if edits is not None:        # BUILD IC-6 (D12): the keyed edits' record (each item's status, the ids after the update) — a new key
        doc["edits"] = edits     # beside the old, only when the update was keyed edits
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, p)
        return f"✓ Muse summary for the hub: {p} (run {doc['run_id']}: {doc['personas']} personas, {doc['channels']} channels)"
    except Exception as e:  # noqa: BLE001 — the hub then leaves the Muse counts out
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"⚠ Muse summary not written ({type(e).__name__}: {e}) — the hub will leave the Muse counts out"


def muse_update_status(info):
    """BUILD IC-6 (D7): Block 2's update for the status file — updated (the whole-field update applied: each amendable field revised at
    the curveball, its edit not applied, or reviewed and unchanged), not updated (the call did not complete: Block 1's campaign), the two
    calls run again (--curveball-full)."""
    st = str((info or {}).get("status") or "")
    if st == "full" or st.startswith("no base"):
        return {"state": "updated", "summary": "the two calls ran again at the curveball", "why": None if st == "full" else st}
    if st.startswith("failed: declined"):
        return {"state": "declined", "why": st[len("failed: "):]}
    if st != "applied":
        return {"state": "not updated", "why": "the update did not complete" + (f": {st[len('failed: '):]}" if st.startswith("failed: ") else
                                                                               f" ({st})" if st else "")}
    e_ = (info or {}).get("edits") if isinstance((info or {}).get("edits"), dict) else None
    if e_:        # BUILD IC-6 (D12): the keyed edits — partly updated when a cut left items not reviewed; each part by its items
        c_ = e_.get("counts") or {}
        us_ = muse_update_line(info)          # BUILD IC-8 (D13): the one status line; its crisis posts are in it
        return {"state": "partly updated" if int(c_.get("not reviewed") or 0) else "updated",
                "summary": us_["line"], "status_problems": us_["problems"], "counts": c_,
                "parts": muse_update_parts(e_), "why": (f"the reply: {e_.get('how')}" if e_.get("how") not in (None, "end_turn") else None)}
    am = {a.get("field") for a in (info or {}).get("amended") or [] if isinstance(a, dict)}
    dr = {d.get("field") for d in (info or {}).get("dropped") or [] if isinstance(d, dict)}
    parts = [{"part": muse_part_name({"field": f}), "status": "revised at the curveball" if f in am else   # E3's page names
              "from Block 1 — its edit was not applied" if f in dr else "reviewed, unchanged", "why": None} for f in MUSE_AMENDABLE]
    n = int((info or {}).get("crisis_posts") or 0)
    return {"state": "updated", "summary": f"{len(am)} of {len(MUSE_AMENDABLE)} fields amended" + (f", {n} crisis post(s) added" if n else ""),
            "parts": parts, "why": None}


def render_html(ctx: Dict[str, Any]) -> str:
    """Render the Muse template with the given context."""
    env = REG.page_env(Environment, FileSystemLoader(str(HARNESS_DIR)), authored=True, build=NR.BUILD_ID)   # BUILD IC-5 (D9): authored, as the Oracle's —
    #                                    unmarked text is Python's; the model's fields carry their marks; the brief's | org (D2 a: one environment)
    template = REG.page_template(env, "muse_template.html.j2")
    html = template.render(**ctx)       # BUILD IC-5 (D2 b): finalize sees every printed value (T12's tag_tree(ctx) gone)
    for p_ in sorted(set(REG.page_problems())):          # BUILD IC-5 (D2 b): what finalize escaped or repaired, by template line
        NR.say(f"    ⚠ authorship: {p_}")
    for t_ in sorted(set(REG.page_tags())):              # a tag finalize met at output (shown as words; a test fails on one)
        NR.say(f"    ⚠ a tag at output, shown as words: {t_}")
    return html


# ═══════════════════════════════════════════════════════════════════════════════
# CLI and Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Muse Track Harness — Marketing Playbook Generator")
    sub = parser.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run")
    run_p.add_argument("--api-url", required=True, help="Base API URL (e.g. https://aipromptchamp.com/api/challenge/)")
    run_p.add_argument("--endpoints", required=True, help="Comma-separated endpoint names")
    run_p.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""), help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    run_p.add_argument("--oracle-dir", default="", help="Path to Oracle checkpoint directory (for cross-track consistency)")
    run_p.add_argument("--architect-url", default="https://ctrl-alt-defeat-arch.netlify.app", help="Architect Netlify URL for QR code and cross-links")
    run_p.add_argument("--oracle-url", default="https://ctrl-alt-defeat.netlify.app", help="Oracle Netlify URL for cross-links")
    run_p.add_argument("--program-name", default="", help="Override program name (if not using Oracle checkpoints)")
    run_p.add_argument("--accent-hex", default="#8b5cf6", help="Brand accent color (hex)")
    run_p.add_argument("--curveball", default="", help="Curveball text to adapt messaging")
    run_p.add_argument("--curveball-full", action="store_true",
                       help="P0.7 (D7): in Block 2, run the two calls again (with the change list) instead of updating Block 1's campaign")
    args = parser.parse_args()

    if args.cmd != "run":
        parser.print_help()
        return
    NR.paths_home(HARNESS_DIR / "checkpoints", "muse")       # BUILD IC-8 (D14, I8): this run's path markers

    if not args.api_key:
        console.print("[red]Error: --api-key required (or set ANTHROPIC_API_KEY env var)[/red]")
        sys.exit(1)

    t0 = time.time()
    console.print("[bold]═══ Muse Track Harness — Marketing Playbook ═══[/bold]")
    NR.write_track_status(HARNESS_DIR / "checkpoints", "muse", "started", block=2 if args.curveball else 1)     # BUILD IC-6 (D7)

    # ── Step 1: Fetch API data ──
    console.print("\n[bold cyan]Phase 1: Fetching API data[/bold cyan]")
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    raw_endpoints = fetch_endpoints(args.api_url, endpoints)
    REG.set_endpoints(endpoints)        # BUILD IC-5 Phase 4 (the replays): the run's endpoint names — admit reads the evidence's "[<name>]" by them

    # ── Step 2: Detect domain ──
    domain_info = detect_domain(raw_endpoints)
    console.print(f"  [green]✓ Domain: {domain_info['domain']}[/green]")

    # ── Step 3: Load Oracle checkpoints (if available) ──
    oracle_ctx = {}
    if args.oracle_dir:
        # Sanitize path: Windows argparse can mangle paths ending with backslash
        # by treating \" as an escaped quote, appending garbage to the string
        raw_path = args.oracle_dir.strip().strip('"').strip("'").rstrip('\\').rstrip('/')
        oracle_path = Path(raw_path)
        console.print(f"\n[bold cyan]Phase 2: Loading Oracle checkpoints[/bold cyan]")
        console.print(f"  [cyan]Raw arg: {repr(args.oracle_dir)}[/cyan]")
        console.print(f"  [cyan]Sanitized: {oracle_path}[/cyan]")
        console.print(f"  [cyan]Exists: {oracle_path.exists()}[/cyan]")

        # If exact path doesn't exist, try common alternatives
        if not oracle_path.exists():
            alternatives = [
                oracle_path / "checkpoints",           # Maybe they pointed at parent
                oracle_path.parent / "checkpoints",     # Maybe they included a filename
                Path(raw_path.rsplit(' ', 1)[0]),       # Maybe argparse ate the next arg
            ]
            for alt in alternatives:
                if alt.exists() and (alt / "master_analysis.json").exists():
                    console.print(f"  [yellow]Found checkpoints at: {alt}[/yellow]")
                    oracle_path = alt
                    break

        if oracle_path.exists():
            ma = oracle_path / "master_analysis.json"
            fm = oracle_path / "financial_model.json"
            console.print(f"  [cyan]master_analysis.json: {ma.exists()}[/cyan]")
            console.print(f"  [cyan]financial_model.json: {fm.exists()}[/cyan]")
            oracle_ctx = load_oracle_checkpoints(str(oracle_path))
            if oracle_ctx.get("program_name"):
                console.print(f"  [green]✓ Loaded: {oracle_ctx['program_name']}[/green]")
            else:
                console.print(f"  [yellow]⚠ Checkpoints loaded but no program_name found[/yellow]")
        else:
            console.print(f"  [yellow]⚠ Path does not exist: {oracle_path}[/yellow]")
            parent = oracle_path.parent
            if parent.exists():
                try:
                    contents = list(parent.iterdir())
                    console.print(f"  [yellow]Contents of {parent}: {[p.name for p in contents[:10]]}[/yellow]")
                except Exception:
                    pass
            console.print(f"  [yellow]Continuing without Oracle context (numbers may differ from Oracle track)[/yellow]")
    elif args.program_name:
        oracle_ctx = {"program_name": args.program_name, "city_name": ""}
    NR.say(f"  Domain label: {oracle_ctx.get('domain_label') or domain_info['label']} — "
           + ("the Oracle's analysis" if oracle_ctx.get("domain_label") else "keyword rules over the endpoint names"))  # P0.6 (D3)

    # BUILD IC-8 (P4.4, p44v): the run's names before any text is fitted — the measure reads the words the page pass prints, and the
    # page pass reads this object (P4.find-measure-class)
    names_ = REG.set_measure_names(REG.ReaderNames(raw_endpoints, NR.records, before=getattr(oracle_ctx.get("_reg"), "_before_raw", None))
                                   if isinstance(raw_endpoints, dict) else None)

    # ── Step 4: Claude API calls ──
    console.print("\n[bold cyan]Phase 3: Generating marketing content via Claude[/bold cyan]")
    api_summary = _summarize_api_data(raw_endpoints)
    _BRIEF.update(doc=_brief_contract(), facts=muse_pipeline_facts(len(endpoints) - len(FETCH_ERRORS)), raw=None, answers={})  # P0.3b

    # P0.7 (D7): Block 2 with Block 1's campaign of this challenge (and of the change list's Block 1 run) → ONE update call over it;
    # no such campaign or --curveball-full → the two calls again, with the announcement AND Python's change list in both (printed)
    changes = cv = base = info = None
    if args.curveball:
        changes, cwhy = REG.admit(NR.changes_from_env(), "data", "change list")   # BUILD IC-5 (D1)
        if changes is None:
            NR.say(f"  curveball: no change list for this run ({cwhy}) — the announcement alone")
        lcv = (changes or {}).get("curveball")
        cv = lcv if isinstance(lcv, dict) and str(lcv.get("text") or "").strip() else {
            "kind": "VERBATIM", "source": "--curveball", "announced_at": None, "text": args.curveball, "tests": [], "api_changed": []}
        if args.curveball_full:
            info = {"status": "full"}
            NR.say("  curveball: --curveball-full — the two calls run again with the change list (no update of Block 1's campaign)")
        else:
            bdet = {}
            base, bwhy = load_muse_block1(os.environ.get("NAIPC_CHALLENGE", "").strip() or None, (changes or {}).get("block1_run_id"), detail=bdet)
            if base is None:
                info = {"status": f"no base: {bwhy}"}
                NR.say(f"  curveball: {bwhy}" + (f" ({bdet['why']})" if bdet.get("why") else "") + " — the two calls run again with the change list")
    if base is not None:
        strategic, creative, info = run_muse_update(args.api_key, base, changes, cv, api_summary, oracle_context_block(oracle_ctx, domain_info),
                                                    muse_rules_parts(oracle_ctx, "muse/update"), reg=oracle_ctx.get("_reg"))   # IC-6 (D12); IC-7 (D4)
        NR.say(f"  curveball: update {info['status']} · {info.get('crisis_posts', 0)} crisis post(s) · {len(info.get('amended') or [])} amendment(s)")
        # BUILD IC-8 (D2): Block 1's answers stand (the update changes the campaign, not the brief), re-resolved AFTER the counts are sealed
        strategic, creative, plain, _BRIEF["answers"], _raw = finalize_muse(oracle_ctx, strategic, creative, info, base=base,
                                                                            program_name=oracle_ctx.get("program_name", f"{domain_info['label']} Navigator"))
        NR.say(f"    {calendar_check(plain[0], NR.facts(_BRIEF['doc'])['pilot_days'])}")     # BUILD IC-5 (D7): Block 2 too, on its words
    else:
        cb_text = args.curveball
        if args.curveball:
            sw = {"same_words": NR.CHANGES_SAME_IC4} if NR.ic4_prompt() else {}       # BUILD IC-4 (D16; open item 5)
            cb_text += "\n\n" + (NR.changes_prompt_block(changes, **sw) if changes else
                                  NR.changes_prompt_block({"curveball": cv, "endpoints": None, "snapshot": {"source": "none"}}, **sw))
        strategic = generate_strategic(
            args.api_key, api_summary, domain_info, oracle_ctx, cb_text
        )

        creative = generate_creative(
            args.api_key, strategic, domain_info, oracle_ctx, cb_text
        )
        written = (strategic, creative)        # BUILD IC-4 (D16): Block 1's campaign as written — its tokens kept for the update's base
        # BUILD IC-8 (D2): the one finalization — counts, seal, texts, then the brief's answers (P0.3b (D9): after Call 2)
        strategic, creative, plain, _ans, raw_answers = finalize_muse(
            oracle_ctx, strategic, creative, answers_of=(lambda pl: muse_brief_answers(args.api_key, *pl)) if _BRIEF["doc"] else None,
            program_name=oracle_ctx.get("program_name", f"{domain_info['label']} Navigator"))
        NR.say(f"    {calendar_check(plain[0], NR.facts(_BRIEF['doc'])['pilot_days'])}")  # P0.6 (D1); BUILD IC-5 (D7): after its words resolve
        if _BRIEF["doc"]:
            _BRIEF["answers"] = _ans
        if not args.curveball:                                  # P0.7 (D7): Block 1's campaign, for the curveball update
            NR.say(f"  {write_muse_block1(*written, _BRIEF['answers'], raw_answers if _BRIEF['doc'] and NR.ic4_prompt() else None, dict(_MUSE_ITEM_IDS) or None)}")

    # ── Step 5: Generate QR code ──
    qr_url = f"{args.architect_url}?utm_source=muse_flyer&utm_medium=qr"
    qr_svg = generate_qr_svg(qr_url)
    if qr_svg:
        console.print(f"  [green]✓ QR code generated for {args.architect_url}[/green]")

    # ── Step 6: Build data inventory ──
    # Helper: determine record count for each endpoint payload
    def _ep_count(ep_name, p):
        return len(NR.records(p))   # P0.7 (D5): the one envelope rule — no event's field names (a flat object is one record)

    total_records = sum(_ep_count(ep, p) for ep, p in raw_endpoints.items())
    ep_summary = ", ".join(f"{ep} ({_ep_count(ep, p)})" for ep, p in raw_endpoints.items())
    data_inventory = f"{len(raw_endpoints)} API endpoints ({total_records} records): {ep_summary}. Marketing content generated by Claude (Anthropic API) with Pydantic validation."

    # ── Step 7: Build context and render ──
    console.print("\n[bold cyan]Phase 4: Rendering Marketing Playbook[/bold cyan]")
    # P0.7 (D7, D11): Block 2's block, its chat lines and the method sentences — each says what ran on this path
    cbv = curveball_view(info, changes, cv) if args.curveball else None
    if cbv:                           # BUILD IC-4 (D3, D1): the announcement's words, each quantity its mention's span
        reg_ = oracle_ctx.get("_reg")
        cbv["first_h"] = reg_.quote_html("announcement", cbv.get("first") or "") if reg_ is not None else REG.esc(cbv.get("first") or "")
    st = str((info or {}).get("status") or "")
    with_list = " and Python's change list" if (changes or {}).get("endpoints") is not None else ""   # only when the data was compared
    if not args.curveball:
        calls, howb = "", ""
    elif st == "applied":
        calls = ("made two structured calls to <strong>Claude</strong> (Anthropic API) in Block 1 and, at the curveball, one update call "
                 "over that campaign")
        howb = (f"One update call at the curveball went over Block 1's campaign, the announcement{with_list}: {len(cbv['amended'])} "
                "field(s) amended, each kept only with a reason that names a real change, and "      # review F10: the reason rule is the
                f"{cbv['crisis_posts']} crisis-response post(s) added; everything else is Block 1's.")  # amendments', not the posts'
        if isinstance((info or {}).get("edits"), dict):         # BUILD IC-6 (D12): the keyed edits
            e_ = info["edits"]
            howb = (f"One update call at the curveball reviewed Block 1's campaign item by item, with the announcement{with_list}. "
                    f"{muse_update_line(info)['line']} Each change was applied only with a reason that names a "      # BUILD IC-8 (D13)
                    "real change, crisis-response posts first; everything else is Block 1's.")
    elif st.startswith("failed"):
        calls = "made two structured calls to <strong>Claude</strong> (Anthropic API) in Block 1 (the curveball's update call did not complete)"
        howb = ("The update call at the curveball did not complete, so this is Block 1's campaign"
                + ("; the change list above is Python's." if with_list else "."))
    else:
        calls = ""
        howb = (f"The two calls ran again at the curveball with the announcement{with_list}; the creative call was asked "
                "for one crisis-response post.")
    cb_chat = ""
    if args.curveball:
        extra = [muse_mode_sentence(info, changes, chat=True)]          # P0.7 r2 (E3 c): the page's mode sentence, never the raw status
        extra += NR.not_applied_chat_lines(muse_not_applied(info))    # right after it, before the track's longer lines
        extra += [f"Part: {x}" for x in cbv.get("parts") or []]       # BUILD IC-6 (D12): each part's status and every stale item
        extra += [f"Stale: {s_['where']} names {s_['names']}, revised at the curveball" for s_ in cbv.get("stale") or []]
        extra += [f"What changed for the campaign: {w['ref']} — {w['impact']}" for w in cbv["what_changed"]]
        extra += [f"Amended: {a['name']} (because {a['because']})" for a in cbv["amended"]]
        if cbv["crisis_posts"]:
            extra.append(f"Crisis-response posts added: {cbv['crisis_posts']} (the first social posts on the page)")
        cb_chat = NR.changes_chat_block(changes or {"curveball": cv, "summary": []}, extra=extra)
    ctx = build_context(
        strategic, creative, oracle_ctx, domain_info,
        args.architect_url, args.oracle_url, qr_svg,
        api_key=args.api_key, data_inventory=data_inventory,
        accent_hex=args.accent_hex,
        competition_label=NR.label_from_env(_BRIEF["doc"]),           # P0.6 (D6)
        endpoints_fetched=len(endpoints) - len(FETCH_ERRORS), curveball_used=bool(args.curveball),
        cb=cbv, cb_chat=cb_chat, calls_sentence=calls, howbuilt_curveball=howb, plain=plain,
        base_strategic=(base or {}).get("strategic") if base is not None else None,     # BUILD IC-8 (D1): the chain's Block 1 value
        update_info=info if base is not None else None, base_run_id=((base or {}).get("meta") or {}).get("run_id") if base is not None else None,   # (D2)
    )
    if args.curveball:              # BUILD IC-6 (D12): each list item's mark — not reviewed, its edit not applied, stale (the keyed edits)
        marks_ = muse_item_marks(info)
        for f_, k_ in (("strategic.personas", "personas"), ("strategic.channel_plan", "channel_plan"), ("strategic.calendar", "calendar"),
                       ("strategic.kpis", "kpis"), ("creative.social_posts", "social_posts")):
            for x_, m_ in zip(ctx.get(k_) or [], marks_.get(f_) or []):
                if m_ and isinstance(x_, dict):
                    x_["cb_mark"] = m_
    html, notes_ = REG.place_notes(render_html(ctx), oracle_ctx.get("_reg"))     # BUILD IC-6 (D9): each "sources disagree" a note
    #                                                                       after its sentence — before the page's line reads it
    html, rw_ = REG.reader_page(html, names_)      # BUILD IC-8 (P4.4, p44v): the names the measure read
    if REG.reader_words_line(rw_):      # BUILD IC-8 (d12c): what a reader meets, in a reader's words — before the page's line
        NR.say(f"  {REG.reader_words_line(rw_)}")
    # BUILD IC-4 (D7): the page's integrity line, from its own text, the Oracle's registry of this run and the data's exempter; the
    # Muse's prose is counted as typed (no link), so its "no source found" stays high until Part 2's tokens (D16)
    # BUILD IC-5 (D9): the page is authored — its numerals (each with its author: the marks' record) go to checkpoints, since the page
    # is written unmarked and its line is recomputed from them
    html, pline, nwhy = REG.place_page_line_numerals(html, oracle_ctx.get("_reg"),
                                                     REG.Exempter.from_data(raw_endpoints, NR.records) if isinstance(raw_endpoints, dict) else None,
                                                     HARNESS_DIR / "checkpoints" / REG.NUMERALS_FILE, NR.run_id())
    if pline:
        console.print(f"  {pline}")
    if nwhy:
        NR.say(f"  ⚠ checkpoints/{REG.NUMERALS_FILE} not written ({nwhy}): this page's line cannot be recomputed")

    # ── Step 8: Write output ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "index.html"
    if REG.notes_words(notes_):         # BUILD IC-6 (D9): the page's notes, said and recorded
        NR.say(f"  {REG.notes_words(notes_)}")
    nw_ = REG.write_page_notes(HARNESS_DIR / "checkpoints", notes_, NR.run_id())
    if nw_:
        NR.say(f"  ⚠ checkpoints/{REG.NOTES_FILE} not written ({nw_})")
    out_path.write_text(REG.strip_build_notes(REG.unmark(html)), encoding="utf-8")   # IC-4 Phase 7 (review c21): no build note in
    #                                                     its scripts; BUILD IC-5 (D9): no mark leaves the process (as the Oracle's page)
    write_chat_context(OUTPUT_DIR, ctx)  # P0.11b: the proxy's context file, uploaded with the page
    console.print(f"  [green]✓ Written to {out_path} ({len(html):,} bytes)[/green]")
    summary_line = write_muse_summary(strategic, creative, ctx, update=st or None,   # P0.6 (D6) + P0.7 (D8): this run's counts and update
                                      dropped=list((info or {}).get("dropped") or []) if args.curveball else None,   # P0.7 r2 (E3 d)
                                      reply_file=(info or {}).get("reply_file") if args.curveball else None,
                                      edits=(info or {}).get("edits") if args.curveball else None)      # BUILD IC-6 (D12)
    NR.say(f"  {summary_line}")
    NR.write_track_status(HARNESS_DIR / "checkpoints", "muse", "built", update=muse_update_status(info) if args.curveball else None,
                          page_run=NR.page_run_of(out_path), block=2 if args.curveball else 1,      # BUILD IC-6 (D7)
                          content=dict(_MUSE_CONTENT) or None)                                      # BUILD IC-8 (D3)
    NR.gate("muse", out_path, HARNESS_DIR / "coverage", fetch_errors=FETCH_ERRORS)  # P0.3 (D7): prints, never raises

    elapsed = time.time() - t0
    console.print(f"\n[bold green]═══ Done in {elapsed:.1f}s ═══[/bold green]")

    # Auto-open in browser
    import webbrowser
    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    try:
        main()
    except BaseException as _e:     # BUILD IC-6 (D7): the top-level handler — "failed" in the status file (best effort)
        NR.track_failed(HARNESS_DIR / "checkpoints", "muse", _e)
        raise
