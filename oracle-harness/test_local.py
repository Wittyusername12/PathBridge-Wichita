#!/usr/bin/env python3
"""
Local test runner for the Oracle Harness.
==========================================
Tests the ENTIRE pipeline except live LLM calls by providing
realistic KC mock data. Verifies: financial computation,
Jinja2 rendering, chart data injection, and HTML output quality.

Usage:
    python test_local.py

Place this file in the same directory as oracle_harness.py and template.html.j2.
No API key or internet connection required.
"""
import sys, json, os, time
from pathlib import Path

# Ensure we can import the harness
sys.argv = ["test_local.py"]
os.environ["API_URL"] = "http://mock"  # Prevent missing env var errors

import oracle_harness as h

def main():
    # P0.11 A8: checkpoints/ and output/ are working-directory-relative (oracle_harness.py:42-43), so the test
    # runs in a sibling folder and never overwrites the real ones; the template path is harness-relative (41).
    test_dir = Path(__file__).resolve().parent / "test_run"
    test_dir.mkdir(exist_ok=True)
    os.chdir(test_dir)
    print("=" * 64)
    print("  ORACLE HARNESS — LOCAL TEST RUNNER (No API calls)")
    print("=" * 64)
    t0 = time.monotonic()

    # ── Step 1: Mock data_loading (simulates fetching KC endpoints) ──
    print("\n[1/4] Creating mock data_loading checkpoint...")
    mock_data_loading = {
        "raw": {},  # Would contain full JSON in a real run
        "summary": [
            {"endpoint": "311-calls", "source": "311-calls", "count": 13, "fields": ["zip", "category", "count"]},
            {"endpoint": "pantries", "source": "pantries", "count": 12, "fields": ["name", "zip", "coldStorage"]},
            {"endpoint": "food-atlas", "source": "food-atlas", "count": 12, "fields": ["censusTract", "foodDesert"]},
            {"endpoint": "store-closures", "source": "store-closures", "count": 4, "fields": ["name", "zip", "closedDate"]},
            {"endpoint": "transit", "source": "transit", "count": 8, "fields": ["route", "zip", "frequency"]},
            {"endpoint": "demographics", "source": "demographics", "count": 12, "fields": ["zip", "povertyRate", "noVehiclePct"]},
            {"endpoint": "supply-alerts", "source": "supply-alerts", "count": 2, "fields": ["type", "severity", "title"]},
            {"endpoint": "harvest", "source": "harvest", "count": 8, "fields": ["zip", "priority", "weeklyLbs"]},
        ]
    }
    h.PipelineRunner().save_checkpoint("data_loading", mock_data_loading)
    print("  ✓ 8 endpoints mocked")

    # ── Step 2: Mock master_analysis (simulates Claude Opus output) ──
    print("\n[2/4] Creating mock master_analysis checkpoint...")
    mock_analysis = {
        "domain": "food_access",
        "program_name": "FoodBridge KC",
        "brand_html": "Food<em>Bridge</em> KC",
        "city_name": "Kansas City",
        "badge_text": "Prepared for the City of Kansas City, MO",
        "hero_title": "Closing Kansas City's<br>Food Access Gap",
        "hero_lead": "A coordinated food distribution network connecting existing pantries, mobile distribution, and transit infrastructure to serve 12,400 families across 6 highest-need ZIP codes — within 60 days of contract award.",
        "summary_title": "A $500K Investment Closes the Food Gap for 12,400 Families and Returns $3.20 Per Dollar in Social Value",
        "bluf_text": "We recommend awarding a 12-month, $500,000 implementation contract to deploy FoodBridge KC — a coordinated food distribution network that closes the access gap created by 4 grocery store closures affecting 46,400 residents, leveraging Kansas City's existing pantry infrastructure and transit routes.",
        "decision_text": "DECISION REQUESTED: Approve a $500,000 service contract for 12 months, with 60-day performance review triggering continuation and annual renewal option based on demonstrated outcomes.",
        "crisis_title": "The Crisis",
        "crisis_body": (
            'Four grocery stores have closed since August 2024, displacing <span class="highlight">46,400 residents</span> '
            'in neighborhoods where <span class="highlight">41% lack vehicle access</span> and '
            '<span class="highlight">52% receive SNAP benefits</span>. Food insecurity in the target ZIPs averages '
            '<span class="highlight">18.2%</span> — significantly above both Missouri\'s statewide rate of '
            '<span class="highlight">~15%</span> (7th highest nationally) '
            'and the national rate of 13.5% [BENCHMARK: USDA ERS 2023]. '
            'The TEFAP federal allocation was cut 40% in March 2026, '
            'creating a <span class="highlight">3-million-pound shortfall</span> at Harvesters. '
            '311 food assistance calls have surged <span class="highlight">24% in ZIP 64130</span> and '
            '<span class="highlight">28% in ZIP 66101</span>. Nine of 12 census tracts in the target area are '
            'officially designated <span class="highlight">USDA food deserts</span> — the highest concentration '
            'in any Missouri metro area.'
        ),
        "crisis_human": (
            "Behind these numbers are working families earning under $23,000 — "
            "facing intersecting barriers that compound each other: income poverty (median $21,200 in ZIP 66101, "
            "vs. $37,000 citywide), transportation poverty (48% no vehicle — meaning a 2.8-mile trip to the nearest "
            "Aldi becomes a 45-minute bus ride with groceries), language barriers (62.8% Hispanic in 66101, "
            "Somali and Swahili-speaking refugee families at Della Lamb), and now food access poverty from the closures. "
            "Each barrier amplifies the others: without a car, you can't reach the distant grocery store; without English, "
            "you can't navigate the pantry intake form; without income, you can't afford the bus fare to get there."
        ),
        "crisis_sources": "Sources: store-closures (4 records), demographics (12 ZIPs), supply-alerts (2 alerts), 311-calls (13 ZIPs), food-atlas (12 tracts)",
        "solution_title": "Why FoodBridge KC",
        "solution_body": (
            'FoodBridge KC <strong>coordinates</strong> — not duplicates — existing resources. '
            'Unlike Harvesters (a food bank), After the Harvest (mobile distribution), or individual pantries (local service), '
            'FoodBridge is the <span class="highlight">connective tissue</span> that closes gaps between them: '
            'routing surplus from cold-storage hubs to transit-accessible pickup points, activating mobile routes in ZIPs '
            'where pantries lack cold storage, and providing <span class="highlight">bilingual intake in Spanish, Somali, '
            'and Swahili</span> for the 63% Hispanic population in ZIP 66101 and refugee communities served by Della Lamb.'
        ),
        "solution_population": (
            '<strong>Population served:</strong> Of 46,400 displaced residents (Total Population in Need), '
            '28,000 live in transit-accessible zones (Serviceable Population). This contract serves '
            '<span class="highlight">12,400 families</span> in Year 1 (Contracted Target Population) — '
            '44% of the serviceable population at $36 per family annually.'
        ),
        "cost_of_inaction": (
            "Without intervention, the City faces an estimated $2.1M in escalating emergency food assistance costs, "
            "increased 311 call center burden (up 24% already), and deepening health disparities in neighborhoods "
            "where food insecurity rates exceed 22%. Every month of delay extends the gap for the 15,000 pounds "
            "of food per week that ZIP 66101 alone requires."
        ),
        "comparison_rows": [
            {"dimension": "Service Coordination", "today": "12 pantries operating independently with no shared scheduling or routing", "proposed": "Centralized coordination hub managing distribution across all 12 sites with optimized routing"},
            {"dimension": "Transit Integration", "today": "No alignment between bus routes and food distribution schedules", "proposed": "8 transit-linked pickup points synchronized with bus schedules; mobile routes to 4 underserved ZIPs"},
            {"dimension": "Language Access", "today": "Only 5 of 12 pantries offer bilingual intake; zero Somali/Swahili capacity", "proposed": "Trilingual intake (Spanish, Somali, Swahili) at all primary sites via Della Lamb partnership"},
            {"dimension": "Fresh Food Capacity", "today": "7 of 12 pantries limited to shelf-stable items; no cold chain coordination", "proposed": "Cold storage network linking Crosslines and CAC walk-ins to mobile distribution for perishable delivery"},
            {"dimension": "Data &amp; Reporting", "today": "No unified tracking of families served, supply utilization, or geographic coverage", "proposed": "Real-time impact dashboard with weekly family counts, unit costs, and biweekly city council reporting"},
        ],
        "evidence_title": "Four Store Closures Displaced 46,400 Residents While Federal Cuts Removed 3 Million Pounds of Food Supply",
        "evidence_cards": [
            {
                "title": "Grocery Closures Created Critical Access Gaps",
                "stat_value": "46,400", "stat_class": "bad",
                "stat_label": "Residents lost neighborhood grocery access",
                "body": (
                    'Four closures since Aug 2024: Save-A-Lot (ZIP 64127, <span class="highlight">12,400</span> impacted), '
                    'Price Chopper (64130, <span class="highlight">18,700</span>), Aldi (64124, <span class="highlight">9,200</span>), '
                    'Dollar General Market (64128, <span class="highlight">6,100</span>). '
                    'This pattern mirrors grocery desertification documented in Detroit, Chicago South Side, and Baltimore — '
                    'where discount grocers exit neighborhoods once poverty exceeds 35%, creating cascading access failures.'
                ),
                "source": "Source: store-closures endpoint (4 records), demographics endpoint (12 ZIPs)"
            },
            {
                "title": "Only 5 of 12 Pantries Can Handle Fresh Food",
                "stat_value": "5 of 12", "stat_class": "warn",
                "stat_label": "Pantries have cold storage capacity",
                "body": (
                    'Only <span class="highlight">Harvesters</span> (industrial), '
                    '<span class="highlight">Community Assistance Council</span> (walk-in), '
                    '<span class="highlight">Crosslines</span> (walk-in), '
                    '<span class="highlight">Della Lamb</span> (small fridge), and '
                    '<span class="highlight">Guadalupe Centers</span> (small fridge) can handle perishables. '
                    'Fresh food access is critical because shelf-stable distributions alone fail to address '
                    'diet-related health disparities — diabetes rates in these ZIPs average 12.8%, above the CDC national benchmark of 11.6%.'
                ),
                "source": "Source: pantries endpoint (12 locations, coldStorage field)"
            },
            {
                "title": "Transit Gaps Block Access for 45% of Residents Without Cars",
                "stat_value": "45%", "stat_class": "bad",
                "stat_label": "No-vehicle rate in ZIP 64127",
                "body": (
                    'While the Troost MAX (Route 101) runs every <span class="highlight">10 min</span>, '
                    'other routes run every <span class="highlight">30 min</span>. ZIP 64134 requires an '
                    '<span class="highlight">8-minute walk</span> from the nearest bus stop. '
                    'When the nearest grocery alternative is 2.8 miles away, a 45-minute bus trip with perishable groceries '
                    'becomes a functional barrier — particularly for elderly residents and families with young children.'
                ),
                "source": "Source: transit endpoint (8 routes), demographics endpoint (noVehiclePct)"
            },
            {
                "title": "A 40% Federal Cut Removed 3 Million Pounds of Food from the Pipeline",
                "stat_value": "40%", "stat_class": "bad",
                "stat_label": "TEFAP allocation reduction (March 2026)",
                "body": (
                    'Harvesters reports a <span class="highlight">3-million-pound</span> quarterly shortfall. '
                    '311 food assistance calls are up <span class="highlight">24%</span> in ZIP 64130 and '
                    '<span class="highlight">28%</span> in ZIP 66101.'
                ),
                "source": "Source: supply-alerts endpoint (2 alerts), 311-calls endpoint (13 ZIPs)"
            },
        ],
        "operations_title": "A Five-Stage Distribution Network Leveraging Existing Pantry and Transit Infrastructure",
        "operations_description": (
            "End-to-end food distribution from sourcing through last-mile delivery and impact reporting. "
            "Each stage maps to existing infrastructure — no new facilities required. "
            "Mobile routes target After the Harvest's 8 priority ZIP areas where food rescue demand is highest."
        ),
        "plan_title": "Full Six-ZIP Coverage Is Achievable Within 60 Days of Contract Award",
        "financial_title": "The $500K Contract Achieves $36 Cost-Per-Family With Growing Surplus Through Revenue Diversification",
        "sensitivity_text": (
            "A 20% drop in enrollment increases cost-per-family to $45 (at city benchmark). "
            "A further 20% TEFAP reduction adds $35K in annual procurement costs, absorbable from the Year 1 surplus. "
            "The model remains viable across all three scenarios; the bear case is survivable, "
            "and the bull case requires strong execution plus favorable state funding conditions."
        ),
        "renewal_label": "60-Day Renewal Trigger",
        "renewal_text": (
            "At Day 60, present the city council with four metrics that justify 12-month continuation: "
            "families served vs. target (goal: 2,800+ by Day 60), cost per family vs. benchmark "
            "($36 vs. $45 national average), geographic coverage (6 of 6 ZIPs operational), "
            "and supply utilization rate (goal: 85%+)."
        ),
        "appendix_title": "Analysis Built on 8 Public Data Endpoints and Federal Cost Standards",
        "data_sources": [
            {"endpoint": "/api/311-calls", "returns": "311 service calls by area and category", "records": 13},
            {"endpoint": "/api/pantries", "returns": "Service locations, hours, capacity, languages", "records": 12},
            {"endpoint": "/api/food-atlas", "returns": "Census: poverty, income, demographics by area", "records": 12},
            {"endpoint": "/api/store-closures", "returns": "Facility/store closures with dates and impact", "records": 4},
            {"endpoint": "/api/transit", "returns": "Transit routes and stops near service points", "records": 8},
            {"endpoint": "/api/demographics", "returns": "Census: poverty, income, demographics by area", "records": 12},
            {"endpoint": "/api/supply-alerts", "returns": "Supply/service status and emergency alerts", "records": 2},
            {"endpoint": "/api/harvest", "returns": "Priority areas with delivery volumes", "records": 8},
        ],
        "data_inventory_sentence": "Analysis integrates 8 API endpoints (71 total records): 311-calls (13 records), pantries (12 records), food-atlas (12 records), store-closures (4 records), transit (8 records), demographics (12 records), supply-alerts (2 records), harvest (8 records).",
        "methodology": (
            "Analysis followed a three-phase approach: (1) exhaustive data ingestion across all 8 API endpoints "
            "with cross-referencing by ZIP code, (2) gap analysis comparing existing pantry capacity and transit "
            "accessibility against displaced population needs, and (3) bottom-up financial modeling using BLS wage "
            "data and TEFAP allowable cost categories (7 CFR 251.8) for line-item estimates. Financial projections "
            "were generated through iterative AI-assisted modeling with Python-verified arithmetic, cross-validated "
            "across three independent scenario runs."
        ),
        "ai_process": (
            "This analysis was generated using a multi-phase AI pipeline: data consolidation across 8 endpoints "
            "→ section-specific generation with cross-referential consistency checks → Python-verified arithmetic "
            "for all financial calculations. Every claim is traceable to a specific API endpoint. Financial projections "
            "were stress-tested through three independent scenario runs with conflicting assumption sets, "
            "and all numeric outputs were validated programmatically against source data before inclusion."
        ),
        "assumptions": [
            "City contract renews at $500K per year for 3 years based on 60-day performance review.",
            "Harvesters maintains current distribution capacity despite TEFAP cuts, supplemented by After the Harvest mobile and private donations.",
            "Personnel costs based on BLS median community health worker wage ($51,030) adjusted for Kansas City cost of living. Benefits at 30% of base salary.",
            "Cold storage partnerships (Crosslines, Community Assistance Council) continue at current capacity with existing MOUs.",
            "Indirect cost rate of 15% elected under 2 CFR 200.414 de minimis provision.",
            "Supplemental grant revenue (Years 2–3) based on comparable food access program funding rates in Missouri.",
        ],
        # === Financial assumptions for Python computation ===
        "contract_value": 500000,
        "contract_years": 3,
        "target_beneficiaries_y1": 12400,
        "growth_rate_y2": 0.22,
        "growth_rate_y3": 0.18,
        "staff_roles": [
            {"role": "Program Manager", "salary": 65000, "count": 1, "benefits_pct": 0.30},
            {"role": "Logistics Coordinator", "salary": 50000, "count": 1, "benefits_pct": 0.30},
            {"role": "Outreach Coordinator", "salary": 46000, "count": 1, "benefits_pct": 0.30},
            {"role": "Data Analyst (PT)", "salary": 30000, "count": 1, "benefits_pct": 0.0},
        ],
        "ops_cost_y1": 80000,
        "tech_cost_y1": 45000,
        "outreach_cost_y1": 20000,
        "supplemental_grants_y2": 75000,
        "supplemental_grants_y3": 125000,
        "earned_revenue_y3": 50000,
        "indirect_rate": 0.15,
        "sroi_outcomes": [
            {"category": "Reduced ER visits from food-related illness", "unit_value": 2200, "quantity": 250, "source": "AHRQ HCUP"},
            {"category": "Reduced emergency food bank calls", "unit_value": 50, "quantity": 6000, "source": "Feeding America"},
            {"category": "Improved child nutrition outcomes", "unit_value": 1200, "quantity": 500, "source": "USDA ERS"},
            {"category": "Avoided SNAP emergency supplement costs", "unit_value": 180, "quantity": 2500, "source": "USDA FNS"},
            {"category": "Improved workforce participation", "unit_value": 750, "quantity": 600, "source": "Feeding America/Gallup"},
        ],
        "deadweight": 0.15,
        "attribution": 0.10,
        "displacement": 0.05,
        "drop_off_rate": 0.10,
        "discount_rate": 0.035,
        # === Mermaid diagram ===
        "mermaid_code": (
            'flowchart TD\n'
            '    SRC["Harvesters + TEFAP + Food Rescue Network"] --> CHK{"Supply Sufficient?"}\n'
            '    CHK -->|Yes| HUB["Cold Storage: Crosslines + CAC"]\n'
            '    CHK -->|Shortfall| MOB["Supplement: After the Harvest + Corporate Food Rescue"]\n'
            '    MOB --> HUB\n'
            '    HUB --> NET{"Distribution Network"}\n'
            '    NET --> FIX["12 Fixed Pantry Sites in 6 ZIPs"]\n'
            '    NET --> ROUTE["Mobile Routes: 4 Underserved ZIPs"]\n'
            '    NET --> BUS["Transit-Linked Pickup: 8 Bus Stops"]\n'
            '    FIX --> TRACK["Family Intake + Impact Tracking"]\n'
            '    ROUTE --> TRACK\n'
            '    BUS --> TRACK\n'
            '    TRACK --> RPT["Monthly Impact Report to City"]\n'
            '\n'
            '    classDef source fill:#2d1a6e,stroke:#9b6dff,color:#e9ecff\n'
            '    classDef hub fill:#0f3d55,stroke:#40e0f0,color:#e9ecff\n'
            '    classDef dist fill:#17452a,stroke:#4eeab5,color:#e9ecff\n'
            '    classDef track fill:#453410,stroke:#fcd34d,color:#e9ecff\n'
            '    classDef report fill:#2d1940,stroke:#c4b5fd,color:#e9ecff\n'
            '    class SRC,MOB source\n'
            '    class CHK,HUB hub\n'
            '    class NET,FIX,ROUTE,BUS dist\n'
            '    class TRACK track\n'
            '    class RPT report'
        ),
        # === Implementation phases ===
        "phases": [
            {
                "summary": "Phase 1: Foundation — Days 1 to 20",
                "content": (
                    "<p><strong>Staffing:</strong> Hire Program Manager ($65K), Logistics Coordinator ($50K), "
                    "Outreach Coordinator ($46K), and part-time Data and Reporting Analyst ($30K). "
                    "FT rates based on BLS median community health worker wage of $51,030 adjusted for role. "
                    "Benefits at 30% of FT base salary. Total personnel Year 1: $240K.</p>"
                    "<p><strong>Infrastructure:</strong> Execute cold-storage MOUs with Crosslines and Community Assistance Council. "
                    "Deploy tracking system (intake forms, delivery logs, impact dashboard).</p>"
                    "<p><strong>Partnerships:</strong> Formalize agreements with Harvesters, After the Harvest, and 6 priority pantry sites.</p>"
                ),
                "timeline_label": "Timeline: Days 1\u201320",
                "timeline_suffix": "of 60-day plan",
                "meter_pct": 33
            },
            {
                "summary": "Phase 2: Launch — Days 21 to 40",
                "content": (
                    "<p><strong>Distribution:</strong> Begin weekly distribution at top 3 ZIPs (66101, 64127, 64130). "
                    "Activate After the Harvest mobile routes (Tue/Thu: 64127, 64130; Wed/Fri: 66101, 66105).</p>"
                    "<p><strong>Outreach:</strong> Launch bilingual awareness campaign — flyers at transit stops, "
                    "partnerships with churches and schools, direct outreach through Della Lamb.</p>"
                    "<p><strong>Measurement:</strong> Begin weekly family-served counts and unit-cost tracking. "
                    "First biweekly report to city at Day 30.</p>"
                ),
                "timeline_label": "Timeline: Days 21\u201340",
                "timeline_suffix": "of 60-day plan",
                "meter_pct": 66
            },
            {
                "summary": "Phase 3: Scale — Days 41 to 60",
                "content": (
                    "<p><strong>Expansion:</strong> Scale to all 6 ZIPs. Onboard remaining pantry partners. "
                    "Add weekend distribution at Bishop Sullivan Center.</p>"
                    "<p><strong>Reporting:</strong> Compile 60-day impact report: families served, cost per family, "
                    "geographic coverage, supply utilization rate. Present findings to city council committee.</p>"
                    "<p><strong>Sustainability:</strong> Identify Year 2 supplemental funding targets "
                    "(state grants, corporate sponsorship, USDA Community Food Projects).</p>"
                ),
                "timeline_label": "Timeline: Days 41\u201360",
                "timeline_suffix": "of 60-day plan",
                "meter_pct": 100
            },
        ],
        "risks": [
            {"risk": "Staff turnover (CHW turnover averages 26%/year nationally)", "likelihood": "Medium", "impact": "High",
             "mitigation": "Cross-train all roles; maintain active candidate pipeline; competitive benefits at 30% of base"},
            {"risk": "Cold storage partner dropout (Crosslines or CAC withdraws)", "likelihood": "Low", "impact": "High",
             "mitigation": "MOUs with 90-day termination notice; backup agreement with Harvesters industrial facility"},
            {"risk": "Further TEFAP reductions beyond 40%", "likelihood": "Medium", "impact": "Medium",
             "mitigation": "Diversify sourcing: corporate food rescue (Trader Joe's, Costco), USDA CSFP, private donation drives"},
            {"risk": "Low enrollment in first 30 days", "likelihood": "Medium", "impact": "Medium",
             "mitigation": "Bilingual outreach through Guadalupe Centers, El Centro, and Della Lamb; 311 call center awareness training"},
        ],
    }
    h.PipelineRunner().save_checkpoint("master_analysis", mock_analysis)
    print("  ✓ KC-realistic master analysis mocked (all narrative + financial assumptions)")

    # ── Step 3: Run the REAL financial model computation ──
    print("\n[3/4] Running financial model (Python computation)...")
    fin_fn = h._STAGE_REGISTRY["financial_model"]["fn"]
    fin = fin_fn({"master_analysis": mock_analysis})
    h.PipelineRunner().save_checkpoint("financial_model", fin)

    # Print key financial outputs for verification
    kpis = fin["kpis"]
    print(f"  ✓ Cost per beneficiary: ${kpis['cpb_y1']}")
    print(f"  ✓ SROI ratio: {kpis['sroi']}")
    print(f"  ✓ Beneficiaries Y1: {kpis['beneficiaries_y1']:,}")

    # Verify math
    base_pl = fin["scenarios"]["base"]["pl_rows"]
    revenue_row = [r for r in base_pl if r["label"] == "Total Revenue"][0]
    expense_items = [r for r in base_pl if r["label"] in ["Personnel","Ops & Transport","Technology","Outreach","Indirect (15%)"]]
    surplus_row = [r for r in base_pl if r["label"] == "Surplus"][0]

    print(f"  ✓ Base P&L: Revenue {revenue_row['cells']}")
    print(f"  ✓ Base P&L: Surplus {surplus_row['cells']}")
    print(f"  ✓ Charts: {len(fin['chart_data'])} scenarios × {len(fin['chart_data']['base']['datasets'])} datasets")

    # ── Step 4: Render final HTML ──
    print("\n[4/4] Rendering HTML via Jinja2...")

    # Mock diagram — use CDN fallback (Mermaid code, no SVG)
    diag = {"svg": None, "mermaid": mock_analysis["mermaid_code"]}
    h.PipelineRunner().save_checkpoint("diagram_generation", diag)

    results = {
        "data_loading": mock_data_loading,
        "master_analysis": mock_analysis,
        "financial_model": fin,
        "diagram_generation": diag,
    }
    ctx = h.build_template_context(results)

    # Add scenario descriptions from the master analysis
    for key in ["base", "bull", "bear"]:
        if key in ctx.get("scenarios", {}):
            if key == "base":
                ctx["scenarios"][key]["description"] = "Revenue diversification begins Year 2 with supplemental grants; surplus grows as unit costs decline with scale."
            elif key == "bull":
                ctx["scenarios"][key]["description"] = "Assumes $150K state supplemental grant from Year 1, two additional cold-storage partnerships reducing ops costs, and 90% staff retention."
            elif key == "bear":
                ctx["scenarios"][key]["description"] = "Assumes no supplemental funding, continued TEFAP reductions, service reduced to 4 ZIPs, and 65% staff retention."

    path = h.render_html(ctx)
    html = path.read_text(encoding="utf-8")
    if "sk-ant" in html:  # P0.11 T6: no key in the page, by design
        print("  ❌ FAIL: the rendered page contains 'sk-ant'")
        sys.exit(1)
    print("  ✓ No API key in page")

    elapsed = time.monotonic() - t0
    print(f"\n{'='*64}")
    print(f"  ✅ DONE in {elapsed:.1f}s")
    print(f"  Output: {path} ({len(html)/1024:.1f} KB, {len(html.splitlines())} lines)")
    print(f"{'='*64}")
    print(f"\n  Open this file in your browser to verify:")
    print(f"    {path.resolve()}")
    print(f"\n  CHECKLIST:")
    print(f"    □ Dark theme loads with glassmorphism cards")
    print(f"    □ KPI counters animate on scroll (12.4K, $36, 3.2:1, 60 Days)")
    print(f"    □ Executive Summary has BLUF + Crisis/Solution grid + Cost of Inaction")
    print(f"    □ 4 evidence cards with colored stats (red/amber)")
    print(f"    □ Mermaid diagram renders (may take a second to load from CDN)")
    print(f"    □ Financial tabs work: Base Case / Optimistic / Conservative")
    print(f"    □ Charts show stacked bars + revenue line with hover tooltips")
    print(f"    □ P&L tables have correct numbers (Surplus should be positive)")
    print(f"    □ Implementation accordion: 3 phases with timeline meters")
    print(f"    □ Risk table: 4 rows with likelihood/impact/mitigation")
    print(f"    □ 60-Day Renewal Trigger box")
    print(f"    □ Appendix: Data Sources (8), Methodology, AI Process, Assumptions (6)")
    print(f"    □ Footer: 'Ctrl Alt Defeat' + the competition label ('NAIPC' without a contract — P0.6 D6)")


if __name__ == "__main__":
    main()
