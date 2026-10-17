# TikTok discovery

This is a supplementary discovery index across HCM–Đà Lạt, HCM–Nha Trang,
and HCM–Vũng Tàu. The collection searches Google for Vietnamese venue,
rest-stop, charging, parking, food, and road-access keywords restricted to TikTok.
It resolves Google's outbound redirects and retains distinct public video IDs.

`discovery.sqlite3` contains searches, videos, query-to-video hits, and direct
metadata attempts. Matching JSON exports and `summary.json` accompany it.
The search URLs and collection timestamps support replay. Google result titles
are retained, with PII scrubbing; result snippets and creator profiles are omitted.
Video links use TikTok's `/embed/<video_id>` format to omit creator handles.

`google_index_only` means only Google discovery has succeeded. A search match
does not confirm venue identity, branch membership, freshness, operational facts,
or the accuracy of a video's claims. `direct_video_metadata` means yt-dlp also
read metadata for that exact video ID. Its publication timestamp, when present,
comes from the video provider; Google relative dates are not converted into dates.
Likes and comment counts are platform counters, not star ratings. No videos,
author profiles, comment bodies, or production POIs are downloaded or promoted.

Run from the repository root:

```powershell
.venv\Scripts\python.exe scripts\discover_tiktok.py
```

Successful Google searches and completed metadata attempts are checkpointed.
The default direct-video pilot tests up to three videos per corridor. A detected
Google challenge stops further searches; it is recorded without attempting to
solve it. The scraper uses ordinary public requests and requires no account.

## Crawl discovered videos

```powershell
.venv\Scripts\python.exe scripts\crawl_tiktok.py
```

This reads the discovery queue and checkpoints public embed captions in the
`embed_crawls` SQLite table and `embed_crawls.json`. `crawl_summary.json` reports
the current outcome. The crawler waits 10 seconds between ordinary requests,
backs off for 45 seconds on rate-limit/overload responses, and ends the batch
after three consecutive access restrictions. An access challenge ends the batch
immediately. Successful video records are skipped when resuming.

To process the full pending queue, continuing through transient 429/503 responses:

```powershell
.venv\Scripts\python.exe scripts\crawl_tiktok.py --full --delay 15 --max-runtime-minutes 120
```

Full mode exponentially increases the wait on consecutive overload/rate-limit
responses, up to 15 minutes, and honors numeric Retry-After headers up to one
hour. It still stops on an access challenge. `active_crawl.json` records the
process ID, scope, attempts, new captions, current state and last update; compare
the process ID with the running process before treating an old status as active.
`embed_attempts.json` preserves request history, including failures after a
previous success. Unsuccessful later requests do not replace retained captions.

The initial HTTP probe returned one visible parking-guidance caption. That
observation was preserved with method `initial_httpx_embed_probe_visible_text`
and identity evidence `requested_public_embed_url`. Its publication date and
counters were unavailable and remain absent; the page body and creator name
were discarded. Automated structured extraction requires an embedded object
with the exact requested video ID. Captions remain supplementary text evidence
and do not qualify as star-rated reviews or verified operating facts.
