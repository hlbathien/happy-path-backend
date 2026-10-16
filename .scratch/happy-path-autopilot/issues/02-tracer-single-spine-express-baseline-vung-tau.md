# Ticket 02: [Tracer Bullet] 48-Hour Schedule Validity, Bounded Early-Arrival Wait & Median-Safe Express Baseline on HCMC <-> Vung Tau

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 01
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
Sweep all 24 hourly departure times (`00:00` through `23:00`) in both directions (`HCMC -> Vung Tau` and `Vũng Tàu -> Sài Gòn`) and verify that **zero** returned stops are ever visited while closed. When a morning driver arrives at a breakfast stop up to 30 minutes before opening (e.g., arriving at `06:06` for a `06:30` opening), the itinerary explicitly accounts for a `24-minute` wait (`arrival_clock = "06:06"`, `service_start_clock = "06:30"`, `departure_clock = "06:30" + dwell`), and divided-highway stops on `QL51` respect `OUTBOUND`-only and `INBOUND`-only median restrictions.

## Root-Cause Failure Modes Eliminated
1. **Visiting Closed Stops Under Fallback**: The v2 solver relaxed opening-hour checks in Pass 2, causing 32 out of 48 hourly requests on Vung Tau to visit closed venues.
2. **All-BOTH Directional Tagging on Divided Highways**: Tagging every `QL51` venue as `BOTH` failed to test median-barrier filtering in real baseline runs.
3. **Brittle Polyline Vertex Count Assertions**: Asserting `len(vertices) == len(stops) + 2` broke as soon as road-following intermediate vertices were added.

## Freeze-Grade Invariants (F2.1 – F2.4)
- **F2.1 (Absolute Operating-Window Validity Across All Passes)**: No solver pass may ever schedule a stop where service begins before `open_minute` or ends after `close_minute`. If a driver arrives at `arr_mod < open_minute` and `open_minute - arr_mod <= max_wait_minutes` (`30 min` in Pass 1; `45 min` in Pass 2), the driver waits (`wait_minutes = open_minute - arr_mod`, `service_start = open_minute`, `dep_mod = service_start + dwell_minutes`). Otherwise, the stop is strictly infeasible at that hour.
- **F2.2 (Divided-Highway Median Enforcement on QL51)**: The Vung Tau corridor seed includes real divided-highway directional stops (`OUTBOUND`-only on the eastbound right side of `QL51` and `INBOUND`-only on the westbound return side). Outbound plans must never select `INBOUND`-only stops, and inbound plans must never select `OUTBOUND`-only stops.
- **F2.3 (Direct Drive-Time Optimization in P1_EXPRESS)**: The `P1_EXPRESS` objective explicitly penalizes `total_drive_minutes` (`-1.5 * total_drive_minutes`) alongside detour minutes and hazard so that `P1_EXPRESS` favors the fastest safe highway spine.
- **F2.4 (Road-Following Sliced Polyline Contract)**: Offline polylines are sliced and interpolated along dense corridor control points from the actual origin coordinate through each planned stop to the actual destination coordinate, yielding `len(decoded_points) >= len(stops) + 2` where the first and last vertices match the requested endpoints within `0.01` degrees.

## Property-Based Acceptance & Regression Gates
1. **48-Hour Bidirectional Schedule-Validity Sweep**: Run all 24 hourly departures (`00:00`..`23:00`) for both `outbound` and `inbound` on `HCMC <-> Vung Tau` (48 plans). Assert that across all 48 responses, every scheduled stop satisfies `open_minute <= (arrival_minute + wait_minutes) % 1440 <= close_minute - dwell_minutes`.
2. **Divided-Highway Median Gate**: Assert that `OUTBOUND`-only `QL51` stops never appear in `inbound` plans and `INBOUND`-only `QL51` stops never appear in `outbound` plans across all 24 hours.
3. **Dense Polyline Geometry Gate**: Decode `encoded_polyline` and assert `len(vertices) >= len(stops) + 2` and endpoint concordance with origin/destination coordinates.
