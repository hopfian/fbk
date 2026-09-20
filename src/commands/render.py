"""Shared story renderers for the timeline-reading command families.

Every surface that prints feed-shaped stories — feed, profile, groups,
pages — renders through this module so a story prints identically no
matter which surface produced it. Hoisted verbatim from commands/feed
(the prior cross-module import of that family's private helpers was a
layering smell): the render layer is shared plumbing, not feed-owned.

Output here is human-decoration only (docs/12 §2: the payload on
stdout is the contract; these lines ride ahead of the compact JSON via
commands.common.emit's ``human`` hook and tests assert on them).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from typing import Any

from domain.common import FeedPage, Story


def story_payload(story: Story) -> dict[str, Any]:
    """JSON-safe projection of one Story for emit().

    Carries the fields downstream commands need to chain further
    operations: the raw opaque story key, actor name, text, the
    feedback id (the react/unreact/comment target handle), permalink,
    and creation time.
    """
    return {
        "id": story.id,
        "key": story.key.raw if story.key else None,
        "actor": story.actor.name if story.actor else None,
        "text": story.text,
        "feedback_id": str(story.feedback.id) if story.feedback else None,
        "permalink": story.permalink,
        "creation_time": story.creation_time,
    }


def print_stories(stories: list[Story], page: FeedPage) -> None:
    """Human output: one line per story — actor, text head, feedback id,
    permalink — then the page summary line (stories shown, pagination)."""
    for story in stories:
        actor = story.actor.name if story.actor else "?"
        head = (story.text or "").replace("\n", " ")[:60]
        fid = str(story.feedback.id) if story.feedback else "-"
        print(f"{actor} | {head} | {fid} | {story.permalink or ''}")
    print(f"-- {len(stories)} stories | has_next_page={page.has_next_page} "
          f"| end_cursor={(page.end_cursor or '')[:32]}")


def print_story_page(page: FeedPage) -> None:
    """Human output for a single fetched page."""
    print_stories(page.stories, page)
