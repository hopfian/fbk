"""Photos surface (docs/02-endpoint-surface-map.md §photos; docs/15 §P2-2).

ARCHITECTURE:

  Follows the any-surface-page law (docs/15 §P3-5) on both scopes: GET
  the photos tab (or the individual album page), harvest the SSR preload
  registration, replay it verbatim, walk the merged payload for typed
  album/photo rows. When the page harvest fails, the service falls back
  to baked preload variables whose only parameterized members are the
  id-bearing tokens — everything else re-rides verbatim. All traffic
  flows through Session/GraphQLClient, so the service is fully
  offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES — live-verified pattern (2026-09):

  The profile photos tab GET
  ``/profile.php?id=<uid>&sk=photos`` SSR-registers
  ``ProfileCometTopAppSectionQuery`` — its replay carries the whole section:
  ``data.node.nav_collections`` (the tab's collection tiles: "<X>'s Photos",
  "Tagged photos", "Albums" — each with its opaque app_collection token id)
  plus ``data.node.all_collections.nodes[].style_renderer.collection`` whose
  ``pageItems.edges`` carry the current collection's grid (Photo nodes with
  ``viewer_image`` uri/dimensions for the photos grid, Album tiles with
  title/count/cover for the albums collection).

  The section id inside the sectionToken is a GLOBAL constant (live-verified
  on a User profile — app_section:4:2305272732 — and a Page —
  app_section:12345678901234568:2305272732), so the own-profile token is
  constructible from the viewer id alone: base64("app_section:<uid>:2305272732").
  That is the one honest fallback when the own photos page cannot be
  harvested (its preload variables otherwise verbatim-baked from the live
  public-page probe).

  Album-scoped photos use the individual album page's own preload
  (live-probed 2026-09): GET ``/media/set/?set=a.<album_id>&type=3``
  SSR-registers ``ProfileCometLegacyAlbumViewRootQuery`` with
  ``mediaSetToken = "a.<album_id>"`` — the token is derivable from the
  numeric album id the albums walk returns, no opaque token needed. Its
  replay carries ``data.mediaset.grid_media.edges`` -> Photo nodes
  (id, image uri/dimensions, viewer_image full dimensions).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import copy
from typing import Any

from auth.bootstrap import extract_preload_registry

from .base import Surface

#: The photos-tab section query (live-probed 2026-09; preload-harvested —
#: NOT in the bundle registry).
PHOTOS_SECTION_QUERY = "ProfileCometTopAppSectionQuery"
PHOTOS_SECTION_DOC_ID = "28071960315806033"

#: The individual-album view query (live-probed 2026-09; preload-harvested).
ALBUM_VIEW_QUERY = "ProfileCometLegacyAlbumViewRootQuery"
ALBUM_VIEW_DOC_ID = "28545531181774259"

#: The photos-tab app section id — the trailing constant of every photos-tab
#: sectionToken (live-verified constant across profile types, 2026-09).
PHOTOS_SECTION_ID = "2305272732"

OWN_PHOTOS_URL = "https://www.facebook.com/profile.php?id={uid}&sk=photos"
ALBUM_URL = "https://www.facebook.com/media/set/?set={token}&type=3"

#: Live-captured SSR variables of the photos-tab preload (2026-09, probe:
#: a public profile's photos tab) — the profile-specific bits are the
#: parameterized placeholders (userID + sectionToken) and collectionToken
#: (None = the default photos grid; an opaque app_collection token scopes
#: the grid to that collection, e.g. the Albums tile).
DEFAULT_PHOTOS_SECTION_VARIABLES: dict[str, Any] = {
    "collectionToken": None,
    "scale": 2,
    "sectionToken": "{section_token}",
    "useDefaultActor": False,
    "userID": "{user_id}",
    "__relay_internal__pv__FBProfile_enable_perf_improv_gkrelayprovider": False,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__FBUnifiedVideoMediaContentContainer_comet_reels_video_footer_defer_loading_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaContentContainer_comet_video_document_picture_in_picture_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaContentContainer_enable_chapters_pill_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__ShouldEnableBakedInTextUnifiedVideorelayprovider": False,
    "__relay_internal__pv__FBUnifiedVideoCometVideoMedia_comet_photosensitive_content_warning_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaHeaderControls_enable_chapters_pill_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__FBUnifiedVideoMediaFooter_organic_ad_cta_on_comet_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaFooter_enable_meta_ai_pill_gkrelayprovider": False,
    "__relay_internal__pv__FBUnifiedVideoMediaFooter_enable_ai_embodiment_chat_pill_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoMediaFooter_enable_video_augment_pills_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoPlayerScrubber_fb_comet_vpv_heatmap_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoDescriptionWithEntities_comet_translations_revamp_sync_caption_with_audio_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBUnifiedVideoFeedbackBar_comet_reels_save_button_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__usePushPipEngagementCounts_comet_video_document_picture_in_picture_gkrelayprovider": False,  # noqa: E501
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": False,
    "__relay_internal__pv__FBUnifiedVideoMenu_fb_reels_ranking_debug_tool_gkrelayprovider": False,
    "__relay_internal__pv__CometAudioLanguageUtils_comet_translations_revamp_preferred_languages_gkrelayprovider": False,  # noqa: E501
}

#: Live-captured SSR variables of the album-page preload (2026-09, probe:
#: media/set/?set=a.374275834747157 — the mediaSetToken is "a." + the
#: numeric album id the albums walk returns).
DEFAULT_ALBUM_VIEW_VARIABLES: dict[str, Any] = {
    "feedbackSource": 67,
    "feedLocation": "ALBUM_FEED",
    "mediaSetToken": "{album_token}",
    "privacySelectorRenderLocation": "COMET_STREAM",
    "renderLocation": "album_feed",
    "scale": 2,
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": False,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": False,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,  # noqa: E501 (live-captured relay param key — unwrappable)
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": False,
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": False,
    "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
    "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": False,
    "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": False,
    "__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider": 150,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
    "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
}


def _text(node: Any, key: str) -> str | None:
    """node[key].text for the TextWithEntities spelling (live shape)."""
    inner = node.get(key) if isinstance(node, dict) else None
    if isinstance(inner, dict):
        text = inner.get("text")
        if isinstance(text, str):
            return text
    return None


class PhotosService(Surface):
    """Own-profile photo surface: albums + photos reads (read-only)."""

    # ------------------------------------------------------------------ tokens
    @staticmethod
    def section_token(user_id: str) -> str:
        """The photos-tab sectionToken: base64("app_section:<uid>:<section id>").

        Args:
            user_id: The profile owner's numeric id.

        Returns:
            The opaque app_section sectionToken; the trailing section id
            is the live-verified global photos constant
            PHOTOS_SECTION_ID (docs/15 §P8-2).
        """
        raw = f"app_section:{user_id}:{PHOTOS_SECTION_ID}"
        return base64.b64encode(raw.encode()).decode()

    @staticmethod
    def album_media_token(album_id: str) -> str:
        """The album-view mediaSetToken: "a." + the numeric album id.

        Args:
            album_id: The numeric album id the albums walk returns (an
                "a."-prefixed token is passed through unchanged).

        Returns:
            The live-observed "a.<album_id>" mediaSetToken format.
        """
        return album_id if album_id.startswith("a.") else f"a.{album_id}"

    # ------------------------------------------------------------- harvesting
    def _section_entry(self) -> Any:
        """Harvest the photos-tab preload from the own photos page (None when
        the page carries no usable registration — e.g. a logged-out shell)."""
        try:
            html = self._fetch(OWN_PHOTOS_URL.format(uid=self.session.user_id()))
            for candidate in extract_preload_registry(html):
                if candidate.query_name == PHOTOS_SECTION_QUERY:
                    return candidate
        except Exception:
            return None
        return None

    def _album_entry(self, album_token: str) -> Any:
        """Harvest the album-view preload from the album page (None when the
        page carries no usable registration)."""
        try:
            html = self._fetch(ALBUM_URL.format(token=album_token))
            for candidate in extract_preload_registry(html):
                if candidate.query_name == ALBUM_VIEW_QUERY:
                    return candidate
        except Exception:
            return None
        return None

    # --------------------------------------------------------------- replays
    def _replay_section(self, entry: Any, collection_token: str | None,
                        doc_id: str | None = None) -> dict[str, Any]:
        """Replay ProfileCometTopAppSectionQuery for the own photos section
        (verbatim harvested variables when available, else the baked
        template); userID/sectionToken/collectionToken are always pinned to
        the requested values."""
        if entry is not None:
            base = copy.deepcopy(entry.variables)
            call_doc_id = doc_id or entry.doc_id
        else:
            base = copy.deepcopy(DEFAULT_PHOTOS_SECTION_VARIABLES)
            call_doc_id = doc_id or PHOTOS_SECTION_DOC_ID
        user_id = self.session.user_id()
        variables = {
            **base,
            "userID": user_id,
            "sectionToken": self.section_token(user_id),
            "collectionToken": collection_token,
        }
        return self.client.call(PHOTOS_SECTION_QUERY, call_doc_id, variables)

    def _replay_album(self, entry: Any, album_token: str) -> dict[str, Any]:
        """Replay ProfileCometLegacyAlbumViewRootQuery for one album
        (verbatim harvested variables when available, else the baked
        template); mediaSetToken is always pinned to the album token."""
        if entry is not None:
            base = copy.deepcopy(entry.variables)
            call_doc_id = entry.doc_id
        else:
            base = copy.deepcopy(DEFAULT_ALBUM_VIEW_VARIABLES)
            call_doc_id = ALBUM_VIEW_DOC_ID
        variables = {**base, "mediaSetToken": album_token}
        return self.client.call(ALBUM_VIEW_QUERY, call_doc_id, variables)

    # ------------------------------------------------------------------ public
    def albums(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Read the own profile's album tiles (live pattern, read-only).

        Two section replays: first with collectionToken None (the default
        grid — its nav_collections carry the tab's collection tiles), then
        scoped to the Albums tile's opaque token, whose
        ``TimelineAppCollectionAlbumsRenderer`` grid carries the album rows
        (numeric Album id, title, "<N> items" count, cover image URL).

        Args:
            limit: Maximum album rows to return; the slice happens
                locally so the wire shape is never edited.

        Returns:
            Album row dicts (id, name, count, cover, url); an empty list
            when the section replay carries no Albums tile (e.g. an
            empty profile).
        """
        entry = self._section_entry()
        section = self._replay_section(entry, None)
        albums_token = self._albums_token(section)
        if albums_token is None:
            return []
        payload = self._replay_section(entry, albums_token)
        rows = self._walk_albums(payload)
        if limit >= 0 and len(rows) > limit:
            rows = rows[:limit]
        return rows

    def photos(self, album_id: str | None = None, *,
               limit: int = 20) -> list[dict[str, Any]]:
        """Read the own profile's photo grid (live pattern, read-only).

        Without ``album_id`` the default photos-grid collection is replayed
        (Photo nodes: id, image uri, viewer_image dimensions, photo url).
        With a numeric ``album_id`` (as returned by ``albums``) the album
        page's ``ProfileCometLegacyAlbumViewRootQuery`` is replayed with
        mediaSetToken "a.<album_id>" — its grid carries that album's photos
        (id, image uri + dimensions, accessibility caption).

        Args:
            album_id: Optional numeric album id scoping the walk to one
                album's media set.
            limit: Maximum rows to return; the slice happens locally so
                the wire shape is never edited.

        Returns:
            Photo row dicts (id, uri, width, height, url/caption), deduped
            on id.
        """
        if album_id is not None:
            album_token = self.album_media_token(album_id)
            entry = self._album_entry(album_token)
            payload = self._replay_album(entry, album_token)
            rows = self._walk_album_media(payload)
        else:
            entry = self._section_entry()
            payload = self._replay_section(entry, None)
            rows = self._walk_photos(payload)
        if limit >= 0 and len(rows) > limit:
            rows = rows[:limit]
        return rows

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _albums_token(section: dict[str, Any]) -> str | None:
        """The Albums nav tile's opaque collection token (live shape: the
        nav node's url ends with /photos_albums; falls back to the node
        named "Albums")."""
        node = (section.get("data") or {}).get("node")
        nav = node.get("nav_collections") if isinstance(node, dict) else None
        nodes = nav.get("nodes") if isinstance(nav, dict) else None
        if not isinstance(nodes, list):
            return None
        for tile in nodes:
            if not isinstance(tile, dict):
                continue
            url = tile.get("url") or ""
            if isinstance(url, str) and url.rstrip("/").endswith("photos_albums"):
                token = tile.get("id")
                if isinstance(token, str) and token:
                    return token
        for tile in nodes:
            if isinstance(tile, dict) and tile.get("name") == "Albums":
                token = tile.get("id")
                if isinstance(token, str) and token:
                    return token
        return None

    @classmethod
    def _collection_edges(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """pageItems.edges of the first collection renderable in a section
        replay (live shape: all_collections.nodes[].style_renderer.collection)."""
        node = (payload.get("data") or {}).get("node")
        collections = node.get("all_collections") if isinstance(node, dict) else None
        nodes = collections.get("nodes") if isinstance(collections, dict) else None
        if not isinstance(nodes, list):
            return []
        for entry in nodes:
            renderer = entry.get("style_renderer") if isinstance(entry, dict) else None
            collection = (renderer.get("collection")
                          if isinstance(renderer, dict) else None)
            page_items = (collection.get("pageItems")
                          if isinstance(collection, dict) else None)
            if isinstance(page_items, dict) and isinstance(page_items.get("edges"), list):
                return [e for e in page_items["edges"] if isinstance(e, dict)]
        return []

    @classmethod
    def _walk_albums(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Album rows off the Albums collection grid (live shape: each edge
        node is a TimelineAppCollectionItem wrapping an Album with title,
        "<N> items" subtitle and cover image)."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in cls._collection_edges(payload):
            item = edge.get("node")
            if not isinstance(item, dict):
                continue
            album = item.get("node")
            album_id = str(album.get("id") or "") if isinstance(album, dict) else ""
            if not album_id or album_id in seen:
                continue
            seen.add(album_id)
            image = item.get("image")
            rows.append({
                "id": album_id,
                "name": _text(item, "title"),
                "count": _text(item, "subtitle_text"),
                "cover": image.get("uri") if isinstance(image, dict) else None,
                "url": item.get("url") or (album.get("url") if isinstance(album, dict) else None),
            })
        return rows

    @classmethod
    def _walk_photos(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Photo rows off the default photos grid (live shape: each edge node
        is a TimelineAppCollectionItem wrapping a Photo with viewer_image)."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in cls._collection_edges(payload):
            item = edge.get("node")
            if not isinstance(item, dict):
                continue
            photo = item.get("node")
            if not isinstance(photo, dict) or photo.get("__typename") != "Photo":
                continue
            photo_id = str(photo.get("id") or "")
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            viewer_image = photo.get("viewer_image")
            image = item.get("image")
            uri = None
            if isinstance(viewer_image, dict):
                uri = viewer_image.get("uri")
            if not uri and isinstance(image, dict):
                uri = image.get("uri")
            rows.append({
                "id": photo_id,
                "uri": uri,
                "width": viewer_image.get("width") if isinstance(viewer_image, dict) else None,
                "height": viewer_image.get("height") if isinstance(viewer_image, dict) else None,
                "url": item.get("url"),
            })
        return rows

    @staticmethod
    def _walk_album_media(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Photo rows off an album-view replay (live shape:
        data.mediaset.grid_media.edges[].node = Photo with image uri/dims)."""
        mediaset = (payload.get("data") or {}).get("mediaset")
        grid = mediaset.get("grid_media") if isinstance(mediaset, dict) else None
        edges = grid.get("edges") if isinstance(grid, dict) else None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(edges, list):
            return rows
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            photo = edge.get("node")
            if not isinstance(photo, dict) or photo.get("__typename") != "Photo":
                continue
            photo_id = str(photo.get("id") or "")
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            image = photo.get("image")
            rows.append({
                "id": photo_id,
                "uri": image.get("uri") if isinstance(image, dict) else None,
                "width": image.get("width") if isinstance(image, dict) else None,
                "height": image.get("height") if isinstance(image, dict) else None,
                "caption": photo.get("accessibility_caption"),
            })
        return rows
