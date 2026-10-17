import json
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from happy_path.config import DB_PATH
from happy_path.core.collection import _youtube, collect_candidates


class TestCollectionEvidence(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_page_cannot_supply_rating_evidence(self):
        name = "Thác Dambri"
        body = '<script type="application/ld+json">' + json.dumps({
            "@type": "TouristAttraction", "name": name,
            "aggregateRating": {"ratingValue": 4.9},
        }) + '</script>'

        class Crawler:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def arun(self, url, **kwargs):
                return SimpleNamespace(url=url, html=body, success=True, status_code=404,
                                       markdown=SimpleNamespace(raw_markdown="Page not found"))

        transport = SimpleNamespace(AsyncWebCrawler=Crawler, BrowserConfig=lambda **kwargs: kwargs,
                                    CrawlerRunConfig=lambda **kwargs: kwargs, CacheMode=SimpleNamespace(BYPASS="bypass"))
        target = dict(poi_id="dl_thac_dambri", corridor_id="hcm_dalat", name_vi=name,
                      commune_name="Đam Bri", source_url="https://example.org/missing")
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", {"crawl4ai": transport}), patch(
            "happy_path.core.collection._youtube", new=AsyncMock(return_value=[])
        ), patch("happy_path.core.collection._facebook", new=AsyncMock(return_value=[])):
            log = Path(directory) / "sources.jsonl"
            candidates = await collect_candidates(DB_PATH, "", "https://rsapi.goong.io", log, [target])
            self.assertIsNone(candidates[0].lifetime_rating)
            receipts = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            web = [row for row in receipts if row["engine"] == "crawl4ai"]
            self.assertTrue(web)
            self.assertTrue(all(not row["success"] and row["reason"] == "WEB_HTTP_404" for row in web))

    async def test_unrelated_youtube_results_cannot_become_venue_evidence(self):
        venue = "Đồi Cừu Suối Nghệ"
        for relevant in (False, True):
            with self.subTest(relevant=relevant), tempfile.TemporaryDirectory() as directory:
                title = unicodedata.normalize("NFD", venue) if relevant else "Tứ Hợp Viện: Tôi Xuyên Không Hơi Sớm"
                video = dict(id="test_video", title=title, description="", comments=None)

                async def subprocess(*args, **kwargs):
                    payload = {"entries": [video]} if any(str(arg).startswith("ytsearch") for arg in args) else video
                    return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(payload).encode(), b"")))

                with patch("happy_path.core.collection.asyncio.create_subprocess_exec", side_effect=subprocess):
                    documents = await _youtube(venue, Path(directory) / "sources.jsonl")
                self.assertEqual(len(documents), 1 if relevant else 0)
                if relevant:
                    self.assertEqual(documents[0]["source_url"], "https://www.youtube.com/watch?v=test_video")
                    self.assertEqual(documents[0]["comments"], [])

    async def test_review_evidence_is_bound_to_one_outlet_and_provider(self):
        name = "Trạm sạc ô tô điện"
        html = '<script type="application/ld+json">' + json.dumps({
            "@type": "ElectricVehicleChargingStation", "name": name,
            "aggregateRating": {"ratingValue": 4.0, "reviewCount": 100},
            "review": [{"reviewBody": "Đánh giá cho một trạm cụ thể", "datePublished": "2026-10-16",
                        "reviewRating": {"ratingValue": 4.0}}],
        }) + '</script>'
        secondary = '<script type="application/ld+json">' + json.dumps({
            "@type": "ElectricVehicleChargingStation", "name": name,
            "aggregateRating": {"ratingValue": 4.8, "reviewCount": 200},
            "review": [{"reviewBody": f"Đánh giá nguồn B {index}", "datePublished": "2026-10-16",
                        "reviewRating": {"ratingValue": 4.8}} for index in range(20)],
        }) + '</script>'

        class Crawler:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def arun(self, url, **kwargs):
                body = secondary if "reviews.example" in url else html
                if "duckduckgo.com" in url:
                    body = '<a class="result__a" href="https://reviews.example/venue">Reviews</a>'
                return SimpleNamespace(url=url,html=body,success=True,status_code=200,
                                       markdown=SimpleNamespace(raw_markdown=name))

        transport = SimpleNamespace(AsyncWebCrawler=Crawler, BrowserConfig=lambda **kwargs: kwargs,
                                    CrawlerRunConfig=lambda **kwargs: kwargs, CacheMode=SimpleNamespace(BYPASS="bypass"))
        targets = [dict(poi_id="station_a",corridor_id="hcm_dalat",name_vi=name,commune_name="Long Thành",source_url="https://example.org/a"),
                   dict(poi_id="station_b",corridor_id="hcm_nhatrang",name_vi=name,commune_name="Cà Ná",source_url="https://example.org/b")]
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", {"crawl4ai": transport}), patch(
            "happy_path.core.collection._youtube", new=AsyncMock(return_value=[])
        ), patch("happy_path.core.collection._facebook", new=AsyncMock(return_value=[])):
            log = Path(directory) / "sources.jsonl"
            unique = await collect_candidates(DB_PATH, "", "https://rsapi.goong.io", log, targets[:1])
            self.assertEqual(unique[0].lifetime_rating, 4.8)
            self.assertEqual(len(unique[0].review_evidence), 20)
            self.assertTrue(all("reviews.example" in review["source_url"] for review in unique[0].review_evidence))
            ambiguous = await collect_candidates(DB_PATH, "", "https://rsapi.goong.io", log, targets)
            for candidate in ambiguous:
                self.assertIsNone(candidate.lifetime_rating)
                self.assertEqual(candidate.review_evidence, [])
            self.assertIn("AMBIGUOUS_ENTITY_NAME", log.read_text())


if __name__ == "__main__":
    unittest.main()
