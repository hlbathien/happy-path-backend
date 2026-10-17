# 02 — Architecture and Physical Contracts

This document specifies the system architecture, physical contracts, invariants, and operational boundaries for Happy Path.

## 1. One authoritative SQLite store

`CorridorRegistry` probes the configured file with `BEGIN IMMEDIATE; ROLLBACK;`, `journal_mode=DELETE`, `synchronous=NORMAL`, and a 15-second busy timeout. Ordinary busy/locked contention never creates another store. Only filesystem-lock failures select the deterministic shared `/tmp/happy_path_shared_<md5(path)>.db`; publication is serialized, and an existing shared store is not overwritten on restart.

Explicit seed resets compile into staging and publish with `os.replace`. Serving uses `scripts/run_backend.py`: Uvicorn loads `.env` before importing settings, only a missing DB is seeded, and `scripts/migrate_db.py` upgrades the authoritative store without replacing sessions, POIs or acquired evidence. A corrupt existing DB fails startup. Direct ASGI lifespan startup also fails on a missing DB. Tests compile into disposable databases; OpenAPI export does not seed or initialize a database.

Session mutations hold a keyed async lock through load, solving, live decoration and persistence. SQLite commits compare `trip_sessions.version` atomically. Another worker's newer commit causes a 409, not a lost update. Unknown sessions return 404. The requested archetype must remain feasible after live decoration before any constraint or duration baseline changes are committed.

## 2. Path, progress, access and tolls

Each alternative branch has a divergence/merge interval. A sequence uses at most one alternative branch, and cannot visit main-spine stops strictly inside that interval. P1 remains main-spine-only. Rendering applies the same exclusion to intermediate spine coordinates and inserts the selected branch's configured coordinate chain. Missing branch chains are warned about rather than fabricated. Provider polylines are not applied to alternative branches when the provider's road choice is unestablished.

Pins project onto the piecewise spine using locally longitude-scaled segments and Haversine access distance. Distances over 30 km return `422 CORRIDOR_ACCESS_OUT_OF_RANGE`. Access distances over 5 km and at most 30 km add `round(distance_km / 45 * 60)` estimated drive minutes to the first/last leg. Exact requested endpoints are retained. These access links and configured control chains are not independently acquired road geometry.

First/last slice legs use their own residual progress distances, not a whole-corridor scale factor. Each driving leg retains a five-minute floor. Out-of-slice locks return 409; a drivable sparse slice can return a zero-stop direct route unless locks or the long-span EV constraint prevent it.

Tolls sum only registered, branch-compatible plazas inside the active directional slice. Main-spine plazas inside an alternative's exclusion interval are omitted; other alternatives' plazas are never charged. Legacy scalar toll fields are retained as historical metadata, not combined with plaza sums. Supplied plaza fees remain estimates until independently corroborated.

## 3. The published schedule is the safety boundary

Aware departure timestamps normalize to Vietnam UTC+7. Naive timestamps and `HH:MM` are local time. For a non-24-hour venue, waiting is `max(0, open_minute - arrival_mod)` and may not exceed the active pass's limit. Service must finish by closing. Explicit `is_24h` venues can serve across midnight without an artificial 1440-minute closing ceiling.

Pass 1 requires at least two stops and permits at most 30 minutes of opening wait. Pass 2 relaxes only minimum stop count to one and waiting to 45 minutes. Neither pass relaxes closing, UV or exertion limits. Sun-trap service overlapping 11:00–14:30 is rejected unless explicitly locked, in which case an override warning is required. At most one HIGH-exertion stop is allowed.

The finite search considers one-to-three-stop sequences. Daytime departures 05:00–11:00 arriving 08:30–14:00 receive a finite +38 arrival-anchor preference; no infinite weight or mandatory overnight anchor is implied. EV spans at least 180 km require an open charging stop in the same [15%,80%] directional window. This is not a battery/connector/stall-availability guarantee.

Goong uses a persistent async client and parallel portfolio calls within a 280 ms outer decoration budget. Every complete positive finite live-leg set rederives waits and clocks, not only >1.35x congestion. The 1.35x threshold controls the advisory only. Infeasible results are dropped, or cause mutation 409 without commit. Timeouts and unavailable data explicitly retain offline estimates with warnings. Mixed timeout/rejection siblings cannot resurrect an invalid candidate.

Pareto objectives remain `(-drive, -detour, +utility, +scenic, -hazard)`. The finite offline frontier is filtered before archetype assignment, and the returned portfolio is filtered again after live retiming. Fewer than three survivors emit `PORTFOLIO_SHORTFALL`.

## 4. Evidence union is not automatic admission

The v5 import preserves source versions in `data/catalog_union.json`, scrubbed provenance in `data/provenance/v5/`, and candidate observations in `data/ingestion_candidates.json`. Google and Goong identities remain distinct. An imported accepted flag is not a verification record. Actual admission still requires NFC/PII handling, commune concordance, valid ratings, entity/source-bound dated review cohorts, genuine low-review evidence and physical metadata, followed by an atomic SQLite promotion.

The current catalog union has 48 logical POI IDs and 36 directed pairs. The active prototype seed operates with 25 places. Four alternative branches have configured chains; two are pending geometry. Thirteen plazas are branch-bound; the orphaned Cà Ná plaza remains in provenance until branch resolution.
