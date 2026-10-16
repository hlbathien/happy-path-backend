# Ticket 08: [Tracer Bullet] Sub-350ms Parallel Goong V2 Decoration, Per-Leg Clock Propagation & Live Mutation Parity

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 02, Ticket 05
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
1. Run `/v1/itineraries/plan` against a slow upstream server (`500ms` delay per request) and verify the **entire 3-candidate portfolio** falls back and completes in **`< 350ms` total wall-clock time** via parallel `asyncio.gather` + `asyncio.wait_for(..., timeout=0.28)` over a shared persistent `httpx.AsyncClient`.
2. Trigger a `2.0x` live congestion spike and verify that **every stop's `arrival_clock` and `departure_clock`**, `total_drive_minutes`, `total_duration_minutes`, and `tradeoff_summary_vi` update coherently.
3. Return a malformed upstream response (`{"overview_polyline": {"points": null}}`) and verify `encoded_polyline` preserves the valid non-null offline fallback string.
4. Execute `/v1/itineraries/mutate` when a Goong API key is active and verify the mutated candidate is also live-decorated.

## Root-Cause Failure Modes Eliminated
1. **Sequential Timeout Compounding (`3 x 390ms = 1.18s`)**: Decorating 3 candidates in a sequential `for` loop and constructing a new `httpx.AsyncClient` inside each call blew past the `<350ms` budget.
2. **Inconsistent Stop Clocks & Summary Under Congestion**: Updating `total_drive_minutes` without shifting `arrival_clock`/`departure_clock` and `tradeoff_summary_vi` left the itinerary internally inconsistent.
3. **Null Polyline Escape**: `r0["overview_polyline"].get("points", default)` returned `None` when `"points": null` was present in JSON.
4. **Mutation Losing Live Traffic State**: `/mutate` skipped `GoongClient.decorate_candidate_polyline`.

## Freeze-Grade Invariants (F8.1 – F8.4)
- **F8.1 (Persistent `httpx.AsyncClient` & Parallel `<280ms` Wall-Clock Budget)**: `GoongClient` reuses a persistent `httpx.AsyncClient` and decorates candidate lists concurrently via `asyncio.gather`, bounded by an outer `asyncio.wait_for(..., timeout=0.280)` so the entire decoration stage completes in `< 320ms` (and whole `/plan` response in `< 350ms`) even when upstream hangs.
- **F8.2 (Full Schedule Clock & Summary Re-Propagation on Live Legs)**: When Goong returns valid `legs` durations whose sum exceeds `1.35x` of `candidate.total_drive_minutes`, `GoongClient` recomputes each stop's `arrival_clock` and `departure_clock` sequentially using the live leg durations (preserving any opening-hour wait buffer), updates `total_drive_minutes` and `total_duration_minutes`, refreshes `tradeoff_summary_vi`, and appends the Vietnamese congestion warning.
- **F8.3 (Null-Safe Polyline Extraction)**: Only overwrite `candidate.encoded_polyline` if `points` is a `str` with `len(points.strip()) > 0`.
- **F8.4 (Live Decoration Parity on `/mutate`)**: `POST /v1/itineraries/mutate` invokes `decorate_candidate_polyline` on `mutated_candidate` before persisting `candidate_durations_json` and returning `MutateResponse`.

## Property-Based Acceptance & Regression Gates
1. **Real Delayed-Socket `<350ms` Wall-Clock Gate**: Against an async delay of `0.60s` per request across 3 candidates, assert `decorate_portfolio` returns in `< 0.34s` wall-clock time.
2. **Clock & Summary Coherence Gate**: On a `2.0x` congestion response, assert that `stops[0].arrival_clock`, `total_drive_minutes`, and `tradeoff_summary_vi` all update coherently.
3. **Null-Points Safety Gate**: Pass `{"routes": [{"overview_polyline": {"points": None}, "legs": []}]}` and assert `candidate.encoded_polyline` remains a non-empty `str`.
4. **Live Mutation Parity Gate**: Verify `/v1/itineraries/mutate` calls live Goong decoration and retains congestion warnings when `api_key` is configured.
