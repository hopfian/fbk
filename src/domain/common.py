"""Typed domain models — every Facebook entity the CLI handles.

Identifier semantics follow docs/14-glossary-and-reference.md:
  * numeric fbids      — 15-17 digit integers (legacy, still used by groups)
  * global (Relay) ids — base64 of "prefix:<numbers>" (feedback:…, comment:…)
  * pfbid              — opaque story identifiers introduced 2022 (base64url)
  * cursors            — opaque connection tokens (page_info.end_cursor)

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

import constants as C


class UnknownNameError(KeyError, ValueError):
    """A from_name() lookup missed. Subclasses both KeyError (the historical
    raw failure) and ValueError (enum semantics) so either handler — or a
    pytest.raises pin on either — keeps working; the message lists valid names."""


class ReactionType(enum.StrEnum):
    """UFI reactions; the enum VALUE is the wire feedback_reaction_id.

    The seven live-harvested reaction ids (docs/15 §P3-2,
    ``constants.UFI_REACTION_IDS``) — the mutation takes the id string
    directly, so this StrEnum serializes into mutation inputs without a
    translation layer. Removal of an existing reaction is NOT a member:
    it is the sentinel id ``"0"`` on the same mutation (docs/15 §P2-3).

    A StrEnum (not bare Enum) so f-string/JSON emission yields the
    ready-to-ship id, never ``ReactionType.LIKE``.
    """

    LIKE = C.UFI_REACTION_IDS["LIKE"]
    LOVE = C.UFI_REACTION_IDS["LOVE"]
    WOW = C.UFI_REACTION_IDS["WOW"]
    HAHA = C.UFI_REACTION_IDS["HAHA"]
    SORRY = C.UFI_REACTION_IDS["SORRY"]
    ANGER = C.UFI_REACTION_IDS["ANGER"]
    SUPPORT = C.UFI_REACTION_IDS["SUPPORT"]

    @classmethod
    def from_name(cls, name: str) -> ReactionType:
        """Case-insensitive CLI-name lookup (LIKE, love, ANGER, ...).

        Args:
            name: User-supplied reaction name; whitespace-stripped and
                upper-cased before the table lookup.

        Returns:
            The matching member, whose ``.value`` is the mutation-ready
            ``feedback_reaction_id``.

        Raises:
            UnknownNameError: When no reaction matches — the message
                lists every valid name so the operator can self-correct.
        """
        try:
            return cls(C.UFI_REACTION_IDS[name.strip().upper()])
        except (KeyError, ValueError):
            raise UnknownNameError(
                f"unknown reaction {name!r} — expected one of: "
                + ", ".join(m.name for m in cls)) from None

    @property
    def reaction_id(self) -> str:
        """The mutation-ready id (== .value)."""
        return self.value


class Privacy(enum.StrEnum):
    """Post-audience privacy. Value is the live-captured `base_state` string
    (doc 15 §P3 ComposerStoryCreateMutation / privacy save mutation).

    The CLI-facing member name maps onto the server's enum value
    (PUBLIC→EVERYONE, PRIVATE→SELF); the wire never sees the CLI spelling.
    """

    PUBLIC = C.PRIVACY_BASE_STATES["PUBLIC"]      # "EVERYONE"
    FRIENDS = C.PRIVACY_BASE_STATES["FRIENDS"]    # "FRIENDS"
    PRIVATE = C.PRIVACY_BASE_STATES["PRIVATE"]    # "SELF"

    @classmethod
    def from_name(cls, name: str) -> Privacy:
        """Case-insensitive CLI-name lookup (public, friends, private).

        Args:
            name: User-supplied privacy name; whitespace-stripped and
                upper-cased before the table lookup.

        Returns:
            The matching member, whose ``.value`` is the
                ``audience.privacy.base_state`` wire literal.

        Raises:
            UnknownNameError: When no privacy matches — the message lists
                every valid name.
        """
        try:
            return cls(C.PRIVACY_BASE_STATES[name.strip().upper()])
        except KeyError:
            raise UnknownNameError(
                f"unknown privacy {name!r} — expected one of: "
                + ", ".join(m.name for m in cls)) from None


def _b64decode_pad(raw: str) -> bytes:
    """Base64-decode a wire id, tolerating stripped ``=`` padding.

    GraphQL global ids arrive unpadded (docs/04 §7 identifier spaces);
    this restores the padding before ``b64decode`` instead of using
    ``validate=False`` leniency, so malformed ids still raise rather than
    silently decoding to garbage.
    """
    return base64.b64decode(raw + "=" * (-len(raw) % 4))


class FeedbackID(BaseModel):
    """A UFI feedback context — the target of reactions and comments.

    The reaction/comment handle the mutation protocol actually consumes
    (docs/04 §6): an opaque base64 global id, canonically the encoding of
    ``feedback:<numeric_story_fbid>`` (the ``ZmVlZGJhY2s6…`` prefix). The
    server binds the story/post/share ids server-side, so this token is
    the only address fbk needs for any UFI action.
    """

    raw: str = Field(..., description="base64 global id, e.g. 'ZmVlZGJhY2s6…'")

    @field_validator("raw")
    @classmethod
    def _check(cls, v: str) -> str:
        """Reject the empty string — an absent feedback id is caller error,
        not a valid handle, and must fail at the model boundary."""
        if not v:
            raise ValueError("empty feedback id")
        return v

    @classmethod
    def from_b64(cls, raw: str) -> FeedbackID:
        """Build from the wire form ('ZmVlZGJhY2s6…' — usually unpadded).

        Also accepts the already-decoded ``feedback:<numeric>`` spelling
        (some payloads ship the prefix form) and normalizes through
        :meth:`from_numeric` so both inputs yield the identical wire token.
        """
        if raw.startswith("feedback:"):
            return cls.from_numeric(raw.split(":", 1)[1])
        return cls(raw=raw)

    @classmethod
    def from_numeric(cls, numeric: str) -> FeedbackID:
        """Build from the legacy numeric id ('3937024329774036').

        Encodes ``feedback:<numeric>`` to unpadded base64 — the canonical
        wire form the real client ships in ``feedback_id`` inputs
        (docs/04 §6).
        """
        return cls(raw=base64.b64encode(f"feedback:{numeric}".encode()).decode().rstrip("="))

    @property
    def numeric(self) -> str:
        """The legacy numeric id ('3937024329774036').

        Fail-soft by design: modern tokens may use other prefixes — the
        rule is "try b64-decode first, treat failure as opaque"
        (docs/04 §6) — so a non-decodable raw falls back to itself rather
        than raising deep inside pagination code.
        """
        try:
            return _b64decode_pad(self.raw).decode("utf-8", "replace").split(":", 1)[1]
        except Exception:
            return self.raw

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


class CommentID(BaseModel):
    """A comment's global id: base64 of 'comment:<post_fbid>_<comment_fbid>'.

    The delete-mutation handle (docs/15 §P3-3): opaque on the wire, but
    the two-component numeric anatomy is recoverable for display and for
    walking ``author.id == c_user`` when locating one's own comments.
    """

    raw: str

    @classmethod
    def from_b64(cls, raw: str) -> CommentID:
        """Build from the wire form (or the decoded 'comment:…' spelling).

        A ``comment:``-prefixed input is split on the ``_`` separator and
        re-encoded via :meth:`from_numeric`, so both spellings normalize
        to the identical wire token.
        """
        if raw.startswith("comment:"):
            post, _, cid = raw.split(":", 1)[1].rpartition("_")
            return cls.from_numeric(post, cid)
        return cls(raw=raw)

    @classmethod
    def from_numeric(cls, post_id: str, comment_id: str) -> CommentID:
        """Build from the (post_fbid, comment_fbid) numeric pair.

        Encodes ``comment:<post>_<comment>`` to unpadded base64 — the
        canonical wire form (docs/15 §P3-3 locate/delete path).
        """
        inner = f"comment:{post_id}_{comment_id}"
        return cls(raw=base64.b64encode(inner.encode()).decode().rstrip("="))

    @property
    def decoded(self) -> tuple[str, str]:
        """(post_fbid, comment_fbid) from the global id.

        Fail-soft like FeedbackID.numeric: a non-b64 raw yields ("", raw)
        instead of a raw binascii.Error deep in parsing.
        """
        try:
            inner = _b64decode_pad(self.raw).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ("", self.raw)
        nums = inner.split(":", 1)[-1]
        post, _, cid = nums.rpartition("_")
        return (post, cid) if post else ("", nums)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


class StoryKey(BaseModel):
    """Opaque Relay story key ('UzpfSUZTOjE6…') — the base64 story id
    GraphQL connections ship (docs/15 §P2-2); replayed verbatim, never
    decoded."""

    raw: str


class Cursor(BaseModel):
    """Opaque connection cursor (page_info.end_cursor).

    The Relay pagination token (docs/04 §5 pattern 1): echo it verbatim
    as the next call's ``cursor`` variable — it embeds internal ids and
    offsets, and partial-decode-and-rebuild is a fragile technique, so
    this model exists to type the pass-through, not to parse it."""

    raw: str


# ----------------------------------------------------------------------------- feed
class Actor(BaseModel):
    """A story/message author stub: the id + name pair every surface
    payload carries in its ``actors`` arrays (docs/04 §9.1)."""

    id: str
    name: str | None = None
    typename: str | None = Field(None, alias="__typename")


class Feedback(BaseModel):
    """UFI context attached to a story.

    The per-story reaction/comment counters plus the :class:`FeedbackID`
    handle every UFI mutation consumes (docs/04 §6).
    """

    id: FeedbackID
    reaction_count: int | None = None
    comment_count: int | None = None


class Story(BaseModel):
    """A feed story (docs/04 §5 taxonomy).

    The decoded, surface-agnostic view of one feed edge: identity keys
    (StoryKey/id), the UFI handle, the author, and display fields. Every
    surface walker emits these so commands render uniform rows.
    """

    id: str | None = None
    key: StoryKey | None = None
    feedback: Feedback | None = None
    actor: Actor | None = None
    text: str | None = None
    permalink: str | None = None
    creation_time: int | None = None


class FeedPage(BaseModel):
    """One page of feed plus pagination plumbing.

    ``end_cursor``/``has_next_page`` are the Relay ``page_info`` echo
    (docs/04 §5 pattern 1): the cursor rides the next call verbatim;
    ``raw_size`` carries the response's byte weight for journal/telemetry
    context (docs/11 §7 journal discipline).
    """

    stories: list[Story] = Field(default_factory=list)
    end_cursor: str | None = None
    has_next_page: bool = False
    raw_size: int = 0


class Comment(BaseModel):
    """A decoded comment node: global id (:class:`CommentID`), author,
    and text — the row shape the comment surfaces and the delete/locate
    paths (docs/15 §P3-3) exchange."""

    id: CommentID
    actor: Actor | None = None
    text: str | None = None


# ----------------------------------------------------------------------------- user
class User(BaseModel):
    """A minimal user node: numeric uid + display name."""

    id: str
    name: str | None = None
    is_self: bool = False


class Profile(BaseModel):
    """Own profile as known from the bootstrap (CurrentUserInitialData).

    Carries the raw identity frame so commands can surface arbitrary
    fields without re-parsing the bootstrap (docs/15 §2 identity shape).
    """

    user: User
    is_business: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------------- messenger
class ThreadSummary(BaseModel):
    """One Messenger thread row: id, title, preview snippet, unread count,
    participant names, and the self/AI-chat flag (docs/15 §P7-3 — the AI
    thread is the only plaintext-sendable target)."""

    id: str
    name: str | None = None
    snippet: str | None = None
    unread: int = 0
    participants: list[str] = Field(default_factory=list)
    is_self_thread: bool = False


class Message(BaseModel):
    """A decoded chat message (own/AI-thread reads; P2P history is E2EE —
    docs/15 §P7-2 — and surfaces as an honest protocol-rejection, never
    as fake plaintext rows)."""

    id: str | None = None
    thread_id: str | None = None
    sender: Actor | None = None
    text: str | None = None
    timestamp_ms: int | None = None


# -------------------------------------------------------------------- groups & pages
class Group(BaseModel):
    """A group node: numeric gid (still fbids in the groups space,
    docs/14 §2), title, privacy, and member count."""

    id: str
    name: str | None = None
    privacy: str | None = None
    member_count: int | None = None
    url: str | None = None


class Page(BaseModel):
    """A page node: id, title, canonical URL, and the viewer's like
    state (the toggle CometPageLikeCommitMutation flips, docs/15 §P2-3
    family)."""

    id: str
    name: str | None = None
    url: str | None = None
    is_liked: bool | None = None


# ----------------------------------------------------------------------------- search
class SearchResult(BaseModel):
    """One typed search node (Group/User/Page/Story/Video — docs/15 §P3-5
    replay yields typed nodes cleanly extractable from the payload)."""

    typename: str
    name: str | None = None
    id: str | None = None
    url: str | None = None
    snippet: str | None = None


class SearchResponse(BaseModel):
    """A search result page: the query echo, the typed rows, and the
    response's byte weight (``raw_size``) for journal context."""

    query: str
    results: list[SearchResult] = Field(default_factory=list)
    raw_size: int = 0


# ---------------------------------------------------------------------- notifications
class NotificationCount(BaseModel):
    """The badge counters (unseen + important-only) from the live-verified
    badge query (docs/15 §3, docs/15 §P6-3)."""

    unseen: int
    unseen_important: int | None = None


class Notification(BaseModel):
    """One notification row: id, typename, and rendered title/body."""

    id: str | None = None
    typename: str | None = None
    title: str | None = None
    body: str | None = None
    unseen: bool = False


# --------------------------------------------------------------------------- settings
class PrivacyState(BaseModel):
    """Default post-privacy state (settings/privacy surface).

    The live-captured shape of the ``privacy_row_input`` the
    CometPrivacySelectorSavePrivacyMutation consumes (docs/15 §P3):
    ``base_state`` is the :class:`Privacy` wire literal, ``allow``/``deny``
    are per-list actor expansions, and ``tag_expansion`` defaults to the
    observed ``TAGGEES`` literal.
    """

    base_state: str
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    tag_expansion: str = "TAGGEES"
