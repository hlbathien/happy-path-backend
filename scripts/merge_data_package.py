#!/usr/bin/env python3
"""Union a bounded data archive with local intake, never trusting its accepted flags.

Only configured branch controls and branch-bound tolls enter the seed model. POIs
must pass the existing evidence-backed SQLite promotion path; originals remain in
scrubbed provenance regardless of their admission outcome. No packaged code runs.
"""
import argparse
from collections import Counter
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
import zipfile

from happy_path.config import DB_PATH
from happy_path.core.graph import CorridorRegistry
from happy_path.core.ingestion import RawScrapedCandidate, normalize_text_fields, promote_verified_candidate_to_db, scrub_pii
from migrate_db import migrate_database

ROOT = Path(__file__).resolve().parent.parent


def scrub_values(value):
    if isinstance(value, str):
        return scrub_pii(value)
    if isinstance(value, list):
        return [scrub_values(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_values(item) for key, item in value.items()}
    return value


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def merge(archive: Path, db_path: Path):
    source = ROOT / 'data/provenance/v5'
    with zipfile.ZipFile(archive) as package:
        prefix = 'happy-path-data-package/'
        # Fixed allowlist: archive paths cannot escape the destination or replace production scripts/DB.
        names = ['seed_corridors.json', 'raw_scraped_candidates.json', 'ingestion_audit_seed.json',
                 'provenance/google_maps_places_raw.json', 'provenance/google_maps_directions_raw.json',
                 'provenance/youtube_2026_roadtrip_transcripts.json', 'provenance/otosaigon_and_evcs_corpus.json']
        documents = {}
        for name in names:
            documents[name] = scrub_values(normalize_text_fields(json.loads(package.read(prefix + 'data/' + name))))
            dump(source / name, documents[name])
        (source / 'DATA_MANIFEST_AND_REMEDIATION_GUIDE.md').write_text(
            scrub_pii(package.read(prefix + 'DATA_MANIFEST_AND_REMEDIATION_GUIDE.md').decode()), encoding='utf-8')
    incoming = documents['seed_corridors.json']
    raw = documents['raw_scraped_candidates.json']
    seed_path = ROOT / 'data/seed_corridors.json'
    seed = json.loads(seed_path.read_text(encoding='utf-8'))
    skipped_plazas = []
    for corridor in seed['corridors']:
        other = next(c for c in incoming['corridors'] if c['corridor_id'] == corridor['corridor_id'])
        branches = {b['branch_id']: b for b in corridor['branches']}
        for branch in other['branches']:
            if branch['branch_id'] in branches:
                for key in ('real_branch_km', 'branch_waypoints'):
                    if key in branch:
                        branches[branch['branch_id']][key] = branch[key]
        plazas = {p['plaza_id']: p for p in corridor.get('toll_plazas', [])}
        for plaza in other.get('toll_plazas', []):
            if plaza['branch_id'] not in branches:
                skipped_plazas.append(plaza)
                continue
            plazas[plaza['plaza_id']] = plaza
        corridor['toll_plazas'] = list(plazas.values())
    dump(seed_path, seed)
    migrate_database(db_path)
    registry = CorridorRegistry(db_path)
    with sqlite3.connect(registry.runtime_db_path) as conn:
        sessions_before = conn.execute('SELECT * FROM trip_sessions ORDER BY session_id').fetchall()
    poi_metadata = {p['poi_id']: p for c in incoming['corridors'] for p in c['pois']}
    allowed = {f.name for f in fields(RawScrapedCandidate)}
    intake_path = ROOT / 'data/ingestion_candidates.json'
    existing = json.loads(intake_path.read_text(encoding='utf-8')) if intake_path.exists() else []
    union = {json.dumps(record, ensure_ascii=False, sort_keys=True): record for record in existing}
    outcomes = []
    today = datetime.now(timezone.utc).date()
    too_few_recent, wrong_low, wrong_average, channel_mismatches = [], [], [], []
    for record in raw:
        metadata = poi_metadata.get(record.get('poi_id'), {})
        values = {key: value for key, value in metadata.items() if key in allowed}
        reviews = record['recent_90d_reviews'] + record['low_review_samples']
        evidence = [dict(text=r['scrubbed_body_vi'], rating=r['star_rating'], published_at=r['review_date'],
                         source_url=r['source_url'], source_channel=r['source_channel']) for r in reviews]
        values.update(candidate_id=record['candidate_id'], poi_id=record.get('poi_id'), corridor_id=record['corridor_id'],
                      venue_name=record['venue_name'], expected_commune=record['expected_commune'],
                      goong_returned_commune=record['goong_returned_commune'],
                      lifetime_rating=record['lifetime_rating'], trailing_90d_rating=record['trailing_90d_rating'],
                      sampled_low_reviews=[r['scrubbed_body_vi'] for r in record['low_review_samples']],
                      raw_testimonial_text=record['scrubbed_testimonial'], source_url=record['source_url'],
                      google_place_id=record['place_id'], goong_place_id='',
                      total_review_count=record.get('user_rating_count', 0), review_evidence=evidence)
        # A Google ChIJ ID is not evidence of a Goong commune-concordant resolution.
        candidate = RawScrapedCandidate(**values)
        normalized = scrub_values(asdict(candidate))
        union[json.dumps(normalized, ensure_ascii=False, sort_keys=True)] = normalized
        verdict = promote_verified_candidate_to_db(registry.runtime_db_path, candidate)
        outcomes.append(dict(candidate_id=record['candidate_id'], poi_id=candidate.poi_id,
                             package_claimed_accepted=bool(record['accepted']), promoted=verdict.accepted,
                             reason=verdict.rejection_reason))
        recent = [r for r in record['recent_90d_reviews']
                  if today - timedelta(days=90) <= datetime.fromisoformat(r['review_date']).date() <= today]
        if len(recent) < 10:
            too_few_recent.append(record['candidate_id'])
        if any(r['star_rating'] > 2 for r in record['low_review_samples']):
            wrong_low.append(record['candidate_id'])
        if round(sum(r['star_rating'] for r in record['recent_90d_reviews']) / len(record['recent_90d_reviews']), 1) != record['trailing_90d_rating']:
            wrong_average.append(record['candidate_id'])
        if any(r['source_channel'] == 'google_maps' and 'youtube' in (urlsplit(r['source_url']).hostname or '') for r in reviews):
            channel_mismatches.append(record['candidate_id'])
    dump(intake_path, list(union.values()))
    # The catalog union preserves conflicting source versions rather than silently
    # overwriting the active seed or equating Google IDs with Goong identities.
    existing_pois = {p['poi_id']: dict(p, corridor_id=c['corridor_id']) for c in seed['corridors'] for p in c['pois']}
    package_pois = {p['poi_id']: dict(p, corridor_id=c['corridor_id']) for c in incoming['corridors'] for p in c['pois']}
    admissions = {o['poi_id']: o for o in outcomes if o['package_claimed_accepted']}
    catalog_pois = [dict(poi_id=poi_id, existing_seed=existing_pois.get(poi_id), package_seed=package_pois.get(poi_id),
                         active=poi_id in existing_pois or admissions.get(poi_id, {}).get('promoted', False),
                         package_admission=admissions.get(poi_id))
                    for poi_id in sorted(existing_pois.keys() | package_pois.keys())]
    old_pairs = {(c['corridor_id'], p['from_poi_id'], p['to_poi_id']): p for c in seed['corridors'] for p in c.get('co_occurrence', [])}
    new_pairs = {(c['corridor_id'], p['from_poi_id'], p['to_poi_id']): p for c in incoming['corridors'] for p in c.get('co_occurrence', [])}
    catalog_pairs = [dict(corridor_id=key[0], from_poi_id=key[1], to_poi_id=key[2],
                          existing_seed=old_pairs.get(key), package_seed=new_pairs.get(key))
                     for key in sorted(old_pairs.keys() | new_pairs.keys())]
    dump(ROOT / 'data/catalog_union.json', dict(status='STAGED_NOT_CERTIFIED', pois=catalog_pois, co_occurrence=catalog_pairs))
    with sqlite3.connect(registry.runtime_db_path) as conn:
        sessions_after = conn.execute('SELECT * FROM trip_sessions ORDER BY session_id').fetchall()
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('pois', 'poi_verifications', 'poi_review_evidence', 'trip_sessions', 'toll_plazas')}
    if sessions_before != sessions_after:
        raise RuntimeError('Import changed trip sessions')
    report = dict(archive=str(archive), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  checked_at=datetime.now(timezone.utc).isoformat(),
                  imported_observations=len(raw), union_observations=len(union),
                  catalog_union_pois=len(catalog_pois), catalog_union_directed_pairs=len(catalog_pairs),
                  active_database_counts=counts,
                  promoted=sum(o['promoted'] for o in outcomes), session_rows_preserved=len(sessions_before),
                  reason_counts=dict(Counter(o['reason'].split(':')[0] for o in outcomes)), outcomes=outcomes,
                  package_evidence_issues=dict(fewer_than_10_actual_recent_reviews=too_few_recent,
                                              low_samples_contain_3_plus_stars=wrong_low,
                                              claimed_trailing_rating_differs_from_supplied_sample=wrong_average,
                                              google_maps_reviews_link_to_youtube=channel_mismatches),
                  missing_branch_geometry=[dict(corridor_id=c['corridor_id'], branch_id=b['branch_id'])
                                           for c in seed['corridors'] for b in c['branches']
                                           if b['branch_id'] != 'MAIN_SPINE' and not b.get('branch_waypoints')],
                  unbound_toll_plazas=skipped_plazas,
                  ticket_09_status='BLOCKED_ON_VERIFIED_EXTERNAL_EVIDENCE',
                  note='Lossless scrubbed intake union, not automatic certification. Packaged code/DB was not installed; configured road controls/tariffs remain unverified estimates.')
    dump(ROOT / 'data/data_package_merge_report.json', report)
    print(json.dumps({k: report[k] for k in ('imported_observations', 'union_observations', 'catalog_union_pois', 'catalog_union_directed_pairs', 'promoted', 'session_rows_preserved', 'active_database_counts', 'reason_counts', 'ticket_09_status')}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--db-path', type=Path, default=DB_PATH)
    args = parser.parse_args()
    merge(args.archive, args.db_path)
