# Ctrl Alt Defeat: a provenance-first LLM pipeline for timed civic-data challenges

This pipeline turns a competition brief and a live JSON API into four published web pages, inside a timed competition block. The pages are a dashboard and business case, a working prototype, an outreach campaign and a hub. When the organizers change the data mid-day (the "curveball"), it compares the new data with the morning's snapshot. It then updates every page from that one list of changes.

It was built for the National AI Prompt Championship (NAIPC) by Shane Waldrop, working with Claude (Anthropic) in iterative build-and-test rounds. Its main example is the Wichita practice challenge, "No Wrong Door": a 90-day, $500,000 pilot to help people find the right door into Wichita's homeless services, including youth leaving foster care.

## The idea: Python counts, the model writes

Language models write fluent analysis, and they also invent, round and misquote numbers. This pipeline splits the work so that every number a judge reads has a known origin.

- **Python fetches and checks the data.** Every API record is fetched, kept raw and compared against later. Nothing a model wrote is used as a data source.
- **Every figure lives in a registry.** Each entry records its value, unit, kind (`MEASURED`, `DERIVED`, `EXTERNAL` or `MODELED`), source or formula, assumption, class, time window and cohort. A computed figure takes the kind of its weakest input.
- **The model cites figures by id instead of typing them.** It writes `[[es_free_beds]]`, and Python puts in the figure. A number the model proposes itself must be written as `[[model:<number>|<what it is>]]`. It is then registered and shown as `MODELED`. Quotes, comparisons and non-figures have their own token forms, and Python resolves or refuses each one.
- **Ranking is deterministic.** A rules engine ranks shelter beds, programs, front doors and homes for each person the brief names. Gates exclude options and published factors score them. Every outcome keeps its trace. The model may explain a ranking, but it cannot change it.
- **Every built page checks itself.** An integrity check reads the page as a person would and counts every number as cited, exempt or untraced. The page then publishes the result. A coverage check maps each deliverable in the brief to the page sections that answer it. A cross-page check flags any fact that two pages state differently.
- **The curveball is a diff, not a restart.** Block 1 snapshots every endpoint. Block 2 compares each endpoint with that snapshot, record by record, and every track updates from the one change list.
- **No page holds a key.** Each page has a chat assistant that talks to a small Cloudflare Worker. The page sends only a context id and the messages; the key lives in the Worker's secret.

## What is in this repository

| Path | What it is |
| --- | --- |
| `landing-harness/pipeline.py` | Runs the four tracks in order (Oracle, Architect, Muse, hub) and computes the Block 2 change list |
| `landing-harness/requirements/` | Challenge contracts: the brief as JSON (`<challenge>.json`), the curveball's announcement word for word (`.curveball.json`), the ranking engine's table (`.engine.json`) and the coverage map (`.map.json`) |
| `oracle-harness/` | The Oracle track: data loading, master analysis, financial model, diagrams, prioritization, deliverables, page assembly |
| `architect-harness/` | The Architect track: an intake flow, a personal plan, a documents checklist, a youth navigator and a housing readiness tracker. The ranking engine runs in the browser (`naipc_engine.js`) |
| `Muse-harness/` | The Muse track: audiences, channel plan, calendar, creative pieces and a bilingual flyer |
| `landing-harness/landing_harness.py` | The hub page; no model call |
| `naipc_registry.py`, `naipc_requirements.py`, `naipc_engine.py` | Shared modules: the registry, tokens and integrity check; the contract and coverage check; the ranking engine. The first two sit byte-identical in all four harness folders, the engine in the Oracle's and the Architect's |
| `worker/chat-proxy.mjs` | The Cloudflare Worker that proxies the pages' chat |
| `tools/fixture_server.py` | Replays recorded API data on 127.0.0.1, so a run needs no network access to the challenge API |
| `tools/fixtures/` | The recorded challenge data for Wichita, Kansas City and Chicago, before (`pre/`) and after (`post/`) each curveball |
| `tools/` (other files) | Static checks, page scans, deployment and run-check scripts |
| `examples/wichita-2026-04-18-block1/` | The four pages from a live Block 1 run on November 23, 2026 |
| `*/test_local.py` | Each harness's local tests |

The project's larger acceptance and regression suites ran in a separate build environment, and they are not part of this repository.

## Running it

You need Python 3.11 or later and an Anthropic API key, because the Oracle, Architect and Muse call the model. Then install the dependencies:

```
pip install anthropic jinja2 pydantic requests rich tenacity typer segno
npm install -g @mermaid-js/mermaid-cli    # optional: diagrams as inline SVG; without it the browser draws them
```

To rehearse the Wichita challenge with no access to the live challenge API, open one terminal at the repository root for the recorded data:

```
python -B tools/fixture_server.py --dir tools/fixtures/wichita-2026-04-18/pre
```

Then, in a second terminal at the repository root, run Block 1:

```
cd landing-harness
python -B pipeline.py --requirements requirements/wichita-2026-04-18.json --api-url http://127.0.0.1:8765/ ^
    --oracle-harness-dir ../oracle-harness --architect-harness-dir ../architect-harness --muse-harness-dir ../Muse-harness
```

(`^` continues a line in the Windows Command Prompt; use `\` in a Unix shell.)

For Block 2, restart the fixture server on `post/` and run the same command with `--curveball-file requirements/wichita-2026-04-18.curveball.json`. `--preflight-only` checks the setup without running anything.

The default folder paths in `pipeline.py` are the author's Windows layout, so pass the `--*-harness-dir` options elsewhere. Each harness writes its page to `output/index.html` in its own folder.

## The example pages

`examples/wichita-2026-04-18-block1/` holds the four pages of the live Block 1 run of November 23, 2026, before the curveball. Open `hub/index.html` first. The hub's links point to the published sites.

## Data and third-party code

The challenge data under `tools/fixtures/` belongs to the competition's organizers. It was served by the challenge API at aipromptchamp.com and is included so the pipeline can run offline. Kansas City and Chicago are earlier 2026 regional challenges.

`vendor/` holds Chart.js and Mermaid, both under the MIT license; see `THIRD_PARTY_NOTICES.md`.

Copyright © 2026 Shane Waldrop. All rights reserved.
