# 03 — Zero-Cost ($0) Scraping & Ingestion Playbook (Freeze-Spec v3.0)

## 1. Mandatory Unicode `NFC` Normalization
Vietnamese text from social media and review platforms frequently mixes precomposed (`NFC`) and decomposed (`NFD`, e.g., `o` + combining tilde/dot) codepoints. Every ingestion function normalizes all input strings via `unicodedata.normalize("NFC", s)` at the boundary before running regexes or token checks.

## 2. Hardened Zero-PII Scrubber (`Nghị định 13/2023/NĐ-CP`)
`scrub_pii(text)` redacts all personal identifiers to `[REDACTED_PII]`:
1. **Phone numbers (including contiguous labels & NBSP)**: `(?:0|\+84)[35789](?:[\s.\-\u00a0]*\d){8}` (catches `SĐT0909123456`, `0909 123 456`, `+84 909.123.456`).
2. **Facebook personal & group-member URLs**: `(?:https?://)?(?:www\.|m\.|web\.|mbasic\.)?(?:facebook|fb)\.com/(?:profile\.php\?id=\d+|groups/[^/\s]+/user/[^/\s]+|(?!groups/)[A-Za-z0-9._-]+)`.
3. **Full Unicode email addresses**: `[^\s@]+@[^\s@]+\.[^\s@]{2,}`.
4. **Social handles**: `@[A-Za-z0-9_.-]+`.

## 3. Admission Gates & Entity-Bound Resolution
1. **Input Bounds Gate**: Reject if `len(expected_commune) < 2`, `len(goong_returned_commune) < 2`, `not (1.0 <= lifetime_rating <= 5.0)`, `not (1.0 <= trailing_90d_rating <= 5.0)`, or `len(sampled_low_reviews) < 3`.
2. **Commune Concordance Gate (`COMMUNE_MISMATCH`)**: `expected_commune.lower() == goong_returned_commune.lower()`.
3. **90-Day Sentiment Drift Gate (`SENTIMENT_DRIFT_DECAY`)**: Reject if `(lifetime_rating - trailing_90d_rating) >= 0.4`.
4. **Bimodal 1-Star Disillusionment Veto (`SHILL_TRAP_VETO`)**: Reject if `>= 15%` of `sampled_low_reviews` match any `NFC`-normalized token in `DISILLUSIONMENT_TOKENS`.
5. **Entity-Bound JSON-LD & Commune-Aware Goong Prediction Selection**:
   - `extract_entity_rating_from_jsonld(jsonld_items, target_venue_name)` filters JSON-LD nodes by normalized name overlap with `target_venue_name` before extracting `aggregateRating`, preventing adjacent hotel/ad widgets from poisoning a POI's rating.
   - `select_commune_matching_prediction(predictions, expected_commune)` selects the first Goong prediction whose `compound.commune` or `description` matches `expected_commune`.
6. **Direct Atomic SQLite Promotion**: Verified candidates promote directly into SQLite (`pois` and `poi_co_occurrence`).
