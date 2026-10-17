# AGENTS.md — Happy Path Autopilot Engine

## 1. Mission & Non-Negotiable Physical Trip Invariants

You are building and maintaining **Project Happy Path (Freeze-Spec v3.0)**, a zero-data-acquisition-cost ($0) backend engine for **VMAP (Tasco Mobility Ecosystem)** on **Ubuntu 22.04 LTS** (`OpenAPI 3.1.0`).
Every code change MUST preserve all 9 real-world road-trip physical invariants:

1. **F1 (Durable Shared Storage & Unified Bounds)**: `CorridorRegistry` uses a live `BEGIN IMMEDIATE; ROLLBACK;` lock probe (`PRAGMA journal_mode = DELETE; PRAGMA busy_timeout = 5000;`) on `DB_PATH`, falling back only on filesystem lock failure to a deterministic shared `/tmp/happy_path_shared_<hash>.db` that survives restarts and is shared across workers. `scripts/seed_db.py` publishes atomically via `os.replace`. `Coordinate` strictly enforces `lat in [8.0, 23.5]` and `lng in [102.0, 110.0]`.
2. **F2 (Zero Closed-Stop Visits Across 48 Hours & Bounded Early Wait)**: No solver pass may ever schedule a stop outside `[open_minute, close_minute - dwell_minutes]`. If a driver arrives up to `max_wait_minutes` (`30 min` in Pass 1; `45 min` in Pass 2) before `open_minute`, the driver waits (`wait_minutes = open_minute - arr_mod`, `service_start_clock = open_minute`, `departure_clock = open_minute + dwell_minutes`). Pass 2 relaxes **only** minimum stop count (`>= 1` stop for late-night 24/7 rest stops), **never** operating hours, UV shield, or exertion caps.
3. **F3 (Physical Interchange Topology & 5D Pareto Non-Dominance)**: Each branch defines `[diverge_km, merge_km]` in `corridor_branches`. An itinerary using alternative branch `B` may NEVER visit a `MAIN_SPINE` stop whose `corridor_km` lies strictly inside `(B.diverge_km, B.merge_km)`. Across `V(c) = (-total_drive_minutes, -total_detour_minutes, +aggregate_utility_score, +aggregate_scenic_score, -hazard_risk_score)`, no returned candidate may be Pareto-dominated by another returned candidate. If `<3` non-dominated candidates exist, emit `PORTFOLIO_SHORTFALL` in `warnings`.
4. **F4 (Unconditional Midday UV Shield, Multi-POI Exertion Cap & Arrival Anchor)**: Stops with `midday_sun_trap=True` are blocked during `[660, 870]` (`11:00–14:30`) in all passes unless explicitly locked in `locked_poi_ids` (which emits `UV_LOCK_OVERRIDE_WARNING`). At most 1 `HIGH`-exertion stop is permitted per itinerary. Morning trips arriving in `[510, 840]` (`08:30–14:00`) prioritize `is_arrival_anchor=True` stops (`+38.0` bonus).
5. **F5 (Unified `[15%, 80%]` Open EV Charger & Branch-Level VETC Tolls)**: For `powertrain == "ev"` on spans `>= 180.0 km`, at least one open stop with `has_ev_charge=True` must lie in `[active_start_km + 0.15 * active_span_km, active_start_km + 0.80 * active_span_km]`. VETC tolls are computed from `corridor_branches.toll_delta_vnd`.
6. **F6 (Piecewise Road-Spine Projection, Custom-Pin Polyline Slicing & `>=5 min` Leg Floor)**: Arbitrary GPS pins project onto piecewise `spine_waypoints`, slice polylines from exact `origin_coords` to `destination_coords`, enforce `>= 5 min` per driving leg, reject out-of-slice locks with `HTTP 409`, and fall back to a 0-stop direct route (`SPARSE_SLICE_DIRECT_DRIVE`) on short drivable slices (`>= 10 km`) with 0 open stops.
7. **F7 (Transactional Session Rollback on 409, No Silent Archetype Swap & True Duration Deltas)**: `/v1/itineraries/mutate` clones `SessionRecord` and commits to SQLite via `BEGIN IMMEDIATE` **only after** verifying the requested `candidate_id` is feasible. Infeasible mutations return `HTTP 409` without mutating stored session state or swapping `P1_EXPRESS` to `P2_ICONIC`. Stored `candidate_durations_json` tracks live-decorated durations for accurate `duration_delta_minutes`.
8. **F8 (`<350ms` Parallel Goong Decoration, Leg Clock Re-Propagation & Mutation Parity)**: `GoongClient` reuses a persistent `httpx.AsyncClient`, decorates candidates concurrently via `asyncio.gather` inside `asyncio.wait_for(..., timeout=0.280)`, propagates live leg durations into each stop's `arrival_clock`/`departure_clock` and `tradeoff_summary_vi`, guards against `points: null`, and decorates `/v1/itineraries/mutate` responses.
9. **F9 (NFC Normalization, Hardened Zero-PII Scrubber, Entity-Bound Scraping & Direct SQLite Promotion)**: All text is normalized to `NFC` before regex/token matching; contiguous phone labels (`SĐT0909...`), NBSP phones, group-member URLs, and Unicode emails are redacted; JSON-LD ratings bind to matching entity names; Goong predictions filter by commune concordance; and verified candidates promote directly into SQLite.

