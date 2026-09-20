"""Marketplace surface (docs/02-endpoint-surface-map.md §marketplace; docs/15 §P2-2).

ARCHITECTURE:

  Follows the any-surface-page law (docs/15 §P3-5) on all three reads:
  GET the marketplace page, harvest the SSR preload registration, replay
  it verbatim, walk the merged payload for typed listing nodes. The
  search feed specifically replays the harvested preload exactly as
  captured — its query term rides inside opaque params blobs
  (``params.bqf.query``), so re-assembling variables is structurally
  impossible and the verbatim preload is the only working shape. All
  traffic flows through Session/GraphQLClient, so the service is fully
  offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES:

  Live-verified pattern: GET /marketplace/ embeds
  ``MarketplaceCometBrowseFeedLightContainerQuery`` (doc_id
  28053535904279798) plus its VERBATIM variables in the page preload
  registry (docs/15 §P2-2); replaying it yields the light browse feed
  whose units carry ``GroupCommerceProductItem`` listing nodes — the
  concrete Marketplace listing typename discovered in the live probe
  (title, price, id; URL assembled from the numeric item id).

  Item detail (live-probed 2026-09): GET /marketplace/item/<id>/ preloads
  ``MarketplacePDPContainerQuery`` — its replay carries the full listing
  via ``viewer.marketplace_product_details_page.target`` — plus the
  registry-backed ``MarketplacePDPC2CMediaViewerWithImagesQuery`` whose
  replay carries the listing photo URLs
  (``target.listing_photos[].image.uri``).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import quote_plus

from auth.bootstrap import extract_preload_registry
from domain.common import SearchResult

from .base import Surface

MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
MARKETPLACE_SEARCH_URL = "https://www.facebook.com/marketplace/search/?query={query}"
MARKETPLACE_BROWSE_QUERY_NAME = "MarketplaceCometBrowseFeedLightContainerQuery"
MARKETPLACE_BROWSE_DOC_ID = "28053535904279798"
MARKETPLACE_LISTING_TYPENAME = "GroupCommerceProductItem"
MARKETPLACE_ITEM_URL = "https://www.facebook.com/marketplace/item/{id}/"

#: The search-results query the marketplace SEARCH page preloads
#: (live-probed 2026-09: GET /marketplace/search/?query=laptop SSR-registers
#: both CometMarketplaceSearchRootQuery — a small shell with no listings —
#: and CometMarketplaceSearchContentContainerQuery, whose replay carries the
#: actual listing nodes; its doc_id comes from the preload, not the bundle
#: registry).
MARKETPLACE_SEARCH_QUERY_NAME = "CometMarketplaceSearchContentContainerQuery"

#: The item-detail query the marketplace ITEM page preloads
#: (live-probed 2026-09: GET /marketplace/item/<id>/ SSR-registers
#: MarketplacePDPContainerQuery with the VERBATIM variables the server
#: itself used — the item id rides as the top-level ``targetId`` variable.
#: Its doc_id is preload-harvested only: not in the bundle registry).
MARKETPLACE_ITEM_QUERY_NAME = "MarketplacePDPContainerQuery"
MARKETPLACE_ITEM_DOC_ID = "38309511388663877"

#: The item media query the same page preloads (live-probed 2026-09: replay
#: carries ``target.listing_photos[].image.uri`` — the item's photo URLs).
#: This one IS registry-resolvable (doc_id 10059604367394414).
MARKETPLACE_ITEM_MEDIA_QUERY_NAME = "MarketplacePDPC2CMediaViewerWithImagesQuery"

#: Live-captured SSR variables of the item-page PDP preload (2026-09):
#: the full preloader the server itself used when rendering
#: /marketplace/item/1462059362647521/ — only ``targetId`` is parameterized.
DEFAULT_ITEM_VARIABLES: dict[str, Any] = {
    "feedbackSource": 56,
    "feedLocation": "MARKETPLACE_MEGAMALL",
    "referralSurfaceString": None,
    "scale": 2,
    "targetId": "{item_id}",
    "useDefaultActor": False,
    "__relay_internal__pv__MarketplacePDPCometSimilarListingsrelayprovider": False,
    "__relay_internal__pv__MarketplacePDPShouldShowRelatedSearchesrelayprovider": True,
    "__relay_internal__pv__MarketplacePDPShouldShowLoggedOutSellerTrustrelayprovider": False,
    "__relay_internal__pv__ShouldUpdateMarketplaceBoostListingBoostedStatusrelayprovider": False,
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
    "__relay_internal__pv__MarketplacePDPShouldShowBSGRecommendationsrelayprovider": False,
    "__relay_internal__pv__MarketplacePDPJobShouldShowSharedGroupsSectionrelayprovider": False,
    "__relay_internal__pv__MarketplacePDPJobIsShareToGroupsEnabledOnCometrelayprovider": False,
}

#: Listing node typenames: the browse feed carries
#: ``GroupCommerceProductItem`` nodes (live-probed 2026-09, docs/15 §P2-2)
#: while the search feed carries ``ProductItem`` nodes (live-probed 2026-09
#: on the /marketplace/search/?query=laptop replay) — both spell the same
#: listing shape (marketplace_listing_title, listing_price, id).
_LISTING_TYPENAMES = (MARKETPLACE_LISTING_TYPENAME, "ProductItem")

#: Live-captured SSR variables of the marketplace browse preload (docs/15 §P2-2):
#: the viewer's buy location, radius, and the relay provider gates the server
#: itself used when rendering /marketplace/.
DEFAULT_MARKETPLACE_VARIABLES: dict[str, Any] = {
    "buyLocation": {"latitude": 24.8833, "longitude": 90.7167},
    "count": 1,
    "cursor": None,
    "imageWidth": 256,
    "mediaType": "image/jpeg",
    "radius": 65000,
    "scale": 2,
    "sizing": "cover-fill-cropped",
    "useSDFPath": True,
    "__relay_internal__pv__CometMarketplaceShouldShowTopPicksStrikethroughrelayprovider": False,
    "__relay_internal__pv__GHLShouldChangeMarketplaceSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__MarketplaceCometAdmodulerelayprovider": True,
    "__relay_internal__pv__CometMarketplaceShouldShowFeedShippingIconrelayprovider": False,
}

#: Any browse-feed query the marketplace page preloads counts as the entry.
_BROWSE_QUERY_RE = re.compile(r"MarketplaceCometBrowseFeed\w*Query")

#: The search-results content query (the search page's shell
#: CometMarketplaceSearchRootQuery preloads too, but carries no listings).
_SEARCH_QUERY_RE = re.compile(r"CometMarketplaceSearch\w*ContainerQuery")

#: The item-detail content query the item page preloads (MarketplacePDP*).
_ITEM_QUERY_RE = re.compile(r"MarketplacePDP\w*ContainerQuery")


class MarketplaceService(Surface):
    """Marketplace browse/search/item surface: page GET -> preload replay ->
    typed listings (read-only)."""

    # ------------------------------------------------------------------ public
    def browse(
        self, *, variables: dict[str, Any] | None = None, limit: int = 20
    ) -> list[SearchResult]:
        """Browse the marketplace home feed (live pattern, read-only).

        GETs /marketplace/, harvests the browse-feed preload entry
        (verbatim variables, docs/15 §P2-2), merges caller overrides
        (``count`` defaults to ``limit``), replays the query, and walks
        the payload for ``GroupCommerceProductItem`` listing nodes.
        Falls back to the live-baked DEFAULT_MARKETPLACE_VARIABLES pair
        when the page carries no browse preload.

        Args:
            variables: Optional top-level overrides merged last over
                the resolved base (preload-harvested or baked).
            limit: Page size for the ``count`` variable AND the local
                slice of the typed rows — the wire shape is edited only
                through the count, never structurally.

        Returns:
            Typed SearchResult rows (typename, name, id, item url,
            price snippet), deduped on id.
        """
        html = self._fetch(MARKETPLACE_URL)
        entry = None
        for candidate in extract_preload_registry(html):
            if candidate.query_name and _BROWSE_QUERY_RE.fullmatch(candidate.query_name):
                entry = candidate
                break
        if entry is not None:
            query_name: str = entry.query_name or MARKETPLACE_BROWSE_QUERY_NAME
            doc_id: str = entry.doc_id
            base: dict[str, Any] = {
                **copy.deepcopy(DEFAULT_MARKETPLACE_VARIABLES),
                **entry.variables,
            }
        else:
            query_name = MARKETPLACE_BROWSE_QUERY_NAME
            doc_id = MARKETPLACE_BROWSE_DOC_ID
            base = copy.deepcopy(DEFAULT_MARKETPLACE_VARIABLES)
        call_variables: dict[str, Any] = {**base, "count": limit, **(variables or {})}
        data = self.client.call(query_name, doc_id, call_variables)
        results = self._walk_listings(data)
        return results[:limit]

    def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Search marketplace listings (live pattern, read-only).

        GETs /marketplace/search/?query=<term>, harvests the search-results
        preload entry (``CometMarketplaceSearchContentContainerQuery`` with
        the VERBATIM variables the server itself used — the query term
        rides inside ``params.bqf.query`` / ``savedSearchQuery``, so the
        preload is replayed exactly as harvested, never re-assembled), and
        walks the payload for listing nodes (``ProductItem`` in the search
        feed, ``GroupCommerceProductItem`` in the browse feed — same
        listing shape).

        Args:
            query: The search term, urlencoded into the page URL; the
                page itself (not the client) bakes the term into the
                preload variables.
            limit: Maximum typed rows to return; the slice happens
                locally so the wire shape is never edited.

        Returns:
            Typed SearchResult rows, deduped on id.

        Raises:
            ValueError: When the page preloads no search query — the
                search feed has no offline template: the verbatim
                preload variables are the only working shape.
        """
        html = self._fetch(MARKETPLACE_SEARCH_URL.format(query=quote_plus(query)))
        entry = None
        for candidate in extract_preload_registry(html):
            if candidate.query_name and _SEARCH_QUERY_RE.fullmatch(candidate.query_name):
                entry = candidate
                break
        if entry is None:
            raise ValueError(
                f"marketplace search {query!r}: the search page preloads no "
                f"{MARKETPLACE_SEARCH_QUERY_NAME} — fetch the search page "
                "first (the verbatim preload variables are the only working "
                "shape)"
            )
        query_name = entry.query_name or MARKETPLACE_SEARCH_QUERY_NAME
        data = self.client.call(query_name, entry.doc_id, copy.deepcopy(entry.variables))
        return self._walk_listings(data)[:limit]

    def item_detail(self, item_id: str, *, image_head: int = 6) -> dict[str, Any]:
        """Decode one marketplace listing in full (live pattern, read-only).

        GETs /marketplace/item/<id>/, harvests the item-detail preload
        entry (``MarketplacePDPContainerQuery`` with the VERBATIM
        variables the server itself used — the item id rides as the
        top-level ``targetId`` variable, substituted to the requested
        id) and replays it; the payload's
        ``viewer.marketplace_product_details_page.target`` node carries
        the listing's full fields (title, redacted description,
        listing_price, marketplace_listing_seller, location_text).
        Falls back to the live-baked DEFAULT_ITEM_VARIABLES template
        when the page carries no PDP preload. Then (best-effort,
        secondary) the page's media-viewer preload is replayed for the
        listing photo URLs; a failed media companion call degrades to
        no images — never to a failed detail.

        Args:
            item_id: The listing's numeric id.
            image_head: Maximum number of photo URLs to return from the
                media-viewer companion replay.

        Returns:
            A typed dict (id, typename, title, description head, price,
            seller, location, images head, url, creation_time).
        """
        # A failed item-page GET degrades DELIBERATELY to the baked
        # DEFAULT_ITEM_VARIABLES path (pinned by test_item_falls_back):
        # the GraphQL replay that follows is itself a live, typed-error
        # call, so a genuinely dead network still surfaces as a typed
        # failure — the fallback only rescues the HTML-scrape leg
        # (deleted listing, HTML variant without the PDP preload).
        try:
            html = self._fetch(MARKETPLACE_ITEM_URL.format(id=item_id))
        except Exception:
            html = ""
        entry = None
        media_entry = None
        for candidate in extract_preload_registry(html):
            name = candidate.query_name
            if name and entry is None and _ITEM_QUERY_RE.fullmatch(name):
                entry = candidate
            elif name == MARKETPLACE_ITEM_MEDIA_QUERY_NAME:
                media_entry = candidate
        if entry is not None:
            query_name: str = entry.query_name or MARKETPLACE_ITEM_QUERY_NAME
            doc_id: str = entry.doc_id
            base = copy.deepcopy(entry.variables)
        else:
            query_name = MARKETPLACE_ITEM_QUERY_NAME
            doc_id = MARKETPLACE_ITEM_DOC_ID
            base = copy.deepcopy(DEFAULT_ITEM_VARIABLES)
        variables = {**base, "targetId": item_id}
        data = self.client.call(query_name, doc_id, variables)
        detail = self._decode_item(data, item_id)
        detail["images"] = self._item_images(item_id, media_entry)[:image_head]
        return detail

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _item_page_node(data: dict[str, Any]) -> dict[str, Any] | None:
        """The product-details page node in a PDP replay (live shape:
        data.viewer.marketplace_product_details_page)."""
        viewer = (data.get("data") or {}).get("viewer")
        if isinstance(viewer, dict):
            page = viewer.get("marketplace_product_details_page")
            if isinstance(page, dict):
                return page
        return None

    @classmethod
    def _decode_item(cls, data: dict[str, Any], item_id: str) -> dict[str, Any]:
        """Typed detail dict off the PDP target node (live-probed 2026-09:
        title / redacted_description.text / listing_price / seller /
        location_text all ride the ``target`` node)."""
        page = cls._item_page_node(data) or {}
        target = page.get("target")
        if not isinstance(target, dict):
            target = page.get("marketplace_listing_renderable_target")
        target = target if isinstance(target, dict) else {}
        description = target.get("redacted_description")
        description_text = (description or {}).get("text") if isinstance(
            description, dict) else None
        listing_price = target.get("listing_price")
        price = None
        if isinstance(listing_price, dict):
            price = (listing_price.get("formatted_amount_zeros_stripped")
                     or listing_price.get("formatted_amount")
                     or listing_price.get("amount"))
        seller = target.get("marketplace_listing_seller")
        seller_node = seller if isinstance(seller, dict) else {}
        location_text = target.get("location_text")
        story = target.get("story")
        url = story.get("url") if isinstance(story, dict) else None
        return {
            "id": str(target.get("id") or item_id),
            "typename": target.get("__typename"),
            "title": target.get("marketplace_listing_title"),
            "description": description_text,
            "price": price,
            "seller": {"id": seller_node.get("id"), "name": seller_node.get("name")},
            "location": location_text.get("text") if isinstance(location_text, dict) else None,
            "images": [],
            "url": url or MARKETPLACE_ITEM_URL.format(id=item_id),
            "creation_time": target.get("creation_time"),
        }

    def _item_images(self, item_id: str, media_entry: Any) -> list[str]:
        """Best-effort photo URLs off the media-viewer companion replay
        (live shape: target.listing_photos[].image.uri). A failed companion
        call degrades to [] — the detail itself is already decoded."""
        if media_entry is not None:
            variables = {**copy.deepcopy(media_entry.variables), "targetId": item_id}
            doc_id = media_entry.doc_id
        else:
            variables = {"targetId": item_id}
            try:
                doc_id = self.doc_id(MARKETPLACE_ITEM_MEDIA_QUERY_NAME)
            except Exception:
                return []
        try:
            data = self.client.call(MARKETPLACE_ITEM_MEDIA_QUERY_NAME, doc_id, variables)
        except Exception:
            return []
        page = self._item_page_node(data) or {}
        target = page.get("target")
        target = target if isinstance(target, dict) else {}
        photos = target.get("listing_photos")
        out: list[str] = []
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict):
                    image = photo.get("image")
                    if isinstance(image, dict) and isinstance(image.get("uri"), str):
                        out.append(image["uri"])
        return out

    @staticmethod
    def _listing_snippet(node: dict[str, Any]) -> str | None:
        """The listing's price snippet (live shapes: formatted_price.text
        or listing_price formatted_amount/amount)."""
        price = node.get("formatted_price")
        if isinstance(price, dict):
            text = price.get("text")
            if isinstance(text, str):
                return text
        listing_price = node.get("listing_price")
        if isinstance(listing_price, dict):
            formatted = listing_price.get("formatted_amount")
            if isinstance(formatted, str):
                return formatted
            amount = listing_price.get("amount")
            if isinstance(amount, str):
                return amount
        return None

    @classmethod
    def _walk_listings(cls, data: dict[str, Any]) -> list[SearchResult]:
        """Collect listing nodes (both listing typenames), deduped on id."""
        out: list[SearchResult] = []
        seen: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                typename = node.get("__typename")
                if typename in _LISTING_TYPENAMES:
                    listing_id = str(node.get("id") or "")
                    if listing_id and listing_id not in seen:
                        seen.add(listing_id)
                        out.append(
                            SearchResult(
                                typename=str(typename),
                                name=(
                                    node.get("marketplace_listing_title")
                                    or node.get("custom_title")
                                    or None
                                ),
                                id=listing_id or None,
                                url=(node.get("url") or MARKETPLACE_ITEM_URL.format(id=listing_id)),
                                snippet=cls._listing_snippet(node),
                            )
                        )
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return out
