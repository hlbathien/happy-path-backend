import json
import tempfile
import unittest
from pathlib import Path

from scripts.expand_dataset import Catalog, canonical_provider_url


class TestResearchCatalog(unittest.TestCase):
    def test_replay_deduplicates_reviews_and_preserves_raw_scale(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Catalog(Path(folder))
            try:
                page = dict(source_url="https://www.foody.vn/vung-tau/test/binh-luan", provider="foody.vn", venue_name="Venue",
                            address=None, aggregate_rating=8.0, rating_scale=10.0, total_review_count=1,
                            reviews=[dict(text="Review", rating=8.0, rating_scale=10.0, published_at="2020-01-01")])
                catalog.add_review_page(page, "hcm_vungtau")
                catalog.add_review_page(page, "hcm_vungtau")
                self.assertEqual(catalog.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
                self.assertEqual(catalog.db.execute("SELECT rating,rating_scale FROM reviews").fetchone(), (8.0, 10.0))
            finally:
                catalog.db.close()

    def test_date_correction_replaces_incomplete_row_without_inflating_count(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Catalog(Path(folder))
            try:
                page = dict(source_url="https://gody.vn/chau-a/viet-nam/lam-dong/venue", provider="gody.vn", venue_name="Venue",
                            address=None, aggregate_rating=4.0, rating_scale=5.0, total_review_count=1,
                            reviews=[dict(text="Review", rating=None, rating_scale=5.0, published_at=None)])
                catalog.add_review_page(page, "hcm_dalat")
                page["reviews"][0]["published_at"] = "2020-01-01"
                catalog.add_review_page(page, "hcm_dalat")
                self.assertEqual(catalog.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
                self.assertEqual(catalog.db.execute("SELECT published_at FROM reviews").fetchone()[0], "2020-01-01")
            finally:
                catalog.db.close()

    def test_geography_and_transport_do_not_count_as_supported_venues(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Catalog(Path(folder))
            try:
                with catalog.db:
                    for identity, types in [("geo", ["political", "sublocality", "sublocality_level_1"]),
                                            ("agency", ["travel_agency"]), ("food", ["restaurant"])]:
                        catalog.db.execute("INSERT INTO places(place_id,name,types_json) VALUES (?,?,?)", (identity, identity, json.dumps(types)))
                catalog.assess_places()
                statuses = dict(catalog.db.execute("SELECT place_id,status FROM place_assessments"))
                self.assertEqual(statuses["geo"], "excluded_geographic_entity")
                self.assertEqual(statuses["agency"], "excluded_transport_service")
                self.assertEqual(statuses["food"], "venue_type_supported")
            finally:
                catalog.db.close()

    def test_source_url_canonicalization_excludes_directories(self):
        self.assertIsNone(canonical_provider_url("https://www.foody.vn/vung-tau/o-dau"))
        self.assertIsNone(canonical_provider_url("https://gody.vn/chau-a/viet-nam/lam-dong"))
        self.assertEqual(canonical_provider_url("https://www.foody.vn/(A(test))/vung-tau/venue/album-anh?x=1"),
                         "https://www.foody.vn/vung-tau/venue/binh-luan")
