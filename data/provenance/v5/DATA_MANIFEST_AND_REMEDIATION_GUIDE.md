# Happy Path v5.0 Bounded Data Package (`happy-path-data-package`)

## 1. Package Scope (Bounded Data Only — Drop-In for Scaffold v3)
This archive contains **only the data layer, database compiler, scraper scripts, and raw multi-source provenance corpora** for Project Happy Path. It is 100% schema-compatible with your `v3` scaffold (`corridors`, `corridor_branches`, `pois`, `corridor_edges`, `poi_co_occurrence`, `ingestion_audit_log`, `poi_reviews`, `trip_sessions`) while adding the missing physical grounding exposed by `TICKETS_01_08_FORENSIC_ASSESSMENT.md`:

- `data/schema.sql`: Backward-compatible DDL adding `toll_plazas`, `corridor_branches.real_branch_km`, `corridor_branches.branch_waypoints_json`, `pois.google_rating`, `pois.user_rating_count`, `pois.ev_max_kw`, `pois.ev_stall_count`, `pois.ev_connector_type`, `pois.is_24h`, `poi_reviews.source_channel` (`UNIQUE(scrubbed_body_vi)`), and `trip_sessions.version`.
- `data/seed_corridors.json`: **48 real Google-Maps-grounded promoted POIs** (`18` HCMC–Da Lat, `16` HCMC–Nha Trang, `14` HCMC–Vung Tau; `100%` real `ChIJ...` Place IDs; `122,761` aggregate real user ratings; `18` EV charging stops with real `30kW–180kW` power ratings and both `24/7` and non-`24/7` operating windows), **7 branch waypoint chains (`branch_waypoints`)**, **14 physical VETC/ePass toll plazas (`toll_plazas`)**, and **36 multi-source Markov co-occurrence pairs** (`12` per corridor).
- `data/raw_scraped_candidates.json` & `data/ingestion_audit_seed.json`: **84 real evaluated candidates** (`48` promoted + **`36` rejected**: `12` `COMMUNE_MISMATCH` cross-province homonyms, `12` `SENTIMENT_DRIFT_DECAY` degraded bus traps & broken/curfewed EV chargers, `12` `SHILL_TRAP_VETO` unbuilt rest stops & tourist traps) and **1,260 100%-unique, zero-template, PII-scrubbed multi-channel reviews** across `google_maps`, `youtube`, `otosaigon`, `facebook`, and `web_driver_guide`.
- `data/happy_path.db`: Pre-compiled SQLite database with branch-aware multi-vertex edge polylines (never leaking excluded main-spine coordinates onto alternative branches).
- `data/provenance/`:
  - `google_maps_places_raw.json`: Raw extracted Google Maps `PlaceResult` objects and verbatim reviews.
  - `google_maps_directions_raw.json`: Real Google Maps `RouteResult` distances, durations, and waypoint chains across all 3 corridors and 7 branches.
  - `youtube_2026_roadtrip_transcripts.json`: Verified 2025–2026 road-trip vlog transcripts (`Thầy Toàn Dạy Lái xe`, `Đi Cùng Thy`, `TÀI XẾ BỤI`).
  - `otosaigon_and_evcs_corpus.json`: Verified Otosaigon EV/corridor threads and 2026 Oto360/EVCS VinFast charging station specifications.
- `scripts/seed_db.py`: Updated deterministic compiler that honors `HAPPY_PATH_DB_PATH`, supports `--if-missing` (preventing `make run` from wiping active sessions), and compiles branch-aware multi-vertex polylines.

## 2. SHA-256 Checksums
```json
{
  "data/happy_path.db": "34d2069691efe8ab9b795295ccb80ea4303384b1c79322a07d5badbfd5cfce72",
  "data/ingestion_audit_seed.json": "aadcdffaa9343c42e117f11c0008e558e6ccae16c8f16516fcd82444d2dfc6eb",
  "data/provenance/google_maps_directions_raw.json": "4d4ef9b993f9b1c970133c69d206b3fad7f74a84b18e34cd6caacd645c2b042c",
  "data/provenance/google_maps_places_raw.json": "1d4329bc4dbfee6da25943d3307b4e4ae71eae83f991f116b42f945eef8d3563",
  "data/provenance/otosaigon_and_evcs_corpus.json": "d402a36a58e3b7f7ed5383b5845a17c164e949f6ca99f5dda88b76219c12bd9f",
  "data/provenance/youtube_2026_roadtrip_transcripts.json": "457a8bbc182455d0aa99129bb48860c800234f560204a38f4264fe64f22bc1dc",
  "data/raw_scraped_candidates.json": "aadcdffaa9343c42e117f11c0008e558e6ccae16c8f16516fcd82444d2dfc6eb",
  "data/schema.sql": "dcd35099340ab48cb457fb2661604af020ea765e6a58cf67d13280afcf6c7de1",
  "data/seed_corridors.json": "ad05041dad50b895be30bb64fe1dbddf2290063a2a3904418e31c6fe6b108c7c",
  "scripts/harvest_goong_matrix.py": "f3d46170da6a8cedf8f703c0d8444cfec708c7531fe361bdfddd01503e9f6c37",
  "scripts/scrape_corridors.py": "96d4e3749169e83cada5e496bfef796d1fefdfbf1d97e8979020c000d0c0b276",
  "scripts/seed_db.py": "175c6bed388c9817a4ce06d6a666e260c3c6fcfa976448c4b85b618d3088e0d5"
}
```