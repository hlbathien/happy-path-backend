# Expanded research dataset — 17 October 2026

The acquisition catalog now contains **499 unique Goong place IDs**, **1998 source review/rating records**, and **434 dated written reviews** across the three research corridor groups. The original 25 itinerary POIs remain in the production database; no unverified record was promoted.

| Research corridor | Discovery areas | Map records | Provider-typed venues | Review-source venues | Dated written reviews |
| --- | ---: | ---: | ---: | ---: | ---: |
| hcm_dalat | 6 | 194 | 91 | 43 | 126 |
| hcm_nhatrang | 9 | 181 | 105 | 67 | 209 |
| hcm_vungtau | 6 | 124 | 55 | 28 | 99 |

Map records are deduplicated by Goong place ID. A record may have more than one region membership. Review-source venues are identified by provider URL, so different platforms or branches are not silently merged into a single business identity. Counts in these two catalogs should not be added to claim a total unique business count.

## What grew

- The first run attempted the 25 registered POIs and extracted zero structured dated star reviews. This expansion executed 84 region/category autocomplete tasks and resolved actual provider geometry into the research catalog.
- 434 written reviews now have both an exact date and a provider score. Another 1559 records contain a provider score without written text. Missing text, dates and scores remain explicit.
- 56 review-source pages expose their own coordinates. Scope is assessed against the 20 km discovery regions rather than copying a Goong commune into an independent-validation field.
- The catalog preserves native Foody 10-point scores and Gody 5-point scores. Text-only comments do not acquire a rating from numbers mentioned in the prose.
- Previous and new public YouTube evidence is deduplicated by video URL and comment text/timestamp: 10 videos and 542 comments.
- The source frontier is checkpointed for further acquisition. A GeoJSON export includes only records with supported provider venue types and marks every point as unadmitted.

## Coverage and quality limits

Provider type classifications: `{"excluded_geographic_entity": 7, "excluded_transport_service": 45, "needs_type_verification": 196, "venue_type_supported": 251}`.

Review-source geographic scope: `{"coordinate_in_discovery_region": 55, "outside_discovery_regions": 1, "outside_drivable_corridor": 4, "province_group_only": 78}`.

Administrative areas and transport/travel businesses are excluded from the supported-venue count. Generic types require manual verification. Côn Đảo source venues are flagged outside the drivable corridors. Province-only review sources still require a precise location and business-identity binding before itinerary use.

Dated written reviews span 2013-08-09 through 2025-05-11. **0 written/rated reviews fall within the trailing 90 days.** The expanded evidence therefore improves historical coverage and discovery, but does not yet establish fresh sentiment, current operating hours, EV availability, median-side access or safe branch topology. Production promotion remains at zero.

Facebook CDP is unavailable in the acquisition environment. The collector records the connection failure and retains no invented Facebook content.

## Implementation and delivery

`scripts/expand_dataset.py` supplies checkpointed discovery, bounded provider crawling, source-specific extraction, geographic/type assessments and completed-shard merging. `src/happy_path/core/review_pages.py` extracts the real Foody/Gody DOM formats, strips reviewer/profile fields, handles both local dates and ISO timestamps, and preserves source rating scales. Reprocessing a corrected date replaces the incomplete row instead of inflating the count.

The SQLite database and JSON exports are the canonical delivered data. Raw reviewer/profile navigation from early cached pages was replaced with retained venue/review content and public venue-discovery URLs. No remaining ticket-regex PII matches or supplied API-key matches were found in retained text fields; foreign-key checks passed. This check covers the defined patterns, not arbitrary personal information mentioned inside a public review.

See README.md for schemas, filtering queries, and the command to continue acquisition. All results are live public-source observations, not generated POIs or sample reviews.

Validation: the complete repository suite passed 23 tests on Windows. The production database was restored after disposable test writes.
