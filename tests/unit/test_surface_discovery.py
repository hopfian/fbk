"""Offline surface-discovery tests: search, marketplace, notifications.

Every test replays the REAL discovery path (page GET -> preload registry
harvest -> verbatim-variable replay -> typed parse) against captured wire
data from assets/ (docs/15 §P2-2, §P3-5). No network, no cookies.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from fakes import ASSETS, StubSession

import constants as C
from auth.bootstrap import extract_preload_registry
from domain.common import Notification, NotificationCount, SearchResult
from surfaces.marketplace import (
    DEFAULT_MARKETPLACE_VARIABLES,
    MARKETPLACE_BROWSE_DOC_ID,
    MARKETPLACE_BROWSE_QUERY_NAME,
    MarketplaceService,
)
from surfaces.notifications import (
    DEFAULT_NOTIFICATIONS_VARIABLES,
    LIST_DOC_ID,
    LIST_QUERY_NAME,
    NotificationsService,
)
from surfaces.search import (
    DEFAULT_SEARCH_VARIABLES,
    SEARCH_FALLBACK_DOC_ID,
    SEARCH_QUERY_NAME,
    SearchService,
)

# --------------------------------------------------------------------- helpers


def load_asset(name: str) -> Any:
    """Load an assets/ JSON file (same contract as tests/conftest.py)."""
    path = ASSETS / name
    if not path.is_file():
        raise FileNotFoundError(f"missing captured fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def search_fixture_payload() -> dict[str, Any]:
    """The real captured search response (docs/15 §P3-5).

    The capture is a 2MB slice truncated mid-stream; the complete ``data``
    object sits before the ``extensions`` object, so the payload is repaired
    by cutting there. If a future capture parses whole, it is used as-is.
    """
    raw = (ASSETS / "search_response_SearchCometResultsInitialResultsQuery.json").read_text(
        encoding="utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        idx = raw.find('\n "extensions":')
        assert idx > 0, "unexpected search fixture layout"
        cut = raw[:idx].rstrip()
        if cut.endswith(","):
            cut = cut[:-1]
        return json.loads(cut + "\n}")


def search_fixture_html() -> str:
    """The REAL preload registration sliced out of the captured search page."""
    raw = (ASSETS / "search_page_sample.html").read_text(
        encoding="utf-8", errors="replace")
    anchor = re.search(
        r'\{"actorID":"\d+",'
        r'"preloaderID":"adp_SearchCometResultsInitialResultsQueryRelayPreloader_[0-9a-f]+",'
        r'"queryID":"29118914987711900","variables":', raw)
    assert anchor is not None, "search preload anchor missing from captured page"
    return raw[anchor.start(): anchor.end() + 6000]


def marketplace_fixture_html() -> str:
    """A minimal page embedding a crafted preload block built from the baked
    marketplace constants (same registration format the live page uses)."""
    variables = copy.deepcopy(DEFAULT_MARKETPLACE_VARIABLES)
    return (
        '<html><body><script>'
        '{"actorID":"12345678901234",'
        '"preloaderID":"adp_MarketplaceCometBrowseFeedLightContainerQueryRelayPreloader_0123456789abcdef",'
        '"queryID":"' + MARKETPLACE_BROWSE_DOC_ID + '",'
        '"variables":' + json.dumps(variables, separators=(",", ":")) + ","
        '"queryName":"' + MARKETPLACE_BROWSE_QUERY_NAME + '"}'
        '</script></body></html>'
    )


# ---------------------------------------------------------------------- search


def test_search_from_real_page_preload(monkeypatch) -> None:
    """Full replay of the live search path against the captured page + payload."""
    session = StubSession({SEARCH_QUERY_NAME: search_fixture_payload()})
    service = SearchService(session)
    monkeypatch.setattr(service, "_fetch", lambda url: search_fixture_html())

    response = service.search("python programming", limit=20)

    assert response.query == "python programming"
    assert response.raw_size > 100_000
    assert len(response.results) >= 1
    assert all(isinstance(r, SearchResult) for r in response.results)
    assert all(r.name for r in response.results)
    # dedup: no (typename, id) pair twice
    keys = {(r.typename, r.id) for r in response.results}
    assert len(keys) == len(response.results)
    assert len(response.results) <= 20

    friendly, doc_id, variables = session.graphql.calls[0]
    assert friendly == SEARCH_QUERY_NAME
    assert doc_id == "29118914987711900"  # live-verified (docs/15 §P3-5)
    # the replayed variables are the page's VERBATIM preloaded variables
    entry = next(e for e in extract_preload_registry(search_fixture_html())
                 if e.query_name == SEARCH_QUERY_NAME)
    assert variables == entry.variables
    assert variables["args"]["text"] == "python programming"


def test_search_fallback_default_variables(monkeypatch) -> None:
    """No preload in the page -> module defaults with args.text = the query."""
    payload = {"data": {"serpResponse": {"results": {"edges": [
        {"node": {"node": {"__typename": "User", "name": "Ada Lovelace",
                           "id": "615999", "url": "https://www.facebook.com/ada"}},
         "debug_overlay_info": []}]}}}}
    session = StubSession({SEARCH_QUERY_NAME: payload})
    service = SearchService(session)
    monkeypatch.setattr(service, "_fetch", lambda url: "<html>logged-out</html>")

    response = service.search("ada lovelace")

    assert [r.name for r in response.results] == ["Ada Lovelace"]
    assert response.results[0].typename == "User"
    _, doc_id, variables = session.graphql.calls[0]
    assert doc_id == SEARCH_FALLBACK_DOC_ID
    expected = copy.deepcopy(DEFAULT_SEARCH_VARIABLES)
    expected["args"]["text"] = "ada lovelace"
    assert variables == expected


# --------------------------------------------------------------- notifications


def test_notifications_badge() -> None:
    """The VERIFIED badge query: doc_id, variables, and typed parse."""
    session = StubSession({"CometNotificationsBadgeCountQuery":
                           {"data": {"viewer": {"notifications_unseen_count": 3}}}})
    service = NotificationsService(session)

    count = service.badge()

    assert isinstance(count, NotificationCount)
    assert count.unseen == 3
    friendly, doc_id, variables = session.graphql.calls[0]
    assert friendly == "CometNotificationsBadgeCountQuery"
    assert doc_id == "9714526941947209"
    assert variables == {"environment": "MAIN_SURFACE"}
    assert doc_id == C.KNOWN_MUTATIONS["CometNotificationsBadgeCountQuery"]


def test_notifications_list_from_live_fixture() -> None:
    """The dropdown query against the sanitized live fixture: typed rows."""
    payload = load_asset("notifications_fixture.json")
    # the live capture had zero unseen notifications (badge 0), so graft a
    # synthetic UNSEEN_AND_UNREAD row to exercise the unseen-state mapping.
    unseen_row = copy.deepcopy(
        next(e for e in payload["data"]["viewer"]["notifications_page"]["edges"]
             if e["node"].get("__typename") == "NotifPageNotificationRow"))
    unseen_row["node"]["notif"]["seen_state"] = "UNSEEN_AND_UNREAD"
    payload["data"]["viewer"]["notifications_page"]["edges"].insert(0, unseen_row)

    session = StubSession({LIST_QUERY_NAME: payload})
    service = NotificationsService(session)

    notifications = service.list(limit=20)

    assert len(notifications) >= 1
    assert all(isinstance(n, Notification) for n in notifications)
    assert all(n.id for n in notifications)
    assert all(n.body == "fixture" for n in notifications)  # sanitized live text
    assert notifications[0].unseen is True                  # UNSEEN_AND_UNREAD row
    assert all(not n.unseen for n in notifications[1:])     # SEEN_* rows
    friendly, doc_id, variables = session.graphql.calls[0]
    assert friendly == LIST_QUERY_NAME
    assert doc_id == LIST_DOC_ID == "28272355145709625"
    assert variables == {**DEFAULT_NOTIFICATIONS_VARIABLES, "count": 20}


# ----------------------------------------------------------------- marketplace


def test_marketplace_browse_crafted_preload(monkeypatch) -> None:
    """Browse through a crafted preload block; overrides merge over defaults."""
    session = StubSession({MARKETPLACE_BROWSE_QUERY_NAME:
                           load_asset("marketplace_fixture.json")})
    service = MarketplaceService(session)
    monkeypatch.setattr(service, "_fetch", lambda url: marketplace_fixture_html())

    listings = service.browse(variables={"count": 2})

    assert len(listings) >= 1
    assert all(isinstance(item, SearchResult) for item in listings)
    assert all(item.typename == "GroupCommerceProductItem" for item in listings)
    assert all(item.id for item in listings)
    assert all(item.url and item.url.startswith(
        "https://www.facebook.com/marketplace/item/") for item in listings)
    assert all(item.name == "fixture" for item in listings)  # sanitized live title
    # prices survive sanitization as the snippet (formatted_price.text -> 'fixture')
    assert all(item.snippet == "fixture" for item in listings)

    friendly, doc_id, variables = session.graphql.calls[0]
    assert friendly == MARKETPLACE_BROWSE_QUERY_NAME
    assert doc_id == MARKETPLACE_BROWSE_DOC_ID == "28053535904279798"
    assert variables == {**DEFAULT_MARKETPLACE_VARIABLES, "count": 2}


def test_marketplace_browse_fallback_defaults(monkeypatch) -> None:
    """No preload in the page -> baked live constants drive the call."""
    session = StubSession({MARKETPLACE_BROWSE_QUERY_NAME:
                           load_asset("marketplace_fixture.json")})
    service = MarketplaceService(session)
    monkeypatch.setattr(service, "_fetch", lambda url: "<html></html>")

    listings = service.browse(limit=5)

    assert len(listings) >= 1
    _, doc_id, variables = session.graphql.calls[0]
    assert doc_id == MARKETPLACE_BROWSE_DOC_ID
    assert variables == {**DEFAULT_MARKETPLACE_VARIABLES, "count": 5}


# --------------------------------------------------------------------- registry


def test_registry_doc_ids() -> None:
    """The doc_ids the services use are the real harvested registry values."""
    registry = StubSession().registry  # the real assets/ registry (doc_id_registry_v2)
    # registry-harvested notifications pairs (ground truth, docs/15):
    assert registry.doc_id("CometNotificationsBadgeCountQuery") == "9714526941947209"
    assert registry.doc_id("CometNotificationsDropdownQuery") == "28272355145709625"
    # registry-harvested marketplace pairs (ground truth):
    assert registry.doc_id("MarketplaceSearchAddressDataSourceQuery") == "9660140454040174"
    assert "CometMarketplaceFeedUnitVisibilityMutation" in registry
    assert "MarketplaceCometBrowseFeedLightPaginationQuery" in registry
    # the search + marketplace browse doc_ids were discovered via the LIVE page
    # preload registries, not the bundle harvest — assert the search pair
    # against the real captured search page (docs/15 §P3-5):
    page = (ASSETS / "search_page_sample.html").read_text(encoding="utf-8", errors="replace")
    anchor = re.search(
        r'"queryID":"29118914987711900",'
        r'"variables":\{"count":5,"allow_streaming":false,"args":\{', page)
    assert anchor is not None
    assert '"queryName":"SearchCometResultsInitialResultsQuery"' in page
