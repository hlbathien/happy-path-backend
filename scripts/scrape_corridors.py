#!/usr/bin/env python3
"""Collect real evidence, apply F9 admission gates, and optionally promote atomically."""
import argparse
import asyncio
import json
from pathlib import Path

from happy_path.config import DB_PATH, GOONG_BASE_URL, load_goong_key
from happy_path.core.collection import append_receipt, collect_candidates, save_candidates, harvest_verified_edges
from happy_path.core.graph import CorridorRegistry
from happy_path.core.ingestion import RawScrapedCandidate, evaluate_scraped_candidate, promote_verified_candidate_to_db

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Happy Path Zero-PII & Anti-Shill Ingestion CLI")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="JSON candidates with dated review and directed-pair source evidence")
    source.add_argument("--targets", type=Path, help="Acquisition targets, independent of already registered POIs")
    parser.add_argument("--promote-to-db", action="store_true", help="Promote evidence-backed candidates directly into SQLite")
    parser.add_argument("--audit-log", type=Path, default=ROOT / "data/ingestion_audit.jsonl")
    args = parser.parse_args()
    runtime_db = CorridorRegistry(DB_PATH).runtime_db_path
    key = load_goong_key()
    if args.input:
        records = json.loads(args.input.read_text(encoding="utf-8"))
        if isinstance(records, dict):
            records = [records]
        candidates = [RawScrapedCandidate(**record) for record in records]
    else:
        targets = json.loads(args.targets.read_text(encoding="utf-8")) if args.targets else None
        candidates = asyncio.run(collect_candidates(runtime_db, key, GOONG_BASE_URL, ROOT / "data/ingestion_sources.jsonl", targets=targets))
        save_candidates(ROOT / "data/ingestion_candidates.json", candidates)
    accepted = 0
    promoted_ids = []
    for candidate in candidates:
        verdict = (promote_verified_candidate_to_db(runtime_db, candidate) if args.promote_to_db
                   else evaluate_scraped_candidate(candidate))
        accepted += int(verdict.accepted)
        if args.promote_to_db and verdict.accepted:
            promoted_ids.append(candidate.poi_id)
        record = dict(corridor_id=candidate.corridor_id, poi_id=candidate.poi_id, venue_name=candidate.venue_name,
                      source_url=candidate.source_url, accepted=verdict.accepted,
                      promoted=bool(args.promote_to_db and verdict.accepted),
                      rejection_reason="" if verdict.accepted else verdict.rejection_reason,
                      scrubbed_text=verdict.scrubbed_text)
        record = append_receipt(args.audit_log, record)
        print(json.dumps({key: value for key, value in record.items() if key != "scrubbed_text"}, ensure_ascii=False))
    if promoted_ids:
        matrix = asyncio.run(harvest_verified_edges(runtime_db, key, GOONG_BASE_URL, ROOT / "data/ingestion_matrix_sources.jsonl", promoted_ids))
        print(json.dumps({"road_matrix": matrix}))
    print(json.dumps(dict(candidates=len(candidates), accepted=accepted,
                          promoted=accepted if args.promote_to_db else 0, audit_log=str(args.audit_log))))


if __name__ == "__main__":
    main()
