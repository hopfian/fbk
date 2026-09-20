"""fbk shell completion: the hidden ``_complete`` walker + bridge scripts.

Unit (offline): drives the REAL ``app.build_parser()`` with the
completions module registered the way ``app._COMMAND_MODULES`` will
wire it — LAST, so the hidden-verb usage-metavar snapshot covers every
family — and pins:

  * the walker at every depth: root families, family verbs, verb-level
    flags, ``--``-prefixed flag completion, prefix filtering, and the
    hidden-verb exclusion (``_complete`` never suggests itself);
  * the never-drift contract: candidates are DERIVED from the parser
    tree (registering the module adds exactly the public ``completions``
    verb, and walker output equals the tree's own visible choices);
  * the hidden verb staying out of ``fbk --help`` — choices listing
    AND usage braces — while still dispatching for the bridges;
  * the three static bridge scripts (self-contained, pure stdout, each
    delegating to ``fbk _complete`` and ending with an install hint).
"""
from __future__ import annotations

import argparse

import pytest

from app import build_parser
from commands.completions import (
    _COMMON_FLAGS,
    _HIDDEN_VERB,
    _SHELLS,
    _candidates,
)


def _root_sub(parser: argparse.ArgumentParser):
    """The root subparsers action of a built parser (stable argparse seam,
    same one app._disallow_abbrev walks)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("root parser has no subparsers action")


def _wired():
    """The real app parser — commands.completions is wired as the LAST
    _COMMAND_MODULES entry (the hidden-metavar snapshot requires last
    position, see commands/completions.py HIDDEN-VERB MECHANICS), so
    build_parser() alone yields the fully-wired tree."""
    return build_parser()


def _visible(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """The parser tree's own answer for what lives at one level: every
    choice name that is not a hidden "_"-prefixed verb, prefix-matched."""
    sub = _root_sub(parser)
    return sorted(name for name in sub.choices
                  if not name.startswith("_") and name.startswith(prefix))


# ------------------------------------------------------------ root level
class TestWalkerRootDepth:
    """Root-level completion: every public family, nothing else."""

    def test_root_lists_exactly_the_trees_visible_families(self):
        parser = _wired()
        # the never-drift pin: walker output == the tree's own choices
        assert _candidates(parser, [""]) == _visible(parser)

    def test_root_lists_36_families(self):
        """33 pre-existing families + `draft` + the public `completions`
        verb; the hidden `_complete` must not be a candidate."""
        out = _candidates(_wired(), [""])
        assert len(out) == 36
        assert "completions" in out
        assert "draft" in out
        assert "registry" in out

    def test_hidden_verb_is_never_a_candidate(self):
        parser = _wired()
        assert _HIDDEN_VERB not in _candidates(parser, [""])
        # typing "_com" must complete to nothing, not to the hidden verb
        assert _candidates(parser, ["_com"]) == []

    def test_root_prefix_filtering(self):
        parser = _wired()
        assert _candidates(parser, ["reg"]) == ["registry"]
        assert _candidates(parser, ["zzz"]) == []

    def test_bare_invocation_completes_the_first_token(self):
        parser = _wired()
        assert _candidates(parser, []) == _candidates(parser, [""])


# ---------------------------------------------------------- family level
class TestWalkerFamilyDepth:
    """Within a family: its verbs, prefix-filtered."""

    def test_family_lists_its_verbs(self):
        parser = _wired()
        assert _candidates(parser, ["registry", ""]) == ["audit", "diff",
                                                         "refresh"]

    def test_family_prefix_filtering(self):
        parser = _wired()
        assert _candidates(parser, ["friends", "req"]) == ["request",
                                                           "requests"]

    def test_family_candidates_equal_the_trees_choices(self):
        parser = _wired()
        sub = _root_sub(parser)
        registry = sub.choices["registry"]
        rsub = next(a for a in registry._actions
                    if isinstance(a, argparse._SubParsersAction))
        expected = sorted(rsub.choices)
        assert _candidates(parser, ["registry", ""]) == expected

    def test_unknown_context_word_keeps_the_reached_depth(self):
        """`fbk registry zzz <tab>`: a bogus word cannot descend, but the
        walker stays at the family node (the same rule that keeps flag
        values from changing depth) — its verbs are offered again."""
        assert _candidates(_wired(), ["registry", "zzz", ""]) == \
            ["audit", "diff", "refresh"]


# ------------------------------------------------------------- verb level
class TestWalkerVerbDepth:
    """Deepest level: no further names — flags are the candidates."""

    def test_verb_level_offers_flags_not_names(self):
        out = _candidates(_wired(), ["registry", "diff", ""])
        # every common plumbing flag is offered at a leaf
        assert set(_COMMON_FLAGS) <= set(out)
        # the verb's own flag is offered too
        assert "--limit" in out
        # and nothing that is not a flag (no deeper names exist)
        assert all(cand.startswith("-") for cand in out)

    def test_flag_words_do_not_change_depth(self):
        """A flag and its value in the completed context must not make the
        walker lose (or change) the leaf it has already reached."""
        out = _candidates(_wired(),
                          ["registry", "refresh", "--workers", "4", ""])
        assert "--workers" in out
        assert "--save" in out

    def test_words_past_a_leaf_stay_at_the_leaf(self):
        parser = _wired()
        assert _candidates(parser, ["registry", "diff", "bogus", ""]) == \
            _candidates(parser, ["registry", "diff", ""])


# --------------------------------------------------------- flag completion
class TestWalkerFlagCompletion:
    """``-``-prefixed partial token: flags at the reached level."""

    def test_root_double_dash_lists_globals_and_common_flags(self):
        """Root carries --debug/--help of its own (--version suppressed);
        the union with the always-appended common flags is exactly the
        common set."""
        assert _candidates(_wired(), ["--"]) == sorted(set(_COMMON_FLAGS))

    def test_double_dash_prefix_filters(self):
        parser = _wired()
        assert _candidates(parser, ["--j"]) == ["--json"]
        assert _candidates(parser, ["--no-"]) == ["--no-journal"]

    def test_version_and_short_help_are_suppressed_noise(self):
        out = _candidates(_wired(), ["--"])
        assert "--version" not in out
        assert "-h" not in out

    def test_family_node_still_offers_the_common_flags(self):
        """The `registry` node itself takes no options, but the operator
        mid-line still gets the common set (v1: always appended)."""
        assert _candidates(_wired(), ["registry", "--"]) == \
            sorted(set(_COMMON_FLAGS))

    def test_leaf_lists_its_own_flags_plus_common(self):
        out = _candidates(_wired(), ["doc-ids", "--"])
        assert {"--search", "--limit"} <= set(out)
        assert set(_COMMON_FLAGS) <= set(out)

    def test_single_dash_offers_long_flags_only(self):
        """A bare `-` prefix matches only long flags (-h is suppressed),
        so every candidate carries the double dash."""
        assert all(cand.startswith("--")
                   for cand in _candidates(_wired(), ["-"]))

    def test_global_flag_context_leaves_root_level(self):
        assert _candidates(_wired(), ["--debug", "reg"]) == ["registry"]


# ------------------------------------------------- never drifts from the tree
class TestNeverDriftsFromTheParserTree:
    """Registration must add EXACTLY the public verb — every pre-existing
    family keeps completing, none changes, none disappears."""

    def test_registration_adds_exactly_the_public_verb(self):
        """Landed state: commands.completions is wired as the LAST
        _COMMAND_MODULES entry. Registration must stay idempotent — the
        public verb appears exactly ONCE (a re-registration would raise
        argparse's conflicting-subparser ValueError), no other family
        changed, and the walker still mirrors the tree exactly."""
        wired = _wired()
        names = list(_root_sub(wired).choices)
        assert names.count("completions") == 1
        assert names.count(_HIDDEN_VERB) == 1
        assert _candidates(wired, [""]) == _visible(wired)


# ---------------------------------------------------- hidden from --help
class TestHiddenFromHelp:
    """The hidden verb must stay invisible in `fbk --help` (choices listing
    AND usage braces) while remaining dispatchable for the bridges."""

    def test_help_output_lacks_the_hidden_verb(self):
        text = _wired().format_help()
        assert _HIDDEN_VERB not in text
        assert "==SUPPRESS==" not in text  # the argparse wart, worked around
        assert "completions" in text  # the public verb IS listed

    def test_usage_braces_list_only_visible_families(self):
        parser = _wired()
        usage = parser.format_usage()
        assert _HIDDEN_VERB not in usage
        # the metavar snapshot mirrors argparse's own rendering order
        # (registration order, not sorted) minus the hidden verb
        sub = _root_sub(parser)
        expected = "{" + ",".join(name for name in sub.choices
                                 if not name.startswith("_")) + "}"
        assert expected in usage

    def test_hidden_verb_still_dispatches(self):
        args = _wired().parse_args([_HIDDEN_VERB, "--", "registry", "d"])
        assert args.words == ["registry", "d"]
        assert callable(args.fn)

    def test_root_choices_still_carry_the_hidden_verb(self):
        """The verb stays in the dispatch map — only the HELP surfaces were
        scrubbed (choices removal would break dispatch entirely)."""
        assert _HIDDEN_VERB in _root_sub(_wired()).choices


# ------------------------------------------------------- _complete handler
class TestCompleteHandler:
    """The hidden verb's handler end to end: pure, one candidate per line."""

    def test_handler_prints_one_candidate_per_line(self, capsys):
        args = _wired().parse_args([_HIDDEN_VERB, "--", "registry", "d"])
        assert args.fn(args) == 0
        assert capsys.readouterr().out.splitlines() == ["diff"]

    def test_handler_output_is_pure(self, capsys):
        args = _wired().parse_args([_HIDDEN_VERB, "--", "reg"])
        args.fn(args)
        assert capsys.readouterr().out == "registry\n"

    def test_handler_root_lists_families_purely(self, capsys):
        """The handler rebuilds the REAL parser via app.build_parser() —
        the tree app._COMMAND_MODULES actually wires. Until the
        orchestrator appends commands.completions that is the bare tree,
        so this pins against build_parser() itself, never a hand list."""
        args = _wired().parse_args([_HIDDEN_VERB, "--", ""])
        assert args.fn(args) == 0
        lines = capsys.readouterr().out.splitlines()
        bare = build_parser()
        expected = sorted(name for name in _root_sub(bare).choices
                          if not name.startswith("_"))
        assert lines == expected
        assert all(line == line.strip() for line in lines)  # no padding noise

    def test_handler_completes_flags(self, capsys):
        args = _wired().parse_args([_HIDDEN_VERB, "--", "registry", "--js"])
        args.fn(args)
        assert capsys.readouterr().out == "--json\n"

    def test_handler_ignores_flag_value_context(self, capsys):
        args = _wired().parse_args(
            [_HIDDEN_VERB, "--", "doc-ids", "--limit", "5", "--s"])
        args.fn(args)
        assert capsys.readouterr().out == "--search\n"


# ------------------------------------------------------- bridge scripts
class TestBridgeScripts:
    """`fbk completions SHELL`: static, self-contained, pure-text scripts."""

    @pytest.mark.parametrize("shell,needles", [
        ("bash", ("complete -", "fbk _complete", "compgen")),
        ("zsh", ("compdef", "fbk _complete")),
        ("powershell", ("Register-ArgumentCompleter", "fbk _complete")),
    ])
    def test_script_shape_and_purity(self, shell, needles, capsys):
        args = _wired().parse_args(["completions", shell])
        assert args.fn(args) == 0
        out = capsys.readouterr().out
        for needle in needles:
            assert needle in out, f"{shell} script lacks {needle!r}"
        # pure text: no JSON envelope, no emit artifacts
        assert not out.lstrip().startswith("{")
        assert out.endswith("\n")

    def test_shell_choices_are_exactly_the_three_bridges(self):
        assert tuple(_SHELLS) == ("bash", "zsh", "powershell")

    def test_template_lines_fit_100_columns(self):
        """ruff counts template lines like any source line (E501 has no
        string exemption) — pin it so edits cannot silently overflow."""
        for name, template in _SHELLS.items():
            longest = max(len(line) for line in template.splitlines())
            assert longest <= 100, f"{name}: {longest}-column line"

    def test_every_template_ends_with_an_install_hint(self):
        for name, template in _SHELLS.items():
            lines = template.splitlines()
            assert "install hint" in lines[-2], name

    def test_unknown_shell_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as ei:
            _wired().parse_args(["completions", "fish"])
        assert ei.value.code == 2
        assert "fish" in capsys.readouterr().err

    def test_json_is_not_accepted_on_the_script_generator(self, capsys):
        """`--json` is meaningless here: the parser deliberately registers
        no common flags, so argparse rejects it (exit 2) instead of
        accepting a flag that lies."""
        with pytest.raises(SystemExit) as ei:
            _wired().parse_args(["completions", "bash", "--json"])
        assert ei.value.code == 2

    def test_scripts_are_self_contained(self):
        """No dependency on external helper scripts: each bridge only
        invokes `fbk _complete` (no sibling files, no curl of helpers)."""
        for template in _SHELLS.values():
            assert "fbk _complete" in template
            assert "http" not in template
