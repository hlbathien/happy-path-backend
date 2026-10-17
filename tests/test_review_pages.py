import unittest

from happy_path.core.review_pages import extract_review_page


class TestPublicReviewPages(unittest.TestCase):
    def test_foody_preserves_native_scale_and_excludes_author(self):
        html = '''<meta property="place:location:latitude" content="11.6779897">
        <meta property="place:location:longitude" content="109.1828853">
        <div class="main-info-title"><h1>Hang Rái</h1></div>
        <div class="ratings-boxes-points">8.0 điểm</div><div class="microsite-review-count">14</div>
        <div class="review-list"><div class="review-item"><a>Private Reviewer</a>
        <div class="review-points">8.4</div><span class="ru-time">29/12/2018 0:36</span>
        <div class="rd-des"><span>Cảnh đẹp SĐT0909123456</span></div></div></div>'''
        page = extract_review_page(html, "https://www.foody.vn/ninh-thuan/hang-rai/binh-luan")
        self.assertEqual(page["aggregate_rating"], 8.0)
        self.assertEqual(page["rating_scale"], 10.0)
        self.assertEqual(page["reviews"][0]["rating"], 8.4)
        self.assertEqual(page["reviews"][0]["published_at"], "2018-12-29")
        self.assertEqual((page["lat"], page["lng"]), (11.6779897, 109.1828853))
        self.assertNotIn("Private Reviewer", str(page))
        self.assertNotIn("0909123456", str(page))

    def test_gody_never_infers_stars_from_review_prose(self):
        html = '''<h1>Thác Dambri</h1><div id="comment-id-1">
        <span class="fs-12">2020-03-06T06:31:16.000000Z</span>
        <p class="ow-break-word">Tôi cho khung cảnh 5 sao nhưng dịch vụ kém.</p></div>
        <div id="comment-id-2"><span class="fs-12">2020-06-06 11:37:15</span>
        <div class="js-destination-rating-ajax" data-avg="2"></div></div>'''
        page = extract_review_page(html, "https://gody.vn/chau-a/viet-nam/lam-dong/thac-dambri")
        self.assertIsNone(page["reviews"][0]["rating"])
        self.assertEqual(page["reviews"][0]["published_at"], "2020-03-06")
        self.assertEqual(page["reviews"][1]["rating"], 2)
        self.assertEqual(page["reviews"][1]["text"], "")
        self.assertEqual(page["reviews"][1]["published_at"], "2020-06-06")

    def test_invalid_dates_and_out_of_scale_scores_remain_missing(self):
        html = '''<div class="main-info-title"><h1>Venue</h1></div><div class="review-list">
        <div class="review-item"><div class="review-points">99</div><span class="ru-time">31/2/2026</span>
        <div class="rd-des"><span>Review</span></div></div></div>'''
        review = extract_review_page(html, "https://www.foody.vn/a/b/binh-luan")["reviews"][0]
        self.assertIsNone(review["rating"])
        self.assertIsNone(review["published_at"])
