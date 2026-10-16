#!/usr/bin/env python3
"""CLI entrypoint for NFC-Normalized Zero-PII scraping, anti-shill gate & direct SQLite promotion (Ticket 09)."""
import argparse
import json
from pathlib import Path
from happy_path.config import DB_PATH
from happy_path.core.ingestion import (
    RawScrapedCandidate,
    evaluate_scraped_candidate,
    promote_verified_candidate_to_db,
)

def main() -> None:
    parser = argparse.ArgumentParser(description="Happy Path Zero-PII & Anti-Shill Ingestion CLI")
    parser.add_argument("--input", type=str, help="Path to JSON candidate file")
    parser.add_argument("--promote-to-db", action="store_true", help="Promote verified candidates directly into SQLite")
    args = parser.parse_args()

    if args.input:
        records = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if isinstance(records, dict):
            records = [records]
        for rec in records:
            cand = RawScrapedCandidate(**rec)
            verdict = (
                promote_verified_candidate_to_db(DB_PATH, cand)
                if args.promote_to_db
                else evaluate_scraped_candidate(cand)
            )
            print(json.dumps(verdict.__dict__, ensure_ascii=False))
    else:
        sample = RawScrapedCandidate(
            venue_name="Trạm Dừng Núi Xả Thô",
            expected_commune="Hàm Thuận Bắc",
            goong_returned_commune="Hàm Thuận Bắc",
            lifetime_rating=4.6,
            trailing_90d_rating=4.5,
            sampled_low_reviews=["Rất sạch sẽ", "Đồ ăn hợp lý", "Bãi đỗ xe rộng"],
            raw_testimonial_text="Liên hệ SĐT0909123456 @driver_vn",
        )
        print(evaluate_scraped_candidate(sample))

if __name__ == "__main__":
    main()
