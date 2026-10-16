# Ticket 03: [Tracer Bullet] Physical Interchange Topology & True 5D Pareto Non-Dominance on HCMC <-> Da Lat

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 02
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
Request itineraries across `HCMC <-> Da Lat` and `HCMC <-> Nha Trang` and verify two mathematical guarantees:
1. **Physical Interchange Continuity**: Any itinerary using an alternative highway branch (`BRANCH_BAOLOC_LOOP`, `BRANCH_QL28B_DAININH`, `BRANCH_VINHHY_DT702`, `BRANCH_BAUTRANG_COAST`, `BRANCH_LONGSON_ISLAND`, `BRANCH_SUOINGHE_LOOP`) only visits `MAIN_SPINE` stops located **upstream of the branch's physical diverge kilometer (`<= diverge_km`)** or **downstream of its physical merge kilometer (`>= merge_km`)**—never teleporting between the main highway and a parallel branch mid-segment.
2. **True 5D Pareto Non-Dominance**: Across the 5-dimensional objective vector `(-total_drive_minutes, -total_detour_minutes, +aggregate_utility_score, +aggregate_scenic_score, -hazard_risk_score)`, no returned candidate in `[P1, P2, P3]` is dominated by any other returned candidate.

## Root-Cause Failure Modes Eliminated
1. **Mid-Corridor Branch Teleportation (`MAIN_SPINE km 338 -> BAUTRANG_COAST km 356`)**: Checking only that at most one non-main `branch_id` label appeared in a sequence allowed the solver to stay on inland `MAIN_SPINE` past the branch divergence point (`km 195`) all the way to `km 338` and then jump sideways onto the coastal branch at `km 356`.
2. **Pareto Dominance Violations (`P3` strictly beating `P2` on all reported metrics)**: Selecting `P1`, `P2`, and `P3` independently without a dominance filter allowed one candidate to be strictly worse than another on every exposed metric.
3. **Silent Portfolio Shortfall**: Returning `<3` candidates on sparse fixtures without an explicit `PORTFOLIO_SHORTFALL` advisory.

## Freeze-Grade Invariants (F3.1 – F3.4)
- **F3.1 (Explicit Branch Interchange Windows `[diverge_km, merge_km]`)**: Every branch is registered in `corridor_branches` with its physical `diverge_km` and `merge_km` measured from HCMC. A stop sequence using alternative branch `B` is physically valid if and only if:
  - Every non-main stop belongs to `B`, AND
  - Every `MAIN_SPINE` stop in the sequence has `corridor_km <= B.diverge_km` or `corridor_km >= B.merge_km`.
  - Directed edges `(u, v)` in `corridor_edges` are only generated for pairs `(u, v)` that satisfy this physical interchange rule.
- **F3.2 (Strict 5D Pareto Non-Dominance Gate)**: Let `V(c) = (-c.total_drive_minutes, -c.total_detour_minutes, c.aggregate_utility_score, c.aggregate_scenic_score, -c.hazard_risk_score)`. Candidate `A` dominates `B` iff `forall i: V_i(A) >= V_i(B)` and `exists j: V_j(A) > V_j(B)`. When selecting `P1`, `P2`, `P3`, any candidate dominated by an already-selected candidate is rejected in favor of the highest-scoring non-dominated candidate for that archetype.
- **F3.3 (Portfolio Shortfall Transparency)**: If the entire feasible, non-dominated frontier contains fewer than 3 distinct candidates, the solver returns only the non-dominated candidates and appends `"PORTFOLIO_SHORTFALL: Không đủ 3 phương án độc lập không bị trội (Pareto-optimal) trên đoạn tuyến/khung giờ này."` to `warnings`.

## Property-Based Acceptance & Regression Gates
1. **96-Run Sweep for Pareto Non-Dominance & Interchange Continuity**: Sweep `HCMC <-> Da Lat` across both directions, all 24 departure hours, and both powertrains (`96` runs). Assert that in 100% of runs:
   - Zero candidates violate `[diverge_km, merge_km]` physical interchange bounds.
   - Zero returned candidates are Pareto-dominated on `V(c)` by another candidate in the same response.
2. **Sparse Fixture Shortfall Gate**: On a controlled single-POI slice, assert the response emits only non-dominated candidates and includes `PORTFOLIO_SHORTFALL` in `warnings`.
