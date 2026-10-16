import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from happy_path.core.graph import normalize_vn

# Hardened Zero-PII regexes (catches contiguous SĐT0909123456, NBSP, web.facebook.com, group-member URLs, Unicode emails)
PII_PATTERNS = [
    re.compile(
        r"(?:https?://)?(?:www\.|m\.|web\.|mbasic\.)?(?:facebook|fb)\.com/"
        r"(?:profile\.php\?id=\d+|groups/[^/\s]+/user/[^/\s]+|(?!groups/)[A-Za-z0-9._-]+)(?:/)?",
        re.IGNORECASE,
    ),
    re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}"),
    re.compile(r"@[A-Za-z0-9_.-]+"),
    re.compile(r"(?:0|\+84)[35789](?:[\s.\-\u00a0]*\d){8}"),
]

DISILLUSIONMENT_TOKENS = tuple(
    unicodedata.normalize("NFC", tok)
    for tok in (
        "vỡ mộng",
        "chỉ được cái view",
        "đường vào nguy hiểm",
        "bắt mua vé",
        "đồ uống dở",
        "chặt chém",
        "cơm tù",
        "không giống trên mạng",
    )
)

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").strip()

@dataclass
class RawScrapedCandidate:
    venue_name: str
    expected_commune: str
    goong_returned_commune: str
    lifetime_rating: float
    trailing_90d_rating: float
    sampled_low_reviews: List[str]
    raw_testimonial_text: str
    poi_id: Optional[str] = None
    corridor_id: str = "hcm_dalat"
    branch_id: str = "MAIN_SPINE"
    goong_place_id: str = "goong_verified_live"
    category: str = "rest_stop"
    lat: float = 11.2015
    lng: float = 107.3512
    corridor_km: float = 114.0
    source_url: str = "https://goong.io/"
    co_occurring_poi_ids: List[str] = field(default_factory=list)

@dataclass
class IngestionVerdict:
    accepted: bool
    scrubbed_text: str
    rejection_reason: str = ""

def scrub_pii(text: str) -> str:
    cleaned = nfc(text)
    for pat in PII_PATTERNS:
        cleaned = pat.sub("[REDACTED_PII]", cleaned)
    return cleaned

def evaluate_scraped_candidate(candidate: RawScrapedCandidate) -> IngestionVerdict:
    scrubbed = scrub_pii(candidate.raw_testimonial_text)
    exp_commune = nfc(candidate.expected_commune)
    ret_commune = nfc(candidate.goong_returned_commune)

    # 1. Strict Input Admission Bounds Gate (Freeze-Spec F9.3)
    if len(exp_commune) < 2 or len(ret_commune) < 2:
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason="INVALID_COMMUNE: expected_commune and goong_returned_commune must be non-empty (>=2 chars)",
        )

    if (
        not math.isfinite(candidate.lifetime_rating)
        or not math.isfinite(candidate.trailing_90d_rating)
        or not (1.0 <= candidate.lifetime_rating <= 5.0)
        or not (1.0 <= candidate.trailing_90d_rating <= 5.0)
    ):
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason="INVALID_RATING: lifetime_rating and trailing_90d_rating must lie within [1.0, 5.0]",
        )

    normalized_reviews = [nfc(r).lower() for r in (candidate.sampled_low_reviews or []) if nfc(r)]
    if len(normalized_reviews) < 3:
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason="INSUFFICIENT_LOW_REVIEW_SAMPLE: at least 3 sampled low reviews are required for anti-shill verification",
        )

    # 2. Commune Concordance Gate (NFC normalized)
    if exp_commune.lower() != ret_commune.lower():
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason=f"COMMUNE_MISMATCH: expected '{exp_commune}', got '{ret_commune}'",
        )

    # 3. 90-Day Sentiment Drift Gate
    if (candidate.lifetime_rating - candidate.trailing_90d_rating) >= 0.4:
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason="SENTIMENT_DRIFT_DECAY: trailing 90d rating dropped >= 0.4 stars below lifetime average",
        )

    # 4. Bimodal 1-Star Disillusionment Veto (NFC normalized)
    hits = sum(
        1 for rev in normalized_reviews
        if any(tok in rev for tok in DISILLUSIONMENT_TOKENS)
    )
    ratio = hits / len(normalized_reviews)
    if ratio >= 0.15:
        return IngestionVerdict(
            accepted=False,
            scrubbed_text=scrubbed,
            rejection_reason=f"SHILL_TRAP_VETO: {ratio:.0%} of sampled low reviews matched disillusionment tokens",
        )

    return IngestionVerdict(accepted=True, scrubbed_text=scrubbed, rejection_reason="VERIFIED")

