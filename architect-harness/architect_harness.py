#!/usr/bin/env python3
"""
Architect Track Harness — NAIPC Competition
Generates an interactive civic resource navigation tool from the challenge API's data, fetched at run time.
Deployed as a single static HTML page.

Usage:
    python architect_harness.py run \
        --api-url https://aipromptchamp.com/api/challenge/ \
        --endpoints pantries,transit,demographics,store-closures,...
"""

import argparse
import base64
import hashlib
import html as _html
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter

import requests
from jinja2 import Environment, FileSystemLoader

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import naipc_requirements as NR  # noqa: E402 — P0.3: the brief, the coverage map, the page attributes and the gate
NR.safe_stdio()   # P0.6 red team RT-3: printing that cannot raise (a cp1252 pipe stopped this harness at its first '→')
import naipc_engine as ENG  # noqa: E402 — BUILD B2 (D7): the engine (its copy in this folder); the closure rules below are its own
import naipc_registry as REG  # noqa: E402 — BUILD IC-4 (D3): the Oracle's registry of this run, the one registry (its copy in this folder)
REG.admit_readers(NR, ENG)   # BUILD IC-5 (D1 e): the contract, the coverage map and the engine sidecar, wherever this process reads them
REG.set_admit_rewrite(NR.ic5_prompt())   # BUILD IC-5 (D1 f): the rewrite only with this build's prompt switch on

# Optional QR code support (graceful if not installed)
try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False

# Legacy qrcode import kept for compatibility but not used for QR generation
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

try:
    from rich.console import Console
    console = Console()
except ImportError:
    class _FallbackConsole:
        def print(self, *a, **kw): print(*a)
    console = _FallbackConsole()

# ─── Constants ───────────────────────────────────────────────────────────────
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
HARNESS_DIR = Path(__file__).parent
OUTPUT_DIR = HARNESS_DIR / "output"
FETCH_ERRORS: List[str] = []  # P0.3 (finding 5): endpoints whose fetch failed this run, reported by the coverage gate
VENDOR_DIR = HARNESS_DIR / "vendor"  # P0.6 (D10, Rule 7): Chart.js 4.4.7 inlined into the page
CHARTJS_FILE, CHARTJS_SHA256 = "chart.umd.js", "2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb"
LANG_KEYS = ("language", "languages", "supported languages")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Data Fetching (shared pattern with Oracle)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_endpoints(api_url: str, endpoints: List[str], timeout: int = 30) -> Dict[str, Any]:
    """Fetch all API endpoints and return {endpoint_name: parsed_json}."""
    results = {}
    for ep in endpoints:
        url = NR.endpoint_url(api_url, ep)  # P0.6 (D8): the one URL builder (NAIPC_URL_SLASH=1 when the server answers only <ep>/)
        console.print(f"  [cyan]→ Fetching {url}[/cyan]")
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            results[ep] = r.json()
            count = len(NR.records(results[ep]))   # P0.7 (D5): the one envelope rule — a bare list or an alerts list is counted, never "?"
            console.print(f"    [green]✓ {ep}: {count} records[/green]")
        except Exception as e:
            NR.say(f"    ✗ {ep}: {e}")          # BUILD IC-7 (D8): plain — rich markup ate "[...]" from an error's text
            results[ep] = {"source": ep, "count": 0, "data": []}
            FETCH_ERRORS.append(ep)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Resource Normalization (the core of the Architect harness)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_id(payload: str) -> str:
    """Generate a short stable ID from a string."""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _coerce_float(v: Any) -> Optional[float]:
    """Try to parse a float from various types."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _extract_zip(text: str) -> Optional[str]:
    """Extract a 5-digit ZIP from text."""
    m = ZIP_RE.search(text or "")
    return m.group(1) if m else None


def _pretty_label(key: str) -> str:
    """Convert a camelCase or snake_case key to a human-readable label.
    Per Hardening Report: strip 'pct' prefix, append 'Rate'. Handle abbreviations and units."""
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', key)
    s = re.sub(r'(\d)([A-Z])', r'\1 \2', s)  # digit→uppercase: "65U" → "65 U"
    s = s.replace('_', ' ').replace('-', ' ')
    words = s.strip().split()
    abbrs = {'er', 'fqhc', 'ob', 'icu', 'copd', 'id', 'avg'}
    unit_words = {'mi': 'mi', 'km': 'km', 'hrs': 'hrs', 'min': 'min', 'days': 'days'}
    result = []
    unit_suffix = ""
    had_pct = False
    for w in words:
        wl = w.lower()
        if wl in unit_words and w == words[-1]:
            unit_suffix = f" ({unit_words[wl]})"
        elif wl in abbrs:
            result.append(wl.upper())
        elif wl == 'pct':
            had_pct = True  # Don't embed %, will append "Rate" if needed
            continue
        else:
            result.append(w.capitalize())
    label = ' '.join(result) + unit_suffix
    # If "pct" was stripped and label doesn't already end with "Rate", append it
    # EXCEPT when label starts with a negation ("No", "Below") — "No Vehicle Rate" is awkward;
    # "No Vehicle" + chart suffix "(%)‎" reads naturally as "No Vehicle (%)"
    negation_prefixes = ('no ', 'below ', 'under ', 'without ')
    if had_pct and not label.lower().endswith('rate') and not any(label.lower().startswith(n) for n in negation_prefixes):
        label = label.strip() + ' Rate'
    return label


def _normalize_pct(v: float) -> float:
    """Per Hardening Report Root Cause 1: if v is a fraction (0 <= v <= 1), convert to
    percentage (×100) BEFORE rounding. KC's 38.2 passes through; Chicago's 0.142 → 14.2."""
    if v is None:
        return None
    if 0.0 <= v <= 1.0:
        v = v * 100
    return round(v, 1)


# Per Hardening Report Root Cause 6: internal identifiers to skip in display
SKIP_DISPLAY_FIELDS = {'areacode', 'area_code', 'area code', 'id', 'objectid', 'fips'}


def _pretty_value(val: Any) -> Any:
    """Clean up raw string values like 'small_fridge' → 'Small Fridge', 'industrial' → 'Industrial', 'walk-in' → 'Walk-In'."""
    if isinstance(val, str) and '_' in val and len(val) < 50:
        return val.replace('_', ' ').strip().title()
    # Capitalize hyphenated lowercase values like "walk-in" → "Walk In", "primary-care" → "Primary Care"
    if isinstance(val, str) and '-' in val and val == val.lower() and len(val) < 30:
        return ' '.join(w.capitalize() for w in val.split('-'))
    # Also capitalize single-word lowercase enum values like "industrial"
    if isinstance(val, str) and val.islower() and ' ' not in val and '-' not in val and len(val) < 30 and val.isalpha():
        return val.capitalize()
    if isinstance(val, list):
        return [_pretty_value(v) for v in val]
    return val


def _is_resource_endpoint(ep_name: str, payload: Any) -> bool:
    """Heuristic: does this endpoint contain user-facing resources (not stats/aggregates)?"""
    if NR.is_alerts(payload):   # P0.7 (D5): an alert-shaped payload is never a resource endpoint, whatever its name
        return False
    # Skip endpoints that are clearly aggregate stats
    skip_names = {'demographics', 'food-atlas', '311-calls', '311-health',
                  'er-visits', 'patient-displacement', 'supply-alerts',
                  'store-closures', 'harvest'}
    if ep_name.lower() in skip_names:
        return False
    # Must have list data with records containing names/addresses
    records = _extract_records(payload)
    if len(records) < 1:
        return False
    # Check if records have name-like or address-like fields
    sample = records[0]
    keys_lower = {k.lower() for k in sample.keys()}
    has_name = any(k in keys_lower for k in ('name', 'title', 'facility', 'organization', 'provider'))
    has_location = any(k in keys_lower for k in ('address', 'zip', 'lat', 'location', 'city'))
    return has_name or has_location


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    """The payload's records by the one envelope rule of all four harnesses (P0.7, D5: NR.records — a list under data, alerts,
    items, results, records, facilities or resources; {"data": {…}} one record; another object one record; a list itself), the
    objects among them. An alerts payload's records are its alerts: _is_resource_endpoint keeps them out of the finder."""
    return [x for x in NR.records(payload) if isinstance(x, dict)]


# ── P0.7 (D5): alerts by shape — every alerts payload becomes crisis events, whatever the endpoint is named ──
# BUILD B2 (D7): the closure rules are the engine's objects (one copy; B1's E-1 listed the twelve): ALERT_TITLE_KEYS, ALERT_DETAIL_KEYS,
# _first_text, STATUS_KEYS, CLOSURE_WORDS, DATA_CLOSED, AT_RISK, _norm_status, record_status, _JOIN_END, _item_lines, apply_curveball.
from naipc_engine import (ALERT_TITLE_KEYS, ALERT_DETAIL_KEYS, _first_text, STATUS_KEYS, CLOSURE_WORDS, DATA_CLOSED,  # noqa: E402
                          AT_RISK, _norm_status, record_status, _JOIN_END, _item_lines, apply_curveball)
ALERT_PLACE_KEYS = ("impactedZips", "impactedAreas", "area")


def _alert_events(ep_name: str, payload: Any) -> List[Dict[str, Any]]:
    """D5: an alert-shaped payload's alerts as crisis events — each keeps the alert's id, title (headline, title or name), detail
    (summary or description), severity, date, type and the places or groups it names (impactedZips, impactedAreas, area;
    affectedGroups — the three events' own keys) and its endpoint. Never raises."""
    out = []
    for a in NR.records(payload):
        if not isinstance(a, dict):
            continue
        places = []
        for k in ALERT_PLACE_KEYS:
            v = a.get(k)
            if isinstance(v, list):
                places += [str(x) for x in v if isinstance(x, (str, int)) and not isinstance(x, bool)]
            elif isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
                places.append(str(v))
        groups = a.get("affectedGroups")
        out.append({"type": "alert", "id": str(a.get("id", "")), "name": _first_text(a, ALERT_TITLE_KEYS),
                    "detail": _first_text(a, ALERT_DETAIL_KEYS), "severity": str(a.get("severity", "")), "date": str(a.get("date", "")),
                    "kind": str(a.get("type", "")), "places": places,
                    "category": ", ".join(str(g) for g in groups) if isinstance(groups, list) else (str(groups) if groups else ""),
                    "endpoint": ep_name})
    return out


# ── P0.7 (D6): a record's own status — record_status, imported from the engine above (B2, D7) ──


