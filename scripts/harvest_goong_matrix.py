#!/usr/bin/env python3
"""Harvest real <=10x10 static Goong road matrices for admitted venues; no invented travel times."""
import asyncio
import json
from pathlib import Path
from happy_path.config import DB_PATH, GOONG_BASE_URL, load_goong_key
from happy_path.core.collection import harvest_verified_edges

if __name__ == "__main__":
    result = asyncio.run(harvest_verified_edges(DB_PATH, load_goong_key(), GOONG_BASE_URL,
                                              Path(__file__).resolve().parent.parent / "data/ingestion_matrix_sources.jsonl"))
    print(json.dumps(result))
