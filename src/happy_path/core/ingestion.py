import hashlib
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from happy_path.core.graph import normalize_vn

PII_PATTERNS = [
    re.compile(
        r"(?:https?://)?(?:www\.|m\.|web\.|mbasic\.)?(?:facebook|fb)\.com/"
        r"(?:profile\.php\?id=\d+|groups/[^/\s]+/user/[^/\s]+|(?!groups/)[A-Za-z0-9._-]+)"
        r"(?:/)?(?:[?#][^\s]*)?",
        re.IGNORECASE,
    ),
    re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}"),
    re.compile(r"@[\w.-]+", re.UNICODE),
    re.compile(r"(?:0|\+84)[35789](?:[\s.\-\u00a0]*\d){8}"),
]

DISILLUSIONMENT_TOKENS = tuple(
    unicodedata.normalize("NFC", token)
    for token in (
        "vỡ mộng", "chỉ được cái view", "đường vào nguy hiểm", "bắt mua vé",
        "đồ uống dở", "chặt chém", "cơm tù", "không giống trên mạng",
    )
)


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def normalize_text_fields(value: Any) -> Any:
    """Normalize external text, including nested JSON, before inspecting it."""
    if isinstance(value, str):
        return nfc(value)
    if isinstance(value, list):
        return [normalize_text_fields(item) for item in value]
    if isinstance(value, dict):
        return {nfc(key): normalize_text_fields(item) for key, item in value.items()}
    return value