def normalize_resources(raw_endpoints: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize all resource-like endpoints into a canonical schema for the
    interactive tool. Also extract dashboard data from non-resource endpoints
    (demographics, 311 calls, store closures, supply alerts) for visualization.

    Returns: {domain_label, resources, filter_hints, zip_centroids, data_sources,
              zip_stats, crisis_events}
    """
    resources = []
    all_zips = {}  # zip -> (lat, lng) for centroid computation
    data_sources = []
    zip_stats = []  # Demographics data for charts
    crisis_events = []  # Store closures, supply alerts, 311 calls for crisis tab
    system_snapshot = {}  # Aggregate community-level data (e.g., PIT count, bed totals)

    # Pre-pass: build hospital name lookup for displacement events
    hosp_names = {}
    for ep_name, payload in raw_endpoints.items():
        if ep_name.lower() == 'hospitals' and isinstance(payload, (dict, list)):
            for rec in _extract_records(payload):
                hid = rec.get("id", "")
                hname = rec.get("name", "")
                if hid and hname:
                    hosp_names[str(hid)] = str(hname)

    for ep_name, payload in raw_endpoints.items():
        # Guard: skip non-dict/list payloads (malformed API responses)
        if not isinstance(payload, (dict, list)):
            data_sources.append({
                "id": ep_name,
                "endpoint": f"/{ep_name}",
                "returns": ep_name,
                "records": 0,
            })
            continue

        records = _extract_records(payload)
        count = len(records)
        data_sources.append({
            "id": ep_name,                  # P0.6 (D6): the endpoint's name; build_context shows the contract's documented path
            "endpoint": f"/{ep_name}",
            "returns": payload.get("source", ep_name) if isinstance(payload, dict) else ep_name,
            "records": count,
        })

        # ── Extract dashboard data from non-resource endpoints ──
        ep_lower = ep_name.lower()

        # Community snapshot → system-wide aggregate (single object, NOT a record array)
        if ep_lower == 'community-snapshot' and isinstance(payload, dict):
            system_snapshot = {k: v for k, v in payload.items()
                              if k not in ('source', 'lastUpdated')}
            data_sources[-1]["records"] = 1  # Single aggregate snapshot
            continue  # Not a resource endpoint — skip normalization

        # P0.7 (D5): alerts BY SHAPE — every alerts payload's alerts become crisis events (id, title, detail, severity, date, type,
        # places, groups), whatever the endpoint is named; never a resource endpoint (the name-keyed system-status and supply-alerts
        # branches are gone: KC's and Chicago's supply alerts were read as records of a list they are not in, and dropped)
        if NR.is_alerts(payload):
            evs = _alert_events(ep_name, payload)
            crisis_events.extend(evs)
            data_sources[-1]["records"] = len(evs)
            continue

        # Demographics → zip_stats for charts
        if ep_lower in ('demographics',):
            for rec in records:
                z = str(rec.get("zip", rec.get("zipCode", rec.get("area", "")))).strip()
                if z:
                    stat = {"zip": z}
                    for k, v in rec.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            label = _pretty_label(k)
                            # Skip internal identifiers per Hardening Report Root Cause 6
                            if label.lower().replace(' ', '') in SKIP_DISPLAY_FIELDS:
                                continue
                            # Normalize percentages: detect 0-1 fractions → ×100 before rounding
                            stat[label] = _normalize_pct(v)
                    zip_stats.append(stat)
                    # Collect centroid if available
                    lat = _coerce_float(rec.get("lat"))
                    lng = _coerce_float(rec.get("lng", rec.get("lon")))
                    if lat and lng:
                        all_zips[z] = (lat, lng)

        # Store closures → crisis events
        if ep_lower in ('store-closures',):
            for rec in records:
                crisis_events.append({
                    "type": "closure",
                    "name": str(rec.get("store", rec.get("name", "Unknown"))),
                    "zip": str(rec.get("zip", rec.get("area", ""))),
                    "date": str(rec.get("closed", rec.get("date", rec.get("closureDate", "")))),
                    "impact": rec.get("impact", rec.get("impactedPopulation", 0)),
                    "detail": str(rec.get("sqft", rec.get("size", ""))),
                })

        # Hospital/clinic closures → crisis events (status=closed in resource endpoints)
        if ep_lower in ('hospitals', 'clinics') and records:
            for rec in records:
                if str(rec.get("status", "")).lower() == "closed":
                    crisis_events.append({
                        "type": "closure",
                        "name": str(rec.get("name", "Unknown facility")),
                        "zip": str(rec.get("zip", rec.get("area", ""))),
                        "date": str(rec.get("closureDate", rec.get("closed", ""))),
                        "impact": rec.get("patientsDisplaced", rec.get("impact", 0)),
                        "detail": str(rec.get("closureReason", "")),
                    })

        # Patient displacement → crisis events
        if ep_lower in ('patient-displacement',):
            # Aggregate by hospital
            disp_by_hosp = {}
            for rec in records:
                h = str(rec.get("hospital", "Unknown"))
                disp_by_hosp.setdefault(h, {"patients": 0, "groups": []})
                disp_by_hosp[h]["patients"] += rec.get("patients", 0)
                cg = rec.get("conditionGroup", "")
                if cg and cg not in disp_by_hosp[h]["groups"]:
                    disp_by_hosp[h]["groups"].append(cg)
            for h, info in disp_by_hosp.items():
                # Look up human-readable hospital name from pre-pass
                display_name = hosp_names.get(h, h.replace('hosp-', '').replace('-', ' ').title())
                crisis_events.append({
                    "type": "displacement",
                    "name": display_name,
                    "zip": "",
                    "count": info["patients"],
                    "category": ", ".join(info["groups"][:3]),
                })

        # ER overcrowding → crisis events (high capacity)
        if ep_lower in ('er-visits',):
            # Find months with >90% capacity
            for rec in records:
                cap = rec.get("capacityPct", 0)
                if cap >= 0.9:
                    crisis_events.append({
                        "type": "er-overcrowding",
                        "name": str(rec.get("hospital", "Unknown")),
                        "zip": str(rec.get("area", rec.get("zip", ""))),
                        "count": rec.get("visits", 0),
                        "category": f"{int(cap*100)}% capacity · {rec.get('month', '')}",
                    })

        # 311 calls → crisis events
        if ep_lower in ('311-calls', '311-health'):
            for rec in records:
                z = str(rec.get("zip", rec.get("area", "")))
                total = rec.get("count", rec.get("total", rec.get("calls", 0)))
                if z and total:
                    crisis_events.append({
                        "type": "311",
                        "zip": z,
                        "count": total,
                        "category": str(rec.get("category", rec.get("type", "distress"))),
                    })

        # Only normalize resource-like endpoints (pantries, clinics, shelters, etc.)
        if not _is_resource_endpoint(ep_name, payload):
            continue

        for rec in records:
            norm = _normalize_record(ep_name, rec)
            if norm:
                resources.append(norm)
                # Collect ZIP centroids from lat/lng data
                loc = norm.get("location", {})
                z = loc.get("zip")
                lat = loc.get("lat")
                lng = loc.get("lng")
                if z and lat and lng:
                    all_zips[z] = (lat, lng)

    # Sort resources deterministically
    resources.sort(key=lambda r: (r.get("category", ""), r.get("name", "")))

    # Build filter hints from the normalized resources
    filter_hints = _compute_filter_hints(resources)

    # Infer domain label from resource types
    domain_label = _infer_domain_label(resources, raw_endpoints)

    # ── Auto-detect chart fields from zip_stats ──
    chart_fields = _compute_chart_fields(zip_stats)

    # ── Detect area vs ZIP identifier ──
    area_label = "ZIP"
    if zip_stats:
        sample = zip_stats[0].get("zip", zip_stats[0].get("Zip", ""))
        # If the "zip" value is not numeric, it's likely a neighborhood name
        if sample and not sample.replace("-", "").isdigit():
            area_label = "Area"

    # ── Domain-adaptive text (P0.6, D4: from the domain label, the brief's unit and this data) ──
    domain_hints = _compute_domain_hints(domain_label, resources, raw_endpoints, zip_stats, area_label)

    return {
        "domain_label": domain_label,
        "resources": resources,
        "filter_hints": filter_hints,
        "zip_centroids": all_zips,
        "data_sources": data_sources,
        "zip_stats": zip_stats,
        "crisis_events": crisis_events,
        "chart_fields": chart_fields,
        "area_label": area_label,
        "domain_hints": domain_hints,
        "system_snapshot": system_snapshot,
    }


def _compute_chart_fields(zip_stats: List[Dict]) -> List[Dict[str, str]]:
    """Auto-detect the top 3 numeric fields from zip_stats for bar charts.
    NOTE: values are already pre-converted by _normalize_pct (0-1 fractions → 0-100).
    No ×100 conversion needed in the template."""
    if not zip_stats:
        return []
    skip = {'population', 'area code', 'areacode', 'zip', 'area', 'median age', 'median income', 'life expectancy'}
    priority_kw = ['poverty', 'insecurity', 'uninsured', 'vehicle', 'diabetes', 'obesity', 'hypertension', 'er distance']
    sample = zip_stats[0]
    candidates = []
    for k, v in sample.items():
        if not isinstance(v, (int, float)):
            continue
        if k.lower().replace(' ', '') in {s.replace(' ', '') for s in skip}:
            continue
        vals = [z.get(k, 0) for z in zip_stats if isinstance(z.get(k), (int, float))]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        var = sum((x - mean) ** 2 for x in vals) / len(vals) if len(vals) > 1 else 0
        kl = k.lower().replace(' ', '')
        # Unit detection: rates/percentages get "%", distances get nothing (label has unit)
        is_rate = any(w in kl for w in ['rate', 'insecur', 'poverty', 'uninsured', 'vehicle'])
        is_distance = any(w in kl for w in ['distance', '(mi)', '(km)'])
        if is_distance:
            unit = ""
        elif is_rate:
            unit = "%"
        else:
            unit = ""  # Unknown fields get no unit suffix
        is_priority = any(pw in k.lower() for pw in priority_kw)
        matched_kw = next((pw for pw in priority_kw if pw in k.lower()), None)
        kw_rank = priority_kw.index(matched_kw) if matched_kw else 99
        candidates.append({"key": k, "label": k, "unit": unit, "variance": var, "priority": is_priority, "matched_kw": matched_kw, "kw_rank": kw_rank})
    # Dedup: when multiple fields match the same keyword, keep the shorter (more general) name
    seen_kw = {}
    deduped = []
    for c in candidates:
        kw = c.get("matched_kw")
        if kw and c["priority"]:
            if kw not in seen_kw or len(c["key"]) < len(seen_kw[kw]["key"]):
                seen_kw[kw] = c
        else:
            deduped.append(c)
    deduped.extend(seen_kw.values())
    deduped.sort(key=lambda c: (not c["priority"], c.get("kw_rank", 99), -c["variance"]))
    return [{"key": c["key"], "label": c["label"], "unit": c["unit"]} for c in deduped[:3]]


def _contract_doc():
    """The contract pipeline.py exported (NAIPC_REQUIREMENTS), or None; never raises (P0.6)."""
    if not os.environ.get("NAIPC_REQUIREMENTS", "").strip():
        return None
    try:
        return REG.admit(NR.from_env(), "data", "contract")   # BUILD IC-5 (D1)
    except Exception:  # noqa: BLE001 — an unusable contract leaves the page on its defaults
        return None


def _compute_domain_hints(domain_label: str, resources: List[Dict], raw_endpoints: Dict, zip_stats: Optional[List[Dict]] = None,
                          area_label: str = "ZIP", unit_plural: Optional[str] = None) -> Dict[str, str]:
    """P0.6 (D4): the "Who this serves" sentence, the chat example and the search hint, built from the domain label, the brief's
    unit and this data — a real area, category and language it holds — never an example from another event."""
    dl = (domain_label or "community").strip()
    units = unit_plural or NR.facts(_contract_doc())["unit_plural"]
    cats = Counter(r.get("category") for r in resources if r.get("category"))
    cat = cats.most_common(1)[0][0] if cats else None
    langs = sorted({x for r in resources for k, v in (r.get("attrs") or {}).items() if k.lower() in LANG_KEYS
                    for x in (v if isinstance(v, list) else [v]) if isinstance(x, str)})
    lang = next((x for x in langs if len(x) > 2 and x.lower() != "english"), None)
    area = next((str(z.get("zip")) for z in (zip_stats or []) if z.get("zip")), None)
    n_areas = len(zip_stats or [])
    where = (f" across the {n_areas} {'ZIP codes' if area_label == 'ZIP' else 'areas'} the data ranks by need") if n_areas else ""
    need = dl.lower() if re.search(r"\bservices?$", dl.lower()) else f"{dl.lower()} services"   # P0.6 review F11: not "… services services"
    persona = (f"{units[:1].upper() + units[1:]} looking for {need}, the case workers who refer them, and the planners "
               f"who track where services fall short{where}.")
    if lang:
        chatbot_q = f"Which sites list {lang} among their languages?"
    elif area:
        chatbot_q = f"What is open near {area}?"
    elif cat:
        chatbot_q = f"Which {cat} resources are open?"       # P0.6 (D7): the data's own status, fetched at run time — not "now"
    else:
        chatbot_q = "Which resources are open?"
    kw = ", ".join(x for x in (cat, lang) if x)
    if area:
        search_hint = f"Enter {'ZIP' if area_label == 'ZIP' else 'area'} (e.g. {area})" + (f" or keywords (e.g. {kw})" if kw else " or keywords")
    else:
        search_hint = f"Enter keywords (e.g. {kw})" if kw else "Enter keywords to search"
    eps = set(raw_endpoints or {})
    crisis_noun = "store closures" if "store-closures" in eps else "facility closures" if eps & {"hospitals", "clinics"} else "closures"
    return {"persona": persona, "chatbot_example": chatbot_q, "search_placeholder": search_hint, "crisis_noun": crisis_noun,
            "units": units}     # BUILD B3 (D13, §9): the dashboard's displaced card and hero in the brief's unit


def _normalize_record(ep_name: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a single API record into the canonical resource schema."""
    # --- Infer core fields by key name patterns ---
    name = _pick_first(rec, ['name', 'title', 'facility', 'organization', 'provider', 'store'])
    if not name or not str(name).strip():
        return None  # Drop records without a name

    category = _pick_first(rec, ['type', 'category', 'kind', 'service_type', 'facility_type'])
    category = str(category or ep_name).strip().replace('_', ' ').replace('-', ' ').title()
    # Fix known abbreviations that .title() breaks (Fqhc → FQHC, Er → ER)
    _abbrevs = {'Fqhc': 'FQHC', 'Er': 'ER', 'Ob Gyn': 'OB-GYN', 'Icu': 'ICU'}
    for wrong, right in _abbrevs.items():
        category = category.replace(wrong, right)

    address = str(_pick_first(rec, ['address', 'location', 'street', 'full_address']) or "")
    zip_code = str(_pick_first(rec, ['zip', 'zipCode', 'zip_code', 'postal_code']) or "")
    if not zip_code:
        zip_code = _extract_zip(address) or ""

    lat = _coerce_float(_pick_first(rec, ['lat', 'latitude', 'y']))
    lng = _coerce_float(_pick_first(rec, ['lng', 'lon', 'longitude', 'x']))

    # --- Build attrs: keep useful display fields, skip core/geo fields ---
    core_keys = {'id', 'name', 'title', 'facility', 'organization', 'provider', 'store',
                 'type', 'category', 'kind', 'service_type', 'facility_type',
                 'address', 'location', 'street', 'full_address',
                 'zip', 'zipCode', 'zip_code', 'postal_code',
                 'lat', 'latitude', 'y', 'lng', 'lon', 'longitude', 'x',
                 'source', 'count'}
    attrs = {}
    for k, v in rec.items():
        if k.lower() in {ck.lower() for ck in core_keys}:
            continue
        if isinstance(v, (str, bool, int, float)):
            attrs[_pretty_label(k)] = _pretty_value(v)
        elif isinstance(v, list) and len(v) <= 15 and all(isinstance(x, (str, int, float, bool)) for x in v):
            attrs[_pretty_label(k)] = _pretty_value(v)

    # --- Build tags for search ---
    tags = [ep_name.lower(), category.lower()]
    for k, v in attrs.items():
        if isinstance(v, str) and len(v) < 50:
            tags.append(v.lower())
        elif isinstance(v, list):
            tags.extend(str(x).lower() for x in v if isinstance(x, str) and len(x) < 50)

    # Precompute search text (lowercased concatenation of all searchable content)
    search_parts = [str(name).lower(), category.lower(), address.lower(), zip_code]
    search_parts.extend(tags)
    for k, v in attrs.items():
        if isinstance(v, str):
            search_parts.append(v.lower())
        elif isinstance(v, list):
            search_parts.extend(str(x).lower() for x in v)
    search_text = " ".join(search_parts)

    rid = rec.get("id") or _safe_id(f"{ep_name}|{name}|{address}|{zip_code}")
    # P0.7 (D6): the record's own status — a closed status closes the resource, named as the data's; "at risk" stays open, tagged
    status, closed_by, closed_reason, at_risk = record_status(rec)
    if at_risk:
        tags.append("at risk")
        search_text += " at risk"

    out = {
        "id": str(rid),
        "name": str(name).strip(),
        "category": category,
        "location": {
            "address": address or None,
            "zip": zip_code or None,
            "lat": lat,
            "lng": lng,
        },
        "tags": list(dict.fromkeys(tags)),       # B2: once each, in the order built (a set's order changed with the hash seed)
        "attrs": attrs,
        "search_text": search_text,
        "source_endpoint": ep_name,
        "status": status,  # P0.7 (D6): the data's own status; the announcement's matcher can still close an open resource
    }
    if closed_by:
        out["closed_by"], out["closed_reason"] = closed_by, closed_reason
    if at_risk:
        out["at_risk"] = True
    return out


def _pick_first(rec: Dict[str, Any], candidates: List[str]) -> Any:
    """Return the value of the first matching key (case-insensitive).
    Tries exact match first, then substring fallback for camelCase compound keys
    (e.g. 'provider' matches 'providerName', 'name' matches 'programName')."""
    rec_lower = {k.lower(): v for k, v in rec.items()}
    # Pass 1: exact match
    for c in candidates:
        v = rec_lower.get(c.lower())
        if v is not None:
            return v
    # Pass 2: substring fallback — candidate is contained within a key
    for c in candidates:
        cl = c.lower()
        for k, v in rec_lower.items():
            if cl in k and v is not None:
                return v
    return None


def _compute_filter_hints(resources: List[Dict]) -> Dict[str, Any]:
    """Compute dynamic filter configuration from the normalized resources."""
    categories = sorted(set(r.get("category", "") for r in resources if r.get("category")))

    # Find boolean attrs (fields that are True/False across resources)
    bool_fields = set()
    # Find enum attrs (fields with a small set of distinct string values)
    enum_fields = {}  # label -> set of values

    for r in resources:
        for k, v in r.get("attrs", {}).items():
            if isinstance(v, bool):
                bool_fields.add(k)
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                if k not in enum_fields:
                    enum_fields[k] = set()
                enum_fields[k].update(v)
            elif isinstance(v, str) and len(v) < 30:
                # Could be an enum if there are few distinct values
                if k not in enum_fields:
                    enum_fields[k] = set()
                enum_fields[k].add(v)

    # Only keep enum fields with 2-8 distinct values AND fewer values than 60% of resources
    # (fields with nearly-unique values like Hours/Phone are display, not filter)
    enum_filters = {}
    n_resources = max(len(resources), 1)
    skip_filter_labels = {'hours', 'phone', 'serves', 'monthly capacity', 'id',
                          'address', 'open hours', 'capacity per day', 'wait weeks'}
    for k, vals in enum_fields.items():
        if k.lower() in skip_filter_labels:
            continue
        if 2 <= len(vals) <= 8 and len(vals) < n_resources * 0.6:
            enum_filters[k] = sorted(vals)

    return {
        "categories": categories,
        "bool_filters": sorted(bool_fields),
        "enum_filters": enum_filters,
    }


def _infer_domain_label(resources: List[Dict], raw_endpoints: Dict) -> str:
    """Infer the civic domain from endpoint names and resource categories."""
    ep_names = set(raw_endpoints.keys())
    cats = set(r.get("category", "").lower() for r in resources)

    # Food access signals
    food_signals = {'pantries', 'food-atlas', 'harvest', 'store-closures', 'food_bank', 'pantry'}
    if ep_names & food_signals or cats & food_signals:
        return "Food Access"

    # Healthcare signals
    health_signals = {'hospitals', 'clinics', 'er-visits', 'patient-displacement', '311-health'}
    if ep_names & health_signals or cats & health_signals:
        return "Healthcare"

    # Workforce signals
    work_signals = {'jobs', 'training', 'workforce', 'employers', 'careers'}
    if ep_names & work_signals or cats & work_signals:
        return "Workforce"

    # Housing signals
    housing_signals = {'shelters', 'housing', 'apartments', 'evictions'}
    if ep_names & housing_signals or cats & housing_signals:
        return "Housing"

    # Fallback: use the most common category — B2 (second live R4): a tie goes to the category met first among the resources, not to Python's
    # string-hash order (Wichita's 10 shelters and 10 homes titled the page "Shelter Inventory" or "Rehousing Options" by the seed)
    if cats:
        order = list(dict.fromkeys(r.get("category", "").lower() for r in resources))
        return max(order, key=lambda c: sum(1 for r in resources if r.get("category", "").lower() == c)).title()
    return "Community Services"  # P0.6 (D3): the neutral fallback


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Curveball Application
# ═══════════════════════════════════════════════════════════════════════════════

# BUILD B2 (D7): _item_lines and apply_curveball are the engine's (imported at the top of the alerts section).


def announcement_closure_word(curveball_text: str) -> bool:
    """D7: the announcement holds one of the matcher's closure words (then a closure that matches no record is said so)."""
    t = (curveball_text or "").lower()
    return any(kw in t for kw in CLOSURE_WORDS)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4: Build Template Context and Render
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
    file in that folder first moves to <harness>/chat-context-archive/<UTC stamp>/ (outside output\\, never uploaded; nothing deleted);
    the count is printed only when something moved."""
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


# ── Port of the April browser prompt builder (architect_template.html.j2 1277–1301, April copy) with JavaScript
#    string semantics; T-ctx runs the April JS under Node on the same app_data and requires identical strings. ──
_UNDEF = object()


def _jget(d, k):
    return d.get(k, _UNDEF) if isinstance(d, dict) else _UNDEF


def _js_truthy(v) -> bool:
    if v is _UNDEF or v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, (int, float)):
        return not (v == 0 or v != v)
    if isinstance(v, str):
        return v != ""
    return True


def _js_str(v) -> str:
    """JavaScript String(v) for the JSON value types app_data carries."""
    if v is _UNDEF:
        return "undefined"
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        if v == int(v) and abs(v) < 1e21:
            return str(int(v))
        r = repr(v)
        if "e" in r:
            m, e = r.split("e")
            return f"{m}e{'+' if int(e) > 0 else '-'}{abs(int(e))}"
        return r
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return ",".join("" if x is None or x is _UNDEF else _js_str(x) for x in v)
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


def _js_entries(d: dict) -> list:
    """Object.entries order: array-index keys ascending first, then string keys in insertion order."""
    def is_index(k):
        return isinstance(k, str) and k.isdigit() and (k == "0" or not k.startswith("0")) and int(k) < 4294967295
    idx = sorted((k for k in d if is_index(k)), key=int)
    return [(k, d[k]) for k in idx] + [(k, v) for k, v in d.items() if not is_index(k)]


def build_architect_chat_context(app_data: dict) -> str:
    res = app_data.get("resources") or []
    zs = app_data.get("zip_stats") or []
    ce = app_data.get("crisis_events") or []
    area = _js_str(app_data.get("area_label") or "ZIP")
    hints = app_data.get("domain_hints") or {}

    def attr(v):
        return "/".join("" if x is None else _js_str(x) for x in v) if isinstance(v, list) else _js_str(v)
    rl = []
    for r in res:
        loc = _jget(r, "location")
        z = _jget(loc, "zip") if isinstance(loc, dict) else _UNDEF
        st = _jget(r, "status")
        attrs = _jget(r, "attrs")
        items = _js_entries(attrs)[:6] if isinstance(attrs, dict) else []
        tail = "".join(f", {k}: {attr(v)}" for k, v in items)
        rl.append(f"• {_js_str(_jget(r, 'name'))} ({_js_str(_jget(r, 'category'))}, {area} {_js_str(z) if _js_truthy(z) else '?'}, "
                  f"status: {_js_str(st) if _js_truthy(st) else 'open'}{tail})")
    zl = []
    for zrow in zs:
        zz = _jget(zrow, "zip")
        zv = zz if _js_truthy(zz) else _jget(zrow, "Zip")
        nums = [(k, v) for k, v in (_js_entries(zrow) if isinstance(zrow, dict) else [])
                if k not in ("zip", "Zip") and isinstance(v, (int, float)) and not isinstance(v, bool)]
        zl.append(f"• {area} {_js_str(zv)}: " + ", ".join(f"{k}: {_js_str(v)}" for k, v in nums))
    cl = []
    for e in ce:
        nm, zp, imp, cnt = _jget(e, "name"), _jget(e, "zip"), _jget(e, "impact"), _jget(e, "count")
        tail = (_js_str(imp) + " displaced") if _js_truthy(imp) else ((_js_str(cnt) + " affected") if _js_truthy(cnt) else "")
        cl.append(f"• {_js_str(_jget(e, 'type'))}: {_js_str(nm) if _js_truthy(nm) else ''} "
                  f"{(area + ' ' + _js_str(zp)) if _js_truthy(zp) else ''} {tail}")
    persona = _jget(hints, "persona")
    cat = _jget(res[0], "category") if res else _UNDEF
    intro = (f"You are Atlas AI, the embedded assistant for a {'civic operations' if _js_truthy(persona) else 'resource navigation'} "
             f"dashboard analyzing {_js_str(cat) if _js_truthy(cat) else 'civic'} data.")
    crisis = "\n".join(cl)
    analysis = (f"<analysis_context>\nRESOURCES ({len(res)} total):\n" + "\n".join(rl) + "\n\n"
                f"{area.upper()} DEMOGRAPHICS ({len(zs)} areas):\n" + "\n".join(zl) + "\n\n"
                f"CRISIS EVENTS ({len(ce)} total):\n{crisis or 'None detected.'}\n</analysis_context>")
    return intro + "\n\n" + analysis


def architect_chat_text(app_data: dict, changes: Optional[Dict] = None, curveball_text: str = "", cb_block: Optional[Dict] = None) -> str:
    """The chat context: the April port above (unchanged), then P0.7's blocks outside it — every alert with its id, title and detail
    (D5: the port prints a crisis event's name only), every closed resource with who closed it (D6), and in Block 2 the CURVEBALL
    block (D7: the announcement, the change summary, the alerts added and removed, the closures, the no-match line)."""
    text = build_architect_chat_context(app_data)
    alerts = [e for e in app_data.get("crisis_events") or [] if e.get("type") == "alert"]
    if alerts:
        text += "\n\nALERTS (id — title — detail; from the data):\n" + "\n".join(
            f"• {e.get('id') or '(no id)'} — {e.get('name') or '(no title)'} — {e.get('detail') or '(no detail)'}"
            + (f" [{e['severity']}]" if e.get("severity") else "") + (f" ({', '.join(e['places'])})" if e.get("places") else "") for e in alerts)
    closed = [r for r in app_data.get("resources") or [] if r.get("status") == "closed"]
    if closed:
        text += "\n\nCLOSED RESOURCES (who closed them):\n" + "\n".join(
            f"• {r.get('name')} ({r.get('source_endpoint')}) — " + ("closed per the data's own status" if r.get("closed_by") == "data" else "closed by the curveball announcement")
            for r in closed)
    if curveball_text:
        extra = []
        if cb_block:
            extra += [f"Closed by the announcement: {name}" for name in cb_block.get("closures") or []]
            if cb_block.get("no_match"):
                extra.append("The announcement describes a closure that matches no record in the data.")
        text += "\n\n" + NR.changes_chat_block(changes or {"curveball": {"text": curveball_text}, "summary": []}, extra=extra)
    return text


def build_architect_faq(ctx: dict) -> list:
    """Static Q&As from the page's own numbers (no model call)."""
    n, o = ctx.get("resource_count", 0), ctx.get("open_count", 0)
    faq = [{"q": "What does this tool show?", "a": f"{n} resources ({o} open) across {len(ctx.get('zip_stats') or [])} areas, from the challenge's data endpoints."},
           {"q": "How do I find help near me?", "a": "Type a ZIP code or a keyword in the search box to filter the list, then select a card for its details."}]
    if n - o > 0:
        faq.append({"q": "Are any resources closed?", "a": f"{n - o} of {n} are marked closed."})
    if ctx.get("data_inventory"):
        faq.append({"q": "Where does the data come from?", "a": f"{ctx['data_inventory']}."})
    return faq


SNAPSHOT_EP = "community-snapshot"   # the snapshot mode's endpoint (normalize_resources reads it by name; P1 generalises the names)


def change_view(changes: Optional[Dict], resources: List[Dict], curveball_text: str, normalized: Dict[str, Any],
                cb_record: Optional[Dict] = None):
    """P0.7 (D7, D4), Block 2 only: (the change block for the template or None, {(endpoint, record id)} of the records added or
    changed, the snapshot's "as of Block 1" label or "", the derived tiles). The block: the announcement and its first sentence, per
    changed endpoint the records added, removed and changed (their ids), the closures the announcement applied (by name), the
    no-match line when the announcement holds a closure word and matched no record, the unchanged endpoints, the alerts added and
    removed. The label: when the change list marks the snapshot's endpoint unchanged. The tiles: each derived figure of the Oracle's
    curveball.json (this run's) compared with one of the snapshot's own fields — shown BESIDE the aggregate, never as its update."""
    if not curveball_text:
        return None, set(), "", []
    eps = (changes or {}).get("endpoints")
    rows, changed = [], set()
    for ep, e in (eps or {}).items():
        st = e.get("status")
        if st == "changed":
            refs = {k: [str(r.get("ref")) for r in e.get(k) or []] for k in ("added", "removed", "changed")}
            env = "; ".join(f"{k} {NR.fmt_value(a)} → {NR.fmt_value(b)}" for k, (a, b) in (e.get("envelope") or {}).items())
            rows.append(dict(refs, endpoint=ep, status=st, envelope=env))
            changed |= {(ep, x) for x in refs["added"] + refs["changed"]}
        elif st in ("new endpoint", "unreadable"):
            rows.append({"endpoint": ep, "status": st, "added": [], "removed": [], "changed": [], "envelope": e.get("reason") or ""})
    closures = [f"{r['name']} ({r.get('source_endpoint')})" for r in resources if r.get("closed_by") == "announcement"]   # two records may share a name
    block = {"announcement": curveball_text, "first": NR.first_sentence(curveball_text), "rows": rows, "closures": closures,
             "no_match": announcement_closure_word(curveball_text) and not closures,
             "unchanged": [ep for ep, e in eps.items() if str(e.get("status", "")).startswith("unchanged")] if eps is not None else None,
             "snapshot": ((changes or {}).get("snapshot") or {}).get("source") or "",
             "alerts": [{"kind": k, "id": i, "title": t, "endpoint": ep} for ep, k, i, t in NR.alert_changes(changes)] if changes else []}
    snap = normalized.get("system_snapshot") or {}
    se = (eps or {}).get(SNAPSHOT_EP) or {}
    asof = ("as of Block 1 — this endpoint did not change at the curveball"
            if snap and str(se.get("status", "")).startswith("unchanged") else "")
    tiles = []
    for d in (cb_record or {}).get("derived") or []:
        c = (d.get("compare") or {}) if isinstance(d, dict) else {}
        if c.get("endpoint") == SNAPSHOT_EP and c.get("field") in snap:
            tiles.append({"label": str(d.get("label") or ""), "value": d.get("block2"), "base": d.get("base2"), "block1": d.get("block1"),
                          "base1": d.get("base1"), "records": d.get("records"), "n": d.get("n_records"), "field": d.get("field"),
                          "minus": d.get("minus_field"), "endpoint": d.get("endpoint"), "compare_field": c.get("field")})
    return block, changed, asof, tiles


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD B2: the Get Help tab (D1–D6, D9) — the engine's bundle for the browser, the static parts, the build-time rationale
# ═══════════════════════════════════════════════════════════════════════════════
ENGINE_JS = "naipc_engine.js"                 # D1: the browser half of the engine, inlined like Chart.js
JSON_LT = chr(92) + "u003c"                   # '<' as a JSON escape: embedded JSON then holds no '<' at all (B2 review: '<!--<script')
RATIONALE_DEADLINE_S = 150                    # the call's whole time (the pipeline gives the Architect 300 s)
RATIONALE_MODEL = "claude-opus-5-5"         # D6: the one build-time call (T1′'s third planned constant)
# BUILD IC-6 (D1): the explanation's limit, effort and stream mode are its profile's, "architect/rationale" (4,000 at low, streamed)
RATIONALE_TOP = 3                           # the options per set the explanation is written for (the plan's top three)
# BUILD IC-4 (D12): the explanation's reply as a list under a structured output (switch on) — additionalProperties false at every level,
# every property required, no recursion, no enum or key from the data, no constraint; the property is "explanation" (never reasoning,
# thinking or trace: the vendor's refusals page lists those names as a reasoning_extraction trigger)
RATIONALE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
                    "properties": {"items": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False, "required": ["preset", "set", "explanation"],
                        "properties": {"preset": {"type": "string"}, "set": {"type": "string"}, "explanation": {"type": "string"}}}}}}