## 2. Repository Map

- `src/happy_path/main.py` — FastAPI application & lifespan management
- `src/happy_path/config.py` — Environment variables & timeout constants
- `src/happy_path/api/routes.py` — `/v1/itineraries/plan`, `/v1/itineraries/mutate`, `/v1/corridors/health`
- `src/happy_path/core/graph.py` — `CorridorRegistry`, lock-probed SQLite lifecycle, piecewise road-spine projection, polyline codec, atomic `SessionRecord` transactions
- `src/happy_path/core/solver.py` — Constrained Braided-DAG Solver, `[diverge_km, merge_km]` interchange gate, bounded wait-for-opening clock simulator, 5D Pareto non-dominance filter
- `src/happy_path/core/ingestion.py` — `NFC` normalizer, hardened Zero-PII scrubber, bimodal 1-star anti-shill gate, entity-bound JSON-LD & commune-aware Goong resolver, direct SQLite promoter
- `src/happy_path/clients/goong.py` — Persistent async Goong V2 client, parallel `<280ms` decoration, per-leg clock propagation
- `src/happy_path/schemas/itinerary.py` — Strict Pydantic contracts (`lat in [8.0, 23.5]`, `lng in [102.0, 110.0]`, action-parameter validators)
- `data/schema.sql` & `data/seed_corridors.json` — Relational DDL & 3-corridor dataset (25 POIs, 10 branches, piecewise spine waypoints, Markov pairs)
- `scripts/seed_db.py`, `scripts/export_openapi.py`, `scripts/scrape_corridors.py`, `scripts/harvest_goong_matrix.py`
- `tests/test_corridors_e2e.py` — Freeze-Grade Property-Based & Exhaustive Sweep Test Suite

## 3. Standard Commands (No Approval Needed for Local Disposable Runs)

- Seed database atomically: `make seed`
- Run full Freeze-Spec test suite: `make test`
- Export OpenAPI 3.1.0 contract: `make openapi`
- Start local server: `make run`

## 4. User instructions

The user authorizes appropriate public-artifact inspection/downloads, dependency installation, and code execution in this sandbox. Never send local credentials to third parties or downloaded code. Model `gpt-6-luna` has unlimited budget and can be accessed via `http://localhost:20128/v1`.

Operate autonomously and unblock yourself with reversible decisions. Inform me if you need any help that you can't do yourself like sudo commands, env vars, etc. Strictly prohibit assurance theatre and ceremonial safeguards: checks or procedures that appear rigorous but add no meaningful impact or engineering value while increasing cognitive, time, or token cost. Examples include unnecessary missingness reports, arbitrary random seeds, random gates, and redundant validation. Remove such measures when found and do not propagate them. Never cheat, make unacceptable shortcuts, bad practices like deterministic fallback when specs ask for working function, or make synthetic or mock data when asked for real data.

Use Crawl4AI + yt-dlp + Facebook CDP/GraphQL for scraping work. If not available in environment, conduct web search and install end-to-end. Do not use Tinyfish or skip using these.
