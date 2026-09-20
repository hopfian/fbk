"""Domain models: typed Pydantic entities for every surface.

Each model documents the Facebook entity it represents and its role in
serialization (the §4 standard); identifier semantics follow docs/14 §2
and are encoded once in the FeedbackID/CommentID codecs.

Public API (docs/14):
  * Identifiers: FeedbackID, CommentID (with b64 codecs)
  * Enums: ReactionType (7 reactions; removal is the id-0 sentinel),
    Privacy (3 base states)
  * Feed: Story, Feedback, FeedPage, Comment, Actor
  * Messenger: ThreadSummary, Message
  * Social: Group, Page, User, Profile, SearchResult
  * Notifications: Notification, NotificationCount
  * Settings: PrivacyState
  * Cursors: Cursor, StoryKey
  * UnknownNameError: typed from_name() miss

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .common import (
    Actor,
    Comment,
    CommentID,
    Cursor,
    Feedback,
    FeedbackID,
    FeedPage,
    Group,
    Message,
    Notification,
    NotificationCount,
    Page,
    Privacy,
    PrivacyState,
    Profile,
    ReactionType,
    SearchResponse,
    SearchResult,
    Story,
    StoryKey,
    ThreadSummary,
    UnknownNameError,
    User,
)

__all__ = [
    "Actor",
    "Comment",
    "CommentID",
    "Cursor",
    "FeedPage",
    "Feedback",
    "FeedbackID",
    "Group",
    "Message",
    "Notification",
    "NotificationCount",
    "Page",
    "Privacy",
    "PrivacyState",
    "Profile",
    "ReactionType",
    "SearchResponse",
    "SearchResult",
    "Story",
    "StoryKey",
    "ThreadSummary",
    "UnknownNameError",
    "User",
]
