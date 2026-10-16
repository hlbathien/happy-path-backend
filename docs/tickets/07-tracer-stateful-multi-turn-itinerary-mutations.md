# Ticket 07: [Tracer Bullet] Atomic Transactional Session Mutations, Strict Action Validation & True Live-Traffic Delta Tracking

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 01, Ticket 03, Ticket 04
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
1. Execute multi-turn voice mutations (`REMOVE_STOP`, `LOCK_STOP`, `SKIP_CATEGORY`, `SHIFT_DEPARTURE`) and verify **transactional atomicity**: if a user attempts an infeasible mutation (such as locking two incompatible branches or two `HIGH`-exertion hikes), the API returns `HTTP 409 Conflict` and **rolls back/preserves the session's prior valid state** so the very next valid mutation succeeds (`HTTP 200`).
2. Attempt to lock a non-main branch stop on `P1_EXPRESS` and receive a clean `HTTP 409` instead of silently substituting `P2_ICONIC`.
3. Send malformed mutation payloads (missing `target_poi_id`, invalid category `"FOOD"`, or invalid date `"not-a-date"`) and receive deterministic `HTTP 422` validation errors.
4. Mutate a plan that was previously decorated with live Goong traffic and observe that `delta_summary_vi` reports the exact duration delta against the **stored live-decorated duration**, not a recomputed offline duration.

## Root-Cause Failure Modes Eliminated
1. **Poisoned Session State After Rejected 409 Mutations**: Writing `session.locked_poi_ids` to SQLite *before* checking solver feasibility poisoned the session permanently whenever a mutation failed.
2. **Silent Archetype Substitution (`P1_EXPRESS` -> `P2_ICONIC`)**: Falling back to `candidates[0]` when the requested `candidate_id` was infeasible silently switched the user's archetype.
3. **Live-Traffic Delta Amnesia**: Recomputing the baseline offline ignored Goong congestion adjustments from `/plan`, producing `-9,644 min` vs `+1 min` delta errors.
4. **Silent Acceptance of Invalid Action Parameters**: Accepting `"not-a-date"` or unknown categories with `HTTP 200`.

## Freeze-Grade Invariants (F7.1 – F7.4)
- **F7.1 (Atomic Copy-on-Write Session Mutation)**: In `POST /v1/itineraries/mutate`, clone the loaded `SessionRecord` in memory, apply the requested mutation to the clone, and run the solver. Only if the requested `payload.candidate_id` is feasible and present in the solved portfolio does the handler commit the updated `SessionRecord` to SQLite inside an atomic `BEGIN IMMEDIATE` transaction. If infeasible, SQLite is untouched and `HTTP 409` is returned.
- **F7.2 (No Silent Archetype Substitution)**: If `payload.candidate_id` cannot be satisfied under the mutated constraints (e.g., locking `BRANCH_LONGSON_ISLAND` while requesting `P1_EXPRESS`, which requires `MAIN_SPINE`), return `HTTP 409 Conflict` with a clear Vietnamese explanation rather than returning `P2_ICONIC`.
- **F7.3 (Strict Action-Parameter Schema Validation)**:
  - `REMOVE_STOP` / `LOCK_STOP`: `target_poi_id` must be non-empty and exist in the active corridor (`HTTP 422` otherwise).
  - `SKIP_CATEGORY`: `target_category` must be one of `{"rest_stop", "dining", "cafe_scenic", "viewpoint", "attraction", "temple_culture"}` (`HTTP 422` otherwise).
  - `SHIFT_DEPARTURE`: `new_departure_iso` must parse as a valid ISO-8601 datetime or `HH:MM` clock (`HTTP 422` otherwise).
- **F7.4 (Persisted Candidate Duration Baseline)**: `trip_sessions` stores `candidate_durations_json` mapping each `candidate_id` to its last returned `total_duration_minutes` (after any Goong live decoration). `MutateResponse` computes `duration_delta_minutes = mutated_candidate.total_duration_minutes - previous_duration` and includes the signed delta (`+X phút` / `-Y phút`) in `delta_summary_vi`.

## Property-Based Acceptance & Regression Gates
1. **Atomic Rollback After 409 Gate**: Lock a `BRANCH_LONGSON_ISLAND` stop on `P2_ICONIC` (`200 OK`), then attempt to lock an incompatible `BRANCH_SUOINGHE_LOOP` stop (`409 Conflict`), then shift departure to `07:00` (`200 OK`)—proving the failed lock did not poison the session.
2. **Archetype Integrity Gate**: Attempt to lock `vt_nha_lon_long_son` on `P1_EXPRESS`; assert `HTTP 409` (never `HTTP 200` with `P2_ICONIC`).
3. **Malformed Input Gate**: Assert `HTTP 422` on missing `target_poi_id`, `target_category="FOOD"`, and `new_departure_iso="not-a-date"`.
4. **Live-Traffic Delta Gate**: Simulate a live-decorated plan (`234 min`), mutate it, and assert `delta_summary_vi` reflects the true delta from `234 min`.
