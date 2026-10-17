import fcntl
import hashlib
import json
import math
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from happy_path.schemas.itinerary import Coordinate

SQLITE_HEADER = b"SQLite format 3\x00"

def normalize_vn(text: str) -> str:
    text = unicodedata.normalize("NFC", text).strip().lower()
    text = text.replace("đ", "d")
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))

def encode_polyline(coords: List[Tuple[float, float]]) -> str:
    result = []
    prev_lat = 0
    prev_lng = 0
    for lat, lng in coords:
        ilat = int(round(lat * 1e5))
        ilng = int(round(lng * 1e5))
        for val in (ilat - prev_lat, ilng - prev_lng):
            v = ~(val << 1) if val < 0 else (val << 1)
            while v >= 0x20:
                result.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            result.append(chr(v + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(result)

def decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0
    n = len(encoded)
    while index < n:
        for is_lng in (False, True):
            shift = 0
            result = 0
            while True:
                if index >= n:
                    return points
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += d
            else:
                lat += d
        points.append((lat / 1e5, lng / 1e5))
    return points

@dataclass(frozen=True)
class BranchMeta:
    branch_id: str
    corridor_id: str
    name_vi: str
    diverge_km: float
    merge_km: float
    toll_delta_vnd: int

@dataclass(frozen=True)
class CorridorMeta:
    corridor_id: str
    name_vi: str
    origin_name: str
    dest_name: str
    origin_aliases: List[str]
    dest_aliases: List[str]
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    total_spine_km: float
    base_vetc_toll_vnd: int
    spine_waypoints: List[Tuple[float, float, float]]

@dataclass(frozen=True)
class POINode:
    poi_id: str
    corridor_id: str
    branch_id: str
    goong_place_id: str
    name_vi: str
    commune_name: str
    category: str
    lat: float
    lng: float
    corridor_km: float
    direction_allowed: str
    sedan_safe: bool
    has_ev_charge: bool
    midday_sun_trap: bool
    is_arrival_anchor: bool
    exertion_level: str
    dwell_minutes: int
    open_minute: int
    close_minute: int
    utility_score: float
    scenic_score: float
    detour_minutes: int
    hazard_level: float
    archetype_affinity: str
    tradeoff_note_vi: str
    source_url: str

@dataclass
class SessionRecord:
    session_id: str
    corridor_id: str
    direction: str
    departure_iso: str
    powertrain: str
    sedan_safe_only: bool
    active_start_km: float
    active_end_km: float
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    locked_poi_ids: Set[str] = field(default_factory=set)
    rejected_poi_ids: Set[str] = field(default_factory=set)
    skip_categories: Set[str] = field(default_factory=set)
    candidate_durations: Dict[str, int] = field(default_factory=dict)

    def clone(self) -> "SessionRecord":
        return SessionRecord(
            session_id=self.session_id,
            corridor_id=self.corridor_id,
            direction=self.direction,
            departure_iso=self.departure_iso,
            powertrain=self.powertrain,
            sedan_safe_only=self.sedan_safe_only,
            active_start_km=self.active_start_km,
            active_end_km=self.active_end_km,
            origin_lat=self.origin_lat,
            origin_lng=self.origin_lng,
            dest_lat=self.dest_lat,
            dest_lng=self.dest_lng,
            locked_poi_ids=set(self.locked_poi_ids),
            rejected_poi_ids=set(self.rejected_poi_ids),
            skip_categories=set(self.skip_categories),
            candidate_durations=dict(self.candidate_durations),
        )

class CorridorRegistry:
    """
    FUSE-safe, restart-durable, multi-worker SQLite registry:
    - Validates SQLite magic header and required tables on startup (raises RuntimeError if missing/corrupt).
    - Probes live BEGIN IMMEDIATE locking on db_path; uses db_path directly if locking succeeds.
    - If mounted on FUSE without lock support, syncs to a deterministic shared /tmp/happy_path_shared_<hash>.db
      that persists across restarts and is shared across all worker processes.
    """
    def __init__(self, db_path: Path):
        self.source_db_path = Path(db_path).resolve()
        self.runtime_db_path = self._resolve_runtime_db_path(self.source_db_path)
        self.corridors: Dict[str, CorridorMeta] = {}
        self.branches_by_corridor: Dict[str, Dict[str, BranchMeta]] = {}
        self.pois_by_corridor: Dict[str, List[POINode]] = {}
        self.pois_by_id: Dict[str, POINode] = {}
        self.edges: Dict[Tuple[str, str, str, str], Tuple[int, float, float, str]] = {}
        self.co_occurrence: Dict[Tuple[str, str, str], float] = {}
        try:
            self._load()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"Corrupt SQLite schema in {self.runtime_db_path}: {exc}") from exc

    @staticmethod
    def _verify_sqlite_file(path: Path) -> None:
        if not path.exists() or path.stat().st_size < 100:
            raise RuntimeError(f"Database file missing or empty: {path}")
        with path.open("rb") as f:
            header = f.read(16)
        if header != SQLITE_HEADER:
            raise RuntimeError(f"Corrupt database header in {path}")

    def _resolve_runtime_db_path(self, src_path: Path) -> Path:
        self._verify_sqlite_file(src_path)
        try:
            conn = sqlite3.connect(str(src_path), timeout=5.0)
            try:
                conn.execute("PRAGMA journal_mode = DELETE;")
                conn.execute("PRAGMA synchronous = NORMAL;")
                conn.execute("PRAGMA busy_timeout = 5000;")
                for table in ("corridors", "corridor_branches", "pois", "corridor_edges", "poi_co_occurrence", "trip_sessions"):
                    conn.execute(f"SELECT * FROM {table} LIMIT 0;")
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                except sqlite3.OperationalError:
                    pass  # Only a failed lock acquisition selects shared storage.
                else:
                    conn.execute("ROLLBACK;")
                    return src_path
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"Corrupt SQLite schema in {src_path}: {exc}") from exc

        path_hash = hashlib.md5(str(src_path).encode("utf-8")).hexdigest()
        shared_tmp = Path(f"/tmp/happy_path_shared_{path_hash}.db")
        # Serialize first publication so another worker cannot replace an active store.
        with shared_tmp.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # Once published, the shared database owns durable session state.
            # Source timestamps must never overwrite committed mutations on restart.
            if not shared_tmp.exists():
                staging = shared_tmp.with_suffix(".tmp")
                try:
                    shutil.copy2(str(src_path), str(staging))
                    staging.replace(shared_tmp)
                finally:
                    staging.unlink(missing_ok=True)
            self._verify_sqlite_file(shared_tmp)
        return shared_tmp

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.runtime_db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _load(self) -> None:
        conn = self._connect()
        try:
            try:
                c_rows = conn.execute("SELECT * FROM corridors").fetchall()
                b_rows = conn.execute("SELECT * FROM corridor_branches").fetchall()
                p_rows = conn.execute("SELECT * FROM pois ORDER BY corridor_km ASC").fetchall()
                e_rows = conn.execute("SELECT * FROM corridor_edges").fetchall()
                co_rows = conn.execute("SELECT * FROM poi_co_occurrence").fetchall()
                conn.execute("SELECT * FROM trip_sessions LIMIT 0;")
            except sqlite3.Error as exc:
                raise RuntimeError(f"Failed loading required tables from {self.runtime_db_path}: {exc}") from exc

            if not c_rows:
                raise RuntimeError(f"Zero corridors found in {self.runtime_db_path}")

            for row in c_rows:
                wps = [tuple(pt) for pt in json.loads(row["spine_waypoints_json"])]
                meta = CorridorMeta(
                    corridor_id=row["corridor_id"],
                    name_vi=row["name_vi"],
                    origin_name=row["origin_name"],
                    dest_name=row["dest_name"],
                    origin_aliases=[normalize_vn(a) for a in json.loads(row["origin_aliases_json"])],
                    dest_aliases=[normalize_vn(a) for a in json.loads(row["dest_aliases_json"])],
                    origin_lat=float(row["origin_lat"]),
                    origin_lng=float(row["origin_lng"]),
                    dest_lat=float(row["dest_lat"]),
                    dest_lng=float(row["dest_lng"]),
                    total_spine_km=float(row["total_spine_km"]),
                    base_vetc_toll_vnd=int(row["base_vetc_toll_vnd"]),
                    spine_waypoints=[(float(lat), float(lng), float(km)) for lat, lng, km in wps],
                )
                self.corridors[meta.corridor_id] = meta
                self.branches_by_corridor[meta.corridor_id] = {}
                self.pois_by_corridor[meta.corridor_id] = []

            for row in b_rows:
                bm = BranchMeta(
                    branch_id=row["branch_id"],
                    corridor_id=row["corridor_id"],
                    name_vi=row["name_vi"],
                    diverge_km=float(row["diverge_km"]),
                    merge_km=float(row["merge_km"]),
                    toll_delta_vnd=int(row["toll_delta_vnd"]),
                )
                self.branches_by_corridor[bm.corridor_id][bm.branch_id] = bm

            for row in p_rows:
                node = POINode(
                    poi_id=row["poi_id"],
                    corridor_id=row["corridor_id"],
                    branch_id=row["branch_id"],
                    goong_place_id=row["goong_place_id"],
                    name_vi=row["name_vi"],
                    commune_name=row["commune_name"],
                    category=row["category"],
                    lat=float(row["lat"]),
                    lng=float(row["lng"]),
                    corridor_km=float(row["corridor_km"]),
                    direction_allowed=row["direction_allowed"],
                    sedan_safe=bool(row["sedan_safe"]),
                    has_ev_charge=bool(row["has_ev_charge"]),
                    midday_sun_trap=bool(row["midday_sun_trap"]),
                    is_arrival_anchor=bool(row["is_arrival_anchor"]),
                    exertion_level=row["exertion_level"],
                    dwell_minutes=int(row["dwell_minutes"]),
                    open_minute=int(row["open_minute"]),
                    close_minute=int(row["close_minute"]),
                    utility_score=float(row["utility_score"]),
                    scenic_score=float(row["scenic_score"]),
                    detour_minutes=int(row["detour_minutes"]),
                    hazard_level=float(row["hazard_level"]),
                    archetype_affinity=row["archetype_affinity"],
                    tradeoff_note_vi=row["tradeoff_note_vi"],
                    source_url=row["source_url"],
                )
                self.pois_by_corridor[node.corridor_id].append(node)
                self.pois_by_id[node.poi_id] = node

            for row in e_rows:
                key = (row["corridor_id"], row["from_node"], row["to_node"], row["time_bucket"])
                self.edges[key] = (
                    int(row["drive_minutes"]),
                    float(row["distance_km"]),
                    float(row["hazard_penalty"]),
                    row["encoded_polyline"],
                )

            for row in co_rows:
                ckey = (row["corridor_id"], row["from_poi_id"], row["to_poi_id"])
                self.co_occurrence[ckey] = float(row["synergy_bonus"])
        finally:
            conn.close()

    def reload(self) -> None:
        self.corridors.clear()
        self.branches_by_corridor.clear()
        self.pois_by_corridor.clear()
        self.pois_by_id.clear()
        self.edges.clear()
        self.co_occurrence.clear()
        self._load()

    def resolve_corridor_and_direction(self, origin_q: str, dest_q: str) -> Tuple[CorridorMeta, str]:
        oq = normalize_vn(origin_q)
        dq = normalize_vn(dest_q)
        for meta in self.corridors.values():
            orig_match = any(a in oq or oq in a for a in meta.origin_aliases)
            dest_match = any(a in dq or dq in a for a in meta.dest_aliases)
            if orig_match and dest_match:
                return meta, "outbound"
            rev_orig = any(a in oq or oq in a for a in meta.dest_aliases)
            rev_dest = any(a in dq or dq in a for a in meta.origin_aliases)
            if rev_orig and rev_dest:
                return meta, "inbound"
        raise ValueError(
            f"Unsupported corridor '{origin_q} -> {dest_q}'. Supported corridors: "
            "HCMC <-> Da Lat, HCMC <-> Nha Trang, HCMC <-> Vung Tau."
        )

    def project_coordinate_to_progress_km(
        self, corridor: CorridorMeta, direction: str, coord: Optional[Coordinate], is_origin: bool
    ) -> float:
        if coord is None:
            return 0.0 if is_origin else corridor.total_spine_km

        # Piecewise projection onto corridor.spine_waypoints
        wps = corridor.spine_waypoints
        best_dist = float("inf")
        best_hcm_km = 0.0

        for idx in range(len(wps) - 1):
            lat1, lng1, km1 = wps[idx]
            lat2, lng2, km2 = wps[idx + 1]
            dx = lng2 - lng1
            dy = lat2 - lat1
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq < 1e-12:
                t = 0.0
            else:
                t = ((coord.lng - lng1) * dx + (coord.lat - lat1) * dy) / seg_len_sq
                t = max(0.0, min(1.0, t))
            proj_lat = lat1 + t * dy
            proj_lng = lng1 + t * dx
            d = haversine_km(coord.lat, coord.lng, proj_lat, proj_lng)
            if d < best_dist:
                best_dist = d
                best_hcm_km = km1 + t * (km2 - km1)

        if direction == "outbound":
            return round(max(0.0, min(corridor.total_spine_km, best_hcm_km)), 1)
        return round(max(0.0, min(corridor.total_spine_km, corridor.total_spine_km - best_hcm_km)), 1)

    def build_sliced_polyline(
        self,
        corridor: CorridorMeta,
        direction: str,
        origin_coord: Coordinate,
        dest_coord: Coordinate,
        stops: List[POINode],
        active_start_km: float,
        active_end_km: float,
    ) -> str:
        """
        Constructs a road-following multi-vertex polyline that:
        1. Starts at exact (origin_coord.lat, origin_coord.lng)
        2. Includes intermediate corridor spine waypoints and every planned stop in travel order
        3. Ends at exact (dest_coord.lat, dest_coord.lng)
        """
        pts: List[Tuple[float, float]] = [(origin_coord.lat, origin_coord.lng)]
        wps_directional = []
        for lat, lng, hcm_km in corridor.spine_waypoints:
            prog_km = hcm_km if direction == "outbound" else (corridor.total_spine_km - hcm_km)
            if active_start_km < prog_km < active_end_km:
                wps_directional.append((prog_km, lat, lng))
        wps_directional.sort(key=lambda item: item[0])

        stop_items = []
        for s in stops:
            s_prog = s.corridor_km if direction == "outbound" else (corridor.total_spine_km - s.corridor_km)
            stop_items.append((s_prog, s.lat, s.lng))

        # Merge stops and intermediate spine waypoints in travel order.
        prev_km = active_start_km
        for s_prog, s_lat, s_lng in stop_items:
            for wp_km, wp_lat, wp_lng in wps_directional:
                if prev_km < wp_km < s_prog:
                    pts.append((wp_lat, wp_lng))
            pts.append((s_lat, s_lng))
            prev_km = s_prog

        for wp_km, wp_lat, wp_lng in wps_directional:
            if prev_km < wp_km < active_end_km:
                pts.append((wp_lat, wp_lng))

        pts.append((dest_coord.lat, dest_coord.lng))
        return encode_polyline(pts)

    def save_session(self, record: SessionRecord) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """INSERT OR REPLACE INTO trip_sessions (
                    session_id, corridor_id, direction, departure_iso, powertrain,
                    sedan_safe_only, active_start_km, active_end_km,
                    origin_lat, origin_lng, dest_lat, dest_lng,
                    locked_poi_ids_json, rejected_poi_ids_json, skip_categories_json,
                    candidate_durations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.session_id,
                    record.corridor_id,
                    record.direction,
                    record.departure_iso,
                    record.powertrain,
                    1 if record.sedan_safe_only else 0,
                    record.active_start_km,
                    record.active_end_km,
                    record.origin_lat,
                    record.origin_lng,
                    record.dest_lat,
                    record.dest_lng,
                    json.dumps(sorted(record.locked_poi_ids)),
                    json.dumps(sorted(record.rejected_poi_ids)),
                    json.dumps(sorted(record.skip_categories)),
                    json.dumps(record.candidate_durations),
                ),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def load_session(self, session_id: str) -> Optional[SessionRecord]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM trip_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            return SessionRecord(
                session_id=row["session_id"],
                corridor_id=row["corridor_id"],
                direction=row["direction"],
                departure_iso=row["departure_iso"],
                powertrain=row["powertrain"],
                sedan_safe_only=bool(row["sedan_safe_only"]),
                active_start_km=float(row["active_start_km"]),
                active_end_km=float(row["active_end_km"]),
                origin_lat=float(row["origin_lat"]) if row["origin_lat"] is not None else None,
                origin_lng=float(row["origin_lng"]) if row["origin_lng"] is not None else None,
                dest_lat=float(row["dest_lat"]) if row["dest_lat"] is not None else None,
                dest_lng=float(row["dest_lng"]) if row["dest_lng"] is not None else None,
                locked_poi_ids=set(json.loads(row["locked_poi_ids_json"])),
                rejected_poi_ids=set(json.loads(row["rejected_poi_ids_json"])),
                skip_categories=set(json.loads(row["skip_categories_json"])),
                candidate_durations={k: int(v) for k, v in json.loads(row["candidate_durations_json"]).items()},
            )
        finally:
            conn.close()
