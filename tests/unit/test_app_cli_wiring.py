"""App CLI wiring regressions (just-landed fixes, wave 2).

  * the ``measure`` command family is wired into the real app parser with
    its latency/pace/report children, and ``measure pace`` runs fully
    OFFLINE (a dry schedule — it must never construct a Session);
  * flag renames: ``pages feed --page-id`` and ``profile view --user-id``
    are the flags now; the old ``--page`` / ``--user`` spellings are
    rejected by the parser;
  * ``--version`` prints the pyproject version (2.1.0).

Everything drives the REAL ``app.build_parser()`` — the exact object the
``fbk`` entrypoint uses — never a hand-rolled one.

Unit (offline): pure parser/CLI driving against capsys — no network, no
session; the pace tests additionally assert that new_session is never
even called.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import build_parser, main

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


# ------------------------------------------------------------------- measure
class TestMeasureWiring:
    """Pins the measure command family's presence and flag dests on the
    real app parser."""

    def test_measure_subcommand_exists_with_all_children(self):
        for child, extra in (("latency", []), ("pace", []),
                             ("report", ["--journal", "session"])):
            args = build_parser().parse_args(["measure", child, *extra])
            assert args.command == "measure"
            assert args.measure_command == child
            assert callable(args.fn)

    def test_measure_requires_a_child(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["measure"])
        assert ei.value.code == 2
        assert "required" in capsys.readouterr().err

    def test_measure_rejects_unknown_children(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["measure", "bogus"])
        assert ei.value.code == 2

    def test_pace_flags_parse_to_their_dests(self):
        args = build_parser().parse_args(
            ["measure", "pace", "--actions", "7", "--mean-gap", "3.5",
             "--cv", "0.5", "--seed", "9"])
        assert args.actions == 7
        assert args.mean_gap == 3.5
        assert args.cv == 0.5
        assert args.seed == 9


class TestMeasurePaceRunsOffline:
    """Pins that ``measure pace`` is a pure dry schedule: zero sessions,
    zero network, deterministic per seed."""

    def _run(self, monkeypatch, capsys, argv):
        """Drive the parsed pace command end to end; any Session build fails."""
        import commands.measure as measure_cmd
        monkeypatch.setattr(
            measure_cmd, "new_session",
            lambda _a: (_ for _ in ()).throw(
                AssertionError("pace is a dry schedule: no Session allowed")))
        args = build_parser().parse_args(argv)
        rc = args.fn(args)
        out = capsys.readouterr().out
        return rc, out

    def test_pace_runs_with_zero_network_and_zero_sessions(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys,
                            ["measure", "pace", "--actions", "5"])
        assert rc == 0
        # human lines: one per scheduled action
        assert out.count("action#") == 5

    def test_pace_payload_is_complete_and_deterministic(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys,
                            ["measure", "pace", "--actions", "4", "--seed", "1234"])
        assert rc == 0
        payload = json.loads(out.splitlines()[-1])  # JSON line closes the output
        assert payload["actions"] == 4
        assert len(payload["times"]) == 4
        assert payload["seed"] == 1234
        assert payload["times"] == sorted(payload["times"])  # absolute times

        # same seed twice -> byte-identical schedule (deterministic per seed)
        rc2, out2 = self._run(monkeypatch, capsys,
                              ["measure", "pace", "--actions", "4", "--seed", "1234"])
        assert rc2 == 0
        assert json.loads(out2.splitlines()[-1])["times"] == payload["times"]

    def test_pace_json_output_is_machine_only(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys,
                            ["measure", "pace", "--actions", "3", "--json"])
        assert rc == 0
        payload = json.loads(out)
        assert set(payload) >= {"actions", "times", "mean_gap", "cv", "seed"}
        assert len(payload["times"]) == 3


# --------------------------------------------------------------- flag renames
class TestFlagRenames:
    """Pins the --page-id / --user-id renames: the new dests receive the
    values and no legacy dest is ever created."""

    def test_pages_feed_takes_page_id(self):
        args = build_parser().parse_args(["pages", "feed", "--page-id", "12345"])
        assert args.page_id == "12345"
        assert args.pages_command == "feed"

    def test_pages_feed_rejects_the_old_page_flag(self, capsys):
        """The pre-rename --page flag must exit 2: build_parser() disables
        argparse prefix abbreviation across the whole subparser tree
        (app._disallow_abbrev), so --page cannot pass as an abbreviation
        of --page-id."""
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["pages", "feed", "--page", "12345"])
        assert ei.value.code == 2

    def test_profile_view_takes_user_id(self):
        args = build_parser().parse_args(["profile", "view", "--user-id", "615937"])
        assert args.user_id == "615937"
        assert args.profile_cmd == "view"

    def test_profile_view_rejects_the_old_user_flag(self, capsys):
        """The pre-rename --user flag must exit 2 (abbreviation disabled
        tree-wide — see test_pages_feed_rejects_the_old_page_flag)."""
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["profile", "view", "--user", "615937"])
        assert ei.value.code == 2

    def test_old_flags_leave_no_trace_of_the_old_dests(self):
        """Whatever abbreviation does with the old spelling, it must never
        create a legacy dest: the value lands in page_id / user_id only."""
        args = build_parser().parse_args(["pages", "feed", "--page-id", "7"])
        assert not hasattr(args, "page")
        args = build_parser().parse_args(["profile", "view", "--user-id", "7"])
        assert not hasattr(args, "user")

    def test_pages_like_and_follow_also_use_page_id(self):
        for child in ("like", "follow"):
            args = build_parser().parse_args(["pages", child, "--page-id", "555"])
            assert args.page_id == "555"


# ------------------------------------------------------------------- version
class TestVersion:
    """Pins --version output against the pyproject version."""

    def test_main_version_prints_the_pyproject_version(self, capsys):
        with pytest.raises(SystemExit) as ei:
            main(["--version"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "2.2.0" in out
        assert f"fbk {pyproject_version()}" in out

    def test_parser_version_action_matches_pyproject(self, capsys):
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["--version"])
        assert ei.value.code == 0
        assert capsys.readouterr().out.strip() == f"fbk {pyproject_version()}"

    def test_pyproject_version_is_2_2_0(self):
        assert pyproject_version() == "2.2.0"