RATIONALE_LIST_FORM = ("Reply with JSON only: {\"items\": [{\"preset\": \"<preset id>\", \"set\": \"<set id>\", \"explanation\": \"<two or "
                       "three sentences>\"}]}, one item for each set you explain.")
# BUILD IC-5 (D16): the navigator names its options by id — with this build's switch on (NAIPC_IC5_PROMPT) each item also holds
# options_named, the ids of the options its explanation names; Python checks each against that plan before the words are read
# (check_named), and D5 (a)'s name check (check_rationale) stays the backstop for the words. IC-4's schema with that one property: a list of
# plain strings — no enum, key or example from the data (the ids are the plan's, given in the request; Python checks them) — every
# property required, additionalProperties false at every level. With the switch off, RATIONALE_SCHEMA and IC-4's words, byte for byte.
RATIONALE_SCHEMA_IC5 = {"type": "object", "additionalProperties": False, "required": ["items"],
                        "properties": {"items": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False, "required": ["preset", "set", "explanation", "options_named"],
                            "properties": {"preset": {"type": "string"}, "set": {"type": "string"}, "explanation": {"type": "string"},
                                           "options_named": {"type": "array", "items": {"type": "string"}}}}}}}
RATIONALE_NAMED_FORM = (" Each item also holds options_named: the id of each option its explanation names, as written before the option's "
                        "name above ([] when it names none).")
# BUILD IC-8 (D6), under the switch (NR.ic8_prompt): the listing names each option first and its id after it ("#<k> <name> — id <id>:"),
# options_named says so, and one line asks for names in the explanation; with the switch off, IC-7's words byte for byte
RATIONALE_NAMED_FORM_IC8 = RATIONALE_NAMED_FORM.replace("as written before the option's name above", "as written after the option's name above")
RATIONALE_NAME_LINE = " Name each option by its name in explanation; its id goes only in options_named."
_NUM = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")
# BUILD IC-7 (D1, I1): this harness's one call and its contract — the explanation's reply under its structured output (IC-4, IC-5), its
# items checked by rationale_items, check_named and check_rationale; an item that fails is not shown and is named; the page renders
# without an explanation (the engine's order stands), so nothing in this reply is essential
CONTRACTS = {"architect/rationale": {
    "form": "schema", "page": "each preset's set → its explanation", "wire": lambda: (RATIONALE_SCHEMA_IC5 if NR.ic5_prompt() else RATIONALE_SCHEMA),
    "rules": "rationale_items, check_named, check_rationale: an item that fails is left out and named", "essential": "none",
    "view": lambda: {"call": "architect/rationale", "schema": "sent", "number_paths": [], "text_paths": ["items[].preset", "items[].set",
                     "items[].explanation"], "count_lists": [], "name_fields": [], "lists": {"items": (0, None)}, "typed_words": "fields"}}}


