"""Source-specific public review extraction. Native rating scales remain explicit."""
import math
import re
from datetime import date
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from happy_path.core.collection import _jsonld
from happy_path.core.ingestion import entity_nodes, nfc, scrub_pii


def _number(value: Any, maximum: float) -> Optional[float]:
    try:
        score = float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None
    return score if math.isfinite(score) and 0 <= score <= maximum else None


def _date(text: str) -> Optional[str]:
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})(?=$|[T\s])", text)
    try:
        if match:
            return date.fromisoformat(match[1]).isoformat()
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
        if match:
            return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
    except ValueError:
        pass
    return None  # Relative dates do not establish an exact publication date.


def extract_review_page(html: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(nfc(html), "html.parser")
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    heading = soup.select_one(".main-info-title h1") if host == "foody.vn" else soup.h1
    name = scrub_pii(heading.get_text(" ", strip=True)) if heading else ""
    result = dict(provider=host, source_url=url, venue_name=name, aggregate_rating=None,
                  rating_scale=None, total_review_count=None, reviews=[], address=None, lat=None, lng=None)
    if not name:
        return result
    if host == "foody.vn":
        lat = soup.find("meta", property="place:location:latitude")
        lng = soup.find("meta", property="place:location:longitude")
        try:
            coordinates = float(lat["content"]), float(lng["content"])
            if all(math.isfinite(value) for value in coordinates) and 8 <= coordinates[0] <= 23.5 and 102 <= coordinates[1] <= 110:
                result["lat"], result["lng"] = coordinates
        except (TypeError, KeyError, ValueError):
            pass
        result["rating_scale"] = 10.0
        aggregate = soup.select_one(".ratings-boxes-points")
        if aggregate:
            match = re.search(r"\d+(?:[.,]\d+)?", aggregate.get_text(" ", strip=True))
            result["aggregate_rating"] = _number(match[0], 10) if match else None
        count = soup.select_one(".microsite-review-count")
        if count and count.get_text(strip=True).isdigit():
            result["total_review_count"] = int(count.get_text(strip=True))
        address = soup.select_one(".main-info-address, .res-common-add")
        result["address"] = scrub_pii(address.get_text(" ", strip=True)) if address else None
        for node in soup.select(".review-list .review-item"):
            score, clock, body = (node.select_one(selector) for selector in (".review-points", ".ru-time", ".rd-des span"))
            body = body or node.select_one(".rd-des")
            if body is None:
                continue
            text = scrub_pii(body.get_text(" ", strip=True))
            rating = _number(score.get_text(strip=True), 10) if score else None
            published = _date(clock.get_text(" ", strip=True)) if clock else None
            if text:
                result["reviews"].append(dict(text=text, rating=rating, rating_scale=10.0,
                                               published_at=published, source_url=url))
    elif host == "gody.vn":
        result["rating_scale"] = 5.0
        # The page heading binds the aggregate; recommendations/widgets are excluded.
        for entity in entity_nodes(_jsonld(html), name):
            aggregate = entity.get("aggregateRating") or {}
            result["aggregate_rating"] = _number(aggregate.get("ratingValue"), 5)
            try:
                result["total_review_count"] = int(aggregate["reviewCount"])
            except (KeyError, TypeError, ValueError):
                pass
            address = entity.get("address")
            if isinstance(address, str):
                result["address"] = scrub_pii(address)
            break
        for node in soup.select('[id^="comment-id-"]'):
            score = node.select_one(".js-destination-rating-ajax[data-avg], .js-destination-rating[data-avg]")
            clock = node.select_one("span.fs-12")
            body = node.select_one("p.ow-break-word")
            text = scrub_pii(body.get_text(" ", strip=True)) if body else ""
            rating = _number(score.get("data-avg"), 5) if score else None
            published = _date(clock.get_text(" ", strip=True)) if clock else None
            if text or rating is not None:
                result["reviews"].append(dict(text=text, rating=rating, rating_scale=5.0,
                                               published_at=published, source_url=url))
    # Deduplicate replayed/rendered review blocks without retaining reviewer profiles.
    result["reviews"] = list({(row["text"].casefold(), row["rating"], row["published_at"]): row
                               for row in result["reviews"]}.values())
    return result
