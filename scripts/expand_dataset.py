#!/usr/bin/env python3
"""Expand a provenance-backed research catalog without bypassing POI admission gates.

Run with .venv/Scripts/python.exe scripts/expand_dataset.py --discover --reviews --social.
The SQLite catalog checkpoints each result and resumes without re-fetching completed work.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from happy_path.config import GOONG_BASE_URL, load_goong_key
from happy_path.core.collection import _facebook, _youtube, append_receipt, goong_get
from happy_path.core.graph import haversine_km, normalize_vn
from happy_path.core.ingestion import normalize_text_fields, scrub_pii
from happy_path.core.review_pages import extract_review_page

ROOT = Path(__file__).resolve().parents[1]
# Centers define discovery areas, not driving routes or verified branch memberships.
REGIONS = {
    "hcm_dalat": [("Dầu Giây", 10.95, 107.14), ("Định Quán", 11.20, 107.35),
                  ("Bảo Lộc", 11.55, 107.81), ("Di Linh", 11.58, 108.08),
                  ("Đức Trọng", 11.73, 108.37), ("Đà Lạt", 11.94, 108.44)],
    "hcm_nhatrang": [("Hàm Tân", 10.72, 107.69), ("Phan Thiết", 10.93, 108.10),
                     ("Bắc Bình", 11.22, 108.52), ("Tuy Phong", 11.25, 108.72),
                     ("Cà Ná", 11.34, 108.88), ("Phan Rang", 11.57, 108.99),
                     ("Vĩnh Hy", 11.72, 109.20), ("Cam Ranh", 11.91, 109.14),
                     ("Nha Trang", 12.24, 109.19)],
    "hcm_vungtau": [("Long Thành", 10.79, 106.95), ("Phú Mỹ", 10.60, 107.05),
                    ("Bà Rịa", 10.50, 107.17), ("Long Sơn", 10.45, 107.08),
                    ("Suối Nghệ", 10.59, 107.23), ("Vũng Tàu", 10.35, 107.08)],
}
CATEGORIES = ("trạm dừng chân", "trạm sạc", "nhà hàng", "khu du lịch")
PROVIDER_REGIONS = {"hcm_dalat": [("lam-dong", "lam-dong")],
                    "hcm_nhatrang": [("ninh-thuan", "ninh-thuan"), ("binh-thuan", "binh-thuan"), ("khanh-hoa", "khanh-hoa")],
                    "hcm_vungtau": [("vung-tau", "ba-ria-vung-tau")]}
ALL_REGIONS = dict(REGIONS)
ALL_PROVIDER_REGIONS = dict(PROVIDER_REGIONS)


def clean(value):
    if isinstance(value, str):
        return scrub_pii(value)
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def stable(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Catalog:
    def __init__(self, output):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(output / "catalog.sqlite3")
        self.db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS places(place_id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL, lng REAL, address TEXT,
          commune TEXT, district TEXT, province TEXT, types_json TEXT, source_url TEXT, collected_at TEXT);
        CREATE TABLE IF NOT EXISTS coverage(place_id TEXT REFERENCES places(place_id),corridor_id TEXT,region TEXT,
          query_category TEXT,distance_to_discovery_center_km REAL,PRIMARY KEY(place_id,corridor_id,region,query_category));
        CREATE TABLE IF NOT EXISTS attempts(task TEXT PRIMARY KEY,success INTEGER,reason TEXT,http_status INTEGER,collected_at TEXT);
        CREATE TABLE IF NOT EXISTS review_venues(source_url TEXT PRIMARY KEY,corridor_id TEXT,provider TEXT,name TEXT,
          address TEXT,aggregate_rating REAL,rating_scale REAL,total_review_count INTEGER,collected_at TEXT);
        CREATE TABLE IF NOT EXISTS reviews(review_key TEXT PRIMARY KEY,source_url TEXT REFERENCES review_venues(source_url),
          published_at TEXT,rating REAL,rating_scale REAL,text TEXT);
        CREATE TABLE IF NOT EXISTS social_documents(source_url TEXT PRIMARY KEY,corridor_id TEXT,venue_name TEXT,
          title TEXT,text TEXT,comments_json TEXT,collected_at TEXT);
        CREATE TABLE IF NOT EXISTS pages(source_url TEXT PRIMARY KEY,corridor_id TEXT,http_status INTEGER,success INTEGER,
          reason TEXT,text TEXT,collected_at TEXT);
        CREATE TABLE IF NOT EXISTS frontier(source_url TEXT PRIMARY KEY,corridor_id TEXT);
        CREATE TABLE IF NOT EXISTS place_assessments(place_id TEXT PRIMARY KEY REFERENCES places(place_id),status TEXT,reason TEXT);
        CREATE TABLE IF NOT EXISTS review_scope_assessments(source_url TEXT PRIMARY KEY REFERENCES review_venues(source_url),status TEXT,reason TEXT);
        CREATE TABLE IF NOT EXISTS review_locations(source_url TEXT PRIMARY KEY REFERENCES review_venues(source_url),lat REAL,lng REAL,provenance TEXT);
        ''')

    def done(self, task):
        return self.db.execute("SELECT 1 FROM attempts WHERE task=? AND success=1", (task,)).fetchone() is not None

    def attempt(self, task, success, reason="", status=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?)", (task, int(success), reason, status, now()))

    def add_review_page(self, page, corridor):
        if not page["venue_name"] or page["rating_scale"] is None or (page["aggregate_rating"] is None and not page["reviews"]):
            return
        page = clean(page)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO review_venues VALUES (?,?,?,?,?,?,?,?,?)", (
                page["source_url"], corridor, page["provider"], page["venue_name"], page["address"],
                page["aggregate_rating"], page["rating_scale"], page["total_review_count"], now()))
            if page.get("lat") is not None and page.get("lng") is not None:
                self.db.execute("INSERT OR REPLACE INTO review_locations VALUES (?,?,?,?)",
                                (page["source_url"], page["lat"], page["lng"], "Source page place:location metadata"))
            for review in page["reviews"]:
                key = stable(json.dumps([page["source_url"], review["published_at"], review["rating"], review["text"].casefold()], ensure_ascii=False))
                if review["published_at"] is not None:
                    self.db.execute("DELETE FROM reviews WHERE source_url=? AND text=? AND rating IS ? AND published_at IS NULL",
                                    (page["source_url"], review["text"], review["rating"]))
                self.db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?)", (
                    key, page["source_url"], review["published_at"], review["rating"], review["rating_scale"], review["text"]))

    def export(self):
        self.assess_places()
        self.db.row_factory = sqlite3.Row
        for table in ("places", "coverage", "place_assessments", "review_venues", "review_locations", "review_scope_assessments", "reviews", "social_documents"):
            rows = [dict(row) for row in self.db.execute(f"SELECT * FROM {table}")]
            (self.output / f"{table}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        self.db.row_factory = None
        features = []
        for place_id, name, lat, lng in self.db.execute("SELECT p.place_id,p.name,p.lat,p.lng FROM places p JOIN place_assessments a USING(place_id) WHERE a.status='venue_type_supported'"):
            corridors = [row[0] for row in self.db.execute("SELECT DISTINCT corridor_id FROM coverage WHERE place_id=?", (place_id,))]
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
                             "properties": {"place_id": place_id, "name": name, "corridors": corridors,
                                            "status": "provider_type_supported", "admitted": False}})
        (self.output / "supported_venues.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
        summary = {
            "unique_goong_places": self.db.execute("SELECT COUNT(*) FROM places").fetchone()[0],
            "place_assessments": dict(self.db.execute("SELECT status,COUNT(*) FROM place_assessments GROUP BY status")),
            "places_by_corridor": dict(self.db.execute("SELECT corridor_id,COUNT(DISTINCT place_id) FROM coverage GROUP BY corridor_id")),
            "discovery_regions_by_corridor": dict(self.db.execute("SELECT corridor_id,COUNT(DISTINCT region) FROM coverage GROUP BY corridor_id")),
            "review_venues_by_corridor": dict(self.db.execute("SELECT corridor_id,COUNT(*) FROM review_venues GROUP BY corridor_id")),
            "review_source_scope": dict(self.db.execute("SELECT status,COUNT(*) FROM review_scope_assessments GROUP BY status")),
            "review_sources_with_coordinates": self.db.execute("SELECT COUNT(*) FROM review_locations").fetchone()[0],
            "review_rows": self.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
            "dated_text_reviews": self.db.execute("SELECT COUNT(*) FROM reviews WHERE published_at IS NOT NULL AND text!=''").fetchone()[0],
            "dated_rated_text_reviews": self.db.execute("SELECT COUNT(*) FROM reviews WHERE published_at IS NOT NULL AND rating IS NOT NULL AND text!=''").fetchone()[0],
            "dated_rated_text_reviews_by_corridor": dict(self.db.execute("SELECT v.corridor_id,COUNT(*) FROM reviews r JOIN review_venues v USING(source_url) WHERE r.published_at IS NOT NULL AND r.rating IS NOT NULL AND r.text!='' GROUP BY v.corridor_id")),
            "native_rating_scales": dict(self.db.execute("SELECT rating_scale,COUNT(*) FROM reviews GROUP BY rating_scale")),
            "recent_90d_rated_text_reviews": self.db.execute("SELECT COUNT(*) FROM reviews WHERE published_at>=date('now','-90 days') AND published_at<=date('now') AND rating IS NOT NULL AND text!=''").fetchone()[0],
            "successful_pages": self.db.execute("SELECT COUNT(*) FROM pages WHERE success=1").fetchone()[0],
            "youtube_videos": self.db.execute("SELECT COUNT(*) FROM social_documents").fetchone()[0],
            "youtube_comments": sum(len(json.loads(row[0])) for row in self.db.execute("SELECT comments_json FROM social_documents")),
            "production_pois_promoted": 0,
        }
        (self.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary

    def assess_places(self):
        supported = {"restaurant", "cafe", "food", "bakery", "gas_station", "electric_vehicle_charging_station",
                     "charging_station", "tourist_attraction", "park", "museum", "hindu_temple", "church",
                     "place_of_worship", "lodging", "hotel", "zoo", "amusement_park", "shopping_mall"}
        records = []
        for place_id, name, raw_types in self.db.execute("SELECT place_id,name,types_json FROM places"):
            types = set(json.loads(raw_types))
            geography = all(t == "political" or t == "geocode" or t == "route" or t == "street_address"
                            or t in {"country", "locality", "sublocality"} or t.startswith(("administrative_area_", "sublocality_")) for t in types)
            if types and geography:
                status, reason = "excluded_geographic_entity", "Provider classifies the result as geography rather than a venue"
            elif types & {"travel_agency", "car_rental", "taxi_stand", "bus_station", "bus_stop"}:
                status, reason = "excluded_transport_service", "Travel/transport business is not a visitor stop"
            elif types & supported:
                status, reason = "venue_type_supported", "Provider venue type supported; hours, safety and reviews still unverified"
            else:
                status, reason = "needs_type_verification", "Provider supplied only generic or unsupported types"
            records.append((place_id, status, reason))
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO place_assessments VALUES (?,?,?)", records)
            for url, name, address, corridor in self.db.execute("SELECT source_url,name,address,corridor_id FROM review_venues").fetchall():
                label = normalize_vn(" ".join([url, name, address or ""]))
                if "con dao" in label or "con-dao" in label:
                    status, reason = "outside_drivable_corridor", "Côn Đảo requires an island crossing and is outside these road corridors"
                else:
                    status, reason = "province_group_only", "Source province groups discovery; exact corridor/branch location is not yet independently bound"
                    location = self.db.execute("SELECT lat,lng FROM review_locations WHERE source_url=?", (url,)).fetchone()
                    if location:
                        distance = min(haversine_km(location[0], location[1], lat, lng) for region, lat, lng in ALL_REGIONS[corridor])
                        status = "coordinate_in_discovery_region" if distance <= 20 else "outside_discovery_regions"
                        reason = f"Source coordinates are {distance:.3f} km from the nearest {corridor} discovery center; branch safety is unverified"
                self.db.execute("INSERT OR REPLACE INTO review_scope_assessments VALUES (?,?,?)", (url, status, reason))

    def merge_from(self, path):
        """Merge checkpointed acquisition shards, keeping publication-date corrections."""
        if Path(path).resolve() == (self.output / "catalog.sqlite3").resolve():
            return
        source = sqlite3.connect(path)
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with self.db:
                for table in ("places", "coverage", "review_venues", "review_locations", "pages", "frontier", "attempts"):
                    if table not in tables:
                        continue
                    for row in source.execute(f"SELECT * FROM {table}"):
                        placeholders = ",".join("?" for _ in row)
                        self.db.execute(f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})", row)
                if "reviews" in tables:
                    for row in source.execute("SELECT * FROM reviews"):
                        key, url, published, rating, scale, text = row
                        if published:
                            self.db.execute("DELETE FROM reviews WHERE source_url=? AND text=? AND rating IS ? AND published_at IS NULL",
                                            (url, text, rating))
                        self.db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?)", row)
                if "social_documents" in tables:
                    for row in source.execute("SELECT * FROM social_documents"):
                        self.add_social_document(row)
        finally:
            source.close()

    def add_social_document(self, row):
        row = list(row)
        existing = self.db.execute("SELECT comments_json FROM social_documents WHERE source_url=?", (row[0],)).fetchone()
        comments = json.loads(row[5])
        if existing:
            comments = json.loads(existing[0]) + comments
        comments = list({(comment.get("text"), comment.get("timestamp")): comment for comment in comments}.values())
        row[5] = json.dumps(comments, ensure_ascii=False)
        self.db.execute("INSERT OR REPLACE INTO social_documents VALUES (?,?,?,?,?,?,?)", row)


def now():
    return datetime.now(timezone.utc).isoformat()


async def discover(catalog):
    key = load_goong_key()
    if not key:
        raise ValueError("Goong key required for live place discovery")
    async with httpx.AsyncClient(timeout=25) as client:
        for corridor, regions in REGIONS.items():
            for region, lat, lng in regions:
                for category in CATEGORIES:
                    query = f"{category} {region}"
                    task = f"goong:{corridor}:{query}"
                    if catalog.done(task):
                        continue
                    response = await goong_get(client, GOONG_BASE_URL + "/Place/AutoComplete", {
                        "api_key": key, "input": query, "location": f"{lat},{lng}", "radius": "20", "limit": "10"})
                    if response.status_code in (401, 403):
                        raise RuntimeError(f"GOONG_AUTH_HTTP_{response.status_code}")
                    if response.status_code != 200:
                        catalog.attempt(task, False, "GOONG_HTTP_ERROR", response.status_code)
                        continue
                    payload = normalize_text_fields(response.json())
                    predictions = payload.get("predictions") or []
                    added, excluded = 0, 0
                    for prediction in predictions:
                        place_id = prediction.get("place_id")
                        if not place_id:
                            continue
                        existing = catalog.db.execute("SELECT lat,lng,types_json FROM places WHERE place_id=?", (place_id,)).fetchone()
                        if existing:
                            point_lat, point_lng, types = existing[0], existing[1], json.loads(existing[2])
                        else:
                            detail_task = "detail:" + place_id
                            if catalog.done(detail_task):
                                continue  # Already excluded/non-venue detail.
                            detail = await goong_get(client, GOONG_BASE_URL + "/Place/Detail", {"api_key": key, "place_id": place_id})
                            if detail.status_code != 200:
                                catalog.attempt(detail_task, False, "DETAIL_HTTP_ERROR", detail.status_code)
                                continue
                            result = normalize_text_fields(detail.json().get("result") or {})
                            location = (result.get("geometry") or {}).get("location") or {}
                            point_lat, point_lng = location.get("lat"), location.get("lng")
                            types = result.get("types") or []
                            if not isinstance(point_lat, (int, float)) or not isinstance(point_lng, (int, float)) or not (8 <= point_lat <= 23.5 and 102 <= point_lng <= 110):
                                catalog.attempt(detail_task, True, "EXCLUDED_INVALID_COORDINATES", detail.status_code)
                                excluded += 1
                                continue
                            if types and set(types) <= {"geocode", "political", "administrative_area_level_1", "administrative_area_level_2", "locality", "route", "street_address"}:
                                catalog.attempt(detail_task, True, "EXCLUDED_GEOGRAPHIC_ENTITY", detail.status_code)
                                excluded += 1
                                continue
                            if haversine_km(lat, lng, point_lat, point_lng) > 20:
                                # Leave unmarked so a later appropriate region can collect this place.
                                excluded += 1
                                continue
                            compound = result.get("compound") or prediction.get("compound") or {}
                            record = [place_id, *clean([result.get("name") or prediction.get("structured_formatting", {}).get("main_text") or "",
                                            point_lat, point_lng, result.get("formatted_address") or prediction.get("description") or "",
                                            compound.get("commune"), compound.get("district"), compound.get("province"),
                                            json.dumps(types, ensure_ascii=False), GOONG_BASE_URL + "/Place/Detail", now()])]
                            with catalog.db:
                                catalog.db.execute("INSERT OR IGNORE INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?)", record)
                            catalog.attempt(detail_task, True)
                        distance = haversine_km(lat, lng, point_lat, point_lng)
                        if distance <= 20:
                            with catalog.db:
                                catalog.db.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?,?,?)", (place_id, corridor, region, category, round(distance, 3)))
                            added += 1
                    catalog.attempt(task, payload.get("status") == "OK", "" if payload.get("status") == "OK" else "GOONG_API_STATUS", response.status_code)
                    append_receipt(catalog.output / "discovery_receipts.jsonl", dict(engine="goong-discovery", corridor_id=corridor, region=region,
                                   query=query, predictions=len(predictions), nearby_venue_matches=added, excluded=excluded, http_status=response.status_code))
                    print(json.dumps({"phase": "discovery", "corridor": corridor, "region": region, "category": category,
                                      "nearby_matches": added, "unique_places": catalog.db.execute("SELECT COUNT(*) FROM places").fetchone()[0]}, ensure_ascii=True), flush=True)
                    catalog.export()


def canonical_provider_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = unquote(parts.path).rstrip("/")
    if host == "foody.vn":
        # Strip ASP.NET cookieless-session URL segments; keep only venue paths.
        if path.startswith("/(A(") and "))/" in path:
            path = "/" + path.split("))/", 1)[1]
        segments = path.strip("/").split("/")
        if len(segments) < 2 or segments[0] not in {pair[0] for pairs in ALL_PROVIDER_REGIONS.values() for pair in pairs}:
            return None
        if segments[1] in {"o-dau", "binh-luan", "hinh-anh", "bo-suu-tap", "food", "travel", "dia-diem", "top-thanh-vien"} or segments[1].startswith(("khu-vuc-", "bo-suu-tap")):
            return None
        return "https://www.foody.vn/" + "/".join(segments[:2]) + "/binh-luan"
    if host == "gody.vn":
        segments = path.strip("/").split("/")
        if len(segments) == 4 and segments[:2] == ["chau-a", "viet-nam"]:
            return "https://gody.vn/" + "/".join(segments)
    return None


async def crawl_reviews(catalog, page_budget, source_file=None):
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    seen, queue = set(), []
    for corridor, regions in PROVIDER_REGIONS.items():
        for foody, gody in regions:
            queue.extend([(corridor, f"https://www.foody.vn/{foody}/o-dau"),
                          (corridor, f"https://gody.vn/chau-a/viet-nam/{gody}")])
    # Known public source identities from the first run, plus region-directory discovery.
    known = [("hcm_dalat", "https://gody.vn/chau-a/viet-nam/lam-dong/thac-dambri"),
             ("hcm_nhatrang", "https://www.foody.vn/ninh-thuan/hang-rai/binh-luan")]
    queue.extend(item for item in known if item[0] in PROVIDER_REGIONS)
    queue.extend((corridor, url) for url, corridor in catalog.db.execute("SELECT source_url,corridor_id FROM frontier") if corridor in PROVIDER_REGIONS)
    # Old checkpoints retain scrubbed Markdown; recover their explicit provider links on resume.
    for corridor, text in catalog.db.execute("SELECT corridor_id,text FROM pages WHERE success=1"):
        for match in re.finditer(r'https?://(?:www\.)?(?:foody\.vn|gody\.vn)/[^\s)<>"\]]+', text):
            candidate = canonical_provider_url(match[0])
            if candidate:
                path = urlsplit(candidate).path
                if corridor in PROVIDER_REGIONS and any(f"/{foody}/" in path or f"/{gody}/" in path for foody, gody in PROVIDER_REGIONS[corridor]):
                    queue.append((corridor, candidate))
    queue = list(dict.fromkeys(queue))
    if source_file:
        queue = [(row["corridor_id"], row["source_url"]) for row in json.loads(source_file.read_text(encoding="utf-8")) if row["corridor_id"] in PROVIDER_REGIONS]
    completed = 0
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        while queue and completed < page_budget:
            corridor, url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            task = "web:" + url
            if catalog.done(task):
                continue
            result = await crawler.arun(url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=30000,
                                        delay_before_return_html=1.5, verbose=False))
            text = scrub_pii(result.markdown.raw_markdown if result.markdown else "")
            challenge = any(marker in text.casefold() for marker in ("captcha", "unusual traffic", "please complete the following challenge", "verify you are human"))
            success = bool(result.success and result.status_code and 200 <= result.status_code < 400 and not challenge)
            reason = "WEB_CHALLENGE_REQUIRED" if challenge else "" if success else "WEB_FETCH_FAILED"
            with catalog.db:
                catalog.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)", (url, corridor, result.status_code, int(success), reason, reason, now()))
            catalog.attempt(task, success, reason, result.status_code)
            append_receipt(catalog.output / "web_receipts.jsonl", dict(engine="crawl4ai", source_url=url, corridor_id=corridor,
                           success=success, reason=reason, http_status=result.status_code))
            completed += 1
            if success:
                page = extract_review_page(result.html or "", result.url or url)
                catalog.add_review_page(page, corridor)
                soup = BeautifulSoup(result.html or "", "html.parser")
                links = []
                for anchor in soup.select("a[href]"):
                    candidate = canonical_provider_url(urljoin(result.url or url, anchor["href"]))
                    if candidate and candidate not in seen:
                        # Provider province must agree with the directory's corridor.
                        path = urlsplit(candidate).path
                        allowed = any(f"/{foody}/" in path or f"/{gody}/" in path for foody, gody in PROVIDER_REGIONS[corridor])
                        if allowed:
                            links.append((corridor, candidate))
                with catalog.db:
                    catalog.db.executemany("INSERT OR IGNORE INTO frontier VALUES (?,?)", ((link, group) for group, link in links))
                    # Retain venue/review evidence and discovery links, excluding reviewer profiles and page navigation.
                    retained = "\n".join([page["venue_name"], page.get("address") or "",
                                         *(review["text"] for review in page["reviews"] if review["text"]),
                                         *(link for group, link in dict.fromkeys(links))])
                    catalog.db.execute("UPDATE pages SET text=? WHERE source_url=?", (retained, url))
                # Interleave corridor queues so a single large directory cannot consume the run.
                if not source_file:
                    queue.extend(dict.fromkeys(links))
                print(json.dumps({"phase": "reviews", "corridor": corridor, "url": url, "review_rows": len(page["reviews"]),
                                  "venue": page["venue_name"], "queued": len(queue)}, ensure_ascii=True), flush=True)
            else:
                print(json.dumps({"phase": "web_failure", "url": url, "status": result.status_code, "reason": reason}), flush=True)
            catalog.export()
            if queue:
                # Round-robin source pages across corridors instead of directory-size bias.
                groups = list(REGIONS)
                offset = groups.index(corridor) + 1
                rotation = groups[offset:] + groups[:offset]
                next_corridor = next((c for c in rotation if any(q[0] == c for q in queue)), None)
                if next_corridor:
                    index = next(i for i, q in enumerate(queue) if q[0] == next_corridor)
                    queue.insert(0, queue.pop(index))


async def social(catalog):
    # Publicly named attractions, resolved as separate social evidence; no synthetic star scores.
    names = {"hcm_dalat": ["Thiền Viện Trúc Lâm Đà Lạt", "Thác Pongour"],
             "hcm_nhatrang": ["Tháp Po Klong Garai", "Vịnh Vĩnh Hy"],
             "hcm_vungtau": ["Hải Đăng Vũng Tàu", "Nhà Lớn Long Sơn"]}
    for corridor, venues in names.items():
        for name in venues:
            task = "youtube:" + name
            if catalog.done(task):
                continue
            documents = await _youtube(name, catalog.output / "social_receipts.jsonl")
            for document in documents:
                with catalog.db:
                    catalog.db.execute("INSERT OR REPLACE INTO social_documents VALUES (?,?,?,?,?,?,?)", (
                        document["source_url"], corridor, document["venue_name"], document["title"], document["text"],
                        json.dumps(document["comments"], ensure_ascii=False), now()))
            catalog.attempt(task, bool(documents), "" if documents else "NO_SOCIAL_EVIDENCE")
            print(json.dumps({"phase": "social", "corridor": corridor, "venue": name, "videos": len(documents)}, ensure_ascii=True), flush=True)
            catalog.export()
    # The connection attempt is recorded; a missing/blocked browser never becomes invented content.
    await _facebook([], catalog.output / "social_receipts.jsonl")


async def main(args):
    global REGIONS, PROVIDER_REGIONS
    if args.corridors:
        REGIONS = {key: value for key, value in REGIONS.items() if key in args.corridors}
        PROVIDER_REGIONS = {key: value for key, value in PROVIDER_REGIONS.items() if key in args.corridors}
    load_dotenv(ROOT / ".env", override=False)
    catalog = Catalog(args.output)
    try:
        for source in args.merge_catalog:
            catalog.merge_from(source)
        if args.discover or args.reviews or args.social:
            plans = args.output / "plans"
            plans.mkdir(exist_ok=True)
            plan = {"collected_at": now(), "regions": REGIONS, "query_categories": CATEGORIES,
                    "discovery_radius_km": 20, "review_page_budget": args.page_budget,
                    "phases": [name for name in ("discover", "reviews", "social") if getattr(args, name)],
                    "source_file": str(args.source_file) if args.source_file else None}
            (plans / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")).write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        phases = []
        if args.discover:
            phases.append(discover(catalog))
        if args.reviews:
            phases.append(crawl_reviews(catalog, args.page_budget, args.source_file))
        if args.social:
            phases.append(social(catalog))
        outcomes = await asyncio.gather(*phases, return_exceptions=True)
        failures = [type(outcome).__name__ + ": " + str(outcome) for outcome in outcomes if isinstance(outcome, Exception)]
        print(json.dumps({"summary": catalog.export(), "phase_failures": failures}, ensure_ascii=True), flush=True)
        if failures:
            raise SystemExit(1)
    finally:
        catalog.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/research_catalog")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--reviews", action="store_true")
    parser.add_argument("--social", action="store_true")
    parser.add_argument("--page-budget", type=int, default=90)
    parser.add_argument("--corridors", nargs="+", choices=list(REGIONS))
    parser.add_argument("--source-file", type=Path, help="Explicit corridor_id/source_url rows; disables recursive expansion")
    parser.add_argument("--merge-catalog", type=Path, action="append", default=[], help="Merge a completed acquisition SQLite shard")
    asyncio.run(main(parser.parse_args()))
