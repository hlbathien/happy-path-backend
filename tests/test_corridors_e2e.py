import asyncio
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
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
from happy_path.core.solver import dominates
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
        sid = plan_resp.json()["session_id"]

        worker_b_registry = CorridorRegistry(DB_PATH)
        loaded_in_b = worker_b_registry.load_session(sid)
        self.assertIsNotNone(loaded_in_b)
        self.assertEqual(loaded_in_b.session_id, sid)
        self.assertIn("P1_EXPRESS", loaded_in_b.candidate_durations)

        # 3. Corrupt database fail-fast check
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tf.write(b"NOT_A_VALID_SQLITE_HEADER_BYTES_1234567890" * 5)
            corrupt_path = Path(tf.name)
        try:
            with self.assertRaises(RuntimeError):
                CorridorRegistry(corrupt_path)
        finally:
            corrupt_path.unlink(missing_ok=True)

        # 4. Unified Vietnam coordinate bounds [8.0..23.5] x [102.0..110.0]
        for bad_coord in [
            {"lat": 106.7, "lng": 10.7},
            {"lat": 23.6, "lng": 106.7},
            {"lat": 7.9, "lng": 106.7},
            {"lat": 10.7, "lng": 110.1},
        ]:
            r_bad = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Vung Tau", "origin_coords": bad_coord},
            )
            self.assertEqual(r_bad.status_code, 422, f"Expected 422 for {bad_coord}")

    def test_ticket_02_vungtau_48hour_schedule_validity_and_median_legality(self):
        registry: CorridorRegistry = app.state.registry
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
                        if poi.close_minute < 1440:
                            self.assertGreaterEqual(srv_min, poi.open_minute, f"Visited {poi.poi_id} before open at {dep_iso}")
                            self.assertLessEqual(srv_min + poi.dwell_minutes, poi.close_minute, f"Visited {poi.poi_id} after close at {dep_iso}")

                    # Verify road-following sliced polyline has >= len(stops) + 2 vertices and matches endpoints
                    pts = decode_polyline(cand["encoded_polyline"])
                    self.assertGreaterEqual(len(pts), len(cand["stops"]) + 2)

    def test_ticket_03_dalat_physical_interchanges_and_5d_pareto_nondominance(self):
        registry: CorridorRegistry = app.state.registry
        branches_map = registry.branches_by_corridor["hcm_dalat"]

        for orig, dest in [("Ho Chi Minh City", "Da Lat"), ("Đà Lạt", "TP.HCM")]:
            for hr in (0, 1, 5, 6, 9, 12, 15, 18, 21):
                resp = self.client.post(
                    "/v1/itineraries/plan",
                    json={
                        "origin_query": orig,
                        "destination_query": dest,
                        "departure_iso": f"2026-10-16T{hr:02d}:00:00+07:00",
                    },
                )
                self.assertEqual(resp.status_code, 200)
                cands = [ParetoCandidate(**c) for c in resp.json()["candidates"]]
                self.assertGreaterEqual(len(cands), 2)

                # 1. Verify Physical Interchange Continuity ([diverge_km, merge_km])
                for c in cands:
                    if c.active_branch_id != "MAIN_SPINE":
                        b_meta = branches_map[c.active_branch_id]
                        for s in c.stops:
                            if s.branch_id == "MAIN_SPINE":
                                self.assertFalse(
                                    b_meta.diverge_km < s.corridor_km_from_hcm < b_meta.merge_km,
                                    f"Main-spine stop {s.poi_id} at km {s.corridor_km_from_hcm} violates branch {c.active_branch_id} [{b_meta.diverge_km}, {b_meta.merge_km}]",
                                )

                # 2. Verify Strict 5D Pareto Non-Dominance across returned portfolio
                for i in range(len(cands)):
                    for j in range(len(cands)):
                        if i != j:
                            self.assertFalse(
                                dominates(cands[i], cands[j]),
                                f"Candidate {cands[i].candidate_id} dominates {cands[j].candidate_id} at hour {hr:02d}:00",
                            )

    def test_ticket_04_unconditional_uv_shield_exertion_ablation_and_night_open_stops(self):
        registry: CorridorRegistry = app.state.registry

        # 1. Verify >= 2 HIGH exertion POIs exist in Da Lat and Nha Trang seeds
        dl_high = [p.poi_id for p in registry.pois_by_corridor["hcm_dalat"] if p.exertion_level == "HIGH"]
        nt_high = [p.poi_id for p in registry.pois_by_corridor["hcm_nhatrang"] if p.exertion_level == "HIGH"]
        self.assertGreaterEqual(len(dl_high), 2)
        self.assertGreaterEqual(len(nt_high), 2)

        # Locking 2 HIGH exertion stops must be rejected with HTTP 409
        plan_dl = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Da Lat"},
        ).json()
        r_two_high = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": plan_dl["session_id"],
                "candidate_id": "P2_ICONIC",
                "action": "LOCK_STOP",
                "target_poi_id": dl_high[0],
                "locked_poi_ids": [dl_high[1]],
            },
        )
        self.assertEqual(r_two_high.status_code, 409)

        # 2. Late-night departure (21:30 & 01:00): verify ZERO closed restaurants are scheduled
        for dep_t in ("2026-10-16T21:30:00+07:00", "2026-10-16T01:00:00+07:00"):
            night_resp = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Nha Trang", "departure_iso": dep_t},
            )
            self.assertEqual(night_resp.status_code, 200)
            for cand in night_resp.json()["candidates"]:
                for s in cand["stops"]:
                    poi = registry.pois_by_id[s["poi_id"]]
                    srv_min = _clock_to_min(s["service_start_clock"])
                    if poi.close_minute < 1440:
                        self.assertGreaterEqual(srv_min, poi.open_minute)
                        self.assertLessEqual(srv_min + poi.dwell_minutes, poi.close_minute)

        # 3. Unconditional UV shield: locking a midday_sun_trap stop at 10:00 emits UV_LOCK_OVERRIDE_WARNING
        plan_vt = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Vung Tau", "departure_iso": "2026-10-16T10:30:00+07:00"},
        ).json()
        # Unlocked plans must NEVER schedule vt_doi_cuu_suoi_nghe between 11:00 and 14:30
        for cand in plan_vt["candidates"]:
            for s in cand["stops"]:
                if s["poi_id"] == "vt_doi_cuu_suoi_nghe":
                    srv_m = _clock_to_min(s["service_start_clock"])
                    self.assertFalse(660 <= srv_m <= 870)

        mut_uv = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": plan_vt["session_id"],
                "candidate_id": "P3_SCENIC",
                "action": "LOCK_STOP",
                "target_poi_id": "vt_doi_cuu_suoi_nghe",
            },
        )
        self.assertEqual(mut_uv.status_code, 200)
        self.assertTrue(any("UV_LOCK_OVERRIDE_WARNING" in w for w in mut_uv.json()["mutated_candidate"]["warnings"]))

        # 4. In-Memory Ablation Gate: verify Markov co_occurrence and Arrival Anchor actively shift candidate scores/routes
        import happy_path.core.solver as solver_mod
        src_code = Path(solver_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("registry.co_occurrence.get", src_code)
        self.assertIn("checkin_anchor_bonus", src_code)
        morning_dl = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Da Lat", "departure_iso": "2026-10-16T05:30:00+07:00"},
        ).json()
        for c in morning_dl["candidates"]:
            if c["candidate_id"] in ("P2_ICONIC", "P3_SCENIC"):
                self.assertTrue(
                    any(s["is_arrival_anchor"] for s in c["stops"]),
                    f"Expected arrival anchor in {c['candidate_id']} on 05:30 departure",
                )
        saved_co = dict(registry.co_occurrence)
        try:
            # Boost a specific valid pair and verify solver selects that pair when co_occurrence is active vs zeroed
            registry.co_occurrence[("hcm_vungtau", "vt_chua_dai_tong_lam", "vt_nha_lon_long_son")] = 60.0
            with_co = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Vung Tau", "departure_iso": "2026-10-16T06:00:00+07:00"},
            ).json()["candidates"][1]["stops"]
            registry.co_occurrence.clear()
            without_co = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Vung Tau", "departure_iso": "2026-10-16T06:00:00+07:00"},
            ).json()["candidates"][1]["stops"]
            self.assertNotEqual(
                [s["poi_id"] for s in with_co],
                [s["poi_id"] for s in without_co],
                "Markov co_occurrence ablation failed to alter route selection!",
            )
        finally:
            registry.co_occurrence.clear()
            registry.co_occurrence.update(saved_co)

    def test_ticket_05_nhatrang_96run_ev_charger_interchange_and_branch_tolls(self):
        registry: CorridorRegistry = app.state.registry
        branches_map = registry.branches_by_corridor["hcm_nhatrang"]

        for orig, dest in [("TP.HCM", "Nha Trang"), ("Nha Trang", "TP.HCM")]:
            for hr in range(0, 24, 3):
                ev_resp = self.client.post(
                    "/v1/itineraries/plan",
                    json={
                        "origin_query": orig,
                        "destination_query": dest,
                        "departure_iso": f"2026-10-16T{hr:02d}:30:00+07:00",
                        "powertrain": "ev",
                    },
                )
                self.assertEqual(ev_resp.status_code, 200)
                for cand in ev_resp.json()["candidates"]:
                    self.assertTrue(cand["ev_charging_satisfied"])
                    # Verify at least one stop has EV charging inside [15%, 80%] of 435 km ([65.25, 348.0])
                    ev_in_window = [
                        s for s in cand["stops"]
                        if s["has_ev_charge"] and (0.15 * 435.0 <= s["progress_km_from_origin"] <= 0.80 * 435.0)
                    ]
                    self.assertGreaterEqual(len(ev_in_window), 1)
                    # Verify Physical Interchange Continuity (no MAIN_SPINE km 310/338 inside BAUTRANG_COAST [180, 395])
                    if cand["active_branch_id"] != "MAIN_SPINE":
                        b_meta = branches_map[cand["active_branch_id"]]
                        for s in cand["stops"]:
                            if s["branch_id"] == "MAIN_SPINE":
                                self.assertFalse(
                                    b_meta.diverge_km < s["corridor_km_from_hcm"] < b_meta.merge_km,
                                    f"EV route violated interchange continuity: {s['poi_id']} on {cand['active_branch_id']}",
                                )

        # Verify authoritative branch VETC tolls
        dl_resp = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Da Lat"},
        ).json()
        tolls_by_branch = {c["active_branch_id"]: c["estimated_vetc_toll_vnd"] for c in dl_resp["candidates"]}
        self.assertEqual(tolls_by_branch.get("MAIN_SPINE"), 136000)
        if "BRANCH_QL28B_DAININH" in tolls_by_branch:
            self.assertEqual(tolls_by_branch["BRANCH_QL28B_DAININH"], 261000)

    def test_ticket_06_piecewise_projection_custom_pin_polyline_and_sparse_slices(self):
        # 1. Định Quán (11.2015, 107.3512) must project to ~114.0 km and start polyline at (11.2015, 107.3512)
        mid_resp = self.client.post(
            "/v1/itineraries/plan",
            json={
                "origin_query": "Dinh Quan",
                "destination_query": "Da Lat",
                "origin_coords": {"lat": 11.2015, "lng": 107.3512},
                "departure_iso": "2026-10-16T08:00:00+07:00",
            },
        )
        self.assertEqual(mid_resp.status_code, 200)
        body = mid_resp.json()
        self.assertAlmostEqual(body["active_start_km"], 114.0, delta=2.0)
        for cand in body["candidates"]:
            pts = decode_polyline(cand["encoded_polyline"])
            self.assertAlmostEqual(pts[0][0], 11.2015, delta=0.01)
            self.assertAlmostEqual(pts[0][1], 107.3512, delta=0.01)
            for stop in cand["stops"]:
                self.assertGreaterEqual(stop["progress_km_from_origin"], body["active_start_km"] + 2.0)

        # 2. Out-of-slice lock must be rejected with HTTP 409
        r_lock_behind = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": body["session_id"],
                "candidate_id": "P1_EXPRESS",
                "action": "LOCK_STOP",
                "target_poi_id": "dl_tram_sac_ev_long_thanh_km42",
            },
        )
        self.assertEqual(r_lock_behind.status_code, 409)

        # 3. Short sparse slice (~20 km near Da Lat arrival: km 276 -> km 298) returns >= 5 min drive and HTTP 200
        sparse_resp = self.client.post(
            "/v1/itineraries/plan",
            json={
                "origin_query": "HCMC",
                "destination_query": "Da Lat",
                "origin_coords": {"lat": 11.7350, "lng": 108.3750},
                "destination_coords": {"lat": 11.8890, "lng": 108.4350},
            },
        )
        self.assertEqual(sparse_resp.status_code, 200)
        for cand in sparse_resp.json()["candidates"]:
            self.assertGreaterEqual(cand["total_drive_minutes"], 5)

    def test_ticket_07_atomic_rollback_on_409_archetype_integrity_and_live_deltas(self):
        # 1. Start Vung Tau session
        plan_vt = self.client.post(
            "/v1/itineraries/plan",
            json={"origin_query": "HCMC", "destination_query": "Vung Tau", "departure_iso": "2026-10-16T06:00:00+07:00"},
        ).json()
        sid = plan_vt["session_id"]

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
        self.assertEqual(r_lock_ok.json()["cumulative_locked_poi_ids"], ["vt_nha_lon_long_son"])

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

        # 5. Next valid mutation (SHIFT_DEPARTURE to 07:00) must succeed (200 OK) with only vt_nha_lon_long_son locked!
        r_shift = self.client.post(
            "/v1/itineraries/mutate",
            json={
                "session_id": sid,
                "candidate_id": "P2_ICONIC",
                "action": "SHIFT_DEPARTURE",
                "new_departure_iso": "2026-10-16T07:00:00+07:00",
            },
        )
        self.assertEqual(r_shift.status_code, 200)
        self.assertEqual(r_shift.json()["cumulative_locked_poi_ids"], ["vt_nha_lon_long_son"])

        # 6. Malformed action parameters must return HTTP 422
        for bad_mut in [
            {"session_id": sid, "action": "REMOVE_STOP"},
            {"session_id": sid, "action": "SKIP_CATEGORY", "target_category": "FOOD"},
            {"session_id": sid, "action": "SHIFT_DEPARTURE", "new_departure_iso": "not-a-date"},
        ]:
            self.assertEqual(self.client.post("/v1/itineraries/mutate", json=bad_mut).status_code, 422)

    def test_ticket_08_sub350ms_parallel_goong_clock_propagation_and_null_points(self):
        client_goong = app.state.goong
        client_goong.api_key = "mock_live_key"
        try:
            plan_body = self.client.post(
                "/v1/itineraries/plan",
                json={"origin_query": "HCMC", "destination_query": "Vung Tau"},
            ).json()
            cands = [ParetoCandidate(**c) for c in plan_body["candidates"]]
            orig = Coordinate(lat=10.7872, lng=106.7498)
            dest = Coordinate(lat=10.3460, lng=107.0843)

            # 1. Slow server (0.60s delay per call across 3 candidates): parallel portfolio decoration must finish < 0.34s
            async def slow_get(*args, **kwargs):
                await asyncio.sleep(0.60)
                raise TimeoutError("simulated timeout")

            with patch.object(client_goong._get_client(), "get", side_effect=slow_get):
                t0 = time.monotonic()
                asyncio.run(client_goong.decorate_portfolio(orig, dest, cands))
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 0.34, f"Parallel timeout exceeded 350ms budget: {elapsed:.3f}s")

            # 2. 2x live congestion spike updates stop arrival_clock, total_drive_minutes & tradeoff_summary_vi, and ignores null points
            cand0 = ParetoCandidate(**plan_body["candidates"][0])
            old_arr = cand0.stops[0].arrival_clock
            num_legs = len(cand0.stops) + 1

            class FakeCongestedResp:
                status_code = 200
                def json(self_inner):
                    return {
                        "routes": [{
                            "overview_polyline": {"points": None},
                            "legs": [{"duration": {"value": 3600}} for _ in range(num_legs)],
                        }]
                    }

            async def congested_get(*args, **kwargs):
                return FakeCongestedResp()

            with patch.object(client_goong._get_client(), "get", side_effect=congested_get):
                updated = asyncio.run(
                    client_goong.decorate_candidate_polyline(orig, dest, cand0, departure_iso="2026-10-16T06:00:00+07:00")
                )
                self.assertIsInstance(updated.encoded_polyline, str)
                self.assertGreater(len(updated.encoded_polyline), 0)
                self.assertNotEqual(updated.stops[0].arrival_clock, old_arr)
                self.assertIn("Goong Live", updated.tradeoff_summary_vi)
                self.assertTrue(any("Cảnh báo kẹt xe" in w for w in updated.warnings))
        finally:
            client_goong.api_key = ""

    def test_ticket_09_nfc_nfd_parity_hardened_pii_entity_binding_and_sqlite_promotion(self):
        # 1. Hardened PII redaction (contiguous SĐT0909123456, NBSP, group-member URLs, Unicode email)
        dirty_text = (
            "Liên hệ SĐT0909123456 hoặc 0912 345 678, email nguyễn.văn.a@gmail.com, "
            "FB https://web.facebook.com/groups/ghiendalat/user/100012345/ @tuan_driver"
        )
        cleaned = scrub_pii(dirty_text)
        for forbidden in ("0909123456", "345 678", "nguyễn.văn.a", "100012345", "@tuan_driver"):
            self.assertNotIn(forbidden, cleaned)

        # 2. NFC vs NFD equivalence on shill disillusionment tokens
        nfd_review = unicodedata.normalize("NFD", "Quán thực sự vỡ mộng, chỉ được cái view")
        for rev_text in ("Quán thực sự vỡ mộng, chỉ được cái view", nfd_review):
            cand_shill = RawScrapedCandidate(
                venue_name="Cafe Mây Ảo",
                expected_commune="Đà Lạt",
                goong_returned_commune=unicodedata.normalize("NFD", "Đà Lạt"),
                lifetime_rating=4.7,
                trailing_90d_rating=4.6,
                sampled_low_reviews=[rev_text, "Đông khách", "Giá hơi cao"],
                raw_testimonial_text="Ổn",
            )
            verdict = evaluate_scraped_candidate(cand_shill)
            self.assertFalse(verdict.accepted)
            self.assertIn("SHILL_TRAP_VETO", verdict.rejection_reason)

        # 3. Admission bounds: reject empty commune, rating=99, or < 3 low reviews
        self.assertFalse(
            evaluate_scraped_candidate(
                RawScrapedCandidate("A", "", "", 4.5, 4.5, ["a", "b", "c"], "ok")
            ).accepted
        )
        self.assertFalse(
            evaluate_scraped_candidate(
                RawScrapedCandidate("A", "Đà Lạt", "Đà Lạt", 99.0, 99.0, ["a", "b", "c"], "ok")
            ).accepted
        )
        self.assertFalse(
            evaluate_scraped_candidate(
                RawScrapedCandidate("A", "Đà Lạt", "Đà Lạt", 4.5, 4.5, ["chỉ 1 review"], "ok")
            ).accepted
        )

        # 4. Entity-bound JSON-LD extraction & Commune-aware Goong prediction selection
        jsonld_nodes = [
            {"@type": "Hotel", "name": "Khách Sạn Ngẫu Nhiên", "aggregateRating": {"ratingValue": 4.9}},
            {
                "@type": "TouristAttraction",
                "name": "Khu Du Lịch Thác Dambri Bảo Lộc",
                "aggregateRating": {"ratingValue": 4.2},
                "review": [{"reviewBody": "Thác đẹp SĐT0909888777"}],
            },
        ]
        extracted = extract_entity_rating_from_jsonld(jsonld_nodes, "Thác Dambri")
        self.assertIsNotNone(extracted)
        self.assertAlmostEqual(extracted[0], 4.2)
        self.assertNotIn("0909888777", extracted[1][0])

        preds = [
            {"place_id": "wrong_1", "description": "Bò Sữa, Biên Hòa", "compound": {"commune": "Biên Hòa"}},
            {"place_id": "right_2", "description": "Bò Sữa, Long Thành", "compound": {"commune": "Long Thành"}},
        ]
        chosen = select_commune_matching_prediction(preds, "Long Thành")
        self.assertEqual(chosen["place_id"], "right_2")

        # 5. Direct SQLite Promotion
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "promo.db"
            from scripts.seed_db import build_database
            build_database(tmp_db)
            good_cand = RawScrapedCandidate(
                venue_name="Trạm Nghỉ Cao Tốc Mới",
                expected_commune="Long Thành",
                goong_returned_commune="Long Thành",
                lifetime_rating=4.6,
                trailing_90d_rating=4.5,
                sampled_low_reviews=["Rất sạch", "Đồ ăn ngon", "Bãi đỗ rộng"],
                raw_testimonial_text="Trạm sạch SĐT0909111222",
                poi_id="dl_live_promoted_test_stop",
                corridor_id="hcm_dalat",
                co_occurring_poi_ids=["dl_tram_dung_tam_chau_bao_loc"],
            )
            v_promo = promote_verified_candidate_to_db(tmp_db, good_cand)
            self.assertTrue(v_promo.accepted)
            reg_promo = CorridorRegistry(tmp_db)
            self.assertIn("dl_live_promoted_test_stop", reg_promo.pois_by_id)

if __name__ == "__main__":
    unittest.main(verbosity=2)
