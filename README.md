# Happy Path Backend

Our backend turns a trip request into up to three clearly explained itineraries, using evidence-backed place data and practical rules to avoid closed stops, impossible detours, exhausting schedules, midday sun exposure, and poorly placed charging breaks. We preserve conflicting evidence rather than guessing, recheck feasibility when plans change, and show uncertainty instead of disguising estimates as facts—aiming for fewer surprises and calmer journeys, while keeping today’s unverified data clearly separate from real-world safety guarantees.

Our data describes where you can drive, where you can stop, and whether that stop fits your trip—not just whether it has good reviews.

- **Roads:** three corridors linking HCMC with Đà Lạt, Nha Trang, and Vũng Tàu, including alternative branches and toll estimates.
- **Places:** locations, opening hours, visit duration, detour time, charging facilities, exertion, and sun exposure.
- **Evidence:** source-linked reviews and observations, checked for the correct venue, freshness, conflicting claims, and suspicious ratings.

> **Today:** 25 places power the prototype; the broader research catalog contains 48 places and 88 observations.

---

## Getting Started

```bash
# Ubuntu 22.04 / Python 3.10; install the pinned environment manager
python3 -m pip install uv==0.12.21
make setup

# Configure environment variables (never commit credentials)
cp .env.example .env

# Run the local backend server (port 8000)
make run PYTHON=.venv/bin/python
```

`make run` uses Uvicorn's native `.env` loading before importing application settings. It seeds only a missing database, migrates the authoritative store without replacing it, and serves on port 8000. Exported environment variables override `.env`. A corrupt existing database fails startup rather than silently resetting. `GOONG_API_KEY` or `GOONG_REST_KEY` enables live decoration. Without a key, responses explicitly identify their offline road model.

### Quick API Check

```bash
# Health check
curl http://localhost:8000/v1/corridors/health

# Plan an itinerary
curl -X POST http://localhost:8000/v1/itineraries/plan \
  -H 'Content-Type: application/json' \
  -d '{"origin_query":"HCMC","destination_query":"Vung Tau","departure_iso":"06:00"}'
```

`POST /v1/itineraries/mutate` requires the returned `session_id`; unknown sessions return 404. Infeasible live schedules and conflicting cross-worker writes return 409 without overwriting prior state. Interactive API documentation is available at `/docs`, `/openapi.json`, and [`openapi.yaml`](openapi.yaml).

## Safe Operating Commands

```bash
make test PYTHON=.venv/bin/python       # Disposable DB; never resets working sessions
make openapi PYTHON=.venv/bin/python    # Export OpenAPI schema; no database initialization
make migrate PYTHON=.venv/bin/python    # Additive schema upgrade; honors HAPPY_PATH_DB_PATH
```

**`make seed` is an explicit destructive reset**, not a startup prerequisite. For an existing production DB, use `make migrate` instead.

## Reproducible Team Handoff

`uv.lock` pins the backend and optional scraping dependency graph with artifact hashes. Use **Python 3.10 on Ubuntu 22.04** and **uv 0.12.21**, then `make setup`; do not use an unlocked `pip install -e .` for reproduction. CI installs this lock, runs the full disposable-database suite, and checks the OpenAPI export. `Dockerfile` uses the same frozen lock.

```bash
make setup
make test
make openapi
git diff --exit-code -- openapi.yaml
```

The authoritative reproducible inputs are `data/schema.sql` and `data/seed_corridors.json`, together with the checked-in catalog, intake evidence, provenance, and source package. Runtime SQLite databases and user sessions are intentionally not distributed. Startup rebuilds a missing DB from the seed; existing deployments must migrate instead of resetting. `.env` and local browser/agent state are excluded; `.env.example` contains blank credential placeholders only.

**Scope:** reproduction means the locked software, checked-in data, offline planning rules, and test outcomes—not byte-identical HTTP responses. Session IDs and timings vary. Goong traffic, scraped pages, authenticated Facebook access, browser binaries, and future evidence change independently; live runs require team-provided credentials and are not frozen by this repository. Container OS packages and image tags are not an archival bit-for-bit build guarantee.

### Expanding to more Vietnam routes

The current dataset covers only HCMC–Đà Lạt, HCMC–Nha Trang, and HCMC–Vũng Tàu; it is not nationwide coverage. Add evidence-backed corridor spines, directional branches/interchanges, branch-bound toll plazas, and venue metadata to the seed/catalog intake. Follow [`docs/01_PRD_AND_CORRIDORS.md`](docs/01_PRD_AND_CORRIDORS.md), [`docs/02_ARCHITECTURE_AND_SOLVER.md`](docs/02_ARCHITECTURE_AND_SOLVER.md), and [`docs/03_SCRAPING_PLAYBOOK_2026.md`](docs/03_SCRAPING_PLAYBOOK_2026.md). Validate each new corridor against the nine invariants in `AGENTS.md`, rebuild a disposable database, and run `make test` before publishing. Research observations are not verified venue admissions or real-world safety guarantees.

## Data Intake & Provenance

```bash
PYTHONPATH=src .venv/bin/python scripts/merge_data_package.py happy-path-data-package.zip
```

The importer materializes the 48-POI / 36-directed-pair source-version union in `data/catalog_union.json`, preserves scrubbed provenance under `data/provenance/v5/`, and unions candidate observations into `data/ingestion_candidates.json`. It never trusts an `accepted` flag, treats a Google Place ID as a Goong resolution, or pads review evidence. Repeated identical observations do not enlarge the intake union. Every attempted POI promotion uses the atomic admission path; rejected observations remain available for correction.

Configured branch controls and branch-bound toll plazas are retained separately from verified venue admission. They are estimates from the supplied package, not independently established road geometry or tariffs. The orphaned Cà Ná plaza is excluded from active toll calculations until its branch is resolved. Historical flat corridor toll fields are not the slice-pricing authority.

For candidate scraping and collection:

```bash
uv sync --frozen --extra scraping --python 3.10
PYTHONPATH=src .venv/bin/python scripts/scrape_corridors.py --targets data/ingestion_targets.json --promote-to-db
```

The collector uses **Crawl4AI**, **yt-dlp**, and **Facebook CDP**. Set `FACEBOOK_CDP_URL` to an authenticated browser's CDP endpoint for Facebook evidence. Blocked/login-only sources are reported, never replaced with invented reviews. Acquiring data is not sufficient for admission: entity binding, commune concordance, dated review cohorts, low-review samples and physical metadata must all pass.

## Core Physical Trip Invariants

- Aware timestamps normalize to Vietnam UTC+7; naive timestamps and `HH:MM` are Vietnam local time.
- True `is_24h` venues may serve across midnight. Bounded opening waits and closing/UV checks also run after every complete live retiming.
- Arrival-anchor preference is scoped to departures 05:00–11:00 and arrivals 08:30–14:00; it is not mandatory overnight detouring.
- Pins over 30 km from the supported spine return 422. Access distances over 5 km add an explicit 45 km/h drive estimate to the first/last leg.
- Slice tolls sum only crossed, branch-compatible plazas. Missing branch road geometry and unavailable live data are warned about explicitly.
- EV stop placement is a constrained scheduling rule, **not** proof of real-time stall availability, connector compatibility or sufficient battery state of charge.
