#!/usr/bin/env python3
"""Landing Page — Offline test runner with embedded sample data.

Usage:
    python test_local.py              # KC food access data (default)
    python test_local.py --chicago    # Chicago healthcare data
"""

import io
import re
import sys
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False

# Import routing from harness
try:
    from landing_harness import infer_diagram_routing, DIAGRAM_TRACKS
except ImportError:
    # Fallback if harness not importable — define inline
    def infer_diagram_routing(endpoints):
        result = []
        for ep in endpoints:
            tracks = ["oracle"]
            ep_lower = ep.lower().replace('-', '').replace('_', '')
            if ep_lower not in ("harvest", "supplyalerts"):
                tracks.append("architect")
            muse_kw = ("demograph", "closure", "displace", "alert", "supply", "311")
            if any(kw in ep_lower for kw in muse_kw):
                tracks.append("muse")
            result.append({"name": ep, "tracks": tracks})
        muse_count = sum(1 for r in result if "muse" in r["tracks"])
        if muse_count < 2:
            for r in result:
                if "muse" not in r["tracks"] and muse_count < 2:
                    r["tracks"].append("muse")
                    muse_count += 1
        return result

    DIAGRAM_TRACKS = {
        "oracle": {"label": "Oracle", "x": 260, "color": "#8b5cf6", "rgba": "139,92,246"},
        "architect": {"label": "Architect", "x": 520, "color": "#22c55e", "rgba": "34,197,94"},
        "muse": {"label": "Muse", "x": 780, "color": "#ec4899", "rgba": "236,72,153"},
    }


# ── KC food access sample ──
KC_ENDPOINTS = ["311-calls", "pantries", "food-atlas", "store-closures", "transit", "demographics", "supply-alerts", "harvest"]
KC_SAMPLE = {
    "program_name": "FoodBridge KC",
    "program_name_html": "Food<em>Bridge</em> KC",
    "tagline": "Connecting families to food — one ZIP at a time",
    "accent_hex": "#8b5cf6",
    "domain_label": "Food Security & Grocery Access",
    "oracle_url": "https://ctrl-alt-defeat.netlify.app",
    "architect_url": "https://ctrl-alt-defeat-arch.netlify.app",
    "muse_url": "https://ctrl-alt-defeat-muse.netlify.app",
    "contract": "$500K",
    "sroi": "4.0 : 1",
    "beneficiaries_y1": "12,000",
    "pitch": (
        "<strong>FoodBridge KC</strong> addresses a compounding food access crisis in Kansas City. "
        "Four grocery store closures and a 40% TEFAP federal supply cut have left 47,300 residents "
        "across 12 monitored ZIP codes without nearby food access. This submission presents three "
        "integrated artifacts — an investable business case (Oracle), a working resident navigation "
        "tool (Architect), and a bilingual community outreach campaign (Muse) — all built from the "
        "same live API data by a single competitor using Claude. The business case demonstrates a "
        "<strong>4.0 : 1</strong> social return on a <strong>$500K</strong> contract serving "
        "<strong>12,000</strong> beneficiaries in Year 1. Same data. Same brand. Same story."
    ),
    "competition_label": "NAIPC Wichita Regional · April 18, 2026",
}

# ── Chicago healthcare sample ──
CHI_ENDPOINTS = ["hospitals", "demographics", "clinics", "er-visits", "patient-displacement", "311-health", "supply-alerts"]
CHI_SAMPLE = {
    "program_name": "CareLink Chicago",
    "program_name_html": "Care<em>Link</em> Chicago",
    "tagline": "Your healthcare bridge when hospitals close",
    "accent_hex": "#8b5cf6",
    "domain_label": "Healthcare Access",
    "oracle_url": "https://ctrl-alt-defeat.netlify.app",
    "architect_url": "https://ctrl-alt-defeat-arch.netlify.app",
    "muse_url": "https://ctrl-alt-defeat-muse.netlify.app",
    "contract": "$750K",
    "sroi": "3.8 : 1",
    "beneficiaries_y1": "47,300",
    "pitch": (
        "<strong>CareLink Chicago</strong> addresses a compounding healthcare access crisis. "
        "Three hospital closures have displaced 82,900 patients across the South and West sides. "
        "This submission presents three integrated artifacts — an investable business case (Oracle), "
        "a working resident navigation tool (Architect), and a bilingual community outreach campaign "
        "(Muse) — all built from the same live API data by a single competitor using Claude. "
        "The business case demonstrates a <strong>3.8 : 1</strong> social return on a "
        "<strong>$750K</strong> contract serving <strong>47,300</strong> beneficiaries in Year 1. "
        "Same data. Same brand. Same story."
    ),
    "competition_label": "NAIPC Wichita Regional · April 18, 2026",
}


