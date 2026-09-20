"""Offline tests for the READ + INTERACTION EXPANSION of the surfaces
(profile view, friends suggestions, marketplace search + item detail,
groups members, photos albums/photos, notifications mark-seen —
docs/02 §2.5/§2.9, docs/15 §P2-2/P2-3).

StubSession-based: the page-harvest reads replay a crafted SSR preload
block through the real extract_preload_registry (canned HTML via a
monkeypatched _fetch), assert id substitution / verbatim preload variables
into the replay, and walk canned payloads into typed rows. The mark-seen
test asserts the schema-decoded 2026-09 input shape of
CometNotificationsUpdateSeenStateMutation (LocalArguments environment +
input; the decoded MARK_ALL_SEEN commit carries NO client_mutation_id) and
the fresh id list harvested from the dropdown query. Command wiring is
exercised through the real argparse parsers with the session constructor
stubbed out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

from commands import friends as friends_cmd
from commands import groups as groups_cmd
from commands import marketplace as marketplace_cmd
from commands import notifications as notifications_cmd
from commands import photos as photos_cmd
from commands import profile as profile_cmd
from config import Config
from graphql.registry import DocIdRegistry
from surfaces.friends import (
    DEFAULT_FRIENDS_VARIABLES,
    ROOT_QUERY_NAME,
    FriendsService,
)
from surfaces.groups import (
    GROUP_MEMBERS_DOC_ID,
    GROUP_MEMBERS_QUERY,
    GroupsService,
)
from surfaces.marketplace import (
    DEFAULT_ITEM_VARIABLES,
    MARKETPLACE_ITEM_DOC_ID,
    MARKETPLACE_ITEM_MEDIA_QUERY_NAME,
    MARKETPLACE_ITEM_QUERY_NAME,
    MARKETPLACE_SEARCH_QUERY_NAME,
    MarketplaceService,
)
from surfaces.notifications import (
    LIST_QUERY_NAME,
    MARK_SEEN_DOC_ID,
    MARK_SEEN_MUTATION,
    MARK_SEEN_VARIABLES_TEMPLATE,
    MAX_WALK_PAGES,
    PAGINATION_DOC_ID,
    PAGINATION_QUERY_NAME,
    PAGINATION_VARIABLES_TEMPLATE,
    NotificationsService,
)
from surfaces.photos import (
    ALBUM_VIEW_DOC_ID,
    ALBUM_VIEW_QUERY,
    PHOTOS_SECTION_DOC_ID,
    PHOTOS_SECTION_QUERY,
    PhotosService,
)
from surfaces.profile import (
    DEFAULT_PROFILE_DOC_ID,
    DEFAULT_PROFILE_QUERY,
    ProfileService,
)

ACTOR_ID = "12345678901234"

# The live-probed wire ids (2026-09 ground truth).
PROFILE_LIST_VIEW_DOC_ID = "28117370721250101"  # ProfileCometTimelineListViewRootQuery
FRIENDS_ROOT_DOC_ID = "28351661531162960"  # FriendingCometRootContentQuery
NOTIFICATIONS_LIST_DOC_ID = "28272355145709625"  # CometNotificationsDropdownQuery
NOTIFICATIONS_BADGE_DOC_ID = "9714526941947209"  # CometNotificationsBadgeCountQuery
NOTIFICATIONS_PAGINATION_DOC_ID = "28762112060039555"  # CometNotificationsListPaginationQuery
# The marketplace search content query is NOT in the harvested registry —
# its doc_id rides the search-page preload (live-probed 2026-09).
MARKETPLACE_SEARCH_DOC_ID = "27517490627932547"
# The marketplace item-detail PDP container is likewise preload-harvested.
# Its media companion (MarketplacePDPC2CMediaViewerWithImagesQuery) IS
# registry-resolvable (PAIRS below).

PAIRS = {
    DEFAULT_PROFILE_QUERY: PROFILE_LIST_VIEW_DOC_ID,
    ROOT_QUERY_NAME: FRIENDS_ROOT_DOC_ID,
    LIST_QUERY_NAME: NOTIFICATIONS_LIST_DOC_ID,
    "CometNotificationsBadgeCountQuery": NOTIFICATIONS_BADGE_DOC_ID,
    PAGINATION_QUERY_NAME: NOTIFICATIONS_PAGINATION_DOC_ID,
    MARK_SEEN_MUTATION: MARK_SEEN_DOC_ID,
    "CometFeedsNotificationsUpdateSeenStateMutation": "24075449245374733",
    "FriendingCometFeedPYMKHScrollPaginationQuery": "23978308165110357",
    MARKETPLACE_ITEM_MEDIA_QUERY_NAME: "10059604367394414",
}

SAMPLE_PAGE_ID = "12345678901234568"


def stub(responses: dict) -> StubSession:
    return StubSession(responses, PAIRS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fbk-test")
    sub = parser.add_subparsers(dest="command", required=True)
    profile_cmd.register(sub)
    friends_cmd.register(sub)
    marketplace_cmd.register(sub)
    notifications_cmd.register(sub)
    groups_cmd.register(sub)
    photos_cmd.register(sub)
    return parser


# ------------------------------------------------------------------ fixtures
def _preload_html(query_name: str, doc_id: str, variables: dict) -> str:
    """A page embedding one SSR preloader registration in the exact wire
    format extract_preload_registry harvests (docs/15 §P2-2)."""
    return _preloads_html([(query_name, doc_id, variables)])


def _preloads_html(registrations: list[tuple[str, str, dict]]) -> str:
    """A page embedding several SSR preloader registrations (one <script>
    block per registration, exact wire format)."""
    blocks = []
    for query_name, doc_id, variables in registrations:
        registration = {
            "actorID": ACTOR_ID,
            "preloaderID": f"adp_{query_name}RelayPreloader_0123456789abcdef",
            "queryID": doc_id,
            "variables": variables,
            "queryName": query_name,
        }
        blocks.append(
            '<script type="application/json">'
            + json.dumps(registration, separators=(",", ":"))
            + "</script>"
        )
    return "<html>" + "".join(blocks) + "</html>"


def _profile_page_html(preload_user_id: str = SAMPLE_PAGE_ID) -> str:
    """A profile page SSR-registering the list-view query with the target's
    id riding the top-level userID variable (live-probed shape)."""
    return _preload_html(
        DEFAULT_PROFILE_QUERY,
        PROFILE_LIST_VIEW_DOC_ID,
        {
            "previousProfileId": None,
            "privacySelectorRenderLocation": "COMET_STREAM",
            "renderLocation": "timeline",
            "scale": 2,
            "userID": preload_user_id,
            "__relay_internal__pv__WebPixelRatiorelayprovider": 2,
        },
    )


def _highlight_unit(unit_id: str, text: str, url: str, feedback_id: str) -> dict:
    """One HighlightPostUnit node — the profile-timeline story spelling
    (live-probed 2026-09: the nested story carries feedback/message/url)."""
    return {
        "__typename": "HighlightPostUnit",
        "id": unit_id,
        "type": "POST",
        "if_has_active_content_for_viewer": {
            "__typename": "HighlightPostUnit",
            "story": {
                "id": unit_id,
                "url": url,
                "actors": [{"__typename": "User", "id": SAMPLE_PAGE_ID, "name": "Sample Page"}],
                "feedback": {
                    "__typename": "Feedback",
                    "id": feedback_id,
                    "reaction_count": {"count": 5},
                    "comment_count": {"count": 2},
                },
                "comet_sections": {
                    "content": {
                        "story": {
                            "message": {"text": {"__typename": "TextWithEntities", "text": text}}
                        }
                    }
                },
            },
        },
    }


PROFILE_VIEW_PAYLOAD = {
    "data": {
        "user": {
            "id": SAMPLE_PAGE_ID,
            "timeline_list": {
                "edges": [
                    {
                        "node": _highlight_unit(
                            "1532996765541719",
                            "profile post one",
                            "https://www.facebook.com/reel/1856023965378356/",
                            "ZmVlZGJhY2s6MTUzMjk5Njc2NTU0MTcxOQ==",
                        )
                    },
                    {
                        "node": _highlight_unit(
                            "1532311112276951",
                            "profile post two",
                            "https://www.facebook.com/reel/904876085813708/",
                            "ZmVlZGJhY2s6MTUzMjMxMTExMjc2NzU5MQ==",
                        )
                    },
                ],
                "page_info": {
                    "__typename": "PageInfo",
                    "end_cursor": "PROFILECURSOR",
                    "has_next_page": True,
                },
            },
        },
    },
}


def _search_page_html() -> str:
    """The marketplace search page SSR-registration, crafted from the
    live probe of /marketplace/search/?query=laptop (2026-09): the query
    term rides inside the verbatim variables."""
    return _preload_html(
        MARKETPLACE_SEARCH_QUERY_NAME,
        MARKETPLACE_SEARCH_DOC_ID,
        {
            "buyLocation": {"latitude": 24.8833, "longitude": 90.7167},
            "contextual_data": None,
            "count": 24,
            "cursor": None,
            "params": {"bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": "laptop"}},
            "savedSearchID": None,
            "savedSearchQuery": "laptop",
            "scale": 2,
            "topicPageParams": {"location_id": "12345678901234571", "url": None},
        },
    )


def _product_item(item_id: str, title: str, formatted: str) -> dict:
    """One ProductItem node — the search-feed listing spelling
    (live-probed 2026-09: title, formatted price, numeric id; the URL is
    assembled from the item id, the node carries no url field)."""
    return {
        "__typename": "ProductItem",
        "id": item_id,
        "listing_price": {"formatted_amount": formatted, "amount": "15000.00"},
        "marketplace_listing_title": title,
        "custom_title": None,
    }


SEARCH_PAYLOAD = {
    "data": {
        "marketplace": {
            "marketplace_search": {
                "feed_units": {
                    "edges": [
                        {
                            "node": {
                                "listing": _product_item(
                                    "28295742886732939", "Laptop+", "BDT15,000"
                                )
                            }
                        },
                        {
                            "node": {
                                "listing": _product_item(
                                    "11111111111111111", "Gaming Laptop", "BDT45,000"
                                )
                            }
                        },
                    ],
                    "page_info": {"end_cursor": None, "has_next_page": True},
                }
            }
        },
    },
}


def _user_node(uid: str, name: str, **extra) -> dict:
    node = {"__typename": "User", "id": uid, "name": name}
    node.update(extra)
    return node


def _root_payload(pymk: list[dict]) -> dict:
    """A canned FriendingCometRootContentQuery response carrying pymk_grid
    rows (the live selection set: User nodes with name/id + profile photo)."""
    return {
        "data": {
            "viewer": {
                "pymk_grid": {
                    "tracking_signature": None,
                    "edges": [{"node": node} for node in pymk],
                    "page_info": {"has_next_page": False, "end_cursor": ""},
                },
            }
        }
    }


def _notif_row(notif_id: str, seen_state: str = "UNSEEN_AND_UNREAD") -> dict:
    """One dropdown row (live shape: NotifPageNotificationRow + notif block
    with the base64 global id)."""
    return {
        "node": {
            "__typename": "NotifPageNotificationRow",
            "notif": {"id": notif_id, "seen_state": seen_state, "body": {"text": "x"}},
        }
    }


def _last_call(session: StubSession) -> tuple[str, str, dict]:
    assert session.graphql.calls, "service made no client call"
    return session.graphql.calls[-1]


# ------------------------------------------------------------------ registry
class TestRegistryPresence:
    """Pins which expansion queries are registry-resolved and which ride
    their page preloads only (live ground truth)."""

    def test_every_new_doc_id_is_harvested(self):
        """Every registry-resolved doc_id resolves in the real asset registry.
        (The marketplace search query id is preload-harvested, not
        registry-resolved — it has no entry by design.)"""
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        for name, doc_id in PAIRS.items():
            assert registry.doc_id(name) == doc_id

    def test_stub_registry_matches_ground_truth(self):
        session = stub({})
        for name, doc_id in PAIRS.items():
            assert session.registry.doc_id(name) == doc_id

    def test_marketplace_search_query_is_preload_harvested(self):
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        assert MARKETPLACE_SEARCH_QUERY_NAME not in registry

    def test_new_page_harvested_queries_are_preload_harvested(self):
        """The 2026-09 read-expansion queries whose doc_ids ride their page
        preloads only (live ground truth): the marketplace item PDP
        container, the groups members root, and both photos queries."""
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        assert MARKETPLACE_ITEM_QUERY_NAME not in registry
        assert GROUP_MEMBERS_QUERY not in registry
        assert PHOTOS_SECTION_QUERY not in registry
        assert ALBUM_VIEW_QUERY not in registry

    def test_marketplace_item_media_query_is_registry_resolved(self):
        registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
        assert registry.doc_id(MARKETPLACE_ITEM_MEDIA_QUERY_NAME) == "10059604367394414"


# -------------------------------------------------------------- profile view
class TestProfileView:
    """Pins the profile view: preload replay with userID substitution,
    typed timeline rows, and the baked-template fallback."""

    def test_view_replays_page_preload_with_id_substituted(self):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        service = ProfileService(session)
        fetched: list[str] = []
        service._fetch = lambda url: fetched.append(url) or _profile_page_html("000000000")
        view = service.view("12345678901234568")

        # the profile page was fetched at the target's root URL
        assert fetched == ["https://www.facebook.com/12345678901234568"]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == DEFAULT_PROFILE_QUERY
        assert doc_id == PROFILE_LIST_VIEW_DOC_ID
        # the requested numeric id replaces the preload's userID (probe:
        # the target id rides ONLY as the top-level userID variable)
        assert variables["userID"] == "12345678901234568"
        # the verbatim preload shape otherwise (not the baked template:
        # the canned registration carries no relay-provider gates)
        assert (
            "privacySelectorRenderLocation" not in variables
            or variables["privacySelectorRenderLocation"] == "COMET_STREAM"
        )
        assert (
            "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider"
            not in variables
        )

        # identity head from the payload's own nodes (actor nodes carry
        # the target's id + name — the list-view selection has no user.name)
        assert view.user.id == "12345678901234568"
        assert view.user.name == "Sample Page"

        # typed feed rows walked out of the HighlightPostUnit nodes
        assert len(view.feed.stories) == 2
        first = view.feed.stories[0]
        assert first.id == "1532996765541719"
        assert first.actor is not None and first.actor.name == "Sample Page"
        assert first.text == "profile post one"
        assert first.permalink == "https://www.facebook.com/reel/1856023965378356/"
        assert first.feedback is not None
        assert first.feedback.reaction_count == 5
        assert view.feed.end_cursor == "PROFILECURSOR"
        assert view.feed.has_next_page is True

    def test_view_vanity_replays_preload_verbatim(self):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        service = ProfileService(session)
        service._fetch = lambda url: _profile_page_html(SAMPLE_PAGE_ID)
        view = service.view("sample.page")
        # vanity slug: the page's own preload userID stands (no substitution)
        _, _, variables = _last_call(session)
        assert variables["userID"] == SAMPLE_PAGE_ID
        assert view.user.id == SAMPLE_PAGE_ID
        assert len(view.feed.stories) == 2

    def test_view_limit_slices_rows_without_touching_variables(self):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        service = ProfileService(session)
        service._fetch = lambda url: _profile_page_html()
        view = service.view("sample.page", limit=1)
        assert len(view.feed.stories) == 1
        assert view.feed.stories[0].text == "profile post one"
        # the wire variables are never edited for the local limit
        _, _, variables = _last_call(session)
        assert "limit" not in variables

    def test_view_falls_back_to_baked_template(self):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        service = ProfileService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        view = service.view("12345678901234568")
        friendly, doc_id, variables = _last_call(session)
        assert friendly == DEFAULT_PROFILE_QUERY
        assert doc_id == DEFAULT_PROFILE_DOC_ID
        assert variables["userID"] == "12345678901234568"
        assert variables["renderLocation"] == "timeline"
        assert len(view.feed.stories) == 2

    def test_view_vanity_without_preload_is_typed_error(self):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        service = ProfileService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        with pytest.raises(ValueError):
            service.view("sample.page")


# -------------------------------------------------------- friends suggestions
class TestFriendsSuggestions:
    """Pins the pymk_grid suggestions read off the friends root query."""

    def test_suggestions_typed_rows_with_default_variables(self):
        session = stub(
            {
                ROOT_QUERY_NAME: _root_payload(
                    [
                        _user_node("1000000000000101", "Eve Evesson"),
                        _user_node("1000000000000102", "Frank Frankson"),
                    ]
                )
            }
        )
        suggestions = FriendsService(session).suggestions()
        assert [(u.name, u.id) for u in suggestions] == [
            ("Eve Evesson", "1000000000000101"),
            ("Frank Frankson", "1000000000000102"),
        ]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == ROOT_QUERY_NAME
        assert doc_id == FRIENDS_ROOT_DOC_ID
        assert variables == DEFAULT_FRIENDS_VARIABLES

    def test_suggestions_limit(self):
        session = stub(
            {
                ROOT_QUERY_NAME: _root_payload(
                    [
                        _user_node("1000000000000101", "Eve Evesson"),
                        _user_node("1000000000000102", "Frank Frankson"),
                    ]
                )
            }
        )
        suggestions = FriendsService(session).suggestions(limit=1)
        assert [u.id for u in suggestions] == ["1000000000000101"]

    def test_suggestions_empty_grid_is_empty_list(self):
        session = stub({ROOT_QUERY_NAME: _root_payload([])})
        assert FriendsService(session).suggestions() == []

    def test_suggestions_missing_grid_is_empty_list(self):
        session = stub({ROOT_QUERY_NAME: {"data": {"viewer": {}}}})
        assert FriendsService(session).suggestions() == []


# -------------------------------------------------------- marketplace search
class TestMarketplaceSearch:
    """Pins the marketplace search: verbatim preload variables (query term
    inside params.bqf), typed ProductItem rows, and url assembly."""

    def test_search_replays_preload_variables_verbatim(self):
        session = stub({MARKETPLACE_SEARCH_QUERY_NAME: SEARCH_PAYLOAD})
        service = MarketplaceService(session)
        fetched: list[str] = []
        service._fetch = lambda url: fetched.append(url) or _search_page_html()
        listings = service.search("laptop")

        # the search page was fetched with the url-encoded term
        assert fetched == ["https://www.facebook.com/marketplace/search/?query=laptop"]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == MARKETPLACE_SEARCH_QUERY_NAME
        assert doc_id == MARKETPLACE_SEARCH_DOC_ID
        # VERBATIM preload variables — the query term rides inside
        # params.bqf.query / savedSearchQuery, never re-assembled
        assert variables == {
            "buyLocation": {"latitude": 24.8833, "longitude": 90.7167},
            "contextual_data": None,
            "count": 24,
            "cursor": None,
            "params": {"bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": "laptop"}},
            "savedSearchID": None,
            "savedSearchQuery": "laptop",
            "scale": 2,
            "topicPageParams": {"location_id": "12345678901234571", "url": None},
        }
        # the local limit never edits the wire variables
        assert "limit" not in variables

        # typed rows walked out of the ProductItem nodes
        assert len(listings) == 2
        first = listings[0]
        assert first.typename == "ProductItem"
        assert first.name == "Laptop+"
        assert first.snippet == "BDT15,000"
        assert first.id == "28295742886732939"
        # the node carries no url — the item url is assembled from the id
        assert first.url == ("https://www.facebook.com/marketplace/item/28295742886732939/")

    def test_search_limit_slices_rows(self):
        session = stub({MARKETPLACE_SEARCH_QUERY_NAME: SEARCH_PAYLOAD})
        service = MarketplaceService(session)
        service._fetch = lambda url: _search_page_html()
        listings = service.search("laptop", limit=1)
        assert len(listings) == 1
        assert listings[0].name == "Laptop+"

    def test_search_still_walks_browse_listing_typename(self):
        """The browse feed's GroupCommerceProductItem spelling survives the
        shared walker (same listing shape, different typename)."""
        payload = {
            "data": {
                "marketplace": {
                    "feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "listing": {
                                        "__typename": "GroupCommerceProductItem",
                                        "id": "777",
                                        "marketplace_listing_title": "Browse Item",
                                        "formatted_price": {"text": "BDT9,000"},
                                        "url": "https://www.facebook.com/marketplace/item/777/",
                                    }
                                }
                            },
                        ]
                    }
                }
            }
        }
        session = stub({MARKETPLACE_SEARCH_QUERY_NAME: payload})
        service = MarketplaceService(session)
        service._fetch = lambda url: _search_page_html()
        listings = service.search("laptop")
        assert len(listings) == 1
        assert listings[0].typename == "GroupCommerceProductItem"
        assert listings[0].name == "Browse Item"
        assert listings[0].snippet == "BDT9,000"
        assert listings[0].url == "https://www.facebook.com/marketplace/item/777/"

    def test_search_without_preload_is_typed_error(self):
        session = stub({MARKETPLACE_SEARCH_QUERY_NAME: SEARCH_PAYLOAD})
        service = MarketplaceService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        with pytest.raises(ValueError):
            service.search("laptop")


# ------------------------------------------------------- marketplace item detail
ITEM_ID = "1462059362647521"
ITEM_URL = f"https://www.facebook.com/marketplace/item/{ITEM_ID}/"


def _item_page_html() -> str:
    """The marketplace item page SSR-registrations, crafted from the live
    probe of /marketplace/item/1462059362647521/ (2026-09): the PDP
    container query plus its registry-backed media companion, with the
    item id riding the top-level targetId of both."""
    return _preloads_html([
        (MARKETPLACE_ITEM_QUERY_NAME, MARKETPLACE_ITEM_DOC_ID,
         {"feedbackSource": 56, "feedLocation": "MARKETPLACE_MEGAMALL",
          "referralSurfaceString": None, "scale": 2, "targetId": "999",
          "useDefaultActor": False}),
        (MARKETPLACE_ITEM_MEDIA_QUERY_NAME, "10059604367394414",
         {"targetId": "999"}),
    ])


def _pdp_payload() -> dict:
    """A canned MarketplacePDPContainerQuery response crafted from the live
    replay (2026-09): the listing's full fields ride
    viewer.marketplace_product_details_page.target."""
    return {
        "data": {
            "viewer": {
                "marketplace_product_details_page": {
                    "__typename": "MarketplaceForSaleItemProductDetailsPage",
                    "target": {
                        "__typename": "GroupCommerceProductItem",
                        "id": ITEM_ID,
                        "marketplace_listing_title": "Laptop+",
                        "redacted_description": {"text": "Gaming laptop, used once."},
                        "listing_price": {
                            "formatted_amount_zeros_stripped": "BDT15,000",
                            "amount": "15000.00",
                            "currency": "BDT",
                        },
                        "marketplace_listing_seller": {
                            "__typename": "User",
                            "id": "10000000000000001",
                            "name": "Person B",
                        },
                        "location_text": {"text": "Muktagacha, Mymensingh"},
                        "creation_time": 1789751981,
                        "story": {"id": "UzpfSTEwMDA3", "url": ITEM_URL},
                    },
                }
            }
        }
    }


def _item_media_payload() -> dict:
    """A canned media-viewer response (live shape:
    target.listing_photos[].image.uri)."""
    return {
        "data": {
            "viewer": {
                "marketplace_product_details_page": {
                    "target": {
                        "__typename": "GroupCommerceProductItem",
                        "id": ITEM_ID,
                        "listing_photos": [
                            {"image": {"uri": "https://cdn.example/p1.jpg",
                                       "width": 960, "height": 960}},
                            {"image": {"uri": "https://cdn.example/p2.jpg",
                                       "width": 960, "height": 960}},
                        ],
                    }
                }
            }
        }
    }


class TestMarketplaceItem:
    """Pins the item-detail PDP replay: targetId substitution, the
    registry-backed media companion, and degradation on media failure."""

    def _stub(self) -> StubSession:
        return stub({
            MARKETPLACE_ITEM_QUERY_NAME: _pdp_payload(),
            MARKETPLACE_ITEM_MEDIA_QUERY_NAME: _item_media_payload(),
        })

    def test_item_replays_preloads_with_target_id_substituted(self):
        session = self._stub()
        service = MarketplaceService(session)
        fetched: list[str] = []
        service._fetch = lambda url: fetched.append(url) or _item_page_html()
        detail = service.item_detail(ITEM_ID)

        # the item page was fetched at the item's own URL
        assert fetched == [ITEM_URL]
        # first the PDP container replay: preload doc_id + verbatim
        # variables, with the requested id substituted into targetId
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == MARKETPLACE_ITEM_QUERY_NAME
        assert doc_id == MARKETPLACE_ITEM_DOC_ID
        assert variables["targetId"] == ITEM_ID
        assert variables["feedbackSource"] == 56
        assert variables["feedLocation"] == "MARKETPLACE_MEGAMALL"
        assert variables["scale"] == 2
        # then the media companion: preload doc_id + targetId only
        friendly, doc_id, variables = session.graphql.calls[1]
        assert friendly == MARKETPLACE_ITEM_MEDIA_QUERY_NAME
        assert doc_id == "10059604367394414"
        assert variables == {"targetId": ITEM_ID}

        # the typed detail dict off the PDP target node
        assert detail["id"] == ITEM_ID
        assert detail["typename"] == "GroupCommerceProductItem"
        assert detail["title"] == "Laptop+"
        assert detail["description"] == "Gaming laptop, used once."
        assert detail["price"] == "BDT15,000"
        assert detail["seller"] == {"id": "10000000000000001", "name": "Person B"}
        assert detail["location"] == "Muktagacha, Mymensingh"
        assert detail["url"] == ITEM_URL
        assert detail["creation_time"] == 1789751981
        # image URLs walked off the media companion replay
        assert detail["images"] == ["https://cdn.example/p1.jpg",
                                    "https://cdn.example/p2.jpg"]

    def test_item_falls_back_to_baked_template(self):
        session = self._stub()
        service = MarketplaceService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        detail = service.item_detail("777")
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == MARKETPLACE_ITEM_QUERY_NAME
        assert doc_id == MARKETPLACE_ITEM_DOC_ID
        # the baked live-captured PDP template with the requested id
        assert variables == {**DEFAULT_ITEM_VARIABLES, "targetId": "777"}
        # the URL decodes from the payload's own story node (the canned
        # response's item), while the id falls back to the requested one
        assert detail["id"] == ITEM_ID
        assert detail["url"] == ITEM_URL

    def test_item_media_failure_degrades_to_no_images(self):
        session = stub({
            MARKETPLACE_ITEM_QUERY_NAME: _pdp_payload(),
            MARKETPLACE_ITEM_MEDIA_QUERY_NAME: RuntimeError("media companion down"),
        })
        service = MarketplaceService(session)
        service._fetch = lambda url: _item_page_html()
        detail = service.item_detail(ITEM_ID)
        # the detail itself decoded; only the secondary media call failed
        assert detail["title"] == "Laptop+"
        assert detail["images"] == []


# ------------------------------------------------------------- groups members
GROUP_ID = "12345678901234567"


def _members_page_html() -> str:
    """The group members page SSR-registration, crafted from the live probe
    of /groups/12345678901234567/members/ (2026-09): the group id rides the
    top-level groupID variable."""
    return _preload_html(GROUP_MEMBERS_QUERY, GROUP_MEMBERS_DOC_ID,
                         {"groupID": "000000000", "scale": 2})


def _members_payload() -> dict:
    """A canned GroupsCometMembersRootQuery response crafted from the live
    replay (2026-09): data.group carries the admin edges plus the newest
    member edges (User nodes with name/id)."""
    return {
        "data": {
            "group": {
                "id": GROUP_ID,
                "group_member_profiles": {"count": 288213},
                "group_admin_profiles": {
                    "edges": [
                        {"node": {"__typename": "User", "id": "10000000000000002",
                                  "name": "Person C"},
                         "cursor": "AQADMIN"}
                    ],
                    "page_info": {"has_next_page": True, "end_cursor": "AQADMINS"},
                },
                "new_members": {
                    "edges": [
                        {"node": {"__typename": "User", "id": "10000000000000003",
                                  "name": "Person D"},
                         "cursor": "AQ1"},
                        {"node": {"__typename": "User", "id": "10000000000000002",
                                  "name": "Person C"},
                         "cursor": "AQ2"},
                        {"node": {"__typename": "User", "id": ACTOR_ID,
                                  "name": "Test User"},
                         "cursor": "AQ3"},
                    ],
                    "page_info": {"has_next_page": True, "end_cursor": "AQNEW"},
                },
            }
        }
    }


class TestGroupsMembers:
    """Pins the group members read: preload groupID substitution, the
    admin/newest merge, dedup, and is_self flagging."""

    def test_members_replays_preload_with_group_id_substituted(self):
        session = stub({GROUP_MEMBERS_QUERY: _members_payload()})
        service = GroupsService(session)
        fetched: list[str] = []
        service._fetch = (lambda url: fetched.append(url)
                          or _members_page_html())
        rows = service.members(GROUP_ID)

        # the members page was fetched at the group's members URL
        assert fetched == [f"https://www.facebook.com/groups/{GROUP_ID}/members/"]
        friendly, doc_id, variables = _last_call(session)
        assert friendly == GROUP_MEMBERS_QUERY
        assert doc_id == GROUP_MEMBERS_DOC_ID
        # the requested group id replaces the preload's groupID
        assert variables["groupID"] == GROUP_ID
        assert variables["scale"] == 2

        # User-like rows: admins head the list, duplicates dropped,
        # the viewer row flagged is_self
        assert len(rows) == 3
        assert rows[0] == {"id": "10000000000000002", "name": "Person C",
                           "role": "ADMIN", "is_self": False}
        assert rows[1]["name"] == "Person D"
        assert rows[1]["role"] is None
        assert rows[2] == {"id": ACTOR_ID, "name": "Test User",
                           "role": None, "is_self": True}

    def test_members_falls_back_to_baked_template(self):
        session = stub({GROUP_MEMBERS_QUERY: _members_payload()})
        service = GroupsService(session)

        def boom(url: str) -> str:
            raise RuntimeError("network down")

        service._fetch = boom
        rows = service.members("12345")
        friendly, doc_id, variables = _last_call(session)
        assert friendly == GROUP_MEMBERS_QUERY
        assert doc_id == GROUP_MEMBERS_DOC_ID
        # the baked live-captured template: groupID + scale only
        assert variables == {"groupID": "12345", "scale": 2}
        assert len(rows) == 3

    def test_members_limit_slices_rows_without_touching_variables(self):
        session = stub({GROUP_MEMBERS_QUERY: _members_payload()})
        service = GroupsService(session)
        service._fetch = lambda url: _members_page_html()
        rows = service.members(GROUP_ID, limit=2)
        assert [row["name"] for row in rows] == ["Person C", "Person D"]
        _, _, variables = _last_call(session)
        assert "limit" not in variables

    def test_members_missing_group_node_is_empty_list(self):
        session = stub({GROUP_MEMBERS_QUERY: {"data": {}}})
        service = GroupsService(session)
        service._fetch = lambda url: _members_page_html()
        assert service.members(GROUP_ID) == []


# --------------------------------------------------------------- photos surface
SECTION_TOKEN = PhotosService.section_token(ACTOR_ID)
ALBUMS_NAV_TOKEN = ("YXBwX2NvbGxlY3Rpb246cGZiaWQwM3ZCdW5CM2NIVlZRTVNyM2FCTXE4VVh2alhR"
                    "MVJtQWhnTU1yNXN3SFFhZDNYdkc0SGtwTVZLdUZCNGZUeDlUQmRoZ3NkTk0y")


def _nav_tiles() -> list[dict]:
    """The photos-tab nav collection tiles (live shape: opaque app_collection
    token ids, names, urls; the Albums tile's url ends photos_albums)."""
    return [
        {"id": "TOK_PHOTOS_BY", "name": "Test User's Photos",
         "url": f"https://www.facebook.com/profile.php?id={ACTOR_ID}&sk=photos_by"},
        {"id": ALBUMS_NAV_TOKEN, "name": "Albums",
         "url": f"https://www.facebook.com/profile.php?id={ACTOR_ID}&sk=photos_albums"},
    ]


def _album_edge(album_id: str, name: str, count: str, cover: str) -> dict:
    """One album grid edge — the AlbumsRenderer spelling (live shape: the
    TimelineAppCollectionItem wraps an Album; title/subtitle_text/image)."""
    return {
        "node": {
            "id": f"app_item_{album_id}",
            "node": {"__typename": "Album", "id": album_id,
                     "url": f"https://www.facebook.com/media/set/?set=a.{album_id}&type=3"},
            "title": {"text": name},
            "subtitle_text": {"text": count},
            "image": {"uri": cover},
            "url": f"https://www.facebook.com/media/set/?set=a.{album_id}&type=3",
            "__typename": "TimelineAppCollectionItem",
        },
        "cursor": f"AQ{album_id}",
    }


def _photo_edge(photo_id: str) -> dict:
    """One photos-grid edge — the PhotosRenderer spelling (live shape: the
    TimelineAppCollectionItem wraps a Photo with viewer_image uri/dims)."""
    return {
        "node": {
            "id": f"app_item_{photo_id}",
            "node": {"__typename": "Photo", "id": photo_id,
                     "viewer_image": {"uri": f"https://cdn.example/{photo_id}.jpg",
                                      "width": 1080, "height": 1350}},
            "image": {"uri": f"https://cdn.example/{photo_id}_grid.jpg"},
            "url": f"https://www.facebook.com/photo.php?fbid={photo_id}&type=3",
            "__typename": "TimelineAppCollectionItem",
        },
        "cursor": f"AQ{photo_id}",
    }


def _section_payload(edges: list[dict], renderer: str) -> dict:
    """A canned ProfileCometTopAppSectionQuery response (live shape: node
    carries the nav tiles plus the current collection's grid)."""
    return {
        "data": {
            "node": {
                "__typename": "TimelineAppSection",
                "id": SECTION_TOKEN,
                "name": "Photos",
                "section_type": "PHOTOS",
                "nav_collections": {"nodes": _nav_tiles()},
                "all_collections": {
                    "nodes": [
                        {
                            "id": ALBUMS_NAV_TOKEN,
                            "style_renderer": {
                                "__typename": renderer,
                                "collection": {
                                    "items": {"count": len(edges)},
                                    "pageItems": {
                                        "edges": edges,
                                        "page_info": {"has_next_page": True,
                                                      "end_cursor": "AQSECTION"},
                                    },
                                },
                            },
                        }
                    ]
                },
            }
        }
    }


ALBUMS_PAYLOAD = _section_payload(
    [_album_edge("374275834747157", "Photos", "10,000 items",
                 "https://cdn.example/cover1.jpg"),
     _album_edge("1473805714794158", "The historical journey of World Cup",
                 "6 items", "https://cdn.example/cover2.jpg")],
    "TimelineAppCollectionAlbumsRenderer",
)

PHOTOS_PAYLOAD = _section_payload(
    [_photo_edge("1533366415504754"), _photo_edge("1533344512173611")],
    "TimelineAppCollectionPhotosRenderer",
)


def _album_media_payload() -> dict:
    """A canned ProfileCometLegacyAlbumViewRootQuery response (live shape:
    data.mediaset.grid_media.edges -> Photo nodes with image uri/dims)."""
    return {
        "data": {
            "mediaset": {
                "__typename": "Album",
                "id": "374275834747157",
                "grid_media": {
                    "edges": [
                        {"node": {"__typename": "Photo", "id": "1533366415504754",
                                  "__isMedia": "Photo",
                                  "image": {"uri": "https://cdn.example/g1.jpg",
                                            "width": 720, "height": 900},
                                  "viewer_image": {"width": 1080, "height": 1350},
                                  "accessibility_caption": "cover collage"},
                         "cursor": "AQG1"},
                        {"node": {"__typename": "Photo", "id": "1533344512173611",
                                  "__isMedia": "Photo",
                                  "image": {"uri": "https://cdn.example/g2.jpg",
                                            "width": 720, "height": 900},
                                  "viewer_image": {"width": 1080, "height": 1350},
                                  "accessibility_caption": None},
                         "cursor": "AQG2"},
                    ],
                    "page_info": {"has_next_page": True, "end_cursor": "AQGRID"},
                },
            }
        }
    }


class TestPhotosAlbums:
    """Pins the albums two-replay pattern: the default section grid first,
    then the Albums tile's opaque collection token."""

    def test_albums_replays_section_then_albums_collection(self):
        """Two section replays (live pattern): the default grid first (its
        nav carries the Albums tile token), then the albums collection."""
        session = stub({PHOTOS_SECTION_QUERY: ALBUMS_PAYLOAD})
        service = PhotosService(session)

        def boom(url: str) -> str:
            raise RuntimeError("no page plane")

        service._fetch = boom
        rows = service.albums()

        assert len(session.graphql.calls) == 2
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == PHOTOS_SECTION_QUERY
        assert doc_id == PHOTOS_SECTION_DOC_ID
        # the baked template: own uid + the constructible sectionToken
        assert variables["userID"] == ACTOR_ID
        assert variables["collectionToken"] is None
        assert variables["sectionToken"] == SECTION_TOKEN
        # the second replay scopes to the Albums tile's opaque token
        friendly, doc_id, variables = session.graphql.calls[1]
        assert friendly == PHOTOS_SECTION_QUERY
        assert variables["collectionToken"] == ALBUMS_NAV_TOKEN

        # typed album rows (id, name, count, cover, url)
        assert rows[0] == {
            "id": "374275834747157",
            "name": "Photos",
            "count": "10,000 items",
            "cover": "https://cdn.example/cover1.jpg",
            "url": "https://www.facebook.com/media/set/?set=a.374275834747157&type=3",
        }
        assert rows[1]["name"] == "The historical journey of World Cup"
        assert rows[1]["count"] == "6 items"

    def test_albums_harvests_own_page_preload_when_present(self):
        session = stub({PHOTOS_SECTION_QUERY: ALBUMS_PAYLOAD})
        service = PhotosService(session)
        service._fetch = lambda url: _preload_html(
            PHOTOS_SECTION_QUERY, "9999988887777",
            {"collectionToken": None, "scale": 2, "sectionToken": "STALE",
             "useDefaultActor": False, "userID": "000"})
        service.albums(limit=1)
        _friendly, doc_id, variables = session.graphql.calls[0]
        # the harvested doc_id + pinned own-profile ids (the stale preload
        # userID/sectionToken are substituted)
        assert doc_id == "9999988887777"
        assert variables["userID"] == ACTOR_ID
        assert variables["sectionToken"] == SECTION_TOKEN

    def test_albums_limit_slices_rows(self):
        session = stub({PHOTOS_SECTION_QUERY: ALBUMS_PAYLOAD})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        rows = service.albums(limit=1)
        assert len(rows) == 1
        assert rows[0]["name"] == "Photos"

    def test_albums_without_albums_nav_tile_is_empty(self):
        payload = _section_payload([], "TimelineAppCollectionPhotosRenderer")
        payload["data"]["node"]["nav_collections"]["nodes"] = [_nav_tiles()[0]]
        session = stub({PHOTOS_SECTION_QUERY: payload})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        assert service.albums() == []
        # only the section replay fired — no albums collection call
        assert len(session.graphql.calls) == 1


class TestPhotosPhotos:
    """Pins the photos grid: default collection replay and the
    album-scoped mediaSetToken form (live shape)."""

    def test_photos_default_grid_rows(self):
        session = stub({PHOTOS_SECTION_QUERY: PHOTOS_PAYLOAD})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        rows = service.photos()

        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = _last_call(session)
        assert friendly == PHOTOS_SECTION_QUERY
        assert doc_id == PHOTOS_SECTION_DOC_ID
        assert variables["userID"] == ACTOR_ID
        assert variables["collectionToken"] is None

        assert rows[0] == {
            "id": "1533366415504754",
            "uri": "https://cdn.example/1533366415504754.jpg",
            "width": 1080,
            "height": 1350,
            "url": "https://www.facebook.com/photo.php?fbid=1533366415504754&type=3",
        }
        assert len(rows) == 2

    def test_photos_album_scoped_replays_album_view_query(self):
        """A numeric album id (as albums() returns) scopes the read via the
        album page's own query — mediaSetToken "a.<album_id>" (live shape)."""
        session = stub({ALBUM_VIEW_QUERY: _album_media_payload()})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        rows = service.photos("374275834747157")

        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = _last_call(session)
        assert friendly == ALBUM_VIEW_QUERY
        assert doc_id == ALBUM_VIEW_DOC_ID
        assert variables["mediaSetToken"] == "a.374275834747157"
        assert variables["feedLocation"] == "ALBUM_FEED"

        assert rows[0] == {
            "id": "1533366415504754",
            "uri": "https://cdn.example/g1.jpg",
            "width": 720,
            "height": 900,
            "caption": "cover collage",
        }
        assert rows[1]["caption"] is None

    def test_photos_album_token_passthrough(self):
        """An already-shaped "a.<id>" token passes through untouched."""
        session = stub({ALBUM_VIEW_QUERY: _album_media_payload()})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        service.photos("a.1473805714794158")
        _, _, variables = _last_call(session)
        assert variables["mediaSetToken"] == "a.1473805714794158"

    def test_photos_limit_slices_rows_without_touching_variables(self):
        session = stub({PHOTOS_SECTION_QUERY: PHOTOS_PAYLOAD})
        service = PhotosService(session)
        service._fetch = lambda url: "<html>no preloads</html>"
        rows = service.photos(limit=1)
        assert [row["id"] for row in rows] == ["1533366415504754"]
        _, _, variables = _last_call(session)
        assert "limit" not in variables


# ------------------------------------------------------ notifications mark-seen
class TestNotificationsMarkSeen:
    """Pins mark-seen: id harvest from the dropdown, and the decoded
    MARK_ALL_SEEN commit shape carrying NO client_mutation_id."""

    LIST_QUERY_ID = "Cg8BdAZqrVZrDwNlbnYPDE1BSU5fU1VSRkFDRQ8CZnQKAQ"
    LAST_SYNC = 1789744747

    def _list_payload(self, ids: list[str]) -> dict:
        return {
            "data": {
                "viewer": {
                    "last_update_timestamp": self.LAST_SYNC,
                    "notifications_page": {
                        "query_id": self.LIST_QUERY_ID,
                        "edges": [_notif_row(i) for i in ids],
                    }
                }
            }
        }

    def test_mark_seen_harvests_ids_and_decoded_shape(self):
        ids = ["bm90aWZpY2F0aW9uAAAA", "bm90aWZpY2F0aW9uBBBB"]
        session = stub(
            {
                LIST_QUERY_NAME: self._list_payload(ids),
                MARK_SEEN_MUTATION: {
                    "data": {
                        "notifications_update_seen_or_read": {
                            "notifications": [
                                {"id": i, "seen_state": "SEEN_BUT_UNREAD"} for i in ids
                            ],
                            "viewer": {"notifications_unseen_count": 0},
                        }
                    }
                },
            }
        )
        result = NotificationsService(session).mark_seen()
        assert len(session.graphql.calls) == 2

        # first the dropdown list harvests the received notif ids
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == LIST_QUERY_NAME
        assert doc_id == NOTIFICATIONS_LIST_DOC_ID
        assert variables["environment"] == "MAIN_SURFACE"

        # then the mutation fires with the decoded MARK_ALL_SEEN shape
        friendly, doc_id, variables = session.graphql.calls[1]
        assert friendly == MARK_SEEN_MUTATION
        assert doc_id == MARK_SEEN_DOC_ID == "32345368141745868"
        # schema-decoded 2026-09: LocalArguments environment + input; the
        # MARK_ALL_SEEN commit carries NO client_mutation_id
        assert set(variables) == {"environment", "input"}
        assert variables["environment"] == "MAIN_SURFACE"
        assert set(variables["input"]) == {
            "actor_id",
            "environment",
            "is_comet",
            "last_notif_sync_time",
            "notif_ids",
            "query_id",
            "source",
            "update_type",
        }
        assert variables["input"]["environment"] == "MAIN_SURFACE"
        assert variables["input"]["is_comet"] is True
        # actor_id is the session viewer id — wire-required (live-verified:
        # a missing actor_id is rejected with variable-coercion 1675012)
        assert variables["input"]["actor_id"] == ACTOR_ID
        # last_notif_sync_time + query_id are the LIST response's own
        # viewer.last_update_timestamp / notifications_page.query_id
        assert variables["input"]["last_notif_sync_time"] == self.LAST_SYNC
        assert variables["input"]["query_id"] == self.LIST_QUERY_ID
        assert variables["input"]["notif_ids"] == ids
        assert variables["input"]["source"] == "unknown"
        assert variables["input"]["update_type"] == "MARK_ALL_SEEN"
        assert "client_mutation_id" not in variables["input"]

        # the merged response: marked count + post-mutation unseen count
        assert result["marked"] == 2
        assert result["unseen"] == 0
        assert "notifications_update_seen_or_read" in result["data"]

    def test_mark_seen_template_matches_decoded_schema(self):
        """The baked template is exactly the decoded commit shape (minus
        the per-call actor/ids/query-id/sync-time payload)."""
        template = json.loads(json.dumps(MARK_SEEN_VARIABLES_TEMPLATE))
        assert template == {
            "environment": "MAIN_SURFACE",
            "input": {
                "actor_id": None,
                "environment": "MAIN_SURFACE",
                "is_comet": True,
                "last_notif_sync_time": 0,
                "notif_ids": [],
                "query_id": None,
                "source": "unknown",
                "update_type": "MARK_ALL_SEEN",
            },
        }

    def test_mark_seen_without_notifications_never_fires(self):
        """The decoded hook guards on a non-empty id list — with no rows the
        mutation is skipped entirely."""
        session = stub({LIST_QUERY_NAME: self._list_payload([])})
        result = NotificationsService(session).mark_seen()
        assert len(session.graphql.calls) == 1  # only the list harvest
        assert result["marked"] == 0
        assert result["unseen"] is None
        assert result["data"] == {}

    def test_mark_seen_each_call_refires_with_fresh_ids(self):
        ids = ["bm90aWZpY2F0aW9uAAAA"]
        responses = {
            LIST_QUERY_NAME: self._list_payload(ids),
            MARK_SEEN_MUTATION: {"data": {}},
        }
        session = stub(responses)
        service = NotificationsService(session)
        service.mark_seen()
        service.mark_seen()
        assert len(session.graphql.calls) == 4
        # each mutation call re-harvested the id list (no stale payload)
        for call in (session.graphql.calls[1], session.graphql.calls[3]):
            assert call[2]["input"]["notif_ids"] == ids


# ------------------------------------------------- pagination (full history)
class _PageSequence:
    """A StubSession.responses stand-in whose lookups pop the next page.

    The stock StubGraphQLClient answers every call with the same canned
    payload; cursor-chaining tests need one payload PER call. The dict()
    conversion in the stub's constructor would pop pages early, so tests
    assign this AFTER stub() builds the session.
    """

    def __init__(self, name: str, pages: list) -> None:
        self._name = name
        self._pages = list(pages)

    def __contains__(self, key: object) -> bool:
        return key == self._name

    def __getitem__(self, key: str) -> dict:
        if key != self._name:
            raise KeyError(key)
        # keep the last page: an over-walking service must see it, not a
        # KeyError that would fake an early stop
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]


