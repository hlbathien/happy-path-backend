# 02 — Freeze-Spec Architecture, Physical Graph & Pareto Solver (v3.0)

## 1. FUSE-Safe, Multi-Worker Durable SQLite Lifecycle
- `CorridorRegistry` probes `DB_PATH` on initialization using `BEGIN IMMEDIATE; ROLLBACK;` with `PRAGMA journal_mode = DELETE; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 5000;`.
- If the mounted filesystem supports standard POSIX/SQLite locking, `DB_PATH` is used directly in place.
- If the filesystem raises `sqlite3.OperationalError` (e.g., FUSE bind mount without lock support), `CorridorRegistry` synchronizes the seed DB to a deterministic shared file `/tmp/happy_path_shared_<md5(DB_PATH)>.db` (never a random directory deleted on exit), ensuring sessions persist across restarts and across all Uvicorn worker processes on the host.
- `scripts/seed_db.py` writes to `/tmp/happy_path_seed_staging_<pid>.db` and publishes atomically via `os.replace`.

## 2. Physical Interchange Graph & Piecewise Road-Spine Projection
- **Physical Interchange Law**: Every branch `B` in `corridor_branches` specifies `[diverge_km, merge_km]`. A sequence of POIs `seq` is branch-compatible if and only if:
  1. At most one non-`MAIN_SPINE` branch `B` appears in `seq`.
  2. If non-`MAIN_SPINE` branch `B` appears in `seq`, then for every stop `p in seq` with `p.branch_id == "MAIN_SPINE"`, `p.corridor_km <= B.diverge_km` or `p.corridor_km >= B.merge_km`.
  3. For `P1_EXPRESS`, only `MAIN_SPINE` stops are eligible unless a user explicitly locks a branch stop on `P2_ICONIC` or `P3_SCENIC`.
- **Piecewise Road-Spine Projection**: Each corridor stores ordered `spine_waypoints = [(lat, lng, km), ...]`. Any custom coordinate `(lat, lng)` is projected onto each consecutive road segment `(W_i, W_{i+1})` to find the minimum Haversine cross-track distance and interpolate the exact road kilometer `km`.
- **Road-Following Sliced Polyline**: Offline polylines begin at exact `origin_coords`, include intermediate `spine_waypoints` along each leg and branch, pass through each planned stop's coordinates, and terminate at exact `destination_coords`.

## 3. Schedule Validity, Bounded Wait Buffer & 5D Pareto Non-Dominance
- **Zero Closed-Stop Visits**: For every stop `p` in a candidate sequence, given arrival clock `arr_mod = arr_min % 1440`:
  - If `arr_mod < p.open_minute`:
    - If `p.open_minute - arr_mod <= max_wait_minutes` (`30` in Pass 1, `45` in Pass 2), `wait_minutes = p.open_minute - arr_mod` and `service_start_mod = p.open_minute`.
    - Otherwise, the stop is **infeasible** and rejected.
  - If `service_start_mod + p.dwell_minutes > p.close_minute`: the stop is **infeasible** and rejected.
- **Unconditional Midday UV Shield & Exertion Cap**:
  - Any stop with `midday_sun_trap == True` whose service window overlaps `[660, 870]` (`11:00–14:30`) is rejected in both Pass 1 and Pass 2 (unless `p.poi_id in locked_ids`, which emits `UV_LOCK_OVERRIDE_WARNING`).
  - Any sequence with `sum(1 for p in seq if p.exertion_level == "HIGH") > 1` is rejected in both Pass 1 and Pass 2.
- **5D Pareto Non-Dominance Filter**:
  - Define objective vector `V(c) = (-c.total_drive_minutes, -c.total_detour_minutes, c.aggregate_utility_score, c.aggregate_scenic_score, -c.hazard_risk_score)`.
  - Candidate `A` dominates `B` iff `all(a >= b for a, b in zip(V(A), V(B)))` and `any(a > b for a, b in zip(V(A), V(B)))`.
  - When constructing `[P1, P2, P3]`, candidates that are dominated by an already-chosen candidate are filtered out so the returned portfolio is guaranteed Pareto non-dominated.
