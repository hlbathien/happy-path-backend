# Happy Path research catalog

This catalog contains live public-source acquisition evidence. It is separate from
`data/happy_path.db`: research records have not passed the itinerary admission gates.
See `REPORT.md` for the completed run's measured coverage and limitations.

## Files

- `catalog.sqlite3`: canonical, checkpointed dataset, with foreign-key relationships.
- `places.json`: unique Goong place IDs, provider coordinates, addresses and native types.
- `coverage.json`: discovery region/category membership; this is not a verified road branch.
- `place_assessments.json`: supported venue types, generic/unknown types, and excluded geographic/transport entities.
- `review_venues.json`: source-specific venue identity, observed aggregate and native scale.
- `review_locations.json`: source-published coordinates and provenance.
- `review_scope_assessments.json`: geometric discovery coverage or unresolved province-level scope.
- `reviews.json`: scrubbed text, actual publication date when available, observed score and native scale.
- `social_documents.json`: public YouTube descriptions/comments without author profiles.
- `summary.json`: counts calculated from SQLite, not requested quotas.
- `tiktok/`: separate Google discovery index and bounded direct-video metadata pilot; see its README.
- `*_receipts.jsonl`: acquisition successes/failures; credentials are excluded.

Rating-only records have empty text. Text-only comments have a null rating. Missing
dates remain null. Foody scores stay on the provider's 10-point scale; Gody scores
stay on its 5-point scale. Comments, ages, ratings and operating hours are never
invented to make a record pass an admission gate. Records are deduplicated within
their source venue; distinct branches and platforms are not silently combined.

## Continue acquisition

From the repository root in PowerShell:

```powershell
.venv\Scripts\python.exe scripts\expand_dataset.py --discover --reviews --social --page-budget 90
```

Existing successful requests are checkpointed. Newly discovered source URLs remain
in the frontier. `--corridors hcm_vungtau` restricts a run to one corridor, and
`--source-file path.json` uses an explicit list of `corridor_id`/`source_url` objects
without recursive page expansion. Goong credentials load from the ignored `.env`.
Facebook still needs a working browser CDP connection.

An acquisition shard can be merged with:

```powershell
.venv\Scripts\python.exe scripts\expand_dataset.py --merge-catalog path\catalog.sqlite3
```

Inspect usable provider classifications without counting excluded records:

```sql
SELECT c.corridor_id, COUNT(DISTINCT c.place_id) AS provider_typed_venues
FROM coverage c JOIN place_assessments a USING(place_id)
WHERE a.status = 'venue_type_supported'
GROUP BY c.corridor_id;
```

Review scores alone do not prove safety, current opening hours, commune concordance,
EV availability or drivable branch membership. Production promotion continues to
require the repository's full evidence and physical-trip checks.
