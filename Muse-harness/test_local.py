#!/usr/bin/env python3
"""Muse Track — Offline test runner with embedded sample marketing content.
Renders the template without API calls for rapid iteration."""

import io
import json
import os
import sys
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

try:
    import segno
    HAS_SEGNO = True
except ImportError:
    HAS_SEGNO = False

# ── Sample marketing content (KC Food Access domain) ──────────────────────

SAMPLE_DATA = {
    "program_name": "FoodBridge KC",
    "program_name_html": "Food<em>Bridge</em> KC",
    "tagline": "Connecting families to food — one ZIP at a time",
    "accent_hex": "#8b5cf6",
    "domain_label": "Food Access",
    "city_name": "Kansas City",
    "hero_title": "Reaching <em>47,300 Displaced Residents</em> Through Bilingual Outreach, Community Partners, and a Mobile-First Digital Tool",
    "hero_lead": "Four grocery closures and a federal supply cut have left Kansas City's highest-poverty corridors without nearby food access. This campaign connects displaced families to the resources they need — in English and Spanish, by phone, print, and screen.",
    "hero_stats": [
        {"value": "47,300", "label": "Residents Displaced"},
        {"value": "12,000", "label": "Year 1 Beneficiaries"},
        {"value": "$36", "label": "Cost Per Family"},
        {"value": "4.0:1", "label": "Social ROI"},
    ],
    "personas": [
        {
            "emoji": "👩‍👧‍👦",
            "name": "María García",
            "age": 34,
            "occupation": "Home health aide",
            "location": "ZIP 66105, KCK",
            "barriers": ["Spanish-speaking", "No vehicle", "No ID", "Lost Crosslines pantry"],
            "channels": ["WhatsApp", "Church bulletin", "Community health worker"],
            "quote": "Crosslines was the only place I could walk to with my kids and get help in Spanish. Now there's nothing."
        },
        {
            "emoji": "👴",
            "name": "James Washington",
            "age": 67,
            "occupation": "Retired steelworker",
            "location": "ZIP 64127, KCMO",
            "barriers": ["Fixed income", "Limited mobility", "No smartphone", "Diabetes diet needs"],
            "channels": ["Local radio", "Community center flyers", "311 phone line"],
            "quote": "Save-A-Lot closing meant I have to take two buses for groceries now. Some weeks I just don't go."
        },
        {
            "emoji": "👩‍🎓",
            "name": "Amina Hassan",
            "age": 28,
            "occupation": "Nursing student and mother",
            "location": "ZIP 64106, KCMO",
            "barriers": ["Somali-speaking", "Halal diet needs", "Transit-dependent", "Childcare constraints"],
            "channels": ["Instagram", "Della Lamb community center", "Mosque bulletin board"],
            "quote": "I need halal options my family can eat. The food bank gives us things we can't use."
        },
    ],
    "channel_plan": [
        {"channel": "Instagram / Facebook", "audience": "María, Amina (18-40)", "content_type": "Bilingual infographics + stories", "frequency": "3x/week", "kpi": "Impressions, QR scans", "reach": "15,000/mo"},
        {"channel": "WhatsApp Groups", "audience": "María (Spanish-speaking families)", "content_type": "Resource alerts in Spanish", "frequency": "2x/week", "kpi": "Group members, forwards", "reach": "2,500/mo"},
        {"channel": "Community Flyers", "audience": "James (seniors, no smartphone)", "content_type": "Bilingual print flyers at centers", "frequency": "Monthly refresh", "kpi": "QR scans, phone calls", "reach": "5,000/mo"},
        {"channel": "Local Radio (KPRS)", "audience": "James (seniors, commuters)", "content_type": "30-sec PSA in English", "frequency": "2x/day for 4 weeks", "kpi": "Call-in volume", "reach": "8,000/mo"},
        {"channel": "Partner Referrals", "audience": "All personas", "content_type": "CHW intake at partner sites", "frequency": "Ongoing", "kpi": "Referral enrollments", "reach": "3,000/mo"},
        {"channel": "Community Centers", "audience": "Amina (immigrant communities)", "content_type": "Multilingual posters + events", "frequency": "Bi-weekly events", "kpi": "Event attendance", "reach": "1,200/mo"},
    ],
    "calendar": [
        {
            "phase_name": "Partner Onboarding",
            "month_label": "Weeks 1–4",
            "milestones": [
                "Sign MOUs with 6 partner pantries",
                "Hire 3 bilingual CHWs",
                "Launch CRM intake dashboard",
                "Print 5,000 bilingual flyers",
                "Establish WhatsApp broadcast groups",
            ],
            "deliverables": ["Partner toolkit", "Brand guidelines", "Flyer templates"],
        },
        {
            "phase_name": "Soft Launch",
            "month_label": "Weeks 5–8",
            "milestones": [
                "Begin social media campaign (3x/week)",
                "Distribute flyers at community centers + churches",
                "Radio PSA campaign on KPRS",
                "First CHW-led community events at Della Lamb",
                "QR code tracking begins",
            ],
            "deliverables": ["Social media calendar", "Radio script", "Event signage"],
        },
        {
            "phase_name": "Full Campaign",
            "month_label": "Weeks 9–12",
            "milestones": [
                "Scale to all 12 monitored ZIPs",
                "Launch referral incentive program",
                "Partner appreciation event",
                "Mid-campaign metrics review",
                "Publish first community impact report",
            ],
            "deliverables": ["Impact report", "Partner newsletter", "Metrics dashboard"],
        },
    ],
    "social_posts": [
        {
            "platform": "Photo-Sharing Platform",
            "card_class": "photo",
            "location": "Kansas City, MO",
            "language": "English",
            "image_headline": "Find Food Near You",
            "image_desc": "Scan the QR code to search pantries by ZIP code, language, and hours — in English and Spanish.",
            "text": "🍎 4 grocery stores have closed in KC's highest-need neighborhoods. But help is here. FoodBridge KC connects you to 12 partner pantries, mobile food distributions, and SNAP enrollment — all in one tool.",
            "hashtags": ["#FoodBridgeKC", "#FoodAccess", "#KansasCity", "#CommunitySupport"],
            "likes": "1,204", "comments": "52", "shares": "318",
        },
        {
            "platform": "Microblog Platform",
            "card_class": "micro",
            "location": "Kansas City, KS",
            "language": "Bilingual EN/ES",
            "image_headline": "Encuentre comida cerca de usted",
            "image_desc": "Escanee el código QR para buscar despensas por código postal e idioma.",
            "text": "🚨 900 familias al mes perdieron acceso a Crosslines. Si necesita ayuda con alimentos en español, FoodBridge KC tiene 6 despensas con servicios bilingües. Llame al 211 o escanee nuestro código QR. | 900 families/month lost Crosslines access. FoodBridge KC has bilingual help.",
            "hashtags": ["#FoodBridgeKC", "#AyudaAlimentaria", "#KC", "#Comunidad"],
            "likes": "847", "comments": "93", "shares": "421",
        },
        {
            "platform": "Community Feed",
            "card_class": "feed",
            "location": "Kansas City Metro",
            "language": "English",
            "image_headline": "",
            "image_desc": "",
            "text": "📢 PARTNER SPOTLIGHT: Guadalupe Centers (64108) now offers walk-in SNAP enrollment through FoodBridge KC every Tuesday and Thursday, 10am–2pm. No ID required. Spanish and English. Share with someone who needs this. 💚",
            "hashtags": ["#FoodBridgeKC", "#SNAPenrollment", "#GuadalupeCenters", "#KCcommunity"],
            "likes": "2,341", "comments": "187", "shares": "892",
        },
    ],
    "flyer": {
        "headline_en": "Need Food? We Can Help.",
        "headline_es": "¿Necesita alimentos? Podemos ayudar.",
        "subheadline_en": "Free food assistance in English and Spanish — no ID required at most locations.",
        "subheadline_es": "Asistencia alimentaria gratuita en inglés y español — sin identificación en la mayoría de los sitios.",
        "body_en": "FoodBridge KC connects you to pantries, mobile food, and SNAP enrollment across 12 Kansas City ZIP codes. Our bilingual community health workers can help you find the right resource for your family.",
        "body_es": "FoodBridge KC le conecta con despensas, distribución móvil de alimentos e inscripción a SNAP en 12 códigos postales de Kansas City. Nuestros trabajadores comunitarios bilingües pueden ayudarle a encontrar el recurso adecuado para su familia.",
        "bullets_en": ["12 partner pantries with walk-in hours", "Mobile food distribution (Tue/Thu/Fri)", "Free SNAP enrollment assistance", "No ID required at most sites"],
        "bullets_es": ["12 despensas asociadas con horario sin cita", "Distribución móvil de alimentos (Mar/Jue/Vie)", "Ayuda gratuita para inscripción a SNAP", "Sin identificación en la mayoría de los sitios"],
        "cta_en": "Scan the QR code or call 211",
        "cta_es": "Escanee el código QR o llame al 211",
        "contact_info": "FoodBridge KC · 211 · foodbridgekc.org",
    },
    "partner_pitch": {
        "headline": "Join FoodBridge KC as a Distribution Partner",
        "problem": "Four grocery closures since August 2024 have displaced 46,400 residents across KC's highest-poverty corridors. The emergency shutdown of Crosslines Community Outreach has left 900 families per month without their primary food source. Remaining pantries report 15–20% capacity surges.",
        "solution": "FoodBridge KC deploys a coordinated navigation hub linking 12 partner pantries, mobile distribution routes, and SNAP enrollment into a single referral network. Our bilingual CHWs route families to the nearest accessible food source matched to their language and transit options.",
        "benefits": [
            "Real-time referral system routes clients to your site when you have capacity",
            "Shared intake data reduces duplicate enrollments across partners",
            "Bilingual CHWs handle intake at your location — no additional staffing needed",
            "Monthly supply coordination with Harvesters optimizes your delivery schedule",
            "Co-branded marketing materials drive awareness to your services",
            "Quarterly impact reports document your community reach for grant applications",
        ],
        "data_points": [
            {"stat": "47,300", "label": "Residents Displaced"},
            {"stat": "$500K", "label": "3-Year Contract"},
            {"stat": "12,000", "label": "Year 1 Beneficiaries"},
            {"stat": "4.0:1", "label": "Social ROI"},
        ],
    },
    "kpis": [
        {"emoji": "👁", "target": "100K", "metric": "Campaign Impressions", "timeframe": "Year 1"},
        {"emoji": "📱", "target": "8,500", "metric": "QR Code Scans", "timeframe": "Year 1"},
        {"emoji": "🤝", "target": "12", "metric": "Partner Organizations", "timeframe": "Month 3"},
        {"emoji": "👥", "target": "12,000", "metric": "Beneficiaries Enrolled", "timeframe": "Year 1"},
        {"emoji": "📞", "target": "3,200", "metric": "211 Call Referrals", "timeframe": "Year 1"},
        {"emoji": "💬", "target": "2,500", "metric": "WhatsApp Group Members", "timeframe": "Month 6"},
    ],
    "brand_handle": "@FoodBridgeKC",
    "architect_url": "https://ctrl-alt-defeat-arch.netlify.app",
    "oracle_url": "https://ctrl-alt-defeat.netlify.app",
    "persona_text": "Displaced residents navigating food access barriers, case workers coordinating referrals, community partners expanding their reach, and city planners tracking outreach effectiveness.",
    "data_inventory": "Data from 8 API endpoints (store-closures, pantries, demographics, transit, food-atlas, 311-calls, supply-alerts, harvest). Marketing content generated by Claude (Anthropic API) with Pydantic validation.",
    "team_name": "Ctrl Alt Defeat",
    "competition_label": "NAIPC Wichita Regional · April 18, 2026",
    "chatbot_enabled": True,  # P0.11b: chat through the proxy (fields from muse_harness in main)
}


