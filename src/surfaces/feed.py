"""FEED surface service: read, paginate, react, comment, publish, edit
(docs/02 §3).

ARCHITECTURE:

  Reads ride the SSR preloader-replay discipline (docs/15 §P2-2): the
  homepage embeds ``CometModernHomeFeedQuery`` with the exact variables
  the server itself used, and this service replays them verbatim — the
  single most faithful and bypass-resistant read available. Pagination
  follows docs/04 §5: the same base variables plus an opaque ``cursor``
  token, sent through ``CometNewsFeedPaginationQuery``.

  Writes (react / comment / publish) replay browser-captured mutation
  templates from ``data/captured_*.json`` (docs/15 §P2-3, §P3-3): the
  captured variables are deep-copied, only the target ids and payload
  text are substituted, and the encrypted ``tracking`` blobs are re-sent
  untouched — they are part of the accepted wire shape, and a
  synthesized substitute would be a strictly less faithful, more
  detectable envelope.

  All traffic flows through Session/GraphQLClient (never raw HTTP), so
  the service is fully offline-testable against StubSession
  (tests/fakes.py); the governor's mutation budget attaches to every
  write at the GraphQL-client layer.

CALIBRATION NOTES:

  * Read doc_ids come from the harvested registry (docs/13 §2); write
    doc_ids come from ``constants.KNOWN_MUTATIONS`` — the live-verified
    end-to-end pairs (docs/15 §P2-3/P3) win over registry harvests.
  * DEFAULT_FEED_VARIABLES is the live-probed 2026-09 preloader set,
    used only when no bootstrap preload entry is available (e.g. a
    stubbed session in tests).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import copy
import json
import re
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

import constants as C
from domain.common import (
    Actor,
    CommentID,
    Feedback,
    FeedbackID,
    FeedPage,
    Privacy,
    ReactionType,
    Story,
    StoryKey,
)

from .base import Surface, _is_reaction, _walk_preorder, load_template

# --------------------------------------------------------------------------- names
FEED_QUERY = "CometModernHomeFeedQuery"
FEED_PAGINATION_QUERY = "CometNewsFeedPaginationQuery"
REACT_MUTATION = "CometUFIFeedbackReactMutation"
CREATE_COMMENT_MUTATION = "useCometUFICreateCommentMutation"
DELETE_COMMENT_MUTATION = "CometUFIDeleteCommentMutation"
COMPOSER_MUTATION = "ComposerStoryCreateMutation"
# Per-post audience save (the captured composer asset carries this mutation
# in the settings-default scope; set_privacy re-targets it at a story's own
# privacy_scope_renderer id - live-verified 2026-09-20).
PRIVACY_SAVE_MUTATION = "CometPrivacySelectorSavePrivacyMutation"
# The composer's life-event category listing — LIVE-CALIBRATED 2026-09-20:
# doc_id 32116841221248541 with variables {"scale": 1} answered with the
# category tree at data.viewer.life_event_categories.nodes. The publish-side
# input for life events is UNKNOWN — this family lists only; do not invent a
# publish shape for it (docs/15 §P3 composer ground truth).
LIFE_EVENT_CATEGORIES_QUERY = "CometComposerLifeEventCategoryListQuery"
LIFE_EVENT_CATEGORIES_DOC_ID = "32116841221248541"
TYPING_START_MUTATION = "CometUFILiveTypingBroadcastMutation_StartMutation"
TYPING_STOP_MUTATION = "CometUFILiveTypingBroadcastMutation_StopMutation"

# --------------------------------------------------- 3-dot post-centric family
# The share / save / notify additions (the 3-dot menu world). The share
# dialog QUERY names below are REGISTRY-GROUNDED (data/doc_id_registry_v3.json
# "carried" entries, docs/13 §2 harvest): ShareToFeedComposerCometDialogQuery
# doc_id 9862910007095900 (the share-to-own-timeline composer dialog) and
# CometUnifiedShareSheetDialogQuery 27699349389751397. The share PUBLISH rides
# the captured ComposerStoryCreateMutation above with a share attachment.
SHARE_TO_FEED_DIALOG_QUERY = "ShareToFeedComposerCometDialogQuery"
SHARE_TO_FEED_DIALOG_DOC_ID = "9862910007095900"
UNIFIED_SHARE_SHEET_QUERY = "CometUnifiedShareSheetDialogQuery"
UNIFIED_SHARE_SHEET_DOC_ID = "27699349389751397"

# CANDIDATE SHAPE - UNVERIFIED (probe pending 2026-09-20): the share element
# inside input.attachments on ComposerStoryCreateMutation. The attachments
# LIST is live-grounded (the photo attach {"photo": {"id": ...}} — captured
# composer assets + surfaces/upload.py); the "share" member shape below is
# the candidate the dialog probes above must confirm (the field name
# "shareable_id" is family-naming inference, not a capture). A wrong
# candidate fails safe through the composer's 1675012 coercion gate (typed
# error, no post created, docs/15 §P3-3).
SHARE_ATTACHMENT_CANDIDATE: dict[str, Any] = {"share": {"shareable_id": None}}

# The actor-scoped "turn on/off notifications" pair (the 3-dot menu action).
# Registry-grounded (v3 "carried" entries): Subscribe 9594760067273294,
# Unsubscribe 25718290144521573 — resolved through _mutation_doc_id.
NOTIFY_SUBSCRIBE_MUTATION = "CommitActorSubscribeStatusSubscribeMutation"
NOTIFY_UNSUBSCRIBE_MUTATION = "CommitActorSubscribeStatusUnsubscribeMutation"

# CANDIDATE VARIABLES - UNVERIFIED (probe pending 2026-09-20): no browser
# capture of either CommitActorSubscribeStatus mutation exists yet, so the
# envelope below follows the standard comet mutation input convention (actor
# id + fresh client_mutation_id) and nothing more — the live probe must
# confirm the field set (a subscribe-status flag is the likeliest addition).
NOTIFY_VARIABLES_CANDIDATE: dict[str, Any] = {
    "input": {
        "actor_id": None,
        "client_mutation_id": None,
    },
}
# The edit-post pair, registry-grounded from the fresh full harvest
# (data/doc_id_registry_v3.json, revision 1047963790, harvested
# 2026-09-20): ComposerStoryEditMutation doc_id 27456358844037239 and
# CometEditFeedComposerDialogQuery doc_id 38644593395156159. NO captured
# variables exist for either (the browser captures cover create/privacy
# only), so every variable shape they send is a CANDIDATE the
# orchestrator's live probe must confirm — the probe-correctable module
# constants below carry the CANDIDATE markers.
EDIT_MUTATION = "ComposerStoryEditMutation"
EDIT_DIALOG_QUERY = "CometEditFeedComposerDialogQuery"
# CANDIDATE - probe pending (2026-09-20): the edit-dialog query's base
# variables. No captured example exists; the shape follows the composer
# family's live-calibrated conventions ({"scale": 1} answered the
# life-event category listing, doc_id 32116841221248541). The live probe
# corrects it HERE, in one place.
EDIT_DIALOG_VARIABLES: dict[str, Any] = {"scale": 1}
# CANDIDATE - probe pending (2026-09-20): the per-post key inside the
# edit-dialog variables ("storyID" after the dialog's own Relay naming).
# Same one-place correction contract as EDIT_DIALOG_VARIABLES.
EDIT_DIALOG_STORY_KEY = "storyID"
# CANDIDATE - probe pending (2026-09-20): where the target story id rides
# in ComposerStoryEditMutation's variables. The most likely shape is
# input.story_id (the create sibling keeps every payload field under
# input); a top-level storyID is the runner-up. A path tuple so the
# probe re-points it in ONE place.
EDIT_STORY_ID_PATH: tuple[str, ...] = ("storyID",)

# --------------------------------------------------------------- verbatim feed vars
# live-probed 2026-09: the full preloader variables for CometModernHomeFeedQuery,
# harvested from the homepage SSR registration (docs/15 §P2-2). Used whenever a
# bootstrap preload entry is not available (e.g. a stubbed session in tests).
DEFAULT_FEED_VARIABLES: dict[str, Any] = {
    "RELAY_INCREMENTAL_DELIVERY": True,
    "connectionClass": "EXCELLENT",
    "feedbackSource": 1,
    "feedInitialFetchSize": 4,
    "feedLocation": "NEWSFEED",
    "feedStyle": "DEFAULT",
    "orderby": ["TOP_STORIES"],
    "privacySelectorRenderLocation": "COMET_STREAM",
    "recentVPVs": [],
    "refreshMode": "COLD_START",
    "renderLocation": "homepage_stream",
    "scale": 2,
    "shouldChangeBRSLabelFieldName": False,
    "shouldObfuscateCategoryField": True,
    "shouldUseBRSLabelFieldNameV1": False,
    "shouldUseBRSLabelFieldNameV2": False,
    "useDefaultActor": False,
    "__relay_internal__pv__GHLShouldChangeSponsoredAuctionDistanceFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldUseSponsoredAuctionLabelFieldNameV1relayprovider": True,
    "__relay_internal__pv__GHLShouldUseSponsoredAuctionLabelFieldNameV2relayprovider": False,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
    "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,  # noqa: E501 (live-captured relay param key — unwrappable)
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": True,
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
    "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
    "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
    "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": False,
    "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": True,
    "__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider": 206,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
    "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
}

# ------------------------------------------------------------------- wire parsing
# Pagination plumbing rides at arbitrary depths in the merged payload, so the
# cheapest faithful extraction is a regex over the JSON re-serialisation
# (docs/04 §5: page_info.end_cursor / has_next_page).
_RE_END_CURSOR = re.compile(r'"end_cursor"\s*:\s*"([^"]+)"')
_RE_HAS_NEXT = re.compile(r'"has_next_page"\s*:\s*true')

# Story keys are base64 of "S:…" (docs/04 §7 identifier table) — 'Uzpf' is the
# base64 prefix of "S:" and marks a Relay story key.
_STORY_KEY_PREFIX = "Uzpf"


def _first_typename(root: Any, typename: str,
                    *, want: Callable[[dict[str, Any]], bool] | None = None,
                    ) -> dict[str, Any] | None:
    """First dict in document order with ``__typename == typename``."""
    for node in _walk_preorder(root):
        if node.get("__typename") == typename and (want is None or want(node)):
            return node
    return None


def _extract_feedback(story: dict[str, Any]) -> Feedback | None:
    """The story's UFI context (docs/04 §6: Feedback is the reaction target).

    Primary source: the first ``__typename == "Feedback"`` node in the story
    subtree. Fallback: the story's own ``feedback`` dict (some payload
    variants omit the __typename on that node) whenever it carries an id.
    """
    fb = _first_typename(story, "Feedback", want=lambda n: bool(n.get("id")))
    if fb is None:
        direct = story.get("feedback")
        if isinstance(direct, dict) and direct.get("id"):
            fb = direct
    if fb is None:
        return None
    fid = FeedbackID.from_b64(str(fb["id"]))

    def _count(key: str) -> int | None:
        """Reaction/comment counts ride as ints or {"count": int} variants."""
        val = fb.get(key)
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, dict):
            count = val.get("count")
            if isinstance(count, int):
                return count
        return None

    return Feedback(id=fid,
                    reaction_count=_count("reaction_count"),
                    comment_count=_count("comment_count"))


def _extract_actor(story: dict[str, Any]) -> Actor | None:
    """First ``User`` node carrying a ``name`` (docs/04 §8 actor model)."""
    user = _first_typename(story, "User", want=lambda n: bool(n.get("name")))
    if user is None:
        return None
    return Actor(**{"__typename": "User",
                    "id": str(user.get("id", "")),
                    "name": str(user["name"])})


def _extract_text(story: dict[str, Any]) -> str | None:
    """First ``TextWithEntities`` node's text field (docs/04 §6 message shape).

    The ``text`` member may be a plain string or a ``{"text": ...}`` dict
    depending on the payload variant — both are handled.
    """
    node = _first_typename(story, "TextWithEntities")
    if node is None:
        return None
    val = node.get("text")
    if isinstance(val, dict):
        val = val.get("text")
    return val if isinstance(val, str) else None


def _extract_permalink(story: dict[str, Any]) -> str | None:
    """First ``wwwURL``/``url`` (https) — the story permalink (docs/04 §7).

    The story's own keys win over anything nested (actor profile URLs would
    otherwise shadow the permalink); ``permalink_url`` is the common
    fallback spelling on feed story nodes.
    """
    for key in ("wwwURL", "url", "permalink_url"):
        val = story.get(key)
        if isinstance(val, str) and val.startswith("https"):
            return val
    for node in _walk_preorder(story):
        for key in ("wwwURL", "url"):
            val = node.get(key)
            if isinstance(val, str) and val.startswith("https"):
                return val
    return None


def _extract_story(node: dict[str, Any]) -> Story:
    """One Relay ``Story`` node -> the typed domain Story (docs/04 §5-§7)."""
    sid = node.get("id")
    sid = sid if isinstance(sid, str) else None
    key = StoryKey(raw=sid) if sid and sid.startswith(_STORY_KEY_PREFIX) else None
    creation = node.get("creation_time")
    return Story(
        id=sid,
        key=key,
        feedback=_extract_feedback(node),
        actor=_extract_actor(node),
        text=_extract_text(node),
        permalink=_extract_permalink(node),
        creation_time=creation if isinstance(creation, int) else None,
    )


def parse_feed_page(payload: dict[str, Any]) -> FeedPage:
    """Merged feed payload -> FeedPage (never raises on an empty feed).

    Stories are every dict with ``__typename == "Story"`` anywhere in the
    tree (docs/04 §3.2); pagination plumbing is regex-extracted from the
    JSON re-serialisation.

    Args:
        payload: The merged GraphQL response document (one or more
            streamed chunks already combined by the client).

    Returns:
        The typed FeedPage; when no Story nodes exist the page is empty
        but still carries ``raw_size`` so callers can log soft-block
        signatures.
    """
    stories = [_extract_story(node) for node in _walk_preorder(payload)
               if node.get("__typename") == "Story"]
    raw = json.dumps(payload, default=str)
    cursor = _RE_END_CURSOR.search(raw)
    return FeedPage(
        stories=stories,
        end_cursor=cursor.group(1) if cursor else None,
        has_next_page=_RE_HAS_NEXT.search(raw) is not None,
        raw_size=len(raw),
    )


def parse_life_event_categories(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Life-event category listing payload -> typed rows (never raises).

    The LIVE-CALIBRATED shape (2026-09-20): categories ride
    ``data.viewer.life_event_categories.nodes`` as
    ``{id, name, icon_id, life_event_types: {nodes: [{id,
    life_event_type_identifier, ...}]}}``. The event-type ``name`` is
    carried when present; the identifier always is.

    Args:
        payload: The merged GraphQL response document.

    Returns:
        One row per category — ``{id, name, icon_id, types}`` with each
        type as ``{id, identifier, name}`` — in document order; an
        absent or empty viewer yields ``[]``.
    """
    root = payload.get("data")
    if not isinstance(root, dict):
        return []
    viewer = root.get("viewer")
    if not isinstance(viewer, dict):
        return []
    conn = viewer.get("life_event_categories")
    if not isinstance(conn, dict):
        return []
    rows: list[dict[str, Any]] = []
    for node in conn.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        types: list[dict[str, Any]] = []
        type_conn = node.get("life_event_types")
        if isinstance(type_conn, dict):
            for tnode in type_conn.get("nodes") or []:
                if not isinstance(tnode, dict):
                    continue
                types.append({
                    "id": tnode.get("id"),
                    "identifier": tnode.get("life_event_type_identifier"),
                    "name": tnode.get("name"),
                })
        rows.append({
            "id": node.get("id"),
            "name": node.get("name"),
            "icon_id": node.get("icon_id"),
            "types": types,
        })
    return rows


