from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from happy_path.core.graph import BranchMeta, CorridorMeta, CorridorRegistry, POINode
from happy_path.schemas.itinerary import (
    CandidateArchetype,
    Coordinate,
    ParetoCandidate,
    PlannedStop,
    PowertrainMode,
)

def parse_departure_minute_and_bucket(departure_iso: str) -> Tuple[int, str, str]:
    s = (departure_iso or "").strip()
    if len(s) == 5 and s[2] == ":":
        minute = int(s[:2]) * 60 + int(s[3:5])
        clock = s
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone(timedelta(hours=7)))
        minute = dt.hour * 60 + dt.minute
        clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if 270 <= minute < 420:
        bucket = "early_morning"
    elif (420 <= minute < 570) or (960 <= minute < 1140):
        bucket = "peak_congestion"
    elif minute >= 1140 or minute < 270:
        bucket = "rain_evening"
    else:
        bucket = "day_normal"
    return minute, clock, bucket

def fmt_clock(total_minutes: int) -> str:
    m = total_minutes % 1440
    return f"{m // 60:02d}:{m % 60:02d}"

def service_wait_minutes(arrival: int, open_minute: int, close_minute: int, dwell: int, is_24h: bool, max_wait: int) -> Optional[int]:
    if is_24h:
        return 0
    arrival_mod = arrival % 1440
    wait = max(0, open_minute - arrival_mod)
    if wait > max_wait or arrival_mod + wait + dwell > close_minute:
        return None
    return wait

