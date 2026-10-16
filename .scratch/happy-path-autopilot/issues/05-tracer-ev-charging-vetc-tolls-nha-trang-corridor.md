# Ticket 05: [Tracer Bullet] EV Fast-Charging Range Window (`[15%, 80%]`), Open-Charger Verification & Authoritative VETC Tolls

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 03, Ticket 04
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
 Sweep all 24 departure hours across `HCMC <-> Nha Trang` and `HCMC <-> Da Lat` with `powertrain="ev"` and verify that 100% of returned candidates include an **open, physical-interchange-compatible** DC fast-charging stop inside `[15%, 80%]` of the active trip span (`>= 180.0 km`), and verify that VETC toll estimates accurately reflect each branch's actual tollway usage (`CT01` vs. early exit onto `DT716/DT702` vs. extended `CT01` to `QL28B`).

## Root-Cause Failure Modes Eliminated
1. **Contradictory Toll Prose vs. Seed Data on `QL28B`**: Seed data had `+155,000 VND` on `QL28B` while earlier prose claimed exiting onto `QL28B` reduced tolls relative to `QL20`.
2. **Spec Drift on EV Window (`25–70%` vs `15–80%`)**: Inconsistent window percentages between ticket prose and solver code.
3. **Physical Interchange Violation on EV Coastal Routes**: EV `P3_SCENIC` jumped from `MAIN_SPINE km 338` to `BRANCH_BAUTRANG_COAST km 356` to grab a charger on `MAIN_SPINE` before teleporting to the coast.

## Freeze-Grade Invariants (F5.1 – F5.3)
- **F5.1 (Unified `[15%, 80%]` Open EV Charger Constraint)**: Whenever `powertrain == "ev"` and `active_span_km >= 180.0`, every candidate must contain at least one stop with `has_ev_charge == True` whose directional progress lies in `[active_start_km + 0.15 * active_span_km, active_start_km + 0.80 * active_span_km]`, and that charger stop must be **open at arrival time** and **physically reachable on the candidate's active branch (`[diverge_km, merge_km]`)**.
- **F5.2 (Branch-Level Authoritative VETC Toll Model)**: Toll deltas are stored on `corridor_branches.toll_delta_vnd` (not inconsistently across individual POIs):
  - `hcm_dalat`: `MAIN_SPINE` (`CT01` HCMC–Dầu Giây + Liên Khương) = `136,000 VND`; `BRANCH_BAOLOC_LOOP` = `+0 VND` (`136,000 VND`); `BRANCH_QL28B_DAININH` (continues on `CT01` Dầu Giây–Phan Thiết–Vĩnh Hảo to Lương Sơn exit) = `+125,000 VND` (`261,000 VND`).
  - `hcm_nhatrang`: `MAIN_SPINE` (`CT01` through Cam Lâm–Vĩnh Hảo) = `309,000 VND`; `BRANCH_VINHHY_DT702` = `-25,000 VND` (`284,000 VND`); `BRANCH_BAUTRANG_COAST` (exits `CT01` early at Phan Thiết onto coastal `DT716/DT702`) = `-65,000 VND` (`244,000 VND`).
  - `hcm_vungtau`: `MAIN_SPINE` (`CT01` Long Thành + `QL51`) = `81,000 VND`; `BRANCH_LONGSON_ISLAND` = `-15,000 VND` (`66,000 VND`); `BRANCH_SUOINGHE_LOOP` = `-15,000 VND` (`66,000 VND`).
- **F5.3 (Coastal EV Charger Coverage)**: Both `MAIN_SPINE` and coastal/mountain branches (`BRANCH_BAUTRANG_COAST`, `BRANCH_QL28B_DAININH`) include verified EV-capable stops (or main-spine EV stops located **upstream of `diverge_km`** at `Km 42` Long Thành / `Km 135` Hàm Tân) so coastal EV routes never violate physical interchange continuity.

## Property-Based Acceptance & Regression Gates
1. **96-Run EV & ICE Sweep on HCMC <-> Nha Trang**: Across 24 hours $	imes$ 2 directions $	imes$ 2 powertrains, assert 100% of EV candidates satisfy `[15%, 80%]` charger placement, physical branch interchange rules, and open operating hours.
2. **Branch Toll Concordance Gate**: Verify `estimated_vetc_toll_vnd` matches `max(0, base_vetc_toll_vnd + branch.toll_delta_vnd)` for every branch across all 3 corridors.
