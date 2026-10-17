"""Regressions at the public API and real SQLite boundaries identified by the audit."""
import asyncio
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from happy_path.config import DB_PATH
from happy_path.core.graph import CorridorRegistry
from happy_path.main import app


def minute(clock):
    return int(clock[:2]) * 60 + int(clock[3:])


class TestRuntimeRepairs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def plan(self, **changes):
        response = self.client.post('/v1/itineraries/plan', json={
            'origin_query': 'HCMC', 'destination_query': 'Vung Tau',
            'departure_iso': '16:00', **changes,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_live_publication_rechecks_hours_and_mutation_rolls_back(self):
        baseline = self.plan()
        registry = app.state.registry
        legs_by_stops = {}
        for candidate in baseline['candidates']:
            clock, legs = 960, []
            for stop in candidate['stops']:
                legs.append((minute(stop['arrival_clock']) - clock) % 1440)
                clock = minute(stop['departure_clock'])
            legs.append(candidate['total_drive_minutes'] - sum(legs))
            legs_by_stops[tuple(s['coordinates']['lat'] for s in candidate['stops'])] = legs

        def traffic(request):
            coords = request.url.params['destination'].split(';')[:-1]
            key = tuple(float(coord.split(',')[0]) for coord in coords)
            legs = legs_by_stops[key]
            return httpx.Response(200, json={'routes': [{'legs': [
                {'duration': {'value': value * 120}} for value in legs
            ]}]})

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.MockTransport(traffic)) as live:
                with patch.object(app.state.goong, 'api_key', 'regression-only'), patch.object(app.state.goong, '_client', live):
                    response = self.client.post('/v1/itineraries/plan', json={
                        'origin_query': 'HCMC', 'destination_query': 'Vung Tau', 'departure_iso': '16:00',
                    })
                    self.assertIn(response.status_code, (200, 409))
                    if response.status_code == 200:
                        for candidate in response.json()['candidates']:
                            for stop in candidate['stops']:
                                poi = registry.pois_by_id[stop['poi_id']]
                                if (poi.open_minute, poi.close_minute) != (0, 1440):
                                    service = minute(stop['service_start_clock'])
                                    self.assertGreaterEqual(service, poi.open_minute)
                                    self.assertLessEqual(service + stop['dwell_minutes'], poi.close_minute)
                    before = registry.load_session(baseline['session_id'])
                    mutation = self.client.post('/v1/itineraries/mutate', json={
                        'session_id': baseline['session_id'], 'candidate_id': 'P1_EXPRESS',
                        'action': 'SHIFT_DEPARTURE', 'new_departure_iso': '16:00',
                    })
                    self.assertEqual(mutation.status_code, 409, mutation.text)
                    self.assertEqual(registry.load_session(baseline['session_id']), before)
        asyncio.run(exercise())

    def test_live_rejection_is_not_lost_when_siblings_timeout(self):
        baseline = self.plan()
        first = next(c for c in baseline['candidates'] if c['candidate_id'] == 'P1_EXPRESS')
        destination = ';'.join([
            f"{s['coordinates']['lat']:.6f},{s['coordinates']['lng']:.6f}" for s in first['stops']
        ] + ['10.346000,107.084300'])

        async def exercise():
            async def traffic(request):
                if request.url.params['destination'] != destination:
                    await asyncio.sleep(0.6)
                legs = [{'duration': {'value': 5 * 60}} for _ in range(len(first['stops']) + 1)]
                legs[len(first['stops']) - 1] = {'duration': {'value': 360 * 60}}
                return httpx.Response(200, json={'routes': [{'legs': legs}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(traffic)) as live:
                with patch.object(app.state.goong, 'api_key', 'regression-only'), patch.object(app.state.goong, '_client', live):
                    response = self.client.post('/v1/itineraries/plan', json={
                        'origin_query': 'HCMC', 'destination_query': 'Vung Tau', 'departure_iso': '16:00',
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertNotIn('P1_EXPRESS', [c['candidate_id'] for c in response.json()['candidates']])
                    self.assertTrue(all(any('GOONG_UNAVAILABLE' in w for w in c['warnings'])
                                        for c in response.json()['candidates']))
        asyncio.run(exercise())

    def test_real_opening_wait_is_recomputed_after_traffic(self):
        baseline = self.plan(origin_query='Vung Tau', destination_query='HCMC', departure_iso='05:54')
        poi_id = 'vt_banh_khot_bai_sau'
        excluded = [p.poi_id for p in app.state.registry.pois_by_corridor['hcm_vungtau'] if p.poi_id != poi_id]

        async def exercise():
            def traffic(request):
                return httpx.Response(200, json={'routes': [{'legs': [
                    {'duration': {'value': 72 * 60}}, {'duration': {'value': 200 * 60}},
                ]}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(traffic)) as live:
                with patch.object(app.state.goong, 'api_key', 'regression-only'), patch.object(app.state.goong, '_client', live):
                    result = self.client.post('/v1/itineraries/mutate', json={
                        'session_id': baseline['session_id'], 'candidate_id': 'P1_EXPRESS',
                        'action': 'LOCK_STOP', 'target_poi_id': poi_id, 'rejected_poi_ids': excluded,
                    })
                    self.assertEqual(result.status_code, 200, result.text)
                    stop = result.json()['mutated_candidate']['stops'][0]
                    self.assertEqual(stop['arrival_clock'], '07:06')
                    self.assertEqual(stop['service_start_clock'], '07:06')
                    self.assertEqual(stop['wait_minutes'], 0)
        asyncio.run(exercise())

    def test_overlapping_mutations_preserve_both_acknowledged_changes(self):
        baseline = self.plan(departure_iso='06:00')

        async def exercise():
            async def delayed(request):
                await asyncio.sleep(0.04)
                return httpx.Response(200, json={'routes': []})
            async with httpx.AsyncClient(transport=httpx.MockTransport(delayed)) as live:
                with patch.object(app.state.goong, 'api_key', 'regression-only'), patch.object(app.state.goong, '_client', live):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
                        results = await asyncio.gather(*[client.post('/v1/itineraries/mutate', json={
                            'session_id': baseline['session_id'], 'candidate_id': 'P1_EXPRESS', **change,
                        }) for change in [
                            {'action': 'SHIFT_DEPARTURE', 'new_departure_iso': '07:00'},
                            {'action': 'SKIP_CATEGORY', 'target_category': 'viewpoint'},
                        ]])
                        self.assertEqual([r.status_code for r in results], [200, 200])
            stored = app.state.registry.load_session(baseline['session_id'])
            self.assertEqual(stored.departure_iso, '07:00')
            self.assertIn('viewpoint', stored.skip_categories)
        asyncio.run(exercise())

    def test_busy_writer_never_selects_another_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contended.db'
            shutil.copy2(DB_PATH, path)
            writer = sqlite3.connect(path, check_same_thread=False)
            writer.execute('BEGIN IMMEDIATE')
            def release():
                time.sleep(5.2)  # Exceeds the old probe's timeout on a real writer lock.
                writer.rollback()
                writer.close()
            thread = threading.Thread(target=release)
            thread.start()
            try:
                registry = CorridorRegistry(path)
                self.assertEqual(registry.runtime_db_path, path.resolve())
            finally:
                thread.join()

    def test_timezone_equivalence_far_pin_rejection_and_unknown_session(self):
        utc = self.plan(departure_iso='2026-10-16T00:00:00Z')
        vietnam = self.plan(departure_iso='2026-10-16T07:00:00+07:00')
        self.assertEqual(utc['departure_clock'], vietnam['departure_clock'])
        self.assertEqual(utc['candidates'], vietnam['candidates'])
        far = self.client.post('/v1/itineraries/plan', json={
            'origin_query': 'HCMC', 'destination_query': 'Vung Tau',
            'origin_coords': {'lat': 21.0285, 'lng': 105.8542},
        })
        self.assertEqual(far.status_code, 422, far.text)
        self.assertIn('CORRIDOR_ACCESS_OUT_OF_RANGE', far.text)
        missing = self.client.post('/v1/itineraries/mutate', json={
            'session_id': 'never-created-session', 'corridor_id': 'hcm_vungtau',
            'candidate_id': 'P1_EXPRESS', 'action': 'SHIFT_DEPARTURE', 'new_departure_iso': '07:00',
        })
        self.assertEqual(missing.status_code, 404, missing.text)