def overlaps_midday(service_start: int, dwell: int) -> bool:
    return any(service_start <= day * 1440 + 870 and service_start + dwell >= day * 1440 + 660
               for day in range(service_start // 1440, (service_start + dwell) // 1440 + 1))

def _progress_km(poi: POINode, corridor: CorridorMeta, direction: str) -> float:
    if direction == "outbound":
        return round(poi.corridor_km, 1)
    return round(max(0.0, corridor.total_spine_km - poi.corridor_km), 1)

def _are_branches_physically_compatible(
    seq: List[POINode],
    branches_map: Dict[str, BranchMeta],
    archetype: CandidateArchetype,
) -> Tuple[bool, str]:
    """
    Enforces Freeze-Spec F3.1 (Physical Interchange Continuity):
    1. P1_EXPRESS is strictly restricted to MAIN_SPINE.
    2. At most ONE non-MAIN_SPINE branch B may appear in seq.
    3. If non-MAIN_SPINE branch B is active, NO MAIN_SPINE stop in seq may have
       corridor_km strictly inside (B.diverge_km, B.merge_km).
    """
    non_main = {p.branch_id for p in seq if p.branch_id != "MAIN_SPINE"}
    if len(non_main) > 1:
        return False, "INVALID_MULTI_BRANCH"
    active_branch = next(iter(non_main)) if non_main else "MAIN_SPINE"
    if archetype == CandidateArchetype.P1_EXPRESS and active_branch != "MAIN_SPINE":
        return False, active_branch

    if active_branch != "MAIN_SPINE":
        b_meta = branches_map.get(active_branch)
        if b_meta is None:
            return False, active_branch
        for p in seq:
            if p.branch_id == "MAIN_SPINE":
                if b_meta.diverge_km < p.corridor_km < b_meta.merge_km:
                    return False, active_branch
    return True, active_branch

def objective_vector(c: ParetoCandidate) -> Tuple[float, float, float, float, float]:
    return (
        float(-c.total_drive_minutes),
        float(-c.total_detour_minutes),
        float(c.aggregate_utility_score),
        float(c.aggregate_scenic_score),
        float(-c.hazard_risk_score),
    )

def dominates(a: ParetoCandidate, b: ParetoCandidate) -> bool:
    va = objective_vector(a)
    vb = objective_vector(b)
    return all(x >= y for x, y in zip(va, vb)) and any(x > y for x, y in zip(va, vb))

def _enumerate_candidates_for_archetype(
    registry: CorridorRegistry,
    corridor: CorridorMeta,
    direction: str,
    dep_min: int,
    time_bucket: str,
    archetype: CandidateArchetype,
    powertrain: PowertrainMode,
    sedan_safe_only: bool,
    active_start_km: float,
    active_end_km: float,
    origin_coord: Coordinate,
    dest_coord: Coordinate,
    locked_ids: Set[str],
    rejected_ids: Set[str],
    skip_categories: Set[str],
    min_stops: int,
    max_wait_minutes: int,
) -> List[ParetoCandidate]:
    dir_tag = "OUTBOUND" if direction == "outbound" else "INBOUND"
    raw_pois = registry.pois_by_corridor[corridor.corridor_id]
    branches_map = registry.branches_by_corridor.get(corridor.corridor_id, {})
    active_span_km = max(10.0, active_end_km - active_start_km)
    require_ev_stop = (powertrain == PowertrainMode.ev and active_span_km >= 180.0)

    # Every locked_id must exist in this corridor and lie inside the active slice
    for lid in locked_ids:
        lpoi = registry.pois_by_id.get(lid)
        if lpoi is None or lpoi.corridor_id != corridor.corridor_id:
            return []
        lprog = _progress_km(lpoi, corridor, direction)
        if not (active_start_km + 2.0 <= lprog <= active_end_km - 1.0):
            return []
        if lpoi.direction_allowed not in ("BOTH", dir_tag):
            return []
        if sedan_safe_only and not lpoi.sedan_safe:
            return []

    eligible: List[POINode] = []
    for p in raw_pois:
        if p.poi_id in rejected_ids or p.category in skip_categories:
            continue
        prog = _progress_km(p, corridor, direction)
        if not (active_start_km + 2.0 <= prog <= active_end_km - 1.0):
            continue
        if p.direction_allowed not in ("BOTH", dir_tag):
            continue
        if sedan_safe_only and not p.sedan_safe:
            continue
        if time_bucket == "rain_evening" and p.hazard_level >= 0.35 and p.poi_id not in locked_ids:
            continue
        eligible.append(p)

    sorted_pois = sorted(eligible, key=lambda p: _progress_km(p, corridor, direction))

    if archetype == CandidateArchetype.P1_EXPRESS:
        label = "P1: Express Velocity & Safety Spine"
        w_drive, w_u, w_s, w_detour, w_haz = 1.65, 0.45, 0.05, 2.60, 28.0
        preferred_aff = {"EXPRESS", "ALL"}
        preferred_branches = {"MAIN_SPINE"}
        target_len = 2
    elif archetype == CandidateArchetype.P2_ICONIC:
        label = "P2: Iconic Balanced & Culinary Autopilot"
        w_drive, w_u, w_s, w_detour, w_haz = 0.25, 1.40, 0.85, 0.45, 10.0
        preferred_aff = {"ICONIC", "ALL"}
        preferred_branches = {"BRANCH_BAOLOC_LOOP", "BRANCH_VINHHY_DT702", "BRANCH_LONGSON_ISLAND", "MAIN_SPINE"}
        target_len = 2 if corridor.total_spine_km < 150 else 3
    else:
        label = "P3: Scenic & Coastal/Mountain Explorer"
        w_drive, w_u, w_s, w_detour, w_haz = 0.05, 0.80, 1.85, 0.10, 4.0
        preferred_aff = {"SCENIC", "ICONIC", "ALL"}
        preferred_branches = {"BRANCH_QL28B_DAININH", "BRANCH_BAUTRANG_COAST", "BRANCH_SUOINGHE_LOOP", "MAIN_SPINE"}
        target_len = 2 if corridor.total_spine_km < 150 else 3

    n = len(sorted_pois)
    subset_indices: List[List[int]] = []
    for i in range(n):
        subset_indices.append([i])
        for j in range(i + 1, n):
            subset_indices.append([i, j])
            for k in range(j + 1, n):
                subset_indices.append([i, j, k])

    scored_candidates: List[Tuple[float, ParetoCandidate]] = []
    origin_access = registry.access_drive_minutes(corridor, direction, origin_coord)
    dest_access = registry.access_drive_minutes(corridor, direction, dest_coord)

    for idxs in subset_indices:
        seq = [sorted_pois[i] for i in idxs]
        if len(seq) < min_stops:
            continue
        seq_ids = {p.poi_id for p in seq}
        if not locked_ids.issubset(seq_ids):
            continue

        # 1. Physical Branch Interchange Check
        branch_ok, active_branch = _are_branches_physically_compatible(seq, branches_map, archetype)
        if not branch_ok:
            continue

        # 2. Unconditional Human Exertion Cap (<= 1 HIGH stop in ALL passes)
        if sum(1 for p in seq if p.exertion_level == "HIGH") > 1:
            continue

        # 3. EV Fast-Charging Range Guarantee ([15%, 80%] of active_span_km)
        if require_ev_stop:
            ev_stops = [
                p for p in seq
                if p.has_ev_charge
                and (active_start_km + 0.15 * active_span_km <= _progress_km(p, corridor, direction) <= active_start_km + 0.80 * active_span_km)
            ]
            if not ev_stops:
                continue

        # 4. Strict Monotonic Forward Progress (+4.0 km minimum separation)
        monotonic_ok = True
        for s_idx in range(len(seq) - 1):
            if _progress_km(seq[s_idx + 1], corridor, direction) < _progress_km(seq[s_idx], corridor, direction) + 4.0:
                monotonic_ok = False
                break
        if not monotonic_ok:
            continue

        # 5. Prevent Consecutive Duplicate Categories (unless explicitly locked)
        cat_ok = True
        for s_idx in range(len(seq) - 1):
            if seq[s_idx].category == seq[s_idx + 1].category and seq[s_idx + 1].poi_id not in locked_ids:
                cat_ok = False
                break
        if not cat_ok:
            continue

        # 6. Simulate Arrival, Wait-for-Opening, Service Start, Departure & UV Shield
        curr_node = "ORIGIN" if direction == "outbound" else "DEST"
        final_node = "DEST" if direction == "outbound" else "ORIGIN"
        curr_min = dep_min
        total_drive = 0
        total_dwell = 0
        total_wait = 0
        total_detour = 0
        max_haz = 0.0
        windows_ok = True
        uv_lock_override = False
        stop_clocks: List[Tuple[int, int, int, int]] = []  # (arr_min, wait_m, srv_min, dep_min)

        for idx_in_seq, poi in enumerate(seq):
            edge_key = (corridor.corridor_id, curr_node, poi.poi_id, time_bucket)
            if edge_key not in registry.edges:
                windows_ok = False
                break
            raw_drive_m, _, haz, _ = registry.edges[edge_key]
            drive_m = max(5, raw_drive_m)
            if idx_in_seq == 0:
                progress = _progress_km(poi, corridor, direction)
                drive_m = max(5, round(raw_drive_m * (progress - active_start_km) / max(1.0, progress))) + origin_access
            arr_min = curr_min + drive_m
            arr_mod = arr_min % 1440

            wait_m = service_wait_minutes(arr_min, poi.open_minute, poi.close_minute, poi.dwell_minutes, poi.is_24h, max_wait_minutes)
            if wait_m is None:
                windows_ok = False
                break
            overlaps_uv = overlaps_midday(arr_min + wait_m, poi.dwell_minutes)
            if poi.midday_sun_trap and overlaps_uv:
                if poi.poi_id not in locked_ids:
                    windows_ok = False
                    break
                uv_lock_override = True

            srv_min = arr_min + wait_m
            dep_stop = srv_min + poi.dwell_minutes
            stop_clocks.append((arr_min, wait_m, srv_min, dep_stop))
            total_drive += drive_m
            total_wait += wait_m
            total_dwell += poi.dwell_minutes
            total_detour += poi.detour_minutes
            max_haz = max(max_haz, haz, poi.hazard_level)
            curr_min = dep_stop
            curr_node = poi.poi_id

        if not windows_ok:
            continue

        last_edge = (corridor.corridor_id, curr_node, final_node, time_bucket)
        if last_edge not in registry.edges:
            continue
        last_drive, _, last_haz, _ = registry.edges[last_edge]
        last_progress = _progress_km(seq[-1], corridor, direction)
        last_drive = max(5, round(last_drive * (active_end_km - last_progress) / max(1.0, corridor.total_spine_km - last_progress))) + dest_access
        total_drive += last_drive
        final_arr_mod = (curr_min + last_drive) % 1440
        max_haz = max(max_haz, last_haz)

        # 7. Multi-Objective Scoring (including direct drive penalty, Markov co-occurrence & 14:00 Check-in Anchor)
        aff_bonus = sum(18.0 if p.archetype_affinity in preferred_aff else -8.0 for p in seq)
        if active_branch in preferred_branches:
            branch_bonus = 34.0 if (archetype != CandidateArchetype.P1_EXPRESS and active_branch != "MAIN_SPINE") else 18.0
        else:
            branch_bonus = -20.0

        synergy_bonus = sum(
            registry.co_occurrence.get((corridor.corridor_id, seq[k].poi_id, seq[k + 1].poi_id), 0.0)
            for k in range(len(seq) - 1)
        )
        checkin_anchor_bonus = (
            38.0
            if (300 <= dep_min <= 660 and 510 <= final_arr_mod <= 840 and any(p.is_arrival_anchor for p in seq) and archetype != CandidateArchetype.P1_EXPRESS)
            else 0.0
        )
        len_penalty = abs(len(seq) - target_len) * 10.0

        score = (
            w_u * sum(p.utility_score for p in seq)
            + w_s * sum(p.scenic_score for p in seq)
            + aff_bonus
            + branch_bonus
            + synergy_bonus
            + checkin_anchor_bonus
            - w_drive * total_drive
            - w_detour * total_detour
            - w_haz * max_haz
            - 0.35 * total_wait
            - len_penalty
        )

        planned_stops: List[PlannedStop] = []
        for idx, (poi, (arr_m, wait_m, srv_m, dep_m)) in enumerate(zip(seq, stop_clocks), start=1):
            planned_stops.append(
                PlannedStop(
                    stop_order=idx,
                    poi_id=poi.poi_id,
                    branch_id=poi.branch_id,
                    goong_place_id=poi.goong_place_id,
                    name_vi=poi.name_vi,
                    commune_name=poi.commune_name,
                    category=poi.category,
                    exertion_level=poi.exertion_level,
                    has_ev_charge=poi.has_ev_charge,
                    is_arrival_anchor=poi.is_arrival_anchor,
                    coordinates=Coordinate(lat=poi.lat, lng=poi.lng),
                    corridor_km_from_hcm=poi.corridor_km,
                    progress_km_from_origin=_progress_km(poi, corridor, direction),
                    arrival_clock=fmt_clock(arr_m),
                    service_start_clock=fmt_clock(srv_m),
                    wait_minutes=wait_m,
                    departure_clock=fmt_clock(dep_m),
                    dwell_minutes=poi.dwell_minutes,
                    detour_minutes=poi.detour_minutes,
                    utility_score=poi.utility_score,
                    scenic_score=poi.scenic_score,
                    tradeoff_note_vi=poi.tradeoff_note_vi,
                    source_url=poi.source_url,
                    open_minute=poi.open_minute, close_minute=poi.close_minute,
                    is_24h=poi.is_24h, midday_sun_trap=poi.midday_sun_trap,
                    is_locked=poi.poi_id in locked_ids,
                    ev_max_kw=poi.ev_max_kw, ev_stall_count=poi.ev_stall_count,
                    ev_connector_type=poi.ev_connector_type,
                )
            )

        b_meta = branches_map.get(active_branch)
        estimated_toll = registry.estimate_toll(corridor, direction, active_branch, active_start_km, active_end_km)

        avg_util = round(sum(p.utility_score for p in seq) / len(seq), 1)
        avg_scenic = round(sum(p.scenic_score for p in seq) / len(seq), 1)
        stop_names = " -> ".join(p.name_vi for p in seq)
        summary_vi = (
            f"{label} [{active_branch}]: {len(seq)} điểm dừng ({stop_names}), "
            f"lái xe {total_drive} phút, lệch tuyến +{total_detour} phút, phí VETC ~{estimated_toll:,}đ."
        )
        sliced_poly = registry.build_sliced_polyline(
            corridor=corridor,
            direction=direction,
            origin_coord=origin_coord,
            dest_coord=dest_coord,
            stops=seq,
            active_start_km=active_start_km,
            active_end_km=active_end_km,
        )
        warnings: List[str] = ['OFFLINE_ROAD_MODEL: Thời gian và hình học ngoại tuyến là ước tính từ dữ liệu cấu hình, không phải xác minh đường bộ trực tiếp.']
        if active_branch != 'MAIN_SPINE' and not b_meta.branch_waypoints:
            warnings.append('MISSING_BRANCH_GEOMETRY: Nhánh tuyến chưa có chuỗi tọa độ đường bộ; hình vẽ chỉ nối các điểm đã biết.')
        if origin_access or dest_access:
            warnings.append(f'CORRIDOR_ACCESS_ESTIMATE: Cộng {origin_access}/{dest_access} phút tiếp cận/rời tuyến (45 km/h).')
        if uv_lock_override:
            warnings.append("UV_LOCK_OVERRIDE_WARNING: Điểm dừng khóa cố định nằm trong khung giờ nắng gắt 11:00-14:30.")
        if (min_stops == 1 and len(seq) == 1
                and seq[0].is_24h):
            warnings.append("NIGHT_SAFE_REST_ROUTING: Khung giờ đặc biệt, lộ trình chỉ giữ các trạm dừng/điểm ngắm cảnh đang mở cửa 24/7.")

        cand = ParetoCandidate(
            candidate_id=archetype,
            label=label,
            corridor_id=corridor.corridor_id,
            active_branch_id=active_branch,
            direction=direction,
            powertrain=powertrain,
            total_drive_minutes=total_drive,
            total_dwell_minutes=total_dwell,
            total_wait_minutes=total_wait,
            total_duration_minutes=total_drive + total_dwell + total_wait,
            total_detour_minutes=total_detour,
            estimated_vetc_toll_vnd=estimated_toll,
            ev_charging_satisfied=True,
            aggregate_utility_score=avg_util,
            aggregate_scenic_score=avg_scenic,
            hazard_risk_score=round(max_haz, 2),
            tradeoff_summary_vi=summary_vi,
            encoded_polyline=sliced_poly,
            stops=planned_stops,
            warnings=warnings,
            max_wait_minutes=max_wait_minutes,
        )
        scored_candidates.append((score, cand))

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    return [c for _, c in scored_candidates]

def solve_pareto_portfolio(
    registry: CorridorRegistry,
    corridor: CorridorMeta,
    direction: str,
    departure_iso: str,
    powertrain: PowertrainMode = PowertrainMode.ice,
    sedan_safe_only: bool = True,
    active_start_km: float = 0.0,
    active_end_km: Optional[float] = None,
    origin_coord: Optional[Coordinate] = None,
    dest_coord: Optional[Coordinate] = None,
    locked_ids: Optional[Set[str]] = None,
    rejected_ids: Optional[Set[str]] = None,
    skip_categories: Optional[Set[str]] = None,
    preferred_candidate_id: Optional[CandidateArchetype] = None,
) -> Tuple[str, str, List[ParetoCandidate]]:
    dep_min, dep_clock, bucket = parse_departure_minute_and_bucket(departure_iso)
    end_km = active_end_km if active_end_km is not None else corridor.total_spine_km
    locked = locked_ids or set()
    rejected = rejected_ids or set()
    skip_cats = skip_categories or set()

    orig_c = origin_coord or (
        Coordinate(lat=corridor.origin_lat, lng=corridor.origin_lng)
        if direction == "outbound"
        else Coordinate(lat=corridor.dest_lat, lng=corridor.dest_lng)
    )
    dest_c = dest_coord or (
        Coordinate(lat=corridor.dest_lat, lng=corridor.dest_lng)
        if direction == "outbound"
        else Coordinate(lat=corridor.origin_lat, lng=corridor.origin_lng)
    )

    used_sigs: Set[Tuple[str, ...]] = set()
    portfolio: List[ParetoCandidate] = []

    archetypes = (CandidateArchetype.P1_EXPRESS, CandidateArchetype.P2_ICONIC, CandidateArchetype.P3_SCENIC)
    if preferred_candidate_id is not None:
        # A mutation must solve its requested archetype before other archetypes consume signatures.
        archetypes = (preferred_candidate_id,) + tuple(arch for arch in archetypes if arch != preferred_candidate_id)
    pools: Dict[CandidateArchetype, List[ParetoCandidate]] = {}
    for arch in archetypes:
        # Pass 1: min_stops=2, max_wait_minutes=30 (always enforces open hours, UV shield, and exertion cap)
        pool = _enumerate_candidates_for_archetype(
            registry, corridor, direction, dep_min, bucket, arch, powertrain,
            sedan_safe_only, active_start_km, end_km, orig_c, dest_c,
            locked, rejected, skip_cats, min_stops=2, max_wait_minutes=30,
        )
        # Pass 2: relax ONLY min_stops=1 and max_wait_minutes=45 (NEVER relaxes open hours, UV shield, or exertion cap)
        if not pool:
            pool = _enumerate_candidates_for_archetype(
                registry, corridor, direction, dep_min, bucket, arch, powertrain,
                sedan_safe_only, active_start_km, end_km, orig_c, dest_c,
                locked, rejected, skip_cats, min_stops=1, max_wait_minutes=45,
            )

        pools[arch] = pool

    # Compute the frontier before assigning archetypes: an unselected route can
    # dominate an early choice just as a later archetype choice can. Metrics for
    # the same stop sequence are identical across archetypes.
    feasible = {tuple(s.poi_id for s in c.stops): c for pool in pools.values() for c in pool}
    frontier = {
        sig for sig, candidate in feasible.items()
        if not any(dominates(other, candidate) for other in feasible.values())
    }
    for arch in archetypes:
        selected: Optional[ParetoCandidate] = None
        for cand in pools[arch]:
            sig = tuple(s.poi_id for s in cand.stops)
            if sig in used_sigs or sig not in frontier:
                continue
            selected = cand
            break

        if selected is not None:
            used_sigs.add(tuple(s.poi_id for s in selected.stops))
            portfolio.append(selected)

    # Sparse slices can drive directly unless locks or the long-span EV charger requirement forbid it.
    active_span_km = end_km - active_start_km
    require_ev_stop = powertrain == PowertrainMode.ev and active_span_km >= 180.0
    if not portfolio and not locked and active_span_km >= 10.0 and not require_ev_stop:
        direct_drive_min = max(5, int(round((active_span_km / 52.0) * 60.0)))
        direct_drive_min += registry.access_drive_minutes(corridor, direction, orig_c) + registry.access_drive_minutes(corridor, direction, dest_c)
        sliced_poly = registry.build_sliced_polyline(
            corridor=corridor,
            direction=direction,
            origin_coord=orig_c,
            dest_coord=dest_c,
            stops=[],
            active_start_km=active_start_km,
            active_end_km=end_km,
        )
        estimated_toll = registry.estimate_toll(corridor, direction, 'MAIN_SPINE', active_start_km, end_km)
        portfolio.append(
            ParetoCandidate(
                candidate_id=CandidateArchetype.P1_EXPRESS,
                label="P1: Express Direct Drive (Sparse Slice)",
                corridor_id=corridor.corridor_id,
                active_branch_id="MAIN_SPINE",
                direction=direction,
                powertrain=powertrain,
                total_drive_minutes=direct_drive_min,
                total_dwell_minutes=0,
                total_wait_minutes=0,
                total_duration_minutes=direct_drive_min,
                total_detour_minutes=0,
                estimated_vetc_toll_vnd=estimated_toll,
                ev_charging_satisfied=True,
                aggregate_utility_score=80.0,
                aggregate_scenic_score=60.0,
                hazard_risk_score=0.05,
                tradeoff_summary_vi=f"P1: Lộ trình chạy thẳng trực tiếp ({active_span_km:.1f} km), lái xe {direct_drive_min} phút.",
                encoded_polyline=sliced_poly,
                stops=[],
                warnings=["SPARSE_SLICE_DIRECT_DRIVE: Đoạn tuyến ngắn hoặc ngoài giờ mở cửa, đề xuất lộ trình chạy thẳng trực tiếp.", "OFFLINE_ROAD_MODEL: Thời gian và hình học ngoại tuyến là ước tính từ dữ liệu cấu hình."],
            )
        )

    if 0 < len(portfolio) < 3:
        for c in portfolio:
            if not any("PORTFOLIO_SHORTFALL" in w for w in c.warnings):
                c.warnings.append(
                    "PORTFOLIO_SHORTFALL: Không đủ 3 phương án độc lập không bị trội (Pareto-optimal) trên đoạn tuyến/khung giờ này."
                )

    return dep_clock, bucket, portfolio
