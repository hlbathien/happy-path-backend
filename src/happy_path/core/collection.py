"""Public web, YouTube and Facebook CDP collection; comments are not star reviews."""
import asyncio
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx

from happy_path.core.graph import CorridorRegistry, normalize_vn
from happy_path.schemas.itinerary import Coordinate
from happy_path.core.ingestion import (
    RawScrapedCandidate, entity_nodes, extract_entity_rating_from_jsonld,
    nfc, normalize_text_fields, scrub_pii, select_commune_matching_prediction,
)


def _scrub_values(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_pii(value)
    if isinstance(value, list):
        return [_scrub_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_values(item) for key, item in value.items()}
    return value


def append_receipt(path: Path, receipt: Dict[str, Any]) -> Dict[str, Any]:
    record = _scrub_values(normalize_text_fields({"collected_at": datetime.now(timezone.utc).isoformat(), **receipt}))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    return record


def save_candidates(path: Path, candidates: List[RawScrapedCandidate]) -> None:
    """Retain real collected evidence for replay; missing ratings stay JSON null, never invented stars."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = _scrub_values(normalize_text_fields([asdict(candidate) for candidate in candidates]))
    path.write_text(json.dumps(records, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def _venue_name(name: str) -> str:
    name = nfc(name).split("(", 1)[0].strip()
    primary = name.split(" & ", 1)[0].strip()
    # Composite itinerary labels are not business names; never reduce a charger to generic "Trạm Dừng".
    if normalize_vn(primary) not in {"tram dung", "tram dung chan", "tram dung chan va sac nhanh ev"}:
        return primary
    return name


def _jsonld(html: str) -> List[Dict[str, Any]]:
    from bs4 import BeautifulSoup
    result = []
    for script in BeautifulSoup(nfc(html), "html.parser").find_all("script", type="application/ld+json"):
        try:
            value = normalize_text_fields(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
        result.extend(value if isinstance(value, list) else [value])
    return result


def _dated_reviews(objects: List[Dict[str, Any]], name: str, source: str) -> List[Dict[str, Any]]:
    result = []
    for entity in entity_nodes(objects, name):
        reviews = entity.get("review") or []
        if isinstance(reviews, dict):
            reviews = [reviews]
        for review in reviews:
            if not isinstance(review, dict):
                continue
            rating = review.get("reviewRating") or {}
            try:
                score = float(rating["ratingValue"])
                published = date.fromisoformat(review["datePublished"][:10]).isoformat()
            except (KeyError, TypeError, ValueError):
                continue
            text = scrub_pii(review.get("reviewBody") or review.get("description") or "")
            if text and 1.0 <= score <= 5.0:
                result.append(dict(text=text, rating=score, published_at=published, source_url=source))
    return result


async def _youtube(name: str, receipts: Path) -> List[Dict[str, Any]]:
    name = _venue_name(name)

    async def run(arguments: List[str]):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-single-json", "--no-warnings", *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            append_receipt(receipts, dict(engine="yt-dlp", venue_name=name, success=False, reason="YOUTUBE_TIMEOUT"))
            return None
        if process.returncode:
            append_receipt(receipts, dict(engine="yt-dlp", venue_name=name, success=False, reason="YOUTUBE_FETCH_FAILED"))
            return None
        try:
            return normalize_text_fields(json.loads(stdout))
        except ValueError:
            append_receipt(receipts, dict(engine="yt-dlp", venue_name=name, success=False, reason="YOUTUBE_INVALID_RESPONSE"))
            return None

    search = await run(["--flat-playlist", "ytsearch5:" + name])
    if search is None:
        return []
    identity = normalize_vn(name)
    video = next((item for item in (search.get("entries") or []) if item and identity in normalize_vn(item.get("title") or "")), None)
    if video is None:
        append_receipt(receipts, dict(engine="yt-dlp", venue_name=name, success=False, reason="YOUTUBE_NO_ENTITY_MATCH"))
        return []
    url = "https://www.youtube.com/watch?v=" + video["id"]
    payload = await run(["--write-comments", "--extractor-args", "youtube:comment_sort=new;max_comments=100", url])
    if payload is None:
        return []
    if identity not in normalize_vn(payload.get("title") or ""):
        append_receipt(receipts, dict(engine="yt-dlp", venue_name=name, source_url=url, success=False, reason="YOUTUBE_ENTITY_MISMATCH"))
        return []
    comments = [{"text": scrub_pii(comment.get("text") or ""), "timestamp": comment.get("timestamp")}
                for comment in (payload.get("comments") or [])]
    document = dict(engine="yt-dlp", venue_name=name, source_url=url, success=True,
                    title=payload.get("title", ""), comments=comments, text=scrub_pii(payload.get("description") or ""))
    append_receipt(receipts, document)
    return [document]


async def goong_get(client: httpx.AsyncClient, url: str, params: Dict[str, str]) -> httpx.Response:
    """Pace offline acquisition after observed burst 429s; never retry authentication or validation failures."""
    await asyncio.sleep(1.0)
    for attempt in range(3):
        response = await client.get(url, params=params)
        if response.status_code != 429 or attempt == 2:
            return response
        retry_after = response.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
        await asyncio.sleep(delay)
    return response


async def _resolve_goong(client: httpx.AsyncClient, base_url: str, key: str, name: str,
                         expected_commune: str, receipts: Path) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    prediction = None
    stage = "autocomplete"
    try:
        response = await goong_get(client, base_url + "/Place/AutoComplete", {"api_key": key, "input": nfc(name)})
        response.raise_for_status()
        payload = normalize_text_fields(response.json())
        prediction = select_commune_matching_prediction(payload.get("predictions") or [], expected_commune)
        append_receipt(receipts, dict(engine="goong", stage=stage, venue_name=name, success=prediction is not None,
                                     http_status=response.status_code, api_status=payload.get("status"),
                                     expected_commune=expected_commune, prediction=prediction,
                                     commune_matches=bool(prediction and nfc((prediction.get("compound") or {}).get("commune") or "").casefold() == nfc(expected_commune).casefold()),
                                     reason="" if prediction else "NO_GOONG_PREDICTION"))
        if prediction is None:
            return None, None
        stage = "detail"
        response = await goong_get(client, base_url + "/Place/Detail", {"api_key": key, "place_id": prediction["place_id"]})
        response.raise_for_status()
        payload = normalize_text_fields(response.json())
        result = payload.get("result") or {}
        location = (result.get("geometry") or {}).get("location")
        append_receipt(receipts, dict(engine="goong-detail", stage=stage, venue_name=name, success=location is not None,
                                     http_status=response.status_code, api_status=payload.get("status"),
                                     place_id=prediction["place_id"], location=location, name=result.get("name"),
                                     compound=result.get("compound"), types=result.get("types"), source_url=result.get("url"),
                                     reason="" if location else "NO_GOONG_LOCATION"))
        return prediction, location
    except (httpx.HTTPError, ValueError) as error:
        append_receipt(receipts, dict(engine="goong", stage=stage, venue_name=name, success=False,
                                     http_status=error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
                                     error_type=type(error).__name__, reason="GOONG_RESOLUTION_FAILED"))
        return prediction, None


async def harvest_verified_edges(db_path: Path, key: str, base_url: str, receipts: Path,
                                 poi_ids: Optional[List[str]] = None) -> Dict[str, int]:
    """Fetch real static road durations for verified stops, then publish graph edges atomically."""
    registry = CorridorRegistry(db_path)
    with sqlite3.connect(registry.runtime_db_path) as conn:
        verified = {row[0] for row in conn.execute("SELECT poi_id FROM poi_verifications")}
    selected = verified if poi_ids is None else verified.intersection(poi_ids)
    if not selected:
        return {"verified_pois": 0, "requests": 0, "edges_written": 0}
    if not key:
        raise ValueError("GOONG_API_KEY_REQUIRED: real road matrices cannot be fabricated")
    edges = []
    requests = 0
    async with httpx.AsyncClient(timeout=25) as client:
        for corridor_id, corridor in registry.corridors.items():
            stops = registry.pois_by_corridor[corridor_id]
            if not selected.intersection(p.poi_id for p in stops):
                continue
            nodes = [dict(node_id="ORIGIN",lat=corridor.origin_lat,lng=corridor.origin_lng,km=0.0,branch="MAIN_SPINE",hazard=0.0)]
            nodes += [dict(node_id=p.poi_id,lat=p.lat,lng=p.lng,km=p.corridor_km,branch=p.branch_id,hazard=p.hazard_level) for p in stops]
            nodes += [dict(node_id="DEST",lat=corridor.dest_lat,lng=corridor.dest_lng,km=corridor.total_spine_km,branch="MAIN_SPINE",hazard=0.0)]
            branches = registry.branches_by_corridor[corridor_id]
            for i in range(0, len(nodes), 10):
                origins = nodes[i:i+10]
                for j in range(0, len(nodes), 10):
                    destinations = nodes[j:j+10]
                    if not any(n["node_id"] in selected for n in origins+destinations):
                        continue
                    response = await goong_get(client, base_url + "/DistanceMatrix", {
                        "api_key": key, "vehicle": "car",
                        "origins": "|".join(f"{n['lat']},{n['lng']}" for n in origins),
                        "destinations": "|".join(f"{n['lat']},{n['lng']}" for n in destinations),
                    })
                    requests += 1
                    if response.status_code != 200:
                        raise RuntimeError(f"GOONG_MATRIX_HTTP_{response.status_code}: graph was not changed")
                    payload = normalize_text_fields(response.json())
                    rows = payload.get("rows") or []
                    if len(rows) != len(origins) or any(len(row.get("elements") or []) != len(destinations) for row in rows):
                        raise ValueError("GOONG_MATRIX_SHAPE_MISMATCH: graph was not changed")
                    append_receipt(receipts, dict(engine="goong-matrix",source_url=base_url+"/DistanceMatrix",corridor_id=corridor_id,
                                                 profile="static_base_not_historical_traffic",origins=[n["node_id"] for n in origins],
                                                 destinations=[n["node_id"] for n in destinations],rows=rows))
                    for u, row in zip(origins, rows):
                        for v, element in zip(destinations, row["elements"]):
                            if u["node_id"] == v["node_id"] or not ({u["node_id"],v["node_id"]} & selected):
                                continue
                            if u["branch"] != v["branch"]:
                                if u["branch"] != "MAIN_SPINE" and v["branch"] != "MAIN_SPINE":
                                    continue
                                alternative, main = (u,v) if u["branch"] != "MAIN_SPINE" else (v,u)
                                branch = branches[alternative["branch"]]
                                if branch.diverge_km < main["km"] < branch.merge_km:
                                    continue
                            if element.get("status") != "OK":
                                continue  # No road route is not permission to invent an edge.
                            seconds = float((element.get("duration") or {}).get("value",0))
                            metres = float((element.get("distance") or {}).get("value",0))
                            if not math.isfinite(seconds) or not math.isfinite(metres) or seconds <= 0 or metres <= 0:
                                continue
                            for bucket in ("early_morning","day_normal","peak_congestion","rain_evening"):
                                hazard = max(u["hazard"],v["hazard"]) * (1.5 if bucket == "rain_evening" else 1.0)
                                # Matrix responses supply no route polyline. Runtime slicing/live decoration owns geometry.
                                edges.append((corridor_id,u["node_id"],v["node_id"],bucket,max(5,math.ceil(seconds/60)),metres/1000,hazard,""))
    conn = sqlite3.connect(registry.runtime_db_path, timeout=5,isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT INTO corridor_edges VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(corridor_id,from_node,to_node,time_bucket) "
                         "DO UPDATE SET drive_minutes=excluded.drive_minutes,distance_km=excluded.distance_km,"
                         "hazard_penalty=excluded.hazard_penalty,encoded_polyline=excluded.encoded_polyline",edges)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"verified_pois":len(selected),"requests":requests,"edges_written":len(edges)}


async def _facebook(urls: List[str], receipts: Path) -> List[Dict[str, Any]]:
    from playwright.async_api import async_playwright
    endpoint = os.getenv("FACEBOOK_CDP_URL") or os.getenv("BU_CDP_URL") or os.getenv("CDP_URL")
    if not endpoint:
        append_receipt(receipts, dict(engine="facebook-cdp", success=False, reason="FACEBOOK_CDP_UNAVAILABLE"))
        return []
    documents = []
    try:
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(endpoint, timeout=10000)
            context = browser.contexts[0]
            page = await context.new_page()
            try:
                for url in urls or ["https://www.facebook.com/"]:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    text = nfc(await page.locator("body").inner_text())
                    if await page.locator('input[type="password"]').count():
                        append_receipt(receipts, dict(engine="facebook-cdp", source_url=url, success=False,
                                                     reason="FACEBOOK_LOGIN_REQUIRED"))
                        break  # No passwords, private tokens, login automation or CAPTCHA bypass.
                    document = dict(engine="facebook-cdp", source_url=url, success=True, text=scrub_pii(text))
                    append_receipt(receipts, document)
                    documents.append(document)
            finally:
                await page.close()
    except Exception as error:
        # A blocked external browser must be reported, never replaced by invented posts.
        append_receipt(receipts, dict(engine="facebook-cdp", success=False, reason="FACEBOOK_CDP_FAILED", error_type=type(error).__name__))
    return documents


async def collect_candidates(db_path: Path, goong_key: str, goong_base_url: str, receipts: Path,
                             targets: Optional[List[Dict[str, Any]]] = None) -> List[RawScrapedCandidate]:
    from bs4 import BeautifulSoup
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    if targets is None:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            targets = [dict(row) for row in connection.execute("SELECT * FROM pois ORDER BY corridor_id,corridor_km") ]
    targets = normalize_text_fields(targets)
    identity_counts = Counter(normalize_vn(_venue_name(target["name_vi"])) for target in targets)
    registry = CorridorRegistry(db_path)
    documents, candidates, facebook_urls = [], [], set()
    fetches = {}
    semaphore = asyncio.Semaphore(3)
    async with AsyncWebCrawler(config=BrowserConfig(headless=False, verbose=False)) as crawler, httpx.AsyncClient(timeout=20) as client:
        async def fetch(url: str):
            async with semaphore:
                result = await crawler.arun(url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=25000, verbose=False))
            text = scrub_pii(result.markdown.raw_markdown if result.markdown else "")
            reason = "" if result.success else "WEB_FETCH_BLOCKED"
            if result.status_code and result.status_code >= 400:
                reason = f"WEB_HTTP_{result.status_code}"
            if any(marker in text.casefold() for marker in ("unusual traffic", "captcha", "please complete the following challenge", "please complete the verification test", "performing security verification")):
                reason = "WEB_CHALLENGE_REQUIRED"
            if " ".join(text.casefold().split()) == "500 sorry, something went wrong.":
                reason = "WEB_APPLICATION_ERROR"
            result.success = bool(result.success and not reason)
            document = dict(engine="crawl4ai", source_url=url, success=bool(result.success and not reason),
                            status=result.status_code, reason=reason, text=text)
            append_receipt(receipts, document)
            documents.append(document)
            return result

        async def get(url: str):
            if url not in fetches:
                fetches[url] = asyncio.create_task(fetch(url))
            return await fetches[url]

        async def collect(target: Dict[str, Any]) -> None:
            name = _venue_name(target["name_vi"])
            prediction, location = None, None
            if goong_key:
                prediction, location = await _resolve_goong(client, goong_base_url, goong_key, name, target["commune_name"], receipts)
            else:
                append_receipt(receipts, dict(engine="goong", venue_name=name, success=False, reason="GOONG_API_KEY_REQUIRED"))
            urls = [target["source_url"]]
            # Search public review sources in addition to first-party/government venue pages.
            search = await get("https://html.duckduckgo.com/html/?q=" + quote_plus('"' + name + '" reviews đánh giá'))
            soup = BeautifulSoup(nfc(search.html or ""), "html.parser")
            for anchor in soup.select("a.result__a")[:3]:
                href = anchor.get("href", "")
                if href.startswith("//"):
                    href = "https:" + href
                query = parse_qs(urlsplit(href).query)
                href = query.get("uddg", [href])[0]
                if urlsplit(href).scheme in ("https", "http") and scrub_pii(href) == href:
                    urls.append(href)
            pages = await asyncio.gather(*(get(url) for url in dict.fromkeys(urls)))
            ambiguous_identity = identity_counts[normalize_vn(name)] > 1
            if ambiguous_identity:
                append_receipt(receipts, dict(engine="entity-binding",venue_name=name,poi_id=target["poi_id"],
                                             success=False,reason="AMBIGUOUS_ENTITY_NAME: source-specific place ID binding required"))
            cohorts = {}
            for result in pages:
                if not result.success:
                    continue
                objects = [] if ambiguous_identity else _jsonld(result.html or "")
                rating = extract_entity_rating_from_jsonld(objects, name)
                dated = _dated_reviews(objects, name, result.url)
                if rating or dated:
                    provider = urlsplit(result.url).hostname
                    cohort = cohorts.setdefault(provider, {"lifetime": None, "reviews": [], "total": 0, "source_url": result.url})
                    if rating and cohort["lifetime"] is None:
                        cohort["lifetime"] = rating[0]
                    cohort["reviews"].extend(dated)
                    for entity in entity_nodes(objects, name):
                        try:
                            cohort["total"] = max(cohort["total"], int((entity.get("aggregateRating") or {}).get("reviewCount", 0)))
                        except (TypeError, ValueError):
                            pass
                for anchor in BeautifulSoup(nfc(result.html or ""), "html.parser").find_all("a", href=True):
                    href = anchor["href"]
                    if urlsplit(href).hostname in ("www.facebook.com", "facebook.com", "web.facebook.com"):
                        # Keep public post/group links, never profile or group-member URLs.
                        path = urlsplit(href).path
                        if ("/posts/" in path or path.startswith("/groups/")) and "/user/" not in path:
                            facebook_urls.add(href)
            today = datetime.now(timezone.utc).date()
            for cohort in cohorts.values():
                cohort["reviews"] = list({(review["text"].casefold(), review["published_at"]): review for review in cohort["reviews"]}.values())
            # Choose by dated coverage, never by a favorable rating; do not mix platform populations.
            provider = max(cohorts, key=lambda item: (
                sum(today-timedelta(days=90) <= date.fromisoformat(review["published_at"]) <= today for review in cohorts[item]["reviews"]),
                len(cohorts[item]["reviews"]),
            ), default=None)
            selected = cohorts[provider] if provider is not None else {"lifetime": None, "reviews": [], "total": 0, "source_url": ""}
            reviews, lifetime, total = selected["reviews"], selected["lifetime"], selected["total"]
            append_receipt(receipts, dict(engine="review-cohort",poi_id=target["poi_id"],venue_name=name,provider=provider,
                                         source_url=selected["source_url"],dated_reviews=len(reviews),success=bool(reviews),
                                         reason="" if reviews else "NO_ENTITY_BOUND_DATED_REVIEW_HISTORY"))
            recent = [review["rating"] for review in reviews if today - timedelta(days=90) <= date.fromisoformat(review["published_at"]) <= today]
            low = [review["text"] for review in reviews if review["rating"] <= 2.0]
            values = {key: target.get(key) for key in (
                "poi_id", "corridor_id", "branch_id", "category", "lat", "lng", "corridor_km", "source_url",
                "direction_allowed", "sedan_safe", "has_ev_charge", "midday_sun_trap", "is_arrival_anchor", "exertion_level",
                "dwell_minutes", "open_minute", "close_minute", "utility_score", "scenic_score", "detour_minutes", "hazard_level", "archetype_affinity",
            )}
            # Never promote seed coordinates as if they came from the live place resolver.
            values["lat"] = location.get("lat") if location else None
            values["lng"] = location.get("lng") if location else None
            if location:
                coordinate = Coordinate(lat=values["lat"], lng=values["lng"])
                values["corridor_km"] = registry.project_coordinate_to_progress_km(
                    registry.corridors[target["corridor_id"]], "outbound", coordinate, is_origin=False,
                )
            candidates.append(RawScrapedCandidate(
                venue_name=name, expected_commune=target["commune_name"],
                goong_returned_commune=((prediction or {}).get("compound") or {}).get("commune") or "",
                goong_place_id=(prediction or {}).get("place_id") or "", lifetime_rating=lifetime,
                trailing_90d_rating=sum(recent) / len(recent) if recent else None,
                sampled_low_reviews=low, raw_testimonial_text="\n".join(review["text"] for review in reviews),
                review_evidence=reviews, total_review_count=total, **values,
            ))
            if not ambiguous_identity:
                async with semaphore:
                    documents.extend(await _youtube(name, receipts))

        # Sequential venue discovery avoids hammering search/review sites; page fetches are shared and bounded.
        for target in targets:
            await collect(target)
    documents.extend(await _facebook(sorted(facebook_urls), receipts))
    for candidate in candidates:
        for other in candidates:
            if (other.poi_id == candidate.poi_id or other.corridor_id != candidate.corridor_id
                or identity_counts[normalize_vn(candidate.venue_name)] > 1 or identity_counts[normalize_vn(other.venue_name)] > 1):
                continue
            sources = set()
            for document in documents:
                if not document.get("success") or urlsplit(document.get("source_url", "")).hostname == "html.duckduckgo.com":
                    continue  # Search snippets are discovery, not independent pair evidence.
                texts = [document.get("text", "")] + [comment["text"] for comment in document.get("comments", [])]
                for text in texts:
                    text = nfc(text).casefold()
                    first, second = text.find(candidate.venue_name.casefold()), text.find(other.venue_name.casefold())
                    if 0 <= first < second:
                        sources.add(document["source_url"])
            if len(sources) >= 2:
                candidate.co_occurring_poi_ids.append(other.poi_id)
                candidate.co_occurrence_sources[other.poi_id] = sorted(sources)
    return candidates
