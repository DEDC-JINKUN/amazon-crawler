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
    def test_product_identity_extracts_canonical_parent_and_explicit_child_membership(self):
        worker = load("amazon_us_worker")
        html = """
        <html><head><link rel="canonical" href="https://www.amazon.com/example/dp/B0DKHT1KY7"></head><body>
          <input id="ASIN" value="B07VK5XSRP"><span id="productTitle">Child variation</span>
          <script>
            var request = "parentAsin=B0DKHT1KY7&landingAsin=B07VK5XSRP";
            var twister = {
              "currentAsin": "B07VK5XSRP",
              "dimensionValuesDisplayData": {"B07VK5XSRP": ["Black"]},
              "colorToAsin": {"Black": {"asin": "B07VK5XSRP"}}
            };
          </script>
        </body></html>
        """

        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B07VK5XSRP")

        self.assertEqual(result["asin"], "B07VK5XSRP")
        self.assertEqual(result["parent_asin"], "B0DKHT1KY7")
        self.assertEqual(result["identity_child_asins"], ["B07VK5XSRP"])

    def test_brand_normalization_removes_store_call_to_action(self):
        worker = load("amazon_us_worker")
        self.assertEqual(worker._normalize_brand("Visit the Eyourlife Store"), "Eyourlife")
        self.assertEqual(worker._normalize_brand("Fixture Brand"), "Fixture Brand")

    def test_buy_box_facts_extract_coupon_and_delivery_without_losing_raw_text(self):
        worker = load("amazon_us_worker")
        html = '<div id="desktop_buybox">Sold by Example Store Save $5.00 with coupon FREE delivery Tuesday</div>'
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["buy_box"]["coupon"], "Save $5.00")
        self.assertIn("FREE delivery", result["buy_box"]["delivery"])
        self.assertEqual(result["buy_box"]["seller"], "Example Store")
        self.assertIn("Sold by Example Store", result["buy_box"]["text"])

    def test_price_normalization_collapses_visual_duplicates(self):
        worker = load("amazon_us_worker")
        self.assertEqual(worker._normalize_price("$23.99 $ 23 . 99"), "$23.99")
        self.assertEqual(worker._normalize_price("HKD235.11 HKD 235 . 11"), "HKD235.11")

    def test_description_does_not_absorb_following_modules_when_container_empty(self):
        worker = load("amazon_us_worker")
        html = '<div id="productDescription"><!-- empty --></div><div id="buybox">Buy Box text must not be description</div>'
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["product_description"], "")

    def test_product_fields_media_and_aplus(self):
        worker = load("amazon_us_worker")
        html = (FIXTURES / "product_unavailable_video_aplus.html").read_text(encoding="utf-8")
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["asin"], "B00RCPDCQU")
        self.assertEqual(result["availability"], "Currently unavailable")
        self.assertEqual(result["title"], "Fixture product title")
        self.assertEqual(result["brand"], "Fixture Brand")
        self.assertEqual(result["price"], "$19.99")
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

    def test_bsr_extracts_primary_rank_and_all_category_entries(self):
        # BSR 标准格式：详情区含多个类目排名，第一条是主类目排名
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="detailBullets">
          <ul>
            <li><span class="a-text-bold">Best Sellers Rank: </span>
                #1,234 in Home &amp; Kitchen (See Top 100 in Home &amp; Kitchen)
                #56 in Kitchen &amp; Dining Storage</li>
          </ul>
        </div>
        </body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["bsr_rank"], 1234)
        self.assertEqual(result["bsr_category"], "Home & Kitchen")
        self.assertEqual(result["bsr_entries"], [
            {"rank": 1234, "category": "Home & Kitchen"},
            {"rank": 56, "category": "Kitchen & Dining Storage"},
        ])

    def test_bsr_real_page_structure_with_span_wrapped_ranks(self):
        # 真实页面结构：排名文本放在子 span 里（标签与排名节点分开）
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="detailBullets">
          <ul>
            <li><span class="a-text-bold">Best Sellers Rank: </span>
                <span>#88 in Electronics (See Top 100 in Electronics)</span>
                <span>#3 in Portable Bluetooth Speakers</span></li>
          </ul>
        </div>
        </body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["bsr_rank"], 88)
        self.assertEqual(result["bsr_category"], "Electronics")
        self.assertEqual(result["bsr_entries"], [
            {"rank": 88, "category": "Electronics"},
            {"rank": 3, "category": "Portable Bluetooth Speakers"},
        ])

    def test_bsr_missing_leaves_fields_empty(self):
        # 页面没有 BSR 排名时：bsr_rank/bsr_category 为 None，条目为空列表
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="detailBullets"><ul><li>ASIN: B00RCPDCQU</li></ul></div>
        </body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertIsNone(result["bsr_rank"])
        self.assertIsNone(result["bsr_category"])
        self.assertEqual(result["bsr_entries"], [])

    def test_bsr_ignores_recommendation_blocks_outside_detail_area(self):
        # 推荐位里的排名不算商品 BSR：只扫详情区，推荐区的排名应被忽略
        worker = load("amazon_us_worker")
        html = """
        <html><body><input id="ASIN" value="B00RCPDCQU">
        <div id="detailBullets"><ul><li>Best Sellers Rank: #200 in Toys &amp; Games</li></ul></div>
        <div id="recommendations">#5 in Sponsored Results</div>
        </body></html>
        """
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(result["bsr_entries"], [{"rank": 200, "category": "Toys & Games"}])


if __name__ == "__main__":
    unittest.main()
