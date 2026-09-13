from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_rank_parser import parse_search_cards, project_target_positions, rank_cards_across_pages


def test_mixed_ads_duplicates_and_multi_target_have_reproducible_positions():
    html = (ROOT / "tests" / "fixtures" / "search_rank_mixed.html").read_text(encoding="utf-8")
    cards = parse_search_cards(html, page=1)
    assert [(card["asin"], card["placement"], card["card_index"]) for card in cards] == [
        ("B000000001", "organic", 1), ("B000000002", "ad", 2), ("B000000001", "organic", 3), ("B000000003", "organic", 4),
    ]
    assert [card["organic_position"] for card in cards] == [1, None, 2, 3]
    assert [card["organic_unique_position"] for card in cards] == [1, None, None, 2]
    positions = project_target_positions(cards, ["B000000001", "B000000002", "B000000003"])
    assert positions["B000000001"]["organic_position"] == 1
    assert positions["B000000002"]["organic_position"] is None
    assert positions["B000000002"]["ad_position"] == 1
    assert positions["B000000003"]["organic_position"] == 3


def test_unrecognized_result_layout_is_not_promoted_to_an_organic_position():
    html = '<div data-component-type="different-layout" data-asin="B000000001">result</div>'
    assert parse_search_cards(html, page=1) == []


def test_contiguous_pages_accumulate_global_ranks_but_gaps_do_not_claim_them():
    first = parse_search_cards((ROOT / "tests" / "fixtures" / "search_rank_mixed.html").read_text(encoding="utf-8"), page=1)
    second = parse_search_cards((ROOT / "tests" / "fixtures" / "search_rank_page_2.html").read_text(encoding="utf-8"), page=2)
    ranked = rank_cards_across_pages({1: first, 2: second})
    assert ranked[-1]["organic_position"] == 4
    gapped = rank_cards_across_pages({2: second})
    assert gapped[0]["organic_position"] is None