def extract_entity_rating_from_jsonld(
    jsonld_objects: List[Dict[str, Any]], target_venue_name: str
) -> Optional[Tuple[float, List[str]]]:
    """
    Entity-Bound JSON-LD Extractor (Freeze-Spec F9.4):
    Matches JSON-LD nodes whose normalized name overlaps target_venue_name so unrelated
    hotel/sidebar widgets on the same page never poison the target venue's rating.
    """
    target_norm = normalize_vn(target_venue_name)
    target_tokens = [t for t in target_norm.split() if len(t) >= 3]

    for obj in jsonld_objects:
        if not isinstance(obj, dict):
            continue
        obj_name = normalize_vn(str(obj.get("name", "")))
        if not obj_name:
            continue
        if target_norm in obj_name or obj_name in target_norm or (
            target_tokens and sum(1 for tok in target_tokens if tok in obj_name) >= max(1, len(target_tokens) // 2)
        ):
            agg = obj.get("aggregateRating") or {}
            try:
                rating_val = float(agg.get("ratingValue", 0.0))
            except (TypeError, ValueError):
                continue
            if not (1.0 <= rating_val <= 5.0):
                continue
            raw_revs = obj.get("review") or []
            revs: List[str] = []
            for r in raw_revs:
                if isinstance(r, dict):
                    body = nfc(str(r.get("reviewBody") or r.get("description") or ""))
                    if body:
                        revs.append(scrub_pii(body))
            return rating_val, revs
    return None

def select_commune_matching_prediction(
    predictions: List[Dict[str, Any]], expected_commune: str
) -> Optional[Dict[str, Any]]:
    """
    Commune-Aware Goong Prediction Selector (Freeze-Spec F9.4):
    Iterates through Goong Autocomplete predictions and selects the first prediction whose
    compound.commune or description matches expected_commune, rather than blindly picking predictions[0].
    """
    if not predictions:
        return None
    exp_norm = normalize_vn(expected_commune)
    for pred in predictions:
        compound = pred.get("compound") or {}
        commune_str = normalize_vn(str(compound.get("commune") or ""))
        desc_str = normalize_vn(str(pred.get("description") or ""))
        if exp_norm and (exp_norm == commune_str or exp_norm in commune_str or exp_norm in desc_str):
            return pred
    return predictions[0]

def promote_verified_candidate_to_db(
    db_path: Path, candidate: RawScrapedCandidate
) -> IngestionVerdict:
    """
    Direct SQLite Promoter (Freeze-Spec F9.5):
    Evaluates candidate through all admission gates and inserts directly into SQLite `pois`
    and `poi_co_occurrence` inside an atomic BEGIN IMMEDIATE transaction.
    """
    verdict = evaluate_scraped_candidate(candidate)
    if not verdict.accepted:
        return verdict

    poi_id = candidate.poi_id or f"live_{normalize_vn(candidate.venue_name).replace(' ', '_')[:32]}"
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            """INSERT OR REPLACE INTO pois (
                poi_id, corridor_id, branch_id, goong_place_id, name_vi, commune_name,
                category, lat, lng, corridor_km, direction_allowed, sedan_safe,
                has_ev_charge, midday_sun_trap, is_arrival_anchor, exertion_level,
                dwell_minutes, open_minute, close_minute, utility_score, scenic_score,
                detour_minutes, hazard_level, archetype_affinity, tradeoff_note_vi, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BOTH', 1, 0, 0, 0, 'LOW', 30, 0, 1440, 90.0, 85.0, 5, 0.05, 'ALL', ?, ?)""",
            (
                poi_id,
                candidate.corridor_id,
                candidate.branch_id,
                candidate.goong_place_id,
                nfc(candidate.venue_name),
                nfc(candidate.goong_returned_commune),
                candidate.category,
                candidate.lat,
                candidate.lng,
                candidate.corridor_km,
                verdict.scrubbed_text,
                candidate.source_url,
            ),
        )
        for next_poi in candidate.co_occurring_poi_ids:
            conn.execute(
                """INSERT OR REPLACE INTO poi_co_occurrence (corridor_id, from_poi_id, to_poi_id, synergy_bonus)
                   VALUES (?, ?, ?, 20.0)""",
                (candidate.corridor_id, poi_id, next_poi),
            )
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()
    return verdict