def _page_payload(ids: list, cursor: str, has_next: bool) -> dict:
    """One pagination-query page: notif edges each carrying the walk
    cursor, plus page_info (live shape: the /notifications/ walker)."""
    return {
        "data": {
            "viewer": {
                "notifications_page": {
                    "edges": [dict(_notif_row(i), cursor=f"{cursor}-{k}")
                              for k, i in enumerate(ids)],
                    "page_info": {"end_cursor": "", "has_next_page": has_next},
                }
            }
        }
    }


class TestNotificationsPagination:
    """Pins the full-history walker: the decoded variable set, cursor
    chaining, dedup at page seams, and the stuck-walk safety cap."""

    def test_template_matches_decoded_schema(self):
        """The baked template is exactly the bundle-decoded LocalArgument
        set (count/cursor/environment/filter_tokens/notif_cache_ids/
        notif_query_flags/scale; after<-cursor, first<-count)."""
        assert PAGINATION_VARIABLES_TEMPLATE == {
            "count": 20,
            "cursor": None,
            "environment": "MAIN_SURFACE",
            "filter_tokens": [],
            "notif_cache_ids": [],
            "notif_query_flags": ["IS_COMET", "INCLUDE_WA_P2B_NOTIFS"],
            "scale": 1,
        }

    def test_page_one_sends_decoded_shape(self):
        session = stub({PAGINATION_QUERY_NAME: _page_payload(
            ["bm90aWZpY2F0aW9uQQ=="], "C1", True)})
        result = NotificationsService(session).page(count=7)

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == PAGINATION_QUERY_NAME
        assert doc_id == NOTIFICATIONS_PAGINATION_DOC_ID == PAGINATION_DOC_ID
        # page one: the decoded variable set with count substituted and a
        # null cursor (relay serializes it as the after arg's default)
        assert variables == {
            "count": 7,
            "cursor": None,
            "environment": "MAIN_SURFACE",
            "filter_tokens": [],
            "notif_cache_ids": [],
            "notif_query_flags": ["IS_COMET", "INCLUDE_WA_P2B_NOTIFS"],
            "scale": 1,
        }
        # rows parsed, walk cursor = the LAST edge cursor, has_next from
        # page_info
        assert [n.id for n in result["rows"]] == ["bm90aWZpY2F0aW9uQQ=="]
        assert result["next_cursor"] == "C1-0"
        assert result["has_next"] is True

    def test_page_cursor_passthrough(self):
        session = stub({PAGINATION_QUERY_NAME: _page_payload([], "C", False)})
        NotificationsService(session).page(cursor="PREV-1")
        assert session.graphql.calls[0][2]["cursor"] == "PREV-1"

    def test_paginate_chains_cursors_across_pages(self):
        pages = [
            _page_payload(["A", "B"], "C1", True),
            _page_payload(["C", "D"], "C2", True),
            _page_payload(["E"], "C3", False),
        ]
        session = stub({})
        session.graphql.responses = _PageSequence(PAGINATION_QUERY_NAME, pages)
        result = NotificationsService(session).paginate(count=2)

        assert len(session.graphql.calls) == 3
        # page one rides a null cursor, then each request chains the
        # previous page's LAST edge cursor as relay's after
        assert session.graphql.calls[0][2]["cursor"] is None
        assert session.graphql.calls[1][2]["cursor"] == "C1-1"
        assert session.graphql.calls[2][2]["cursor"] == "C2-1"
        # the walk ends when page_info.has_next_page goes false
        assert [n.id for n in result["notifications"]] == list("ABCDE")
        assert result["pages"] == 3
        assert result["has_next"] is False

    def test_paginate_dedupes_page_seam_repeats(self):
        pages = [
            _page_payload(["A", "B"], "C1", True),
            _page_payload(["B", "C"], "C2", False),
        ]
        session = stub({})
        session.graphql.responses = _PageSequence(PAGINATION_QUERY_NAME, pages)
        result = NotificationsService(session).paginate()
        # relay seams repeat B; the walk emits it once
        assert [n.id for n in result["notifications"]] == ["A", "B", "C"]
        assert result["pages"] == 2

    def test_paginate_stops_when_page_lacks_a_cursor(self):
        # has_next_page true but no edge cursor: the walk cannot continue
        payload = {"data": {"viewer": {"notifications_page": {
            "edges": [],
            "page_info": {"end_cursor": "", "has_next_page": True},
        }}}}
        session = stub({PAGINATION_QUERY_NAME: payload})
        result = NotificationsService(session).paginate()
        assert len(session.graphql.calls) == 1
        assert result["pages"] == 1
        assert result["has_next"] is False

    def test_paginate_cap_stops_a_stuck_walk(self):
        """The same page returned forever (has_next_page true with a
        cursor) must stop at max_pages — never burn the request budget."""
        session = stub({PAGINATION_QUERY_NAME: _page_payload(
            ["A"], "C", True)})
        result = NotificationsService(session).paginate(max_pages=3)
        assert result["pages"] == 3
        assert result["has_next"] is True  # truncated by the cap
        # the default cap is the safety constant the CLI rides
        assert MAX_WALK_PAGES == 50

    def test_page_at_walks_to_the_requested_page(self):
        pages = [
            _page_payload(["A"], "C1", True),
            _page_payload(["B"], "C2", True),
            _page_payload(["C"], "C3", False),
        ]
        session = stub({})
        session.graphql.responses = _PageSequence(PAGINATION_QUERY_NAME, pages)
        result = NotificationsService(session).page_at(3, count=2)
        assert len(session.graphql.calls) == 3
        assert [n.id for n in result["rows"]] == ["C"]
        assert result["page"] == 3

    def test_page_at_short_history_lands_on_the_last_page(self):
        pages = [_page_payload(["A"], "C1", False)]
        session = stub({})
        session.graphql.responses = _PageSequence(PAGINATION_QUERY_NAME, pages)
        result = NotificationsService(session).page_at(5)
        assert len(session.graphql.calls) == 1
        assert result["page"] == 1  # deepest reachable, not the asked-for 5
        assert [n.id for n in result["rows"]] == ["A"]

    def test_page_at_rejects_non_positive_index(self):
        session = stub({})
        with pytest.raises(ValueError):
            NotificationsService(session).page_at(0)


