"""Deterministic parsing and ranking rules for saved Amazon search HTML.

This module deliberately parses saved source only.  Network acquisition lives in
``search_rank_worker`` so the ranking rules can be replayed and audited without
making a request.
"""
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import re
from typing import Any, Iterable


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
SPONSORED_RE = re.compile(r"\b(sponsored|advertisement|adholder)\b", re.IGNORECASE)


class _SearchCardParser(HTMLParser):
    """Extract Amazon result-card boundaries without treating arbitrary links as cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, Any]] = []
        self._stack: list[str] = []
        self._active: dict[str, Any] | None = None
        self._active_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        is_card = (
            tag.lower() == "div"
            and "s-search-result" in values.get("data-component-type", "").lower()
            and bool(ASIN_RE.fullmatch(values.get("data-asin", "")))
        )
        self._stack.append(tag.lower())
        if self._active is None and is_card:
            self._active = {
                "asin": values["data-asin"].upper(),
                "attributes": values,
                "text": [],
                "classes": [values.get("class", "")],
            }
            self._active_depth = len(self._stack)
        elif self._active is not None:
            self._active["classes"].append(values.get("class", ""))
            for key in ("aria-label", "data-component-type", "data-cy"):
                if values.get(key):
                    self._active["text"].append(values[key])

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._active["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._active is not None and self._active_depth == len(self._stack) and tag.lower() == "div":
            self.cards.append(self._active)
            self._active = None
            self._active_depth = None
        if self._stack:
            self._stack.pop()


def parse_search_cards(html: str, *, page: int) -> list[dict[str, Any]]:
    """Return ordered cards with an explicit ad/organic/unknown classification.

    ``card_index`` counts all cards on the page. ``organic_position`` counts
    every non-ad card, including duplicates; ``organic_unique_position`` is the
    first-seen ASIN projection.  A card that cannot be classified remains
    ``unknown`` and is never promoted to an organic rank.
    """
    if page < 1:
        raise ValueError("page must be positive")
    parser = _SearchCardParser()
    parser.feed(html)
    parser.close()
    organic_count = 0
    ad_count = 0
    unique_organic_count = 0
    seen_organic: set[str] = set()
    output: list[dict[str, Any]] = []
    for card_index, item in enumerate(parser.cards, start=1):
        markers = " ".join([*item["classes"], *item["text"], item["attributes"].get("data-ad-details", "")])
        marker = SPONSORED_RE.search(unescape(markers))
        # Amazon's documented search-result component is organic unless an ad
        # marker is present.  A malformed component with no layout marker is
        # deliberately unknown rather than guessed as organic.
        component = item["attributes"].get("data-component-type", "").lower()
        placement = "ad" if marker else "organic" if component == "s-search-result" else "unknown"
        asin = item["asin"]
        organic_position = None
        organic_unique_position = None
        ad_position = None
        if placement == "organic":
            organic_count += 1
            organic_position = organic_count
            if asin not in seen_organic:
                seen_organic.add(asin)
                unique_organic_count += 1
                organic_unique_position = unique_organic_count
        elif placement == "ad":
            ad_count += 1
            ad_position = ad_count
        output.append({
            "page": page,
            "card_index": card_index,
            "asin": asin,
            "placement": placement,
            "organic_position": organic_position,
            "organic_unique_position": organic_unique_position,
            "ad_position": ad_position,
            "layout_context": {
                "component_type": component or None,
                "classification_marker": marker.group(0).lower() if marker else None,
                "duplicate_organic_asin": placement == "organic" and organic_unique_position is None,
            },
        })
    return output


def project_target_positions(cards: Iterable[dict[str, Any]], target_asins: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Choose the first auditable occurrence per placement for each target.

    Natural and sponsored positions are independent.  A duplicate card is kept
    in raw cards but cannot overwrite an earlier occurrence for the same target.
    """
    targets = {str(asin).upper() for asin in target_asins if ASIN_RE.fullmatch(str(asin))}
    result = {asin: {"observed_asin": asin, "organic_position": None, "ad_position": None, "page": None, "card_index": None, "matches": []} for asin in targets}
    for card in cards:
        asin = str(card.get("asin") or "").upper()
        if asin not in result:
            continue
        value = result[asin]
        value["matches"].append(card)
        if card.get("placement") == "organic" and value["organic_position"] is None:
            value.update({"organic_position": card.get("organic_position"), "page": card.get("page"), "card_index": card.get("card_index")})
        if card.get("placement") == "ad" and value["ad_position"] is None:
            value["ad_position"] = card.get("ad_position")
    return result


def rank_cards_across_pages(pages: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Apply global ranks across the contiguous page prefix beginning at page 1.

    A missing earlier page means later positions cannot be honest global ranks;
    their positions are set to ``None`` while retaining their raw card evidence.
    """
    organic = ad = unique_organic = 0
    seen_organic: set[str] = set()
    contiguous = True
    expected_page = 1
    output: list[dict[str, Any]] = []
    for page in sorted(pages):
        if page != expected_page:
            contiguous = False
        for original in pages[page]:
            card = dict(original)
            if not contiguous:
                card["organic_position"] = None
                card["organic_unique_position"] = None
                card["ad_position"] = None
            elif card.get("placement") == "organic":
                organic += 1
                card["organic_position"] = organic
                asin = str(card.get("asin") or "").upper()
                if asin in seen_organic:
                    card["organic_unique_position"] = None
                else:
                    seen_organic.add(asin); unique_organic += 1
                    card["organic_unique_position"] = unique_organic
            elif card.get("placement") == "ad":
                ad += 1
                card["ad_position"] = ad
            output.append(card)
        expected_page += 1
    return output
