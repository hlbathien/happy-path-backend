# Ticket 06: [Tracer Bullet] Piecewise Road-Spine Projection, Custom-Pin Polyline Slicing & Short-Slice Direct Fallback

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 03
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
Submit a mid-corridor GPS origin (e.g., Định Quán `11.20, 107.35` or Hồ Tràm `10.48, 107.35`), a short 15 km slice (`[280, 295]`), or an out-of-slice locked stop:
1. Piecewise road-spine projection maps `(11.20, 107.35)` within `[110.0, 118.0] km` on the Da Lat spine.
2. The returned `encoded_polyline` starts at `(11.20, 107.35)` and ends at the exact requested destination coordinate.
3. No driving leg ever rounds to `0 minutes` (`>= 5 min` floor), ultra-short drivable slices (`>= 10 km`) with 0 open stops gracefully return a 0-stop direct-drive `P1_EXPRESS` route with a `SPARSE_SLICE_DIRECT_DRIVE` warning instead of crashing with `409`, and locking a stop outside `[active_start_km, active_end_km]` deterministically returns `HTTP 409`.

## Root-Cause Failure Modes Eliminated
1. **Offline Polyline Ignoring Custom Pin Coordinates**: Offline geometry always started at canonical HCMC `(10.7872, 106.7498)` even when the user started at `km 114`.
2. **Zero-Minute Driving Legs on Short Slices**: Proportional scaling without a floor produced `0-minute` driving legs on `[286, 290]`.
3. **Out-of-Slice Locked Stops Surviving Slicing**: Locking a `km 148` stop when starting at `km 279` routed the driver backwards by 131 km!
4. **Crow-Flies Axis Projection Error**: Projecting onto a straight chord between HCMC and Da Lat introduced a 9 km error on curved highways.

## Freeze-Grade Invariants (F6.1 – F6.4)
- **F6.1 (Piecewise Road-Spine Polyline Projection)**: Each corridor defines an ordered sequence of `(lat, lng, km)` control waypoints along its physical highway spine. Coordinate projection snaps to the nearest piecewise road segment and interpolates the exact kilometer mark along the highway.
- **F6.2 (Strict Slice Bounds `[active_start_km + 2.0, active_end_km - 1.0]` & Out-of-Slice Lock Veto)**: Every stop in a candidate—including explicitly locked stops—must satisfy `active_start_km + 2.0 <= progress_km <= active_end_km - 1.0`. If any `locked_poi_id` falls outside the active slice, the solver rejects the lock (`HTTP 409` on `/mutate`).
- **F6.3 (Minimum 5-Minute Driving Leg Floor)**: Every driving leg between distinct points (`ORIGIN -> stop_1`, `stop_k -> stop_{k+1}`, `stop_m -> DEST`) enforces a minimum duration of `max(5, scaled_minutes)`.
- **F6.4 (Zero-Stop Direct-Drive Fallback on Sparse Drivable Slices)**: When `active_end_km - active_start_km >= 10.0 km` but zero intermediate stops are eligible/open inside the slice, the solver returns a 0-stop direct-drive `P1_EXPRESS` candidate from `origin_coords` to `destination_coords` with `"SPARSE_SLICE_DIRECT_DRIVE: Đoạn tuyến ngắn hoặc ngoài giờ mở cửa, đề xuất lộ trình chạy thẳng trực tiếp."` in `warnings`. Slices `< 10.0 km` return `HTTP 400`.

## Property-Based Acceptance & Regression Gates
1. **Custom-Pin Polyline Endpoint Gate**: Verify that decoded polyline first and last coordinates match `origin_coords` and `destination_coords` within `0.01` degrees on mid-corridor starts and intermediate resort ends.
2. **Short/Sparse Slice & Zero-Minute Leg Gate**: Probe slices `[279, 300]` and `[280, 292]`; assert HTTP 200, `total_drive_minutes >= 5`, and no leg duration equals `0`.
3. **Out-of-Slice Lock Rejection Gate**: Start a session at `km 114` and attempt to `LOCK_STOP` a `km 42` stop; assert `HTTP 409` and verify session state is uncorrupted.
