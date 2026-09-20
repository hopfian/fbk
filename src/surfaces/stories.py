"""Stories surface service (docs/02 §2 endpoint families, docs/15 §P2-2).

The stories tray read: one homepage-preloaded query replayed with the
server's own variables and typed into ``StoryTile`` rows — plus the story
lifecycle family (create text/photo/video, viewers, reply) decoded from
the /stories/create/ composer chunk graph 2026-09-20.

ARCHITECTURE:

  A single read leg with preload precedence (docs/15 §P2-2): the homepage
  bootstrap's SSR preloader registration is the faithful source for both
  the doc_id and the verbatim variables; only when no preload is available
  (e.g. a stubbed offline session) does the service fall back to the baked
  live-probed default set. All I/O delegates to the session's GraphQL
  client — governor-paced, offline-testable against tests/fakes.py.

  The create family rides ``StoriesCreateMutation`` — the composer's own
  commit, with the input assembled exactly the way the decoded transformer
  pipeline builds it (SATP text posts carry message + text_format fields;
  photo/video posts carry the uploaded fbid under attachments). The photo
  and video ingestion reuse the live-verified composer upload surfaces
  (UploadService / VideoUploadService), which return the fbids the
  story attachment rides.

CALIBRATION NOTES:

  Ground truth (live-verified 2026-09-18, read-only):

  * TRAY — the homepage SSR-registers ``StoriesTrayRectangularRootQuery``
    (doc_id 27831542276468324, registry sources preload+relayOperation) with
    the VERBATIM variables the server itself used (docs/15 §P2-2); the query
    replayed live returns tiles under
    ``data.me.unified_stories_buckets.edges[].node``. Live-decoded tile
    structure: ``id``, ``story_bucket_owner`` (User/Page node with id, name,
    profilePic.uri), ``unified_stories.nodes`` (the card list — card_count is
    its length), ``unified_stories_with_notes``, ``is_bucket_seen_by_viewer``,
    ``is_bucket_live``, ``story_bucket_type``, ``thumbnail_story_to_show``;
    the connection carries ``page_info.end_cursor`` / ``has_next_page``.

  The sibling ``StoriesTrayRectangularQuery`` (28261665493524355, registry)
  is the tray-refetch form used by the tray component after interactions;
  the root query is the one the page itself preloads, so it is the one this
  service replays.

  Bundle-decoded 2026-09-20 (the /stories/create/ chunk graph — carrier
  bundles Od_plfFIzpe/8RCEteEs629 of the composer page's bootloader map):

  * CREATE — ``StoriesCreateMutation`` (doc_id 26770527039211553,
    LocalArguments {input}; field story_create(data: $input) ->
    {story_id, logging_token, items[].story.id}). The input is the
    transformer pipeline's output: audiences
    ([{stories: {self: {target_id: <viewer>}}}]), audiences_is_complete,
    source "WWW", logging {composer_session_id}, and per media type —
    SATP (Stories Adaptive Text Post): message {ranges, text} +
    text_format_metadata {inspirations_custom_font_id} +
    text_format_preset_id; PHOTO: attachments [{photo: {id, overlays}}]
    (the overlays list carries tag/music/product sticker shapes, [] for a
    plain photo); VIDEO: attachments [{video: {id}}] (the video id is the
    uploader's fbid, state UPLOADED); privacy rides
    audiences[].stories.self.audience_info
    {client_has_per_story_experience, disable_server_fallback_to_default_privacy,
    story_privacy_row} — the SAME privacy_row_input shape the composer's
    privacy-save mutation carries (live-verified base_state/allow/deny/
    tag_expansion_state); the GenAI label rides
    ai_generated_self_disclosure_metadata.was_self_disclosed_as_ai_generated.
  * VIEWERS — ``StoriesSuspenseViewerSheetViewerListV2Query``
    (38550750487903492, LocalArguments {cursor, id, viewerCount: 5}) — the
    viewer-sheet's seen-by list.
  * REPLY — ``useStoriesSendReplyMutation`` (27836479672650087,
    LocalArgument {input}): text replies carry {attribution_id_v2, message,
    story_id, story_reply_type}; sticker/gif variants swap the media field
    (sticker_id / gif_url).
  * MENU — ``StoriesSuspenseCardOptionMenuExperimentalWithEntryPointQuery``
    (38364108549904404): the story 3-dot menu read whose response carries
    the per-option action data (DELETE etc., StoriesEnums.STORIES_OPTION_TYPES).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from auth.bootstrap import PreloadEntry
from constants import PRIVACY_BASE_STATES

from .base import Surface

#: Friendly name + live-harvested doc_id of the tray root query
#: (docs/15 §P2-2 preload; the refetch sibling is NOT used here — see
#: CALIBRATION NOTES).
TRAY_QUERY = "StoriesTrayRectangularRootQuery"
TRAY_DOC_ID = "27831542276468324"

#: Live-verified 2026-09-18: the VERBATIM homepage SSR preloader variables
#: for StoriesTrayRectangularRootQuery (docs/15 §P2-2). Used whenever a
#: bootstrap preload entry is not available (e.g. a stubbed session).
DEFAULT_STORIES_TRAY_VARIABLES: dict[str, Any] = {
    "blur": 20,
    "bucketsToFetch": 6,
    "isFbNotesIncluded": True,
    "scale": 2,
    "__relay_internal__pv__StoriesTrayTileCoverImageWidthrelayprovider": 110,
    "__relay_internal__pv__StoriesTrayTileCoverImageHeightrelayprovider": 160,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
    "__relay_internal__pv__StoriesTrayTileShouldSkipPrefetchImageURIrelayprovider": False,
    "__relay_internal__pv__StoriesTrayProfessionalInset3DEnabledrelayprovider": True,
    "__relay_internal__pv__StoriesShouldEnableVideoAutoplayrelayprovider": True,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
}

# ── Story lifecycle (bundle-decoded 2026-09-20; see CALIBRATION NOTES) ──────

#: The composer's story-create commit (the one createStories fires).
STORY_CREATE_MUTATION = "StoriesCreateMutation"
STORY_CREATE_DOC_ID = "26770527039211553"

#: The viewer-sheet's seen-by list query (cursor-paged).
VIEWERS_QUERY = "StoriesSuspenseViewerSheetViewerListV2Query"
VIEWERS_DOC_ID = "38550750487903492"

#: The viewer-sheet's reply commit (text/sticker/gif variants share it).
STORY_REPLY_MUTATION = "useStoriesSendReplyMutation"
STORY_REPLY_DOC_ID = "27836479672650087"

#: The story 3-dot menu read (response carries per-option action data).
STORY_MENU_QUERY = "StoriesSuspenseCardOptionMenuExperimentalWithEntryPointQuery"

#: The composer root query (live-read 2026-09-20: doc_id 27421431377506306;
#: preload variables from the /stories/create/ SSR). Its response carries
#: the SATP background preset catalog, the custom font catalog, and the
#: account's unified-stories audience setting.
COMPOSER_ROOT_QUERY = "StoriesCreateQuery"
COMPOSER_ROOT_DOC_ID = "27421431377506306"
COMPOSER_ROOT_VARIABLES: dict[str, Any] = {
    "crosspostCallerName": "fx_product_foundation_client_FXOnline_client_cache",
    "crosspostCustomPartnerParams": [
        {"key": "CROSSPOSTING_DESTINATION_APP", "value": "IG"},
        {"key": "CROSSPOSTING_SHARE_TO_SURFACE", "value": ""},
        {"key": "SHOULD_RETURN_AUTO_XPOST_SETTING", "value": "true"},
    ],
    "crosspostServiceNames": ["CROSS_POSTING_SETTING"],
    "satpScale": 3,
    "scale": 2,
    "shouldFetchCrosspostMetadata": False,
}

#: The story privacy selector read (registry 23931148206490572,
#: live-read 2026-09-20): the audience-mode catalog + the account default.
STORY_PRIVACY_QUERY = "StoriesCometPrivacySelectorDialogQuery"

#: The wire enum of story reply types (decoded from the viewer bundle's
#: useStoriesSendReply commit sites; TEXT is the message-carrying variant).
STORY_REPLY_TYPE_TEXT = "TEXT"

#: The transformer pipeline's base input fields (everything except the
#: media-type-specific attachments/message block, which each create_*
#: method adds — see CALIBRATION NOTES). The decoded commit appends
#: ``tracking`` (the useNotificationsTrackingString context, a non-null
#: list) unconditionally and rides ``navigation_data`` as an explicit null
#: when no nav attribution exists — both required by the input type's
#: coercion (live finding 2026-09-20: omitting either is 1675012
#: noncoercible_variable_value).
def _story_create_base_input(self_id: str) -> dict[str, Any]:
    """The media-independent StoryCreateInput fields (decoded pipeline)."""
    return {
        "audiences": [{"stories": {"self": {"target_id": self_id}}}],
        "audiences_is_complete": True,
        "logging": {"composer_session_id": str(uuid4())},
        "navigation_data": None,
        "source": "WWW",
        "tracking": [""],
    }


def _story_privacy_audience(self_id: str, privacy: str) -> dict[str, Any]:
    """One audiences[] entry carrying a story privacy row.

    The decoded StoriesCreatePrivacyTransformer shape: the self audience's
    ``audience_info`` gains ``client_has_per_story_experience`` plus the
    SAME privacy_row_input the composer's privacy-save mutation carries
    (base_state/allow/deny/tag_expansion_state - live-verified shape), and
    ``disable_server_fallback_to_default_privacy`` goes true whenever an
    explicit row is coerced.
    """
    base_state = PRIVACY_BASE_STATES[privacy]
    return {
        "stories": {
            "self": {
                "target_id": self_id,
                "audience_info": {
                    "client_has_per_story_experience": True,
                    "disable_server_fallback_to_default_privacy": True,
                    "story_privacy_row": {
                        "allow": [],
                        "base_state": base_state,
                        "deny": [],
                        "tag_expansion_state": "TAGGEES",
                    },
                },
            }
        }
    }





class StoryTile(BaseModel):
    """One stories-tray tile (live-decoded bucket node, 2026-09-18).

    Represents one ``unified_stories_buckets`` edge node — the tray entry a
    viewer taps to open a story stack. Fields follow the live-decoded tile
    structure named in the module CALIBRATION NOTES; ``card_count`` is the
    length of the bucket's ``unified_stories.nodes`` list, and the
    ``is_seen``/``is_live`` flags are the bucket-level viewer/liveness
    markers the real client renders badges from.
    """

    id: str
    owner_id: str | None = None
    owner_name: str | None = None
    card_count: int = 0
    thumbnail: str | None = None
    is_seen: bool | None = None
    is_live: bool | None = None
    bucket_type: str | None = None
    typename: str | None = None


def _thumbnail_head(bucket: dict[str, Any]) -> str | None:
    """The tile thumbnail head: the owner's profilePic URI (live-observed
    tile cover source; thumbnail_story_to_show is null on unwatched tiles)."""
    owner = bucket.get("story_bucket_owner")
    if not isinstance(owner, dict):
        return None
    pic = owner.get("profilePic") or owner.get("profile_picture")
    uri = pic.get("uri") if isinstance(pic, dict) else None
    return uri if isinstance(uri, str) else None


def _tile(bucket: dict[str, Any]) -> StoryTile:
    """One bucket node -> a typed StoryTile (live-decoded tile structure)."""
    owner = bucket.get("story_bucket_owner")
    owner = owner if isinstance(owner, dict) else {}
    stories = bucket.get("unified_stories")
    nodes = stories.get("nodes") if isinstance(stories, dict) else None
    return StoryTile(
        id=str(bucket.get("id") or ""),
        owner_id=str(owner.get("id")) if owner.get("id") else None,
        owner_name=owner.get("name") if isinstance(owner.get("name"), str) else None,
        card_count=len(nodes) if isinstance(nodes, list) else 0,
        thumbnail=_thumbnail_head(bucket),
        is_seen=(bucket.get("is_bucket_seen_by_viewer")
                 if isinstance(bucket.get("is_bucket_seen_by_viewer"), bool)
                 else None),
        is_live=(bucket.get("is_bucket_live")
                 if isinstance(bucket.get("is_bucket_live"), bool) else None),
        bucket_type=(bucket.get("story_bucket_type")
                     if isinstance(bucket.get("story_bucket_type"), str) else None),
        typename=(bucket.get("__typename")
                  if isinstance(bucket.get("__typename"), str) else None),
    )


def parse_tray_tiles(payload: dict[str, Any]) -> list[StoryTile]:
    """Merged tray payload -> typed StoryTiles (never raises on an empty
    tray; live-decoded path ``data.me.unified_stories_buckets.edges``).

    Args:
        payload: The merged GraphQL response from the tray query replay.

    Returns:
        One StoryTile per well-shaped bucket edge node, document order;
        ``[]`` when the payload carries no ``edges`` list (an empty tray or
        a degraded/soft-blocked response both degrade to empty).
    """
    out: list[StoryTile] = []
    me = payload.get("data", {}).get("me", {})
    buckets = me.get("unified_stories_buckets") if isinstance(me, dict) else None
    edges = buckets.get("edges") if isinstance(buckets, dict) else None
    if not isinstance(edges, list):
        return out
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        bucket = edge.get("node")
        if isinstance(bucket, dict) and bucket.get("id"):
            out.append(_tile(bucket))
    return out


def _dig(node: Any, path: list[str]) -> Any:
    """Walk a nested dict path; None at the first missing link."""
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _parse_viewers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Merged viewer-list payload -> viewer rows (tolerant walk).

    The viewer-sheet response nests the seen-by connection under the
    viewer node; the exact key names vary by deploy, so the walker picks
    up every dict carrying a user-ish shape (id + name/typename) beneath
    any ``viewers``/``edges``/``nodes`` container.

    Args:
        payload: The merged GraphQL response from the viewers query.

    Returns:
        One ``{"user_id", "name", "typename"}`` dict per well-shaped
        viewer row; ``[]`` when nothing parses.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            user_id = node.get("id")
            name = node.get("name")
            if (isinstance(user_id, str) and isinstance(name, str)
                    and user_id not in seen):
                seen.add(user_id)
                out.append({
                    "user_id": user_id,
                    "name": name,
                    "typename": node.get("__typename"),
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload.get("data", {}))
    return out


class StoriesService(Surface):
    """Stories surface: the tray read (docs/02 §2, docs/15 §P2-2)."""

    def _tray_preload(self) -> PreloadEntry | None:
        """The homepage bootstrap's tray preload entry, when present
        (docs/15 §P2-2 — the SSR registration is the faithful variable
        and queryID source for the tray read)."""
        for entry in self.session.bootstrap().preloads:
            if entry.query_name == TRAY_QUERY:
                return entry
        return None

    def tray(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The stories tray tiles, newest first, up to ``limit``.

        Variable precedence (docs/15 §P2-2): the bootstrap preloader entry
        when present, else the baked live-probed variables; the page size
        is raised to ``limit`` via bucketsToFetch when it exceeds the
        baked default. Tiles are typed StoryTiles (owner name/id,
        card_count, thumbnail head).

        Args:
            limit: Tile cap; also raises the wire-side
                ``bucketsToFetch`` when larger than the baked default of 6.

        Returns:
            ``StoryTile.model_dump()`` dicts (id, owner_id, owner_name,
            card_count, thumbnail, is_seen, is_live, bucket_type,
            typename) in payload order; ``[]`` for an empty tray.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        entry = self._tray_preload()
        if entry is not None:
            doc_id, variables = entry.doc_id, copy.deepcopy(entry.variables)
        else:
            doc_id = self.doc_id(TRAY_QUERY)
            variables = copy.deepcopy(DEFAULT_STORIES_TRAY_VARIABLES)
        variables["bucketsToFetch"] = max(int(variables.get("bucketsToFetch") or 0),
                                          limit)
        payload = self.client.call(TRAY_QUERY, doc_id, variables)
        tiles = parse_tray_tiles(payload)[:limit]
        return [t.model_dump() for t in tiles]

    # ------------------------------------------------------------ lifecycle
    def _create_story(self, media_block: dict[str, Any],
                      *, privacy: str | None = None,
                      ai_label: bool = False) -> dict[str, Any]:
        """Commit one StoriesCreateMutation with the decoded input shape.

        Args:
            media_block: The media-type-specific input fields (SATP message
                + text_format trio, or the attachments list) — merged over
                the pipeline's base fields.
            privacy: Optional CLI audience name (PUBLIC/FRIENDS/PRIVATE);
                None rides the account's default story audience.
            ai_label: Marks the story as self-disclosed AI content (the
                decoded GenAILabelTransformer field).

        Returns:
            The merged mutation response (story_create.story_id is the new
            story's id; items[].story.id the relay story node).

        Raises:
            ValueError: When ``privacy`` is not a known CLI name.
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        self_id = self.session.user_id()
        variables_input = _story_create_base_input(self_id)
        # the privacy transformer (qex-gated) rewrites the self audience's
        # audience_info even with no explicit row: the fresh-state
        # audience ALWAYS carries the two client flags (live finding
        # 2026-09-20 - an audience_info-less self entry is 1675012
        # noncoercible)
        audience_info: dict[str, Any] = {
            "client_has_per_story_experience": True,
            "disable_server_fallback_to_default_privacy": False,
        }
        if privacy is not None:
            if privacy not in PRIVACY_BASE_STATES:
                raise ValueError(
                    f"unknown audience {privacy!r} - use one of "
                    f"{', '.join(sorted(PRIVACY_BASE_STATES))}")
            audience_info.update({
                "disable_server_fallback_to_default_privacy": True,
                "story_privacy_row": {
                    "allow": [],
                    "base_state": PRIVACY_BASE_STATES[privacy],
                    "deny": [],
                    "tag_expansion_state": "TAGGEES",
                },
            })
        variables_input["audiences"] = [{
            "stories": {"self": {"target_id": self_id,
                                 "audience_info": audience_info}},
        }]
        variables_input.update(media_block)
        # the GenAI label transformer runs UNCONDITIONALLY in the decoded
        # pipeline - the disclosure metadata field always rides the input
        # (true or false), never omitted
        variables_input["ai_generated_self_disclosure_metadata"] = {
            "was_self_disclosed_as_ai_generated": bool(ai_label),
        }
        return self.client.call(
            STORY_CREATE_MUTATION,
            self._mutation_doc_id(STORY_CREATE_MUTATION),
            {"input": variables_input},
        )

    def create_text(self, text: str, *, privacy: str | None = None,
                    font_id: str | None = None, preset_id: str | None = None,
                    ai_label: bool = False) -> dict[str, Any]:
        """Create a text (SATP) story.

        The decoded SATP transformer trio rides the input: message (the
        composer message shape), text_format_metadata with the optional
        custom font, and text_format_preset_id (the background style
        preset). ABSENT options are OMITTED, never null: the decoded
        transformer forwards ``satpData.fontID``/``presetID`` verbatim,
        and a JS ``undefined`` is an omitted field — an explicit null is
        a different wire value (and a noncoercible one against
        non-nullable id fields).

        Args:
            text: The story text.
            privacy: Optional CLI audience name (PUBLIC/FRIENDS/PRIVATE).
            font_id: Optional custom font id (the SATP font picker).
            preset_id: Optional background style preset id (the SATP
                background swatch picker).
            ai_label: Mark as self-disclosed AI content.

        Returns:
            The merged mutation response.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        media_block: dict[str, Any] = {"message": {"ranges": [], "text": text}}
        # the SATP transformer always emits text_format_metadata; an absent
        # custom font serializes as an EMPTY object (a JS undefined value
        # vanishes from the key, not the field)
        metadata: dict[str, Any] = {}
        if font_id is not None:
            metadata["inspirations_custom_font_id"] = font_id
        media_block["text_format_metadata"] = metadata
        if preset_id is not None:
            media_block["text_format_preset_id"] = preset_id
        return self._create_story(
            media_block,
            privacy=privacy,
            ai_label=ai_label,
        )

    def create_photo(self, image_path: Path | str, *, privacy: str | None = None,
                     ai_label: bool = False) -> dict[str, Any]:
        """Create a photo story: composer ingest -> fbid -> story attach.

        Reuses the live-verified composer photo uploader for the ingest and
        rides the decoded photo transformer's attachment shape
        (``attachments: [{photo: {id, overlays: []}}]`` - the overlays list
        is the sticker/tag/music container, empty for a plain photo).

        Args:
            image_path: Local image file (image/* MIME - the uploader's own
                accept filter).
            privacy: Optional CLI audience name (PUBLIC/FRIENDS/PRIVATE).
            ai_label: Mark as self-disclosed AI content.

        Returns:
            ``{"photo_id": <fbid>, "story": <mutation response>}``.

        Raises:
            UploadError: The ingest leg failed (missing file, bad MIME,
                non-200, no photoID in the payload).
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        from .upload import UploadService
        # upload pipeline out of the tray-only import graph
        upload = UploadService(self.session).upload_photo(image_path)
        photo_id = upload["photo_id"]
        story = self._create_story(
            {"attachments": [{"photo": {"id": photo_id, "overlays": []}}]},
            privacy=privacy,
            ai_label=ai_label,
        )
        return {"photo_id": photo_id, "story": story}

    def create_video(self, video_path: Path | str, *, privacy: str | None = None,
                     ai_label: bool = False) -> dict[str, Any]:
        """Create a video story: three-stage ingest -> fbid -> story attach.

        Reuses the live-verified composer video uploader (START -> rupload
        bytes -> RECEIVE) and rides the decoded video transformer's
        attachment shape (``attachments: [{video: {id}}]``).

        Args:
            video_path: Local video file (.mp4/.mov).
            privacy: Optional CLI audience name (PUBLIC/FRIENDS/PRIVATE).
            ai_label: Mark as self-disclosed AI content.

        Returns:
            ``{"video_id": <fbid>, "story": <mutation response>}``.

        Raises:
            VideoUploadError: The ingest legs failed.
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        from .video_upload import VideoUploadService
        upload = VideoUploadService(self.session).upload_video(video_path)
        video_id = upload["video_id"]
        story = self._create_story(
            {"attachments": [{"video": {"id": video_id}}]},
            privacy=privacy,
            ai_label=ai_label,
        )
        return {"video_id": video_id, "story": story}

    def viewers(self, story_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Read a story's seen-by viewer list (the viewer-sheet query).

        Args:
            story_id: The story CARD id (the b64 ``S:_I...`` form the tray
                tiles carry in ``unified_stories.nodes``).
            limit: Viewer-row cap; rides the query's ``viewerCount``.

        Returns:
            One dict per viewer row (user id/name + seen state as present);
            ``[]`` for a story with no viewers or a degraded payload.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {
            "cursor": None,
            "id": story_id,
            "viewerCount": limit,
        }
        # registry-orphaned operation (bundle-decoded only): the baked
        # doc_id constant is the resolution source
        payload = self.client.call(VIEWERS_QUERY, VIEWERS_DOC_ID, variables)
        return _parse_viewers(payload)[:limit]

    def reply(self, story_id: str, text: str) -> dict[str, Any]:
        """Reply to a story with text (the viewer-sheet's own commit).

        The decoded useStoriesSendReplyMutation input for the TEXT variant:
        ``{story_id, message, story_reply_type: "TEXT"}`` (+ the optional
        attribution the viewer context carries, omitted here - the decoded
        commit builds it from context state that a CLI run does not have).

        Args:
            story_id: The story CARD id.
            text: The reply text.

        Returns:
            The merged mutation response.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        variables: dict[str, Any] = {
            "input": {
                "message": {"ranges": [], "text": text},
                "story_id": story_id,
                "story_reply_type": STORY_REPLY_TYPE_TEXT,
            }
        }
        return self.client.call(
            STORY_REPLY_MUTATION,
            self._mutation_doc_id(STORY_REPLY_MUTATION),
            variables,
        )

    # -------------------------------------------------------------- catalogs
    def composer_root(self) -> dict[str, Any]:
        """The stories composer root read (live-verified 2026-09-20).

        Returns the raw merged payload of ``StoriesCreateQuery`` - the
        SATP background preset catalog, the custom font list, the
        account's unified-stories audience setting, and the upload
        config all ride ``data.viewer``.
        """
        return self.client.call(
            COMPOSER_ROOT_QUERY, COMPOSER_ROOT_DOC_ID, COMPOSER_ROOT_VARIABLES)

    def presets(self) -> list[dict[str, Any]]:
        """The SATP background style presets (live catalog read).

        Returns:
            ``{"preset_id", "font_id", "has_background_image"}`` rows in
            catalog order; ``[]`` on a degraded payload.
        """
        root = self.composer_root()
        out: list[dict[str, Any]] = []
        # live path (2026-09-20): data.visual_composer_satp_collections
        collections = _dig(root, ["data",
                                  "visual_composer_satp_collections"])
        for collection in collections if isinstance(collections, list) else []:
            presets = collection.get("presets") \
                if isinstance(collection, dict) else None
            for preset in presets if isinstance(presets, list) else []:
                if not isinstance(preset, dict):
                    continue
                font = preset.get("inspirations_custom_font_object")
                bg = preset.get("portrait_background_image")
                out.append({
                    "preset_id": preset.get("preset_id"),
                    "font_id": (font.get("id")
                                if isinstance(font, dict) else None),
                    "has_background_image": isinstance(bg, dict),
                })
        return out

    def fonts(self) -> list[dict[str, Any]]:
        """The SATP custom font catalog (live read).

        Returns:
            ``{"id", "name", "url"}`` rows in catalog order.
        """
        root = self.composer_root()
        nodes = _dig(root, ["data", "viewer", "inspirations_data",
                             "custom_font", "nodes"])
        out: list[dict[str, Any]] = []
        for font in nodes if isinstance(nodes, list) else []:
            if not isinstance(font, dict):
                continue
            out.append({
                "id": font.get("id"),
                "name": font.get("font_name"),
                "url": font.get("font_url"),
            })
        return out

    def audience(self) -> dict[str, Any]:
        """The story audience state: the default mode + the mode catalog.

        Reads both live-verified sources: the composer root's
        ``unified_stories_setting.audience_mode`` (the account default)
        and the privacy selector's audience-mode list.

        Returns:
            ``{"default_mode", "modes": [{"mode", "header",
            "description"}...]}``.
        """
        root = self.composer_root()
        setting = _dig(root, ["data", "viewer", "unified_stories_setting"])
        default = (setting.get("audience_mode")
                   if isinstance(setting, dict) else None)
        payload = self.client.call(
            STORY_PRIVACY_QUERY, self.doc_id(STORY_PRIVACY_QUERY), {"scale": 1})
        modes: list[dict[str, Any]] = []
        rows = _dig(payload, ["data", "viewer", "stories_data",
                              "audience_mode_list"])
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                modes.append({
                    "mode": row.get("unified_stories_audience_mode"),
                    "header": row.get("header"),
                    "description": row.get("description"),
                })
        return {"default_mode": default, "modes": modes}
