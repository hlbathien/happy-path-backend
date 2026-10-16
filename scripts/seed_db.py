#!/usr/bin/env python3
"""
Atomic SQLite Seed Compiler (Freeze-Spec v3.0):
- Compiles corridors, corridor_branches, pois, poi_co_occurrence, and physically valid corridor_edges.
- Enforces physical branch interchange topology: never generates edges between MAIN_SPINE stops inside (diverge_km, merge_km) and an alternative branch.
- Publishes atomically via os.replace from /tmp staging so concurrent readers never observe a partial database.
"""
import json
import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "data" / "schema.sql"
SEED_JSON_PATH = ROOT / "data" / "seed_corridors.json"
DEFAULT_DB_PATH = ROOT / "data" / "happy_path.db"

TIME_BUCKETS = {
    "early_morning": (62.0, 1.00),
    "day_normal": (52.0, 1.15),
    "peak_congestion": (38.0, 1.45),
    "rain_evening": (42.0, 1.35),
}

def _encode_coords_polyline(coords: list) -> str:
    """Standard Google/Goong Encoded Polyline Algorithm format."""
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

def _is_pair_physically_connected(u_branch: str, u_km: float, v_branch: str, v_km: float, branches_map: dict) -> bool:
    if u_branch == "MAIN_SPINE" and v_branch == "MAIN_SPINE":
        return True
    if u_branch == v_branch:
        return True
    if u_branch != "MAIN_SPINE" and v_branch != "MAIN_SPINE":
        return False
    # One is MAIN_SPINE, one is alternative branch B
    alt_b = u_branch if u_branch != "MAIN_SPINE" else v_branch
    main_km = u_km if u_branch == "MAIN_SPINE" else v_km
    b_meta = branches_map.get(alt_b)
    if not b_meta:
        return False
    # MAIN_SPINE node must lie outside (diverge_km, merge_km)
    return (main_km <= b_meta["diverge_km"]) or (main_km >= b_meta["merge_km"])

def build_database(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = Path(f"/tmp/happy_path_seed_staging_{os.getpid()}.db")
    if tmp_db.exists():
        tmp_db.unlink()

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    seed = json.loads(SEED_JSON_PATH.read_text(encoding="utf-8"))

    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.executescript("PRAGMA journal_mode = DELETE; PRAGMA synchronous = NORMAL;")
        conn.executescript(schema_sql)

        for corr in seed["corridors"]:
            cid = corr["corridor_id"]
            conn.execute(
                """INSERT INTO corridors (
                    corridor_id, name_vi, origin_name, dest_name,
                    origin_aliases_json, dest_aliases_json,
                    origin_lat, origin_lng, dest_lat, dest_lng,
                    total_spine_km, base_vetc_toll_vnd, spine_waypoints_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid, corr["name_vi"], corr["origin_name"], corr["dest_name"],
                    json.dumps(corr["origin_aliases"], ensure_ascii=False),
                    json.dumps(corr["dest_aliases"], ensure_ascii=False),
                    corr["origin_lat"], corr["origin_lng"],
                    corr["dest_lat"], corr["dest_lng"],
                    corr["total_spine_km"], corr.get("base_vetc_toll_vnd", 0),
                    json.dumps(corr["spine_waypoints"]),
                ),
            )

            branches_map = {}
            for br in corr.get("branches", []):
                branches_map[br["branch_id"]] = br
                conn.execute(
                    """INSERT INTO corridor_branches (
                        branch_id, corridor_id, name_vi, diverge_km, merge_km, toll_delta_vnd
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (br["branch_id"], cid, br["name_vi"], br["diverge_km"], br["merge_km"], br["toll_delta_vnd"]),
                )

            pois = corr["pois"]
            for p in pois:
                conn.execute(
                    """INSERT INTO pois (
                        poi_id, corridor_id, branch_id, goong_place_id, name_vi, commune_name, category,
                        lat, lng, corridor_km, direction_allowed, sedan_safe,
                        has_ev_charge, midday_sun_trap, is_arrival_anchor, exertion_level,
                        dwell_minutes, open_minute, close_minute, utility_score, scenic_score,
                        detour_minutes, hazard_level, archetype_affinity, tradeoff_note_vi, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p["poi_id"], cid, p.get("branch_id", "MAIN_SPINE"), p["goong_place_id"],
                        p["name_vi"], p["commune_name"], p["category"],
                        p["lat"], p["lng"], p["corridor_km"], p["direction_allowed"], p["sedan_safe"],
                        p.get("has_ev_charge", 0), p.get("midday_sun_trap", 0), p.get("is_arrival_anchor", 0),
                        p.get("exertion_level", "LOW"),
                        p["dwell_minutes"], p["open_minute"], p["close_minute"],
                        p["utility_score"], p["scenic_score"], p["detour_minutes"],
                        p["hazard_level"], p["archetype_affinity"], p["tradeoff_note_vi"], p["source_url"],
                    ),
                )

            for co in corr.get("co_occurrence", []):
                conn.execute(
                    """INSERT INTO poi_co_occurrence (corridor_id, from_poi_id, to_poi_id, synergy_bonus)
                       VALUES (?, ?, ?, ?)""",
                    (cid, co["from_poi_id"], co["to_poi_id"], co["synergy_bonus"]),
                )

            nodes = [("ORIGIN", 0.0, 0, 0.0, "MAIN_SPINE", corr["origin_lat"], corr["origin_lng"])]
            for p in pois:
                nodes.append((p["poi_id"], p["corridor_km"], p["detour_minutes"], p["hazard_level"], p["branch_id"], p["lat"], p["lng"]))
            nodes.append(("DEST", corr["total_spine_km"], 0, 0.0, "MAIN_SPINE", corr["dest_lat"], corr["dest_lng"]))

            for u_id, u_km, u_det, u_haz, u_br, u_lat, u_lng in nodes:
                for v_id, v_km, v_det, v_haz, v_br, v_lat, v_lng in nodes:
                    if u_id == v_id:
                        continue
                    if not _is_pair_physically_connected(u_br, u_km, v_br, v_km, branches_map):
                        continue
                    dist = abs(v_km - u_km)
                    if dist < 0.5:
                        dist = 2.0
                    poly = _encode_coords_polyline([(u_lat, u_lng), (v_lat, v_lng)])
                    for bucket, (speed_kmh, detour_mult) in TIME_BUCKETS.items():
                        base_drive = (dist / speed_kmh) * 60.0
                        detour_cost = (v_det * 0.65) * detour_mult
                        drive_min = max(5, int(round(base_drive + detour_cost)))
                        haz = round(max(u_haz, v_haz) * (1.5 if bucket == "rain_evening" else 1.0), 3)
                        conn.execute(
                            """INSERT INTO corridor_edges (
                                corridor_id, from_node, to_node, time_bucket,
                                drive_minutes, distance_km, hazard_penalty, encoded_polyline
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (cid, u_id, v_id, bucket, drive_min, round(dist, 2), haz, poly),
                        )
        conn.commit()
    finally:
        conn.close()

    # Atomic publication: try os.replace if on same filesystem, else write to temp in dest dir and os.replace
    dest_tmp = db_path.parent / f".{db_path.name}.tmp.{os.getpid()}"
    shutil.copy2(str(tmp_db), str(dest_tmp))
    os.replace(str(dest_tmp), str(db_path))
    tmp_db.unlink(missing_ok=True)
    print(f"[OK] Atomically built and published SQLite database at {db_path}")

if __name__ == "__main__":
    build_database()
