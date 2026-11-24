#!/usr/bin/env python3
"""
Architect Track — Offline Test Runner
Generates a test interactive tool from embedded KC-like data.
Usage: python test_local.py
"""
import json
import os
import sys
from pathlib import Path

# Add harness dir to path
HARNESS_DIR = Path(__file__).parent
sys.path.insert(0, str(HARNESS_DIR))

from architect_harness import normalize_resources, build_context, render_html, OUTPUT_DIR, write_chat_context

# ─── Embedded test data (KC food-access endpoints) ────────────────────────────
TEST_DATA = {
    "pantries": {
        "source": "pantries",
        "count": 5,
        "data": [
            {
                "id": "p-001", "name": "Harvesters Community Food Network",
                "address": "3801 Topping Ave, Kansas City, MO 64129", "zip": "64129",
                "lat": 39.0654, "lng": -94.5368, "hours": "Mon-Fri 8am-4pm",
                "phone": "816-929-3000", "language": ["English", "Spanish"],
                "idRequired": False, "type": "food_bank",
                "monthlyCapacity": 2500000, "coldStorage": True
            },
            {
                "id": "p-002", "name": "Bishop Sullivan Center",
                "address": "3936 Troost Ave, Kansas City, MO 64110", "zip": "64110",
                "lat": 39.0402, "lng": -94.5712, "hours": "Mon,Wed,Fri 9am-12pm",
                "phone": "816-931-4751", "language": ["English", "Spanish"],
                "idRequired": False, "type": "pantry",
                "monthlyCapacity": 800, "coldStorage": False
            },
            {
                "id": "p-003", "name": "Guadalupe Centers - Food Pantry",
                "address": "1015 W Avenida Cesar E Chavez, Kansas City, MO 64108", "zip": "64108",
                "lat": 39.0833, "lng": -94.5867, "hours": "Tue,Thu 10am-2pm",
                "phone": "816-561-6885", "language": ["Spanish", "English"],
                "idRequired": False, "type": "pantry",
                "monthlyCapacity": 600, "coldStorage": True, "coldStorageCapacity": "fridge"
            },
            {
                "id": "p-004", "name": "Crosslines Community Outreach",
                "address": "736 Shawnee Ave, Kansas City, KS 66105", "zip": "66105",
                "lat": 39.1063, "lng": -94.6282, "hours": "Mon-Fri 9am-3pm",
                "phone": "913-281-3388", "language": ["English", "Spanish"],
                "idRequired": True, "type": "pantry",
                "monthlyCapacity": 900, "coldStorage": True, "coldStorageCapacity": "walk-in"
            },
            {
                "id": "p-005", "name": "El Centro Academy",
                "address": "650 Minnesota Ave, Kansas City, KS 66101", "zip": "66101",
                "lat": 39.1144, "lng": -94.6347, "hours": "Mon-Fri (school schedule)",
                "phone": "913-677-0100", "language": ["Spanish", "English"],
                "idRequired": False, "type": "school",
                "monthlyCapacity": 120, "coldStorage": False
            },
        ],
    },
    "transit": {
        "source": "transit",
        "count": 3,
        "data": [
            {"route": "39-Prospect", "stop": "Prospect & 31st", "zip": "64128",
             "lat": 39.0655, "lng": -94.5530, "frequency_min": 15,
             "nearest_pantry": "Bishop Sullivan Center", "walk_min": 4.1},
            {"route": "24-Independence", "stop": "Independence & Van Brunt", "zip": "64124",
             "lat": 39.0998, "lng": -94.5387, "frequency_min": 20,
             "nearest_pantry": "Harvesters", "walk_min": 6.2},
            {"route": "51-KCK/Minnesota", "stop": "Minnesota & 7th", "zip": "66101",
             "lat": 39.1144, "lng": -94.6350, "frequency_min": 20,
             "nearest_pantry": "El Centro Academy", "walk_min": 0.8},
        ],
    },
    "demographics": {
        "source": "demographics",
        "count": 4,
        "data": [
            {"zip": "66101", "population": 14200, "poverty_rate": 44.6,
             "hispanic_pct": 62.8, "no_vehicle_pct": 48, "snap_pct": 56,
             "food_insecurity_rate": 24.1},
            {"zip": "64127", "population": 12400, "poverty_rate": 41.3,
             "hispanic_pct": 28.6, "no_vehicle_pct": 45, "snap_pct": 52,
             "food_insecurity_rate": 21.4},
            {"zip": "66105", "population": 9200, "poverty_rate": 39.8,
             "hispanic_pct": 71.4, "no_vehicle_pct": 51, "snap_pct": 49,
             "food_insecurity_rate": 22.6},
            {"zip": "64130", "population": 18700, "poverty_rate": 38.2,
             "hispanic_pct": 12.4, "no_vehicle_pct": 38, "snap_pct": 42,
             "food_insecurity_rate": 18.2},
        ],
    },
    "store-closures": {
        "source": "store-closures",
        "count": 2,
        "data": [
            {"store": "Save-A-Lot", "zip": "64127", "closed": "Aug 2024",
             "impact": 12400, "sqft": 28000},
            {"store": "Price Chopper", "zip": "64130", "closed": "Nov 2024",
             "impact": 18700, "sqft": 42000},
        ],
    },
    # P0.7 (B8, §9): the live shape — an envelope {status, lastUpdated} and the records under "alerts" (the KC and Chicago APIs)
    "supply-alerts": {
        "status": "normal",
        "lastUpdated": "2026-03-28T09:00:00-05:00",
        "alerts": [
            {"id": "sa-001", "type": "tefap_cut", "severity": "high", "title": "TEFAP Federal Allocation Reduced 40%",
             "description": "40% TEFAP federal allocation cut effective March 2026", "date": "2026-03-01", "impactedZips": []},
        ],
    },
}