# ------------------------------------------------------------ command wiring
class TestCommands:
    """Pins the read-expansion commands' argparse wiring with the session
    constructor stubbed out."""

    def _run(
        self, monkeypatch, capsys, session, argv, *, fetch: str | None = None
    ) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        monkeypatch.setattr(profile_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(friends_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(marketplace_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(notifications_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(groups_cmd, "new_session", lambda _a: session)
        monkeypatch.setattr(photos_cmd, "new_session", lambda _a: session)
        if fetch is not None:
            monkeypatch.setattr(ProfileService, "_fetch", lambda self, url: fetch)
            monkeypatch.setattr(MarketplaceService, "_fetch", lambda self, url: fetch)
            monkeypatch.setattr(GroupsService, "_fetch", lambda self, url: fetch)
            monkeypatch.setattr(PhotosService, "_fetch", lambda self, url: fetch)
        code = args.fn(args)
        captured = capsys.readouterr().out
        return code, captured

    def test_profile_view_command(self, monkeypatch, capsys):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        code, out = self._run(
            monkeypatch,
            capsys,
            session,
            ["profile", "view", "--user-id", "12345678901234568"],
            fetch=_profile_page_html("000000000"),
        )
        assert code == 0
        assert "Sample Page" in out
        assert "profile post one" in out
        _, _, variables = _last_call(session)
        assert variables["userID"] == "12345678901234568"

    def test_profile_view_command_limit(self, monkeypatch, capsys):
        session = stub({DEFAULT_PROFILE_QUERY: PROFILE_VIEW_PAYLOAD})
        code, out = self._run(
            monkeypatch,
            capsys,
            session,
            ["profile", "view", "--user-id", "sample.page", "--limit", "1"],
            fetch=_profile_page_html(),
        )
        assert code == 0
        assert "-- 1 stories" in out
        assert "profile post two" not in out

    def test_friends_suggestions_command(self, monkeypatch, capsys):
        session = stub(
            {
                ROOT_QUERY_NAME: _root_payload(
                    [
                        _user_node("1000000000000101", "Eve Evesson"),
                    ]
                )
            }
        )
        code, out = self._run(monkeypatch, capsys, session, ["friends", "suggestions"])
        assert code == 0
        assert "suggestions: 1" in out
        assert "Eve Evesson 1000000000000101" in out

    def test_marketplace_search_command(self, monkeypatch, capsys):
        session = stub({MARKETPLACE_SEARCH_QUERY_NAME: SEARCH_PAYLOAD})
        code, out = self._run(
            monkeypatch,
            capsys,
            session,
            ["marketplace", "search", "laptop", "--limit", "1"],
            fetch=_search_page_html(),
        )
        assert code == 0
        assert "listings for 'laptop': 1" in out
        assert "Laptop+" in out
        assert "BDT15,000" in out

    def test_notifications_mark_seen_command(self, monkeypatch, capsys):
        ids = ["bm90aWZpY2F0aW9uAAAA"]
        session = stub(
            {
                LIST_QUERY_NAME: {
                    "data": {
                        "viewer": {
                            "last_update_timestamp": 1789744747,
                            "notifications_page": {
                                "query_id": "Cg8BdAZqrVZrDwNlbnYPDE1BSU5fU1VSRkFDRQ8CZnQKAQ",
                                "edges": [_notif_row(i) for i in ids],
                            },
                        }
                    }
                },
                MARK_SEEN_MUTATION: {
                    "data": {
                        "notifications_update_seen_or_read": {
                            "viewer": {"notifications_unseen_count": 0},
                        }
                    }
                },
            }
        )
        code, out = self._run(monkeypatch, capsys, session, ["notifications", "mark-seen"])
        assert code == 0
        assert "marked seen: 1" in out
        assert "unseen now: 0" in out
        friendly, doc_id, _ = session.graphql.calls[1]
        assert friendly == MARK_SEEN_MUTATION
        assert doc_id == "32345368141745868"

    def test_notifications_list_page_command(self, monkeypatch, capsys):
        session = stub({PAGINATION_QUERY_NAME: _page_payload(
            ["bm90aWZpY2F0aW9uQQ=="], "C1", False)})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["notifications", "list", "--page", "1", "--limit", "5"],
        )
        assert code == 0
        assert "notifications: 1 (page 1)" in out
        # --limit rides the pagination query's per-page count
        friendly, _, variables = session.graphql.calls[0]
        assert friendly == PAGINATION_QUERY_NAME
        assert variables["count"] == 5
        assert variables["cursor"] is None

    def test_notifications_list_all_command(self, monkeypatch, capsys):
        session = stub({PAGINATION_QUERY_NAME: _page_payload(
            ["bm90aWZpY2F0aW9uQQ=="], "C1", False)})
        code, out = self._run(
            monkeypatch, capsys, session, ["notifications", "list", "--all"])
        assert code == 0
        assert "pages walked: 1" in out
        assert "history ended" in out
        assert "count\": 1" in out or "[read]" in out

    def test_notifications_list_page_zero_rejected(self, monkeypatch, capsys):
        session = stub({})
        code, out = self._run(
            monkeypatch, capsys, session, ["notifications", "list", "--page", "0"])
        assert code == 2
        assert "--page is 1-based" in out
        assert session.graphql.calls == []  # no request burned on a bad flag

    def test_notifications_list_page_and_all_are_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["notifications", "list", "--page", "1", "--all"])

    def test_marketplace_item_command(self, monkeypatch, capsys):
        session = stub({
            MARKETPLACE_ITEM_QUERY_NAME: _pdp_payload(),
            MARKETPLACE_ITEM_MEDIA_QUERY_NAME: _item_media_payload(),
        })
        code, out = self._run(
            monkeypatch, capsys, session,
            ["marketplace", "item", "--item-id", ITEM_ID],
            fetch=_item_page_html(),
        )
        assert code == 0
        assert f"item {ITEM_ID}: Laptop+" in out
        assert "Person B" in out
        assert "BDT15,000" in out
        assert "Muktagacha, Mymensingh" in out
        assert "images: 2" in out
        assert "https://cdn.example/p1.jpg" in out

    def test_groups_members_command(self, monkeypatch, capsys):
        session = stub({GROUP_MEMBERS_QUERY: _members_payload()})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["groups", "members", "--group-id", GROUP_ID, "--limit", "2"],
            fetch=_members_page_html(),
        )
        assert code == 0
        assert "members: 2" in out
        assert "Person C 10000000000000002 [ADMIN]" in out
        assert "Person D 10000000000000003" in out

    def test_photos_albums_command(self, monkeypatch, capsys):
        session = stub({PHOTOS_SECTION_QUERY: ALBUMS_PAYLOAD})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["photos", "albums", "--limit", "1"],
            fetch="<html>no preloads</html>",
        )
        assert code == 0
        assert "albums: 1" in out
        assert "Photos 374275834747157 10,000 items" in out

    def test_photos_photos_command_album_scoped(self, monkeypatch, capsys):
        session = stub({ALBUM_VIEW_QUERY: _album_media_payload()})
        code, out = self._run(
            monkeypatch, capsys, session,
            ["photos", "photos", "--album-id", "374275834747157", "--limit", "1"],
            fetch="<html>no preloads</html>",
        )
        assert code == 0
        assert "photos (album 374275834747157): 1" in out
        assert "1533366415504754 720x900" in out
