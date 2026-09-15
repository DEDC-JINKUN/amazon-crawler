from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class Response:
    def __init__(self, body, *, status=200, content_type="application/json"):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self): return self.body


def test_woot_adapter_normalizes_reviews_with_stable_source_separated_ids():
    from woot_review_adapter import WootReviewAdapter

    payload = {"Reviews": [{
        "Author": "Alice", "Title": "Works", "Text": "Useful product",
        "OverallRating": 5, "OriginDescription": "Reviewed in the United States on September 1, 2026",
        "IsVerifiedPurchase": True,
    }], "PagingNext": "next-token"}
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return Response(json.dumps(payload))

    adapter = WootReviewAdapter(user_agent="Mozilla/5.0 Agent/review-worker", open_request=open_request)
    first = adapter.fetch_page("B0BLCBRBVZ", star=5, sort=1)
    second = adapter.fetch_page("B0BLCBRBVZ", star=5, sort=1)

    assert first == second
    assert first["next_token"] == "next-token"
    review = first["reviews"][0]
    assert review["review_id"].startswith("woot-")
    assert review["source_type"] == "woot_ajax"
    assert review["unique_key"] == f"US|B0BLCBRBVZ|woot_ajax|{review['review_id']}"
    assert review["verified"] is True and review["rating"] == "5"
    assert requests[0][0].get_header("User-agent").endswith("Agent/review-worker")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response("<html>Woot: Forbidden</html>", status=403, content_type="text/html"), "WootUpstreamBlocked"),
        (Response("<html>not json</html>", status=200, content_type="text/html"), "WootInvalidResponse"),
        (Response('{"unexpected":[]}', status=200), "WootInvalidResponse"),
    ],
)
def test_woot_adapter_fails_closed_instead_of_returning_false_zero_reviews(response, error):
    import woot_review_adapter as module

    adapter = module.WootReviewAdapter(
        user_agent="Mozilla/5.0 Agent/review-worker",
        open_request=lambda *_args, **_kwargs: response,
    )
    with pytest.raises(getattr(module, error)):
        adapter.fetch_page("B0BLCBRBVZ")


def test_woot_adapter_rejects_opaque_user_agent():
    from woot_review_adapter import WootReviewAdapter

    with pytest.raises(ValueError, match="Agent/<name>"):
        WootReviewAdapter(user_agent="Mozilla/5.0 Chrome/120")
