PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS corridors (
    corridor_id TEXT PRIMARY KEY,
    name_vi TEXT NOT NULL,
    origin_name TEXT NOT NULL,
    dest_name TEXT NOT NULL,
    origin_aliases_json TEXT NOT NULL,
    dest_aliases_json TEXT NOT NULL,
    origin_lat REAL NOT NULL,
    origin_lng REAL NOT NULL,
    dest_lat REAL NOT NULL,
    dest_lng REAL NOT NULL,
    total_spine_km REAL NOT NULL,
    base_vetc_toll_vnd INTEGER NOT NULL DEFAULT 0,
    spine_waypoints_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corridor_branches (
    branch_id TEXT NOT NULL,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    name_vi TEXT NOT NULL,
    diverge_km REAL NOT NULL,
    merge_km REAL NOT NULL,
    toll_delta_vnd INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (corridor_id, branch_id)
);

CREATE TABLE IF NOT EXISTS pois (
    poi_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    branch_id TEXT NOT NULL DEFAULT 'MAIN_SPINE',
    goong_place_id TEXT NOT NULL,
    name_vi TEXT NOT NULL,
    commune_name TEXT NOT NULL,
    category TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    corridor_km REAL NOT NULL,
    direction_allowed TEXT NOT NULL CHECK (direction_allowed IN ('BOTH', 'OUTBOUND', 'INBOUND')),
    sedan_safe INTEGER NOT NULL DEFAULT 1,
    has_ev_charge INTEGER NOT NULL DEFAULT 0,
    midday_sun_trap INTEGER NOT NULL DEFAULT 0,
    is_arrival_anchor INTEGER NOT NULL DEFAULT 0,
    exertion_level TEXT NOT NULL DEFAULT 'LOW' CHECK (exertion_level IN ('LOW', 'MODERATE', 'HIGH')),
    dwell_minutes INTEGER NOT NULL,
    open_minute INTEGER NOT NULL,
    close_minute INTEGER NOT NULL,
    utility_score REAL NOT NULL,
    scenic_score REAL NOT NULL,
    detour_minutes INTEGER NOT NULL,
    hazard_level REAL NOT NULL DEFAULT 0.05,
    archetype_affinity TEXT NOT NULL CHECK (archetype_affinity IN ('EXPRESS', 'ICONIC', 'SCENIC', 'ALL')),
    tradeoff_note_vi TEXT NOT NULL,
    source_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corridor_edges (
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    time_bucket TEXT NOT NULL CHECK (time_bucket IN ('early_morning', 'day_normal', 'peak_congestion', 'rain_evening')),
    drive_minutes INTEGER NOT NULL,
    distance_km REAL NOT NULL,
    hazard_penalty REAL NOT NULL DEFAULT 0.0,
    encoded_polyline TEXT NOT NULL,
    PRIMARY KEY (corridor_id, from_node, to_node, time_bucket)
);

CREATE TABLE IF NOT EXISTS poi_co_occurrence (
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    from_poi_id TEXT NOT NULL REFERENCES pois(poi_id),
    to_poi_id TEXT NOT NULL REFERENCES pois(poi_id),
    synergy_bonus REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (corridor_id, from_poi_id, to_poi_id)
);

CREATE TABLE IF NOT EXISTS trip_sessions (
    session_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    departure_iso TEXT NOT NULL,
    powertrain TEXT NOT NULL CHECK (powertrain IN ('ice', 'ev')),
    sedan_safe_only INTEGER NOT NULL DEFAULT 1,
    active_start_km REAL NOT NULL,
    active_end_km REAL NOT NULL,
    origin_lat REAL,
    origin_lng REAL,
    dest_lat REAL,
    dest_lng REAL,
    locked_poi_ids_json TEXT NOT NULL DEFAULT '[]',
    rejected_poi_ids_json TEXT NOT NULL DEFAULT '[]',
    skip_categories_json TEXT NOT NULL DEFAULT '[]',
    candidate_durations_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pois_corridor_km ON pois(corridor_id, corridor_km);
CREATE INDEX IF NOT EXISTS idx_edges_lookup ON corridor_edges(corridor_id, time_bucket);