def _shown(v) -> str:
    """A record value as the page prints it: text as it is, a list joined, a number as the engine words it."""
    return ENG._show(v) if not isinstance(v, str) else v


def help_view(raw_endpoints: Dict[str, Any], curveball_text: str, doc, reg=None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """(the Get Help view or None, console lines). None when the challenge has no engine sidecar with an intake (KC today: the page
    renders as before) — the lines say why. Never raises.
    BUILD IC-4 (D3): with the Oracle's registry of this run (reg), each figure the lists print is its span (the "_h" forms: REG.span_if):
    a document's processing days and fee, a program's age window (its record's words), intake lag and free places (Python's comparison
    of the record's two fields, registered here: calc:diff), a home's facts, and the lists' record counts."""
    lines: List[str] = []
    try:
        cpath = os.environ.get("NAIPC_REQUIREMENTS", "").strip()
        if not cpath or doc is None:
            return None, ["Get Help tab: not built — no contract (NAIPC_REQUIREMENTS not set or not usable)"]
        spec, why = REG.admit(ENG.load_spec(cpath), "data", "engine sidecar")   # BUILD IC-5 (D1)
        if spec is None:
            return None, [f"Get Help tab: not built — {why}; the page keeps its three tabs"]
        base = block1_base(doc) if (curveball_text or "").strip() else None
        bundle = ENG.for_page(spec, doc, raw_endpoints or {}, curveball_text or "", page="architect", base=base)
        lines += [f"  engine: {p}" for p in bundle.get("problems") or []]
        if bundle.get("status") != "ok":
            why = {"no intake": "the engine sidecar has no intake section"}.get(bundle.get("status"), "the engine sidecar did not validate")
            return None, lines + [f"Get Help tab: not built — {why}; the page keeps its three tabs"]
        try:
            js = (HARNESS_DIR / ENGINE_JS).read_text(encoding="utf-8")
        except OSError as e:
            return None, lines + [f"Get Help tab: not built — {ENGINE_JS} not readable ({type(e).__name__})"]
        if "</" in js:
            return None, lines + [f"Get Help tab: not built — {ENGINE_JS} holds '</', which would end its script block"]
        when, src, problem = ENG.as_of()
        if problem:
            lines.append(f"  engine: {problem}")
        # BUILD IC-5 (D6d): the page's data holds what the page reads — the source (NAIPC_AS_OF, a variable's name) stays in the console
        bundle["clock"] = {"fixed": when.strftime("%Y-%m-%dT%H:%M") if src == "NAIPC_AS_OF" else None}
        rat, why = rationale(spec, doc, raw_endpoints or {}, curveball_text or "", bundle, base)
        bundle["rationale"] = REG.out_tree(rat, "plain")     # BUILD IC-5 (D2 c): the navigator's data — the explanations' tags in words
        lines.append(f"  rationale: {why}")
        names = {s["id"]: {r["id"]: r["name"] for r in s["records"]} for s in bundle["sets"]}
        view = {"bundle": bundle, "bundle_json": json.dumps(bundle, ensure_ascii=False, allow_nan=False).replace("<", JSON_LT),
                "engine_js": js, "set_labels": {s["id"]: s["label"] for s in bundle["sets"]}, "names": names,
                "set_endpoints": {s["id"]: s["table"]["endpoint"] for s in bundle["sets"]}}
        ck = bundle.get("checklist")
        if ck:
            dn = {i["id"]: i["name"] for i in ck["items"]}
            usd = str(ck["fields"].get("fee") if isinstance(ck.get("fields"), dict) else "").lower().endswith("usd")

            def _days(v):
                return f"{_shown(v)} day{'' if v == 1 else 's'} to process" if ENG._num(v) else "processing time not given"

            def _fee(v):
                if not ENG._num(v):
                    return ""
                return "none" if v == 0 else (f"${_shown(v)}" if usd else _shown(v))
            cf, cep = ck.get("fields") or {}, ck.get("endpoint")

            def _dh(i, v, k, text):      # BUILD IC-4 (D3): the record's field as its span, when the registry holds it shown so
                return REG.span_if(reg, f"{cep}.{i['id']}.{cf.get(k)}", text) if cf.get(k) else _html.escape(text)
            view["docs"] = [dict(i, needs_names=[dn.get(x, x) for x in i["needs"]], opens_names=[dn.get(x, x) for x in i["opens"]],
                                 days_s=_days(i.get("days")), fee_s=_fee(i.get("fee")),
                                 days_h=(_dh(i, i.get("days"), "days", _shown(i["days"])) + REG.esc(_days(i["days"])[len(_shown(i["days"])):])
                                         if ENG._num(i.get("days")) else _html.escape(_days(i.get("days")))),
                                 fee_h=(_dh(i, i.get("fee"), "fee", _fee(i["fee"])) if ENG._num(i.get("fee")) and i["fee"] != 0 else REG.esc(_fee(i.get("fee")))),
                                 unlocks_s=", ".join(_shown(x) for x in i["unlocks"]) if isinstance(i.get("unlocks"), list) else _shown(i.get("unlocks") or ""),
                                 blockers_l=[_shown(x) for x in i["blockers"]] if isinstance(i.get("blockers"), list) else ([i["blockers"]] if i.get("blockers") else []),
                                 help_l=[_shown(x) for x in i["help"]] if isinstance(i.get("help"), list) else ([i["help"]] if i.get("help") else []))
                            for i in ck["items"]]
            view["doc_names"] = dn
            view["docs_n_h"] = REG.span_if(reg, f"{cep}.count", str(len(view["docs"])))     # BUILD IC-4 (D3)
            for d in view["docs"]:          # BUILD IC-4 (D3, D1): the record's words, each quantity its mention's span
                q_ = (lambda k, s, d=d: reg.quote_html(f"{cep}.{d['id']}.{cf.get(k)}", s) if reg is not None and cf.get(k) else REG.esc(s))
                d["blockers_h"] = NR.join_clauses(q_("blockers", x) for x in d["blockers_l"])      # IC-4 (D8): one stop at a join
                d["help_h"] = NR.join_clauses(q_("help", x) for x in d["help_l"])
                d["notes_h"] = q_("notes", str(d.get("notes") or "")) if d.get("notes") else ""
            shut = [(c["name"], c.get("why") or "", _frags(c["name"])) for c in bundle.get("closures") or []]
            for d in view["docs"]:
                txt = _flat(" ".join(d["help_l"] + d["blockers_l"] + [str(d.get("notes") or "")]))
                d["closed"] = sorted({f"{nm} (closed by {why})" if why else nm for nm, why, fr in shut if any(_has_frag(txt, f) for f in fr)})
        nv = bundle.get("navigator")
        if nv:
            ps = next(s for s in bundle["sets"] if s["id"] == nv["set"])
            unit = next((u for k, u in (("week", "weeks"), ("day", "days"), ("month", "months")) if k in str(nv["lag_field"]).lower()), "")
            view["programs"] = [{"id": r["id"], "name": r["name"], "window": _shown(r.get("window")),
                                 "lag_s": (f"{_shown(r['lag'])} {unit[:-1] if r.get('lag') == 1 and unit else unit}".strip() if r.get("lag") is not None else "not given"),
                                 "free_s": (r["free_note"] if r.get("free_note") else f"{_shown(r['free'])} free" if r.get("free") is not None
                                            else "free places not given"),                # B2: a declared placeholder says what it is
                                 "check": next((x["check"] for x in ps["records"] if x["id"] == r["id"]), None)} for r in nv["rows"]]
            nep, fr = view["set_endpoints"].get(nv["set"]), list(nv.get("free_fields") or [])
            for p_, r in zip(view["programs"], nv["rows"]):         # BUILD IC-4 (D3): each figure as its span (the one registry)
                m_ = reg.mention_at(f"{nep}.{r['id']}.{nv['window_field']}", p_["window"]) if reg is not None and r.get("window") is not None else None
                p_["window_h"] = reg.span(m_) if m_ else REG.esc(p_["window"])
                lag = REG.span_if(reg, f"{nep}.{r['id']}.{nv['lag_field']}", _shown(r["lag"])) if r.get("lag") is not None else ""
                p_["lag_h"] = lag + REG.esc(p_["lag_s"][len(_shown(r["lag"])):]) if lag else REG.esc(p_["lag_s"])
                free = None
                if reg is not None and r.get("free") is not None and not r.get("free_note") and len(fr) == 2:
                    e_, _w = reg.lookup(f"calc:diff|{nep}.{r['id']}.{fr[0]}|{nep}.{r['id']}.{fr[1]}")
                    free = e_ if e_ and e_.get("value") == r["free"] and e_.get("shown") == _shown(r["free"]) else None
                p_["free_h"] = reg.span(free) + " free" if free else REG.esc(p_["free_s"])
                if r.get("free_note") and reg is not None and fr:      # "no cap (slotsAvailable 999 in the data)": the field's value its span
                    v_ = _shown((next((x for x in NR.records((raw_endpoints or {}).get(nep)) if isinstance(x, dict)
                                       and str(x.get("id")) == str(r["id"])), None) or {}).get(fr[0]))
                    sp_ = REG.span_if(reg, f"{nep}.{r['id']}.{fr[0]}", v_)
                    fw_ = ENG.field_text(fr[0])          # BUILD IC-8 (D12): the note names the field in words
                    if v_ and sp_ != _html.escape(v_) and f"{fw_} {v_} " in r["free_note"]:
                        p_["free_h"] = REG.esc(r["free_note"]).replace(REG.esc(f"{fw_} {v_} "), f"{REG.esc(fw_)} {sp_} ", 1)
                c_ = r.get("closed")
                if c_:      # IC-4 Phase 7 (review c1, D3): a closed or vetoed program is never offered as free — both assertions print
                    said = f"closed by {c_.get('why') or 'the data'}" + (f": \u201c{c_['segment']}\u201d" if c_.get("segment") else "")
                    p_["free_s"] = f"{said} \u2014 the data still reads {p_['free_s']}"
                    p_["free_h"] = f'<strong class="gh-closed">{REG.esc(said)}</strong> \u2014 the data still reads {p_["free_h"]}'
            view["progs_n_h"] = REG.span_if(reg, f"{nep}.count", str(len(view["programs"])))
        rd = bundle.get("readiness")
        if rd:
            hs = next(s for s in bundle["sets"] if s["id"] == rd["set"])
            shows = {r["id"]: r["show"] for r in hs["records"]}
            view["homes"] = [{"id": r["id"], "name": r["name"],
                              "barriers": [_shown(x) for x in r["barriers"]] if isinstance(r["barriers"], list) else ([_shown(r["barriers"])] if r["barriers"] else []),
                              "facts": [(f, _shown(v)) for f, v in (shows.get(r["id"]) or {}).items() if f != rd["barriers_field"]]} for r in rd["rows"]]
            hep = view["set_endpoints"].get(rd["set"])
            for hh in view["homes"]:          # BUILD IC-4 (D3): a home's facts as their spans (the record's fields), its screening words linked
                hh["facts_h"] = [REG.span_if(reg, f"{hep}.{hh['id']}.{f}", v) for f, v in hh["facts"]]
                hh["barriers_h"] = NR.join_clauses(reg.quote_html(f"{hep}.{hh['id']}.{rd['barriers_field']}", x) if reg is not None else REG.esc(x)
                                             for x in hh["barriers"])
            view["homes_n_h"] = REG.span_if(reg, f"{hep}.count", str(len(view["homes"])))
        n_sec = 2 + sum(1 for k in ("checklist", "navigator", "readiness") if bundle.get(k))
        lines.insert(0, f"Get Help tab: {len(bundle['intake']['steps'])} steps, {len(bundle['presets'])} presets, {len(bundle['sets'])} option sets "
                        f"({', '.join(s['id'] for s in bundle['sets'])}), {n_sec} sections; open now read at "
                        + (f"{bundle['clock']['fixed']} (NAIPC_AS_OF)" if bundle["clock"]["fixed"] else "the viewer's clock"))
        return view, lines
    except Exception as e:  # noqa: BLE001 — a page is never lost to the Get Help tab
        return None, lines + [f"Get Help tab: not built — {type(e).__name__}: {e}"]


def _plan_input(res, bundle):
    """The rationale's input (D6): per preset, per set of the plan, the top options with their points and the counts — the trace's
    numbers only. Returns (text, {preset: {set: {"top": [ids], "numbers": set, "ids": set}}})."""
    plan_sets = [sid for part in bundle["intake"]["plan"] for sid in part.get("sets") or []]
    allowed, out = {}, []
    for p in res["presets"]:
        out.append(f"PRESET {p['id']} — {p['name']}: {p['who']}")
        allowed[p["id"]] = {}
        for c in p["cases"]:
            if c["set"] not in plan_sets:
                continue
            lab = next((s["label"] for s in bundle["sets"] if s["id"] == c["set"]), c["set"])
            k = c["counts"]
            txt = [f"  SET {c['set']} ({lab}): {k['records']} records — {k['eligible']} eligible, {k['full']} full, {k['closed']} closed or not "
                   f"open now, {k['other']} not for this person, {k['confirm']} to confirm"
                   + ("" if c["ranked"] else " — nothing is ranked: no sentences for this set (a note is shown only beside a ranked option)")]
            for i, x in enumerate(c["ranked"][:RATIONALE_TOP], 1):     # B2 (second live R4): each bracket names what the record holds
                txt.append((f"    #{i} {x['name']} — id {x['id']}: " if NR.ic8_prompt() else f"    #{i} {x['id']} {x['name']}: ")   # IC-8 (D6)
                           + f"{x['score']} points = "
                           + " + ".join(f"{q['label']} {q['points']} ({q['reads']}: {q['value']})" for q in x["components"]))
            for x in c["confirm"][:RATIONALE_TOP]:
                txt.append(f"    to confirm: {x['id']} {x['name']} — {x['detail']}")
            out += txt
            # B2 (review): the figures a sentence may use — the counts, scores, points and values given, the confirm details' own figures and
            # the preset's numbers; never the digits of an id or a rank label
            nums = {float(v) for v in k.values() if ENG._num(v)}
            for x in c["ranked"][:RATIONALE_TOP]:
                nums.add(float(x["score"]))
                for q in x["components"]:
                    nums |= {float(q["points"])} | {float(m.replace(",", "")) for m in _NUM.findall(str(q["value"]))}
            for x in c["confirm"][:RATIONALE_TOP]:
                nums |= {float(m.replace(",", "")) for m in _NUM.findall(str(x["detail"]))}
            nums |= {float(a["value"]) for pr in [next((q for q in bundle["presets"] if q["id"] == p["id"]), {})]
                     for a in (pr.get("person") or {}).values() if isinstance(a, dict) and ENG._num(a.get("value"))}
            allowed[p["id"]][c["set"]] = {"top": [x["id"] for x in c["ranked"][:RATIONALE_TOP]], "counts": dict(k), "numbers": nums,
                                          "ranked": [{"id": x["id"], "name": x["name"], "score": x["score"],          # BUILD IC-8 (D6):
                                                      "components": [{"label": q["label"], "points": q["points"]}      # the engine's
                                                                     for q in x["components"]]} for x in c["ranked"][:RATIONALE_TOP]],   # sentence
                                          "ids": {x["id"] for x in c["ranked"][:RATIONALE_TOP] + c["confirm"][:RATIONALE_TOP]}}
    return "\n".join(out), allowed


RATIONALE_SYSTEM = ("You explain a ranking that a fixed engine already computed. You never change the order and never add an option, a "
                    "number or a fact. Write plain words for the person and their case worker.")


_STOP = {"a", "an", "the", "of", "and", "for", "to", "in", "at", "by", "on", "with", "or", "/", "-", "&", "—", "–"}
_DIGITS = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")                  # every number, glued to letters or not ("9pm", "3rd")
# B2 (§9 → fixed): a number written as a word is read like a digit — two to ninety-nine ("one" alone is left out: "one of", "the one")
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_WORDNUM = re.compile(r"(?<![\w-])(?:(" + "|".join(_TENS) + r")(?:[- ](" + "|".join(_UNITS) + r"))?|("
                      + "|".join([k for k in _UNITS if k != "one"] + list(_TEENS)) + r"))(?![\w-])", re.I)


def _word_numbers(text):
    """[(the words, their value)] for every number written as a word in text."""
    out = []
    for m in _WORDNUM.finditer(text):
        if m.group(1):
            out.append((m.group(0), _TENS[m.group(1).lower()] + (_UNITS[m.group(2).lower()] if m.group(2) else 0)))
        else:
            out.append((m.group(0), {**_UNITS, **_TEENS}[m.group(3).lower()]))
    return out


_NAME_TOK = re.compile(r"[A-Za-z0-9]+(?:[.'’&-][A-Za-z0-9]+)*")
_SHAPE = re.compile(r"\d|[A-Z][^A-Z]*[A-Z]")      # a digit, or two capitals or more: an acronym or a coined name (211, ETV, SOUL, EmberHope)


def _short_names(names):
    """B2 (the live R4 run): {token: record id} — each record's one-word short names: the tokens of its name with a proper name's shape
    (a digit, or two capitals or more) that no other record's name holds. A writer shortens "211 Kansas / United Way" to "211" and
    "TRAIL (Transitioning Resources …)" to "TRAIL"; an ordinary capitalized word ("Health", "Program") is never one (it would over-match)."""
    held = {}
    for rid, nm in names:
        for t in dict.fromkeys(_NAME_TOK.findall(str(nm))):      # in the name's order: no output depends on the hash seed
            if _SHAPE.search(t):
                held.setdefault(t, set()).add(rid)
    return {t: next(iter(ids)) for t, ids in held.items() if len(ids) == 1}


def _says(sent, tok):
    """Whether the sentence holds the short name as a whole word, as written (case kept: "TRAIL", not "trail"); a possessive counts."""
    return re.search(r"(?<![\w.'’-])" + re.escape(tok) + r"(?![\w-]|\.\w)", sent) is not None


def _without_names(sent, names, short=None):
    """The sentence with the plan's own option names taken out, whole, by a two-word fragment (any punctuation between the words), or by
    a one-word short name (`short`: token → record id, for the plan's records), so a number that is part of a name ("211 Kansas / United
    Way", "211 serves 2 languages") is not read as a figure the sentence claims."""
    pats = set()
    for nm in names:
        pats.add(re.escape(str(nm)))
        pats |= {r"\b" + r"\W+".join(re.escape(w) for w in f.split()) + r"\b" for f in _frags(nm) if len(f.split()) > 1}
    for pt in sorted(pats, key=lambda x: (-len(x), x)):          # the longest first; a tie by its text, never by the hash seed
        sent = re.sub(pt, " ", sent, flags=re.I)
    for t in sorted(short or {}, key=lambda x: (-len(x), x)):
        sent = re.sub(r"(?<![\w.'’-])" + re.escape(t) + r"(?![\w-]|\.\w)", " ", sent)
    return sent


def _flat(t):
    """Lower case, apostrophes and punctuation as spaces, one space between words: the one form both a sentence and a name are read in."""
    return " ".join(re.sub(r"[.,;:()'\"’‘“”!?]", " ", str(t).lower()).split())


def _has_frag(text_flat, frag):
    return re.search(r"(?<!\w)" + re.escape(frag) + r"(?!\w)", text_flat) is not None


def _sentences(text):
    """BUILD IC-8 (D1, one finder): the registry's split_sentences, each stripped — this module's own split at . ! ? with its own
    abbreviations (_ABBR: "St. Anthony" was cut, review) is gone; "St.", "H.O.T." and initials are the one finder's too."""
    return [x.strip() for x in REG.split_sentences(text or "") if x.strip()]


def _frags(name):
    """A record name's two-word fragments without a stop word (the closure matcher's unit): "Open Door" names United Methodist Open
    Door even when the sentence shortens it. A name with no such pair ("Center of Hope", "CommunityHealth") is matched whole."""
    w = [x for x in _flat(name).split() if x]
    fr = {f"{a} {b}" for a, b in zip(w, w[1:]) if a not in _STOP and b not in _STOP}
    return fr or ({" ".join(w)} if w else set())


def _strip_ids(sent, name_of):
    """BUILD IC-8 (D6, step 1): each echoed known record id out of a sentence — "<id> <name>" → "<name>", "<name> (<id>)" → "<name>", a
    lone id → its record's name; matched whole, longest first, before any name is masked (F20: a name that holds digits, "211 Kansas /
    United Way", is never touched here). → (the sentence, how many were stripped: id_stripped, not a loss)."""
    out, n = sent, 0
    for rid in sorted(name_of, key=lambda x: (-len(str(x)), str(x))):
        nm = " ".join(str(name_of[rid] or rid).split())
        rx = re.compile(r"(?<![\w-])" + re.escape(str(rid)) + r"(?![\w-])", re.I)
        pos = 0
        while True:
            m = rx.search(out, pos)
            if not m:
                break
            head = re.escape(nm[:max(4, min(len(nm), 16))])
            if re.match(r"\s*" + head, out[m.end():], re.I):                  # "<id> <name>": the id and its space go
                end = m.end() + len(re.match(r"\s*", out[m.end():]).group(0))
                out, pos = out[:m.start()] + out[end:], m.start()
            elif re.search(head + r"[^.!?]{0,80}?\(\s*$", out[:m.start()], re.I) and re.match(r"\s*\)", out[m.end():]):
                b = out[:m.start()].rstrip()[:-1].rstrip()                        # "<name> (<id>)": the parenthesis goes
                e = m.end() + len(re.match(r"\s*\)", out[m.end():]).group(0))
                out, pos = b + out[e:], len(b)
            else:                                                                # a lone id: its record's name
                out, pos = out[:m.start()] + nm + out[m.end():], m.start() + len(nm)
            n += 1
    return (" ".join(out.split()) if n else sent), n


_NOMINAL = re.compile(r"\b(?:Route|Highway|Hwy\.?)\s+\d+\b|\b(?:I|K|US)-\d+\b|\bTitle\s+\d+\b|\b\d+(?:st|nd|rd|th)\b"
                      r"|\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.m\.|p\.m\.|am|pm)(?![\w.])|\b(?:[01]?\d|2[0-3]):[0-5]\d\b", re.I)
#   BUILD IC-8 (P4.4, p44m): a clock time only — 1–12 before am/pm, H:MM with H 0–23 and MM 00–59; '13pm' or '9:75' is a number to check
_LINES = ("211", "311", "911", "988")
_LINE_AFTER = re.compile(r"\b(?:call|calls|calling|dial|dials|dialing|text|texts|texting)\s*$", re.I)


def _mask_nominal(nt, a, plan_names):
    """BUILD IC-8 (D6, step 2): a sentence's nominal numbers masked before the number check — a "#k" that is a rank of this plan, a named
    line (211, 311, 911, 988) where a plan record's name holds it or call, dial or text is the word before, and the nominal forms:
    Route, Highway, I-, K- or US- with a number, Title with a number, ordinals, clock times."""
    ranks = set(range(1, len(a.get("top") or []) + 1))
    nt = re.sub(r"#(\d+)\b", lambda m: " " if int(m.group(1)) in ranks else m.group(0), nt)
    held = {ln for ln in _LINES for nm in plan_names if re.search(r"(?<!\d)" + ln + r"(?!\d)", str(nm))}
    nt = re.sub(r"(?<![\w.,])(?:211|311|911|988)(?![\w,]|\.\d)",
                lambda m: " " if m.group(0) in held or _LINE_AFTER.search(nt[:m.start()]) else m.group(0), nt)
    return _NOMINAL.sub(" ", nt)


def engine_sentence(k, x):
    """BUILD IC-8 (D6): the engine's sentence for a ranked option no kept sentence explains — its name, its rank, its score and each
    component's points, the plan's own figures as the listing gives them ("<name> ranks #<k> with <score> points: <component>
    <points>, …."); checked like any sentence (fallback_default)."""
    parts = ", ".join(f"{q['label']} {q['points']}" for q in x.get("components") or [])
    return f"{x['name']} ranks #{k} with {x['score']} points" + (f": {parts}." if parts else ".")


def _rule_why(verdicts):
    """BUILD IC-8 (D3, D6): THE unit rule on one explanation's sentences ([(sentence, kept?, reason)]) — the failed ones may go only if a
    sentence stays and no kept sentence after one opens by pointing back (REG.POINT_BACK). → None, or the rule that fails."""
    keep = [i for i, v in enumerate(verdicts) if v[1]]
    gone = [i for i, v in enumerate(verdicts) if not v[1]]
    if not gone:
        return None
    if not keep:
        return "only_sentence"
    for g in gone:
        nxt = next((j for j in keep if j > g), None)
        lead = verdicts[nxt][0].lstrip(" \"'“‘(") if nxt is not None else ""
        if lead and any(lead == w or lead.startswith(w + " ") or lead.startswith(w + ",") for w in REG.POINT_BACK):
            return "next_points_back"
    return None


def check_rationale(reply_obj, allowed, bundle, units=None):
    """D6, checked in Python: a sentence is kept only if every number in it occurs in that plan's input and every option it names is
    in that plan (by any record name of the page). Returns ({preset: {set: {"sentences", "top"}}}, [lines: each sentence dropped and each
    note not used, in the reply's order], the number of those lines that are single sentences the check dropped).
    BUILD IC-8 (D6): each sentence in four steps — (1) an echoed known id stripped (_strip_ids); (2) the plan's names and the nominal
    forms masked (_mask_nominal); (3) an id-shaped string of a governed prefix (the page's record ids' own prefixes) that names no record:
    unknown_id; (4) the number check. The page shows the stripped sentence: no record id. A sentence that fails goes under D3's unit rule
    (_rule_why), else the explanation goes whole. The engine's sentence (engine_sentence) for each option of a ranked set (its top
    RATIONALE_TOP: the options the page shows a note beside) that no kept sentence names — after the set's kept sentences, or alone in a
    set no kept sentence explains — each checked like any sentence. `units` (a list)
    receives one {unit, disposition, reason, item} per sentence (architect.<preset>.<set>.s<k>: added_valid, or omitted with its
    reason) and per engine sentence (….engine.<rank>: fallback_default)."""
    kept, dropped, n_bad = {}, [], 0
    U = units if isinstance(units, list) else []
    recs = [(r["id"], r["name"]) for s in bundle["sets"] for r in s["records"]]
    ids_all = {r["id"] for s in bundle["sets"] for r in s["records"]}
    name_of = {rid: nm for rid, nm in recs}
    short = _short_names(recs)                                       # B2 (live R4): one-word short names, page-wide
    forms = ENG.name_forms({rid: nm for rid, nm in recs})           # BUILD IC-5 (D5 a): the status decision's own name forms
    pre_ = sorted({m.group(1).lower() for i in ids_all for m in [re.fullmatch(r"([A-Za-z]+(?:-[A-Za-z]+)*)-\d+", str(i))] if m},
                  key=lambda x: (-len(x), x))
    unk_rx = re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, pre_)) + r")-\d+(?![\w-])", re.I) if pre_ else None

    def unit(pid, sid, tail, d, r, item=None):
        U.append({"unit": f"architect.{pid}.{sid}.{tail}", "disposition": d, "reason": r, "item": item or f"{pid}.{sid}",
                  "source": "default" if d == "fallback_default" else "candidate"})

    def checked(sent, a, plan_names, extra_names=()):
        """(the stripped sentence, its failure reasons [], the in-plan options it names, the number of ids stripped, the bad numbers, the
        options outside the plan, the unknown ids, the options named only by a piece)."""
        s1, n_strip = _strip_ids(sent, name_of)
        nt = _without_names(s1, list(plan_names) + list(extra_names), {t: r for t, r in short.items() if r in a["ids"]})
        unk = sorted({m.group(0) for m in unk_rx.finditer(s1)}) if unk_rx else []
        nt = _mask_nominal(unk_rx.sub(" ", nt) if unk_rx else nt, a, plan_names)
        bad_n = ([m for m in _DIGITS.findall(nt) if float(m.replace(",", "")) not in a["numbers"]]
                 + [w for w, v in _word_numbers(nt) if float(v) not in a["numbers"]])
        spans_ = ENG.names_in(s1, forms)
        by_id = {" ".join(str(name_of.get(r) or r).split()).lower(): r for r in name_of      # BUILD IC-8 (P4.4, p44m): a name the strip put
                 if re.search(r"(?<![\w-])" + re.escape(str(r)) + r"(?![\w-])", sent, re.I)}   # in for an id names that id's record only
        if by_id:
            spans_ = sorted({(s_, e_, by_id.get(" ".join(s1[s_:e_].split()).lower(), rid), h_) for s_, e_, rid, h_ in spans_})
        mine_ = {(s_, e_) for s_, e_, rid, _h in spans_ if rid in a["ids"]}
        out_ = [(rid, how) for s_, e_, rid, how in spans_ if rid not in a["ids"] and (s_, e_) not in mine_]
        bad_o = sorted({rid for rid, how in out_ if not how.startswith("the piece")}
                       | {r for t, r in short.items() if r not in a["ids"] and _says(s1, t)})    # B2: "TRAIL", "EmberHope"
        frag_o = sorted({rid for rid, how in out_ if how.startswith("the piece")} - set(bad_o))
        why = (["number_not_in_trace"] if bad_n else []) + (["option_outside_plan"] if bad_o else []) + (["unknown_id"] if unk else [])
        named = {rid for _s, _e, rid, _h in spans_ if rid in a["ids"]}
        return s1, why, named, n_strip, bad_n, bad_o, unk, frag_o

    if not isinstance(reply_obj, dict):
        return {}, ["the reply is not a JSON object"], 0
    for pid, sets in reply_obj.items():
        if pid not in allowed:
            dropped.append(f"{pid}: no such preset")
            continue
        if not isinstance(sets, dict):
            dropped.append(f"{pid}: not an object of sets")
            continue
        for sid, text in sets.items():
            if sid not in allowed[pid]:
                dropped.append(f"{pid}.{sid}: not a set of that plan")
                continue
            if not isinstance(text, str) or not text.strip():
                dropped.append(f"{pid}.{sid}: no text")
                continue
            a = allowed[pid][sid]
            if not a["top"]:          # B2 (live R4): the page shows a note only beside a ranked option (why()), so none is kept here
                dropped.append(f"{pid}.{sid}: not kept — nothing is ranked in this set (a note is shown only beside a ranked option)")
                continue
            flagged_, verdicts, bad_named = [], [], set()
            text = text.encode("utf-8", "replace").decode("utf-8")        # B2 (review): a lone surrogate would stop the page's write
            plan_names = [nm for rid, nm in recs if rid in a["ids"]]
            for sent in _sentences(text):
                s1, why, named, n_strip, bad_n, bad_o, unk, frag_o = checked(sent, a, plan_names)
                if not why and frag_o:
                    flagged_.append(s1)                 # BUILD IC-5 (D5b): the page's record holds it (D5 a: console and page)
                    dropped.append(f"{pid}.{sid}: {FLAGGED} \"{s1[:240]}\" — it names an option outside the plan only by a piece "
                                   f"of its name ({', '.join(frag_o)}): to read")
                if why:
                    n_bad += 1
                    bad_named |= named
                    dropped.append(f"{pid}.{sid}: dropped \"{s1[:240]}\" — " + "; ".join(
                        ([f"number(s) not in the trace: {', '.join(bad_n)}"] if bad_n else [])
                        + ([f"option(s) not in the plan: {', '.join(bad_o)}"] if bad_o else [])
                        + ([f"an id that names no record: {', '.join(unk)}"] if unk else [])))
                verdicts.append((s1, not why, "+".join(why) if why else ("id_stripped" if n_strip else "checked"), named))
            rule_ = _rule_why(verdicts)                  # BUILD IC-8 (D3): the failed sentences go only as the rule allows
            if rule_:
                dropped.append(f"{pid}.{sid}: the explanation goes whole — its failed sentences cannot go alone ({rule_})")
            good = [v[0] for v in verdicts if v[1]] if not rule_ else []
            for k_, v in enumerate(verdicts, 1):
                unit(pid, sid, f"s{k_}", "added_valid" if (v[1] and not rule_) else "omitted", v[2] if (v[1] and not rule_) or not v[1] else rule_)
            named_kept = set().union(*[v[3] for v in verdicts if v[1]]) if good else set()
            eng_at = []                 # BUILD IC-8 (P4.4, p44a): the engine's lines' places — the page names them as the engine's
            for k_, x in enumerate(a.get("ranked") or [], 1):       # BUILD IC-8 (D6): each option no kept sentence names
                if good and x["id"] not in named_kept:
                    e_ = engine_sentence(k_, x)
                    if not checked(e_, a, plan_names, [q["label"] for q in x.get("components") or []])[1]:
                        eng_at.append(len(good))
                        good.append(e_)
                        unit(pid, sid, f"engine.{k_}", "fallback_default", "engine_sentence")
            if good:
                kept.setdefault(pid, {})[sid] = {"sentences": good, "top": a["top"], "counts": a.get("counts"),
                                                 **({"flags": flagged_} if flagged_ else {}), **({"engine_at": eng_at} if eng_at else {})}
    for pid, sets_ in allowed.items():                  # BUILD IC-8 (D6): a ranked set of the plan that no kept sentence explains:
        for sid, a in sets_.items():                    # each of its options the engine's sentence
            if a.get("top") and sid not in kept.get(pid, {}) and a.get("ranked"):
                plan_names = [nm for rid, nm in recs if rid in a["ids"]]
                eng_ = []
                for k_, x in enumerate(a["ranked"], 1):
                    e_ = engine_sentence(k_, x)
                    if not checked(e_, a, plan_names, [q["label"] for q in x.get("components") or []])[1]:
                        eng_.append(e_)
                        unit(pid, sid, f"engine.{k_}", "fallback_default", "engine_sentence")
                if eng_:
                    kept.setdefault(pid, {})[sid] = {"sentences": eng_, "top": a["top"], "counts": a.get("counts"), "engine": True,
                                                     "engine_at": list(range(len(eng_)))}      # BUILD IC-8 (P4.4, p44a)
    return kept, dropped, n_bad


