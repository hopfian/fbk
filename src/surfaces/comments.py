"""DEEP-COMMENTS surface service: read a post's comment tree and act on
individual comments (docs/02 §2, docs/04 §6 UFI object model, docs/15).

ARCHITECTURE:

  Reads follow the any-surface-page law (docs/15 §P3-5): GET the
  permalink page, harvest the SSR preload registration, replay it
  verbatim. Writes replay the live-verified captured mutation templates
  (assets/captured_mutations.json) with only the comment-level feedback
  id and reaction substituted — the encrypted ``tracking`` blobs ride
  untouched, the same verbatim-replay invariant FeedService enforces.
  Comment CREATE stays in FeedService.comment (docs/15 §P3-3); this
  service owns reading, reacting, editing, deleting and voting.

CALIBRATION NOTES — every shape below is live ground truth, verified
2026-09 on a public sample-page permalink:

* READ: GET the permalink page -> preloads carry
  ``CometSinglePostDialogContentQuery`` (doc_id 28700792632890407) with the
  VERBATIM variables the server itself used (including the b64 ``storyID``
  story key, docs/04 §7). Replaying it yields a ~600KB payload whose
  comment nodes ride at
  ``node_v2.comet_sections.feedback.story.story_ufi_container.story
  .feedback_context.feedback_target_with_context.comment_list_renderer
  .feedback.comment_rendering_instance_for_feed_location.comments.edges``.
  Each ``Comment`` node: b64 ``id`` ('comment:<post>_<cid>'), ``author``,
  ``body.text`` and its own ``feedback`` object whose b64 id is
  'feedback:<post>_<cid>' — the comment-level reaction target (docs/04 §6).
  Reply-expander stub nodes (``{__typename, feedback, id,
  inline_replies_expander_renderer}``) are skipped.
* REACT: the live-verified CometUFIFeedbackReactMutation templates in
  assets/captured_mutations.json (docs/15 §P2-3), substituting the COMMENT's
  own feedback id; ``feedback_source`` for the comment context is "OBJECT"
  (decoded from the live comment-create capture in
  assets/captured_comment_mutations.json — the captured "NEWS_FEED" value
  is the feed-story context, the comment context sends "OBJECT"). The
  like->remove pair was live-verified on the post's top comment.
* EDIT: ``useCometUFIEditCommentMutation`` (registry doc_id 28765227863112159)
  — schema-decoded from its owning bundle: LocalArguments
  {feedLocation, input, scale, translationType, useDefaultActor} and the
  input {attachments, attribution_id_v2, comment_id, formatting_style,
  message, tracking} built by CometUFICommentEditor's commit handler.
* DELETE: the LIVE-PROVEN ``CometUFIDeleteCommentMutation`` shape
  (KNOWN_MUTATIONS doc_id 28058620387108821, docs/15 §P3-3 — the captured
  comment was actually deleted). The registry's hook-based sibling
  ``useCometUFIDeleteCommentMutation`` (27386493047638332) also decodes
  cleanly, but the 28058620387108821 one is live-proven, so it wins.
* VOTE/UNVOTE: ``useCometUFICommentVoteMutation`` (33995920373387348) with
  ``{input: {comment_id, new_vote_state: UPVOTE|DOWNVOTE}}`` and
  ``useCometUFICommentUnvoteMutation`` (25722792667420652) with
  ``{input: {comment_id}}`` — both decoded cleanly from the comment
  action-link bundles, so they are wired.

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import copy
import re
import time
from collections.abc import Callable
from typing import Any, Literal

import constants as C
from auth.bootstrap import extract_preload_registry
from domain.common import (
    Actor,
    Comment,
    CommentID,
    FeedbackID,
    ReactionType,
)

from .base import Surface, _is_reaction, _walk_preorder, load_template

# --------------------------------------------------------------------- names
POST_CONTENT_QUERY = "CometSinglePostDialogContentQuery"
REACT_MUTATION = "CometUFIFeedbackReactMutation"
EDIT_MUTATION = "useCometUFIEditCommentMutation"
DELETE_MUTATION = "CometUFIDeleteCommentMutation"   # live-proven shape
VOTE_MUTATION = "useCometUFICommentVoteMutation"
UNVOTE_MUTATION = "useCometUFICommentUnvoteMutation"

#: The comment-context value of the react mutation's feedback_source enum,
#: decoded from the live comment-create capture (assets/
#: captured_comment_mutations.json) and live-verified by the like->remove
#: calibration on the post's top comment (docs/15).
COMMENT_FEEDBACK_SOURCE = "OBJECT"

#: Base64 of "comment:" — marks a Relay comment global id (docs/04 §7).
_COMMENT_ID_PREFIX = "Y29tbWVudD"

# -------------------------------------------------------- captured templates
def _load_template(asset_name: str, friendly: str,
                   *, where: Callable[[dict[str, Any]], bool] | None = None,
                   ) -> dict[str, Any]:
    """Deep-copied captured mutation variables for ``friendly``.

    Thin delegate to :func:`surfaces.base.load_template` (docs/15 §P2-3):
    same shared cache, same scan, same deep-copy semantics — and, via
    base's default ``check_asset=True``, the exact missing-asset
    RuntimeError (``captured template asset missing: <path>``) this
    module has always raised.
    """
    return load_template(asset_name, friendly, where=where)


# ---------------------------------------------------------------- id helpers
def _normalize_feedback(feedback_id: str) -> str:
    """Comment feedback target -> the wire (base64) form.

    Accepts a b64 global id ('ZmVlZGJhY2s6…', padded or not), a decoded
    'feedback:<post>_<cid>' form (the comment-level feedback prefix), or a
    bare numeric fbid (docs/04 §7).
    """
    raw = str(feedback_id)
    if raw.startswith("feedback:"):
        inner = raw.split(":", 1)[1]
        return base64.b64encode(
            f"feedback:{inner}".encode()).decode().rstrip("=")
    if raw.isdigit():
        return FeedbackID.from_numeric(raw).raw
    return raw


def _normalize_comment(comment: CommentID | str) -> str:
    """Comment target -> the wire (base64) form (docs/04 §7)."""
    if isinstance(comment, CommentID):
        return comment.raw
    raw = str(comment)
    if raw.startswith("comment:"):
        post, _, cid = raw.split(":", 1)[1].rpartition("_")
        return CommentID.from_numeric(post, cid).raw
    if re.fullmatch(r"\d+_\d+", raw):
        post, _, cid = raw.rpartition("_")
        return CommentID.from_numeric(post, cid).raw
    return CommentID.from_b64(raw).raw


def _attribution(surface: str = "CometSinglePostDialogRoot.react") -> str:
    """A minified-product attribution string (docs/15 §P2-3 wire shape)."""
    return (f"{surface},comet.post.single_dialog,via_cold_start,"
            f"{int(time.time() * 1000)},,,,;")


def parse_comments(payload: dict[str, Any]) -> list[Comment]:
    """Merged post-content payload -> typed Comments, document order.

    A node counts as a comment when it has ``__typename == "Comment"``, a
    b64 comment global id, and an author or a body — which skips the
    reply-expander stubs the payload also carries.

    Args:
        payload: The merged CometSinglePostDialogContentQuery response
            document (streamed chunks already combined).

    Returns:
        Typed Comment rows in document order, deduped on the b64 comment
        global id; an empty payload yields an empty list, never an
        exception.
    """
    out: list[Comment] = []
    seen: set[str] = set()
    for node in _walk_preorder(payload):
        if node.get("__typename") != "Comment":
            continue
        cid = node.get("id")
        if not isinstance(cid, str) or not cid.startswith(_COMMENT_ID_PREFIX):
            continue
        author = node.get("author")
        body = node.get("body")
        if not isinstance(author, dict) and not isinstance(body, dict):
            continue  # reply-expander stub
        if cid in seen:
            continue
        seen.add(cid)
        text: str | None = None
        if isinstance(body, dict):
            val = body.get("text")
            text = val if isinstance(val, str) else None
        actor: Actor | None = None
        if isinstance(author, dict) and author.get("id"):
            actor = Actor.model_validate({
                "__typename": str(author.get("__typename", "User")),
                "id": str(author["id"]),
                "name": (str(author["name"])
                         if isinstance(author.get("name"), str) else None),
            })
        out.append(Comment(id=CommentID.from_b64(cid), actor=actor,
                           text=text))
    return out


class CommentsService(Surface):
    """The deep-comments surface: read + react/edit/delete/vote (docs/02 §2).

    Reads replay the permalink page's own preload registration
    (docs/15 §P2-2); writes replay the live-verified captured templates
    with only the comment-level feedback id / reaction / text
    substituted — the encrypted tracking blobs ride untouched.
    """

    # ------------------------------------------------------------------ reads
    def read(self, permalink: str, *, limit: int = 30) -> list[Comment]:
        """Read a post's comments (incl. replies) from its permalink.

        GETs the permalink page, harvests the CometSinglePostDialogContentQuery
        preload (verbatim variables + doc_id, docs/15 §P2-2) and replays it;
        the merged payload is walked into typed ``Comment`` rows (b64
        CommentID decoded, author, body text).

        Args:
            permalink: The post's FULL permalink URL — the page GET is
                the doc_id/variable source (docs/15 §P2-2); there is no
                offline template for this surface.
            limit: Maximum number of comment rows to return; the walk
                is sliced locally so the wire shape is never edited.

        Returns:
            Up to ``limit`` typed Comments in document order. Each
            comment's reaction target is its OWN feedback id — the b64
            ``feedback:<post>_<cid>`` global id found next to each
            comment node (docs/04 §6) — to be passed to react().

        Raises:
            ValueError: If ``permalink`` is not an http(s) URL.
            RuntimeError: If the permalink page carries no
                CometSinglePostDialogContentQuery preload — the surface
                moved; re-probe per docs/13 §2.
        """
        if not str(permalink).startswith("http"):
            raise ValueError(
                "read() needs the post's full permalink URL — the page GET "
                "is the doc_id/variable source (docs/15 §P2-2); use "
                "FeedService for feedback-id-only flows")
        html = self._fetch(str(permalink))
        entry = None
        for candidate in extract_preload_registry(html):
            if candidate.query_name == POST_CONTENT_QUERY:
                entry = candidate
                break
        if entry is None:
            raise RuntimeError(
                "permalink page carried no CometSinglePostDialogContentQuery "
                "preload — the surface moved; re-probe per docs/13 §2")
        data = self.client.call(POST_CONTENT_QUERY, entry.doc_id,
                                dict(entry.variables))
        return parse_comments(data)[:limit]

    # ---------------------------------------------------------------- UFI
    def react(self, feedback_id: str, reaction: ReactionType, *,
              feedback_source: str | None = None) -> dict[str, Any]:
        """Apply a reaction to a COMMENT's feedback context.

        ``feedback_id`` is the comment's OWN feedback id — the b64
        'ZmVlZGJhY2s6<post>_<cid>' global id found in each comment node's
        feedback object (docs/04 §6), NOT the post's feedback id. Replays
        the live-verified like capture (assets/captured_mutations.json) with
        the substituted id and reaction; the encrypted ``tracking`` blobs
        are re-sent verbatim.

        Args:
            feedback_id: The comment-level feedback target (b64 global
                id, decoded 'feedback:<post>_<cid>' form, or bare
                numeric fbid — normalized by _normalize_feedback).
            reaction: The ReactionType to apply; its reaction_id rides
                input.feedback_reaction_id (docs/15 §P3-2).
            feedback_source: Overrides the react mutation's
                feedback_source enum; defaults to the decoded
                comment-context value "OBJECT" (live-verified), e.g.
                the captured "NEWS_FEED" for feed-story contexts.

        Returns:
            The merged mutation response with the updated comment
            feedback (viewer_feedback_reaction_info, reaction counts).
        """
        template = _load_template(
            "captured_mutations.json", REACT_MUTATION,
            where=lambda v: _is_reaction(v, C.UFI_REACTION_IDS["LIKE"]))
        variables = copy.deepcopy(template)
        variables["input"]["feedback_id"] = _normalize_feedback(feedback_id)
        variables["input"]["feedback_reaction_id"] = reaction.reaction_id
        if feedback_source is not None:
            variables["input"]["feedback_source"] = feedback_source
        else:
            variables["input"]["feedback_source"] = COMMENT_FEEDBACK_SOURCE
        return self.client.call(
            REACT_MUTATION, self._mutation_doc_id(REACT_MUTATION), variables)

    def unreact(self, feedback_id: str, *,
                feedback_source: str | None = None) -> dict[str, Any]:
        """Remove the viewer's reaction on a comment (reaction id "0").

        Same feedback-id semantics and verbatim tracking replay as
        react(); replays the REMOVE capture (the second live-verified
        template in assets/captured_mutations.json) with reaction id "0".

        Args:
            feedback_id: The comment-level feedback target, same
                accepted forms as react().
            feedback_source: Same override semantics as react();
                defaults to the comment-context "OBJECT".

        Returns:
            The merged mutation response with the updated comment
            feedback.
        """
        template = _load_template(
            "captured_mutations.json", REACT_MUTATION,
            where=lambda v: _is_reaction(v, "0"))
        variables = copy.deepcopy(template)
        variables["input"]["feedback_id"] = _normalize_feedback(feedback_id)
        if feedback_source is not None:
            variables["input"]["feedback_source"] = feedback_source
        else:
            variables["input"]["feedback_source"] = COMMENT_FEEDBACK_SOURCE
        return self.client.call(
            REACT_MUTATION, self._mutation_doc_id(REACT_MUTATION), variables)

    def edit(self, comment: CommentID | str, text: str) -> dict[str, Any]:
        """Edit a comment's text (useCometUFIEditCommentMutation).

        Replays the schema-decoded envelope from the CometUFICommentEditor
        commit handler: the input {attachments, attribution, comment_id,
        formatting_style PLAIN_TEXT, message, tracking} plus the decoded
        LocalArguments (feedLocation, scale, translationType,
        useDefaultActor and the three relay provider gates).

        Args:
            comment: The comment target — a CommentID, b64 global id,
                decoded 'comment:<post>_<cid>' form, or bare
                '<post_fbid>_<comment_fbid>' pair.
            text: The replacement comment body; rides input.message.text
                with an empty ranges list and PLAIN_TEXT formatting.

        Returns:
            The merged mutation response carrying the edited comment.
        """
        variables: dict[str, Any] = {
            "feedLocation": "NEWSFEED",
            "input": {
                "attachments": None,
                "attribution_id_v2": _attribution(),
                "comment_id": _normalize_comment(comment),
                "formatting_style": "PLAIN_TEXT",
                "message": {"ranges": [], "text": text},
                "tracking": [],
            },
            "scale": 1,
            "translationType": "ORIGINAL",
            "useDefaultActor": False,
            "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
            "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
            "__relay_internal__pv__IsWorkUserrelayprovider": False,
        }
        return self.client.call(EDIT_MUTATION, self.doc_id(EDIT_MUTATION),
                                variables)

    def delete(self, comment: CommentID | str, *,
               render_location: str = "group") -> dict[str, Any]:
        """Delete a comment via the LIVE-PROVEN CometUFIDeleteCommentMutation
        (KNOWN_MUTATIONS doc_id 28058620387108821 — the captured comment was
        actually deleted, docs/15 §P3-3).

        The variable envelope below is the verified live shape — extra
        undeclared variables provoke ``1675012
        noncoercible_variable_value`` (docs/15 §P3-3), so the set is
        exactly the decoded LocalArguments.

        Args:
            comment: The comment target, same accepted forms as edit().
            render_location: The renderLocation enum value ("group" for
                the live-verified group-render context).

        Returns:
            The merged mutation response carrying
            ``data.comment_delete.deleted_comment_id``.
        """
        variables: dict[str, Any] = {
            "groupID": "12345678901234567",
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
            DELETE_MUTATION, self._mutation_doc_id(DELETE_MUTATION),
            variables)

    # --------------------------------------------------------------- votes
    def vote(self, comment: CommentID | str,
             state: Literal["UPVOTE", "DOWNVOTE"]) -> dict[str, Any]:
        """Cast a Q&A-style vote on a comment (useCometUFICommentVoteMutation).

        Decoded hook input: ``{input: {comment_id, new_vote_state}}``.

        Args:
            comment: The comment target, same accepted forms as edit().
            state: The vote enum — "UPVOTE" or "DOWNVOTE".

        Returns:
            The merged mutation response; viewer_comment_vote_state on
            the response comment confirms the new state.

        Raises:
            ValueError: If ``state`` is not one of the two vote enums.
        """
        if state not in ("UPVOTE", "DOWNVOTE"):
            raise ValueError(f"vote state must be UPVOTE or DOWNVOTE, got {state!r}")
        variables: dict[str, Any] = {
            "input": {
                "comment_id": _normalize_comment(comment),
                "new_vote_state": state,
            },
        }
        return self.client.call(VOTE_MUTATION, self.doc_id(VOTE_MUTATION),
                                variables)

    def unvote(self, comment: CommentID | str) -> dict[str, Any]:
        """Remove the viewer's vote (useCometUFICommentUnvoteMutation).

        Decoded hook input: ``{input: {comment_id}}``.

        Args:
            comment: The comment target, same accepted forms as edit().

        Returns:
            The merged mutation response; the response comment's
            viewer_comment_vote_state returns to NONE.
        """
        variables: dict[str, Any] = {
            "input": {"comment_id": _normalize_comment(comment)},
        }
        return self.client.call(UNVOTE_MUTATION, self.doc_id(UNVOTE_MUTATION),
                                variables)