@dataclass
class RawScrapedCandidate:
    venue_name: str
    expected_commune: str
    goong_returned_commune: str
    lifetime_rating: Optional[float]
    trailing_90d_rating: Optional[float]
    sampled_low_reviews: List[str]
    raw_testimonial_text: str
    poi_id: Optional[str] = None
    corridor_id: str = "hcm_dalat"
    branch_id: str = "MAIN_SPINE"
    goong_place_id: str = ""
    category: str = "rest_stop"
    lat: Optional[float] = None
    lng: Optional[float] = None
    corridor_km: Optional[float] = None
    source_url: str = ""
    direction_allowed: Optional[str] = None
    sedan_safe: Optional[bool] = None
    has_ev_charge: Optional[bool] = None
    midday_sun_trap: Optional[bool] = None
    is_arrival_anchor: Optional[bool] = None
    exertion_level: Optional[str] = None
    dwell_minutes: Optional[int] = None
    open_minute: Optional[int] = None
    close_minute: Optional[int] = None
    utility_score: float = 70.0
    scenic_score: float = 50.0
    detour_minutes: int = 0
    hazard_level: float = 0.05
    archetype_affinity: str = "ALL"
    total_review_count: int = 0
    total_low_review_count: Optional[int] = None
    review_evidence: List[Dict[str, Any]] = field(default_factory=list)
    co_occurring_poi_ids: List[str] = field(default_factory=list)
    co_occurrence_sources: Dict[str, List[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for item in fields(self):
            setattr(self, item.name, normalize_text_fields(getattr(self, item.name)))


@dataclass
class IngestionVerdict:
    accepted: bool
    scrubbed_text: str
    rejection_reason: str = ""


def scrub_pii(text: str) -> str:
    cleaned = nfc(text)
    for pattern in PII_PATTERNS:
        cleaned = pattern.sub("[REDACTED_PII]", cleaned)
    return cleaned


def evaluate_scraped_candidate(candidate: RawScrapedCandidate) -> IngestionVerdict:
    # Also normalize candidates edited after construction, before any validation.
    candidate.__post_init__()
    scrubbed = scrub_pii(candidate.raw_testimonial_text)

    def reject(reason: str) -> IngestionVerdict:
        return IngestionVerdict(False, scrubbed, reason)

    if len(candidate.expected_commune) < 2 or len(candidate.goong_returned_commune) < 2:
        return reject("INVALID_COMMUNE: both communes must have at least 2 characters")
    if any(not isinstance(rating, (int, float)) or not math.isfinite(rating) or not 1.0 <= rating <= 5.0
           for rating in (candidate.lifetime_rating, candidate.trailing_90d_rating)):
        return reject("INVALID_RATING: ratings must be finite and within [1.0, 5.0]")
    reviews = [review.casefold() for review in candidate.sampled_low_reviews if review]
    if len(reviews) < 3:
        return reject("INSUFFICIENT_LOW_REVIEW_SAMPLE: at least 3 non-empty low reviews required")
    if candidate.expected_commune.casefold() != candidate.goong_returned_commune.casefold():
        return reject("COMMUNE_MISMATCH: Goong commune differs from expected commune")
    # Decimal avoids bypassing the inclusive 0.4 boundary (e.g. 4.6 - 4.2).
    if Decimal(str(candidate.lifetime_rating)) - Decimal(str(candidate.trailing_90d_rating)) >= Decimal("0.4"):
        return reject("SENTIMENT_DRIFT_DECAY: trailing 90d rating dropped >= 0.4 stars")
    hits = sum(any(token in review for token in DISILLUSIONMENT_TOKENS) for review in reviews)
    if hits * 100 >= 15 * len(reviews):
        return reject(f"SHILL_TRAP_VETO: {hits}/{len(reviews)} low reviews match disillusionment tokens")
    return IngestionVerdict(True, scrubbed, "VERIFIED")


VENUE_TYPES = {
    "Place", "LocalBusiness", "TouristAttraction", "Waterfall", "Park", "Hotel", "LodgingBusiness",
    "Restaurant", "FoodEstablishment", "CafeOrCoffeeShop", "GasStation", "ElectricVehicleChargingStation",
    "Store", "ShoppingCenter", "CivicStructure", "LandmarksOrHistoricalBuildings", "BuddhistTemple",
}


def entity_nodes(jsonld_objects: List[Dict[str, Any]], target_venue_name: str):
    """Yield only named venue entities, never unrelated rating/review widgets."""
    objects = normalize_text_fields(jsonld_objects)
    target = set(normalize_vn(nfc(target_venue_name)).split())
    if not target:
        return
    pending = list(objects)
    while pending:
        obj = pending.pop(0)
        if not isinstance(obj, dict):
            continue
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            child = obj.get(key)
            if isinstance(child, list):
                pending.extend(child)
            elif isinstance(child, dict):
                pending.append(child)
        types = obj.get("@type", [])
        if isinstance(types, str):
            types = [types]
        types = {item.rsplit("/", 1)[-1].rsplit(":", 1)[-1] for item in types if isinstance(item, str)}
        name = set(normalize_vn(obj.get("name", "")).split())
        if types & VENUE_TYPES and target <= name:
            yield obj


def extract_entity_rating_from_jsonld(
    jsonld_objects: List[Dict[str, Any]], target_venue_name: str
) -> Optional[Tuple[float, List[str]]]:
    for obj in entity_nodes(jsonld_objects, target_venue_name):
        aggregate = obj.get("aggregateRating")
        if not isinstance(aggregate, dict):
            continue
        try:
            rating = float(aggregate.get("ratingValue"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(rating) or not 1.0 <= rating <= 5.0:
            continue
        reviews = obj.get("review") or []
        if isinstance(reviews, dict):
            reviews = [reviews]
        bodies = [scrub_pii(review.get("reviewBody") or review.get("description") or "")
                  for review in reviews if isinstance(review, dict)]
        return rating, [body for body in bodies if body]
    return None


def _commune_key(text: str) -> str:
    return re.sub(r"^(?:xa|phuong|thi tran)\s+", "", normalize_vn(nfc(text)))


def select_commune_matching_prediction(
    predictions: List[Dict[str, Any]], expected_commune: str
) -> Optional[Dict[str, Any]]:
    predictions = normalize_text_fields(predictions)
    expected = _commune_key(expected_commune)
    for prediction in predictions:
        commune = (prediction.get("compound") or {}).get("commune") or ""
        # A supplied commune is authoritative. A street name cannot override it.
        segments = [commune] if commune else re.split(r"[,;\n]", prediction.get("description") or "")
        if expected and any(expected == _commune_key(segment) for segment in segments):
            return prediction
    return predictions[0] if predictions else None


def _public_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and scrub_pii(value) == value


def _promotion_evidence_error(candidate: RawScrapedCandidate) -> str:
    today = datetime.now(timezone.utc).date()
    recent, low, seen = [], set(), set()
    for review in candidate.review_evidence:
        try:
            published = date.fromisoformat(review["published_at"][:10])
            rating = float(review["rating"])
            text = scrub_pii(review["text"])
            source = review["source_url"]
        except (KeyError, TypeError, ValueError):
            return "INVALID_REVIEW_EVIDENCE: each review needs text, date, rating and source URL"
        if not text or not math.isfinite(rating) or not 1.0 <= rating <= 5.0 or published > today or not _public_url(source):
            return "INVALID_REVIEW_EVIDENCE: invalid text, date, rating or source URL"
        key = (text.casefold(), published.isoformat())
        if key in seen:
            continue
        seen.add(key)
        if published >= today - timedelta(days=90):
            recent.append(rating)
        if rating <= 2.0:
            low.add(text.casefold())
    if len(recent) < 10:
        return "INSUFFICIENT_RECENT_REVIEW_EVIDENCE: at least 10 distinct dated trailing-90d reviews required"
    if round(sum(recent) / len(recent), 1) != round(candidate.trailing_90d_rating, 1):
        return "REVIEW_RATING_MISMATCH: trailing rating is not supported by dated reviews"
    sampled = {scrub_pii(review).casefold() for review in candidate.sampled_low_reviews if review}
    minimum = 5
    if candidate.total_low_review_count is not None and 3 <= candidate.total_low_review_count < 5 and candidate.total_review_count >= 50:
        minimum = 3
    if len(sampled) < minimum or not sampled <= low:
        return f"INSUFFICIENT_LOW_REVIEW_EVIDENCE: at least {minimum} distinct sampled 1-2 star reviews required"
    if candidate.total_review_count < len(seen) or (candidate.total_low_review_count is not None and candidate.total_low_review_count < len(low)):
        return "INVALID_REVIEW_COUNTS: totals cannot be smaller than the observed review sample"
    for next_poi in candidate.co_occurring_poi_ids:
        urls = candidate.co_occurrence_sources.get(next_poi, [])
        if len(set(urls)) < 2 or not all(_public_url(url) for url in urls):
            return "INSUFFICIENT_CO_OCCURRENCE_EVIDENCE: each directed pair needs 2 distinct public source URLs"
    return ""


def _metadata_error(candidate: RawScrapedCandidate) -> str:
    if (not candidate.venue_name or not candidate.goong_place_id or not _public_url(candidate.source_url)
        or candidate.lat is None or candidate.lng is None or candidate.corridor_km is None
        or not 8.0 <= candidate.lat <= 23.5 or not 102.0 <= candidate.lng <= 110.0
        or not math.isfinite(candidate.corridor_km) or candidate.corridor_km < 0
        or candidate.direction_allowed not in ("BOTH", "OUTBOUND", "INBOUND")
        or any(flag not in (True, False) for flag in (candidate.sedan_safe, candidate.has_ev_charge,
                                                       candidate.midday_sun_trap, candidate.is_arrival_anchor))
        or candidate.exertion_level not in ("LOW", "MODERATE", "HIGH")
        or candidate.open_minute is None or candidate.close_minute is None or candidate.dwell_minutes is None
        or not 0 <= candidate.open_minute < candidate.close_minute <= 1440
        or not 0 < candidate.dwell_minutes <= candidate.close_minute - candidate.open_minute):
        return "INVALID_POI_METADATA: verified location, operating hours and physical safety attributes required"
    return ""


def promote_verified_candidate_to_db(db_path: Path, candidate: RawScrapedCandidate) -> IngestionVerdict:
    verdict = evaluate_scraped_candidate(candidate)
    if verdict.accepted:
        error = _promotion_evidence_error(candidate) or _metadata_error(candidate)
        if error:
            verdict = IngestionVerdict(False, verdict.scrubbed_text, error)
    poi_id = candidate.poi_id or f"live_{candidate.corridor_id}_{hashlib.sha256(candidate.venue_name.encode()).hexdigest()[:16]}"
    candidate.poi_id = poi_id
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("BEGIN IMMEDIATE;")
        if verdict.accepted:
            corridor = conn.execute("SELECT total_spine_km FROM corridors WHERE corridor_id=?", (candidate.corridor_id,)).fetchone()
            branch = conn.execute("SELECT diverge_km,merge_km FROM corridor_branches WHERE corridor_id=? AND branch_id=?",
                                  (candidate.corridor_id, candidate.branch_id)).fetchone()
            if corridor is None or candidate.corridor_km > corridor[0] or branch is None or not branch[0] <= candidate.corridor_km <= branch[1]:
                raise sqlite3.IntegrityError("POI must lie inside its registered corridor branch")
            previous = conn.execute("SELECT corridor_id FROM pois WHERE poi_id=?", (poi_id,)).fetchone()
            if previous and previous[0] != candidate.corridor_id:
                raise sqlite3.IntegrityError("Cannot move an existing POI to a different corridor")
            columns = ["corridor_id", "branch_id", "goong_place_id", "category", "lat", "lng", "corridor_km",
                       "direction_allowed", "sedan_safe", "has_ev_charge", "midday_sun_trap", "is_arrival_anchor",
                       "exertion_level", "dwell_minutes", "open_minute", "close_minute", "utility_score", "scenic_score",
                       "detour_minutes", "hazard_level", "archetype_affinity", "source_url"]
            columns += ["name_vi", "commune_name", "tradeoff_note_vi"]
            values = [getattr(candidate, column) for column in columns[:-3]]
            values += [scrub_pii(candidate.venue_name), scrub_pii(candidate.goong_returned_commune), verdict.scrubbed_text]
            conn.execute(
                f"INSERT INTO pois (poi_id,{','.join(columns)}) VALUES ({','.join('?' for _ in range(len(columns)+1))}) "
                f"ON CONFLICT(poi_id) DO UPDATE SET {','.join(column+'=excluded.'+column for column in columns)}",
                [poi_id] + values,
            )
            conn.execute("DELETE FROM poi_review_evidence WHERE poi_id=?", (poi_id,))
            for review in candidate.review_evidence:
                text = scrub_pii(review["text"])
                key = hashlib.sha256((text.casefold() + review["published_at"][:10]).encode()).hexdigest()
                conn.execute("INSERT OR IGNORE INTO poi_review_evidence VALUES (?,?,?,?,?,?)",
                             (poi_id, key, review["published_at"][:10], review["rating"], text, review["source_url"]))
            conn.execute(
                "INSERT INTO poi_verifications (poi_id,lifetime_rating,trailing_90d_rating,total_review_count,total_low_review_count) "
                "VALUES (?,?,?,?,?) ON CONFLICT(poi_id) DO UPDATE SET lifetime_rating=excluded.lifetime_rating,"
                "trailing_90d_rating=excluded.trailing_90d_rating,total_review_count=excluded.total_review_count,"
                "total_low_review_count=excluded.total_low_review_count,verified_at=CURRENT_TIMESTAMP",
                (poi_id, candidate.lifetime_rating, candidate.trailing_90d_rating, candidate.total_review_count, candidate.total_low_review_count),
            )
            for next_poi in candidate.co_occurring_poi_ids:
                other = conn.execute("SELECT corridor_id FROM pois WHERE poi_id=?", (next_poi,)).fetchone()
                if other != (candidate.corridor_id,) or next_poi == poi_id:
                    raise sqlite3.IntegrityError("Co-occurrence endpoints must be distinct POIs in the same corridor")
                conn.execute("INSERT INTO poi_co_occurrence VALUES (?,?,?,20.0) ON CONFLICT DO NOTHING",
                             (candidate.corridor_id, poi_id, next_poi))
                for url in set(candidate.co_occurrence_sources[next_poi]):
                    conn.execute("INSERT OR IGNORE INTO poi_co_occurrence_sources VALUES (?,?,?,?)",
                                 (candidate.corridor_id, poi_id, next_poi, url))
        conn.execute(
            "INSERT INTO ingestion_audit (corridor_id,poi_id,venue_name,source_url,accepted,rejection_reason) VALUES (?,?,?,?,?,?)",
            (candidate.corridor_id, poi_id, scrub_pii(candidate.venue_name), scrub_pii(candidate.source_url),
             int(verdict.accepted), "" if verdict.accepted else verdict.rejection_reason),
        )
        conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()
    return verdict