def main():
    # Choose dataset
    use_chicago = "--chicago" in sys.argv
    endpoints = CHI_ENDPOINTS if use_chicago else KC_ENDPOINTS
    sample = CHI_SAMPLE if use_chicago else KC_SAMPLE
    city_label = "Chicago (healthcare)" if use_chicago else "KC (food access)"
    print(f"\n  Dataset: {city_label}")

    script_dir = Path(__file__).parent
    out_dir = script_dir / "test_output"  # P0.11 A8: never the real output/
    out_dir.mkdir(exist_ok=True)

    # Generate QR code
    qr_svg = ""
    if HAS_SEGNO:
        try:
            qr = segno.make("https://ctrl-alt-defeat.netlify.app", error="m")
            buf = io.BytesIO()
            qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
            qr_svg = buf.getvalue().decode("utf-8")
            m = re.search(r'width="(\d+)" height="(\d+)"', qr_svg)
            if m:
                qr_svg = qr_svg.replace(
                    f'width="{m.group(1)}" height="{m.group(2)}"',
                    f'width="{m.group(1)}" height="{m.group(2)}" viewBox="0 0 {m.group(1)} {m.group(2)}"'
                )
            print("  ✓ QR code generated")
        except Exception as e:
            print(f"  ⚠ QR failed: {e}")

    # Build diagram routing
    diagram_endpoints = infer_diagram_routing(endpoints)
    ep_count = len(endpoints)
    total_connections = sum(len(e["tracks"]) for e in diagram_endpoints)
    print(f"  ✓ Diagram routing: {ep_count} endpoints, {total_connections} connections")

    ctx = {
        **sample,
        "qr_svg": qr_svg,
        "resource_count": str(ep_count),
        "zip_count": str(ep_count),
        "diagram_endpoints": diagram_endpoints,
        "diagram_tracks": DIAGRAM_TRACKS,
        "model_label": "claude opus 5.5",
    }
    env = Environment(loader=FileSystemLoader(str(script_dir)), autoescape=False)
    tmpl = env.get_template("landing_template.html.j2")
    html = tmpl.render(**ctx)

    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\n  ✅ Landing page rendered: {out_path} ({len(html):,} bytes)")

    # Verification
    print("\n=== Verification ===")
    checks = [
        ("Program name", sample["program_name"] in html),
        ("Oracle card", "Business Case" in html),
        ("Architect card", "Resident Tool" in html or "Resource Navigator" in html),
        ("Muse card", "Launch Campaign" in html or "Outreach Playbook" in html),
        ("SROI stat", sample["sroi"].split()[0] in html),
        ("Contract stat", sample["contract"] in html),
        ("Beneficiaries stat", sample["beneficiaries_y1"] in html),
        ("Cross-track URLs", "ctrl-alt-defeat-arch" in html),
        ("Venture pitch", "Same data" in html),
        ("Team name", "Ctrl Alt Defeat" in html),
        ("Competition label", "NAIPC Wichita" in html),
        ("QR code" if qr_svg else "QR skipped", bool(qr_svg) or not HAS_SEGNO),
        # Diagram checks
        (f"SVG diagram present", 'id="flow-svg"' in html),
        (f"{ep_count} tier1 paths", html.count('class="flow-path tier1"') == ep_count),
        (f"{total_connections} tier2 paths", html.count('class="flow-path tier2"') == total_connections),
        (f"{total_connections} dot groups", html.count('class="flow-dot"') == total_connections),
        (f"{ep_count} endpoint labels", html.count('class="ep-label"') == ep_count),
        (f"First endpoint label", f">{endpoints[0]}<" in html),
        (f"Last endpoint label", f">{endpoints[-1]}<" in html),
        ("3 track nodes", html.count('class="track-circle"') == 3),
        ("Synergy cards", html.count('class="syn-card') == 3),
        ("Boot terminal", 'id="terminal"' in html),
        ("Model label in boot line", "claude opus 5.5" in html and "opus 4.6" not in html),
        ("Particle canvas", 'id="particles"' in html),
    ]
    for label, ok in checks:
        print(f"  [{'✓' if ok else '✗'}] {label}")

    all_ok = all(ok for _, ok in checks)
    print(f"\n  {'✅ All checks passed!' if all_ok else '❌ Some checks failed!'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
