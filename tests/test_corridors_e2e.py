import asyncio
import hashlib
import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from happy_path.clients.goong import GoongClient
from happy_path.config import DB_PATH
from happy_path.core.graph import CorridorRegistry, decode_polyline
from happy_path.core.ingestion import (
    RawScrapedCandidate,
    evaluate_scraped_candidate,
    extract_entity_rating_from_jsonld,
    promote_verified_candidate_to_db,
    scrub_pii,
    select_commune_matching_prediction,
)
from happy_path.core.solver import solve_pareto_portfolio
from happy_path.main import app
from happy_path.schemas.itinerary import Coordinate, ParetoCandidate

def _clock_to_min(clk: str) -> int:
    return int(clk[:2]) * 60 + int(clk[3:5])

class TestHappyPathFreezeSpecV3E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_ticket_01_durable_multiworker_storage_and_bounds(self):
        # 1. Health check
        resp = self.client.get("/v1/corridors/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual({c["corridor_id"] for c in data["corridors"]}, {"hcm_dalat", "hcm_nhatrang", "hcm_vungtau"})

        # 2. Cross-worker / restart durability: create session via HTTP, load via a separate CorridorRegistry instance
        plan_resp = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Vung Tau"},
        )
        self.assertEqual(plan_resp.status_code, 200)
        plan = plan_resp.json()
        sid = plan["session_id"]
        expected = app.state.registry.load_session(sid)
        self.assertIsNotNone(expected)
        self.assertEqual(expected.candidate_durations, {
            candidate["candidate_id"]: candidate["total_duration_minutes"]
            for candidate in plan["candidates"]
        })

        worker_b_registry = CorridorRegistry(DB_PATH)
        self.assertEqual(worker_b_registry.load_session(sid), expected)
        self.assertEqual(worker_b_registry.runtime_db_path, app.state.registry.runtime_db_path)

        expected.locked_poi_ids.add(plan["candidates"][0]["stops"][0]["poi_id"])
        expected.rejected_poi_ids.add(next(
            poi.poi_id for poi in worker_b_registry.pois_by_corridor[expected.corridor_id]
            if poi.poi_id not in expected.locked_poi_ids
        ))
        worker_b_registry.save_session(expected)
        del worker_b_registry
        restarted_registry = CorridorRegistry(DB_PATH)
        self.assertEqual(restarted_registry.load_session(sid), expected)
        self.assertEqual(app.state.registry.load_session(sid), expected)

        # 3. Missing, truncated, corrupt, and incomplete databases must fail fast.
        with tempfile.TemporaryDirectory() as tmpdir:
            for name, contents in [
                ("missing", None),
                ("truncated", b"SQLite format 3\x00"),
                ("bad_header", b"NOT_A_VALID_SQLITE_HEADER_BYTES_1234567890" * 5),
                ("corrupt_pages", b"SQLite format 3\x00" + b"\x00" * 384),
            ]:
                with self.subTest(database=name):
                    corrupt_path = Path(tmpdir) / f"{name}.db"
                    if contents is not None:
                        corrupt_path.write_bytes(contents)
                    with self.assertRaises(RuntimeError):
                        CorridorRegistry(corrupt_path)

            incomplete_path = Path(tmpdir) / "missing_sessions.db"
            shutil.copy2(DB_PATH, incomplete_path)
            conn = sqlite3.connect(str(incomplete_path))
            try:
                conn.execute("DROP TABLE trip_sessions")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(RuntimeError, "trip_sessions"):
                CorridorRegistry(incomplete_path)

        # 4. Inclusive bounds apply to both coordinate inputs.
        for lat, lng in [(8.0, 102.0), (23.5, 110.0)]:
            coord = Coordinate(lat=lat, lng=lng)
            self.assertEqual((coord.lat, coord.lng), (lat, lng))
        for field in ["origin_coords", "destination_coords"]:
            for lat, lng in [(23.6, 106.7), (7.9, 106.7), (10.7, 110.1), (106.7, 10.7), (10.7, 101.9)]:
                with self.subTest(field=field, lat=lat, lng=lng):
                    r_bad = self.client.post(
                        "/v1/itineraries/plan",
                        json={"origin_query": "HCMC", "destination_query": "Vung Tau", field: {"lat": lat, "lng": lng}},
                    )
                    self.assertEqual(r_bad.status_code, 422)
                    self.assertTrue(any(
                        error["loc"][:2] == ["body", field]
                        for error in r_bad.json()["detail"]
                    ))

    def test_ticket_01_lock_failure_uses_durable_shared_storage(self):
        response = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Vung Tau"},
        )
        self.assertEqual(response.status_code, 200)
        sid = response.json()["session_id"]
        connect = sqlite3.connect

        # Simulate only the unsupported filesystem lock; all storage remains real SQLite.
        class LocklessConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "BEGIN IMMEDIATE;":
                    raise sqlite3.OperationalError("disk I/O error acquiring filesystem lock")
                return super().execute(sql, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "seed.db"
            shutil.copy2(DB_PATH, source)
            path_hash = hashlib.md5(str(source.resolve()).encode("utf-8")).hexdigest()
            shared = Path(f"/tmp/happy_path_shared_{path_hash}.db")
            for artifact in [shared, shared.with_suffix(".lock"), shared.with_suffix(".tmp")]:
                self.addCleanup(artifact.unlink, missing_ok=True)

            def connect_with_lock_failure(database, *args, **kwargs):
                if Path(database) == source:
                    kwargs["factory"] = LocklessConnection
                return connect(database, *args, **kwargs)

            with patch("happy_path.core.graph.sqlite3.connect", side_effect=connect_with_lock_failure):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    worker_a, worker_b = list(pool.map(CorridorRegistry, [source, source]))
                self.assertEqual(worker_a.runtime_db_path, shared)
                self.assertEqual(worker_b.runtime_db_path, shared)
                record = worker_a.load_session(sid)
                self.assertIsNotNone(record)
                record.skip_categories.add("viewpoint")
                worker_a.save_session(record)
                self.assertEqual(worker_b.load_session(sid), record)
                del worker_a, worker_b
                self.assertTrue(shared.exists())
                restarted = CorridorRegistry(source)
                self.assertEqual(restarted.load_session(sid), record)

    def test_ticket_02_vungtau_48hour_schedule_validity_and_median_legality(self):
        registry: CorridorRegistry = app.state.registry
        corridor = registry.corridors["hcm_vungtau"]
        seen_directional = set()
        for poi_id, direction in [("vt_bo_sua_long_thanh_outbound", "OUTBOUND"), ("vt_bo_sua_long_thanh_inbound", "INBOUND")]:
            self.assertEqual(registry.pois_by_id[poi_id].direction_allowed, direction)
        # Sweep all 24 hours x 2 directions (48 requests)
        for orig, dest, expected_dir in [("HCMC", "Vung Tau", "outbound"), ("Vũng Tàu", "Sài Gòn", "inbound")]:
            for hr in range(24):
                dep_iso = f"2026-10-16T{hr:02d}:00:00+07:00"
                resp = self.client.post(
                    "/v1/itineraries/plan",
                    json={"origin_query": orig, "destination_query": dest, "departure_iso": dep_iso},
                )
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["direction"], expected_dir)
                self.assertGreaterEqual(len(body["candidates"]), 1)

                for cand in body["candidates"]:
                    prog = [s["progress_km_from_origin"] for s in cand["stops"]]
                    self.assertEqual(prog, sorted(prog))
                    for s in cand["stops"]:
                        poi = registry.pois_by_id[s["poi_id"]]
                        # Verify median-barrier direction legality
                        if expected_dir == "outbound":
                            self.assertIn(poi.direction_allowed, ("BOTH", "OUTBOUND"))
                        else:
                            self.assertIn(poi.direction_allowed, ("BOTH", "INBOUND"))
                        # Verify stop is genuinely open at service_start_clock
                        srv_min = _clock_to_min(s["service_start_clock"])
                        self.assertGreaterEqual(srv_min, poi.open_minute, f"Visited {poi.poi_id} before open at {dep_iso}")
                        self.assertLessEqual(srv_min + poi.dwell_minutes, poi.close_minute, f"Visited {poi.poi_id} after close at {dep_iso}")
                        self.assertEqual(srv_min, (_clock_to_min(s["arrival_clock"]) + s["wait_minutes"]) % 1440)
                        self.assertEqual(_clock_to_min(s["departure_clock"]), (srv_min + poi.dwell_minutes) % 1440)
                        self.assertGreaterEqual(s["wait_minutes"], 0)
                        self.assertLessEqual(s["wait_minutes"], 45)
                        if poi.direction_allowed != "BOTH":
                            seen_directional.add(poi.poi_id)

                    # Verify road-following sliced polyline has >= len(stops) + 2 vertices and matches endpoints
                    pts = decode_polyline(cand["encoded_polyline"])
                    self.assertGreaterEqual(len(pts), len(cand["stops"]) + 2)
                    endpoints = [(corridor.origin_lat, corridor.origin_lng), (corridor.dest_lat, corridor.dest_lng)]
                    if expected_dir == "inbound":
                        endpoints.reverse()
                    for actual, expected in zip([pts[0], pts[-1]], endpoints):
                        for value, target in zip(actual, expected):
                            self.assertAlmostEqual(value, target, delta=0.00001)
                    for stop in cand["stops"]:
                        self.assertTrue(any(
                            abs(lat - stop["coordinates"]["lat"]) <= 0.00001
                            and abs(lng - stop["coordinates"]["lng"]) <= 0.00001
                            for lat, lng in pts
                        ))
        self.assertIn("vt_bo_sua_long_thanh_outbound", seen_directional)
        self.assertIn("vt_bo_sua_long_thanh_inbound", seen_directional)

    def test_ticket_02_bounded_wait_and_closing_boundaries(self):
        registry = app.state.registry
        # Isolate the real breakfast venue through supported mutation constraints.
        poi_id = "vt_banh_khot_bai_sau"
        for departure, status, wait, service, finish in [
            ("05:54", 200, 24, "06:30", "07:15"),
            ("05:33", 200, 45, "06:30", "07:15"),
            ("05:32", 409, None, None, None),
            ("19:58", 200, 0, "20:15", "21:00"),
            ("19:59", 409, None, None, None),
        ]:
            with self.subTest(departure=departure):
                plan = self.client.post("/v1/itineraries/plan", json={
                    "origin_query": "Vung Tau", "destination_query": "HCMC",
                    "departure_iso": departure,
                })
                self.assertEqual(plan.status_code, 200)
                response = self.client.post("/v1/itineraries/mutate", json={
                    "session_id": plan.json()["session_id"], "candidate_id": "P1_EXPRESS",
                    "action": "LOCK_STOP", "target_poi_id": poi_id,
                    "rejected_poi_ids": [p.poi_id for p in registry.pois_by_corridor["hcm_vungtau"] if p.poi_id != poi_id],
                })
                self.assertEqual(response.status_code, status, response.text)
                if status == 200:
                    candidate = response.json()["mutated_candidate"]
                    self.assertEqual(len(candidate["stops"]), 1)
                    stop = candidate["stops"][0]
                    self.assertEqual(stop["wait_minutes"], wait)
                    self.assertEqual(stop["service_start_clock"], service)
                    self.assertEqual(stop["departure_clock"], finish)
                    self.assertEqual(candidate["total_wait_minutes"], wait)
                    self.assertEqual(candidate["total_duration_minutes"], candidate["total_drive_minutes"] + candidate["total_dwell_minutes"] + wait)

    def test_ticket_02_slice_keeps_nearby_spine_waypoint(self):
        # A 10-km sparse slice starts only 1 km before the km-46 road control point.
        origin = {"lat": 10.71579, "lng": 107.00593}
        destination = {"lat": 10.5957, "lng": 107.0496}
        for orig, dest in [(origin, destination), (destination, origin)]:
            outbound = orig == origin
            response = self.client.post("/v1/itineraries/plan", json={
                "origin_query": "HCMC" if outbound else "Vung Tau",
                "destination_query": "Vung Tau" if outbound else "HCMC",
                "origin_coords": orig, "destination_coords": dest,
            })
            self.assertEqual(response.status_code, 200, response.text)
            for candidate in response.json()["candidates"]:
                points = decode_polyline(candidate["encoded_polyline"])
                self.assertEqual(points[0], (orig["lat"], orig["lng"]))
                self.assertEqual(points[-1], (dest["lat"], dest["lng"]))
                self.assertIn((10.71, 107.01), points)

    def test_ticket_03_dalat_physical_interchanges_and_5d_pareto_nondominance(self):
        registry: CorridorRegistry = app.state.registry
        branches_map = registry.branches_by_corridor["hcm_dalat"]

        # Independent oracle: changing the solver's dominance predicate must not
        # silently change the contract checked by this sweep.
        def vector(c):
            return (-c.total_drive_minutes, -c.total_detour_minutes,
                    c.aggregate_utility_score, c.aggregate_scenic_score, -c.hazard_risk_score)

        def is_dominated_by(candidate, other):
            a, b = vector(other), vector(candidate)
            return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))

        for orig, dest in [("Ho Chi Minh City", "Da Lat"), ("Đà Lạt", "TP.HCM")]:
            for hr in range(24):
                for powertrain in ("ice", "ev"):
                    with self.subTest(origin=orig, hour=hr, powertrain=powertrain):
                        resp = self.client.post(
                            "/v1/itineraries/plan",
                            json={
                                "origin_query": orig,
                                "destination_query": dest,
                                "departure_iso": f"2026-10-16T{hr:02d}:00:00+07:00",
                                "powertrain": powertrain,
                            },
                        )
                        self.assertEqual(resp.status_code, 200, resp.text)
                        cands = [ParetoCandidate(**c) for c in resp.json()["candidates"]]
                        self.assertGreaterEqual(len(cands), 1)
                        self.assertEqual(len({tuple(s.poi_id for s in c.stops) for c in cands}), len(cands))
                        for c in cands:
                            if c.candidate_id == "P1_EXPRESS":
                                self.assertTrue(all(s.branch_id == "MAIN_SPINE" for s in c.stops))
                            if c.active_branch_id != "MAIN_SPINE":
                                b_meta = branches_map[c.active_branch_id]
                                for s in c.stops:
                                    self.assertIn(s.branch_id, ("MAIN_SPINE", c.active_branch_id))
                                    if s.branch_id == "MAIN_SPINE":
                                        self.assertFalse(
                                            b_meta.diverge_km < s.corridor_km_from_hcm < b_meta.merge_km,
                                            f"{s.poi_id} violates {c.active_branch_id} interchange window",
                                        )
                            if len(cands) < 3:
                                self.assertTrue(any("PORTFOLIO_SHORTFALL" in w for w in c.warnings))
                        for i, c in enumerate(cands):
                            for j, other in enumerate(cands):
                                if i != j:
                                    self.assertFalse(is_dominated_by(c, other), f"{other.candidate_id} dominates {c.candidate_id}")

        # A feasible route omitted from the portfolio can still dominate an
        # early archetype choice. Removing a stop must not expose a better route
        # that was already feasible in the unrestricted pool.
        corridor = registry.corridors["hcm_dalat"]
        route_ids = {"dl_tram_dung_tam_chau_bao_loc", "dl_thac_datanla_alpine_anchor"}
        _, _, alternatives = solve_pareto_portfolio(
            registry, corridor, "outbound", "00:00", locked_ids=route_ids,
            rejected_ids={p.poi_id for p in registry.pois_by_corridor["hcm_dalat"]} - route_ids,
        )
        self.assertTrue(alternatives)
        _, _, unrestricted = solve_pareto_portfolio(registry, corridor, "outbound", "00:00")
        for candidate in unrestricted:
            for alternative in alternatives:
                self.assertFalse(is_dominated_by(candidate, alternative), "Unselected feasible route dominates portfolio member")

    def test_ticket_03_single_poi_portfolio_shortfall(self):
        registry: CorridorRegistry = app.state.registry
        _, _, candidates = solve_pareto_portfolio(
            registry, registry.corridors["hcm_dalat"], "outbound", "00:00",
            active_start_km=30.0, active_end_km=55.0,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual([s.poi_id for s in candidates[0].stops], ["dl_tram_sac_ev_long_thanh_km42"])
        self.assertTrue(any("PORTFOLIO_SHORTFALL" in w for w in candidates[0].warnings))

    def test_ticket_04_unconditional_uv_shield_exertion_ablation_and_night_open_stops(self):
        from dataclasses import replace

        registry: CorridorRegistry = app.state.registry
        required_high = {
            "hcm_dalat": {"dl_thac_dambri", "dl_thac_pongour_duc_trong"},
            "hcm_nhatrang": {"nt_doi_cat_bau_trang", "nt_hang_rai_vuon_nho_thai_an"},
        }
        for cid, ids in required_high.items():
            self.assertTrue(ids.issubset({p.poi_id for p in registry.pois_by_corridor[cid]
                                         if p.exertion_level == "HIGH"}))

        def plan(destination, clock):
            response = self.client.post("/v1/itineraries/plan", json={
                "origin_query": "HCMC", "destination_query": destination,
                "departure_iso": f"2026-10-16T{clock}:00+07:00",
            })
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        # Positive control proves the two-HIGH denial is not topology or hours.
        lock_two = {
            "session_id": plan("Da Lat", "06:00")["session_id"],
            "candidate_id": "P2_ICONIC", "action": "LOCK_STOP",
            "target_poi_id": "dl_thac_dambri",
            "locked_poi_ids": ["dl_thac_pongour_duc_trong"],
        }
        self.assertEqual(self.client.post("/v1/itineraries/mutate", json=lock_two).status_code, 409)
        with patch.dict(registry.pois_by_corridor, {"hcm_dalat": [
            replace(p, exertion_level="LOW") for p in registry.pois_by_corridor["hcm_dalat"]
        ]}):
            control = self.client.post("/v1/itineraries/mutate", json=lock_two)
            self.assertEqual(control.status_code, 200, control.text)
            self.assertTrue(required_high["hcm_dalat"].issubset({
                s["poi_id"] for s in control.json()["mutated_candidate"]["stops"]
            }))

        for destination in ("Da Lat", "Nha Trang", "Vung Tau"):
            for clock in ("21:30", "23:30", "01:00", "01:30", "03:30"):
                with self.subTest(destination=destination, departure=clock):
                    candidates = plan(destination, clock)["candidates"]
                    stops = [s for c in candidates for s in c["stops"]]
                    self.assertTrue(stops)
                    for stop in stops:
                        poi = registry.pois_by_id[stop["poi_id"]]
                        service = _clock_to_min(stop["service_start_clock"])
                        self.assertGreaterEqual(service, poi.open_minute)
                        self.assertLessEqual(service + poi.dwell_minutes, poi.close_minute)

        # Check service overlap, including intervals starting before 11:00.
        for hour in range(8, 14):
            result = plan("Vung Tau", f"{hour:02d}:30")
            for candidate in result["candidates"]:
                for stop in candidate["stops"]:
                    poi = registry.pois_by_id[stop["poi_id"]]
                    if poi.midday_sun_trap:
                        start = _clock_to_min(stop["service_start_clock"])
                        self.assertFalse(start <= 870 and start + poi.dwell_minutes >= 660)
            if hour == 10:
                plan_vt = result
        locked_uv = self.client.post("/v1/itineraries/mutate", json={
            "session_id": plan_vt["session_id"], "candidate_id": "P3_SCENIC",
            "action": "LOCK_STOP", "target_poi_id": "vt_doi_cuu_suoi_nghe",
        })
        self.assertEqual(locked_uv.status_code, 200, locked_uv.text)
        self.assertTrue(any("UV_LOCK_OVERRIDE_WARNING" in w
                            for w in locked_uv.json()["mutated_candidate"]["warnings"]))

        # Single real-POI pools force Pass 2 without changing safety attributes.
        corridor = registry.corridors["hcm_vungtau"]
        for poi_id, clock, feasible, night_warning in [
            ("vt_bo_sua_long_thanh_outbound", "21:30", True, True),
            ("vt_banh_khot_bai_sau", "21:30", False, False),
            ("vt_banh_khot_bai_sau", "06:00", True, False),
            ("vt_doi_cuu_suoi_nghe", "10:30", False, False),
        ]:
            with self.subTest(pass2_poi=poi_id), patch.dict(registry.pois_by_corridor, {
                corridor.corridor_id: [registry.pois_by_id[poi_id]],
            }):
                _, _, candidates = solve_pareto_portfolio(registry, corridor, "outbound", clock)
                self.assertEqual(any(c.stops for c in candidates), feasible)
                self.assertEqual(any("NIGHT_SAFE_REST_ROUTING" in w
                                     for c in candidates for w in c.warnings), night_warning)
                if poi_id == "vt_doi_cuu_suoi_nghe":
                    _, _, locked = solve_pareto_portfolio(
                        registry, corridor, "outbound", clock, locked_ids={poi_id},
                    )
                    self.assertTrue(locked)
                    self.assertTrue(any("UV_LOCK_OVERRIDE_WARNING" in w for c in locked for w in c.warnings))

        # Seed-only scoring ablations: no boosted pair or candidate-index assumption.
        def iconic_ids(result):
            candidates = {c["candidate_id"]: c for c in result["candidates"]}
            self.assertIn("P2_ICONIC", candidates)
            return [s["poi_id"] for s in candidates["P2_ICONIC"]["stops"]]

        with_co = plan("Nha Trang", "09:30")
        with patch.dict(registry.co_occurrence, {}, clear=True):
            without_co = plan("Nha Trang", "09:30")
        self.assertNotEqual(iconic_ids(with_co), iconic_ids(without_co))

        with_anchor = plan("Da Lat", "04:15")
        candidates = {c["candidate_id"]: c for c in with_anchor["candidates"]}
        for archetype in ("P2_ICONIC", "P3_SCENIC"):
            self.assertIn(archetype, candidates)
            candidate = candidates[archetype]
            self.assertTrue(510 <= (255 + candidate["total_duration_minutes"]) % 1440 <= 840)
            self.assertTrue(any(s["is_arrival_anchor"] for s in candidate["stops"]))
        with patch.dict(registry.pois_by_corridor, {"hcm_dalat": [
            replace(p, is_arrival_anchor=False) for p in registry.pois_by_corridor["hcm_dalat"]
        ]}):
            without_anchor = plan("Da Lat", "04:15")
        self.assertNotEqual(iconic_ids(with_anchor), iconic_ids(without_anchor))

    def test_ticket_05_nhatrang_96run_ev_charger_interchange_and_branch_tolls(self):
        registry: CorridorRegistry = app.state.registry
        expected_tolls = {
            "hcm_nhatrang": {"MAIN_SPINE": 309000, "BRANCH_VINHHY_DT702": 284000,
                             "BRANCH_BAUTRANG_COAST": 244000},
            "hcm_dalat": {"MAIN_SPINE": 136000, "BRANCH_BAOLOC_LOOP": 136000,
                          "BRANCH_QL28B_DAININH": 261000},
            "hcm_vungtau": {"MAIN_SPINE": 81000, "BRANCH_LONGSON_ISLAND": 66000,
                            "BRANCH_SUOINGHE_LOOP": 66000},
        }
        coastal_bounds = {"BRANCH_BAUTRANG_COAST": (180.0, 395.0),
                          "BRANCH_VINHHY_DT702": (325.0, 395.0)}
        seen_ev_branches = {"hcm_nhatrang": set(), "hcm_dalat": set()}

        # 96 Nha Trang requests (24 hours x 2 directions x EV/ICE), plus
        # 48 Da Lat EV requests. This includes every three-hour departure.
        for cid, destination, powertrains in [
            ("hcm_nhatrang", "Nha Trang", ("ev", "ice")),
            ("hcm_dalat", "Da Lat", ("ev",)),
        ]:
            for orig, dest in [("TP.HCM", destination), (destination, "TP.HCM")]:
                for hr in range(24):
                    for powertrain in powertrains:
                        with self.subTest(corridor=cid, origin=orig, hour=hr, powertrain=powertrain):
                            resp = self.client.post("/v1/itineraries/plan", json={
                                "origin_query": orig,
                                "destination_query": dest,
                                "departure_iso": f"2026-10-16T{hr:02d}:30:00+07:00",
                                "powertrain": powertrain,
                            })
                            self.assertEqual(resp.status_code, 200, resp.text)
                            body = resp.json()
                            self.assertTrue(body["candidates"])
                            start = body["active_start_km"]
                            span = body["active_end_km"] - start
                            for cand in body["candidates"]:
                                branch = cand["active_branch_id"]
                                self.assertEqual(cand["estimated_vetc_toll_vnd"], expected_tolls[cid][branch])
                                if powertrain == "ev":
                                    seen_ev_branches[cid].add(branch)
                                    self.assertTrue(cand["ev_charging_satisfied"])
                                    self.assertTrue(any(
                                        s["has_ev_charge"] and
                                        start + 0.15 * span <= s["progress_km_from_origin"] <= start + 0.80 * span
                                        for s in cand["stops"]
                                    ))
                                bounds = coastal_bounds.get(branch)
                                if bounds is None and branch != "MAIN_SPINE":
                                    meta = registry.branches_by_corridor[cid][branch]
                                    bounds = (meta.diverge_km, meta.merge_km)
                                for stop in cand["stops"]:
                                    poi = registry.pois_by_id[stop["poi_id"]]
                                    service = _clock_to_min(stop["service_start_clock"])
                                    self.assertGreaterEqual(service, poi.open_minute)
                                    self.assertLessEqual(service + stop["dwell_minutes"], poi.close_minute)
                                    self.assertEqual(service, (_clock_to_min(stop["arrival_clock"]) + stop["wait_minutes"]) % 1440)
                                    self.assertIn(stop["branch_id"], ("MAIN_SPINE", branch))
                                    if bounds and stop["branch_id"] == "MAIN_SPINE":
                                        self.assertFalse(bounds[0] < stop["corridor_km_from_hcm"] < bounds[1])
        for cid, branches in seen_ev_branches.items():
            self.assertEqual(branches, set(expected_tolls[cid]))

        # Exercise each branch, including branches not selected by an unlocked
        # portfolio, using real seeded POIs through the solver's lock contract.
        for cid, tolls in expected_tolls.items():
            for branch, expected in tolls.items():
                with self.subTest(corridor=cid, branch=branch):
                    poi = next(p for p in registry.pois_by_corridor[cid] if p.branch_id == branch)
                    _, _, candidates = solve_pareto_portfolio(
                        registry, registry.corridors[cid], "outbound", "06:00",
                        locked_ids={poi.poi_id},
                    )
                    branch_candidates = [c for c in candidates if c.active_branch_id == branch]
                    self.assertTrue(branch_candidates)
                    for candidate in branch_candidates:
                        self.assertEqual(candidate.estimated_vetc_toll_vnd, expected)

        # Sparse direct routes must use the same authoritative branch toll,
        # not a distance-proportional estimate.
        _, _, direct = solve_pareto_portfolio(
            registry, registry.corridors["hcm_nhatrang"], "outbound", "09:00",
            active_start_km=400.0, active_end_km=420.0,
        )
        self.assertTrue(direct)
        self.assertEqual(direct[0].stops, [])
        self.assertEqual(direct[0].estimated_vetc_toll_vnd, 309000)

    def test_ticket_06_piecewise_projection_custom_pin_polyline_and_sparse_slices(self):
        # Piecewise projection and exact offline endpoints, not the canonical HCMC origin.
        mid_resp = self.client.post("/v1/itineraries/plan", json={
            "origin_query": "Dinh Quan", "destination_query": "Da Lat",
            "origin_coords": {"lat": 11.2015, "lng": 107.3512},
            "departure_iso": "2026-10-16T08:00:00+07:00",
        })
        self.assertEqual(mid_resp.status_code, 200, mid_resp.text)
        body = mid_resp.json()
        self.assertAlmostEqual(body["active_start_km"], 114.0, delta=2.0)
        for cand in body["candidates"]:
            points = decode_polyline(cand["encoded_polyline"])
            self.assertEqual(points[0], (11.2015, 107.3512))
            self.assertEqual(points[-1], (11.9404, 108.4583))
            previous_departure = 8 * 60
            elapsed_drive = 0
            for stop in cand["stops"]:
                self.assertGreaterEqual(stop["progress_km_from_origin"], body["active_start_km"] + 2.0)
                self.assertLessEqual(stop["progress_km_from_origin"], body["active_end_km"] - 1.0)
                leg_minutes = (_clock_to_min(stop["arrival_clock"]) - previous_departure) % 1440
                self.assertGreaterEqual(leg_minutes, 5)
                elapsed_drive += leg_minutes
                previous_departure = _clock_to_min(stop["departure_clock"])
            self.assertGreaterEqual(cand["total_drive_minutes"] - elapsed_drive, 5)

        # A rejected lock must leave the durable session unchanged.
        registry = app.state.registry
        before_lock = registry.load_session(body["session_id"])
        r_lock_behind = self.client.post("/v1/itineraries/mutate", json={
            "session_id": body["session_id"], "candidate_id": "P1_EXPRESS",
            "action": "LOCK_STOP", "target_poi_id": "dl_tram_sac_ev_long_thanh_km42",
        })
        self.assertEqual(r_lock_behind.status_code, 409)
        self.assertEqual(registry.load_session(body["session_id"]), before_lock)

        # The required 22-km slice drives in both directions. At night its
        # only eligible venue is closed; the 12-km slice has no eligible venues.
        for origin, destination, departure, direct in [
            ({"lat": 11.7350, "lng": 108.3750}, {"lat": 11.8890, "lng": 108.4350}, "06:00", False),
            ({"lat": 11.7350, "lng": 108.3750}, {"lat": 11.8890, "lng": 108.4350}, "23:00", True),
            ({"lat": 11.7630, "lng": 108.38591}, {"lat": 11.8470, "lng": 108.41864}, "06:00", True),
        ]:
            for outbound in (True, False):
                orig, dest = (origin, destination) if outbound else (destination, origin)
                for powertrain in ("ice", "ev"):
                    with self.subTest(origin=orig, departure=departure, powertrain=powertrain):
                        sparse_resp = self.client.post("/v1/itineraries/plan", json={
                            "origin_query": "HCMC" if outbound else "Da Lat",
                            "destination_query": "Da Lat" if outbound else "HCMC",
                            "origin_coords": orig, "destination_coords": dest,
                            "departure_iso": departure, "powertrain": powertrain,
                        })
                        self.assertEqual(sparse_resp.status_code, 200, sparse_resp.text)
                        candidates = sparse_resp.json()["candidates"]
                        self.assertTrue(candidates)
                        for cand in candidates:
                            self.assertGreaterEqual(cand["total_drive_minutes"], 5)
                            points = decode_polyline(cand["encoded_polyline"])
                            self.assertEqual(points[0], (orig["lat"], orig["lng"]))
                            self.assertEqual(points[-1], (dest["lat"], dest["lng"]))
                            previous_departure = _clock_to_min(departure)
                            elapsed_drive = 0
                            for stop in cand["stops"]:
                                leg_minutes = (_clock_to_min(stop["arrival_clock"]) - previous_departure) % 1440
                                self.assertGreaterEqual(leg_minutes, 5)
                                elapsed_drive += leg_minutes
                                previous_departure = _clock_to_min(stop["departure_clock"])
                            self.assertGreaterEqual(cand["total_drive_minutes"] - elapsed_drive, 5)
                            if direct:
                                self.assertEqual(cand["candidate_id"], "P1_EXPRESS")
                                self.assertEqual(cand["stops"], [])
                                self.assertEqual(cand["total_duration_minutes"], cand["total_drive_minutes"])
                                self.assertTrue(any(w.startswith("SPARSE_SLICE_DIRECT_DRIVE:") for w in cand["warnings"]))

        # A five-kilometer slice is not drivable under the API contract.
        too_short = self.client.post("/v1/itineraries/plan", json={
            "origin_query": "HCMC", "destination_query": "Da Lat",
            "origin_coords": {"lat": 11.9147, "lng": 108.44665},
        })
        self.assertEqual(too_short.status_code, 400)

    def test_ticket_07_atomic_rollback_on_409_archetype_integrity_and_live_deltas(self):
        # 1. Start Vung Tau session
        plan_vt = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Vung Tau", "departure_iso": "2026-10-16T06:00:00+07:00"},
        ).json()
        sid = plan_vt["session_id"]
        registry = app.state.registry
        initial_session = registry.load_session(sid)

        # 2. Requesting P1_EXPRESS with a non-main branch lock (vt_nha_lon_long_son) must return 409 (never substitute P2!)
        r_p1_branch = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": sid,
                "candidate_id": "P1_EXPRESS",
                "action": "LOCK_STOP",
                "target_poi_id": "vt_nha_lon_long_son",
            },
        )
        self.assertEqual(r_p1_branch.status_code, 409)
        self.assertEqual(registry.load_session(sid), initial_session)

        # 3. Lock vt_nha_lon_long_son on P2_ICONIC -> 200 OK
        r_lock_ok = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": sid,
                "candidate_id": "P2_ICONIC",
                "action": "LOCK_STOP",
                "target_poi_id": "vt_nha_lon_long_son",
            },
        )
        self.assertEqual(r_lock_ok.status_code, 200)
        self.assertEqual(r_lock_ok.json()["mutated_candidate"]["candidate_id"], "P2_ICONIC")
        self.assertEqual(r_lock_ok.json()["cumulative_locked_poi_ids"], ["vt_nha_lon_long_son"])
        locked_session = registry.load_session(sid)
        self.assertEqual(locked_session.locked_poi_ids, {"vt_nha_lon_long_son"})

        # 4. Attempt incompatible cross-branch lock (vt_doi_cuu_suoi_nghe) -> 409 Conflict AND rollback!
        r_lock_conflict = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": sid,
                "candidate_id": "P2_ICONIC",
                "action": "LOCK_STOP",
                "target_poi_id": "vt_doi_cuu_suoi_nghe",
            },
        )
        self.assertEqual(r_lock_conflict.status_code, 409)
        self.assertEqual(registry.load_session(sid), locked_session)

        # 5. Next valid mutation (SHIFT_DEPARTURE to 07:00) must succeed (200 OK) with only vt_nha_lon_long_son locked!
        r_shift = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": sid,
                "candidate_id": "P2_ICONIC",
                "action": "SHIFT_DEPARTURE",
                "new_departure_iso": "07:00",
            },
        )
        self.assertEqual(r_shift.status_code, 200)
        self.assertEqual(r_shift.json()["cumulative_locked_poi_ids"], ["vt_nha_lon_long_son"])
        shifted_session = registry.load_session(sid)
        self.assertEqual(shifted_session.locked_poi_ids, {"vt_nha_lon_long_son"})
        self.assertEqual(shifted_session.departure_iso, "07:00")

        # A legal P2 route must not disappear merely because P1 took the same signature first.
        night_plan = self.client.post("/v1/itineraries/plan", json={
            "origin_query": "HCMC", "destination_query": "Vung Tau",
            "departure_iso": "2026-10-16T21:30:00+07:00",
        }).json()
        night_candidate = next(c for c in night_plan["candidates"] if c["candidate_id"] == "P2_ICONIC")
        removed = set()
        for _ in range(2):
            removed.add(night_candidate["stops"][0]["poi_id"])
            mutation = self.client.post("/v1/itineraries/mutate", json={
                "session_id": night_plan["session_id"], "candidate_id": "P2_ICONIC",
                "action": "REMOVE_STOP", "target_poi_id": night_candidate["stops"][0]["poi_id"],
            })
            self.assertEqual(mutation.status_code, 200, mutation.text)
            night_candidate = mutation.json()["mutated_candidate"]
            self.assertEqual(night_candidate["candidate_id"], "P2_ICONIC")
            self.assertTrue(night_candidate["stops"])
            for stop in night_candidate["stops"]:
                self.assertNotIn(stop["poi_id"], removed)
                poi = app.state.registry.pois_by_id[stop["poi_id"]]
                service = _clock_to_min(stop["service_start_clock"])
                self.assertGreaterEqual(service, poi.open_minute)
                self.assertLessEqual(service + stop["dwell_minutes"], poi.close_minute)

        # 6. Malformed action parameters must return HTTP 422
        for bad_mut in [
            {"session_id": sid, "action": "REMOVE_STOP"},
            {"session_id": sid, "action": "LOCK_STOP"},
            {"session_id": sid, "action": "LOCK_STOP", "target_poi_id": "   "},
            {"session_id": sid, "action": "REMOVE_STOP", "target_poi_id": "unknown_poi"},
            {"session_id": sid, "action": "LOCK_STOP", "target_poi_id": "dl_thac_pongour_duc_trong"},
            {"session_id": sid, "action": "SKIP_CATEGORY", "target_category": "FOOD"},
            {"session_id": sid, "action": "SHIFT_DEPARTURE", "new_departure_iso": "not-a-date"},
        ]:
            with self.subTest(payload=bad_mut):
                self.assertEqual(self.client.post("/v1/itineraries/mutate", json=bad_mut).status_code, 422)
                self.assertEqual(registry.load_session(sid), shifted_session)

        # Exercise real Goong decoration at its HTTP boundary. A sparse slice has
        # no dwell/wait, so the provider's minutes are the exact returned duration.
        live_minutes = 234

        def live_direction(request):
            self.assertEqual(request.url.path.rsplit("/", 1)[-1], "direction")
            return httpx.Response(200, json={
                "routes": [{"legs": [{"duration": {"value": live_minutes * 60}}]}],
            })

        async def exercise_live_deltas(live_client):
            with patch.object(app.state.goong, "api_key", "ticket07-test-key"), \
                 patch.object(app.state.goong, "_client", live_client):
                plan = self.client.post("/v1/itineraries/plan", json={
                    "origin_query": "HCMC", "destination_query": "Da Lat",
                    "origin_coords": {"lat": 11.7630, "lng": 108.38591},
                    "destination_coords": {"lat": 11.8470, "lng": 108.41864},
                    "departure_iso": "06:00",
                })
                self.assertEqual(plan.status_code, 200, plan.text)
                body = plan.json()
                self.assertEqual(body["candidates"][0]["total_duration_minutes"], 234)
                live_sid = body["session_id"]
                self.assertEqual(registry.load_session(live_sid).candidate_durations, {"P1_EXPRESS": 234})

                nonlocal live_minutes
                for minutes, departure, delta in [(235, "06:01", 1), (233, "06:02", -2)]:
                    live_minutes = minutes
                    response = self.client.post("/v1/itineraries/mutate", json={
                        "session_id": live_sid, "candidate_id": "P1_EXPRESS",
                        "action": "SHIFT_DEPARTURE", "new_departure_iso": departure,
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertEqual(result["mutated_candidate"]["total_duration_minutes"], minutes)
                    self.assertTrue(any("Cảnh báo kẹt xe" in warning
                                        for warning in result["mutated_candidate"]["warnings"]))
                    self.assertEqual(result["duration_delta_minutes"], delta)
                    self.assertIn(f"{delta:+d} phút", result["delta_summary_vi"])
                    self.assertEqual(registry.load_session(live_sid).candidate_durations, {"P1_EXPRESS": minutes})

        async def run_live_deltas():
            async with httpx.AsyncClient(transport=httpx.MockTransport(live_direction)) as live_client:
                await exercise_live_deltas(live_client)

        asyncio.run(run_live_deltas())

    def test_ticket_08_sub350ms_parallel_goong_clock_propagation_and_null_points(self):
        with patch.object(app.state.goong, "api_key", ""):
            response = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Vung Tau"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        cands = [ParetoCandidate(**c) for c in response.json()["candidates"]]
        self.assertEqual(len(cands), 3)
        orig = Coordinate(lat=10.7872, lng=106.7498)
        dest = Coordinate(lat=10.3460, lng=107.0843)

        async def delayed_socket_gate():
            requests = []
            handlers = set()

            async def slow_direction(reader, writer):
                task = asyncio.current_task()
                handlers.add(task)
                try:
                    requests.append(await reader.readuntil(b"\r\n\r\n"))
                    await asyncio.sleep(0.60)
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{"routes":[]}')
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    handlers.discard(task)

            server = await asyncio.start_server(slow_direction, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            goong = GoongClient(api_key="ticket08-test-key", base_url=f"http://127.0.0.1:{port}")
            persistent_client = goong._get_client()
            before = [c.model_dump() for c in cands]
            try:
                async with server:
                    t0 = time.monotonic()
                    await goong.decorate_portfolio(orig, dest, cands)
                    elapsed = time.monotonic() - t0
                    self.assertLess(elapsed, 0.34, f"Parallel socket timeout exceeded budget: {elapsed:.3f}s")
                    self.assertEqual(len(requests), 3)
                    self.assertEqual([c.model_dump() for c in cands], before)
                    self.assertIs(goong._get_client(), persistent_client)
            finally:
                await goong.aclose()
                pending = list(handlers)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            self.assertTrue(persistent_client.is_closed)

        asyncio.run(delayed_socket_gate())

        async def congestion_gate():
            baseline = cands[0]
            # Unequal live legs sum to exactly 2x the offline drive time.
            num_legs = len(baseline.stops) + 1
            first_legs = [17 + idx * 11 for idx in range(num_legs - 1)]
            live_legs = first_legs + [baseline.total_drive_minutes * 2 - sum(first_legs)]
            for points, minutes in [(None, live_legs), ("", live_legs), ("   ", live_legs),
                                    (None, [1] + live_legs[1:])]:
                with self.subTest(points=points, minutes=minutes):
                    candidate = baseline.model_copy(deep=True)
                    candidate.stops[0].wait_minutes = 7
                    candidate.total_wait_minutes = sum(s.wait_minutes for s in candidate.stops)
                    candidate.total_duration_minutes += 7
                    old_summary = candidate.tradeoff_summary_vi
                    old_arrival = candidate.stops[0].arrival_clock
                    fallback = candidate.encoded_polyline

                    def live_direction(request):
                        return httpx.Response(200, json={"routes": [{
                            "overview_polyline": {"points": points},
                            "legs": [{"duration": {"value": m * 60}} for m in minutes],
                        }]})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(live_direction)) as client:
                        goong = GoongClient(api_key="ticket08-test-key")
                        with patch.object(goong, "_client", client):
                            await goong.decorate_candidate_polyline(orig, dest, candidate, departure_iso="06:00")
                    self.assertIsInstance(candidate.encoded_polyline, str)
                    self.assertTrue(candidate.encoded_polyline)
                    self.assertEqual(candidate.encoded_polyline, fallback)
                    self.assertNotEqual(candidate.stops[0].arrival_clock, old_arrival)
                    clock = 360
                    for stop, leg in zip(candidate.stops, minutes):
                        clock += max(5, leg)
                        self.assertEqual(_clock_to_min(stop.arrival_clock), clock % 1440)
                        clock += stop.wait_minutes
                        self.assertEqual(_clock_to_min(stop.service_start_clock), clock % 1440)
                        clock += stop.dwell_minutes
                        self.assertEqual(_clock_to_min(stop.departure_clock), clock % 1440)
                    drive = sum(max(5, m) for m in minutes)
                    self.assertEqual(candidate.total_drive_minutes, drive)
                    self.assertEqual(candidate.total_duration_minutes,
                                     drive + candidate.total_dwell_minutes + candidate.total_wait_minutes)
                    self.assertNotEqual(candidate.tradeoff_summary_vi, old_summary)
                    self.assertIn(f"lái xe {drive} phút (Goong Live)", candidate.tradeoff_summary_vi)
                    self.assertTrue(any("Cảnh báo kẹt xe" in w for w in candidate.warnings))

            # Missing, incomplete, or invalid live legs must not fabricate a schedule.
            for legs in [[], [{"duration": {"value": 99999}}],
                         [{"duration": {"value": 99999}}] * (num_legs - 1) + [{}]]:
                with self.subTest(legs=legs):
                    candidate = baseline.model_copy(deep=True)
                    def invalid_direction(request):
                        return httpx.Response(200, json={"routes": [{
                            "overview_polyline": {"points": None}, "legs": legs,
                        }]})
                    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_direction)) as client:
                        goong = GoongClient(api_key="ticket08-test-key")
                        with patch.object(goong, "_client", client):
                            await goong.decorate_candidate_polyline(orig, dest, candidate)
                    self.assertEqual(candidate, baseline)

        asyncio.run(congestion_gate())

    def test_ticket_09_nfc_nfd_parity_hardened_pii_entity_binding_and_sqlite_promotion(self):
        dirty_text = (
            "Liên hệ SĐT0909123456 hoặc 0912\u00a0345\u00a0678, email nguyễn.văn.a@gmail.com, "
            "FB https://web.facebook.com/groups/ghiendalat/user/100012345/ @tuan_driver"
        )
        for encoding in ("NFC", "NFD"):
            cleaned = scrub_pii(unicodedata.normalize(encoding, dirty_text))
            self.assertEqual(cleaned.count("[REDACTED_PII]"), 5)
            for forbidden in ("0909123456", "0912", "345", "678", "nguyễn", "gmail", "facebook", "100012345", "@"):
                self.assertNotIn(forbidden, cleaned)
            for token in ("vỡ mộng", "chỉ được cái view"):
                cand = RawScrapedCandidate(
                    unicodedata.normalize(encoding, "Quán Đẹp"), "Đà Lạt",
                    unicodedata.normalize(encoding, "Đà Lạt"), 4.7, 4.6,
                    [unicodedata.normalize(encoding, token), "Đông khách", "Giá hơi cao"], "Ổn",
                )
                self.assertEqual(cand.venue_name, "Quán Đẹp")
                self.assertEqual(cand.goong_returned_commune, "Đà Lạt")
                self.assertTrue(evaluate_scraped_candidate(cand).rejection_reason.startswith("SHILL_TRAP_VETO"))

        for overrides, reason in [
            ({"expected_commune": "", "goong_returned_commune": ""}, "INVALID_COMMUNE"),
            ({"goong_returned_commune": " "}, "INVALID_COMMUNE"),
            ({"lifetime_rating": 99.0}, "INVALID_RATING"),
            ({"trailing_90d_rating": float("nan")}, "INVALID_RATING"),
            ({"lifetime_rating": float("inf")}, "INVALID_RATING"),
            ({"sampled_low_reviews": ["Một", "Hai", " "]}, "INSUFFICIENT_LOW_REVIEW_SAMPLE"),
            ({"goong_returned_commune": "Biên Hòa"}, "COMMUNE_MISMATCH"),
            ({"lifetime_rating": 4.6, "trailing_90d_rating": 4.2}, "SENTIMENT_DRIFT_DECAY"),
        ]:
            with self.subTest(reason=reason, overrides=overrides):
                values = dict(venue_name="Quán", expected_commune="Đà Lạt", goong_returned_commune="Đà Lạt",
                              lifetime_rating=4.5, trailing_90d_rating=4.5,
                              sampled_low_reviews=["Đông khách", "Chờ lâu", "Giá cao"], raw_testimonial_text=dirty_text)
                values.update(overrides)
                verdict = evaluate_scraped_candidate(RawScrapedCandidate(**values))
                self.assertFalse(verdict.accepted)
                self.assertTrue(verdict.rejection_reason.startswith(reason), verdict.rejection_reason)

        jsonld_nodes = [{"@graph": [
            {"@type": "Hotel", "name": "Khách Sạn Dambri", "aggregateRating": {"ratingValue": 4.9}},
            {"@type": "Product", "name": "Thác Dambri", "aggregateRating": {"ratingValue": 4.9}},
            {"@type": ["Place", "TouristAttraction"], "name": "Khu Du Lịch Thác Dambri Bảo Lộc",
             "aggregateRating": {"ratingValue": 4.2},
             "review": {"reviewBody": "Thác đẹp SĐT0909888777"}},
        ]}]
        extracted = extract_entity_rating_from_jsonld(jsonld_nodes, unicodedata.normalize("NFD", "Thác Dambri"))
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted[0], 4.2)
        self.assertEqual(extracted[1], ["Thác đẹp SĐT[REDACTED_PII]"])
        self.assertIsNone(extract_entity_rating_from_jsonld(jsonld_nodes, ""))
        preds = [
            {"place_id": "wrong_1", "description": "Bò Sữa, Biên Hòa", "compound": {"commune": "Biên Hòa"}},
            {"place_id": "right_2", "description": "Bò Sữa, Long Thành", "compound": {"commune": "Long Thành"}},
        ]
        self.assertEqual(select_commune_matching_prediction(preds, "Long Thành")["place_id"], "right_2")
        self.assertEqual(select_commune_matching_prediction(preds, "Đam Bri")["place_id"], "wrong_1")
        self.assertIsNone(select_commune_matching_prediction([], "Long Thành"))
        self.assertEqual(select_commune_matching_prediction([
            {"place_id": "substring", "compound": {"commune": "Thanh An"}},
            {"place_id": "exact", "description": "Quán, Xã An, Việt Nam"},
        ], "An")["place_id"], "exact")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "promo.db"
            from scripts.seed_db import build_database
            build_database(tmp_db)
            today = datetime.now(timezone.utc).date()
            low_reviews = ["Chờ lâu", "Giá cao", "Bãi xe chật", "Đông khách", "Phục vụ chậm"]
            evidence = [dict(text=text, rating=2.0, published_at=(today - timedelta(days=100+i)).isoformat(),
                             source_url=f"https://example.org/reviews/low/{i}") for i, text in enumerate(low_reviews)]
            evidence += [dict(text=f"Đánh giá thử nghiệm {i} SĐT0909111222", rating=4.5,
                              published_at=(today - timedelta(days=i+1)).isoformat(),
                              source_url=f"https://example.org/reviews/recent/{i}") for i in range(10)]
            good_cand = RawScrapedCandidate(
                venue_name=unicodedata.normalize("NFD", "Trạm Nghỉ Thử Nghiệm"),
                expected_commune="Long Thành", goong_returned_commune="Long Thành",
                lifetime_rating=4.6, trailing_90d_rating=4.5, sampled_low_reviews=low_reviews,
                raw_testimonial_text=dirty_text, poi_id="dl_live_promoted_test_stop", corridor_id="hcm_dalat",
                goong_place_id="test_place", source_url="https://example.org/venue", lat=11.2015, lng=107.3512,
                corridor_km=114.0, open_minute=360, close_minute=1200, dwell_minutes=20,
                direction_allowed="BOTH", sedan_safe=True, has_ev_charge=True, midday_sun_trap=False, is_arrival_anchor=False,
                exertion_level="LOW", total_review_count=100, total_low_review_count=10, review_evidence=evidence,
                co_occurring_poi_ids=["dl_tram_dung_tam_chau_bao_loc"],
                co_occurrence_sources={"dl_tram_dung_tam_chau_bao_loc": ["https://example.org/trip/1", "https://example.org/trip/2"]},
            )
            reg_promo = CorridorRegistry(tmp_db)
            with patch.object(app.state, "registry", reg_promo):
                before = self.client.get("/v1/corridors/health").json()
                self.assertEqual(next(c["verified_poi_count"] for c in before["corridors"] if c["corridor_id"] == "hcm_dalat"), 0)
            good_cand.review_evidence = evidence[:5]
            insufficient = promote_verified_candidate_to_db(tmp_db, good_cand)
            self.assertFalse(insufficient.accepted)
            self.assertTrue(insufficient.rejection_reason.startswith("INSUFFICIENT_RECENT_REVIEW_EVIDENCE"))
            good_cand.review_evidence = evidence
            self.assertTrue(promote_verified_candidate_to_db(tmp_db, good_cand).accepted)
            self.assertTrue(promote_verified_candidate_to_db(tmp_db, good_cand).accepted)
            with sqlite3.connect(tmp_db) as conn:
                stored = conn.execute("SELECT name_vi,has_ev_charge,open_minute,close_minute,tradeoff_note_vi FROM pois WHERE poi_id=?",
                                      (good_cand.poi_id,)).fetchone()
                self.assertEqual(stored[:4], ("Trạm Nghỉ Thử Nghiệm", 1, 360, 1200))
                self.assertEqual(stored[4], scrub_pii(dirty_text))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM poi_review_evidence").fetchone()[0], 15)
                self.assertNotIn("0909111222", str(conn.execute("SELECT review_text FROM poi_review_evidence").fetchall()))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM poi_co_occurrence_sources").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingestion_audit WHERE accepted=0").fetchone()[0], 1)
            with patch.object(app.state, "registry", reg_promo):
                health = self.client.get("/v1/corridors/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(next(c["verified_poi_count"] for c in health.json()["corridors"] if c["corridor_id"] == "hcm_dalat"), 1)
            # A cross-corridor pair must roll the whole promotion back, not leave a partial POI.
            good_cand.poi_id = "dl_rollback_test"
            good_cand.co_occurring_poi_ids = ["vt_nha_lon_long_son"]
            good_cand.co_occurrence_sources = {"vt_nha_lon_long_son": ["https://example.org/trip/1", "https://example.org/trip/2"]}
            with self.assertRaises(sqlite3.IntegrityError):
                promote_verified_candidate_to_db(tmp_db, good_cand)
            with sqlite3.connect(tmp_db) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM pois WHERE poi_id='dl_rollback_test'").fetchone())

if __name__ == "__main__":
    unittest.main(verbosity=2)