def run_test(curveball: str = ""):
    """Run the full Architect pipeline with test data."""
    print("═══ Architect Track — Test Runner ═══\n")

    # Phase 1: Normalize resources
    print("Phase 1: Normalizing resources...")
    normalized = normalize_resources(TEST_DATA)

    print(f"  Domain: {normalized['domain_label']}")
    print(f"  Resources: {len(normalized['resources'])}")
    print(f"  ZIP centroids: {len(normalized['zip_centroids'])}")
    print(f"  Categories: {normalized['filter_hints']['categories']}")
    print(f"  Bool filters: {normalized['filter_hints']['bool_filters']}")
    print(f"  Enum filters: {list(normalized['filter_hints']['enum_filters'].keys())}")

    # Show normalized resources
    print("\n  Normalized resources:")
    for r in normalized["resources"]:
        loc = r.get("location", {})
        print(f"    [{r['id'][:8]}] {r['name'][:40]:<40} | {r['category']:<15} | ZIP {loc.get('zip', '?')}")

    # Phase 2: Build context and render
    print("\nPhase 2: Building context...")
    ctx = build_context(normalized, curveball_text=curveball)

    print(f"  Program name: {ctx['program_name']}")
    print(f"  Resources: {ctx['resource_count']}")
    print(f"  Open: {ctx['open_count']}")
    print(f"  Data inventory: {ctx['data_inventory']}")
    if curveball:
        print(f"  Curveball: {curveball[:80]}...")

    # Phase 3: Render HTML
    print("\nPhase 3: Rendering HTML...")
    html = render_html(ctx)

    test_out = HARNESS_DIR / "test_output"  # P0.11 A8: never the real output/ (OUTPUT_DIR)
    test_out.mkdir(parents=True, exist_ok=True)
    out_path = test_out / "index.html"
    out_path.write_text(html, encoding="utf-8")
    ctx_file = write_chat_context(test_out, ctx)  # P0.11b: the proxy's context file beside the page
    print(f"  Written to {out_path} ({len(html):,} bytes)")

    # Verification checklist
    print("\n═══ Verification Checklist ═══")
    checks = [
        ("Resources embedded in HTML", '"resources"' in html),
        ("ZIP centroids embedded", '"zip_centroids"' in html),
        ("Search input present", 'id="q"' in html),
        ("Results container present", 'id="results"' in html),
        ("Detail panel present", 'id="det"' in html),
        ("Category chips container", 'id="cat-chips"' in html),
        ("Team name in footer", "Ctrl Alt Defeat" in html),
        ("Haversine function", "hav(" in html),
        ("Event delegation for cards", 'closest(".card")' in html),
        ("Empty state handling", "No resources match" in html),
        ("No API key in page", "sk-ant" not in html),
        ("Chat context file written", ctx_file is not None and ctx_file.exists()),
        ("Chat config in page", ctx["chat_context_id"] in html),
        # P0.7 (D5): the alert reaches the crisis events by shape, with its id — and never the finder
        ("Supply alert sa-001 is a crisis event", any(e.get("type") == "alert" and e.get("id") == "sa-001" for e in normalized["crisis_events"])),
        ("No alert in the finder", not any(r.get("source_endpoint") == "supply-alerts" for r in normalized["resources"])),
        ("Supply alerts counted (1 record)", next((d["records"] for d in normalized["data_sources"] if d["id"] == "supply-alerts"), None) == 1),
    ]
    if curveball:
        checks.append(("Curveball alert visible", 'class="alert"' in html and 'role="alert"' in html))
        checks.append(("CLOSED badge in data", '"closed"' in html))

    all_ok = True
    for label, ok in checks:
        status = "✓" if ok else "✗"
        if not ok:
            all_ok = False
        print(f"  [{status}] {label}")

    if all_ok:
        print("\n  ✅ All checks passed!")
    else:
        print("\n  ❌ Some checks failed — review output")

    print(f"\n  Open: file://{out_path.resolve()}")
    return all_ok


if __name__ == "__main__":
    curveball = ""
    if "--curveball" in sys.argv:
        idx = sys.argv.index("--curveball")
        if idx + 1 < len(sys.argv):
            curveball = sys.argv[idx + 1]

    sys.exit(0 if run_test(curveball=curveball) else 1)