def rationale_items(obj, named=None):
    """BUILD IC-4 (D12): the list form's reply ({"items": [{"preset", "set", "explanation"}]}) as the map check_rationale reads (preset ->
    set -> text), with a line for each item not used: one that is not an object with the three texts, or a second explanation for the same
    set (the first is used). A reply that is not an object holding a list of items gives (None, [its line]).
    BUILD IC-5 (D16): `named` (a dict; this build's switch on) is filled with each used item's own options_named — preset -> set -> its
    ids, each stripped and once, in the reply's order — or None for an item whose options_named is not a list of strings: it gives no ids,
    and its words alone are checked (check_rationale)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return None, ["the reply holds no list of explanations (an object with a list named items)"]
    out, lines = {}, []
    for n, it in enumerate(obj["items"], 1):
        p, s, e = (it.get(k) if isinstance(it, dict) else None for k in ("preset", "set", "explanation"))
        if not (isinstance(p, str) and isinstance(s, str) and isinstance(e, str)):
            lines.append(f"item {n}: not an explanation with its preset, its set and its text")
            continue
        if s in out.get(p, {}):
            lines.append(f"{p}.{s}: a second explanation for the same set — the first is used")
            continue
        out.setdefault(p, {})[s] = e
        if named is not None:           # BUILD IC-5 (D16): the item's own ids, taken with its explanation
            v = it.get("options_named")
            named.setdefault(p, {})[s] = (list(dict.fromkeys(x.strip() for x in v))
                                          if isinstance(v, list) and all(isinstance(x, str) for x in v) else None)
    return out, lines


def check_named(obj, named, allowed, bundle):
    """BUILD IC-5 (D16): each explanation's options_named, checked against its own plan before its words are read — the options it names
    by their ids, never by its text (no identity from text or position). An explanation is not used when its ids hold an option outside
    that plan (another set's, or one the plan neither ranks nor asks to confirm) or an id no option of the page has: the reply says it names
    an option outside the plan, whatever its words say. An explanation without ids (`named` holds None for it) is passed on: its words alone
    are checked; one whose preset or set is not the plan's is passed on for check_rationale to name. D5 (a)'s name check (check_rationale)
    reads every sentence of what is passed on — the backstop. Returns (obj without the explanations not used, [a line for each not used],
    [a line to read for each used one whose ids and the names its words hold differ], (the number checked by id, the number of those not
    used, the number without ids))."""
    recs = [(r["id"], r["name"]) for s in bundle["sets"] for r in s["records"]]
    ids_all = {rid for rid, _nm in recs}
    forms = ENG.name_forms({rid: nm for rid, nm in recs})          # D5 (a)'s own name forms: the words' reading, to compare only
    kept, lines, diff, n_list, n_out, n_none = {}, [], [], 0, 0, 0
    for pid, sets in (obj or {}).items():
        if not isinstance(sets, dict) or pid not in allowed:
            kept[pid] = sets                                         # no such preset, or not an object of sets: check_rationale names it
            continue
        for sid, text in sets.items():
            a = allowed[pid].get(sid)
            ids = ((named or {}).get(pid) or {}).get(sid)
            if a is None or ids is None:                             # not a set of that plan (check_rationale names it), or no ids
                n_none += a is not None
                kept.setdefault(pid, {})[sid] = text
                continue
            n_list += 1
            outside = sorted({i for i in ids if i in ids_all and i not in a["ids"]})
            unknown = [i for i in ids if i not in ids_all]
            if outside or unknown:
                n_out += 1
                lines.append(f"{pid}.{sid}: not used — its options_named holds " + "; ".join(
                    ([f"an option outside this plan ({', '.join(outside)})"] if outside else [])
                    + ([f"no option of this page ({', '.join(repr(i[:60]) for i in unknown)})"] if unknown else [])))
                continue
            kept.setdefault(pid, {})[sid] = text
            words = sorted({rid for _s, _e, rid, how in ENG.names_in(text if isinstance(text, str) else "", forms)
                            if not how.startswith("the piece")})
            if sorted(ids) != words:
                diff.append(f"{pid}.{sid}: its options_named lists {', '.join(sorted(ids)) or 'none'}; its words name {', '.join(words) or 'none'}")
    return kept, lines, diff, (n_list, n_out, n_none)


def _named_counts(n):
    """BUILD IC-5 (D16): the count line's words for the ids' check — "" with the switch off (n None) or when no explanation of the plan
    was read."""
    n_list, n_out, n_none = n or (0, 0, 0)
    if not (n_list or n_none):
        return ""
    if not n_list:
        return f"; options named by id: none given ({n_none} explanation(s) checked on their words alone)"
    return (f"; options named by id: {n_list - n_out} of {n_list} explanation(s) within their plan"
            + (f", {n_none} gave none (checked on their words alone)" if n_none else ""))


FLAGGED = "kept and flagged"      # BUILD IC-5 (D5b): a console line's words for a sentence kept with its flag (counted apart)


def _drop_counts(n_bad, n_lines):
    """B2 (second live R4): the console counts each kind in its own unit — sentences the check dropped, and notes not used whole (the
    count once read "24 sentence(s) kept …, 2 dropped" where the 2 were notes of two sentences each)."""
    return f"{n_bad} sentence(s) dropped" + (f", {n_lines - n_bad} note(s) not used" if n_lines > n_bad else "")


def _call_ident(label, profile, second=None):
    """BUILD IC-8 (D8): a model call's identity (NR.new_call: its call id, sequence, generation attempt and journal) as model_call's
    keyword arguments."""
    ident = NR.new_call(HARNESS_DIR / "checkpoints", label, profile, 2 if second is not None else 1)
    return {"journal": ident["journal"], "ident": ident}


def call_claude(api_key: str, system_prompt: str, user_prompt: str, model: str = RATIONALE_MODEL, fmt: dict | None = None) -> str:
    """One streamed call, the Muse's pattern (muse_harness.py call_claude): the SDK never retries (max_retries=0); the stop reason is
    named; anything but a finished answer with text raises. BUILD IC-4 (D12): `fmt`, a JSON schema, is sent as output_config.format
    ({"type": "json_schema", "schema": fmt}); without it output_config holds the effort alone, as before. BUILD IC-6 (D1): its limit,
    effort, stream mode and the call log's label are its profile's, "architect/rationale"."""
    prof = NR.profile("architect/rationale")
    import anthropic
    import httpx
    import threading
    client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0))
    t0 = time.monotonic()
    attempt = NR.next_attempt(HARNESS_DIR / "checkpoints", prof["label"])     # BUILD IC-7 (D10): a re-run never overwrites a journal
    blk = NR.block_end_for("architect", time.monotonic)      # BUILD IC-6 (D3, I3): never past the block's clock
    end_at = t0 + RATIONALE_DEADLINE_S if blk is None else min(t0 + RATIONALE_DEADLINE_S, blk)
    try:            # BUILD IC-3 (D5): the one helper — every event read, a line in checkpoints/call_log.jsonl, the call ended at its end
        #             time (B2's deadline, RATIONALE_DEADLINE_S, read on every event and by a timer, not only on text)
        msg, _text = NR.model_call(client, {"model": model, "max_tokens": prof["max_tokens"],
                                            "output_config": NR.output_config(prof["effort"], fmt), "system": system_prompt,
                                            "messages": [{"role": "user", "content": user_prompt}]},
                                   label=prof["label"], attempt=attempt, log_dir=HARNESS_DIR / "checkpoints", clock=time.monotonic,
                                   end_at=end_at, stream=prof["stream"], timer=threading.Timer,
                                   profile="architect/rationale",
                                   **_call_ident(prof["label"], "architect/rationale"))     # BUILD IC-6 (D6); IC-8 (D8): its identity
    except Exception:
        p_, _why = NR.save_partial(HARNESS_DIR / "checkpoints", prof["label"], 1, (getattr(NR.model_call, "last", None) or (None, ""))[1])
        if p_ is not None:
            NR.say(f"    partial answer kept: {p_}")
        raise
    reason = getattr(msg, "stop_reason", None)
    th = ((getattr(NR.model_call, "last", None) or ({}, ""))[0] or {}).get("thinking_tokens", "not reported")
    NR.say(f"    {getattr(msg, 'model', '?')} · stop={reason} · out={getattr(getattr(msg, 'usage', None), 'output_tokens', None)} tok · "
           + (f"think={th} tok" if isinstance(th, int) else "think=not reported") + f" · {time.monotonic() - t0:.1f}s")
    if reason != "end_turn":
        raise RuntimeError(f"stop_reason={reason} is not a finished answer")
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise RuntimeError("the model finished without any text")
    return text


def block1_base(doc):
    """B2: Block 1's payloads for the engine at Block 2 (the pipeline's snapshot beside this folder, else the Oracle's Block 1 base), so a
    field or a record the curveball removed is told from a typo in the sidecar. None when neither is there (the engine then names both)."""
    cid = ((doc or {}).get("challenge") or {}).get("id")
    return (REG.admit(NR.block1_payloads(HARNESS_DIR.parent / "landing-harness", cid, HARNESS_DIR.parent / "oracle-harness" / "checkpoints"),
                      "data", "block1 payloads") if cid else None)     # BUILD IC-5 (D1): the organizers' Block 1 data, a file a run wrote


_RAT_UNITS = []            # BUILD IC-8 (D3, D6): this run's explanation units (check_rationale, _rationale) — finalize_architect's
_RAT_STATE = {"read": False}   # whether a reply was read and checked this run (no call, no reply: no unit — Phase 3's path)


def finalize_architect(checkpoint_dir):
    """BUILD IC-8 (D2, D3, D6): the Architect's one outcome per unit — each sentence of each explanation (architect.<preset>.<set>.s<k>),
    each engine sentence (….engine.<rank>, fallback_default), each explanation not used (architect.<preset>.<set>, omitted), as
    check_rationale recorded them this run; then each fit of this process that changed its text (REG.fit_units). Written to
    checkpoints/dispositions_<run id>.jsonl with the content line. No reply read this run (no key, the call failed): no unit, None.
    Under NAIPC_STRICT=1 an exception raises; live it is named and the page stands. → the content (counts) or None."""
    if not _RAT_STATE["read"]:
        return None
    try:
        disp = REG.Dispositions(NR.BUILD_ID, "architect", NR.run_id())
        for x in _RAT_UNITS:
            disp.expect(x["unit"])
            if not disp.has(x["unit"]):
                disp.add(x["unit"], x["disposition"], x["reason"], x.get("source") or "candidate", item=x.get("item"))
        REG.fit_units(disp, "architect")
        why_ = disp.write(Path(checkpoint_dir) / f"dispositions_{NR.run_id()}.jsonl", {"invocation": os.environ.get("NAIPC_INVOCATION_NUMBER")})
        NR.say(f"  {disp.content_line('Architect')}" + (f" (dispositions not written: {why_})" if why_ else ""))
        return disp.content()
    except Exception as e:  # noqa: BLE001 — §1: live, the page stands and the record's loss is named
        if REG.strict():
            raise
        NR.say(f"  ⚠ the Architect's dispositions not recorded ({type(e).__name__}) — the page stands")
        return None


def rationale(spec, doc, raw_endpoints, curveball_text, bundle, base=None):
    """Never raises: the page is built with or without the notes."""
    try:
        return _rationale(spec, doc, raw_endpoints, curveball_text, bundle, base)
    except Exception as e:  # noqa: BLE001
        return None, f"not shown — {type(e).__name__}: {str(e)[:120]}"


def _rationale(spec, doc, raw_endpoints, curveball_text, bundle, base=None):
    """(rationale or None, why) — P1.4 (D6). One build-time call when ANTHROPIC_API_KEY is set: each preset's plan with its trace at the
    run's clock in, JSON preset → set → sentences out; each sentence checked (check_rationale); the reply kept in checkpoints/."""
    _RAT_UNITS.clear()                     # BUILD IC-8 (D3, D6): this run's units, recorded by finalize_architect
    _RAT_STATE["read"] = False
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        NR.mark_path("rationale.not_called.no_key")     # BUILD IC-8 (D14; RECORD 2): the rationale not used — a path of its own
        return None, "not written — ANTHROPIC_API_KEY not set (the plan shows no explanation; the engine's order stands)"
    with ENG.names_mode(raw=not NR.ic8_prompt()):     # BUILD IC-8 (F29): the request's reads and details — IC-7's text with the switch off
        res = ENG.run(spec, doc, raw_endpoints, curveball_text, page="architect", base=base)       # the bundle's own validation
    if res.get("status") != "ok" or not res.get("presets"):
        NR.mark_path("rationale.not_called.not_ranked")     # BUILD IC-8 (D14)
        return None, "not written — the presets could not be ranked"
    text, allowed = _plan_input(res, bundle)
    prompt = (f"The engine ranked these options at {res['as_of']}. For each preset and each set below that ranks an option, write two or "
              "three plain sentences on why the first option leads (its points, from the figures given) and what to confirm. Each factor "
              "is given as its name, its points, then in brackets what the record holds and its value. The name says what earns points, "
              "and what the record holds can measure the opposite: "
              + ("\"No fee 0.0 (fee required: yes)\" means a fee is required, and \"Items kept 16.0 (loss rate: 0.2)\" means a loss rate "
                 "of 0.2. " if NR.ic8_prompt() else       # BUILD IC-8 (D12, Q-SWITCH): the examples read as the reads now do
                 "\"No fee 0.0 (feeRequired: yes)\" means a fee is required, and \"Items kept 16.0 (lossRate: 0.2)\" means a loss rate of 0.2. ")
              + "Write a value as what the record holds, in plain words, never beside "
              "the factor's name as if it measured that. Use only the numbers and the option names given here, and write every number in "
              "digits, as given. " + (RATIONALE_LIST_FORM if NR.ic4_prompt() else      # BUILD IC-4 (D12): the list form (switch on)
                                      "Reply with JSON only: {\"<preset id>\": {\"<set id>\": \"<sentences>\"}}.")
              + (((RATIONALE_NAMED_FORM_IC8 + RATIONALE_NAME_LINE) if NR.ic8_prompt() else RATIONALE_NAMED_FORM) if NR.ic5_prompt() else "")
              + "\n\n" + text)      # BUILD IC-5 (D16): the ids it names; IC-8 (D6): after the name, and names in the words
    try:
        reply = call_claude(os.environ["ANTHROPIC_API_KEY"].strip(), RATIONALE_SYSTEM, prompt,      # BUILD IC-5 (D16): the ids' schema
                            fmt=(RATIONALE_SCHEMA_IC5 if NR.ic5_prompt() else RATIONALE_SCHEMA) if NR.ic4_prompt() else None)
    except Exception as e:  # noqa: BLE001
        NR.mark_path("rationale.not_used.call_failed")     # BUILD IC-8 (D14)
        return None, f"not written — the call failed ({type(e).__name__}: {str(e)[:120]})"
    kept_as = f"rationale_reply_{re.sub(r'[^A-Za-z0-9_-]+', '-', NR.run_id() or 'run')}.txt"   # BUILD IC-4 (D8): one name, the file written
    try:
        cp = HARNESS_DIR / "checkpoints"
        cp.mkdir(parents=True, exist_ok=True)
        (cp / kept_as).write_bytes(reply.encode("utf-8", errors="replace"))
        kept_as = f"kept in checkpoints/{kept_as}"
    except OSError as e:
        kept_as = f"not kept: checkpoints/{kept_as} could not be written ({type(e).__name__})"
    try:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", reply, re.DOTALL)
        obj, dfind = NR.strict_loads((m.group(1) if m else reply).strip(), "rationale")    # BUILD IC-3 (D4): the one strict decoder
        NR.record_decode_findings(HARNESS_DIR / "checkpoints", dfind)
        obj = REG.admit(obj, "reply", "rationale")        # BUILD IC-5 (D1, I1): the one way in (this harness had no input cleaner)
        REG.write_admit_findings(HARNESS_DIR / "checkpoints", NR.run_id())
    except Exception:  # noqa: BLE001
        NR.mark_path("rationale.not_used.not_json")     # BUILD IC-8 (D14)
        return None, f"not shown — the reply is not JSON ({kept_as})"
    pre, named, ids_diff, n_ids = [], ({} if NR.ic5_prompt() else None), [], None    # BUILD IC-5 (D16): the ids, with the switch on
    if NR.ic4_prompt():             # BUILD IC-4 (D12): the list form read back into preset -> set -> text; each item not used named
        obj, pre = rationale_items(obj, named)
        if obj is None:
            for d in pre:
                NR.say(f"    rationale: {d}")
            NR.mark_path("rationale.not_used.no_list")     # BUILD IC-8 (D14)
            return None, f"not shown — the reply holds no list of explanations ({kept_as})"
    if named is not None:           # BUILD IC-5 (D16): each explanation's ids checked against its plan before its words are read
        obj0_ = obj
        obj, not_used, ids_diff, n_ids = check_named(obj, named, allowed, bundle)
        pre = pre + not_used
        for p_, s_ in [(p_, s_) for p_, ss_ in (obj0_ or {}).items() if isinstance(ss_, dict) and p_ in allowed for s_ in ss_
                       if s_ in allowed[p_] and allowed[p_][s_].get("top") and s_ not in ((obj or {}).get(p_) or {})]:
            _RAT_UNITS.append({"unit": f"architect.{p_}.{s_}", "disposition": "omitted", "reason": "named_outside_plan",
                               "item": f"{p_}.{s_}", "source": "candidate"})       # BUILD IC-8 (D3): an explanation not used, one unit
    kept, dropped, n_bad = check_rationale(obj, allowed, bundle, units=_RAT_UNITS)
    _RAT_STATE["read"] = True
    dropped = pre + dropped
    for d in dropped:
        NR.say(f"    rationale: {d}")
    for d in ids_diff:              # BUILD IC-5 (D16): to read — a used explanation whose ids and the names its words hold differ
        NR.say(f"    rationale ids: {d}")
    n_flag = sum(1 for d in dropped if f": {FLAGGED} " in d)         # BUILD IC-5 (D5b): kept with a flag, never a note not used
    if not kept:
        NR.mark_path("rationale.not_shown.none_passed")     # BUILD IC-8 (D14)
        return None, ("not shown — no sentence passed the check against the trace"
                      + (f" ({_drop_counts(n_bad, len(dropped) - n_flag)})" if dropped else "") + _named_counts(n_ids))
    n_eng = sum(len(v.get("engine_at") or []) for p in kept.values() for v in p.values())    # BUILD IC-8 (P4.4, p44a): read from the
    n = sum(len(v["sentences"]) for p in kept.values() for v in p.values()) - n_eng            # entries the page shows, by their places
    n_pc = sum(1 for p in kept.values() if any(len(v["sentences"]) > len(v.get("engine_at") or []) for v in p.values()))
    NR.mark_path("rationale.shown")     # BUILD IC-8 (D14)
    return ({"as_of": res["as_of"], "model": RATIONALE_MODEL, "by": kept, "claude": n, "engine": n_eng},
            f"{n} of Claude's sentence(s) kept for {n_pc} preset(s)" + (f" ({n_flag} {FLAGGED})" if n_flag else "")
            + (f"; {n_eng} of the engine's line(s) for option(s) or set(s) no kept sentence explains" if n_eng else "")
            + f"; {_drop_counts(n_bad, len(dropped) - n_flag)} (checked against the trace)" + _named_counts(n_ids))


def engine_chat_block(view) -> str:
    """D6: the chat context gains the engine's table and the records' values (the Worker answers only from the context)."""
    if not view:
        return ""
    b, out = view["bundle"], ["PRIORITIZATION TABLE (the Get Help tab ranks with it in the browser; from the engine sidecar):"]
    for s in b["sets"]:
        t = s["table"]
        out.append(f"• {s['label']} ({t['endpoint']}, {t['records']} records). Gates: " + "; ".join(f"{g['label']}: {g['text']}" for g in t["gates"])
                   + ". Factors (points): " + "; ".join(f"{f['label']} {f['max']} — {f['formula']} — {f['why']}" for f in t["factors"])
                   + ". Ties: " + ", ".join(t["tie_breakers"]) + ".")
        for r in s["records"]:
            vals = dict(r["raw"], **{f: a["value"] for f, a in r["ann"].items()})
            out.append(f"  - {r['name']} ({r['id']}): " + ", ".join(f"{k}: {_shown(v)}" for k, v in vals.items())
                       + (f"; closed or flagged: {r['pre'][0][2]}" if r["pre"] and r["pre"][0][1] == "fail" else "")
                       + "".join(f"; {k}: {_shown(v)}" for k, v in r["contact"].items()))
    ck = b.get("checklist")
    if ck:
        out.append(f"DOCUMENTS ({ck['label']}, unlock order): " + "; ".join(
            f"{i['name']} ({_shown(i.get('days'))} days, fee {_shown(i.get('fee'))}, unlocks {_shown(i.get('unlocks'))})" for i in ck["items"]))
    return "\n".join(out)


def build_context(
    normalized: Dict[str, Any],
    curveball_text: str = "",
    brand_config: Optional[Dict] = None,
    changes: Optional[Dict] = None,
    cb_record: Optional[Dict] = None,
    raw_endpoints: Optional[Dict[str, Any]] = None,
    reg=None,
) -> Dict[str, Any]:
    """Build the Jinja2 template context from normalized resource data. P0.7: `changes` is this run's change list (NAIPC_CHANGES),
    `cb_record` the Oracle's curveball.json of this run (--curveball-json) — both only in Block 2. BUILD B2: `raw_endpoints` (the API's
    payloads as fetched) feed the engine's bundle for the Get Help tab (help_view)."""
    resources = normalized["resources"]
    resources, alert_html = apply_curveball(resources, curveball_text)
    cb_block, changed_ids, snapshot_asof, derived_tiles = change_view(changes, resources, curveball_text, normalized, cb_record)
    if cb_block:                       # BUILD IC-4 (D3, D1): the announcement's words, each quantity its mention's span
        q_ = (lambda s: reg.quote_html("announcement", s)) if reg is not None else REG.esc
        cb_block = dict(cb_block, first_h=q_(cb_block.get("first") or ""), announcement_h=q_(cb_block.get("announcement") or ""))
    for r in resources:     # P0.7 (D7): the cards of records the curveball added or changed
        if (r.get("source_endpoint"), r.get("id")) in changed_ids:
            r["changed_at_curveball"] = True

    if derived_tiles and isinstance(raw_endpoints, dict):     # BUILD IC-8 (D12): the tiles' script prints these — in a reader's words
        nm_ = REG.ReaderNames(raw_endpoints, NR.records, before=getattr(reg, "_before_raw", None))
        derived_tiles = [dict(t, field=REG.field_words(t["field"]) if t.get("field") else t.get("field"),
                              minus=REG.field_words(t["minus"]) if t.get("minus") else t.get("minus"),
                              endpoint=REG.endpoint_words(t["endpoint"]) if t.get("endpoint") else t.get("endpoint"),
                              records=[nm_.ids.get(str(x), str(x)) for x in t["records"]] if isinstance(t.get("records"), list) else t.get("records"))
                         for t in derived_tiles]
    # Build the app_data JSON blob that the JS app will consume
    app_data = {
        "domain_label": normalized["domain_label"],
        "resources": resources,
        "zip_centroids": normalized["zip_centroids"],
        "filter_hints": normalized["filter_hints"],
        "zip_stats": normalized.get("zip_stats", []),
        "crisis_events": normalized.get("crisis_events", []),
        "curveball_alert": alert_html,
        "chart_fields": normalized.get("chart_fields", []),
        "area_label": normalized.get("area_label", "ZIP"),
        "domain_hints": normalized.get("domain_hints", {}),
        "system_snapshot": normalized.get("system_snapshot", {}),
        "snapshot_asof": snapshot_asof,          # P0.7 (D4): the snapshot tiles' label in Block 2 when its endpoint did not change
        "derived_tiles": derived_tiles,          # P0.7 (D4): the Oracle's derived figures beside the aggregate (never its update)
    }

    # P0.6 (D6): the contract's documented path for each endpoint (api.prefix + name); without a contract the endpoint's name
    doc = _contract_doc()
    pre = "/" + str(((doc or {}).get("api") or {}).get("prefix", "")).strip("/") if doc else ""
    data_sources = [dict(ds, endpoint=(f"{pre.rstrip('/')}/{ds.get('id', ds['endpoint'].strip('/'))}" if doc else ds["endpoint"]))
                    for ds in normalized["data_sources"]]
    # Build data inventory sentence (same pattern as Oracle)
    ep_count = len(data_sources)
    total_records = sum(ds["records"] for ds in data_sources)
    ep_parts = [f"{ds.get('id', ds['endpoint'])} ({ds['records']})" for ds in data_sources if ds['records'] > 0]
    ep_count_active = len(ep_parts)
    data_inventory = f"{ep_count_active} API endpoints ({total_records} records): {', '.join(ep_parts)}"
    # BUILD IC-4 (D3): the same counts as spans of the one registry (the data's: data.endpoint_count, data.record_count, <endpoint>.count)
    for ds in data_sources:
        ds["records_h"] = REG.span_if(reg, f"{ds.get('id', ds['endpoint'])}.count", str(ds["records"]))
    data_inventory_h = (f"{REG.span_if(reg, 'data.endpoint_count', str(ep_count_active))} API endpoints "
                        f"({REG.span_if(reg, 'data.record_count', str(total_records))} records): "
                        + ", ".join(f"{REG.esc(str(ds.get('id', ds['endpoint'])))} ({ds['records_h']})" for ds in data_sources if ds["records"] > 0))

    # Brand defaults — P0.6 (D3): the program identity the pipeline hands over (the Oracle's name and domain label)
    brand = brand_config or {}
    domain_label = REG.fit_text(brand.get("domain_label"), "oracle.domain_label") or normalized["domain_label"]   # BUILD IC-8 (D1)
    domain_label_source = "oracle" if brand.get("domain_label") else "rules"
    if domain_label != normalized["domain_label"]:
        app_data["domain_label"] = domain_label
        app_data["domain_hints"] = dict(_compute_domain_hints(domain_label, resources, {}, normalized.get("zip_stats"), normalized.get("area_label", "ZIP")),
                                        crisis_noun=normalized.get("domain_hints", {}).get("crisis_noun", "closures"))
    hints = app_data.get("domain_hints", {})
    program_name = brand.get("program_name") or f"{domain_label} Navigator"
    tagline = brand.get("tagline", "Find the help you need, near you")
    accent = brand.get("accent_hex", "#8b5cf6")
    try:   # P0.6 (D10): Chart.js 4.4.7 inlined when the vendored file is present and unchanged; else the CDN tag
        _cj = (VENDOR_DIR / CHARTJS_FILE).read_bytes()
        chartjs_inline = _cj.decode("utf-8") if hashlib.sha256(_cj).hexdigest() == CHARTJS_SHA256 else None
    except OSError:
        chartjs_inline = None
    if chartjs_inline is None:
        NR.say(f"  ⚠ vendor/{CHARTJS_FILE} is missing or changed — the page loads Chart.js from the CDN instead (needs WiFi; Rule 7)")

    # Generate QR code for mobile access (segno SVG — same library Oracle uses)
    deploy_url = os.environ.get("DEPLOY_URL", "")
    qr_svg = ""
    if deploy_url and HAS_SEGNO:
        try:
            qr = segno.make(deploy_url, error="m")
            buf = io.BytesIO()
            qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
            qr_svg = REG.inline_svg(buf.getvalue().decode("utf-8"))      # BUILD IC-4 (D7, D8): no XML declaration in the body
            # segno SVGs lack viewBox — add it so CSS can scale the SVG properly
            import re as _re
            _m = _re.search(r'width="(\d+)" height="(\d+)"', qr_svg)
            if _m:
                qr_svg = qr_svg.replace(
                    f'width="{_m.group(1)}" height="{_m.group(2)}"',
                    f'width="{_m.group(1)}" height="{_m.group(2)}" viewBox="0 0 {_m.group(1)} {_m.group(2)}"'
                )
            print(f"  ✓ QR code generated ({len(qr_svg)} bytes SVG)")
        except Exception as e:
            print(f"  ⚠ QR code generation failed: {e}")
            qr_svg = ""
    elif deploy_url:
        print(f"  ⚠ QR code skipped (install segno: pip install segno)")

    ctx = {
        "app_data_json": json.dumps(app_data, default=str).replace("<", JSON_LT),   # P0.7 (D11), B2 (review): no '</' or '<!--' in the block
        "app_data": json.loads(json.dumps(app_data, default=str)),     # BUILD IC-5 (D2 e): the object the template prints | tojson | built
        "program_name": program_name,
        # BUILD IC-8 (D7, Q-OG): the head's preview — a sentence Python writes from the engine's counts and the program's name
        "preview": REG.page_preview(f"{program_name} ranks the services in the published data ({len(resources)} in all) for each person's "
                                    "situation and shows how each rank was reached.")[0],
        "tagline": tagline,
        "accent_hex": (accent if isinstance(accent, str) and accent[:1] == "#" and len(accent) in (4, 5, 7, 9)
                       and all(ch_ in "0123456789abcdefABCDEF" for ch_ in accent[1:]) else "#8b5cf6"),   # BUILD IC-5 (D2 e): a colour, or the default
        "domain_label": domain_label,
        "domain_label_source": domain_label_source,  # P0.6 (D7): the pill's AI mark only when the Oracle's analysis named the domain
        "area_label": normalized.get("area_label", "ZIP"),
        "endpoints_fetched": sum(1 for ds in data_sources if ds.get("id") not in FETCH_ERRORS),   # P0.6 (D6): counted, never "8"
        "chartjs_inline": chartjs_inline,
        "resource_count": len(resources),
        "open_count": sum(1 for r in resources if r.get("status") != "closed"),
        "data_inventory": data_inventory,
        "data_inventory_h": data_inventory_h,                                        # BUILD IC-4 (D3)
        "ep_total_h": REG.span_if(reg, "data.endpoint_count", str(len(data_sources))),
        "records_total_h": REG.span_if(reg, "data.record_count", str(total_records)),
        "data_inventory_short": str(total_records),
        "total_records": total_records,
        "data_sources": data_sources,
        "zip_stats": normalized.get("zip_stats", []),
        "crisis_count": len(normalized.get("crisis_events", [])),
        "curveball_alert": alert_html,
        "curveball_text": curveball_text,
        "any_closed": any(r.get("status") == "closed" for r in resources),          # P0.7 (D6): "Show closed" whenever one is closed
        "closed_by_data": sum(1 for r in resources if r.get("closed_by") == "data"),
        "closed_by_announcement": sum(1 for r in resources if r.get("closed_by") == "announcement"),
        "cb_block": cb_block,                                                        # P0.7 (D7): "What changed at the curveball"
        "team_name": os.environ.get("TEAM_NAME", "Ctrl Alt Defeat"),
        "competition_label": NR.label_from_env(doc),   # P0.6 (D6): COMPETITION_LABEL, else the contract's, else "NAIPC"
        "chatbot_enabled": True,
        "deploy_url": deploy_url,
        "qr_svg": qr_svg,
        "domain_hints_persona": hints.get("persona", "Community members seeking resources and services."),
        "domain_hints_chatbot": hints.get("chatbot_example", "Which resources are nearby?"),
        "domain_hints_search": hints.get("search_placeholder", "Enter ZIP or keywords to search"),
        # Cross-track links (Full Stack integration — set ORACLE_URL and MUSE_URL env vars)
        "oracle_url": os.environ.get("ORACLE_URL", ""),
        "muse_url": os.environ.get("MUSE_URL", ""),
    }
    help_v, help_lines = help_view(raw_endpoints or {}, curveball_text, doc, reg)     # BUILD B2: the Get Help tab, or None (the lines say why)
    for ln in help_lines:
        NR.say(ln)
    ctx["help"] = help_v
    chat_text = architect_chat_text(app_data, changes, curveball_text, cb_block)
    if help_v:
        chat_text += "\n\n" + engine_chat_block(help_v)
    ctx.update(chat_fields("architect", chat_text, build_architect_faq(ctx)))
    present = None if help_v else [n for n, *_ in NR.SECTIONS["architect"] if n not in ("intake", "plan", "checklist", "navigator", "readiness")]
    if help_v:
        present = [n for n, *_ in NR.SECTIONS["architect"] if n not in ("checklist", "navigator", "readiness") or help_v["bundle"].get(n)]
    ctx["cov"] = NR.page_context("architect", present=present)  # P0.3 (D4, D8); B2: the panel links only the sections on this page; BUILD IC-5 (D1 e): Python's, never admitted
    REG.write_admit_findings(HARNESS_DIR / "checkpoints", NR.run_id())   # BUILD IC-5 (D1): every finding of this run written before the page
    return ctx


def render_html(ctx: Dict[str, Any]) -> str:
    """Render the Jinja2 template with the given context."""
    env = REG.page_env(Environment, FileSystemLoader(str(HARNESS_DIR)), authored=False, build=NR.BUILD_ID)  # IC-5 (D2 a); IC-8 (d16c)
    template = REG.page_template(env, "architect_template.html.j2")
    # BUILD IC-4 (D7): whose each region's numbers are — the page's line counts them by the table's category
    env.globals["PB"] = {"python": REG.PY_BLOCK, "design": REG.DESIGN_BLOCK, "org": REG.ORG_BLOCK}
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
    parser = argparse.ArgumentParser(description="Architect Track Harness")
    sub = parser.add_subparsers(dest="cmd")
    run_p = sub.add_parser("run")
    run_p.add_argument("--api-url", required=True, help="Base API URL")
    run_p.add_argument("--endpoints", required=True, help="Comma-separated endpoint names")
    run_p.add_argument("--curveball", default="", help="Curveball text")
    run_p.add_argument("--brand-config", default="", help="Path to brand_config.json from Oracle")
    run_p.add_argument("--curveball-json", default="", help="P0.7 (D7): the Oracle's checkpoints/curveball.json of this run")
    run_p.add_argument("--oracle-dir", default="", help="BUILD IC-4 (D3): the Oracle's checkpoints of this run (its registry, the one registry)")
    args = parser.parse_args()

    if args.cmd != "run":
        parser.print_help()
        return
    NR.paths_home(HARNESS_DIR / "checkpoints", "architect")       # BUILD IC-8 (D14, I8): this run's path markers

    t0 = time.time()
    console.print("[bold]═══ Architect Track Harness ═══[/bold]")
    NR.write_track_status(HARNESS_DIR / "checkpoints", "architect", "started", block=2 if args.curveball else 1)     # BUILD IC-6 (D7)

    # Step 1: Fetch data
    console.print("\n[bold cyan]Phase 1: Fetching API data[/bold cyan]")
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    raw = fetch_endpoints(args.api_url, endpoints)

    # Step 2: Normalize resources
    console.print("\n[bold cyan]Phase 2: Normalizing resources[/bold cyan]")
    normalized = normalize_resources(raw)
    console.print(f"  [green]✓ Domain: {normalized['domain_label']}[/green]")
    console.print(f"  [green]✓ Resources: {len(normalized['resources'])}[/green]")
    console.print(f"  [green]✓ ZIP centroids: {len(normalized['zip_centroids'])}[/green]")
    console.print(f"  [green]✓ Categories: {normalized['filter_hints']['categories']}[/green]")
    console.print(f"  [green]✓ Bool filters: {normalized['filter_hints']['bool_filters']}[/green]")
    console.print(f"  [green]✓ Enum filters: {list(normalized['filter_hints']['enum_filters'].keys())}[/green]")

    # Step 3: the program identity the pipeline hands over (P0.6, D3): read through the module's never-raise reader (utf-8-sig;
    # a BOM, a malformed file, another challenge or a missing name → the fallback name and a printed line, never a crash)
    brand = None
    if args.brand_config:
        ident, why = REG.admit(NR.read_identity(args.brand_config, os.environ.get("NAIPC_CHALLENGE", "").strip() or None), "saved", "identity")   # BUILD IC-5 (D1)
        if ident:
            brand = {"program_name": ident["program_name"], "brand_html": ident["brand_html"], "domain_label": ident["domain_label"]}
            NR.say(f"  Program identity: {ident['program_name']}" + (f" · domain {ident['domain_label']}" if ident.get("domain_label") else "") + f" ({ident.get('source') or '?'})")
        else:
            NR.say(f"  Program identity not used — {why}; the page names itself from its domain label")

    # P0.7 (D7): Block 2 reads this run's change list (NAIPC_CHANGES) and the Oracle's curveball.json of this run (--curveball-json)
    changes = cb_record = None
    if args.curveball:
        changes, why = REG.admit(NR.changes_from_env(), "data", "change list")   # BUILD IC-5 (D1)
        NR.say(f"  Change list: {len((changes or {}).get('endpoints') or {})} endpoint(s) compared, api_changed "
               f"{', '.join((changes or {}).get('api_changed') or []) or 'none'}" if changes else
               f"  ⚠ no change list for this run ({why}) — the page shows the announcement and its closures only")
        if args.curveball_json:
            cb_record, why = REG.admit(NR.read_curveball_json(args.curveball_json, os.environ.get("NAIPC_CHALLENGE", "").strip() or None,
                                                              NR.run_id()), "saved", NR.CURVEBALL_FILE)   # BUILD IC-5 (D1)
            NR.say(f"  Curveball record: update {cb_record.get('update')}, {len(cb_record.get('derived') or [])} derived figure(s)" if cb_record else
                   f"  Curveball record not used — {why}")

    # Step 4: Build context and render
    console.print("\n[bold cyan]Phase 3: Rendering interactive tool[/bold cyan]")
    reg, base_ids = None, set()    # BUILD IC-4 (D3): the Oracle's registry of this run with this run's data — the page's figures are its spans
    if args.oracle_dir:
        reg, rwhy = REG.read_oracle(args.oracle_dir, os.environ.get("NAIPC_CHALLENGE", "").strip() or None)
        if reg is not None:
            cdoc = _contract_doc() or {}
            reg.set_data(raw, NR.records)
            reg.set_facts(cdoc.get("authority"), cdoc.get("aliases"))
            base_ids = set(reg.entries)
        NR.say(f"  Registry: the Oracle's of this run ({len(base_ids)} figure(s)); the page's figures are its spans" if reg is not None else
               f"  Registry: not read ({rwhy}) — the page's figures are plain text")
    # BUILD IC-8 (P4.4, p44v): the run's names before any text is fitted; the page pass reads this object (P4.find-measure-class)
    names_ = REG.set_measure_names(REG.ReaderNames(raw, NR.records, before=getattr(reg, "_before_raw", None)) if isinstance(raw, dict) else None)
    ctx = build_context(normalized, curveball_text=args.curveball, brand_config=brand, changes=changes, cb_record=cb_record, raw_endpoints=raw,
                        reg=reg)
    NR.say(f"  Resources: {ctx['resource_count']} · open {ctx['open_count']} · closed per the data {ctx['closed_by_data']} · "
           f"closed by the announcement {ctx['closed_by_announcement']}")
    html, notes_ = REG.place_notes(render_html(ctx), reg)      # BUILD IC-6 (D9): each "sources disagree" a note after its sentence
    html, rw_ = REG.reader_page(html, names_)      # BUILD IC-8 (P4.4, p44v): the names the measure read
    if REG.reader_words_line(rw_):      # BUILD IC-8 (d12c): what a reader meets, in a reader's words — before the page's line
        NR.say(f"  {REG.reader_words_line(rw_)}")
    # BUILD IC-4 (D7): the page's integrity line, from its own text, the Oracle's registry of this run and the data's exempter
    html, pline = REG.place_page_line(html, reg, REG.Exempter.from_data(raw, NR.records) if isinstance(raw, dict) else None)
    if pline:
        NR.say(f"  {pline}")

    # Step 5: Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "index.html"
    if REG.notes_words(notes_):         # BUILD IC-6 (D9): the page's notes, said and recorded
        NR.say(f"  {REG.notes_words(notes_)}")
    nw_ = REG.write_page_notes(HARNESS_DIR / "checkpoints", notes_, NR.run_id())
    if nw_:
        NR.say(f"  ⚠ checkpoints/{REG.NOTES_FILE} not written ({nw_})")
    out_path.write_text(REG.strip_build_notes(html), encoding="utf-8")     # IC-4 Phase 7 (review c21): no build note in its scripts
    write_chat_context(OUTPUT_DIR, ctx)  # P0.11b: the proxy's context file, uploaded with the page
    console.print(f"  [green]✓ Written to {out_path} ({len(html):,} bytes)[/green]")
    NR.gate("architect", out_path, HARNESS_DIR / "coverage", fetch_errors=FETCH_ERRORS)  # P0.3 (D7): prints, never raises

    # Step 6: Write checkpoint for downstream tracks (Landing, Muse)
    checkpoint_dir = HARNESS_DIR / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "resource_count": ctx["resource_count"],
        "open_count": ctx["open_count"],
        "total_records": ctx["total_records"],
        "domain_label": ctx["domain_label"],
        "crisis_count": ctx["crisis_count"],
        "zip_count": len(normalized["zip_centroids"]),
        "area_count": len(normalized.get("zip_stats") or []),   # P0.6 review F4: the page's "Areas Monitored" (zip_stats), for the hub
        "category_count": len(normalized["filter_hints"]["categories"]),
        "categories": normalized["filter_hints"]["categories"],
        "data_sources": ctx["data_sources"],
        "curveball_applied": bool(args.curveball),
        "closed_by_data": ctx["closed_by_data"],                   # P0.7 (D6)
        "closed_by_announcement": ctx["closed_by_announcement"],   # P0.7 (D6)
        "program_name": ctx["program_name"],
        "domain_label_source": ctx["domain_label_source"],      # P0.6
        "endpoints_fetched": ctx["endpoints_fetched"],
        "run_id": NR.run_id(),
    }
    cp_path = checkpoint_dir / "architect_summary.json"
    cp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    console.print(f"  [green]✓ Checkpoint: {cp_path}[/green]")
    if reg is not None:            # BUILD IC-4 (D3): what this page registered beyond the Oracle's registry, for the pipeline's checks
        awhy = REG.write_added(checkpoint_dir / REG.ADDED_FILE, reg, base_ids, "architect")
        NR.say(f"  Registry: {len(set(reg.entries) - base_ids)} figure(s) this page added → checkpoints/{REG.ADDED_FILE}" if awhy is None else
               f"  ⚠ Registry: the page's additions were not written ({awhy})")

    content_ = finalize_architect(checkpoint_dir)          # BUILD IC-8 (D3, D6): one outcome per unit, counted into the status
    NR.write_track_status(checkpoint_dir, "architect", "built", page_run=NR.page_run_of(out_path), block=2 if args.curveball else 1,   # IC-6 (D7)
                          content=content_)
    elapsed = time.time() - t0
    console.print(f"\n[bold green]Done in {elapsed:.1f}s[/bold green]")

    # Auto-open browser (belt-and-suspenders with shell-level `start` command)
    import webbrowser
    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    try:
        main()
    except BaseException as _e:     # BUILD IC-6 (D7): the top-level handler — "failed" in the status file (best effort)
        NR.track_failed(HARNESS_DIR / "checkpoints", "architect", _e)
        raise
