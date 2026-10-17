from datetime import datetime
import re
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, root_validator, validator

VALID_CATEGORIES = {
    "rest_stop",
    "dining",
    "cafe_scenic",
    "viewpoint",
    "attraction",
    "temple_culture",
}

CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

class VehicleMode(str, Enum):
    car = "car"
    suv = "suv"

class PowertrainMode(str, Enum):
    ice = "ice"
    ev = "ev"

class CandidateArchetype(str, Enum):
    P1_EXPRESS = "P1_EXPRESS"
    P2_ICONIC = "P2_ICONIC"
    P3_SCENIC = "P3_SCENIC"

class MutationAction(str, Enum):
    REMOVE_STOP = "REMOVE_STOP"
    LOCK_STOP = "LOCK_STOP"
    SKIP_CATEGORY = "SKIP_CATEGORY"
    SHIFT_DEPARTURE = "SHIFT_DEPARTURE"

class Coordinate(BaseModel):
    lat: float = Field(..., ge=8.0, le=23.5, description="Latitude in decimal degrees (Vietnam bounds: 8.0 to 23.5)")
    lng: float = Field(..., ge=102.0, le=110.0, description="Longitude in decimal degrees (Vietnam bounds: 102.0 to 110.0)")

    def to_goong_str(self) -> str:
        return f"{self.lat:.6f},{self.lng:.6f}"

class PlanRequest(BaseModel):
    origin_query: str = Field(..., min_length=2, example="Ho Chi Minh City")
    destination_query: str = Field(..., min_length=2, example="Da Lat")
    origin_coords: Optional[Coordinate] = None
    destination_coords: Optional[Coordinate] = None
    departure_iso: str = Field(
        default="2026-10-16T06:00:00+07:00",
        description="ISO-8601 departure timestamp or HH:MM (Vietnam UTC+7)",
    )
    vehicle_mode: VehicleMode = Field(default=VehicleMode.car)
    powertrain: PowertrainMode = Field(default=PowertrainMode.ice)
    sedan_safe_only: bool = Field(default=True)

    @validator("departure_iso")
    def validate_departure_iso(cls, v: str) -> str:
        s = (v or "").strip()
        if CLOCK_RE.match(s):
            return s
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
            return s
        except Exception as exc:
            raise ValueError(f"Invalid ISO-8601 departure timestamp: {v!r}") from exc

class PlannedStop(BaseModel):
    stop_order: int
    poi_id: str
    branch_id: str
    goong_place_id: str
    name_vi: str
    commune_name: str
    category: str
    exertion_level: str
    has_ev_charge: bool
    is_arrival_anchor: bool
    coordinates: Coordinate
    corridor_km_from_hcm: float
    progress_km_from_origin: float
    arrival_clock: str
    service_start_clock: str
    wait_minutes: int = 0
    departure_clock: str
    dwell_minutes: int
    detour_minutes: int
    utility_score: float
    scenic_score: float
    tradeoff_note_vi: str
    source_url: str
    open_minute: int
    close_minute: int
    is_24h: bool
    midday_sun_trap: bool
    is_locked: bool = False
    ev_max_kw: int = 0
    ev_stall_count: int = 0
    ev_connector_type: str = 'NONE'

class ParetoCandidate(BaseModel):
    candidate_id: CandidateArchetype
    label: str
    corridor_id: str
    active_branch_id: str
    direction: str
    powertrain: PowertrainMode
    total_drive_minutes: int
    total_dwell_minutes: int
    total_wait_minutes: int = 0
    total_duration_minutes: int
    total_detour_minutes: int
    estimated_vetc_toll_vnd: int
    ev_charging_satisfied: bool
    aggregate_utility_score: float
    aggregate_scenic_score: float
    hazard_risk_score: float
    tradeoff_summary_vi: str
    encoded_polyline: str
    stops: List[PlannedStop]
    warnings: List[str] = Field(default_factory=list)
    max_wait_minutes: int = 30

class PlanResponse(BaseModel):
    session_id: str
    corridor_id: str
    corridor_name_vi: str
    direction: str
    active_start_km: float
    active_end_km: float
    departure_clock: str
    time_bucket: str
    powertrain: PowertrainMode
    candidates: List[ParetoCandidate]

class MutateRequest(BaseModel):
    session_id: str = Field(..., min_length=4, description="Stateful trip session ID returned by /plan")
    corridor_id: Optional[str] = Field(default=None)
    direction: Optional[str] = Field(default=None)
    candidate_id: CandidateArchetype = Field(default=CandidateArchetype.P2_ICONIC)
    action: MutationAction
    target_poi_id: Optional[str] = None
    target_category: Optional[str] = None
    new_departure_iso: Optional[str] = None
    locked_poi_ids: List[str] = Field(default_factory=list)
    rejected_poi_ids: List[str] = Field(default_factory=list)

    @root_validator(skip_on_failure=True)
    def validate_action_required_fields(cls, values: dict) -> dict:
        action = values.get("action")
        target_poi = (values.get("target_poi_id") or "").strip()
        target_cat = (values.get("target_category") or "").strip()
        new_dep = (values.get("new_departure_iso") or "").strip()

        if action in (MutationAction.REMOVE_STOP, MutationAction.LOCK_STOP):
            if not target_poi:
                raise ValueError(f"{action} requires a non-empty target_poi_id.")
            values["target_poi_id"] = target_poi
        elif action == MutationAction.SKIP_CATEGORY:
            if not target_cat or target_cat not in VALID_CATEGORIES:
                raise ValueError(f"SKIP_CATEGORY requires target_category in {sorted(VALID_CATEGORIES)}.")
            values["target_category"] = target_cat
        elif action == MutationAction.SHIFT_DEPARTURE:
            if not new_dep:
                raise ValueError("SHIFT_DEPARTURE requires a non-empty new_departure_iso.")
            if not CLOCK_RE.match(new_dep):
                try:
                    datetime.fromisoformat(new_dep.replace("Z", "+00:00"))
                except Exception as exc:
                    raise ValueError(f"Invalid new_departure_iso: {new_dep!r}") from exc
            values["new_departure_iso"] = new_dep
        return values

class MutateResponse(BaseModel):
    session_id: str
    mutated_candidate: ParetoCandidate
    duration_delta_minutes: int
    cumulative_locked_poi_ids: List[str]
    cumulative_rejected_poi_ids: List[str]
    delta_summary_vi: str

class CorridorHealthItem(BaseModel):
    corridor_id: str
    name_vi: str
    total_spine_km: float
    verified_poi_count: int
    branches: List[str]
    bidirectional_ready: bool

class CorridorHealthResponse(BaseModel):
    status: str
    engine: str
    corridors: List[CorridorHealthItem]
