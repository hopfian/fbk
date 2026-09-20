"""Friends command dispatch (just-landed static-dispatch fix).

The five friending verbs (request/cancel/accept/decline/unfriend) resolve
through a static verb->handler table — mypy-checked at import time —
because the old runtime ``globals()`` lookup drifted silently when a
verb and its ``cmd_<verb>`` handler diverged. Pins parse-time wiring for
every friends subcommand: the verb resolves to exactly its handler.

Unit (offline): parser wiring only — no session is built, no network.
"""
from __future__ import annotations

import argparse

import pytest

from commands import friends as friends_cmd


def build_parser() -> argparse.ArgumentParser:
    """A root parser with only the friends family registered."""
    parser = argparse.ArgumentParser(prog="fbk-test")
    sub = parser.add_subparsers(dest="command", required=True)
    friends_cmd.register(sub)
    return parser


class TestVerbDispatch:
    """Each verb parses with --user-id and resolves to its own handler."""

    @pytest.mark.parametrize("verb, handler", [
        ("request", "cmd_request"),
        ("cancel", "cmd_cancel"),
        ("accept", "cmd_accept"),
        ("decline", "cmd_decline"),
        ("unfriend", "cmd_unfriend"),
    ])
    def test_verb_resolves_to_its_handler(self, verb, handler):
        args = build_parser().parse_args(
            ["friends", verb, "--user-id", "12345678901234"])
        assert args.fn.__name__ == handler
        assert args.user_id == "12345678901234"
        assert args.friends_command == verb

    def test_every_verb_fn_is_the_module_level_command(self):
        """The static table binds the very objects the module defines —
        no late-bound string lookups that can drift apart."""
        table = {"request": friends_cmd.cmd_request,
                 "cancel": friends_cmd.cmd_cancel,
                 "accept": friends_cmd.cmd_accept,
                 "decline": friends_cmd.cmd_decline,
                 "unfriend": friends_cmd.cmd_unfriend}
        for verb, fn in table.items():
            args = build_parser().parse_args(
                ["friends", verb, "--user-id", "1"])
            assert args.fn is fn, verb


class TestOtherSubcommands:
    """The read/auxiliary subcommands keep their own handlers."""

    @pytest.mark.parametrize("argv, handler", [
        (["friends", "list"], "cmd_list"),
        (["friends", "requests"], "cmd_requests"),
        (["friends", "suggestions"], "cmd_suggestions"),
        (["friends", "clear-badge"], "cmd_clear_badge"),
    ])
    def test_subcommand_resolves_to_its_handler(self, argv, handler):
        assert build_parser().parse_args(argv).fn.__name__ == handler


class TestParserEdges:
    """Required flags stay required; common plumbing flags ride along."""

    def test_user_id_is_required_on_verbs(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["friends", "request"])
        assert exc.value.code == 2  # argparse usage error

    def test_common_flags_accepted_on_verbs(self):
        args = build_parser().parse_args([
            "friends", "unfriend", "--user-id", "7",
            "--json", "--no-journal", "--retry", "2",
            "--root", "somewhere", "--cookies", "other.txt"])
        assert args.fn.__name__ == "cmd_unfriend"
        assert args.as_json is True
        assert args.no_journal is True
        assert args.retry == 2

    def test_requests_direction_flag_parses(self):
        args = build_parser().parse_args(
            ["friends", "requests", "--direction", "incoming"])
        assert args.fn.__name__ == "cmd_requests"
        assert args.direction == "incoming"

    def test_bogus_verb_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["friends", "befriend", "--user-id", "1"])
        assert exc.value.code == 2
