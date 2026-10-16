# Ticket 09: [Tracer Bullet] NFC-Normalized Anti-Shill Gate, Hardened Zero-PII Scrubber, Entity-Bound Scraping & Direct SQLite Promotion

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: Ticket 01
- **Slice Type**: Vertical Tracer Bullet

## What Can I Demo When This Is Done?
1. Feed visually identical Vietnamese disillusionment reviews encoded in both **Unicode `NFC` and decomposed `NFD`** (`vỡ mộng`) and observe identical `SHILL_TRAP_VETO` rejections.
2. Feed adversarial PII inputs—contiguous phone labels (`SĐT0909123456`), non-breaking-space phones (`0909 123 456`), `web.facebook.com` profile URLs, Facebook group-member paths (`/groups/12345/user/67890/`), and Unicode email local parts (`nguyễn.văn.a@gmail.com`)—and verify 100% redaction to `[REDACTED_PII]`.
3. Feed a multi-entity JSON-LD page (where a nearby 4.9★ hotel appears before a 4.2★ waterfall) and a multi-prediction Goong Autocomplete payload (where `predictions[0]` is in the wrong commune and `predictions[1]` matches the expected commune) and verify the collector binds the right entity rating and selects the commune-concordant Goong prediction.
4. Run the CLI promotion pipeline to promote verified candidates and co-occurrence pairs **directly into SQLite (`pois` and `poi_co_occurrence`)** while rejecting empty communes, out-of-range ratings (`99.0`), or insufficient low-review samples (`< 3` reviews).

## Root-Cause Failure Modes Eliminated
1. **Unicode NFD Bypass of Disillusionment Veto**: Decomposed Vietnamese diacritics bypassed NFC substring matching.
2. **PII Leaks on Contiguous Labels, NBSP, Group-Member URLs & Unicode Emails**: Word-boundary `` failed on `SĐT0909123456`, standard spaces missed ` `, and ASCII email regexes left Unicode prefixes intact.
3. **Cross-Entity JSON-LD Rating Poisoning & Blind `predictions[0]` Snapping**: Grabbing the first `AggregateRating` on a page or the first Goong prediction attributed wrong ratings and wrong communes.
4. **Unvalidated CLI Inputs (`"" == ""` communes, `rating = 99`) & Disconnected JSON Promotion**: Promoting unvalidated records to a dead JSON file instead of SQLite.

## Freeze-Grade Invariants (F9.1 – F9.5)
- **F9.1 (Mandatory Unicode `NFC` Normalization)**: All string fields (`venue_name`, `expected_commune`, `goong_returned_commune`, `sampled_low_reviews`, `raw_testimonial_text`) are normalized via `unicodedata.normalize("NFC", text)` before any validation, regex scrubbing, or token matching.
- **F9.2 (Hardened Zero-PII Scrubber)**: `scrub_pii` strips:
  - Vietnamese phone numbers (`(?:0|\+84)[35789](?:[\s.\- ]*\d){8}`) even when preceded by letters (`SĐT0909123456`) without requiring a leading ``.
  - Any Facebook profile or group-member URL (`(?:https?://)?(?:www\.|m\.|web\.|mbasic\.)?(?:facebook|fb)\.com/(?:profile\.php\?id=\d+|groups/[^/\s]+/user/[^/\s]+|(?!groups/)[A-Za-z0-9._-]+)`).
  - Full Unicode email addresses (`[^\s@]+@[^\s@]+\.[^\s@]{2,}`).
  - Social `@handles` (`@[A-Za-z0-9_.-]+`).
- **F9.3 (Strict Admission Bounds & Sample Size Gate)**: `evaluate_scraped_candidate` rejects:
  - `INVALID_COMMUNE` if `expected_commune` or `goong_returned_commune` has `< 2` characters.
  - `INVALID_RATING` if `lifetime_rating` or `trailing_90d_rating` is outside `[1.0, 5.0]` or non-finite.
  - `INSUFFICIENT_LOW_REVIEW_SAMPLE` if `len(sampled_low_reviews) < 3`.
  - `COMMUNE_MISMATCH`, `SENTIMENT_DRIFT_DECAY` (`>= 0.4`), and `SHILL_TRAP_VETO` (`>= 15%`).
- **F9.4 (Entity-Bound JSON-LD Extraction & Commune-Aware Goong Prediction Selection)**:
  - JSON-LD extraction matches `@type` and normalized entity `name` against the target `venue_name` (never pooling unrelated page widgets).
  - Goong Autocomplete resolution iterates through `predictions` and selects the first prediction whose commune/compound matches `expected_commune`, falling back to `predictions[0]` only if none match (which then fails the Commune Concordance Gate).
- **F9.5 (Direct SQLite Promotion with Provenance)**: `promote_verified_candidate_to_db` inserts verified candidates (with `goong_place_id`, `commune_name`, and scrubbed testimonial) and co-occurrence pairs directly into SQLiteInside an atomic transaction.

## Property-Based Acceptance & Regression Gates
1. **NFC vs. NFD Equivalence Gate**: Assert identical `SHILL_TRAP_VETO` verdicts on NFC and NFD encodings of `"vỡ mộng"` and `"chỉ được cái view"`.
2. **Adversarial PII Redaction Gate**: Test `SĐT0909123456`, `0909 123 456`, `https://web.facebook.com/groups/ghiendalat/user/100012345/`, and `nguyễn.văn.a@domain.vn`; assert zero PII tokens survive.
3. **Bounds, Entity-Binding & SQLite Promotion Gate**: Verify rejection of empty communes, `rating=99`, and `<3` low reviews; verify entity-specific JSON-LD rating extraction, commune-aware Goong prediction selection, and end-to-end promotion into SQLite visible via `/v1/corridors/health`.
