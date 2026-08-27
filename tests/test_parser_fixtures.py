#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ParserFixtureTests(unittest.TestCase):
    def test_product_fields_media_and_aplus(self):
        worker = load("amazon_us_worker")
        html = (FIXTURES / "product_unavailable_video_aplus.html").read_text(encoding="utf-8")
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["asin"], "B00RCPDCQU")
        self.assertEqual(result["availability"], "Currently unavailable")
        self.assertEqual(result["title"], "Fixture product title")
        self.assertEqual(result["brand"], "Fixture Brand")
        self.assertEqual(result["reported_ratings"], "1234")
        self.assertEqual(result["bullets"], ["First bullet", "Second bullet"])
        self.assertTrue(result["aplus_present"])
        self.assertTrue(any(item["media_type"] == "video" for item in result["media"]))
        self.assertTrue(all(item["placement"] != "excluded" for item in result["media"]))
        self.assertTrue(any(item["image_url"].endswith("aplus.jpg") for item in result["content_modules"]))
        self.assertEqual(result["specs"]["Color"], "Blue")
        self.assertEqual(result["specs"]["Material"], "Cotton")

    def test_product_videos_and_review_link(self):
        worker = load("amazon_us_worker")
        html = (FIXTURES / "product_video_player.html").read_text(encoding="utf-8")
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDI50")
        self.assertEqual(result["asin"], "B00RCPDI50")
        self.assertTrue(any(item["media_type"] == "video" for item in result["media"]))
        self.assertEqual(result["review_link"], "https://www.amazon.com/product-reviews/B00RCPDI50")

    def test_review_anchor_is_not_pagination_link_and_count_only_is_review_count(self):
        worker = load("amazon_us_worker")
        html = """
        <html><body>
          <input id="ASIN" value="B00RCPDCQU">
          <link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU">
          <div id="acrPopover">4.0 out of 5 stars</div>
          <a id="acrCustomerReviewLink" href="#averageCustomerReviewsAnchor"><span>(17)</span></a>
          <div id="averageCustomerReviewsAnchor"><span>Reviews</span></div>
          <script>captcha should not be visible</script><style>.x{}</style><noscript>captcha</noscript>
          <div id="availability"><span>Currently unavailable</span><script>tracking text</script></div>
        </body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["review_link"], "")
        self.assertEqual(result["review_section_anchor"], "#averageCustomerReviewsAnchor")
        self.assertEqual(result["reported_rating_count"], "")
        self.assertEqual(result["reported_review_count"], "17")
        self.assertEqual(result["review_count_source"], "review_count_only")
        self.assertEqual(result["availability"], "Currently unavailable")
        self.assertNotIn("captcha", worker.visible_html_text(html).lower())

        cross_asin = html.replace(
            'href="#averageCustomerReviewsAnchor"',
            'href="https://www.amazon.com/product-reviews/B0CRQVDJJ7/ref=x"',
        )
        cross_result = worker.parse_product_html(cross_asin, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(cross_result["review_link"], "")

    def test_specs_li_div_and_colon_fallback(self):
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="product-information">
          <ul><li data-label="Color">Blue</li><li aria-label="Material">Cotton</li></ul>
          <div><span>Weight</span><span>1 kg</span></div>
          <div>Country of origin: USA</div>
        </div></body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["specs"], {
            "Color": "Blue", "Material": "Cotton", "Weight": "1 kg", "Country of origin": "USA",
        })

    def test_media_filters_pixels_recommendations_and_unclassified_images(self):
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="imageBlock"><img src="/images/main.jpg"><img src="/images/grey-pixel.gif"><img src="/images/pixel.gif?1x1"></div>
        <div class="recommendations"><img src="/images/recommended.jpg"></div>
        <div><img src="/images/unclassified.jpg"></div>
        <div id="aplus"><img src="/images/aplus.jpg"></div>
        </body></html>
        """
        media = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")["media"]
        self.assertEqual([item["asset_url"] for item in media], [
            "https://www.amazon.com/images/main.jpg", "https://www.amazon.com/images/aplus.jpg",
        ])
        self.assertEqual([item["placement"] for item in media], ["gallery", "aplus"])
        self.assertEqual([item["entry_type"] for item in media], ["image", "image"])

    def test_review_page_and_next_link(self):
        worker = load("amazon_us_worker")
        html = (FIXTURES / "reviews_page_1.html").read_text(encoding="utf-8")
        records, next_url = worker.parse_reviews_html(html, 1, "https://www.amazon.com/product-reviews/B00RCPDCQU")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "Useful item")
        self.assertEqual(records[0]["body"], "Fixture review body")
        self.assertEqual(records[0]["review_date"], "Reviewed in the United States on January 1, 2026")
        self.assertTrue(records[0]["verified"])
        self.assertEqual(json.loads(records[0]["review_images_json"]), ["https://images.example/review.jpg"])
        self.assertEqual(next_url, "https://www.amazon.com/product-reviews/B00RCPDCQU?pageNumber=2")


if __name__ == "__main__":
    unittest.main()
