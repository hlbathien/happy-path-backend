# Ticket 04: [Tracer Bullet] Unconditional UV Shield, Multi-POI Exertion Cap, Check-In Anchor & Safe Night Rest Routing

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 03
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
1. Depart at any midday hour (`08:00`–`13:00`): exposed outdoor stops (`midday_sun_trap = 1`) are **never** scheduled between `11:00` and `14:30` in either Pass 1 or Pass 2 (unless explicitly locked by `locked_poi_ids`, which triggers an explicit `UV_LOCK_OVERRIDE_WARNING`).
2. Depart at night (`21:30`–`03:00`): instead of routing drivers to closed restaurants at `02:45 AM`, Pass 2 relaxes the *minimum stop count* to `1` so the itinerary routes exclusively through genuinely open **24/7 highway rest stops (`open_minute = 0, close_minute = 1440`)** with a night-driving safety advisory.
3. Ablate (zero out) the Exertion Cap, Markov Co-Occurrence table, or Check-In Anchor bonus in memory and watch the automated regression suite immediately catch every single ablation!

## Root-Cause Failure Modes Eliminated
1. **Nighttime Fallback Routing to Closed Restaurants at 02:45 AM**: Relaxing `open_minute/close_minute` in Pass 2 caused `21:30` departures to schedule restaurants at `02:44 AM` that do not open until `06:00 AM`.
2. **UV Protection Bypassed in Pass 2**: Fallback mode disabled the `11:00–14:30` UV shield.
3. **Untested Exertion Cap & Scoring Weights**: Having only 1 `HIGH`-exertion POI in the seed database meant removing the exertion cap still passed all tests, and no test verified that Markov co-occurrence or `14:00` check-in anchors actually changed route selection.

## Freeze-Grade Invariants (F4.1 – F4.4)
- **F4.1 (Safe Nighttime Pass-2 Relaxation)**: Pass 1 requires `len(seq) >= 2` (when `>=2` stops are eligible) and `max_wait_minutes = 30`. If Pass 1 finds no feasible sequence (e.g., late-night departure at `21:30` when only 24/7 rest stops are open), Pass 2 relaxes **only** the minimum stop count down to `len(seq) >= 1` and increases `max_wait_minutes = 45`, while keeping **strict operating hours (`open_minute <= service_start <= close_minute - dwell`)**, **Midday UV Shield**, and **Exertion Cap** 100% enforced!
- **F4.2 (Unconditional Midday UV Heat Shield)**: For any stop with `midday_sun_trap == True`, service overlapping `[660, 870]` (`11:00`–`14:30`) is rejected in both Pass 1 and Pass 2. Only if `poi.poi_id in locked_ids` is it permitted, and `cand.warnings` must append `"UV_LOCK_OVERRIDE_WARNING: Điểm dừng khóa cố định nằm trong khung giờ nắng gắt 11:00-14:30."`.
- **F4.3 (Multi-POI Exertion Cap)**: Every corridor with scenic hikes seeds at least 2 `HIGH`-exertion POIs, and the solver strictly enforces `sum(1 for p in seq if p.exertion_level == "HIGH") <= 1` across all passes.
- **F4.4 (Check-In Anchor Enforcement & Markov Co-Occurrence Differential)**: When destination arrival falls in `[510, 840]` (`08:30`–`14:00`) and an open `is_arrival_anchor` stop exists in the eligible pool, `P2_ICONIC` and `P3_SCENIC` enforce an arrival-anchor bonus (`+38.0`) sufficient to guarantee anchor inclusion on morning departures, and `poi_co_occurrence` pairs shift candidate selection on ties/near-ties.

## Property-Based Acceptance & Regression Gates
1. **Nighttime Open-Venue Gate (`21:30`..`03:30`)**: Across all 3 corridors, verify that `21:30`, `23:30`, and `01:30` departures only schedule stops that are genuinely open at their arrival/service clock (zero closed-restaurant visits at 2–4 AM).
2. **Mutation-Testing / Ablation Gate**: In the test suite, programmatically verify that:
   - The seed database contains `>= 2` `HIGH`-exertion POIs on Da Lat and Nha Trang, and attempting to lock 2 `HIGH`-exertion POIs returns `HTTP 409`.
   - Zeroing out `registry.co_occurrence` or `checkin_anchor_bonus` measurably alters the selected stop sequence or score on the canonical morning benchmark.
