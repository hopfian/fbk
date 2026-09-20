"""Pins the render-layer hoist (wave 3): the shared story renderers live
in commands.render and all four timeline command families resolve their
helpers through it — the old cross-module import of commands.feed's
PRIVATE helpers (profile/groups/pages `from .feed import _print_page,
_story_payload`) was a layering smell flagged twice.

The render output bytes themselves stay pinned by the existing
feed/profile/groups/pages command tests (the "-- N stories" lines etc.);
this file pins the WIRING: one owner module, no private copies left.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commands.feed as feed_cmd
import commands.groups as groups_cmd
import commands.pages as pages_cmd
import commands.profile as profile_cmd
from commands.render import print_stories, print_story_page, story_payload


class TestRenderOwnership:
    """commands.render owns the shared renderers; every timeline family
    binds the same function objects (no duplicate or stale copies)."""

    def test_timeline_families_resolve_the_hoisted_helpers(self):
        for mod in (groups_cmd, pages_cmd, profile_cmd):
            assert mod.story_payload is story_payload
            assert mod.print_story_page is print_story_page
        # feed uses the aggregated-stories printer for multi-page walks
        assert feed_cmd.story_payload is story_payload
        assert feed_cmd.print_stories is print_stories

    def test_the_private_feed_copies_are_gone(self):
        for private in ("_story_payload", "_print_stories", "_print_page"):
            assert not hasattr(feed_cmd, private)
            assert not hasattr(groups_cmd, private)
            assert not hasattr(pages_cmd, private)
            assert not hasattr(profile_cmd, private)

    def test_no_command_module_imports_feed_privates(self):
        import commands

        assert "from .feed import" not in (
            Path(commands.profile.__file__).read_text(encoding="utf-8"))
        assert "from .feed import" not in (
            Path(commands.groups.__file__).read_text(encoding="utf-8"))
        assert "from .feed import" not in (
            Path(commands.pages.__file__).read_text(encoding="utf-8"))