def main():
    script_dir = Path(__file__).parent
    out_dir = script_dir / "test_output"  # P0.11 A8: never the real output/
    out_dir.mkdir(exist_ok=True)

    # Generate QR code for the Architect URL
    qr_svg = ""
    architect_url = SAMPLE_DATA.get("architect_url", "")
    if architect_url and HAS_SEGNO:
        try:
            qr = segno.make(architect_url, error="m")
            buf = io.BytesIO()
            qr.save(buf, kind="svg", scale=4, border=4, dark="#1a1a2e", light="#ffffff")
            qr_svg = buf.getvalue().decode("utf-8")
            import re
            m = re.search(r'width="(\d+)" height="(\d+)"', qr_svg)
            if m:
                qr_svg = qr_svg.replace(
                    f'width="{m.group(1)}" height="{m.group(2)}"',
                    f'width="{m.group(1)}" height="{m.group(2)}" viewBox="0 0 {m.group(1)} {m.group(2)}"'
                )
            print(f"  ✓ QR code generated ({len(qr_svg)} bytes SVG)")
        except Exception as e:
            print(f"  ⚠ QR code generation failed: {e}")

    # Build template context
    ctx = {**SAMPLE_DATA, "qr_svg": qr_svg}
    sys.path.insert(0, str(script_dir))
    from muse_harness import build_muse_chat, write_chat_context  # P0.11b chat fields (no key)
    ctx.update(build_muse_chat(ctx))

    # Render template
    env = Environment(loader=FileSystemLoader(str(script_dir)))
    tmpl = env.get_template("muse_template.html.j2")
    html = tmpl.render(**ctx)

    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    ctx_file = write_chat_context(out_dir, ctx)  # P0.11b: the proxy's context file beside the page
    print(f"\n  ✅ Muse playbook rendered: {out_path}")
    print(f"  Open: file:///{out_path.resolve()}")

    # ── Verification Checklist ──
    print("\n═══ Verification Checklist ═══")
    checks = [
        ("Program name in HTML", SAMPLE_DATA["program_name"] in html),
        ("Hero stats rendered", "47,300" in html and "12,000" in html),
        ("Personas rendered (3)", html.count("persona glass") >= 3),
        ("Channel matrix rows", html.count("<tr>") >= 7),
        # exact timeline classes (muse_template.html.j2:437): "phase is-open" first, then "phase"; 3 = the sample calendar
        ("Timeline phases (3)", html.count('class="phase is-open"') + html.count('class="phase"') == 3),
        ("Social posts rendered", html.count("social-card") >= 3),
        ("Flyer English content", "Need Food?" in html),
        ("Flyer Spanish content", "Necesita alimentos" in html),
        ("Partner pitch rendered", "Join FoodBridge" in html),
        ("KPI cards rendered", html.count("kpi-card") >= 4),
        ("Cross-track links", "oracle_url" not in html or "ctrl-alt-defeat" in html),
        ("AI Process section", "How This Campaign Was Built" in html),
        ("Team name in footer", "Ctrl Alt Defeat" in html),
        ("No API key in page", "sk-ant" not in html),
        ("Chat context file written", ctx_file is not None and ctx_file.exists()),
        ("Chat config in page", ctx["chat_context_id"] in html),
        ("QR code present" if qr_svg else "QR skipped (no segno)", bool(qr_svg) or not HAS_SEGNO),
    ]
    for label, ok in checks:
        print(f"  [{'✓' if ok else '✗'}] {label}")
    
    all_ok = all(ok for _, ok in checks)
    print(f"\n  {'✅ All checks passed!' if all_ok else '❌ Some checks failed!'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
