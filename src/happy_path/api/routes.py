import sqlite3
import uuid
from fastapi import APIRouter, HTTPException, Request
from happy_path.clients.goong import GoongClient
from happy_path.core.graph import CorridorRegistry, SessionRecord
from happy_path.core.solver import solve_pareto_portfolio
from happy_path.schemas.itinerary import (
    Coordinate,
    CorridorHealthItem,
    CorridorHealthResponse,
    MutateRequest,
    MutateResponse,
    MutationAction,
    PlanRequest,
    PlanResponse,
    PowertrainMode,
)

router = APIRouter()

@router.post(
    "/v1/itineraries/plan",
    response_model=PlanResponse,
    responses={400: {"description": "Invalid corridor or slice"}, 409: {"description": "No feasible route under constraints"}},
    tags=["Autopilot Planner"],
)
async def plan_itineraries(payload: PlanRequest, request: Request) -> PlanResponse:
    registry: CorridorRegistry = request.app.state.registry
    goong: GoongClient = request.app.state.goong

    try:
        corridor, direction = registry.resolve_corridor_and_direction(
            payload.origin_query, payload.destination_query
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    start_km = registry.project_coordinate_to_progress_km(corridor, direction, payload.origin_coords, is_origin=True)
    end_km = registry.project_coordinate_to_progress_km(corridor, direction, payload.destination_coords, is_origin=False)
    if end_km - start_km < 10.0:
        raise HTTPException(
            status_code=400,
            detail=f"Requested slice [{start_km:.1f} km, {end_km:.1f} km] is shorter than minimum 10.0 km drivable span.",
        )

    orig_coord = payload.origin_coords or (
        Coordinate(lat=corridor.origin_lat, lng=corridor.origin_lng)
        if direction == "outbound"
        else Coordinate(lat=corridor.dest_lat, lng=corridor.dest_lng)
    )
    dest_coord = payload.destination_coords or (
        Coordinate(lat=corridor.dest_lat, lng=corridor.dest_lng)
        if direction == "outbound"
        else Coordinate(lat=corridor.origin_lat, lng=corridor.origin_lng)
    )

    dep_clock, bucket, candidates = solve_pareto_portfolio(
        registry=registry,
        corridor=corridor,
        direction=direction,
        departure_iso=payload.departure_iso,
        powertrain=payload.powertrain,
        sedan_safe_only=payload.sedan_safe_only,
        active_start_km=start_km,
        active_end_km=end_km,
        origin_coord=orig_coord,
        dest_coord=dest_coord,
    )

    if not candidates:
        raise HTTPException(status_code=409, detail="Không tìm thấy lộ trình khả thi thỏa mãn các ràng buộc an toàn.")

    await goong.decorate_portfolio(
        orig_coord, dest_coord, candidates, payload.vehicle_mode.value, payload.departure_iso
    )

    session_id = str(uuid.uuid4())
    cand_durations = {c.candidate_id.value: c.total_duration_minutes for c in candidates}
    registry.save_session(
        SessionRecord(
            session_id=session_id,
            corridor_id=corridor.corridor_id,
            direction=direction,
            departure_iso=payload.departure_iso,
            powertrain=payload.powertrain.value,
            sedan_safe_only=payload.sedan_safe_only,
            active_start_km=start_km,
            active_end_km=end_km,
            origin_lat=orig_coord.lat,
            origin_lng=orig_coord.lng,
            dest_lat=dest_coord.lat,
            dest_lng=dest_coord.lng,
            locked_poi_ids=set(),
            rejected_poi_ids=set(),
            skip_categories=set(),
            candidate_durations=cand_durations,
        )
    )

    return PlanResponse(
        session_id=session_id,
        corridor_id=corridor.corridor_id,
        corridor_name_vi=corridor.name_vi,
        direction=direction,
        active_start_km=start_km,
        active_end_km=end_km,
        departure_clock=dep_clock,
        time_bucket=bucket,
        powertrain=payload.powertrain,
        candidates=candidates,
    )

@router.post(
    "/v1/itineraries/mutate",
    response_model=MutateResponse,
    responses={
        404: {"description": "Session not found"},
        409: {"description": "Infeasible mutation rolled back without modifying session"},
        422: {"description": "Invalid mutation action parameter"},
    },
    tags=["Autopilot Planner"],
)
async def mutate_itinerary(payload: MutateRequest, request: Request) -> MutateResponse:
    registry: CorridorRegistry = request.app.state.registry
    goong: GoongClient = request.app.state.goong

    persisted_session = registry.load_session(payload.session_id)
    if persisted_session is None:
        if not payload.corridor_id or payload.corridor_id not in registry.corridors:
            raise HTTPException(status_code=404, detail=f"Session '{payload.session_id}' not found.")
        c_meta = registry.corridors[payload.corridor_id]
        persisted_session = SessionRecord(
            session_id=payload.session_id,
            corridor_id=c_meta.corridor_id,
            direction=payload.direction or "outbound",
            departure_iso=payload.new_departure_iso or "2026-10-16T06:00:00+07:00",
            powertrain="ice",
            sedan_safe_only=True,
            active_start_km=0.0,
            active_end_km=c_meta.total_spine_km,
            origin_lat=c_meta.origin_lat,
            origin_lng=c_meta.origin_lng,
            dest_lat=c_meta.dest_lat,
            dest_lng=c_meta.dest_lng,
        )

    # Atomic Copy-on-Write: mutate an in-memory clone first! Never persist before feasibility is verified.
    staged = persisted_session.clone()
    corridor = registry.corridors[staged.corridor_id]
    valid_corridor_poi_ids = {p.poi_id for p in registry.pois_by_corridor[corridor.corridor_id]}

    if payload.action in (MutationAction.REMOVE_STOP, MutationAction.LOCK_STOP):
        if payload.target_poi_id not in valid_corridor_poi_ids:
            raise HTTPException(
                status_code=422,
                detail=f"target_poi_id '{payload.target_poi_id}' does not belong to corridor '{corridor.corridor_id}'.",
            )

    staged.locked_poi_ids.update(payload.locked_poi_ids)
    staged.rejected_poi_ids.update(payload.rejected_poi_ids)

    if payload.action == MutationAction.REMOVE_STOP and payload.target_poi_id:
        staged.rejected_poi_ids.add(payload.target_poi_id)
        staged.locked_poi_ids.discard(payload.target_poi_id)
    elif payload.action == MutationAction.LOCK_STOP and payload.target_poi_id:
        staged.locked_poi_ids.add(payload.target_poi_id)
        staged.rejected_poi_ids.discard(payload.target_poi_id)
    elif payload.action == MutationAction.SKIP_CATEGORY and payload.target_category:
        staged.skip_categories.add(payload.target_category)
    elif payload.action == MutationAction.SHIFT_DEPARTURE and payload.new_departure_iso:
        staged.departure_iso = payload.new_departure_iso

    orig_coord = (
        Coordinate(lat=staged.origin_lat, lng=staged.origin_lng)
        if staged.origin_lat is not None and staged.origin_lng is not None
        else Coordinate(lat=corridor.origin_lat, lng=corridor.origin_lng)
    )
    dest_coord = (
        Coordinate(lat=staged.dest_lat, lng=staged.dest_lng)
        if staged.dest_lat is not None and staged.dest_lng is not None
        else Coordinate(lat=corridor.dest_lat, lng=corridor.dest_lng)
    )

    _, _, candidates = solve_pareto_portfolio(
        registry=registry,
        corridor=corridor,
        direction=staged.direction,
        departure_iso=staged.departure_iso,
        powertrain=PowertrainMode(staged.powertrain),
        sedan_safe_only=staged.sedan_safe_only,
        active_start_km=staged.active_start_km,
        active_end_km=staged.active_end_km,
        origin_coord=orig_coord,
        dest_coord=dest_coord,
        locked_ids=staged.locked_poi_ids,
        rejected_ids=staged.rejected_poi_ids,
        skip_categories=staged.skip_categories,
        preferred_candidate_id=payload.candidate_id,
    )

    # Strict Archetype Integrity: never silently substitute P2_ICONIC when P1_EXPRESS was requested!
    matched = next((c for c in candidates if c.candidate_id == payload.candidate_id), None)
    if matched is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Không thể thực hiện thao tác {payload.action.value} cho phương án {payload.candidate_id.value} "
                "(vi phạm tính liên tục nhánh tuyến, giới hạn vận động, trạm sạc EV hoặc khung giờ mở cửa). "
                "Phiên làm việc đã được giữ nguyên an toàn."
            ),
        )

    await goong.decorate_candidate_polyline(
        orig_coord, dest_coord, matched, vehicle="car", departure_iso=staged.departure_iso
    )

    prev_duration = staged.candidate_durations.get(
        matched.candidate_id.value, matched.total_duration_minutes
    )
    duration_delta = matched.total_duration_minutes - prev_duration
    staged.candidate_durations[matched.candidate_id.value] = matched.total_duration_minutes

    # Commit atomically ONLY after feasibility and archetype integrity succeed
    registry.save_session(staged)

    sign_str = f"+{duration_delta}" if duration_delta >= 0 else f"{duration_delta}"
    delta_msg = (
        f"Đã cập nhật phiên {staged.session_id[:8]} ({payload.action.value}) cho {matched.candidate_id.value}: "
        f"gồm {len(matched.stops)} điểm dừng, tổng thời gian {matched.total_duration_minutes} phút "
        f"(chênh lệch {sign_str} phút so với phương án trước)."
    )
    return MutateResponse(
        session_id=staged.session_id,
        mutated_candidate=matched,
        duration_delta_minutes=duration_delta,
        cumulative_locked_poi_ids=sorted(staged.locked_poi_ids),
        cumulative_rejected_poi_ids=sorted(staged.rejected_poi_ids),
        delta_summary_vi=delta_msg,
    )

@router.get("/v1/corridors/health", response_model=CorridorHealthResponse, tags=["System Health"])
async def corridor_health(request: Request) -> CorridorHealthResponse:
    registry: CorridorRegistry = request.app.state.registry
    registry.reload()
    with sqlite3.connect(registry.runtime_db_path) as conn:
        verified_counts = dict(conn.execute(
            "SELECT p.corridor_id,COUNT(*) FROM pois p JOIN poi_verifications v USING(poi_id) GROUP BY p.corridor_id"
        ))
    items = []
    for cid, meta in registry.corridors.items():
        branches = sorted(registry.branches_by_corridor.get(cid, {}).keys())
        items.append(
            CorridorHealthItem(
                corridor_id=cid,
                name_vi=meta.name_vi,
                total_spine_km=meta.total_spine_km,
                verified_poi_count=verified_counts.get(cid, 0),
                branches=branches,
                bidirectional_ready=True,
            )
        )
    return CorridorHealthResponse(
        status="ok",
        engine="braided_interchange_dag_sqlite_v3_freeze",
        corridors=items,
    )