# --------------------------------------------------------------- edit parsing
# The edit-dialog payload has NO live capture yet (probe pending), so the
# parser below is DEFENSIVE BY DESIGN: it walks only keys the wider
# composer family already uses, tolerates every absence, and never raises
# on an unexpected shape — the live probe refines it against the real
# response without this code having ever crashed on one.

class EditablePost(BaseModel):
    """The typed view of one post's editable state (the edit dialog).

    Every member is best-effort: the dialog query's exact response shape
    is a probe-pending candidate, so each field stays ``None``/empty
    when the payload carries nothing for it — absence is a valid
    observation, never an error.
    """

    story_id: str | None = None
    text: str | None = None
    privacy_base_state: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    feeling: str | None = None
    activity: str | None = None
    place_id: str | None = None


def _extract_privacy_base_state(root: dict[str, Any]) -> str | None:
    """First ``base_state`` string anywhere in the tree (never raises).

    The audience state rides ``audience.privacy.base_state`` on the
    create side (the live-captured enum); here any dict carrying a
    string ``base_state`` wins — the dialog may nest it under a
    differently-named scope node.
    """
    for node in _walk_preorder(root):
        val = node.get("base_state")
        if isinstance(val, str):
            return val
    return None


def _extract_edit_attachments(root: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``{"photo"|"video": {"id": ...}}`` element, document order.

    The wire attach-element shape (upload.py post_photo/post_album and
    video_upload.py post_video — mediaAttachmentAreaTransformUtil's
    output). The dialog may echo one attachment in several sections, so
    duplicated (kind, id) pairs collapse to the first occurrence.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in _walk_preorder(root):
        for kind in ("photo", "video"):
            val = node.get(kind)
            if not isinstance(val, dict) or val.get("id") is None:
                continue
            media_id = str(val["id"])
            if (kind, media_id) in seen:
                continue
            seen.add((kind, media_id))
            out.append({kind: {"id": media_id}})
    return out


def _extract_target_id(root: dict[str, Any], target_type: str) -> str | None:
    """The ``target_id`` riding a node whose ``target_type`` matches.

    The create-side feeling/activity candidate fields (publish) ride the
    ``target_type``/``target_id`` pair; the dialog's editable state is
    expected to echo the same pair.
    """
    for node in _walk_preorder(root):
        if node.get("target_type") == target_type:
            tid = node.get("target_id")
            if tid is not None and not isinstance(tid, (dict, list)):
                return str(tid)
    return None


def _extract_place_id(root: dict[str, Any]) -> str | None:
    """First scalar ``place_id`` anywhere in the tree (never raises)."""
    for node in _walk_preorder(root):
        val = node.get("place_id")
        if val is not None and not isinstance(val, (dict, list)):
            return str(val)
    return None


def parse_editable_post(payload: dict[str, Any]) -> EditablePost:
    """Merged edit-dialog payload -> the typed EditablePost (never raises).

    DEFENSIVE BY DESIGN: no live example of
    CometEditFeedComposerDialogQuery's response exists yet (probe
    pending), so the parser walks only known composer-family keys — the
    first TextWithEntities text, the first base_state string, the
    photo/video attach elements, target_type/target_id pairs, and
    place_id — and every member degrades to ``None``/empty when absent.
    The live probe refines this against the real response.

    Args:
        payload: The merged GraphQL response document.

    Returns:
        The typed editable state; an empty or unexpectedly-shaped
        payload yields an all-absent EditablePost, never an exception.
    """
    story = _first_typename(payload, "Story")
    sid = story.get("id") if story is not None else None
    return EditablePost(
        story_id=sid if isinstance(sid, str) else None,
        text=_extract_text(payload),
        privacy_base_state=_extract_privacy_base_state(payload),
        attachments=_extract_edit_attachments(payload),
        feeling=_extract_target_id(payload, "FEELING"),
        activity=_extract_target_id(payload, "ACTIVITY"),
        place_id=_extract_place_id(payload),
    )


def _set_by_path(root: dict[str, Any], path: tuple[str, ...],
                 value: Any) -> None:
    """Set ``value`` at a dotted path, creating missing dict levels.

    The edit mutation's story-identifier location is a CANDIDATE
    (EDIT_STORY_ID_PATH — probe pending); this setter walks the path so
    the probe re-points the constant without touching the call site.
    Missing intermediate levels are created as empty dicts so a
    top-level candidate path works against the captured template too.
    """
    node = root
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


# ------------------------------------------------------------ captured templates
# docs/15 P2-3/P3: mutation variables replay VERBATIM from browser captures
# (data/captured_*.json) - substitute only the target ids and payload text,
# re-send the encrypted tracking blobs untouched. The loader itself is
# canonical in surfaces/base.py (load_template); this module-level seam
# preserves the historical _load_template name callers patch.


def _load_template(asset_name: str, friendly: str,
                   *, where: Callable[[dict[str, Any]], bool] | None = None,
                   ) -> dict[str, Any]:
    """Deep-copied captured mutation variables for ``friendly``.

    Thin delegate to :func:`surfaces.base.load_template` (docs/15 §P2-3):
    same shared cache, same scan, same deep-copy semantics. The wrapper
    passes ``check_asset=False`` to preserve this module's historical
    missing-asset behavior — the unguarded read surfaces the filesystem's
    native FileNotFoundError, not base's pre-check RuntimeError.
    """
    return load_template(asset_name, friendly, where=where, check_asset=False)


# ---------------------------------------------------------------- id normalization
def _normalize_feedback(feedback: FeedbackID | str) -> str:
    """Feedback target -> the wire (base64) form.

    Accepts a FeedbackID, a b64 global id ('ZmVlZGJhY2s6…', padded or not),
    a decoded 'feedback:<numeric>' form, or a bare numeric fbid (docs/04 §7).
    """
    if isinstance(feedback, FeedbackID):
        return feedback.raw
    raw = str(feedback)
    if raw.isdigit():
        return FeedbackID.from_numeric(raw).raw
    return FeedbackID.from_b64(raw).raw


def _normalize_comment(comment: CommentID | str) -> str:
    """Comment target -> the wire (base64) form.

    Accepts a CommentID, a b64 global id, a decoded 'comment:<post>_<cid>'
    form, or a bare '<post_fbid>_<comment_fbid>' pair (docs/04 §7).
    """
    if isinstance(comment, CommentID):
        return comment.raw
    raw = str(comment)
    if re.fullmatch(r"\d+_\d+", raw):
        post, _, cid = raw.rpartition("_")
        return CommentID.from_numeric(post, cid).raw
    return CommentID.from_b64(raw).raw


def feedback_for_post(post_id: str) -> FeedbackID:
    """A post's numeric id -> its UFI feedback handle (GROUNDED).

    A post's feedback context id is b64("feedback:<post_id>") — exactly
    the domain FeedbackID codec's :meth:`FeedbackID.from_numeric`
    semantics (docs/04 §6-§7). Live fixture pin: post 3937024329774036
    <-> the wire token "ZmVlZGJhY2s6MzkzNzAyNDMyOTc3NDAzNg" (the
    docs/15 §P2-3 captured reaction pair). The 3-dot UX is post-centric
    — the operator thinks in post ids — so the CLI react/comment/unreact
    verbs derive their feedback target through this helper.

    Args:
        post_id: The target post's numeric id.

    Returns:
        The FeedbackID whose ``.raw`` is the mutation-ready wire token.
    """
    return FeedbackID.from_numeric(post_id)


# ------------------------------------------------------------------------ service
class FeedService(Surface):
    """The feed surface API: read/paginate the stream, act on posts (docs/02 §3).

    Reads replay the server's own preloader variables (docs/15 §P2-2);
    writes replay live-verified mutation captures (docs/15 §P2-3, §P3-3)
    with only the target id and payload substituted — the encrypted
    tracking blobs ride untouched. All traffic flows through
    Session/GraphQLClient, so the service is fully offline-testable
    against StubSession (tests/fakes.py).
    """

    # ------------------------------------------------------------------ reads
    def _feed_preload(self) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        """(doc_id, variables) from the bootstrap preloader, when present.

        docs/15 §P2-2: the homepage SSR registers CometModernHomeFeedQuery
        with the VERBATIM variables and queryID the server itself used — the
        most faithful replay source for the initial feed read.
        """
        for entry in self.session.bootstrap().preloads:
            if entry.query_name == FEED_QUERY:
                return entry.doc_id, dict(entry.variables)
        return None, None

    def read(self, *, variables: dict[str, Any] | None = None) -> FeedPage:
        """Fetch page 1 of the home feed via CometModernHomeFeedQuery.

        Variable precedence (docs/15 §P2-2): the bootstrap preloader entry
        when available, else the baked DEFAULT_FEED_VARIABLES
        (live-probed 2026-09); caller overrides win over both.

        Args:
            variables: Optional top-level overrides merged over the
                resolved base variables; keys the caller does not touch
                keep their verbatim-captured values.

        Returns:
            The typed FeedPage: stories in document order, the opaque
            ``end_cursor`` and ``has_next_page`` pagination flag, plus
            ``raw_size`` so callers can log soft-block signatures. An
            empty feed yields an empty page, never an exception.
        """
        pre_doc_id, pre_vars = self._feed_preload()
        base = pre_vars if pre_vars else dict(DEFAULT_FEED_VARIABLES)
        merged = {**base, **(variables or {})}
        doc_id = pre_doc_id if pre_doc_id else self.doc_id(FEED_QUERY)
        payload = self.client.call(FEED_QUERY, doc_id, merged)
        return parse_feed_page(payload)

    def paginate(self, cursor: str, *,
                 variables: dict[str, Any] | None = None) -> FeedPage:
        """Fetch the next feed page via CometNewsFeedPaginationQuery.

        docs/04 §5: page 2+ replays the base read variables plus the
        opaque ``end_cursor`` token echoed back from the previous page —
        the cursor embeds server-side state and must never be edited.

        Args:
            cursor: The opaque ``end_cursor`` from the previous page's
                FeedPage (docs/04 §5 cursor convention).
            variables: Optional top-level overrides merged last.

        Returns:
            The next typed FeedPage, same walker and fallback semantics
            as read().
        """
        merged = {**DEFAULT_FEED_VARIABLES, "cursor": cursor, **(variables or {})}
        payload = self.client.call(FEED_PAGINATION_QUERY,
                                   self.doc_id(FEED_PAGINATION_QUERY), merged)
        return parse_feed_page(payload)

    # ------------------------------------------------------------------ UFI
    def react(self, feedback: FeedbackID | str, reaction: ReactionType, *,
              feedback_referrer: str = "") -> dict[str, Any]:
        """Apply a reaction to a feedback context (docs/15 §P2-3, live-verified).

        Replays the captured like mutation (assets/captured_mutations.json,
        the LIKE capture): substitute input.feedback_id and
        input.feedback_reaction_id = reaction.reaction_id; the encrypted
        ``tracking`` blobs and session metadata are re-sent verbatim.

        Args:
            feedback: The UFI target — a FeedbackID, b64 global id,
                decoded 'feedback:<numeric>' form, or bare numeric fbid
                (normalized by _normalize_feedback).
            reaction: The ReactionType to apply; its reaction_id rides
                input.feedback_reaction_id (constants.UFI_REACTION_IDS,
                docs/15 §P3-2).
            feedback_referrer: Optional path-style referrer overriding
                the captured input.feedback_referrer.

        Returns:
            The merged mutation response; callers read the new
            reaction_count from it.
        """
        template = _load_template("captured_mutations.json", REACT_MUTATION,
                                  where=lambda v: _is_reaction(v, C.UFI_REACTION_IDS["LIKE"]))
        variables = copy.deepcopy(template)
        variables["input"]["feedback_id"] = _normalize_feedback(feedback)
        variables["input"]["feedback_reaction_id"] = reaction.reaction_id
        # The captured actor_id is scrubbed synthetic data (12345678901234,
        # the post-scrub placeholder): ALWAYS substitute the live viewer -
        # a stale actor is a field_exception on the wire (live-verified
        # breakage 2026-09-20, post-scrub; the pre-scrub captures carried
        # the real id verbatim).
        variables["input"]["actor_id"] = self.session.user_id()
        if feedback_referrer:
            variables["input"]["feedback_referrer"] = feedback_referrer
        return self.client.call(REACT_MUTATION, self._mutation_doc_id(REACT_MUTATION),
                                variables)

    def unreact(self, feedback: FeedbackID | str) -> dict[str, Any]:
        """Remove the viewer's reaction (feedback_reaction_id "0", docs/15 §P2-3).

        Uses the captured REMOVE mutation (feedback_reaction_id == "0") from
        assets/captured_mutations.json; same verbatim tracking replay as
        react() — remove is the same mutation with the zero reaction id,
        not a separate unlike operation.

        Args:
            feedback: The UFI target, same accepted forms as react().

        Returns:
            The merged mutation response with the updated feedback.
        """
        template = _load_template("captured_mutations.json", REACT_MUTATION,
                                  where=lambda v: _is_reaction(v, "0"))
        variables = copy.deepcopy(template)
        variables["input"]["feedback_id"] = _normalize_feedback(feedback)
        # Same post-scrub actor substitution as react(): the captured
        # actor_id is the synthetic placeholder, never the live viewer.
        variables["input"]["actor_id"] = self.session.user_id()
        return self.client.call(REACT_MUTATION, self._mutation_doc_id(REACT_MUTATION),
                                variables)

    def comment(self, feedback: FeedbackID | str, text: str, *,
                group_id: str | None = None) -> dict[str, Any]:
        """Create a comment (docs/15 §P3-3, live-verified in-browser capture).

        Replays assets/captured_comment_mutations.json (the
        useCometUFICreateCommentMutation entry): substitute input.feedback_id,
        input.message = {"ranges": [], "text": text}, a fresh
        client_mutation_id, and the groupID override when given; the
        captured encrypted ``tracking`` blobs ride verbatim.

        Args:
            feedback: The UFI target of the post being commented on,
                same accepted forms as react().
            text: The comment body; rides input.message.text with an
                empty ranges list (no formatting).
            group_id: Optional group scope; overrides the top-level
                groupID variable for group-post comment contexts
                (docs/15 §P3-3 delete-mutation calibration showed the
                group-context variable set is strict).

        Returns:
            The merged mutation response; comment mutations return the
            refreshed comment connection (docs/04 §2.2 ex. 3).
        """
        template = _load_template("captured_comment_mutations.json",
                                  CREATE_COMMENT_MUTATION)
        variables = copy.deepcopy(template)
        variables["input"]["feedback_id"] = _normalize_feedback(feedback)
        variables["input"]["message"] = {"ranges": [], "text": text}
        variables["input"]["client_mutation_id"] = str(uuid4())
        # groupID substitutes ALWAYS, not only when given: the capture is a
        # GROUP-context comment (groupID 12345678901234567, the captured
        # probe-group context — synthetic in-tree; the real probe id is
        # operator-local), and replaying that id against a personal-post
        # feedback is a
        # field_exception 1357010 on the wire (live finding 2026-09-20 -
        # groupID must match the feedback's owning context or be None).
        variables["groupID"] = group_id
        return self.client.call(
            CREATE_COMMENT_MUTATION, self._mutation_doc_id(CREATE_COMMENT_MUTATION),
            variables)

    def delete_comment(self, comment: CommentID | str, *,
                       group_id: str | None = "12345678901234567",
                       render_location: str = "group") -> dict[str, Any]:
        """Delete a comment via CometUFIDeleteCommentMutation (live-verified:
        the captured comment was actually deleted — docs/15 §P3-3).

        The variable envelope below is the VERIFIED live shape — extra
        undeclared variables (e.g. feedLocation) provoke
        ``1675012 noncoercible_variable_value`` (docs/15 §P3-3), so the
        set is exactly the decoded LocalArguments, nothing more.

        Args:
            comment: The comment target — a CommentID, b64 global id,
                decoded 'comment:<post>_<cid>' form, or bare
                '<post_fbid>_<comment_fbid>' pair (normalized by
                _normalize_comment).
            group_id: The owning group's id; the live capture rode a
                group-render context, so the default is the captured
                probe-group context (synthetic in-tree; the real probe
                id is operator-local).
            render_location: The renderLocation enum value ("group" for
                the live-verified group context).

        Returns:
            The merged mutation response carrying
            ``data.comment_delete.deleted_comment_id``.
        """
        variables: dict[str, Any] = {
            "groupID": group_id,
            "inviteShortLinkKey": None,
            "renderLocation": render_location,
            "scale": 2,
            "__relay_internal__pv__groups_comet_use_glvrelayprovider": False,
            "input": {
                "client_mutation_id": "1",
                "comment_id": _normalize_comment(comment),
                "actor_id": self.session.user_id(),
            },
        }
        return self.client.call(
            DELETE_COMMENT_MUTATION, self._mutation_doc_id(DELETE_COMMENT_MUTATION),
            variables)

    def publish(self, text: str, privacy: Privacy, *,
                feed_location: str = "NEWSFEED",
                render_location: str = "homepage_stream",
                group_id: str | None = None,
                ai_generated: bool | None = None,
                text_format_preset_id: str | None = None,
                tags: list[tuple[str, str]] | None = None,
                feeling: str | None = None,
                activity: str | None = None,
                place_id: str | None = None) -> dict[str, Any]:
        """Publish a post via ComposerStoryCreateMutation (docs/15 §P3,
        live-verified: the test post was created and deleted).

        Replays assets/captured_composer.json: set the message text, the
        audience base_state (privacy.value — the live-captured string
        enum), a fresh idempotence_token ("<uuid>_FEED") and
        composer_session_id, the actor id, and the top-level
        feedLocation/renderLocation; groupID is only touched when a
        group_id is given. The enrichment kwargs below ride input
        fields that the live-proven 1675012 coercion gate screens
        cleanly: an unknown field is rejected as a typed
        noncoercible_variable_value with no post created, so a wrong
        candidate shape fails safe (docs/15 §P3-3).

        Args:
            text: The post body; rides input.message.text.
            privacy: The audience selector value; privacy.value rides
                input.audience.privacy.base_state ("EVERYONE"/"FRIENDS"/
                "SELF" — the live-captured string enums).
            feed_location: Top-level feedLocation enum ("NEWSFEED" or
                "GROUP" for group posts, docs/15 §P4-4).
            render_location: Top-level renderLocation enum
                ("homepage_stream" or "group").
            group_id: Optional group scope overriding the top-level
                groupID plus the location enums' group context.
            ai_generated: Optional AI-disclosure toggle; when set, rides
                input.ai_generated_self_disclosure_metadata
                .was_self_disclosed_as_ai_generated (the captured field —
                None leaves the captured value verbatim).
            text_format_preset_id: Optional background-color preset id
                (small-text posts); rides input.text_format_preset_id —
                the captured field, "0" = no background.
            tags: Mention tags as (user_id, display_name) pairs. Each
                pair appends an "@<display_name>" marker to the text
                (space-separated) and a mention range to
                input.message.ranges: {"entity": {"id": <user_id>},
                "length": <marker length>, "offset": <marker start in
                the composed text>, "render_type": "mention"}.
                CANDIDATE SHAPE - UNVERIFIED: mention ranges ride
                input.message.ranges but no captured example exists
                (2026-09-20 calibration: the account has zero friends,
                so live verification was impossible).
            feeling: Feeling target id; rides input.target_type="FEELING"
                plus input.target_id. CANDIDATE SHAPE - UNVERIFIED: the
                captured post carried no feeling, so the field pair is a
                legacy-input candidate; a wrong candidate fails safe via
                the coercion gate (typed error, no post created).
            activity: Activity target id; rides
                input.target_type="ACTIVITY" plus input.target_id.
                CANDIDATE SHAPE - UNVERIFIED — same calibration status
                as ``feeling``.
            place_id: Check-in place id; rides input.place_id. CANDIDATE
                SHAPE - UNVERIFIED — same calibration status as
                ``feeling``.

        Returns:
            The merged mutation response of the created story.

        Raises:
            ValueError: When ``feeling`` and ``activity`` are both set
                (they share input.target_type/target_id), or when a tag
                pair carries an empty user id or display name (an empty
                marker would mispoint the mention range).
        """
        if feeling is not None and activity is not None:
            raise ValueError("feeling and activity are mutually exclusive "
                             "(both ride input.target_type/target_id)")
        variables = copy.deepcopy(load_template(
            "captured_composer.json", COMPOSER_MUTATION))
        composed = text
        ranges: list[dict[str, Any]] = []
        for user_id, display_name in tags or []:
            if not user_id or not display_name:
                raise ValueError(
                    "tag pair must be (user_id, display_name), both "
                    f"non-empty — got ({user_id!r}, {display_name!r})")
            marker = f"@{display_name}"
            composed = f"{composed} {marker}" if composed else marker
            ranges.append({
                "entity": {"id": user_id},
                "length": len(marker),
                "offset": len(composed) - len(marker),
                "render_type": "mention",
            })
        variables["input"]["message"]["text"] = composed
        if ranges:
            variables["input"]["message"]["ranges"] = ranges
        variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = str(uuid4())
        variables["input"]["actor_id"] = self.session.user_id()
        variables["feedLocation"] = feed_location
        variables["renderLocation"] = render_location
        if group_id is not None:
            variables["groupID"] = group_id
        if ai_generated is not None:
            variables["input"]["ai_generated_self_disclosure_metadata"][
                "was_self_disclosed_as_ai_generated"] = ai_generated
        if text_format_preset_id is not None:
            variables["input"]["text_format_preset_id"] = text_format_preset_id
        if feeling is not None:
            variables["input"]["target_type"] = "FEELING"
            variables["input"]["target_id"] = feeling
        if activity is not None:
            variables["input"]["target_type"] = "ACTIVITY"
            variables["input"]["target_id"] = activity
        if place_id is not None:
            variables["input"]["place_id"] = place_id
        return self.client.call(COMPOSER_MUTATION,
                                self._mutation_doc_id(COMPOSER_MUTATION), variables)

    # ------------------------------------------------------------------ edit
    def fetch_editable(self, post_id: str) -> EditablePost:
        """Fetch one post's editable state (CometEditFeedComposerDialogQuery).

        Doc_id 38644593395156159 from the fresh full harvest
        (data/doc_id_registry_v3.json, revision 1047963790). The variables
        are the CANDIDATE EDIT_DIALOG_VARIABLES plus the per-post
        EDIT_DIALOG_STORY_KEY — both module constants are the
        probe-correctable seam: when the orchestrator's live probe fires,
        it adjusts them in ONE place and every call follows.

        Args:
            post_id: The target post's numeric id (the story node's
                ``post_id``, as :meth:`publish` echoes at
                ``story_create.feed_story_edge.node.post_id``).

        Returns:
            The typed EditablePost — a DEFENSIVE parse of the dialog
            response: every member the payload does not carry reads as
            absent (None/empty), never an exception. The parser walks
            known composer-family keys only; the live probe refines it.
        """
        variables = {**EDIT_DIALOG_VARIABLES, EDIT_DIALOG_STORY_KEY: post_id}
        payload = self.client.call(EDIT_DIALOG_QUERY,
                                   self.doc_id(EDIT_DIALOG_QUERY), variables)
        return parse_editable_post(payload)

    def edit(self, post_id: str, *,
             text: str | None = None,
             privacy: Privacy | None = None,
             ai_generated: bool | None = None,
             text_format_preset_id: str | None = None,
             tags: list[tuple[str, str]] | None = None,
             feeling: str | None = None,
             activity: str | None = None,
             place_id: str | None = None,
             attachments: list[str] | None = None) -> dict[str, Any]:
        """Edit one existing post via ComposerStoryEditMutation.

        Doc_id 27456358844037239 from the fresh full harvest
        (data/doc_id_registry_v3.json, revision 1047963790). NO captured
        edit variables exist, so the mutation variables are built FROM
        THE CREATE-SIDE TEMPLATE (a deep copy of the captured
        ComposerStoryCreateMutation entry — the structural sibling) with
        the story identifier set at the CANDIDATE EDIT_STORY_ID_PATH.
        An edit is a DELTA: every omitted kwarg leaves the captured
        template value untouched — only what the operator passes
        changes. A wrong candidate shape fails safe through the 1675012
        coercion gate (typed error, no edit applied — docs/15 §P3-3).

        Args:
            post_id: The target post's numeric id (same form as
                :meth:`set_privacy` accepts).
            text: Replacement post text; rides input.message.text. When
                ``tags`` are given, the mention markers append to THIS
                text (never to the unknown live text — an edit cannot
                compose ranges against text it does not have).
            privacy: The new audience; privacy.value rides
                input.audience.privacy.base_state (the live-captured
                string enums). Omitted -> the captured value stands.
            ai_generated: Optional AI-disclosure toggle; rides the
                captured input.ai_generated_self_disclosure_metadata
                bool. Omitted -> captured value.
            text_format_preset_id: Background-color preset id; rides the
                captured input.text_format_preset_id ("0" = none).
            tags: Mention tags as (user_id, display_name) pairs, same
                range construction as :meth:`publish` — CANDIDATE SHAPE
                - UNVERIFIED (no captured example, 2026-09-20
                calibration). Requires ``text``.
            feeling: Feeling target id; rides the input.target_type/
                target_id candidate pair — CANDIDATE SHAPE - UNVERIFIED,
                same calibration status as ``publish``'s.
            activity: Activity target id; same candidate pair as
                ``feeling``, mutually exclusive with it.
            place_id: Check-in place id; rides input.place_id.
                CANDIDATE SHAPE - UNVERIFIED.
            attachments: ALREADY-UPLOADED photo ids to attach (the
                caller uploads first via the upload surfaces and passes
                the ids — this method does no uploading). Each id rides
                one ``{"photo": {"id": ...}}`` element of
                input.attachments, the live-verified create-side attach
                shape (upload.py post_photo). CANDIDATE on the edit
                mutation - UNVERIFIED: the edit sibling's acceptance of
                input.attachments is probe pending.

        Returns:
            The merged ComposerStoryEditMutation response.

        Raises:
            ValueError: When ``feeling`` and ``activity`` are both set
                (they share input.target_type/target_id), when ``tags``
                are given without ``text`` (mention ranges are built
                against the composed text, and the edit delta has no
                other source for it), or when a tag pair carries an
                empty user id or display name.
        """
        if feeling is not None and activity is not None:
            raise ValueError("feeling and activity are mutually exclusive "
                             "(both ride input.target_type/target_id)")
        if tags and text is None:
            raise ValueError("tags require text: mention ranges are built "
                             "against the composed text, and an edit delta "
                             "has no other source for it")
        variables = copy.deepcopy(load_template(
            "captured_composer.json", COMPOSER_MUTATION))
        if text is not None:
            composed = text
            ranges: list[dict[str, Any]] = []
            for user_id, display_name in tags or []:
                if not user_id or not display_name:
                    raise ValueError(
                        "tag pair must be (user_id, display_name), both "
                        f"non-empty — got ({user_id!r}, {display_name!r})")
                marker = f"@{display_name}"
                composed = f"{composed} {marker}" if composed else marker
                ranges.append({
                    "entity": {"id": user_id},
                    "length": len(marker),
                    "offset": len(composed) - len(marker),
                    "render_type": "mention",
                })
            variables["input"]["message"]["text"] = composed
            if ranges:
                variables["input"]["message"]["ranges"] = ranges
        if privacy is not None:
            variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        if ai_generated is not None:
            variables["input"]["ai_generated_self_disclosure_metadata"][
                "was_self_disclosed_as_ai_generated"] = ai_generated
        if text_format_preset_id is not None:
            variables["input"]["text_format_preset_id"] = text_format_preset_id
        if feeling is not None:
            variables["input"]["target_type"] = "FEELING"
            variables["input"]["target_id"] = feeling
        if activity is not None:
            variables["input"]["target_type"] = "ACTIVITY"
            variables["input"]["target_id"] = activity
        if place_id is not None:
            variables["input"]["place_id"] = place_id
        if attachments is not None:
            variables["input"]["attachments"] = [
                {"photo": {"id": photo_id}} for photo_id in attachments]
        # The story identifier rides the CANDIDATE path; fresh
        # idempotence/composer-session tokens and the actor id are
        # minted per call exactly like publish — a replayed captured
        # idempotence token would collide server-side with the original
        # create.
        # storyID wants the STORY TOKEN, not the numeric post id (live
        # probe 2026-09-20: numeric -> noncoercible_variable_value): the
        # token form - decoded from the create echo's feed_story_edge
        # .node.id - is b64("S:_I<actor_id>:<post_id>:<post_id>").
        story_token = base64.b64encode(
            f"S:_I{self.session.user_id()}:{post_id}:{post_id}".encode()).decode()
        _set_by_path(variables, EDIT_STORY_ID_PATH, story_token)
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = str(uuid4())
        variables["input"]["actor_id"] = self.session.user_id()
        return self.client.call(EDIT_MUTATION,
                                self._mutation_doc_id(EDIT_MUTATION), variables)

    def life_event_categories(self) -> list[dict[str, Any]]:
        """List the composer's life-event categories with their event types.

        The LIVE-CALIBRATED query (2026-09-20):
        CometComposerLifeEventCategoryListQuery, doc_id 32116841221248541,
        variables ``{"scale": 1}``, answered with the category tree at
        ``data.viewer.life_event_categories.nodes`` (docs/15 §P3 composer
        ground truth). The doc_id is pinned from that calibration — not
        resolved through the harvested registry, whose older harvest may
        not carry this deploy's id.

        The publish-side input for life events is UNKNOWN and
        deliberately not invented; this listing is the only grounded
        piece of the life-event family.

        Returns:
            One typed row per category — ``{id, name, icon_id, types}``
            with each type as ``{id, identifier, name}`` — in document
            order; an absent or empty viewer yields ``[]``.
        """
        payload = self.client.call(LIFE_EVENT_CATEGORIES_QUERY,
                                   LIFE_EVENT_CATEGORIES_DOC_ID, {"scale": 1})
        return parse_life_event_categories(payload)

    def set_privacy(self, post_id: str, privacy: Privacy) -> dict[str, Any]:
        """Change one existing post's audience (live-verified 2026-09-20).

        The per-post privacy write id is the story's own scope:
        ``b64('privacy_scope_renderer:{"id":<post_id>}')`` - the scope
        the picker query (CometPrivacySelectorPickerContainerQuery)
        RESOLVED live for a published feed story: probed with that
        candidate, the server echoed the same write id and returned the
        post's actual options with the then-current audience selected.

        Replays the VERIFIED captured save template
        (captured_composer.json's CometPrivacySelectorSavePrivacyMutation
        entry - the same wire shape the settings default-privacy save
        rides, docs/15 §P3): base_state, the per-post write id, the
        actor id and a fresh client_mutation_id are substituted; the
        render-location fields and relay-provider gates re-ride
        verbatim.

        Args:
            post_id: The target post's numeric id (the story node's
                ``post_id``, as returned by :meth:`publish` at
                ``story_create.feed_story_edge.node.post_id``).
            privacy: The new audience; privacy.value rides
                input.privacy_row_input.base_state ("EVERYONE"/
                "FRIENDS"/"SELF" - the live-captured string enums).

        Returns:
            The merged CometPrivacySelectorSavePrivacyMutation
            response; verify the change took effect by re-reading the
            post's scope through the picker query (the selected option
            flips to the new base_state).
        """
        # The scope id rides UNQUOTED inside the b64 JSON (captured shape:
        # privacy_scope_renderer:{"id":8787670733} for the default scope;
        # the per-story form verified live with the post's own id).
        scope = f'privacy_scope_renderer:{{"id":{post_id}}}'
        variables = copy.deepcopy(load_template(
            "captured_composer.json", PRIVACY_SAVE_MUTATION))
        variables["input"]["privacy_row_input"]["base_state"] = privacy.value
        variables["input"]["privacy_write_id"] = base64.b64encode(
            scope.encode()).decode()
        variables["input"]["actor_id"] = self.session.user_id()
        variables["input"]["client_mutation_id"] = str(uuid4())
        return self.client.call(PRIVACY_SAVE_MUTATION,
                                self._mutation_doc_id(PRIVACY_SAVE_MUTATION),
                                variables)

    def typing(self, feedback: FeedbackID | str,
               state: Literal["start", "stop"], *,
               session_id: str | None = None) -> dict[str, Any]:
        """Broadcast a live-typing signal (docs/15 §P3-3 companion mutation).

        state="start" fires the Start mutation, "stop" the Stop mutation
        (doc_ids picked from KNOWN_MUTATIONS by that name suffix). The
        typing session id defaults to a fresh uuid per broadcast burst.

        Args:
            feedback: The UFI target being typed into, same accepted
                forms as react().
            state: "start" or "stop" — selects the paired mutation.
            session_id: Optional typing-session id shared across one
                burst; defaults to a fresh uuid per call.

        Returns:
            The merged mutation response (an ack envelope; the signal
            is fire-and-forget in the captured client too).

        Raises:
            ValueError: If ``state`` is neither "start" nor "stop".
        """
        if state not in ("start", "stop"):
            raise ValueError(f"state must be 'start' or 'stop', got {state!r}")
        friendly = (TYPING_START_MUTATION if state == "start"
                    else TYPING_STOP_MUTATION)
        variables: dict[str, Any] = {
            "input": {
                "feedback_id": _normalize_feedback(feedback),
                "session_id": session_id or str(uuid4()),
                "actor_id": self.session.user_id(),
                "client_mutation_id": "1",
            },
        }
        return self.client.call(friendly, self._mutation_doc_id(friendly),
                                variables)

    # ---------------------------------------------------------- 3-dot share
    def share(self, post_id: str, text: str = "",
              privacy: Privacy = Privacy.FRIENDS) -> dict[str, Any]:
        """Share one post to the viewer's own timeline (docs/15 §P4 recon).

        Rides the SAME live-verified ComposerStoryCreateMutation template
        publish() replays (captured_composer.json): the share rides
        input.attachments — the list itself is live-grounded by the photo
        attach ({"photo": {"id": ...}}, surfaces/upload.py) — with the
        member built from :data:`SHARE_ATTACHMENT_CANDIDATE`
        (CANDIDATE SHAPE - UNVERIFIED, probe pending: the dialog probes
        ShareToFeedComposerCometDialogQuery / CometUnifiedShareSheetDialogQuery
        must confirm the field name). A wrong candidate fails safe
        through the composer's 1675012 coercion gate (typed error, no
        post created, docs/15 §P3-3). ``text`` rides the composer message
        as the share comment; ``privacy`` the share's audience.

        Args:
            post_id: The shared post's numeric id.
            text: Optional share comment; rides input.message.text
                (empty string = share without a comment).
            privacy: The shared post's audience; privacy.value rides
                input.audience.privacy.base_state ("EVERYONE"/"FRIENDS"/
                "SELF" — the live-captured string enums).

        Returns:
            The merged mutation response of the created share story.
        """
        variables = copy.deepcopy(load_template(
            "captured_composer.json", COMPOSER_MUTATION))
        variables["input"]["message"]["text"] = text
        variables["input"]["audience"]["privacy"]["base_state"] = privacy.value
        variables["input"]["idempotence_token"] = f"{uuid4()}_FEED"
        variables["input"]["logging"]["composer_session_id"] = str(uuid4())
        variables["input"]["actor_id"] = self.session.user_id()
        attachment = copy.deepcopy(SHARE_ATTACHMENT_CANDIDATE)
        attachment["share"]["shareable_id"] = post_id
        variables["input"]["attachments"] = [attachment]
        return self.client.call(COMPOSER_MUTATION,
                                self._mutation_doc_id(COMPOSER_MUTATION),
                                variables)

    # ---------------------------------------------------- 3-dot notifications
    def notify(self, actor_id: str,
               state: Literal["on", "off"]) -> dict[str, Any]:
        """Turn post notifications from one actor on or off.

        The actor-scoped subscribe pair — the 3-dot menu's "turn on/off
        notifications": state="on" fires CommitActorSubscribeStatusSubscribe
        Mutation, "off" the Unsubscribe sibling (registry-grounded v3
        "carried" doc_ids, resolved through _mutation_doc_id). Variables
        are built from :data:`NOTIFY_VARIABLES_CANDIDATE`
        (CANDIDATE VARIABLES - UNVERIFIED, probe pending: no captured
        example exists; the live probe must confirm the field set).

        Args:
            actor_id: The actor (user/page) whose posts to (un)subscribe.
            state: "on" or "off" — selects the paired mutation.

        Returns:
            The merged mutation response (an ack envelope).

        Raises:
            ValueError: If ``state`` is neither "on" nor "off".
        """
        if state not in ("on", "off"):
            raise ValueError(f"state must be 'on' or 'off', got {state!r}")
        friendly = (NOTIFY_SUBSCRIBE_MUTATION if state == "on"
                    else NOTIFY_UNSUBSCRIBE_MUTATION)
        variables = copy.deepcopy(NOTIFY_VARIABLES_CANDIDATE)
        variables["input"]["actor_id"] = actor_id
        variables["input"]["client_mutation_id"] = str(uuid4())
        return self.client.call(friendly, self._mutation_doc_id(friendly),
                                variables)
